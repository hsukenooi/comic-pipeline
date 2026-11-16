"""BUI-1415: the purchase record and the offer log.

DB layer (idempotent migration on an existing DB, idempotent upsert, the
read-time refinement of purchase type and collection status, monthly spend)
and the routes against the real server with the real plugin loaded.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import create_tables
from gixen_overlay.purchases import (
    insert_offer,
    list_purchases,
    monthly_spend,
    upsert_purchases,
)


def _purchase(line="111111111111-1", item="111111111111", **kw):
    row = {
        "order_line_item_id": line, "order_id": "O1", "item_id": item,
        "transaction_id": line.split("-")[1], "title": "X-Men #1 CGC 9.0",
        "seller": "s1", "purchased_at": "2026-11-03T09:14:26.000Z",
        "quantity": 1, "price": 200.0, "shipping": 10.0, "tax": 15.0,
        "total": 225.0, "currency": "USD", "purchase_type": "unknown",
        "cancelled": False,
    }
    row.update(kw)
    return row


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
        "max_bid REAL NOT NULL, fmv_id INTEGER, status TEXT)"
    )
    create_tables(conn)
    yield conn
    conn.close()


def test_migration_is_idempotent_and_keeps_rows(tmp_path):
    path = tmp_path / "existing.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
        "max_bid REAL NOT NULL, fmv_id INTEGER)"
    )
    create_tables(conn)
    upsert_purchases(conn, [_purchase()])
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    create_tables(conn)  # second boot on the same DB
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1
    # The stub bids table has no status column: the refinement degrades.
    assert list_purchases(conn)[0]["purchase_type"] == "unknown"


def test_upsert_is_idempotent_and_keeps_user_state(db):
    assert upsert_purchases(db, [_purchase()]) == {"inserted": 1, "updated": 0}
    db.execute("UPDATE purchases SET collection_status = 'skipped', purchase_type = 'buy_it_now'")
    assert upsert_purchases(db, [_purchase(total=230.0)]) == {"inserted": 0, "updated": 1}
    row = db.execute("SELECT * FROM purchases").fetchone()
    assert row["total"] == 230.0
    assert row["collection_status"] == "skipped"
    assert row["purchase_type"] == "buy_it_now"  # unknown never downgrades a known type
    assert db.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1


def test_refinement_from_bids_offers_and_seen_set(db):
    upsert_purchases(db, [
        _purchase("100000000001-1", "100000000001"),
        _purchase("100000000002-1", "100000000002"),
        _purchase("100000000003-1", "100000000003"),
        _purchase("100000000004-1", "100000000004"),
    ])
    db.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('100000000001', 1, 'WON')")
    db.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('100000000004', 1, 'LOST')")
    db.execute("INSERT INTO collection_wins_seen (item_id) VALUES ('100000000003')")
    insert_offer(db, {"item_id": "100000000002", "offer": 150.0, "outcome": "accepted"})
    by_item = {r["item_id"]: r for r in list_purchases(db)}
    assert by_item["100000000001"]["purchase_type"] == "auction"
    assert by_item["100000000001"]["collection_status"] == "snipe_win"
    assert by_item["100000000002"]["purchase_type"] == "best_offer"
    assert by_item["100000000002"]["collection_status"] == "pending"
    assert by_item["100000000003"]["collection_status"] == "recorded"
    assert by_item["100000000004"]["purchase_type"] == "unknown"  # a LOST bid is not a win
    pending = list_purchases(db, collection_status="pending")
    assert {r["item_id"] for r in pending} == {"100000000002", "100000000004"}


def test_monthly_spend_excludes_cancelled(db):
    upsert_purchases(db, [
        _purchase("100000000001-1", "100000000001"),
        _purchase("100000000002-1", "100000000002", purchased_at="2026-11-20T00:00:00Z",
                  price=100.0, shipping=None, tax=None, total=100.0),
        _purchase("100000000003-1", "100000000003", cancelled=True),
        _purchase("100000000004-1", "100000000004", purchased_at="2026-10-01T00:00:00Z"),
        _purchase("100000000005-1", "100000000005", currency="GBP", total=50.0),
    ])
    spend = monthly_spend(db)
    assert [(m["month"], m["currency"]) for m in spend] == [
        ("2026-11", "GBP"), ("2026-11", "USD"), ("2026-10", "USD")]
    spend = [m for m in spend if m["currency"] == "USD"]
    assert spend[0] == {"month": "2026-11", "currency": "USD", "count": 2, "total": 325.0,
                        "price": 300.0, "shipping": 10.0, "tax": 15.0}
    assert {m["month"] for m in monthly_spend(db, months=1)} == {"2026-11"}
    assert len(list_purchases(db, month="2026-10")) == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_purchase_routes_round_trip(api):
    body = {"purchases": [_purchase(), _purchase("111111111112-2", "111111111112")]}
    assert api.post("/api/comics/purchases", json=body).json() == {"inserted": 2, "updated": 0}
    assert api.post("/api/comics/purchases", json=body).json() == {"inserted": 0, "updated": 2}
    got = api.get("/api/comics/purchases", params={"month": "2026-11"}).json()
    assert got["count"] == 2
    assert api.get("/api/comics/purchases/spend").json()["months"][0]["total"] == 450.0
    r = api.post("/api/comics/purchases/111111111112-2/collection-status",
                 json={"status": "skipped"})
    assert r.status_code == 200
    pending = api.get("/api/comics/purchases", params={"collection_status": "pending"}).json()
    assert [p["item_id"] for p in pending["purchases"]] == ["111111111111"]
    assert api.post("/api/comics/purchases/nope-1/collection-status",
                    json={"status": "skipped"}).status_code == 404


@pytest.mark.parametrize("bad", [
    {"item_id": "abc"}, {"price": -1}, {"purchase_type": "steal"},
    {"purchased_at": "yesterday"},
])
def test_purchase_validation(api, bad):
    r = api.post("/api/comics/purchases", json={"purchases": [_purchase(**bad)]})
    assert r.status_code == 422


def test_bad_month_is_422(api):
    assert api.get("/api/comics/purchases", params={"month": "2026-13"}).status_code == 422


def test_offer_routes(api):
    r = api.post("/api/comics/offers", json={
        "item_id": "123456789012", "seller": "s1", "ask": 300, "offer": 240,
    })
    assert r.status_code == 201
    offer = r.json()
    assert offer["outcome"] == "pending" and offer["source"] == "manual"
    r = api.patch(f"/api/comics/offers/{offer['id']}", json={
        "counteroffer": 270, "final_price": 265, "outcome": "accepted",
    })
    assert r.status_code == 200
    assert (r.json()["ask"], r.json()["counteroffer"], r.json()["final_price"]) == (300, 270, 265)
    listed = api.get("/api/comics/offers", params={"item_id": "123456789012"}).json()
    assert listed["count"] == 1 and listed["offers"][0]["outcome"] == "accepted"
    assert api.patch("/api/comics/offers/999", json={"outcome": "declined"}).status_code == 404
    assert api.patch(f"/api/comics/offers/{offer['id']}", json={"outcome": None}).status_code == 422
    assert api.post("/api/comics/offers", json={"item_id": "1234567", "offer": 0}).status_code == 422
