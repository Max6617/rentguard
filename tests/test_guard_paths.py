"""The guard's less-travelled branches: the ones a defect would hide in.

`guard.py` decides when a machine stops, so every branch in it is a money branch. These cover the
paths the money tests do not reach: a zero rate, a provider idle reading that is not a dict, each
drift finding, a heartbeat with no timestamp, a provider that fails mid-watch, an instance that
vanishes, and the sleep between healthy polls.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class GuardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=5.0),
                          state_dir=self.root)
        self.cfg.provider = "fake"
        self.ledger = Ledger(self.cfg.ledger_path)

    def snap(self, **over) -> guard.Snapshot:
        base = dict(at="2026-10-14T12:00:00+00:00", status="RUNNING", uptime_hours=0.5,
                    gpu_util=50.0, mem_util=10.0, rate_usd_per_hour=1.0, open_run_id="r",
                    run_usd=0.5, spent_usd=0.5, cap_usd=10.0, per_run_cap_usd=5.0,
                    max_hours=6.0, idle_minutes=5.0, idle_for_minutes=0.0)
        base.update(over)
        return guard.Snapshot(**base)


class SnapshotPaths(GuardCase):
    def test_minutes_to_cap_is_none_without_a_rate(self):
        self.assertIsNone(self.snap(rate_usd_per_hour=0.0).minutes_to_cap,
                          "a free machine was given a time-to-cap")

    def test_idle_ignores_gpu_entries_that_are_not_dicts(self):
        self.assertIsNone(guard._idle_minutes_since_boot(["not a gpu"], 3600.0),
                          "a malformed GPU entry produced an idle reading")


class DriftFindings(GuardCase):
    def test_an_open_run_with_no_instance_is_a_finding(self):
        findings = guard.state_findings({"instance_id": None},
                                        {"run_id": "r1", "instance_id": None})
        self.assertEqual([f.code for f in findings], ["open-run-no-instance"])

    def test_state_and_row_naming_different_instances_is_a_finding(self):
        findings = guard.state_findings({"instance_id": "B"},
                                        {"run_id": "r1", "instance_id": "A"})
        self.assertIn("state-row-mismatch", [f.code for f in findings])

    def test_without_a_gate_it_warns_rather_than_staying_silent(self):
        findings = guard.state_findings({"instance_id": "A"}, None)
        self.assertIn(("recorded-instance-no-open-run", "warn"),
                      [(f.code, f.severity) for f in findings],
                      "a recorded instance and no way to ask about it passed without a word")

    def test_a_gate_saying_not_live_is_not_drift(self):
        findings = guard.state_findings({"instance_id": "A"}, None,
                                        provider_gate=lambda i: {"status": "EXITED"})
        self.assertEqual(findings, [], "a finished machine was reported as drift")

    def test_a_gate_that_cannot_answer_is_not_proof_of_drift(self):
        findings = guard.state_findings({"instance_id": "A"}, None, provider_gate=lambda i: None)
        self.assertEqual(findings, [], "'could not ask' was treated as 'gone'")


class Heartbeat(GuardCase):
    def test_a_heartbeat_without_a_timestamp_reads_as_unknown(self):
        self.cfg.heartbeat_path.write_text("{}", encoding="utf-8")
        self.assertIsNone(guard.heartbeat_age_minutes(self.cfg.heartbeat_path),
                          "a heartbeat file with no 'at' was reported as a time")


class WatchPaths(GuardCase):
    def test_a_provider_error_ends_the_watch_with_a_named_reason(self):
        class Boom(FakeProvider):
            def get(self, instance_id):
                raise ProviderError("transport down")

        reason = guard.watch(self.cfg, Boom(), self.ledger, instance_id="pod-A",
                             interval=0.0, max_iterations=2, log=lambda *a: None)
        self.assertIn("provider-error", reason, f"unexpected reason: {reason!r}")

    def test_a_vanished_instance_closes_the_row(self):
        self.ledger.open(purpose="x", provider="fake", instance_id="pod-A", gpu_id="g",
                         rate_usd_per_hour=1.0)

        reason = guard.watch(self.cfg, FakeProvider(), self.ledger, instance_id="pod-A",
                             interval=0.0, max_iterations=2, log=lambda *a: None)

        self.assertEqual(reason, "instance gone")
        self.assertIsNone(self.ledger.open_run(),
                          "the row stayed open for a machine that no longer exists")

    def test_a_healthy_machine_is_polled_then_the_watch_ends(self):
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=60, gpu_util=90.0)

        reason = guard.watch(self.cfg, fake, self.ledger, instance_id="pod-A",
                             interval=0.001, max_iterations=2, log=lambda *a: None)

        self.assertEqual(reason, "guard-iteration-limit",
                         f"a healthy machine ended the watch for another reason: {reason!r}")
        self.assertIsNone(self.ledger.open_run(), "a healthy watch touched the ledger")


if __name__ == "__main__":
    unittest.main(verbosity=2)
