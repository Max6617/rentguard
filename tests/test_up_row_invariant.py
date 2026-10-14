"""`up`: create ⇒ either a row or a stop, even on an interruption (defect 10).

Pod ``2abx0hz2boe6ow`` (RTX 3080 Ti) was live on 2026-10-14 17:57 in no ledger at all. `doctor`
found it and Cadence adopted it from the provider's uptime and stopped it. The detector worked; the
gap it was covering is this one: the ledger row opened only after the **second** alive reading, so
everything between ``create`` returning and that moment — a ``wait_running`` that can legitimately
run for minutes — was a window in which a live machine had no record, and an interruption anywhere
in it left one billing that nothing named.

These tests pin the invariant, and the design behind it: **the row is opened before the machine can
exist** and corrected in place when the provider names the instance. That choice is deliberate over
"stop the machine on the way out", because a stop is code that has to run *after* the interruption,
which is exactly when the process may be dying (Ctrl-C twice, a kill, a power cut), while a row
written before the irreversible step survives all of them. The stop is still attempted when the
process is alive to do it; the row is what is guaranteed.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.guard import state_findings  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class UpCase(unittest.TestCase):
    """A scratch project, the fake provider, and no network."""

    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.write_state({})

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.cfg.ledger_path)

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def invoke_up(self, provider, *argv: str) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "invariant", *argv])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out):
            try:
                code = cli.cmd_up(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def rows(self) -> list[dict]:
        return self.ledger.entries()


# ============================================================== the row comes first

class RowBeforeConfirmation(UpCase):
    def test_the_row_names_the_machine_while_it_is_still_provisioning(self):
        """Sequence: `up` runs and the fake's provisioning delay is read from inside
        ``wait_running`` — the moment the machine exists and is billing but is not yet confirmed.

        Effect: a row already exists and already names the machine. The old order left this window
        empty, and `wait_running` is where most of a rental's first minutes go.
        """

        class ReadsTheLedgerWhileWaiting(FakeProvider):
            def __init__(self, *a, watch=None, **k):
                super().__init__(*a, **k)
                self.watch = watch
                self.seen: dict | None = None

            def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
                self.seen = self.watch() if self.watch else None
                return super().wait_running(instance_id, timeout_minutes, poll_s, log)

        provider = ReadsTheLedgerWhileWaiting(
            watch=lambda: Ledger(self.cfg.ledger_path).open_run())

        code, out = self.invoke_up(provider)

        self.assertEqual(code, 0, f"up failed on a healthy machine.\noutput:\n{out}")
        self.assertIsNotNone(provider.seen, "the machine was billing before a row existed for it")
        self.assertEqual(provider.seen.get("instance_id"), "fake-1",
                         f"the row does not name the machine that is billing: {provider.seen}")
        final = self.ledger.open_run()
        self.assertIsNotNone(final)
        self.assertEqual(final.get("run_id"), provider.seen.get("run_id"),
                         "the confirmed rental replaced the provisional row instead of correcting "
                         "it, which would count the same minutes twice or drop them")
        self.assertEqual(final.get("instance_id"), "fake-1")

    def test_the_providers_own_rate_replaces_the_catalogue_estimate(self):
        """Sequence: the catalogue says 0.13/h, the created pod costs 0.19/h.

        Effect: the row bills at the pod's own rate. The provisional row exists before the provider
        has answered, so its rate is a catalogue estimate; leaving that estimate in place would
        charge the wrong number for the whole run.
        """

        class PricierThanAdvertised(FakeProvider):
            def create(self, **kwargs):
                pod = super().create(**kwargs)
                self.instances[pod["id"]].rate_usd_per_hour = 0.19
                return self.get(pod["id"])

        code, out = self.invoke_up(PricierThanAdvertised())

        self.assertEqual(code, 0, f"up failed.\noutput:\n{out}")
        run = self.ledger.open_run()
        self.assertEqual(run.get("rate_usd_per_hour"), 0.19,
                         f"the row kept the catalogue estimate: {run}")


# ============================================================== the ways it ends

class InterruptedUp(UpCase):
    def test_an_interrupt_during_the_wait_records_and_stops_the_machine(self):
        """Sequence: `create` succeeds and the wait for the machine is interrupted (Ctrl-C,
        measured as KeyboardInterrupt).

        Effect: the rental is recorded and the machine is stopped. Before the fix there was no row
        at all, which is pod 2abx0hz2boe6ow's story: money that exists only in the provider's log.
        """

        class Interrupted(FakeProvider):
            def wait_running(self, *a, **k):
                raise KeyboardInterrupt

        provider = Interrupted()

        with self.assertRaises(KeyboardInterrupt):
            self.invoke_up(provider)

        rows = self.rows()
        self.assertEqual(len(rows), 1, f"{len(rows)} rows for one created machine: {rows}")
        row = rows[0]
        self.assertEqual(row.get("instance_id"), "fake-1",
                         f"the recorded row does not name the machine: {row}")
        self.assertTrue(row.get("stopped_at"),
                        f"the row was left open with nothing to close it: {row}")
        self.assertIsNotNone(row.get("estimated_usd"),
                             f"the interrupted rental recorded no cost: {row}")
        self.assertFalse(row.get("voided"),
                         "an interrupted rental was voided as though it had never existed")
        self.assertIn(("fake-1", "stop"), provider.actions,
                      "the interrupted machine was left billing")
        self.assertEqual(provider.get("fake-1")["status"], "EXITED")

    def test_a_machine_that_never_becomes_usable_is_recorded_and_stopped(self):
        """Sequence: `create` returns, and every reading says the machine is RUNNING with uptime 0
        and no ports — the crash loop the tool refuses to upload to.

        Effect: it is stopped, and the row is closed naming it with the seconds it existed. A row
        that is absent (or void) for a machine that really billed is the same defect in the other
        direction: cost that is not on any ledger.
        """

        class NeverUsable(FakeProvider):
            def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
                return self.get(instance_id) or {}

        provider = NeverUsable()

        code, out = self.invoke_up(provider)

        self.assertNotEqual(code, 0, f"a crash loop was reported as a rental.\noutput:\n{out}")
        rows = self.rows()
        self.assertEqual(len(rows), 1, f"{len(rows)} rows for one created machine: {rows}")
        row = rows[0]
        self.assertEqual(row.get("instance_id"), "fake-1")
        self.assertTrue(row.get("stopped_at"), f"the row was left open: {row}")
        self.assertFalse(row.get("voided"),
                         "a machine that existed and billed was recorded as if it had not")
        self.assertIsNotNone(row.get("estimated_usd"))
        self.assertIn(("fake-1", "stop"), provider.actions)

    def test_a_rental_nothing_was_created_for_is_voided_not_charged(self):
        """Sequence: every candidate is offered by the catalogue and refused by `create` (the M2
        shape), so no machine ever exists.

        Effect: exit 3, and the row that existed for the duration is voided rather than counted.
        Voiding is not silence — the row stays in the file with its reason, which is the visible
        half of "we tried and nothing happened".
        """
        fake = FakeProvider()
        fake.unavailable_gpus = {"Fake GPU 24GB"}
        fake.offer_unavailable = True

        code, out = self.invoke_up(fake, "--purpose", "no stock")

        self.assertEqual(code, 3, f"up exited {code}.\noutput:\n{out}")
        rows = self.rows()
        self.assertEqual(len(rows), 1, f"{len(rows)} rows: {rows}")
        self.assertTrue(rows[0].get("voided"),
                        f"a row for a machine that was never created was counted: {rows[0]}")
        self.assertEqual(self.ledger.total_usd(), 0.0,
                         "a refused create contributed money to the total")

    def test_a_create_that_lost_its_answer_leaves_a_row_doctor_reports(self):
        """Sequence: `create` makes the machine and the answer never comes back (here:
        KeyboardInterrupt raised from inside `create`, the shape a lost response has).

        Effect: a row exists — it was opened before the call — and it names no instance, because the
        id is exactly what was lost. That is the honest limit of the fix: `doctor` reports
        ``open-run-no-instance`` as an error, so the residue is loud rather than invisible. What is
        *not* left is silence, and the row's rate is the catalogue's, so the accrual is not zero.
        """

        class LostTheAnswer(FakeProvider):
            def create(self, **kwargs):
                super().create(**kwargs)
                raise KeyboardInterrupt

        provider = LostTheAnswer()

        with self.assertRaises(KeyboardInterrupt):
            self.invoke_up(provider)

        row = self.ledger.open_run()
        self.assertIsNotNone(row, "a machine was created with no row of any kind")
        self.assertIsNone(row.get("instance_id"),
                          "the lost id cannot be invented, and must not be")
        self.assertGreater(row.get("rate_usd_per_hour") or 0.0, 0.0,
                           f"the row's accrual counts as zero: {row}")
        codes = [f.code for f in state_findings({}, row)]
        self.assertIn("open-run-no-instance", codes,
                      f"doctor has nothing to say about this row: {row}")

    def test_an_interrupted_rental_closes_its_row_even_when_the_stop_is_refused(self):
        """Sequence: the wait is interrupted and the provider refuses the stop.

        Effect: the row is closed anyway, recording what the machine cost, and the output says the
        machine may still be billing. This is the shutdown-path rule: a refused stop must not skip
        the ledger close, or the spend hides in an open row while the machine keeps running.
        """

        class InterruptedAndUnstoppable(FakeProvider):
            def wait_running(self, *a, **k):
                raise KeyboardInterrupt

        provider = InterruptedAndUnstoppable()
        provider.fail_actions = {"stop"}

        with self.assertRaises(KeyboardInterrupt):
            self.invoke_up(provider)

        row = self.ledger.open_run()
        self.assertIsNone(row, "a refused stop left the row open instead of recording the cost")
        rows = self.rows()
        self.assertEqual(len(rows), 1, f"{len(rows)} rows: {rows}")
        self.assertTrue(rows[0].get("stopped_at"))
        self.assertIn(("fake-1", "stop"), provider.actions)

    def test_a_rental_whose_row_vanished_is_stopped_instead_of_kept(self):
        """Sequence: `create` succeeds, and by the time the row is corrected there is no row (another
        process closed it, or a lock was broken).

        Effect: the machine is stopped and `up` fails. A machine billing with no row is the one state
        this tool exists to prevent, and keeping it because the row is missing would be the worst
        reading of "the ledger is the record".
        """

        class Healthy(FakeProvider):
            pass

        provider = Healthy()
        with mock.patch.object(Ledger, "finalise", lambda self, run_id, **fields: None):
            code, out = self.invoke_up(provider)

        self.assertNotEqual(code, 0, f"a rental with no row was reported as success.\n{out}")
        self.assertIn(("fake-1", "stop"), provider.actions,
                      "the machine was left billing with no row for it")
        self.assertIsNone(self.ledger.open_run())

    def test_a_catalogue_that_raises_does_not_stop_the_rental(self):
        """The estimate is a courtesy: a provider that cannot price a GPU must not block the rental
        or crash it — the pod's own rate is recorded seconds later either way."""
        from rentguard.provider import ProviderError

        class Unpriceable(FakeProvider):
            def price_of(self, gpu_id, cloud="SECURE"):
                raise ProviderError("catalogue unavailable")

        code, out = self.invoke_up(Unpriceable())

        self.assertEqual(code, 0, f"an unpriceable GPU blocked the rental.\n{out}")
        run = self.ledger.open_run()
        self.assertEqual(run.get("instance_id"), "fake-1")
        self.assertEqual(run.get("rate_usd_per_hour"), 0.13,
                         f"the pod's own rate was not recorded: {run}")

    def test_a_provisional_row_left_by_an_earlier_run_is_voided_by_the_next_one(self):
        """Sequence: an earlier `up` died between opening its row and being told the instance id; a
        new `up` runs.

        Effect: the abandoned row is voided, not counted and not kept open. Counting it beside the
        new row would charge the same minutes twice; leaving it open would block the new row.
        """
        abandoned = self.ledger.open(purpose="lost", provider="fake", instance_id=None,
                                     gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.13,
                                     tag="provisional")

        code, out = self.invoke_up(FakeProvider())

        self.assertEqual(code, 0, f"up failed.\noutput:\n{out}")
        rows = {row["run_id"]: row for row in self.rows()}
        self.assertTrue(rows[abandoned["run_id"]].get("voided"),
                        f"the abandoned row was counted: {rows[abandoned['run_id']]}")
        open_run = self.ledger.open_run()
        self.assertIsNotNone(open_run)
        self.assertNotEqual(open_run["run_id"], abandoned["run_id"])
        self.assertEqual(open_run.get("instance_id"), "fake-1")


