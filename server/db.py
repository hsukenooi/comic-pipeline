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
    auction_end_at  TEXT,
    notes           TEXT,
    added_at        TEXT DEFAULT (datetime('now')),
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
"""


_COLUMN_MIGRATIONS = [
    "ALTER TABLE bids ADD COLUMN ebay_title TEXT",
    "ALTER TABLE bids ADD COLUMN status_mirror TEXT",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists


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
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, grade, fmv_low, fmv_high,
                            fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year, grade) DO UPDATE SET
            fmv_low         = excluded.fmv_low,
            fmv_high        = excluded.fmv_high,
            fmv_comps       = excluded.fmv_comps,
            fmv_confidence  = excluded.fmv_confidence,
            fmv_notes       = excluded.fmv_notes,
            fmv_updated_at  = excluded.fmv_updated_at
        """,
        (title, issue, year, grade, fmv_low, fmv_high,
         fmv_comps, fmv_confidence, fmv_notes, now),
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
    conn.execute(
        "UPDATE bids SET status=?, winning_bid=?, resolved_at=?, status_mirror=? WHERE item_id=? AND status NOT IN ('PURGED')",
        (status, winning_bid, resolved_at, status_mirror, item_id),
    )
    conn.commit()


def cache_ebay_data(
    conn: sqlite3.Connection,
    item_id: str,
    title: str | None,
    seller: str | None,
    end_at: str | None,
) -> None:
    conn.execute(
        "UPDATE bids SET ebay_title=?, seller=?, auction_end_at=? WHERE item_id=? AND status NOT IN ('PURGED')",
        (title, seller, end_at, item_id),
    )
    conn.commit()


def delete_bid(conn: sqlite3.Connection, item_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id=? AND status NOT IN ('PURGED')",
        (now, item_id),
    )
    conn.commit()


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
