"""Tests for GET /api/comics/won-auctions/value (BUI-664).

BUI-664 was deferred from the comps-data-flywheel plan specifically because
a full "portfolio" view (cost basis vs. current FMV across the WHOLE
collection) would mislead: measured on the live Mini 2026-08-03, only 172 of
2,191 owned collection rows carry both a cost basis (a WON Gixen bid) and a
linked, priced FMV. The ticket's own resolution was to ship the narrower
thing — cost basis for auctions we actually won through Gixen — and to name
it so it cannot read as full-collection coverage. These tests pin both
halves of that: the query's WON-only scope (and its BUI-660 tombstone
correctness), and the naming itself — the word "portfolio" must never appear
anywhere in this endpoint's response.
"""
from __future__ import annotations

import os
import sqlite3

from gixen_overlay.db import create_tables, get_won_auctions_cost_basis, upsert_comic, upsert_fmv

# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """In-memory DB with the minimal bids stub the plugin's FK chain expects,
    now including the columns get_won_auctions_cost_basis reads directly
    (status/winning_bid/prior_status/auction_end_at/resolved_at) — mirrors
    the shape test_gixen_overlay_routes.py's bids table already has via the
    real host schema, hand-rolled here since this file uses raw sqlite3
    rather than the real server app for its DB-layer tests.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE bids (
            id             INTEGER PRIMARY KEY,
            item_id        TEXT NOT NULL,
            max_bid        REAL NOT NULL,
            fmv_id         INTEGER,
            status         TEXT,
            prior_status   TEXT,
            winning_bid    REAL,
            auction_end_at TEXT,
            resolved_at    TEXT
        )
    """)
    conn.commit()
    return conn


def _bid(conn, item_id, *, status=None, winning_bid=None, prior_status=None,
         auction_end_at=None) -> int:
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, "
        "prior_status, auction_end_at) VALUES (?, 1.0, ?, ?, ?, ?)",
        (item_id, status, winning_bid, prior_status, auction_end_at),
    )
    conn.commit()
    return cur.lastrowid


def _link(conn, bid_id, fmv_id, *, is_primary=True) -> None:
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
        (bid_id, fmv_id, 1 if is_primary else 0),
    )
    conn.commit()


def _comic_and_fmv(conn, *, title="X-Men", issue="1", year=1963,
                    grade=9.0, low=None, high=None) -> tuple[int, int]:
    comic_id = upsert_comic(conn, title, issue, year)
    fmv_id = upsert_fmv(conn, comic_id, grade=grade, low=low, high=high)
    return comic_id, fmv_id


# ---------------------------------------------------------------------------
# get_won_auctions_cost_basis (db layer)
# ---------------------------------------------------------------------------


def test_empty_on_fresh_db():
    conn = _make_db()
    create_tables(conn)
    assert get_won_auctions_cost_basis(conn) == []


def test_won_bid_with_priced_primary_fmv_is_included():
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="WON", winning_bid=100.0)
    _link(conn, bid_id, fmv_id)

    rows = get_won_auctions_cost_basis(conn)
    assert len(rows) == 1
    assert rows[0]["item_id"] == "1"
    assert rows[0]["cost_basis"] == 100.0
    assert rows[0]["current_fmv_high"] == 150.0
    assert rows[0]["unrealized_gain_loss"] == 50.0


def test_lost_bid_is_excluded():
    """A LOST auction has no purchase price by definition — nothing to
    compare a cost basis against."""
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="LOST", winning_bid=90.0)
    _link(conn, bid_id, fmv_id)

    assert get_won_auctions_cost_basis(conn) == []


def test_pending_bid_is_excluded():
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="PENDING")
    _link(conn, bid_id, fmv_id)

    assert get_won_auctions_cost_basis(conn) == []


def test_won_bid_with_null_winning_bid_is_excluded():
    """R3-style guard: no trustworthy price, no cost basis."""
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="WON", winning_bid=None)
    _link(conn, bid_id, fmv_id)

    assert get_won_auctions_cost_basis(conn) == []


def test_won_bid_with_unpriced_fmv_is_excluded():
    """A grade-only stub FMV (fmv.high NULL) has no current value to compare
    against — the row is excluded rather than reported with a null gain."""
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=None)
    bid_id = _bid(conn, "1", status="WON", winning_bid=100.0)
    _link(conn, bid_id, fmv_id)

    assert get_won_auctions_cost_basis(conn) == []


def test_secondary_lot_member_is_excluded():
    """A lot's winning_bid prices the whole lot, not one book — same
    _PRIMARY_LINK_CLAUSE rule get_first_party_outcomes already applies."""
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="WON", winning_bid=100.0)
    _link(conn, bid_id, fmv_id, is_primary=False)

    assert get_won_auctions_cost_basis(conn) == []


def test_removed_row_admitted_via_prior_status_won():
    """BUI-660: a WON bid later purge-swept to the REMOVED tombstone still
    counts via its prior_status — same rule get_first_party_outcomes uses."""
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="REMOVED", winning_bid=100.0,
                  prior_status="WON")
    _link(conn, bid_id, fmv_id)

    rows = get_won_auctions_cost_basis(conn)
    assert len(rows) == 1
    assert rows[0]["cost_basis"] == 100.0


def test_removed_row_with_lost_prior_status_excluded():
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="REMOVED", winning_bid=90.0,
                  prior_status="LOST")
    _link(conn, bid_id, fmv_id)

    assert get_won_auctions_cost_basis(conn) == []


def test_removed_row_with_null_prior_status_excluded():
    """AE9-style: a REMOVED row with no recoverable prior_status stays
    excluded — no history is fabricated for it."""
    conn = _make_db()
    create_tables(conn)
    _, fmv_id = _comic_and_fmv(conn, high=150.0)
    bid_id = _bid(conn, "1", status="REMOVED", winning_bid=90.0,
                  prior_status=None)
    _link(conn, bid_id, fmv_id)

    assert get_won_auctions_cost_basis(conn) == []


def test_sorted_by_unrealized_gain_loss_descending():
    conn = _make_db()
    create_tables(conn)
    _, fmv1 = _comic_and_fmv(conn, title="A", high=110.0)
    _, fmv2 = _comic_and_fmv(conn, title="B", issue="2", high=300.0)
    _, fmv3 = _comic_and_fmv(conn, title="C", issue="3", high=50.0)
    b1 = _bid(conn, "1", status="WON", winning_bid=100.0)  # +10
    b2 = _bid(conn, "2", status="WON", winning_bid=100.0)  # +200
    b3 = _bid(conn, "3", status="WON", winning_bid=100.0)  # -50
    _link(conn, b1, fmv1)
    _link(conn, b2, fmv2)
    _link(conn, b3, fmv3)

    rows = get_won_auctions_cost_basis(conn)
    assert [r["item_id"] for r in rows] == ["2", "1", "3"]
    assert [r["unrealized_gain_loss"] for r in rows] == [200.0, 10.0, -50.0]


# ---------------------------------------------------------------------------
# GET /api/comics/won-auctions/value (route layer)
# ---------------------------------------------------------------------------


def _create_comic_and_fmv(api, *, title, issue, year, grade, fmv_high=None):
    body = {"title": title, "issue": issue, "year": year, "grade": grade}
    if fmv_high is not None:
        body["fmv_high"] = fmv_high
    row = api.post("/api/comics", json=body).json()
    return row["comic_id"], row["fmv_id"]


def _link_bid_to_fmv(db_path, item_id, fmv_id, *, is_primary=True):
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    try:
        bid = raw.execute(
            "SELECT id FROM bids WHERE item_id=?", (item_id,)
        ).fetchone()
        if is_primary:
            raw.execute("UPDATE bid_fmvs SET is_primary=0 WHERE bid_id=?", (bid["id"],))
            raw.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid["id"]))
        raw.execute(
            "INSERT OR REPLACE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bid["id"], fmv_id, 1 if is_primary else 0),
        )
        raw.commit()
    finally:
        raw.close()


def _add_resolved_bid(api, db_path, item_id, fmv_id, *, status, winning_bid,
                       is_primary=True, days_ago=1):
    api.post("/api/bids", json={"item_id": item_id, "max_bid": winning_bid})
    _link_bid_to_fmv(db_path, item_id, fmv_id, is_primary=is_primary)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET status=?, winning_bid=?, "
        "auction_end_at=datetime('now', ?) WHERE item_id=?",
        (status, winning_bid, f"-{days_ago} days", item_id),
    )
    raw.commit()
    raw.close()


def test_route_empty_on_fresh_db(api):
    r = api.get("/api/comics/won-auctions/value")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["won_auctions"] == []


def test_route_returns_cost_basis_and_current_fmv(api):
    db_path = os.environ["DB_PATH"]
    _, fmv_id = _create_comic_and_fmv(
        api, title="Uncanny X-Men", issue="141", year=1981, grade=9.2,
        fmv_high=500.0,
    )
    _add_resolved_bid(api, db_path, "900000001", fmv_id,
                       status="WON", winning_bid=350.0)

    r = api.get("/api/comics/won-auctions/value")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    row = body["won_auctions"][0]
    assert row["item_id"] == "900000001"
    assert row["title"] == "Uncanny X-Men"
    assert row["cost_basis"] == 350.0
    assert row["current_fmv_high"] == 500.0
    assert row["unrealized_gain_loss"] == 150.0


def test_route_excludes_lost_bid(api):
    db_path = os.environ["DB_PATH"]
    _, fmv_id = _create_comic_and_fmv(
        api, title="Amazing Fantasy", issue="15", year=1962, grade=8.0,
        fmv_high=1000.0,
    )
    _add_resolved_bid(api, db_path, "900000002", fmv_id,
                       status="LOST", winning_bid=800.0)

    r = api.get("/api/comics/won-auctions/value")
    assert r.json()["count"] == 0


# --- naming: the deliverable this ticket exists to enforce -------------------


def test_route_response_never_says_portfolio(api):
    """BUI-664's own framing: naming this correctly was as much the
    deliverable as the query. 'Portfolio' implies full-collection coverage
    this endpoint deliberately does not have — it must never appear in the
    path, field names, or prose of the response."""
    db_path = os.environ["DB_PATH"]
    _, fmv_id = _create_comic_and_fmv(
        api, title="Daredevil", issue="1", year=1964, grade=7.0,
        fmv_high=400.0,
    )
    _add_resolved_bid(api, db_path, "900000003", fmv_id,
                       status="WON", winning_bid=300.0)

    r = api.get("/api/comics/won-auctions/value")
    assert "portfolio" not in r.text.lower()


def test_route_path_is_not_portfolio(api):
    assert api.get("/api/comics/portfolio").status_code == 404


def test_route_response_carries_a_coverage_note(api):
    """The narrowness must be legible in the response body itself, not just
    in documentation a caller might not read."""
    r = api.get("/api/comics/won-auctions/value")
    body = r.json()
    assert "coverage_note" in body
    assert "not" in body["coverage_note"].lower()
    assert "won_auctions" in body


def test_route_sorted_by_gain_loss_descending(api):
    db_path = os.environ["DB_PATH"]
    _, fmv1 = _create_comic_and_fmv(
        api, title="Book A", issue="1", year=2000, grade=9.0, fmv_high=110.0,
    )
    _, fmv2 = _create_comic_and_fmv(
        api, title="Book B", issue="1", year=2000, grade=9.0, fmv_high=300.0,
    )
    _add_resolved_bid(api, db_path, "900000010", fmv1,
                       status="WON", winning_bid=100.0)
    _add_resolved_bid(api, db_path, "900000011", fmv2,
                       status="WON", winning_bid=100.0)

    rows = api.get("/api/comics/won-auctions/value").json()["won_auctions"]
    assert [r["item_id"] for r in rows] == ["900000011", "900000010"]
