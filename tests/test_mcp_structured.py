"""The MCP tool result must carry structured data, not only text.

The design rule is "return the structured data alongside its text". An agent that has to parse prose to
find a cost will eventually parse it wrong, and this is the layer where the wrong number would be
acted on.

No network, no spend: the fake provider exports its account, and the server is driven in-process.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import mcp_server  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class McpReturnsStructuredData(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        (self.root / "rentguard.toml").write_text(
            'provider = "fake"\n[budget]\ncap_usd = 10.0\nper_run_cap_usd = 5.0\n',
            encoding="utf-8")
        state = self.root / ".rentguard" / "fake-state.json"
        fake = FakeProvider(rate_usd_per_hour=2.0, state_path=state)
        fake.add("pod-A", uptime_seconds=600)
        fake.save()
        self._env = dict(os.environ)
        os.environ["RENTGUARD_ROOT"] = str(self.root)
        os.environ["RENTGUARD_PROVIDER"] = "fake"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_dispatch_returns_text_and_a_structured_object(self) -> None:
        text, structured, is_error = mcp_server.dispatch("rentguard_status", {})

        self.assertFalse(is_error)
        self.assertIsInstance(structured, dict, "no structured data was returned")
        self.assertEqual(structured.get("command"), "status")
        self.assertIn("spent", structured)
        self.assertIsInstance(text, str)

    def test_a_tools_call_response_carries_structured_content(self) -> None:
        """Drive the real handler and read the JSON-RPC response it writes."""
        response: dict = {}

        def capture(msg_id, result=None, error=None) -> None:
            response["id"] = msg_id
            response["result"] = result
            response["error"] = error

        original = mcp_server._respond
        mcp_server._respond = capture
        try:
            mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "rentguard_ledger", "arguments": {}}})
        finally:
            mcp_server._respond = original

        result = response.get("result") or {}
        self.assertIn("content", result)
        self.assertEqual(result.get("isError"), False)
        structured = result.get("structuredContent")
        self.assertIsInstance(structured, dict,
                              "the tool result carried text but no structuredContent")
        self.assertEqual(structured.get("command"), "ledger")
        self.assertIn("runs", structured)

    def test_an_unknown_tool_is_an_error_without_structured_data(self) -> None:
        text, structured, is_error = mcp_server.dispatch("rentguard_nonsense", {})
        self.assertTrue(is_error)
        self.assertIsNone(structured)
        self.assertIn("unknown tool", text)

    def test_the_tool_result_is_serialisable(self) -> None:
        """Whatever is put in the response has to survive the JSON-RPC writer."""
        _, structured, _ = mcp_server.dispatch("rentguard_status", {})
        json.dumps({"structuredContent": structured})


if __name__ == "__main__":
    unittest.main()
