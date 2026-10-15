"""The session driver: a command run inside an interactive session, with its failures named.

Defect 9's second half. The first half (the framed pull) is `tests/test_pull_frame.py`; this is the
other route the 2026-10-15 window proved broken: `ssh -tt target '<command>'` **drops the exec** on the
community proxy route. The pull returned 556 bytes of prompt text with no header, and
`run --json -- '<command>'` hung past 120 s and had to be killed. So the command goes into the
session's stdin behind a begin marker, the exit status comes back as an in-band sentinel, and the read
is bounded — a dropped session never returns at all, which is the shape a sentinel alone cannot catch.

Everything here is offline: hand-written transcripts for the parser (the three shapes already have
documents: banner plus echo, a session that never starts the command, a session that stops early), and
a fake child process for the bound. The live proof is the driver's own window.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, session  # noqa: E402
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.session import BEGIN, EXIT_SENTINEL, SessionError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

COMMAND = "cd /workspace && python train.py"

#: The one line the driver writes, as a PTY echoes it back: the markers appear inside it, which is
#: why the parser must match whole lines and never search for the marker text.
ECHO = (f"printf '%s\\n' {BEGIN}; {COMMAND}; rc=$?; "
        f"printf '\\n%s %d\\n' {EXIT_SENTINEL} $rc; exit $rc")

BANNER = ("Welcome to the RunPod community cloud.\r\n"
          "This machine is billed while it runs.\r\n")


def transcript(*, output: str = "epoch 1 loss 0.5\n", code: int = 0, banner: str = BANNER,
               echo: bool = True, marker: bool = True, sentinel: bool = True,
               crlf: bool = True) -> bytes:
    """A transcript shaped the way the real route returns one: banner, echo, output, sentinel.

    The driver always prints one newline immediately before the sentinel (`printf '\\n%s %d\\n'`), so
    a faithful transcript has it too: it is what terminates a command's last line when that line has
    no newline of its own, and what makes an empty line when it has one.
    """
    body = banner
    if echo:
        body += ECHO + "\r\n"
    if marker:
        body += BEGIN + "\r\n" + output
    if sentinel:
        body += "\r\n"                       # the driver's own newline, always present
        body += f"{EXIT_SENTINEL} {code}\r\n"
    return body.encode("utf-8") if crlf else body.replace("\r\n", "\n").encode("utf-8")


class TheDriverLineCarriesTheStatus(unittest.TestCase):
    def test_the_command_goes_into_the_session_behind_a_marker_and_a_sentinel(self) -> None:
        line = session.driver_command(COMMAND)

        self.assertIn(BEGIN, line, f"no begin marker: {line!r}")
        self.assertIn(COMMAND, line, f"the command is not in the driver line: {line!r}")
        self.assertIn("rc=$?", line, f"the remote status is not captured: {line!r}")
        self.assertIn(EXIT_SENTINEL, line, f"no exit sentinel: {line!r}")
        self.assertIn("exit $rc", line, f"the session does not end with the command's status: {line!r}")

    def test_the_command_runs_in_a_subshell_so_a_status_still_comes_back(self) -> None:
        """A command that ends in `exit 7` would otherwise end the shell before the sentinel, and the
        driver would report -1 for a status the machine knew (measured on the window pod)."""
        line = session.driver_command("cd /workspace && echo x && exit 7")

        self.assertIn("( cd /workspace && echo x && exit 7 )", line,
                      f"the command is not isolated in a subshell: {line!r}")

    def test_the_drivers_own_blank_line_is_not_the_commands_output(self) -> None:
        """The sentinel is printed after a blank line so it cannot glue onto a command whose output
        lacks a final newline; that blank line must not reach the caller as output. This is the case
        where the command **did** end with a newline (`'driver-ok\\n'` → `'driver-ok\\n'`); the case
        where it did not is `test_a_command_without_a_final_newline_keeps_its_exact_bytes` below, and
        the two are different branches of the same trim."""
        code, output = session.parse_transcript(
            transcript(output="driver-ok\n"))

        self.assertEqual(code, 0, "the sentinel was not parsed")
        self.assertEqual(output, "driver-ok\n",
                         f"the driver's own blank line reads as the command's output: {output!r}")

    def test_a_command_without_a_final_newline_keeps_its_exact_bytes(self) -> None:
        """The other branch, pinned here as well as in Max's shell-driven
        `tests/test_session_protocol_attack.py::…test_a_command_without_a_final_newline_keeps_its_exact_bytes`
        (which skips where no POSIX shell runs): there the driver's newline terminated the command's
        last line, so it is dropped rather than reduced to a blank line. `printf 'x'` must read back
        as `'x'`, not `'x\\n'`."""
        code, output = session.parse_transcript(
            transcript(output="x"))

        self.assertEqual(code, 0, "the sentinel was not parsed")
        self.assertEqual(output, "x", f"a newline was invented for a command that printed none: {output!r}")

    def test_the_raw_transcript_keeps_the_drivers_newline(self) -> None:
        """The trim is in the *parser*, never in the driver line: the newline before the sentinel is
        what stops a command without a final newline gluing its output onto it, so a "fix" that
        removed it from `driver_command` would trade one defect for the other. Max's shell-driven
        file asserts this on the raw transcript; this asserts it where no POSIX shell runs."""
        raw = transcript(output="x")

        self.assertIn(b"\n" + EXIT_SENTINEL.encode(), raw,
                      f"the driver's newline before the sentinel is gone: {raw!r}")
        self.assertIn("\r\n" + EXIT_SENTINEL, raw.decode("utf-8", "replace"),
                      "the driver's newline is not its own line in the transcript")

    def test_the_commands_own_newlines_survive_the_trim(self) -> None:
        """Both directions of the same rule, at the parser: only the driver's own newline goes."""
        for printed, expected in (("", ""), ("\n", "\n"), ("\n\n", "\n\n"), ("a\nb", "a\nb"),
                                 ("a\nb\n", "a\nb\n")):
            with self.subTest(printed=printed):
                code, output = session.parse_transcript(
                    transcript(output=printed))

                self.assertEqual(code, 0, f"the sentinel was not parsed for {printed!r}")
                self.assertEqual(output, expected,
                                 f"the command printed {printed!r} and the driver returned "
                                 f"{output!r}")


