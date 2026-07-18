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
from pathlib import Path

from server.db import init_db


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _db_path_of(conn: sqlite3.Connection) -> str:
    """BUI-409: _run_ebay_fallback's apply phase now opens its OWN
    write_transaction() connection resolved via main._get_db_path() — a
    SEPARATE connection from the seed/assert `conn` this file's tests build
    with init_db(). Both point at the same on-disk WAL file (committed
    writes on one are visible to the other), so tests just need to hand
    main._db_path the same path `conn` was opened on. PRAGMA database_list's
    3rd column is that path, avoiding a second `tmp_path` thread-through at
    every call site."""
    return conn.execute("PRAGMA database_list").fetchone()[2]


def _seed(conn: sqlite3.Connection, item_id: str, *, status="ENDED",
          max_bid=25.0, snipe_group=0, auction_end_at=None,
          gixen_vanished_at=None, added_at=None, group_changed_at=None):
    # added_at defaults to a week ago so seeded rows predate any group win the
    # test stages (the _group_won_before lifetime bound requires the win to
    # fall at or after the classified row's added_at).
    if added_at is None:
        added_at = _iso(timedelta(days=-7))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, "
        "auction_end_at, gixen_vanished_at, added_at, group_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, max_bid, status, snipe_group, auction_end_at,
         gixen_vanished_at, added_at, group_changed_at),
    )
    conn.commit()


def _run_fallback(monkeypatch, conn, *, price="$10.00"):
    """Drive _run_ebay_fallback against a real (tmp) DB with eBay mocked.

    BUI-409: the apply phase now runs inside `async with m._write_locked():`
    and resolves its write_transaction() connection via `m._get_db_path()`
    (a SEPARATE connection from `conn`, same on-disk file — see
    `_db_path_of`'s docstring), so both must be set alongside `m._db`.
    `_write_lock` is created here rather than at module scope so it's bound
    to the SAME loop asyncio.run() below drives this on (the established
    convention — see test_server_api.py's test_sniper_loop_commits_under_
    write_lock)."""
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", Path(_db_path_of(conn)))
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

    async def run():
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        await m._run_ebay_fallback()

    asyncio.run(run())


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


# ---------------------------------------------------------------------------
# BUI-381: the fallback consults the durable group_wins ledger, and its own
# WON inference feeds it.
# ---------------------------------------------------------------------------

from server.db import record_group_win


def test_fallback_ledger_evidence_blocks_phantom_won_after_winner_purged(
        tmp_path, monkeypatch):
    """Winner-row destruction (the BUI-381 case-1 window): with no WON bids row
    anywhere — it was swept to REMOVED by a purge — a ledger entry recorded at
    WON time still classifies the cancelled sibling REMOVED, not WON."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "481000001", snipe_group=2,
          auction_end_at=_iso(timedelta(hours=-1)))
    record_group_win(conn, "481000099", 2, _iso(timedelta(days=-1)))
    conn.commit()
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "481000001")
    conn.close()
    assert row["status"] == "REMOVED"


def test_fallback_ledger_respects_lifetime_bound(tmp_path, monkeypatch):
    """A ledger win that predates the sibling's added_at is a stale entry from
    a recycled group number — the genuine-win inference must still run."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "481000002", snipe_group=3,
          auction_end_at=_iso(timedelta(hours=-1)),
          added_at=_iso(timedelta(days=-2)))
    record_group_win(conn, "481000098", 3, _iso(timedelta(days=-30)))
    conn.commit()
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "481000002")
    conn.close()
    assert row["status"] == "WON"
    assert row["winning_bid"] == 10.0


