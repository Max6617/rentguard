"""Independent verification of `03dd52d`: a pull cannot hold a machine open, a stop cannot hang.

Written from outside the fix's own tests (`tests/test_stop_cannot_hang.py`), and with different
mechanisms on purpose, so a mistake shared between the implementation and its test cannot pass:

- the pull bound is exercised with a **real child process** that sleeps for 30 s (`_ssh_to_file`
  kills it), not by stubbing the bound function;
- the stop bound is exercised with a provider whose ordinary stop **blocks**, and the escalation is
  observed as a call to `force_stop`, not as a printed sentence;
- the bounds are read from a **rentguard.toml the user writes**, which is the question the fix's
  tests do not ask: is the bound configurable, or an attribute only a test can set?

Two cases the fix's own tests do not cover at all are here because they are money cases: a provider
stop that is **refused** must still close this root's ledger row, and the bounds must be reachable
through the config file rather than only through the dataclass.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli, config as config_module  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.ledger import Ledger, now_utc  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

#: A real child that hangs: what a stuck ssh route looks like from `_ssh_to_file`'s side.
HANG = [sys.executable, "-c", "import time; time.sleep(30)"]
PATTERN = "results/*.json"

TOML = """provider = "fake"
state_dir = ".rentguard"

[budget]
cap_usd = 5.0
per_run_cap_usd = 2.0
stop_timeout_s = 0.25

[artefacts]
pull = ["results/*.json"]
pull_timeout_s = 0.3

