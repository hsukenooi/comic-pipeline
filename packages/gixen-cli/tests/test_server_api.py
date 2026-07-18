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


def test_add_bid_rejects_overlong_seller(api):
    """Write path mirrors the read endpoint's 1-128 char seller validation."""
    r = api.post("/api/bids", json={
        "item_id": "412000004", "max_bid": 50.0, "seller": "x" * 129,
    })
    assert r.status_code == 422


def test_add_bid_empty_seller_stored_as_null(api):
    """An empty/whitespace seller normalizes to NULL rather than an empty-string key."""
    r = api.post("/api/bids", json={
        "item_id": "412000005", "max_bid": 50.0, "seller": "   ",
    })
    assert r.status_code == 200
    row = _dbconn().execute(
        "SELECT seller FROM bids WHERE item_id='412000005'"
    ).fetchone()
    assert row["seller"] is None


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


# ---------------------------------------------------------------------------
# BUI-392: PATCH /api/bids/{id} snipe_group None-vs-0 passthrough — a
# max_bid-only edit (snipe_group omitted from the body) must not silently
# un-group the snipe, on either the local DB row or the live Gixen snipe.
# ---------------------------------------------------------------------------

def test_edit_bid_max_bid_only_preserves_snipe_group(api):
    """Omitting snipe_group from the PATCH body preserves the existing group
    — the pre-fix default of 0 silently un-grouped the snipe."""
    api.post("/api/bids", json={"item_id": "200000010", "max_bid": 50.0, "snipe_group": 4})
    r = api.patch("/api/bids/200000010", json={"max_bid": 75.0})
    assert r.status_code == 200
    data = r.json()
    assert data["max_bid"] == 75.0
    assert data["snipe_group"] == 4
    # Gixen's own modify form has no passthrough concept — it must still
    # receive the resolved current group, not a literal "None".
    assert api.mock_gixen.modify_snipe.call_args.kwargs.get("snipe_group") == 4


def test_edit_bid_max_bid_only_does_not_stamp_group_changed_at(api):
    api.post("/api/bids", json={"item_id": "200000011", "max_bid": 50.0, "snipe_group": 4})
    r = api.patch("/api/bids/200000011", json={"max_bid": 75.0})
    assert r.status_code == 200
    row = _dbconn().execute(
        "SELECT group_changed_at FROM bids WHERE item_id='200000011'"
    ).fetchone()
    assert row["group_changed_at"] is None


def test_edit_bid_explicit_zero_still_ungroups(api):
    """An explicit snipe_group=0 in the body is still a real un-group request,
    distinct from omitting the field entirely."""
    api.post("/api/bids", json={"item_id": "200000012", "max_bid": 50.0, "snipe_group": 4})
    r = api.patch("/api/bids/200000012", json={"max_bid": 75.0, "snipe_group": 0})
    assert r.status_code == 200
    data = r.json()
    assert data["snipe_group"] == 0
    row = _dbconn().execute(
        "SELECT group_changed_at FROM bids WHERE item_id='200000012'"
    ).fetchone()
    assert row["group_changed_at"] is not None
    assert api.mock_gixen.modify_snipe.call_args.kwargs.get("snipe_group") == 0


def test_edit_bid_explicit_group_change_stamps_group_changed_at(api):
    api.post("/api/bids", json={"item_id": "200000013", "max_bid": 50.0, "snipe_group": 4})
    r = api.patch("/api/bids/200000013", json={"max_bid": 75.0, "snipe_group": 9})
    assert r.status_code == 200
    data = r.json()
    assert data["snipe_group"] == 9
    row = _dbconn().execute(
        "SELECT group_changed_at FROM bids WHERE item_id='200000013'"
    ).fetchone()
    assert row["group_changed_at"] is not None


def test_edit_bid_no_pending_row_falls_back_to_zero_not_stale_terminal_group(api):
    """Adversarial-review catch: when there's no live PENDING row for item_id
    (only an old terminal row from a prior bidding cycle, e.g. a BUI-178-style
    re-listing) and the PATCH omits snipe_group, the Gixen-side resolution
    must NOT leak the terminal row's stale snipe_group into the live snipe —
    it falls back to 0, same as the fully-un-ingested edge case."""
    _seed_bid_row("200000014", status="WON", snipe_group=7)
    r = api.patch("/api/bids/200000014", json={"max_bid": 75.0})
    assert r.status_code == 200
    assert api.mock_gixen.modify_snipe.call_args.kwargs.get("snipe_group") == 0


# ---------------------------------------------------------------------------
# BUI-401: bid_offset gets the same None-passthrough as snipe_group — a
# max_bid-only edit must not silently reset a tuned fire-offset back to 6, on
# either the local DB row or the live Gixen snipe.
# ---------------------------------------------------------------------------

def test_edit_bid_max_bid_only_preserves_bid_offset_and_group(api):
    """Omitting bid_offset AND snipe_group from the PATCH body preserves both —
    the pre-BUI-401 default of 6 silently reset the tuned offset."""
    api.post("/api/bids", json={"item_id": "200000020", "max_bid": 50.0,
                                "bid_offset": 12, "snipe_group": 4})
    r = api.patch("/api/bids/200000020", json={"max_bid": 75.0})
    assert r.status_code == 200
    data = r.json()
    assert data["max_bid"] == 75.0
    assert data["bid_offset"] == 12   # preserved, not reset to 6
    assert data["snipe_group"] == 4   # preserved (BUI-392)
    # Gixen's modify form has no passthrough — it must receive the resolved
    # current values, not literal Nones or defaults.
    kwargs = api.mock_gixen.modify_snipe.call_args.kwargs
    assert kwargs.get("bid_offset") == 12
    assert kwargs.get("snipe_group") == 4


def test_edit_bid_explicit_bid_offset_writes_through(api):
    """An explicit bid_offset is a real change — written locally and sent to
    Gixen — while an omitted snipe_group is still preserved."""
    api.post("/api/bids", json={"item_id": "200000021", "max_bid": 50.0,
                                "bid_offset": 12, "snipe_group": 4})
    r = api.patch("/api/bids/200000021", json={"max_bid": 75.0, "bid_offset": 9})
    assert r.status_code == 200
    data = r.json()
    assert data["bid_offset"] == 9
    assert data["snipe_group"] == 4   # omitted → preserved
    kwargs = api.mock_gixen.modify_snipe.call_args.kwargs
    assert kwargs.get("bid_offset") == 9
    assert kwargs.get("snipe_group") == 4


def test_edit_bid_no_pending_row_falls_back_to_default_offset(api):
    """With no live PENDING row (only a stale terminal row) and bid_offset
    omitted, the Gixen-side resolution falls back to 6 — it must NOT leak the
    terminal row's offset, mirroring the snipe_group fallback."""
    _seed_bid_row("200000022", status="WON", snipe_group=7)
    r = api.patch("/api/bids/200000022", json={"max_bid": 75.0})
    assert r.status_code == 200
    assert api.mock_gixen.modify_snipe.call_args.kwargs.get("bid_offset") == 6


# ---------------------------------------------------------------------------
# BUI-402: the bid_offset/snipe_group passthrough resolve read must run INSIDE
# _api_lock (with the modify + update_bid), closing the TOCTOU where a
# concurrent group/offset-changing PATCH landed between the read and the modify
# and was silently reverted on Gixen. The modify/retry path still works under
# the single acquisition.
# ---------------------------------------------------------------------------

def test_edit_bid_resolves_passthrough_under_api_lock(api, monkeypatch):
    """The passthrough resolve (get_pending_bid_by_item_id) fires while
    _api_lock is held — pre-fix that read happened before the lock, so a
    concurrent PATCH could change the group/offset in the window."""
    import server.main as sm
    api.post("/api/bids", json={"item_id": "200000023", "max_bid": 50.0,
                                "bid_offset": 11, "snipe_group": 5})

    real = sm.get_pending_bid_by_item_id
    observed = []

    def spy(conn, item_id):
        lock = sm._api_lock
        observed.append(lock is not None and lock.locked())
        return real(conn, item_id)

    monkeypatch.setattr(sm, "get_pending_bid_by_item_id", spy)
    r = api.patch("/api/bids/200000023", json={"max_bid": 75.0})  # omits both
    assert r.status_code == 200
    # The resolve is the first get_pending_bid_by_item_id call, and it saw the
    # lock held.
    assert observed and observed[0] is True


def test_edit_bid_cache_fallback_retry_resends_resolved_passthrough(api):
    """The stale-cached-dbidid retry (BUI-116) still works under the single
    _api_lock acquisition, and the retry re-sends the RESOLVED current
    bid_offset/snipe_group — not None/defaults."""
    from gixen_client import GixenModifyNotConfirmedError

    api.post("/api/bids", json={"item_id": "200000024", "max_bid": 50.0,
                                "bid_offset": 11, "snipe_group": 5})
    # Warm a cached dbidid so the fast path (with dbidid) is attempted first;
    # the list-lookup retry only runs when a cached id was used.
    conn = _dbconn()
    conn.execute("UPDATE bids SET dbidid='stale-abc' WHERE item_id='200000024'")
    conn.commit()
    conn.close()

    api.mock_gixen.modify_snipe.side_effect = [
        GixenModifyNotConfirmedError("200000024", 75.0),  # cached id stale
        None,  # retry via list lookup succeeds
    ]
    r = api.patch("/api/bids/200000024", json={"max_bid": 75.0})  # omits both
    assert r.status_code == 200
    assert api.mock_gixen.modify_snipe.call_count == 2
    # The retry (second call, dbidid cleared) carries the resolved values.
    retry_kwargs = api.mock_gixen.modify_snipe.call_args_list[1].kwargs
    assert retry_kwargs.get("bid_offset") == 11
    assert retry_kwargs.get("snipe_group") == 5
    # Local DB reflects the new bid but preserves offset + group.
    data = r.json()
    assert data["max_bid"] == 75.0
    assert data["bid_offset"] == 11
    assert data["snipe_group"] == 5


def test_cli_edit_max_bid_only_preserves_both_end_to_end(api, monkeypatch):
    """BUI-401 end-to-end: a bare `gixen edit <id> <bid>` (server mode) routed
    through the real PATCH route preserves BOTH the tuned bid_offset AND the
    snipe_group — neither silently reset. Exercises CLI payload composition →
    server passthrough → update_bid → DB."""
    from urllib.parse import urlparse
    from click.testing import CliRunner
    import cli as cli_mod

    api.post("/api/bids", json={"item_id": "200000025", "max_bid": 50.0,
                                "bid_offset": 12, "snipe_group": 6})

    # Route the CLI's requests.<method> call into the in-process TestClient.
    def fake_patch(url, **kwargs):
        kwargs.pop("timeout", None)
        return api.patch(urlparse(url).path, json=kwargs.get("json"))

    monkeypatch.setattr(cli_mod.requests, "patch", fake_patch)
    monkeypatch.setattr(cli_mod, "_server_url", lambda: "http://testserver")

    result = CliRunner().invoke(cli_mod.cli, ["edit", "200000025", "75.0"])
    assert result.exit_code == 0, result.output

    row = _dbconn().execute(
        "SELECT max_bid, bid_offset, snipe_group FROM bids WHERE item_id='200000025'"
    ).fetchone()
    assert row["max_bid"] == 75.0
    assert row["bid_offset"] == 12   # not reset to 6
    assert row["snipe_group"] == 6   # not un-grouped


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


def test_remove_bid_already_gone_tombstones_not_404(api):
    """BUI-164: when the snipe has already vanished from Gixen (remove_snipe
    raises GixenSnipeNotFoundError), the desired end state of a remove is
    already true. The endpoint must tombstone the local row REMOVED, not 404
    and leave it PENDING (where it lingers in /api/snipes and could re-fire)."""
    from gixen_client import GixenSnipeNotFoundError

    api.post("/api/bids", json={"item_id": "300000099", "max_bid": 50.0})
    api.mock_gixen.remove_snipe.side_effect = GixenSnipeNotFoundError("gone from Gixen")

    r = api.delete("/api/bids/300000099")
    assert r.status_code == 200
    assert r.json()["status"] == "REMOVED"

    conn = sqlite3.connect(os.environ["DB_PATH"])
    status = conn.execute(
        "SELECT status FROM bids WHERE item_id='300000099'"
    ).fetchone()[0]
    conn.close()
    assert status == "REMOVED"  # not left PENDING


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


