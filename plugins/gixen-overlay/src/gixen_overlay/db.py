"""Comic-specific database functions for the gixen-overlay plugin."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table creation (called from register_db_tables hookimpl)
# ---------------------------------------------------------------------------


def create_tables(conn: sqlite3.Connection) -> None:
    """Create comics, fmv, and bid_fmvs tables. Idempotent (IF NOT EXISTS)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
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
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")
    # Migration order matters: _migrate_fmv_split leaves comics with year NOT NULL
    # and an inline UNIQUE(title, issue, year); _migrate_year_nullable then rebuilds
    # the table without those constraints. The partial unique indexes below assume
    # the post-year-nullable schema and must run after both migrations.
    _migrate_fmv_split(conn)
    _migrate_year_nullable(conn)
    # Partial unique indexes enforce identity in both regimes — see
    # docs/plans/2026-05-21-001-refactor-drop-comics-year-not-null-plan.md.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiy_yes "
        "ON comics(title, issue, year) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_ti_null "
        "ON comics(title, issue) WHERE year IS NULL"
    )


# ---------------------------------------------------------------------------
# One-time data migration: collapse legacy comics table into comics+fmv+bid_fmvs
# ---------------------------------------------------------------------------


def _migrate_fmv_split(conn: sqlite3.Connection) -> None:
    """Idempotent migration from the legacy monolithic comics schema.

    Gate: if comics.grade column is absent (already migrated or fresh DB),
    return immediately — with one exception: if comics_old also exists, that
    indicates a crash mid-rebuild and we raise to prevent silent data loss.

    IMPORTANT: Uses raw conn.execute() SQL only. Never calls CRUD helpers
    (upsert_fmv, upsert_comic, link_fmv_to_bid) — those call conn.commit()
    which would destroy the host's SAVEPOINT and make rollback impossible.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    if "grade" not in cols:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "comics_old" in tables:
            raise RuntimeError(
                "DB in crashed mid-migration state: comics_old exists but comics "
                "has no grade column — manual recovery required before server start"
            )
        return

    logger.info("fmv-split migration: starting")

    # Step 1: Select survivor comics per (title, issue, year) group.
    # Priority: locg_id NOT NULL > fmv_low NOT NULL > newest fmv_updated_at > lowest id.
    survivors = conn.execute("""
        SELECT c.id, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id, c.created_at
        FROM comics c
        INNER JOIN (
            SELECT
                title, issue, year,
                COALESCE(
                    MIN(CASE WHEN locg_id IS NOT NULL THEN id END),
                    MIN(CASE WHEN fmv_low IS NOT NULL THEN id END),
                    MIN(CASE WHEN fmv_updated_at IS NOT NULL THEN id END),
                    MIN(id)
                ) AS survivor_id
            FROM comics
            GROUP BY title, issue, year
        ) grp ON c.id = grp.survivor_id
    """).fetchall()

    survivor_ids = [r["id"] for r in survivors]
    # Build O(n) mapping from legacy comic_id to survivor_id for the same (title, issue, year)
    survivor_key_map = {(s["title"], s["issue"], s["year"]): s["id"] for s in survivors}
    id_to_survivor: dict[int, int] = {s["id"]: s["id"] for s in survivors}

    all_comics = conn.execute(
        "SELECT id, title, issue, year FROM comics"
    ).fetchall()
    for c in all_comics:
        if c["id"] not in id_to_survivor:
            id_to_survivor[c["id"]] = survivor_key_map[(c["title"], c["issue"], c["year"])]

    # Step 2: Manufacture fmv rows for each legacy (survivor_id, grade) pair.
    legacy_rows = conn.execute(
        "SELECT id, grade, fmv_low, fmv_high, fmv_comps, fmv_confidence, fmv_notes "
        "FROM comics WHERE grade IS NOT NULL"
    ).fetchall()

    fmv_inserted = 0
    # (survivor_id, grade) -> fmv_id lookup built as we insert
    fmv_lookup: dict[tuple[int, float], int] = {}

    for row in legacy_rows:
        survivor_id = id_to_survivor.get(row["id"], row["id"])
        grade = row["grade"]
        key = (survivor_id, grade)

        # Check if fmv row already exists (shouldn't on first run, but be safe)
        existing = conn.execute(
            "SELECT id, low FROM fmv WHERE comic_id=? AND grade=?",
            (survivor_id, grade),
        ).fetchone()

        if existing is None:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                """
                INSERT INTO fmv (comic_id, grade, low, high, comps, confidence, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (survivor_id, grade, row["fmv_low"], row["fmv_high"],
                 row["fmv_comps"], row["fmv_confidence"], row["fmv_notes"],
                 now if row["fmv_low"] is not None else None),
            )
            fmv_lookup[key] = cur.lastrowid
            fmv_inserted += 1
        else:
            fmv_id = existing["id"]
            if existing["low"] is None and row["fmv_low"] is not None:
                # Losing row has FMV data; take it and merge notes
                existing_notes = conn.execute(
                    "SELECT notes FROM fmv WHERE id=?", (fmv_id,)
                ).fetchone()["notes"]
                merged_notes = (
                    f"[merged from legacy comic_id={row['id']}] "
                    + (existing_notes or "")
                ).strip()
                conn.execute(
                    """
                    UPDATE fmv SET low=?, high=?, comps=?, confidence=?, notes=?
                    WHERE id=?
                    """,
                    (row["fmv_low"], row["fmv_high"], row["fmv_comps"],
                     row["fmv_confidence"], merged_notes, fmv_id),
                )
            fmv_lookup[key] = fmv_id

    # Step 3: Repoint bids.fmv_id from comic_id+grade to fmv_id.
    bids_linked = 0
    bid_rows = conn.execute(
        "SELECT b.id AS bid_id, b.comic_id, c.grade "
        "FROM bids b "
        "JOIN comics c ON c.id = b.comic_id "
        "WHERE b.comic_id IS NOT NULL"
    ).fetchall()

    for b in bid_rows:
        survivor_id = id_to_survivor.get(b["comic_id"], b["comic_id"])
        grade = b["grade"]
        if grade is None:
            continue
        fmv_id = fmv_lookup.get((survivor_id, grade))
        if fmv_id is not None:
            conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, b["bid_id"]))
            bids_linked += 1

    # Step 4: Migrate bid_comics -> bid_fmvs (only if bid_comics exists).
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    junction_inserted = 0
    junction_skipped = 0

    if "bid_comics" in tables:
        bc_rows = conn.execute(
            "SELECT bc.bid_id, bc.comic_id, bc.is_primary, c.grade "
            "FROM bid_comics bc "
            "JOIN comics c ON c.id = bc.comic_id"
        ).fetchall()

        for bc in bc_rows:
            survivor_id = id_to_survivor.get(bc["comic_id"], bc["comic_id"])
            grade = bc["grade"]
            if grade is None:
                junction_skipped += 1
                continue
            fmv_id = fmv_lookup.get((survivor_id, grade))
            if fmv_id is None:
                junction_skipped += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
                (bc["bid_id"], fmv_id, bc["is_primary"]),
            )
            junction_inserted += cur.rowcount

        # Step 5: Drop bid_comics (removes FK blocking non-survivor delete).
        conn.execute("DROP TABLE bid_comics")

    # Step 6: Delete non-survivor comics rows.
    if any(sid is None for sid in survivor_ids):
        raise RuntimeError(
            "survivor_ids contains None — sqlite3.Row row_factory misconfiguration"
        )
    if survivor_ids:
        placeholders = ",".join("?" * len(survivor_ids))
        conn.execute(
            f"DELETE FROM comics WHERE id NOT IN ({placeholders})", survivor_ids
        )

    # Step 7: Rebuild comics table via Python-memory approach.
    # SQLite 3.26+ updates FK references on RENAME, so DROP TABLE comics_old
    # would fail if fmv rows exist (FK follows the rename). Solution: save
    # fmv and bid_fmvs to Python memory, drop them, rebuild comics, restore.
    saved_fmv = conn.execute(
        "SELECT id, comic_id, grade, low, high, comps, confidence, notes, updated_at FROM fmv"
    ).fetchall()
    saved_bid_fmvs = conn.execute(
        "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs"
    ).fetchall()

    conn.execute("DROP TABLE bid_fmvs")
    conn.execute("DROP TABLE fmv")
    conn.execute("ALTER TABLE comics RENAME TO comics_old")
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
        INSERT INTO comics (id, title, issue, year, locg_id, locg_variant_id, created_at)
        SELECT id, title, issue, year, locg_id, locg_variant_id, created_at
        FROM comics_old
    """)
    conn.execute("DROP TABLE comics_old")

    # Recreate fmv and bid_fmvs with full FK constraints.
    conn.execute("""
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
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)

    # Restore fmv rows preserving original ids.
    for f in saved_fmv:
        conn.execute(
            """
            INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f["id"], f["comic_id"], f["grade"], f["low"], f["high"],
             f["comps"], f["confidence"], f["notes"], f["updated_at"]),
        )

    # Restore bid_fmvs rows.
    for bf in saved_bid_fmvs:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
        )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")

    logger.info(
        "fmv-split migration complete: survivors=%d fmv_inserted=%d "
        "bids_linked=%d junction_inserted=%d junction_skipped=%d",
        len(survivor_ids), fmv_inserted, bids_linked, junction_inserted, junction_skipped,
    )


# ---------------------------------------------------------------------------
# One-time data migration: drop comics.year NOT NULL (PER-98)
# ---------------------------------------------------------------------------


def _migrate_year_nullable(conn: sqlite3.Connection) -> None:
    """Idempotent migration: relax comics.year from NOT NULL to nullable.

    Gate: if comics.year already has notnull=0, return (or raise if the
    distinctly-named intermediate comics_old_ynull is present — crash recovery).

    Uses comics_old_ynull (not comics_old) so _migrate_fmv_split's own
    crash-recovery gate (which keys on comics_old) doesn't fire on a crash
    here. See PER-98 plan, Unit 1.

    IMPORTANT: raw conn.execute() only. Never call CRUD helpers that issue
    conn.commit() — would destroy the host SAVEPOINT (per
    docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md).
    """
    # PRAGMA table_info row layout: (cid, name, type, notnull, dflt_value, pk).
    _PRAGMA_NOTNULL = 3
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(comics)")}
    year_col = cols.get("year")
    # Gate fires for two states that both mean "migration already done":
    #   * year_col is None — comics has no year column (only reachable if comics
    #     itself doesn't exist yet, since the fresh-DB schema always has a year
    #     column; the surrounding create_tables runs CREATE TABLE IF NOT EXISTS
    #     before this migration so this path falls through to the crash check).
    #   * year_col[_PRAGMA_NOTNULL] == 0 — year is already nullable.
    if year_col is None or year_col[_PRAGMA_NOTNULL] == 0:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "comics_old_ynull" in tables:
            raise RuntimeError(
                "DB in crashed mid-migration state: comics_old_ynull exists but comics "
                "is already year-nullable — manual recovery required before server start"
            )
        return

    logger.info("year-nullable migration: starting")

    saved_fmv = conn.execute(
        "SELECT id, comic_id, grade, low, high, comps, confidence, notes, updated_at FROM fmv"
    ).fetchall()
    saved_bid_fmvs = conn.execute(
        "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs"
    ).fetchall()

    conn.execute("DROP TABLE bid_fmvs")
    conn.execute("DROP TABLE fmv")
    conn.execute("ALTER TABLE comics RENAME TO comics_old_ynull")
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO comics (id, title, issue, year, locg_id, locg_variant_id, created_at)
        SELECT id, title, issue, year, locg_id, locg_variant_id, created_at
        FROM comics_old_ynull
    """)
    conn.execute("DROP TABLE comics_old_ynull")

    conn.execute("""
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
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    for f in saved_fmv:
        conn.execute(
            """
            INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f["id"], f["comic_id"], f["grade"], f["low"], f["high"],
             f["comps"], f["confidence"], f["notes"], f["updated_at"]),
        )
    for bf in saved_bid_fmvs:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
        )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")

    comics_count = conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
    logger.info(
        "year-nullable migration complete: comics=%d fmv=%d bid_fmvs=%d",
        comics_count, len(saved_fmv), len(saved_bid_fmvs),
    )


