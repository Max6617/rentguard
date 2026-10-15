"""Ruling 2026-10-15: no unreadable input may price to zero silently.

A missing or unreadable **rate**, a missing or unreadable **uptime**, and an unreadable
``started_at`` with no witness are one defect: the reading says ``$0.000`` for money it never read,
so the cap cannot fire on it and a person reads headroom that does not exist. The rule is the
corrupt-start rule generalised — the state is an explicit **pricing-unknown** with its reason
named, carried into ``status`` and the guard's log, and a decision that would hang on that price
treats it as unknown rather than as zero.

Red at `cdb2fba`, measured before this file existed:

- ``cost`` absent on a live unrecorded pod  → ``rate=0.0``, ``run_usd=0.0``, ``decide=None`` even
  at a $1.00 cap;
- ``uptime: None`` on a live unrecorded pod → ``run_usd=0.0``, ``decide=None``, and idle is
  disabled as well because the uptime cannot be attributed;
- an unreadable ``started_at`` with no witness → ``run_usd=0.0``, ``spent_usd=0.0``,
  ``accrual_unknown=False`` (never set anywhere), ``status`` printing ``this run: $0.000`` and
  ``~450 min to the cap``.

What is asserted here is the effect: the snapshot carries the unknown state, the printed figure
names it, and the guard keeps watching a healthy machine instead of stopping it over an unreadable
field.
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

RATE = 2.0


def pod(status: str = "RUNNING", *, uptime=None, cost: float | None = RATE,
        ident: str = "pod-A") -> dict:
    """A canonical payload with the field under test left out."""
    payload = {"id": ident, "status": status, "gpu": {"id": "Fake GPU 24GB"},
               "runtime": {"uptime": uptime, "ports": {"22": "tcp"},
                           "gpus": [{"util": 0.0}]}}
    if cost is not None:
        payload["cost"] = cost
    return payload


def cfg_for(cap: float, root: Path) -> Config:
    return Config(budget=Budget(cap_usd=cap, per_run_cap_usd=cap, idle_minutes=0.0),
                  state_dir=root / ".rentguard")


def corrupt_start_row(ledger: Ledger) -> None:
    ledger.append({"run_id": "20260101T000000Z-aaaaaa", "purpose": "truncated",
                   "provider": "fake", "instance_id": "pod-A", "gpu_id": "Fake GPU 24GB",
                   "rate_usd_per_hour": RATE, "started_at": "the-ledger-was-truncated",
                   "stopped_at": None, "wall_clock_seconds": None, "wall_clock_hours": None,
                   "estimated_usd": None, "closed_by": None, "tag": ""})


def status_text(cfg: Config, provider, shown_pod: dict) -> str:
    args = cli.build_parser().parse_args(["status"])
    out = io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", lambda c: provider), \
            mock.patch.object(cli, "_pod", lambda p, c: shown_pod), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        cli.cmd_status(args)
    return out.getvalue()


class PricingUnknownIsInTheSnapshot(unittest.TestCase):
    def assert_names_the_unknown(self, snap, *needles: str) -> None:
        self.assertTrue(getattr(snap, "pricing_unknown", False),
                        f"the reading was not marked pricing-unknown: {snap}")
        text = json.dumps(snap.__dict__, default=str).lower()
        self.assertTrue(any(n in text for n in needles),
                        f"the reason does not name the unreadable field ({needles}): {snap}")

    def test_a_missing_rate_is_pricing_unknown(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)                      # no row: the pod's rate is the price

        snap = guard.snapshot(pod(uptime=3600.0, cost=None), cfg, ledger)

        self.assert_names_the_unknown(snap, "cost", "rate", "price")

    def test_a_missing_uptime_is_pricing_unknown(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)

        snap = guard.snapshot(pod(uptime=None), cfg, ledger)

        self.assert_names_the_unknown(snap, "uptime")

    def test_an_unreadable_start_without_a_witness_is_pricing_unknown(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        corrupt_start_row(ledger)

        snap = guard.snapshot(pod(uptime=None), cfg, ledger)

        self.assert_names_the_unknown(snap, "started_at", "start")

    def test_an_unreadable_start_with_a_witness_is_still_pricing_unknown(self) -> None:
        """The same flag covers the witnessed case: the pod's clock priced it, so the number is a
        witness rather than a reading, and ``priced_from`` says where it came from."""
        cfg = cfg_for(10.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)
        corrupt_start_row(ledger)

        snap = guard.snapshot(pod(uptime=3600.0), cfg, ledger)

        self.assertEqual(snap.priced_from, "uptime",
                         f"the pod's own clock did not price the row: {snap}")
        self.assert_names_the_unknown(snap, "started_at", "start")


class StatusDoesNotPrintAZeroItDidNotRead(unittest.TestCase):
    def assert_status_names_the_unknown(self, text: str, *needles: str) -> None:
        self.assertIn("UNKNOWN", text.upper(),
                      f"status printed a figure for money it never read:\n{text}")
        self.assertTrue(any(n in text.lower() for n in needles),
                        f"status did not name the unreadable field ({needles}):\n{text}")

    def test_a_missing_rate_is_named(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=RATE)
        provider.add("pod-A", uptime_seconds=3600, status="RUNNING")
        ledger = Ledger(cfg.ledger_path)                      # no row
        shown = pod(uptime=3600.0, cost=None)

        self.assert_status_names_the_unknown(status_text(cfg, provider, shown),
                                             "cost", "rate", "price")

    def test_a_missing_uptime_is_named(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=RATE)
        provider.add("pod-A", uptime_seconds=0, status="RUNNING")
        ledger = Ledger(cfg.ledger_path)
        shown = pod(uptime=None)

        self.assert_status_names_the_unknown(status_text(cfg, provider, shown), "uptime")

    def test_an_unreadable_start_without_a_witness_is_named(self) -> None:
        cfg = cfg_for(15.0, scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=RATE)
        provider.add("pod-A", uptime_seconds=0, status="RUNNING")
        ledger = Ledger(cfg.ledger_path)
        corrupt_start_row(ledger)

        self.assert_status_names_the_unknown(status_text(cfg, provider, pod(uptime=None)),
                                             "started_at", "start")


class TheGuardKeepsWatchingAndNamesTheUnknown(unittest.TestCase):
    def test_an_unreadable_price_is_not_a_reason_to_stop_a_healthy_machine(self) -> None:
        cfg = cfg_for(10.0, scratch_dir(self))
        ledger = Ledger(cfg.ledger_path)

        class NoRateFake(FakeProvider):
            def get(self, instance_id):
                live = super().get(instance_id)
                if live is None:
                    return None
                live = dict(live)
                live.pop("cost", None)
                return live

        provider = NoRateFake(rate_usd_per_hour=RATE)
        provider.add("pod-A", uptime_seconds=3600, status="RUNNING")
        lines: list[str] = []

        reason = guard.watch(cfg, provider, ledger, instance_id="pod-A", interval=0,
                             log=lines.append, max_iterations=2)

        self.assertNotIn(("pod-A", "stop"), provider.actions,
                         f"a healthy machine was stopped over an unreadable field: {reason!r}")
        self.assertEqual(reason, "guard-iteration-limit", reason)
        log = "\n".join(lines)
        self.assertIn("UNKNOWN", log.upper(),
                      f"the guard's log kept a $0.000 it never read:\n{log}")
        self.assertIn("cost" if "cost" in log.lower() else "rate", log.lower(),
                      f"the guard's log did not name the unreadable field:\n{log}")
        heartbeat = json.loads(cfg.heartbeat_path.read_text(encoding="utf-8"))
        self.assertTrue(heartbeat.get("pricing_unknown"),
                        f"the heartbeat carried the zero as a reading: {heartbeat}")


if __name__ == "__main__":
    unittest.main()
