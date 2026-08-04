"""Tests for the comps ledger (BUI-656) — `gixen_overlay.db.upsert_comps` /
`create_tables`'s `comps` table, and `POST /api/comics/comps`.

The ledger's whole reason to exist (see the comps-data-flywheel plan's
Problem Frame) is that `fmv` stores a comp COUNT under
`UNIQUE(comic_id, grade)` — a recompute overwrites it, retaining no history.
These tests pin the two invariants that make `comps` different: re-observing
an already-known comp only ever bumps bookkeeping (KTD4 — a sold listing is
immutable, so the first recorded price/date wins), and identity is nullable
and never inferred (KTD3 — the COALESCE(comic_id, -1) trick that lets an
identity-free row still dedupe against itself).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gixen_overlay.db import create_tables, upsert_comic, upsert_comps

# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """Create an in-memory DB with the minimal bids stub the plugin expects.

    Mirrors test_gixen_overlay_db.py's `_make_db` — `comps` has no FK to
    `bids`, but `bid_fmvs` (created by the same `create_tables` call) does,
    so the stub keeps this consistent with every other test in the suite.
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


def _comp(**overrides) -> dict:
    base = {
        "pool": "raw",
        "provider": "sold-comps.com",
        "product_id": "1234567890",
        "title": "X-Men #1 CGC 9.4",
        "price": 500.0,
        "sold_date": "2026-07-01",
        "grade": 9.4,
        "buying_format": "AUCTION",
        "link": "https://ebay.com/itm/1234567890",
        "query": "X-Men 1",
        "tier": "1",
        "from_cache": False,
        "observed_at": "2026-07-01T00:00:00Z",
        "provenance": "live",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# create_tables: schema shape
# ---------------------------------------------------------------------------


def test_create_tables_creates_comps():
    conn = _make_db()
    create_tables(conn)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    assert "comps" in {row[0] for row in cur}
    conn.close()


def test_create_tables_comps_is_idempotent(db):
    create_tables(db)  # second call must not raise
    assert db.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 0


def test_comps_check_rejects_unknown_pool(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO comps (pool, provider, product_id, provenance, "
            "first_seen_at, last_seen_at) "
            "VALUES ('bogus', 'p', '1', 'live', 'x', 'x')"
        )


def test_comps_check_rejects_unknown_provenance(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO comps (pool, provider, product_id, provenance, "
            "first_seen_at, last_seen_at) "
            "VALUES ('raw', 'p', '1', 'bogus', 'x', 'x')"
        )


def test_comps_accepts_every_documented_pool_and_provenance(db):
    comic_id = _comic(db)
    for pool in ("raw", "slab"):
        for provenance in ("live", "backfill-cache", "backfill-capture"):
            db.execute(
                "INSERT INTO comps (comic_id, pool, provider, product_id, "
                "provenance, first_seen_at, last_seen_at) "
                "VALUES (?, ?, 'p', ?, ?, 'x', 'x')",
                (comic_id, pool, f"{pool}-{provenance}", provenance),
            )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 6


# ---------------------------------------------------------------------------
# upsert_comps: fresh inserts
# ---------------------------------------------------------------------------


def test_upsert_comps_inserts_new_row(db):
    comic_id = _comic(db)
    result = upsert_comps(db, comic_id, [_comp()])
    assert result == {"inserted": 1, "updated": 0, "conflicts": 0}
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["comic_id"] == comic_id
    assert row["provider"] == "sold-comps.com"
    assert row["price"] == 500.0
    assert row["sold_date"] == "2026-07-01"
    assert row["seen_count"] == 1
    assert row["conflict_count"] == 0
    assert row["first_seen_at"] == row["last_seen_at"]


def test_upsert_comps_inserts_identity_free_row_with_null_comic_id(db):
    result = upsert_comps(db, None, [_comp()])
    assert result == {"inserted": 1, "updated": 0, "conflicts": 0}
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["comic_id"] is None


def test_upsert_comps_batch_of_two_new_comps(db):
    comic_id = _comic(db)
    result = upsert_comps(
        db, comic_id,
        [_comp(product_id="1"), _comp(product_id="2")],
    )
    assert result == {"inserted": 2, "updated": 0, "conflicts": 0}
    assert db.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 2


def test_upsert_comps_missing_required_field_raises_named_value_error(db):
    """A direct caller bypassing the route's pydantic model (e.g. a future
    backfill script) gets a named error, not a bare KeyError."""
    comic_id = _comic(db)
    comp = _comp()
    del comp["provider"]
    with pytest.raises(ValueError, match="provider"):
        upsert_comps(db, comic_id, [comp])


# ---------------------------------------------------------------------------
# upsert_comps: re-observation (KTD4)
# ---------------------------------------------------------------------------


def test_upsert_comps_reobservation_same_price_bumps_seen_count(db):
    """AE1: same product id at the same price -> seen_count 2, every other
    field unchanged, conflict_count 0."""
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp()])
    result = upsert_comps(db, comic_id, [_comp()])
    assert result == {"inserted": 0, "updated": 1, "conflicts": 0}
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["seen_count"] == 2
    assert row["conflict_count"] == 0
    assert row["price"] == 500.0
    assert row["sold_date"] == "2026-07-01"


def test_upsert_comps_reobservation_different_price_flags_conflict(db):
    """AE1: same product id at a different price -> stored price unchanged,
    conflict_count 1."""
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp(price=500.0)])
    result = upsert_comps(db, comic_id, [_comp(price=550.0)])
    assert result == {"inserted": 0, "updated": 1, "conflicts": 1}
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["price"] == 500.0  # first answer wins, never rewritten
    assert row["conflict_count"] == 1
    assert row["seen_count"] == 2


