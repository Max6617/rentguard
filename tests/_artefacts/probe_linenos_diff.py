"""Dump trace._find_executable_linenos per file, for one interpreter.

    python dump_linenos.py <root> <out.json>

Walks every .py under <root> except build/, dist/, __pycache__/, .git/ and vendored scratch.
Source-only: nothing is imported and nothing is executed. A ``None`` key (3.13 yields one for an
offset with no source line; 3.12 skips those) is recorded separately, because it can never match a
tracer's integer ``f_lineno``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from trace import _find_executable_linenos

SKIP = {"build", "dist", "__pycache__", ".git", ".rentguard-test-tmp", "venv", ".venv"}


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2])
    data: dict[str, dict] = {"_python": sys.version, "_root": str(root)}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP for part in rel.parts):
            continue
        try:
            keys = list(_find_executable_linenos(str(path)))
            ints = sorted(k for k in keys if isinstance(k, int))
            data[rel.as_posix()] = {"lines": ints, "none_key": len(ints) != len(keys),
                                    "raw": len(keys), "error": None}
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            data[rel.as_posix()] = {"lines": [], "none_key": False, "raw": 0,
                                    "error": f"{type(exc).__name__}: {exc}"}
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    rows = [(k, v) for k, v in data.items() if not k.startswith("_")]
    print(f"{sys.version.split()[0]}: {len(rows)} files, "
          f"{sum(len(v['lines']) for _, v in rows)} int lines, "
          f"{sum(1 for _, v in rows if v['none_key'])} files with a None key -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
