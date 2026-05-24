---
title: "feat: Port FMV Schema Split to gixen-overlay Plugin"
type: feat
status: active
date: 2026-05-19
origin: docs/2026-05-13-comic-fmv-split.md
---

# feat: Port FMV Schema Split to gixen-overlay Plugin

**Target repos:** `comic-pipeline` (primary — all plugin work), `gixen-cli` (one-line prerequisite)

## Overview

The current `comics` table conflates identity and per-grade valuations under `UNIQUE(title, issue, year, grade)`. When an agent revises a grade after FMV has been researched, the upsert creates a new shadow row instead of updating the existing FMV — leaving bids linked to FMV-less rows (the 2026-05-13 incident). This plan ports the schema redesign already proven in `origin/mac-mini/fmv-split-work` into `comic-pipeline/plugins/gixen-overlay/`, which now owns all comic logic after PER-30.

## Problem Frame

The legacy schema makes "bid has a grade with no FMV row" structurally possible — a grade revision silently inserts a second `comics` row with `fmv_low=NULL` and some bids end up linked to FMV-less shadow rows (see origin doc §"The bug being fixed"). The fix is to split `comics` into three tables so that the FK itself guarantees the valuation row exists. The implementation is complete on `origin/mac-mini/fmv-split-work` (24 commits) but lives in gixen-cli, which is now comic-free. This plan moves it to the plugin where it belongs.

## Requirements Trace

- **R1.** `comics` holds identity only: `UNIQUE(title, issue, year)`, no `grade` or `fmv_*` columns.
- **R2.** `fmv` table holds per-grade valuations: `UNIQUE(comic_id, grade)`, FK to `comics`.
- **R3.** `bid_fmvs` junction `(bid_id, fmv_id, is_primary)` replaces `bid_comics`, with FKs to `bids` and `fmv`.
- **R4.** `bids.fmv_id` (nullable) replaces `bids.comic_id` as the bid-to-comic link.
- **R5.** A one-time data migration collapses shadow comics rows, manufactures `fmv` rows, repoints `bids`, and migrates the junction. Migration is idempotent (gated on `comics.grade` column existence).
- **R6.** API request shapes stay flat (`POST /api/comics`, `POST /api/extract-comics`) — handlers split internally. Response shapes update to reflect the new model.
- **R7.** All work lands in `plugins/gixen-overlay/` except one line in `gixen-cli/server/db.py`.
- **R8.** Test coverage preserved and extended; migration tested from realistic pre-migration state.

## Scope Boundaries

- No changes to gixen-cli's core route logic (`/api/snipes`, `/api/history`, `/api/bids`).
- `bids.comic_id` column stays as a soft-deprecated nullable artifact after migration (no new code sets it; removing it requires a host-table rebuild that is out of scope).
- Static `v2-comics.html` may need follow-up if its JS assumes the old flat comics shape.
- `POST /api/comics/{id}/fmv` was added then removed as redundant in the source branch — not included here.

### Deferred to Separate Tasks

- Update `v2-comics.html` JS to consume new `GET /api/comics` response shape: separate PR once the API shape is confirmed.
- Fix `pyproject.toml` absolute `pythonpath` entry (`/Users/hsukenooi/Projects/gixen-cli`): separate quality task.

## Context & Research

### Relevant Code and Patterns

