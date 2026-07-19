"""Tests for the BUI-427 matcher-bypassing collection remediation endpoints.

`POST /api/comics/collection/remediate/delete` and
`POST /api/comics/collection/remediate/set-copies` locate their target row by
STABLE IDENTITY (`gixen_item_id`, or `full_title` + `release_date` +
`source`) — never through `cmd_collection_check`'s masthead-alias /
X-Men-split / leading-article matcher (the BUI-254 `DELETE
/api/comics/collection` endpoint's matcher, which is exactly what can't
disambiguate a volume-mis-filed row — see BUI-424). Mirrors the fixture
pattern in test_collection_delete_api.py, kept in its own file per that
file's documented ownership-split convention.
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


# Two rows sharing the SAME masthead-alias series+issue (a check-matcher could
# resolve either as "Thor #127"), distinguished only by gixen_item_id.
_THOR_TWINS = [
    {
        "full_title": "The Mighty Thor #127",
        "series_name": "The Mighty Thor (Vol. 3)",
        "publisher_name": "Marvel Comics",
        "release_date": "2015-01-01",
        "in_collection": 1,
        "gixen_item_id": "mighty-thor-127",
        "source": "agent_win",
    },
    {
        "full_title": "Thor #127",
        "series_name": "Thor (Vol. 1)",
        "publisher_name": "Marvel Comics",
        "release_date": "1966-04-01",
        "in_collection": 1,
        "gixen_item_id": "thor-127",
        "source": "locg_export",
    },
]

# BUI-424 duplicate-twin case: same full_title + release_date, different source.
_BATMAN_DUPLICATE_TWINS = [
    {
        "full_title": "Batman #1",
        "series_name": "Batman",
        "publisher_name": "DC Comics",
        "release_date": "1940-04-25",
        "in_collection": 1,
        "gixen_item_id": "win-1",
        "source": "agent_win",
    },
    {
        "full_title": "Batman #1",
        "series_name": "Batman",
        "publisher_name": "DC Comics",
        "release_date": "1940-04-25",
        "in_collection": 1,
        "gixen_item_id": "export-1",
        "source": "locg_export",
    },
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    _seed_collection(store, _THOR_TWINS + _BATMAN_DUPLICATE_TWINS)

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


def _comics(client) -> list[dict]:
    payload = json.loads((client.store / "collection.json").read_text())
    return payload["comics"]


# ---------------------------------------------------------------------------
# delete — identity resolution
# ---------------------------------------------------------------------------


def test_delete_by_gixen_item_id_targets_exact_row_not_fuzzy_twin(client):
    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "mighty-thor-127"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["action"] == "removed"
    assert body["removed"]["gixen_item_id"] == "mighty-thor-127"

    remaining_ids = {row["gixen_item_id"] for row in _comics(client)}
    assert "mighty-thor-127" not in remaining_ids
    assert "thor-127" in remaining_ids  # the alias twin survives untouched


def test_delete_by_full_title_release_date_source_hits_only_matching_twin(client):
    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={
            "full_title": "Batman #1",
            "release_date": "1940-04-25",
            "source": "agent_win",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed"]["gixen_item_id"] == "win-1"

    remaining_ids = {row["gixen_item_id"] for row in _comics(client)}
    assert "win-1" not in remaining_ids
    assert "export-1" in remaining_ids  # the locg_export twin survives


def test_delete_decrements_multi_copy_row(client):
    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "thor-127", "dry_run": False},
    )
    # single-copy row -> removed outright; bump to multi-copy first via set-copies
    assert r.json()["action"] == "removed"

    # Re-seed a multi-copy row and confirm decrement path.
    _seed_collection(client.store, [{**_THOR_TWINS[1], "in_collection": 2}])
    r2 = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "thor-127"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["action"] == "decremented"
    assert r2.json()["remaining_copies"] == 1


def test_delete_404_not_found_never_touches_wrong_row(client):
    before = (client.store / "collection.json").read_bytes()
    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "does-not-exist"},
    )
    assert r.status_code == 404
    assert (client.store / "collection.json").read_bytes() == before


def test_delete_409_ambiguous_when_multiple_rows_share_identity(client):
    _seed_collection(client.store, [
        {"full_title": "X #1", "release_date": None, "source": None, "gixen_item_id": "a", "in_collection": 1},
        {"full_title": "X #1", "release_date": None, "source": None, "gixen_item_id": "b", "in_collection": 1},
    ])
    before = (client.store / "collection.json").read_bytes()

    r = client.post("/api/comics/collection/remediate/delete", json={"full_title": "X #1"})

    assert r.status_code == 409
    assert (client.store / "collection.json").read_bytes() == before


def test_delete_422_when_neither_identity_given(client):
    r = client.post("/api/comics/collection/remediate/delete", json={})
    assert r.status_code == 422


def test_delete_422_when_both_identities_given(client):
    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "thor-127", "full_title": "Thor #127"},
    )
    assert r.status_code == 422


def test_delete_409_when_never_imported(client):
    (client.store / "collection.json").unlink()
    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "thor-127"},
    )
    assert r.status_code == 409


def test_delete_dry_run_previews_without_mutating(client):
    before = (client.store / "collection.json").read_bytes()

    r = client.post(
        "/api/comics/collection/remediate/delete",
        json={"gixen_item_id": "thor-127", "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview"
    assert body["action"] == "would_remove"
    assert body["row"]["gixen_item_id"] == "thor-127"

    assert (client.store / "collection.json").read_bytes() == before
    remaining_ids = {row["gixen_item_id"] for row in _comics(client)}
    assert "thor-127" in remaining_ids

    history_path = client.store / "import-history.jsonl"
    if history_path.exists():
        records = [json.loads(line) for line in history_path.read_text().strip().splitlines()]
        assert not any(r["type"] == "collection_remediate_delete" for r in records)


def test_delete_logs_to_audit_trail(client):
    client.post("/api/comics/collection/remediate/delete", json={"gixen_item_id": "thor-127"})

    records = [
        json.loads(line)
        for line in (client.store / "import-history.jsonl").read_text().strip().splitlines()
    ]
    delete_records = [r for r in records if r["type"] == "collection_remediate_delete"]
    assert len(delete_records) == 1
    assert delete_records[0]["details"]["removed"]["gixen_item_id"] == "thor-127"


# ---------------------------------------------------------------------------
# set-copies
# ---------------------------------------------------------------------------


def test_set_copies_to_explicit_value(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["previous_in_collection"] == 1
    assert body["new_in_collection"] == 5

    rows = {row["gixen_item_id"]: row for row in _comics(client)}
    assert rows["thor-127"]["in_collection"] == 5
    assert rows["mighty-thor-127"]["in_collection"] == 1  # untouched


def test_set_copies_by_full_title_release_date_source_hits_only_matching_twin(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={
            "full_title": "Batman #1",
            "release_date": "1940-04-25",
            "source": "agent_win",
            "in_collection": 7,
        },
    )
    assert r.status_code == 200, r.text

    rows = {row["gixen_item_id"]: row for row in _comics(client)}
    assert rows["win-1"]["in_collection"] == 7
    assert rows["export-1"]["in_collection"] == 1  # untouched


def test_set_copies_with_delta(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "delta": 3},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_in_collection"] == 4


def test_set_copies_zero_does_not_delete_the_row(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": 0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_in_collection"] == 0

    rows = {row["gixen_item_id"]: row for row in _comics(client)}
    assert "thor-127" in rows
    assert rows["thor-127"]["in_collection"] == 0


def test_set_copies_negative_delta_refused_not_clamped(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "delta": -5},
    )
    assert r.status_code == 422
    rows = {row["gixen_item_id"]: row for row in _comics(client)}
    assert rows["thor-127"]["in_collection"] == 1  # untouched


def test_set_copies_422_negative_absolute_value(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": -1},
    )
    assert r.status_code == 422


def test_set_copies_422_when_both_in_collection_and_delta_given(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": 2, "delta": 1},
    )
    assert r.status_code == 422


def test_set_copies_422_when_neither_in_collection_nor_delta_given(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127"},
    )
    assert r.status_code == 422


def test_set_copies_404_not_found(client):
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "does-not-exist", "in_collection": 5},
    )
    assert r.status_code == 404


def test_set_copies_dry_run_previews_without_mutating(client):
    before = (client.store / "collection.json").read_bytes()

    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": 9, "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview"
    assert body["current_in_collection"] == 1
    assert body["new_in_collection"] == 9

    assert (client.store / "collection.json").read_bytes() == before
    rows = {row["gixen_item_id"]: row for row in _comics(client)}
    assert rows["thor-127"]["in_collection"] == 1

    history_path = client.store / "import-history.jsonl"
    if history_path.exists():
        records = [json.loads(line) for line in history_path.read_text().strip().splitlines()]
        assert not any(r["type"] == "collection_remediate_set_copies" for r in records)


def test_set_copies_409_when_never_imported(client):
    (client.store / "collection.json").unlink()
    r = client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": 5},
    )
    assert r.status_code == 409


def test_set_copies_logs_to_audit_trail(client):
    client.post(
        "/api/comics/collection/remediate/set-copies",
        json={"gixen_item_id": "thor-127", "in_collection": 4},
    )

    records = [
        json.loads(line)
        for line in (client.store / "import-history.jsonl").read_text().strip().splitlines()
    ]
    set_records = [r for r in records if r["type"] == "collection_remediate_set_copies"]
    assert len(set_records) == 1
    assert set_records[0]["details"]["new_in_collection"] == 4