def test_fallback_late_group_join_not_backdated(tmp_path, monkeypatch):
    """BUI-384 in the fallback path: an ENDED row that joined its group only
    AFTER the group's win (group_changed_at postdates the win) is not
    classified REMOVED off that pre-membership win — the genuine-win
    inference must still run. Also exercises _ebay_fallback_rows carrying
    group_changed_at through to _cancelled_before_end."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "484000001", status="WON", snipe_group=2,
          auction_end_at=_iso(timedelta(days=-1)))
    conn.execute("UPDATE bids SET winning_bid=20.0 WHERE item_id='484000001'")
    conn.commit()
    _seed(conn, "484000002", snipe_group=2,
          auction_end_at=_iso(timedelta(hours=-1)),
          group_changed_at=_iso(timedelta(minutes=-30)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "484000002")
    conn.close()
    assert row["status"] == "WON"
    assert row["winning_bid"] == 10.0


def test_fallback_group_change_before_win_still_blocks_phantom_won(
        tmp_path, monkeypatch):
    """Converse guard: a membership change that PRECEDES the win keeps the
    fallback's cancel classification — REMOVED, not a phantom WON."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "484000003", status="WON", snipe_group=3,
          auction_end_at=_iso(timedelta(days=-1)))
    conn.execute("UPDATE bids SET winning_bid=20.0 WHERE item_id='484000003'")
    conn.commit()
    _seed(conn, "484000004", snipe_group=3,
          auction_end_at=_iso(timedelta(hours=-1)),
          group_changed_at=_iso(timedelta(days=-2)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "484000004")
    conn.close()
    assert row["status"] == "REMOVED"


def test_fallback_inferred_won_records_group_evidence(tmp_path, monkeypatch):
    """A WON inferred by the fallback itself on a grouped row lands in the
    ledger — so even this recovered win survives a later purge sweep as
    evidence for its siblings."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "481000003", snipe_group=4,
          auction_end_at=_iso(timedelta(hours=-1)))
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "481000003")
    ledger = conn.execute(
        "SELECT * FROM group_wins WHERE item_id='481000003'"
    ).fetchone()
    conn.close()
    assert row["status"] == "WON"
    assert ledger is not None
    assert ledger["snipe_group"] == 4


# ---------------------------------------------------------------------------
# BUI-385: re-keying the ledger to (group, item, won_end_at) records genuine
# re-wins at distinct ends WITHOUT enabling a false REMOVED — the member_since
# bound in _group_won_before still gates every stored end.
# ---------------------------------------------------------------------------

def test_fallback_rewin_distinct_ends_each_gated_by_its_own_bound(
        tmp_path, monkeypatch):
    """Two genuine wins for the same (group, item) are recorded at distinct
    ends (the BUI-385 re-win case), positioned so EACH is excluded by a
    DIFFERENT bound in _group_won_before: end A predates the sibling's
    membership (lower bound), end B falls after the sibling's own auction end
    (upper/dual-win-margin bound). With both ledger rows present, the sibling
    still resolves WON — a distinct-end second entry cannot manufacture cancel
    evidence unless it genuinely falls inside member_since..cutoff. (The
    load-bearing multi-row proof — a second end that DOES fall in-window
    correctly classifying REMOVED — is the paired
    test_fallback_rewin_recent_end_within_membership_classifies below.)"""
    conn = init_db(tmp_path / "fb.db")
    # Sibling joined its group 5d ago; its own auction ended 1h ago.
    _seed(conn, "485000001", snipe_group=2,
          auction_end_at=_iso(timedelta(hours=-1)),
          added_at=_iso(timedelta(days=-5)))
    # End A: 30d ago — before member_since (lower bound excludes it).
    record_group_win(conn, "485000099", 2, _iso(timedelta(days=-30)))
    # End B: 1min ago — after the sibling's own end, so it cannot have
    # cancelled it (cutoff = end - margin; upper bound excludes it).
    record_group_win(conn, "485000099", 2, _iso(timedelta(minutes=-1)))
    conn.commit()
    # Both ends stored — the re-win was not collapsed to one row.
    assert conn.execute(
        "SELECT COUNT(*) FROM group_wins WHERE item_id='485000099'"
    ).fetchone()[0] == 2
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "485000001")
    conn.close()
    assert row["status"] == "WON"  # not REMOVED — no false cancel
    assert row["winning_bid"] == 10.0


def test_fallback_rewin_recent_end_within_membership_classifies(
        tmp_path, monkeypatch):
    """The value BUI-385 adds: with a stale recycled-group win (T-30d) AND a
    genuine recent re-win (T-2h) both recorded, a sibling whose membership
    spans the recent win is correctly classified REMOVED off it. The old
    (group, item) key kept only the first (stale) end and would have MISSED
    this — a WON-permissive evidence gap. The recent end is a genuine auction
    end, so this is sound cancel evidence, not a false REMOVED."""
    conn = init_db(tmp_path / "fb.db")
    # Sibling seeded a week ago (default), ending 1h ago.
    _seed(conn, "485000002", snipe_group=3,
          auction_end_at=_iso(timedelta(hours=-1)))
    # Stale end recorded FIRST (the order the old key kept), genuine re-win
    # second.
    record_group_win(conn, "485000098", 3, _iso(timedelta(days=-30)))
    record_group_win(conn, "485000098", 3, _iso(timedelta(hours=-2)))
    conn.commit()
    _run_fallback(monkeypatch, conn, price="$10.00")
    row = _status_row(conn, "485000002")
    conn.close()
    assert row["status"] == "REMOVED"


# ---------------------------------------------------------------------------
# BUI-382: every fallback write must be id-targeted, not item_id-wide. A
# re-listed/re-added item can carry a live PENDING row sharing an item_id
# with an old resolved/tombstoned row (the BUI-178 class of blast radius —
# see test_mark_bids_purged_spares_live_pending_sharing_item_id in
# test_server_db.py for the analogous mark_bids_purged fix).
# ---------------------------------------------------------------------------

def test_fallback_won_write_spares_live_pending_sharing_item_id(tmp_path, monkeypatch):
    """The WON-inference write and the title/end-date write both must target
    only the resolved row's id. Pre-fix, both were item_id-wide and would
    collateral-stamp a live re-added PENDING snipe sharing the item_id: the
    title/end write would corrupt its (not-yet-captured) auction_end_at with
    the OLD auction's end time, and update_bid_status's default WHERE
    (status NOT IN tombstones) would flip the live PENDING row itself to
    WON."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "491000001", status="ENDED",
          auction_end_at=_iso(timedelta(hours=-1)))
    old_id = conn.execute(
        "SELECT id FROM bids WHERE item_id='491000001'"
    ).fetchone()["id"]
    # Re-added live snipe for the same (re-listed) item_id. auction_end_at
    # is NULL — not yet captured — exactly the state an item_id-wide
    # COALESCE write would corrupt.
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group) "
        "VALUES ('491000001', 40.0, 'PENDING', 0)"
    )
    live_id = cur.lastrowid
    conn.commit()

    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", Path(_db_path_of(conn)))
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(
        m, "_fetch_ebay_item_sync",
        lambda iid: {"item_id": iid, "title": "Old Auction Title",
                     "current_price": "$10.00",
                     "end_date_iso": "2026-01-01T00:00:00.000Z"},
    )

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)

    async def run():
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        await m._run_ebay_fallback()

    asyncio.run(run())

    old = conn.execute("SELECT * FROM bids WHERE id=?", (old_id,)).fetchone()
    live = conn.execute("SELECT * FROM bids WHERE id=?", (live_id,)).fetchone()
    conn.close()

    assert old["status"] == "WON"
    assert old["winning_bid"] == 10.0
    assert old["ebay_title"] == "Old Auction Title"

    # The live re-added snipe must be completely untouched.
    assert live["status"] == "PENDING"
    assert live["winning_bid"] is None
    assert live["ebay_title"] is None
    assert live["auction_end_at"] is None


