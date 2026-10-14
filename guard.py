"""The guard: decide when a rented machine should stop.

The guard is the only part of rentguard that spends money by not acting, so it is written to be
boring, inspectable and testable without a network:

- ``snapshot`` turns a provider payload and the ledger into plain numbers.
- ``decide`` is a pure function from a snapshot to a reason (or None).
- ``watch`` is the loop, and does nothing but call the two above and then stop the instance.
- ``state_findings`` is the pure drift check that ``doctor`` reports: the ledger and the account
  disagreeing, in either direction.

If the numbers are wrong the loop is wrong; the loop itself never invents one.

The one case that is easy to get wrong, and is handled here on purpose: **a machine can be billing
with no ledger row** (a lost append, a hand-made pod, a crash between rent and record). The guard
must still know what it is costing, so when no row is open it takes the cost from the provider's
own uptime instead of assuming zero. Assuming zero is how a cap fails silently.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .ledger import Ledger, iso, now_utc
from .provider import Instance, Provider, ProviderError

#: Statuses that mean "this machine can still be billing". A recorded instance that is not in this
#: set is finished business, not drift: a clean `down` leaves exactly that.
LIVE_STATUSES = ("RUNNING", "PROVISIONING", "STARTING")


@dataclass
class Finding:
    """One disagreement between what is recorded and what exists."""

    code: str
    severity: str  # "error" stops the line; "warn" is worth reading
    message: str


@dataclass
class Snapshot:
    at: str
    status: str
    uptime_hours: float
    gpu_util: float | None
    mem_util: float | None
    rate_usd_per_hour: float
    open_run_id: str | None
    run_usd: float          # this run, so far
    spent_usd: float        # every closed run + this run
    cap_usd: float
    per_run_cap_usd: float
    max_hours: float
    idle_minutes: float
    recorded: bool = True                   # is there a ledger row for this machine?
    idle_since_start_minutes: float = 0.0   # idle observed by the caller since it started
    idle_for_minutes: float = 0.0
    max_idle_minutes: float | None = None   # from the provider's own reading, when reported

    @property
    def remaining_usd(self) -> float:
        return round(self.cap_usd - self.spent_usd, 6)

    @property
    def run_remaining_usd(self) -> float:
        return round(self.per_run_cap_usd - self.run_usd, 6)

    @property
    def minutes_to_cap(self) -> float | None:
        if self.rate_usd_per_hour <= 0:
            return None
        return round(self.remaining_usd / self.rate_usd_per_hour * 60.0, 1)

    @property
    def effective_idle_minutes(self) -> float:
        """Idle time, from whichever source has seen more of it."""
        candidates = [self.idle_for_minutes, self.idle_since_start_minutes]
        if self.max_idle_minutes is not None:
            candidates.append(self.max_idle_minutes)
        return round(max(candidates), 1)


def snapshot(pod: Instance, cfg: Config, ledger: Ledger, *,
             idle_for_minutes: float = 0.0, idle_since_start_minutes: float = 0.0,
             at: datetime | None = None) -> Snapshot:
    at = at or now_utc()
    runtime = pod.get("runtime") or {}
    gpus = runtime.get("gpus") or []
    util = float(gpus[0].get("util") or 0.0) if gpus else None
    mem_util = float(gpus[0].get("memoryUtil") or 0.0) if gpus else None
    open_run = ledger.open_run()
    uptime_hours = round(float(runtime.get("uptime") or 0) / 3600.0, 4)

    if open_run is not None:
        run_usd = ledger.live_usd(at)
        rate = float(open_run.get("rate_usd_per_hour") or 0.0)
    else:
        # No row: the money is still being spent. Price it from the provider's uptime — the row
        # that would have costed this run never existed, so uptime is the only witness.
        rate = float(pod.get("cost") or 0.0)
        run_usd = round(rate * uptime_hours, 6)
    spent = round(ledger.total_usd() + run_usd, 6)

    provider_idle = _idle_minutes_since_boot(runtime.get("gpus"))
    return Snapshot(
        at=iso(at),
        status=str(pod.get("status") or "UNKNOWN"),
        uptime_hours=uptime_hours,
        gpu_util=util,
        mem_util=mem_util,
        rate_usd_per_hour=rate,
        open_run_id=(open_run or {}).get("run_id"),
        recorded=open_run is not None,
        idle_since_start_minutes=round(idle_since_start_minutes, 1),
        max_idle_minutes=provider_idle,
        run_usd=run_usd,
        spent_usd=spent,
        cap_usd=cfg.budget.cap_usd,
        per_run_cap_usd=cfg.budget.per_run_cap_usd,
        max_hours=cfg.budget.max_hours_per_run,
        idle_minutes=cfg.budget.idle_minutes,
        idle_for_minutes=round(idle_for_minutes, 1),
    )


def _idle_minutes_since_boot(gpus) -> float | None:
    """RunPod reports how long each GPU has been idle since boot; use the busiest one."""
    if not gpus:
        return None
    idle_seconds = [float(g.get("idle") or 0.0) for g in gpus if isinstance(g, dict)]
    if not idle_seconds:
        return None
    return round(min(idle_seconds) / 60.0, 1)


def decide(snap: Snapshot) -> str | None:
    """Return the reason to stop, or None to keep going. Pure, and ordered by severity."""
    if snap.status in ("EXITED", "TERMINATED"):
        return "already stopped"
    if snap.remaining_usd <= 0:
        return f"total cap reached (${snap.spent_usd:.2f} of ${snap.cap_usd:.2f})"
    if snap.run_remaining_usd <= 0:
        return f"per-run cap reached (${snap.run_usd:.2f} of ${snap.per_run_cap_usd:.2f})"
    if snap.uptime_hours >= snap.max_hours:
        return f"max hours reached ({snap.uptime_hours:.2f} h of {snap.max_hours:.2f} h)"
    if snap.idle_minutes > 0 and snap.effective_idle_minutes >= snap.idle_minutes:
        return (f"idle for {snap.effective_idle_minutes:.0f} min "
                f"(limit {snap.idle_minutes:.0f} min, gpu util {snap.gpu_util})")
    return None


def state_findings(state: dict, open_run: dict | None, *, provider_gate=None) -> list[Finding]:
    """Drift between the state file and the ledger, in both directions.

    ``state`` stays the source of truth for "which instance did I mean"; the ledger for "is that
    instance paid for". Two silent failures come from these disagreeing:

    - a row is open and its instance is gone (or is not recorded anywhere), so nothing is billed
      as far as the ledger is concerned, and the spend hides;
    - a *live* instance is recorded and no row is open, so a machine can bill with nothing watching
      it.

    The word that carries the second case is **live**. An instance that has EXITED or TERMINATED is
    finished business: a clean `down` leaves exactly that, and reporting it as drift every time would
    make the drift warning meaningless.

    ``provider_gate`` is an optional callable taking an instance id and returning what the provider
    says, or ``None`` when it cannot tell (which is not the same as "missing"):

    - a dict for a live instance (its canonical payload) — status is read from it;
    - an empty dict for a non-live or gone instance;
    - ``None`` when the provider could not be asked.

    It is how the doctor turns a suspicion into a finding without doing its own I/O.
    """
    findings: list[Finding] = []
    tracked = state.get("instance_id")

    def ask(instance_id: str) -> dict | None:
        if provider_gate is None or not instance_id:
            return None
        return provider_gate(instance_id)

    if open_run is not None:
        row_instance = open_run.get("instance_id")
        if not row_instance:
            findings.append(Finding(
                code="open-run-no-instance", severity="error",
                message=f"run {open_run.get('run_id')} is open with no instance recorded: "
                        f"nothing links it to a machine"))
        else:
            if tracked and tracked != row_instance:
                findings.append(Finding(
                    code="state-row-mismatch", severity="error",
                    message=f"state records instance {tracked!r} but run "
                            f"{open_run.get('run_id')} is against {row_instance!r}"))
            # A row that is open while the provider cannot see its instance is money with no
            # machine behind it. ``None`` means "could not ask", which is not "gone".
            if provider_gate is not None and ask(row_instance) == {}:
                findings.append(Finding(
                    code="open-run-instance-missing", severity="error",
                    message=f"open run {open_run.get('run_id')} names instance "
                            f"{row_instance!r}, which the provider does not show"))
    elif tracked:
        # Only drift while the machine can still be billing. An EXITED instance with a closed row
        # is the correct end state after a normal `down`, not a finding.
        reported = ask(tracked)
        if reported is None:
            status = None  # could not ask: say nothing rather than guess
        else:
            status = str(reported.get("status") or "").upper()
        if status in LIVE_STATUSES:
            findings.append(Finding(
                code="recorded-instance-no-open-run", severity="error",
                message=f"instance {tracked} is live ({status}) but no ledger row is open; its "
                        f"spend is not being recorded. `rentguard adopt {tracked} --purpose ...`"))
        elif reported is None and provider_gate is None:
            # No provider gate at all: the caller has not told us whether it is live, so report the
            # weaker, non-committal form rather than staying silent about a possible bill.
            findings.append(Finding(
                code="recorded-instance-no-open-run", severity="warn",
                message=f"instance {tracked} is recorded but no ledger row is open; if it is "
                        f"billing, `rentguard adopt {tracked} --purpose ...`"))

    return findings


def write_heartbeat(path: Path, snap: Snapshot, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(snap)
    payload["note"] = note
    payload["pid"] = None
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def heartbeat_age_minutes(path: Path) -> float | None:
    """How long ago the guard last reported. A supervisor you believe in and that is not running is
    worse than no supervisor; this is how ``status`` tells you which one you have."""
    if not path.exists():
        return None
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))["at"]
        then = datetime.fromisoformat(stamp)
    except (OSError, ValueError, KeyError):
        return None
    return round((now_utc() - then).total_seconds() / 60.0, 1)


def watch(cfg: Config, provider: Provider, ledger: Ledger, *, instance_id: str,
          interval: float = 60.0, log=print, max_iterations: int | None = None) -> str:
    """Foreground supervisor. Returns the reason it stopped.

    Every iteration re-reads the provider; nothing is carried in from an earlier reading. The only
    state that accumulates is how long the site of the machine has been idle.
    """
    idle_from_events = 0.0
    started = now_utc()
    iterations = 0
    while True:
        iterations += 1
        try:
            pod = provider.get(instance_id)
        except ProviderError as exc:
            log(f"provider error: {exc}")
            return f"provider-error: {exc}"
        if pod is None:
            # Gone from the account: close the row so its cost stops being invisible.
            row = ledger.close(note="guard-instance-gone")
            if row:
                log(f"instance is gone; closed run {row['run_id']} at ${row['estimated_usd']}")
            else:
                log("instance no longer exists; guard exits")
            write_heartbeat(cfg.heartbeat_path, snapshot_fallback(cfg, ledger),
                            note="instance gone")
            return "instance gone"

        runtime = pod.get("runtime") or {}
        gpus = runtime.get("gpus") or []
        util = float(gpus[0].get("util") or 0.0) if gpus else 0.0
        idle_from_events = idle_from_events + interval if util == 0.0 else 0.0

        snap = snapshot(pod, cfg, ledger, idle_for_minutes=idle_from_events,
                        idle_since_start_minutes=(now_utc() - started).total_seconds() / 60.0)
        write_heartbeat(cfg.heartbeat_path, snap)
        reason = decide(snap)
        log(f"  {snap.at}  {snap.status:<10} up {snap.uptime_hours:5.2f}h  "
            f"run ${snap.run_usd:.3f}{'' if snap.recorded else ' (unrecorded)'}  "
            f"total ${snap.spent_usd:.3f}/${snap.cap_usd:.2f}  "
            f"gpu {snap.gpu_util}%  idle {snap.effective_idle_minutes:.0f}m")
        if reason:
            if snap.status not in ("EXITED", "TERMINATED"):
                provider.action(instance_id, "stop")
                log(f"guard stopped the instance: {reason}")
            ledger.close(note=f"guard:{reason}", instance_status=snap.status)
            write_heartbeat(cfg.heartbeat_path, snap, note=f"stopped: {reason}")
            return reason
        if max_iterations is not None and iterations >= max_iterations:
            return "guard-iteration-limit"
        if interval > 0:
            import time
            time.sleep(interval)


def snapshot_fallback(cfg: Config, ledger: Ledger) -> Snapshot:
    """A heartbeat-shaped snapshot for the moment there is no instance to read."""
    return snapshot({"id": "", "status": "GONE", "runtime": {}}, cfg, ledger)
