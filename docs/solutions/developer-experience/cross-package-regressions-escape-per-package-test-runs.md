---
title: "Cross-Package Regressions Escape Per-Package Test Runs — Trust the Full CI Matrix, Not Green Local Suites"
date: 2026-07-11
category: docs/solutions/developer-experience
module: "apps/ebay + packages/gixen-cli + plugins/gixen-overlay (CI workspace job)"
problem_type: developer_experience
component: testing_framework
severity: medium
applies_when:
  - "Working across multiple uv workspace packages/apps in a single session (parallel agents or sequential edits)"
  - "Changing a function's return type or shape when a sibling package imports it or shells out to it"
  - "Running only the touched package's own test suite before considering a change done"
  - "Writing or reviewing a mock that hardcodes a dependency's return shape (e.g. return_value=[...])"
  - "A sibling package has a guard test that regex-scans another package's source file"
tags: [cross-package-regressions, test-isolation, ci-gate, mocking, guard-tests, uv-workspace, multi-agent, contract-drift]
---

# Cross-Package Regressions Escape Per-Package Test Runs — Trust the Full CI Matrix, Not Green Local Suites

## Context

`comic-pipeline` is a uv workspace: `packages/*` (gixen-cli, locg-cli) + `plugins/*` (gixen-overlay) share one venv, and `apps/*` (ebay, fmv) are **not** workspace members — they are `uv tool install`ed and tested separately. There is **no repo-wide test runner**; each package/app is tested from its own directory (`cd packages/gixen-cli && uv run pytest`, `cd apps/ebay && uv run --with pytest pytest`). CI (`.github/workflows/ci.yml`) mirrors that split into separate jobs: `workspace` (gixen-cli + locg-cli + gixen-overlay) and `apps-python` (apps/ebay + apps/fmv).

The consequence: **a green local run only proves the touched package doesn't regress itself.** It says nothing about sibling packages that import the changed function, nor about cross-package tests that assert on the changed file's contents. During a parallel multi-agent session (BUI-283/BUI-297/BUI-298), two locally-green changes each broke a *different* package and were caught only by the full CI matrix / EM review — never by the implementing agent's own test loop.

## Guidance

Treat "my package's suite is green" as **necessary, not sufficient** whenever you touch code another package can see. Three specific traps, each with a concrete check:

**1. Return-shape / signature changes — grep every consumer, not just the file's own tests.**
```sh
grep -rn "verify_with_claude" packages/ plugins/ apps/
```
Run this *before* changing a shared function's contract, and again after, to confirm every call site was updated. A consumer in a sibling file — or a sibling package — is not exercised by the changed file's own test suite.

**2. Mocks that hardcode a function's old return shape mask the very contract change you need to catch.**
When you change what a function returns, find and update every mock of it **in the same pass** as the signature change — never as a follow-up cleanup:
```sh
grep -rn "verify_with_claude" apps/ebay/tests/ packages/*/tests/ plugins/*/tests/
```
Prefer mocks that mirror the real return shape (tuple in → tuple out) over convenience shortcuts (`return_value=[m1]`). A shortcut mock enforces nothing about the contract — it just freezes the old one and hides the break from the suite meant to catch it.

**3. Cross-package guard tests that assert on a file's source text break on legitimate relocation.**
Before moving/refactoring code out of a file, grep for any test — in any package — that references that filename or scans its contents:
```sh
grep -rln "grade_photos" packages/*/tests/ plugins/*/tests/ apps/*/tests/
```
These tests often assert on substrings (`"json.load" in content`, retry-logic literals) rather than behavior. A relocation makes the assertion false without making the behavior wrong — re-point the assertion at the new location/delegation pattern, don't delete it.

**Bottom-line rule:** after any cross-package-visible change, run `gh pr checks` and wait for the full matrix — especially the `workspace` job — before declaring the work done. A single package's local `uv run pytest` is not a substitute for CI. (And in `apps/*`, plain `uv run pytest` silently no-ops and false-passes — it must be `uv run --with pytest pytest`; another false-green failure mode — see the sibling doc below. *(auto memory [claude])*)

## Why This Matters

Both regressions shipped through an agent's own GREEN local suite and were caught only downstream:

- **Regression 1 (BUI-297) — masked by old-shape mocks.** `verify_with_claude()` in `apps/ebay/src/seller_scan.py` changed from returning a bare list to a `(kept, dropped)` tuple (later `(kept, dropped, filtered)`). The sibling consumer `apps/ebay/src/wishlist_sellers.py` (~line 597) still unpacked it as a list → `TypeError: list indices must be integers` in the **unattended, scheduled** `/comic:wishlist-sellers` flow — a production failure with no human present to notice. The change's own test file, `test_wishlist_sellers.py`, was GREEN because its mocks (`return_value=[m1]`, …) still returned the OLD list shape — the mocks actively hid the break from the very suite meant to catch it. The fix touched ~17–22 mock sites, added handling for `dropped` (must stay uncached/unseen with a loud stderr WARNING so items resurface next run), and uncovered a third latent bug: dropped candidates were being persisted as a permanent `False` verdict and would never re-verify.