def test_fallback_ended_no_price_write_spares_live_pending_sharing_item_id(
        tmp_path, monkeypatch):
    """Same guard for the "no usable price" ENDED write: pre-fix, its
    item_id-wide update_bid_status call would also flip a live re-added
    PENDING sibling to ENDED."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "491000002", status="ENDED",
          auction_end_at=_iso(timedelta(hours=-1)))
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group) "
        "VALUES ('491000002', 40.0, 'PENDING', 0)"
    )
    live_id = cur.lastrowid
    conn.commit()

    _run_fallback(monkeypatch, conn, price=None)

    old = conn.execute(
        "SELECT status, winning_bid FROM bids WHERE item_id='491000002' "
        "AND status='ENDED'"
    ).fetchone()
    live = conn.execute("SELECT * FROM bids WHERE id=?", (live_id,)).fetchone()
    conn.close()

    assert old["status"] == "ENDED"
    assert old["winning_bid"] is None
    assert live["status"] == "PENDING"          # not clobbered to ENDED
    assert live["winning_bid"] is None


def test_fallback_purged_price_write_does_not_leak_to_sibling_tombstone(
        tmp_path, monkeypatch):
    """The purged-branch winning_bid write must target only the row being
    resolved. Multiple tombstoned rows can share an item_id (e.g. a
    re-listed item swept twice) — pre-fix, the item_id-wide UPDATE would
    stamp this price onto an unrelated sibling tombstone's already-recorded
    winning_bid too."""
    conn = init_db(tmp_path / "fb.db")
    recent = _iso(timedelta(hours=-1))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, resolved_at) "
        "VALUES ('491000003', 25.0, 'REMOVED', ?, ?)", (recent, recent),
    )
    target_id = conn.execute(
        "SELECT id FROM bids WHERE item_id='491000003' AND winning_bid IS NULL"
    ).fetchone()["id"]
    # An older, already-resolved tombstone sharing the same item_id — well
    # outside the 7-day window so it never enters _ebay_fallback_rows itself.
    old_resolved = _iso(timedelta(days=-30))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, "
        "resolved_at, winning_bid) VALUES "
        "('491000003', 10.0, 'REMOVED', ?, ?, 5.0)",
        (old_resolved, old_resolved),
    )
    sibling_id = conn.execute(
        "SELECT id FROM bids WHERE item_id='491000003' AND winning_bid=5.0"
    ).fetchone()["id"]
    conn.commit()

    _run_fallback(monkeypatch, conn, price="$15.00")

    target = conn.execute(
        "SELECT winning_bid FROM bids WHERE id=?", (target_id,)
    ).fetchone()
    sibling = conn.execute(
        "SELECT winning_bid FROM bids WHERE id=?", (sibling_id,)
    ).fetchone()
    conn.close()

    assert target["winning_bid"] == 15.0
    assert sibling["winning_bid"] == 5.0  # untouched


# ---------------------------------------------------------------------------
# BUI-382: ebay_no_price_at short-circuits the re-scan set once eBay has
# already given a definitive "no usable price" answer for a row.
# ---------------------------------------------------------------------------

def test_fallback_ended_no_price_stays_in_rescan_set(tmp_path, monkeypatch):
    """A non-purged ENDED row with no usable eBay price does NOT get
    ebay_no_price_at stamped, unlike the tombstone branches below — it stays
    eligible for a future WON inference. A single "no price" answer here
    can't be told apart from eBay's item data not having settled yet, and
    permanently excluding it would risk foreclosing a genuine win (forbidden
    by BUI-146). This is a deliberate scope decision (see the comment at the
    call site in _run_ebay_fallback): the unbounded re-scan cost for this
    branch is accepted risk, not fixed by BUI-382."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "491000004", status="ENDED",
          auction_end_at=_iso(timedelta(hours=-1)))
    _run_fallback(monkeypatch, conn, price=None)

    row = conn.execute(
        "SELECT status, winning_bid, ebay_no_price_at FROM bids "
        "WHERE item_id='491000004'"
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    remaining = {r["item_id"] for r in m._ebay_fallback_rows(conn, now)}
    conn.close()

    assert row["status"] == "ENDED"
    assert row["winning_bid"] is None
    assert row["ebay_no_price_at"] is None
    assert "491000004" in remaining


def test_fallback_purged_no_price_stamps_marker_and_exits_rescan_set(
        tmp_path, monkeypatch):
    """A tombstoned row with no usable eBay price gets ebay_no_price_at
    stamped and no longer matches the 7-day tombstone branch of
    _ebay_fallback_rows — pre-fix it would be re-fetched every sync until it
    aged out of the window."""
    conn = init_db(tmp_path / "fb.db")
    recent = _iso(timedelta(hours=-1))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, resolved_at) "
        "VALUES ('491000005', 25.0, 'REMOVED', ?, ?)", (recent, recent),
    )
    conn.commit()

    _run_fallback(monkeypatch, conn, price=None)

    row = conn.execute(
        "SELECT status, winning_bid, ebay_no_price_at FROM bids "
        "WHERE item_id='491000005'"
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    remaining = {r["item_id"] for r in m._ebay_fallback_rows(conn, now)}
    conn.close()

    assert row["status"] == "REMOVED"
    assert row["winning_bid"] is None
    assert row["ebay_no_price_at"] is not None
    assert "491000005" not in remaining


def test_fallback_cancelled_removed_no_price_stamps_marker(tmp_path, monkeypatch):
    """The BUI-371 cancelled-before-end REMOVED classification also produces
    a NULL-winning_bid tombstone when eBay has no usable price — it must get
    the same ebay_no_price_at treatment so it doesn't re-enter the 7-day
    re-scan set either (notes carries CANCELLED_TOMBSTONE_NOTE, not
    DEDUP_TOMBSTONE_NOTE, so the notes-based dedup-loser exclusion alone
    would not have caught it)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "491000006", status="PENDING",
          auction_end_at=_iso(timedelta(hours=-1)),
          gixen_vanished_at=_iso(timedelta(hours=-3)))
    _run_fallback(monkeypatch, conn, price=None)

    row = conn.execute(
        "SELECT status, winning_bid, ebay_no_price_at FROM bids "
        "WHERE item_id='491000006'"
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    remaining = {r["item_id"] for r in m._ebay_fallback_rows(conn, now)}
    conn.close()

    assert row["status"] == "REMOVED"
    assert row["winning_bid"] is None
    assert row["ebay_no_price_at"] is not None
    assert "491000006" not in remaining


def test_fallback_no_price_marker_prevents_second_ebay_fetch(tmp_path, monkeypatch):
    """End-to-end: once a tombstoned row is marked ebay_no_price_at, a
    subsequent _run_ebay_fallback call must not invoke the eBay fetch for it
    again."""
    conn = init_db(tmp_path / "fb.db")
    recent = _iso(timedelta(hours=-1))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, auction_end_at, resolved_at) "
        "VALUES ('491000007', 25.0, 'REMOVED', ?, ?)", (recent, recent),
    )
    conn.commit()

    fetch = MagicMock(return_value={
        "item_id": "491000007", "title": "T",
        "current_price": None, "end_date_iso": None,
    })
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", Path(_db_path_of(conn)))
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(m, "_fetch_ebay_item_sync", fetch)

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)

    async def run():
        # Fresh _write_lock per asyncio.run() call — a Lock created on one
        # loop can't be awaited from another (see _run_fallback's docstring).
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        await m._run_ebay_fallback()

    asyncio.run(run())
    assert fetch.call_count == 1

    asyncio.run(run())
    conn.close()
    assert fetch.call_count == 1  # no second fetch — row already excluded


def test_fallback_won_ledger_write_scoped_to_own_group_not_sibling(
        tmp_path, monkeypatch):
    """The id-targeted WON write (BUI-382) must not widen group_wins evidence
    capture: a live re-added sibling sharing the item_id but carrying a
    DIFFERENT snipe_group must not contribute its group to the ledger entry
    recorded for the resolved row's own group."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "491000008", status="ENDED", snipe_group=5,
          auction_end_at=_iso(timedelta(hours=-1)))
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status, snipe_group, auction_end_at) "
        "VALUES ('491000008', 40.0, 'PENDING', 7, ?)",
        (_iso(timedelta(days=1)),),
    )
    conn.commit()

    _run_fallback(monkeypatch, conn, price="$10.00")

    won = conn.execute(
        "SELECT status FROM bids WHERE item_id='491000008' AND status='WON'"
    ).fetchone()
    ledger = conn.execute(
        "SELECT snipe_group FROM group_wins WHERE item_id='491000008'"
    ).fetchall()
    live = conn.execute(
        "SELECT status FROM bids WHERE item_id='491000008' AND snipe_group=7"
    ).fetchone()
    conn.close()

    assert won is not None
    assert [row["snipe_group"] for row in ledger] == [5]
    assert live["status"] == "PENDING"  # sibling untouched


