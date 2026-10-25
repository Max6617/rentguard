"""`up` must not let a recorded instance silently outvote the config (defect 17).

Measured on the operator's window: with ``cloud = "SECURE"`` and an L40S/4090 preference, `up` resumed the
**community 3080 Ti** it had recorded, then tried a stopped L40S and the provider refused ("not enough
free GPUs on the host machine to start this pod"). The config is the promise. A recorded instance may
be reused or resumed, but never as a silent substitution:

- the refusal names **both** facts — what the config asks and what the instance is;
- reusing a live off-config instance prints a warning **and records it durably** (the failure that
  cost a window was a silent action, and a printed warning is invisible to whoever reads afterwards);
- ``--force`` is visible after the fact: the state says the config was overridden, by what, and when.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

WANTED_GPU = "NVIDIA L40S"
RECORDED_GPU = "NVIDIA GeForce RTX 3080 Ti"


class UpConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"cloud": "SECURE", "gpu_preference": [WANTED_GPU, "NVIDIA RTX 4090"]}

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self) -> dict:
        return json.loads(self.cfg.state_path.read_text(encoding="utf-8"))

    def invoke_up(self, provider, *argv: str) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "config", *argv])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out):
            try:
                code = cli.cmd_up(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def recorded(self, *, status: str = "EXITED", gpu: str = RECORDED_GPU,
                 cloud: str = "COMMUNITY") -> FakeProvider:
        provider = FakeProvider()
        provider.add("pod-A", status=status, uptime_seconds=600, gpu_id=gpu)
        self.write_state({"instance_id": "pod-A", "cloud": cloud, "gpu_id": gpu})
        return provider


class OffConfigInstance(UpConfigCase):
    def test_up_refuses_to_start_a_recorded_instance_the_config_does_not_ask_for(self):
        """The incident: a stopped community 3080 Ti is recorded, the config asks SECURE + L40S."""
        provider = self.recorded()

        code, out = self.invoke_up(provider)

        self.assertEqual(code, 2, f"up exited {code} instead of refusing.\n{out}")
        self.assertEqual(provider.actions, [], f"the off-config instance was started anyway: {out}")
        for fact in ("SECURE", "COMMUNITY", WANTED_GPU, RECORDED_GPU):
            self.assertIn(fact, out, f"the refusal does not name {fact!r}:\n{out}")

    def test_force_starts_it_and_records_that_the_config_was_overridden(self):
        """An override is a decision, and a decision that is not written down is a silent one."""
        provider = self.recorded()

        code, out = self.invoke_up(provider, "--force")

        self.assertEqual(code, 0, f"a forced resume failed.\n{out}")
        self.assertIn(("pod-A", "start"), provider.actions, out)
        recorded = self.state().get("config_override")
        self.assertIsNotNone(recorded, f"the override was not recorded: {self.state()}")
        self.assertTrue(recorded.get("forced"), recorded)
        self.assertEqual(recorded.get("instance_id"), "pod-A", recorded)
        self.assertTrue(any(WANTED_GPU in str(reason) or RECORDED_GPU in str(reason)
                            for reason in recorded.get("mismatches") or []),
                        f"the override does not say what was overridden: {recorded}")

    def test_a_live_off_config_instance_is_reused_but_recorded(self):
        """A live machine is billing, so it is reused rather than restarted — and the mismatch is
        written down, not only printed."""
        provider = self.recorded(status="RUNNING")

        code, out = self.invoke_up(provider)

        self.assertEqual(code, 0, f"up failed on a live instance.\n{out}")
        self.assertIn("warning", out.lower(), f"the mismatch was not warned about:\n{out}")
        recorded = self.state().get("config_override")
        self.assertIsNotNone(recorded, f"the mismatch was not recorded durably: {self.state()}")
        self.assertFalse(recorded.get("forced"), recorded)

    def test_a_matching_instance_is_not_treated_as_an_override(self):
        """Control: the same flow with a config that matches the recorded instance."""
        provider = self.recorded(status="EXITED", gpu=WANTED_GPU, cloud="SECURE")
        self.cfg.runpod = {"cloud": "SECURE", "gpu_preference": [WANTED_GPU]}

        code, out = self.invoke_up(provider)

        self.assertEqual(code, 0, f"a matching instance was refused.\n{out}")
        self.assertIn(("pod-A", "start"), provider.actions, out)
        self.assertIsNone(self.state().get("config_override"),
                          f"a matching instance recorded an override: {self.state()}")


if __name__ == "__main__":
    unittest.main()
