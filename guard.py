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
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .ledger import Ledger, closed_run_end, iso, now_utc
from .provider import Instance, Provider, ProviderError

#: The statuses a provider reports for a machine that is genuinely up. It is vocabulary now, not a
#: decision set: since Gilbert's ruling of 2026-10-16 the question "may this machine still bill?" is
#: answered by ``is_billing`` (the terminal set's complement), because a status in neither set —
#: `STOPPING`, `ERROR` — is invisible to a test written this way round.
LIVE_STATUSES = ("RUNNING", "PROVISIONING", "STARTING")

#: Statuses that mean the rental is over. A machine in one of these cannot bill again without a new
#: start, so its ledger row — open or closed — is the whole truth about what it has cost.
TERMINAL_STATUSES = ("EXITED", "TERMINATED")


def is_billing(status: str | None) -> bool:
    """Whether a machine reporting this status may still be charging money.

    The question is answered by the **terminal** set, not by the live set (Gilbert's ruling,
    2026-10-16). Written the other way round — ``status in LIVE_STATUSES`` — every status the tool has
    never heard of fell through as "not billing": `STOPPING` (a stop in flight, still charging),
    `ERROR` (a pod that failed to start), and anything a provider adds later. Three callers read it
    that way and a machine in one of those states was invisible to all three: `up`'s
    untracked-instance refusal, `_pod`'s fallback to the open row, and ``state_findings`` (doctor's
    drift). See `tests/test_status_polarity.py`.

    A false refusal is loud, bounded and takes one word (``--force``) to override; a silent billing
    machine is neither. So a status that is missing, empty or unknown is **billing** here — nothing
    about it says the machine finished.
    """
    return str(status if status is not None else "").upper() not in TERMINAL_STATUSES

