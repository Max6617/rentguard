# Limitations

What rentguard does not do, and what it cannot promise. Read this before trusting it with money you
cannot lose.

Describes **`0d01d57`** (2026-10-16). Where a number or a quote appears, it names the revision it was
read at.

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

**`up --dry-run` used to spend money, and that is why it is tested.** `cmd_up` called
`provider.create` inside the GPU-preference loop *before* it checked the dry-run flag, so a "dry" run
could rent a machine and still print "nothing was rented and nothing was spent": against RunPod it made
three real create attempts (each refused) and against the fake it left `fake-1` in `PROVISIONING` with no
ledger row. Fixed at `ce69cea`, and the test asserts on the provider call (`f7bf556`) rather than on the
sentence that lied. The record is [KNOWN-FAILURES](KNOWN-FAILURES.md), F-4.

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
terminal, suspend the laptop, or let it crash, and nothing stops the instance. The heartbeat is how
`status` and `doctor` tell you which of those happened, and it is read as a **process fact** (Gilbert's
ruling, 2026-10-16): the file names the pid that wrote it, and `alive` requires that pid to be running
**and** the writer not to have marked itself finished. `cmd_watch` marks on every exit path, and `up`
marks its own wait guard before its release returns, so a guard that ended reads `finished (last
heartbeat X min ago)` rather than `alive`; a pid that is gone reads `not running (pid P is gone; …)`; a
writer that is running but has gone quiet for more than five minutes reads `running (pid P) but silent
for X min — probably wedged`; and with no heartbeat at all it reads `NO GUARD`. The age is on the line
in every one of those, as the secondary signal it is — never the verdict. Nothing restarts the guard
for you.

**One measured limit on the pid check**, on Windows only: an exited process whose handle is still held
elsewhere can be probed successfully, so a guard that was **killed or crashed** can read as running
until that handle closes. A clean exit is unaffected — the writer's own mark is what says it finished —
so this touches the kill/crash case alone, and the freshness rule in `watch`'s own `armed_before`
(a running pid, unmarked, **and** a heartbeat inside a multiple of the poll interval) is what keeps the
protection claim honest there.

**It overshoots by at least one poll.** The guard checks every 60 seconds by default. A run that
crosses the cap just after a check pays for up to one more interval before the stop is sent, and the
provider's own stop latency is not modelled.

**Idle means idle, and the guard's arithmetic for it is narrow on purpose.** Three rules keep the idle
stop from firing on a working pod: time spent *watching* is not time *idle* (the guard's own elapsed
counter grows while the GPU is busy and is no longer a reason to stop anything); a sampled GPU
utilisation above zero forces idle to zero, because the provider's per-GPU idle figure is measured against
the host and can read half an hour on a card visibly at work; and a pod cannot have been idle longer than
it has existed, so the idle figure is bounded by the pod's age. When the provider reports no uptime at
all, nothing read from it is attributable to this pod, so it is not a reason to stop it either.

**Spend that rentguard never recorded is invisible to the cap.** An instance billing on the account with
no ledger row sits outside every check. `doctor` separates two cases by the machine's **name**: one
carrying a `rentguard-<tag>` that is not this root's is another root's machine — that root's ledger owns
it, and this one only warns, because stopping a machine someone else is paying for is worse than a
warning; a name no project claims (including the old fixed `rentguard-workbench`) is a stray, and that is
the hard error. `adopt` gives a machine a row, backdated from the provider's uptime. If you never run
either, the cap compares your cap against a total that is missing that machine.

