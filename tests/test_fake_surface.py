"""The fake provider's own surface: payload shapes, status semantics, and the ways it refuses.

The fake is the instrument every money test rests on, so the model it presents has to be exercised
too — a fake that quietly returns a convenient shape tests the tool against a world that does not
exist. Every test here asserts a *contract* fact about that model (alive means uptime and a port; a
terminated instance is gone; an unknown verb is refused), never merely that a line ran.

Two things are deliberately left uncovered and named here rather than papered over: the module-level
`Provider = FakeProvider` alias, which the tracer reports as line 0 and which has no behaviour to
assert, and any branch that would need a real network to reach.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard.fake import ArtifactStore, FakeProvider  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

DATA = b'{"accuracy": 0.9}'


class ArtefactStoreTest(unittest.TestCase):
    def test_the_bytes_that_were_put_come_back_and_can_be_hashed(self):
        """Effect: the artefact the fake hands back can be checked byte for byte, which is what lets
        an artefact test assert the file arrived rather than that something did."""
        store = ArtifactStore()
        artefact = store.put("pod-A", "results/out.json", DATA)

        self.assertEqual(artefact.data, DATA)
        self.assertEqual(hashlib.sha256(artefact.data).hexdigest(), hashlib.sha256(DATA).hexdigest())
        self.assertEqual(artefact.path, "results/out.json")
        self.assertEqual(store.match("pod-A", ["results/*.json"]), {"results/out.json": DATA})
        self.assertEqual(store.match("pod-A", ["logs/*.log"]), {})


class MachineStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeProvider()

    def test_a_machine_is_alive_only_with_uptime_and_a_port(self):
        """The crash-loop rule, asserted on the fake's own payload: RUNNING is not enough."""
        self.fake.add("pod-A", uptime_seconds=0, ports=None)
        self.assertFalse(self.fake.is_alive(self.fake.get("pod-A") or {}))

        self.fake.action("pod-A", "start")
        self.assertTrue(self.fake.is_alive(self.fake.get("pod-A") or {}),
                        "a started machine with uptime and a port should be usable")

    def test_crash_loop_leaves_a_running_machine_unusable(self):
        """Effect: the payload models the shape that wasted an hour — RUNNING, uptime 0, ports null
        — and `is_alive` refuses it."""
        self.fake.add("pod-A", uptime_seconds=3600)
        self.fake.crash_loop("pod-A")
        pod = self.fake.get("pod-A") or {}

        self.assertEqual(pod["status"], "RUNNING")
        self.assertEqual(pod["runtime"]["uptime"], 0)
        self.assertIsNone(pod["runtime"]["ports"])
        self.assertFalse(self.fake.is_alive(pod), "a crash loop was reported as usable")

    def test_idle_zeroes_utilisation_and_moves_the_clock(self):
        """Effect: the idle window can be exercised without a network, with the meter still
        running."""
        instance = self.fake.add("pod-A", uptime_seconds=60)
        self.fake.busy("pod-A", util=95)

        self.fake.idle("pod-A", seconds=900)

        pod = self.fake.get("pod-A") or {}
        self.assertEqual(pod["runtime"]["gpus"][0]["util"], 0.0)
        self.assertEqual(pod["runtime"]["gpus"][0]["memoryUtil"], 0.0)
        self.assertGreaterEqual(instance.uptime_seconds, 960,
                                "idle() must let the meter keep running")

    def test_disappear_removes_a_machine_from_the_account(self):
        """Effect: the "someone else's console click" case is modelled, not just a None return."""
        self.fake.add("pod-A")
        self.fake.disappear("pod-A")

        self.assertIsNone(self.fake.get("pod-A"))
        self.assertEqual(self.fake.list_pods(), [])

    def test_start_restores_a_port_and_terminate_removes_the_machine(self):
        """Effect: the two state transitions the CLI acts on are modelled faithfully."""
        self.fake.add("pod-A", status="EXITED", uptime_seconds=10, ports=None)
        self.fake.action("pod-A", "start")
        started = self.fake.get("pod-A") or {}
        self.assertEqual(started["status"], "RUNNING")
        self.assertIsNotNone(started["runtime"]["ports"], "start did not restore a port")

        self.fake.action("pod-A", "terminate")
        self.assertEqual(self.fake.instances["pod-A"].status, "TERMINATED")
        self.assertIsNone(self.fake.get("pod-A"), "a terminated machine is still returned")


class RefusalsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeProvider()

    def test_an_unknown_action_is_refused_by_the_fake_itself(self):
        """Effect: a typo in the CLI cannot be silently accepted by the instrument."""
        self.fake.add("pod-A")
        with self.assertRaises(ProviderError) as caught:
            self.fake.action("pod-A", "delete")
        self.assertIn("unknown action", str(caught.exception))

    def test_an_action_on_a_machine_that_does_not_exist_is_refused(self):
        with self.assertRaises(ProviderError) as caught:
            self.fake.action("no-such-pod", "stop")
        self.assertIn("no instance", str(caught.exception))

    def test_a_failing_action_can_be_asked_for(self):
        """Effect: a provider 500 at stop time is reproducible, which is what the `down` tests need.
        """
        self.fake.add("pod-A")
        self.fake.fail_actions = {"stop"}
        with self.assertRaises(ProviderError) as caught:
            self.fake.action("pod-A", "stop")
        self.assertIn("refused by the fake", str(caught.exception))

    def test_wait_running_refuses_a_machine_that_does_not_exist(self):
        with self.assertRaises(ProviderError) as caught:
            self.fake.wait_running("no-such-pod")
        self.assertIn("does not exist", str(caught.exception))

    def test_wait_running_refuses_a_machine_in_a_terminal_state(self):
        self.fake.add("pod-A", status="TERMINATED")
        with self.assertRaises(ProviderError) as caught:
            self.fake.wait_running("pod-A")
        self.assertIn("TERMINATED", str(caught.exception))

    def test_wait_running_provisions_an_unusable_machine_exactly_once(self):
        """Sequence: a machine that reports RUNNING with uptime 0 and no port (the crash-loop
        payload). Effect: `wait_running` models *provisioning* — one simulated second, then a
        published port — because otherwise every freshly created pod would be permanently unusable
        and the documented offline `up` flow could not run at all.

        The crash-loop case is modelled by `crash_loop()`, which `status` and `run` then refuse; a
        test that needs a machine which never becomes usable drives the real provider's transport
        instead of the fake, because this fake cannot express it.
        """
        self.fake.add("pod-A", status="RUNNING", uptime_seconds=0, ports=None)

        pod = self.fake.wait_running("pod-A")

        self.assertEqual(pod["status"], "RUNNING")
        self.assertGreater(pod["runtime"]["uptime"], 0, "provisioning left the uptime at zero")
        self.assertIsNotNone(pod["runtime"]["ports"], "provisioning left the port unpublished")
        self.assertTrue(self.fake.is_alive(pod), "wait_running returned an unusable machine")


class CatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeProvider(rate_usd_per_hour=1.25)

    def test_the_catalog_describes_the_gpu_with_a_price(self):
        rows = self.fake.catalog()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "Fake GPU 24GB")
        self.assertEqual(rows[0]["memory"], 24)
        self.assertEqual(rows[0]["price"]["secure"], 1.25)

    def test_a_preference_that_names_the_fake_gpu_is_offered(self):
        self.assertEqual(self.fake.candidates(["Fake GPU 24GB"]), ["Fake GPU 24GB"])
        self.assertEqual(self.fake.choose_gpu(["Fake GPU 24GB"]), "Fake GPU 24GB")

    def test_a_preference_the_fake_does_not_own_is_offered_as_asked(self):
        """Effect: `up` can be driven in tests with the real default preference list, because the
        fake offers the names it was given instead of refusing everything it does not have."""
        self.assertEqual(self.fake.candidates(["NVIDIA H100", "NVIDIA A100"]),
                         ["NVIDIA H100", "NVIDIA A100"])
        self.assertEqual(self.fake.choose_gpu(["NVIDIA H100", "NVIDIA A100"]), "NVIDIA H100")

    def test_an_empty_preference_falls_back_to_the_fake_gpu(self):
        """Effect: a config that names no GPU still rents something offline, which is what makes
        the README's `RENTGUARD_PROVIDER=fake rentguard up` work on a fresh project."""
        self.assertEqual(self.fake.candidates([]), ["Fake GPU 24GB"])

    def test_a_catalog_with_no_stock_is_a_refusal_not_an_empty_list(self):
        """Effect: the "nothing available" path is reproducible, so `up`'s refusal is testable."""
        self.fake.refuse_gpus = True
        with self.assertRaises(ProviderError) as caught:
            self.fake.candidates(["Fake GPU 24GB"])
        self.assertIn("Fake GPU 24GB", str(caught.exception))

    def test_every_candidate_refused_leaves_nothing_to_try(self):
        """Effect: when the fake's own GPU is the unavailable one and the config names nothing
        else, the refusal names the GPU it is refusing instead of offering a substitute."""
        self.fake.unavailable_gpus = {"Fake GPU 24GB"}
        with self.assertRaises(ProviderError) as caught:
            self.fake.candidates([])
        self.assertIn("refuses", str(caught.exception))


class PersistedStateTest(unittest.TestCase):
    def test_a_corrupt_state_file_is_ignored_rather_than_raising(self):
        """Sequence: `fake-state.json` is half-written (an interrupted process). Effect: the fake
        starts with an empty account instead of crashing the whole suite — and no money path
        depends on a file written by a previous run."""
        root = scratch_dir(self)
        state_path = root / "fake-state.json"
        state_path.write_text("{not json at all", encoding="utf-8")

        fake = FakeProvider(state_path=state_path)

        self.assertEqual(fake.list_pods(), [], "a corrupt state file produced machines")
        self.assertTrue(state_path.exists(), "the corrupt file was silently rewritten")

    def test_machines_survive_a_second_provider_on_the_same_state_file(self):
        """Effect: the "fresh shell" case the README documents is modelled: one account file, one
        machine, seen by the next process."""
        root = scratch_dir(self)
        state_path = root / "fake-state.json"
        first = FakeProvider(state_path=state_path)
        first.add("pod-A", uptime_seconds=600)
        first.advance(60)
        del first

        second = FakeProvider(state_path=state_path)

        self.assertIsNotNone(second.get("pod-A"), "the machine did not survive the process")


if __name__ == "__main__":
    unittest.main(verbosity=2)
