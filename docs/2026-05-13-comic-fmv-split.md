# Comic / FMV Schema Split Implementation Plan (v2)

> **Snapshot at planning time — code is the source of truth.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize the schema so `comics` holds identity only, a new `fmv` table holds per-grade valuations, and `bids.fmv_id` is the single FK linking a listing to its (comic, grade) valuation row. This makes "bid has a grade with no FMV row" structurally impossible — the FK guarantees the row exists.

**Architecture:** Three-table layout. `comics(id, title, issue, year, locg_id, locg_variant_id)` is the identity row keyed by `UNIQUE(title, issue, year)`. `fmv(id, comic_id, grade, low, high, comps, confidence, notes, updated_at)` is the per-grade valuation row keyed by `UNIQUE(comic_id, grade)`. `bids.fmv_id` is a nullable FK into `fmv` (nullable because Gixen-web-added bids land in our DB before any operator has classified the comic). The lot junction renames to `bid_fmvs(bid_id, fmv_id, is_primary)`. A startup migration in `server/db.py:_apply_migrations` collapses shadow `comics` rows, manufactures `fmv` rows for every legacy `(comic_id, grade)` pair (including grade-only rows with NULL valuation), and repoints every bid + junction row at its new `fmv_id`. Existing API request shapes stay flat for CLI/skill backward compatibility; handlers split the payload internally.

**Tech Stack:** Python 3.11+, SQLite (WAL mode, foreign_keys ON), FastAPI, Pydantic v2, pytest. Existing migration framework lives in `server/db.py:60-94` (`_COLUMN_MIGRATIONS` + `_apply_migrations`).

---

## Background — read this first

### The bug being fixed

The legacy `comics.UNIQUE(title, issue, year, grade)` makes grade part of identity. When an agent revises a grade or year after FMV has already been researched (the 2026-05-13 incident), `ON CONFLICT(...) DO UPDATE` no longer fires — a fresh row is inserted with `fmv_low=NULL`, and 14 bids ended up linked to FMV-less shadow rows. Mapping (incident-style):

```
id=42  title="Spider-Man" issue="300" year=1988 grade=9.0  fmv_low=800   ← original
id=58  title="Spider-Man" issue="300" year=1988 grade=9.2  fmv_low=NULL  ← shadow after grade revision
id=59  title="Spider-Man" issue="300" year=1989 grade=9.0  fmv_low=NULL  ← shadow after year typo correction
```

### Why three tables (and not just the two from v1 of this plan)

Earlier draft: `comics` (identity), `comic_fmv(comic_id, grade)` (FMV by grade), `bids.grade` (which copy the bid is for). FMV lookup was `JOIN comic_fmv ON bids.comic_id = comic_fmv.comic_id AND bids.grade = comic_fmv.grade`. That left a hole: a bid could carry `(comic_id=42, grade=9.4)` with **no** matching `comic_fmv` row, and the dashboard would silently render `—`. The same class of "FMV invisible" failure we're trying to eliminate, just reshaped.

The fix is to make the link itself the FMV row. **`bids.fmv_id` is a single FK to `fmv(id)`.** If a bid has `fmv_id`, the `fmv` row exists by FK guarantee. Grade is read from `fmv.grade`. "I want to record a grade but haven't researched FMV yet" creates an `fmv` row with NULL `low`/`high` — the grade is still pinned, the dashboard still renders `—`, the warning still fires, but the database invariant holds.

**Why this matters in plain English:** the schema can no longer represent the broken state. Anything claiming a grade is also claiming a (possibly empty) valuation row exists for that grade on that comic. Researching FMV is `UPDATE fmv SET low=?, high=? WHERE id=?`, not "create a new row and hope the bid was pointing at the right grade."

### Final schema

```sql
CREATE TABLE comics (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    issue           TEXT NOT NULL,
    year            INTEGER NOT NULL,
    locg_id         INTEGER,
    locg_variant_id INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(title, issue, year)
);

CREATE TABLE fmv (
    id          INTEGER PRIMARY KEY,
    comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
    grade       REAL NOT NULL,
    low         REAL,
    high        REAL,
    comps       INTEGER,
    confidence  TEXT CHECK(confidence IN ('high', 'medium', 'low') OR confidence IS NULL),
    notes       TEXT,
    updated_at  TEXT,
    UNIQUE(comic_id, grade)
);

CREATE INDEX idx_fmv_comic ON fmv(comic_id);

CREATE TABLE bids (
    id                  INTEGER PRIMARY KEY,
    item_id             TEXT NOT NULL,
    fmv_id              INTEGER REFERENCES fmv(id),   -- nullable
    max_bid             REAL NOT NULL,
    bid_offset          INTEGER DEFAULT 6,
    snipe_group         INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),
    winning_bid         REAL,
    seller              TEXT,
    auction_end_at      TEXT,
    local_snipe_at      TEXT,
    local_snipe_result  TEXT,
    notes               TEXT,
    ebay_title          TEXT,
    status_mirror       TEXT,
    cached_current_bid  TEXT,
    cached_at           TEXT,
    added_at            TEXT DEFAULT (datetime('now')),
    resolved_at         TEXT
);

CREATE INDEX idx_bids_item_id ON bids(item_id);
CREATE INDEX idx_bids_fmv ON bids(fmv_id);

CREATE TABLE bid_fmvs (
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    fmv_id     INTEGER NOT NULL REFERENCES fmv(id)  ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bid_id, fmv_id)
);

CREATE INDEX idx_bid_fmvs_bid ON bid_fmvs(bid_id);
```

### Conflict resolution rules during migration

1. **Comic identity collapse.** If multiple legacy `comics` rows share `(title, issue, year)`, pick the survivor by priority:
   1. row with non-null `locg_id`,
   2. row with non-null `fmv_low`,
   3. row with the most recent `fmv_updated_at` (NULL sorts last),
   4. lowest `id` (deterministic tiebreaker).
2. **Cross-year typos are NOT auto-merged.** Same `(title, issue)` with different `year` (e.g. 1975 vs 1976) stays distinct. Could be a typo, could be a reprint. Future `dedup-by-locg` tool handles it. Open question #1 below.
3. **FMV row manufacture.** For every legacy `comics` row with `grade IS NOT NULL`, insert one `fmv` row at `(survivor.id, legacy.grade)` carrying the legacy FMV tuple (mapped to the new column names: `fmv_low→low`, `fmv_high→high`, etc.). If two legacy rows collide on `(survivor.id, grade)`, the row with `fmv_low IS NOT NULL` wins; the loser's `notes` get prefixed with `"[merged from legacy comic_id=X] "` so nothing is dropped silently.
4. **Bid FK repoint.** Each legacy `bids` row with `comic_id IS NOT NULL`: resolve the grade from the legacy `comics.grade` of its original `comic_id`. If grade is non-NULL, find the `fmv(comic_id=survivor.id, grade=that_grade)` row and set `bids.fmv_id` to it. If grade is NULL, leave `bids.fmv_id` NULL (the bid keeps `item_id`/`max_bid`/etc. and shows in the dashboard as an unclassified row).
5. **Junction repoint.** Each legacy `bid_comics(bid_id, comic_id, is_primary)` row resolves the bid's grade (per rule 4) and inserts `bid_fmvs(bid_id, fmv_id, is_primary)`. If the grade is NULL we **skip** that junction row entirely — keeping it would require inventing an `fmv` row with NULL grade, which the `grade NOT NULL` constraint forbids. Skipped junction rows are logged.

### Workflow consequence (read this before reviewing)

**The new schema makes "park a grade with no FMV" require an explicit `fmv` row.** Old behavior allowed `bids.grade = 9.2` to sit there indefinitely with no valuation context. New behavior requires the caller to materialize `fmv(comic_id, grade=9.2, low=NULL, high=NULL, ...)`. The CLI `add` command does this transparently — if you pass `--grade 9.2` without FMV, the server creates the `fmv` row with NULL `low`/`high` and points the bid at it. The dashboard still renders `—` for unresearched FMV, and the existing `fmv_warning` field still fires (now triggered by `fmv.low IS NULL` instead of "no matching row"). The user has accepted this trade-off: a tiny extra write at bid-add time in exchange for the schema-level invariant that bids can never have a "phantom grade" again.

### API decisions locked in

- **`POST /api/comics` keeps its flat shape.** Internally calls `upsert_comic(title, issue, year, locg_*)` and, if `grade` is supplied, `upsert_fmv(comic_id, grade, low, high, ...)`. Backward compat for every existing caller.
- **`POST /api/bids` keeps its flat shape.** Internally upserts identity + FMV + sets `bids.fmv_id`. If the caller passes a grade with no FMV fields, the `fmv` row is created with NULL `low`/`high` — the warning still fires.
- **`GET /api/snipes` JOIN shrinks** to `bids LEFT JOIN fmv ON fmv.id = bids.fmv_id LEFT JOIN comics ON comics.id = fmv.comic_id`. The two-condition JOIN from the v1 plan disappears.
- **`fmv_warning` semantic shifts.** Old: "bid has grade but no `comic_fmv` row." New: "bid has `fmv_id` but `fmv.low IS NULL`." Same user-visible effect (dashboard renders `—`); cleaner trigger condition.

---

## File Structure

- **Modify** `server/db.py` (~408 lines today):
  - `_SCHEMA`: leave the legacy strings in place for first-init compatibility; the rebuild step in the migration replaces both `comics` and `bids` with the new shapes. Add the new `fmv` and `bid_fmvs` CREATE statements (idempotent IF NOT EXISTS).
  - `_COLUMN_MIGRATIONS`: append `ALTER TABLE bids ADD COLUMN fmv_id INTEGER REFERENCES fmv(id)` (idempotent — caught by the duplicate-column handler).
  - `_apply_migrations`: append `_migrate_fmv_split` (idempotent; gated on detecting legacy schema markers).
  - Rewrite `upsert_comic` for identity-only on `(title, issue, year)`.
  - Add `upsert_fmv(conn, comic_id, grade, low, high, comps, confidence, notes) -> int` returning the `fmv.id`.
  - Add `set_bid_fmv(conn, bid_id, fmv_id)`.
  - Add `get_fmv_for_bid(conn, bid_id)` returning the joined `fmv` row.
  - Add `link_fmv_to_bid(conn, bid_id, fmv_id, is_primary)`. **Delete** `link_comic_to_bid` entirely — `grep` confirms the only production callers are `server/main.py:1163` and `server/main.py:1336` (rewritten in Tasks 6 + 8 of this plan) and the legacy tests in `tests/test_server_db.py:250-335` (rewritten in Task 5). No skill, CLI, or external script imports it. No deprecation shim needed.
  - Update `list_comics` to optionally JOIN `fmv` when a grade filter is supplied; default returns identity only.
  - Replace `get_comics_for_bid` / `get_primary_comic_for_bid` with `get_fmvs_for_bid` / `get_primary_fmv_for_bid`.
- **Modify** `server/main.py`:
  - `UpsertComicRequest`: unchanged external shape (still has `grade`/`fmv_low`/`fmv_high`/...). Internally split.
  - `AddBidRequest`: unchanged external shape. Internally upserts identity → FMV → sets `bids.fmv_id`.
  - `api_upsert_comic`: route to `upsert_comic` + `upsert_fmv`.
  - `api_add_bid`: route to `upsert_comic` + `upsert_fmv` + `insert_bid(... fmv_id=...)` + `link_fmv_to_bid(... is_primary=True)`.
  - `api_get_snipes`, `api_get_history`, `api_get_all_bids`: rewrite SELECT to JOIN `fmv` + `comics` via `bids.fmv_id`. The flat response field names stay (`comic_title`, `comic_issue`, `comic_year`, `comic_grade`, `fmv_low`, `fmv_high`, `fmv_comps`, `fmv_confidence`, `fmv_notes`) so the dashboard JS needs zero changes; the API's `bids.comic_id` field is replaced by `fmv_id`.
  - `api_extract_comics`: call `upsert_comic` for identity, `upsert_fmv` to manufacture the grade row (NULL `low`/`high` if `parsed.grade` is set but no FMV available), then `link_fmv_to_bid`. If `parsed.grade` is NULL, the bid stays unclassified.
  - `api_link_locg`: still rewrites `comics.locg_id`; auto-create branch uses new `upsert_comic` signature and manufactures an `fmv` stub at the bid's primary grade.
  - New endpoint `POST /api/comics/{id}/fmv` (per-grade upsert) — Task 9.
- **Modify** `/Users/hsukenooi/Projects/Brain v3.0/.claude/commands/comic/snipe-add.md` (Task 8b):
  - Add a Caveat clause: a null-valuation `fmv(comic_id, grade, low=NULL, high=NULL)` row is created **only when the user explicitly opts to record a grade without FMV** ("add without FMV at grade X", "park at grade X"). If the user is silent on grade, or chooses a "proceed without FMV" path without specifying a grade, the skill sets `bids.fmv_id = NULL` and does NOT manufacture an `fmv` row.
  - The intent: the schema's null-valuation `fmv` row exists for explicit opt-in only. Drift into "always create a stub" would re-introduce the implicit-state problem the schema split is supposed to eliminate. `bids.fmv_id = NULL` remains the legitimate unclassified state for any bid lacking a researched grade.
- **Modify** `cli.py`:
  - No payload changes for `add` (server handler does the split). Docstring already mentions the flat flags; no change needed.
- **Modify** `tests/test_server_db.py`: rewrite legacy `upsert_comic` tests for the new signature, add `fmv` helper tests, add migration tests, add the FK-invariant test.
- **Modify** `tests/test_server_api.py`: update FMV expectations to flow through `bids.fmv_id`, add per-grade and per-lot e2e tests.
- **Create** `scripts/snapshot_db.sh`: pre/post-migration sanity script (Task 1).

The dashboard JS in `server/static/index.html` and `server/static/v2-comics.html` reads `fmv_low`/`fmv_high`/`comic_grade`/`comic_title` etc. from the snipes payload. The server-side rename is contained in queries — handler responses keep the same field names — so the dashboard requires zero changes.

---

## Task 1: Snapshot scripts and backup procedure

**Files:**
- Create: `/Users/hsukenooi/Projects/gixen-cli/scripts/snapshot_db.sh`

- [ ] **Step 1: Create the directory if missing**

Run: `mkdir -p /Users/hsukenooi/Projects/gixen-cli/scripts`
Expected: silent success.

- [ ] **Step 2: Write the snapshot script**

Create `scripts/snapshot_db.sh` with this exact content:

```bash
#!/usr/bin/env bash
# Pre/post-migration snapshot for the FMV split. Prints counts and a small
# sample so before/after diffs are obvious. Read-only — safe to run anytime.
set -euo pipefail

DB_PATH="${1:-$HOME/.gixen-server/db.sqlite}"
if [[ ! -f "$DB_PATH" ]]; then
  echo "DB not found at $DB_PATH" >&2
  exit 1
fi

echo "=== Snapshot of $DB_PATH at $(date -u +%FT%TZ) ==="

echo
echo "-- Schema --"
sqlite3 "$DB_PATH" ".schema comics"
sqlite3 "$DB_PATH" ".schema fmv" 2>/dev/null || echo "(fmv: does not exist yet)"
sqlite3 "$DB_PATH" ".schema bid_fmvs" 2>/dev/null || echo "(bid_fmvs: does not exist yet)"
sqlite3 "$DB_PATH" "PRAGMA table_info(bids);" \
  | grep -E '^[0-9]+\|(fmv_id|comic_id|grade)\|'

echo
echo "-- Counts --"
sqlite3 "$DB_PATH" "SELECT 'comics rows', COUNT(*) FROM comics;"
sqlite3 "$DB_PATH" "SELECT 'distinct (title,issue,year)', COUNT(*) FROM (SELECT DISTINCT title, issue, year FROM comics);"
sqlite3 "$DB_PATH" "SELECT 'comics with fmv_low NOT NULL', COUNT(*) FROM comics WHERE fmv_low IS NOT NULL;" 2>/dev/null \
  || echo "(legacy fmv_low column gone — post-migration DB)"
sqlite3 "$DB_PATH" "SELECT 'fmv rows', COUNT(*) FROM fmv;" 2>/dev/null || echo "(fmv: 0 / missing)"
sqlite3 "$DB_PATH" "SELECT 'fmv rows with low NOT NULL', COUNT(*) FROM fmv WHERE low IS NOT NULL;" 2>/dev/null || true
sqlite3 "$DB_PATH" "SELECT 'bids rows', COUNT(*) FROM bids;"
sqlite3 "$DB_PATH" "SELECT 'bids with comic_id NOT NULL (legacy)', COUNT(*) FROM bids WHERE comic_id IS NOT NULL;" 2>/dev/null \
  || echo "(bids.comic_id: column gone — post-migration DB)"
sqlite3 "$DB_PATH" "SELECT 'bids with fmv_id NOT NULL', COUNT(*) FROM bids WHERE fmv_id IS NOT NULL;" 2>/dev/null \
  || echo "(bids.fmv_id: column missing — pre-migration DB)"
sqlite3 "$DB_PATH" "SELECT 'bid_comics rows (legacy)', COUNT(*) FROM bid_comics;" 2>/dev/null || true
sqlite3 "$DB_PATH" "SELECT 'bid_fmvs rows', COUNT(*) FROM bid_fmvs;" 2>/dev/null || echo "(bid_fmvs: missing)"

echo
echo "-- Suspected shadow comics (same title+issue, multiple rows) --"
sqlite3 "$DB_PATH" "
  SELECT title, issue, COUNT(*) AS n_rows
  FROM comics
  GROUP BY title, issue
  HAVING n_rows > 1
  ORDER BY n_rows DESC, title
  LIMIT 20;
"

echo
echo "-- Sample of bids with FMV linkage --"
sqlite3 "$DB_PATH" "
  SELECT b.item_id, b.fmv_id,
         f.grade, f.low, f.high,
         c.title, c.issue, c.year
  FROM bids b
  LEFT JOIN fmv   f ON f.id = b.fmv_id
  LEFT JOIN comics c ON c.id = f.comic_id
  WHERE b.status != 'PURGED'
  ORDER BY b.added_at DESC
  LIMIT 10;
" 2>/dev/null || sqlite3 "$DB_PATH" "
  SELECT b.item_id, b.comic_id, c.title, c.issue, c.year, c.grade
  FROM bids b
  LEFT JOIN comics c ON c.id = b.comic_id
  WHERE b.status != 'PURGED'
  ORDER BY b.added_at DESC
  LIMIT 10;
"
```

- [ ] **Step 3: Make it executable**

Run: `chmod +x /Users/hsukenooi/Projects/gixen-cli/scripts/snapshot_db.sh`
Expected: silent success.

- [ ] **Step 4: Verify it runs the error path cleanly**

Run: `bash /Users/hsukenooi/Projects/gixen-cli/scripts/snapshot_db.sh /tmp/does-not-exist.sqlite`
Expected: prints `DB not found at /tmp/does-not-exist.sqlite`, exits non-zero.

- [ ] **Step 5: Commit**

```bash
git add scripts/snapshot_db.sh
git commit -m "Add pre/post migration snapshot script for FMV split"
```

---

## Task 2: Add `fmv` and `bid_fmvs` tables and `bids.fmv_id` column (additive only)

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/db.py` (the `_SCHEMA` string at lines 10-57 and `_COLUMN_MIGRATIONS` at lines 60-71)
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_db.py`

This task is purely additive: we add the new structures without touching the legacy `comics.grade` column or `bids.comic_id`. The data migration happens in Task 4.

- [ ] **Step 1: Write the failing tests for the additive schema**

Append to `tests/test_server_db.py`:

```python
def test_fmv_table_exists(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_fmv_has_expected_columns(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(fmv)")}
    assert cols == {
        "id", "comic_id", "grade", "low", "high", "comps",
        "confidence", "notes", "updated_at",
    }


def test_fmv_unique_on_comic_and_grade(db):
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='fmv'"
    ).fetchone()["sql"]
    normalized = sql.replace(" ", "")
    assert "UNIQUE(comic_id,grade)" in normalized


def test_bid_fmvs_has_expected_columns(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(bid_fmvs)")}
    assert cols == {"bid_id", "fmv_id", "is_primary"}


def test_bids_fmv_id_column_exists(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(bids)")}
    assert "fmv_id" in cols
```

- [ ] **Step 2: Run tests, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py::test_fmv_table_exists tests/test_server_db.py::test_bids_fmv_id_column_exists -v`
Expected: FAIL — `fmv` / `bid_fmvs` tables missing, `bids.fmv_id` column missing.

- [ ] **Step 3: Add the new tables to `_SCHEMA`**

In `server/db.py`, replace the entire `_SCHEMA = """ ... """` block (currently lines 10-57) with:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS comics (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    issue           TEXT NOT NULL,
    year            INTEGER NOT NULL,
    grade           REAL,
    fmv_low         REAL,
    fmv_high        REAL,
    fmv_comps       INTEGER,
    fmv_confidence  TEXT CHECK(fmv_confidence IN ('high', 'medium', 'low') OR fmv_confidence IS NULL),
    fmv_notes       TEXT,
    fmv_updated_at  TEXT,
    locg_id         INTEGER,
    locg_variant_id INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(title, issue, year, grade)
);

CREATE TABLE IF NOT EXISTS bids (
    id              INTEGER PRIMARY KEY,
    item_id         TEXT NOT NULL,
    comic_id        INTEGER REFERENCES comics(id),
    max_bid         REAL NOT NULL,
    bid_offset      INTEGER DEFAULT 6,
    snipe_group     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),
    winning_bid     REAL,
    seller          TEXT,
    auction_end_at      TEXT,
    local_snipe_at      TEXT,
    local_snipe_result  TEXT,
    notes               TEXT,
    added_at            TEXT DEFAULT (datetime('now')),
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);

CREATE TABLE IF NOT EXISTS bid_comics (
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bid_id, comic_id)
);

CREATE INDEX IF NOT EXISTS idx_bid_comics_bid ON bid_comics(bid_id);

CREATE TABLE IF NOT EXISTS fmv (
    id          INTEGER PRIMARY KEY,
    comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
    grade       REAL NOT NULL,
    low         REAL,
    high        REAL,
    comps       INTEGER,
    confidence  TEXT CHECK(confidence IN ('high', 'medium', 'low') OR confidence IS NULL),
    notes       TEXT,
    updated_at  TEXT,
    UNIQUE(comic_id, grade)
);

CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id);

CREATE TABLE IF NOT EXISTS bid_fmvs (
    bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
    fmv_id     INTEGER NOT NULL REFERENCES fmv(id)  ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bid_id, fmv_id)
);

CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id);
"""
```

(Legacy `comics` and `bid_comics` shapes stay in `_SCHEMA` for now. The rebuild step in Task 4 swaps them for the post-migration shape. Once any DB has been migrated, the `CREATE TABLE IF NOT EXISTS` lines are no-ops.)

- [ ] **Step 4: Add the `bids.fmv_id` ALTER**

In `server/db.py`, replace `_COLUMN_MIGRATIONS` (currently lines 60-71) with:

```python
_COLUMN_MIGRATIONS = [
    # bids columns added since the original schema
    "ALTER TABLE bids ADD COLUMN ebay_title TEXT",
    "ALTER TABLE bids ADD COLUMN status_mirror TEXT",
    "ALTER TABLE bids ADD COLUMN cached_current_bid TEXT",
    "ALTER TABLE bids ADD COLUMN cached_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_result TEXT",
    # comics columns added since the original schema
    "ALTER TABLE comics ADD COLUMN locg_id INTEGER",
    "ALTER TABLE comics ADD COLUMN locg_variant_id INTEGER",
    # FMV split (2026-05-13): fmv_id is the single FK from bids into the
    # per-grade fmv table. ALTER is idempotent (caught by the
    # "duplicate column" handler in _apply_migrations).
    "ALTER TABLE bids ADD COLUMN fmv_id INTEGER REFERENCES fmv(id)",
]
```

- [ ] **Step 5: Run the new tests, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -k "fmv_table_exists or fmv_has_expected_columns or fmv_unique_on_comic_and_grade or bid_fmvs_has_expected_columns or bids_fmv_id_column_exists" -v`
Expected: 5 PASS.

- [ ] **Step 6: Run the full db test suite to confirm no regressions**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -v`
Expected: existing tests still pass (the legacy `upsert_comic` keeps its old signature for now).

- [ ] **Step 7: Commit**

```bash
git add server/db.py tests/test_server_db.py
git commit -m "Add fmv and bid_fmvs tables, bids.fmv_id column (additive)"
```

---

## Task 3: New helpers `upsert_fmv`, `set_bid_fmv`, `get_fmv_for_bid`, `link_fmv_to_bid`

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/db.py` (add new functions after `upsert_comic`)
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_db.py`:

```python
from server.db import (
    upsert_fmv, set_bid_fmv, get_fmv_for_bid, link_fmv_to_bid,
)


def test_upsert_fmv_inserts_with_values(db):
    cid = upsert_comic(db, title="Hulk", issue="181", year=1974)
    fid = upsert_fmv(db, comic_id=cid, grade=9.2,
                     low=4000.0, high=5500.0, comps=12,
                     confidence="high", notes="GPA Jan 2026")
    assert isinstance(fid, int)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 4000.0
    assert row["high"] == 5500.0
    assert row["confidence"] == "high"
    assert row["updated_at"] is not None


def test_upsert_fmv_inserts_null_valuation(db):
    """Grade-only row, no FMV researched yet. updated_at stays NULL because
    no actual valuation was supplied."""
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, low=None, high=None, comps=None,
                     confidence=None, notes=None)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["grade"] == 9.2
    assert row["low"] is None
    assert row["high"] is None
    assert row["updated_at"] is None


def test_upsert_fmv_idempotent_on_conflict(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    f1 = upsert_fmv(db, cid, 8.0, low=500.0, high=700.0, comps=5,
                    confidence="medium", notes="")
    f2 = upsert_fmv(db, cid, 8.0, low=550.0, high=750.0, comps=8,
                    confidence="high", notes="Updated")
    assert f1 == f2
    row = db.execute("SELECT low, confidence FROM fmv WHERE id=?", (f1,)).fetchone()
    assert row["low"] == 550.0
    assert row["confidence"] == "high"


def test_upsert_fmv_preserves_on_partial_update(db):
    cid = upsert_comic(db, "Spawn", "1", 1992)
    fid = upsert_fmv(db, cid, 9.8, 100.0, 150.0, 5, "high", "first pass")
    upsert_fmv(db, cid, 9.8, low=120.0, high=None, comps=None,
               confidence=None, notes=None)
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 120.0
    assert row["high"] == 150.0
    assert row["comps"] == 5
    assert row["confidence"] == "high"
    assert row["notes"] == "first pass"


def test_upsert_fmv_different_grades_coexist(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    f1 = upsert_fmv(db, cid, 9.2, 800.0, 1000.0, 12, "high", "")
    f2 = upsert_fmv(db, cid, 7.0, 200.0, 300.0, 8, "high", "")
    assert f1 != f2
    rows = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
        (cid,),
    ).fetchall()
    assert [(r["grade"], r["low"]) for r in rows] == [(7.0, 200.0), (9.2, 800.0)]


def test_set_bid_fmv_sets_value(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    fid = upsert_fmv(db, cid, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "111111", 50.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_set_bid_fmv_accepts_none(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    fid = upsert_fmv(db, cid, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "111112", 50.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    set_bid_fmv(db, bid_id, None)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] is None


def test_get_fmv_for_bid_returns_joined_row(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, 800.0, 1000.0, 12, "high", "")
    bid_id = insert_bid(db, "111113", 600.0, None, 6, 0, "s")
    set_bid_fmv(db, bid_id, fid)
    fmv = get_fmv_for_bid(db, bid_id)
    assert fmv is not None
    assert fmv["low"] == 800.0
    assert fmv["grade"] == 9.2
    assert fmv["comic_id"] == cid


def test_get_fmv_for_bid_returns_none_when_unlinked(db):
    bid_id = insert_bid(db, "111114", 600.0, None, 6, 0, "s")
    assert get_fmv_for_bid(db, bid_id) is None


def test_link_fmv_to_bid_basic(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, None, None, None, None, None)
    bid_id = insert_bid(db, "111115", 600.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, fid)
    rows = db.execute("SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == fid
    assert rows[0]["is_primary"] == 0


def test_link_fmv_to_bid_primary_mirrors_to_bids_fmv_id(db):
    cid = upsert_comic(db, "ASM", "300", 1988)
    fid = upsert_fmv(db, cid, 9.2, None, None, None, None, None)
    bid_id = insert_bid(db, "111116", 600.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_link_fmv_to_bid_primary_demotes_prior(db):
    cid = upsert_comic(db, "Daredevil", "1", 1993)
    f1 = upsert_fmv(db, cid, 9.0, None, None, None, None, None)
    f2 = upsert_fmv(db, cid, 7.0, None, None, None, None, None)
    bid_id = insert_bid(db, "111117", 100.0, None, 6, 0, "s")
    link_fmv_to_bid(db, bid_id, f1, is_primary=True)
    link_fmv_to_bid(db, bid_id, f2, is_primary=True)
    by_fmv = {
        r["fmv_id"]: r["is_primary"] for r in db.execute(
            "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,)
        )
    }
    assert by_fmv[f1] == 0
    assert by_fmv[f2] == 1


def test_fk_invariant_fmv_id_must_exist(db):
    """Trying to set bids.fmv_id to a non-existent fmv.id fails the FK."""
    bid_id = insert_bid(db, "111118", 100.0, None, 6, 0, "s")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE bids SET fmv_id=999999 WHERE id=?", (bid_id,))
        db.commit()


def test_fk_invariant_bid_fmvs_fmv_id_must_exist(db):
    """Junction can't point at a non-existent fmv either."""
    bid_id = insert_bid(db, "111119", 100.0, None, 6, 0, "s")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, 999999, 0)",
            (bid_id,),
        )
        db.commit()
```

