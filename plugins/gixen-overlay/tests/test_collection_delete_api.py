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


def test_delete_single_dateless_row_is_deletable_not_a_false_404(client):
    """S1 follow-up: matched_release_date can be "" (empty string), not just
    None, for an owned-but-dateless row (cmd_collection_check reads it
    straight off the row). The pin must normalize "" and None the same way on
    BOTH sides of the comparison — otherwise the one owned row that IS the
    match gets excluded by its own release_date and the endpoint 404s on a
    book that is, in fact, sitting right there in the collection."""
    _seed_collection(client.store, [{
        "full_title": "Conan the Barbarian #1",
        "series_name": "Conan the Barbarian",
        "publisher_name": "Marvel Comics",
        "release_date": "",  # dateless owned row
        "in_collection": 1,
    }])

    r = client.delete(
        "/api/comics/collection",
        params={"series": "Conan the Barbarian", "issue": "1", "year": "1970"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["removed"]["full_title"] == "Conan the Barbarian #1"

    check = client.get(
        "/api/comics/collection/check",
        params={"series": "Conan the Barbarian", "issue": "1", "year": "1970"},
    ).json()
    assert check["match_status"] == "not_in_cache"


# --- S1 regression: full_title alone is not unique across eras -------------
#
# LOCG's full_title carries no year (BUI-199 — "Hulk #1", not "Hulk (2008)
# #1"), so a collection holding both the 1962 and 2008 "Hulk #1" has TWO owned
# rows with an identical full_title. Locating the row to delete by full_title
# alone would silently touch whichever row happens to come first in the list,
# not necessarily the era the `year` param asked for. The endpoint must pin on
# `matched_release_date` too so it deletes the SAME row cmd_collection_check
# resolved via its year gate.

_HULK_TWO_ERAS = [
    {
        "full_title": "Hulk #1",
        "series_name": "Hulk",
        "publisher_name": "Marvel Comics",
        "release_date": "1962-05-01",
        "in_collection": 1,
    },
    {
        "full_title": "Hulk #1",
        "series_name": "Hulk",
        "publisher_name": "Marvel Comics",
        "release_date": "2008-06-01",
        "in_collection": 1,
    },
]


def test_delete_pins_by_release_date_removes_requested_era(client):
    _seed_collection(client.store, _HULK_TWO_ERAS)

    r = client.delete("/api/comics/collection", params={"series": "Hulk", "issue": "1", "year": "2008"})
    assert r.status_code == 200, r.text
    assert r.json()["removed"]["release_date"] == "2008-06-01"

    # The 1962 copy survives untouched.
    survivor = client.get(
        "/api/comics/collection/check", params={"series": "Hulk", "issue": "1", "year": "1962"}
    ).json()
    assert survivor["match_status"] == "in_collection"
    assert survivor["matched_release_date"] == "1962-05-01"

    # The 2008 copy is really gone — not just decremented, each row here is a
    # single copy.
    gone = client.get(
        "/api/comics/collection/check", params={"series": "Hulk", "issue": "1", "year": "2008"}
    ).json()
    assert gone["match_status"] == "not_in_cache"


def test_delete_pins_by_release_date_reverse_era(client):
    """Same setup, deleting the OTHER era — proves the pin follows the `year`
    param, not a fixed first/last-row bias."""
    _seed_collection(client.store, _HULK_TWO_ERAS)

    r = client.delete("/api/comics/collection", params={"series": "Hulk", "issue": "1", "year": "1962"})
    assert r.status_code == 200, r.text
    assert r.json()["removed"]["release_date"] == "1962-05-01"

    survivor = client.get(
        "/api/comics/collection/check", params={"series": "Hulk", "issue": "1", "year": "2008"}
    ).json()
    assert survivor["match_status"] == "in_collection"
    assert survivor["matched_release_date"] == "2008-06-01"


def test_delete_dry_run_pins_by_release_date(client):
    """The preview uses the same pinning predicate as the real delete, so it
    previews the era actually requested, not just the first row by title."""
    _seed_collection(client.store, _HULK_TWO_ERAS)

    r = client.delete(
        "/api/comics/collection",
        params={"series": "Hulk", "issue": "1", "year": "2008", "dry_run": "true"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["would_remove"]["release_date"] == "2008-06-01"

    # Both eras still owned — a preview never mutates.
    for yr, date in (("1962", "1962-05-01"), ("2008", "2008-06-01")):
        check = client.get(
            "/api/comics/collection/check", params={"series": "Hulk", "issue": "1", "year": yr}
        ).json()
        assert check["match_status"] == "in_collection"
        assert check["matched_release_date"] == date


def test_delete_409_when_pin_is_ambiguous_across_dateless_duplicates(client):
    """Two owned rows share both full_title AND a missing release_date — the
    pin can't disambiguate them, so the endpoint refuses with 409 rather than
    silently deleting whichever comes first."""
    dupes = [
        {
            "full_title": "Uncanny X-Men #142",
            "series_name": "Uncanny X-Men",
            "publisher_name": "Marvel Comics",
            "release_date": None,
            "in_collection": 1,
        },
        {
            "full_title": "Uncanny X-Men #142",
            "series_name": "Uncanny X-Men",
            "publisher_name": "Marvel Comics",
            "release_date": None,
            "in_collection": 1,
        },
    ]
    _seed_collection(client.store, dupes)

    collection_path = client.store / "collection.json"
    before = collection_path.read_bytes()

    r = client.delete("/api/comics/collection", params={"series": "Uncanny X-Men", "issue": "142"})
    assert r.status_code == 409

    # Refusing to guess means no mutation happened — both rows survive.
    check = client.get(
        "/api/comics/collection/check", params={"series": "Uncanny X-Men", "issue": "142"}
    ).json()
    assert check["match_status"] == "in_collection"

    # A refused (409) delete is a true no-op: the cheap pre-check refuses
    # BEFORE cache.apply() runs, so it never rotates the .bak ring or rewrites
    # last_writer metadata for a delete that never actually happened. Assert
    # the store file is byte-for-byte unchanged, and that no backup was
    # created (repeated ambiguous calls must not churn/evict the backup ring).
    assert collection_path.read_bytes() == before
    assert not (client.store / "collection.json.bak.0").exists()


def test_delete_dry_run_409_when_pin_is_ambiguous(client):
    dupes = [
        {
            "full_title": "Uncanny X-Men #142",
            "series_name": "Uncanny X-Men",
            "publisher_name": "Marvel Comics",
            "release_date": None,
            "in_collection": 1,
        },
        {
            "full_title": "Uncanny X-Men #142",
            "series_name": "Uncanny X-Men",
            "publisher_name": "Marvel Comics",
            "release_date": None,
            "in_collection": 1,
        },
    ]
    _seed_collection(client.store, dupes)

    r = client.delete(
        "/api/comics/collection",
        params={"series": "Uncanny X-Men", "issue": "142", "dry_run": "true"},
    )
    assert r.status_code == 409
