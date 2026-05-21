---
title: "refactor: Drop comics.year NOT NULL — Decouple Year from Primary Identity"
type: refactor
status: active
date: 2026-05-21
origin: Linear PER-98
---

# Drop comics.year NOT NULL — Decouple Year from Primary Identity

## Overview

Make `comics.year` optional metadata rather than part of primary identity. `(title, issue)` becomes the practical join key for raw eBay comics; `year` fills in opportunistically (parser, LOCG, manual) but its absence never blocks linking. Migrate the existing schema, change `upsert_comic` to handle NULL-year inserts with reconciliation, and remove the year-required gate from `/api/extract-comics`.

## Problem Frame

Today, `comics.year` is `NOT NULL` and is part of `UNIQUE(title, issue, year)`. To satisfy that constraint when eBay titles omit the year, PER-70 added an LOCG year-lookup fallback. LOCG sits behind Cloudflare Turnstile (PER-83), and the Playwright fingerprinting workaround is failing today — so the dashboard is hostage to a runtime dependency chain that exists only to fill in a column that most callers don't need.

In practice, the reboot-collision case (X-Men 1963 vs 1991) is rare for the kinds of raw comics that show up unlinked in this dataset, and when it does matter we already have a stronger discriminator (`locg_id` / `locg_variant_id`). The cost of keeping `year NOT NULL` is paid every link cycle; the benefit accrues only to a small slice of edge-case comics.

## Requirements Trace

- **R1.** `comics.year` accepts `NULL` after migration; existing yeared rows are preserved.
- **R2.** Two partial unique indexes enforce identity: one for `(title, issue, year)` when year is set, one for `(title, issue)` when year is NULL.
- **R3.** `upsert_comic(year=None)` works without raising, returning a stable id for repeat calls with the same `(title, issue)`.
- **R4.** When a NULL-year row later sees a yeared `upsert_comic` for the same `(title, issue)`, it reconciles: either promotes in-place (no yeared row exists yet) or merges into the existing yeared row (yeared row wins, FK children move over).
- **R5.** When an upsert with `year=None` finds an existing yeared row for the same `(title, issue)`, it returns that yeared row's id and does not create a NULL-year shadow.
- **R6.** `/api/extract-comics` no longer skips bids with `reason: "no year extracted (locg fallback failed)"`. LOCG year resolution stays as opportunistic enrichment, not a gate.
- **R7.** All 18 currently-unlinked bids on the mini link cleanly (modulo titles with no issue number); `/comics` shows cond + fmv on every linkable row.
- **R8.** Migration is idempotent, re-entrant on crash, and follows the established Python-memory + SAVEPOINT-safe pattern from `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md`.
- **R9.** Reconciliation never silently discards priced FMV data. On `(comic_id, grade)` collision during a merge, the row carrying real prices (`low IS NOT NULL`) wins regardless of which side is yeared; collisions where both or neither carry prices preserve the survivor (yeared row). When the yeared row is a stub (`low IS NULL`) and the null row carries prices, the transplant is **per-column COALESCE** — non-null survivor columns (e.g., a previously-set `high` or `comps`) are never overwritten by null inbound columns; only columns where the survivor is null take the inbound value. Every merge that discards a row logs a `WARNING` with the discarded row's id, grade, and reason — so unexpected data loss is auditable.
- **R10.** Reconciliation never silently merges a reboot. If a `upsert_comic(year=Y)` call arrives and there is a NULL-year row plus any yeared row at the same `(title, issue)` with a year other than Y, the call raises `ReconciliationConflictError` instead of promoting the NULL row — the situation requires manual disambiguation (the existence of a different-year row is a reboot signal).

## Scope Boundaries

- **Not in scope:** Backfilling NULL years from LOCG or other sources — that becomes a separate value-add task once PER-83 is unblocked.
- **Not in scope:** Changes to the dashboard JSON shape. `_build_comics_row` in `plugins/gixen-overlay/src/gixen_overlay/routes.py` already does not surface `year` to the client; no UI change is needed.
- **Not in scope:** Removing or rewriting `resolve_year_and_locg` (PER-70). It stays, just becomes optional.
- **Not in scope:** Multi-process write safety. `gixen-overlay` is single-process under the host; reconciliation does not need an SQLite write lock beyond what the host SAVEPOINT and per-call commit already provide.

### Deferred to Separate Tasks

- **PER-83 follow-up:** Once LOCG/CF works again, opportunistic year backfill for NULL rows can be added behind a sweeper endpoint. Not blocking this plan.
- **PER-70 simplification:** Removing the now-optional LOCG fallback call from `/api/extract-comics` can wait until we see whether it's still useful in practice.
- **Title-string normalization (acknowledged limitation):** The new `idx_comics_ti_null` treats `"Amazing Spider-Man"` and `"The Amazing Spider-Man"` as distinct identities. The yeared regime already had this exposure; this plan elevates `(title, issue)` to primary identity in the NULL case, where the exposure carries more weight. Mitigation (e.g., a `normalized_title` column or insert-time lower/strip-prefix normalization) is out of scope here — it's an orthogonal identity-quality improvement that should land as its own ticket, ideally with a backfill pass over the existing yeared rows. Until then: post-migration `/api/extract-comics` runs may create NULL-shadow rows for comics that exist yeared under a different `title` string; these will not collapse via the short-circuit until a normalized identity layer exists.

## Context & Research

### Relevant Code and Patterns

