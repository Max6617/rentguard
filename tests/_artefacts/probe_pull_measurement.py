"""Probe: what the pull transport's measurement decides, printed without a rental.

Run from the checkout's parent (the package path is printed first). It exercises the pure functions
the pull is built on — the inventory command, the parser, the selection and the bound — with shapes a
real machine can present, and prints the command the remote shell would execute.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO.parent))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Config  # noqa: E402

print("package          :", cli.__file__)
print()

print("== the inventory command: configured patterns are interpolated unquoted ==")
print(cli._pull_inventory_command("/workspace", ["grok/runs/*", "results/my run/*"]))
print()

print("== the archive command: paths are quoted, but a leading '-' is not protected ==")
print(cli._pull_remote_command("/workspace", ["-cache", "grok/runs/run-16"], pty=False))
print()

print("== selection ==")


def entry(index, kind, mtime, size, path):
    return cli.PullEntry(pattern=["p"][index] if index == 0 else f"p{index}",
                         path=path, kind=kind, mtime=mtime, bytes=size)


def show(label, entries, all_entries=False):
    selected, skipped = cli._pull_selection(entries, all_entries=all_entries)
    print(f"{label}: selected={[e.path for e in selected]} skipped={[e.path for e in skipped]}")


show("equal mtimes run-9/run-10 ", [entry(0, "d", 300, 1, "grok/runs/run-9"),
                                   entry(0, "d", 300, 1, "grok/runs/run-10")])
show("nan mtime, first listed  ", [entry(0, "d", float("nan"), 1, "grok/runs/run-a-old"),
                                   entry(0, "d", 900, 1, "grok/runs/run-z-new")])
show("inf mtime                ", [entry(0, "d", float("inf"), 1, "grok/runs/run-boom"),
                                   entry(0, "d", 900, 1, "grok/runs/run-z-new")])
show("two directory patterns   ", [entry(0, "d", 900, 1, "grok/runs/run-16"),
                                   entry(1, "d", 899, 1, "checkpoints/step-100")])
show("same path twice          ", [entry(0, "d", 800, 1, "grok/runs/run-15"),
                                   entry(0, "d", 900, 1, "grok/runs/run-16"),
                                   entry(1, "d", 900, 1, "grok/runs/run-16")])
show("--all, mixed             ", [entry(0, "d", 800, 1, "grok/runs/run-15"),
                                   entry(0, "d", 900, 1, "grok/runs/run-16"),
                                   entry(1, "f", 900, 1, "results/final.json")],
     all_entries=True)
print()

print("== bound arithmetic ==")
for label, floor, rate, volume in (
        ("default, 700 MB      ", 120.0, 1_000_000.0, 700_000_000),
        ("floor 0, volume 0    ", 0.0, 1_000_000.0, 0),
        ("floor nan, volume 0  ", float("nan"), 1_000_000.0, 0),
        ("floor inf, volume 0  ", float("inf"), 1_000_000.0, 0),
        ("rate inf, 700 MB     ", 120.0, float("inf"), 700_000_000),
):
    cfg = Config()
    cfg.artefacts.pull_timeout_s = floor
    cfg.artefacts.pull_bytes_per_second = rate
    try:
        print(f"{label}: bound={cli._pull_bound(cfg, volume)!r}")
    except Exception as exc:  # noqa: BLE001 - the crash is the probe's point
        print(f"{label}: {type(exc).__name__}: {exc}")
print()

print("== readable but wrong inventory: a size the bound cannot convert ==")
text = "\n".join([cli.PULL_INVENTORY_HEADER,
                  "0\tf\t900\t" + "9" * 400 + "\tresults/huge.bin",
                  cli.PULL_INVENTORY_END, ""])
entries = cli._parse_pull_inventory(text, ["results/*"])
print("parsed bytes     :", entries[0].bytes, "(digits:", len(str(entries[0].bytes)), ")")
cfg = Config()
try:
    print("bound            :", cli._pull_bound(cfg, sum(e.bytes for e in entries)))
except Exception as exc:  # noqa: BLE001
    print("bound            :", type(exc).__name__, "-", exc)
print()

print("== nan/inf mtime parse ==")
for text in ("0\td\tnan\t1\tgrok/runs/x", "0\td\tinf\t1\tgrok/runs/x"):
    parsed = cli._parse_pull_inventory(
        "\n".join([cli.PULL_INVENTORY_HEADER, text, cli.PULL_INVENTORY_END, ""]),
        ["grok/runs/*"])
    print(f"{text!r} -> mtime={parsed[0].mtime!r}")
print()

print("== the mtime the inventory reads is the directory's own, not the tree's ==")
print("(illustration on this host; the same POSIX rule applies to the Linux container)")
import shutil  # noqa: E402
import time  # noqa: E402

probe = HERE.parent / "_mtime_probe"
try:
    run = probe / "runs" / "run-16"
    (run / "checkpoints").mkdir(parents=True)
    (run / "metrics.json").write_text("{}", encoding="utf-8")
    before = run.stat().st_mtime_ns
    time.sleep(1.1)
    (run / "checkpoints" / "step-100.pt").write_bytes(b"weights")
    after = run.stat().st_mtime_ns
    print(f"run dir mtime before a write into checkpoints/: {before}")
    print(f"run dir mtime after  a write into checkpoints/: {after}")
    print("advanced:", before != after)
finally:
    shutil.rmtree(probe, ignore_errors=True)
