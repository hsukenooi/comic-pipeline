"""Tests for gixen_overlay.db — all use in-memory SQLite, no disk side effects."""
from __future__ import annotations

import sqlite3
import pytest

from gixen_overlay.db import (
    create_tables,
    upsert_comic,
    upsert_fmv,
    set_bid_fmv,
    get_fmv_for_bid,
    link_fmv_to_bid,
    get_primary_fmv_for_bid,
    list_comics,
    check_reconciliation_conflict,
    ReconciliationConflictError,
)


def _make_db(*, with_fmv_id: bool = True) -> sqlite3.Connection:
    """Create an in-memory DB with the minimal bids stub the plugin expects."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    cols = "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL"
    if with_fmv_id:
        cols += ", fmv_id INTEGER"
    conn.execute(f"CREATE TABLE bids ({cols})")
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


def _insert_bid(conn, item_id="100000001", max_bid=50.0) -> int:
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid) VALUES (?, ?)", (item_id, max_bid)
    )
    conn.commit()
    return cur.lastrowid


def _insert_comic(conn, title="X-Men", issue="1", year=1963) -> int:
    return upsert_comic(conn, title, issue, year)


# ---------------------------------------------------------------------------
# create_tables
# ---------------------------------------------------------------------------


def test_create_tables_creates_fmv_and_bid_fmvs():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fmv" in tables
    assert "bid_fmvs" in tables
    conn.close()


def test_create_tables_does_not_create_bid_comics():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bid_comics" not in tables
    conn.close()


def test_create_tables_is_idempotent(db):
    create_tables(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fmv" in tables
    assert "bid_fmvs" in tables


# ---------------------------------------------------------------------------
# upsert_comic (identity-only)
# ---------------------------------------------------------------------------


def test_upsert_comic_inserts_new_record(db):
    cid = upsert_comic(db, "Amazing Spider-Man", "300", 1988)
    assert isinstance(cid, int) and cid > 0
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["issue"] == "300"
    assert row["year"] == 1988


def test_upsert_comic_returns_same_id_on_conflict(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963)
    id2 = upsert_comic(db, "X-Men", "1", 1963)
    assert id1 == id2


def test_upsert_comic_updates_locg_id_via_coalesce(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, locg_id=12345)
    id2 = upsert_comic(db, "X-Men", "1", 1963, locg_id=None)
    assert id1 == id2
    row = db.execute("SELECT locg_id FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345


def test_upsert_comic_no_grade_or_fmv_columns(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    cols = {row[1] for row in db.execute("PRAGMA table_info(comics)")}
    assert "grade" not in cols
    assert "fmv_low" not in cols


# ---------------------------------------------------------------------------
# upsert_fmv
# ---------------------------------------------------------------------------


def test_upsert_fmv_inserts_and_returns_id(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0, comps=12, confidence="high")
    assert isinstance(fid, int) and fid > 0


def test_upsert_fmv_on_same_comic_grade_updates_nonnull_fields(db):
    cid = _insert_comic(db)
    fid1 = upsert_fmv(db, cid, 9.2, low=800.0)
    fid2 = upsert_fmv(db, cid, 9.2, low=850.0)
    assert fid1 == fid2
    row = db.execute("SELECT low FROM fmv WHERE id=?", (fid1,)).fetchone()
    assert row["low"] == 850.0


def test_upsert_fmv_coalesce_does_not_overwrite_with_none(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)
    upsert_fmv(db, cid, 9.2, low=None)
    row = db.execute("SELECT low, high FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 800.0
    assert row["high"] == 1000.0


def test_upsert_fmv_grade_none_raises(db):
    cid = _insert_comic(db)
    with pytest.raises(ValueError, match="grade is required"):
        upsert_fmv(db, cid, None)


def test_upsert_fmv_grade_only_stub_has_null_updated_at(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2)
    row = db.execute("SELECT updated_at FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["updated_at"] is None


def test_upsert_fmv_subsequent_call_with_low_sets_updated_at(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2)
    upsert_fmv(db, cid, 9.2, low=500.0)
    row = db.execute("SELECT updated_at FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["updated_at"] is not None


# ---------------------------------------------------------------------------
# link_fmv_to_bid / set_bid_fmv / get_fmv_for_bid
# ---------------------------------------------------------------------------


def test_link_fmv_to_bid_non_primary(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=False)
    rows = db.execute("SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == fid
    assert rows[0]["is_primary"] == 0


def test_link_fmv_to_bid_primary_mirrors_to_bids_fmv_id(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_link_fmv_to_bid_primary_demotes_prior(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid1 = upsert_fmv(db, cid, 9.0, low=700.0)
    fid2 = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid1, is_primary=True)
    link_fmv_to_bid(db, bid_id, fid2, is_primary=True)
    rows = {r["fmv_id"]: r["is_primary"]
            for r in db.execute("SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,))}
    assert rows[fid1] == 0
    assert rows[fid2] == 1


def test_link_fmv_to_bid_idempotent(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid)
    link_fmv_to_bid(db, bid_id, fid)
    count = db.execute(
        "SELECT COUNT(*) FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchone()[0]
    assert count == 1


def test_link_fmv_to_bid_nonexistent_fmv_raises_fk(db):
    bid_id = _insert_bid(db)
    with pytest.raises(sqlite3.IntegrityError):
        link_fmv_to_bid(db, bid_id, 9999, is_primary=False)


def test_set_bid_fmv_sets_and_clears(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    set_bid_fmv(db, bid_id, fid)
    assert db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()["fmv_id"] == fid
    set_bid_fmv(db, bid_id, None)
    assert db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()["fmv_id"] is None


def test_get_fmv_for_bid_returns_fmv_row(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    set_bid_fmv(db, bid_id, fid)
    row = get_fmv_for_bid(db, bid_id)
    assert row is not None
    assert row["id"] == fid
    assert row["low"] == 800.0


def test_get_fmv_for_bid_returns_none_when_unlinked(db):
    bid_id = _insert_bid(db)
    assert get_fmv_for_bid(db, bid_id) is None


def test_get_primary_fmv_for_bid_integration(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Daredevil", "1", 1964, locg_id=12345)
    fid = upsert_fmv(db, cid, 9.4, low=600.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = get_primary_fmv_for_bid(db, bid_id)
    assert row is not None
    assert row["grade"] == 9.4
    assert row["low"] == 600.0
    assert row["title"] == "Daredevil"
    assert row["locg_id"] == 12345


# ---------------------------------------------------------------------------
# list_comics (joined read path)
# ---------------------------------------------------------------------------


def test_list_comics_returns_all(db):
    upsert_comic(db, "X-Men", "1", 1963)
    upsert_comic(db, "Hulk", "181", 1974)
    rows = list_comics(db)
    assert len(rows) == 2


def test_list_comics_one_row_per_fmv_grade(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    upsert_fmv(db, cid, 9.0, low=700.0)
    upsert_fmv(db, cid, 9.2, low=800.0)
    rows = list_comics(db)
    assert len(rows) == 2
    grades = {r["grade"] for r in rows}
    assert grades == {9.0, 9.2}


def test_list_comics_comic_without_fmv_returns_one_null_row(db):
    upsert_comic(db, "X-Men", "1", 1963)
    rows = list_comics(db)
    assert len(rows) == 1
    assert rows[0]["grade"] is None
    assert rows[0]["fmv_low"] is None


def test_list_comics_filter_by_grade(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    upsert_fmv(db, cid, 9.0, low=700.0)
    upsert_fmv(db, cid, 9.2, low=800.0)
    rows = list_comics(db, grade=9.2)
    assert len(rows) == 1
    assert rows[0]["fmv_low"] == 800.0


def test_list_comics_filter_by_title(db):
    upsert_comic(db, "X-Men", "1", 1963)
    upsert_comic(db, "Hulk", "181", 1974)
    rows = list_comics(db, title="X-Men")
    assert len(rows) == 1
    assert rows[0]["title"] == "X-Men"


def test_list_comics_empty_db(db):
    assert list_comics(db) == []


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def _make_legacy_db() -> sqlite3.Connection:
    """Build a pre-migration DB with the old comics schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER,
            fmv_id INTEGER,
            max_bid REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER NOT NULL,
            grade           REAL,
            fmv_low         REAL,
            fmv_high        REAL,
            fmv_comps       INTEGER,
            fmv_confidence  TEXT,
            fmv_notes       TEXT,
            fmv_updated_at  TEXT,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(title, issue, year, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_comics (
            bid_id     INTEGER NOT NULL REFERENCES bids(id),
            comic_id   INTEGER NOT NULL REFERENCES comics(id),
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, comic_id)
        )
    """)
    conn.commit()
    return conn


def test_migration_collapses_shadow_rows_to_one_comic():
    conn = _make_legacy_db()
    # Two comics rows: same title/issue/year, different grade (the classic shadow bug)
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (2, 'ASM', '300', 1988, 9.0, 600.0)")
    conn.commit()
    create_tables(conn)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1
    fmv_rows = conn.execute("SELECT * FROM fmv ORDER BY grade").fetchall()
    assert len(fmv_rows) == 2
    grades = {r["grade"] for r in fmv_rows}
    assert grades == {9.0, 9.2}


def test_migration_sets_bids_fmv_id():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    bid = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert bid["fmv_id"] is not None
    fmv = conn.execute("SELECT * FROM fmv WHERE id=?", (bid["fmv_id"],)).fetchone()
    assert fmv["grade"] == 9.2


def test_migration_migrates_bid_comics_to_bid_fmvs():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    bf = conn.execute("SELECT * FROM bid_fmvs WHERE bid_id=1").fetchall()
    assert len(bf) == 1
    assert bf[0]["is_primary"] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bid_comics" not in tables


def test_migration_fmv_bid_fmvs_survive_python_memory_roundtrip():
    """Verifies the Python-memory table rebuild preserves all field values."""
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("""
        INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, fmv_comps, fmv_confidence, fmv_notes)
        VALUES (1, 'ASM', '300', 1988, 9.2, 800.0, 1000.0, 12, 'high', 'Key issue')
    """)
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    fmv = conn.execute("SELECT * FROM fmv").fetchone()
    assert fmv["low"] == 800.0
    assert fmv["high"] == 1000.0
    assert fmv["comps"] == 12
    assert fmv["confidence"] == "high"
    assert fmv["notes"] == "Key issue"


def test_migration_bid_with_null_comic_id_stays_unlinked():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', NULL, 50.0)")
    conn.commit()
    create_tables(conn)
    bid = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert bid["fmv_id"] is None


def test_migration_is_idempotent():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    create_tables(conn)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1
    fmv_rows = conn.execute("SELECT * FROM fmv").fetchall()
    assert len(fmv_rows) == 1


def test_migration_crash_recovery_raises_runtime_error():
    """If comics has no grade column but comics_old exists, raise RuntimeError."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            locg_id INTEGER,
            locg_variant_id INTEGER,
            created_at TEXT,
            UNIQUE(title, issue, year)
        )
    """)
    # Simulate mid-rebuild crash: comics_old still present
    conn.execute("""
        CREATE TABLE comics_old (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            grade REAL
        )
    """)
    conn.commit()
    with pytest.raises(RuntimeError, match="crashed mid-migration state"):
        create_tables(conn)


def test_migration_fresh_db_no_legacy_data_is_noop():
    """Fresh DB (no comics rows, no bid_comics) migration gate returns immediately."""
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics_old" not in tables
    assert "bid_comics" not in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_migration_regression_second_grade_revision_upserts_fmv_not_new_comic():
    """After migration, a grade revision updates the existing fmv row, not inserts a new comic."""
    conn = _make_legacy_db()
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    # Now use new API: upsert same identity, different grade — should create another fmv row
    cid = upsert_comic(conn, "ASM", "300", 1988)
    upsert_fmv(conn, cid, 9.4, low=900.0)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1, "Must not create a second comics row"
    fmv_rows = conn.execute("SELECT * FROM fmv WHERE comic_id=?", (cid,)).fetchall()
    assert len(fmv_rows) == 2


# ---------------------------------------------------------------------------
# Integration: sqlite_master verification
# ---------------------------------------------------------------------------


def test_register_db_tables_creates_tables_via_sqlite_master():
    conn = _make_db()
    create_tables(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics" in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables
    assert "bid_comics" not in tables
    conn.close()


# ---------------------------------------------------------------------------
# PER-98 year-nullable migration (_migrate_year_nullable)
# ---------------------------------------------------------------------------


def _make_post_fmv_split_db_with_notnull_year() -> sqlite3.Connection:
    """Build a DB in the post-fmv-split, pre-year-nullable shape:
    comics has UNIQUE(title, issue, year) and year NOT NULL; fmv + bid_fmvs exist.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
            fmv_id INTEGER, max_bid REAL NOT NULL
        )
    """)
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
        CREATE TABLE fmv (
            id INTEGER PRIMARY KEY,
            comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade REAL NOT NULL, low REAL, high REAL, comps INTEGER,
            confidence TEXT, notes TEXT, updated_at TEXT,
            UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_fmvs (
            bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.commit()
    return conn


def test_year_nullable_migration_relaxes_year_notnull():
    conn = _make_post_fmv_split_db_with_notnull_year()
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'ASM', '300', 1988)")
    conn.commit()
    create_tables(conn)
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(comics)")}
    assert info["year"][3] == 0, "year column should be nullable (notnull=0)"
    rows = conn.execute("SELECT id, title, issue, year FROM comics").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == 1 and rows[0]["year"] == 1988


def test_year_nullable_migration_preserves_fmv_and_bid_fmvs():
    conn = _make_post_fmv_split_db_with_notnull_year()
    conn.execute("INSERT INTO bids (id, item_id, max_bid) VALUES (1, 'item1', 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'ASM', '300', 1988)")
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, notes, updated_at) "
        "VALUES (1, 1, 9.2, 800.0, 1000.0, 12, 'high', 'Key', '2026-05-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    fmv = conn.execute("SELECT * FROM fmv WHERE id=1").fetchone()
    assert fmv["comic_id"] == 1 and fmv["low"] == 800.0 and fmv["high"] == 1000.0
    assert fmv["notes"] == "Key"
    bf = conn.execute("SELECT * FROM bid_fmvs WHERE bid_id=1").fetchone()
    assert bf["fmv_id"] == 1 and bf["is_primary"] == 1


def test_year_nullable_migration_installs_partial_unique_indexes():
    conn = _make_post_fmv_split_db_with_notnull_year()
    create_tables(conn)
    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='comics'"
    )}
    assert "idx_comics_tiy_yes" in indexes
    assert "idx_comics_ti_null" in indexes


def test_year_nullable_migration_partial_indexes_enforce_uniqueness():
    conn = _make_post_fmv_split_db_with_notnull_year()
    create_tables(conn)
    # Two NULL-year rows for same (title, issue) → conflict on idx_comics_ti_null
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', NULL)")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', NULL)")
        conn.commit()
    conn.rollback()
    # Two yeared rows for same (title, issue, year) → conflict on idx_comics_tiy_yes
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '400', 1995)")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '400', 1995)")
        conn.commit()
    conn.rollback()
    # Mixed: (ASM, '500', NULL) and (ASM, '500', 1988) coexist
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '500', NULL)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '500', 1988)")
    conn.commit()
    rows = conn.execute(
        "SELECT year FROM comics WHERE title='ASM' AND issue='500' ORDER BY year"
    ).fetchall()
    assert [r["year"] for r in rows] == [1988, None] or [r["year"] for r in rows] == [None, 1988]


def test_year_nullable_migration_idempotent():
    conn = _make_post_fmv_split_db_with_notnull_year()
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'ASM', '300', 1988)")
    conn.commit()
    create_tables(conn)
    create_tables(conn)
    rows = conn.execute("SELECT id, year FROM comics").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == 1 and rows[0]["year"] == 1988


def test_year_nullable_migration_empty_fmv_succeeds():
    conn = _make_post_fmv_split_db_with_notnull_year()
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'ASM', '300', 1988)")
    conn.commit()
    create_tables(conn)
    fmv_count = conn.execute("SELECT COUNT(*) FROM fmv").fetchone()[0]
    bf_count = conn.execute("SELECT COUNT(*) FROM bid_fmvs").fetchone()[0]
    assert fmv_count == 0 and bf_count == 0
    # Tables still exist
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"comics", "fmv", "bid_fmvs"}.issubset(tables)


def test_year_nullable_migration_crash_recovery_raises():
    """If year is already nullable but comics_old_ynull exists, raise."""
    conn = _make_db()
    create_tables(conn)
    # Simulate mid-rebuild crash
    conn.execute("""
        CREATE TABLE comics_old_ynull (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER NOT NULL, locg_id INTEGER, locg_variant_id INTEGER, created_at TEXT,
            UNIQUE(title, issue, year)
        )
    """)
    conn.commit()
    with pytest.raises(RuntimeError, match="crashed mid-migration state"):
        create_tables(conn)


def test_year_nullable_migration_does_not_trip_on_fmv_split_intermediate():
    """A leftover comics_old (fmv-split's name) must NOT trigger this gate."""
    conn = _make_db()
    create_tables(conn)
    # year is already nullable here. Create a comics_old table — belongs to the
    # other migration, must not raise from _migrate_year_nullable's gate.
    conn.execute("CREATE TABLE comics_old (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    # Should not raise — comics_old_ynull is absent
    from gixen_overlay.db import _migrate_year_nullable
    _migrate_year_nullable(conn)


# ---------------------------------------------------------------------------
# PER-98 upsert_comic reconciliation (year=None, promote, merge, reboot guard)
# ---------------------------------------------------------------------------


def test_upsert_comic_year_none_inserts_null_row(db):
    cid = upsert_comic(db, "ASM", "300", year=None)
    row = db.execute("SELECT year FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["year"] is None


def test_upsert_comic_year_none_idempotent(db):
    cid1 = upsert_comic(db, "ASM", "300", year=None)
    cid2 = upsert_comic(db, "ASM", "300", year=None)
    assert cid1 == cid2
    n = db.execute(
        "SELECT COUNT(*) FROM comics WHERE title='ASM' AND issue='300'"
    ).fetchone()[0]
    assert n == 1


def test_upsert_comic_year_none_short_circuits_to_yeared_row(db):
    """R5: year=None on (title, issue) where a yeared row exists returns the yeared row."""
    yeared_id = upsert_comic(db, "ASM", "300", year=1988)
    null_call_id = upsert_comic(db, "ASM", "300", year=None)
    assert null_call_id == yeared_id
    rows = db.execute(
        "SELECT id, year FROM comics WHERE title='ASM' AND issue='300'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["year"] == 1988


def test_upsert_comic_year_none_merges_locg_on_short_circuit(db):
    yeared_id = upsert_comic(db, "ASM", "300", year=1988)
    upsert_comic(db, "ASM", "300", year=None, locg_id=42, locg_variant_id=7)
    row = db.execute(
        "SELECT year, locg_id, locg_variant_id FROM comics WHERE id=?",
        (yeared_id,),
    ).fetchone()
    assert row["year"] == 1988
    assert row["locg_id"] == 42
    assert row["locg_variant_id"] == 7


def test_upsert_comic_promotes_null_row_in_place(db):
    """R4 promotion: year=None then year=Y returns the same id; row gains year."""
    null_id = upsert_comic(db, "ASM", "300", year=None)
    yeared_id = upsert_comic(db, "ASM", "300", year=1988)
    assert null_id == yeared_id
    rows = db.execute(
        "SELECT id, year FROM comics WHERE title='ASM' AND issue='300'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["year"] == 1988


def test_upsert_comic_promote_preserves_fmv_children(db):
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0)
    promoted_id = upsert_comic(db, "ASM", "300", year=1988)
    assert promoted_id == null_id
    fmv = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=?", (promoted_id,)
    ).fetchone()
    assert fmv["grade"] == 9.2 and fmv["low"] == 800.0


def test_upsert_comic_merge_no_fmv_collision(db):
    """R4 merge with non-overlapping grades: fmv children reparent, NULL row gone."""
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0)
    # Force the yeared row to be a separate id by inserting via raw SQL.
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)"
    )
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    upsert_fmv(db, yeared_id, grade=8.5, low=500.0)
    # Trigger merge.
    returned = upsert_comic(db, "ASM", "300", year=1988)
    assert returned == yeared_id
    rows = db.execute(
        "SELECT id, year FROM comics WHERE title='ASM' AND issue='300'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["id"] == yeared_id
    fmv_rows = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
        (yeared_id,),
    ).fetchall()
    assert [(r["grade"], r["low"]) for r in fmv_rows] == [(8.5, 500.0), (9.2, 800.0)]


