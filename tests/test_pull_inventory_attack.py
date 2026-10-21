"""Attack the inventory protocol: the shell-free shape it must emit, and the real command executed.

Revision under attack: ``78fd49c`` (code ``982bcb9``), after ``3c82450`` quoted the loop's patterns.
The defect that shipped: ``for pattern in {members}`` joined the patterns **unquoted**, so the shell
expanded ``grok/runs/*`` in the *outer* list; the index became a match's position, and with two
matches ``_parse_pull_inventory`` refused "pattern 1 of 1" and the pull transferred nothing (measured
2026-10-20: a cell's artefacts lost, $0.625225).

This file attacks the fix from both sides:

* **shell-free** — the emitted command must quote each configured pattern in the outer list and leave
  the inner expansion unquoted; the parser+selection must accept the answer the fixed command
  produces (two entries sharing the pattern's index) and attribute them to the right pattern;
* **executed** — the real command in a real POSIX shell against a scratch tree (skipped where no
  shell can run; it ran on the CI runners). It covers one pattern/two directories, two configured
  patterns, a character class, files vs directories, and a symlinked directory.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

SHELL_CANDIDATES = (
    "bash", "sh",
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\msys64\usr\bin\bash.exe",
    r"C:\msys64\usr\bin\sh.exe",
    r"C:\cygwin64\bin\bash.exe",
    r"C:\cygwin64\bin\sh.exe",
    "/bin/bash", "/bin/sh",
)


def find_posix_shell() -> str | None:
    for candidate in SHELL_CANDIDATES:
        path = (candidate if candidate.startswith(("/", "C:", "c:")) else shutil.which(candidate))
        if not path:
            continue
        try:
            probe = subprocess.run([path, "-c", "printf shell-ok"], capture_output=True, text=True,
                                   timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "shell-ok" in probe.stdout:
            return path
    return None


SHELL = find_posix_shell()


def framed(*lines: str) -> str:
    return "\n".join([cli.PULL_INVENTORY_HEADER, *lines, cli.PULL_INVENTORY_END, ""])


def entry_line(index: int, kind: str, mtime: float, size: int, path: str) -> str:
    return f"{index}\t{kind}\t{mtime}\t{size}\t{path}"


def outer_list(command: str) -> list[str]:
    """The words the shell iterates in ``for pattern in <here>; do``."""
    start = command.index("for pattern in ") + len("for pattern in ")
    end = command.index("; do", start)
    return shlex.split(command[start:end])


class TheCommandQuotesThePatterns(unittest.TestCase):
    def test_each_configured_pattern_is_one_quoted_outer_word(self) -> None:
        """The defect was the outer list expanding: each pattern must be *quoted* there, so the shell
        iterates the configured patterns, and the index stays the pattern's position."""
        command = cli._pull_inventory_command("/workspace", ["grok/runs/*", "logs/*"])

        for pattern in ("grok/runs/*", "logs/*"):
            self.assertIn(shlex.quote(pattern), command,
                          f"{pattern!r} is not shell-quoted in the outer list: {command}")
        self.assertEqual(outer_list(command), ["grok/runs/*", "logs/*"], command)
        self.assertIn("for entry in $pattern", command,
                      f"the inner expansion is not the unquoted one: {command}")

    def test_a_character_class_is_quoted_but_still_expands_inside_the_loop(self) -> None:
        command = cli._pull_inventory_command("/workspace", ["results/step-[0-9]*"])

        self.assertIn("'results/step-[0-9]*'", command, command)
        self.assertEqual(outer_list(command), ["results/step-[0-9]*"], command)
        self.assertIn("for entry in $pattern", command, command)


class TheAnswerTheFixedCommandProduces(unittest.TestCase):
    """The output of one pattern matching two directories: both lines carry the *pattern's* index
    (zero), not the match's position. The parser and the selection must accept and attribute it."""

    def setUp(self) -> None:
        self.cfg_patterns = ["grok/runs/*"]

    def test_two_matches_share_the_pattern_index_and_both_are_attributed(self) -> None:
        text = framed(
            entry_line(0, "d", 900, 10_000, "grok/runs/run-16"),
            entry_line(0, "d", 800, 10_000, "grok/runs/run-15"),
        )

        entries = cli._parse_pull_inventory(text, self.cfg_patterns)

        self.assertEqual([entry.path for entry in entries],
                         ["grok/runs/run-16", "grok/runs/run-15"])
        for entry in entries:
            self.assertEqual(entry.patterns, ("grok/runs/*",), entry)

    def test_a_declared_collection_trims_the_second_match_and_all_keeps_it(self) -> None:
        entries = cli._parse_pull_inventory(
            framed(entry_line(0, "d", 900, 10_000, "grok/runs/run-16"),
                   entry_line(0, "d", 800, 10_000, "grok/runs/run-15")),
            self.cfg_patterns)

        selected, skipped, overlap = cli._pull_selection(
            entries, all_entries=False, run_patterns=["grok/runs/*"])
        self.assertEqual([entry.path for entry in selected], ["grok/runs/run-16"])
        self.assertEqual([entry.path for entry in skipped], ["grok/runs/run-15"])
        self.assertEqual(overlap, [])

        selected, skipped, _ = cli._pull_selection(entries, all_entries=True,
                                                   run_patterns=["grok/runs/*"])
        self.assertEqual(len(selected), 2)
        self.assertEqual(skipped, [])

    def test_a_file_pattern_and_a_declared_directory_pattern_attribute_separately(self) -> None:
        entries = cli._parse_pull_inventory(
            framed(entry_line(0, "f", 900, 100, "results/final.json"),
                   entry_line(1, "d", 900, 10_000, "runs/run-16"),
                   entry_line(1, "d", 800, 10_000, "runs/run-15")),
            ["results/*.json", "runs/*"])

        selected, skipped, _ = cli._pull_selection(
            entries, all_entries=False, run_patterns=["runs/*"])
        by_path = {entry.path: entry for entry in selected}
        self.assertIn("results/final.json", by_path)
        self.assertEqual(by_path["results/final.json"].patterns, ("results/*.json",),
                         by_path["results/final.json"])
        self.assertIn("runs/run-16", by_path)
        self.assertEqual(by_path["runs/run-16"].patterns, ("runs/*",), by_path["runs/run-16"])
        self.assertEqual([entry.path for entry in skipped], ["runs/run-15"])