# ---------------------------------------------------------------------------
# Comic CRUD (identity-only)
# ---------------------------------------------------------------------------


class ReconciliationConflictError(Exception):
    """Raised by upsert_comic when a NULL-year row and a yeared row coexist
    for the same (title, issue) but the inbound year differs from the yeared
    row's year — implies a reboot collision that needs manual disambiguation.

    Inherits from Exception (not RuntimeError) because a reboot collision is
    an expected application-level condition surfaced for caller resolution,
    not a programming error.
    """


def _merge_locg_fields(
    conn: sqlite3.Connection,
    comic_id: int,
    locg_id: int | None,
    locg_variant_id: int | None,
) -> None:
    """COALESCE the two locg columns into the named comic. No-op if both args are None."""
    if locg_id is None and locg_variant_id is None:
        return
    conn.execute(
        """
        UPDATE comics SET
            locg_id         = COALESCE(?, locg_id),
            locg_variant_id = COALESCE(?, locg_variant_id)
        WHERE id = ?
        """,
        (locg_id, locg_variant_id, comic_id),
    )


def check_reconciliation_conflict(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int | None,
) -> str | None:
    """Read-only predicate: returns the conflict message that upsert_comic
    would raise for these args, or None if no conflict.

    Used by api_extract_comics to pre-check every issue in a multi-issue lot
    before any writes — so a mid-loop conflict on idx=1 doesn't leave the
    idx=0 writes committed as orphan rows. See PER-98 todo 004.
    """
    if year is None:
        n = conn.execute(
            "SELECT COUNT(*) FROM comics WHERE title=? AND issue=? AND year IS NOT NULL",
            (title, issue),
        ).fetchone()[0]
        if n >= 2:
            years = sorted(
                r["year"] for r in conn.execute(
                    "SELECT year FROM comics WHERE title=? AND issue=? AND year IS NOT NULL",
                    (title, issue),
                )
            )
            return (
                f"year=None inbound for ({title!r}, {issue!r}) but multiple yeared "
                f"reboot siblings exist (years: {years}) — manual disambiguation required."
            )
        return None

    existing_null = conn.execute(
        "SELECT 1 FROM comics WHERE title=? AND issue=? AND year IS NULL LIMIT 1",
        (title, issue),
    ).fetchone()
    existing_yeared = conn.execute(
        "SELECT 1 FROM comics WHERE title=? AND issue=? AND year=? LIMIT 1",
        (title, issue, year),
    ).fetchone()
    other_yeared = conn.execute(
        "SELECT year FROM comics "
        "WHERE title=? AND issue=? AND year IS NOT NULL AND year != ? LIMIT 1",
        (title, issue, year),
    ).fetchone()
    if other_yeared is not None and existing_null is not None and existing_yeared is None:
        return (
            f"NULL-year row exists alongside yeared row for ({title!r}, {issue!r}) "
            f"at year {other_yeared['year']}; inbound year {year} would create a "
            f"second yeared identity — manual disambiguation required."
        )
    return None


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int | None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
) -> int:
    """Upsert a comic identity row. Returns the comic id.

    Handles the five PER-98 reconciliation cases:
      1. year=None + existing yeared row at (title, issue) -> short-circuit, merge locg fields.
      2. year=None + no yeared row -> INSERT-or-update the NULL-row.
      3. year=Y + NULL row + other-year yeared row -> raise ReconciliationConflictError.
      4. year=Y + only a NULL row -> promote it in place (preserve FK children).
      5. year=Y + matching yeared row + NULL row -> merge (R9 fmv-aware), delete NULL row.

    See docs/plans/2026-05-21-001-refactor-drop-comics-year-not-null-plan.md.

    Always issues conn.commit() on return — must not be called inside a SAVEPOINT
    or outer transaction. Same constraint as upsert_fmv and link_fmv_to_bid.
    """
    if year is None:
        return _upsert_comic_null_year(conn, title, issue, locg_id, locg_variant_id)
    return _upsert_comic_with_year(conn, title, issue, year, locg_id, locg_variant_id)


