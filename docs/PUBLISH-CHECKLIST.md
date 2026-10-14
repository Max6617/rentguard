# Publish checklist

What has to be true before rentguard goes public, and what the user's own publish action would do.
This page performs no publish and prescribes no push: publishing is the user's decision alone, and it
is the only question that goes back to them.

## The revision this page describes

Everything below was measured at **`eef6bdb`** (`eef6bdbff5d5ac5784f73b1c6395203a6d19f811`) in a clean
worktree, with the package path verified inside that worktree (`rentguard.mcp_server.__file__`).

| Measurement | Value | How |
| --- | --- | --- |
| Suite | **398 passed, 0 failed, 54 subtests** | `python -m pytest -q -p no:cacheprovider` in a clean worktree at `eef6bdb`, and identical with and without `RENTGUARD_PROVIDER=fake` |
| Coverage | **98.6% (2,589 / 2,625)** | `python tools/coverage.py` at `eef6bdb`; floor 98.0%, exits **0**, and the 94.0% target is met |
| Features | **10 CLI commands, 11 MCP tools** | `tests/test_feature_matrix.py`, green here |

The standard is **94% of the code and 100% of the features**. The feature matrix is the evidence for the
features half, because a line number says code was *reached*, never that its behaviour was checked — the
guard defect that stopped a 32-second-old pod lived in lines that all executed.

**One module is individually below the 94% figure**: `ledger.py` at 93.7%. Everything else clears it, so
read the total as the project's number and not as a promise about every module.

| Module | Cover | Note |
| --- | --- | --- |
| `fake.py` | 99.6% | only the tracer's sentinel line unhit |
| `runpod.py` | 99.3% | 92.7% at `721436a`; the RunPod surface is covered now |
| `provider.py` | 99.2% | — |
| `guard.py` | 99.1% | — |
| `cli.py` | 99.0% | — |
| `config.py` | 99.0% | — |
| `mcp_server.py` | 98.7% | was 90.3% at `458e128` |
| `ledger.py` | 93.7% | below 94% on its own, and the lowest here |

The figures move with the tree: `18b5fc0` measured 96.1%; `458e128` 97.5%; `cdd7113` 98.4% (295 tests);
`f158396` 97.99% (318 tests, **exit 1**, one line short); `7febd07` 98.4% (322 tests, exit 0); `310f449`
98.1% (350 tests, exit 0); `721436a` 98.1% (355 tests, exit 0); `eef6bdb` 98.6% (398 tests, exit 0). Each
was re-measured when a measured module changed, never carried forward.

## 1. The tool is proven

- [x] **M1 — the suite is green with no network and no spend.** 398 passed, 0 failed at `eef6bdb`,
      driving the real CLI through the in-memory fake provider, and identical with and without
      `RENTGUARD_PROVIDER=fake`. `tests/test_ledger_paths.py` covers the ledger's own file paths,
      including the lock and a truncated line.
- [x] **The coverage ratchet works, and it caught a regression this session.** The series is the
      evidence the mechanism does its job:
      - at `f158396`: **2,336 / 2,384 = 97.99%**, floor 98.0%, `tools/coverage.py` **exits 1** — one
        line short. `e19c521` and `f158396` fixed two defects and added `cli.py` and `runpod.py` lines
        faster than tests;
      - at `7febd07`: **2,347 / 2,384 = 98.4%**, **exits 0**, after a test commit covered the new pull
        and rate branches;
      - at `310f449`: **2,494 / 2,542 = 98.1%**, **exits 0**; `721436a` adds tests only and holds 98.1%
        (**355 passed**); `eef6bdb` is **2,589 / 2,625 = 98.6%**, **exits 0**, after the night's guard,
        pull and `up` fixes with their tests.
      The floor was **not** lowered: it was flagged as an open gate (the ratchet's own comment says it
      only moves upwards) and the tests closed it.
