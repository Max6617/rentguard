# Known failures

Open defects, with a reproduction. A defect leaves this file when a test covers it and the test passes.

## F-4 — `up --dry-run` rents the machine it says it did not rent

**Open at `2ebe660`.** No test covers it.

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
absent or empty and append no ledger row, while still printing the GPU it would have requested. It must
fail on `2ebe660` before it passes.

## F-1 — an open ledger row with no instance reports zero spend, and `status` says nothing is renting

**Confirmed** 2026-10-14 by hand, in a temp root, no network writes and no spend.

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

**Found** 2026-10-14, on the live account.

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
