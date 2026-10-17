# Known failures

Defects, each with a reproduction. A fixed defect stays here as the record: the reproduction is the
evidence that the test which closes it is testing something real, and the fix commit is named. Entries
say where they stand at the revision this file describes, **`1014ade`** (2026-10-16).

Where a commit is cited, it is the commit that fixed the defect; where a test is cited, it passes at
`1014ade` unless the entry says otherwise.

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
green at `a9cffa0`. The rule it leaves behind: a closed row anchors the remainder, so a live machine is
never priced twice.

## The money-direction family — `up` rents a second machine while a live one is already billing (fixed)

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

### The flaky one that came with them (fixed at `be4e7c7`)

`tests/test_doctor_spend_edges.py::VoidedAndFutureRows::test_a_voided_row_contributes_to_neither_number`
asserted that the total `doctor` *prints* and the total it *enforces* agree to four decimal places. The
two figures are two samples of a live quantity — the second is recomputed when the assertion runs — so at
the $2/h rate these cases use (about $0.000556 per second) `places=4` demanded that both samples be taken
within about 0.09 s of each other. It failed three times: in the traced coverage run at `7f52cc3`, twice
untraced at the same revision, and again under the tracer at `a9cffa0` ($4.001065 vs $4.001132, a 6.7e-5
gap — 0.12 s of drift).

**Fixed at `be4e7c7`** (called defect 19 there). The tolerance is now the accrual rather than a fixed
four places, and the failure message names the interval instead of implying a code defect. The reasoning
is in the test: one cent is about 18 s of accrual at that rate, so `places=2` cannot mask what these
cases hunt for — each looks for a whole $2.00 row that was counted, mispriced or invented, two hundred
times the tolerance.

## Defect 16 — a guard can report itself armed while watching a dead instance (fixed)

**Fixed at `a9cffa0`.** The state file is a cache and the open ledger row is the record that survives it,
which is also why `_pod` falls back to that row. A guard armed while a stale `state.json` still named the
previous, already-stopped pod watched that dead id: it returned at once, reported itself armed, and left
the live machine billing unguarded — the exact state the arming call exists to prevent. `cli.py` now
resolves the live instance through the ledger's open row first, and `tests/test_watch_arming.py` (132
lines) is the test that closes it. Note for anyone reading the numbers: `tests/test_pull_pattern_no_match.py`
also calls its own finding "defect 16", from the earlier unmatched-pattern work — the same number, two
different defects.

## Defect 17 — `run` failed a clean command whose pull legitimately delivered nothing (fixed)

**Found** by Cadence during the 2026-10-15 shakedown: `stage()` uploads inputs and produces no
artefacts, so the framed pull answered `patterns-matched-nothing`, and `run` exited non-zero — a clean
command reported as a failed run (37 s, $0.0091, row `20261015T204112Z-d52aa6`).

**Fixed at `042fd21`**, and the change is a distinction rather than a blanket exemption:

- a pull that **matched nothing** is not evidence that a result was lost; it is evidence that there was
  nothing to deliver, which is normal for a command that only reads, stages or validates. `run`'s exit
  code then follows the **command**, and the result still says `pulled: false` with
  `pull_reason: "patterns-matched-nothing"` — visible, never silent;
- a pull that **broke** (a timed-out session, an unreadable frame, a stream that is not an archive) still
  fails the run, because that is the class where files may exist and be lost;
- **`run --require-artefacts`** is the strict caller's opt-in: then any pull that did not deliver fails
  the run, matched-nothing included. `run --help` and the MCP tool description say so.

`down` is deliberately unchanged: it is the last moment before the container is destroyed, so a pull
that delivered nothing stays visible and non-zero there. `tests/test_run_pull_nothing.py` is the test
that closes it.

**One inconsistency left in place and reported rather than fixed at midnight**, from that file's own
docstring: the two pull seams disagree about an *empty* delivery. The tar path names it
`patterns-matched-nothing` and exits 1; a provider that hands its artefacts over in memory matching
nothing is reported `pulled: true, files: []` and exits 0, which is what `tests/test_stop_cannot_hang.py`
and `tests/test_feature_matrix.py` rely on. Since `down`'s non-zero exit is keyed on the pull's exit
code, the two seams cannot be made consistent without changing `down`'s ruled behaviour.

## The stop gate asked `is_alive`, so a crash-looping pod's row was closed under it (fixed)

**Found** by Forge on 2026-10-16 while restructuring `cmd_down` for the B19 re-attack — and recorded as
its own finding rather than as a by-product of it, because the re-attack's findings were about *which
witness* confirms a closure, and this is about *which machines* a closure is attempted for. Gilbert's
ruling on the report that carried it: "name it in the record as your finding".

