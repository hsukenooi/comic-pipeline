"""Tests for the BUI-950 slab watch set.

Three layers, in order:

  1. The MIGRATION (`comics.slab_watch`), rehearsed on a backup copy of a
     pre-migration DB — mirrors test_comps_exclusion.py's BUI-947 pattern,
     the closest prior art for "one additive nullable column + a rehearsal
     test."
  2. The DB-layer read/write functions: `list_comics_for_slab_watch` (the
     per-comic raw-high/override map) and `set_comic_slab_watch` (the hand
     override toggle).
  3. The two routes — `GET /api/comics/slab-watch` (threshold inclusion,
     hand include/exclude, the no-raw-row case, the per-request
     `SLAB_WATCH_MIN_FMV` read, and the identity join against a JSON
     wish-list, including a `(Vol. 2)`-decorated title and a leading "The")
     and `POST /api/comics/{id}/slab-watch` (the toggle's 404/422
     boundary) — against the real server with the real overlay plugin
     loaded, the same `_seeded_client` pattern test_collection_api.py uses.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import (
    create_tables,
    list_comics_for_slab_watch,
    set_comic_slab_watch,
    upsert_comic,
    upsert_fmv,
)

from .conftest import _seed_wish_list, _seeded_client


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db(path: str = ":memory:") -> sqlite3.Connection:
    """Mirrors test_comps_exclusion.py's `_make_db`: `comics`/`fmv` carry no
    FK to `bids`, but `bid_fmvs` (same `create_tables` call) does, so the
    stub keeps this consistent with the rest of the suite."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bids (id INTEGER PRIMARY KEY, "
        "item_id TEXT NOT NULL, max_bid REAL NOT NULL, fmv_id INTEGER)"
    )
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 1. The migration, rehearsed on a backup copy
# ---------------------------------------------------------------------------


def _pre_migration_db(path: str) -> None:
    """Build a DB file at `path` in today's schema MINUS `comics.slab_watch`,
    with a real comic in it. Built by DROPping the column off a fully
    migrated DB (see test_comps_exclusion.py's `_pre_migration_db` for why
    that beats a hand-frozen `CREATE TABLE` literal), with WAL set explicitly
    so the `.backup` rehearsal below is meaningful.
    """
    conn = _make_db(path)
    conn.execute("PRAGMA journal_mode=WAL")
    create_tables(conn)
    upsert_comic(conn, "Invincible", "1", 2003)
    conn.execute("ALTER TABLE comics DROP COLUMN slab_watch")
    conn.commit()
    conn.close()


def _comics_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(comics)")}


def _comics_snapshot(conn) -> list[tuple]:
    return conn.execute(
        "SELECT id, title, issue, year, variant, locg_id, locg_variant_id "
        "FROM comics ORDER BY id"
    ).fetchall()


def test_migration_rehearsal_on_a_backup_copy(tmp_path):
    """The ticket's done-when: rehearse on a `sqlite3 .backup` copy (never a
    plain `cp` of a WAL-mode DB — see docs/reference on WAL backup safety and
    test_comps_exclusion.py's identical rehearsal) before the real migration
    ever touches a live DB."""
    live = tmp_path / "live.db"
    _pre_migration_db(str(live))

    backup = tmp_path / "backup.db"
    src = sqlite3.connect(str(live))
    dst = sqlite3.connect(str(backup))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    conn = _make_db(str(backup))
    assert "slab_watch" not in _comics_columns(conn)
    before = _comics_snapshot(conn)
    assert len(before) == 1

    create_tables(conn)

    assert "slab_watch" in _comics_columns(conn)
    # No backfill: every pre-existing row is unchanged and NULL.
    assert _comics_snapshot(conn) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM comics WHERE slab_watch IS NOT NULL"
    ).fetchone()[0] == 0
    conn.close()


def test_migration_is_idempotent_run_twice_on_the_backup(tmp_path):
    """SQLite refuses a duplicate ADD COLUMN, so the PRAGMA guard is the
    whole migration — running it again (e.g. every process start) must be a
    silent no-op, including after a hand override has been written."""
    live = tmp_path / "live.db"
    _pre_migration_db(str(live))
    conn = _make_db(str(live))
    create_tables(conn)
    comic_id = conn.execute("SELECT id FROM comics").fetchone()[0]
    set_comic_slab_watch(conn, comic_id, 1)

    create_tables(conn)  # second run
    create_tables(conn)  # third, for good measure

    assert conn.execute(
        "SELECT slab_watch FROM comics WHERE id = ?", (comic_id,)
    ).fetchone()[0] == 1
    conn.close()


def test_a_fresh_install_has_the_column_and_no_overrides(db):
    assert "slab_watch" in _comics_columns(db)
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    row = db.execute(
        "SELECT slab_watch FROM comics WHERE id = ?", (comic_id,)
    ).fetchone()
    assert row["slab_watch"] is None


# ---------------------------------------------------------------------------
# 2. DB-layer read/write
# ---------------------------------------------------------------------------


