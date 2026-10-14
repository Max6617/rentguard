"""Doctor exit code: clean, live-with-row, and live-with-no-row (the real drift)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

WS = Path(r"C:\Users\besen\.dsh\bot\workspace")
ROOT = WS / "rentguard" / ".verif4"
PY = sys.executable


def run(*args):
    env = dict(os.environ, RENTGUARD_PROVIDER="fake", RENTGUARD_ROOT=str(ROOT))
    p = subprocess.run([PY, "-m", "rentguard.cli", *args], capture_output=True,
                       text=True, cwd=str(WS), env=env)
    return p.returncode, p.stdout, p.stderr


def fresh():
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "rentguard.toml").write_text(
        'provider = "fake"\n[budget]\ncap_usd = 10.0\nper_run_cap_usd = 5.0\n', encoding="utf-8")


def findings(out):
    if not out.strip().startswith("{"):
        return None
    return [f["code"] for f in (json.loads(out).get("findings") or [])]


fresh()
rc, out, _ = run("doctor", "--json")
print(f"1. clean root, nothing rented       rc={rc}  findings={findings(out)}")

run("up", "--purpose", "span")
rc, out, _ = run("doctor", "--json")
print(f"2. live instance, row open          rc={rc}  findings={findings(out)}")

# Delete the ledger row, leaving a live billing instance unrecorded: the drift that hides money.
(ROOT / ".rentguard" / "runs.jsonl").write_text("", encoding="utf-8")
rc, out, err = run("doctor", "--json")
print(f"3. live instance, NO ledger row     rc={rc}  findings={findings(out)}")
print("   (must be non-zero with recorded-instance-no-open-run)")

(ROOT / ".rentguard" / "runs.jsonl").write_text("", encoding="utf-8")
rc, out, _ = run("doctor")
print(f"4. same, human output               rc={rc}  shows drift: "
      f"{'DRIFT' in out or 'no ledger row' in out or 'ERROR' in out}")