# ============================================================== the mechanism

class FinaliseTest(unittest.TestCase):
    """``Ledger.finalise``: correct the open row in place, and refuse to do more than that."""

    def setUp(self) -> None:
        self.ledger = Ledger(scratch_dir(self) / "runs.jsonl")

    def open_provisional(self) -> dict:
        return self.ledger.open(purpose="p", provider="fake", instance_id=None, gpu_id="G",
                                rate_usd_per_hour=0.13, tag="provisional")

    def test_finalise_fills_in_the_instance_and_keeps_the_identity(self):
        run = self.open_provisional()

        out = self.ledger.finalise(run["run_id"], instance_id="fake-1", gpu_id="H",
                                   rate_usd_per_hour=0.19)

        self.assertEqual(out["run_id"], run["run_id"], "finalise gave the row a new identity")
        self.assertEqual(out["started_at"], run["started_at"],
                         "finalise moved the start of the rental")
        self.assertEqual(out["instance_id"], "fake-1")
        self.assertEqual(out["gpu_id"], "H")
        self.assertEqual(out["rate_usd_per_hour"], 0.19)
        self.assertEqual(out["tag"], "provisional", "finalise dropped the row's tag")
        self.assertIsNone(out["stopped_at"], "finalise closed the row")
        self.assertEqual(self.ledger.open_run()["instance_id"], "fake-1",
                         "the correction was not written to the file")

    def test_finalise_refuses_to_close_the_row(self):
        run = self.open_provisional()

        with self.assertRaises(ValueError):
            self.ledger.finalise(run["run_id"], stopped_at="2026-01-01T00:00:00+00:00")

        self.assertIsNotNone(self.ledger.open_run(), "the refused call closed the row anyway")

    def test_finalise_does_nothing_for_a_row_that_is_not_open(self):
        run = self.open_provisional()
        self.ledger.close(note="done")

        self.assertIsNone(self.ledger.finalise(run["run_id"], instance_id="fake-1"))
        self.assertIsNone(self.ledger.finalise("no-such-run", instance_id="fake-1"))


if __name__ == "__main__":
    unittest.main()