[runpod]
gpu_preference = ["Fake GPU 24GB"]
"""


def cli_args(*argv: str):
    return cli.build_parser().parse_args(list(argv))


class SshPullProvider(FakeProvider):
    """The fake, but artefacts come over ssh as RunPod's do, so the bounded ssh path is the one
    under test. It subclasses the canonical fake so it cannot drift from the provider interface."""

    def pull_artifacts(self, instance, patterns):
        raise NotImplementedError


class BoundCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=5.0, per_run_cap_usd=2.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.artefacts.pull = [PATTERN]
        self.cfg.artefacts.local_dir = str(self.root / "artefacts")
        self.ledger = Ledger(self.cfg.ledger_path)
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        self.ledger.open(purpose="bounds", provider="fake", instance_id="pod-A",
                         gpu_id="Fake GPU 24GB", rate_usd_per_hour=2.0,
                         started_at=now_utc())
        self.provider = SshPullProvider()
        self.provider.add("pod-A", uptime_seconds=1800, rate_usd_per_hour=2.0)

    def down(self) -> tuple[int, str, float]:
        out = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: self.provider), \
                mock.patch.object(cli, "_ssh_argv", lambda cfg, pod, provider=None: (HANG, "unused")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.cmd_down(cli_args("down"))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), time.monotonic() - started


class ThePullIsBounded(BoundCase):
    def test_a_pull_that_never_returns_still_stops_the_machine_and_names_the_lost_paths(self):
        """Sequence: `down` with artefacts configured and an ssh pull that hangs (a real child
        sleeping 30 s), `pull_timeout_s = 0.3`.

        Effect required: the child is killed at the bound, the machine is stopped anyway, the row is
        closed, the exit is non-zero, and the paths that were not pulled are named. Asserted on the
        clock as well: the 30 s child must not be waited for.
        """
        self.cfg.artefacts.pull_timeout_s = 0.3

        code, out, elapsed = self.down()

        self.assertLess(elapsed, 10.0,
                        f"`down` waited {elapsed:.1f}s on a bounded pull (bound 0.3s): the child was "
                        f"not killed.\n{out}")
        self.assertIn(("pod-A", "stop"), self.provider.actions,
                      f"the machine was left billing after a hung pull.\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"the row was left open.\n{out}")
        self.assertNotEqual(code, 0, f"a lost pull exited 0.\n{out}")
        self.assertIn(PATTERN, out, f"the paths that were lost are not named.\n{out}")
        self.assertIn("pull_timeout_s", out,
                      f"the failure does not say which bound was hit.\n{out}")

    def test_the_pull_bound_is_the_config_value_not_a_constant(self):
        """Effect: a 2 s bound and a 30 s child means `down` returns in about the bound, not in
        minutes and not in the 120 s default. This is what makes the number a user can set.
        """
        self.cfg.artefacts.pull_timeout_s = 2.0

        code, out, elapsed = self.down()

        self.assertLess(elapsed, 10.0, f"a 2 s bound did not bound anything: {elapsed:.1f}s.\n{out}")
        self.assertGreaterEqual(elapsed, 1.0,
                                f"the pull returned in {elapsed:.2f}s, before its 2 s bound: the "
                                f"bound is not what is being waited on.\n{out}")


class TheStopIsBounded(BoundCase):
    def test_a_stop_that_blocks_escalates_to_the_provider_endpoint(self):
        """Sequence: the ordinary stop never returns (a hung socket), `stop_timeout_s = 0.25`.

        Effect required: one call to `provider.force_stop` is made, `down` does not wait for the
        blocked call, and — since the escalation is what actually stops the machine here — the account
        then agrees, so the row closes and the command is clean. `stop_confirm_s` is small because a
        second bound now gates the close (defect 1, B19: a stop *call* is not a stopped machine).
        """
        self.cfg.budget.stop_timeout_s = 0.25
        self.cfg.budget.stop_confirm_s = 2.0
        self.cfg.artefacts.pull = []          # keep the pull out of this case
        calls: list[tuple[str, str]] = []

        def blocking_action(instance_id, action):
            calls.append(("action", action))
            time.sleep(30)

        def force_stop(instance_id):
            calls.append(("force_stop", instance_id))
            self.provider.instances[instance_id].status = "EXITED"

        self.provider.action = blocking_action          # type: ignore[method-assign]
        self.provider.force_stop = force_stop           # type: ignore[method-assign]

        code, out, elapsed = self.down()

        self.assertLess(elapsed, 10.0,
                        f"`down` waited {elapsed:.1f}s on a blocked stop (bound 0.25s).\n{out}")
        self.assertIn(("force_stop", "pod-A"), calls,
                      f"the provider's own stop endpoint was never called: {calls}\n{out}")
        self.assertIsNone(self.ledger.open_run(), f"the row was left open.\n{out}")
        self.assertEqual(code, 0, f"an escalated stop that worked was reported as a failure.\n{out}")

    def test_a_refused_stop_leaves_the_row_open_and_the_spend_visible(self):
        """Sequence: the provider refuses the stop (a 500 on the action call), the escalation is
        refused too, and the machine stays RUNNING.

        Effect required (Gilbert's ruling, 2026-10-16 — it supersedes the earlier "close the row
        anyway"): the row is **left open**, because a stop call the provider did not honour is not a
        stopped machine. An open row is what keeps the spend counted: it accrues, `doctor` prints it,
        and the cap-check and the guard both see it. The exit is non-zero and the refusal is named.
        """
        self.cfg.artefacts.pull = []
        self.cfg.budget.stop_timeout_s = 1.0
        self.cfg.budget.stop_confirm_s = 0.3
        self.provider.fail_actions = {"stop"}     # ordinary stop and the inherited force_stop

        code, out, elapsed = self.down()

        self.assertIsNotNone(self.ledger.open_run(),
                             "a refused stop closed the row while the machine was still RUNNING: the "
                             "ledger says the window is over and the machine is billing.\n" + out)
        self.assertNotEqual(code, 0, f"a refused stop exited 0.\n{out}")
        self.assertIn("could not stop", out.lower(),
                      f"the refusal is not reported.\n{out}")
        self.assertGreater(self.ledger.live_usd(), 0.0,
                           "the refused stop's spend stopped accruing: the row is not counting it")
        self.assertIn("NOT closed", out, f"the output does not say the row was left open.\n{out}")


class TheBoundsAreConfigurable(unittest.TestCase):
    def test_both_bounds_arrive_from_the_config_file_a_user_writes(self):
        """Sequence: a project writes `stop_timeout_s` and `pull_timeout_s` in rentguard.toml, the
        shape `rentguard init` documents.

        Effect required: `config.load` surfaces both values, so the bounds are the user's numbers
        and not constants only a test can reach. The defaults are asserted too, because a default
        that reads as "no bound" would make the whole protection optional.
        """
        root = scratch_dir(self)
        (root / "rentguard.toml").write_text(TOML, encoding="utf-8")

        cfg = config_module.load(root=root)

        self.assertEqual(cfg.budget.stop_timeout_s, 0.25, "stop_timeout_s did not reach the config")
        self.assertEqual(cfg.artefacts.pull_timeout_s, 0.3, "pull_timeout_s did not reach the config")
        self.assertGreater(Budget().stop_timeout_s, 0, "the default stop bound is not a bound")
        self.assertGreater(config_module.load(root=scratch_dir(self)).artefacts.pull_timeout_s, 0,
                           "the default pull bound is not a bound")


if __name__ == "__main__":
    unittest.main(verbosity=2)
