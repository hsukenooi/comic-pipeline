"""Tests for `fmv_accuracy_report` / GET /api/comics/accuracy (BUI-977).

A real-estate-style fixed-window accuracy report, distinct from
`calibration_report`: this one scores every eligible resolved auction's final
price against the FMV band midpoint it was actually bid against, with no
admit gate. The load-bearing rule under test throughout is "the band in
force when the bid was added" — the latest `fmv_history` snapshot at-or-
before `bids.added_at`, never the current `fmv` row unconditionally (which
would leak a book's own auction price back into the band it's being scored
against, since `get_first_party_outcomes` feeds resolved auctions into the
comp pool that prices later `fmv` rows).
"""
from __future__ import annotations

import os
import sqlite3

from gixen_overlay.db import create_tables, fmv_accuracy_report, upsert_comic, upsert_fmv

# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """In-memory DB with the minimal bids stub the plugin's FK chain expects,
    including `added_at` — the column this report's in-force-at-bid-time
    lookup is keyed on, absent from test_won_auctions_value.py's stub because
    that report doesn't need it.
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
            resolved_at    TEXT,
            notes          TEXT,
            added_at       TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _bid(conn, item_id, *, status="WON", winning_bid=100.0, prior_status=None,
         auction_end_at="2026-03-15 12:00:00", resolved_at=None,
         added_at="2026-03-01 00:00:00", notes=None) -> int:
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, prior_status, "
        "auction_end_at, resolved_at, notes, added_at) "
        "VALUES (?, 1.0, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, status, winning_bid, prior_status, auction_end_at, resolved_at,
         notes, added_at),
    )
    conn.commit()
    return cur.lastrowid


def _link(conn, bid_id, fmv_id, *, is_primary=True) -> None:
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
        (bid_id, fmv_id, 1 if is_primary else 0),
    )
    conn.commit()


def _comic_and_fmv(conn, *, title="X-Men", issue="1", year=1963, grade=9.0,
                    low=None, high=None, certifier=None, label=None) -> tuple[int, int]:
    comic_id = upsert_comic(conn, title, issue, year)
    fmv_id = upsert_fmv(conn, comic_id, grade=grade, low=low, high=high,
                         certifier=certifier, label=label)
    return comic_id, fmv_id


def _history(conn, comic_id, grade, *, low, high, recorded_at, certifier="none",
             label="universal", confidence=None, comps=None, source="upsert") -> int:
    cur = conn.execute(
        """
        INSERT INTO fmv_history (
            comic_id, grade, low, high, comps, confidence, flag_reason, notes,
            recorded_at, source, certifier, label
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
        """,
        (comic_id, grade, low, high, comps, confidence, recorded_at, source,
         certifier, label),
    )
    conn.commit()
    return cur.lastrowid


def _db():
    conn = _make_db()
    create_tables(conn)
    return conn


# ---------------------------------------------------------------------------
# Empty / shape
# ---------------------------------------------------------------------------


def test_empty_on_fresh_db():
    conn = _db()
    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0
    assert report["overall"]["share_within_10pct"] is None
    assert report["by_month"] == []
    assert report["band_source_counts"] == {}
    assert "rows" not in report


def test_include_rows_false_by_default_omits_rows_key():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert "rows" not in report


def test_include_rows_true_returns_expected_fields():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "900", winning_bid=125.0, notes="hello")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["bid_id"] == bid_id
    assert row["item_id"] == "900"
    assert row["price"] == 125.0
    assert row["low"] == 100.0
    assert row["high"] == 150.0
    assert row["band_source"] == "current_fmv"
    assert row["fmv_history_id"] is None
    assert row["notes"] == "hello"
    assert row["status"] == "WON"
    assert set(row) == {
        "bid_id", "item_id", "comic_id", "title", "issue", "grade", "certifier",
        "status", "price", "low", "high", "band_source", "fmv_history_id",
        "confidence", "comps", "notes", "ended",
    }


# ---------------------------------------------------------------------------
# Band-in-force-at-bid-time: the load-bearing rule
# ---------------------------------------------------------------------------


