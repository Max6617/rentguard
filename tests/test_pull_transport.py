"""The artefact transport: a bound sized to the volume, and one run per pull.

Measured 2026-10-17 on a rental window (the operator's numbers, verified against the ledger): a run directory
is ~700 MB (527 MB cache + 172 MB step-named checkpoints + the rest), `[artefacts] pull_timeout_s` defaulted
to 120 s, and one window logged **26 timeouts**. Worse, the configured pattern was ``grok/runs/*``, which
makes every pull re-archive every earlier run as well — each pull larger than the last, so the failure
compounds and the first pull alone succeeded, because only one directory existed.

Two contracts this file pins, both without a rental (the fake provider, a scratch directory and a fake ssh
that answers two commands):

* the bound comes from the **volume of what this pull will carry**, not from a legacy constant, and a pull
  that exceeds it names the volume, the bound and the next step — it is a failure, not a log line;
* the archive carries **the run just finished** (the newest entry when a pattern matches directories), and
  what it left behind is named, because a silent "1 of 16" is how this cost a window.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tarfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Budget, Config, ConfigError  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

MB = 1_000_000


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class SshPullProvider(FakeProvider):
    """The fake, but artefacts come over ssh as RunPod's do, so `cmd_pull` takes the tar path."""

    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


def inventory_line(index: int, kind: str, mtime: float, size: int, path: str) -> str:
    """One inventory member: the configured pattern's **index**, kind, mtime, bytes, path."""
    return f"{index}\t{kind}\t{mtime:g}\t{size}\t{path}"


def framed(body: str) -> str:
    """The inventory answer as the machine sends it: between its two marks.

    The frame is not decoration — a PTY route echoes the command and folds prompt text into stdout, so
    the tool reads only the region between the marks and treats a missing mark as a failed measurement
    rather than as an empty inventory.
    """
    return f"{cli.PULL_INVENTORY_HEADER}\n{body}\n{cli.PULL_INVENTORY_END}\n"


class TransportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.artefacts.pull = ["grok/runs/*"]
        # Declared as a collection of runs, so the cases about "the run just finished" keep their
        # meaning; the declaration's own behaviour has its own class below.
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
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

    # ------------------------------------------------------------- the two fake ssh commands

    def capture(self, argv, target, remote, *, timeout):
        """The inventory call: the tool asks the machine what the patterns *are* before it transfers."""
        self.commands.append(remote)
        self.bounds.append(timeout)
        return self.inventory_exit, self.inventory or ""

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

    def pull(self, *argv) -> tuple[int, str, dict]:
        args = cli_args("pull", "--json", *argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                mock.patch.object(cli, "_ssh_argv", lambda cfg, pod, provider=None: (["ssh"], "pod")), \
                mock.patch.object(cli, "_ssh_pty", lambda provider, pod: False), \
                mock.patch.object(cli, "_ssh_capture", self.capture), \
                mock.patch.object(cli, "_ssh_to_file", self.archive), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_pull(args)
        text = out.getvalue().strip()
        return code, err.getvalue(), json.loads(text) if text else {}

    # ------------------------------------------------------------------------------- fixtures

    def runs(self, *runs: tuple[str, float, int]) -> str:
        """A framed inventory of run directories: (name, mtime, bytes)."""
        body = "\n".join(inventory_line(0, "d", mtime, size, f"grok/runs/{name}")
                         for name, mtime, size in runs)
        return framed(body)

    def archive_remote(self) -> str:
        """The archive command (the one that is not the inventory)."""
        return [command for command in self.commands if "tar czf" in command][-1]

    def delivered(self) -> list[str]:
        dest = Path(self.cfg.artefacts.local_dir)
        return sorted(str(path.relative_to(dest)).replace("\\", "/")
                      for path in dest.rglob("*") if path.is_file())


class TheBoundIsSizedFromTheVolume(TransportCase):
    def test_the_archive_bound_comes_from_this_pulls_volume(self) -> None:
        """700 MB at the default 1 MB/s is a 700 s bound, not the 120 s legacy default: the
        window's runs sat on that default and timed out 26 times."""
        volume = 700 * MB
        self.inventory = self.runs(("run-16", 300.0, volume))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, f"a sized pull failed: {err}")
        self.assertEqual(self.bounds[-1], 700,
                         f"the archive was bounded at {self.bounds[-1]:g}s for a {volume} byte pull")
        self.assertEqual(document.get("volume_bytes"), volume, document)
        self.assertEqual(document.get("timeout_s"), 700, document)

    def test_a_small_pull_still_gets_the_configured_floor(self) -> None:
        """The configured `pull_timeout_s` is a floor, not the whole bound: a tiny pull does not get a
        one-second bound just because its volume is tiny."""
        self.cfg.artefacts.pull_timeout_s = 90.0
        self.inventory = self.runs(("run-1", 100.0, 1000))
        self.tar_members = {"grok/runs/run-1/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(self.bounds[-1], 90.0, f"the floor was not honoured: {self.bounds[-1]}")

    def test_a_pull_that_exceeds_its_bound_fails_and_names_the_volume_and_the_next_step(self) -> None:
        """A timeout is a failure the caller must act on. It names the volume it was sized from, the
        bound it hit, and what to do — `down --keep-running` keeps the machine for a retry instead of
        stopping it over a result that is still there."""
        self.inventory = self.runs(("run-16", 300.0, 700 * MB))
        self.timed_out = True

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, f"a timed-out pull exited 0: {document}")
        self.assertFalse(document.get("pulled"), document)
        self.assertEqual(document.get("reason"), "ssh-timeout", document)
        self.assertEqual(document.get("volume_bytes"), 700 * MB, document)
        self.assertIn("700", err, f"the volume is not named:\n{err}")
        self.assertIn("--keep-running", err, f"the timeout names no next step:\n{err}")


