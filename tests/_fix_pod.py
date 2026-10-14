"""Put the fixed _pod() back after the red demonstration."""
import io
from pathlib import Path

CLI = Path(r"C:\Users\besen\.dsh\bot\workspace\rentguard\cli.py")

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

FIXED = '''    instance_id = _read_state(cfg).get("instance_id")
    if not instance_id:
        return None
    pod = provider.get(instance_id)
    if pod is None or pod.get("status") == "TERMINATED":
        return None
    return pod'''

src = io.open(CLI, encoding="utf-8").read()
if FIXED in src and BUGGY not in src:
    print("already fixed")
else:
    assert BUGGY in src, "neither buggy nor fixed _pod found"
    io.open(CLI, "w", encoding="utf-8", newline="").write(src.replace(BUGGY, FIXED, 1))
    print("fixed _pod restored")
