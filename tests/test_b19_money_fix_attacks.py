"""Attack the three B19 money-path fixes at `1b41c1d` — one class per fix.

Each test states the requirement it checks, so a failure is the finding. The failures this file is
designed to produce at `1b41c1d` are listed in the verdict artefact; the passing tests are the
controls and the boundaries. Offline: the in-memory fake, no network, no rental.

Requirement map:
  down   — the account's listing is the witness (the fix's own words). A GET that disagrees with
           `list_pods`, and a `down` that stops one machine and closes another's row, break it.
           A refused/unreadable/zero-bound reading must leave the row open.
  guard  — no wait guard may outlive `up`: no heartbeat may be written after `_release_wait_guard`
           returns, and no guard thread may survive any exit.
  adopt  — an `--uptime-hours` estimate must not claim time the machine cannot have had, and must
           not be able to produce a row that records nothing for a billing machine.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cfg_for(root: Path, *, cap: float = 10.0, per_run: float = 5.0,
            stop_confirm_s: float = 30.0) -> Config:
    cfg = Config(budget=Budget(cap_usd=cap, per_run_cap_usd=per_run, idle_minutes=0.0,
                               stop_confirm_s=stop_confirm_s),
                 state_dir=root / ".rentguard")
    cfg.provider = "fake"
    cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
    return cfg


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)
        self.provider = FakeProvider(rate_usd_per_hour=2.0)
        self.ledger = Ledger(self.cfg.ledger_path)

    def run_cmd(self, fn, argv: list[str]):
        args = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: self.provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(args)
        text = out.getvalue()
        document: dict = {}
        if "{" in text:
            try:
                document = json.loads(text[text.index("{"):])
            except ValueError:
                document = {"_unparseable": text}
        return code, document, text + err.getvalue()

    def down(self, *, stop_confirm_s: float | None = None):
        if stop_confirm_s is not None:
            self.cfg.budget.stop_confirm_s = stop_confirm_s
        return self.run_cmd(cli.cmd_down, ["down", "--json"])

    def record_state(self, instance_id: str) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": instance_id}), encoding="utf-8")

    def open_row(self, instance_id: str, hours: float = 1.0) -> dict:
        return self.ledger.open(purpose="window", provider="fake", instance_id=instance_id,
                                gpu_id="g", rate_usd_per_hour=2.0,
                                started_at=now_utc() - timedelta(hours=hours))

    def guard_threads(self) -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name.startswith("rentguard-wait-guard-")]


class TheAccountsListingIsTheWitness(Base):
    """The fix's own sentence: "the account's own listing decides". The code asks the
    single-instance endpoint and never the pod list, so the two can disagree."""

    def test_a_get_saying_exited_while_the_listing_shows_running_does_not_close_the_row(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        real_get = self.provider.get

        def get_says_exited(instance_id):
            pod = real_get(instance_id)
            return {**pod, "status": "EXITED"} if pod else None
        self.provider.get = get_says_exited
        self.open_row("pod-A")
        self.record_state("pod-A")

        code, document, text = self.down()

        listed = [p for p in self.provider.list_pods() if p.get("id") == "pod-A"]
        self.assertTrue(listed and listed[0]["status"] == "RUNNING",
                        "fixture: the account listing does not show pod-A live")
        self.assertIsNotNone(
            self.ledger.open_run(),
            f"the row closed while the account listed pod-A RUNNING: the witness was a GET that "
            f"said EXITED, and no other reading was consulted (exit {code}, "
            f"action {document.get('action')!r})\n{text}")

    def test_a_get_returning_none_while_the_listing_shows_running_does_not_close_the_row(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        real_get = self.provider.get
        self.provider.get = lambda iid: None if iid == "pod-A" else real_get(iid)
        self.open_row("pod-A")
        self.record_state("pod-A")

        code, document, text = self.down()

        listed = [p for p in self.provider.list_pods() if p.get("id") == "pod-A"]
        self.assertTrue(listed, "fixture: the account listing does not show pod-A")
        self.assertIsNotNone(
            self.ledger.open_run(),
            f"the row closed on a GET that returned None while the account listed pod-A "
            f"{listed[0]['status']}: absence from one endpoint is not absence from the account "
            f"(exit {code}, action {document.get('action')!r})\n{text}")

    def test_a_down_that_stops_one_machine_does_not_close_anothers_row(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")   # the state's instance
        self.provider.add("pod-B", uptime_seconds=900, status="RUNNING")   # the open row's instance
        self.record_state("pod-A")
        row_b = self.open_row("pod-B")

        code, document, text = self.down()

        self.assertEqual(self.provider.get("pod-B")["status"], "RUNNING",
                         "fixture: pod-B is not live")
        self.assertIsNotNone(
            self.ledger.open_run(),
            f"down stopped pod-A (the state's instance) and closed pod-B's row while pod-B is still "
            f"RUNNING and billing — the confirmation asked about the machine it stopped, not the "
            f"machine the row names. Closed row {row_b['run_id']} at "
            f"${self.ledger.spent_on(row_b['run_id'])} (exit {code})\n{text}")

    def test_an_unreadable_account_leaves_the_row_open_and_is_named(self) -> None:
        """The fix's docstring promises an unreadable account is "reported as unverified". That
        promise lives in `_confirm_stopped`; `down` asks the account again *first* (`_pod`), and that
        call has no handler, so the command dies there instead of naming the failure."""
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.open_row("pod-A")
        self.record_state("pod-A")

        def broken_get(instance_id):
            raise ProviderError("the account could not be read")
        self.provider.get = broken_get

        try:
            code, document, text = self.down()
        except ProviderError as exc:
            self.assertIsNotNone(self.ledger.open_run(), "the row closed despite the error")
            self.fail(f"down raised an uncaught {type(exc).__name__} from `_pod`'s listing call "
                      f"(cli.py:242), before `_confirm_stopped` could report it as unverified: the "
                      f"row stays open, but the command tracebacks instead of naming the failure")

        self.assertIsNotNone(self.ledger.open_run(),
                             f"a stop nobody could check closed the row:\n{text}")
        self.assertEqual(code, 1, f"an unverified stop exited 0:\n{text}")


class DownKeepsItsConservativeHalf(Base):
    def test_a_stop_that_returns_without_stopping_leaves_the_row_open_at_bound_zero(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.open_row("pod-A")
        self.record_state("pod-A")

        def action_without_stopping(instance_id, action):
            self.provider.actions.append((instance_id, action))     # accepted; machine unchanged
        self.provider.action = action_without_stopping

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIsNotNone(self.ledger.open_run(),
                             f"one read that says RUNNING closed the row:\n{text}")
        self.assertEqual(code, 1, f"a still-billing machine exited 0:\n{text}")
        self.assertEqual(document.get("action"), "still-billing", document)

    def test_bound_zero_closes_the_row_when_the_single_read_says_stopped(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.open_row("pod-A")
        self.record_state("pod-A")

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIsNone(self.ledger.open_run(), f"an EXITED reading did not close the row:\n{text}")
        self.assertEqual(code, 0, text)

    def test_confirm_stopped_polls_until_the_machine_stops(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        real_get = self.provider.get
        calls = {"n": 0}

        def flaky(instance_id):
            calls["n"] += 1
            pod = real_get(instance_id)
            return {**pod, "status": "RUNNING" if calls["n"] < 3 else "EXITED"}
        self.provider.get = flaky

        confirmed, detail = cli._confirm_stopped(self.provider, "pod-A", bound=5.0, interval=0.01)

        self.assertTrue(confirmed, f"the poll gave up before the machine stopped: {detail}")
        self.assertGreaterEqual(calls["n"], 3, "the wait did not poll")

    def test_abandon_created_closes_the_row_and_leaves_the_live_machine_for_doctor(self) -> None:
        """The half Forge named rather than fixed. Reachable: `up` interrupted (or its wait failed)
        after the stop was refused. The effect is a closed row for a machine that may be running —
        and it stays visible, because the state still names it and `doctor` reports the live machine
        with no row as an error."""
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.provider.fail_actions.add("stop")
        self.open_row("pod-A")
        self.record_state("pod-A")
        pod = self.provider.get("pod-A")

        cli._abandon_created(self.cfg, self.provider, self.ledger, pod)

        self.assertIsNone(self.ledger.open_run(), "the interrupt path left the row open")
        self.assertEqual(self.provider.get("pod-A")["status"], "RUNNING",
                         "fixture: the machine stopped")
        findings = guard.state_findings({"instance_id": "pod-A"}, None,
                                        provider_gate=lambda i: self.provider.get(i))
        self.assertTrue(any(f.severity == "error" for f in findings),
                        f"a live machine with a closed row is not an error for doctor: {findings}")


class TheWaitGuardCannotOutliveUp(Base):
    def test_the_guard_does_not_write_a_heartbeat_after_it_is_released(self) -> None:
        """`_release_wait_guard` joins for at most 5 s. A provider read slower than that returns with
        the thread alive, and the guard — which writes its heartbeat *after* the read — then writes
        one after `up` has released it."""
        class SlowGet(FakeProvider):
            def __init__(self, *a, delay=0.0, **k):
                super().__init__(*a, **k)
                self.delay = delay
                self.calls = 0

            def get(self, instance_id):
                self.calls += 1
                if self.calls == 1:
                    time.sleep(self.delay)
                return super().get(instance_id)

        provider = SlowGet(rate_usd_per_hour=2.0, delay=6.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.provider = provider

        pair = cli._start_wait_guard(self.cfg, provider, self.ledger, "pod-A",
                                     log=lambda *_: None, interval=0.05)
        thread, _release = pair
        time.sleep(0.3)                              # the guard is inside the slow read
        started = time.monotonic()
        cli._release_wait_guard(pair)
        joined_in = time.monotonic() - started
        alive_at_return = thread.is_alive()
        thread.join(15)
        heartbeat = (self.cfg.heartbeat_path.read_text("utf-8")
                     if self.cfg.heartbeat_path.exists() else "")

        self.assertFalse(
            alive_at_return,
            f"_release_wait_guard returned after {joined_in:.1f}s with the wait guard still inside "
            f"provider.get; it then wrote {self.cfg.heartbeat_path.name} "
            f"({'a heartbeat' if heartbeat else 'nothing'}) — a heartbeat that survives `up` "
            f"returning, which `status` and `doctor` read as protection armed")

    def test_no_wait_guard_thread_survives_a_successful_up(self) -> None:
        code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "probe", "--json"])
        time.sleep(0.2)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.guard_threads(), [],
                         f"a wait guard outlived a successful up: {self.guard_threads()}")

    def test_no_wait_guard_thread_survives_the_unusable_path(self) -> None:
        unusable = {"status": "RUNNING", "cost": 2.0,
                    "runtime": {"uptime": 0, "ports": None, "gpus": [{"util": 0.0}]}}
        # The confirmation bound is config, and this fixture pins it to zero: the test is about a
        # thread surviving, and with the default 30 s it spent that long confirming a machine the
        # provider will never stop (reported by Forge, 2026-10-16, as a 2x suite slowdown).
        self.cfg.budget.stop_confirm_s = 0.0
        with mock.patch.object(self.provider, "wait_running",
                               lambda iid, log=None: {**unusable, "id": iid}), \
                mock.patch.object(self.provider, "get",
                                  lambda iid: {**unusable, "id": iid}):
            code, document, text = self.run_cmd(cli.cmd_up,
                                                ["up", "--purpose", "probe", "--json"])
        time.sleep(0.2)
        self.assertEqual(code, 1, f"an unusable machine did not fail the rent:\n{text}")
        self.assertEqual(self.guard_threads(), [],
                         f"a wait guard outlived the unusable path: {self.guard_threads()}")

    def test_two_wait_guards_for_two_machines_are_both_released(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.provider.add("pod-B", uptime_seconds=600, status="RUNNING")
        a = cli._start_wait_guard(self.cfg, self.provider, self.ledger, "pod-A",
                                  log=lambda *_: None, interval=0.05)
        b = cli._start_wait_guard(self.cfg, self.provider, self.ledger, "pod-B",
                                  log=lambda *_: None, interval=0.05)
        cli._release_wait_guard(a)
        cli._release_wait_guard(b)

        self.assertFalse(a[0].is_alive(), "the first guard outlived its release")
        self.assertFalse(b[0].is_alive(), "the second guard outlived its release")


class AdoptEstimateCannotRecordNothing(Base):
    def adopt(self, provider, argv_extra: list[str]):
        self.provider = provider
        return self.run_cmd(cli.cmd_adopt,
                            ["adopt", "pod-A", "--purpose", "probe", "--json", *argv_extra])

    def test_a_negative_estimate_does_not_open_a_row_that_records_nothing(self) -> None:
        """`--uptime-hours=-2` implies a start in the future, which the creation check cannot see
        (a future instant does not precede creation). The row opens, accrues $0.00, and the cap
        reads nothing for a machine that is billing."""
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        before = now_utc()

        code, document, text = self.adopt(self.provider, ["--uptime-hours=-2"])

        row = self.ledger.open_run()
        started = datetime.fromisoformat(row["started_at"]) if row else None
        # The failure message has to survive the *passing* case too: when the refusal works there is
        # no row, and formatting `started` (None) raised TypeError before the assertion could run.
        # Forge, 2026-10-16: message repaired, requirement untouched.
        ahead = (f"{(started - before).total_seconds() / 3600:.2f} h in the future" if started
                 else "no row was opened at all")
        self.assertNotEqual(
            code, 0,
            f"--uptime-hours=-2 was accepted and opened a row starting {row and row['started_at']} "
            f"({ahead}) whose "
            f"accrual is ${self.ledger.live_usd():.3f}; the cap and the guard read nothing for a "
            f"machine that is billing\n{text}")

    def test_the_estimate_boundaries_at_creation_and_the_slack(self) -> None:
        age = 1800.0                                  # the machine is 30 minutes old
        for seconds, accepted, label in [(age, True, "exactly at creation"),
                                         (age + 30.0, True, "30 s inside the 60 s slack"),
                                         (age + 90.0, False, "90 s before creation")]:
            with self.subTest(label=label):
                self.setUp()
                self.provider.add("pod-A", uptime_seconds=0,
                                  created_at=self.provider._now() - age, status="RUNNING")
                code, document, text = self.adopt(self.provider,
                                                  [f"--uptime-hours={seconds / 3600.0}"])
                self.assertEqual(code, 0 if accepted else 2,
                                 f"{label}: expected {'accepted' if accepted else 'refused'}, "
                                 f"got {code}:\n{text}")
                if not accepted:
                    self.assertEqual(document.get("reason"), "uptime-precedes-creation", document)

    def test_an_estimate_without_created_at_is_refused_or_the_gap_is_named(self) -> None:
        """The named gap: with no `createdAt` the check is skipped, so nothing bounds the estimate."""
        self.provider.add("pod-A", uptime_seconds=600, created_at=0, status="RUNNING")

        code, document, text = self.adopt(self.provider, ["--uptime-hours=100"])

        row = self.ledger.open_run()
        self.assertNotEqual(
            code, 0,
            f"with no createdAt, --uptime-hours=100 opened a row starting "
            f"{row and row['started_at']} — 100 h back, past any machine that could exist, and "
            f"nothing checked it (${self.ledger.live_usd():.2f} recorded)\n{text}")

    def test_a_provider_uptime_contradicting_created_at_still_backdates_the_row(self) -> None:
        """The second named gap, pinned as the current behaviour: the creation check runs only for
        `--uptime-hours`, so a provider uptime that cannot fit in the machine's life backdates the
        row past creation unexamined. This test asserts what happens today, not what should."""
        self.provider.add("pod-A", uptime_seconds=3 * 3600 - 600,
                          created_at=self.provider._now() - 600, status="RUNNING")   # 10 min old

        code, document, text = self.adopt(self.provider, [])

        row = self.ledger.open_run()
        self.assertIsNotNone(row, f"no row:\n{text}")
        started = datetime.fromisoformat(row["started_at"])
        created = cli._instance_created_at(self.provider.get("pod-A"))
        self.assertLess(started, created, "fixture: the row does not precede creation")
        self.assertEqual(code, 0, text)

    def test_a_naive_created_at_is_read_as_utc(self) -> None:
        """A stamp without a zone is read as UTC. The direction of the error follows the host's
        offset; this test pins the interpretation, not a refusal."""
        offset = datetime.now().astimezone().utcoffset()
        if not offset or not offset.total_seconds():
            self.skipTest("this host is at UTC, so a naive stamp cannot be misread")

        class NaiveCreatedAt(FakeProvider):
            def get(self, instance_id):
                pod = super().get(instance_id)
                if pod is None:
                    return None
                local = datetime.now().astimezone().replace(tzinfo=None) - timedelta(minutes=10)
                return {**pod, "createdAt": local.isoformat(timespec="seconds")}

        provider = NaiveCreatedAt(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")

        code, document, text = self.adopt(provider, ["--uptime-hours=1"])

        read = cli._instance_created_at(provider.get("pod-A"))
        drift = (read - (now_utc() - timedelta(minutes=10))).total_seconds()
        self.assertAlmostEqual(drift, offset.total_seconds(), delta=5,
                               msg=f"a naive stamp was not read as UTC: drift {drift}s, "
                                   f"offset {offset}")


class TheReAttackAt4e7f3d3(Base):
    """The fixes at `4e7f3d3` re-attacked. The five findings are closed (the classes above pass);
    these are the holes the new code opens or leaves, and the boundaries it now states."""

    def test_a_status_that_is_neither_live_nor_terminal_does_not_close_the_row(self) -> None:
        """`_confirm_stopped`'s own docstring: "EXITED, TERMINATED or gone means it can no longer
        bill, and anything else means it still can." The code is the opposite for every status that
        is neither live nor terminal — `if last not in LIVE_STATUSES: return True` — and the listing
        gate uses the same set, so both witnesses agree a machine in a transitional state has
        stopped. `STOPPING` is the concrete case; the class is any status the provider adds outside
        the two sets."""
        self.provider.add("pod-A", uptime_seconds=600, status="STOPPING")
        self.open_row("pod-A")
        self.record_state("pod-A")

        def action_keeps_it_stopping(instance_id, action):
            self.provider.actions.append((instance_id, action))   # accepted; still shutting down
        self.provider.action = action_keeps_it_stopping

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIsNotNone(
            self.ledger.open_run(),
            f"the row closed on a status that is neither live nor EXITED/TERMINATED/gone: the pod "
            f"reports STOPPING, which the docstring says still bills, and both witnesses treat it "
            f"as finished (exit {code}, action {document.get('action')!r})\n{text}")

    def test_a_crash_loop_that_cannot_be_stopped_does_not_close_its_row(self) -> None:
        """The `is_alive` -> `billing` change is right; this pins that the stopped-half still holds
        when the stop is refused: a RUNNING pod with no uptime and no ports is billing, and its row
        must not close under it."""
        self.provider.add("pod-A", uptime_seconds=0, ports=None, status="RUNNING")
        self.provider.fail_actions.add("stop")
        self.open_row("pod-A")
        self.record_state("pod-A")

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIsNotNone(self.ledger.open_run(),
                             f"the row closed under a crash loop that would not stop:\n{text}")
        self.assertEqual(code, 1, f"a still-billing crash loop exited 0:\n{text}")

    def test_an_unreadable_listing_does_not_close_the_row(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.open_row("pod-A")
        self.record_state("pod-A")

        def broken_listing():
            raise ProviderError("the account listing could not be read")
        self.provider.list_pods = broken_listing

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIsNotNone(self.ledger.open_run(),
                             f"the row closed although the listing was never read:\n{text}")
        self.assertEqual(code, 1, text)

    def test_down_leaves_the_rows_machine_alone_when_it_is_not_the_states(self) -> None:
        """The new rule: `down` stops what its own state names and may only close the row when the
        account also says the *row's* machine has stopped. With both live, pod-A (state) stops,
        pod-B (row) is untouched, and the row stays open."""
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.provider.add("pod-B", uptime_seconds=900, status="RUNNING")
        self.record_state("pod-A")
        self.open_row("pod-B")

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIn(("pod-A", "stop"), self.provider.actions,
                      f"the state's machine was not stopped:\n{text}")
        self.assertNotIn(("pod-B", "stop"), self.provider.actions,
                         f"the row's machine was stopped although the state names pod-A:\n{text}")
        self.assertEqual(self.provider.get("pod-B")["status"], "RUNNING", "fixture")
        self.assertIsNotNone(self.ledger.open_run(),
                             f"the row closed while the machine it names is still billing:\n{text}")
        self.assertEqual(code, 1, text)

    def test_nan_and_inf_estimates_are_refused_not_crashed(self) -> None:
        """The closure of finding 9: `--uptime-hours` takes a float, so nan/inf/-inf parse, and all
        three are refused by the named reason rather than reaching `timedelta`."""
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                self.setUp()
                self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
                try:
                    code, document, text = self.adopt_(self.provider, [f"--uptime-hours={value}"])
                except (ValueError, OverflowError) as exc:
                    self.fail(f"--uptime-hours={value} crashed with {type(exc).__name__}: {exc} "
                              f"instead of a named refusal; no row should be opened from a value "
                              f"that is not a length of time")
                self.assertNotEqual(code, 0,
                                    f"--uptime-hours={value} was accepted (exit {code}); a row "
                                    f"opened from a value that is not a length of time\n{text}")
                self.assertEqual(document.get("reason"), "uptime-not-a-length-of-time", document)

    def adopt_(self, provider, argv_extra: list[str]):
        self.provider = provider
        return self.run_cmd(cli.cmd_adopt,
                            ["adopt", "pod-A", "--purpose", "probe", "--json", *argv_extra])

    def test_the_provider_uptime_bounds_an_estimate_without_created_at(self) -> None:
        """The bound that narrows finding 6: with no `createdAt`, the machine's own positive uptime
        is the witness (plus the same 60 s slack), and an estimate inside it is still accepted."""
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=1800, created_at=0, status="RUNNING")   # 0.5 h reported

        code, document, text = self.adopt_(provider, ["--uptime-hours=0.5"])
        self.assertEqual(code, 0, f"an estimate equal to the reported uptime was refused:\n{text}")
        self.assertIsNotNone(self.ledger.open_run(), text)

        self.setUp()
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=1800, created_at=0, status="RUNNING")
        code, document, text = self.adopt_(provider, ["--uptime-hours=0.52"])   # 1872 s > 1860 s
        self.assertEqual(code, 2, f"an estimate past the reported uptime was accepted:\n{text}")
        self.assertEqual(document.get("reason"), "uptime-exceeds-life", document)

    def test_the_residual_gap_is_no_created_at_and_no_positive_uptime(self) -> None:
        """Named in `cmd_adopt` and limitations.md: with neither witness, any positive estimate is
        accepted. This asserts the gap, not the desired behaviour."""
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=0, created_at=0, status="EXITED")   # no createdAt, 0 up

        code, document, text = self.adopt_(provider, ["--uptime-hours=50"])

        row = self.ledger.open_run()
        self.assertIsNotNone(row, f"no row:\n{text}")
        started = datetime.fromisoformat(row["started_at"])
        self.assertGreater((now_utc() - started).total_seconds(), 49 * 3600,
                           "fixture: the row is not 50 h back")
        self.assertEqual(code, 0, text)

    def test_the_release_join_is_unbounded_on_a_read_that_does_not_return(self) -> None:
        """The trade the new gate buys: `_release_wait_guard` no longer returns while the guard is
        inside a provider read. Measured, not asserted as a defect — the provider's own request
        timeout is what bounds it, and no heartbeat can land either way."""
        unblock = threading.Event()

        class BlockingGet(FakeProvider):
            def get(self, instance_id):
                unblock.wait(10.0)
                return super().get(instance_id)

        provider = BlockingGet(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        pair = cli._start_wait_guard(self.cfg, provider, self.ledger, "pod-A",
                                     log=lambda *_: None, interval=0.05)
        time.sleep(0.2)
        released = threading.Thread(target=lambda: cli._release_wait_guard(pair), daemon=True)
        released.start()
        time.sleep(0.5)
        still_joining = released.is_alive()
        unblock.set()
        released.join(10)

        self.assertTrue(still_joining, "the release returned while the guard was still reading")


class TheUnknownStatusBoundary(Base):
    """The polarity Forge reported rather than changed: the *closure* rule (findings 8) says a status
    that is not EXITED/TERMINATED is still unfinished, while *visibility* — `_live`, `_pod`'s
    fallback, `doctor`'s live list, `guard.state_findings` — says only RUNNING/PROVISIONING/STARTING
    is live. A machine in neither set (`STOPPING`, `ERROR`) is therefore called unfinished when a row
    is closed and invisible when a bill is looked for.

    Effect required: a machine the tool's own closure rule will not call finished must not be
    invisible. It need not block the rent — it may be seconds from gone — but `up` may not rent
    beside it in silence, and `doctor` may not print "nothing is billing" while it exists."""

    UNKNOWN_STATUSES = ("STOPPING", "ERROR")

    def unrecorded(self, status: str):
        self.provider.add("pod-unfinished", uptime_seconds=600, status=status)   # no row, no state
        return self.run_cmd(cli.cmd_up, ["up", "--purpose", "probe", "--json"])

    def test_up_does_not_rent_in_silence_beside_an_unfinished_machine(self) -> None:
        for status in self.UNKNOWN_STATUSES:
            with self.subTest(status=status):
                self.setUp()
                code, document, text = self.unrecorded(status)
                self.assertTrue(
                    code != 0 or "pod-unfinished" in text,
                    f"up rented beside a {status} machine with no ledger row and never named it: "
                    f"`_live` reads LIVE_STATUSES, so it is not in the untracked list; the closure "
                    f"rule calls the same machine unfinished (exit {code})\n{text}")

    def test_doctor_does_not_say_nothing_is_billing_beside_an_unfinished_machine(self) -> None:
        for status in self.UNKNOWN_STATUSES:
            with self.subTest(status=status):
                self.setUp()
                self.provider.add("pod-unfinished", uptime_seconds=600, status=status)
                code, document, text = self.run_cmd(cli.cmd_doctor, ["doctor", "--json"])
                named = "pod-unfinished" in text or any(
                    "pod-unfinished" in json.dumps(f) for f in document.get("findings", []))
                self.assertTrue(
                    named,
                    f"doctor did not name a {status} machine that has no ledger row, so an "
                    f"unrecorded bill in a status the closure rule calls unfinished is invisible "
                    f"(exit {code})\n{text}")

    def test_a_running_machine_with_no_row_still_refuses_and_is_reported(self) -> None:
        """The control: the polarity is right for a genuinely live status."""
        self.provider.add("pod-running", uptime_seconds=600, status="RUNNING")
        code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "probe", "--json"])
        self.assertEqual(code, 2, f"a live unrecorded machine did not refuse the rent:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

        self.setUp()
        self.provider.add("pod-running", uptime_seconds=600, status="RUNNING")
        code, document, text = self.run_cmd(cli.cmd_doctor, ["doctor", "--json"])
        self.assertIn("pod-running", text, f"doctor did not name the live unrecorded machine:\n{text}")
        self.assertEqual(code, 1, f"doctor did not fail on a live unrecorded machine:\n{text}")

    def test_a_stopping_machine_with_an_open_row_keeps_it_open(self) -> None:
        """The closure half of the same machine: with a row, `down` leaves it open (finding 8's fix),
        which is what bounds the exposure of the invisible case. The stop call is accepted but the
        machine stays STOPPING, which is the shape the fix was written for."""
        self.provider.add("pod-unfinished", uptime_seconds=600, status="STOPPING")
        self.open_row("pod-unfinished")
        self.record_state("pod-unfinished")

        def action_keeps_it_stopping(instance_id, action):
            self.provider.actions.append((instance_id, action))
        self.provider.action = action_keeps_it_stopping

        code, document, text = self.down(stop_confirm_s=0.0)

        self.assertIsNotNone(self.ledger.open_run(),
                             f"a STOPPING machine's row closed:\n{text}")
        self.assertEqual(code, 1, text)


class TheReusePathAndTheInvertedRuling(Base):
    """Gilbert's ruling inverts the polarity (`guard.is_billing`), so `up` refuses for a status it
    cannot call finished — loud, and escaped with `--force`. These pin the ruling's own contract,
    and the one path it did not reach: `up`'s **reuse** branch, which Forge reported rather than
    changed."""

    def test_the_inverted_refusal_names_the_machine_and_force_is_the_escape(self) -> None:
        for status in ("STOPPING", "ERROR", ""):
            with self.subTest(status=status):
                self.setUp()
                self.provider.add("pod-block", uptime_seconds=600, status=status)
                code, document, text = self.run_cmd(cli.cmd_up,
                                                    ["up", "--purpose", "probe", "--json"])
                self.assertEqual(code, 2, f"a {status} machine did not block the rent:\n{text}")
                self.assertEqual(document.get("reason"), "live-untracked-instance", document)
                self.assertIn("pod-block", text, f"the refusal does not name the machine:\n{text}")
                self.assertIn("--force", text, f"the refusal does not offer the escape:\n{text}")

                self.setUp()
                self.provider.add("pod-block", uptime_seconds=600, status=status)
                code, document, text = self.run_cmd(
                    cli.cmd_up, ["up", "--purpose", "probe", "--force", "--json"])
                self.assertEqual(code, 0,
                                 f"--force did not get past a {status} machine:\n{text}")
                self.assertEqual(len(self.provider.created), 1, text)

    def test_up_reuse_does_not_report_a_crash_loop_as_a_rental(self) -> None:
        """The reuse branch checks no usability and arms no wait guard. State names pod-A, a row is
        open for it, and the machine reports RUNNING with no uptime and no ports — the crash loop
        `run` refuses to upload to. `up` reports `reused`, exit 0, and prints ssh access."""
        self.provider.add("pod-A", uptime_seconds=0, ports=None, status="RUNNING")
        self.record_state("pod-A")
        self.open_row("pod-A")

        code, document, text = self.run_cmd(cli.cmd_up,
                                            ["up", "--purpose", "again", "--json"])

        self.assertTrue(
            code != 0 or "not usable" in text.lower() or "crash" in text.lower(),
            f"up reported {document.get('action')!r} with exit {code} for a machine it cannot "
            f"upload to: the reuse branch checks neither `is_alive` nor the second reading the "
            f"create path requires, and arms no wait guard, so a crash loop is billed with nothing "
            f"watching it\n{text}")

    def test_up_reuse_of_a_usable_machine_still_succeeds(self) -> None:
        """The control: the fix must not turn every reuse into a failure."""
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")   # uptime and ports set
        self.record_state("pod-A")
        self.open_row("pod-A")

        code, document, text = self.run_cmd(cli.cmd_up,
                                            ["up", "--purpose", "again", "--json"])

        self.assertEqual(code, 0, f"a usable machine could not be reused:\n{text}")
        self.assertEqual(document.get("action"), "reused", document)
        self.assertEqual(self.provider.created, [], f"reuse rented a second machine:\n{text}")


class TheReuseFixAtD8accac(Base):
    """`up`'s reuse path now takes a second alive reading, stops what fails it, and arms a wait
    guard for a resume. These attack each clause, plus the failure path the create path handles and
    this one does not."""

    def test_a_machine_that_vanishes_between_the_two_readings_is_not_a_reuse(self) -> None:
        """`confirmed = provider.get(...) or pod` discards the second reading when it says the
        machine is gone, so the stale first reading is what gets checked and reported."""
        class Vanishing(FakeProvider):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.calls = 0

            def get(self, instance_id):
                self.calls += 1
                if self.calls >= 2:
                    return None                     # gone between the two readings
                return super().get(instance_id)

        provider = Vanishing(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.provider = provider
        self.record_state("pod-A")

        code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "again", "--json"])

        gone = provider.get("pod-A") is None
        self.assertTrue(gone, "fixture: the machine did not vanish")
        self.assertNotEqual(
            code, 0,
            f"up reported {document.get('action')!r} (exit {code}) for a machine the account no "
            f"longer shows: the second reading returned nothing and the stale first one was used, "
            f"so a row is now open against a machine that does not exist\n{text}")

    def test_a_resume_whose_wait_fails_does_not_leave_a_billing_machine_behind(self) -> None:
        """The create path's `except BaseException` stops the machine and closes the row. The reuse
        branch has only a `finally` that releases the guard, so a `wait_running` that raises leaves
        the machine it just started RUNNING with no row and nothing watching it."""
        class BrokenResume(FakeProvider):
            def wait_running(self, instance_id, log=None, **kwargs):
                raise ProviderError("instance did not become usable after 12 min")

        provider = BrokenResume(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.provider = provider
        self.record_state("pod-A")                   # no row: the machine is not yet recorded

        raised = None
        try:
            code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "resume", "--json"])
        except ProviderError as exc:
            raised, code, document, text = exc, None, {}, ""

        status = provider.get("pod-A")
        status = status.get("status") if status else None
        recorded = any(r.get("instance_id") == "pod-A" for r in self.ledger.entries())
        self.assertFalse(
            status == "RUNNING" and not recorded,
            f"a failed resume left pod-A {status} with no ledger row and no guard: the restore was "
            f"started, the wait raised ({raised or 'no exception: exit %s' % code}), and nothing "
            f"stopped the machine the command had just revived\n{text}")

    def test_the_guard_is_armed_while_a_resume_waits(self) -> None:
        """The clause: a resume's wait is guarded, not only a create's."""
        class RecordingResume(FakeProvider):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.guard_during_wait = None

            def wait_running(self, instance_id, log=None, **kwargs):
                self.guard_during_wait = any(
                    t.name.startswith("rentguard-wait-guard-") for t in threading.enumerate())
                time.sleep(0.2)                      # the pilot's first ten minutes, scaled down
                return super().wait_running(instance_id, log=log)

        provider = RecordingResume(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.provider = provider
        self.record_state("pod-A")

        code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "resume", "--json"])
        time.sleep(0.2)

        self.assertTrue(provider.guard_during_wait,
                        f"no wait guard was armed while the resume waited:\n{text}")
        self.assertEqual(code, 0, f"a slow but successful resume failed:\n{text}")
        self.assertEqual(document.get("action"), "reused", document)
        self.assertNotIn(("pod-A", "stop"), provider.actions,
                         f"a resume that became usable was stopped:\n{text}")
        self.assertEqual(self.guard_threads(), [],
                         f"a wait guard outlived the resume: {self.guard_threads()}")

    def test_a_machine_that_becomes_usable_between_the_readings_is_not_stopped(self) -> None:
        """`wait_running` returns one reading that merely looks alive; the second read is the
        witness. A machine usable on the second read must be reported, not stopped, even though the
        payload the wait returned does not look alive."""
        not_yet = {"status": "RUNNING", "cost": 2.0,
                   "runtime": {"uptime": 0, "ports": None, "gpus": [{"util": 0.0}]}}

        class LateResume(FakeProvider):
            def wait_running(self, instance_id, log=None, **kwargs):
                return {**not_yet, "id": instance_id}     # this reading says not usable ...
            # ... and `get` is the real, usable payload.

        provider = LateResume(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="EXITED")
        self.provider = provider
        self.record_state("pod-A")

        code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "resume", "--json"])

        self.assertEqual(code, 0, f"the second reading said usable and the rent failed:\n{text}")
        self.assertEqual(document.get("action"), "reused", document)
        self.assertNotIn(("pod-A", "stop"), provider.actions,
                         f"a machine usable on the second read was stopped:\n{text}")


class TheSharedRetireMachineAtFe38468(Base):
    """`_retire_machine` owns one invariant: the machine is this root's, and it is either usable,
    stopped, or visibly billing in an open row. Each branch is attacked directly, then the create
    path's half of finding 13 and the dying exception."""

    def retire(self, provider, *, instance="pod-A", reason="unusable", dying=False,
               stop_confirm_s=None):
        if stop_confirm_s is not None:
            self.cfg.budget.stop_confirm_s = stop_confirm_s
        self.provider = provider
        return cli._retire_machine(self.cfg, provider, self.ledger, instance, reason=reason,
                                   dying=dying)

    def test_an_unreadable_account_leaves_the_row_open_and_stops_nothing(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.open_row("pod-A")

        def broken(instance_id):
            raise ProviderError("the account could not be read")
        self.provider.get = broken

        result = self.retire(self.provider)

        self.assertIsNone(result, "an unreadable account returned a closed row")
        self.assertIsNotNone(self.ledger.open_run(),
                             "the row closed although the account was never read")
        self.assertNotIn(("pod-A", "stop"), self.provider.actions,
                         "a machine was stopped on an unreadable account")

    def test_a_machine_named_for_another_root_is_left_alone_and_its_row_voided(self) -> None:
        import hashlib
        other_tag = hashlib.sha256(r"C:\somewhere\else".encode()).hexdigest()[:8]
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.provider.instances["pod-A"].id = "pod-A"
        real_get = self.provider.get
        self.provider.get = lambda iid: {**real_get(iid), "name": f"rentguard-{other_tag}"}
        row = self.open_row("pod-A")

        result = self.retire(self.provider)

        self.assertIsNone(result)
        self.assertNotIn(("pod-A", "stop"), self.provider.actions,
                         "another root's machine was stopped")
        closed = [r for r in self.ledger.entries() if r.get("run_id") == row["run_id"]][0]
        self.assertTrue(closed.get("voided"), f"the row was not voided: {closed}")
        self.assertEqual(float(closed.get("estimated_usd") or 0.0), 0.0, closed)

    def test_an_unconfirmed_stop_leaves_the_row_open(self) -> None:
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.open_row("pod-A")

        def action_without_stopping(instance_id, action):
            self.provider.actions.append((instance_id, action))     # accepted; still RUNNING
        self.provider.action = action_without_stopping

        result = self.retire(self.provider, stop_confirm_s=0.0)

        self.assertIsNone(result, "an unconfirmed stop returned a closed row")
        self.assertIsNotNone(self.ledger.open_run(),
                             "the row closed although the account still shows the machine billing")
        self.assertIn(("pod-A", "stop"), self.provider.actions, "no stop was attempted")

    def test_a_gone_machine_closes_its_own_row_and_no_other(self) -> None:
        self.open_row("pod-A")                         # one open row: pod-A's
        result = self.retire(self.provider)            # the provider has no pod-A at all

        self.assertIsNotNone(result, "a vanished machine's row was left open")
        self.assertIsNone(self.ledger.open_run(), "the row is still open")

        # And a row naming a different machine is left exactly as it is.
        self.setUp()
        other = self.open_row("pod-B")
        result = self.retire(self.provider, instance="pod-A")
        self.assertIsNone(result, "a row naming another machine was closed")
        self.assertIsNotNone(self.ledger.open_run(), "pod-B's row was closed by pod-A's exit")
        self.assertEqual(self.ledger.open_run().get("run_id"), other["run_id"])

    def test_the_create_path_does_not_open_a_row_for_a_machine_that_vanished(self) -> None:
        """Finding 13's other half: the create path had the same `or pod`, so a machine gone by the
        second reading was reported as a rental with a row against it."""
        class GoneAfterCreate(FakeProvider):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.gone = False

            def wait_running(self, instance_id, log=None, **kwargs):
                pod = super().wait_running(instance_id, log=log)
                self.gone = True
                return pod

            def get(self, instance_id):
                if self.gone:
                    return None
                return super().get(instance_id)

        provider = GoneAfterCreate(rate_usd_per_hour=2.0)
        self.provider = provider
        self.cfg.budget.stop_confirm_s = 0.0

        code, document, text = self.run_cmd(cli.cmd_up, ["up", "--purpose", "probe", "--json"])

        created_id = (document.get("instance") or {}).get("id")
        open_row = self.ledger.open_run()
        self.assertNotEqual(code, 0,
                            f"the create path reported a rental for a machine the account does not "
                            f"show (exit {code}, action {document.get('action')!r})\n{text}")
        self.assertTrue(open_row is None or open_row.get("instance_id") != created_id,
                        f"a row is open against a machine the account does not show: {open_row}")

    def test_the_dying_path_closes_without_waiting_and_leaves_the_machine_visible(self) -> None:
        """`_abandon_created` is `_retire_machine(dying=True)`: the interrupt path closes the row
        without waiting for the account. The machine is stopped first, and what may still bill is
        left visible. This pins the effect and the deliberate exception."""
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.open_row("pod-A")
        self.record_state("pod-A")
        self.cfg.budget.stop_confirm_s = 30.0          # a waiting path would spend this
        self.provider.action = lambda iid, action: self.provider.actions.append((iid, action))
        pod = self.provider.get("pod-A")

        started = time.monotonic()
        cli._abandon_created(self.cfg, self.provider, self.ledger, pod)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, f"the dying path waited {elapsed:.1f}s for the account")
        self.assertIn(("pod-A", "stop"), self.provider.actions,
                      "the dying path did not even try to stop the machine")
        self.assertIsNone(self.ledger.open_run(), "the dying path left the row open")
        self.assertEqual(self.provider.get("pod-A")["status"], "RUNNING", "fixture")
        findings = guard.state_findings({"instance_id": "pod-A"}, None,
                                        provider_gate=lambda i: self.provider.get(i))
        self.assertTrue(any(f.severity == "error" for f in findings),
                        f"the machine left behind is invisible to doctor: {findings}")


if __name__ == "__main__":
    unittest.main()
