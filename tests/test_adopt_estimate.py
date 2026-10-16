"""`adopt --uptime-hours` must not backdate a start to before the machine existed (defect 3, B19).

The pilot window, 2026-10-16: row `20261016T151659Z-7e958f` was adopted with
`--uptime-hours 0.31` — an **estimate**, passed rather than read — and `adopt` backdated its start to
15:16:59, which is **before the pod was created** (15:23:31). About 0.11 h was counted twice: the row
says $0.276147 where the machine's own life supports ≈$0.20, so the episode is over-recorded by
roughly $0.23.

An estimate is a legitimate input — `adopt` exists for machines whose upload is already over the
account, and `--uptime-hours` is the way to record one whose report is gone — but it must not be able
to claim time the machine **cannot have had**. RunPod's payload carries the creation instant
(`createdAt`), so the check is possible rather than a guess: measured on a live pod,
`createdAt = 2026-10-16T10:42:54.019Z`.

So: an estimate whose implied start precedes the provider's own creation time is **refused**, with
both instants named, and no row is opened. A smaller estimate — inside the machine's life — is still
accepted, and so is an estimate when the provider gives **neither** a creation instant **nor** a
positive uptime: a check that cannot be made must not invent an answer.

Two holes the re-attack on 2026-10-16 (`tests/test_b19_money_fix_attacks.py`, findings 5 and 6) found
in the first version of that check, both closed here:

* `--uptime-hours=-2` was accepted. The implied start is in the **future**, so the creation check
  (which only looks backwards) could not see it, and the row then accrued $0.00 for a machine that was
  billing while the cap and the guard read nothing. An estimate of 0 or less is now refused outright.
* with no `createdAt` the check was skipped entirely, so `--uptime-hours=100` opened a row 100 h back
  for a machine the provider described as ten minutes old. The machine's own **positive uptime** is the
  second witness: an estimate longer than that claims time the machine cannot have had either. A
  *zero* uptime is not a witness — that is what a stopped or restarted machine reports, which is the
  case `--uptime-hours` exists for — so the residual gap (no creation instant *and* no positive
  uptime) stays open and is named in `cmd_adopt`'s docstring rather than closed with an invented bound.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class AdoptCase(unittest.TestCase):
    AGE_SECONDS = 300.0        # the machine was created five minutes ago

    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.ledger = Ledger(self.cfg.ledger_path)
        self.provider = FakeProvider(rate_usd_per_hour=0.89)
        # The fake's clock is monotonic, so the age is expressed in its own units; its payload
        # converts that to the wall-clock `createdAt` a provider reports.
        self.provider.add("pod-A", uptime_seconds=0, status="EXITED",
                          created_at=FakeProvider._now() - self.AGE_SECONDS)

    def adopt(self, *argv: str) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["adopt", "pod-A", "--json", *argv])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: self.provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_adopt(args)
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, document, text + err.getvalue()


class AnEstimateOlderThanTheMachine(AdoptCase):
    def test_an_estimate_that_precedes_creation_is_refused(self) -> None:
        code, document, text = self.adopt("--uptime-hours", "0.5")     # 30 min against a 5 min life

        self.assertEqual(code, 2, f"an estimate older than the machine was accepted:\n{text}")
        self.assertEqual(document.get("reason"), "uptime-precedes-creation", document)
        self.assertIsNone(self.ledger.open_run(),
                          "a row was opened at a start the machine cannot have had: the ledger now "
                          f"over-records it by construction\n{text}")
        self.assertIn("created", text.lower(), f"the refusal does not name the creation time:\n{text}")

    def test_an_estimate_inside_the_machines_life_is_still_accepted(self) -> None:
        """The control: three minutes of a five-minute-old machine is a legitimate estimate."""
        code, document, text = self.adopt("--uptime-hours", "0.05")

        self.assertEqual(code, 0, f"a legitimate estimate was refused:\n{text}")
        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"no row was opened:\n{text}")
        self.assertEqual(run.get("instance_id"), "pod-A", run)

    def test_an_estimate_is_accepted_when_the_provider_does_not_say_when_it_was_created(self) -> None:
        """No creation time and no positive uptime: the check cannot be made, and one witness is
        not invented. The remaining limit is declared in `cmd_adopt`'s docstring."""
        self.provider.instances["pod-A"].created_at = 0.0        # the payload then carries no stamp

        code, document, text = self.adopt("--uptime-hours", "5.0")

        self.assertEqual(code, 0, f"an uncheckable estimate was refused:\n{text}")
        self.assertIsNotNone(self.ledger.open_run(), text)


