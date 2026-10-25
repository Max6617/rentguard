# Limitations

What rentguard does not do, and what it cannot promise. Read this before trusting it with money you
cannot lose.

rentguard is a thin, honest layer over a provider's API: it records what it spends, it refuses what it
cannot account for, and it stops machines when a rule fires. It is not a billing system, not a provider
console, and not a supervisor that runs by itself. Every limit below is real and is stated in the
direction that costs you the least.

## Money

**It never knows your balance.** Nothing in this tool reads an account balance, because no supported
provider exposes one reliably: RunPod's billing figures lag, and a null total means "unavailable", not
"$0.00". `[budget].balance_usd` is accepted in the config for backwards compatibility and is **not read
by any code** — do not put a number there and expect anything to happen. rentguard reports what it has
recorded against the cap you set. The provider's console is the only source of truth for the account.

**The ledger is an estimate, not an invoice.** Cost is `rate × wall clock`, where the rate is what the
provider quoted when the row was opened. It excludes everything the provider bills that is not the
instance's hourly rate: storage and network volumes, container disk, egress, image pulls, minimum
billing increments, spot or preemption repricing, taxes, and credits. [What the cap guarantees](cap.md)
lists this in full.

**Recorded totals exclude the open row.** `ledger` and `doctor` sum closed rows only, so they read low
while something is running. `ledger` prints the open row's accrued cost on its own line under the
table, and `status` prints the live estimate under `this run` and `all runs`. When a row is open and
the provider shows no instance, `status` prints a `DRIFT` headline and `doctor` reports
`open-run-instance-missing` — detection, not repair: nothing closes a drifted row on its own, and until
you run `down` or `adopt` it sits open and outside the total.

**A closed drifted row is an estimate from the row's start to the moment you close it.** If the machine
really died an hour earlier, the ledger over-counts; if it kept billing after the row was closed, it
under-counts. For a vanished instance nobody can know, and a conservative estimate is better than a zero
that hides it.

**Spend that rentguard never recorded is invisible to the cap.** An instance billing on the account
with no ledger row sits outside every check. `doctor` separates two cases by the machine's **name**: one
carrying this project's generated name is this project's machine; a machine carrying another project's
tagged name is that project's — this root only warns, because stopping a machine someone else is paying
for is worse than a warning; and a name no project claims is a stray, which is the hard error. `adopt`
gives a machine a row, backdated from the provider's uptime. If you never run either, the cap compares
your cap against a total that is missing that machine.

**Every status except `EXITED` and `TERMINATED` counts as billing.** `up` refuses to rent while the
account shows such a machine with no ledger row here, `doctor` counts it as live and names it, and the
drift check treats it as an open bill — including a status this tool has never heard of (`STOPPING`,
`ERROR`, anything a provider adds later). That is deliberately over-strict: a false refusal is loud,
names the machine, and takes one word (`--force`) to override, while a silent billing machine is
neither. The cost is real and known: an `ERROR` pod that never billed blocks a rent until it is
terminated or `--force` is passed, and a machine stopped a moment ago blocks one for the seconds it
spends in the provider's transitional state — the same command rents as soon as the provider reports
`EXITED`. A machine whose status is missing or unknown counts as billing too, because nothing about it
says the machine finished.

**`down` closes a row only when the account agrees.** Two witnesses are consulted for every machine a
closure must answer for — the instance's own record and the account's **listing** — and the row stays
**open** unless both call the machine finished, where finished means `EXITED`, `TERMINATED`, or gone
from the account. A stop call the provider did not honour is therefore never read as a stopped machine:
`down` exits 1 with `action: "still-billing"`, and the accrual keeps counting so the cap keeps seeing
it. The machine the **open row** names is confirmed too, even when it is not the one this command
stopped; if it is still billing, the row stays open until it is stopped, and `down` does not stop that
machine for you. `down` waits `[budget].stop_confirm_s` (30 s) for the account to stop listing the
machine before it refuses to call the window closed. An account that cannot be read at all is not an
account that says the machine is gone, so the row is left exactly as it was and `down` exits 1.

**`adopt --uptime-hours` is an estimate, with three declared limits.** The value must be a positive
finite number of hours: zero, a negative, `nan` and `inf` are refused outright, because such a row would
record nothing for a machine that is billing. Beyond that:

- with **neither** a creation instant **nor** a positive uptime, any positive estimate is accepted,
  because refusing there would leave real spend unrecorded, and over-recording is the conservative
  direction;
