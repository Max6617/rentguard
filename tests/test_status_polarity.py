"""Terminal is the only state that is not billing (Gilbert's ruling, 2026-10-16).

`LIVE_STATUSES` — RUNNING, PROVISIONING, STARTING — was read as the complement of "finished" in three
places, so every status outside both sets was invisible: `STOPPING` (a stop in flight, still charging),
`ERROR` (a pod that failed to start), and anything a provider adds later. The three places were `_live`
(which `up`'s untracked-instance refusal and `doctor`'s account summary read), `_pod`'s fallback to the
open row, and `guard.state_findings` (which `doctor` reports as drift).

The ruling: **terminal is the only state that is not billing; everything else is.** A false refusal is
loud, bounded and takes one word (`--force`) to override; a silent billing machine is neither. The cost
is accepted explicitly rather than discovered: an `ERROR` machine now blocks a rent, and a machine that
was stopped a moment ago blocks one for the seconds it spends `STOPPING`. The tests below pin both, and
`test_the_block_ends_when_the_provider_says_exited` measures that the second one ends on its own.

Measured costs this suite reports rather than hides:

- `ERROR` blocks `up` with exit 2 (`test_an_errored_machine_with_no_row_blocks_a_rent`) — for a pod
  that failed to start and may never have billed. `--force` is the escape the message names.
- A just-stopped machine blocks `up` for the length of the provider's transition: refused while it
  reports `STOPPING`, renting the moment it reports `EXITED`. At this provider's real stop latency
  (seconds) that is seconds of annoyance; it is not a permanent block, because `EXITED` is terminal.
  This is the sequence Gilbert asked about, measured rather than argued.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli, guard  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class PolarityCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                          state_dir=scratch_dir(self))
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.write_state({})

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.cfg.ledger_path)

    def run_cmd(self, func, argv: list[str], provider) -> tuple[int, str]:
        args = cli.build_parser().parse_args(argv)
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = func(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def up(self, provider, *extra: str) -> tuple[int, str]:
        return self.run_cmd(cli.cmd_up, ["up", "--purpose", "polarity", *extra], provider)

    def doctor(self, provider) -> tuple[int, str]:
        return self.run_cmd(cli.cmd_doctor, ["doctor"], provider)


class UpRefusesOverAnythingThatMayBill(PolarityCase):
    def test_a_stopping_machine_with_no_row_blocks_a_rent(self) -> None:
        """`STOPPING` is a stop in flight: the machine is still charging while it shuts down."""
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, status="STOPPING")

        code, out = self.up(provider)

        self.assertEqual(code, 2, f"up rented beside a machine that may still be billing:\n{out}")
        self.assertIn("pod-A", out, f"the machine it refused over is not named:\n{out}")
        self.assertIn("--force", out, f"the escape is not offered:\n{out}")
        self.assertEqual(provider.created, [], f"a machine was created anyway:\n{out}")

    def test_an_errored_machine_with_no_row_blocks_a_rent(self) -> None:
        """The cost of the ruling, stated: `ERROR` reads as billing now. A pod that failed to start
        may never have billed, and `up` still refuses — loudly, by name, with `--force` offered."""
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=0, status="ERROR")

        code, out = self.up(provider)

        self.assertEqual(code, 2, f"up rented beside an ERROR machine:\n{out}")
        self.assertIn("pod-A", out, f"the machine is not named:\n{out}")
        self.assertIn("--force", out, f"the escape is not offered:\n{out}")
        self.assertEqual(provider.created, [], f"a machine was created anyway:\n{out}")

    def test_force_rents_anyway(self) -> None:
        """The one word that overrides the refusal, as the message promises."""
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, status="STOPPING")

        code, out = self.up(provider, "--force")

        self.assertEqual(code, 0, f"--force did not rent:\n{out}")
        self.assertEqual(len(provider.created), 1, f"no machine was created under --force:\n{out}")

    def test_the_block_ends_when_the_provider_says_exited(self) -> None:
        """The close-then-rent sequence, measured: the same command, seconds apart.

        A machine stopped a moment ago sits in the provider's transitional state while the stop
        completes. `up` refuses then, and rents as soon as the provider reports `EXITED` — so the cost
        is the length of that transition, not a permanent block.
        """
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, status="STOPPING")

        refused, out_refused = self.up(provider)
        self.assertEqual(refused, 2, f"the transitional machine did not block the rent:\n{out_refused}")
        self.assertEqual(provider.created, [], f"a machine was created while pod-A was STOPPING:\n{out_refused}")

        provider.instances["pod-A"].status = "EXITED"        # the provider finishes the stop
        rented, out_rented = self.up(provider)

        self.assertEqual(rented, 0, f"up stayed blocked after the machine finished:\n{out_rented}")
        self.assertEqual(len(provider.created), 1, f"nothing was rented afterwards:\n{out_rented}")

    def test_a_stopping_machine_named_only_by_the_open_row_is_reused_not_doubled(self) -> None:
        """`_pod`'s fallback: the state file is gone, and the open row names the machine."""
        provider = FakeProvider(rate_usd_per_hour=2.0)
        provider.add("pod-A", uptime_seconds=600, status="STOPPING")
        run = self.ledger.open(purpose="polarity", provider="fake", instance_id="pod-A",
                               gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                               started_at=now_utc() - timedelta(hours=0.2))

        code, out = self.up(provider)

        self.assertEqual(provider.created, [],
                         f"up rented a second machine beside one the open row names:\n{out}")
        self.assertEqual(self.ledger.open_run().get("run_id"), run["run_id"],
                         f"the machine's own row was replaced:\n{out}")


