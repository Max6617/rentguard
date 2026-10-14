# rentguard

Rent a GPU with a hard spend cap, a per-run cost ledger, and artefact pull.

`rentguard` is a command-line tool for renting GPUs by the hour without watching the bill. It rents a
machine on RunPod, opens a ledger row before it rents, runs commands over ssh, pulls the
results back, and stops the machine when the money or the idle time runs out.

Version 0.1. RunPod only, Python 3.11+, standard library only. Not published yet — install from the
checkout, below.

## Install

```bash
git clone <repository-url>
cd rentguard
python -m pip install .
rentguard --version
```

Until the first release there is no PyPI package, so a checkout plus `PYTHONPATH` works too:

```bash
set PYTHONPATH=C:\path\to\the\checkout      # Windows
export PYTHONPATH=/path/to/the/checkout     # Linux, macOS
```

## A first capped rental

```bash
rentguard init                            # write a commented rentguard.toml
rentguard doctor                          # config, key, spend, live instances, drift
rentguard up --purpose "baseline" --cap 5 # rent; cap this project at $5
rentguard watch                           # in a second terminal: stop at the cap or on idle
rentguard run -- python train.py          # run remotely over ssh
rentguard status                          # uptime, this run's cost, total, guard heartbeat
rentguard pull                            # pull the configured artefacts
rentguard down                            # stop the machine, close the row, record the cost
rentguard ledger                          # every run with its wall clock and dollars
```

`watch` is a foreground process and it is the thing that stops the machine. If it is not running, the
cap does not fire. `status` tells you whether its heartbeat is fresh.

A machine can be billing that the ledger has no row for — a pod started somewhere else, or one whose
state file was lost. `rentguard adopt <id> --purpose "..."` records it, backdated from the provider's
own uptime, so the money already spent is recorded rather than hidden. `doctor` is what finds those:

```console
$ rentguard doctor
account pods   : 1 total, 1 live
  orphan-1  RUNNING      $2.0/h  Fake GPU 24GB                UNTRACKED

  ERROR live-instance-no-ledger-row: 1 live instance(s) with no ledger row: orphan-1. Either `rentguard adopt <id> --purpose ...` or stop them.

$ rentguard adopt orphan-1 --purpose "found billing"
adopted        : orphan-1  RUNNING  $2.0/h
row            : 20261014T122521Z-4b21b2
backdated start: 2026-10-14T12:25:21+00:00  (uptime 1.50 h from the provider)
cost already on the clock: $3.00 — this is spend that happened before the ledger knew about it, and it is now recorded rather than hidden
```

`down` pulls the configured artefacts before it stops the machine. If that pull fails it stops anyway,
names the paths that were lost, and exits non-zero; `down --keep-running` is the deliberate opposite —
leave the machine up to retry the copy, knowing it keeps billing.

No account yet? Every command also runs against an in-memory provider — real behaviour, no network, no
key, no money: [test the cap without spending](docs/fake.md).

Step by step, with the output of every command: [quickstart](docs/quickstart.md). Every command also
takes `--json`: stdout then carries exactly one JSON document and the human text goes to stderr, so
`json.loads(stdout)` works.

## The cap

The cap is a dollar figure in `rentguard.toml`. It is enforced in two places, both outside the
provider:

- before renting, `up` refuses when the recorded spend reaches the cap, when a live instance has no
  ledger row, or when an open row names an instance that is not there;
- while renting, `watch` stops the instance when the total or the per-run cap is crossed, when the
  run exceeds its maximum hours, or when the GPU has been idle for the idle window.

What that does and does not cover — storage, egress, provider billing lag, the polling interval, a
guard that is not running — is written out in [what the cap guarantees](docs/cap.md). Read it before
trusting the number with money you cannot lose.

## Configuration

`rentguard init` writes this file (values shown are the defaults); `rentguard init --print` shows it
without writing. It is read from `./rentguard.toml` or `~/.config/rentguard/config.toml`:

```toml
provider = "runpod"       # or "fake": in-memory, no network, no money
state_dir = ".rentguard"  # top-level keys go ABOVE the first [table], or they land inside it

[budget]
cap_usd = 25.0            # hard ceiling for the whole project, across all runs
per_run_cap_usd = 5.0     # ceiling for a single open run
max_hours_per_run = 6.0   # wall-clock ceiling for one rental
idle_minutes = 10         # stop after this long with the GPU idle; 0 disables

[artefacts]
pull = ["results/**/*.json", "logs/*.log"]
local_dir = "./artefacts"
remote_dir = "/workspace"

[runpod]
cloud = "SECURE"
image = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
container_disk_gb = 50
gpu_preference = ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A6000"]
volume_id = ""            # empty: no network volume, so the container disk dies with the instance
volume_mount = "/workspace"
```

The API key comes from the environment, never from the file: `DSH_SECRET_RUNPOD_API_KEY` (a saved
secret appears as this name) or `RUNPOD_API_KEY`.

Every key in `[budget]` and `[artefacts]` is checked; a typo there is an error at startup. The
`[runpod]` section is passed to the provider as-is, so a typo in it is ignored silently — check the
spelling of anything you add there. Known rough edges are collected in
[limitations](docs/limitations.md).

## Documentation

- [Quickstart](docs/quickstart.md) — install to a closed ledger row, command by command.
- [What the cap guarantees](docs/cap.md) — the exact list, and the exact holes.
- [Providers](docs/providers.md) — the interface, the in-memory fake, and how to add Vast or Lambda.
- [Test the cap without spending](docs/fake.md) — the fake provider, and a free demonstration.
- [MCP server](docs/mcp.md) — running rentguard as a tool for a Bot team, and the approval rule.
- [Limitations](docs/limitations.md) — what is not true about this tool yet.
- [Verification](docs/VERIFICATION.md) — the commands behind these pages, with their real output.
- [Known failures](docs/KNOWN-FAILURES.md) — open defects with reproductions.
- [Publish checklist](docs/PUBLISH-CHECKLIST.md) — the definition of release-ready.

## Licence

MIT.
