"""`up` must not refuse to rent because *another root* has a machine running (2026-10-16).

Found in a proxy window: with a sibling root's pod live on the account (tracked and guarded in
*its* ledger), `up` from a second root answered

    error: 1 live instance(s) with no ledger row: <pod-id>. Renting again would start a second
    bill.

and exit 2. That machine is not this root's bill: another root's ledger records it and another root's
guard watches it. `doctor` has drawn that distinction since 2026-10-14 — a machine whose name belongs
to another root is a **warn** ("do not stop them from here"), not an error — and `up`'s refusal was the
same over-strictness one call over: with one sibling renting, no other root on the account could rent
at all.

The refusal stays for a live machine this root claims, or one that claims nobody: those are the
unrecorded bills of ours the check exists for.
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
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

OTHER_ROOT = r"C:\somewhere\else\entirely"
OTHER_TAG = hashlib.sha256(OTHER_ROOT.encode("utf-8")).hexdigest()[:8]


class NamedPod(FakeProvider):
    """The fake's payload names a pod after its id; the name is the signal under test here."""

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


class UpAndAnotherRoot(unittest.TestCase):
    def setUp(self) -> None:
        # The root is pinned in the environment rather than inherited from it: the classification
        # under test is relative to *this* config's root, and an ambient RENTGUARD_ROOT would make
        # the test measure the shell that started it.
        self.root = scratch_dir(self)
        env = mock.patch.dict(os.environ, {"RENTGUARD_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0, idle_minutes=0.0),
                          state_dir=self.root / ".rentguard")
        self.cfg.provider = "fake"
        self.cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
        self.ledger = Ledger(self.cfg.ledger_path)

    def up(self, provider: FakeProvider) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "probe", "--dry-run", "--json"])
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

    def test_a_live_machine_named_for_another_root_does_not_block_the_rent(self) -> None:
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-other", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider)

        self.assertNotEqual(document.get("reason"), "live-untracked-instance",
                            f"another root's machine blocked this root's rental:\n{text}")
        self.assertEqual(code, 0, f"up exited {code} over a sibling root's live machine:\n{text}")
        self.assertEqual(provider.created, [],
                         f"a dry run created a machine:\n{text}")

    def test_a_live_machine_that_claims_nobody_still_refuses(self) -> None:
        """The control, and the reason the check exists: an unclaimed live machine may be ours."""
        provider = NamedPod(name="workbench")
        provider.add("pod-stray", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider)

        self.assertEqual(code, 2, f"an unclaimed live machine did not block the rent:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)

    def test_a_live_machine_claiming_this_root_still_refuses(self) -> None:
        """Two roots can rent under one configured name, so "this root's name" is not proof that
        this root's ledger records it: the refusal keeps its conservative half."""
        provider = NamedPod(name=cli._instance_name(self.cfg))
        provider.add("pod-maybe-ours", uptime_seconds=600, status="RUNNING")

        code, document, text = self.up(provider)

        self.assertEqual(code, 2, f"a live machine carrying this root's name did not refuse:\n{text}")
        self.assertEqual(document.get("reason"), "live-untracked-instance", document)


if __name__ == "__main__":
    unittest.main()