def test_set_comic_slab_watch_unknown_id_returns_none(db):
    assert set_comic_slab_watch(db, 999999, 1) is None


def test_set_comic_slab_watch_round_trips_through_1_0_and_null(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)

    result = set_comic_slab_watch(db, comic_id, 1)
    assert result == {"comic_id": comic_id, "slab_watch": 1}
    row = list_comics_for_slab_watch(db)[0]
    assert row["slab_watch"] == 1

    set_comic_slab_watch(db, comic_id, 0)
    row = list_comics_for_slab_watch(db)[0]
    assert row["slab_watch"] == 0

    set_comic_slab_watch(db, comic_id, None)
    row = list_comics_for_slab_watch(db)[0]
    assert row["slab_watch"] is None


def test_set_comic_slab_watch_only_ever_writes_1_0_or_null_via_the_column_check(db):
    """Defense in depth below the pydantic boundary (models.SlabWatchRequest):
    the column's own CHECK constraint refuses anything else, even a direct
    Python caller that bypasses the route."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    with pytest.raises(sqlite3.IntegrityError):
        set_comic_slab_watch(db, comic_id, 2)


def test_list_comics_for_slab_watch_raw_high_is_max_across_grades(db):
    """raw_high is the MAX fmv.high across every RAW (certifier='none') row
    at ANY grade — not just the row at one particular grade."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_fmv(db, comic_id, grade=9.8, low=50, high=120, certifier="none")
    upsert_fmv(db, comic_id, grade=9.0, low=40, high=90, certifier="none")
    # A CGC slab row at the same comic must NOT pollute the raw high-water
    # mark (BUI-924's certifier identity) — pin the whole point of scoping
    # `list_comics_for_slab_watch`'s LEFT JOIN to certifier='none'.
    upsert_fmv(db, comic_id, grade=9.8, low=800, high=1200, certifier="cgc")

    row = list_comics_for_slab_watch(db)[0]
    assert row["raw_high"] == 120


def test_list_comics_for_slab_watch_no_fmv_row_is_null_not_zero(db):
    upsert_comic(db, "Invincible", "1", 2003)
    row = list_comics_for_slab_watch(db)[0]
    assert row["raw_high"] is None
    assert row["slab_watch"] is None


# ---------------------------------------------------------------------------
# 3. Routes — GET /api/comics/slab-watch, POST .../{id}/slab-watch
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    with _seeded_client(tmp_path, monkeypatch, store) as c:
        yield c


