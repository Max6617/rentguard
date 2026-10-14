# M2 — the real rental

The gate between "the tests pass against a fake" and "ready to publish". One real RunPod instance, one
trivial job, everything checked against the ledger. Budget: **under $2**, expected around **$0.05**.

The point is not that the tool works. The point is that the *money arithmetic* is right on a machine
that is really billing, because that is the one claim a fake provider cannot test.

## Preconditions

- [ ] M1 green: the whole suite passes against the fake provider, no network, no spend.
- [ ] `rentguard doctor` reports **nothing live** and no drift.
- [ ] The ledger total is whatever it is; record it as the opening balance for this test.

## Procedure

```bash
# 1. before anything: is anything already billing?
rentguard doctor

# 2. rent, with the cap set for this test only
rentguard up --purpose "M2 end-to-end proof" --gpu "NVIDIA GeForce RTX 3070" --cap 2.0

# 3. a job that produces an artefact, so the pull has something to prove
rentguard run -- python -c "import json,pathlib,os; pathlib.Path('results').mkdir(exist_ok=True); pathlib.Path('results/m2.json').write_text(json.dumps({'ok': True, 'whoami': os.uname().nodename}))"

# 4. the artefact must be local, and it must say what the machine said
cat artefacts/results/m2.json

# 5. the books
rentguard ledger
rentguard status

# 6. stop, and close the row
rentguard down
rentguard ledger
rentguard doctor
```

## Run 1 — 2026-10-14, 15:10 UTC (failed, and that is the point)

**Money: $0.0091.** Instance `csb2cyeemezbgv`, RTX 4090 at $0.89/h, up 36.7 s, stopped cleanly and
reconciled by hand: `36.7 s ÷ 3600 × 0.89 = $0.00907` against a ledger row of `$0.009147`.

What worked: the cap, the ledger row and its arithmetic, `down`, and `doctor` quiet afterwards (6 pods
total, 0 live). What did **not** work: the run itself.

```
$ rentguard run -- 'mkdir -p results; hostname > results/m2.json; nvidia-smi -L >> results/m2.json'
bash: line 1: mkdir -p results; hostname > results/m2.json; ...: No such file or directory
pull failed (ssh exited 2)
command exited 127; artefacts pulled
```

Three defects, none of which the fake provider could ever show:

1. **A compound command is passed to the remote shell pre-quoted**, so bash receives it as one word
   and answers `No such file or directory`. `cmd_run` does
   `" ".join(shlex.quote(part) for part in command)`; when the caller's own shell has already made
   the command a single argument, that quotes the whole line. Multi-word invocations
   (`run -- python train.py`) work, so the documented example passes while the general case fails.
2. **`run` reports "artefacts pulled" after printing that the pull failed.** The exit code is right;
   the sentence contradicts the line above it.
3. **A stale `ssh_key` path** made ssh warn `Identity file ... not accessible` on every call and
   connect anyway. Config fixed; a missing key path should arguably be an error rather than a warning
   nobody reads.

Also learned: catalog availability is not real capacity. The RTX 3070 and the RTX A4000 both showed
as available and both were refused at creation with "no longer any instances available with the
requested specifications". `up` should walk the preference list rather than dying on the first
refusal — the parked project's own `rp.py` did exactly that.

## Run 2 — 2026-10-14, 15:15 UTC (passed)

**Money: $0.007911.** Instance `xw16pcbi1avy33`, RTX 4090 at $0.89/h, up 32 s. Run 1 and run 2 together
cost **$0.017**.

```
$ rentguard run -- 'mkdir -p results; hostname > results/m2.json; nvidia-smi -L >> results/m2.json'
85585d1b19e3
GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-4654f56e-...)
pulled 1 file(s) into .../artefacts
  results/m2.json
```

Against the pass criteria:

1. **Artefact local, pulled as the run finished** — yes, on the `run` line, not at `down`.
2. **Content is the machine's, not this laptop's** — the file holds `85585d1b19e3` and the 4090's UUID;
   this machine is neither.
3. **Cost reconciles by hand** — 32 s ÷ 3600 × 0.89 = **$0.0079111**, and the ledger row says
   `$0.007911`. (Recomputing from the rounded `0.0089 h` gives a cent-level rounding artefact; from
   seconds it is exact.)
4. **Within the cap** — $2.15 of a $5.00 cap, and the cap was set for this test.
5. **`doctor` clean at the end** — 7 pods, 0 live, nothing billing, no drift, ledger lines readable.
6. **Under $2** — $0.017 for both runs.

Also confirmed in run 2: the compound command reaches the remote shell correctly (`_remote_command`),
the artefact pull fires on the run rather than at the end, and the ssh line no longer carries the
stale `-i` path.

## Still open after run 2

- `up` tries to **resume** a recorded stopped instance and dies when the host has no free GPU, instead
  of renting a fresh one. A finished instance should not be able to block the next rental, and there is
  no `rentguard forget` to drop one deliberately.

## Pass criteria

1. **The artefact is on disk, locally**, pulled when the run finished — not at step 6.
2. **The artefact's content matches the machine's** — the hostname in the file is the instance's, not
   this laptop's. A pull that silently returns a stale local file passes every other check.
3. **The ledger row exists, is closed, and its cost equals** `wall_clock_seconds / 3600 ×
   rate_usd_per_hour`, recomputed by hand from the row's own fields. If these disagree, the tool's
   central number is wrong.
4. **The recorded cost is within the cap**, and the cap was set for this test rather than inherited.
5. **`doctor` at the end reports nothing live** and no drift.
6. **The closing total is under $2.**

## Abort criteria

Stop and report rather than continue if: `doctor` shows a live instance nobody recorded; the cap is
already exceeded; the artefact comes back empty or from the wrong machine; or the ledger row's cost
cannot be reconciled by hand.

An abort is a result. A test that only passes when everything works tests nothing.

## What this does not prove

That the tool is useful, that anyone wants it, or that a second provider works. It proves the cap, the
ledger and the pull are correct against a real billing machine — no more, and that is enough for M3.
