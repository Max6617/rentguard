"""One pytest run with a tracer active, in this process, so a caller can repeat it in fresh processes.

    python traced_run_once.py <pytest target...>

The gate's tracer is what changed the interleaving in the reported flake, so the check runs the
target the way `tools/coverage.py` does. One run per process: the suite's scratch handling is
per-process, and repeating `pytest.main` in one process removes the temp root between runs.
"""

from __future__ import annotations

import sys

import pytest


def tracer(frame, event, arg):
    return tracer


def main() -> int:
    sys.settrace(tracer)
    try:
        code = pytest.main([*sys.argv[1:], "-q", "-p", "no:cacheprovider"])
    finally:
        sys.settrace(None)
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
