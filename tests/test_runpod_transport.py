"""RunPod's transport and payload handling, with no network.

`runpod.py` is the module where every defect the paid runs found lives, and it was the least covered
file in the tree at 21%. Nothing here talks to RunPod: either `_request` is replaced, or
`urllib.request.urlopen` itself is, so the retry and error-mapping code is exercised for real.

Two levels on purpose:

- **Transport level** stubs `urlopen` and drives status codes — 404 must not be retried, 429 and 5xx
  must be, and a malformed body must not crash the tool.
- **Method level** stubs `_request` and asserts what each call *asks for* — the create payload, the
  action name, the catalog parameters. A test that only stubs the method cannot see a wrong body.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard.provider import ProviderError  # noqa: E402
from rentguard.runpod import RunPod  # noqa: E402

KEY = "test-key-not-real"


class FakeResponse:
    """The bit of an HTTP response `_request` actually uses."""

    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, detail: bytes = b'{"detail":"nope"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.runpod.io/x", code, "err", {}, io.BytesIO(detail))


class RequestTest(unittest.TestCase):
    """The transport itself: status codes, retries, malformed bodies."""

    def setUp(self) -> None:
        self.pod = RunPod(KEY)
        # Sleeps are real in _request; the tests want the behaviour, not the delay.
        patcher = mock.patch("rentguard.runpod.time.sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_200_returns_parsed_json(self) -> None:
        body = b'{"id": "abc", "status": "RUNNING"}'
        with mock.patch("rentguard.runpod.urllib.request.urlopen", return_value=FakeResponse(body)):
            self.assertEqual(self.pod._request("GET", "/v2/pods/abc"),
                             {"id": "abc", "status": "RUNNING"})

    def test_an_empty_body_is_an_empty_dict_not_a_crash(self) -> None:
        with mock.patch("rentguard.runpod.urllib.request.urlopen", return_value=FakeResponse(b"")):
            self.assertEqual(self.pod._request("GET", "/v2/pods/abc"), {})

    def test_a_404_is_not_retried_and_names_the_code(self) -> None:
        # Retrying a 404 would be four wasted calls on a machine that does not exist, and `get`
        # depends on the code appearing in the message to return None.
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        side_effect=http_error(404)) as urlopen:
            with self.assertRaises(ProviderError) as caught:
                self.pod._request("GET", "/v2/pods/gone")
        self.assertIn("404", str(caught.exception))
        self.assertEqual(urlopen.call_count, 1)

    def test_a_400_is_not_retried(self) -> None:
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        side_effect=http_error(400, b'{"detail":"bad request"}')) as urlopen:
            with self.assertRaises(ProviderError) as caught:
                self.pod._request("POST", "/v2/pods", body={})
        self.assertIn("bad request", str(caught.exception))
        self.assertEqual(urlopen.call_count, 1)

    def test_a_500_is_retried_then_raises(self) -> None:
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        side_effect=http_error(500)) as urlopen:
            with self.assertRaises(ProviderError) as caught:
                self.pod._request("GET", "/v2/pods", retries=3)
        self.assertEqual(urlopen.call_count, 3)
        self.assertIn("after 3 attempts", str(caught.exception))

    def test_a_500_that_clears_returns_the_body(self) -> None:
        # The retry must return a good answer rather than only ever giving up gracefully.
        effects = [http_error(500), FakeResponse(b'{"ok": true}')]
        with mock.patch("rentguard.runpod.urllib.request.urlopen", side_effect=effects):
            self.assertEqual(self.pod._request("GET", "/v2/pods"), {"ok": True})

    def test_a_429_is_retried(self) -> None:
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        side_effect=http_error(429, b'{"detail":"slow down"}')) as urlopen:
            with self.assertRaises(ProviderError) as caught:
                self.pod._request("GET", "/v2/pods", retries=2)
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("rate limited", str(caught.exception))

    def test_a_malformed_body_is_retried_then_raises(self) -> None:
        # A proxy returning HTML must produce a provider error, never a JSONDecodeError escaping
        # into the CLI.
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        return_value=FakeResponse(b"<html>not json</html>")) as urlopen:
            with self.assertRaises(ProviderError):
                self.pod._request("GET", "/v2/pods", retries=2)
        self.assertEqual(urlopen.call_count, 2)

    def test_a_network_error_is_retried_then_raises(self) -> None:
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("dns")) as urlopen:
            with self.assertRaises(ProviderError):
                self.pod._request("GET", "/v2/pods", retries=2)
        self.assertEqual(urlopen.call_count, 2)

    def test_a_timeout_is_retried_then_raises(self) -> None:
        with mock.patch("rentguard.runpod.urllib.request.urlopen",
                        side_effect=TimeoutError("slow")) as urlopen:
            with self.assertRaises(ProviderError):
                self.pod._request("GET", "/v2/pods", retries=2)
        self.assertEqual(urlopen.call_count, 2)

    def test_no_api_key_is_refused_at_construction(self) -> None:
        with self.assertRaises(ProviderError):
            RunPod("")


class MethodTest(unittest.TestCase):
    """What each method asks the API for. `_request` is replaced; the payloads are asserted."""

    def setUp(self) -> None:
        self.pod = RunPod(KEY)
        self.calls: list[tuple] = []

    def stub(self, response):
        def fake(method, path, body=None, params=None, retries=3):
            self.calls.append((method, path, body, params))
            return response
        self.pod._request = fake  # type: ignore[method-assign]

    def test_get_returns_none_on_404(self) -> None:
        def fake(*a, **k):
            raise ProviderError("GET /v2/pods/x -> 404: gone")
        self.pod._request = fake  # type: ignore[method-assign]
        self.assertIsNone(self.pod.get("x"))

    def test_get_reraises_anything_that_is_not_404(self) -> None:
        def fake(*a, **k):
            raise ProviderError("GET /v2/pods/x -> 500: exploded")
        self.pod._request = fake  # type: ignore[method-assign]
        with self.assertRaises(ProviderError):
            self.pod.get("x")

    def test_create_sends_the_fields_runpod_requires(self) -> None:
        self.stub({"id": "new", "status": "RUNNING"})
        self.pod.create(gpu_id="NVIDIA GeForce RTX 4090", image="img", container_disk_gb=50,
                        cloud="SECURE", ports=["22/tcp", "8888/http"], name="workbench")
        method, path, body, _ = self.calls[0]
        self.assertEqual((method, path), ("POST", "/v2/pods"))
        self.assertEqual(body["name"], "workbench")
        self.assertEqual(body["disk"], 50)
        self.assertEqual(body["ports"], ["22/tcp", "8888/http"])
        self.assertTrue(body["startSsh"])
        self.assertEqual(body["gpu"]["id"], "NVIDIA GeForce RTX 4090")
        self.assertEqual(body["gpu"]["count"], 1)
        self.assertIn("minRamPerGpu", body["gpu"])
        self.assertIn("minVcpuCountPerGpu", body["gpu"])
        self.assertNotIn("mounts", body)

    def test_create_attaches_a_volume_only_when_one_is_named(self) -> None:
        self.stub({"id": "new"})
        self.pod.create(gpu_id="g", image="i", container_disk_gb=1, cloud="SECURE", ports=[],
                        name="n", volume_id="vol-1", volume_mount="/workspace")
        body = self.calls[0][2]
        self.assertEqual(body["mounts"]["network"][0]["volumeId"], "vol-1")
        self.assertEqual(body["mounts"]["network"][0]["path"], "/workspace")

    def test_create_passes_environment_only_when_given(self) -> None:
        self.stub({"id": "new"})
        self.pod.create(gpu_id="g", image="i", container_disk_gb=1, cloud="SECURE", ports=[],
                        name="n", env={"HF_TOKEN": "x"})
        self.assertEqual(self.calls[0][2]["env"], {"HF_TOKEN": "x"})

    def test_a_refused_create_propagates(self) -> None:
        # This is the real shape when a catalogue says yes and the API says no; `up` relies on it
        # raising so its fallback can try the next GPU.
        def fake(*a, **k):
            raise ProviderError('POST /v2/pods -> 400: {"detail":"There are no longer any instances '
                                'available with the requested specifications."}')
        self.pod._request = fake  # type: ignore[method-assign]
        with self.assertRaises(ProviderError) as caught:
            self.pod.create(gpu_id="g", image="i", container_disk_gb=1, cloud="SECURE", ports=[],
                            name="n")
        self.assertIn("no longer any instances", str(caught.exception))

    def test_each_action_is_sent_by_name(self) -> None:
        for name in ("start", "stop", "terminate"):
            with self.subTest(action=name):
                self.calls.clear()
                self.stub({})
                self.pod.action("pod-1", name)
                self.assertEqual(self.calls[0][0], "POST")
                self.assertEqual(self.calls[0][1], "/v2/pods/pod-1/action")
                self.assertEqual(self.calls[0][2], {"action": name})

    def test_an_invalid_action_is_refused_before_the_api(self) -> None:
        self.stub({})
        with self.assertRaises(AssertionError):
            self.pod.action("pod-1", "delete")
        self.assertEqual(self.calls, [])

    def test_list_pods_accepts_both_shapes(self) -> None:
        self.stub([{"id": "a"}])
        self.assertEqual(self.pod.list_pods(), [{"id": "a"}])
        self.stub({"pods": [{"id": "b"}]})
        self.assertEqual(self.pod.list_pods(), [{"id": "b"}])

    def test_catalog_returns_the_gpu_list(self) -> None:
        self.stub({"gpus": [{"id": "G"}]})
        self.assertEqual(self.pod.catalog(), [{"id": "G"}])

    def test_catalog_sends_the_availability_parameters(self) -> None:
        self.stub({"gpus": []})
        self.pod.catalog()
        self.assertEqual(self.calls[0][3], {"include": "AVAILABILITY", "product": "POD"})


class WaitRunningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pod = RunPod(KEY)
        patcher = mock.patch("rentguard.runpod.time.sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def pod_payload(status: str, uptime=None, ports=None) -> dict:
        return {"id": "p", "status": status,
                "runtime": {"uptime": uptime, "ports": ports, "gpus": [{"util": 0}]}}

    def test_returns_as_soon_as_the_machine_is_usable(self) -> None:
        alive = self.pod_payload("RUNNING", uptime=30, ports={"22": "tcp"})
        self.pod.get = lambda _id: alive  # type: ignore[method-assign]
        self.assertEqual(self.pod.wait_running("p"), alive)

    def test_a_crash_loop_never_becomes_usable(self) -> None:
        # RUNNING with uptime 0 and null ports is the shape that cost an hour: it must time out,
        # not be reported as working.
        crash = self.pod_payload("RUNNING", uptime=0, ports=None)
        self.pod.get = lambda _id: crash  # type: ignore[method-assign]
        with self.assertRaises(ProviderError) as caught:
            self.pod.wait_running("p", timeout_minutes=0)
        self.assertIn("not usable", str(caught.exception))

    def test_a_terminated_instance_raises_immediately(self) -> None:
        self.pod.get = lambda _id: self.pod_payload("TERMINATED")  # type: ignore[method-assign]
        with self.assertRaises(ProviderError) as caught:
            self.pod.wait_running("p")
        self.assertIn("TERMINATED", str(caught.exception))

    def test_an_error_instance_raises_immediately(self) -> None:
        self.pod.get = lambda _id: self.pod_payload("ERROR")  # type: ignore[method-assign]
        with self.assertRaises(ProviderError):
            self.pod.wait_running("p")

    def test_an_instance_that_does_not_exist_times_out(self) -> None:
        self.pod.get = lambda _id: None  # type: ignore[method-assign]
        with self.assertRaises(ProviderError):
            self.pod.wait_running("p", timeout_minutes=0)


class SshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pod = RunPod(KEY)

    def test_direct_ssh_gives_options_and_a_target(self) -> None:
        args, target = self.pod.ssh_argv(
            {"ssh": {"direct": {"host": "1.2.3.4", "port": 4444, "username": "root"}}}, None)
        self.assertIn("-p", args)
        self.assertIn("4444", args)
        self.assertEqual(target, "root@1.2.3.4")

    def test_a_key_is_passed_through(self) -> None:
        args, _ = self.pod.ssh_argv(
            {"ssh": {"direct": {"host": "h", "port": 1, "username": "u"}}}, Path("C:/key"))
        self.assertIn("-i", args)
        self.assertIn(str(Path("C:/key")), args)

    def test_proxy_access_has_no_port_flag(self) -> None:
        args, target = self.pod.ssh_argv(
            {"ssh": {"proxy": {"host": "p", "username": "u"}}}, None)
        self.assertNotIn("-p", args)
        self.assertEqual(target, "u@p")

    def test_an_instance_with_no_endpoint_raises(self) -> None:
        with self.assertRaises(ProviderError):
            self.pod.ssh_argv({"ssh": {}}, None)

    def test_the_old_spelling_still_works(self) -> None:
        # `ssh_args` was the pre-interface name; it points at the one implementation so an old
        # import cannot drift from the new one.
        self.assertIs(RunPod.ssh_args, RunPod.ssh_argv)

    def test_normalise_keeps_the_provider_ssh_block_nested(self) -> None:
        instance = {"id": "p", "status": "RUNNING", "ssh": {"direct": {"host": "h"}},
                    "dataCenterId": "US-IL-1"}
        normalised = self.pod.normalise(instance)
        self.assertEqual(normalised["machine"]["ssh"], {"direct": {"host": "h"}})
        self.assertEqual(normalised["machine"]["dataCenterId"], "US-IL-1")


if __name__ == "__main__":
    unittest.main()
