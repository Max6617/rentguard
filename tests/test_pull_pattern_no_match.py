"""A pull pattern that matches nothing is a named finding, not a lost result (defect 16).

Measured 2026-10-14 on a real 4090 (direct SSH): with
``pull = ["…/declared-timing-3080ti/*", "…/batchA-*/*"]`` GNU tar exited 2 on the second pattern,
the pull was read as a transport failure, a **successful** run reported ``run_exit 1``, and the
container holding the only copy of the results was then stopped. The rate survived only because it
happened to be in stdout.

GNU tar writes the archive for the members that *did* match and exits non-zero at the end. So the
exit code alone cannot decide: the archive is read first, what arrived is delivered, and the patterns
that matched nothing are named. Only an archive with nothing in it, or one that will not open, is a
failed pull.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tarfile
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

MATCH = "results/declared-timing-3080ti/out.json"
PATTERNS = ["results/declared-timing-3080ti/*", "results/batchA-*/*"]


def gzip_tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class SshDouble:
    """``subprocess.Popen``-shaped: answers the pull's inventory, then writes the archive body.

    The pull measures before it transfers (2026-10-17), so this double gets two calls. The inventory it
    answers is built from the archive's own members, so the selection and the volume match what the
    archive will deliver; an empty archive answers an empty inventory, which the tool reports as
    ``patterns-matched-nothing`` before it transfers anything.
    """

    def __init__(self, body: bytes, exit_code: int):
        self.body = body
        self.exit_code = exit_code
        self.inventory_call = False

    def __call__(self, argv, stdout=None, **kwargs):
        self.inventory_call = pull_ssh.is_inventory(argv)
        if stdout is not None:
            if self.inventory_call:
                names = (tarfile.open(fileobj=io.BytesIO(self.body)).getnames()
                         if self.body else [])
                stdout.write(pull_ssh.inventory_text(
                    *[(0, "f", 100.0 + index, 1, name) for index, name in enumerate(names)]
                ).encode())
            elif self.body:
                stdout.write(self.body)
            stdout.flush()
        return self

    def wait(self, timeout=None):
        return 0 if self.inventory_call else self.exit_code

    def kill(self):
        pass


class PullNoMatch(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.artefacts = Artefacts(pull=list(PATTERNS), remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.fake = FakeProvider()
        self.fake.add("pod-A", uptime_seconds=600)
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")

    def ssh_provider(self) -> FakeProvider:
        class SshOnly(FakeProvider):
            def pull_artifacts(self, instance, patterns):
                raise NotImplementedError

        provider = SshOnly()
        provider.add("pod-A", uptime_seconds=600)
        return provider

    def invoke(self, transport) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["pull", "--json"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.ssh_provider()), \
                mock.patch.object(cli.subprocess, "Popen", transport), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_pull(args)
        return code, out.getvalue()

    def test_a_pattern_that_cannot_match_does_not_lose_the_result(self):
        """The incident: one pattern matched, one could not, tar exited 2, the results were lost and
        the run was reported as failed."""
        transport = SshDouble(gzip_tar({MATCH: b'{"steps_per_second": 412.5}'}), exit_code=2)

        code, out = self.invoke(transport)

        self.assertEqual(code, 0, f"a pull whose pattern matched nothing failed.\n{out}")
        landed = Path(self.cfg.artefacts.local_dir) / MATCH
        self.assertTrue(landed.exists(), f"the result that did match was lost:\n{out}")
        self.assertEqual(landed.read_bytes(), b'{"steps_per_second": 412.5}')
        document = json.loads(out)
        self.assertEqual(document.get("unmatched_patterns"),
                         ["results/batchA-*/*"],
                         f"the pattern that matched nothing is not named: {document}")
        self.assertEqual(document.get("tar_exit_code"), 2, document)

    def test_a_pull_that_matched_nothing_at_all_still_fails(self):
        """Control: an archive with nothing in it is not a result. The failure names the reason
        rather than only the exit code."""
        transport = SshDouble(b"", exit_code=2)

        code, out = self.invoke(transport)

        self.assertNotEqual(code, 0, f"an empty pull was reported as success.\n{out}")
        self.assertIn("matched", out, f"the reason is not named:\n{out}")


    def test_a_down_whose_pull_delivered_nothing_names_patterns_not_paths(self):
        """Sequence: `down` on a live machine, and its pre-stop pull delivered nothing.

        Effect: the sentence says what the code can know — the *patterns* the pull could not deliver.
        There is no remote listing in that branch, so promising paths claimed more than it had.
        """
        self.cfg.artefacts.pull = ["results/*.json"]
        self.ledger = Ledger(self.cfg.ledger_path)
        self.ledger.open(purpose="down with an empty pull", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.18)
        provider = self.ssh_provider()
        args = cli.build_parser().parse_args(["down"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli.subprocess, "Popen", SshDouble(b"", exit_code=2)), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_down(args)
        text = out.getvalue()

        self.assertIn("patterns", text, f"the loss was not named as patterns:\n{text}")
        self.assertIn("results/*.json", text, text)
        self.assertNotIn("paths", text.lower(), f"a path promise was made:\n{text}")
        self.assertIn(("pod-A", "stop"), provider.actions, text)

    def test_a_down_whose_pull_delivered_something_surfaces_the_unmatched_pattern(self):
        """Sequence: `down` pulls; one file arrives and one configured pattern matched nothing.

        Effect: the unmatched pattern is said out loud, and the sentence does not claim the machine
        is about to lose what has already been fetched.
        """
        self.cfg.artefacts.pull = list(PATTERNS)
        self.ledger = Ledger(self.cfg.ledger_path)
        self.ledger.open(purpose="down with a partial pull", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=0.18)
        provider = self.ssh_provider()
        args = cli.build_parser().parse_args(["down"])
        out = io.StringIO()
        transport = SshDouble(gzip_tar({MATCH: b"x"}), exit_code=2)
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli.subprocess, "Popen", transport), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_down(args)
        text = out.getvalue()

        self.assertIn("results/batchA-*/*", text, f"the unmatched pattern was hidden:\n{text}")
        self.assertIn("matched nothing", text, text)
        self.assertNotIn("nothing was delivered", text,
                         f"a successful delivery was called nothing:\n{text}")


if __name__ == "__main__":
    unittest.main()
