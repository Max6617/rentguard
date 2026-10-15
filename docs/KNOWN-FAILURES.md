# Known failures

Defects, each with a reproduction. A fixed defect stays here as the record: the reproduction is the
evidence that the test which closes it is testing something real, and the fix commit is named. Entries
say where they stand at the revision this file describes, **`ddd79c9`** (2026-10-15).

Where a commit is cited, it is the commit that fixed the defect; where a test is cited, it passes at
`ddd79c9` unless the entry says otherwise.

## F-5 — the guard can stop a freshly started machine as "idle" (fixed)

**Fixed** at `d5fdaea`: provider idle is bounded by the pod's own uptime and ignored when uptime is
unknown. Covered by `tests/test_money_defects.py` — `test_a_thirty_second_old_machine_is_not_stopped_as_idle`,
`test_a_machine_idle_past_the_window_is_still_stopped`, `test_the_locally_observed_idle_counter_still_stops`.

`guard.snapshot` takes the idle time from the provider's per-GPU `idle` seconds
(`_idle_minutes_since_boot`) and `decide` compares `max(idle_for_minutes, idle_since_start_minutes,
max_idle_minutes)` against `idle_minutes`. Nothing clamps the provider's idle figure to the machine's own
uptime, so a pod whose provider-reported GPU idle exceeds the window is stopped at its first sample —
even seconds after it started. On a paid run that destroys the work it was rented for.

Reproduction on the pure decision path, no network and no spend: a pod 32 seconds old whose provider
reports 30 minutes of GPU idle, with `idle_minutes = 5`:

```text
pod uptime                     : 32 s
provider-reported GPU idle     : 30.0 min
effective idle used by decide  : 30.0 min
idle_minutes from config       : 5.0 min
decide() -> 'idle for 30 min (limit 5 min, gpu util 0.0)'
```

Observed on a real pod as `idle for 30 min (limit 5 min)` on a machine that was 32 seconds old.

**Why it matters.** The idle rule exists to stop a machine nobody is using. Firing it on a machine that
has barely started turns the safety net into the accident, and it is the one path where the guard
spends money by acting.

**Required behaviour.** Clamp the provider's idle figure to the machine's uptime before it can decide a
stop, and ignore an idle reading larger than the uptime. The locally observed zero-utilisation counter
already has that shape; the provider figure must not be able to outvote it.

**The test that closes this.** Snapshot a pod with `uptime = 32` and `gpus[0].idle = 1800`, and assert
`decide(...)` is `None`; a second case with real idle past the window must still stop. It failed on
`af1ba5a` and passes at `458e128`.

## F-4 — `up --dry-run` rents the machine it says it did not rent (fixed)

**Fixed** at `ce69cea`: `dry_run` is decided before any `create` call. The test asserts on the provider
call rather than on the sentence that used to lie (`f7bf556`), in `tests/test_money_defects.py` —
`test_dry_run_makes_zero_create_calls`, with `test_a_real_up_does_make_the_create_call` as the control.

`cmd_up` calls `provider.create` inside the preference-retry loop *before* it checks `dry_run`, so the
flag only changes the sentence printed afterwards. Against a provider with stock, `--dry-run` rents a
machine, prints "nothing was rented and nothing was spent", and leaves no ledger row and no state
pointer — a billing machine that nothing records.

Reproduction, free, with the fake provider, in a fresh root whose `[runpod].gpu_preference` names
`"Fake GPU 24GB"`:

```console
$ RENTGUARD_PROVIDER=fake rentguard up --purpose "dry check" --dry-run
requesting Fake GPU 24GB
dry run: would request Fake GPU 24GB; nothing was rented and nothing was spent
[exit 0]

$ cat .rentguard/fake-state.json
  "instances": [
    {
      "id": "fake-1",
      "status": "PROVISIONING",
      ...

$ rentguard ledger
no runs recorded
```

One `create`, one instance on the account, no ledger row.

**Why it matters.** `--dry-run` is the command a careful person reaches for *before* spending, and the
quickstart and cap pages point at it as the way to check the choice without renting. A flag whose whole
purpose is "do not spend" is the worst place to spend by accident.

**Required behaviour.** Check `dry_run` before any `create` call. The GPU choice belongs to
`provider.choose_gpu`, which asks the catalog and rents nothing; the retry-on-refusal loop is a `create`
concern and must be skipped when the flag is set. A refused preference during a dry run should be
reported as a refusal, not retried into a rental.

**The test that closes this.** With the fake provider, `up --dry-run` must leave `fake-state.json`
absent or empty and append no ledger row, while still printing the GPU it would have requested. It
failed on `af1ba5a` and passes at `458e128`.

## F-1 — an open ledger row with no instance reports zero spend, and `status` says nothing is renting

**Fixed.** Confirmed 2026-10-14 by hand in a temp root, no network writes and no spend, and now covered
by `tests/test_known_failures.py` (4 tests) and `tests/test_drift.py` (3 tests), green at `458e128`;
the working output is the worked example in [Limitations](limitations.md).

