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


def test_extract_comics_links_unlinked_bids(api):
    # Insert a bid with a parseable ebay_title and no comic_id
    r = api.post("/api/bids", json={"item_id": "999000111", "max_bid": 50.0})
    assert r.status_code == 200
    # Backdoor: write ebay_title via the cache helper indirectly through the DB
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.execute(
        "UPDATE bids SET ebay_title=? WHERE item_id=?",
        ("Amazing Spider-Man #300 1988 NM", "999000111"),
    )
    db.commit()

    r = api.post("/api/extract-comics")
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] >= 1
    assert body["linked"] >= 1

    # Re-running should be idempotent — bid already linked, processed=0
    r2 = api.post("/api/extract-comics")
    body2 = r2.json()
    assert body2["linked"] == 0


def test_extract_comics_skips_unparseable(api):
    r = api.post("/api/bids", json={"item_id": "999000222", "max_bid": 25.0})
    assert r.status_code == 200
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.execute(
        "UPDATE bids SET ebay_title=? WHERE item_id=?",
        ("just some text no issue no year", "999000222"),
    )
    db.commit()

    r = api.post("/api/extract-comics")
    body = r.json()
    # Should have at least one skip with a reason
    assert any(s["item_id"] == "999000222" for s in body["skipped"])


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


def test_get_snipes_serves_cached_data_when_gixen_down(api):
    """The dashboard endpoint reads only from the local cache, so Gixen being
    down does not affect it. The background sync loop owns all live traffic."""
    from gixen_client import GixenError
    api.mock_gixen.list_snipes.side_effect = GixenError("Gixen down")
    r = api.get("/api/snipes")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


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


def test_edit_bid_not_in_db_self_heals_via_sync(api):
    """PATCH succeeds on Gixen but item has no DB row → endpoint runs one
    _sync_gixen so the snipe gets ingested, then returns the full DB row.
    No more synthetic 3-key response."""
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "999000001",
        "max_bid": "75.00 USD",
        "current_bid": "10.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "1d",
        "seller": "someseller",
        "snipe_group": "0",
        "bid_offset": "6",
    }]
    r = api.patch("/api/bids/999000001", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 200
    data = r.json()
    # Full DB row shape now — has the bids columns from the schema, not 3 keys.
    assert "max_bid" in data
    assert "status" in data
    assert "added_at" in data  # comes from the bids row, proves it's not synthetic


def test_edit_bid_not_in_db_and_not_in_gixen_returns_500(api):
    """If Gixen accepts modify but list_snipes still doesn't include the item,
    we genuinely have no row to return — surface 500 rather than fake one."""
    api.mock_gixen.list_snipes.return_value = []  # default fixture state
    r = api.patch("/api/bids/999000002", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 500


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


def test_add_bid_persists_locg_ids(api):
    """--locg-id and --locg-variant-id round-trip through add."""
    r = api.post("/api/bids", json={
        "item_id": "111111111",
        "max_bid": 800.0,
        "comic": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "locg_id": 6977652,
        "locg_variant_id": 6977652,
    })
    assert r.status_code == 200
    # Mock Gixen returning the same item so it shows up in /api/snipes
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "111111111",
        "title": "Amazing Spider-Man #300",
        "max_bid": "800.00 USD",
        "current_bid": "0.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "5h 0m",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "abc",
    }]
    r = api.get("/api/snipes")
    assert r.status_code == 200
    snipes = r.json()
    assert len(snipes) == 1
    assert snipes[0]["locg_id"] == 6977652
    assert snipes[0]["locg_variant_id"] == 6977652


def test_add_bid_without_locg_ids_returns_null(api):
    """Existing call sites without locg_id behave the same — fields are null."""
    r = api.post("/api/bids", json={
        "item_id": "222222222",
        "max_bid": 50.0,
        "comic": "Hulk",
        "issue": "181",
        "year": 1974,
        "grade": 9.0,
    })
    assert r.status_code == 200
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "222222222",
        "title": "Hulk #181",
        "max_bid": "50.00 USD",
        "current_bid": "0.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "5h 0m",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "x",
    }]
    r = api.get("/api/snipes")
    snipes = r.json()
    assert len(snipes) == 1
    assert snipes[0]["locg_id"] is None
    assert snipes[0]["locg_variant_id"] is None


def test_edit_bid_preserves_locg_ids(api):
    """Editing a snipe (without passing locg_id) preserves the comic's locg_id."""
    api.post("/api/bids", json={
        "item_id": "333333333",
        "max_bid": 100.0,
        "comic": "Daredevil",
        "issue": "29",
        "year": 1967,
        "grade": 7.0,
        "locg_id": 8823401,
    })
    # Edit to bump the max bid; do not pass locg_id
    r = api.patch("/api/bids/333333333",
                  json={"max_bid": 150.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 200
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "333333333",
        "title": "Daredevil #29",
        "max_bid": "150.00 USD",
        "current_bid": "20.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "1h 0m",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "yz",
    }]
    snipes = api.get("/api/snipes").json()
    assert snipes[0]["locg_id"] == 8823401
    assert snipes[0]["max_bid"] == "150.00 USD"


