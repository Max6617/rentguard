"""F-6: storage is a cost that bills while nothing runs, so it must appear in the report.

The account carried a 150 GB network volume from a finished project — $10.50 a month, billing every
day, named in no report for three days, and larger than every rental in this project's ledger put
together. Instance drift has a detector; this is storage drift's.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class StorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=25.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text("{}", encoding="utf-8")
        self.fake = FakeProvider()

    def run_cli(self, func, *argv: str) -> str:
        args = cli.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out):
            func(args)
        return out.getvalue()


class DoctorNamesStorage(StorageCase):
    def test_an_unattached_volume_is_named_with_its_monthly_cost(self):
        """Sequence: the account holds a 150 GB volume in US-IL-1 attached to no instance.

        Effect: `doctor` names it, says what it costs per month, and says it is attached to nothing.
        """
        self.fake.add_volume("small-agents-vol", size_gb=150.0, region="US-IL-1")

        out = self.run_cli(cli.cmd_doctor, "doctor")

        self.assertIn("small-agents-vol", out, f"the volume is not named:\n{out}")
        self.assertIn("10.50", out, f"the monthly cost is missing (150 GB x $0.07):\n{out}")
        self.assertIn("US-IL-1", out, f"the region is missing:\n{out}")
        self.assertIn("NOTHING", out, f"the volume is not reported as unattached:\n{out}")

    def test_an_attached_volume_is_not_called_unattached(self):
        self.fake.add("pod-A", uptime_seconds=600)
        self.fake.add_volume("in-use-vol", size_gb=20.0, attached_to="pod-A")

        out = self.run_cli(cli.cmd_doctor, "doctor")

        self.assertIn("in-use-vol", out)
        self.assertIn("attached to pod-A", out, f"an attached volume was called unattached:\n{out}")


class LedgerExcludesStorage(StorageCase):
    def test_the_ledger_says_storage_is_not_in_the_total(self):
        """Sequence: `rentguard ledger` before any rental has ever been recorded.

        Effect: the summary says the total covers rentals only, so nobody reads the cap line as the
        whole bill.
        """
        out = self.run_cli(cli.cmd_ledger, "ledger")

        lowered = out.lower()
        self.assertIn("storage", lowered, f"the ledger never mentions storage:\n{out}")
        self.assertIn("not included", lowered, f"the ledger does not say what is excluded:\n{out}")


class RunPodVolumes(unittest.TestCase):
    def test_runpod_volumes_are_canonicalised_with_their_monthly_cost(self):
        """The transport seam, scripted: RunPod's field names in, the canonical shape out."""
        from rentguard.runpod import RunPod

        provider = RunPod("test-key-not-real")
        provider._request = lambda method, path, body=None, params=None, retries=3: {
            "volumes": [{"id": "v1", "name": "small-agents-vol", "size": 150,
                         "dataCenterId": "US-IL-1"}]}

        volumes = provider.volumes()

        self.assertEqual(len(volumes), 1, volumes)
        volume = volumes[0]
        self.assertEqual(volume["id"], "v1")
        self.assertEqual(volume["name"], "small-agents-vol")
        self.assertEqual(volume["size_gb"], 150.0)
        self.assertEqual(volume["region"], "US-IL-1")
        self.assertEqual(volume["monthly_usd"], 10.5, "150 GB at $0.07/GB is $10.50 a month")
        self.assertIsNone(volume["attached_to"], "RunPod does not say what uses a volume")


if __name__ == "__main__":
    unittest.main(verbosity=2)