#: What an instance's **name** says about whose ledger holds it. The ledger is a local file, so a
#: machine rented by one project root is invisible in another's; the name is the only cross-root
#: signal on the account, and it is what the default name's root tag encodes.
#:
#: ``CLAIM_THIS_ROOT`` is not proof that a row exists here — a sibling root can rent under the same
#: configured name — and ``CLAIM_ANOTHER_ROOT`` is proof that this root should keep its hands off.
#: Only ``CLAIM_UNCLAIMED`` means nobody anywhere owns it, and only that is a hard error.
CLAIM_THIS_ROOT = "this-root"
CLAIM_ANOTHER_ROOT = "another-root"
CLAIM_UNCLAIMED = "unclaimed"


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
    #: The accrual below is **not a reading from the ledger row**: either the row's ``started_at`` is
    #: unreadable and the number is a witness (see ``priced_from``), or a closed row cannot say when
    #: its paid-for window ended and no remainder could be established at all. Either way the number
    #: is not the row's own, and it must not be read as though it were.
    accrual_unknown: bool = False
    #: Where that witness came from — ``"uptime"`` when the pod's own clock priced it, or
    #: ``"since-close"`` when a closed row anchored the remainder. The finding is named beside this
    #: number, never instead of it.
    priced_from: str | None = None
    #: **No price could be established** for this machine, and ``pricing_unknown_reason`` says which
    #: input was missing or unreadable. Distinct from ``priced_from``: a price derived from the pod's
    #: own clock is derived, not unknown, and keeping the two apart is what stops `up` refusing where
    #: a witness exists. A decision that would hang on the price treats this as unknown, never as
    #: zero — a cap cannot fire on a number nobody read, and ``$0.000`` here means only that.
    pricing_unknown: bool = False
    pricing_unknown_reason: str | None = None

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
        """Idle time, from whichever **idle** source has seen more of it.

        ``idle_since_start_minutes`` is deliberately not a candidate: it is elapsed watch time, and it
        grows while the GPU is busy. Treating elapsed time as idle time stops a healthy pod for being
        watched — measured 2026-10-14 in `grok/pod-timing/guard-watch.log`, a pod at ``up 0.00h``
        with ``gpu None%`` stopped as "idle 30m (limit 5 min)". The field stays on the snapshot as
        what it actually is: how long this guard has been watching, which is worth reading and is not
        a reason to stop anything.
        """
        if self.gpu_util is not None and self.gpu_util > 0:
            # The GPU is demonstrably busy. The provider's per-GPU idle is measured against the
            # **host**, so it can read half an hour on a pod whose card we can see working at 87%.
            # A sampled utilisation beats a number that is not about this pod.
            return 0.0
        candidates = [self.idle_for_minutes]
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
    raw_uptime = runtime.get("uptime")
    uptime_seconds = float(raw_uptime) if raw_uptime is not None else None
    uptime_hours = round((uptime_seconds or 0.0) / 3600.0, 4)

    unreadable = ledger.unreadable_open_rows() if open_run is not None else []
    terminal = str(pod.get("status") or "").upper() in TERMINAL_STATUSES
    # Only a machine with no row open *here* can have had an earlier part of its life recorded and
    # closed. When a row is open it is the whole record of the run in progress.
    closed = None if open_run is not None else ledger.closed_run_for(pod.get("id"))
    priced_from: str | None = None
    recorded = open_run is not None
    accrual_unknown = False
    #: Why no price could be established at all, if that is the case — the state the reading must
    #: never dress up as a number. `priced_from` answers a different question ("where did this price
    #: come from"); this one answers "is there a price at all".
    unknown_reason: str | None = None
    if open_run is not None:
        rate = float(open_run.get("rate_usd_per_hour") or 0.0)
        if unreadable and uptime_seconds is not None:
            # The row's own start cannot be read, so price it from the pod's clock: uptime is an
            # independent witness, bounded by the machine's own end by construction, so the cap can
            # still fire on the one run whose accrual is unknown. Unknown must never read as zero —
            # and the row stays quarantined and named beside this number, not instead of it. The
            # price is derived, not unknown: `priced_from` says so.
            run_usd = round(rate * uptime_hours, 6)
            priced_from = "uptime"
            accrual_unknown = True
        elif unreadable:
            # No start and no clock to stand in for it: nothing can price this run, and a `$0.000`
            # here is the cap reading low on the one run whose accrual nobody can compute.
            run_usd = 0.0
            accrual_unknown = True
            unknown_reason = (f"the open run {open_run.get('run_id')} has an unreadable "
                              f"{unreadable[0].get('field')} ({unreadable[0].get('reason')}) and the "
                              f"provider reports no uptime to price it from")
        elif rate <= 0.0:
            # A recorded run whose rate is missing or zero accrues at nothing forever: the same
            # silent zero, one field over.
            run_usd = 0.0
            accrual_unknown = True
            unknown_reason = (f"the open run {open_run.get('run_id')} carries no rate "
                              f"(rate_usd_per_hour={open_run.get('rate_usd_per_hour')!r}), so its "
                              f"accrual cannot be priced")
        else:
            run_usd = ledger.live_usd(at)
    elif closed is not None:
        # A row for this machine has already closed, and the machine is still here. The closed row
        # paid for the part of this machine's life it covered, and `total_usd()` already contains it;
        # pricing the machine from its **whole** uptime on top of that bills those same minutes
        # twice. What is left is the time since that row closed — measured from the row's own end,
        # never from "now minus uptime" — so the reading can never exceed what the machine has
        # billed. Gating this on the machine's status (`TERMINAL_STATUSES`) was not enough: a row
        # closed while the machine was still RUNNING was re-priced from its whole uptime, and the
        # stop decision ran on a total the machine had not reached (2026-10-15, $14.00 read for an
        # $8.00 bill).
        rate = float(closed.get("rate_usd_per_hour") or pod.get("cost") or 0.0)
        recorded = True
        end = closed_run_end(closed)
        if terminal:
            # The rental is over and `total_usd()` is the whole truth about what it cost. Re-pricing
            # the machine from a stale uptime invents money that was recorded once — measured
            # 2026-10-15: a sample after the batch-A row closed read `$15.581/$15.00` against a cap
            # that was never reached. A finished machine is finished business, not unrecorded.
            run_usd = 0.0
        elif end is None:
            # The row cannot say when its paid-for window ended, so the remainder cannot be computed:
            # it is unknown, which is never zero. `accrual_unknown` carries that rather than a
            # number nobody read.
            run_usd = 0.0
            accrual_unknown = True
            unknown_reason = (f"the closed row {closed.get('run_id')} names no readable end "
                              f"(stopped_at={closed.get('stopped_at')!r}), so the time this machine "
                              f"has billed since it closed cannot be priced")
        else:
            run_usd = round(rate * max(0.0, (at - end).total_seconds()) / 3600.0, 6)
            priced_from = "since-close"
    elif terminal:
        # No row at all for a machine whose rental is over: it cannot bill again without a new start,
        # so nothing is being spent and nothing needs to be invented.
        rate = float(pod.get("cost") or 0.0)
        run_usd = 0.0
        recorded = False
    else:
        # No row and still billing: the money is being spent and nothing records it. Price it from
        # the provider's uptime — the row that would have costed this run never existed, so uptime is
        # the only witness. Both halves of the price have to *be* there for that: a machine the
        # provider describes without a rate or without an uptime is one nothing can price, and
        # `$0.000` would be the cap reading low on a machine that is billing (2026-10-15).
        rate = float(pod.get("cost") or 0.0)
        if "cost" not in pod or rate <= 0.0:
            run_usd = 0.0
            accrual_unknown = True
            unknown_reason = (f"the provider reports no rate (cost={pod.get('cost')!r}) for {pod.get('id')!r}, "
                              f"and no ledger row records it, so its spend cannot be priced")
        elif uptime_seconds is None:
            run_usd = 0.0
            accrual_unknown = True
            unknown_reason = (f"the provider reports no uptime for {pod.get('id')!r}, and no ledger "
                              f"row records it, so how long it has billed cannot be priced")
        else:
            run_usd = round(rate * uptime_hours, 6)
    spent = round(ledger.total_usd() + run_usd, 6)

    provider_idle = _idle_minutes_since_boot(runtime.get("gpus"), uptime_seconds)
    if uptime_seconds is None:
        # Nothing read here can be attributed to this pod, so nothing here can be a reason to stop
        # it: the provider's per-GPU idle is measured against the host, and a caller's own count can
        # outlive the pod that a restart reset to zero uptime.
        provider_idle = None
        idle_for_minutes = 0.0
    else:
        provider_idle = _idle_minutes_since_boot(runtime.get("gpus"), uptime_seconds)
        # A pod that has existed for three minutes cannot have been idle for thirty. The caller's
        # count is about *this* pod, and a restart makes it stale.
        idle_for_minutes = min(idle_for_minutes, uptime_seconds / 60.0)
    return Snapshot(
        at=iso(at),
        status=str(pod.get("status") or "UNKNOWN"),
        uptime_hours=uptime_hours,
        gpu_util=util,
        mem_util=mem_util,
        rate_usd_per_hour=rate,
        open_run_id=(open_run or {}).get("run_id"),
        recorded=recorded,
        idle_since_start_minutes=round(idle_since_start_minutes, 1),
        max_idle_minutes=provider_idle,
        run_usd=run_usd,
        spent_usd=spent,
        cap_usd=cfg.budget.cap_usd,
        per_run_cap_usd=cfg.budget.per_run_cap_usd,
        max_hours=cfg.budget.max_hours_per_run,
        idle_minutes=cfg.budget.idle_minutes,
        idle_for_minutes=round(idle_for_minutes, 1),
        accrual_unknown=accrual_unknown,
        priced_from=priced_from,
        pricing_unknown=unknown_reason is not None,
        pricing_unknown_reason=unknown_reason,
    )


