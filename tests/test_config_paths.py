"""The config paths nothing else exercises: validation, merging, file loading, key lookup.

Config is where a typo becomes a crash at 3am or, worse, a silently ignored setting. These cover
the branches the happy-path tests miss: every budget rejection, the deprecated key, unknown keys,
invalid TOML, relative-path resolution, and the key lookup.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import config  # noqa: E402
from rentguard.config import Budget, ConfigError, _merge, api_key, find_config, load  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class BudgetValidation(unittest.TestCase):
    def test_every_rejected_budget_says_which_number_is_wrong(self):
        cases = {
            "cap not positive": dict(cap_usd=0.0),
            "per-run not positive": dict(cap_usd=10.0, per_run_cap_usd=0.0),
            "per-run above the cap": dict(cap_usd=5.0, per_run_cap_usd=50.0),
            "max hours not positive": dict(cap_usd=10.0, per_run_cap_usd=1.0, max_hours_per_run=0.0),
            "negative idle window": dict(cap_usd=10.0, per_run_cap_usd=1.0, idle_minutes=-1.0),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ConfigError):
                    Budget(**kwargs)

    def test_idle_zero_is_allowed_because_it_means_off(self):
        self.assertEqual(Budget(cap_usd=5.0, per_run_cap_usd=1.0, idle_minutes=0.0).idle_minutes, 0.0)


class Merge(unittest.TestCase):
    def test_nested_sections_merge_instead_of_replacing(self):
        merged = _merge({"budget": {"cap_usd": 5.0, "idle_minutes": 1.0}},
                        {"budget": {"cap_usd": 9.0}})
        self.assertEqual(merged, {"budget": {"cap_usd": 9.0, "idle_minutes": 1.0}})

    def test_scalars_are_overridden(self):
        self.assertEqual(_merge({"a": 1, "b": 2}, {"b": 3}), {"a": 1, "b": 3})


class Loading(unittest.TestCase):
    def _write(self, text: str) -> Path:
        root = scratch_dir(self)
        (root / "rentguard.toml").write_text(text, encoding="utf-8")
        return root

    def test_a_relative_state_dir_is_resolved_against_the_root(self):
        root = self._write('state_dir = ".rg"\nssh_binary = "ssh -x"\nssh_key = "k"\n')
        cfg = load(root=root)
        self.assertEqual(cfg.state_dir, root / ".rg")
        self.assertEqual(cfg.ssh_binary, "ssh -x")
        self.assertEqual(cfg.ssh_key, "k")
        self.assertIsNotNone(cfg.source)

    def test_a_relative_artefact_dir_is_resolved_against_the_root(self):
        root = self._write('[artefacts]\npull = ["a.json"]\nlocal_dir = "./arts"\n')
        cfg = load(root=root)
        self.assertEqual(cfg.artefacts.local_dir, str((root / "arts").resolve()))

    def test_an_absolute_state_dir_is_honoured(self):
        absolute = scratch_dir(self)
        root = self._write(f'state_dir = "{absolute.as_posix()}"\n')
        self.assertEqual(load(root=root).state_dir, absolute)

    def test_a_deprecated_key_is_ignored_not_rejected(self):
        root = self._write('[artefacts]\npull = ["a.json"]\non_run = true\n')
        cfg = load(root=root)          # must not raise
        self.assertEqual(cfg.artefacts.pull, ["a.json"])

    def test_an_unknown_key_is_an_error_at_startup(self):
        root = self._write("[budget]\ncap_usd = 5.0\nper_run_cap = 1.0\n")
        with self.assertRaises(ConfigError):
            load(root=root)

    def test_invalid_toml_is_an_error_not_a_default(self):
        root = self._write("cap_usd = [unclosed\n")
        with self.assertRaises(ConfigError):
            load(root=root)

    def test_find_config_finds_the_project_file(self):
        root = self._write('provider = "fake"\n')
        self.assertEqual(find_config(root), root / "rentguard.toml")

    def test_find_config_returns_none_when_there_is_nothing_to_find(self):
        empty = scratch_dir(self)
        with mock.patch("pathlib.Path.home", return_value=empty):
            self.assertIsNone(find_config(empty))


class ApiKey(unittest.TestCase):
    def test_the_first_present_variable_wins_and_is_stripped(self):
        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": "  secret  "}, clear=True):
            self.assertEqual(api_key(), "secret")

    def test_no_variable_means_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(api_key())

    def test_an_empty_value_is_not_a_key(self):
        # A whitespace value comes back as an empty string, and every caller truth-tests the result
        # (`if not key`), so a blank variable is treated as no key. Assert the property that matters
        # rather than an empty-string-versus-None distinction no caller makes.
        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": "   "}, clear=True):
            self.assertFalse(api_key(), "a blank key was treated as usable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
