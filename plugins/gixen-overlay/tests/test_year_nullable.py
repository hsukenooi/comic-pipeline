"""Tests for PER-98 — nullable year + upsert_comic reconciliation."""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import (
    create_tables,
    upsert_comic,
    upsert_fmv,
    sweep_orphan_yearless_comics,
)


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


def test_migration_preserves_bids_fmv_id():
    """bids.fmv_id REFERENCES fmv(id) ON DELETE SET NULL, so dropping the fmv
    table during the rebuild fires that cascade and wipes every bid's primary
    fmv link. The migration must save and restore those values."""
    # Use the post-PER-98 schema so bids.fmv_id has the live FK.
    conn = _legacy_db_with_year_not_null()
    conn.execute("DROP TABLE bids")
    conn.execute(
        "CREATE TABLE bids ("
        "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL, "
        "fmv_id INTEGER REFERENCES fmv(id) ON DELETE SET NULL)"
    )
    conn.execute("INSERT INTO bids (id, item_id, max_bid) VALUES (1, 'a', 10.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)")
    conn.execute("INSERT INTO fmv (id, comic_id, grade, low, high) VALUES (10, 1, 9.0, 50, 60)")
    conn.execute("UPDATE bids SET fmv_id = 10 WHERE id = 1")
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (1, 10, 1)")
    conn.commit()

    create_tables(conn)

    row = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert row["fmv_id"] == 10, "bids.fmv_id must survive the migration"


def test_migration_drops_orphan_junction_rows_defensively():
    """sqlite3 CLI defaults to PRAGMA foreign_keys=OFF, which can leave
    orphan bid_fmvs / fmv rows when deletes bypass CASCADE. The migration
    must filter them out instead of crashing on FK enforcement."""
    conn = _legacy_db_with_year_not_null()
    conn.execute("INSERT INTO bids (id, item_id, max_bid) VALUES (1, 'a', 10.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)")
    conn.execute("INSERT INTO fmv (id, comic_id, grade) VALUES (1, 1, 9.0)")
    conn.commit()
    # PRAGMA foreign_keys is per-connection and ignored inside an active txn,
    # so flip it off only outside one — same trick the sqlite3 CLI uses
    # implicitly. Plants the orphan, then turn FKs back on.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (1, 999, 1)")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    create_tables(conn)  # must not raise

    # Orphan junction is dropped; valid rows survive.
    surviving = conn.execute("SELECT bid_id, fmv_id FROM bid_fmvs ORDER BY fmv_id").fetchall()
    assert [(r["bid_id"], r["fmv_id"]) for r in surviving] == []
    real_fmv = conn.execute("SELECT id FROM fmv").fetchall()
    assert [r["id"] for r in real_fmv] == [1]


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


def test_upsert_yeared_with_conflicting_sibling_skips_promotion():
    """PER-104: yearless row must not be promoted when a yeared row at a
    different year already exists — that would create two yeared siblings."""
    conn = _fresh_db()
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1987)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")
    yearless_id = conn.execute(
        "SELECT id FROM comics WHERE title='X' AND issue='1' AND year IS NULL"
    ).fetchone()[0]

    result = upsert_comic(conn, title="X", issue="1", year=1988)

    # Returns the yearless row unchanged.
    assert result == yearless_id
    row = conn.execute("SELECT year FROM comics WHERE id=?", (yearless_id,)).fetchone()
    assert row["year"] is None
    # No row at year=1988 was created.
    count_1988 = conn.execute(
        "SELECT count(*) FROM comics WHERE title='X' AND issue='1' AND year=1988"
    ).fetchone()[0]
    assert count_1988 == 0
    # Still exactly two rows (yeared 1987 + yearless).
    count = conn.execute(
        "SELECT count(*) FROM comics WHERE title='X' AND issue='1'"
    ).fetchone()[0]
    assert count == 2


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


# ---------------------------------------------------------------------------
# PER-103: orphan yearless cleanup
# ---------------------------------------------------------------------------


def test_yearless_insert_cleans_orphan_yearless_no_fmv():
    """PER-103: yearless insert finding canonical_yeared also deletes a
    pre-existing yearless orphan that has no fmv children."""
    conn = _fresh_db()
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")

    result = upsert_comic(conn, title="X", issue="1")

    yeared_id = conn.execute(
        "SELECT id FROM comics WHERE title='X' AND issue='1' AND year=1963"
    ).fetchone()[0]
    assert result == yeared_id
    # Orphan gone.
    assert conn.execute(
        "SELECT count(*) FROM comics WHERE title='X' AND issue='1' AND year IS NULL"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM comics WHERE title='X' AND issue='1'"
    ).fetchone()[0] == 1


def test_yearless_insert_cleans_orphan_and_migrates_fmv_no_conflict():
    """PER-103: orphan yearless fmv (no grade conflict on yeared) is
    reassigned to the yeared row — no data loss."""
    conn = _fresh_db()
    yeared_id = upsert_comic(conn, title="X", issue="1", year=1963)
    # Manually seed orphan yearless with an fmv at a grade the yeared row lacks.
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")
    yearless_id = conn.execute(
        "SELECT id FROM comics WHERE title='X' AND issue='1' AND year IS NULL"
    ).fetchone()[0]
    upsert_fmv(conn, comic_id=yearless_id, grade=9.2, low=800, high=1200)

    upsert_comic(conn, title="X", issue="1")

    # Orphan gone.
    assert conn.execute(
        "SELECT count(*) FROM comics WHERE year IS NULL AND title='X'"
    ).fetchone()[0] == 0
    # fmv migrated to yeared row.
    fmv = conn.execute(
        "SELECT * FROM fmv WHERE comic_id=? AND grade=9.2", (yeared_id,)
    ).fetchone()
    assert fmv is not None
    assert fmv["low"] == 800
    assert fmv["high"] == 1200


