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

| # | requirement | enforced-by | checked-by | state |
|---|---|---|---|---|
| R1 | CLI surface complete — ten commands | code: the parser | — | **met** |
| R2 | MCP surface complete | — | — | **NOT MET** — no `watch`, no `init` |
| R3 | Money-critical paths hardened with tests | 74 tests, fake provider | suite run | **met** (logic only) |
| R4 | The guard proven end to end on a real machine | — | — | **NOT MET** — `watch` has never run against a rented pod |
| R5 | End-to-end proof on a real rented GPU | 3 rentals, $0.0264 | ledger reconciled by hand | **met** |
| R6 | Documentation | — | read through | **partial** — README omits `adopt` |
| R7 | Never reports an account balance | code: the provider interface has no such method | — | **met** |
| R8 | Never publishes anything | — | — | **wish** — discipline, not a control |
| R9 | The cap protects the user | only while `watch` runs | — | **wish** — see below |
| R10 | 94% of the code and 100% of the features exercised | `tools/coverage.py`, ratchet floor in CI | the CI run | **NOT MET** — 74.8% measured 2026-10-14, `runpod.py` at 21.0% |

## The wishes

**W1 — the cap only exists while the guard is running.** `watch` is a foreground process that nothing
starts. If it is not running, there is no cap: the instance bills until someone notices. This is
precisely the failure the tool was built for, and the 16.4-hour pod it caught was exactly that. In the
MCP workflow it is worse — a bot can rent with no way to arm the guard at all (R2).

**W2 — "do not publish" is a promise, not a lock.** Nothing in the repository prevents a push to a
public remote. Given R8 matters to the user, "no remote configured" is a fact today and could stop
being one silently.

**W3 — network volumes are entirely unexercised.** A user who sets one is running untested code.

**W4 — the guard is not merely unproven; it is wrong.** A paid run stopped a pod that was **32 seconds
old**, reporting "idle for 30 min (limit 5 min)": the provider's per-GPU idle seconds are compared
against the idle window without clamping to the machine's own uptime. So the requirement the tool
exists for — a forgotten machine stops itself — can currently destroy a working one. This is what
R4's wish looked like once it was tested, and it is why R4 is a defect rather than a gap.

**The coverage number is a floor, and the guard defect proves it.** Those lines all executed. 74.8%
of the code can be reached while the behaviour is wrong, so R10's first half is the cheap half; the
feature matrix — command × provider × platform — is where the value is.

## Permissions

- Renting requires the Main Bot's approval. Not enforced by code; the cap is the backstop, and per W1
  the cap is itself conditional.
- Publishing is the user's decision alone.

## Scopes

Everything here is scope `project` except W2, which is `all` — the "never publish without asking" rule
belongs to every project, not this one.
