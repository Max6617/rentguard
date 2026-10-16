"""The `cli.py` branches nothing else reaches: error paths, the ssh+tar pull, and `main`.

The ssh+tar path matters most: it is the path a real provider uses (RunPod inherits
``pull_artifacts``'s NotImplementedError), and it was entirely uncovered — the fake's in-memory pull
is what the other tests exercise.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config, ConfigError  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.provider import Provider, ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402
from datetime import timedelta  # noqa: E402


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.ledger = Ledger(self.cfg.ledger_path)
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def invoke(self, func, argv: list[str], provider) -> tuple[int, str]:
        args = cli.build_parser().parse_args(argv)
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()


class TinyHelpers(unittest.TestCase):
    def test_the_dict_builders_tolerate_nothing(self):
        self.assertIsNone(cli._run_dict(None))
        self.assertIsNone(cli._closed_dict(None))

    def test_an_empty_command_is_an_empty_string(self):
        self.assertEqual(cli._remote_command([]), "")


class DoctorProviderTrouble(CliCase):
    def test_a_provider_that_raises_on_get_does_not_crash_doctor(self):
        """The probe answers "could not ask" rather than dying, so one unreadable instance does not
        take the whole pre-flight report with it."""
        class BoomGet(FakeProvider):
            def get(self, instance_id):
                raise ProviderError("transport down")

        self.write_state({"instance_id": "pod-A"})
        code, out = self.invoke(cli.cmd_doctor, ["doctor"], BoomGet())

        self.assertEqual(code, 0, f"doctor crashed on a provider error.\noutput:\n{out}")


class DoctorVolumeReference(CliCase):
    def test_a_volume_named_by_a_pods_top_level_volume_id_is_not_called_unattached(self):
        class PodsWithVolumes(FakeProvider):
            def list_pods(self):
                return [{"id": "pod-1", "status": "EXITED", "cost": 0.5,
                         "volumeId": "vol-1", "runtime": {}},
                        {"id": "pod-2", "status": "EXITED", "cost": 0.5,
                         "volumeId": "other", "runtime": {}}]

            def volumes(self):
                return [{"id": "vol-1", "name": "used", "size_gb": 10.0, "region": "R",
                         "attached_to": None, "monthly_usd": 0.7},
                        {"id": "vol-2", "name": "unmatched", "size_gb": 10.0, "region": "R",
                         "attached_to": None, "monthly_usd": 0.7},
                        {"id": None, "name": "nameless", "size_gb": 1.0, "region": "R",
                         "attached_to": None, "monthly_usd": 0.07}]

        code, out = self.invoke(cli.cmd_doctor, ["doctor"], PodsWithVolumes())

        self.assertIn("used", out, out)
        self.assertIn("STOPPED pod pod-1", out, f"a top-level volumeId was not matched:\n{out}")
        self.assertIn("unmatched", out, "a volume matched by nothing was not listed")
        self.assertIn("nameless", out, "a volume with no id crashed the report")


class UpUnusableMachine(CliCase):
    def test_up_says_so_when_it_cannot_stop_an_unusable_machine(self):
        """The refused stop is reported, and the row stays **open** — Gilbert's clause.

        `up`'s unusable exit now runs the same discipline as `down`: stop, confirm against the
        account, and close the row only when the account agrees. A stop the provider refused is not a
        stopped machine, so the row keeps accruing where the operator can see it (findings 12-14,
        2026-10-16). `stop_confirm_s` is pinned small so the test measures the rule, not the bound.
        """
        class CannotStop(FakeProvider):
            def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
                pod = self.get(instance_id) or {}
                return {**pod, "status": "RUNNING",
                        "runtime": {"uptime": 600, "ports": {"22": "tcp"},
                                    "gpus": [{"util": 0}]}}

            def action(self, instance_id, action):
                raise ProviderError("provider refused the stop")

        self.cfg.budget.stop_confirm_s = 0.1
        code, out = self.invoke(cli.cmd_up, ["up", "--purpose", "x"], CannotStop())

        self.assertNotEqual(code, 0, f"an unusable machine was reported as a rental.\n{out}")
        self.assertIn("could not stop", out, f"the refused stop was not reported:\n{out}")
        self.assertIn("NOT closed", out, f"the open row is not stated:\n{out}")
        self.assertIsNotNone(Ledger(self.cfg.ledger_path).open_run(),
                             f"the row closed over a machine whose stop was never confirmed:\n{out}")


class PullWithoutConfiguredPatterns(CliCase):
    def test_pull_says_nothing_is_configured(self):
        self.write_state({"instance_id": "pod-A"})
        self.cfg.artefacts.pull = []
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600)

        code, out = self.invoke(cli.cmd_pull, ["pull"], provider)

        self.assertEqual(code, 0, out)
        self.assertIn("nothing configured to pull", out, out)


class PullOverSsh(CliCase):
    """The real provider path: no in-memory pull, so ssh + tar."""

    class SshProvider(FakeProvider):
        def pull_artifacts(self, instance, patterns):
            raise NotImplementedError      # everything that reaches the machine over ssh

    def setUp(self) -> None:
        super().setUp()
        self.write_state({"instance_id": "pod-A"})
        self.cfg.artefacts.pull = ["results/*.json"]
        self.cfg.artefacts.local_dir = str(self.cfg.state_dir / "artefacts")
        provider = self.SshProvider()
        provider.add("pod-A", uptime_seconds=600)
        self.provider = provider

    def _archive_writer(self, member: str, data: bytes, exit_code: int = 0):
        """A stand-in for ``subprocess.Popen`` on the pull path: it writes, then it is waited on.

        The pull starts ssh with ``Popen`` so that its wait can be bounded — see
        ``tests/test_stop_cannot_hang.py`` — so this double has to be ``Popen``-shaped, not a
        ``subprocess.call`` that returns an exit code.
        """
        case = self

        class Proc:
            def __init__(self, argv, stdout=None, **kwargs):
                case.pull_argv = list(argv)
                case.pull_stdin = kwargs.get("stdin")
                if exit_code == 0:
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                        info = tarfile.TarInfo(member)
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
                    if stdout is not None:
                        stdout.write(buffer.getvalue())
                        stdout.flush()

            def wait(self, timeout=None):
                case.pull_timeout = timeout
                return exit_code

            def kill(self):  # pragma: no cover - the bounded path is tested elsewhere
                pass

        return Proc

    def test_a_pull_over_ssh_extracts_the_archive(self):
        with mock.patch.object(cli.subprocess, "Popen",
                               self._archive_writer("results/out.json", b'{"ok":true}')):
            code, out = self.invoke(cli.cmd_pull, ["pull"], self.provider)

        self.assertEqual(code, 0, f"the pull failed.\noutput:\n{out}")
        landed = Path(self.cfg.artefacts.local_dir) / "results" / "out.json"
        self.assertTrue(landed.exists(), f"the archive was not extracted:\n{out}")
        self.assertEqual(landed.read_bytes(), b'{"ok":true}')

    def test_a_pull_over_ssh_reports_a_failing_ssh(self):
        with mock.patch.object(cli.subprocess, "Popen",
                               self._archive_writer("x", b"", exit_code=2)):
            code, out = self.invoke(cli.cmd_pull, ["pull"], self.provider)

        self.assertNotEqual(code, 0, f"a failed ssh pull was reported as success.\n{out}")
        self.assertIn("pull failed", out, out)

    def test_a_pull_over_ssh_is_waited_on_with_a_bound(self):
        """The pull cannot be allowed to hang: `down` pulls before it stops, so an unbounded pull is
        an unbounded bill (measured on 2026-10-14)."""
        with mock.patch.object(cli.subprocess, "Popen",
                               self._archive_writer("results/out.json", b"{}")):
            self.invoke(cli.cmd_pull, ["pull"], self.provider)

        self.assertEqual(self.pull_timeout, self.cfg.artefacts.pull_timeout_s,
                         f"the bound was not the configured one: {self.pull_timeout}")
        self.assertIsNotNone(self.pull_stdin, "the child was given no stdin to reach EOF on")


class MainEntry(CliCase):
    def test_main_returns_zero_for_a_working_command(self):
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: FakeProvider()):
            self.assertEqual(cli.main(["status"]), 0)

    def test_main_turns_a_config_error_into_exit_one(self):
        with mock.patch.object(cli, "cmd_status", side_effect=ConfigError("bad config")):
            self.assertEqual(cli.main(["status"]), 1)

    def test_main_turns_an_interrupt_into_130(self):
        with mock.patch.object(cli, "cmd_status", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["status"]), 130)


if __name__ == "__main__":
    unittest.main(verbosity=2)
