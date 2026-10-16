# Quickstart

Install, write one config file, rent a small GPU, run a command, get the files back, see what it
cost, and have the machine stop itself.

Time: about ten minutes, most of it waiting for the pod to boot. Money: whatever your chosen GPU
costs per hour, capped by the `cap_usd` you set.

**The install evidence in §1 was re-run at `51e350e`; the free rehearsal in "Rehearse without paying"
was run at `e53f544`** (both 2026-10-15), and each says which. The money-path behaviours in §3
(`adopt --uptime-hours`), §4 (`up` beside a machine that may still bill, and the resume) and §8 (`down`'s
closure) describe **`0d01d57`** (2026-10-16), and the transcripts beside them were run at that revision.
The command transcripts are the free fake-provider ones and their real output is in
[VERIFICATION.md](VERIFICATION.md). The paid step was run separately as milestone M2 — see
[M2 — the real rental](M2-REAL-RENTAL.md) — and this page marks which transcripts are free and which
are M2's.

## 1. Install

The repository is public — <https://github.com/Max6617/rentguard> — but there is no PyPI package yet, so
install from a checkout:

```bash
git clone https://github.com/Max6617/rentguard
cd rentguard
python -m pip install .
rentguard --version
```

**What was actually run here, at `51e350e` on Python 3.12.6**, is the wheel below, built with the host
interpreter and installed into a fresh virtual environment. `python -m pip install .` itself could not be
run on this machine: there is no route to PyPI, so pip's isolated build waits at
`Installing build dependencies: started` (measured: still there after 7 minutes, then killed) because it
cannot fetch `setuptools>=68`. With network, that is the one command you need, and CI runs
`python -m pip install -e .` plus `rentguard --version` on every push.

```console
$ python -m pip wheel . --no-deps --no-build-isolation -w dist
Building wheels for collected packages: rentguard
  Created wheel for rentguard: filename=rentguard-0.1.0-py3-none-any.whl size=85921 sha256=ac4116b2…
Successfully built rentguard
[20 s, exit 0]

$ python -m pip install --no-index --no-deps dist/rentguard-0.1.0-py3-none-any.whl
Successfully installed rentguard-0.1.0

$ rentguard --version
rentguard 0.1.0
```

`--no-build-isolation` uses the `setuptools` already installed instead of fetching one; `--no-index
--no-deps` installs the wheel with nothing to resolve, which is correct because rentguard has no
dependencies. Both flags are for this host only — a machine with PyPI does not need either. Installed
that way, `rentguard doctor --json` works against a root that has a ledger:

```console
$ rentguard doctor --json
{
  "command": "doctor",
  "version": "0.1.0",
  ...
  "open_run": {"run_id": "20261015T210041Z-383698", "purpose": "install rerun", ...},
  "spend": {"recorded_usd": 0.0, "accruing_usd": 8.6e-05, "total_usd": 8.6e-05, ...},
  "findings": []
}
[exit 0]
```

No PyPI package exists, so a checkout plus `PYTHONPATH` works as well:

```bash
set PYTHONPATH=C:\path\to\the\checkout      # Windows
export PYTHONPATH=/path/to/the/checkout     # Linux, macOS
```

## 2. Write the config

```console
$ rentguard init
wrote C:\work\rentguard.toml
next           : `rentguard doctor`, then `rentguard up --purpose ...`
```

It writes every default, commented, so nothing starts from an empty file. `rentguard init --print`
shows the same text without writing. The parts worth reading:

```toml
provider = "runpod"       # or "fake": in-memory, no network, no money
state_dir = ".rentguard"  # top-level keys go ABOVE the first [table], or they land inside it

[budget]
cap_usd = 25.0            # hard ceiling for the whole project, across all runs
per_run_cap_usd = 5.0     # ceiling for a single open run
max_hours_per_run = 6.0
idle_minutes = 10         # stop after this long with the GPU idle; 0 disables
stop_timeout_s = 45       # a stop slower than this escalates to the provider's stop endpoint

[artefacts]
pull = ["results/**/*.json", "logs/*.log"]
local_dir = "./artefacts"
remote_dir = "/workspace"
pull_timeout_s = 120      # one ssh pull may take this long, then it is killed and reported

[runpod]
gpu_preference = ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A6000"]
volume_id = ""            # empty: no network volume, so the container disk dies with the instance
volume_mount = "/workspace"
```

