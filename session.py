"""The session driver: run a command *inside* an interactive ssh session, with a bound.

Why this exists, measured rather than assumed (2026-10-15, community 3080 Ti through
``ssh.runpod.io``):

- the machine's ssh endpoint **drops the exec**. ``ssh -tt target '<command>'`` returns prompt text
  and the command never runs: a framed pull came back as 556 bytes with no header, and
  ``run --json -- '<command>'`` hung past 120 s and had to be killed. Passing the command as an
  argument is not a transport on that route.
- what does work is the interactive session: the command goes into the session's **stdin**, and the
  exit status comes back in-band as a sentinel, because a PTY folds the streams together and an
  out-of-band status cannot be trusted.

So a session is: open the interactive session, write one line that prints a begin marker, runs the
command, remembers ``$?``, prints an exit sentinel carrying it, and exits. Everything the PTY adds —
the banner, the echoed line, CRLF — is the driver's problem, not the caller's:

- the echo line contains the markers, so it is dropped by matching *lines that are exactly* the
  marker, never by searching for the marker inside a line;
- CRLF is normalised to LF, and the incremental decoder keeps a multi-byte character split across
  two reads from becoming two replacement characters;
- a transcript without the sentinel is a **named** failure: the session returned, the command did
  not, and saying so is the difference between "nothing ran" and a plausible empty success;
- the read is bounded. A dropped exec never returns at all, so the bound is the only thing that
  turns that hang into a report.
"""

from __future__ import annotations

import codecs
import subprocess
import threading
from dataclasses import dataclass

#: The line the remote side prints before the command's own output starts. Distinctive on purpose: a
#: command that echoes bash internals may print anything, and the echo of *our own* line must not be
#: mistaken for it (see ``TranscriptFilter``).
BEGIN = "RENTGUARD-SESSION-BEGIN"
#: ``"<EXIT_SENTINEL> <code>"`` is the last line of a complete transcript.
EXIT_SENTINEL = "RENTGUARD-SESSION-EXIT"
#: How much of a transcript is kept when the command never produces its sentinel. A failure report
#: wants the beginning (the banner, the refusal) and the end (where it stopped); this is generous
#: enough for both and bounded so a runaway session cannot exhaust memory.
TRANSCRIPT_LIMIT = 8192


class SessionError(RuntimeError):
    """The session did not carry a usable transcript. Named, never a symptom."""


@dataclass
class SessionResult:
    """What came back from one session.

    ``exit_code`` is the command's own status from the sentinel, or ``-1`` when the session produced
    none. ``bounded`` is true when the wait expired — the session never returned — which is the shape
    a dropped exec has, and the reason it is reported rather than waited on.
    """

    exit_code: int
    output: str
    bounded: bool = False
    reason: str | None = None
    bytes_seen: int = 0
    detail: str = ""


def driver_command(command: str) -> str:
    """The one line written into the session's stdin.

    The command runs in a **subshell** (`( … )`), which is what makes the sentinel reachable: a
    command that ends in `exit 7` — or `exec`, or `set -e` failing — ends the *shell* it runs in, and
    without the subshell it takes the driver's own `printf` and `exit` with it, so the transcript
    carries no status at all. Measured on the 2026-10-15 window pod: a `cd … && echo x && exit 7`
    came back with the begin marker, the output, and no sentinel, and the driver reported -1 for a
    command whose status was known to the machine.

    ``rc=$?`` and ``exit $rc`` then matter: without them the session would close with the shell's
    status (always 0 after a ``printf``), and a failed command would read as a successful one.
    """
    return (f"printf '%s\\n' {BEGIN}; ( {command} ); rc=$?; "
            f"printf '\\n%s %d\\n' {EXIT_SENTINEL} $rc; exit $rc")


