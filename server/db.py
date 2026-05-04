from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".gixen-server" / "db.sqlite"

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
"""


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
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError as e:
            # Idempotent column adds: ignore "duplicate column name". Anything
            # else (disk full, locked DB, syntax error in a future migration)
            # should not be silently swallowed.
            if "duplicate column" not in str(e).lower():
                raise

    # Backfill bid_comics from existing bids.comic_id values. INSERT OR IGNORE
    # makes this idempotent — re-running on an already-migrated DB is a no-op.
    conn.execute(
        """
        INSERT OR IGNORE INTO bid_comics (bid_id, comic_id, is_primary)
        SELECT id, comic_id, 1 FROM bids WHERE comic_id IS NOT NULL
        """
    )
    conn.commit()


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:
        conn.close()
        raise
    _apply_migrations(conn)
    os.chmod(path, 0o600)
    return conn


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


def insert_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    comic_id: int | None,
    bid_offset: int,
    snipe_group: int,
    seller: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, comic_id, bid_offset, snipe_group, seller)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, comic_id, bid_offset, snipe_group, seller),
    )
    conn.commit()
    return cur.lastrowid


def get_bid_by_item_id(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def link_comic_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    comic_id: int,
    is_primary: bool = False,
) -> None:
    """Add a comic to a bid's set. If is_primary, demote any prior primary,
    promote this one, and mirror to bids.comic_id (backward-compat pointer).
    Idempotent: re-running with the same args is a no-op aside from primary
    bookkeeping."""
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


def update_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    bid_offset: int,
    snipe_group: int,
) -> None:
    conn.execute(
        "UPDATE bids SET max_bid=?, bid_offset=?, snipe_group=? WHERE item_id=? AND status='PENDING'",
        (max_bid, bid_offset, snipe_group, item_id),
    )
    conn.commit()


def update_bid_status(
    conn: sqlite3.Connection,
    item_id: str,
    status: str,
    winning_bid: float | None = None,
    resolved_at: str | None = None,
    status_mirror: str | None = None,
) -> None:
    # COALESCE on status_mirror so callers that don't have a fresh mirror value
    # (e.g. the eBay fallback path) don't clobber the last-known mirror status.
    # Caller must conn.commit() — this helper is hot-path inside loops where
    # the caller batches the commit at the end of the cycle.
    conn.execute(
        "UPDATE bids SET status=?, winning_bid=?, resolved_at=?, "
        "status_mirror=COALESCE(?, status_mirror) "
        "WHERE item_id=? AND status NOT IN ('PURGED')",
        (status, winning_bid, resolved_at, status_mirror, item_id),
    )


def cache_gixen_data(
    conn: sqlite3.Connection,
    item_id: str,
    title: str | None,
    seller: str | None,
    current_bid: str | None,
) -> None:
    """Cache Gixen-sourced fields. Does not touch auction_end_at — that's
    eBay's domain (Gixen only provides relative time-to-end). COALESCE keeps
    the existing value when the caller passes None.

    cached_at is only refreshed when at least one input field is non-NULL,
    so all-NULL writes (common for SCHEDULED snipes whose Gixen row hasn't
    populated current_bid yet) don't make the freshness indicator lie about
    when we last got real data.

    Caller must conn.commit() — this helper is hot-path inside the
    _sync_gixen loop where commits are batched at the end of the cycle.
    """
    has_data = any(v is not None for v in (title, seller, current_bid))
    if not has_data:
        return  # nothing to write, don't bump cached_at
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE bids SET "
        "ebay_title=COALESCE(?, ebay_title), "
        "seller=COALESCE(?, seller), "
        "cached_current_bid=COALESCE(?, cached_current_bid), "
        "cached_at=? "
        "WHERE item_id=? AND status NOT IN ('PURGED')",
        (title, seller, current_bid, now, item_id),
    )


def delete_bid(conn: sqlite3.Connection, item_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id=? AND status NOT IN ('PURGED')",
        (now, item_id),
    )
    conn.commit()


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


def get_all_bids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bids ORDER BY added_at DESC").fetchall()


def get_pending_bids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bids WHERE status='PENDING'").fetchall()


def mark_bids_purged(conn: sqlite3.Connection, item_ids: list[str]) -> None:
    if not item_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    # placeholders contains only '?' chars — no user data is interpolated
    placeholders = ",".join("?" * len(item_ids))
    conn.execute(
        f"UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id IN ({placeholders})",
        [now, *item_ids],
    )
    conn.commit()


def set_auction_end_time(conn: sqlite3.Connection, item_id: str, end_time_iso: str) -> None:
    conn.execute(
        "UPDATE bids SET auction_end_at=? WHERE item_id=? AND status='PENDING'",
        (end_time_iso, item_id),
    )
    conn.commit()


def get_bids_ready_to_snipe(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """Return PENDING bids whose fire time (auction_end_at - bid_offset) has arrived."""
    return conn.execute(
        """
        SELECT * FROM bids
        WHERE status = 'PENDING'
          AND local_snipe_at IS NULL
          AND auction_end_at IS NOT NULL
          AND datetime(auction_end_at, '-' || bid_offset || ' seconds') <= datetime(?)
        """,
        (now_iso,),
    ).fetchall()


def set_local_snipe_result(
    conn: sqlite3.Connection,
    item_id: str,
    fired_at: str,
    result: str,
) -> None:
    conn.execute(
        "UPDATE bids SET local_snipe_at=?, local_snipe_result=? WHERE item_id=? AND status='PENDING'",
        (fired_at, result, item_id),
    )
    conn.commit()
