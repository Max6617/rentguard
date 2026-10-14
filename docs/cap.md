# What the cap guarantees, and what it does not

A cap is a dollar number in `rentguard.toml`. It is the reason this tool exists, so it is worth
stating exactly what it does.

**The cap only exists while `rentguard watch` is running.** It is not a property of the machine and not
something the provider enforces; it is a property of the guard. `watch` is a foreground process that
nothing starts for you. If no guard is running, there is no cap: the instance bills until someone
notices. That is the failure this tool was built for, and it is the honest limit of everything below.

**The guard has a known defect.** It can stop a freshly started machine: `watch` compares the provider's
per-GPU idle seconds against `idle_minutes` without clamping them to the machine's own uptime, so a pod
can be stopped at its first sample. Reproduced on the pure decision path (no network, no spend) with the
provider reporting 30 minutes of GPU idle on a machine 32 seconds old:

```text
pod uptime                     : 32 s
provider-reported GPU idle     : 30.0 min
effective idle used by decide  : 30.0 min
idle_minutes from config       : 5.0 min
decide() -> 'idle for 30 min (limit 5 min, gpu util 0.0)'
```

Until this is fixed, use `idle_minutes` with care or run `watch` with a large interval. It is filed in
[KNOWN-FAILURES](KNOWN-FAILURES.md), and the wording here comes out when the fix lands with a test.

**The short version.** While `rentguard watch` is running, it stops the instance when the estimate for
spend crosses the cap. Everything below is about what that does and does not cover.

## Where the money is checked

| Check | When | What it reads | What it does |
| --- | --- | --- | --- |
| Recorded spend against the cap | `rentguard up`, before anything is rented | closed rows **plus the accrued cost of the open row** | refuses, exit code 2, unless `--cap N` or `--force` |
| A live instance with no ledger row | `rentguard up`, before renting | live instances on the account | refuses; `adopt` it or stop it |
| An open row naming a missing instance | `rentguard up`, before renting | the ledger and the provider | refuses; `down` closes the row |
| The choice, without renting | `rentguard up --dry-run` | **broken at `af1ba5a`** — `cmd_up` calls `create` before it checks the flag, so it can rent a machine and still print "nothing was rented" | see [limitations](limitations.md); do not use |
| Total cap | every `watch` poll | closed rows plus the live estimate of the open row | stops the instance, closes the row |
| Per-run cap | every `watch` poll | the live estimate of the open row | stops the instance, closes the row |
| Maximum hours | every `watch` poll | the provider's uptime for the instance | stops the instance, closes the row |
| Idle GPU | every `watch` poll | GPU utilisation held at 0 for the idle window | stops the instance, closes the row |
| Already stopped | every `watch` poll | provider status `EXITED` or `TERMINATED` | closes the row, sends no stop |

When more than one is true, the first one in this order is the one reported: already stopped, total
cap, per-run cap, maximum hours, idle.

## The arithmetic

```
cost_usd = rate_usd_per_hour * (now - started_at) / 3600
```

- `rate_usd_per_hour` is taken from the provider's own `cost` field when the row is opened, or when
  an existing instance is adopted, and is then stored on the row.
- Closing a row writes `wall_clock_hours` and `estimated_usd` from that row's own start and stop
  timestamps. The arithmetic has one home, so the ledger does not depend on the provider's billing
  report.
- Closing twice does not double-count: the second close finds no open row and returns nothing.

## What it does not cover

**It only exists while the guard runs.** `watch` must be running for any of the rows above to fire.
`status` prints the heartbeat age and says `NO HEARTBEAT — nothing is watching this instance` or
`the guard is probably dead` after five minutes of silence. `doctor` prints the same. Nobody restarts
the guard for you.

**It overshoots by at least one poll.** The guard checks every `--interval` seconds (60 by default).
Worst case, a run crosses the cap just after a check and is stopped one interval later: at $2.00/h,
60 seconds is $0.03. The row is closed at the moment of the decision, so the ledger records that
moment, not the moment the provider's meter actually stops.