def test_upsert_comic_merge_fmv_collision_yeared_has_prices(db):
    """R9: collision where yeared row already has prices -> yeared wins; null fmv discarded."""
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0)
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    upsert_fmv(db, yeared_id, grade=9.2, low=900.0)
    upsert_comic(db, "ASM", "300", year=1988)
    fmv_rows = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=?", (yeared_id,)
    ).fetchall()
    assert len(fmv_rows) == 1
    assert fmv_rows[0]["low"] == 900.0
    # NULL row gone
    assert db.execute(
        "SELECT COUNT(*) FROM comics WHERE id=?", (null_id,)
    ).fetchone()[0] == 0


def test_upsert_comic_merge_fmv_collision_null_has_prices_yeared_stub(db, caplog):
    """R9: yeared stub gets prices transplanted from null row; null fmv deleted; WARN."""
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0, high=1000.0, comps=10,
               confidence="high", notes="from null")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    # Stub fmv: grade set, no prices (mirrors api_link_locg auto-stub behavior).
    upsert_fmv(db, yeared_id, grade=9.2)
    import logging
    with caplog.at_level(logging.WARNING, logger="gixen_overlay.db"):
        upsert_comic(db, "ASM", "300", year=1988)
    fmv = db.execute(
        "SELECT low, high, comps, confidence, notes FROM fmv WHERE comic_id=?",
        (yeared_id,),
    ).fetchone()
    assert fmv["low"] == 800.0
    assert fmv["high"] == 1000.0
    assert fmv["comps"] == 10
    assert fmv["confidence"] == "high"
    assert fmv["notes"] == "from null"
    # Verify a WARNING about transplant was emitted
    assert any("transplanted" in rec.message for rec in caplog.records)
    # Null fmv is gone and null comic row was deleted
    assert db.execute(
        "SELECT COUNT(*) FROM fmv WHERE comic_id=?", (null_id,)
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM comics WHERE id=?", (null_id,)
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM fmv WHERE comic_id=?", (yeared_id,)
    ).fetchone()[0] == 1


