# Verification log

What was actually run for the pages in this directory, with the real output. If a claim in the docs is
not here and not in a test, it is not verified.

Environment: Windows, Python 3.12.6, `PYTHONUTF8=1` (without it the console mangles the em-dashes in
the output), run from scratch project directories with `PYTHONPATH` pointing at the checkout.
Date: 2026-10-14, between 14:47 and 15:25 local.

## Revision

The pages were re-run against three revisions, each extracted with `git archive` so that a mid-edit
working tree could not affect the run:

| Revision | Message | Tests |
| --- | --- | --- |
| `8a97a48` | rentguard: quiesced M1 tree (53 tests green) | 53 tests, **3 failures** |
| `528d15b` | run: pass a pre-quoted command through; up: walk the GPU preference list on refusal | 58 tests, 0 failures |
| `2ebe660` | fake: offer_unavailable models catalogue-yes/create-no for the fallback path | 58 tests, 0 failures |

The first commit is **not** green as extracted. Its three failures are all the same defect —
`status` erases the recorded instance id when the provider no longer shows it:

```text
FAIL test_status_json_carries_the_drift_for_a_machine_consumer   (recorded_instance_id is null)
FAIL test_the_instance_id_is_still_recoverable_afterwards
FAIL test_status_does_not_erase_the_recorded_instance_id_on_a_404
```

The later commits fix it and carry the green claim. The page edits made after 15:22
(`README.md`, `docs/cap.md`, `docs/limitations.md`, `docs/quickstart.md`) are uncommitted as of this
writing.

The hashes Forge pinned for `2ebe660` were checked against the commit's blobs, not the working files
(the sandbox's `core.autocrlf` leaves CRLF on disk): `git rev-parse 2ebe660:cli.py` gives
`3affb3eb5eb3…`, `provider.py` `882b36cd5585…`, `guard.py` `0596a7df9d37…`, which are the sha256
`12831AA0A1DD6399`, `A8984861D2AC1730`, `CD19F45C57904F6F` in LF. Re-verified on that extracted tree:
58 tests, 0 failures, twice; `status` after a clean `down` prints
`machine : stopped (EXITED) — nothing is billing for it now` with no crash-loop or "NO ROW" line; and
`ledger --json` splits stdout and stderr as shown below.

**`up --dry-run` is a money-risk defect, present at all three revisions.** `cmd_up` calls
`provider.create` inside the preference-retry loop (`cli.py:713`) *before* the dry-run check
(`cli.py:727`), so a "dry" run makes real create calls. Against RunPod it printed:

```console
$ rentguard up --purpose baseline --cap 5 --dry-run
requesting NVIDIA GeForce RTX 4090
  NVIDIA GeForce RTX 4090 was refused; trying NVIDIA L40S
requesting NVIDIA L40S
  NVIDIA L40S was refused; trying NVIDIA RTX A6000
requesting NVIDIA RTX A6000
dry run: would request NVIDIA RTX A6000; nothing was rented and nothing was spent
```

Every attempt was refused, so nothing was created — but if stock had existed, `--dry-run` would have
rented a machine and still printed "nothing was rented", with no state pointer and no ledger row
written. No tested revision checks `dry_run` before creating, and no test covers it. The command is
therefore not documented as usable: [quickstart](quickstart.md) and [cap](cap.md) say so, and
[limitations](limitations.md) carries the reproduction.

Commands run against the extracted `8a97a48` tree, all free, no network writes, $0: `init`; `doctor`
keyless and with the fake; `ledger`; the fake span (`up` twice from separate processes, `status`,
`down` with the pre-stop pull, `ledger`); the nothing-rented paths for `status`, `watch`, `down`,
`run`, `pull`, `adopt`; and an MCP `tools/call`. They matched the pages, except `--dry-run`.

**Not run: the artefact round trip through the CLI.** The fake has no ssh server, so `run` would
shell out to `ssh fake@127.0.0.1`; the pull path with real files is exercised in-process by
`test_money_paths.py` and `test_artefacts_mcp_guards.py`, and its real output is M2's to produce.

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

## `rentguard up --dry-run` — not a safe form

Earlier in the day this ran against RunPod's catalog and printed:

```console
$ rentguard up --purpose "baseline" --cap 5 --dry-run
dry run: would request NVIDIA GeForce RTX 4090; nothing was rented and nothing was spent
```

That transcript is not evidence that `--dry-run` is safe. At `2ebe660` it is not: `cmd_up` calls
`provider.create` before it checks the flag, so "nothing was rented" is printed *after* the machine was
requested. Run free against the fake, it leaves an instance behind:

```console
$ RENTGUARD_PROVIDER=fake rentguard up --purpose "dry check" --dry-run
requesting Fake GPU 24GB
dry run: would request Fake GPU 24GB; nothing was rented and nothing was spent
[exit 0]

$ cat .rentguard/fake-state.json
  "instances": [
    {
      "id": "fake-1",
      "status": "PROVISIONING",
      ...

$ rentguard ledger
no runs recorded
```

One `create`, no ledger row, and the line above it denying that anything happened. Against RunPod that
is a rented, billing machine with nothing recording it. Do not use `--dry-run`; the reproduction is in
[limitations](limitations.md).

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

Two streams on purpose. At `2ebe660`, on an empty root:

```console
$ rentguard ledger --json          # stdout
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

$ rentguard ledger --json          # stderr
no runs recorded
```

stdout is exactly one JSON document and `json.loads(stdout)` succeeds; the human prose is on stderr.
Merging the two (`2>&1`) puts the prose in front of the document, which then does not parse.

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

| Suite | At `8a97a48` | At `2ebe660` (HEAD, 15:3x) |
| --- | --- | --- |
| `test_money_paths.py` | 23 tests, 1 failure | 28 tests, OK |
| `test_artefacts_mcp_guards.py` | 5 tests, OK | 5 tests, OK |
| `test_rentguard.py` | 16 tests, OK | 16 tests, OK |
| `test_drift.py` | 3 tests, OK | 3 tests, OK |
| `test_known_failures.py` | 4 tests, 2 failures | 4 tests, OK |
| `test_fake_span.py` | 2 tests, OK | 2 tests, OK |

53 tests at `8a97a48` with 3 failures (the erased instance id, above); 58 tests at `528d15b` and
`2ebe660` with none. No network, no spend. `--dry-run` is not covered by any of them.

The tree also moved earlier in the window: at 15:08 an intermediate edit made keyless `doctor` raise
`TypeError: note() missing 1 required positional argument`, and `resolve("runpod")` briefly returned
the base `Provider` class. Both were fixed within minutes and are not in the table above.

## Not verified

- **The paid path by this pass.** `up` against RunPod, `run` over ssh, a real `pull` and a real `down`
  were not run *for this log*: they spend money. They were run as milestone M2 — twice on 2026-10-14,
  $0.017 in total, the second pass green with the artefact pulled from the right machine and the ledger
  reconciled by hand: [M2 — the real rental](M2-REAL-RENTAL.md).
- **`pip install .`.** The local sandbox refused `python -m venv` (`ensurepip` failed) and a
  `--target` install (temp-directory permissions), so the console script produced by `pyproject.toml`
  was not exercised here. The module is exercised as `python -m rentguard.cli`.
- **A provider other than RunPod or the fake.** None exists yet.
