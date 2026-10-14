"""Cross-root claims: a live pod with no row *here* is not the same as a pod nobody recorded.

The ledger is a local file, so a machine rented by one project root is invisible in another root's
ledger. Measured on 2026-10-14: pod ``2abx0hz2boe6ow`` was live at $0.18/h, recorded in
``grok/pod-timing/.rentguard/runs.jsonl`` (open row ``20261014T162601Z-4573d1``), and the workspace
root's ``doctor`` read it as ``recorded-instance-no-open-run`` — **exit 1**, the same shape as the
A6000 stray that burned $2.14. That was survivable while the only consequence was a warning. It is
not survivable now: the invariant for an unrecorded machine is "stop it", so a false positive there
kills a legitimate multi-hour run on another root's books.

The one cross-root signal on the account is the instance **name**: the default carries a tag derived
from the root, so two roots cannot share an account invisibly. What a name can and cannot prove is
the whole design here:

* a rentguard tag for a *different* root ⇒ another root rented it (that root's ledger owns it);
* this root's own configured name ⇒ the row may be in another root's ledger too (a sibling can copy
  a config), so absence of a row here is not proof that nobody recorded it;
* any other name ⇒ **unclaimed**, and only that is the hard error. ``rentguard-workbench`` — the
  A6000 stray, an old fixed default with no root tag — stays unclaimed on purpose; a rule that
  treated every name starting with "rentguard" as claimed would have let that stray through.
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

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

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

OTHER_ROOT = Path("C:/some/other/root")
OTHER_TAG = hashlib.sha256(str(OTHER_ROOT).encode("utf-8")).hexdigest()[:8]
PROJECT_NAME = "rentguard-m2"


class NamedPod(FakeProvider):
    """The fake's payload has no ``name``, and the name is the signal under test."""

    def __init__(self, *args, name: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.pod_name = name

    def _named(self, pod: dict) -> dict:
        return {**pod, "name": self.pod_name}

    def get(self, instance_id):
        pod = super().get(instance_id)
        return None if pod is None else self._named(pod)

    def create(self, **kwargs):
        # RunPod's create response carries the pod's name, and the name is what the stop path checks.
        return self._named(super().create(**kwargs))

    def list_pods(self):
        return [self._named(pod) for pod in super().list_pods()]


class ClaimCase(unittest.TestCase):
    """A scratch root, pinned in the environment rather than inherited from it."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(budget=Budget(cap_usd=10.0, per_run_cap_usd=5.0), state_dir=self.root)
        self.cfg.provider = "fake"
        env = mock.patch.dict(os.environ, {"RENTGUARD_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def write_state(self, state: dict) -> None:
        self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_path.write_text(json.dumps(state), encoding="utf-8")

    def invoke_doctor(self, provider) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["doctor", "--json"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                contextlib.redirect_stdout(out):
            try:
                code = cli.cmd_doctor(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        text = out.getvalue()
        return code, json.loads(text), text


class ClaimOfAName(unittest.TestCase):
    """What a name says about whose books hold the machine."""

    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.cfg = Config(state_dir=self.root)
        env = mock.patch.dict(os.environ, {"RENTGUARD_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.our_tag = cli._root_tag(cli.root_path())
        self.assertNotEqual(self.our_tag, OTHER_TAG, "the fixture's two roots hash alike")

    def test_this_projects_configured_name_claims_this_root(self):
        self.cfg.runpod = {"name": PROJECT_NAME}
        self.assertEqual(cli.instance_claim(self.cfg, {"name": PROJECT_NAME}), CLAIM_THIS_ROOT,
                         "a pod named as this root names its rentals was called a stranger's")

    def test_our_own_tag_claims_this_root(self):
        self.assertEqual(cli.instance_claim(self.cfg, {"name": f"rentguard-{self.our_tag}"}),
                         CLAIM_THIS_ROOT)

    def test_another_roots_tag_is_another_root(self):
        self.cfg.runpod = {"name": PROJECT_NAME}
        self.assertEqual(cli.instance_claim(self.cfg, {"name": f"rentguard-{OTHER_TAG}"}),
                         CLAIM_ANOTHER_ROOT,
                         "another root's tagged pod was not recognised as another root's")

    def test_a_name_no_project_claims_is_unclaimed(self):
        for name in ("small-agents-workbench", "rentguard-workbench", "", "training"):
            with self.subTest(name=name):
                self.assertEqual(cli.instance_claim(self.cfg, {"name": name}), CLAIM_UNCLAIMED,
                                 f"{name!r} was treated as a project's")

    def test_an_instance_with_no_name_at_all_is_unclaimed(self):
        self.assertEqual(cli.instance_claim(self.cfg, {}), CLAIM_UNCLAIMED)


class DoctorAcrossRoots(ClaimCase):
    """The finding and the exit code, per name."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.runpod = {"name": PROJECT_NAME}

    def codes(self, doc: dict) -> list[str]:
        return [finding["code"] for finding in doc["findings"]]

    def errors(self, doc: dict) -> list[dict]:
        return [finding for finding in doc["findings"] if finding["severity"] == "error"]

    def test_an_untracked_pod_named_for_another_root_is_not_a_hard_error(self):
        """Sequence: another root's tagged pod is live; this root's state names nothing; `doctor`.

        Effect: the pod is reported as another root's, and doctor exits 0. Before the fix it was
        ``live-instance-no-ledger-row`` and exit 1 — the same reading as a stray, on the strength of a
        ledger that simply cannot see the other root's file.
        """
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-B", uptime_seconds=600)

        code, doc, text = self.invoke_doctor(provider)

        self.assertEqual(code, 0, f"another root's tracked pod was a hard error.\n{text}")
        self.assertIn("tracked-by-another-root", self.codes(doc),
                      f"nothing said whose pod it is: {doc['findings']}")
        self.assertEqual(self.errors(doc), [], f"an error was raised for another root's pod: {doc}")

    def test_this_projects_name_with_no_row_here_is_a_warning_not_an_error(self):
        """Sequence: the live pod of 2026-10-14, as this root actually saw it — its own state names
        the machine, the workspace ledger has no row for it, and the row is open in another root.

        Effect: a warning that says the row may be another root's, and exit 0.
        """
        provider = NamedPod(name=PROJECT_NAME)
        provider.add("pod-A", uptime_seconds=600)
        self.write_state({"instance_id": "pod-A"})

        code, doc, text = self.invoke_doctor(provider)

        self.assertEqual(code, 0, f"a live pod another root records was a hard error.\n{text}")
        self.assertIn("row-not-in-this-root", self.codes(doc),
                      f"nothing said where the row might be: {doc['findings']}")
        self.assertEqual(self.errors(doc), [], doc)

    def test_an_untracked_pod_named_for_this_root_is_a_warning(self):
        """Sequence: the state file was lost, and the live pod carries this project's name.

        Effect: a warning, not a hard error: the name says the row may be in another root's ledger,
        and this root cannot see that file.
        """
        provider = NamedPod(name=PROJECT_NAME)
        provider.add("pod-C", uptime_seconds=600)

        code, doc, text = self.invoke_doctor(provider)

        self.assertEqual(code, 0, f"a pod named for this project was a hard error.\n{text}")
        self.assertIn("row-not-in-this-root", self.codes(doc), doc["findings"])

    def test_a_tracked_unclaimed_pod_is_still_a_hard_error(self):
        """Control: the name claims no project, so nothing anywhere owns it. Exit 1, as before."""
        provider = NamedPod(name="small-agents-workbench")
        provider.add("pod-A", uptime_seconds=600)
        self.write_state({"instance_id": "pod-A"})

        code, doc, text = self.invoke_doctor(provider)

        self.assertNotEqual(code, 0, f"an unclaimed live pod exited 0.\n{text}")
        self.assertIn("recorded-instance-no-open-run", self.codes(doc), doc["findings"])

    def test_an_untracked_unclaimed_pod_is_still_a_hard_error(self):
        """Control for the A6000 case: ``rentguard-workbench`` has no root tag and belongs to no
        project, which is exactly the stray that burned $2.14 in a night."""
        provider = NamedPod(name="rentguard-workbench")
        provider.add("pod-D", uptime_seconds=600)

        code, doc, text = self.invoke_doctor(provider)

        self.assertNotEqual(code, 0, f"the A6000 shape exited 0.\n{text}")
        self.assertIn("live-instance-no-ledger-row", self.codes(doc), doc["findings"])


class NoStopOnAForeignName(ClaimCase):
    """The stop path must not kill a machine whose books another root holds."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.runpod = {"name": PROJECT_NAME}
        self.ledger = Ledger(self.cfg.ledger_path)

    def up(self, provider) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["up", "--purpose", "claim"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                mock.patch.object(cli, "api_key", lambda *a, **k: "test-key"), \
                mock.patch.object(Ledger, "finalise", lambda self, run_id, **fields: None), \
                contextlib.redirect_stdout(out):
            try:
                code = cli.cmd_up(args)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue()

    def test_a_machine_named_for_another_root_is_not_stopped(self):
        """Sequence: the row this root opened is gone, and the machine's name belongs to another
        root. The defect-10 exit path stops the machine rather than keep it unrecorded.

        Effect: it is **not** stopped, and the output says whose it is. Stopping it would kill another
        root's job on the strength of this root's missing row.
        """
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")

        code, out = self.up(provider)

        self.assertNotEqual(code, 0, f"a rental with no row here was reported as success.\n{out}")
        self.assertNotIn(("fake-1", "stop"), provider.actions,
                         f"a machine named for another root was stopped.\n{out}")
        self.assertIn("another root", out.lower(), f"the reason was not stated.\n{out}")

    def test_a_machine_named_for_this_root_is_still_stopped(self):
        """Control: the same path, a name this project claims — the machine is stopped, because a
        machine we named and cannot record must not keep billing."""
        provider = NamedPod(name=PROJECT_NAME)

        code, out = self.up(provider)

        self.assertNotEqual(code, 0, f"a rental with no row here was reported as success.\n{out}")
        self.assertIn(("fake-1", "stop"), provider.actions,
                      f"a machine of ours was left billing with no row.\n{out}")


class StatusSaysWhatItKnows(ClaimCase):
    """`status`'s "no row" line is the same claim as doctor's, and has to be as careful with it."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.runpod = {"name": PROJECT_NAME}

    def invoke_status(self, provider) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["status"])
        out = io.StringIO()
        with mock.patch.object(cli, "_config", lambda: self.cfg), \
                mock.patch.object(cli, "_provider", lambda cfg: provider), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_status(args)
        return code, out.getvalue()

    def test_status_does_not_claim_nothing_records_another_roots_machine(self):
        """Sequence: a live pod named for another root, no row here, `status`.

        Effect: it says the row may be in another root's ledger rather than that nothing is recording
        the machine. The second sentence is what sent me looking for a stray that was not one.
        """
        provider = NamedPod(name=f"rentguard-{OTHER_TAG}")
        provider.add("pod-A", uptime_seconds=600)
        self.write_state({"instance_id": "pod-A"})

        code, out = self.invoke_status(provider)

        self.assertIn("another root's ledger", out, f"the claim was absolute.\n{out}")
        self.assertNotIn("with nothing recording it", out, out)

    def test_status_still_says_nothing_records_an_unclaimed_machine(self):
        """Control: an unclaimed machine keeps the plain sentence, because for it the sentence is
        true."""
        provider = NamedPod(name="small-agents-workbench")
        provider.add("pod-A", uptime_seconds=600)
        self.write_state({"instance_id": "pod-A"})

        code, out = self.invoke_status(provider)

        self.assertIn("with nothing recording it", out, f"an unrecorded machine was softened.\n{out}")


if __name__ == "__main__":
    unittest.main()