- `plugins/gixen-overlay/src/gixen_overlay/db.py` — owns the schema, `_migrate_fmv_split`, `upsert_comic`, `upsert_fmv`, `list_comics`. The migration pattern from `_migrate_fmv_split` is the template: gated by `PRAGMA table_info`, crash-recovery guard, raw `conn.execute()` only, Python-memory FK-children pattern.
- `plugins/gixen-overlay/src/gixen_overlay/models.py` — `UpsertComicRequest.year: int` needs to become `int | None`.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py:375-387` — year resolution in `api_extract_comics`; the skip branch this plan removes is the `if primary_resolution is None` block at lines 381-386.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py:122-127` — `api_link_locg` copies `primary["year"]` when auto-upserting a lot-issue row. Becomes a NULL-pass-through.
- `plugins/gixen-overlay/tests/test_gixen_overlay_db.py:330-490` — fmv-split migration tests are the model for the new migration's coverage (shadow-row collapse, crash recovery, idempotency).

### Institutional Learnings

- `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md` — the mandatory pre-read. Three rules carry over verbatim:
  1. Save FK-child rows (`fmv`, `bid_fmvs`) to Python memory before `ALTER TABLE comics RENAME`. SQLite 3.26+ rewrites FK references to follow the rename, which would block `DROP TABLE comics_old`.
  2. Inside the host-managed SAVEPOINT, use only raw `conn.execute()`. Never call `upsert_comic`/`upsert_fmv` — they call `conn.commit()` which destroys the host's SAVEPOINT and makes rollback a no-op.
  3. `PRAGMA foreign_keys=OFF` is silently ignored inside a SAVEPOINT; do not rely on it.
- `docs/solutions/database-issues/plugin-owned-read-endpoints-cross-repo-2026-05-19.md` — orthogonal, but a reminder that plugin-owned schema changes don't need coordination with the host repo.

### External References

- SQLite partial indexes: `CREATE UNIQUE INDEX ... WHERE <predicate>` is supported and is the right primitive for the dual-shape uniqueness here.
- SQLite 3.35+ `ON CONFLICT(col, col) WHERE <predicate>` allows UPSERT against a partial unique index by naming the index's predicate. Python 3.11 ships with sqlite ≥3.37; the macOS system Python sqlite is also ≥3.39. Safe to use.

## Key Technical Decisions