- **Source implementation (read-only reference):** `origin/mac-mini/fmv-split-work` commits `020d864`–`597209b` in `gixen-cli`. Key commits: `020d864` (DDL), `536440e` (CRUD helpers), `aef127e` (migration), `f875f5a`–`e157787` (routes).
- **Plugin DB layer:** `plugins/gixen-overlay/src/gixen_overlay/db.py` — current `comics`/`bid_comics` DDL and CRUD to replace.
- **Plugin routes:** `plugins/gixen-overlay/src/gixen_overlay/routes.py` — `POST /api/comics`, `POST /api/extract-comics`, `POST /api/bids/{item_id}/comics/locg`.
- **Plugin test patterns:** `tests/test_gixen_overlay_db.py` (in-memory SQLite with minimal `bids` stub, `foreign_keys=ON`); `tests/test_gixen_overlay_routes.py` (`tmp_path` DB, raw `sqlite3.connect` for test data seeding, deferred `from server.main import app`).
- **Host migration pattern:** `gixen-cli/server/db.py` `_COLUMN_MIGRATIONS` + `_apply_migrations` — try/except catches `"duplicate column"` only; column additions are idempotent.
- **Table-rebuild ordering:** `PRAGMA foreign_keys=OFF` is a no-op inside a SAVEPOINT (the plugin hook runs in one). SQLite 3.26+ DOES update FK references when tables are renamed — the FK in `fmv(comic_id REFERENCES comics(id))` follows the rename to `comics_old`, so `DROP TABLE comics_old` fails with "FOREIGN KEY constraint failed" if any fmv rows exist. The only safe rebuild sequence is: (1) save fmv and bid_fmvs rows to Python memory, (2) DROP bid_fmvs, (3) DROP fmv, (4) RENAME comics→comics_old, (5) CREATE comics (new schema), (6) INSERT survivors, (7) DROP comics_old (safe — no FK children remain), (8) CREATE fmv, (9) CREATE bid_fmvs, (10) restore fmv rows, (11) restore bid_fmvs rows, (12) CREATE indexes.
- **Design doc:** `docs/2026-05-13-comic-fmv-split.md` — conflict-resolution rules, migration rationale, API decisions (see origin: `docs/2026-05-13-comic-fmv-split.md`).

### Institutional Learnings

- No `docs/solutions/` directory in either repo. Key patterns are in the live code.
- `PRAGMA foreign_keys=OFF` preceding `SAVEPOINT` is the correct ordering when needed outside a plugin hook. Inside a hook (where a savepoint already exists), the PRAGMA is a no-op — plan the migration ordering to avoid needing it.
- `DROP TABLE IF EXISTS bids_old` before any rename-and-rebuild prevents crash-recovery failures.
- Column list in `INSERT ... SELECT` must be fully enumerated — never `SELECT *` — to avoid silent data corruption when schemas differ.
- Indexes must be recreated explicitly after a table rebuild.

## Key Technical Decisions

- **`bids.fmv_id` goes in gixen-cli's `_COLUMN_MIGRATIONS`, not the plugin hook.** The plugin protocol does not endorse plugins modifying host tables. Adding a nullable `INTEGER` column to `bids` is a one-line host migration; the plugin then uses it. (see origin doc §"API decisions locked in")
- **Migration runs inside `create_tables()` (the `register_db_tables` hook).** It is gated on `PRAGMA table_info(comics)` detecting the legacy `grade` column. Idempotent — a no-op on already-migrated databases.
- **No `PRAGMA foreign_keys=OFF` in the migration.** Operation ordering (drop junction → delete non-survivors → rename parent → rebuild parent) avoids needing it, which is required since the hook runs inside a per-plugin savepoint.
- **`bid_comics` is dropped by the migration, not kept.** Fresh databases never get it (new `create_tables` only creates `bid_fmvs`); existing databases have it dropped after junction migration.
- **`upsert_comic` becomes identity-only.** Callers that pass grade/FMV data now call `upsert_comic` + `upsert_fmv` in sequence. The flat API request shapes are preserved; the route handlers split them internally.
- **`GET /api/comics` returns one row per `(comic, fmv)` pair.** This preserves the old flat per-grade shape the dashboard HTML expects, minimizing HTML changes.

## Open Questions

### Resolved During Planning

- **Can the migration run inside a pluggy savepoint (no `PRAGMA foreign_keys=OFF`)?** Yes — the operation ordering avoids it. The correct sequence drops the FK child tables (bid_fmvs, fmv) to Python memory before renaming the parent (comics), then rebuilds everything from memory after the new comics schema is in place. This avoids both the PRAGMA no-op constraint and the SQLite 3.26+ FK-follows-rename issue that would otherwise make `DROP TABLE comics_old` fail.
- **Where does `bids.fmv_id` live?** In gixen-cli's `_COLUMN_MIGRATIONS` as a prerequisite. The plugin does not modify host tables via its hook.
- **Keep or drop `bid_comics`?** Drop during migration. Fresh DBs never get it; existing DBs have it populated then dropped.
- **Keep or drop `bids.comic_id`?** Keep as soft-deprecated artifact — removing it requires a host-table rebuild outside plugin scope.

### Deferred to Implementation

- **`list_comics` JOIN shape.** Whether to `LEFT JOIN fmv` (one row per grade) or return nested fmv arrays depends on what the HTML can absorb. Implement as flat JOIN first; adjust if the HTML update is larger than expected.
- **`link_fmv_to_bid` idempotency in junction migration.** If a bid had multiple `bid_comics` entries pointing at the same `(survivor_id, grade)`, the migration may attempt duplicate `bid_fmvs` inserts — `INSERT OR IGNORE` handles this but verify in tests.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