def test_upsert_comic_merge_collision_discard_warning(db, caplog):
    """R9: when yeared already has prices and null also has prices, log warns of discard."""
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0)
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    upsert_fmv(db, yeared_id, grade=9.2, low=750.0)
    import logging
    with caplog.at_level(logging.WARNING, logger="gixen_overlay.db"):
        upsert_comic(db, "ASM", "300", year=1988)
    assert any("discarded" in rec.message for rec in caplog.records)
    # Yeared row kept its prices, null row's fmv is gone, null comic row deleted
    fmv = db.execute(
        "SELECT low FROM fmv WHERE comic_id=? AND grade=9.2", (yeared_id,)
    ).fetchone()
    assert fmv["low"] == 750.0
    assert db.execute(
        "SELECT COUNT(*) FROM fmv WHERE comic_id=?", (null_id,)
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM comics WHERE id=?", (null_id,)
    ).fetchone()[0] == 0


def test_upsert_comic_merge_transplant_preserves_yeared_partial_columns(db, caplog):
    """Todo 002: transplant must not overwrite a non-null yeared column with null.

    Yeared fmv has low=None but high=1200 (e.g., set by a prior targeted
    upsert_fmv). Null-row fmv has low=800 but high=None. After merge, the
    yeared fmv should have low=800 (transplanted) AND high=1200 (preserved).
    """
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0)  # high left NULL
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    # Yeared fmv: low NULL, but high already set
    upsert_fmv(db, yeared_id, grade=9.2, high=1200.0)
    upsert_comic(db, "ASM", "300", year=1988)
    fmv = db.execute(
        "SELECT low, high FROM fmv WHERE comic_id=? AND grade=9.2", (yeared_id,)
    ).fetchone()
    assert fmv["low"] == 800.0   # transplanted from null
    assert fmv["high"] == 1200.0  # preserved from yeared


