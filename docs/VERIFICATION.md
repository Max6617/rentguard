# Verification log

What was actually run for the pages in this directory, with the real output. If a claim in the docs is
not here and not in a test, it is not verified.

Environment: Windows, Python 3.12.6, `PYTHONUTF8=1` (without it the console mangles the em-dashes in
the output), run from scratch project directories with `PYTHONPATH` pointing at the checkout.
Date: 2026-10-14, between 14:47 and 15:25 local.

## Revision

**This log is a history, not a snapshot.** The record in this section and the ones under "Exercised at
`af1ba5a`" was written at `af1ba5a` (2026-10-14) and is kept as it was measured then. The revisions and
numbers that are current — the published tip `605564e`, whose code the last of them measures — are in
[After `eef6bdb`](#after-eef6bdb-measured-2026-10-15) and
[Install](#install-the-wheel-into-a-clean-venv). **Which revision is the published tip is not written
down here as a value**, because a value in a committed file goes stale the moment anyone pushes; the
live answer is the run list,
`https://api.github.com/repos/Max6617/rentguard/actions/runs?per_page=1`, and the fixed verdicts this
page does carry are for the revisions named beside them.

The pages were re-run against three revisions, each extracted with `git archive` so that a mid-edit
working tree could not affect the run:

| Revision | Message | Tests |
| --- | --- | --- |
| `8a97a48` | rentguard: quiesced M1 tree (53 tests green) | 53 tests, **3 failures** |
| `528d15b` | run: pass a pre-quoted command through; up: walk the GPU preference list on refusal | 58 tests, 0 failures |
| `2ebe660` | fake: offer_unavailable models catalogue-yes/create-no for the fallback path | 58 tests, 0 failures |
| `af1ba5a` | licence and packaging: Max Reischl (Max6617) as the copyright holder and author | 74 tests, 0 failures |
| `74b9f7a` | docs: the mcp page documents all eleven tools, and an armed guard is bounded | 189 tests, 0 failures |

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

**`af1ba5a` (HEAD) is the current pass.** The worktree was clean except for the untracked
`CHARTER.md`, and the suite is **74 passed / 0 failed** (twice). Everything added since `2ebe660` was
run for real: the `init` template, `adopt`, the guard heartbeat wording in `status`, MCP
`structuredContent`, and `down --keep-running` (through its own suite). Two defects remain open and are
recorded below and in [KNOWN-FAILURES](KNOWN-FAILURES.md): F-4 (`--dry-run` creates) and F-5 (the idle
clamp).

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
wrote <workspace>\doc-fresh\rentguard.toml
next           : `rentguard doctor`, then `rentguard up --purpose ...`
```

*(Host paths in the transcripts below are elided to `<workspace>` — the runs were made in a scratch
checkout of that name. Nothing else in the output is changed; the elision exists so a public page does not
carry a machine's layout, and it is marked here rather than done quietly.)*

The generated file writes the defaults: `cap_usd = 25.0`, `per_run_cap_usd = 5.0`,
`max_hours_per_run = 6.0`, `idle_minutes = 10`, `gpu_preference = [...4090, L40S, A6000]`, and
`state_dir` **above** the first table. Earlier in the day it wrote `volume_name = ""` while the rent
path reads `volume_id`; at `af1ba5a` it writes `volume_id` and `volume_mount` (see below).

## `rentguard doctor`, no key

```console
$ rentguard doctor
rentguard      : v0.1.0
config         : <workspace>\doc-fresh\rentguard.toml
state dir      : <workspace>\doc-fresh\.rentguard
provider       : runpod
api key        : MISSING ($DSH_SECRET_RUNPOD_API_KEY)
spend recorded : $0.00 of a $25.00 cap (0 run(s))
open run       : (none)
ledger lines   : all readable
guard heartbeat: (never)  <-- STALE, nothing is watching

  ERROR no-api-key: no API key: the account cannot be inspected
```

Exit code 1. `rentguard ledger` on the same empty project prints `no runs recorded` and exits 0.

## `rentguard up --dry-run` — the form that used to rent

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

One `create`, no ledger row, and the line above it denying that anything happened — against RunPod that
was a rented, billing machine with nothing recording it. Fixed at `ce69cea`, with the test asserting on
the provider call (`f7bf556`) instead of on the sentence. The reproduction is kept in
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
ledger row     : 20261014T131705Z-44d37b  (provisional: opened before the machine exists)
requesting Fake GPU 24GB
instance       : fake-1  $0.13/h
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

The row line is first and says `provisional` from `f158396`: `up` opens the row **before** it calls
`create`, so an interruption between the two cannot leave a machine billing with nothing recording it.
The invariant is that a `create` implies either a row or a stop, even on `SIGKILL`; the row is corrected
with the instance id and the pod's own rate once the provider names the machine (`Ledger.finalise`), and
voided if every candidate is refused.

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

`tools/list` returns eleven tools: `rentguard_status`, `rentguard_ledger`, `rentguard_doctor`,
`rentguard_start`, `rentguard_run`, `rentguard_pull`, `rentguard_stop`, `rentguard_adopt`,
`rentguard_watch_once`, `rentguard_watch`, `rentguard_init`. The transcript below is from `2ebe660`,
when the list was eight and a reply carried `content` only; from `af1ba5a` each result also carries
`structuredContent`, and from `d3b209a` the guard tools are in the list (see
[Exercised at af1ba5a](#exercised-at-af1ba5a)).

## Exercised at `af1ba5a`

**`init`** now writes `volume_id` and `volume_mount` (not the old `volume_name`, which was silently
ignored), and keeps `provider`, `state_dir` and the `ssh_*` keys **above the first `[table]`** so TOML
cannot file them inside one. `init --print` was run; the template carries a comment saying why.

**`adopt`**, free, against an untracked instance on the fake account with 1.5 h of uptime at $2.00/h:

```console
$ rentguard doctor
account pods   : 1 total, 1 live
  orphan-1  RUNNING      $2.0/h  Fake GPU 24GB                UNTRACKED

  ERROR live-instance-no-ledger-row: 1 live instance(s) with no ledger row: orphan-1. Either `rentguard adopt <id> --purpose ...` or stop them.
[exit 1]

$ rentguard adopt orphan-1 --purpose "found billing"
adopted        : orphan-1  RUNNING  $2.0/h
row            : 20261014T122521Z-4b21b2
backdated start: 2026-10-14T12:25:21+00:00  (uptime 1.50 h from the provider)
cost already on the clock: $3.00 — this is spend that happened before the ledger knew about it, and it is now recorded rather than hidden
[exit 0]
```

**The guard heartbeat in `status`** is expressed as age, not as "armed". After `watch` writes one:

```console
$ rentguard watch
watching fake-1 — stop at $2.00 this run, $5.00 total, 6.0 h, or 10 min idle
  2026-10-14T13:55:31+00:00  EXITED     up  0.01h  run $0.001 (unrecorded)  total $0.001/$5.00  gpu 0.0%  idle 60m
guard finished: already stopped

$ rentguard status
...
guard          : alive (heartbeat 0.0 min ago)
```

With no heartbeat it prints `NO HEARTBEAT — nothing is watching this instance`; past five minutes,
`heartbeat 12.4 min old — the guard is probably dead`. There is no literal "armed" or "disarmed" string
in the output.

**F-5, the idle clamp**, on the pure decision path — no network, no spend:

```text
pod uptime                     : 32 s
provider-reported GPU idle     : 30.0 min
effective idle used by decide  : 30.0 min
idle_minutes from config       : 5.0 min
decide() -> 'idle for 30 min (limit 5 min, gpu util 0.0)'
```

**F-4, `--dry-run`**, free, in a fresh root:

```console
$ RENTGUARD_PROVIDER=fake rentguard up --purpose "dry" --dry-run
requesting Fake GPU 24GB
dry run: would request Fake GPU 24GB; nothing was rented and nothing was spent
[exit 0]

$ rentguard ledger
no runs recorded
# and .rentguard/fake-state.json holds fake-1 in PROVISIONING
```

**The MCP server** at this revision returns `structuredContent` next to `content`. Two real sessions
exercised all eleven tools against the fake — `status`, `ledger`, `doctor`, `start`, `run`, `pull`,
`stop`, `adopt`, and then `watch_once`, `watch`, `init`. `rentguard_adopt` returned, in part:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "adopted        : orphan-2  RUNNING  $1.0/h\n..."}], "isError": false, "structuredContent": {"command": "adopt", "adopted": true, "uptime_hours": 0.5, "accrued_usd": 0.500176, ...}}}
```

The arming tools, on a machine whose per-run cap was already crossed, with no guard running beforehand:

```json
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "watching fake-1 ...\nguard stopped the instance: per-run cap reached ($0.00 of $0.00)\n..."}], "isError": false, "structuredContent": {"command": "watch", "once": true, "max_iterations": 1, "armed_before": false, "heartbeat_age_minutes": null, "reason": "per-run cap reached ($0.00 of $0.00)", "instance_id": "fake-1", "stopped": true, "armed": false, "total_usd": 7.2e-05, "cap_usd": 1.0}}}
{"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "watching fake-1 ...\nguard finished: already stopped"}], "isError": false, "structuredContent": {"command": "watch", "once": false, "max_iterations": 1, "armed_before": true, "heartbeat_age_minutes": 0.0, "reason": "already stopped", "instance_id": "fake-1", "stopped": true, "armed": false, "total_usd": 7.2e-05, "cap_usd": 1.0}}}
```

`rentguard_init` with `print: true` returned the 2327-character commented template as its text, with
`isError: false`, and wrote nothing.

## Tests

Run from the checkout, `python -m pytest tests -q`:

| Suite | At `2ebe660` | At `af1ba5a` (HEAD) |
| --- | --- | --- |
| `test_money_paths.py` | 28 tests, OK | 33 tests, OK |
| `test_rentguard.py` | 16 tests, OK | 16 tests, OK |
| `test_artefacts_mcp_guards.py` | 5 tests, OK | 5 tests, OK |
| `test_init_config.py` | — | 4 tests, OK |
| `test_known_failures.py` | 4 tests, OK | 4 tests, OK |
| `test_mcp_structured.py` | — | 4 tests, OK |
| `test_down_keep_running.py` | — | 3 tests, OK |
| `test_drift.py` | 3 tests, OK | 3 tests, OK |
| `test_fake_span.py` | 2 tests, OK | 2 tests, OK |

58 tests at `2ebe660`, **74 at `af1ba5a`**, 0 failures at both; 53 at `8a97a48` with 3 failures (the
erased instance id, above). No network, no spend. `--dry-run` (F-4) and the idle clamp (F-5) are
covered by no test.

The tree also moved earlier in the window: at 15:08 an intermediate edit made keyless `doctor` raise
`TypeError: note() missing 1 required positional argument`, and `resolve("runpod")` briefly returned
the base `Provider` class. Both were fixed within minutes and are not in the table above.

## Exercised or only described

For the ten CLI commands and the eleven MCP tools at `af1ba5a` and `d3b209a`:

| Surface | Exercised | Only described |
| --- | --- | --- |
| `init` | `init --print`, plus a written file | — |
| `doctor` | keyless (exit 1), with the fake, and with an untracked live instance (exit 1) | against RunPod: the key was stripped from the shell for this pass |
| `up` | fake: a fresh rent and resuming an `EXITED` instance | against RunPod (M2 ran it; this pass did not) |
| `status` | running, stopped, and with a fresh heartbeat | — |
| `watch` | on a stopped instance (writes a heartbeat, exits) | stopping a live pod: the decisions are exercised in-process by the suite, never through the CLI against a real machine — charter R4 |
| `down` | fake, with the pre-stop pull | `--keep-running` through the CLI: covered by `test_down_keep_running.py`, not by a failed pull in this log |
| `ledger` | empty, and with an open row | — |
| `pull` | fake (0 files, exit 0) | over ssh against a real machine (M2 ran it) |
| `adopt` | fake orphan, 1.5 h uptime, backdated to $3.00 | — |
| `run` | — | through the CLI: the fake has no ssh server. Covered in-process by the suite, and run for real once in M2 |
| MCP `status`, `ledger`, `doctor`, `start`, `run`, `pull`, `stop`, `adopt` | real sessions on the fake, `content` + `structuredContent` | MCP `run` only on its "nothing is rented" path |
| MCP `watch_once`, `watch` | both, on a machine over its per-run cap: the decision, the stop, and `armed_before` / `armed` | a guard left running for a long time: it is bounded by `max_iterations` by design |
| MCP `init` | `print: true` returned the template and wrote nothing | writing a file through the tool |

## Later revisions

After `af1ba5a` the tree grew fast. At **`74b9f7a`** a clean worktree ran 189 passed / 0 failed; at
**`cdd7113`** 295 passed / 0 failed with coverage 98.4%; at **`f158396`** 318 passed / 0 failed with
coverage 2,336 / 2,384 = 97.99%, below the floor and exit 1; at **`7febd07`** 322 passed / 0 failed with
coverage 2,347 / 2,384 = 98.4%; at **`310f449`** 350 passed / 0 failed with coverage 2,494 / 2,542 =
98.1%; at **`721436a`** 355 passed / 0 failed with coverage 2,494 / 2,542 = 98.1%; at **`eef6bdb`** — the
publishing candidate in [PUBLISH-CHECKLIST](PUBLISH-CHECKLIST.md) — it runs **398 passed, 0 failed, 54
subtests**, identical with and without `RENTGUARD_PROVIDER=fake`, and coverage measures
**2,589 / 2,625 = 98.6%** against the 98.0% floor, **exit 0**. Per module at `eef6bdb`: `fake.py` 99.6,
`runpod.py` 99.3, `provider.py` 99.2, `guard.py` 99.1, `cli.py` 99.0, `config.py` 99.0, `mcp_server.py`
98.7, `ledger.py` 93.7 — only `ledger.py` is below the 94% figure now, and `runpod.py` has come up from
92.7% at `721436a`. Each figure was re-measured when a measured module changed, never carried forward.

A note on the figures: the same revision measured elsewhere can differ in the denominator (a different
tracer pass over the same files). This log records the run made in a clean worktree at the named sha, with
`rentguard.mcp_server.__file__` verified inside that worktree.

### After `eef6bdb`, measured 2026-10-15

The tree moved past this log's last pass. Each row was measured in a clean worktree with the package path
verified inside it:

| Revision | Suite | Coverage (Python 3.12), exit |
| --- | --- | --- |
| `eef6bdb` | 398 passed, 0 failed, 54 subtests | 2,589 / 2,625 = 98.6%, exit 0 |
| `7f52cc3` | 418 passed, 0 failed, 54 subtests, identical with and without `RENTGUARD_PROVIDER=fake` | 2,697 / 2,747 = 98.2%, exit 0 |
| `e53f544` | 425 passed, **9 failed** — the red-first attacks, awaiting the fixes | 2,710 / 2,761 = 98.2%, exit 0 |
| `1452718` | 456 passed, 0 failed, 54 subtests | 2,809 / 2,863 = 98.1%, exit 0 |
| `ddd79c9` | 471 passed, 0 failed, 54 subtests | 2,957 / 3,038 = **97.3%, exit 1** — below the 98.0% floor |
| `a9cffa0` | 485 passed, 0 failed, 54 subtests | 3,013 / 3,054 = 98.7%, exit 0 |
| `0d01d57` — the money-path pass (2026-10-16) | 601 passed, **2 failed**, 8 skipped, 99 subtests | **not re-measured** — measured modules changed since `be4e7c7`, so the floor figure would be stale here |

The two failures at `0d01d57` are Assay's attacks on the reuse-path fix —
`tests/test_b19_money_fix_attacks.py::TheReuseFixAtD8accac`: a machine that vanishes between the two
liveness readings, and a resume whose wait fails. Both are open, named in
[limitations](limitations.md), and being fixed. The money-path behaviours in [quickstart](quickstart.md)
§3/§4/§8 and [cap](cap.md) were written at this revision, and the transcripts beside them (`up`
refusing beside a machine that may still bill, and four `adopt --uptime-hours` refusals) were run there
against the fake provider.
| `042fd21` | 490 passed, 0 failed, 54 subtests | 3,037 / 3,080 = 98.6%, exit 0 |
| `be4e7c7` — the revision `1ba1198` published | 490 passed, 0 failed, 54 subtests, identical with and without `RENTGUARD_PROVIDER=fake` | 3,037 / 3,080 = **98.6%, exit 0** — floor cleared, no module below 94% |
| `605564e` — **published tip**, carrying defects 20 and 21 | **507 passed**, 0 failed, **59 subtests**, identical with and without `RENTGUARD_PROVIDER=fake` (32.04 s / 36.88 s, nothing skipped) | 3,050 / 3,093 = **98.6%, exit 0** — `session.py` **167/167**, and the gate fails a red suite before it compares the floor (defect 21) |

The `605564e` row is the worktree measurement described below: the suite was run plain and with
`RENTGUARD_PROVIDER=fake`, then the coverage gate with CI's own variable, in a clean worktree whose
package directory is named `rentguard` (the layout trap is in the notes above). Its two code changes
against `be4e7c7` are the driver fix for defect 20 (`b9447e8`) and the gate fix for defect 21
(`605564e`), both carried by tests that were red first — so unlike the rows above it, this one measures
modules that changed. Which revision is the tip now, and what CI says about it, is read from the run
list rather than from this table.

`be4e7c7` is tests-only on top of `042fd21` (defect 19's fix to the flaky spend assertion), so it
changes no measured module and the figures carry over; the suite was re-run there, and Gilbert measured
the same revision with CI's environment and got the same numbers.

**The red at `ddd79c9` was closed by a tool fix and by tests, not by a lower floor.** `f832596` made the
tracer count lines rather than line-table artefacts (`isinstance(n, int) and n > 0`) and reach worker
threads (`threading.settrace`) — the second half matters because lines that ran off the main thread were
not counted at all: `session.py` was 83.2% (129/155) and is **100%** (154/154) since `a9cffa0`, with
`fake.py`, `provider.py` and `runpod.py` also at 100%. The lowest measured module is `ledger.py` at
96.2%, and the total is above the 0.98 floor. No module is below the 94% figure.

The nine reds at `e53f544` are one story, and each names the effect on money: the two `up` shapes that
rent a second machine while a live one is billing, defect 15's live half (a live machine priced from its
whole uptime), a doubled row after a refused stop, and an unreadable price printed as zero. They are
closed by `3287a4f`, `13d7dc7` and `946ba28`, and green since `a9cffa0`; the reproductions stay in
[KNOWN-FAILURES](KNOWN-FAILURES.md).

One failure is not part of that story, and it is now fixed. In the **traced** run at `7f52cc3`,
`tests/test_doctor_spend_edges.py::VoidedAndFutureRows::test_a_voided_row_contributes_to_neither_number`
failed ($4.001065 vs $4.001132, `places=4`), and in the traced run at `a9cffa0` again — the assertion
compared two samples of a live quantity, and the second one is recomputed when it runs. `be4e7c7`
(tests only, defect 19 there) makes the tolerance the accrual rather than a fixed four places. It changes
no measured module, so the `042fd21` coverage figure stands; the suite passes at every revision named
above.

**CI is red at the published tip, and the culprit is the divisor.** At `7f52cc3` the CI run
`38721243756` passes `test (3.11)` and `test (3.12)` through every step and fails `test (3.13)` at the
**Coverage** step; the entry-point check after it never runs. The measurement is in
`tests/_artefacts/linenos_312_vs_313_2026-10-15.txt` (taken at `3287a4f`): the same hits over 3.13's
line table are **2,716 / 2,775 = 97.9%** against the 98.0% floor. CPython 3.13 yields a `None` line from
`dis.findlinestarts`, `trace._find_executable_linenos` keeps it as a key, and the denominator grows by 8
(one per measured module) while the numerator does not move. Zero integer line numbers differ between
the two interpreters over 65 files, so there is nothing to cover and nothing to delete.

**The filter landed at `f832596`, and it is in the published tip.** `tools/coverage.py` now reads
`{n for n in _find_executable_linenos(str(path)) if isinstance(n, int) and n > 0}`, and the same commit
adds `threading.settrace` so lines that ran on a worker thread are counted too; `tests/test_coverage_tool.py`
pins both halves. The completed verdict for the *previous* tip (`38721243756`, for `7f52cc3`) is red
because that revision does not have the filter — history, and immutable. **The verdict for the published
tip is not written here: read it at**
`https://api.github.com/repos/Max6617/rentguard/actions/runs?per_page=1`, which names the run and its
`head_sha`. A live status in a committed file is wrong eventually — this page said "in flight" for an
hour after the run had finished. Whether the 3.13 job passes cannot be checked on this host (no pytest
for 3.13 here), so the claim recorded is the narrow one: the divisor it failed on is fixed, and the 3.12
measurement at `be4e7c7` is 3,037 / 3,080 = 98.6%, exit 0.

Notes, all measured:

- **The coverage ratchet caught a regression and then closed it.** At `f158396`, with the floor at 0.98
  (raised at `e3fb62a` when the measurement was 98.4%), the total was 2,336 / 2,384 = 97.99% — one line
  short, so `tools/coverage.py` exited 1. The floor was **not** lowered: it was flagged as the open gate,
  `7febd07` covered the new pull and rate branches (98.4%, exit 0), and `310f449`, `721436a` and `eef6bdb`
  all hold or improve on it.
- **The night's five fixes, each red-first with its test and an independent attack.** The guard's idle
  arithmetic (`f28b2dd`, attacked by `c59d6a8`): elapsed watch time is not idle time, a sampled busy GPU
  forces idle to zero, the caller's counter is bounded by the pod's age, and unknown uptime is not a
  reason to stop. An unmatched pull pattern is a finding (`a1848ef`), with the match tested by `fnmatch`
  rather than a prefix (`5072b0e`). `up` may not let a recorded instance outvote the config's cloud and
  card (`c1135e1`) — it refuses with exit 2 and records a `--force` override. An empty archive is a
  failure, not zero files (`5072b0e`), and a partial pull keeps what it got while naming the gap, with the
  lost-artefact sentence naming patterns (`eef6bdb`, attacked by `fec0305`).
- **The 310f449/03dd52d work, all with tests in the tree.** `mcp_server` returns a `run`'s stdout/stderr
  in its result and reserves the real fd 1 for the duration of every tool call; the known-hosts null
  device is the local platform's, substituted by the CLI; `doctor` and the stop path read the machine's
  name as a claim — another root's tag is a warning, an unclaimed name is the hard error, and `down`
  refuses to stop a machine named for another root, voiding its own provisional row instead; and both a
  pull and a stop are bounded (`[artefacts].pull_timeout_s`, `[budget].stop_timeout_s`), with the stop
  escalating to the provider's own endpoint.
- **CI's `RENTGUARD_PROVIDER=fake` defect is closed at `cdd7113`.** The workflow sets that variable for
  its Test and Coverage steps, and it used to fail two `tests/test_cli_branches.py` tests: `248 passed /
  2 failed` with it set, `250 passed / 0 failed` without it, measured at `6f31c27` and again at
  `458e128`. The fix pins the provider inside those two tests rather than changing the CI step, and the
  suite is now 398 / 0 either way.
- **A stale `.rentguard-tmp` makes `test_ledger_paths.py` fail** (`PermissionError` creating a
  `runs.jsonl.lock` inside it, 16 tests in the main checkout). Nothing is wrong with the ledger: a fresh
  worktree runs that file green. Delete `.rentguard-tmp` in the checkout if the suite goes red there.
- **`pip install .` and a wheel build cannot be checked in this sandbox** (see Not verified): pip cannot
  use the DSH temp directory, so the build tracker and wheel cache fail to clean up and the build exits 2.
  The install evidence in the checklist is labelled as measured at an earlier revision for that reason.

## Storage

Live and read-only, 2026-10-14. The account's only network volume, `small-agents-vol` (150 GB, US-IL-1,
$10.50/month), was deleted on the user's instruction and executed by Forge. The sequence is recorded
from that work and matched by a read-only `GET /v2/network-volumes` at 16:54 (an empty list):

```text
BEFORE: [{"dataCenter":"US-IL-1","id":"ii4wouejo8","name":"small-agents-vol","size":150,"type":"STANDARD"}]
pods referencing it: 52n9hq6atcwa18 (small-agents-workbench, EXITED)
DELETE /v2/network-volumes/ii4wouejo8 -> 400 "You must remove this network volume from all pods before deleting it."
terminate 52n9hq6atcwa18 (EXITED, not billing)
DELETE /v2/network-volumes/ii4wouejo8 -> 204 No Content
AFTER: []
```

`doctor` against the account afterwards, run for this log:

```console
account pods   : 9 total, 0 live
  (nothing is billing)
network volumes: 0  ($0.00/month, billed while nothing runs)
[exit 0]
```

The report the fix produces, against the live payload's numbers. The volume is gone, so this is a
reproduction of the current code rather than the 16:47 transcript — same text:

```console
network volumes: 1  ($10.50/month, billed while nothing runs)
  small-agents-vol  150 GB  US-IL-1  $10.50/month  in the config of STOPPED pod pod-old — nothing is using it and it bills anyway

  WARN  storage-unattached: volume 'small-agents-vol' (150 GB, US-IL-1) costs $10.50/month and no running instance uses it; delete it if it is not wanted
  WARN  storage-not-in-ledger: $10.50/month of network storage is NOT in the ledger total, which covers rentals only
```

Endpoint: `GET /v2/network-volumes` on `api.runpod.io`, response key `networkVolumes`, region field
`dataCenter`; `rest.runpod.io/v1/networkvolumes` is 404.

## Install: the wheel, into a clean venv

Run at `51e350e` (2026-10-15), on Python 3.12.6, and first run the same way at `e53f544`. The claim this
backs is the one a stranger cares about: the checkout builds, installs, and its console script runs.

**`python -m pip install .` was not run here, and the step it stops at is named.** With no route to
PyPI, pip's isolated build never gets past fetching `setuptools>=68` for the build environment:

```text
Processing c:\...\rentguard
  Installing build dependencies: started
[still there after 7 minutes; killed]
```

`https://pypi.org/simple/setuptools/` from this host closes the connection. On a machine with network
this is the one command, and CI runs `python -m pip install -e .` on every push (green on 3.11 and
3.12 at `7f52cc3`).

**A plain `python -m venv` also fails here, for a different and named reason** — the sandbox refuses a
mode-0700 directory, and `ensurepip` writes inside one it just created:

```console
$ python -m venv <dir>
Error: Command '['<dir>\Scripts\python.exe', '-m', 'ensurepip', '--upgrade', '--default-pip']' returned non-zero exit status 1.

$ <dir>\Scripts\python.exe -m ensurepip --upgrade --default-pip
...
  File "C:\Python312\Lib\ensurepip\__init__.py", line 184, in _bootstrap
    with open(filename, "wb") as fp:
PermissionError: [Errno 13] Permission denied: '...\tmp\tmpgshq35og\pip-24.2-py3-none-any.whl'
During handling of the above exception ... PermissionError: [WinError 5] Zugriff verweigert
```

That is the environment, not the packaging, and it was measured directly: a direct write into
`tempfile.mkdtemp()` fails with `Errno 13`, and the `os.chmod` that would widen it fails with
`WinError 5`, while a sibling directory created with `os.mkdir(path)` (no explicit mode) is writable.
`python -m venv --without-pip` followed by `ensurepip` under a `sitecustomize` shim that widens only
that one mode works, and everything after it is ordinary:

```console
$ python -m pip wheel . --no-deps --no-build-isolation -w dist
Processing c:\...\rentguard
  Preparing metadata (pyproject.toml): finished with status 'done'
Building wheels for collected packages: rentguard
  Created wheel for rentguard: filename=rentguard-0.1.0-py3-none-any.whl size=85921 sha256=ac4116b2a3d7899239cade713d1f94a7e559987270b0eb832eb0ed3ee2592991
Successfully built rentguard
[20 s, exit 0]

$ <venv>\Scripts\python.exe -m pip install --no-index --no-deps dist/rentguard-0.1.0-py3-none-any.whl
Successfully installed rentguard-0.1.0

$ <venv>\Scripts\rentguard.exe --version
rentguard 0.1.0

$ <venv>\Scripts\rentguard.exe doctor --json      # in a root that has a ledger, provider fake
{
  "command": "doctor",
  "version": "0.1.0",
  ...
  "open_run": {"run_id": "20261015T210041Z-383698", "purpose": "install rerun", ...
  "spend": {"recorded_usd": 0.0, "accruing_usd": 8.6e-05, "total_usd": 8.6e-05, ...},
  "findings": []
}
[exit 0]
```

The same path was run for **keelcheck** at its `c5168d0`: `pip wheel` produced
`keelcheck-0.0.1-py3-none-any.whl` (55,005 bytes, sha256 `c05efef8…`) in 27 s, it installed into its own
clean venv, `keelcheck --version` printed `keelcheck 0.0.1`, and `keelcheck gate hash keelcheck/cli.py`
in the keel checkout printed `sha256:79885adac75a12d83afa49f5cea59f65e159f0332a0e929935d30dc4340df2c0`
(exit 0). The evidence is also written into keel's own README.

The free rehearsal in [quickstart](quickstart.md) was re-run from the installed console script at
`e53f544`, and every transcript there is that revision's real output.

## Not verified

- **The paid path by this pass.** `up` against RunPod, `run` over ssh, a real `pull` and a real `down`
  were not run *for this log*: they spend money. They were run as milestone M2 — twice on 2026-10-14,
  $0.017 in total, the second pass green with the artefact pulled from the right machine and the ledger
  reconciled by hand: [M2 — the real rental](M2-REAL-RENTAL.md).
- **`pip install .` itself, on this host.** Still not reproducible, and now for a named reason: there is
  no route to PyPI, so the isolated build waits at `Installing build dependencies: started` (see
  "Install" above). The wheel path stands in for it and is green; CI's `pip install -e .` on 3.11 and
  3.12 at `7f52cc3` is the independent evidence that the packaging installs.
- **A provider other than RunPod or the fake.** None exists yet.
