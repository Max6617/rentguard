"""Probe: the three re-attack shapes at 339544f, printed without a rental.

Run from the checkout root's parent. It shows the PTY archive command, the member list tar receives,
the trailing-newline pattern the loader accepts, and the skipped summary's wording.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO.parent))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Config, ConfigError  # noqa: E402

print("package:", cli.__file__)
print()

print("== PTY archive command: the selected path, as tar receives it ==")
pty = cli._pull_remote_command("/workspace", ["grok/runs/run-16"], pty=True)
direct = cli._pull_remote_command("/workspace", ["grok/runs/run-16"], pty=False)
print("pty command   :", pty)
print("direct command:", direct)


def members_of(command: str) -> list[str]:
    start = command.index(" -- ") + 4
    try:
        end = command.index(";", start)
    except ValueError:
        end = len(command)
    return shlex.split(command[start:end])


print("pty members   :", members_of(pty))
print("direct members:", members_of(direct))
tar = shutil.which("tar")
if tar:
    run = HERE.parent / "_probe_tmp"
    try:
        run.mkdir(parents=True, exist_ok=True)
        (run / "grok" / "runs" / "run-16").mkdir(parents=True, exist_ok=True)
        (run / "grok" / "runs" / "run-16" / "out.json").write_text("{}")
        result = subprocess.run([tar, "czf", str(run / "pty.tgz"), "--", *members_of(pty)],
                                cwd=run, capture_output=True, text=True)
        print("tar on pty members: rc=", result.returncode, "stderr=",
              result.stderr.strip().splitlines()[:2])
        result = subprocess.run([tar, "czf", str(run / "direct.tgz"), "--",
                                 *members_of(direct)], cwd=run, capture_output=True, text=True)
        print("tar on direct     : rc=", result.returncode, "stderr=", result.stderr.strip())
    finally:
        shutil.rmtree(run, ignore_errors=True)
print()

print("== the pattern validation: a trailing newline ==")
for text, label in (("grok/runs/*", "plain"), ("grok/runs/*\n", "trailing newline"),
                    ("my results/*", "a space")):
    try:
        Artefacts(pull=[text])
        print(f"{label:>17}: accepted")
    except ConfigError as exc:
        print(f"{label:>17}: refused - {str(exc)[:48]}")
print("inventory command with the newline pattern:")
print(repr(cli._pull_inventory_command("/workspace", ["grok/runs/*\n"])))
print()

print("== the declaration: undeclared carries everything, declared trims ==")
cfg = Config()
cfg.artefacts.pull = ["grok/runs/run-16/*"]
entries = cli._parse_pull_inventory(
    "\n".join([cli.PULL_INVENTORY_HEADER,
               "0\td\t900\t552700000\tgrok/runs/run-16/cache",
               "0\td\t899\t172000000\tgrok/runs/run-16/checkpoints",
               cli.PULL_INVENTORY_END, ""]),
    cfg.artefacts.pull)
selected, skipped = cli._pull_selection(entries, all_entries=False)
print("undeclared selected:", [e.path for e in selected], "skipped:", [e.path for e in skipped])
selected, skipped = cli._pull_selection(entries, all_entries=False,
                                        run_patterns=["grok/runs/run-16/*"])
print("declared   selected:", [e.path for e in selected], "skipped:", [e.path for e in skipped])
print("(the note beside a declared skip says 'older run entr(y/ies)')")
print()

print("== the declaration validation ==")
try:
    Artefacts(pull=["grok/runs/*"], run_patterns=["checkpoints/*"])
    print("declaration outside pull: accepted")
except ConfigError:
    print("declaration outside pull: refused")
try:
    Artefacts(pull=["grok/runs/*\n"], run_patterns=["grok/runs/*\n"])
    print("declaration with a trailing newline: accepted")
except ConfigError:
    print("declaration with a trailing newline: refused")
print()

print("== the new bound and parser controls ==")
cfg = Config(artefacts=Artefacts(pull_timeout_s=0.0, pull_bytes_per_second=1_000_000.0))
print("zero floor, zero volume :", cli._pull_bound(cfg, 0))
print("max accepted volume     :", cli._pull_bound(cfg, 1 << 53))
for field, value in (("pull_timeout_s", float("nan")), ("pull_timeout_s", float("inf")),
                     ("pull_bytes_per_second", float("nan"))):
    try:
        Artefacts(**{field: value})
        print(f"{field}={value}: accepted")
    except ConfigError as exc:
        print(f"{field}={value}: refused")
print()

print("== the ledger cross-check (refusal names the row start and --all) ==")
print("_open_run_start_epoch needs a row; the command's emit carries")
print("reason='no-entry-since-the-run-started', run_started_at, newest_entry_at, selected, skipped.")
