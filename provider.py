"""The provider interface, and the registry that turns a config name into an instance.

A provider is a *thin* thing. It knows how to rent, inspect, stop and reach one kind of machine, and
nothing else. It does not know about the cap, the ledger, the guard or artefacts: those live above
it, identical for every provider. That split is why a second provider — Vast, Lambda, a local box
under systemd — can be added by writing one module and registering one name, without touching
``ledger.py``, ``guard.py``, ``cli.py`` or the artefact logic.

Canonical payload shapes
------------------------

Every provider returns the same dictionaries, so nothing above this layer parses a provider-specific
schema. Keys marked * may be missing.

    instance = {
        "id": str,                      # the provider's identifier for the machine
        "name": str*,                   # display name
        "status": str,                  # provider status, upper-case: RUNNING, EXITED, ...
        "cost": float,                  # USD per hour for the whole instance
        "gpu": {"id": str, "count": int}*,
        "runtime": {                    # present once the machine reports itself
            "uptime": float,            # seconds since it started billing
            "ports": dict | None,       # None/absent means "no reachable port yet"
            "gpus": [{"util": float, "memoryUtil": float}]*,   # percent
        },
        "ssh": {"direct": {"host", "port", "username"},
                "proxy": {"host", "port", "username"}},        # at least one, when reachable
    }

    gpu = {
        "id": str,                      # exactly what ``create(gpu_id=...)`` accepts
        "memory": int*,                 # GiB
        "price": {"secure": float, "community": float}*,        # USD per hour
        "secure": str, "community": str,   # availability: "High"|"Medium"|"Low"|"None"
    }

The contract, in full
---------------------

- ``get`` returns the canonical instance, or ``None`` when the instance does not exist. A provider
  must not raise for "gone": gone is a normal answer that the guard and ``doctor`` need.
- ``get`` and ``list_pods`` never invent fields. A null or missing uptime stays null; ``is_alive``
  is the single place that decides what null means.
- ``list_pods`` returns every instance on the account, whatever its state.
- ``create`` starts billing. It is the only method that does, and ``up`` is the only caller, after
  the cap has been checked.
- ``action(instance_id, "stop"|"start"|"terminate")`` changes machine state at the provider.
- Money is never read. No provider exposes a reliable balance, so this interface has no method for
  it, on purpose.

To add a provider: subclass ``Provider`` in a new module, set ``name`` and ``needs_api_key``,
implement the methods above, then add one line to ``_MODULES`` below.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Callable, Iterable


class ProviderError(RuntimeError):
    """Anything a provider call can fail with: bad key, transport, provider 4xx/5xx."""


def is_alive(instance: dict) -> bool:
    """True only when a machine is genuinely usable: it has billed for a positive time *and* it
    exposes a port. ``status == "RUNNING"`` alone is a claim, not a state.

    This is a property of the payload, not of the provider, so it is a module-level function and
    not something an implementation can get wrong: every provider, and every test double, reports
    the same two fields, and this is the one place that decides what they mean.
    """
    runtime = (instance or {}).get("runtime") or {}
    uptime = runtime.get("uptime")
    ports = runtime.get("ports")
    return bool(uptime and float(uptime) > 0 and ports)


#: Provider payloads are dictionaries whose keys are fixed by the contract above.
Instance = dict[str, Any]
Gpu = dict[str, Any]


class Provider:
    """The interface every provider implements. The defaults are the honest no-ops.

    Optional test seams, all ``None`` in production, let a suite drive the real CLI end to end with
    no network: ``artifacts`` (pull artefacts from memory), ``command_runner`` (run a remote command
    in memory), ``terminal_states`` (statuses that mean "not billing").
    """

    #: Name used in ``provider = "..."`` in rentguard.toml.
    name: str = ""
    #: Whether an API key must be present before this provider can be constructed.
    needs_api_key: bool = True
    #: Statuses that mean "this machine exists but is not billing".
    terminal_states: tuple[str, ...] = ("EXITED", "TERMINATED")

    #: Test seams. ``machine`` is the provider's own handle, passed back by ``get``-time callers.
    command_runner: Callable[[list[str], str], int] | None = None
    artifacts: Any = None

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    # ------------------------------------------------------------------ instances

    def get(self, instance_id: str) -> Instance | None:
        raise NotImplementedError

    def list_pods(self) -> list[Instance]:
        raise NotImplementedError

    def is_alive(self, instance: Instance) -> bool:
        """Kept as a method for callers that hold a provider; the decision itself is the module
        function above, so a provider cannot implement a different answer."""
        return is_alive(instance)

    def wait_running(self, instance_id: str, timeout_minutes: float = 12.0, poll_s: float = 10.0,
                     log: Callable[[str], Any] = print) -> Instance:
        raise NotImplementedError

    def create(self, *, gpu_id: str, image: str, container_disk_gb: int, cloud: str,
               ports: Iterable[str], name: str, volume_id: str | None = None,
               volume_mount: str = "/workspace", env: dict | None = None) -> Instance:
        raise NotImplementedError

    def action(self, instance_id: str, action: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ catalog

    def catalog(self) -> list[Gpu]:
        """Everything the provider can rent, with prices and availability."""
        return []

    def candidates(self, preference: list[str], cloud: str = "SECURE",
                   need_memory_gb: int | None = None) -> list[str]:
        """Every preferred GPU that looks available in this tier, best first.

        This is the list `up` walks when a catalogue hit turns out to be stale: a GPU can be offered
        in the catalogue and still be refused at creation ("no longer any instances available").
        Raises only when the list is empty; a caller that wants one GPU calls ``choose_gpu``.
        """
        rows = {gpu.get("id"): gpu for gpu in self.catalog()}
        offered: list[str] = []
        for gpu_id in preference:
            row = rows.get(gpu_id)
            if row is None:
                continue
            if need_memory_gb and int(row.get("memory") or 0) < need_memory_gb:
                continue
            if _offered(row.get(cloud.lower(), row.get(cloud))):
                offered.append(gpu_id)
        if not offered:
            raise ProviderError(f"none of the preferred GPUs is available in {cloud}: {preference}")
        return offered

    def choose_gpu(self, preference: list[str], cloud: str = "SECURE",
                   need_memory_gb: int | None = None) -> str:
        """First preferred GPU that is available in the requested cloud tier."""
        return self.candidates(preference, cloud, need_memory_gb)[0]

    def price_of(self, gpu_id: str, cloud: str = "SECURE") -> float | None:
        for gpu in self.catalog():
            if gpu.get("id") == gpu_id:
                price = (gpu.get("price") or {}).get(cloud.lower())
                return float(price) if price is not None else None
        return None

    # ------------------------------------------------------------------ access and artefacts

    def ssh_argv(self, instance: Instance, key: Path | None = None) -> tuple[list[str], str]:
        """SSH options and ``user@host`` for a reachable instance.

        Consumers prepend their own ssh binary. Raises when the instance is not reachable yet.
        """
        ssh = instance.get("ssh") or {}
        direct = ssh.get("direct")
        proxy = ssh.get("proxy")
        if direct:
            return ["-p", str(direct["port"])], f"{direct['username']}@{direct['host']}"
        if proxy:
            return [], f"{proxy['username']}@{proxy['host']}"
        raise ProviderError("the instance has no ssh endpoint yet (is it RUNNING?)")

    def pull_artifacts(self, instance: Instance, patterns: list[str]) -> dict[str, bytes]:
        """Fetch the matching remote files into memory.

        Providers that cannot do this (everything that reaches the machine over ssh) inherit the
        NotImplementedError, and the CLI falls back to ssh + tar. The in-memory fake implements it,
        which is what makes the artefact path testable without a network.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def normalise(instance: dict) -> Instance:
        """Turn a provider-specific payload into the canonical instance shape.

        The base implementation assumes the payload is already canonical, so a new provider works
        before it has customised anything. Override only where the provider's names differ.
        """
        return dict(instance)


