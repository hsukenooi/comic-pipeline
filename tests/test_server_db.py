"""Unit tests for server/db.py — all use tmp_path, no disk side effects."""
import sqlite3
import pytest
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import (
    init_db, upsert_comic, insert_bid, get_bid_by_item_id,
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
    assert "comics" in tables
    assert "bids" in tables


def test_wal_mode_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_upsert_comic_inserts(db):
    comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="300",
                            year=1988, grade=9.2,
                            fmv_low=800.0, fmv_high=1000.0,
                            fmv_comps=12, fmv_confidence="high",
                            fmv_notes="Key issue")
    assert isinstance(comic_id, int)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["grade"] == 9.2
    assert row["fmv_confidence"] == "high"


def test_upsert_comic_updates_on_conflict(db):
    id1 = upsert_comic(db, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=500.0, fmv_high=700.0,
                       fmv_comps=5, fmv_confidence="medium", fmv_notes="")
    id2 = upsert_comic(db, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=550.0, fmv_high=750.0,
                       fmv_comps=8, fmv_confidence="high", fmv_notes="Updated")
    assert id1 == id2
    row = db.execute("SELECT fmv_low FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["fmv_low"] == 550.0


def test_insert_bid(db):
    bid_id = insert_bid(db, item_id="123456789", max_bid=800.0,
                        comic_id=None, bid_offset=6, snipe_group=0,
                        seller="seller1")
    assert isinstance(bid_id, int)
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["item_id"] == "123456789"
    assert row["status"] == "PENDING"
    assert row["max_bid"] == 800.0


def test_insert_bid_links_comic(db):
    comic_id = upsert_comic(db, "Hulk", "181", 1974, 9.0,
                            50.0, 70.0, 10, "high", "")
    bid_id = insert_bid(db, "987654321", 60.0, comic_id, 6, 0, "seller2")
    row = db.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["comic_id"] == comic_id


def test_get_bid_by_item_id(db):
    insert_bid(db, "111222333", 50.0, None, 6, 0, "s")
    row = get_bid_by_item_id(db, "111222333")
    assert row is not None
    assert row["item_id"] == "111222333"


def test_get_bid_by_item_id_missing(db):
    assert get_bid_by_item_id(db, "999999999") is None


def test_update_bid(db):
    insert_bid(db, "444555666", 50.0, None, 6, 0, "s")
    update_bid(db, "444555666", max_bid=60.0, bid_offset=10, snipe_group=1)
    row = get_bid_by_item_id(db, "444555666")
    assert row["max_bid"] == 60.0
    assert row["snipe_group"] == 1


def test_update_bid_status(db):
    insert_bid(db, "777888999", 100.0, None, 6, 0, "s")
    update_bid_status(db, "777888999", status="WON",
                      winning_bid=85.0, resolved_at="2026-04-25T12:00:00")
    row = get_bid_by_item_id(db, "777888999")
    assert row["status"] == "WON"
    assert row["winning_bid"] == 85.0
    assert row["resolved_at"] == "2026-04-25T12:00:00"


def test_delete_bid_marks_purged(db):
    insert_bid(db, "555444333", 30.0, None, 6, 0, "s")
    delete_bid(db, "555444333")
    row = get_bid_by_item_id(db, "555444333")
    assert row["status"] == "PURGED"


def test_delete_bid_marks_won_bid_purged(db):
    insert_bid(db, "666777888", 50.0, None, 6, 0, "s")
    update_bid_status(db, "666777888", status="WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    delete_bid(db, "666777888")
    row = get_bid_by_item_id(db, "666777888")
    assert row["status"] == "PURGED"


def test_get_all_bids_returns_list(db):
    insert_bid(db, "100000001", 10.0, None, 6, 0, "s")
    insert_bid(db, "100000002", 20.0, None, 6, 0, "s")
    rows = get_all_bids(db)
    item_ids = [r["item_id"] for r in rows]
    assert "100000001" in item_ids
    assert "100000002" in item_ids


def test_mark_bids_purged_sets_status(db):
    insert_bid(db, "200000001", 50.0, None, 6, 0, "s")
    insert_bid(db, "200000002", 60.0, None, 6, 0, "s")
    mark_bids_purged(db, ["200000001", "200000002"])
    row1 = get_bid_by_item_id(db, "200000001")
    row2 = get_bid_by_item_id(db, "200000002")
    assert row1["status"] == "PURGED"
    assert row2["status"] == "PURGED"
    assert row1["resolved_at"] is not None


def test_mark_bids_purged_transitions_won_bid(db):
    insert_bid(db, "200000003", 50.0, None, 6, 0, "s")
    update_bid_status(db, "200000003", "WON", winning_bid=42.0, resolved_at="2026-04-25T10:00:00")
    mark_bids_purged(db, ["200000003"])
    row = get_bid_by_item_id(db, "200000003")
    assert row["status"] == "PURGED"
    assert row["winning_bid"] == 42.0


def test_mark_bids_purged_empty_list_is_noop(db):
    insert_bid(db, "200000004", 50.0, None, 6, 0, "s")
    mark_bids_purged(db, [])
    row = get_bid_by_item_id(db, "200000004")
    assert row["status"] == "PENDING"


def test_update_bid_noop_on_non_pending(db):
    insert_bid(db, "300000001", 50.0, None, 6, 0, "s")
    update_bid_status(db, "300000001", "WON", winning_bid=40.0, resolved_at="2026-04-25T10:00:00")
    update_bid(db, "300000001", max_bid=999.0, bid_offset=6, snipe_group=0)
    row = get_bid_by_item_id(db, "300000001")
    assert row["max_bid"] == 50.0  # unchanged — update_bid guards on status='PENDING'


def test_upsert_comic_persists_locg_ids(db):
    """locg_id and locg_variant_id round-trip through upsert_comic."""
    comic_id = upsert_comic(
        db, title="Amazing Spider-Man", issue="300", year=1988, grade=9.2,
        fmv_low=800.0, fmv_high=1000.0,
        fmv_comps=12, fmv_confidence="high", fmv_notes="",
        locg_id=6977652, locg_variant_id=6977652,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] == 6977652
    assert row["locg_variant_id"] == 6977652


def test_upsert_comic_locg_ids_default_to_null(db):
    """Backwards compat: existing call sites without locg_id keep working."""
    comic_id = upsert_comic(
        db, title="Hulk", issue="181", year=1974, grade=9.0,
        fmv_low=50.0, fmv_high=70.0,
        fmv_comps=10, fmv_confidence="high", fmv_notes="",
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["locg_id"] is None
    assert row["locg_variant_id"] is None


def test_upsert_comic_locg_ids_preserved_on_conflict(db):
    """A second upsert without locg_id must not clobber the existing values."""
    id1 = upsert_comic(
        db, title="X-Men", issue="1", year=1963, grade=8.0,
        fmv_low=500.0, fmv_high=700.0,
        fmv_comps=5, fmv_confidence="medium", fmv_notes="",
        locg_id=12345, locg_variant_id=67890,
    )
    id2 = upsert_comic(
        db, title="X-Men", issue="1", year=1963, grade=8.0,
        fmv_low=550.0, fmv_high=750.0,
        fmv_comps=8, fmv_confidence="high", fmv_notes="Updated",
        # No locg_id passed — should preserve prior values
    )
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345
    assert row["locg_variant_id"] == 67890


def test_upsert_comic_locg_ids_updated_when_provided(db):
    """A second upsert with new locg_id values should overwrite the stored ones."""
    id1 = upsert_comic(
        db, title="Spawn", issue="1", year=1992, grade=9.8,
        fmv_low=100.0, fmv_high=150.0,
        fmv_comps=5, fmv_confidence="high", fmv_notes="",
        locg_id=100, locg_variant_id=None,
    )
    id2 = upsert_comic(
        db, title="Spawn", issue="1", year=1992, grade=9.8,
        fmv_low=110.0, fmv_high=160.0,
        fmv_comps=6, fmv_confidence="high", fmv_notes="",
        locg_id=200, locg_variant_id=300,
    )
    assert id1 == id2
    row = db.execute("SELECT * FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 200
    assert row["locg_variant_id"] == 300
