"""The CLI branches the feature matrix does not reach: error paths, refusals, and small guards.

Each test drives the real command function in-process against the canonical fake provider (no
network, no spend) and asserts the *effect*: the exit code, the money decision, the file on disk.
These are the lines the coverage report listed as never executed; every one of them is reachable
without a network, so none of them belongs behind a `no cover` marker.

Division of labour with `test_feature_matrix.py`: that file proves every command *works*; this one
proves the paths where it must *refuse* work work too — which is where a money bug hides.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, iso, now_utc  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def cfg_for(root: Path, **budget) -> Config:
    cfg = Config(budget=Budget(cap_usd=budget.get("cap", 10.0),
                               per_run_cap_usd=budget.get("per_run", 5.0),
                               idle_minutes=budget.get("idle", 10.0)),
                 state_dir=root)
    cfg.provider = "fake"
    cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
    return cfg


class BrokenProvider(FakeProvider):
    """The fake, with the account calls that a real provider can fail on made to fail.

    It subclasses the canonical fake so it cannot drift from the provider interface, and it exists
    for one thing only: a provider that answers `list_pods` with an error is a real state (`doctor`
    must survive it, not crash), and the fake never produces it.
    """

    def list_pods(self):
        raise ProviderError("502: the account API is down")

    def volumes(self):
        raise ProviderError("502: the storage API is down")


class BranchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = cfg_for(self.root)
        self.ledger = Ledger(self.cfg.ledger_path)
        self.state = {}

    def write_state(self, state: dict) -> None:
        self.state = state
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def invoke(self, func, args, provider=None) -> tuple[int, str]:
        provider = provider if provider is not None else FakeProvider()
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except ProviderError:
                code = 1
        return code, out.getvalue()

    def open_run(self, *, instance_id: str = "pod-A", hours_ago: float = 0.5,
                 rate: float = 2.0) -> dict:
        from datetime import timedelta
        return self.ledger.open(purpose="probe", provider="fake", instance_id=instance_id,
                                gpu_id="Fake GPU 24GB", rate_usd_per_hour=rate,
                                started_at=now_utc() - timedelta(hours=hours_ago))


# ============================================================== provider construction

class ProviderConstruction(unittest.TestCase):
    def test_an_unknown_provider_name_is_a_clean_error_not_a_traceback(self):
        """Effect: the operator is told the name is wrong, in one line, and nothing is rented."""
        cfg = Config()
        cfg.provider = "vast-not-installed"
        with mock.patch.object(cli, "api_key", lambda *a, **k: "k"):
            with self.assertRaises(SystemExit) as caught:
                cli._provider(cfg)

        self.assertIn("unknown provider", str(caught.exception))

    def test_runpod_without_a_key_is_refused_before_any_request(self):
        cfg = Config()
        cfg.provider = "runpod"
        with mock.patch.object(cli, "api_key", lambda *a, **k: None):
            with self.assertRaises(SystemExit) as caught:
                cli._provider(cfg)

        self.assertIn("no API key", str(caught.exception))


# ============================================================== doctor

class DoctorBranches(BranchCase):
    def test_doctor_without_a_key_reports_it_as_an_error_and_exits_nonzero(self):
        """Effect: a keyless RunPod doctor says which finding stopped it, and exits 1, instead of
        printing a healthy-looking report about an account it never reached."""
        provider = FakeProvider()
        with mock.patch.object(cli, "api_key", lambda *a, **k: None), \
                mock.patch.object(cli, "provider_name", lambda cfg: "runpod"):
            code, out = self.invoke(cli.cmd_doctor, cli_args("doctor"), provider)

        self.assertEqual(code, 1, f"keyless doctor exited {code}.\n{out}")
        self.assertIn("no-api-key", out, f"the finding is not named.\n{out}")

    def test_doctor_survives_a_provider_that_cannot_be_built(self):
        """Effect: a provider that cannot be constructed is a finding with exit 1, not a stack
        trace. `_provider` turns a bad name or a bad key into `SystemExit`; the command has to
        catch it and report it as a finding."""
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider",
                                  side_effect=SystemExit("error: unknown provider 'nope'")), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "not-a-real-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.cmd_doctor(cli_args("doctor"))

        printed = out.getvalue()
        self.assertEqual(code, 1, f"doctor exited {code}.\n{printed}")
        self.assertIn("provider-unavailable", printed, f"the finding is not named.\n{printed}")

    def test_doctor_reports_an_account_api_that_fails_instead_of_crashing(self):
        """Sequence: the provider is fine but the account calls fail (a 502). Effect: doctor says so
        as a finding and names it, because "cannot tell" is not "nothing is billing"."""
        code, out = self.invoke(cli.cmd_doctor, cli_args("doctor"), BrokenProvider())

        lowered = out.lower()
        self.assertIn("provider-error", lowered, f"the provider failure is not a finding.\n{out}")
        self.assertIn("502", out, f"the reason is not reported.\n{out}")


# ============================================================== status

class StatusBranches(BranchCase):
    def test_a_stopped_machine_is_reported_as_not_billing(self):
        """Effect: no drift warning and no "arm the guard" line for a machine that cannot bill."""
        self.write_state({"instance_id": "pod-A"})
        fake = FakeProvider()
        fake.add("pod-A", status="EXITED", uptime_seconds=600)

        code, out = self.invoke(cli.cmd_status, cli_args("status"), fake)

        self.assertEqual(code, 0, f"status exited {code}.\n{out}")
        self.assertIn("nothing is billing", out.lower(), f"status did not say it.\n{out}")

    def test_a_machine_with_no_ledger_row_is_named_as_such(self):
        """Effect: status says the money has nothing recording it, and names `adopt`."""
        self.write_state({"instance_id": "pod-A"})
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=3600, rate_usd_per_hour=2.0)

        code, out = self.invoke(cli.cmd_status, cli_args("status"), fake)

        self.assertIn("no row", out.lower(), f"status did not report the missing row.\n{out}")
        self.assertIn("adopt", out, f"the remedy is not offered.\n{out}")

    def test_a_fresh_heartbeat_is_reported_as_a_live_guard(self):
        """Effect: the operator is told a supervisor is watching, with its age."""
        self.write_state({"instance_id": "pod-A"})
        self.open_run()
        self.cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.heartbeat_path.write_text(json.dumps({"at": iso(now_utc())}), encoding="utf-8")
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=3600)

        code, out = self.invoke(cli.cmd_status, cli_args("status"), fake)

        self.assertIn("alive", out.lower(), f"a fresh heartbeat was not called alive.\n{out}")


# ============================================================== up

class UpBranches(BranchCase):
    def test_cap_flags_override_the_config_for_this_run(self):
        """Effect: `--cap` and `--per-run-cap` change the numbers the command then enforces."""
        fake = FakeProvider()
        code, out = self.invoke(cli.cmd_up,
                                cli_args("up", "--dry-run", "--cap", "3", "--per-run-cap", "1"),
                                fake)

        self.assertEqual(self.cfg.budget.cap_usd, 3.0, f"--cap was ignored.\n{out}")
        self.assertEqual(self.cfg.budget.per_run_cap_usd, 1.0, f"--per-run-cap was ignored.\n{out}")

    def test_up_refuses_when_an_invisible_open_run_is_past_the_cap(self):
        """Sequence: an open row against an instance the provider does not show, 100 h old at $2/h
        ($200) and a $10 cap. Effect: refuse, rent nothing.

        The refusal comes from the pre-rent cap check, which already counts an open run's accrued
        cost. That makes the later "cap-reached-open-run" branch in `cmd_up` unreachable: its
        condition (hidden >= cap) implies the first one. Reported as dead code, not tested.
        """
        self.open_run(hours_ago=100.0, instance_id="vanished")
        fake = FakeProvider()

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "second"), fake)

        self.assertEqual(code, 2, f"up exited {code} instead of refusing.\n{out}")
        self.assertEqual(fake.created, [], f"up rented anyway.\n{out}")
        self.assertIn("cap", out.lower(), f"the reason is not named.\n{out}")

    def test_up_resumes_an_exited_instance_and_records_it(self):
        """Sequence: the state names an EXITED pod. Effect: it is started, waited for, and given a
        ledger row backdated from its own uptime."""
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        fake = FakeProvider()
        fake.add("pod-A", status="EXITED", uptime_seconds=600, rate_usd_per_hour=2.0)

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "resume"), fake)

        self.assertEqual(code, 0, f"up exited {code}.\n{out}")
        self.assertIn(("pod-A", "start"), fake.actions, f"the pod was not started.\n{out}")
        run = self.ledger.open_run()
        self.assertIsNotNone(run, f"no row for the resumed machine.\n{out}")
        self.assertLess(abs((now_utc() - __import__("datetime").datetime.fromisoformat(
            run["started_at"])).total_seconds() - 600), 120,
            f"the row was not backdated from uptime: {run['started_at']}")

    def test_dry_run_resume_starts_nothing(self):
        """Effect: `--dry-run` on a resumable machine touches the provider in no way."""
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        fake = FakeProvider()
        fake.add("pod-A", status="EXITED", uptime_seconds=600)

        code, out = self.invoke(cli.cmd_up,
                                cli_args("up", "--purpose", "resume", "--dry-run"), fake)

        self.assertEqual(code, 0, f"dry run exited {code}.\n{out}")
        self.assertEqual(fake.actions, [], f"a dry run started the machine: {fake.actions}")
        self.assertIn("would resume", out, f"the dry run did not say what it would do.\n{out}")

    def test_up_reports_every_candidate_refused_at_create(self):
        """Sequence: the catalogue offers a GPU that `create` refuses (the M2 shape, and nothing
        else to try). Effect: exit 3, nothing recorded, and the refusal is reported per attempt."""
        fake = FakeProvider()
        fake.unavailable_gpus = {"Fake GPU 24GB"}
        fake.offer_unavailable = True

        code, out = self.invoke(cli.cmd_up, cli_args("up", "--purpose", "refused"), fake)

        self.assertEqual(code, 3, f"up exited {code}.\n{out}")
        self.assertIn("refused", out.lower(), f"the refusal is not reported.\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"a refused rental opened a row.\n{out}")


# ============================================================== down

class DownBranches(BranchCase):
    def test_down_reports_a_machine_it_could_not_stop(self):
        """Sequence: the stop call fails (a provider 500). Effect: the operator is told the machine
        may still be billing, and the row is still closed at its real cost.

        `cmd_watch` wraps this call in a `try` (cli.py:880); `cmd_down` does not (cli.py:1012), and
        the ledger close comes after it — so an exception here leaves the spend hidden in an open
        row while the machine keeps billing.
        """
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.open_run(hours_ago=1.0)
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=3600, rate_usd_per_hour=2.0)
        fake.fail_actions = {"stop"}

        code, out = self.invoke(cli.cmd_down, cli_args("down"), fake)

        self.assertIsNone(self.ledger.open_run(),
                          "a refused stop escaped `down` before the ledger row was closed: the "
                          "machine is (probably) still billing and its cost is not in the ledger."
                          f"\noutput:\n{out}")
        self.assertIn("could not stop", out.lower(), f"the failed stop is not reported.\n{out}")

    def test_down_says_a_volume_survives_the_stop(self):
        """Effect: the difference between a container disk and a network volume is stated, because
        it is the difference between losing the results and not.

        The provider contract puts the volume in `volumeId` (provider.py); `cmd_down` reads
        `pod["volume"]`, so this line cannot fire for any provider that follows the contract.
        """
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.open_run(hours_ago=0.5)
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=1800, rate_usd_per_hour=2.0, volume=True)

        code, out = self.invoke(cli.cmd_down, cli_args("down"), fake)

        self.assertIn("volume survives", out.lower(),
                      "a machine with a network volume was not told the volume survives the stop: "
                      "cmd_down reads pod['volume'], the contract reports volumeId."
                      f"\noutput:\n{out}")

    def test_down_without_an_instance_closes_the_row_and_says_which(self):
        """Sequence: the state records nothing but a row is open (the instance vanished). Effect:
        the row is closed at its real cost and named, so the spend stops hiding."""
        run = self.open_run(hours_ago=3.0, instance_id="vanished")

        code, out = self.invoke(cli.cmd_down, cli_args("down"), FakeProvider())

        self.assertEqual(code, 0, f"down exited {code}.\n{out}")
        self.assertIn(run["run_id"], out, f"the closed row is not named.\n{out}")
        self.assertAlmostEqual(self.ledger.total_usd(), 6.0, delta=0.1,
                               msg=f"the vanished run was recorded as ${self.ledger.total_usd()}")


# ============================================================== adopt, run, pull

class AdoptAndRunBranches(BranchCase):
    def test_adopt_refuses_while_a_run_is_already_open(self):
        """Effect: no second row; the one to close is named."""
        run = self.open_run()
        fake = FakeProvider()
        fake.add("pod-B", uptime_seconds=600)

        code, out = self.invoke(cli.cmd_adopt,
                                cli_args("adopt", "pod-B", "--purpose", "x"), fake)

        self.assertEqual(code, 1, f"adopt exited {code}.\n{out}")
        self.assertIn("already open", out.lower(), f"the reason is not given.\n{out}")
        self.assertEqual(len(self.ledger.entries()), 1, "adopt opened a second row")

    def test_run_with_no_command_is_refused(self):
        """Effect: `run` with nothing to run exits 1 and does not open an ssh connection."""
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.open_run()
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=600)

        with mock.patch("rentguard.cli.subprocess.call") as ssh:
            code, out = self.invoke(cli.cmd_run, cli_args("run", "--"), fake)

        self.assertEqual(code, 1, f"run exited {code}.\n{out}")
        self.assertFalse(ssh.called, f"an ssh call was attempted: {ssh.call_args}")
        self.assertIn("nothing to run", out, f"the reason is not given.\n{out}")

    def test_run_uses_ssh_when_the_provider_has_no_in_memory_runner(self):
        """Effect: a provider without a `command_runner` reaches the machine with ssh, and `run`
        returns the command's exit code."""
        self.write_state({"instance_id": "pod-A", "provider": "fake"})
        self.open_run()
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=600)
        seen: list[list[str]] = []

        def fake_call(argv, *a, **k):
            seen.append(list(argv))
            return 7

        with mock.patch("rentguard.cli.subprocess.call", fake_call):
            code, out = self.invoke(cli.cmd_run, cli_args("run", "--", "python", "train.py"), fake)

        self.assertEqual(code, 7, f"the command's exit code was not returned.\n{out}")
        self.assertTrue(seen and seen[0] and seen[0][0].endswith("ssh"),
                        f"no ssh command line was built: {seen}")

    def test_print_access_says_so_when_the_machine_has_no_ssh(self):
        """Effect: a machine with no reachable endpoint is reported as such instead of raising."""
        fake = FakeProvider()
        fake.add("pod-A", uptime_seconds=600)
        pod = fake.get("pod-A") or {}
        with mock.patch.object(fake, "ssh_argv",
                               side_effect=ProviderError("no ssh endpoint yet")):
            code, out = self.invoke(lambda args: cli._print_access(self.cfg, fake, pod),
                                    cli_args("status"), fake)

        self.assertIn("not available", out.lower(),
                      f"the missing access was not reported.\n{out}")