def _upsert_comic_null_year(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    locg_id: int | None,
    locg_variant_id: int | None,
) -> int:
    """Handle cases 1-2: year=None branch of upsert_comic.

    Case 1a (R10, year=None side): 2+ yeared reboot siblings exist for
        (title, issue) -> raise ReconciliationConflictError. Without a year
        hint, the caller cannot pick the right run (X-Men 1963 vs X-Men 1991).
    Case 1b: exactly one yeared row exists -> return its id, merge locg fields.
    Case 2: no yeared row -> INSERT or update the NULL-row via partial unique index.

    Always commits on return (unless raising).
    """
    yeared_rows = conn.execute(
        "SELECT id, year FROM comics WHERE title=? AND issue=? AND year IS NOT NULL",
        (title, issue),
    ).fetchall()
    if len(yeared_rows) >= 2:
        years = sorted(r["year"] for r in yeared_rows)
        raise ReconciliationConflictError(
            f"year=None inbound for ({title!r}, {issue!r}) but multiple yeared "
            f"reboot siblings exist (years: {years}) — manual disambiguation required."
        )
    if yeared_rows:
        existing_yeared = yeared_rows[0]
        _merge_locg_fields(conn, existing_yeared["id"], locg_id, locg_variant_id)
        conn.commit()
        return existing_yeared["id"]

    conn.execute(
        """
        INSERT INTO comics (title, issue, year, locg_id, locg_variant_id)
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(title, issue) WHERE year IS NULL DO UPDATE SET
            locg_id         = COALESCE(excluded.locg_id,         locg_id),
            locg_variant_id = COALESCE(excluded.locg_variant_id, locg_variant_id)
        """,
        (title, issue, locg_id, locg_variant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year IS NULL",
        (title, issue),
    ).fetchone()
    return row["id"]


def _upsert_comic_with_year(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int,
    locg_id: int | None,
    locg_variant_id: int | None,
) -> int:
    """Handle cases 3-5 + default: year=Y branch of upsert_comic.

    Case 3 (R10 reboot guard): NULL row exists AND another year exists for
        (title, issue) but no row matches the inbound year exactly -> raise
        ReconciliationConflictError. Without an exact match, promotion or
        merge would create a second yeared identity.
    Case 4 (promote): only a NULL row exists -> UPDATE its year in place,
        preserving comic_id and FK children.
    Case 5 (merge): NULL row + matching yeared row -> reparent fmv children
        (R9 priced-wins), delete NULL row, keep yeared as survivor.
    Default: no NULL row -> INSERT-or-update via the yeared partial index.

    Always commits on return.
    """
    existing_null = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year IS NULL LIMIT 1",
        (title, issue),
    ).fetchone()
    existing_yeared = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year=? LIMIT 1",
        (title, issue, year),
    ).fetchone()
    other_yeared = conn.execute(
        "SELECT id, year FROM comics "
        "WHERE title=? AND issue=? AND year IS NOT NULL AND year != ? LIMIT 1",
        (title, issue, year),
    ).fetchone()

    # Case 3 (R10): NULL row + a non-matching yeared row + no matching yeared row
    # -> refuse. (If a matching yeared row exists, Case 5 below handles it cleanly
    # even when another-year siblings are present.)
    if (
        other_yeared is not None
        and existing_null is not None
        and existing_yeared is None
    ):
        raise ReconciliationConflictError(
            f"NULL-year row exists alongside yeared row for ({title!r}, {issue!r}) "
            f"at year {other_yeared['year']}; inbound year {year} would create a "
            f"second yeared identity — manual disambiguation required."
        )

    # Case 4: only a NULL row exists -> promote in place (preserves FK children).
    if existing_null is not None and existing_yeared is None:
        conn.execute(
            """
            UPDATE comics SET
                year            = ?,
                locg_id         = COALESCE(?, locg_id),
                locg_variant_id = COALESCE(?, locg_variant_id)
            WHERE id = ?
            """,
            (year, locg_id, locg_variant_id, existing_null["id"]),
        )
        conn.commit()
        return existing_null["id"]

    # Case 5: NULL row + matching yeared row -> merge NULL into yeared.
    if existing_null is not None and existing_yeared is not None:
        survivor_id = existing_yeared["id"]
        null_id = existing_null["id"]
        _merge_null_row_into_yeared(conn, null_id=null_id, survivor_id=survivor_id)
        _merge_locg_fields(conn, survivor_id, locg_id, locg_variant_id)
        conn.execute("DELETE FROM comics WHERE id = ?", (null_id,))
        conn.commit()
        return survivor_id

    # Default: no NULL row, possibly a sibling other-year row (legit reboot).
    # INSERT-or-update via the yeared partial index.
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, locg_id, locg_variant_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year) WHERE year IS NOT NULL DO UPDATE SET
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


