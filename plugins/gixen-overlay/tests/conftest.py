"""Shared fixtures for the gixen-overlay test suite.

BUI-630: the `api` fixture (the real comics server — `server.main.app` — with
the real overlay plugin wired in via the same entry-point-discovery path
production uses) was hand-copied across three files: test_gixen_overlay_routes.py,
test_heartbeats.py, and test_rejected_writes_ledger.py. The three copies had
drifted slightly (a different subset of `GixenClient` methods stubbed on the
mock, and test_gixen_overlay_routes.py alone stashed the mock on
`client.mock_gixen`, which nothing reads). None of that drift was load-bearing
for any test: `_ensure_collection_store()` (routes.py) sets `LOCG_DATA_DIR`
itself whenever it is unset, so every collection/wish-list endpoint call is
tmp_path-isolated regardless of whether the fixture also sets it up front; and
no test in any of the three files calls `add_snipe`/`modify_snipe`/
`remove_snipe` or reads `client.mock_gixen`. This fixture is the union of all
three copies (every stub any of them used), so consolidating here changes no
test's observable behavior.

BUI-640: a second, similar hand-copied pattern — feeding a `client` fixture
(not `api`) that additionally seeds a locg-cli collection/wish-list store —
was independently duplicated across test_collection_api.py,
test_collection_delete_api.py, test_collection_backup_restore_api.py, and
test_collection_remediate_api.py. Checked for divergence the way BUI-630 did:
`_install_real_plugin`, `_mock_gixen`, `_seed_collection`, and the
env-setup/patch/TestClient block were byte-identical across all four (now
factored into `_seeded_client` below); `_seed_wish_list` was byte-identical in
the two files that had it. What differs, and stays put, is DATA: which
comics/wish-list items each file seeds, and whether a file seeds a wish list
at all (test_collection_delete_api.py and test_collection_remediate_api.py
seed no wish list — forcing one on them via a shared fixture would silently
add untested wish-list state to every delete/remediate test, not a pure
refactor). So `_seed_wish_list` is NOT called from `_seeded_client`; each
file's own `client` fixture still calls it explicitly, only where it already
did. test_collection_api.py also keeps its own separate
`server_default_store_client` fixture (BUI-476) that deliberately does NOT
set `LOCG_DATA_DIR` — that fixture is a distinct pattern by design, not more
of this drift, so it stays inline in that file, just reusing these helpers
instead of redefining them.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from importlib.metadata import EntryPoint
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _install_real_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _mock_gixen() -> MagicMock:
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.modify_snipe.return_value = None
    m.remove_snipe.return_value = True
    m.purge_completed.return_value = None
    return m


def _seed_collection(store: Path, comics: list[dict]) -> None:
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


def _seed_wish_list(store: Path, items: list[dict]) -> None:
    (store / "wish-list.json").write_text(
        json.dumps({"updated_at": "2026-06-01T00:00:00Z", "items": items})
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The real comics server with the real overlay plugin loaded."""
    _install_real_plugin(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path / "store"))
    mock = _mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock
            yield client


@contextmanager
def _seeded_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Path):
    """The real comics server + overlay plugin, LOCG_DATA_DIR pointed at a
    pre-seeded collection/wish-list store directory.

    Callers seed `store` (via `_seed_collection` / `_seed_wish_list`) before
    entering this context manager. The yielded TestClient carries `.store` so
    tests can reseed mid-test or read the store files directly (many do).
    """
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
