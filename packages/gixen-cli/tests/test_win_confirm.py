"""Tests for the BUI-1346 win confirmation (server/win_confirm.py).

Gixen marks a snipe WON when its max equals the final price; eBay gives the
tie to the earlier bidder. The check reverses WON -> LOST only on a positive
OUTBID from eBay. A failed lookup, an unresolved item, or a null status
changes nothing. The `ebay-bid-status` subprocess is mocked.
"""
import asyncio
import json
import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as m
from server import win_confirm as wc
from server.db import (
    WIN_REVERSED_NOTE, get_bid_by_id, init_db, mark_bids_purged, record_group_win,
    update_bid_status,
)

TIE_ITEM = "188733995492"  # one of the two known phantom wins


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "wc.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(wc, "_last_lookup_at", 0.0)
    monkeypatch.setattr(wc, "_lookup_failing", False)
    monkeypatch.setattr(m, "_ebay_bid_status_bin", lambda: "ebay-bid-status")


def _seed_won(conn, item_id=TIE_ITEM, *, max_bid=25.0, winning_bid=25.0,
              ended=timedelta(hours=-2), notes=None, snipe_group=0):
    end = _iso(ended)
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, winning_bid, snipe_group, "
        "auction_end_at, resolved_at, notes) VALUES (?, ?, 'WON', ?, ?, ?, ?, ?)",
        (item_id, max_bid, winning_bid, snipe_group, end, end, notes),
    )
    conn.commit()
    return cur.lastrowid


def _result(item_id, status, *, resolved=True, price=25.0):
    return {"item_id": item_id, "resolved": resolved, "status": status,
            "final_price": price, "currency": "USD", "source": "itemBiddingActivity"}


def _completed(payload=None, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["ebay-bid-status"], returncode=returncode,
        stdout=json.dumps(payload) if payload is not None else "", stderr=stderr,
    )


def _run(monkeypatch, conn, proc):
    """Run one confirmation pass against `conn` with subprocess.run mocked."""
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", Path(conn.execute("PRAGMA database_list").fetchone()[2]))

    async def go():
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        return await wc.run_win_confirmation(force=True)

    kwargs = {"side_effect": proc} if isinstance(proc, BaseException) or callable(proc) else {"return_value": proc}
    with patch.object(wc.subprocess, "run", **kwargs) as run:
        reversed_count = asyncio.run(go())
    return reversed_count, run


def test_tie_outbid_reverses_won_to_lost(monkeypatch, conn):
    row_id = _seed_won(conn)
    before = get_bid_by_id(conn, row_id)
    n, run = _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "OUTBID")]))
    assert n == 1
    args = run.call_args[0][0]
    assert args[0] == "ebay-bid-status" and TIE_ITEM in args and "--json" in args
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "LOST"
    assert row["ebay_bid_status"] == "OUTBID"
    assert row["ebay_bid_checked_at"] is not None
    assert row["notes"] == WIN_REVERSED_NOTE
    assert "BUI-1346" in row["notes"]
    assert row["winning_bid"] == 25.0
    # Never touches the sniper-arming end time or the original resolution time.
    assert row["auction_end_at"] == before["auction_end_at"]
    assert row["resolved_at"] == before["resolved_at"]


def test_reversed_row_survives_next_gixen_sync_won_write(monkeypatch, conn):
    """Gixen keeps listing the tie as WON; the next sync's update_bid_status
    WON must not undo the reversal."""
    row_id = _seed_won(conn)
    _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "OUTBID")]))
    update_bid_status(conn, TIE_ITEM, "WON", 25.0, _iso(timedelta()), "Won", only_id=row_id)
    update_bid_status(conn, TIE_ITEM, "WON", 25.0, _iso(timedelta()))  # item-wide form too
    conn.commit()
    assert get_bid_by_id(conn, row_id)["status"] == "LOST"


def test_reversal_keeps_group_wins_ledger_row(monkeypatch, conn):
    """Gixen cancelled the siblings on its belief it won, so the evidence stays."""
    row_id = _seed_won(conn, snipe_group=3)
    end = get_bid_by_id(conn, row_id)["auction_end_at"]
    record_group_win(conn, TIE_ITEM, 3, end, recorded_at=end)
    conn.commit()
    _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "OUTBID")]))
    assert conn.execute("SELECT COUNT(*) FROM group_wins WHERE item_id=?", (TIE_ITEM,)).fetchone()[0] == 1


def test_existing_note_is_kept_and_marker_appended(monkeypatch, conn):
    row_id = _seed_won(conn, notes="user note")
    _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "OUTBID")]))
    assert get_bid_by_id(conn, row_id)["notes"] == f"user note; {WIN_REVERSED_NOTE}"