**`down` closes a row only when the account agrees, and `adopt --uptime-hours` is an estimate**
(added 2026-10-16). Two witnesses are consulted for every machine a closure must answer for — the
instance's own record and the account's **listing** — and the row stays **open** unless both call the
machine finished, where finished means `EXITED`, `TERMINATED`, or gone from the account: every other
status counts as still billing, including one this tool has never heard of (`STOPPING`, a transitional
or provider-specific state). A stop call the provider did not honour is therefore never read as a
stopped machine (exit 1, `action: "still-billing"`, and the accrual keeps counting). The machine the
**open row** names is
confirmed too, even when it is not the one this command stopped; if it is still billing, the row stays
open until it is stopped, and `down` does not stop that machine for you. `adopt --uptime-hours` carries
three declared limits: with **neither** a creation instant **nor** a positive uptime, any positive
estimate is accepted, because refusing there would leave real spend unrecorded and over-recording is the
conservative direction; a *provider-reported* uptime that contradicts `createdAt` is not cross-checked;
and a `createdAt` with no timezone is read as UTC, so the check moves with the host's offset — east of
UTC it can refuse a legitimate estimate. An estimate that is not a positive finite number of hours —
zero, a negative, `nan`, `inf` — is refused outright: it would record nothing for a machine that is
billing.