`cmd_down` decided both questions from one expression:
`live = status not in ("EXITED", "TERMINATED") and is_alive(pod)`. `is_alive` is the **usability** test —
a positive uptime *and* a port — so a pod reporting `RUNNING` with zero uptime and null ports was read as
"nothing to stop". No stop call was sent, `_confirm_stopped` never ran, `still_billing` stayed false, and
the row was **closed**. That is defect 1's shape with a different trigger: the ledger says the window is
over while the machine bills, and the cap and the guard read nothing.

The shape is not hypothetical. It is the state the first pilot attempt sat in for about ten minutes on
2026-10-16 — `RUNNING but not usable yet (uptime=None, ports=NULL) — waiting`, the log that `B19`'s
defect 2 (unwatched window) and `tests/test_up_arms_the_guard.py` were written from — and it is the
machine the guard refuses to arm against. `down` is the command a person reaches for when that pod is
misbehaving, and it was the one command that would have declared the window finished.

**Fixed at `4e7f3d3`.** The two questions are separate now: `billing = status not in
TERMINAL_STATUSES` gates the **stop call and the confirmation**, while `usable = billing and
is_alive(pod)` gates only the pre-stop **pull**, which is the one action that genuinely needs a machine
that answers. A crash-looping pod is stopped, confirmed against both witnesses (the instance's record and
the account's listing), and only then is its row closed.

**The test that closes it.** `tests/test_down_stop_verified.py`,
`AStopThatIsHonoured::test_a_crash_loop_pod_is_stopped_rather_than_having_its_row_closed_under_it`: a
provider whose pod is `status="RUNNING", uptime_seconds=0, ports=None` must appear in
`provider.actions` as `("pod-A", "stop")`, and the row must be closed only once the account agrees the
machine stopped.

## The closure read every status it did not know as "finished" (fixed)

**Found** by Assay's re-attack of the B19 money fixes (`746c1be`, verdict
`tests/_artefacts/b19_reattack_verdict_2026-10-16.txt`, finding 8), 2026-10-16.

`_confirm_stopped`'s own docstring said "EXITED, TERMINATED or gone means it can no longer bill, and
anything else means it still can", and the code did the opposite: `if last not in LIVE_STATUSES:
return True`. A pod the provider reports as `STOPPING` — still shutting down, and still billing until
it is `EXITED` — was therefore read as finished, and so was any transitional or provider-specific
status the tool has never heard of.

The second half is what made it silent: the listing gate asked the same question with the same set
(`_listed_live`), so **both witnesses agreed** on the wrong answer. One set used twice is not two
witnesses, and the whole point of a two-witness closure is that the witnesses can disagree.

Measured with the in-memory fake: a pod `STOPPING` whose stop call is accepted but leaves it shutting
down made `down` exit 0 with `action: "stopped"`, and the row closed under a machine that was still
billing.

**Fixed at `5356d93`.** The set that ends a closure is the **terminal** one: `EXITED`, `TERMINATED`,
or gone from the account. `TERMINAL_STATUSES` replaces `LIVE_STATUSES` in `_confirm_stopped`, in
`cmd_down`'s `billing` gate, and in the listing gate, which is now `_listed_unfinished` — every
instance the account still lists in a status other than terminal keeps the row open — and the refusal
names the status (`pod-A is still STOPPING on a single read`).

**The tests that close it.** `tests/test_down_stop_verified.py::AStatusThatHasNotFinished`: a
`STOPPING` pod keeps its row open and exits non-zero, and a listing that shows `STOPPING` keeps the
row open even when the instance's own record returns nothing.

**Reported, not changed:** the same polarity decides "is this machine live" in `_live` (which `up`'s
untracked-instance refusal reads), in `_pod`'s fallback, and in `guard.state_findings` (which `doctor`
reports as drift). Inverting those would make every unknown status, `ERROR` included, read as billing —
over-reporting rather than over-recording — so it is a ruling of its own rather than a line added here.

## `--uptime-hours=nan` and `inf` crashed instead of being refused (fixed)

**Found** by the same re-attack (finding 9), 2026-10-16.

`--uptime-hours` takes a float, so `nan` and `inf` parse. The refusal that closes finding 5 is
`estimate <= 0`, and **every comparison with NaN is False** while infinity is not `<= 0`, so neither
was refused; the next line, `now_utc() - timedelta(seconds=uptime)`, then raised `ValueError` for NaN
and `OverflowError` for infinity out of `cmd_adopt` — a traceback where a named refusal already
existed.

**Fixed at `5356d93`.** `math.isfinite` joins the guard, and the reason is
`uptime-not-a-length-of-time`, which is what the message already said: `0`, a negative, `nan` and
`inf` are one class — a value that is not a positive finite number of hours, and one that would open a
row recording nothing for a machine that is billing.

**The test that closes it.**
`tests/test_adopt_estimate.py::AnEstimateThatIsNoLengthOfTime::test_nan_and_infinity_are_refused_rather_than_crashing`
(`nan`, `inf`, `-inf`, each refused with exit 2 and no row opened).

## A status in neither set was unfinished for the closure and invisible for a bill (fixed)

**Found** by Assay's attack on the unknown-status boundary (`9e00ec4`, verdict
`tests/_artefacts/b19_unknown_status_boundary_2026-10-16.txt`, findings 10 and 11), 2026-10-16 — the
boundary Forge named in the report that closed findings 8 and 9, attacked rather than left on trust.

Finding 8 put the closure on the **terminal** set while the visibility rule (`_live`, `_pod`'s fallback,
doctor's live list, `guard.state_findings`) still read the **live** set, so the two disagreed about
every status in neither: a `STOPPING` or `ERROR` machine was *unfinished* when a row was closed and
*invisible* when a bill was looked for. Both shapes reproduced with one pod and no ledger row:

```text
$ rentguard doctor
account pods   : 1 total, 0 live
  (nothing is billing)