def test_upsert_comic_merge_multi_grade_mixed_collision_and_reparent(db, caplog):
    """T3: null row has two fmv rows — one collides with yeared, one doesn't.

    The colliding one should be discarded (yeared has prices); the non-colliding
    one should reparent. Survivor ends up with exactly two fmv rows.
    """
    null_id = upsert_comic(db, "ASM", "300", year=None)
    upsert_fmv(db, null_id, grade=9.2, low=800.0)  # will collide
    upsert_fmv(db, null_id, grade=8.5, low=400.0)  # no collision
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    upsert_fmv(db, yeared_id, grade=9.2, low=900.0)  # yeared wins the collision
    upsert_comic(db, "ASM", "300", year=1988)
    rows = db.execute(
        "SELECT grade, low FROM fmv WHERE comic_id=? ORDER BY grade",
        (yeared_id,),
    ).fetchall()
    assert [(r["grade"], r["low"]) for r in rows] == [(8.5, 400.0), (9.2, 900.0)]
    assert db.execute(
        "SELECT COUNT(*) FROM comics WHERE id=?", (null_id,)
    ).fetchone()[0] == 0
    # No orphan fmv rows
    assert db.execute(
        "SELECT COUNT(*) FROM fmv WHERE comic_id=?", (null_id,)
    ).fetchone()[0] == 0


