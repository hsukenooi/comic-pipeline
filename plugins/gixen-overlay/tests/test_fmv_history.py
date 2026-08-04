"""Tests for fmv_history (BUI-659) — the append-only FMV price ledger.

`fmv` stores exactly one row per (comic_id, grade) — a recompute overwrites
it, so there is no way to ask what a book was worth last month. `fmv_history`
is the append-only fix: every `POST /api/comics` upsert that carries a grade
appends an immutable snapshot, and a one-time migration seeds one row per
pre-existing `fmv` row. These tests pin the two invariants that matter most:
the append happens on EVERY upsert including a no-op one (never conditional
on "did anything change"), and the seeding migration cannot double-seed on a
restart.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from gixen_overlay.db import (
    append_fmv_history,
    create_tables,
    upsert_comic,
    upsert_fmv,
)

# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """In-memory DB with the minimal bids stub the plugin's FK chain expects.

    Mirrors test_comps_ledger.py's `_make_db`.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
        "max_bid REAL NOT NULL, fmv_id INTEGER)"
    )
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


def _comic(conn, title="X-Men", issue="1", year=1963) -> int:
    return upsert_comic(conn, title, issue, year)


# ---------------------------------------------------------------------------
# create_tables: schema shape
# ---------------------------------------------------------------------------


def test_create_tables_creates_fmv_history():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "fmv_history" in tables
    conn.close()


def test_create_tables_fmv_history_is_idempotent(db):
    create_tables(db)  # second call must not raise
    assert db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0] == 0


def test_fmv_history_check_rejects_unknown_source(db):
    comic_id = _comic(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO fmv_history (comic_id, grade, source) "
            "VALUES (?, 9.4, 'bogus')",
            (comic_id,),
        )


def test_fmv_history_accepts_every_documented_source(db):
    comic_id = _comic(db)
    for source in ("upsert", "backfill"):
        db.execute(
            "INSERT INTO fmv_history (comic_id, grade, source) VALUES (?, 9.4, ?)",
            (comic_id, source),
        )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0] == 2


def test_fmv_history_comic_id_is_not_null(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO fmv_history (comic_id, grade, source) VALUES (NULL, 9.4, 'upsert')"
        )


