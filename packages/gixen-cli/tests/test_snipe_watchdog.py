"""BUI-604: the per-snipe outcome watchdog.

The watchdog treats every PENDING snipe as a healthcheck: near its end it
expects the snipe to still be listed on Gixen, and after its end it expects a
terminal status to arrive within a deadline. The ABSENCE of that transition —
not an error — is the alert.

Two properties matter more than any single verdict here, and both have tests
that would fail loudly if a future change traded them away:

  * The watchdog is READ-ONLY. It must never write a bids row. Flipping a row
    out of PENDING would rob the local sniper of a real bid
    (get_bids_ready_to_snipe fires on PENDING rows) or expose a stray one, and
    the eBay WON-inference must never be gated by anything here
    (BUI-146/371/381). test_the_endpoint_writes_nothing pins that against the
    whole bids table, not against a code path.
  * It never cries wolf about a silence it caused itself. If our own Gixen sync
    is stale, "no outcome arrived" is explained by our blindness, so the
    per-snipe alerts are withheld and the stale sync is reported instead.
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gixen_client import GixenConnectionError  # noqa: E402
from server import main as smain  # noqa: E402
from server.db import init_db  # noqa: E402

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
CYCLE = timedelta(seconds=smain.SYNC_INTERVAL)


def _row(**over):
    """A PENDING bids row as _classify_watchdog_row sees it."""
    base = {
        "id": 1,
        "item_id": "111111111",
        "ebay_title": "Amazing Spider-Man #238",
        "auction_end_at": (NOW + CYCLE).isoformat(),
        "gixen_vanished_at": None,
        "local_snipe_result": None,
    }
    base.update(over)
    return base


def _classify(**over):
    return smain._classify_watchdog_row(_row(**over), NOW, None)


# --------------------------------------------------------------------------
# the post-end deadline — the ticket's core alert
# --------------------------------------------------------------------------

def test_a_snipe_still_pending_past_the_deadline_is_the_alert():
    bucket, alert = _classify(
        auction_end_at=(NOW - smain._WATCHDOG_POST_END_DEADLINE - CYCLE).isoformat(),
    )
    assert bucket == "missing_outcome"
    assert alert["kind"] == "missing_outcome"
    assert alert["item_id"] == "111111111"
    assert alert["seconds_overdue"] > smain._WATCHDOG_POST_END_DEADLINE.total_seconds()


def test_a_snipe_inside_the_deadline_is_not_yet_an_alert():
    """The deadline exists because resolution legitimately takes cycles — one
    to observe the end, one for the BUI-417 deferral. Alerting sooner would
    fire on every normally-resolving snipe."""
    bucket, alert = _classify(
        auction_end_at=(NOW - smain._WATCHDOG_POST_END_DEADLINE + CYCLE).isoformat(),
    )
    assert (bucket, alert) == ("ok", None)


def test_an_auction_that_ended_while_the_server_was_down_does_not_alarm_on_restart():
    """The overdue clock runs from max(end, process start), so a 3-day outage
    does not produce a wall of alerts the moment the box comes back — the sync
    gets its full deadline of real uptime to resolve the backlog first."""
    just_started = NOW - CYCLE
    bucket, _ = smain._classify_watchdog_row(
        _row(auction_end_at=(NOW - timedelta(days=3)).isoformat()),
        NOW,
        just_started,
    )
    assert bucket == "ok"


def test_the_restart_grace_expires_rather_than_suppressing_forever():
    started = NOW - smain._WATCHDOG_POST_END_DEADLINE - CYCLE
    bucket, alert = smain._classify_watchdog_row(
        _row(auction_end_at=(NOW - timedelta(days=3)).isoformat()), NOW, started,
    )
    assert bucket == "missing_outcome"
    # Reported overdue is measured from the AUCTION end, not the grace floor —
    # a human needs to know the book has been unaccounted for three days.
    assert alert["seconds_overdue"] > 3 * 86400 - 60


def test_a_locally_fired_snipe_past_the_deadline_still_alerts():
    """The local backup bidder placing a bid is not an outcome. If Gixen never
    reported one, the result of that bid is still unknown — and carrying the
    local result into the payload is what tells a human where to look."""
    bucket, alert = _classify(
        auction_end_at=(NOW - smain._WATCHDOG_POST_END_DEADLINE - CYCLE).isoformat(),
        local_snipe_result="OK: bid placed",
    )
    assert bucket == "missing_outcome"
    assert alert["local_snipe_result"] == "OK: bid placed"


# --------------------------------------------------------------------------
# the pre-end presence check
# --------------------------------------------------------------------------

def test_a_confirmed_vanish_inside_the_pre_end_window_alerts():
    bucket, alert = _classify(
        auction_end_at=(NOW + CYCLE).isoformat(),
        gixen_vanished_at=(NOW - smain._WATCHDOG_VANISH_CONFIRM - timedelta(seconds=1)).isoformat(),
    )
    assert bucket == "vanished_before_end"
    assert alert["seconds_to_end"] == int(CYCLE.total_seconds())


def test_a_vanish_stamp_younger_than_one_cycle_is_not_yet_evidence():
    """_record_vanish_observations clears the stamp the moment the row
    reappears, so a single transient scrape miss heals on the next pass. A
    banner raised before that pass had its chance is a wolf cry."""
    bucket, alert = _classify(
        auction_end_at=(NOW + CYCLE).isoformat(),
        gixen_vanished_at=(NOW - timedelta(seconds=5)).isoformat(),
    )
    assert (bucket, alert) == ("ok", None)


def test_a_vanish_far_from_the_end_is_not_worth_interrupting_anyone():
    bucket, alert = _classify(
        auction_end_at=(NOW + smain._WATCHDOG_PRE_END_WINDOW + CYCLE).isoformat(),
        gixen_vanished_at=(NOW - 5 * CYCLE).isoformat(),
    )
    assert (bucket, alert) == ("ok", None)


def test_a_snipe_still_listed_on_gixen_is_ok_however_close_to_its_end():
    bucket, alert = _classify(
        auction_end_at=(NOW + timedelta(seconds=30)).isoformat(),
        gixen_vanished_at=None,
    )
    assert (bucket, alert) == ("ok", None)


def test_a_null_end_pending_row_is_context_never_an_alert():
    """A PENDING row with no captured end is the BUI-85/BUI-417 deferral case —
    it is SUPPOSED to sit PENDING for a cycle, and it is inert to the local
    sniper. Neither check can run on it, so it is counted, not alerted."""
    bucket, alert = _classify(auction_end_at=None)
    assert (bucket, alert) == ("unknown_end", None)


def test_an_unparseable_end_does_not_raise():
    bucket, alert = _classify(auction_end_at="not-a-timestamp")
    assert (bucket, alert) == ("unknown_end", None)


def test_a_naive_sqlite_timestamp_is_read_as_utc_not_a_crash():
    """bids timestamps are UTC, but SQLite's own defaults render naive. A
    naive/aware comparison would raise TypeError and 500 the endpoint."""
    bucket, _ = _classify(
        auction_end_at=(NOW - smain._WATCHDOG_POST_END_DEADLINE - CYCLE)
        .replace(tzinfo=None).isoformat(sep=" "),
    )
    assert bucket == "missing_outcome"


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

def test_every_window_is_a_multiple_of_the_sync_interval():
    """Resolution only happens on a completed sync pass, so the windows are
    measured in cycles. A round number of minutes would drift out of meaning
    the moment GIXEN_SYNC_INTERVAL is tuned."""
    for window in (
        smain._WATCHDOG_PRE_END_WINDOW,
        smain._WATCHDOG_VANISH_CONFIRM,
        smain._WATCHDOG_POST_END_DEADLINE,
        smain._WATCHDOG_SYNC_STALE_AFTER,
    ):
        assert window.total_seconds() % smain.SYNC_INTERVAL == 0


def test_the_observation_gate_is_tighter_than_the_post_end_deadline():
    """Load-bearing ordering: it makes "no outcome for 4 cycles" unstateable
    from observations older than 3, so the watchdog can never blame Gixen for a
    silence that is really our own sync being dead."""
    assert smain._WATCHDOG_SYNC_STALE_AFTER < smain._WATCHDOG_POST_END_DEADLINE


# --------------------------------------------------------------------------
# the whole report, against a real DB
# --------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "watchdog.db"


@pytest.fixture
def db(db_path):
    conn = init_db(db_path)
    yield conn
    conn.close()


def _seed(conn, item_id, status, *, end_at=None, vanished_at=None):
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, gixen_vanished_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, 20.0, status, end_at, vanished_at),
    )
    conn.commit()


def _report(conn, **over):
    kwargs = {
        "process_started_at": 0.0,
        "last_sync_ok_at": NOW.timestamp() - 5,
        "last_sync_snipe_count": 3,
    }
    kwargs.update(over)
    return smain._build_snipe_watchdog_report(conn, NOW, **kwargs)


LONG_AGO = (NOW - timedelta(days=2)).isoformat()


def test_terminal_and_tombstoned_rows_are_never_reported_as_missing(db):
    """A tombstone is not a terminal auction outcome (BUI-49) — and it is
    equally not a MISSING one. Selecting PENDING only gets both right at once.
    Covers every status the CHECK constraint allows, so a future status that
    forgets this distinction fails here.
    """
    for status in ("WON", "LOST", "FAILED", "ENDED", "REMOVED", "PURGED"):
        _seed(db, f"9{status}", status, end_at=LONG_AGO)
    report = _report(db)
    assert report["alerts"] == []
    assert report["healthy"] is True
    assert report["pending_watched"] == 0


def test_the_alert_clears_when_the_snipe_finally_resolves(db):
    """The verdict is a pure function of current state — there is nothing
    stored, so there is nothing to acknowledge and nothing to leave stuck on.
    """
    _seed(db, "222222222", "PENDING", end_at=LONG_AGO)
    assert _report(db)["counts"]["missing_outcome"] == 1

    db.execute("UPDATE bids SET status='ENDED' WHERE item_id='222222222'")
    db.commit()
    after = _report(db)
    assert after["alerts"] == []
    assert after["healthy"] is True


def test_the_alert_clears_when_the_snipe_reappears_on_gixen(db):
    _seed(
        db, "333333333", "PENDING",
        end_at=(NOW + CYCLE).isoformat(),
        vanished_at=(NOW - 2 * CYCLE).isoformat(),
    )
    assert _report(db)["counts"]["vanished_before_end"] == 1

    # What _record_vanish_observations does the moment the row is listed again.
    db.execute("UPDATE bids SET gixen_vanished_at=NULL WHERE item_id='333333333'")
    db.commit()
    assert _report(db)["alerts"] == []


def test_a_stale_sync_withholds_per_snipe_alerts_and_says_why(db):
    _seed(db, "444444444", "PENDING", end_at=LONG_AGO)
    stale_by = smain._WATCHDOG_SYNC_STALE_AFTER.total_seconds() + 60
    report = _report(db, last_sync_ok_at=NOW.timestamp() - stale_by)

    assert [a["kind"] for a in report["alerts"]] == ["sync_stale"]
    assert report["healthy"] is False
    assert report["sync"]["state"] == "stale"
    # De-escalated, never hidden.
    assert report["suppressed_alert_count"] == 1
    assert report["counts"]["missing_outcome"] == 1


def test_a_server_that_has_never_synced_is_not_healthy(db):
    report = _report(db, last_sync_ok_at=0.0)
    assert report["sync"]["state"] == "never"
    assert report["sync"]["last_ok_at"] is None
    assert report["healthy"] is False


def test_a_quiet_period_with_no_snipes_at_all_is_healthy(db):
    """BUI-263's lesson: zero live snipes is a quiet week, not an outage."""
    report = _report(db, last_sync_snipe_count=0)
    assert report["healthy"] is True
    assert report["alerts"] == []
    assert report["sync"]["last_snipe_count"] == 0


