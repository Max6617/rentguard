"""Re-attack on the fixes for the pull-measurement attack, at an earlier revision.

The eleven closed cases live in ``tests/test_pull_selection_attack.py``. This file attacks the new
code itself, and holds the controls the fix must not have broken.

Two shapes found here:

* **the PTY archive command names characters, not paths.** ``_pull_remote_command`` joins the selected
  paths into one string and then hands that string to ``_pty_pull_command``, which joins *it* again —
  iterating the string. Every proxy/PTY pull therefore asks the remote tar for single-character
  members. This file runs the member list through a real ``tar`` and shows it cannot stat them, while
  the direct route's command (joined once) works. Introduced earlier: in an earlier revision the PTY
  builder received the pattern *list*.
* **a trailing newline in a configured pattern passes ``_PATTERN_SAFE``.** The regex ends with ``$``,
  which also matches just before a final newline, so ``"grok/runs/*\\n"`` is accepted at load even
  though the same validation says whitespace is refused — for ``pull`` and for ``run_patterns``. TOML's
  ``\\n`` escape makes it reachable from an ordinary config file.

Controls: the declaration rule (an undeclared pattern carries every match and names the ambiguity; a
declaration outside ``pull`` is refused), the ledger cross-check (refusal, the 15-minute allowance,
``--all`` override, and `down` still stopping over a refused pull), the non-finite/oversized inventory
refusals, two declared collections each contributing their newest, the ``--`` separator, and the
literal-directory-pattern answer.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli, config  # noqa: E402
from rentguard.config import Artefacts, Budget, Config, ConfigError  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

MB = 1_000_000


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class SshPullProvider(FakeProvider):
    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


def member_list(command: str) -> list[str]:
    """The arguments the remote shell passes to tar after its ``--`` separator.

    The command is ``… tar czf <tmp> -- <members>; rc=$?; …``. Splitting the member text with
    ``shlex`` is the shell's own word-splitting rule for this (already quoted) list, which is what
    tar receives on the machine.
    """
    start = command.index(" -- ") + len(" -- ")
    try:
        end = command.index(";", start)
    except ValueError:      # the direct route has no framed tail: the members end the command
        end = len(command)
    return shlex.split(command[start:end])


class PullFixCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.artefacts.pull = ["grok/runs/*"]
        self.cfg.artefacts.local_dir = str(self.root / "artefacts")
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        self.provider = SshPullProvider()
        self.provider.add("pod-A", uptime_seconds=600, rate_usd_per_hour=2.0)
        self.inventory = ""
        self.inventory_exit = 0
        self.tar_members: dict[str, bytes] = {}
        self.tar_exit = 0
        self.timed_out = False
        self.commands: list[str] = []
        self.bounds: list[float] = []

    def capture(self, argv, target, remote, *, timeout):
        self.commands.append(remote)
        self.bounds.append(timeout)
        return self.inventory_exit, self.inventory

    def archive(self, argv, target, remote, *, sink, timeout):
        self.commands.append(remote)
        self.bounds.append(timeout)
        if self.timed_out:
            return -1, True
        with tarfile.open(fileobj=sink, mode="w:gz") as tar:
            for name, data in self.tar_members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return self.tar_exit, False

    def patched(self):
        return [
            mock.patch.object(cli, "_config", lambda: self.cfg),
            mock.patch.object(cli, "_provider", lambda cfg: self.provider),
            mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"),
            mock.patch.object(cli, "_ssh_argv", lambda cfg, pod, provider=None: (["ssh"], "pod")),
            mock.patch.object(cli, "_ssh_pty", lambda provider, pod: False),
            mock.patch.object(cli, "_ssh_capture", self.capture),
            mock.patch.object(cli, "_ssh_to_file", self.archive),
        ]

    def run_command(self, function, args) -> tuple[int, str, dict]:
        out, err = io.StringIO(), io.StringIO()
        patches = self.patched()
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = function(args)
        finally:
            for patch in reversed(patches):
                patch.stop()
        text = out.getvalue().strip()
        return code, err.getvalue(), json.loads(text) if text else {}

    def pull(self, *argv) -> tuple[int, str, dict]:
        return self.run_command(cli.cmd_pull, cli_args("pull", "--json", *argv))

    def down(self, *argv) -> tuple[int, str, dict]:
        return self.run_command(cli.cmd_down, cli_args("down", "--json", *argv))

    @staticmethod
    def line(index: int, kind: str, mtime, size: int, path: str) -> str:
        return f"{index}\t{kind}\t{mtime}\t{size}\t{path}"

    @staticmethod
    def framed(*lines: str) -> str:
        return "\n".join([cli.PULL_INVENTORY_HEADER, *lines, cli.PULL_INVENTORY_END, ""])

    def inventory_of(self, *lines: str) -> None:
        self.inventory = self.framed(*lines)

    def archives(self) -> list[str]:
        return [command for command in self.commands if "tar czf" in command]

    def open_row(self, *, started_hours_ago: float) -> dict:
        return Ledger(self.cfg.ledger_path).open(
            purpose="reattack", provider="fake", instance_id="pod-A", gpu_id="g",
            rate_usd_per_hour=2.0,
            started_at=datetime.now(timezone.utc) - timedelta(hours=started_hours_ago))


class ThePtyArchiveNamesPathsNotCharacters(PullFixCase):
    def test_the_pty_archive_members_are_the_selected_path(self) -> None:
        """The effect that matters: what tar receives. The PTY command's member list must be the
        exact selected path, so `tar` can stat it. Today the list is its characters."""
        tar = shutil.which("tar")
        if tar is None:
            self.skipTest("no tar on this host to execute the member list")

        run = self.root / "remote"
        (run / "grok" / "runs" / "run-16").mkdir(parents=True)
        (run / "grok" / "runs" / "run-16" / "out.json").write_text("{}", encoding="utf-8")
        command = cli._pull_remote_command(str(run), ["grok/runs/run-16"], pty=True)
        members = member_list(command)

        archive = self.root / "pty.tar.gz"
        result = subprocess.run([tar, "czf", str(archive), "--", *members],
                                cwd=run, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"the PTY archive asks tar for {members!r}, and tar cannot stat them: "
                         f"{result.stderr.strip()}")
        self.assertEqual(members, ["grok/runs/run-16"],
                         f"the PTY archive asks tar for {members!r}")
        with tarfile.open(archive) as tar_file:
            self.assertIn("grok/runs/run-16/out.json", tar_file.getnames())

    def test_the_direct_archive_members_are_the_selected_path_the_control(self) -> None:
        """The direct route is the control: joined once, so the same command builder hands tar the
        exact path."""
        command = cli._pull_remote_command("/workspace", ["grok/runs/run-16", "results/a.json"],
                                           pty=False)
        self.assertEqual(member_list(command), ["grok/runs/run-16", "results/a.json"], command)


class ThePatternValidationHasAWholeStringHole(PullFixCase):
    def test_a_trailing_newline_in_a_pattern_is_refused_at_load(self) -> None:
        """`_PATTERN_SAFE` ends in `$`, which matches before a final newline; a TOML `\\n` escape in
        a basic string produces exactly that, so a pattern with trailing whitespace reaches the
        remote shell — the thing the validation says it refuses."""
        path = self.root / "newline.toml"
        path.write_text('[artefacts]\npull = ["grok/runs/*\\n"]\n', encoding="utf-8")
        with self.assertRaises(ConfigError, msg="a pattern with a trailing newline was accepted"):
            config.load(path=path)
        with self.assertRaises(ConfigError):
            Artefacts(pull=["grok/runs/*\n"])
        # The declaration is held to the same rule: the trailing newline is the only reason this can
        # be refused (the pattern *is* in pull), so this is the shell-safety check itself.
        with self.assertRaises(ConfigError):
            Artefacts(pull=["grok/runs/*\n"], run_patterns=["grok/runs/*\n"])
        # The control next door: ordinary globs, including a character class, stay accepted.
        Artefacts(pull=["grok/runs/*", "results/step-[0-9]*"])


class TheDeclaredRunPatterns(PullFixCase):
    def test_a_declaration_outside_pull_is_refused(self) -> None:
        """A declaration nothing is pulled for would be a silent no-op."""
        with self.assertRaises(ConfigError):
            Artefacts(pull=["grok/runs/*"], run_patterns=["checkpoints/*"])
        Artefacts(pull=["grok/runs/*"], run_patterns=["grok/runs/*"])

    def test_an_undeclared_directory_pattern_is_not_trimmed(self) -> None:
        """The ruling's safe default: without a declaration, every matched directory is carried;
        the ambiguity is named with the declaration that would trim it."""
        self.cfg.artefacts.pull = ["grok/runs/run-16/*"]
        self.inventory_of(
            self.line(0, "d", 900, 527 * MB, "grok/runs/run-16/cache"),
            self.line(0, "d", 899, 172 * MB, "grok/runs/run-16/checkpoints"),
        )
        self.tar_members = {"grok/runs/run-16/cache/blob.bin": b"x"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")),
                         {"grok/runs/run-16/cache", "grok/runs/run-16/checkpoints"}, document)
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertIn("run_patterns", err, f"the ambiguity is not named with its fix:\n{err}")


class TheNewControls(PullFixCase):
    def test_the_ledger_cross_check_refuses_a_pre_dating_directory_and_all_overrides(self) -> None:
        self.open_row(started_hours_ago=1)
        self.inventory_of(self.line(0, "d", int(time.time() - 3 * 3600), 10 * MB,
                                   "grok/runs/run-14"))
        self.tar_members = {"grok/runs/run-14/old.json": b"{}"}

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "no-entry-since-the-run-started", document)
        self.assertEqual(self.archives(), [], "an archive was built for a refused selection")
        self.assertIn("--all", err, f"the override is not named:\n{err}")
        self.assertIn("open run started", err, f"the row's start is not named:\n{err}")

        self.commands.clear()
        code, err, document = self.pull("--all")
        self.assertEqual(code, 0, err)
        self.assertIn("grok/runs/run-14", self.archives()[-1], document)

    def test_a_directory_inside_the_skew_allowance_is_carried(self) -> None:
        self.open_row(started_hours_ago=1)
        self.inventory_of(self.line(0, "d", int(time.time() - 10 * 60), 10 * MB,
                                   "grok/runs/run-16"))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, f"a directory within the 15-minute allowance was refused:\n{err}")
        self.assertEqual(document.get("selected"), ["grok/runs/run-16"], document)

    def test_a_refused_pull_in_down_still_stops_the_machine(self) -> None:
        self.cfg.budget.stop_confirm_s = 0.0
        self.open_row(started_hours_ago=1)
        self.inventory_of(self.line(0, "d", int(time.time() - 3 * 3600), 700 * MB,
                                   "grok/runs/run-14"))

        code, err, document = self.down()

        self.assertNotEqual(code, 0, document)
        self.assertFalse(document.get("pulled"), document)
        self.assertEqual(self.provider.instances["pod-A"].status, "EXITED",
                         "a refused pull left the machine billing by default")
        self.assertIn("grok/runs/*", err, f"the patterns at risk are not named:\n{err}")

    def test_a_non_finite_mtime_is_inventory_unreadable(self) -> None:
        for value in ("nan", "inf"):
            with self.subTest(value=value):
                self.commands.clear()
                self.inventory_of(self.line(0, "d", value, 10 * MB, "grok/runs/run-16"))
                code, err, document = self.pull()
                self.assertNotEqual(code, 0, document)
                self.assertEqual(document.get("reason"), "inventory-unreadable", document)
                self.assertEqual(self.archives(), [], "the tool archived a non-finite measurement")

    def test_an_oversized_size_is_inventory_unreadable(self) -> None:
        self.inventory_of(self.line(0, "f", 900, (1 << 53) + 1, "results/huge.bin"))
        self.cfg.artefacts.pull = ["results/*"]

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "inventory-unreadable", document)
        self.assertEqual(self.archives(), [])

    def test_two_run_collections_each_contribute_their_newest(self) -> None:
        self.cfg.artefacts.pull = ["grok/runs/*", "checkpoints/*"]
        self.cfg.artefacts.run_patterns = ["grok/runs/*", "checkpoints/*"]
        self.inventory_of(
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
            self.line(1, "d", 899, 10 * MB, "checkpoints/step-100"),
            self.line(1, "d", 700, 10 * MB, "checkpoints/step-99"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")),
                         {"grok/runs/run-16", "checkpoints/step-100"}, document)
        self.assertEqual(document.get("skipped"), 2, document)

    def test_a_literal_directory_pattern_is_not_reported_unmatched(self) -> None:
        self.cfg.artefacts.pull = ["grok/runs/*", "grok/runs/run-16"]
        self.inventory_of(
            self.line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(1, "d", 900, 10 * MB, "grok/runs/run-16"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("unmatched_patterns"), [], document)
        # Undeclared, both entries are carried; the literal pattern is answered by the selection.
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertEqual(set(document.get("selected")),
                         {"grok/runs/run-15", "grok/runs/run-16"}, document)

    def test_the_archive_has_an_end_of_options_marker_on_both_routes(self) -> None:
        for pty in (False, True):
            with self.subTest(pty=pty):
                command = cli._pull_remote_command("/workspace", ["-cache"], pty=pty)
                self.assertIn(" -- ", command, f"the archive has no '--' separator: {command}")


if __name__ == "__main__":
    unittest.main()
