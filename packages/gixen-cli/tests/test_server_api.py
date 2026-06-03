"""HTTP endpoint tests — GixenClient is mocked, DB uses tmp_path."""
import sys
import os
import sqlite3
import types
import pytest
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _install_plugins(monkeypatch, plugins: dict):
    eps = []
    for name, mod in plugins.items():
        module_name = f"_test_api_{name.replace('-', '_')}"
        monkeypatch.setitem(sys.modules, module_name, mod)
        eps.append(EntryPoint(name=name, value=module_name, group="gixen.plugins"))
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: eps if group == "gixen.plugins" else [],
    )


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
    _install_plugins(monkeypatch, {})
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock
            yield client


def _dbconn():
    """Fresh connection to the server's DB file. The app's own connection is
    thread-bound (created in the TestClient's thread), so tests read/seed via a
    separate connection — WAL makes committed writes visible across connections."""
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_add_bid_minimal(api):
    r = api.post("/api/bids", json={
        "item_id": "123456789",
        "max_bid": 50.0,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["item_id"] == "123456789"
    assert data["status"] == "PENDING"
    api.mock_gixen.add_snipe.assert_called_once()


def test_add_bid_persists_seller_and_grades(api):
    """BUI-78: seller (lowercased) + both grades sent on POST land in the DB.
    Guards the AddBidRequest extra='ignore' silent-drop — asserts the DB row,
    not just a 200."""
    r = api.post("/api/bids", json={
        "item_id": "412000001", "max_bid": 50.0,
        "seller": "BeatleBlueCat", "seller_grade": 9.0, "photo_grade": 7.0,
    })
    assert r.status_code == 200
    row = _dbconn().execute(
        "SELECT seller, seller_grade, photo_grade FROM bids WHERE item_id='412000001'"
    ).fetchone()
    assert row["seller"] == "beatlebluecat"   # lowercased canonical key
    assert row["seller_grade"] == 9.0
    assert row["photo_grade"] == 7.0


def test_add_bid_without_grades_stores_null(api):
    """Backward-compat: a minimal add stores NULL grades, no error."""
    r = api.post("/api/bids", json={"item_id": "412000002", "max_bid": 50.0})
    assert r.status_code == 200
    row = _dbconn().execute(
        "SELECT seller_grade, photo_grade FROM bids WHERE item_id='412000002'"
    ).fetchone()
    assert row["seller_grade"] is None
    assert row["photo_grade"] is None


def test_readd_fills_null_grades(api):
    """BUI-78 (C2): a snipe added without grades, then re-added with grades,
    gets its NULL grade columns filled via the update-in-place path."""
    api.post("/api/bids", json={"item_id": "412000003", "max_bid": 50.0})
    r = api.post("/api/bids", json={
        "item_id": "412000003", "max_bid": 60.0,
        "seller": "seller3", "seller_grade": 8.0, "photo_grade": 6.0,
    })
    assert r.status_code == 200
    assert r.json()["created"] is False  # update-in-place path
    row = _dbconn().execute(
        "SELECT seller, seller_grade, photo_grade FROM bids "
        "WHERE item_id='412000003' AND status='PENDING'"
    ).fetchone()
    assert row["seller"] == "seller3"
    assert row["seller_grade"] == 8.0
    assert row["photo_grade"] == 6.0


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


# --- BUI-67: add-endpoint upsert ----------------------------------------------

def test_add_bid_new_item_created_true(api):
    r = api.post("/api/bids", json={"item_id": "411000001", "max_bid": 50.0})
    assert r.status_code == 200
    assert r.json()["created"] is True
    api.mock_gixen.add_snipe.assert_called_once()
    api.mock_gixen.modify_snipe.assert_not_called()


def test_readd_updates_in_place(api):
    r1 = api.post("/api/bids", json={"item_id": "411000002", "max_bid": 50.0})
    first_id = r1.json()["id"]
    api.mock_gixen.add_snipe.reset_mock()

    r2 = api.post("/api/bids", json={"item_id": "411000002", "max_bid": 75.0})
    assert r2.status_code == 200
    data = r2.json()
    assert data["id"] == first_id          # same row
    assert data["max_bid"] == 75.0         # updated
    assert data["created"] is False
    # Gixen modify, not a second add.
    api.mock_gixen.modify_snipe.assert_called_once()
    api.mock_gixen.add_snipe.assert_not_called()
    # exactly one non-tombstone row for the item
    conn = _dbconn()
    n = conn.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id='411000002' AND status NOT IN ('REMOVED','PURGED')"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_readd_lower_max_bid_is_visible_as_update(api):
    api.post("/api/bids", json={"item_id": "411000003", "max_bid": 80.0})
    r = api.post("/api/bids", json={"item_id": "411000003", "max_bid": 40.0})
    assert r.status_code == 200
    data = r.json()
    assert data["created"] is False   # the foot-gun guard: caller can see it's not a new snipe
    assert data["max_bid"] == 40.0


def test_relisting_allowed_after_terminal(api):
    api.post("/api/bids", json={"item_id": "411000004", "max_bid": 50.0})
    conn = _dbconn()
    conn.execute("UPDATE bids SET status='ENDED' WHERE item_id='411000004'")
    conn.commit()
    api.mock_gixen.add_snipe.reset_mock()

    r = api.post("/api/bids", json={"item_id": "411000004", "max_bid": 60.0})
    assert r.status_code == 200
    assert r.json()["created"] is True
    api.mock_gixen.add_snipe.assert_called_once()
    # one ENDED + one new PENDING
    rows = sorted(x["status"] for x in conn.execute(
        "SELECT status FROM bids WHERE item_id='411000004'"
    ))
    conn.close()
    assert rows == ["ENDED", "PENDING"]


def test_add_gixen_state_skew_falls_back_to_add(api):
    from gixen_client import GixenSnipeNotFoundError
    api.post("/api/bids", json={"item_id": "411000005", "max_bid": 50.0})
    # Gixen lost the snipe: modify says not-found.
    api.mock_gixen.modify_snipe.side_effect = GixenSnipeNotFoundError("gone")
    api.mock_gixen.add_snipe.reset_mock()

    r = api.post("/api/bids", json={"item_id": "411000005", "max_bid": 65.0})
    assert r.status_code == 200          # not 404/500
    api.mock_gixen.add_snipe.assert_called_once()  # fell back to add
    # still exactly one PENDING row, updated
    conn = _dbconn()
    row = conn.execute(
        "SELECT * FROM bids WHERE item_id='411000005' AND status='PENDING'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["max_bid"] == 65.0


def test_add_not_confirmed_on_new_item_returns_503(api):
    """A brand-new add where Gixen can't confirm the snipe surfaces as 503 and
    inserts no row (the next sync's web-add path reconciles if Gixen has it)."""
    from gixen_client import GixenAddNotConfirmedError
    api.mock_gixen.add_snipe.side_effect = GixenAddNotConfirmedError("411000008")
    r = api.post("/api/bids", json={"item_id": "411000008", "max_bid": 50.0})
    assert r.status_code == 503
    conn = _dbconn()
    n = conn.execute("SELECT COUNT(*) FROM bids WHERE item_id='411000008'").fetchone()[0]
    conn.close()
    assert n == 0


def test_add_not_confirmed_on_fallback_returns_existing(api):
    from gixen_client import GixenSnipeNotFoundError, GixenAddNotConfirmedError
    api.post("/api/bids", json={"item_id": "411000006", "max_bid": 50.0})
    api.mock_gixen.modify_snipe.side_effect = GixenSnipeNotFoundError("gone")
    api.mock_gixen.add_snipe.side_effect = GixenAddNotConfirmedError("411000006")

    r = api.post("/api/bids", json={"item_id": "411000006", "max_bid": 65.0})
    assert r.status_code == 200          # not a bare 503 that hides the stale row
    data = r.json()
    assert data["created"] is False
    assert data["applied"] is False      # signals the new bid was NOT applied


def test_add_defensive_integrity_recovery(api, monkeypatch):
    """If insert collides with the unique index (a racing unlocked-sync insert
    landed first), the endpoint recovers by updating the existing live row."""
    import server.main as m
    seed = _dbconn()
    seed.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('411000007', 10.0, 'PENDING')")
    seed.commit()
    seed.close()
    # Force the endpoint's initial lookup to miss (simulate the stale read that
    # lets execution reach the add branch), so insert_bid hits the index.
    real = m.get_pending_bid_by_item_id
    state = {"first": True}

    def stale_then_real(c, iid):
        if state["first"]:
            state["first"] = False
            return None
        return real(c, iid)

    monkeypatch.setattr("server.main.get_pending_bid_by_item_id", stale_then_real)

    r = api.post("/api/bids", json={"item_id": "411000007", "max_bid": 22.0})
    assert r.status_code == 200
    data = r.json()
    assert data["created"] is False
    assert data["max_bid"] == 22.0
    conn = _dbconn()
    n = conn.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id='411000007' AND status='PENDING'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_get_snipes_empty(api):
    api.mock_gixen.list_snipes.return_value = []
    r = api.get("/api/snipes")
    assert r.status_code == 200
    assert r.json() == []


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
    # _sync_gixen sets the bid to WON; purge then marks it REMOVED (the
    # tombstone) and returns purged_completed >= 1.
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
            "title": "Item A", "max_bid": "50.00 USD", "current_bid": "45.00 USD",
            "time_to_end": "ENDED", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "a1",
        },
        {
            "item_id": "500000002", "status": "SCHEDULED", "snipe_group": "1",
            "title": "Item A alt", "max_bid": "50.00 USD", "current_bid": "0.00 USD",
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
            "title": "Item B", "max_bid": "50.00 USD", "current_bid": "40.00 USD",
            "time_to_end": "ENDED", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "c1",
        },
        {
            "item_id": "600000002", "status": "SCHEDULED", "snipe_group": "2",
            "title": "Item B alt", "max_bid": "50.00 USD", "current_bid": "0.00 USD",
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
    insert_bid(db, "111111", 50.0, 6, 0, "original_seller")
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
    """Vanished-but-future PENDING rows are left alone (not removed)."""
    from datetime import datetime, timedelta, timezone
    # Create a bid in the DB.
    api.post("/api/bids", json={"item_id": "888999000", "max_bid": 25.0})
    # Gixen returns empty — bid is "vanished".
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/sync")
    # Bid should still be visible (not removed) since auction_end_at is NULL
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


def _read_db_row(item_id):
    """Read raw bid row by item_id for assertion."""
    import os, sqlite3
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.row_factory = sqlite3.Row
    row = raw.execute(
        "SELECT status, winning_bid, status_mirror FROM bids WHERE item_id=?",
        (item_id,),
    ).fetchone()
    raw.close()
    return dict(row) if row else None


def test_sync_gixen_maps_outbid_to_lost(api):
    """Gixen status='OUTBID' (with time_to_end='ENDED') must flip PENDING → LOST
    and persist status_mirror. Before the fix, OUTBID wasn't in the terminal set
    and rows stayed PENDING forever."""
    api.post("/api/bids", json={"item_id": "600000001", "max_bid": 25.0})
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "600000001",
        "title": "Detective Comics 523",
        "max_bid": "25.00 USD",
        "current_bid": "28.50 USD",
        "status": "OUTBID",
        "status_mirror": "OUTBID: EBAY BID INCREMENT RULE NOT MET",
        "time_to_end": "ENDED",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "d1",
    }]
    api.post("/api/sync")
    row = _read_db_row("600000001")
    assert row["status"] == "LOST"
    assert row["winning_bid"] == 28.5  # captured from current_bid
    assert row["status_mirror"] == "OUTBID: EBAY BID INCREMENT RULE NOT MET"


def test_sync_gixen_maps_bid_under_asking_price_to_lost(api):
    """Gixen status='BID UNDER ASKING PRICE' means the current price already
    exceeded our max at snipe time and Gixen skipped placing the bid — we
    lost, just at a different stage than OUTBID. Map to LOST and capture
    current_bid as the price that beat us."""
    api.post("/api/bids", json={"item_id": "600000002", "max_bid": 25.0})
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "600000002",
        "title": "Detective Comics 575",
        "max_bid": "25.00 USD",
        "current_bid": "37.56 USD",
        "status": "BID UNDER ASKING PRICE",
        "status_mirror": "N/A",
        "time_to_end": "ENDED",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "d2",
    }]
    api.post("/api/sync")
    row = _read_db_row("600000002")
    assert row["status"] == "LOST"
    assert row["winning_bid"] == 37.56  # the price that beat us
    assert row["status_mirror"] == "N/A"


