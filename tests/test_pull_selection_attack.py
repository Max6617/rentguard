"""Attack on the pull transport's *measurement*: the selection it makes and the bound it sizes.

Revision under attack: ``f873f00`` (code ``6f5b16d``). The change measures the machine before it
transfers: an inventory names every entry each pattern matches, the pull carries the **newest
directory** plus every file, and the bound is ``max(pull_timeout_s, ceil(volume / bytes_per_second))``.

Four shapes were named as the ones to hit hardest, plus my own:

1. a run directory whose mtime does not sort newest (preserved timestamp / clock skew): the pull then
   carries an older cell and says nothing;
2. two runs finished in the same second: the tie is broken by path, so ``run-9`` beats ``run-10``;
3. an inventory that is readable but names an older run than the one the open ledger row recorded: the
   pull must not silently deliver it;
4. ``--all`` on a mixed pattern, and ``--all`` when a pattern matches nothing.

My own attack surface, because the *measurement* is what the whole pull now trusts: a non-finite mtime
or a non-finite configured rate/floor, a byte count the bound arithmetic cannot convert, two directory
patterns (or one pattern inside a run) which the global "newest directory" rule collapses to one
directory, and a duplicate member that is counted as skipped while it is carried.

The costs this file defends are the ones the transport fix was written for: an artefact that was
written on the machine and is not carried, and a bound that lets a billing machine outlive its pull.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
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
    """The fake, with artefacts over ssh, so `cmd_pull` takes the measured tar path."""

    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


class PullAttackCase(unittest.TestCase):
    """One fake machine, a scripted inventory, and a scripted archive command.

    The fake ssh records the *remote command* it was asked to run, because that string is the
    contract: it is what the remote shell executes, and it is where a re-expanded glob, an unquoted
    path or a dropped pattern becomes visible without a rental.
    """

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

    # ------------------------------------------------------------------ the two ssh commands

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

    def pull(self, *argv) -> tuple[int, str, dict]:
        args = cli_args("pull", "--json", *argv)
        out, err = io.StringIO(), io.StringIO()
        patches = self.patched()
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

    def down(self, *argv) -> tuple[int, str, dict]:
        args = cli_args("down", "--json", *argv)
        out, err = io.StringIO(), io.StringIO()
        patches = self.patched()
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.cmd_down(args)
        finally:
            for patch in reversed(patches):
                patch.stop()
        text = out.getvalue().strip()
        return code, err.getvalue(), json.loads(text) if text else {}

    # ------------------------------------------------------------------------------- fixtures

    @staticmethod
    def line(index: int, kind: str, mtime, size: int, path: str) -> str:
        return f"{index}\t{kind}\t{mtime}\t{size}\t{path}"

    @staticmethod
    def framed(*lines: str) -> str:
        return "\n".join([cli.PULL_INVENTORY_HEADER, *lines, cli.PULL_INVENTORY_END, ""])

    def inventory_of(self, *lines: str) -> None:
        self.inventory = self.framed(*lines)

    def archive_remote(self) -> str:
        return [command for command in self.commands if "tar czf" in command][-1]

    def archives(self) -> list[str]:
        return [command for command in self.commands if "tar czf" in command]

    def open_row(self, *, started_hours_ago: float) -> dict:
        return Ledger(self.cfg.ledger_path).open(
            purpose="attack", provider="fake", instance_id="pod-A", gpu_id="g",
            rate_usd_per_hour=2.0,
            started_at=datetime.now(timezone.utc) - timedelta(hours=started_hours_ago))


class TheSelectionTrustsTheMachine(PullAttackCase):
    """Shapes 1 and 3: the newest directory is whatever the machine's clock says, and the ledger's
    own record of which run is in flight is not consulted at all."""

    def test_a_readable_inventory_older_than_the_open_run_is_not_a_silent_success(self) -> None:
        """Shape 3. The only open row began an hour ago; the machine's newest directory predates it
        by two hours. That is a readable inventory naming a different run than the one in flight, and
        the pull must not carry it as a success — the cell the run just wrote is the one that is
        missing. (Forge's named direction: prefer entries with ``mtime >=`` the open row's start.)

        Red at f873f00: exit 0, ``pulled: true``, and ``grok/runs/run-14`` extracted into local_dir. Refused since 339544f.
        """
        self.open_row(started_hours_ago=1)
        now = time.time()
        self.inventory_of(self.line(0, "d", int(now - 3 * 3600), 10 * MB, "grok/runs/run-14"))
        self.tar_members = {"grok/runs/run-14/old.json": b"{}"}

        code, err, document = self.pull()

        self.assertFalse(
            code == 0 and document.get("pulled") is True,
            f"the pull reported success carrying {document.get('selected')} while the only open run "
            f"started an hour ago and every matched directory is older than that:\n{err}")

    def test_a_preserved_old_mtime_hides_the_run_just_finished(self) -> None:
        """Shape 1, recorded rather than demanded, **for a declared collection** (the only case where
        the newest-entry rule runs at all under the declaration rule): with a preserved/restored mtime
        on the run that just finished, the newest-by-mtime directory is an earlier cell, and nothing
        names the fact.

        This pins the effect: the pull carries ``grok/runs/run-15`` and reports ``grok/runs/run-16``
        — the run the fixture just finished — as an *older* run left on the machine.
        """
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.open_row(started_hours_ago=5)
        now = time.time()
        self.inventory_of(
            self.line(0, "d", int(now - 4 * 3600), 10 * MB, "grok/runs/run-16"),
            self.line(0, "d", int(now - 2 * 3600), 10 * MB, "grok/runs/run-15"),
        )
        self.tar_members = {"grok/runs/run-15/old.json": b"{}",
                            "grok/runs/run-16/new.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("selected"), ["grok/runs/run-15"], document)
        self.assertIn("run-16", err, f"the run left behind is not named:\n{err}")


class TheTieIsBrokenByPath(PullAttackCase):
    def test_equal_mtimes_let_run_9_beat_run_10(self) -> None:
        """Shape 2. Two run directories with the same ``stat -c %Y`` second: the tie is broken by
        path, and lexicographically ``run-9`` sorts above ``run-10``. With unpadded run indexes the
        loser can be the run that finished second — and it is described as an *older* run, which the
        measurement cannot establish.

        Pinned here so the shape and its wording are in the record; the tie is the smallest window
        in the rule, not a silent absence of one.
        """
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory_of(
            self.line(0, "d", 300, 10 * MB, "grok/runs/run-9"),
            self.line(0, "d", 300, 10 * MB, "grok/runs/run-10"),
        )
        self.tar_members = {"grok/runs/run-9/a.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("selected"), ["grok/runs/run-9"], document)
        self.assertEqual(document.get("skipped"), 1, document)
        self.assertIn("older run", err, f"the equal-mtime loser is not described:\n{err}")


class TheArithmeticTrustsTheMeasurement(PullAttackCase):
    """A readable inventory is still a claim. These are the shapes where a *readable* claim makes the
    selection or the bound wrong, which the parser cannot catch by design."""

    def test_a_nan_mtime_lets_the_first_listed_directory_win(self) -> None:
        """A non-finite mtime is not a measurement, but ``float("nan")`` parses and every comparison
        against it is false, so ``max`` keeps the *first* directory the shell listed. A machine (or a
        broken ``stat`` wrapper) that reports ``nan`` for an older run makes that run the newest.

        The safe effect: the pull does not carry the older directory as a success. Red at f873f00; refused since 339544f.
        """
        self.inventory_of(
            self.line(0, "d", "nan", 10 * MB, "grok/runs/run-a-old"),
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-z-new"),
        )
        self.tar_members = {"grok/runs/run-a-old/old.json": b"{}"}

        code, err, document = self.pull()

        self.assertFalse(
            code == 0 and document.get("pulled") is True
            and document.get("selected") == ["grok/runs/run-a-old"],
            f"a nan mtime selected the first-listed directory and reported success: {document}\n{err}")

    def test_an_infinity_mtime_always_wins(self) -> None:
        """``inf`` is the opposite polarity of the same defect: it parses, and it makes whichever
        directory reports it the newest for ever, even when a run finished later with a real mtime.
        """
        self.inventory_of(
            self.line(0, "d", "inf", 10 * MB, "grok/runs/run-boom"),
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-z-new"),
        )
        self.tar_members = {"grok/runs/run-boom/old.json": b"{}"}

        code, err, document = self.pull()

        self.assertFalse(
            code == 0 and document.get("pulled") is True
            and document.get("selected") == ["grok/runs/run-boom"],
            f"an inf mtime was accepted as the newest run: {document}\n{err}")

    def test_a_byte_count_the_bound_cannot_convert_is_a_named_failure(self) -> None:
        """``int(size)`` accepts an arbitrary number of digits, and the bound divides it by a float:
        ``10**400 / 1_000_000.0`` raises ``OverflowError``. One readable inventory line therefore
        crashes the command that sizes the pull — and `down` calls this command before it stops a
        machine. It must be a named failure, not a traceback.

        Red at f873f00: ``OverflowError`` escaped ``cmd_pull``. Refused by the parser since 339544f.
        """
        self.inventory_of(self.line(0, "f", 900, 10 ** 400, "results/huge.bin"))
        self.cfg.artefacts.pull = ["results/*"]

        try:
            code, err, document = self.pull()
        except OverflowError as exc:
            self.fail(f"the bound arithmetic escaped cmd_pull as {type(exc).__name__}: {exc}")

        self.assertNotEqual(code, 0, f"an unconvertible size was accepted: {document}")

    def test_a_negative_byte_count_leaves_the_floor(self) -> None:
        """A negative size is not a size; today it is trusted and it *shrinks* the bound to the floor
        (``total_bytes <= 0``). Recorded, not demanded: the pull is then killed while a real transfer
        is still arriving, which is the direction the whole fix exists to remove.

        Green while ``_pull_bound`` treats a non-positive measurement as "nothing to carry".
        """
        self.cfg.artefacts.pull_timeout_s = 90.0
        self.cfg.artefacts.pull_bytes_per_second = 1_000_000.0
        cfg = self.cfg
        self.assertEqual(cli._pull_bound(cfg, -700 * MB), 90.0)

    def test_a_duplicate_member_is_not_an_older_run(self) -> None:
        """Two patterns matching the same directory (``grok/runs/*`` and the run by name) list it
        twice. The selection keeps one object, so the other is counted and named as an *older run
        entry* which is carried anyway: ``skipped`` says 1 while nothing was left behind.

        Red at f873f00: ``skipped`` was 2 and the prose named the delivered run among the left-behind.
        """
        self.cfg.artefacts.pull = ["grok/runs/*", "grok/runs/run-16"]
        self.cfg.artefacts.run_patterns = ["grok/runs/*"]
        self.inventory_of(
            self.line(0, "d", 800, 10 * MB, "grok/runs/run-15"),
            self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"),
            self.line(1, "d", 900, 10 * MB, "grok/runs/run-16"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("selected"), ["grok/runs/run-16"], document)
        left_behind = err.split("left on the machine:", 1)[-1]
        self.assertEqual(document.get("skipped"), 1,
                         f"one directory was carried twice and counted as skipped: {document}")
        self.assertNotIn("run-16", left_behind,
                         f"the delivered run is named as left on the machine:\n{err}")


class TheRunCollectionIsDeclared(PullAttackCase):
    """Gilbert's ruling (2026-10-17): a directory pattern is a collection of runs only when the config
    **declares** it in ``[artefacts] run_patterns``. ``grok/runs/*`` and ``grok/runs/run-16/*`` are
    the same glob shape, so the tool no longer infers the difference; an undeclared pattern carries
    everything it matches, which is the direction that cannot lose half a run."""

    def test_two_undeclared_directory_patterns_carry_every_match(self) -> None:
        """The global "newest directory" rule this attack found dropped the second collection (and,
        inside one run, half its outputs). Undeclared, every match is now carried and nothing is left
        behind; the ambiguity is named rather than silently trimmed."""
        self.cfg.artefacts.pull = ["grok/runs/*", "checkpoints/*"]
        self.inventory_of(
            self.line(0, "d", 900, 30 * MB, "grok/runs/run-16"),
            self.line(0, "d", 800, 30 * MB, "grok/runs/run-15"),
            self.line(1, "d", 899, 700 * MB, "checkpoints/step-100"),
            self.line(1, "d", 700, 650 * MB, "checkpoints/step-99"),
        )
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(set(document.get("selected")),
                         {"grok/runs/run-16", "grok/runs/run-15",
                          "checkpoints/step-100", "checkpoints/step-99"}, document)
        self.assertEqual(document.get("skipped"), 0, document)
        archive = self.archive_remote()
        for wanted in ("grok/runs/run-16", "grok/runs/run-15",
                       "checkpoints/step-100", "checkpoints/step-99"):
            self.assertIn(wanted, archive, f"an undeclared match was dropped: {archive}")
        self.assertIn("run_patterns", err, f"the ambiguity is not named with its fix:\n{err}")

    def test_a_declared_collection_carries_its_newest_and_all_fetches_the_rest(self) -> None:
        """Declared, the collection is run-bounded: the newest directory is carried and the rest is
        named as skipped with ``--all`` as the backstop; ``--all`` carries every entry."""
        self.cfg.artefacts.pull = ["grok/runs/run-16/*"]
        self.cfg.artefacts.run_patterns = ["grok/runs/run-16/*"]
        self.inventory_of(
            self.line(0, "d", 900, 527 * MB, "grok/runs/run-16/cache"),
            self.line(0, "d", 899, 172 * MB, "grok/runs/run-16/checkpoints"),
        )
        self.tar_members = {"grok/runs/run-16/cache/blob.bin": b"x"}

        code, err, document = self.pull()

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("selected"), ["grok/runs/run-16/cache"], document)
        self.assertEqual(document.get("skipped"), 1, document)
        self.assertNotIn("checkpoints", self.archive_remote(),
                         f"a declared collection carried more than its newest: {self.archive_remote()}")
        self.assertIn("checkpoints", err, f"the skipped entry is not named:\n{err}")
        self.assertIn("--all", err, f"the backstop is not named:\n{err}")

        self.commands.clear()
        self.tar_members = {"grok/runs/run-16/cache/blob.bin": b"x",
                            "grok/runs/run-16/checkpoints/step.pt": b"w"}
        code, err, document = self.pull("--all")

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        self.assertIn("grok/runs/run-16/cache", archive, archive)
        self.assertIn("grok/runs/run-16/checkpoints", archive, f"--all left one behind: {archive}")
        self.assertEqual(document.get("skipped"), 0, document)


class TheAllBackfill(PullAttackCase):
    """Shape 4: `--all` is the escape hatch and it must not inherit the one-run rule."""

    def test_all_mixed_carries_every_directory_and_file(self) -> None:
        self.cfg.artefacts.pull = ["grok/runs/*", "results/*.json"]
        self.inventory_of(
            self.line(0, "d", 800, 100 * MB, "grok/runs/run-15"),
            self.line(0, "d", 900, 100 * MB, "grok/runs/run-16"),
            self.line(1, "f", 900, 1000, "results/final.json"),
        )
        self.tar_members = {"grok/runs/run-15/a.json": b"{}",
                            "grok/runs/run-16/b.json": b"{}",
                            "results/final.json": b"{}"}

        code, err, document = self.pull("--all")

        self.assertEqual(code, 0, err)
        archive = self.archive_remote()
        for wanted in ("grok/runs/run-15", "grok/runs/run-16", "results/final.json"):
            self.assertIn(wanted, archive, archive)
        self.assertEqual(document.get("skipped"), 0, document)
        self.assertEqual(document.get("volume_bytes"), 200 * MB + 1000, document)
        self.assertNotIn("left on the machine", err, f"--all still skipped something:\n{err}")

    def test_all_with_a_pattern_that_matched_nothing_still_transfers_the_rest(self) -> None:
        self.cfg.artefacts.pull = ["grok/runs/*", "results/*.json"]
        self.inventory_of(self.line(0, "d", 900, 10 * MB, "grok/runs/run-16"))
        self.tar_members = {"grok/runs/run-16/out.json": b"{}"}

        code, err, document = self.pull("--all")

        self.assertEqual(code, 0, err)
        self.assertEqual(document.get("unmatched_patterns"), ["results/*.json"], document)
        self.assertIn("results/*.json", err, err)

    def test_all_when_every_pattern_matched_nothing_starts_no_transfer(self) -> None:
        self.cfg.artefacts.pull = ["grok/runs/*", "results/*.json"]
        self.inventory_of()

        code, err, document = self.pull("--all")

        self.assertNotEqual(code, 0, document)
        self.assertEqual(document.get("reason"), "patterns-matched-nothing", document)
        self.assertEqual(self.archives(), [], "an archive was built for patterns that matched nothing")


class TheBoundCannotBeSizedFromNothing(PullAttackCase):
    """The bound is what stops a pull from outliving the machine it bills. These are the
    configurations and measurements that make it zero or infinite."""

    def test_a_configured_floor_that_is_not_finite_is_refused(self) -> None:
        """``nan`` and ``inf`` are legal TOML floats, and the only validation is ``< 0``, which both
        pass. With ``nan`` the bound is nan (and a nan ``wait`` never expires); with ``inf`` it is
        infinite. A typo like ``1e999`` reaches inf.

        The effect asserted: the config is refused at load, or the bound it produces is finite.
        """
        for text in ("nan", "inf", "1e999"):
            with self.subTest(value=text):
                path = self.root / f"floor-{text}.toml"
                path.write_text(f"[artefacts]\npull_timeout_s = {text}\n", encoding="utf-8")
                try:
                    cfg = config.load(path=path)
                except ConfigError:
                    continue
                bound = cli._pull_bound(cfg, 0)
                self.assertTrue(math.isfinite(bound),
                                f"pull_timeout_s = {text} produces a bound of {bound}")

    def test_a_configured_rate_that_is_not_finite_is_refused(self) -> None:
        """``pull_bytes_per_second = nan`` passes ``<= 0``; the bound then calls ``math.ceil(nan)``,
        which raises ``ValueError`` — a crash where a named failure belongs.
        """
        path = self.root / "rate-nan.toml"
        path.write_text("[artefacts]\npull_bytes_per_second = nan\n", encoding="utf-8")
        try:
            cfg = config.load(path=path)
        except ConfigError:
            return
        try:
            bound = cli._pull_bound(cfg, 700 * MB)
        except ValueError as exc:
            self.fail(f"pull_bytes_per_second = nan makes the bound arithmetic raise: {exc}")
        self.assertTrue(math.isfinite(bound), f"pull_bytes_per_second = nan gives bound {bound}")

    def test_a_zero_floor_with_a_zero_byte_selection_still_has_time_to_run(self) -> None:
        """``pull_timeout_s = 0`` is documented as "no floor, pure volume". A selection of empty
        files measures zero bytes, so the bound is zero and the child is killed the instant it is
        started: a pull that would have succeeded cannot. The bound must still allow a transfer.

        Red at f873f00: ``_pull_bound(cfg, 0)`` was 0.0; the bound is clamped to PULL_MIN_BOUND_S since 339544f.
        """
        cfg = Config(artefacts=Artefacts(pull_timeout_s=0.0, pull_bytes_per_second=1_000_000.0))
        bound = cli._pull_bound(cfg, 0)
        self.assertGreater(bound, 0.0,
                           "a zero-byte pull at a zero floor gets a zero-second bound; the child is "
                           "killed before it can exit")
        with open(os.devnull, "wb") as sink:
            started = time.monotonic()
            code, timed_out = cli._ssh_to_file(
                [sys.executable, "-c", "import time; time.sleep(0.2)"], "target", "command",
                sink=sink, timeout=bound)
            elapsed = time.monotonic() - started
        self.assertEqual((code, timed_out), (0, False),
                         f"a command that exits 0 in 0.2s was killed at a bound of {bound} ({elapsed:.2f}s)")


class TheTransportFailureCannotCostMoney(PullAttackCase):
    """A timed-out pull is a failure the caller acts on — and the money rule outranks it: the machine
    still stops by default, and `--keep-running` still keeps it up deliberately."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.budget.stop_confirm_s = 0.0
        self.open_row(started_hours_ago=1)
        self.cfg.artefacts.pull_timeout_s = 120.0
        self.cfg.artefacts.pull_bytes_per_second = 1_000_000.0

    def test_a_timed_out_pull_is_not_a_clean_stop(self) -> None:
        # The directory is measured as current, so this exercises the *transport* bound rather than
        # the ledger cross-check (a pre-dating directory would be refused before a byte moves).
        self.inventory_of(self.line(0, "d", int(time.time()), 700 * MB, "grok/runs/run-16"))
        self.timed_out = True

        code, err, document = self.down()

        self.assertNotEqual(code, 0, f"down reported a clean stop after a timed-out pull: {document}")
        self.assertFalse(document.get("pulled"), document)
        self.assertEqual(self.provider.instances["pod-A"].status, "EXITED",
                         "the pull failed and the machine was left billing")
        self.assertIn("did not return within", err, f"the transport bound was not the failure:\n{err}")
        self.assertIn("keep-running", err, f"the failure names no way to retry:\n{err}")

    def test_keep_running_keeps_the_machine_up_and_says_so(self) -> None:
        self.inventory_of(self.line(0, "d", int(time.time()), 700 * MB, "grok/runs/run-16"))
        self.timed_out = True

        code, err, document = self.down("--keep-running")

        self.assertNotEqual(code, 0, document)
        self.assertEqual(self.provider.instances["pod-A"].status, "RUNNING",
                         "--keep-running did not keep the machine running")
        self.assertIn("did not return within", err, f"the transport bound was not the failure:\n{err}")
        self.assertIn("BILLING", err, err)


class TheArchiveCannotWriteOutsideLocalDir(PullAttackCase):
    def test_a_parent_member_is_refused_and_writes_nothing_outside(self) -> None:
        """The archive command carries exact paths from the inventory, and that inventory is the
        machine's answer. A member that climbs out of ``local_dir`` must not be extracted there."""
        self.inventory_of(self.line(0, "f", 900, 10, "results/ok.json"))
        self.tar_members = {"results/ok.json": b"{}", "../escaped.txt": b"owned"}

        code, err, document = self.pull()

        self.assertNotEqual(code, 0, f"an archive with a parent member was reported as a success: {document}")
        self.assertFalse((self.root / "escaped.txt").exists(),
                         "a tar member escaped local_dir")
        self.assertFalse((Path(self.cfg.artefacts.local_dir).parent / "escaped.txt").exists(),
                         "a tar member escaped local_dir")


if __name__ == "__main__":
    unittest.main()
