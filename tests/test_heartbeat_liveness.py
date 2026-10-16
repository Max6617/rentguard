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
import time
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

    def test_only_an_integer_is_a_pid(self) -> None:
        """A float is not a pid: `int(1.5)` is 1, and on POSIX pid 1 is a live process.

        Measured on CI at `e230f80`: `pid_running(1.5)` was True on Linux (pid 1 → PermissionError →
        the "alive" branch) and False on Windows. That is a false *alive* — the dangerous direction —
        so the probe takes integers only, with no float truncation and no booleans.

        The `kill` seam is deliberately not used: these values must be refused before any probe runs,
        which is what makes the test mean the same thing on both platforms.
        """
        for value in (1.5, 2.0, 0.5, "42", True, False, b"42"):
            with self.subTest(pid=value):
                self.assertFalse(guard.pid_running(value), f"{value!r} was read as a pid")

    def test_a_probe_that_raises_something_else_is_gone_not_a_traceback(self) -> None:
        """POSIX `os.kill` raises **OverflowError** for a pid that does not fit a C int.

        A heartbeat carrying such a pid — hand-edited, truncated, a foreign writer — must read as no
        proof of life, not propagate out of `heartbeat_reading` and take `status` and `doctor` with it
        (Assay's finding 15, 2026-10-16). The `kill` seam is how this host sees the POSIX shape.
        """
        def overflowing(pid, sig):
            raise OverflowError("signed integer is greater than maximum")
        self.assertFalse(guard.pid_running(2 ** 40, kill=overflowing),
                         "a pid the probe cannot signal was called running")

    def test_a_heartbeat_with_an_unsignallable_pid_reads_as_no_proof_of_life(self) -> None:
        def overflowing(pid, sig):
            raise OverflowError("signed integer is greater than maximum")
        path = Path(scratch_dir(self)) / "heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": iso(now_utc()), "pid": 2 ** 40, "ended": False}),
                        encoding="utf-8")

        reading = guard.heartbeat_reading(path, kill=overflowing)     # must not raise

        self.assertFalse(reading.running, reading)
        self.assertFalse(reading.alive, reading)

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

    def test_a_guard_id_is_an_identity_a_second_guard_in_this_process_does_not_share(self) -> None:
        """Two guards in one process share the pid, so the pid cannot tell them apart.

        Releasing one must not mark the other's file finished (Assay's finding 17, 2026-10-16). The
        guard's own id is the identity they do not share.
        """
        guard.write_heartbeat(self.path, guard.snapshot_fallback(
            Config(state_dir=self.path.parent), Ledger(self.path.parent / "runs.jsonl")),
            guard_id="second")

        self.assertFalse(guard.end_heartbeat(self.path, guard_id="first"),
                         "a guard's exit marked another guard's file in the same process")
        self.assertFalse(json.loads(self.path.read_text(encoding="utf-8")).get("ended"))
        self.assertTrue(guard.end_heartbeat(self.path, guard_id="second"),
                        "the guard that wrote the file could not mark its own")
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

    def test_the_structured_stale_flag_follows_the_prose(self) -> None:
        """`stale` is the field a script or a Bot gates on, so it must not contradict the sentence.

        A live, unmarked, 60-minute-old heartbeat is `wedged` in prose; `alive` (the process fact) is
        true, but `stale` must stay the **age** it always was (Assay's finding 16, 2026-10-16).
        """
        provider = self.machine()
        self.heartbeat(at=iso(now_utc() - timedelta(minutes=60)))

        args = cli.build_parser().parse_args(["status", "--json"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.cmd_status(args)
        document = json.loads(out.getvalue()[out.getvalue().index("{"):])

        self.assertTrue(document["guard"]["stale"],
                        f"a wedged guard reads as not stale in the JSON a script gates on: "
                        f"{document['guard']}")
        self.assertTrue(document["guard"]["running"], document["guard"])
        self.assertFalse(document["guard"]["ended"], document["guard"])

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

        files = guard.heartbeat_files(self.cfg.heartbeat_path)
        self.assertTrue(files, "a watch that ran wrote no heartbeat file")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
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
        self.assertTrue(guard.heartbeat_files(self.cfg.heartbeat_path),
                        "no wait guard wrote a heartbeat file, so this test proves nothing")

        code, out = self.status(provider)
        self.assertNotIn("alive", out.lower(),
                         f"`up`'s released wait guard still reads as protection:\n{out}")
        self.assertIn("finished", out.lower(), f"the marker is not reported:\n{out}")


    def test_releasing_one_wait_guard_leaves_a_live_second_one_alive(self) -> None:
        """Two real wait guards, one heartbeat path, one process: the shared-pid case end to end."""
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.write_state({"instance_id": "pod-A"})
        ledger = Ledger(self.cfg.ledger_path)

        first = cli._start_wait_guard(self.cfg, provider, ledger, "pod-A",
                                     log=lambda *_: None, interval=0.05)
        second = cli._start_wait_guard(self.cfg, provider, ledger, "pod-A",
                                       log=lambda *_: None, interval=5.0)
        time.sleep(0.4)                       # both have written
        cli._release_wait_guard(first)
        time.sleep(0.05)
        reading = guard.heartbeat_reading(self.cfg.heartbeat_path)
        second_still_running = second[0].is_alive()
        cli._release_wait_guard(second)

        self.assertTrue(second_still_running, "fixture: the second guard had already stopped")
        self.assertTrue(reading is not None and reading.alive,
                        f"the still-running second guard reads as finished: "
                        f"ended={reading and reading.ended}, pid={reading and reading.pid}")


class TheGuardSet(unittest.TestCase):
    """One heartbeat file per guard, and the reader takes the best of the set (finding 17).

    Two guards can watch one instance in one process — an operator's `watch` beside `up`'s wait guard,
    or two wait guards — where the pid and the instance id are both shared. One file let the guard that
    finished mark the other's reading `finished`; one file per guard is the identity, and a reader that
    asks "is anything watching?" takes the best of them.
    """

    def setUp(self) -> None:
        self.cfg = Config(state_dir=scratch_dir(self))
        self.path = self.cfg.heartbeat_path

    def guard_file(self, guard_id: str, *, pid=None, ended: bool = False,
                   age_minutes: float = 0.0) -> Path:
        target = guard.guard_heartbeat_path(self.path, guard_id)
        payload = {"at": iso(now_utc() - timedelta(minutes=age_minutes)),
                   "pid": os.getpid() if pid is None else pid, "ended": ended,
                   "guard_id": guard_id}
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def snapshot(self):
        return guard.snapshot_fallback(self.cfg, Ledger(self.cfg.ledger_path))

    def test_a_finished_guard_does_not_hide_a_live_one(self) -> None:
        self.guard_file("first", ended=True)
        self.guard_file("second")

        reading = guard.heartbeat_reading(self.path)

        self.assertTrue(reading.alive, f"a live guard was hidden by a finished one: {reading}")

    def test_two_live_guards_report_the_fresher(self) -> None:
        self.guard_file("slow", age_minutes=3.0)
        self.guard_file("fast", age_minutes=0.1)

        reading = guard.heartbeat_reading(self.path)

        self.assertLess(reading.age_minutes, 1.0, f"the stale guard won: {reading}")

    def test_when_every_guard_has_finished_the_newest_is_reported(self) -> None:
        self.guard_file("old", ended=True, age_minutes=30.0)
        self.guard_file("new", ended=True, age_minutes=0.5)

        reading = guard.heartbeat_reading(self.path)

        self.assertTrue(reading.ended, reading)
        self.assertLess(reading.age_minutes, 1.0, f"the older marker was reported: {reading}")

    def test_a_legacy_single_file_is_still_read(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"at": iso(now_utc()), "pid": os.getpid(),
                                         "ended": False}), encoding="utf-8")

        self.assertEqual(guard.heartbeat_files(self.path), [self.path])
        self.assertTrue(guard.heartbeat_reading(self.path).alive)

    def test_a_heartbeat_pruned_between_the_listing_and_the_stat_is_skipped(self) -> None:
        """Every write prunes the set, so a reader can list the directory while another guard removes
        a file from it. The set is built from the files that still answer a stat, so the read reports
        the survivor instead of raising ``FileNotFoundError`` (Assay's finding, 2026-10-16): a gone
        file is not a heartbeat, and a crash here would take ``status`` and ``doctor`` with it.
        """
        survivor = self.guard_file("survivor")
        doomed = self.guard_file("doomed")
        real_glob = Path.glob

        def glob_then_prune(directory, pattern):
            found = list(real_glob(directory, pattern))
            for candidate in found:
                if candidate == doomed:          # the concurrent prune, right here
                    candidate.unlink()
            return iter(found)

        with mock.patch.object(Path, "glob", glob_then_prune):
            files = guard.heartbeat_files(self.path)
            reading = guard.heartbeat_reading(self.path)

        self.assertEqual(files, [survivor], "a path that no longer answers a stat was still listed")
        self.assertIsNotNone(reading, "the surviving guard was lost with the pruned file")
        self.assertTrue(reading.alive, reading)

    def test_the_writer_prunes_a_finished_sibling_but_not_a_live_or_crashed_one(self) -> None:
        finished = self.guard_file("finished", ended=True, age_minutes=30.0)
        live = self.guard_file("live")
        crashed = self.guard_file("crashed", pid=dead_pid())        # unmarked, and gone

        guard.write_heartbeat(guard.guard_heartbeat_path(self.path, "writer"), self.snapshot())

        self.assertFalse(finished.exists(),
                         "a finished marker older than the window was left to accumulate")
        self.assertTrue(live.exists(), "a live guard's heartbeat was pruned")
        self.assertTrue(crashed.exists(),
                        "a crashed guard's heartbeat was pruned: the pid is evidence worth keeping")


if __name__ == "__main__":
    unittest.main()