class ThePullCarriesOneRun(TransportCase):
    def test_only_the_newest_run_is_archived(self) -> None:
        """`grok/runs/*` used to re-archive every earlier run on every pull. The archive carries the
        run just finished — the newest entry — and says how many it left behind."""
        self.inventory = self.runs(("run-14", 100.0, 10 * MB), ("run-15", 200.0, 20 * MB),
                                   ("run-16", 300.0, 30 * MB))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("grok/runs/run-16", archive, archive)
        self.assertNotIn("grok/runs/run-14", archive, f"an earlier run was re-archived: {archive}")
        self.assertNotIn("grok/runs/run-15", archive, f"an earlier run was re-archived: {archive}")
        self.assertEqual(document.get("selected"), ["grok/runs/run-16"], document)
        self.assertEqual(document.get("skipped"), 2, document)
        self.assertIn("2", err, f"what was left behind is not named:\n{err}")
        self.assertEqual(self.delivered(), ["grok/runs/run-16/out.json"])

    def test_all_fetches_every_run(self) -> None:
        """The escape hatch for a backfill: `--all` archives every matched entry, and the bound is
        sized from the whole selection."""
        self.inventory = self.runs(("run-14", 100.0, 10 * MB), ("run-15", 200.0, 20 * MB),
                                   ("run-16", 300.0, 30 * MB))
        self.tar_members = {"grok/runs/run-14/a.json": b"{}", "grok/runs/run-16/b.json": b"{}"}

        code, err, document = self.pull("--all")

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        for run in ("run-14", "run-15", "run-16"):
            self.assertIn(f"grok/runs/{run}", archive, f"{run} is missing from an --all archive")
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertEqual(document.get("volume_bytes"), 60 * MB, document)

    def test_a_file_pattern_is_not_run_bounded(self) -> None:
        """A pattern that matches *files* is one run's outputs already; it is not re-archiving a
        collection of runs, so every match is carried."""
        self.cfg.artefacts.pull = ["results/*.json"]
        self.inventory = framed("\n".join([
            inventory_line(0, "f", 100, 10, "results/a.json"),
            inventory_line(0, "f", 200, 20, "results/b.json"),
        ]))
        self.tar_members = {"results/a.json": b"a", "results/b.json": b"b"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("results/a.json", archive, archive)
        self.assertIn("results/b.json", archive, archive)
        self.assertEqual(document.get("skipped"), 0, document)

    def test_a_mixed_selection_takes_the_newest_run_and_the_files(self) -> None:
        """Directories are the runs; files are the patterns that are already one run's. A mixed config
        carries the newest run plus every file, and skips the older runs."""
        self.cfg.artefacts.pull = ["grok/runs/*", "results/*.json"]
        self.inventory = framed(
            "\n".join([inventory_line(0, "d", mtime, size, f"grok/runs/{name}")
                       for name, mtime, size in (("run-15", 200.0, 20 * MB),
                                                 ("run-16", 300.0, 30 * MB))]
                      + [inventory_line(1, "f", 300, 100, "results/final.json")]))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}", "results/final.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("grok/runs/run-16", archive, archive)
        self.assertNotIn("grok/runs/run-15", archive, f"an older run was carried: {archive}")
        self.assertIn("results/final.json", archive, archive)
        self.assertEqual(document.get("skipped"), 1, document)


