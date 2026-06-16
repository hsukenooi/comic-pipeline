"""Comic-specific database functions for the gixen-overlay plugin."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table creation (called from register_db_tables hookimpl)
# ---------------------------------------------------------------------------


def create_tables(conn: sqlite3.Connection) -> None:
    """Create comics, fmv, and bid_fmvs tables. Idempotent (IF NOT EXISTS).

    `comics.year` is nullable. Uniqueness is enforced by two partial indexes
    so a comic exists at most once per (title, issue): either yeared or
    yearless, never both at the same time after reconciliation in
    upsert_comic.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER,
            variant         TEXT,
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
    # BUI-113: remember which seller-scan wish-list matches have already been
    # surfaced, so repeat scans default to showing only new ones. Standalone —
    # no FK or JOIN to comics/bids — so it's a plain additive table that needs
    # none of the Python-memory rebuild machinery the comic tables require.
    # Only matches get recorded (a handful of item_ids per scan, not every
    # listing), so the table stays small and means exactly "matches I've shown".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seller_scan_seen (
            item_id       TEXT PRIMARY KEY,
            seller        TEXT,
            first_seen_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migration_state (
            migration TEXT PRIMARY KEY
        )
    """)
    _migrate_fmv_split(conn)
    _migrate_year_nullable(conn)
    _migrate_sweep_allcaps_orphans(conn)
    # variant column must exist before the unique-index migration references it.
    _migrate_add_variant_column(conn)
    _migrate_lowercase_title_indexes(conn)
    # Partial unique indexes go AFTER migrations so the legacy duplicate-row
    # cleanup (fmv-split collapses (title, issue, year, grade) duplicates into
    # one comic) has run before we try to enforce uniqueness on the cleaned set.
    # LOWER(title) expression indexes enforce uniqueness case-insensitively so
    # direct SQL writes also can't create case-variant duplicates.
    # BUI-28: variant is part of the identity, so a base cover and its Newsstand
    # (etc.) variant are distinct rows. COALESCE(variant,'') folds NULL→'' so two
    # base rows still collide (SQLite treats bare NULLs as distinct in indexes).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiyv "
        "ON comics(LOWER(title), issue, year, COALESCE(variant,'')) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiv_nullyear "
        "ON comics(LOWER(title), issue, COALESCE(variant,'')) WHERE year IS NULL"
    )
    # Drop the pre-variant indexes (they'd wrongly reject a second variant row).
    conn.execute("DROP INDEX IF EXISTS idx_comics_tiy")
    conn.execute("DROP INDEX IF EXISTS idx_comics_ti_nullyear")


# ---------------------------------------------------------------------------
# Migration crash-guard helpers
# ---------------------------------------------------------------------------


def _assert_no_migration_marker(conn: sqlite3.Connection, name: str) -> None:
    """Raise RuntimeError if a crash marker for `name` is present in migration_state.

    Called before each migration's gate so a crash in the post-DROP window
    (where the schema looks already-migrated and the gate would return early)
    still surfaces instead of silently leaving fmv/bid_fmvs empty.
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration=?", (name,)
    ).fetchone()
    if row is not None:
        raise RuntimeError(
            f"DB in crashed mid-migration state: '{name}' marker present — "
            "restore from pre-migration snapshot before restarting"
        )


def _set_migration_marker(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO migration_state (migration) VALUES (?)", (name,)
    )


