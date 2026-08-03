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
"""
from __future__ import annotations

from importlib.metadata import EntryPoint
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
