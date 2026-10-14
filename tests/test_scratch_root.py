"""Where the suite is allowed to put scratch files, and in what order it decides.

The publish-facing behaviour is that a stranger's machine gets nothing next to their clone: scratch
goes to `RENTGUARD_SCRATCH` if the user set it, otherwise to the platform temp dir. The workspace
sibling is this sandbox's fallback, because the confinement here refuses the platform temp dir for
child processes (measured: three `PermissionError` failures when the suite used `tempfile`).

These tests drive the chooser directly, with the environment and the writability probe injected, so
the order is asserted without changing this machine's real environment.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard.tests import _scratch  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class ScratchRootOrder(unittest.TestCase):
    def test_an_explicit_rentguard_scratch_wins_over_everything(self):
        """Sequence: the user sets RENTGUARD_SCRATCH and the platform temp dir is perfectly
        writable. Effect: the user's directory is used, not the default."""
        chosen = _scratch.choose_root(env={_scratch.ENV_VAR: r"C:\somewhere\my-scratch"},
                                      temp_dir=Path("C:/temp"), writable=lambda path: True)

        self.assertEqual(chosen, Path(r"C:\somewhere\my-scratch"),
                         "an explicit RENTGUARD_SCRATCH was ignored")

    def test_a_writable_platform_temp_dir_is_preferred(self):
        """The publish-facing case: on an ordinary machine, scratch lands in the platform temp dir,
        so the suite leaves nothing beside the checkout."""
        seen: list[Path] = []

        def writable(path: Path) -> bool:
            seen.append(path)
            return True

        chosen = _scratch.choose_root(env={}, temp_dir=Path("C:/temp"), writable=writable)

        expected = Path("C:/temp") / _scratch.TEMP_SUBDIR
        self.assertEqual(chosen, expected,
                         f"a writable temp dir was not used: chose {chosen}")
        self.assertEqual(seen, [expected], "the chooser probed something other than the temp dir")

    def test_the_workspace_sibling_is_only_a_fallback(self):
        """Sequence: the platform temp dir refuses writes (this sandbox). Effect: the sibling is
        used, and that is a documented environment workaround, not the design."""
        chosen = _scratch.choose_root(env={}, temp_dir=Path("C:/temp"),
                                      writable=lambda path: False)

        self.assertEqual(chosen, _scratch.SIBLING_FALLBACK)

    def test_an_override_wins_even_when_nothing_else_is_writable(self):
        chosen = _scratch.choose_root(env={_scratch.ENV_VAR: "D:/scratch"},
                                      temp_dir=Path("C:/temp"), writable=lambda path: False)

        self.assertEqual(chosen, Path("D:/scratch"))

    def test_the_probe_writes_reads_and_removes_rather_than_guessing(self):
        """The probe is the thing the decision rests on, so it is asserted directly: a real
        directory passes, and a path under a file does not."""
        root = scratch_dir(self)
        a_file = root / "not-a-directory"
        a_file.write_text("x", encoding="utf-8")

        self.assertTrue(_scratch.is_writable(root), "a plain writable directory was called unwritable")
        self.assertFalse(_scratch.is_writable(a_file / "sub"),
                         "a path under a file was called writable")
        self.assertEqual([p.name for p in root.iterdir()], ["not-a-directory"],
                         "the probe left a file behind")

    def test_the_suite_actually_uses_the_chosen_root(self):
        """Effect: a scratch directory handed to a test is inside this run's directory under the
        root the chooser picked, so the policy above is not decoration."""
        root = scratch_dir(self)
        run = _scratch.run_root()
        chosen = _scratch.scratch_root()

        self.assertEqual(run.parent, chosen, f"{run} is not under the chosen scratch root {chosen}")
        self.assertEqual(root.parent, run, f"{root} is not inside this run's directory {run}")

    def test_every_run_gets_its_own_named_directory(self):
        """Effect: two runs in one checkout cannot collide, because the run directory carries the
        process id and a random suffix."""
        run = _scratch.run_root()

        self.assertTrue(run.name.startswith(_scratch.RUN_PREFIX),
                        f"the run directory is not named for a run: {run.name}")
        self.assertTrue(run.is_dir(), f"the run directory does not exist: {run}")

    def test_stale_run_directories_are_removed_and_live_ones_are_left(self):
        """Effect: leftovers from an earlier run are swept at start, while a directory that may
        belong to a run still in flight is left alone."""
        base = scratch_dir(self)
        stale = base / f"{_scratch.RUN_PREFIX}999-old"
        stale.mkdir()
        (stale / "leftover.txt").write_text("x", encoding="utf-8")
        old = time.time() - 7200
        os.utime(stale, (old, old))
        fresh = base / f"{_scratch.RUN_PREFIX}998-maybe-live"
        fresh.mkdir()

        removed = _scratch.sweep_stale(base, keep=fresh)

        self.assertIn(stale.name, removed, f"the stale directory was not swept: {removed}")
        self.assertFalse(stale.exists(), "a stale run directory survived the sweep")
        self.assertTrue(fresh.exists(), "a directory that may belong to a live run was removed")

    def test_a_runs_directory_is_removed_by_cleanup(self):
        """Effect: the run cleans up after itself, so a stranger's checkout does not accumulate
        one directory per run."""
        original = _scratch._run_dir
        victim = scratch_dir(self) / "victim"
        victim.mkdir()
        _scratch._run_dir = victim
        try:
            _scratch.cleanup_run()
            self.assertFalse(victim.exists(), "cleanup left the run directory behind")
            _scratch.cleanup_run()   # idempotent
        finally:
            _scratch._run_dir = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
