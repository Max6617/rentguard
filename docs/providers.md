# Providers

RunPod is the only provider in v0.1. The point of this page is that adding a second one is a
contained job: a provider knows how to rent, inspect, stop and reach one kind of machine, and nothing
else. The cap, the ledger, the guard and the artefact logic sit above it and are identical for every
provider.

Everything below is the contract in [`rentguard/provider.py`](../provider.py).

## The canonical shape

Nothing above the provider layer parses a provider-specific schema. A provider returns dictionaries
in this shape; keys marked `*` may be missing.

```python
instance = {
    "id": str,                      # the provider's identifier for the machine
    "name": str*,                   # display name
    "status": str,                  # provider status, upper-case: RUNNING, EXITED, ...
    "cost": float,                  # USD per hour for the whole instance
    "gpu": {"id": str, "count": int}*,
    "runtime": {                    # present once the machine reports itself
        "uptime": float,            # seconds since it started billing
        "ports": dict | None,       # None/absent means "no reachable port yet"
        "gpus": [{"util": float, "memoryUtil": float}]*,
    },
    "ssh": {"direct": {"host", "port", "username"},
            "proxy": {"host", "port", "username"}},   # at least one, when reachable
}

gpu = {
    "id": str,                      # exactly what create(gpu_id=...) accepts
    "memory": int*,                 # GiB
    "price": {"secure": float, "community": float}*,
    "secure": str, "community": str,  # availability: "High" | "Medium" | "Low" | "None"
}
```

## The interface

Subclass `rentguard.provider.Provider`. Set `name`, and `needs_api_key` if no key is required.

| Method | Required | Notes |
| --- | --- | --- |
| `get(instance_id) -> instance \| None` | yes | `None` means "does not exist". Never raise for gone. |
| `list_pods() -> [instance]` | yes | every instance on the account, whatever its state |
| `create(...) -> instance` | yes | **starts billing**; `up` is the only caller, after the cap check |
| `action(instance_id, "start" \| "stop" \| "terminate")` | yes | change machine state at the provider |
| `wait_running(instance_id, timeout_minutes, poll_s, log) -> instance` | yes | block until usable, or raise |
| `is_alive(instance) -> bool` | inherited | `uptime > 0 and ports`; override only if that is wrong for you |
| `catalog() -> [gpu]` | inherited `[]` | what can be rented |
| `choose_gpu(preference, cloud, need_memory_gb)` | inherited | first preference that is available |
| `price_of(gpu_id, cloud)` | inherited | from `catalog()` |
| `ssh_argv(instance, key) -> (args, target)` | inherited raises | how a caller reaches the machine |
| `pull_artifacts(instance, patterns) -> {path: bytes}` | inherited raises | falls back to ssh + `tar` |
| `normalise(instance) -> instance` | inherited identity | intended for field-name translation, but nothing calls it today: return canonical shapes from `get`/`list_pods` directly |

The contract, from the module docstring:

- `get` and `list_pods` never invent fields. A null or missing uptime stays null; `is_alive` is the
  single place that decides what null means.
- `create` is the only method that starts billing.
- Money is never read. No provider exposes a reliable balance, so the interface has no method for it,
  on purpose.

Nothing about the cap, the ledger, the guard or artefacts belongs in a provider.

## How a provider is selected

The order is fixed: `RENTGUARD_PROVIDER` in the environment, then `provider = "..."` in
`rentguard.toml`, then `"runpod"`.

```bash
RENTGUARD_PROVIDER=fake python -m rentguard.cli up --purpose demo
```

`provider.resolve(name)` and `provider.build(name, api_key)` look the name up in `_MODULES`, a one-line
map in `provider.py`:

```python
_MODULES = {
    "runpod": "rentguard.runpod",
    "fake": "rentguard.fake",
}
```

**Selection, stated plainly:** `cli._provider` builds through `provider.make_provider`, which resolves
the name in `_MODULES` and constructs the class. Adding the one line is enough for the CLI to reach a
new provider; no branch in `cli.py` is needed. What a new provider still cannot do is read its own
config section — see step 5 below.

## The fake

Without an in-memory provider, the only way to test that the cap fires is to spend money trying to
exceed it. `rentguard.fake.FakeProvider` implements the whole interface with no network and no clock of
its own, and reproduces the ways rented machines betray you: `disappear()`, `crash_loop()`, `idle()`,
`advance()`, `fail_actions`, and GPUs that are listed but cannot be created.

```bash
RENTGUARD_PROVIDER=fake rentguard doctor
```

Its helper list, how to drive the real CLI with it across processes, and a runnable demonstration of
the cap firing with nothing spent: **[Test the cap without spending](fake.md)**. The suite's money
paths (`tests/test_money_paths.py`, 28 tests) run on it.

## Adding Vast, Lambda, or a local box

**Not supported yet.** What follows is the shape a second provider has to fit, not a path anyone has
walked. RunPod and the fake are the only registered providers, `cli._rp` reads `cfg.runpod` so there is
no `[vast]` section, and nothing here has been done end to end. Treat it as the map for the next
person, not as a feature.

1. Create `rentguard/vast.py` with a `Provider` subclass. The module must expose a class named
   `Provider` (the registry reads that name).
2. Implement `get`, `list_pods`, `create`, `action` and `wait_running` in the canonical shape. Today
   the payload is used exactly as returned — `normalise` exists in the interface but nothing calls it —
   so if the provider's field names differ, translate inside those methods and keep the names in one
   place.
3. Implement `ssh_argv` — how a caller actually reaches the machine — and `pull_artifacts` if you can
   fetch files without ssh. If you cannot, leave `pull_artifacts` unimplemented and the CLI falls
   back to `ssh` + `tar`.
4. Add `"vast": "rentguard.vast"` to `_MODULES`. That one line is the whole registration; the CLI
   resolves it through `make_provider`.
5. Configuration: there is no `[vast]` section. Provider settings are read from `cfg.runpod` by
   `cli._rp`, so a second provider either reuses that section or the config plumbing needs a change
   first. This is a known gap, not a design intention.
6. Prove it with the fake suite's money paths plus a mapping test for your own payloads: given a raw
   provider payload, `get` must return the canonical shape, and a gone instance must return `None`.

```python
from pathlib import Path
from rentguard.provider import Provider, ProviderError


class Vast(Provider):
    name = "vast"
    needs_api_key = True

    def __init__(self, api_key: str, timeout: int = 60):
        super().__init__(api_key)
        self._key = api_key

    def get(self, instance_id: str):
        ...  # None when it does not exist; never raise for "gone"

    def list_pods(self) -> list[dict]:
        ...

    def create(self, *, gpu_id, image, container_disk_gb, cloud, ports, name,
               volume_id=None, volume_mount="/workspace", env=None):
        ...  # the only method that starts billing

    def action(self, instance_id: str, action: str) -> None:
        ...

    def wait_running(self, instance_id, timeout_minutes=12.0, poll_s=10.0, log=print):
        ...

    def ssh_argv(self, instance: dict, key: Path | None = None):
        ...
```

Raise `rentguard.provider.ProviderError` for anything that can fail — a bad key, transport, a 4xx or
5xx from the provider. That is the one exception the CLI catches and turns into an error line rather
than a traceback.
