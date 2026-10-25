"""The drift a missing instance leaves behind: an open ledger row the provider no longer shows.

    python rentguard/tests/test_drift_missing_instance.py -v

Sequence: record an open run against an instance, age it with the fake's own clock (``advance``),
make the instance vanish (``disappear``), then ask ``status`` and ``doctor``. The test asserts what
is required: the row and its accrued cost are surfaced, the drift is named, and the instance id is
still recoverable afterwards.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import unittest
import uuid
from argparse import Namespace
from datetime import timedelta
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402

TMPROOT = WS / ".rentguard-tmp"
RUN_HOURS = 3.0
RATE = 2.0


class DriftOnAMissingInstance(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = TMPROOT / uuid.uuid4().hex
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.state_dir, ignore_errors=True))

        self.cfg = Config(budget=Budget(cap_usd=25.0, per_run_cap_usd=5.0),
                          state_dir=self.state_dir)
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.ledger = Ledger(self.cfg.ledger_path)

        self.fake = FakeProvider(rate_usd_per_hour=RATE, gpu_id="Fake GPU 24GB")
        self.fake.add("drift-demo", rate_usd_per_hour=RATE)
        self.fake.busy("drift-demo")
        self.run = self.ledger.open(
            purpose="f1-drift", provider="fake", instance_id="drift-demo",
            gpu_id="Fake GPU 24GB", rate_usd_per_hour=RATE,
            started_at=now_utc() - timedelta(hours=RUN_HOURS))
        self.cfg.state_path.write_text(
            json.dumps({"instance_id": "drift-demo", "provider": "fake"}, indent=2) + "\n",
            encoding="utf-8")

        # Age it through the provider's own clock, then make it vanish.
        self.fake.advance(int(RUN_HOURS * 3600))
        self.fake.disappear("drift-demo")
        self.assertIsNone(self.fake.get("drift-demo"), "fixture: the instance should be gone")

    # ---------------------------------------------------------------- helpers

    def invoke(self, func, args) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
             mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
             mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
             contextlib.redirect_stdout(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except cli.ProviderError:
                code = 1
        return code, out.getvalue()

    def recorded_id(self) -> str | None:
        return json.loads(self.cfg.state_path.read_text(encoding="utf-8")).get("instance_id")

    # ---------------------------------------------------------------- the required behaviour

    def test_status_surfaces_the_open_row_and_a_non_zero_accrued_cost(self):
        """`status` is what an agent calls to decide whether anything is billing. With the instance
        gone and the row open, it must show the row and the accrued dollars, not silence."""
        code, out = self.invoke(cli.cmd_status, Namespace(json=False))

        self.assertIn(str(self.run["run_id"]), out,
                      f"status does not surface the open row {self.run['run_id']}.\noutput:\n{out}")
        self.assertIn("drift-demo", out, f"status does not name the instance.\noutput:\n{out}")
        accrued = self.ledger.live_usd()
        self.assertGreater(accrued, 0.0, "fixture: no accrued cost")
        self.assertIn(f"{accrued:.1f}", out,
                      f"status does not report the ${accrued:.2f} accrued on the open row.\n"
                      f"output:\n{out}")
        self.assertNotIn("no instance recorded", out.lower(),
                         f"status still claims nothing is recorded.\noutput:\n{out}")

    def test_status_json_carries_the_drift_for_a_machine_consumer(self):
        """The MCP server and scripts read the structured form, not the prose."""
        code, out = self.invoke(cli.cmd_status, Namespace(json=True))

        payload = json.loads(out[out.index("{"):])
        self.assertTrue(payload.get("drift"), f"json drift flag is not set: {payload}")
        self.assertEqual(payload.get("recorded_instance_id"), "drift-demo",
                         f"json lost the recorded id: {payload}")
        accrued = (payload.get("spent") or {}).get("accrued_usd") or 0.0
        self.assertGreater(accrued, 0.0, f"json reports no accrued cost: {payload}")

    def test_doctor_names_the_drift_and_fails(self):
        """doctor must flag the drift direction 'open row, no instance' as an error, so a caller
        that only reads the exit code does not see health."""
        code, out = self.invoke(cli.cmd_doctor, Namespace(json=False))

        self.assertNotEqual(code, 0, f"doctor exited 0 with drift present.\noutput:\n{out}")
        lowered = out.lower()
        self.assertTrue("open-run-instance-missing" in lowered or "does not show" in lowered,
                        f"doctor does not name the drift.\noutput:\n{out}")

    def test_the_instance_id_is_still_recoverable_afterwards(self):
        """The recorded id is the only link back to the row; no read command may destroy it."""
        self.invoke(cli.cmd_status, Namespace(json=False))
        self.invoke(cli.cmd_doctor, Namespace(json=False))

        self.assertEqual(self.recorded_id(), "drift-demo",
                         "a read command erased the link from the state file to the open row")


if __name__ == "__main__":
    unittest.main(verbosity=2)
