"""Artefacts lost, the MCP server, and two supervisors at once: the paths that lose results or money.

Written red-first, for the cases nobody has covered yet:

1. the instance vanishes while the command runs and `run` still reports success, so the results are
   assumed present when they are gone (the `run`-finishes-normally case lives in
   `test_money_paths.py::ArtefactPull`, so it is not duplicated here);
2. a tool the MCP server exposes raises, or a client sends a line that is not a JSON object: the
   server must answer an error and keep serving, not die;
3. two guards are watching the same instance at once, which is the shape in which a ledger rewrite
   can lose a row another writer just appended;
4. a guard killed while holding the ledger lock leaves the lock behind, and every later run then has
   to be recorded or the cap reads a total that is missing rows.

Everything here runs against the canonical in-memory provider (`rentguard/fake.py`): no network, no
spend, no second fake to drift from it. Arguments come from `cli.build_parser()`, so a new CLI flag
does not turn a test into a test of its own fixture. Scratch state goes under the workspace, not the
repo, and not under `mkdtemp` — a 0700 temp directory cannot be removed by the confined token this
suite runs under.

A failure here is a finding to report with its observed numbers; the implementation is never adjusted
to make these pass.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import threading
import unittest
import uuid
from argparse import Namespace
from pathlib import Path
from unittest import mock

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))

from rentguard import cli, guard, mcp_server  # noqa: E402
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402

TMPROOT = WORKSPACE / ".assay-tmp"


# --------------------------------------------------------------------- fixtures

def cli_args(*argv: str) -> Namespace:
    """Arguments exactly as the CLI parser builds them.

    Hand-built Namespaces break the moment the CLI grows a flag (it grew `--dry-run` and `--json`
    while this file was being written), and then the test measures its own fixture instead of the
    tool.
    """
    return cli.build_parser().parse_args(list(argv))


class Case(unittest.TestCase):
    """A scratch state directory, a config, a ledger, and a way to call the CLI with a fake."""

    def setUp(self) -> None:
        self.root = TMPROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.cfg = Config(
            budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=10.0),
            state_dir=self.root,
            # The fake's catalog has exactly one GPU; without this the preference list from the
            # real defaults matches nothing and `up` fails for a reason unrelated to the test.
            runpod={"gpu_preference": ["Fake GPU 24GB"]},
        )
        self.ledger = Ledger(self.cfg.ledger_path)
        self.write_state({})

    # ---------------------------------------------------------------- helpers

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def invoke(self, func, args, provider) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
             mock.patch.object(cli, "_provider", lambda cfg: provider), \
             mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
             contextlib.redirect_stdout(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except cli.ProviderError:
                code = 1
        return code, out.getvalue()

    def open_run(self, *, hours_ago: float = 0.5, rate: float = 2.0,
                 instance_id: str = "pod-A") -> dict:
        from datetime import timedelta
        return self.ledger.open(purpose="probe", provider="fake", instance_id=instance_id,
                                gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                                started_at=now_utc() - timedelta(hours=hours_ago))

    def artefacts_cfg(self, *, on_run: bool) -> Path:
        dest = self.root / "artefacts"
        self.cfg.artefacts = Artefacts(pull=["results/**/*.json"], local_dir=str(dest),
                                       remote_dir="/workspace", on_run=on_run)
        return dest

    @staticmethod
    def landed(dest: Path) -> list[str]:
        if not dest.exists():
            return []
        return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())


# ================================================================ PATH 1
# the instance vanishes while the command runs

class ArtefactLostMidRun(Case):
    def test_run_reports_lost_artefacts_instead_of_success(self):
        """Sequence: `rentguard run -- python train.py` on pod-A; part-way through the command the
        pod disappears from the account (a crash, a provider-side kill). Artefacts are configured
        to be pulled after the run.

        Effect required: `run` exits non-zero and says the artefacts were not pulled. Reporting the
        command's exit code as success while the results are gone is how a week of experiments is
        discovered to be missing at the end.
        """
        self.artefacts_cfg(on_run=True)
        self.write_state({"instance_id": "pod-A"})
        self.open_run(hours_ago=0.5)
        fake = FakeProvider()
        fake.add("pod-A", rate_usd_per_hour=2.0, uptime_seconds=1800)
        fake.put("results/out.json", '{"accuracy": 0.9}', instance_id="pod-A")

        def runner(argv, command):
            fake.disappear("pod-A")   # the machine dies with the results still on it
            return 0

        fake.command_runner = runner
        args = cli_args("run", "--", "python", "train.py")

        code, out = self.invoke(cli.cmd_run, args, fake)

        self.assertNotEqual(
            code, 0,
            "run reported success even though the instance vanished mid-run and no artefact was "
            f"pulled. output:\n{out}")
        lowered = out.lower()
        self.assertTrue(
            any(w in lowered for w in ("not pulled", "lost", "artefact")),
            "run failed silently about the results: nothing in the output says the artefacts were "
            f"left on a machine that no longer exists.\noutput:\n{out}")


# ================================================================ PATH 2
# the MCP server must answer, not die

def _tools_list(msg_id: int) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "tools/list"})


class McpServerRobustness(unittest.TestCase):
    def _serve(self, lines: str, patches=()) -> str:
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("sys.stdin", io.StringIO(lines)))
            stack.enter_context(contextlib.redirect_stdout(out))
            for patch in patches:
                stack.enter_context(patch)
            mcp_server.main()
        return out.getvalue()

    def test_a_non_object_json_line_does_not_kill_the_server(self):
        """Sequence: a client sends a JSON array instead of an object, then a normal tools/list.

        Effect required: the second request is answered. The server's own contract is that a
        malformed line must not kill it; dying here leaves the client with a dead server instead of
        an error, which is the one failure the stdio transport cannot report.
        """
        lines = "[1, 2, 3]\n" + _tools_list(7) + "\n"
        try:
            out = self._serve(lines)
        except Exception as exc:  # the server died inside main()
            self.fail(f"a non-object JSON line killed the server: {type(exc).__name__}: {exc}")
        self.assertIn('"id": 7', out, f"the request after the bad line was not answered.\n"
                                     f"responses seen:\n{out}")
        self.assertIn("rentguard_status", out, f"tools/list returned no tool list.\n{out}")

    def test_a_tool_that_raises_is_answered_and_the_server_keeps_serving(self):
        """Sequence: `rentguard_ledger` is called and its command raises (an unreadable state dir,
        a bug); the client then calls tools/list.

        Effect required: the failing call gets a response tied to its id (an error or isError), and
        the next request is still answered. A raise inside a tool must not end the process.
        """
        boom = mock.patch.object(cli, "cmd_ledger", side_effect=RuntimeError("ledger unreadable"))
        call = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "rentguard_ledger", "arguments": {}}})
        try:
            out = self._serve(call + "\n" + _tools_list(4) + "\n", patches=[boom])
        except Exception as exc:
            self.fail(f"a raising tool killed the server: {type(exc).__name__}: {exc}")
        self.assertIn('"id": 3', out, f"the failed call got no response for its id.\n{out}")
        self.assertTrue('"error"' in out or '"isError": true' in out,
                        f"the failed call was reported as a normal result.\n{out}")
        self.assertIn('"id": 4', out, f"the server stopped serving after the raise.\n{out}")
        self.assertIn("rentguard_status", out, f"tools/list returned no tool list.\n{out}")


# ================================================================ PATH 3
# two guards on one instance at the same time

class TwoGuardsAtOnce(Case):
    def test_two_guards_close_the_run_exactly_once(self):
        """Sequence: two supervisors watch pod-A at the same time (a second terminal, a scheduler
        that started `watch` twice). The run is 3h old at $2/h against a $5 per-run cap.

        Effect required: the machine is stopped and the row is closed exactly once, for one cost.
        Two supervisors must not double-count the run, close it twice, or lose the row.
        """
        self.open_run(hours_ago=3.0)   # $6 at $2/h, per-run cap $5
        fake = FakeProvider()
        fake.add("pod-A", rate_usd_per_hour=2.0, uptime_seconds=3 * 3600)
        outcomes: list[str] = []

        def supervisor() -> None:
            try:
                outcomes.append(guard.watch(self.cfg, fake, Ledger(self.cfg.ledger_path),
                                            instance_id="pod-A", interval=0.0,
                                            max_iterations=3, log=lambda *a: None))
            except Exception as exc:  # a supervisor that dies is its own finding
                outcomes.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=supervisor) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        rows = self.ledger.entries()
        closed = [r for r in rows if r.get("stopped_at")]
        total = self.ledger.total_usd()
        self.assertEqual(len(rows), 1,
                         f"two guards produced {len(rows)} ledger rows: {[r.get('run_id') for r in rows]}, "
                         f"outcomes={outcomes!r}")
        self.assertEqual(len(closed), 1, f"the run was not closed exactly once: {outcomes!r}")
        self.assertIsNone(self.ledger.open_run(),
                          f"a row stayed open after both guards finished: {outcomes!r}")
        self.assertAlmostEqual(total, 6.0, delta=0.1,
                               msg=f"the run was recorded as ${total}, not the ~$6.00 it burned "
                                   f"(outcomes={outcomes!r}, stops={fake.actions!r})")


# ================================================================ PATH 4
# the lock a killed guard leaves behind

class StaleLock(Case):
    def test_a_lock_left_by_a_killed_guard_does_not_block_recording(self):
        """Sequence: a guard was killed while holding the ledger lock, so its lock directory
        (`runs.jsonl.lock`) is still there. The operator then runs `up`, which must record a row.

        Effect required: the row is recorded. If a stale lock can stop the ledger from being
        written, then a crashed supervisor silently turns every later run into unrecorded spend —
        the cap reads a total that is missing rows.
        """
        lock = self.cfg.ledger_path.with_name(self.cfg.ledger_path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(lock)   # exactly what a process killed inside Ledger._lock leaves behind
        self.write_state({})
        fake = FakeProvider()

        # Bounded: a blocked ledger is observed as "not finished in 5 s" instead of waiting out the
        # lock's own 30 s timeout. The thread is a daemon so the blocked wait cannot outlive the run.
        outcome: dict = {}

        def attempt() -> None:
            try:
                outcome["result"] = self.invoke(cli.cmd_up,
                                                cli_args("up", "--purpose", "after-a-crash"), fake)
            except Exception as exc:  # noqa: BLE001 - the failure is the finding
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        if worker.is_alive():
            self.fail("a stale lock (runs.jsonl.lock left by a killed guard) still blocked the "
                      "ledger 5 s after `up`, so no row was recorded: the machine bills and the "
                      "cap cannot see it")
        if "error" in outcome:
            self.fail(f"a stale lock stopped the ledger from being written: {outcome['error']}")

        code, out = outcome["result"]
        self.assertIsNotNone(
            self.ledger.open_run(),
            f"up rented {[r.get('instance_id') for r in fake.created] or 'nothing'} and recorded "
            f"no ledger row while a stale lock existed (exit {code}). A run with no row is spend "
            f"the cap cannot see.\noutput:\n{out}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