def _idle_minutes_since_boot(gpus, uptime_seconds: float | None) -> float | None:
    """How long the busiest GPU has been idle, bounded by how long the pod has existed.

    RunPod reports per-GPU idle time against the **host**, not against the pod, so a machine seconds
    old can report half an hour of idle. Comparing that with the idle window stops a healthy pod at
    its first sample — measured on the real account: a pod 32 s old was stopped for "idle for 30 min
    (limit 5 min)". With no uptime at all the reading cannot be attributed to this pod, so it is
    ignored rather than trusted.
    """
    if not gpus or uptime_seconds is None:
        return None
    idle_seconds = [float(g.get("idle") or 0.0) for g in gpus if isinstance(g, dict)]
    if not idle_seconds:
        return None
    return round(min(min(idle_seconds), uptime_seconds) / 60.0, 1)


def decide(snap: Snapshot) -> str | None:
    """Return the reason to stop, or None to keep going. Pure, and ordered by severity.

    A price nobody could establish is treated as unknown, not as zero: the per-run cap hangs on the
    run's own price, so it does not fire on one that was never read. The **total** check still does,
    because it compares the cap against money already recorded — a lower bound, and one that a stop
    may act on. The clock (`max_hours`) is independent of the price as well and keeps bounding a
    machine that is billing, which is the last thing standing between a pod the provider describes
    oddly and an unbounded bill.
    """
    if snap.status in ("EXITED", "TERMINATED"):
        return "already stopped"
    if snap.remaining_usd <= 0:
        return f"total cap reached (${snap.spent_usd:.2f} of ${snap.cap_usd:.2f})"
    if not snap.pricing_unknown and snap.run_remaining_usd <= 0:
        return f"per-run cap reached (${snap.run_usd:.2f} of ${snap.per_run_cap_usd:.2f})"
    if snap.uptime_hours >= snap.max_hours:
        return f"max hours reached ({snap.uptime_hours:.2f} h of {snap.max_hours:.2f} h)"
    if snap.idle_minutes > 0 and snap.effective_idle_minutes >= snap.idle_minutes:
        return (f"idle for {snap.effective_idle_minutes:.0f} min "
                f"(limit {snap.idle_minutes:.0f} min, gpu util {snap.gpu_util})")
    return None


