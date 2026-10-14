"""Cross-invocation check of the fake provider: the two-command sequence M1 depends on.

Runs the real CLI in separate processes with RENTGUARD_PROVIDER=fake, so provider state has to
survive between commands the way it would for a person at a shell. No network, no spend.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

WS = Path(r"C:\Users\besen\.dsh\bot\workspace")
ROOT = WS / "rentguard" / ".verif3"
PY = sys.executable


def run(*args):
    env = dict(os.environ, RENTGUARD_PROVIDER="fake", RENTGUARD_ROOT=str(ROOT))
    p = subprocess.run([PY, "-m", "rentguard.cli", *args], capture_output=True,
                       text=True, cwd=str(WS), env=env)
    return p.returncode, p.stdout, p.stderr


def fresh():
    import shutil
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "rentguard.toml").write_text(
        'provider = "fake"\n[budget]\ncap_usd = 10.0\nper_run_cap_usd = 5.0\n',
        encoding="utf-8")


print("=== F-5: two commands, two processes, one state ===")
fresh()
rc, out, err = run("up", "--purpose", "span-1")
print(f"1. up            rc={rc}  {'instance ' + out.splitlines()[1].split()[2] if 'instance' in out else out.strip()[:60]}")
rc2, out2, err2 = run("status", "--json")
doc = json.loads(out2) if out2.strip().startswith("{") else {}
print(f"2. status        rc={rc2}  recorded_instance_id={doc.get('recorded_instance_id')!r}  "
      f"instance={(doc.get('instance') or {}).get('id')!r}")
rc3, out3, _ = run("up", "--purpose", "span-2")
print(f"3. up again      rc={rc3}  first line: {out3.splitlines()[0] if out3 else ''}")
before = len((ROOT / ".rentguard" / "runs.jsonl").read_text(encoding="utf-8").splitlines())
print(f"   ledger rows   : {before} (a second rent would make this 2)")

print()
print("=== doctor exit code: clean root, then drift ===")
fresh()
rc, out, err = run("doctor", "--json")
print(f"clean root       rc={rc}  (must be 0)")
(ROOT / ".rentguard").mkdir(parents=True, exist_ok=True)
(ROOT / ".rentguard" / "state.json").write_text(
    json.dumps({"instance_id": "gone-1", "provider": "fake"}), encoding="utf-8")
rc, out, err = run("doctor", "--json")
findings = [f["code"] for f in (json.loads(out).get("findings") or [])] if out.strip().startswith("{") else []
print(f"drift present    rc={rc}  findings={findings}")
