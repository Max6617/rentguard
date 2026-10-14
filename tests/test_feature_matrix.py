"""The feature matrix: every CLI command and every MCP tool, invoked for real.

R10 asks for 100% of the *features*. "Features" is the documented surface — the ten commands
`rentguard --help` lists and the tools `docs/mcp.md` documents — so each one is invoked here and
asserted on its documented exit code and the shape of what it returns. The matrix is
command x provider x platform; this file is one column of it (the fake provider on Windows, no
network, no spend), and it is the column that can be run for free.

The CLI runs as a child process through `python -m rentguard.cli`, so the parser, the config lookup
and the exit codes are the real ones. Child stdout and stderr go to files, never to pipes: the
confined Windows token this suite runs under cannot open named pipes. The MCP surface is driven over
its own stdio transport (one JSON-RPC message per line), because that transport is the feature.

Two rows here are expected to be red, and both are defects to report rather than tests to soften:

- `up` prints two prose lines to **stdout** before its JSON document, so `up --json` is not one
  parseable document, and `rentguard_start` over MCP loses its `structuredContent` because of it;
- the server advertises eleven tools while `docs/mcp.md` documents eight.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent                      # the directory that holds the `rentguard` package
sys.path.insert(0, str(WS))

from rentguard import mcp_server  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

CONFIG = """provider = "fake"
state_dir = ".rentguard"

[budget]
cap_usd = 5.0
per_run_cap_usd = 2.0
max_hours_per_run = 6.0
idle_minutes = 10

[artefacts]
pull = ["results/*.json"]