def test_edit_bid_unconfirmed_modify_returns_503_and_leaves_db_unchanged(api):
    """BUI-115: when Gixen accepts the modify POST but the new bid never goes
    live, modify_snipe raises GixenModifyNotConfirmedError → 503, and the local
    DB must NOT be updated (no false 'new bid' while Gixen keeps the old)."""
    from gixen_client import GixenModifyNotConfirmedError
    api.post("/api/bids", json={"item_id": "800000077", "max_bid": 50.0})

    api.mock_gixen.modify_snipe.side_effect = GixenModifyNotConfirmedError("800000077", 75.0)
    r = api.patch("/api/bids/800000077", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 503

    # DB still shows the original bid — the write was skipped.
    rows = api.get("/api/bids").json()
    row = next(b for b in rows if b["item_id"] == "800000077")
    assert row["max_bid"] == 50.0


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
    # BUI-407: delete_bid no longer self-commits — api_purge's sibling loop
    # must now commit itself. removed_siblings is a plain in-process counter
    # and the mock-call assertion above proves nothing about the DB write, so
    # read the sibling back via a SEPARATE connection (not the app's own) to
    # prove the tombstone actually landed on disk.
    row = _dbconn().execute(
        "SELECT status FROM bids WHERE item_id=?", ("500000002",)
    ).fetchone()
    assert row["status"] == "REMOVED"


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
    # BUI-407: insert_bid no longer self-commits — _insert_web_added_bids must
    # now commit itself. GET /api/snipes reads via the app's OWN _db
    # singleton, the same connection _sync_gixen just wrote on, so it would
    # see this row even with no commit at all (a connection always sees its
    # own uncommitted writes). Read back via a SEPARATE connection to prove
    # the insert actually reached disk.
    row = _dbconn().execute(
        "SELECT status FROM bids WHERE item_id=?", ("777888999",)
    ).fetchone()
    assert row is not None
    assert row["status"] == "PENDING"


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


def test_sync_gixen_vanished_ended_write_spares_resolved_sibling_sharing_item_id(api):
    """BUI-388: the vanished-ended ENDED write (server/main.py, just below the
    BUI-371 REMOVED branch) must target only the row being transitioned, not
    every row sharing its item_id. Pre-fix, its item_id-wide update_bid_status
    call would also collateral-stamp an older resolved-but-not-yet-purged
    sibling sharing the item_id (e.g. a prior listing of a re-listed/re-added
    item) — clobbering its already-recorded status/winning_bid/resolved_at
    with this row's ENDED/None/now values (the BUI-178 class, the same one
    BUI-371 already guarded against for the REMOVED branch immediately above
    this one, and BUI-382 guarded for every write in _run_ebay_fallback)."""
    import os, sqlite3
    from datetime import datetime, timedelta, timezone

    # Live snipe that will vanish + end.
    api.post("/api/bids", json={"item_id": "700000001", "max_bid": 25.0})
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE bids SET auction_end_at=? WHERE item_id=? AND status='PENDING'",
        (past, "700000001"),
    )
    live_id = raw.execute(
        "SELECT id FROM bids WHERE item_id=? AND status='PENDING'",
        ("700000001",),
    ).fetchone()[0]

    # Old, already-resolved sibling sharing the same item_id (a prior listing
    # of a re-listed item) — WON and not yet purged, so it is still visible
    # to an item_id-wide write (WON/ENDED/etc. are not tombstone statuses).
    old_resolved_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cur = raw.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, resolved_at, "
        "snipe_group) VALUES (?, 50.0, 'WON', 45.0, ?, 0)",
        ("700000001", old_resolved_at),
    )
    old_id = cur.lastrowid
    raw.commit()
    raw.close()

    # Gixen returns empty → the live snipe vanished and its recorded end has
    # already passed.
    api.mock_gixen.list_snipes.return_value = []
    api.post("/api/sync")

    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    old_row = raw.execute(
        "SELECT status, winning_bid, resolved_at FROM bids WHERE id=?", (old_id,)
    ).fetchone()
    live_row = raw.execute(
        "SELECT status, winning_bid FROM bids WHERE id=?", (live_id,)
    ).fetchone()
    raw.close()

    # The old resolved sibling must be completely untouched.
    assert old_row["status"] == "WON"
    assert old_row["winning_bid"] == 45.0
    assert old_row["resolved_at"] == old_resolved_at
    # The vanished live snipe transitions to ENDED as intended.
    assert live_row["status"] == "ENDED"
    assert live_row["winning_bid"] is None


def test_sync_gixen_terminal_transition_write_spares_resolved_sibling_sharing_item_id(api):
    """BUI-390: the terminal write in _sync_gixen's Gixen-status transition loop
    must target only the row being transitioned, not every row sharing its
    item_id. Pre-fix, its item_id-wide update_bid_status call would also
    collateral-stamp an older resolved-but-unpurged sibling (a prior listing of
    a re-listed/re-added item) with this snipe's terminal status + winning_bid —
    the BUI-178 class the REMOVED branch (BUI-371), the vanished-ended write
    (BUI-388) and every _run_ebay_fallback write (BUI-382) already guard."""
    from datetime import datetime, timedelta, timezone

    # Old, already-resolved WON sibling — INSERTED FIRST so it gets the LOWER id.
    # A live PENDING row is always the newest row for its item_id (no second row
    # can be added while a PENDING one exists), which is exactly why
    # get_bid_by_item_id (id DESC LIMIT 1) resolves to the live row below, not
    # this stale one. WON is not a tombstone, so an item_id-wide write sees it.
    old_resolved_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn = _dbconn()
    old_id = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, resolved_at, "
        "snipe_group) VALUES (?, 50.0, 'WON', 45.0, ?, 0)",
        ("710000001", old_resolved_at),
    ).lastrowid
    conn.commit()
    conn.close()

    # Live snipe (new PENDING row, higher id). Gixen now reports it OUTBID/ENDED
    # → transitions to LOST with current_bid as the price that beat us. OUTBID is
    # in _BID_PROCESSED_STATUSES, so the BUI-371 REMOVED branch is skipped and we
    # exercise the plain terminal write under test.
    api.post("/api/bids", json={"item_id": "710000001", "max_bid": 25.0})
    conn = _dbconn()
    live_id = conn.execute(
        "SELECT id FROM bids WHERE item_id=? AND status='PENDING'", ("710000001",)
    ).fetchone()["id"]
    conn.close()
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "710000001", "title": "T", "max_bid": "25.00 USD",
        "current_bid": "30.00 USD", "status": "OUTBID",
        "status_mirror": "OUTBID: EBAY BID INCREMENT RULE NOT MET",
        "time_to_end": "ENDED", "seller": "s", "snipe_group": "0",
        "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "d710",
    }]
    api.post("/api/sync")

    # The old resolved WON sibling must be completely untouched.
    old_row = _read_row_by_id(old_id)
    assert old_row["status"] == "WON"
    assert old_row["winning_bid"] == 45.0
    assert old_row["resolved_at"] == old_resolved_at
    # The live snipe transitions to LOST with the beating price.
    live_row = _read_row_by_id(live_id)
    assert live_row["status"] == "LOST"
    assert live_row["winning_bid"] == 30.0


def test_sync_gixen_won_transition_spares_sibling_group_wins_ledger(api):
    """BUI-390 (WON branch): on a WON transition the id-targeted write must also
    keep update_bid_status's group-win capture from recording a FALSE group_wins
    ledger entry for an older resolved sibling sharing the item_id + snipe_group.
    Pre-fix, the item_id-wide write captured every non-tombstoned sibling with a
    non-null auction_end_at, ledgering a win for a stale row it also (wrongly)
    stamped WON. This exercises the one branch the OUTBID->LOST test above can't:
    update_bid_status's WON-only win_rows SELECT (server/db.py), now id-scoped."""
    from datetime import datetime, timedelta, timezone

    old_end = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    old_resolved_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    live_end = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    # Old resolved LOST sibling in group 7 with a real auction_end_at — INSERTED
    # FIRST (lower id). LOST is not a tombstone and its end is non-null, so an
    # item_id-wide WON capture would ledger it as a (false) group win.
    conn = _dbconn()
    old_id = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, resolved_at, "
        "snipe_group, auction_end_at) VALUES (?, 50.0, 'LOST', 45.0, ?, 7, ?)",
        ("720000001", old_resolved_at, old_end),
    ).lastrowid
    conn.commit()
    conn.close()

    # Live snipe in the same group 7 (higher id); give it its own auction_end_at
    # so its OWN legitimate win is the only ledger entry expected.
    api.post("/api/bids", json={"item_id": "720000001", "max_bid": 25.0})
    conn = _dbconn()
    conn.execute(
        "UPDATE bids SET auction_end_at=?, snipe_group=7 "
        "WHERE item_id=? AND status='PENDING'",
        (live_end, "720000001"),
    )
    live_id = conn.execute(
        "SELECT id FROM bids WHERE item_id=? AND status='PENDING'", ("720000001",)
    ).fetchone()["id"]
    conn.commit()
    conn.close()

    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "720000001", "title": "T", "max_bid": "25.00 USD",
        "current_bid": "60.00 USD", "status": "WON", "status_mirror": None,
        "time_to_end": "ENDED", "seller": "s", "snipe_group": "7",
        "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "d720",
    }]
    assert api.post("/api/sync").status_code == 200

    # The old LOST sibling must be completely untouched.
    old_row = _read_row_by_id(old_id)
    assert old_row["status"] == "LOST"
    assert old_row["winning_bid"] == 45.0
    assert old_row["resolved_at"] == old_resolved_at
    # The live snipe won.
    assert _read_row_by_id(live_id)["status"] == "WON"
    # The ledger carries only the live win's end — never the stale sibling's.
    ledger = api.get("/api/group-wins?item_id=720000001").json()
    ends = [r["won_end_at"] for r in ledger]
    assert old_end not in ends
    assert ends == [live_end]


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
# POST /api/sync structured error handling (BUI-386)
# ---------------------------------------------------------------------------


def test_api_sync_returns_503_on_gixen_connection_error(api):
    """A Gixen-unreachable failure must surface as an honest 503 with a
    structured detail, not a misleadingly-successful {"synced": 0} (the old
    behavior, since _sync_gixen's default reraise=False swallowed the error
    internally) and not a raw unhandled-exception 500."""
    from gixen_client import GixenConnectionError

    api.mock_gixen.list_snipes.side_effect = GixenConnectionError("no route to host")
    r = api.post("/api/sync")
    assert r.status_code == 503
    assert "no route to host" in r.json()["detail"]


def test_api_sync_returns_503_on_gixen_error(api):
    """A generic GixenError (e.g. a login/parse failure) also surfaces as 503,
    matching the convention every other Gixen-backed endpoint in this file
    already uses (api_add_bid, api_edit_bid, api_remove_bid)."""
    from gixen_client import GixenError

    api.mock_gixen.list_snipes.side_effect = GixenError("session expired")
    r = api.post("/api/sync")
    assert r.status_code == 503
    assert "session expired" in r.json()["detail"]


def test_api_sync_returns_structured_500_on_unexpected_error(api, monkeypatch):
    """A genuine server bug downstream of the Gixen call (not a GixenError)
    must not propagate as a raw unhandled exception — it must be caught,
    logged, and reported as a structured 500. Regression for BUI-386: before
    the fix, this exception would escape api_sync entirely and hit FastAPI's
    generic handler."""
    import server.main as m

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "cache_gixen_data", _boom)
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "600000006",
        "max_bid": "25.00 USD",
        "current_bid": "5.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "2 d, 4 h",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
    }]
    r = api.post("/api/sync")
    assert r.status_code == 500
    assert r.json()["detail"]  # structured payload, not an empty/raw body


def test_api_sync_succeeds_when_gixen_healthy(api):
    """Sanity check: the reraise=True + try/except wiring must not disturb
    the ordinary success path."""
    api.mock_gixen.list_snipes.return_value = []
    r = api.post("/api/sync")
    assert r.status_code == 200
    assert r.json() == {"synced": 0}


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


# --- BUI-85: vanished PENDING with no captured auction_end_at ---

def _seed_pending_null_end(api, item_id, max_bid=25.0):
    """Add a PENDING bid (auction_end_at stays NULL — no sync captured an end)."""
    r = api.post("/api/bids", json={"item_id": item_id, "max_bid": max_bid})
    assert r.status_code == 200
    row = _read_db_row(item_id)
    assert row["status"] == "PENDING"


def _arm_ebay(monkeypatch, end_iso):
    """Make _sync_gixen's eBay path active and return a fixed listing end time."""
    import server.main as m
    monkeypatch.setattr(m, "_ebay_fetch_bin", lambda: "ebay-fetch")
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(
        m, "_fetch_ebay_item_sync",
        lambda iid: {"end_date_iso": end_iso} if end_iso is not None else None,
    )


def test_vanished_null_end_past_ebay_end_flips_to_ended(api, monkeypatch):
    """Vanished PENDING + NULL end + eBay says the auction already ended → ENDED."""
    from datetime import datetime, timedelta, timezone
    _seed_pending_null_end(api, "850000001")
    api.mock_gixen.list_snipes.return_value = []  # vanished
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _arm_ebay(monkeypatch, past)

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("850000001")["status"] == "ENDED"


def test_vanished_null_end_future_ebay_end_tombstones_removed(api, monkeypatch):
    """Vanished PENDING + NULL end + eBay says the auction is still live (Gixen
    healthy this sync) → user removed it → REMOVED, never WON/LOST/ENDED."""
    from datetime import datetime, timedelta, timezone
    _seed_pending_null_end(api, "850000002")
    # Gixen returns a *different* live snipe → non-empty list (not a scrape glitch).
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "850099999", "max_bid": "10.00 USD", "current_bid": "1.00 USD",
        "status": "SCHEDULED", "time_to_end": "3h", "seller": "s",
        "snipe_group": "0", "bid_offset": "6",
    }]
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    _arm_ebay(monkeypatch, future)

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("850000002")["status"] == "REMOVED"


def test_vanished_null_end_future_ebay_end_empty_list_left_pending(api, monkeypatch):
    """Same future-end case but Gixen returned an EMPTY list (possible scrape
    glitch) → do NOT mass-tombstone live snipes; leave PENDING."""
    from datetime import datetime, timedelta, timezone
    _seed_pending_null_end(api, "850000003")
    api.mock_gixen.list_snipes.return_value = []  # empty → glitch guard trips
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    _arm_ebay(monkeypatch, future)

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("850000003")["status"] == "PENDING"


def test_vanished_null_end_no_ebay_data_left_pending(api, monkeypatch):
    """eBay returns nothing → can't disambiguate → leave PENDING, retry later."""
    _seed_pending_null_end(api, "850000004")
    api.mock_gixen.list_snipes.return_value = []
    _arm_ebay(monkeypatch, None)  # _fetch_ebay_item_sync → None

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("850000004")["status"] == "PENDING"