def state_findings(state: dict, open_run: dict | None, *, provider_gate=None,
                   claim=None) -> list[Finding]:
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

    ``claim`` is an optional callable taking the provider's payload for an instance and returning one
    of the ``CLAIM_*`` constants: whose *name* the machine carries. It exists because "no row here"
    and "nobody recorded it" are different facts, and only the second is a hard error — a live pod
    another root's ledger is paying for must not be reported as a stray, because the fix for a stray
    is to stop it. With no ``claim`` the check behaves as it always did: a live instance with no row
    is an error.
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
        # Only drift while the machine can still be billing. An EXITED or TERMINATED instance with a
        # closed row is the correct end state after a normal `down`, not a finding — and everything
        # else is billing, including a status this tool has never heard of (`is_billing`).
        reported = ask(tracked)
        if reported is None:
            status = None  # could not ask: say nothing rather than guess
        else:
            status = str(reported.get("status") or "").upper()
        if reported is not None and is_billing(status):
            kind = claim(reported) if claim is not None else None
            if kind == CLAIM_ANOTHER_ROOT:
                findings.append(Finding(
                    code="tracked-by-another-root", severity="warn",
                    message=f"instance {tracked} reports {status or 'no status'}, which is not "
                            f"finished, and this ledger has no open row "
                            f"for it, but its name {reported.get('name')!r} belongs to a different "
                            f"rentguard root: that root's ledger is the one recording it. Do not "
                            f"stop it from here and do not `adopt` it here — either would double-count "
                            f"a machine someone else is paying for"))
            elif kind == CLAIM_THIS_ROOT:
                findings.append(Finding(
                    code="row-not-in-this-root", severity="warn",
                    message=f"instance {tracked} reports {status or 'no status'}, which is not "
                            f"finished, and no row is open for it here, "
                            f"but its name {reported.get('name')!r} is this project's: the row may be "
                            f"in another root's ledger (two roots can rent under one name), or it was "
                            f"lost. Check before stopping it; `adopt` here would double-record a "
                            f"machine another root may be paying for"))
            else:
                findings.append(Finding(
                    code="recorded-instance-no-open-run", severity="error",
                    message=f"instance {tracked} reports {status or 'no status'}, which is not "
                            f"finished, but no ledger row is open, and "
                            f"its name {str(reported.get('name') or '')!r} claims no project — its "
                            f"spend is not being recorded anywhere. `rentguard adopt {tracked} "
                            f"--purpose ...`"))
        elif reported is None and provider_gate is None:
            # No provider gate at all: the caller has not told us whether it is live, so report the
            # weaker, non-committal form rather than staying silent about a possible bill.
            findings.append(Finding(
                code="recorded-instance-no-open-run", severity="warn",
                message=f"instance {tracked} is recorded but no ledger row is open; if it is "
                        f"billing, `rentguard adopt {tracked} --purpose ...`"))

    return findings


#: How long a running guard may go quiet before its heartbeat is *stale* as well as running. The
#: verdict is the process fact; this is the window the human line calls "probably wedged" and the
#: window the structured ``stale`` flag has always meant.
GUARD_STALE_MINUTES = 5.0