def test_failed_lookup_changes_nothing_and_logs_once(monkeypatch, conn, caplog):
    """An auth/scope error (the base-scope production token) is a failed lookup."""
    row_id = _seed_won(conn)
    fail = _completed(returncode=5, stderr="ebay-bid-status: every eBay source failed")
    with caplog.at_level(logging.WARNING, logger=m.logger.name):
        n1, _ = _run(monkeypatch, conn, fail)
        n2, run2 = _run(monkeypatch, conn, fail)
    assert n1 == n2 == 0
    run2.assert_called_once()  # still retried: nothing was stamped
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "WON"
    assert row["ebay_bid_checked_at"] is None and row["ebay_bid_status"] is None
    assert row["notes"] is None
    assert sum("lookup failed" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("proc", [
    _completed(returncode=3, stderr="token expired"),
    _completed(payload=None),  # empty stdout -> bad JSON
    _completed(payload={"not": "a list"}),
    subprocess.TimeoutExpired(cmd="ebay-bid-status", timeout=600),
    OSError("exec failed"),
])
def test_any_lookup_failure_is_a_no_op(monkeypatch, conn, proc):
    row_id = _seed_won(conn)
    n, _ = _run(monkeypatch, conn, proc)
    assert n == 0
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "WON" and row["ebay_bid_checked_at"] is None


def test_null_status_keeps_won_and_stamps_checked(monkeypatch, conn):
    """eBay saw no bid of ours (Gixen never fired): change nothing but mark
    the row checked so it is not re-queried forever."""
    row_id = _seed_won(conn)
    n, _ = _run(monkeypatch, conn, _completed([_result(TIE_ITEM, None)]))
    assert n == 0
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "WON"
    assert row["ebay_bid_status"] is None
    assert row["ebay_bid_checked_at"] is not None
    assert wc.candidate_rows(conn, datetime.now(timezone.utc)) == []


def test_winning_keeps_won_and_stamps_checked(monkeypatch, conn):
    row_id = _seed_won(conn)
    _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "WINNING")]))
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "WON" and row["ebay_bid_status"] == "WINNING"
    assert row["ebay_bid_checked_at"] is not None


@pytest.mark.parametrize("res", [
    _result(TIE_ITEM, "OUTBID", resolved=False),  # e.g. absent from WonList/LostList
    _result(TIE_ITEM, "SOMETHING_NEW"),
])
def test_unresolved_or_unknown_answer_changes_nothing(monkeypatch, conn, res):
    row_id = _seed_won(conn)
    _run(monkeypatch, conn, _completed([res]))
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "WON" and row["ebay_bid_checked_at"] is None


def test_missing_item_in_output_changes_nothing(monkeypatch, conn):
    row_id = _seed_won(conn)
    _run(monkeypatch, conn, _completed([_result("999", "OUTBID")]))
    assert get_bid_by_id(conn, row_id)["status"] == "WON"


def test_candidate_window(conn):
    now = datetime.now(timezone.utc)
    recent = _seed_won(conn, "1", ended=timedelta(hours=-2))
    _seed_won(conn, "2", ended=timedelta(minutes=-5))   # not settled yet
    _seed_won(conn, "3", ended=timedelta(days=-100))    # past eBay's ~90 days
    _seed_won(conn, "test-item")                        # non-numeric: would fail the batch
    checked = _seed_won(conn, "4")
    conn.execute("UPDATE bids SET ebay_bid_checked_at='x' WHERE id=?", (checked,))
    conn.execute("INSERT INTO bids (item_id, max_bid, status, auction_end_at) "
                 "VALUES ('5', 9.0, 'LOST', ?)", (_iso(timedelta(hours=-2)),))
    conn.commit()
    assert [r["id"] for r in wc.candidate_rows(conn, now)] == [recent]


def test_row_changed_since_gather_is_skipped(conn):
    """TOCTOU: a row purged between gather and apply is not written."""
    row_id = _seed_won(conn)
    rows = wc.candidate_rows(conn, datetime.now(timezone.utc))
    conn.execute("UPDATE bids SET status='REMOVED' WHERE id=?", (row_id,))
    assert wc.apply_results(conn, rows, {TIE_ITEM: _result(TIE_ITEM, "OUTBID")}, "now") == 0
    assert get_bid_by_id(conn, row_id)["status"] == "REMOVED"


def test_throttle_skips_lookup_within_interval(monkeypatch, conn):
    _seed_won(conn)
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(wc, "_last_lookup_at", datetime.now(timezone.utc).timestamp())
    with patch.object(wc.subprocess, "run") as run:
        assert asyncio.run(wc.run_win_confirmation()) == 0
    run.assert_not_called()


