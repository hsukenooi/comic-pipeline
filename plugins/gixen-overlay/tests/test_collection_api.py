"""Integration tests for the BUI-91/92 collection + wish-list API endpoints.

These exercise the real gixen-cli server (server.main.app) with the real
overlay plugin loaded, pointing locg-cli's store (LOCG_DATA_DIR) at a seeded
temp directory. Mirrors the `api` fixture pattern in
test_gixen_overlay_routes.py but adds the seeded collection/wish-list store.
"""
from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _install_real_plugin(monkeypatch):
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
    return m


def _seed_collection(store, comics):
    """Write a minimal collection.json the locg matcher can read."""
    payload = {
        "schema_version": 1,
        "last_full_import": "2026-06-01T00:00:00.000000Z",
        "last_import_source": "seed.xlsx",
        "migration_in_progress": False,
        "last_writer": None,
        "series_name_index": {},
        "comics": comics,
    }
    (store / "collection.json").write_text(json.dumps(payload))


def _seed_wish_list(store, items):
    (store / "wish-list.json").write_text(
        json.dumps({"updated_at": "2026-06-01T00:00:00Z", "items": items})
    )


# A small owned collection: one owned ASM #300, one *wish-list-only* row
# (in_collection=0) to prove the copies-owned gate is respected.
_OWNED = [
    {
        "full_title": "The Amazing Spider-Man #300",
        "series_name": "The Amazing Spider-Man",
        "publisher_name": "Marvel Comics",
        "release_date": "1988-05-01",
        "in_collection": 1,
    },
    {
        "full_title": "Fantastic Four #48",
        "series_name": "Fantastic Four",
        "publisher_name": "Marvel Comics",
        "release_date": "1966-03-01",
        "in_collection": 0,  # wish-list / read but not owned
    },
]

_WISH = [
    {"name": "Fantastic Four #48", "id": 6977652},
    {"name": "X-Men #1", "id": None},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    _seed_collection(store, _OWNED)
    _seed_wish_list(store, _WISH)

    _install_real_plugin(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))  # overrides the server default
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    with patch("server.main.GixenClient", return_value=_mock_gixen()):
        from server.main import app

        with TestClient(app) as c:
            c.store = store
            yield c


# --- collection check ------------------------------------------------------

