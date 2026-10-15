"""Probe: what the two numbers in `ACorruptStart` are actually made of.

The failing assertion is `printed == enforced`. This prints both sides and the open row before and
after `doctor`, so the witness question is answered from numbers rather than from inference.

Run:  python tests/_artefacts/probe_corrupt_start_numbers.py
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
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402
import unittest  # noqa: E402


def cli_args(*argv):
    return cli.build_parser().parse_args(list(argv))


case = unittest.TestCase("run")
root = scratch_dir(case)
cfg = Config(budget=Budget(cap_usd=15.0, per_run_cap_usd=10.0), state_dir=root / ".rentguard")
cfg.provider = "fake"
ledger = Ledger(cfg.ledger_path)
fake = FakeProvider()                       # note: no pod added, exactly as the fixture has it

closed = ledger.open(purpose="closed", provider="fake", instance_id="pod-old",
                     gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                     started_at=now_utc() - timedelta(hours=2))
ledger.close(note="down", stopped_at=now_utc() - timedelta(hours=1))
open_run = ledger.open(purpose="open", provider="fake", instance_id="pod-A",
                       gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                       started_at=now_utc() - timedelta(hours=1))
rows = ledger.entries()
rows[-1]["started_at"] = "the-ledger-was-truncated"
ledger._rewrite(rows)

print(f"after the corruption : total_usd={ledger.total_usd()} live_usd={ledger.live_usd()}")
print(f"open row             : {json.dumps(ledger.open_run(), sort_keys=True)}")

out, err = io.StringIO(), io.StringIO()
with mock.patch.object(cli, "_config", lambda: cfg), \
        mock.patch.object(cli, "_provider", lambda c: fake), \
        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        code = cli.cmd_doctor(cli_args("doctor", "--json"))
    except SystemExit as exc:
        code = exc.code

stdout = out.getvalue().strip()
document = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
print(f"doctor exit          : {code}")
print(f"spend                : {json.dumps(document.get('spend'), sort_keys=True)}")
print(f"findings             : "
      f"{json.dumps([f.get('code') for f in document.get('findings') or []])}")
print(f"after doctor         : total_usd={ledger.total_usd()} live_usd={ledger.live_usd()}")
print(f"open row after       : {json.dumps(ledger.open_run(), sort_keys=True)}")