def test_upsert_comps_reobservation_different_sold_date_flags_conflict(db):
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp(sold_date="2026-07-01")])
    result = upsert_comps(db, comic_id, [_comp(sold_date="2026-07-02")])
    assert result["conflicts"] == 1
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["sold_date"] == "2026-07-01"


def test_upsert_comps_never_updates_title_or_link(db):
    """No code path UPDATEs price, sold_date, title, or link on an existing
    row — the acceptance criterion, tested against the two fields not
    otherwise covered by the price/sold_date conflict tests above."""
    comic_id = _comic(db)
    upsert_comps(
        db, comic_id,
        [_comp(title="Original Title", link="http://original")],
    )
    upsert_comps(
        db, comic_id,
        [_comp(title="Changed Title", link="http://changed")],
    )
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["title"] == "Original Title"
    assert row["link"] == "http://original"


def test_upsert_comps_title_disagreement_does_not_flag_conflict(db):
    """KTD4 is explicit: the conflict signal is a disagreeing PRICE or SOLD
    DATE only. A title (or any other non-market-fact field) disagreement is
    silently absorbed as an ordinary re-observation — bookkeeping still
    advances, but it must not count as a conflict."""
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp(title="Original Title")])
    result = upsert_comps(db, comic_id, [_comp(title="Retitled Listing")])
    assert result == {"inserted": 0, "updated": 1, "conflicts": 0}
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["title"] == "Original Title"
    assert row["seen_count"] == 2
    assert row["conflict_count"] == 0


def test_upsert_comps_price_never_backfilled_once_first_seen_null(db):
    """The 'never rewrites' rule is unconditional (per the acceptance
    criterion's literal wording), including the case where the FIRST
    observation had no price at all — a later observation supplying one does
    NOT backfill it. Documents the behavior so a well-intentioned future
    'helpful backfill' change doesn't quietly violate KTD4."""
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp(price=None)])
    result = upsert_comps(db, comic_id, [_comp(price=500.0)])
    assert result == {"inserted": 0, "updated": 1, "conflicts": 0}
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row["price"] is None


