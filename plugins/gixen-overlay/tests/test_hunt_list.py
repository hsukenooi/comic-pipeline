"""BUI-1420: the hunt list tab (`/api/comics/hunt-list`, `/comics/hunt-list`)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from .conftest import _seeded_client


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    with _seeded_client(tmp_path, monkeypatch, store) as c:
        yield c


def _book(client, title, issue="1", year=2000, min_grade=6.0, ceiling=500.0, **kw):
    r = client.post("/api/comics", json={"title": title, "issue": issue, "year": year})
    assert r.status_code == 200, r.text
    cid = r.json()["comic_id"]
    if min_grade is not None or ceiling is not None:
        body = {"min_grade": min_grade, "ceiling": ceiling, **kw}
        r = client.post(f"/api/comics/{cid}/slab-watch", json=body)
        assert r.status_code == 200, r.text
    return cid


def _copy(client, cid, item, **kw):
    row = {"item_id": item, "comic_id": cid, "grade": 6.0, "market": 1200.0,
           "confidence": "MEDIUM", "last_seen_at": "2026-11-17T00:00:00+00:00", **kw}
    r = client.post("/api/comics/slab-alerts", json={"copies": [row]})
    assert r.status_code == 200, r.text


def _get(client):
    r = client.get("/api/comics/hunt-list")
    assert r.status_code == 200, r.text
    return r.json()


def test_shape_with_alerts(client):
    cid = _book(client, "Amazing Spider-Man", "23", 1965, 6.0, 900.0)
    _copy(client, cid, "100000000001", last_alerted_at="2026-11-16T10:00:00+00:00")
    _copy(client, cid, "100000000002", grade=7.0, market=1500.0, confidence="HIGH",
          last_alerted_at="2026-11-17T08:00:00+00:00")
    body = _get(client)
    assert body["count"] == 1
    b = body["books"][0]
    assert (b["title"], b["issue"], b["year"]) == ("Amazing Spider-Man", "23", 1965)
    assert b["min_grade"] == 6.0 and b["ceiling"] == 900.0
    assert b["live_copies"] == 2 and b["scanned"] is True
    assert b["last_alerted_at"] == "2026-11-17T08:00:00+00:00"
    # nearest grade at or above min_grade wins, labelled with its grade
    assert b["market"] == {"value": 1200.0, "confidence": "MEDIUM", "grade": 6.0}


def test_unscanned_book_and_empty_list(client):
    assert _get(client)["books"] == []
    _book(client, "Hulk", "181")
    b = _get(client)["books"][0]
    assert b["scanned"] is False and b["live_copies"] == 0
    assert b["last_alerted_at"] is None and b["market"] is None


def test_book_without_min_grade_or_ceiling_excluded(client):
    _book(client, "No Grade", min_grade=None, ceiling=500.0)
    _book(client, "No Ceiling", min_grade=6.0, ceiling=None)
    _book(client, "Hunted")
    assert [b["title"] for b in _get(client)["books"]] == ["Hunted"]


def test_ended_copies_not_counted(client):
    cid = _book(client, "X-Men", "94")
    _copy(client, cid, "100000000003")
    _copy(client, cid, "100000000004", ended_at="2026-11-16T00:00:00+00:00")
    assert _get(client)["books"][0]["live_copies"] == 1


def test_alerts_without_market(client):
    cid = _book(client, "Giant-Size X-Men", "1")
    _copy(client, cid, "100000000005", market=None, confidence=None)
    b = _get(client)["books"][0]
    assert b["scanned"] is True and b["market"] is None


def test_market_falls_back_below_min_grade_and_naive_timestamp_is_utc(client):
    cid = _book(client, "Fantastic Four", "48", min_grade=8.0)
    _copy(client, cid, "100000000006", grade=6.0, market=900.0,
          last_alerted_at="2026-11-17T08:00:00")
    b = _get(client)["books"][0]
    assert b["market"]["grade"] == 6.0
    assert b["last_alerted_at"] == "2026-11-17T08:00:00+00:00"


def test_sorted_by_recent_alert_then_title(client):
    a = _book(client, "Alpha", "1")
    z = _book(client, "Zeta", "1")
    m = _book(client, "Mu", "1")
    _book(client, "Beta", "1")
    _copy(client, z, "100000000007", last_alerted_at="2026-11-17T09:00:00+00:00")
    _copy(client, a, "100000000008", last_alerted_at="2026-11-16T09:00:00+00:00")
    _copy(client, m, "100000000009")
    assert [b["title"] for b in _get(client)["books"]] == ["Zeta", "Alpha", "Beta", "Mu"]


def test_spend_present_with_no_cap(client):
    now = datetime.now(timezone.utc).isoformat()
    r = client.post("/api/comics/purchases", json={"purchases": [{
        "order_line_item_id": "1-1", "order_id": "O1", "item_id": "111111111111",
        "transaction_id": "1", "title": "X-Men #1", "seller": "s",
        "purchased_at": now, "quantity": 1, "price": 200.0, "shipping": 10.0,
        "tax": 15.0, "total": 225.0, "currency": "USD",
        "purchase_type": "unknown", "cancelled": False}]})
    assert r.status_code == 200, r.text
    spend = _get(client)["spend"]
    assert spend["cap"] is None and spend["month"] == now[:7]
    assert spend["entries"][0]["total"] == 225.0


def test_spend_empty_month(client):
    spend = _get(client)["spend"]
    assert spend["cap"] is None and spend["entries"] == []


def test_page_and_tab(client):
    r = client.get("/comics/hunt-list")
    assert r.status_code == 200 and "hunt list" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert {"label": "hunt list", "path": "/comics/hunt-list"} in client.app.state.dashboard_tabs
