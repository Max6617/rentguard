# Known failures

Open defects, with a reproduction. A defect leaves this file when a test covers it and the test passes.

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
