# Quickstart

Install, write one config file, rent a small GPU, run a command, get the files back, see what it cost,
and have the machine stop itself.

Time: about ten minutes, most of it waiting for the pod to boot. Money: whatever your chosen GPU costs
per hour, capped by the `cap_usd` you set.

Output below is real output from the in-memory provider, with `<project>` standing in for the working
directory; your paths will differ. Nothing on this page spends money unless you replace `fake` with a
real provider and a key.

## 1. Install

From a checkout:

```bash
git clone https://github.com/Max6617/rentguard
cd rentguard
python -m pip install .
rentguard --version
```

```console
$ rentguard --version
rentguard 0.1.0
```

A checkout plus `PYTHONPATH` works too, if you would rather not install anything:

```bash
set PYTHONPATH=C:\path\to\the\checkout      # Windows
export PYTHONPATH=/path/to/the/checkout     # Linux, macOS
```

If the machine has no route to PyPI, build the wheel from the checkout and install that instead:

```bash
python -m pip wheel . --no-deps --no-build-isolation -w dist
python -m pip install --no-index --no-deps dist/rentguard-0.1.0-py3-none-any.whl
rentguard --version
```

`--no-build-isolation` uses the `setuptools` already installed; `--no-index --no-deps` installs the
wheel with nothing to resolve, which is correct because rentguard has no dependencies.

## 2. Write the config

```console
$ rentguard init
wrote <project>\rentguard.toml
next           : `rentguard doctor`, then `rentguard up --purpose ...`
```

It writes every default, commented, so nothing starts from an empty file. `rentguard init --print`
shows the same text without writing. The parts worth reading:

```toml
provider = "runpod"       # or "fake": in-memory, no network, no money
state_dir = ".rentguard"  # top-level keys go ABOVE the first [table], or they land inside it
allow_renting = true      # set false to make this file refuse to rent (see below)

[budget]
cap_usd = 25.0            # hard ceiling for the whole project, across all runs
per_run_cap_usd = 5.0     # ceiling for a single open run
max_hours_per_run = 6.0   # wall-clock ceiling for one rental
idle_minutes = 10         # stop after this long with the GPU idle; 0 disables
stop_timeout_s = 45       # a stop slower than this escalates to the provider's stop endpoint
run_timeout_s = 3600      # a dropped session never returns at all; this kills it, 0 means no bound
stop_confirm_s = 30       # `down` waits this long for the account to confirm a stop

[artefacts]
pull = ["results/**/*.json", "logs/*.log"]
run_patterns = []         # patterns that name ONE DIRECTORY PER RUN, e.g. ["runs/*"]
local_dir = "./artefacts"
remote_dir = "/workspace"
pull_timeout_s = 120      # the FLOOR of a pull's bound: the volume decides the rest
pull_bytes_per_second = 1e+06   # assumed throughput: a 700 MB run gets ~700 s, not 120

[runpod]
cloud = "SECURE"
image = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
container_disk_gb = 50
gpu_preference = ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A6000"]
volume_id = ""            # empty: no network volume, so the container disk dies with the instance
volume_mount = "/workspace"
```

Rules that are enforced rather than suggested: `per_run_cap_usd` must not exceed `cap_usd`; both must
be greater than zero; an unknown key in `[budget]` or `[artefacts]` is an error at startup; an artefact
pattern may contain only plain glob characters, because the remote shell expands it.

If `[artefacts].pull` is non-empty, `run` pulls those patterns when each run finishes — whatever its
exit code — and `down` pulls again before it stops the machine. A failed pull at `down` does not cancel
the stop: it stops anyway, exits non-zero, and names the patterns that may still hold files, because a
machine left running to retry a copy is money spent by default. `down --keep-running` is the deliberate
opposite.