def test_falls_back_to_current_fmv_when_no_history_snapshot_exists():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["band_source"] == "current_fmv"
    assert row["low"] == 100.0 and row["high"] == 150.0
    assert report["band_source_counts"] == {"current_fmv": 1}


def test_uses_history_snapshot_in_force_when_bid_was_added():
    """The snapshot on file when the bid was placed, not the live `fmv` row
    (which may have moved since) and not a later snapshot, is what gets
    scored."""
    conn = _db()
    comic_id, fmv_id = _comic_and_fmv(conn, grade=9.0, high=999.0, low=900.0)
    _history(conn, comic_id, 9.0, low=100.0, high=150.0,
             recorded_at="2026-02-01T00:00:00.000000+00:00")
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-03-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["band_source"] == "history"
    assert row["low"] == 100.0 and row["high"] == 150.0


def test_never_leaks_a_snapshot_recorded_after_the_bid_was_added():
    """The leakage this report exists to avoid: a snapshot recorded AFTER the
    auction (which may already have folded this very auction's own price
    into the comp pool that produced it) must never be scored, even though
    it is the 'latest' snapshot on file."""
    conn = _db()
    comic_id, fmv_id = _comic_and_fmv(conn, grade=9.0, high=999.0, low=900.0)
    _history(conn, comic_id, 9.0, low=100.0, high=150.0,
             recorded_at="2026-02-01T00:00:00.000000+00:00")
    # A later snapshot, recorded after the bid — must be ignored.
    _history(conn, comic_id, 9.0, low=200.0, high=250.0,
             recorded_at="2026-04-01T00:00:00.000000+00:00")
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-03-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["low"] == 100.0 and row["high"] == 150.0


def test_picks_the_latest_in_force_snapshot_not_the_earliest():
    conn = _db()
    comic_id, fmv_id = _comic_and_fmv(conn, grade=9.0, high=999.0, low=900.0)
    _history(conn, comic_id, 9.0, low=50.0, high=75.0,
             recorded_at="2026-01-01T00:00:00.000000+00:00")
    _history(conn, comic_id, 9.0, low=100.0, high=150.0,
             recorded_at="2026-02-01T00:00:00.000000+00:00")
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-03-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["low"] == 100.0 and row["high"] == 150.0


def test_timestamp_format_normalization_is_required_for_the_match():
    """bids.added_at is 'YYYY-MM-DD HH:MM:SS' (space separator, no offset);
    fmv_history.recorded_at is ISO 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00' ('T'
    separator, offset-suffixed). A byte-for-byte comparison of the two
    formats is wrong: without normalizing, a 'T'-separated recorded_at sorts
    (and compares) differently than a space-separated added_at even for the
    same moment, and this snapshot — recorded the same second the bid was
    added — would wrongly fail `recorded_at <= added_at` and fall back to
    current_fmv instead of matching. This test fails if the normalization
    (`replace(added_at, ' ', 'T')` vs `substr(recorded_at, 1, 19)`) is
    removed from the query."""
    conn = _db()
    comic_id, fmv_id = _comic_and_fmv(conn, grade=9.0, high=999.0, low=900.0)
    _history(conn, comic_id, 9.0, low=100.0, high=150.0,
             recorded_at="2026-03-01T00:00:00.000000+00:00")
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-03-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["band_source"] == "history"
    assert row["low"] == 100.0 and row["high"] == 150.0


def test_history_snapshot_scoped_to_same_certifier_and_label():
    """A slab snapshot must never be picked as the band for a raw bid at the
    same grade, and vice versa — the certifier/label columns are part of the
    match key, same as the fmv row's own UNIQUE(comic_id, grade, certifier,
    label)."""
    conn = _db()
    comic_id, fmv_id = _comic_and_fmv(conn, grade=9.0, high=150.0, low=100.0,
                                       certifier="none", label="universal")
    _history(conn, comic_id, 9.0, low=500.0, high=600.0,
             recorded_at="2026-02-01T00:00:00.000000+00:00",
             certifier="cgc", label="universal")
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-03-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["band_source"] == "current_fmv"
    assert row["low"] == 100.0 and row["high"] == 150.0