The generated file is valid TOML with the keys the rent path actually reads (`volume_id`, not the old
`volume_name` that used to be written and silently ignored). If `[artefacts].pull` is non-empty, `run`
pulls those patterns when each run finishes — whatever its exit code — and `down` pulls again before it
stops the machine. A failed pull at `down` does not cancel the stop: it stops anyway, exits non-zero, and
prints which patterns were lost, because a machine left running to retry a copy is money spent by
default. `down --keep-running` is the deliberate opposite. See [limitations](limitations.md).

Rules that are enforced rather than suggested: `per_run_cap_usd` must not exceed `cap_usd`; both must
be greater than zero; an unknown key in `[budget]` or `[artefacts]` is an error at startup.

Put the API key in the environment, never in the file:

```bash
set DSH_SECRET_RUNPOD_API_KEY=...           # Windows (a saved secret appears as this name)
export DSH_SECRET_RUNPOD_API_KEY=...        # Linux, macOS
```

`RUNPOD_API_KEY` is also accepted.

## 3. Check the setup

```console
$ rentguard doctor
rentguard      : v0.1.0
config         : C:\work\rentguard.toml
state dir      : C:\work\.rentguard
provider       : runpod
api key        : MISSING ($DSH_SECRET_RUNPOD_API_KEY)
open run       : (none)
ledger lines   : all readable
guard heartbeat: (never)  <-- STALE, nothing is watching

  ERROR no-api-key: no API key: the account cannot be inspected
```

Without a key `doctor` prints that finding and exits 1 — deliberately, so a script cannot read a
healthy report from an unconfigured machine. With a key it also lists the account's instances, and
turns problems into `ERROR <code>: <message>` lines and a non-zero exit:

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
already spent is recorded rather than hidden. A live machine carrying **another project's** name is the
softer case: `doctor` warns, because that project's ledger owns it, and `down` will not stop a machine
someone else is paying for.

`adopt --uptime-hours` is an **estimate**, for a machine whose report is gone — the provider says
nothing, so you supply the hours. It must be a positive finite number that fits inside the machine's
life, or the row is refused rather than opened at a start that records nothing. Zero, a negative, `nan`
and `inf` are one class: not a length of time.

```console
$ rentguard adopt fake-1 --purpose "estimate" --uptime-hours 0
note           : uptime given as 0.0 h, not read from the provider
error: --uptime-hours 0.0 is not a length of time, so a row starting there would record nothing for a machine that is billing: 0 starts the row now, a negative value starts it in the future, where it never accrues, and 0.0 is not a finite number of hours at all. Pass a positive number of hours — the smallest sensible value is just above 0 (0.01 h is 36 s); this machine is at most 21.4326 h old, and any estimate inside its life is accepted.
[exit 2]
```

## 4. Rent a machine

Then rent:

```bash
rentguard up --purpose "baseline" --cap 5
```

`--dry-run` is safe again as of `ce69cea`: it decides before any `create` call, so it makes no rental at
all. It once did — three real create attempts went out against RunPod, and the fake was left holding
`fake-1` — and that record is in [limitations](limitations.md). The test now asserts on the provider
call, not on the sentence. Run free against the fake at `e53f544`:

```console
$ rentguard up --purpose dry --dry-run
dry run: would request NVIDIA GeForce RTX 4090 (then NVIDIA L40S, NVIDIA RTX A6000); nothing was rented and nothing was spent
[exit 0]

$ rentguard ledger
no runs recorded
```

`--cap` sets the project ceiling for this and later runs; `--purpose` is what the ledger row will say.
`up` writes the ledger row **before** it rents, marked provisional, so an interruption between the two
cannot leave a machine billing with nothing recording it; the row is corrected with the instance id and
the pod's real rate once the provider names the machine. If every candidate GPU is refused, the row is
voided instead. The output names the GPU it chose, the row, the `ssh` command to reach the machine, and
the cap that now applies.

The paid path was run as milestone M2 on 2026-10-14: twice, $0.017 in total, the second pass green with
the artefact pulled from the right machine and the ledger reconciled by hand. The transcripts on this
page are still the free ones — see [M2 — the real rental](M2-REAL-RENTAL.md).

`up` refuses when renting would add a bill nobody is tracking:

```console
$ rentguard up --purpose "again"
error: $5.24 already recorded (including the open run's accrued cost) against a $5.00 cap. Raise the cap deliberately (--cap N) or pass --force. This is a money decision.
```

