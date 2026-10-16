"""A guard must not report itself armed unless it names a machine that is actually billing.

Defect 16, found by Cadence during the 2026-10-15 shakedown, and it is a defect against an invariant
this project had already ruled: *no `up` may return success while a machine it can see is billing
without an open row, or with no guard able to see it.*

What happened: window 1's pod was stopped, window 2's was not up yet, and a `watch` was armed in
between. It bound to the **previous**, stopped pod, printed `watching <dead-id>`, polled once, returned
`already stopped` and exited **0**. Every word of that reads as "protection armed"; nothing was
watching. Window 2's pod then appeared and billed with no guard until a person noticed and re-armed.

The rule pinned here, in the two shapes it has:

- a guard that cannot name a **live** instance must **refuse to arm** — a non-zero exit, `armed: false`
  and a reason, never a clean return that a caller reads as protection;
- when the state file's instance is stopped but the **ledger's open row** names a live machine, the
  guard watches that machine: the row is the record that survives a stale state file, and the machine
  it names is the one that is billing.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class FlipsAfterArming(FakeProvider):
    """RUNNING for the arming read, then whatever the machine really does next.

    That ordering is the shape the frame window hit: the guard is armed while the pod is live
    (defect 16's rule requires a live instance), and only the *poll* sees a machine that has since
    stopped.
    """

    def __init__(self, *args, then: str = "EXITED", raises: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = 0
        self.then = then
        self.raises = raises

    def get(self, instance_id):
        self.reads += 1
        if self.reads > 1:
            if self.raises:
                raise ProviderError("the provider is unreachable")
            pod = super().get(instance_id)
            return None if pod is None else {**pod, "status": self.then}
        return super().get(instance_id)


class ArmingCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.ledger = Ledger(self.cfg.ledger_path)
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def watch(self, provider, *argv: str) -> tuple[int, str, dict]:
        args = cli.build_parser().parse_args(["watch", *argv, "--json"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_watch(args)
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, text + err.getvalue(), document


class AStopIsOnlyReportedWhenItHappened(ArmingCase):
    """`cmd_watch` printed `the guard stopped the machine: already stopped` and emitted
    `stopped: true` for a machine it found already stopped (defect 23; the guard's own line in the
    frame window was `guard finished: already stopped`)."""

    def arm_and_poll(self, provider, *, minutes_ago: float = 0.0) -> tuple[int, str, dict]:
        from datetime import timedelta
        self.write_state({"instance_id": "pod-A"})
        self.ledger.open(purpose="probe", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc() - timedelta(minutes=minutes_ago))
        return self.watch(provider, "--once")

    def test_a_machine_already_stopped_is_not_reported_as_stopped(self) -> None:
        provider = FlipsAfterArming(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")

        code, text, document = self.arm_and_poll(provider)

        self.assertFalse(document.get("stopped", True),
                         f"a machine the guard did not stop was reported as stopped: {document}")
        self.assertNotIn("the guard stopped the machine", text,
                         f"the summary claims a stop that did not happen:\n{text}")
        self.assertIn("already stopped", text.lower(),
                      f"the output does not say what it found:\n{text}")
        self.assertNotIn(("pod-A", "stop"), provider.actions,
                         "the guard called stop on a machine that was already stopped")

    def test_a_provider_error_is_not_reported_as_a_stop(self) -> None:
        provider = FlipsAfterArming(rate_usd_per_hour=2.0, raises=True)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")

        code, text, document = self.arm_and_poll(provider)

        self.assertFalse(document.get("stopped", True),
                         f"a guard that could not read the provider claimed a stop: {document}")
        self.assertNotIn("the guard stopped the machine", text,
                         f"an unreadable provider was reported as a stop:\n{text}")
        self.assertIn("provider", text.lower(), f"the failure is not named:\n{text}")

    def test_a_real_stop_is_still_reported_as_stopped(self) -> None:
        """The control: the field stays true when the guard really did stop the machine."""
        self.cfg.budget.per_run_cap_usd = 0.10     # five minutes at $2/h is already over it
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")

        code, text, document = self.arm_and_poll(provider, minutes_ago=5.0)

        self.assertTrue(document.get("stopped"), f"a real stop was not reported: {document}")
        self.assertIn(("pod-A", "stop"), provider.actions,
                      f"the guard did not stop an over-cap machine:\n{text}")
        self.assertIn("per-run cap", document.get("reason", "") or "",
                      f"the reason is not the cap: {document}")


class AStoppedInstanceIsNotArmed(ArmingCase):
    def test_watch_refuses_instead_of_reporting_a_clean_return(self) -> None:
        """The measured shape: a stopped instance, no open row, nothing else live."""
        self.write_state({"instance_id": "pod-dead"})
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-dead", uptime_seconds=600, status="EXITED")

        code, text, document = self.watch(provider, "--once")

        self.assertNotEqual(code, 0, f"a guard armed against a stopped machine exited 0:\n{text}")
        self.assertFalse(document.get("armed", False),
                         f"the refusal still reported itself armed: {document}")
        self.assertIn("NOT armed", text,
                      f"the output does not say the machine is unguarded:\n{text}")
        self.assertEqual(document.get("reason"), "no-live-instance", document)
        self.assertNotIn("watching pod-dead", text,
                         f"the output claimed to be watching a stopped machine:\n{text}")

    def test_watch_refuses_before_anything_is_rented(self) -> None:
        """The defect's own moment: armed between two windows, with no machine at all yet."""
        provider = FakeProvider(rate_usd_per_hour=2.0)

        code, text, document = self.watch(provider, "--once")

        self.assertNotEqual(code, 0, f"a guard with nothing to watch exited 0:\n{text}")
        self.assertFalse(document.get("armed", False), document)


class TheOpenRowNamesTheLiveMachine(ArmingCase):
    def test_watch_binds_to_the_machine_the_open_row_names(self) -> None:
        """A stale state file names the previous, stopped pod; the ledger's open row names the one
        that is billing now. The guard watches the billing machine, not the dead one."""
        self.write_state({"instance_id": "pod-dead"})
        self.ledger.open(purpose="window 2", provider="fake", instance_id="pod-live",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0)
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-dead", uptime_seconds=600, status="EXITED")
        provider.add("pod-live", uptime_seconds=120, status="RUNNING")

        code, text, document = self.watch(provider, "--once")

        self.assertEqual(document.get("instance_id"), "pod-live",
                         f"the guard watched {document.get('instance_id')!r} instead of the machine "
                         f"that is billing:\n{text}")
        self.assertIn("watching pod-live", text,
                      f"the guard did not say which machine it was watching:\n{text}")
        self.assertEqual(code, 0, f"the guard failed on a live machine:\n{text}")


class ALiveInstanceIsWatchedAsBefore(ArmingCase):
    def test_the_control_still_arms(self) -> None:
        self.write_state({"instance_id": "pod-A"})
        self.ledger.open(purpose="window 1", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0)
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=60, status="RUNNING")

        code, text, document = self.watch(provider, "--once")

        self.assertEqual(code, 0, f"a live machine was not watched:\n{text}")
        self.assertIn("watching pod-A", text, f"the guard did not name the machine:\n{text}")
        self.assertEqual(document.get("instance_id"), "pod-A", document)


if __name__ == "__main__":
    unittest.main()