**A pull carries what its patterns declare.** A pattern in `run_patterns` names a collection of runs —
one directory per run, `runs/*` — and carries only the **newest** directory each pull; `rentguard pull
--all` fetches the rest while the machine is up. Every other pattern carries everything it matches. An
undeclared pattern that matches several directories says so in the output, with the declaration that
would trim it. The pull first runs an inventory on the machine, and its ssh bound is then
`max(pull_timeout_s, volume / pull_bytes_per_second)`. See [Limitations](limitations.md) for the two
bounds of the newest-directory rule.

### A config that refuses to rent

Configs are found in the **current directory**, which is not always the directory you meant: an MCP
client starts where its own configuration says, and a `rentguard.toml` that happens to live there then
decides the rules for everything started beside it. So a config can say "not here":

```toml
allow_renting = false
renting_disabled_reason = "this checkout is a template"
```

```console
$ rentguard up --purpose x
error: `up` did nothing and no money was spent — the config <project>\rentguard.toml sets allow_renting = false, so this file refuses to rent.
       the reason it gives: this checkout is a template
       --force does not override this. Rent from the config that owns the money, or set allow_renting = true in this one deliberately.
[exit 2]
```

`up` and `adopt` refuse **before a provider is built or a row opened**, and exit 2 (`--json`:
`action: "refused"`, `reason: "renting-disabled"`). The default is `true`, and a non-boolean is refused
at load rather than read as yes. `doctor` reports the refusal as a `renting-disabled` warning;
`ledger`, `status` and **`down`** are never gated, because a live machine must stay stoppable whatever
its config says. Keep the key above the first `[table]`: below one, TOML files it inside that table and
nothing reads it.

Put the API key in the environment, never in the file:

```bash
set RUNPOD_API_KEY=...           # Windows
export RUNPOD_API_KEY=...        # Linux, macOS
```

## 3. Check the setup

```console
$ rentguard doctor
rentguard      : v0.1.0
config         : <project>\rentguard.toml
state dir      : <project>\.rentguard
provider       : fake  (in-memory: no network, no spend)
api key        : present  (not needed for the fake)
open run       : (none)
ledger lines   : all readable
account pods   : 0 total, 0 live
  (nothing is billing)
guard heartbeat: NO GUARD — nothing is watching this instance, and nothing is billing on the account
spend recorded : $0.00 closed + $0.00 accruing = $0.00 of a $25.00 cap (0 run(s))
network volumes: 0  ($0.00/month, billed while nothing runs)
[exit 0]
```

Without a key, `doctor` prints `ERROR no-api-key` and exits 1 — deliberately, so a script cannot read a
healthy report from an unconfigured machine. With a key it also lists the account's instances and turns
problems into `ERROR <code>: <message>` lines and a non-zero exit:

```console
account pods   : 1 total, 1 live
  fake-1  RUNNING      $0.13/h  NVIDIA GeForce RTX 4090      UNTRACKED  name='fake-1'
spend recorded : $0.00 closed + $0.00 accruing = $0.00 of a $25.00 cap (0 run(s))
network volumes: 0  ($0.00/month, billed while nothing runs)

  ERROR live-instance-no-ledger-row: 1 live instance(s) with no ledger row and no project's name: fake-1.
  Either `rentguard adopt <id> --purpose ...` or stop them.
```

That is the failure this tool exists to catch: an instance billing that nothing remembers renting.
`rentguard adopt <id> --purpose "..."` gives it a row, backdated to the provider's uptime so the money
already spent is recorded rather than hidden. A machine carrying **another project's** name is the softer
case: `doctor` warns, because that project's ledger owns it, and `down` will not stop a machine someone
else is paying for.

`adopt --uptime-hours` is an estimate for a machine whose report is gone. It must be a positive finite
number of hours that fits inside the machine's life, or the row is refused rather than opened at a start
that records nothing:

```console
$ rentguard adopt fake-1 --purpose "estimate" --uptime-hours 0
note           : uptime given as 0.0 h, not read from the provider
error: --uptime-hours 0.0 is not a length of time, so a row starting there would record nothing for a machine that is billing: 0 starts the row now, a negative value starts it in the future, where it never accrues, and 0.0 is not a finite number of hours at all. Pass a positive number of hours — the smallest sensible value is just above 0 (0.01 h is 36 s); this machine is at most 21.4326 h old, and any estimate inside its life is accepted.
[exit 2]
```

