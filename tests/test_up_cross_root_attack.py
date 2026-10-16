"""Independent attack on defect 22's fix (`345960e`): another root's live machine does not block.

The author's three tests are `tests/test_up_another_root.py` (dry run, one name shape each). These
attack the same condition from the other side — a real rent rather than a dry run, every value
`instance_claim` can return, two roots sharing one configured name, a root that *configures*
another root's tagged name, a lost state file, a closed row, and the `--force` path.

What the change is, and what it is not, measured rather than argued:

- it is a **per-root** guardrail: `up` from this root now ignores a live machine whose name claims
  another root, so one sibling renting no longer stops every other root on the account;
- it is **not** account-wide: a machine whose name claims another root is skipped whether or not
  any ledger anywhere tracks it, so a stray carrying a foreign tag no longer blocks this root's
  rent. `test_a_foreign_named_stray_that_nobody_tracks_still_does_not_block` pins that literally.
- the refusal that remains is by *name*: this root's own name, or no name at all, still refuses —
  which is why a root that configures another root's tagged name refuses instead of skipping.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import Budget, Config  # noqa: E402
from rentguard.fake import FakeProvider  # noqa: E402
from rentguard.guard import (  # noqa: E402
    CLAIM_ANOTHER_ROOT,
    CLAIM_THIS_ROOT,
    CLAIM_UNCLAIMED,
)
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

OTHER_ROOT = r"C:\somewhere\else\entirely"
OTHER_TAG = hashlib.sha256(OTHER_ROOT.encode("utf-8")).hexdigest()[:8]


class NamedPod(FakeProvider):
    """The fake names a pod after its id; the name is the signal under test."""

    def __init__(self, *args, name: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.pod_name = name

    def _named(self, pod: dict) -> dict:
        return {**pod, "name": self.pod_name}

    def get(self, instance_id):
        pod = super().get(instance_id)
        return None if pod is None else self._named(pod)

    def list_pods(self):
        return [self._named(pod) for pod in super().list_pods()]


class UpAcrossRoots(unittest.TestCase):
    def setUp(self) -> None:
        # The root is pinned, never inherited: the classification is relative to *this* config's
        # root, and an ambient RENTGUARD_ROOT would make the test measure the shell that ran it.
        self.root = scratch_dir(self)
        env = mock.patch.dict(os.environ, {"RENTGUARD_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.ledger = Ledger(self.cfg.ledger_path)
        self.live_ids = lambda provider: [p["id"] for p in provider.list_pods()  # noqa: E731
                                          if p.get("status") == "RUNNING"]

    def up(self, provider: FakeProvider, *, dry_run: bool = False,
           force: bool = False) -> tuple[int, dict, str]:
        argv = ["up", "--purpose", "probe", "--json"]
        if dry_run:
            argv.append("--dry-run")
        if force:
            argv.append("--force")
        args = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda c: provider), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.cmd_up(args)
        text = out.getvalue()
        try:
            document = json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            document = {"_unparseable": text}
        return code, document, text + err.getvalue()

    # ------------------------------------------------------------------ the fix's own direction

    def test_a_sibling_roots_machine_does_not_block_a_real_rent(self) -> None:
        """Not a dry run: the machine is really requested, so the assertion is that a new one exists
        beside the sibling's, and that the sibling's was not touched."""
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-other", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider)

        self.assertEqual(code, 0, f"a sibling root's live machine blocked the rent:\n{text}")
        self.assertEqual(len(provider.created), 1,
                         f"no machine was created although nothing of this root's is live:\n{text}")
        self.assertNotIn(("pod-other", "stop"), provider.actions,
                         f"the other root's machine was touched:\n{text}")
        self.assertIn("pod-other", self.live_ids(provider),
                      "the sibling's machine disappeared from the account")

    def test_a_foreign_named_stray_that_nobody_tracks_still_does_not_block(self) -> None:
        """The boundary, pinned: the skip is by name, so it does not matter whether any ledger
        anywhere holds this machine. This is the account-wide protection the change is not."""
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-orphan", uptime_seconds=3600, status="RUNNING")   # no row anywhere

        code, document, text = self.up(provider)

        self.assertEqual(code, 0, f"the foreign-named stray blocked the rent:\n{text}")
        self.assertEqual(len(provider.created), 1, text)
        self.assertIn("pod-orphan", self.live_ids(provider),
                      "the stray vanished; the point is that it is still billing and untracked")
        self.assertTrue(all(r.get("instance_id") != "pod-orphan" for r in self.ledger.entries()),
                        f"the stray was recorded in this ledger: {self.ledger.entries()}")

    # ------------------------------------------------------------------ the conservative half

    def test_a_machine_carrying_this_roots_name_still_refuses(self) -> None:
        provider = NamedPod(name=cli._instance_name(self.cfg))
        provider.add("pod-maybe-ours", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 2, f"this root's own name did not refuse:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    def test_a_machine_with_no_name_at_all_still_refuses(self) -> None:
        provider = NamedPod(name="")
        provider.add("pod-anon", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 2, f"a nameless live machine did not refuse:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    def test_an_unclaimed_name_still_refuses(self) -> None:
        provider = NamedPod(name="workbench")   # the old fixed default: not a root's tag
        provider.add("pod-stray", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 2, f"an unclaimed live machine did not refuse:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    # ------------------------------------------------- two roots, one configured name (the control)

    def test_two_roots_sharing_one_configured_name_both_refuse(self) -> None:
        """`_instance_name` returns the configured name verbatim, so a shared explicit name makes
        the machine THIS root's to *both* roots and neither skips: doubt refuses, which is the safe
        direction. Probe directly, then through `up`."""
        other = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0),
                       state_dir=self.root / "other-root" / ".rentguard")
        other.runpod = {"name": "team-gpu"}
        self.cfg.runpod = {"name": "team-gpu"}
        pod = {"id": "pod-shared", "name": "team-gpu", "status": "RUNNING", "cost": 2.0,
               "runtime": {"uptime": 600, "ports": {"22": "tcp"}, "gpus": [{"util": 0.0}]}}

        self.assertEqual(cli.instance_claim(self.cfg, pod), CLAIM_THIS_ROOT)
        self.assertEqual(cli.instance_claim(other, pod), CLAIM_THIS_ROOT,
                         "a second root sharing the name did not claim the machine, so it would skip "
                         "a live machine it may be paying for")

        provider = NamedPod(name="team-gpu")
        provider.add("pod-shared", uptime_seconds=600, status="RUNNING")
        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 2, f"a shared configured name did not refuse:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    def test_a_root_that_configures_another_roots_tagged_name_refuses_not_skips(self) -> None:
        """A copy-pasted config: `runpod.name` set to another root's tag. `_instance_name` returns it
        verbatim, so it is THIS root's by its own configuration and must refuse — the machine is not
        skipped just because its name *pattern* says another root."""
        self.cfg.runpod = {"name": f"rentguard-{OTHER_TAG}"}
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-copied", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 2,
                         f"a copied config skipped a live machine it may be paying for:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    # ------------------------------------------------------------------ every claim value

    def test_every_name_shape_maps_to_one_of_the_three_claims(self) -> None:
        this_tag = cli._root_tag(cli.root_path())
        shapes = [
            (f"rentguard-{this_tag}", CLAIM_THIS_ROOT),
            (f"rentguard-{OTHER_TAG}", CLAIM_ANOTHER_ROOT),
            ("workbench", CLAIM_UNCLAIMED),
            ("", CLAIM_UNCLAIMED),
            ("rentguard-zzzzzzzz", CLAIM_UNCLAIMED),        # not hex: not a root's tag
            ("rentguard-1234567", CLAIM_UNCLAIMED),         # seven characters: not the shape
            ("my-rentguard-1234abcd", CLAIM_UNCLAIMED),     # tag must be the whole name
        ]
        for name, expected in shapes:
            with self.subTest(name=name):
                self.assertEqual(cli.instance_claim(self.cfg, {"name": name}), expected,
                                 f"{name!r} is classified {cli.instance_claim(self.cfg, {'name': name})!r}")

    # ------------------------------------------------------------------ paths around the check

    def test_a_closed_row_does_not_account_for_a_live_machine(self) -> None:
        """The 2026-10-15 hole stays closed: only the state file or an **open** row accounts for a
        machine. A closed row beside a live, this-root-named machine still refuses."""
        self.ledger.open(purpose="first", provider="fake", instance_id="pod-A", gpu_id="g",
                         rate_usd_per_hour=2.0)
        self.ledger.close(note="down", instance_status="RUNNING")
        provider = NamedPod(name=cli._instance_name(self.cfg))
        provider.add("pod-A", uptime_seconds=7200, status="RUNNING")

        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 2, f"a closed row accounted for a live machine:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    def test_a_lost_state_file_does_not_change_the_foreign_skip(self) -> None:
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-other", uptime_seconds=600, status="RUNNING")
        self.assertFalse(self.cfg.state_path.exists())

        code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 0, f"a lost state file turned a foreign machine into a refusal:\n{text}")

    def test_a_moved_project_still_accounts_for_its_own_live_machine(self) -> None:
        """The tag comes from the root *path*, so moving the project changes it. With the state file
        intact the old machine is still recorded here and is reused, not skipped — the ordinary
        move is safe."""
        old_tag_name = cli._instance_name(self.cfg)          # the pod was rented under this tag
        provider = NamedPod(name=old_tag_name)
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps({"instance_id": "pod-A"}), encoding="utf-8")
        moved = scratch_dir(self) / "moved-project"

        with mock.patch.dict(os.environ, {"RENTGUARD_ROOT": str(moved)}):
            self.assertNotEqual(cli._instance_name(self.cfg), old_tag_name,
                                "the tag did not change with the root, so this case is not tested")
            code, document, text = self.up(provider, dry_run=True)

        self.assertEqual(code, 0, f"the moved root refused over its own state's machine:\n{text}")
        self.assertEqual(provider.created, [],
                         f"the moved root rented a second machine beside its own:\n{text}")

    def test_a_moved_root_with_no_state_file_skips_its_own_machine(self) -> None:
        """The boundary of the fix, pinned so it cannot be mistaken for account-wide protection.

        Both conditions together: the root moved (so the machine's name carries the old tag and
        reads as another root's) **and** the state file is gone (so nothing here records it). The
        machine is then skipped and a second one is rented while the first bills. Either condition
        alone is safe — the state file accounts for it, and the tag is not read as foreign."""
        provider = NamedPod(name=f"rentguard-{cli._root_tag(cli.root_path())}")
        provider.add("pod-A", uptime_seconds=600, status="RUNNING")
        moved = scratch_dir(self) / "moved-project"
        self.assertFalse(self.cfg.state_path.exists())

        with mock.patch.dict(os.environ, {"RENTGUARD_ROOT": str(moved)}):
            code, document, text = self.up(provider)

        self.assertEqual(code, 0, f"the moved root refused:\n{text}")
        self.assertEqual(len(provider.created), 1,
                         f"the moved root did not rent beside its own unrecorded machine:\n{text}")
        self.assertIn("pod-A", self.live_ids(provider),
                      f"the first machine vanished; the point is that both are now billing:\n{text}")

    def test_force_still_rents_beside_an_unclaimed_machine(self) -> None:
        provider = NamedPod(name="workbench")
        provider.add("pod-stray", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider, force=True)

        self.assertEqual(code, 0, f"--force no longer overrides the unclaimed refusal:\n{text}")
        self.assertEqual(len(provider.created), 1, f"--force created nothing:\n{text}")


if __name__ == "__main__":
    unittest.main()
