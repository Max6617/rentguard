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
    """``subprocess.Popen``-shaped: writes a body, then is waited on."""

    def __init__(self, body: bytes, exit_code: int):
        self.body = body
        self.exit_code = exit_code

    def __call__(self, argv, stdout=None, **kwargs):
        if stdout is not None and self.body:
            stdout.write(self.body)
            stdout.flush()
        return self

    def wait(self, timeout=None):
        return self.exit_code

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


if __name__ == "__main__":
    unittest.main()
