"""The proxy ssh route: a pod with no direct endpoint, reached through RunPod's ssh proxy.

**Why this file exists.** Every real rental before 2026-10-14 was a secure-cloud pod with
``ssh.direct``. Cadence's community pod had none, so ``run`` took the proxy route and came back
with ``Error: Your SSH client doesn't support PTY``; the pull that followed read that sentence as a
tar stream and died with ``ReadError: not a gzip file``.

**What is observed, and what is modelled.** The rule this repo learned the hard way is that a
fixture which manufactures the provider's behaviour is a test of the fixture. So:

* *Observed* (real community pod, 2026-10-14): the proxy answers an exec request with no PTY with
  ``Error: Your SSH client doesn't support PTY``, that text arrives on **stdout**, and the client
  exits **0** — which is why the pull did not report "ssh exited N"; it went on to read the sentence
  as an archive. The command does not run.
* *Modelled* (a property of a pty, not a claim about RunPod): once a session has a pty, its output
  arrives with ``\\n`` rewritten to ``\\r\\n``, and the remote stderr is folded into it. That is what
  makes ``tar czf -`` unusable through this route and why the archive travels base64-encoded there.

The provider under test is the **real** ``RunPod``, so the argv asserted here is the argv that
ships. Only the transport below it is scripted, and it is scripted to the observation above. No
account is contacted: ``urlopen`` is patched to fail, so a test that quietly reached the live API
would fail rather than pass.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import types
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Budget, Config  # noqa: E402
from rentguard.provider import Provider  # noqa: E402
from rentguard.runpod import RunPod  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

KEY = "test-key"

#: The route the observed pod offered: no ``direct`` block, only the proxy.
PROXY_SSH = {"proxy": {"host": "ssh.runpod.io", "port": 443, "username": "2abx0hz2boe6ow"}}
DIRECT_SSH = {"direct": {"host": "203.0.113.7", "port": 40123, "username": "root"}}
PROXY_HOST = "ssh.runpod.io"


def gzip_tar(members: dict[str, bytes]) -> bytes:
    """A real gzip tar archive, built by the same tarfile the pull reads it back with."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class SshTransport:
    """``ssh``, scripted: an ordinary byte channel to a direct host, a PTY-only proxy to RunPod.

    Callable with ``subprocess.call``'s signature, which is where it is installed.
    """

    PTY_ERROR = "Error: Your SSH client doesn't support PTY"

    def __init__(self, archive: bytes = b"", *, body: bytes | None = None, exit_code: int = 0):
        self.archive = archive
        self.body = body          # non-archive bytes on stdout: the proxy's refusal text, say
        self.exit_code = exit_code
        self.calls: list[list[str]] = []
        self.executed: list[str] = []   # remote commands that really ran
        self.refused: list[str] = []    # remote commands the endpoint never ran
        #: The pull starts its ssh through ``Popen`` (so the wait can be bounded); `run` still uses
        #: ``call``. One double, two shapes, because the tests below drive both commands.
        self.popen_mode = True
        self.stdin = None
        self.timeout: float | None = None
        self.killed = False

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if kwargs.get("stdin") is subprocess.PIPE:
            # The **session driver**'s shape (2026-10-15): `ssh -tt target` with pipes, no command in
            # the argv at all — the command is written into stdin behind a marker, because an exec
            # passed as an argument is dropped on this route and the session hangs. Modelled the same
            # way the pty behaves: the line is echoed back, the command runs, and the driver's own
            # sentinel carries the status out in-band.
            self.stdout = SessionPipe()
            self.stdin = DriverStdin(self)
            return self
        target, remote = argv[-2], argv[-1]
        sink = kwargs.get("stdout")
        self.stdin = kwargs.get("stdin", self.stdin)
        proxy = PROXY_HOST in target
        if proxy and "-tt" not in argv:
            # Observed: without a PTY the proxy answers with this line and exits 0. The command
            # never runs, so a caller that trusts the exit code believes it did.
            self.refused.append(remote)
            self._text(sink, self.PTY_ERROR + "\n")
            self.exit_code = 0
        else:
            self.executed.append(remote)
            if self.body is not None:
                self._bytes(sink, self.body)
            elif "base64" in remote:
                # The remote command frames the body now: header, body, terminator.
                payload = base64.b64encode(self.archive).decode()
                self._text(sink, f"{cli.FRAME_HEADER} {len(payload)}\n{payload}\n{cli.FRAME_END}\n")
            else:
                self._bytes(sink, self.archive)
        return self if self.popen_mode else self.exit_code

    def wait(self, timeout=None):
        """``Popen.wait``: the pull's child. The bound is recorded, not waited for."""
        self.timeout = timeout
        return self.exit_code

    def kill(self):
        self.killed = True

    def _text(self, sink, text: str) -> None:
        """A PTY session: LF becomes CRLF on the way back to the client."""
        self._bytes(sink, text.replace("\n", "\r\n").encode())

    @staticmethod
    def _bytes(sink, data: bytes) -> None:
        if sink is None:
            return                      # `run` inherits stdout; those tests assert the argv instead
        sink.write(data)
        sink.flush()