@dataclass
class HeartbeatReading:
    """What the heartbeat file says about its writer, and whether that writer is still running.

    The verdict is the writer's own **mark** first and its **pid** second, with the age carried beside
    them as the secondary signal it has always been (Gilbert's ruling, 2026-10-16: *liveness must be a
    process fact, not a timestamp fact*). ``alive`` needs both facts: a guard that has finished is not
    protection even while its process lives, and a pid that is gone is not protection even when the
    heartbeat is seconds old. ``stale`` is the **age** window, and it stays that: a running guard that
    has been silent for longer than the window is alive *and* stale, which is what a caller gating on
    protection needs to see (Assay's finding 16, 2026-10-16).
    """

    age_minutes: float
    pid: int | None
    ended: bool
    running: bool

    @property
    def alive(self) -> bool:
        return self.running and not self.ended

    @property
    def stale(self) -> bool:
        return self.age_minutes > GUARD_STALE_MINUTES or not self.alive


def pid_running(pid: int | None, *, kill=os.kill) -> bool:
    """Whether a process with this pid is running on this host.

    Three answers, not two, and the middle one is the one a single ``except OSError`` gets wrong
    (Gilbert's ruling, 2026-10-16):

    - ``ProcessLookupError`` — POSIX: no such process. **Gone.**
    - ``PermissionError`` — POSIX, and Windows access denied: the process is there and is not ours to
      signal. **Running**; calling this gone is the direction that silently disarms the check.
    - ``OSError`` — Windows raises ``EINVAL`` (errno 22) for a pid with no process, measured on this
      host on 2026-10-16; ``os.kill(pid, 0)`` was also measured *not* to terminate the target there.
      **Gone**, and any other ``OSError`` reads the same way: a probe that failed is not proof of life.
    - ``OverflowError`` — POSIX raises it for a pid that does not fit a C int (``2 ** 40``), which a
      hand-edited, truncated or foreign heartbeat can carry. **Gone** (finding 15, 2026-10-16): a read
      that a corrupt pid can crash is a read that takes `status` and `doctor` with it, and the honest
      answer for a pid nobody can signal is that there is no proof of life.

    ``kill`` is a seam so both platforms' behaviours are testable on either host; production passes
    ``os.kill``. There is one documented limit, in `docs/limitations.md`: on Windows an exited process
    whose handle is still held elsewhere can be probed successfully, which is why a clean exit's own
    mark — not this probe — is what a finished guard relies on.
    """
    try:
        number = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def guard_heartbeat_path(path: Path, guard_id: str) -> Path:
    """The file one guard writes: ``heartbeat-<guard_id>.json`` beside the project's heartbeat.

    **One file per guard**, because one file cannot carry two guards' liveness. Two guards can watch
    one instance in one process — an operator's `watch` beside `up`'s wait guard, or two wait guards —
    where the pid *and* the instance id are shared, so a single file let the one that finished mark the
    other's reading as `finished` (finding 17, 2026-10-16). The plain ``heartbeat.json`` stays readable
    as the legacy single-guard name.
    """
    return path.parent / f"heartbeat-{guard_id}.json"


def heartbeat_files(path: Path) -> list[Path]:
    """Every heartbeat file this project may have written, newest first.

    The legacy single file plus one per guard. Sorted by modification time so a caller that only wants
    the latest raw payload (a test, a probe) does not have to parse them all.
    """
    found = [path] if path.exists() else []
    found.extend(p for p in path.parent.glob("heartbeat-*.json") if p != path)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def heartbeat_readings(path: Path, *, kill=None) -> list[HeartbeatReading]:
    """Every readable heartbeat in this project's set, best first (see ``heartbeat_reading``)."""
    readings = []
    for candidate in heartbeat_files(path):
        reading = _read_one(candidate, kill=kill)
        if reading is not None:
            readings.append(reading)
    return _best_first(readings)


