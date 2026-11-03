"""Confirm Gixen wins against eBay's own bidding record (BUI-1346).

Gixen marks a snipe WON when its max bid equals the final price, but eBay
awards an exact tie to the EARLIER bidder, so a tie is a phantom win (two
known: items 188733995492 and 800412993298). This module asks eBay, through
the apps/ebay `ebay-bid-status` console script, about every recently ended
WON row and reverses WON -> LOST only on a positive OUTBID.

Evidence only. This is a downstream check on rows that are already WON; it
never touches the eBay WON inference in fallback._run_ebay_fallback
(BUI-146/BUI-371/BUI-381 forbid gating it). The rules:

- OUTBID from eBay -> status LOST, ebay_bid_status='OUTBID', notes gets
  WIN_REVERSED_NOTE appended. update_bid_status refuses any later WON write
  onto that row, so Gixen's still-WON listing cannot undo it next sync.
- WINNING, or a null status (eBay saw no bid of ours) -> status unchanged;
  only the checked marker is stamped so the row is never re-queried.
- A failed lookup (missing script, token or scope error, timeout, bad JSON)
  or an unresolved item -> nothing written at all; the row is retried on a
  later run until it ages out of the ~90-day window eBay keeps the data.

The group_wins ledger row a reversed win recorded is deliberately kept: Gixen
believed it won and cancelled the group siblings on that belief, so those
siblings never bid and their REMOVED classification stays correct (see the
BUI-385/BUI-1346 note in server/db.py).

Writes are id-targeted and status-guarded (`WHERE id=? AND status='WON'`)
under the app-wide write lock, after a fresh re-read, the same TOCTOU shape
as _run_ebay_fallback (BUI-417). bids.auction_end_at is only read, never
written: it arms the sniper.

Import direction: `import server.main as main` (module reference), as in
server/fallback.py, so tests can monkeypatch main's helpers.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

from server.db import (
    EBAY_BID_STATUS_OUTBID, WIN_REVERSED_NOTE, get_bid_by_id, write_transaction,
)
from server.fallback import _parse_iso_utc

import server.main as main

# eBay keeps itemBiddingActivity data about 90 days after the end; stay inside.
WINDOW = timedelta(days=89)
# Skip auctions that ended moments ago so eBay's record has settled.
SETTLE = timedelta(minutes=30)
# At most one eBay lookup per interval, and a bounded batch per lookup.
INTERVAL_S = 1800.0
BATCH = 40
LOOKUP_TIMEOUT_S = 180
_EBAY_STATUSES = ("WINNING", EBAY_BID_STATUS_OUTBID)

_last_lookup_at = 0.0
_lookup_failing = False


def candidate_rows(db: sqlite3.Connection, now_dt: datetime) -> list[sqlite3.Row]:
    """WON rows not yet checked whose auction ended within the window and at
    least SETTLE ago, newest first, capped at BATCH. A row with no parseable
    end (auction_end_at, else resolved_at) cannot be bounded and is skipped."""
    rows = db.execute(
        "SELECT id, item_id, auction_end_at, resolved_at FROM bids "
        "WHERE status='WON' AND ebay_bid_checked_at IS NULL ORDER BY id DESC"
    ).fetchall()
    out = []
    for row in rows:
        end_dt = _parse_iso_utc(row["auction_end_at"]) or _parse_iso_utc(row["resolved_at"])
        if end_dt is None:
            continue
        if now_dt - WINDOW <= end_dt <= now_dt - SETTLE:
            out.append(row)
        if len(out) >= BATCH:
            break
    return out


def _note_failure(reason: str) -> None:
    """Log a lookup failure once per failing streak, not every run."""
    global _lookup_failing
    if not _lookup_failing:
        main.logger.warning(
            "win_confirm: eBay bid-status lookup failed (%s); no win changed, "
            "retrying on later syncs (logged once until it recovers)", reason,
        )
    _lookup_failing = True


def lookup_sync(item_ids: list[str]) -> dict[str, dict] | None:
    """Run `ebay-bid-status ... --json`. Return {item_id: result} or None on
    any failure. Never raises."""
    global _lookup_failing
    bin_path = main._ebay_bid_status_bin()
    if bin_path is None:
        _note_failure("ebay-bid-status console script not found; set EBAY_BID_STATUS_BIN")
        return None
    try:
        proc = subprocess.run(
            [bin_path, *item_ids, "--json"],
            capture_output=True, text=True, timeout=LOOKUP_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        _note_failure(f"{type(e).__name__}")
        return None
    if proc.returncode != 0:
        _note_failure(f"exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _note_failure("unparseable JSON")
        return None
    if not isinstance(payload, list):
        _note_failure("JSON is not a list")
        return None
    if _lookup_failing:
        main.logger.info("win_confirm: eBay bid-status lookup recovered")
    _lookup_failing = False
    return {
        str(r["item_id"]): r for r in payload
        if isinstance(r, dict) and r.get("item_id") is not None
    }


def apply_results(
    wconn: sqlite3.Connection, rows: list[sqlite3.Row],
    results: dict[str, dict], now_iso: str,
) -> int:
    """Apply eBay's answers to `rows` on `wconn`. Returns the number of wins
    reversed. Caller holds the write lock and commits."""
    reversed_count = 0
    for row in rows:
        res = results.get(str(row["item_id"]))
        if not res or res.get("resolved") is not True:
            continue  # no definitive answer: change nothing, retry later
        status = res.get("status")
        if status is not None and status not in _EBAY_STATUSES:
            continue  # unknown vocabulary is not evidence
        fresh = get_bid_by_id(wconn, row["id"])
        if fresh is None or fresh["status"] != "WON" or fresh["ebay_bid_checked_at"]:
            continue  # changed since gather (BUI-417 TOCTOU shape)
        if status == EBAY_BID_STATUS_OUTBID:
            price = res.get("final_price")
            usd_price = (
                float(price) if isinstance(price, (int, float)) and price > 0
                and res.get("currency") in (None, "USD") else None
            )
            wconn.execute(
                "UPDATE bids SET status='LOST', "
                "winning_bid=COALESCE(?, winning_bid), "
                "ebay_bid_status=?, ebay_bid_checked_at=?, "
                "notes=CASE WHEN notes IS NULL OR notes='' THEN ? "
                "ELSE notes || '; ' || ? END "
                "WHERE id=? AND status='WON'",
                (usd_price, EBAY_BID_STATUS_OUTBID, now_iso,
                 WIN_REVERSED_NOTE, WIN_REVERSED_NOTE, row["id"]),
            )
            reversed_count += 1
            main.logger.warning(
                "win_confirm: %s (row %s) eBay reports OUTBID; Gixen WON "
                "reversed to LOST (BUI-1346)", row["item_id"], row["id"],
            )
        else:
            wconn.execute(
                "UPDATE bids SET ebay_bid_status=?, ebay_bid_checked_at=? "
                "WHERE id=? AND status='WON'",
                (status, now_iso, row["id"]),
            )
    return reversed_count


async def run_win_confirmation(*, force: bool = False) -> int:
    """One throttled confirmation pass. Returns the number of wins reversed.
    Never raises: a background-task failure must not touch the sync loop."""
    global _last_lookup_at
    try:
        now_dt = datetime.now(timezone.utc)
        if not force and now_dt.timestamp() - _last_lookup_at < INTERVAL_S:
            return 0
        rows = candidate_rows(main._get_db(), now_dt)
        if not rows:
            return 0
        _last_lookup_at = now_dt.timestamp()
        item_ids = list(dict.fromkeys(str(r["item_id"]) for r in rows))
        results = await asyncio.to_thread(lookup_sync, item_ids)
        if not results:
            return 0
        async with main._write_locked():
            with write_transaction(main._get_db_path()) as wconn:
                return apply_results(wconn, rows, results, now_dt.isoformat())
    except Exception:  # noqa: BLE001  # fire-and-forget task-level safety net, same shape as _run_ebay_fallback's
        main.logger.exception("win_confirm: unexpected error; no win changed")
        return 0