### Final Schema (plugin-owned tables + host column)

```
comics          fmv               bid_fmvs
──────────      ──────────────    ──────────────────
id (PK)     ←── comic_id (FK)    bid_id (FK→bids)
title           id (PK)       ←── fmv_id (FK→fmv)
issue           grade             is_primary
year            low
locg_id         high
locg_variant_id comps
                confidence
                notes
                updated_at

bids (host-owned, plugin adds fmv_id column)
──────────────────────────────
id (PK)
item_id
fmv_id (FK→fmv, nullable, added via gixen-cli _COLUMN_MIGRATIONS)
comic_id (soft-deprecated, stays NULL for new bids)
... (all existing columns unchanged)
```

### Migration Flow (runs once inside `create_tables()`)

```
Gate:
  comics.grade column present?
  └─ No →
       comics_old table exists?
       ├─ Yes → raise RuntimeError("DB in crashed mid-migration state: comics_old exists
       │          but comics has no grade column — manual recovery required")
       └─ No → return immediately (already migrated or fresh DB)
  └─ Yes →
      1. SELECT survivors: per (title, issue, year), pick by
         locg_id NOT NULL > fmv_low NOT NULL > newest fmv_updated_at > lowest id
      2. INSERT INTO fmv for each legacy (survivor_id, grade) pair
         Conflict: if survivor already has fmv.low, keep it; merge notes from loser
      3. Build fmv_lookup: (comic_id, grade) → fmv_id
      4. UPDATE bids SET fmv_id=? WHERE comic_id IS NOT NULL AND grade IS NOT NULL
      5. INSERT OR IGNORE INTO bid_fmvs from bid_comics (skip null-grade bids)
      6. DROP TABLE bid_comics
      7. DELETE FROM comics WHERE id NOT IN (survivor_ids)
      8. Save fmv rows to Python list; save bid_fmvs rows to Python list
      9. DROP TABLE bid_fmvs
     10. DROP TABLE fmv
     11. RENAME comics → comics_old
     12. CREATE comics (new schema: id, title, issue, year, locg_id, locg_variant_id, UNIQUE)
     13. INSERT survivors from comics_old
     14. DROP TABLE comics_old (safe — no FK children remain)
     15. CREATE TABLE fmv (...) + CREATE TABLE bid_fmvs (...)
     16. Restore fmv rows and bid_fmvs rows from Python lists
     17. CREATE INDEX idx_fmv_comic, idx_bid_fmvs_bid
```

## Implementation Units

- [ ] **Unit 0: Add `bids.fmv_id` to gixen-cli (prerequisite)**

**Goal:** Add the `fmv_id` column to the host `bids` table so the plugin can set it.

**Requirements:** R4

**Dependencies:** None — lands first as its own gixen-cli PR.

**Target repo:** `gixen-cli` (not `comic-pipeline`)

**Files:**
- Modify: `server/db.py`
- Test: `tests/test_server_db.py`

**Approach:**
- Append `"ALTER TABLE bids ADD COLUMN fmv_id INTEGER"` to `_COLUMN_MIGRATIONS` (no `REFERENCES fmv(id)` — keep it a plain INTEGER to avoid dangling FK when gixen-cli runs without the plugin).
- No other gixen-cli changes.

**Test scenarios:**
- Happy path: `init_db` on a fresh DB creates `bids` with `fmv_id` column present (`PRAGMA table_info(bids)`).
- Edge case: `init_db` on an existing DB that already has `fmv_id` column (second call) is a no-op and does not raise.

**Verification:**
- `pytest tests/test_server_db.py` passes with the two new test cases.
- `PRAGMA table_info(bids)` on a fresh DB shows `fmv_id` column.

---

- [ ] **Unit 1: New tables DDL in `create_tables()`**

**Goal:** Create `fmv` and `bid_fmvs` tables (plus their indexes) as idempotent DDL. Fresh databases get the new schema from the start; existing databases get the tables added alongside the still-present old tables (which the migration in Unit 3 will upgrade).

**Requirements:** R1, R2, R3

**Dependencies:** Unit 0 merged and `bids.fmv_id` present on the host.

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_db.py`

**Approach:**
- Update `create_tables()` to use the new `comics` DDL (`UNIQUE(title, issue, year)` — no `grade`, no `fmv_*`). The `IF NOT EXISTS` means this is a no-op on existing DBs where the old schema is still present; the migration in Unit 3 handles the column-drop.
- Add `CREATE TABLE IF NOT EXISTS fmv (...)` and `CREATE TABLE IF NOT EXISTS bid_fmvs (...)` with correct FKs and unique constraints.
- Add indexes: `idx_fmv_comic ON fmv(comic_id)`, `idx_bid_fmvs_bid ON bid_fmvs(bid_id)`.
- Remove `CREATE TABLE IF NOT EXISTS bid_comics` from `create_tables()` — existing databases already have it; fresh ones should not get it.
- Remove the legacy `INSERT OR IGNORE INTO bid_comics ... SELECT id, comic_id, 1 FROM bids` seed from `create_tables()`.
- Each DDL statement is a separate `conn.execute(...)` call (no `executescript`).

**Patterns to follow:**
- `plugins/gixen-overlay/src/gixen_overlay/db.py` existing `create_tables()` for statement-per-execute discipline.
- `docs/2026-05-13-comic-fmv-split.md` §"Final schema" for the exact DDL.

**Test scenarios:**
- Happy path: `create_tables` on a fresh minimal bids stub creates `fmv` and `bid_fmvs` (verify via `sqlite_master`).
- Happy path: `create_tables` on a fresh DB does NOT create `bid_comics` (negative assertion).
- Edge case: `create_tables` called twice (idempotent — no error, tables still present).
- Integration: Minimal bids stub for these tests must include `fmv_id INTEGER` column (unit tests reference it when testing `bid_fmvs` FK).

**Verification:**
- `pytest tests/test_gixen_overlay_db.py` passes.
- `sqlite_master` on a fresh DB shows `fmv`, `bid_fmvs` present and `bid_comics` absent.

---

- [ ] **Unit 2: CRUD helpers — `upsert_fmv`, `set_bid_fmv`, `get_fmv_for_bid`, `link_fmv_to_bid`**

**Goal:** Add the four FMV-layer CRUD functions and update `upsert_comic` to identity-only. These are the building blocks used by migration and routes.

**Requirements:** R1, R2, R3, R4

**Dependencies:** Unit 1

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_db.py`

**Approach:**
- `upsert_comic(conn, title, issue, year, locg_id, locg_variant_id)` — identity-only; UNIQUE on `(title, issue, year)`; COALESCE updates `locg_id`, `locg_variant_id` on conflict. Remove `grade`, `fmv_*` parameters.
- `upsert_fmv(conn, comic_id, grade, low, high, comps, confidence, notes)` — UNIQUE on `(comic_id, grade)`; COALESCE on all value fields; `updated_at` bumped only when at least one non-NULL value field is provided (grade-only stubs get `updated_at=NULL`); raises `ValueError` if `grade is None`.
- `set_bid_fmv(conn, bid_id, fmv_id)` — `UPDATE bids SET fmv_id=?`; `None` clears the link.
- `get_fmv_for_bid(conn, bid_id)` — JOIN `bids → fmv`, returns `fmv.*`.
- `link_fmv_to_bid(conn, bid_id, fmv_id, is_primary)` — inserts into `bid_fmvs`; if `is_primary`, demotes prior primary entries and mirrors to `bids.fmv_id`; idempotent.
- `get_primary_fmv_for_bid(conn, bid_id)` — JOIN `bid_fmvs → fmv → comics`; returns fmv row with comic fields for the primary entry.

**Patterns to follow:**
- Existing `upsert_comic` and `link_comic_to_bid` in `plugins/gixen-overlay/src/gixen_overlay/db.py` for COALESCE conflict-update patterns and commit discipline.
- Source branch `536440e` for the `upsert_fmv` implementation logic.

