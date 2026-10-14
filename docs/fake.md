# Test the cap without spending

A money guarantee that has only ever been tested by spending money is a claim, not a guarantee. So
`rentguard` ships an in-memory provider: `provider = "fake"`, or `RENTGUARD_PROVIDER=fake` for one
command. No API key, no network, no clock of its own, no money.

It is the instrument the whole suite runs on, and it is also how to watch the tool work before you have
an account.

## Run the real CLI against it

Two lines of config are enough:

```toml
provider = "fake"

[runpod]
gpu_preference = ["Fake GPU 24GB"]   # the fake only has this GPU; no stock means no rental
```

Then every command behaves as it does against a provider, including across processes: the fake saves
its machines to `.rentguard/fake-state.json`, so `up` in one shell, `status` in the next and `down` in
a third all see the same instance. A `FakeProvider()` built in a test without a state path stays in
memory and never touches disk.

```console
$ RENTGUARD_PROVIDER=fake rentguard doctor
provider       : fake  (in-memory: no network, no spend)
api key        : MISSING ($DSH_SECRET_RUNPOD_API_KEY)  (not needed for the fake)
```

The step-by-step rehearsal is in the [quickstart](quickstart.md#rehearse-without-paying).

## What it reproduces

Rented machines betray you in specific ways, and each one has a helper:

| Helper | What it simulates |
| --- | --- |
| `add(id, status=..., uptime_seconds=..., rate_usd_per_hour=...)` | a machine already on the account |
| `advance(seconds, instance_id=None)` | time passing for every running machine (billing continues) |
| `disappear(id)` | a crash, a provider kill, a click in someone else's console |
| `crash_loop(id)` | `RUNNING` with zero uptime and null ports |
| `idle(id)` / `busy(id)` | utilisation at zero / at work, while the meter runs |
| `put(path, data)` | a file on the machine, so the artefact path has something to pull |
| `fail_actions` | an action the provider refuses |
| `actions`, `created` | what the code under test actually asked for, in order |
| `refuse_gpus`, `unavailable_gpus` | a GPU that is listed but cannot be created (availability is not capacity) |

Payloads are shaped like RunPod's on purpose: a fake that returns a convenient shape tests the program
against a world that does not exist.

## The demonstration: the cap fires, nothing is spent

```python
from datetime import timedelta

from rentguard.config import Budget, Config
from rentguard.fake import FakeProvider
from rentguard.guard import watch
from rentguard.ledger import Ledger, now_utc

provider = FakeProvider(rate_usd_per_hour=2.0)
provider.add("fake-1", uptime_seconds=3 * 3600)          # three hours old, $2.00/h
ledger = Ledger(".rentguard/runs.jsonl")
ledger.open(purpose="free cap demonstration", provider="fake", instance_id="fake-1",
            gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
            started_at=now_utc() - timedelta(hours=3))
cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0))
print(watch(cfg, provider, ledger, instance_id="fake-1", interval=0.0, max_iterations=3))
```

Real output, no network, no money:

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

That is the per-run cap, the stop action, the closed ledger row with its cost, and the idle rule — all
asserted without an account. The same trick is what lets `tests/test_money_paths.py` (23 tests) cover
the cap, the drift and the artefact paths for free.

## What it cannot prove

The fake is not RunPod. Its payload shape is modelled on RunPod's, but availability, provisioning
times, billing increments, API failures and the provider's own stop latency are invented or
simplified. Passing the whole suite against the fake proves the arithmetic and the decisions; it does
not prove the arithmetic against a real meter. That is what the one paid end-to-end rental (M2) is
for, and why the cap is never called proven until it has run against a machine that is really billing.
