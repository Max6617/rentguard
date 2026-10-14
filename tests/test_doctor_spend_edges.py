"""Attacks on the `doctor` spend line (`a10aa66`): the printed total against the enforced one.

The line now reads `$X closed + $Y accruing = $Z of a cap`, and the claim is that `$Z` is the number
the cap is enforced against. The only disagreement that matters is between those two numbers, so
every case here computes both: the printed `$Z` from the command's own document, and the enforced
value as `ledger.total_usd() + ledger.live_usd()` — which is what `up`'s pre-rent check and the
guard's snapshot use.

The edges are the ones the fix's own test does not build: a voided row, a row with an unparseable or
future start, a root with no open row, two roots reading one account, and an open row whose machine
the provider already stopped.
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

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=15.0, per_run_cap_usd=10.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.ledger = Ledger(self.cfg.ledger_path)
        self.fake = FakeProvider()

    # ---------------------------------------------------------------- helpers

    def closed_run(self, *, hours: float, rate: float = 2.0, purpose: str = "closed",
                   void: bool = False) -> dict:
        run = self.ledger.open(purpose=purpose, provider="fake", instance_id="pod-old",
                               gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                               started_at=now_utc() - timedelta(hours=hours + 1))
        return self.ledger.close(note="void" if void else "down",
                                 stopped_at=now_utc() - timedelta(hours=1), void=void)

    def open_run(self, *, hours_ago: float, rate: float = 2.0,
                 instance_id: str = "pod-A") -> dict:
        return self.ledger.open(purpose="open", provider="fake", instance_id=instance_id,
                                gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                                started_at=now_utc() - timedelta(hours=hours_ago))

    def doctor(self) -> tuple[int, str, dict]:
        """Run the real command; return (exit code, all text a human sees, the document)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.cmd_doctor(cli_args("doctor", "--json"))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        stdout = out.getvalue().strip()
        document = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
        return code, out.getvalue() + err.getvalue(), document

    def enforced(self) -> float:
        """The number `up` refuses on and the guard prices against."""
        return round(self.ledger.total_usd() + self.ledger.live_usd(), 6)

    def printed(self, document: dict) -> float:
        spend = document.get("spend") or {}
        return round(float(spend.get("total_usd") or 0.0), 6)

    def assert_readings_agree(self, document: dict, text: str) -> None:
        enforced = self.enforced()
        printed = self.printed(document)
        self.assertAlmostEqual(
            printed, enforced, places=4,
            msg=f"the printed total ${printed} and the enforced total ${enforced} disagree."
                f"\nspend={document.get('spend')}\noutput:\n{text}")


class VoidedAndFutureRows(DoctorCase):
    def test_a_voided_row_contributes_to_neither_number(self):
        """Sequence: a closed $2.00 row, a voided row (its instance vanished, so its cost is not
        invented), and an open run one hour old at $2/h.

        Effect required: both numbers are $4.00 — the voided row is nothing in the reading and
        nothing in the enforcement.
        """
        self.closed_run(hours=1.0)
        self.closed_run(hours=1.0, purpose="voided", void=True)
        self.open_run(hours_ago=1.0)

        code, text, document = self.doctor()

        self.assert_readings_agree(document, text)
        self.assertAlmostEqual(self.printed(document), 4.0, places=2,
                               msg=f"the voided row was counted: {document.get('spend')}")

    def test_a_future_start_contributes_nothing_to_either_number(self):
        """Sequence: an open row whose `started_at` is an hour in the future (a clock that jumped
        backwards between processes). Effect: no negative accrual, and the two numbers agree."""
        self.closed_run(hours=1.0)
        self.ledger.open(purpose="future", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc() + timedelta(hours=1))

        code, text, document = self.doctor()

        self.assert_readings_agree(document, text)
        self.assertGreaterEqual(self.printed(document), 0.0, text)
        self.assertAlmostEqual(self.printed(document), 2.0, places=2, msg=text)


class NoOpenRowAndTwoRoots(DoctorCase):
    def test_no_open_row_means_both_numbers_are_the_closed_total(self):
        """The workspace root's shape: rows, none of them open. Effect: the two numbers are the
        closed total, with a zero accrual named as such."""
        self.closed_run(hours=1.0)
        self.closed_run(hours=0.5)

        code, text, document = self.doctor()

        self.assert_readings_agree(document, text)
        self.assertEqual((document.get("spend") or {}).get("accruing_usd"), 0.0, text)
        self.assertAlmostEqual(self.printed(document), 3.0, places=2, msg=text)

    def test_another_roots_live_machine_does_not_inflate_this_roots_total(self):
        """Sequence: this root has a closed $2.00 row and no machine; the account also holds a live
        pod that belongs to a *different* root (another state dir), with no row here.

        Effect required: the printed total is this root's — $2.00 — and equals the enforced number
        for this root. The other root's machine is a *finding* (an untracked instance), not money
        added to a cap that cannot see it. A line that folded account-wide accrual into this root's
        total would refuse rentals this root can afford.
        """
        self.closed_run(hours=1.0)
        other = FakeProvider()
        other.add("pod-other-root", uptime_seconds=7200, rate_usd_per_hour=2.0)
        self.fake.instances.update(other.instances)

        code, text, document = self.doctor()

        self.assert_readings_agree(document, text)
        self.assertAlmostEqual(self.printed(document), 2.0, places=2,
                               msg=f"another root's machine was priced into this root's cap: "
                                   f"{document.get('spend')}")
        self.assertIn("pod-other-root", text,
                      f"the other root's live machine was not reported as untracked.\n{text}")


class TheStoppedMachineStopsAccruing(DoctorCase):
    def test_an_open_row_whose_machine_already_stopped_does_not_accrue_to_now(self):
        """Sequence: an open row started three hours ago at $2/h ($6 from its start); the provider
        reports the machine EXITED with two hours of uptime, i.e. it stopped billing an hour ago.

        Effect required: the accrual may not exceed what the provider supports — $4.00 — so the
        printed total stops growing when the machine did. Otherwise the reading reports a spend it
        did not read, and it grows forever on a machine that is not billing.
        """
        self.open_run(hours_ago=3.0)
        self.fake.add("pod-A", status="EXITED", uptime_seconds=7200, rate_usd_per_hour=2.0)

        code, text, document = self.doctor()

        accruing = float((document.get("spend") or {}).get("accruing_usd") or 0.0)
        self.assertLessEqual(
            accruing, 4.05,
            f"an open row on a stopped machine accrued ${accruing:.2f}: the row is three hours old "
            f"but the provider reports two hours of uptime, so an hour of it was not read — the "
            f"printed total grows past the truth.\nspend={document.get('spend')}\n{text}")


class ACorruptStart(DoctorCase):
    def test_an_unparseable_start_leaves_a_number_to_read(self):
        """Sequence: the open row's `started_at` is corrupt (an interrupted write). Effect required:
        the human still gets a number, and it is the same number the enforcement uses — the reading
        and the enforcement are one computation.

        If this fails by raising, the reading is unavailable: the one line a person uses to decide
        whether to stop a machine cannot be printed at all.
        """
        self.closed_run(hours=1.0)
        self.open_run(hours_ago=1.0)
        rows = self.ledger.entries()
        rows[-1]["started_at"] = "the-ledger-was-truncated"
        self.ledger._rewrite(rows)

        code, text, document = self.doctor()

        self.assert_readings_agree(document, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
