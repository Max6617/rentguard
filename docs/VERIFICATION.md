# Verification log

What was actually run for the pages in this directory, with the real output. If a claim in the docs is
not here and not in a test, it is not verified.

Environment: Windows, Python 3.12.6, `PYTHONUTF8=1` (without it the console mangles the em-dashes in
the output), run from a scratch project directory with `PYTHONPATH` pointing at the checkout.
Date: 2026-10-14, between 14:47 and 15:10 local. The tree changed several times during that window
(Forge was landing `init`, `--json`, the provider interface and the drift checks); the transcripts
below are from the final pass.

## Version and help

```console
$ rentguard --version
rentguard 0.1.0
```

`rentguard --help` lists: `doctor`, `init`, `up`, `status`, `watch`, `down`, `ledger`, `pull`, `adopt`,
`run`.

## `rentguard init`

```console
$ rentguard init
wrote C:\Users\besen\.dsh\bot\workspace\doc-fresh\rentguard.toml
next           : `rentguard doctor`, then `rentguard up --purpose ...`
```

The generated file writes the defaults: `cap_usd = 25.0`, `per_run_cap_usd = 5.0`,
`max_hours_per_run = 6.0`, `idle_minutes = 10`, `gpu_preference = [...4090, L40S, A6000]`. It also
writes `volume_name = ""` while the rent path reads `volume_id` (see limitations).

## `rentguard doctor`, no key

```console
$ rentguard doctor
rentguard      : v0.1.0
config         : C:\Users\besen\.dsh\bot\workspace\doc-fresh\rentguard.toml
state dir      : C:\Users\besen\.dsh\bot\workspace\doc-fresh\.rentguard
provider       : runpod
api key        : MISSING ($DSH_SECRET_RUNPOD_API_KEY)
spend recorded : $0.00 of a $25.00 cap (0 run(s))
open run       : (none)
ledger lines   : all readable
guard heartbeat: (never)  <-- STALE, nothing is watching

  ERROR no-api-key: no API key: the account cannot be inspected
```

Exit code 1. `rentguard ledger` on the same empty project prints `no runs recorded` and exits 0.

## `rentguard up --dry-run`, with a key (read-only)

```console
$ rentguard up --purpose "baseline" --cap 5 --dry-run
dry run: would request NVIDIA GeForce RTX 4090; nothing was rented and nothing was spent
```

This calls the provider catalog and the cap check, and rents nothing. It is the only `up` form that
was run; the paid path is milestone M2.

## The in-memory rehearsal

`RENTGUARD_PROVIDER=fake`, config with `gpu_preference = ["Fake GPU 24GB"]`.

The fake persists its machines in `.rentguard/fake-state.json`, so these were separate processes.

The span was re-verified after Forge's F-5 fix, including the two sequences Assay was asked for (clean
root exits 0; a second `up` from a fresh process does not rent a second machine):

```console
$ rentguard doctor                                  # clean root
...
account pods   : 0 total, 0 live
  (nothing is billing)
[exit 0]

$ rentguard up --purpose "doc rehearsal"            # process 1
requesting Fake GPU 24GB
instance       : fake-1  $0.13/h
ledger row     : 20261014T131705Z-44d37b
ssh            : ssh fake@127.0.0.1

cap            : $5.00 total, $2.00 per run, 6.0 h max

$ rentguard up --purpose "second attempt"           # process 2; no second rental
instance fake-1 is already RUNNING
ssh            : ssh fake@127.0.0.1

$ rentguard down                                    # process 3
stopping fake-1
run closed     : 20261014T131705Z-44d37b  0.0008 h  $0.000108
total spend    : $0.00 of $5.00

$ rentguard doctor
account pods   : 1 total, 0 live
  (nothing is billing)
[exit 0]
```

`.rentguard/fake-state.json` held one instance (`fake-1`, `RUNNING`) at the point of the second `up`.

## The cap fires, with nothing spent

A script driving the real guard against the fake, output as printed (this is the transcript in
[fake.md](fake.md)):

```console
$ python demo_fake.py
healthy            : True 600 s uptime, ports set
crash_loop         : False
disappear          : get -> None  list_pods -> []
  2026-10-14T13:14:59+00:00  RUNNING    up  3.00h  run $6.000  total $6.000/$10.00  gpu 0.0%  idle 0m
guard stopped the instance: per-run cap reached ($6.00 of $5.00)

guard returned     : per-run cap reached ($6.00 of $5.00)
provider actions   : [('fake-1', 'stop')]
row left open      : None
recorded           : $6.0000

idle decision      : idle for 11 min (limit 10 min, gpu util 0.0)
```

No network, no key, no spend. The script's own `FakeProvider` also failed initially on
`FakeInstance.payload()` without a bound clock (`AttributeError: '_now'`), which is why it goes through
`provider.get()`; a direct `add()` + `instance.payload()` call is not a supported path.

