"""`run` must not fail because a command legitimately delivered nothing to pull (defect 17).

The operator's `stage()` aborted on it during the 2026-10-15 shakedown: staging uploads inputs and produces
no artefacts, so the framed pull answered `patterns-matched-nothing` and `run` exited non-zero — a
clean command reported as a failed run (37 s, $0.0091, row <run-id>).

The rule this file pins, and the reason it is not simply "stop failing on a failed pull":

- a pull that **matched nothing** is not evidence that a result was lost. It is evidence that there was
  nothing to deliver, which is a normal outcome for a command that only reads, stages or validates.
  `run`'s exit code then follows the **command**, and the result still says `pulled: false` with
  `pull_reason: "patterns-matched-nothing"` — visible, never silent.
- a pull that **broke** (a timed-out session, an unreadable frame, a stream that is not an archive) is
  the class where the files may exist and be lost, so it still fails the run. That distinction is the
  whole point of the change: one reason is named, the other is not.
- `run --require-artefacts` is the strict caller's opt-in: then *any* pull that did not deliver fails
  the run, matched-nothing included. `run --help` and the MCP tool description say so.

`down` is deliberately not changed here: it is the last moment before the container is destroyed, so a
pull that delivered nothing stays visible and non-zero there, as ruled on 2026-10-14 —
`tests/test_pull_unmatched.py::DownWithAPartialDelivery` pins it, and a first attempt at making `down`
reason-aware turned that test red, which is the file's whole job.

One inconsistency found while implementing this, **left in place and reported rather than flipped at
midnight**: the two pull seams disagree about an *empty* delivery. The tar path names it
`patterns-matched-nothing` and exits 1 (ruled, and pinned by `test_pull_unmatched.py`); a provider that
hands its artefacts over in memory (`FakeProvider.pull_artifacts` matching nothing) is reported
`pulled: true, files: []` and exits 0, which is what `tests/test_stop_cannot_hang.py` and
`tests/test_feature_matrix.py` rely on for a clean stop. Since `down`'s non-zero exit is keyed on the
pull's exit code, the two seams cannot be made consistent without changing `down`'s ruled behaviour, so
this commit leaves both seams as they are and `cmd_pull` says so at the branch.
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
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

PATTERNS = ["results/*.json"]


class RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.artefacts = Artefacts(pull=list(PATTERNS), remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.provider = FakeProvider(rate_usd_per_hour=2.0)
        self.provider.add("pod-A", uptime_seconds=120, status="RUNNING")
        self.provider.command_runner = lambda argv, remote: self.command_code
        self.command_code = 0
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        Ledger(cfg_path := self.cfg.ledger_path).open(
            purpose="stage", provider="fake", instance_id="pod-A", gpu_id="g",
            rate_usd_per_hour=2.0)

    def run_command(self, pull_exit: int, reason: str | None, *argv: str) -> tuple[int, dict, str]:
        """`run` with the pull replaced by its outcome: one command, one delivered-summary."""
        args = cli.build_parser().parse_args(["run", "--json", *argv, "--", "python", "stage.py"])
        out, err = io.StringIO(), io.StringIO()

        def fake_pull(cfg, run_args):
            if reason is None:
                cli._pull_summary(pulled=True, reason=None, files=["results/out.json"],
                                  unmatched_patterns=[])
            else:
                cli._pull_summary(pulled=False, reason=reason, files=[],
                                  unmatched_patterns=list(PATTERNS))
            return pull_exit

        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: self.provider), \
                mock.patch.object(cli, "_try_pull", fake_pull), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_run(args)
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, document, text + err.getvalue()


class ANothingMatchedPullDoesNotFailTheRun(RunCase):
    def test_a_command_that_delivered_nothing_exits_with_the_commands_status(self) -> None:
        self.command_code = 0

        code, document, text = self.run_command(1, "patterns-matched-nothing")

        self.assertEqual(code, 0,
                         f"a clean command was reported as a failed run because nothing was pulled:"
                         f"\n{text}")
        self.assertFalse(document.get("pulled", True),
                         f"the result claims artefacts were pulled: {document}")
        self.assertEqual(document.get("pull_reason"), "patterns-matched-nothing", document)
        self.assertIn("matched nothing", text,
                      f"the outcome is not named in the prose, so it is silent:\n{text}")

    def test_a_failing_command_still_reports_its_own_status(self) -> None:
        self.command_code = 3

        code, document, text = self.run_command(1, "patterns-matched-nothing")

        self.assertEqual(code, 3, f"the command's status was replaced by the pull's:\n{text}")


class ABrokenPullStillFailsTheRun(RunCase):
    def test_a_timed_out_pull_is_a_failed_run(self) -> None:
        code, document, text = self.run_command(1, "ssh-timeout")

        self.assertEqual(code, 1,
                         f"a pull that broke was reported as a clean run — the files may be lost:"
                         f"\n{text}")
        self.assertEqual(document.get("pull_reason"), "ssh-timeout", document)


class RequireArtefactsIsTheStrictOptIn(RunCase):
    def test_the_flag_makes_a_matched_nothing_pull_fail_the_run(self) -> None:
        code, document, text = self.run_command(1, "patterns-matched-nothing", "--require-artefacts")

        self.assertEqual(code, 1,
                         f"--require-artefacts did not make the empty delivery a failure:\n{text}")
        self.assertTrue(document.get("require_artefacts"), document)

    def test_the_flag_is_invisible_when_artefacts_arrived(self) -> None:
        code, document, text = self.run_command(0, None, "--require-artefacts")

        self.assertEqual(code, 0, f"a delivered pull failed under --require-artefacts:\n{text}")
        self.assertTrue(document.get("pulled"), document)


if __name__ == "__main__":
    unittest.main()
