"""Reusing a machine whose earlier row closed: the closed hours are not charged twice.

After a refused stop, the row is closed while the machine is still RUNNING, and the next `up` reuses
that machine. The old `_open_if_needed` backdated the new row to the machine's whole uptime, so the
ledger read **$12.00 for a $6.00 bill** (the verifier, `TwoRowsNamingOneInstanceDoNotDoubleCount`): the
closed row's hours were counted by the closed row and again by the new one.

The rule pinned here: the new row starts where the closed row's paid-for window ended — never at the
machine's boot when a closed row already covers part of it — and where that end cannot be read, `up`
refuses rather than picking a direction. Both are `up`-side halves of the same rule the guard applies
to its remainder (`tests/test_guard_remainder_since_close.py`), through the same `closed_run_end`.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

RATE = 2.0


def cfg_for(root: Path, *, cap: float = 10.0) -> Config:
    cfg = Config(budget=Budget(cap_usd=cap, per_run_cap_usd=cap, idle_minutes=0.0),
                 state_dir=root / ".rentguard")
    cfg.provider = "fake"
    cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
    return cfg


def run_up(cfg: Config, provider: FakeProvider) -> tuple[int, dict, str]:
    args = cli.build_parser().parse_args(["up", "--purpose", "again", "--json"])
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", lambda c: provider), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.cmd_up(args)
    text = out.getvalue()
    try:
        document = json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError:
        document = {"_unparseable": text}
    return code, document, text + err.getvalue()


class ReuseStartsWhereTheClosedRowEnded(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = cfg_for(scratch_dir(self))
        self.provider = FakeProvider(rate_usd_per_hour=RATE)
        self.ledger = Ledger(self.cfg.ledger_path)
        self.write_state({"instance_id": "pod-A", "provider": "fake"})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def close_a_row(self, *, opened_hours_ago: float, closed_hours_ago: float) -> dict:
        now = now_utc()
        self.ledger.open(purpose="first", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=RATE,
                         started_at=now - timedelta(hours=opened_hours_ago))
        return self.ledger.close(note="down", stopped_at=now - timedelta(hours=closed_hours_ago),
                                 instance_status="RUNNING")

    def test_the_ledger_never_exceeds_the_machines_own_bill(self) -> None:
        self.provider.add("pod-A", uptime_seconds=3 * 3600, status="RUNNING")   # 3 h = $6 real
        self.close_a_row(opened_hours_ago=3.0, closed_hours_ago=0.0)            # the refused stop

        code, document, text = run_up(self.cfg, self.provider)

        total = round(self.ledger.total_usd() + self.ledger.live_usd(), 6)
        self.assertLessEqual(total, 6.05,
                             f"the ledger reads ${total:.2f} for a machine that has billed $6.00: "
                             f"the closed row's hours were charged again\n{text}")
        self.assertEqual(self.provider.created, [], f"a second machine was rented\n{text}")
        self.assertEqual(code, 0, f"reusing the machine exited {code}\n{text}")
        snap = guard.snapshot(self.provider.get("pod-A"), self.cfg, self.ledger)
        self.assertIsNone(guard.decide(snap),
                          f"the guard would stop a machine whose bill is $6.00: {guard.decide(snap)!r}")

    def test_the_new_row_starts_where_the_closed_row_ended(self) -> None:
        self.provider.add("pod-A", uptime_seconds=3 * 3600, status="RUNNING")
        closed = self.close_a_row(opened_hours_ago=3.0, closed_hours_ago=1.0)   # ended an hour ago

        code, document, text = run_up(self.cfg, self.provider)

        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"the reused machine has no open row\n{text}")
        started = datetime.fromisoformat(run["started_at"])
        ended = datetime.fromisoformat(closed["stopped_at"])
        self.assertLess(abs((started - ended).total_seconds()), 2.0,
                        f"the new row starts at {run['started_at']} instead of where the closed row "
                        f"ended ({closed['stopped_at']}): the earlier hours are charged twice")
        self.assertAlmostEqual(self.ledger.live_usd(), RATE, delta=0.1,
                               msg=f"the hour since the row closed is not what the new row accrues: "
                                   f"${self.ledger.live_usd():.3f}")


class AnUnreadableEndIsRefusedNotGuessed(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = cfg_for(scratch_dir(self))
        self.provider = FakeProvider(rate_usd_per_hour=RATE)
        self.provider.add("pod-A", uptime_seconds=3 * 3600, status="RUNNING")
        self.ledger = Ledger(self.cfg.ledger_path)
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        now = now_utc()
        self.ledger.open(purpose="first", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=RATE,
                         started_at=now - timedelta(hours=3.0))
        self.closed = self.ledger.close(note="down", stopped_at=now - timedelta(hours=1.0),
                                        instance_status="RUNNING")
        rows = self.ledger.entries()
        rows[-1].update({"stopped_at": "the-stop-was-truncated",
                         "started_at": "the-start-was-truncated",
                         "wall_clock_seconds": None})
        self.ledger._rewrite(rows)

    def test_up_refuses_rather_than_placing_the_start_by_guess(self) -> None:
        code, document, text = run_up(self.cfg, self.provider)

        self.assertEqual(code, 2, f"up exited {code} on a start it cannot place\n{text}")
        self.assertEqual(document.get("reason"), "closed-row-unreadable-stop", document)
        self.assertEqual(self.provider.created, [], f"a machine was rented anyway\n{text}")
        self.assertIsNone(self.ledger.open_run(),
                          f"a row was opened at a guessed start: {self.ledger.open_run()}")

    def test_the_helper_itself_starts_now_rather_than_at_the_boot(self) -> None:
        """The defensive path: a caller that did not refuse first. Starting at the machine's boot
        would charge the closed row's hours a second time, so the row starts now and says so."""
        pod = self.provider.get("pod-A")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run = cli._open_if_needed(self.cfg, self.ledger, pod, "forced", self.provider)

        self.assertIsNotNone(run, f"no row was opened at all:\n{out.getvalue()}")
        started = datetime.fromisoformat(run["started_at"])
        self.assertLess(abs((now_utc() - started).total_seconds()), 5.0,
                        f"the row was backdated to the machine's boot although a closed row already "
                        f"covers part of it: {run['started_at']}")
        self.assertIn("no readable end", out.getvalue(),
                      f"the guessed start was not named:\n{out.getvalue()}")


if __name__ == "__main__":
    unittest.main()