- **Two partial unique indexes, not a NULL-safe single index.** SQLite treats NULLs as distinct in `UNIQUE`, so `UNIQUE(title, issue, year)` would silently allow infinite `(title, issue, NULL)` duplicates. The partial-index pair (`WHERE year IS NULL` and `WHERE year IS NOT NULL`) gives clean enforcement in both regimes with no app-level workarounds for the common case. Rationale: keeps dedup logic in the database where it belongs and lets `ON CONFLICT` do the work.
- **Reconciliation in `upsert_comic`, not a separate sweeper.** Resolving NULL→year transitions at write time (when we already know the new year) is cheaper and avoids a stale-NULL-row class of bug. The alternative (write NULL rows freely, sweep later) creates a window where the same comic has two ids, which complicates `bid_fmvs` linkage. Reasoning: write-path is already the natural place to converge on identity.
- **Yeared row wins identity; richer FMV row wins data, per column.** On a NULL ↔ yeared merge, the yeared row is the *identity* survivor (it has stronger identity for the eventual reboot case). But on a per-grade `fmv` collision, the row carrying real prices (`low IS NOT NULL`) wins — even if that's the NULL row's fmv. Mechanism: when the yeared row's fmv at the colliding grade has `low IS NULL` and the NULL row's fmv has `low IS NOT NULL`, COALESCE each priced column (`low`, `high`, `comps`, `confidence`, `notes`, `updated_at`) into the yeared row's fmv — survivor's non-null columns are preserved, the inbound row's values fill in nulls. Then delete the NULL row's fmv. Rationale: yeared-wins-always silently destroys priced FMV when a yeared row was created earlier via `api_link_locg`'s empty-stub auto-create; per-row transplant overwrites partial data set by earlier targeted `upsert_fmv` calls. Per-column COALESCE handles both. Every discard logs a `WARNING` so unexpected data loss is visible.
- **Promote in place when possible, but detect reboots first.** When a NULL-year row exists for `(title, issue)` and an inbound `upsert_comic(year=Y)` arrives, check for *any* yeared row at `(title, issue)` before promoting — not just one at year Y. If a yeared row exists at a different year (Y' ≠ Y), that is a reboot signal (X-Men 1963 vs 1991): raise `ReconciliationConflictError` instead of promoting. The caller must disambiguate manually. If no yeared row exists at any year, `UPDATE comics SET year=? WHERE id=<null-row-id>` reuses the existing `comic_id` and preserves FK children. Rationale: silently promoting a NULL row to year Y when a yeared row at Y' already exists would leave two yeared rows for the same logical comic, breaking the identity invariant.
- **Year=None lookup short-circuits to yeared row when one exists.** Calling `upsert_comic(title, issue, year=None)` when `(title, issue, <some-year>)` already exists returns the yeared row's id rather than creating a NULL shadow. Rationale: avoids creating an inferior row when we already have a better identity. Costs nothing extra: a `SELECT id FROM comics WHERE title=? AND issue=? AND year IS NOT NULL` runs first; if it hits, return.
- **No data dedup is needed in the migration itself.** Every existing row has a year (NOT NULL today), so the new `idx_comics_tiy_yes` partial index covers exactly the same shape as the old `UNIQUE(title, issue, year)`. The migration is purely schema-level — no row collapse needed.
- **Reuse the `_migrate_fmv_split` Python-memory machinery.** A new `_migrate_year_nullable` function follows the same pattern: gate, save FK children to Python lists, rename + rebuild, drop old, recreate children, restore.
- **Use a distinct intermediate table name (`comics_old_ynull`), not `comics_old`.** `_migrate_fmv_split` already owns the `comics_old` name in its crash-recovery gate (`db.py:75-82`). If `_migrate_year_nullable` crashed between `ALTER TABLE ... RENAME` and `DROP TABLE`, restart would hit `_migrate_fmv_split`'s gate first, raising its own (now misleading) `"comics has no grade column"` error. A distinct intermediate name keeps each migration's crash signal independent and operator error messages accurate.

## Open Questions

### Resolved During Planning

- **Q: Do we need to handle multi-process writes?** No — `gixen-overlay` runs single-process under the host. The reconciliation logic in `upsert_comic` runs within a single `conn.execute(...)` + `conn.commit()` cycle and is safe.
- **Q: Does the dashboard surface year?** No — `_build_comics_row` does not include `year` in the response. Verified by reading `plugins/gixen-overlay/src/gixen_overlay/routes.py:235-263`. No UI change required.
- **Q: Does SQLite support `ON CONFLICT` against partial unique indexes?** Yes, from 3.35+ (technically 3.24 for partial-index upsert in some forms, 3.35 widened the syntax). Both Python 3.11+ and the macOS system Python ship with new-enough sqlite.

### Deferred to Implementation

- **Q: Should the NULL-row promotion `UPDATE` also merge `locg_id` from the inbound call?** Likely yes (use `COALESCE` like the current upsert), but final SQL shape is best decided when wiring the merge branch in `upsert_comic`.
- **Q: Does the LOCG opportunistic call in `api_extract_comics` still earn its keep once the year-required gate is gone?** Defer; can be measured after the gate is removed. Plan keeps it as opportunistic enrichment.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

**Schema shape after migration:**

    comics
      id              PK
      title           TEXT NOT NULL
      issue           TEXT NOT NULL
      year            INTEGER NULL          -- was NOT NULL
      locg_id         INTEGER NULL
      locg_variant_id INTEGER NULL
      created_at      TEXT
      -- UNIQUE(title, issue, year) is REMOVED from the table definition.

    UNIQUE INDEX idx_comics_tiy_yes  ON comics(title, issue, year) WHERE year IS NOT NULL
    UNIQUE INDEX idx_comics_ti_null  ON comics(title, issue)       WHERE year IS NULL

**`upsert_comic(title, issue, year, ...)` decision tree:**

    if year is None:
        existing_yeared = SELECT id FROM comics WHERE title=? AND issue=? AND year IS NOT NULL LIMIT 1
        if existing_yeared:
            merge locg fields into existing_yeared (COALESCE), return existing_yeared.id
        else:
            INSERT ... ON CONFLICT(title, issue) WHERE year IS NULL DO UPDATE SET
                locg_id = COALESCE(excluded.locg_id, locg_id), ...
            return id
    else:  # year is set; call it Y
        existing_null            = SELECT id FROM comics WHERE title=? AND issue=? AND year IS NULL LIMIT 1
        existing_yeared_for_Y    = SELECT id FROM comics WHERE title=? AND issue=? AND year=Y LIMIT 1
        other_yeared             = SELECT id, year FROM comics WHERE title=? AND issue=? AND year IS NOT NULL AND year != Y LIMIT 1

        # Reboot guard (R10): if any other-year row exists, refuse to merge or promote.
        if other_yeared and existing_null:
            raise ReconciliationConflictError(
                f"NULL-year row exists alongside yeared row for {(title, issue)} at year {other_yeared.year}; "
                f"inbound year {Y} would create a second yeared identity — manual disambiguation required."
            )

        if existing_null and not existing_yeared_for_Y:
            # Promote in place — preserves FK children, no other yeared row exists.
            UPDATE comics SET year=Y, locg_id=COALESCE(?, locg_id), ... WHERE id=existing_null.id
            return existing_null.id

        elif existing_null and existing_yeared_for_Y:
            # Merge into yeared survivor. Yeared row keeps identity; FMV ROW WITH PRICES WINS PER GRADE (R9).
            for null_fmv in SELECT id, grade, low, high, comps, confidence, notes, updated_at
                            FROM fmv WHERE comic_id = existing_null.id:
                yeared_fmv = SELECT id, low FROM fmv WHERE comic_id = existing_yeared_for_Y.id AND grade = null_fmv.grade
                if yeared_fmv is None:
                    # No collision — reparent the null row's fmv.
                    UPDATE fmv SET comic_id=existing_yeared_for_Y.id WHERE id=null_fmv.id
                elif yeared_fmv.low IS NULL and null_fmv.low IS NOT NULL:
                    # Yeared stub vs NULL-row priced fmv → per-column COALESCE so a
                    # non-null yeared column (e.g., a previously-set high) is preserved.
                    UPDATE fmv SET
                        low        = COALESCE(low,        null_fmv.low),
                        high       = COALESCE(high,       null_fmv.high),
                        comps      = COALESCE(comps,      null_fmv.comps),
                        confidence = COALESCE(confidence, null_fmv.confidence),
                        notes      = COALESCE(notes,      null_fmv.notes),
                        updated_at = COALESCE(updated_at, null_fmv.updated_at)
                        WHERE id=yeared_fmv.id
                    DELETE FROM fmv WHERE id=null_fmv.id   # also drops bid_fmvs via ON DELETE CASCADE
                    logger.warning("upsert_comic merge: transplanted prices from null-row fmv id=%d into yeared-row fmv id=%d (grade=%s)",
                                   null_fmv.id, yeared_fmv.id, null_fmv.grade)
                else:
                    # Yeared row already has prices (or both lack prices). Yeared wins.
                    DELETE FROM fmv WHERE id=null_fmv.id
                    if null_fmv.low is not None:
                        logger.warning("upsert_comic merge: discarded null-row priced fmv id=%d (grade=%s) — yeared row id=%d already had prices",
                                       null_fmv.id, null_fmv.grade, yeared_fmv.id)
            merge locg fields into existing_yeared_for_Y (COALESCE)
            DELETE FROM comics WHERE id=existing_null.id
            return existing_yeared_for_Y.id

        elif other_yeared and not existing_null:
            # Other-year row exists but no NULL row to reconcile — let the regular ON CONFLICT path
            # create or update the (title, issue, Y) row; the other-year row stays as a sibling
            # (reboot). This is the existing yeared-regime behavior, unchanged.
            INSERT ... ON CONFLICT(title, issue, year) WHERE year IS NOT NULL DO UPDATE SET
                locg_id = COALESCE(excluded.locg_id, locg_id), ...
            return id

        else:
            INSERT ... ON CONFLICT(title, issue, year) WHERE year IS NOT NULL DO UPDATE SET
                locg_id = COALESCE(excluded.locg_id, locg_id), ...
            return id

**Migration flow (mirrors `_migrate_fmv_split`):**

1. Gate: if `comics.year` is already nullable → check for `comics_old_ynull` (crash recovery, raise) else return.
2. `saved_fmv = SELECT * FROM fmv` to Python list.
3. `saved_bid_fmvs = SELECT * FROM bid_fmvs` to Python list.
4. `DROP TABLE bid_fmvs; DROP TABLE fmv;` (sever FK children).
5. `ALTER TABLE comics RENAME TO comics_old_ynull`.
6. `CREATE TABLE comics (...)` with `year INTEGER` (no NOT NULL, no inline UNIQUE).
7. `INSERT INTO comics SELECT * FROM comics_old_ynull`.
8. `DROP TABLE comics_old_ynull`.
9. `CREATE TABLE IF NOT EXISTS fmv (...) REFERENCES comics(id)` (same shape).
10. `CREATE TABLE IF NOT EXISTS bid_fmvs (...)` (same shape).
11. Restore `fmv` rows preserving `id`.
12. Restore `bid_fmvs` rows.
13. Recreate `idx_fmv_comic` and `idx_bid_fmvs_bid`.
14. Create the two partial unique indexes on `comics`.

## Implementation Units

- [ ] **Unit 1: Schema migration — drop NOT NULL, install partial unique indexes**

**Goal:** Recreate the `comics` table with `year INTEGER` (nullable) and install the two partial unique indexes. Migration is idempotent and re-entrant.

**Requirements:** R1, R2, R8.

**Dependencies:** None.

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py` (extend `create_tables`; add `_migrate_year_nullable` next to `_migrate_fmv_split`)
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_db.py`

**Approach:**
- Add `_migrate_year_nullable(conn)` called from `create_tables` after `_migrate_fmv_split`.
- Gate by inspecting `PRAGMA table_info(comics)` — if `year` is already nullable (`notnull == 0`), check for `comics_old_ynull` (raise if present, return otherwise). Use the `_ynull` suffix to avoid collision with `_migrate_fmv_split`'s `comics_old` crash-recovery gate (`db.py:75-82`).
- Follow Python-memory pattern: save `fmv` and `bid_fmvs` to Python lists, drop them, rename `comics → comics_old_ynull`, create new `comics` with nullable year and no inline `UNIQUE`, copy rows, drop `comics_old_ynull`, recreate `fmv` and `bid_fmvs` with `IF NOT EXISTS`, restore rows preserving ids, recreate indexes.
- Inside the gate body, write the two partial unique indexes (`idx_comics_tiy_yes`, `idx_comics_ti_null`).
- Top-level `create_tables` for fresh DBs: update the inline `CREATE TABLE comics` to drop the inline `UNIQUE(title, issue, year)` and `NOT NULL` on year, and add the two `CREATE UNIQUE INDEX IF NOT EXISTS` statements unconditionally.
- Hard rule: raw `conn.execute()` only. No `upsert_comic`, no `conn.commit()`.

**Execution note:** Read `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md` before touching this code. The three footguns in that doc are the failure modes most likely to hit here.

**Patterns to follow:**
- `_migrate_fmv_split` in `plugins/gixen-overlay/src/gixen_overlay/db.py:62-318` — gate, save-to-memory, rename, recreate, restore, index.

**Test scenarios:**
- *Happy path:* Migrate a DB seeded with yeared rows + fmv + bid_fmvs; assert all rows survive with original ids, partial unique indexes exist, `year` column has `notnull = 0`.
- *Idempotency:* Call `create_tables` twice in a row; assert no errors, row counts unchanged, schema unchanged.
- *Crash recovery:* Seed a DB with post-migration `comics` schema **plus** a leftover `comics_old_ynull` table; assert `create_tables` raises `RuntimeError` matching `"crashed mid-migration"`.
- *Crash recovery (distinct from `_migrate_fmv_split`):* Seed a DB with post-migration `comics` schema **plus** a leftover `comics_old` table (the fmv-split's intermediate name); assert this does NOT trigger `_migrate_year_nullable`'s gate — it belongs to the other migration.
- *Edge case (empty fmv):* Migrate a DB with `comics` rows but zero `fmv` and zero `bid_fmvs` rows; assert migration completes and tables are recreated.
- *Partial-index enforcement:* After migration, insert two rows with same `(title, issue, NULL)`; assert `IntegrityError`. Insert two rows with same `(title, issue, 1988)`; assert `IntegrityError`. Insert `(ASM, 300, NULL)` and `(ASM, 300, 1988)`; assert both succeed (different partial indexes).

**Verification:**
- `PRAGMA table_info(comics)` shows `year` with `notnull = 0`.
- `PRAGMA index_list(comics)` includes `idx_comics_tiy_yes` and `idx_comics_ti_null` as unique partial indexes.
- All pre-existing yeared rows survive with original ids; FK children intact.

---

- [ ] **Unit 2: `UpsertComicRequest.year` becomes optional**

**Goal:** Pydantic model accepts `year=None`.

**Requirements:** R3.

**Dependencies:** None (parallel with Unit 1; not coupled).

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/models.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py` (if a model-validation test exists; otherwise covered by Unit 4 route tests)

**Approach:**
- Change `year: int` to `year: int | None = None`.

**Test scenarios:**
- *Happy path:* `UpsertComicRequest(title="X-Men", issue="1")` validates; `year` is `None`.
- *Happy path:* `UpsertComicRequest(title="X-Men", issue="1", year=1963)` still validates.
- *Edge case:* `year=0` validates (no implicit truthiness check should reject it).

**Verification:**
- Model round-trips with and without `year`.

---

- [ ] **Unit 3: `upsert_comic` reconciliation logic**

**Goal:** `upsert_comic(year=None)` and the NULL↔yeared transitions work without orphans, duplicate `(title, issue, year)` rows, silent FMV data loss, or silent reboot merges.

**Requirements:** R3, R4, R5, R9, R10.

**Dependencies:** Unit 1 (schema must be in place).

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py` (`upsert_comic` signature and body)
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_db.py`

**Approach:**
- Change signature to `year: int | None`.
- Define a new module-level exception `ReconciliationConflictError(RuntimeError)` in `db.py`. Used by the reboot guard (R10) and re-raised to FastAPI as a 409.
- Implement the decision tree in the High-Level Technical Design section. Five branches: (1) `year=None` + yeared row exists → short-circuit; (2) `year=None` + no yeared → INSERT-or-update NULL-row; (3) reboot guard raises when NULL + other-year coexist; (4) promote-in-place when only a NULL row exists; (5) merge NULL → yeared with priced-fmv-wins-per-grade rule.
- **Merge path FMV collision handling (R9):** for each NULL-row fmv, SELECT the yeared row's fmv at the same grade. If absent → reparent via `UPDATE fmv SET comic_id`. If present with `yeared.low IS NULL` and `null.low IS NOT NULL` → transplant the priced columns (`low`, `high`, `comps`, `confidence`, `notes`, `updated_at`) into the yeared row's fmv via UPDATE, then DELETE the NULL row's fmv. If present and yeared has prices (or both lack prices) → DELETE the NULL row's fmv; if it carried prices, log `WARNING` with both fmv ids and grade. `bid_fmvs` rows referencing any deleted fmv cascade-delete via the existing FK.
- **Reboot guard (R10):** before promoting or merging, SELECT `id, year FROM comics WHERE title=? AND issue=? AND year IS NOT NULL AND year != Y LIMIT 1`. If a row comes back AND a NULL row also exists, raise `ReconciliationConflictError` with a message naming both years. Callers (`api_extract_comics`, `api_link_locg`, `api_upsert_comic`) translate this to a 409 response or a `skipped` entry so the user can disambiguate.
- Wrap the merge sequence so it appears atomic from the caller's perspective: do all `UPDATE`/`DELETE`/`INSERT` calls before the trailing `conn.commit()`. Do not commit mid-merge. **Transaction model:** Python's `sqlite3` module under its default `isolation_level=""` opens an implicit deferred transaction on the first DML and ends it on `conn.commit()`. The multi-statement merge is therefore atomic from the caller's perspective as long as no intermediate `conn.commit()` runs and the implementation does not switch to autocommit mode. A `try/except IntegrityError` block to catch the `(comic_id, grade)` collision is fine within this transaction — only the failing statement is aborted, not the whole transaction (deferred-isolation semantics). Do not use an explicit `BEGIN/COMMIT` block here — it would conflict with the existing deferred-transaction model used elsewhere in `db.py`.
- Use `INSERT ... ON CONFLICT(title, issue, year) WHERE year IS NOT NULL DO UPDATE` and `INSERT ... ON CONFLICT(title, issue) WHERE year IS NULL DO UPDATE` to target the partial indexes.

**Test scenarios:**
- *Happy path:* `upsert_comic("ASM", "300", year=1988)` twice → same id, no duplicate row.
- *Happy path:* `upsert_comic("ASM", "300", year=None)` twice → same id, single NULL-year row.
- *R5 short-circuit:* `upsert_comic("ASM", "300", year=1988)` then `upsert_comic("ASM", "300", year=None)` → returns the yeared row's id; no NULL row is created.
- *R4 promotion:* `upsert_comic("ASM", "300", year=None)` → returns id N. Then `upsert_comic("ASM", "300", year=1988)` → returns same id N; row's year is now 1988; only one row exists.
- *R4 promotion preserves fmv:* `upsert_comic("ASM", "300", year=None)` → id N; `upsert_fmv(N, grade=9.2, low=800)`; then `upsert_comic("ASM", "300", year=1988)` → returns N; the fmv row at grade 9.2 with low=800 is intact.
- *R4 merge — no fmv collision:* Insert NULL-year row id A with `fmv` at grade 9.2; separately insert yeared row id B for `(ASM, 300, 1988)` with `fmv` at grade 8.5. `upsert_comic("ASM", "300", year=1988)` (or any call that triggers reconciliation) → row A is gone, row B remains, B now has fmv at both 9.2 and 8.5, no orphan rows.
- *R4 + R9 merge — fmv collision, yeared has prices:* NULL row id A has fmv at grade 9.2 with `low=NULL`; yeared row id B for `(ASM, 300, 1988)` has fmv at grade 9.2 with `low=900`. After merge, B's 9.2 fmv keeps `low=900`; A's fmv is deleted (no warning, no priced data lost).
- *R4 + R9 merge — fmv collision, NULL row has prices, yeared stub:* NULL row id A has fmv at grade 9.2 with `low=800`; yeared row id B has fmv at grade 9.2 with `low=NULL` (empty stub created earlier by `api_link_locg`). After merge, B's 9.2 fmv now has `low=800` (transplanted from A); A's fmv is deleted; a `WARNING` log records the transplant.
- *R9 silent-discard guard:* NULL row id A has fmv at grade 9.2 with `low=800`; yeared row id B has fmv at grade 9.2 with `low=750`. After merge, B's 9.2 fmv keeps `low=750`; A's fmv is deleted; a `WARNING` log records that priced data was discarded with both ids and the grade.
- *R10 reboot guard:* Seed `(ASM, 300, 1987)` id=11 and `(ASM, 300, NULL)` id=10. Call `upsert_comic("ASM", "300", year=1988)`. Assert `ReconciliationConflictError` is raised; both rows are unchanged.
- *R10 reboot guard — same year is fine:* Seed `(ASM, 300, 1988)` id=11 and `(ASM, 300, NULL)` id=10. Call `upsert_comic("ASM", "300", year=1988)`. Assert merge proceeds normally; id=10 is gone, id=11 absorbs fmv children per R9.
- *R10 — no NULL row, other-year row OK:* Seed `(ASM, 300, 1987)` id=11 only. Call `upsert_comic("ASM", "300", year=1988)`. Assert a second yeared row `(ASM, 300, 1988)` is created (legitimate reboot sibling); 1987 row is untouched; no error.
- *Error path:* `upsert_comic("ASM", "300", year=None)` with an existing `bid_fmvs` row pointing at the NULL row's fmv, followed by promotion — assert `bid_fmvs` row still points at a valid fmv post-promotion (promotion preserves both comic_id and fmv_id).
- *Locg merge on short-circuit:* `upsert_comic("ASM", "300", year=1988)` then `upsert_comic("ASM", "300", year=None, locg_id=42)` → yeared row's `locg_id` becomes 42 (COALESCE behavior preserved).

**Verification:**
- No `(title, issue, NULL)` row exists when a `(title, issue, year)` row exists for the same key.
- After every documented call sequence, exactly one row matches each logical comic.
- All `fmv` rows reference a real `comics.id`; no FK orphans.
- Every `(comic_id, grade)` fmv pair where prices existed before reconciliation still has prices after — or a `WARNING` log explains where they went.
- Reboot conflicts surface as `ReconciliationConflictError`, never as a silent extra row.

---

- [ ] **Unit 4: `/api/extract-comics` drops the year-required skip path**

**Goal:** Bids whose eBay titles lack a year, and whose LOCG fallback fails, link cleanly with `year=NULL`.

**Requirements:** R6, R7.

**Dependencies:** Unit 3 (`upsert_comic(year=None)` must work).

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/routes.py` (`api_extract_comics`, around lines 375-398)
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- Keep the opportunistic `resolve_year_and_locg(series, issues[0])` call when `parsed.year is None` — its result, if any, populates `primary_resolution` and provides `locg_id`/`locg_variant_id`.
- Remove the `if primary_resolution is None: skip` branch.
- Pass `year=year` (which may be `None`) to `upsert_comic`. Existing `primary_resolution.year` assignment stays inside the `if primary_resolution` guard so `year` falls through as `None` when LOCG fails.
- **Multi-issue lots:** Preserve the current behavior — when LOCG resolves a year for the primary issue, that year is applied to all issues in a multi-issue lot (the existing `for idx, issue in enumerate(issues): ... year=year` loop). When LOCG fails, all issues in the lot get `year=None`. Rationale: a single eBay lot is typically a same-era run; spreading the resolved year across issues matches the most likely identity and aligns with the existing `locg_id` propagation to `idx == 0` only.
- **Reboot conflict handling (R10):** wrap the per-bid `upsert_comic` calls so a `ReconciliationConflictError` adds a `skipped` entry with `reason="reboot conflict (manual disambiguation required)"` and continues with the next bid. Do not let the exception fail the whole `/api/extract-comics` request.
- No change needed in `api_link_locg`: it already reads `primary["year"]` from the SELECT result, which will be `None` when the primary comic has no year. The downstream `upsert_comic` call (Unit 3) handles `year=None` natively. A `ReconciliationConflictError` here propagates as a 409 (Unit 5).

**Test scenarios:**
- *Happy path:* Seed a bid with an eBay title that parses to series+issue but no year; mock `resolve_year_and_locg` to return `None`; POST `/api/extract-comics`; assert the bid is `linked: 1` with no entry in `skipped`; assert a `comics` row exists with `year IS NULL`; assert a `bid_fmvs` row exists.
- *Happy path:* Same as above but mock `resolve_year_and_locg` to return a `LocgResolution(year=1988, locg_id=42, ...)`; assert `comics` row has `year=1988` and `locg_id=42`.
- *Edge case:* Seed two bids with title parses that yield the same `(series, issue)` and no year; assert both link to the same single `comics` row (NULL-year dedup via `idx_comics_ti_null`).
- *R7 acceptance:* Run extract-comics on a fixture that mirrors the 18 currently-unlinked bids; assert all 18 link cleanly (mock LOCG to fail uniformly to exercise the new path).
- *Regression — yeared path:* Existing test that exercises a yeared-title bid should still pass unchanged.
- *R10 caller behavior:* Seed `(ASM, 300, 1987)` and `(ASM, 300, NULL)`; seed a bid whose title parses to `(ASM, 300)` with no year and mock LOCG to return `year=1988`. POST `/api/extract-comics`; assert the bid lands in `skipped` with `reason` mentioning "reboot conflict"; assert the request returns 200 (other bids still processed).

**Verification:**
- `skipped` response array no longer contains `"no year extracted (locg fallback failed)"`.
- `comics` table accepts NULL-year inserts and dedups them across multiple `/api/extract-comics` runs.

---

- [ ] **Unit 5: `api_link_locg` regression check for NULL-year primary**

**Goal:** When a bid's primary fmv points at a NULL-year comic and an LOCG link arrives with `issue=` for a lot row, the auto-upsert of the lot-issue comic succeeds with `year=NULL`.

**Requirements:** R4 (the runtime side — NULL year propagated correctly through the existing route).

**Dependencies:** Unit 3.

**Files:**
- Read-only: `plugins/gixen-overlay/src/gixen_overlay/routes.py:122-127` — no change expected, but verified
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- The existing `upsert_comic(db, title=primary["title"], issue=req.issue, year=primary["year"])` call already passes through whatever `primary["year"]` is. With Unit 3, `None` is now a valid input.
- Wrap the call (and the earlier `upsert_comic` in the same handler) so a `ReconciliationConflictError` becomes a 409 response with the exception's message in the detail field. This is the only behavior change in this unit.
- If the SQL selecting `primary["year"]` from the dashboard JSON path uses a non-nullable coercion anywhere, fix it here. (None observed in current code.)

**Test scenarios:**
- *Happy path:* Seed a bid linked to a NULL-year primary comic + fmv; POST `/api/bids/<id>/comics/locg` with `{ "locg_id": 99, "issue": "302" }`; assert the route returns 200, a new `comics` row exists for `("ASM", "302", NULL)`, and the response body shows `year: null`.
- *R10 reboot conflict:* Seed `(ASM, 300, 1987)` and `(ASM, 300, NULL)` (with the bid linked to the NULL row); POST `/api/bids/<id>/comics/locg` against a scenario that forces an inbound year of 1988 on `(ASM, 300)`; assert the route returns 409 with the conflict message.

**Verification:**
- Route returns 200 in both yeared-primary and NULL-year-primary cases; no path raises a "year cannot be None" error.

---

- [ ] **Unit 6: Confirm dashboard handles NULL year (no-op verification)**

**Goal:** Spot-check that `/api/comics/snipes` and `/api/comics/history` JSON responses are unchanged by NULL-year comics being present.

**Requirements:** R7.

**Dependencies:** Units 1, 3, 4.

**Files:**
- Read-only: `plugins/gixen-overlay/src/gixen_overlay/routes.py:_build_comics_row`, `_COMICS_AGGREGATES`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- `_build_comics_row` does not surface `year`. `_COMICS_AGGREGATES` does not reference `comics.year`. No change expected — this unit exists to lock the assumption with a test.

**Test scenarios:**
- *Happy path:* Seed two bids — one linked to a yeared comic, one to a NULL-year comic, both with priced fmv. Hit `/api/comics/snipes`; assert both rows are present, both have `cond_grade`, `fmv_low`, `fmv_high`, `value_pct` populated, and the JSON shape is identical between them (no `year` field on either; no NULL surfacing).

**Verification:**
- Both dashboard endpoints return identical shapes regardless of `comics.year` value.

## System-Wide Impact

- **Interaction graph:** `upsert_comic` is called from `api_upsert_comic` (Unit 4 indirectly), `api_link_locg` (Unit 5), and `api_extract_comics` (Unit 4). All three become NULL-year tolerant after Unit 3.
- **Error propagation:** Reconciliation's merge path involves multiple `UPDATE`/`DELETE`/`INSERT` calls before commit. If any call raises (e.g., `IntegrityError` on the `fmv` move that we don't catch), the merge must abort cleanly — the caller will then see an exception and decide whether to retry. The existing `try/except` blocks in `api_extract_comics` (lines 408-410) and `api_link_locg` (which lets exceptions propagate to FastAPI) already handle this.
- **State lifecycle risks:** A partial merge (FK children moved, NULL row not yet deleted) would leave the schema in a recoverable but ugly state. Mitigation: ordering — move FK children first, then `DELETE` the NULL row last; if the delete fails, the next reconciliation pass converges. Worst case: an orphan `(title, issue, NULL)` row with no fmv children, which is harmless.
- **API surface parity:** `UpsertComicRequest.year` becoming `int | None` is a backwards-compatible API change (existing callers passing `year=int` still work). New callers can omit it.
- **Integration coverage:** The reconciliation merge is the highest-risk cross-layer behavior. Unit 3's "merge — no fmv collision" and "merge — fmv collision" tests must exercise it end-to-end against a real SQLite (`:memory:` connection with `foreign_keys=ON`), not mocked.
- **Unchanged invariants:**
  - `comics.id` is stable across all reconciliation paths. Existing `bid_fmvs.fmv_id → fmv.comic_id` references continue to resolve.
  - `fmv` schema is unchanged; `bid_fmvs` schema is unchanged.
  - The dashboard JSON shape (`_build_comics_row`) is unchanged.
  - `_migrate_fmv_split` is unchanged. The new `_migrate_year_nullable` runs after it and is independent.
  - `list_comics(year=None)` continues to mean "any year" (no filter on year), not "rows where year IS NULL". Existing callers see no behavior change. Querying explicitly for NULL-year rows requires raw SQL; no current caller needs this.

## Alternative Approaches Considered

- **Sentinel year value (e.g., `year=0` for "unknown") with the existing `UNIQUE(title, issue, year)` intact.** This would deliver R3–R7 with no schema migration, no partial indexes, and no reconciliation merge path. Rejected because (a) `year=0` leaks into every SQL filter (`WHERE year != 0`), `_build_comics_row` SQL aggregates, and any future year-range query — a sentinel needs to be remembered everywhere; (b) the NULL/NOT NULL distinction is more honest about "this field is genuinely absent" and surfaces cleanly in `_build_comics_row` if year is ever exposed; (c) reconciliation still has to exist (a year=0 row must eventually merge with the real-year row) — the sentinel only moves the same problem into a different shape. The complexity-saved-by-sentinel is mostly illusory once the merge logic is acknowledged as inherent to the problem.
- **Single NULL-safe `UNIQUE(title, issue, year)` with app-level dedup.** Rejected because SQLite treats NULL as distinct, so this is not enforceable in the DB — every `INSERT` path would need its own dedup pre-check. Centralizing in two partial indexes is cleaner.
- **Background sweeper instead of write-time reconciliation.** Rejected because (a) it widens the time window during which the same logical comic has two ids, complicating `bid_fmvs` consistency; (b) destructive side-effects (Unit 3's merge path) are easier to audit when triggered by an explicit `upsert_comic` call than by a periodic sweeper that runs unattended. Trade-off accepted: in exchange for write-time convergence, the `WARNING` logs added in R9 carry the audit-trail burden a sweeper would otherwise carry through its log output.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Migration crashes mid-rebuild, leaving `comics_old_ynull` orphaned | Crash-recovery gate at the start of `_migrate_year_nullable` raises `RuntimeError` if `comics_old_ynull` is present alongside a year-nullable `comics`; manual recovery only. Distinct intermediate name avoids colliding with `_migrate_fmv_split`'s `comics_old` gate. |
| `conn.commit()` inside `upsert_comic` destroys the host SAVEPOINT during the migration | The migration uses raw SQL only and never calls `upsert_comic`. CRUD helpers retain their existing `conn.commit()`. Enforced by code review and the pre-read doc warning. |
| `ON CONFLICT(...) WHERE ...` syntax against a partial index varies by SQLite version | Sticking to documented 3.35+ syntax; both target Pythons ship newer. If it fails, fall back to a manual SELECT-then-INSERT-or-UPDATE in `upsert_comic` (already viable given the existing pre-checks for promotion/merge). |
| Merge path leaves orphan `bid_fmvs` rows when an fmv is discarded due to collision | `bid_fmvs.fmv_id` has `ON DELETE CASCADE`; deleting the losing fmv automatically removes them. Verified by Unit 3 collision test. |
| Reconciliation logic is subtle; future readers misuse it | Inline comment in `upsert_comic` describing the five cases (year=None hit yeared, year=None new, reboot guard, promote, merge) and pointing at this plan. Plus Unit 3 tests as executable documentation. |
| LOCG fallback remains in `api_extract_comics` and now does work for no functional reason | Acceptable; deferred. Removing the call is cheap and can happen when we have data showing it never enriches in practice. |
| Title-string drift creates NULL-shadow rows post-migration (`"X-Men"` vs `"The X-Men"`) | Acknowledged as out-of-scope; tracked in "Deferred to Separate Tasks". The R9 warning log makes accidental duplicates noticeable in the operational log even before a normalization ticket lands. |
| `ReconciliationConflictError` (R10) surfaces in places callers don't expect | Caller-side: `api_extract_comics` swallows into `skipped`; `api_link_locg` / `api_upsert_comic` translate to a 409 with a clear message. Tests cover both. |

## Documentation / Operational Notes

- Update `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md` only if a new failure mode is discovered during implementation. The existing doc remains the canonical reference.
- After the migration runs on the dev DB, spot-check: `SELECT COUNT(*) FROM comics WHERE year IS NULL` should be 0 until `api_extract_comics` runs on a year-less title; row counts and fmv parity should match pre-migration snapshots.
- No rollback plan beyond restoring from the pre-migration DB snapshot. The migration is one-way once `comics_old` is dropped. Recommend the implementer snapshots the dev DB before running.

## Sources & References

- **Origin:** Linear PER-98 — "Drop comics.year NOT NULL — Decouple Year from Primary Identity"
- **Mandatory pre-read:** `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md`
- **Commit reference:** `7dfbe8f` — `docs: compound SQLite FK-follows-RENAME and SAVEPOINT migration constraints`
- **Template migration:** `plugins/gixen-overlay/src/gixen_overlay/db.py:_migrate_fmv_split` (lines 62-318)
- **Related Linear tickets:** PER-70 (LOCG year fallback — becomes opportunistic), PER-83 (CF Turnstile blocker — lower priority after this), PER-90 (bare-number issue extraction — shipped, complementary)
