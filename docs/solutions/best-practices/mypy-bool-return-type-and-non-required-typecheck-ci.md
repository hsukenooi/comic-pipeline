---
title: "A `-> bool` function returning an and/or chain over `.get()` fails mypy; the typecheck CI job is non-required"
date: 2026-07-04
category: best-practices
module: apps/fmv (fmv_runner.py) + plugins/gixen-overlay (routes.py)
problem_type: best_practice
component: tooling
severity: low
related_components:
  - gixen-overlay
  - comic-fmv
  - tooling
applies_when:
  - "Writing or reviewing a Python helper annotated `-> bool`"
  - "A boolean-returning function's body is an and/or chain over `dict.get()` or other optional/Any values"
  - "Reasoning about whether a green PR actually passed mypy (which CI checks are required)"
tags: [mypy, type-hints, bool, ci, non-required-check, python-gotcha, bui-188]
---

# A `-> bool` function returning an and/or chain over `.get()` fails mypy; the typecheck CI job is non-required

## Context

The BUI-188 `typecheck` CI job (`uvx mypy --ignore-missing-imports` over two files) went red on `main` on a function nobody had just touched. It surfaced incidentally: an unrelated PR's CI run (BUI-289 / PR #123, a test+docs change) showed `typecheck` failing on `plugins/gixen-overlay/src/gixen_overlay/routes.py:1434`. The failure was pre-existing and had been sitting red on `main` because — as this doc's second half explains — `typecheck` is a **non-required** check, so it never blocked the merge that introduced it. Two reusable facts came out of it.

## Guidance

**1. A `-> bool` function whose body is an `and`/`or` chain starting from an optional/`Any` value silently violates its own return type. Wrap it in `bool(...)`.**

Python's `and`/`or` are **operand-returning, not bool-coercing**: `a and b` evaluates to `a` (if falsy) or `b`, keeping the operands' types. And `dict.get("k")` is typed `Any | None`. So a chain like `row.get("in_collection") and row.get("x") == y` has inferred type `Any | bool | None` — not `bool`. Under a function annotated `-> bool`, mypy raises `Incompatible return value type (got "Any | bool | None", expected "bool") [return-value]`. The runtime behavior is already correct (callers use it in boolean context); it is purely an annotation-vs-inference gap. The fix is a `bool(...)` wrap around the whole expression — a no-op at runtime, an explicit narrowing to the type checker.

This is the type-hygiene cousin of the exception-ordering gotcha noted in the sibling FMV doc (`requests.exceptions.JSONDecodeError` subclasses both `ValueError` and `RequestException`): both are cases where Python's "obvious" reading of an expression differs from what the type system / MRO actually sees.

**2. Know which CI checks are *required*. Green required-checks does not mean mypy is green.**

This repo's `.github/workflows/ci.yml` runs several independent jobs: `workspace` (syncs the uv workspace, smoke-imports, and runs the `gixen-cli` / `locg-cli` / `gixen-overlay` pytest suites), `apps-python` (`apps/ebay` + `apps/fmv` pytest, each `uv run --with pytest`), `lint` (ruff exception-hygiene rules), `ezship` (tsc + vitest), and `typecheck` (BUI-188: `uvx mypy --ignore-missing-imports apps/fmv/src/fmv_runner.py plugins/gixen-overlay/src/gixen_overlay/routes.py` — **non-strict, only those two files**). The `typecheck` job is **not a required status check**: a PR whose only red check is `typecheck` reports `mergeStateStatus: UNSTABLE` / `mergeable: MERGEABLE` (not `BLOCKED`) and merges normally. That is how a type error sat red on `main` unnoticed. When a check is advisory-only, its failures accumulate silently — inspect `gh pr checks <n>` (not just "is it mergeable") before assuming the tree is clean, and run `uvx mypy` locally on the two targeted files before committing changes to them.

## Why This Matters

The mypy job exists precisely because `fmv_runner.py` and `routes.py` are the most `None`-juggling files in the repo (BUI-188). Letting it rot red defeats the point — a real `Optional`-handling regression in those money-path files would blend into the pre-existing noise. And the `-> bool` pattern is easy to reintroduce: `dict.get()`-driven predicates are idiomatic here, so any new one that forgets the `bool()` wrap re-reds the job.

## When to Apply

- Writing any `-> bool` predicate whose body is an `and`/`or` chain over `.get()`, attribute access on an optional, or other `Any`/`Optional` values — wrap the return in `bool(...)`.
- Reviewing a PR touching `apps/fmv/src/fmv_runner.py` or `plugins/gixen-overlay/src/gixen_overlay/routes.py` — the `typecheck` job covers only these; a green *required* set says nothing about them.
- Before trusting "CI is green": confirm whether the failing check is required (`BLOCKED`) or advisory (`UNSTABLE`).

## Examples

Before (mypy: `Incompatible return value type (got "Any | bool | None", expected "bool")`):

```python
def _is_pinned_collection_row(row, full_title, release_date) -> bool:
    return (
        row.get("in_collection")                              # Any | None
        and row.get("full_title") == full_title
        and (row.get("release_date") or None) == release_date
    )
```

After (`bool(...)` narrows the operand-chain to the annotated type; runtime unchanged):

```python
def _is_pinned_collection_row(row, full_title, release_date) -> bool:
    return bool(
        row.get("in_collection")
        and row.get("full_title") == full_title
        and (row.get("release_date") or None) == release_date
    )
```

Reproduce the CI check locally (both targeted files, so any sibling surfaces too):

```
uvx mypy --ignore-missing-imports apps/fmv/src/fmv_runner.py plugins/gixen-overlay/src/gixen_overlay/routes.py
```

## Related

- `docs/solutions/best-practices/fmv-self-referential-feedback-deflation-guard.md` — the sibling Python gotcha (`JSONDecodeError` subclassing both `ValueError` and `RequestException`) surfaced during the same FMV feedback-loop work.
- `docs/solutions/developer-experience/apps-tests-require-uv-run-with-pytest.md` — the `apps/*` tests need `uv run --with pytest` (the exact invocation the `apps-python` CI job uses). CI *does* run the pytest suites as the merge gate (BUI-140, `workspace` + `apps-python` jobs); `typecheck` is the non-required outlier described above.
- BUI-290 (fix), surfaced via BUI-289 / PR #123; the `typecheck` job is BUI-188.
