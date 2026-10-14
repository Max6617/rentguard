"""The provider contract, exercised through its own defaults.

`provider.py` is the interface every provider implements, so its uncovered lines are the defaults a
*second* provider inherits: the methods that must raise, the availability rules, the factory and the
registry. A provider module can override any of them, which is exactly why the base behaviour needs
its own tests — nothing else in the suite proves what a subclass gets for free.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.provider import (  # noqa: E402
    Provider,
    ProviderError,
    _offered,
    available,
    build,
    make_provider,
    provider_name,
    resolve,
)
from rentguard.runpod import RunPod  # noqa: E402


class Info:
    """Instances are plain dicts in the canonical shape."""

    @staticmethod
    def direct() -> dict:
        return {"ssh": {"direct": {"host": "h", "port": 22, "username": "u"}}}

    @staticmethod
    def proxy() -> dict:
        return {"ssh": {"proxy": {"host": "p", "port": 443, "username": "v"}}}

    @staticmethod
    def neither() -> dict:
        return {"ssh": {}}


class BaseDefaults(unittest.TestCase):
    """What a brand-new provider subclass inherits."""

    def setUp(self) -> None:
        self.base = Provider()

    def test_the_unimplemented_methods_raise_rather_than_pretend(self):
        cases = {
            "get": lambda: self.base.get("x"),
            "list_pods": lambda: self.base.list_pods(),
            "wait_running": lambda: self.base.wait_running("x"),
            "create": lambda: self.base.create(gpu_id="g", image="i", container_disk_gb=1,
                                               cloud="SECURE", ports=[], name="n"),
            "action": lambda: self.base.action("x", "stop"),
            "pull_artifacts": lambda: self.base.pull_artifacts({}, ["*"]),
        }
        for name, call in cases.items():
            with self.subTest(method=name):
                with self.assertRaises(NotImplementedError):
                    call()

    def test_catalog_is_empty_by_default(self):
        self.assertEqual(self.base.catalog(), [])

    def test_normalise_returns_a_copy(self):
        payload = {"id": "x", "status": "RUNNING"}
        out = self.base.normalise(payload)
        self.assertEqual(out, payload)
        self.assertIsNot(out, payload, "normalise returned the caller's own dict")

    def test_is_alive_needs_both_time_and_a_port(self):
        cases = [
            ({"uptime": 60, "ports": {"22": "tcp"}}, True),
            ({"uptime": 0, "ports": {"22": "tcp"}}, False),    # RUNNING with zero uptime: a claim
            ({"uptime": None, "ports": {"22": "tcp"}}, False),
            ({"uptime": 60, "ports": None}, False),            # no port: nothing to reach
            ({}, False),
        ]
        for runtime, expected in cases:
            with self.subTest(runtime=runtime):
                self.assertIs(self.base.is_alive({"runtime": runtime}), expected)

    def test_ssh_argv_prefers_direct_then_proxy_then_raises(self):
        argv, target = self.base.ssh_argv(Info.direct())
        self.assertEqual(argv, ["-p", "22"])
        self.assertEqual(target, "u@h")

        argv, target = self.base.ssh_argv(Info.proxy())
        self.assertEqual(argv, [])
        self.assertEqual(target, "v@p")

        with self.assertRaises(ProviderError):
            self.base.ssh_argv(Info.neither())


class Catalog(Provider):
    """A provider that only has a catalogue, so the selection defaults are the thing under test."""

    name = "catalog"
    needs_api_key = False

    def __init__(self, rows):
        super().__init__(None)
        self.rows = rows

    def catalog(self):
        return self.rows


ROWS = [
    {"id": "A", "memory": 24, "secure": True, "community": "High",
     "price": {"secure": 1.0, "community": 0.5}},
    {"id": "B", "memory": 48, "secure": False, "community": "None",
     "price": {"secure": None, "community": None}},
    {"id": "C", "memory": 80, "secure": "Medium", "community": "Low",
     "price": {"secure": 2.0, "community": 1.0}},
]


class Selection(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Catalog(ROWS)

    def test_candidates_skip_unknown_and_unoffered_rows(self):
        self.assertEqual(self.p.candidates(["A", "missing", "C"]), ["A", "C"])
        self.assertEqual(self.p.candidates(["C"], need_memory_gb=48), ["C"])
        with self.assertRaises(ProviderError):
            # A has 24 GiB and the need is 48, so nothing is left: an empty list is an error, not an
            # empty answer.
            self.p.candidates(["A"], need_memory_gb=48)

    def test_candidates_raise_when_nothing_is_offered(self):
        with self.assertRaises(ProviderError):
            self.p.candidates(["B"])           # secure False means not offered in SECURE
        with self.assertRaises(ProviderError):
            self.p.candidates(["nope"])

    def test_choose_gpu_takes_the_first_offered(self):
        self.assertEqual(self.p.choose_gpu(["missing", "C", "A"]), "C")

    def test_price_of_reads_the_tier(self):
        self.assertEqual(self.p.price_of("A", "community"), 0.5)
        self.assertIsNone(self.p.price_of("A", "nope"))
        self.assertIsNone(self.p.price_of("missing"))


class Offered(unittest.TestCase):
    def test_a_boolean_false_is_not_offered(self):
        self.assertFalse(_offered(False))
        self.assertTrue(_offered(True))

    def test_the_stock_strings(self):
        for value, expected in [("High", True), ("Medium", True), ("Low", True),
                                ("None", False), ("null", False), ("", False), ("  ", False),
                                (None, False)]:
            with self.subTest(value=value):
                self.assertIs(_offered(value), expected)


class Registry(unittest.TestCase):
    def test_available_names_the_providers(self):
        self.assertEqual(available(), ["fake", "runpod"])

    def test_resolve_finds_the_subclass_not_the_base(self):
        # Every provider module does `from .provider import Provider`, so an attribute lookup would
        # return the base class whose methods raise. This is the check that catches that.
        self.assertIs(resolve("runpod"), RunPod)
        self.assertIs(resolve("fake"), FakeProvider)
        self.assertTrue(issubclass(resolve("runpod"), Provider))

    def test_resolve_rejects_an_unknown_name(self):
        with self.assertRaises(ProviderError):
            resolve("vast")

    def test_build_refuses_a_keyed_provider_without_a_key(self):
        with self.assertRaises(ProviderError):
            build("runpod", api_key=None)

    def test_build_makes_a_keyless_provider(self):
        self.assertIsInstance(build("fake"), FakeProvider)

    def test_provider_name_prefers_the_environment(self):
        cfg = SimpleNamespace(provider="runpod")
        with mock.patch.dict(os.environ, {"RENTGUARD_PROVIDER": "fake"}):
            self.assertEqual(provider_name(cfg), "fake")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(provider_name(cfg), "runpod")

    def test_make_provider_uses_the_config(self):
        cfg = SimpleNamespace(provider="fake")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(make_provider(cfg), FakeProvider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
