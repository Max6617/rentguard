"""A live run's log has to arrive while it is live (defect 24).

Measured 2026-10-16: both window roots logged their guard with
`python -m rentguard.cli watch > guard.log`, and **the file stayed empty for the whole window** —
Python block-buffers a redirected stdout, so the lines were still in the buffer when the window
ended and appeared only when the process exited, exactly when nobody needs them. An empty guard log
during a live window is indistinguishable from a dead guard.

The authoritative liveness check is the **heartbeat file**, which `status` and `doctor` read; the log
is history, and history has to arrive as it happens. Two tests: one unit-level (the writer flushes),
one through a real child process and a real pipe, which is the effect itself — a line has to arrive
*while the process that wrote it is still running*.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402


class RecordingStream:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        self.flushes += 1


class TheWriterFlushes(unittest.TestCase):
    def test_note_flushes_the_stream_it_wrote_to(self) -> None:
        stream = RecordingStream()
        with mock.patch.object(sys, "stdout", stream):
            cli.note("a line a person is watching for")

        self.assertEqual(stream.flushes, 1,
                         "the line was written without being flushed: with stdout redirected to a "
                         "file it would sit in the buffer until the process exits")
        self.assertIn("a line a person is watching for", "".join(stream.written))


class ALineArrivesWhileTheProcessRuns(unittest.TestCase):
    """The effect, through a real child: read a line from its stdout while it is still alive."""

    def test_a_note_reaches_a_pipe_before_the_process_ends(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, r'%s'); "
             "from rentguard.cli import note; note('live-line'); time.sleep(15)" % WS],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line: list[bytes] = []

        def read() -> None:
            line.append(child.stdout.readline())

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(10.0)
        arrived = bool(line and line[0].strip())
        child.kill()
        child.wait()

        self.assertTrue(arrived,
                        "a line written a moment after start did not reach the pipe before the "
                        "process was killed: a redirected log would stay empty for the whole run")


if __name__ == "__main__":
    unittest.main()
