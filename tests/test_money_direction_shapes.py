"""Money-direction shapes the suite did not cover, attacked at `7f52cc3` (red-first, 2026-10-15).

Every case is a sequence of ordinary commands against the in-memory fake, and every assertion is
about the effect on money: how many machines are billing afterwards, and whether what the tool
would enforce (closed rows plus the open run's accrual) matches the machine's own bill. The
provider is injected, so no ambient ``RENTGUARD_PROVIDER`` can change what these measure.

Red at `7f52cc3`. Case by case, with the severity call:

1. ``ALiveMachineNamedByAnOpenRowIsNotSuperseded`` — with the state file lost (a shape the code
   says out loud it survives), a live machine the *ledger* names is not reused and not refused:
   ``up`` closes that machine's row and rents a second one. The first is then billing with no open
   row and no guard — a bill the cap cannot see. SEVERE: money at risk, and a success reported.
2. ``ALiveMachineNamedByAClosedRowIsNotExempt`` — a live machine whose id appears only in a CLOSED
   row is exempt from ``up``'s untracked-instance refusal, so it rents a second machine while the
   first bills. SEVERE: the same unrecorded, unguarded bill.
3. ``AClosedRowOnALiveMachineIsNotRePriced`` — a row closed while its machine is still RUNNING is
   priced a second time from the machine's whole uptime, so ``guard.decide`` stops a live machine
   whose real spend is under the cap. The same root cause as defect 15; this case is the severe
   half (a stop on a live machine), kept for the post-fix verification.
4. ``TwoRowsNamingOneInstanceDoNotDoubleCount`` — after a refused stop, ``up`` opens a second row
   for the same machine, backdated to its boot, and the ledger reads $12 for a $6 bill. Same
   family as 3; conservative direction, so NOT severe, but it produces a false stop.
5. ``AnUnreadableAccrualIsNotPrintedAsAWholeZero`` — an open row whose start is unreadable and
   whose pod cannot witness it is printed as ``$0.000`` with ``accrual_unknown=False``; the field
   documented to mark exactly this is never set. NOT severe: ``up`` refuses and ``watch`` keeps
   watching, so no machine is left unstopped — the reading is the false part.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cfg_for(cap: float, root: Path) -> Config:
    return Config(budget=Budget(cap_usd=cap, per_run_cap_usd=cap, idle_minutes=0.0),
                  state_dir=root / ".rentguard")


def live_pods(provider: FakeProvider) -> list[dict]:
    return [p for p in provider.list_pods() if p.get("status") == "RUNNING"]


def run_up(cfg: Config, provider: FakeProvider) -> tuple[int, dict, str]:
    """`up` with the provider injected. The document is whatever `up` emitted."""
    args = cli.build_parser().parse_args(["up", "--purpose", "second", "--json"])
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", lambda c: provider), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.cmd_up(args)
    try:
        document = json.loads(out.getvalue()) if out.getvalue().strip() else {}
    except ValueError:
        document = {"_unparseable": out.getvalue()}
    return code, document, err.getvalue()


def open_row_for(cfg: Config, ledger: Ledger, instance_id: str, *, hours: float,
                 rate: float = 2.0) -> dict:
    return ledger.open(purpose="first", provider="fake", instance_id=instance_id,
                       gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                       started_at=now_utc() - timedelta(hours=hours))


class ALiveMachineNamedByAnOpenRowIsNotSuperseded(unittest.TestCase):
    def test_up_does_not_close_the_row_or_rent_a_second_machine(self) -> None:
        cfg = cfg_for(5.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=3600, status="RUNNING")          # $2, billing
        ledger = Ledger(cfg.ledger_path)
        open_row_for(cfg, ledger, "pod-A", hours=2)                           # ~$4 accrued
        # state.json is absent: the documented "empty or truncated" shape.

        code, document, err = run_up(cfg, provider)

        self.assertEqual(provider.created, [],
                         f"a second machine was rented while pod-A was live and the ledger named "
                         f"it; up exited {code} with {document}\n{err}")
        self.assertEqual(len(live_pods(provider)), 1,
                         f"two machines are now billing: "
                         f"{[p.get('id') for p in live_pods(provider)]}\n{err}")
        open_run = ledger.open_run()
        self.assertIsNotNone(
            open_run,
            f"pod-A's open row was closed while pod-A was still RUNNING, so its bill stopped "
            f"being recorded and no guard watches it\n{err}")
        self.assertEqual(open_run.get("instance_id"), "pod-A",
                         f"the open row now names {open_run.get('instance_id')!r} while pod-A "
                         f"bills\n{err}")


class ALiveMachineNamedByAClosedRowIsNotExempt(unittest.TestCase):
    def test_up_refuses_while_a_live_machine_has_no_open_row(self) -> None:
        cfg = cfg_for(5.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=7200, status="RUNNING")          # $4 recorded, still up
        ledger = Ledger(cfg.ledger_path)
        open_row_for(cfg, ledger, "pod-A", hours=2)
        ledger.close(note="down", instance_status="RUNNING")                  # the stop was refused
        # state.json is absent; the ledger names pod-A only in a closed row.

        code, document, err = run_up(cfg, provider)

        self.assertEqual(code, 2,
                         f"up did not refuse although a live machine had no open row; exit {code}, "
                         f"{document}\n{err}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)
        self.assertEqual(provider.created, [],
                         f"a second machine was rented while pod-A was live with a closed row\n{err}")
        self.assertEqual(len(live_pods(provider)), 1,
                         f"two machines are now billing: "
                         f"{[p.get('id') for p in live_pods(provider)]}\n{err}")


class AClosedRowOnALiveMachineIsNotRePriced(unittest.TestCase):
    def test_the_guard_does_not_stop_a_live_machine_under_its_true_spend(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=4 * 3600, status="RUNNING")      # 4 h life, $8 billed
        ledger = Ledger(cfg.ledger_path)
        open_row_for(cfg, ledger, "pod-A", hours=4)
        ledger.close(note="down", stopped_at=now_utc() - timedelta(hours=1),
                     instance_status="RUNNING")                               # closed 1 h ago at ~$6
        closed = ledger.total_usd()
        machine_ceiling = round(closed + 2.0 + 0.05, 6)   # + the hour since, at $2/h, with slack

        snap = guard.snapshot(provider.get("pod-A"), cfg, ledger)

        self.assertLessEqual(
            snap.spent_usd, machine_ceiling,
            f"the reading ${snap.spent_usd} exceeds what the machine can have billed "
            f"(${machine_ceiling}): the closed row's ${closed} was priced again from uptime "
            f"{snap.uptime_hours} h")
        self.assertIsNone(
            guard.decide(snap),
            f"the guard would stop pod-A although its real spend (${closed} + $2.00) is under the "
            f"$10 cap: {guard.decide(snap)!r}")


class TwoRowsNamingOneInstanceDoNotDoubleCount(unittest.TestCase):
    def test_the_ledger_never_exceeds_the_machines_own_bill(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=3 * 3600, status="RUNNING")      # 3 h = $6 real
        ledger = Ledger(cfg.ledger_path)
        open_row_for(cfg, ledger, "pod-A", hours=3)
        ledger.close(note="down", instance_status="RUNNING")                  # $6 recorded to now
        cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")

        code, document, err = run_up(cfg, provider)                           # reuses pod-A

        total = round(ledger.total_usd() + ledger.live_usd(), 6)
        self.assertLessEqual(
            total, 6.0 + 0.05,
            f"the ledger reads ${total} for a machine that has billed $6: rows name pod-A twice "
            f"and the second is backdated to its boot\n{err}")
        snap = guard.snapshot(provider.get("pod-A"), cfg, ledger)
        self.assertIsNone(
            guard.decide(snap),
            f"the guard would stop a machine whose bill is $6 against a $10 cap: "
            f"{guard.decide(snap)!r}")


class AnUnreadableAccrualIsNotPrintedAsAWholeZero(unittest.TestCase):
    def _corrupt_ledger(self, cfg: Config, ledger: Ledger) -> None:
        ledger.append({"run_id": "20260101T000000Z-aaaaaa", "purpose": "truncated",
                       "provider": "fake", "instance_id": "pod-A", "gpu_id": "Fake GPU 24GB",
                       "rate_usd_per_hour": 2.0, "started_at": "the-ledger-was-truncated",
                       "stopped_at": None, "wall_clock_seconds": None, "wall_clock_hours": None,
                       "estimated_usd": None, "closed_by": None, "tag": ""})

    def test_the_snapshot_marks_the_accrual_unknown(self) -> None:
        cfg = cfg_for(15.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        self._corrupt_ledger(cfg, ledger)
        pod = {"id": "pod-A", "status": "RUNNING", "cost": 2.0,
               "runtime": {"uptime": None, "ports": None, "gpus": [{"util": None}]}}

        snap = guard.snapshot(pod, cfg, ledger)

        self.assertTrue(
            snap.accrual_unknown,
            f"an unreadable open row was reported as a whole number: "
            f"run_usd={snap.run_usd} spent_usd={snap.spent_usd} priced_from={snap.priced_from}")

    def test_a_witnessed_unreadable_row_is_still_flagged_unknown(self) -> None:
        cfg = cfg_for(15.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        self._corrupt_ledger(cfg, ledger)
        pod = {"id": "pod-A", "status": "RUNNING", "cost": 2.0,
               "runtime": {"uptime": 3600, "ports": {"22": "tcp"}, "gpus": [{"util": 0.0}]}}

        snap = guard.snapshot(pod, cfg, ledger)

        self.assertEqual(snap.priced_from, "uptime",
                         f"the pod's own clock did not price the row: {snap}")
        self.assertTrue(
            snap.accrual_unknown,
            f"the witness was recorded as a reading: {snap}")

    def test_status_does_not_print_the_false_zero_bare(self) -> None:
        cfg = cfg_for(15.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=0, status="RUNNING")
        ledger = Ledger(cfg.ledger_path)
        self._corrupt_ledger(cfg, ledger)
        pod = {"id": "pod-A", "status": "RUNNING", "cost": 2.0,
               "runtime": {"uptime": None, "ports": None, "gpus": [{"util": None}]}}
        args = cli.build_parser().parse_args(["status"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                mock.patch.object(cli, "_pod", lambda p, c: pod), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.cmd_status(args)

        self.assertIn("UNKNOWN", out.getvalue(),
                      f"status printed an unreadable accrual as a number without saying so:\n"
                      f"{out.getvalue()}")


if __name__ == "__main__":
    unittest.main()
