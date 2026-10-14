# Quickstart

Install, write one config file, rent a small GPU, run a command, get the files back, see what it
cost, and have the machine stop itself.

Time: about ten minutes, most of it waiting for the pod to boot. Money: whatever your chosen GPU
costs per hour, capped by the `cap_usd` you set.

The free commands below were run for this page against commit `2ebe660`; their real output is in
[VERIFICATION.md](VERIFICATION.md). The paid step was run separately as milestone M2 — see
[M2 — the real rental](M2-REAL-RENTAL.md) — and this page marks which transcripts are free and which
are M2's.

## 1. Install

```bash
git clone <repository-url>
cd rentguard
python -m pip install .
rentguard --version
```

No PyPI package exists yet. From a checkout, `PYTHONPATH` works as well:

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

[budget]
cap_usd = 25.0            # hard ceiling for the whole project, across all runs
per_run_cap_usd = 5.0     # ceiling for a single open run
max_hours_per_run = 6.0
idle_minutes = 10         # stop after this long with the GPU idle; 0 disables

[artefacts]
pull = ["results/**/*.json", "logs/*.log"]
local_dir = "./artefacts"
remote_dir = "/workspace"

[runpod]
gpu_preference = ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A6000"]
volume_id = ""            # empty: no network volume, so the container disk dies with the instance

state_dir = ".rentguard"
```

If `[artefacts].pull` is non-empty, `run` pulls those patterns when each run finishes — whatever its
exit code — and `down` pulls again before it stops the machine. A failed pull at `down` does not cancel
the stop: it stops anyway, exits non-zero, and prints which patterns were lost, because a machine left
running to retry a copy is money spent by default. Note that the generated file says `volume_name`
where the code reads `volume_id` — edit it if you need a volume. See [limitations](limitations.md).

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
spend recorded : $0.00 of a $25.00 cap (0 run(s))
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
  ea0si2rzoyw8e4  RUNNING      $0.13/h  NVIDIA GeForce RTX 3070        UNTRACKED

  ERROR live-instance-no-ledger-row: 1 live instance(s) with no ledger row: ea0si2rzoyw8e4.
  Either `rentguard adopt <id> --purpose ...` or stop them.
```

That is the failure this tool exists to catch: an instance billing that nothing remembers renting.
`rentguard adopt <id> --purpose "..."` gives it a row, backdated to the provider's uptime so the money
already spent is recorded rather than hidden.

## 4. Rent a machine

Then rent:

```bash
rentguard up --purpose "baseline" --cap 5
```

**Do not use `up --dry-run` on this revision.** It calls the provider's `create` before it checks the
dry-run flag, so it can rent a machine and still print "nothing was rented". At commit `2ebe660` the
same code made three create attempts against RunPod (all refused) during a "dry" run, and left
`fake-1` behind against the fake. See [limitations](limitations.md).

`--cap` sets the project ceiling for this and later runs; `--purpose` is what the ledger row will say.
The output names the GPU it chose, the instance id and its hourly rate, the ledger row it opened, the
`ssh` command to reach the machine, and the cap that now applies.

The paid path was run as milestone M2 on 2026-10-14: twice, $0.017 in total, the second pass green with
the artefact pulled from the right machine and the ledger reconciled by hand. The transcripts on this
page are still the free ones — see [M2 — the real rental](M2-REAL-RENTAL.md).

`up` refuses when renting would add a bill nobody is tracking:

```console
$ rentguard up --purpose "again"
error: $5.24 already recorded (including the open run's accrued cost) against a $5.00 cap. Raise the cap deliberately (--cap N) or pass --force. This is a money decision.
```

Exit code 2. `--force` (or a deliberately higher `--cap`) is the way past it.

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
lost. Output from the free rehearsal in §9:

```console
$ rentguard down
pulling artefacts from fake-1 before stopping it
pulled 0 file(s) into C:\work\artefacts
stopping fake-1
run closed     : 20261014T131911Z-2ff393  0.001 h  $0.000144
total spend    : $0.00 of $5.00
```

Then:

```console
$ rentguard ledger

run                    gpu                          hours      usd  closed by          purpose
----------------------------------------------------------------------------------------------------------------------
20261014T131911Z-2ff39 Fake GPU 24GB                 0.00   0.0001  down               doc rehearsal
----------------------------------------------------------------------------------------------------------------------
total                                                0.00   0.0001
cap: $5.00   remaining: $5.00
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
one rented — the sequence below spans several processes. Add `gpu_preference = ["Fake GPU 24GB"]` to
`[runpod]` so the fake has something to rent.

```console
$ rentguard doctor
provider       : fake  (in-memory: no network, no spend)
api key        : MISSING ($DSH_SECRET_RUNPOD_API_KEY)  (not needed for the fake)
...
account pods   : 0 total, 0 live
  (nothing is billing)
```

`doctor` exits 0 on a clean root.

```console
$ rentguard up --purpose "doc rehearsal"
requesting Fake GPU 24GB
instance       : fake-1  $0.13/h
ledger row     : 20261014T131911Z-2ff393
ssh            : ssh fake@127.0.0.1

cap            : $5.00 total, $2.00 per run, 6.0 h max
next           : `rentguard watch` in another terminal, or start it with nohup. Until something is watching, only you are.

$ rentguard up --purpose "second attempt"
instance fake-1 is already RUNNING
ssh            : ssh fake@127.0.0.1

$ rentguard status
instance       : fake-1  RUNNING
gpu            : Fake GPU 24GB  $0.13/h
uptime         : 0.00 h   gpu util 0.0%
this run       : $0.000 of $2.00
all runs       : $0.000 of $5.00  (~2307.6 min to the cap at this rate)
guard          : NO HEARTBEAT — nothing is watching this instance

$ rentguard down
pulling artefacts from fake-1 before stopping it
pulled 0 file(s) into C:\work\artefacts
stopping fake-1
run closed     : 20261014T131911Z-2ff393  0.001 h  $0.000144
total spend    : $0.00 of $5.00
```

The second `up` is the point: the account persisted between processes, and the tool reused `fake-1`
instead of renting a second machine. For the fake's failure modes — a machine that vanishes, a crash
loop, utilisation that drops to zero while the meter runs — see
[Test the cap without spending](fake.md).