def _merge_null_row_into_yeared(
    conn: sqlite3.Connection,
    *,
    null_id: int,
    survivor_id: int,
) -> None:
    """Move fmv children from null_id to survivor_id, applying R9 priced-wins rule.

    Per-grade collision behavior:
      - No collision -> reparent the null row's fmv via UPDATE.
      - Survivor stub (low IS NULL) + null priced -> transplant prices into survivor's
        fmv, delete null's fmv. WARN.
      - Otherwise -> delete null's fmv. WARN only if priced data was discarded.

    bid_fmvs referencing the deleted fmv cascade via FK ON DELETE CASCADE.
    Must be called inside an active transaction (caller commits).
    """
    null_fmv_rows = conn.execute(
        "SELECT id, grade, low, high, comps, confidence, notes, updated_at "
        "FROM fmv WHERE comic_id=?",
        (null_id,),
    ).fetchall()
    for nf in null_fmv_rows:
        sf = conn.execute(
            "SELECT id, low FROM fmv WHERE comic_id=? AND grade=?",
            (survivor_id, nf["grade"]),
        ).fetchone()
        if sf is None:
            # No collision: reparent. Safe against UNIQUE(comic_id, grade) because
            # the explicit SELECT above just verified the survivor has no fmv at
            # this grade, and earlier iterations of this loop only ever DELETE
            # null-side fmv rows (never INSERT into the survivor).
            conn.execute(
                "UPDATE fmv SET comic_id=? WHERE id=?",
                (survivor_id, nf["id"]),
            )
            continue
        if sf["low"] is None and nf["low"] is not None:
            # Per-column COALESCE so a non-null survivor column (e.g., high or
            # comps set by an earlier targeted upsert_fmv) is never overwritten
            # by a null on the inbound row. The `low IS NULL` discriminator
            # selects the branch; the actual transplant is per-column.
            conn.execute(
                """
                UPDATE fmv SET
                    low        = COALESCE(low,        ?),
                    high       = COALESCE(high,       ?),
                    comps      = COALESCE(comps,      ?),
                    confidence = COALESCE(confidence, ?),
                    notes      = COALESCE(notes,      ?),
                    updated_at = COALESCE(updated_at, ?)
                WHERE id=?
                """,
                (nf["low"], nf["high"], nf["comps"], nf["confidence"],
                 nf["notes"], nf["updated_at"], sf["id"]),
            )
            conn.execute("DELETE FROM fmv WHERE id=?", (nf["id"],))
            logger.warning(
                "upsert_comic merge: transplanted prices from null-row fmv id=%s "
                "into yeared-row fmv id=%s (grade=%s)",
                nf["id"], sf["id"], nf["grade"],
            )
            continue
        # Survivor wins. Warn only if priced data was actually discarded.
        conn.execute("DELETE FROM fmv WHERE id=?", (nf["id"],))
        if nf["low"] is not None:
            logger.warning(
                "upsert_comic merge: discarded null-row priced fmv id=%s (grade=%s) — "
                "yeared row's fmv id=%s already had prices",
                nf["id"], nf["grade"], sf["id"],
            )