**It is an estimate, not an invoice.** The estimate excludes everything the provider bills that is
not the instance's hourly rate: storage and network volumes, container disk, network egress, image
pulls, minimum billing increments, spot or preemption repricing, taxes, and any credits on the
account. The provider's console is the source of truth for what was actually charged.

**It reads no balance.** No provider here exposes a reliable balance endpoint, and this interface
deliberately has no method for one. The cap is compared against *recorded spend*, never against your
account balance. An instance that was already billing before rentguard saw it is invisible to the
cap; `doctor` is what finds those, and `adopt` is what records them (backdated to the provider's
uptime, so the money already spent is not hidden).

**Recorded totals exclude the open row.** `ledger` and `doctor` sum closed rows only, so they read
low while something is running. `status` prints `this run` and `all runs` including the live estimate,
and prints a `DRIFT` headline when a row is open and the provider shows no instance.

**A malformed ledger line is reported, not silently dropped.** `ledger` and `doctor` count unreadable
lines; `doctor` turns them into an `ERROR ledger-line-unreadable` finding and exits non-zero. The
money inside such a line is still not counted, so the number to trust is the one in the provider's
console.

**`doctor` exits non-zero when it finds an error.** Any finding with severity `error` — no API key, an
unreadable ledger line, drift in either direction, a live instance with no row — makes `doctor` exit 1
after printing `ERROR <code>: <message>` lines. A script can gate on it.

**Maximum hours follow the provider's uptime, which a restart resets.** `max_hours_per_run` is
measured against the uptime the provider reports for the current boot. Stopping and starting the same
instance resets that number, while the ledger row keeps its original start time. A cap that depends on
uptime is therefore not a cap on the row's wall clock.

**It stops, it does not terminate.** `watch` sends `stop`. Whether a stopped machine still bills for
its disk or volume is a provider question this ledger does not model.

**It cannot stop what it does not know about.** An instance on the account with no ledger row is
outside the cap entirely. That is the failure the tool is built around: `doctor` lists live instances
with no row and warns, and `adopt` gives one a row.

**Two processes can share one ledger, with a lock.** `open` and `close` take an exclusive lock file
(created with `O_EXCL`, broken if older than 60 seconds), so two `up`s in two terminals cannot both
open a row. The lock covers only the check-then-append window, and if the filesystem refuses to create
it the ledger proceeds unlocked rather than refusing to write; in that case the sequential
"one open row" check is all the protection there is.

## Prove the decision without spending

The guard is a pure decision (`rentguard.guard.decide`) fed by a snapshot, and the money paths run
against an in-memory provider with no network and no spend. At commit `af1ba5a`, `python -m pytest
tests -q` from the checkout:

| Suite | Tests | What it covers |
| --- | --- | --- |
| `test_money_paths.py` | 33 | the cap, the per-run cap, idle, drift, artefacts, provider refusals |
| `test_rentguard.py` | 16 | ledger arithmetic and guard decisions |
| `test_artefacts_mcp_guards.py` | 5 | artefacts, the MCP tool surface, guard locks |
| `test_init_config.py` | 4 | the generated config is valid TOML and the keys are the ones read |
| `test_known_failures.py` | 4 | the drift defect, as a regression guard |
| `test_mcp_structured.py` | 4 | every tool returns `structuredContent` alongside its text |
| `test_down_keep_running.py` | 3 | the failed-pull rule, and the `--keep-running` opt-in |
| `test_drift.py` | 3 | ledger and account drift |
| `test_fake_span.py` | 2 | the fake's account persisting across processes |

74 tests, 0 failures, no network, no spend. What the suite proves is the fake's copy of a provider, not
RunPod's real behaviour — that is what M2 is for. The `--dry-run` defect above is not covered by any of
these tests.

Open defects are listed with reproductions in [KNOWN-FAILURES.md](KNOWN-FAILURES.md); a defect leaves
that file when a test covers it and passes.