def _upsert(client, **kwargs) -> int:
    payload = {"title": "Invincible", "issue": "1", "year": 2003, **kwargs}
    resp = client.post("/api/comics", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["comic_id"]


def test_slab_watch_threshold_inclusion_and_exclusion(client):
    above = _upsert(
        client, title="Pricey Book", issue="1", year=2000,
        grade=9.8, fmv_high=250,
    )
    below = _upsert(
        client, title="Cheap Book", issue="1", year=2001,
        grade=9.8, fmv_high=40,
    )
    _seed_wish_list(client.store, [
        {"name": "Pricey Book #1", "id": None},
        {"name": "Cheap Book #1", "id": None},
    ])

    resp = client.get("/api/comics/slab-watch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["threshold"] == 100.0
    ids = {item["comic_id"]: item for item in body["items"]}
    assert above in ids and ids[above]["reason"] == "threshold"
    assert ids[above]["raw_high"] == 250
    assert ids[above]["certifier"] == "cgc"
    assert below not in ids


def test_slab_watch_hand_include_and_exclude_round_trip_with_reasons(client):
    cheap = _upsert(
        client, title="Cheap Hand-Included", issue="1", year=2001,
        grade=9.8, fmv_high=10,
    )
    pricey = _upsert(
        client, title="Pricey Hand-Excluded", issue="1", year=2002,
        grade=9.8, fmv_high=999,
    )
    _seed_wish_list(client.store, [
        {"name": "Cheap Hand-Included #1", "id": None},
        {"name": "Pricey Hand-Excluded #1", "id": None},
    ])

    # Before any override: cheap book out, pricey book in (threshold).
    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"]: item for item in body["items"]}
    assert cheap not in ids
    assert pricey in ids and ids[pricey]["reason"] == "threshold"

    # Hand-include the cheap book, hand-exclude the pricey one.
    resp = client.post(f"/api/comics/{cheap}/slab-watch", json={"slab_watch": 1})
    assert resp.status_code == 200
    assert resp.json() == {"comic_id": cheap, "slab_watch": 1}
    resp = client.post(f"/api/comics/{pricey}/slab-watch", json={"slab_watch": 0})
    assert resp.status_code == 200
    assert resp.json() == {"comic_id": pricey, "slab_watch": 0}

    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"]: item for item in body["items"]}
    assert cheap in ids and ids[cheap]["reason"] == "hand"
    assert pricey not in ids  # hand-exclude always wins over threshold

    # Clearing the override restores threshold-only behavior.
    client.post(f"/api/comics/{cheap}/slab-watch", json={"slab_watch": None})
    client.post(f"/api/comics/{pricey}/slab-watch", json={"slab_watch": None})
    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"]: item for item in body["items"]}
    assert cheap not in ids
    assert pricey in ids and ids[pricey]["reason"] == "threshold"


def test_slab_watch_no_raw_row_enters_only_by_hand(client):
    """A wish-list book whose comics row carries no priced FMV at all (no
    `POST /api/comics` grade ever posted) never qualifies by threshold —
    there is nothing to compare against the line — and enters the set ONLY
    via the hand override."""
    unpriced = _upsert(client, title="Unpriced Book", issue="1", year=1999)
    _seed_wish_list(client.store, [{"name": "Unpriced Book #1", "id": None}])

    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"]: item for item in body["items"]}
    assert unpriced not in ids

    client.post(f"/api/comics/{unpriced}/slab-watch", json={"slab_watch": 1})
    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"]: item for item in body["items"]}
    assert unpriced in ids
    assert ids[unpriced]["reason"] == "hand"
    assert ids[unpriced]["raw_high"] is None


def test_slab_watch_min_fmv_read_per_request(client, monkeypatch):
    """SLAB_WATCH_MIN_FMV is read fresh on every request (KTD2), never
    cached at process start."""
    comic_id = _upsert(
        client, title="Mid Book", issue="1", year=2005, grade=9.8, fmv_high=75,
    )
    _seed_wish_list(client.store, [{"name": "Mid Book #1", "id": None}])

    body = client.get("/api/comics/slab-watch").json()
    assert body["threshold"] == 100.0
    assert comic_id not in {item["comic_id"] for item in body["items"]}

    monkeypatch.setenv("SLAB_WATCH_MIN_FMV", "50")
    body = client.get("/api/comics/slab-watch").json()
    assert body["threshold"] == 50.0
    assert comic_id in {item["comic_id"] for item in body["items"]}

    monkeypatch.delenv("SLAB_WATCH_MIN_FMV")
    body = client.get("/api/comics/slab-watch").json()
    assert body["threshold"] == 100.0
    assert comic_id not in {item["comic_id"] for item in body["items"]}


def test_slab_watch_identity_join_handles_vol_suffix_and_leading_the(client):
    """The wish-list side carries a leading "The" the comics side lacks, and
    the comics side carries a `(Vol. 2)` + year-range decoration the
    wish-list side lacks — `_normalize_series_key` must fold both away so
    the two meet on one key."""
    comic_id = _upsert(
        client, title="Sandman (Vol. 2) (1989 - 1996)", issue="1", year=1989,
        grade=9.8, fmv_high=300,
    )
    _seed_wish_list(client.store, [{"name": "The Sandman #1", "id": None}])

    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"]: item for item in body["items"]}
    assert comic_id in ids
    assert ids[comic_id]["reason"] == "threshold"
    assert ids[comic_id]["title"] == "Sandman (Vol. 2) (1989 - 1996)"


def test_slab_watch_ambiguous_key_breaks_tie_on_wish_year_never_gates(client):
    """Two `comics` rows collide on the same normalized key + issue (two
    eras of one masthead — the documented `_normalize_series_key` looseness,
    see the "LOCG reuses Vol. N labels" project memory). A wish item whose
    stamped Cover Year matches one of them resolves onto THAT one; the year
    is a tiebreak, never a gate — both candidates stay priceable/qualifying
    candidates regardless of the year match."""
    old_era = _upsert(
        client, title="X-Men (Vol. 1) (1963 - 1981)", issue="1", year=1963,
        grade=9.8, fmv_high=500,
    )
    new_era = _upsert(
        client, title="X-Men (Vol. 2) (1991 - 2001)", issue="1", year=1991,
        grade=9.8, fmv_high=150,
    )
    _seed_wish_list(client.store, [
        {"name": "X-Men #1", "id": None, "year": "1991"},
    ])

    body = client.get("/api/comics/slab-watch").json()
    ids = {item["comic_id"] for item in body["items"]}
    # The wish's stamped year (1991) resolves it onto the new-era row, not
    # the old one, even though the old-era row alone would also qualify.
    assert new_era in ids
    assert old_era not in ids


def test_slab_watch_empty_wish_list_yields_empty_result(client):
    """A never-imported wish-list (FileNotFoundError) yields an empty set —
    same fail-soft posture as GET /api/comics/wish-list, never a 500."""
    resp = client.get("/api/comics/slab-watch")
    assert resp.status_code == 200
    assert resp.json() == {"threshold": 100.0, "count": 0, "items": []}


def test_post_slab_watch_404s_on_unknown_comic_id(client):
    resp = client.post("/api/comics/999999/slab-watch", json={"slab_watch": 1})
    assert resp.status_code == 404


def test_post_slab_watch_422s_on_an_out_of_vocabulary_value(client):
    comic_id = _upsert(client)
    resp = client.post(f"/api/comics/{comic_id}/slab-watch", json={"slab_watch": 2})
    assert resp.status_code == 422
