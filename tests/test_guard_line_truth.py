"""The missing-guard line must be true of a machine that has **stopped** (Gilbert's ruling, 2026-10-20).

Measured in the window root after the one-cell window closed: `status` printed

    guard          : NO GUARD — nothing is watching this instance, so it will bill until someone stops it

for an instance the same output printed as `machine : stopped (EXITED) — nothing is billing for it now`.
Two adjacent lines contradicting each other, on the read path people use **after** a stop. The sentence
was written when "no guard" always meant a live machine; a stop is the normal end of a rental, and the
line has to follow the machine's own state.

So the tail is conditional: a **terminal** instance (EXITED/TERMINATED) says nothing is billing; a live
one — and any state that cannot be established — keeps the warning, because the safe direction for an
unknown machine is "it may be billing".

The structured `guard` document is deliberately **unchanged**: `status` already carries
`instance.status`, so no schema change is made for a sentence (the same step-back as the heartbeat-set
keys, 2026-10-16).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

WILL_BILL = "will bill until someone stops it"


class GuardLineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text("{}", encoding="utf-8")
        self.fake = FakeProvider()

    def invoke(self, func, argv: list[str]) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()

    def machine(self, *, status: str) -> str:
        """One instance on the fake account, in the state the case is about, recorded in state.json."""
        instance = self.fake.create(gpu_id="Fake GPU 24GB", image="img", container_disk_gb=20,
                                    cloud="COMMUNITY", ports=["22/tcp"], name="guard-line")
        instance_id = instance["id"] if isinstance(instance, dict) else instance.id
        self.fake.action(instance_id, "start" if status == "RUNNING" else "stop")
        self.cfg.state_path.write_text(json.dumps({"instance_id": instance_id}), encoding="utf-8")
        return instance_id

    def finished_guard(self) -> None:
        """Write a heartbeat and mark it finished: the reading a clean exit leaves behind."""
        snap = guard.snapshot_fallback(self.cfg, Ledger(self.cfg.ledger_path))
        guard.write_heartbeat(self.cfg.heartbeat_path, snap, note="fixture")
        self.assertTrue(guard.end_heartbeat(self.cfg.heartbeat_path),
                        "the fixture could not mark its own heartbeat finished")

    @staticmethod
    def status_guard_line(text: str) -> str:
        lines = [line for line in text.splitlines() if line.startswith("guard ")]
        assert len(lines) == 1, f"expected exactly one `guard` line in status:\n{text}"
        return lines[0]

    @staticmethod
    def doctor_guard_line(text: str) -> str:
        lines = [line for line in text.splitlines() if line.startswith("guard heartbeat")]
        assert len(lines) == 1, f"expected exactly one guard-heartbeat line in doctor:\n{text}"
        return lines[0]


class StatusFollowsTheMachine(GuardLineCase):
    def test_a_stopped_machine_with_no_guard_does_not_say_it_will_bill(self) -> None:
        self.machine(status="EXITED")

        code, out, err = self.invoke(cli.cmd_status, ["status"])
        text = out + err

        line = self.status_guard_line(text)
        self.assertEqual(code, 0, text)
        self.assertNotIn(WILL_BILL, line, f"a stopped machine still reads as billing:\n{text}")
        self.assertIn("nothing is billing", line, f"the line does not say what is true:\n{text}")
        self.assertIn("EXITED", line, f"the line does not name the state it is reading:\n{text}")

    def test_a_live_machine_with_no_guard_still_says_it_will_bill(self) -> None:
        """The control: the warning exists for the machine that is actually spending."""
        self.machine(status="RUNNING")

        code, out, err = self.invoke(cli.cmd_status, ["status"])
        text = out + err

        line = self.status_guard_line(text)
        self.assertEqual(code, 0, text)
        self.assertIn(WILL_BILL, line, f"a billing machine lost its warning:\n{text}")

    def test_a_stopped_machine_with_a_finished_guard_does_not_say_it_will_bill(self) -> None:
        self.machine(status="EXITED")
        self.finished_guard()

        code, out, err = self.invoke(cli.cmd_status, ["status"])
        text = out + err

        line = self.status_guard_line(text)
        self.assertEqual(code, 0, text)
        self.assertIn("finished", line, f"the finished guard is not reported:\n{text}")
        self.assertNotIn(WILL_BILL, line, f"a stopped machine still reads as billing:\n{text}")
        self.assertIn("nothing is billing", line, text)

    def test_a_live_machine_with_a_finished_guard_still_says_it_will_bill(self) -> None:
        self.machine(status="RUNNING")
        self.finished_guard()

        code, out, err = self.invoke(cli.cmd_status, ["status"])
        text = out + err

        line = self.status_guard_line(text)
        self.assertEqual(code, 0, text)
        self.assertIn(WILL_BILL, line, f"a billing machine lost its warning:\n{text}")

    def test_the_guard_document_is_unchanged_by_the_sentence(self) -> None:
        """No schema change for a sentence: the document is the same as before, keys and all."""
        self.machine(status="EXITED")

        code, out, err = self.invoke(cli.cmd_status, ["status", "--json"])

        self.assertEqual(code, 0, out + err)
        document = json.loads(out)
        self.assertEqual(set(document["guard"]),
                         {"heartbeat_age_minutes", "stale", "pid", "running", "ended"}, document["guard"])
        self.assertEqual(document["instance"]["status"], "EXITED", document)


class DoctorFollowsTheAccount(GuardLineCase):
    def test_a_stopped_account_with_no_guard_does_not_say_it_will_bill(self) -> None:
        self.machine(status="EXITED")

        code, out, err = self.invoke(cli.cmd_doctor, ["doctor"])
        text = out + err

        line = self.doctor_guard_line(text)
        self.assertIn(code, (0, 1), text)
        self.assertNotIn(WILL_BILL, line, f"a stopped account still reads as billing:\n{text}")
        self.assertIn("nothing is billing", line, text)

    def test_a_live_account_with_no_guard_still_says_it_will_bill(self) -> None:
        self.machine(status="RUNNING")

        code, out, err = self.invoke(cli.cmd_doctor, ["doctor"])
        text = out + err

        line = self.doctor_guard_line(text)
        self.assertIn(code, (0, 1), text)
        self.assertIn(WILL_BILL, line, f"a billing account lost its warning:\n{text}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
