"""The guard heartbeat is a **process fact**, not a timestamp fact (Gilbert's ruling, 2026-10-16).

`status` used to print `guard: alive` from the heartbeat's **age alone**, and a guard that has finished
leaves a heartbeat fresh enough to read as protection. Measured before this fix: `up` arms a wait guard
while it waits, releases it before returning — and `status` then printed

    guard          : alive (heartbeat 0.0 min ago)

with nothing watching, while `up`'s own closing line says "Until something is watching, only you are".
A false protection report is the same class as the false money reports this project has been closing.

The rule this file pins: liveness is a process fact.

* the heartbeat records the **pid** that wrote it;
* `status`/`doctor` say `alive` only while that pid is running **and** the writer has not marked itself
  ended;
* a guard that exits cleanly **marks** the file ended (`cmd_watch` on every exit path, and `up`'s wait
  guard before its release join returns), so "finished" and "silent" are different readings;
* the age stays on the line as a **secondary** signal, never the verdict.

A heartbeat with no pid records cannot satisfy "that pid is running", so it reads as *not running*
rather than alive: the safe direction, and the reason the fixture in
`tests/test_cli_branches.py::StatusBranches` writes a pid like a real guard does.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, iso, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def dead_pid() -> int:
    """A pid no process on this host has, so the probe must say "not running"."""
    return 2 ** 31 - 1


class HeartbeatCase(unittest.TestCase):
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

    def heartbeat(self, **fields) -> dict:
        payload = {"at": iso(now_utc()), "pid": os.getpid(), "ended": False}
        payload.update(fields)
        self.cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def run_cmd(self, func, argv: list[str], provider) -> tuple[int, str]:
        args = cli.build_parser().parse_args(argv)
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def status(self, provider) -> tuple[int, str]:
        return self.run_cmd(cli.cmd_status, ["status"], provider)


class ThePidProbe(unittest.TestCase):
    """`pid_running` distinguishes gone, not-ours and alive — the POSIX half `OSError` would swallow."""

    def test_a_missing_pid_is_not_running(self) -> None:
        self.assertFalse(guard.pid_running(None), "a heartbeat with no pid read as running")
        self.assertFalse(guard.pid_running(0), "pid 0 read as running")
        self.assertFalse(guard.pid_running(-1), "a negative pid read as running")

    def test_a_gone_process_is_not_running(self) -> None:
        def gone(pid, sig):
            raise ProcessLookupError(3, "No such process")
        self.assertFalse(guard.pid_running(4242, kill=gone))

    def test_a_live_process_we_do_not_own_is_running(self) -> None:
        """POSIX raises PermissionError for a live process of another user: that is **alive**."""
        def not_ours(pid, sig):
            raise PermissionError(1, "Operation not permitted")
        self.assertTrue(guard.pid_running(4242, kill=not_ours),
                        "a live process we may not signal was called dead")

    def test_a_windows_dead_pid_reads_as_gone(self) -> None:
        """Windows raises EINVAL (errno 22) for a pid with no process; measured on this host."""
        def windows_gone(pid, sig):
            raise OSError(22, "Invalid argument")
        self.assertFalse(guard.pid_running(4242, kill=windows_gone))

    def test_silence_is_running(self) -> None:
        self.assertTrue(guard.pid_running(4242, kill=lambda pid, sig: None))

    def test_a_pid_that_is_not_a_number_is_not_running(self) -> None:
        """A malformed payload must not read as protection either."""
        self.assertFalse(guard.pid_running("not-a-pid"), "a non-numeric pid read as running")

    def test_the_real_probe_agrees_about_this_process_and_an_impossible_one(self) -> None:
        """No seam: the actual call, on the host the suite is running on."""
        self.assertTrue(guard.pid_running(os.getpid()), "this very process read as not running")
        self.assertFalse(guard.pid_running(dead_pid()), "an impossible pid read as running")


class TheHeartbeatCarriesItsWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(state_dir=scratch_dir(self))
        self.path = self.cfg.heartbeat_path

    def test_write_heartbeat_records_the_writing_pid_and_that_it_has_not_ended(self) -> None:
        snap = guard.snapshot({"id": "pod-A", "status": "RUNNING", "cost": 1.0,
                               "runtime": {"uptime": 600, "ports": {"22": "tcp"}, "gpus": []}},
                              self.cfg, Ledger(self.cfg.ledger_path))
        guard.write_heartbeat(self.path, snap)

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], os.getpid(), "the heartbeat does not name its writer")
        self.assertFalse(payload.get("ended"), "a fresh heartbeat was written already marked ended")

    def test_end_heartbeat_marks_the_file_its_own_writer_left(self) -> None:
        guard.write_heartbeat(self.path, guard.snapshot_fallback(self.cfg, Ledger(self.cfg.ledger_path)))

        self.assertTrue(guard.end_heartbeat(self.path), "the writer's own heartbeat was not marked")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("ended"))
        self.assertIn("ended_at", payload, "the marker does not say when the guard finished")
        self.assertIn("at", payload, "the marker threw away the last reading")

    def test_end_heartbeat_leaves_another_processs_heartbeat_alone(self) -> None:
        """Two guards share one path; the one that ends must not end the other's claim."""
        self.heartbeat_written_by_someone_else = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"at": iso(now_utc()), "pid": os.getpid() + 1,
                                         "ended": False}), encoding="utf-8")

        self.assertFalse(guard.end_heartbeat(self.path),
                         "a heartbeat written by another pid was marked ended by this one")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertFalse(payload.get("ended"), payload)

    def test_end_heartbeat_is_a_no_op_with_no_heartbeat(self) -> None:
        self.assertFalse(guard.end_heartbeat(self.path), "marking a file that does not exist")
        self.assertFalse(self.path.exists(), "the marker created a heartbeat out of nothing")

    def test_end_heartbeat_gives_up_on_a_file_it_cannot_read(self) -> None:
        """A corrupt payload is not something to rewrite: the reader already treats it as no guard."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for payload in ("{ this is not json", "[1, 2, 3]"):
            with self.subTest(payload=payload):
                self.path.write_text(payload, encoding="utf-8")
                self.assertFalse(guard.end_heartbeat(self.path), payload)

    def test_end_heartbeat_accepts_an_explicit_pid(self) -> None:
        """The seam for a caller that marks on behalf of the writer it names."""
        guard.write_heartbeat(self.path, guard.snapshot_fallback(
            Config(state_dir=self.path.parent), Ledger(self.path.parent / "runs.jsonl")))
        self.assertTrue(guard.end_heartbeat(self.path, pid=os.getpid()))
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8")).get("ended"))


class TheReading(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(state_dir=scratch_dir(self))
        self.path = self.cfg.heartbeat_path

    def write(self, **fields) -> None:
        payload = {"at": iso(now_utc()), "pid": os.getpid(), "ended": False}
        payload.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_no_file_is_no_reading(self) -> None:
        self.assertIsNone(guard.heartbeat_reading(self.path))
        self.assertIsNone(guard.heartbeat_age_minutes(self.path))

    def test_a_file_without_a_timestamp_is_no_reading(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{}", encoding="utf-8")
        self.assertIsNone(guard.heartbeat_reading(self.path),
                          "a heartbeat with no 'at' was read as a time")

    def test_a_corrupt_file_is_no_reading(self) -> None:
        """Unreadable JSON, and JSON that is not an object: both are *no reading*, not protection."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for payload in ("{ not json at all", "[1, 2, 3]", '{"at": "not-a-timestamp"}'):
            with self.subTest(payload=payload):
                self.path.write_text(payload, encoding="utf-8")
                self.assertIsNone(guard.heartbeat_reading(self.path), payload)
                self.assertIsNone(guard.heartbeat_age_minutes(self.path), payload)

    def test_a_fresh_heartbeat_from_a_live_pid_is_alive(self) -> None:
        self.write()
        reading = guard.heartbeat_reading(self.path)
        self.assertTrue(reading.alive, reading)
        self.assertEqual(reading.pid, os.getpid())
        self.assertFalse(reading.ended)

    def test_a_finished_guard_is_not_alive_even_with_a_live_pid(self) -> None:
        """The verdict: the writer said it finished. The pid still runs (this process wrote it)."""
        self.write(ended=True, ended_at=iso(now_utc()))
        reading = guard.heartbeat_reading(self.path)
        self.assertFalse(reading.alive, reading)
        self.assertTrue(reading.ended)

    def test_a_dead_pid_is_not_alive_even_with_a_fresh_age(self) -> None:
        """The backstop for a crash: nothing marked it ended, the age is seconds, the pid is gone."""
        self.write(pid=dead_pid())
        reading = guard.heartbeat_reading(self.path)
        self.assertFalse(reading.alive, reading)
        self.assertFalse(reading.running, reading)
        self.assertLess(reading.age_minutes, 5.0)

    def test_a_stale_heartbeat_from_a_live_pid_is_alive_but_silent(self) -> None:
        """Age is the secondary signal: the process runs, so it is alive; the silence is reported."""
        self.write(at=iso(now_utc() - timedelta(minutes=12)))
        reading = guard.heartbeat_reading(self.path)
        self.assertTrue(reading.alive, reading)
        self.assertGreater(reading.age_minutes, 5.0)


class StatusReadsTheVerdict(HeartbeatCase):
    def machine(self) -> FakeProvider:
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})
        return provider

    def test_a_finished_guard_is_not_reported_as_alive(self) -> None:
        """The reproduction: a released wait guard leaves a fresh heartbeat, and it said it ended."""
        provider = self.machine()
        self.heartbeat(ended=True, ended_at=iso(now_utc()))

        code, out = self.status(provider)

        self.assertNotIn("alive", out.lower(),
                         f"a finished guard was reported as protection:\n{out}")
        self.assertIn("finished", out.lower(), f"the marker is not named:\n{out}")
        self.assertIn("nothing is watching", out.lower(), f"the consequence is not stated:\n{out}")

    def test_a_dead_pid_is_not_reported_as_alive(self) -> None:
        provider = self.machine()
        self.heartbeat(pid=dead_pid())

        code, out = self.status(provider)

        self.assertNotIn("alive", out.lower(),
                         f"a heartbeat from a gone process was reported as protection:\n{out}")
        self.assertIn("not running", out.lower(), f"the reason is not stated:\n{out}")

    def test_a_heartbeat_with_no_pid_is_not_reported_as_alive(self) -> None:
        """A pre-upgrade or hand-written heartbeat cannot satisfy "that pid is running"."""
        provider = self.machine()
        self.heartbeat(pid=None)

        code, out = self.status(provider)

        self.assertNotIn("alive", out.lower(), f"a pid-less heartbeat read as protection:\n{out}")

    def test_a_live_guard_is_reported_as_alive_with_its_age(self) -> None:
        provider = self.machine()
        self.heartbeat()

        code, out = self.status(provider)

        self.assertIn("alive", out.lower(), f"a live guard was not reported:\n{out}")
        self.assertIn("heartbeat", out.lower(), f"the age is not shown:\n{out}")

    def test_a_stale_heartbeat_from_a_live_pid_is_reported_as_silent_not_healthy(self) -> None:
        """The process is there, so the verdict is not "gone" — but nothing is watching either.

        `alive` is reserved for a running guard whose heartbeat is fresh; a wedged one is reported as
        running **and** silent, so the word never covers a stale file.
        """
        provider = self.machine()
        self.heartbeat(at=iso(now_utc() - timedelta(minutes=12)))

        code, out = self.status(provider)

        self.assertIn("running", out.lower(), f"the process fact is not stated:\n{out}")
        self.assertNotIn("alive", out.lower(),
                         f"a 12-minute silence was reported as protection:\n{out}")
        self.assertTrue(any(word in out.lower() for word in ("silent", "stale", "wedged")),
                        f"a 12-minute silence was reported as healthy:\n{out}")

    def test_doctor_says_the_same_thing(self) -> None:
        provider = self.machine()
        self.heartbeat(ended=True, ended_at=iso(now_utc()))

        code, out = self.run_cmd(cli.cmd_doctor, ["doctor"], provider)

        self.assertNotIn("alive", out.lower(), f"doctor reported a finished guard as alive:\n{out}")
        self.assertIn("finished", out.lower(), f"doctor does not name the marker:\n{out}")


