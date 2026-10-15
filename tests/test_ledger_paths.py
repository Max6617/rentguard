"""The ledger's uncovered paths: spend attribution, the void rule, the lock, and the job log.

`ledger.py` is where the money is recorded, and at 75.5% the uncovered lines were the ones that only
run under stress: a second concurrent writer, a lock left behind by a killed process, a row for a
machine that vanished, and a corrupt line in the file. Those are exactly the cases that decide whether
the total is trustworthy, so they are tested directly rather than through the CLI.

Nothing here needs a network or a rental.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _scratch import scratch_dir  # noqa: E402 - the suite's tempfile-free scratch helper
from rentguard.ledger import JobLog, Ledger, LedgerLocked  # noqa: E402

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        # Workspace-local scratch, not `tempfile`: the sandbox refuses the chmod a mode-0700 temp
        # directory needs, so `tempfile.TemporaryDirectory` fails here *after* the assertions — a
        # green test reported as an error. Forge measured 16 such failures against the first version
        # of this file, which passed in my environment. Environment-dependent tests are worse than
        # none, so this uses the same helper as the rest of the suite.
        self.root = scratch_dir(self)
        self.ledger = Ledger(self.root / "runs.jsonl")

    def opened(self, rate=2.0, hours_ago=1.0, purpose="a run"):
        return self.ledger.open(purpose=purpose, provider="test", instance_id="pod-1",
                                gpu_id="G", rate_usd_per_hour=rate,
                                started_at=T0 - timedelta(hours=hours_ago))


class SpentOnTest(LedgerCase):
    """Attributing spend to one run, which `doctor` and the cap use."""

    def test_a_closed_runs_cost_is_returned(self) -> None:
        run = self.opened(rate=2.0, hours_ago=1.0)  # started one hour before T0
        self.ledger.close(note="down", stopped_at=T0)
        self.assertAlmostEqual(self.ledger.spent_on(run["run_id"]), 2.0, places=6)

    def test_an_unknown_run_costs_nothing(self) -> None:
        self.assertEqual(self.ledger.spent_on("never-existed"), 0.0)

    def test_an_open_run_has_no_recorded_cost_yet(self) -> None:
        run = self.opened()
        self.assertEqual(self.ledger.spent_on(run["run_id"]), 0.0)


class AppendTest(LedgerCase):
    def test_appending_a_finished_row_computes_its_cost(self) -> None:
        # A row written by something other than `close` (a migration, a hand-written entry) still has
        # to carry arithmetic that agrees with everyone else's.
        row = self.ledger.append({
            "run_id": "hand-written", "purpose": "imported", "provider": "test",
            "instance_id": "pod-9", "gpu_id": "G", "rate_usd_per_hour": 3.0,
            "started_at": iso(T0), "stopped_at": iso(T0 + timedelta(hours=2)),
            "wall_clock_seconds": 7200, "wall_clock_hours": 2.0,
            "estimated_usd": None, "closed_by": "import", "tag": "",
        })
        self.assertAlmostEqual(row["estimated_usd"], 6.0, places=6)
        self.assertAlmostEqual(self.ledger.total_usd(), 6.0, places=6)


class VoidTest(LedgerCase):
    """A row whose machine never existed must close, contribute nothing, and stay visible."""

    def test_a_voided_row_contributes_nothing_to_the_total(self) -> None:
        self.opened(rate=99.0)
        closed = self.ledger.close(note="guard-instance-gone", void=True)
        self.assertTrue(closed["voided"])
        self.assertEqual(closed["voided_reason"], "guard-instance-gone")
        self.assertEqual(closed["estimated_usd"], 0.0)
        self.assertEqual(self.ledger.total_usd(), 0.0)

    def test_a_voided_row_is_still_in_the_ledger(self) -> None:
        # The gap has to be visible in `ledger` rather than disappearing from the books.
        self.opened()
        self.ledger.close(note="void it", void=True)
        rows = self.ledger.entries()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["voided"])
        self.assertIsNotNone(rows[0]["stopped_at"])

    def test_closing_an_empty_ledger_is_not_an_error(self) -> None:
        self.assertIsNone(self.ledger.close(note="down"))


class LockTest(LedgerCase):
    """Two writers, and a writer that died holding the lock. Both decide whether the books are safe."""

    def test_the_lock_is_released_after_use(self) -> None:
        lock = self.ledger._lock_path()
        with self.ledger._lock():
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_a_fresh_lock_held_elsewhere_causes_a_clear_failure(self) -> None:
        lock = self.ledger._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("held", encoding="utf-8")
        with self.assertRaises(LedgerLocked) as caught:
            with self.ledger._lock(timeout=0.05, stale_after=600):
                pass
        self.assertIn("rentguard process", str(caught.exception))
        lock.unlink()

    def test_a_stale_lock_file_is_broken_and_the_write_proceeds(self) -> None:
        lock = self.ledger._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("killed mid-write", encoding="utf-8")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with self.ledger._lock(timeout=0.05, stale_after=60):
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_a_stale_lock_directory_is_broken_too(self) -> None:
        # Windows has no atomic file create that fails on an existing path in every Python version,
        # so a lock can be a directory; both shapes must be recognised as stale.
        lock = self.ledger._lock_path()
        lock.mkdir(parents=True, exist_ok=True)
        old = time.time() - 3600
        os.utime(lock, (old, old))
        self.assertTrue(Ledger._break_lock_if_stale(lock, 60))
        self.assertFalse(lock.exists())

    def test_a_fresh_lock_directory_is_broken_regardless_of_age(self) -> None:
        # The lock this code takes is always a file, so a directory is never a live lock: it is a
        # leftover, and waiting for it to age out would block every write for `stale_after`. On
        # Windows `os.open` on a directory raised PermissionError and the write proceeded unlocked,
        # which hid this; on POSIX it raised FileExistsError and the write waited out the timeout.
        lock = self.ledger._lock_path()
        lock.mkdir(parents=True, exist_ok=True)
        self.assertTrue(Ledger._break_lock_if_stale(lock, 600))
        self.assertFalse(lock.exists())

    def test_a_fresh_lock_is_not_broken(self) -> None:
        lock = self.ledger._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("fresh", encoding="utf-8")
        self.assertFalse(Ledger._break_lock_if_stale(lock, 600))
        self.assertTrue(lock.exists())
        lock.unlink()

    def test_breaking_a_lock_that_vanished_is_not_an_error(self) -> None:
        missing = self.root / "nothing.lock"
        self.assertFalse(Ledger._break_lock_if_stale(missing, 0))

    def test_the_lock_is_not_reentrant(self) -> None:
        # A nested acquire must fail rather than silently interleave with the outer one; the first
        # version of this test called a write inside a held lock and deadlocked, which is the same
        # property seen from the other side.
        with self.ledger._lock():
            with self.assertRaises(LedgerLocked):
                with self.ledger._lock(timeout=0.05):
                    pass

    def test_two_sequential_writes_both_land(self) -> None:
        self.opened(purpose="first")
        self.ledger.close(note="down", stopped_at=T0)
        self.opened(purpose="second")
        self.assertEqual(len(self.ledger.entries()), 2)


class JobLogTest(LedgerCase):
    def test_entries_reads_what_append_wrote(self) -> None:
        jobs = JobLog(self.root / "jobs.jsonl")
        jobs.append({"command": "python train.py", "exit_code": 0})
        jobs.append({"command": "python eval.py", "exit_code": 1})
        self.assertEqual([j["exit_code"] for j in jobs.entries()], [0, 1])

    def test_a_blank_line_is_skipped(self) -> None:
        path = self.root / "jobs.jsonl"
        path.write_text('{"exit_code": 0}\n\n   \n', encoding="utf-8")
        self.assertEqual(len(JobLog(path).entries()), 1)

    def test_a_corrupt_line_does_not_lose_the_others(self) -> None:
        path = self.root / "jobs.jsonl"
        path.write_text('{"exit_code": 0}\n{not json\n{"exit_code": 2}\n', encoding="utf-8")
        self.assertEqual([j["exit_code"] for j in JobLog(path).entries()], [0, 2])

    def test_a_missing_job_log_is_empty_not_an_error(self) -> None:
        self.assertEqual(JobLog(self.root / "absent.jsonl").entries(), [])


class RewriteTest(LedgerCase):
    def test_a_rewrite_leaves_one_row_per_line_and_no_temporaries(self) -> None:
        self.opened()
        self.ledger.close(note="down", stopped_at=T0 + timedelta(minutes=30))
        text = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])
        leftovers = [f.name for f in self.root.iterdir() if f.name.startswith(".runs-")]
        self.assertEqual(leftovers, [], "a temporary ledger file was left behind")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    unittest.main()
