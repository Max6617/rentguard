"""A config can **refuse to rent**, and the refusal is loud, named, and spends nothing.

Why this exists (B17, 2026-10-20): the MCP tools resolve `./rentguard.toml` from the directory they are
started in, and this workspace's root carries one — a COMMUNITY / 3080 Ti route with a $0.10 cap and a
ledger nobody reads. Any rental through that door would have spent money under rules its operator never
read. Deleting the file was not the answer: its ledger holds real rows, and a config that can be started
from any directory has to be able to say "not this one, and here is where to go instead".

The shape: two top-level keys, `allow_renting = false` and `renting_disabled_reason = "…"`. The default
is `true`, so a stranger's fresh project rents exactly as before; the refusal is checked **before** a
provider is constructed or a row is opened, so it cannot spend a cent; and it is deliberately **not**
overridable by `--force`, because a money guardrail that a flag walks through is not a guardrail.

What must keep working: everything that does not spend. `doctor`, `ledger`, `status` and — the important
one — `down`, because a live machine must always be stoppable, whatever its config says about renting.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

from rentguard import cli, config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

REASON = "this is the workspace root: the project that owns the money is elsewhere"


class ConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)

    def write_config(self, body: str) -> Path:
        path = self.root / "rentguard.toml"
        path.write_text(body, encoding="utf-8")
        return path


class TheConfigCanDeclareItselfNonRenting(ConfigCase):
    def test_a_config_file_can_refuse_to_rent_and_say_why(self) -> None:
        self.write_config(f'allow_renting = false\nrenting_disabled_reason = "{REASON}"\n')

        cfg = config.load(root=self.root)

        self.assertFalse(cfg.allow_renting)
        self.assertEqual(cfg.renting_disabled_reason, REASON)

    def test_the_default_is_to_rent(self) -> None:
        """A stranger's config has never heard of this key, and must keep renting."""
        self.write_config('provider = "fake"\n')

        cfg = config.load(root=self.root)

        self.assertTrue(cfg.allow_renting)
        self.assertEqual(cfg.renting_disabled_reason, "")

    def test_a_config_that_does_not_exist_allows_renting(self) -> None:
        cfg = config.load(root=self.root)

        self.assertTrue(cfg.allow_renting)

    def test_a_non_boolean_is_refused_rather_than_read_as_true(self) -> None:
        self.write_config('allow_renting = "no"\n')

        with self.assertRaises(config.ConfigError) as caught:
            config.load(root=self.root)

        self.assertIn("allow_renting", str(caught.exception))

    def test_a_reason_that_is_not_a_string_is_refused(self) -> None:
        self.write_config('allow_renting = false\nrenting_disabled_reason = 17\n')

        with self.assertRaises(config.ConfigError) as caught:
            config.load(root=self.root)

        self.assertIn("renting_disabled_reason", str(caught.exception))

    def test_a_reason_is_trimmed(self) -> None:
        self.write_config('allow_renting = false\nrenting_disabled_reason = "  use the other root  "\n')

        self.assertEqual(config.load(root=self.root).renting_disabled_reason, "use the other root")


