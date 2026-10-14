"""The guard must not stop a pod for idleness the pod has not lived through (defect 15).

Measured 2026-10-14 18:32:21Z in `grok/pod-timing/guard-watch.log`: a pod was stopped at
``up 0.00h``, ``gpu None%``, reported as ``idle 30m (limit 5 min)``. Two arms could produce that
number, and both are wrong:

- ``idle_since_start_minutes`` is **elapsed watch time**, not idle time. It grows whether the GPU is
  busy or not, so a healthy pod watched for half an hour is stopped for being watched — the worst
  kind of guard, one that kills the run it exists to protect.
- the guard's own accumulator (and the provider's per-GPU reading) can outlive the pod they are
  counting for: a restart resets the pod's uptime to zero while the caller keeps counting.

The provider's arm was clamped by uptime at ``d5fdaea`` — a 32-second-old pod was stopped for
"idle 30 min" — and this is the same shape failing through the other arms. The rule: **idle time is
bounded by how long the pod has existed, and elapsed time is never idle time.**
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def pod(uptime: float | None, *, util: float | None = 0.0, idle: float | None = 1800.0,
        status: str = "RUNNING") -> dict:
    gpus = [] if util is None else [{"util": util, "idle": idle}]
    return {"id": "pod-A", "status": status, "cost": 0.18,
            "runtime": {"uptime": uptime, "ports": {"22": "tcp"}, "gpus": gpus}}


class FreshPodIsNotIdle(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=0.5, idle_minutes=5.0),
                          state_dir=scratch_dir(self))
        self.ledger = Ledger(self.cfg.ledger_path)
        self.ledger.open(purpose="fresh pod", provider="fake", instance_id="pod-A",
                         gpu_id="NVIDIA GeForce RTX 3080 Ti", rate_usd_per_hour=0.18)

    def snap(self, pod_dict: dict, **kwargs):
        return guard.snapshot(pod_dict, self.cfg, self.ledger, **kwargs)

    def test_a_pod_seconds_old_is_not_stopped_for_thirty_minutes_of_idle(self):
        """The incident: the guard had been counting for 30 minutes, the pod had existed for none."""
        snap = self.snap(pod(uptime=0.0, util=0.0), idle_for_minutes=30.0,
                         idle_since_start_minutes=30.0)

        self.assertIsNone(guard.decide(snap),
                          f"a pod 0.00 h old was stopped: {guard.decide(snap)!r} "
                          f"(effective idle {snap.effective_idle_minutes} min)")

    def test_watching_a_busy_pod_does_not_make_it_idle(self):
        """Elapsed time is not idle time: a pod with the GPU at 87% is not idle, however long the
        guard has been watching it."""
        snap = self.snap(pod(uptime=3600.0, util=87.0), idle_for_minutes=0.0,
                         idle_since_start_minutes=30.0)

        self.assertIsNone(guard.decide(snap),
                          f"a busy pod was stopped for being watched: {guard.decide(snap)!r}")

    def test_an_unknown_uptime_is_never_a_stop_reason(self):
        """Control from `d5fdaea`, kept: a reading that cannot be attributed to this pod is ignored."""
        snap = self.snap(pod(uptime=None, util=0.0), idle_for_minutes=30.0,
                         idle_since_start_minutes=30.0)

        self.assertIsNone(guard.decide(snap), f"stopped on an unattributable reading: {guard.decide(snap)!r}")

    def test_a_genuinely_idle_pod_is_still_stopped(self):
        """Control: the fix must not be "never stop for idleness". An hour-old pod whose GPU has been
        idle for 30 minutes is what the guard is for."""
        snap = self.snap(pod(uptime=3600.0, util=0.0, idle=1800.0), idle_for_minutes=1800.0 / 60.0)

        reason = guard.decide(snap)
        self.assertIsNotNone(reason, "a genuinely idle machine was left billing")
        self.assertIn("idle", reason or "")

    def test_an_idle_pod_is_not_stopped_on_the_first_poll(self):
        """The accumulator is in minutes and the interval is in seconds: adding the interval raw made
        a one-minute poll look like an hour of idleness, so the first poll of an idle pod stopped it."""
        provider = mock.Mock()
        provider.get.return_value = pod(uptime=3600.0, util=0.0, idle=60.0)
        provider.action = mock.Mock()

        reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A", interval=60.0,
                             log=lambda *a, **k: None, max_iterations=1)

        self.assertEqual(reason, "guard-iteration-limit",
                         f"the first poll stopped an idle pod ({reason!r})")
        provider.action.assert_not_called()

    def test_the_guard_loop_uses_the_bounded_number(self):
        """The loop, not only the arithmetic: a watch that has been running for an hour over a pod
        that started a minute ago must not stop it."""
        provider = mock.Mock()
        provider.get.return_value = pod(uptime=60.0, util=0.0)
        provider.action = mock.Mock()

        reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A", interval=0.0,
                             log=lambda *a, **k: None, max_iterations=1)

        self.assertEqual(reason, "guard-iteration-limit",
                         f"the guard stopped a one-minute-old pod: {reason!r}")
        provider.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