Zero, a negative, `nan` and `inf` are one class: not a length of time.

## 4. Rent a machine

```bash
rentguard up --purpose "baseline" --cap 5
```

`--dry-run` decides before any `create` call, so it makes no rental at all:

```console
$ rentguard up --purpose dry --dry-run
dry run: would request NVIDIA GeForce RTX 4090 (then NVIDIA L40S, NVIDIA RTX A6000); nothing was rented and nothing was spent
[exit 0]

$ rentguard ledger
no runs recorded
```

`--cap` sets the project ceiling for this and later runs; `--purpose` is what the ledger row will say.
`--per-run-cap` sets the single-rental ceiling. `up` writes the ledger row **before** it rents, marked
provisional, so an interruption between the two cannot leave a machine billing with nothing recording
it; the row is corrected with the instance id and the pod's real rate once the provider names the
machine. If every candidate GPU is refused, the row is voided instead. The output names the GPU it
chose, the row, the `ssh` command to reach the machine, and the cap that now applies:

```console
$ rentguard up --purpose "quickstart demo"
ledger row     : 20261025T144218Z-735d12  (provisional: opened before the machine exists)
requesting NVIDIA GeForce RTX 4090
instance       : fake-1  $0.13/h
ssh            : ssh fake@127.0.0.1

cap            : $25.00 total, $5.00 per run, 6.0 h max
next           : `rentguard watch` in another terminal, or start it with nohup. Until something is watching, only you are.
[exit 0]
```

`up` refuses when renting would add a bill nobody is tracking. Recorded spend at the cap:

```console
$ rentguard up --purpose "again"
error: $5.24 already recorded (including the open run's accrued cost) against a $5.00 cap. Raise the cap deliberately (--cap N) or pass --force. This is a money decision.
[exit 2]
```

A recorded instance that contradicts the config — a different cloud tier, or a card not in
`gpu_preference`:

```console
$ rentguard up --purpose "baseline"
error: the recorded instance is not what this config asks for, so it will not be started:
  gpu: the config asks NVIDIA GeForce RTX 4090, this instance is 'NVIDIA GeForce RTX 3080 Ti'
  terminate it (`rentguard down`) and rent the config's card, or pass --force to start it anyway — the override is recorded
[exit 2]
```

A machine it cannot account for — one that may still be billing and has no ledger row here. **"May
still be billing" is everything except `EXITED`, `TERMINATED`, or gone**, including a status this tool
has never heard of:

```console
$ rentguard up --purpose "beside an error pod"
error: 1 instance(s) on the account may still be billing with no ledger row here: fake-1. Renting again could start a second bill.
       stop it (`rentguard down`), record it (`rentguard adopt fake-1 --purpose ...`), or pass --force to rent anyway — a status that is not EXITED/TERMINATED counts as billing, including a STOPPING machine that is about to finish.
[exit 2]
```

The cost is real and deliberate: a pod that is `STOPPING` after a `down` blocks a rent for those seconds,
and an `ERROR` pod that never billed blocks it until it is terminated or you pass `--force`. A false
refusal is loud and takes one word; a silent billing machine is neither.

A **resume** is held to the same rule as a fresh rent. When `up` finds a machine it already recorded, it
takes a second liveness reading before calling it a rental: a machine that is not usable is stopped, its
row is closed, and the command exits 1 with `unusable`. While a resume waits for the machine to answer, a
guard is armed, so the wait cannot bill unwatched.

## 5. Start the guard

In a second terminal:

```bash
rentguard watch
```

This is the process that stops the machine. It polls every 60 seconds by default and stops when the
total cap is crossed, the per-run cap is crossed, the run exceeds `max_hours_per_run`, the GPU has been
idle for `idle_minutes`, or the instance is already stopped. To outlive your shell:

