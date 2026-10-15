"""Diff two dump_linenos.py outputs and print the source text of every differing line.

    python compare_linenos.py <a.json> <b.json> <root> [--only name1,name2,...]

A and B are interpreters. For each file: the int lines executable under A only and under B only,
and the source text of each, classified. The None key is reported separately, because a tracer can
never record it: it inflates the denominator without a possible hit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def source_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def classify(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "blank"
    if stripped.startswith("#"):
        return "comment"
    if stripped in {")", "]", "}", ",", ":", "):", "],", "},", "):", "]", "}", "else:", "try:",
                    "finally:"} or stripped.startswith((")", "]", "}", ",", ".")):
        return "continuation"
    if stripped.startswith(("except ", "elif ", "else:", "finally:", "try:")):
        return "compound-header"
    return "statement"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--only=")), None)
    a_path, b_path, root = Path(args[0]), Path(args[1]), Path(args[2])
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    va = a.get("_python", "?").split()[0]
    vb = b.get("_python", "?").split()[0]
    print(f"A = python {va}   B = python {vb}")
    print(f"root = {root}")

    files = sorted(f for f in set(a) | set(b) if not f.startswith("_"))
    if only:
        wanted = set(only.split(","))
        files = [f for f in files if f in wanted]

    total_only_a = total_only_b = 0
    differing = 0
    missing = []
    errors = []
    none_a = none_b = 0
    for rel in files:
        ea, eb = a.get(rel), b.get(rel)
        if ea is None or eb is None:
            missing.append(rel)
            continue
        if ea.get("error") or eb.get("error"):
            errors.append((rel, ea.get("error"), eb.get("error")))
            continue
        none_a += bool(ea.get("none_key"))
        none_b += bool(eb.get("none_key"))
        la, lb = set(ea["lines"]), set(eb["lines"])
        only_a, only_b = sorted(la - lb), sorted(lb - la)
        if not only_a and not only_b:
            continue
        differing += 1
        total_only_a += len(only_a)
        total_only_b += len(only_b)
        src = source_lines(root / rel)
        print(f"\n{rel}: A={len(la)} B={len(lb)}  only-A={len(only_a)} only-B={len(only_b)}"
              f"  A_none_key={bool(ea.get('none_key'))} B_none_key={bool(eb.get('none_key'))}")
        for tag, nums in (("only-A(3.12)", only_a), ("only-B(3.13)", only_b)):
            for n in nums:
                text = src[n - 1] if 0 < n <= len(src) else "<no such line>"
                print(f"   {tag} {rel}:{n}: [{classify(text):<15}] {text!r}")

    print("\n" + "=" * 78)
    print(f"files compared: {len(files)}  files differing: {differing}")
    print(f"int lines only in A(3.12): {total_only_a}   int lines only in B(3.13): {total_only_b}"
          f"   net (B-A): {total_only_b - total_only_a}")
    print(f"files with a None key: A={none_a}  B={none_b}   (a None key can never be hit)")
    if missing:
        print(f"PRESENT IN ONE DUMP ONLY: {missing}")
    if errors:
        print("ERRORS (disagreement other than attribution):")
        for rel, ea, eb in errors:
            print(f"   {rel}: A={ea!r} B={eb!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
