#!/usr/bin/env python3
"""Line coverage for rentguard, using only the standard library.

Why not `coverage`: it is not installed here and pip has no network, and the number matters more than
the tooling. `sys.settrace` records the lines that ran; `trace._find_executable_linenos` says which
lines were executable. That is the same measurement, with no dependency to install.

    python tools/coverage.py               # measure, compare against the floor
    python tools/coverage.py --min 0.80    # fail below 80% instead of the recorded floor

The floor is a **ratchet**: it exists so the number cannot go backwards while we climb to the target
the user set, 94% of the code with 100% of the features exercised. Raise it whenever the measurement
beats it, in the same commit that earns the rise.

A line number is a floor, not a promise. It says code was *reached*, never that its behaviour was
checked: the guard defect that stopped a 32-second-old pod lives in lines that all executed. Feature
coverage is the half this script cannot see, and the half that catches that class of bug.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from trace import _find_executable_linenos

import pytest

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FLOOR = 0.98  # measured 98.4% on 2026-10-14 after the MCP-protocol tests; ratchet only upwards
TARGET = 0.94


def _executable_lines(path: Path) -> set[int]:
    """The lines this interpreter considers executable, without the entries that are not lines.

    Two artefacts of the line table, both measured on 2026-10-15 rather than assumed:

    - 3.13's ``dis.findlinestarts`` yields a ``None`` for an offset with no source line, which 3.12
      skips. One per file, denominator only: zero integer line numbers differ between the two
      versions, and on an unchanged hit count the rate moves 98.2% → 97.9% against the floor. A
      ``None`` can never be hit (``frame.f_lineno`` is an int), so counting it is a version-dependent
      penalty on every module, not a coverage result.
    - a ``0``, which neither interpreter can execute either: there is no line 0 of a Python file.

    Both are dropped here, with the reason beside the number they change, rather than folded into a
    lower floor. `tools/executable_lines.py` in the experiment's record printed the per-file diff
    that names them.
    """
    return {n for n in _find_executable_linenos(str(path)) if isinstance(n, int) and n > 0}


def measure(argv: list[str]) -> tuple[dict[str, tuple[int, int]], set[str]]:
    executed: dict[str, set[int]] = {}
    seen: set[str] = set()

    def tracer(frame, event, arg):
        if event == "line":
            filename = frame.f_code.co_filename
            path = Path(filename)
            if (path.suffix == ".py" and path.parent.name == REPO.name
                    and not path.name.startswith("__init__")):
                # a module body of ours, wherever it resolved from: this is what the empty-rows
                # diagnosis reports, because a checkout whose package resolved elsewhere
                # measures nothing rather than failing.
                seen.add(str(path.parent))
            if (path.suffix == ".py" and path.parent == REPO
                    and not path.name.startswith("__init__")):
                executed.setdefault(str(path), set()).add(frame.f_lineno)
        return tracer

    sys.settrace(tracer)
    # `sys.settrace` only reaches the calling thread. The guard's stop, `run`'s session reader and
    # the stop-bound worker all do their work on threads, and without this the lines they execute
    # count as uncovered — the measurement would be of the main thread, not of the program.
    threading.settrace(tracer)
    try:
        pytest.main(argv)
    finally:
        threading.settrace(None)
        sys.settrace(None)

    result: dict[str, tuple[int, int]] = {}
    for path in sorted(p for p in REPO.glob("*.py") if p.name != "__init__.py"):
        try:
            executable = _executable_lines(path)
        except Exception:  # noqa: BLE001 - an unreadable module must not hide the others
            continue
        hit = len(executable & executed.get(str(path), set()))
        result[path.name] = (hit, len(executable))

    total_hit = sum(hit for hit, _ in result.values())
    if result and total_hit == 0:
        # Every module present, nothing hit: that is never a legitimate result of running the
        # suite, it is a checkout whose package resolved somewhere the tracer was not watching.
        # Reporting it as 0.0% would let a hand-set floor turn a broken measurement into a pass.
        return {}, seen
    return result, seen


def _diagnose_unmeasured(seen: set[str]) -> None:
    print("no coverage measured — the tracer reached no module body of ours in this checkout.")
    print(f"  expected the package here: {REPO}")
    if seen:
        print("  the tracer did reach a package of this name, but elsewhere:")
        for where in sorted(seen):
            print(f"    {where}")
        print("  that is the checkout-placement trap: the tests resolve `rentguard` through the")
        print("  *parent* directory, so a worktree must be created as `<scratch>/rentguard`.")
    else:
        print("  the tracer reached no package of this name at all — did the suite import it?")
    print("  this is a measurement failure, not a coverage result.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min", type=float, default=DEFAULT_FLOOR,
                        help=f"fail below this fraction (ratchet; default {DEFAULT_FLOOR})")
    parser.add_argument("--target", type=float, default=TARGET,
                        help=f"the number being climbed towards (default {TARGET})")
    parser.add_argument("pytest_args", nargs="*", default=["tests", "-q", "-p", "no:cacheprovider"])
    args = parser.parse_args()

    rows, seen = measure(args.pytest_args or ["tests", "-q", "-p", "no:cacheprovider"])
    if not rows:
        _diagnose_unmeasured(seen)
        return 2

    print(f"\n{'module':<22}{'lines':>7}{'hit':>7}{'cover':>9}")
    total_lines = total_hit = 0
    for name, (hit, total) in rows.items():
        total_lines += total
        total_hit += hit
        pct = (100.0 * hit / total) if total else 100.0
        print(f"{name:<22}{total:>7}{hit:>7}{pct:>8.1f}%")
    overall = (total_hit / total_lines) if total_lines else 0.0
    print("-" * 45)
    print(f"{'TOTAL':<22}{total_lines:>7}{total_hit:>7}{overall * 100:>8.1f}%")
    print(f"floor {args.min * 100:.1f}%   target {args.target * 100:.1f}%")

    if overall < args.min:
        print(f"\nFAIL: {overall * 100:.1f}% is below the floor of {args.min * 100:.1f}%")
        return 1
    if overall < args.target:
        print(f"\nok — above the floor, {args.target * 100:.0f}% is still to come")
    else:
        print("\ntarget met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
