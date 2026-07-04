---
title: "apps/* tests need `uv run --with pytest pytest` — plain `uv run pytest` silently no-ops"
date: 2026-07-03
last_updated: 2026-07-04
category: docs/solutions/developer-experience
module: apps/ebay
problem_type: developer_experience
component: testing_framework
severity: medium
applies_when:
  - "Running or delegating the test suite for anything under apps/ (apps/ebay, apps/fmv)"
  - "A test run reports success but you did not see a real pytest summary line (N passed)"
tags: [pytest, uv, testing, apps, workspace, false-pass, ci]
---

# apps/* tests need `uv run --with pytest pytest` — plain `uv run pytest` silently no-ops

## Context

The repo is a uv workspace, but **`apps/*` are deliberately not workspace members** (they are `uv tool install`-managed and shell out on PATH — see the root `CLAUDE.md`). As a side effect, `pytest` is **not** a declared dependency of `apps/ebay` (or `apps/fmv`). Running the usual `uv run pytest` from an app directory therefore fails to even spawn pytest — and the failure is easy to misread as "tests ran and passed," which is exactly what happened during the BUI-271–282 simplification pass: a sub-agent reported an app suite green while 7 tests were actually broken.

## Guidance

Run app tests by injecting pytest for the invocation:

```sh
cd apps/ebay && uv run --with pytest pytest -q
```

This matches the `apps-python` job in `.github/workflows/ci.yml`. If a test also needs a runtime dep that isn't installed in the ephemeral env, add it too (e.g. `uv run --with pytest --with requests pytest -q`).

Workspace packages are unaffected — `packages/gixen-cli`, `packages/locg-cli`, and `plugins/gixen-overlay` all have pytest available, so plain `uv run pytest` works there (gixen-cli also takes `-m "not integration"`).

The tell for the failure mode: plain `uv run pytest` in an app dir prints

```
error: Failed to spawn: `pytest`
  Caused by: No such file or directory (os error 2)
```

with a **non-zero exit** and **no `N passed` summary line**. Any "green" report for an app suite that lacks a real pytest summary line did not actually run the tests.

## Why This Matters

A false pass is worse than a loud failure: a green that ran nothing hides the regression from *you*. CI does now run the app suites — the `apps-python` job runs `apps/ebay` + `apps/fmv` with exactly `uv run --with pytest pytest` (BUI-140), so a real break is still caught there. But don't lean on that: a locally-green-but-actually-skipped run means you pushed believing the change was tested, and you lose the fast local signal. (The fix is the same invocation CI uses — which is the tell that plain `uv run pytest` is the wrong local command.) Treat the presence of a genuine `N passed` line as the proof of a run, not the command's exit banner.

## When to Apply

- Any time you run tests under `apps/` yourself.
- Any time you delegate an app-test run to a sub-agent — mandate the `--with pytest` form and require the real summary line (`N passed`) back as evidence, not a "tests pass" assertion.

## Examples

Wrong (silently does nothing, reads as success to the unwary):

```sh
cd apps/ebay && uv run pytest -q
# error: Failed to spawn: `pytest` ...   <- NOT a pass
```

Right (matches CI):

```sh
cd apps/ebay && uv run --with pytest pytest -q
# 809 passed, 1 warning in 8.01s        <- a real run
```

## Related

- Root `CLAUDE.md` → "Commands" (apps/* are not workspace members; `scripts/install.sh`).
- `.github/workflows/ci.yml` → the `apps-python` job (canonical invocation).
