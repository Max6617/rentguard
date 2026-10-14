"""Attacks on `f28b2dd` (the guard that stopped a fresh pod), from outside its own tests.

Four things, each one an experiment rather than a re-read:

(a) **units** — a test that actually measures "one interval is one minute". The defect was seconds
    added to a minutes counter, so the assertion is a *rate*: N intervals of 60 s must be N minutes,
    not 60·N. A default that hid the units (interval 1 s, or a 0-minute window) would pass a test
    that only checked "it stopped eventually";
(b) **both directions** — a busy pod whose utilisation reading is *missing* must survive, and a
    genuinely idle old pod must still be stopped;
(c) **unknown uptime is not zero uptime** — the two are different facts and must not collapse into
    each other in the snapshot;
(d) **the MCP path** — `rentguard_watch`/`rentguard_watch_once` must run the same loop, shown by the
    machine actually being stopped through that route, not by asserting that a call was made.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli, guard, mcp_server  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.provider import Provider  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def pod_payload(*, uptime=3600, util=0.0, with_gpus=True, provider_idle=None,
                cost=2.0, status="RUNNING") -> dict:
    gpus = []
    if with_gpus:
        gpu: dict = {"util": util, "memoryUtil": 0.0}
        if provider_idle is not None:
            gpu["idle"] = provider_idle
        gpus = [gpu]
    return {"id": "pod-A", "status": status, "cost": cost, "gpu": {"id": "Fake GPU 24GB"},
            "runtime": {"uptime": uptime, "ports": {"22": "tcp"}, "gpus": gpus}}


class ScriptedProvider(Provider):
    """One fixed payload to `get`, and a record of every action. No network."""

    name = "scripted"

    def __init__(self, payload: dict):
        super().__init__(None)
        self.payload = payload
        self.actions: list[tuple[str, str]] = []
        self.force_stops: list[str] = []

    def get(self, instance_id):
        return dict(self.payload)

    def list_pods(self):
        return [dict(self.payload)]

    def action(self, instance_id, action):
        self.actions.append((instance_id, action))

    def force_stop(self, instance_id):
        self.force_stops.append(instance_id)
        self.action(instance_id, "stop")


class IdleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=100.0, per_run_cap_usd=50.0, idle_minutes=5.0),
                          state_dir=self.root)
        self.cfg.provider = "fake"
        self.ledger = Ledger(self.cfg.ledger_path)

    def watch(self, provider: ScriptedProvider, *, interval: float = 60.0,
              iterations: int = 3) -> list:
        """Run the real loop, capturing every snapshot it decides on."""
        seen: list = []

        def record(snap):
            seen.append(snap)
            return None          # never stop: this test is about the numbers, not the decision

        with mock.patch.object(guard, "decide", record), \
                mock.patch("time.sleep", lambda *_: None):
            guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A",
                        interval=interval, max_iterations=iterations, log=lambda *a: None)
        return seen

    def snapshot(self, payload: dict, **kwargs):
        return guard.snapshot(payload, self.cfg, self.ledger, **kwargs)


class TheUnitsAreMinutes(IdleCase):
    def test_one_interval_of_sixty_seconds_is_one_minute_of_idle(self):
        """Sequence: an idle pod watched for three intervals of 60 s.

        Effect required: the idle count grows by **one minute per interval**. The defect this guards
        added `interval` (seconds) to a minutes counter, so five minutes of watching read as five
        hours; a test that only checks "it stopped" cannot see that, and this one does.
        """
        provider = ScriptedProvider(pod_payload(uptime=3600, util=0.0))

        snaps = self.watch(provider, interval=60.0, iterations=3)
        idle = [snap.idle_for_minutes for snap in snaps]

        self.assertEqual(len(snaps), 3, f"the loop did not run three times: {idle}")
        self.assertEqual([round(value - idle[0], 6) for value in idle], [0.0, 1.0, 2.0],
                         f"60 s intervals did not count as minutes: {idle} (a seconds-as-minutes "
                         f"counter would read {[round(v * 60, 1) for v in idle]})")
        self.assertLessEqual(max(idle), 3.0,
                             f"idle grew faster than one minute per interval: {idle}")

    def test_thirty_second_intervals_also_convert(self):
        """Effect: two 30 s intervals are one minute — the conversion is by time, not by a fixed
        assumption that an interval happens to be a minute."""
        provider = ScriptedProvider(pod_payload(uptime=3600, util=0.0))

        snaps = self.watch(provider, interval=30.0, iterations=2)
        idle = [snap.idle_for_minutes for snap in snaps]

        self.assertAlmostEqual(idle[1] - idle[0], 0.5, places=6,
                               msg=f"two 30 s intervals should be 0.5 min apart: {idle}")

    def test_elapsed_watch_time_is_not_idle_time(self):
        """Sequence: a busy pod (87% utilisation) watched for four hours by a caller that reports
        its own elapsed watch time.

        Effect required: elapsed watch time is not a reason to stop anything. It is not a candidate
        for the idle figure, so a four-hour watch of a working card stops nothing — the defect this
        guards stopped exactly that pod, at `up 0.00h`, as "idle 30m".
        """
        pod = pod_payload(uptime=3600, util=87.0, provider_idle=1800)

        snap = self.snapshot(pod, idle_for_minutes=0.0, idle_since_start_minutes=240.0)

        self.assertEqual(snap.idle_since_start_minutes, 240.0, "the fixture lost the elapsed time")
        self.assertEqual(snap.effective_idle_minutes, 0.0,
                         f"elapsed watch time was counted as idle: {snap.effective_idle_minutes} min")
        self.assertIsNone(guard.decide(snap), "a working pod was stopped for being watched")


class BothDirections(IdleCase):
    def test_a_pod_with_no_utilisation_reading_is_not_called_idle(self):
        """Sequence: a pod reports no GPU block at all (the reading is absent, as in `gpu None%`),
        and there is no provider idle figure. Watched for five intervals of 60 s.

        Effect required: never stopped. An absent reading is not evidence of idleness; the defect
        treated it as zero utilisation and stopped the machine.
        """
        provider = ScriptedProvider(pod_payload(uptime=3600, with_gpus=False))

        snaps = self.watch(provider, interval=60.0, iterations=5)

        self.assertEqual(provider.actions, [], f"a pod with no utilisation reading was stopped")
        self.assertTrue(all(snap.effective_idle_minutes == 0.0 for snap in snaps),
                        f"an absent reading was counted as idle: "
                        f"{[s.effective_idle_minutes for s in snaps]}")

    def test_a_genuinely_idle_old_pod_is_still_stopped(self):
        """Control for the case above: a pod an hour old whose GPU reports half an hour idle, with a
        five-minute window, must still be stopped — otherwise the "fix" would be to never stop."""
        provider = ScriptedProvider(pod_payload(uptime=3600, util=0.0, provider_idle=1800))

        snaps = self.watch(provider, interval=60.0, iterations=1)
        reason = guard.decide(snaps[-1])

        self.assertIsNotNone(reason, f"a genuinely idle machine was left billing: {snaps[-1]}")
        self.assertIn("idle", reason or "")

    def test_a_busy_pod_beats_a_host_scoped_idle_reading(self):
        """Effect: a sampled 87% utilisation outranks the provider's per-GPU idle number, which is
        measured against the host and can read half an hour on a pod the card is working in."""
        provider = ScriptedProvider(pod_payload(uptime=3600, util=87.0, provider_idle=1800))

        snaps = self.watch(provider, interval=60.0, iterations=2)

        self.assertEqual(snaps[-1].effective_idle_minutes, 0.0,
                         f"a busy pod was counted as idle: {snaps[-1].effective_idle_minutes} min")
        self.assertIsNone(guard.decide(snaps[-1]), "a busy pod was stopped as idle")
        self.assertEqual(provider.actions, [])


class UnknownUptimeIsNotZero(IdleCase):
    def test_an_unknown_uptime_and_a_zero_uptime_are_different_facts(self):
        """Sequence: the same pod payload twice, once with no uptime at all and once with uptime
        0.00 h. The caller's own idle count says 30 minutes in both cases.

        Effect required: with uptime unknown, nothing read can be attributed to this pod, so the
        provider figure is None and the caller's count is dropped; with uptime 0 the pod is simply
        brand new, which is a different fact. Neither may be a reason to stop, and the two must not
        collapse into the same reading.
        """
        unknown = pod_payload(uptime=None, util=0.0, provider_idle=1800)
        zero = pod_payload(uptime=0, util=0.0, provider_idle=1800)

        snap_unknown = self.snapshot(unknown, idle_for_minutes=30.0)
        snap_zero = self.snapshot(zero, idle_for_minutes=30.0)

        self.assertIsNone(snap_unknown.max_idle_minutes,
                          "a provider idle reading was trusted with no uptime to bound it")
        self.assertEqual(snap_unknown.idle_for_minutes, 0.0,
                         "the caller's idle count survived an unknown uptime")
        self.assertIsNotNone(snap_zero.max_idle_minutes,
                             "a zero uptime was treated as unknown, or the reading was dropped")
        self.assertEqual(snap_zero.max_idle_minutes, 0.0,
                         f"a brand-new pod reported idle: {snap_zero.max_idle_minutes}")
        self.assertNotEqual(snap_unknown.max_idle_minutes, snap_zero.max_idle_minutes,
                            "unknown uptime and zero uptime collapsed into one reading")
        self.assertIsNone(guard.decide(snap_unknown), "stopped on a reading with no uptime")
        self.assertIsNone(guard.decide(snap_zero), "stopped a brand-new pod")


class McpWatchSharesTheLoop(IdleCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.budget.per_run_cap_usd = 5.0
        self.cfg.budget.cap_usd = 25.0
        self.ledger.open(purpose="mcp-watch", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc() - timedelta(hours=3))   # $6 against a $5 run cap
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        self.fake = ScriptedProvider(pod_payload(uptime=3 * 3600, util=0.0))

    def dispatch(self, name: str, arguments: dict) -> tuple:
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake):
            return mcp_server.dispatch(name, arguments)

    def test_watch_once_through_mcp_stops_an_over_cap_machine(self):
        """Effect: the machine is stopped *through the MCP route*. That is the proof the tool shares
        the loop; an assertion that a function was called would pass even if the MCP tool had its own
        copy of the logic."""
        text, structured, is_error = self.dispatch("rentguard_watch_once", {})

        self.assertIn(("pod-A", "stop"), self.fake.actions,
                      f"the MCP watch tool did not act on an over-cap machine.\n{text}")
        self.assertIsNotNone(structured, f"no structured decision was returned.\n{text}")

    def test_both_watch_tools_go_through_the_same_loop(self):
        """Effect: both tools reach `guard.watch`, and `watch_once` is a one-poll guard while
        `watch` honours its `max_iterations`."""
        seen: list[dict] = []

        def recorder(cfg, provider, ledger, **kwargs):
            seen.append(kwargs)
            return "patched-stop"

        self.fake = ScriptedProvider(pod_payload(uptime=3 * 3600, util=0.0))
        with mock.patch.object(cli, "guard_watch", recorder):
            first = self.dispatch("rentguard_watch_once", {})
            second = self.dispatch("rentguard_watch", {"max_iterations": 2})

        self.assertEqual(len(seen), 2, f"the two tools did not both reach the loop: {seen}")
        self.assertEqual(seen[0].get("max_iterations"), 1,
                         f"watch_once is not one poll: {seen[0]}")
        self.assertEqual(seen[1].get("max_iterations"), 2,
                         f"watch ignored max_iterations: {seen[1]}")
        self.assertIn("patched-stop", str(first) + str(second),
                      "the result did not come from the loop that ran")


class ACorruptStartDoesNotKillTheGuard(IdleCase):
    """Both halves of the requirement, because either alone can be satisfied the wrong way.

    1. The guard **survives** a truncated `started_at` — a machine billing without a working guard
       is money at risk, so the loop may not die on one unreadable field.
    2. It does **not** survive by pricing the unreadable run as a silent `$0.00`. A guard that
       forgets the spend is worse than one that dies: the cap then reads *low*, which is the
       direction that lets a rental start against a number nobody can compute. Either the money is
       priced from something readable — the provider's own uptime is the witness `up` already uses
       when there is no row at all — or the condition is named.

    A later "fix" that satisfies the first test by making the reading disappear fails the second.
    """

    def corrupt_open_row(self, *, hours_ago: float = 1.0, rate: float = 2.0) -> None:
        from datetime import timedelta
        self.ledger.open(purpose="corrupt", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                         started_at=now_utc() - timedelta(hours=hours_ago))
        rows = self.ledger.entries()
        rows[-1]["started_at"] = "the-ledger-was-truncated"
        self.ledger._rewrite(rows)

    def test_the_guard_survives_a_corrupt_start_on_the_open_row(self):
        """Sequence: the open row's `started_at` is corrupt (an interrupted write), a machine is
        live and busy, and the guard takes a reading.

        Effect required (half 1): the loop survives it and returns a reason. The same
        `ledger.live_usd()` that prices the run is what `doctor` and `up` call, so if it raises here
        the guard dies — and a dead guard on a billing machine is the failure this tool exists to
        prevent.
        """
        self.corrupt_open_row()
        provider = ScriptedProvider(pod_payload(uptime=3600, util=90.0))

        try:
            reason = guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A",
                                 interval=0.0, max_iterations=1, log=lambda *a: None)
        except Exception as exc:  # noqa: BLE001 - the raised exception IS the finding
            self.fail(f"a corrupt started_at on the open row killed the guard loop: "
                      f"{type(exc).__name__}: {exc} — the machine is unwatched while it bills")

        self.assertIsNotNone(reason, "the guard returned no reason")

    def test_a_corrupt_start_does_not_price_the_unreadable_run_as_a_silent_zero(self):
        """Sequence: the same corrupt open row, two hours old at $2/h, on a machine the provider
        reports with 7,200 s of uptime; the guard takes one reading.

        Effect required (half 2): the run is **not** silently worth nothing. Either it is priced
        from something readable — the provider's uptime supports about $4.00 — or the unreadable
        field is named in the guard's own output. A heartbeat with `run_usd == 0.0` and no mention
        of the corrupt field is the failure: the cap would read low and let a rental start.
        """
        self.corrupt_open_row(hours_ago=2.0)
        provider = ScriptedProvider(pod_payload(uptime=7200, util=90.0))
        lines: list[str] = []

        try:
            guard.watch(self.cfg, provider, self.ledger, instance_id="pod-A", interval=0.0,
                        max_iterations=1, log=lines.append)
        except Exception as exc:  # noqa: BLE001 - half 1 of the requirement, in this test's terms
            self.fail(f"the guard did not survive the corrupt row, so it could not price it either: "
                      f"{type(exc).__name__}: {exc}")

        heartbeat = json.loads(self.cfg.heartbeat_path.read_text(encoding="utf-8"))
        named = any(word in " ".join(lines).lower()
                    for word in ("unreadable", "corrupt", "cannot read", "started_at"))
        self.assertTrue(
            float(heartbeat.get("run_usd") or 0.0) > 0.0 or named,
            "the guard survived the corrupt row by making the money disappear: the heartbeat "
            f"reports run_usd={heartbeat.get('run_usd')} and nothing in the guard's output names the "
            f"unreadable field {list(heartbeat)[:6]}. Price the run from the provider's uptime, or "
            f"name the condition — a cap that reads low lets a rental start.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
