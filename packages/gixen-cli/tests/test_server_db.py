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
    insert_bid(db, "200000001", 50.0, 6, 0, "s")
    insert_bid(db, "200000002", 60.0, 6, 0, "s")
    mark_bids_purged(db, ["200000001", "200000002"])
    row1 = get_bid_by_item_id(db, "200000001")
    row2 = get_bid_by_item_id(db, "200000002")
    assert row1["status"] == "REMOVED"
    assert row2["status"] == "REMOVED"
    assert row1["resolved_at"] is not None


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
