"""A bounded `watch` must be **readable as ending at its bound** (the design rule, 2026-10-22).

Measured on a rental window: a watch armed as `watch --interval 30 --max-iterations 1000` (8.3 h) returned long
before that bound, exited **0**, and left the pod unguarded; the heartbeat kept reading "alive" for
minutes afterwards, so the read path said protected while the process was gone — twelve such calls and a
hand re-arming loop kept one pod watched for three hours. A zero exit is the dangerous half: a caller
reading only the exit code, or an agent reading `armed`, cannot tell "ended at its own bound" from "still
guarding".

So the bound is **explicit in three places**: the text the command prints when it arms, the closing line
and JSON when it ends (with `armed: false`, `iterations`, `max_iterations`), and the guard's own
heartbeat — which now carries `max_iterations`, `iterations`, `interval_s` and `ends_at`, so `status` can
say *when protection stops by itself*. A bounded watch that ends at that bound exits **2**; a watch that
stopped a machine or found one already stopped still exits 0.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli, guard, mcp_server  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import iso, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class WatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                          state_dir=self.root / ".rg")
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.cfg.artefacts.pull = []
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.fake = FakeProvider()
        instance = self.fake.create(gpu_id="Fake GPU 24GB", image="img", container_disk_gb=20,
                                    cloud="COMMUNITY", ports=["22/tcp"], name="watch-bound")
        self.instance_id = instance["id"] if isinstance(instance, dict) else instance.id
        self.fake.action(self.instance_id, "start")
        self.cfg.state_path.write_text(json.dumps({"instance_id": self.instance_id}),
                                       encoding="utf-8")

    def watch(self, *extra: str) -> tuple[int, str, dict]:
        args = cli.build_parser().parse_args(["watch", "--json", *extra])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_watch(args)
        text = out.getvalue()
        return code, err.getvalue(), json.loads(text) if text.strip() else {}


class ABoundedWatchEndsAtItsBound(WatchCase):
    def test_the_poll_count_is_the_bound_and_the_reason_says_so(self) -> None:
        code, err, document = self.watch("--interval", "0", "--max-iterations", "5")

        self.assertEqual(document.get("reason"), "guard-iteration-limit", err)
        self.assertEqual(document.get("iterations"), 5, f"the bound was not honoured: {document}")
        self.assertEqual(document.get("max_iterations"), 5, document)
        self.assertFalse(document.get("armed"), document)

    def test_ending_at_the_bound_is_not_exit_zero(self) -> None:
        """The half that cost the window: a zero exit reads as success to every caller."""
        code, err, document = self.watch("--interval", "0", "--max-iterations", "5")

        self.assertEqual(code, 2, f"a watch that ended at its bound exited {code}:\n{err}")
        self.assertTrue(document.get("protection_ended_at_bound"), document)
        self.assertIn("ENDED", err, f"the closing line does not say protection ended:\n{err}")
        self.assertIn("NOTHING is watching", err, err)

    def test_the_arming_line_states_the_bound_and_that_protection_ends(self) -> None:
        code, err, document = self.watch("--interval", "30", "--max-iterations", "1")

        self.assertEqual(code, 2, err)
        self.assertIn("protection ENDS", err, f"the arming line does not name the bound:\n{err}")
        self.assertIn("1 poll", err, err)
        self.assertAlmostEqual(document.get("bounded_hours"), 30 / 3600.0, places=6)


class TheHeartbeatCarriesTheBound(WatchCase):
    def test_the_guard_writes_its_bound_and_how_far_it_got(self) -> None:
        self.watch("--interval", "0.5", "--max-iterations", "2")

        files = list(Path(self.cfg.state_dir).glob("heartbeat-watch-*.json"))
        self.assertEqual(len(files), 1, f"expected one guard heartbeat, found {files}")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload.get("max_iterations"), 2, payload)
        self.assertEqual(payload.get("iterations"), 2, payload)
        self.assertEqual(payload.get("interval_s"), 0.5, payload)
        self.assertTrue(payload.get("ends_at"), f"the heartbeat names no end of its own:{payload}")

    def test_a_finished_guard_is_not_protection(self) -> None:
        """The control for the read path: after it returns, the set must not read as alive."""
        self.watch("--interval", "0", "--max-iterations", "2")

        reading = guard.heartbeat_reading(self.cfg.heartbeat_path)
        self.assertIsNotNone(reading, "the guard left no heartbeat at all")
        self.assertFalse(reading.alive, f"a returned guard still reads as protection: {reading}")
        self.assertTrue(reading.ended, reading)


class TheReadPathShowsTheBound(WatchCase):
    def test_status_names_the_bound_of_a_live_bounded_guard(self) -> None:
        snap = guard.snapshot_fallback(self.cfg, cli.Ledger(self.cfg.ledger_path))
        guard.write_heartbeat(self.cfg.heartbeat_path, snap, guard_id="live-bounded",
                              bound={"max_iterations": 5, "iterations": 3, "interval_s": 30.0,
                                     "ends_at": iso(now_utc() + timedelta(seconds=60))})

        args = cli.build_parser().parse_args(["status"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_status(args)

        text = out.getvalue() + err.getvalue()
        line = next((ln for ln in text.splitlines() if ln.startswith("guard ")), "")
        self.assertEqual(code, 0, text)
        self.assertIn("bounded", line, f"status does not show a bounded guard's bound:\n{text}")
        self.assertIn("poll 3 of 5", line, line)


class Controls(WatchCase):
    def test_watch_once_is_still_a_successful_sample(self) -> None:
        code, err, document = self.watch("--once", "--interval", "0")

        self.assertEqual(code, 0, f"a one-poll sample became an error:\n{err}")
        self.assertFalse(document.get("armed"), document)

    def test_a_watch_that_stopped_a_machine_still_exits_zero(self) -> None:
        """The control: a guard that did its job is a success, not a bound ending in disguise."""
        cli.Ledger(self.cfg.ledger_path).open(
            purpose="probe", provider="fake", instance_id=self.instance_id,
            gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
            started_at=now_utc() - timedelta(hours=3))
        instance = self.fake.instances[self.instance_id]
        instance.rate = 2.0
        instance.uptime_seconds = 3 * 3600

        code, err, document = self.watch("--interval", "0", "--max-iterations", "5")

        self.assertTrue(document.get("stopped"), f"the guard did not stop it: {document}\n{err}")
        self.assertEqual(code, 0, f"a guard that did its job exited {code}:\n{err}")


class TheSurfaceSaysItToo(unittest.TestCase):
    def test_the_help_text_states_that_protection_ends_at_the_bound(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["watch", "--help"])

        text = out.getvalue()
        self.assertIn("protection ENDS", text, f"the help does not name the bound:\n{text}")

    def test_the_mcp_description_states_the_hard_end(self) -> None:
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "rentguard_watch")

        resource = tool["inputSchema"]["properties"]["max_iterations"]["description"]
        self.assertIn("ENDS protection", resource, resource)
        self.assertIn("hard end to protection", tool["description"], tool["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