def test_check_owned(client):
    r = client.get("/api/comics/collection/check", params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"})
    assert r.status_code == 200
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["full_title_matched"] == "The Amazing Spider-Man #300"
    assert body["cache_age_days"] is not None


def test_check_owned_with_leading_article_dropped(client):
    """Parity with the local matcher: identify often drops the leading article
    ("The"), and the series-key normalizer strips it — so the plain name still
    matches the owned "The Amazing Spider-Man" (the BUI-45 / 17-owned-Hulks
    failure class)."""
    r = client.get("/api/comics/collection/check", params={"series": "The Amazing Spider-Man", "issue": "300", "year": "1988"})
    assert r.json()["match_status"] == "in_collection"


def test_check_not_owned_returns_not_in_cache(client):
    r = client.get("/api/comics/collection/check", params={"series": "Batman", "issue": "1", "year": "1940"})
    assert r.status_code == 200
    body = r.json()
    assert body["match_status"] == "not_in_cache"
    assert body["full_title_matched"] is None


def test_check_wishlist_only_row_is_not_owned(client):
    """A row with in_collection=0 (wish-list/read but not owned) must NOT count
    as owned — the copies-owned gate (BUI-26 bug D)."""
    r = client.get("/api/comics/collection/check", params={"series": "Fantastic Four", "issue": "48", "year": "1966"})
    assert r.json()["match_status"] == "not_in_cache"


def test_check_requires_series_and_issue(client):
    assert client.get("/api/comics/collection/check", params={"series": "Batman"}).status_code == 422
    assert client.get("/api/comics/collection/check", params={"issue": "1"}).status_code == 422


# --- wish-list -------------------------------------------------------------

def test_wish_list_returns_items(client):
    r = client.get("/api/comics/wish-list")
    assert r.status_code == 200
    items = r.json()
    names = {i["name"] for i in items}
    assert names == {"Fantastic Four #48", "X-Men #1"}


def test_wish_list_title_filter(client):
    r = client.get("/api/comics/wish-list", params={"title": "Fantastic"})
    assert [i["name"] for i in r.json()] == ["Fantastic Four #48"]


def test_wish_list_empty_when_never_imported(client):
    (client.store / "wish-list.json").unlink()
    r = client.get("/api/comics/wish-list")
    assert r.status_code == 200
    assert r.json() == []


# --- status / export -------------------------------------------------------

def test_collection_status(client):
    r = client.get("/api/comics/collection/status")
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 2
    assert body["last_full_import"] == "2026-06-01T00:00:00.000000Z"
    assert "locg_cli_version" in body


def test_collection_export_returns_csv(client):
    r = client.get("/api/comics/collection/export")
    assert r.status_code == 200
    body = r.json()
    # The endpoint returns the file *contents* (read from the server store) so
    # the caller can save + upload them; plus the pending-push counts.
    assert isinstance(body["csv"], str) and body["csv"].strip()  # non-empty CSV (header at least)
    assert "notes_md" in body
    assert isinstance(body["ready_count"], int)


# ===========================================================================
# BUI-92: write endpoints
# ===========================================================================

def _reseed_with_index(store, index):
    """Re-seed collection.json with a series_name_index so record-win resolves
    canonical series without a Metron call."""
    payload = json.loads((store / "collection.json").read_text())
    payload["series_name_index"] = index
    (store / "collection.json").write_text(json.dumps(payload))


_ASM_INDEX = {"amazing spider-man": "The Amazing Spider-Man"}


def test_record_win_appends_and_is_readable(client):
    _reseed_with_index(client.store, _ASM_INDEX)
    win = {
        "item_id": "115500000001",
        "current_bid": "42.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "301", "year": "1988"},
    }
    r = client.post("/api/comics/collection/record-win", json={"wins": [win]})
    assert r.status_code == 200, r.text
    assert r.json()["rows_written"] == 1

    # R8: the append is immediately visible on the next read from the same store.
    # No `year` filter here: a record-win row resolved via series_name_index has
    # no Metron data, so release_date is None — a year-gated check would miss it
    # (pre-existing locg-cli behavior, faithfully wrapped by the endpoint).
    chk = client.get("/api/comics/collection/check", params={"series": "Amazing Spider-Man", "issue": "301"})
    assert chk.json()["match_status"] == "in_collection"


def test_record_win_skips_already_owned(client):
    _reseed_with_index(client.store, _ASM_INDEX)
    win = {
        "item_id": "115500000002",
        "current_bid": "999.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    }
    r = client.post("/api/comics/collection/record-win", json={"wins": [win]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_written"] == 0
    assert body["skipped_already_owned"] >= 1


def test_wish_list_add_appends(client):
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #1"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Daredevil #1" in names


def test_wish_list_add_rejects_empty_title(client):
    assert client.post("/api/comics/wish-list", json={"title": "   "}).status_code == 422
    assert client.post("/api/comics/wish-list", json={}).status_code == 422


def test_import_requires_a_file(client):
    assert client.post("/api/comics/collection/import").status_code == 422


def test_import_rejects_bad_upload(client):
    r = client.post(
        "/api/comics/collection/import",
        files={"file": ("junk.xlsx", b"not a real xlsx", "application/octet-stream")},
    )
    assert r.status_code == 422


def test_import_rejects_over_cap_upload_without_parsing(client):
    """BUI-106: an over-cap upload is rejected with 413 during streaming, before
    the whole body is buffered and handed to the parser. We assert the parser
    (cmd_collection_import) is never reached — proof the abort happens early, not
    after locg-cli's stat()-based 10 MB guard fires on an already-buffered file."""
    from unittest.mock import patch
    from locg.collection_io import MAX_XLSX_BYTES

    oversize = b"\0" * (MAX_XLSX_BYTES + 1)
    with patch("gixen_overlay.routes.cmd_collection_import") as parse:
        r = client.post(
            "/api/comics/collection/import",
            files={"file": ("huge.xlsx", oversize, "application/octet-stream")},
        )
    assert r.status_code == 413
    parse.assert_not_called()


def test_import_applies_xlsx_and_is_readable(client):
    import io
    import openpyxl
    from locg.collection_io import LOCG_XLSX_HEADERS

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(LOCG_XLSX_HEADERS))
    ws.append([
        "Marvel", "X-Men", "X-Men #1", "1963-09-01",
        1, 0, 0, None, "Print", None, None, None, None,
        None, None, None, None, None, None, None, None,
    ])
    buf = io.BytesIO()
    wb.save(buf)

    r = client.post(
        "/api/comics/collection/import",
        files={"file": ("import.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    # import_xlsx merges; the new row is added.
    assert r.json().get("added", 0) >= 1

    chk = client.get("/api/comics/collection/check", params={"series": "X-Men", "issue": "1", "year": "1963"})
    assert chk.json()["match_status"] == "in_collection"


# ===========================================================================
# Review hardening: R11 endpoint guard + import error-class distinction
# ===========================================================================

def test_check_refuses_when_store_never_imported(client):
    """R11 defense-in-depth: an un-imported store must NOT answer 'not owned'
    for everything — the endpoint returns 409, not 200 not_in_cache, so a
    caller that skips the bootstrap guard can't be tricked into a dupe buy."""
    (client.store / "collection.json").unlink()  # never-imported store
    r = client.get("/api/comics/collection/check", params={"series": "Batman", "issue": "1", "year": "1940"})
    assert r.status_code == 409


def test_check_not_in_cache_still_200_when_imported(client):
    """A genuinely-not-owned comic against an *imported* store is still a normal
    200 not_in_cache (the guard only fires on a never-imported store)."""
    r = client.get("/api/comics/collection/check", params={"series": "Batman", "issue": "1", "year": "1940"})
    assert r.status_code == 200
    assert r.json()["match_status"] == "not_in_cache"


def test_import_server_fault_is_not_masked_as_422(client):
    """An unexpected server-side fault during import (e.g. OSError) must NOT be
    caught by the narrow 'bad upload' clause and mislabeled a 422 client error.
    It propagates as a server fault (a 500 in production; the TestClient, with
    raise_server_exceptions=True, re-raises it here) — the point is it is never
    returned as a 422."""
    from unittest.mock import patch
    with patch("gixen_overlay.routes.cmd_collection_import", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            client.post(
                "/api/comics/collection/import",
                files={"file": ("import.xlsx", b"anything", "application/octet-stream")},
            )


# --- BUI-130: wish-list conflict audit + bulk remove + add prevention --------

def test_conflicts_flags_owned_wish_list_item(client):
    """A wish-list entry the user owns is surfaced; an unowned one is not."""
    _seed_wish_list(client.store, [
        {"name": "Amazing Spider-Man #300", "id": 111},  # owned (matches ASM #300)
        {"name": "X-Men #1", "id": None},                # not owned
    ])
    r = client.get("/api/comics/wish-list/conflicts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["checked"] == 2
    assert [c["name"] for c in body["conflicts"]] == ["Amazing Spider-Man #300"]
    assert body["conflicts"][0]["full_title_matched"] == "The Amazing Spider-Man #300"


def test_conflicts_excludes_wish_only_row(client):
    """The default seed wish-lists Fantastic Four #48, which is in_collection=0
    (wish/read but not owned). The copies-owned gate means it is NOT a conflict."""
    r = client.get("/api/comics/wish-list/conflicts")
    assert r.status_code == 200, r.text
    assert r.json()["conflicts"] == []


def test_conflicts_409_when_never_imported(client):
    """An un-imported collection can't answer ownership — refuse rather than
    report a false 'no conflicts' (R11)."""
    (client.store / "collection.json").write_text(json.dumps({
        "schema_version": 1,
        "last_full_import": None,
        "series_name_index": {},
        "comics": [],
    }))
    assert client.get("/api/comics/wish-list/conflicts").status_code == 409


def test_remove_conflicts_removes_only_owned(client):
    _seed_wish_list(client.store, [
        {"name": "Amazing Spider-Man #300", "id": None},  # owned
        {"name": "X-Men #1", "id": None},                 # not owned
    ])
    r = client.post("/api/comics/wish-list/remove-conflicts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_count"] == 1
    assert body["remaining"] == 1
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert names == {"X-Men #1"}


def test_remove_conflicts_409_when_never_imported(client):
    (client.store / "collection.json").write_text(json.dumps({
        "schema_version": 1,
        "last_full_import": None,
        "series_name_index": {},
        "comics": [],
    }))
    assert client.post("/api/comics/wish-list/remove-conflicts").status_code == 409


def test_wish_list_add_rejects_owned_title(client):
    """Part 3: the API boundary refuses to wish-list a book already owned."""
    r = client.post("/api/comics/wish-list", json={"title": "Amazing Spider-Man #300"})
    assert r.status_code == 409, r.text
    # The owned title was not appended.
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Amazing Spider-Man #300" not in names


def test_wish_list_add_force_overrides_owned(client):
    r = client.post(
        "/api/comics/wish-list",
        json={"title": "Amazing Spider-Man #300", "force": True},
    )
    assert r.status_code == 200, r.text
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Amazing Spider-Man #300" in names


def test_wish_list_add_allows_unowned_title(client):
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #181"})
    assert r.status_code == 200, r.text