def test_vanished_null_end_still_on_gixen_not_touched(api, monkeypatch):
    """A NULL-end PENDING row still present in Gixen's list is not eBay-resolved
    — the normal time_to_end path owns it."""
    from datetime import datetime, timedelta, timezone
    _seed_pending_null_end(api, "850000005")
    # Still on Gixen with a live time_to_end → handled by the normal path.
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "850000005", "max_bid": "25.00 USD", "current_bid": "1.00 USD",
        "status": "SCHEDULED", "time_to_end": "4h", "seller": "s",
        "snipe_group": "0", "bid_offset": "6",
    }]
    # If eBay were (wrongly) consulted it would say "ended"; assert it isn't.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _arm_ebay(monkeypatch, past)

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("850000005")["status"] == "PENDING"


def _insert_old_resolved_won_sibling(item_id):
    """Insert an older, already-resolved WON row sharing item_id (a prior
    listing of a re-listed/re-added item). WON is not a tombstone, so an
    item_id-wide write would clobber it. Returns (old_id, old_resolved_at)."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    old_resolved_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn = _dbconn()
    old_id = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, resolved_at, "
        "snipe_group) VALUES (?, 50.0, 'WON', 45.0, ?, 0)",
        (item_id, old_resolved_at),
    ).lastrowid
    conn.commit()
    conn.close()
    return old_id, old_resolved_at


def _read_row_by_id(row_id):
    conn = _dbconn()
    row = conn.execute(
        "SELECT status, winning_bid, resolved_at FROM bids WHERE id=?", (row_id,)
    ).fetchone()
    conn.close()
    return row


def test_vanished_null_end_ended_write_spares_resolved_sibling_sharing_item_id(api, monkeypatch):
    """BUI-390: the vanished-null-end ENDED write (now _apply_vanished_null_end,
    BUI-410) must id-target the row it is resolving, not stamp every row sharing
    the item_id. Pre-fix, its item_id-wide update_bid_status would also clobber
    an older resolved WON sibling — the BUI-178 class the sync-loop writes
    (BUI-388) already guard."""
    from datetime import datetime, timedelta, timezone
    _seed_pending_null_end(api, "860000001")
    old_id, old_resolved_at = _insert_old_resolved_won_sibling("860000001")

    api.mock_gixen.list_snipes.return_value = []  # vanished
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _arm_ebay(monkeypatch, past)  # eBay: already ended → ENDED

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("860000001")["status"] == "ENDED"
    old = _read_row_by_id(old_id)
    assert old["status"] == "WON"
    assert old["winning_bid"] == 45.0
    assert old["resolved_at"] == old_resolved_at


def test_vanished_null_end_removed_write_spares_resolved_sibling_sharing_item_id(api, monkeypatch):
    """BUI-390: the vanished-null-end REMOVED (tombstone) write (now
    _apply_vanished_null_end, BUI-410) must id-target the row it is resolving.
    Pre-fix, its item_id-wide write would also tombstone an older resolved WON
    sibling sharing the item_id."""
    from datetime import datetime, timedelta, timezone
    _seed_pending_null_end(api, "860000002")
    old_id, old_resolved_at = _insert_old_resolved_won_sibling("860000002")

    # Gixen returns a DIFFERENT live snipe → non-empty list (not a scrape
    # glitch), so a future-end vanished row tombstones REMOVED.
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "860099999", "max_bid": "10.00 USD", "current_bid": "1.00 USD",
        "status": "SCHEDULED", "time_to_end": "3h", "seller": "s",
        "snipe_group": "0", "bid_offset": "6",
    }]
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    _arm_ebay(monkeypatch, future)

    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("860000002")["status"] == "REMOVED"
    old = _read_row_by_id(old_id)
    assert old["status"] == "WON"
    assert old["winning_bid"] == 45.0
    assert old["resolved_at"] == old_resolved_at


# ---------------------------------------------------------------------------
# BUI-371: cancelled-before-end disambiguation (vanish-time + group-win)
# ---------------------------------------------------------------------------

def _seed_bid_row(item_id, *, status="PENDING", max_bid=25.0, snipe_group=0,
                  auction_end_at=None, gixen_vanished_at=None,
                  winning_bid=None, resolved_at=None, added_at=None,
                  group_changed_at=None):
    """Raw-insert a bids row so tests control every disambiguation input.

    added_at defaults to a week ago so seeded rows predate any group win the
    test stages (the _group_won_before lifetime bound requires the win to fall
    at or after the classified row's added_at)."""
    if added_at is None:
        added_at = _iso_ago(days=7)
    conn = _dbconn()
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, "
        "auction_end_at, gixen_vanished_at, winning_bid, resolved_at, added_at, "
        "group_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, max_bid, status, snipe_group, auction_end_at,
         gixen_vanished_at, winning_bid, resolved_at, added_at,
         group_changed_at),
    )
    conn.commit()
    conn.close()


def _gixen_listing(item_id, *, status, time_to_end, snipe_group="0",
                   current_bid="10.00 USD", max_bid="25.00 USD"):
    return {
        "item_id": item_id, "title": "T", "max_bid": max_bid,
        "current_bid": current_bid, "status": status, "status_mirror": None,
        "time_to_end": time_to_end, "seller": "s",
        "snipe_group": snipe_group, "bid_offset": "6", "bid_offset_mirror": "6",
        "dbidid": f"d{item_id}",
    }


def _iso_ago(**kwargs):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _vanish_col(item_id):
    conn = _dbconn()
    row = conn.execute(
        "SELECT gixen_vanished_at FROM bids WHERE item_id=?", (item_id,)
    ).fetchone()
    conn.close()
    return row["gixen_vanished_at"]


def test_group_cancelled_sibling_listed_ended_tombstones_removed(api):
    """A sibling still listed on Gixen with an unrecognized (cancelled) status
    that reaches its own auction end must resolve REMOVED, not ENDED — ENDED
    would feed the eBay fallback's phantom-WON inference (BUI-371)."""
    _seed_bid_row("371000001", status="WON", snipe_group=3,
                  auction_end_at=_iso_ago(days=2), winning_bid=20.0,
                  resolved_at=_iso_ago(days=2))
    _seed_bid_row("371000002", snipe_group=3, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000002", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="3"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000002")["status"] == "REMOVED"
    # The tombstone carries the BUI-371 audit marker (BUI-67 convention).
    from server.db import CANCELLED_TOMBSTONE_NOTE
    conn = _dbconn()
    note = conn.execute(
        "SELECT notes FROM bids WHERE item_id='371000002'"
    ).fetchone()["notes"]
    conn.close()
    assert note == CANCELLED_TOMBSTONE_NOTE


