"""Adversarial suite for rentguard's money and data paths, driven by an in-memory fake
provider: no network, no spend.

Run (from the workspace root):

    python -m pytest -q -p no:cacheprovider rentguard/tests/test_money_paths.py

Every test states the exact user sequence in its docstring, and asserts the *effect* that must
hold (money stopped, row recorded, corruption reported), never that a code path was entered.
A failure here is a finding to report with its observed numbers; the implementation is not
adjusted to make these pass.

There is exactly one fake: the canonical ``rentguard/fake.py``. A second, private double drifts
from the provider surface it imitates, and then a test passes against a world the tool does not
live in; its ``add``/``advance``/``disappear``/``crash_loop``/``put`` cover every machine this
suite needs. Arguments come from ``cli.build_parser()`` for the same reason: a hand-built
``Namespace`` breaks the moment the CLI grows a flag, and then the test measures its own fixture.

Note: this suite makes its own scratch directories under the workspace instead of
``tempfile.TemporaryDirectory``, because a ``mkdtemp`` directory (mode 0700) cannot be listed or
removed by the confined Windows token used to run tests here.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import threading
import unittest
import uuid
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sys

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))

from rentguard import cli, guard  # noqa: E402
from rentguard.cli import _instance_name, _remote_command, _temp_tar  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, iso, now_utc  # noqa: E402

T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
TMPROOT = WORKSPACE / ".assay-tmp"


# --------------------------------------------------------------------- fixtures

def cli_args(*argv: str) -> Namespace:
    """Arguments exactly as the real parser builds them, including flags added since this wrote."""
    return cli.build_parser().parse_args(list(argv))


def make_state_dir(case: unittest.TestCase) -> Path:
    d = TMPROOT / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    case.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


class MoneyCase(unittest.TestCase):
    def make_cfg(self, *, cap: float = 10.0, per_run: float = 5.0,
                 idle: float = 10.0, max_hours: float = 6.0) -> Config:
        return Config(
            budget=Budget(cap_usd=cap, per_run_cap_usd=per_run,
                          max_hours_per_run=max_hours, idle_minutes=idle),
            state_dir=self.state_dir,
            # The canonical fake's catalog holds one GPU; without this the real preference list
            # matches nothing and `up` fails for a reason the test is not about.
            runpod={"gpu_preference": ["Fake GPU 24GB"]},
        )

    def setUp(self) -> None:
        self.state_dir = make_state_dir(self)
        self.cfg = self.make_cfg()
        self.ledger = Ledger(self.cfg.ledger_path)
        self.write_state({})

    # ---------------------------------------------------------------- helpers

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def invoke(self, func, args, provider) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
             mock.patch.object(cli, "_provider", lambda cfg: provider), \
             mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
             contextlib.redirect_stdout(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except cli.ProviderError:
                code = 1
        return code, out.getvalue()

    def machine(self, instance_id: str = "pod-A", *, rate: float = 2.0, uptime: float = 3600.0,
                status: str = "RUNNING", ports: bool = True,
                gpu_util: float = 50.0) -> FakeProvider:
        """The canonical fake with one machine on it, shaped like the scenario at hand."""
        fake = FakeProvider()
        fake.add(instance_id, rate_usd_per_hour=rate, status=status, uptime_seconds=uptime,
                 ports=({"22": "tcp", "8888": "http"} if ports else None), gpu_util=gpu_util)
        return fake

    def close_old_run(self, *, hours: float, rate: float = 2.0,
                      instance_id: str = "pod-old") -> dict:
        run = self.ledger.open(purpose="old", provider="fake", instance_id=instance_id,
                               gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate, started_at=T0)
        return self.ledger.close(note="down", stopped_at=T0 + timedelta(hours=hours))

    def open_run(self, *, hours_ago: float, rate: float = 2.0,
                 instance_id: str = "pod-A") -> dict:
        return self.ledger.open(purpose="probe", provider="fake", instance_id=instance_id,
                                gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                                started_at=now_utc() - timedelta(hours=hours_ago))


# ====================================================================== PATH 1
# the cap is exceeded

class CapIsExceeded(MoneyCase):
    def test_up_refuses_when_the_open_run_has_already_crossed_the_cap(self):
        """Sequence: open a run; let its live cost pass the cap ($2/h x 100h = $200 > $10);
        then run `rentguard up` again with no pod in state.

        Effect required: `up` must not rent anything, and must not report success.
        """
        self.open_run(hours_ago=100.0)
        provider = FakeProvider()

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "second"), provider)

        self.assertEqual(
            provider.created, [],
            "up rented a new machine while an open run had already accrued $200 of a $10 cap; "
            f"the pre-rent check reads only closed rows. output:\n{out}")
        self.assertNotEqual(code, 0, f"up reported success while over the cap. output:\n{out}")

    def test_a_run_that_was_never_recorded_is_stopped_by_the_guard(self):
        """Sequence: a machine is billing ($2/h) with no open ledger row (lost row, crashed
        `up`, hand-made instance); cap $1. A guard is started.

        Effect required: the guard stops it, or refuses to run. It must not spin and return
        its iteration limit while the money burns.
        """
        self.cfg = self.make_cfg(cap=1.0, per_run=1.0)
        provider = self.machine("pod-A", uptime=1800)   # $1 already spent

        reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A",
                             interval=0.0, max_iterations=2, log=lambda *a: None)

        self.assertIn(
            ("pod-A", "stop"), provider.actions,
            f"guard never stopped a machine burning $2/h against a $1 cap because it had no "
            f"ledger row; reason was {reason!r}, actions {provider.actions!r}")


# ====================================================================== PATH 2
# per-run cap before the total cap

class PerRunCap(MoneyCase):
    def test_guard_stops_on_the_per_run_cap_before_the_total_cap(self):
        """Sequence: open a run 3h ago at $2/h ($6), per-run cap $5, total cap $10; guard runs.

        Effect: the instance is stopped and the row closed at ~$6, and the reason names the
        per-run cap (the binding one), not the total cap.
        """
        self.open_run(hours_ago=3.0)
        provider = self.machine("pod-A", uptime=3 * 3600)

        reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A",
                             interval=0.0, max_iterations=2, log=lambda *a: None)

        self.assertIn(("pod-A", "stop"), provider.actions, f"not stopped: {reason!r}")
        self.assertIn("per-run cap", reason, f"wrong reason: {reason!r}")
        closed = self.ledger.open_run()
        self.assertIsNone(closed, "the run was stopped but its ledger row stayed open")


class TotalCap(MoneyCase):
    def test_guard_stops_on_the_total_cap_across_runs(self):
        """Sequence: a closed run of $9, then a new run 1h ago at $2/h ($11 > $10 cap).

        Effect: stopped, row closed, reason names the total cap.
        """
        self.close_old_run(hours=4.5, rate=2.0)  # $9 closed
        self.open_run(hours_ago=1.0)
        provider = self.machine("pod-A", uptime=3600)

        reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A",
                             interval=0.0, max_iterations=2, log=lambda *a: None)

        self.assertIn(("pod-A", "stop"), provider.actions, f"not stopped: {reason!r}")
        self.assertIn("total cap", reason, f"wrong reason: {reason!r}")
        self.assertIsNone(self.ledger.open_run())


# ====================================================================== PATH 3
# instance stopped by someone else while a run is open

class StoppedElsewhere(MoneyCase):
    def test_stopped_elsewhere_closes_the_run_and_records_the_cost(self):
        """Sequence: run open for 1h at $2/h; someone stops the pod in the provider console
        (status EXITED, still visible). The guard sees it.

        Effect: the row is closed with the real wall-clock cost, and the guard does not try to
        stop an already-stopped pod.
        """
        self.open_run(hours_ago=1.0)
        provider = self.machine("pod-A", status="EXITED", uptime=3600)

        reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A",
                             interval=0.0, max_iterations=2, log=lambda *a: None)

        self.assertIn("stopped", reason, f"unexpected guard result: {reason!r}")
        self.assertEqual(provider.actions, [], "the guard tried to stop a pod already stopped")
        self.assertIsNone(self.ledger.open_run(), "the run stayed open after the pod exited")
        total = self.ledger.total_usd()
        self.assertAlmostEqual(total, 2.0, delta=0.05,
                               msg=f"cost of the exited run was recorded as ${total}, not ~$2.00")


# ====================================================================== PATH 4
# two runs opened by accident

class TwoRunsOpened(MoneyCase):
    def test_a_second_open_run_is_refused_when_checked_sequentially(self):
        """Sequence: open a run, then open another in the same process.

        Effect: the second is refused and exactly one row is open.
        """
        self.open_run(hours_ago=0.1, instance_id="pod-one")
        with self.assertRaises(RuntimeError):
            self.open_run(hours_ago=0.1, instance_id="pod-two")

    def test_two_concurrent_openers_cannot_both_open_a_run(self):
        """Sequence: `up` and `adopt` run at the same time (two terminals, a retrying Bot).
        The check-then-append in Ledger.open is not atomic.

        The interleaving is forced deterministically with a barrier inside open_run, because
        that is the schedule the OS is free to produce. Effect required: exactly one open row.
        """
        real_open_run = Ledger.open_run
        barrier = threading.Barrier(2)
        barrier_broken: list[str] = []

        def racy_open_run(ledger_self):
            result = real_open_run(ledger_self)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                barrier_broken.append("timeout")
            return result

        results: list[str] = []

        def opener() -> None:
            ledger = Ledger(self.cfg.ledger_path)
            try:
                with mock.patch.object(Ledger, "open_run", racy_open_run):
                    ledger.open(purpose="race", provider="fake", instance_id="i",
                                gpu_id="g", rate_usd_per_hour=1.0, started_at=now_utc())
                results.append("opened")
            except RuntimeError:
                results.append("refused")

        threads = [threading.Thread(target=opener) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        open_rows = [r for r in self.ledger.entries() if not r.get("stopped_at")]
        self.assertEqual(
            len(open_rows), 1,
            f"concurrent opens produced {len(open_rows)} open rows ({results}), "
            f"barrier={barrier_broken}: the open-run check is check-then-act")


# ====================================================================== PATH 5
# the ledger written by an interrupted process

class InterruptedWrite(MoneyCase):
    def test_a_truncated_ledger_line_is_reported_not_silently_dropped(self):
        """Sequence: a valid $2.00 run is in the ledger; a second process is killed mid-append
        and leaves a partial JSON line (with money in it). Then `doctor` runs.

        Effect required: the unreadable line is surfaced; spend must not disappear in silence.
        """
        self.close_old_run(hours=1.0, rate=2.0)  # a valid closed $2.00 row
        with self.ledger.path.open("a", encoding="utf-8") as fh:
            fh.write('{"run_id": "lost-run", "estimated_usd": 1.50, "stopped_at": "2026')

        self.assertAlmostEqual(self.ledger.total_usd(), 2.0, places=6)  # the valid row survives
        code, out = self.invoke(cli.cmd_doctor, cli_args("doctor"), FakeProvider())
        lowered = out.lower()
        self.assertTrue(
            any(w in lowered for w in ("corrupt", "unreadable", "unpars", "truncat", "malformed")),
            "a partial ledger line was dropped in silence: doctor reports "
            f"${self.ledger.total_usd():.2f} and never mentions the lost line.\noutput:\n{out}")

    def test_a_truncated_state_file_does_not_rent_a_second_machine(self):
        """Sequence: state.json is half-written (interrupted `_write_state`) while pod-A is
        still RUNNING and billing; the operator runs `up` for the next job.

        Effect required: no second machine is rented on top of a live one, and `up` does not
        report success. Forgetting a live instance is how two bills happen.
        """
        self.cfg.state_path.write_text('{"instance_id": "pod-A", "provider": "runp',
                                       encoding="utf-8")
        provider = self.machine("pod-A", uptime=7200)

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "second"), provider)

        self.assertEqual(
            provider.created, [],
            "a truncated state file made the tool forget a live, billing instance and rent a "
            f"second one. output:\n{out}")
        self.assertNotEqual(code, 0, f"up reported success with a live untracked pod. output:\n{out}")


# ====================================================================== PATH 6
# guard whose state file is stale

class StaleGuard(MoneyCase):
    def test_a_stale_heartbeat_is_reported_as_not_watching(self):
        """Sequence: the guard died 30 minutes ago (heartbeat file 30 min old); `status` runs.

        Effect: the operator is told nothing is watching.
        """
        self.write_state({"instance_id": "pod-A"})
        self.open_run(hours_ago=0.1)
        self.cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.heartbeat_path.write_text(
            json.dumps({"at": iso(now_utc() - timedelta(minutes=30))}), encoding="utf-8")
        provider = self.machine("pod-A", uptime=600)

        code, out = self.invoke(cli.cmd_status, cli_args("status"), provider)

        lowered = out.lower()
        self.assertTrue(any(w in lowered for w in ("probably dead", "stale", "not watching")),
                        f"a 30-minute-old heartbeat was reported as healthy.\noutput:\n{out}")


# ====================================================================== PATH 7
# a machine that reports RUNNING with zero uptime

class CrashLoop(MoneyCase):
    def test_run_refuses_to_upload_to_a_crash_looping_pod(self):
        """Sequence: the pod reports RUNNING with uptime 0 and null ports (crash loop);
        `rentguard run -- python train.py` is issued.

        Effect: no ssh command is executed and the command fails.
        """
        self.write_state({"instance_id": "pod-A"})
        self.open_run(hours_ago=0.1)
        provider = self.machine("pod-A", uptime=0.0, ports=False)
        args = cli_args("run", "--", "python", "train.py")

        with mock.patch("rentguard.cli.subprocess.call") as ssh_call:
            code, out = self.invoke(cli.cmd_run, args, provider)

        self.assertFalse(ssh_call.called,
                         f"uploaded to a crash-looping pod.\noutput:\n{out}")
        self.assertNotEqual(code, 0, f"crash loop was reported as success.\noutput:\n{out}")

    def test_status_marks_a_running_pod_with_zero_uptime_as_not_usable(self):
        """Sequence: the pod reports RUNNING, uptime 0, null ports; `status` runs.

        Effect: the output says the machine is not usable (crash loop), because RUNNING alone
        is a claim, not a state.
        """
        self.write_state({"instance_id": "pod-A"})
        self.open_run(hours_ago=0.1)
        provider = self.machine("pod-A", uptime=0.0, ports=False)

        code, out = self.invoke(cli.cmd_status, cli_args("status"), provider)

        lowered = out.lower()
        self.assertTrue(any(w in lowered for w in ("not usable", "unusable", "crash")),
                        f"a crash-looping pod was reported as a usable RUNNING machine.\n"
                        f"output:\n{out}")


# ====================================================================== state drift
# both directions. The ledger is the record; the state file is a cache.

class StateDrift(MoneyCase):
    def test_doctor_flags_a_ledger_row_open_against_an_instance_that_is_gone(self):
        """Sequence: a run is open for pod-Z; pod-Z no longer exists on the account; `doctor`.

        Effect: doctor reports drift (non-zero), so a script or MCP client sees a failure
        instead of a healthy-looking report.
        """
        self.open_run(hours_ago=1.0, instance_id="pod-Z")
        provider = FakeProvider()

        code, out = self.invoke(cli.cmd_doctor, cli_args("doctor"), provider)

        self.assertNotEqual(
            code, 0,
            "doctor detected the orphaned open row in its text but still exited 0, so a script "
            "or MCP client sees isError=false: the drift is advisory, not a failure.\n"
            f"output:\n{out}")

    def test_doctor_flags_a_tracked_instance_with_no_ledger_row(self):
        """Sequence: state records pod-A; pod-A is RUNNING (crash loop, uptime 0, no ports);
        the ledger has no row for it. `doctor` runs.

        Effect: doctor reports drift (non-zero); matching the state file is not the same as
        having a ledger row.
        """
        self.write_state({"instance_id": "pod-A"})
        provider = self.machine("pod-A", uptime=0.0, ports=False)

        code, out = self.invoke(cli.cmd_doctor, cli_args("doctor"), provider)

        self.assertNotEqual(
            code, 0,
            "doctor detected the missing ledger row in its text but still exited 0, so a script "
            "or MCP client sees isError=false: the drift is advisory, not a failure.\n"
            f"output:\n{out}")

    def test_doctor_is_quiet_after_a_clean_down(self):
        """Sequence: rent pod-A, work, `down` it. The end state is an EXITED instance with a closed
        ledger row; `doctor` runs.

        Effect: doctor exits 0 and reports no drift. A finished rental is not drift, and if it were
        reported as drift every clean stop would train the operator to ignore the warning.
        """
        self.write_state({"instance_id": "pod-A"})
        provider = self.machine("pod-A", status="EXITED", uptime=7200)
        self.close_old_run(hours=2.0, rate=2.0, instance_id="pod-A")

        code, out = self.invoke(cli.cmd_doctor, cli_args("doctor"), provider)

        self.assertEqual(
            code, 0,
            "doctor called a clean rental an error: an EXITED instance with a closed row is the "
            f"correct end state after `down`.\noutput:\n{out}")
        lowered = out.lower()
        self.assertNotIn("error", lowered,
                         f"doctor reported an error after a clean down.\noutput:\n{out}")
        self.assertNotIn("drift", lowered,
                         f"doctor reported drift after a clean down.\noutput:\n{out}")


# ====================================================================== billing start
class BillingStart(MoneyCase):
    def test_up_backdates_a_run_for_a_machine_already_billing(self):
        """Sequence: pod-A has been RUNNING for 2h at $2/h ($4 spent) and the ledger has no row
        (lost append, first run after a crash). The operator runs `up`.

        Effect: the opened row is backdated to the real start, so the $4 is recorded, not
        hidden. Starting the clock at zero is how overspend becomes invisible.
        """
        self.write_state({"instance_id": "pod-A"})
        provider = self.machine("pod-A", uptime=7200)

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "recover"), provider)

        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"up did not open a ledger row.\noutput:\n{out}")
        started = datetime.fromisoformat(run["started_at"])
        expected = now_utc() - timedelta(hours=2)
        drift = abs((started - expected).total_seconds())
        self.assertLess(
            drift, 120,
            f"the row was opened at {run['started_at']} instead of ~2h earlier ({iso(expected)}): "
            f"${self.ledger.total_usd():.2f} recorded against $4.00 already spent on pod-A")


# ====================================================================== drift hides money
# An open row whose instance has vanished used to be invisible to `status` ("no instance
# recorded, $0.00") and therefore also to the cap check. These are the repro plus the money
# consequence underneath it.

class DriftHidesMoney(MoneyCase):
    def setUp(self) -> None:
        super().setUp()
        # The recorded link survives, but the provider no longer shows the machine (a crash, a
        # provider-side termination, someone else's console click).
        self.write_state({"instance_id": "drift-demo", "provider": "fake"})
        self.open_run(hours_ago=3.0, rate=2.0, instance_id="drift-demo")

    def test_status_reports_an_open_row_whose_instance_is_gone(self):
        """Sequence: one open ledger row for instance drift-demo, started 3h ago at $2/h ($6
        accrued); the provider shows no such instance; `rentguard status`.

        Effect required: the open row and its accrued cost are the headline, and status does not
        claim nothing is recorded. Silence here is how an unattended machine's money hides.
        """
        provider = FakeProvider()

        code, out = self.invoke(cli.cmd_status, cli_args("status"), provider)

        lowered = out.lower()
        self.assertNotIn("no instance recorded", lowered,
                         f"status said no instance while a row was open.\noutput:\n{out}")
        self.assertIn("drift-demo", out, f"status does not name the drifted instance:\n{out}")
        self.assertTrue("drift" in lowered or "gone" in lowered or "no longer" in lowered,
                        f"status does not say the instance is gone.\noutput:\n{out}")
        self.assertIn("6.0", out,
                      f"status does not report the $6 accrued on the open row.\noutput:\n{out}")

    def test_status_does_not_erase_the_recorded_instance_id_on_a_404(self):
        """Sequence: as above; after `status`, read the state file.

        Effect required: the recorded id is the only link back to the open row; a read command
        must not delete it.
        """
        provider = FakeProvider()
        self.invoke(cli.cmd_status, cli_args("status"), provider)

        state = json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state.get("instance_id"), "drift-demo",
                         "status erased the only link from the state file to the open ledger row")

    def test_up_after_the_instance_vanished_records_the_new_machine(self):
        """Sequence: the drifted open row above; the operator runs `up --purpose second`, which
        rents a replacement machine.

        Effect required: the row that is open after `up` names the machine that is now billing.
        An open stale row must not swallow the new instance: that is a live machine with no ledger
        row and a cap check reading $0.00.
        """
        provider = FakeProvider()

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "second"), provider)

        self.assertTrue(provider.created, f"up rented nothing:\noutput:\n{out}")
        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"no open row after up.\noutput:\n{out}")
        live = [p.get("id") for p in provider.list_pods()
                if p.get("status") not in ("EXITED", "TERMINATED")]
        self.assertIn(
            run["instance_id"], live,
            f"the open row names {run['instance_id']!r} but the live instance is "
            f"{live!r}: the replacement machine is billing with no row, and the "
            f"stale row keeps the cap check reading low.\noutput:\n{out}")


# ====================================================================== M1: the fake itself
# The fake provider is the only way these money paths can be tested without spending, so the
# documented offline flow must work end to end.

class ForgeFakeProviderFlow(MoneyCase):
    def test_the_documented_offline_flow_up_then_status_succeeds(self):
        """Sequence: `RENTGUARD_PROVIDER=fake rentguard up --purpose m1` (here: the CLI with
        rentguard.fake.FakeProvider, one shared instance, no network, no money).

        Effect required: `up` succeeds, records a ledger row for the instance it created, and a
        later `status` sees that instance. If this fails, no money path is testable for free.
        """
        provider = FakeProvider(rate_usd_per_hour=0.13)
        self.cfg = self.make_cfg(cap=5.0, per_run=2.0)
        self.cfg.provider = "fake"

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "m1"), provider)

        self.assertEqual(code, 0, f"up with the fake provider failed.\noutput:\n{out}")
        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"up rented an instance without a ledger row.\noutput:\n{out}")
        code2, out2 = self.invoke(cli.cmd_status, cli_args("status"), provider)
        self.assertIn(str(run["instance_id"]), out2,
                      f"status does not see the instance up just rented.\noutput:\n{out2}")


# ====================================================================== artefacts
# README: "Artefacts come off the machine as the run finishes — not at the end of the day."
# One rule (Gilbert, 2026-10-14): if [artefacts].pull is non-empty, `run` pulls when each run
# finishes and `down` pulls before it stops. `on_run` is deleted — there is no second switch.
# The two tests below cover the `run` half of that rule; the `down` half is a separate commit.

class ArtefactPull(MoneyCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.artefacts.pull = ["results/*.json"]
        self.cfg.artefacts.local_dir = str(self.state_dir / "artefacts")
        self.write_state({"instance_id": "fake-1", "provider": "fake"})
        self.open_run(hours_ago=0.1, instance_id="fake-1")
        self.fake = FakeProvider(gpu_id="Fake GPU 24GB")
        self.fake.add("fake-1", gpu_id="Fake GPU 24GB")
        self.fake.advance(60)  # a positive uptime: the machine is usable
        self.fake.put("results/out.json", '{"ok": true}', instance_id="fake-1")

    def pulled_path(self) -> Path:
        return Path(self.cfg.artefacts.local_dir) / "results" / "out.json"

    def test_run_pulls_artefacts_as_the_run_finishes_with_a_plain_config(self):
        """Sequence: a config with [artefacts].pull set (the README's example, which never mentions
        on_run); the machine holds results/out.json; `run -- python train.py` succeeds.

        Effect required: the file is on local disk when the command returns. If it is not, the next
        `down` or container restart loses it — the one outcome the tool exists to prevent.
        """
        self.fake.command_runner = lambda argv, remote: 0
        args = cli_args("run", "--", "python", "train.py")

        code, out = self.invoke(cli.cmd_run, args, self.fake)

        self.assertEqual(code, 0, f"run failed.\noutput:\n{out}")
        self.assertTrue(
            self.pulled_path().exists(),
            f"run finished and {self.pulled_path()} is not on local disk: with the config the README "
            f"shows, nothing is pulled and the artefacts die with the container.\noutput:\n{out}")

    def test_run_pulls_artefacts_even_when_the_command_fails(self):
        """Sequence: as above, but the remote command exits 1 (a crash mid-training).

        Effect required: a failed run is exactly when the logs and partial results are wanted, so
        they must still be pulled as the run finishes.
        """
        self.fake.command_runner = lambda argv, remote: 1
        args = cli_args("run", "--", "python", "train.py")

        self.invoke(cli.cmd_run, args, self.fake)

        self.assertTrue(self.pulled_path().exists(),
                        "a run that exited non-zero pulled nothing: the crash logs are gone")

    def test_pull_machinery_works_when_on_run_is_explicitly_set(self):
        """Control: with on_run = true the file does arrive, so a failure above is about the
        default, not about a broken pull path."""
        self.cfg.artefacts.on_run = True
        self.fake.command_runner = lambda argv, remote: 0
        args = cli_args("run", "--", "python", "train.py")

        code, out = self.invoke(cli.cmd_run, args, self.fake)

        self.assertTrue(self.pulled_path().exists(), f"pull path itself is broken.\noutput:\n{out}")
        self.assertEqual(self.pulled_path().read_bytes(), b'{"ok": true}')


# ====================================================================== the remote command
# M2 paid $0.0091 to find this: `run` quoted a compound command the caller's own shell had already
# quoted, so bash received one word and executed nothing. The single-argument form is the one the
# README uses; both must reach the remote shell intact.

class RemoteCommandForm(MoneyCase):
    def _capture(self, argv: list[str]) -> tuple[int, str, str]:
        provider = self.machine("pod-A", uptime=600)
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.open_run(hours_ago=0.1, instance_id="pod-A")
        seen: dict[str, str] = {}
        provider.command_runner = lambda av, remote: (seen.update(remote=remote), 0)[1]
        code, out = self.invoke(cli.cmd_run, cli_args(*argv), provider)
        return code, out, seen.get("remote", "")

    def test_a_compound_command_reaches_the_remote_shell_unquoted(self):
        """Sequence: `rentguard run -- 'mkdir -p results; hostname > results/m2.json'` — the caller's
        shell has already made this one argument.

        Effect: the remote command keeps the compound line as the shell wrote it, so the remote shell
        parses the `;` and `>` itself. Quoting it again sends one word, and bash looks for a program
        named `mkdir -p results; hostname ...` — which is exactly what M2 did.
        """
        compound = "mkdir -p results; hostname > results/m2.json"
        code, out, remote = self._capture(["run", "--", compound])

        self.assertEqual(code, 0, f"run failed.\noutput:\n{out}")
        self.assertIn(compound, remote,
                      f"the compound line was mangled before it reached the shell:\n{remote!r}")

    def test_the_remote_command_helper_passes_one_argument_through(self):
        """The rule, stated directly: one argument is the shell's own text, several are separate
        words that must each be quoted."""
        self.assertEqual(_remote_command(["python train.py"]), "python train.py")
        self.assertEqual(_remote_command(["echo", "hello world"]), "echo 'hello world'")
        self.assertEqual(_remote_command(["a;b", "c"]), "'a;b' c")

    def test_separate_arguments_are_quoted_individually(self):
        """Sequence: `run -- python train.py` (several arguments).

        Effect: each argument stays one word on the remote side, so a quote in an argument is data
        and the program name is not glued to the shell's punctuation.
        """
        code, out, remote = self._capture(["run", "--", "python", "train.py"])

        self.assertEqual(code, 0, f"run failed.\noutput:\n{out}")
        self.assertIn("python train.py", remote, f"arguments were mangled: {remote!r}")


# ====================================================================== GPU fallback
# M2 also found that a catalogue entry is not capacity: the RTX 3070 showed available and create
# refused it. `up` must walk the preference list until the provider accepts one.

class GpuFallback(MoneyCase):
    def _prefer(self, *gpus: str, unavailable: set[str] | None = None,
                offer_unavailable: bool = False) -> FakeProvider:
        self.cfg.runpod = {"gpu_preference": list(gpus)}
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.unavailable_gpus = set(unavailable or ())
        # Model the M2 shape: the catalogue offers the GPU, `create` refuses it.
        provider.offer_unavailable = offer_unavailable
        return provider

    def test_up_falls_back_when_the_first_catalog_hit_is_refused(self):
        """Sequence: preferences [A, B]; the catalogue offers both, but `create(A)` is refused the
        way RunPod refuses stale stock, and `create(B)` is accepted.

        Effect: a machine is rented, and it is B — the next preference — not a dead end. Without the
        walk, the whole tool is unusable whenever the first catalogue hit is stale.
        """
        provider = self._prefer("GPU-A", "GPU-B", unavailable={"GPU-A"}, offer_unavailable=True)

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "fallback"), provider)

        self.assertEqual(code, 0, f"up died instead of falling back.\noutput:\n{out}")
        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"up rented nothing and opened no row.\noutput:\n{out}")
        pod = provider.get(run["instance_id"]) or {}
        self.assertEqual((pod.get("gpu") or {}).get("id"), "GPU-B",
                         f"up did not fall back to the next preference; it rented "
                         f"{(pod.get('gpu') or {}).get('id')!r}.\noutput:\n{out}")
        self.assertIn("GPU-A was refused", out, f"the refusal was not reported:\n{out}")

    def test_up_reports_every_refusal_when_none_is_available(self):
        """Sequence: preferences [A, B], and the provider refuses both.

        Effect: up exits non-zero and names both, so the operator sees which cards were tried rather
        than one generic failure.
        """
        provider = self._prefer("GPU-A", "GPU-B",
                                unavailable={"GPU-A", "GPU-B", "Fake GPU 24GB"})

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "none"), provider)

        self.assertNotEqual(code, 0, f"up reported success with nothing rentable.\noutput:\n{out}")
        self.assertIn("GPU-A", out, f"the first refusal is not named:\n{out}")
        self.assertIn("GPU-B", out, f"the second refusal is not named:\n{out}")
        self.assertEqual(self.ledger.open_run(), None,
                         "a row was opened for a machine that was never created")


# ====================================================================== a pull must not trap money
# Found on the real account: `down` pulled before stopping, the pull crashed on an unwritable temp
# dir, and the exception skipped the stop — leaving an instance billing because a tar stream failed.

class DownSurvivesAPullFailure(MoneyCase):
    def _live_down(self, provider):
        self.cfg.artefacts.pull = ["results/*.json"]
        self.cfg.artefacts.local_dir = str(self.state_dir / "artefacts")
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.open_run(hours_ago=0.1, instance_id="pod-A")
        return self.invoke(cli.cmd_down, cli_args("down"), provider)

    def test_down_stops_the_machine_when_the_pull_crashes(self):
        """Sequence: a live instance, artefacts configured, and the pull raises instead of returning.

        Effect: the machine is stopped and the row closed anyway (exit non-zero). A crash between
        "pull" and "stop" is how a machine keeps billing with nobody watching it.
        """
        provider = self.machine("pod-A", uptime=600)
        with mock.patch.object(cli, "cmd_pull", side_effect=RuntimeError("no temp dir")):
            code, out = self._live_down(provider)

        self.assertIn(("pod-A", "stop"), provider.actions,
                      f"down skipped the stop after the pull raised.\noutput:\n{out}")
        self.assertIsNone(self.ledger.open_run(),
                          f"the row stayed open after a crashed pull.\noutput:\n{out}")
        self.assertNotEqual(code, 0, f"a crashed pull was reported as a clean stop.\noutput:\n{out}")

    def test_down_stops_the_machine_when_the_pull_returns_failure(self):
        """Same, for a pull that returns non-zero rather than raising."""
        provider = self.machine("pod-A", uptime=600)
        with mock.patch.object(cli, "cmd_pull", return_value=1):
            code, out = self._live_down(provider)

        self.assertIn(("pod-A", "stop"), provider.actions, f"not stopped.\noutput:\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"row stayed open.\noutput:\n{out}")
        self.assertNotEqual(code, 0)

    def test_the_pull_archive_is_built_where_it_is_told_to(self):
        """The system temp dir is not always writable; the archive must land in the given directory."""
        target = self.state_dir / "pull-tmp"
        with _temp_tar(target) as (handle, archive):
            handle.write(b"data")
            self.assertTrue(Path(archive).is_relative_to(target),
                            f"archive {archive} was not built under {target}")
            self.assertTrue(Path(archive).exists(), "the archive file was not created")


# ====================================================================== one account, one ledger
# Also found on the real account: a pod leased from a different state dir carried the fixed default
# name, so nothing tied it to any ledger. The name now encodes the project root.

class InstanceNaming(MoneyCase):
    def test_the_name_is_derived_from_the_project_root(self):
        with mock.patch.dict(os.environ, {"RENTGUARD_ROOT": r"C:\project-one"}):
            first = _instance_name(self.cfg)
        with mock.patch.dict(os.environ, {"RENTGUARD_ROOT": r"C:\project-two"}):
            second = _instance_name(self.cfg)

        self.assertTrue(first.startswith("rentguard-"), first)
        self.assertNotEqual(first, second,
                            "two different project roots produced the same instance name, so a "
                            "rental from a second state dir is invisible to the first")
        self.assertNotIn("workbench", first)

    def test_an_explicit_name_in_the_config_wins(self):
        self.cfg.runpod = {"name": "my-baseline-run"}
        self.assertEqual(_instance_name(self.cfg), "my-baseline-run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