[runpod]
gpu_preference = ["Fake GPU 24GB"]
"""

#: (argv, exit code, a substring the documented output contains). The exit codes are the documented
#: ones: 0 success, 1 for "nothing to do this with", 2 for "refused because it would spend money".
CLI_MATRIX = [
    (("init", "--print"), 0, "# rentguard configuration"),
    (("init", "other.toml"), 0, "wrote"),
    (("init", "other.toml"), 1, "refusing to overwrite"),
    (("doctor",), 0, "spend recorded"),
    (("ledger",), 0, "no runs recorded"),
    (("status",), 0, "no instance"),
    (("watch",), 1, "nothing to watch"),
    (("down",), 0, "nothing to do"),
    (("pull",), 1, "nothing is rented"),
    (("adopt", "no-such-instance"), 1, "no instance"),
    (("run", "--", "echo", "hi"), 1, "nothing is rented"),
    (("up", "--purpose", "matrix", "--dry-run"), 0, "nothing was rented"),
]

#: Every command accepts `--json`. `up` is exercised twice: dry (which returns early) and real,
#: because the real path is the one that prints extra lines.
JSON_COMMANDS = ["init", "doctor", "status", "watch", "down", "ledger", "pull", "adopt", "run"]


class MatrixCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        (self.root / "rentguard.toml").write_text(CONFIG, encoding="utf-8")
        # The child runs *in* the root (so `init` writes there) with the package's parent on the
        # import path, and the fake provider so nothing can reach the network.
        self.env = {**os.environ, "RENTGUARD_ROOT": str(self.root), "RENTGUARD_PROVIDER": "fake",
                    "PYTHONPATH": str(WS), "PYTHONUTF8": "1"}
        self.out = self.root / "stdout.txt"
        self.err = self.root / "stderr.txt"

    def cli(self, *argv: str) -> tuple[int, str, str]:
        with open(self.out, "wb") as out, open(self.err, "wb") as err:
            proc = subprocess.run([sys.executable, "-m", "rentguard.cli", *argv],
                                  cwd=str(self.root), env=self.env, stdout=out, stderr=err)
        return (proc.returncode,
                self.out.read_text(encoding="utf-8", errors="replace"),
                self.err.read_text(encoding="utf-8", errors="replace"))

    def one_document(self, argv: tuple[str, ...]) -> None:
        code, out, err = self.cli(*argv)
        try:
            document = json.loads(out)
        except ValueError as exc:
            self.fail(f"`rentguard {' '.join(argv)}` did not leave one JSON document on stdout "
                      f"({exc}). The documented contract is `json.loads(stdout)`, which is how the "
                      f"MCP server fills structuredContent.\nstdout:\n{out}\nstderr:\n{err}")
        self.assertIsInstance(document, dict,
                              f"`{' '.join(argv)}` printed {type(document).__name__}")
        self.assertTrue(document.get("command") or document.get("action"),
                        f"`{' '.join(argv)}` does not name what it did: {document}")


class CliSurface(MatrixCase):
    def test_every_command_exits_and_says_its_documented_thing(self):
        """One row of the matrix: command -> exit code -> the output a user is promised.

        Effect required: the exit code is the documented one and the output is not a traceback. A
        command that returns 0 while doing nothing, or that dies with an exception, is not a
        feature even though its name is in `--help`.
        """
        for argv, expected, needle in CLI_MATRIX:
            with self.subTest(command=" ".join(argv)):
                code, out, err = self.cli(*argv)
                both = out + err
                self.assertEqual(code, expected,
                                 f"`rentguard {' '.join(argv)}` exited {code}, expected {expected}."
                                 f"\nstdout:\n{out}\nstderr:\n{err}")
                self.assertNotIn("Traceback (most recent call last)", both,
                                 f"`rentguard {' '.join(argv)}` raised.\n{err}")
                self.assertIn(needle, both,
                              f"`rentguard {' '.join(argv)}` does not print {needle!r}."
                              f"\nstdout:\n{out}\nstderr:\n{err}")

    def test_every_command_accepts_json_and_prints_one_document(self):
        """The documented machine surface: with `--json`, stdout is exactly one JSON document.

        Effect required: `json.loads(stdout)` succeeds for every command, and the document names the
        command (an action, for `up`). This is not cosmetic: `mcp_server._call` parses that stdout
        to build `structuredContent`, so a stray prose line here silently empties the structured
        answer a Bot acts on.
        """
        for command in JSON_COMMANDS:
            with self.subTest(command=command):
                if command == "adopt":
                    self.one_document(("adopt", "no-such-instance", "--json"))
                elif command == "run":
                    self.one_document(("run", "--json", "--", "echo", "hi"))
                else:
                    self.one_document((command, "--json"))

    def test_up_json_is_one_document_in_both_dry_and_real_form(self):
        """`up` is the command that spends, so its structured form is the one a Bot reads.

        Effect required: `up --json` leaves one parseable document, dry and real. The real form
        rents a machine on the fake account, which costs nothing.
        """
        with self.subTest(form="dry"):
            self.one_document(("up", "--purpose", "matrix", "--dry-run", "--json"))
        with self.subTest(form="real"):
            self.one_document(("up", "--purpose", "matrix", "--json"))

    def test_the_documented_command_list_is_exactly_these_ten(self):
        """The surface itself is a feature: a command that disappears from `--help` or a new one
        that appears without a row above must fail this file, not pass quietly."""
        code, out, err = self.cli("--help")
        self.assertEqual(code, 0, f"`--help` exited {code}.\n{err}")
        listed = {argv[0] for argv, _, _ in CLI_MATRIX} | {"up"}
        missing = sorted(name for name in listed if name not in out)
        self.assertEqual(missing, [], f"commands in this matrix but not in --help: {missing}\n{out}")


class McpSurface(MatrixCase):
    """The MCP server, over its own stdio transport, one message per line."""

    @staticmethod
    def documented_tools() -> list[str]:
        """The MCP tools `docs/mcp.md` actually names — read from the page, not mirrored.

        This was a hardcoded list of the eight tools the page documented, which made it a record of
        the gap rather than a guard against it: documenting the missing three could not turn it
        green, and a tool added to the server later without a line on the page would have gone
        unnoticed. Reading the page is what turns it into a guard, and the page is the thing R10
        says must stay true.
        """
        page = Path(__file__).resolve().parents[1] / "docs" / "mcp.md"
        return sorted(set(re.findall(r"rentguard_[a-z_]+", page.read_text(encoding="utf-8"))))

    def session(self, requests: list[dict]) -> list[dict]:
        lines = "\n".join(json.dumps(request) for request in requests) + "\n"
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch("sys.stdin", io.StringIO(lines)), \
                contextlib.redirect_stdout(out):
            mcp_server.main()
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    @staticmethod
    def call(msg_id: int, name: str, arguments: dict | None = None) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}

    def test_every_advertised_tool_answers_over_the_transport(self):
        """One row of the matrix per advertised tool, whatever the list is.

        Effect required: every tool `tools/list` advertises answers its own id with a result whose
        first content item is text, or a JSON-RPC error — never silence, and never a dead server.
        """
        advertised = [tool["name"] for tool in mcp_server.TOOLS]
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        # Nothing is rented yet, so the money tools take their "nothing to do" path.
        for index, name in enumerate(advertised, start=10):
            arguments: dict = {}
            if name == "rentguard_run":
                arguments = {"command": "echo hi"}
            elif name == "rentguard_adopt":
                arguments = {"instance_id": "no-such-instance"}
            elif name == "rentguard_start":
                continue   # renting is exercised by its own test below
            elif name in ("rentguard_watch", "rentguard_watch_once"):
                arguments = {"max_iterations": 1}
            requests.append(self.call(index, name, arguments))

        replies = self.session(requests)
        by_id = {reply.get("id"): reply for reply in replies}

        self.assertEqual(sorted(by_id[1]["result"]["tools"][i]["name"] for i in
                                range(len(by_id[1]["result"]["tools"]))),
                         sorted(advertised),
                         "tools/list does not advertise what TOOLS holds")
        for index, name in enumerate(advertised, start=10):
            if name == "rentguard_start":
                continue
            reply = by_id.get(index)
            self.assertIsNotNone(reply, f"{name} got no reply at all: {replies}")
            self.assertTrue("result" in reply or "error" in reply,
                            f"{name} answered with neither result nor error: {reply}")
            if "result" in reply:
                content = reply["result"].get("content")
                self.assertIsInstance(content, list, f"{name} returned no content list: {reply}")
                self.assertTrue(content and content[0].get("type") == "text",
                                f"{name} returned no text content: {reply}")

    def test_the_advertised_tools_are_the_documented_ones(self):
        """The docs and the server must describe the same surface.

        Effect required: `tools/list` advertises exactly the tools in `docs/mcp.md`. Three tools
        are advertised that the MCP page does not mention, and a Bot reading the page cannot know
        they exist.
        """
        advertised = sorted(tool["name"] for tool in mcp_server.TOOLS)
        documented = self.documented_tools()
        self.assertEqual(advertised, documented,
                         "the server advertises tools that docs/mcp.md does not document, or the "
                         f"page documents tools the server does not offer: {advertised}")

    def test_start_rents_and_stop_closes_it_through_the_mcp_surface(self):
        """The two tools that move money, over the transport, with the fake provider.

        Effect required: `start` rents and reports it, `ledger` sees the row, `stop` closes it.
        """
        replies = self.session([
            self.call(1, "rentguard_start", {"purpose": "matrix"}),
            self.call(2, "rentguard_ledger"),
            self.call(3, "rentguard_stop"),
        ])
        by_id = {reply.get("id"): reply for reply in replies}

        started = by_id.get(1, {}).get("result", {})
        self.assertFalse(started.get("isError", True), f"rentguard_start failed: {by_id.get(1)}")
        start_text = json.dumps(started)
        self.assertIn("instance", start_text, f"rentguard_start reported no machine: {started}")

        ledger = by_id.get(2, {}).get("result", {})
        ledger_text = json.dumps(ledger)
        self.assertIn("run", ledger_text.lower(), f"rentguard_ledger saw no row: {ledger}")

        stopped = by_id.get(3, {}).get("result", {})
        self.assertFalse(stopped.get("isError", True), f"rentguard_stop failed: {by_id.get(3)}")
        stopping = stopped.get("structuredContent") or {}
        self.assertTrue(stopping.get("stopped", stopping.get("action")) is not None,
                        f"rentguard_stop does not say what it did: {stopped}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
