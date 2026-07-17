"""Unit tests for server/db.py — all use tmp_path, no disk side effects."""
import sqlite3
import pytest
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import (
    init_db, insert_bid, get_bid_by_item_id, get_pending_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged,
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_init_creates_tables(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}
    assert "bids" in tables
    assert "comics" not in tables
    assert "bid_comics" not in tables


def test_bids_comic_id_has_no_foreign_key(db):
    """Fresh init_db creates bids.comic_id with no FK declaration."""
    fk_rows = db.execute("PRAGMA foreign_key_list(bids)").fetchall()
    assert len(fk_rows) == 0


def test_wal_mode_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_insert_bid(db):
    bid_id = insert_bid(db, item_id="123456789", max_bid=800.0,
                        bid_offset=6, snipe_group=0,
                        seller="seller1")
    assert isinstance(bid_id, int)
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["item_id"] == "123456789"
    assert row["status"] == "PENDING"
    assert row["max_bid"] == 800.0


def test_get_bid_by_item_id(db):
    insert_bid(db, "111222333", 50.0, 6, 0, "s")
    row = get_bid_by_item_id(db, "111222333")
    assert row is not None
    assert row["item_id"] == "111222333"


def test_get_bid_by_item_id_missing(db):
    assert get_bid_by_item_id(db, "999999999") is None


def test_get_pending_bid_by_item_id_returns_pending(db):
    insert_bid(db, "555000111", 40.0, 6, 0, "s")
    row = get_pending_bid_by_item_id(db, "555000111")
    assert row is not None
    assert row["item_id"] == "555000111"
    assert row["status"] == "PENDING"


def test_get_pending_bid_by_item_id_none_when_only_terminal(db):
    insert_bid(db, "555000222", 40.0, 6, 0, "s")
    update_bid_status(db, "555000222", "ENDED", resolved_at="2026-06-01T00:00:00+00:00")
    db.commit()
    assert get_pending_bid_by_item_id(db, "555000222") is None


def test_get_pending_bid_by_item_id_none_when_unknown(db):
    assert get_pending_bid_by_item_id(db, "999999999") is None


def test_get_pending_bid_by_item_id_ignores_newer_tombstone(db):
    """A newer REMOVED row must not shadow the live PENDING row — the exact
    case get_bid_by_item_id (latest-of-any-status) gets wrong."""
    pending_id = insert_bid(db, "555000333", 40.0, 6, 0, "s")
    # A later, higher-id tombstone for the same item (e.g. a removed re-add).
    db.execute(
        "INSERT INTO bids (item_id, max_bid, status) VALUES ('555000333', 99.0, 'REMOVED')"
    )
    db.commit()
    # get_bid_by_item_id would return the newer REMOVED row...
    assert get_bid_by_item_id(db, "555000333")["status"] == "REMOVED"
    # ...but the PENDING-specific lookup returns the live row.
    row = get_pending_bid_by_item_id(db, "555000333")
    assert row is not None
    assert row["id"] == pending_id
    assert row["status"] == "PENDING"


def test_update_bid(db):
    insert_bid(db, "444555666", 50.0, 6, 0, "s")
    update_bid(db, "444555666", max_bid=60.0, bid_offset=10, snipe_group=1)
    row = get_bid_by_item_id(db, "444555666")
    assert row["max_bid"] == 60.0
    assert row["snipe_group"] == 1


def test_update_bid_status(db):
    insert_bid(db, "777888999", 100.0, 6, 0, "s")
    update_bid_status(db, "777888999", status="WON",
                      winning_bid=85.0, resolved_at="2026-04-25T12:00:00")
    row = get_bid_by_item_id(db, "777888999")
    assert row["status"] == "WON"
    assert row["winning_bid"] == 85.0
    assert row["resolved_at"] == "2026-04-25T12:00:00"


def test_delete_bid_marks_purged(db):
    insert_bid(db, "555444333", 30.0, 6, 0, "s")
    delete_bid(db, "555444333")
    row = get_bid_by_item_id(db, "555444333")
    assert row["status"] == "REMOVED"


def test_delete_bid_marks_won_bid_purged(db):
    insert_bid(db, "666777888", 50.0, 6, 0, "s")
    update_bid_status(db, "666777888", status="WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    delete_bid(db, "666777888")
    row = get_bid_by_item_id(db, "666777888")
    assert row["status"] == "REMOVED"


def test_get_all_bids_returns_list(db):
    insert_bid(db, "100000001", 10.0, 6, 0, "s")
    insert_bid(db, "100000002", 20.0, 6, 0, "s")
    rows = get_all_bids(db)
    item_ids = [r["item_id"] for r in rows]
    assert "100000001" in item_ids
    assert "100000002" in item_ids


def test_mark_bids_purged_sets_status(db):
    # mark_bids_purged is the completed-sweep, so it runs against resolved bids.
    insert_bid(db, "200000001", 50.0, 6, 0, "s")
    insert_bid(db, "200000002", 60.0, 6, 0, "s")
    update_bid_status(db, "200000001", status="WON", winning_bid=45.0,
                      resolved_at="2026-04-25T10:00:00")
    update_bid_status(db, "200000002", status="LOST", winning_bid=None,
                      resolved_at="2026-04-25T10:00:00")
    mark_bids_purged(db, ["200000001", "200000002"])
    row1 = get_bid_by_item_id(db, "200000001")
    row2 = get_bid_by_item_id(db, "200000002")
    assert row1["status"] == "REMOVED"
    assert row2["status"] == "REMOVED"
    assert row1["resolved_at"] is not None


def test_mark_bids_purged_spares_live_pending_sharing_item_id(db):
    """BUI-178: a completed-sweep must not tombstone a live PENDING row that
    shares an item_id with an old resolved row (a re-listed/re-added item).
    Only the resolved row is tombstoned; the live snipe survives.
    """
    # Old win for this item, then a re-add creates a new live PENDING row.
    won_id = insert_bid(db, "200000003", 50.0, 6, 0, "s")
    update_bid_status(db, "200000003", status="WON", winning_bid=45.0,
                      resolved_at="2026-04-25T10:00:00")
    pending_id = insert_bid(db, "200000003", 70.0, 6, 0, "s")

    mark_bids_purged(db, ["200000003"])

    won_row = db.execute("SELECT status FROM bids WHERE id=?", (won_id,)).fetchone()
    pending_row = db.execute("SELECT status FROM bids WHERE id=?",
                             (pending_id,)).fetchone()
    assert won_row["status"] == "REMOVED"        # the resolved row is swept
    assert pending_row["status"] == "PENDING"    # the live snipe is spared


def test_mark_bids_purged_transitions_won_bid(db):
    insert_bid(db, "200000003", 50.0, 6, 0, "s")
    update_bid_status(db, "200000003", "WON", winning_bid=42.0, resolved_at="2026-04-25T10:00:00")
    mark_bids_purged(db, ["200000003"])
    row = get_bid_by_item_id(db, "200000003")
    assert row["status"] == "REMOVED"
    assert row["winning_bid"] == 42.0


def test_mark_bids_purged_empty_list_is_noop(db):
    insert_bid(db, "200000004", 50.0, 6, 0, "s")
    mark_bids_purged(db, [])
    row = get_bid_by_item_id(db, "200000004")
    assert row["status"] == "PENDING"


def test_update_bid_noop_on_non_pending(db):
    insert_bid(db, "300000001", 50.0, 6, 0, "s")
    update_bid_status(db, "300000001", "WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    update_bid(db, "300000001", max_bid=999.0, bid_offset=6, snipe_group=0)
    row = get_bid_by_item_id(db, "300000001")
    assert row["max_bid"] == 50.0  # unchanged — update_bid guards on status='PENDING'


def test_update_bid_none_snipe_group_noop_on_non_pending(db):
    """The snipe_group=None passthrough (BUI-392) assembles a different SET list
    than the explicit-int branch — confirm it still carries the same
    WHERE status='PENDING' guard rather than silently dropping it."""
    insert_bid(db, "300000002", 50.0, 6, 4, "s")
    update_bid_status(db, "300000002", "WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    update_bid(db, "300000002", max_bid=999.0, bid_offset=6, snipe_group=None)
    row = get_bid_by_item_id(db, "300000002")
    assert row["max_bid"] == 50.0  # unchanged — the None branch guards on status='PENDING' too


# ---------------------------------------------------------------------------
# FK-removal migration
# ---------------------------------------------------------------------------

def test_fk_removal_migration_drops_comics_reference(tmp_path):
    """On a legacy DB with bids.comic_id REFERENCES comics(id), init_db removes
    the FK. Existing bids rows survive the table rebuild intact."""
    legacy_db_path = tmp_path / "legacy.db"

    # Build a minimal legacy DB with the old bids schema (FK present) + comics.
    raw = sqlite3.connect(str(legacy_db_path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            grade REAL
        );
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER REFERENCES comics(id),
            max_bid REAL NOT NULL,
            bid_offset INTEGER DEFAULT 6,
            snipe_group INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDING',
            winning_bid REAL,
            seller TEXT,
            auction_end_at TEXT,
            local_snipe_at TEXT,
            local_snipe_result TEXT,
            notes TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
    """)
    raw.execute("INSERT INTO comics (title, issue, year) VALUES ('Hulk', '181', 1974)")
    raw.execute("INSERT INTO bids (item_id, max_bid, comic_id) VALUES ('legacy001', 50.0, 1)")
    raw.commit()
    raw.close()

    conn = init_db(legacy_db_path)
    try:
        fk_after = conn.execute("PRAGMA foreign_key_list(bids)").fetchall()
        assert not any(row["table"] == "comics" for row in fk_after)
        row = conn.execute(
            "SELECT item_id, max_bid, comic_id FROM bids WHERE item_id='legacy001'"
        ).fetchone()
        assert row is not None
        assert row["max_bid"] == 50.0
        assert row["comic_id"] == 1
    finally:
        conn.close()


def test_fk_removal_migration_is_idempotent(tmp_path):
    """Calling init_db twice on a DB that already has no FK is a no-op."""
    db_path = tmp_path / "nofk.db"
    conn = init_db(db_path)
    assert len(conn.execute("PRAGMA foreign_key_list(bids)").fetchall()) == 0
    conn.close()

    conn2 = init_db(db_path)
    assert len(conn2.execute("PRAGMA foreign_key_list(bids)").fetchall()) == 0
    conn2.close()


def test_fk_removal_migration_preserves_fmv_id_and_later_columns(tmp_path):
    """BUI-64: a legacy DB that has BOTH the comics FK and a populated fmv_id
    must keep fmv_id (and every other column) through the FK-removal rebuild.
    The rebuild previously hardcoded a column list that dropped fmv_id."""
    legacy_db_path = tmp_path / "legacy_fmv.db"
    raw = sqlite3.connect(str(legacy_db_path))
    raw.execute("PRAGMA journal_mode=WAL")
    # Old bids schema WITH the comics FK and the full later column set,
    # including fmv_id populated with real data.
    raw.executescript("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER NOT NULL, grade REAL
        );
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER REFERENCES comics(id),
            max_bid REAL NOT NULL,
            bid_offset INTEGER DEFAULT 6,
            snipe_group INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDING',
            winning_bid REAL,
            seller TEXT,
            auction_end_at TEXT,
            local_snipe_at TEXT,
            local_snipe_result TEXT,
            notes TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT,
            ebay_title TEXT,
            status_mirror TEXT,
            cached_current_bid TEXT,
            cached_at TEXT,
            fmv_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
    """)
    raw.execute("INSERT INTO comics (title, issue, year) VALUES ('Hulk', '181', 1974)")
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, comic_id, fmv_id, ebay_title) "
        "VALUES ('legacy_fmv001', 50.0, 1, 42, 'Hulk #181')"
    )
    raw.commit()
    raw.close()

    conn = init_db(legacy_db_path)
    try:
        # FK is gone...
        fk_after = conn.execute("PRAGMA foreign_key_list(bids)").fetchall()
        assert not any(row["table"] == "comics" for row in fk_after)
        # ...and the fmv_id column + its data survived the rebuild.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
        assert "fmv_id" in cols
        row = conn.execute(
            "SELECT comic_id, fmv_id, ebay_title FROM bids WHERE item_id='legacy_fmv001'"
        ).fetchone()
        assert row["fmv_id"] == 42
        assert row["comic_id"] == 1
        assert row["ebay_title"] == "Hulk #181"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# fmv_id column migration
# ---------------------------------------------------------------------------


def test_bids_fmv_id_column_present_on_fresh_db(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    assert "fmv_id" in cols
    conn.close()


def test_bids_fmv_id_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idem.db"
    conn = init_db(db_path)
    conn.close()
    conn2 = init_db(db_path)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(bids)")}
    assert "fmv_id" in cols
    conn2.close()


# ---------------------------------------------------------------------------
# seller_grade / photo_grade column migration (BUI-78)
# ---------------------------------------------------------------------------


def test_bids_grade_columns_present_on_fresh_db(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    assert "seller_grade" in cols
    assert "photo_grade" in cols
    conn.close()


def test_bids_grade_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idem_grades.db"
    conn = init_db(db_path)
    conn.close()
    conn2 = init_db(db_path)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(bids)")}
    assert "seller_grade" in cols
    assert "photo_grade" in cols
    conn2.close()


# ---------------------------------------------------------------------------
# gixen_vanished_at column migration (BUI-371)
# ---------------------------------------------------------------------------


def test_bids_gixen_vanished_at_column_present_on_fresh_db(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    assert "gixen_vanished_at" in cols
    conn.close()


def test_bids_gixen_vanished_at_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idem_vanish.db"
    conn = init_db(db_path)
    conn.close()
    conn2 = init_db(db_path)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(bids)")}
    assert "gixen_vanished_at" in cols
    conn2.close()


def test_gixen_vanished_at_present_after_legacy_rebuild(tmp_path):
    """A pre-BUI-49 DB forces the status-rename table rebuild; the rebuilt
    table (created from _BIDS_TABLE_SQL) must still carry gixen_vanished_at —
    the ALTER runs before the rebuild, so a missing _BIDS_TABLE_SQL entry
    would either drop the column or fail the rebuild's column copy."""
    db_path = tmp_path / "legacy_vanish.db"
    _seed_old_db(db_path)
    conn = init_db(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    assert "gixen_vanished_at" in cols
    # And the rebuild actually happened (CHECK now permits REMOVED).
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bids'"
    ).fetchone()["sql"]
    assert "REMOVED" in sql
    conn.close()


def test_update_bid_clears_vanish_stamp(db):
    """update_bid runs right after a successful Gixen add/modify — first-party
    proof the snipe is live, so any stale vanish stamp must be cleared."""
    insert_bid(db, "900200001", 25.0, 6, 0, None)
    db.execute(
        "UPDATE bids SET gixen_vanished_at='2026-01-01T00:00:00+00:00' "
        "WHERE item_id='900200001'"
    )
    db.commit()
    from server.db import update_bid
    update_bid(db, "900200001", 30.0, 6, 0)
    row = db.execute(
        "SELECT gixen_vanished_at, max_bid FROM bids WHERE item_id='900200001'"
    ).fetchone()
    assert row["gixen_vanished_at"] is None
    assert row["max_bid"] == 30.0


# ---------------------------------------------------------------------------
# ebay_no_price_at column migration (BUI-382)
# ---------------------------------------------------------------------------


def test_bids_ebay_no_price_at_column_present_on_fresh_db(tmp_path):
    conn = init_db(tmp_path / "fresh_no_price.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    assert "ebay_no_price_at" in cols
    conn.close()


def test_bids_ebay_no_price_at_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idem_no_price.db"
    conn = init_db(db_path)
    conn.close()
    conn2 = init_db(db_path)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(bids)")}
    assert "ebay_no_price_at" in cols
    conn2.close()


def test_ebay_no_price_at_present_after_legacy_rebuild(tmp_path):
    """A pre-BUI-49 DB forces the status-rename table rebuild; the rebuilt
    table (created from _BIDS_TABLE_SQL) must still carry ebay_no_price_at —
    the ALTER runs before the rebuild, so a missing _BIDS_TABLE_SQL entry
    would either drop the column or fail the rebuild's column copy."""
    db_path = tmp_path / "legacy_no_price.db"
    _seed_old_db(db_path)
    conn = init_db(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
    assert "ebay_no_price_at" in cols
    # And the rebuild actually happened (CHECK now permits REMOVED).
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bids'"
    ).fetchone()["sql"]
    assert "REMOVED" in sql
    conn.close()


def test_grade_columns_survive_bids_rebuild(tmp_path):
    """BUI-78: seller_grade/photo_grade must be in _BIDS_TABLE_SQL so the
    FK-removal rebuild preserves them (and their data). A legacy DB carrying the
    comics FK forces the rebuild; the columns already exist with data, so the
    rebuild's `INSERT INTO bids (...cols...)` raises 'no such column' unless the
    rebuilt schema declares them."""
    legacy_db_path = tmp_path / "legacy_grades.db"
    raw = sqlite3.connect(str(legacy_db_path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER NOT NULL, grade REAL
        );
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER REFERENCES comics(id),
            max_bid REAL NOT NULL,
            bid_offset INTEGER DEFAULT 6,
            snipe_group INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDING',
            winning_bid REAL,
            seller TEXT,
            auction_end_at TEXT,
            local_snipe_at TEXT,
            local_snipe_result TEXT,
            notes TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT,
            ebay_title TEXT,
            status_mirror TEXT,
            cached_current_bid TEXT,
            cached_at TEXT,
            fmv_id INTEGER,
            seller_grade REAL,
            photo_grade REAL
        );
        CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
    """)
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, comic_id, seller_grade, photo_grade) "
        "VALUES ('legacy_grade001', 50.0, NULL, 9.0, 7.0)"
    )
    raw.commit()
    raw.close()

    conn = init_db(legacy_db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bids)")}
        assert "seller_grade" in cols
        assert "photo_grade" in cols
        row = conn.execute(
            "SELECT seller_grade, photo_grade FROM bids WHERE item_id='legacy_grade001'"
        ).fetchone()
        assert row["seller_grade"] == 9.0
        assert row["photo_grade"] == 7.0
    finally:
        conn.close()


def test_insert_bid_persists_grades(db):
    """BUI-78: insert_bid stores seller_grade/photo_grade when supplied."""
    insert_bid(db, "700000001", 50.0, 6, 0, "someseller",
               seller_grade=9.0, photo_grade=7.5)
    row = get_bid_by_item_id(db, "700000001")
    assert row["seller_grade"] == 9.0
    assert row["photo_grade"] == 7.5


def test_insert_bid_grades_default_null(db):
    """Backward-compat: the existing positional call (no grades) stores NULL."""
    insert_bid(db, "700000002", 50.0, 6, 0, "someseller")
    row = get_bid_by_item_id(db, "700000002")
    assert row["seller_grade"] is None
    assert row["photo_grade"] is None


def test_update_bid_grades_fills_only_nulls(db):
    """BUI-78 (C2): update_bid_grades fills NULL grade/seller columns but never
    overwrites already-set values — completing an incomplete insert, not editing."""
    from server.db import update_bid_grades
    # Row added without grades (seller present).
    insert_bid(db, "700000003", 50.0, 6, 0, "buyer")
    update_bid_grades(db, "700000003", seller=None, seller_grade=9.0, photo_grade=6.5)
    row = get_bid_by_item_id(db, "700000003")
    assert row["seller_grade"] == 9.0
    assert row["photo_grade"] == 6.5
    # A second call with different values must NOT overwrite the set grades.
    update_bid_grades(db, "700000003", seller=None, seller_grade=2.0, photo_grade=1.0)
    row = get_bid_by_item_id(db, "700000003")
    assert row["seller_grade"] == 9.0
    assert row["photo_grade"] == 6.5


def test_update_bid_grades_seller_is_authoritative(db):
    """The buy-flow username is the canonical key, so update_bid_grades overwrites
    a prior (e.g. sync-set store-name) seller — while grades stay fill-NULL only."""
    from server.db import update_bid_grades
    insert_bid(db, "700000006", 50.0, 6, 0, "Beatle Blue Cat Collectibles")  # sync-style store name
    update_bid_grades(db, "700000006", seller="beatlebluecat", seller_grade=9.0, photo_grade=7.0)
    row = get_bid_by_item_id(db, "700000006")
    assert row["seller"] == "beatlebluecat"   # buy-flow username wins
    assert row["seller_grade"] == 9.0
    # A grade already set is NOT overwritten on a later call (fill-NULL).
    update_bid_grades(db, "700000006", seller="beatlebluecat", seller_grade=1.0, photo_grade=1.0)
    row = get_bid_by_item_id(db, "700000006")
    assert row["seller_grade"] == 9.0
    assert row["photo_grade"] == 7.0


def test_cache_gixen_data_does_not_overwrite_existing_seller(db):
    """BUI-78 (A1): a sync must not overwrite an INSERT-time seller username
    with Gixen's scraped store display name."""
    from server.db import cache_gixen_data
    insert_bid(db, "700000004", 50.0, 6, 0, "beatlebluecat")
    cache_gixen_data(db, "700000004", "Some Title", "Beatle Blue Cat Collectibles", "10.00 USD")
    db.commit()
    row = get_bid_by_item_id(db, "700000004")
    assert row["seller"] == "beatlebluecat"  # INSERT value wins


def test_cache_gixen_data_fills_null_seller(db):
    """A1 must still let sync populate seller when it started NULL (web-added)."""
    from server.db import cache_gixen_data
    insert_bid(db, "700000005", 50.0, 6, 0, None)
    cache_gixen_data(db, "700000005", "T", "scraped_seller", "1.00 USD")
    db.commit()
    row = get_bid_by_item_id(db, "700000005")
    assert row["seller"] == "scraped_seller"


# ---------------------------------------------------------------------------
# PURGED -> REMOVED status rename migration (BUI-49)
# ---------------------------------------------------------------------------

# A DB created before BUI-49: the status CHECK lacks 'REMOVED' and the tombstone
# rows are still 'PURGED'. Full current column set so column-preservation can be
# asserted (notably fmv_id, which the FK-rebuild precedent is known to drop).
_OLD_SCHEMA_SQL = """
    CREATE TABLE bids (
        id              INTEGER PRIMARY KEY,
        item_id         TEXT NOT NULL,
        comic_id        INTEGER,
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
        resolved_at         TEXT,
        ebay_title          TEXT,
        status_mirror       TEXT,
        cached_current_bid  TEXT,
        cached_at           TEXT,
        fmv_id              INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
"""


def _seed_old_db(path):
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(_OLD_SCHEMA_SQL)
    # A removed tombstone with a populated fmv_id + ebay_title (preservation),
    # plus untouched non-tombstone rows.
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, fmv_id, ebay_title) "
        "VALUES ('purged001', 50.0, 'PURGED', 12.0, 42, 'Hulk #181')"
    )
    raw.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('pending001', 30.0, 'PENDING')")
    raw.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('won001', 99.0, 'WON')")
    raw.commit()
    raw.close()


def test_status_rename_migration_remaps_purged_to_removed(tmp_path):
    """On a pre-BUI-49 DB, init_db rewrites PURGED rows to REMOVED and leaves
    non-tombstone rows untouched."""
    db_path = tmp_path / "old.db"
    _seed_old_db(db_path)

    conn = init_db(db_path)
    try:
        assert conn.execute(
            "SELECT status FROM bids WHERE item_id='purged001'"
        ).fetchone()["status"] == "REMOVED"
        assert conn.execute("SELECT COUNT(*) FROM bids WHERE status='PURGED'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM bids WHERE item_id='pending001'"
        ).fetchone()["status"] == "PENDING"
        assert conn.execute(
            "SELECT status FROM bids WHERE item_id='won001'"
        ).fetchone()["status"] == "WON"
    finally:
        conn.close()


def test_status_rename_migration_preserves_all_columns(tmp_path):
    """The rebuild must carry every column — guards the fmv_id-drop trap (KTD-3)."""
    db_path = tmp_path / "old.db"
    _seed_old_db(db_path)

    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT max_bid, winning_bid, fmv_id, ebay_title FROM bids WHERE item_id='purged001'"
        ).fetchone()
        assert row["max_bid"] == 50.0
        assert row["winning_bid"] == 12.0
        assert row["fmv_id"] == 42
        assert row["ebay_title"] == "Hulk #181"
        # The column itself must still exist on the rebuilt table.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bids)")}
        assert "fmv_id" in cols
    finally:
        conn.close()


def test_status_rename_migration_is_idempotent(tmp_path):
    """Running init_db again on an already-migrated DB is a no-op (no error, no
    re-migration, data intact)."""
    db_path = tmp_path / "old.db"
    _seed_old_db(db_path)
    conn = init_db(db_path)
    conn.close()

    conn2 = init_db(db_path)
    try:
        assert conn2.execute(
            "SELECT status FROM bids WHERE item_id='purged001'"
        ).fetchone()["status"] == "REMOVED"
        assert conn2.execute("SELECT COUNT(*) FROM bids").fetchone()[0] == 3
    finally:
        conn2.close()


def test_auction_end_at_backfill_from_resolved_at(tmp_path):
    """BUI-83: a resolved (terminal) row whose auction_end_at was never captured
    gets it backfilled from resolved_at, so it isn't lost from both the active
    and history views."""
    db_path = tmp_path / "bf.db"
    conn = init_db(db_path)
    insert_bid(conn, "143000001", 25.0, 6, 0, "s")
    # Simulate a legacy row that resolved before the resolve-time COALESCE
    # backfill landed: terminal status + resolved_at set, auction_end_at NULL.
    conn.execute(
        "UPDATE bids SET status='LOST', resolved_at='2026-05-23T22:33:19+00:00', "
        "auction_end_at=NULL WHERE item_id='143000001'"
    )
    conn.commit()
    conn.close()

    conn2 = init_db(db_path)  # re-run migrations → backfill
    try:
        row = conn2.execute(
            "SELECT auction_end_at FROM bids WHERE item_id='143000001'"
        ).fetchone()
        assert row["auction_end_at"] == "2026-05-23T22:33:19+00:00"
    finally:
        conn2.close()


def test_auction_end_at_backfill_skips_tombstone_and_unresolved(tmp_path):
    """The backfill must not touch the soft-delete tombstone (its resolved_at is
    a removal time, not an auction end) nor a live PENDING row with no
    resolved_at."""
    db_path = tmp_path / "bf2.db"
    conn = init_db(db_path)
    insert_bid(conn, "rem000001", 25.0, 6, 0, "s")
    insert_bid(conn, "pend00001", 25.0, 6, 0, "s")
    conn.execute(
        "UPDATE bids SET status='REMOVED', resolved_at='2026-05-23T00:00:00+00:00', "
        "auction_end_at=NULL WHERE item_id='rem000001'"
    )
    conn.commit()
    conn.close()

    conn2 = init_db(db_path)
    try:
        assert conn2.execute(
            "SELECT auction_end_at FROM bids WHERE item_id='rem000001'"
        ).fetchone()["auction_end_at"] is None
        assert conn2.execute(
            "SELECT auction_end_at FROM bids WHERE item_id='pend00001'"
        ).fetchone()["auction_end_at"] is None
    finally:
        conn2.close()


def test_removed_status_accepted_after_migration(tmp_path):
    """Post-migration, the CHECK accepts an explicit REMOVED insert."""
    db_path = tmp_path / "old.db"
    _seed_old_db(db_path)
    conn = init_db(db_path)
    try:
        conn.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('r1', 1.0, 'REMOVED')")
        conn.commit()
        assert conn.execute(
            "SELECT status FROM bids WHERE item_id='r1'"
        ).fetchone()["status"] == "REMOVED"
    finally:
        conn.close()


# --- BUI-67: collapse duplicate PENDING snipes + partial unique index ---------
#
# Seed via a raw schema that lacks the partial unique index (it's the migration
# being tested), so duplicate PENDING rows can be inserted before init_db runs
# the collapse. Reuses _OLD_SCHEMA_SQL (no unique index, CHECK widened to REMOVED
# by the status-rename migration before the dedup runs).

def _seed_dup_db(path, inserts):
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(_OLD_SCHEMA_SQL)
    for sql in inserts:
        raw.execute(sql)
    raw.commit()
    raw.close()


def test_dedup_collapses_duplicate_pending(tmp_path):
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status, cached_at) "
        "VALUES (10, '236831609134', 20.0, 'PENDING', '2026-06-01T00:00:00+00:00')",
        "INSERT INTO bids (id, item_id, max_bid, status, cached_at) "
        "VALUES (11, '236831609134', 20.0, 'PENDING', '2026-06-01T00:01:00+00:00')",
    ])
    conn = init_db(db_path)
    try:
        pend = conn.execute(
            "SELECT id FROM bids WHERE item_id='236831609134' AND status='PENDING'"
        ).fetchall()
        assert len(pend) == 1
        assert pend[0]["id"] == 11  # MAX(id) survives
        removed = conn.execute(
            "SELECT * FROM bids WHERE item_id='236831609134' AND status='REMOVED'"
        ).fetchone()
        assert removed["notes"] == "deduped BUI-67"
        assert removed["resolved_at"] is not None
        assert "+00:00" in removed["resolved_at"]  # ISO form, matches delete_bid
    finally:
        conn.close()