# ============================================================== the small guards

class SmallGuards(BranchCase):
    def test_open_if_needed_reuses_the_row_for_the_same_machine(self):
        """Effect: calling it twice does not open a second row — the one open row is returned."""
        run = self.open_run(instance_id="pod-A")
        fake = FakeProvider()
        pod = {"id": "pod-A", "status": "RUNNING", "cost": 2.0,
               "runtime": {"uptime": 600}, "gpu": {"id": "Fake GPU 24GB"}}

        again = cli._open_if_needed(self.cfg, self.ledger, pod, "x", fake)

        self.assertEqual(again["run_id"], run["run_id"], "a second row was opened for one machine")
        self.assertEqual(len(self.ledger.entries()), 1)

    def test_copy_matching_refuses_a_member_that_escapes_the_destination(self):
        """Sequence: a rented machine returns a file named `../escape.txt` (untrusted input from a
        machine we chose, but a machine nonetheless). Effect: nothing is written outside `dest`,
        and the attempt raises rather than silently correcting the path."""
        dest = self.root / "artefacts"
        dest.mkdir(parents=True, exist_ok=True)
        outside = (dest / ".." / "escape.txt").resolve()

        with self.assertRaises(ValueError):
            cli._copy_matching({"../escape.txt": b"x"}, dest)

        self.assertFalse(outside.exists(), f"a file escaped into {outside}")

    def test_copy_matching_writes_the_files_that_stay_inside(self):
        """Control for the guard above: ordinary nested paths are written."""
        dest = self.root / "artefacts"

        written = cli._copy_matching({"results/out.json": b'{"ok": true}'}, dest)

        self.assertEqual(written, ["results/out.json"])
        self.assertEqual((dest / "results" / "out.json").read_bytes(), b'{"ok": true}')

    def test_temp_tar_falls_back_and_reports_when_nothing_is_writable(self):
        """Sequence: the preferred directory cannot be created and the system temp is not usable
        either. Effect: an OSError that names the underlying failure, not a crash halfway through a
        pull with a half-written archive."""
        bad = self.root / "not-a-directory"
        bad.write_text("x", encoding="utf-8")

        with mock.patch("tempfile.gettempdir", lambda: str(bad)):
            with self.assertRaises(OSError) as caught:
                with cli._temp_tar(bad) as (handle, archive):
                    handle.write(b"x")

        self.assertTrue(str(caught.exception), "the OSError carries no reason")

    def test_temp_tar_uses_the_directory_it_is_given(self):
        """Control: the caller's writable directory is used, and removed afterwards."""
        directory = self.root / "state"
        with cli._temp_tar(directory) as (handle, archive):
            handle.write(b"x")
            self.assertTrue(archive.exists(), "the archive file was not created")

        self.assertEqual(list(directory.glob(".rentguard-pull-*")), [],
                         "the temporary pull directory was left behind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