def test_missing_console_script_skips_subprocess(monkeypatch, conn):
    row_id = _seed_won(conn)
    monkeypatch.setattr(m, "_ebay_bid_status_bin", lambda: None)
    _, run = _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "OUTBID")]))
    run.assert_not_called()
    assert get_bid_by_id(conn, row_id)["status"] == "WON"


def test_migration_adds_columns_to_existing_schema_db(tmp_path):
    """An existing pre-BUI-1346 DB (bids already rebuilt, columns absent) gains
    both columns; legacy WON rows read NULL and still accept a WON write."""
    db_path = tmp_path / "existing.db"
    first = init_db(db_path)
    first.execute("INSERT INTO bids (item_id, max_bid, status) VALUES ('old1', 10.0, 'WON')")
    first.commit()
    first.close()
    raw = sqlite3.connect(str(db_path))
    raw.execute("ALTER TABLE bids DROP COLUMN ebay_bid_status")
    raw.execute("ALTER TABLE bids DROP COLUMN ebay_bid_checked_at")
    raw.commit()
    raw.close()
    conn = init_db(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bids)")}
        assert {"ebay_bid_status", "ebay_bid_checked_at"} <= cols
        row = conn.execute("SELECT * FROM bids WHERE item_id='old1'").fetchone()
        assert row["status"] == "WON" and row["ebay_bid_status"] is None
        update_bid_status(conn, "old1", "WON", 9.0, "2026-11-01T00:00:00+00:00", only_id=row["id"])
        conn.commit()
        assert conn.execute("SELECT winning_bid FROM bids WHERE item_id='old1'").fetchone()[0] == 9.0
    finally:
        conn.close()


# --- BUI-1348: a WON purged before it was checked ------------------------------


def test_purge_swept_won_is_still_checked_and_reversed(monkeypatch, conn):
    """POST /api/purge tombstones every WON (mark_bids_purged), often inside
    SETTLE. The swept row must still be checked: it stays REMOVED (the purge
    still hides it) but its prior_status, which the first-party comps read,
    flips WON -> LOST and it carries the reversal marker."""
    row_id = _seed_won(conn, ended=timedelta(minutes=-10))  # not settled yet
    mark_bids_purged(conn, [TIE_ITEM])
    conn.commit()
    assert get_bid_by_id(conn, row_id)["status"] == "REMOVED"
    # Later, once settled:
    conn.execute("UPDATE bids SET auction_end_at=? WHERE id=?",
                 (_iso(timedelta(hours=-2)), row_id))
    conn.commit()
    n, _ = _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "OUTBID")]))
    assert n == 1
    row = get_bid_by_id(conn, row_id)
    assert row["status"] == "REMOVED"
    assert row["prior_status"] == "LOST"
    assert row["ebay_bid_status"] == "OUTBID"
    assert row["ebay_bid_checked_at"] is not None
    assert row["notes"] == WIN_REVERSED_NOTE


def test_purge_swept_winning_keeps_prior_won(monkeypatch, conn):
    row_id = _seed_won(conn)
    mark_bids_purged(conn, [TIE_ITEM])
    conn.commit()
    n, _ = _run(monkeypatch, conn, _completed([_result(TIE_ITEM, "WINNING")]))
    assert n == 0
    row = get_bid_by_id(conn, row_id)
    assert (row["status"], row["prior_status"]) == ("REMOVED", "WON")
    assert row["ebay_bid_status"] == "WINNING" and row["ebay_bid_checked_at"]


def test_purge_between_gather_and_apply_still_reverses(conn):
    """TOCTOU: the purge running between gather and apply does not lose the
    check; the reversal lands on prior_status instead."""
    row_id = _seed_won(conn)
    rows = wc.candidate_rows(conn, datetime.now(timezone.utc))
    mark_bids_purged(conn, [TIE_ITEM])
    assert wc.apply_results(conn, rows, {TIE_ITEM: _result(TIE_ITEM, "OUTBID")}, "now") == 1
    row = get_bid_by_id(conn, row_id)
    assert (row["status"], row["prior_status"]) == ("REMOVED", "LOST")


def test_tombstone_without_prior_won_is_not_a_candidate(conn):
    """A REMOVED row that was never a Gixen WON (a removed live snipe, a
    swept LOST, or a pre-BUI-660 tombstone with no prior_status) is skipped."""
    for item, prior in (("11", "PENDING"), ("12", "LOST"), ("13", None)):
        conn.execute(
            "INSERT INTO bids (item_id, max_bid, status, prior_status, auction_end_at) "
            "VALUES (?, 9.0, 'REMOVED', ?, ?)", (item, prior, _iso(timedelta(hours=-2))),
        )
    conn.commit()
    assert wc.candidate_rows(conn, datetime.now(timezone.utc)) == []
