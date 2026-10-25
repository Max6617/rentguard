"""The PTY pull frame, checkable offline from hand-written transcripts (defect 9, framing half).

A PTY folds the remote stderr into stdout and rewrites LF as CRLF, so a bare ``tar czf -`` cannot
cross it and neither can a bare base64 body: the body's extent has to be **stated**. The frame is

    RENTGUARD-ARCHIVE <characters>\n
    <base64 body, no newlines>\n
    RENTGUARD-END\n

and it is checked in this order, before anything is extracted: the body against the header's count,
the body against strict base64, the decoded length against what that count implies, and the first two
bytes against the gzip magic. Each failure is named.

These transcripts are hand-written to the shapes already in the record: a good frame, the 21-byte
prompt-text pull (2026-10-14, the community pod), a truncated frame, and an empty archive.
"""

from __future__ import annotations

import base64
import contextlib
import io
import io as _io  # noqa: F401 - kept explicit for the transcript builders below
import json
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
from rentguard.tests import _pull_ssh as pull_ssh  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

PATTERNS = ["results/*.json"]


def gzip_tar(members: dict[str, bytes]) -> bytes:
    if not members:
        return b""          # an archive with no members, as GNU tar leaves it
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def framed(archive: bytes, *, declared: int | None = None, terminator: bool = True,
           newline: str = "\r\n") -> bytes:
    """A session transcript: header, body, terminator, with the pty's CRLF."""
    body = base64.b64encode(archive).decode()
    count = len(body) if declared is None else declared
    text = f"{cli.FRAME_HEADER} {count}{newline}{body}{newline}"
    if terminator:
        text += f"{cli.FRAME_END}{newline}"
    return text.encode()


class FrameCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.artefacts = Artefacts(pull=list(PATTERNS), remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")

    def provider(self):
        class SshOnly(FakeProvider):
            def pull_artifacts(self, instance, patterns):
                raise NotImplementedError

            def ssh_requires_pty(self, instance):
                return True                     # the proxy route: framed, base64, checked

        provider = SshOnly()
        provider.add("pod-A", uptime_seconds=600)
        return provider

    def pull(self, transcript: bytes, exit_code: int = 0) -> tuple[int, dict]:
        class Child:
            def __call__(self, argv, stdout=None, **kwargs):
                if stdout is not None:
                    stdout.write(transcript)
                    stdout.flush()
                return self

            def wait(self, timeout=None):
                return exit_code

            def kill(self):
                pass

        def inventory(provider, argv, target, remote, **kwargs):
            """The measurement on a PTY route goes through the session driver; its answer is the
            archive's own member, so the pull reaches the frame this file is about."""
            return 0, pull_ssh.file_entries({"results/out.json": b"x"}), ""

        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider()), \
                mock.patch.object(cli, "_run_remote", inventory), \
                mock.patch.object(cli.subprocess, "Popen", Child()), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_pull(cli.build_parser().parse_args(["pull", "--json"]))
        return code, json.loads(out.getvalue() or "{}")


class TheFrameIsCheckableOffline(FrameCase):
    def test_a_good_frame_extracts_the_archive(self):
        code, document = self.pull(framed(gzip_tar({"results/out.json": b'{"ok": true}'})))

        self.assertEqual(code, 0, document)
        self.assertEqual(document.get("files"), ["results/out.json"], document)
        landed = Path(self.cfg.artefacts.local_dir) / "results" / "out.json"
        self.assertEqual(landed.read_bytes(), b'{"ok": true}')

    def test_the_21_byte_prompt_text_pull_is_named(self):
        """The real one: the session returned prompt text and nothing else (21 bytes)."""
        code, document = self.pull(b"root@3da253ba2eba:/workspace#\r\n")

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "frame-invalid", document)
        self.assertIn("header", document.get("detail", ""), document)

    def test_a_frame_without_its_terminator_is_named(self):
        code, document = self.pull(framed(gzip_tar({"results/out.json": b"x"}), terminator=False))

        self.assertNotEqual(code, 0, document)
        self.assertIn("terminator", document.get("detail", ""), document)

    def test_a_body_that_disagrees_with_its_header_is_named(self):
        code, document = self.pull(framed(gzip_tar({"results/out.json": b"x"}), declared=9999))

        self.assertNotEqual(code, 0, document)
        self.assertIn("declared", document.get("detail", ""), document)

    def test_an_empty_archive_never_reads_as_a_result(self):
        """The empty-archive shape: a header of zero, no body, the terminator, and gzip magic that
        is not there — so it fails on the frame's own arithmetic, before extraction."""
        code, document = self.pull(framed(b""))

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("files"), [], document)
        self.assertIn("gzip", document.get("detail", ""), document)


if __name__ == "__main__":
    unittest.main()