class DoctorCountsAnythingThatMayBill(PolarityCase):
    """`doctor`'s "live" count means "may still be billing" — the terminal set's complement.

    The word is kept because it is the codebase's word for billing (`ledger.live_usd()`, the
    `live_instances` key, `live-instance-no-ledger-row`), and because every transcript in the docs was
    produced with it. What changed is *which statuses it counts*.
    """

    def test_a_stopping_machine_is_counted_and_reported(self) -> None:
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, status="STOPPING")

        code, out = self.doctor(provider)

        self.assertIn("1 total, 1 live", out,
                      f"a transitional machine was left out of the account summary:\n{out}")
        self.assertIn("live-instance-no-ledger-row", out,
                      f"a transitional machine with no row was not a finding:\n{out}")
        self.assertEqual(code, 1, f"doctor exited {code} over a machine that may bill:\n{out}")

    def test_an_errored_machine_is_counted_and_reported(self) -> None:
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=0, status="ERROR")

        code, out = self.doctor(provider)

        self.assertIn("1 total, 1 live", out, out)
        self.assertEqual(code, 1, f"doctor exited {code} over a machine that may bill:\n{out}")

    def test_a_finished_machine_is_not_counted(self) -> None:
        """The control: EXITED is the end state, and the summary still says nothing is billing."""
        provider = FakeProvider()
        provider.add("pod-A", uptime_seconds=600, status="EXITED")

        code, out = self.doctor(provider)

        self.assertIn("nothing is billing", out.lower(),
                      f"a finished machine was counted as billing:\n{out}")
        self.assertEqual(code, 0, f"doctor exited {code} over a finished machine:\n{out}")


class DriftSeesIt(PolarityCase):
    def _findings(self, status: str) -> list[guard.Finding]:
        return guard.state_findings({"instance_id": "pod-A"}, None,
                                    provider_gate=lambda i: {"id": "pod-A", "status": status})

    def test_a_stopping_machine_with_no_row_is_drift(self) -> None:
        findings = self._findings("STOPPING")
        self.assertEqual([f.code for f in findings], ["recorded-instance-no-open-run"], findings)
        self.assertEqual(findings[0].severity, "error", findings[0])

    def test_an_errored_machine_with_no_row_is_drift(self) -> None:
        findings = self._findings("ERROR")
        self.assertEqual([f.code for f in findings], ["recorded-instance-no-open-run"], findings)

    def test_a_finished_machine_with_no_row_is_not_drift(self) -> None:
        for status in ("EXITED", "TERMINATED"):
            with self.subTest(status=status):
                self.assertEqual(self._findings(status), [],
                                 f"{status} is the end state, not drift")

    def test_a_provider_that_cannot_answer_is_still_not_proof_of_drift(self) -> None:
        findings = guard.state_findings({"instance_id": "pod-A"}, None,
                                        provider_gate=lambda i: None)
        self.assertEqual(findings, [], "'could not ask' became 'billing'")

    def test_is_billing_is_the_terminal_set_complement(self) -> None:
        """A status the tool has never seen — or does not have at all — reads as billing.

        That is the whole ruling: the only state that is safely "finished" is one the provider names
        as finished. Everything else is a machine that might be charging.
        """
        for status in ("RUNNING", "PROVISIONING", "STARTING", "STOPPING", "ERROR", "WAT",
                       "unknown", "", None):
            with self.subTest(status=status):
                self.assertTrue(guard.is_billing(status), f"{status!r} was read as finished")
        for status in ("EXITED", "TERMINATED", "exited", "terminated"):
            with self.subTest(status=status):
                self.assertFalse(guard.is_billing(status), f"{status!r} was read as billing")


if __name__ == "__main__":
    unittest.main()
