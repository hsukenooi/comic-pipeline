"""Integration tests for gixen-overlay plugin routes.

Uses the real gixen-cli server (server.main.app) with the real plugin
loaded via the entry-point discovery path.
"""
from __future__ import annotations

import os
import sqlite3
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _install_real_plugin(monkeypatch):
    """Wire the actual gixen-overlay plugin into gixen.plugins.entry_points."""
    ep = EntryPoint(
        name="gixen-overlay",
        value="gixen_overlay.plugin:plugin",
        group="gixen.plugins",
    )
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: [ep] if group == "gixen.plugins" else [],
    )


def _mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.modify_snipe.return_value = None
    m.remove_snipe.return_value = True
    m.purge_completed.return_value = None
    return m


@pytest.fixture
def api(tmp_path, monkeypatch):
    _install_real_plugin(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    mock = _mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock
            yield client


# ---------------------------------------------------------------------------
# GET /v2/comics
# ---------------------------------------------------------------------------


def test_comics_returns_html(api):
    r = api.get("/comics")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_comics_page_references_new_endpoints_and_dynamic_tabs(api):
    body = api.get("/comics").text
    assert "/api/comics/snipes" in body
    assert "/api/comics/history" in body
    assert "/api/dashboard-tabs" in body


def test_comics_page_has_no_stale_v2_comics_urls(api):
    body = api.get("/comics").text
    # Route was renamed from /v2/comics to /comics; nothing inside the page
    # should still point at the old URL.
    assert "/v2/comics" not in body


# ---------------------------------------------------------------------------
# GET /api/comics
# ---------------------------------------------------------------------------


def test_list_comics_empty_on_fresh_db(api):
    r = api.get("/api/comics")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# POST /api/comics
# ---------------------------------------------------------------------------


def test_upsert_comic_creates_comic(api):
    r = api.post("/api/comics", json={
        "title": "Amazing Spider-Man", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800.0, "fmv_high": 1000.0,
        "fmv_comps": 12, "fmv_confidence": "high", "fmv_notes": "Key issue",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["id"] > 0
    assert data["title"] == "Amazing Spider-Man"
    # POST returns the comics (identity) row; fmv data lives in the fmv table
    assert "fmv_confidence" not in data
    # Verify fmv was stored via GET /api/comics
    gr = api.get("/api/comics", params={"grade": 9.2})
    assert gr.status_code == 200
    rows = gr.json()
    assert len(rows) == 1
    assert rows[0]["fmv_confidence"] == "high"


def test_upsert_comic_twice_upserts(api):
    payload = {"title": "X-Men", "issue": "1", "year": 1963,
               "grade": 8.0, "fmv_low": 500.0, "fmv_high": 700.0,
               "fmv_comps": 5, "fmv_confidence": "medium", "fmv_notes": ""}
    r1 = api.post("/api/comics", json=payload)
    payload["fmv_low"] = 550.0
    r2 = api.post("/api/comics", json=payload)
    assert r1.json()["id"] == r2.json()["id"]
    # fmv_low is updated in the fmv table; verify via GET
    gr = api.get("/api/comics", params={"grade": 8.0})
    assert gr.status_code == 200
    assert gr.json()[0]["fmv_low"] == 550.0


def test_upsert_comic_without_grade_creates_no_fmv(api):
    r = api.post("/api/comics", json={"title": "X-Men", "issue": "1", "year": 1963})
    assert r.status_code == 200
    gr = api.get("/api/comics")
    assert gr.status_code == 200
    rows = gr.json()
    assert len(rows) == 1
    assert rows[0]["grade"] is None


def test_upsert_comic_missing_year_returns_422(api):
    r = api.post("/api/comics", json={"title": "X-Men", "issue": "1"})
    assert r.status_code == 422


def test_upsert_comic_invalid_confidence_returns_422(api):
    r = api.post("/api/comics", json={
        "title": "X-Men", "issue": "1", "year": 1963,
        "fmv_confidence": "very_high",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/extract-comics
# ---------------------------------------------------------------------------


def test_extract_comics_links_unlinked_bid(api):
    r = api.post("/api/bids", json={"item_id": "999000111", "max_bid": 50.0})
    assert r.status_code == 200
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Amazing Spider-Man #300 1988 NM", "999000111"))
    raw.commit()
    raw.close()

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 1
    assert body["linked"] == 1


def test_extract_comics_year_falls_back_to_locg(api, monkeypatch):
    """Title without a year resolves via the LOCG fallback and links cleanly."""
    from gixen_overlay import locg_lookup, routes
    from gixen_overlay.locg_lookup import LocgResolution

    api.post("/api/bids", json={"item_id": "999000113", "max_bid": 50.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Uncanny X-Men #211 (NM+) MARAUDERS WOLVERINE", "999000113"))
    raw.commit()
    raw.close()

    calls = []

    def fake_resolve(series, issue):
        calls.append((series, issue))
        return LocgResolution(year=1986, locg_id=12345, locg_variant_id=None)

    monkeypatch.setattr(routes, "resolve_year_and_locg", fake_resolve)

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] == 1
    assert body["skipped"] == []
    # Fallback was the path that resolved the year
    assert calls and calls[0][1] == "211"

    # Comic row carries the resolved year and locg_id
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    rows = raw.execute(
        "SELECT title, issue, year, locg_id FROM comics WHERE issue=?", ("211",)
    ).fetchall()
    raw.close()
    assert len(rows) == 1
    assert rows[0]["year"] == 1986
    assert rows[0]["locg_id"] == 12345


def test_extract_comics_year_fallback_failure_keeps_skip(api, monkeypatch):
    """When LOCG can't resolve, the bid stays skipped with an informative reason."""
    from gixen_overlay import routes

    api.post("/api/bids", json={"item_id": "999000114", "max_bid": 50.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Giant-Size Fantastic Four # 6 Fine Cond", "999000114"))
    raw.commit()
    raw.close()

    monkeypatch.setattr(routes, "resolve_year_and_locg", lambda *_: None)

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] == 0
    assert len(body["skipped"]) == 1
    assert "locg fallback failed" in body["skipped"][0]["reason"]


def test_extract_comics_does_not_call_locg_when_year_present(api, monkeypatch):
    """The LOCG fallback is only invoked when the title parser misses the year."""
    from gixen_overlay import routes

    api.post("/api/bids", json={"item_id": "999000115", "max_bid": 50.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Amazing Spider-Man #300 1988 NM", "999000115"))
    raw.commit()
    raw.close()

    def boom(*_a, **_kw):
        raise AssertionError("resolve_year_and_locg should not be called when year is parsed from title")

    monkeypatch.setattr(routes, "resolve_year_and_locg", boom)

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    assert r.json()["linked"] == 1


def test_extract_comics_idempotent(api):
    r = api.post("/api/bids", json={"item_id": "999000112", "max_bid": 50.0})
    assert r.status_code == 200
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("X-Men #1 1963 VF", "999000112"))
    raw.commit()
    raw.close()

    api.post("/api/extract-comics")
    r2 = api.post("/api/extract-comics")
    assert r2.json()["linked"] == 0


# ---------------------------------------------------------------------------
# POST /api/bids/{item_id}/comics/locg
# ---------------------------------------------------------------------------


def test_locg_link_sets_locg_id(api):
    api.post("/api/bids", json={"item_id": "555000010", "max_bid": 30.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    # Title must include a grade so extract-comics creates an fmv row and sets fmv_id
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Daredevil #1 1993 VF", "555000010"))
    raw.commit()
    raw.close()
    api.post("/api/extract-comics")

    r = api.post("/api/bids/555000010/comics/locg", json={"locg_id": 1931243})
    assert r.status_code == 200
    assert r.json()["locg_id"] == 1931243
    assert r.json()["is_primary"] is True


def test_locg_link_unknown_item_returns_404(api):
    r = api.post("/api/bids/000000000/comics/locg", json={"locg_id": 12345})
    assert r.status_code == 404


def test_locg_link_no_primary_returns_409(api):
    api.post("/api/bids", json={"item_id": "555000011", "max_bid": 10.0})
    r = api.post("/api/bids/555000011/comics/locg", json={"locg_id": 12345})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# GET /api/dashboard-tabs
# ---------------------------------------------------------------------------


def test_dashboard_tabs_returns_comics_tab(api):
    r = api.get("/api/dashboard-tabs")
    assert r.status_code == 200
    tabs = r.json()
    assert isinstance(tabs, list)
    assert len(tabs) == 1
    assert tabs[0] == {"label": "comics", "path": "/comics"}


# ---------------------------------------------------------------------------
# GET /api/comics/snipes  +  GET /api/comics/history
# ---------------------------------------------------------------------------


def _set_bid_fields(db_path, item_id, **fields):
    """Patch arbitrary bids columns by item_id."""
    raw = sqlite3.connect(db_path)
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        raw.execute(f"UPDATE bids SET {sets} WHERE item_id=?",
                    (*fields.values(), item_id))
        raw.commit()
    finally:
        raw.close()


def _link_comic(db_path, item_id, *, title, issue, year, grade,
                fmv_low=None, fmv_high=None, is_primary=True):
    """Create a comic + fmv row and link it to a bid via bid_fmvs."""
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute(
            "INSERT OR IGNORE INTO comics (title, issue, year) VALUES (?, ?, ?)",
            (title, issue, year),
        )
        cid = raw.execute(
            "SELECT id FROM comics WHERE title=? AND issue=? AND year=?",
            (title, issue, year),
        ).fetchone()["id"]
        raw.execute(
            "INSERT OR REPLACE INTO fmv (comic_id, grade, low, high) VALUES (?, ?, ?, ?)",
            (cid, grade, fmv_low, fmv_high),
        )
        fid = raw.execute(
            "SELECT id FROM fmv WHERE comic_id=? AND grade=?", (cid, grade)
        ).fetchone()["id"]
        bid = raw.execute(
            "SELECT id FROM bids WHERE item_id=?", (item_id,)
        ).fetchone()
        if is_primary:
            raw.execute("UPDATE bid_fmvs SET is_primary=0 WHERE bid_id=?", (bid["id"],))
            raw.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fid, bid["id"]))
        raw.execute(
            "INSERT OR REPLACE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bid["id"], fid, 1 if is_primary else 0),
        )
        raw.commit()
    finally:
        raw.close()


# --- /api/comics/snipes ---


def test_comics_snipes_empty_on_fresh_db(api):
    r = api.get("/api/comics/snipes")
    assert r.status_code == 200
    assert r.json() == []


def test_comics_snipes_single_linked_comic_returns_full_enrichment(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000001", "max_bid": 125.0})
    _set_bid_fields(db_path, "100000001",
                    cached_current_bid="120.00 USD",
                    auction_end_at="2099-01-01T00:00:00+00:00")
    _link_comic(db_path, "100000001",
                title="Amazing Spider-Man", issue="300", year=1988,
                grade=9.4, fmv_low=100.0, fmv_high=200.0)

    r = api.get("/api/comics/snipes")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["item_id"] == "100000001"
    assert row["cond_grade"] == 9.4
    assert row["cond_extra_count"] == 0
    assert row["fmv_low"] == 100.0
    assert row["fmv_high"] == 200.0
    assert row["lot_count"] == 1
    assert row["needs_linking"] is False
    assert row["max_bid_numeric"] == 125.0
    assert row["current_bid_numeric"] == 120.0
    # 120 / midpoint(150) * 100 = 80.0
    assert row["value_pct"] == pytest.approx(80.0)


def test_comics_snipes_lot_aggregates_fmv_and_blanks_value(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000002", "max_bid": 300.0})
    _set_bid_fields(db_path, "100000002",
                    cached_current_bid="200.00 USD",
                    auction_end_at="2099-01-01T00:00:00+00:00")
    _link_comic(db_path, "100000002",
                title="Lot Comic A", issue="1", year=1990,
                grade=9.4, fmv_low=50.0, fmv_high=100.0, is_primary=True)
    _link_comic(db_path, "100000002",
                title="Lot Comic B", issue="2", year=1990,
                grade=8.0, fmv_low=40.0, fmv_high=100.0, is_primary=False)
    _link_comic(db_path, "100000002",
                title="Lot Comic C", issue="3", year=1990,
                grade=7.0, fmv_low=60.0, fmv_high=100.0, is_primary=False)

    row = api.get("/api/comics/snipes").json()[0]
    assert row["cond_grade"] == 9.4
    assert row["cond_extra_count"] == 2
    assert row["fmv_low"] == 150.0  # sum of 50+40+60
    assert row["fmv_high"] == 300.0
    assert row["lot_count"] == 3
    # Lots never get a value % per R17 even when all components are priced.
    assert row["value_pct"] is None


def test_comics_snipes_needs_linking_when_no_bid_fmvs(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000003", "max_bid": 30.0})
    _set_bid_fields(db_path, "100000003",
                    auction_end_at="2099-01-01T00:00:00+00:00")

    row = api.get("/api/comics/snipes").json()[0]
    assert row["needs_linking"] is True
    assert row["lot_count"] == 0
    # cond_extra_count is clamped — must not be -1 when lot_count is 0.
    assert row["cond_extra_count"] == 0
    assert row["cond_grade"] is None
    assert row["fmv_low"] is None
    assert row["fmv_high"] is None
    assert row["value_pct"] is None


def test_comics_snipes_partial_null_single_comic_nulls_value(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000004", "max_bid": 150.0})
    _set_bid_fields(db_path, "100000004",
                    cached_current_bid="100.00 USD",
                    auction_end_at="2099-01-01T00:00:00+00:00")
    # fmv_high is NULL — single-comic partial-null rule: keep the available
    # bound, but null value_pct because the midpoint isn't computable.
    _link_comic(db_path, "100000004",
                title="Partial Comic", issue="1", year=1990,
                grade=9.0, fmv_low=100.0, fmv_high=None)

    row = api.get("/api/comics/snipes").json()[0]
    assert row["fmv_low"] == 100.0   # available bound is preserved
    assert row["fmv_high"] is None
    assert row["value_pct"] is None  # but no value signal without a midpoint


def test_comics_snipes_lot_partial_null_nulls_aggregate(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000005", "max_bid": 300.0})
    _set_bid_fields(db_path, "100000005",
                    cached_current_bid="200.00 USD",
                    auction_end_at="2099-01-01T00:00:00+00:00")
    # Two priced, one unpriced — SUM would silently drop the third.
    _link_comic(db_path, "100000005",
                title="Lot A", issue="1", year=1990,
                grade=9.4, fmv_low=100.0, fmv_high=200.0, is_primary=True)
    _link_comic(db_path, "100000005",
                title="Lot B", issue="2", year=1990,
                grade=8.0, fmv_low=50.0, fmv_high=150.0, is_primary=False)
    _link_comic(db_path, "100000005",
                title="Lot C (unpriced)", issue="3", year=1990,
                grade=7.0, fmv_low=None, fmv_high=None, is_primary=False)

    row = api.get("/api/comics/snipes").json()[0]
    assert row["lot_count"] == 3
    assert row["fmv_low"] is None  # nulled because one component is unpriced
    assert row["fmv_high"] is None
    assert row["value_pct"] is None


def test_comics_snipes_excludes_purged(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000006", "max_bid": 50.0})
    _set_bid_fields(db_path, "100000006",
                    status="PURGED",
                    auction_end_at="2099-01-01T00:00:00+00:00")

    r = api.get("/api/comics/snipes")
    assert r.status_code == 200
    assert all(row["item_id"] != "100000006" for row in r.json())


def test_comics_snipes_includes_ended_but_pending_bid(api):
    """Mirror /api/snipes: do NOT filter by end date server-side.

    A snipe whose auction has ended but whose status hasn't transitioned yet
    should still appear in /api/comics/snipes — the JS will partition it via
    isEnded() and move it to the ended table.
    """
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000007", "max_bid": 50.0})
    _set_bid_fields(db_path, "100000007",
                    auction_end_at="2000-01-01T00:00:00+00:00")  # past

    r = api.get("/api/comics/snipes")
    assert any(row["item_id"] == "100000007" for row in r.json())


# --- /api/comics/history ---


def test_comics_history_empty_on_fresh_db(api):
    r = api.get("/api/comics/history")
    assert r.status_code == 200
    assert r.json() == []


def test_comics_history_returns_recently_ended_snipes(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "200000001", "max_bid": 475.0})
    _set_bid_fields(db_path, "200000001",
                    auction_end_at="2025-01-01T00:00:00+00:00",  # within last 7 days from "now"? no — adjust
                    status="WON",
                    winning_bid=412.0)
    # Re-set auction_end_at to 1 day ago in SQLite's "now"
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET auction_end_at = datetime('now', '-1 day') WHERE item_id=?",
        ("200000001",),
    )
    raw.commit()
    raw.close()
    _link_comic(db_path, "200000001",
                title="Spider-Man", issue="300", year=1988,
                grade=9.4, fmv_low=300.0, fmv_high=500.0)

    rows = api.get("/api/comics/history").json()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "200000001"
    assert rows[0]["status"] == "WON"
    assert rows[0]["winning_bid"] == 412.0
    assert rows[0]["max_bid_numeric"] == 475.0
    assert rows[0]["cond_grade"] == 9.4


def test_comics_history_dedups_by_max_id_per_item(api):
    """Two rows for the same item_id (re-snipe after purge) → one in history."""
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at) "
        "VALUES (?, ?, 'PURGED', datetime('now', '-2 days'))",
        ("200000002", 100.0),
    )
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at) "
        "VALUES (?, ?, 'LOST', datetime('now', '-1 day'))",
        ("200000002", 120.0),
    )
    raw.commit()
    raw.close()

    rows = api.get("/api/comics/history").json()
    matching = [r for r in rows if r["item_id"] == "200000002"]
    assert len(matching) == 1
    # Latest row should be the LOST one (higher id).
    assert matching[0]["status"] == "LOST"


def test_comics_history_includes_resolved_at_fallback_for_null_auction_end(api):
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, resolved_at) "
        "VALUES (?, ?, 'LOST', NULL, datetime('now', '-2 days'))",
        ("200000003", 50.0),
    )
    raw.commit()
    raw.close()

    rows = api.get("/api/comics/history").json()
    assert any(r["item_id"] == "200000003" for r in rows)


def test_comics_history_excludes_active_snipes(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "200000004", "max_bid": 50.0})
    _set_bid_fields(db_path, "200000004",
                    auction_end_at="2099-01-01T00:00:00+00:00")  # far future

    rows = api.get("/api/comics/history").json()
    assert all(r["item_id"] != "200000004" for r in rows)


def test_comics_snipes_triggers_fresh_sync(api):
    """/api/comics/snipes calls _ensure_fresh_sync just like /api/snipes does.

    Patches at the import site (gixen_overlay.routes) because the route binds
    the symbol at module load.
    """
    with patch("gixen_overlay.routes._ensure_fresh_sync") as mock_sync, \
         patch("gixen_overlay.routes._spawn_fallback_task") as mock_spawn:
        r = api.get("/api/comics/snipes")
        assert r.status_code == 200
        assert mock_sync.called
        assert mock_spawn.called