class TheRunCollectionIsDeclared(TransportCase):
    """`[artefacts] run_patterns` declares which patterns are **collections of runs** (the design rule,
    2026-10-17). A declared collection carries the newest entry and names the rest; an **undeclared**
    pattern carries everything it matches, because a pattern cannot be told apart from one run's
    contents by its shape — `grok/runs/*` and `grok/runs/run-16/*` are the same glob — and carrying the
    wrong half of a run is the loss this whole exercise exists to prevent.
    """

    def test_a_declared_collection_carries_the_newest_and_names_the_rest(self) -> None:
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory = self.runs(("run-15", 200.0, 20 * MB), ("run-16", 300.0, 30 * MB))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("grok/runs/run-16", archive, archive)
        self.assertNotIn("grok/runs/run-15", archive, archive)
        self.assertEqual(document.get("skipped"), 1, document)
        self.assertIn("left on the machine", err, err)

    def test_an_undeclared_directory_pattern_carries_every_match(self) -> None:
        """The default cannot silently skip: two directories under one pattern are carried, and the
        output says how many and how large they are, and how to opt into the one-run rule."""
        self.cfg.artefacts.pull = ["grok/runs/run-16/*"]
        self.inventory = framed("\n".join([
            inventory_line(0, "d", 900, 527 * MB, "grok/runs/run-16/cache"),
            inventory_line(0, "d", 899, 172 * MB, "grok/runs/run-16/checkpoints"),
        ]))
        self.tar_members = {"grok/runs/run-16/cache/blob.bin": b"x",
                            "grok/runs/run-16/checkpoints/step-100.pt": b"y"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("grok/runs/run-16/cache", archive, archive)
        self.assertIn("grok/runs/run-16/checkpoints", archive,
                      f"an undeclared pattern dropped half a run: {archive}")
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertIn("grok/runs/run-16/cache", err, err)
        self.assertIn("run_patterns", err, f"the declaration is not offered:\n{err}")

    def test_all_carries_every_match_even_for_a_declared_collection(self) -> None:
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory = self.runs(("run-15", 200.0, 20 * MB), ("run-16", 300.0, 30 * MB))
        self.tar_members = {"grok/runs/run-15/a.json": b"{}", "grok/runs/run-16/b.json": b"{}"}

        code, err, document = self.pull("--all")

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        for run in ("run-15", "run-16"):
            self.assertIn(f"grok/runs/{run}", archive, archive)
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertNotIn("left on the machine", err, err)

    def test_a_declaration_that_names_a_pattern_not_in_pull_is_refused(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            Artefacts(pull=["results/*.json"], run_patterns=["grok/runs/*"])
        self.assertIn("run_patterns", str(caught.exception))

    def test_a_run_pattern_is_held_to_the_same_shell_safety(self) -> None:
        with self.assertRaises(ConfigError):
            Artefacts(pull=["grok/runs/*"], run_patterns=["grok/runs/*; rm -rf /"])


class TheTwoContractsMeetOnOnePath(TransportCase):
    """A directory named by a declared collection **and** by an undeclared pattern is carried, and the
    reason is named: carrying honours both contracts, cannot lose data, and the declaration's trim is an
    optimisation rather than a promise (the design rule, 2026-10-20)."""

    def overlap_setup(self) -> None:
        self.cfg.artefacts.pull = ["grok/runs/*", "grok/runs/run-1*"]
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory = framed("\n".join([
            inventory_line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            inventory_line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
            inventory_line(1, "d", 900, 10 * MB, "grok/runs/run-16"),
            inventory_line(1, "d", 800, 10 * MB, "grok/runs/run-15"),
        ]))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}", "grok/runs/run-15/old.json": b"{}"}

    def test_a_path_named_by_both_kinds_of_pattern_is_carried_with_the_reason(self) -> None:
        self.overlap_setup()

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("grok/runs/run-15", archive,
                      f"a path matched by an undeclared pattern was dropped by the declaration: {archive}")
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertIn("also matched by an undeclared pattern", err,
                      f"the reason it was carried is not named:\n{err}")
        self.assertNotIn("left on the machine", err, f"nothing was skipped, but the pull says so:\n{err}")

    def test_the_ambiguity_note_is_true_about_what_was_carried(self) -> None:
        """The note describes the **carried** subset, so it cannot say "carried all of them" while the
        pull's own report names a skipped run (attack finding, 2026-10-20)."""
        self.overlap_setup()

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertIn("2 of 2", err, err)                    # the scope line is the truth
        self.assertIn("matched 2 directories", err, err)
        self.assertIn("carried all of them", err, err)
        self.assertNotIn("left on the machine", err, err)

    def test_a_declared_collection_alone_still_trims_an_older_run(self) -> None:
        """The ruling is about the overlap, not about the declaration: a directory named only by a
        declared collection is still trimmed and named."""
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory = self.runs(("run-15", 200.0, 20 * MB), ("run-16", 300.0, 30 * MB))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("skipped"), 1, document)
        self.assertIn("left on the machine", err, err)
        self.assertNotIn("grok/runs/run-15", self.archive_remote(), self.archive_remote())


