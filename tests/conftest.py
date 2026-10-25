"""Test-run setup for the rentguard suite.

**Measured caveat, do not tidy this away.** This file alone does not make ``tempfile`` usable here:
its ``os.chmod(path, 0o777)`` is refused by the sandbox, so a test reverted to
``tempfile.TemporaryDirectory`` still fails with ``PermissionError`` (three of them, measured). The
suite therefore relies on the workspace-local scratch helper in ``tests/_scratch.py``.

Two jobs, both of them environment jobs rather than product ones.

**1. Put temporary files somewhere writable.** On this machine the sandbox makes the
system temp directory unwritable for a child process: ``tempfile.TemporaryDirectory()`` succeeds,
then every write inside it raises ``PermissionError``. That turns correct tests into failures and
hides the real result. ``tests/_scratch.py`` owns the policy — ``RENTGUARD_SCRATCH``, else the
platform temp dir when it is genuinely writable, else a sibling of the checkout — and this hook
points ``tempfile`` at the same root so the two cannot disagree. Nothing in the product reads that
directory, and ``.gitignore`` excludes the sibling.

**2. Make temp cleanup tolerant.** Confined directories cannot be ``chmod``-ed, so
``shutil.rmtree`` fails while restoring permissions *after* the test body has already passed.
``ignore_errors=True`` is what ``shutil.rmtree`` does when the caller does not care whether every
file was removed; these tests do not care, and they must not report a passing assertion as a
failure because of it.

**3. Take the provider key away.** A real RunPod key lives in this environment, so a test that
forgets to select the fake provider silently queries the live account. That happened once: a
documentation pass meant to prove the "no key" path listed the real account instead (read-only, no
spend). No test needs a key — the fake provider is the point — so none is given one unless it asks:
``RENTGUARD_ALLOW_REAL_KEY=1``.

Product behaviour is asserted in the test modules. This file must not change any of it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scratch import cleanup_run, run_root  # noqa: E402  (scratch policy lives in one file)

#: Environment variables that carry a provider credential.
KEY_VARS = ("DSH_SECRET_RUNPOD_API_KEY", "RUNPOD_API_KEY")

#: The repository root: tests/ -> rentguard/ -> workspace/
REPO = Path(__file__).resolve().parents[2]

_real_rmtree = shutil.rmtree
_real_mkdtemp = tempfile.mkdtemp


def _tolerant_rmtree(path, ignore_errors=False, onerror=None, **kwargs):
    # `onexc` only exists from Python 3.12, so it is forwarded only when a caller passed it: naming
    # it at all, even as None, is a TypeError on 3.11 and failed every test's cleanup there.
    if onerror is not None:
        kwargs["onerror"] = onerror
    return _real_rmtree(path, ignore_errors=True, **kwargs)


def _writable_mkdtemp(suffix=None, prefix=None, dir=None):
    """``mkdtemp`` that leaves a directory this process can actually write into.

    ``tempfile`` creates the directory with mode 0o700. Under the sandbox that mode makes
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
        # The same policy the suite's own scratch helper uses: RENTGUARD_SCRATCH, else the platform
        # temp dir when it is writable, else the workspace sibling this sandbox needs. Pointing
        # `tempfile` at *this run's* directory means a test that still uses tempfile lands inside the
        # per-run scratch instead of somewhere a previous run can interfere with.
        tempfile.tempdir = str(run_root())
    except OSError:
        pass  # fall back to the system temp dir and let the failure be visible
    shutil.rmtree = _tolerant_rmtree
    tempfile.mkdtemp = _writable_mkdtemp


def pytest_unconfigure(config):  # noqa: ARG001 - pytest hook signature
    shutil.rmtree = _real_rmtree
    tempfile.mkdtemp = _real_mkdtemp
    cleanup_run()   # a run leaves no directory behind for the next one to trip over


@pytest.fixture(autouse=True)
def _no_live_provider_key(monkeypatch):
    """No test may reach the live account by accident.

    Run with ``RENTGUARD_ALLOW_REAL_KEY=1`` to opt out — for a deliberate live check, not for the
    suite. Every test that needs a provider builds the fake one.
    """
    if os.environ.get("RENTGUARD_ALLOW_REAL_KEY") == "1":
        return
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
