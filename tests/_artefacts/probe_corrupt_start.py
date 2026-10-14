"""Probe: how the guard now handles a corrupt `started_at` on the open row (working tree).

Run:  python tests/_artefacts/probe_corrupt_start.py

Not a test. It prints the heartbeat the guard wrote and the lines it logged, so the mechanism is
named: priced from the provider's uptime, or the field named as unreadable.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.provider import Provider  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402
import unittest  # noqa: E402


class Stub(Provider):
    name = "stub"

    def get(self, instance_id):
        return {"id": "pod-A", "status": "RUNNING", "cost": 2.0, "gpu": {"id": "Fake GPU 24GB"},
                "runtime": {"uptime": 7200, "ports": {"22": "tcp"},
                            "gpus": [{"util": 90.0, "memoryUtil": 10.0}]}}

    def list_pods(self):
        return [self.get("pod-A")]

    def action(self, instance_id, action):
        print(f"  action: {instance_id} {action}")


case = unittest.TestCase("run")
root = scratch_dir(case)
cfg = Config(budget=Budget(cap_usd=100.0, per_run_cap_usd=50.0, idle_minutes=5.0), state_dir=root)
ledger = Ledger(cfg.ledger_path)
ledger.open(purpose="corrupt", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now_utc() - timedelta(hours=2))
rows = ledger.entries()
rows[-1]["started_at"] = "the-ledger-was-truncated"
ledger._rewrite(rows)

lines: list[str] = []
try:
    reason = guard.watch(cfg, Stub(None), ledger, instance_id="pod-A", interval=0.0,
                         max_iterations=1, log=lines.append)
    print(f"guard returned : {reason!r}")
except Exception as exc:  # noqa: BLE001
    print(f"guard RAISED   : {type(exc).__name__}: {exc}")

heartbeat = json.loads(Path(cfg.heartbeat_path).read_text(encoding="utf-8"))
print(f"heartbeat      : run_usd={heartbeat.get('run_usd')} spent_usd={heartbeat.get('spent_usd')} "
      f"uptime_hours={heartbeat.get('uptime_hours')} max_idle_minutes={heartbeat.get('max_idle_minutes')}")
print("guard lines:")
for line in lines:
    print(f"  {line}")