Exit code 2. `--force` (or a deliberately higher `--cap`) is the way past it.

`up` also refuses to reuse or resume a recorded instance that contradicts the config — a different cloud
tier, or a card that is not in `gpu_preference`:

```console
$ rentguard up --purpose "baseline"
error: the recorded instance is not what this config asks for, so it will not be started:
  gpu: the config asks NVIDIA GeForce RTX 4090, this instance is 'NVIDIA GeForce RTX 3080 Ti'
  terminate it (`rentguard down`) and rent the config's card, or pass --force to start it anyway — the override is recorded
```

Exit code 2. The config is the promise, and a substitution that nobody chose is how a window gets spent on
a card you did not ask for. `--force` overrides it, and the override is written into the state file as
well as printed, so it is visible after the fact.

`up` also refuses beside a machine it cannot account for — one that may still be billing and has no
ledger row here. **"May still be billing" is everything except `EXITED`, `TERMINATED`, or gone**,
including a status this tool has never heard of. It names the machine, and `--force` rents anyway:

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
row is closed, and the command exits 1 with `unusable` — `run` and `pull` would have refused it seconds
later. While a resume waits for the machine to answer, a guard is armed, so the wait cannot bill
unwatched. **Two holes in that path are open at `0d01d57`** and being fixed — a machine that vanishes
between the two readings is still reported `reused`, and a resume whose wait fails leaves the revived
machine billing with no row and no guard. Both are named with their reproductions in
[limitations](limitations.md); until they are green, a `reused` line is worth confirming with `rentguard
status`.

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
guard          : alive (heartbeat 0.3 min ago)
```

```console
guard          : NO HEARTBEAT — nothing is watching this instance
guard          : heartbeat 12.4 min old — the guard is probably dead; nothing is watching this instance
```

## 6. Run a command

```bash
rentguard run -- python train.py
```

The command runs over `ssh` in `remote_dir` (`/workspace` by default), and its exit code becomes
`run`'s exit code. Whenever `[artefacts].pull` is non-empty, those patterns are pulled immediately
after it returns — including after a failure, which is usually when you want them most. If the
instance disappeared mid-run, `run` says the artefacts are lost and exits 1 rather than reporting a
clean success.

A pull that **matched nothing** is not that case, and it does not fail the command: a command that only
reads, stages or validates legitimately produces no artefacts, and `run` still reports
`pull_reason: "patterns-matched-nothing"` so the empty delivery is visible. If you want an empty
delivery to fail the run anyway, ask for it:

```bash
rentguard run --require-artefacts -- python validate.py
```

`--require-artefacts` makes the run fail unless the pull delivered something; a pull that *broke* fails
the run either way, because there the files may exist and be lost.

If the machine reports `RUNNING` but has zero uptime and no ports, it is crash-looping and `run`
refuses to touch it:

```console
$ rentguard run -- python train.py
error: instance ea0si2rzoyw8e4 reports RUNNING but is not usable (uptime=0, ports=NULL).
A pod that stays RUNNING with uptime 0 and null ports is crash-looping. Not uploading to it.
```

## 7. Get the files back

```bash
rentguard pull
```

```console
pulled 12 file(s) into C:\work\artefacts
  results/baseline.json
  logs/train.log
  ...