- [x] **M2 — real rentals end to end.** Three paid runs on RunPod, **$0.0264** in total
      ($0.0091 + $0.0079 + $0.0094), three ledger rows reconciled by hand from the rows' own fields, and
      one artefact pulled from the right machine. The record is [M2 — the real rental](M2-REAL-RENTAL.md).
- [x] **Money and data defects found and fixed, each red-first with its test.**
      `up --dry-run` could rent (`ce69cea`, asserted on the provider call by `f7bf556`);
      `up` declared success on one alive sample (`4db7d3e`); the guard could stop a healthy pod (`d5fdaea`);
      `adopt` mispriced an exited pod (`590c58e`); storage was invisible (`fb867b6`, `f4225ff`);
      a pull could hold a machine open and a stop could hang (`03dd52d`).
      The night's five, all with an independent attack beside them: the guard's idle arithmetic — elapsed
      watch time is not idle time, a busy GPU outvotes the provider's host idle, and the count is bounded
      by the pod's age (`f28b2dd`, attacked by `c59d6a8`); a pull pattern that matched nothing is a
      finding rather than a silent success (`a1848ef`, and the prefix-vs-fnmatch hole in `5072b0e`);
      `up` may not let a recorded instance silently outvote the config's cloud and card (`c1135e1`);
      an empty archive is a failure, not a delivery of zero files (`5072b0e`); and a partial pull reports
      its gap, with the lost-artefact sentence naming the patterns (`eef6bdb`, both attacked by
      `fec0305`).
- [ ] **R4 — the guard has never run against a rented pod.** The stop decision is exercised in-process
      and through the CLI against the fake, but `watch` has never been armed on a real billing machine,
      so the sentence "a forgotten machine stops itself" is still unproven end to end. This is the one
      feature the matrix cannot cover and the fake cannot fake.

## 2. The repo is a repo

- [x] **`LICENSE`** (MIT), `Copyright (c) 2026 Max Reischl (github.com/Max6617)`, consistent with
      `pyproject.toml` (`license = { text = "MIT" }`, `authors = [{ name = "Max Reischl" }]`).
- [x] **`.gitignore`** covers caches, build output, `.rentguard/` state, `artefacts/`, the test scratch
      roots (`.rentguard-tmp/`, `.assay-tmp/`, `.verif/`), a local `rentguard.toml`, and `*.bak`.
- [x] **No key material in the tree.** `git grep -l 'rpa_'` finds nothing; the key is read only from
      `DSH_SECRET_RUNPOD_API_KEY` / `RUNPOD_API_KEY` and is never written to a file.
- [x] **`pyproject.toml`** has name, version `0.1.0`, description, readme, `requires-python >= 3.11`,
      MIT licence, the author, keywords, classifiers, and the console entry point
      `rentguard = rentguard.cli:main`. Project URLs are deliberately absent until there is a public
      home, with the reason recorded in the file.
- [x] **`CHANGELOG.md`** carries the `0.1.0` (unreleased) entry.
- [ ] **The install evidence is older than this pin, and this sandbox still cannot produce it.** The
      editable install and the console script were verified at an earlier revision, after the
      `pyproject.toml` package-dir fix. Re-attempted at `eef6bdb`: `python -m venv` fails outright here
      (`ensurepip` exits 1), `python -m pip install -e . --no-deps` fails (exit 1) and
      `python -m pip wheel .` fails (exit 2), all because pip and `ensurepip` cannot use the DSH temp
      directory. So the install claim is **measured at an older revision than this one**, and the one
      thing that must happen in a normal environment before publishing is a fresh-venv
      `pip install rentguard` plus `rentguard --version`. It is not verified here.
- [x] **CI is green under its own environment, and it was a real defect.** The workflow sets
      `RENTGUARD_PROVIDER: fake` for the Test and Coverage steps, and that used to fail two
      `tests/test_cli_branches.py` tests: **248 passed / 2 failed with the variable, 250 passed / 0 failed
      without it**, measured at `6f31c27` and again at `458e128`. Closed at `cdd7113`, which pins the
      provider *inside* those two tests rather than changing the CI step, and the suite is now
      **398 passed / 0 failed both with the variable and without it**.

