"""Scratch directories for the test suite: where they live, and how a run cleans up after itself.

**Where.** In order of preference:

1. ``RENTGUARD_SCRATCH``, when set — an explicit answer wins over any guess;
2. the platform temp dir (``$TEMP``/``$TMP``/``$TMPDIR``), **when a child process can actually write
   there**. That is what a stranger's machine gets, and it leaves nothing next to their clone;
3. a sibling of the checkout, ``<parent>/.rentguard-test-tmp`` — the fallback for this sandbox.

**How each run stays independent.** Every process gets one ``run-<pid>-<random>`` directory under
the chosen root. Directories left by earlier runs are removed at start, and this run's own directory
is removed at exit. So the suite is idempotent in one checkout: run it twice and the second run is
green for the same reason the first was, not because of what the first left behind.

**Measured, so nobody "tidiies" this away.** Under the DSH confinement a ``tempfile`` directory is
created mode 0700 and the ``chmod`` that would widen it is refused, so writes inside it fail with
``PermissionError``. That is a property of mode-0700 temp directories here, not of the platform temp
dir as a whole: ordinary directories created under the same temp dir work (which is what (2) probes
for before choosing it).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

WORKSPACE = Path(__file__).resolve().parents[2]
SIBLING_FALLBACK = WORKSPACE / ".rentguard-test-tmp"
ENV_VAR = "RENTGUARD_SCRATCH"
TEMP_SUBDIR = "rentguard-tests"
RUN_PREFIX = "run-"


def _platform_temp() -> Path:
    """The real platform temp dir.

    ``tempfile.gettempdir()`` is deliberately not used: ``conftest.py`` points it at this run's
    scratch so that any remaining ``tempfile`` user lands there too, and reading it back here would
    nest ``rentguard-tests`` inside itself and defeat the preference order below.
    """
    for var in ("TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    return Path(tempfile.gettempdir())


def is_writable(directory: Path) -> bool:
    """True when a file can be created, read back and removed there.

    A probe rather than a guess: the permission that matters is the one this process has, and on
    Windows an inherited deny or a package entry can refuse a directory that looks ordinary.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".rentguard-write-probe-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        ok = probe.read_text(encoding="utf-8") == "ok"
        probe.unlink()
        return ok
    except OSError:
        return False


def choose_root(*, env: Mapping[str, str] | None = None, temp_dir: Path | None = None,
                writable: Callable[[Path], bool] = is_writable) -> Path:
    """The scratch root for this run, by the preference order above.

    ``env``, ``temp_dir`` and ``writable`` are injectable so the order itself can be tested without
    changing this machine's environment.
    """
    source = os.environ if env is None else env
    override = (source.get(ENV_VAR) or "").strip()
    if override:
        return Path(override)
    platform_temp = Path(temp_dir if temp_dir is not None else _platform_temp()) / TEMP_SUBDIR
    if writable(platform_temp):
        return platform_temp
    return SIBLING_FALLBACK


def sweep_stale(base: Path, *, keep: Path | None = None) -> list[str]:
    """Remove run directories left by earlier runs. Returns the names removed.

    Best-effort by design: a directory that cannot be removed must not fail the run that is starting,
    and the next run will try again. Anything created in the last hour that is not ours is left
    alone, on the chance that a second run is in progress in the same checkout.
    """
    removed: list[str] = []
    cutoff = time.time() - 3600
    try:
        entries = list(base.glob(f"{RUN_PREFIX}*"))
    except OSError:
        return removed
    for entry in entries:
        if keep is not None and entry == keep:
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        except OSError:
            continue
    return removed


_base_cache: Path | None = None
_run_dir: Path | None = None


def scratch_root() -> Path:
    """The chosen base directory, resolved once and created."""
    global _base_cache
    if _base_cache is None:
        _base_cache = choose_root()
        _base_cache.mkdir(parents=True, exist_ok=True)
    return _base_cache


def forget_choice() -> None:
    """Drop the cached choice and the run directory. For tests of the chooser."""
    global _base_cache, _run_dir
    _base_cache = None
    _run_dir = None


def run_root() -> Path:
    """This process's own scratch directory, under the chosen root, removed at exit."""
    global _run_dir
    if _run_dir is None:
        base = scratch_root()
        candidate = base / f"{RUN_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
        candidate.mkdir(parents=True, exist_ok=True)
        sweep_stale(base, keep=candidate)
        _run_dir = candidate
        atexit.register(cleanup_run)
    return _run_dir


def cleanup_run() -> None:
    """Remove this run's directory. Safe to call more than once."""
    if _run_dir is not None:
        shutil.rmtree(_run_dir, ignore_errors=True)


def scratch_dir(case) -> Path:
    """A fresh directory under this run's scratch root, removed when ``case`` finishes."""
    path = run_root() / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