class TranscriptFilter:
    """Turns the bytes of a PTY session into ``(exit code, the command's output)``.

    Fed incrementally, so the same code serves the final parse and a caller that wants the output as
    it arrives. Everything before the begin marker is dropped — that is the banner, the shell prompt
    and the echoed command line — and everything from the exit sentinel on is dropped too.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self.started = False
        self._chunks: list[str] = []
        #: The newest output line, held back until the next line (or the sentinel) says whether its
        #: final newline is the command's or the driver's. Without this hold the live stream and the
        #: final output disagree by exactly that newline — measured in the window and again by Max's
        #: `test_a_command_without_a_final_newline_keeps_its_exact_bytes`.
        self._pending: str | None = None
        self.exit_code: int | None = None
        self.done = False
        self.malformed: str | None = None
        self.seen_bytes = 0

    @property
    def output(self) -> str:
        return "".join(self._chunks)

    def feed(self, data: bytes) -> str:
        """Consume ``data`` and return the part of it that is the command's output."""
        self.seen_bytes += len(data)
        self._buffer += self._decoder.decode(data)
        return self._drain()

    def finish(self) -> str:
        """Flush the decoder and the last partial line (a session killed mid-line)."""
        self._buffer += self._decoder.decode(b"", final=True)
        return self._drain(flush=True) + self._release()

    def _release(self) -> str:
        """Deliver the held-back line: its newline is the command's, because no sentinel claimed it."""
        if self._pending is None:
            return ""
        chunk, self._pending = self._pending, None
        self._chunks.append(chunk)
        return chunk

    def _drain(self, flush: bool = False) -> str:
        produced: list[str] = []
        while True:
            newline = self._buffer.find("\n")
            if newline < 0:
                break
            line = self._buffer[:newline].replace("\r", "")
            self._buffer = self._buffer[newline + 1:]
            produced.append(self._consume(line))
        if flush and self._buffer:
            produced.append(self._consume(self._buffer.replace("\r", "")))
            self._buffer = ""
        return "".join(produced)

    def _consume(self, line: str) -> str:
        if not self.started:
            # The echo of our own driver line contains the marker text inside it, so only a line that
            # *is* the marker starts the output. Nothing before it is the command's.
            if line.strip() == BEGIN:
                self.started = True
            return ""
        if self.done:
            return ""
        if line.strip().startswith(EXIT_SENTINEL + " "):
            # The driver prints exactly one newline before the sentinel, so that a command whose
            # output does not end with one cannot glue the sentinel onto its last line. That newline
            # is the driver's, and which line it terminates says which case this is:
            #
            # - the command ended with a newline, so the driver's made an **empty line** — drop it
            #   (measured on the window pod: `echo driver-ok` came back as `'driver-ok\n\n'`);
            # - the command did **not** end with a newline, so the driver's terminated its last
            #   line — drop that newline and only that one (`printf 'x'` must come back as `'x'`,
            #   not `'x\n'`; measured by Max's `test_a_command_without_a_final_newline_keeps_its_exact_bytes`).
            #
            # None of the command's own bytes are touched: `printf '\n'` and `echo; echo` still read
            # back as `'\n'` and `'\n\n'`, because in those cases the driver's newline is the *extra*
            # one, which is the empty line dropped above.
            if self._pending == "\n":
                self._pending = None
            elif self._pending is not None and self._pending.endswith("\n"):
                self._pending = self._pending[:-1]
            produced = self._release()
            code = line.strip()[len(EXIT_SENTINEL) + 1:].strip()
            try:
                self.exit_code = int(code)
            except ValueError:
                self.malformed = (f"the exit sentinel carried {code!r}, which is not an exit code")
            self.done = True
            return produced
        # One line is held back: which line the driver's newline terminates is only knowable once the
        # next line (or the sentinel) arrives, and the live stream must be the same bytes as the
        # final output.
        produced = self._release()
        self._pending = line + "\n"
        return produced


def parse_transcript(data: bytes) -> tuple[int, str]:
    """``(exit code, output)`` from a complete transcript, or a named ``SessionError``."""
    parsed = TranscriptFilter()
    parsed.feed(data)
    parsed.finish()
    if parsed.malformed:
        raise SessionError(parsed.malformed)
    if parsed.exit_code is None:
        raise SessionError(_why_no_sentinel(parsed))
    return parsed.exit_code, parsed.output


def _why_no_sentinel(parsed: TranscriptFilter) -> str:
    """Say which shape arrived: never started, or started and stopped early.

    The two have different fixes — one is a route that ignores the session, the other is a command
    that died the machine — and the transcript usually makes it obvious which one it is.
    """
    size = parsed.seen_bytes
    if not parsed.started:
        return (f"the session returned {size} bytes and never reached the begin marker "
                f"({BEGIN}): the command was not started (a banner, a refusal, or a dropped session)")
    return (f"the session reached the begin marker and returned {size} bytes without the exit sentinel "
            f"({EXIT_SENTINEL}): the command started and the session ended before it finished")


