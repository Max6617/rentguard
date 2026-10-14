"""Reproduce the two mechanisms that make a test run's scratch unusable, and name the paths.

Run:  python tests/_artefacts/probe_scratch_failure.py

Not a test. Three experiments, each printing the exact exception and the exact path, so the
mechanism is named from evidence rather than from a narrative:

A. a fresh ``tempfile.mkdtemp`` directory, then a write inside it  -> the mode-0700 mechanism;
B. the same under the old suite scratch root (``<workspace>/.rentguard-tmp``);
C. a stale entry where a directory is expected (a file planted at the scratch root) -> the
   stale-entry mechanism, and the exact path it breaks on.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO.parent
sys.path.insert(0, str(WORKSPACE))

from rentguard.tests import _scratch  # noqa: E402


def attempt(label: str, action) -> None:
    print(f"\n--- {label}")
    try:
        result = action()
        print(f"    OK: {result}")
    except Exception as exc:  # noqa: BLE001 - the exception is the result
        print(f"    {type(exc).__name__}: {exc}")
        for line in traceback.format_exc().splitlines()[-4:]:
            print(f"      {line}")


def write_inside(directory: Path) -> str:
    target = directory / "probe.txt"
    target.write_text("x", encoding="utf-8")
    size = target.stat().st_size
    target.unlink()
    return f"wrote and removed {target} ({size} byte)"


def a_fresh_mkdtemp() -> None:
    def action() -> str:
        directory = Path(tempfile.mkdtemp(prefix="probe-mkdtemp-"))
        mode = oct(os.stat(directory).st_mode & 0o777)
        try:
            return f"{directory} mode={mode}: " + write_inside(directory)
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)
    attempt("A. fresh mkdtemp directory under the platform temp dir", action)


def b_mkdtemp_under_the_old_root() -> None:
    old_root = WORKSPACE / ".rentguard-tmp"

    def action() -> str:
        old_root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="probe-", dir=str(old_root)))
        mode = oct(os.stat(directory).st_mode & 0o777)
        try:
            return f"{directory} mode={mode}: " + write_inside(directory)
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)
    attempt("B. fresh mkdtemp directory under the old suite root (<workspace>/.rentguard-tmp)", action)


def c_stale_entry_at_the_root() -> None:
    base = WORKSPACE / ".rentguard-test-tmp"
    base.write_text("a stale file where the suite wants a directory\n", encoding="utf-8")

    def action() -> str:
        return f"is_writable({base}) -> {_scratch.is_writable(base)}"

    attempt("C1. a stale FILE at the chosen scratch root: the writability probe", action)
    attempt("C2. a stale FILE at the chosen scratch root: creating the root",
            lambda: _scratch.scratch_root())
    base.unlink(missing_ok=True)


if __name__ == "__main__":
    print(f"platform temp: {_scratch._platform_temp()}")
    print(f"chosen root  : {_scratch.choose_root()}")
    a_fresh_mkdtemp()
    b_mkdtemp_under_the_old_root()
    c_stale_entry_at_the_root()
