"""BUI-1419: the slab-alert seen-set and Act/Pass ratings.

DB layer (fresh schema, a pre-migration DB, idempotent upsert that only
changes the keys sent) and the routes against the real server.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import create_tables
from gixen_overlay.purchases import upsert_purchases
from gixen_overlay.slab_alerts import list_slab_alerts, record_ratings, upsert_slab_alerts

_BIDS = ("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
         "max_bid REAL NOT NULL, fmv_id INTEGER, status TEXT)")


def _copy(item="123456789012", **kw):
    row = {"item_id": item, "comic_id": 7, "title": "ASM #23 CGC 6.0", "grade": 6.0,
           "listing_type": "BIN", "best_offer": False, "price": 900.0,
           "shipping": 10.0, "expected": 982.0, "market": 1200.0,
           "confidence": "MEDIUM", "last_seen_at": "2026-11-17T00:00:00+00:00"}
    row.update(kw)
    return row


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_BIDS)
    create_tables(conn)
    yield conn
    conn.close()


def test_fresh_schema_has_table(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(slab_alerts)")}
    assert {"item_id", "comic_id", "first_seen_at", "ended_at", "last_alerted_at",
            "rating", "rated_at", "alerted_expected"} <= cols


def test_pre_migration_db_gains_table_and_keeps_rows(tmp_path):
    path = tmp_path / "pre.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_BIDS)
    create_tables(conn)
    conn.execute("DROP TABLE slab_alerts")  # the DB as it was before BUI-1419
    upsert_purchases(conn, [{
        "order_line_item_id": "111111111111-1", "item_id": "111111111111",
        "purchased_at": "2026-11-03T00:00:00Z", "price": 1.0, "total": 1.0,
        "purchase_type": "unknown", "cancelled": False}])
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    create_tables(conn)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1
    upsert_slab_alerts(conn, [_copy()])
    assert conn.execute("SELECT COUNT(*) FROM slab_alerts").fetchone()[0] == 1
    conn.close()


def test_pre_bui1449_table_gains_drop_columns_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_BIDS)
    create_tables(conn)
    upsert_slab_alerts(conn, [_copy()])
    # Rebuild the table in its BUI-1419 shape, without the three drop columns.
    for col in ("dropped_reason", "dropped_at", "dropped_price"):
        conn.execute(f"ALTER TABLE slab_alerts DROP COLUMN {col}")
    conn.commit()
    assert "dropped_price" not in {r[1] for r in conn.execute("PRAGMA table_info(slab_alerts)")}
    create_tables(conn)
    create_tables(conn)  # idempotent
    cols = {r[1] for r in conn.execute("PRAGMA table_info(slab_alerts)")}
    assert {"dropped_reason", "dropped_at", "dropped_price"} <= cols
    (row,) = list_slab_alerts(conn)
    assert row["item_id"] == "123456789012" and row["dropped_reason"] is None
    conn.close()


def test_upsert_idempotent_and_partial(db):
    assert upsert_slab_alerts(db, [_copy()]) == {"inserted": 1, "updated": 0}
    assert upsert_slab_alerts(db, [_copy()]) == {"inserted": 0, "updated": 1}
    upsert_slab_alerts(db, [{"item_id": "123456789012",
                             "last_alerted_at": "2026-11-17T01:00:00+00:00",
                             "alerted_expected": 982.0, "alerted_best_offer": False,
                             "alerted_last_day": False}])
    # An observation-only write never clears the alert state or first_seen_at.
    upsert_slab_alerts(db, [_copy(price=850.0, last_seen_at="2026-11-18T00:00:00+00:00")])
    (row,) = list_slab_alerts(db)
    assert row["price"] == 850.0 and row["alert_count"] == 1
    assert row["alerted_expected"] == 982.0
    assert row["first_seen_at"] == "2026-11-17T00:00:00+00:00"
    assert row["best_offer"] == 0


def test_insert_needs_comic_id(db):
    with pytest.raises(ValueError):
        upsert_slab_alerts(db, [{"item_id": "123456789012"}])


def test_ratings_idempotent_and_unknown(db):
    upsert_slab_alerts(db, [_copy()])
    r = {"item_id": "123456789012", "rating": "act"}
    assert record_ratings(db, [r]) == {"recorded": 1, "unknown": []}
    assert record_ratings(db, [r, {"item_id": "999999999", "rating": "pass"}]) == {
        "recorded": 1, "unknown": ["999999999"]}
    assert list_slab_alerts(db)[0]["rating"] == "act"
    with pytest.raises(sqlite3.IntegrityError):
        record_ratings(db, [{"item_id": "123456789012", "rating": "maybe"}])


def test_filters(db):
    upsert_slab_alerts(db, [_copy(), _copy("123456789013", ended_at="2026-11-17T05:00:00Z"),
                            _copy("123456789014", comic_id=8,
                                  last_alerted_at="2026-11-17T05:00:00Z")])
    assert len(list_slab_alerts(db, open_only=True)) == 2
    assert [r["item_id"] for r in list_slab_alerts(db, alerted_only=True)] == ["123456789014"]
    assert len(list_slab_alerts(db, comic_id=7)) == 2


def test_routes_round_trip(api):
    body = {"copies": [_copy(), _copy("123456789013")]}
    assert api.post("/api/comics/slab-alerts", json=body).json() == {"inserted": 2, "updated": 0}
    assert api.post("/api/comics/slab-alerts", json=body).json() == {"inserted": 0, "updated": 2}
    r = api.post("/api/comics/slab-alerts", json={"copies": [
        {"item_id": "123456789013", "ended_at": "2026-11-17T09:00:00Z"}]})
    assert r.status_code == 200
    got = api.get("/api/comics/slab-alerts", params={"open": "true"}).json()
    assert [c["item_id"] for c in got["copies"]] == ["123456789012"]
    got = api.get("/api/comics/slab-alerts").json()
    assert {c["item_id"]: c["price"] for c in got["copies"]} == {
        "123456789012": 900.0, "123456789013": 900.0}
    r = api.post("/api/comics/slab-alerts/ratings",
                 json={"ratings": [{"item_id": "123456789012", "rating": "pass"}]})
    assert r.json() == {"recorded": 1, "unknown": []}


@pytest.mark.parametrize("bad", [
    {"item_id": "abc"}, {"grade": 11}, {"price": -1}, {"last_seen_at": "today"},
])
def test_route_validation(api, bad):
    assert api.post("/api/comics/slab-alerts",
                    json={"copies": [_copy(**bad)]}).status_code == 422


def test_route_new_item_without_comic_id_is_422(api):
    r = api.post("/api/comics/slab-alerts", json={"copies": [{"item_id": "123456789012"}]})
    assert r.status_code == 422
    r = api.post("/api/comics/slab-alerts/ratings",
                 json={"ratings": [{"item_id": "123456789012", "rating": "meh"}]})
    assert r.status_code == 422


def test_routes_drop_stamp_round_trip_and_clear(api):
    api.post("/api/comics/slab-alerts", json={"copies": [_copy()]})
    stamp = {"item_id": "123456789012", "dropped_reason": "grade_mismatch",
             "dropped_at": "2026-11-17T01:00:00+00:00", "dropped_price": 900.0}
    assert api.post("/api/comics/slab-alerts", json={"copies": [stamp]}).status_code == 200
    # An observation-only write leaves the stamp alone.
    api.post("/api/comics/slab-alerts", json={"copies": [_copy(price=900.0)]})
    (row,) = api.get("/api/comics/slab-alerts").json()["copies"]
    assert (row["dropped_reason"], row["dropped_price"]) == ("grade_mismatch", 900.0)
    api.post("/api/comics/slab-alerts", json={"copies": [
        {"item_id": "123456789012", "dropped_reason": None}]})
    (row,) = api.get("/api/comics/slab-alerts").json()["copies"]
    assert row["dropped_reason"] is None
    bad = dict(stamp, dropped_at="yesterday")
    assert api.post("/api/comics/slab-alerts", json={"copies": [bad]}).status_code == 422
