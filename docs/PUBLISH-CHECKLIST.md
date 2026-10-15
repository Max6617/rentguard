# Publish checklist

What has to be true before rentguard goes public, and what the user's own publish action would do.
This page performs no publish and prescribes no push: publishing is the user's decision alone, and it
is the only question that goes back to them.

## The two revisions this page describes

They are not the same revision, so every number below says which one it was read at.

- **`7f52cc3` is published.** It is the tip of `origin/master` on the public repository
  (<https://github.com/Max6617/rentguard>) — what a stranger clones today.
- **`042fd21` is the revision measured for this pass.** The tree is two commits further on —
  `9df8481` (this page's own earlier docs commit) and `be4e7c7` (tests only: the fix for the flaky
  spend assertion, defect 19) — and `master` is **17 commits ahead** of the published tip, not pushed.
  Nothing after `042fd21` touches a measured module, so the coverage figure below still describes the
  tree.

Everything in these tables was measured in a clean worktree with the package path verified inside that
worktree (`rentguard.mcp_server.__file__`), unless the line says otherwise.

| Measurement | Published tip `7f52cc3` | Local HEAD `042fd21` |
| --- | --- | --- |
| Suite | **418 passed, 0 failed, 54 subtests** | **490 passed, 0 failed, 54 subtests** |
| Coverage (Python 3.12) | **2,697 / 2,747 = 98.2%**, floor 98.0%, exit **0** | **3,037 / 3,080 = 98.6%**, floor 98.0%, exit **0** |
| CI, on push | 3.11 **green**, 3.12 **green**, 3.13 **RED** at the Coverage step | not run — nothing is pushed |

The suite figures are identical with and without `RENTGUARD_PROVIDER=fake` at both revisions. The
coverage floor is `0.98` in `tools/coverage.py`, and it was not lowered to make any number look better.

**The coverage gate is green again, and both halves of that were fixes rather than patience.** The red
97.3% at `ddd79c9` had two causes: the new `session.py` was barely reached, and the tracer counted a
`None` line-table entry per module and missed every line that ran on a thread other than the main one.
`f832596` fixed the tool (`isinstance(n, int) and n > 0`, plus `threading.settrace`), and `session.py`
is now **100%** (154 lines); `fake.py`, `provider.py` and `runpod.py` are at 100% too, and the lowest
measured module is `ledger.py` at 96.2%. Nothing is below the 94% figure, and the total clears the
floor.

| Module | `7f52cc3` | `042fd21` |
| --- | --- | --- |
| `fake.py` | 99.6% | **100.0%** |
| `provider.py` | 99.2% | **100.0%** |
| `runpod.py` | 99.3% | **100.0%** |
| `session.py` | — (does not exist) | **100.0%** |
| `mcp_server.py` | 98.7% | 99.1% |
| `cli.py` | 98.0% | 98.3% |
| `guard.py` | 99.2% | 98.3% |
| `config.py` | 99.0% | 99.0% |
| `ledger.py` | 94.3% | **96.2%** — the lowest, and well above 94% |

The figures move with the tree, and each was re-measured when a measured module changed rather than
carried forward: `18b5fc0` 96.1%; `458e128` 97.5%; `cdd7113` 98.4%; `f158396` **97.99%, exit 1**, one
line short; `7febd07` 98.4%; `310f449` 98.1%; `721436a` 98.1%; `eef6bdb` 98.6%; `7f52cc3` 98.2%;
`ddd79c9` **97.3%, exit 1**; `a9cffa0` 98.7%; `042fd21` **98.6%, exit 0**. The floor was never lowered
on any of the three occasions it went red. **The tree moved three times during this pass** (`ddd79c9` →
`a9cffa0` → `042fd21` while the page was being written), so the numbers above are pinned to the revision
named beside them and any commit after `042fd21` that touches one of these modules makes them stale.

## 1. The published revision's CI is red; the fix for it is in the unpushed tree

**A checklist that does not mention a red badge is not a checklist.** At the published tip the CI run
`38721243756` fails: `test (3.11)` and `test (3.12)` pass every step, and `test (3.13)` passes Install
and Test and then **fails at the Coverage step**; the step after it ("Check the entry point exists")
never runs, so on 3.13 the entry point is untested by CI. The published revision is unchanged, so that
badge stays red until the fixes below are pushed.

The number behind that is carried by
[`tests/_artefacts/linenos_312_vs_313_2026-10-15.txt`](../tests/_artefacts/linenos_312_vs_313_2026-10-15.txt),
measured at `3287a4f`: the same test hits divided by 3.13's line table read **2,716 / 2,775 = 97.9%**
against the 98.0% floor — red by 0.1 points. The cause is not the code and not the floor: CPython 3.13's
`dis.findlinestarts` yields a `None` line, which `trace._find_executable_linenos` keeps as a key, so the
denominator grows by one per measured module (8 in total) while the numerator is unchanged. Over all 65
files, **zero integer line numbers differ between the interpreters**, so there is no statement to cover
and none to delete.

The fix landed in the local tree at `f832596` and is **not published**, so the red badge above stands
until the push:

```python
return {n for n in _find_executable_linenos(str(path)) if isinstance(n, int) and n > 0}
```

The same commit makes the tracer reach other threads (`threading.settrace`), for the same reason: lines
that ran on a worker thread were not counted at all. `tests/test_coverage_tool.py` pins both halves, and
the `None`-entry artefact is gone with them. What this host cannot say is whether the 3.13 job then
passes end to end: there is no pytest for 3.13 here, so the one thing that can be stated is that the
divisor it failed on is fixed and the local 3.12 measurement at `042fd21` is 3,037 / 3,080 = 98.6%,
exit 0.

## 2. The tool is proven

- [x] **M1 — the suite is green with no network and no spend.** 418 passed, 0 failed, 54 subtests at
      `7f52cc3`; 490 at `042fd21`, identical with and without `RENTGUARD_PROVIDER=fake` at both. The
      real CLI is driven through the in-memory fake provider.
- [x] **The coverage ratchet works, and it has caught a regression three times.** The series is the
      evidence:
      - at `f158396`: **2,336 / 2,384 = 97.99%**, floor 98.0%, **exit 1** — one line short, because two
        defect fixes added `cli.py` and `runpod.py` lines faster than tests;
      - at `7febd07`: **2,347 / 2,384 = 98.4%**, exit 0, after a test commit covered the new branches;
      - at `310f449`, `721436a`, `eef6bdb`, `7f52cc3` and `1452718` it holds at 98.1–98.6%, exit 0;
      - at `ddd79c9` it is **2,957 / 3,038 = 97.3%, exit 1** again, because a new module (`session.py`)
        landed before its tests and the tracer was missing threaded lines;
      - at `a9cffa0` (3,013 / 3,054 = 98.7%) and `042fd21` (3,037 / 3,080 = 98.6%) it is green again,
        after `f832596` fixed the tracer and `session.py` reached 100%.
      The floor was **not** lowered on any of the three occasions: each was flagged as the open gate and
      the tests closed it.
- [x] **M2 — real rentals end to end.** Three paid runs on RunPod, **$0.0264** in total ($0.0091 +
      $0.0079 + $0.0094), three ledger rows reconciled by hand, one artefact pulled from the right
      machine. The record is [M2 — the real rental](M2-REAL-RENTAL.md).
- [x] **The install path is proven, not asserted.** See §3.
- [x] **Money and data defects found and fixed, each red-first with its test.** `up --dry-run` could
      rent (`ce69cea`, asserted on the provider call by `f7bf556`); `up` declared success on one alive
      sample (`4db7d3e`); the guard could stop a healthy pod (`d5fdaea`); `adopt` mispriced an exited
      pod (`590c58e`); storage was invisible (`fb867b6`, `f4225ff`); a pull could hold a machine open
      and a stop could hang (`03dd52d`). The night of 2026-10-14 added five more, each with an
      independent attack: the guard's idle arithmetic (`f28b2dd`, attacked by `c59d6a8`); an unmatched
      pull pattern is a finding (`a1848ef`, the prefix-vs-fnmatch hole in `5072b0e`); `up` may not let a
      recorded instance outvote the config (`c1135e1`); an empty archive is a failure (`5072b0e`); a
      partial pull names its gap (`eef6bdb`, attacked by `fec0305`). 2026-10-15 added the money-direction
      family: defect 15, a closed row priced a second time from the machine's uptime (`cdb2fba` terminal,
      `13d7dc7` live); `up` renting a second machine while a live one bills (`3287a4f`); a price that
      was never read printed as a number (`946ba28`); defect 16, a guard armed while a stale state
      file names the previous pod — it watched a dead id, reported itself armed, and left the live
      machine billing unguarded (`a9cffa0`); and defect 17, `run` failing a clean command because its
      pull legitimately delivered nothing (`042fd21`, which adds `run --require-artefacts` for the
      caller who wants an empty delivery to fail); and defect 19, a spend assertion that compared two
      samples of a live meter at a fixed four decimal places — a wall-clock race rather than a code
      defect (`be4e7c7`, tests only). All are green at the current HEAD (`be4e7c7`) — and **all of them
      are unpushed**, so the published tip does not have them.