# ---------------------------------------------------------------------------
# FMV CRUD
# ---------------------------------------------------------------------------


def upsert_fmv(
    conn: sqlite3.Connection,
    comic_id: int,
    grade: float | None,
    low: float | None = None,
    high: float | None = None,
    comps: int | None = None,
    confidence: str | None = None,
    notes: str | None = None,
) -> int:
    """Upsert a per-grade FMV row. Returns the fmv id."""
    if grade is None:
        raise ValueError("grade is required for upsert_fmv")
    has_value = any(v is not None for v in (low, high, comps, confidence, notes))
    now = datetime.now(timezone.utc).isoformat() if has_value else None
    conn.execute(
        """
        INSERT INTO fmv (comic_id, grade, low, high, comps, confidence, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(comic_id, grade) DO UPDATE SET
            low         = COALESCE(excluded.low,        low),
            high        = COALESCE(excluded.high,       high),
            comps       = COALESCE(excluded.comps,      comps),
            confidence  = COALESCE(excluded.confidence, confidence),
            notes       = COALESCE(excluded.notes,      notes),
            updated_at  = CASE WHEN excluded.low IS NOT NULL THEN excluded.updated_at
                               ELSE updated_at END
        """,
        (comic_id, grade, low, high, comps, confidence, notes, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM fmv WHERE comic_id=? AND grade=?", (comic_id, grade)
    ).fetchone()
    return row["id"]


def set_bid_fmv(conn: sqlite3.Connection, bid_id: int, fmv_id: int | None) -> None:
    """Set or clear bids.fmv_id."""
    conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    conn.commit()


def get_fmv_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    """Return the fmv row linked via bids.fmv_id, or None."""
    return conn.execute(
        "SELECT f.* FROM bids b JOIN fmv f ON f.id = b.fmv_id WHERE b.id=?",
        (bid_id,),
    ).fetchone()


def link_fmv_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    fmv_id: int,
    is_primary: bool = False,
) -> None:
    """Insert into bid_fmvs. If primary, demote prior entries and mirror to bids.fmv_id."""
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


def get_primary_fmv_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    """Return the primary fmv row (with comic fields) for a bid."""
    return conn.execute(
        """
        SELECT f.*, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id
        FROM bid_fmvs bf
        JOIN fmv f ON f.id = bf.fmv_id
        JOIN comics c ON c.id = f.comic_id
        WHERE bf.bid_id = ? AND bf.is_primary = 1
        LIMIT 1
        """,
        (bid_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def list_comics(
    conn: sqlite3.Connection,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
) -> list[sqlite3.Row]:
    """Return comics enriched with FMV data. One row per (comic, fmv) pair."""
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
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"""
        SELECT c.id, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id,
               f.id AS fmv_id, f.grade,
               f.low AS fmv_low, f.high AS fmv_high, f.comps AS fmv_comps,
               f.confidence AS fmv_confidence, f.notes AS fmv_notes,
               f.updated_at AS fmv_updated_at
        FROM comics c
        LEFT JOIN fmv f ON f.comic_id = c.id
        {where}
        ORDER BY c.id, f.grade
        """,
        params,
    ).fetchall()