def test_upsert_comic_reboot_guard_raises(db):
    """R10: NULL row + other-year row + inbound different year -> raise."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1987)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', NULL)")
    db.commit()
    with pytest.raises(ReconciliationConflictError, match="manual disambiguation"):
        upsert_comic(db, "ASM", "300", year=1988)
    # Both rows still present
    n = db.execute(
        "SELECT COUNT(*) FROM comics WHERE title='ASM' AND issue='300'"
    ).fetchone()[0]
    assert n == 2


def test_upsert_comic_reboot_guard_same_year_merges(db):
    """R10: NULL row + matching-year row -> merge proceeds (no reboot)."""
    null_id = upsert_comic(db, "ASM", "300", year=None)
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    returned = upsert_comic(db, "ASM", "300", year=1988)
    assert returned == yeared_id
    assert db.execute("SELECT COUNT(*) FROM comics WHERE id=?", (null_id,)).fetchone()[0] == 0


def test_upsert_comic_reboot_no_null_row_creates_sibling(db):
    """R10: no NULL row + other-year row -> sibling reboot row is fine."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1987)")
    db.commit()
    new_id = upsert_comic(db, "ASM", "300", year=1988)
    rows = db.execute(
        "SELECT year FROM comics WHERE title='ASM' AND issue='300' ORDER BY year"
    ).fetchall()
    assert [r["year"] for r in rows] == [1987, 1988]
    assert db.execute("SELECT year FROM comics WHERE id=?", (new_id,)).fetchone()["year"] == 1988