**Test scenarios:**
- Happy path: `upsert_fmv` inserts a new fmv row and returns its id.
- Happy path: `upsert_fmv` on an existing `(comic_id, grade)` pair updates only non-NULL fields (COALESCE semantics).
- Edge case: `upsert_fmv` with `grade=None` raises `ValueError`.
- Edge case: grade-only stub (all value fields NULL) has `updated_at=NULL`; subsequent call with `low=500` bumps `updated_at`.
- Happy path: `link_fmv_to_bid` with `is_primary=True` demotes prior primary and mirrors to `bids.fmv_id`.
- Edge case: `link_fmv_to_bid` called twice for same `(bid_id, fmv_id)` is idempotent.
- Error path: `link_fmv_to_bid` with a non-existent `fmv_id` raises FK violation (with `foreign_keys=ON`).
- Happy path: `upsert_comic` returns same id on re-insert with same `(title, issue, year)`; updates `locg_id` via COALESCE.
- Integration: `get_primary_fmv_for_bid` returns the fmv row with comic fields after a full `upsert_comic` → `upsert_fmv` → `link_fmv_to_bid` chain.

**Verification:**
- `pytest tests/test_gixen_overlay_db.py` passes.
- Calling `upsert_comic` no longer accepts `grade` or `fmv_*` parameters.

---

- [ ] **Unit 3: `_migrate_fmv_split()` — one-time data migration**