- a *provider-reported* uptime that contradicts the creation instant is not cross-checked;
- a creation instant with no timezone is read as UTC, so the check moves with the host's offset — east
  of UTC it can refuse a legitimate estimate.

**`max_hours_per_run` follows the provider's uptime, not the row's clock.** A stop/start cycle resets
the uptime the provider reports, while the ledger row keeps its original start.

**One open row at a time.** The ledger refuses a second open row and takes a short-lived lock file
around the check and the append, so two processes racing are serialised. The lock is a safety net, not a
database: it covers only the open/close window, and where a lock file cannot be created the ledger
proceeds unlocked rather than refusing to record money.

**Two projects keep two ledgers.** The ledger is a local JSONL file per project root. Two projects that
should share one budget will keep two ledgers unless they share a `state_dir`. `RENTGUARD_ROOT` exists
so an MCP client or service reads the same files as your shell.

## The guard

**The cap only exists while `watch` runs.** `watch` is a foreground process on your machine; nothing
starts it for you and nothing restarts it. Close the terminal, suspend the laptop, or let it crash, and
nothing stops the instance: the machine bills until someone notices. That is the failure this tool was
built for, and it is the honest limit of everything in [the cap page](cap.md).

**The heartbeat is a process fact, not a timestamp.** Each guard writes its own file in the state dir
(`heartbeat-<id>.json`, plus the legacy `heartbeat.json`). `alive` requires the pid that wrote it to be
running **and** the writer not to have marked itself finished. One file per guard matters when more
than one is watching — an operator's `watch` beside `up`'s wait guard — because a shared file let the
guard that finished mark the other's reading `finished`; the readers take the **best** of the set, so a
live guard wins over a finished one. A guard that ended reads `finished (last heartbeat X min ago)`
rather than `alive`; a pid that is gone reads `not running (pid P is gone; …)`; a writer that is running
but silent for more than five minutes reads `running (pid P) but silent for X min — probably wedged`;
and with no heartbeat at all it reads `NO GUARD`. The age is printed on every line as the secondary
signal, never as the verdict.

**The verdict answers for one guard; `doctor` adds the set beside it.** The reported reading stays
single on purpose — it answers *is anything watching?*, and with the best-of preference a crashed guard
beside a live one cannot make `armed` true. The set is a **diagnostic**, not a second verdict and not a
schema: `doctor` prints one line when more than one heartbeat file is readable, and adds a **warn**
(`guard-crashed`) for a reading whose pid is gone and which never marked itself finished. That warn is
not an error and does not change the exit code — a crashed guard beside a live one is a protection
redundancy loss, not a stop.

```text
guard heartbeat: alive (pid 26900, heartbeat 0.0 min ago)
guard set      : 2 heartbeat files — alive (pid 26900, 0 min ago); not running (pid 999999, 8 min ago)
  WARN  guard-crashed: a guard is gone and never marked its heartbeat finished (pid 999999, …)
```

The set appears **only** there: `status` never prints it, and none of the JSON output carries it. Three
limits, deliberately. The number is **readings, not guards**: two files can be one guard's past, and a
finished marker older than the window is residue — not counted, and removed by the next writer along
with the legacy file. A reading carries no file or guard id, only its process facts and age. And a
crashed guard whose file cannot be `stat`-ed (a permission or ACL refusal) is dropped from the set
entirely, so both the count and the warn under-report rather than invent a reading.

**One measured limit on the pid check, on Windows only:** an exited process whose handle is still held
elsewhere can be probed successfully, so a guard that was **killed or crashed** can read as running
until that handle closes. A clean exit is unaffected — the writer's own mark is what says it finished —
so this touches the kill/crash case alone, and the freshness rule a bounded watch applies (a running
pid, unmarked, **and** a heartbeat inside a multiple of the poll interval) is what keeps its protection
claim honest.

**It overshoots by at least one poll.** The guard checks every `--interval` seconds (60 by default). A
run that crosses the cap just after a check pays for up to one more interval before the stop is sent,
and the provider's own stop latency is not modelled. The row is closed at the moment of the decision,
so the ledger records that moment, not the moment the provider's meter stops.