def test_dedup_forward_fills_survivor(tmp_path):
    """Survivor (MAX id) inherits the older row's auction_end_at/fmv_id and the
    higher max_bid — the data-loss guard."""
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status, auction_end_at, fmv_id, cached_at) "
        "VALUES (10, 'itemA', 20.0, 'PENDING', '2026-06-05T12:00:00+00:00', 42, '2026-06-01T00:05:00+00:00')",
        "INSERT INTO bids (id, item_id, max_bid, status, auction_end_at, fmv_id, cached_at) "
        "VALUES (11, 'itemA', 15.0, 'PENDING', NULL, NULL, NULL)",
    ])
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM bids WHERE item_id='itemA' AND status='PENDING'"
        ).fetchone()
        assert row["id"] == 11
        assert row["auction_end_at"] == "2026-06-05T12:00:00+00:00"
        assert row["fmv_id"] == 42
        assert row["max_bid"] == 20.0
    finally:
        conn.close()


def test_dedup_divergent_end_times_pick_freshest(tmp_path):
    """When both rows carry an auction_end_at, the freshest cached_at wins — even
    if that's the lower-id row, not the survivor's own (stale) value."""
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        # lower id, FRESHER cached_at
        "INSERT INTO bids (id, item_id, max_bid, status, auction_end_at, cached_at) "
        "VALUES (10, 'itemB', 20.0, 'PENDING', '2026-06-05T12:00:30+00:00', '2026-06-01T00:09:00+00:00')",
        # higher id (survivor), STALER cached_at
        "INSERT INTO bids (id, item_id, max_bid, status, auction_end_at, cached_at) "
        "VALUES (11, 'itemB', 20.0, 'PENDING', '2026-06-05T12:00:00+00:00', '2026-06-01T00:01:00+00:00')",
    ])
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM bids WHERE item_id='itemB' AND status='PENDING'"
        ).fetchone()
        assert row["id"] == 11  # survivor is still MAX(id)
        assert row["auction_end_at"] == "2026-06-05T12:00:30+00:00"  # but the fresher end time
    finally:
        conn.close()