def test_yearless_insert_cleans_orphan_fmv_conflict_coalesces():
    """PER-103: when both yeared and yearless have fmv at the same grade, the
    yearless values fill in gaps (COALESCE) — yeared non-null fields win."""
    conn = _fresh_db()
    yeared_id = upsert_comic(conn, title="X", issue="1", year=1963)
    upsert_fmv(conn, comic_id=yeared_id, grade=9.2, low=900)
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")
    yearless_id = conn.execute(
        "SELECT id FROM comics WHERE year IS NULL AND title='X'"
    ).fetchone()[0]
    # Yearless has low=800 (different) and high=1200 (yeared lacks it).
    upsert_fmv(conn, comic_id=yearless_id, grade=9.2, low=800, high=1200)

    upsert_comic(conn, title="X", issue="1")

    fmv = conn.execute(
        "SELECT * FROM fmv WHERE comic_id=? AND grade=9.2", (yeared_id,)
    ).fetchone()
    # Yeared low=900 wins (non-null), yearless high=1200 fills the gap.
    assert fmv["low"] == 900
    assert fmv["high"] == 1200
    # Only one fmv row at that grade.
    assert conn.execute(
        "SELECT count(*) FROM fmv WHERE grade=9.2"
    ).fetchone()[0] == 1


def test_yearless_insert_reparents_bid_fmvs_on_conflict():
    """PER-103: bid_fmvs pointing to a yearless fmv are reparented to the
    surviving yeared fmv when grade conflicts on merge."""
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO bids (id, item_id, max_bid) VALUES (1, 'eb1', 50.0)"
    )
    yeared_id = upsert_comic(conn, title="X", issue="1", year=1963)
    yeared_fmv_id = upsert_fmv(conn, comic_id=yeared_id, grade=9.2, low=900)
    # Orphan yearless + its fmv linked to bid 1.
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")
    yearless_id = conn.execute(
        "SELECT id FROM comics WHERE year IS NULL AND title='X'"
    ).fetchone()[0]
    yearless_fmv_id = upsert_fmv(conn, comic_id=yearless_id, grade=9.2, low=800)
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (1, ?, 1)",
        (yearless_fmv_id,),
    )

    upsert_comic(conn, title="X", issue="1")

    # bid_fmvs now points to yeared fmv.
    row = conn.execute(
        "SELECT fmv_id FROM bid_fmvs WHERE bid_id=1"
    ).fetchone()
    assert row["fmv_id"] == yeared_fmv_id
    # Yearless fmv gone.
    assert conn.execute(
        "SELECT count(*) FROM fmv WHERE id=?", (yearless_fmv_id,)
    ).fetchone()[0] == 0


def test_sweep_dry_run_reports_without_mutating():
    """sweep_orphan_yearless_comics dry_run=True returns details but makes no changes."""
    conn = _fresh_db()
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', 1963)")
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")
    upsert_fmv(
        conn,
        comic_id=conn.execute(
            "SELECT id FROM comics WHERE year IS NULL"
        ).fetchone()[0],
        grade=9.2,
        low=800,
    )

    result = sweep_orphan_yearless_comics(conn, dry_run=True)

    assert result["dry_run"] is True
    assert result["would_merge"] == 1
    assert result["details"][0]["title"] == "X"
    # Nothing mutated.
    assert conn.execute(
        "SELECT count(*) FROM comics WHERE year IS NULL"
    ).fetchone()[0] == 1


def test_sweep_merges_orphan_and_removes_yearless():
    """sweep_orphan_yearless_comics dry_run=False merges fmv and removes yearless row."""
    conn = _fresh_db()
    yeared_id = upsert_comic(conn, title="X", issue="1", year=1963)
    conn.execute("INSERT INTO comics (title, issue, year) VALUES ('X', '1', NULL)")
    yearless_id = conn.execute(
        "SELECT id FROM comics WHERE year IS NULL AND title='X'"
    ).fetchone()[0]
    upsert_fmv(conn, comic_id=yearless_id, grade=9.2, low=800)

    result = sweep_orphan_yearless_comics(conn, dry_run=False)

    assert result["dry_run"] is False
    assert result["merged"] == 1
    assert conn.execute(
        "SELECT count(*) FROM comics WHERE year IS NULL"
    ).fetchone()[0] == 0
    fmv = conn.execute(
        "SELECT * FROM fmv WHERE comic_id=? AND grade=9.2", (yeared_id,)
    ).fetchone()
    assert fmv["low"] == 800


def test_sweep_no_orphans_returns_zero():
    """sweep_orphan_yearless_comics on a clean DB returns merged=0."""
    conn = _fresh_db()
    upsert_comic(conn, title="X", issue="1", year=1963)

    result = sweep_orphan_yearless_comics(conn, dry_run=False)

    assert result["merged"] == 0
    assert result["details"] == []