class RentingThroughADecoyConfigIsRefused(ConfigCase):
    """The money path: refused at the door, before a provider is built or a row is opened."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.write_config(
            f'provider = "fake"\nallow_renting = false\nrenting_disabled_reason = "{REASON}"\n'
            'state_dir = ".rentguard"\n')
        self.cfg = config.load(root=self.root)
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.fake = FakeProvider()

    def invoke(self, argv: list[str], *, provider=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider if provider is not None
                                  else self.fake), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_up_refuses_and_names_the_config_and_the_reason(self) -> None:
        code, out, err = self.invoke(["up", "--purpose", "decoy"])

        text = out + err
        self.assertEqual(code, 2, f"`up` through a non-renting config did not refuse:\n{text}")
        self.assertIn("allow_renting = false", text, text)
        self.assertIn(str(self.path), text, f"the refusal does not name the config it came from:\n{text}")
        self.assertIn(REASON, text, f"the refusal does not repeat the reason it was given:\n{text}")

    def test_the_refusal_says_force_does_not_override_it(self) -> None:
        code, out, err = self.invoke(["up", "--purpose", "decoy", "--force"])

        text = out + err
        self.assertEqual(code, 2, f"--force walked through a non-renting config:\n{text}")
        self.assertIn("--force", text, f"the refusal does not say --force will not help:\n{text}")

    def test_no_row_is_opened_and_nothing_is_rented(self) -> None:
        self.invoke(["up", "--purpose", "decoy"])

        self.assertIsNone(Ledger(self.cfg.ledger_path).open_run(),
                          "a refused rent still opened a ledger row")
        self.assertEqual(self.fake.created, [], "a refused rent still asked the provider to create")
        self.assertEqual(self.fake.actions, [], f"a refused rent still acted: {self.fake.actions}")

    def test_the_provider_is_not_even_constructed(self) -> None:
        """The check is at the door: a decoy config must not even build a provider to be refused."""
        with mock.patch.object(cli, "_provider",
                               side_effect=AssertionError("a provider was constructed for a refused rent")):
            code, out, err = self.invoke(["up", "--purpose", "decoy"])

        self.assertEqual(code, 2, out + err)
        self.assertIn(REASON, out + err)

    def test_adopt_refuses_too(self) -> None:
        """`adopt` is a money-moving action on a machine that is already billing: same door."""
        code, out, err = self.invoke(["adopt", "pod-1"])

        self.assertEqual(code, 2, f"`adopt` through a non-renting config did not refuse:\n{out}{err}")
        self.assertIn(REASON, out + err)
        self.assertIsNone(Ledger(self.cfg.ledger_path).open_run())

    def test_the_json_report_carries_a_named_refusal(self) -> None:
        code, out, err = self.invoke(["up", "--purpose", "decoy", "--json"])

        self.assertEqual(code, 2, err)
        document = json.loads(out)
        self.assertEqual(document.get("action"), "refused", document)
        self.assertEqual(document.get("reason"), "renting-disabled", document)
        self.assertEqual(document.get("detail"), REASON, document)

    def test_an_empty_reason_still_refuses_and_says_it_has_none(self) -> None:
        self.write_config('provider = "fake"\nallow_renting = false\n')
        self.cfg = config.load(root=self.root)
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}

        code, out, err = self.invoke(["up", "--purpose", "decoy"])

        self.assertEqual(code, 2, out + err)
        self.assertIn("no reason", out + err)


class EverythingThatDoesNotSpendStillWorks(ConfigCase):
    """`doctor`, `ledger`, `status` and `down` must not be gated by the renting flag.

    `down` is the one that matters most: a live machine has to be stoppable from any config, whatever
    that config says about renting — the alternative is a config change that leaves a pod billing.
    """

    def setUp(self) -> None:
        super().setUp()
        self.path = self.write_config(
            f'provider = "fake"\nallow_renting = false\nrenting_disabled_reason = "{REASON}"\n'
            'state_dir = ".rentguard"\n')
        self.cfg = config.load(root=self.root)
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.fake = FakeProvider()

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.fake), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_doctor_runs_and_reports_the_refusal_as_a_warning(self) -> None:
        code, out, err = self.invoke(["doctor"])
        text = out + err

        self.assertEqual(code, 0, f"`doctor` failed on a non-renting config:\n{text}")
        self.assertIn("renting-disabled", text, f"`doctor` does not report the refusal:\n{text}")
        self.assertIn(REASON, text, text)

    def test_ledger_and_status_still_run(self) -> None:
        self.assertEqual(self.invoke(["ledger"])[0], 0)
        self.assertEqual(self.invoke(["status"])[0], 0)

    def test_down_still_stops_a_live_machine(self) -> None:
        """The control that matters: the flag must never be able to keep a pod billing."""
        instance = self.fake.create(gpu_id="Fake GPU 24GB", image="img", container_disk_gb=20,
                                    cloud="COMMUNITY", ports=["22/tcp"], name="decoy-run")
        instance_id = instance["id"] if isinstance(instance, dict) else instance.id
        Ledger(self.cfg.ledger_path).open(purpose="control", provider="fake",
                                          instance_id=instance_id, gpu_id="Fake GPU 24GB",
                                          rate_usd_per_hour=0.13)
        (self.cfg.state_path).parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": instance_id}), encoding="utf-8")

        code, out, err = self.invoke(["down"])

        text = out + err
        self.assertEqual(code, 0, f"`down` was gated by the renting flag:\n{text}")
        self.assertIsNone(Ledger(self.cfg.ledger_path).open_run(),
                          f"`down` did not close the row:\n{text}")
        pod = self.fake.get(instance_id)
        status = (pod or {}).get("status")
        self.assertIn(status, ("EXITED", "TERMINATED"),
                      f"the machine is still up after `down`: {status}\n{text}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