**Goal:** Idempotent migration that collapses shadow comics rows, manufactures `fmv` rows, repoints `bids.fmv_id`, migrates `bid_comics → bid_fmvs`, drops `bid_comics`, drops non-survivor comics rows, and rebuilds `comics` to remove legacy columns.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** Unit 2

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_db.py`

**Approach:**
- Add `_migrate_fmv_split(conn)` called at the end of `create_tables()`.
- **CRITICAL: `_migrate_fmv_split()` must use raw `conn.execute()` SQL exclusively.** Never call CRUD helpers (`upsert_fmv`, `upsert_comic`, `link_fmv_to_bid`) — they call `conn.commit()` internally, which silently destroys the host's SAVEPOINT and makes rollback on error impossible.
- **Gate:**
  - If `"grade"` not in `{row[1] for row in conn.execute("PRAGMA table_info(comics)")}` (grade column absent):
    - Check if `comics_old` exists in `sqlite_master`. If yes: raise `RuntimeError("DB in crashed mid-migration state: comics_old exists but comics has no grade column — manual recovery required")`.
    - Otherwise return immediately (already migrated or fresh DB).
- **Step 1 — survivors:** Subquery per `(title, issue, year)` group picks survivor id by: `locg_id NOT NULL` > `fmv_low NOT NULL` > most recent `fmv_updated_at` (NULL sorts last) > lowest `id`.
- **Step 2 — fmv manufacture:** For each legacy `comics` row with `grade IS NOT NULL`, INSERT into `fmv(comic_id=survivor_id, grade, low=fmv_low, high=fmv_high, ...)`. On `(comic_id, grade)` conflict: if existing `fmv.low IS NULL` and incoming `fmv_low IS NOT NULL`, UPDATE to take the incoming values and prefix the existing `notes` with `"[merged from legacy comic_id=X] "`. Otherwise skip (existing row already has valuation; log and continue). All via raw SQL.
- **Step 3 — bid repoint:** For each `bids` row with `comic_id IS NOT NULL`: look up survivor_id and legacy `comics.grade` for that `comic_id`. If `grade IS NOT NULL`, find the manufactured `fmv_id` and `conn.execute("UPDATE bids SET fmv_id=?", ...)`. If `grade IS NULL`, skip (bid stays unlinked).
- **Step 4 — junction migration:** For each `bid_comics` row: resolve the bid's grade via `bids.comic_id → comics.grade`. If grade is non-NULL, find or create an fmv stub and `conn.execute("INSERT OR IGNORE INTO bid_fmvs ...")`. Skip if grade is NULL (log skipped count).
- **Step 5 — drop `bid_comics`:** `conn.execute("DROP TABLE bid_comics")`. Removes the only FK into comics that blocked non-survivor delete.
- **Step 6 — delete non-survivors:** `conn.execute("DELETE FROM comics WHERE id NOT IN (...)")`. Safe after Step 5.
- **Step 7 — rebuild `comics`:** Because SQLite 3.26+ updates FK references on rename, `fmv(comic_id REFERENCES comics(id))` would follow the rename to `comics_old`, causing `DROP TABLE comics_old` to fail with FK constraint error. Use the Python-memory approach instead:
  - Save all `fmv` rows to a Python list (raw SQL `SELECT * FROM fmv`).
  - Save all `bid_fmvs` rows to a Python list.
  - `DROP TABLE bid_fmvs` (removes FK child referencing fmv).
  - `DROP TABLE fmv` (removes FK child referencing comics).
  - `RENAME TABLE comics TO comics_old`.
  - `CREATE TABLE comics (id, title, issue, year, locg_id, locg_variant_id, created_at, UNIQUE(title,issue,year))`.
  - `INSERT INTO comics (...) SELECT id, title, issue, year, locg_id, locg_variant_id, created_at FROM comics_old` (explicit column list — no `SELECT *`).
  - `DROP TABLE comics_old` (safe — no FK children remain).
  - `CREATE TABLE fmv (...)` and `CREATE TABLE bid_fmvs (...)` with full FK constraints.
  - Restore fmv rows and bid_fmvs rows from Python lists via parameterized INSERT.
  - `CREATE INDEX idx_fmv_comic ON fmv(comic_id)` and `CREATE INDEX idx_bid_fmvs_bid ON bid_fmvs(bid_id)`.
- Log survivor count, fmv_inserted count, bids_linked count, junction_inserted count, junction_skipped count at INFO level.

**Patterns to follow:**
- Source branch `aef127e` for the full migration logic (port to plugin context, using raw SQL throughout).
- Do NOT follow the CRUD helper call pattern from the source branch — all DB writes in `_migrate_fmv_split()` must be raw `conn.execute()` calls.

**Test scenarios:**
- Happy path: DB with two `comics` rows sharing `(title, issue, year)` but different `grade` — migration collapses to one comic row; two `fmv` rows manufactured under the survivor.
- Happy path: `bids.fmv_id` correctly set after migration for a bid that had `comic_id` pointing at a graded comic.
- Happy path: `bid_comics` rows are migrated to `bid_fmvs`; `bid_comics` table is absent after migration.
- Happy path: `fmv` and `bid_fmvs` rows survive the Python-memory round-trip (Step 7) with all field values intact.
- Edge case: bid with `comic_id` pointing at a graded comic, and `bid_comics` entry for a different issue in the same lot — junction row with the lot issue gets its own fmv stub.
- Edge case: bid with `comic_id IS NULL` — `bids.fmv_id` stays NULL; no crash.
- Edge case: two legacy comics share `(title, issue, year)` and the same grade with conflicting FMV values — the row with `fmv_low NOT NULL` wins; the loser's notes are merged.
- Edge case: migration is idempotent — calling `create_tables()` again on a fully-migrated DB is a no-op (gate returns immediately, no error).
- Crash recovery: DB has `comics` with no `grade` column AND `comics_old` present (simulates mid-migration kill) — `create_tables()` raises `RuntimeError` with message containing "crashed mid-migration state".
- Regression (2026-05-13 incident): create two comics rows for same issue, different grade. Verify that after migration, a second grade revision does NOT create a second comics row — `upsert_fmv` on the single comics row updates the existing `fmv` row instead.
- Integration: fresh DB (no legacy data) — migration gate fires and returns immediately; `comics` schema is identity-only from the start; no `bid_comics` table exists.

**Verification:**
- `pytest tests/test_gixen_overlay_db.py` passes including all migration tests.
- On a pre-migration fixture DB: after calling `create_tables()`, `PRAGMA table_info(comics)` shows no `grade` or `fmv_*` columns; `bid_comics` absent from `sqlite_master`; `fmv` rows present for all graded comics; `bids.fmv_id` populated for all graded bids.

---

- [ ] **Unit 4: Write-path routes**

**Goal:** Update `POST /api/comics`, `POST /api/extract-comics`, and `POST /api/bids/{item_id}/comics/locg` to route through the new three-table model.

**Requirements:** R6

**Dependencies:** Unit 2

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/routes.py`
- Modify: `plugins/gixen-overlay/src/gixen_overlay/models.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- **`POST /api/comics`:** Keep `UpsertComicRequest` flat (grade, fmv_low, fmv_high, fmv_comps, fmv_confidence, fmv_notes fields stay on the model for CLI/skill compatibility). Route calls `upsert_comic(title, issue, year, locg_id, locg_variant_id)` then, if `req.grade is not None`, `upsert_fmv(comic_id, req.grade, req.fmv_low, ...)`. Return value: dict of the `comics` row only (fmv data available via `GET /api/comics` enriched endpoint).
- **`POST /api/extract-comics`:** Change filter from `comic_id IS NULL` to `fmv_id IS NULL`. For each parsed title: call `upsert_comic` (identity-only), then if `parsed.grade is not None`, call `upsert_fmv` to manufacture a stub (NULL low/high), then `link_fmv_to_bid` instead of `link_comic_to_bid`. Bids with no parseable grade get linked to the comic via a grade-NULL fmv stub only if the schema allows — since `fmv.grade NOT NULL`, ungraded bids cannot get an fmv link; they stay `fmv_id=NULL` (acceptable — same as old `comic_id=NULL` for ungraded bids). Return counts split into `linked_with_fmv` and `linked_without_fmv` (or preserve existing `linked` key; clarify at implementation).
- **`POST /api/bids/{item_id}/comics/locg`:** Resolve target comic via `bid.fmv_id → fmv.comic_id` instead of `bid.comic_id`. For the `req.issue` path (auto-upsert for lot issues): `upsert_comic` + `upsert_fmv` (stub at primary fmv's grade) + `link_fmv_to_bid`. Response `is_primary` flag derived from whether the target comic_id matches `bid.fmv_id → fmv.comic_id` (see source branch `dc9a129` for derivation logic).

**Patterns to follow:**
- Source branch commits `567b6da` and `e157787` for the route handler changes.
- Existing `routes.py` for `request.app.state.db` access and error shape.

**Test scenarios:**
- Happy path: `POST /api/comics` with grade + FMV fields creates a `comics` row and a `fmv` row; second call with same identity but updated `fmv_low` updates the `fmv` row (not creates a new `comics` row).
- Happy path: `POST /api/comics` without grade creates a `comics` row only (no `fmv` row).
- Happy path: `POST /api/extract-comics` on a bid with a parseable grade (e.g., "Amazing Spider-Man #300 1988 NM") links the bid via `fmv_id`; calling it again is idempotent (skips bids where `fmv_id` already set).
- Happy path: `POST /api/extract-comics` on a bid with a parseable title but no grade: comic is linked but `bids.fmv_id` remains NULL.
- Happy path: `POST /api/bids/{item_id}/comics/locg` with no `issue` resolves via `fmv_id` chain; sets `locg_id` on the resolved comic.
- Error path: `POST /api/bids/{item_id}/comics/locg` when bid has no `fmv_id` (no primary fmv linkage) and no `issue` param returns 409.
- Error path: `POST /api/comics` with invalid `fmv_confidence` returns 422.

**Verification:**
- `pytest tests/test_gixen_overlay_routes.py` passes.
- `POST /api/comics` with a grade creates both a `comics` row and a `fmv` row (verified via direct DB read in test).

---

- [ ] **Unit 5: Read-path — `list_comics` and `GET /api/comics`**

**Goal:** Update `list_comics` and `GET /api/comics` to return enriched data that includes FMV fields alongside comic identity, preserving the old flat per-grade row shape the dashboard relies on.

**Requirements:** R6

**Dependencies:** Unit 3 (migration must have run so that `fmv` table exists and is populated)

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py`
- Modify: `plugins/gixen-overlay/src/gixen_overlay/routes.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- `list_comics` becomes a JOIN: `SELECT c.id, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id, f.id AS fmv_id, f.grade, f.low AS fmv_low, f.high AS fmv_high, f.comps AS fmv_comps, f.confidence AS fmv_confidence, f.notes AS fmv_notes, f.updated_at AS fmv_updated_at FROM comics c LEFT JOIN fmv f ON f.comic_id = c.id WHERE [filters] ORDER BY c.id, f.grade`. This returns one row per `(comic, fmv)` pair — effectively the same shape as the old per-grade rows, just with identity deduplicated into the comic columns.
- Filter params: `title`, `issue`, `year` filter on `comics`; `grade` filters on `fmv.grade`. All nullable.
- For comics with no `fmv` rows, the LEFT JOIN returns one row with `grade=NULL, fmv_low=NULL, ...`.
- `GET /api/comics` returns `[dict(r) for r in rows]` — unchanged in the route, the join result is flat.

**Technical design:** The flat JOIN output mirrors the old schema shape. A comic with two grades (e.g., 9.0 and 9.2) returns two rows, just as the old `comics` table did (which had `UNIQUE(title, issue, year, grade)`). The dashboard HTML likely iterates these rows directly, so no HTML change should be needed for basic display.

**Patterns to follow:**
- Existing `list_comics` in `plugins/gixen-overlay/src/gixen_overlay/db.py` for filter-clause construction.

**Test scenarios:**
- Happy path: `GET /api/comics` after inserting a comic with two grades returns two rows, both with the same `id`/`title`/`issue`/`year` and different `grade`/`fmv_low` values.
- Happy path: `GET /api/comics` for a comic with no fmv rows returns one row with `grade=NULL`.
- Happy path: `GET /api/comics?grade=9.2` filters to only rows where `fmv.grade = 9.2`.
- Edge case: Empty DB returns empty list.
- Integration: After running `POST /api/comics` with grade=9.2, `GET /api/comics` returns the created fmv row's data in the enriched flat shape.

**Verification:**
- `pytest tests/test_gixen_overlay_routes.py` passes.
- `GET /api/comics` response for a two-grade comic has two objects in the list, each with `fmv_low`, `fmv_high`, `grade` populated.

## System-Wide Impact

- **Interaction graph:** `register_db_tables` hook fires inside the per-plugin savepoint in `_invoke_db_tables_isolated`. The migration runs as part of this hook — DDL and data operations all within one savepoint. If the migration raises, only this plugin's work rolls back; gixen-cli core and other plugins are unaffected.
- **Error propagation:** Migration failures surface as exceptions inside the savepoint; the host logs them and continues. The server starts but with the old schema intact and a logged error — the operator can investigate and restart.
- **State lifecycle risks:** The migration is a one-time destructive reshape. The compound gate (grade absent AND comics_old absent → return; grade absent AND comics_old present → raise RuntimeError) makes it idempotent on success and detectable on crash. Risk window: a process kill between Steps 11–14 (after RENAME, before DROP comics_old) leaves `comics_old` with no grade column in the new `comics` — the compound gate detects this and raises `RuntimeError` on next start rather than silently treating the empty new `comics` table as a completed migration.
- **API surface parity:** `bids.fmv_id` added to `bids` table → gixen-cli's `SELECT * FROM bids` (used by `/api/bids`, `/api/history`, `/api/snipes`) will include `fmv_id` in responses automatically. Callers that consume the bids shape will now see this extra nullable field — additive, backward compatible.
- **Integration coverage:** The route tests (`test_gixen_overlay_routes.py`) boot the full gixen-cli lifespan with the real plugin loaded, so they exercise the complete hook → migration → route chain. Unit DB tests verify migration correctness in isolation using pre-migration fixtures.
- **Unchanged invariants:** `bids.comic_id` remains in the schema (nullable, no new writes). gixen-cli's core endpoints (`/api/snipes`, `/api/history`, `/api/bids`) are not modified. The pluggy hook contract is unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Migration crashes mid-flight on Mac Mini (production DB) | Compound gate: if `grade` absent AND `comics_old` exists → raise RuntimeError; test crash scenario explicitly |
| SQLite 3.26+ updates FK references on RENAME — `DROP TABLE comics_old` fails with FK constraint | Python-memory approach: drop fmv+bid_fmvs to memory, drop tables, rename, rebuild, restore from memory |
| `conn.commit()` inside CRUD helpers destroys SAVEPOINT | `_migrate_fmv_split()` uses raw `conn.execute()` SQL only — never calls CRUD helpers |
| `PRAGMA foreign_keys=OFF` no-op inside savepoint breaks assumed migration strategy | Architecture decision: migration is redesigned to avoid this PRAGMA entirely |
| Existing `test_gixen_overlay_routes.py` tests reference old `fmv_confidence` response shape | Update affected tests in Unit 4; do not delete — adapt to new shape |
| `pyproject.toml` absolute pythonpath breaks CI | Known pre-existing issue; not introduced here; deferred |
| `v2-comics.html` JS assumes old flat `comics` response | Unit 5's flat JOIN output preserves the per-grade row shape; HTML changes should be minimal or zero |

## Sources & References

- **Origin document:** `docs/2026-05-13-comic-fmv-split.md`
- **Source branch:** `origin/mac-mini/fmv-split-work` in gixen-cli repo — commits `020d864` through `597209b`
- **Key reference commits:** `020d864` (DDL), `536440e` (CRUD helpers), `aef127e` (migration), `f875f5a` (upsert_comic identity-only), `567b6da` (route write paths), `57fe3ef` (read paths), `e157787` (extract-comics), `dc9a129` (is_primary from bid_fmvs)
- Plugin current state: `plugins/gixen-overlay/src/gixen_overlay/db.py`, `routes.py`, `models.py`
- Migration pattern precedent: `gixen-cli/server/db.py` `_apply_migrations` (lines 44–114)
