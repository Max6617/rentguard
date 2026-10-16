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

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
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

        self.assertTrue(self.cfg.heartbeat_path.exists(),
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


if __name__ == "__main__":
    unittest.main()