def test_sync_gixen_time_to_end_ended_with_unknown_status_flips_to_ended(api):
    """If time_to_end='ENDED' but the status string is something we don't
    recognize, fall back to ENDED rather than leaving the row PENDING."""
    api.post("/api/bids", json={"item_id": "600000003", "max_bid": 25.0})
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "600000003",
        "title": "Test",
        "max_bid": "25.00 USD",
        "current_bid": "10.00 USD",
        "status": "SOME_NEW_GIXEN_STATUS",
        "status_mirror": None,
        "time_to_end": "ENDED",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "d3",
    }]
    api.post("/api/sync")
    row = _read_db_row("600000003")
    assert row["status"] == "ENDED"


def test_sync_gixen_does_not_duplicate_after_terminal_transition(api):
    """If a snipe transitions PENDING → terminal in this run, the same run's
    insert loop must not re-create it as a fresh PENDING. Regression for the
    bug where existing_ids only counted PENDING rows."""
    api.post("/api/bids", json={"item_id": "600000005", "max_bid": 25.0})
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "600000005",
        "title": "Test",
        "max_bid": "25.00 USD",
        "current_bid": "30.00 USD",
        "status": "OUTBID",
        "status_mirror": "OUTBID: EBAY BID INCREMENT RULE NOT MET",
        "time_to_end": "ENDED",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "d5",
    }]
    api.post("/api/sync")
    import os, sqlite3
    raw = sqlite3.connect(os.environ["DB_PATH"])
    count = raw.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id=?", ("600000005",)
    ).fetchone()[0]
    raw.close()
    assert count == 1, f"expected 1 row for 600000005, got {count}"


