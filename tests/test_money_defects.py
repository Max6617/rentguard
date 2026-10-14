"""The three money defects the charter records, each with the control that stops a hollow fix.

Red first: these are written before the fixes, and every one of them must fail on the current code.
A control is part of every case, because a "fix" that simply never stops a machine, or never
reports success, would pass a defect-only test.

1. **W4 / F-5 — the guard stops a freshly started machine as idle.** The provider's per-GPU idle
   seconds are compared against the idle window without being clamped to the machine's own uptime,
   so a pod 32 s old that reports 30 min of GPU idle is stopped at its first sample. Control: a
   machine that really has been idle past the window is still stopped.
2. **`up` reports success on one favourable reading.** `wait_running` returns the first payload that
   looks alive; a pod that is alive for exactly one sample and crash-looping after it is reported as
   a successful rental and gets a ledger row. Driven through `RunPod._request`, the one seam that
   touches the network. Control: a machine that stays alive is still reported as a successful rental.
3. **F-4 — `--dry-run` rents what it says it did not rent.** Asserted on the transport: the number of
   `POST /v2/pods` calls, not the sentence printed afterwards. Control: a real `up` does create.
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

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.runpod import RunPod  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cfg_for(root: Path, *, idle_minutes: float = 10.0, cap: float = 25.0,
            per_run: float = 5.0) -> Config:
    cfg = Config(budget=Budget(cap_usd=cap, per_run_cap_usd=per_run, idle_minutes=idle_minutes),
                 state_dir=root)
    cfg.provider = "fake"
    cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
    return cfg


def pod_payload(*, status: str = "RUNNING", uptime: float = 3600.0, ports: bool = True,
                gpu_idle: float | None = None, util: float = 0.0, cost: float = 1.0) -> dict:
    gpu: dict = {"util": util, "memoryUtil": util}
    if gpu_idle is not None:
        gpu["idle"] = gpu_idle   # RunPod reports seconds since the GPU last did work
    return {
        "id": "pod-1", "status": status, "cost": cost, "gpu": {"id": "Fake GPU 24GB"},
        "runtime": {"uptime": uptime, "ports": {"22": "tcp"} if ports else None, "gpus": [gpu]},
        "ssh": {"direct": {"host": "127.0.0.1", "port": 2222, "username": "root"}},
    }


# ============================================================== 1. W4 / F-5 — the idle clamp

class FreshMachineIsNotIdle(unittest.TestCase):
    """The guard must never stop a machine younger than its own idle window."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root, idle_minutes=5.0)
        self.ledger = Ledger(self.cfg.ledger_path)

    def decide_for(self, pod: dict, *, local_idle: float = 0.0) -> tuple:
        snap = guard.snapshot(pod, self.cfg, self.ledger, idle_for_minutes=local_idle,
                              idle_since_start_minutes=0.0)
        return snap, guard.decide(snap)

    def test_a_thirty_second_old_machine_is_not_stopped_as_idle(self):
        """Sequence: a pod that started 32 s ago and whose provider-reported GPU idle is 30 min
        (the shape a real pod showed); idle_minutes = 5. The guard takes its first look.

        Effect required: no stop. The machine has done nothing yet; stopping it here spends the
        rental on nothing, which is the one way the guard itself loses money.
        """
        pod = pod_payload(uptime=32, gpu_idle=1800, util=0.0)

        snap, reason = self.decide_for(pod)

        self.assertIsNone(
            reason,
            f"the guard stopped a {snap.uptime_hours * 60:.1f}-min-old machine as idle: provider "
            f"idle {snap.max_idle_minutes} min, local idle {snap.idle_for_minutes} min, window "
            f"{snap.idle_minutes} min -> {reason!r}")

    def test_a_machine_idle_past_the_window_is_still_stopped(self):
        """Control for the clamp: a machine that has been up long enough and whose GPU idle really
        exceeds the window must still be stopped, or the "fix" is to never stop anything."""
        pod = pod_payload(uptime=3600, gpu_idle=1800, util=0.0)   # 1 h old, 30 min idle, window 5

        snap, reason = self.decide_for(pod)

        self.assertIsNotNone(reason, "a genuinely idle machine was left billing")
        self.assertIn("idle", reason, f"the stop reason does not name idle: {reason!r}")

    def test_the_locally_observed_idle_counter_still_stops(self):
        """Second control: the guard's own zero-utilisation counter is the reading that must keep
        working. A long-lived pod with five consecutive idle samples is stopped as before."""
        pod = pod_payload(uptime=3600, gpu_idle=0, util=0.0)

        snap, reason = self.decide_for(pod, local_idle=7.0)   # one sample more than the window

        self.assertIsNotNone(reason, "the local idle counter stopped deciding anything")
        self.assertIn("idle", reason, f"the stop reason does not name idle: {reason!r}")