class ATranscriptIsParsedByItsMarkers(unittest.TestCase):
    def test_the_banner_the_echo_and_the_crlf_are_stripped(self) -> None:
        code, output = session.parse_transcript(transcript(output="epoch 1 loss 0.5\nepoch 2 loss 0.4\n"))

        self.assertEqual(code, 0, "a successful command did not report its status")
        self.assertEqual(output, "epoch 1 loss 0.5\nepoch 2 loss 0.4\n",
                         f"the transcript was not cleaned: {output!r}")

    def test_the_echoed_driver_line_is_not_the_begin_marker(self) -> None:
        """The echo contains the marker *text*; a parser that searched for it would start the output
        at its own command line and hand the caller the driver's plumbing as the result."""
        code, output = session.parse_transcript(transcript(output="only this line\n"))

        self.assertEqual(output, "only this line\n",
                         f"the echoed command line leaked into the output: {output!r}")

    def test_a_failing_command_reports_its_own_status(self) -> None:
        code, output = session.parse_transcript(
            transcript(output="Traceback\n", code=2, sentinel=True))

        self.assertEqual(code, 2, "the sentinel's status was not used")
        self.assertEqual(output, "Traceback\n")

    def test_a_sentinel_printed_by_the_command_does_not_end_the_transcript_early(self) -> None:
        """A command that prints a lookalike line is data; the last sentinel is the real one."""
        code, output = session.parse_transcript(
            transcript(output=f"look: {EXIT_SENTINEL} 99 is not the real one\n", code=7))

        self.assertEqual(code, 7, f"the command's own text was read as the exit status: {code}")
        self.assertIn("is not the real one", output,
                      f"the output was truncated at the lookalike line: {output!r}")

    def test_the_output_arrives_as_the_bytes_do(self) -> None:
        """The filter is incremental: the same code serves the parse and a live reader, including a
        multi-byte character split across two reads and the driver's own newline before the sentinel
        arriving in its own chunk."""
        parsed = session.TranscriptFilter()
        text = ""
        for chunk in [b"banner\r\n", BEGIN.encode() + b"\r\n", "café ".encode()[:4],
                      "café ".encode()[4:] + b"\n", b"\r\n",
                      f"{EXIT_SENTINEL} 0\r\n".encode()]:
            text += parsed.feed(chunk)
        text += parsed.finish()

        self.assertEqual(text, "café \n", f"a split character or the CRLF was mangled: {text!r}")
        self.assertEqual(parsed.exit_code, 0)

    def test_a_prompt_after_the_sentinel_is_not_output(self) -> None:
        """A real session ends with the sentinel and then the shell's prompt. Everything after the
        sentinel is the session's, not the command's."""
        code, output = session.parse_transcript(
            transcript(output="done\n") + b"root@pod:/workspace# \r\n")

        self.assertEqual(code, 0)
        self.assertEqual(output, "done\n", f"the shell prompt leaked into the output: {output!r}")

    def test_a_long_transcript_is_truncated_with_the_count(self) -> None:
        """A failed session's excerpt is bounded, and says how much it dropped: an unbounded buffer is
        how a runaway session exhausts the caller's memory while reporting a failure."""
        text = session.session_excerpt(b"x" * (session.TRANSCRIPT_LIMIT * 3))

        self.assertIn("characters dropped", text, f"the truncation is not named: {text[-200:]}")
        self.assertLess(len(text), session.TRANSCRIPT_LIMIT * 2,
                        "the excerpt was not bounded")


