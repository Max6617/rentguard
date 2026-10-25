# What the cap guarantees, and what it does not

A cap is a dollar number in `rentguard.toml`. It is the reason this tool exists, so it is worth stating
exactly what it does.

**The cap only exists while `rentguard watch` is running.** It is not a property of the machine and not
something the provider enforces; it is a property of the guard. `watch` is a foreground process that
nothing starts for you. If no guard is running there is no cap: the instance bills until someone
notices. That is the failure this tool was built for, and it is the honest limit of everything below.

**The short version.** While `rentguard watch` runs, it stops the instance when the estimate for spend
crosses the cap. `up` checks the same budget before it rents. Everything else on this page is about what
that covers and what it misses.

## The two caps

| Setting | Default | What it bounds |
| --- | --- | --- |
| `[budget].cap_usd` | `25.0` | the **whole project**, across every run, forever |
| `[budget].per_run_cap_usd` | `5.0` | **one open row** — one rental, from `up` to `down` |
| `[budget].max_hours_per_run` | `6.0` | one rental's wall clock, read from the provider's uptime |
| `[budget].idle_minutes` | `10` | how long the GPU may sit idle before the guard stops it; `0` disables the idle rule |

`per_run_cap_usd` must not exceed `cap_usd`; both must be greater than zero; `max_hours_per_run` must be
greater than zero; `idle_minutes` must be zero or more. A violation is an error when the config loads,
not a surprise at 3am.

`cap_usd` is the number to set from your wallet. `per_run_cap_usd` is the number that keeps one run from
eating the whole budget: the total cap stops the project, the per-run cap stops a single experiment that
is overrunning. Both are checked on the same poll, and the total cap is reported first when both are
crossed.

## Where the money is checked

| Check | When | What it reads | What it does |
| --- | --- | --- | --- |
| Recorded spend against the total cap | `rentguard up`, before anything is rented | closed rows **plus the accrued cost of the open row** | refuses, exit code 2, unless `--cap N` or `--force` |
| A machine that may still be billing with no ledger row here | `rentguard up`, before renting | every instance on the account whose status is not `EXITED`/`TERMINATED`, and the name that says whose it is | refuses an **unclaimed** machine, naming it (`adopt` it, stop it, or pass `--force` to rent anyway); a name another project tags is a warning, because that project's ledger owns it |
| An open row naming a missing instance | `rentguard up`, before renting | the ledger and the provider | refuses; `down` closes the row |
| The choice, without renting | `rentguard up --dry-run` | the GPU choice and the cap check, with no `create` call | prints what it would request and rents nothing |
| Total cap | every `watch` poll | closed rows plus the live estimate of the open row | stops the instance, closes the row |
| Per-run cap | every `watch` poll | the live estimate of the open row | stops the instance, closes the row |
| Maximum hours | every `watch` poll | the provider's uptime for the instance | stops the instance, closes the row |
| Idle GPU | every `watch` poll | GPU utilisation held at 0 for the idle window | stops the instance, closes the row |
| Already stopped | every `watch` poll | provider status `EXITED` or `TERMINATED` | closes the row, sends no stop |
| Closing the row on `down` | `rentguard down`, after the stop | the instance's own record **and** the account's listing | closes only when both call the machine terminal (`EXITED`, `TERMINATED`, or gone); otherwise exit 1, `action: "still-billing"`, and the row stays **open** so the accrual and the cap keep seeing it |

When more than one stop rule is true, the first in this order is the one reported: already stopped,
total cap, per-run cap, maximum hours, idle.

A **resume** is held to the same rule as a fresh rent: `up` takes a second liveness reading on a machine
it already recorded before calling it a rental, and arms a guard while it waits for the machine to
answer. Both of `up`'s paths end a rental they will not confirm the same way: a machine the account no
longer shows is not a rental, a failed resume stops the machine it revived, and a stop that cannot be
confirmed leaves the row open rather than closed.

## What the guard is

The guard is a loop — a foreground process — that asks three questions on every poll and stops the
machine when any answer says to:

1. **What does the ledger say we have spent?** Closed rows plus the live estimate of the open row. When
   that reaches `cap_usd` or `per_run_cap_usd`, stop.
2. **How long has this machine existed?** The provider's uptime for the instance. When it reaches
   `max_hours_per_run`, stop.
3. **Is the GPU working?** Sampled utilisation, with the idle arithmetic below. When the GPU has been
   idle for `idle_minutes`, stop.

The decision itself is a pure function — `rentguard.guard.decide`, fed by a snapshot — so it can be
tested without spending anything. `watch --once` takes a single reading and reports the decision without
staying alive; `watch` keeps polling until a rule fires or the process is stopped.

