"""Attacks on the `doctor` spend line: the printed total against the enforced one.

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
        """A closed row of a known length.

        One `now_utc()` for both ends. Taking the time twice made the closed hour carry the drift
        between the two calls, and this suite produced a `$2.000556` that was briefly read as a
        witness when it was a second of test time.
        """
        now = now_utc()
        run = self.ledger.open(purpose=purpose, provider="fake", instance_id="pod-old",
                               gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                               started_at=now - timedelta(hours=hours + 1))
        return self.ledger.close(note="void" if void else "down",
                                 stopped_at=now - timedelta(hours=1), void=void)

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
        """The printed total and the enforced total are two samples of a *live* quantity.

        `printed` comes from the `doctor` call that just ran; `enforced` is recomputed here, so the two
        differ by whatever accrued in between. At the $2/h rate these cases use, accrual is $0.000556 per
        second, which makes `places=4` a demand that both samples were taken within about 0.09 s of each
        other — a wall-clock race, not a property of the code. It surfaced three times: in the traced run
        in an earlier revision, untraced twice at the same revision, and again under the tracer in a clean worktree
        in an earlier revision (defect 19, 2026-10-15). One cent is ~18 s of accrual at that rate, so this tolerance
        cannot mask what these cases are about: every one of them hunts for a whole $2.00 row that was
        counted, mispriced or invented, two hundred times the tolerance below.
        """
        enforced = self.enforced()
        printed = self.printed(document)
        self.assertAlmostEqual(
            printed, enforced, places=2,
            msg=f"the printed total ${printed} and the enforced total ${enforced} disagree by more than a "
                f"cent — more than 18 s of accrual at these rates, so this is not the sampling interval."
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

        Severity, and why this queues rather than blocking (the design rule): the error is in the
        *conservative* direction — the cap reads as more spent than it is, so `up` refuses earlier
        rather than later. That is exactly why a later fix must not make the accrual optimistic:
        stopping the growth is the requirement; shrinking a real spend is not.
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


class CorruptStartCase(DoctorCase):
    """A corrupt `started_at` on the open row, with and without the one witness that can price it.

    The distinction is the whole question at `doctor`'s call site: if the row's start cannot be read
    and the pod's own clock is available, the accrual is the uptime-priced value; if the pod is not
    there either, there is no witness at all and the honest reading is a named unknown — not a
    number, and not zero dressed as a number.
    """

    def corrupt_open_row(self, *, hours_ago: float = 1.0) -> None:
        self.open_run(hours_ago=hours_ago)
        rows = self.ledger.entries()
        rows[-1]["started_at"] = "the-ledger-was-truncated"
        self.ledger._rewrite(rows)

    def up_dry_run(self) -> tuple[int, dict]:
        """`up`'s own cap decision on this ledger, without creating anything."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.cmd_up(cli_args("up", "--purpose", "probe", "--dry-run", "--json"))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        stdout = out.getvalue().strip()
        document = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
        return code, document


class ACorruptStartWithNoWitness(CorruptStartCase):
    def test_with_no_witness_the_total_is_unknown_and_the_cap_refuses(self):
        """Sequence: the open row's `started_at` is corrupt **and** the instance is not on the
        account at all, so there is no pod clock to fall back on. `doctor`, then `up --dry-run`.

        Effect required: the reading declares the total unknown (`spend.total_usd` is None, the
        accrual flagged unknown, the row and field named) and the enforcement refuses rather than
        computing a number — `up` exits 2 with `open-run-accrual-unknown`. Agreement here means both
        sides decline to invent the number, not that two figures match.

        The witness for this expectation is *absence*: `provider.get('pod-A')` returns None, which
        the run's own finding records as `open-run-instance-missing`.
        """
        self.closed_run(hours=1.0)
        self.corrupt_open_row(hours_ago=1.0)

        code, text, document = self.doctor()
        spend = document.get("spend") or {}
        codes = [finding.get("code") for finding in document.get("findings") or []]

        self.assertIsNone(spend.get("total_usd"),
                          f"a total was printed with no start and no uptime to price it from: "
                          f"spend={spend}\n{text}")
        self.assertTrue(spend.get("accrual_unknown"), f"the unknown accrual is not flagged: {spend}")
        self.assertIn("ledger-row-unreadable-start", codes, f"the corrupt row is not named: {codes}")
        self.assertIn("open-run-instance-missing", codes,
                      f"the fixture is supposed to have no pod; the finding is absent: {codes}")

        up_code, up_document = self.up_dry_run()
        self.assertEqual(up_code, 2,
                         f"up did not refuse on an accrual nobody can compute: {up_document}")
        self.assertEqual(up_document.get("reason"), "open-run-accrual-unknown", up_document)


class ACorruptStartWithThePodClock(CorruptStartCase):
    def test_with_the_pod_clock_the_accrual_is_priced_from_uptime(self):
        """Sequence: the same corrupt `started_at`, but the pod is readable and reports one hour of
        uptime at $2/h. `doctor`, then `up --dry-run`.

        Effect required: the accrual is the uptime-priced **$2.00**, the total is the closed $2.00
        plus it = **$4.00**, the row is still named as unreadable alongside the number, and the cap
        decision proceeds — this is the run whose row is corrupt and whose cap can still fire.

        The witness is the provider's own reading: `provider.get('pod-A')['runtime']['uptime']`.
        """
        self.closed_run(hours=1.0)
        self.corrupt_open_row(hours_ago=1.0)
        self.fake.add("pod-A", status="EXITED", uptime_seconds=3600, rate_usd_per_hour=2.0)

        code, text, document = self.doctor()
        spend = document.get("spend") or {}
        codes = [finding.get("code") for finding in document.get("findings") or []]

        self.assertAlmostEqual(float(spend.get("accruing_usd") or 0.0), 2.0, delta=0.05,
                               msg=f"the accrual was not priced from the pod's hour of uptime: "
                                   f"spend={spend}\n{text}")
        self.assertAlmostEqual(float(spend.get("total_usd") or 0.0), 4.0, delta=0.05,
                               msg=f"the printed total is not the closed total plus the uptime-priced "
                                   f"accrual: spend={spend}\n{text}")
        self.assertIn("ledger-row-unreadable-start", codes,
                      f"the row was priced but no longer named as unreadable: {codes}")

        up_code, up_document = self.up_dry_run()
        self.assertNotEqual(up_document.get("reason"), "open-run-accrual-unknown",
                            f"up still calls the accrual unknown although the pod clock priced it: "
                            f"{up_document}")
        self.assertEqual(up_code, 0, f"the cap decision did not proceed: {up_document}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