class AMissingSentinelIsNamed(unittest.TestCase):
    def test_a_session_that_never_starts_the_command_is_named(self) -> None:
        """The measured shape: a route that ignores the session returns a banner and a prompt, and
        nothing else. There is no marker to start from, and saying so is the whole report."""
        with self.assertRaises(SessionError) as caught:
            session.parse_transcript(transcript(echo=False, marker=False, sentinel=False))

        message = str(caught.exception)
        self.assertIn("begin marker", message, f"the shape is not named: {message}")
        self.assertIn("not started", message, f"the likely cause is not named: {message}")

    def test_a_session_that_stops_early_is_named_differently(self) -> None:
        with self.assertRaises(SessionError) as caught:
            session.parse_transcript(transcript(output="half a line", sentinel=False))

        message = str(caught.exception)
        self.assertIn("begin marker", message, "the report does not say the command did start")
        self.assertIn("sentinel", message, f"the sentinel is not named: {message}")
        self.assertNotIn("not started", message,
                         f"a started-then-stopped session was reported as a dropped one: {message}")

    def test_an_empty_transcript_is_named(self) -> None:
        with self.assertRaises(SessionError):
            session.parse_transcript(b"")

    def test_a_sentinel_that_is_not_a_number_is_named(self) -> None:
        with self.assertRaises(SessionError) as caught:
            session.parse_transcript(transcript(sentinel=False)
                                     + f"{EXIT_SENTINEL} not-a-number\r\n".encode())

        self.assertIn("not an exit code", str(caught.exception))


class _FakeChild:
    """A child process whose stdout is scripted, and which records whether it was killed.

    ``stall`` models a session that has said everything it is going to say and never closes — the
    dropped-exec shape. ``kill`` then unblocks the read, which is what a real kill does when it closes
    the pipe (modelled, not observed: the kill's effect on the reader is a property of pipes).
    """

    def __init__(self, chunks: list[bytes], *, stall: bool = False) -> None:
        self.stdout = self
        self.stdin = self
        self._chunks = list(chunks)
        self._stall = stall
        self.killed = False
        self.waited = False
        self.written = b""
        self.closed = False

    # stdin side
    def write(self, data: bytes) -> None:
        self.written += data

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    # stdout side
    def read(self, size: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._stall:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not self.killed:
                time.sleep(0.02)
        return b""

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        self.waited = True
        return 0


class TheSessionIsBounded(unittest.TestCase):
    def test_a_session_that_never_returns_is_killed_and_named(self) -> None:
        child = _FakeChild([], stall=True)
        started = time.monotonic()

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=0.3,
                                     popen=lambda *a, **k: child)

        self.assertTrue(result.bounded, f"the bound did not fire: {result}")
        self.assertEqual(result.exit_code, -1, f"a bound is not an exit status: {result.exit_code}")
        self.assertIn("run_timeout_s", result.reason or "",
                      f"the report does not name the bound: {result.reason}")
        self.assertTrue(child.killed, "the session was not killed when the bound expired")
        self.assertLess(time.monotonic() - started, 10.0,
                        "the caller waited for a session that never returns")

    def test_a_complete_session_returns_its_status_and_output(self) -> None:
        child = _FakeChild([transcript(output="done\n", code=3)])
        shown: list[str] = []

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=5.0, on_text=shown.append,
                                     popen=lambda *a, **k: child)

        self.assertFalse(result.bounded, f"a finished session was called bounded: {result}")
        self.assertEqual(result.exit_code, 3, f"the sentinel's status was lost: {result}")
        self.assertEqual(result.output, "done\n", f"the output was lost: {result.output!r}")
        self.assertEqual("".join(shown), "done\n", f"the live stream is not the output: {shown}")
        self.assertIn(BEGIN.encode(), child.written,
                      "the command was not written into the session's stdin")
        self.assertTrue(child.closed, "the session's stdin was left open")

    def test_a_session_that_returns_without_the_sentinel_is_a_named_failure(self) -> None:
        child = _FakeChild([transcript(echo=False, sentinel=False)])

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=5.0,
                                     popen=lambda *a, **k: child)

        self.assertEqual(result.exit_code, -1, f"a transcript without a status read as one: {result}")
        self.assertFalse(result.bounded, "a returned session was called bounded")
        self.assertIn("begin marker", result.reason or "", f"the failure is not named: {result.reason}")
        self.assertGreater(result.bytes_seen, 0, "the bytes the session returned were not counted")

    def test_a_written_session_that_dies_is_named(self) -> None:
        class Broken(_FakeChild):
            def write(self, data: bytes) -> None:
                raise OSError("broken pipe")

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=5.0,
                                     popen=lambda *a, **k: Broken([]))

        self.assertEqual(result.exit_code, -1, result)
        self.assertIn("could not be written", result.reason or "", result.reason)

    def test_a_read_that_dies_is_a_named_failure_not_a_traceback(self) -> None:
        class Broken(_FakeChild):
            def read(self, size: int = -1) -> bytes:
                raise OSError("connection reset by peer")

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=5.0,
                                     popen=lambda *a, **k: Broken([]))

        self.assertEqual(result.exit_code, -1,
                         f"a dead read produced an exit status: {result}")
        self.assertIn("begin marker", result.reason or "",
                      f"the failure does not say the command never started: {result.reason}")

    def test_an_unbounded_session_completes_and_is_not_killed(self) -> None:
        """`run_timeout_s = 0` is the explicit "this may take as long as it takes": no bound, no
        kill, and the status still comes from the sentinel."""
        child = _FakeChild([transcript(output="long job\n", code=0)])

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=0,
                                     popen=lambda *a, **k: child)

        self.assertFalse(result.bounded, f"an unbounded session was called bounded: {result}")
        self.assertEqual(result.exit_code, 0, result)
        self.assertFalse(child.killed, "an unbounded session was killed")

    def test_a_sentinel_that_is_not_a_number_fails_the_session(self) -> None:
        child = _FakeChild([transcript(sentinel=False) + f"{EXIT_SENTINEL} nope\r\n".encode()])

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=5.0,
                                     popen=lambda *a, **k: child)

        self.assertEqual(result.exit_code, -1, f"a malformed sentinel read as a status: {result}")
        self.assertIn("not an exit code", result.reason or "", result.reason)

    def test_the_last_partial_line_of_a_killed_session_is_shown(self) -> None:
        """The session is killed mid-output: what arrived before the kill is the failure's evidence,
        including a final line that never got its newline."""
        child = _FakeChild([BEGIN.encode() + b"\r\nhalf a line"], stall=True)
        shown: list[str] = []

        result = session.run_session(["ssh"], "pod", COMMAND, timeout=0.3, on_text=shown.append,
                                     popen=lambda *a, **k: child)

        self.assertTrue(result.bounded, f"the bound did not fire: {result}")
        self.assertEqual("".join(shown), "half a line\n",
                         f"the partial line was lost when the session was killed: {shown}")


