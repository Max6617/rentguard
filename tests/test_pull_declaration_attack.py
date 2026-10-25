"""Attack on the declared run collections (``[artefacts] run_patterns``), at an earlier revision.

The declaration is the ruling that replaced the per-pattern inference: a **declared** collection carries
only its newest directory, and an **undeclared** pattern carries everything it matches. This file aims
at the shapes where those two contracts meet, and at the measurement that tells the operator what
happened.

Attack shapes from the fixer's own list: a declared collection whose newest entry is a file; a path
named by both a declared and an undeclared pattern; ``--all`` with a declared collection and a failed
inventory; a declaration for a pattern whose matches are only files; and whether the "carried all of
them" note can be wrong when one directory is named by two patterns.

Found in an earlier revision: **a path matched by both a declared and an undeclared pattern is trimmed, and
the note then says it was not.** With ``pull = ["grok/runs/*", "grok/runs/run-1*"]`` and
``run_patterns = ["grok/runs/*"]``, the declared pattern trims ``run-15`` while the undeclared
pattern's note says "matched 2 directories … carried all of them" — and the same pull prints "1 of 2
matched" and names ``run-15`` as left on the machine.

The design rule (2026-10-17) is that the shared path is **carried**, with the reason named: carrying
honours both contracts (it is the newest of the collection *and* everything the undeclared pattern
matched), it cannot lose data, and the volume-sized bound makes the cost visible. The two tests below
assert that ruling: red before the ruling, green after it.

Recorded rather than demanded:

* a declared pattern that matches only files trims nothing (every file is carried, and nothing says the
  declaration had no effect);
* a declared collection whose newest match is a file still carries every file — the "newest entry" rule
  is about directories, which is the safe reading of the contract.
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import re
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

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

MB = 1_000_000


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class SshPullProvider(FakeProvider):
    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


class DeclarationCase(unittest.TestCase):
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
        self.timed_out = False
        self.commands: list[str] = []

    def capture(self, argv, target, remote, *, timeout):
        self.commands.append(remote)
        return self.inventory_exit, self.inventory

    def archive(self, argv, target, remote, *, sink, timeout):
        self.commands.append(remote)
        if self.timed_out:
            return -1, True
        with tarfile.open(fileobj=sink, mode="w:gz") as tar:
            for name, data in self.tar_members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return 0, False

    def pull(self, *argv) -> tuple[int, str, dict]:
        args = cli_args("pull", "--json", *argv)
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(cli, "_config", lambda: self.cfg),
            mock.patch.object(cli, "_provider", lambda cfg: self.provider),
            mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"),
            mock.patch.object(cli, "_ssh_argv", lambda cfg, pod, provider=None: (["ssh"], "pod")),
            mock.patch.object(cli, "_ssh_pty", lambda provider, pod: False),
            mock.patch.object(cli, "_ssh_capture", self.capture),
            mock.patch.object(cli, "_ssh_to_file", self.archive),
        ]
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.cmd_pull(args)
        finally:
            for patch in reversed(patches):
                patch.stop()
        text = out.getvalue().strip()
        return code, err.getvalue(), json.loads(text) if text else {}

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


class TheTwoContractsMeetOnOnePath(DeclarationCase):
    """The design rule (2026-10-17): a path matched by **both** a declared collection and an
    undeclared pattern is **carried**, with the reason named. Carrying honours both contracts — it is
    the newest of the collection *and* everything the undeclared pattern matched — it cannot lose
    data, and the volume-sized bound makes the cost visible. The tests here are red in an earlier revision,
    where the declaration trims the shared path and the note claims otherwise."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.artefacts.pull = ["grok/runs/*", "grok/runs/run-1*"]
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        # Both patterns match both runs: the declared collection would trim to the newest, the
        # undeclared one promises everything it matches.
        self.inventory_of(
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
            self.line(1, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(1, "d", 800, 10 * MB, "grok/runs/run-15"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

    def test_a_shared_path_is_carried_with_the_reason_named(self) -> None:
        """The ruling: carry it, and say why. Nothing is left on the machine for a pattern that
        promised everything it matched."""
        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")),
                         {"grok/runs/run-16", "grok/runs/run-15"}, document)
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertNotIn("left on the machine", err, f"a shared path was skipped:\n{err}")
        reasons = [line for line in err.splitlines()
                   if re.search(r"(?i)(declared|run_patterns|undeclared)", line)]
        self.assertTrue(reasons, f"the shared path is carried but no note names why:\n{err}")

    def test_the_carried_all_note_is_true_for_every_pattern(self) -> None:
        """The count in each "matched N directories" note is the number of those directories the pull
        actually carried. A note that overstates it is the false statement this file exists to stop."""
        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        selected = list(document.get("selected") or [])
        notes = re.findall(r"pattern\s*: (\S+) matched (\d+) directories", err)
        if not notes:      # a fix may suppress the note; then it cannot be false
            return
        for pattern, count in notes:
            carried = [path for path in selected if fnmatch.fnmatch(path, pattern)]
            self.assertEqual(int(count), len(carried),
                             f"the note says {pattern} matched {count} directories, and the pull "
                             f"carried {len(carried)} of them: {err}")


class TheDeclarationAppliesToDirectoriesOnly(DeclarationCase):
    def test_a_declared_pattern_that_matches_only_files_trims_nothing(self) -> None:
        """A declaration for a file pattern has no effect: every file is carried. Recorded — the
        safe direction, but the operator may believe the declaration trims to the newest file."""
        self.cfg.artefacts.pull = ["results/*"]
        self.cfg.artefacts.run_patterns = ["results/*"]
        self.inventory_of(
            self.line(0, "f", 900, 100, "results/latest.json"),
            self.line(0, "f", 800, 100, "results/older.json"),
        )
        self.tar_members = {"results/latest.json": b"{}", "results/older.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("selected"),
                         ["results/latest.json", "results/older.json"], document)
        self.assertEqual(document.get("skipped"), 0, document)

    def test_a_declared_collection_whose_newest_match_is_a_file_carries_every_file(self) -> None:
        """A declared collection can match a file newer than its newest directory. The rule is about
        directories: every file is carried, and the newest *directory* is the one chosen. Recorded —
        trimming to the newest entry read literally would drop ``run-old`` and gain nothing."""
        self.cfg.artefacts.pull = ["results/*"]
        self.cfg.artefacts.run_patterns = ["results/*"]
        self.inventory_of(
            self.line(0, "f", 900, 100, "results/latest.json"),
            self.line(0, "d", 800, 5000, "results/run-old"),
        )
        self.tar_members = {"results/latest.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")), {"results/latest.json", "results/run-old"},
                         document)
        self.assertEqual(document.get("skipped"), 0, document)


class TheBackfillDoesNotBypassTheMeasurement(DeclarationCase):
    def test_all_with_a_declared_collection_and_an_unreadable_inventory_still_fails(self) -> None:
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory = "this is not the inventory protocol\n"

        code, err, document = self.pull("--all")

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "inventory-unreadable", document)
        self.assertEqual(self.archives(), [],
                         f"--all archived without a measurement: {self.archives()}")

    def test_all_with_a_declared_collection_carries_every_entry(self) -> None:
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory_of(
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull("--all")

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")), {"grok/runs/run-16", "grok/runs/run-15"},
                         document)
        self.assertEqual(document.get("skipped"), 0, document)


class TheAmbiguityNoteCountsMembers(DeclarationCase):
    def test_two_undeclared_patterns_matching_the_same_dirs_report_members_once(self) -> None:
        """One directory named by two patterns is one member (the parser merges it). The note's count
        and volume must be the member's, not one per pattern-match — and the note is per pattern,
        which is noise but not a wrong number."""
        self.cfg.artefacts.pull = ["grok/runs/*", "grok/runs/run-*"]
        self.inventory_of(
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
            self.line(1, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(1, "d", 800, 10 * MB, "grok/runs/run-15"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")), {"grok/runs/run-16", "grok/runs/run-15"},
                         document)
        self.assertEqual(document.get("skipped"), 0, document)
        notes = re.findall(r"pattern\s*: (\S+) matched (\d+) directories, ([0-9.]+) MB",
                           err)
        self.assertEqual(len(notes), 2, f"the note is not one per undeclared pattern:\n{err}")
        for pattern, count, volume in notes:
            self.assertEqual((pattern, count, volume), (pattern, "2", "20.0"),
                             f"the note counts pattern matches, not members: {err}")
        self.assertNotIn("left on the machine", err, f"something was called skipped:\n{err}")


if __name__ == "__main__":
    unittest.main()