def test_upsert_comps_batch_mixed_insert_update_and_conflict_counts(db):
    """One call carrying a fresh insert, a clean re-observation, and a
    conflicting re-observation together — the returned counts must sum
    correctly across a mixed batch, not just a batch of one."""
    comic_id = _comic(db)
    upsert_comps(
        db, comic_id,
        [_comp(product_id="clean", price=100.0),
         _comp(product_id="conflicted", price=200.0)],
    )
    result = upsert_comps(
        db, comic_id,
        [
            _comp(product_id="new", price=300.0),           # fresh insert
            _comp(product_id="clean", price=100.0),          # clean re-observe
            _comp(product_id="conflicted", price=250.0),     # conflicting re-observe
        ],
    )
    assert result == {"inserted": 1, "updated": 2, "conflicts": 1}


def test_upsert_comps_last_seen_at_advances_on_reobservation(db):
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp()])
    # Force a distinguishable clock tick without depending on real elapsed
    # wall time between the two upsert_comps calls in this test.
    db.execute("UPDATE comps SET first_seen_at='2020-01-01T00:00:00+00:00', "
               "last_seen_at='2020-01-01T00:00:00+00:00'")
    db.commit()
    upsert_comps(db, comic_id, [_comp()])
    row = db.execute("SELECT first_seen_at, last_seen_at FROM comps").fetchone()
    assert row["first_seen_at"] == "2020-01-01T00:00:00+00:00"  # untouched
    assert row["last_seen_at"] != "2020-01-01T00:00:00+00:00"  # bumped


# ---------------------------------------------------------------------------
# upsert_comps: identity (KTD3 / AE8)
# ---------------------------------------------------------------------------


def test_upsert_comps_same_product_id_different_pool_creates_two_rows(db):
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp(pool="raw"), _comp(pool="slab")])
    rows = db.execute("SELECT pool FROM comps ORDER BY pool").fetchall()
    assert [r["pool"] for r in rows] == ["raw", "slab"]


def test_upsert_comps_null_identity_dedupes_via_coalesce(db):
    """AE8/KTD3: two backfilled comps with the same product_id/provider and
    no resolvable comic fold into ONE row, not two, because the unique index
    folds NULL identity through COALESCE(comic_id, -1)."""
    upsert_comps(db, None, [_comp()])
    result = upsert_comps(db, None, [_comp()])
    assert result == {"inserted": 0, "updated": 1, "conflicts": 0}
    rows = db.execute("SELECT * FROM comps WHERE comic_id IS NULL").fetchall()
    assert len(rows) == 1
    assert rows[0]["seen_count"] == 2


def test_upsert_comps_same_product_id_different_comic_id_creates_two_rows(db):
    """A listing surfaced for two different books is two observations, not a
    conflict."""
    comic_a = _comic(db, title="X-Men", issue="1", year=1963)
    comic_b = _comic(db, title="X-Men", issue="1", year=1991)
    upsert_comps(db, comic_a, [_comp()])
    upsert_comps(db, comic_b, [_comp()])
    rows = db.execute("SELECT DISTINCT comic_id FROM comps").fetchall()
    assert {r["comic_id"] for r in rows} == {comic_a, comic_b}
    assert db.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 2


def test_upsert_comps_deleting_comic_orphans_comps_not_deletes(db):
    """Deleting a comics row leaves its comps present with comic_id NULL —
    ON DELETE SET NULL, not CASCADE: a bad comics row's deletion must not
    delete market facts."""
    comic_id = _comic(db)
    upsert_comps(db, comic_id, [_comp()])
    db.execute("DELETE FROM comics WHERE id=?", (comic_id,))
    db.commit()
    row = db.execute("SELECT * FROM comps").fetchone()
    assert row is not None
    assert row["comic_id"] is None


# ---------------------------------------------------------------------------
# POST /api/comics/comps (route layer)
# ---------------------------------------------------------------------------


def _create_comic(api, **overrides) -> int:
    body = {"title": "Amazing Spider-Man", "issue": "1", "year": 1963}
    body.update(overrides)
    return api.post("/api/comics", json=body).json()["comic_id"]


