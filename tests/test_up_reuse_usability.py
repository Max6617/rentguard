"""`up`'s reuse path: a second alive reading, and a guard while it waits (finding 12, 2026-10-16).

The **create** path requires two consecutive alive readings, stops a machine that is not usable, and
arms a wait guard while `wait_running` runs (defect 2, B19). The **reuse** path — the branch taken when
the state file or the open row already names a machine — did none of that: it noted the status, kept or
opened the row, printed ssh access and emitted `action: "reused"`, exit 0.

Measured by Assay at `5277ed8` with a pod reporting RUNNING, `uptime=0`, `ports=None` — the crash loop
`run` and `pull` refuse to touch (`is_alive` requires a positive uptime *and* a port):

    instance pod-A is already RUNNING
    ssh            : ssh fake@127.0.0.1
    action "reused", exit 0

So a machine that was billing with nothing watching it, and that `run` would refuse seconds later, was
reported as a rental. That is defect 2's shape on the one path the B19 work did not reach.

Both halves are fixed the same way the create path fixes them: one more `provider.get` and `is_alive`
on it (the second reading), the unusable path when it fails (stop, close the row, exit non-zero, and a
sentence saying the machine is not usable), and a wait guard armed around the resume's `wait_running`
and released on every way out — the caps are the guard's to enforce, and a pod that never becomes
usable is the state that needs a bound most.

The control that must not change: reusing a **usable** machine still succeeds with `action: "reused"`
and no second rental.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class ResumeNeverBecomesUsable(FakeProvider):
    """A resume that restarts into the crash loop: RUNNING with no uptime and no ports, for ever.

    `start` is accepted and leaves the machine unusable — the restart that never comes up — and `stop`
    is honoured, so the wait guard's stop is what ends `wait_running`. The row's accrual, not the
    machine's uptime, is what the caps are measured against while it is stuck.
    """

    def action(self, instance_id, action):
        self.actions.append((instance_id, action))
        instance = self.instances[instance_id]
        if action == "start":
            instance.status = "RUNNING"
            instance.uptime_seconds = 0.0
            instance.clock_offset = 0.0
            instance.ports = None
        elif action == "stop":
            instance.status = "EXITED"
            instance.ports = None
        return None

    def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
        deadline = time.time() + 20.0
        while time.time() < deadline:
            pod = self.get(instance_id) or {}
            if str(pod.get("status")) not in ("RUNNING", "PROVISIONING", "STARTING"):
                return pod
            time.sleep(0.02)
        return self.get(instance_id) or {}


class ReuseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.cfg.ledger_path)

    def open_row(self, instance_id: str = "pod-A", rate: float = 2.0) -> dict:
        return self.ledger.open(purpose="reuse", provider="fake", instance_id=instance_id,
                                gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                                started_at=now_utc() - timedelta(hours=0.5))

    def up(self, provider) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "again", "--json"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.cmd_up(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, document, text


class ACrashLoopIsNotARental(ReuseCase):
    def test_reuse_of_a_crash_loop_does_not_report_a_rental(self) -> None:
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=0, ports=None, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})
        self.open_row()

        code, document, text = self.up(provider)

        self.assertNotEqual(code, 0, f"a crash loop was reported as a rental:\n{text}")
        self.assertNotEqual(document.get("action"), "reused", document)
        self.assertIn("not usable", text.lower(), f"the reason is not stated:\n{text}")
        self.assertIn(("pod-A", "stop"), provider.actions,
                      f"the machine was left billing instead of stopped:\n{text}")
        self.assertIsNone(self.ledger.open_run(),
                          f"the row stayed open over a machine that was stopped:\n{text}")

    def test_reuse_of_a_usable_machine_still_succeeds(self) -> None:
        """The control: this must not turn every reuse into a failure."""
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})
        run = self.open_row()

        code, document, text = self.up(provider)

        self.assertEqual(code, 0, f"a usable machine could not be reused:\n{text}")
        self.assertEqual(document.get("action"), "reused", document)
        self.assertEqual(provider.created, [], f"reuse rented a second machine:\n{text}")
        self.assertEqual(self.ledger.open_run().get("run_id"), run["run_id"], text)

    def test_a_resume_that_never_becomes_usable_is_watched_by_a_guard(self) -> None:
        """The resume wait needs a guard for the same reason the create wait does.

        The open row accrues $0.065 (0.5 h at $0.13/h), under the $10 cap, and the per-run cap is one
        cent — so `up` proceeds, and the guard's first poll during the resume is already over the cap
        and stops the machine. Before the fix nothing was armed on this path, so there was no
        heartbeat and no stop from a guard.
        """
        provider = ResumeNeverBecomesUsable(rate_usd_per_hour=0.13)
        provider.add("pod-A", uptime_seconds=1800, status="EXITED")
        self.write_state({"instance_id": "pod-A"})
        self.open_row(rate=0.13)
        self.cfg.budget.per_run_cap_usd = 0.01

        code, document, text = self.up(provider)

        self.assertTrue(guard.heartbeat_files(self.cfg.heartbeat_path),
                        f"no guard was watching while `up` waited to resume a billing machine:\n{text}")
        self.assertIn(("pod-A", "stop"), provider.actions,
                      f"the guard did not stop a machine that kept billing:\n{text}")
        self.assertNotEqual(code, 0, f"a machine that never became usable was reported:\n{text}")

    def test_no_wait_guard_thread_survives_a_reuse(self) -> None:
        provider = ResumeNeverBecomesUsable(rate_usd_per_hour=0.13)
        provider.add("pod-A", uptime_seconds=1800, status="EXITED")
        self.write_state({"instance_id": "pod-A"})
        self.open_row(rate=0.13)
        self.cfg.budget.per_run_cap_usd = 0.01

        self.up(provider)

        time.sleep(0.2)
        alive = [t for t in threading.enumerate() if t.name.startswith("rentguard-wait-guard-")]
        self.assertEqual(alive, [], f"a wait guard outlived the reuse: {alive}")


class TheSecondReadingCannotSayGone(ReuseCase):
    """Finding 13: `or pod` folds "gone" into the stale first reading.

    pod-A is usable and state-named; between the first and the second reading the account stops showing
    it. `provider.get(...) or pod` then checked — and reported — the stale payload: `action: "reused"`,
    exit 0, and a row opened against a machine that does not exist. "Gone" is the one answer `or`
    cannot express.
    """

    class Vanishing(FakeProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def get(self, instance_id):
            self.calls += 1
            return None if self.calls >= 2 else super().get(instance_id)

    def test_a_machine_gone_between_the_readings_is_not_a_reuse(self) -> None:
        provider = self.Vanishing(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})

        code, document, text = self.up(provider)

        self.assertNotEqual(code, 0,
                            f"a machine the account no longer shows was called a rental:\n{text}")
        self.assertNotEqual(document.get("action"), "reused", document)
        self.assertIsNone(self.ledger.open_run(),
                          f"a row was opened against a machine that does not exist:\n{text}")
        self.assertIn("gone", text.lower(), f"the reason is not stated:\n{text}")

    def test_the_create_path_answers_gone_the_same_way(self) -> None:
        """The same line lives on the create path, and it runs the same discipline.

        A machine that vanishes between the create path's two readings must not be reported as
        `created` either, and the row the create opened must not be left open against it.
        """
        class VanishingAfterCreate(FakeProvider):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.waited = False

            def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
                pod = super().wait_running(instance_id, timeout_minutes, poll_s, log)
                self.waited = True                      # the account stops showing it from here
                return pod

            def get(self, instance_id):
                return None if self.waited else super().get(instance_id)

        provider = VanishingAfterCreate(rate_usd_per_hour=2.0)

        code, document, text = self.up(provider)

        self.assertNotEqual(code, 0, f"a machine that vanished was reported as rented:\n{text}")
        self.assertNotEqual(document.get("action"), "created", document)
        self.assertIsNone(self.ledger.open_run(),
                          f"a row was left open against a machine the account does not show:\n{text}")
        self.assertIn("gone", text.lower(), f"the reason is not stated:\n{text}")


class AFailedResumeIsNotLeftBilling(ReuseCase):
    """Finding 14: a `wait_running` that raises left the revived machine RUNNING, unrecorded, unguarded.

    The create path's `except BaseException` stops the machine and ends the row. The reuse branch had
    only a `finally` that released the guard, so the failure propagated with the machine the command
    had just started still billing.
    """

    class BrokenResume(FakeProvider):
        def wait_running(self, instance_id, log=None, **kwargs):
            raise ProviderError("instance did not become usable after 12 min")

    def test_a_failed_resume_does_not_leave_a_billing_machine_behind(self) -> None:
        provider = self.BrokenResume(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.write_state({"instance_id": "pod-A"})

        raised = None
        try:
            self.up(provider)
        except ProviderError as exc:            # the command still reports the failure
            raised = exc

        self.assertIsNotNone(raised, "the wait's failure was swallowed")
        status = (provider.get("pod-A") or {}).get("status")
        recorded = any(row.get("instance_id") == "pod-A" for row in self.ledger.entries())
        self.assertFalse(status == "RUNNING" and not recorded,
                         f"the failed resume left pod-A {status} with no row and nothing watching it")
        self.assertIn(("pod-A", "stop"), provider.actions,
                      "the machine the command revived was not stopped")

    def test_an_unconfirmable_stop_leaves_the_row_open_and_visibly_billing(self) -> None:
        """Gilbert's clause: stop it, confirm against the terminal set, and if the stop cannot be
        confirmed keep the row **open** rather than closing it."""
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=0, ports=None, status="RUNNING")
        provider.fail_actions = {"stop"}          # the stop is refused, so it keeps billing
        self.write_state({"instance_id": "pod-A"})
        self.open_row()
        self.cfg.budget.stop_confirm_s = 0.1

        code, document, text = self.up(provider)

        self.assertNotEqual(code, 0, f"a machine whose stop was refused exited 0:\n{text}")
        self.assertIsNotNone(
            self.ledger.open_run(),
            f"the row closed over a machine whose stop was never confirmed:\n{text}")
        self.assertIn("NOT closed", text, f"the open row is not stated:\n{text}")

class TheRetirePathKeepsItsOwnRules(ReuseCase):
    """`_retire_machine`'s own branches: the money-direction answers it must not get wrong."""

    class UnreadableAfterStart(FakeProvider):
        """The state file names pod-A and the first read works; everything after the start does not."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def get(self, instance_id):
            self.calls += 1
            if self.calls >= 2:
                raise ProviderError("the account could not be read")
            return super().get(instance_id)

        def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
            raise ProviderError("instance did not become usable after 12 min")

    def test_an_unreadable_account_leaves_the_row_open(self) -> None:
        """A machine nobody can read about cannot be stopped or confirmed, so the row stays OPEN."""
        provider = self.UnreadableAfterStart(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.write_state({"instance_id": "pod-A"})
        row = self.open_row()

        try:
            self.up(provider)
        except ProviderError:
            pass

        self.assertIsNotNone(self.ledger.open_run(),
                             "the row closed although the account could never be read")
        self.assertEqual(self.ledger.open_run().get("run_id"), row["run_id"])

    def test_the_interrupt_path_closes_the_row_even_when_the_account_is_unreadable(self) -> None:
        """Defect 10's rule, kept: on the way out the row is the guarantee, whatever the provider says.
        `_abandon_created` is the one place a row closes without the account's answer."""
        class Unreadable(FakeProvider):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.calls = 0

            def get(self, instance_id):
                self.calls += 1
                if self.calls >= 2:
                    raise ProviderError("the account could not be read")
                return super().get(instance_id)

        provider = Unreadable(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        pod = provider.get("pod-A")              # the payload `_pod` would have handed `up`
        self.write_state({"instance_id": "pod-A"})
        self.open_row()

        cli._abandon_created(self.cfg, provider, self.ledger, pod)

        self.assertIsNone(self.ledger.open_run(),
                          "the interrupt path left the row open, so a killing blow would leave no record")

    def test_the_retire_path_does_not_stop_another_roots_machine(self) -> None:
        """The name decides: another root's machine is that root's job, and this root's row is voided."""
        class Foreign(FakeProvider):
            def get(self, instance_id):
                pod = super().get(instance_id)
                return None if pod is None else {**pod, "name": "rentguard-00000000"}

            def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
                raise ProviderError("instance did not become usable after 12 min")

        provider = Foreign(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.write_state({"instance_id": "pod-A"})
        self.open_row()

        try:
            self.up(provider)
        except ProviderError:
            pass

        self.assertNotIn(("pod-A", "stop"), provider.actions,
                         "another root's machine was stopped because this ledger had no row for it")
        self.assertIsNone(self.ledger.open_run(), "the row this root opened was left accruing")
        entries = self.ledger.entries()
        self.assertTrue(entries and entries[0].get("voided"),
                        f"the row was not voided: {entries}")


if __name__ == "__main__":
    unittest.main()