# ---------------------------------------------------------------------------
# BUI-399: _run_ebay_fallback must roll back the shared singleton connection
# on an unexpected exception, matching api_sync (BUI-386) and
# _ensure_fresh_sync/_sync_loop (BUI-391) — the loop above batches its DML
# into one end-of-cycle commit (BUI-382's db.commit()), so an unexpected
# mid-loop bug can leave stray uncommitted writes stranded on the connection
# for whatever the *next* successful cycle's commit happens to absorb.
# ---------------------------------------------------------------------------


def test_fallback_rolls_back_on_unexpected_exception(tmp_path, monkeypatch):
    """A genuine bug in the fallback's gather phase (not a Gixen/eBay
    connectivity issue — those are handled per-row inside gather, not via
    this generic except) must still roll back the shared `_db` connection.
    BUI-409: this function no longer writes to `_db` itself (see
    _run_ebay_fallback's except-block comment), so the rollback is no longer
    protecting a partial cycle of ITS OWN writes — it stays as the
    still-active BUI-386/391/399 systemwide convention (a defensive
    safety net for whatever else might be mid-write on the shared
    connection when an unrelated coroutine's bug fires) until Stage 3
    (BUI-410) retires it. This test guards that the call site itself wasn't
    dropped in the BUI-409 restructure."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "471099001", auction_end_at=_iso(timedelta(hours=-1)))

    class _RollbackSpy:
        """Thin proxy around a real connection: delegates everything except
        rollback() (counted here), matching the BUI-391 rollback-spy test
        pattern (test_server_api.py's test_background_entry_points_..._
        rollback_on_unexpected_exception). A real connection is needed
        underneath (unlike that test's bare MagicMock) because
        _ebay_fallback_rows runs a genuine SQL query before the injected
        failure fires."""

        def __init__(self, real):
            self._real = real
            self.rollbacks = 0

        def __getattr__(self, name):
            return getattr(self._real, name)

        def rollback(self):
            self.rollbacks += 1
            self._real.rollback()

    spy = _RollbackSpy(conn)
    monkeypatch.setattr(m, "_db", spy)
    monkeypatch.setattr(m, "_db_path", Path(_db_path_of(conn)))
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)

    def _boom(_iid):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "_fetch_ebay_item_sync", _boom)

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)

    async def run():
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        await m._run_ebay_fallback()  # must swallow (fire-and-forget), not raise

    asyncio.run(run())
    conn.close()
    assert spy.rollbacks == 1


# ---------------------------------------------------------------------------
# BUI-409 (Stage 2 of BUI-400's shared-connection isolation rollout):
# freeze-mid-cycle harness (design doc §6). Proves the gather-then-apply
# restructure actually removed the cross-await open transaction: while
# _run_ebay_fallback is paused mid-fetch (no DB write held — it hasn't
# reached its write_transaction() apply phase yet), a concurrent api_purge
# must be free to run to completion and commit, and _run_ebay_fallback's
# later apply phase must neither prematurely have committed anything during
# the pause nor get rolled back by (or itself roll back) api_purge's write.
# ---------------------------------------------------------------------------

def test_freeze_mid_cycle_purge_does_not_corrupt_fallback_apply(tmp_path, monkeypatch):
    """Gate the eBay fetch on an asyncio.Event the test owns, pause the
    fallback mid-gather, run api_purge concurrently, then release the gate
    and let the fallback finish. Asserts:

    1. api_purge completes (and tombstones the row) WHILE the fallback is
       still frozen in phase 1 — proving _write_lock is never held across
       the fetch await (Stage 1's api_purge write is not blocked by the
       paused fallback).
    2. At that same moment, the fallback's own writes (ebay_title,
       winning_bid) have NOT landed yet — proving phase 2 (apply) hasn't
       started, so nothing was prematurely committed mid-pause.
    3. After the gate is released and the fallback's apply phase runs, the
       purge's REMOVED tombstone survives untouched — update_bid_status's
       own tombstone WHERE-guard (`status NOT IN (...)`) makes the
       fallback's now-stale WON/LOST write for this row a no-op, so neither
       writer corrupts or rolls back the other's committed work.

    The eBay fetch runs inside asyncio.to_thread (a real worker thread), so
    the Event/gate handshake crosses threads via call_soon_threadsafe /
    run_coroutine_threadsafe rather than touching the asyncio primitives
    directly from the worker thread.
    """
    conn = init_db(tmp_path / "freeze.db")
    _seed(conn, "409000001", status="ENDED",
          auction_end_at=_iso(timedelta(hours=-1)))
    conn.commit()
    path = Path(_db_path_of(conn))

    mock_client = MagicMock()
    mock_client.list_snipes.return_value = []
    mock_client.purge_completed.return_value = None
    mock_client.remove_snipe.return_value = True

    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", path)
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(m, "_api_client", mock_client)

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)

    async def run():
        loop = asyncio.get_running_loop()
        # Fresh, loop-bound locks/state — same convention as _run_fallback.
        monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        monkeypatch.setattr(m, "_api_lock", asyncio.Lock())

        fetch_gate = asyncio.Event()
        fetch_entered = asyncio.Event()

        def _gated_fetch(iid):
            # Runs in a to_thread worker thread — signal + wait via
            # threadsafe primitives, never touch the Event directly here.
            loop.call_soon_threadsafe(fetch_entered.set)
            asyncio.run_coroutine_threadsafe(fetch_gate.wait(), loop).result()
            return {"item_id": iid, "title": "T", "current_price": "$10.00",
                    "end_date_iso": None}

        monkeypatch.setattr(m, "_fetch_ebay_item_sync", _gated_fetch)

        fallback_task = asyncio.create_task(m._run_ebay_fallback())
        await asyncio.wait_for(fetch_entered.wait(), timeout=5.0)
        # The fallback is now frozen in phase 1 (gather), blocked inside the
        # fetch, with zero DB writes held — the property BUI-409 exists to
        # guarantee.

        # Timeout, not a bare await: if _run_ebay_fallback ever regressed to
        # hold _write_lock across the fetch (exactly the bug this test
        # exists to catch), api_purge's own _write_locked() acquisition
        # would block forever on a lock the frozen fallback task can't
        # release until fetch_gate.set() below — which is unreachable past
        # a hang here. Fail fast instead of hanging the whole suite.
        purge_result = await asyncio.wait_for(
            m.api_purge(m.PurgeRequest(sibling_ids=[])), timeout=5.0
        )
        assert purge_result["purged_completed"] == 1

        mid_pause = conn.execute(
            "SELECT status, winning_bid, ebay_title FROM bids "
            "WHERE item_id='409000001'"
        ).fetchone()
        # (1) api_purge ran to completion and committed while the fallback
        # was still paused — not blocked by a lock the fallback would have
        # held across the fetch await under the old (pre-BUI-409) shape.
        assert mid_pause["status"] == "REMOVED"
        # (2) The fallback's own apply-phase writes have not landed — phase
        # 2 hasn't started yet, so nothing was prematurely committed.
        assert mid_pause["winning_bid"] is None
        assert mid_pause["ebay_title"] is None

        fetch_gate.set()  # release the gate — gather completes, apply runs
        await asyncio.wait_for(fallback_task, timeout=5.0)

    asyncio.run(run())

    row = conn.execute(
        "SELECT status, winning_bid, ebay_title FROM bids "
        "WHERE item_id='409000001'"
    ).fetchone()
    conn.close()
    # (3) The purge's REMOVED tombstone survives: the fallback's apply
    # phase used its stale (pre-purge) snapshot (is_purged=False, since the
    # row was still ENDED when phase 1 read it) and so attempted the
    # WON/LOST-inference update_bid_status() call — but that call's own
    # `status NOT IN (tombstones)` WHERE guard makes it a no-op against the
    # now-REMOVED row. Neither writer rolled back or clobbered the other's
    # committed work.
    assert row["status"] == "REMOVED"
    # The unconditional title/end-date write (BUI-382: runs regardless of
    # status) DOES still land — it's harmless and orthogonal to status.
    assert row["ebay_title"] == "T"


# ---------------------------------------------------------------------------
# BUI-409 review follow-up: regression coverage for the two new code paths
# the gather-then-apply restructure introduces — a mixed-outcome batch
# (one row's fetch fails, a sibling's succeeds, in the SAME cycle), and an
# unexpected exception raised inside phase 2 (apply) rather than phase 1
# (gather, already covered by test_fallback_rolls_back_on_unexpected_
# exception above).
# ---------------------------------------------------------------------------


def test_fallback_mixed_fetch_outcomes_only_skips_failing_row(tmp_path, monkeypatch):
    """Two rows eligible in the SAME cycle: one whose eBay fetch fails, one
    that succeeds. The failing row must get NO write at all (still exactly
    as seeded) while the sibling row — fetched successfully in the same
    gather phase — still gets applied in phase 2. Proves the `fetched`
    dict's per-row failure isolation actually works across a real multi-row
    batch, not just a single-row cycle (every other existing test in this
    file seeds at most one row that _ebay_fallback_rows actually matches)."""
    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "409100001", auction_end_at=_iso(timedelta(hours=-1)))
    _seed(conn, "409100002", auction_end_at=_iso(timedelta(hours=-1)))
    conn.commit()

    def _fetch(iid):
        if iid == "409100001":
            return None  # simulated fetch failure for this row only
        return {"item_id": iid, "title": "T", "current_price": "$10.00",
                "end_date_iso": None}

    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", Path(_db_path_of(conn)))
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(m, "_fetch_ebay_item_sync", _fetch)

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)

    async def run():
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        await m._run_ebay_fallback()

    asyncio.run(run())

    failed_row = _status_row(conn, "409100001")
    ok_row = _status_row(conn, "409100002")
    conn.close()

    # The failing row's fetch never returned data — untouched, still
    # exactly as seeded.
    assert failed_row["status"] == "ENDED"
    assert failed_row["winning_bid"] is None
    # The sibling, fetched successfully in the SAME cycle, still gets
    # applied — one row's fetch failure doesn't abort the batch.
    assert ok_row["status"] == "WON"
    assert ok_row["winning_bid"] == 10.0


def test_fallback_apply_phase_exception_rolls_back_whole_batch(tmp_path, monkeypatch):
    """An unexpected exception raised INSIDE phase 2 (apply) — as opposed to
    test_fallback_rolls_back_on_unexpected_exception's phase-1 (gather)
    trigger via a raising _fetch_ebay_item_sync — must roll back
    write_transaction()'s entire batch, including any row that already
    applied successfully earlier in the SAME transaction. This matches the
    old code's single-end-of-cycle-commit behavior exactly: a mid-loop bug
    there also discarded the whole cycle's writes, not just the offending
    row's. Forces the exception via update_bid_status (as imported into
    server.fallback) rather than the eBay fetch, so it fires only after
    gather has already succeeded for both rows and phase 2 is underway."""
    import server.fallback as fb

    conn = init_db(tmp_path / "fb.db")
    _seed(conn, "409200001", auction_end_at=_iso(timedelta(hours=-1)))
    _seed(conn, "409200002", auction_end_at=_iso(timedelta(hours=-1)))
    conn.commit()

    real_update_bid_status = fb.update_bid_status

    def _boom_for_002(conn_arg, item_id, *a, **kw):
        if item_id == "409200002":
            raise RuntimeError("apply-phase boom")
        return real_update_bid_status(conn_arg, item_id, *a, **kw)

    monkeypatch.setattr(fb, "update_bid_status", _boom_for_002)
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", Path(_db_path_of(conn)))
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(
        m, "_fetch_ebay_item_sync",
        lambda iid: {"item_id": iid, "title": "T", "current_price": "$10.00",
                     "end_date_iso": None},
    )

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)

    async def run():
        monkeypatch.setattr(m, "_write_lock", asyncio.Lock())
        await m._run_ebay_fallback()  # must swallow (fire-and-forget), not raise

    asyncio.run(run())

    row1 = _status_row(conn, "409200001")
    row2 = _status_row(conn, "409200002")
    conn.close()

    # Whichever row processed first (plain SELECT, no ORDER BY — order
    # isn't guaranteed), NEITHER row's write survives: write_transaction()'s
    # rollback discards the whole apply transaction, not just the row that
    # raised.
    assert row1["status"] == "ENDED"
    assert row1["winning_bid"] is None
    assert row2["status"] == "ENDED"
    assert row2["winning_bid"] is None


def test_fallback_same_cycle_group_win_classifies_sibling_in_one_call(
        tmp_path, monkeypatch):
    """The core same-connection read-after-write claim in
    _run_ebay_fallback's docstring, proven with BOTH rows resolved by a
    SINGLE _run_ebay_fallback() call (every other group-evidence test in
    this file pre-seeds the winning row's group_wins ledger entry via a
    SEPARATE record_group_win() call before invoking the fallback — this
    test instead lets the fallback itself classify the winner as WON mid-
    cycle, via update_bid_status's own group_wins insert, and checks a
    LATER row in the SAME batch sees that evidence). Winner ends well
    before the sibling (respecting the _group_won_before margin/lifetime
    bounds), so the sibling — a snipe in the same group whose own auction
    ended after the winner's — resolves REMOVED (cancelled before end) off
    evidence recorded during THIS SAME cycle, not a pre-existing row."""
    conn = init_db(tmp_path / "fb.db")
    # Winner: ended 3 hours ago, well past the winner's own event; added a
    # week ago (default) so member_since predates its win.
    _seed(conn, "409300001", snipe_group=9,
          auction_end_at=_iso(timedelta(hours=-3)))
    # Sibling: same group, added a week ago too, its OWN auction ending
    # 1 hour ago — after the winner's end (3h ago) plus the 10-minute
    # margin, so the winner's evidence covers it.
    _seed(conn, "409300002", snipe_group=9,
          auction_end_at=_iso(timedelta(hours=-1)))
    conn.commit()

    # Winner's price ($10, below its $25 max) infers WON; the sibling's own
    # fetch price is irrelevant once cancel evidence classifies it REMOVED
    # first (the cancelled-before-end check runs before the WON/LOST
    # inference branch).
    _run_fallback(monkeypatch, conn, price="$10.00")

    winner = _status_row(conn, "409300001")
    sibling = _status_row(conn, "409300002")
    ledger = conn.execute(
        "SELECT * FROM group_wins WHERE item_id='409300001'"
    ).fetchone()
    conn.close()

    assert winner["status"] == "WON"
    assert ledger is not None, (
        "the winner's WON transition (inferred mid-cycle by this SAME "
        "_run_ebay_fallback() call) must have recorded group-win evidence"
    )
    assert sibling["status"] == "REMOVED", (
        "the sibling, resolved in the SAME batch/cycle, must see the "
        "winner's group_wins evidence even though it was written by an "
        "EARLIER iteration of this same apply-phase loop, not a prior call"
    )
