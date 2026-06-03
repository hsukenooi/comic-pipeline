"""Integration tests for gixen-overlay plugin routes.

Uses the real gixen-cli server (server.main.app) with the real plugin
loaded via the entry-point discovery path.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_STATIC_DIR = os.path.join(
    os.path.dirname(__file__), "..", "src", "gixen_overlay", "static"
)


def _extract_js_function(source: str, name: str) -> str:
    """Slice a top-level `function <name>(...) { ... }` out of JS source by
    brace-matching, so we can execute it in node without a JS test runner."""
    start = source.index(f"function {name}(")
    depth = 0
    i = source.index("{", start)
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated function {name} in source")  # pragma: no cover


def _run_outcome(row: dict) -> str:
    """Execute v2-comics.html's outcome() in node against a single row and
    return the rendered HTML string. Skips if node is unavailable."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not installed")
    with open(os.path.join(_STATIC_DIR, "v2-comics.html")) as fh:
        html = fh.read()
    outcome_src = _extract_js_function(html, "outcome")
    script = (
        "function numericMax(r){return r.max_bid_numeric!=null?"
        "parseFloat(r.max_bid_numeric):null;}\n"
        f"{outcome_src}\n"
        "const row=JSON.parse(process.argv[1]);\n"
        "process.stdout.write(outcome(row));\n"
    )
    out = subprocess.run(
        [node, "-e", script, json.dumps(row)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


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


def test_upsert_comic_without_year_creates_yearless_row(api):
    """PER-98: year is optional — yearless inserts create a NULL-year row."""
    r = api.post("/api/comics", json={"title": "X-Men", "issue": "1"})
    assert r.status_code == 200
    assert r.json()["year"] is None


def test_upsert_comic_response_includes_comic_id_and_fmv_id(api):
    """PER-144: response includes both comic_id and fmv_id when FMV is provided."""
    r = api.post("/api/comics", json={
        "title": "Daredevil", "issue": "1", "year": 1964,
        "grade": 7.0, "fmv_low": 1200.0, "fmv_high": 1500.0,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["comic_id"] == data["id"]
    assert data["comic_id"] > 0
    assert isinstance(data["fmv_id"], int)
    assert data["fmv_id"] > 0


def test_upsert_comic_response_fmv_id_null_without_grade(api):
    """PER-144: fmv_id is null when no grade is supplied."""
    r = api.post("/api/comics", json={
        "title": "Daredevil", "issue": "1", "year": 1964,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["comic_id"] == data["id"]
    assert data["fmv_id"] is None


def test_upsert_comic_invalid_confidence_returns_422(api):
    r = api.post("/api/comics", json={
        "title": "X-Men", "issue": "1", "year": 1963,
        "fmv_confidence": "very_high",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/comics — locg_id and max_age_days filters (comic-fmv cache lookup)
# ---------------------------------------------------------------------------


def test_list_comics_by_locg_id(api):
    """GET /api/comics?locg_id=N returns rows with that LOCG ID."""
    api.post("/api/comics", json={
        "title": "Amazing Spider-Man", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800, "fmv_high": 1000, "fmv_comps": 12,
        "fmv_confidence": "high", "fmv_notes": "key",
        "locg_id": 6977652,
    })
    api.post("/api/comics", json={
        "title": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0, "fmv_low": 50, "fmv_high": 70, "fmv_comps": 10,
        "fmv_confidence": "high", "fmv_notes": "",
        "locg_id": 12345,
    })
    rows = api.get("/api/comics", params={"locg_id": 6977652}).json()
    assert len(rows) == 1
    assert rows[0]["title"] == "Amazing Spider-Man"


def test_list_comics_max_age_days_excludes_stale(api):
    """GET /api/comics?max_age_days=N excludes rows past the cutoff."""
    from datetime import datetime, timedelta, timezone
    import sqlite3

    api.post("/api/comics", json={
        "title": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0, "fmv_low": 50, "fmv_high": 70, "fmv_comps": 10,
        "fmv_confidence": "high", "fmv_notes": "",
    })
    conn = sqlite3.connect(os.environ["DB_PATH"])
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute("UPDATE fmv SET updated_at = ?", (old,))
    conn.commit()
    conn.close()

    assert api.get("/api/comics", params={"max_age_days": 7}).json() == []
    assert len(api.get("/api/comics", params={"max_age_days": 60}).json()) == 1


def test_list_comics_locg_id_plus_max_age(api):
    """The fmv-cache lookup pattern from comic-fmv: locg_id + grade + max_age_days."""
    api.post("/api/comics", json={
        "title": "ASM", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800, "fmv_high": 1000, "fmv_comps": 12,
        "fmv_confidence": "high", "fmv_notes": "",
        "locg_id": 6977652,
    })
    rows = api.get("/api/comics", params={
        "locg_id": 6977652, "grade": 9.2, "max_age_days": 7,
    }).json()
    assert len(rows) == 1
    assert rows[0]["fmv_low"] == 800


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


def test_extract_comics_year_fallback_failure_creates_yearless_row(api, monkeypatch):
    """PER-98: when LOCG can't resolve year, the bid still links — yearless."""
    from gixen_overlay import routes

    api.post("/api/bids", json={"item_id": "999000114", "max_bid": 50.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Amazing Spider-Man #300 NM", "999000114"))
    raw.commit()
    raw.close()

    monkeypatch.setattr(routes, "resolve_year_and_locg", lambda *_: None)

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    # No longer skipped — comic + fmv + bid_fmvs all created with year=NULL
    assert body["linked"] == 1
    assert body["skipped"] == []

    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    rows = raw.execute(
        "SELECT title, issue, year FROM comics WHERE issue=?", ("300",)
    ).fetchall()
    raw.close()
    assert len(rows) == 1
    assert rows[0]["year"] is None


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


def test_extract_comics_caps_title_reuses_existing_valued_fmv(api):
    """ALL-CAPS eBay title must reuse existing valued FMV row, not create a stub."""
    # Pre-populate a properly-cased comic + valued FMV via POST /api/comics
    r = api.post("/api/comics", json={
        "title": "Batman", "issue": "375", "year": 1984,
        "grade": 8.0, "fmv_low": 50.0, "fmv_high": 70.0, "fmv_confidence": "high",
    })
    assert r.status_code == 200

    # Add a bid with an ALL-CAPS eBay title for the same comic
    api.post("/api/bids", json={"item_id": "999000120", "max_bid": 60.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("BATMAN #375 1984 VF", "999000120"))
    raw.commit()
    raw.close()

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] == 1

    # Only one comic row must exist — no stub duplicate
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    comics = raw.execute("SELECT id FROM comics WHERE issue='375'").fetchall()
    assert len(comics) == 1

    # The bid_fmvs junction must point at the valued FMV, not a stub
    fmvs = raw.execute(
        "SELECT f.low FROM bid_fmvs bf JOIN fmv f ON f.id=bf.fmv_id "
        "JOIN bids b ON b.id=bf.bid_id WHERE b.item_id='999000120'"
    ).fetchall()
    raw.close()
    assert len(fmvs) == 1
    assert fmvs[0]["low"] == 50.0


def test_extract_comics_no_grade_links_to_existing_valued_fmv(api):
    """Grade-null bid links to any existing valued FMV for the comic."""
    api.post("/api/comics", json={
        "title": "Batman", "issue": "375", "year": 1984,
        "grade": 8.0, "fmv_low": 50.0, "fmv_high": 70.0, "fmv_confidence": "high",
    })
    api.post("/api/bids", json={"item_id": "999000121", "max_bid": 60.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Batman #375 1984", "999000121"))
    raw.commit()
    raw.close()

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] == 1
    assert body["skipped"] == []

    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    fmvs = raw.execute(
        "SELECT f.low FROM bid_fmvs bf JOIN fmv f ON f.id=bf.fmv_id "
        "JOIN bids b ON b.id=bf.bid_id WHERE b.item_id='999000121'"
    ).fetchall()
    raw.close()
    assert len(fmvs) == 1
    assert fmvs[0]["low"] == 50.0


def test_extract_comics_no_grade_no_fmv_goes_to_skipped(api):
    """Grade-null bid with no existing FMV is skipped, not falsely reported as linked."""
    api.post("/api/bids", json={"item_id": "999000122", "max_bid": 60.0})
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET ebay_title=? WHERE item_id=?",
                ("Batman #375 1984", "999000122"))
    raw.commit()
    raw.close()

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] == 0
    assert any(s["item_id"] == "999000122" for s in body["skipped"])

    # Confirm no bid_fmvs junction was written
    raw = sqlite3.connect(db_path)
    count = raw.execute(
        "SELECT COUNT(*) FROM bid_fmvs bf JOIN bids b ON b.id=bf.bid_id "
        "WHERE b.item_id='999000122'"
    ).fetchone()[0]
    raw.close()
    assert count == 0


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
# POST /api/bids/{item_id}/link-fmv
# ---------------------------------------------------------------------------


def test_link_fmv_creates_junction_and_returns_linked(api):
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "600000001", "max_bid": 50.0})
    # Create comic + fmv with a known locg_id
    api.post("/api/comics", json={
        "title": "Amazing Spider-Man", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800.0, "fmv_high": 1000.0, "locg_id": 77777,
    })

    r = api.post("/api/bids/600000001/link-fmv",
                 json={"locg_id": 77777, "grade": 9.2})
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "600000001"
    assert body["linked"] is True
    assert isinstance(body["fmv_id"], int)

    # Confirm /api/comics/snipes now shows enrichment for this bid
    snipes = api.get("/api/comics/snipes").json()
    row = next(s for s in snipes if s["item_id"] == "600000001")
    assert row["cond_grade"] == 9.2
    assert row["fmv_low"] == 800.0
    assert row["fmv_high"] == 1000.0


def test_link_fmv_unknown_item_returns_404(api):
    r = api.post("/api/bids/999999999/link-fmv",
                 json={"locg_id": 77777, "grade": 9.2})
    assert r.status_code == 404


def test_link_fmv_unknown_fmv_returns_404(api):
    api.post("/api/bids", json={"item_id": "600000002", "max_bid": 50.0})
    r = api.post("/api/bids/600000002/link-fmv",
                 json={"locg_id": 99999, "grade": 9.8})
    assert r.status_code == 404


def test_link_fmv_by_comic_id_skips_locg_lookup(api):
    """PER-143 strategy 1: comic_id+grade resolves without touching locg_id."""
    api.post("/api/bids", json={"item_id": "600000010", "max_bid": 50.0})
    # No locg_id set on the comic at all
    r = api.post("/api/comics", json={
        "title": "Hulk", "issue": "181", "year": 1974,
        "grade": 9.0, "fmv_low": 300.0, "fmv_high": 500.0,
    })
    comic_id = r.json()["id"]

    r = api.post(
        "/api/bids/600000010/link-fmv",
        json={"comic_id": comic_id, "grade": 9.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert isinstance(body["fmv_id"], int)

    # Sanity: dashboard now shows enrichment for this bid
    row = next(s for s in api.get("/api/comics/snipes").json()
               if s["item_id"] == "600000010")
    assert row["cond_grade"] == 9.0
    assert row["fmv_low"] == 300.0


def test_link_fmv_by_series_issue_grade_when_locg_id_null(api):
    """PER-143 strategy 3: series+issue+grade resolves when locg_id is NULL."""
    api.post("/api/bids", json={"item_id": "600000011", "max_bid": 50.0})
    # locg_id deliberately omitted — mirrors the post-fmv_runner reality
    api.post("/api/comics", json={
        "title": "Avengers", "issue": "10", "year": 1964,
        "grade": 5.0, "fmv_low": 200.0, "fmv_high": 300.0,
    })

    r = api.post(
        "/api/bids/600000011/link-fmv",
        json={"series": "Avengers", "issue": "10", "grade": 5.0},
    )
    assert r.status_code == 200
    assert r.json()["linked"] is True

    row = next(s for s in api.get("/api/comics/snipes").json()
               if s["item_id"] == "600000011")
    assert row["cond_grade"] == 5.0
    assert row["fmv_low"] == 200.0


def test_link_fmv_by_series_issue_grade_case_insensitive(api):
    """series matches case-insensitively — ALL-CAPS eBay titles should still link."""
    api.post("/api/bids", json={"item_id": "600000012", "max_bid": 50.0})
    api.post("/api/comics", json={
        "title": "Amazing Spider-Man", "issue": "300", "year": 1988,
        "grade": 9.2, "fmv_low": 800.0, "fmv_high": 1000.0,
    })

    r = api.post(
        "/api/bids/600000012/link-fmv",
        json={"series": "AMAZING SPIDER-MAN", "issue": "300", "grade": 9.2},
    )
    assert r.status_code == 200
    assert r.json()["linked"] is True


def test_link_fmv_by_series_issue_year_grade_disambiguates(api):
    """When year is supplied, only the (series, issue, year, grade) row matches."""
    api.post("/api/bids", json={"item_id": "600000013", "max_bid": 50.0})
    # Two volumes of the same series at the same grade
    api.post("/api/comics", json={
        "title": "X-Men", "issue": "1", "year": 1963,
        "grade": 8.0, "fmv_low": 5000.0, "fmv_high": 8000.0,
    })
    api.post("/api/comics", json={
        "title": "X-Men", "issue": "1", "year": 1991,
        "grade": 8.0, "fmv_low": 10.0, "fmv_high": 20.0,
    })

    r = api.post(
        "/api/bids/600000013/link-fmv",
        json={"series": "X-Men", "issue": "1", "year": 1991, "grade": 8.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True

    row = next(s for s in api.get("/api/comics/snipes").json()
               if s["item_id"] == "600000013")
    assert row["fmv_low"] == 10.0  # the 1991 volume, not 1963


def test_link_fmv_404_lists_attempted_strategies(api):
    """404 detail names every strategy actually attempted."""
    api.post("/api/bids", json={"item_id": "600000014", "max_bid": 50.0})
    r = api.post(
        "/api/bids/600000014/link-fmv",
        json={"comic_id": 99999, "locg_id": 88888,
              "series": "Nope", "issue": "1", "grade": 1.0},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "comic_id=99999" in detail
    assert "locg_id=88888" in detail
    assert "series='Nope'" in detail


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


def test_comics_snipes_excludes_removed(api):
    """BUI-49: 'REMOVED' is the renamed tombstone — also excluded from snipes."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "100000016", "max_bid": 50.0})
    _set_bid_fields(db_path, "100000016",
                    status="REMOVED",
                    auction_end_at="2099-01-01T00:00:00+00:00")

    r = api.get("/api/comics/snipes")
    assert r.status_code == 200
    assert all(row["item_id"] != "100000016" for row in r.json())


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


def test_comics_history_excludes_purged_within_window(api):
    """BUI-50: a removed (PURGED) snipe whose auction ended within the 7-day
    window must not appear in recently-ended (it would render a false 'won')."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "200000005", "max_bid": 50.0})
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET status='PURGED', winning_bid=12.0, "
        "auction_end_at=datetime('now', '-1 day') WHERE item_id=?",
        ("200000005",),
    )
    raw.commit()
    raw.close()

    rows = api.get("/api/comics/history").json()
    assert all(r["item_id"] != "200000005" for r in rows)


def test_comics_history_purged_does_not_shadow_legit_loss(api):
    """BUI-50: filtering PURGED inside the MAX(id) dedup subquery (not just the
    outer query) means a later add-then-remove (PURGED, higher id) for the same
    item does not hide the earlier legitimate LOST row."""
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    # Lower id: a real loss. Higher id: re-added then removed (PURGED).
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at) "
        "VALUES (?, ?, 'LOST', datetime('now', '-2 days'))",
        ("200000006", 120.0),
    )
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, auction_end_at) "
        "VALUES (?, ?, 'PURGED', ?, datetime('now', '-1 day'))",
        ("200000006", 120.0, 10.0),
    )
    raw.commit()
    raw.close()

    rows = api.get("/api/comics/history").json()
    matching = [r for r in rows if r["item_id"] == "200000006"]
    assert len(matching) == 1
    assert matching[0]["status"] == "LOST"


def test_comics_history_excludes_removed_within_window(api):
    """BUI-49: a 'REMOVED' (renamed tombstone) bid within the 7-day window must
    not appear in recently-ended, same as the legacy 'PURGED' value."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "200000015", "max_bid": 50.0})
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET status='REMOVED', winning_bid=12.0, "
        "auction_end_at=datetime('now', '-1 day') WHERE item_id=?",
        ("200000015",),
    )
    raw.commit()
    raw.close()

    rows = api.get("/api/comics/history").json()
    assert all(r["item_id"] != "200000015" for r in rows)


def test_comics_history_removed_does_not_shadow_legit_loss(api):
    """BUI-49: the dedup-subquery tombstone filter covers 'REMOVED' too — a
    later add-then-remove (REMOVED, higher id) does not hide the earlier LOST."""
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at) "
        "VALUES (?, ?, 'LOST', datetime('now', '-2 days'))",
        ("200000016", 120.0),
    )
    raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, auction_end_at) "
        "VALUES (?, ?, 'REMOVED', ?, datetime('now', '-1 day'))",
        ("200000016", 120.0, 10.0),
    )
    raw.commit()
    raw.close()

    rows = api.get("/api/comics/history").json()
    matching = [r for r in rows if r["item_id"] == "200000016"]
    assert len(matching) == 1
    assert matching[0]["status"] == "LOST"


# --- JS outcome() (v2-comics.html) -------------------------------------------


def test_outcome_purged_never_renders_won():
    """BUI-50 defense-in-depth: a leaked PURGED row must never be painted 'won'
    by the winning_bid<=max_bid heuristic, even when the stale snapshot is low."""
    html = _run_outcome({
        "status": "PURGED",
        "winning_bid": 10.0,   # stale pre-removal snapshot, well below max
        "max_bid_numeric": 120.0,
    })
    assert 'pill won' not in html  # never the green "won" pill
    assert "removed" in html


def test_outcome_removed_never_renders_won():
    """BUI-49: the renamed tombstone 'REMOVED' is handled exactly like 'PURGED'
    — never the 'won' pill, even when the stale snapshot is below max_bid."""
    html = _run_outcome({
        "status": "REMOVED",
        "winning_bid": 10.0,
        "max_bid_numeric": 120.0,
    })
    assert 'pill won' not in html
    assert "removed" in html


def test_outcome_won_still_renders_won():
    """Guard: the new PURGED branch doesn't disturb a genuine WON row."""
    html = _run_outcome({
        "status": "WON", "winning_bid": 100.0, "max_bid_numeric": 120.0,
    })
    assert 'pill won' in html


def test_outcome_lost_still_renders_outbid():
    """Guard: a genuine LOST row still renders 'outbid'."""
    html = _run_outcome({
        "status": "LOST", "winning_bid": 130.0, "max_bid_numeric": 120.0,
    })
    assert 'pill lost' in html
    assert "outbid" in html


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


# ---------------------------------------------------------------------------
# POST /api/comics/verify  (PER-99)
# ---------------------------------------------------------------------------


def test_verify_fully_linked(api):
    """A bid with comic + fmv (populated low/high) + junction + bids.fmv_id."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000001", "max_bid": 100.0})
    _link_comic(db_path, "300000001",
                title="Amazing Spider-Man", issue="300", year=1988,
                grade=9.2, fmv_low=800.0, fmv_high=1000.0)

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000001", "grade": 9.2}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {"total": 1, "fully_linked": 1, "issues": 0}
    row = body["results"][0]
    assert row["verdict"] == "fully_linked"
    assert row["missing"] == []
    assert row["comic_id"] > 0
    assert row["fmv_id"] > 0
    assert row["bid_fmv_id"] == row["fmv_id"]


def test_verify_no_bid(api):
    """item_id that never made it into the bids table."""
    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "999999999", "grade": 9.2}],
    })
    assert r.status_code == 200
    row = r.json()["results"][0]
    assert row["verdict"] == "no_bid"
    assert row["missing"] == ["bids row"]


def test_verify_no_comic_when_bid_has_no_links(api):
    """Bid exists but no bid_fmvs junction — `/comic:fmv` step never ran."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000002", "max_bid": 50.0})

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000002", "grade": 9.2}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "no_comic"
    assert "bid_fmvs junction" in row["missing"]


def test_verify_no_comic_when_locg_id_unknown(api):
    """locg_id passed but no comic row matches — every link is missing."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000003", "max_bid": 50.0})

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000003", "grade": 9.2, "locg_id": 999999}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "no_comic"
    assert "comics row" in row["missing"]


def test_verify_fmv_stub(api):
    """Comic + fmv at grade exist, but fmv.low/high are NULL (`/comic:fmv` never ran)."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000004", "max_bid": 50.0})
    _link_comic(db_path, "300000004",
                title="Spawn", issue="9", year=1993,
                grade=9.4, fmv_low=None, fmv_high=None)

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000004", "grade": 9.4}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "fmv_stub"
    assert "fmv.low" in row["missing"]
    assert "fmv.high" in row["missing"]


def test_verify_no_fmv_at_grade(api):
    """Comic exists with fmv at a different grade — caller asked for 9.8."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000005", "max_bid": 50.0})
    _link_comic(db_path, "300000005",
                title="Hulk", issue="181", year=1974,
                grade=9.0, fmv_low=300.0, fmv_high=500.0)

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000005", "grade": 9.8}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "no_fmv_at_grade"
    assert row["missing"] == ["fmv row at grade 9.8"]


def test_verify_partial_when_bids_fmv_id_missing(api):
    """Junction exists with populated fmv, but bids.fmv_id is NULL."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000006", "max_bid": 50.0})
    _link_comic(db_path, "300000006",
                title="X-Men", issue="266", year=1990,
                grade=9.6, fmv_low=40.0, fmv_high=60.0)
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE bids SET fmv_id = NULL WHERE item_id = ?", ("300000006",))
    raw.commit()
    raw.close()

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000006", "grade": 9.6}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "partial"
    assert "bids.fmv_id" in row["missing"]


def test_verify_partial_when_bids_fmv_id_mismatches(api):
    """bids.fmv_id points at a different fmv than the one matched by grade."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000007", "max_bid": 50.0})
    # Primary fmv at grade 8.0
    _link_comic(db_path, "300000007",
                title="ASM", issue="252", year=1984,
                grade=8.0, fmv_low=100.0, fmv_high=150.0, is_primary=True)
    # Second fmv at grade 9.2 — but not flagged as primary
    _link_comic(db_path, "300000007",
                title="ASM", issue="252", year=1984,
                grade=9.2, fmv_low=400.0, fmv_high=500.0, is_primary=False)

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000007", "grade": 9.2}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "partial"
    assert any("bids.fmv_id" in m for m in row["missing"])


def test_verify_locg_id_mismatch_is_partial(api):
    """locg_id passed differs from what's on the comic row — surfaces in `missing`."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000008", "max_bid": 50.0})
    _link_comic(db_path, "300000008",
                title="ASM", issue="300", year=1988,
                grade=9.2, fmv_low=800.0, fmv_high=1000.0)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE comics SET locg_id = ? WHERE title = ? AND issue = ?",
        (11111, "ASM", "300"),
    )
    raw.commit()
    raw.close()

    r = api.post("/api/comics/verify", json={
        "items": [{"item_id": "300000008", "grade": 9.2, "locg_id": 22222}],
    })
    row = r.json()["results"][0]
    assert row["verdict"] == "partial"
    assert any("locg_id" in m for m in row["missing"])


