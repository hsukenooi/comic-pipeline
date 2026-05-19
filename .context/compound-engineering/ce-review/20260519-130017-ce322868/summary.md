# CE Review Run — 20260519-130017-ce322868

**Branch:** feat/fmv-schema-split  
**Base:** main  
**Date:** 2026-05-19  
**Mode:** autofix

## Reviewers Dispatched

- correctness-reviewer
- testing-reviewer
- maintainability-reviewer
- data-migrations-reviewer
- adversarial-reviewer
- kieran-python-reviewer

## Applied Fixes (safe_auto)

| Severity | File | Fix |
|----------|------|-----|
| P0 | `db.py:231` | None guard for `survivor_ids` before DELETE to prevent silent data loss if row_factory misconfigured |
| P2 | `db.py:107-121` | Replaced O(n×m) `id_to_survivor` inner loop with O(n) dict lookup via `survivor_key_map` |
| P2 | `db.py:362` | Fixed `grade: float` annotation to `grade: float | None` (function accepts and raises on None) |
| P3 | `db.py:271,285` | Added `IF NOT EXISTS` to `CREATE TABLE fmv` and `CREATE TABLE bid_fmvs` in migration rebuild |
| P3 | `db.py:221-225` | Fixed `junction_inserted` counter to use `cur.rowcount` (INSERT OR IGNORE returns 0 on conflict) |
| P2 | `tests/test_gixen_overlay_db.py:431` | Strengthened `test_migration_is_idempotent` to assert fmv row count after second `create_tables` |
| P3 | `tests/test_gixen_overlay_routes.py:156` | Changed `>= 1` to `== 1` in `test_extract_comics_links_unlinked_bid` assertions |

## Residual Findings (gated/manual — not auto-applied)

| Severity | Class | Finding |
|----------|-------|---------|
| P0/P1 | manual | `id_to_survivor.get(..., row["id"])` fallback in migration creates orphan fmv rows pointing to deleted comic_ids — requires raising RuntimeError (behavior change) |
| P1 | manual | Crash gate only detects one mid-migration state; states between DROP fmv and RENAME comics are undetected |
| P2 | gated_auto | bid_comics migration lacks ORDER BY — is_primary can be lost on INSERT OR IGNORE ordering ambiguity |
| P2 | gated_auto | `locg_id` unconditionally overwritten in locg-link route; should COALESCE |
| P2 | gated_auto | Missing test scenarios for several edge cases (grade-only stub, lot migration, no-grade extract) |

## Test Results

51 passed, 0 failed after autofix application.
