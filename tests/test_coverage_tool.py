"""The coverage gate's divisor counts **lines**, not line-table artefacts.

The GitHub CI failed the `Coverage` step on Python 3.13 only (`test (3.13)`, 2026-10-15) while 3.11 and
3.12 were green: `TOTAL 2769 lines, 2710 hit = 97.9%` against a 98.0% floor. The cause was measured
rather than assumed — `tools/executable_lines.py`'s per-file diff over the same checkout found **zero**
integer line numbers differing between 3.12.6 and 3.13.15, and exactly one `None` entry per module that
3.13's `dis.findlinestarts` yields and 3.12 skips. A `None` is not a line and `frame.f_lineno` is never
`None`, so it can only ever be an unhittable denominator entry: a version-dependent penalty, not a
coverage result. A `0` behaves the same way on both.

What is pinned here is the *fix and its direction*: the artefact is dropped, and the floor does **not**
move to make the red go away. A lower number with no sentence is not a fix.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
REPO = Path(__file__).resolve().parents[1]      # the repository root *is* the package
sys.path.insert(0, str(WS))

_SPEC = importlib.util.spec_from_file_location("rentguard_coverage_tool", REPO / "tools" / "coverage.py")
coverage_tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(coverage_tool)


class TheDivisorCountsLines(unittest.TestCase):
    def test_a_none_entry_is_not_a_line(self) -> None:
        """The 3.13 artefact: one per file, counted by `len(set)` and impossible to hit."""
        with mock.patch.object(coverage_tool, "_find_executable_linenos",
                               staticmethod(lambda path: [1, 2, None, 4])):
            self.assertEqual(coverage_tool._executable_lines(Path("x.py")), {1, 2, 4})

    def test_a_zero_entry_is_not_a_line_either(self) -> None:
        with mock.patch.object(coverage_tool, "_find_executable_linenos",
                               staticmethod(lambda path: [0, 3, 5])):
            self.assertEqual(coverage_tool._executable_lines(Path("x.py")), {3, 5})

    def test_the_divisor_of_a_real_module_is_lines_only(self) -> None:
        lines = coverage_tool._executable_lines(REPO / "session.py")

        self.assertTrue(lines, "the divisor is empty for a module that has lines")
        self.assertTrue(all(isinstance(n, int) and n > 0 for n in lines),
                        f"a non-line survived the filter: {sorted(lines)[:5]}")

    def test_the_floor_did_not_move(self) -> None:
        """The CI red was fixed by counting the right thing, not by lowering the bar: 98.0 with the
        denominator and the reason beside it, or 98.0 because the artefact is gone."""
        self.assertEqual(coverage_tool.DEFAULT_FLOOR, 0.98,
                         "the floor was changed; a per-interpreter floor needs its denominator and its "
                         "reason written down, which this is not")


class ARedSuiteIsNotAGreenGate(unittest.TestCase):
    """Defect 21: the gate printed `target met`, exit 0, over `1 failed, 499 passed`.

    `pytest.main` returns a count rather than a verdict, and lines run whether or not the assertions
    hold — so a publish precondition reading "the gate is green" could be met by a red tree. Keel's
    gate states the rule; this pins it here.
    """

    def run_gate(self, rows: dict[str, tuple[int, int]], suite_status: int, *argv: str) -> int:
        with mock.patch.object(coverage_tool, "measure",
                               lambda args: (rows, set(), suite_status)), \
                mock.patch.object(sys, "argv", ["coverage.py", *argv]):
            return coverage_tool.main()

    def test_a_failing_suite_fails_the_gate_even_above_the_floor(self) -> None:
        code = self.run_gate({"cli.py": (100, 100)}, suite_status=1)

        self.assertEqual(code, 1,
                         "a gate over a failing suite exited 0: a publish precondition that reads "
                         "\"the gate is green\" can then be met by a red tree")

    def test_a_failing_suite_fails_even_below_a_lowered_floor(self) -> None:
        """The suite's verdict comes first, so no floor setting can turn a red suite green."""
        code = self.run_gate({"cli.py": (100, 100)}, 1, "--min", "0")

        self.assertEqual(code, 1, "a hand-set floor turned a red suite into a green gate")

    def test_a_passing_suite_still_gates_on_the_floor(self) -> None:
        """The control: the floor is still what fails a green suite that measured too little."""
        code = self.run_gate({"cli.py": (1, 4)}, suite_status=0)

        self.assertEqual(code, 1, "a green suite below the floor passed the gate")

    def test_a_passing_suite_above_the_floor_passes(self) -> None:
        code = self.run_gate({"cli.py": (100, 100)}, suite_status=0)

        self.assertEqual(code, 0, "a green suite above the floor failed the gate")


if __name__ == "__main__":
    unittest.main()
