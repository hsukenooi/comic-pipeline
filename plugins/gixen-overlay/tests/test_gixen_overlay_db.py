"""Tests for gixen_overlay.db — all use in-memory SQLite, no disk side effects."""
from __future__ import annotations

import sqlite3
import pytest

from gixen_overlay.db import (
    create_tables,
    upsert_comic,
    link_comic_to_bid,
    get_comics_for_bid,
    get_primary_comic_for_bid,
    list_comics,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Minimal bids table required by bid_comics FK and link_comic_to_bid
    conn.execute("""
        CREATE TABLE bids (
            id       INTEGER PRIMARY KEY,
            item_id  TEXT NOT NULL,
            comic_id INTEGER,
            max_bid  REAL NOT NULL
        )
    """)
    conn.commit()
    create_tables(conn)
    yield conn
    conn.close()


def _insert_bid(conn, item_id="100000001", max_bid=50.0):
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid) VALUES (?, ?)", (item_id, max_bid)
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# create_tables
# ---------------------------------------------------------------------------


def test_create_tables_creates_comics_and_bid_comics():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, comic_id INTEGER, max_bid REAL)")
    conn.commit()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics" in tables
    assert "bid_comics" in tables
    conn.close()


def test_create_tables_is_idempotent(db):
    # Second call must not raise
    create_tables(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics" in tables
    assert "bid_comics" in tables


# ---------------------------------------------------------------------------
# upsert_comic
# ---------------------------------------------------------------------------


def test_upsert_comic_inserts_new_record(db):
    cid = upsert_comic(db, "Amazing Spider-Man", "300", 1988, 9.2,
                       800.0, 1000.0, 12, "high", "Key issue")
    assert isinstance(cid, int) and cid > 0
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["issue"] == "300"
    assert row["year"] == 1988
    assert row["grade"] == 9.2
    assert row["fmv_confidence"] == "high"


def test_upsert_comic_updates_on_duplicate(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, 8.0, 500.0, 700.0, 5, "medium", "")
    id2 = upsert_comic(db, "X-Men", "1", 1963, 8.0, 550.0, 750.0, 8, "high", "Updated")
    assert id1 == id2
    row = db.execute("SELECT fmv_low FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["fmv_low"] == 550.0


def test_upsert_comic_grade_none_is_unique_key(db):
    id1 = upsert_comic(db, "Hulk", "181", 1974, None, 50.0, 70.0, 5, "high", "")
    id2 = upsert_comic(db, "Hulk", "181", 1974, None, 60.0, 80.0, 6, "high", "")
    assert id1 == id2


def test_upsert_comic_returns_id(db):
    cid = upsert_comic(db, "Spawn", "1", 1992, 9.8, 100.0, 150.0, 5, "high", "")
    assert isinstance(cid, int)


def test_upsert_comic_locg_ids_preserved_on_conflict(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, 8.0, 500.0, 700.0, 5, "medium", "",
                       locg_id=12345, locg_variant_id=67890)
    id2 = upsert_comic(db, "X-Men", "1", 1963, 8.0, 550.0, 750.0, 8, "high", "Updated")
    assert id1 == id2
    row = db.execute("SELECT locg_id, locg_variant_id FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345
    assert row["locg_variant_id"] == 67890


# ---------------------------------------------------------------------------
# link_comic_to_bid
# ---------------------------------------------------------------------------


def test_link_comic_to_bid_basic(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Daredevil", "1", 1993, None, None, None, None, None, None)
    link_comic_to_bid(db, bid_id, cid)
    rows = db.execute("SELECT * FROM bid_comics WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["comic_id"] == cid
    assert rows[0]["is_primary"] == 0


def test_link_comic_to_bid_primary_mirrors_to_bids(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Batman", "1", 1940, None, None, None, None, None, None)
    link_comic_to_bid(db, bid_id, cid, is_primary=True)
    row = db.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["comic_id"] == cid


def test_link_comic_to_bid_idempotent(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Superman", "1", 1939, None, None, None, None, None, None)
    link_comic_to_bid(db, bid_id, cid)
    link_comic_to_bid(db, bid_id, cid)
    count = db.execute("SELECT COUNT(*) FROM bid_comics WHERE bid_id=?", (bid_id,)).fetchone()[0]
    assert count == 1


def test_link_comic_primary_demotes_prior(db):
    bid_id = _insert_bid(db)
    cid1 = upsert_comic(db, "Test", "1", 1990, None, None, None, None, None, None)
    cid2 = upsert_comic(db, "Test", "2", 1990, None, None, None, None, None, None)
    link_comic_to_bid(db, bid_id, cid1, is_primary=True)
    link_comic_to_bid(db, bid_id, cid2, is_primary=True)
    rows = {r["comic_id"]: r["is_primary"]
            for r in db.execute("SELECT comic_id, is_primary FROM bid_comics WHERE bid_id=?", (bid_id,)).fetchall()}
    assert rows[cid1] == 0
    assert rows[cid2] == 1


# ---------------------------------------------------------------------------
# get_comics_for_bid / get_primary_comic_for_bid
# ---------------------------------------------------------------------------


def test_get_comics_for_bid_primary_first(db):
    bid_id = _insert_bid(db)
    ids = [upsert_comic(db, "Daredevil", str(i), 1993, None, None, None, None, None, None) for i in range(1, 4)]
    link_comic_to_bid(db, bid_id, ids[2])
    link_comic_to_bid(db, bid_id, ids[1])
    link_comic_to_bid(db, bid_id, ids[0], is_primary=True)
    rows = get_comics_for_bid(db, bid_id)
    assert len(rows) == 3
    assert rows[0]["id"] == ids[0]
    assert rows[0]["is_primary"] == 1


def test_get_comics_for_bid_empty(db):
    bid_id = _insert_bid(db)
    assert get_comics_for_bid(db, bid_id) == []


def test_get_primary_comic_for_bid_returns_primary(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Avengers", "1", 1963, None, None, None, None, None, None)
    link_comic_to_bid(db, bid_id, cid, is_primary=True)
    row = get_primary_comic_for_bid(db, bid_id)
    assert row is not None
    assert row["id"] == cid


def test_get_primary_comic_for_bid_none_when_no_primary(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Avengers", "2", 1963, None, None, None, None, None, None)
    link_comic_to_bid(db, bid_id, cid)  # not primary
    assert get_primary_comic_for_bid(db, bid_id) is None


# ---------------------------------------------------------------------------
# list_comics
# ---------------------------------------------------------------------------


def test_list_comics_returns_all(db):
    upsert_comic(db, "X-Men", "1", 1963, None, None, None, None, None, None)
    upsert_comic(db, "Hulk", "181", 1974, None, None, None, None, None, None)
    rows = list_comics(db)
    assert len(rows) == 2


def test_list_comics_filters_by_title(db):
    upsert_comic(db, "X-Men", "1", 1963, None, None, None, None, None, None)
    upsert_comic(db, "Hulk", "181", 1974, None, None, None, None, None, None)
    rows = list_comics(db, title="X-Men")
    assert len(rows) == 1
    assert rows[0]["title"] == "X-Men"


def test_list_comics_empty_returns_empty_list(db):
    assert list_comics(db) == []


# ---------------------------------------------------------------------------
# Integration: create_tables verified via sqlite_master
# ---------------------------------------------------------------------------


def test_register_db_tables_creates_tables_via_sqlite_master():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, comic_id INTEGER, max_bid REAL)")
    conn.commit()
    create_tables(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics" in tables
    assert "bid_comics" in tables
    conn.close()