class SessionPipe:
    """The read end of a session's stdout: everything the session produced, then EOF."""

    def __init__(self) -> None:
        self._data = bytearray()

    def feed(self, data: bytes) -> None:
        self._data += data

    def read(self, size: int = -1) -> bytes:
        data = bytes(self._data)
        self._data.clear()
        return data


class DriverStdin:
    """The write end of a session's stdin: the driver's line, echoed and then run.

    Reading it here is the *point* of the double: what the command is can only be learned from the
    bytes the driver writes, because the argv carries no command on this route.
    """

    def __init__(self, transport: "SshTransport") -> None:
        self.transport = transport
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        line = self.buffer.decode()
        begin = line.find(cli.session.BEGIN)
        if begin < 0:
            return
        command = line[begin + len(cli.session.BEGIN) + 2:].split("; rc=$?")[0]
        self.transport.executed.append(command)
        self.transport.stdout.feed(
            ("Welcome to the proxy\r\n" + line.replace("\n", "\r\n") + "\r\n"
             + cli.session.BEGIN + "\r\nran\r\n"
             + f"{cli.session.EXIT_SENTINEL} 0\r\n").encode())


class ProxyCase(unittest.TestCase):
    """A config, a state file and a live-looking pod, with the real RunPod provider."""

    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "runpod"
        self.cfg.artefacts = Artefacts(pull=["results/*.json"], remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))
        self.pod = {
            "id": "2abx0hz2boe6ow", "name": "rentguard-community", "status": "RUNNING",
            "cost": 0.19, "gpu": {"id": "NVIDIA GeForce RTX 3080 Ti", "count": 1},
            "runtime": {"uptime": 90.0, "ports": {"22": "tcp"}, "gpus": [{"util": 0}]},
            "ssh": dict(PROXY_SSH),
        }
        self.provider = RunPod(KEY)
        self.write_state({"instance_id": self.pod["id"]})

    def use_route(self, ssh: dict) -> None:
        """Point the fixture at another route: the same pod, a different way in."""
        self.pod["ssh"] = dict(ssh)

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def invoke(self, func, argv: list[str], transport, *, patch: str = "Popen") -> tuple[int, str]:
        """Run a command with the transport installed: ``Popen`` for the pull, ``call`` for `run`."""
        transport.popen_mode = patch == "Popen"
        args = cli.build_parser().parse_args(argv)
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(self.provider, "get", lambda instance_id: self.pod), \
                mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("a test reached the live API")), \
                mock.patch.object(cli.subprocess, patch, transport), \
                contextlib.redirect_stdout(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()


class ArgvTest(unittest.TestCase):
    """Which route the provider says it is using, and what it hands to ssh."""

    def setUp(self) -> None:
        self.pod = RunPod(KEY)

    def test_a_proxy_endpoint_forces_a_pty(self):
        """The proxy only speaks through a PTY, so the argv must ask for one.

        ``-tt`` (two t's) rather than ``-t``: rentguard is not a terminal, and a single ``-t`` is
        ignored in exactly that case.
        """
        args, target = self.pod.ssh_argv({"ssh": dict(PROXY_SSH)}, None)

        self.assertIn("-tt", args, "the proxy route did not ask for the PTY it requires")
        self.assertNotIn("-p", args, "a proxy route has no port to forward")
        self.assertEqual(target, "2abx0hz2boe6ow@ssh.runpod.io")
        self.assertTrue(self.pod.ssh_requires_pty({"ssh": dict(PROXY_SSH)}))

    def test_a_direct_endpoint_is_a_plain_byte_channel(self):
        """The control: a direct endpoint must not be turned into a PTY, because a PTY is what
        corrupts a tar stream — and it is the route every secure-cloud pod uses."""
        args, target = self.pod.ssh_argv({"ssh": dict(DIRECT_SSH)}, None)

        self.assertNotIn("-tt", args, "a direct endpoint was made into a PTY")
        self.assertIn("-p", args)
        self.assertEqual(target, "root@203.0.113.7")
        self.assertFalse(self.pod.ssh_requires_pty({"ssh": dict(DIRECT_SSH)}))

    def test_a_provider_with_no_ssh_at_all_has_no_pty_claim(self):
        self.assertFalse(self.pod.ssh_requires_pty({"ssh": {}}))
        self.assertFalse(Provider().ssh_requires_pty({"ssh": dict(PROXY_SSH)}),
                         "the interface default must be the plain byte channel")

    def test_the_normalised_payload_keeps_the_route_readable(self):
        """``_ssh_route`` is the one place that decides the route, so both answers agree even when
        the instance arrives nested under ``machine``, which is what ``normalise`` produces."""
        nested = self.pod.normalise({"id": "p", "status": "RUNNING", "ssh": dict(PROXY_SSH)})

        self.assertTrue(self.pod.ssh_requires_pty(nested))
        args, _ = self.pod.ssh_argv(nested, None)
        self.assertIn("-tt", args)

    def test_the_known_hosts_null_device_is_the_local_platforms_name(self):
        """The provider says *where* to look; the CLI says what this platform's null device is called.

        A hardcoded ``NUL`` is a Windows name, and on Linux or macOS it is a file literally called
        ``NUL`` written into the user's working directory — on the very route whose known-hosts file
        it names. The provider therefore hands over a ``{null}`` placeholder, which ``cli._ssh``
        substitutes, and this host can exercise both substitutions.
        """
        args, _ = self.pod.ssh_argv({"ssh": dict(DIRECT_SSH)}, None)
        self.assertIn("UserKnownHostsFile={null}", args,
                      f"the provider named a platform device itself: {args}")

        cfg = Config()
        argv, _ = cli._ssh_argv(cfg, self.pod, {"ssh": dict(DIRECT_SSH)})
        here = "UserKnownHostsFile=" + ("NUL" if os.name == "nt" else "/dev/null")
        self.assertIn(here, argv, f"the CLI did not substitute this platform's device: {argv}")

        # The other platform, on this host: `cli.os` is patched, never the real `os` module. Setting
        # `os.name` globally makes `pathlib` try to instantiate `PosixPath` on Windows, and the line
        # that does it may be anywhere — under `tools/coverage.py` it is the tracer's own `Path()` on
        # every line, and a trace function that raises is switched off for the rest of the session, so
        # the gate would measure a fraction of the suite while every test still passed.
        with mock.patch.object(cli, "os", types.SimpleNamespace(name="posix")):
            self.assertEqual(cli._ssh("UserKnownHostsFile={null}"),
                             "UserKnownHostsFile=/dev/null")


class PullTest(ProxyCase):
    """The pull: the archive has to survive a text-only channel."""

    def test_a_pull_through_the_proxy_lands_the_artefacts(self):
        """Sequence: a community pod with no direct ssh; `rentguard pull` runs.

        Effect: the artefacts land with their exact bytes, and the session asked for a PTY — the
        archive crossed the PTY as base64, because a PTY rewrites LF as CRLF and a gzip stream
        cannot survive that.
        """
        archive = gzip_tar({"results/out.json": b'{"ok": true}'})
        transport = SshTransport(archive)

        code, out = self.invoke(cli.cmd_pull, ["pull"], transport)

        self.assertEqual(code, 0, f"the pull through the proxy failed.\noutput:\n{out}")
        landed = Path(self.cfg.artefacts.local_dir) / "results" / "out.json"
        self.assertTrue(landed.exists(), f"the artefact was not extracted:\n{out}")
        self.assertEqual(landed.read_bytes(), b'{"ok": true}')
        self.assertTrue(transport.calls, "no ssh was attempted")
        self.assertIn("-tt", transport.calls[0])
        self.assertIn("base64", transport.executed[0],
                      "binary was pushed through a PTY instead of being encoded")

    def test_a_pull_that_gets_a_sentence_instead_of_an_archive_says_so(self):
        """Sequence: ssh exits 0 but the stream is the endpoint's own text, which is exactly what
        the proxy returned on the real pod.

        Effect: a clear failure and a non-zero exit — never ``ReadError: not a gzip file`` raised
        from inside tarfile, which is what a person saw.
        """
        transport = SshTransport(body=b"Error: Your SSH client doesn't support PTY\n")

        code, out = self.invoke(cli.cmd_pull, ["pull"], transport)

        self.assertNotEqual(code, 0, f"a non-archive stream was reported as a pulled result:\n{out}")
        self.assertIn("not a framed archive", out, f"the failure did not name the stream:\n{out}")
        self.assertNotIn("Traceback", out)

    def test_a_direct_pull_still_streams_the_tar(self):
        """The control: on the direct route the tar goes down the wire unchanged, so the fix cannot
        have quietly moved every pull onto the encoded path."""
        self.use_route(DIRECT_SSH)
        archive = gzip_tar({"results/out.json": b'{"ok": true}'})
        transport = SshTransport(archive)

        code, out = self.invoke(cli.cmd_pull, ["pull"], transport)

        self.assertEqual(code, 0, f"the direct pull failed.\noutput:\n{out}")
        self.assertNotIn("-tt", transport.calls[0])
        self.assertEqual(transport.calls[0][-1], "cd /workspace && tar czf - results/*.json")
        landed = Path(self.cfg.artefacts.local_dir) / "results" / "out.json"
        self.assertEqual(landed.read_bytes(), b'{"ok": true}')

    def test_a_pull_whose_remote_tar_failed_is_not_reported_as_a_kept_result(self):
        """Sequence: the remote tar fails, so the encoded body is empty while the session exits 2.

        Effect: failure, named as a failed pull. The exit status has to survive the encoding dance:
        an empty body that decoded to an empty archive and reported "pulled 0 files" would be a
        lost result reported as a kept one.
        """
        transport = SshTransport(archive=b"", exit_code=2)

        code, out = self.invoke(cli.cmd_pull, ["pull"], transport)

        self.assertNotEqual(code, 0, f"a failed remote tar was reported as success:\n{out}")
        self.assertIn("pull failed", out)

    def test_a_stream_that_decodes_to_something_that_is_not_an_archive_is_reported(self):
        """Sequence: ssh exits 0 and the body is well-formed base64, but of something that is not a
        tar archive — a truncated transfer, a proxy that answered in text it happened to encode.

        Effect: a clear failure, not a traceback out of tarfile. The base64 decoder cannot tell an
        archive from any other bytes; the archive check is the one that can.
        """
        transport = SshTransport(body=base64.b64encode(b"this is not a tar archive"))

        code, out = self.invoke(cli.cmd_pull, ["pull"], transport)

        self.assertNotEqual(code, 0, f"a non-archive was reported as a pulled result:\n{out}")
        self.assertIn("not a framed archive", out, f"the failure did not name the stream:\n{out}")
        self.assertNotIn("Traceback", out)

    def test_a_truncated_base64_stream_is_reported(self):
        """Sequence: the session's output is base64 that stops in the middle of a group (a dropped
        connection, a killed transfer).

        Effect: a reported failure. Decoding what arrived and calling it the archive is how a
        partial file lands in the results directory looking complete.
        """
        transport = SshTransport(body=b"QUJD\r\nQQ\r\n")

        code, out = self.invoke(cli.cmd_pull, ["pull"], transport)

        self.assertNotEqual(code, 0, f"a truncated stream was reported as success:\n{out}")
        self.assertIn("not a framed archive", out, f"the failure did not name the stream:\n{out}")


class RunTest(ProxyCase):
    """`run` through the proxy: the command has to reach the machine at all."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.artefacts = Artefacts(pull=[], remote_dir="/workspace",
                                       local_dir=str(self.cfg.state_dir / "artefacts"))

    def test_run_reaches_a_proxy_pod(self):
        """Sequence: a community pod, `rentguard run -- python train.py`.

        Effect: the session carries a PTY and the remote command really ran. Without the PTY the
        endpoint answers with its refusal text and the command never executes, which is the defect.

        Since 2026-10-15 the command travels **inside the session** (`stdin`) rather than as an ssh
        argument: on this route an exec argument is dropped and the session never returns (measured —
        `run --json` hung past 120 s while the machine billed). What this test asserts is unchanged:
        a PTY was asked for, the endpoint refused nothing, and exactly one command ran on the
        machine. The double learns the transport; the effect is the same.
        """
        transport = SshTransport()

        code, out = self.invoke(cli.cmd_run, ["run", "--", "python", "train.py"], transport)

        self.assertEqual(code, 0, f"the run through the proxy failed.\noutput:\n{out}")
        self.assertIn("-tt", transport.calls[0], "the proxy session did not ask for a PTY")
        self.assertEqual(transport.refused, [], "the command never ran on the machine")
        self.assertEqual(transport.executed, ["cd /workspace && python train.py"])
        self.assertIn("ran", out, f"the command's own output did not reach the caller:\n{out}")

    def test_run_on_a_direct_pod_is_unchanged(self):
        """The control: no PTY is forced where the route does not need one."""
        self.use_route(DIRECT_SSH)
        transport = SshTransport()

        code, out = self.invoke(cli.cmd_run, ["run", "--", "python", "train.py"], transport,
                                patch="call")

        self.assertEqual(code, 0, f"the direct run failed.\noutput:\n{out}")
        self.assertNotIn("-tt", transport.calls[0])
        self.assertEqual(transport.executed, ["cd /workspace && python train.py"])


if __name__ == "__main__":
    unittest.main()