def test_the_report_names_the_windows_it_judged_by(db):
    """The thresholds move with GIXEN_SYNC_INTERVAL, so a consumer reading a
    verdict has to be able to see which ones produced it."""
    windows = _report(db)["windows"]
    assert windows["sync_interval_seconds"] == smain.SYNC_INTERVAL
    assert windows["post_end_deadline_seconds"] == int(
        smain._WATCHDOG_POST_END_DEADLINE.total_seconds()
    )


# --------------------------------------------------------------------------
# the endpoint + the sync stamp
# --------------------------------------------------------------------------

def _mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.purge_completed.return_value = None
    return m


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: [],
    )
    monkeypatch.setenv("DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    # BUI-573: keep both background loops out of this test's event loop.
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    with patch("server.main.GixenClient", return_value=_mock_gixen()):
        from server.main import app
        with TestClient(app) as client:
            yield client


def _bids_snapshot():
    conn = sqlite3.connect(os.environ["DB_PATH"])
    try:
        return conn.execute("SELECT * FROM bids ORDER BY id").fetchall()
    finally:
        conn.close()


def test_the_endpoint_writes_nothing(api):
    """The safety property this whole ticket rests on, pinned against the bids
    table rather than against a code path: a watchdog that can flip a row out
    of PENDING can rob a real bid or expose a stray one."""
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at) "
        "VALUES ('555555555', 30.0, 'PENDING', ?)",
        ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),),
    )
    conn.commit()
    conn.close()

    before = _bids_snapshot()
    for _ in range(3):
        assert api.get("/api/snipe-watchdog").status_code == 200
    assert _bids_snapshot() == before