class TheArmedClaimNeedsEveryFact(HeartbeatCase):
    """`armed_before` is a claim about protection: not ended, pid running, **and** fresh.

    The freshness clause is what catches a guard that crashed and whose pid the OS later handed to an
    unrelated process — the pid is running and nothing marked it ended, but the heartbeat is old.
    """

    def watch_once(self, provider) -> dict:
        args = cli.build_parser().parse_args(["watch", "--once", "--json"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.cmd_watch(args)
        text = out.getvalue()
        return json.loads(text[text.index("{"):]) if "{" in text else {}

    def provider(self) -> FakeProvider:
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})
        return provider

    def armed_before(self, **fields) -> bool:
        self.heartbeat(**fields)
        document = self.watch_once(self.provider())
        return bool(document.get("armed_before"))

    def test_a_live_fresh_unended_guard_is_armed(self) -> None:
        self.assertTrue(self.armed_before(), "a live, fresh guard was not read as armed")

    def test_a_finished_guard_is_not_armed(self) -> None:
        self.assertFalse(self.armed_before(ended=True, ended_at=iso(now_utc())),
                         "a finished guard was read as armed")

    def test_a_dead_pid_is_not_armed(self) -> None:
        self.assertFalse(self.armed_before(pid=dead_pid()),
                         "a heartbeat from a gone process was read as armed")

    def test_a_stale_heartbeat_is_not_armed_even_with_a_live_pid(self) -> None:
        """The reused-pid case: running, unended, and old — not a claim anyone can make."""
        self.assertFalse(self.armed_before(at=iso(now_utc() - timedelta(minutes=30))),
                         "a 30-minute-old heartbeat was read as armed")

    def test_no_heartbeat_at_all_is_not_armed(self) -> None:
        document = self.watch_once(self.provider())
        self.assertFalse(document.get("armed_before"), document)


