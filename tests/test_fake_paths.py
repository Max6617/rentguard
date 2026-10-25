"""The fake's own fidelity surface: the lines that model a provider payload or status.

The carve-out: the fake is where the invented-payload class of bug lived, so every line that
models what a provider *is* — its catalogue row, its status transitions, how a machine reports
itself — is a fidelity risk rather than a coverage gap. These exercise those. Lines that modelled
nothing real were deleted instead: `Artifact.size`/`sha256` (no caller) and a `wait_running`
re-check that could not fire.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class Payloads(unittest.TestCase):
    def test_the_catalogue_row_is_the_shape_the_selection_defaults_read(self):
        fake = FakeProvider(rate_usd_per_hour=1.25, gpu_id="GPU Z")
        row = fake.catalog()[0]
        for key in ("id", "memory", "secure", "community", "price"):
            self.assertIn(key, row, f"the catalogue row is missing {key}: {row}")
        self.assertEqual(row["id"], "GPU Z")
        self.assertEqual(row["price"]["secure"], 1.25)
        self.assertTrue(fake.choose_gpu(["GPU Z"]), "choose_gpu returned nothing for a real GPU")

    def test_the_instance_payload_carries_the_canonical_fields(self):
        fake = FakeProvider()
        fake.add("pod-1", uptime_seconds=600, gpu_util=42.0)
        pod = fake.get("pod-1")
        for key in ("id", "status", "cost", "gpu", "runtime", "ssh"):
            self.assertIn(key, pod, f"the instance payload is missing {key}: {pod}")
        self.assertIn("ports", pod["runtime"])
        self.assertIn("uptime", pod["runtime"])


class StatusSemantics(unittest.TestCase):
    def test_crash_loop_is_running_with_no_uptime_and_no_ports(self):
        fake = FakeProvider()
        fake.add("pod-1", uptime_seconds=600)
        fake.crash_loop("pod-1")
        pod = fake.get("pod-1")
        self.assertEqual(pod["status"], "RUNNING")
        self.assertEqual(pod["runtime"]["uptime"], 0)
        self.assertIsNone(pod["runtime"]["ports"])
        self.assertFalse(fake.is_alive(pod), "a crash loop was reported as usable")

    def test_idle_stops_the_gpu_while_the_clock_keeps_running(self):
        fake = FakeProvider()
        fake.add("pod-1", uptime_seconds=60, gpu_util=90.0)
        before = fake.get("pod-1")["runtime"]["uptime"]
        fake.idle("pod-1", seconds=300)
        pod = fake.get("pod-1")
        self.assertEqual(pod["runtime"]["gpus"][0]["util"], 0.0)
        self.assertGreater(pod["runtime"]["uptime"], before, "the meter stopped with the GPU")

    def test_terminate_removes_the_machine_from_the_account(self):
        fake = FakeProvider()
        fake.add("pod-1", uptime_seconds=60)
        fake.action("pod-1", "terminate")
        self.assertIsNone(fake.get("pod-1"), "a terminated machine was still on the account")
        self.assertEqual(fake.list_pods(), [])

    def test_start_opens_a_port_on_a_machine_that_has_none(self):
        fake = FakeProvider()
        fake.add("pod-1", uptime_seconds=0, ports=None)
        fake.action("pod-1", "start")
        pod = fake.get("pod-1")
        self.assertEqual(pod["status"], "RUNNING")
        self.assertTrue(pod["runtime"]["ports"], "a started machine published no port")
        self.assertTrue(fake.is_alive(pod))


class Refusals(unittest.TestCase):
    def test_an_unknown_action_is_refused(self):
        fake = FakeProvider()
        fake.add("pod-1")
        with self.assertRaises(ProviderError):
            fake.action("pod-1", "reboot")

    def test_an_action_on_a_missing_instance_is_refused(self):
        with self.assertRaises(ProviderError):
            FakeProvider().action("nope", "stop")

    def test_wait_running_rejects_a_missing_instance(self):
        with self.assertRaises(ProviderError):
            FakeProvider().wait_running("nope")

    def test_wait_running_rejects_a_terminal_instance(self):
        fake = FakeProvider()
        fake.add("pod-1", status="TERMINATED")
        with self.assertRaises(ProviderError):
            fake.wait_running("pod-1")

    def test_the_no_stock_path_can_be_asked_for(self):
        fake = FakeProvider()
        fake.refuse_gpus = True
        with self.assertRaises(ProviderError):
            fake.candidates(["anything"])


class CandidateRules(unittest.TestCase):
    def test_a_preference_that_names_nothing_it_accepts_falls_back_to_its_own_gpu(self):
        fake = FakeProvider(gpu_id="Fake GPU 24GB")
        fake.unavailable_gpus = {"X"}
        self.assertEqual(fake.candidates(["X"]), ["Fake GPU 24GB"])

    def test_everything_refused_is_refused_not_papered_over(self):
        fake = FakeProvider(gpu_id="Fake GPU 24GB")
        fake.unavailable_gpus = {"X", "Fake GPU 24GB"}
        with self.assertRaises(ProviderError):
            fake.candidates(["X"])


class Persistence(unittest.TestCase):
    def test_a_corrupt_state_file_is_ignored_rather_than_fatal(self):
        root = scratch_dir(self)
        path = root / "fake-state.json"
        path.write_text("{not json", encoding="utf-8")
        fake = FakeProvider(state_path=path)      # must not raise
        self.assertEqual(fake.list_pods(), [])

    def test_a_saved_account_comes_back_on_construction(self):
        path = scratch_dir(self) / "fake-state.json"
        first = FakeProvider(state_path=path)
        first.add("pod-1", uptime_seconds=600)
        second = FakeProvider(state_path=path)
        self.assertIsNotNone(second.get("pod-1"), "the saved account did not come back")


class ClockScope(unittest.TestCase):
    def test_advance_can_move_one_machine_without_moving_the_others(self):
        fake = FakeProvider()
        fake.add("pod-1", uptime_seconds=0)
        fake.add("pod-2", uptime_seconds=0)
        fake.advance(300, instance_id="pod-1")
        self.assertGreater(fake.get("pod-1")["runtime"]["uptime"], 0)
        self.assertEqual(fake.get("pod-2")["runtime"]["uptime"], 0,
                         "advance moved a machine it was not asked about")


if __name__ == "__main__":
    unittest.main(verbosity=2)
