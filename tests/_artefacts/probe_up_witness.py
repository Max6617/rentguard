"""Probe: why `up` says the accrual is unknown while `provider.get` returns a clock.

Mimics `ACorruptStartWithThePodClock` exactly, then prints what `up` sees: the open row, what the
fake returns for `get` and `list_pods`, and the document `up --dry-run` emits.

Run against a chosen checkout:
    $env:PYTHONPATH = "<worktree parent>"; python tests/_artefacts/probe_up_witness.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent))

import rentguard  # noqa: E402
from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402
import unittest  # noqa: E402

print(f"package          : {rentguard.__file__}")

case = unittest.TestCase("run")
root = scratch_dir(case)
cfg = Config(budget=Budget(cap_usd=15.0, per_run_cap_usd=10.0), state_dir=root / ".rentguard")
cfg.provider = "fake"
ledger = Ledger(cfg.ledger_path)
fake = FakeProvider()

now = now_utc()
ledger.open(purpose="closed", provider="fake", instance_id="pod-old", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now - timedelta(hours=2))
ledger.close(note="down", stopped_at=now - timedelta(hours=1))
ledger.open(purpose="open", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now - timedelta(hours=1))
rows = ledger.entries()
rows[-1]["started_at"] = "the-ledger-was-truncated"
ledger._rewrite(rows)
fake.add("pod-A", status="EXITED", uptime_seconds=3600, rate_usd_per_hour=2.0)

print(f"open row         : {json.dumps(ledger.open_run(), sort_keys=True)}")
print(f"fake.get(pod-A)  : {json.dumps(fake.get('pod-A'), sort_keys=True)}")
print(f"fake.list_pods() : {json.dumps(fake.list_pods(), sort_keys=True)[:200]}")
print(f"state.json exists: {cfg.state_path.exists()}")

out, err = io.StringIO(), io.StringIO()
with mock.patch.object(cli, "_config", lambda: cfg), \
        mock.patch.object(cli, "_provider", lambda c: fake), \
        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        code = cli.cmd_up(cli.build_parser().parse_args(
            ["up", "--purpose", "probe", "--dry-run", "--json"]))
    except SystemExit as exc:
        code = exc.code

stdout = out.getvalue().strip()
document = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
print(f"up exit          : {code}")
print(f"up document      : {json.dumps(document, sort_keys=True)}")