def test_dedup_preserves_relisting(tmp_path):
    """An ENDED + a PENDING row for the same item are both left intact — only
    PENDING duplicates collapse."""
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (10, 'itemD', 20.0, 'ENDED')",
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (11, 'itemD', 25.0, 'PENDING')",
    ])
    conn = init_db(db_path)
    try:
        statuses = sorted(
            r["status"] for r in conn.execute("SELECT status FROM bids WHERE item_id='itemD'")
        )
        assert statuses == ["ENDED", "PENDING"]
        assert conn.execute(
            "SELECT COUNT(*) FROM bids WHERE item_id='itemD' AND notes='deduped BUI-67'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_dedup_three_way_collapse(tmp_path):
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status, fmv_id, cached_at) "
        "VALUES (10, 'itemE', 20.0, 'PENDING', 7, '2026-06-01T00:01:00+00:00')",
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (11, 'itemE', 22.0, 'PENDING')",
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (12, 'itemE', 21.0, 'PENDING')",
    ])
    conn = init_db(db_path)
    try:
        pend = conn.execute(
            "SELECT * FROM bids WHERE item_id='itemE' AND status='PENDING'"
        ).fetchall()
        assert len(pend) == 1
        assert pend[0]["id"] == 12
        assert pend[0]["fmv_id"] == 7  # union of live fields across the group
        assert pend[0]["max_bid"] == 22.0  # MAX across the group
        assert conn.execute(
            "SELECT COUNT(*) FROM bids WHERE item_id='itemE' AND status='REMOVED'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_pending_unique_index_enforced_after_migration(tmp_path):
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (10, 'itemF', 20.0, 'PENDING')",
    ])
    conn = init_db(db_path)
    try:
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(bids)")}
        assert "idx_bids_pending_item_id" in idx
        # Second live snipe for the same item is rejected at the DB.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('itemF', 30.0, 'PENDING')")
        conn.rollback()
        # A PENDING for a new item is fine; a non-PENDING for the same item is fine.
        conn.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('itemG', 30.0, 'PENDING')")
        conn.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('itemF', 30.0, 'ENDED')")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM bids WHERE item_id='itemF'").fetchone()[0] == 2
    finally:
        conn.close()