def test_history_snapshot_with_null_high_is_ignored():
    conn = _db()
    comic_id, fmv_id = _comic_and_fmv(conn, grade=9.0, high=150.0, low=100.0)
    _history(conn, comic_id, 9.0, low=None, high=None,
             recorded_at="2026-02-01T00:00:00.000000+00:00")
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-03-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, include_rows=True)
    row = report["rows"][0]
    assert row["band_source"] == "current_fmv"


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_excludes_multi_comic_lot():
    """A lot's winning_bid prices every linked book at once, not one book —
    excluded outright, unlike get_first_party_outcomes which just picks the
    primary comic to represent the bid."""
    conn = _db()
    _, fmv1 = _comic_and_fmv(conn, title="A", high=150.0, low=100.0)
    _, fmv2 = _comic_and_fmv(conn, title="B", issue="2", high=250.0, low=200.0)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv1, is_primary=True)
    _link(conn, bid_id, fmv2, is_primary=False)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


def test_excludes_secondary_link_even_as_sole_junction():
    """Defensive: _PRIMARY_LINK_CLAUSE is still applied even though a sole
    junction is normally always primary by construction."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id, is_primary=False)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


def test_excludes_row_with_null_low():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=None)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


def test_excludes_row_with_null_high():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=None, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


def test_excludes_pending_bid():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", status="PENDING", winning_bid=None)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


def test_excludes_won_bid_with_null_winning_bid():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", status="ENDED", winning_bid=None)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


def test_removed_row_admitted_via_prior_status_won():
    """BUI-660: a purge-swept WON bid still counts via prior_status, same
    rule get_first_party_outcomes/calibration_report apply."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", status="REMOVED", winning_bid=125.0, prior_status="WON")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 1
    assert report["overall"]["by_status"]["WON"]["n"] == 1


def test_removed_row_with_null_prior_status_excluded():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", status="REMOVED", winning_bid=125.0, prior_status=None)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["n"] == 0


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def test_metrics_computed_against_midpoint():
    """low=100, high=150 -> mid=125.
    price=125 -> exact hit (0% error, within 10/20%, in-band, width 40%).
    price=140 -> +12% error (within 20% not 10%, in-band).
    price=200 -> +60% error (outside 20%, above band).
    price=50  -> -60% error (outside 20%, below band).
    """
    conn = _db()
    for item_id, price in [("1", 125.0), ("2", 140.0), ("3", 200.0), ("4", 50.0)]:
        _, fmv_id = _comic_and_fmv(conn, title=item_id, high=150.0, low=100.0)
        bid_id = _bid(conn, item_id, winning_bid=price)
        _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    overall = report["overall"]
    assert overall["n"] == 4
    assert overall["share_within_10pct"] == 25.0
    assert overall["share_within_20pct"] == 50.0
    assert overall["in_band_pct"] == 50.0
    assert overall["above_band_pct"] == 25.0
    assert overall["below_band_pct"] == 25.0
    assert overall["median_band_width_pct"] == 40.0
    # signed errors: 0, +0.12, +0.60, -0.60 -> mean = +0.03 -> 3%
    assert round(overall["mean_signed_error_pct"], 4) == 3.0
    # abs errors: 0, 0.12, 0.60, 0.60 -> median = 0.36 -> 36%
    assert round(overall["mdape_pct"], 4) == 36.0