def test_fmv_history_cascades_on_comics_delete(db):
    """Unlike comps (ON DELETE SET NULL), fmv_history is *about* the comics
    row, so deleting the book deletes its price history with it."""
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id)
    assert db.execute(
        "SELECT COUNT(*) FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()[0] == 1

    db.execute("DELETE FROM comics WHERE id=?", (comic_id,))
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# append_fmv_history
# ---------------------------------------------------------------------------


def test_append_fmv_history_snapshots_current_fmv_row(db):
    comic_id = _comic(db)
    fmv_id = upsert_fmv(
        db, comic_id, grade=9.4, low=100.0, high=150.0, comps=5,
        confidence="high", notes="a note",
    )
    append_fmv_history(db, fmv_id)
    row = db.execute("SELECT * FROM fmv_history").fetchone()
    assert row["comic_id"] == comic_id
    assert row["grade"] == 9.4
    assert row["low"] == 100.0
    assert row["high"] == 150.0
    assert row["comps"] == 5
    assert row["confidence"] == "high"
    assert row["notes"] == "a note"
    assert row["flag_reason"] is None
    assert row["source"] == "upsert"


def test_append_fmv_history_recorded_at_matches_fmv_updated_at(db):
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id)
    fmv_updated_at = db.execute(
        "SELECT updated_at FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()["updated_at"]
    assert fmv_updated_at is not None
    history_recorded_at = db.execute(
        "SELECT recorded_at FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()["recorded_at"]
    assert history_recorded_at == fmv_updated_at


def test_append_fmv_history_raises_for_unknown_fmv_id(db):
    with pytest.raises(ValueError, match="no fmv row with id"):
        append_fmv_history(db, 999999)


def test_append_fmv_history_always_appends_two_calls_two_rows(db):
    """R11 refinement: identical values, called twice, produce two rows —
    'we re-measured and nothing moved' is a fact worth a row."""
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id)
    append_fmv_history(db, fmv_id)
    assert db.execute(
        "SELECT COUNT(*) FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()[0] == 2


def test_append_fmv_history_accepts_explicit_backfill_source(db):
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id, source="backfill")
    row = db.execute("SELECT source FROM fmv_history").fetchone()
    assert row["source"] == "backfill"


# ---------------------------------------------------------------------------
# The one-time seeding migration
# ---------------------------------------------------------------------------
#
# `create_tables` seeds fmv_history from pre-existing `fmv` rows exactly
# once, gated via migration_state (never re-runs on a restart). To exercise
# that "pre-existing" scenario without hand-rolling a whole legacy schema
# (the fmv-split/year-nullable precedent), these tests build fmv rows through
# the normal API on an already-migrated DB, then roll back exactly the two
# artifacts BUI-659 adds (drop fmv_history, clear its migration_state marker)
# to simulate "a DB that had fmv rows before this migration shipped," and
# call create_tables again — the "deploy" moment.


def _simulate_pre_bui659_state(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM migration_state WHERE migration='seed_fmv_history'")
    conn.execute("DROP TABLE fmv_history")
    conn.commit()


def test_seed_migration_seeds_one_row_per_existing_fmv_row(db):
    comic1 = _comic(db, title="X-Men", issue="1", year=1963)
    upsert_fmv(db, comic1, grade=9.4, low=100.0, high=150.0, comps=5,
               confidence="high", notes="priced")
    comic2 = _comic(db, title="Fantastic Four", issue="1", year=1961)
    upsert_fmv(db, comic2, grade=8.0)  # bare stub: no price, updated_at NULL
    comic3 = _comic(db, title="Daredevil", issue="1", year=1964)
    upsert_fmv(db, comic3, grade=9.0, flag_reason="one_sided")

    _simulate_pre_bui659_state(db)
    create_tables(db)  # the "deploy" moment

    fmv_rows = {
        (r["comic_id"], r["grade"]): r
        for r in db.execute("SELECT * FROM fmv").fetchall()
    }
    history_rows = db.execute("SELECT * FROM fmv_history").fetchall()
    assert len(history_rows) == len(fmv_rows) == 3

    for h in history_rows:
        assert h["source"] == "backfill"
        src = fmv_rows[(h["comic_id"], h["grade"])]
        assert h["low"] == src["low"]
        assert h["high"] == src["high"]
        assert h["comps"] == src["comps"]
        assert h["confidence"] == src["confidence"]
        assert h["flag_reason"] == src["flag_reason"]
        assert h["notes"] == src["notes"]
        assert h["recorded_at"] == src["updated_at"]

    # The bare stub (never priced) carries its NULL updated_at through too —
    # no fallback to "now" is fabricated for it.
    stub_history = db.execute(
        "SELECT recorded_at FROM fmv_history WHERE comic_id=?", (comic2,)
    ).fetchone()
    assert stub_history["recorded_at"] is None


def test_seed_migration_is_idempotent_no_double_seed(db):
    comic_id = _comic(db)
    upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    _simulate_pre_bui659_state(db)

    create_tables(db)
    first_count = db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0]
    create_tables(db)  # simulate a restart — must not double-seed
    second_count = db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0]

    assert first_count == 1
    assert second_count == first_count