def _clear_migration_marker(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM migration_state WHERE migration=?", (name,))


def _migrate_add_variant_column(conn: sqlite3.Connection) -> None:
    """Add the nullable `variant` column to comics if absent (BUI-28).

    Additive and idempotent. Existing rows keep variant=NULL (treated as the
    base edition); variants split off only on the next encounter — no bulk
    backfill of historically conflated rows.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    if "variant" not in cols:
        conn.execute("ALTER TABLE comics ADD COLUMN variant TEXT")


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
    # Check marker before the gate: a crash after DROP TABLE comics_old leaves
    # the schema looking already-migrated (no grade col, no comics_old), so the
    # gate would return early and hide the incomplete fmv/bid_fmvs restore.
    _assert_no_migration_marker(conn, "fmv_split")

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
            fmv_lookup[key] = cur.lastrowid  # type: ignore[assignment]  # INSERT always yields int lastrowid
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
    # Write crash marker before first DROP so a crash mid-restore is detectable
    # on next startup (the gate would otherwise return early on the clean schema).
    _set_migration_marker(conn, "fmv_split")
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

    _clear_migration_marker(conn, "fmv_split")

    logger.info(
        "fmv-split migration complete: survivors=%d fmv_inserted=%d "
        "bids_linked=%d junction_inserted=%d junction_skipped=%d",
        len(survivor_ids), fmv_inserted, bids_linked, junction_inserted, junction_skipped,
    )


# ---------------------------------------------------------------------------
# One-time migration: drop comics.year NOT NULL (PER-98)
# ---------------------------------------------------------------------------


def _migrate_year_nullable(conn: sqlite3.Connection) -> None:
    """Make comics.year nullable + swap the UNIQUE(title, issue, year) constraint
    for two partial unique indexes (see create_tables).

    Gate: if comics.year is already nullable, return immediately. Detected via
    PRAGMA table_info — when notnull=0 on the year column.

    IMPORTANT: Uses raw conn.execute() SQL only. Never calls CRUD helpers
    (upsert_fmv, upsert_comic, link_fmv_to_bid) — those call conn.commit()
    which would destroy the host's SAVEPOINT and make rollback impossible.

    Pattern: Python-memory rebuild (per docs/solutions/database-issues/
    sqlite-fk-rename-savepoint-pragma-2026-05-19.md). SQLite 3.26+ rewrites
    FK references on RENAME, so fmv rows would block DROP TABLE comics_old.
    Save FK children to Python memory first, drop them, rebuild, restore.
    """
    # Check marker before the gate: a crash after DROP TABLE comics_old leaves
    # year already nullable, so the gate would return early and hide the
    # incomplete fmv/bid_fmvs restore.
    _assert_no_migration_marker(conn, "year_nullable")

    year_col = next(
        (row for row in conn.execute("PRAGMA table_info(comics)") if row[1] == "year"),
        None,
    )
    if year_col is None:
        # No comics table yet — create_tables made it from scratch with the
        # nullable schema. Nothing to migrate.
        return
    if year_col[3] == 0:
        # year is already nullable. Migration done (or fresh install). Bail.
        return

    logger.info("year-nullable migration: starting")

    # Only carry rows that survive a JOIN against the live parents. CASCADE
    # deletes can be bypassed by sqlite3 CLI sessions that didn't opt into
    # PRAGMA foreign_keys=ON (default OFF), leaving orphan junction rows that
    # would fail FK enforcement when re-inserted.
    saved_fmv = conn.execute(
        """
        SELECT f.id, f.comic_id, f.grade, f.low, f.high, f.comps, f.confidence, f.notes, f.updated_at
        FROM fmv f
        JOIN comics c ON c.id = f.comic_id
        """
    ).fetchall()
    saved_bid_fmvs = conn.execute(
        """
        SELECT bf.bid_id, bf.fmv_id, bf.is_primary
        FROM bid_fmvs bf
        JOIN fmv f ON f.id = bf.fmv_id
        JOIN bids b ON b.id = bf.bid_id
        """
    ).fetchall()
    # bids.fmv_id is declared REFERENCES fmv(id) ON DELETE SET NULL. Dropping
    # the fmv table fires that cascade and nulls every bid's primary fmv link.
    # Save current values so we can restore the column after the rebuild.
    saved_bid_fmv_id = conn.execute(
        "SELECT b.id, b.fmv_id FROM bids b "
        "JOIN fmv f ON f.id = b.fmv_id WHERE b.fmv_id IS NOT NULL"
    ).fetchall()

    # Write crash marker before first DROP so a crash mid-restore is detectable
    # on next startup (the gate would otherwise return early on the nullable schema).
    _set_migration_marker(conn, "year_nullable")

    conn.execute("DROP TABLE bid_fmvs")
    conn.execute("DROP TABLE fmv")
    conn.execute("ALTER TABLE comics RENAME TO comics_old")
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
        FROM comics_old
    """)
    conn.execute("DROP TABLE comics_old")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiy "
        "ON comics(title, issue, year) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_ti_nullyear "
        "ON comics(title, issue) WHERE year IS NULL"
    )

    # Recreate FK children with full constraints, restore from memory.
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
    # Restore bids.fmv_id values that the SET NULL cascade wiped when fmv dropped.
    for b in saved_bid_fmv_id:
        conn.execute("UPDATE bids SET fmv_id = ? WHERE id = ?", (b["fmv_id"], b["id"]))

    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")

    _clear_migration_marker(conn, "year_nullable")

    logger.info(
        "year-nullable migration complete: %d fmv, %d bid_fmvs, %d bids.fmv_id restored",
        len(saved_fmv), len(saved_bid_fmvs), len(saved_bid_fmv_id),
    )