def test_the_endpoint_does_not_drive_the_sync_it_observes(api):
    """No _ensure_fresh_sync, no fallback spawn: an observer that perturbs the
    thing it observes is not an observer, and a dashboard poll must not be able
    to schedule work on the money path."""
    with patch("server.main._ensure_fresh_sync") as ensure, \
         patch("server.main._spawn_fallback_task") as spawn:
        assert api.get("/api/snipe-watchdog").status_code == 200
    ensure.assert_not_called()
    spawn.assert_not_called()


def test_the_endpoint_returns_the_report_shape(api):
    body = api.get("/api/snipe-watchdog").json()
    assert set(body) >= {
        "generated_at", "healthy", "alerts", "counts",
        "suppressed_alert_count", "pending_watched", "sync", "windows",
    }
    assert isinstance(body["alerts"], list)


def test_a_completed_sync_stamps_the_watchdogs_observation(monkeypatch, db, db_path):
    monkeypatch.setattr(smain, "_last_sync_ok_at", 0.0)
    monkeypatch.setattr(smain, "_last_sync_snipe_count", None)
    monkeypatch.setattr(smain, "_db_path", db_path)

    client = MagicMock()
    client.list_snipes.return_value = []
    import asyncio

    async def run():
        smain._write_lock = asyncio.Lock()
        await smain._sync_gixen(db, client)

    asyncio.run(run())
    assert smain._last_sync_ok_at > 0
    assert smain._last_sync_snipe_count == 0


def test_a_failed_sync_does_not_stamp_a_healthy_observation(monkeypatch, db):
    """Blindness is exactly the state the watchdog has to notice, so a Gixen
    failure must not look like a completed pass."""
    monkeypatch.setattr(smain, "_last_sync_ok_at", 0.0)
    client = MagicMock()
    client.list_snipes.side_effect = GixenConnectionError("gixen down")
    import asyncio

    asyncio.run(smain._sync_gixen(db, client))
    assert smain._last_sync_ok_at == 0.0