class GuardsMarkThemselvesEnded(HeartbeatCase):
    def test_a_watch_that_returns_marks_its_heartbeat_ended(self) -> None:
        """A foreground `watch` that stops at the cap is finished, not protection."""
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})
        self.ledger.open(purpose="probe", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc() - timedelta(hours=1))
        self.cfg.budget.per_run_cap_usd = 0.01

        self.run_cmd(cli.cmd_watch, ["watch", "--once"], provider)

        payload = json.loads(self.cfg.heartbeat_path.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("ended"),
                        f"a watch that returned left a heartbeat reading as protection: {payload}")

    def test_up_releases_its_wait_guard_as_finished(self) -> None:
        """The original reproduction, end to end: `up` then `status` must not say `alive`."""
        class SlowWait(FakeProvider):
            def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
                import time
                time.sleep(0.4)          # long enough for the wait guard to poll once
                return super().wait_running(instance_id, timeout_minutes, poll_s, log)

        provider = SlowWait(rate_usd_per_hour=0.13)

        code, out = self.run_cmd(cli.cmd_up, ["up", "--purpose", "probe"], provider)
        self.assertEqual(code, 0, f"the rent failed:\n{out}")
        self.assertTrue(self.cfg.heartbeat_path.exists(),
                        "no wait guard wrote a heartbeat, so this test proves nothing")

        code, out = self.status(provider)
        self.assertNotIn("alive", out.lower(),
                         f"`up`'s released wait guard still reads as protection:\n{out}")
        self.assertIn("finished", out.lower(), f"the marker is not reported:\n{out}")


if __name__ == "__main__":
    unittest.main()
