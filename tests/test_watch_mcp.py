"""The MCP surface for arming protection: ``watch_once``, a bounded ``watch``, and ``init``.

A Bot must be able to arm the cap without a human terminal, and to tell whether protection is
currently armed. These drive ``mcp_server.dispatch`` — the same entry the stdio server calls — so
they test the tool surface, not a private helper.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli, mcp_server  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class SurfaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=10.0),
                          state_dir=self.root)
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.ledger = Ledger(self.cfg.ledger_path)
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def dispatch(self, name: str, provider: FakeProvider, arguments: dict | None = None):
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider):
            return mcp_server.dispatch(name, arguments or {})

    def over_cap_machine(self) -> FakeProvider:
        """A run 3 h old at $2/h ($6) against a $5 per-run cap: the guard must decide to stop."""
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.ledger.open(purpose="probe", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc() - timedelta(hours=3))
        fake = FakeProvider(rate_usd_per_hour=2.0)
        fake.add("pod-A", rate_usd_per_hour=2.0, uptime_seconds=3 * 3600)
        return fake


# ============================================================== the tool list

class ToolList(unittest.TestCase):
    def test_the_arming_tools_are_exposed(self):
        names = {tool["name"] for tool in mcp_server.TOOLS}
        for expected in ("rentguard_watch_once", "rentguard_watch", "rentguard_init"):
            self.assertIn(expected, names,
                          f"{expected} is missing: the MCP surface cannot arm the cap")


# ============================================================== watch_once

class WatchOnce(SurfaceCase):
    def test_watch_once_returns_the_decision_and_the_armed_state(self):
        """Sequence: a machine over its per-run cap, no guard heartbeat; `watch_once` is called.

        Effect: the tool returns the stop decision AND says whether a guard was already armed — a
        one-shot poll must not claim a guard is still running after it returns.
        """
        fake = self.over_cap_machine()

        text, structured, is_error = self.dispatch("rentguard_watch_once", fake)

        self.assertIsNotNone(structured, f"no structured result:\n{text}")
        self.assertIn("armed_before", structured, "the tool does not report the armed state")
        self.assertFalse(structured["armed"],
                         "a one-shot poll claimed protection was still armed after it returned")
        self.assertIn("per-run cap", structured.get("reason", ""),
                      f"the decision is not reported: {structured}")
        self.assertTrue(structured.get("stopped"), f"the stop was not reported: {structured}")

    def test_watch_once_on_a_healthy_machine_stops_nothing(self):
        """Control: a machine inside its caps is not stopped by a single reading."""
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.ledger.open(purpose="probe", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc() - timedelta(minutes=1))
        fake = FakeProvider(rate_usd_per_hour=2.0)
        fake.add("pod-A", rate_usd_per_hour=2.0, uptime_seconds=60, gpu_util=90.0)

        text, structured, is_error = self.dispatch("rentguard_watch_once", fake)

        self.assertFalse(structured.get("stopped"),
                         f"a healthy machine was stopped by one reading: {structured}")
        self.assertIsNotNone(self.ledger.open_run(),
                             "the guard closed a row for a machine it did not stop")


# ============================================================== bounded watch

class BoundedWatch(SurfaceCase):
    def test_bounded_watch_stops_an_over_cap_machine_and_says_protection_ended(self):
        fake = self.over_cap_machine()

        text, structured, is_error = self.dispatch("rentguard_watch", fake, {"max_iterations": 2})

        self.assertTrue(structured.get("stopped"), f"the guard did not stop it: {structured}")
        self.assertIn("per-run cap", structured.get("reason", ""))
        self.assertFalse(structured.get("armed"),
                         "a bounded watch claimed protection was still armed after it returned")
        self.assertIsNone(self.ledger.open_run(), "the row stayed open after the guard stopped it")


# ============================================================== init

class InitTool(SurfaceCase):
    def test_init_prints_a_usable_template(self):
        text, structured, is_error = self.dispatch("rentguard_init", FakeProvider(),
                                                   {"print": True})

        self.assertFalse(is_error, text)
        self.assertIn("cap_usd", text, f"the template has no budget section:\n{text}")


# ============================================================== adopt: no silent zero

class AdoptNoSilentZero(SurfaceCase):
    def test_adopt_refuses_to_record_zero_for_an_exited_machine(self):
        """Sequence: an EXITED instance, provider reports uptime 0, no `--uptime-hours`.

        Effect: no row is opened at $0; the tool says the cost is unrecoverable and asks for the
        number. A quiet zero for a machine that ran is worse than an honest refusal.
        """
        fake = FakeProvider(rate_usd_per_hour=0.59)
        fake.add("pod-gone", status="EXITED", uptime_seconds=0.0, rate_usd_per_hour=0.59)

        text, structured, is_error = self.dispatch("rentguard_adopt", fake,
                                                   {"instance_id": "pod-gone"})

        self.assertTrue(is_error, f"a silent $0 row was accepted:\n{text}")
        self.assertEqual(structured.get("reason"), "uptime-unrecoverable", text)
        self.assertIsNone(self.ledger.open_run(), "a row was opened at $0 anyway")

    def test_adopt_accepts_an_explicit_uptime_for_an_exited_machine(self):
        fake = FakeProvider(rate_usd_per_hour=0.59)
        fake.add("pod-gone", status="EXITED", uptime_seconds=0.0, rate_usd_per_hour=0.59)

        text, structured, is_error = self.dispatch(
            "rentguard_adopt", fake, {"instance_id": "pod-gone", "uptime_hours": 1.0})

        self.assertFalse(is_error, text)
        run = self.ledger.open_run()
        self.assertIsNotNone(run, "an explicit uptime was given and still no row opened")
        self.assertGreater(run.get("rate_usd_per_hour") or 0, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