class TheInventoryDecidesBeforeAnythingIsTransferred(TransportCase):
    def test_an_inventory_that_matches_nothing_starts_no_transfer(self) -> None:
        """The patterns matched nothing: the pull fails as `patterns-matched-nothing` *before* an
        archive is built, instead of moving bytes to find that out."""
        self.inventory = framed("")
        self.cfg.artefacts.pull = ["results/*.json"]

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "patterns-matched-nothing", document)
        self.assertEqual([command for command in self.commands if "tar czf" in command], [],
                         "an archive was built for patterns that matched nothing")
        self.assertEqual(document.get("unmatched_patterns"), ["results/*.json"], document)

    def test_an_unreadable_inventory_is_a_named_failure_not_a_wild_archive(self) -> None:
        """If the inventory cannot be read, nothing is archived: falling back to the old
        archive-the-root behaviour is the defect itself, and it would do it silently."""
        self.inventory = "this is not the inventory protocol\n"

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "inventory-unreadable", document)
        self.assertEqual([command for command in self.commands if "tar czf" in command], [],
                         "the tool archived the root after failing to read its own inventory")
        self.assertIn("inventory", err, err)

    def test_an_inventory_that_never_returns_is_a_named_timeout(self) -> None:
        """The measurement is bounded by the configured floor: a hung ssh before any bytes move is a
        pull that cannot be sized, and it must say so rather than wait."""
        self.inventory_exit = -1                  # killed at the bound, as a real child is

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "ssh-timeout", document)
        self.assertIn("did not return", document.get("detail", ""), document)
        self.assertEqual([command for command in self.commands if "tar czf" in command], [])

    def test_an_inventory_command_that_fails_is_a_named_failure(self) -> None:
        """A machine whose shell cannot answer (no `stat`, a refused working directory) names the exit
        code and starts no transfer."""
        self.inventory_exit = 3

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "inventory-unreadable", document)
        self.assertIn("exited 3", document.get("detail", ""), document)
        self.assertEqual([command for command in self.commands if "tar czf" in command], [])


