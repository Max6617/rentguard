"""`down` may only call a window closed when the account agrees the machine stopped (defect 1).

The pilot window, 2026-10-16: `rentguard down` printed `run closed : <run-id>  0.1782 h
$0.158469` and exited 0 — and the pod was **still RUNNING and still billing**. `doctor` immediately
afterwards listed it as `RUNNING` and `tracked`, with *"no row is open for it here"*. A
second `down` did nothing; `adopt` + `down` closed a third row and the machine was still live. Only a
`podTerminate` call to the provider stopped it.

For several minutes two things were true at once: **the ledger says the window is closed** and **the
machine is billing**. That is the silent spend this tool exists to prevent, and it is silent precisely
because the ledger — the record a reader trusts — is the half that lies.

So the acceptance pinned here: a stop *call* that returns is not a machine that stopped. The row is
closed only when the account agrees on **both** witnesses — the instance's own record
(`provider.get`) and the account's own listing (`provider.list_pods`) — and the row stays **open**
when either of them still calls the machine billing. An unreadable witness is not agreement either.
That keeps the accrual counted, the cap seeing it, and `doctor` showing a live machine with a row
against it rather than a closed window beside a billing pod. The exit code is non-zero and the output
says what is true.

The re-attack on 2026-10-16 (`tests/test_money_fix_attacks.py`, findings 1-3) is the reason both
witnesses are here: the first version of this fix asked only the single-instance endpoint, so a `get`
that said `EXITED` — or returned nothing — while the listing still showed the machine RUNNING closed
the row over a billing pod, `down` could close **another** machine's row than the one it stopped, and
an unreadable account tracebacked out of the command instead of naming the failure.
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
from rentguard.provider import ProviderError  # noqa: E402
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

    def test_a_crash_loop_pod_is_stopped_rather_than_having_its_row_closed_under_it(self) -> None:
        """RUNNING with no uptime and no ports is not *usable* — and it is still billing.

        Those were one condition in the first version of this fix, so a crash-looping pod was
        skipped and its row closed while it billed. The stop gate is "can it charge me", which is the
        status; only the pull needs a machine that answers.
        """
        provider = FakeProvider(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=0, ports=None, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertIn(("pod-A", "stop"), provider.actions,
                      f"a billing crash loop was never asked to stop:\n{text}")
        self.assertIsNone(self.ledger.open_run(), f"the row stayed open after the stop:\n{text}")
        self.assertEqual(code, 0, f"a stopped crash loop exited {code}:\n{text}")


class TheListingDisagreesWithTheEndpoint(DownCase):
    """The listing is the witness, so a single-endpoint answer cannot close the row.

    Re-attack findings 1-3 (2026-10-16). Every case here is a *provider* that answers one way on
    `get` and another on `list_pods`; the row must stay open whenever either says billing.
    """

    class Disagree(FakeProvider):
        def __init__(self, *args, own="EXITED", listing="RUNNING", **kwargs):
            super().__init__(*args, **kwargs)
            self.own, self.listing = own, listing

        def get(self, instance_id):
            if self.own is None:                  # the endpoint returns nothing at all
                return None
            pod = super().get(instance_id)
            if pod is None:
                return None
            return {**pod, "status": self.own}

        def list_pods(self):
            rows = super().list_pods()
            return [{**row, "status": self.listing} for row in rows]

    def test_a_get_that_says_exited_while_the_listing_says_running_keeps_the_row_open(self) -> None:
        provider = self.Disagree(rate_usd_per_hour=0.89, own="EXITED", listing="RUNNING")
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertIsNotNone(
            self.ledger.open_run(),
            "the row closed on a GET that said EXITED while the account listing showed pod-A "
            f"RUNNING: the listing is the witness\ntext:\n{text}")
        self.assertNotEqual(code, 0, f"a still-billing machine exited 0:\n{text}")
        self.assertIn("listing", text.lower(), f"the witness that disagreed is not named:\n{text}")

    def test_a_get_that_returns_nothing_while_the_listing_says_running_keeps_the_row_open(self) -> None:
        provider = self.Disagree(rate_usd_per_hour=0.89, own=None, listing="RUNNING")
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertIsNotNone(
            self.ledger.open_run(),
            "absence from the single-instance endpoint was read as absence from the account while "
            f"the listing still showed pod-A RUNNING\ntext:\n{text}")
        self.assertNotEqual(code, 0, f"a still-billing machine exited 0:\n{text}")

    def test_an_unreadable_listing_keeps_the_row_open(self) -> None:
        """An unreadable witness is not agreement: `get` says EXITED, the listing cannot be read."""
        class NoListing(self.Disagree):
            def list_pods(self):
                raise ProviderError("the account listing is not answering")

        provider = NoListing(rate_usd_per_hour=0.89, own="EXITED", listing="RUNNING")
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)

        self.assertIsNotNone(self.ledger.open_run(),
                             f"a listing nobody could read was treated as agreement:\n{text}")
        self.assertNotEqual(code, 0, f"an unverified stop exited 0:\n{text}")
        self.assertIn("listing could not be read", text, f"the failure is not named:\n{text}")

    def test_an_unreadable_account_is_named_rather_than_tracebacking(self) -> None:
        """`down` asks the account through `_pod` first; that call used to have no handler."""
        class Broken(FakeProvider):
            def get(self, instance_id):
                raise ProviderError("the account could not be read")

        provider = Broken(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")

        code, document, text = self.down(provider)      # must return, not raise

        self.assertIsNotNone(self.ledger.open_run(),
                             f"a stop nobody could check closed the row:\n{text}")
        self.assertEqual(code, 1, f"an unverified stop exited {code}:\n{text}")
        self.assertIn("could not be read", text, f"the failure is not named:\n{text}")


class DownAnswersForTheMachineTheRowNames(DownCase):
    """`down` stops the machine its state names, and may only close the **row's** machine's books.

    Re-attack finding 2 (2026-10-16): the state file named pod-A and the open row named pod-B, both
    live; the first version stopped and confirmed pod-A and then closed pod-B's row while pod-B was
    still billing — a closed row for a machine still charging, which is defect 1 one machine over.
    """

    def two_machines(self, *, row_status="RUNNING"):
        provider = FakeProvider(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="RUNNING")     # the state's instance
        provider.add("pod-B", uptime_seconds=3600, status=row_status)    # the open row's instance
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A", "provider": "fake"}),
                                       encoding="utf-8")
        self.ledger.close(note="fixture")
        self.ledger.open(purpose="row's machine", provider="fake", instance_id="pod-B",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.89,
                         started_at=now_utc() - timedelta(hours=1))
        return provider

    def test_a_live_machine_the_row_names_keeps_the_row_open_and_is_not_stopped(self) -> None:
        provider = self.two_machines()

        code, document, text = self.down(provider)

        self.assertIn(("pod-A", "stop"), provider.actions, f"the state's machine was not stopped:\n{text}")
        self.assertEqual(provider.get("pod-B")["status"], "RUNNING",
                         f"this command stopped a machine its state file never named:\n{text}")
        self.assertIsNotNone(
            self.ledger.open_run(),
            "down closed pod-B's row while pod-B was still RUNNING and billing — the confirmation "
            f"asked about the machine it stopped, not the machine the row names\ntext:\n{text}")
        self.assertNotEqual(code, 0, f"a row closed over a billing machine exited 0:\n{text}")
        self.assertIn("pod-B", text, f"the machine that keeps the row open is not named:\n{text}")

    def test_the_row_closes_once_both_machines_are_finished(self) -> None:
        """The control: with the row's machine already stopped, the row closes and the stop is real."""
        provider = self.two_machines(row_status="EXITED")

        code, document, text = self.down(provider)

        self.assertIsNone(self.ledger.open_run(),
                          f"a row stayed open with nothing billing behind it:\n{text}")
        self.assertEqual(code, 0, f"a clean double-finish exited {code}:\n{text}")


