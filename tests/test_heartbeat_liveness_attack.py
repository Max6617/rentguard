"""Attack the liveness rule at `75d6d37` (code `a45c26e`): liveness is a process fact.

Gilbert's ruling replaced the age as the verdict with the writer's own mark and its pid. These
attack the three seams the change creates — the pid probe (with its `kill` seam), the structured
verdict fields, and two guards sharing one heartbeat path — plus the controls that must keep
passing. Offline: in-memory fake, hand-written heartbeat files, no rental.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, iso, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cfg_for(root: Path) -> Config:
    cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                 state_dir=root / ".rentguard")
    cfg.provider = "fake"
    cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
    return cfg


def write_heartbeat(path: Path, *, pid=None, ended: bool | None = None,
                    age_minutes: float = 0.0, at: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = at or iso(now_utc() - timedelta(minutes=age_minutes))
    payload = {"at": stamp, "note": "fixture"}
    if pid is not None or pid is False:
        payload["pid"] = pid
    if ended is not None:
        payload["ended"] = ended
    path.write_text(json.dumps(payload), encoding="utf-8")


class PidProbeSeams(unittest.TestCase):
    """`pid_running`'s three answers, and the fourth a probe can produce."""

    @staticmethod
    def raising(exc):
        def kill(pid, sig):
            raise exc
        return kill

    def test_permission_error_is_alive(self) -> None:
        self.assertIs(guard.pid_running(4321, kill=self.raising(PermissionError())), True,
                      "a live process we may not signal was called gone")

    def test_process_lookup_is_gone(self) -> None:
        self.assertIs(guard.pid_running(4321, kill=self.raising(ProcessLookupError())), False)

    def test_a_plain_oserror_is_gone(self) -> None:
        self.assertIs(guard.pid_running(4321, kill=self.raising(OSError(22, "EINVAL"))), False)

    def test_a_probe_that_raises_another_error_is_gone_not_a_traceback(self) -> None:
        """POSIX `os.kill` raises **OverflowError**, not OSError, for a pid that does not fit a C
        int: a heartbeat carrying such a pid (hand-edited, truncated, or a foreign writer) crashes
        `status` and `doctor` on Linux if the probe lets it through."""
        def kill(pid, sig):
            raise OverflowError("signed integer is greater than maximum")
        try:
            answer = guard.pid_running(2 ** 40, kill=kill)
        except OverflowError as exc:
            self.fail(f"a pid the probe cannot signal crashed the read with {type(exc).__name__}: "
                      f"{exc}; every failure that is not PermissionError must read as gone")
        self.assertIs(answer, False)

    def test_things_that_are_not_pids_are_gone(self) -> None:
        for value in (None, "", "abc", 0, -1, 1.5):
            with self.subTest(pid=value):
                self.assertIs(guard.pid_running(value), False)

    def test_only_a_real_integer_is_a_pid_and_the_read_path_agrees(self) -> None:
        """The lesson from this file's own CI red: `test_things_that_are_not_pids_are_gone` used the
        real probe for 1.5, `int(1.5)` truncated it to pid 1, and pid 1 is *alive* on POSIX
        (PermissionError) while Windows answers EINVAL — so the seam-free case was green here and red
        there. This pins the cross-platform truth without a seam, and the read path with it."""
        self.assertIs(guard.pid_running(os.getpid()), True,
                      "the reader's own pid was called gone")
        for value in (1.5, 2.0, 0.5, "42", b"42", True, False):
            with self.subTest(pid=value):
                self.assertIs(guard.pid_running(value), False, f"{value!r} was read as a pid")

        path = cfg_for(scratch_dir(self)).heartbeat_path
        write_heartbeat(path, pid=1234.0, ended=False, age_minutes=0.0)   # a JSON float
        reading = guard.heartbeat_reading(path)
        self.assertIs(reading.pid, None, f"a float pid survived the read: {reading}")
        self.assertIs(reading.running, False)
        self.assertIs(reading.alive, False)


