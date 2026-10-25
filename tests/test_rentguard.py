"""Unit tests that need no network and no money.

    python -m unittest discover -s rentguard/tests -t .

The point of these is the arithmetic in the ledger and the decision in the guard: if either is
wrong, the tool spends money it should not, and no integration test will tell you that.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard.config import Budget, Config, ConfigError  # noqa: E402
from rentguard.guard import Snapshot, decide, snapshot  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = scratch_dir(self)
        self.ledger = Ledger(self.tmp / "runs.jsonl")

    def test_open_close_records_cost_from_wall_clock_and_rate(self) -> None:
        run = self.ledger.open(purpose="baseline", provider="runpod", instance_id="abc",
                               gpu_id="RTX 4090", rate_usd_per_hour=0.89, started_at=T0)
        self.assertIsNone(run["stopped_at"])
        self.assertEqual(self.ledger.total_usd(), 0.0)

        closed = self.ledger.close(note="down", stopped_at=T0 + timedelta(hours=2))
        self.assertEqual(closed["wall_clock_hours"], 2.0)
        self.assertAlmostEqual(closed["estimated_usd"], 1.78, places=6)
        self.assertEqual(closed["closed_by"], "down")
        self.assertIsNone(self.ledger.open_run())

    def test_closing_twice_does_not_double_count(self) -> None:
        self.ledger.open(purpose="x", provider="runpod", instance_id="abc",
                         gpu_id="g", rate_usd_per_hour=1.0, started_at=T0)
        self.ledger.close(note="down", stopped_at=T0 + timedelta(hours=1))
        self.assertIsNone(self.ledger.close(note="down-again"))
        self.assertAlmostEqual(self.ledger.total_usd(), 1.0, places=6)

    def test_live_cost_of_an_open_run(self) -> None:
        self.ledger.open(purpose="x", provider="runpod", instance_id="abc",
                         gpu_id="g", rate_usd_per_hour=2.0, started_at=T0)
        self.assertAlmostEqual(self.ledger.live_usd(at=T0 + timedelta(minutes=30)), 1.0, places=6)
        self.assertAlmostEqual(self.ledger.total_usd(), 0.0, places=6)

    def test_two_open_runs_are_refused(self) -> None:
        self.ledger.open(purpose="a", provider="runpod", instance_id="1",
                         gpu_id="g", rate_usd_per_hour=1.0, started_at=T0)
        with self.assertRaises(RuntimeError):
            self.ledger.open(purpose="b", provider="runpod", instance_id="2",
                             gpu_id="g", rate_usd_per_hour=1.0, started_at=T0)

    def test_a_corrupt_line_does_not_lose_the_ledger(self) -> None:
        self.ledger.open(purpose="a", provider="runpod", instance_id="1",
                         gpu_id="g", rate_usd_per_hour=1.0, started_at=T0)
        self.ledger.close(note="down", stopped_at=T0 + timedelta(hours=1))
        with self.ledger.path.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual(len(self.ledger.entries()), 1)
        self.assertAlmostEqual(self.ledger.total_usd(), 1.0, places=6)

    def test_file_is_valid_jsonl_after_a_close(self) -> None:
        self.ledger.open(purpose="a", provider="runpod", instance_id="1",
                         gpu_id="g", rate_usd_per_hour=1.0, started_at=T0)
        self.ledger.close(note="down", stopped_at=T0 + timedelta(hours=3))
        for line in self.ledger.path.read_text(encoding="utf-8").splitlines():
            json.loads(line)


class BudgetTest(unittest.TestCase):
    def test_per_run_cap_above_total_cap_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            Budget(cap_usd=5.0, per_run_cap_usd=50.0)

    def test_zero_cap_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            Budget(cap_usd=0.0)


class GuardDecisionTest(unittest.TestCase):
    def snap(self, **over) -> Snapshot:
        base = dict(at="2026-10-14T12:00:00+00:00", status="RUNNING", uptime_hours=0.5,
                    gpu_util=80.0, mem_util=50.0, rate_usd_per_hour=1.0, open_run_id="r1",
                    run_usd=0.5, spent_usd=0.5, cap_usd=10.0, per_run_cap_usd=5.0,
                    max_hours=6.0, idle_minutes=10.0, idle_for_minutes=0.0)
        base.update(over)
        return Snapshot(**base)

    def test_healthy_run_keeps_going(self) -> None:
        self.assertIsNone(decide(self.snap()))

    def test_total_cap_stops(self) -> None:
        reason = decide(self.snap(spent_usd=10.0))
        self.assertIn("total cap", reason or "")

    def test_per_run_cap_stops_even_under_the_total_cap(self) -> None:
        reason = decide(self.snap(run_usd=5.0, spent_usd=5.0))
        self.assertIn("per-run cap", reason or "")

    def test_max_hours_stops(self) -> None:
        reason = decide(self.snap(uptime_hours=6.0))
        self.assertIn("max hours", reason or "")

    def test_idle_stops(self) -> None:
        reason = decide(self.snap(gpu_util=0.0, idle_for_minutes=11.0))
        self.assertIn("idle", reason or "")

    def test_idle_does_not_stop_before_the_window(self) -> None:
        self.assertIsNone(decide(self.snap(gpu_util=0.0, idle_for_minutes=9.0)))

    def test_already_stopped_instance_is_not_stopped_again(self) -> None:
        self.assertEqual(decide(self.snap(status="EXITED")), "already stopped")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_reads_only_what_the_provider_reports(self) -> None:
        ledger = Ledger(scratch_dir(self) / "runs.jsonl")
        ledger.open(purpose="x", provider="runpod", instance_id="i", gpu_id="g",
                    rate_usd_per_hour=0.89, started_at=T0)
        cfg = Config(budget=Budget(cap_usd=25.0, per_run_cap_usd=5.0))
        pod = {"id": "i", "status": "RUNNING", "cost": 0.89,
               "runtime": {"uptime": 3600, "ports": {"22": "tcp"},
                           "gpus": [{"util": 55, "memoryUtil": 40}]}}
        snap = snapshot(pod, cfg, ledger, at=T0 + timedelta(hours=1))
        self.assertEqual(snap.uptime_hours, 1.0)
        self.assertEqual(snap.gpu_util, 55.0)
        self.assertAlmostEqual(snap.run_usd, 0.89, places=6)
        self.assertAlmostEqual(snap.remaining_usd, 24.11, places=6)


if __name__ == "__main__":
    unittest.main()