**Every status except `EXITED` and `TERMINATED` counts as billing** (Gilbert's ruling, 2026-10-16).
`up` refuses to rent while the account shows such a machine with no ledger row here, `doctor` counts it
as live and names it, and the drift check treats it as an open bill — including a status this tool has
never heard of (`STOPPING`, `ERROR`, anything a provider adds later). That is deliberately over-strict:
a false refusal is loud, names the machine, and takes one word (`--force`) to override, while a silent
billing machine is neither. The cost is real and known, and it is the price of not being silent: an
`ERROR` pod that never billed blocks a rent until it is terminated or `--force` is passed, and a machine
stopped a moment ago blocks one for the seconds it spends in the provider's transitional state — the
same command rents as soon as the provider reports `EXITED`. A machine whose status is missing or
unknown counts as billing too, because nothing about it says the machine finished.

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
while it crash-loops. `is_alive` requires a positive uptime *and* a port; `up` — both the path that
rents a new machine and the path that reuses one it already recorded — `run` and `pull` refuse to treat
anything else as a rental.

**A resume is held to the same rule as a fresh rent** (fixed at `fe38468`; two holes were open at
`0d01d57` and are named below). `up` takes a second liveness reading on a machine it already recorded,
stops a machine that fails it and ends the row (exit 1, `unusable`), and arms a guard while a resume
waits for the machine to answer. Both of `up`'s paths — renting a new machine and reusing a recorded one
— end a rental they will not confirm through the same discipline:

- a machine the account **no longer shows** is not a rental: the second reading is an answer in its own
  right, not a reason to fall back to the stale first one (`provider.get(...) or pod` used to report
  `reused`, exit 0, and open a row against a machine that does not exist);
- a resume whose **wait fails** stops the machine it revived; the failure is still reported with a
  non-zero exit;
- when the stop cannot be confirmed the row stays **open**, so the accrual and the cap keep seeing it —
  the one exception is an interrupt, where the row is closed without waiting because a row that exists
  is what survives a dying process.

The reproductions are `tests/test_b19_money_fix_attacks.py::TheReuseFixAtD8accac` (green since
`fe38468`) and `tests/test_up_reuse_usability.py`.

**Without a network volume, the container disk dies with the instance.** A restart gives you a fresh
container and none of your results. If you need results to survive, either set `[runpod].volume_id` or
make sure `pull` has run before the machine stops. (`down` prints a "volume survives" line only when
the provider payload carries a `volume` key; that message is not a reliable check of whether a volume
is mounted.)

**A network volume bills monthly, and it cannot be deleted while any pod still points at it.** It costs
its $/GB every month whether or not a pod exists, so a forgotten one is a permanent small bill. RunPod
refuses the delete while *any* pod references the volume — including one that is only `EXITED`: the
request returns `400 You must remove this network volume from all pods before deleting it`, so that pod
has to be **terminated** first, not merely stopped. `doctor` reports volumes, their monthly cost, and
which are attached to nothing; `ledger` states that its total covers rentals only. The volume path is
unit-covered, not exercised end to end — see [KNOWN-FAILURES](KNOWN-FAILURES.md), F-6.

**Everything remote goes over ssh.** `run` and `pull` use the `ssh` binary on your PATH, with
`StrictHostKeyChecking=no` and the local null device for known hosts (`NUL` on Windows, `/dev/null`
elsewhere — the provider states the option, the CLI substitutes this platform's device). No ssh client, no
remote work.

**The community-pod route is unproven on hardware, and it currently fails.** A community pod has no
direct endpoint, so it is reached through the `ssh.runpod.io` proxy, which only allocates a PTY: without
`-tt` the proxy answers `Error: Your SSH client doesn't support PTY`, so `-tt` is passed and the archive
travels base64-encoded (a PTY rewrites LF as CRLF). Even so, defect 9 is open: the PTY opens but the
command still does not execute, and the pull reads prompt text back. The session transport is being built
from **scripted transcripts**, not from a real community pod, which is why its tests pass and the route is
still not usable. Rent a secure pod, or expect `run` not to work. The direct route is unchanged and is
what the real rentals in this project used.

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

**A pull cannot hold a machine open, and a stop cannot hang.** An ssh pull is bounded by
`[artefacts].pull_timeout_s` (120 s by default): when the bound expires the child is killed and the pull
is reported as `ssh-timeout`, and `down` stops anyway. A stop is bounded by `[budget].stop_timeout_s`
(45 s by default): if the ordinary call does not return within that, it escalates to the provider's own
stop endpoint. Both bounds exist for the same reason — no socket is worth a machine that keeps billing.

**A pull says what it did not get.** Three cases are reported rather than passed off as a clean copy: a
configured pattern that no archived member matches is a finding (`matched nothing`), with the match tested
by `fnmatch` against the member path — `results/**/*.json` is answered by `results/out.json`, but
`results/notes.txt` does not answer `results/*.json` the way a plain prefix test would; an archive with no
members at all is a failure (`patterns-matched-nothing`), not a delivery of zero files, because the
artefacts may still be on the machine; and a partial pull keeps the members it did get while naming the
ones it did not. When a machine cannot be pulled at all, `down`'s sentence names the configured patterns
that are about to be lost rather than saying "artefacts" in general.

**`run` does not fail because a pull delivered nothing, unless you tell it to.** A command that only
reads, stages or validates produces no artefacts, so a pull that matched nothing leaves `run`'s exit
code to the command, with `pulled: false` and `pull_reason: "patterns-matched-nothing"` still in the
result. A pull that **broke** fails the run, because there the files may exist and be lost.
`run --require-artefacts` is the opt-in that makes an empty delivery fail the run too. `down` keeps the
stricter rule unchanged: it is the last moment before the container is destroyed, so a pull that
delivered nothing stays non-zero there.

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

**The fake provider models a world that is not RunPod.** It is 99.6% covered with nothing real unhit,
but coverage is not fidelity: the fake is the one place a fixture can manufacture a payload that never
existed. The rule, recorded in `tests/test_storage.py`'s docstring, is that a provider-payload fixture
comes from a live response or the provider's schema, and an invented one says so. The volume read is the
case in point: the first version invented the response key, passed, and hid that the live call returned
nothing (`f4225ff`). The fake is also a program that can be wrong on its own terms — a helper that never
saved the machine it added was found and fixed at `458e128` — so "green against the fake" means the
decisions and the arithmetic are right, never that the provider was understood. At `042fd21` the lowest
measured module is `ledger.py` at 96.2% and nothing is below the 94% figure — but that number is a
snapshot: `session.py` was added at `ddd79c9` and measured 83.2% before it had tests, which put the
whole project under its own 98.0% coverage floor for a night. Coverage moves when code lands, and the
floor is a ratchet rather than a promise about the next commit.

**The MCP server is stdio-only and has no authentication of its own.** Whoever can call its tools can
rent a machine. The approval rule — renting needs the Main Bot's approval, and the cap is the backstop
rather than the permission — is a team policy, not something the server can enforce on its own. See
[MCP server](mcp.md).

**No web UI, no scheduler, no auto-restart.** rentguard is a CLI plus an MCP server. When something
must run on a timer, that something is your `cron`, `systemd` or process manager, and it is your job
to notice when it stops.