class TheStructuredStaleFlag(unittest.TestCase):
    """The prose verdict handles age; the machine-readable `stale` field must too, because that is
    what a script or an agent gates on."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)

    def test_a_wedged_guard_is_not_reported_as_not_stale(self) -> None:
        write_heartbeat(self.cfg.heartbeat_path, pid=os.getpid(), ended=False, age_minutes=60.0)

        reading = guard.heartbeat_reading(self.cfg.heartbeat_path)
        line, facts = cli._guard_report(reading)

        self.assertIn("wedged", line, f"a silent guard was not called wedged: {line!r}")
        self.assertIs(facts["stale"], True,
                      f"the structured verdict says a guard silent for 60 min is not stale: {facts}")

    def test_status_json_does_not_call_a_wedged_guard_not_stale(self) -> None:
        write_heartbeat(self.cfg.heartbeat_path, pid=os.getpid(), ended=False, age_minutes=60.0)
        provider = FakeProvider(rate_usd_per_hour=2.0)
        args = cli.build_parser().parse_args(["status", "--json"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.cmd_status(args)
        text = out.getvalue()
        document = json.loads(text[text.index("{"):]) if "{" in text else {}
        self.assertIs(document.get("guard", {}).get("stale"), True,
                      f"status told a script the wedged guard is not stale: {document.get('guard')}")


class TwoGuardsShareOnePath(unittest.TestCase):
    def test_releasing_one_guard_does_not_mark_a_live_other_finished(self) -> None:
        """Two guards in one process share the pid, so `end_heartbeat`'s pid check cannot tell them
        apart: the one that finishes marks the file for both."""
        cfg = cfg_for(scratch_dir(self))
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        ledger = Ledger(cfg.ledger_path)

        first = cli._start_wait_guard(cfg, provider, ledger, "pod-A",
                                      log=lambda *_: None, interval=0.05)
        second = cli._start_wait_guard(cfg, provider, ledger, "pod-A",
                                       log=lambda *_: None, interval=5.0)
        time.sleep(0.4)                        # both have written a heartbeat
        cli._release_wait_guard(first)         # marks the file ended, same pid
        time.sleep(0.05)
        reading = guard.heartbeat_reading(cfg.heartbeat_path)
        second_alive = second[0].is_alive()
        cli._release_wait_guard(second)

        self.assertTrue(second_alive, "fixture: the second guard was not running")
        self.assertTrue(
            reading is not None and reading.alive,
            f"the still-running second guard reads as finished: ended={reading and reading.ended}, "
            f"pid={reading and reading.pid} — releasing one guard marked the shared file for both")


class ControlsThatMustKeepPassing(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)

    def test_a_payload_with_no_pid_is_not_protection(self) -> None:
        write_heartbeat(self.cfg.heartbeat_path, pid=False, age_minutes=0.0)   # no pid key at all

        reading = guard.heartbeat_reading(self.cfg.heartbeat_path)
        line, facts = cli._guard_report(reading)

        self.assertIs(reading.running, False)
        self.assertIs(reading.alive, False)
        self.assertIn("not running", line, f"a pid-less reading was not called not-running: {line!r}")

    def test_end_heartbeat_leaves_another_writers_file_alone_and_a_missing_file_missing(self) -> None:
        path = self.cfg.heartbeat_path
        write_heartbeat(path, pid=os.getpid() + 1 if os.getpid() != 12345 else 12345, ended=False)

        self.assertFalse(guard.end_heartbeat(path), "another pid's file was marked")
        self.assertIs(guard.heartbeat_reading(path).ended, False, "another pid's file was marked")

        missing = self.root / "nope.json"
        self.assertFalse(guard.end_heartbeat(missing), "a missing file was marked")
        self.assertFalse(missing.exists(), "a marker was invented for a file nobody wrote")

    def test_armed_before_needs_the_mark_the_pid_and_the_age(self) -> None:
        cases = [
            (dict(pid=os.getpid(), ended=False, age_minutes=0.0), True, "unmarked, live, fresh"),
            (dict(pid=os.getpid(), ended=False, age_minutes=60.0), False, "live but silent"),
            (dict(pid=os.getpid(), ended=True, age_minutes=0.0), False, "marked finished"),
            (dict(pid=None, ended=False, age_minutes=0.0), False, "no pid"),
        ]
        for kwargs, expected, label in cases:
            with self.subTest(label=label):
                self.setUp()
                provider = FakeProvider(rate_usd_per_hour=2.0)
                provider.add("pod-A", uptime_seconds=600, status="RUNNING")
                self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
                Ledger(self.cfg.ledger_path).open(purpose="probe", provider="fake",
                                                  instance_id="pod-A", gpu_id="g",
                                                  rate_usd_per_hour=2.0)
                write_heartbeat(self.cfg.heartbeat_path, **kwargs)
                args = cli.build_parser().parse_args(["watch", "--once", "--interval", "60",
                                                      "--json"])
                out = io.StringIO()
                with mock.patch.object(cli, "_config", lambda: self.cfg), \
                        mock.patch.object(cli, "_provider", lambda c: provider), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    cli.cmd_watch(args)
                text = out.getvalue()
                document = json.loads(text[text.index("{"):]) if "{" in text else {}
                self.assertIs(document.get("armed_before"), expected,
                              f"{label}: armed_before={document.get('armed_before')!r} guessed wrong")


    def test_the_disclosed_handle_caveat_reads_alive_only_while_the_file_is_fresh(self) -> None:
        """Forge's disclosed limit: on Windows an exited process whose handle is held elsewhere can
        be probed successfully, so a killed guard can read as running. This pins the shape and the
        bound that covers the *armed* claim: freshness. It asserts the caveat, not a requirement."""
        path = self.cfg.heartbeat_path
        write_heartbeat(path, pid=999999, ended=False, age_minutes=0.0)
        with mock.patch.object(guard, "pid_running", lambda pid, **k: True):
            fresh = guard.heartbeat_reading(path)
            line, facts = cli._guard_report(fresh)
        self.assertIn("alive", line, f"the caveat did not read alive: {line!r}")
        self.assertIs(facts["stale"], False, facts)

        write_heartbeat(path, pid=999999, ended=False, age_minutes=60.0)
        with mock.patch.object(guard, "pid_running", lambda pid, **k: True):
            stale = guard.heartbeat_reading(path)
            line, _ = cli._guard_report(stale)
        self.assertIn("wedged", line,
                      f"the freshness clause did not cover the caveat: {line!r}")


class TheGuardSetAtE230f80(unittest.TestCase):
    """One file per guard, best-of ordering and pruning. Attacks the read against a concurrent prune,
    the ordering when a crashed guard and a finished one coexist, and what a caller is told about the
    guards that are not the reported one."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)

    def test_a_heartbeat_removed_between_the_listing_and_the_stat_does_not_crash_the_read(self) -> None:
        """Every `write_heartbeat` prunes the set, so `status`/`doctor` can list the directory while
        another process removes a file from it. `heartbeat_files` sorts by `p.stat()`, and a file gone
        between the glob and the stat raises FileNotFoundError out of the whole read."""
        path = self.cfg.heartbeat_path
        write_heartbeat(guard.guard_heartbeat_path(path, "ended-guard"),
                        pid=os.getpid(), ended=True, age_minutes=30.0)
        real_glob = Path.glob

        def glob_then_a_guard_prunes(directory, pattern):
            found = list(real_glob(directory, pattern))
            for candidate in found:              # the concurrent prune, right here
                try:
                    candidate.unlink()
                except OSError:
                    pass
            return iter(found)

        try:
            with mock.patch.object(Path, "glob", glob_then_a_guard_prunes):
                reading = guard.heartbeat_reading(path)
        except FileNotFoundError as exc:
            self.fail(f"a heartbeat pruned between the listing and the stat crashed the read with "
                      f"{type(exc).__name__}: {exc}; the read must skip a file that is gone")
        self.assertIsNone(reading, "no file survived, so the reading should be None")

    def test_the_newest_of_a_crashed_and_a_finished_guard_is_reported(self) -> None:
        """A crashed guard is a dead pid with no mark; a finished one is a live pid with the mark.
        Neither is alive, so the newest is what a caller is shown."""
        path = self.cfg.heartbeat_path
        cases = [
            (1.0, 20.0, "not running", "the crashed guard is newer"),
            (20.0, 1.0, "finished", "the finished guard is newer"),
        ]
        for crashed_age, finished_age, wanted, label in cases:
            with self.subTest(label=label):
                self.setUp()
                path = self.cfg.heartbeat_path
                write_heartbeat(guard.guard_heartbeat_path(path, "crashed"),
                                pid=999999, ended=False, age_minutes=crashed_age)
                write_heartbeat(guard.guard_heartbeat_path(path, "finished"),
                                pid=os.getpid(), ended=True, age_minutes=finished_age)
                line, facts = cli._guard_report(guard.heartbeat_reading(path))
                self.assertIn(wanted, line, f"{label}: {line!r}")

    def test_a_crashed_guard_beside_a_live_one_is_not_reported_at_all(self) -> None:
        """Records what a caller is told today, for Forge's design question: the best-of returns the
        live guard, and nothing in the line or the facts says another guard crashed."""
        path = self.cfg.heartbeat_path
        write_heartbeat(guard.guard_heartbeat_path(path, "crashed"),
                        pid=999999, ended=False, age_minutes=8.0)
        write_heartbeat(guard.guard_heartbeat_path(path, "live"),
                        pid=os.getpid(), ended=False, age_minutes=0.2)

        line, facts = cli._guard_report(guard.heartbeat_reading(path))

        self.assertIn("alive", line, line)
        self.assertNotIn("999999", line, f"the crashed guard is named after all: {line!r}")
        self.assertNotIn("guards", facts, f"the facts now carry a count: {facts}")

    def test_the_read_path_takes_the_probe_seam(self) -> None:
        """Finding 15's second half: the probe is a seam on the read path, so the POSIX OverflowError
        shape cannot reach `status` or `doctor`."""
        path = self.cfg.heartbeat_path
        write_heartbeat(path, pid=2 ** 40, ended=False, age_minutes=0.0)

        def overflow(pid, sig):
            raise OverflowError("signed integer is greater than maximum")

        reading = guard.heartbeat_reading(path, kill=overflow)

        self.assertIsNotNone(reading)
        self.assertIs(reading.running, False, "an unsignalable pid read as running")
        self.assertIs(reading.alive, False)
        self.assertIn("not running", cli._guard_report(reading)[0])

    def test_a_reused_pid_in_a_crashed_guard_file_reads_alive_only_while_fresh(self) -> None:
        """The disclosed caveat on the set: a crashed guard's file is kept as evidence, and if its pid
        is reused by a live process the probe cannot tell. The freshness window bounds it."""
        path = self.cfg.heartbeat_path
        write_heartbeat(guard.guard_heartbeat_path(path, "crashed"), pid=999999,
                        ended=False, age_minutes=0.5)
        with mock.patch.object(guard, "pid_running", lambda pid, **k: True):
            line, facts = cli._guard_report(guard.heartbeat_reading(path))
        self.assertIn("alive", line, line)

        write_heartbeat(guard.guard_heartbeat_path(path, "crashed"), pid=999999,
                        ended=False, age_minutes=8.0)
        with mock.patch.object(guard, "pid_running", lambda pid, **k: True):
            reading = guard.heartbeat_reading(path)
            line, facts = cli._guard_report(reading)
        self.assertIn("wedged", line, line)
        self.assertIs(facts["stale"], True, facts)

    def test_the_prune_removes_a_finished_old_file_and_keeps_a_crashed_one(self) -> None:
        """Forge's claim, pinned: a finished marker older than the window is pruned; a crashed
        guard's file (dead pid, no mark) stays as evidence."""
        path = self.cfg.heartbeat_path
        ended_old = guard.guard_heartbeat_path(path, "ended-old")
        crashed = guard.guard_heartbeat_path(path, "crashed")
        write_heartbeat(ended_old, pid=os.getpid(), ended=True, age_minutes=30.0)
        write_heartbeat(crashed, pid=999999, ended=False, age_minutes=30.0)

        guard._prune_finished_heartbeats(guard.guard_heartbeat_path(path, "writer"))

        self.assertFalse(ended_old.exists(), "the finished old marker was not pruned")
        self.assertTrue(crashed.exists(), "the crashed guard's evidence was pruned")


