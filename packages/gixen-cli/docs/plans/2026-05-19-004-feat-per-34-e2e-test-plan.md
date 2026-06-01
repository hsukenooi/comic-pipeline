---
title: "feat: PER-34 E2E Validation — Static Checks and Manual Test Checklist"
type: feat
status: active
date: 2026-05-19
---

# feat: PER-34 E2E Validation — Static Checks and Manual Test Checklist

**Target repo:** gixen-cli

## Overview

After PER-31 (ebay-fetch to comic-pipeline/apps/ebay), PER-32 (ezship-cli to comic-pipeline/apps/ezship), and PER-33 (comic skills to comic-pipeline/skills/), verify that the full /comic:buy orchestration chain has no path regressions. Deliver a static validation script (automatable) plus a manual E2E test checklist document for the live session verification.

## Problem Frame

Three migrations changed the paths that the /comic: skill files reference. A path regression anywhere in the chain would cause a skill to silently fail at runtime (wrong directory, missing script, wrong config key). The goal is to catch those regressions before relying on the workflow in production.

## Requirements Trace

- R1. Static check: all 9 skill files exist in `comic-pipeline/skills/`
- R2. Static check: Brain vault symlink resolves to `comic-pipeline/skills/`
- R3. Static check: no stale path references remain in any skill file
- R4. Static check: referenced tool paths (`apps/ebay/src/ebay_fetch.py`, `apps/ezship/src/cli.ts`) exist at the new locations
- R5. Static check: ebay-fetch `config.json` key names used in `grade.md` match the actual `load_config()` implementation
- R6. Manual checklist: documented steps for a live /comic:identify and /comic:buy dry-run

## Scope Boundaries

- No live eBay API calls, Gixen login, or LOCG requests in the automated checks
- No credential validation (verifying that actual credentials work requires a live session)
- No changes to skill logic — validation only
- Does not cover ezship-add live submission (requires active shipment)

## Key Technical Decisions

- **pytest for static checks, not shell**: pytest integrates with the existing `tests/` suite in gixen-cli, provides clear failure reporting, and is already configured in pyproject.toml.
- **Relative path resolution from HOME**: the skill files use `~/Projects/` paths. Tests expand these via `Path.home()`, not hardcoded `/Users/hsukenooi`.
- **Skip live-only assertions**: mark checks that require credentials with `pytest.mark.integration` so they can be excluded from CI.
- **Checklist as a docs/ markdown file**: gives the manual tester a browser-readable checklist that survives as a record of what was validated.

## Implementation Units

- [ ] **Unit 1: Static validation test file**

**Goal:** Automated checks for all post-migration path and structural invariants

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** None (reads filesystem state only)

**Files:**
- Create: `tests/test_skill_migration.py`

**Approach:**
- Expand `~/Projects/comic-pipeline/skills/` via `Path.home()` and assert all 9 skill files exist
- Verify that `~/Projects/Brain v3.0/.claude/commands/comic` is a symlink (not a directory) and resolves to `comic-pipeline/skills/`
- Read each skill file and grep for stale path strings (`ebay-cli`, `ebay-sniper`, `ezship-cli`, `Brain v3.0`)
- Verify `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` exists
- Verify `~/Projects/comic-pipeline/apps/ezship/src/cli.ts` exists
- Verify `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` contains `cfg.get("client_id")` (matching what grade.md expects from the config)
- No mocking — reads real filesystem state

**Patterns to follow:**
- `tests/test_gixen_client.py` — uses `pytest`, `pathlib.Path`, straightforward assertions
- `tests/test_server_db.py` — pattern for fixture-free file-system assertions

**Test scenarios:**
- Happy path: all 9 expected skill files exist in `comic-pipeline/skills/` → assertions pass
- Happy path: symlink at Brain vault path resolves to the skills directory → `os.path.islink()` and `os.readlink()` confirm target
- Edge case: stale path grep finds zero matches across all skill files → no `ebay-cli`, `ebay-sniper`, `ezship-cli`, `Brain v3.0` strings remain
- Happy path: `ebay_fetch.py` exists at new path and contains `client_id` key reference → confirms credential key alignment with grade.md
- Edge case: if a test finds a missing file, the failure message includes the expected path for easy diagnosis

**Verification:**
- `pytest tests/test_skill_migration.py` passes with zero failures
- No `pytest.skip()` calls triggered for any of the static checks

---

- [ ] **Unit 2: Manual E2E test checklist document**

**Goal:** Documented checklist for the live /comic:buy session verification a human must perform

**Requirements:** R6

**Dependencies:** Unit 1 (static checks must pass before running live session)

**Files:**
- Create: `docs/per-34-manual-test-checklist.md`

**Approach:**
- Document the pre-flight checks (env vars, server up, credentials set)
- Step-by-step trace of the full /comic:buy chain: identify → collection-check → grade (conditional) → fmv → snipe-add
- Include the specific commands to run at each step
- Include what to verify at each gate (e.g., ebay_fetch.py returns valid JSON, FMV table appears, snipe confirms in Gixen)
- Mark steps that require active eBay auction URLs (tester supplies these)
- Include a "results" table where the tester fills in pass/fail per step

**Test scenarios:**
- Test expectation: none — this is a documentation-only unit with no behavioral code

**Verification:**
- Document exists at `docs/per-34-manual-test-checklist.md`
- All five skill steps (identify, collection-check, grade, fmv, snipe-add) have documented verification criteria

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `Brain v3.0` path has a space — shell globs may mishandle it | Use `pathlib.Path` throughout, not shell expansion |
| `os.readlink()` returns the raw symlink target string which may differ from the canonical path | Resolve both paths with `Path.resolve()` before comparing |
| Tests run on a machine where `comic-pipeline` is not checked out | Assert at test start and skip with a clear message if `~/Projects/comic-pipeline` does not exist |

## Sources & References

- PER-31: apps/ebay migration
- PER-32: apps/ezship migration
- PER-33: comic skills move + symlink
- Related plan: `docs/plans/2026-05-19-003-feat-move-comic-skills-to-comic-pipeline-plan.md`