def test_seed_migration_uses_fmv_updated_at_not_the_migrations_clock(db):
    comic_id = _comic(db)
    upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    # Backdate updated_at so a fresh "now" computed by the migration would be
    # trivially distinguishable from it.
    db.execute(
        "UPDATE fmv SET updated_at=? WHERE comic_id=?",
        ("2020-01-01T00:00:00+00:00", comic_id),
    )
    db.commit()

    _simulate_pre_bui659_state(db)
    create_tables(db)

    row = db.execute(
        "SELECT recorded_at FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()
    assert row["recorded_at"] == "2020-01-01T00:00:00+00:00"


def test_fresh_and_migrated_db_end_in_the_same_fmv_history_schema():
    """(c) A fresh DB (fmv_history created empty) and an existing DB (created
    via the seeding migration finding pre-existing fmv rows) must define the
    identical fmv_history schema."""
    fresh = _make_db()
    create_tables(fresh)

    migrated = _make_db()
    create_tables(migrated)
    comic_id = _comic(migrated)
    upsert_fmv(migrated, comic_id, grade=9.4, low=100.0)
    _simulate_pre_bui659_state(migrated)
    create_tables(migrated)

    fresh_cols = [tuple(r) for r in fresh.execute("PRAGMA table_info(fmv_history)")]
    migrated_cols = [
        tuple(r) for r in migrated.execute("PRAGMA table_info(fmv_history)")
    ]
    assert fresh_cols == migrated_cols
    assert db_index_names(fresh) == db_index_names(migrated)
    fresh.close()
    migrated.close()


def db_index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='fmv_history'"
        )
    }


# ---------------------------------------------------------------------------
# POST /api/comics — the live append (route layer)
# ---------------------------------------------------------------------------


def _create_comic(api, **overrides) -> dict:
    body = {"title": "Amazing Spider-Man", "issue": "1", "year": 1963}
    body.update(overrides)
    return api.post("/api/comics", json=body).json()


def _history_rows_from_db(comic_id: int) -> list[sqlite3.Row]:
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    rows = raw.execute(
        "SELECT * FROM fmv_history WHERE comic_id=? ORDER BY id", (comic_id,)
    ).fetchall()
    raw.close()
    return rows


def test_upsert_comic_with_grade_appends_one_history_row(api):
    data = _create_comic(api, grade=9.4, fmv_low=100.0, fmv_high=150.0)
    rows = _history_rows_from_db(data["comic_id"])
    assert len(rows) == 1
    assert rows[0]["grade"] == 9.4
    assert rows[0]["low"] == 100.0
    assert rows[0]["source"] == "upsert"


def test_upsert_comic_without_grade_appends_no_history_row(api):
    data = _create_comic(api)
    rows = _history_rows_from_db(data["comic_id"])
    assert rows == []


def test_upsert_comic_two_identical_upserts_append_two_history_rows(api):
    payload = {"title": "Hulk", "issue": "181", "year": 1974,
               "grade": 9.8, "fmv_low": 5000.0, "fmv_high": 7000.0}
    r1 = api.post("/api/comics", json=payload).json()
    r2 = api.post("/api/comics", json=payload).json()
    assert r1["comic_id"] == r2["comic_id"]
    rows = _history_rows_from_db(r1["comic_id"])
    assert len(rows) == 2


def test_upsert_comic_multi_issue_lot_422_appends_no_history_row(api):
    r = api.post("/api/comics", json={
        "title": "Amazing Spider-man #18,19,20,21,22 lot of 5 NM Gems Wow",
        "issue": "18",
        "grade": 9.0,
    })
    assert r.status_code == 422
    # No comic was even created, so there is nothing to look up by comic_id —
    # assert the table is empty outright.
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    count = raw.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0]
    raw.close()
    assert count == 0


def test_append_failure_does_not_break_the_upsert_response(api, caplog):
    """A broken append degrades to a log line, never a broken write — the fmv
    row itself is already durable by the time append_fmv_history runs."""
    def _explode(*a, **kw):
        raise sqlite3.OperationalError("no such table: fmv_history")

    with patch("gixen_overlay.routes.append_fmv_history", side_effect=_explode):
        r = api.post("/api/comics", json={
            "title": "Iron Man", "issue": "1", "year": 1968,
            "grade": 9.0, "fmv_low": 300.0, "fmv_high": 400.0,
        })
    assert r.status_code == 200
    data = r.json()
    assert data["fmv_id"] is not None

    # The fmv row itself landed even though the history append blew up.
    gr = api.get("/api/comics", params={"grade": 9.0})
    assert gr.json()[0]["fmv_low"] == 300.0

    assert _history_rows_from_db(data["comic_id"]) == []
    assert any(
        "append_fmv_history failed" in r.message for r in caplog.records
    )
