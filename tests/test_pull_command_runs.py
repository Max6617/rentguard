"""The archive command is **executed**, not inspected, against a real `tar` in a real POSIX shell.

Measured 2026-10-17 (Assay's report, Gilbert's ruling): `_pull_remote_command` joined the selected paths,
and `_pty_pull_command` joined that string **again**, so on the proxy route tar received a list of
characters — `rc=1`, "Couldn't visit directory", while the direct route gave `rc=0`. Every proxied pull
would have delivered nothing, and `down` stops the machine by default after such a pull: one failed pull
would cost the run's artefacts *and* its machine.

The tests that existed only inspected the built string, which is exactly how a double join survived. These
run the constructed command through a shell with a real `tar` and check the archive's **members**. They
skip where no POSIX shell runs (the file runs on the CI runners, and in any container); the proxy route's
hardware proof has been outstanding for weeks because no proxy-capable card could be rented — this is that
route proven in software first.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def find_shell() -> str | None:
    """A POSIX shell that actually runs here (the same probe the session tests use).

    Order matters on Windows: `shutil.which("bash")` finds the WSL launcher first, which refuses under
    this sandbox, so each candidate is probed and only one that runs is used.
    """
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\sh.exe",
                      shutil.which("bash"), shutil.which("sh"), "/bin/bash", "/bin/sh"):
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and b"ok" in probe.stdout:
            return candidate
    return None


SHELL = find_shell()


class TheCommandNamesPathsNotCharacters(unittest.TestCase):
    """The property behind the executed tests, checkable on every host: a selected path reaches the
    remote command **once**, as one word.

    The bug this pins: `_pull_remote_command` joined the paths and `_pty_pull_command` joined that string
    again, so tar received a list of characters (the path arrived space-separated, `g r o k / r u n s`).
    A string assertion like this one would have caught it where the executed test could not run.
    """

    def test_the_pty_command_carries_the_path_once_and_quoted_once(self) -> None:
        command = cli._pull_remote_command("work", ["grok/runs/run-16"], pty=True)

        self.assertEqual(command.count("grok/runs/run-16"), 1,
                         f"the selected path is not one word in the PTY command:\n{command}")
        self.assertIn("tar czf /tmp/.rentguard-pull-$$.tar.gz -- grok/runs/run-16;", command,
                      command)

    def test_the_direct_command_carries_every_path_once_after_the_separator(self) -> None:
        command = cli._pull_remote_command("work", ["grok/runs/run-16", "results/final.json"],
                                           pty=False)

        self.assertEqual(command, "cd work && tar czf - -- grok/runs/run-16 results/final.json")


class TheArchiveCommandRuns(unittest.TestCase):
    def setUp(self) -> None:
        if SHELL is None:
            self.skipTest("no POSIX shell runs on this host; this file runs on the CI runners and in "
                          "any POSIX container")
        self.root = scratch_dir(self)
        self.work = self.root / "work"
        (self.work / "grok" / "runs" / "run-16").mkdir(parents=True)
        (self.work / "grok" / "runs" / "run-15").mkdir(parents=True)
        (self.work / "grok" / "runs" / "run-16" / "out.json").write_text("{}", encoding="utf-8")
        # A name with a space and one that starts with a dash: both are exact paths from the inventory
        # and both must reach tar as names, not as a split word and not as an option.
        (self.work / "grok" / "runs" / "run-16" / "out file.json").write_text("{}", encoding="utf-8")
        (self.work / "grok" / "runs" / "run-16" / "-cache").write_text("c", encoding="utf-8")
        (self.work / "grok" / "runs" / "run-15" / "old.json").write_text("{}", encoding="utf-8")

    def run_command(self, command: str) -> bytes:
        proc = subprocess.run([SHELL, "-c", command], cwd=str(self.root), capture_output=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"the archive command failed ({proc.returncode}):\n{command}\n"
                         f"stderr: {proc.stderr.decode('utf-8', 'replace')}")
        return proc.stdout

    def selected(self) -> list[str]:
        return ["grok/runs/run-16"]

    def members(self, archive: bytes) -> list[str]:
        """The archive's **files**, sorted: directory entries are a tar flavour's own detail, and what
        this file is about is which paths were carried."""
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            return sorted(member.name for member in tar.getmembers() if member.isfile())

    def test_the_direct_archive_command_carries_exactly_the_selected_paths(self) -> None:
        command = cli._pull_remote_command("work", self.selected(), pty=False)

        names = self.members(self.run_command(command))

        self.assertEqual(names, ["grok/runs/run-16/-cache",
                                 "grok/runs/run-16/out file.json",
                                 "grok/runs/run-16/out.json"],
                         f"the direct archive did not carry exactly the selected path:\n{command}")

    def test_the_pty_archive_command_lands_the_same_members(self) -> None:
        """The proxy route: the archive inside a framed, base64 transcript, exactly as a PTY carries it.

        This is the shape that was broken — the builder joined its input again, so tar saw characters
        — and the test executes it with a real tar rather than reading the string.
        """
        command = cli._pull_remote_command("work", self.selected(), pty=True)

        transcript = self.run_command(command)
        declared, body = cli._parse_archive_frame(transcript)
        archive = base64.b64decode(body, validate=True)

        self.assertIn(b"\x1f\x8b", archive[:2], "the framed body is not a gzip stream")
        self.assertEqual(declared, len(body), "the frame's header disagrees with its body")
        self.assertEqual(self.members(archive),
                         ["grok/runs/run-16/-cache",
                          "grok/runs/run-16/out file.json",
                          "grok/runs/run-16/out.json"],
                         f"the PTY archive did not carry exactly the selected path:\n{command}")

    def test_a_leading_dash_path_is_archived_not_read_as_an_option(self) -> None:
        """`tar czf - -cache` reads `-cache` as an option; `--` is the separator that makes it a name."""
        command = cli._pull_remote_command("work", ["grok/runs/run-16/-cache"], pty=False)

        names = self.members(self.run_command(command))

        self.assertEqual(names, ["grok/runs/run-16/-cache"], command)


if __name__ == "__main__":
    unittest.main()
