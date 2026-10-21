"""Probe: the inventory command's outer word list, and the parser on the answer the fix produces.

Read-only. It prints the command the machine runs (the shell-free evidence: each configured pattern is
a quoted outer word, the inner expansion is unquoted) and parses the post-fix shape — one pattern
matching two directories, both lines carrying the pattern's index 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402

print("package:", cli.__file__)
print()

for patterns in (["grok/runs/*"], ["grok/runs/*", "logs/*"], ["results/step-[0-9]*"]):
    command = cli._pull_inventory_command("/workspace", patterns)
    start = command.index("for pattern in ") + len("for pattern in ")
    end = command.index("; do", start)
    print("patterns:", patterns)
    print("  outer words:", repr(command[start:end]))
    print("  inner expansion unquoted:", "for entry in $pattern" in command)
    print()

print("== the answer the fixed command produces: two matches, one pattern index ==")
text = "\n".join([cli.PULL_INVENTORY_HEADER,
                  "0\td\t900\t10000\tgrok/runs/run-16",
                  "0\td\t800\t9000\tgrok/runs/run-15",
                  cli.PULL_INVENTORY_END, ""])
entries = cli._parse_pull_inventory(text, ["grok/runs/*"])
for entry in entries:
    print(f"  {entry.path}  kind={entry.kind}  patterns={entry.patterns}")
selected, skipped, overlap = cli._pull_selection(entries, all_entries=False,
                                                 run_patterns=["grok/runs/*"])
print("  declared: selected", [e.path for e in selected], "skipped", [e.path for e in skipped])
