# Limitations

What rentguard does not do, and what it cannot promise. Read this before trusting it with money you
cannot lose.

## Worked example: an open row whose instance is gone

A tool that hides an open ledger row is a tool that hides money. This happened, and it is why the
drift checks exist.

Reproduced by hand on 2026-10-14, no network writes and no spend. Setup: one ledger row, no
`stopped_at`, naming an instance that does not exist, started three hours earlier at $2.00/h.

**What the tool did before the fix:**

```console
$ rentguard status
no instance recorded
spend so far   : $0.00 of $25.00

$ rentguard ledger
drift-demo  ...  OPEN  -  open row whose instance does not exist
total                                   0.00   0.0000
```

`status` is the command a person or a Bot calls to decide whether anything is billing. It answered
"no instance, $0.00" while an open row existed — the one state in which money is likeliest to be
hiding: a crash, a provider-side termination, a click in someone else's console. The ledger total read
low by the whole accrued amount, which also makes the cap check read low. The cause was one line: the
state file's instance id was cleared when the provider reported 404, without closing the row or marking
it unresolved.

**The same setup on the current code:**

```console
$ rentguard status
DRIFT          : run 20261014T101144Z-086c00 is open, but the provider shows no instance 'vanished-1'
accrued        : $6.001 at $2.00/h since 2026-10-14T10:11:44+00:00
this run       : $6.001 of $5.00
recorded spend : $0.00 of $25.00 (an open row is not counted until it is closed, so this figure reads low)
purpose        : open row whose instance does not exist
next           : `rentguard down` closes the row and records the cost; `rentguard adopt <id>` if the machine does still exist

$ rentguard doctor
...
  ERROR open-run-instance-missing: open run 20261014T101144Z-086c00 names instance 'vanished-1', which the provider does not show
```

`doctor` exits 1 on that finding. `ledger` prints the open row's accrual under its table, and `down`
closes the row:

```console
$ rentguard down
no instance recorded; closed row 20261014T101144Z-086c00

$ rentguard ledger
20261014T101144Z-086c0 Fake GPU 24GB                 3.00   6.0056  down-no-instance   open row whose instance does not exist
total                                                3.00   6.0056
cap: $25.00   remaining: $18.99
```

What remains yours to do:

- **Detection is not repair.** Nothing closes a drifted row on its own. Until you run `down` or
  `adopt`, it sits open and outside the total.
- **The closed figure is an estimate from the row's start to the moment you close it.** If the machine
  really died an hour earlier, the ledger over-counts; if it kept billing after the row was closed, it
  under-counts. For a vanished instance nobody can know, and a conservative estimate is better than a
  zero that hides it.
- The behaviour is held by `tests/test_drift.py` (3 tests, passing).

## Money

**`up --dry-run` is currently unsafe.** At commit `2ebe660`, `cmd_up` calls `provider.create` inside
the GPU-preference loop *before* it checks the dry-run flag. A dry run therefore makes real create
calls: run against RunPod on 2026-10-14 it attempted three rentals (the RTX 4090, L40S and A6000 were
each refused at create) and then printed `dry run: would request NVIDIA RTX A6000; nothing was rented
and nothing was spent`. Run free against the fake, it leaves `fake-1` in `PROVISIONING` and no ledger
row — [KNOWN-FAILURES](KNOWN-FAILURES.md), F-4. Had a RunPod attempt succeeded, the pod would have been
billing with no state pointer and no ledger row, because both are written after the dry-run return —
the exact untracked-instance failure the tool exists to prevent. No test covers this. Do not use
`--dry-run` until it is fixed; check the cap by reading `rentguard ledger` and `rentguard status`
instead.

**It never knows your balance.** Nothing in this tool reads an account balance, because no supported
provider exposes one reliably: RunPod's billing figures lag, and a null total there means
"unavailable", not "$0.00". `[budget].balance_usd` is still accepted in the config for backwards
compatibility and is **not read by any code** — do not put a number there and expect anything to
happen. The tool reports what it has recorded, against the cap you set. The provider's console is the
only source of truth for the account.

**The ledger is an estimate, not an invoice.** Cost is `rate × wall clock`, where the rate is what the
provider quoted when the row was opened. It excludes everything else the provider bills — storage and
network volumes, container disk, egress, image pulls, minimum billing increments, spot repricing,
taxes, credits. [What the cap guarantees](cap.md) lists this in full.

**The cap only fires while `watch` runs.** `watch` is a foreground process on your machine. Close the
terminal, suspend the laptop, or let it crash, and nothing stops the instance. `status` and `doctor`
report the guard heartbeat and say so when it is more than five minutes old; nothing restarts it for
you. Treat "is the heartbeat fresh" as a check you own.

**It overshoots by at least one poll.** The guard checks every 60 seconds by default. A run that
crosses the cap just after a check pays for up to one more interval before the stop is sent, and the
provider's own stop latency is not modelled.

**Spend that rentguard never recorded is invisible to the cap.** An instance billing on the account
with no ledger row sits outside every check. `doctor` lists those and warns; `adopt` gives one a row,
backdated from the provider's uptime. If you never run either, the cap compares your cap against a
total that is missing that machine.

**Recorded totals exclude the open row.** `ledger` and `doctor` sum closed rows only; `ledger` prints
the open row's accrued cost on its own line under the table, and `status` prints the live estimate
under `this run` and `all runs`. The worked example above is what that looks like when the row is real
money and the instance is gone.

