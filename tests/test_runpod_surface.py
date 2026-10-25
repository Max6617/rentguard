"""The RunPod surface the transport tests do not reach, and the provider defaults under it.

`test_runpod_transport.py` drives status codes and payload shapes; it leaves the parts that only a
*live-shaped* call touches: the query string `_request` builds for a params call, the "RUNNING but
not usable yet" wait, `force_stop`'s one-shot request, network-volume parsing, and the PTY route.
Those are the two modules where tonight's defects lived, so they are worth more than the percentage.

Everything here is offline: `urllib.request.urlopen` is replaced, or `_request` is, and nothing
touches RunPod. The provider.py half asserts the base-class defaults a provider inherits, including
the one that makes `force_stop` work for a provider that never overrides it.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import provider as provider_module  # noqa: E402
from rentguard.provider import Provider, ProviderError  # noqa: E402
from rentguard.runpod import RunPod  # noqa: E402

KEY = "test-key-not-real"


def alive_pod(instance_id: str = "p") -> dict:
    return {"id": instance_id, "status": "RUNNING",
            "runtime": {"uptime": 30, "ports": {"22": "tcp"}, "gpus": [{"util": 10}]}}


class FakeResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RequestQueryTest(unittest.TestCase):
    def test_a_params_call_is_built_into_a_query_string(self):
        """Effect: a caller that passes params gets them on the URL. `catalog()` is the one live
        example, and a silently dropped query is a silently different catalogue."""
        pod = RunPod(KEY)
        seen: list[str] = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            return FakeResponse(b'{"gpus": []}')

        with mock.patch("rentguard.runpod.urllib.request.urlopen", fake_urlopen):
            pod.catalog()

        self.assertTrue(seen, "no request was made")
        self.assertIn("include=AVAILABILITY", seen[0])
        self.assertIn("product=POD", seen[0])
        self.assertTrue(seen[0].startswith("https://api.runpod.io/v2/catalog/gpus?"), seen[0])


class WaitRunningLogTest(unittest.TestCase):
    def test_a_running_but_unusable_reading_is_logged_and_waited_through(self):
        """Sequence: the first reading is RUNNING with uptime 0 and no port, the second is usable.

        Effect: the caller is told what it is waiting for, and `wait_running` returns the usable
        payload instead of treating the first reading as either success or failure.
        """
        pod = RunPod(KEY)
        unusable = {"id": "p", "status": "RUNNING",
                    "runtime": {"uptime": 0, "ports": None, "gpus": []}}
        good = alive_pod()
        samples = [unusable, good]
        pod.get = lambda _id: samples.pop(0) if samples else good   # type: ignore[method-assign]
        lines: list[str] = []

        with mock.patch("rentguard.runpod.time.sleep", lambda *_: None):
            result = pod.wait_running("p", timeout_minutes=1, poll_s=0, log=lines.append)

        self.assertEqual(result["id"], "p")
        self.assertTrue(any("not usable yet" in line for line in lines),
                        f"the wait was not explained: {lines}")
        self.assertTrue(any("uptime=0" in line and "NULL" in line for line in lines),
                        f"the reason was not reported: {lines}")


class ForceStopTest(unittest.TestCase):
    def test_force_stop_is_one_shot_with_a_short_timeout(self):
        """Effect: the escalation has no retry loop and uses the short socket timeout, which is the
        whole difference between it and an ordinary stop that can hang."""
        pod = RunPod(KEY)
        calls: list[tuple] = []

        def fake_request(method, path, body=None, params=None, retries=3, timeout=None):
            calls.append((method, path, body, retries, timeout))
            return {}

        pod._request = fake_request   # type: ignore[method-assign]

        pod.force_stop("pod-1")

        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/v2/pods/pod-1/action")
        self.assertEqual(calls[0][2], {"action": "stop"})
        self.assertEqual(calls[0][3], 1, "the escalation retried, so it can still hang")
        self.assertEqual(calls[0][4], pod.force_stop_timeout,
                         "the escalation did not use the short socket timeout")


class VolumesTest(unittest.TestCase):
    def _with_payload(self, payload) -> list[dict]:
        pod = RunPod(KEY)
        pod._request = lambda *a, **k: payload   # type: ignore[method-assign]
        return pod.volumes()

    def test_a_volume_bills_a_monthly_cost_and_keeps_its_region(self):
        """Effect: the continuous cost of a volume reaches the report, with the field spelling the
        live payload uses (`dataCenter`) and the older one (`dataCenterId`)."""
        rows = self._with_payload({"networkVolumes": [
            {"id": "vol-1", "name": "weights", "size": 100, "dataCenter": "EU-RO-1"},
            {"id": "vol-2", "size": "50", "dataCenterId": "US-IL-1"},
        ]})

        self.assertEqual([row["id"] for row in rows], ["vol-1", "vol-2"])
        self.assertEqual(rows[0]["name"], "weights")
        self.assertEqual(rows[0]["region"], "EU-RO-1")
        self.assertEqual(rows[1]["name"], "(unnamed)", "a nameless volume should be labelled")
        self.assertEqual(rows[1]["region"], "US-IL-1")
        self.assertEqual(rows[0]["size_gb"], 100.0)
        self.assertEqual(rows[1]["size_gb"], 50.0)
        expected = round(100.0 * provider_module.Provider.volume_usd_per_gb_month, 4)
        self.assertEqual(rows[0]["monthly_usd"], expected)
        self.assertIsNone(rows[0]["attached_to"])

    def test_a_list_payload_and_an_empty_one_both_work(self):
        """Effect: the shape RunPod actually returns is not the only one accepted, and a volume
        field the account does not have is an empty list rather than an exception."""
        self.assertEqual(len(self._with_payload([{"id": "v", "size": 10}])), 1)
        self.assertEqual(self._with_payload({}), [])


class RouteTest(unittest.TestCase):
    def test_only_the_proxy_route_requires_a_pty(self):
        """Effect: the PTY flag follows the route, because a proxy session without one answers
        "Error: Your SSH client doesn't support PTY" and the command never runs."""
        direct = {"ssh": {"direct": {"host": "h", "port": 1, "username": "u"}}}
        proxy = {"ssh": {"proxy": {"host": "p", "username": "u"}}}

        self.assertFalse(RunPod(KEY).ssh_requires_pty(direct))
        self.assertTrue(RunPod(KEY).ssh_requires_pty(proxy))


class ProviderDefaultsTest(unittest.TestCase):
    def test_the_default_force_stop_is_the_ordinary_stop(self):
        """Effect: a provider that never overrides `force_stop` still gets the escalation, otherwise
        the bound in `down` would do nothing for every provider added after RunPod."""
        actions: list[tuple[str, str]] = []

        class Bare(Provider):
            name = "bare"

            def action(self, instance_id, action):
                actions.append((instance_id, action))

        Bare().force_stop("pod-1")

        self.assertEqual(actions, [("pod-1", "stop")])

    def test_a_provider_with_no_storage_reports_none_and_prices_a_volume_by_the_gb(self):
        """Effect: the base class answers for a provider without volumes, and the flat per-GB price
        is one place, so a provider cannot invent its own arithmetic."""
        base = Provider()

        self.assertEqual(base.volumes(), [])
        self.assertEqual(base.volume_monthly_usd(100), 7.0)
        self.assertEqual(base.volume_monthly_usd(0), 0.0)

    def test_resolve_refuses_a_module_that_defines_no_matching_provider(self):
        """Sequence: the registry points at a module that has no `Provider` subclass named for it
        (the shape of a typo in the one-line registration).

        Effect: a clear ProviderError naming the module, not a silent fall back to the base class
        whose every method raises NotImplementedError.
        """
        with mock.patch.dict(provider_module._MODULES, {"json": "json"}):
            with self.assertRaises(ProviderError) as caught:
                provider_module.resolve("json")

        self.assertIn("defines no Provider subclass", str(caught.exception))
        self.assertIn("json", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
