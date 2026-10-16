"""`up` arms a guard as soon as the row names a machine (defect 2, B19).

The pilot window, 2026-10-16 17:23:31: `up` had not returned â€” `batch_b.log` was a wall of
`RUNNING but not usable yet (uptime=None, ports=NULL) â€” waiting` â€” and **no guard was armed**, because
the driver arms one only after `up` returns (`run_batch_b.py::arm_guard`). `per_run_cap_usd 2.0` and
`max_hours_per_run 2.2` are enforced *by the guard*, so the one state that needs a bound â€” a machine
that bills and never becomes usable â€” was the one state with none.

Arming *before* `up` is not the fix: that binds the guard to the previous, dead instance (defect 16).
Arming **as soon as the row names a machine** is, and the invariant it serves is B19's: *a window
cannot bill without a guard watching it, including when `up` never returns.*

The test's machine never becomes usable and `wait_running` keeps polling, so only a guard that was
armed during the wait can stop it. Two assertions carry that and cannot pass by accident: the guard's
own reason appears (`per-run cap`), and a **heartbeat** was written while `up` waited â€” `up`'s own
unusable-machine path also calls stop, so the stop alone would not prove a guard existed.
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
from rentguard.guard import ReleaseGate  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class NeverUsable(FakeProvider):
    """RUNNING with no uptime and no ports, for as long as the caller keeps waiting.

    That is the measured shape, and it is why the guard matters here: nothing in this pod can be
    measured (no uptime means no idle reading and no clock bound), so the only bound that can fire is
    a **cap**, and only a guard is watching the cap.
    """

    def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
        # The crash loop as the provider reports it: RUNNING, no uptime, no ports, forever.
        self.instances[instance_id].status = "RUNNING"
        self.instances[instance_id].uptime_seconds = 0.0
        self.instances[instance_id].ports = None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            pod = self.get(instance_id) or {}
            if str(pod.get("status")) != "RUNNING":
                return pod
            time.sleep(0.05)
        return self.get(instance_id) or {}


class UpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=0.01, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def run_up(self, provider) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "pilot", "--json"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_up(args)
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, document, text + err.getvalue()


class ACrashLoopIsWatched(UpCase):
    def test_the_cap_is_enforced_while_up_waits_for_a_machine_that_never_works(self) -> None:
        # $0.50/s on the clock, against a 1-cent per-run cap and a $10 total cap: the per-run cap is
        # breached 20 ms after the row opens and the total cap only after 20 s, so *which* cap fired
        # is deterministic for any first poll that happens in the first 20 s. The earlier rate
        # ($10/s) put the total cap 1 s in, which made the reason a race against the guard thread's
        # first scheduling — the assertion failed once under load, measuring the machine rather than
        # the guard. Hardened 2026-10-16 while fixing the B19 re-attack findings.
        provider = NeverUsable(rate_usd_per_hour=1800.0)

        started = time.monotonic()
        code, document, text = self.run_up(provider)
        elapsed = time.monotonic() - started

        self.assertIn("guard stopped the instance", text,
                      f"no guard stopped anything while `up` waited: the machine billed unguarded "
                      f"(defect 2)\n{text}")
        self.assertIn("per-run cap", text,
                      f"the wrong bound fired, so the guard's numbers are not the row's:\n{text}")
        self.assertTrue(self.cfg.heartbeat_path.exists(),
                        f"no guard heartbeat was written while `up` waited, so nothing was watching "
                        f"the cap:\n{text}")
        self.assertIn(("fake-1", "stop"), provider.actions,
                      f"the machine was left billing:\n{text}")
        self.assertNotEqual(code, 0, f"a machine that never became usable exited 0:\n{text}")
        self.assertLess(elapsed, 15.0,
                        f"`up` waited {elapsed:.1f}s: the guard did not stop the crash loop")

    def test_a_usable_machine_still_returns_success(self) -> None:
        """The control: arming during the wait must not turn an ordinary rental into a failure."""
        provider = FakeProvider(rate_usd_per_hour=0.13)

        code, document, text = self.run_up(provider)

        self.assertEqual(code, 0, f"a healthy rental failed:\n{text}")
        self.assertIsNotNone(Ledger(self.cfg.ledger_path).open_run(),
                             f"the row is not open after a successful `up`:\n{text}")


class AReleasedWaitGuardWritesNothing(UpCase):
    """No heartbeat may be stamped after `_release_wait_guard` returns (re-attack finding 4).

    Assay's case in `tests/test_b19_money_fix_attacks.py` asserts the *thread* is not alive when the
    release returns; this one asserts the **effect** that liveness is a proxy for — the heartbeat file
    `status` and `doctor` read must not appear after `up` has released the guard, because a fresh
    heartbeat there reads as "protection armed" while nothing is watching.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ledger = Ledger(self.cfg.ledger_path)

    class SlowGet(FakeProvider):
        def __init__(self, *args, delay=0.0, **kwargs):
            super().__init__(*args, **kwargs)
            self.delay = delay
            self.calls = 0

        def get(self, instance_id):
            self.calls += 1
            if self.calls == 1:
                time.sleep(self.delay)
            return super().get(instance_id)

    def test_a_guard_released_inside_a_slow_read_leaves_no_heartbeat(self) -> None:
        provider = self.SlowGet(rate_usd_per_hour=0.13, delay=1.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")

        handle = cli._start_wait_guard(self.cfg, provider, self.ledger, "pod-A",
                                       log=lambda *_: None, interval=0.05)
        time.sleep(0.2)                       # the guard is inside its first, slow read
        cli._release_wait_guard(handle)       # returns only when that read is over

        self.assertFalse(self.cfg.heartbeat_path.exists(),
                         "a released wait guard stamped a heartbeat after `up` had released it: "
                         "`status` and `doctor` read a fresh heartbeat as protection armed")

    def test_the_heartbeat_is_written_while_the_guard_is_armed(self) -> None:
        """The control: the release gate must not silence a guard that is still armed."""
        provider = FakeProvider(rate_usd_per_hour=0.13)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")

        handle = cli._start_wait_guard(self.cfg, provider, self.ledger, "pod-A",
                                       log=lambda *_: None, interval=0.05)
        deadline = time.monotonic() + 5.0
        while not self.cfg.heartbeat_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cli._release_wait_guard(handle)

        self.assertTrue(self.cfg.heartbeat_path.exists(),
                        "an armed wait guard wrote no heartbeat, so nothing was watching")


class TheReleaseGate(unittest.TestCase):
    def test_a_write_attempted_after_the_release_does_not_run(self) -> None:
        """The mechanism the wait guard's silence rests on, without a thread in the way."""
        gate = ReleaseGate()
        ran: list[str] = []

        self.assertTrue(gate.attempt(lambda: ran.append("armed")))
        gate.set()
        self.assertFalse(gate.attempt(lambda: ran.append("after the release")))
        self.assertEqual(ran, ["armed"])
        self.assertTrue(gate.is_set(), "the release did not end the loop either")


if __name__ == "__main__":
    unittest.main()