- [ ] **R4 — the guard has never run against a rented pod.** The stop decision is exercised in-process
      and through the CLI against the fake, but `watch` has never been armed on a real billing machine,
      so "a forgotten machine stops itself" is still unproven end to end. This is the one feature the
      feature matrix cannot cover and the fake cannot fake.

## 3. The install is proven, by the wheel route

- [x] **A wheel builds with the host interpreter, installs into a clean venv, and the console script
      runs.** Re-run at `51e350e` (2026-10-15), on Python 3.12.6, in this order:
      1. `python -m pip wheel . --no-deps --no-build-isolation -w dist` — the build runs where a backend
         exists (`C:\Python312` carries setuptools) → `rentguard-0.1.0-py3-none-any.whl`, **85,921 bytes**,
         sha256 `ac4116b2…`, about 20 s, exit 0. (The wheel is larger than the 76,824 B measured at
         `e53f544` because `session.py` is in it now.)
      2. `<venv>\Scripts\python.exe -m pip install --no-index --no-deps dist\rentguard-0.1.0-py3-none-any.whl`
         → `Successfully installed rentguard-0.1.0`.
      3. `<venv>\Scripts\rentguard.exe --version` → `rentguard 0.1.0`.
      4. `rentguard doctor --json` against a root holding `.rentguard\runs.jsonl` → one JSON document,
         `findings: []`, exit 0.
      **What is proven is this route, not `pip install` from an index.** The two flags are host-only and
      load-bearing here: `--no-build-isolation`, because there is no route to PyPI so an isolated build
      waits at `Installing build dependencies: started` for a `setuptools>=68` it cannot fetch; and
      `--no-index --no-deps`, because there is nothing to resolve. One sandbox step sits in the middle and
      is also host-only: `python -m venv` dies in `ensurepip` here, writing into a mode-0700 temp directory
      (`PermissionError [Errno 13]`, then `WinError 5`), so the venv is made with `--without-pip` and pip
      bootstrapped under a `sitecustomize` shim that widens only that mode. Commands and output:
      [quickstart](quickstart.md#1-install), [VERIFICATION](VERIFICATION.md#install-the-wheel-into-a-clean-venv).
- [ ] **`python -m pip install .` itself was not run here, and the reason is named.** On a machine with
      network that is the one command, and **CI runs `python -m pip install -e .` plus
      `rentguard --version` on every push**: green on 3.11 and 3.12 at `7f52cc3`, and the 3.13 job's
      Install step also succeeds — it stops later, at Coverage.

## 4. The repo is a repo

- [x] **`LICENSE`** (MIT), `Copyright (c) 2026 Max Reischl (github.com/Max6617)`, consistent with
      `pyproject.toml` (`license = { text = "MIT" }`, `authors = [{ name = "Max Reischl" }]`).
- [x] **`.gitignore`** covers caches, build output, `.rentguard/` state, `artefacts/`, the test scratch
      roots, a local `rentguard.toml`, and `*.bak`.
- [x] **No key material in the tree.** `git grep -l 'rpa_'` finds nothing; the key is read only from
      `DSH_SECRET_RUNPOD_API_KEY` / `RUNPOD_API_KEY` and is never written to a file.
- [x] **`pyproject.toml`** carries name, version `0.1.0`, readme, `requires-python >= 3.11`, MIT, the
      author, keywords, classifiers and the console entry point `rentguard = rentguard.cli:main`. Project
      URLs are deliberately absent until there is a public home.
- [x] **`CHANGELOG.md`** carries the `0.1.0` (unreleased) entry.

## 5. The docs are true

- [x] README, quickstart, cap semantics, provider guide, the fake, the MCP page, limitations, the M2
      record and this log are written, with the output of the commands that back them.
- [x] `docs/mcp.md` is the one page a test reads: `tests/test_feature_matrix.py` extracts the tool names
      from it and compares them with `tools/list`, so the page and the server cannot drift apart.
- [x] Every page names the revision it describes; where a number appears it names where it was read.
- [x] The pages state plainly that no provider balance is read, that the cap only exists while `watch`
      runs, and that an instance without a network volume is a container.

## 6. The name is available

- [x] **`rentguard` is free on PyPI** — `https://pypi.org/pypi/rentguard/json` returns `404`, checked
      2026-10-14. Re-check it on the day of publishing.

## 7. Publication state

- **The repository is public and part of its history is pushed.** `github.com/Max6617/rentguard`
  exists, public, MIT, with `origin` wired to it in the local clone. The pushed tip is **`7f52cc3`**.
- **17 commits are unpushed** (`git rev-list --count origin/master..master` = 17): the defect-15 fix
  and its attack, the pricing-unknown contract, the money-direction fixes, the 3.13 line-number
  artefact, the `session.py` session driver, the coverage-tool fix, defects 16, 17 and 19, and this
  page's own docs commits. So the published repository is behind this tree, and it does not contain the
  fixes listed in §2.
- **The local gates are green.** Coverage at `042fd21` is 3,037 / 3,080 = 98.6%, exit 0, and the suite
  is 490 passed / 0 failed. The 3.13 tracer artefact that reddens the published badge is fixed in the
  tree by `f832596` (§1), and what remains for a green badge is the push itself. This host cannot run
  3.13, so "CI will be green" is not claimed here — only that the reason it failed is gone.
- **The user's publish action would**: push those commits (`git push origin master`) — and, separately,
  publish version `0.1.0` to PyPI if they choose to. Nothing else is outstanding on the local side.
- **What becomes irreversible**: the repository is already public, so its history is already out;
  pushing more commits adds to a record that cannot be withdrawn. A published PyPI version cannot be
  unpublished, only yanked.

## 8. Known limitations

- **The cap only exists while `watch` runs.** It is a property of the guard, not of the machine; if no
  guard is running the instance bills until someone notices. Every MCP arming call says whether
  protection is still armed. See [what the cap guarantees](cap.md).
- **The guard has never been armed on a real pod** (R4). This is the largest gap between what is tested
  and what is promised.
- **No account balance is ever read.** No supported provider exposes one reliably, so the tool reports
  recorded spend against the user's cap and nothing else. See [limitations](limitations.md).
- **An instance with no network volume is a container.** A restart discards everything on it, which is
  why artefacts are pulled as each run finishes and again before `down` stops the machine.
- **The network-volume attach path is dropped for lack of a volume, not untested.** The account's only
  volume was deleted on the user's instruction; with nothing left to attach, `[runpod].volume_id`
  cannot be exercised here. The transport seam is unit-covered (`da51005`). See `CHARTER.md` W3.
- **The community-pod ssh route must not be relied on.** A community pod has no direct endpoint, so it
  is reached through the `ssh.runpod.io` proxy. Defect 9 is open: the PTY opens, but the command still
  does not execute and the pull reads prompt text back. The session transport is built from **scripted
  transcripts** rather than a real community pod, so its tests pass while the route stays unusable.
  Renting a community pod means expecting `run` not to work.
- **Coverage is not fidelity, and the standard says so.** The 100%-of-features half is what the feature
  matrix checks; a line number only says code was reached. `fake.py` is 100% and still the one place a
  fixture can manufacture a payload that never existed — the rule is recorded in `tests/test_storage.py`.
  The converse is what took this page red earlier tonight: `session.py` was added and measured at 83.2%
  before it had tests, which is code that exists and is barely reached, and no feature test would have
  fixed it. It is 100% now, and the way there was tests, not a lower floor.
- **The published badge is red and the local tree is green.** The 3.13 job failed on a tracer artefact
  (fixed at `f832596`, §1) and the local coverage floor was red while `session.py` was thin (fixed at
  `a9cffa0`). Neither was a broken tool; both were open jobs, and both are closed in the tree that has
  not been pushed yet.

## 9. The ask

When every gate above is settled, one message to the user, and it asks one question:

1. what it is, in two sentences;
2. the exact thing being published — a public repo under which account, and whether a PyPI release is
   wanted;
3. the evidence: the test count and coverage at a named commit, the three real rentals and their total;
4. what becomes irreversible;
5. **publish?**
