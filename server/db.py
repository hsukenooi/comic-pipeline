from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".gixen-server" / "db.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    id              INTEGER PRIMARY KEY,
    item_id         TEXT NOT NULL,
    comic_id        INTEGER,
    max_bid         REAL NOT NULL,
    bid_offset      INTEGER DEFAULT 6,
    snipe_group     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED','REMOVED')),
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
"""


_COLUMN_MIGRATIONS = [
    # bids columns added since the original schema
    "ALTER TABLE bids ADD COLUMN ebay_title TEXT",
    "ALTER TABLE bids ADD COLUMN status_mirror TEXT",
    "ALTER TABLE bids ADD COLUMN cached_current_bid TEXT",
    "ALTER TABLE bids ADD COLUMN cached_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_result TEXT",
    # Plain INTEGER (no FK) so gixen-cli starts cleanly without the plugin.
    # The plugin reads/writes this column when present.
    "ALTER TABLE bids ADD COLUMN fmv_id INTEGER",
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

    # Remove the FK on bids.comic_id for existing databases that were created
    # before this refactor. SQLite has no ALTER TABLE DROP CONSTRAINT, so we
    # must rebuild the table. PRAGMA foreign_keys cannot be changed inside an
    # active transaction — it must precede any BEGIN/SAVEPOINT.
    fk_rows = conn.execute("PRAGMA foreign_key_list(bids)").fetchall()
    if any(row["table"] == "comics" for row in fk_rows):
        conn.execute("DROP TABLE IF EXISTS bids_old")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("SAVEPOINT fk_rebuild")
        try:
            conn.execute("ALTER TABLE bids RENAME TO bids_old")
            conn.execute("""
                CREATE TABLE bids (
                    id              INTEGER PRIMARY KEY,
                    item_id         TEXT NOT NULL,
                    comic_id        INTEGER,
                    max_bid         REAL NOT NULL,
                    bid_offset      INTEGER DEFAULT 6,
                    snipe_group     INTEGER DEFAULT 0,
                    status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED','REMOVED')),
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
                )
            """)
            conn.execute("""
                INSERT INTO bids (
                    id, item_id, comic_id, max_bid, bid_offset, snipe_group,
                    status, winning_bid, seller, auction_end_at, local_snipe_at,
                    local_snipe_result, notes, added_at, resolved_at,
                    ebay_title, status_mirror, cached_current_bid, cached_at
                )
                SELECT
                    id, item_id, comic_id, max_bid, bid_offset, snipe_group,
                    status, winning_bid, seller, auction_end_at, local_snipe_at,
                    local_snipe_result, notes, added_at, resolved_at,
                    ebay_title, status_mirror, cached_current_bid, cached_at
                FROM bids_old
            """)
            conn.execute("DROP TABLE bids_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id)")
            conn.execute("RELEASE fk_rebuild")
        except Exception:
            try:
                conn.execute("ROLLBACK TO fk_rebuild")
            except Exception:
                pass
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    # Rename the soft-delete tombstone status PURGED -> REMOVED (BUI-49). Two
    # parts: (1) widen the CHECK to *allow* REMOVED, and (2) remap existing data.
    #
    # (1) SQLite can't ALTER a CHECK constraint, so widening the allowed-status
    # set requires a table rebuild (same pattern as the FK removal above).
    # Idempotency is by feature detection: only rebuild while the live CHECK
    # still lacks REMOVED. The INSERT copies EVERY column (introspected from the
    # live table) verbatim, so no column is silently dropped — the FK-rebuild
    # above hardcodes a column list that omits fmv_id, and this must not repeat
    # that trap (BUI-49 plan KTD-3). bid_fmvs has an FK to bids(id), so FK
    # enforcement is disabled for the rebuild, like the FK removal above.
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bids'"
    ).fetchone()
    if table_sql_row and "REMOVED" not in (table_sql_row["sql"] or ""):
        cols = ", ".join(row[1] for row in conn.execute("PRAGMA table_info(bids)"))
        conn.execute("DROP TABLE IF EXISTS bids_status_rename_old")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("SAVEPOINT status_rename")
        try:
            conn.execute("ALTER TABLE bids RENAME TO bids_status_rename_old")
            conn.execute("""
                CREATE TABLE bids (
                    id              INTEGER PRIMARY KEY,
                    item_id         TEXT NOT NULL,
                    comic_id        INTEGER,
                    max_bid         REAL NOT NULL,
                    bid_offset      INTEGER DEFAULT 6,
                    snipe_group     INTEGER DEFAULT 0,
                    status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED','REMOVED')),
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
                    cached_at           TEXT,
                    fmv_id              INTEGER
                )
            """)
            conn.execute(
                f"INSERT INTO bids ({cols}) SELECT {cols} FROM bids_status_rename_old"
            )
            conn.execute("DROP TABLE bids_status_rename_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id)")
            conn.execute("RELEASE status_rename")
        except Exception:
            try:
                conn.execute("ROLLBACK TO status_rename")
            except Exception:
                pass
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    # (2) Remap the tombstone value. Runs in all cases — whether the CHECK was
    # just widened above, or already allowed REMOVED on a fresh / FK-rebuilt DB
    # that still held legacy PURGED rows. Idempotent: matches 0 rows once done.
    conn.execute("UPDATE bids SET status='REMOVED' WHERE status='PURGED'")
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


def insert_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    bid_offset: int,
    snipe_group: int,
    seller: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, bid_offset, snipe_group, seller)
        VALUES (?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, bid_offset, snipe_group, seller),
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
    # COALESCE on status_mirror so callers that don't have a fresh mirror value
    # (e.g. the eBay fallback path) don't clobber the last-known mirror status.
    # Caller must conn.commit() — this helper is hot-path inside loops where
    # the caller batches the commit at the end of the cycle.
    conn.execute(
        "UPDATE bids SET status=?, winning_bid=?, resolved_at=?, "
        "auction_end_at=COALESCE(auction_end_at, ?), "
        "status_mirror=COALESCE(?, status_mirror) "
        "WHERE item_id=? AND status NOT IN ('PURGED', 'REMOVED')",
        (status, winning_bid, resolved_at, resolved_at, status_mirror, item_id),
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
        "WHERE item_id=? AND status NOT IN ('PURGED', 'REMOVED')",
        (title, seller, current_bid, now, item_id),
    )


def delete_bid(conn: sqlite3.Connection, item_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    # Soft-delete tombstone. Renamed PURGED -> REMOVED in BUI-49; skip rows that
    # already carry either tombstone value so we don't re-stamp resolved_at.
    conn.execute(
        "UPDATE bids SET status='REMOVED', resolved_at=? WHERE item_id=? AND status NOT IN ('PURGED', 'REMOVED')",
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
    # Tombstone completed bids. Renamed PURGED -> REMOVED in BUI-49.
    conn.execute(
        f"UPDATE bids SET status='REMOVED', resolved_at=? WHERE item_id IN ({placeholders})",
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
