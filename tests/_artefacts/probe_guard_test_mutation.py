"""Mutation probe: if the wait guard is never armed, does the crash-loop case still fail?

The fix at ``2ed04bf`` widened the crash-loop case's bound from 15 s to 30 s (two guard intervals).
A longer bound could in principle make the case vacuous. This probe runs the *same* test with
``_start_wait_guard`` patched to arm nothing and checks that it fails for the right reasons (no stop,
no heartbeat, the machine left billing) — i.e. the case still catches an unarmed guard.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
WS = REPO.parent
sys.path.insert(0, str(WS))

from rentguard import cli  # noqa: E402
from rentguard.tests.test_up_arms_the_guard import (  # noqa: E402
    ACrashLoopIsWatched,
)

print("package:", cli.__file__)
print()

CASE = "test_the_cap_is_enforced_while_up_waits_for_a_machine_that_never_works"
suite = unittest.TestSuite([ACrashLoopIsWatched(CASE)])
with mock.patch.object(cli, "_start_wait_guard", lambda *a, **k: None), \
        mock.patch.object(cli, "_release_wait_guard", lambda *a, **k: None):
    result = unittest.TextTestRunner(verbosity=2).run(suite)

print()
print("with no guard armed: failures =", len(result.failures), "errors =", len(result.errors))
for test, trace in result.failures + result.errors:
    print("---", test)
    for line in trace.splitlines():
        if "AssertionError" in line or "no guard" in line or "billed" in line:
            print("   ", line.strip())
