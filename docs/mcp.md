# MCP server

`rentguard` runs as an MCP server over stdio, so a team of agents can rent, watch and stop machines
through tool calls instead of a shell. It is the same code as the CLI: each tool is one CLI command, and
each reply carries both the text and the structured document.

The server advertises eleven tools, all documented below — including the three that arm or configure the
guard: `rentguard_watch_once`, `rentguard_watch` and `rentguard_init`.

**Protecting a machine from an agent is bounded, not a daemon.** A tool call cannot be a foreground loop,
so an MCP-armed guard protects while the call runs and no longer. The alternative would be a detached
daemon, and that is deliberately not built: a supervisor you believe in and that is not running is worse
than no supervisor. Every watch return says whether protection is still armed, so a caller can never
believe a guard is running when it is not. The cap itself still only exists while a watch runs — see
[the cap page](cap.md).

```bash
python -m rentguard.mcp_server
```

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, per the MCP stdio convention. Nothing else
is written to stdout — a stray `print` corrupts the stream and the client sees a dead server rather than
an error, so every command's output is captured and returned inside the tool result.

Accepted protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`. `initialize` echoes the client's
version when it is one of these, and the latest otherwise.

## Environment

| Variable | Meaning |
| --- | --- |
| `RENTGUARD_ROOT` | project root: where `rentguard.toml` and `.rentguard/` live. Without it, the server's working directory. |
| `RUNPOD_API_KEY` | the provider key. Never printed, never returned in a result. |
| `RENTGUARD_PROVIDER=fake` | use the in-memory provider: no network, no key, no money. For trying the tools. |
| `PYTHONPATH` | the checkout, until the package is installed. |

A client entry looks like this:

```json
{
  "command": "<python>",
  "args": ["-m", "rentguard.mcp_server"],
  "env": {
    "PYTHONPATH": "<checkout>",
    "RENTGUARD_ROOT": "<project root>",
    "RUNPOD_API_KEY": "<key>"
  }
}
```

## Tools

| Tool | Money | What it does |
| --- | --- | --- |
| `rentguard_status` | read-only | instance, GPU, uptime, this run's cost, total against the cap, guard heartbeat |
| `rentguard_ledger` | read-only | every rental with wall clock and cost, and the total |
| `rentguard_doctor` | read-only | config, key, recorded spend, heartbeat, and live instances with no ledger row |
| `rentguard_start` | **spends** | rent or resume an instance; refuses when recorded spend reaches the cap, and refuses outright when the config sets `allow_renting = false` |
| `rentguard_run` | uses a rented machine | run a shell command remotely over ssh, then pull artefacts if configured; its result carries the command's `exit_code` and its output (`stdout`, `stderr`, the byte counts, and `truncated` when the tail was cut) |
| `rentguard_pull` | reads from the machine | pull the configured artefact patterns now |
| `rentguard_stop` | stops spend | stop the instance and close the ledger row with its cost |
| `rentguard_adopt` | records spend | give an already-running instance a ledger row, backdated from its uptime; also refused when the config sets `allow_renting = false` |
| `rentguard_watch_once` | stops spend if the decision says so | one poll: the guard's decision, and whether a guard was already armed |
| `rentguard_watch` | stops spend if the decision says so | a bounded guard: poll up to `max_iterations` times, then return — reaching that bound **ends protection** and is reported as an error |
| `rentguard_init` | writes a file, or prints | write or print the commented config template |

Arguments: `rentguard_start` needs `purpose` (optional `gpu`); `rentguard_run` needs `command` (optional
`workdir`); `rentguard_adopt` needs `instance_id` (optional `purpose`, `uptime_hours`);
`rentguard_watch_once` takes `interval`; `rentguard_watch` takes `max_iterations` (default 1) and
`interval`; `rentguard_init` takes `path`, `force` and `print`. Everything else takes no arguments.

**A config can close the door.** The tools resolve `./rentguard.toml` from the directory the server was
started in, which is not always the project you meant. A config that sets `allow_renting = false` (with
`renting_disabled_reason` for the message) makes `rentguard_start` and `rentguard_adopt` refuse **before
a provider is built or a row opened**: the call comes back as an error naming that config file and its
reason, with `action: "refused"` and `reason: "renting-disabled"` in the structured result, and nothing
is spent. `rentguard_doctor`, `ledger`, `status` and — importantly — `stop` are never gated by it, so a
machine that is already billing can always be stopped from wherever you are.

## Arming the guard from an agent

`rentguard_watch_once` takes a single reading and returns the decision plus `armed_before`. The armed
state is read **before** the poll on purpose: a poll writes a heartbeat of its own, so asking afterwards
would always answer "armed" about a guard that has already returned. A one-shot poll does not keep a
guard alive, and the result says so with `armed: false`.

`rentguard_watch` is the same loop under a bound: it polls up to `max_iterations` times, then returns the
reason, whether it stopped anything, and `armed: false`. Use it when an agent must hold protection for a
while; use `watch_once` when the agent is driving the machine itself and only wants a reading.

**The bound is a hard end to protection.** When the loop reaches `max_iterations` it returns
`reason: "guard-iteration-limit"`, `iterations`, `max_iterations`, `interval_seconds`, `bounded_hours`,
`protection_ended_at_bound: true` and `armed: false`, and the call is an **error** (`isError: true`; the
CLI exits 2) — so a zero exit never means "still guarded". Make `max_iterations` × `interval` outlast
the window you are guarding; a watch that stops a machine, or finds it already stopped, still returns
success.

Real replies, on a machine whose per-run cap was already crossed, with no guard running beforehand:

```json
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "watching fake-1 ...\nguard stopped the instance: per-run cap reached ($0.00 of $0.00)\nguard finished: per-run cap reached ..."}], "isError": false, "structuredContent": {"command": "watch", "once": true, "max_iterations": 1, "armed_before": false, "heartbeat_age_minutes": null, "reason": "per-run cap reached ($0.00 of $0.00)", "instance_id": "fake-1", "stopped": true, "armed": false}}}
{"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "watching fake-1 ...\nguard finished: already stopped"}], "isError": false, "structuredContent": {"command": "watch", "once": false, "max_iterations": 1, "armed_before": true, "heartbeat_age_minutes": 0.0, "reason": "already stopped", "instance_id": "fake-1", "stopped": true, "armed": false}}}
```

The second call reports `armed_before: true` because the first had just written a heartbeat, and
`armed: false` because it returned. That pair is the whole point: the caller can tell the difference
between "a guard was running when I asked" and "a guard is running now", and the answer to the second is
always no.

## The approval rule

From the server's own tool descriptions:

> the money-moving tools say they require the operator's approval unless the team has agreed otherwise.
> The hard cap in the config is the backstop for when that rule is ignored — a policy that depends on
> everyone remembering it is not a policy.

In plain terms:

- **Renting a machine needs the operator's approval.** `rentguard_start` is the tool that spends money,
  and its description says so. The server cannot check who is calling or what was agreed; it is a stdio
  process with no authentication of its own, and whoever can call it can rent.
- **The cap is not the permission.** It is the thing that limits the damage when the permission step is
  skipped or forgotten. `--force` and `--cap` are deliberately not exposed as MCP arguments, so a caller
  cannot raise its own ceiling through this interface.
- **Stopping needs no approval.** `rentguard_stop` is the safe direction and should be called when a
  machine is idle.

## A real session

The server is pipeable, which is also how to check a change without a client. These lines were sent to
it, one per line, against a project with one stopped instance:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doc","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"rentguard_status","arguments":{}}}
```

The replies, one line each (the `tools/list` result is elided; it is the eleven tools in the table
above):

```json
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "rentguard", "version": "0.1.0"}}}
{"jsonrpc": "2.0", "id": 2, "result": {"tools": [ ... 11 tools ... ]}}
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "instance       : fake-1  EXITED\n..."}], "isError": false, "structuredContent": {"command": "status", "provider": "fake", "recorded_instance_id": "fake-1", "instance": {"id": "fake-1", "status": "EXITED", "usable": false, ...}, "open_run": null, "drift": false, "spent": {"recorded_usd": 7.2e-05, "accrued_usd": 0.0, "total_usd": 7.2e-05, "cap_usd": 25.0, ...}}}}
```

**Both forms, one call.** `content` gives the text a person would read and `structuredContent` gives the
same answer as an object, so an agent does not have to parse prose. The server asks the command for
`--json`: stdout is the structured document, stderr is the text, and the tool result is `isError: true`
when the command exits non-zero.

**A tool that spawns a child cannot leak onto the protocol stream.** `rentguard_run` starts an ssh
child, and a child inherits the real file descriptor 1 — not the captured `sys.stdout`. Left alone, its
output would appear between JSON-RPC messages and the client would read garbage. Two things prevent it:
`run` returns the command's `stdout` and `stderr` **inside its result** (`exit_code`, `stdout`, `stderr`,
the byte counts, and `truncated` when only the tail was kept), and for the duration of every tool call
the server points fd 1 at fd 2, so anything that bypasses the capture lands on the server's log stream
instead. The stream is reserved for every tool, not patched for one.

## Errors

A command that exits non-zero still returns a result; the exit code is appended to the text and
`isError` is `true`:

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"rentguard_run","arguments":{"command":"python train.py"}}}
{"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "error: nothing is rented. `rentguard up` first.\n(exit code 1)"}], "isError": true}}
```

Protocol-level failures use JSON-RPC errors: an unknown method gets `-32601`, and an unexpected
exception inside a tool gets `-32603`. A line that is not JSON is ignored — it does not kill the server:

```json
{"jsonrpc":"2.0","id":6,"method":"no/such/method"}
{"jsonrpc": "2.0", "id": 6, "error": {"code": -32601, "message": "method not found: no/such/method"}}
```
