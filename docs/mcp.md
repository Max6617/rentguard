# MCP server

`rentguard` runs as an MCP server over stdio, so a Bot team can rent, watch and stop machines through
tool calls instead of a shell. It is the same code as the CLI: each tool is one CLI command with its
output captured.

```bash
python -m rentguard.mcp_server
```

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, per the MCP stdio convention. Nothing
else is written to stdout — a stray `print` corrupts the stream and the client sees a dead server
rather than an error, so every command's output is captured and returned inside the tool result.

Accepted protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`. `initialize` echoes the
client's version when it is one of these, and the latest otherwise.

## Environment

| Variable | Meaning |
| --- | --- |
| `RENTGUARD_ROOT` | project root: where `rentguard.toml` and `.rentguard/` live. Without it, the server's working directory. |
| `DSH_SECRET_RUNPOD_API_KEY` | the provider key. Never printed, never returned in a result. |
| `RENTGUARD_PROVIDER=fake` | use the in-memory provider: no network, no key, no money. For trying the tools. |
| `PYTHONPATH` | the checkout, until the package is installed. |

A client entry looks like this:

```json
{
  "command": "<python>",
  "args": ["-m", "rentguard.mcp_server"],
  "env": {
    "PYTHONPATH": "<project root>",
    "RENTGUARD_ROOT": "<project root>",
    "DSH_SECRET_RUNPOD_API_KEY": "$DSH_SECRET_RUNPOD_API_KEY"
  }
}
```

## Tools

| Tool | Money | What it does |
| --- | --- | --- |
| `rentguard_status` | read-only | instance, GPU, uptime, this run's cost, total against the cap, guard heartbeat |
| `rentguard_ledger` | read-only | every rental with wall clock and cost, and the total |
| `rentguard_doctor` | read-only | config, key, recorded spend, heartbeat, and live instances with no ledger row |
| `rentguard_start` | **spends** | rent or resume an instance; refuses when recorded spend reaches the cap |
| `rentguard_run` | uses a rented machine | run a shell command remotely over ssh, then pull artefacts if configured |
| `rentguard_pull` | reads from the machine | pull the configured artefact patterns now |
| `rentguard_stop` | stops spend | stop the instance and close the ledger row with its cost |
| `rentguard_adopt` | records spend | give an already-running instance a ledger row, backdated from its uptime |

`rentguard_start` requires a `purpose`; everything else takes no arguments except `rentguard_run`
(`command`, optional `workdir`) and `rentguard_adopt` (`instance_id`, optional `purpose` and
`uptime_hours`).

## The approval rule

From the server's own module docstring:

> the money-moving tools are described as requiring the Main Bot's approval. The hard cap in the
> config is the backstop for when a Bot ignores that — a policy that depends on everyone remembering
> it is not a policy.

In plain terms:

- **Renting a machine needs the Main Bot's approval.** `rentguard_start` is the tool that spends
  money, and its description says so. The server cannot check who is calling or what was agreed; it
  is a stdio process with no authentication of its own, and whoever can call it can rent.
- **The cap is not the permission.** It is the thing that limits the damage when the permission step
  is skipped or forgotten. `--force` and `--cap` are deliberately not exposed as MCP arguments, so a
  Bot cannot raise its own ceiling through this interface.
- **Stopping needs no approval.** `rentguard_stop` is the safe direction and should be called when a
  machine is idle.

## A real session

The server is pipeable, which is also how to check a change without a client. These lines were sent to
it, one per line, against an empty project:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doc","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"rentguard_ledger","arguments":{}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"rentguard_status","arguments":{}}}
```

The replies, one line each (the `tools/list` result is elided; it is the eight tools in the table
above):

```json
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "rentguard", "version": "0.1.0"}}}
{"jsonrpc": "2.0", "id": 2, "result": {"tools": [ ... 8 tools ... ]}}
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "no runs recorded"}], "isError": false}}
{"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "no instance recorded\nspend so far   : $0.00 of $25.00"}], "isError": false}}
```

**Not structured yet.** `content` carries the CLI's text and nothing else. The structured form
(`--json`) exists in the CLI; handing it to the caller as well is not wired into this server, so a Bot
that wants numbers must parse the text or call the CLI itself. That is a known gap, listed in
[limitations](limitations.md).

## Errors

A command that exits non-zero still returns a result; the exit code is appended to the text and
`isError` is `true`:

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"rentguard_run","arguments":{"command":"python train.py"}}}
{"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "error: nothing is rented. `rentguard up` first.\n(exit code 1)"}], "isError": true}}
```

Protocol-level failures use JSON-RPC errors: an unknown method gets `-32601`, and an unexpected
exception inside a tool gets `-32603`. A line that is not JSON is ignored — it does not kill the
server:

```json
{"jsonrpc":"2.0","id":6,"method":"no/such/method"}
{"jsonrpc": "2.0", "id": 6, "error": {"code": -32601, "message": "method not found: no/such/method"}}
```