Setup: one ledger row, `instance_id` that does not exist, `started_at` three hours ago,
`stopped_at` none.

```
$ rentguard status
no instance recorded
spend so far   : $0.00 of $25.00

$ rentguard doctor
spend recorded : $0.00 of a $25.00 cap (1 run(s))
open run       : drift-demo
account pods   : 5 total, 0 live
  (nothing is billing)

$ rentguard ledger
drift-demo  ...  OPEN  -  open row whose instance does not exist
total                                   0.00   0.0000
```

**Why it matters.** `status` is what a user or a Bot calls to decide whether anything is billing. It
answered "no instance, $0.00" while an open row existed. A run whose instance has vanished is exactly
when money is most likely to be hiding — a crash, a provider-side termination, a click in someone
else's console — and the tool's answer is silence. Open rows also count as zero spend, so the cap
check reads low.

**Root cause.** `cli._pod()` clears `state.instance_id` when the provider returns 404, without
closing the ledger row or marking it unresolved. The only link back to the open row is destroyed by a
silent state mutation.

**Required behaviour.**

1. `status` reports the open row and its accrued cost even when no instance is recorded, and states
   that the instance is gone or unreachable. An open row with no visible instance is the headline.
2. `doctor` flags both directions of drift: an open row with no instance, and a recorded instance
   with no open row.
3. The 404 path must not erase the instance id without either closing the row or marking it
   unresolved.

**The test that closes this.** With the fake provider: open a run against an instance the fake
reports as missing, then assert that `status` surfaces the row and a non-zero accrued cost, that
`doctor` names the drift, and that the instance id is still recoverable afterwards. It must fail on
the current code before it passes.

## F-3 — two state dirs for one account means two ledgers, and one of them is invisible

**Found** 2026-10-14 on the live account. **Partly addressed at `458e128`.** Item 2 is in: when the
config names nothing, `cli._instance_name` derives `rentguard-<8 hex of the project root>`, so two state
dirs rent under different names and a second ledger is visible from the provider's side. Item 1 is not:
`doctor` still prints the state dir on the third line, after the version and the config path.

`doctor` reported a live instance with no ledger row: `mtsd5jl89a520b`, RTX A6000 at $0.59/h, named
`rentguard-workbench`. That name is the **default**. The project's config sets `rentguard-m2` and a
GPU preference of 4090/A4500/A4000, and the A6000 appears only in the built-in defaults — so the
instance was rented by a process that never read the project config, because it ran with a different
working directory and therefore a different state dir and a different `runs.jsonl`.

The guard worked: `doctor` found the untracked instance, `up` refused to rent a second machine while
it was live, and the message named the instance and both remedies. What no part of the tool prevents
is the split ledger itself.

**Why it matters.** Spend is recorded against a cap per state dir. Two state dirs means two caps, two
totals, and a real balance that neither of them knows. This is the failure `config.py`'s own comment
warns about, and it happened to the people who wrote it.

**Required behaviour.**

1. `doctor` prints the state dir it is using as the first line, not the fourth, so a mismatch is
   obvious rather than something you notice afterwards.
2. Better, and stronger: **stamp the project on the instance at creation** — derive a short tag from
   the state dir and put it in the instance name or a provider tag — so a second ledger is visible
   from the provider side even to a process that has never seen the first one.
3. Document it in the quickstart: one project, one `RENTGUARD_ROOT`, and say what breaks when a bot or
   a shell runs from somewhere else.

**The test that closes this.** Two `Config` objects with different `state_dir` values, one instance
created through the first, then assert that the second reports the instance as tracked-but-foreign
rather than simply absent.

## F-6 — network volumes are invisible and bill while nothing runs (fixed)

**Fixed** at `fb867b6`, with the payload-parsing correction at `f4225ff`. Covered by
`tests/test_storage.py` (5 tests). Verified live against the real account at 16:47 on 2026-10-14.

`doctor` used to list pods and nothing else, so an ongoing **storage** cost had no ledger row and no
warning. The account carried `small-agents-vol`: 150 GB in US-IL-1, **$10.50/month, billed whether or
not a pod exists**. It ran for three days without appearing in any report, and cost more than every
rental in this project's ledger combined. Instance drift had a detector; storage drift had none.

The report now names the volume, its size, its region, its monthly cost, and what is (not) using it.
This is the current code against the live payload's numbers — the account no longer lists the volume, so
it is a reproduction rather than the 16:47 transcript:

```console
network volumes: 1  ($10.50/month, billed while nothing runs)
  small-agents-vol  150 GB  US-IL-1  $10.50/month  in the config of STOPPED pod pod-old — nothing is using it and it bills anyway

  WARN  storage-unattached: volume 'small-agents-vol' (150 GB, US-IL-1) costs $10.50/month and no running instance uses it; delete it if it is not wanted
  WARN  storage-not-in-ledger: $10.50/month of network storage is NOT in the ledger total, which covers rentals only
```

`ledger` states the same exclusion itself: `storage: NOT included above — network volumes bill monthly
while nothing runs`.