class AStatusThatHasNotFinished(DownCase):
    """Only `EXITED`, `TERMINATED` or gone may close a row (re-attack finding 8, 2026-10-16).

    `_confirm_stopped`'s own docstring said "EXITED, TERMINATED or gone means it can no longer bill,
    and anything else means it still can", and the code did the opposite for every status outside
    `LIVE_STATUSES`: `if last not in LIVE_STATUSES: return True`. A machine the provider reports as
    `STOPPING` — neither live nor finished, and still billing while it shuts down — had its row closed
    with **both** witnesses agreeing, because the listing gate used the same set. One set, used twice,
    is what made the two witnesses agree on a wrong answer.
    """

    def test_a_stopping_pod_keeps_its_row_open(self) -> None:
        provider = IgnoresStop(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="STOPPING")   # a stop that is still in flight
        self.cfg.budget.stop_confirm_s = 0.0

        code, document, text = self.down(provider)

        self.assertIsNotNone(
            self.ledger.open_run(),
            "the row closed on a status that is neither live nor EXITED/TERMINATED/gone: pod-A "
            f"reports STOPPING, which still bills (exit {code}, action "
            f"{document.get('action')!r})\n{text}")
        self.assertNotEqual(code, 0, f"a still-shutting-down machine exited 0:\n{text}")
        self.assertIn("STOPPING", text, f"the status that blocked the closure is not named:\n{text}")

    def test_a_listing_that_shows_a_stopping_machine_keeps_the_row_open(self) -> None:
        """The listing gate's own half: the instance's record says gone, the listing says STOPPING."""
        class GoneButStopping(FakeProvider):
            def get(self, instance_id):
                return None

            def list_pods(self):
                rows = super().list_pods()
                return [{**row, "status": "STOPPING"} for row in rows]

        provider = GoneButStopping(rate_usd_per_hour=0.89)
        provider.add("pod-A", uptime_seconds=1800, status="STOPPING")

        code, document, text = self.down(provider)

        self.assertIsNotNone(
            self.ledger.open_run(),
            "the listing showed pod-A STOPPING and the row closed anyway, because absence from the "
            f"instance endpoint was read as finished (exit {code})\n{text}")
        self.assertNotEqual(code, 0, f"a still-shutting-down machine exited 0:\n{text}")
        self.assertIn("STOPPING", text, f"the status the listing reported is not named:\n{text}")


if __name__ == "__main__":
    unittest.main()
