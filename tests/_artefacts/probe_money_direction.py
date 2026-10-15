"""Probe: money-direction shapes the suite does not cover, at revision 7f52cc3.

Each block prints the printed figure beside the enforced figure and the machine's own bill.
Read-only with respect to the implementation; the provider is the in-memory fake.

Run from the worktree root:
    python tests/_artefacts/probe_money_direction.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from datetime import timedelta
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import run_root  # noqa: E402

BASE = run_root() / "money-direction"
BASE.mkdir(parents=True, exist_ok=True)


def fresh(name: str) -> Config:
    root = BASE / name
    if root.exists():
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return Config(budget=Budget(cap_usd=5.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                  state_dir=root / ".rentguard")


def cfg_cap(cap: float, root_name: str) -> Config:
    cfg = fresh(root_name)
    cfg.budget.cap_usd = cap
    cfg.budget.per_run_cap_usd = cap
    return cfg


def run_doctor(cfg: Config, provider: FakeProvider) -> tuple[int, str]:
    args = cli.build_parser().parse_args(["doctor"])
    out = io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", lambda c: provider), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_doctor(args)
    return code, out.getvalue()


def run_up(cfg: Config, provider: FakeProvider) -> tuple[int, str]:
    args = cli.build_parser().parse_args(["up", "--purpose", "second"])
    out = io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", lambda c: provider), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_up(args)
    return code, out.getvalue()


def rows_report(ledger: Ledger) -> str:
    parts = []
    for row in ledger.entries():
        parts.append(f"{str(row.get('run_id'))[:15]} inst={row.get('instance_id')!r} "
                     f"open={row.get('stopped_at') is None} usd={row.get('estimated_usd')} "
                     f"rate={row.get('rate_usd_per_hour')} by={row.get('closed_by')}")
    return " | ".join(parts) or "(no rows)"


def live_report(provider: FakeProvider) -> str:
    return ", ".join(f"{p.get('id')}:{p.get('status')}:up={p['runtime']['uptime']}"
                     for p in provider.list_pods())


# ---------------------------------------------------------------- P1
print("=" * 78)
print("P1  state lost + an OPEN row naming a live machine; `up` again")
print("=" * 78)
cfg = fresh("p1")
provider = FakeProvider(rate_usd_per_hour=2.0)
provider.add("pod-A", uptime_seconds=3600, status="RUNNING")       # 1 h = $2, billing
ledger = Ledger(cfg.ledger_path)
ledger.open(purpose="first", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now_utc() - timedelta(hours=2))
print(f"before : live={live_report(provider)}  rows={rows_report(ledger)}")
print(f"before : state.json exists={cfg.state_path.exists()}  "
      f"ledger total={ledger.total_usd()} accruing={ledger.live_usd()}")
code, out = run_up(cfg, provider)
print(f"after  : up exit={code}  created={provider.created}")
print(f"after  : live={live_report(provider)}")
print(f"after  : rows={rows_report(ledger)}")
print(f"after  : state={json.loads(cfg.state_path.read_text()) if cfg.state_path.exists() else None}")
print(f"after  : ledger total={ledger.total_usd()} accruing={ledger.live_usd()} "
      f"open={ledger.open_run() and ledger.open_run().get('instance_id')}")
print(f"after  : the machine pod-A is still RUNNING; its row is "
      f"{'closed' if ledger.spent_on(next((r['run_id'] for r in ledger.entries() if r.get('instance_id') == 'pod-A'), '')) or True else '?'}")
print("up output tail:")
print("\n".join(out.strip().splitlines()[-8:]))
dcode, dout = run_doctor(cfg, provider)
print(f"doctor : exit={dcode}")
for line in dout.strip().splitlines():
    if "pod-A" in line or "ERROR" in line or "WARN" in line or "account pods" in line:
        print(f"         {line}")

# ---------------------------------------------------------------- P2
print()
print("=" * 78)
print("P2  state lost + a CLOSED row naming a live machine (stop failed at `down`); `up` again")
print("=" * 78)
cfg = fresh("p2")
provider = FakeProvider(rate_usd_per_hour=2.0)
provider.add("pod-A", uptime_seconds=7200, status="RUNNING")       # 2 h = $4, still billing
ledger = Ledger(cfg.ledger_path)
ledger.open(purpose="first", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now_utc() - timedelta(hours=2))
closed = ledger.close(note="down", instance_status="RUNNING")      # the stop was refused
print(f"before : live={live_report(provider)}  rows={rows_report(ledger)}")
print(f"before : closed row cost={closed['estimated_usd']}  cap={cfg.budget.cap_usd}  "
      f"state.json exists={cfg.state_path.exists()}")
code, out = run_up(cfg, provider)
print(f"after  : up exit={code}  created={provider.created}")
print(f"after  : live={live_report(provider)}")
print(f"after  : rows={rows_report(ledger)}")
print(f"after  : ledger total={ledger.total_usd()} accruing={ledger.live_usd()}")

# ---------------------------------------------------------------- P3
print()
print("=" * 78)
print("P3  a row closed while its machine is RUNNING: guard.snapshot / guard.decide")
print("=" * 78)
cfg = cfg_cap(10.0, "p3")
provider = FakeProvider(rate_usd_per_hour=2.0)
provider.add("pod-A", uptime_seconds=4 * 3600, status="RUNNING")   # 4 h life, $8 billed so far
ledger = Ledger(cfg.ledger_path)
ledger.open(purpose="first", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now_utc() - timedelta(hours=4))
closed = ledger.close(note="down", stopped_at=now_utc() - timedelta(hours=1),
                      instance_status="RUNNING")                   # row closed 1 h ago at $6
pod = provider.get("pod-A")
snap = guard.snapshot(pod, cfg, ledger)
print(f"ledger : closed={ledger.total_usd()} accruing={ledger.live_usd()} "
      f"closed+accruing={round(ledger.total_usd() + ledger.live_usd(), 6)}")
print(f"snap   : run_usd={snap.run_usd} spent_usd={snap.spent_usd} recorded={snap.recorded} "
      f"accrual_unknown={snap.accrual_unknown} priced_from={snap.priced_from}")
print(f"truth  : the machine's own bill = {closed['estimated_usd']} closed + "
      f"$2.00 for the hour since = ${round(closed['estimated_usd'] + 2.0, 2)}")
print(f"decide : {guard.decide(snap)!r}")

# ---------------------------------------------------------------- P4
print()
print("=" * 78)
print("P4  two rows naming one instance: `down` closed R1 (stop refused), state still names it, `up` again")
print("=" * 78)
cfg = cfg_cap(10.0, "p4")
provider = FakeProvider(rate_usd_per_hour=2.0)
provider.add("pod-A", uptime_seconds=3 * 3600, status="RUNNING")   # 3 h = $6 real
ledger = Ledger(cfg.ledger_path)
ledger.open(purpose="first", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now_utc() - timedelta(hours=3))
r1 = ledger.close(note="down", instance_status="RUNNING")          # $6 recorded over 3 h
cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
code, out = run_up(cfg, provider)
print(f"after  : up exit={code}  created={provider.created}")
print(f"after  : live={live_report(provider)}")
print(f"after  : rows={rows_report(ledger)}")
print(f"after  : ledger total={ledger.total_usd()} accruing={ledger.live_usd()} "
      f"sum={round(ledger.total_usd() + ledger.live_usd(), 6)}")
print(f"truth  : the machine's own bill = $6.00 (3 h at $2/h)")
snap = guard.snapshot(provider.get("pod-A"), cfg, ledger)
print(f"snap   : run_usd={snap.run_usd} spent_usd={snap.spent_usd}")

# ---------------------------------------------------------------- P5
print()
print("=" * 78)
print("P5  a rate that changes between the row opening and the close")
print("=" * 78)
cfg = fresh("p5")
provider = FakeProvider(rate_usd_per_hour=4.0)                     # the provider now bills $4/h
provider.add("pod-A", uptime_seconds=2 * 3600, status="RUNNING")
ledger = Ledger(cfg.ledger_path)
ledger.open(purpose="first", provider="fake", instance_id="pod-A", gpu_id="Fake GPU 24GB",
            rate_usd_per_hour=2.0, started_at=now_utc() - timedelta(hours=2))  # opened at $2/h
closed = ledger.close(note="down", stopped_at=now_utc())
print(f"ledger : closed={closed['estimated_usd']} (2 h at the row's $2/h)")
print(f"truth  : the machine billed 2 h at its own $4/h = $8.00")
print(f"cap    : cap={cfg.budget.cap_usd}; up's pre-rent check would read "
      f"{round(ledger.total_usd() + ledger.live_usd(), 6)}")

# ---------------------------------------------------------------- P6
print()
print("=" * 78)
print("P6  an open row whose start is unreadable and whose pod cannot be witnessed: status/snapshot")
print("=" * 78)
cfg = cfg_cap(15.0, "p6")
provider = FakeProvider(rate_usd_per_hour=2.0)
provider.add("pod-A", uptime_seconds=0, status="RUNNING")
ledger = Ledger(cfg.ledger_path)
ledger.append({"run_id": "20260101T000000Z-aaaaaa", "purpose": "truncated",
               "provider": "fake", "instance_id": "pod-A", "gpu_id": "Fake GPU 24GB",
               "rate_usd_per_hour": 2.0, "started_at": "the-ledger-was-truncated",
               "stopped_at": None, "wall_clock_seconds": None, "wall_clock_hours": None,
               "estimated_usd": None, "closed_by": None, "tag": ""})
pod = {"id": "pod-A", "status": "RUNNING", "cost": 2.0,
       "runtime": {"uptime": None, "ports": None, "gpus": [{"util": None}]}}
snap = guard.snapshot(pod, cfg, ledger)
print(f"ledger : open_run={ledger.open_run().get('run_id')} "
       f"quarantined={ledger.unreadable_open_rows()} total={ledger.total_usd()} "
       f"accrual()={ledger.accrual()}")
print(f"snap   : run_usd={snap.run_usd} spent_usd={snap.spent_usd} "
      f"accrual_unknown={snap.accrual_unknown} priced_from={snap.priced_from}")
with mock.patch.object(cli, "_config", lambda: cfg), \
        mock.patch.object(cli, "_provider", lambda c: provider), \
        mock.patch.object(cli, "_pod", lambda p, c: pod):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.cmd_status(cli.build_parser().parse_args(["status"]))
print(f"status : exit={code}")
for line in out.getvalue().strip().splitlines():
    if any(k in line for k in ("this run", "all runs", "UNKNOWN", "unknown", "instance", "usable")):
        print(f"         {line}")

print()
print("=" * 78)
print("done")