# ---------------------------------------------------------------------------
# One-time data migration: sweep ALL-CAPS yearless orphans created pre-PER-123
# ---------------------------------------------------------------------------


def _migrate_sweep_allcaps_orphans(conn: sqlite3.Connection) -> None:
    """Merge ALL-CAPS yearless orphan comics into their yeared siblings exactly once.

    Cleans up stubs created before PER-123 added case-insensitive title matching.
    Gate: migration_state row 'sweep_allcaps_orphans' present → already ran.

    IMPORTANT: Uses raw conn.execute() only — no conn.commit(). Called from
    create_tables() which runs inside the host's per-plugin SAVEPOINT; calling
    conn.commit() here would destroy it (same constraint as _migrate_fmv_split
    and _migrate_year_nullable).
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration='sweep_allcaps_orphans'"
    ).fetchone()
    if row is not None:
        return

    orphans = conn.execute(
        """
        SELECT
            c.id   AS yearless_id,
            c.title,
            c.issue,
            (SELECT id FROM comics
             WHERE LOWER(title)=LOWER(c.title) AND issue=c.issue AND year IS NOT NULL
             ORDER BY (locg_id IS NULL), id LIMIT 1) AS yeared_id
        FROM comics c
        WHERE c.year IS NULL
          AND EXISTS (
              SELECT 1 FROM comics
              WHERE LOWER(title)=LOWER(c.title) AND issue=c.issue AND year IS NOT NULL
          )
        """
    ).fetchall()

    for orphan in orphans:
        _merge_yearless_into_yeared(conn, orphan["yearless_id"], orphan["yeared_id"])
        conn.execute("DELETE FROM comics WHERE id=?", (orphan["yearless_id"],))

    if orphans:
        logger.info(
            "_migrate_sweep_allcaps_orphans: merged %d orphan(s): %s",
            len(orphans),
            [(r["title"], r["issue"]) for r in orphans],
        )
    _set_migration_marker(conn, "sweep_allcaps_orphans")


# ---------------------------------------------------------------------------
# One-time schema migration: LOWER(title) expression unique indexes (PER-120)
# ---------------------------------------------------------------------------


def _migrate_lowercase_title_indexes(conn: sqlite3.Connection) -> None:
    """Replace case-sensitive partial unique indexes with LOWER(title), variant-aware ones.

    Old: ON comics(title, issue, year) / ON comics(title, issue)
    New: ON comics(LOWER(title), issue, year, COALESCE(variant,'')) /
         ON comics(LOWER(title), issue, COALESCE(variant,''))   (BUI-28)

    Gate: migration_state row 'lowercase_title_indexes' present → already ran.
    (DBs that ran the pre-variant version of this migration are upgraded to the
    variant-aware indexes by the unconditional create/drop in create_tables.)

    IMPORTANT: Uses raw conn.execute() only — no conn.commit(). Called from
    create_tables() which runs inside the host's per-plugin SAVEPOINT. The index
    creation here happens in create_tables' autocommit window (before any
    migration marker INSERT opens a transaction) so the indexes survive a caller
    rollback.
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration='lowercase_title_indexes'"
    ).fetchone()
    if row is not None:
        return

    conn.execute("DROP INDEX IF EXISTS idx_comics_tiy")
    conn.execute("DROP INDEX IF EXISTS idx_comics_ti_nullyear")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiyv "
        "ON comics(LOWER(title), issue, year, COALESCE(variant,'')) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiv_nullyear "
        "ON comics(LOWER(title), issue, COALESCE(variant,'')) WHERE year IS NULL"
    )
    _set_migration_marker(conn, "lowercase_title_indexes")


