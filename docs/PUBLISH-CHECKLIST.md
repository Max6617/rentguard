# Publish checklist

The definition of M3. Nothing here is asked of the user until every box below is true, and the only
question that goes to them is the last section.

## Status — 2026-10-14 15:28 UTC

| section | state |
|---|---|
| 1. proven | M1 green (58 tests, fake provider, no network). M2 **passed** on a real rented 4090 — artefact pulled from the right machine, ledger reconciled by hand, pod stopped. |
| 2. repo | `.gitignore` thorough; no secrets in the working tree or in history (env-var *names* only); `LICENSE` (MIT) added; `CHANGELOG.md` added; CI workflow added; `pyproject.toml` has authors, keywords, classifiers; `rentguard.egg-info` and `cli.py.bak` removed. |
| 3. docs | quickstart, cap semantics, provider guide, MCP page, limitations, M2 record, verification notes — all written and run against real output. |
| 4. name | **`rentguard` is free on PyPI** — the JSON API returns 404. |
| open | the ordering-dependent test (`ArtefactLostMidRun`); `pip install -e .` not verified locally (my venv attempt hung without network, so CI is the only evidence); the frozen revision has not yet had its own real rental. |

## 1. The tool is proven

- [ ] M1: the fake-provider suite is green, no network, no spend.
- [ ] M2: one real rental end to end — `up`, `run`, artefact pulled, `stop`, ledger row closed — with
      the observed cost matching the ledger's arithmetic to the cent.
- [ ] Every open defect in `docs/KNOWN-FAILURES.md` is either fixed with a test or moved to a
      "known limitations" section with a reproduction.
- [ ] `python -m unittest discover` and the fake-provider suite both pass from a clean checkout.

## 2. The repo is a repo

- [ ] `LICENSE` (MIT) present and consistent with `pyproject.toml`.
- [ ] `.gitignore` covers `__pycache__/`, `*.pyc`, `.rentguard/`, `artefacts/`, `*.egg-info/`,
      `dist/`, `build/`, and any local config holding a key.
- [ ] No secret anywhere in the tree or in git history. Verify with a search for the key prefix and
      for the literal env var names; a leaked key in the first commit is the classic way a good tool
      starts badly.
- [ ] `pyproject.toml` has: name, version, description, readme, requires-python, license, authors,
      classifiers, project URLs, and an entry point that works after `pip install .`
- [ ] Version bumped to 0.1.0 for the first release, with a `CHANGELOG.md` entry.
- [ ] CI workflow running the test suite on Python 3.11, 3.12 and 3.13, with no network access.

## 3. The docs are true

- [ ] Every command in the README has been run and its real output pasted.
- [ ] The limitations page states: no reliable balance endpoint exists, so the tool reports spend
      against a cap and never the account balance; the guard is a foreground process whose liveness
      must be checked; an instance without a network volume is a container that a restart discards;
      a machine reporting RUNNING with zero uptime is a crash loop.
- [ ] The MCP page states the approval rule, and that the cap is the backstop for when it is
      forgotten rather than the permission itself.

## 4. The name is available

- [ ] PyPI: is `rentguard` free? Check, and if taken pick a fallback — `rentguard-cli`, `gpu-rentguard`,
      `spendcap` — and decide before the announcement, not after.
- [ ] No existing project under a confusingly similar name dominates search results.
- [ ] The CLI name does not collide with anything already on PATH in the usual environments.

## 5. The ask

When all of the above is true, one message to the user containing:

1. what it is, in two sentences;
2. the exact thing being published — public repo under which account, and a PyPI release or not;
3. the evidence: test counts, the one real rental's cost, the ledger row;
4. what becomes irreversible (a public repo can be deleted, but a published package version cannot be
   unpublished);
5. and the single question: publish?

Anything that fails before then is my problem, not the user's.