def test_verify_summary_counts(api):
    """Mixed batch: summary reports total/fully_linked/issues correctly."""
    db_path = os.environ["DB_PATH"]
    api.post("/api/bids", json={"item_id": "300000010", "max_bid": 50.0})
    _link_comic(db_path, "300000010",
                title="Good", issue="1", year=1990,
                grade=9.0, fmv_low=10.0, fmv_high=20.0)
    api.post("/api/bids", json={"item_id": "300000011", "max_bid": 50.0})
    _link_comic(db_path, "300000011",
                title="Stub", issue="1", year=1990,
                grade=9.0, fmv_low=None, fmv_high=None)

    r = api.post("/api/comics/verify", json={
        "items": [
            {"item_id": "300000010", "grade": 9.0},
            {"item_id": "300000011", "grade": 9.0},
            {"item_id": "999000000", "grade": 9.0},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {"total": 3, "fully_linked": 1, "issues": 2}
    verdicts = [r["verdict"] for r in body["results"]]
    assert verdicts == ["fully_linked", "fmv_stub", "no_bid"]


# ---------------------------------------------------------------------------
# GET /api/seller-reliability (BUI-78)
# ---------------------------------------------------------------------------


def _seed_graded_bid(api, item_id, seller, seller_grade, photo_grade, status="PENDING"):
    """Create a bid then set its seller + grades + status directly."""
    api.post("/api/bids", json={"item_id": item_id, "max_bid": 50.0})
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.execute(
        "UPDATE bids SET seller=?, seller_grade=?, photo_grade=?, status=? WHERE item_id=?",
        (seller, seller_grade, photo_grade, status, item_id),
    )
    raw.commit()
    raw.close()


def test_seller_reliability_avg_and_sample(api):
    # sellera: +1.0, +2.0, +1.5 -> avg 1.5 over n=3
    _seed_graded_bid(api, "800000001", "sellera", 9.0, 8.0)
    _seed_graded_bid(api, "800000002", "sellera", 9.2, 7.2)
    _seed_graded_bid(api, "800000003", "sellera", 9.0, 7.5)
    r = api.get("/api/seller-reliability", params={"seller": "sellera"})
    assert r.status_code == 200
    body = r.json()
    assert body["seller"] == "sellera"
    assert body["sample_size"] == 3
    assert body["avg_deviation"] == pytest.approx(1.5)


def test_seller_reliability_excludes_missing_grade_and_tombstones(api):
    _seed_graded_bid(api, "800000010", "sellerb", 9.0, 8.0)              # +1.0, counts
    _seed_graded_bid(api, "800000011", "sellerb", 9.0, None)            # missing photo grade
    _seed_graded_bid(api, "800000012", "sellerb", 5.0, 1.0, status="REMOVED")  # tombstone
    r = api.get("/api/seller-reliability", params={"seller": "sellerb"})
    assert r.status_code == 200
    body = r.json()
    assert body["sample_size"] == 1
    assert body["avg_deviation"] == pytest.approx(1.0)


def test_seller_reliability_unknown_seller_zero_sample(api):
    r = api.get("/api/seller-reliability", params={"seller": "nobody-here"})
    assert r.status_code == 200
    body = r.json()
    assert body["seller"] == "nobody-here"
    assert body["sample_size"] == 0
    assert body["avg_deviation"] is None


def test_seller_reliability_missing_param_422(api):
    assert api.get("/api/seller-reliability").status_code == 422


def test_seller_reliability_overlong_param_422(api):
    r = api.get("/api/seller-reliability", params={"seller": "x" * 129})
    assert r.status_code == 422


def test_seller_reliability_case_insensitive(api):
    _seed_graded_bid(api, "800000020", "beatlebluecat", 9.0, 7.0)  # +2.0
    r = api.get("/api/seller-reliability", params={"seller": "BeatleBlueCat"})
    assert r.status_code == 200
    body = r.json()
    assert body["sample_size"] == 1
    assert body["avg_deviation"] == pytest.approx(2.0)


def test_seller_reliability_matches_mixedcase_stored_seller(api):
    """Legacy/sync rows may store a mixed-case seller; the lowercased query must
    still match them (WHERE LOWER(seller) = ?)."""
    api.post("/api/bids", json={"item_id": "800000030", "max_bid": 50.0})
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.execute(
        "UPDATE bids SET seller='MixedCaseSeller', seller_grade=9.0, photo_grade=8.0 WHERE item_id=?",
        ("800000030",),
    )
    raw.commit()
    raw.close()
    r = api.get("/api/seller-reliability", params={"seller": "mixedcaseseller"})
    assert r.status_code == 200
    assert r.json()["sample_size"] == 1


def test_readd_over_sync_seller_normalizes_key(api):
    """Web-added (sync) row carries a mixed-case store name; a buy-flow re-add
    with grades must normalize the seller to the username so the advisory finds it."""
    api.post("/api/bids", json={"item_id": "800000031", "max_bid": 50.0})
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.execute(
        "UPDATE bids SET seller='Beatle Blue Cat Collectibles' WHERE item_id=?",
        ("800000031",),
    )
    raw.commit()
    raw.close()
    # Buy-flow re-add (existing PENDING -> modify path) supplies the username + grades.
    r = api.post("/api/bids", json={
        "item_id": "800000031", "max_bid": 60.0,
        "seller": "beatlebluecat", "seller_grade": 9.0, "photo_grade": 7.0,
    })
    assert r.status_code == 200
    adv = api.get("/api/seller-reliability", params={"seller": "beatlebluecat"})
    assert adv.json()["sample_size"] == 1
    assert adv.json()["avg_deviation"] == pytest.approx(2.0)