def _ledger(api) -> list[dict]:
    return api.get("/api/comics/health/rejections?hours=24").json()["rejections"]


def test_ingest_comps_returns_counts(api):
    comic_id = _create_comic(api)
    r = api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": [_comp()]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "comic_id": comic_id, "inserted": 1, "updated": 0, "conflicts": 0,
    }


def test_ingest_comps_persists_to_the_real_db(api):
    db_path = os.environ["DB_PATH"]
    comic_id = _create_comic(api)
    api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": [_comp(product_id="999")]},
    )
    raw = sqlite3.connect(db_path)
    row = raw.execute(
        "SELECT comic_id, provider, product_id, price FROM comps "
        "WHERE product_id='999'"
    ).fetchone()
    raw.close()
    assert row == (comic_id, "sold-comps.com", "999", 500.0)


def test_ingest_comps_accepts_null_comic_id(api):
    r = api.post(
        "/api/comics/comps",
        json={"comic_id": None, "comps": [_comp()]},
    )
    assert r.status_code == 200
    assert r.json()["comic_id"] is None


def test_ingest_comps_raw_and_slab_together(api):
    """U3's shape: one call posts both pools for one book, distinguished
    per-comp by `pool`."""
    comic_id = _create_comic(api)
    r = api.post(
        "/api/comics/comps",
        json={
            "comic_id": comic_id,
            "comps": [_comp(pool="raw", product_id="raw1"),
                      _comp(pool="slab", product_id="slab1")],
        },
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2


def test_ingest_comps_unknown_pool_422s_and_writes_nothing(api):
    comic_id = _create_comic(api)
    r = api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": [_comp(pool="bogus")]},
    )
    assert r.status_code == 422
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    count = raw.execute("SELECT COUNT(*) FROM comps").fetchone()[0]
    raw.close()
    assert count == 0


def test_ingest_comps_unknown_provenance_422s_and_writes_nothing(api):
    comic_id = _create_comic(api)
    r = api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": [_comp(provenance="bogus")]},
    )
    assert r.status_code == 422
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    count = raw.execute("SELECT COUNT(*) FROM comps").fetchone()[0]
    raw.close()
    assert count == 0


def test_ingest_comps_rejection_lands_in_rejected_writes_ledger(api):
    """LedgerRoute persists the refusal for free — no new code in this
    endpoint had to ask for it."""
    comic_id = _create_comic(api)
    api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": [_comp(pool="bogus")]},
    )
    rows = [r for r in _ledger(api) if r["path"] == "/api/comics/comps"]
    assert len(rows) == 1
    assert rows[0]["status"] == 422


def test_ingest_comps_empty_list_is_rejected(api):
    comic_id = _create_comic(api)
    r = api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": []},
    )
    assert r.status_code == 422


