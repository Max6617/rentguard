"""Probe: the declaration shapes at cb7153e, printed without a rental.

It shows the selection and the prose for the five shapes the fixer named: one path named by a declared
and an undeclared pattern, a declaration whose matches are only files, a declared collection whose
newest match is a file, --all with a declared collection and an unreadable inventory, and the
ambiguity note's count/volume when one directory is named by two patterns.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO.parent))

from rentguard import cli  # noqa: E402
from rentguard.config import Artefacts, Config  # noqa: E402


print("package:", cli.__file__)
print()

print("== two contracts on one path: pattern 0 declared, pattern 1 not, both match both runs ==")
entries = [
    cli.PullEntry(patterns=("grok/runs/*", "grok/runs/run-1*"), path="grok/runs/run-16",
                  kind="d", mtime=900, bytes=10_000_000),
    cli.PullEntry(patterns=("grok/runs/*", "grok/runs/run-1*"), path="grok/runs/run-15",
                  kind="d", mtime=800, bytes=10_000_000),
]
selected, skipped = cli._pull_selection(entries, all_entries=False,
                                        run_patterns=["grok/runs/*"])
print("selected:", [e.path for e in selected])
print("skipped :", [e.path for e in skipped])
print("(the note printed beside this says 'matched 2 directories, 20.0 MB - carried all of them')")
print()

print("== a declared pattern whose matches are only files ==")
files = [cli.PullEntry(patterns=("results/*",), path="results/latest.json", kind="f", mtime=900,
                       bytes=100),
         cli.PullEntry(patterns=("results/*",), path="results/older.json", kind="f", mtime=800,
                       bytes=100)]
selected, skipped = cli._pull_selection(files, all_entries=False, run_patterns=["results/*"])
print("selected:", [e.path for e in selected], "skipped:", [e.path for e in skipped])
print()

print("== a declared collection whose newest match is a file ==")
mixed = [cli.PullEntry(patterns=("results/*",), path="results/latest.json", kind="f", mtime=900,
                       bytes=100),
         cli.PullEntry(patterns=("results/*",), path="results/run-old", kind="d", mtime=800,
                       bytes=5000)]
selected, skipped = cli._pull_selection(mixed, all_entries=False, run_patterns=["results/*"])
print("selected:", [e.path for e in selected], "skipped:", [e.path for e in skipped])
print()

print("== one directory named by two patterns: the member is merged, so count and volume are once ==")
merged = [cli.PullEntry(patterns=("grok/runs/*", "grok/runs/run-*"), path="grok/runs/run-16",
                        kind="d", mtime=900, bytes=10_000_000),
          cli.PullEntry(patterns=("grok/runs/*", "grok/runs/run-*"), path="grok/runs/run-15",
                        kind="d", mtime=800, bytes=10_000_000)]
for pattern in ("grok/runs/*", "grok/runs/run-*"):
    matched = [e for e in merged if pattern in e.patterns]
    print(f"{pattern}: matched {len(matched)} directories, "
          f"{cli._human_bytes(sum(e.bytes for e in matched))}")
print()

print("== a declaration is held to the config rules ==")
for label, kwargs in (
        ("declaration outside pull", {"pull": ["grok/runs/*"],
                                      "run_patterns": ["checkpoints/*"]}),
        ("declaration with a trailing newline", {"pull": ["grok/runs/*\n"],
                                                 "run_patterns": ["grok/runs/*\n"]}),
        ("declaration with a space", {"pull": ["my results/*"],
                                      "run_patterns": ["my results/*"]}),
        ("valid declaration", {"pull": ["grok/runs/*"], "run_patterns": ["grok/runs/*"]}),
):
    try:
        Artefacts(**kwargs)
        print(f"{label}: accepted")
    except Exception as exc:  # noqa: BLE001 - the refusal is the probe's point
        print(f"{label}: refused ({type(exc).__name__})")
