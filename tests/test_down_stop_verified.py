"""`down` may only call a window closed when the account agrees the machine stopped (defect 1, B19).

The pilot window, 2026-10-16: `rentguard down` printed `run closed : 20261016T152331Z-eda81e  0.1782 h
$0.158469` and exited 0 — and the pod was **still RUNNING and still billing**. `doctor` immediately
afterwards listed `hads5eexmhbsfu RUNNING $0.89/h tracked` with *"no row is open for it here"*. A
second `down` did nothing; `adopt` + `down` closed a third row and the machine was still live. Only a
`podTerminate` call to the provider stopped it.

For several minutes two things were true at once: **the ledger says the window is closed** and **the
machine is billing**. That is the silent spend this tool exists to prevent, and it is silent precisely
because the ledger — the record a reader trusts — is the half that lies.

So the acceptance pinned here: a stop *call* that returns is not a machine that stopped. The row is
closed only when the account listing stops showing the machine as billing (`EXITED`, `TERMINATED`, or
gone from the account); when it does not, the row stays **open** — so the accrual keeps being counted,
the cap keeps seeing it, and `doctor` shows a live machine with a row against it rather than a closed
window beside a billing pod. The exit code is non-zero and the output says what is true.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class IgnoresStop(FakeProvider):
    """A provider whose stop call returns happily and whose machine keeps running.

    That is the measured shape: the API answered, the pod stayed RUNNING. `fail_actions` is the
    *other* case (the call raises), and it is asserted separately below.
    """

    def action(self, instance_id: str, verb: str):
        self.actions.append((instance_id, verb))
        return {}


class DownCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.artefacts = Artefacts(pull=[], remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.cfg.budget.stop_confirm_s = 0.3          # the bound, kept short for a test
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A", "provider": "fake"}),
                                       encoding="utf-8")
        self.ledger = Ledger(self.cfg.ledger_path)
        self.ledger.open(purpose="pilot", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.89,
                         started_at=now_utc() - timedelta(hours=0.5))

    def down(self, provider) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["down", "--json"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_down(args)
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, document, text + err.getvalue()


class AStopThatIsNotHonoured(DownCase):
    def test_the_row_stays_open_while_the_machine_keeps_billing(self) -> None:
        provider = IgnoresStop(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertIsNotNone(
            self.ledger.open_run(),
            "the row was closed while the account still showed the machine RUNNING: the ledger says "
            f"the window is over and the machine is billing (this is defect 1)\n{text}")
        self.assertNotEqual(code, 0, f"a window that did not close exited 0:\n{text}")
        self.assertIn(("pod-A", "stop"), provider.actions, f"no stop was attempted:\n{text}")
        self.assertIn("still", text.lower(), f"the output does not say the machine is still up:\n{text}")
        self.assertIn("running", text.lower(), f"the status is not named:\n{text}")
        self.assertFalse(document.get("stopped", True), document)

    def test_the_kind_of_failure_is_named(self) -> None:
        provider = IgnoresStop(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertEqual(document.get("action"), "still-billing", document)
        self.assertIn("stop_confirm_s", text,
                      f"the bound that expired is not named, so a reader cannot tell why:\n{text}")


class AStopThatIsHonoured(DownCase):
    def test_the_row_closes_when_the_account_agrees_the_machine_stopped(self) -> None:
        """The control: the ordinary shape, which must not become a refusal."""
        provider = FakeProvider(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertIsNone(self.ledger.open_run(), f"the row stayed open after a real stop:\n{text}")
        self.assertEqual(code, 0, f"a clean stop exited {code}:\n{text}")
        self.assertTrue(document.get("stopped"), document)
        self.assertEqual(provider.get("pod-A")["status"], "EXITED", f"the machine kept running:\n{text}")

    def test_a_stop_call_that_raises_and_leaves_the_machine_up_does_not_close_the_row(self) -> None:
        """The other failure shape: the call raises, the machine stays RUNNING."""
        provider = FakeProvider(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")
        provider.fail_actions = {"stop"}

        code, document, text = self.down(provider)

        self.assertIsNotNone(
            self.ledger.open_run(),
            "a refused stop closed the row while the machine was still RUNNING: the spend is now "
            f"recorded as finished work and the machine is still billing\n{text}")
        self.assertNotEqual(code, 0, f"a refused stop exited 0:\n{text}")
        self.assertIn("could not stop", text.lower(), f"the refusal is not reported:\n{text}")

    def test_a_machine_already_stopped_still_closes_its_row(self) -> None:
        """The second control: nothing was stopped because nothing was billing."""
        provider = FakeProvider(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="EXITED")

        code, document, text = self.down(provider)

        self.assertIsNone(self.ledger.open_run(), f"a stopped machine's row stayed open:\n{text}")
        self.assertEqual(code, 0, f"an already-stopped machine exited {code}:\n{text}")


if __name__ == "__main__":
    unittest.main()
