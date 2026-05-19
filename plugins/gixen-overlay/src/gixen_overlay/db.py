"""Comic-specific database functions for the gixen-overlay plugin.

These functions were extracted from server/db.py (PER-30). The plugin owns
its own data layer; core gixen-cli only provides generic bid functions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Table creation (called from register_db_tables hookimpl)
# ---------------------------------------------------------------------------


def create_tables(conn: sqlite3.Connection) -> None:
    """Create comics and bid_comics tables. Idempotent (IF NOT EXISTS)."""
    conn.execute("""
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
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_comics (
            bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, comic_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bid_comics_bid ON bid_comics(bid_id)"
    )
    conn.execute("""
        INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary)
        SELECT id, comic_id, 1 FROM bids WHERE comic_id IS NOT NULL
    """)


# ---------------------------------------------------------------------------
# Comic CRUD
# ---------------------------------------------------------------------------


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int,
    grade: float | None,
    fmv_low: float | None,
    fmv_high: float | None,
    fmv_comps: int | None,
    fmv_confidence: str | None,
    fmv_notes: str | None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, grade, fmv_low, fmv_high,
                            fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at,
                            locg_id, locg_variant_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year, grade) DO UPDATE SET
            fmv_low         = COALESCE(excluded.fmv_low,        fmv_low),
            fmv_high        = COALESCE(excluded.fmv_high,       fmv_high),
            fmv_comps       = COALESCE(excluded.fmv_comps,      fmv_comps),
            fmv_confidence  = COALESCE(excluded.fmv_confidence, fmv_confidence),
            fmv_notes       = COALESCE(excluded.fmv_notes,      fmv_notes),
            fmv_updated_at  = CASE WHEN excluded.fmv_low IS NOT NULL THEN excluded.fmv_updated_at ELSE fmv_updated_at END,
            locg_id         = COALESCE(excluded.locg_id,         locg_id),
            locg_variant_id = COALESCE(excluded.locg_variant_id, locg_variant_id)
        """,
        (title, issue, year, grade, fmv_low, fmv_high,
         fmv_comps, fmv_confidence, fmv_notes, now,
         locg_id, locg_variant_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year=? AND grade IS ?",
        (title, issue, year, grade),
    ).fetchone()
    return row["id"]


def link_comic_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    comic_id: int,
    is_primary: bool = False,
) -> None:
    """Add a comic to a bid's set. Idempotent; primary bookkeeping mirrors to bids.comic_id."""
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


def get_comics_for_bid(conn: sqlite3.Connection, bid_id: int) -> list[sqlite3.Row]:
    """All comics linked to a bid, primary first, then by numeric issue order."""
    return conn.execute(
        """
        SELECT c.*, bc.is_primary
        FROM bid_comics bc
        JOIN comics c ON c.id = bc.comic_id
        WHERE bc.bid_id = ?
        ORDER BY bc.is_primary DESC,
                 CAST(c.issue AS INTEGER),
                 c.issue
        """,
        (bid_id,),
    ).fetchall()


def get_primary_comic_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*
        FROM bid_comics bc
        JOIN comics c ON c.id = bc.comic_id
        WHERE bc.bid_id = ? AND bc.is_primary = 1
        LIMIT 1
        """,
        (bid_id,),
    ).fetchone()


def list_comics(
    conn: sqlite3.Connection,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if title is not None:
        clauses.append("LOWER(title) = LOWER(?)")
        params.append(title)
    if issue is not None:
        clauses.append("issue = ?")
        params.append(issue)
    if year is not None:
        clauses.append("year = ?")
        params.append(year)
    if grade is not None:
        clauses.append("grade = ?")
        params.append(grade)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT * FROM comics {where} ORDER BY id",
        params,
    ).fetchall()