**What happens when it fires.** The guard sends a stop, closes the row at the moment of the decision,
and prints the reason (`per-run cap reached ($6.00 of $5.00)`, `idle for 11 min (limit 10 min, gpu util
0.0)`, and so on). It does not terminate the machine, and it does not wait for the provider's meter to
stop. If the stop call fails or the account will not confirm it, the row stays open and the guard keeps
seeing the spend.

**Idle means idle, narrowly.** Three rules keep the idle stop from firing on a working pod: time spent
*watching* is not time *idle*; a sampled GPU utilisation above zero forces idle to zero, because the
provider's per-GPU idle figure is measured against the host; and a pod cannot have been idle longer
than it has existed. When the provider reports no uptime at all, nothing read from it is attributable to
this pod, so it is not a reason to stop it.

**A bounded watch ends protection at its bound.** `watch --max-iterations N` returns at the bound with
`protection_ended_at_bound: true` and `armed: false`, and the CLI exits 2 — a zero exit never means
"still guarded". Make `N × interval` outlast the window you are guarding. There is no detached or
background guard: a supervisor you believe in and that is not running is worse than none.

## The arithmetic

```text
cost_usd = rate_usd_per_hour * (now - started_at) / 3600
```

- `rate_usd_per_hour` starts at the catalogue's price when `up` opens a **provisional** row — the row
  comes *before* the machine, because `create` is the irreversible step — and is corrected from the
  pod's own `cost` field once the provider names the instance. `adopt` takes it directly. What is stored
  on the row is what the arithmetic uses.
- Closing a row writes `wall_clock_hours` and `estimated_usd` from that row's own start and stop
  timestamps. The arithmetic has one home, so the ledger does not depend on the provider's billing
  report.
- Closing twice does not double-count: the second close finds no open row and returns nothing.

## What it does not cover

**It only exists while the guard runs.** `watch` must be running for any of the stop rules to fire.
`status` and `doctor` report the guard heartbeat as a process fact — a running, unmarked pid — so a
guard that ended is never reported as protection, and nothing restarts it for you.

**It overshoots by at least one poll.** The guard checks every `--interval` seconds (60 by default).
Worst case, a run crosses the cap just after a check and is stopped one interval later: at $2.00/h, 60
seconds is $0.03. The row is closed at the moment of the decision, so the ledger records that moment,
not the moment the provider's meter actually stops.

**It is an estimate, not an invoice.** The estimate excludes everything the provider bills that is not
the instance's hourly rate: storage and network volumes, container disk, network egress, image pulls,
minimum billing increments, spot or preemption repricing, taxes, and any credits on the account. The
provider's console is the source of truth for what was actually charged.

**It reads no balance.** No provider here exposes a reliable balance endpoint, and this interface
deliberately has no method for one. The cap is compared against *recorded spend*, never against your
account balance. An instance that was already billing before rentguard saw it is invisible to the cap;
`doctor` is what finds those, and `adopt` is what records them, backdated to the provider's uptime.

**It cannot stop what it does not know about.** An instance on the account with no ledger row *here* is
outside the cap entirely. That is the failure the tool is built around: `doctor` lists those and uses
the machine's name to say what can be done about it — an unclaimed name is a hard error (`adopt` it or
stop it), while a name another project tags is only a warning, because that project's ledger owns it.

**The ledger follows the provider's clock for hours, and its own clock for money.** `max_hours_per_run`
is measured against the uptime the provider reports for the current boot, which a stop/start cycle
resets, while the ledger row keeps its original start time. A cap that depends on uptime is therefore
not a cap on the row's wall clock.

**A malformed ledger line is reported, not silently dropped.** `ledger` and `doctor` count unreadable
lines; `doctor` turns them into an `ERROR ledger-line-unreadable` finding and exits non-zero. The money
inside such a line is still not counted, so the number to trust is the one in the provider's console.

**Two processes can share one ledger, with a lock.** `open` and `close` take an exclusive lock file
(created with `O_EXCL`, broken if older than 60 seconds), so two `up`s in two terminals cannot both open
a row. The lock covers only the check-then-append window, and if the filesystem refuses to create it the
ledger proceeds unlocked rather than refusing to write; in that case the sequential "one open row" check
is all the protection there is.

The full list of limits — the guard's heartbeat semantics, the pull's bounds, the provider's status
vocabulary and the rest — is in [Limitations](limitations.md).

## Prove the decisions without spending

The guard is a pure decision and the money paths run against an in-memory provider with no network and
no spend: [Test the cap without spending](fake.md) shows the cap firing, the stop action and the closed
row without an account. The suite in `tests/` runs the same paths; it is the fake's copy of a provider,
not RunPod's real behaviour, which is why a real rental is still the last word on the arithmetic.
