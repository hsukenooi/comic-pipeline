"""Tests for PER-98 — nullable year + upsert_comic reconciliation."""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import create_tables, upsert_comic, upsert_fmv


def _fresh_db() -> sqlite3.Connection:
    """In-memory DB with the bids stub the plugin's FK chain depends on."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids ("
        "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL, "
        "fmv_id INTEGER REFERENCES fmv(id) ON DELETE SET NULL)"
    )
    create_tables(conn)
    return conn


def _legacy_db_with_year_not_null() -> sqlite3.Connection:
    """Simulate the pre-PER-98 schema so the migration has work to do."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids ("
        "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL, "
        "fmv_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE comics ("
        "id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL, "
        "year INTEGER NOT NULL, locg_id INTEGER, locg_variant_id INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')), UNIQUE(title, issue, year))"
    )
    conn.execute(
        "CREATE TABLE fmv ("
        "id INTEGER PRIMARY KEY, "
        "comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE, "
        "grade REAL NOT NULL, low REAL, high REAL, comps INTEGER, "
        "confidence TEXT, notes TEXT, updated_at TEXT, "
        "UNIQUE(comic_id, grade))"
    )
    conn.execute(
        "CREATE TABLE bid_fmvs ("
        "bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE, "
        "fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE, "
        "is_primary INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (bid_id, fmv_id))"
    )
    return conn


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_relaxes_year_to_nullable():
    conn = _legacy_db_with_year_not_null()
    # Seed legacy data so the migration has rows to preserve.
    conn.execute("INSERT INTO bids (item_id, max_bid) VALUES ('111', 10.0)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    conn.execute("INSERT INTO fmv (comic_id, grade, low, high) VALUES (1, 9.0, 100, 120)")
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()

    create_tables(conn)

    year_col = next(r for r in conn.execute("PRAGMA table_info(comics)") if r[1] == "year")
    assert year_col[3] == 0, "year should be nullable after migration"

    # Data preserved
    comic = conn.execute("SELECT * FROM comics WHERE id=1").fetchone()
    assert comic["title"] == "X" and comic["year"] == 1963
    fmv = conn.execute("SELECT * FROM fmv WHERE id=1").fetchone()
    assert fmv["low"] == 100
    junction = conn.execute("SELECT * FROM bid_fmvs WHERE bid_id=1").fetchone()
    assert junction is not None


def test_migration_is_idempotent():
    """Running create_tables again on a migrated DB must be a no-op."""
    conn = _legacy_db_with_year_not_null()
    conn.execute("INSERT INTO bids (item_id, max_bid) VALUES ('a', 1.0)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    conn.commit()
    create_tables(conn)

    rows_before = conn.execute("SELECT * FROM comics ORDER BY id").fetchall()
    create_tables(conn)
    rows_after = conn.execute("SELECT * FROM comics ORDER BY id").fetchall()
    assert [tuple(r) for r in rows_before] == [tuple(r) for r in rows_after]


def test_partial_unique_indexes_prevent_dupes():
    conn = _fresh_db()
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    conn.rollback()

    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('Y', '1', NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO comics (title, issue, year) VALUES ('Y', '1', NULL)")


def test_partial_indexes_allow_yeared_and_yearless_same_title_issue():
    """The partial-index design allows (T,I,Y) and (T,I,NULL) to coexist at
    the DB layer — application code (upsert_comic) is responsible for not
    creating that pair, but the indexes don't fight it."""
    conn = _fresh_db()
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")


# ---------------------------------------------------------------------------
# upsert_comic reconciliation
# ---------------------------------------------------------------------------


def test_upsert_yearless_creates_null_row():
    conn = _fresh_db()
    cid = upsert_comic(conn, title="X", issue="1")
    row = conn.execute("SELECT year FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["year"] is None


def test_upsert_yeared_twice_reuses_row():
    conn = _fresh_db()
    a = upsert_comic(conn, title="X", issue="1", year=1963)
    b = upsert_comic(conn, title="X", issue="1", year=1963)
    assert a == b


def test_upsert_yearless_then_yeared_promotes_in_place():
    """Yearless placeholder gets its year filled in — no second row created."""
    conn = _fresh_db()
    a = upsert_comic(conn, title="X", issue="1")
    b = upsert_comic(conn, title="X", issue="1", year=1963)
    assert a == b
    row = conn.execute("SELECT year FROM comics WHERE id=?", (b,)).fetchone()
    assert row["year"] == 1963
    # Only one row should exist
    count = conn.execute("SELECT count(*) FROM comics WHERE title='X' AND issue='1'").fetchone()[0]
    assert count == 1


def test_upsert_yeared_then_yearless_reuses_canonical():
    """Yearless insert with a canonical yeared row already present must
    return the canonical row, not a yearless duplicate."""
    conn = _fresh_db()
    a = upsert_comic(conn, title="X", issue="1", year=1963)
    b = upsert_comic(conn, title="X", issue="1")
    assert a == b
    count = conn.execute("SELECT count(*) FROM comics WHERE title='X' AND issue='1'").fetchone()[0]
    assert count == 1


def test_upsert_locg_metadata_merged_via_coalesce():
    """locg_id and locg_variant_id fill in via COALESCE on subsequent upserts."""
    conn = _fresh_db()
    a = upsert_comic(conn, title="X", issue="1", year=1963)
    upsert_comic(conn, title="X", issue="1", year=1963, locg_id=999, locg_variant_id=888)
    row = conn.execute("SELECT locg_id, locg_variant_id FROM comics WHERE id=?", (a,)).fetchone()
    assert row["locg_id"] == 999 and row["locg_variant_id"] == 888

    # COALESCE preserves existing — passing None doesn't clobber.
    upsert_comic(conn, title="X", issue="1", year=1963, locg_id=None)
    row = conn.execute("SELECT locg_id FROM comics WHERE id=?", (a,)).fetchone()
    assert row["locg_id"] == 999


def test_yearless_insert_with_multiple_yeared_prefers_locg():
    """When historical data has multiple yeared rows for one (title, issue) —
    the PER-98 backfill mistake — prefer the one with locg_id."""
    conn = _fresh_db()
    # Two yeared rows: one with locg_id, one without
    conn.execute("INSERT INTO comics (title, issue, year, locg_id) VALUES ('X', '1', 1963, NULL)")
    conn.execute("INSERT INTO comics (title, issue, year, locg_id) VALUES ('X', '1', 1986, 12345)")
    expected = conn.execute("SELECT id FROM comics WHERE locg_id=12345").fetchone()[0]
    actual = upsert_comic(conn, title="X", issue="1")
    assert actual == expected
