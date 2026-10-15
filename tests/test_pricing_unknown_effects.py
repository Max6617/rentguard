"""An unpriceable machine decides nothing as zero — and the money that *was* read still counts.

The third effect of the 2026-10-15 pricing-unknown ruling, at the decision itself: a machine whose
rate or uptime nobody could read is not a per-run stop (unknown is not a reason to kill a healthy
machine, and not zero either), while the **total** cap keeps acting on money that was recorded — a
lower bound a stop may act on. `tests/test_pricing_unknown.py` pins the state, the printed figure and
the guard's log; this pins what the decision does with them.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

RATE = 2.0


def pod(status: str = "RUNNING", uptime: float | None = 3600.0, *, cost: float | None = RATE) -> dict:
    payload = {"id": "pod-A", "status": status,
               "runtime": {"uptime": uptime, "ports": {"22": "tcp"}, "gpus": [{"util": 0.0}]}}
    if cost is not None:
        payload["cost"] = cost
    return payload


class AnUnknownPriceIsNotAZero(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=1.0, idle_minutes=0.0,
                                        max_hours_per_run=6.0),
                          state_dir=scratch_dir(self))
        self.ledger = Ledger(self.cfg.ledger_path)

    def test_an_unpriceable_machine_is_not_a_per_run_stop(self) -> None:
        snap = guard.snapshot(pod(cost=None), self.cfg, self.ledger)

        self.assertTrue(snap.pricing_unknown, f"a missing rate was priced: {snap}")
        self.assertIn("cost", str(snap.pricing_unknown_reason).lower(),
                      f"the reason does not name the missing field: {snap.pricing_unknown_reason}")
        self.assertIsNone(guard.decide(snap),
                          f"a healthy machine was stopped over a field nobody could read: "
                          f"{guard.decide(snap)!r}")

    def test_a_missing_uptime_is_not_a_per_run_stop_either(self) -> None:
        snap = guard.snapshot(pod(uptime=None), self.cfg, self.ledger)

        self.assertTrue(snap.pricing_unknown, f"a missing uptime was priced: {snap}")
        self.assertIsNone(guard.decide(snap),
                          f"the clock decided on an uptime nobody read: {guard.decide(snap)!r}")

    def test_money_that_was_read_still_stops_the_machine(self) -> None:
        """The total check compares the cap against recorded money — a lower bound a stop may act
        on — so an unpriceable run does not switch the cap off."""
        now = now_utc()
        self.ledger.open(purpose="earlier", provider="fake", instance_id="pod-B", gpu_id="g",
                         rate_usd_per_hour=RATE, started_at=now - timedelta(hours=1.0))
        self.ledger.close(note="down", stopped_at=now)          # $2.00 recorded, against a $1 cap
        self.cfg.budget.cap_usd = 1.0

        snap = guard.snapshot(pod(cost=None), self.cfg, self.ledger)

        self.assertTrue(snap.pricing_unknown, f"fixture: the run must be unpriceable: {snap}")
        self.assertEqual(guard.decide(snap), "total cap reached ($2.00 of $1.00)",
                         f"the cap stopped seeing money that was actually read: {snap}")


if __name__ == "__main__":
    unittest.main()
