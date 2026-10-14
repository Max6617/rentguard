"""Which executable lines never ran, module by module, with the source of each.

This is the same measurement as ``tools/coverage.py`` (``sys.settrace`` plus
``trace._find_executable_linenos``) with the one addition the classification needs: it prints the
lines that were **not** reached, so each can be called (a) reachable by a test, (b) dead code — which
belongs to whoever owns the module, not behind a `no cover` marker — or (c) reachable only with a
network or a real key.

    python tests/_artefacts/probe_missing_lines.py            # default: the whole suite
    python tests/_artefacts/probe_missing_lines.py tests/test_feature_matrix.py -q

Not a test. It writes ``tests/_artefacts/missing_lines_<date>.txt`` and prints a summary.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from trace import _find_executable_linenos

REPO = Path(__file__).resolve().parents[2]
ARTEFACTS = Path(__file__).resolve().parent

sys.path.insert(0, str(REPO.parent))

import pytest  # noqa: E402


def measure(argv: list[str]) -> dict[Path, set[int]]:
    executed: dict[Path, set[int]] = {}

    def tracer(frame, event, arg):
        if event == "line":
            path = Path(frame.f_code.co_filename)
            if path.suffix == ".py" and path.parent == REPO and not path.name.startswith("__init__"):
                executed.setdefault(path, set()).add(frame.f_lineno)
        return tracer

    sys.settrace(tracer)
    try:
        pytest.main(argv)
    finally:
        sys.settrace(None)
    return executed


def main() -> int:
    argv = sys.argv[1:] or ["tests", "-q", "--tb=no", "-p", "no:cacheprovider"]
    executed = measure(argv)

    report = [f"missing executable lines — {' '.join(argv)}", ""]
    totals = {"executable": 0, "hit": 0}
    for path in sorted(p for p in REPO.glob("*.py") if p.name != "__init__.py"):
        executable = set(_find_executable_linenos(str(path)))
        hit = executable & executed.get(path, set())
        missing = sorted(executable - hit)
        totals["executable"] += len(executable)
        totals["hit"] += len(hit)
        source = path.read_text(encoding="utf-8").splitlines()
        report.append(f"===== {path.name}  {len(hit)}/{len(executable)}  missing {len(missing)}")
        for number in missing:
            text = source[number - 1].strip() if number <= len(source) else ""
            report.append(f"  {number:5d}  {text[:110]}")
        report.append("")

    overall = 100.0 * totals["hit"] / totals["executable"]
    report.insert(1, f"TOTAL {totals['hit']}/{totals['executable']} = {overall:.1f}%")
    text = "\n".join(report)
    target = ARTEFACTS / f"missing_lines_{date.today().isoformat()}.txt"
    target.write_text(text + "\n", encoding="utf-8")
    print(text[:4000])
    print(f"\n... full report: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