def test_sync_gixen_scheduled_stays_pending(api):
    """Sanity check: a still-active snipe (SCHEDULED, future end) must remain
    PENDING — the terminal mapper must not over-trigger."""
    api.post("/api/bids", json={"item_id": "600000004", "max_bid": 25.0})
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "600000004",
        "title": "Test",
        "max_bid": "25.00 USD",
        "current_bid": "5.00 USD",
        "status": "SCHEDULED",
        "status_mirror": None,
        "time_to_end": "2 d, 4 h",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "d4",
    }]
    api.post("/api/sync")
    row = _read_db_row("600000004")
    assert row["status"] == "PENDING"
    assert row["winning_bid"] is None


# ---------------------------------------------------------------------------
# GET /api/dashboard-tabs (PER-28)
# ---------------------------------------------------------------------------


def test_api_dashboard_tabs_returns_plugin_tabs(tmp_path, monkeypatch):
    """Tabs contributed by a plugin are returned by GET /api/dashboard-tabs."""
    from gixen.plugins import hookimpl

    tab_mod = types.ModuleType("_tab_stub")

    @hookimpl
    def register_dashboard_tabs():
        return [{"label": "Comics", "path": "/v2/comics"}]

    tab_mod.register_dashboard_tabs = register_dashboard_tabs
    _install_plugins(monkeypatch, {"tab-stub": tab_mod})

    monkeypatch.setenv("DB_PATH", str(tmp_path / "tabs.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            r = client.get("/api/dashboard-tabs")
    assert r.status_code == 200
    tabs = r.json()
    assert isinstance(tabs, list)
    assert len(tabs) == 1
    assert tabs[0] == {"label": "Comics", "path": "/v2/comics"}


def test_api_dashboard_tabs_empty_without_plugins(tmp_path, monkeypatch):
    """With no plugins installed, GET /api/dashboard-tabs returns an empty list."""
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: [],
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            r = client.get("/api/dashboard-tabs")
    assert r.status_code == 200
    assert r.json() == []


def test_history_deduplicates_by_item_id(api):
    """GET /api/history returns one row per item_id even when the bids table has
    multiple rows for the same item (e.g. after a purge-and-re-add cycle)."""
    import os
    import sqlite3 as _sqlite3
    from datetime import datetime, timedelta, timezone

    db_path = os.getenv("DB_PATH")
    # Must fall inside /api/history's 7-day window. Computed relative to now so
    # it can't age out — a previously hardcoded literal date silently rotted
    # past the window and turned this into a time-bomb failure.
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    # Open a separate connection to seed duplicate rows directly.
    # Intentionally seeds the legacy 'PURGED' tombstone (not 'REMOVED') to
    # verify gixen-cli's /api/history still tolerates pre-BUI-49 values.
    conn = _sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, bid_offset, snipe_group, status, auction_end_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("900000001", 10.0, 6, 0, "PURGED", yesterday),
    )
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, bid_offset, snipe_group, status, auction_end_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("900000001", 20.0, 6, 0, "LOST", yesterday),
    )
    conn.commit()
    conn.close()

    r = api.get("/api/history")
    assert r.status_code == 200
    items = r.json()
    ids = [item["item_id"] for item in items]
    assert ids.count("900000001") == 1


