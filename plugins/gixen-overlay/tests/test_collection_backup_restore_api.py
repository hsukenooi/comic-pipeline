"""Tests for the BUI-433 collection backup/restore API endpoints.

`/comic:collection-sync` Step 1 used to back up the store with a client-
orchestrated `cp -r` + `ssh` + `case "$(hostname)"` MacBook/Mac-Mini branch,
and the restore path was prose-only (no command). These endpoints move the
backup onto the server (which already has filesystem access to the store) and
make restore executable. Mirrors the `client` fixture pattern in
test_collection_api.py, pointing locg-cli's store (LOCG_DATA_DIR) at a seeded
temp directory.
"""
from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from pathlib import Path
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


def _seed_wish_list(store, items):
    (store / "wish-list.json").write_text(
        json.dumps({"updated_at": "2026-06-01T00:00:00Z", "items": items})
    )


_OWNED = [
    {
        "full_title": "The Amazing Spider-Man #300",
        "series_name": "The Amazing Spider-Man",
        "publisher_name": "Marvel Comics",
        "release_date": "1988-05-01",
        "in_collection": 1,
    },
]

_WISH = [{"name": "Batman #1", "id": 12345}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    _seed_collection(store, _OWNED)
    _seed_wish_list(store, _WISH)

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


# --- backup ------------------------------------------------------------

def test_backup_returns_durable_path_and_nonempty_sanity_count(client):
    r = client.post("/api/comics/collection/backup")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["comics_count"] == 1
    assert body["wish_list_count"] == 1
    assert body["files"]["collection.json"] > 0
    assert body["files"]["wish-list.json"] > 0
    backup_path = Path(body["backup_path"])
    assert backup_path.is_dir()
    assert (backup_path / "collection.json").exists()
    assert (backup_path / "wish-list.json").exists()


def test_backup_path_is_distinct_from_rotating_bak_ring(client):
    """The named backup directory must not live inside the store dir where
    CollectionCache's in-store .bak.0/1/2 rotation happens."""
    r = client.post("/api/comics/collection/backup")
    backup_path = Path(r.json()["backup_path"])
    assert client.store not in backup_path.parents, (
        "the named backup must not live inside the store dir — it would be "
        "reachable by (and confusable with) the in-store .bak.N ring"
    )
    assert backup_path.parent.name == "store-backups"


def test_two_backups_get_distinct_paths(client):
    """Two backup calls must not collide/overwrite each other."""
    r1 = client.post("/api/comics/collection/backup")
    r2 = client.post("/api/comics/collection/backup")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["backup_path"] != r2.json()["backup_path"]
    # Both must still be independently readable afterwards.
    assert Path(r1.json()["backup_path"]).is_dir()
    assert Path(r2.json()["backup_path"]).is_dir()


def test_backup_survives_subsequent_store_churn(client):
    """BUI-433 constraint: a backup taken now must survive later mutations
    that rotate/evict the in-store .bak ring (only 3 generations deep)."""
    r = client.post("/api/comics/collection/backup")
    backup_path = Path(r.json()["backup_path"])
    pre_churn = (backup_path / "collection.json").read_bytes()

    # Churn the store past the 3-generation .bak ring depth via
    # record-win/commit (BUI-453: the old standalone record-win endpoint was
    # removed; commit is the sole write path now, and equally exercises the
    # store-write/.bak-rotation logic this test is churning against).
    for i in range(5):
        client.post(
            "/api/comics/collection/record-win/commit",
            json={"wins": [{
                "item_id": f"churn-{i}",
                "current_bid": 1.0,
                "end_date_iso": "2026-07-01T00:00:00Z",
                "identify_data": {"series": f"Churn Series {i}", "issue": "1", "year": "2020"},
            }]},
        )

    assert (backup_path / "collection.json").read_bytes() == pre_churn


def test_backup_hard_fails_on_empty_store(client):
    """An empty/never-established store must not report backup success —
    Step 1 needs to STOP on failure, never silently proceed."""
    (client.store / "collection.json").unlink()
    (client.store / "wish-list.json").unlink()
    r = client.post("/api/comics/collection/backup")
    assert r.status_code == 500


# --- restore -------------------------------------------------------------

def test_restore_round_trips_a_store(client):
    """BUI-433 acceptance: restore must round-trip a backed-up store."""
    backup = client.post("/api/comics/collection/backup").json()

    # Simulate a bad destructive write after the backup (e.g. a botched sync).
    (client.store / "collection.json").write_text(json.dumps({
        "schema_version": 1,
        "last_full_import": "2026-06-01T00:00:00.000000Z",
        "last_import_source": "seed.xlsx",
        "migration_in_progress": False,
        "last_writer": None,
        "series_name_index": {},
        "comics": [],
    }))
    _seed_wish_list(client.store, [])

    r = client.post(
        "/api/comics/collection/restore",
        json={"backup_path": backup["backup_path"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["comics_count"] == 1
    assert body["wish_list_count"] == 1

    live = json.loads((client.store / "collection.json").read_text())
    assert {c["full_title"] for c in live["comics"]} == {"The Amazing Spider-Man #300"}
    live_wish = json.loads((client.store / "wish-list.json").read_text())
    assert {i["name"] for i in live_wish["items"]} == {"Batman #1"}

    # And the restored store is usable again through the normal read path.
    check = client.get(
        "/api/comics/collection/check",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    ).json()
    assert check["match_status"] == "in_collection"


def test_restore_rejects_path_outside_backups_root(client):
    """backup_path must be a path this server itself returned — an arbitrary
    filesystem path (path traversal / unrelated directory) is refused, not
    silently accepted."""
    r = client.post(
        "/api/comics/collection/restore",
        json={"backup_path": "/etc"},
    )
    assert r.status_code == 422


def test_restore_rejects_path_traversal_out_of_backups_root(client):
    backup = client.post("/api/comics/collection/backup").json()
    traversal = str(Path(backup["backup_path"]) / ".." / ".." / "store")
    r = client.post(
        "/api/comics/collection/restore",
        json={"backup_path": traversal},
    )
    assert r.status_code == 422


def test_restore_missing_backup_dir_returns_404(client):
    backups_root = client.store.parent / "store-backups"
    r = client.post(
        "/api/comics/collection/restore",
        json={"backup_path": str(backups_root / "does-not-exist")},
    )
    assert r.status_code == 404


def test_restore_rejects_empty_backup_path(client):
    r = client.post("/api/comics/collection/restore", json={"backup_path": ""})
    assert r.status_code == 422
    r2 = client.post("/api/comics/collection/restore", json={})
    assert r2.status_code == 422