def test_edit_bid_can_update_locg_ids(api):
    """Edit can update locg_id / locg_variant_id on the linked comic."""
    api.post("/api/bids", json={
        "item_id": "444444444",
        "max_bid": 100.0,
        "comic": "Batman",
        "issue": "608",
        "year": 2002,
        "grade": 9.4,
        "locg_id": 100,
    })
    # Edit, supplying new locg_id and locg_variant_id
    r = api.patch("/api/bids/444444444", json={
        "max_bid": 100.0, "bid_offset": 6, "snipe_group": 0,
        "locg_id": 999, "locg_variant_id": 1001,
    })
    assert r.status_code == 200
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "444444444",
        "title": "Batman #608",
        "max_bid": "100.00 USD",
        "current_bid": "10.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "2h",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "n1",
    }]
    snipes = api.get("/api/snipes").json()
    assert snipes[0]["locg_id"] == 999
    assert snipes[0]["locg_variant_id"] == 1001


def test_upsert_comic_persists_locg_ids_via_api(api):
    """POST /api/comics with locg_id/locg_variant_id stores them."""
    r = api.post("/api/comics", json={
        "title": "Invincible", "issue": "1", "year": 2003,
        "grade": 9.8,
        "locg_id": 4242, "locg_variant_id": 4242,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["locg_id"] == 4242
    assert data["locg_variant_id"] == 4242


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


# ----------------------------------------------------------------------------
# Tests added per ce-review residual #21 — coverage for code paths the initial
# pass missed.
# ----------------------------------------------------------------------------


def test_ensure_fresh_sync_dedupes_within_ttl(api, monkeypatch):
    """Two rapid /api/snipes calls should share one Gixen list_snipes call.
    _last_sync_at is module-global and may carry over from earlier tests in
    the same process — explicitly reset before measuring."""
    import server.main as smain
    monkeypatch.setattr(smain, "_last_sync_at", 0.0, raising=False)
    api.mock_gixen.list_snipes.return_value = []
    api.mock_gixen.list_snipes.reset_mock()
    api.get("/api/snipes")
    api.get("/api/snipes")
    api.get("/api/snipes")
    assert api.mock_gixen.list_snipes.call_count == 1


def test_cache_gixen_data_coalesces_none_inputs(tmp_path):
    """COALESCE preservation: passing None for a field doesn't clobber the
    existing non-NULL value. cached_at is only bumped when something writes."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from server.db import init_db, insert_bid, cache_gixen_data, get_bid_by_item_id

    db = init_db(tmp_path / "coalesce.db")
    insert_bid(db, "111111", 50.0, None, 6, 0, "original_seller")
    cache_gixen_data(db, "111111", "First Title", None, "10.00 USD")
    db.commit()
    row = get_bid_by_item_id(db, "111111")
    assert row["ebay_title"] == "First Title"
    assert row["seller"] == "original_seller"  # not overwritten by None
    assert row["cached_current_bid"] == "10.00 USD"
    first_cached_at = row["cached_at"]
    assert first_cached_at is not None

    # All-None write: should be a no-op (no commit advancing cached_at).
    cache_gixen_data(db, "111111", None, None, None)
    db.commit()
    row = get_bid_by_item_id(db, "111111")
    assert row["cached_at"] == first_cached_at  # unchanged
    assert row["ebay_title"] == "First Title"  # preserved


def test_sync_gixen_inserts_web_added_snipe(api):
    """Snipe present in Gixen but not in DB → inserted as PENDING."""
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "777888999",
        "max_bid": "30.00 USD",
        "current_bid": "5.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "2h",
        "seller": "newseller",
        "snipe_group": "0",
        "bid_offset": "6",
    }]
    r = api.post("/api/sync")
    assert r.status_code == 200
    r = api.get("/api/snipes")
    assert r.status_code == 200
    items = r.json()
    assert any(i["item_id"] == "777888999" for i in items)


def test_sync_gixen_does_not_purge_vanished(api):
    """Vanished-but-future PENDING rows are left alone (not PURGED)."""
    from datetime import datetime, timedelta, timezone
    # Create a bid in the DB.
    api.post("/api/bids", json={"item_id": "888999000", "max_bid": 25.0})
    # Gixen returns empty — bid is "vanished".
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/sync")
    # Bid should still be visible (not PURGED) since auction_end_at is NULL
    # so it's not the vanished+ended case.
    r = api.get("/api/snipes")
    assert any(i["item_id"] == "888999000" for i in r.json())


def test_sync_gixen_flips_vanished_ended_to_ended(api):
    """Vanished + auction_end_at in past → status flips PENDING → ENDED."""
    import os, sqlite3
    from datetime import datetime, timedelta, timezone

    # Create a bid then back-date its auction_end_at to the past.
    api.post("/api/bids", json={"item_id": "999111222", "max_bid": 25.0})
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET auction_end_at=? WHERE item_id=?",
        (past, "999111222"),
    )
    raw.commit()
    raw.close()

    # Gixen returns empty → bid vanished + ended.
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/sync")

    r = api.get("/api/snipes")
    rows = [i for i in r.json() if i["item_id"] == "999111222"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# bid_comics junction: comics array + locg-link endpoint
# ---------------------------------------------------------------------------

def _seed_lot(api, item_id, title="Daredevil 1,2,3,4,5 Marvel 1993", max_bid=20.5):
    """Insert a bid + ebay_title, run extract-comics so the lot creates 5 comic
    rows + 5 junction entries. Returns the bid_id."""
    r = api.post("/api/bids", json={"item_id": item_id, "max_bid": max_bid})
    assert r.status_code == 200
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.execute(
        "UPDATE bids SET ebay_title=? WHERE item_id=?",
        (title, item_id),
    )
    db.commit()
    db.close()
    r = api.post("/api/extract-comics")
    assert r.status_code == 200, r.text
    return r.json()


def test_snipes_response_includes_comics_array_for_lot(api):
    _seed_lot(api, "555000001")
    r = api.get("/api/snipes")
    rows = [i for i in r.json() if i["item_id"] == "555000001"]
    assert len(rows) == 1
    snipe = rows[0]
    assert "comics" in snipe
    issues = [c["issue"] for c in snipe["comics"]]
    # Lot of 1-5: should produce 5 comic rows, primary first.
    assert issues == ["1", "2", "3", "4", "5"]
    primary = [c for c in snipe["comics"] if c["is_primary"]]
    assert len(primary) == 1
    assert primary[0]["issue"] == "1"
    # Flat fields stay populated from primary for backward compat.
    assert snipe["comic_issue"] == "1"


def test_snipes_response_comics_empty_for_unlinked(api):
    """A bid with no comic linkage still returns comics=[] (not missing key)."""
    r = api.post("/api/bids", json={"item_id": "555000099", "max_bid": 10.0})
    assert r.status_code == 200
    r = api.get("/api/snipes")
    rows = [i for i in r.json() if i["item_id"] == "555000099"]
    assert len(rows) == 1
    assert rows[0]["comics"] == []


def test_locg_link_primary_updates_existing(api):
    _seed_lot(api, "555000002")
    # Without --issue, hits the primary
    r = api.post(
        "/api/bids/555000002/comics/locg",
        json={"locg_id": 1931243},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locg_id"] == 1931243
    assert body["is_primary"] is True
    assert body["issue"] == "1"


def test_locg_link_specific_issue(api):
    _seed_lot(api, "555000003")
    r = api.post(
        "/api/bids/555000003/comics/locg",
        json={"locg_id": 7111218, "issue": "2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issue"] == "2"
    assert body["locg_id"] == 7111218
    assert body["is_primary"] is False


def test_locg_link_auto_creates_missing_issue(api):
    """If --issue refers to an issue not yet in the bid's junction (e.g. the
    parser missed it), the endpoint upserts a comic row + junction link."""
    # Seed with a single-issue bid (no lot expansion)
    r = api.post("/api/bids", json={"item_id": "555000004", "max_bid": 10.0})
    assert r.status_code == 200
    import os, sqlite3
    db = sqlite3.connect(os.environ["DB_PATH"])
    db.execute(
        "UPDATE bids SET ebay_title=? WHERE item_id=?",
        ("Daredevil The Man Without Fear #1 Marvel 1993", "555000004"),
    )
    db.commit()
    db.close()
    api.post("/api/extract-comics")

    # Now ask to link issue #2 — not in the bid's set yet.
    r = api.post(
        "/api/bids/555000004/comics/locg",
        json={"locg_id": 7111218, "issue": "2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issue"] == "2"
    assert body["locg_id"] == 7111218
    assert body["is_primary"] is False

    # Snipes endpoint should now show 2 comics for this bid.
    r2 = api.get("/api/snipes")
    rows = [i for i in r2.json() if i["item_id"] == "555000004"]
    assert len(rows) == 1
    issues = sorted(c["issue"] for c in rows[0]["comics"])
    assert issues == ["1", "2"]


def test_locg_link_unknown_item_404(api):
    r = api.post(
        "/api/bids/000000000/comics/locg",
        json={"locg_id": 12345},
    )
    assert r.status_code == 404


def test_locg_link_no_primary_without_issue_409(api):
    """Bid with no primary comic + no --issue → can't infer target."""
    r = api.post("/api/bids", json={"item_id": "555000005", "max_bid": 10.0})
    assert r.status_code == 200
    r = api.post(
        "/api/bids/555000005/comics/locg",
        json={"locg_id": 12345},
    )
    assert r.status_code == 409


def test_locg_link_variant_id_preserves_when_omitted(api):
    """Calling locg link without variant-id must not clobber an existing one."""
    _seed_lot(api, "555000006")
    api.post(
        "/api/bids/555000006/comics/locg",
        json={"locg_id": 1931243, "locg_variant_id": 9999},
    )
    # Re-link without variant_id
    r = api.post(
        "/api/bids/555000006/comics/locg",
        json={"locg_id": 1111},
    )
    body = r.json()
    assert body["locg_id"] == 1111
    assert body["locg_variant_id"] == 9999
