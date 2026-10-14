"""Attacks on `a1848ef` (a pull pattern that matched nothing), from outside its own tests.

The fix says a non-zero tar exit is no longer a transport failure: the members that arrived are
extracted, `unmatched_patterns` names the pattern that matched nothing beside `tar_exit_code`, and
only an empty or unopenable archive is a failed pull. Three experiments:

1. an archive with **no members** must still fail — the one case where there is genuinely nothing to
   deliver, and the case a careless "tar exited non-zero is fine now" would silently pass;
2. a run whose pattern matched nothing must not be reported as a successful pull of artefacts that do
   not exist: the members are delivered, and the unanswered pattern is named;
3. `unmatched_patterns` attributes by **literal prefix**, so there is a shape where it over-claims a
   match — named below with the case that shows it.

The ssh child here is a real process that writes a real tar stream to stdout and exits with a real
code, so the archive parsing and the exit-code handling are the product's own.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

#: Runs in a child: write the archive whose hex is argv[1] to stdout, then exit with argv[2].
WRITER = ("import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); "
          "sys.stdout.buffer.flush(); sys.exit(int(sys.argv[2]))")


def tar_hex(members: dict[str, bytes]) -> str:
    """A real gzip tar stream, hex-encoded so it can travel in argv."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue().hex()


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class SshPullProvider(FakeProvider):
    """The fake, but artefacts come over ssh (as RunPod's do), so `cmd_pull` takes the tar path."""

    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


class PullCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.artefacts.local_dir = str(self.root / "artefacts")
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        self.ledger = Ledger(self.cfg.ledger_path)
        self.ledger.open(purpose="pull", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0, started_at=now_utc())
        self.provider = SshPullProvider()
        self.provider.add("pod-A", uptime_seconds=600, rate_usd_per_hour=2.0)

    def pull(self, patterns: list[str], *, tar_members: dict[str, bytes],
             tar_exit: int) -> tuple[int, dict]:
        self.cfg.artefacts.pull = patterns
        child = [sys.executable, "-c", WRITER, tar_hex(tar_members), str(tar_exit)]
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(cli, "_ssh_argv", lambda cfg, pod, provider=None: (child, "x")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = cli.cmd_pull(cli_args("pull", "--json"))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        text = out.getvalue().strip()
        document = json.loads(text) if text else {}
        return code, document


class AnEmptyArchiveIsAFailure(PullCase):
    def test_an_archive_with_no_members_still_fails(self):
        """Sequence: the configured pattern matches nothing on the machine, so tar archives no
        member and exits 2.

        Effect required: the pull fails, and the document says the patterns matched nothing rather
        than reporting a successful delivery of zero files. A "non-zero tar exit is fine now" fix
        that lost this case would call a completely empty result a success.
        """
        code, document = self.pull(["results/*.json"], tar_members={}, tar_exit=2)

        self.assertNotEqual(code, 0, f"an empty archive was reported as a clean pull: {document}")
        self.assertFalse(document.get("pulled"), f"an empty archive was called pulled: {document}")
        self.assertEqual(document.get("reason"), "patterns-matched-nothing", document)
        self.assertEqual(document.get("files"), [], document)
        self.assertEqual(document.get("unmatched_patterns"), ["results/*.json"], document)


class MembersAreDeliveredAndTheGapIsNamed(PullCase):
    def test_a_matched_member_is_extracted_even_when_another_pattern_matched_nothing(self):
        """Sequence: the archive holds `results/out.json`; the config asks for `results/*.json` and
        `logs/*.log`; tar exits 2 (it could not find the logs).

        Effect required: the delivered member is on disk and named in `files`, the pull is *not*
        reported as a failure, and `logs/*.log` is named as matching nothing. Reading the exit code
        alone used to lose the only copy of a successful run's results.
        """
        code, document = self.pull(["results/*.json", "logs/*.log"],
                                   tar_members={"results/out.json": b'{"ok": true}'}, tar_exit=2)

        self.assertEqual(code, 0, f"a delivered member was reported as a failed pull: {document}")
        self.assertTrue(document.get("pulled"), document)
        self.assertEqual(document.get("files"), ["results/out.json"], document)
        self.assertEqual(document.get("unmatched_patterns"), ["logs/*.log"], document)
        self.assertEqual(document.get("tar_exit_code"), 2, document)
        landed = Path(self.cfg.artefacts.local_dir) / "results" / "out.json"
        self.assertTrue(landed.exists(), f"the delivered member was not written to {landed}")

    def test_a_pattern_that_matched_nothing_never_claims_a_file_that_does_not_exist(self):
        """Effect: `files` lists what arrived, nothing more. A pull that matched only the JSON
        pattern must not name a log file."""
        code, document = self.pull(["results/*.json", "logs/*.log"],
                                   tar_members={"results/out.json": b"x"}, tar_exit=2)

        self.assertEqual(document.get("files"), ["results/out.json"], document)
        self.assertNotIn("logs/", json.dumps(document.get("files")), document)


class ThePrefixRuleOverClaims(PullCase):
    def test_a_sibling_file_under_the_prefix_hides_an_unanswered_pattern(self):
        """Sequence: the config asks for `results/*.json`; the machine delivers only
        `results/notes.txt` (same directory, different extension); tar exits 0 because it did match
        *something*.

        Effect required: `results/*.json` is named as unmatched, because no `.json` was archived.
        `_unmatched_patterns` compares literal prefixes, so anything under `results/` answers the
        pattern — this is the over-claim, and the consequence is a run whose JSON artefacts are
        missing being reported as a clean pull with nothing to flag.
        """
        code, document = self.pull(["results/*.json"],
                                   tar_members={"results/notes.txt": b"notes"}, tar_exit=0)

        self.assertEqual(document.get("unmatched_patterns"), ["results/*.json"],
                         "the pattern that matched no .json was reported as answered: the prefix "
                         f"rule counts any sibling under results/ as a match. document: {document}")

    def test_the_helper_names_a_pattern_with_no_prefix_match(self):
        """Control: when nothing under the prefix arrived at all, the helper does name the pattern,
        so the over-claim above is about the prefix rule and not about the helper never firing."""
        self.assertEqual(cli._unmatched_patterns(["logs/*.log"], ["results/out.json"]),
                         ["logs/*.log"])
        self.assertEqual(cli._unmatched_patterns(["results/*.json"], ["results/out.json"]), [])


class TheMatcherInBothDirections(PullCase):
    """`_unmatched_patterns` tries each pattern literally and with `**/` collapsed, because `fnmatch`
    has no recursive-wildcard semantics. Both directions matter: an over-claim says a pattern was
    answered by a member it should not match (the original defect), and an under-claim flags a clean
    pull as having lost something (its mirror)."""

    def test_a_leading_recursive_wildcard_answers_a_top_level_member(self):
        """Control: `**/*.json` and a member at the root — the collapse exists for exactly this, so
        it must keep working."""
        self.assertEqual(cli._unmatched_patterns(["**/*.json"], ["out.json"]), [])
        self.assertEqual(cli._unmatched_patterns(["results/**/*.json"], ["results/out.json"]), [])

    def test_a_nested_member_is_not_answered_by_a_prefix_wildcard_pattern(self):
        """Sequence: the config asks for `**/logs/*.log` (logs anywhere) and the machine delivered
        `x/logs/run.log`.

        Effect required: the pattern *is* answered — the member is what it asked for. Only the fully
        collapsed form (`logs/*.log`) is tried, and that one needs the member to start with `logs/`,
        so a nested member under a prefix is reported as matching nothing. A clean pull then reads as
        having lost the logs.

        (The delivered member is the same file tar matched: GNU tar's wildcards cross `/`, so
        `**/logs/*.log` did select `x/logs/run.log` remotely. This is the under-claim direction.)
        """
        result = cli._unmatched_patterns(["**/logs/*.log"], ["x/logs/run.log"])

        self.assertEqual(result, [],
                         "a member that matches the pattern (logs anywhere) was reported as "
                         "matching nothing: only the fully collapsed form is tried, so a pattern "
                         "with a prefix before `**/` cannot answer a nested member")

    def test_a_star_crossing_a_directory_is_a_deliberate_boundary_not_an_over_claim(self):
        """The closest thing to an over-claim: `results/*.json` is answered by
        `results/nested/out.json`, because `fnmatch`'s `*` crosses `/`.

        This is recorded as a *boundary*, not a finding: tar's own wildcards cross `/` too, so that
        member is the file the remote tar selected for this pattern. Naming it as unmatched would
        flag a pull that really did deliver what was asked for.
        """
        self.assertEqual(cli._unmatched_patterns(["results/*.json"], ["results/nested/out.json"]), [])


class AOneSidedMissStaysAFinding(PullCase):
    def test_members_arriving_while_one_pattern_missed_stays_a_named_finding(self):
        """Sequence: two patterns configured, the archive holds a member for the first only, and tar
        exits 2 because it could not find the second.

        Effect required: the pull **succeeds**, the delivered member is on disk, and the missed
        pattern is named. The empty-archive failure is one branch away and must not swallow this
        class — a one-sided miss is a finding, not a lost result.
        """
        code, document = self.pull(["results/*.json", "logs/*.log"],
                                   tar_members={"results/out.json": b'{"ok": true}'}, tar_exit=2)

        self.assertEqual(code, 0, f"a delivered member was reported as a failed pull: {document}")
        self.assertTrue(document.get("pulled"), document)
        self.assertEqual(document.get("unmatched_patterns"), ["logs/*.log"], document)
        self.assertEqual(document.get("files"), ["results/out.json"], document)
        self.assertEqual(document.get("tar_exit_code"), 2, document)

    def test_no_pattern_missed_means_no_finding(self):
        """Control: a clean archive reports no unmatched patterns, so the finding above is about the
        miss and not about the field always being populated."""
        code, document = self.pull(["results/*.json"],
                                   tar_members={"results/out.json": b"x"}, tar_exit=0)

        self.assertEqual(code, 0, document)
        self.assertEqual(document.get("unmatched_patterns"), [], document)


class DownNamesWhatItIsAboutToLose(PullCase):
    def down(self) -> tuple[int, str]:
        self.cfg.artefacts.pull = ["results/*.json", "logs/*.log"]
        child = [sys.executable, "-c", WRITER, tar_hex({}), "2"]   # an empty archive, tar exit 2
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(cli, "_ssh_argv", lambda cfg, pod, provider=None: (child, "x")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.cmd_down(cli_args("down"))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def test_the_sentence_names_the_patterns_the_failed_pull_could_not_deliver(self):
        """Sequence: `down` on a live machine whose pull fails outright (the remote tar archived
        nothing), two patterns configured.

        Effect required: the machine is stopped, the row is closed, the exit is non-zero, and the
        sentence names what could not be delivered. Asserted against the same list a standalone
        `pull` reports, so the sentence cannot claim more than the pull found: with nothing
        delivered, every configured pattern is genuinely not in hand.

        Boundary worth stating: a pull that delivered *anything* returns 0, so this sentence only
        ever fires when nothing arrived. "Paths that exist on the machine" cannot be narrower than
        the patterns without a remote listing, which `down` does not do.
        """
        code, out = self.down()

        self.assertIn(("pod-A", "stop"), self.provider.actions, f"the machine was left billing.\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"the row was left open.\n{out}")
        self.assertNotEqual(code, 0, f"a lost pull exited 0.\n{out}")
        self.assertIn("will be lost", out, f"the sentence was not printed.\n{out}")
        for pattern in ("results/*.json", "logs/*.log"):
            self.assertIn(pattern, out, f"{pattern} is not named as not pulled.\n{out}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
