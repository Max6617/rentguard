"""A lost state file does not lose the machine: the open ledger row is the record that survives it.

`state.json` is a cache — the code says so at both call sites — and the open row is the record. Before
the fix in this commit, `_pod` read only the state file, so with the file gone a live machine named by
the ledger was invisible to `down`, to `status` and to `up`: `up` closed that machine's row and rented
a second one beside it (Assay's `ALiveMachineNamedByAnOpenRowIsNotSuperseded`), and `down` reported
"no instance recorded" while the machine kept billing.

What is pinned here is the effect on the *other* commands, which is money either way: `down` must stop
the machine the books name, and `status` must not claim nothing is recorded. The refusal side of the
same change — a live machine whose only row is *closed* is not accounted for — is
`tests/test_money_direction_shapes.py::ALiveMachineNamedByAClosedRowIsNotExempt`.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cfg_for(root: Path) -> Config:
    cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                 state_dir=root / ".rentguard")
    cfg.provider = "fake"
    return cfg


def invoke(func, args, cfg: Config, provider: FakeProvider) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", lambda c: provider), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = func(args)
    return code, out.getvalue()


class ALostStateFileDoesNotHideTheMachine(unittest.TestCase):
    """The state file is gone; the ledger's open row names a machine that is still RUNNING."""

    def setUp(self) -> None:
        self.cfg = cfg_for(scratch_dir(self))
        self.provider = FakeProvider(rate_usd_per_hour=2.0)
        self.provider.add("pod-A", uptime_seconds=3600, status="RUNNING")
        self.ledger = Ledger(self.cfg.ledger_path)
        self.run = self.ledger.open(purpose="batch", provider="fake", instance_id="pod-A",
                                    gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                                    started_at=now_utc() - timedelta(hours=1))
        self.assertFalse(self.cfg.state_path.exists(), "fixture: the state file must be absent")

    def test_down_stops_the_machine_the_open_row_names(self) -> None:
        code, out = invoke(cli.cmd_down, cli.build_parser().parse_args(["down"]),
                           self.cfg, self.provider)

        self.assertIn(("pod-A", "stop"), self.provider.actions,
                      f"down left the machine billing because the state file was lost:\n{out}")
        self.assertEqual(code, 0, f"down exited {code}:\n{out}")
        row = self.ledger.entries()[0]
        self.assertIsNotNone(row.get("stopped_at"),
                            f"the row was left open after the machine stopped: {row}")
        self.assertGreater(float(row.get("estimated_usd") or 0.0), 0.0,
                           f"the hour the machine billed was recorded as nothing: {row}")

    def test_status_reports_the_machine_instead_of_nothing_recorded(self) -> None:
        code, out = invoke(cli.cmd_status, cli.build_parser().parse_args(["status"]),
                           self.cfg, self.provider)

        self.assertIn("pod-A", out,
                      f"status did not name the machine the open row records:\n{out}")
        self.assertNotIn("no instance recorded", out.lower(),
                         f"status claimed nothing was recorded while pod-A billed:\n{out}")

    def test_up_reuses_the_machine_instead_of_renting_a_second_one(self) -> None:
        code, out = invoke(cli.cmd_up,
                           cli.build_parser().parse_args(["up", "--purpose", "again", "--json"]),
                           self.cfg, self.provider)

        self.assertEqual(self.provider.created, [],
                         f"up rented a second machine beside a billing one:\n{out}")
        self.assertEqual(self.ledger.open_run().get("run_id"), self.run["run_id"],
                         "up replaced the machine's open row instead of reusing it")


if __name__ == "__main__":
    unittest.main()
