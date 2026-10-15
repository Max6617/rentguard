"""Independent attack on the defect-15 fix (`cdb2fba`) — my own construction, not a re-read.

Gilbert's rule for a fix like this: try to make a closed row contribute to the guard's money
again, *and* try to make a live unrecorded machine stop being priced — the second is what such a
fix usually breaks while proving the first.

What holds at `cdb2fba`, measured rather than assumed:

- a terminal machine with a closed row for it is not re-priced (``run_usd == 0``, ``spent_usd ==
  total_usd()``, ``recorded`` true) with an uptime ten times the row's window;
- a terminal machine with *no* row for it invents nothing either: it reads the ledger's total and
  ``recorded`` false;
- a live, unrecorded machine is still priced from its own uptime, and the cap fires on it;
- a crossing between two polls is caught at the first sample after it.

What does not hold: the new branch is ``status in TERMINAL_STATUSES``, so the same closed row is
re-priced from the whole uptime for every **live** status — ``RUNNING``, ``PROVISIONING`` and
``STARTING`` (``LIVE_STATUSES``). The ``RUNNING`` half is pinned in
``tests/test_money_direction_shapes.py``; the ``PROVISIONING``/``STARTING`` half here. The fix is
gated by the machine's status, not by "a closed row already paid for part of this machine".
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


def pod(status: str, uptime, *, cost: float | None = RATE, ident: str = "pod-A") -> dict:
    payload = {"id": ident, "status": status,
               "runtime": {"uptime": uptime, "ports": {"22": "tcp"},
                           "gpus": [{"util": 0.0}]}}
    if cost is not None:
        payload["cost"] = cost
    return payload


def cfg_for(cap: float, root: Path) -> Config:
    return Config(budget=Budget(cap_usd=cap, per_run_cap_usd=cap, idle_minutes=0.0),
                  state_dir=root / ".rentguard")


def close_the_row(ledger: Ledger, *, hours_ago_closed: float | None = None,
                  status: str = "EXITED") -> dict | None:
    return ledger.close(
        note="down",
        stopped_at=None if hours_ago_closed is None
        else now_utc() - timedelta(hours=hours_ago_closed),
        instance_status=status)


class ATerminalMachineIsNeverRePriced(unittest.TestCase):
    def test_a_closed_row_is_the_whole_truth_with_ten_times_the_uptime(self) -> None:
        cfg = cfg_for(15.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        ledger.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
                    rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
        close_the_row(ledger)

        snap = guard.snapshot(pod("EXITED", 36000.0), cfg, ledger)   # 10 h of uptime available

        self.assertEqual(snap.run_usd, 0.0,
                         f"the closed row was priced again from the machine's uptime: "
                         f"run_usd={snap.run_usd}")
        self.assertAlmostEqual(snap.spent_usd, ledger.total_usd(), delta=1e-6,
                               msg=f"the reading is not the ledger's figure: {snap}")
        self.assertTrue(snap.recorded, "a machine with a closed row was called unrecorded")
        reason = guard.decide(snap)
        self.assertIn(reason, (None, "already stopped"),
                      f"the finished rental fired a money reason off a re-derived accrual: {reason!r}")

    def test_a_terminal_machine_with_no_row_invents_nothing(self) -> None:
        cfg = cfg_for(15.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        ledger.open(purpose="other", provider="fake", instance_id="pod-B", gpu_id="g",
                    rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
        close_the_row(ledger)

        snap = guard.snapshot(pod("EXITED", 36000.0, ident="pod-A"), cfg, ledger)

        self.assertEqual(snap.run_usd, 0.0,
                         f"a finished machine with no row was priced from its stale uptime: "
                         f"{snap.run_usd}")
        self.assertAlmostEqual(snap.spent_usd, ledger.total_usd(), delta=1e-6, msg=str(snap))


class ALiveMachineWithAClosedRowIsStillRePriced(unittest.TestCase):
    def test_a_starting_machine_is_not_priced_from_its_whole_uptime(self) -> None:
        """The gap: the branch is `status in TERMINAL_STATUSES`, so every live status falls through
        to the unrecorded estimate. `PROVISIONING` is not terminal and is in `LIVE_STATUSES`.

        Effect required: the reading never exceeds what the machine can have billed — the closed
        row's figure plus the time since it closed. At `cdb2fba` it reads $14.00 for an $8.00 bill.
        """
        cfg = cfg_for(15.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        ledger.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
                    rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=4))
        close_the_row(ledger, hours_ago_closed=1, status="PROVISIONING")
        ceiling = round(ledger.total_usd() + 2.0 + 0.05, 6)   # + the hour since, at $2/h

        snap = guard.snapshot(pod("PROVISIONING", 14400.0), cfg, ledger)

        self.assertLessEqual(
            snap.spent_usd, ceiling,
            f"the reading ${snap.spent_usd} exceeds what the machine can have billed "
            f"(${ceiling}): the closed row's ${ledger.total_usd()} was priced again from uptime "
            f"{snap.uptime_hours} h")


class ALiveUnrecordedMachineIsStillPriced(unittest.TestCase):
    def test_the_cap_still_fires_on_an_unrecorded_machine(self) -> None:
        cfg = cfg_for(1.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)                       # no rows at all

        snap = guard.snapshot(pod("RUNNING", 3600.0), cfg, ledger)

        self.assertAlmostEqual(snap.run_usd, RATE, delta=0.01,
                               msg=f"a live unrecorded machine stopped being priced: {snap}")
        self.assertFalse(snap.recorded, "an unrecorded machine was called recorded")
        self.assertIsNotNone(guard.decide(snap),
                             f"the cap did not fire on a live unrecorded machine: {snap}")

    def test_another_instances_closed_row_does_not_hide_a_live_machine(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        ledger.open(purpose="other", provider="fake", instance_id="pod-B", gpu_id="g",
                    rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
        close_the_row(ledger)

        snap = guard.snapshot(pod("RUNNING", 3600.0, ident="pod-A"), cfg, ledger)

        self.assertAlmostEqual(snap.run_usd, RATE, delta=0.01,
                               msg=f"a closed row for another instance hid a live machine: {snap}")


class APollCrossingIsCaughtAtTheFirstSampleAfter(unittest.TestCase):
    def test_under_the_cap_is_kept_and_over_it_is_stopped(self) -> None:
        cfg = cfg_for(1.0, scratch_dir(self))
        cfg.budget.per_run_cap_usd = 1.0
        ledger = Ledger(cfg.ledger_path)
        started = now_utc() - timedelta(hours=1)
        ledger.open(purpose="fast meter", provider="fake", instance_id="pod-A", gpu_id="g",
                    rate_usd_per_hour=60.0, started_at=started)

        before = guard.snapshot(pod("RUNNING", 59.0, cost=60.0), cfg, ledger,
                                at=started + timedelta(seconds=59))
        after = guard.snapshot(pod("RUNNING", 61.0, cost=60.0), cfg, ledger,
                               at=started + timedelta(seconds=61))

        self.assertLess(before.spent_usd, 1.0, f"the sample under the cap is already over: {before}")
        self.assertIsNone(guard.decide(before),
                          f"the guard stopped a machine still under its cap: {guard.decide(before)!r}")
        self.assertIsNotNone(guard.decide(after),
                             f"the sample past the cap fired no reason: {after}")


if __name__ == "__main__":
    unittest.main()
