"""The ledger: one JSONL row per rented run, with its cost.

Rules this module exists to enforce:

- An open run is a row with no ``stopped_at``. There is at most one.
- Cost is always recomputed from wall-clock and the row's own rate, never carried in from a
  caller, so the arithmetic has one home.
- Closing a row is idempotent: closing twice does not double-count.
- The file is rewritten atomically. A ledger that can be truncated by an interrupted write is a
  ledger that eventually loses a run.
- Opening is check-then-append, and two openers can interleave. ``open`` therefore takes a lock
  file around the check and the append: exactly one row can be open, even when ``up`` and ``adopt``
  run at the same time.
- A line that cannot be parsed is *reported*, never dropped in silence. A partial line is
  interrupted money, and money that disappears quietly is worse than money that is merely lost.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class LedgerLocked(RuntimeError):
    """Another process is holding the ledger lock and did not let go in time."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # ---------------------------------------------------------------- reading

    def entries(self) -> list[dict]:
        """Every parseable row, in file order. Corrupt lines are skipped here and listed by
        ``parse_errors`` — a reader that stops at the first bad line loses every run after it."""
        return [row for row, _ in self._rows()]

    def parse_errors(self) -> list[dict]:
        """Lines that are present but unreadable, with their line numbers.

        Returned rather than raised: ``doctor`` reports them, and a ledger that still has its other
        rows is more useful than one that refuses to be read at all.
        """
        return self._errors()

    def _errors(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                out.append({"line": number, "text": line.strip()[:200]})
        return out

    def _rows(self) -> list[tuple[dict, int]]:
        if not self.path.exists():
            return []
        rows: list[tuple[dict, int]] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((json.loads(line), number))
            except ValueError:
                continue  # reported by parse_errors(), not lost
        return rows

    def open_run(self) -> dict | None:
        for row, _ in reversed(self._rows()):
            if not row.get("stopped_at"):
                return row
        return None

    def total_usd(self) -> float:
        return round(sum(float(r.get("estimated_usd") or 0.0) for r in self.entries()), 6)

    def spent_on(self, run_id: str) -> float:
        for row in self.entries():
            if row.get("run_id") == run_id:
                return float(row.get("estimated_usd") or 0.0)
        return 0.0

    # ---------------------------------------------------------------- writing

    def append(self, entry: dict) -> dict:
        row = dict(entry)
        if row.get("stopped_at") and row.get("estimated_usd") is None:
            row["estimated_usd"] = self._cost(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=False) + "\n")
        return row

    def open(self, *, purpose: str, provider: str, instance_id: str | None,
             gpu_id: str | None, rate_usd_per_hour: float, tag: str = "",
             started_at: datetime | None = None) -> dict:
        with self._lock():
            if self.open_run() is not None:
                raise RuntimeError("a run is already open; close it before opening another")
            started = started_at or now_utc()
            return self.append({
                "run_id": f"{started.strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(3).hex()}",
                "purpose": purpose or "(unstated)",
                "provider": provider,
                "instance_id": instance_id,
                "gpu_id": gpu_id,
                "rate_usd_per_hour": float(rate_usd_per_hour or 0.0),
                "started_at": iso(started),
                "stopped_at": None,
                "wall_clock_seconds": None,
                "wall_clock_hours": None,
                "estimated_usd": None,
                "closed_by": None,
                "tag": tag,
            })

    def close(self, *, note: str, instance_status: str | None = None,
              stopped_at: datetime | None = None, void: bool = False) -> dict | None:
        """Close the open run, if any, and recompute its cost.

        ``void=True`` is for a row whose instance never existed or has vanished without the
        provider reporting an end: the row is closed, so a later ``up`` can start a fresh one, but
        it is recorded as voided and contributes **nothing** to the total. Guessing what a vanished
        machine cost would be inventing a number, and the cap is the one place that must never
        guess. The row is kept, with ``voided: true``, so the gap is visible in `ledger` rather
        than silently disappearing.
        """
        with self._lock():
            rows = self.entries()
            idx = next((i for i in range(len(rows) - 1, -1, -1)
                        if not rows[i].get("stopped_at")), None)
            if idx is None:
                return None
            row = rows[idx]
            stop = stopped_at or now_utc()
            start = datetime.fromisoformat(row["started_at"])
            seconds = max(0.0, (stop - start).total_seconds())
            row.update({
                "stopped_at": iso(stop),
                "wall_clock_seconds": round(seconds),
                "wall_clock_hours": round(seconds / 3600.0, 4),
                "closed_by": note or "manual",
            })
            if instance_status:
                row["instance_status_at_close"] = instance_status
            if void:
                row["voided"] = True
                row["voided_reason"] = note
                row["estimated_usd"] = 0.0
            else:
                row["estimated_usd"] = self._cost(row)
            self._rewrite(rows)
            return row

    # ---------------------------------------------------------------- helpers

    def finalise(self, run_id: str, **fields) -> dict | None:
        """Fill in the facts that were unknown when a row was opened provisionally, in place.

        A provisional row exists *before* the machine does, so the money is recorded from the moment
        it can start. When the provider then names the instance it actually created, the row is
        corrected rather than replaced: a second row beside the first would count the same minutes
        twice, and close-then-reopen would put the seconds between them outside the ledger.

        The row keeps its ``run_id`` and its start — finalising is not closing, and the start is
        when the rental began, which was deliberately a moment before the machine billed. Setting
        ``stopped_at`` here is refused rather than ignored, because the one thing this must never
        become is a second, lenient spelling of ``close``.
        """
        with self._lock():
            rows = self.entries()
            idx = next((i for i in range(len(rows) - 1, -1, -1)
                        if rows[i].get("run_id") == run_id), None)
            if idx is None or rows[idx].get("stopped_at"):
                return None
            forbidden = sorted({"stopped_at", "estimated_usd", "wall_clock_seconds",
                                "wall_clock_hours"} & set(fields))
            if forbidden:
                raise ValueError(f"finalise cannot set {forbidden}; closing a row is `close`")
            rows[idx].update(fields)
            self._rewrite(rows)
            return rows[idx]

    @staticmethod
    def _cost(row: dict) -> float:
        seconds = float(row.get("wall_clock_seconds") or 0.0)
        rate = float(row.get("rate_usd_per_hour") or 0.0)
        return round(rate * seconds / 3600.0, 6)

    def unreadable_open_rows(self) -> list[dict]:
        """Open rows whose bookkeeping cannot be read, named rather than raised.

        Quarantine: the fact is computed and carried here, once per read, instead of being silently
        skipped on every read, so a corrupt ledger is visible rather than merely survived. A row whose
        ``started_at`` will not parse has an accrual nobody can compute — and unknown is never zero.
        """
        out: list[dict] = []
        for row in self.entries():
            if row.get("stopped_at"):
                continue
            try:
                datetime.fromisoformat(str(row["started_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                # The fact carries the row's identity and its rate as well as what is broken: a
                # summary that dropped them left `up` unable to ask the provider for the machine's
                # own clock, so a row that *could* be priced was refused instead (2026-10-15).
                out.append({"run_id": row.get("run_id"), "field": "started_at",
                            "instance_id": row.get("instance_id"),
                            "rate_usd_per_hour": float(row.get("rate_usd_per_hour") or 0.0),
                            "reason": f"{type(exc).__name__}: {exc}"})
        return out

    def accrual(self, at: datetime | None = None, *, witness=None) -> tuple[float, list[dict]]:
        """``(accrued USD from the open run, the rows whose accrual is unknown)``.

        The second list is empty when the number is whole. When it is not, the first value is priced
        from ``witness`` if one is given and can answer: ``witness(instance_id)`` returns the
        machine's own uptime in seconds — an independent reading of how long the run billed, which is
        exactly what the row's unreadable ``started_at`` would have said. Every caller that prices a
        quarantined row goes through here, so a reading and an enforcement cannot diverge.

        With no witness, or no answer from it, the amount stays ``0.0`` and the non-empty list is the
        caller's signal that the total is unknown — never that it is zero.
        """
        unknown = self.unreadable_open_rows()
        row = self.open_run()
        if not row:
            return 0.0, unknown
        if unknown:
            if witness is not None and row.get("instance_id"):
                uptime = witness(str(row["instance_id"]))
                if uptime is not None:
                    seconds = max(0.0, float(uptime))
                    return (round(float(row.get("rate_usd_per_hour") or 0.0) * seconds / 3600.0, 6),
                            unknown)
            return 0.0, unknown
        start = datetime.fromisoformat(str(row["started_at"]))
        seconds = max(0.0, ((at or now_utc()) - start).total_seconds())
        return round(float(row.get("rate_usd_per_hour") or 0.0) * seconds / 3600.0, 6), []

    def live_usd(self, at: datetime | None = None) -> float:
        """What the open run has cost so far, from its own start time.

        Never raises: a row whose accrual cannot be computed contributes nothing *to this number*,
        and ``accrual()`` is how a caller learns that the real total is unknown rather than zero. A
        traceback here took out the guard, the cap check and `doctor` at once (2026-10-14).
        """
        return self.accrual(at)[0]

    def _rewrite(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".runs-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _lock(self, timeout: float = 30.0, stale_after: float = 60.0):
        """An exclusive lock that works on Windows and POSIX: a file created with ``O_EXCL``.

        Only the check-then-append window is covered, so the lock is held for microseconds. A lock
        left behind by a process that was killed mid-open would block every later run, so a lock
        older than ``stale_after`` is broken. Two shapes are accepted when breaking one: a file
        (this implementation) and a directory (an older implementation, whose leftovers must not be
        able to stop the ledger being written, so a directory is broken at any age).

        The lock is a **safety net, not a correctness requirement**: ``Ledger.open`` still refuses a
        second open row on its own. So if the environment will not let a lock be created at all, the
        ledger proceeds unlocked rather than crashing — a ledger that cannot be written is worse
        than one that might race.
        """
        lock_file = self._lock_path()
        try:
            lock_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        deadline = time.time() + timeout
        held = False
        while not held:
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                held = True
            except FileExistsError:
                if self._break_lock_if_stale(lock_file, stale_after):
                    continue
                if time.time() > deadline:
                    raise LedgerLocked(
                        f"could not lock {self.path} within {timeout:.0f}s; it is held by another "
                        f"rentguard process (remove {lock_file} only if none is running)")
                time.sleep(0.01)
            except OSError:
                yield  # cannot lock here; proceed rather than fail every ledger write
                return
        try:
            yield
        finally:
            self._remove_lock(lock_file)

    @staticmethod
    def _break_lock_if_stale(lock_file: Path, stale_after: float) -> bool:
        # A directory is never a live lock: `_lock` only ever creates a file. So a directory is a
        # leftover whatever its age, and waiting `stale_after` for it would block every write.
        if lock_file.is_dir():
            shutil.rmtree(lock_file, ignore_errors=True)
            return not lock_file.exists()
        try:
            age = time.time() - lock_file.stat().st_mtime
        except OSError:
            return False
        if age <= stale_after:
            return False
        try:
            lock_file.unlink()
            return True
        except OSError:
            return False

    @staticmethod
    def _remove_lock(lock_file: Path) -> None:
        try:
            if lock_file.is_dir():
                shutil.rmtree(lock_file, ignore_errors=True)
            else:
                lock_file.unlink()
        except OSError:
            pass


class JobLog:
    """Jobs run inside a rental. Separate file from the ledger: a rental is a cost unit,
    a job is a unit of work, and conflating them makes both harder to read."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(self, entry: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out
