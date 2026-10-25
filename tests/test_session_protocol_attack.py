"""Independent attack on the session driver's shell semantics (the verifier, 2026-10-15).

The author's `test_the_command_runs_in_a_subshell_so_a_status_still_comes_back` asserts that the driver
*line* contains `( ... )`. That is a fact about the string. These run the driver line through a
real POSIX shell and assert the effect: the status comes back, the output is byte-faithful, and a
command that ends its own shell (`exit`, `exec`, `set -e`) still reports because the subshell is
what carries the sentinel.

Also pins the blank-line rule from both sides: the driver's blank line must not reach the caller,
and a command whose output *is* a blank line must still come back with one. The second direction is
where a "pop the last newline" fix over-corrects.

Offline: a local shell, no ssh, no machine.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import session  # noqa: E402
from rentguard.session import BEGIN, EXIT_SENTINEL  # noqa: E402


def find_shell() -> str | None:
    """A POSIX shell that actually runs here.

    Order matters on Windows: `shutil.which("bash")` finds the WSL launcher first, which refuses
    under this sandbox ("Zugriff verweigert"), so each candidate is probed with a command and only
    one that runs is used.
    """
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\sh.exe",
                      shutil.which("bash"), shutil.which("sh"), "/bin/bash", "/bin/sh"):
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"],
                                   capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and b"ok" in probe.stdout:
            return candidate
    return None


SHELL = find_shell()


class TheDriverLineCarriesTheStatusInARealShell(unittest.TestCase):
    """`exit 7` is the shape that broke it: the command's `exit` ended the shell and took the
    sentinel with it, so the driver reported -1 for a status the machine knew."""

    def setUp(self) -> None:
        if SHELL is None:
            self.skipTest(
                "no POSIX shell runs on this host: MSYS/Git bash dies with \"couldn't create "
                "signal pipe, Win32 error 5\" and the WSL launcher is refused under the sandbox. "
                "This file runs wherever /bin/bash exists, which is the CI runners.")

    def run_driver(self, command: str) -> tuple[int, str, str, int]:
        """(sentinel code, parsed output, raw stdout, the shell's own exit status)."""
        proc = subprocess.run([SHELL, "-c", session.driver_command(command)],
                              capture_output=True, timeout=30)
        raw = proc.stdout.decode("utf-8", "replace")
        code, output = session.parse_transcript(proc.stdout)
        return code, output, raw, proc.returncode

    def test_exit_does_not_take_the_sentinel_with_it(self) -> None:
        code, output, raw, shell_status = self.run_driver("exit 7")

        self.assertEqual(code, 7,
                         f"`exit 7` came back as {code} (raw={raw!r}); the subshell did not carry "
                         f"the status")
        self.assertEqual(output, "", f"`exit 7` produced output: {output!r}")
        self.assertEqual(shell_status, 7, "the driver's own exit status is not the command's")

    def test_output_before_the_exit_is_kept(self) -> None:
        code, output, _, _ = self.run_driver("echo hello; exit 3")

        self.assertEqual(code, 3, f"the status was lost: {code}")
        self.assertEqual(output, "hello\n", f"the output was lost or altered: {output!r}")

    def test_set_e_failing_does_not_take_the_sentinel_with_it(self) -> None:
        code, output, _, _ = self.run_driver("set -e; false; echo unreachable")

        self.assertEqual(code, 1, f"a failing `set -e` command came back as {code}")
        self.assertEqual(output, "", f"the unreachable line was reported as output: {output!r}")

    def test_exec_does_not_take_the_sentinel_with_it(self) -> None:
        code, output, _, _ = self.run_driver("exec echo replaced")

        self.assertEqual(code, 0, f"`exec` came back as {code}")
        self.assertEqual(output, "replaced\n", f"`exec` output was lost: {output!r}")

    def test_a_command_without_a_final_newline_keeps_its_exact_bytes(self) -> None:
        code, output, raw, _ = self.run_driver("printf 'x'")

        self.assertEqual(code, 0, f"the status was lost: {code}")
        self.assertEqual(output, "x",
                         f"a command with no final newline came back as {output!r}: the driver's "
                         f"blank line leaked or a newline was invented")
        self.assertIn(f"\n{EXIT_SENTINEL}", raw,
                      "the driver's blank line before the sentinel is gone, so a command without a "
                      "final newline would glue its output onto the sentinel")

    def test_an_empty_line_of_output_is_still_output(self) -> None:
        """The other direction of the same rule: dropping the driver's blank line must not eat the
        command's own blank line."""
        code, output, _, _ = self.run_driver("printf '\\n'")

        self.assertEqual(code, 0, f"the status was lost: {code}")
        self.assertEqual(output, "\n", f"the command's blank line was eaten: {output!r}")

    def test_two_blank_lines_of_output_are_two_blank_lines(self) -> None:
        code, output, _, _ = self.run_driver("echo; echo")

        self.assertEqual(code, 0, f"the status was lost: {code}")
        self.assertEqual(output, "\n\n", f"the command's blank lines were altered: {output!r}")

    def test_the_transcript_is_complete(self) -> None:
        code, _, raw, _ = self.run_driver("true")

        self.assertEqual(code, 0, raw)
        self.assertTrue(raw.startswith(BEGIN), f"the begin marker is not at the start: {raw!r}")
        self.assertIn(EXIT_SENTINEL, raw, f"no sentinel in the transcript: {raw!r}")


if __name__ == "__main__":
    unittest.main()