class TheDoctorSetLineAt04925b8(unittest.TestCase):
    """`doctor` names the whole set when more than one reading exists. Attacks: what counts as a
    reading, whether the line can be read as a second verdict, and whether it stays out of the
    machine-readable surfaces."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)
        self.provider = FakeProvider(rate_usd_per_hour=2.0)
        self.provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        # The pod is recorded by the state *and* an open row, so doctor has no drift to report and its
        # exit code measures the set line rather than an unrecorded machine.
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        Ledger(self.cfg.ledger_path).open(purpose="probe", provider="fake", instance_id="pod-A",
                                          gpu_id="g", rate_usd_per_hour=2.0)

    def doctor(self, argv=("doctor", "--json")):
        args = cli.build_parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: self.provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_doctor(args)
        return code, out.getvalue(), err.getvalue()

    def per_guard(self, name, **kwargs):
        write_heartbeat(guard.guard_heartbeat_path(self.cfg.heartbeat_path, name), **kwargs)

    def test_a_stale_legacy_file_does_not_make_the_set_line_permanent(self) -> None:
        """The legacy `heartbeat.json` is read forever and never pruned (`_prune_finished_heartbeats`
        globs `heartbeat-*.json` only), so a state dir that has one from an older revision counts two
        readings for ever — one live guard, one piece of residue — and `doctor` prints a set line on
        every run. The line's own words then overstate the number of guards."""
        write_heartbeat(self.cfg.heartbeat_path, pid=os.getpid(), ended=True, age_minutes=30.0)
        self.per_guard("watch-abc", pid=os.getpid(), ended=False, age_minutes=0.1)

        readings = guard.heartbeat_readings(self.cfg.heartbeat_path)
        line = cli._guard_set_line(readings)

        self.assertIsNone(
            line,
            f"one live guard plus an old finished legacy file is reported as a set: {line!r} — the "
            f"legacy file is never pruned, so this line is permanent and its count is files, not "
            f"guards")

    def test_the_set_line_stays_out_of_json_and_does_not_change_the_exit(self) -> None:
        self.per_guard("live", pid=os.getpid(), ended=False, age_minutes=0.1)
        self.per_guard("crashed", pid=999999, ended=False, age_minutes=8.0)

        code, out, err = self.doctor()

        document = json.loads(out[out.index("{"):]) if "{" in out else {}
        self.assertIn("guard set", err, f"the set line is not in the prose: {err!r}")
        self.assertNotIn("guard set", out, "the set line leaked into the JSON document")
        self.assertNotIn("guard_set", document, f"the document grew a set key: {document}")
        self.assertNotIn("guard set", json.dumps(document), document)
        self.assertEqual(code, 0, f"the diagnostic changed doctor's exit code:\n{err}")

    def test_the_set_line_is_not_on_status(self) -> None:
        self.per_guard("live", pid=os.getpid(), ended=False, age_minutes=0.1)
        self.per_guard("crashed", pid=999999, ended=False, age_minutes=8.0)
        args = cli.build_parser().parse_args(["status", "--json"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: self.provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.cmd_status(args)
        self.assertNotIn("guard set", out.getvalue() + err.getvalue(),
                         "the set line appeared on status")

    def test_a_corrupt_member_is_not_counted_and_a_pid_less_one_is(self) -> None:
        """A file that will not parse is not a reading at all, so it cannot be counted; a legacy
        reading with no pid is one, and it says so."""
        self.per_guard("live", pid=os.getpid(), ended=False, age_minutes=0.1)
        broken = guard.guard_heartbeat_path(self.cfg.heartbeat_path, "broken")
        broken.write_text("{not json", encoding="utf-8")

        self.assertIsNone(cli._guard_set_line(guard.heartbeat_readings(self.cfg.heartbeat_path)),
                          "an unparseable file was counted as a reading")

        legacy = self.cfg.heartbeat_path
        legacy.write_text(json.dumps({"at": iso(now_utc()), "note": "legacy, no pid"}),
                          encoding="utf-8")
        line = cli._guard_set_line(guard.heartbeat_readings(legacy))
        self.assertIsNotNone(line, "the pid-less legacy reading was not counted")
        self.assertIn("no pid", line, f"a pid-less reading is labelled some other way: {line!r}")
        self.assertIn("not running", line, line)


if __name__ == "__main__":
    unittest.main()