# ---------------------------------------------------------------------------
# Comic CRUD (identity-only)
# ---------------------------------------------------------------------------


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int | None = None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
    variant: str | None = None,
) -> int:
    """Upsert a comic identity row. Returns the comic id.

    `variant` (BUI-28) is part of the row identity: a base cover and its
    Newsstand/Direct/etc. variant of the same (title, issue, year) get distinct
    comic ids. An empty/blank variant normalizes to NULL (the base edition). All
    the reconciliation below is scoped to a single variant.

    Year is optional. Reconciliation rules keep at most one row per
    (title, issue, variant) logical comic:

    - Yeared insert finds an existing yeared row at the same year → updates
      locg metadata, returns it.
    - Yeared insert finds an existing yearless row for the same (title, issue)
      → promotes it (UPDATE comics SET year=?), returns it. Avoids creating a
      duplicate alongside the yearless placeholder. Exception: if a yeared row
      at a *different* year already exists, promotion is skipped (warning logged,
      yearless row returned unchanged) to prevent two yeared siblings (PER-104).
    - Yearless insert finds an existing yeared row for the same (title, issue)
      → prefers the yeared one (returns its id without creating a yearless
      duplicate). Locg metadata still gets merged in.
    - Yearless insert finds an existing yearless row → updates locg, returns.

    When multiple yeared rows exist for the same (title, issue) — pre-PER-98
    historical data — the one with locg_id set wins; ties broken by lowest id.
    """
    # BUI-28: normalize blank variant to NULL (base edition) and scope every
    # identity query to this variant so reconciliation never crosses variants.
    variant = (variant or "").strip() or None
    v_sql = "variant=?" if variant is not None else "variant IS NULL"
    v_param: tuple = (variant,) if variant is not None else ()

    if year is not None:
        # Yeared insert.
        existing_yeared = conn.execute(
            f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year=? AND {v_sql}",
            (title, issue, year, *v_param),
        ).fetchone()
        if existing_yeared is not None:
            conn.execute(
                "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
                "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
                (locg_id, locg_variant_id, existing_yeared["id"]),
            )
            conn.commit()
            return existing_yeared["id"]
        # Look for a yearless placeholder to promote.
        existing_yearless = conn.execute(
            f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NULL AND {v_sql}",
            (title, issue, *v_param),
        ).fetchone()
        if existing_yearless is not None:
            # Guard (PER-104): if a yeared row at a *different* year already
            # exists, promoting would create two yeared siblings. Skip and warn.
            conflicting_yeared = conn.execute(
                "SELECT id FROM comics "
                f"WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NOT NULL AND year!=? AND {v_sql}",
                (title, issue, year, *v_param),
            ).fetchone()
            if conflicting_yeared is not None:
                logger.warning(
                    "upsert_comic: skipping yearless promotion — yeared sibling "
                    "conflict (title=%r issue=%r incoming_year=%r variant=%r); keeping "
                    "yearless row",
                    title,
                    issue,
                    year,
                    variant,
                )
                return existing_yearless["id"]
            conn.execute(
                "UPDATE comics SET year=?, "
                "locg_id=COALESCE(?, locg_id), "
                "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
                (year, locg_id, locg_variant_id, existing_yearless["id"]),
            )
            conn.commit()
            return existing_yearless["id"]
        # No existing row — insert fresh.
        cur = conn.execute(
            "INSERT INTO comics (title, issue, year, variant, locg_id, locg_variant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, issue, year, variant, locg_id, locg_variant_id),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]  # INSERT always yields int lastrowid

    # Yearless insert. Prefer an existing yeared row if one exists — never
    # create a yearless duplicate next to a yeared canonical row.
    canonical_yeared = conn.execute(
        f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NOT NULL AND {v_sql} "
        "ORDER BY (locg_id IS NULL), id LIMIT 1",
        (title, issue, *v_param),
    ).fetchone()
    if canonical_yeared is not None:
        # PER-103: clean up any pre-existing yearless orphan alongside the
        # canonical yeared row before returning.
        orphan = conn.execute(
            f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NULL AND {v_sql}",
            (title, issue, *v_param),
        ).fetchone()
        if orphan is not None:
            _merge_yearless_into_yeared(conn, orphan["id"], canonical_yeared["id"])
            conn.execute("DELETE FROM comics WHERE id=?", (orphan["id"],))
        conn.execute(
            "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
            "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
            (locg_id, locg_variant_id, canonical_yeared["id"]),
        )
        conn.commit()
        return canonical_yeared["id"]
    existing_yearless = conn.execute(
        f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NULL AND {v_sql}",
        (title, issue, *v_param),
    ).fetchone()
    if existing_yearless is not None:
        conn.execute(
            "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
            "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
            (locg_id, locg_variant_id, existing_yearless["id"]),
        )
        conn.commit()
        return existing_yearless["id"]
    cur = conn.execute(
        "INSERT INTO comics (title, issue, year, variant, locg_id, locg_variant_id) "
        "VALUES (?, ?, NULL, ?, ?, ?)",
        (title, issue, variant, locg_id, locg_variant_id),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]  # INSERT always yields int lastrowid


# ---------------------------------------------------------------------------
# Orphan yearless cleanup (PER-103)
# ---------------------------------------------------------------------------


def _merge_yearless_into_yeared(
    conn: sqlite3.Connection, yearless_id: int, yeared_id: int
) -> None:
    """Reparent all fmv children from a yearless orphan onto a yeared row.

    For each fmv grade on yearless_id:
    - No conflict (yeared has no fmv at that grade): reassign comic_id in-place.
    - Conflict (yeared already has fmv at that grade): COALESCE non-null fields
      into the yeared fmv, reparent bid_fmvs and bids.fmv_id, then delete the
      duplicate yearless fmv row.

    Does NOT delete the yearless comics row — caller's responsibility.
    """
    yearless_fmvs = conn.execute(
        "SELECT id, grade, low, high, comps, confidence, notes, updated_at "
        "FROM fmv WHERE comic_id=?",
        (yearless_id,),
    ).fetchall()

    for yfmv in yearless_fmvs:
        yeared_fmv = conn.execute(
            "SELECT id FROM fmv WHERE comic_id=? AND grade=?",
            (yeared_id, yfmv["grade"]),
        ).fetchone()

        if yeared_fmv is None:
            conn.execute("UPDATE fmv SET comic_id=? WHERE id=?", (yeared_id, yfmv["id"]))
        else:
            # Merge non-null fields from yearless into yeared (COALESCE keeps
            # existing non-null values; yearless fills gaps only).
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
                (
                    yfmv["low"], yfmv["high"], yfmv["comps"],
                    yfmv["confidence"], yfmv["notes"], yfmv["updated_at"],
                    yeared_fmv["id"],
                ),
            )
            # Reparent bid_fmvs; preserve the higher is_primary if both exist.
            for bf in conn.execute(
                "SELECT bid_id, is_primary FROM bid_fmvs WHERE fmv_id=?",
                (yfmv["id"],),
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)
                    ON CONFLICT(bid_id, fmv_id) DO UPDATE
                        SET is_primary = MAX(is_primary, excluded.is_primary)
                    """,
                    (bf["bid_id"], yeared_fmv["id"], bf["is_primary"]),
                )
            # Reparent bids.fmv_id.
            conn.execute(
                "UPDATE bids SET fmv_id=? WHERE fmv_id=?",
                (yeared_fmv["id"], yfmv["id"]),
            )
            # Delete the now-redundant yearless fmv (cascade cleans its bid_fmvs).
            conn.execute("DELETE FROM fmv WHERE id=?", (yfmv["id"],))


def sweep_orphan_yearless_comics(
    conn: sqlite3.Connection, dry_run: bool = False
) -> dict:
    """Find yearless rows that have a yeared sibling and merge them in.

    When dry_run=True reports what would change without touching the DB.
    Returns a summary dict with 'merged' (or 'would_merge') count and details.
    """
    orphans = conn.execute(
        """
        SELECT
            c.id   AS yearless_id,
            c.title,
            c.issue,
            (SELECT id FROM comics
             WHERE LOWER(title)=LOWER(c.title) AND issue=c.issue AND year IS NOT NULL
             ORDER BY (locg_id IS NULL), id LIMIT 1) AS yeared_id
        FROM comics c
        WHERE c.year IS NULL
          AND EXISTS (
              SELECT 1 FROM comics
              WHERE LOWER(title)=LOWER(c.title) AND issue=c.issue AND year IS NOT NULL
          )
        """
    ).fetchall()

    details = [
        {
            "title": row["title"],
            "issue": row["issue"],
            "yearless_id": row["yearless_id"],
            "yeared_id": row["yeared_id"],
        }
        for row in orphans
    ]

    if dry_run:
        return {"dry_run": True, "would_merge": len(details), "details": details}

    for row in orphans:
        _merge_yearless_into_yeared(conn, row["yearless_id"], row["yeared_id"])
        conn.execute("DELETE FROM comics WHERE id=?", (row["yearless_id"],))
    if orphans:
        conn.commit()

    logger.info("sweep_orphan_yearless_comics: merged %d orphan(s)", len(orphans))
    return {"dry_run": False, "merged": len(details), "details": details}


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
    """Insert into bid_fmvs and keep one junction per comic per bid.

    Primary links replace any prior junction pointing at the *same comic*
    (so a grade-only stub re-linked to a valued FMV collapses to one row
    rather than leaving a demoted null-valued duplicate — BUI-82) and demote
    other-comic junctions to lot members. A sole junction is always primary
    so the dashboard's grade/FMV aggregates (which key off is_primary=1)
    never blank.
    """
    if is_primary:
        # Drop prior junctions for the same comic as the new FMV; genuine
        # other-comic lot members survive and are demoted just below.
        conn.execute(
            """
            DELETE FROM bid_fmvs
            WHERE bid_id = ?
              AND fmv_id != ?
              AND fmv_id IN (
                  SELECT other.id FROM fmv other
                  JOIN fmv target ON target.comic_id = other.comic_id
                  WHERE target.id = ?
              )
            """,
            (bid_id, fmv_id, fmv_id),
        )
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
        # A sole junction must be primary, else cond_grade/fmv blank out.
        sole = conn.execute(
            "SELECT COUNT(*) FROM bid_fmvs WHERE bid_id=?", (bid_id,)
        ).fetchone()[0] == 1
        if sole:
            conn.execute(
                "UPDATE bid_fmvs SET is_primary=1 WHERE bid_id=? AND fmv_id=?",
                (bid_id, fmv_id),
            )
            conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
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
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
    max_age_days: float | None = None,
) -> list[sqlite3.Row]:
    """Return comics enriched with FMV data. One row per (comic, fmv) pair.

    locg_id: filter to one canonical issue (used by comic-fmv to look up a
        fresh FMV by LOCG ID + grade without juggling title spellings).
    locg_variant_id: BUI-139 — two variant rows of one issue share the same
        issue-level locg_id (only locg_variant_id differs), so a locg_id+grade
        lookup alone is variant-blind and can return a base cover's FMV for a
        Newsstand variant (a different price tier). When set, scope to that
        exact variant. (Base/NULL-variant disambiguation is done caller-side in
        comic-fmv's _db_lookup, since an absent query param can't express NULL.)
    max_age_days: if set, only return rows where the joined fmv.updated_at
        is within the last N days. Stale rows are excluded so callers can't
        accidentally reuse outdated FMVs.
    """
    clauses: list[str] = []
    params: list[Any] = []
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
    if locg_id is not None:
        clauses.append("c.locg_id = ?")
        params.append(locg_id)
    if locg_variant_id is not None:
        clauses.append("c.locg_variant_id = ?")
        params.append(locg_variant_id)
    if max_age_days is not None:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max_age_days)).isoformat()
        clauses.append("f.updated_at IS NOT NULL AND f.updated_at >= ?")
        params.append(cutoff)
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


# ---------------------------------------------------------------------------
# Seller-scan seen-tracking (BUI-113)
# ---------------------------------------------------------------------------


def get_seen_item_ids(
    conn: sqlite3.Connection, seller: str | None = None
) -> set[str]:
    """Return the set of seller-scan item_ids already surfaced.

    `seller` is an optional filter; omitted (the default) returns every seen
    item_id, which is what seller_scan.py wants — item_ids are globally unique
    on eBay, so a match surfaced under any seller shouldn't re-appear.
    """
    if seller is not None:
        rows = conn.execute(
            "SELECT item_id FROM seller_scan_seen WHERE seller=?", (seller,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT item_id FROM seller_scan_seen").fetchall()
    return {r["item_id"] for r in rows}


def mark_items_seen(
    conn: sqlite3.Connection, item_ids: list[str], seller: str | None = None
) -> int:
    """Record item_ids as surfaced. Returns the number of newly-inserted rows.

    INSERT OR IGNORE preserves the original first_seen_at (and seller) on a
    re-mark, so the timestamp reflects when a match was *first* shown.
    """
    inserted = 0
    for item_id in item_ids:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seller_scan_seen (item_id, seller) VALUES (?, ?)",
            (item_id, seller),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted
