"""`init` must write a config that the rest of the tool actually reads.

The failure this guards is silent and expensive: a key written under the wrong table header, or
under a name nothing reads, looks right in the file and does nothing at runtime. A volume id in a
generated config that is never attached is a rented machine with no storage; a `state_dir` parsed
into `[runpod]` is a ledger in the wrong place.

No network, no spend: the template is written into a scratch directory and loaded back.
"""

from __future__ import annotations

import sys
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rentguard import cli  # noqa: E402
from rentguard.config import load  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402


class InitWritesAConfigThatLoads(unittest.TestCase):
    def setUp(self) -> None:
        self.root = scratch_dir(self)
        self.target = self.root / "rentguard.toml"
        cli.cmd_init(Namespace(path=str(self.target), force=False, print=False, json=False))

    def test_the_generated_file_loads_without_error(self) -> None:
        cfg = load(root=self.root)
        self.assertIsNotNone(cfg.source)
        self.assertEqual(cfg.source, self.target)

    def test_top_level_keys_stay_top_level(self) -> None:
        """A key after a [table] header belongs to that table. `state_dir` and the ssh options are
        top-level, so they must appear before the first header or they are silently ignored."""
        cfg = load(root=self.root)

        self.assertNotIn("state_dir", cfg.runpod,
                         "state_dir landed inside [runpod] and is ignored by the tool")
        self.assertNotIn("ssh_binary", cfg.runpod, "ssh_binary landed inside [runpod]")
        self.assertNotIn("ssh_key", cfg.runpod, "ssh_key landed inside [runpod]")
        self.assertEqual(cfg.state_dir, self.root / ".rentguard",
                         "state_dir from the generated config was not applied")

    def test_the_volume_key_is_the_one_the_rent_path_reads(self) -> None:
        """`init` and `up` must agree on the name. The rent path reads ``volume_id``."""
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("volume_id", text, "the template does not write the key the rent path reads")
        self.assertNotIn("volume_name", text,
                         "the template writes volume_name, which no code reads")

        # And a value put there reaches the rent path.
        self.target.write_text(text.replace('volume_id = ""', 'volume_id = "vol-abc"'),
                               encoding="utf-8")
        cfg = load(root=self.root)
        self.assertEqual(cli._rp(cfg, "volume_id"), "vol-abc")

    def test_the_removed_on_run_switch_is_not_written(self) -> None:
        text = self.target.read_text(encoding="utf-8")
        self.assertNotIn("on_run", text,
                         "init writes on_run, which was deleted: it would be dead config")


if __name__ == "__main__":
    unittest.main()
