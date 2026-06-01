---
title: "feat: Migrate ebay-cli into comic-pipeline as apps/ebay"
type: feat
status: active
date: 2026-05-19
---

# feat: Migrate ebay-cli into comic-pipeline as apps/ebay

**Target repo:** comic-pipeline (`~/Projects/comic-pipeline`)

## Overview

Move `ebay_fetch.py` and its tests from the standalone `ebay-cli` repo into `comic-pipeline/apps/ebay/`, wired as a self-contained Python package with a `pyproject.toml`. Archive the original `ebay-cli` GitHub repo to retire it cleanly.

## Problem Frame

`ebay-cli` is a standalone repo with a single module (`ebay_fetch.py`) that fetches structured listing data from the eBay Browse API. The comic-pipeline monorepo is the canonical home for all comic-workflow tooling. Consolidating here simplifies dependency management, keeps the tooling co-located, and removes a dangling standalone repo.

## Requirements Trace

- R1. `apps/ebay/` package exists in comic-pipeline with `pyproject.toml` and `ebay-fetch` CLI entry point
- R2. `ebay_fetch.py` and `test_ebay_fetch.py` are in the new location and tests pass
- R3. Original `ebay-cli` GitHub repo is archived

## Scope Boundaries

- No changes to `ebay_fetch.py` logic — this is a straight file migration
- Integration tests (require eBay credentials) continue to be skipped by default
- The `ebay-fetch` CLI invocation via subprocess in integration tests uses `sys.executable` + module path; this is acceptable for integration tests and is not refactored here

### Deferred to Separate Tasks

- PER-32 (ezship-cli migration): identical pattern, separate issue
- Wiring `ebay-fetch` into the `comic:identify` skill: PER-33

## Context & Research

### Relevant Code and Patterns

- `plugins/gixen-overlay/pyproject.toml` — the layout to mirror: hatchling build, `src/` layout, `[project.scripts]`, `[tool.pytest.ini_options]` with `pythonpath = ["src"]`. Note: gixen-overlay uses a **package** layout (`src/gixen_overlay/` with `__init__.py`); `apps/ebay` uses a **flat module** layout (`src/ebay_fetch.py`). The `pyproject.toml` structure is the same; the hatchling build target differs (see Key Technical Decisions).
- `plugins/gixen-overlay/tests/` — test files co-located in `tests/` dir at package root

### Institutional Learnings

- None directly applicable — this is a file migration following an established pattern

## Key Technical Decisions

- **Single-module layout (`src/ebay_fetch.py`, not `src/ebay_fetch/__init__.py`)**: `ebay_fetch.py` is a flat module, not a package. Keeping it as `src/ebay_fetch.py` matches the original import style (`import ebay_fetch`) without restructuring. The `pyproject.toml` `[tool.hatch.build.targets.wheel]` can declare `packages = ["src"]` or use `sources` to include the flat file.
- **`requests` is the only non-stdlib dep**: Declare it in `[project.dependencies]`. No need to pull `gixen-cli` as a dependency (unlike `gixen-overlay`).
- **CLI entry point**: `ebay-fetch = "ebay_fetch:main"` under `[project.scripts]`.
- **Plan lives in gixen-cli docs/plans**: All PER-* plans are there; comic-pipeline has no `docs/plans/` directory.

## Output Structure

```
apps/ebay/
├── pyproject.toml
├── src/
│   └── ebay_fetch.py
└── tests/
    ├── __init__.py
    └── test_ebay_fetch.py
```

## Implementation Units

- [ ] **Unit 1: Scaffold apps/ebay and migrate source**

**Goal:** Create the `apps/ebay/` package structure, copy `ebay_fetch.py` and `test_ebay_fetch.py`, write `pyproject.toml`, and verify tests pass.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Create: `apps/ebay/pyproject.toml`
- Create: `apps/ebay/src/ebay_fetch.py` (from `~/Projects/ebay-cli/ebay_fetch.py`)
- Create: `apps/ebay/tests/__init__.py`
- Create: `apps/ebay/tests/test_ebay_fetch.py` (from `~/Projects/ebay-cli/test_ebay_fetch.py`)

**Approach:**
- Mirror `plugins/gixen-overlay/pyproject.toml` but replace `gixen-cli` dep with `requests`, set `name = "ebay-fetch"`, add `[project.scripts]` with `ebay-fetch = "ebay_fetch:main"`, and set `[tool.pytest.ini_options] pythonpath = ["src"]`
- For hatchling: flat module needs `[tool.hatch.build.targets.wheel]` with `include = ["src/ebay_fetch.py"]` (hatchling does not auto-discover flat modules the way it does packages)
- `ebay_fetch.py`: no changes to logic
- `test_ebay_fetch.py`: no changes needed — `import ebay_fetch` resolves via the `pythonpath = ["src"]` pytest config
- After creating files, run `pytest tests/` from `apps/ebay/` to confirm all unit tests pass (integration tests will skip automatically)

**Patterns to follow:**
- `plugins/gixen-overlay/pyproject.toml` — build system, layout, pytest config

**Test scenarios:**
- Happy path: `pytest apps/ebay/tests/` from comic-pipeline root passes (all non-integration tests green)
- Edge case: integration tests are automatically skipped (no credentials required to pass CI)

**Verification:**
- `pytest tests/` from within `apps/ebay/` exits 0 with all non-integration tests passing
- `pip install -e apps/ebay/` followed by `ebay-fetch --help` produces the help text without error

- [ ] **Unit 2: Archive ebay-cli GitHub repo**

**Goal:** Archive the `hsukenooi/ebay-cli` GitHub repository to signal it is no longer actively maintained.

**Requirements:** R3

**Dependencies:** Unit 1 (migration confirmed working before archiving)

**Files:** None in comic-pipeline — this is a GitHub API action

**Approach:**
- Run `gh repo archive hsukenooi/ebay-cli` to set the repository to archived/read-only on GitHub
- The local clone at `~/Projects/ebay-cli/` can remain as-is; archiving only affects GitHub

**Test scenarios:**
- Test expectation: none — archiving is a one-way GitHub API action; verify visually that the repo shows "Archived" badge on GitHub

**Verification:**
- `gh repo view hsukenooi/ebay-cli --json isArchived` returns `{"isArchived": true}`

## System-Wide Impact

- **Interaction graph:** No other code imports `ebay_fetch` directly; the module is standalone
- **Unchanged invariants:** `ebay_fetch.py` logic, public CLI interface (`ebay-fetch <ids> [--json] [--fields]`), and config file location (`~/.config/ebay-fetch/`) are all unchanged

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Hatchling flat-module discovery fails (doesn't find `src/ebay_fetch.py`) | Use explicit `include` in `[tool.hatch.build.targets.wheel]` |
| Archive is irreversible | Confirm migration tests pass before running `gh repo archive` |

## Sources & References

- Related PR series: PER-29 (monorepo bootstrap), PER-30 (gixen-overlay plugin)
- Pattern file: `plugins/gixen-overlay/pyproject.toml`
- Linear issue: PER-31