**Idle means idle, and the guard's arithmetic for it is narrow on purpose.** Three rules keep the idle
stop from firing on a working pod: time spent *watching* is not time *idle* (the guard's own elapsed
counter grows while the GPU is busy and is not a reason to stop anything); a sampled GPU utilisation
above zero forces idle to zero, because the provider's per-GPU idle figure is measured against the host
and can read half an hour on a card visibly at work; and a pod cannot have been idle longer than it has
existed, so the idle figure is bounded by the pod's age. When the provider reports no uptime at all,
nothing read from it is attributable to this pod, so it is not a reason to stop it either.

**It stops, it does not terminate.** `watch` sends `stop`. Whether a stopped machine still bills for
its disk or volume is a provider question this ledger does not model.

**A bounded watch ends protection at its bound.** `watch --max-iterations N` returns at the bound with
`protection_ended_at_bound: true` and `armed: false`, and the CLI exits 2 — so a zero exit never means
"still guarded". Make `N × interval` outlast the window you are guarding. There is no detached or
background guard behind this: a supervisor you believe in and that is not running is worse than none.

**An interrupt closes the row without confirmation.** Every other unconfirmed stop leaves the row open,
but on an interrupt the row is closed without waiting, because a row that exists is what survives a
dying process.

## The machine

**`RUNNING` is a claim, not a state.** A pod can report `RUNNING` with zero uptime and null ports while
it crash-loops. `is_alive` requires a positive uptime *and* a port; `up`, `run` and `pull` refuse to
treat anything else as a rental. `run` and `pull` read a machine **three times, 45 s apart**, before
refusing: one listing that is a moment old is not a crash loop, and a machine that becomes usable on a
later read is used.

**A resume is held to the same rule as a fresh rent.** `up` takes a second liveness reading on a machine
it already recorded, stops a machine that fails it and ends the row (exit 1, `unusable`), and arms a
guard while a resume waits for the machine to answer. Both of `up`'s paths — renting a new machine and
reusing a recorded one — end a rental they will not confirm the same way: a machine the account no
longer shows is not a rental; a resume whose wait fails stops the machine it revived, with a non-zero
exit; and when the stop cannot be confirmed the row stays open, except on an interrupt.

**Without a network volume, the container disk dies with the instance.** A restart gives you a fresh
container and none of your results. If you need results to survive, either set `[runpod].volume_id` or
make sure the pull has run before the machine stops. (`down` prints a "volume survives" line only when
the provider payload carries a `volume` key; that message is not a reliable check of whether a volume is
mounted.)

**A network volume bills monthly, and it cannot be deleted while any pod still points at it.** It costs
its $/GB every month whether or not a pod exists, so a forgotten one is a permanent small bill. RunPod
refuses the delete while *any* pod references the volume — including one that is only `EXITED`: the
request returns `400 You must remove this network volume from all pods before deleting it`, so that pod
has to be **terminated** first, not merely stopped. `doctor` reports volumes, their monthly cost, and
which are attached to nothing; `ledger` states that its total covers rentals only. The volume path is
unit-covered, not exercised end to end on a live account.