def test_dedup_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (10, 'itemH', 20.0, 'PENDING')",
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (11, 'itemH', 20.0, 'PENDING')",
    ])
    conn = init_db(db_path)
    conn.close()
    conn2 = init_db(db_path)  # re-run must be a clean no-op
    try:
        assert conn2.execute(
            "SELECT COUNT(*) FROM bids WHERE item_id='itemH' AND status='PENDING'"
        ).fetchone()[0] == 1
        idx = {r["name"] for r in conn2.execute("PRAGMA index_list(bids)")}
        assert "idx_bids_pending_item_id" in idx
    finally:
        conn2.close()


def test_dedup_creates_index_when_no_duplicates(tmp_path):
    """Crash re-entrancy proxy: a DB with no PENDING dups (collapse already done,
    index not yet created) still gets the index on the next init_db."""
    db_path = tmp_path / "dup.db"
    _seed_dup_db(db_path, [
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (10, 'itemI', 20.0, 'PENDING')",
        "INSERT INTO bids (id, item_id, max_bid, status) VALUES (11, 'itemJ', 20.0, 'WON')",
    ])
    conn = init_db(db_path)
    try:
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(bids)")}
        assert "idx_bids_pending_item_id" in idx
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BUI-79: bid_fmvs FK survives / is repaired across the bids rename
# ---------------------------------------------------------------------------

# Current bids schema (REMOVED already in the CHECK) so the broken state can be
# reproduced faithfully without tripping the status-rename rebuild.
_CURRENT_BIDS_SQL = """
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
    );
    CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);
"""

# Overlay-owned tables (gixen-overlay/db.py) sharing the DB. bid_fmvs.bid_id
# REFERENCES bids(id) is the FK BUI-79 is about.
_OVERLAY_TABLES_SQL = """
    CREATE TABLE comics (
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL, year INTEGER
    );
    CREATE TABLE fmv (
        id INTEGER PRIMARY KEY,
        comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
        grade REAL NOT NULL
    );
    CREATE TABLE bid_fmvs (
        bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
        fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
        is_primary  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bid_id, fmv_id)
    );
    CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id);
"""


def _seed_overlay_rows(raw):
    raw.execute("INSERT INTO bids (id, item_id, max_bid, status) VALUES (1, 'b1', 50.0, 'PENDING')")
    raw.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'Hulk', '181', 1974)")
    raw.execute("INSERT INTO fmv (id, comic_id, grade) VALUES (1, 1, 6.0)")
    raw.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (1, 1, 1)")