- [ ] **Step 2: Run, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -k "upsert_fmv or set_bid_fmv or get_fmv_for_bid or link_fmv_to_bid or fk_invariant" -v`
Expected: ImportError / NameError on the missing helpers.

- [ ] **Step 3: Implement the helpers**

In `server/db.py`, after the existing `upsert_comic` function (currently ending at line 154 — *the rewritten version comes in Task 5; this task appends the new helpers regardless of which signature `upsert_comic` has*), append:

```python
def upsert_fmv(
    conn: sqlite3.Connection,
    comic_id: int,
    grade: float,
    low: float | None,
    high: float | None,
    comps: int | None,
    confidence: str | None,
    notes: str | None,
) -> int:
    """Upsert per-grade FMV row. Returns the fmv.id.

    COALESCE on every value field preserves existing entries when partial
    updates arrive. `updated_at` is bumped only when at least one valuation
    field is non-NULL on this call — so a grade-only stub stays with
    updated_at=NULL until real research lands."""
    if grade is None:
        raise ValueError("upsert_fmv: grade is required")
    now = datetime.now(timezone.utc).isoformat()
    any_val = any(v is not None for v in (low, high, comps, confidence, notes))
    conn.execute(
        """
        INSERT INTO fmv (comic_id, grade, low, high, comps, confidence,
                         notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(comic_id, grade) DO UPDATE SET
            low        = COALESCE(excluded.low,        low),
            high       = COALESCE(excluded.high,       high),
            comps      = COALESCE(excluded.comps,      comps),
            confidence = COALESCE(excluded.confidence, confidence),
            notes      = COALESCE(excluded.notes,      notes),
            updated_at = CASE WHEN ? THEN excluded.updated_at ELSE updated_at END
        """,
        (comic_id, grade, low, high, comps, confidence, notes,
         now if any_val else None,
         1 if any_val else 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM fmv WHERE comic_id=? AND grade=?",
        (comic_id, grade),
    ).fetchone()
    return row["id"]


def set_bid_fmv(
    conn: sqlite3.Connection,
    bid_id: int,
    fmv_id: int | None,
) -> None:
    """Set bids.fmv_id. None clears the linkage (e.g. unclassified bid)."""
    conn.execute("UPDATE bids SET fmv_id = ? WHERE id = ?", (fmv_id, bid_id))
    conn.commit()


def get_fmv_for_bid(
    conn: sqlite3.Connection,
    bid_id: int,
) -> sqlite3.Row | None:
    """Return the fmv row this bid points at via fmv_id, or None if unlinked.
    Includes comic_id so callers can read it without a second query."""
    return conn.execute(
        """
        SELECT f.*
        FROM bids b
        JOIN fmv  f ON f.id = b.fmv_id
        WHERE b.id = ?
        """,
        (bid_id,),
    ).fetchone()


def link_fmv_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    fmv_id: int,
    is_primary: bool = False,
) -> None:
    """Insert into bid_fmvs. If is_primary, demote prior primary entries for
    this bid and mirror to bids.fmv_id. Idempotent."""
    if is_primary:
        conn.execute(
            "UPDATE bid_fmvs SET is_primary=0 WHERE bid_id=? AND fmv_id != ?",
            (bid_id, fmv_id),
        )
        conn.execute(
            """
            INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(bid_id, fmv_id) DO UPDATE SET is_primary = 1
            """,
            (bid_id, fmv_id),
        )
        conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, 0)",
            (bid_id, fmv_id),
        )
    conn.commit()
```

- [ ] **Step 4: Run the new tests, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -k "upsert_fmv or set_bid_fmv or get_fmv_for_bid or link_fmv_to_bid or fk_invariant" -v`
Expected: 14 PASS.

- [ ] **Step 5: Run the full db suite**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add server/db.py tests/test_server_db.py
git commit -m "Add upsert_fmv, set_bid_fmv, get_fmv_for_bid, link_fmv_to_bid helpers + FK invariant tests"
```

---

## Task 4: Data migration `_migrate_fmv_split`

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/db.py` (extend `_apply_migrations`)
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_db.py`

This is the hard one. The migration:
1. Collapses shadow `comics` rows by `(title, issue, year)` (survivor priority: `locg_id` → `fmv_low` → `fmv_updated_at` → `id`).
2. Manufactures one `fmv` row per legacy `(survivor_id, grade)` pair, carrying the legacy FMV values (renamed columns).
3. Repoints each `bids` row's `fmv_id` to the matching `fmv.id` (resolving the bid's grade from the legacy `comics.grade` of its original `comic_id`).
4. Repoints `bid_comics` rows into `bid_fmvs`, resolving via the bid's grade. Skips junction rows whose bid has a NULL grade.
5. Rebuilds `comics` and `bids` via the SQLite rename-and-rebuild dance to drop legacy columns.

- [ ] **Step 1: Write the failing migration tests**

Append to `tests/test_server_db.py`:

```python
import sqlite3 as _sqlite3  # alias to avoid clash with sqlite3 used in fixtures


def _build_legacy_db(path):
    """Construct a DB at the pre-split schema, bypassing init_db's new code.
    Mirrors the schema in production as of commit 941201f."""
    conn = _sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = _sqlite3.Row
    conn.executescript("""
    CREATE TABLE comics (
        id              INTEGER PRIMARY KEY,
        title           TEXT NOT NULL,
        issue           TEXT NOT NULL,
        year            INTEGER NOT NULL,
        grade           REAL,
        fmv_low         REAL,
        fmv_high        REAL,
        fmv_comps       INTEGER,
        fmv_confidence  TEXT,
        fmv_notes       TEXT,
        fmv_updated_at  TEXT,
        locg_id         INTEGER,
        locg_variant_id INTEGER,
        created_at      TEXT DEFAULT (datetime('now')),
        UNIQUE(title, issue, year, grade)
    );
    CREATE TABLE bids (
        id              INTEGER PRIMARY KEY,
        item_id         TEXT NOT NULL,
        comic_id        INTEGER REFERENCES comics(id),
        max_bid         REAL NOT NULL,
        bid_offset      INTEGER DEFAULT 6,
        snipe_group     INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'PENDING',
        winning_bid     REAL,
        seller          TEXT,
        auction_end_at      TEXT,
        local_snipe_at      TEXT,
        local_snipe_result  TEXT,
        notes               TEXT,
        added_at            TEXT DEFAULT (datetime('now')),
        resolved_at         TEXT,
        ebay_title          TEXT,
        status_mirror       TEXT,
        cached_current_bid  TEXT,
        cached_at           TEXT
    );
    CREATE TABLE bid_comics (
        bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
        comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
        is_primary INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bid_id, comic_id)
    );
    """)
    conn.commit()
    return conn


def test_migration_collapses_shadow_rows_into_single_comic(tmp_path):
    """Same (title, issue, year), three grades, only the first has FMV.
    After migration: one comics row, three fmv rows (one per grade), bids
    repointed to the matching fmv.id."""
    path = tmp_path / "legacy.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, "
        "fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at, locg_id) "
        "VALUES (42, 'Spider-Man', '300', 1988, 9.0, 800, 1000, 12, 'high', "
        "'orig', '2026-05-01T00:00:00', 99999)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (58, 'Spider-Man', '300', 1988, 9.2)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (60, 'Spider-Man', '300', 1988, 8.0)"
    )
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 42, 700)")
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (2, '222', 58, 900)")
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (3, '333', 60, 400)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 42, 1)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (2, 58, 1)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (3, 60, 1)")
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        rows = new.execute(
            "SELECT id, locg_id FROM comics WHERE title='Spider-Man' AND issue='300' AND year=1988"
        ).fetchall()
        assert len(rows) == 1
        survivor_id = rows[0]["id"]
        assert rows[0]["locg_id"] == 99999

        fmv_rows = new.execute(
            "SELECT id, grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
            (survivor_id,),
        ).fetchall()
        assert [r["grade"] for r in fmv_rows] == [8.0, 9.0, 9.2]
        by_grade = {r["grade"]: r["low"] for r in fmv_rows}
        assert by_grade[9.0] == 800.0
        assert by_grade[9.2] is None
        assert by_grade[8.0] is None

        fmv_by_grade = {r["grade"]: r["id"] for r in fmv_rows}
        bid_rows = new.execute(
            "SELECT item_id, fmv_id FROM bids ORDER BY item_id"
        ).fetchall()
        bid_fmv_by_item = {r["item_id"]: r["fmv_id"] for r in bid_rows}
        assert bid_fmv_by_item["111"] == fmv_by_grade[9.0]
        assert bid_fmv_by_item["222"] == fmv_by_grade[9.2]
        assert bid_fmv_by_item["333"] == fmv_by_grade[8.0]

        junc_rows = new.execute(
            "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs ORDER BY bid_id"
        ).fetchall()
        assert {(r["bid_id"], r["fmv_id"]) for r in junc_rows} == {
            (1, fmv_by_grade[9.0]),
            (2, fmv_by_grade[9.2]),
            (3, fmv_by_grade[8.0]),
        }
        assert all(r["is_primary"] == 1 for r in junc_rows)
    finally:
        new.close()


def test_migration_is_idempotent(tmp_path):
    """Running init_db on an already-migrated DB is a no-op."""
    path = tmp_path / "idem.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.commit()
    conn.close()

    conn1 = init_db(path)
    cc1 = conn1.execute("SELECT COUNT(*) AS n FROM comics").fetchone()["n"]
    fc1 = conn1.execute("SELECT COUNT(*) AS n FROM fmv").fetchone()["n"]
    bf1 = conn1.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()["fmv_id"]
    conn1.close()

    conn2 = init_db(path)
    cc2 = conn2.execute("SELECT COUNT(*) AS n FROM comics").fetchone()["n"]
    fc2 = conn2.execute("SELECT COUNT(*) AS n FROM fmv").fetchone()["n"]
    bf2 = conn2.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()["fmv_id"]
    conn2.close()

    assert cc1 == cc2 == 1
    assert fc1 == fc2 == 1
    assert bf1 == bf2 is not None


def test_migration_handles_null_grade_bid(tmp_path):
    """Bid with comic_id but no grade on the linked comic → fmv_id stays NULL,
    bid stays in the table (it's still a real auction we're tracking)."""
    path = tmp_path / "null.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade) "
        "VALUES (1, 'Hulk', '181', 1974, NULL)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.execute(
        "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        bid = new.execute(
            "SELECT id, fmv_id FROM bids WHERE id=1"
        ).fetchone()
        assert bid is not None
        assert bid["fmv_id"] is None
        fmv_count = new.execute("SELECT COUNT(*) AS n FROM fmv").fetchone()["n"]
        assert fmv_count == 0
        junc_count = new.execute("SELECT COUNT(*) AS n FROM bid_fmvs").fetchone()["n"]
        assert junc_count == 0
    finally:
        new.close()


def test_migration_preserves_orphan_fmv(tmp_path):
    """A legacy comics row with FMV but no bids still gets an fmv row so the
    valuation isn't lost."""
    path = tmp_path / "orphan.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'X-Men', '1', 1963, 8.0, 5000)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        fmv = new.execute("SELECT low FROM fmv WHERE grade=8.0").fetchone()
        assert fmv is not None
        assert fmv["low"] == 5000.0
    finally:
        new.close()


def test_migration_survivor_prefers_locg_then_fmv(tmp_path):
    path = tmp_path / "survivor.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, locg_id) "
        "VALUES (10, 'ASM', '300', 1988, 9.4, 11111)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (20, 'ASM', '300', 1988, 9.2, 800)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        survivor = new.execute(
            "SELECT id, locg_id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
        ).fetchone()
        assert survivor["locg_id"] == 11111
        grades = {r["grade"] for r in new.execute(
            "SELECT grade FROM fmv WHERE comic_id=?", (survivor["id"],)
        )}
        assert grades == {9.2, 9.4}
        fmv92 = new.execute(
            "SELECT low FROM fmv WHERE comic_id=? AND grade=9.2",
            (survivor["id"],),
        ).fetchone()
        assert fmv92["low"] == 800
    finally:
        new.close()


def test_migration_lot_with_grade_creates_one_bid_fmvs_per_comic(tmp_path):
    """bid_comics row over a 3-issue lot, bid grade=6.0 → 3 bid_fmvs rows,
    each pointing to an fmv at grade 6.0 for the respective comic."""
    path = tmp_path / "lot.db"
    conn = _build_legacy_db(path)
    for i, cid in enumerate((101, 102, 103), start=1):
        conn.execute(
            "INSERT INTO comics (id, title, issue, year, grade) "
            "VALUES (?, 'Daredevil', ?, 1993, 6.0)",
            (cid, str(i)),
        )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 101, 100)"
    )
    for cid in (101, 102, 103):
        conn.execute(
            "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, ?, ?)",
            (cid, 1 if cid == 101 else 0),
        )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        comic_ids = {
            r["id"] for r in new.execute(
                "SELECT id FROM comics WHERE title='Daredevil'"
            )
        }
        assert len(comic_ids) == 3
        fmv_rows = new.execute("SELECT id, grade FROM fmv").fetchall()
        assert {r["grade"] for r in fmv_rows} == {6.0}
        assert len(fmv_rows) == 3
        junc = new.execute(
            "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs WHERE bid_id=1"
        ).fetchall()
        assert len(junc) == 3
        primary = [r for r in junc if r["is_primary"] == 1]
        assert len(primary) == 1
        primary_fmv_id = primary[0]["fmv_id"]
        bid_fmv = new.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()["fmv_id"]
        assert bid_fmv == primary_fmv_id
    finally:
        new.close()


def test_migration_post_state_drops_legacy_columns(tmp_path):
    """After migration: comics.grade, comics.fmv_*, bids.comic_id are gone."""
    path = tmp_path / "post.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        comic_cols = {row[1] for row in new.execute("PRAGMA table_info(comics)")}
        bid_cols = {row[1] for row in new.execute("PRAGMA table_info(bids)")}
        assert "grade" not in comic_cols
        assert "fmv_low" not in comic_cols
        assert "fmv_high" not in comic_cols
        assert "fmv_comps" not in comic_cols
        assert "fmv_confidence" not in comic_cols
        assert "fmv_notes" not in comic_cols
        assert "fmv_updated_at" not in comic_cols
        assert "comic_id" not in bid_cols
        assert "grade" not in bid_cols
        assert "fmv_id" in bid_cols
    finally:
        new.close()
```

- [ ] **Step 2: Run, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -k "migration" -v`
Expected: every new migration test fails.

- [ ] **Step 3: Implement the migration**

In `server/db.py`, replace `_apply_migrations` (currently lines 74-94) with:

```python
def _apply_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Legacy backfill from before bid_comics existed.
    conn.execute(
        """
        INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary)
        SELECT id, comic_id, 1 FROM bids WHERE comic_id IS NOT NULL
        """
    )
    conn.commit()

    _migrate_fmv_split(conn)


def _migrate_fmv_split(conn: sqlite3.Connection) -> None:
    """Collapse comics shadow rows, manufacture fmv rows for every legacy
    (comic_id, grade) pair, and repoint bids/junction at the new fmv_id.

    Idempotent — gated on the presence of the legacy `comics.grade` column.
    Once the rebuild step runs, this column is gone and the function returns
    immediately on subsequent calls.

    Survivor priority: locg_id NOT NULL > fmv_low NOT NULL > most recent
    fmv_updated_at > lowest id."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    if "grade" not in cols:
        return  # already migrated

    import logging
    log = logging.getLogger(__name__)

    # 1. Compute survivor id per (title, issue, year) group.
    survivors = conn.execute("""
        SELECT title, issue, year,
               (SELECT id FROM comics c2
                WHERE c2.title=c1.title AND c2.issue=c1.issue AND c2.year=c1.year
                ORDER BY
                  CASE WHEN c2.locg_id        IS NOT NULL THEN 0 ELSE 1 END,
                  CASE WHEN c2.fmv_low        IS NOT NULL THEN 0 ELSE 1 END,
                  CASE WHEN c2.fmv_updated_at IS NULL    THEN 1 ELSE 0 END,
                  c2.fmv_updated_at DESC,
                  c2.id ASC
                LIMIT 1) AS survivor_id
        FROM comics c1
        GROUP BY title, issue, year
    """).fetchall()
    survivor_map: dict[tuple[str, str, int], int] = {
        (r["title"], r["issue"], r["year"]): r["survivor_id"] for r in survivors
    }

    # 2. Build fmv rows. For every legacy comic row with grade IS NOT NULL,
    #    insert an fmv row at (survivor_id, grade) carrying the legacy
    #    valuation tuple. Conflicts on (survivor_id, grade) resolved by
    #    "row with fmv_low NOT NULL wins"; the loser's notes are prefixed.
    legacy_rows = conn.execute(
        "SELECT id, title, issue, year, grade, fmv_low, fmv_high, fmv_comps, "
        "fmv_confidence, fmv_notes, fmv_updated_at "
        "FROM comics"
    ).fetchall()
    legacy_to_survivor: dict[int, int] = {
        r["id"]: survivor_map[(r["title"], r["issue"], r["year"])]
        for r in legacy_rows
    }
    fmv_inserted = 0
    for row in legacy_rows:
        if row["grade"] is None:
            continue
        survivor_id = legacy_to_survivor[row["id"]]
        existing = conn.execute(
            "SELECT id, low FROM fmv WHERE comic_id=? AND grade=?",
            (survivor_id, row["grade"]),
        ).fetchone()
        if existing is not None:
            if existing["low"] is None and row["fmv_low"] is not None:
                conn.execute(
                    """
                    UPDATE fmv
                    SET low=?, high=?, comps=?, confidence=?,
                        notes = COALESCE(?, notes),
                        updated_at=?
                    WHERE id=?
                    """,
                    (row["fmv_low"], row["fmv_high"], row["fmv_comps"],
                     row["fmv_confidence"],
                     f"[merged from legacy comic_id={row['id']}] "
                     + (row["fmv_notes"] or ""),
                     row["fmv_updated_at"], existing["id"]),
                )
            else:
                log.info(
                    "fmv split: skip legacy comic_id=%s grade=%s "
                    "(survivor fmv row already has valuation)",
                    row["id"], row["grade"],
                )
            continue
        conn.execute(
            """
            INSERT INTO fmv (comic_id, grade, low, high, comps,
                             confidence, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (survivor_id, row["grade"], row["fmv_low"], row["fmv_high"],
             row["fmv_comps"], row["fmv_confidence"], row["fmv_notes"],
             row["fmv_updated_at"]),
        )
        fmv_inserted += 1

    # 3. (comic_id, grade) → fmv_id lookup used by steps 4 and 5.
    fmv_lookup_rows = conn.execute("SELECT id, comic_id, grade FROM fmv").fetchall()
    fmv_by_pair: dict[tuple[int, float], int] = {
        (r["comic_id"], r["grade"]): r["id"] for r in fmv_lookup_rows
    }
    legacy_grade: dict[int, float | None] = {
        r["id"]: r["grade"] for r in legacy_rows
    }

    # 4. Repoint bids. For each bid with comic_id NOT NULL, resolve survivor
    #    and grade, look up fmv_id, set bids.fmv_id.
    bids_linked = 0
    bids_with_null = 0
    bid_rows = conn.execute(
        "SELECT id, comic_id FROM bids WHERE comic_id IS NOT NULL"
    ).fetchall()
    for b in bid_rows:
        legacy_cid = b["comic_id"]
        survivor_id = legacy_to_survivor[legacy_cid]
        grade = legacy_grade[legacy_cid]
        if grade is None:
            bids_with_null += 1
            continue
        fmv_id = fmv_by_pair[(survivor_id, grade)]
        conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, b["id"]))
        bids_linked += 1

    # 5. Migrate bid_comics → bid_fmvs. Resolve via the bid's primary grade
    #    (which is the legacy comics.grade of bids.comic_id). Junction rows
    #    whose bid has no resolvable grade are skipped.
    junction_inserted = 0
    junction_skipped = 0
    bc_rows = conn.execute(
        "SELECT bc.bid_id, bc.comic_id, bc.is_primary "
        "FROM bid_comics bc "
        "JOIN bids b ON b.id = bc.bid_id"
    ).fetchall()
    for bc in bc_rows:
        bid_row = conn.execute(
            "SELECT comic_id FROM bids WHERE id=?", (bc["bid_id"],)
        ).fetchone()
        if bid_row is None or bid_row["comic_id"] is None:
            junction_skipped += 1
            continue
        grade = legacy_grade[bid_row["comic_id"]]
        if grade is None:
            junction_skipped += 1
            continue
        survivor_id = legacy_to_survivor[bc["comic_id"]]
        key = (survivor_id, grade)
        if key not in fmv_by_pair:
            # Junction comic didn't carry this grade in legacy data; create
            # a NULL-valuation fmv stub so the junction row can land.
            conn.execute(
                "INSERT INTO fmv (comic_id, grade, updated_at) VALUES (?, ?, NULL)",
                (survivor_id, grade),
            )
            new_id = conn.execute(
                "SELECT id FROM fmv WHERE comic_id=? AND grade=?",
                (survivor_id, grade),
            ).fetchone()["id"]
            fmv_by_pair[key] = new_id
        fmv_id = fmv_by_pair[key]
        conn.execute(
            """
            INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary)
            VALUES (?, ?, ?)
            """,
            (bc["bid_id"], fmv_id, bc["is_primary"]),
        )
        junction_inserted += 1

    # 6. Delete non-survivor comics rows. FKs are already repointed.
    survivor_ids = list({s for s in survivor_map.values()})
    if survivor_ids:
        placeholders = ",".join("?" * len(survivor_ids))
        conn.execute(
            f"DELETE FROM comics WHERE id NOT IN ({placeholders})",
            survivor_ids,
        )
    collapsed = len(legacy_rows) - len(survivor_ids)

    # 7. Rebuild comics and bids to drop legacy columns. SQLite has no DROP
    #    COLUMN before 3.35 and no DROP CONSTRAINT at all, so use the standard
    #    rename-and-rebuild dance. Wrap in a savepoint so a failure leaves
    #    the DB recoverable (caller restores from backup).
    conn.execute("SAVEPOINT fmv_split_rebuild")
    try:
        # Comics: drop grade, fmv_*, change UNIQUE to (title, issue, year).
        conn.execute("ALTER TABLE comics RENAME TO comics_legacy")
        conn.execute("""
            CREATE TABLE comics (
                id              INTEGER PRIMARY KEY,
                title           TEXT NOT NULL,
                issue           TEXT NOT NULL,
                year            INTEGER NOT NULL,
                locg_id         INTEGER,
                locg_variant_id INTEGER,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(title, issue, year)
            )
        """)
        conn.execute("""
            INSERT INTO comics
            (id, title, issue, year, locg_id, locg_variant_id, created_at)
            SELECT id, title, issue, year, locg_id, locg_variant_id, created_at
            FROM comics_legacy
        """)
        conn.execute("DROP TABLE comics_legacy")

        # Bids: drop comic_id. fmv_id is already populated.
        conn.execute("ALTER TABLE bids RENAME TO bids_legacy")
        conn.execute("""
            CREATE TABLE bids (
                id                  INTEGER PRIMARY KEY,
                item_id             TEXT NOT NULL,
                fmv_id              INTEGER REFERENCES fmv(id),
                max_bid             REAL NOT NULL,
                bid_offset          INTEGER DEFAULT 6,
                snipe_group         INTEGER DEFAULT 0,
                status              TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),
                winning_bid         REAL,
                seller              TEXT,
                auction_end_at      TEXT,
                local_snipe_at      TEXT,
                local_snipe_result  TEXT,
                notes               TEXT,
                ebay_title          TEXT,
                status_mirror       TEXT,
                cached_current_bid  TEXT,
                cached_at           TEXT,
                added_at            TEXT DEFAULT (datetime('now')),
                resolved_at         TEXT
            )
        """)
        conn.execute("""
            INSERT INTO bids (
                id, item_id, fmv_id, max_bid, bid_offset, snipe_group, status,
                winning_bid, seller, auction_end_at, local_snipe_at,
                local_snipe_result, notes, ebay_title, status_mirror,
                cached_current_bid, cached_at, added_at, resolved_at
            )
            SELECT
                id, item_id, fmv_id, max_bid, bid_offset, snipe_group, status,
                winning_bid, seller, auction_end_at, local_snipe_at,
                local_snipe_result, notes, ebay_title, status_mirror,
                cached_current_bid, cached_at, added_at, resolved_at
            FROM bids_legacy
        """)
        conn.execute("DROP TABLE bids_legacy")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_fmv ON bids(fmv_id)")

        # bid_comics: drop entirely — replaced by bid_fmvs.
        conn.execute("DROP TABLE IF EXISTS bid_comics")

        conn.execute("RELEASE fmv_split_rebuild")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT fmv_split_rebuild")
        conn.execute("RELEASE fmv_split_rebuild")
        raise

    conn.commit()

    log.warning(
        "FMV split migration complete: collapsed %d shadow comics, "
        "inserted %d fmv rows, linked %d bids to fmv_id (%d bids left with NULL "
        "fmv_id due to missing grade), migrated %d bid_fmvs junction rows (skipped %d).",
        collapsed, fmv_inserted, bids_linked, bids_with_null,
        junction_inserted, junction_skipped,
    )
```

Notes:
- `PRAGMA foreign_keys=ON` is set in `init_db`. The savepoint defers FK checks until `RELEASE`. If FK violations are reported on release, the rebuild is broken and the migration aborts.
- Step 5 manufactures a NULL-valuation `fmv` stub when a junction comic doesn't already have one at the bid's grade. This preserves lot linkage in the parser-created data without losing fidelity.

- [ ] **Step 4: Run the migration tests, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -k "migration" -v`
Expected: all migration tests PASS.

- [ ] **Step 5: Run the full db test suite — expect failures in legacy upsert_comic tests**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -v`
Expected: `test_upsert_comic_inserts`, `test_upsert_comic_updates_on_conflict`, `test_upsert_comic_persists_locg_ids` etc. now fail because the new `comics` schema has no `grade`/`fmv_*` columns. **These will be fixed in Task 5.**

- [ ] **Step 6: Commit**

```bash
git add server/db.py tests/test_server_db.py
git commit -m "Migrate to fmv-id linkage: collapse comics shadows, build fmv rows, repoint bids

- comics keyed by (title, issue, year); grade/fmv_* columns removed
- new fmv table holds per-grade valuations; UNIQUE(comic_id, grade)
- bids.fmv_id replaces bids.comic_id
- bid_comics → bid_fmvs junction migrated, skipping null-grade bids
- legacy tables rebuilt via SQLite rename-and-recreate inside a savepoint"
```

---

## Task 5: Rewrite `upsert_comic`, `insert_bid` signatures, junction helpers

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/db.py` (the `upsert_comic` function at lines 114-154, plus `insert_bid`, `list_comics`, **delete** `link_comic_to_bid`, **replace** `get_comics_for_bid` / `get_primary_comic_for_bid` with the new `fmv`-based helpers)
- Modify: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_db.py`

`upsert_comic` is identity-only. `insert_bid` takes `fmv_id` (not `comic_id`). `get_comics_for_bid` and `get_primary_comic_for_bid` are replaced by `get_fmvs_for_bid` / `get_primary_fmv_for_bid`. `link_comic_to_bid` is **deleted outright** — no shim. Production callers in `server/main.py` are rewritten in Tasks 6 + 8, and the legacy tests in `tests/test_server_db.py:250-335` are rewritten in Step 1 below.

- [ ] **Step 1: Update the failing legacy tests**

