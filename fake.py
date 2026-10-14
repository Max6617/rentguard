"""The in-memory fake provider.

Every money path in rentguard goes through a provider, which means that without a fake, the only way
to test that the cap fires is to spend money trying to exceed it. A money guarantee that has never
been tested is a marketing claim, so the fake is part of the design rather than a testing nicety.

The fake reproduces the specific ways rented machines betray you:

- an instance that disappears while a run is open (a crash, a provider-side kill, someone else's
  console click) — money that no ledger row accounts for;
- an instance that reports ``RUNNING`` with zero uptime and null ports — a crash loop, which is not
  a machine you may upload to;
- an instance created but not yet usable, so ``wait_running`` has something to wait for;
- utilisation that goes to zero while the meter keeps running;
- an action that fails.

Payloads are shaped like RunPod's, because a fake that returns a convenient shape tests a program
against a world that does not exist.

Usage from a shell:

    RENTGUARD_PROVIDER=fake python -m rentguard.cli up --purpose demo

and from a test:

    fake = FakeProvider()
    fake.advance(3600)          # an hour passes for every running instance
    fake.disappear("fake-1")    # someone else's console click
"""

from __future__ import annotations

import fnmatch
import hashlib
import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .provider import Instance, Provider, ProviderError


@dataclass
class Artifact:
    """One file the fake machine has produced."""

    path: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class ArtifactStore(dict):
    """``instance_id -> {remote path: Artifact}``, with the two operations the CLI asks for."""

    def put(self, instance_id: str, path: str, data: bytes | str) -> Artifact:
        blob = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        artifact = Artifact(path=path, data=blob)
        self.setdefault(instance_id, {})[path] = artifact
        return artifact

    def match(self, instance_id: str, patterns: list[str]) -> dict[str, bytes]:
        files = self.get(instance_id, {})
        return {path: art.data for path, art in files.items()
                if any(fnmatch.fnmatch(path, pattern) for pattern in patterns)}


@dataclass
class FakeInstance:
    """One rented machine, with a mutable clock and utilisation."""

    id: str
    gpu_id: str = "Fake GPU 24GB"
    rate_usd_per_hour: float = 0.13
    status: str = "RUNNING"
    uptime_seconds: float = 0.0
    ports: dict | None = field(default_factory=lambda: {"22": "tcp", "8888": "http"})
    gpu_util: float = 0.0
    mem_util: float = 0.0
    present: bool = True  # False simulates the instance vanishing from the account
    volume: bool = False
    #: Uptime accumulated at ``created_at`` by simulated clocks (``advance``). Wall-clock time
    #: between processes is added on top by ``seconds_elapsed``, so uptime keeps moving when a
    #: later command runs against a reloaded fake.
    clock_offset: float = 0.0
    created_at: float = 0.0

    def seconds_elapsed(self, now: float) -> float:
        """Uptime now: what the simulated clock gave it, plus wall-clock time since it started."""
        extra = max(0.0, now - self.created_at) if self.created_at else 0.0
        return self.uptime_seconds + extra

    def payload(self, now: float | None = None) -> dict:
        """The canonical instance shape, plus the raw ``ssh`` block a provider would return."""
        clock = self._now() if now is None else now
        uptime = self.seconds_elapsed(clock)
        return {
            "id": self.id,
            "name": self.id,
            "status": self.status,
            "cost": self.rate_usd_per_hour,
            "gpu": {"id": self.gpu_id, "count": 1},
            "dataCenterId": "FAKE-1",
            "volumeId": "fake-volume" if self.volume else None,
            "runtime": {
                "uptime": int(uptime),
                "ports": self.ports,
                "gpus": [{"util": self.gpu_util, "memoryUtil": self.mem_util}],
            },
            "ssh": {
                "direct": {"host": "127.0.0.1", "port": 2222, "username": "fake"},
                "proxy": None,
            },
        }


class FakeProvider(Provider):
    """Deterministic provider for tests. No network, no clock drift, no money.

    Time only moves when a test calls ``advance``, and instances only change when the code under test
    calls the provider API. That determinism is the point: a test that measures spend must not also
    be measuring the test runner's speed.
    """

    name = "fake"
    needs_api_key = False
    terminal_states = ("EXITED", "TERMINATED")

    def __init__(self, api_key: str | None = None, *, rate_usd_per_hour: float = 0.13,
                 gpu_id: str = "Fake GPU 24GB", name: str = "",
                 state_path: str | Path | None = None) -> None:
        super().__init__(api_key)
        self.rate = rate_usd_per_hour
        self.gpu_id = gpu_id
        self.display_name = name or "Fake GPU 24GB"
        self.instances: dict[str, FakeInstance] = {}
        self.actions: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.fail_actions: set[str] = set()
        self.refuse_gpus = False  # set True to exercise the "no stock" path deliberately
        #: GPUs the catalog still lists but `create` refuses, as RunPod does when a catalogue hit is
        #: stale: "There are no longer any instances available with the requested specifications".
        #: This is the M2 finding — availability is not capacity — made reproducible.
        self.unavailable_gpus: set[str] = set()
        self.requested_gpus: list[list[str]] = []
        self.artifacts = ArtifactStore()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        #: Where this fake remembers itself between processes. ``None`` means "in memory only",
        #: which is what every test wants: a test constructing its own fake must never see state
        #: left on disk by another run. The CLI passes the state dir for `provider = "fake"`.
        self._state_path = Path(state_path) if state_path is not None else None
        self._restore()

    # ------------------------------------------------------------ persistence

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def save(self) -> None:
        """Persist the account, so the next process sees the machine this one rented.

        The documented offline flow is three commands in three processes. Without this, `up`
        creates `fake-1` in one process, `status` starts from an empty account, and the README's
        own example reports drift. Gated on ``provider = "fake"`` by ``cli._provider``, and a no-op
        when the fake was built without a state path, which is what tests do.
        """
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "version": 1,
            "next_id": next(self._ids),
            "instances": [
                {"id": i.id, "gpu_id": i.gpu_id, "rate_usd_per_hour": i.rate_usd_per_hour,
                 "status": i.status, "uptime_seconds": i.uptime_seconds, "ports": i.ports,
                 "gpu_util": i.gpu_util, "mem_util": i.mem_util, "present": i.present,
                 "volume": i.volume, "clock_offset": i.clock_offset, "created_at": i.created_at}
                for i in self.instances.values()
            ],
        }
        self._state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def _restore(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except ValueError:
            return
        now = self._now()
        for raw in state.get("instances", []):
            kwargs = {k: raw[k] for k in ("gpu_id", "rate_usd_per_hour", "status", "uptime_seconds",
                                          "ports", "gpu_util", "mem_util", "present", "volume")
                      if k in raw}
            kwargs["clock_offset"] = raw.get("clock_offset", raw.get("uptime_seconds", 0.0))
            kwargs["created_at"] = raw.get("created_at", now)
            self.instances[raw["id"]] = FakeInstance(id=raw["id"], **kwargs)
        self._ids = itertools.count(int(state.get("next_id") or len(self.instances) + 1))

    # ------------------------------------------------------------ test helpers

    def add(self, instance_id: str = "fake-1", **kwargs) -> FakeInstance:
        """Put a machine on the account without renting it through ``create``."""
        created_at = kwargs.pop("created_at", self._now())
        instance = FakeInstance(
            id=instance_id,
            gpu_id=kwargs.pop("gpu_id", self.gpu_id),
            rate_usd_per_hour=kwargs.pop("rate_usd_per_hour", self.rate),
            created_at=created_at,
            **kwargs,
        )
        instance.clock_offset = instance.uptime_seconds
        self.instances[instance_id] = instance
        return instance

    def advance(self, seconds: float, instance_id: str | None = None) -> None:
        """Time passes for every running machine. Nothing else happens on its own."""
        for iid, instance in self.instances.items():
            if instance_id is not None and iid != instance_id:
                continue
            if instance.status == "RUNNING" and instance.present:
                instance.uptime_seconds += seconds
                instance.clock_offset += seconds
        self.save()

    def disappear(self, instance_id: str) -> None:
        """The instance is gone from the account: a crash, a kill, someone else's click."""
        instance = self.instances.get(instance_id)
        if instance is not None:
            instance.present = False
            self.save()

    def crash_loop(self, instance_id: str) -> None:
        """RUNNING with zero uptime and null ports. Not a machine you may upload to."""
        instance = self.instances[instance_id]
        instance.status = "RUNNING"
        instance.uptime_seconds = 0.0
        instance.clock_offset = 0.0
        instance.created_at = self._now()
        instance.ports = None
        self.save()

    def idle(self, instance_id: str, seconds: float = 0.0) -> None:
        """The GPU stops working while the meter keeps running."""
        instance = self.instances[instance_id]
        instance.gpu_util = 0.0
        instance.mem_util = 0.0
        self.advance(seconds)
        self.save()

    def busy(self, instance_id: str, util: float = 95.0) -> None:
        instance = self.instances[instance_id]
        instance.gpu_util = util
        instance.mem_util = min(util, 90.0)

    def put(self, path: str, data: bytes | str, instance_id: str | None = None) -> Artifact:
        """Give a machine a file, so the artefact path has something to pull."""
        target = instance_id or next(iter(self.instances), "")
        return self.artifacts.put(target, path, data)

    def candidates(self, preference: list[str], cloud: str = "SECURE",
                   need_memory_gb: int | None = None) -> list[str]:
        """The GPUs `up` may try, best first.

        The fake owns exactly one GPU, so it is always on the list: the base implementation refuses
        a preference list that does not name it, which makes the documented offline flow —
        `RENTGUARD_PROVIDER=fake rentguard up`, from this module's own docstring — fail on a fresh
        config.

        ``unavailable_gpus`` entries **stay on the list**, because that is the M2 defect being
        modelled: the catalogue says the GPU is there and ``create`` refuses it. If this method
        filtered them out it would hide the very path `up` has to walk, and the fallback would look
        tested when it had never run. Set ``refuse_gpus`` for the no-stock path where nothing is
        offered at all.
        """
        self.requested_gpus.append(list(preference))
        if self.refuse_gpus:
            raise ProviderError(f"none of the preferred GPUs is available: {list(preference)}")
        offered = list(preference)
        if self.gpu_id not in offered:
            offered.append(self.gpu_id)
        if not offered:
            raise ProviderError(f"none of the preferred GPUs is available: {list(preference)}")
        return offered

    def choose_gpu(self, preference: list[str], cloud: str = "SECURE",
                   need_memory_gb: int | None = None) -> str:
        """The first GPU this fake will accept."""
        return self.candidates(preference, cloud, need_memory_gb)[0]

    # ------------------------------------------------------------ Provider API

    def get(self, instance_id: str) -> Instance | None:
        instance = self.instances.get(instance_id)
        if instance is None or not instance.present:
            return None
        return instance.payload(self._now())

    def list_pods(self) -> list[Instance]:
        now = self._now()
        return [i.payload(now) for i in self.instances.values() if i.present]

    def wait_running(self, instance_id: str, timeout_minutes: float = 12.0,
                     poll_s: float = 10.0, log: Callable[[str], Any] = print) -> Instance:
        """Succeed as soon as the machine is usable; fail the way the provider would.

        A machine created by ``create`` starts with zero uptime, exactly as a real pod does, so this
        is where a test can prove that rentguard waits rather than uploading to an unusable box.
        """
        instance = self.instances.get(instance_id)
        if instance is None or not instance.present:
            raise ProviderError(f"instance {instance_id} does not exist")
        if instance.status in self.terminal_states:
            raise ProviderError(f"instance went to {instance.status}")
        now = self._now()
        pod = instance.payload(now)
        if not self.is_alive(pod):
            # Deterministic: one simulated second of provisioning, no real sleep. A machine becomes
            # usable when it has both billed and published a port, exactly as ``action("start")``
            # makes one. Setting only the uptime would leave every freshly created pod permanently
            # unusable, which is the crash loop — not provisioning.
            with self._lock:
                instance.status = "RUNNING"
                instance.uptime_seconds = max(instance.uptime_seconds, 1.0)
                instance.clock_offset = max(instance.clock_offset, 1.0)
                if not instance.ports:
                    instance.ports = {"22": "tcp", "8888": "http"}
            self.save()
            pod = instance.payload(now)
        if not self.is_alive(pod):
            raise ProviderError(
                f"instance {instance_id} not usable (uptime={pod['runtime'].get('uptime')}, "
                f"ports={'set' if pod['runtime'].get('ports') else 'NULL'})")
        return pod

    def create(self, *, gpu_id: str, image: str, container_disk_gb: int, cloud: str,
               ports: list[str], name: str, volume_id: str | None = None,
               volume_mount: str = "/workspace", env: dict | None = None) -> Instance:
        if gpu_id in self.unavailable_gpus:
            # Exactly what the provider answers when the catalogue was stale: the row said available,
            # the request is refused. `up` is expected to walk to the next preference from here.
            raise ProviderError(
                f"no longer any instances available with the requested specifications ({gpu_id})")
        with self._lock:
            instance_id = f"fake-{next(self._ids)}"
        request = {"gpu_id": gpu_id, "image": image, "container_disk_gb": container_disk_gb,
                   "cloud": cloud, "ports": list(ports), "name": name,
                   "volume_id": volume_id, "volume_mount": volume_mount, "env": env}
        self.created.append(request)
        instance = self.add(instance_id, gpu_id=gpu_id, status="PROVISIONING",
                            uptime_seconds=0.0, ports=None)
        self.save()
        return instance.payload(self._now())

    def action(self, instance_id: str, action: str) -> None:
        if action not in ("start", "stop", "terminate"):
            raise ProviderError(f"unknown action {action!r}")
        self.actions.append((instance_id, action))
        instance = self.instances.get(instance_id)
        if instance is None:
            raise ProviderError(f"no instance {instance_id}")
        if action in self.fail_actions:
            raise ProviderError(f"action {action} refused by the fake")
        if action == "stop":
            instance.status = "EXITED"
            instance.ports = None
        elif action == "terminate":
            instance.status = "TERMINATED"
            instance.present = False
        else:
            instance.status = "RUNNING"
            instance.uptime_seconds = max(instance.uptime_seconds, 1.0)
            instance.clock_offset = max(instance.clock_offset, 1.0)
            if instance.ports is None:
                instance.ports = {"22": "tcp", "8888": "http"}
        self.save()

    # ------------------------------------------------------------ catalog

    def catalog(self) -> list[dict]:
        return [{"id": self.gpu_id, "name": self.display_name, "memory": 24,
                 "secure": "HIGH", "community": "HIGH",
                 "price": {"secure": self.rate, "community": self.rate}}]

    # ------------------------------------------------------------ access and artefacts

    def ssh_argv(self, instance: Instance, key: Path | None = None) -> tuple[list[str], str]:
        # The fake has no ssh; a test that needs the remote command replaced sets ``command_runner``.
        return [], "fake@127.0.0.1"

    def pull_artifacts(self, instance: Instance, patterns: list[str]) -> dict[str, bytes]:
        return self.artifacts.match(str(instance.get("id")), patterns)


#: Every provider module exposes a class named ``Provider``, which is what the registry in
#: ``provider.py`` reads. The fake keeps the descriptive name as well so tests can say
#: ``from rentguard.fake import FakeProvider``.
Provider = FakeProvider
