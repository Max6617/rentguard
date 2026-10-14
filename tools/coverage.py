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
from pathlib import Path
from trace import _find_executable_linenos

import pytest

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FLOOR = 0.90  # measured 90.9% on 2026-10-14 after the config-path tests; ratchet only upwards
TARGET = 0.94


def measure(argv: list[str]) -> dict[str, tuple[int, int]]:
    executed: dict[str, set[int]] = {}

    def tracer(frame, event, arg):
        if event == "line":
            filename = frame.f_code.co_filename
            path = Path(filename)
            if (path.suffix == ".py" and path.parent == REPO
                    and not path.name.startswith("__init__")):
                executed.setdefault(str(path), set()).add(frame.f_lineno)
        return tracer

    sys.settrace(tracer)
    try:
        pytest.main(argv)
    finally:
        sys.settrace(None)

    result: dict[str, tuple[int, int]] = {}
    for path in sorted(p for p in REPO.glob("*.py") if p.name != "__init__.py"):
        try:
            executable = set(_find_executable_linenos(str(path)))
        except Exception:  # noqa: BLE001 - an unreadable module must not hide the others
            continue
        hit = len(executable & executed.get(str(path), set()))
        result[path.name] = (hit, len(executable))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min", type=float, default=DEFAULT_FLOOR,
                        help=f"fail below this fraction (ratchet; default {DEFAULT_FLOOR})")
    parser.add_argument("--target", type=float, default=TARGET,
                        help=f"the number being climbed towards (default {TARGET})")
    parser.add_argument("pytest_args", nargs="*", default=["tests", "-q", "-p", "no:cacheprovider"])
    args = parser.parse_args()

    rows = measure(args.pytest_args or ["tests", "-q", "-p", "no:cacheprovider"])
    if not rows:
        print("no modules measured — is the working directory the repository root?")
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