class AnEstimateThatIsNoLengthOfTime(AdoptCase):
    """An estimate of 0 or less records nothing for a machine that is billing (re-attack finding 5).

    `--uptime-hours=-2` puts the row's start two hours in the **future** — where it can never accrue —
    and the creation check cannot see that, because a future instant does not precede creation. A typo
    is enough. Re-attack finding 5 called this SEVERE: the row, the cap and the guard all read $0.00
    while the machine charged.
    """

    def test_a_negative_estimate_is_refused_and_no_row_opens(self) -> None:
        code, document, text = self.adopt("--uptime-hours", "-2")

        self.assertEqual(code, 2, f"a negative estimate was accepted:\n{text}")
        self.assertEqual(document.get("reason"), "uptime-not-a-length-of-time", document)
        self.assertIsNone(self.ledger.open_run(),
                          f"a row was opened that can never accrue:\n{text}")
        self.assertIn("positive", text, f"the refusal does not say what to pass instead:\n{text}")

    def test_a_zero_estimate_is_refused_and_no_row_opens(self) -> None:
        code, document, text = self.adopt("--uptime-hours", "0")

        self.assertEqual(code, 2, f"a zero estimate was accepted:\n{text}")
        self.assertEqual(document.get("reason"), "uptime-not-a-length-of-time", document)
        self.assertIsNone(self.ledger.open_run(), text)

    def test_nan_and_infinity_are_refused_rather_than_crashing(self) -> None:
        """Re-attack finding 9: `float` parses both, `<= 0` sees neither (NaN comparisons are False),
        and `timedelta(seconds=...)` then raised ValueError / OverflowError out of `cmd_adopt`."""
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                self.setUp()
                code, document, text = self.adopt(f"--uptime-hours={value}")

                self.assertEqual(code, 2, f"--uptime-hours={value} was not refused:\n{text}")
                self.assertEqual(document.get("reason"), "uptime-not-a-length-of-time", document)
                self.assertIsNone(self.ledger.open_run(), text)

    def test_the_message_names_the_smallest_sensible_value(self) -> None:
        """Gilbert's requirement on the refusal: say what the smallest sensible value is."""
        code, document, text = self.adopt("--uptime-hours", "-0.5")

        self.assertEqual(code, 2, text)
        self.assertIn("0.01 h is 36 s", text,
                      f"the smallest sensible estimate is not named:\n{text}")
        self.assertIn("36 s", text, f"the refusal does not put a unit on it:\n{text}")


class TheSecondWitnessIsTheProvidersOwnUptime(AdoptCase):
    """With no `createdAt`, the machine's positive uptime bounds the estimate (re-attack finding 6).

    The first version checked nothing without a creation instant, so `--uptime-hours=100` opened a row
    100 h back for a machine the provider described as ten minutes old.
    """

    def no_creation_instant(self, *, uptime_seconds: float, status: str = "RUNNING") -> None:
        instance = self.provider.instances["pod-A"]
        instance.created_at = 0.0            # the payload then carries no `createdAt`
        instance.uptime_seconds = uptime_seconds
        instance.clock_offset = uptime_seconds
        instance.status = status

    def test_an_estimate_longer_than_the_providers_uptime_is_refused(self) -> None:
        self.no_creation_instant(uptime_seconds=600.0)          # ten minutes of uptime

        code, document, text = self.adopt("--uptime-hours", "5.0")

        self.assertEqual(code, 2, f"a 5 h estimate against a 10 min machine was accepted:\n{text}")
        self.assertEqual(document.get("reason"), "uptime-exceeds-life", document)
        self.assertIsNone(self.ledger.open_run(),
                          f"a row was opened for time the machine had not lived:\n{text}")
        self.assertIn("uptime", text.lower(), f"the refusal does not name the witness:\n{text}")

    def test_the_boundary_is_the_providers_uptime_plus_the_slack(self) -> None:
        for extra, accepted, label in ((30.0, True, "30 s inside the 60 s slack"),
                                       (90.0, False, "90 s past it")):
            with self.subTest(label=label):
                self.setUp()
                self.no_creation_instant(uptime_seconds=600.0)
                code, document, text = self.adopt("--uptime-hours",
                                                  f"{(600.0 + extra) / 3600.0}")
                self.assertEqual(code, 0 if accepted else 2,
                                 f"{label}: expected {'accepted' if accepted else 'refused'}:\n{text}")

    def test_a_zero_uptime_is_not_a_witness_so_the_named_gap_stays_open(self) -> None:
        """0 is what a stopped or restarted machine reports — the case `--uptime-hours` is for."""
        self.no_creation_instant(uptime_seconds=0.0)

        code, document, text = self.adopt("--uptime-hours", "3.0")

        self.assertEqual(code, 0, f"a legitimate estimate was refused:\n{text}")
        self.assertIsNotNone(self.ledger.open_run(), text)


if __name__ == "__main__":
    unittest.main()