In `tests/test_server_db.py`, first update the imports at line 14 — remove `link_comic_to_bid`, `get_comics_for_bid`, `get_primary_comic_for_bid` and add the new helpers (`link_fmv_to_bid`, `get_fmvs_for_bid`, `get_primary_fmv_for_bid` are imported in Task 3's test additions, so just remove the dead names here). The line currently reads:

```python
from server.db import (
    init_db, upsert_comic, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged,
    link_comic_to_bid, get_comics_for_bid, get_primary_comic_for_bid,
)
```

Change it to:

```python
from server.db import (
    init_db, upsert_comic, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged,
)
```

Then replace these existing tests:

Replace `test_upsert_comic_inserts` (currently around lines 38-48):

```python
def test_upsert_comic_inserts(db):
    comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="300", year=1988)
    assert isinstance(comic_id, int)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["issue"] == "300"
    assert row["year"] == 1988
```

Replace `test_upsert_comic_updates_on_conflict` (lines 51-60):

```python
def test_upsert_comic_idempotent_on_identity(db):
    id1 = upsert_comic(db, title="X-Men", issue="1", year=1963)
    id2 = upsert_comic(db, title="X-Men", issue="1", year=1963)
    assert id1 == id2


def test_upsert_comic_different_year_is_distinct_row(db):
    a = upsert_comic(db, "Hulk", "181", 1974)
    b = upsert_comic(db, "Hulk", "181", 1975)
    assert a != b
```

Replace `test_insert_bid_links_comic` (lines 74-79):

```python
def test_insert_bid_links_via_fmv_id(db):
    comic_id = upsert_comic(db, "Hulk", "181", 1974)
    fmv_id = upsert_fmv(db, comic_id, 9.0, 50.0, 70.0, 8, "high", "")
    bid_id = insert_bid(db, "987654321", 60.0, fmv_id, 6, 0, "seller2")
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fmv_id
```

Replace `test_upsert_comic_persists_locg_ids` (lines 170-181):

```python
def test_upsert_comic_persists_locg_ids(db):
    comic_id = upsert_comic(
        db, title="Amazing Spider-Man", issue="300", year=1988,
        locg_id=6977652, locg_variant_id=6977652,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] == 6977652
    assert row["locg_variant_id"] == 6977652
```

Replace `test_upsert_comic_locg_ids_default_to_null` (lines 183-192):

```python
def test_upsert_comic_locg_ids_default_to_null(db):
    comic_id = upsert_comic(db, title="Hulk", issue="181", year=1974)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] is None
    assert row["locg_variant_id"] is None
```

Replace `test_upsert_comic_locg_ids_preserved_on_conflict` (lines 195-212):

```python
def test_upsert_comic_locg_ids_preserved_on_conflict(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, locg_id=12345, locg_variant_id=67890)
    id2 = upsert_comic(db, "X-Men", "1", 1963)
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345
    assert row["locg_variant_id"] == 67890
```

Replace `test_upsert_comic_locg_ids_updated_when_provided` (lines 215-232):

```python
def test_upsert_comic_locg_ids_updated_when_provided(db):
    id1 = upsert_comic(db, "Spawn", "1", 1992, locg_id=100)
    id2 = upsert_comic(db, "Spawn", "1", 1992, locg_id=200, locg_variant_id=300)
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 200
    assert row["locg_variant_id"] == 300
```

Replace the `_make_lot` helper (lines 239-247):

```python
def _make_lot(db, item_id="900000001", n=3, series="Daredevil: The Man Without Fear"):
    """Insert a bid + N comics (each with one fmv row at grade 9.0).
    Returns (bid_id, [fmv_id, ...])."""
    fmv_ids = []
    for i in range(1, n + 1):
        cid = upsert_comic(db, series, str(i), 1993)
        fmv_ids.append(upsert_fmv(db, cid, 9.0, None, None, None, None, None))
    bid_id = insert_bid(db, item_id, 100.0, fmv_ids[0], 6, 0, "s")
    return bid_id, fmv_ids
```

Replace the entire block of junction tests `test_link_comic_to_bid_basic` through `test_get_primary_comic_for_bid_none_when_only_secondary` (around lines 250-335) with:

```python
def test_link_fmv_to_bid_creates_junction(db):
    bid_id, fmv_ids = _make_lot(db, n=2)
    link_fmv_to_bid(db, bid_id, fmv_ids[1])
    rows = db.execute(
        "SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchall()
    fmv_ids_in_junction = {r["fmv_id"] for r in rows}
    assert fmv_ids[1] in fmv_ids_in_junction


def test_link_fmv_to_bid_idempotent_secondary(db):
    bid_id, fmv_ids = _make_lot(db, n=1)
    link_fmv_to_bid(db, bid_id, fmv_ids[0])
    link_fmv_to_bid(db, bid_id, fmv_ids[0])
    n = db.execute(
        "SELECT COUNT(*) AS n FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchone()["n"]
    assert n == 1
```

Replace `test_migration_backfills_bid_comics_from_legacy_bids` and `test_migration_backfill_is_idempotent` (around lines 338-385) with:

```python
def test_post_migration_bid_fmvs_mirrors_primary_linkage(tmp_path):
    """After the FMV-split migration, every bid with fmv_id NOT NULL has a
    matching primary row in bid_fmvs. Mirrors the pre-FMV-split test that
    bid_comics carried the primary pointer."""
    path = tmp_path / "post.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low) "
        "VALUES (1, 'Hulk', '181', 1974, 9.0, 50)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, '111', 1, 60)"
    )
    conn.execute(
        "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)"
    )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        bid = new.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
        assert bid["fmv_id"] is not None
        junc = new.execute(
            "SELECT is_primary FROM bid_fmvs WHERE bid_id=1 AND fmv_id=?",
            (bid["fmv_id"],),
        ).fetchone()
        assert junc is not None
        assert junc["is_primary"] == 1
    finally:
        new.close()
```

- [ ] **Step 2: Run the updated tests, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py::test_upsert_comic_inserts -v`
Expected: FAIL — `upsert_comic` still requires legacy positional args.

- [ ] **Step 3: Rewrite `upsert_comic`, `insert_bid`, `list_comics`**

In `server/db.py`, replace `upsert_comic` (lines 114-154):

```python
def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
) -> int:
    """Upsert a comic identity row keyed by (title, issue, year). Returns id.

    Per-grade FMV lives in the `fmv` table — call `upsert_fmv(conn, id, grade, ...)`."""
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, locg_id, locg_variant_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year) DO UPDATE SET
            locg_id         = COALESCE(excluded.locg_id,         locg_id),
            locg_variant_id = COALESCE(excluded.locg_variant_id, locg_variant_id)
        """,
        (title, issue, year, locg_id, locg_variant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year=?",
        (title, issue, year),
    ).fetchone()
    return row["id"]
```

Replace `insert_bid` (lines 157-174). The third positional is now `fmv_id`:

```python
def insert_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    fmv_id: int | None,
    bid_offset: int,
    snipe_group: int,
    seller: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, fmv_id, bid_offset, snipe_group, seller)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, fmv_id, bid_offset, snipe_group, seller),
    )
    conn.commit()
    return cur.lastrowid
```

Replace `list_comics` (lines 322-346):

```python
def list_comics(
    conn: sqlite3.Connection,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
) -> list[sqlite3.Row]:
    """List comic identity rows. If `grade` is supplied, JOIN fmv and return
    valuation columns inline (renamed to the legacy `fmv_*` keys for response
    compatibility). Without `grade`, returns identity rows only."""
    clauses, params = [], []
    if title is not None:
        clauses.append("LOWER(c.title) = LOWER(?)")
        params.append(title)
    if issue is not None:
        clauses.append("c.issue = ?")
        params.append(issue)
    if year is not None:
        clauses.append("c.year = ?")
        params.append(year)

    if grade is not None:
        clauses.append("f.grade = ?")
        params.append(grade)
        sql = f"""
            SELECT c.*,
                   f.grade,
                   f.low  AS fmv_low,
                   f.high AS fmv_high,
                   f.comps AS fmv_comps,
                   f.confidence AS fmv_confidence,
                   f.notes AS fmv_notes,
                   f.updated_at AS fmv_updated_at
            FROM comics c
            JOIN fmv f ON f.comic_id = c.id
            {"WHERE " + " AND ".join(clauses) if clauses else ""}
            ORDER BY c.id
        """
    else:
        sql = f"""
            SELECT c.* FROM comics c
            {"WHERE " + " AND ".join(clauses) if clauses else ""}
            ORDER BY c.id
        """
    return conn.execute(sql, params).fetchall()
```

- [ ] **Step 4: Replace `get_comics_for_bid` / `get_primary_comic_for_bid` and delete `link_comic_to_bid`**

In `server/db.py`, replace the current `get_comics_for_bid` and `get_primary_comic_for_bid` functions (lines 216-242) with the two new fmv-keyed helpers below, and **delete the old `link_comic_to_bid` function** (currently lines 184-213) outright — no shim, no replacement. Production callers move to `link_fmv_to_bid` in Tasks 6 + 8; the legacy tests have already been rewritten in Step 1 above.

```python
def get_fmvs_for_bid(conn: sqlite3.Connection, bid_id: int) -> list[sqlite3.Row]:
    """All fmv rows linked to a bid via bid_fmvs, JOINed with comic identity.
    Primary first, then by numeric issue order."""
    return conn.execute(
        """
        SELECT f.id AS fmv_id, f.comic_id, f.grade,
               f.low, f.high, f.comps, f.confidence, f.notes, f.updated_at,
               c.title, c.issue, c.year, c.locg_id, c.locg_variant_id,
               bf.is_primary
        FROM bid_fmvs bf
        JOIN fmv    f ON f.id = bf.fmv_id
        JOIN comics c ON c.id = f.comic_id
        WHERE bf.bid_id = ?
        ORDER BY bf.is_primary DESC,
                 CAST(c.issue AS INTEGER),
                 c.issue
        """,
        (bid_id,),
    ).fetchall()


def get_primary_fmv_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT f.id AS fmv_id, f.comic_id, f.grade,
               f.low, f.high, f.comps, f.confidence, f.notes,
               c.title, c.issue, c.year
        FROM bid_fmvs bf
        JOIN fmv    f ON f.id = bf.fmv_id
        JOIN comics c ON c.id = f.comic_id
        WHERE bf.bid_id = ? AND bf.is_primary = 1
        LIMIT 1
        """,
        (bid_id,),
    ).fetchone()
```

Then delete the entire `link_comic_to_bid` function (currently lines 184-213). The diff is a straight removal:

```python
# DELETE this entire block:
def link_comic_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    comic_id: int,
    is_primary: bool = False,
) -> None:
    """Add a comic to a bid's set. If is_primary, demote any prior primary,
    promote this one, and mirror to bids.comic_id (backward-compat pointer)."""
    if is_primary:
        conn.execute(
            "UPDATE bid_comics SET is_primary=0 WHERE bid_id=? AND comic_id != ?",
            (bid_id, comic_id),
        )
        conn.execute(
            """
            INSERT INTO bid_comics (bid_id, comic_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(bid_id, comic_id) DO UPDATE SET is_primary = 1
            """,
            (bid_id, comic_id),
        )
        conn.execute("UPDATE bids SET comic_id=? WHERE id=?", (comic_id, bid_id))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary) VALUES (?, ?, 0)",
            (bid_id, comic_id),
        )
    conn.commit()
```

After this step the legacy junction helper is gone from the codebase entirely.

- [ ] **Step 5: Run all db tests**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add server/db.py tests/test_server_db.py
git commit -m "Rewrite upsert_comic identity-only; insert_bid takes fmv_id; bid_fmvs helpers replace bid_comics"
```

---

## Task 6: Route `POST /api/comics` + `POST /api/bids` through `upsert_fmv` + `bids.fmv_id`

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/main.py` (imports + handlers at lines 743-830)
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_api.py`

`UpsertComicRequest` and `AddBidRequest` Pydantic shapes do not change. The handler maps `req.fmv_low → upsert_fmv(... low=req.fmv_low ...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_api.py`:

```python
def test_post_comics_writes_identity_and_fmv(api):
    payload = {
        "title": "ASM", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800.0, "fmv_high": 1000.0,
        "fmv_comps": 12, "fmv_confidence": "high", "fmv_notes": "key",
    }
    r = api.post("/api/comics", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "ASM"
    rows = api.get(
        "/api/comics",
        params={"title": "ASM", "issue": "300", "year": 1988, "grade": 9.2},
    ).json()
    assert len(rows) == 1
    assert rows[0]["fmv_low"] == 800.0
    assert rows[0]["fmv_confidence"] == "high"


def test_post_comics_no_grade_writes_identity_only(api):
    r = api.post("/api/comics", json={"title": "Hulk", "issue": "181", "year": 1974})
    assert r.status_code == 200
    rows = api.get("/api/comics", params={"title": "Hulk", "grade": 9.0}).json()
    assert rows == []


def test_post_comics_grade_only_creates_fmv_stub(api):
    """Grade supplied with no valuation fields → fmv row created with NULL
    low/high (preserves the FK invariant for future bids)."""
    r = api.post("/api/comics", json={
        "title": "X-Men", "issue": "1", "year": 1963, "grade": 8.0,
    })
    assert r.status_code == 200
    rows = api.get(
        "/api/comics",
        params={"title": "X-Men", "issue": "1", "year": 1963, "grade": 8.0},
    ).json()
    assert len(rows) == 1
    assert rows[0]["fmv_low"] is None
    assert rows[0]["fmv_high"] is None
    assert rows[0]["grade"] == 8.0


def test_add_bid_routes_to_fmv_id(api):
    payload = {
        "item_id": "999111222",
        "max_bid": 50.0,
        "comic": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0,
        "fmv_low": 50.0, "fmv_high": 70.0,
        "fmv_comps": 8, "fmv_confidence": "high",
    }
    r = api.post("/api/bids", json=payload)
    assert r.status_code == 200
    bid_id = r.json()["id"]
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.row_factory = sqlite3.Row
    bid = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert bid["fmv_id"] is not None
    fmv = db.execute(
        "SELECT comic_id, grade, low FROM fmv WHERE id=?", (bid["fmv_id"],)
    ).fetchone()
    assert fmv["grade"] == 9.0
    assert fmv["low"] == 50.0
    comic = db.execute(
        "SELECT title, issue, year FROM comics WHERE id=?", (fmv["comic_id"],)
    ).fetchone()
    assert (comic["title"], comic["issue"], comic["year"]) == ("Hulk", "181", 1974)
    cols = {row[1] for row in db.execute("PRAGMA table_info(comics)")}
    assert "grade" not in cols
    assert "fmv_low" not in cols
    bid_cols = {row[1] for row in db.execute("PRAGMA table_info(bids)")}
    assert "comic_id" not in bid_cols


def test_add_bid_grade_no_fmv_still_links(api):
    """Grade without FMV: fmv row created with NULL low/high, bid still gets
    fmv_id set, warning fires."""
    r = api.post("/api/bids", json={
        "item_id": "999111223",
        "max_bid": 50.0,
        "comic": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["fmv_id"] is not None
    assert body.get("warning") is not None
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.row_factory = sqlite3.Row
    fmv = db.execute("SELECT low FROM fmv WHERE id=?", (body["fmv_id"],)).fetchone()
    assert fmv["low"] is None


def test_add_bid_no_comic_leaves_fmv_id_null(api):
    """Bid with no comic flags → fmv_id NULL, no warning."""
    r = api.post("/api/bids", json={"item_id": "999111224", "max_bid": 25.0})
    assert r.status_code == 200
    body = r.json()
    assert body.get("fmv_id") is None
    assert body.get("warning") is None
```

Also update the existing `test_upsert_comic` and `test_upsert_comic_twice_updates` (lines 82-108):

```python
def test_upsert_comic(api):
    payload = {
        "title": "Amazing Spider-Man", "issue": "300", "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0, "fmv_high": 1000.0,
        "fmv_comps": 12, "fmv_confidence": "high", "fmv_notes": "Key issue",
    }
    r = api.post("/api/comics", json=payload)
    assert r.status_code == 200
    rows = api.get(
        "/api/comics",
        params={"title": "Amazing Spider-Man", "issue": "300", "year": 1988, "grade": 9.2},
    ).json()
    assert rows[0]["fmv_low"] == 800.0
    assert rows[0]["fmv_confidence"] == "high"


def test_upsert_comic_twice_updates(api):
    payload = {"title": "X-Men", "issue": "1", "year": 1963,
               "grade": 8.0, "fmv_low": 500.0, "fmv_high": 700.0,
               "fmv_comps": 5, "fmv_confidence": "medium", "fmv_notes": ""}
    api.post("/api/comics", json=payload)
    payload["fmv_low"] = 550.0
    api.post("/api/comics", json=payload)
    rows = api.get(
        "/api/comics",
        params={"title": "X-Men", "issue": "1", "year": 1963, "grade": 8.0},
    ).json()
    assert rows[0]["fmv_low"] == 550.0
```