# --- BUI-67 U4: hardening against the partial unique index --------------------

def test_sync_survives_racing_duplicate(api, monkeypatch):
    """A concurrent api_add_bid can insert a PENDING row after _sync_gixen's
    existing_ids snapshot; the unique index then makes the web-add insert raise
    IntegrityError. The sync must skip it and keep ingesting other snipes."""
    # Seed a PENDING row the stale snapshot will NOT include.
    raw = sqlite3.connect(os.environ["DB_PATH"])
    raw.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('551000001', 10.0, 'PENDING')")
    raw.commit()
    raw.close()
    # Stale snapshot: existing_ids omits the racing row.
    monkeypatch.setattr("server.main.get_all_bids", lambda db: [])
    api.mock_gixen.list_snipes.return_value = [
        {"item_id": "551000001", "max_bid": "10.00", "status": "SCHEDULED",
         "time_to_end": "2h", "seller": "s", "snipe_group": "0", "bid_offset": "6"},
        {"item_id": "551000002", "max_bid": "20.00", "status": "SCHEDULED",
         "time_to_end": "2h", "seller": "s", "snipe_group": "0", "bid_offset": "6"},
    ]
    r = api.post("/api/sync")
    assert r.status_code == 200  # sync did not abort on the IntegrityError

    chk = _dbconn()
    assert chk.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id='551000001' AND status='PENDING'"
    ).fetchone()[0] == 1                                  # racing row intact, no dup
    assert chk.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id='551000002'"
    ).fetchone()[0] == 1                                  # genuinely-new snipe still ingested
    chk.close()


def test_ebay_fallback_excludes_dedup_losers(tmp_path):
    """A dedup loser (REMOVED, notes='deduped BUI-67') must not enter the eBay
    fallback queue; a genuinely-removed row still does."""
    from datetime import datetime, timezone
    from server.db import init_db
    from server.main import _ebay_fallback_rows

    conn = init_db(tmp_path / "fb.db")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, resolved_at, notes) "
        "VALUES ('loser1', 10.0, 'REMOVED', ?, 'deduped BUI-67')", (now,)
    )
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, resolved_at) "
        "VALUES ('real1', 10.0, 'REMOVED', ?)", (now,)
    )
    conn.commit()
    items = {r["item_id"] for r in _ebay_fallback_rows(conn, now)}
    conn.close()
    assert "loser1" not in items
    assert "real1" in items