def _best_first(readings: list[HeartbeatReading]) -> list[HeartbeatReading]:
    """Order readings by what a caller asking "is anything watching?" should see first.

    A live, unmarked, fresh guard comes first; then any live one (a wedged guard is still a process);
    then the newest of whatever is left, so `status` reports `finished` or `not running` rather than
    nothing when every guard has ended.
    """
    def rank(reading: HeartbeatReading) -> tuple[int, float]:
        if reading.alive:
            return 0, reading.age_minutes
        if reading.running and not reading.ended:
            return 1, reading.age_minutes
        return 2, reading.age_minutes
    return sorted(readings, key=rank)


def heartbeat_reading(path: Path, *, kill=None) -> HeartbeatReading | None:
    """The reading `status`, `doctor` and `watch` decide on: the **best** of the guard set.

    ``None`` means *no reading* — no heartbeat file at all, or none readable — and every caller reads
    it as *no guard*. A live unmarked guard anywhere in the set wins, so one guard finishing cannot
    hide another that is still watching (finding 17); when none is alive, the newest reading is
    returned, so a finished or gone guard is still reported as exactly that.
    """
    readings = heartbeat_readings(path, kill=kill)
    return readings[0] if readings else None


def _read_one(path: Path, *, kill=None) -> HeartbeatReading | None:
    """One file, or ``None`` when it is not a readable heartbeat."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        then = datetime.fromisoformat(payload["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_pid = payload.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) else None
    running = pid_running(pid) if kill is None else pid_running(pid, kill=kill)
    return HeartbeatReading(age_minutes=round((now_utc() - then).total_seconds() / 60.0, 1),
                            pid=pid, ended=bool(payload.get("ended")), running=running)


def heartbeat_age_minutes(path: Path) -> float | None:
    """How long ago the guard last reported, or ``None`` with no readable heartbeat.

    Kept for callers that want only the age, but the age alone cannot tell a live guard from one that
    has finished — that verdict is ``heartbeat_reading``'s (ruling, 2026-10-16).
    """
    reading = heartbeat_reading(path)
    return None if reading is None else reading.age_minutes


def write_heartbeat(path: Path, snap: Snapshot, note: str = "",
                    guard_id: str | None = None) -> None:
    """Write a reading, naming the **process** and the **guard** that wrote it, unmarked.

    ``pid`` used to be written as a placeholder (``None``), which is why `status` had nothing but the
    age to go on and read a finished guard as protection (Gilbert's ruling, 2026-10-16). ``guard_id``
    is the identity two guards in *one* process do not share: the pid cannot tell them apart, so a mark
    matched on the pid alone ended a live guard's file (finding 17, 2026-10-16).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(snap)
    payload["note"] = note
    payload["pid"] = os.getpid()
    payload["guard_id"] = guard_id
    payload["ended"] = False
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _prune_finished_heartbeats(path)


def _prune_finished_heartbeats(path: Path) -> None:
    """Remove sibling heartbeat files that have been marked finished and gone quiet.

    One file per guard means a file per guard invocation, so a clean exit's marker would otherwise
    accumulate for ever. Only files that say they are **ended** and are older than the stale window are
    removed — a live guard's file is never touched, and a crashed guard's file (a dead pid, no mark)
    stays, which is evidence worth keeping. Best-effort: a file that cannot be read or removed is left
    for a caller that can.
    """
    for sibling in path.parent.glob("heartbeat-*.json"):
        if sibling == path:
            continue
        reading = _read_one(sibling)
        if reading is None or not reading.ended or reading.age_minutes <= GUARD_STALE_MINUTES:
            continue
        try:
            sibling.unlink()
        except OSError:
            continue


def end_heartbeat(path: Path, pid: int | None = None, guard_id: str | None = None) -> bool:
    """Mark the heartbeat finished, when it is this guard's to mark: ``True`` if it was.

    A guard that exits cleanly says so, so "finished" and "silent" stop being the same reading. The
    file is marked only when it is **this guard's**: ``guard_id`` is the identity two guards in one
    process do not share, and the pid covers a file written by another process (a legacy payload with
    no ``guard_id`` is matched on the pid alone). Two guards can share one heartbeat path — an
    operator's `watch` and `up`'s wait guard, or two wait guards — and a file another guard wrote is
    left alone. A missing file is left missing: its absence already reads as *no guard*, and writing a
    marker there would invent a reading nobody made.
    """
    if not path.exists():
        return False
    mine = os.getpid() if pid is None else pid
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    owner_id = payload.get("guard_id")
    if owner_id is not None and owner_id != guard_id:
        return False
    owner = payload.get("pid")
    if owner_id is None and isinstance(owner, int) and owner != mine:
        return False
    payload["ended"] = True
    payload["ended_at"] = iso(now_utc())
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


class ReleaseGate(threading.Event):
    """A wait guard's release handle, and the lock that makes a released guard write nothing.

    `up`'s wait guard polls the provider while `up` waits, and the heartbeat it writes is the file
    ``status`` and ``doctor`` read to answer "is anything watching?". A guard the caller has released
    must therefore write nothing more: a heartbeat stamped after that release reads as protection
    armed while nothing is watching, which is the shape this project has paid for twice (2026-10-15,
    and again as re-attack finding 4 on 2026-10-16).

    ``attempt`` and ``set`` share one lock, so "still armed?" and the write are a single step: a
    release either happens before a write — which then does not happen — or after a write has
    completed, and that write belongs to the guard while it was still armed. This matters because a
    bounded ``join`` can return with the thread still inside a slow ``provider.get``; with this gate
    that read can no longer land a heartbeat, so the join bound is no longer what correctness rests
    on.

    It *is* an ``Event`` (``is_set``/``wait``), because it is also what ends the loop.
    """

    def __init__(self) -> None:
        super().__init__()
        self._write_lock = threading.Lock()

    def attempt(self, write) -> bool:
        """Run ``write`` if this guard is still armed; return whether it ran."""
        with self._write_lock:
            if self.is_set():
                return False
            write()
            return True

    def set(self) -> None:
        """Release the guard: after this returns, no gate of this guard's writes can run."""
        with self._write_lock:
            super().set()


def watch(cfg: Config, provider: Provider, ledger: Ledger, *, instance_id: str,
          interval: float = 60.0, log=print, max_iterations: int | None = None,
          stop_event=None, guard_id: str | None = None,
          heartbeat_path: Path | None = None) -> str:
    """Foreground supervisor. Returns the reason it stopped.

    Every iteration re-reads the provider; nothing is carried in from an earlier reading. The only
    state that accumulates is how long the site of the machine has been idle.

    ``stop_event`` is an optional `threading.Event`-like object (``is_set()`` and ``wait(timeout)``):
    the loop ends when it is set, and waits on it instead of sleeping so the caller can end the loop
    promptly. It exists for `up`'s wait-guard (B19, 2026-10-16): a thread watching the machine while
    `up` waits for it to become usable must stop before `up` returns, because a heartbeat left behind
    by a guard that dies with the process is exactly the "protection believed armed" shape this
    project keeps finding. It stops nothing on the way out; the reason says so.

    When the event is a ``ReleaseGate`` — which is what the wait guard passes — every heartbeat is
    written through it, so a release that arrives while the guard is inside a slow ``provider.get``
    cannot be followed by a heartbeat, and the guard returns ``wait-guard-released`` instead of
    reporting a reading taken before the release. A plain ``Event`` or ``None`` keeps the old
    behaviour: check at the top of each iteration, write as usual.

    ``guard_id`` names **this guard** in every heartbeat it writes, and ``heartbeat_path`` is the file
    it writes to — one file per guard (``guard_heartbeat_path``), because one file cannot carry two
    guards' liveness (finding 17, 2026-10-16).
    """
    heartbeat_path = heartbeat_path or cfg.heartbeat_path
    idle_from_events = 0.0
    started = now_utc()
    iterations = 0

    def heartbeat(snap: Snapshot, note: str = "") -> bool:
        """Write the heartbeat, unless the caller has released this guard.

        The check and the write are one step when the release handle carries a gate
        (``ReleaseGate.attempt``), so a guard released during a slow provider read — the case a
        bounded join could not close — cannot land a heartbeat afterwards. The write names this guard
        (``guard_id``), so a second guard in the same process is not mistaken for it later.
        """
        gate = getattr(stop_event, "attempt", None)
        if gate is None:
            write_heartbeat(heartbeat_path, snap, note=note, guard_id=guard_id)
            return True
        return bool(gate(lambda: write_heartbeat(heartbeat_path, snap, note=note,
                                                 guard_id=guard_id)))

    while True:
        if stop_event is not None and stop_event.is_set():
            return "wait-guard-released"
        iterations += 1
        try:
            pod = provider.get(instance_id)
        except ProviderError as exc:
            log(f"provider error: {exc}")
            return f"provider-error: {exc}"
        if stop_event is not None and stop_event.is_set():
            # Released while this iteration was reading the provider. Nothing below this line may
            # happen: a reading taken before the release is not a reason to stop a machine the
            # caller has taken over, and a heartbeat stamped now would be dated after the release.
            return "wait-guard-released"
        if pod is None:
            # Gone from the account: close the row so its cost stops being invisible.
            row = ledger.close(note="guard-instance-gone")
            if row:
                log(f"instance is gone; closed run {row['run_id']} at ${row['estimated_usd']}")
            else:
                log("instance no longer exists; guard exits")
            if heartbeat(snapshot_fallback(cfg, ledger), note="instance gone"):
                return "instance gone"
            return "wait-guard-released"

        runtime = pod.get("runtime") or {}
        gpus = runtime.get("gpus") or []
        raw_util = gpus[0].get("util") if gpus else None
        util = float(gpus[0].get("util") or 0.0) if gpus else 0.0
        # Idle accumulates in **minutes** and only when the GPU is *known* to be doing nothing:
        # ``interval`` is seconds (adding it raw made five minutes of watching look like five hours),
        # and an absent utilisation reading is not evidence of idleness — the log line that reported
        # this defect said ``gpu None%``.
        if raw_util is not None and float(raw_util) == 0.0:
            idle_from_events = idle_from_events + interval / 60.0
        else:
            idle_from_events = 0.0

        snap = snapshot(pod, cfg, ledger, idle_for_minutes=idle_from_events,
                        idle_since_start_minutes=(now_utc() - started).total_seconds() / 60.0)
        if not heartbeat(snap):
            return "wait-guard-released"
        # An unreadable ledger row is a reason to keep watching, not to stop: the accrual is unknown,
        # which is never a reason to kill a machine that is billing — a dead guard is worse than a
        # wrong number, and this is the failure the tool exists to prevent (2026-10-14).
        unreadable = ledger.unreadable_open_rows()
        if unreadable:
            for row in unreadable:
                log(f"ledger row {row['run_id']} has an unreadable {row['field']} "
                    f"({row['reason']}): its accrual is UNKNOWN and the guard keeps watching")
            reason = None
        else:
            reason = decide(snap)
        if snap.pricing_unknown:
            # Never a silent zero: the log names what could not be read, so a person reads
            # "no price" rather than "$0.000", and the heartbeat carries the same flag.
            log(f"  pricing UNKNOWN: {snap.pricing_unknown_reason} — the guard keeps watching, and "
                f"the money figure below is a lower bound")
        log(f"  {snap.at}  {snap.status:<10} up "
            f"{f'{snap.uptime_hours:5.2f}h' if runtime.get('uptime') is not None else '   ?  h'}  "
            f"run ${snap.run_usd:.3f}"
            f"{'' if snap.recorded else ' (unrecorded)'}"
            f"{' (since the row closed)' if snap.priced_from == 'since-close' else ''}"
            f"{' (UNKNOWN: not read)' if snap.accrual_unknown else ''}  "
            f"total ${snap.spent_usd:.3f}/${snap.cap_usd:.2f}  "
            f"gpu {snap.gpu_util}%  idle {snap.effective_idle_minutes:.0f}m")
        if reason:
            if snap.status not in ("EXITED", "TERMINATED"):
                provider.action(instance_id, "stop")
                log(f"guard stopped the instance: {reason}")
            ledger.close(note=f"guard:{reason}", instance_status=snap.status)
            heartbeat(snap, note=f"stopped: {reason}")
            return reason
        if max_iterations is not None and iterations >= max_iterations:
            return "guard-iteration-limit"
        if interval > 0:
            if stop_event is not None:
                # Woken rather than slept: the caller sets it to end this loop and then joins the
                # thread, so a released guard is finished before the caller returns.
                stop_event.wait(interval)
            else:
                import time
                time.sleep(interval)


def snapshot_fallback(cfg: Config, ledger: Ledger) -> Snapshot:
    """A heartbeat-shaped snapshot for the moment there is no instance to read."""
    return snapshot({"id": "", "status": "GONE", "runtime": {}}, cfg, ledger)