```bash
nohup rentguard watch > guard.log 2>&1 &      # Linux, macOS
start /b rentguard watch > guard.log           # Windows cmd
```

Then check it, because a supervisor you believe in and that is not running is worse than none:

```console
$ rentguard status
guard          : alive (pid <pid>, heartbeat 0.3 min ago)
```

`alive` needs a **running process that has not marked itself finished**, so a guard that has ended is
never reported as protection. The same line reads, in the other cases:

```console
guard          : finished (last heartbeat 0.1 min ago) — nothing is watching this instance, and it has stopped (EXITED), so nothing is billing
guard          : not running (pid <pid> is gone; last heartbeat 12.4 min ago) — nothing is watching this instance, so it will bill until someone stops it
guard          : NO GUARD — nothing is watching this instance, and nothing is billing on the account
```

The tail follows the **machine**: a stopped instance cannot bill, a running one can, and a state that
could not be established keeps the warning. The age is printed as the secondary signal; it is not the
verdict.

A bounded watch ends protection at its bound:

```console
$ rentguard watch --max-iterations 3 --interval 30
...
guard finished: guard-iteration-limit
protection : NOT armed now — a watch stops when it returns, so run it again (or keep `rentguard watch` running in a terminal) or the machine is unguarded
[exit 2]
```

Make `max_iterations × interval` outlast the window you are guarding. `watch --once` takes a single
reading and reports the decision; it does not claim protection.

## 6. Run a command

```bash
rentguard run -- python train.py
```

The command runs over `ssh` in `remote_dir` (`/workspace` by default), and its exit code becomes `run`'s
exit code. Whenever `[artefacts].pull` is non-empty, those patterns are pulled immediately after it
returns — including after a failure, which is usually when you want them most. If the instance
disappeared mid-run, `run` says the artefacts are lost and exits 1 rather than reporting a clean success.

A pull that **matched nothing** does not fail the command: a command that only reads, stages or
validates legitimately produces no artefacts, and `run` still reports
`pull_reason: "patterns-matched-nothing"` so the empty delivery is visible. If you want an empty
delivery to fail the run anyway, ask for it:

```bash
rentguard run --require-artefacts -- python validate.py
```

`--require-artefacts` makes the run fail unless the pull delivered something; a pull that *broke* fails
the run either way, because there the files may exist and be lost. `--workdir` sets the remote working
directory.

A machine that reports `RUNNING` with zero uptime and no ports is not usable, and neither is an expired
one. `run` and `pull` read the machine **three times, 45 s apart**, and refuse only if the last read
still disagrees:

```console
$ rentguard run -- echo hi
usable         : read 1 of 3 reports EXITED with uptime=421, ports=NULL — reading again in 45s before refusing (one stale listing is not a crash loop)
usable         : read 2 of 3 reports EXITED with uptime=466, ports=NULL — reading again in 45s before refusing (one stale listing is not a crash loop)
error: instance fake-1 reports EXITED but is not usable on 3 reads, 45s apart (last read: uptime=511, ports=NULL). A pod that stays RUNNING with uptime 0 and null ports is crash-looping. Not uploading to it.
[exit 1]
```

## 7. Get the files back

```bash
rentguard pull
```

```console
$ rentguard pull
pulled 12 file(s) into <project>\artefacts
  results/baseline.json
  logs/train.log
  ...
```

Artefacts are pulled with `tar` over `ssh` into `local_dir`. `down` pulls once more before it stops the
machine, so the last run's results are taken even if you only call `down`. `pull --all` carries every
matched entry instead of only the newest directory of a declared run collection; it can be very large,
because it transfers everything ever written.

**If there is no network volume, the container disk dies with the instance** — a stopped pod is a fresh
container next time, with none of your files. Pull, or attach a volume.

## 8. Stop and read the bill

```bash
rentguard down
```

Stops the instance and closes the ledger row with its wall-clock cost. If `[artefacts].pull` is set it
pulls first; a failed pull still stops the machine, exits non-zero and names what may be lost.

