"""The inventory command is **executed**, not inspected, in a real POSIX shell with two matches.

Measured 2026-10-20 on the one-cell window ($0.625225, verified against the ledger): the remote command
was built as `for pattern in {members}` with the patterns joined **unquoted**, so the shell expanded the
pattern list *before* the loop. `grok/runs/*` therefore became one iteration per matched directory, each
tagged with an increasing index, while `_parse_pull_inventory` accepts only `index < len(patterns)`. With
one match (the smoke) that passed; with two matches the second iteration named pattern 1 when 1 was
configured, the pull refused, nothing was transferred, and `down` stopped the pod: the cell trained fully
and its artefacts were lost with the machine.

The rule this file pins: the **outer** loop iterates the configured patterns **literally**, one iteration
per pattern, so the index is the pattern's position — while the **inner** loop still expands `$pattern`,
because that is where the glob belongs.

The tests that existed faked the inventory lines, which is exactly why this shipped: nothing ever ran the
command that was built. The executed cases here need a POSIX shell with GNU `stat` and `du`; they skip
where none runs (this sandbox refuses Git Bash and WSL) and run on the CI runners. The shell-free case
beside them pins the same defect on any host, so it cannot return unseen by either.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def find_shell() -> str | None:
    """A POSIX shell that actually runs here (the same probe the other execution tests use)."""
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


def loop_members(command: str) -> list[str]:
    """The words the **outer** loop iterates, exactly as they reach the shell.

    Splitting the raw segment rather than un-quoting it is the point: an unquoted `grok/runs/*` and a
    quoted `'grok/runs/*'` un-quote to the same word, and only the raw form says whether the shell is
    allowed to expand it before the loop runs.
    """
    segment = command.split("for pattern in ", 1)[1].split("; do", 1)[0]
    return segment.split()


class TheOuterLoopIteratesTheConfiguredPatterns(unittest.TestCase):
    """The shell-free half: the loop's members are the patterns **quoted**, one word each.

    An unquoted pattern in that list is expanded by the shell before the loop body runs, which is the
    defect: the body then iterates matched *paths*, and the index it prints is a directory's position
    rather than its pattern's. A string assertion cannot run the shell, but it can see whether the shell
    was given the chance — which is what the executed cases below then prove on a real one.
    """

    def test_every_loop_member_is_a_quoted_pattern_not_a_bare_glob(self) -> None:
        patterns = ["grok/runs/*", "results/*.json"]
        command = cli._pull_inventory_command("work", patterns)

        self.assertEqual(loop_members(command), [shlex.quote(pattern) for pattern in patterns],
                         f"the inventory loop does not name its configured patterns literally:\n"
                         f"{command}")

    def test_the_inner_loop_still_expands_the_pattern(self) -> None:
        """Quoting the outer list must not stop the glob: the inner loop expands `$pattern` unquoted."""
        command = cli._pull_inventory_command("work", ["grok/runs/*"])

        self.assertIn("for entry in $pattern; do", command,
                      f"the inner loop no longer expands the pattern, so nothing would be matched:\n"
                      f"{command}")


class TheInventoryCommandRuns(unittest.TestCase):
    def setUp(self) -> None:
        if SHELL is None:
            self.skipTest("no POSIX shell runs on this host; this file runs on the CI runners and in "
                          "any POSIX container")
        self.root = scratch_dir(self)
        self.work = self.root / "work"
        runs = self.work / "grok" / "runs"
        for name in ("run-15", "run-16"):
            (runs / name).mkdir(parents=True)
            (runs / name / "out.json").write_text("{}", encoding="utf-8")
        # A member whose name has a space: the path is the inventory line's fifth, tab-separated field
        # and must come back whole.
        (runs / "run-16" / "out file.json").write_text("{}", encoding="utf-8")
        results = self.work / "results"
        results.mkdir()
        for name in ("a.json", "b.json"):
            (results / name).write_text("{}", encoding="utf-8")

    def inventory(self, patterns: list[str]) -> tuple[str, list[cli.PullEntry]]:
        """Run the real command in a real shell against the scratch tree, then parse its answer."""
        command = cli._pull_inventory_command("work", patterns)
        proc = subprocess.run([SHELL, "-c", command], cwd=str(self.root), capture_output=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"the inventory command failed ({proc.returncode}):\n{command}\n"
                         f"stderr: {proc.stderr.decode('utf-8', 'replace')}")
        text = proc.stdout.decode("utf-8", "replace")
        return text, cli._parse_pull_inventory(text, patterns)

    def test_two_matches_of_one_pattern_both_read_as_that_pattern(self) -> None:
        """The measured failure: two matches, one pattern — the second must still be pattern 0."""
        text, entries = self.inventory(["grok/runs/*"])

        self.assertIn(cli.PULL_INVENTORY_HEADER, text)
        self.assertEqual(sorted(entry.path for entry in entries),
                         ["grok/runs/run-15", "grok/runs/run-16"], text)
        for entry in entries:
            self.assertEqual(entry.patterns, ("grok/runs/*",),
                             f"{entry.path} was not attributed to the pattern that matched it:\n{text}")
            self.assertEqual(entry.kind, "d", text)
            self.assertGreater(entry.bytes, 0, text)

    def test_each_configured_pattern_keeps_its_own_position(self) -> None:
        """Two patterns, four matches: the index is the pattern's position, never the match's."""
        text, entries = self.inventory(["grok/runs/*", "results/*.json"])

        by_pattern: dict[str, list[str]] = {}
        for entry in entries:
            self.assertEqual(len(entry.patterns), 1, f"{entry.path} was attributed twice:\n{text}")
            by_pattern.setdefault(entry.patterns[0], []).append(entry.path)
        self.assertEqual(sorted(by_pattern.get("grok/runs/*", [])),
                         ["grok/runs/run-15", "grok/runs/run-16"], text)
        self.assertEqual(sorted(by_pattern.get("results/*.json", [])),
                         ["results/a.json", "results/b.json"], text)

    def test_a_pattern_that_matches_nothing_contributes_no_entry(self) -> None:
        text, entries = self.inventory(["grok/empty/*"])

        self.assertEqual(entries, [], text)
        self.assertIn(cli.PULL_INVENTORY_END, text)

    def test_a_name_with_a_space_comes_back_whole(self) -> None:
        """The path field is tab-separated, so a space in a member must not move the columns."""
        text, entries = self.inventory(["grok/runs/run-16/*"])

        self.assertEqual(sorted(entry.path for entry in entries),
                         ["grok/runs/run-16/out file.json", "grok/runs/run-16/out.json"], text)
        for entry in entries:
            self.assertEqual(entry.kind, "f", text)


if __name__ == "__main__":
    unittest.main()