def test_ingest_comps_missing_required_field_422s(api):
    comic_id = _create_comic(api)
    comp = _comp()
    del comp["provider"]
    r = api.post(
        "/api/comics/comps",
        json={"comic_id": comic_id, "comps": [comp]},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/comics/comps (BUI-662) — the read side of this same table
# ---------------------------------------------------------------------------
#
# Shares the identity-resolution and 400-vs-200-empty-list contract with
# GET /api/comics/fmv-history — see tests/test_fmv_history.py for that
# endpoint's own tests and the scope-boundary contract test both endpoints
# share (never called from the pricing path).


def test_get_comps_by_comic_id(api):
    comic_id = _create_comic(api)
    api.post("/api/comics/comps", json={"comic_id": comic_id, "comps": [_comp()]})
    r = api.get("/api/comics/comps", params={"comic_id": comic_id})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["product_id"] == "1234567890"


def test_get_comps_by_title_issue_year(api):
    comic_id = _create_comic(api, title="Daredevil", issue="1", year=1964)
    api.post("/api/comics/comps", json={"comic_id": comic_id, "comps": [_comp()]})
    r = api.get("/api/comics/comps", params={
        "title": "Daredevil", "issue": "1", "year": 1964,
    })
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_get_comps_unresolvable_comic_id_400s(api):
    r = api.get("/api/comics/comps", params={"comic_id": 999999})
    assert r.status_code == 400


def test_get_comps_unresolvable_title_issue_400s(api):
    r = api.get("/api/comics/comps", params={
        "title": "Nonexistent Series", "issue": "1",
    })
    assert r.status_code == 400


def test_get_comps_no_identity_supplied_400s(api):
    r = api.get("/api/comics/comps")
    assert r.status_code == 400


def test_get_comps_known_book_with_no_comps_returns_empty_list(api):
    """A known book with no comps must 200 with [] — never 400. 'No comps'
    and 'wrong book' are distinguishable states."""
    comic_id = _create_comic(api)
    r = api.get("/api/comics/comps", params={"comic_id": comic_id})
    assert r.status_code == 200
    assert r.json() == []


def test_get_comps_newest_first(api):
    comic_id = _create_comic(api)
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [
            _comp(product_id="old", observed_at="2026-01-01T00:00:00Z"),
            _comp(product_id="new", observed_at="2026-07-01T00:00:00Z"),
        ],
    })
    r = api.get("/api/comics/comps", params={"comic_id": comic_id})
    rows = r.json()
    assert [row["product_id"] for row in rows] == ["new", "old"]


def test_get_comps_grade_filter_narrows(api):
    comic_id = _create_comic(api)
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [
            _comp(product_id="a", grade=9.4),
            _comp(product_id="b", grade=9.8),
        ],
    })
    r = api.get("/api/comics/comps", params={"comic_id": comic_id, "grade": 9.4})
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["product_id"] == "a"


def test_get_comps_pool_filter_narrows(api):
    comic_id = _create_comic(api)
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [
            _comp(product_id="raw1", pool="raw"),
            _comp(product_id="slab1", pool="slab"),
        ],
    })
    r = api.get("/api/comics/comps", params={"comic_id": comic_id, "pool": "slab"})
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["product_id"] == "slab1"


def test_get_comps_provider_filter_narrows(api):
    comic_id = _create_comic(api)
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [
            _comp(product_id="p1", provider="sold-comps.com"),
            _comp(product_id="p2", provider="serpapi"),
        ],
    })
    r = api.get("/api/comics/comps", params={
        "comic_id": comic_id, "provider": "serpapi",
    })
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["product_id"] == "p2"


def test_get_comps_days_filter_uses_observed_at_not_first_seen_at(api):
    """A comp whose row is old (first_seen_at backdated) but whose sale is
    fresh (observed_at recent) must pass a tight `days` filter — staleness
    here is about the market data's age, not the ledger row's age."""
    comic_id = _create_comic(api)
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(days=365)).isoformat()
    recent_iso = (now - timedelta(hours=1)).isoformat()
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [_comp(product_id="old-sale", observed_at=old_iso)],
    })
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [_comp(product_id="recent-sale", observed_at=recent_iso)],
    })
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    # Backdate first_seen_at on the recent sale, so its ROW looks old even
    # though its underlying SALE (observed_at) is fresh.
    raw.execute(
        "UPDATE comps SET first_seen_at=? WHERE product_id='recent-sale'",
        (old_iso,),
    )
    raw.commit()
    raw.close()

    r = api.get("/api/comics/comps", params={"comic_id": comic_id, "days": 30})
    rows = r.json()
    # A 30-day window on observed_at admits only the recent sale, even though
    # its row's first_seen_at was just backdated to look old.
    assert {row["product_id"] for row in rows} == {"recent-sale"}


def test_get_comps_limit_respected(api):
    comic_id = _create_comic(api)
    api.post("/api/comics/comps", json={
        "comic_id": comic_id,
        "comps": [_comp(product_id=str(i)) for i in range(5)],
    })
    r = api.get("/api/comics/comps", params={"comic_id": comic_id, "limit": 2})
    assert len(r.json()) == 2
