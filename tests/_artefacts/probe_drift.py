"""Gilbert's drift repro, offline: shows what `status` does now, and what `up` still does after it.

    python rentguard/tests/_artefacts/probe_drift.py

No network, no spend: the provider is an in-memory FakeProvider that no longer shows the machine.
This is evidence for the failure report; it changes nothing in the implementation.
"""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from datetime import timedelta
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WS))

ROOT = WS / ".assay-tmp" / "drift-probe"
shutil.rmtree(ROOT, ignore_errors=True)
(ROOT / ".rentguard").mkdir(parents=True)

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402

cfg = Config(budget=Budget(cap_usd=25.0, per_run_cap_usd=5.0), state_dir=ROOT / ".rentguard")
cfg.state_path.write_text(json.dumps({"instance_id": "drift-demo", "provider": "runpod"},
                                     indent=2) + "\n", encoding="utf-8")
ledger = Ledger(cfg.ledger_path)
row = ledger.open(purpose="drift-demo", provider="runpod", instance_id="drift-demo",
                  gpu_id="g", rate_usd_per_hour=2.0,
                  started_at=now_utc() - timedelta(hours=3))
print(f"fixture: open row {row['run_id']} for instance 'drift-demo', $2.00/h, started 3h ago "
      f"(accrued ${ledger.live_usd():.2f}); the provider shows no such instance\n")


class GoneProvider:
    """The provider no longer shows drift-demo; create() makes a replacement that does exist."""
    name = "gone"

    def __init__(self):
        self.created = []

    def get(self, instance_id):
        return None

    def list_pods(self):
        return []

    def action(self, instance_id, action):
        pass

    def create(self, **kw):
        self.created.append(kw)
        return {"id": "pod-new-1", "status": "RUNNING", "cost": 2.0,
                "gpu": {"id": "g"}, "runtime": {"uptime": 60, "ports": {"22": "tcp"},
                                                 "gpus": [{"util": 90, "memoryUtil": 60}]},
                "ssh": {"direct": {"host": "h", "port": 22, "username": "root"}}}

    def wait_running(self, instance_id, **kw):
        return self.get(instance_id)

    def choose_gpu(self, preference, cloud="SECURE"):
        return preference[0]

    def is_alive(self, instance):
        return True

    def ssh_argv(self, instance, key=None):
        return [], "root@h"


provider = GoneProvider()

with mock.patch.object(cli, "_config", lambda: cfg), \
     mock.patch.object(cli, "_provider", lambda c: provider):
    print("$ rentguard status")
    try:
        print(f"[exit {cli.main(['status'])}]")
    except BaseException:  # noqa: BLE001
        traceback.print_exc(limit=5)

    print("\n$ rentguard up --purpose second")
    try:
        print(f"[exit {cli.main(['up', '--purpose', 'second'])}]")
    except BaseException:  # noqa: BLE001
        traceback.print_exc(limit=5)

print("\ncreated by up :", [c.get('name') for c in provider.created])
print("live instances:", ["pod-new-1"] if provider.created else [])
open_row = ledger.open_run()
print("open ledger row:", None if open_row is None else
      {k: open_row.get(k) for k in ("run_id", "instance_id", "purpose")})
print("rows in ledger:", [(r.get('instance_id'), r.get('stopped_at'), r.get('estimated_usd'))
                          for r in ledger.entries()])
print(f"cap check would read: ${ledger.total_usd():.2f} of ${cfg.budget.cap_usd:.2f} "
      f"(an open row counts as zero until it is closed)")
