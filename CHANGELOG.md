# Changelog

Notable changes, newest first. This project follows semantic versioning once it has users; before
that, every release is `0.x` and may change anything.

## 0.1.0 — unreleased

First release. RunPod only.

**What it does**

- `rentguard up` rents (or resumes) an instance, choosing the first preferred GPU that is actually
  offered in the configured cloud tier, and falls through to the next candidate when the provider
  refuses one.
- `rentguard run -- <command>` runs a command over ssh and pulls the configured artefacts as the run
  finishes — not at the end, because an instance without a network volume is a container that a
  restart discards.
- `rentguard watch` is a foreground supervisor that stops the instance at the total cap, the per-run
  cap, the maximum hours, or after an idle window.
- `rentguard down` stops the instance and closes the ledger row with its real cost.
- `rentguard ledger` lists every rental with its wall-clock and dollars.
- `rentguard doctor` reports recorded spend, the guard heartbeat, any live instance with no ledger
  row, and drift in both directions between the ledger and the state file.
- `rentguard adopt <id>` records an instance that is already running, backdated from the provider's
  own uptime, so money already spent is recorded rather than starting the clock at zero.
- `rentguard init` writes a commented config.

**MCP server.** `python -m rentguard.mcp_server` exposes the same operations over stdio, so a team of
agents can rent, watch and stop machines through one place that enforces the cap.

**What it deliberately does not do**

- It never reports an account balance. No provider exposes one reliably, and a null billing total
  means "unavailable", not "$0.00".
- It does not pull artefacts at the end; it pulls them as each run finishes.
- It does not treat `status: RUNNING` as proof a machine works: a pod reporting RUNNING with zero
  uptime and null ports is a crash loop, and `run` refuses it.

**Proven end to end** on a real rented RTX 4090 twice, for $0.017 in total, including a deliberately
failed run that found three defects the in-memory provider could not. See `docs/M2-REAL-RENTAL.md`.
