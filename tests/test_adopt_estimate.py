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
accepted, and so is an estimate when the provider does not say when the machine was created: a check
that cannot be made must not invent an answer.
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
        """No creation time in the payload: the check cannot be made, and it must not invent one."""
        self.provider.instances["pod-A"].created_at = 0.0        # the payload then carries no stamp

        code, document, text = self.adopt("--uptime-hours", "5.0")

        self.assertEqual(code, 0, f"an uncheckable estimate was refused:\n{text}")
        self.assertIsNotNone(self.ledger.open_run(), text)


if __name__ == "__main__":
    unittest.main()
