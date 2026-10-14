"""Offline probe: drive the real CLI against Forge's own FakeProvider.

No network, no spend: RENTGUARD_PROVIDER=fake uses rentguard/fake.py.

    python rentguard/tests/_artefacts/probe_cli_fake.py

Prints, for each command, the exit code or the traceback, plus the files it left behind.
This is evidence for the failure report; it changes nothing in the implementation.
"""

from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path

WS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WS))

ROOT = WS / ".assay-tmp" / "cli-fake-probe"
shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "rentguard.toml").write_text(
    'provider = "fake"\n\n'
    "[budget]\n"
    "cap_usd = 5.0\n"
    "per_run_cap_usd = 2.0\n"
    "max_hours_per_run = 6.0\n"
    "idle_minutes = 10\n\n"
    "[runpod]\n"
    'gpu_preference = ["Fake GPU 24GB"]\n',
    encoding="utf-8")

os.environ["RENTGUARD_ROOT"] = str(ROOT)
os.environ["RENTGUARD_PROVIDER"] = "fake"

from rentguard import cli  # noqa: E402

def run(*argv: str) -> None:
    print(f"\n$ rentguard {' '.join(argv)}")
    sys.stdout.flush()
    try:
        code = cli.main(list(argv))
        print(f"[exit {code}]")
    except SystemExit as exc:
        print(f"[SystemExit {exc.code}]")
    except BaseException:  # noqa: BLE001 - a traceback is the evidence
        traceback.print_exc(limit=6)


print(f"root: {ROOT}")
run("up", "--purpose", "m1-probe")
run("run", "--", "echo", "hi")
run("status")
run("doctor")
run("ledger")
run("down")

print("\nfiles left behind:")
for p in sorted(ROOT.rglob("*")):
    print(" ", p.relative_to(ROOT), "" if p.is_dir() else p.stat().st_size)
