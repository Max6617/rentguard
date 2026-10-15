"""A live machine with a closed row is priced for the time **since that row closed** — never twice.

Defect 15's first fix (`cdb2fba`) stopped a closed row being re-priced while the machine was EXITED,
but it gated that on the machine's status, so a machine that was still RUNNING when its row closed was
priced from its whole uptime on top of the row that had already paid for part of it. Assay measured the
effect: a row closed at $6.00, a machine RUNNING with 4 h of uptime at $2/h, a $15 cap — `spent_usd`
read $14.00 against a machine that could have billed $8.00, and that figure is what the stop decision
runs on.

The rule pinned here: a closed row for this instance is the anchor for what is left, the remainder is
``rate × (now − the row's end)`` floored at zero, the anchor is the row's own end and not "now minus
uptime", and where that end cannot be read the remainder is **unknown** rather than zero. The other
directions — terminal statuses stay at the ledger's figure, an unrecorded live machine is still priced
from its uptime, another instance's closed row hides nothing — are in
`tests/test_guard_closed_row_attack.py` and `tests/test_guard_closed_row.py`.
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


def pod(status: str = "RUNNING", uptime: float = 18000.0, *, cost: float = RATE,
        ident: str = "pod-A") -> dict:
    payload = {"id": ident, "status": status, "cost": cost,
               "runtime": {"uptime": uptime, "ports": {"22": "tcp"}, "gpus": [{"util": 0.0}]}}
    return payload


class ARemainderIsMeasuredFromTheRowsOwnEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=20.0, per_run_cap_usd=20.0, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.ledger = Ledger(self.cfg.ledger_path)

    def close_a_row(self, *, opened_hours_ago: float, closed_hours_ago: float,
                    rate: float = RATE, void: bool = False) -> dict:
        now = now_utc()
        self.ledger.open(purpose="batch", provider="fake", instance_id="pod-A", gpu_id="g",
                         rate_usd_per_hour=rate,
                         started_at=now - timedelta(hours=opened_hours_ago))
        return self.ledger.close(note="down", stopped_at=now - timedelta(hours=closed_hours_ago),
                                 instance_status="RUNNING", void=void)

    def corrupt_last_row(self, fields: dict) -> None:
        rows = self.ledger.entries()
        rows[-1].update(fields)
        self.ledger._rewrite(rows)

    def test_the_remainder_is_the_hours_since_the_row_closed(self) -> None:
        self.close_a_row(opened_hours_ago=3.0, closed_hours_ago=2.0)     # $6.00 covered
        snap = guard.snapshot(pod(uptime=18000.0), self.cfg, self.ledger)  # 5 h old

        self.assertAlmostEqual(snap.run_usd, 4.0, delta=0.05,
                               msg=f"the machine's hours were not priced from the row's end: {snap}")
        self.assertEqual(snap.priced_from, "since-close", f"the provenance is not named: {snap}")
        self.assertTrue(snap.recorded, "a machine whose row closed was called unrecorded")
        self.assertAlmostEqual(snap.spent_usd, self.ledger.total_usd() + snap.run_usd, delta=1e-6,
                               msg=f"the reading is not the closed figure plus the remainder: {snap}")

    def test_a_row_closed_this_instant_leaves_no_remainder(self) -> None:
        self.close_a_row(opened_hours_ago=1.0, closed_hours_ago=0.0)
        snap = guard.snapshot(pod(uptime=3600.0), self.cfg, self.ledger)

        self.assertAlmostEqual(snap.run_usd, 0.0, delta=0.01,
                               msg=f"a machine whose row closed now was charged a remainder: {snap}")

    def test_the_reading_never_exceeds_the_machines_own_bill(self) -> None:
        self.close_a_row(opened_hours_ago=4.0, closed_hours_ago=1.0)
        ceiling = self.ledger.total_usd() + RATE + 0.05
        snap = guard.snapshot(pod(uptime=4 * 3600.0), self.cfg, self.ledger)

        self.assertLessEqual(snap.spent_usd, ceiling,
                             f"the reading ${snap.spent_usd} exceeds the machine's own bill "
                             f"(${ceiling}): the closed window was counted twice")

    def test_a_terminal_machine_keeps_the_ledger_figure(self) -> None:
        self.close_a_row(opened_hours_ago=4.0, closed_hours_ago=2.0)
        snap = guard.snapshot(pod("EXITED", 18000.0), self.cfg, self.ledger)

        self.assertEqual(snap.run_usd, 0.0,
                         f"a finished machine was priced a remainder: {snap}")
        self.assertAlmostEqual(snap.spent_usd, self.ledger.total_usd(), delta=1e-6)


class AnUnreadableAnchorIsNotARemainder(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=20.0, per_run_cap_usd=20.0, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.ledger = Ledger(self.cfg.ledger_path)
        now = now_utc()
        self.row = self.ledger.open(purpose="batch", provider="fake", instance_id="pod-A",
                                    gpu_id="g", rate_usd_per_hour=RATE,
                                    started_at=now - timedelta(hours=3.0))
        self.closed = self.ledger.close(note="down", stopped_at=now - timedelta(hours=2.0),
                                        instance_status="RUNNING")

    def corrupt_last_row(self, fields: dict) -> None:
        rows = self.ledger.entries()
        rows[-1].update(fields)
        self.ledger._rewrite(rows)

    def test_an_unreadable_end_is_unknown_rather_than_zero(self) -> None:
        self.corrupt_last_row({"stopped_at": "the-stop-was-truncated",
                               "started_at": "the-start-was-truncated",
                               "wall_clock_seconds": None})
        snap = guard.snapshot(pod(uptime=18000.0), self.cfg, self.ledger)

        self.assertTrue(snap.accrual_unknown,
                        f"an unreadable anchor priced the remainder as a number: {snap}")
        self.assertIsNone(snap.priced_from, f"a remainder was claimed with no anchor: {snap}")
        self.assertAlmostEqual(snap.spent_usd, self.ledger.total_usd(), delta=1e-6,
                               msg=f"the reading exceeds the ledger's own figure: {snap}")

    def test_the_end_is_reconstructed_when_only_the_stop_is_unreadable(self) -> None:
        """The row carries the same instant twice: `started_at` + `wall_clock_seconds` is the stop
        `close` derived. A reader that gave up on the first field would call a readable end unknown."""
        self.corrupt_last_row({"stopped_at": "the-stop-was-truncated"})
        snap = guard.snapshot(pod(uptime=18000.0), self.cfg, self.ledger)

        self.assertEqual(snap.priced_from, "since-close",
                         f"the reconstructable end was called unknown: {snap}")
        self.assertAlmostEqual(snap.run_usd, 4.0, delta=0.05,
                               msg=f"the remainder was not priced from the row's own end: {snap}")

    def test_a_voided_row_does_not_anchor_a_remainder(self) -> None:
        """A voided row contributed nothing to the total, so it paid for none of the machine's
        minutes and must not be treated as the end of a window that was charged for."""
        self.corrupt_last_row({"voided": True, "estimated_usd": 0.0})
        snap = guard.snapshot(pod(uptime=3600.0), self.cfg, self.ledger)

        self.assertFalse(snap.recorded, "a voided row was read as recording this machine")
        self.assertIsNone(snap.priced_from, f"a voided row anchored a remainder: {snap}")
        self.assertAlmostEqual(snap.run_usd, RATE, delta=0.01,
                               msg=f"an unrecorded live machine stopped being priced: {snap}")


if __name__ == "__main__":
    unittest.main()