**Everything remote goes over ssh.** `run` and `pull` use the `ssh` binary on your PATH, with
`StrictHostKeyChecking=no` and the local null device for known hosts (`NUL` on Windows, `/dev/null`
elsewhere — the provider states the option, the CLI substitutes this platform's device). No ssh client,
no remote work.

**The community-pod route is unproven on hardware, and it currently fails.** A community pod has no
direct endpoint, so it is reached through RunPod's `ssh.runpod.io` proxy, which only allocates a PTY:
without `-tt` the proxy answers `Error: Your SSH client doesn't support PTY`, so `-tt` is passed and the
archive travels base64-encoded (a PTY rewrites LF as CRLF). Even so the route is open: the PTY opens but
the command does not execute, and the pull reads prompt text back. Rent a secure pod, or expect `run`
not to work. The direct route is unchanged and is what real rentals used.

**A live window freezes the checkout it runs from.** The tool is imported from the checkout it lives in,
so a driver that runs `python -m rentguard.cli` from a working tree dies at **import** if that tree is
mid-edit — and the pod bills while nothing runs. Run live windows from a pinned copy (a committed
worktree, an installed copy), and treat a rented window as a freeze on any checkout it uses: write in a
separate worktree while it is up, merge once the window is closed.

**A run is bounded by `[budget].run_timeout_s` (3600 s by default).** This is not a limit on the work:
it exists because a *dropped* session never returns at all, and the machine bills while it hangs. A
legitimate job that needs longer sets it higher; `0` means no bound.

## Artefacts

**Patterns are validated at load, because the remote shell expands them.** The inventory expands a
pattern in a POSIX shell, so only plain globs are accepted — `[A-Za-z0-9_./*?[]@%+,:=~-]`. Whitespace,
quotes, `;`, `$`, backticks, `<`, `>`, `|`, `&`, `(`, `)` and backslashes are refused when the config is
loaded, rather than executed on the machine. A filename containing a tab or newline cannot be carried:
entry paths in the inventory answer are tab-separated.

**A machine has to answer the inventory, and it is assumed to be Linux.** The measurement uses `stat
-c`, `du -sb` and a POSIX `sh`, the same family as the GNU `tar` the archive already needed; a container
without them fails the pull as `inventory-unreadable` **before** anything is transferred, never by
falling back to archiving the root.

**Artefacts are pulled at two moments by default, and once deliberately.** If `[artefacts].pull` is
non-empty, `run` pulls when each run finishes and `down` pulls before stopping the machine. The old
`[artefacts].on_run` switch is deleted: a config that still contains it is accepted with the key dropped
rather than rejected, so a stale file does not refuse to run. On `down`, a failed pull does not cancel
the stop — it stops anyway, exits non-zero, and names the configured patterns that may still hold files.
`down --keep-running` is the explicit opposite: leave the machine up so the copy can be retried, which
keeps billing. The stop is the default because a machine that keeps billing is the worse loss. Call
`rentguard pull` yourself between runs if you want a copy at any other moment.

**A pull is bounded by the volume it carries, not by a constant.** An ssh pull's bound is
`max([artefacts].pull_timeout_s, volume / [artefacts].pull_bytes_per_second)`, with a floor of 5 s and
a ceiling of one day. The 120 s `pull_timeout_s` is a floor: a ~700 MB run directory gets roughly 700 s
at the default 1 MB/s. When the bound expires the child is killed and the pull is reported as
`ssh-timeout`, naming the volume and the bound, and `down` stops anyway. A pull that would need more
than a day is reported with the bound it was given rather than allowed to outlive the machine.

**A stop cannot hang.** A stop is bounded by `[budget].stop_timeout_s` (45 s by default): if the
ordinary call does not return within that, it escalates to the provider's own stop endpoint. Both bounds
exist for the same reason — no socket is worth a machine that keeps billing.

**One pull carries what its patterns declare, never more by inference.** A pattern listed in
`[artefacts].run_patterns` names a collection of runs — one directory per run, `grok/runs/*` — and
carries only the **newest** directory each pull, naming the rest with `rentguard pull --all` as the
backstop. Every **other** pattern carries **everything it matches**: a glob cannot be told apart from a
run's contents by its shape (`grok/runs/*` and `grok/runs/run-16/*` are the same rule), and carrying the
wrong half of a run is the loss this tool exists to prevent — with a volume-sized bound, carrying more
is a cost, not a failure. An undeclared pattern matching several directories says so in the output
(count, volume, and the declaration that would trim it). `--all` carries every match everywhere and can
be very large. A `run_patterns` entry must also appear in `pull`, because a declaration that nothing is
pulled for would be a silent no-op.

**Two limits of the "newest directory" rule, named rather than fixed.** The inventory reads `stat -c %Y`
of the **run directory itself**, so a run that writes into a nested subdirectory does not advance it: a
preserved or restored mtime can make the *previous* run look newest, and the pull then carries the wrong
directory while naming the just-finished one as skipped — `pull --all` carries both. And equal mtimes
(two runs finished in the same second) are broken by path, lexicographically, so `run-9` beats `run-10`;
the loser is named as an older run though the measurement cannot establish that. Both are why the rule
is opt-in: an undeclared pattern never makes either choice.

**The two contracts meet on one path.** When a directory is named by a **declared** collection *and* by
an **undeclared** pattern, it is **carried**, and the pull says why (`carried: … — also matched by an
undeclared pattern`). Carrying honours both contracts, cannot lose data, and the volume-sized bound
makes the cost visible rather than fatal. An overlapping declaration is legitimate; refusing it would
block a pull for no gain.

**A pull says what it did not get.** Three cases are reported rather than passed off as a clean copy: a
configured pattern that no delivered member matches is a finding (`matched nothing`), with the match
tested by `fnmatch` against the member path — `results/**/*.json` is answered by `results/out.json`, but
`results/notes.txt` does not answer `results/*.json` the way a plain prefix test would; an archive with
no members at all is a failure (`patterns-matched-nothing`), not a delivery of zero files, because the
artefacts may still be on the machine; and a partial pull keeps the members it did get while naming the
ones it did not. When a machine cannot be pulled at all, `down`'s sentence names the configured patterns
that are about to be lost rather than saying "artefacts" in general.

**The two pull seams disagree about an empty delivery.** The framed ssh pull reports
`patterns-matched-nothing` and fails; a provider that hands its artefacts over in memory (the fake)
reports `pulled: true, files: []` and exits 0. Since `down`'s non-zero exit is keyed on the pull's exit
code, a fake rehearsal's `down` can exit 0 after carrying nothing, while a real machine's would fail.

**`run` does not fail because a pull delivered nothing, unless you tell it to.** A command that only
reads, stages or validates produces no artefacts, so a pull that matched nothing leaves `run`'s exit
code to the command, with `pulled: false` and `pull_reason: "patterns-matched-nothing"` still in the
result. A pull that **broke** (`ssh-timeout`, `frame-invalid`, `stream-not-an-archive`) fails the run,
because there the files may exist and be lost. `run --require-artefacts` is the opt-in that makes an
empty delivery fail the run too. `down` keeps the stricter rule: it is the last moment before the
container is destroyed.

**`--json` splits the two streams, so merge them deliberately.** With `--json`, stdout carries exactly
one JSON document and the human prose goes to stderr, so `json.loads(stdout)` works without hunting for
the last JSON value. A caller that wants both reads two streams; one that already merges them with
`2>&1` gets the prose in front of the document, which is not valid JSON.

## Data on your side

**The ledger is a local JSONL file, one per project root.** No database, no server; a short-lived lock
file serialises the open/close window (see above).

**A malformed ledger line is skipped when reading, but it is reported.** A corrupt line must not take
the whole ledger with it, so the reader skips it. `doctor` counts unreadable lines, prints
`ERROR ledger-line-unreadable`, and exits non-zero; `ledger --json` reports them under
`malformed_lines`. The money inside such a line is still not counted, so the provider's console is the
only total that includes it.

**`state.json` is a single pointer to one instance.** Truncate or hand-edit it and the pointer is
lost — but `up` cross-checks live instances against the ledger and refuses to rent on top of a machine
the ledger does not name, and `doctor` reports it. The ledger is the record that counts; the state file
is only the convenience pointer.

**`[runpod]` keys are not validated, and a top-level key below a table is swallowed.** `[budget]` and
`[artefacts]` reject unknown keys at startup; the provider section is passed through untouched, so a
typo there is accepted and ignored. Worse, `state_dir`, `provider`, `allow_renting` and the `ssh_*` keys
are top-level: put one below a `[table]` header and TOML files it inside that table, where nothing reads
it. `init` writes them above the first header, with a comment saying why.

**There is only one provider settings section.** Provider settings are read from `[runpod]` regardless
of which provider is selected, so a second provider has nowhere natural to put its own keys — see
[Providers](providers.md).

**Commands that need the provider need the API key even when nothing is rented.** `status`, `watch`,
`down`, `run` and `pull` build the provider before they read the state file, so without a key they fail
with the key error rather than "no instance recorded". `doctor` and `ledger` work without a key.

**`doctor` exits non-zero when it finds an error.** Any finding with severity `error` — no API key, an
unreadable ledger line, drift in either direction, a live instance with no row — makes `doctor` exit 1
after printing `ERROR <code>: <message>` lines. A script can gate on it.

## Interface and scope

**RunPod only, for v0.1.** The provider interface is small and the in-memory fake implements it for
tests; see [Providers](providers.md) for what a second provider must supply.

**The fake provider models a world that is not RunPod.** It is well covered, but coverage is not
fidelity: the fake is the one place a fixture can manufacture a payload that never existed. Its payload
shapes are modelled on RunPod's, and a fixture that was invented rather than taken from a live response
is labelled as invented. "Green against the fake" means the decisions and the arithmetic are right,
never that the provider was understood.

**The MCP server is stdio-only and has no authentication of its own.** Whoever can call its tools can
rent a machine. The approval rule — renting needs the operator's approval, and the cap is the backstop
rather than the permission — is a team policy, not something the server can enforce on its own. See
[MCP server](mcp.md).

**No web UI, no scheduler, no auto-restart.** rentguard is a CLI plus an MCP server. When something must
run on a timer, that something is your `cron`, `systemd` or process manager, and it is your job to
notice when it stops.
