"""The usability gate **reads again before it refuses** (Gilbert's ruling, 2026-10-22).

Measured on the grid: at 00:01 on 2026-10-22 cell 3 (`batchB-lr3e-4-s2`, row
`20261021T215143Z-bdb358`, 2.16 h, $1.92) trained to its last step, and the cell's own `run` was
refused with *"reports RUNNING but is not usable (uptime=None, ports=NULL) … Not uploading to it"*.
The machine was usable: `down`'s pull landed the same 73 files seconds later. One reading of the
listing was treated as a verdict, and the window ended on the no-artefact rule — which is correct for
a lost artefact, and was the wrong answer here.

So the gate that guards the upload reads again: three readings, ~45 s apart, and it refuses only a
machine that is still unusable on the **last** one. The cost of waiting is seconds of a billed
machine; the cost of refusing on one stale listing is a window.
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

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


def reading(*, usable: bool, status: str = "RUNNING", port: int = 2200) -> dict:
    """One listing, as the provider hands it over: RUNNING is a claim, uptime and ports the evidence."""
    return {
        "id": "pod-1", "status": status, "name": "rg", "cost": 0.89,
        "runtime": {"uptime": 600.0 if usable else None,
                    "ports": ({"22": "tcp"} if usable else None),
                    "gpus": [{"util": 10.0}]},
        "ssh": {"direct": {"host": "127.0.0.1", "port": port, "username": "root"}},
    }


class ScriptedProvider(FakeProvider):
    """A fake whose `get` answers a script, then repeats its last answer.

    A resume is exactly this shape: the account's listing is a moment old, and the next read shows the
    machine the pull is about to use. Every call is counted, because "how many reads did the gate make"
    is the property under test.
    """

    def __init__(self, readings: list[dict]) -> None:
        super().__init__()
        self.script = list(readings)
        self.gets = 0

    def get(self, instance_id: str):
        self.gets += 1
        if not self.script:
            return None
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0), state_dir=self.root / ".rg")
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.cfg.artefacts.pull = ["results/*.json"]
        self.cfg.artefacts.local_dir = str(self.root / "artefacts")
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-1"}), encoding="utf-8")
        # No test here may wait: the interval and the sleep are seams, and both are pinned.
        self.sleeps: list[float] = []
        self.patch(cli, "USABLE_READ_INTERVAL_S", 0.0)
        self.patch(cli, "_USABLE_SLEEP", self.sleeps.append)

    def patch(self, obj, name: str, value) -> None:
        patcher = mock.patch.object(obj, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, func, argv: list[str], provider) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()


class TheGateReadsAgainBeforeRefusing(Case):
    def test_two_unusable_readings_then_a_usable_one_lets_the_pull_run(self) -> None:
        provider = ScriptedProvider([reading(usable=False), reading(usable=False),
                                     reading(usable=True)])
        provider.artifacts.put("pod-1", "results/out.json", b'{"ok":true}')

        code, out, err = self.invoke(cli.cmd_pull, ["pull"], provider)

        text = out + err
        self.assertEqual(code, 0, f"a machine that became usable was refused:\n{text}")
        self.assertTrue((Path(self.cfg.artefacts.local_dir) / "results" / "out.json").exists(),
                        f"the pull did not land the file:\n{text}")
        self.assertEqual(len(self.sleeps), 2,
                         f"the gate did not wait between its reads: sleeps={self.sleeps}\n{text}")

    def test_a_machine_that_stays_unusable_is_still_refused(self) -> None:
        provider = ScriptedProvider([reading(usable=False)])

        with self.assertRaises(SystemExit) as caught:
            self.invoke(cli.cmd_pull, ["pull"], provider)

        message = str(caught.exception)
        self.assertIn("not usable", message, message)
        self.assertIn("pod-1", message, message)
        self.assertIn("3", message, f"the refusal does not say how many reads it made: {message}")
        self.assertEqual(provider.gets, 3, f"the gate made {provider.gets} reads, not 3")
        self.assertEqual(len(self.sleeps), 2, self.sleeps)

    def test_a_usable_first_reading_costs_no_wait_and_no_extra_read(self) -> None:
        provider = ScriptedProvider([reading(usable=True)])

        code, out, err = self.invoke(cli.cmd_pull, ["pull"], provider)

        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.sleeps, [], f"a usable machine was made to wait: {self.sleeps}")
        self.assertEqual(provider.gets, 1, "the gate read the listing again for no reason")


class TheGateNamesWhatItSaw(Case):
    def test_a_later_read_says_so_and_names_the_evidence(self) -> None:
        provider = ScriptedProvider([reading(usable=False), reading(usable=True)])

        code, out, err = self.invoke(cli.cmd_pull, ["pull"], provider)

        text = out + err
        self.assertEqual(code, 0, text)
        self.assertIn("usable", text.lower(), text)
        self.assertIn("2 of", text, f"the gate does not say which read agreed:\n{text}")

    def test_a_provider_that_cannot_be_re_read_does_not_count_as_usable(self) -> None:
        class Flaky(FakeProvider):
            gets = 0

            def get(self, instance_id):
                Flaky.gets += 1
                if Flaky.gets == 1:
                    return reading(usable=False)
                raise ProviderError("the account did not answer")

        with self.assertRaises(SystemExit) as caught:
            self.invoke(cli.cmd_pull, ["pull"], Flaky())

        message = str(caught.exception)
        self.assertIn("not usable", message, message)
        self.assertIn("did not answer", message,
                      f"the refusal does not say the re-read failed: {message}")


class TheVerifiedReadingIsTheOneUsed(Case):
    def test_run_builds_its_ssh_from_the_reading_the_gate_verified(self) -> None:
        """A stale reading has no ports; the reading the gate accepts does, and ssh must be built from it."""
        provider = ScriptedProvider([reading(usable=False, port=2200),
                                     reading(usable=True, port=2222)])
        seen: list[dict] = []

        class Done:
            returncode = 0
            stdout = b"ok\n"
            stderr = b""

        with mock.patch.object(cli, "_ssh_argv",
                               lambda cfg, prov, pod: (seen.append(pod), (["ssh"], "t"))[1]), \
                mock.patch.object(cli, "_run_remote", lambda *a, **k: (0, "", "")):
            code, out, err = self.invoke(cli.cmd_run, ["run", "--", "echo", "hi"], provider)

        text = out + err
        self.assertEqual(code, 0, text)
        self.assertTrue(seen, f"no ssh was built:\n{text}")
        used = seen[-1]
        self.assertIsNotNone((used.get("runtime") or {}).get("ports"),
                             f"ssh was built from the stale reading, which has no ports: {used}")
        self.assertEqual((used.get("runtime") or {}).get("uptime"), 600.0, used)


if __name__ == "__main__":
    unittest.main(verbosity=2)