def test_upsert_comic_bid_fmvs_survive_promotion(db):
    """Promoted NULL row keeps bid_fmvs links because comic_id and fmv_id are preserved."""
    bid_id = _insert_bid(db)
    null_id = upsert_comic(db, "ASM", "300", year=None)
    fmv_id = upsert_fmv(db, null_id, grade=9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fmv_id, is_primary=True)
    upsert_comic(db, "ASM", "300", year=1988)
    bf = db.execute(
        "SELECT bid_id, fmv_id FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchone()
    assert bf is not None and bf["fmv_id"] == fmv_id


def test_upsert_comic_year_none_with_two_reboot_siblings_raises(db):
    """Todo 003: year=None + 2+ yeared rows for (title, issue) -> raise."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 1963)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 1991)")
    db.commit()
    with pytest.raises(ReconciliationConflictError, match="multiple yeared reboot siblings"):
        upsert_comic(db, "X-Men", "1", year=None)
    # No NULL row should have been created
    n = db.execute(
        "SELECT COUNT(*) FROM comics WHERE title='X-Men' AND issue='1' AND year IS NULL"
    ).fetchone()[0]
    assert n == 0


def test_upsert_comic_year_none_with_one_yeared_sibling_short_circuits(db):
    """Todo 003: year=None + exactly 1 yeared row -> short-circuit (regression guard)."""
    yeared_id = upsert_comic(db, "X-Men", "1", year=1963)
    returned = upsert_comic(db, "X-Men", "1", year=None)
    assert returned == yeared_id


def test_upsert_comic_year_none_three_siblings_lists_all_years(db):
    """Todo 003: error message names every conflicting year, sorted."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 1991)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 1963)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 2019)")
    db.commit()
    with pytest.raises(ReconciliationConflictError, match=r"\[1963, 1991, 2019\]"):
        upsert_comic(db, "X-Men", "1", year=None)