- **Regression 2 (BUI-283) — source-text guard tests in *other* packages.** A refactor made `apps/ebay/src/grade_photos.py` delegate OAuth/fetch to `apps/ebay/src/ebay_fetch.py`, removing grade_photos.py's inline `json.load` config read and its 429-retry logic. This broke two guard tests that live in **other packages** and assert on grade_photos.py's source text: `packages/gixen-cli/tests/test_skill_migration.py::test_grade_photos_reads_json_not_dotenv` (asserted `"json.load" in content`) and `plugins/gixen-overlay/tests/test_grade_doc.py::test_download_checks_http_status_and_retries_429` (asserted the retry logic lived in grade_photos.py). The implementing agent ran only `cd apps/ebay && uv run --with pytest pytest` (834 green) and never ran the gixen-cli/gixen-overlay suites, so neither failure was visible locally. CI's `workspace` job caught both.

Both share one shape: the blast radius extended **outside the directory the agent was working in and testing**, and the local "all green" signal was actively misleading, not merely incomplete.

## When to Apply

- Changing the **return type, return shape, or parameter signature** of any function other files/packages import — grep every call site before merging.
- **Refactoring/relocating code** between files — grep for tests in *other* packages that assert on the old file's source text or behavior location.
- Editing a function that **has mocked call sites** — update the mocks' return shape in the same change; prefer mocks that mirror the real contract.
- Working in `apps/ebay`, specifically the `seller_scan.py` / `wishlist_sellers.py` / `grade_photos.py` / `ebay_fetch.py` cluster — these four have a documented history of exactly these cross-file contract and delegation couplings.
- Before marking any PR done: run `gh pr checks` and confirm the full matrix (`workspace`, `apps-python`, `lint`, `ezship`, `typecheck`) — don't infer repo-wide safety from one job or one local package run.

## Examples

**Mock-masking a contract change (bad vs. good):**
```python
# BAD — mock pins the OLD shape, hides the break from the suite
patch.object(ws, "verify_with_claude", MagicMock(return_value=[m1]))  # now returns (kept, dropped)!
...
verified_reps = verify_with_claude(representatives)   # consumer still treats it as a list
for rep in verified_reps:      # TypeError once verify_with_claude actually returns a tuple
    ...

# GOOD — mock mirrors the real (kept, dropped) contract, so any consumer
# that fails to unpack it fails the test immediately
patch.object(ws, "verify_with_claude", MagicMock(return_value=([m1], [])))  # (kept, dropped)
...
kept, dropped = verify_with_claude(representatives)
for rep in dropped:            # never-verified → stay uncached/unseen, warn loudly
    print(f"WARNING: candidate never verified, will resurface next run: {rep}", file=sys.stderr)
```

**Guard-test relocation (bad vs. good assertion):**
```python
# BAD — asserts on source text that legitimately moved; breaks on a valid refactor
content = GRADE_PHOTOS_SCRIPT.read_text()
assert "json.load" in content          # now false: grade_photos delegates to ebay_fetch.load_config

# GOOD — assert the delegation relationship instead of the literal that moved
content = GRADE_PHOTOS_SCRIPT.read_text()
assert "load_config" in content
assert "from ebay_fetch import" in content

# And re-point the retry-logic assertion at its new home (ebay_fetch.py, not grade_photos.py):
content = EBAY_FETCH_SCRIPT.read_text()
assert "429" in content and "status_code" in content
```

## Related
- `docs/solutions/developer-experience/apps-tests-require-uv-run-with-pytest.md` — sibling gotcha in the same uv-workspace / CI-split area. That doc is about tests *not running at all* (plain `uv run pytest` no-ops in `apps/*`); this one is about tests *running and passing* while still missing a cross-package break.
- `docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md` §(b) — prescribes an import-time smoke test (`test_workspace_imports.py`) so a host rename/signature change surfaces as a plugin-side CI failure. This doc is the general, repo-wide version of that one-coupling mitigation: grep all consumers + update mock shapes + grep guard tests + trust the CI matrix.
- `docs/solutions/best-practices/mypy-bool-return-type-and-non-required-typecheck-ci.md` — shares the "a green PR doesn't mean everything is green; inspect `gh pr checks`, not just mergeability" prevention rule.
- Linear: BUI-297 (verify_with_claude list→tuple), BUI-298 (multi-seller batching that grew the tuple), BUI-283 (grade_photos→ebay_fetch delegation).
