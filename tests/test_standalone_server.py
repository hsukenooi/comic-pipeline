"""
Standalone gixen-cli verification tests (PER-35).

Verify the server behaves correctly for generic (non-comic) users.
Tests marked xfail document behavior that requires PER-28/PER-30
(plugin system + comic route decontamination) to be merged before they can pass.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.modify_snipe.return_value = None
    m.remove_snipe.return_value = True
    m.purge_completed.return_value = None
    return m


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    """Server fixture with mocked GixenClient (simulates standalone install)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    # Disable background tasks that require live Gixen session
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    mock_gixen = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock_gixen):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock_gixen
            yield client


# --- R4: Server starts without error ---

def test_server_starts_standalone(standalone):
    """Server health endpoint reachable in standalone mode."""
    r = standalone.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- R1: Generic snipe/bid CRUD works ---

def test_snipes_list_standalone(standalone):
    """GET /api/snipes returns 200 in standalone mode."""
    r = standalone.get("/api/snipes")
    assert r.status_code == 200


def test_bids_list_standalone(standalone):
    """GET /api/bids returns 200 in standalone mode."""
    r = standalone.get("/api/bids")
    assert r.status_code == 200


def test_add_bid_standalone(standalone):
    """POST /api/bids succeeds in standalone mode."""
    r = standalone.post("/api/bids", json={"item_id": "123456789", "max_bid": 25.0})
    assert r.status_code == 200


def test_delete_bid_standalone(standalone):
    """DELETE /api/bids/{item_id} returns expected response in standalone mode."""
    standalone.post("/api/bids", json={"item_id": "987654321", "max_bid": 10.0})
    r = standalone.delete("/api/bids/987654321")
    assert r.status_code in (200, 404)


# --- R2/R3: Plugin system and comic route removal ---
# These tests document the TARGET behavior after PER-28 + PER-30 are merged to main.
# They currently xfail because:
# - gixen/plugins.py does not exist on current main
# - /api/dashboard-tabs route not yet registered in server/main.py
# - /api/comics and /api/extract-comics are still inline in server/main.py


@pytest.mark.xfail(
    reason="Requires PER-28 (dashboard-tabs endpoint) to be merged to main",
    strict=False,
)
def test_dashboard_tabs_empty_without_plugin(standalone):
    """/api/dashboard-tabs returns [] when no comic plugin is installed."""
    r = standalone.get("/api/dashboard-tabs")
    assert r.status_code == 200
    assert r.json() == [], f"Expected no plugin tabs, got: {r.json()}"


@pytest.mark.xfail(
    reason="Requires PER-30 (comic route decontamination) to be merged to main",
    strict=False,
)
def test_comics_route_absent_without_plugin(standalone):
    """/api/comics should return 404 when no comic plugin is installed."""
    r = standalone.get("/api/comics")
    assert r.status_code == 404, (
        "/api/comics is still registered inline in server/main.py — PER-30 not complete"
    )


@pytest.mark.xfail(
    reason="Requires PER-30 (comic route decontamination) to be merged to main",
    strict=False,
)
def test_extract_comics_route_absent_without_plugin(standalone):
    """/api/extract-comics should return 404 when no comic plugin is installed."""
    r = standalone.post("/api/extract-comics")
    assert r.status_code == 404, (
        "/api/extract-comics is still registered inline in server/main.py — PER-30 not complete"
    )