def session_excerpt(data: bytes) -> str:
    """A short, readable account of what a failed session returned."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > TRANSCRIPT_LIMIT:
        half = TRANSCRIPT_LIMIT // 2
        text = f"{text[:half]}\n... [{len(text) - TRANSCRIPT_LIMIT} characters dropped] ...\n{text[-half:]}"
    return text


def run_session(argv: list[str], target: str, command: str, *, timeout: float,
                on_text=None, popen=None) -> SessionResult:
    """Run ``command`` inside an interactive session, bounded by ``timeout`` seconds.

    The command goes into the session's **stdin**, never as an ssh argument: on the community proxy
    route the argument is dropped and the session hangs (measured 2026-10-15). ``on_text``, when
    given, receives the command's output as it arrives, already stripped of the banner and the echo,
    so a person watching still sees it live.

    The bound is enforced by reading on a worker thread and joining it with a timeout. A routed ssh
    that never returns cannot be made to return, and a `run` that waits forever on a billing machine
    is the defect this exists to close — so the session is killed and ``bounded`` is set instead.

    ``popen`` is a seam for the tests, resolved at call time so that replacing
    ``subprocess.Popen`` reaches it.
    """
    # Resolved at call time, not bound as a default: a test (or a caller) that replaces
    # `subprocess.Popen` must be able to reach this, and a default argument would freeze the original
    # in place at import and quietly run the real ssh.
    popen = popen or subprocess.Popen
    proc = popen([*argv, target], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.STDOUT)
    parsed = TranscriptFilter()
    raw: list[bytes] = []
    kept = 0

    def reader() -> None:
        nonlocal kept
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                if kept < TRANSCRIPT_LIMIT * 2:
                    raw.append(chunk)
                    kept += len(chunk)
                cooked = parsed.feed(chunk)
                if cooked and on_text is not None:
                    on_text(cooked)
                if parsed.done:
                    break
        except Exception:  # noqa: BLE001 - a read that dies is reported as a failed session below
            pass
        finally:
            tail = parsed.finish()
            if tail and on_text is not None:
                on_text(tail)

    try:
        proc.stdin.write(driver_command(command).encode("utf-8"))
        proc.stdin.flush()
        proc.stdin.close()
    except (OSError, ValueError) as exc:
        proc.kill()
        return SessionResult(exit_code=-1, output="", bounded=False,
                             reason=f"the session could not be written to: {exc}")
    worker = threading.Thread(target=reader, name="rentguard-session-reader", daemon=True)
    worker.start()
    if timeout and timeout > 0:
        worker.join(timeout)
        bounded = worker.is_alive()
    else:
        # An explicit 0 is "this run may take as long as it takes": a person watching a long job, who
        # can see it working and can interrupt it. The default is a real bound; this is the opt-out.
        worker.join()
        bounded = False
    if bounded:
        proc.kill()
        # The kill closes the pipe, so the reader returns almost at once; this second join only stops
        # the session from being waited on while the thread still holds the stream.
        worker.join(2.0)
    proc.wait()
    output = parsed.output
    seen = b"".join(raw)
    detail = session_excerpt(seen) if seen else ""
    if bounded:
        return SessionResult(exit_code=-1, output=output, bounded=True,
                             reason=f"the session did not return within {timeout:g}s "
                                    f"([budget] run_timeout_s) and was killed",
                             bytes_seen=parsed.seen_bytes, detail=detail)
    if parsed.malformed:
        return SessionResult(exit_code=-1, output=output, reason=parsed.malformed,
                             bytes_seen=parsed.seen_bytes, detail=detail)
    if parsed.exit_code is None:
        return SessionResult(exit_code=-1, output=output,
                             reason=_why_no_sentinel(parsed),
                             bytes_seen=parsed.seen_bytes, detail=detail)
    return SessionResult(exit_code=parsed.exit_code, output=output,
                         bytes_seen=parsed.seen_bytes, detail=detail)
