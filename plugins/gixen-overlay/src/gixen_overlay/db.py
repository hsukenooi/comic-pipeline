"""Comic-specific database functions for the gixen-overlay plugin."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

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
    _migrate_fmv_split(conn)
    _migrate_year_nullable(conn)
    # Partial unique indexes go AFTER migrations so the legacy duplicate-row
    # cleanup (fmv-split collapses (title, issue, year, grade) duplicates into
    # one comic) has run before we try to enforce uniqueness on the cleaned set.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiy "
        "ON comics(title, issue, year) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_ti_nullyear "
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

    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")

    logger.info("year-nullable migration complete: %d fmv, %d bid_fmvs restored",
                len(saved_fmv), len(saved_bid_fmvs))


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
) -> int:
    """Upsert a comic identity row. Returns the comic id.

    Year is optional. Reconciliation rules keep at most one row per
    (title, issue) logical comic:

    - Yeared insert finds an existing yeared row at the same year → updates
      locg metadata, returns it.
    - Yeared insert finds an existing yearless row for the same (title, issue)
      → promotes it (UPDATE comics SET year=?), returns it. Avoids creating a
      duplicate alongside the yearless placeholder.
    - Yearless insert finds an existing yeared row for the same (title, issue)
      → prefers the yeared one (returns its id without creating a yearless
      duplicate). Locg metadata still gets merged in.
    - Yearless insert finds an existing yearless row → updates locg, returns.

    When multiple yeared rows exist for the same (title, issue) — pre-PER-98
    historical data — the one with locg_id set wins; ties broken by lowest id.
    """
    if year is not None:
        # Yeared insert.
        existing_yeared = conn.execute(
            "SELECT id FROM comics WHERE title=? AND issue=? AND year=?",
            (title, issue, year),
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
            "SELECT id FROM comics WHERE title=? AND issue=? AND year IS NULL",
            (title, issue),
        ).fetchone()
        if existing_yearless is not None:
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
            "INSERT INTO comics (title, issue, year, locg_id, locg_variant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, issue, year, locg_id, locg_variant_id),
        )
        conn.commit()
        return cur.lastrowid

    # Yearless insert. Prefer an existing yeared row if one exists — never
    # create a yearless duplicate next to a yeared canonical row.
    canonical_yeared = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year IS NOT NULL "
        "ORDER BY (locg_id IS NULL), id LIMIT 1",
        (title, issue),
    ).fetchone()
    if canonical_yeared is not None:
        conn.execute(
            "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
            "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
            (locg_id, locg_variant_id, canonical_yeared["id"]),
        )
        conn.commit()
        return canonical_yeared["id"]
    existing_yearless = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year IS NULL",
        (title, issue),
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
        "INSERT INTO comics (title, issue, year, locg_id, locg_variant_id) "
        "VALUES (?, ?, NULL, ?, ?)",
        (title, issue, locg_id, locg_variant_id),
    )
    conn.commit()
    return cur.lastrowid


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
    locg_id: int | None = None,
    max_age_days: float | None = None,
) -> list[sqlite3.Row]:
    """Return comics enriched with FMV data. One row per (comic, fmv) pair.

    locg_id: filter to one canonical issue (used by comic-fmv to look up a
        fresh FMV by LOCG ID + grade without juggling title spellings).
    max_age_days: if set, only return rows where the joined fmv.updated_at
        is within the last N days. Stale rows are excluded so callers can't
        accidentally reuse outdated FMVs.
    """
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
    if locg_id is not None:
        clauses.append("c.locg_id = ?")
        params.append(locg_id)
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
