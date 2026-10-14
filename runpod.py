"""RunPod provider.

Zero dependencies on purpose: urllib, not requests, so `pip install rentguard` cannot collide with
anything in your environment.

Two hard-won rules are encoded here, both from a run that wasted an hour:

1. **`status: RUNNING` is a claim, not a state.** A pod can report RUNNING with `uptime = 0` and
   `ports = null` for minutes while it is crash-looping. Such a pod is not usable and must not be
   uploaded to. `is_alive` requires a positive uptime *and* a non-null port set.
2. **A null billing total means "unavailable", not "$0.00".** Nothing in this module reads the
   account balance, because there is no endpoint that reports it reliably.

This is the only module in rentguard that knows RunPod's field names: ``normalise`` and
``ssh_argv`` translate its payloads into the canonical shape documented in ``provider.py``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .provider import Provider, ProviderError


class RunPod(Provider):
    name = "runpod"
    needs_api_key = True
    API = "https://api.runpod.io"

    def __init__(self, api_key: str, timeout: int = 60):
        if not api_key:
            raise ProviderError("no RunPod API key")
        super().__init__(api_key)
        self._key = api_key
        self.timeout = timeout
        # GPU request floors; kept here because they are RunPod request fields, not rentguard ideas.
        self.min_ram_per_gpu = 32
        self.min_vcpu = 8

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None, retries: int = 3) -> dict:
        url = f"{self.API}{path}"
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                if exc.code == 429:
                    last = ProviderError(f"rate limited: {detail}")
                    time.sleep(3 * (attempt + 1))
                    continue
                if exc.code >= 500:
                    last = ProviderError(f"{exc.code}: {detail}")
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError(f"{method} {path} -> {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(2 ** attempt)
        raise ProviderError(f"{method} {path} failed after {retries} attempts: {last}")

    # ---------------------------------------------------------------- pods

    def get(self, instance_id: str) -> dict | None:
        try:
            return self._request("GET", f"/v2/pods/{instance_id}")
        except ProviderError as exc:
            if "404" in str(exc):
                return None
            raise

    def wait_running(self, instance_id: str, timeout_minutes: float = 12.0,
                     poll_s: float = 10.0, log=print) -> dict:
        deadline = time.time() + timeout_minutes * 60
        pod: dict = {}
        while time.time() < deadline:
            pod = self.get(instance_id) or {}
            status = pod.get("status")
            if status in ("ERROR", "TERMINATED"):
                raise ProviderError(f"instance went to {status}: {json.dumps(pod)[:200]}")
            if self.is_alive(pod):
                return pod
            if status == "RUNNING":
                runtime = pod.get("runtime") or {}
                log(f"  RUNNING but not usable yet "
                    f"(uptime={runtime.get('uptime')}, ports="
                    f"{'set' if runtime.get('ports') else 'NULL'}) — waiting")
            time.sleep(poll_s)
        raise ProviderError(f"instance {instance_id} not usable after {timeout_minutes} min "
                            f"(last status {pod.get('status')})")

    def create(self, *, gpu_id: str, image: str, container_disk_gb: int, cloud: str,
               ports: list[str], name: str, volume_id: str | None = None,
               volume_mount: str = "/workspace", env: dict | None = None) -> dict:
        body: dict = {
            "name": name,
            "image": image,
            "cloud": cloud,
            "disk": container_disk_gb,
            "ports": ports,
            "startSsh": True,
            "gpu": {"id": gpu_id, "count": 1, "minRamPerGpu": self.min_ram_per_gpu,
                    "minVcpuCountPerGpu": self.min_vcpu},
        }
        if volume_id:
            body["mounts"] = {"network": [{"volumeId": volume_id, "path": volume_mount}]}
        if env:
            body["env"] = env
        return self._request("POST", "/v2/pods", body=body)

    def action(self, instance_id: str, action: str) -> None:
        assert action in ("start", "stop", "terminate")
        self._request("POST", f"/v2/pods/{instance_id}/action", body={"action": action})

    def list_pods(self) -> list[dict]:
        payload = self._request("GET", "/v2/pods")
        return payload if isinstance(payload, list) else payload.get("pods", [])

    # ---------------------------------------------------------------- catalog

    def catalog(self) -> list[dict]:
        payload = self._request("GET", "/v2/catalog/gpus",
                                params={"include": "AVAILABILITY", "product": "POD"})
        return payload.get("gpus", [])

    # choose_gpu and price_of are inherited: the availability and price fields in RunPod's catalog
    # are exactly the canonical ones documented in provider.py.

    # ---------------------------------------------------------------- storage

    def volumes(self) -> list[dict]:
        """Network volumes, in the canonical shape, with what they cost while nothing runs.

        RunPod bills a volume whether or not a pod exists, so this is a *continuous* cost that no
        ledger row covers. ``attached_to`` is left to the caller, which is the only place that knows
        which pods exist: a volume is attached when a pod names it.
        """
        payload = self._request("GET", "/v2/network-volumes")
        rows = payload if isinstance(payload, list) else payload.get("volumes", [])
        volumes: list[dict] = []
        for row in rows:
            size = float(row.get("size") or 0.0)
            volumes.append({
                "id": row.get("id"),
                "name": row.get("name") or "(unnamed)",
                "size_gb": size,
                "region": row.get("dataCenterId") or row.get("dataCenterIds") or "",
                "attached_to": None,
                "monthly_usd": self.volume_monthly_usd(size),
            })
        return volumes

    # ---------------------------------------------------------------- access

    def normalise(self, instance: dict) -> dict:
        """Canonical instance, with RunPod's ``ssh`` block preserved under ``machine``.

        A provider's ssh block is not part of the portable shape — the point of keeping it nested is
        that a future provider with a different access mechanism cannot accidentally be read here.
        """
        pod = super().normalise(instance)
        pod["machine"] = {"ssh": instance.get("ssh") or {},
                          "dataCenterId": instance.get("dataCenterId")}
        return pod

    def ssh_argv(self, instance: dict, key: Path | None) -> tuple[list[str], str]:
        pod = instance.get("machine") if "machine" in instance else instance
        ssh = (pod or {}).get("ssh") or {}
        direct = ssh.get("direct")
        proxy = ssh.get("proxy")
        common = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL"]
        if key:
            common = ["-i", str(key), *common]
        if direct:
            return [*common, "-p", str(direct["port"])], f"{direct['username']}@{direct['host']}"
        if proxy:
            return common, f"{proxy['username']}@{proxy['host']}"
        raise ProviderError("the instance has no ssh endpoint yet (is it RUNNING?)")

    #: The pre-interface spelling, kept pointed at the one implementation so old imports work and
    #: new ones cannot drift from them.
    ssh_args = ssh_argv
