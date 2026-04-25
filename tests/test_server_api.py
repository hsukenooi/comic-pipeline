"""HTTP endpoint tests — GixenClient is mocked, DB uses tmp_path."""
import pytest
from unittest.mock import MagicMock, patch
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
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock
            yield client


def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_upsert_comic(api):
    r = api.post("/api/comics", json={
        "title": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0,
        "fmv_high": 1000.0,
        "fmv_comps": 12,
        "fmv_confidence": "high",
        "fmv_notes": "Key issue",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["id"] > 0
    assert data["title"] == "Amazing Spider-Man"


def test_upsert_comic_twice_updates(api):
    payload = {"title": "X-Men", "issue": "1", "year": 1963,
               "grade": 8.0, "fmv_low": 500.0, "fmv_high": 700.0,
               "fmv_comps": 5, "fmv_confidence": "medium", "fmv_notes": ""}
    r1 = api.post("/api/comics", json=payload)
    payload["fmv_low"] = 550.0
    r2 = api.post("/api/comics", json=payload)
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["fmv_low"] == 550.0


def test_upsert_comic_missing_required_field(api):
    r = api.post("/api/comics", json={"title": "X-Men", "issue": "1"})  # missing year
    assert r.status_code == 422


def test_add_bid_no_comic(api):
    r = api.post("/api/bids", json={
        "item_id": "123456789",
        "max_bid": 50.0,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["item_id"] == "123456789"
    assert data["status"] == "PENDING"
    api.mock_gixen.add_snipe.assert_called_once()


def test_add_bid_with_comic_links_fmv(api):
    r = api.post("/api/bids", json={
        "item_id": "987654321",
        "max_bid": 800.0,
        "comic": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0,
        "fmv_high": 1000.0,
        "fmv_comps": 12,
        "fmv_confidence": "high",
        "fmv_notes": "",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["comic_id"] is not None


def test_add_bid_invalid_item_id(api):
    r = api.post("/api/bids", json={"item_id": "abc", "max_bid": 50.0})
    assert r.status_code == 422


def test_add_bid_negative_max_bid(api):
    r = api.post("/api/bids", json={"item_id": "123456789", "max_bid": -10.0})
    assert r.status_code == 422


def test_add_bid_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.add_snipe.side_effect = GixenError("Gixen down")
    r = api.post("/api/bids", json={"item_id": "111222333", "max_bid": 50.0})
    assert r.status_code == 503


def test_get_snipes_empty(api):
    api.mock_gixen.list_snipes.return_value = []
    r = api.get("/api/snipes")
    assert r.status_code == 200
    assert r.json() == []


def test_get_snipes_merges_fmv(api):
    # Add a bid with comic context first
    api.post("/api/bids", json={
        "item_id": "555666777",
        "max_bid": 60.0,
        "comic": "Hulk",
        "issue": "181",
        "year": 1974,
        "grade": 9.0,
        "fmv_low": 50.0,
        "fmv_high": 70.0,
        "fmv_comps": 8,
        "fmv_confidence": "high",
        "fmv_notes": "",
    })
    # Mock Gixen returning the same item
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "555666777",
        "title": "Incredible Hulk #181",
        "max_bid": "60.00 USD",
        "current_bid": "45.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "5h 0m",
        "seller": "comicseller",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "abc123",
    }]
    r = api.get("/api/snipes")
    assert r.status_code == 200
    snipes = r.json()
    assert len(snipes) == 1
    assert snipes[0]["item_id"] == "555666777"
    assert snipes[0]["fmv_low"] == 50.0
    assert snipes[0]["fmv_confidence"] == "high"


def test_get_snipes_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.list_snipes.side_effect = GixenError("Gixen down")
    r = api.get("/api/snipes")
    assert r.status_code == 503


def test_edit_bid(api):
    api.post("/api/bids", json={"item_id": "200000001", "max_bid": 50.0})
    r = api.patch("/api/bids/200000001", json={"max_bid": 75.0, "bid_offset": 10, "snipe_group": 0})
    assert r.status_code == 200
    assert r.json()["max_bid"] == 75.0
    api.mock_gixen.modify_snipe.assert_called_once()


def test_edit_bid_not_found(api):
    from gixen_client import GixenSnipeNotFoundError
    api.mock_gixen.modify_snipe.side_effect = GixenSnipeNotFoundError("not found")
    r = api.patch("/api/bids/999999999", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 404


def test_remove_bid(api):
    api.post("/api/bids", json={"item_id": "300000001", "max_bid": 50.0})
    r = api.delete("/api/bids/300000001")
    assert r.status_code == 200
    api.mock_gixen.remove_snipe.assert_called_once()


def test_purge(api):
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 200
    data = r.json()
    assert "purged_completed" in data
    assert "removed_siblings" in data
    api.mock_gixen.list_snipes.assert_called()
    api.mock_gixen.purge_completed.assert_called_once()


def test_sync_captures_won_status(api):
    """Sync updates bid status when Gixen reports WON."""
    # Add a bid so there's a DB record
    api.post("/api/bids", json={"item_id": "400000001", "max_bid": 50.0})

    # Mock Gixen returning the item as WON
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "400000001",
        "title": "Test",
        "max_bid": "50.00 USD",
        "current_bid": "42.00 USD",
        "status": "WON",
        "time_to_end": "ENDED",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "xyz",
    }]

    # Trigger sync via purge (which calls _sync_gixen internally).
    # _sync_gixen sets the bid to WON; purge then marks it PURGED and
    # returns purged_completed >= 1.
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.json()["purged_completed"] >= 1


def test_edit_bid_non_numeric_item_id(api):
    r = api.patch("/api/bids/abc", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 422


def test_remove_bid_non_numeric_item_id(api):
    r = api.delete("/api/bids/abc")
    assert r.status_code == 422


def test_edit_bid_not_in_db_returns_synthetic(api):
    """PATCH succeeds on Gixen but item has no DB row — returns 3-key synthetic response."""
    r = api.patch("/api/bids/999000001", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "PENDING"
    assert set(data.keys()) == {"item_id", "max_bid", "status"}


def test_purge_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.purge_completed.side_effect = GixenError("Gixen down")
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 503


def test_upsert_comic_invalid_confidence(api):
    r = api.post("/api/comics", json={
        "title": "X-Men", "issue": "1", "year": 1963,
        "fmv_confidence": "very_high",
    })
    assert r.status_code == 422


def test_remove_bid_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.remove_snipe.side_effect = GixenError("network error")
    api.post("/api/bids", json={"item_id": "700000001", "max_bid": 50.0})
    r = api.delete("/api/bids/700000001")
    assert r.status_code == 503


def test_edit_bid_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.modify_snipe.side_effect = GixenError("network error")
    r = api.patch("/api/bids/800000001", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 503


def test_purge_removes_siblings(api):
    """Sibling loop executes when a group has a WON snipe."""
    api.post("/api/bids", json={"item_id": "500000001", "max_bid": 50.0})
    api.mock_gixen.list_snipes.return_value = [
        {
            "item_id": "500000001", "status": "WON", "snipe_group": "1",
            "title": "Comic A", "max_bid": "50.00 USD", "current_bid": "45.00 USD",
            "time_to_end": "ENDED", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "a1",
        },
        {
            "item_id": "500000002", "status": "SCHEDULED", "snipe_group": "1",
            "title": "Comic A alt", "max_bid": "50.00 USD", "current_bid": "0.00 USD",
            "time_to_end": "5h 0m", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "b2",
        },
    ]
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 200
    assert r.json()["removed_siblings"] == 1
    api.mock_gixen.remove_snipe.assert_called_once_with("500000002")


def test_purge_sibling_failure_swallowed(api):
    """GixenError from sibling removal is swallowed; response still 200."""
    from gixen_client import GixenError
    api.post("/api/bids", json={"item_id": "600000001", "max_bid": 50.0})
    api.mock_gixen.list_snipes.return_value = [
        {
            "item_id": "600000001", "status": "WON", "snipe_group": "2",
            "title": "Comic B", "max_bid": "50.00 USD", "current_bid": "40.00 USD",
            "time_to_end": "ENDED", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "c1",
        },
        {
            "item_id": "600000002", "status": "SCHEDULED", "snipe_group": "2",
            "title": "Comic B alt", "max_bid": "50.00 USD", "current_bid": "0.00 USD",
            "time_to_end": "5h 0m", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "d2",
        },
    ]
    api.mock_gixen.remove_snipe.side_effect = GixenError("Gixen down")
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 200
    assert r.json()["removed_siblings"] == 0