def test_mean_signed_error_positive_means_priced_low():
    """A price that consistently clears ABOVE the midpoint yields a positive
    mean signed error — the band priced the book low relative to what it
    actually sold for."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)  # mid=125
    bid_id = _bid(conn, "1", winning_bid=200.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["mean_signed_error_pct"] > 0


def test_mean_signed_error_negative_means_priced_high():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)  # mid=125
    bid_id = _bid(conn, "1", winning_bid=50.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["overall"]["mean_signed_error_pct"] < 0


# ---------------------------------------------------------------------------
# WON/LOST split
# ---------------------------------------------------------------------------


def test_split_by_status():
    conn = _db()
    _, fmv1 = _comic_and_fmv(conn, title="A", high=150.0, low=100.0)
    _, fmv2 = _comic_and_fmv(conn, title="B", issue="2", high=150.0, low=100.0)
    won_id = _bid(conn, "1", status="WON", winning_bid=125.0)
    lost_id = _bid(conn, "2", status="LOST", winning_bid=140.0)
    _link(conn, won_id, fmv1)
    _link(conn, lost_id, fmv2)

    report = fmv_accuracy_report(conn)
    by_status = report["overall"]["by_status"]
    assert by_status["WON"]["n"] == 1
    assert by_status["LOST"]["n"] == 1
    assert report["overall"]["n"] == 2


# ---------------------------------------------------------------------------
# Per-month bucketing
# ---------------------------------------------------------------------------


def test_by_month_grouping():
    conn = _db()
    _, fmv1 = _comic_and_fmv(conn, title="A", high=150.0, low=100.0)
    _, fmv2 = _comic_and_fmv(conn, title="B", issue="2", high=150.0, low=100.0)
    _, fmv3 = _comic_and_fmv(conn, title="C", issue="3", high=150.0, low=100.0)
    b1 = _bid(conn, "1", winning_bid=125.0, auction_end_at="2026-02-10 00:00:00")
    b2 = _bid(conn, "2", winning_bid=130.0, auction_end_at="2026-02-20 00:00:00")
    b3 = _bid(conn, "3", winning_bid=135.0, auction_end_at="2026-03-05 00:00:00")
    _link(conn, b1, fmv1)
    _link(conn, b2, fmv2)
    _link(conn, b3, fmv3)

    report = fmv_accuracy_report(conn)
    months = {m["month"]: m["n"] for m in report["by_month"]}
    assert months == {"2026-02": 2, "2026-03": 1}
    # Sorted ascending.
    assert [m["month"] for m in report["by_month"]] == ["2026-02", "2026-03"]


def test_month_uses_resolved_at_when_auction_end_at_is_null():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0, auction_end_at=None,
                  resolved_at="2026-05-10 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert [m["month"] for m in report["by_month"]] == ["2026-05"]


# ---------------------------------------------------------------------------
# days filter
# ---------------------------------------------------------------------------


def test_days_none_includes_old_rows():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0,
                  auction_end_at="2020-01-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, days=None)
    assert report["overall"]["n"] == 1


def test_days_bounds_out_old_rows():
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0,
                  auction_end_at="2020-01-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn, days=30)
    assert report["overall"]["n"] == 0


# ---------------------------------------------------------------------------
# GET /api/comics/accuracy (route layer)
# ---------------------------------------------------------------------------


def test_route_empty_on_fresh_db(api):
    r = api.get("/api/comics/accuracy")
    assert r.status_code == 200
    body = r.json()
    assert body["overall"]["n"] == 0
    assert body["by_month"] == []
    assert "rows" not in body


def _create_comic_and_fmv(api, *, title, issue, year, grade, fmv_high=None, fmv_low=None):
    body = {"title": title, "issue": issue, "year": year, "grade": grade}
    if fmv_high is not None:
        body["fmv_high"] = fmv_high
    if fmv_low is not None:
        body["fmv_low"] = fmv_low
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


def test_route_scores_a_resolved_auction(api):
    db_path = os.environ["DB_PATH"]
    _, fmv_id = _create_comic_and_fmv(
        api, title="Uncanny X-Men", issue="141", year=1981, grade=9.2,
        fmv_high=150.0, fmv_low=100.0,
    )
    _add_resolved_bid(api, db_path, "900000001", fmv_id,
                       status="WON", winning_bid=125.0)

    r = api.get("/api/comics/accuracy")
    assert r.status_code == 200
    body = r.json()
    assert body["overall"]["n"] == 1
    assert body["overall"]["in_band_pct"] == 100.0


def test_route_include_rows_true(api):
    db_path = os.environ["DB_PATH"]
    _, fmv_id = _create_comic_and_fmv(
        api, title="Amazing Fantasy", issue="15", year=1962, grade=8.0,
        fmv_high=1000.0, fmv_low=800.0,
    )
    _add_resolved_bid(api, db_path, "900000002", fmv_id,
                       status="WON", winning_bid=900.0)

    r = api.get("/api/comics/accuracy?include_rows=true")
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["item_id"] == "900000002"


def test_route_excludes_lost_with_no_winning_bid(api):
    db_path = os.environ["DB_PATH"]
    _, fmv_id = _create_comic_and_fmv(
        api, title="Daredevil", issue="1", year=1964, grade=7.0,
        fmv_high=400.0, fmv_low=300.0,
    )
    api.post("/api/bids", json={"item_id": "900000003", "max_bid": 100.0})
    _link_bid_to_fmv(db_path, "900000003", fmv_id)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET status='ENDED', winning_bid=NULL, "
        "auction_end_at=datetime('now') WHERE item_id=?",
        ("900000003",),
    )
    raw.commit()
    raw.close()

    r = api.get("/api/comics/accuracy")
    assert r.json()["overall"]["n"] == 0


# ---------------------------------------------------------------------------
# Band-width slice + pre/post BUI-528 split (BUI-983)
# ---------------------------------------------------------------------------


def test_zero_width_band_lands_in_zero_bucket():
    """low == high (the legacy shape BUI-528 stopped producing) must land in
    its own 'zero' bucket, not get folded into 'under_30pct' just because its
    width ratio also happens to be under 30%."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=50.0, low=50.0)
    bid_id = _bid(conn, "1", winning_bid=50.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    by_width = report["by_width_bucket"]
    assert by_width["zero"]["n"] == 1
    assert by_width["under_30pct"]["n"] == 0
    assert by_width["30_50pct"]["n"] == 0
    assert by_width["50pct_plus"]["n"] == 0


def test_width_buckets_split_by_ratio():
    """Four rows, one per bucket, at the exact boundary values: 0.30 and 0.50
    are each pushed into the HIGHER bucket (half-open [lo, hi) on the low
    side), matching the audit's 'under 30%' / '30-50%' / '50%+' labels."""
    conn = _db()
    # under_30pct: mid=112.5, width=25/112.5 ~= 22.2%
    _, fmv1 = _comic_and_fmv(conn, title="A", high=125.0, low=100.0)
    # exactly 30% wide (mid=100, width=30/100=0.30) -> falls into 30_50pct
    _, fmv2 = _comic_and_fmv(conn, title="B", issue="2", high=115.0, low=85.0)
    # exactly 50% wide (mid=100, width=50/100=0.50) -> falls into 50pct_plus
    _, fmv3 = _comic_and_fmv(conn, title="C", issue="3", high=125.0, low=75.0)
    # comfortably 50%+: mid=150, width=100/150 ~= 66.7%
    _, fmv4 = _comic_and_fmv(conn, title="D", issue="4", high=200.0, low=100.0)
    for i, fmv_id in enumerate([fmv1, fmv2, fmv3, fmv4], start=1):
        bid_id = _bid(conn, str(i), winning_bid=100.0)
        _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    by_width = report["by_width_bucket"]
    assert by_width["zero"]["n"] == 0
    assert by_width["under_30pct"]["n"] == 1
    assert by_width["30_50pct"]["n"] == 1
    assert by_width["50pct_plus"]["n"] == 2
    # Additive: the overall population is untouched by the new slice.
    assert report["overall"]["n"] == 4


def test_prepost_bui528_split_by_history_recorded_at_boundary():
    """A history row recorded exactly at the cutoff is 'post'; one recorded a
    microsecond before is 'pre' — proves the boundary is >= (inclusive of the
    cutoff instant), not a same-day approximation."""
    conn = _db()
    comic_pre, fmv_pre = _comic_and_fmv(conn, title="Pre", grade=9.0,
                                         high=999.0, low=900.0)
    _history(conn, comic_pre, 9.0, low=100.0, high=150.0,
             recorded_at="2026-07-24T23:59:59.999999+00:00")
    bid_pre = _bid(conn, "1", winning_bid=125.0, added_at="2026-08-01 00:00:00")
    _link(conn, bid_pre, fmv_pre)

    comic_post, fmv_post = _comic_and_fmv(conn, title="Post", grade=9.0,
                                           high=999.0, low=900.0)
    _history(conn, comic_post, 9.0, low=100.0, high=150.0,
             recorded_at="2026-07-25T00:00:00+00:00")
    bid_post = _bid(conn, "2", winning_bid=125.0, added_at="2026-08-01 00:00:00")
    _link(conn, bid_post, fmv_post)

    report = fmv_accuracy_report(conn)
    by_prepost = report["by_prepost_bui528"]
    assert by_prepost["pre_bui_528"]["n"] == 1
    assert by_prepost["post_bui_528"]["n"] == 1
    assert by_prepost["unknown"]["n"] == 0
    assert report["overall"]["n"] == 2


def test_prepost_bui528_uses_fmv_updated_at_for_current_fmv_rows():
    """A current_fmv-fallback row (no history snapshot in force) is dated by
    the live `fmv` row's own `updated_at`, not its auction's `added_at`."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    conn.execute("UPDATE fmv SET updated_at=? WHERE id=?",
                 ("2026-06-01T00:00:00.000000+00:00", fmv_id))
    conn.commit()
    bid_id = _bid(conn, "1", winning_bid=125.0, added_at="2026-08-01 00:00:00")
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert report["by_prepost_bui528"]["pre_bui_528"]["n"] == 1
    assert report["by_prepost_bui528"]["post_bui_528"]["n"] == 0


def test_prepost_bui528_null_timestamp_goes_to_unknown_not_dropped():
    """Defensive: a `current_fmv` row can in principle carry a real price
    with a NULL `updated_at` (a row written by some path that never stamped
    one). It must land in the explicit 'unknown' bucket — never silently
    dropped, and never guessed into pre/post."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    conn.execute("UPDATE fmv SET updated_at=NULL WHERE id=?", (fmv_id,))
    conn.commit()
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    by_prepost = report["by_prepost_bui528"]
    assert by_prepost["unknown"]["n"] == 1
    assert by_prepost["pre_bui_528"]["n"] == 0
    assert by_prepost["post_bui_528"]["n"] == 0
    # Never silently dropped from the overall population either.
    assert report["overall"]["n"] == 1


def test_new_slices_are_additive_existing_keys_unchanged():
    """BUI-983 is additive-only: the pre-existing top-level keys and
    `overall`'s own key set must not change shape."""
    conn = _db()
    _, fmv_id = _comic_and_fmv(conn, high=150.0, low=100.0)
    bid_id = _bid(conn, "1", winning_bid=125.0)
    _link(conn, bid_id, fmv_id)

    report = fmv_accuracy_report(conn)
    assert set(report) == {
        "days", "overall", "by_month", "band_source_counts",
        "by_width_bucket", "by_prepost_bui528",
    }
    assert set(report["overall"]) == {
        "n", "share_within_10pct", "share_within_20pct", "mdape_pct",
        "mean_signed_error_pct", "in_band_pct", "above_band_pct",
        "below_band_pct", "median_band_width_pct", "by_status",
    }
    assert set(report["by_width_bucket"]) == {
        "zero", "under_30pct", "30_50pct", "50pct_plus",
    }
    assert set(report["by_prepost_bui528"]) == {
        "pre_bui_528", "post_bui_528", "unknown",
    }


def test_width_and_prepost_slices_present_and_empty_on_fresh_db():
    """Both new slices must appear with n=0 (not be absent) when there is no
    data at all — matches the existing empty-db contract for the other
    slices."""
    conn = _db()
    report = fmv_accuracy_report(conn)
    for bucket in ("zero", "under_30pct", "30_50pct", "50pct_plus"):
        assert report["by_width_bucket"][bucket]["n"] == 0
        assert report["by_width_bucket"][bucket]["share_within_10pct"] is None
    for bucket in ("pre_bui_528", "post_bui_528", "unknown"):
        assert report["by_prepost_bui528"][bucket]["n"] == 0