#: Provider name -> module holding its ``Provider`` subclass. One line per provider is the whole
#: integration cost; nothing else in rentguard needs to learn a new name.
_MODULES = {
    "runpod": "rentguard.runpod",
    "fake": "rentguard.fake",
}


def _offered(availability: Any) -> bool:
    """Whether a catalog row offers this cloud tier.

    Providers report availability two ways: a per-tier boolean, and a stock level as text
    ("High"/"Medium"/"Low"/"None"). ``False`` means *not offered in this tier* — but a check that
    only excludes the strings "None"/"NULL"/"" reads ``False`` as available, so a GPU that exists
    only in the community tier can be chosen for a secure rental and then fail at the API. Ask the
    provider what it means rather than truth-testing it.
    """
    if availability is None:
        return False
    if isinstance(availability, bool):
        return availability
    return str(availability).strip().upper() not in ("", "NONE", "NULL", "FALSE")


def available() -> list[str]:
    """Registered provider names, sorted. Used by ``init`` to document the real choices."""
    return sorted(_MODULES)


def resolve(name: str) -> type[Provider]:
    """Return the ``Provider`` subclass registered under ``name``.

    Found by *name attribute*, not by looking up an attribute called ``Provider`` in the module.
    Every provider module does ``from .provider import Provider`` to subclass it, which shadows any
    such attribute with the base class itself — and the base class is exactly the thing whose
    methods raise NotImplementedError. Resolving that way silently disables every real provider
    while the fake (which happens to alias ``Provider = FakeProvider``) keeps working, so the test
    suite stays green and the tool is broken where it matters.
    """
    key = (name or "").strip().lower()
    module_name = _MODULES.get(key)
    if module_name is None:
        raise ProviderError(f"unknown provider {name!r}; known providers: {', '.join(available())}")
    module = importlib.import_module(module_name)
    for obj in vars(module).values():
        if (isinstance(obj, type) and issubclass(obj, Provider) and obj is not Provider
                and getattr(obj, "name", "") == key):
            return obj
    raise ProviderError(
        f"provider module {module_name!r} defines no Provider subclass named {key!r}")


def build(name: str, api_key: str | None = None, **kwargs) -> Provider:
    """Construct the provider named in the config.

    A provider that does not need a key (the in-memory fake) is built without one; a provider that
    does is never built silently without one.
    """
    cls = resolve(name)
    if cls.needs_api_key and not api_key:
        raise ProviderError(
            "no API key available. Set $DSH_SECRET_RUNPOD_API_KEY or $RUNPOD_API_KEY, or set "
            f"RENTGUARD_PROVIDER=fake to run without one (provider {cls.name or name}).")
    return cls(api_key, **kwargs)


# ---------------------------------------------------------------------- config glue

def provider_name(cfg: Any) -> str:
    """Which provider to use: the environment wins, then the config, then RunPod.

    ``RENTGUARD_PROVIDER`` exists so a test can drive the real CLI against the fake without editing
    anyone's config file — and so a person can try the fake with one variable.
    """
    return (os.environ.get("RENTGUARD_PROVIDER") or getattr(cfg, "provider", "runpod")).strip()


def make_provider(cfg: Any, api_key: str | None = None, **kwargs) -> Provider:
    """Build the provider for ``cfg``. The one construction path, CLI and MCP alike."""
    from .config import api_key as read_api_key

    return build(provider_name(cfg), api_key if api_key is not None else read_api_key(), **kwargs)
