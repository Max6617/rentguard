"""A closed row is the ledger's figure, never re-derived from a stale uptime (defect 15).

Cadence's `guard-watch-batchA-2.log`: on the sample *after* the batch-A row had closed (machine
already EXITED, stopped by the driver's own `down`) the guard printed

    run $5.941 (unrecorded) total $15.581/$15.00

— it read the closed row as an unrecorded instance and added a fresh accrued estimate on top of it, a
transient total **above** a cap that was never reached. The ledger was right all along. Nothing was
live, so no stop ran; on a live machine that total is what the stop decision runs on.

The invariants, pinned here:

1. a sample after a row has closed must not price that instance through the unrecorded branch;
2. no reading may exceed the ledger's own closed + accruing;
3. a closed row's cost is the ledger's figure, never re-derived from the provider's clock.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def pod(status: str, uptime: float, rate: float = 2.0) -> dict:
    return {"id": "pod-A", "status": status, "cost": rate,
            "runtime": {"uptime": uptime, "ports": {"22": "tcp"}, "gpus": [{"util": 0.0}]}}


class AClosedRowIsNotAnUnrecordedMachine(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=15.0, per_run_cap_usd=15.0, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.ledger = Ledger(self.cfg.ledger_path)

    def close_a_row(self, hours: float = 2.0, rate: float = 2.0) -> dict:
        self.ledger.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
                         rate_usd_per_hour=rate, started_at=now_utc() - timedelta(hours=hours))
        return self.ledger.close(note="down", stopped_at=now_utc(), instance_status="RUNNING")

    def test_the_sample_after_the_close_does_not_price_the_run_again(self):
        closed = self.close_a_row(hours=2.0, rate=2.0)          # $4.00 recorded
        # The provider still reports an uptime for the finished machine: that is what was priced.
        snap = guard.snapshot(pod("EXITED", 7200.0), self.cfg, self.ledger)

        self.assertEqual(snap.run_usd, 0.0,
                         f"a closed row's cost was re-derived from uptime: {snap.run_usd}")
        self.assertTrue(snap.recorded, "a machine with a closed row was called unrecorded")
        self.assertAlmostEqual(snap.spent_usd, self.ledger.total_usd(), delta=1e-6)
        self.assertAlmostEqual(closed["estimated_usd"], 4.0, delta=0.01)

    def test_the_reading_never_exceeds_the_ledgers_own_total(self):
        self.close_a_row(hours=3.0, rate=2.0)                   # $6.00 against a $15 cap
        snap = guard.snapshot(pod("EXITED", 10800.0), self.cfg, self.ledger)

        ledger_total = self.ledger.total_usd() + self.ledger.live_usd()
        self.assertLessEqual(snap.spent_usd, ledger_total + 1e-6,
                             f"the reading ${snap.spent_usd} exceeded the ledger's ${ledger_total}")
        # "already stopped" is the correct verdict for a finished machine; what must not happen is a
        # *money* reason firing off a re-derived accrual.
        reason = guard.decide(snap)
        self.assertIn(reason, (None, "already stopped"),
                      f"a finished rental was stopped on a money reason: {reason!r}")

    def test_a_live_unrecorded_machine_is_still_priced_from_its_uptime(self):
        """Control: the defect is the double count, not the witness."""
        snap = guard.snapshot(pod("RUNNING", 3600.0), self.cfg, self.ledger)

        self.assertAlmostEqual(snap.run_usd, 2.0, delta=0.01,
                               msg="an unrecorded live machine stopped being priced")
        self.assertFalse(snap.recorded, "an unrecorded machine was called recorded")


if __name__ == "__main__":
    unittest.main()
