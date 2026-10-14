# rentguard

Rent a GPU with a hard spend cap, a per-run cost ledger, and artefact pull.

`rentguard` is a command-line tool for renting GPUs by the hour without watching the bill. It rents a
machine on RunPod, opens a ledger row at the machine's own rate, runs commands over ssh, pulls the
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

No account yet? Every command also runs against an in-memory provider — real behaviour, no network, no
key, no money: [test the cap without spending](docs/fake.md).

Step by step, with the output of every command: [quickstart](docs/quickstart.md). Every command also
takes `--json`, which prints the human output and then one JSON document on stdout; `up` takes
`--dry-run` to check the cap and the GPU choice without renting.

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
cloud = "SECURE"
image = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
container_disk_gb = 50
gpu_preference = ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX A6000"]
volume_id = ""            # empty: no network volume, so the container disk dies with the instance

state_dir = ".rentguard"
```

The API key comes from the environment, never from the file: `DSH_SECRET_RUNPOD_API_KEY` (a saved
secret appears as this name) or `RUNPOD_API_KEY`.

Every key in `[budget]` and `[artefacts]` is checked; a typo there is an error at startup. The
`[runpod]` section is passed to the provider as-is, so a typo in it is ignored silently — including
`volume_name`, which the generated template still writes even though the rent path reads `volume_id`.
Known rough edges are collected in [limitations](docs/limitations.md).

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