class TheInventoryProtocolIsChecked(TransportCase):
    """The parser is strict inside the frame: a member it cannot read is a failure, never a smaller
    volume (a bound sized from a truncated inventory is a pull killed while it was still arriving)."""

    def parse(self, body: str) -> list:
        return cli._parse_pull_inventory(framed(body), ["grok/runs/*"])

    def test_a_missing_terminator_is_a_failure(self) -> None:
        with self.assertRaises(ValueError) as caught:
            cli._parse_pull_inventory(f"{cli.PULL_INVENTORY_HEADER}\n0\td\t1\t2\tx\n",
                                      ["grok/runs/*"])
        self.assertIn("terminator", str(caught.exception))

    def test_a_line_that_is_not_five_fields_is_a_failure(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.parse("0\td\t1\t2\n")
        self.assertIn("five", str(caught.exception))

    def test_a_malformed_line_is_a_failure(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.parse("0\tx\t1\t2\tpath\n")
        self.assertIn("malformed", str(caught.exception))

    def test_a_pattern_index_that_does_not_exist_is_a_failure(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.parse("7\td\t1\t2\tpath\n")
        self.assertIn("only 1 are configured", str(caught.exception))

    def test_a_non_numeric_size_is_a_failure(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.parse("0\td\t1\tbig\tpath\n")
        self.assertIn("non-numeric", str(caught.exception))

    def test_blank_lines_inside_the_frame_are_ignored(self) -> None:
        entries = self.parse("\n0\td\t1\t2\tgrok/runs/run-1\n\n")

        self.assertEqual([entry.path for entry in entries], ["grok/runs/run-1"])


class TheBoundAndItsUnits(unittest.TestCase):
    def test_a_zero_volume_leaves_the_floor(self) -> None:
        cfg = Config(artefacts=Artefacts(pull_timeout_s=90.0, pull_bytes_per_second=1_000_000.0))
        self.assertEqual(cli._pull_bound(cfg, 0), 90.0)

    def test_sizes_are_shown_in_units_a_person_can_check(self) -> None:
        self.assertEqual(cli._human_bytes(512), "512 B")
        self.assertEqual(cli._human_bytes(2000), "2.0 KB")
        self.assertEqual(cli._human_bytes(700 * MB), "700.0 MB")
        self.assertEqual(cli._human_bytes(5 * 1000 * MB), "5.0 GB")

    def test_the_config_refuses_a_bound_it_cannot_size(self) -> None:
        with self.assertRaises(ConfigError):
            Artefacts(pull_bytes_per_second=0)
        with self.assertRaises(ConfigError):
            Artefacts(pull_timeout_s=-1)

    def test_the_measurement_call_is_itself_bounded(self) -> None:
        """`_ssh_capture` against a real child that will not return: killed at the bound, `-1`."""
        started = time.monotonic()
        code, out = cli._ssh_capture([sys.executable, "-c", "import time; time.sleep(30)"],
                                     "ignored-target", "ignored-command", timeout=0.3)
        elapsed = time.monotonic() - started

        self.assertEqual((code, out), (-1, ""))
        self.assertLess(elapsed, 10.0, f"the measurement was not bounded: {elapsed:.1f}s")



if __name__ == "__main__":
    unittest.main()
