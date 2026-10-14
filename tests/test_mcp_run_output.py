"""What `run` gives back over MCP: the command's output, and only one document on the protocol.

**The rule.** ``mcp_server.py``'s own first lines say nothing may be written to stdout but JSON-RPC,
because stdout *is* the protocol stream. Two things broke that for `run`:

* ``cmd_run`` passed no ``stdout=`` to the child, and ``subprocess`` inherits the **file
  descriptor** — a Python-level ``redirect_stdout`` cannot contain it, so the remote command's output
  went onto the protocol stream. This is the same class as the ``up --json`` prose leak: the stream
  is reserved, and that is a rule rather than an incident.
* The command's output could not be read by the caller **at all**. A Bot calling ``rentguard_run``
  wants its output as much as its exit code, and the fix for the leak is the same one that supplies
  it: capture both streams and return them in the tool result.

The tests below pin both halves: the output arrives in the result, and nothing the command writes to
the process's stdout reaches fd 1. ``os.write(1, ...)`` stands in for the ssh child, which is exactly
what inherits fd 1.
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

from rentguard import cli, mcp_server  # noqa: E402
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


@contextlib.contextmanager
def fd_to(path: Path, fd: int):
    """Point an OS-level file descriptor at a file for the duration of the block.

    The only way to observe what a child process writes: ``redirect_stdout`` moves Python's
    ``sys.stdout``, and a child never sees that.
    """
    with open(path, "wb") as sink:
        saved = os.dup(fd)
        os.dup2(sink.fileno(), fd)
        try:
            yield
        finally:
            os.dup2(saved, fd)
            os.close(saved)


class RunOverMcp(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.artefacts = Artefacts(pull=[], remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.fake = FakeProvider()
        self.fake.add("pod-A", uptime_seconds=120)
        self.write_state({"instance_id": "pod-A"})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def call_run(self, *, runner=None, completed=None):
        """One `rentguard_run` tool call, with the provider and the transport under test patched."""
        if runner is not None:
            self.fake.command_runner = runner
        run_call = mock.Mock(return_value=completed) if completed is not None else mock.Mock(
            side_effect=AssertionError("subprocess.run should not be reached in this test"))
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                mock.patch.object(cli.subprocess, "run", run_call), \
                mock.patch.object(cli.subprocess, "call",
                                  mock.Mock(side_effect=AssertionError(
                                      "subprocess.call streams the child onto our stdout"))):
            text, structured, is_error = mcp_server.dispatch("rentguard_run",
                                                             {"command": "python train.py"})
        return text, structured, is_error, run_call

    # ---------------------------------------------------------------- the output reaches the caller

    def test_a_real_command_is_captured_not_streamed(self):
        """Sequence: `rentguard_run` over the MCP surface, with the ssh child replaced by a scripted
        process that prints to both streams.

        Effect: the tool result carries the output, and the child is run with its streams piped —
        inheriting ours is what put it on the protocol.
        """
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"epoch 1\nloss 0.5\n",
                                                stderr=b"cuda warning\n")

        text, structured, is_error, run_call = self.call_run(completed=completed)

        self.assertFalse(is_error, f"a successful command was reported as an error:\n{text}")
        self.assertIsNotNone(structured, f"the tool result carried no document:\n{text}")
        self.assertEqual(structured.get("stdout", "").strip(), "epoch 1\nloss 0.5")
        self.assertEqual(structured.get("stderr", "").strip(), "cuda warning")
        kwargs = run_call.call_args.kwargs
        self.assertEqual(kwargs.get("stdout"), subprocess.PIPE,
                         f"the child inherited our stdout: {run_call.call_args}")
        self.assertEqual(kwargs.get("stderr"), subprocess.PIPE,
                         f"the child inherited our stderr: {run_call.call_args}")

    def test_the_tool_result_carries_what_the_command_printed(self):
        """Sequence: the in-memory command seam (the fake) prints on both streams and fails.

        Effect: the output is in the result even for a failed command — a failure is exactly when it
        is wanted — and the exit code is the command's.
        """

        def runner(argv, remote):
            print("hello from the machine")
            print("a warning", file=sys.stderr)
            return 3

        text, structured, is_error, _ = self.call_run(runner=runner)

        self.assertTrue(is_error, f"a failing command was reported as success:\n{text}")
        self.assertEqual(structured.get("exit_code"), 3, structured)
        self.assertEqual(structured.get("stdout", "").strip(), "hello from the machine")
        self.assertEqual(structured.get("stderr", "").strip(), "a warning")
        self.assertIn("hello from the machine", text,
                      f"the person-facing text lost the command's output:\n{text}")

    def test_a_long_output_is_truncated_and_says_so(self):
        """Sequence: a command whose output is larger than the tool result should carry.

        Effect: the tail is returned, the size is reported, and truncation is a stated fact rather
        than a caller assuming it got everything.
        """
        limit = cli.RUN_OUTPUT_LIMIT
        body = "x" * (limit + 50) + "\nRESULT AT THE END\n"

        def runner(argv, remote):
            print(body, end="")
            return 0

        text, structured, is_error, _ = self.call_run(runner=runner)

        self.assertTrue(structured.get("truncated"), f"truncation was not declared: {structured}")
        self.assertTrue(structured["stdout"].rstrip("\n").endswith("RESULT AT THE END"),
                        "the tail, where a result lives, was dropped")
        self.assertEqual(structured.get("stdout_bytes"), len(body.encode()),
                         "the reported size does not describe what the command produced")
        self.assertLessEqual(len(structured["stdout"]), limit)
        self.assertIn("truncated", text, f"the person was not told: {text}")

    # ---------------------------------------------------------------- the stream stays clean

    def test_a_child_writing_to_fd_one_cannot_reach_the_protocol_stream(self):
        """Sequence: a command writes to the process's **file descriptor** 1, which is what an ssh
        child does and what ``redirect_stdout`` cannot intercept.

        Effect: those bytes land on fd 2 (the server's log stream) and never on fd 1, which is the
        JSON-RPC stream. This is defence in depth behind the capture: the server must stay clean even
        for a command that spawns something new.
        """
        leaked = self.cfg.state_dir / "fd1.txt"
        logged = self.cfg.state_dir / "fd2.txt"

        def leaky(args):
            os.write(1, b"LEAKED-ONTO-THE-PROTOCOL\n")
            return 0

        with mock.patch.object(cli, "cmd_ledger", leaky), fd_to(leaked, 1), fd_to(logged, 2):
            text, structured, is_error = mcp_server.dispatch("rentguard_ledger", {})

        self.assertNotIn("LEAKED", leaked.read_text(encoding="utf-8", errors="replace"),
                         "a child's output reached the JSON-RPC stream")
        self.assertIn("LEAKED", logged.read_text(encoding="utf-8", errors="replace"),
                      "the child's output was swallowed instead of moving to the log stream")
        self.assertFalse(is_error, text)

    def test_a_platform_that_refuses_the_descriptor_move_still_answers(self):
        """Sequence: ``os.dup`` raises, as a platform without the facility would.

        Effect: the tool call still runs and still returns its document. Defence in depth must not
        become a single point of failure, which is why the capture at the source is the guarantee.
        """
        with mock.patch.object(mcp_server.os, "dup", side_effect=OSError("no dup here")):
            text, structured, is_error = mcp_server.dispatch("rentguard_ledger", {})

        self.assertFalse(is_error, f"the server failed to answer:\n{text}")
        self.assertIsNotNone(structured, f"no document came back:\n{text}")

    # ---------------------------------------------------------------- the control

    def test_without_json_a_person_still_sees_the_command_live(self):
        """Control: the CLI's interactive path is unchanged — the child inherits the terminal, so a
        training run is watchable as it happens. Capturing is for the machine-readable path only."""
        args = cli.build_parser().parse_args(["run", "--", "python", "train.py"])
        stream_call = mock.Mock(return_value=0)
        run_call = mock.Mock(side_effect=AssertionError("the interactive path must not capture"))
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                mock.patch.object(cli.subprocess, "call", stream_call), \
                mock.patch.object(cli.subprocess, "run", run_call), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_run(args)

        self.assertEqual(code, 0)
        self.assertTrue(stream_call.called, "the child was not run")
        self.assertNotIn("stdout", stream_call.call_args.kwargs,
                         "the interactive path captured the child instead of inheriting the terminal")


if __name__ == "__main__":
    unittest.main()