# ============================================================== 2. one alive sample is not proof

class UpNeedsMoreThanOneAliveSample(unittest.TestCase):
    """`up` must not call a rental successful on the strength of a single favourable reading."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)
        self.ledger = Ledger(self.cfg.ledger_path)

    # ---------------------------------------------------------------- the scripted transport

    def transport(self, samples: list[dict]) -> tuple[RunPod, list[tuple[str, str]]]:
        """A real `RunPod` whose `_request` is a script: the network seam, not the provider.

        Everything above the seam — `candidates`, `create`, `wait_running`, `get` — is the real
        code, so a test that counts a `create` call counts the one the tool would really make.
        """
        state = {"gets": 0}
        calls: list[tuple[str, str]] = []

        def script(method: str, path: str, body=None, params=None, retries: int = 3):
            calls.append((method, path))
            if path == "/v2/catalog/gpus":
                return {"gpus": [{"id": "Fake GPU 24GB", "memory": 24, "secure": "High",
                                  "price": {"secure": 1.0}}]}
            if method == "POST" and path == "/v2/pods":
                return {"id": "pod-1", "status": "PROVISIONING", "cost": 1.0}
            if method == "POST" and path.endswith("/action"):
                return {}
            if method == "GET" and path == "/v2/pods/pod-1":
                if state["gets"] >= len(samples):
                    # The machine is gone before it ever settled: the provider says so.
                    raise ProviderError("GET /v2/pods/pod-1 -> 500: the machine disappeared")
                sample = samples[state["gets"]]
                state["gets"] += 1
                return sample
            return {}

        provider = RunPod("test-key-not-real")
        provider._request = script   # type: ignore[method-assign]
        return provider, calls

    def invoke_up(self, provider: RunPod, *argv: str) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "defect", *argv])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
             mock.patch.object(cli, "_provider", lambda cfg: provider), \
             mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
             mock.patch("rentguard.runpod.time.sleep", lambda *_: None), \
             contextlib.redirect_stdout(out):
            try:
                code = cli.cmd_up(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except ProviderError:
                code = 1
        return code, out.getvalue()

    # ---------------------------------------------------------------- the cases

    def test_up_does_not_report_success_after_one_alive_sample(self):
        """Sequence: the pod reports RUNNING, uptime 3 min, ports set (one usable reading), then
        RUNNING with uptime 0 and null ports for every later reading, and finally it is gone.

        Effect required: `up` does not report success, and no ledger row claims that machine. A
        crash loop that flickers usable once is exactly the pod the paid run wasted an hour on, and
        a ledger row for it is a rental the tool believes in and the user does not have.
        """
        alive = pod_payload(status="RUNNING", uptime=180, ports=True)
        crash = pod_payload(status="RUNNING", uptime=0, ports=False)
        provider, calls = self.transport([alive, crash, crash, crash])

        code, out = self.invoke_up(provider)

        self.assertNotEqual(
            code, 0,
            "up reported a successful rental after a single alive reading of a machine that was "
            f"crash-looping immediately afterwards. calls={calls!r}\noutput:\n{out}")
        run = self.ledger.open_run()
        self.assertIsNone(
            run, f"a ledger row was opened for a machine that was never usable: {run}")

    def test_up_still_succeeds_for_a_machine_that_stays_alive(self):
        """Control: a machine that is usable on every reading is a successful `up` with an open
        ledger row — otherwise the fix for the case above would be to refuse everything."""
        alive = pod_payload(status="RUNNING", uptime=180, ports=True)
        provider, calls = self.transport([alive, alive, alive])

        code, out = self.invoke_up(provider)

        self.assertEqual(code, 0, f"up failed on a healthy machine.\ncalls={calls!r}\noutput:\n{out}")
        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"up rented a healthy machine and left no ledger row.\n{out}")
        self.assertEqual(run.get("instance_id"), "pod-1",
                         f"the open row names the wrong machine: {run}")


# ============================================================== 3. F-4 — dry run spends nothing

class DryRunMakesNoCreateCall(unittest.TestCase):
    """`--dry-run` is the command a careful person uses before spending. It must not spend."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)
        self.ledger = Ledger(self.cfg.ledger_path)

    def transport(self) -> tuple[RunPod, list[tuple[str, str]], list[dict]]:
        calls: list[tuple[str, str]] = []
        created: list[dict] = []

        def script(method: str, path: str, body=None, params=None, retries: int = 3):
            calls.append((method, path))
            if path == "/v2/catalog/gpus":
                return {"gpus": [{"id": "Fake GPU 24GB", "memory": 24, "secure": "High",
                                  "price": {"secure": 1.0}}]}
            if method == "POST" and path == "/v2/pods":
                created.append(body or {})
                return {"id": "pod-1", "status": "PROVISIONING", "cost": 1.0}
            if method == "GET" and path == "/v2/pods/pod-1":
                return pod_payload(status="RUNNING", uptime=180, ports=True)
            return {}

        provider = RunPod("test-key-not-real")
        provider._request = script   # type: ignore[method-assign]
        return provider, calls, created

    def invoke_up(self, provider: RunPod, *argv: str) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "dry", *argv])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
             mock.patch.object(cli, "_provider", lambda cfg: provider), \
             mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
             mock.patch("rentguard.runpod.time.sleep", lambda *_: None), \
             contextlib.redirect_stdout(out):
            try:
                code = cli.cmd_up(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except ProviderError:
                code = 1
        return code, out.getvalue()

    def test_dry_run_makes_zero_create_calls(self):
        """Sequence: `rentguard up --purpose dry --dry-run` against a provider that has stock.

        Effect required: zero `POST /v2/pods` calls on the transport, and nothing recorded — no
        ledger row, no state pointer. The printed sentence is not the evidence; the call is.
        """
        provider, calls, created = self.transport()

        code, out = self.invoke_up(provider, "--dry-run")

        self.assertEqual(
            created, [],
            "`--dry-run` created a machine: it printed 'nothing was rented and nothing was spent' "
            f"after calling create {len(created)} time(s). calls={calls!r}\noutput:\n{out}")
        self.assertEqual(code, 0, f"a dry run exited {code}.\noutput:\n{out}")
        self.assertIsNone(self.ledger.open_run(), "a dry run opened a ledger row")
        state = (json.loads(self.cfg.state_path.read_text(encoding="utf-8"))
                 if self.cfg.state_path.exists() else {})
        self.assertIsNone(state.get("instance_id"),
                          f"a dry run recorded an instance pointer: {state}")
        self.assertIn("Fake GPU 24GB", out,
                      f"the dry run does not say which GPU it would have requested.\n{out}")

    def test_a_real_up_does_make_the_create_call(self):
        """Control for the assertion above: without the flag, one `POST /v2/pods` happens, so a
        passing dry-run test is a fact about the flag and not about a transport that never fires."""
        provider, calls, created = self.transport()

        code, out = self.invoke_up(provider)

        self.assertEqual(len(created), 1,
                         f"a real `up` made {len(created)} create call(s).\ncalls={calls!r}\n{out}")
        self.assertEqual(code, 0, f"a real up failed.\noutput:\n{out}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