Update `test_add_bid_with_comic_links_fmv` and `test_add_bid_with_fmv_no_warning` so they assert on `data["fmv_id"]` instead of `data["comic_id"]`. (Quickest path: rename the assertion field — every other expectation stays.)

- [ ] **Step 2: Run, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "upsert_comic or add_bid or post_comics" -v`
Expected: failures because `api_upsert_comic` still calls the old `upsert_comic` signature.

- [ ] **Step 3: Update imports and `api_upsert_comic`**

Replace the imports at lines 22-28:

```python
from server.db import (
    DB_PATH, init_db, upsert_comic, upsert_fmv, set_bid_fmv,
    get_fmv_for_bid, link_fmv_to_bid, list_comics, insert_bid,
    get_bid_by_item_id, update_bid, update_bid_status, delete_bid,
    get_all_bids, get_pending_bids, mark_bids_purged, cache_gixen_data,
    set_auction_end_time, get_bids_ready_to_snipe, set_local_snipe_result,
    get_fmvs_for_bid, get_primary_fmv_for_bid,
)
```

Replace `api_upsert_comic` (lines 743-761):

```python
@app.post("/api/comics")
async def api_upsert_comic(req: UpsertComicRequest):
    """Upsert a comic identity (title/issue/year) and, if grade is supplied,
    its per-grade fmv row. Flat shape kept for backward compat."""
    db = _get_db()
    comic_id = upsert_comic(
        db, title=req.title, issue=req.issue, year=req.year,
        locg_id=req.locg_id, locg_variant_id=req.locg_variant_id,
    )
    if req.grade is not None:
        # Always create the fmv row when a grade is given, even with NULL
        # valuation. Schema invariant: a recorded grade must have an fmv row.
        upsert_fmv(
            db, comic_id=comic_id, grade=req.grade,
            low=req.fmv_low, high=req.fmv_high, comps=req.fmv_comps,
            confidence=req.fmv_confidence, notes=req.fmv_notes,
        )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    return dict(row)
```

- [ ] **Step 4: Update `api_add_bid`**

Replace the comic-resolution block (lines 768-783):

```python
    fmv_id: int | None = None
    if req.comic and req.issue and req.year is not None:
        comic_id = upsert_comic(
            db, title=req.comic, issue=req.issue, year=req.year,
            locg_id=req.locg_id, locg_variant_id=req.locg_variant_id,
        )
        if req.grade is not None:
            # Caller (CLI / skill) supplied a grade explicitly, so materialize
            # the (comic, grade) fmv row. NULL low/high is fine here: the
            # caller has opted in to recording the grade. See Caveat #2 — the
            # skill layer enforces that "silent on grade" produces no grade
            # in this request, so we never reach this branch for silent flows.
            fmv_id = upsert_fmv(
                db, comic_id=comic_id, grade=req.grade,
                low=req.fmv_low, high=req.fmv_high, comps=req.fmv_comps,
                confidence=req.fmv_confidence, notes=req.fmv_notes,
            )
```

Replace the `insert_bid(...)` call and surrounding result block (lines 799-808):

```python
    bid_id = insert_bid(
        db,
        item_id=req.item_id,
        max_bid=req.max_bid,
        fmv_id=fmv_id,
        bid_offset=req.bid_offset,
        snipe_group=req.snipe_group,
        seller=None,
    )
    if fmv_id is not None:
        link_fmv_to_bid(db, bid_id, fmv_id, is_primary=True)
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    result = dict(row)
```

Replace the FMV-warning block (lines 811-828):

```python
    # Surface a warning if this bid's fmv row has no valuation. Same dashboard
    # consequence as before (renders '—'); now the trigger is fmv.low IS NULL.
    if fmv_id is not None:
        fmv_row = db.execute(
            "SELECT comic_id, low FROM fmv WHERE id=?", (fmv_id,)
        ).fetchone()
        if fmv_row is not None and fmv_row["low"] is None:
            comic_row = db.execute(
                "SELECT title, issue FROM comics WHERE id=?",
                (fmv_row["comic_id"],),
            ).fetchone()
            logger.warning(
                "bid added with no FMV for item_id=%s fmv_id=%s — "
                "dashboard will render '—' for this row.",
                req.item_id, fmv_id,
            )
            result["warning"] = (
                f"fmv record for {comic_row['title']} #{comic_row['issue']} "
                f"at grade {req.grade} has no valuation (low IS NULL). "
                f"Dashboard will render '—'. Run /comic:fmv or POST "
                f"/api/comics with FMV fields to fix."
            )
```

- [ ] **Step 5: Run tests, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "upsert_comic or add_bid or post_comics" -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "Route POST /api/comics + /api/bids through upsert_fmv and bids.fmv_id"
```

---

## Task 7: Rewrite `/api/snipes`, `/api/history`, `/api/bids` JOIN through `bids.fmv_id`

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/main.py` (handlers at lines 833-1027)
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_api.py`

Response field names stay (`comic_title`, `comic_issue`, `comic_year`, `comic_grade`, `fmv_low`, `fmv_high`, `fmv_comps`, `fmv_confidence`, `fmv_notes`) so the dashboard JS needs no changes. The flat-fields `comic_id` is replaced by `fmv_id` (verify `grep -rn "\.comic_id" server/static/` shows no top-level usage before merging — the JS reads `comic_id` only inside `r.comics[]`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_api.py`:

```python
def test_get_snipes_joins_fmv_via_fmv_id(api):
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/comics", json={
        "title": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0, "fmv_low": 50.0, "fmv_high": 70.0,
        "fmv_comps": 8, "fmv_confidence": "high",
    })
    api.post("/api/comics", json={
        "title": "Hulk", "issue": "181", "year": 1974,
        "grade": 7.0, "fmv_low": 20.0, "fmv_high": 30.0,
        "fmv_comps": 5, "fmv_confidence": "medium",
    })
    api.post("/api/bids", json={
        "item_id": "888000001", "max_bid": 60.0,
        "comic": "Hulk", "issue": "181", "year": 1974, "grade": 9.0,
    })
    api.post("/api/bids", json={
        "item_id": "888000002", "max_bid": 25.0,
        "comic": "Hulk", "issue": "181", "year": 1974, "grade": 7.0,
    })

    snipes = api.get("/api/snipes").json()
    by_item = {s["item_id"]: s for s in snipes}
    assert by_item["888000001"]["fmv_low"] == 50.0
    assert by_item["888000002"]["fmv_low"] == 20.0
    assert by_item["888000001"]["comic_grade"] == 9.0
    assert by_item["888000002"]["comic_grade"] == 7.0


def test_get_snipes_null_valuation_surfaces_warning(api):
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/bids", json={
        "item_id": "888000003", "max_bid": 1500.0,
        "comic": "ASM", "issue": "300", "year": 1988, "grade": 9.4,
    })
    snipes = api.get("/api/snipes").json()
    by_item = {s["item_id"]: s for s in snipes}
    assert by_item["888000003"]["fmv_low"] is None
    assert by_item["888000003"]["comic_grade"] == 9.4
    assert by_item["888000003"].get("fmv_warning") is not None


def test_get_snipes_unclassified_bid_has_no_warning(api):
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/bids", json={"item_id": "888000004", "max_bid": 250.0})
    snipes = api.get("/api/snipes").json()
    by_item = {s["item_id"]: s for s in snipes}
    assert by_item["888000004"]["fmv_low"] is None
    assert by_item["888000004"]["comic_grade"] is None
    assert by_item["888000004"].get("fmv_warning") is None
```

Update the existing `test_get_snipes_merges_fmv` (lines 235-270):

```python
def test_get_snipes_merges_fmv(api):
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/comics", json={
        "title": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0, "fmv_low": 50.0, "fmv_high": 70.0,
        "fmv_comps": 8, "fmv_confidence": "high",
    })
    api.post("/api/bids", json={
        "item_id": "987654321", "max_bid": 60.0,
        "comic": "Hulk", "issue": "181", "year": 1974, "grade": 9.0,
    })
    snipes = api.get("/api/snipes").json()
    assert len(snipes) == 1
    assert snipes[0]["fmv_low"] == 50.0
    assert snipes[0]["fmv_confidence"] == "high"
    assert snipes[0]["comic_grade"] == 9.0
```

- [ ] **Step 2: Run, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "get_snipes" -v`
Expected: failures — SELECT still references `bids.comic_id`.

- [ ] **Step 3: Rewrite `api_get_snipes`**

Replace the main SELECT (lines 845-855):

```python
    rows = db.execute("""
        SELECT b.*,
               c.title AS comic_title,
               c.issue AS comic_issue,
               c.year  AS comic_year,
               f.grade AS comic_grade,
               f.low   AS fmv_low,
               f.high  AS fmv_high,
               f.comps AS fmv_comps,
               f.confidence AS fmv_confidence,
               f.notes      AS fmv_notes,
               c.locg_id, c.locg_variant_id
        FROM bids b
        LEFT JOIN fmv    f ON f.id = b.fmv_id
        LEFT JOIN comics c ON c.id = f.comic_id
        WHERE b.status != 'PURGED'
        ORDER BY b.added_at DESC
    """).fetchall()
```

Replace the lot-aware second-query block (lines 864-887):

```python
    bid_ids = [r["id"] for r in rows]
    comics_by_bid: dict[int, list[dict]] = {bid_id: [] for bid_id in bid_ids}
    if bid_ids:
        placeholders = ",".join("?" * len(bid_ids))
        comic_rows = db.execute(
            f"""
            SELECT bf.bid_id, bf.is_primary,
                   f.id AS fmv_id, f.comic_id, f.grade,
                   c.title, c.issue, c.year,
                   c.locg_id, c.locg_variant_id
            FROM bid_fmvs bf
            JOIN fmv    f ON f.id = bf.fmv_id
            JOIN comics c ON c.id = f.comic_id
            WHERE bf.bid_id IN ({placeholders})
            ORDER BY bf.bid_id, bf.is_primary DESC,
                     CAST(c.issue AS INTEGER), c.issue
            """,
            bid_ids,
        ).fetchall()
        for cr in comic_rows:
            comics_by_bid[cr["bid_id"]].append({
                "fmv_id": cr["fmv_id"],
                "comic_id": cr["comic_id"],
                "title": cr["title"],
                "issue": cr["issue"],
                "year": cr["year"],
                "grade": cr["grade"],
                "locg_id": cr["locg_id"],
                "locg_variant_id": cr["locg_variant_id"],
                "is_primary": bool(cr["is_primary"]),
            })
```

Replace the result-building loop (lines 889-925):

```python
    result = []
    for row in rows:
        item = dict(row)
        end_date_iso = item.get("auction_end_at")
        title = item.get("ebay_title") or item.get("comic_title") or ""
        fmv_warning = None
        if item.get("fmv_id") is not None and item.get("fmv_low") is None:
            fmv_warning = (
                f"no FMV at grade {item.get('comic_grade')} for "
                f"{item.get('comic_title') or '?'} #{item.get('comic_issue') or '?'}"
            )
        result.append({
            "item_id": item["item_id"],
            "title": title,
            "current_bid": item.get("cached_current_bid"),
            "max_bid": f"{item['max_bid']:.2f} USD",
            "bid_offset": item["bid_offset"],
            "snipe_group": item["snipe_group"],
            "time_to_end": _iso_to_relative(end_date_iso),
            "end_date_iso": end_date_iso,
            "status": item["status"],
            "status_mirror": item.get("status_mirror"),
            "winning_bid": item.get("winning_bid"),
            "seller": item.get("seller"),
            "cached_at": item.get("cached_at"),
            "comic_title": item.get("comic_title"),
            "comic_issue": item.get("comic_issue"),
            "comic_year": item.get("comic_year"),
            "comic_grade": item.get("comic_grade"),
            "fmv_low": item.get("fmv_low"),
            "fmv_high": item.get("fmv_high"),
            "fmv_comps": item.get("fmv_comps"),
            "fmv_confidence": item.get("fmv_confidence"),
            "fmv_notes": item.get("fmv_notes"),
            "fmv_warning": fmv_warning,
            "fmv_id": item.get("fmv_id"),
            "locg_id": item.get("locg_id"),
            "locg_variant_id": item.get("locg_variant_id"),
            "local_snipe_at": item.get("local_snipe_at"),
            "local_snipe_result": item.get("local_snipe_result"),
            "comics": comics_by_bid.get(item["id"], []),
        })
```

- [ ] **Step 4: Apply the same SELECT swap to `api_get_history`**

Replace the SELECT (lines 934-952):

```python
    rows = db.execute("""
        SELECT b.*,
               c.title AS comic_title,
               c.issue AS comic_issue,
               c.year  AS comic_year,
               f.grade AS comic_grade,
               f.low   AS fmv_low,
               f.high  AS fmv_high,
               f.comps AS fmv_comps,
               f.confidence AS fmv_confidence,
               f.notes      AS fmv_notes,
               c.locg_id, c.locg_variant_id
        FROM bids b
        LEFT JOIN fmv    f ON f.id = b.fmv_id
        LEFT JOIN comics c ON c.id = f.comic_id
        WHERE (
          b.auction_end_at IS NOT NULL
          AND datetime(b.auction_end_at) <= datetime('now')
          AND datetime(b.auction_end_at) >= datetime('now', '-7 days')
        ) OR (
          b.auction_end_at IS NULL
          AND b.resolved_at IS NOT NULL
          AND datetime(b.resolved_at) >= datetime('now', '-7 days')
        )
        ORDER BY COALESCE(b.auction_end_at, b.resolved_at) DESC
    """).fetchall()
```

Add `fmv_warning` to the response dict (same logic as in snipes) and swap `comic_id` for `fmv_id`.

- [ ] **Step 5: Apply the same SELECT swap to `api_get_all_bids`**

Replace the SELECT (lines 995-1004):

```python
    rows = db.execute("""
        SELECT b.*,
               c.title AS comic_title,
               c.issue AS comic_issue,
               c.year  AS comic_year,
               f.grade AS comic_grade,
               f.low   AS fmv_low,
               f.high  AS fmv_high,
               f.comps AS fmv_comps,
               f.confidence AS fmv_confidence,
               f.notes      AS fmv_notes,
               c.locg_id, c.locg_variant_id
        FROM bids b
        LEFT JOIN fmv    f ON f.id = b.fmv_id
        LEFT JOIN comics c ON c.id = f.comic_id
        ORDER BY COALESCE(b.auction_end_at, b.added_at) DESC
    """).fetchall()
```

Swap `comic_id` → `fmv_id` in the response dict.

- [ ] **Step 6: Run all snipes/history/bids tests**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "get_snipes or get_history or get_all_bids" -v`
Expected: all PASS.

- [ ] **Step 7: Full test suite**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py tests/test_server_api.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "JOIN fmv via bids.fmv_id in snipes/history/bids endpoints"
```

---

## Task 8: Update `api_extract_comics` and `api_link_locg`

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/main.py` (handlers at lines 1089-1177 and 1249-1329)
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_api.py`