**The volume is deleted.** Deleted on the user's instruction, executed by Forge, and confirmed by a
direct, read-only `GET /v2/network-volumes` at 16:54 on 2026-10-14 (an empty list) and by `doctor`
reading `network volumes: 0  ($0.00/month, billed while nothing runs)`. The pod that referenced it,
`52n9hq6atcwa18` (`small-agents-workbench`), was `EXITED` and **not billing**; it was terminated only
because the provider refuses the delete while any pod still points at the volume. The sequence:

```text
BEFORE: [{"dataCenter":"US-IL-1","id":"ii4wouejo8","name":"small-agents-vol","size":150,"type":"STANDARD"}]
pods referencing it: 52n9hq6atcwa18 (small-agents-workbench, EXITED)
DELETE /v2/network-volumes/ii4wouejo8 -> 400 "You must remove this network volume from all pods before deleting it."
terminate 52n9hq6atcwa18 (EXITED, not billing)
DELETE /v2/network-volumes/ii4wouejo8 -> 204 No Content
AFTER: []
```

**Two provider facts learned doing it, both measured:**

- **RunPod refuses to delete a volume while any pod references it, including an `EXITED` one.**
  `DELETE /v2/network-volumes/ii4wouejo8` returned
  `400 {"detail":"You must remove this network volume from all pods before deleting it."}`, so the
  referencing pod has to be **terminated** first, not merely stopped. A stopped pod's stale mount blocks
  it.
- **The endpoint is `GET /v2/network-volumes` on `api.runpod.io`**, with response key `networkVolumes`
  and region field `dataCenter`. `rest.runpod.io/v1/networkvolumes` is 404. The schema matters: the
  first `volumes()` read `volumes`/`dataCenterId`, returned nothing against the live API, and its
  invented fixture passed anyway — which is why the fixture in `tests/test_storage.py` is quoted from a
  live response.

**Related:** the network-volume *path* (`[runpod].volume_id` attached at `up`) has never been exercised
and now cannot be on this account — with the only volume deleted there is nothing left to attach.
Recorded as **dropped for lack of a volume, not untested**: the transport seam is unit-covered
(`da51005`), and `CHARTER.md` W3 says the same.

## Defect 15 — a closed row was priced a second time from the machine's own uptime (fixed)

**Found** by Cadence's guard log (`guard-watch-batchA-2.log`). On the sample *after* the batch-A row had
closed — the machine already `EXITED`, stopped by the driver's own `down` — the guard printed

```text
run $5.941 (unrecorded) total $15.581/$15.00
```

a transient total **above** a cap that was never reached. The row had been closed, so its money was
already in the ledger; the guard read the instance as unrecorded and added a fresh accrued estimate on
top of it. Nothing was live, so no stop ran. On a live machine, that total is exactly what the stop
decision runs on.

**Fixed in two steps.** `cdb2fba` made a closed row the ledger's figure for a machine in a terminal
status, and no reading may exceed the ledger's own closed + accruing (`tests/test_guard_closed_row.py`).
`13d7dc7` closed the half that was still open: the fix keyed on `status in TERMINAL_STATUSES`, so a
*live* status with a closed row for that instance fell through to the unrecorded branch and was priced
from the machine's whole uptime. The attack that pinned it —
`tests/test_guard_closed_row_attack.py`, a `PROVISIONING` machine reading $14.00 for an $8.00 bill — is
green at `ddd79c9`. The rule it leaves behind: a closed row anchors the remainder, so a live machine is
never priced twice.

## Defect 16 — `up` rents a second machine while a live one is already billing (fixed)

Two shapes, both red-first at `7f52cc3` in `tests/test_money_direction_shapes.py`, both severe because
the first machine keeps billing with no open row and no guard — a bill the cap cannot see:

1. **A live machine named by the *open* row is superseded.** With `state.json` lost (a shape the code
   says out loud it survives), `up` closed that machine's row and rented a second one, then reported
   success.
2. **A live machine named only by a *closed* row is exempt.** It appears in no open row, so `up`'s
   untracked-instance refusal did not fire and a second machine was rented while the first billed.

**Fixed at `3287a4f`**: a live machine the ledger names is reused, and a closed row does not account for
it. The related shapes that came with it are fixed at `946ba28` — a price nobody read is never printed
as a number, and a reused row starts where the last one ended (`tests/test_pricing_unknown.py`, `tests/
test_up_reuse_after_close.py`, `tests/test_state_lost_ledger_fallback.py`).

### The flaky one that came with them

`tests/test_doctor_spend_edges.py::VoidedAndFutureRows::test_a_voided_row_contributes_to_neither_number`
asserts that the total `doctor` *prints* and the total it *enforces* agree to four decimal places. The
two figures are computed from the wall clock at different instants, so a slow run can widen the gap
past the tolerance: in a traced coverage run at `7f52cc3` it failed ($4.001065 vs $4.001132), while two
untraced suite runs at the same revision passed. Red when the machine is busy, green when it is not —
recorded here rather than called a fix, because a wall-clock-dependent assertion can fail for a
stranger too.