def _seed_broken_bid_fmvs_db(path):
    """A DB already broken by a pre-fix bids rename: bid_fmvs.bid_id REFERENCES a
    dropped temp table (the exact BUI-49 -> BUI-79 breakage)."""
    raw = sqlite3.connect(str(path))
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(_CURRENT_BIDS_SQL)     # REMOVED present -> no rebuild fires
    raw.executescript(_OVERLAY_TABLES_SQL)
    _seed_overlay_rows(raw)
    raw.commit()
    # SQLite 3.26+ rewrites bid_fmvs.bid_id -> REFERENCES bids_status_rename_old
    # on the rename; dropping that temp table leaves the FK dangling.
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("ALTER TABLE bids RENAME TO bids_status_rename_old")
    raw.executescript(_CURRENT_BIDS_SQL)
    raw.execute("INSERT INTO bids SELECT * FROM bids_status_rename_old")
    raw.execute("DROP TABLE bids_status_rename_old")
    raw.commit()
    targets = {fk["table"] for fk in raw.execute("PRAGMA foreign_key_list(bid_fmvs)")}
    assert "bids_status_rename_old" in targets  # sanity: the FK really is broken
    raw.close()


def _bid_fmvs_fk_targets(conn):
    return {fk["table"] for fk in conn.execute("PRAGMA foreign_key_list(bid_fmvs)")}