`api_extract_comics` parses titles, creates `comics`, and — when the title parser extracts a grade — manufactures an `fmv` stub at that grade (NULL valuation) and links each bid. The grade comes from the title text itself, so manufacturing the `fmv` row is consistent with the Caveat #2 rule: the row represents data explicitly present in the listing, not an implicit fallback. If the parser doesn't extract a grade, the bid stays at `fmv_id = NULL` — the legitimate unclassified state. `api_link_locg`'s auto-create branch uses the new identity-only `upsert_comic` and manufactures an `fmv` row at the bid's primary grade (which the operator explicitly chose by linking LOCG to this bid).

- [ ] **Step 1: Update `api_extract_comics` WHERE filter**

Replace lines 1259-1268:

```python
    rows = db.execute(
        """
        SELECT id, item_id, ebay_title
        FROM bids
        WHERE fmv_id IS NULL
          AND ebay_title IS NOT NULL
          AND ebay_title != ''
          AND status != 'PURGED'
        """
    ).fetchall()
```

- [ ] **Step 2: Update the per-bid try/except block**

Replace the block at lines 1303-1322:

```python
        try:
            for idx, issue in enumerate(issues):
                comic_id = upsert_comic(
                    db, title=parsed.series, issue=issue, year=parsed.year,
                )
                if parsed.grade is not None:
                    # Manufacture an fmv stub at the parsed grade (NULL
                    # valuation — parser doesn't supply FMV).
                    fid = upsert_fmv(
                        db, comic_id=comic_id, grade=parsed.grade,
                        low=None, high=None, comps=None,
                        confidence=None,
                        notes=f"auto-linked from eBay title (confidence={parsed.confidence})",
                    )
                    link_fmv_to_bid(db, row["id"], fid, is_primary=(idx == 0))
            linked += 1
        except Exception as e:
            errors.append({"item_id": item_id, "error": f"link failed: {e}"})
```

(If `parsed.grade is None`, the comic identity is still upserted but no fmv row is created and the bid stays unclassified. The next `extract-comics` run picks it up once a grade is known.)

- [ ] **Step 3: Update `api_link_locg` to use fmv linkage**

In `server/main.py`, the existing `api_link_locg` handler references `bid_row["comic_id"]` and `get_primary_comic_for_bid`. Replace all such references with `fmv_id` and `get_primary_fmv_for_bid`. The full handler becomes (replacing lines 1089-1177):

```python
@app.post("/api/bids/{item_id}/comics/locg")
async def api_link_locg(item_id: str, req: LocgLinkRequest):
    """Persist a resolved LOCG ID against a specific comic in a bid's set.

    Without `issue`: target the bid's primary comic (resolved via fmv_id).
    With `issue`: find a comic in the bid's junction matching that issue;
    if missing, auto-upsert one using the primary's series/year, manufacture
    an fmv stub at the primary's grade, and link as non-primary."""
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()

    bid_row = get_bid_by_item_id(db, item_id)
    if bid_row is None:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in DB")

    target_comic_id: int | None = None

    if req.issue is not None:
        match = db.execute(
            """
            SELECT c.id
            FROM bid_fmvs bf
            JOIN fmv    f ON f.id = bf.fmv_id
            JOIN comics c ON c.id = f.comic_id
            WHERE bf.bid_id = ? AND c.issue = ?
            LIMIT 1
            """,
            (bid_row["id"], req.issue),
        ).fetchone()
        if match:
            target_comic_id = match["id"]
        else:
            primary = get_primary_fmv_for_bid(db, bid_row["id"])
            if primary is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Bid {item_id} has no primary fmv linkage; cannot infer "
                        "series/year for auto-create. Run extract-comics first."
                    ),
                )
            target_comic_id = upsert_comic(
                db, title=primary["title"], issue=req.issue, year=primary["year"],
            )
            # Manufacture an fmv stub at the primary's grade so the junction
            # row can land. Inherits the bid's grade by convention.
            stub_fmv = upsert_fmv(
                db, comic_id=target_comic_id, grade=primary["grade"],
                low=None, high=None, comps=None,
                confidence=None,
                notes="auto-linked via locg-link",
            )
            link_fmv_to_bid(db, bid_row["id"], stub_fmv, is_primary=False)
    else:
        if bid_row["fmv_id"] is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bid {item_id} has no primary fmv linkage. Pass --issue "
                    "to target a specific issue or run extract-comics first."
                ),
            )
        primary_fmv_row = db.execute(
            "SELECT comic_id FROM fmv WHERE id=?", (bid_row["fmv_id"],)
        ).fetchone()
        target_comic_id = primary_fmv_row["comic_id"]

    db.execute(
        """
        UPDATE comics
        SET locg_id = ?,
            locg_variant_id = COALESCE(?, locg_variant_id)
        WHERE id = ?
        """,
        (req.locg_id, req.locg_variant_id, target_comic_id),
    )
    db.commit()

    row = db.execute(
        "SELECT id AS comic_id, title, issue, year, locg_id, locg_variant_id "
        "FROM comics WHERE id = ?",
        (target_comic_id,),
    ).fetchone()
    # is_primary derived from bid's fmv_id's comic
    is_primary = False
    if bid_row["fmv_id"] is not None:
        primary_fmv_row = db.execute(
            "SELECT comic_id FROM fmv WHERE id=?", (bid_row["fmv_id"],)
        ).fetchone()
        is_primary = (primary_fmv_row["comic_id"] == target_comic_id)
    return {**dict(row), "is_primary": is_primary}
```

Also update `api_edit_bid`'s locg-update block (lines 1053-1067). Replace `bid_row["comic_id"]` with the resolved primary comic id:

```python
    if req.locg_id is not None or req.locg_variant_id is not None:
        bid_row = get_bid_by_item_id(db, item_id)
        target_comic_id: int | None = None
        if bid_row is not None and bid_row["fmv_id"] is not None:
            fmv_row = db.execute(
                "SELECT comic_id FROM fmv WHERE id=?", (bid_row["fmv_id"],)
            ).fetchone()
            if fmv_row is not None:
                target_comic_id = fmv_row["comic_id"]
        if target_comic_id is not None:
            db.execute(
                """
                UPDATE comics
                SET locg_id = COALESCE(?, locg_id),
                    locg_variant_id = COALESCE(?, locg_variant_id)
                WHERE id = ?
                """,
                (req.locg_id, req.locg_variant_id, target_comic_id),
            )
            db.commit()
```

- [ ] **Step 4: Add a regression test**

Append to `tests/test_server_api.py`:

```python
def test_extract_comics_writes_fmv_stub_with_grade(api):
    api.mock_gixen.list_snipes.return_value = []
    r = api.post("/api/bids", json={"item_id": "777000111", "max_bid": 50.0})
    assert r.status_code == 200
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.row_factory = sqlite3.Row
    db.execute(
        "UPDATE bids SET ebay_title=? WHERE item_id=?",
        ("Amazing Spider-Man #300 1988 CGC 9.4", "777000111"),
    )
    db.commit()

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    bid = db.execute(
        "SELECT fmv_id FROM bids WHERE item_id=?", ("777000111",)
    ).fetchone()
    assert bid["fmv_id"] is not None
    fmv = db.execute(
        "SELECT grade, low FROM fmv WHERE id=?", (bid["fmv_id"],)
    ).fetchone()
    assert fmv["grade"] == 9.4
    assert fmv["low"] is None  # parser doesn't supply FMV
```

- [ ] **Step 5: Run, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "extract_comics or link_locg or link_l or extract_comics_writes_fmv_stub" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "Extract-comics manufactures fmv stub at parsed grade; locg-link routes through fmv_id"
```

---

## Task 8b: Update `snipe-add` skill to enforce explicit-opt-in for null-FMV rows

**Files:**
- Modify: `/Users/hsukenooi/Projects/Brain v3.0/.claude/commands/comic/snipe-add.md`

The schema's null-valuation `fmv` row is an explicit opt-in only (see Caveat #2). The `snipe-add` skill is the user-facing entry point that decides whether to materialize one. This task pins that rule into the skill so future revisions can't drift.

- [ ] **Step 1: Locate the skill's "FMV missing" decision branch**

Read `/Users/hsukenooi/Projects/Brain v3.0/.claude/commands/comic/snipe-add.md` end to end. Find the section that handles the "no FMV available yet" path (search for terms like "without FMV", "proceed", "null FMV", "skip FMV", or the section that describes what happens when `/comic:fmv` declines to produce a valuation). The skill currently leaves grade-only behaviour underspecified.

- [ ] **Step 2: Add a Caveat clause to the skill**

Append (or place inline near the FMV decision branch) a Caveat clause that reads roughly:

```markdown
## Caveat: null-valuation FMV rows are explicit opt-in only

When the user opts to add a snipe without FMV research:

- **If the user explicitly names a grade** ("add at grade 9.2 without FMV", "park at grade 7.0"): call `POST /api/bids` with `grade=<that grade>` and no `fmv_low`/`fmv_high`/etc. The server will materialize `fmv(comic_id, <grade>, low=NULL, high=NULL)` and set `bids.fmv_id` to it. The dashboard renders `—` and `fmv_warning` fires.
- **If the user does not name a grade** (silent on grade, or chooses "proceed without FMV" without specifying): call `POST /api/bids` with **no** `grade` and **no** FMV fields. `bids.fmv_id` stays NULL — the legitimate unclassified state. Do NOT manufacture an `fmv` row with a guessed grade.

Rationale: the database schema (post-2026-05-13 fmv split) makes a null-valuation `fmv` row a deliberate "we know the grade but haven't researched value" state. Filling it in implicitly — even with a sensible default like "9.0 from the listing" or "the modal grade in this run" — re-introduces the implicit-state class of bug the split was designed to eliminate. The skill must reflect the user's explicit choice, not a heuristic.
```

(The exact heading and wording can match the skill's existing tone; the rule itself is the load-bearing part.)

- [ ] **Step 3: Manually verify the skill reads cleanly end-to-end**

Read the modified skill top-to-bottom. Confirm the rule is visible at the point a future agent (or a fresh you) would need it — i.e. near the FMV decision branch, not buried in a postscript. If the skill has an existing "edge cases" or "common mistakes" section, also reference the new rule there.

- [ ] **Step 4: Commit (after Tasks 1-8 are merged into the gixen-cli repo)**

```bash
# This commit lives in the Brain v3.0 repo, not gixen-cli.
cd "/Users/hsukenooi/Projects/Brain v3.0"
git add .claude/commands/comic/snipe-add.md
git commit -m "snipe-add: null-FMV fmv rows are explicit opt-in only

Aligns the skill with the 2026-05-13 fmv split rule that a null-valuation
fmv(comic_id, grade) row exists only when the user explicitly names a grade
to park. Silent-on-grade flows must leave bids.fmv_id = NULL instead."
```

---

## Task 9: Optional precise endpoint `POST /api/comics/{id}/fmv`

**Files:**
- Modify: `/Users/hsukenooi/Projects/gixen-cli/server/main.py`
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_api.py`:

```python
def test_post_comic_fmv_upserts_per_grade(api):
    r = api.post("/api/comics", json={"title": "Hulk", "issue": "181", "year": 1974})
    comic_id = r.json()["id"]
    r = api.post(f"/api/comics/{comic_id}/fmv", json={
        "grade": 9.0, "low": 50.0, "high": 70.0,
        "comps": 8, "confidence": "high", "notes": "GPA",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["grade"] == 9.0
    assert body["low"] == 50.0


def test_post_comic_fmv_rejects_missing_grade(api):
    r = api.post("/api/comics", json={"title": "Hulk", "issue": "181", "year": 1974})
    comic_id = r.json()["id"]
    r = api.post(f"/api/comics/{comic_id}/fmv", json={"low": 50.0})
    assert r.status_code == 422


def test_post_comic_fmv_unknown_comic_returns_404(api):
    r = api.post("/api/comics/999999/fmv", json={"grade": 9.0, "low": 50.0})
    assert r.status_code == 404
```

- [ ] **Step 2: Run, verify failure**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "post_comic_fmv" -v`
Expected: 404 / endpoint-missing failures.

- [ ] **Step 3: Add request model + endpoint**

After `UpsertComicRequest` in `server/main.py` (around line 624), add:

```python
class UpsertFmvRequest(BaseModel):
    grade: float
    low: float | None = None
    high: float | None = None
    comps: int | None = None
    confidence: str | None = None
    notes: str | None = None

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: str | None) -> str | None:
        if v is not None and v not in ("high", "medium", "low"):
            raise ValueError("confidence must be high, medium, or low")
        return v
```

After `api_upsert_comic`, add:

```python
@app.post("/api/comics/{comic_id}/fmv")
async def api_upsert_comic_fmv(comic_id: int, req: UpsertFmvRequest):
    db = _get_db()
    if db.execute("SELECT 1 FROM comics WHERE id=?", (comic_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"comic_id {comic_id} not found")
    fmv_id = upsert_fmv(
        db, comic_id=comic_id, grade=req.grade,
        low=req.low, high=req.high, comps=req.comps,
        confidence=req.confidence, notes=req.notes,
    )
    row = db.execute("SELECT * FROM fmv WHERE id=?", (fmv_id,)).fetchone()
    return dict(row)
```

- [ ] **Step 4: Run, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py -k "post_comic_fmv" -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "Add POST /api/comics/{id}/fmv for precise per-grade upsert"
```

---

## Task 10: Real-incident regression test using the 2026-05-13 ghost-row pattern

**Files:**
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_db.py`

- [ ] **Step 1: Write the regression test**

Append to `tests/test_server_db.py`:

```python
def test_migration_recovers_2026_05_13_incident(tmp_path):
    """14 bids spread across 4 shadow comics rows after grade revisions.
    Post-migration: one comics identity, 4 fmv rows, all 14 bids point at the
    fmv row matching their original grade, and the original FMV at 9.0 is intact."""
    path = tmp_path / "incident.db"
    conn = _build_legacy_db(path)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, "
        "fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at, locg_id) "
        "VALUES (1, 'Spider-Man', '300', 1988, 9.0, 800, 1000, 12, 'high', "
        "'GPA Jan 2026', '2026-05-01T00:00:00', 99999)"
    )
    for shadow_id, grade in [(2, 9.2), (3, 8.0), (4, 9.4)]:
        conn.execute(
            "INSERT INTO comics (id, title, issue, year, grade) VALUES (?, 'Spider-Man', '300', 1988, ?)",
            (shadow_id, grade),
        )
    for i, comic_id in enumerate([1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 4]):
        conn.execute(
            "INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (?, ?, ?, 600)",
            (100 + i, f"99{i:07d}", comic_id),
        )
        conn.execute(
            "INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (?, ?, 1)",
            (100 + i, comic_id),
        )
    conn.commit()
    conn.close()

    new = init_db(path)
    try:
        survivor = new.execute(
            "SELECT id FROM comics WHERE title='Spider-Man' AND issue='300' AND year=1988"
        ).fetchone()
        assert survivor is not None

        fmvs = {r["grade"]: r["low"] for r in new.execute(
            "SELECT grade, low FROM fmv WHERE comic_id=?", (survivor["id"],)
        )}
        assert fmvs == {9.0: 800, 9.2: None, 8.0: None, 9.4: None}

        fmv_id_by_grade = {r["grade"]: r["id"] for r in new.execute(
            "SELECT id, grade FROM fmv WHERE comic_id=?", (survivor["id"],)
        )}
        all_bids = new.execute("SELECT item_id, fmv_id FROM bids").fetchall()
        assert all(b["fmv_id"] is not None for b in all_bids)
        item_to_grade = {f"99{i:07d}": g for i, g in enumerate(
            [9.0, 9.0, 9.0, 9.0, 9.2, 9.2, 9.2, 8.0, 8.0, 8.0, 9.4, 9.4, 9.4, 9.4]
        )}
        for b in all_bids:
            expected_fmv = fmv_id_by_grade[item_to_grade[b["item_id"]]]
            assert b["fmv_id"] == expected_fmv

        bid_at_9 = next(b for b in all_bids if item_to_grade[b["item_id"]] == 9.0)
        bid_id = new.execute(
            "SELECT id FROM bids WHERE item_id=?", (bid_at_9["item_id"],)
        ).fetchone()["id"]
        recovered = get_fmv_for_bid(new, bid_id)
        assert recovered["low"] == 800
    finally:
        new.close()
