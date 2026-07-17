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
    asyncio.run(m._run_ebay_fallback())

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
    monkeypatch.setattr(m, "_ebay_fallback_lock", asyncio.Lock())
    monkeypatch.setattr(m, "_ebay_cooldown_until", 0.0)
    monkeypatch.setattr(m, "_fetch_ebay_item_sync", fetch)

    async def _nosleep(_):
        return None

    monkeypatch.setattr(m.asyncio, "sleep", _nosleep)
    asyncio.run(m._run_ebay_fallback())
    assert fetch.call_count == 1

    asyncio.run(m._run_ebay_fallback())
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