[exit 0, no finding naming it]

$ rentguard up --purpose ...
[exit 0, action "created", the machine never named]
```

`doctor` is the tool that exists to find unrecorded bills, and it was silent for exactly the machine it
was built to surface. Bounded for `STOPPING` (the stop finishes in seconds), unbounded for `ERROR` if
the provider keeps billing a pod that failed to start.

**Fixed at `86a260a`** by Gilbert's ruling: **terminal is the only state that is not billing;
everything else is.** One predicate, `guard.is_billing(status)` — the terminal set's complement, with a
missing, empty or unknown status counting as billing — replaces the live-set test in all of them.
`up` refuses and names the machine, `doctor` counts and reports it, the drift check fires, `_pod` reuses
a stopping machine the open row names instead of renting beside it, and the guard may be armed against
a machine that may still be charging.

Assay's verdict recommended the softer shape — leave `_live` alone and only *report* such a machine, so
that an `ERROR` pod does not block a rent. Gilbert ruled the other way and accepted the cost: a false
refusal is loud, names the machine and takes one word (`--force`) to override, while a silent billing
machine is neither.

**The cost, declared rather than discovered** (`docs/limitations.md`): an `ERROR` pod that never billed
blocks a rent until it is terminated or `--force` is passed, and a just-stopped machine blocks one for
the seconds it spends `STOPPING`. Measured as transient, not permanent:
`tests/test_status_polarity.py::UpRefusesOverAnythingThatMayBill::test_the_block_ends_when_the_provider_says_exited`
runs the same `up` twice — refused while the pod reports `STOPPING`, renting the moment the provider
reports `EXITED`.

**The tests that close it.** `tests/test_status_polarity.py` (13 tests, 15 subtests): `up` over
`STOPPING` and `ERROR` with the `--force` escape, the two-call sequence, the reuse of a stopping machine
named only by the open row, doctor's count and finding, the drift finding for both statuses with
`EXITED`/`TERMINATED` as controls, and `is_billing` itself.

## `up`'s reuse path reported a crash loop as a rental (fixed)

**Found** by Assay (`eedb07b`, verdict `tests/_artefacts/b19_inverted_ruling_verdict_2026-10-16.txt`,
finding 12), 2026-10-16 — on the one `up` path the B19 work had not reached, reported by Forge one
message earlier and then attacked rather than trusted.

The **create** path requires two consecutive alive readings, stops a machine that is not usable, and
arms a wait guard while `wait_running` runs (defect 2). The **reuse** branch — taken when the state
file or the open row already names a machine — did none of that: it noted the status, kept or opened
the row, printed ssh access and emitted `action: "reused"`, exit 0. Measured at `5277ed8`, with a pod
reporting `RUNNING`, `uptime=0`, `ports=None` (the crash loop `run` and `pull` refuse to touch):

```console
$ rentguard up --purpose again
instance pod-A is already RUNNING
ssh            : ssh fake@127.0.0.1
[action "reused", exit 0]
```

So a machine that was billing with nothing watching it, and that `run` would refuse seconds later, was
reported as a rental. Reachable from a pod that gets stuck after a successful `up`, or from the
defect-10 window (killed between `_write_state` and the second alive read) followed by `up` again.

**Fixed at `d8accac`.** The reuse branch now does what the create path does: one more `provider.get`
and `is_alive` on it, the unusable path when it fails (stop the machine, close the row, exit 1, and a
sentence saying the machine is not usable), and a wait guard armed around a resume's `wait_running` and
released on every way out — including an interrupt. `up`'s contract is now one rule on both paths: a
machine that is not usable is not a rental, and a window that bills while `up` waits is watched.

**The tests that close it.** `tests/test_up_reuse_usability.py` (4 tests): reuse of a crash loop is not
reported, stopped and closed; a resume that never becomes usable is watched (a heartbeat exists and the
guard's stop is in `provider.actions`) and reported as `unusable`; no wait-guard thread survives a
reuse; and the control — reuse of a **usable** machine still succeeds with `action: "reused"` and no
second rental.

## The reuse path's failure exits: a vanished machine was "reused", and a failed resume was left billing (fixed)

**Found** by Assay's attack on `d8accac` (`0d01d57`, verdict
`tests/_artefacts/b19_reuse_fix_verdict_2026-10-16.txt`, findings 13 and 14), 2026-10-16 — both inside
the branch that had just closed finding 12, which is why the fix is a shape rather than two lines.

**Finding 13 — "gone" is the one answer `or` cannot express.** `confirmed = provider.get(id) or pod`
discarded a second reading that returned nothing and then checked, and reported, the stale first one.
With pod-A usable and named by the state, and the account no longer showing it:

```console
$ rentguard up --purpose again
[action "reused", exit 0]
[the row opened for it names pod-A, which the account does not show]
```

**Finding 14 — a resume whose wait fails left the machine it revived billing.** The create path's
`except BaseException` stops the machine and ends the row; the reuse branch had only a `finally` that
released the wait guard, so `wait_running` raising — a provider error, an `ERROR` transition during the
start, or the 12-minute timeout, which is the pilot's own slow-resume shape — propagated with the
machine `RUNNING`, no ledger row and no guard.

**Fixed at `fe38468`, as a shape.** Both of `up`'s paths now end a rental they will not confirm through
**one** function, `_retire_machine`, whose invariant is: *the machine is this root's, and it is either
usable, stopped, or visibly billing in an open row.* It asks the account first (`None` = gone: nothing
is stopped, and only the row that names this instance is closed), stops a machine that is there unless
another root's name claims it, and closes the row only when the account agrees the stop —
`_may_close_the_row`'s two witnesses, the rule `down` follows. An unconfirmed stop leaves the row
**open**, so the accrual and the cap keep seeing it. The create path's unusable exit and the reuse
path's are the same code now, and `_abandon_created` is the one deliberate difference, documented in its
own docstring: the interrupt path closes the row without waiting, because a row that exists is the
guarantee that survives a dying process (defect 10's ruling, kept).

**The tests that close it.** `tests/test_up_reuse_usability.py` (11 tests): the vanished machine on the
reuse path **and** on the create path (the same `or` lived in both), the failed resume that stops what
it revived, the unconfirmable stop that leaves the row open, the unreadable account that leaves it open,
the interrupt path that closes it anyway, another root's machine that is not stopped and whose row is
voided, and the controls. `tests/test_cli_paths.py` moves its assertion to the shared message and now
also pins that the row stays open when the stop was refused.

## A finished guard read as protection, because liveness was an age (fixed)

**Found** by Forge on 2026-10-16 while measuring `up` after the polarity ruling, and ruled on by Gilbert
the same evening: *liveness must be a process fact, not a timestamp fact.*

`status` decided `guard: alive` from the heartbeat's age alone — under five minutes and it said
protection was there. But a guard that has **finished** leaves its last heartbeat behind:
`up` arms a wait guard while it waits for a machine to answer and releases it before returning, and a
`watch` that stops at the cap exits. Measured at `261c6b8`, with the wait made to last half a second so
the guard polled once:

```console
$ rentguard up --purpose probe
[exit 0; its wait guard was armed and released inside the command]
$ rentguard status
guard          : alive (heartbeat 0.0 min ago)
```

Nothing was watching, and `up`'s own closing line says "Until something is watching, only you are" — the
tool contradicting itself inside one invocation. A false protection report is the same class as the
false money reports above: it is the reading that stops someone arming the guard that would have caught
the bill.

**Fixed at `a45c26e`.** The heartbeat now carries the **pid** that wrote it (it used to write a `None`
placeholder) and an `ended` mark, and one reading decides every caller: `alive` requires the pid to be
running **and** the writer not to have marked itself finished. `pid_running` keeps POSIX's three
answers — gone, not-ours-so-alive, running — instead of folding them into one `OSError`; `cmd_watch`
marks itself ended on every exit path and `up`'s wait guard is marked inside its thread, so the mark
lands before the release join returns; and the age is printed on every verdict line without ever being
the verdict. `watch`'s `armed_before` needs all three facts (unmarked, running, fresh within a multiple
of the poll interval), because a crashed guard's pid can be reused by an unrelated process.

**One measured limit**, in [limitations](limitations.md): on Windows an exited process whose handle is
still held elsewhere can be probed successfully, so a **killed** guard can read as `running` until that
handle closes. A clean exit is unaffected — its own mark is the verdict — so the limit touches the
kill/crash case alone, which the freshness rule in `armed_before` covers for the protection claim.

**The tests that close it.** `tests/test_heartbeat_liveness.py` (33 tests, 5 subtests): the probe's
three answers plus the real call against this process and an impossible pid, the writer's pid and mark,
`end_heartbeat` leaving another writer's file alone, the reader's four verdicts, the three-clause armed
claim, `watch` marking itself on return, and the reproduction — `up` then `status` no longer says
`alive`. Two fixtures that hand-wrote the pre-ruling shape now write the shape a guard writes.

### The three seams in that fix, and what closed them (fixed at `1014ade`)

Assay attacked the rule at `75d6d37` (`352a7d9`, verdict
`tests/_artefacts/heartbeat_liveness_attack_2026-10-16.txt`) and found three leaks in the fix itself. Its
controls — the three pid answers, a pid-less payload, the ownership rule, the `armed_before` triple, the
freshness bound and the disclosed Windows caveat — all held.

- **A probe failure that is not an `OSError` crashed the read** (finding 15). POSIX `os.kill` raises
  `OverflowError` for a pid that does not fit a C int, so a heartbeat carrying such a pid — hand-edited,
  truncated, or from a foreign writer — propagated out of `heartbeat_reading` and took `status` and
  `doctor` with it. It reads as gone now, and the probe is a seam on the read path so the POSIX shape is
  testable on this Windows host.
- **The structured `stale` field contradicted the prose** (finding 16, the one that matters for money).
  `stale` was `not alive`, and `alive` is the process fact, so a live, unmarked, 60-minute-old heartbeat
  read `wedged` in prose and `stale: false` in the JSON a script or a Bot gates on — a wedged guard
  reported as nothing to worry about. `stale` is the **age** window again (`GUARD_STALE_MINUTES`, the
  same five minutes the prose uses), so both readings say the same thing.
- **Releasing one guard marked a live second guard finished** (finding 17). Two guards in one process
  share the pid, and two guards for one instance share the instance id, so the pid-keyed mark could not
  tell them apart: the guard that finished marked the shared file and the live one read `finished` until
  its next write. There is now **one heartbeat file per guard** (`heartbeat-<guard id>.json`; the legacy
  `heartbeat.json` is still read), the mark is matched on that id, the readers take the **best** of the
  set — a live unmarked guard wins over a finished one, and when none is alive the newest reading is
  reported, so `finished` and `not running` still show — and the next writer prunes markers older than
  the window. A crashed guard's file is kept: its dead pid is evidence.

All three were red first in `tests/test_heartbeat_liveness.py`, which held 43 test methods at `1014ade`
and 46 with 12 subtests now, after the two added for finding 18 and the dead tier (the "39 tests and 15
subtests" written here when this entry was made was a miscount; 43 is what the file has at that commit),
and five fixtures across four files that expected a guard to have written the single legacy path now ask
the set. The disclosed Windows handle caveat is unchanged and still in [limitations](limitations.md).

### The read against the prune, the ordering's dead tier, and the CI red (fixed at `0e43da0`)

Assay's next attack (`d9cbb1d`, verdict `tests/_artefacts/heartbeat_set_attack_2026-10-16.txt`) found one
seam, in the same window's new code:

- **A heartbeat pruned between the listing and the stat crashed the whole read** (finding 18, fixed at
  `3cbfc15`). `heartbeat_files` sorted the set by `p.stat().st_mtime`, and every `write_heartbeat` prunes
  the set — so a reader can list the directory while another guard removes a file from it, and the bare
  stat raised `FileNotFoundError: [WinError 2] …\heartbeat-ended-guard.json` out of `heartbeat_reading`.
  That is `status` and `doctor` — the two commands a person uses to ask *is anything watching, is anything
  billing* — tracebacking because a file went away. Reproduced with a glob seam that unlinks what it just
  found. The set is built from the files that still answer a stat now: a vanished candidate is skipped,
  and the survivors are ordered newest first. My test pins the set's **contents** — the surviving guard is
  still reported — not only the absence of a crash.

The same attack run could not make the rest of the set rule fail: a crashed guard beside a finished one
reports the newest, and every control held. One reading in it was a code-truth defect rather than a
finding:

- **The ordering's second tier could never be reached** (fixed at `0e43da0`). `alive` *is*
  `running and not ended`, so `_best_first`'s middle branch was dead code and the docstring described a
  tier — "then any live one (a wedged guard is still a process)" — that does not exist. The ordering is
  one live tier, freshest first, then the newest of the rest; that is what the code already did, and the
  tier is deleted rather than covered. `test_a_wedged_live_guard_still_wins_over_a_finished_one` pins the
  tier that remains.

**The published tip was red for a different reason, and CI found it rather than this host** (fixed at
`aa6c721`). Run `38952072673` fails one test in all three jobs at `e230f80`:
`PidProbeSeams::test_things_that_are_not_pids_are_gone` SUBFAILED with `pid=1.5`. `pid_running` truncated
its argument, so `1.5` became pid 1; `os.kill(1, 0)` raises `PermissionError` on Linux — a live process we
do not own, which is the **one** branch that returns True — so a malformed heartbeat read as protection.
Windows raises `EINVAL` for the same call and reads False, which is why the local suite was green. A false
*alive* is the dangerous direction, and CI was the only witness. The probe takes integers only now (no
floats, no booleans), and `test_only_an_integer_is_a_pid` pins the non-integer shapes without a `kill`
seam so the test means the same on both platforms.

The revision of record is `0e43da0`: 680 passed / 8 skipped / 123 subtests in both provider modes, gate
3,489 / 3,430 statements = 98.3%, exit 0.

### Re-attacked and clean (verified at `aed798f`)

Assay re-attacked the three fixes at `34d73a8` (`aed798f` carries the tests and the verdict
`tests/_artefacts/heartbeat_reattack_2026-10-16.txt`): **65 passed, 31 subtests, 0 failed** across its
attack file and `tests/test_heartbeat_liveness.py`, and no new hole found in the ordering, the pruning,
the freshness bound or the ownership rules.

- The integer rule is verified **seam-free**, which its own earlier seam test could not see: the reader's
  own pid reads alive; `1.5`, `2.0`, `0.5`, `"42"`, `b"42"`, `True` and `False` all read gone; and a
  heartbeat carrying a JSON **float pid** reads as *no pid*, so it is not protection rather than a
  truncated pid 1. The CI red taught the shape of the test as much as the code: a case that uses a seam to
  simulate another platform must be paired with a seam-free case, or it passes on one host and fails on
  the other.
- One boundary, read from the code rather than measured, kept deliberately: `heartbeat_files` skips a
  candidate on **any** `OSError`, so a heartbeat whose `stat` is refused for a reason other than "gone" (a
  permission or ACL refusal) is dropped with the pruned ones and the tool reads *NO GUARD* while a guard
  writes. That is the safe direction — under-reporting protection rather than inventing it — and a tool
  cannot report what it cannot stat.
- **Ruled three times, and the third is the shape (Gilbert, 2026-10-16).** The first — *no new machinery* —
  was about refusals; the second put the set in `facts` **and** warned; the third stepped back from the
  schema half: the doctor line as built, one line, never on `status`, never in JSON, *"a schema change for a
  diagnostic is not worth the surface it adds"*. So what ships is the **line** plus the **warn** in
  `doctor`'s existing findings channel:

  ```
  guard heartbeat: alive (pid 26900, heartbeat 0.0 min ago)
  guard set      : 2 heartbeat files — alive (pid 26900, 0 min ago); not running (pid 2147483647, 8 min ago)
    WARN  guard-crashed: a guard is gone and never marked its heartbeat finished (pid 2147483647, …)
  ```

  The `facts` version was built at `266a42d` and **reverted at `00199d6`**: `_guard_report` is the one
  reported reading's facts again, and `_reading_facts`, `guard.readings` and `guard.others` are gone. The
  warn stays because the second ruling asked for it by name and the third did not retract it — it is a
  `Finding`, not a new key, and it never changes `doctor`'s exit code, which turns on errors alone. The
  limits are named in [limitations](limitations.md): the count is readings rather than guards, a reading
  carries no file or guard id, and a crashed guard whose file cannot be `stat`-ed is dropped from the set
  entirely — under-reporting, deliberately.
- **Assay's finding 19 in that attack** (`2ffce2e`) was not only noise, so it did not stay noise. The legacy
  `heartbeat.json` is read for ever and was pruned by nothing, so a migrated state dir reported two readings
  permanently — one live guard plus residue — and the set line overstated the number of guards on every run.
  The **count** being wrong is what made it worth fixing rather than tolerating: the writer now prunes the
  legacy path like any other finished sibling, a finished marker older than the window is not counted at all
  (residue, by the same rule the writer prunes by), and the line says "N heartbeat files" because the count
  is readings. The attack's labelling note is fixed with it: a bool is an `int`, so a hand-edited
  `"pid": true` survived as a pid and every line called it `pid True` while the probe read it as not
  running; `_read_one` rejects booleans and floats now, so the facts and the prose agree on **no pid**.

### The fixture that made the gate red, and the one run that stayed nameless (fixed at `554d079`)

One gate run in sixteen at `aed798f` printed a red internal suite and exited 1 while `TOTAL` sat above the
floor — the gate's own suite-verdict rule (defect 21) doing its job — and my output filter threw the failing
test's name away. Fifteen green runs after it (six in the same suite-then-gate sequence) left one
fingerprint: **`cli.py` 1708 against the usual 1706**, two extra lines, exactly the two lines of
`_confirm_stopped`'s `except ProviderError` branch.

The name came back the ordinary way: the flake stopped being rare, failing 3 runs in 4 in one batch and
then 1 in 10 under the tracer, and it is
`tests/test_money_defects.py::UpNeedsMoreThanOneAliveSample::test_up_does_not_report_success_after_one_alive_sample`.
**The mechanism was the fixture, not the tool.** `up` arms a wait guard; that guard polls `get` from its
own thread; and the test's scripted transport kept **one sample cursor for every thread**, so the guard's
reads consumed the script. In the bad interleaving the list ran out before the stop confirmation, the
transport answered 500, `_confirm_stopped` could not agree with an unreadable witness, and `up` left the
row **open** — the correct, conservative direction, and the opposite of what the case asserts. The test was
measuring the interleaving. Fixed in the fixture at `554d079`: only the main thread advances the script, a
guard-thread read returns the machine's latest main-thread reading, the last sample holds once the list
ends, and the tail is **EXITED** rather than a 500 — the docstring always said "finally it is gone" and the
fixture never implemented a witness the account could agree with. **1 failure in 10** traced runs before,
**0 in 20** after. The lesson kept: a test that shares mutable script state across threads measures its own
scheduling.

The revision of record is `00199d6`: 691 passed / 8 skipped / 133 subtests in both provider modes, gate
3,518 / 3,459 statements = 98.3%, exit 0.

## The artefact transport: a bound that was a constant, and a pattern that re-sent everything (fixed)

Measured 2026-10-17 in the grid window — Cadence's numbers, verified against the ledger: **sixteen completed
trainings turned into one usable run**, in a **$14.693806** window. Two defects in `pull`, both of them
silent in the direction that loses data:

**The bound was a legacy constant.** `[artefacts] pull_timeout_s` defaulted to 120 s while a run directory
is ~700 MB (527 MB cache + 172 MB step-named checkpoints + the rest), so one run's pull sat on the edge and
the window logged **26 timeouts**. A timeout is a failure the caller must act on, and the bound was answering
a question nobody had asked: *how long may a transfer take* — with a number unrelated to *how much it
carries*.

**The pattern re-archived everything ever written.** `grok/runs/*` matched every run directory, so each
pull re-sent every earlier run as well: each pull was larger than the last, the failure compounded, and the
first pull of the window succeeded only because one directory existed.

**The fix is one design: measure, then transfer.** An **inventory** command runs before the archive — a
framed text answer listing, per matched entry, its pattern index, kind (`d`/`f`), mtime, size and path
(`stat -c`, `du -sb`, POSIX `sh`: the same family as the GNU `tar` the archive already needed). From it:

- **One run per pull.** When a pattern matches directories, only the **newest** entry — the run just
  finished — goes into the archive, and the output names how many older runs were left behind and that
  `rentguard pull --all` fetches them. A pattern of **files** carries every match, because it already names
  one run's outputs rather than a collection of runs.
- **The bound is sized from that volume**: `max(pull_timeout_s, volume / pull_bytes_per_second)` — the
  configured 120 s stays as the **floor** for a small pull, and the new `pull_bytes_per_second` (default
  1 MB/s, validated positive) is the assumed throughput. A 700 MB run now gets ~700 s instead of 120.
- **A timeout is a named failure**: `ssh-timeout` with `volume_bytes`, `timeout_s`, `bytes_per_second`,
  `selected`/`skipped`, and the next step (`down --keep-running` keeps the machine for a retry). `pull`,
  `run` and `down` all exit non-zero on it — the timeout can no longer be a log line a window closes over.
- **An unreadable inventory is a named failure** (`inventory-unreadable`, or `ssh-timeout` when the
  measurement itself hung), and nothing is transferred. Falling back to archiving the root when the
  measurement fails would be the defect itself, silently.
- The archive carries **exact, shell-quoted paths** from the measurement, so the remote shell can no longer
  re-expand the configured glob.

**Red first** in `tests/test_pull_transport.py` (21 tests): the volume-derived bound and its floor, the
timeout's named failure with the volume and the next step, one-run selection with the skipped count,
`--all`, file and mixed patterns, an empty inventory starting no transfer, an unreadable inventory starting
none, an inventory that hangs or exits non-zero, the protocol's five malformed-line shapes, the size
formatter and the config's two validations, and a real child killed by `_ssh_capture`'s bound. The existing
pull tests' fake ssh answers the inventory too (`tests/_pull_ssh.py`); a fake that answered only the
archive would now fail as `inventory-unreadable` — the safe direction, but not what those tests are about.

**Boundaries kept named rather than fixed.** The inventory assumes a Linux container (`stat -c`, `du -sb`,
POSIX `sh`); a machine that cannot answer names the failure instead of transferring. Entry paths are
tab-separated, so a filename containing a tab or newline is not supported by the inventory. And the
"newest directory is the run just finished" rule is the tool's reading of the shape `grok/runs/*` declares:
a config whose directories are not runs would pull only the newest — `rentguard pull --all` is the
backstop, and the `pull` line names what it skipped.

**The ratchet, measured.** The first gate run after this change came in at **97.7%, under the 98.0% floor**
(`cli.py` 97.0%, `config.py` 96.5%): the new code added failure branches faster than its tests reached them.
It was closed with **tests, not by moving the floor** — the parser's five malformed-line shapes, the
inventory timeout and non-zero exit, a real child killed by the measure's own bound, the config's two
validations — and the gate then read 98.2%. That is the ratchet doing its job: a floor that is argued down
when a change is inconvenient is not a floor.

The revision of record is `6f5b16d`: 712 passed / 8 skipped / 133 subtests in both provider modes, gate
3,670 / 3,603 statements = 98.2%, exit 0.

## The proxy route's archive command named characters, not paths (fixed at `0ebf95b`)

Assay's re-attack on the transport fix (2026-10-17) found two defects in the fix itself, both red first:

- **The PTY/proxy archive command carried characters, not paths (severe).** `_pull_remote_command` joined
  the selected paths and `_pty_pull_command` joined that string **again**, so tar received
  `g r o k / r u n s / r u n - 1 6`: measured with a real `tar`, `rc=1` "Couldn't visit directory" while
  the direct route gave `rc=0`. Every proxied pull would have delivered nothing, and `down` stops the
  machine by default after a failed pull — one move would have lost a proxied window's artefacts *and* its
  machine. Introduced at `6f5b16d`; at `555d4ff` the PTY builder received the pattern list. The fix is one
  join, in the builder that owns the layering.
- **The test that should have caught it only inspected the string.** `tests/test_pull_command_runs.py`
  now **executes** the constructed command in a real POSIX shell against a real `tar` on a scratch tree —
  both routes, the archive's own members checked, a member whose name contains a space, and a member
  named `-cache` (the `--` separator) — and a shell-free property test pins the same statement (the path
  reaches the command once, as one word), so it is checkable where the shell cannot run. The executed
  tests skip here and run on the CI runners.
- **`_PATTERN_SAFE` ended in `$`**, which matches before a final newline, so a TOML `"\n"` escape in
  `pull`/`run_patterns` passed validation and reached the remote inventory command, where a newline is a
  second command. `fullmatch` now.

**The proxy route's hardware proof has been outstanding for weeks** because no proxy-capable card could be
rented — and the software would have failed first. It is proven in software now: the archive command is
executed against a real `tar` through a real shell before anyone spends money proving it on hardware.

The revision of record is `0ebf95b`: 751 passed / 11 skipped / 140 subtests in both provider modes, gate
3,765 / 3,701 statements = 98.3%, exit 0.
