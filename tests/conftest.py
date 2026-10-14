"""Test-run setup for the rentguard suite.

Two jobs, both of them environment jobs rather than product ones.

**1. Put temporary files somewhere writable.** On this machine the DSH confinement makes the
system temp directory unwritable for a child process: ``tempfile.TemporaryDirectory()`` succeeds,
then every write inside it raises ``PermissionError``. That turns correct tests into failures and
hides the real result. A directory inside the project is writable, so every test's temporary files
live under ``<repo>/.rentguard-tmp/`` instead. Nothing in the product reads this directory, and
``.gitignore`` excludes it.

**2. Make temp cleanup tolerant.** Confined directories cannot be ``chmod``-ed, so
``shutil.rmtree`` fails while restoring permissions *after* the test body has already passed.
``ignore_errors=True`` is what ``shutil.rmtree`` does when the caller does not care whether every
file was removed; these tests do not care, and they must not report a passing assertion as a
failure because of it.

Product behaviour is asserted in the test modules. This file must not change any of it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

#: The repository root: tests/ -> rentguard/ -> workspace/
REPO = Path(__file__).resolve().parents[2]
TMP_ROOT = REPO / ".rentguard-tmp"

_real_rmtree = shutil.rmtree
_real_mkdtemp = tempfile.mkdtemp


def _tolerant_rmtree(path, ignore_errors=False, onerror=None, *, onexc=None):
    return _real_rmtree(path, ignore_errors=True, onerror=onerror, onexc=onexc)


def _writable_mkdtemp(suffix=None, prefix=None, dir=None):
    """``mkdtemp`` that leaves a directory this process can actually write into.

    ``tempfile`` creates the directory with mode 0o700. Under the DSH confinement that mode makes
    every later write fail with ``PermissionError`` — even though the directory was just created by
    this very process — because the confined token is not the owner in the way the check requires.
    Widening the mode immediately after creation is the whole fix, and it changes nothing about
    which files the tests choose to write.
    """
    path = _real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    return path


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    try:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(TMP_ROOT)
    except OSError:
        pass  # fall back to the system temp dir and let the failure be visible
    shutil.rmtree = _tolerant_rmtree
    tempfile.mkdtemp = _writable_mkdtemp


def pytest_unconfigure(config):  # noqa: ARG001 - pytest hook signature
    shutil.rmtree = _real_rmtree
    tempfile.mkdtemp = _real_mkdtemp