def test_group_cancelled_sibling_listed_lost_tombstones_removed(api):
    """A plain Gixen LOST on a group-cancelled sibling is a loss we never
    contested — REMOVED, so it can't pollute the calibration report's
    LOST-above-fmv_high analysis (BUI-371 secondary)."""
    _seed_bid_row("371000003", status="WON", snipe_group=4,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000004", snipe_group=4, auction_end_at=_iso_ago(hours=2))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000004", status="LOST", time_to_end="ENDED",
                       snipe_group="4", current_bid="30.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    row = _read_db_row("371000004")
    assert row["status"] == "REMOVED"
    assert row["winning_bid"] is None


def test_group_sibling_outbid_stays_lost(api):
    """OUTBID proves Gixen placed our bid — the loss is genuine and must stay
    LOST (calibration correctness) even when a group sibling won earlier."""
    _seed_bid_row("371000005", status="WON", snipe_group=5,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000006", snipe_group=5, auction_end_at=_iso_ago(hours=2))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000006", status="OUTBID", time_to_end="ENDED",
                       snipe_group="5", current_bid="28.50 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    row = _read_db_row("371000006")
    assert row["status"] == "LOST"
    assert row["winning_bid"] == 28.5


def test_group_win_within_margin_not_reclassified(api):
    """Gixen's FAQ: auctions ending within ~2 minutes can BOTH fire (dual-win
    window). A group win inside the safety margin is not cancel evidence —
    keep today's WON-permissive ENDED so a real result can still be inferred."""
    _seed_bid_row("371000007", status="WON", snipe_group=6,
                  auction_end_at=_iso_ago(hours=2, minutes=1), winning_bid=20.0,
                  resolved_at=_iso_ago(hours=2))
    _seed_bid_row("371000008", snipe_group=6, auction_end_at=_iso_ago(hours=2))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000008", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="6"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000008")["status"] == "ENDED"


def test_winner_and_sibling_same_sync_processed_won_first(api):
    """After a sync gap, the winner's WON and the sibling's cancelled-ENDED
    arrive in one list pull. The WON transition must be applied first so the
    sibling's classification sees the group-win evidence — even when the
    sibling precedes the winner in Gixen's list order."""
    _seed_bid_row("371000009", snipe_group=7, auction_end_at=_iso_ago(days=2))
    _seed_bid_row("371000010", snipe_group=7, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000010", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="7"),
        _gixen_listing("371000009", status="WON", time_to_end="ENDED",
                       snipe_group="7", current_bid="20.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000009")["status"] == "WON"
    assert _read_db_row("371000010")["status"] == "REMOVED"


def test_vanish_stamp_set_and_cleared_on_reappear(api):
    """A PENDING row missing from a healthy (non-empty) list gets
    gixen_vanished_at stamped; reappearing clears it (transient scrape miss)."""
    api.post("/api/bids", json={"item_id": "371000011", "max_bid": 25.0})
    other = _gixen_listing("371099999", status="SCHEDULED", time_to_end="3 h")
    api.mock_gixen.list_snipes.return_value = [other]
    assert api.post("/api/sync").status_code == 200
    assert _vanish_col("371000011") is not None

    api.mock_gixen.list_snipes.return_value = [
        other,
        _gixen_listing("371000011", status="SCHEDULED", time_to_end="2 h"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _vanish_col("371000011") is None


def test_empty_list_does_not_stamp_vanish(api):
    """An empty scrape is a possible glitch — never stamp vanish times off it."""
    api.post("/api/bids", json={"item_id": "371000012", "max_bid": 25.0})
    api.mock_gixen.list_snipes.return_value = []
    assert api.post("/api/sync").status_code == 200
    assert _vanish_col("371000012") is None


def test_vanished_well_before_end_tombstones_removed(api):
    """Observed missing from Gixen well before its auction end → the snipe was
    cancelled while live (user or group cancel) → REMOVED, never ENDED."""
    _seed_bid_row("371000013", auction_end_at=_iso_ago(minutes=30),
                  gixen_vanished_at=_iso_ago(hours=2))
    api.mock_gixen.list_snipes.return_value = []
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000013")["status"] == "REMOVED"


def test_vanished_after_end_flips_ended(api):
    """First observed missing only after the auction ended — consistent with a
    normally-executed snipe Gixen dropped → ENDED (fallback may infer WON)."""
    _seed_bid_row("371000014", auction_end_at=_iso_ago(hours=1),
                  gixen_vanished_at=_iso_ago(minutes=30))
    api.mock_gixen.list_snipes.return_value = []
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000014")["status"] == "ENDED"


def test_vanished_within_margin_flips_ended(api):
    """A vanish observed inside the safety margin of the end is ambiguous
    (end-time estimation error) → preserve WON-permissive ENDED."""
    _seed_bid_row("371000015", auction_end_at=_iso_ago(minutes=30),
                  gixen_vanished_at=_iso_ago(minutes=35))
    api.mock_gixen.list_snipes.return_value = []
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000015")["status"] == "ENDED"


def test_vanished_group_sibling_removed_without_vanish_stamp(api):
    """Group-win evidence alone (no vanish stamp — e.g. server was down when
    the sibling vanished) still classifies a vanished-ended sibling REMOVED."""
    _seed_bid_row("371000016", status="WON", snipe_group=8,
                  auction_end_at=_iso_ago(days=1), winning_bid=15.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000017", snipe_group=8, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = []
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000017")["status"] == "REMOVED"


def test_group_zero_is_never_group_evidence(api):
    """snipe_group=0 means 'no group' (the schema default for most snipes).
    A WON group-0 row must never count as cancel evidence for an unrelated
    group-0 row — without the guard, any past win would tombstone any
    unrelated ended snipe."""
    _seed_bid_row("371000018", status="WON", snipe_group=0,
                  auction_end_at=_iso_ago(days=1), winning_bid=15.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000019", snipe_group=0, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000019", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="0"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000019")["status"] == "ENDED"


def test_group_sibling_bid_under_asking_price_stays_lost(api):
    """BID UNDER ASKING PRICE proves Gixen evaluated our snipe at fire time —
    like OUTBID, it is exempt from group-cancel reclassification."""
    _seed_bid_row("371000020", status="WON", snipe_group=9,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000021", snipe_group=9, auction_end_at=_iso_ago(hours=2))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000021", status="BID UNDER ASKING PRICE",
                       time_to_end="ENDED", snipe_group="9",
                       current_bid="40.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    row = _read_db_row("371000021")
    assert row["status"] == "LOST"
    assert row["winning_bid"] == 40.0


def test_invalid_snipe_group_string_is_not_evidence(api):
    """A non-numeric snipe_group from a Gixen parse quirk must not crash the
    sync or trigger reclassification — it parses as 'no group'."""
    _seed_bid_row("371000022", status="WON", snipe_group=4,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000023", snipe_group=4, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000023", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="N/A"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000023")["status"] == "ENDED"


def test_group_evidence_uses_resolved_at_when_winner_end_missing(api):
    """A WON sibling whose auction_end_at was never captured still provides
    evidence via its resolved_at (when we observed the win)."""
    _seed_bid_row("371000024", status="WON", snipe_group=5,
                  auction_end_at=None, winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("371000025", snipe_group=5, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000025", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="5"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000025")["status"] == "REMOVED"


def test_reused_group_number_old_win_is_not_evidence(api):
    """Gixen group numbers (1-10) get recycled across unrelated campaigns.
    A WON row from a prior campaign — ended before this snipe was even added
    — cannot have group-cancelled it and must not reclassify its result
    (worst case it would suppress a real win via the fallback)."""
    _seed_bid_row("371000026", status="WON", snipe_group=7,
                  auction_end_at=_iso_ago(days=30), winning_bid=20.0,
                  resolved_at=_iso_ago(days=30), added_at=_iso_ago(days=37))
    # New, unrelated snipe reusing group 7; added well after that old win.
    _seed_bid_row("371000027", snipe_group=7, auction_end_at=_iso_ago(hours=1),
                  added_at=_iso_ago(days=2))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("371000027", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="7"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("371000027")["status"] == "ENDED"


def test_readd_clears_vanish_stamp(api):
    """Re-adding a snipe (update-in-place upsert) is first-party proof it is
    live on Gixen again — the stale vanish stamp must be cleared so it can't
    later masquerade as cancel evidence."""
    api.post("/api/bids", json={"item_id": "371000028", "max_bid": 25.0})
    conn = _dbconn()
    conn.execute(
        "UPDATE bids SET gixen_vanished_at=? WHERE item_id='371000028'",
        (_iso_ago(hours=2),),
    )
    conn.commit()
    conn.close()
    r = api.post("/api/bids", json={"item_id": "371000028", "max_bid": 30.0})
    assert r.status_code == 200
    assert r.json()["created"] is False  # update-in-place path
    assert _vanish_col("371000028") is None


def test_vanish_stamp_respects_scrape_start(tmp_path):
    """A PENDING row added after the scrape snapshot began is legitimately
    absent from that list — its absence is not a vanish observation."""
    from datetime import datetime, timedelta, timezone
    from server.db import init_db
    from server.main import _record_vanish_observations

    conn = init_db(tmp_path / "vanish.db")
    now_dt = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, added_at) "
        "VALUES ('900100001', 10.0, 'PENDING', ?)",
        (now_dt.isoformat(),),
    )
    conn.commit()

    # Scrape started BEFORE the row was added → not stamped.
    _record_vanish_observations(
        conn, {"other-item"}, now_dt.isoformat(),
        (now_dt - timedelta(minutes=1)).isoformat(),
    )
    conn.commit()
    row = conn.execute(
        "SELECT gixen_vanished_at FROM bids WHERE item_id='900100001'"
    ).fetchone()
    assert row["gixen_vanished_at"] is None

    # Scrape started AFTER the row was added → genuine vanish, stamped.
    _record_vanish_observations(
        conn, {"other-item"}, now_dt.isoformat(),
        (now_dt + timedelta(minutes=1)).isoformat(),
    )
    conn.commit()
    row = conn.execute(
        "SELECT gixen_vanished_at FROM bids WHERE item_id='900100001'"
    ).fetchone()
    conn.close()
    assert row["gixen_vanished_at"] is not None


# ---------------------------------------------------------------------------
# BUI-381: group-evidence durability (snipe_group sync refresh + the durable
# group_wins ledger surviving winner-row destruction)
# ---------------------------------------------------------------------------

def _read_col(item_id, col):
    conn = _dbconn()
    row = conn.execute(
        f"SELECT {col} FROM bids WHERE item_id=?", (item_id,)
    ).fetchone()
    conn.close()
    return row[col] if row else None


def _group_win_row(item_id):
    conn = _dbconn()
    row = conn.execute(
        "SELECT * FROM group_wins WHERE item_id=?", (item_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def test_sync_refreshes_snipe_group_on_pending_row(api):
    """A retroactive `gixen group N` applied on Gixen's web UI reaches the DB
    on the next sync — previously _sync_gixen never refreshed snipe_group on
    existing rows, so a later group win strengthened nothing (BUI-381)."""
    api.post("/api/bids", json={"item_id": "381000001", "max_bid": 25.0})
    assert _read_col("381000001", "snipe_group") == 0
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000001", status="SCHEDULED", time_to_end="3 h",
                       snipe_group="4"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_col("381000001", "snipe_group") == 4

    # Un-grouping flows back too: stale membership could otherwise falsely
    # group-cancel a genuine result (the dangerous direction).
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000001", status="SCHEDULED", time_to_end="3 h",
                       snipe_group="0"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_col("381000001", "snipe_group") == 0


def test_sync_unparseable_snipe_group_keeps_db_value(api):
    """A Gixen parse quirk ('N/A') must not clobber real group membership —
    the refresh skips unparseable values rather than coercing to 0."""
    _seed_bid_row("381000002", snipe_group=3)
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000002", status="SCHEDULED", time_to_end="3 h",
                       snipe_group="N/A"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_col("381000002", "snipe_group") == 3


def test_sync_parse_miss_snipe_group_preserves_membership(api):
    """BUI-383: a client-side snipe_group parse miss now arrives as None
    ('unknown') instead of the old '0' — the refresh must skip it so real
    membership survives a transient scrape miss. Pre-fix, the miss arrived
    as the perfectly-parseable '0' and durably CLEARED the group (N → 0),
    weakening the BUI-371 group-cancel evidence."""
    _seed_bid_row("383000001", snipe_group=3)
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("383000001", status="SCHEDULED", time_to_end="3 h",
                       snipe_group=None),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_col("383000001", "snipe_group") == 3

    # A genuine listed '0' is a positive un-group claim and IS mirrored.
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("383000001", status="SCHEDULED", time_to_end="3 h",
                       snipe_group="0"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_col("383000001", "snipe_group") == 0


def test_web_added_bid_insert_survives_parse_miss_snipe_group(api):
    """BUI-383 companion to the 'N/A' case: a brand-new web-added snipe whose
    snipe_group arrives as None (a client parse miss) inserts as group 0 and
    the refresh corrects it once the value parses."""
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("383000002", status="SCHEDULED", time_to_end="3 h",
                       snipe_group=None),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("383000002")["status"] == "PENDING"
    assert _read_col("383000002", "snipe_group") == 0


def test_group_evidence_survives_winner_purge_with_failed_sibling_removal(api):
    """The BUI-381 case-1 regression: a purge sweeps the WON winner to REMOVED
    while its sibling-removal leg fails (sibling stays live). The sibling must
    STILL classify REMOVED at its own end — from the group_wins ledger written
    at WON time, not from the (now destroyed) WON row."""
    from gixen_client import GixenError
    _seed_bid_row("381000003", snipe_group=6, auction_end_at=_iso_ago(days=1))
    _seed_bid_row("381000004", snipe_group=6, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000003", status="WON", time_to_end="ENDED",
                       snipe_group="6", current_bid="20.00 USD"),
        _gixen_listing("381000004", status="SCHEDULED", time_to_end="5 h",
                       snipe_group="6"),
    ]
    api.mock_gixen.remove_snipe.side_effect = GixenError("Gixen down")
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 200
    assert r.json()["removed_siblings"] == 0
    # The sweep destroyed the WON row; the ledger entry survives it.
    assert _read_db_row("381000003")["status"] == "REMOVED"
    assert _read_db_row("381000004")["status"] == "PENDING"
    won = _group_win_row("381000003")
    assert won is not None and won["snipe_group"] == 6

    # The sibling later reaches its own end, still cancelled on Gixen's list.
    conn = _dbconn()
    conn.execute(
        "UPDATE bids SET auction_end_at=? WHERE item_id='381000004'",
        (_iso_ago(hours=1),),
    )
    conn.commit()
    conn.close()
    api.mock_gixen.remove_snipe.side_effect = None
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000004", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="6"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("381000004")["status"] == "REMOVED"


def test_web_added_terminal_winner_classifies_sibling(api, monkeypatch):
    """A winner first seen already-terminal via the web-add path never gets a
    bids row — its win must still be recorded (from the list + eBay's end
    time) so the cancelled sibling resolves REMOVED, not ENDED (BUI-381)."""
    _seed_bid_row("381000006", snipe_group=9, auction_end_at=_iso_ago(hours=1))
    _arm_ebay(monkeypatch, _iso_ago(days=1))  # the winner's true end, from eBay
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000005", status="WON", time_to_end="ENDED",
                       snipe_group="9", current_bid="20.00 USD"),
        _gixen_listing("381000006", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="9"),
    ]
    assert api.post("/api/sync").status_code == 200
    # The winner still never gets a bids row (web-add skips terminal snipes)...
    assert _read_db_row("381000005") is None
    # ...but its win landed in the durable ledger...
    won = _group_win_row("381000005")
    assert won is not None and won["snipe_group"] == 9
    # ...and classified the cancelled sibling.
    assert _read_db_row("381000006")["status"] == "REMOVED"


def test_web_added_terminal_winner_no_ebay_end_stays_permissive(api, monkeypatch):
    """When eBay can't supply the row-less winner's end time, no evidence is
    recorded — an observation-time proxy would be unsound against the
    lifetime bound — and the sibling keeps today's WON-permissive ENDED."""
    _seed_bid_row("381000008", snipe_group=8, auction_end_at=_iso_ago(hours=1))
    _arm_ebay(monkeypatch, None)
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000007", status="WON", time_to_end="ENDED",
                       snipe_group="8", current_bid="20.00 USD"),
        _gixen_listing("381000008", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="8"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _group_win_row("381000007") is None
    assert _read_db_row("381000008")["status"] == "ENDED"


def test_web_added_winner_end_predating_sibling_add_is_not_evidence(api, monkeypatch):
    """Ledger evidence obeys the same lifetime bound as live WON rows: a
    row-less win whose eBay end predates the sibling's added_at is a stale win
    in a recycled group number, not cancel evidence (the BUI-371 review's P0,
    extended to the BUI-381 ledger)."""
    _seed_bid_row("381000010", snipe_group=7, auction_end_at=_iso_ago(hours=1),
                  added_at=_iso_ago(days=2))
    _arm_ebay(monkeypatch, _iso_ago(days=30))  # win long before the sibling existed
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000009", status="WON", time_to_end="ENDED",
                       snipe_group="7", current_bid="20.00 USD"),
        _gixen_listing("381000010", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="7"),
    ]
    assert api.post("/api/sync").status_code == 200
    # Recorded (it is a real win) but excluded by the lifetime bound.
    assert _group_win_row("381000009") is not None
    assert _read_db_row("381000010")["status"] == "ENDED"


def _arm_ebay_counting(monkeypatch, end_iso):
    """_arm_ebay, but returns the list of item_ids fetched from eBay."""
    import server.main as m
    calls = []
    monkeypatch.setattr(m, "_ebay_fetch_bin", lambda: "ebay-fetch")
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)

    def _fetch(iid):
        calls.append(iid)
        return {"end_date_iso": end_iso} if end_iso is not None else None

    monkeypatch.setattr(m, "_fetch_ebay_item_sync", _fetch)
    return calls


def test_web_added_winner_future_ebay_end_not_recorded(api, monkeypatch):
    """eBay reporting a FUTURE end for a snipe Gixen says WON is
    self-contradictory — eBay is describing a different (re-listed same-ID)
    auction. Never recorded; the sibling keeps WON-permissive ENDED."""
    _seed_bid_row("381000012", snipe_group=6, auction_end_at=_iso_ago(hours=1))
    _arm_ebay(monkeypatch, _iso_ago(hours=-5))  # 5 hours in the FUTURE
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000011", status="WON", time_to_end="ENDED",
                       snipe_group="6", current_bid="20.00 USD"),
        _gixen_listing("381000012", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="6"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _group_win_row("381000011") is None
    assert _read_db_row("381000012")["status"] == "ENDED"


def test_web_added_winner_second_sync_skips_ebay_call(api, monkeypatch):
    """Once a row-less winner's evidence is recorded, later syncs (the winner
    stays listed until purge) must not spend another eBay call on it."""
    calls = _arm_ebay_counting(monkeypatch, _iso_ago(days=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000013", status="WON", time_to_end="ENDED",
                       snipe_group="5", current_bid="20.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert api.post("/api/sync").status_code == 200
    assert calls == ["381000013"]  # exactly one fetch across both syncs
    assert _group_win_row("381000013") is not None


def test_web_added_winner_cooldown_blocks_fetch(api, monkeypatch):
    """An active eBay cooldown suppresses the row-less-winner fetch entirely
    (retry on a later sync); nothing is recorded and the sibling keeps the
    WON-permissive ENDED."""
    import server.main as m
    from datetime import datetime, timezone
    calls = _arm_ebay_counting(monkeypatch, _iso_ago(days=1))
    monkeypatch.setattr(
        m, "_ebay_cooldown_until",
        datetime.now(timezone.utc).timestamp() + 600,
    )
    _seed_bid_row("381000015", snipe_group=4, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000014", status="WON", time_to_end="ENDED",
                       snipe_group="4", current_bid="20.00 USD"),
        _gixen_listing("381000015", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="4"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert calls == []
    assert _group_win_row("381000014") is None
    assert _read_db_row("381000015")["status"] == "ENDED"


def test_web_added_winner_fetch_cap_per_sync(api, monkeypatch):
    """Row-less-winner eBay fetches are capped per sync (the BUI-85
    discipline) so a post-outage backlog can't serialize unbounded blocking
    subprocess calls inside one sync."""
    import server.main as m
    calls = _arm_ebay_counting(monkeypatch, _iso_ago(days=1))
    monkeypatch.setattr(m, "_LISTED_WIN_FETCH_MAX_PER_SYNC", 1)
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000016", status="WON", time_to_end="ENDED",
                       snipe_group="2", current_bid="20.00 USD"),
        _gixen_listing("381000017", status="WON", time_to_end="ENDED",
                       snipe_group="3", current_bid="20.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert len(calls) == 1  # budget spent; the second winner waits for the next sync
    assert api.post("/api/sync").status_code == 200
    assert len(calls) == 2  # and gets recorded then


def test_web_added_bid_insert_survives_bad_snipe_group(api):
    """A brand-new non-terminal web-added snipe with an unparseable
    snipe_group must not crash the sync batch (int('N/A') used to raise an
    uncaught ValueError) — it inserts as group 0 and the refresh corrects it
    once the value parses."""
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000018", status="SCHEDULED", time_to_end="3 h",
                       snipe_group="N/A"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("381000018")["status"] == "PENDING"
    assert _read_col("381000018", "snipe_group") == 0


def test_tombstoned_winner_still_records_evidence(api, monkeypatch):
    """A winner whose DB row was tombstoned (user removed it, but Gixen kept
    the snipe and it won) gets eBay-backed ledger evidence — update_bid_status
    skips tombstones, so without this path the win would leave no trace."""
    _seed_bid_row("381000019", status="REMOVED", snipe_group=8,
                  resolved_at=_iso_ago(days=3))
    _seed_bid_row("381000020", snipe_group=8, auction_end_at=_iso_ago(hours=1))
    _arm_ebay(monkeypatch, _iso_ago(days=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("381000019", status="WON", time_to_end="ENDED",
                       snipe_group="8", current_bid="20.00 USD"),
        _gixen_listing("381000020", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="8"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("381000019")["status"] == "REMOVED"  # tombstone kept
    assert _group_win_row("381000019") is not None
    assert _read_db_row("381000020")["status"] == "REMOVED"


# ---------------------------------------------------------------------------
# BUI-410 (Stage 3): _sync_gixen gather-then-apply. These guard the two
# properties the gather-then-apply restructure had to preserve exactly and
# that no pre-existing test covered: (1) the listed-win fetch budget is not
# spent on a Case-A winner (whose evidence update_bid_status records for free),
# so a genuinely row-less winner later in the same sync keeps its fetch — a
# wrong answer either way reintroduces a phantom-WON; and (2) the sync commits
# only through its own write_transaction(), never on the shared _db.
# ---------------------------------------------------------------------------

def test_listed_win_case_a_does_not_consume_fetch_budget(api, monkeypatch):
    """The listed-win eBay fetch budget must NOT be burned on a Case-A winner
    (one with a live PENDING row whose WON transition records the group_win
    itself). If the pre-cycle skip predicate _listed_win_evidence_already_covered
    were wrong and fetched Case-A winners, budget=1 would be exhausted by the
    Case-A winner ordered first, STARVING the genuinely row-less winner behind
    it of its one fetch — leaving that winner's cancelled sibling to phantom-WON
    (the exact BUI-146/371 failure). With the correct predicate, the Case-A
    winner is skipped (no fetch, no budget) and the row-less winner is fetched."""
    import server.main as m
    # Case-A winner: a live PENDING row (group 2) with a genuine end, so its
    # WON transition records its own ledger entry via update_bid_status.
    _seed_bid_row("410000001", snipe_group=2, auction_end_at=_iso_ago(days=1))
    # Row-less (Case-B) winner: group 3, no DB row.
    calls = _arm_ebay_counting(monkeypatch, _iso_ago(days=1))
    monkeypatch.setattr(m, "_LISTED_WIN_FETCH_MAX_PER_SYNC", 1)
    api.mock_gixen.list_snipes.return_value = [
        # Case-A winner listed FIRST — if it wrongly consumed the budget, the
        # row-less winner below would be starved.
        _gixen_listing("410000001", status="WON", time_to_end="ENDED",
                       snipe_group="2", current_bid="20.00 USD"),
        _gixen_listing("410000002", status="WON", time_to_end="ENDED",
                       snipe_group="3", current_bid="20.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    # Only the row-less winner was fetched — the Case-A winner was skipped
    # pre-fetch, so the single budget slot went to the winner that needs it.
    assert calls == ["410000002"]
    # Both winners still leave ledger evidence: the Case-A one via
    # update_bid_status (from its stored end), the row-less one via the fetch.
    assert _read_db_row("410000001")["status"] == "WON"
    assert _group_win_row("410000001") is not None
    assert _group_win_row("410000002") is not None


def test_sync_routes_all_writes_off_shared_db(api, _db_untouched):
    """BUI-410 core isolation property: a full _sync_gixen cycle that applies a
    real terminal transition commits ONLY on its own write_transaction()
    connection — never on the shared singleton _db (whose bleed-across-await
    this whole rollout exists to remove). The transition still lands durably
    (read back via a SEPARATE connection to prove the cross-connection commit
    is visible, not just the app's own connection seeing its uncommitted work)."""
    api.post("/api/bids", json={"item_id": "410000010", "max_bid": 50.0})
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("410000010", status="WON", time_to_end="ENDED",
                       snipe_group="0", current_bid="42.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _db_untouched.commits == 0, (
        "the shared _db was committed by _sync_gixen — it must route through "
        "write_transaction() only (BUI-410)"
    )
    row = _dbconn().execute(
        "SELECT status, winning_bid FROM bids WHERE item_id='410000010'"
    ).fetchone()
    assert row["status"] == "WON"
    assert row["winning_bid"] == 42.0


def test_web_added_bid_insert_survives_bad_bid_offset(api):
    """BUI-410 companion to test_web_added_bid_insert_survives_bad_snipe_group:
    a brand-new web-added snipe whose bid_offset is an unparseable scrape quirk
    ('N/A') must not crash the sync — it inserts with the default offset 6.
    Before Stage 3 an int('N/A') here only failed the (already-committed)
    web-add; now _insert_web_added_bids runs inside _sync_gixen's single
    write_transaction(), so an unguarded crash would abort the whole cycle."""
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "410200001", "max_bid": "30.00 USD", "current_bid": "5.00 USD",
        "status": "SCHEDULED", "time_to_end": "2h", "seller": "s",
        "snipe_group": "0", "bid_offset": "N/A",
    }]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("410200001")["status"] == "PENDING"
    assert _read_col("410200001", "bid_offset") == 6


def test_bad_bid_offset_web_add_does_not_abort_cycle_evidence(api):
    """BUI-410 regression guard (found by code review): a malformed bid_offset
    on ONE web-added snipe must not discard the rest of the cycle's committed
    work. A WON transition applied earlier in the SAME sync (and its group_wins
    evidence) must survive even though a co-listed web-added snipe carries an
    unparseable bid_offset. Pre-fix, the int('N/A') ValueError aborted the whole
    write_transaction(), reverting the WON row to PENDING and dropping its ledger
    entry — leaving a cancelled sibling exposed to the fallback's phantom-WON."""
    _seed_bid_row("410200010", snipe_group=3, auction_end_at=_iso_ago(days=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("410200010", status="WON", time_to_end="ENDED",
                       snipe_group="3", current_bid="20.00 USD"),
        {"item_id": "410200011", "max_bid": "10.00 USD", "current_bid": "1.00 USD",
         "status": "SCHEDULED", "time_to_end": "3h", "seller": "s",
         "snipe_group": "0", "bid_offset": "N/A"},
    ]
    assert api.post("/api/sync").status_code == 200
    # The WON transition + its ledger survived (the cycle was not aborted).
    assert _read_db_row("410200010")["status"] == "WON"
    assert _group_win_row("410200010") is not None
    # ...and the malformed web-added snipe still inserted with the default offset.
    assert _read_db_row("410200011")["status"] == "PENDING"
    assert _read_col("410200011", "bid_offset") == 6


def test_listed_win_divergent_group_records_under_listed_group(api, monkeypatch):
    """BUI-410 _listed_win_evidence_already_covered divergent-group branch
    (flagged untested by code review): a winner whose newest DB row is a
    non-tombstone terminal row carrying a DIFFERENT group than Gixen now lists
    (it resolved before its group was known) must still get eBay-backed ledger
    evidence recorded under the LISTED group — so a sibling in the listed group
    classifies REMOVED. The pre-cycle predicate must return False (fetch) here;
    a wrong skip would drop the evidence and phantom-WON the sibling."""
    # Winner: an ENDED row from a prior cycle carrying group 5, now listed WON
    # under group 7 (the divergent-group case — refresh_snipe_group skips the
    # non-PENDING row, so its stored group stays 5).
    _seed_bid_row("410300001", status="ENDED", snipe_group=5,
                  auction_end_at=_iso_ago(days=1))
    # Sibling in the LISTED group (7), cancelled and reaching its own end.
    _seed_bid_row("410300002", snipe_group=7, auction_end_at=_iso_ago(hours=1))
    _arm_ebay(monkeypatch, _iso_ago(days=1))  # winner's true end, from eBay
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("410300001", status="WON", time_to_end="ENDED",
                       snipe_group="7", current_bid="20.00 USD"),
        _gixen_listing("410300002", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="7"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("410300001")["status"] == "WON"
    # Evidence recorded under the LISTED group (7), not the stale stored group 5.
    conn = _dbconn()
    g7 = conn.execute(
        "SELECT 1 FROM group_wins WHERE item_id='410300001' AND snipe_group=7"
    ).fetchone()
    conn.close()
    assert g7 is not None
    # ...and it classified the group-7 sibling REMOVED.
    assert _read_db_row("410300002")["status"] == "REMOVED"


def test_parse_snipe_group_variants():
    """Blank/absent/unparseable → None (unknown — never coerced to the
    positive 'no group' claim); genuine values, including '0', parse."""
    from server.main import _parse_snipe_group
    assert _parse_snipe_group("3") == 3
    assert _parse_snipe_group(4) == 4
    assert _parse_snipe_group("0") == 0
    assert _parse_snipe_group(0) == 0
    assert _parse_snipe_group(None) is None
    assert _parse_snipe_group("") is None
    assert _parse_snipe_group("   ") is None
    assert _parse_snipe_group("N/A") is None


# ---------------------------------------------------------------------------
# BUI-116: cached-dbidid edit fast-path + staleness fallback
# ---------------------------------------------------------------------------

def _seed_dbidid(item_id, dbidid):
    conn = _dbconn()
    conn.execute("UPDATE bids SET dbidid=? WHERE item_id=?", (dbidid, item_id))
    conn.commit()
    conn.close()


def _read_dbidid(item_id):
    conn = _dbconn()
    row = conn.execute("SELECT dbidid FROM bids WHERE item_id=?", (item_id,)).fetchone()
    conn.close()
    return row["dbidid"] if row else None


def test_edit_bid_uses_cached_dbidid(api):
    api.post("/api/bids", json={"item_id": "810000001", "max_bid": 50.0})
    _seed_dbidid("810000001", "cached5001")
    r = api.patch("/api/bids/810000001", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 200
    assert api.mock_gixen.modify_snipe.call_args.kwargs.get("dbidid") == "cached5001"


def test_edit_bid_null_cache_passes_none(api):
    api.post("/api/bids", json={"item_id": "810000002", "max_bid": 50.0})
    r = api.patch("/api/bids/810000002", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 200
    assert api.mock_gixen.modify_snipe.call_args.kwargs.get("dbidid") is None


def test_edit_bid_stale_dbidid_falls_back_and_clears_cache(api):
    from gixen_client import GixenModifyNotConfirmedError
    api.post("/api/bids", json={"item_id": "810000003", "max_bid": 50.0})
    _seed_dbidid("810000003", "stale")
    # cached attempt unconfirmed; list-based fallback succeeds.
    api.mock_gixen.modify_snipe.side_effect = [
        GixenModifyNotConfirmedError("810000003", 75.0), None,
    ]
    r = api.patch("/api/bids/810000003", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 200
    calls = api.mock_gixen.modify_snipe.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs.get("dbidid") == "stale"   # cached fast path first
    assert calls[1].kwargs.get("dbidid") is None        # list-based fallback
    assert _read_dbidid("810000003") is None            # stale cache cleared
    rows = api.get("/api/bids").json()
    assert next(b for b in rows if b["item_id"] == "810000003")["max_bid"] == 75.0


def test_edit_bid_both_attempts_unconfirmed_returns_503(api):
    from gixen_client import GixenModifyNotConfirmedError
    api.post("/api/bids", json={"item_id": "810000004", "max_bid": 50.0})
    _seed_dbidid("810000004", "stale")
    api.mock_gixen.modify_snipe.side_effect = GixenModifyNotConfirmedError("810000004", 75.0)
    r = api.patch("/api/bids/810000004", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 503
    rows = api.get("/api/bids").json()
    assert next(b for b in rows if b["item_id"] == "810000004")["max_bid"] == 50.0  # DB unchanged


def test_remove_bid_uses_cached_dbidid(api):
    api.post("/api/bids", json={"item_id": "820000001", "max_bid": 50.0})
    _seed_dbidid("820000001", "cached9")
    r = api.delete("/api/bids/820000001")
    assert r.status_code == 200
    assert api.mock_gixen.remove_snipe.call_args.kwargs.get("dbidid") == "cached9"


def test_remove_bid_stale_dbidid_falls_back(api):
    from gixen_client import GixenError
    api.post("/api/bids", json={"item_id": "820000002", "max_bid": 50.0})
    _seed_dbidid("820000002", "stale")
    api.mock_gixen.remove_snipe.side_effect = [GixenError("still in list"), True]
    r = api.delete("/api/bids/820000002")
    assert r.status_code == 200
    calls = api.mock_gixen.remove_snipe.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs.get("dbidid") == "stale"
    assert calls[1].kwargs.get("dbidid") is None
    assert _read_dbidid("820000002") is None


# ─── _parse_time_to_end (BUI-184) ─────────────────────────────────────────────

def test_parse_time_to_end_zero_seconds_is_timedelta_not_none():
    """A snipe seen at exactly '0 s' parses to timedelta(0) so auction_end_at is
    set and the local sniper fires it — not None (BUI-184)."""
    import server.main as m
    from datetime import timedelta

    assert m._parse_time_to_end("0 s") == timedelta(seconds=0)
    assert m._parse_time_to_end("0 m, 0 s") == timedelta(seconds=0)


def test_parse_time_to_end_unparseable_is_none():
    """A genuinely empty/unparseable string still returns None."""
    import server.main as m

    assert m._parse_time_to_end("") is None
    assert m._parse_time_to_end("ENDED") is None
    assert m._parse_time_to_end("garbage") is None


def test_parse_time_to_end_normal_values():
    import server.main as m
    from datetime import timedelta

    assert m._parse_time_to_end("1 d, 20 h, 59 m") == timedelta(
        days=1, hours=20, minutes=59)
    assert m._parse_time_to_end("45 s") == timedelta(seconds=45)


# ---------------------------------------------------------------------------
# BUI-384: late group join must not be backdated — _group_won_before bounds
# evidence by max(added_at, group_changed_at), and every snipe_group write
# path stamps group_changed_at on a real change
# ---------------------------------------------------------------------------

def test_late_group_join_via_sync_not_backdated(api):
    """THE BUI-384 regression (false-REMOVED direction): a snipe added long
    ago and joined to a group on Gixen's web UI only AFTER that group's win
    (the join lands via the BUI-381 sync mirror) must NOT be classified
    REMOVED off the pre-join win — the win predates its membership. It keeps
    the WON-permissive ENDED so a genuine result can still be inferred."""
    _seed_bid_row("384000001", status="WON", snipe_group=3,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    # Added a week ago (well before the win), but grouped only now: the DB
    # row still carries group 0; the list mirrors the retroactive join.
    _seed_bid_row("384000002", snipe_group=0, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("384000002", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="3"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("384000002")["status"] == "ENDED"   # not REMOVED
    # The mirror stamped the membership start that protected it.
    assert _read_col("384000002", "snipe_group") == 3
    assert _read_col("384000002", "group_changed_at") is not None


def test_late_group_join_via_edit_not_backdated(api):
    """Same false-REMOVED shape through the edit path: PATCHing a snipe into
    a group after that group's win stamps group_changed_at, so the pre-join
    win is not cancel evidence at the row's own end."""
    _seed_bid_row("384000003", status="WON", snipe_group=4,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("384000004", snipe_group=0, auction_end_at=_iso_ago(hours=1))
    r = api.patch("/api/bids/384000004",
                  json={"max_bid": 30.0, "snipe_group": 4})
    assert r.status_code == 200
    assert _read_col("384000004", "group_changed_at") is not None

    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("384000004", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="4", max_bid="30.00 USD"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("384000004")["status"] == "ENDED"  # not REMOVED


def test_late_group_join_vanished_row_not_backdated(api):
    """The vanished-ended resolver applies the same membership bound (its row
    query must carry group_changed_at): a late-joined sibling that vanished
    from Gixen's list (purged with the winner, say) still isn't classified
    off the pre-join win."""
    _seed_bid_row("384000005", status="WON", snipe_group=5,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    # Joined the group only 30 minutes ago (post-win), then ended; absent
    # from the (non-empty) list with no pre-end vanish stamp.
    _seed_bid_row("384000006", snipe_group=5, auction_end_at=_iso_ago(hours=1),
                  group_changed_at=_iso_ago(minutes=30))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("384099999", status="SCHEDULED", time_to_end="3 h"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("384000006")["status"] == "ENDED"  # not REMOVED


def test_group_change_before_win_still_classifies_removed(api):
    """Guard against over-suppression: a membership change that PRECEDES the
    win keeps the group-cancel classification — the win fell inside the
    row's membership window."""
    _seed_bid_row("384000007", status="WON", snipe_group=6,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("384000008", snipe_group=6, auction_end_at=_iso_ago(hours=1),
                  group_changed_at=_iso_ago(days=2))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("384000008", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="6"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("384000008")["status"] == "REMOVED"


def test_unparseable_group_changed_at_is_not_evidence(api):
    """A present-but-unparseable membership stamp makes the membership start
    unknowable — WON-permissive: classify nothing."""
    _seed_bid_row("384000009", status="WON", snipe_group=7,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("384000010", snipe_group=7, auction_end_at=_iso_ago(hours=1),
                  group_changed_at="not-a-timestamp")
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("384000010", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="7"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("384000010")["status"] == "ENDED"


def test_null_group_changed_at_keeps_added_at_bound(api):
    """Rows whose group never changed since insert (stamp NULL — every
    pre-migration row) keep the original added_at bound: win inside the
    lifetime → REMOVED, exactly the shipped BUI-371 behavior."""
    _seed_bid_row("384000011", status="WON", snipe_group=8,
                  auction_end_at=_iso_ago(days=1), winning_bid=20.0,
                  resolved_at=_iso_ago(days=1))
    _seed_bid_row("384000012", snipe_group=8, auction_end_at=_iso_ago(hours=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("384000012", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="8"),
    ]
    assert api.post("/api/sync").status_code == 200
    assert _read_db_row("384000012")["status"] == "REMOVED"


# ---------------------------------------------------------------------------
# BUI-385: group_wins forensics endpoint + listed-win provenance end-to-end
# ---------------------------------------------------------------------------

def _seed_group_win(snipe_group, item_id, won_end_at, recorded_at, source):
    conn = _dbconn()
    conn.execute(
        "INSERT INTO group_wins (snipe_group, item_id, won_end_at, recorded_at, "
        "source) VALUES (?, ?, ?, ?, ?)",
        (snipe_group, item_id, won_end_at, recorded_at, source),
    )
    conn.commit()
    conn.close()


def test_get_group_wins_endpoint_shape(api):
    """GET /api/group-wins exposes the ledger with provenance — the forensics
    surface answering 'which win classified this row REMOVED' over HTTP."""
    _seed_group_win(5, "385000001", "2026-06-01T00:00:00+00:00",
                    "2026-06-01T00:05:00+00:00", "status-transition")
    r = api.get("/api/group-wins")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "id", "snipe_group", "item_id", "won_end_at", "recorded_at", "source",
    }
    assert row["snipe_group"] == 5
    assert row["item_id"] == "385000001"
    assert row["won_end_at"] == "2026-06-01T00:00:00+00:00"
    assert row["recorded_at"] == "2026-06-01T00:05:00+00:00"
    assert row["source"] == "status-transition"


def test_get_group_wins_filters_by_group_and_item(api):
    _seed_group_win(5, "385000002", "2026-06-01T00:00:00+00:00",
                    "2026-06-01T00:05:00+00:00", "status-transition")
    _seed_group_win(6, "385000003", "2026-06-02T00:00:00+00:00",
                    "2026-06-02T00:05:00+00:00", "listed-win")
    # snipe_group filter
    rows = api.get("/api/group-wins?snipe_group=6").json()
    assert [r["item_id"] for r in rows] == ["385000003"]
    assert rows[0]["source"] == "listed-win"
    # item_id filter
    rows = api.get("/api/group-wins?item_id=385000002").json()
    assert [r["snipe_group"] for r in rows] == [5]
    # combined, no match
    assert api.get("/api/group-wins?snipe_group=5&item_id=385000003").json() == []


def test_get_group_wins_orders_newest_recorded_first(api):
    _seed_group_win(7, "385000004", "2026-06-01T00:00:00+00:00",
                    "2026-06-01T00:00:00+00:00", "startup-backfill")
    _seed_group_win(7, "385000005", "2026-06-05T00:00:00+00:00",
                    "2026-06-05T00:00:00+00:00", "status-transition")
    rows = api.get("/api/group-wins?snipe_group=7").json()
    assert [r["item_id"] for r in rows] == ["385000005", "385000004"]


def test_web_added_terminal_winner_records_listed_win_source(api, monkeypatch):
    """Writer 3 end-to-end: a row-less winner recorded via the sync's listed-win
    path is tagged source 'listed-win' in the ledger, and the endpoint shows
    it — the full forensics loop for the web-add case."""
    _seed_bid_row("385000007", snipe_group=9, auction_end_at=_iso_ago(hours=1))
    _arm_ebay(monkeypatch, _iso_ago(days=1))
    api.mock_gixen.list_snipes.return_value = [
        _gixen_listing("385000006", status="WON", time_to_end="ENDED",
                       snipe_group="9", current_bid="20.00 USD"),
        _gixen_listing("385000007", status="CANCELLED", time_to_end="ENDED",
                       snipe_group="9"),
    ]
    assert api.post("/api/sync").status_code == 200
    rows = api.get("/api/group-wins?item_id=385000006").json()
    assert len(rows) == 1
    assert rows[0]["source"] == "listed-win"
    assert rows[0]["snipe_group"] == 9


# ---------------------------------------------------------------------------
# BUI-391: _sync_gixen reraise error-path hygiene
# ---------------------------------------------------------------------------

def test_sync_gixen_gixenerror_log_wording_matches_reraise(monkeypatch):
    """BUI-391: the GixenError log claims 'suppressed' only when the exception
    is actually swallowed (reraise=False). On the reraise path (api_sync,
    _sync_loop) it must NOT falsely claim suppression — the exception is about
    to propagate to the caller. Exercised directly: list_snipes raises before
    any DB use, so no lifespan/connection is needed."""
    import asyncio
    from unittest.mock import MagicMock
    import server.main as m

    client = MagicMock()
    client.list_snipes.side_effect = m.GixenError("boom")
    db = MagicMock()  # never touched — the error is raised before any DB work

    logged: list[str] = []
    monkeypatch.setattr(
        m.logger, "warning",
        lambda msg, *a, **k: logged.append(msg % a if a else msg),
    )

    # reraise=False → swallowed → returns [] → log accurately says "suppressed".
    logged.clear()
    assert asyncio.run(m._sync_gixen(db, client, reraise=False)) == []
    gx = [line for line in logged if "GixenError" in line]
    assert gx, "expected a GixenError log line on the suppress path"
    assert all("suppressed" in line and "reraised" not in line for line in gx)

    # reraise=True → propagates → log must NOT claim suppression.
    logged.clear()
    with pytest.raises(m.GixenError):
        asyncio.run(m._sync_gixen(db, client, reraise=True))
    gx = [line for line in logged if "GixenError" in line]
    assert gx, "expected a GixenError log line on the reraise path"
    assert all("reraised" in line and "suppressed" not in line for line in gx)


def test_background_entry_points_swallow_unexpected_exception_without_db_rollback(monkeypatch):
    """BUI-410 (Stage 3 landed): _ensure_fresh_sync and _sync_loop must still
    SWALLOW an unexpected (non-Gixen) exception from _sync_gixen and degrade
    (not propagate), but they no longer reach for `_db.rollback()`. The BUI-391
    rollback is retired because _sync_gixen no longer batches DML on the shared
    singleton `_db` — its writes are on its own write_transaction() (rolled
    back + closed by that factory on error) and its gather phase only reads
    `_db`, so there is no stranded partial cycle to roll back. This test guards
    both halves: the exception is handled, and `_db.rollback()` is NOT called."""
    import asyncio
    from unittest.mock import MagicMock
    import server.main as m

    class _RollbackSpy:
        def __init__(self):
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    # --- _ensure_fresh_sync (dashboard on-visit pull) ---
    async def ensure_case():
        spy = _RollbackSpy()
        monkeypatch.setattr(m, "_db", spy)
        monkeypatch.setattr(m, "_sync_gixen", boom)
        monkeypatch.setattr(m, "_api_client", MagicMock())
        monkeypatch.setattr(m, "_last_sync_at", 0.0)  # force staleness → runs
        # Locks must be created inside this running loop.
        monkeypatch.setattr(m, "_sync_lock", asyncio.Lock())
        monkeypatch.setattr(m, "_api_lock", asyncio.Lock())
        await m._ensure_fresh_sync()  # must swallow (degrade), not raise
        return spy.rollbacks

    assert asyncio.run(ensure_case()) == 0

    # --- _sync_loop (one background iteration) ---
    async def loop_case():
        spy = _RollbackSpy()
        monkeypatch.setattr(m, "_db", spy)
        monkeypatch.setattr(m, "_sync_gixen", boom)
        monkeypatch.setattr(m, "_sync_client", MagicMock())
        task = asyncio.create_task(m._sync_loop())
        await asyncio.sleep(0.05)  # let one iteration run + fail + back off
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return spy.rollbacks

    assert asyncio.run(loop_case()) == 0


# ---------------------------------------------------------------------------
# BUI-410 (Stage 3 landed): api_purge and api_edit_bid no longer roll back the
# shared singleton connection around their _sync_gixen calls — the BUI-386/391/
# 399 per-caller rollback net is superseded now that _sync_gixen writes only
# through its own write_transaction() (see the convention doc). These still
# guard that an unexpected bug is reported as a structured 500 (not an
# unhandled crash) — just WITHOUT a `_db.rollback()`, since there is no
# stranded partial cycle on `_db` to discard.
# ---------------------------------------------------------------------------


class _RollbackSpy:
    """Thin proxy around the app's real DB connection: delegates every
    attribute except rollback() (counted here). A bare MagicMock won't do —
    api_purge/api_edit_bid run real queries (e.g. the completed-bids SELECT,
    get_bid_by_item_id) before the injected _sync_gixen failure fires, and
    sqlite3.Connection is an immutable C type (monkeypatching rollback at the
    class level raises TypeError), so the connection itself can't be spied on
    directly."""

    def __init__(self, real):
        self._real = real
        self.rollbacks = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def rollback(self):
        self.rollbacks += 1
        self._real.rollback()


def test_api_purge_reports_500_on_unexpected_sync_error_without_db_rollback(api, monkeypatch):
    """api_purge's pre-purge sync must still report a structured 500 on a
    genuine unexpected bug (not a GixenError — _sync_gixen's default
    reraise=False swallows those internally), but BUI-410 retired the
    accompanying `_db.rollback()`: _sync_gixen no longer batches DML on the
    shared `_db` (its writes go through write_transaction(), the gather phase
    only reads), so there is no stranded partial cycle to discard."""
    import server.main as m

    spy = _RollbackSpy(m._db)
    monkeypatch.setattr(m, "_db", spy)

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "_sync_gixen", _boom)

    r = api.post("/api/purge", json={})
    assert r.status_code == 500
    assert r.json()["detail"]
    assert spy.rollbacks == 0


def test_api_edit_bid_reports_500_on_unexpected_post_modify_sync_error_without_rollback(api, monkeypatch):
    """api_edit_bid's post-modify sync (the web-added-snipe-not-yet-ingested
    branch) must still report a structured 500 on a genuine unexpected bug —
    Gixen already accepted the modify by this point — but BUI-410 retired the
    `_db.rollback()`: the post-modify _sync_gixen strands nothing on `_db`."""
    import server.main as m

    spy = _RollbackSpy(m._db)
    monkeypatch.setattr(m, "_db", spy)

    async def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "_sync_gixen", _boom)

    # No prior /api/bids insert for this item_id — get_bid_by_item_id returns
    # None after the modify (mock_gixen.modify_snipe succeeds by default),
    # driving api_edit_bid into the post-modify sync branch under test.
    r = api.patch("/api/bids/399000001", json={"max_bid": 25.0})
    assert r.status_code == 500
    assert r.json()["detail"]
    assert spy.rollbacks == 0


# ---------------------------------------------------------------------------
# BUI-394: GET /api/bids exposes `id` + accepts ?item_id=/?snipe_group=
# filters, for correlating a /api/group-wins ledger entry back to the exact
# bids row it classified. Additive only — existing fields/shape unchanged.
# ---------------------------------------------------------------------------

def test_get_all_bids_exposes_id(api):
    api.post("/api/bids", json={"item_id": "394000001", "max_bid": 25.0})
    rows = api.get("/api/bids").json()
    row = next(b for b in rows if b["item_id"] == "394000001")
    assert isinstance(row["id"], int)


def test_get_all_bids_filters_by_item_id(api):
    _seed_bid_row("394000002", snipe_group=5)
    _seed_bid_row("394000003", snipe_group=5)
    rows = api.get("/api/bids?item_id=394000002").json()
    assert [r["item_id"] for r in rows] == ["394000002"]


def test_get_all_bids_filters_by_snipe_group(api):
    _seed_bid_row("394000004", snipe_group=8)
    _seed_bid_row("394000005", snipe_group=8)
    _seed_bid_row("394000006", snipe_group=9)
    rows = api.get("/api/bids?snipe_group=8").json()
    assert sorted(r["item_id"] for r in rows) == ["394000004", "394000005"]


def test_get_all_bids_combined_filters_no_match(api):
    _seed_bid_row("394000007", snipe_group=8)
    assert api.get("/api/bids?snipe_group=8&item_id=394000099").json() == []


def test_get_all_bids_combined_filters_positive_match(api):
    """Both filters together narrow to the single row satisfying both (AND
    semantics), not just the no-match case above."""
    _seed_bid_row("394000011", snipe_group=8)
    _seed_bid_row("394000012", snipe_group=8)
    rows = api.get("/api/bids?snipe_group=8&item_id=394000011").json()
    assert [r["item_id"] for r in rows] == ["394000011"]


def test_get_all_bids_snipe_group_non_integer_returns_422(api):
    r = api.get("/api/bids?snipe_group=notanumber")
    assert r.status_code == 422


def test_get_all_bids_no_filter_returns_full_table_unchanged(api):
    """No filter params → every existing row is returned, same as before
    BUI-394 (contract stability for the no-filter call)."""
    _seed_bid_row("394000008", snipe_group=1)
    _seed_bid_row("394000009", snipe_group=2)
    rows = api.get("/api/bids").json()
    ids = {r["item_id"] for r in rows}
    assert {"394000008", "394000009"}.issubset(ids)


def test_get_all_bids_response_shape_still_includes_pre_existing_fields(api):
    """Guards additive-only: every field present before BUI-394 must still be
    present and unrenamed alongside the new `id` field."""
    api.post("/api/bids", json={"item_id": "394000010", "max_bid": 25.0})
    rows = api.get("/api/bids").json()
    row = next(b for b in rows if b["item_id"] == "394000010")
    assert set(row) == {
        "id", "item_id", "title", "max_bid", "bid_offset", "snipe_group",
        "end_date_iso", "added_at", "status", "status_mirror", "winning_bid",
        "seller", "local_snipe_at", "local_snipe_result", "gixen_vanished_at",
        "notes", "ebay_no_price_at", "group_changed_at",
    }


# ---------------------------------------------------------------------------
# BUI-408 (Stage 1 of BUI-400's shared-connection isolation rollout): the
# already-await-free writers (api_add_bid, api_edit_bid, api_purge,
# _sniper_loop's set_local_snipe_result — the overlay's api_link_locg is
# covered in gixen-overlay's own test suite) now commit on their own
# short-lived write_transaction() connection under the single app-wide
# _write_lock, instead of the shared _db. Tests below cover the two
# regression nets the design doc's §6 calls for (the debug/test invariant
# guard, and the pure lock-free isolation proof) plus confirm each routed
# writer actually holds _write_lock at the moment it commits.
# ---------------------------------------------------------------------------


def _make_lock_checking_connection():
    """Factory (BUI-408 design doc §6's "debug/test invariant guard: fail if
    commit() is ever called while not _write_lock.locked()"). Returns a
    fresh sqlite3.Connection subclass + the list its commit() calls record
    into: each commit() appends whether server.main._write_lock was held at
    that instant, then asserts it.

    sqlite3.Connection is an immutable C type (monkeypatching commit()
    directly raises TypeError — see test_server_db.py's
    test_write_transaction_calls_rollback_explicitly_on_exception), so a
    factory subclass passed as sqlite3.connect(..., factory=...) is the
    supported way to observe a real connection's method calls. A fresh class
    per call keeps each test's record list isolated."""
    import server.main as m

    record: list[bool] = []

    class _Checking(sqlite3.Connection):
        def commit(self):
            locked = bool(m._write_lock and m._write_lock.locked())
            record.append(locked)
            assert locked, (
                "BUI-408 invariant violated: commit() called while "
                "_write_lock was not held"
            )
            return super().commit()

    return _Checking, record


@pytest.fixture
def _write_lock_guard(api, monkeypatch):
    """Scoped commit-lock invariant guard. Depends on `api` so fixture setup
    order guarantees the app's long-lived _db is already open (via the real
    lifespan) BEFORE this patches server.db's `sqlite3.connect` — so this
    only ever intercepts write_transaction()'s ephemeral write connections,
    never _db's own startup-migration commits. Returns the observed-lock-
    state list so a test can assert the guard actually engaged (non-empty)
    rather than passing vacuously."""
    import server.db as db_module

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    monkeypatch.setattr(db_module.sqlite3, "connect", _connect_with_check)
    return record


class _DbCommitSpy:
    """Proxy around the app's real `_db` connection: delegates everything
    except commit(), which is counted (BUI-408, mirrors the existing
    _RollbackSpy pattern below/BUI-399). Complementary proof that the
    shared `_db` is genuinely never committed by a routed writer — closes a
    gap `_write_lock_guard` alone can't: that guard only observes
    connections opened via `write_transaction()` AFTER it patches
    `sqlite3.connect`, so it structurally cannot see a stray commit on the
    already-open `_db` (a regression that kept the correct
    `write_transaction()` call but ALSO left a stray `db.commit()` on `_db`
    would pass `_write_lock_guard`'s checks unnoticed)."""

    def __init__(self, real):
        self._real = real
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        self.commits += 1
        self._real.commit()


@pytest.fixture
def _db_untouched(api, monkeypatch):
    """See _DbCommitSpy. As of BUI-410 (Stage 3) this is now safe to attach to
    a test that triggers _sync_gixen too: the sync no longer commits on the
    shared `_db` (all its DML goes through its own write_transaction(), the
    gather phase only reads), so asserting zero `_db` commits across a sync is
    a genuine Stage-3 regression guard, not a false failure. (The one direct
    `_db` writer still standing is api_remove_bid's delete_bid()/commit(), so
    keep this off the remove path.)"""
    import server.main as m
    spy = _DbCommitSpy(m._db)
    monkeypatch.setattr(m, "_db", spy)
    return spy


def test_add_bid_commits_under_write_lock(api, _write_lock_guard, _db_untouched):
    """Covers _add_bid_row's insert path."""
    r = api.post("/api/bids", json={"item_id": "408000001", "max_bid": 12.0})
    assert r.status_code == 200
    assert _write_lock_guard, "guard never observed a commit — test is vacuous"
    assert all(_write_lock_guard)
    assert _db_untouched.commits == 0, (
        "the shared _db was committed — this writer must route through "
        "write_transaction() only"
    )


def test_readd_existing_commits_under_write_lock(api, _write_lock_guard, _db_untouched):
    """Covers _modify_and_update_bid's write path (api_add_bid's modify
    branch, taken when a live PENDING row already exists)."""
    api.post("/api/bids", json={"item_id": "408000002", "max_bid": 12.0})
    r = api.post("/api/bids", json={"item_id": "408000002", "max_bid": 15.0})
    assert r.status_code == 200
    assert _write_lock_guard
    assert all(_write_lock_guard)
    assert _db_untouched.commits == 0, (
        "the shared _db was committed — this writer must route through "
        "write_transaction() only"
    )


def test_edit_bid_commits_under_write_lock(api, _write_lock_guard, _db_untouched):
    """Covers api_edit_bid's main update_bid write. The item is seeded first
    so get_bid_by_item_id finds it after the modify — never falls into the
    post-sync-reapply branch (which, as of BUI-410, also commits only through
    write_transaction(), never on `_db`), so asserting zero `_db` commits is
    safe here."""
    api.post("/api/bids", json={"item_id": "408000003", "max_bid": 12.0})
    r = api.patch(
        "/api/bids/408000003",
        json={"max_bid": 20.0, "bid_offset": 6, "snipe_group": 0},
    )
    assert r.status_code == 200
    assert _write_lock_guard
    assert all(_write_lock_guard)
    assert _db_untouched.commits == 0, (
        "the shared _db was committed — this writer must route through "
        "write_transaction() only"
    )


def test_edit_bid_cache_clear_retry_commits_under_write_lock(api, monkeypatch):
    """Covers _modify_with_cache_fallback's _clear_cached_dbidid write path —
    the GixenModifyNotConfirmedError retry branch, reached only when a
    cached dbidid exists.

    Does NOT use the `_write_lock_guard` fixture: `sqlite3.connect` is one
    shared attribute on the single process-wide `sqlite3` module object, so
    patching it (as that fixture does) patches EVERY caller, including this
    test's own admin seed write via `_dbconn()`. Seed first, THEN install
    the guard, so only the write under test is checked. Also asserts the
    shared `_db` is never committed (complementary to `_db_untouched` — see
    that fixture's docstring; this test predates seeding _db_untouched's
    manual-pattern siblings, so it inlines the same _DbCommitSpy check)."""
    from gixen_client import GixenModifyNotConfirmedError
    import server.db as db_module
    import server.main as m

    api.post("/api/bids", json={"item_id": "408000004", "max_bid": 12.0})
    conn = _dbconn()
    conn.execute(
        "UPDATE bids SET dbidid=? WHERE item_id=?", ("cache408004", "408000004")
    )
    conn.commit()
    conn.close()

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    monkeypatch.setattr(db_module.sqlite3, "connect", _connect_with_check)
    db_spy = _DbCommitSpy(m._db)
    monkeypatch.setattr(m, "_db", db_spy)

    api.mock_gixen.modify_snipe.side_effect = [
        GixenModifyNotConfirmedError("408000004", 20.0), None,
    ]
    r = api.patch(
        "/api/bids/408000004",
        json={"max_bid": 20.0, "bid_offset": 6, "snipe_group": 0},
    )
    assert r.status_code == 200
    assert record, "guard never observed a commit — test is vacuous"
    assert all(record)
    assert db_spy.commits == 0, (
        "the shared _db was committed — this writer must route through "
        "write_transaction() only"
    )


def test_purge_commits_under_write_lock(api, _write_lock_guard, _db_untouched):
    """Covers both api_purge write sites: mark_bids_purged and the sibling
    loop's delete_bid (reuses test_purge_removes_siblings' setup).

    BUI-410: api_purge's step 1 pre-purge `_sync_gixen` now routes all its DML
    through write_transaction() too (never `_db`), so `_db_untouched` is now a
    valid guard here — it directly proves the sync was routed off the shared
    connection, the core Stage-3 property. mark_bids_purged + delete_bid + the
    sync all commit under `_write_lock` (checked by `_write_lock_guard`)."""
    api.post("/api/bids", json={"item_id": "408000005", "max_bid": 50.0})
    api.mock_gixen.list_snipes.return_value = [
        {
            "item_id": "408000005", "status": "WON", "snipe_group": "1",
            "title": "Item A", "max_bid": "50.00 USD", "current_bid": "45.00 USD",
            "time_to_end": "ENDED", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "a1",
        },
        {
            "item_id": "408000006", "status": "SCHEDULED", "snipe_group": "1",
            "title": "Item A alt", "max_bid": "50.00 USD", "current_bid": "0.00 USD",
            "time_to_end": "5h 0m", "seller": "s",
            "bid_offset": "6", "bid_offset_mirror": "6", "dbidid": "b2",
        },
    ]
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 200
    assert r.json()["removed_siblings"] == 1
    assert _write_lock_guard
    assert all(_write_lock_guard)
    # BUI-410: the pre-purge _sync_gixen (which set 408000005 WON) committed
    # only on its own write_transaction() connection, never on the shared _db.
    assert _db_untouched.commits == 0, (
        "the shared _db was committed during purge — _sync_gixen must route "
        "through write_transaction() only (BUI-410)"
    )


def test_add_bid_integrity_recovery_commits_under_write_lock(api, monkeypatch):
    """Covers _add_bid_row's IntegrityError-recovery write_transaction()
    block (main.py's second _write_locked() site inside _add_bid_row) —
    reuses test_add_defensive_integrity_recovery's stale-read setup to force
    the partial-unique-index collision recovery path. Not using
    `_write_lock_guard`: seed first, THEN install the guard (see
    test_edit_bid_cache_clear_retry_commits_under_write_lock's docstring for
    why — sqlite3.connect is one shared process-wide attribute)."""
    import server.db as db_module
    import server.main as m

    seed = _dbconn()
    seed.execute(
        "INSERT INTO bids (item_id, max_bid, status) VALUES ('408000030', 10.0, 'PENDING')"
    )
    seed.commit()
    seed.close()
    real = m.get_pending_bid_by_item_id
    state = {"first": True}

    def stale_then_real(c, iid):
        if state["first"]:
            state["first"] = False
            return None
        return real(c, iid)

    monkeypatch.setattr("server.main.get_pending_bid_by_item_id", stale_then_real)

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    monkeypatch.setattr(db_module.sqlite3, "connect", _connect_with_check)
    db_spy = _DbCommitSpy(m._db)
    monkeypatch.setattr(m, "_db", db_spy)

    r = api.post("/api/bids", json={"item_id": "408000030", "max_bid": 22.0})
    assert r.status_code == 200
    assert r.json()["created"] is False
    assert record, "guard never observed a commit — test is vacuous"
    assert all(record)
    assert db_spy.commits == 0, (
        "the shared _db was committed — this writer must route through "
        "write_transaction() only"
    )


def test_remove_bid_cache_clear_retry_commits_under_write_lock(api, monkeypatch):
    """Covers _remove_with_cache_fallback's _clear_cached_dbidid write path
    (api_remove_bid's stale-cached-dbidid retry branch) — mirrors
    test_edit_bid_cache_clear_retry_commits_under_write_lock's pattern for
    the DELETE endpoint. Not using `_write_lock_guard`: seed first, THEN
    install the guard (see that test's docstring for why).

    No `_DbCommitSpy`/`_db_untouched` check here (unlike its edit-endpoint
    sibling): api_remove_bid is NOT in BUI-408's scope (the ticket names
    only api_add_bid/api_edit_bid/api_purge) — its own final
    `delete_bid(db, item_id); db.commit()` still legitimately writes
    through the shared `_db`, only the shared `_clear_cached_dbidid` helper
    call (used by both this and api_edit_bid) was routed. Asserting zero
    `_db` commits here would be a false failure, not a caught regression.
    """
    from gixen_client import GixenError
    import server.db as db_module

    api.post("/api/bids", json={"item_id": "408000031", "max_bid": 12.0})
    conn = _dbconn()
    conn.execute(
        "UPDATE bids SET dbidid=? WHERE item_id=?", ("cache408031", "408000031")
    )
    conn.commit()
    conn.close()

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    monkeypatch.setattr(db_module.sqlite3, "connect", _connect_with_check)

    api.mock_gixen.remove_snipe.side_effect = [GixenError("stale dbidid"), True]
    r = api.delete("/api/bids/408000031")
    assert r.status_code == 200
    assert record, "guard never observed a commit — test is vacuous"
    assert all(record)


def test_edit_bid_post_sync_reapply_commits_under_write_lock(api, monkeypatch):
    """Covers api_edit_bid's post-sync re-apply write_transaction() block —
    the self-heal-via-sync branch reached when Gixen accepts the modify but
    the item has no DB row yet (mirrors
    test_edit_bid_not_in_db_self_heals_via_sync's setup). Not using
    `_write_lock_guard`: this test has no admin seed write to worry about,
    but the guard must still be installed before the PATCH call, which is
    equivalent here — kept manual for consistency with the other two
    call-site tests above.

    BUI-410: this branch's whole POINT is running `_sync_gixen`, which as of
    Stage 3 commits only through its own write_transaction() (a _Checking
    connection here, so its commit is one of the `record` entries and is
    asserted to be under `_write_lock` too) — it no longer commits on the
    shared `_db`.
    """
    import server.db as db_module

    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "408000032",
        "max_bid": "75.00 USD",
        "current_bid": "10.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "1d",
        "seller": "someseller",
        "snipe_group": "0",
        "bid_offset": "6",
    }]

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    monkeypatch.setattr(db_module.sqlite3, "connect", _connect_with_check)

    r = api.patch(
        "/api/bids/408000032",
        json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0},
    )
    assert r.status_code == 200
    assert "added_at" in r.json()  # proves the post-sync branch actually ran
    assert record, "guard never observed a commit — test is vacuous"
    assert all(record)


def test_sniper_loop_commits_under_write_lock(tmp_path, monkeypatch):
    """BUI-408: no test anywhere drives _sniper_loop directly (see
    test_server_db.py's BUI-407 comment on set_local_snipe_result) — this is
    the only coverage for both the commit-free contract AND, now, the lock
    invariant on this specific writer. Follows
    test_background_entry_points_rollback_on_unexpected_exception's pattern
    above: drive the loop as a real background task against a real on-disk
    DB, bypassing the TestClient/lifespan portal entirely so the
    asyncio.Lock created below lives on the SAME loop asyncio.run() drives
    this test on (a Lock built on a different loop raises on first `async
    with`)."""
    import asyncio
    import server.db as db_module
    import server.main as m
    from server.db import init_db

    path = tmp_path / "sniper.db"
    db = init_db(path)
    db.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, bid_offset) "
        "VALUES ('408000010', 15.0, 'PENDING', '2020-01-01T00:00:00+00:00', 6)"
    )
    db.commit()

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    class _FakeBidder:
        async def place_bids_concurrent(self, bids):
            return [{"success": True, "message": "placed"} for _ in bids]

    # _sniper_loop never calls _sync_gixen, so — unlike api_purge/the
    # post-sync-reapply branch — the shared _db has no legitimate commit of
    # its own here; wrap it in _DbCommitSpy so a regression that routed
    # set_local_snipe_result back through _db would be caught, not just one
    # that skipped _write_lock.
    db_spy = _DbCommitSpy(db)
    monkeypatch.setattr(m, "_db", db_spy)
    monkeypatch.setattr(m, "_db_path", path)
    monkeypatch.setattr(m, "_bidder", _FakeBidder())
    monkeypatch.setattr(db_module.sqlite3, "connect", _connect_with_check)

    async def run():
        # Created inside the running loop asyncio.run() drives this test on.
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        task = asyncio.create_task(m._sniper_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    row = db.execute(
        "SELECT local_snipe_result FROM bids WHERE item_id='408000010'"
    ).fetchone()
    db.close()

    assert row["local_snipe_result"] == "OK: placed"
    assert record, "guard never observed a commit — test is vacuous"
    assert all(record)
    assert db_spy.commits == 0, (
        "the shared _db was committed — this writer must route through "
        "write_transaction() only"
    )


def test_write_lock_invariant_guard_fires_without_lock(tmp_path):
    """BUI-408 design doc §6: proves the guard mechanism itself is live —
    calling write_transaction() directly, bypassing _write_locked(), must
    trip the assertion. This is the regression net that would catch a
    future writer that forgets to acquire the lock."""
    import asyncio
    import server.db as db_module
    from server.db import init_db, insert_bid, write_transaction

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    path = tmp_path / "invariant_unlocked.db"
    init_db(path).close()

    async def unlocked_write():
        with write_transaction(path) as conn:  # bypasses _write_locked()
            insert_bid(conn, "408200001", 10.0, 6, 0, "s")

    with patch.object(db_module.sqlite3, "connect", _connect_with_check):
        with pytest.raises(AssertionError, match="BUI-408 invariant"):
            asyncio.run(unlocked_write())

    assert record == [False], "guard must observe exactly one unlocked commit attempt"


def test_write_lock_invariant_guard_silent_when_locked(tmp_path, monkeypatch):
    """Complement to the test above: the correct _write_locked()-wrapped
    pattern every Stage-1 writer now uses must NOT trip the guard."""
    import asyncio
    import server.db as db_module
    import server.main as m
    from server.db import init_db, insert_bid, write_transaction

    checking_cls, record = _make_lock_checking_connection()
    real_connect = db_module.sqlite3.connect

    def _connect_with_check(path_arg, *a, **kw):
        return real_connect(path_arg, *a, factory=checking_cls, **kw)

    path = tmp_path / "invariant_locked.db"
    init_db(path).close()
    monkeypatch.setattr(m, "_db_path", path)

    async def locked_write():
        # Created inside the running loop asyncio.run() drives this on.
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        async with m._write_locked():
            with write_transaction(m._get_db_path()) as conn:
                insert_bid(conn, "408200002", 10.0, 6, 0, "s")

    with patch.object(db_module.sqlite3, "connect", _connect_with_check):
        asyncio.run(locked_write())  # must NOT raise

    assert record == [True], "guard must observe exactly one LOCKED commit"


def test_lockfree_sync_write_and_overlay_write_are_isolated(tmp_path):
    """BUI-408 design doc §6 'pure lock-free regression': before Stage 1,
    the overlay's api_link_locg wrote + committed directly on the shared
    _db with NO lock (design doc §2's second lock-free writer, alongside
    _sync_loop -> _sync_gixen) — a shared connection meant either side's
    commit()/rollback() could flush or discard the OTHER's not-yet-committed
    DML (design doc §1's exposure). Stage 1 moves the overlay's write onto
    its OWN write_transaction() connection — still no lock (only the four
    gixen-cli writers + set_local_snipe_result take _write_lock; the
    overlay's remaining lock-free peer, _sync_gixen, is untouched — Stage 3
    territory) — so this proves isolation now comes from being on SEPARATE
    connections, not from an app-level asyncio.Lock (`_write_lock`): a
    _sync_gixen-style batcher (DML on _db, held open across a delay — the
    "await" bleed window design doc §1 identifies) and an api_link_locg-style
    write (write_transaction() on its own connection, committing while the
    batcher is still uncommitted) can interleave with ZERO app-level lock and
    neither corrupts the other's DATA. This does NOT prove the two can never
    contend or block each other — SQLite's own single-writer-at-a-time
    constraint (see below) still serializes them at the engine level; that
    residual busy_timeout/stall exposure between the migrated writers and
    the still-untouched _sync_gixen (BUI-409 already migrated
    _run_ebay_fallback) is real and is Stage 3's job to close, not this
    test's.

    Uses a real background thread (not asyncio) for the batcher's delay:
    SQLite allows only one writer transaction at a time process-wide even in
    WAL mode, so once the overlay's write below legitimately contends for
    that slot, its blocking C-level busy_timeout retry would otherwise
    freeze the single-threaded event loop and deadlock against a same-loop
    coroutine's timer (verified — an asyncio.Event-gated version of this
    test hangs, and production code never crosses threads with a connection
    either — a single uvicorn worker, one event loop). A background thread's
    commit doesn't depend on the event loop, so busy_timeout's
    retry-then-succeed behavior works as designed (verified against a
    minimal reproduction outside pytest first). check_same_thread=False is
    a test-harness-only device to get that real concurrent write window;
    the batcher connection is used from exactly one thread at a time
    (main thread only up to thread.start(), background thread only after).
    """
    import threading
    import time
    from server.db import init_db, insert_bid, write_transaction

    path = tmp_path / "lockfree_regression.db"
    init_db(path).close()  # create schema on a throwaway connection first
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = sqlite3.Row

    batcher_dml_landed = threading.Event()

    def batcher():
        # Mimics _sync_gixen's shape: DML on the shared _db, THEN a delay
        # (the bleed window), THEN commit — deliberately with NO lock,
        # matching _sync_loop's lock-free design.
        insert_bid(db, "408300001", 11.0, 6, 0, "batcher")
        batcher_dml_landed.set()
        time.sleep(0.2)
        db.commit()

    thread = threading.Thread(target=batcher)
    thread.start()
    batcher_dml_landed.wait()

    # Mimics api_link_locg's Stage-1 write: its OWN write_transaction()
    # connection, no lock — the zero-lock case. The batcher's transaction is
    # still open here (mid-sleep); write_transaction()'s busy_timeout=5000
    # makes this block briefly and then succeed once the batcher commits and
    # releases SQLite's single-writer slot, rather than corrupting data or
    # erroring.
    with write_transaction(path) as wconn:
        insert_bid(wconn, "408300002", 22.0, 6, 0, "overlay")

    thread.join()
    db.close()

    check = sqlite3.connect(str(path))
    check.row_factory = sqlite3.Row
    try:
        rows = {r["item_id"] for r in check.execute("SELECT item_id FROM bids").fetchall()}
    finally:
        check.close()

    assert "408300001" in rows, "the batcher's write must land once IT commits"
    assert "408300002" in rows, (
        "the overlay's isolated write_transaction() write must land "
        "independent of the batcher's still-uncommitted DML on _db"
    )
