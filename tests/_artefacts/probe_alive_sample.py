"""Probe: what `up` really does with [alive, crash, crash, crash] from the transport.

Run:  python tests/_artefacts/probe_alive_sample.py

Not a test. It exists to show the exit code, the output, the transport calls and the ledger state for
the one-alive-sample sequence, because a failing assertion hides the rest.
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
from rentguard.ledger import Ledger  # noqa: E402
from rentguard.provider import ProviderError  # noqa: E402
from rentguard.runpod import RunPod  # noqa: E402
from rentguard.tests._scratch import scratch_dir  # noqa: E402

ALIVE = {"id": "pod-1", "status": "RUNNING", "cost": 1.0, "gpu": {"id": "Fake GPU 24GB"},
         "runtime": {"uptime": 180, "ports": {"22": "tcp"}, "gpus": [{"util": 0.0}]},
         "ssh": {"direct": {"host": "127.0.0.1", "port": 2222, "username": "root"}}}
CRASH = {"id": "pod-1", "status": "RUNNING", "cost": 1.0, "gpu": {"id": "Fake GPU 24GB"},
         "runtime": {"uptime": 0, "ports": None, "gpus": [{"util": 0.0}]},
         "ssh": {"direct": {"host": "127.0.0.1", "port": 2222, "username": "root"}}}


class Case:
    pass


case = unittest.TestCase("run")   # scratch_dir only needs addCleanup
case.root = scratch_dir(case)
cfg = Config(budget=Budget(cap_usd=25.0, per_run_cap_usd=5.0, idle_minutes=10.0),
             state_dir=case.root)
cfg.provider = "pod"
cfg.runpod = {"gpu_preference": ["Fake GPU 24GB"]}
ledger = Ledger(cfg.ledger_path)

state = {"gets": 0}
calls: list[tuple[str, str]] = []
samples = [ALIVE, CRASH, CRASH, CRASH]


def script(method, path, body=None, params=None, retries=3):
    calls.append((method, path))
    if path == "/v2/catalog/gpus":
        return {"gpus": [{"id": "Fake GPU 24GB", "memory": 24, "secure": "High",
                          "price": {"secure": 1.0}}]}
    if method == "POST" and path == "/v2/pods":
        return {"id": "pod-1", "status": "PROVISIONING", "cost": 1.0}
    if method == "POST" and path.endswith("/action"):
        return {}
    if method == "GET" and path == "/v2/pods/pod-1":
        if state["gets"] >= len(samples):
            raise ProviderError("GET /v2/pods/pod-1 -> 500: the machine disappeared")
        sample = samples[state["gets"]]
        state["gets"] += 1
        print(f"  sample {state['gets']}: uptime={sample['runtime']['uptime']} "
              f"ports={sample['runtime']['ports']}")
        return sample
    return {}


provider = RunPod("test-key-not-real")
provider._request = script

args = cli.build_parser().parse_args(["up", "--purpose", "probe"])
out = io.StringIO()
with mock.patch.object(cli, "_config", lambda: cfg), \
     mock.patch.object(cli, "_provider", lambda c: provider), \
     mock.patch.object(cli, "api_key", lambda *a, **k: "k"), \
     mock.patch("rentguard.runpod.time.sleep", lambda *_: None), \
     contextlib.redirect_stdout(out):
    try:
        code = cli.cmd_up(args)
    except SystemExit as exc:
        code = exc.code
    except ProviderError as exc:
        code = f"ProviderError: {exc}"

print(f"exit        : {code}")
print(f"calls       : {calls}")
print(f"ledger rows : {json.dumps(ledger.entries(), indent=2)}")
print(f"state       : {cfg.state_path.read_text(encoding='utf-8') if cfg.state_path.exists() else '(none)'}")
print("--- output ---")
print(out.getvalue())
