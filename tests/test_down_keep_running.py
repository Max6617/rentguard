"""`down` when the artefact pull fails.

Gilbert's ruling, and the reason it is a money rule rather than a preference:

- the pull happens **before** the stop, because an instance with no network volume is a container
  and a stopped container's files are gone;
- if the pull fails, `down` stops anyway, exits non-zero and names the paths that were lost — a
  machine that keeps billing is the worse loss, so that decision must not be the default;
- `--keep-running` is the explicit opt-in that trades money for a retry.

No network and no spend: the provider is the in-memory fake, and only the pull is replaced.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

PATTERN = "results/*.json"


class DownWithFailedPull(unittest.TestCase):
    def setUp(self) -> None:
        root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=root / ".rentguard")
        self.cfg.artefacts.pull = [PATTERN]
        self.provider = FakeProvider(rate_usd_per_hour=2.0)
        self.provider.add("pod-A", uptime_seconds=600)
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        self.ledger = Ledger(self.cfg.ledger_path)
        self.ledger.open(purpose="probe", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0)

    def _down(self, *, keep_running: bool, pull_exit: int) -> tuple[int, str, bool]:
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(cli, "_try_pull", lambda cfg, args: pull_exit), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.cmd_down(Namespace(keep_running=keep_running, json=False))
        stopped = ("pod-A", "stop") in self.provider.actions
        return code, out.getvalue(), stopped

    def test_the_machine_stops_and_the_exit_is_non_zero_when_the_pull_fails(self) -> None:
        code, out, stopped = self._down(keep_running=False, pull_exit=1)

        self.assertTrue(stopped, "the machine was left billing after a failed pull by default")
        self.assertNotEqual(code, 0, f"a lost pull exited 0, so a script reads it as a clean "
                                     f"stop:\n{out}")
        self.assertIn(PATTERN, out, f"the lost path was not named:\n{out}")
        self.assertIn("--keep-running", out,
                      f"the deliberate way to retry was not offered:\n{out}")

    def test_keep_running_leaves_the_machine_up_so_the_copy_can_be_retried(self) -> None:
        code, out, stopped = self._down(keep_running=True, pull_exit=1)

        self.assertFalse(stopped, "--keep-running stopped the machine anyway")
        self.assertIsNotNone(self.ledger.open_run(),
                             "the row was closed while the machine is still billing")
        self.assertNotEqual(code, 0, f"a failed pull exited 0:\n{out}")
        self.assertIn(PATTERN, out, f"the lost path was not named:\n{out}")

    def test_a_pull_that_raises_still_stops_the_machine(self) -> None:
        """Sequence: `down` with artefacts configured, and the pull *raises* instead of returning
        an exit code — the real shape when the tar stream cannot be written (an unwritable temp
        dir, a full disk, a broken pipe).

        Effect required: the machine is stopped anyway, and the exit is non-zero. This is the case
        a test that stubs `_try_pull` to return 1 cannot see: an exception escaping between the
        pull and the stop leaves the machine billing because a tar stream failed.
        """
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(cli, "cmd_pull",
                                  side_effect=OSError("cannot write the tar stream")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.cmd_down(Namespace(keep_running=False, json=False))

        self.assertIn(("pod-A", "stop"), self.provider.actions,
                      "the pull raised and the machine was left billing")
        self.assertNotEqual(code, 0,
                            f"a pull that crashed exited 0, so a script reads it as a clean "
                            f"stop:\n{out.getvalue()}")
        self.assertIn("pull failed", out.getvalue(),
                      f"the crashed pull was not reported:\n{out.getvalue()}")

    def test_a_successful_pull_stops_the_machine_and_exits_zero(self) -> None:
        code, out, stopped = self._down(keep_running=False, pull_exit=0)

        self.assertTrue(stopped)
        self.assertEqual(code, 0, f"a clean pull and stop exited non-zero:\n{out}")
        self.assertIsNone(self.ledger.open_run())


if __name__ == "__main__":
    unittest.main()