## Drift, reported rather than silent

An open ledger row whose instance is not in the account, with the fake provider:

```console
$ rentguard status
DRIFT          : run 20261014T125948Z-13c207 is open, but the provider shows no instance 'fake-1'
accrued        : $0.001 at $0.13/h since 2026-10-14T12:59:48+00:00
this run       : $0.001 of $2.00
recorded spend : $0.00 of $5.00 (an open row is not counted until it is closed, so this figure reads low)
purpose        : doc rehearsal
next           : `rentguard down` closes the row and records the cost; `rentguard adopt <id>` if the machine does still exist
```

`doctor` on the same state prints `ERROR open-run-instance-missing: ...` and exits 1.

The full worked example behind the limitations page (one open row, no `stopped_at`, instance
`vanished-1`, started three hours earlier at $2.00/h):

```console
$ rentguard status
DRIFT          : run 20261014T101144Z-086c00 is open, but the provider shows no instance 'vanished-1'
accrued        : $6.001 at $2.00/h since 2026-10-14T10:11:44+00:00
this run       : $6.001 of $5.00
recorded spend : $0.00 of $25.00 (an open row is not counted until it is closed, so this figure reads low)

$ rentguard doctor
...
  ERROR open-run-instance-missing: open run 20261014T101144Z-086c00 names instance 'vanished-1', which the provider does not show

$ rentguard ledger
20261014T101144Z-086c0 Fake GPU 24GB                 OPEN        -  -                  open row whose instance does not exist
total                                                0.00   0.0000
open row       : 20261014T101144Z-086c00  accrued $6.0013 (not in the total until closed)

$ rentguard down
no instance recorded; closed row 20261014T101144Z-086c00

$ rentguard ledger
20261014T101144Z-086c0 Fake GPU 24GB                 3.00   6.0056  down-no-instance   open row whose instance does not exist
total                                                3.00   6.0056
cap: $25.00   remaining: $18.99
```

For comparison, the pre-fix behaviour that made this the first entry in KNOWN-FAILURES.md: `status`
printed `no instance recorded / spend so far   : $0.00 of $25.00`, and `ledger` showed the row `OPEN`
with a total of `0.0000`.

## `--json`

```console
$ rentguard ledger --json
no runs recorded
{
  "command": "ledger",
  "runs": [],
  "total_usd": 0,
  "accrued_usd": 0.0,
  "total_with_open_usd": 0.0,
  "cap_usd": 25.0,
  "remaining_usd": 25.0,
  "open_run": null,
  "malformed_lines": []
}
```

The human text comes first and the JSON document after it, on stdout.

## The MCP server

Sent over stdin, one JSON-RPC message per line, against an empty project:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doc","version":"0"}}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"rentguard_ledger","arguments":{}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"rentguard_run","arguments":{"command":"python train.py"}}}
{"jsonrpc":"2.0","id":6,"method":"no/such/method"}
```

Replies:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "rentguard", "version": "0.1.0"}}}
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "no runs recorded"}], "isError": false}}
{"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "error: nothing is rented. `rentguard up` first.\n(exit code 1)"}], "isError": true}}
{"jsonrpc": "2.0", "id": 6, "error": {"code": -32601, "message": "method not found: no/such/method"}}
```

`tools/list` returned eight tools: `rentguard_status`, `rentguard_ledger`, `rentguard_doctor`,
`rentguard_start`, `rentguard_run`, `rentguard_pull`, `rentguard_stop`, `rentguard_adopt`. A line that
is not JSON was sent and produced no reply, as designed.

## Tests

Run from the checkout, `python rentguard/tests/<file>`:

| Suite | Result |
| --- | --- |
| `test_money_paths.py` | 23 tests, OK |
| `test_artefacts_mcp_guards.py` | 5 tests, OK |
| `test_rentguard.py` | 16 tests, OK |
| `test_drift.py` | 3 tests, OK |

47 tests, all passing, no network, no spend.

The tree kept moving during the window: at 15:08 an intermediate edit made keyless `doctor` raise
`TypeError: note() missing 1 required positional argument`, and it was fixed within minutes. The
transcripts above are from the state that followed that fix. `resolve("runpod")` returning the base
`Provider` class was another intermediate break, also fixed (it now returns `rentguard.runpod.RunPod`).

## Not verified

- **A paid rental.** `up` against RunPod, `run` over ssh, a real `pull`, a real `down` and the
  resulting ledger row are milestone M2 and were not run: they spend money. The `up --dry-run` call
  above touched the provider's catalog only.
- **`pip install .`.** The local sandbox refused `python -m venv` (`ensurepip` failed) and a
  `--target` install (temp-directory permissions), so the console script produced by `pyproject.toml`
  was not exercised here. The module is exercised as `python -m rentguard.cli`.
- **A provider other than RunPod or the fake.** None exists yet.
