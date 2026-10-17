"""The two commands a pull's fake ssh answers, for tests that stand in for ``subprocess.Popen``.

Since 2026-10-17 the transport **measures before it transfers**: `pull` first runs an *inventory* on the
machine (a framed text answer: one run directory per pattern, with its size) and only then archives the
selected entries, with a bound sized from their volume. A fake ssh therefore gets called twice. One that
answers only the archive makes `pull` fail as `inventory-unreadable` — the safe direction, and not what
those tests are about — so they answer both, through this helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
if str(WS) not in sys.path:
    sys.path.insert(0, str(WS))

from rentguard.cli import PULL_INVENTORY_END, PULL_INVENTORY_HEADER, PULL_INVENTORY_MARKER


def is_inventory(argv) -> bool:
    """Whether this child was started for the inventory rather than for the archive."""
    return bool(argv) and str(argv[-1]).startswith(PULL_INVENTORY_MARKER)


def inventory_text(*entries: tuple[int, str, float, int, str]) -> str:
    """A framed inventory answer for ``(pattern index, kind, mtime, size, path)`` entries."""
    lines = [PULL_INVENTORY_HEADER]
    lines += [f"{index}\t{kind}\t{mtime:g}\t{size}\t{path}" for index, kind, mtime, size, path in entries]
    lines.append(PULL_INVENTORY_END)
    return "\n".join(lines) + "\n"


def file_entries(members: dict[str, bytes]) -> str:
    """An inventory for a file pattern: every member the fake archive will deliver, as a file entry.

    Files are not run-bounded (the pattern already names one run's outputs), so an inventory of exactly
    the archive's members is the faithful answer for those tests — and an empty archive gets an empty
    framed inventory, which the tool reports as ``patterns-matched-nothing`` before a byte moves.
    """
    return inventory_text(*[(0, "f", 100.0 + index, len(data), name)
                            for index, (name, data) in enumerate(members.items())])


def run_entries(names_and_sizes: list[tuple[str, int]]) -> str:
    """An inventory of run directories, oldest first, for a test about which run is carried."""
    return inventory_text(*[(0, "d", 100.0 + index, size, name)
                            for index, (name, size) in enumerate(names_and_sizes)])
