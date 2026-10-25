"""`doctor`'s spend line must be the number the cap is enforced against.

Found in a read-only check on 2026-10-14 23:05: one project root printed
``spend recorded: $0.61 of a $15.00 cap (12 run(s))`` while the open row was a live $0.89/h rental
accruing since 19:43 — the operator's arithmetic said ~$1.42. The ledger's open row carries
``estimated_usd: null``, so the printed total was closed rows only, while `up`'s cap check and the
guard's snapshot both enforce against ``total_usd() + live_usd()``.

That is a *display* gap, not an accounting one — but it is the "two numbers for one thing" class in
the figure a human reads to decide whether to stop, so the line names its parts and their sum, and the
document carries the same three numbers.
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

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class SpendLineIncludesTheOpenRun(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=15.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.ledger = Ledger(self.cfg.ledger_path)
        closed = self.ledger.open(purpose="closed", provider="fake", instance_id="pod-closed",
                                  gpu_id="g", rate_usd_per_hour=1.0,
                                  started_at=now_utc() - timedelta(minutes=30))
        self.ledger.close(note="done", stopped_at=now_utc())
        # One closed row of ~$0.50 (30 minutes at $1/h) and an open one an hour old at $1/h.
        run = self.ledger.open(purpose="live window", provider="fake", instance_id="pod-live",
                               gpu_id="g", rate_usd_per_hour=1.0,
                               started_at=now_utc() - timedelta(hours=1))
        self.open_run_id = run["run_id"]

    def doctor(self, *argv: str) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["doctor", "--json", *argv])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: FakeProvider()), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.cmd_doctor(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_the_document_carries_the_accrual_and_their_sum(self):
        _, document, _ = self.doctor()
        spend = document["spend"]

        self.assertGreater(spend["recorded_usd"], 0.0, spend)
        self.assertGreater(spend["accruing_usd"], 0.0, spend)
        # The accrual grows between two readings, so the comparison is to a tenth of a cent: the
        # claim is that the figures are one arithmetic, not that nothing moved in between.
        self.assertAlmostEqual(spend["total_usd"],
                               spend["recorded_usd"] + spend["accruing_usd"], delta=0.001)
        # And it is the same arithmetic the cap is enforced against.
        self.assertAlmostEqual(spend["total_usd"],
                               self.ledger.total_usd() + self.ledger.live_usd(), delta=0.001)

    def test_the_human_line_names_both_parts_and_the_sum(self):
        _, document, err = self.doctor()
        spend = document["spend"]

        self.assertIn(f"${spend['recorded_usd']:.2f} closed", err, err)
        self.assertIn(f"${spend['accruing_usd']:.2f} accruing", err, err)
        self.assertIn(f"${spend['total_usd']:.2f} of a $15.00 cap", err, err)


if __name__ == "__main__":
    unittest.main()