class RunOnAPtyRouteIsBounded(unittest.TestCase):
    """The wiring, not just the unit: `run` on a PTY route goes through the driver, and a session
    that never returns is a reported failure rather than a hang the machine bills through."""

    class PtyFake(FakeProvider):
        def ssh_argv(self, instance, key=None):        # noqa: ANN001, ANN201
            return ["-tt"], "pod-A@ssh.runpod.io"

        def ssh_requires_pty(self, instance) -> bool:  # noqa: ANN001
            return True

    def test_run_reports_the_bound_instead_of_waiting(self) -> None:
        cfg = Config(budget=Budget(cap_usd=5.0, per_run_cap_usd=5.0, run_timeout_s=0.3),
                     state_dir=scratch_dir(self))
        cfg.provider = "fake"
        cfg.artefacts = Artefacts(pull=[], remote_dir="/workspace",
                                  local_dir=str(cfg.state_dir / "artefacts"))
        provider = self.PtyFake(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=60, status="RUNNING")
        cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        Ledger(cfg.ledger_path).open(purpose="batch", provider="fake", instance_id="pod-A",
                                     gpu_id="g", rate_usd_per_hour=2.0)
        child = _FakeChild([], stall=True)
        args = cli.build_parser().parse_args(["run", "--json", "--", "python", "train.py"])
        out, err = io.StringIO(), io.StringIO()

        started = time.monotonic()
        with mock.patch.object(cli, "_config", lambda: cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                mock.patch("subprocess.Popen", lambda *a, **k: child), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_run(args)

        text, said = out.getvalue(), err.getvalue()
        document = json.loads(text[text.index("{"):]) if "{" in text else {}
        self.assertLess(time.monotonic() - started, 20.0,
                        f"run waited on a session that never returns:\n{said}")
        self.assertEqual(code, 1, f"a bounded session was reported as a clean run:\n{said}")
        self.assertNotEqual(document.get("exit_code"), 0,
                            f"a run with no exit status read as a success: {document}")
        # Under `--json` the prose goes to stderr and the document to stdout; the bound is named in
        # the prose, and the document carries the -1 that says no status came back.
        self.assertIn("run_timeout_s", said, f"the bound is not named in the output:\n{said}")
        self.assertEqual(document.get("exit_code"), -1, document)
        self.assertTrue(child.killed, "the session was not killed when the bound expired")


if __name__ == "__main__":
    unittest.main()