def _assert_can_link_fmv(conn):
    """The acceptance check: INSERT INTO bid_fmvs must succeed under enforced FKs
    (the bug raised 'no such table: bids_status_rename_old' here)."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO bids (id, item_id, max_bid, status) VALUES (2, 'b2', 9.0, 'PENDING')")
    conn.execute("INSERT INTO fmv (id, comic_id, grade) VALUES (2, 1, 8.0)")
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (2, 2, 1)")
    conn.commit()


def test_status_rename_preserves_bid_fmvs_fk(tmp_path):
    """Preventive (BUI-79): the PURGED->REMOVED rebuild leaves bid_fmvs.bid_id
    referencing bids(id), not the dropped temp table."""
    db_path = tmp_path / "overlay_old.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(_OLD_SCHEMA_SQL)       # pre-BUI-49 bids: CHECK lacks REMOVED
    raw.executescript(_OVERLAY_TABLES_SQL)
    _seed_overlay_rows(raw)
    raw.commit()
    raw.close()

    conn = init_db(db_path)
    try:
        assert "bids" in _bid_fmvs_fk_targets(conn)
        assert "bids_status_rename_old" not in _bid_fmvs_fk_targets(conn)
        assert conn.execute("SELECT COUNT(*) FROM bid_fmvs").fetchone()[0] == 1
        _assert_can_link_fmv(conn)
    finally:
        conn.close()


def test_repair_heals_dangling_bid_fmvs_fk(tmp_path):
    """Repair (BUI-79): an already-broken DB is healed on the next init_db, with
    bid_fmvs rows preserved."""
    db_path = tmp_path / "broken.db"
    _seed_broken_bid_fmvs_db(db_path)

    conn = init_db(db_path)
    try:
        assert "bids" in _bid_fmvs_fk_targets(conn)
        assert "bids_status_rename_old" not in _bid_fmvs_fk_targets(conn)
        assert conn.execute("SELECT COUNT(*) FROM bid_fmvs").fetchone()[0] == 1
        _assert_can_link_fmv(conn)
    finally:
        conn.close()


def test_repair_is_idempotent(tmp_path):
    """Re-running init_db on an already-healed DB leaves bid_fmvs untouched."""
    db_path = tmp_path / "broken.db"
    _seed_broken_bid_fmvs_db(db_path)
    init_db(db_path).close()

    conn2 = init_db(db_path)
    try:
        assert "bids" in _bid_fmvs_fk_targets(conn2)
        assert conn2.execute("SELECT COUNT(*) FROM bid_fmvs").fetchone()[0] == 1
    finally:
        conn2.close()


def test_repair_noop_without_overlay(tmp_path):
    """gixen-cli standalone (no bid_fmvs table): the repair is a clean no-op and
    the status rename still runs."""
    db_path = tmp_path / "standalone.db"
    _seed_old_db(db_path)  # pre-BUI-49 bids, no overlay tables

    conn = init_db(db_path)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bid_fmvs'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT status FROM bids WHERE item_id='purged001'"
        ).fetchone()["status"] == "REMOVED"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BUI-116: dbidid cache column
# ---------------------------------------------------------------------------

def test_dbidid_column_present_on_fresh_db(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(bids)")]
    assert "dbidid" in cols


def test_init_db_idempotent_with_dbidid(tmp_path):
    """Running init_db twice doesn't error on the dbidid migration (duplicate
    column is tolerated) and the column persists."""
    path = tmp_path / "twice.db"
    init_db(path).close()
    conn = init_db(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bids)")]
    assert "dbidid" in cols


def test_cache_gixen_data_writes_dbidid(db):
    from server.db import cache_gixen_data
    insert_bid(db, "700000010", 50.0, 6, 0, "seller")
    cache_gixen_data(db, "700000010", "Title", None, "10.00 USD", dbidid="abc123")
    db.commit()
    row = get_bid_by_item_id(db, "700000010")
    assert row["dbidid"] == "abc123"


def test_cache_gixen_data_writes_dbidid_even_with_no_other_data(db):
    """A SCHEDULED snipe with no title/seller/current_bid still gets its dbidid
    cached — the has_data early-return must not skip the dbidid write."""
    from server.db import cache_gixen_data
    insert_bid(db, "700000011", 50.0, 6, 0, "seller")
    cache_gixen_data(db, "700000011", None, None, None, dbidid="xyz789")
    db.commit()
    row = get_bid_by_item_id(db, "700000011")
    assert row["dbidid"] == "xyz789"


def test_cache_gixen_data_skips_dbidid_on_removed_row(db):
    from server.db import cache_gixen_data, delete_bid
    insert_bid(db, "700000012", 50.0, 6, 0, "seller")
    delete_bid(db, "700000012")  # -> REMOVED tombstone
    cache_gixen_data(db, "700000012", None, None, None, dbidid="should-not-write")
    db.commit()
    row = get_bid_by_item_id(db, "700000012")
    assert row["dbidid"] is None


# ---------------------------------------------------------------------------
# BUI-381: durable group-win evidence ledger (group_wins)
# ---------------------------------------------------------------------------

def test_group_wins_table_created(db):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "group_wins" in tables


def test_update_bid_status_won_records_group_win(db):
    """A WON classification on a grouped row with a captured auction end
    lands in the group_wins ledger with that genuine end."""
    insert_bid(db, "881000001", 50.0, 6, 3, "s")
    db.execute(
        "UPDATE bids SET auction_end_at='2026-06-30T23:55:00+00:00' "
        "WHERE item_id='881000001'"
    )
    update_bid_status(db, "881000001", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT * FROM group_wins WHERE item_id='881000001'"
    ).fetchone()
    assert row is not None
    assert row["snipe_group"] == 3
    assert row["won_end_at"] == "2026-06-30T23:55:00+00:00"


def test_update_bid_status_won_without_end_records_nothing(db):
    """A WON whose auction end was never captured records NO ledger entry —
    the permanent ledger never stores the COALESCE observation-time proxy
    (it could falsely group-cancel a sibling added after the real win). The
    live WON row keeps serving its shipped BUI-371 proxy evidence instead,
    until purged."""
    insert_bid(db, "881000011", 50.0, 6, 3, "s")
    update_bid_status(db, "881000011", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    # The row itself got the proxy end via COALESCE (shipped behavior)...
    assert get_bid_by_item_id(db, "881000011")["auction_end_at"] == \
        "2026-07-01T00:00:00+00:00"
    # ...but the ledger stays clean.
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000011'"
    ).fetchone() is None


def test_update_bid_status_won_group_zero_records_nothing(db):
    """Group 0 means 'no group' — never evidence."""
    insert_bid(db, "881000002", 50.0, 6, 0, "s")
    update_bid_status(db, "881000002", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000002'"
    ).fetchone() is None


def test_update_bid_status_non_won_records_nothing(db):
    insert_bid(db, "881000003", 50.0, 6, 3, "s")
    update_bid_status(db, "881000003", "LOST", 60.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000003'"
    ).fetchone() is None


def test_group_win_evidence_survives_purge_sweep(db):
    """The BUI-381 case-1 core: mark_bids_purged destroys the WON row (status
    REMOVED), but the ledger entry recorded at classification time survives."""
    insert_bid(db, "881000004", 50.0, 6, 5, "s")
    db.execute(
        "UPDATE bids SET auction_end_at='2026-06-30T23:55:00+00:00' "
        "WHERE item_id='881000004'"
    )
    update_bid_status(db, "881000004", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    mark_bids_purged(db, ["881000004"])
    assert get_bid_by_item_id(db, "881000004")["status"] == "REMOVED"
    row = db.execute(
        "SELECT * FROM group_wins WHERE item_id='881000004'"
    ).fetchone()
    assert row is not None
    assert row["snipe_group"] == 5


def test_update_bid_status_only_id_scopes_recording(db):
    """With only_id, both the WON write and the ledger recording are scoped
    to the one row — an older resolved row sharing the item_id (the BUI-178
    re-listed shape) contributes nothing."""
    old_id = insert_bid(db, "881000012", 50.0, 6, 4, "s")
    db.execute(
        "UPDATE bids SET status='LOST', auction_end_at='2026-05-01T00:00:00+00:00' "
        "WHERE id=?", (old_id,),
    )
    new_id = insert_bid(db, "881000012", 60.0, 6, 5, "s")
    db.execute(
        "UPDATE bids SET auction_end_at='2026-06-30T23:55:00+00:00' WHERE id=?",
        (new_id,),
    )
    db.commit()
    update_bid_status(
        db, "881000012", "WON", 42.0, "2026-07-01T00:00:00+00:00", only_id=new_id,
    )
    db.commit()
    groups = {
        r["snipe_group"] for r in db.execute(
            "SELECT snipe_group FROM group_wins WHERE item_id='881000012'"
        )
    }
    assert groups == {5}  # only the targeted row's group; old LOST row ignored
    old_status = db.execute(
        "SELECT status FROM bids WHERE id=?", (old_id,)
    ).fetchone()["status"]
    assert old_status == "LOST"


def test_record_group_win_skips_null_end(db):
    """End-less evidence is unsound against the lifetime bound — never
    recorded."""
    from server.db import record_group_win
    record_group_win(db, "881000005", 3, None)
    db.commit()
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000005'"
    ).fetchone() is None


def test_record_group_win_skips_unparseable_end(db):
    """An end the classifier could never parse is useless — never stored."""
    from server.db import record_group_win
    record_group_win(db, "881000013", 3, "not-a-timestamp")
    db.commit()
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000013'"
    ).fetchone() is None


def test_record_group_win_skips_far_future_end(db):
    """A 'win' whose end is beyond the estimation allowance has not ended —
    self-contradictory input (e.g. eBay describing a re-listed same-ID
    auction), never stored. An end just inside the allowance (normal
    end-time estimation error) still records."""
    from datetime import datetime, timedelta, timezone
    from server.db import record_group_win
    far_future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    near_future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    record_group_win(db, "881000014", 3, far_future)
    record_group_win(db, "881000015", 3, near_future)
    db.commit()
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000014'"
    ).fetchone() is None
    assert db.execute(
        "SELECT 1 FROM group_wins WHERE item_id='881000015'"
    ).fetchone() is not None


def test_record_group_win_idempotent(db):
    """Re-recording the SAME win (same group, item, end) is a no-op — the
    common case of a WON row re-classified WON on every sync. Keyed on
    (group, item, won_end_at) since BUI-385, so identical re-records still
    collapse to one row."""
    from server.db import record_group_win
    record_group_win(db, "881000006", 3, "2026-07-01T00:00:00+00:00")
    record_group_win(db, "881000006", 3, "2026-07-01T00:00:00+00:00")
    db.commit()
    rows = db.execute(
        "SELECT * FROM group_wins WHERE item_id='881000006'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["won_end_at"] == "2026-07-01T00:00:00+00:00"


def test_record_group_win_relisted_rewin_records_distinct_end(db):
    """BUI-385: a genuine re-listed re-win of the same eBay id in the same
    group ends at a DISTINCT time and records a SECOND ledger entry — the old
    (group, item) key collapsed it to the first win, a WON-permissive evidence
    miss for recycled group numbers. Both stored ends are genuine auction ends
    (record_group_win's guards reject proxies), so this strengthens evidence
    soundly, never a false-REMOVED double-count."""
    from server.db import record_group_win
    record_group_win(db, "881000018", 3, "2026-06-01T00:00:00+00:00")
    record_group_win(db, "881000018", 3, "2026-07-01T00:00:00+00:00")
    db.commit()
    ends = {
        r["won_end_at"] for r in db.execute(
            "SELECT won_end_at FROM group_wins WHERE item_id='881000018'"
        )
    }
    assert ends == {"2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"}


def test_migration_backfills_group_wins_from_existing_won_rows(tmp_path):
    """WON rows that predate recording-at-classification-time are seeded into
    the ledger on startup; rows with no usable end time are skipped, and so
    are the identifiable proxy shapes (auction_end_at == resolved_at, i.e.
    the COALESCE fill at resolution or the BUI-83 legacy backfill) — the
    permanent ledger stores only genuine auction ends."""
    path = tmp_path / "backfill.db"
    conn = init_db(path)
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, "
        "auction_end_at, resolved_at) "
        "VALUES ('881000007', 25.0, 'WON', 4, '2026-06-01T00:00:00+00:00', "
        "'2026-06-01T00:05:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group) "
        "VALUES ('881000008', 25.0, 'WON', 4)"  # no end, no resolved_at
    )
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, "
        "auction_end_at, resolved_at) "
        "VALUES ('881000016', 25.0, 'WON', 4, '2026-06-02T00:00:00+00:00', "
        "'2026-06-02T00:00:00+00:00')"  # proxy shape: end == resolved_at
    )
    conn.commit()
    conn.close()

    conn = init_db(path)
    try:
        row = conn.execute(
            "SELECT won_end_at FROM group_wins WHERE item_id='881000007'"
        ).fetchone()
        assert row is not None
        assert row["won_end_at"] == "2026-06-01T00:00:00+00:00"
        assert conn.execute(
            "SELECT 1 FROM group_wins WHERE item_id='881000008'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM group_wins WHERE item_id='881000016'"
        ).fetchone() is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BUI-385: group_wins ledger hardening — source provenance + re-win re-keying
# ---------------------------------------------------------------------------

def test_group_wins_source_column_present(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(group_wins)")}
    assert "source" in cols


def test_group_wins_source_migration_idempotent(tmp_path):
    """Re-opening an already-migrated DB doesn't choke on the duplicate
    `source` column add (the _COLUMN_MIGRATIONS 'duplicate column' guard)."""
    path = tmp_path / "idem_source.db"
    init_db(path).close()
    conn2 = init_db(path)
    cols = {r[1] for r in conn2.execute("PRAGMA table_info(group_wins)")}
    conn2.close()
    assert "source" in cols


def test_group_wins_unique_index_rekeyed_to_include_end(db):
    """The unique index is (snipe_group, item_id, won_end_at) — the old 2-col
    index is gone, so a distinct-end re-win is no longer blocked."""
    idx_names = {
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='group_wins'"
        )
    }
    assert "idx_group_wins_group_item_end" in idx_names
    assert "idx_group_wins_group_item" not in idx_names
    cols = [
        r[2] for r in db.execute("PRAGMA index_info(idx_group_wins_group_item_end)")
    ]
    assert cols == ["snipe_group", "item_id", "won_end_at"]


def test_group_wins_index_swap_migrates_and_is_idempotent(tmp_path):
    """A legacy DB carrying the old 2-col unique index is re-keyed on open, and
    re-opening the migrated DB leaves the 3-col index in place (idempotent)."""
    path = tmp_path / "idx_swap.db"
    conn = init_db(path)
    # Simulate a pre-BUI-385 DB: drop the new index, recreate the old 2-col one.
    conn.execute("DROP INDEX IF EXISTS idx_group_wins_group_item_end")
    conn.execute(
        "CREATE UNIQUE INDEX idx_group_wins_group_item "
        "ON group_wins(snipe_group, item_id)"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)  # migration should swap the index
    try:
        idx = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='group_wins'"
            )
        }
        assert "idx_group_wins_group_item_end" in idx
        assert "idx_group_wins_group_item" not in idx
    finally:
        conn.close()

    # Re-open the already-swapped DB — still exactly the 3-col index.
    conn = init_db(path)
    try:
        idx = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='group_wins'"
            )
        }
        assert "idx_group_wins_group_item_end" in idx
        assert "idx_group_wins_group_item" not in idx
    finally:
        conn.close()


def test_update_bid_status_won_records_status_transition_source(db):
    """Writer 1: update_bid_status's WON transition tags source
    'status-transition'."""
    from server.db import GROUP_WIN_SOURCE_STATUS_TRANSITION
    insert_bid(db, "885000001", 50.0, 6, 3, "s")
    db.execute(
        "UPDATE bids SET auction_end_at='2026-06-30T23:55:00+00:00' "
        "WHERE item_id='885000001'"
    )
    update_bid_status(db, "885000001", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT source FROM group_wins WHERE item_id='885000001'"
    ).fetchone()
    assert row["source"] == GROUP_WIN_SOURCE_STATUS_TRANSITION


def test_record_group_win_default_source_is_status_transition(db):
    from server.db import record_group_win, GROUP_WIN_SOURCE_STATUS_TRANSITION
    record_group_win(db, "885000002", 3, "2026-07-01T00:00:00+00:00")
    db.commit()
    row = db.execute(
        "SELECT source FROM group_wins WHERE item_id='885000002'"
    ).fetchone()
    assert row["source"] == GROUP_WIN_SOURCE_STATUS_TRANSITION


def test_record_group_win_listed_win_source_stored(db):
    """Writer 3 stores its provenance verbatim (main.py passes it)."""
    from server.db import (
        record_group_win, GROUP_WIN_SOURCE_LISTED_WIN, GROUP_WIN_SOURCES,
    )
    record_group_win(
        db, "885000003", 3, "2026-07-01T00:00:00+00:00",
        source=GROUP_WIN_SOURCE_LISTED_WIN,
    )
    db.commit()
    row = db.execute(
        "SELECT source FROM group_wins WHERE item_id='885000003'"
    ).fetchone()
    assert row["source"] == GROUP_WIN_SOURCE_LISTED_WIN
    assert row["source"] in GROUP_WIN_SOURCES


def test_migration_backfill_tags_startup_backfill_source(tmp_path):
    """Writer 2: the startup backfill tags the rows it seeds
    'startup-backfill'."""
    from server.db import GROUP_WIN_SOURCE_STARTUP_BACKFILL
    path = tmp_path / "bf_source.db"
    conn = init_db(path)
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, "
        "auction_end_at, resolved_at) "
        "VALUES ('885000004', 25.0, 'WON', 4, '2026-06-01T00:00:00+00:00', "
        "'2026-06-01T00:05:00+00:00')"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)
    try:
        row = conn.execute(
            "SELECT source FROM group_wins WHERE item_id='885000004'"
        ).fetchone()
        assert row is not None
        assert row["source"] == GROUP_WIN_SOURCE_STARTUP_BACKFILL
    finally:
        conn.close()


def test_migration_tags_pre_column_rows_legacy(tmp_path):
    """A ledger row written before the source column existed (NULL source) is
    stamped 'legacy' on the next open — no writer can leave a NULL source."""
    from server.db import GROUP_WIN_SOURCE_LEGACY
    path = tmp_path / "legacy_source.db"
    conn = init_db(path)
    # Simulate a pre-BUI-385 write: a ledger row with no source set. (The
    # column exists post-migration, so force it NULL to mimic the old shape.)
    conn.execute(
        "INSERT INTO group_wins (snipe_group, item_id, won_end_at, recorded_at, "
        "source) VALUES (7, '885000005', '2026-06-01T00:00:00+00:00', "
        "'2026-06-01T00:00:00+00:00', NULL)"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)
    try:
        row = conn.execute(
            "SELECT source FROM group_wins WHERE item_id='885000005'"
        ).fetchone()
        assert row["source"] == GROUP_WIN_SOURCE_LEGACY
        # And no row anywhere is left NULL.
        assert conn.execute(
            "SELECT COUNT(*) FROM group_wins WHERE source IS NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_record_group_win_rejects_unknown_source(db):
    """The closed vocabulary is enforced at the write boundary — a typo'd tag
    raises instead of silently landing in the permanent ledger and surfacing
    over /api/group-wins. Nothing is written."""
    from server.db import record_group_win
    with pytest.raises(ValueError, match="unknown source"):
        record_group_win(
            db, "885000006", 3, "2026-07-01T00:00:00+00:00", source="bogus",
        )
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM group_wins WHERE item_id='885000006'"
    ).fetchone()[0] == 0


def test_update_bid_status_won_resync_is_ledger_idempotent(db):
    """The common re-sync case: a WON row re-classified WON on a later sync
    (same status, same genuine end) must NOT add a second ledger row under the
    (group, item, won_end_at) key — only a genuinely distinct end does. Guards
    against per-sync ledger bloat from the re-key."""
    insert_bid(db, "885000007", 50.0, 6, 3, "s")
    db.execute(
        "UPDATE bids SET auction_end_at='2026-06-30T23:55:00+00:00' "
        "WHERE item_id='885000007'"
    )
    update_bid_status(db, "885000007", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    update_bid_status(db, "885000007", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM group_wins WHERE item_id='885000007'"
    ).fetchone()[0] == 1


def test_migration_backfill_records_distinct_ends_for_rewin(tmp_path):
    """The re-key reaches the startup backfill too: two WON bids rows sharing
    (group, item) with DISTINCT genuine ends seed TWO ledger rows, where the
    old 2-col key collapsed them to one. Both tagged startup-backfill."""
    from server.db import GROUP_WIN_SOURCE_STARTUP_BACKFILL
    path = tmp_path / "bf_rewin.db"
    conn = init_db(path)
    # Two WON rows, same item+group, distinct non-proxy ends (resolved_at NULL).
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, auction_end_at) "
        "VALUES ('885000008', 25.0, 'WON', 4, '2026-06-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, auction_end_at) "
        "VALUES ('885000008', 25.0, 'WON', 4, '2026-07-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)
    try:
        rows = conn.execute(
            "SELECT won_end_at, source FROM group_wins WHERE item_id='885000008'"
        ).fetchall()
        ends = {r["won_end_at"] for r in rows}
        assert ends == {"2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"}
        assert {r["source"] for r in rows} == {GROUP_WIN_SOURCE_STARTUP_BACKFILL}
    finally:
        conn.close()


def test_group_wins_index_swap_over_populated_legacy_table(tmp_path):
    """The 'the 3-col index can never fail to build' guarantee, with ACTUAL
    rows present in a pre-BUI-385 2-col-unique table. Migrating swaps the
    index, preserves all rows, and the new key then admits a distinct-end
    re-win the old key would have collapsed."""
    path = tmp_path / "idx_populated.db"
    conn = init_db(path)
    # Simulate a pre-BUI-385 DB: the old 2-col unique index over seeded rows.
    conn.execute("DROP INDEX IF EXISTS idx_group_wins_group_item_end")
    conn.execute(
        "CREATE UNIQUE INDEX idx_group_wins_group_item "
        "ON group_wins(snipe_group, item_id)"
    )
    conn.execute(
        "INSERT INTO group_wins (snipe_group, item_id, won_end_at, recorded_at) "
        "VALUES (5, '886000001', '2026-06-01T00:00:00+00:00', "
        "'2026-06-01T00:00:00+00:00'), "
        "(5, '886000002', '2026-06-02T00:00:00+00:00', '2026-06-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)  # must not crash building the 3-col index over rows
    try:
        idx = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='group_wins'"
            )
        }
        assert "idx_group_wins_group_item_end" in idx
        assert "idx_group_wins_group_item" not in idx
        # Both legacy rows survived the swap.
        assert conn.execute("SELECT COUNT(*) FROM group_wins").fetchone()[0] == 2
        # And a distinct (past) re-win end of an existing (group, item) now
        # records — the 2-col key would have collapsed it.
        from server.db import record_group_win
        record_group_win(conn, "886000001", 5, "2026-06-15T00:00:00+00:00")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM group_wins WHERE item_id='886000001'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_refresh_snipe_group_updates_pending_only(db):
    from server.db import refresh_snipe_group
    from server.db import refresh_snipe_group
    insert_bid(db, "881000009", 50.0, 6, 0, "s")
    insert_bid(db, "881000010", 50.0, 6, 7, "s")
    update_bid_status(db, "881000010", "WON", 42.0, "2026-07-01T00:00:00+00:00")
    db.commit()
    refresh_snipe_group(db, "881000009", 4)   # PENDING → updated
    refresh_snipe_group(db, "881000010", 9)   # WON → untouched
    db.commit()
    assert get_bid_by_item_id(db, "881000009")["snipe_group"] == 4
    assert get_bid_by_item_id(db, "881000010")["snipe_group"] == 7


def test_refresh_snipe_group_spares_terminal_row_sharing_item_id(db):
    """The literal BUI-178 shape: an old resolved row and a live PENDING row
    share one item_id — the refresh touches only the PENDING row."""
    from server.db import refresh_snipe_group
    old_id = insert_bid(db, "881000017", 50.0, 6, 7, "s")
    db.execute("UPDATE bids SET status='WON' WHERE id=?", (old_id,))
    new_id = insert_bid(db, "881000017", 60.0, 6, 0, "s")
    db.commit()
    refresh_snipe_group(db, "881000017", 4)
    db.commit()
    rows = {
        r["id"]: r["snipe_group"] for r in db.execute(
            "SELECT id, snipe_group FROM bids WHERE item_id='881000017'"
        )
    }
    assert rows[old_id] == 7   # terminal row untouched
    assert rows[new_id] == 4   # live row refreshed


# ---------------------------------------------------------------------------
# BUI-384: group_changed_at — stamp every snipe_group mutation so
# _group_won_before can bound evidence by group MEMBERSHIP, not row lifetime
# ---------------------------------------------------------------------------

def test_group_changed_at_column_present_on_fresh_db(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(bids)")]
    assert "group_changed_at" in cols


def test_bids_group_changed_at_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idem_group_changed.db"
    init_db(db_path).close()
    conn2 = init_db(db_path)
    cols = {row[1] for row in conn2.execute("PRAGMA table_info(bids)")}
    assert "group_changed_at" in cols
    conn2.close()


def test_group_changed_at_survives_table_rebuild(tmp_path):
    """The rebuild shape (_BIDS_TABLE_SQL) carries the column: a legacy DB
    whose CHECK still lacks REMOVED is rebuilt after the column migration
    ran, and the copied rows keep their stamps."""
    path = tmp_path / "rebuild.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Minimal legacy shape: PURGED-only CHECK forces the BUI-49 rebuild.
    conn.execute(
        "CREATE TABLE bids ("
        " id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL,"
        " bid_offset INTEGER DEFAULT 6, snipe_group INTEGER DEFAULT 0,"
        " status TEXT DEFAULT 'PENDING' CHECK(status IN "
        " ('PENDING','WON','LOST','FAILED','ENDED','PURGED')),"
        " winning_bid REAL, seller TEXT, comic_id INTEGER,"
        " auction_end_at TEXT, local_snipe_at TEXT, local_snipe_result TEXT,"
        " notes TEXT, added_at TEXT DEFAULT (datetime('now')), resolved_at TEXT)"
    )
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, snipe_group) VALUES ('884000001', 10.0, 3)"
    )
    conn.commit()
    conn.close()

    conn = init_db(path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bids)")]
        assert "group_changed_at" in cols
        row = get_bid_by_item_id(conn, "884000001")
        assert row["group_changed_at"] is None  # never changed → NULL
    finally:
        conn.close()


def test_update_bid_stamps_group_changed_at_on_change(db):
    insert_bid(db, "884000002", 50.0, 6, 0, "s")
    assert get_bid_by_item_id(db, "884000002")["group_changed_at"] is None
    update_bid(db, "884000002", 55.0, 6, 4)  # 0 → 4: a real group change
    row = get_bid_by_item_id(db, "884000002")
    assert row["snipe_group"] == 4
    assert row["group_changed_at"] is not None


def test_update_bid_does_not_stamp_when_group_unchanged(db):
    """An edit that keeps the group (e.g. a max_bid bump) must not re-stamp —
    that would narrow the evidence window and weaken legitimate group-cancel
    evidence."""
    insert_bid(db, "884000003", 50.0, 6, 4, "s")
    update_bid(db, "884000003", 60.0, 6, 4)  # same group
    row = get_bid_by_item_id(db, "884000003")
    assert row["snipe_group"] == 4
    assert row["group_changed_at"] is None

    # And once stamped, an unchanged-group edit preserves the ORIGINAL stamp.
    update_bid(db, "884000003", 60.0, 6, 7)
    first_stamp = get_bid_by_item_id(db, "884000003")["group_changed_at"]
    assert first_stamp is not None
    update_bid(db, "884000003", 65.0, 6, 7)
    assert get_bid_by_item_id(db, "884000003")["group_changed_at"] == first_stamp


def test_update_bid_stamps_on_ungroup(db):
    """Leaving a group (N → 0) is also a membership change and stamps."""
    insert_bid(db, "884000004", 50.0, 6, 5, "s")
    update_bid(db, "884000004", 50.0, 6, 0)
    row = get_bid_by_item_id(db, "884000004")
    assert row["snipe_group"] == 0
    assert row["group_changed_at"] is not None


# ---------------------------------------------------------------------------
# BUI-392: snipe_group=None passthrough — a max_bid-only edit must not
# silently un-group the snipe or stamp group_changed_at for a membership
# change that never happened. Explicit 0 must still un-group.
# ---------------------------------------------------------------------------

def test_update_bid_none_snipe_group_preserves_existing_group(db):
    """A max_bid-only edit (snipe_group=None) leaves snipe_group untouched —
    the pre-BUI-392 bug coerced this to 0 and silently un-grouped the snipe."""
    insert_bid(db, "884000008", 50.0, 6, 4, "s")
    update_bid(db, "884000008", 60.0, 6, None)
    row = get_bid_by_item_id(db, "884000008")
    assert row["max_bid"] == 60.0       # the field the caller actually meant to change
    assert row["snipe_group"] == 4      # unchanged, not coerced to 0


def test_update_bid_none_snipe_group_does_not_stamp_group_changed_at(db):
    """No membership change occurred, so group_changed_at must stay NULL."""
    insert_bid(db, "884000009", 50.0, 6, 4, "s")
    update_bid(db, "884000009", 60.0, 6, None)
    assert get_bid_by_item_id(db, "884000009")["group_changed_at"] is None


def test_update_bid_none_snipe_group_preserves_existing_stamp(db):
    """If group_changed_at was already stamped by a prior real group change,
    a subsequent max_bid-only (None) edit must not clear or move that stamp."""
    insert_bid(db, "884000010", 50.0, 6, 0, "s")
    update_bid(db, "884000010", 50.0, 6, 4)  # real change: stamps
    first_stamp = get_bid_by_item_id(db, "884000010")["group_changed_at"]
    assert first_stamp is not None

    update_bid(db, "884000010", 70.0, 6, None)  # max_bid-only edit
    row = get_bid_by_item_id(db, "884000010")
    assert row["max_bid"] == 70.0
    assert row["snipe_group"] == 4               # still untouched
    assert row["group_changed_at"] == first_stamp  # stamp preserved verbatim


def test_update_bid_explicit_zero_still_ungroups_after_none_passthrough(db):
    """Explicit 0 is a real request to un-group, distinct from None passthrough
    — the whole point of the None/0 split."""
    insert_bid(db, "884000011", 50.0, 6, 4, "s")
    update_bid(db, "884000011", 55.0, 6, None)  # passthrough: no-op on group
    update_bid(db, "884000011", 55.0, 6, 0)     # explicit un-group
    row = get_bid_by_item_id(db, "884000011")
    assert row["snipe_group"] == 0
    assert row["group_changed_at"] is not None


# ---------------------------------------------------------------------------
# BUI-401: bid_offset=None passthrough — a max_bid-only edit must not silently
# reset a tuned fire-offset back to 6 (the same latent bug snipe_group had
# pre-BUI-392). An explicit offset still writes through. bid_offset and
# snipe_group passthrough are independent — either, both, or neither can be None.
# ---------------------------------------------------------------------------

def test_update_bid_none_bid_offset_preserves_existing_offset(db):
    """A max_bid-only edit (bid_offset=None) leaves bid_offset untouched — the
    pre-BUI-401 code always wrote bid_offset, resetting a tuned 12 back to 6."""
    insert_bid(db, "884001001", 50.0, 12, 0, "s")
    update_bid(db, "884001001", 60.0, None, 0)
    row = get_bid_by_item_id(db, "884001001")
    assert row["max_bid"] == 60.0       # the field the caller meant to change
    assert row["bid_offset"] == 12      # unchanged, not reset to 6


def test_update_bid_explicit_bid_offset_writes_through(db):
    """An explicit offset is a real change and IS written."""
    insert_bid(db, "884001002", 50.0, 12, 0, "s")
    update_bid(db, "884001002", 60.0, 9, 0)
    assert get_bid_by_item_id(db, "884001002")["bid_offset"] == 9


def test_update_bid_both_none_preserves_offset_and_group(db):
    """A true max_bid-only edit (both fields None) changes only max_bid, leaving
    the tuned offset AND the group membership intact."""
    insert_bid(db, "884001003", 50.0, 12, 7, "s")
    update_bid(db, "884001003", 88.0, None, None)
    row = get_bid_by_item_id(db, "884001003")
    assert row["max_bid"] == 88.0
    assert row["bid_offset"] == 12
    assert row["snipe_group"] == 7
    assert row["group_changed_at"] is None  # no membership change recorded


def test_update_bid_none_bid_offset_noop_on_non_pending(db):
    """The bid_offset passthrough branch must honor the same WHERE
    status='PENDING' guard as every other update_bid path."""
    insert_bid(db, "884001004", 50.0, 12, 0, "s")
    update_bid_status(db, "884001004", "WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    update_bid(db, "884001004", max_bid=999.0, bid_offset=None, snipe_group=None)
    row = get_bid_by_item_id(db, "884001004")
    assert row["max_bid"] == 50.0       # unchanged — guarded on status='PENDING'
    assert row["bid_offset"] == 12


def test_refresh_snipe_group_stamps_group_changed_at(db):
    from server.db import refresh_snipe_group
    insert_bid(db, "884000005", 50.0, 6, 0, "s")
    refresh_snipe_group(db, "884000005", 4, changed_at="2026-07-01T00:00:00+00:00")
    db.commit()
    row = get_bid_by_item_id(db, "884000005")
    assert row["snipe_group"] == 4
    assert row["group_changed_at"] == "2026-07-01T00:00:00+00:00"


def test_refresh_snipe_group_no_stamp_when_unchanged(db):
    """Mirroring the same group every sync (the common case) must not
    re-stamp — the WHERE's `snipe_group != ?` guards the write."""
    from server.db import refresh_snipe_group
    insert_bid(db, "884000006", 50.0, 6, 4, "s")
    refresh_snipe_group(db, "884000006", 4, changed_at="2026-07-01T00:00:00+00:00")
    db.commit()
    assert get_bid_by_item_id(db, "884000006")["group_changed_at"] is None


def test_refresh_snipe_group_defaults_changed_at_to_now(db):
    from server.db import refresh_snipe_group
    insert_bid(db, "884000007", 50.0, 6, 0, "s")
    refresh_snipe_group(db, "884000007", 2)
    db.commit()
    stamp = get_bid_by_item_id(db, "884000007")["group_changed_at"]
    assert stamp is not None
