"""Tests for the BUI-254 single-entry collection DELETE endpoint.

Mirrors the fixture pattern in test_collection_api.py (real gixen-cli server +
overlay plugin, LOCG_DATA_DIR pointed at a seeded temp store), kept in its own
file per the wave's ownership split: another agent is actively editing
test_collection_api.py this wave.
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


# One single-copy row and one two-copy row, so the tests exercise both the
# "remove outright" and "decrement" branches of the in_collection copy count
# (BUI-249/250/251: in_collection is a copies-owned count, not a boolean).
_OWNED = [
    {
        "full_title": "The Amazing Spider-Man #300",
        "series_name": "The Amazing Spider-Man",
        "publisher_name": "Marvel Comics",
        "release_date": "1988-05-01",
        "in_collection": 1,
    },
    {
        "full_title": "Batman #1",
        "series_name": "Batman",
        "publisher_name": "DC Comics",
        "release_date": "1940-04-25",
        "in_collection": 2,
    },
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    _seed_collection(store, _OWNED)

    _install_real_plugin(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    with patch("server.main.GixenClient", return_value=_mock_gixen()):
        from server.main import app

        with TestClient(app) as c:
            c.store = store
            yield c


def test_delete_removes_single_copy_row(client):
    """A single-copy row is hard-deleted outright, and the full removed record
    is returned so a mistake can be manually reversed (record-win/re-import)."""
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["action"] == "removed"
    assert body["removed"]["full_title"] == "The Amazing Spider-Man #300"
    assert body["remaining_copies"] == 0

    check = client.get(
        "/api/comics/collection/check",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    ).json()
    assert check["match_status"] == "not_in_cache"


def test_delete_logs_the_removed_record_to_the_audit_log(client):
    """The full removed record is also logged (not just returned), so a
    mistaken hard delete can be reversed even without a live tombstone row."""
    client.delete(
        "/api/comics/collection",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    )
    lines = (client.store / "import-history.jsonl").read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    delete_records = [r for r in records if r["type"] == "collection_delete"]
    assert len(delete_records) == 1
    assert delete_records[0]["details"]["removed"]["full_title"] == "The Amazing Spider-Man #300"
    assert delete_records[0]["details"]["action"] == "removed"


def test_delete_decrements_a_multi_copy_row_instead_of_removing_it(client):
    """A row with more than one copy is decremented, not removed outright, so
    deleting one erroneous copy doesn't un-own the others."""
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Batman", "issue": "1", "year": "1940"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "decremented"
    assert body["removed"]["in_collection"] == 2  # the pre-mutation snapshot
    assert body["remaining_copies"] == 1

    # Still owned — the other copy remains.
    check = client.get(
        "/api/comics/collection/check",
        params={"series": "Batman", "issue": "1", "year": "1940"},
    ).json()
    assert check["match_status"] == "in_collection"


def test_delete_404_when_not_owned(client):
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Fantastic Four", "issue": "1", "year": "1961"},
    )
    assert r.status_code == 404
    # Untouched.
    check = client.get(
        "/api/comics/collection/check",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    ).json()
    assert check["match_status"] == "in_collection"


def test_delete_422_when_series_or_issue_blank(client):
    assert client.delete(
        "/api/comics/collection", params={"series": "  ", "issue": "300"}
    ).status_code == 422
    assert client.delete(
        "/api/comics/collection", params={"series": "Batman", "issue": "  "}
    ).status_code == 422


def test_delete_dry_run_previews_without_removing(client):
    """dry_run=true returns what WOULD be removed but leaves the store
    untouched — the preview half of the dry-run-then-confirm safeguard."""
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988", "dry_run": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview"
    assert body["action"] == "would_remove"
    assert body["would_remove"]["full_title"] == "The Amazing Spider-Man #300"

    # Still owned — nothing was actually mutated.
    check = client.get(
        "/api/comics/collection/check",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    ).json()
    assert check["match_status"] == "in_collection"

    # No audit entry either — a preview is a pure read.
    history_path = client.store / "import-history.jsonl"
    if history_path.exists():
        records = [json.loads(line) for line in history_path.read_text().strip().splitlines()]
        assert not any(r["type"] == "collection_delete" for r in records)


def test_delete_dry_run_on_multi_copy_row_previews_decrement(client):
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Batman", "issue": "1", "year": "1940", "dry_run": "true"},
    )
    body = r.json()
    assert body["action"] == "would_decrement"
    assert body["remaining_copies"] == 1


def test_delete_dry_run_404_when_not_owned(client):
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Fantastic Four", "issue": "1", "year": "1961", "dry_run": "true"},
    )
    assert r.status_code == 404


def test_delete_409_when_never_imported(client):
    """R11: refuse to answer a delete request against a store with no import
    yet — an un-imported store would 404 every delete, which looks like a
    successful (if unusual) 'nothing to remove' rather than the real problem."""
    (client.store / "collection.json").unlink()
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    )
    assert r.status_code == 409


def test_delete_409_when_never_imported_dry_run_too(client):
    (client.store / "collection.json").unlink()
    r = client.delete(
        "/api/comics/collection",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988", "dry_run": "true"},
    )
    assert r.status_code == 409