**The row is closed only when the account agrees the machine has finished.** Two witnesses are consulted
— the instance's own record and the account's listing — and finished means `EXITED`, `TERMINATED`, or
gone. Every other status counts as still billing, including one this tool has never heard of, so a stop
the provider did not honour, a machine caught in `STOPPING`, or a **crash-looping** pod all leave the row
**open** and exit 1 with `action: "still-billing"`; the accrual keeps counting and the cap keeps seeing
it. The machine the **open row** names is confirmed the same way, even when it is not the one this call
stopped, and `down` does not stop that machine for you.

```console
$ rentguard down
pulling artefacts from fake-1 before stopping it
pulled 0 file(s) into <project>\artefacts
pulled 0 file(s) into <project>\artefacts — the configured patterns matched nothing (results/**/*.json, logs/*.log)
stopping fake-1: stopped
confirmed      : the account lists none of them as billing
run closed     : 20261025T144218Z-735d12  0.0006 h  $7.2e-05
total spend    : $0.00 of $25.00
[exit 0]
```

Then:

```console
$ rentguard ledger
run                    gpu                          hours      usd  closed by          purpose
----------------------------------------------------------------------------------------------------------------------
20261025T144218Z-735d1 NVIDIA GeForce RTX 4090       0.00   0.0001  down               quickstart demo
----------------------------------------------------------------------------------------------------------------------
total                                                0.00   0.0001
cap: $25.00   remaining: $25.00
storage: NOT included above — network volumes bill monthly while nothing runs; `rentguard doctor` lists them with their cost
```

The arithmetic is `rate × wall clock`, recomputed from the row's own start and stop times when the row
is closed. It is an estimate of the instance's hourly cost, not the provider's invoice — see
[what the cap guarantees](cap.md).

## 9. Rehearse without paying

`RENTGUARD_PROVIDER=fake` swaps the provider for an in-memory one: no network, no key, no money. It
writes its account to `.rentguard/fake-state.json`, so a command in one shell sees what the previous one
rented — the sequence below spans several processes. The fake's catalogue answers to the same GPU names
as RunPod's, so nothing needs adding to the config.

```console
$ RENTGUARD_PROVIDER=fake rentguard up --purpose "rehearsal"
ledger row     : 20261025T144218Z-735d12  (provisional: opened before the machine exists)
requesting NVIDIA GeForce RTX 4090
instance       : fake-1  $0.13/h
ssh            : ssh fake@127.0.0.1

cap            : $25.00 total, $5.00 per run, 6.0 h max
next           : `rentguard watch` in another terminal, or start it with nohup. Until something is watching, only you are.

$ RENTGUARD_PROVIDER=fake rentguard up --purpose "second attempt"
instance fake-1 is already RUNNING
ssh            : ssh fake@127.0.0.1

$ RENTGUARD_PROVIDER=fake rentguard down
pulling artefacts from fake-1 before stopping it
...
run closed     : 20261025T144218Z-735d12  0.0006 h  $7.2e-05
total spend    : $0.00 of $25.00
```

The second `up` is the point: the account persisted between processes, and the tool reused `fake-1`
instead of renting a second machine. For the fake's failure modes — a machine that vanishes, a crash
loop, utilisation that drops to zero while the meter runs — see [Test the cap without
spending](fake.md).

## When nothing is rented

With the provider reachable and nothing rented, `status`, `down` and `ledger` exit 0; `watch`, `run`,
`pull` and `adopt` exit 1. `ledger` works without a key, and `doctor` works without a key but exits 1.

```console
$ rentguard status
no instance recorded
spend so far   : $0.00 of $5.00

$ rentguard watch
no instance recorded; nothing to watch

$ rentguard down
nothing to do

$ rentguard run -- echo hi
error: nothing is rented. `rentguard up` first.

$ rentguard pull
error: nothing is rented

$ rentguard adopt nosuchpod123
error: no instance nosuchpod123
```
