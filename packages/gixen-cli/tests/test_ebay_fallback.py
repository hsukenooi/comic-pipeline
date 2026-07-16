"""Tests for the subprocess-based eBay fallback fetch (BUI-66).

The server no longer module-imports ebay_fetch; _fetch_ebay_item_sync shells
out to the `ebay-fetch` console script with --json. These tests mock the
subprocess so they don't hit eBay or require the binary to be installed.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import server.main as m


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["ebay-fetch", "x", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def ebay_ready(monkeypatch):
    """Pretend the binary is resolvable and credentials are present."""
    monkeypatch.setattr(m, "_ebay_fetch_bin", lambda: "ebay-fetch")
    monkeypatch.setattr(m, "_ebay_creds_available", lambda: True)


def test_fetch_returns_first_item(ebay_ready):
    item = {"item_id": "123", "title": "Amazing Spider-Man #300",
            "current_price": "$250.00", "end_date_iso": "2026-06-01T12:00:00.000Z"}
    with patch.object(m.subprocess, "run", return_value=_completed(json.dumps([item]))) as run:
        result = m._fetch_ebay_item_sync("123")
    assert result == item
    # invoked the console script with --json for that item id
    args = run.call_args[0][0]
    assert args[0] == "ebay-fetch" and "123" in args and "--json" in args


def test_fetch_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(m, "_ebay_fetch_bin", lambda: None)
    with patch.object(m.subprocess, "run") as run:
        assert m._fetch_ebay_item_sync("123") is None
    run.assert_not_called()  # no subprocess spawned when binary missing


def test_fetch_no_creds_returns_none(monkeypatch):
    monkeypatch.setattr(m, "_ebay_fetch_bin", lambda: "ebay-fetch")
    monkeypatch.setattr(m, "_ebay_creds_available", lambda: False)
    with patch.object(m.subprocess, "run") as run:
        assert m._fetch_ebay_item_sync("123") is None
    run.assert_not_called()


def test_fetch_nonzero_exit_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run",
                      return_value=_completed("", returncode=1, stderr="boom")):
        assert m._fetch_ebay_item_sync("123") is None


def test_fetch_empty_array_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run", return_value=_completed("[]")):
        assert m._fetch_ebay_item_sync("123") is None


def test_fetch_bad_json_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run", return_value=_completed("not json")):
        assert m._fetch_ebay_item_sync("123") is None


def test_fetch_timeout_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired(cmd="ebay-fetch", timeout=30)):
        assert m._fetch_ebay_item_sync("123") is None


# ---------------------------------------------------------------------------
# BUI-371: _run_ebay_fallback must not phantom-WON a cancelled snipe, while
# the BUI-146 genuine-win inference stays fully intact.
# ---------------------------------------------------------------------------

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from server.db import init_db


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _seed(conn: sqlite3.Connection, item_id: str, *, status="ENDED",
          max_bid=25.0, snipe_group=0, auction_end_at=None,
          gixen_vanished_at=None, added_at=None):
    # added_at defaults to a week ago so seeded rows predate any group win the
    # test stages (the _group_won_before lifetime bound requires the win to
    # fall at or after the classified row's added_at).
    if added_at is None:
        added_at = _iso(timedelta(days=-7))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, "
        "auction_end_at, gixen_vanished_at, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, max_bid, status, snipe_group, auction_end_at,
         gixen_vanished_at, added_at),
    )
    conn.commit()


def _run_fallback(monkeypatch, conn, *, price="$10.00"):
    """Drive _run_ebay_fallback against a real (tmp) DB with eBay mocked."""
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(
        m, "_fetch_ebay_item_sync",
        lambda iid: {"item_id": iid, "title": "T", "current_price": price,
                     "end_date_iso": None},
    )

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)
    asyncio.run(m._run_ebay_fallback())


def _status_row(conn, item_id):
    return conn.execute(
        "SELECT status, winning_bid FROM bids WHERE item_id=?", (item_id,)
    ).fetchone()


def test_fallback_genuine_win_still_inferred(tmp_path, monkeypatch):
    """BUI-146 regression guard: an ENDED row with no cancel evidence and a
    final price below our max is still inferred WON — the inference is how
    genuine wins are recovered when Gixen drops an ended snipe early."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000001", auction_end_at=_iso(timedelta(hours=-1)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "471000001")
    conn.close()
    assert row["status"] == "WON"
    assert row["winning_bid"] == 10.0


