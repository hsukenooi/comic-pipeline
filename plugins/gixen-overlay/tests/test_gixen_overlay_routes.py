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
