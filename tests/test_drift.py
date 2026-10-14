"""Drift tests: what the tool says when the account and the ledger disagree.

These run the real CLI against the fake provider with no network and no spend. They exist because
drift is the state in which money hides: an open ledger row whose instance has vanished, or a live
instance nobody recorded.

Written before the fix on purpose, so they were red against the code of the day; a test that passes
before the fix is testing the wrong thing. They are green now, and the assertions are about the
facts a script can act on (the instance id is named, exactly once; an accrued cost is not zero),
never about the wording of a message.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import load as load_config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

T0 = datetime.now(timezone.utc) - timedelta(hours=3)


class DriftTest(unittest.TestCase):
    """A run is open, the instance it names does not exist."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        (self.root / "rentguard.toml").write_text(
            'provider = "fake"\n[budget]\ncap_usd = 25.0\nper_run_cap_usd = 5.0\n',
            encoding="utf-8")
        self.ledger = Ledger(self.root / ".rentguard" / "runs.jsonl")
        self.run = self.ledger.open(
            purpose="open row whose instance has vanished",
            provider="fake", instance_id="vanished-1", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=0.13, started_at=T0)
        (self.root / ".rentguard" / "state.json").write_text(
            json.dumps({"instance_id": "vanished-1"}), encoding="utf-8")
        self._env = dict(os.environ)
        os.environ["RENTGUARD_ROOT"] = str(self.root)
        os.environ["RENTGUARD_PROVIDER"] = "fake"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _run_cli(self, func) -> str:
        # An empty account: the instance the ledger names is gone. The provider is injected rather
        # than built, so the test drives the real command against a provider it controls. The config
        # is read at call time from $RENTGUARD_ROOT, not at import time: `cli._config` is the module
        # default, and the root for this test is whatever setUp put in the environment.
        buf = io.StringIO()
        with mock.patch.object(cli, "_config", load_config), \
                mock.patch.object(cli, "_provider", lambda cfg: FakeProvider()), \
                contextlib.redirect_stdout(buf):
            func(Namespace(json=False))
        return buf.getvalue()

    def test_status_reports_the_open_row_when_the_instance_is_gone(self) -> None:
        out = self._run_cli(cli.cmd_status)
        # The row must be named: silence here is the bug, not the absence of an instance.
        self.assertIn(self.run["run_id"], out,
                      f"status did not mention the open run.\n--- actual ---\n{out}")

    def test_status_reports_accrued_cost_of_an_open_row(self) -> None:
        out = self._run_cli(cli.cmd_status)
        self.assertIn("accrued", out.lower(),
                      f"status did not report an accrued cost.\n--- actual ---\n{out}")
        # Three hours at $0.13/h is ~$0.39. The exact figure matters less than that it is not zero.
        self.assertRegex(out, r"\$0\.3[0-9]",
                         f"accrued cost of a three-hour-old row is missing or zero."
                         f"\n--- actual ---\n{out}")

    def test_doctor_names_the_drift(self) -> None:
        out = self._run_cli(cli.cmd_doctor)
        # Assert on the fact, not the phrasing: doctor must name the instance the ledger believes
        # in and the account does not show. (An earlier version of this test asserted the word
        # "drift" and failed the moment the wording changed, which is a test of the message rather
        # than of the behaviour.)
        self.assertIn("vanished-1", out,
                      f"doctor did not name the missing instance.\n--- actual ---\n{out}")
        # And it must say it once: a repeated line is noise in a list a human is meant to read.
        self.assertEqual(out.count("vanished-1"), 1,
                         f"the drift was reported more than once.\n--- actual ---\n{out}")


if __name__ == "__main__":
    unittest.main()