## 3. The docs are true

- [x] README, quickstart, cap semantics, provider guide, the fake, the MCP page, limitations, the M2
      record and the verification log are all written, with the output of the commands that back them.
- [x] `docs/mcp.md` is the one page a test reads: `tests/test_feature_matrix.py` extracts the tool names
      from it and compares them with `tools/list`, so the page and the server cannot drift apart.
- [x] Every page marks what was **exercised** and what is only **described**; the table is in
      [VERIFICATION](VERIFICATION.md).
- [x] The pages state plainly that no provider balance is read, that the cap only exists while `watch`
      runs, and that an instance without a network volume is a container.

## 4. The name is available

- [x] **`rentguard` is free on PyPI** — `https://pypi.org/pypi/rentguard/json` returns `404`, checked
      2026-10-14. This is true as of that check and should be re-checked on the day of publishing.

## 5. Publication state

- **The repository is public, and part of the history is pushed.** `github.com/Max6617/rentguard` exists,
  public, MIT. The pushed tip is **`ad2e880`**, and **7 commits have landed since** it, up to `eef6bdb`
  (`git rev-list --count origin/master..master` = 7), so the published repository is behind this tree.
- **The user's publish action would**: push those 7 commits (`git push origin master`), and — separately —
  publish version `0.1.0` to PyPI if they choose to. Nothing else is outstanding on the local side.
- **What becomes irreversible**: the repository is already public, so its history is already out; pushing
  more commits adds to a record that cannot be withdrawn. A published PyPI version cannot be unpublished,
  only yanked.

## 6. Known limitations

- **The cap only exists while `watch` runs.** It is a property of the guard, not of the machine; if no
  guard is running the instance bills until someone notices. The MCP surface arms it in bounded calls
  (`watch_once`, `watch`) and every return says whether protection is still armed. See
  [what the cap guarantees](cap.md).
- **The guard has never been armed on a real pod** (R4 above). This is the largest gap between what is
  tested and what is promised.
- **No account balance is ever read.** No supported provider exposes one reliably, so the tool reports
  recorded spend against the user's cap and nothing else. See [limitations](limitations.md).
- **An instance with no network volume is a container.** A restart discards everything on it, which is
  why artefacts are pulled as each run finishes and again before `down` stops the machine.
- **The network-volume attach path is dropped for lack of a volume, not untested.** The account's only
  volume was deleted on the user's instruction; with nothing left to attach, `[runpod].volume_id`
  cannot be exercised here. The transport seam is unit-covered (`da51005`). See `CHARTER.md` W3.
- **The community-pod ssh route must not be relied on.** A community pod has no direct endpoint, so it is
  reached through the `ssh.runpod.io` proxy. Defect 9 is open: the PTY opens, but the command still does
  not execute and the pull reads prompt text back. The session transport is being built from **scripted
  transcripts** rather than a real community pod, so it is untested on hardware; the direct route is
  unaffected. Renting a community pod means expecting `run` not to work.
- **One module is individually below the 94% figure**: `ledger.py` at 93.7%. Everything else clears it —
  `runpod.py` is 99.3% at this pin, up from 92.7% at `721436a`. The 98.6% total clears the standard and
  the 98.0% floor; read it as the project's number, not as a promise about every module.
- **`fake.py` is no longer a limitation.** It is 99.6% with nothing real unhit; the only line the tracer
  reports is its sentinel. It is the module where a fixture can manufacture a world that is not real, so
  the standing rule is recorded in `tests/test_storage.py`: a provider-payload fixture comes from a live
  response or the provider's schema, and an invented one says so.

## 7. The ask

When every gate above is settled, one message to the user, and it asks one question:

1. what it is, in two sentences;
2. the exact thing being published — a public repo under which account, and whether a PyPI release is
   wanted;
3. the evidence: the test count and coverage at a named commit, the three real rentals and their total;
4. what becomes irreversible;
5. **publish?**
