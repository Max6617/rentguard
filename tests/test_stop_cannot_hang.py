"""Nothing may hang long enough to hold a machine open (defect 14).

Measured 2026-10-14, on Cadence's authorised community-pod window: `down` pulls before it stops —
deliberately, because a container with no volume loses its results on stop — and that pull was
`subprocess.call([...], stdout=handle)` with **no stdin and no timeout**. On the proxy route the
session never saw the command, never saw EOF, and never returned: `down` sat for over ten minutes at
$0.18/h with no guard armed, and the operator stopped the pod by hand through the provider's API.
The row cost 0.3746 h / $0.06745 for a job meant to take minutes.

Two bounds, and one escalation:

- the pull's ssh child gets ``stdin=DEVNULL``, so a session that lost its exec sees EOF and returns
  instead of waiting for input that will never come;
- the pull is waited on with a **bounded** wait (``[artefacts] pull_timeout_s``); when the bound
  expires the child is killed, the failure is named, and — this is the part that matters — the stop
  proceeds anyway with the patterns that were lost listed;
- the stop itself is bounded (``[budget] stop_timeout_s``) and, if the ordinary call does not come
  back, one fresh hurried call goes to the provider's stop endpoint. A stuck socket must never be
  able to hold a machine open.

These are transport-level assertions on purpose: a test that *waits* for a hang cannot be written, so
the contract is asserted where it can be — on the child's stdio and on the wait's bound — and the
observable outcome (row closed, stop called, patterns named) is asserted beside it.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tarfile
import time
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests import _pull_ssh as pull_ssh  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

PATTERN = "results/*.json"


def gzip_tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class ScriptedSsh:
    """Stands in for ``subprocess.Popen`` on the pull path, and records how it was started.

    It answers **both** commands a pull now runs: the inventory (a framed text answer built from the
    archive's own members, so the selection and the volume are consistent with what the archive will
    deliver) and then the archive. ``hang`` hangs the archive, not the measurement — a test about the
    transfer's bound must not be satisfied by the inventory's.
    """

    def __init__(self, archive: bytes = b"", *, exit_code: int = 0, hang: bool = False):
        self.archive = archive
        self.exit_code = exit_code
        self.hang = hang
        self.argv: list[str] = []
        self.stdin: object = None
        self.timeout: float | None = None
        self.started = 0
        self.killed = False
        self.inventory_call = False
        self.archive_call = False

    def __call__(self, argv, **kwargs):
        self.started += 1
        self.argv = list(argv)
        self.stdin = kwargs.get("stdin")
        sink = kwargs.get("stdout")
        self.inventory_call = pull_ssh.is_inventory(self.argv)
        self.archive_call = not self.inventory_call
        if self.inventory_call:
            names = (tarfile.open(fileobj=io.BytesIO(self.archive)).getnames()
                     if self.archive else [])
            if sink is not None:
                sink.write(pull_ssh.inventory_text(
                    *[(0, "f", 100.0 + index, 1, name) for index, name in enumerate(names)]
                ).encode())
            return self
        if sink is not None and self.archive and not self.hang:
            sink.write(self.archive)
        return self

    def wait(self, timeout=None):
        if self.inventory_call:
            return 0
        self.timeout = timeout
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.exit_code

    def kill(self):
        self.killed = True


class SshOnly(FakeProvider):
    """A provider that reaches its machine over ssh, as a real one does.

    The fake pulls artefacts from memory, which is the documented seam for providers that can; these
    tests are about the path that cannot, so they need one that says so the way ``RunPod`` does.
    """

    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


class StopCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.artefacts = Artefacts(pull=[PATTERN], remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.fake = FakeProvider()
        self.fake.add("pod-A", uptime_seconds=600)
        self.write_state({"instance_id": "pod-A"})

    def ssh_provider(self) -> FakeProvider:
        provider = SshOnly()
        provider.add("pod-A", uptime_seconds=600)
        return provider

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.cfg.ledger_path)

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def invoke(self, func, argv: list[str], provider) -> tuple[int, str]:
        args = cli.build_parser().parse_args(argv)
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                contextlib.redirect_stdout(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()


# ============================================================== the pull is bounded

class PullIsBounded(StopCase):
    def test_the_pull_child_gets_no_stdin_and_a_bounded_wait(self):
        """Sequence: `rentguard pull` on a route that answers.

        Effect: the ssh child is started with stdin on the null device and waited on with the
        configured bound — the two things whose absence held `down` open for ten minutes — and a
        real archive still lands.
        """
        self.cfg.artefacts.pull_timeout_s = 75.0
        scripted = ScriptedSsh(gzip_tar({"results/out.json": b'{"ok": true}'}))

        with mock.patch.object(cli.subprocess, "Popen", scripted):
            code, out = self.invoke(cli.cmd_pull, ["pull"], self.ssh_provider())

        self.assertEqual(code, 0, f"the bounded pull failed.\n{out}")
        self.assertEqual(scripted.started, 2,
                         f"the pull did not run through the bounded child twice (inventory, then "
                         f"archive).\n{out}")
        self.assertTrue(scripted.archive_call, "the second call was not the archive")
        self.assertEqual(scripted.stdin, subprocess.DEVNULL,
                         "the child inherited a stdin a stuck session can wait on")
        self.assertEqual(scripted.timeout, 75.0, f"the wait was not the configured floor.\n{out}")
        landed = Path(self.cfg.artefacts.local_dir) / "results" / "out.json"
        self.assertEqual(landed.read_bytes(), b'{"ok": true}')

    def test_a_pull_that_never_returns_is_killed_and_named(self):
        """Sequence: the ssh child never returns (a route that hangs).

        Effect: the bound expires, the child is killed, the failure says so, and `pull` exits 1. A
        pull that cannot end is a machine that cannot stop.
        """
        self.cfg.artefacts.pull_timeout_s = 3.0
        scripted = ScriptedSsh(gzip_tar({"results/out.json": b"x"}), hang=True)

        with mock.patch.object(cli.subprocess, "Popen", scripted):
            code, out = self.invoke(cli.cmd_pull, ["pull"], self.ssh_provider())

        self.assertNotEqual(code, 0, f"a hung pull was reported as success.\n{out}")
        self.assertTrue(scripted.killed, f"the hung child was left running.\n{out}")
        self.assertIn("did not return", out, f"the bound was not named.\n{out}")
        self.assertIn("pull_timeout_s", out, f"the failure does not say which bound.\n{out}")

    def test_down_stops_the_machine_when_its_pull_never_returns(self):
        """Sequence: `down` on a live machine, and the pre-stop pull hangs.

        Effect: the machine is stopped and the row is closed anyway, the paths that were lost are
        named, and the exit code is non-zero. This is the ten-minute hang, in one test.
        """
        self.cfg.artefacts.pull_timeout_s = 3.0
        self.ledger.open(purpose="hanging pull", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.18)
        scripted = ScriptedSsh(gzip_tar({"results/out.json": b"x"}), hang=True)
        provider = self.ssh_provider()

        with mock.patch.object(cli.subprocess, "Popen", scripted):
            code, out = self.invoke(cli.cmd_down, ["down"], provider)

        self.assertIn(("pod-A", "stop"), provider.actions,
                      f"the machine was left billing because its pull hung.\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"the row was left open.\n{out}")
        self.assertIsNotNone(self.ledger.entries()[0].get("estimated_usd"), out)
        self.assertIn(PATTERN, out, f"the lost paths were not named.\n{out}")
        self.assertNotEqual(code, 0, f"a lost pull exited 0.\n{out}")


# ============================================================== the stop cannot be held open

class StopCannotBeHeldOpen(StopCase):
    class SlowStop(FakeProvider):
        """A stop call that does not come back inside the bound."""

        delay = 0.6

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.forced: list[str] = []

        def action(self, instance_id, action):
            if action == "stop":
                time.sleep(self.delay)
            return super().action(instance_id, action)

        def force_stop(self, instance_id):
            self.forced.append(instance_id)
            return super().action(instance_id, "stop")

    def test_a_stop_that_does_not_return_escalates_to_the_provider_endpoint(self):
        """Sequence: `down` on a live machine whose ordinary stop never comes back.

        Effect: one fresh hurried call goes to the provider's stop endpoint — what the operator did
        by hand — and the row is still closed with its cost.
        """
        self.cfg.budget.stop_timeout_s = 0.05
        self.ledger.open(purpose="slow stop", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.18)
        provider = self.SlowStop()
        provider.add("pod-A", uptime_seconds=600)

        code, out = self.invoke(cli.cmd_down, ["down"], provider)

        self.assertEqual(provider.forced, ["pod-A"],
                         f"a stop that did not return was waited on instead of escalated.\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"the row was left open.\n{out}")
        self.assertIn("escalat", out.lower(), f"the escalation is not reported.\n{out}")

    def test_a_stop_that_returns_is_not_escalated(self):
        """Control: an ordinary stop is not doubled, and the bound does not fire."""
        self.cfg.budget.stop_timeout_s = 5.0
        self.ledger.open(purpose="normal stop", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.18)
        provider = self.SlowStop()
        provider.delay = 0.0
        provider.add("pod-A", uptime_seconds=600)

        code, out = self.invoke(cli.cmd_down, ["down"], provider)

        self.assertEqual(provider.forced, [], f"a working stop was escalated anyway.\n{out}")
        self.assertIn(("pod-A", "stop"), provider.actions, out)
        self.assertEqual(code, 0, f"a clean stop exited {code}.\n{out}")


if __name__ == "__main__":
    unittest.main()
