"""Temporarily restore the pre-fix _pod() to show the drift tests red, then restore.

Run: python restore_buggy_pod.py revert   -> puts the buggy _pod back (tests must go red)
     python restore_buggy_pod.py restore  -> puts the fixed _pod back
"""
import io
import sys
from pathlib import Path

CLI = Path(r"C:\Users\besen\.dsh\bot\workspace\rentguard\cli.py")

FIXED = '''    instance_id = _read_state(cfg).get("instance_id")
    if not instance_id:
        return None
    pod = provider.get(instance_id)
    if pod is None or pod.get("status") == "TERMINATED":
        return None
    return pod'''

BUGGY = '''    state = _read_state(cfg)
    instance_id = state.get("instance_id")
    if not instance_id:
        return None
    pod = provider.get(instance_id)
    if pod is None or pod.get("status") == "TERMINATED":
        state.pop("instance_id", None)
        _write_state(cfg, state)
        return None
    return pod'''

mode = sys.argv[1]
src = io.open(CLI, encoding="utf-8").read()
if mode == "revert":
    assert FIXED in src, "fixed _pod not found"
    io.open(CLI, "w", encoding="utf-8", newline="").write(src.replace(FIXED, BUGGY, 1))
    print("pre-fix _pod restored")
elif mode == "restore":
    assert BUGGY in src, "buggy _pod not found"
    io.open(CLI, "w", encoding="utf-8", newline="").write(src.replace(BUGGY, FIXED, 1))
    print("fixed _pod restored")
else:
    raise SystemExit("usage: revert|restore")