@unittest.skipUnless(SHELL, "no POSIX shell can run on this host (the sandbox's named-pipe refusal)")
class TheCommandExecutedInAShell(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = scratch_dir(self)
        for name in ("run-15", "run-16"):
            (self.tree / "grok" / "runs" / name).mkdir(parents=True)
            (self.tree / "grok" / "runs" / name / "out.json").write_text("{}", encoding="utf-8")
        (self.tree / "logs").mkdir()
        (self.tree / "logs" / "a.log").write_text("a", encoding="utf-8")
        (self.tree / "logs" / "b.log").write_text("b", encoding="utf-8")
        (self.tree / "results").mkdir()
        for name in ("a", "b"):
            (self.tree / "results" / f"step-{name}.json").write_text("{}", encoding="utf-8")

    def run_inventory(self, patterns: list[str]) -> tuple[str, str, str]:
        command = cli._pull_inventory_command(str(self.tree), patterns)
        result = subprocess.run([SHELL, "-c", command], cwd=self.tree, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"the inventory command failed: {result.stderr}\n{command}")
        return command, result.stdout, result.stderr

    def parse(self, patterns: list[str]) -> list:
        command, out, err = self.run_inventory(patterns)
        try:
            return cli._parse_pull_inventory(out, patterns)
        except ValueError as exc:
            self.fail(f"the command's own answer is unparseable: {exc}\n"
                      f"command: {command}\nstdout: {out!r}\nstderr: {err!r}")

    def test_one_pattern_two_directories(self) -> None:
        entries = self.parse(["grok/runs/*"])

        self.assertEqual([entry.path for entry in entries],
                         ["grok/runs/run-15", "grok/runs/run-16"])
        for entry in entries:
            self.assertEqual(entry.patterns, ("grok/runs/*",), entry)
            self.assertEqual(entry.kind, "d", entry)
            self.assertGreater(entry.bytes, 0, entry)
            self.assertGreater(entry.mtime, 0, entry)

    def test_two_configured_patterns_map_to_their_own_index(self) -> None:
        entries = self.parse(["grok/runs/*", "logs/*"])

        by_path = {entry.path: entry for entry in entries}
        for path in ("grok/runs/run-15", "grok/runs/run-16"):
            self.assertEqual(by_path[path].patterns, ("grok/runs/*",), by_path[path])
        for path in ("logs/a.log", "logs/b.log"):
            self.assertEqual(by_path[path].patterns, ("logs/*",), by_path[path])
            self.assertEqual(by_path[path].kind, "f", by_path[path])

    def test_a_character_class_and_files(self) -> None:
        entries = self.parse(["results/step-[0-9]*"])

        self.assertEqual([entry.path for entry in entries],
                         ["results/step-a.json", "results/step-b.json"])
        for entry in entries:
            self.assertEqual(entry.patterns, ("results/step-[0-9]*",), entry)
            self.assertEqual(entry.kind, "f", entry)

    def test_a_symlinked_directory_is_measured_as_a_directory(self) -> None:
        """Recorded: ``[ -d ]`` follows the link, so ``latest`` is listed as a directory; the size
        and mtime are the link's (``du``) and the target's (``stat``). The archive carries the link,
        not the target, which is why the selection prefers the newest *real* name on a tie."""
        target = self.tree / "grok" / "runs" / "run-16"
        link = self.tree / "grok" / "runs" / "latest"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"this host cannot create a directory symlink: {exc}")

        entries = self.parse(["grok/runs/*"])

        by_path = {entry.path: entry for entry in entries}
        self.assertIn("grok/runs/latest", by_path, by_path)
        self.assertEqual(by_path["grok/runs/latest"].kind, "d", by_path["grok/runs/latest"])
        self.assertLess(by_path["grok/runs/latest"].bytes, by_path["grok/runs/run-16"].bytes,
                        "the symlink was measured as its target")


if __name__ == "__main__":
    unittest.main()