```

- [ ] **Step 2: Run, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py::test_migration_recovers_2026_05_13_incident -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_server_db.py
git commit -m "Regression: 2026-05-13 incident pattern survives FMV split migration"
```

---

## Task 11: End-to-end test — distinct FMV per grade survives the full request cycle

**Files:**
- Test: `/Users/hsukenooi/Projects/gixen-cli/tests/test_server_api.py`

- [ ] **Step 1: Write the e2e test**

Append to `tests/test_server_api.py`:

```python
def test_e2e_distinct_fmv_per_grade_on_same_comic(api):
    api.mock_gixen.list_snipes.return_value = []
    r1 = api.post("/api/comics", json={
        "title": "ASM", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800.0, "fmv_high": 1000.0,
        "fmv_comps": 12, "fmv_confidence": "high",
    })
    r2 = api.post("/api/comics", json={
        "title": "ASM", "issue": "300", "year": 1988,
        "grade": 7.0, "fmv_low": 200.0, "fmv_high": 300.0,
        "fmv_comps": 8, "fmv_confidence": "high",
    })
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]   # one identity

    api.post("/api/bids", json={
        "item_id": "555000001", "max_bid": 1500.0,
        "comic": "ASM", "issue": "300", "year": 1988, "grade": 9.2,
    })
    api.post("/api/bids", json={
        "item_id": "555000002", "max_bid": 400.0,
        "comic": "ASM", "issue": "300", "year": 1988, "grade": 7.0,
    })

    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    n = db.execute(
        "SELECT COUNT(*) FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()[0]
    assert n == 1

    snipes = api.get("/api/snipes").json()
    by_item = {s["item_id"]: s for s in snipes}
    assert by_item["555000001"]["fmv_low"] == 800.0
    assert by_item["555000002"]["fmv_low"] == 200.0
    assert by_item["555000001"]["fmv_id"] != by_item["555000002"]["fmv_id"]
```

- [ ] **Step 2: Run, verify pass**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_api.py::test_e2e_distinct_fmv_per_grade_on_same_comic -v`
Expected: PASS.

- [ ] **Step 3: Run full suite**

Run: `cd /Users/hsukenooi/Projects/gixen-cli && pytest tests/test_server_db.py tests/test_server_api.py tests/test_title_parser.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_server_api.py
git commit -m "E2E: same comic identity, distinct fmv rows per grade across bids"
```

---

## Task 12: Rollout — backup, snapshot, deploy

**Files:** none (operational steps; agent prints these for the operator).

- [ ] **Step 1: Stop the server**

Run: `launchctl unload ~/Library/LaunchAgents/com.gixen.server.plist`
Expected: silent. Verify with `curl -s http://localhost:8000/health` returning nothing.

- [ ] **Step 2: Back up the live DB**

Run: `cp ~/.gixen-server/db.sqlite ~/.gixen-server/db.sqlite.pre-fmv-split.bak`
Expected: file copied. Confirm: `ls -lh ~/.gixen-server/db.sqlite.pre-fmv-split.bak` exists.

- [ ] **Step 3: Pre-migration snapshot**

Run: `bash /Users/hsukenooi/Projects/gixen-cli/scripts/snapshot_db.sh > /tmp/snapshot-before.txt`
Expected: file written. `cat /tmp/snapshot-before.txt` shows `fmv: does not exist yet`, `bids.fmv_id: column missing`, and counts of shadow rows.

- [ ] **Step 4: Start the server (migration runs in `init_db`)**

Run: `launchctl load ~/Library/LaunchAgents/com.gixen.server.plist`
Expected: server starts. Tail the log (`tail -n 100 ~/.gixen-server/server.log`) for:
`FMV split migration complete: collapsed N shadow comics, inserted M fmv rows, linked K bids to fmv_id (...), migrated B bid_fmvs junction rows (skipped S).`

- [ ] **Step 5: Post-migration snapshot**

Run: `bash /Users/hsukenooi/Projects/gixen-cli/scripts/snapshot_db.sh > /tmp/snapshot-after.txt`
Expected: `fmv rows` > 0; `bids with fmv_id NOT NULL` ≈ `bids with comic_id NOT NULL` from before; "Suspected shadow comics" section is empty.

- [ ] **Step 6: Diff**

Run: `diff /tmp/snapshot-before.txt /tmp/snapshot-after.txt | less`
Expected: schema replaced; `fmv` rows materialized; counts redistributed.

- [ ] **Step 7: Smoke-test the dashboard**

Run: `curl -s http://localhost:8000/api/snipes | jq '.[0] | {item_id, comic_grade, fmv_low, fmv_warning, fmv_id}'`
Expected: FMV values intact for rows that had FMV pre-migration; `fmv_warning` populated where `fmv.low IS NULL`.

- [ ] **Step 8: Rollback procedure (if something looks wrong)**

```bash
launchctl unload ~/Library/LaunchAgents/com.gixen.server.plist
mv ~/.gixen-server/db.sqlite ~/.gixen-server/db.sqlite.failed-migration
cp ~/.gixen-server/db.sqlite.pre-fmv-split.bak ~/.gixen-server/db.sqlite
git revert HEAD~12..HEAD     # adjust to match the commit range from Tasks 1-11
launchctl load ~/Library/LaunchAgents/com.gixen.server.plist
```

---

## Caveats (workflow consequences of the new schema)

1. **`bids.fmv_id = NULL` is the only implicit "no FMV" state.** Bids with no operator metadata (Gixen-web-added snipes, quick bids placed without research) sit with `fmv_id = NULL`. The dashboard renders these with no comic title and `—` for FMV. That's the legitimate default state.

2. **A null-valuation `fmv(comic_id, grade, low=NULL, high=NULL)` row is created only on explicit user opt-in.** When the user explicitly says "add this without FMV at grade X" or "park this at grade X without researching FMV yet," `POST /api/bids` materializes the `fmv` row and points the bid at it — the FK invariant holds, the dashboard renders `—`, and `fmv_warning` fires. When the user is silent on grade, or chooses a "proceed without FMV" path that doesn't name a grade, the skill / handler MUST set `bids.fmv_id = NULL` instead. The schema's null-valuation row is not a fallback; it's a documented opt-in. Task 8b in this plan adds this clause to `snipe-add.md` so the skill can't drift into "always create a stub" behaviour. Drift in either direction (always-stub or never-stub) re-introduces the implicit-state problem the schema split is supposed to eliminate.

3. **Lot-aware view stays lot-aware.** `bid_fmvs` lets a single bid cover multiple `(comic, grade)` tuples — useful for mixed-grade lots. The migration carries forward existing lot junction rows where the grade is known. See "Future capability: lot-aware bids with per-issue grades" below for how this enables a richer skill flow going forward.

4. **Cross-grade FMV correction is now a single update.** Researching FMV at grade 9.2 is `UPDATE fmv SET low=?, high=? WHERE comic_id=? AND grade=9.2`. The bid's `fmv_id` doesn't move. The dashboard re-renders the next time it's loaded.

---

## Future capability: lot-aware bids with per-issue grades

The new `bid_fmvs(bid_id, fmv_id, is_primary)` junction lets one bid resolve to N distinct `fmv(comic, grade)` pairs. This unlocks mixed-grade lot tracking — a use case the legacy schema couldn't represent cleanly because grade was a single column on the comic identity row.

**Example (post-migration capability, no skill changes yet):** an eBay listing for "X-Men #94–100, mixed grades" can be recorded as:
- one row in `bids` with `fmv_id` pointing at the lot's primary (e.g. issue #94 at NM 9.4),
- seven rows in `bid_fmvs`, each pointing at a different `fmv` row: `fmv(X-Men #94, 9.4)`, `fmv(X-Men #95, 8.0)`, `fmv(X-Men #96, 9.0)`, etc.,
- seven `fmv` rows, one per issue at its actual grade.

The dashboard's lot-aware `comics: [...]` array (`api_get_snipes` second-query block) already surfaces this — each entry carries its own `grade` field via `fmv.grade`. No additional schema work needed.

**Migration scope:** this plan **does not** generate per-issue grades for historical lot bids. Legacy `bid_comics` rows carried only the bid's primary grade, which Task 4 step 5 propagates to every junction row. Historical lots stay single-grade. New lots (post-migration) can use the full per-issue capability.

**Skill follow-ups (separate PR, not part of this plan):**
- `/comic:identify` already produces the per-issue list (no change).
- `/comic:fmv` needs to compute one FMV per `(issue, grade)` pair instead of one per lot.
- `/comic:snipe-add` needs to call `POST /api/bids` once for the bid identity + max_bid, then issue N additional calls (or a single batch endpoint, TBD in that PR) that materialize `fmv` rows per issue and insert `bid_fmvs` junction rows.
- A new API endpoint may be needed: `POST /api/bids/{item_id}/fmvs` to attach additional `(comic, grade)` pairs to an existing bid without re-stating the bid identity.

These changes do not block the schema migration. They're additive on top of the migrated schema and can land in any release after this plan ships.

---

## Open questions flagged for the user (resolved)

1. **Year-typo de-duplication:** ship as-is, separate `dedup-by-locg` tool later. Same as v1. **Resolved: ship as-is.**
2. **Drop legacy columns and `link_comic_to_bid` immediately:** **Resolved: drop both now.** Legacy `comics.grade` / `comics.fmv_*` / `bids.comic_id` are dropped in Task 4 step 7. `link_comic_to_bid` is deleted entirely in Task 5 step 4 (grep confirmed only `server/main.py:1163` and `server/main.py:1336` call it, both rewritten in Tasks 6 + 8; the legacy tests are rewritten in Task 5). No deprecation shim. No follow-up cleanup PR needed.
3. **Frontend `fmv_warning` rendering:** still out of scope. The field is returned but the dashboard JS doesn't render it yet. No change.

---

## New design tensions surfaced by this revision

- **Junction migration for null-grade bids.** A legacy `bid_comics` row whose bid has no resolvable grade (because the linked legacy `comics.grade IS NULL`) cannot migrate to `bid_fmvs` (which requires a real `fmv_id`). The migration **skips** these junction rows and logs the skip count. The bid still survives (just unclassified); re-running `extract-comics` after migration repopulates the junction once a grade is parseable. The alternative — manufacturing a phantom grade like 0.0 to preserve the junction — would re-introduce the bug shape we're trying to prevent.
- **Lot rows with per-issue grades distinct from the bid's primary.** The migration uses the **bid's primary comic's grade** to resolve every junction row's `fmv_id`, not the per-comic grade on each `bid_comics.comic_id`. This matches how the parser populated grades (one grade per bid, propagated to every issue in the lot). If you find historical data with per-issue grades distinct from the primary, the migration loses that fidelity. Snapshot the pre-migration DB and grep for it; if any row shows up, raise it before Task 12. The "Future capability" section above describes how new lots avoid this limitation going forward.

---

## Self-Review

**Spec coverage:**
- Rename `comic_fmv` → `fmv`: Task 2 (schema), Task 4 (migration). ✅
- Drop `fmv_` prefix from columns: Task 2 (schema), Task 3 (helpers), Task 6 (handler maps API field names to new columns via column aliasing in SELECT). ✅
- Replace `bids.comic_id` + `bids.grade` with `bids.fmv_id`: Task 2 (column add), Task 4 (data migration + rebuild), Task 5 (`insert_bid` signature). ✅
- Rename `bid_comics` → `bid_fmvs`: Task 2 (schema), Task 4 (migration + drop legacy), Task 5 (junction helpers). ✅
- Delete `link_comic_to_bid` outright (no shim): Task 5 step 4 (deletes the function), Task 5 step 1 (rewrites the legacy tests and removes the import), Tasks 6 + 8 (rewrite the two production callers in `server/main.py`). ✅
- FK invariant test: Task 3 (`test_fk_invariant_fmv_id_must_exist`, `test_fk_invariant_bid_fmvs_fmv_id_must_exist`). ✅
- Migration creates fmv rows with NULL `low`/`high` for grade-without-FMV: Task 4 (step 5 manufactures stubs when needed) and `upsert_fmv` (Task 3) supports the null-valuation path. ✅
- Lot-with-grade test: Task 4 (`test_migration_lot_with_grade_creates_one_bid_fmvs_per_comic`). ✅
- 2026-05-13 incident regression: Task 10. ✅
- `POST /api/bids` flat shape preserved, internally upserts identity + fmv + sets fmv_id: Task 6. ✅
- `GET /api/snipes` JOIN through `bids.fmv_id`: Task 7. ✅
- `fmv_warning` semantic shift: Task 7 step 3 (warning fires when `fmv.low IS NULL`). ✅
- Null-FMV `fmv` row is explicit opt-in only: Caveat #2, Task 8b (skill update). ✅
- Future capability — lot-aware bids with per-issue grades: dedicated section between Caveats and Open Questions; no implementation in this plan. ✅
- Workflow consequence (Caveats section): top of plan + bottom Caveats. ✅
- Open questions resolved (year-typo dedup deferred, drop legacy now, frontend warning out of scope): bottom of plan. ✅

**Placeholder scan:** searched the plan for TBD / TODO / "appropriate" / "similar to" — none.

**Type consistency:** `upsert_fmv(comic_id, grade, low, high, comps, confidence, notes) -> int`, `set_bid_fmv(bid_id, fmv_id)`, `link_fmv_to_bid(bid_id, fmv_id, is_primary)`, `insert_bid(item_id, max_bid, fmv_id, ...)`. All consistent across Tasks 3, 5, 6, 7, 8.

**Highest-risk task:** Task 4 (data migration + table rebuild). Mitigated by the savepoint at step 7 and the pre-migration backup at Task 12 step 2. Migration tests cover shadow-collapse, idempotency, null-grade, orphan-FMV, survivor-priority, lot-with-grade, and post-state cases.