```

Artefacts are pulled with `tar` over `ssh` into `local_dir`. `down` pulls once more before it stops the
machine, so the last run's results are taken even if you only call `down`. **If there is no network
volume, the container disk dies with the instance** — a stopped pod is a fresh container next time,
with none of your files. Pull, or attach a volume.

## 8. Stop and read the bill

```bash
rentguard down
```

Stops the instance and closes the ledger row with its wall-clock cost. If `[artefacts].pull` is set it
pulls first; a failed pull still stops the machine, exits non-zero and prints which patterns were
lost.

**The row is closed only when the account agrees the machine has finished.** Two witnesses are consulted
— the instance's own record and the account's listing — and finished means `EXITED`, `TERMINATED`, or
gone. Every other status counts as still billing, including one this tool has never heard of, so a stop
the provider did not honour, a machine caught in `STOPPING`, or a **crash-looping** pod all leave the row
**open** and exit 1 with `action: "still-billing"`; the accrual keeps counting and the cap keeps seeing
it. The machine the **open row** names is confirmed the same way, even when it is not the one this call
stopped, and `down` does not stop that machine for you.

Output from the free rehearsal in §9:

```console
$ rentguard down
pulling artefacts from fake-1 before stopping it
pulled 0 file(s) into C:\work\artefacts
stopping fake-1: stopped
run closed     : 20261015T202609Z-5b3160  0.0916 h  $0.011917
total spend    : $0.01 of $25.00
```

Then:

```console
$ rentguard ledger
run                    gpu                          hours      usd  closed by          purpose
----------------------------------------------------------------------------------------------------------------------
20261015T202609Z-5b316 NVIDIA GeForce RTX 4090       0.09   0.0119  down               doc rehearsal
----------------------------------------------------------------------------------------------------------------------
total                                                0.09   0.0119
cap: $25.00   remaining: $24.99
storage: NOT included above — network volumes bill monthly while nothing runs; `rentguard doctor` lists them with their cost
```

The arithmetic is `rate × wall clock`, recomputed from the row's own start and stop times when the row
is closed. It is an estimate of the instance's hourly cost, not the provider's invoice — see
[what the cap guarantees](cap.md).

## When nothing is rented

Without a key, the provider-backed commands refuse before they look at the state file:

```console
$ rentguard status
error: no API key available. Set $DSH_SECRET_RUNPOD_API_KEY or $RUNPOD_API_KEY, or set
RENTGUARD_PROVIDER=fake to run without one (provider runpod).
```

`ledger` works without a key, and `doctor` works without a key but exits 1. With a key (or the fake
provider) and nothing rented, they say so:

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

With the provider reachable and nothing rented, `status`, `down` and `ledger` exit 0; `watch`, `run`,
`pull` and `adopt` exit 1.

## Rehearse without paying

`RENTGUARD_PROVIDER=fake` swaps the provider for an in-memory one: no network, no key, no money. It
writes its account to `.rentguard/fake-state.json`, so a command in one shell sees what the previous
one rented — the sequence below spans several processes. The fake's catalogue answers to the same GPU
names as RunPod's, so nothing needs adding to the config.

This is the real output at `e53f544`, run from the console script installed in §1:

```console
$ rentguard doctor
provider       : fake  (in-memory: no network, no spend)
api key        : present  (not needed for the fake)
...
account pods   : 0 total, 0 live
  (nothing is billing)
spend recorded : $0.00 closed + $0.00 accruing = $0.00 of a $25.00 cap (0 run(s))
network volumes: 0  ($0.00/month, billed while nothing runs)
[exit 0]
```

```console
$ rentguard up --purpose "doc rehearsal"
ledger row     : 20261015T202609Z-5b3160  (provisional: opened before the machine exists)
requesting NVIDIA GeForce RTX 4090
instance       : fake-1  $0.13/h
ssh            : ssh fake@127.0.0.1

cap            : $25.00 total, $5.00 per run, 6.0 h max
next           : `rentguard watch` in another terminal, or start it with nohup. Until something is watching, only you are.

$ rentguard up --purpose "second attempt"
instance fake-1 is already RUNNING
ssh            : ssh fake@127.0.0.1

$ rentguard status
instance       : fake-1  RUNNING
gpu            : NVIDIA GeForce RTX 4090  $0.13/h
uptime         : 0.00 h   gpu util 0.0%
this run       : $0.000 of $5.00
all runs       : $0.000 of $25.00  (~11538.4 min to the cap at this rate)
guard          : NO GUARD — nothing is watching this instance, so it will bill until someone stops it

$ rentguard down
pulling artefacts from fake-1 before stopping it
pulled 0 file(s) into C:\work\artefacts
stopping fake-1: stopped
run closed     : 20261015T202609Z-5b3160  0.0916 h  $0.011917
total spend    : $0.01 of $25.00

$ rentguard watch                                # nothing left to guard
watching fake-1 — stop at $5.00 this run, $25.00 total, 6.0 h, or 10 min idle
  2026-10-15T20:31:41+00:00  EXITED     up  0.09h  run $0.000  total $0.012/$25.00  gpu 0.0%  idle 1m
guard finished: already stopped
the guard stopped the machine: already stopped
[exit 0]
```

The second `up` is the point: the account persisted between processes, and the tool reused `fake-1`
instead of renting a second machine. `watch` on a stopped instance takes one reading, writes the
heartbeat, and returns. For the fake's failure modes — a machine that vanishes, a crash loop,
utilisation that drops to zero while the meter runs — see
[Test the cap without spending](fake.md).
