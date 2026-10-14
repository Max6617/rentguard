"""Scratch directories for the test suite, without ``tempfile``.

A ``tempfile.TemporaryDirectory`` is created mode 0700 under the system temp dir, and the confined
Windows token the suite runs under cannot then list, chmod or remove it. The directory itself is
fine; the cleanup is what fails, and it fails *after* the assertions, so a green test is reported as
an error. Tests here therefore make an ordinary directory under the workspace, which the sandbox can
both write and remove, and clean it up with ``ignore_errors``.

That is also why these tests do not leave anything behind: each helper call registers its own
cleanup with ``unittest``, and removal is best-effort.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
SCRATCH_ROOT = WORKSPACE / ".rentguard-test-tmp"


def scratch_dir(case) -> Path:
    """A fresh directory under the workspace, removed when ``case`` finishes."""
    path = SCRATCH_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
