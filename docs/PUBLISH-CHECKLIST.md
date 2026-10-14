# Publish checklist

What has to be true before rentguard goes public, and what the user's own publish action would do.
This page performs no publish and prescribes no push: publishing is the user's decision alone, and it
is the only question that goes back to them.

## The revision this page describes

Everything below was measured at **`f158396`** (`f1583963fe03efac25207a6e80724664689b2bdd`) in a clean
worktree, with the package path verified inside that worktree (`rentguard.mcp_server.__file__`).

| Measurement | Value | How |
| --- | --- | --- |
| Suite | **318 passed, 0 failed, 50 subtests** | `python -m pytest -q -p no:cacheprovider` in a clean worktree at `f158396`, and identical with and without `RENTGUARD_PROVIDER=fake` |
| Coverage | **97.99% (2,336 / 2,384) — below the 98.0% floor** | `python tools/coverage.py` at `f158396` exits **1**; 2,337 lines (one more) clears it. The 94.0% target is met |
| Features | **10 CLI commands, 11 MCP tools** | `tests/test_feature_matrix.py`, green here |

The standard is **94% of the code and 100% of the features**. The feature matrix is the evidence for the
features half, because a line number says code was *reached*, never that its behaviour was checked — the
guard defect that stopped a 32-second-old pod lived in lines that all executed. The code half is
**currently failing its own ratchet** at this pin: the floor is 0.98 (raised at `e3fb62a` when the
measurement was 98.4%) and the two commits after it, `e19c521` and `f158396`, added lines faster than
tests. One more covered line closes it.

**Two modules are individually below the 94% figure**, so the total is not a promise about each of them:
`ledger.py` at 93.7% and `runpod.py` at 93.8%. `mcp_server.py`, the weakest at `458e128` (90.3%), is
99.1%.

| Module | Cover | Note |
| --- | --- | --- |
| `fake.py` | 99.6% | only the tracer's sentinel line unhit |
| `mcp_server.py` | 99.1% | was 90.3% at `458e128` |
| `config.py` | 99.0% | — |
| `guard.py` | 99.0% | — |
| `cli.py` | 98.6% | — |
| `provider.py` | 96.6% | — |
| `runpod.py` | 93.8% | below 94% on its own |
| `ledger.py` | 93.7% | below 94% on its own |

The figures move with the tree: `18b5fc0` measured 96.1% with `fake.py` at 87.7%; `458e128` measured
97.5% with `mcp_server.py` at 90.3%; `cdd7113` measured 98.4% (295 tests); `f158396` measures 97.99%
(318 tests). Each was re-measured when a measured module changed, never carried forward.

## 1. The tool is proven

- [x] **M1 — the suite is green with no network and no spend.** 318 passed, 0 failed at `f158396`,
      driving the real CLI through the in-memory fake provider, and identical with and without
      `RENTGUARD_PROVIDER=fake`. `tests/test_ledger_paths.py` covers the ledger's own file paths,
      including the lock and a truncated line.
- [ ] **The coverage ratchet fails at this pin.** `tools/coverage.py` at `f158396` measures
      **2,336 / 2,384 = 97.99%** against a floor of 98.0% and exits 1. The floor was raised at `e3fb62a`
      when the measurement was 98.4%; `e19c521` and `f158396` added `cli.py` and `runpod.py` lines faster
      than tests. One more covered line (2,337) clears it. Either close it or lower the ratchet
      deliberately — the floor's own comment says it only moves upwards.
- [x] **M2 — real rentals end to end.** Three paid runs on RunPod, **$0.0264** in total
      ($0.0091 + $0.0079 + $0.0094), three ledger rows reconciled by hand from the rows' own fields, and
      one artefact pulled from the right machine. The record is [M2 — the real rental](M2-REAL-RENTAL.md).
- [x] **Money defects found and fixed, each with a test.**
      `up --dry-run` could rent (`ce69cea`, asserted on the provider call by `f7bf556`);
      `up` declared success on one alive sample (`4db7d3e`); the guard could stop a healthy pod (`d5fdaea`);
      `adopt` mispriced an exited pod (`590c58e`); storage was invisible (`fb867b6`, `f4225ff`).
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
- [ ] **The install evidence is older than this pin.** The editable install and the console script were
      verified at an earlier revision, after the `pyproject.toml` package-dir fix. They could **not** be
      re-run in this sandbox: at `458e128`, `python -m pip install -e . --no-deps` fails (exit 1) and
      `python -m pip wheel .` fails (exit 2), both because pip's build tracker and wheel cache cannot use
      the DSH temp directory. So that claim is **measured at an older revision than this one**, and should
      be re-checked in a normal environment before publishing.
- [x] **CI is green under its own environment, and it was a real defect.** The workflow sets
      `RENTGUARD_PROVIDER: fake` for the Test and Coverage steps, and that used to fail two
      `tests/test_cli_branches.py` tests: **248 passed / 2 failed with the variable, 250 passed / 0 failed
      without it**, measured at `6f31c27` and again at `458e128`. Closed at `cdd7113`, which pins the
      provider *inside* those two tests rather than changing the CI step, and the suite is now
      **318 passed / 0 failed both with the variable and without it**.

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

- **No git remote is configured** (`git remote -v` is empty), nothing has been pushed, and there are no
  tags. Everything lives in one local repository.
- **The user's publish action would**: create a public repository under Max6617's GitHub account and
  push this history to it, and — separately — publish version `0.1.0` to PyPI if they choose to.
- **What becomes irreversible**: a public repository can be deleted, though its forks and clones cannot;
  a published PyPI version cannot be unpublished, only yanked, and the name stays taken by this project.

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
- **Two modules are individually below the 94% figure**: `ledger.py` at 93.7% and `runpod.py` at 93.8%.
  The 97.99% total does not clear the line either — it is under the 98.0% ratchet at this pin — so read
  the total as "below the floor by one line" rather than as the standard met. `mcp_server.py`, the
  weakest at the earlier pin (90.3%), is now 99.1%.
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
