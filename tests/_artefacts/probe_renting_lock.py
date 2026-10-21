"""Probe: the workspace root's decoy config refuses, and its ledger is untouched.

Read-only against the decoy: the refusal happens before a provider is constructed, so no rental and
no provider call is possible. The decoy's ledger is counted before and after. It also prints the
duplicate ``source`` field the lock's patch left in ``Config`` (a cosmetic clean-up).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
WS = REPO.parent
WORKSPACE = next(p for p in [REPO.parent, *REPO.parents] if (p / "rentguard.toml").is_file())  # the decoy root
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.config import load  # noqa: E402
from rentguard.ledger import Ledger  # noqa: E402

print("package:", cli.__file__)
print()

cfg = load(root=WORKSPACE)
print("decoy config       :", cfg.source)
print("allow_renting      :", cfg.allow_renting)
print("reason             :", cfg.renting_disabled_reason[:90] + "...")
print("state dir          :", cfg.state_dir)
print()

ledger = Ledger(cfg.ledger_path)
before_rows = len(ledger.entries())
before_total = ledger.total_usd()
print(f"decoy ledger before: {before_rows} rows, ${before_total:.4f}")
print()

provider = mock.Mock(side_effect=AssertionError("a provider was constructed"))
up_args = cli.build_parser().parse_args(["up", "--json", "--force", "--purpose", "probe"])
adopt_args = cli.build_parser().parse_args(
    ["adopt", "pod-A", "--uptime-hours", "2", "--json"])

for label, func, args in (("up --force", cli.cmd_up, up_args),
                          ("adopt --uptime-hours 2", cli.cmd_adopt, adopt_args)):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "_config", lambda: cfg), \
            mock.patch.object(cli, "_provider", provider), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = func(args)
    document = json.loads(out.getvalue() or "{}")
    print(f"{label}: exit {code}, action={document.get('action')!r}, "
          f"reason={document.get('reason')!r}, provider_called={provider.called}")
    print("  prose:", " | ".join(line.strip() for line in err.getvalue().splitlines()[:2]))
print()

after_rows = len(Ledger(cfg.ledger_path).entries())
after_total = Ledger(cfg.ledger_path).total_usd()
print(f"decoy ledger after : {after_rows} rows, ${after_total:.4f}")
print("unchanged          :", (before_rows, round(before_total, 4)) == (after_rows, round(after_total, 4)))
print()

source = (REPO / "config.py").read_text(encoding="utf-8")
print("duplicate `source` field annotations in Config:",
      sum(1 for line in source.splitlines() if line.strip() == "source: Path | None = None"))
