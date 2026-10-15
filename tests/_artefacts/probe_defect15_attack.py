"""Probe: independent attack on the defect-15 fix (`cdb2fba`), in both directions.

Direction (a): make a closed row contribute to the guard's money again.
Direction (b): make a live, unrecorded machine stop being priced.
Plus the cap-crossing shape: what a poll interval costs on a fast meter.

Read-only with respect to the implementation; the provider is the in-memory fake for the
live-pod shapes and hand-built canonical payloads for the branch shapes.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent))

from rentguard import guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import run_root  # noqa: E402

BASE = run_root() / "defect15-attack"
BASE.mkdir(parents=True, exist_ok=True)
RATE = 2.0


def cfg_for(cap: float, name: str) -> Config:
    root = BASE / name
    root.mkdir(parents=True, exist_ok=True)
    return Config(budget=Budget(cap_usd=cap, per_run_cap_usd=cap, idle_minutes=0.0),
                  state_dir=root / ".rentguard")


def pod(status: str, uptime, *, cost: float | None = RATE, ident: str = "pod-A") -> dict:
    payload = {"id": ident, "status": status,
               "runtime": {"uptime": uptime, "ports": {"22": "tcp"},
                           "gpus": [{"util": 0.0}]}}
    if cost is not None:
        payload["cost"] = cost
    return payload


def show(tag: str, snap, ledger: Ledger) -> None:
    reason = guard.decide(snap)
    print(f"{tag:<34} status={snap.status:<9} run_usd={snap.run_usd:<9} "
          f"spent_usd={snap.spent_usd:<9} total={ledger.total_usd():<9} "
          f"recorded={snap.recorded!s:<5} rate={snap.rate_usd_per_hour:<5} decide={reason!r}")


print("=" * 100)
print("(a) a closed row must not contribute anything to the guard's money again")
print("=" * 100)

# A1: the exact defect-15 shape: EXITED, closed row for it, uptime ten times the row's window.
cfg = cfg_for(15.0, "a1")
led = Ledger(cfg.ledger_path)
led.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
         rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
closed = led.close(note="down", instance_status="EXITED")            # $2.00
show("A1 EXITED, closed row, up 10h", guard.snapshot(pod("EXITED", 36000.0), cfg, led), led)
print(f"{'':34} closed row ${closed['estimated_usd']} + machine uptime 10 h would have read "
      f"${RATE * 10:.2f}")

# A2: TERMINATED, same.
cfg = cfg_for(15.0, "a2")
led = Ledger(cfg.ledger_path)
led.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
         rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
led.close(note="down", instance_status="TERMINATED")
show("A2 TERMINATED, closed row, up 10h", guard.snapshot(pod("TERMINATED", 36000.0), cfg, led), led)

# A3: EXITED with NO closed row for it (another instance's closed row is present).
cfg = cfg_for(15.0, "a3")
led = Ledger(cfg.ledger_path)
led.open(purpose="other", provider="fake", instance_id="pod-B", gpu_id="g",
         rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
led.close(note="down", instance_status="EXITED")
show("A3 EXITED, no row for it, up 10h", guard.snapshot(pod("EXITED", 36000.0), cfg, led), led)

# A4: the live-status machine with a closed row for it — the branch is by status, not by row.
for status in ("PROVISIONING", "STARTING"):
    cfg = cfg_for(15.0, f"a4-{status}")
    led = Ledger(cfg.ledger_path)
    led.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
             rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=4))
    led.close(note="down", stopped_at=now_utc() - timedelta(hours=1),
              instance_status=status)                                 # $6.00, closed 1 h ago
    show(f"A4 {status}, closed row, up 4h", guard.snapshot(pod(status, 14400.0), cfg, led), led)
print(f"{'':34} the machine's own bill is $6.00 closed + $2.00 since = $8.00")

# A5: RUNNING with a closed row for it (the same shape as the money-direction file, for the record).
cfg = cfg_for(10.0, "a5")
led = Ledger(cfg.ledger_path)
led.open(purpose="batch A", provider="fake", instance_id="pod-A", gpu_id="g",
         rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=4))
led.close(note="down", stopped_at=now_utc() - timedelta(hours=1), instance_status="RUNNING")
show("A5 RUNNING, closed row, up 4h", guard.snapshot(pod("RUNNING", 14400.0), cfg, led), led)

print()
print("=" * 100)
print("(b) a live, unrecorded machine must stay priced")
print("=" * 100)

# B1: control — RUNNING, no row at all, cost known, one hour of uptime.
cfg = cfg_for(10.0, "b1")
led = Ledger(cfg.ledger_path)
show("B1 RUNNING, no row, cost 2, up 1h", guard.snapshot(pod("RUNNING", 3600.0), cfg, led), led)

# B2: the control attacked — the same machine with no `cost` in the payload.
cfg = cfg_for(10.0, "b2")
led = Ledger(cfg.ledger_path)
show("B2 RUNNING, no row, cost ABSENT", guard.snapshot(pod("RUNNING", 3600.0, cost=None), cfg, led), led)

# B3: the control attacked — RUNNING with no uptime (the crash-loop payload).
cfg = cfg_for(10.0, "b3")
led = Ledger(cfg.ledger_path)
show("B3 RUNNING, no row, uptime None", guard.snapshot(pod("RUNNING", None), cfg, led), led)

# B4: RUNNING with no row for it, but another instance's closed row present (must not confuse).
cfg = cfg_for(10.0, "b4")
led = Ledger(cfg.ledger_path)
led.open(purpose="other", provider="fake", instance_id="pod-B", gpu_id="g",
         rate_usd_per_hour=RATE, started_at=now_utc() - timedelta(hours=1))
led.close(note="down", instance_status="EXITED")
show("B4 RUNNING, no row for it, cost 2", guard.snapshot(pod("RUNNING", 3600.0), cfg, led), led)

print()
print("=" * 100)
print("(c) an accrual that crosses the cap between two polls")
print("=" * 100)
cfg = cfg_for(1.0, "c1")
cfg.budget.per_run_cap_usd = 1.0
led = Ledger(cfg.ledger_path)
started = now_utc() - timedelta(hours=1)
led.open(purpose="fast meter", provider="fake", instance_id="pod-A", gpu_id="g",
         rate_usd_per_hour=60.0, started_at=started)
for seconds in (59.0, 61.0, 120.0):
    at = started + timedelta(seconds=seconds)
    snap = guard.snapshot(pod("RUNNING", seconds, cost=60.0), cfg, led, at=at)
    print(f"{seconds:>6.0f} s after the start: spent=${snap.spent_usd:<8} "
          f"decide={guard.decide(snap)!r}")
print(f"{'':34} cap $1.00, rate $60/h: the crossing is at 60 s, the first decision after it")
print(f"{'':34} is at the next poll (60 s later) = $2.00, twice the cap. Documented in cap.md")
print(f"{'':34} as 'overshoots by at least one poll'; the overshoot bound is rate x interval,")
print(f"{'':34} not a fraction of the cap.")

print()
print("done")
