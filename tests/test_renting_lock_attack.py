"""Attack the renting lock: a config that says ``allow_renting = false`` must not spend, and must not
lock the operator out of stopping or inspecting a machine.

Revision under attack: ``78fd49c`` (code ``982bcb9``). The lock is read in ``load()``, and ``up`` /
``adopt`` refuse before a provider is constructed or a row is opened. My aim is the boundaries the
fix's own tests do not take: ``--force``, ``--dry-run``, ``adopt --uptime-hours``, the real CLI's exit
code and file effects, the MCP ``dispatch`` path, and ``down`` on a live machine with the lock on.

The real CLI is run as a subprocess against a scratch root (so ``load()`` and ``main`` are the real
ones), and the in-process tests patch only ``_config``/``_provider``. No rental is possible: the
provider is the fake, and every refusal happens before one is constructed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli, mcp_server  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

CONFIG = """\
provider = "fake"
state_dir = ".rentguard"
allow_renting = false
renting_disabled_reason = "the workspace root is not a project root"

[budget]
cap_usd = 5.0
per_run_cap_usd = 0.1
max_hours_per_run = 0.5

[artefacts]
pull = []
"""


class LockedRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.config_path = self.root / "rentguard.toml"
        self.write_config(CONFIG)
        self.state_dir = self.root / ".rentguard"

    def write_config(self, text: str) -> None:
        self.config_path.write_text(text, encoding="utf-8")

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("DSH_SECRET_RUNPOD_API_KEY", "RUNPOD_API_KEY")}
        env["RENTGUARD_ROOT"] = str(self.root)
        env["RENTGUARD_PROVIDER"] = "fake"
        env["PYTHONPATH"] = str(WS)
        return env

    def cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "rentguard.cli", *argv],
                              cwd=self.root, env=self.env(), capture_output=True, text=True,
                              timeout=120)

    def document(self, result: subprocess.CompletedProcess) -> dict:
        return json.loads(result.stdout)

    def assert_nothing_was_created(self) -> None:
        self.assertFalse((self.state_dir / "runs.jsonl").exists(),
                         f"a ledger row was opened:\n{self.state_dir}")
        self.assertFalse((self.state_dir / "fake-state.json").exists(),
                         f"a machine was created:\n{self.state_dir}")
        self.assertFalse((self.state_dir / "state.json").exists(),
                         f"the instance state was written:\n{self.state_dir}")


class TheRealCliRefuses(LockedRoot):
    def test_up_refuses_even_with_force(self) -> None:
        result = self.cli("up", "--json", "--force", "--purpose", "attack")

        self.assertEqual(result.returncode, 2, result.stderr)
        document = self.document(result)
        self.assertEqual(document.get("action"), "refused", document)
        self.assertEqual(document.get("reason"), "renting-disabled", document)
        self.assertEqual(document.get("config"), str(self.config_path), document)
        self.assertIn("the workspace root is not a project root", document.get("detail") or "", document)
        self.assertIn("--force does not override", result.stderr)
        self.assert_nothing_was_created()

    def test_adopt_with_an_uptime_estimate_refuses_before_the_row(self) -> None:
        result = self.cli("adopt", "pod-A", "--uptime-hours", "2", "--json")

        self.assertEqual(result.returncode, 2, result.stderr)
        document = self.document(result)
        self.assertEqual(document.get("action"), "refused", document)
        self.assertEqual(document.get("reason"), "renting-disabled", document)
        self.assertEqual(document.get("instance_id"), "pod-A", document)
        self.assert_nothing_was_created()

    def test_a_dry_run_refuses_too(self) -> None:
        """`up --dry-run` spends nothing, and the gate still refuses it: the lock is on `up`, not on
        spending. Recorded as the effect, not judged."""
        result = self.cli("up", "--json", "--dry-run")

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.document(result).get("reason"), "renting-disabled")
        self.assert_nothing_was_created()

    def test_the_default_is_to_rent_the_control(self) -> None:
        """No lock key: `up --dry-run` is not refused (it still rents nothing)."""
        self.write_config(CONFIG.replace('allow_renting = false\n', "")
                                .replace('renting_disabled_reason = "the workspace root is not a '
                                         'project root"\n', ""))

        result = self.cli("up", "--json", "--dry-run")

        self.assertNotEqual(result.returncode, 2, f"the default refused to rent:\n{result.stderr}")
        text = result.stdout + result.stderr
        self.assertNotIn("renting-disabled", text)
        self.assert_nothing_was_created()

    def test_a_non_boolean_is_refused_at_load(self) -> None:
        self.write_config(CONFIG.replace("allow_renting = false", 'allow_renting = "no"'))

        result = self.cli("up", "--json")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("allow_renting", result.stderr)
        self.assert_nothing_was_created()


class EverythingElseStillWorks(LockedRoot):
    def test_doctor_reports_the_lock_as_a_warning(self) -> None:
        result = self.cli("doctor", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.document(result)
        codes = [(finding.get("code"), finding.get("severity"))
                 for finding in document.get("findings") or []]
        self.assertIn(("renting-disabled", "warn"), codes,
                      f"the lock is not reported as a warn: {codes}")
        self.assertIn("renting        : REFUSED", result.stderr)

    def test_status_and_ledger_run_without_a_refusal(self) -> None:
        for argv in (("status", "--json"), ("ledger", "--json")):
            with self.subTest(argv=argv[0]):
                result = self.cli(*argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("renting-disabled", result.stderr)

    def test_down_still_stops_a_live_machine_with_the_lock_on(self) -> None:
        """The one thing the lock must never gate: a machine that is billing must stay stoppable."""
        cfg = Config(budget=Budget(cap_usd=5.0, per_run_cap_usd=0.1, stop_confirm_s=0.0),
                     state_dir=self.state_dir)
        cfg.provider = "fake"
        cfg.artefacts.pull = []
        cfg.allow_renting = False
        cfg.renting_disabled_reason = "locked"
        cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, rate_usd_per_hour=2.0)
        row = Ledger(cfg.ledger_path).open(
            purpose="live", provider="fake", instance_id="pod-A", gpu_id="g",
            rate_usd_per_hour=2.0,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1))
        out, err = io.StringIO(), io.StringIO()
        args = cli.build_parser().parse_args(["down", "--json"])
        with mock.patch.object(cli, "_config", lambda: cfg), \
                mock.patch.object(cli, "_provider", lambda config: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_down(args)

        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(provider.instances["pod-A"].status, "EXITED",
                         "a live machine was not stopped because the config refuses to rent")
        self.assertIsNone(Ledger(cfg.ledger_path).open_run(), "the row was left open")
        self.assertGreater(Ledger(cfg.ledger_path).total_usd(), 0.0, err.getvalue())
        self.assertIsNotNone(row.get("started_at"), "the fixture row is malformed")


class TheMcpDoorIsClosed(LockedRoot):
    def test_dispatch_start_refuses_before_a_provider(self) -> None:
        cfg = self.locked_config()
        provider = mock.Mock(side_effect=AssertionError("a provider was constructed"))
        with mock.patch.object(cli, "_config", lambda: cfg), \
                mock.patch.object(cli, "_provider", provider):
            text, structured, is_error = mcp_server.dispatch("rentguard_start", {"purpose": "x"})

        self.assertTrue(is_error, text)
        self.assertEqual((structured or {}).get("action"), "refused", structured)
        self.assertEqual((structured or {}).get("reason"), "renting-disabled", structured)
        self.assertIn("allow_renting = false", text)
        provider.assert_not_called()
        self.assert_nothing_was_created()

    def test_dispatch_adopt_refuses_before_a_provider(self) -> None:
        cfg = self.locked_config()
        provider = mock.Mock(side_effect=AssertionError("a provider was constructed"))
        with mock.patch.object(cli, "_config", lambda: cfg), \
                mock.patch.object(cli, "_provider", provider):
            text, structured, is_error = mcp_server.dispatch(
                "rentguard_adopt", {"instance_id": "pod-A", "uptime_hours": 2})

        self.assertTrue(is_error, text)
        self.assertEqual((structured or {}).get("reason"), "renting-disabled", structured)
        provider.assert_not_called()
        self.assert_nothing_was_created()

    def locked_config(self) -> Config:
        cfg = Config(budget=Budget(cap_usd=5.0, per_run_cap_usd=0.1), state_dir=self.state_dir)
        cfg.provider = "fake"
        cfg.artefacts.pull = []
        cfg.allow_renting = False
        cfg.renting_disabled_reason = "locked by the fixture"
        cfg.source = self.config_path
        return cfg


if __name__ == "__main__":
    unittest.main()