# ---------------------------------------------------------------------------
# PER-98 todo 004: check_reconciliation_conflict (read-only pre-check)
# ---------------------------------------------------------------------------


def test_check_reconciliation_conflict_clean_state_returns_none(db):
    assert check_reconciliation_conflict(db, "ASM", "300", year=1988) is None
    assert check_reconciliation_conflict(db, "ASM", "300", year=None) is None


def test_check_reconciliation_conflict_reports_reboot_for_year_set(db):
    """Same shape as Case 3: NULL row + other-year row, no exact match."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1987)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', NULL)")
    db.commit()
    msg = check_reconciliation_conflict(db, "ASM", "300", year=1988)
    assert msg is not None
    assert "manual disambiguation" in msg
    assert "1987" in msg


def test_check_reconciliation_conflict_reports_reboot_for_year_none(db):
    """Same shape as Case 1a (Todo 003): 2+ yeared siblings, year=None inbound."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 1963)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('X-Men', '1', 1991)")
    db.commit()
    msg = check_reconciliation_conflict(db, "X-Men", "1", year=None)
    assert msg is not None
    assert "multiple yeared reboot siblings" in msg


def test_check_reconciliation_conflict_does_not_write(db):
    """Pre-check must be read-only."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1987)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', NULL)")
    db.commit()
    before = db.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
    check_reconciliation_conflict(db, "ASM", "300", year=1988)
    check_reconciliation_conflict(db, "ASM", "300", year=None)
    after = db.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
    assert before == after


def test_check_reconciliation_conflict_matches_match_with_other_year_is_fine(db):
    """year=Y where the exact-year row exists falls to Case 5 (merge), not conflict."""
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1987)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', NULL)")
    db.commit()
    # Inbound year matches 1988 — merge path, no conflict.
    assert check_reconciliation_conflict(db, "ASM", "300", year=1988) is None


def test_upsert_comic_merge_cascades_bid_fmvs_on_discarded_fmv(db):
    """When a null-row fmv is deleted during merge, its bid_fmvs cascade-delete."""
    bid_id = _insert_bid(db)
    null_id = upsert_comic(db, "ASM", "300", year=None)
    null_fmv = upsert_fmv(db, null_id, grade=9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, null_fmv, is_primary=True)
    db.execute("INSERT INTO comics (title, issue, year) VALUES ('ASM', '300', 1988)")
    db.commit()
    yeared_id = db.execute(
        "SELECT id FROM comics WHERE title='ASM' AND issue='300' AND year=1988"
    ).fetchone()["id"]
    upsert_fmv(db, yeared_id, grade=9.2, low=900.0)
    upsert_comic(db, "ASM", "300", year=1988)
    # bid_fmvs row that pointed at null_fmv is gone (cascade)
    bf_count = db.execute(
        "SELECT COUNT(*) FROM bid_fmvs WHERE bid_id=? AND fmv_id=?",
        (bid_id, null_fmv),
    ).fetchone()[0]
    assert bf_count == 0
