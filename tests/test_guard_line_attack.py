"""Attack the guard line's truth: the missing-guard sentence must follow the **machine**, not the
guard, and it must never claim an account is quiet when it is not.

Revision under attack: an earlier revision. The change makes a stopped machine read "and it
has stopped (EXITED), so nothing is billing" instead of "it will bill until someone stops it", and
moves `doctor`'s verdict line after the account listing. The aim is the edges the fix's own cases do
not take: a machine in a non-terminal, non-billing-looking status (PROVISIONING, a failed state), a
`doctor` whose listing failed, and a `doctor` with a live machine beside one this root does not own.
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
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class GuardLineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.artefacts.pull = []
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        self.provider = FakeProvider()
        self.provider.add("pod-A", uptime_seconds=600, rate_usd_per_hour=2.0)

    def run_cli(self, func, *argv) -> tuple[int, str, dict]:
        args = cli_args(*argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(args)
        text = out.getvalue().strip()
        return code, err.getvalue(), json.loads(text) if text else {}

    def status(self) -> tuple[int, str, dict]:
        return self.run_cli(cli.cmd_status, "status", "--json")

    def doctor(self) -> tuple[int, str, dict]:
        return self.run_cli(cli.cmd_doctor, "doctor", "--json")

    def machine(self, status: str, *, uptime: float = 600.0, ports=None) -> None:
        instance = self.provider.instances["pod-A"]
        instance.status = status
        instance.uptime_seconds = uptime
        instance.ports = ports

    def assert_warns(self, err: str) -> None:
        self.assertIn("nothing is watching this instance, so it will bill until someone stops it",
                      err, f"the missing-guard warning is absent:\n{err}")
        self.assertNotIn("so nothing is billing", err, f"a billing machine was called quiet:\n{err}")

    def assert_quiet(self, err: str) -> None:
        self.assertIn("so nothing is billing", err, f"the stopped machine is not described:\n{err}")
        self.assertNotIn("so it will bill until someone stops it", err,
                         f"a stopped machine was called billing:\n{err}")


class StatusFollowsTheMachine(GuardLineCase):
    def test_a_provisioning_machine_with_no_guard_keeps_the_warning(self) -> None:
        """PROVISIONING is not terminal: the account can still bill for it, so the warning stands
        (the fixer's judgement; pinned here as the effect)."""
        self.machine("PROVISIONING", uptime=0.0, ports=None)

        code, err, _ = self.status()

        self.assertEqual(code, 0, err)
        self.assert_warns(err)

    def test_failed_state_machine_with_no_guard_keeps_the_warning(self) -> None:
        """`ERROR` is not one of the terminal states either: the polarity ruling says treat it as
        billing until the provider says EXITED/TERMINATED."""
        self.machine("ERROR", uptime=600.0, ports={"22": "tcp"})

        code, err, _ = self.status()

        self.assertEqual(code, 0, err)
        self.assert_warns(err)

    def test_exited_machine_with_no_guard_says_it_has_stopped(self) -> None:
        self.machine("EXITED", uptime=600.0, ports=None)

        code, err, _ = self.status()

        self.assertEqual(code, 0, err)
        self.assert_quiet(err)
        self.assertIn("and it has stopped (EXITED)", err, err)

    def test_the_guard_document_keeps_its_keys(self) -> None:
        self.machine("EXITED", uptime=600.0, ports=None)

        _, _, document = self.status()

        self.assertEqual(set((document.get("guard") or {})),
                         {"heartbeat_age_minutes", "stale", "pid", "running", "ended"}, document)


class DoctorFollowsTheAccount(GuardLineCase):
    def test_a_failed_listing_keeps_the_warning(self) -> None:
        """The account was not read, so "nothing is billing on the account" is not established."""
        self.machine("EXITED", uptime=600.0, ports=None)
        with mock.patch.object(self.provider, "list_pods",
                               side_effect=ProviderError("the account listing refused")):
            code, err, document = self.doctor()

        self.assertEqual(code, 0, err)
        self.assertIn("will bill until someone stops it", err, f"the warning is absent:\n{err}")
        self.assertNotIn("nothing is billing on the account", err,
                         f"an unread account was called quiet:\n{err}")
        codes = [f.get("code") for f in document.get("findings") or []]
        self.assertIn("provider-error", codes, f"the listing failure is not reported: {codes}")

    def test_a_live_other_root_machine_keeps_the_warning(self) -> None:
        """A live pod this root does not own is still billing on the *account*; the tail must not say
        nothing is billing."""
        self.machine("EXITED", uptime=600.0, ports=None)
        tracked = self.provider.get("pod-A")
        foreign = {"id": "pod-B", "name": "rentguard-deadbeef", "status": "RUNNING",
                   "runtime": {"uptime": 600.0, "ports": {"22": "tcp"}, "gpus": [{"util": 95}]},
                   "cost": 2.0, "gpu": {"id": "Fake GPU 24GB"}}
        with mock.patch.object(self.provider, "list_pods", return_value=[tracked, foreign]):
            code, err, document = self.doctor()

        self.assertEqual(code, 0, err)
        self.assertIn("will bill until someone stops it", err, f"the warning is absent:\n{err}")
        self.assertNotIn("nothing is billing on the account", err,
                         f"a live other-root pod was ignored:\n{err}")
        self.assertIn("2 total, 1 live", err, err)
        claims = {pod.get("claim") for pod in document.get("live_instances") or []}
        self.assertEqual(claims, {cli.CLAIM_ANOTHER_ROOT}, claims)

    def test_a_quiet_account_says_nothing_is_billing(self) -> None:
        self.machine("EXITED", uptime=600.0, ports=None)

        code, err, _ = self.doctor()

        self.assertEqual(code, 0, err)
        self.assertIn("nothing is billing on the account", err, err)
        self.assertNotIn("will bill until someone stops it", err, err)


if __name__ == "__main__":
    unittest.main()
