"""F-5: the fake provider must span processes, and a clean root must make `doctor` exit 0.

    python rentguard/tests/test_fake_span.py -v

Every `up` below is a separate `python -m rentguard.cli` process — the "fresh shell" case Gilbert
asked for. With the fake's account persisted under the run root, the second process must see the
machine the first one rented and must not rent a second one.

Child processes are started with stdout/stderr sent to the null device rather than pipes: the
confined Windows token cannot open named pipes, and these tests assert on the files, not the prose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
PY = sys.executable
TMPROOT = WS / ".assay-tmp"

TOML = """provider = "fake"

[budget]
cap_usd = 5.0
per_run_cap_usd = 2.0
max_hours_per_run = 6.0
idle_minutes = 10

[runpod]
gpu_preference = ["Fake GPU 24GB"]
"""


class FakeSpansProcesses(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMPROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        (self.root / "rentguard.toml").write_text(TOML, encoding="utf-8")
        self.env = {**os.environ, "RENTGUARD_ROOT": str(self.root),
                    "RENTGUARD_PROVIDER": "fake"}

    def cli(self, *argv: str) -> int:
        return subprocess.run([PY, "-m", "rentguard.cli", *argv], cwd=str(WS),
                              env=self.env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def fake_state_path(self) -> Path | None:
        for name in ("fake-state.json", "fake_state.json"):
            candidate = self.root / ".rentguard" / name
            if candidate.exists():
                return candidate
        return None

    def ledger_rows(self) -> list[dict]:
        path = self.root / ".rentguard" / "runs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_doctor_on_a_clean_root_exits_zero(self):
        """No state, no ledger, no drift, provider "fake": `doctor` must exit 0, not treat the
        absence of an account as a failure. (Drift exits non-zero; emptiness is healthy.)"""
        code = self.cli("doctor")

        self.assertEqual(code, 0, "doctor exited non-zero on a clean root with no drift")

    def test_two_ups_from_separate_processes_do_not_rent_twice(self):
        """Sequence: `rentguard up --purpose first` in one shell, then `up --purpose second` in a
        fresh shell. The fake's account lives in a file under the run root.

        Effect required: one machine, one open ledger row, and the second command does not create
        anything. If the fake forgets between processes, the smoke flow the README documents cannot
        be tested, and a second `up` is a second bill.
        """
        first = self.cli("up", "--purpose", "first")
        second = self.cli("up", "--purpose", "second")

        state_path = self.fake_state_path()
        self.assertIsNotNone(state_path, "the fake wrote no account file under the run root")
        instances = json.loads(state_path.read_text(encoding="utf-8")).get("instances", [])
        self.assertEqual(len(instances), 1,
                         f"the second up rented another machine: {instances}")
        self.assertEqual(first, 0, "the first up failed")
        self.assertEqual(second, 0, "the second up failed")

        open_rows = [r for r in self.ledger_rows() if not r.get("stopped_at")]
        self.assertEqual(len(open_rows), 1, f"expected one open row, got {open_rows}")
        self.assertEqual(open_rows[0].get("instance_id"), instances[0]["id"],
                         "the open row does not name the machine the fake holds")


if __name__ == "__main__":
    unittest.main(verbosity=2)
