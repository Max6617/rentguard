"""The MCP protocol surface: negotiation, notifications, errors, and the stdio loop.

An MCP tool schema and a protocol reply are payloads another process reads, so a wrong one is the
same class of bug as an invented provider fixture. These cover the replies an agent's client acts
on, and the stdio loop's refusal to die on a stray line.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import mcp_server  # noqa: E402


def session(lines: str) -> list[dict]:
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(lines)), contextlib.redirect_stdout(out):
        mcp_server.main()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def request(payload: dict) -> str:
    return json.dumps(payload) + "\n"


class Negotiation(unittest.TestCase):
    def test_initialize_echoes_a_supported_protocol_version(self):
        replies = session(request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}}))
        result = replies[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "rentguard")
        self.assertIn("tools", result["capabilities"])

    def test_initialize_falls_back_to_the_latest_known_version(self):
        replies = session(request({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                   "params": {"protocolVersion": "1999-01-01"}}))
        self.assertEqual(replies[0]["result"]["protocolVersion"], mcp_server.LATEST)

    def test_a_notification_gets_no_reply_and_ping_does(self):
        replies = session(request({"jsonrpc": "2.0", "method": "notifications/initialized"})
                          + request({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
        self.assertEqual(len(replies), 1, f"a notification was answered: {replies}")
        self.assertEqual(replies[0]["id"], 3)
        self.assertEqual(replies[0]["result"], {})

    def test_an_unknown_method_is_answered_with_method_not_found(self):
        replies = session(request({"jsonrpc": "2.0", "id": 4, "method": "no/such/method"}))
        self.assertEqual(replies[0]["error"]["code"], -32601)


class StdioLoop(unittest.TestCase):
    def test_blank_malformed_and_non_object_lines_do_not_kill_the_server(self):
        lines = ("\n"                                       # blank
                 + "not json at all\n"                      # malformed
                 + "[1, 2, 3]\n"                            # valid JSON, not an object
                 + "42\n"                                   # valid JSON, a number
                 + request({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}))
        replies = session(lines)
        self.assertEqual(replies[0]["id"], 5, f"the server stopped answering: {replies}")
        self.assertTrue(replies[0]["result"]["tools"])

    def test_the_module_runs_as_a_stdio_server(self):
        """The entry point itself: a client starts it and closes stdin, and it exits cleanly."""
        proc = subprocess.run([sys.executable, "-m", "rentguard.mcp_server"], input="",
                              capture_output=True, text=True, cwd=str(WS),
                              env={**os.environ, "PYTHONPATH": str(WS)})
        self.assertEqual(proc.returncode, 0, f"the server exited {proc.returncode}: {proc.stderr[-300:]}")


class CallCapture(unittest.TestCase):
    def test_a_systemexit_code_becomes_the_tool_result(self):
        def refuse(args):
            raise SystemExit(3)

        text, structured, code = mcp_server._call(refuse)

        self.assertEqual(code, 3, "a SystemExit code was lost")
        self.assertIsNone(structured)

    def test_stdout_that_is_not_json_leaves_no_structured_result(self):
        def chatty(args):
            print("this is prose, not a document")
            return 0          # a command returns its exit code, as the real ones do

        text, structured, code = mcp_server._call(chatty)

        self.assertIsNone(structured, "prose was accepted as a structured result")
        self.assertIn("prose", text)
        self.assertEqual(code, 0)


class Dispatch(unittest.TestCase):
    def test_run_without_a_command_is_refused_before_anything_runs(self):
        text, structured, is_error = mcp_server.dispatch("rentguard_run", {"command": "   "})

        self.assertTrue(is_error, "an empty command was accepted")
        self.assertIn("command is required", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