def test_fallback_vanish_evidence_blocks_phantom_won(tmp_path, monkeypatch):
    """A snipe observed vanished from Gixen well before its end was cancelled
    while live — never bid on — so a below-max final price must resolve
    REMOVED, not WON. Covers the fallback-beats-sync race and the original
    BUI-146 manual-cancel trigger (no bid group needed)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000002", status="PENDING",
          auction_end_at=_iso(timedelta(hours=-1)),
          gixen_vanished_at=_iso(timedelta(hours=-3)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "471000002")
    conn.close()
    assert row["status"] == "REMOVED"
    assert row["winning_bid"] == 10.0  # final price recorded for history parity


def test_fallback_group_evidence_heals_legacy_ended_sibling(tmp_path, monkeypatch):
    """An un-purged group-cancelled sibling already flipped ENDED (e.g. before
    this fix deployed, or while Gixen was unreachable) is classified REMOVED by
    the fallback itself — group-win evidence, no vanish stamp needed."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000003", status="WON", snipe_group=2,
          auction_end_at=_iso(timedelta(days=-1)))
    conn.execute("UPDATE bids SET winning_bid=20.0 WHERE item_id='471000003'")
    conn.commit()
    _seed(conn, "471000004", snipe_group=2,
          auction_end_at=_iso(timedelta(hours=-1)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "471000004")
    conn.close()
    assert row["status"] == "REMOVED"


@pytest.mark.parametrize("vanish_offset_min", [
    -65,   # 5 min before end: inside the safety margin — ambiguous
    -55,   # 5 min after end: consistent with an executed snipe Gixen dropped
])
def test_fallback_ambiguous_vanish_still_infers_won(tmp_path, monkeypatch,
                                                    vanish_offset_min):
    """Vanish stamps at/after end (or within the margin) are not cancel
    evidence — the WON-permissive inference must keep running (BUI-146)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000005",
          auction_end_at=_iso(timedelta(hours=-1)),
          gixen_vanished_at=_iso(timedelta(minutes=vanish_offset_min)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "471000005")
    conn.close()
    assert row["status"] == "WON"


def test_fallback_vanish_evidence_blocks_lost_pollution(tmp_path, monkeypatch):
    """Cancel evidence with a final price at/above our max would otherwise
    record LOST — a loss we never contested, polluting the calibration
    report. It must resolve REMOVED instead (BUI-371 secondary)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000006", status="PENDING",
          auction_end_at=_iso(timedelta(hours=-1)),
          gixen_vanished_at=_iso(timedelta(hours=-3)))
    _run_fallback(monkeypatch, conn, price="$30.00")
    row = _status_row(conn, "471000006")
    conn.close()
    assert row["status"] == "REMOVED"


def test_fallback_reused_group_old_win_still_infers_won(tmp_path, monkeypatch):
    """The P0 guard from review: a WON row left over from a prior campaign in
    a recycled group number — ended before this snipe was added — is NOT
    cancel evidence. The genuine-win inference must still run, or a real win
    on the new campaign would be suppressed (the exact BUI-146 failure mode
    the lifetime bound exists to prevent)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000007", status="WON", snipe_group=3,
          auction_end_at=_iso(timedelta(days=-30)),
          added_at=_iso(timedelta(days=-37)))
    conn.execute("UPDATE bids SET winning_bid=20.0 WHERE item_id='471000007'")
    conn.commit()
    # New unrelated snipe reusing group 3, added long after that old win.
    _seed(conn, "471000008", snipe_group=3,
          auction_end_at=_iso(timedelta(hours=-1)),
          added_at=_iso(timedelta(days=-2)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "471000008")
    conn.close()
    assert row["status"] == "WON"
    assert row["winning_bid"] == 10.0


def test_fallback_no_price_with_evidence_still_removed(tmp_path, monkeypatch):
    """Cancel evidence with no usable final price resolves REMOVED with no
    winning-bid claim (rather than the generic no-price ENDED)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000009", status="PENDING",
          auction_end_at=_iso(timedelta(hours=-1)),
          gixen_vanished_at=_iso(timedelta(hours=-3)))
    _run_fallback(monkeypatch, conn, price=None)
    row = _status_row(conn, "471000009")
    conn.close()
    assert row["status"] == "REMOVED"
    assert row["winning_bid"] is None


def test_fallback_local_ok_bid_overrides_cancel_evidence(tmp_path, monkeypatch):
    """An 'OK:' local_snipe_result proves our local sniper actually fired a
    bid — 'cancelled, never bid' cannot apply no matter what the vanish stamp
    suggests, so the normal WON inference runs."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471000010", status="PENDING",
          auction_end_at=_iso(timedelta(hours=-1)),
          gixen_vanished_at=_iso(timedelta(hours=-3)))
    conn.execute(
        "UPDATE bids SET local_snipe_result='OK: bid placed' "
        "WHERE item_id='471000010'"
    )
    conn.commit()
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "471000010")
    conn.close()
    assert row["status"] == "WON"