**`max_hours_per_run` follows the provider's uptime, not the row's clock.** A stop/start cycle resets
the uptime the provider reports, while the ledger row keeps its original start.

**One open row at a time.** The ledger refuses a second open row and takes a short-lived lock file
around the check and the append, so two processes racing are serialised. The lock is a safety net, not
a database: it covers only the open/close window, and where a lock file cannot be created the ledger
proceeds unlocked rather than refusing to record money.

## The machine

**`RUNNING` is a claim, not a state.** A pod can report `RUNNING` with zero uptime and null ports
while it crash-loops. `is_alive` requires a positive uptime *and* a port; `run` and `pull` refuse to
touch anything else.

**Without a network volume, the container disk dies with the instance.** A restart gives you a fresh
container and none of your results. If you need results to survive, either set `[runpod].volume_id` or
make sure `pull` has run before the machine stops. (`down` prints a "volume survives" line only when
the provider payload carries a `volume` key; that message is not a reliable check of whether a volume
is mounted.)

**Everything remote goes over ssh.** `run` and `pull` use the `ssh` binary on your PATH, with
`StrictHostKeyChecking=no` and `UserKnownHostsFile=NUL` on Windows. No ssh client, no remote work.

**Artefact patterns are expanded by the remote shell and joined without quoting.** A pattern
containing a space breaks the pull; `**` behaves like `*` under a POSIX `sh`. Use plain globs.

**Artefacts are pulled at two moments by default, and once deliberately.** If `[artefacts].pull` is
non-empty, `run` pulls when each run finishes and `down` pulls before stopping the machine.
`[artefacts].on_run` was deleted; a config that still contains it is accepted with the key dropped
rather than rejected, so a stale file does not refuse to run. On `down`, a failed pull does not cancel
the stop — it stops anyway, exits non-zero, and prints which patterns were lost.
`down --keep-running` is the explicit opposite: leave the machine up so the copy can be retried, which
keeps billing. The stop is the default because a machine that keeps billing is the worse loss. Call
`rentguard pull` yourself between runs if you want a copy at any other moment.

**`--json` splits the two streams, so merge them deliberately.** With `--json`, stdout carries exactly
one JSON document and the human prose goes to stderr, so `json.loads(stdout)` works without hunting for
the last JSON value. A caller that wants both reads two streams; one that already merges them with
`2>&1` gets the prose in front of the document, which is not valid JSON.

**The MCP server exposes all ten commands as eleven tools, and an armed guard is bounded.** [MCP
server](mcp.md) returns both forms — `content` is the text, `structuredContent` is the same answer as an
object. Protection is not a daemon, though: `rentguard_watch` runs for a bounded number of polls and
`rentguard_watch_once` takes a single reading, and both report `armed: false` because the call has
returned. A Bot that wants the cap enforced for longer must call them again. A detached guard was
deliberately not built — a supervisor you believe in and that is not running is worse than none.

## Data on your side

**The ledger is a local JSONL file, one per project root.** No database, no server; a short-lived lock
file serialises the open/close window (see "One open row at a time"). Two projects that should share
one budget will keep two ledgers unless they share a `state_dir`. `RENTGUARD_ROOT` exists so an MCP
client or service reads the same files as your shell.

**A malformed ledger line is skipped when reading, but it is reported.** A corrupt line must not take
the whole ledger with it, so the reader skips it. `doctor` counts unreadable lines, prints
`ERROR ledger-line-unreadable`, and exits non-zero; `ledger --json` reports them under
`malformed_lines`. The money inside such a line is still not counted, so the provider's console is the
only total that includes it.

**`state.json` is a single pointer to one instance.** Truncate or hand-edit it and the pointer is
lost — but `up` cross-checks live instances against the ledger and refuses to rent on top of a machine
the ledger does not name, and `doctor` reports it. The ledger is the record that counts; the state
file is only the convenience pointer.

**`[runpod]` keys are not validated, and a top-level key below a table is swallowed.** `[budget]` and
`[artefacts]` reject unknown keys at startup; the provider section is passed through untouched, so a
typo there is accepted and ignored. Worse, `state_dir`, `provider` and the `ssh_*` keys are top-level:
put one below a `[table]` header and TOML files it inside that table, where nothing reads it. `init`
now writes them above the first header, with a comment saying why.

**There is only one provider settings section.** Provider settings are read from `[runpod]` regardless
of which provider is selected, so a second provider has nowhere natural to put its own keys — see
[Providers](providers.md), step 5.

**Commands that need the provider need the API key even when nothing is rented.** `status`, `watch`,
`down`, `run` and `pull` build the provider before they read the state file, so without a key they
fail with the key error rather than "no instance recorded". `doctor` and `ledger` work without a key.

## Interface and scope

**RunPod only, for v0.1.** The provider interface is small and the in-memory fake implements it for
tests; see [Providers](providers.md) for what a second provider must supply.

**The MCP server is stdio-only and has no authentication of its own.** Whoever can call its tools can
rent a machine. The approval rule — renting needs the Main Bot's approval, and the cap is the backstop
rather than the permission — is a team policy, not something the server can enforce on its own. See
[MCP server](mcp.md).

**No web UI, no scheduler, no auto-restart.** rentguard is a CLI plus an MCP server. When something
must run on a timer, that something is your `cron`, `systemd` or process manager, and it is your job
to notice when it stops.
