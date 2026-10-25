# rentguard

Rent a GPU with a hard spend cap, a per-run cost ledger, and artefact pull.

`rentguard` is a command-line tool for renting GPUs by the hour without watching the bill. It rents a
machine on RunPod, opens a ledger row before it rents, runs commands over ssh, pulls the results back as
each run finishes, and stops the machine when the money or the idle time runs out. The cap is enforced
by rentguard itself, not by the provider, and every dollar it records comes from its own ledger — so it
is honest about what it knows and about what it does not.

Version 0.1. RunPod only, Python 3.11+, standard library only.

## Install

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

Step by step, with the output of every command: [quickstart](docs/quickstart.md).

## What it guarantees

- **A hard cap you set.** `cap_usd` bounds the whole project and `per_run_cap_usd` bounds one rental.
  `up` refuses before it rents when the recorded spend would cross the cap; `watch` stops the machine
  when a poll crosses it. The cap is a number in your config, checked outside the provider.
- **A per-run ledger.** Every rental is a row: purpose, instance, GPU, rate, start and stop times, wall
  clock, and estimated cost. `ledger` and `ledger --json` are the record.
- **Artefacts pulled as each run finishes.** If `[artefacts].pull` is set, `run` pulls the configured
  patterns when each command returns and `down` pulls again before stopping. An instance without a
  network volume is a container: a restart discards everything on it.
- **The stop rules.** `watch` stops the machine on the total cap, the per-run cap, `max_hours_per_run`,
  an idle GPU window, or when the machine has already stopped. Each firing prints its reason.
- **A refusal instead of a guess.** A machine that may still be billing and has no ledger row here blocks
  a rent until you adopt it, stop it, or pass `--force`. A stop the provider did not honour leaves the
  ledger row open, so the cost keeps counting.

The cap only exists while `rentguard watch` runs: it is a foreground process, and `status` tells you
whether its heartbeat is a live process fact. Nothing starts or restarts it for you.

## Finding machines that nothing recorded

A machine can be billing that the ledger has no row for — a pod started somewhere else, or one whose
state file was lost. `doctor` is what finds those, and `adopt` is what records them, backdated from the
provider's own uptime so the money already spent is recorded rather than hidden:

```console
$ rentguard doctor
account pods   : 1 total, 1 live
  orphan-1  RUNNING      $2.0/h  Fake GPU 24GB                UNTRACKED  name='orphan-1'

  ERROR live-instance-no-ledger-row: 1 live instance(s) with no ledger row: orphan-1. Either `rentguard adopt <id> --purpose ...` or stop them.

$ rentguard adopt orphan-1 --purpose "found billing"
adopted        : orphan-1  RUNNING  $2.0/h
row            : 20261014T122521Z-4b21b2
backdated start: 2026-10-14T12:25:21+00:00  (uptime 1.50 h from the provider)
cost already on the clock: $3.00 — this is spend that happened before the ledger knew about it, and it is now recorded rather than hidden
```

`down` calls a window closed only when the account **agrees** the machine has stopped: it consults both
the instance's own record and the account's listing, and any disagreement keeps the row open (exit 1,
`action: "still-billing"`). "Stopped" means `EXITED`, `TERMINATED` or gone — every other status counts
as still billing. `down` pulls the configured artefacts before it stops the machine; if that pull fails
it stops anyway, names what may be lost, and exits non-zero. `down --keep-running` is the deliberate
opposite: leave the machine up to retry the copy, knowing it keeps billing.

## Two ways to run it

- **The CLI** — the ten commands above, with `--json` on every one. With `--json`, stdout carries
  exactly one JSON document and the human text goes to stderr, so `json.loads(stdout)` works.
- **The MCP server** — `python -m rentguard.mcp_server` exposes the same operations over stdio as eleven
  tools, so a team of agents can rent, watch and stop machines through one place that enforces the cap.
  See [MCP server](docs/mcp.md).

No account yet? Every command also runs against an in-memory provider — real behaviour, no network, no
key, no money: [test the cap without spending](docs/fake.md).

## Configuration

`rentguard init` writes the config (values shown are the defaults); `rentguard init --print` shows it
without writing. It is read from `./rentguard.toml` or `~/.config/rentguard/config.toml`:

```toml
provider = "runpod"       # or "fake": in-memory, no network, no money
state_dir = ".rentguard"  # top-level keys go ABOVE the first [table], or they land inside it
allow_renting = true      # false makes this file refuse to rent; `--force` does not override it

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

The API key comes from the environment, never from the file: `RUNPOD_API_KEY`.

Every key in `[budget]` and `[artefacts]` is checked; a typo there is an error at startup. The `[runpod]`
section is passed to the provider as-is, so a typo in it is ignored silently — check the spelling of
anything you add there.

## Documentation

- [Quickstart](docs/quickstart.md) — install to a closed ledger row, command by command.
- [What the cap guarantees](docs/cap.md) — what the cap enforces, what the guard is, and what it misses.
- [Providers](docs/providers.md) — the provider interface, the fake, and how to add another provider.
- [Test the cap without spending](docs/fake.md) — the in-memory provider and a free demonstration.
- [MCP server](docs/mcp.md) — running rentguard as a tool for a team of agents.
- [Limitations](docs/limitations.md) — every limit, stated plainly.

## Licence

MIT. See [LICENSE](LICENSE).
