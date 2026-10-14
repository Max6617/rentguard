# Charter — rentguard

The one file a human reads. Drafted from the opening conversation on 2026-10-14, approved as a
by-hand test of the charter idea. Requirements carry how they are honoured; anything with neither a
mechanism nor a check is a wish.

## The frozen original

Verbatim, never edited:

> Take rentguard from v0.1 to a publishable release with a dedicated Bot team, deciding all design
> questions autonomously: complete the CLI and MCP surface, harden the money-critical paths with
> tests, prove it end-to-end on a real rented GPU within a small budget, and write the documentation.
> Do not publish anything publicly (no public repo, no PyPI, no announcement) and do not ask the user
> anything else — the publication approval is the only question that goes back to them.

## What it is for

Renting a GPU without losing money or results: a hard spend cap, a per-run cost ledger, artefacts
pulled as each run finishes, and a machine that stops itself.

## What it is good at — and what it is not for

Good at: one small, boring, correct rental path on RunPod, with the books always recountable by hand.
Not for: fleet management, multi-provider scheduling, or anything that needs a server. Not a job
runner; it starts machines and keeps the receipts.

## Requirements

Re-measured at `74b9f7a` (clean worktree, 189 passed / 0 failed, 45 subtests; coverage by
`tools/coverage.py`).

| # | requirement | enforced-by | checked-by | state |
|---|---|---|---|---|
| R1 | CLI surface complete — ten commands | code: the parser | — | **met** |
| R2 | MCP surface complete | — | — | **met** — eleven tools, incl. `watch`, `watch_once`, `init` (`590c58e`) |
| R3 | Money-critical paths hardened with tests | 189 tests at `74b9f7a`, fake provider | the clean-worktree suite run | **met** (logic only) |
| R4 | The guard proven end to end on a real machine | — | — | **NOT MET** — `watch` has never run against a rented pod; the three M2 rentals never armed it |
| R5 | End-to-end proof on a real rented GPU | 3 rentals, $0.0264 | ledger reconciled by hand | **met** — incl. the real A4000→4090 fallback |
| R6 | Documentation | — | read through | **met** — README covers `adopt`; the MCP page documents all eleven tools |
| R7 | Never reports an account balance | code: the provider interface has no such method | — | **met** |
| R8 | Never publishes anything | — | — | **wish** — discipline, not a control |
| R9 | The cap protects the user | only while `watch` runs | — | **wish** — see below |
| R10 | 94% of the code and 100% of the features exercised | `tools/coverage.py`, ratchet floor 89% | the clean-worktree run | **half met** — 100% of the features green (feature matrix: 10 commands, 11 MCP tools); 90.2% of the code, 94% still to come |

## The wishes

**W1 — the cap only exists while the guard is running.** `watch` is a foreground process that nothing
starts. If it is not running, there is no cap: the instance bills until someone notices. This is
precisely the failure the tool was built for, and the 16.4-hour pod it caught was exactly that. R2 is
now closed — a Bot can arm a bounded guard over MCP (`watch_once`, `watch`), and every return states
whether protection is still armed — but a persistent guard would need a detached daemon, which was
deliberately not built. A bounded, honest guard over a supervisor nobody watches.

**W2 — "do not publish" is a promise, not a lock.** Nothing in the repository prevents a push to a
public remote. Given R8 matters to the user, "no remote configured" is a fact today and could stop
being one silently.

**W3 — the network-volume path is unexercised, and now unexercisable.** The account's
only volume (`small-agents-vol`, 150 GB, US-IL-1, $10.50/month) was found billing while no pod ran
and has since been deleted (`DELETE` returned 204 after the EXITED pod still referencing it was
terminated; a read-only check at 16:54 shows zero volumes on the account). With nothing left to attach, the
`[runpod].volume_id` path cannot be exercised on this account; the transport seam is unit-covered
(`da51005`). Recorded as dropped for lack of a volume, not as untested. `doctor` did find that volume and its
monthly cost before the deletion, which is the behaviour F-6 was about.

**W4 — the guard was not merely unproven; it was wrong.** A paid run stopped a pod that was **32
seconds old**, reporting "idle for 30 min (limit 5 min)": the provider's per-GPU idle seconds were
compared against the idle window without clamping to the machine's own uptime, so the requirement
the tool exists for — a forgotten machine stops itself — could destroy a working one. This is what
R4's wish looked like once it was tested, and it is why R4 is a defect rather than a gap. **Fixed** at
`d5fdaea`: provider idle is clamped to the pod's own uptime and ignored when uptime is unknown, with
a control test that a genuinely idle old pod is still stopped. R4 stays open for a different reason —
the guard has still never run against a rented pod.

**The coverage number is a floor, and the guard defect proves it.** Every line of the guard defect
executed. 74.8% of the code could be reached while the behaviour was wrong, so R10's first half is
the cheap half; the feature matrix — command × provider × platform — is where the value is. At
`74b9f7a` the floor stands at 90.2%, and all four fixed money defects sit in lines that were already
covered.

## Permissions

- Renting requires the Main Bot's approval. Not enforced by code; the cap is the backstop, and per W1
  the cap is itself conditional.
- Publishing is the user's decision alone.

## Scopes

Everything here is scope `project` except W2, which is `all` — the "never publish without asking" rule
belongs to every project, not this one.
