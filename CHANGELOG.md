# Changelog

Notable changes, newest first. This project follows semantic versioning once it has users; before that,
every release is `0.x` and may change anything.

## 0.1.0 — 2026-10-25

First release. RunPod only, Python 3.11+, standard library only.

**What it does**

- `rentguard up` rents (or resumes) an instance, choosing the first preferred GPU that is actually
  offered in the configured cloud tier, and falls through to the next candidate when the provider refuses
  one. It writes the ledger row before it rents, so an interruption cannot leave a machine billing with
  nothing recording it.
- `rentguard run -- <command>` runs a command over ssh and pulls the configured artefacts as the run
  finishes — not only at the end, because an instance without a network volume is a container that a
  restart discards.
- `rentguard watch` is a foreground supervisor that stops the instance at the total cap, the per-run cap,
  the maximum hours, or after an idle window. `watch --once` takes a single reading; a bounded watch says
  when protection ends.
- `rentguard down` stops the instance and closes the ledger row with its real cost — only when the
  account agrees the machine has stopped. A stop the provider did not honour leaves the row open.
- `rentguard ledger` lists every rental with its wall clock and dollars.
- `rentguard doctor` reports recorded spend, the guard heartbeat, network volumes, and any live instance
  with no ledger row; it exits non-zero on an error so a script can gate on it.
- `rentguard adopt <id>` records an instance that is already running, backdated from the provider's own
  uptime, so money already spent is recorded rather than hidden.
- `rentguard status` reports uptime, this run's cost, the total against the cap, and the guard heartbeat
  as a live process fact.
- `rentguard pull` pulls the configured artefacts now; `pull --all` carries every matched entry.
- `rentguard init` writes a commented config, with every default, and `--print` shows it without writing.
- Every command takes `--json`: one JSON document on stdout, human text on stderr.

**MCP server.** `python -m rentguard.mcp_server` exposes the same operations over stdio as eleven tools,
so a team of agents can rent, watch and stop machines through one place that enforces the cap.

**What it deliberately does not do**

- It never reports an account balance. No provider exposes one reliably, and a null billing total means
  "unavailable", not "$0.00".
- It does not pull artefacts only at the end; it pulls them as each run finishes.
- It does not treat `status: RUNNING` as proof a machine works: a pod reporting RUNNING with zero uptime
  and null ports is a crash loop, and `run` refuses it after three readings.
- It does not watch anything by itself. The cap exists while `watch` runs; nothing starts or restarts it.

**Documented limits.** Every limit is written out in [Limitations](docs/limitations.md), including the
missing provider balance, the guard's foreground nature, the pull's bounds and the container-without-a-
volume rule.

**Exercised end to end on real rented RunPod GPUs**: renting, running commands over ssh, pulling
artefacts and closing the ledger with the recorded cost, replicated by hand against the provider's own
record.
