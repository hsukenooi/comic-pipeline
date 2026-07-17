"""Gixen backend server — FastAPI app with SQLite storage and Gixen proxy."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from gixen_client import (
    GixenClient, GixenError, GixenConnectionError, GixenSnipeNotFoundError,
    GixenAddNotConfirmedError, GixenModifyNotConfirmedError,
    find_sibling_cleanup_targets,
)
from gixen.plugins import (
    load_plugins,
    _invoke_db_tables_isolated,
    _invoke_register_routes,
    _collect_dashboard_tabs,
)
from server.db import (
    DB_PATH, init_db, insert_bid, update_bid_grades, get_bid_by_item_id,
    get_pending_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    mark_bids_purged, cache_gixen_data, DEDUP_TOMBSTONE_NOTE,
    CANCELLED_TOMBSTONE_NOTE,
    set_auction_end_time, get_bids_ready_to_snipe, set_local_snipe_result,
    refresh_snipe_group, record_group_win,
    TOMBSTONE_STATUSES_SQL,
)
import ebay_bidder

# The eBay Browse-API fallback (winning-bid capture for ENDED auctions) shells
# out to the `ebay-fetch` console script from apps/ebay rather than importing
# ebay_fetch as a module (BUI-66). apps/* are NOT uv workspace members, so the
# module's transitive deps aren't in the server venv — a subprocess against the
# installed console script sidesteps that, and inherits the server's eBay
# credentials from the environment.
def _ebay_fetch_bin() -> str | None:
    """Resolve the `ebay-fetch` console script to an invocable path, or None.

    Resolved at CALL time, never at import time: EBAY_FETCH_BIN comes from the
    server .env (loaded in the lifespan, *after* this module is imported), and
    the LaunchAgent's PATH may not include ~/.local/bin where uv installs the
    script — so an import-time shutil.which would spuriously report it missing.
    A value containing a path separator is used verbatim if executable; a bare
    name is looked up on PATH.
    """
    name = os.getenv("EBAY_FETCH_BIN", "ebay-fetch")
    if os.sep in name or (os.altsep and os.altsep in name):
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name)


logger = logging.getLogger(__name__)

# The host configures the plugin subsystem's logger explicitly so the audit
# trail emitted by load_plugins() (plugin discovery, registration, validation
# errors) is visible at INFO. Uvicorn does not configure the root logger by
# default, so propagation alone wouldn't show these messages — attach a
# stream handler with a uvicorn-style prefix so the lines blend into the
# normal startup log.
_plugin_logger = logging.getLogger("gixen.plugins")
_plugin_logger.setLevel(logging.INFO)
if not _plugin_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     gixen.plugins: %(message)s"))
    _plugin_logger.addHandler(_h)
# Note: propagate stays True so pytest's caplog (which attaches to root) can
# capture these records in tests. Uvicorn's default config attaches no root
# handler, so propagation does not cause double-logging in production.

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

_db: sqlite3.Connection | None = None
_api_client: GixenClient | None = None
_api_lock: asyncio.Lock | None = None
_sync_lock: asyncio.Lock | None = None
_last_sync_at: float = 0.0
_SYNC_TTL = 5.0  # concurrent dashboard loads within this window share one Gixen pull
_ebay_fallback_lock: asyncio.Lock | None = None
_ebay_cooldown_until: float = 0.0
_EBAY_COOLDOWN = 300.0  # seconds; suppress eBay fallback after a rate-limit storm
# BUI-85: cap eBay lookups for vanished PENDING rows with no captured end time,
# so a backlog of them can't flood the rate-limited eBay budget in one sync.
_VANISHED_NULL_END_MAX_PER_SYNC = 5
# BUI-381: same discipline for row-less listed-winner evidence lookups — a
# post-outage catch-up sync with several unrecorded group winners must not
# serialize unbounded 30s-timeout eBay subprocess calls inside the sync
# (which api callers hold _api_lock across). Unrecorded winners retry on
# later syncs; they stay on Gixen's list until purged.
_LISTED_WIN_FETCH_MAX_PER_SYNC = 5
# Tracked so the lifespan teardown can cancel + await any in-flight fallback
# task before _db.close() runs. Without this the task can hit a closed DB.
_ebay_fallback_task: asyncio.Task | None = None
# Local-eBay bidder (per-snipe direct-HTTP bid placement). Initialized in
# lifespan; used by the Gixen-side state machine to fire the timed bid.
_bidder: "ebay_bidder.EbayBidder | None" = None

# Separate Gixen client for the background sync loop, so its long-running scrapes
# don't contend on _api_lock with request-handler writes.
_sync_client: GixenClient | None = None
SYNC_INTERVAL = int(os.getenv("GIXEN_SYNC_INTERVAL", "600"))  # 10 min default
_SYNC_BACKOFF_MAX = 3600  # cap exponential backoff at 1 hour


def _get_db() -> sqlite3.Connection:
    assert _db is not None, "DB not initialized"
    return _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERMINAL_GIXEN_STATUSES: frozenset[str] = frozenset({"WON", "LOST", "FAILED", "ENDED"})

# Gixen reports many ended-auction states the original 4-status set misses.
# Map every Gixen status we've observed in production to our internal terminal
# set {WON, LOST, FAILED, ENDED}. Keys are normalized (upper-case, stripped).
#
# OUTBID and BID UNDER ASKING PRICE are both losses: in OUTBID Gixen placed
# our bid but eBay's proxy revealed a higher standing max; in BID UNDER ASKING
# PRICE the current price already exceeded our max at snipe time so Gixen
# skipped the submission. Different mechanics, same outcome — we lost, and
# current_bid is the price that beat us.
_GIXEN_TERMINAL_MAP: dict[str, str] = {
    "WON": "WON",
    "LOST": "LOST",
    "OUTBID": "LOST",
    "BID UNDER ASKING PRICE": "LOST",
    "FAILED": "FAILED",
    "ENDED": "ENDED",
}

# Gixen statuses that are positive evidence Gixen actually processed our bid:
# OUTBID means our bid was placed and beaten; BID UNDER ASKING PRICE means
# Gixen evaluated the snipe at fire time. A snipe carrying one of these was
# not group-cancelled, so its LOST is a genuine contested loss and is exempt
# from the BUI-371 group-cancel reclassification (the calibration report
# depends on real losses staying LOST).
_BID_PROCESSED_STATUSES: frozenset[str] = frozenset({"OUTBID", "BID UNDER ASKING PRICE"})


def _map_terminal_status(gixen_status: str, time_to_end: str) -> str | None:
    """Map a Gixen snipe to our internal terminal status when its auction is done.

    `time_to_end == 'ENDED'` is Gixen's authoritative signal that the auction
    is over. If Gixen reports a recognized terminal status, return its mapped
    value. If only `time_to_end` says ENDED (status string we don't recognize),
    fall back to ENDED — the eBay fallback path can later refine it to WON/LOST.
    Returns None for active snipes (no transition needed).
    """
    mapped = _GIXEN_TERMINAL_MAP.get(gixen_status.upper().strip())
    if mapped:
        return mapped
    if time_to_end.upper().strip() == "ENDED":
        return "ENDED"
    return None

# ebay_fetch.load_config calls sys.exit(1) on missing credentials. Detect that
# eagerly with explicit env-var checks so a misconfiguration shows up as a
# clean log line rather than getting laundered into a fake "fetch failed".
# Once we've logged the problem once we suppress the spam — credentials don't
# get fixed by this process.
_EBAY_CREDS_OK: bool | None = None  # tri-state: None=unchecked, True=ok, False=missing


def _ebay_creds_available() -> bool:
    global _EBAY_CREDS_OK
    if _EBAY_CREDS_OK is not None:
        return _EBAY_CREDS_OK
    has_creds = bool(os.getenv("EBAY_CLIENT_ID")) and bool(os.getenv("EBAY_CLIENT_SECRET"))
    if not has_creds:
        logger.warning(
            "_fetch_ebay_item_sync: EBAY_CLIENT_ID and/or EBAY_CLIENT_SECRET not set; "
            "skipping eBay fallback (silently from here on)"
        )
    _EBAY_CREDS_OK = has_creds
    return has_creds


def _fetch_ebay_item_sync(item_id: str) -> dict | None:
    bin_path = _ebay_fetch_bin()
    if bin_path is None:
        return None
    if not _ebay_creds_available():
        return None
    try:
        proc = subprocess.run(
            [bin_path, item_id, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            logger.warning(
                "_fetch_ebay_item_sync %s: ebay-fetch exited %d: %s",
                item_id, proc.returncode, (proc.stderr or "").strip()[:200],
            )
            return None
        results = json.loads(proc.stdout)
        if results:
            return results[0]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logger.warning("_fetch_ebay_item_sync %s: %s", item_id, e)
    return None


def _parse_end_iso(end_iso: str | None) -> datetime | None:
    """Parse an eBay itemEndDate ('2025-05-01T12:34:56.000Z') into an aware
    datetime, or None if unparseable."""
    if not end_iso:
        return None
    try:
        return datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def iso_to_relative(end_date_iso: str | None) -> str:
    if not end_date_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        diff = dt - datetime.now(timezone.utc)
        total_seconds = diff.total_seconds()
        if total_seconds <= 0:
            return "ENDED"
        days = int(total_seconds // 86400)
        hours = int((total_seconds % 86400) // 3600)
        minutes = int((total_seconds % 3600) // 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        return " ".join(parts) if parts else "<1m"
    except (ValueError, TypeError):
        return "—"


# ---------------------------------------------------------------------------
# Cancelled-before-end classification (BUI-371)
# ---------------------------------------------------------------------------
# A snipe cancelled while its auction was still live (user removal on Gixen's
# web UI, or Gixen's bid-group auto-cancel after a sibling won) never places a
# bid — so it must resolve to the REMOVED tombstone, never ENDED/LOST, or the
# eBay price fallback can stamp a phantom WON on it (BUI-146). Per the BUI-146
# decision the WON inference itself is never gated; instead these helpers
# supply POSITIVE evidence of a pre-end cancellation, checked wherever a
# PENDING row is about to take a terminal status. Anything ambiguous falls
# through to today's WON-permissive behavior.
#
# Margin rationale: a snipe Gixen actually executes stays on Gixen's list until
# its auction ends, so it can only be observed vanished *after* the end; and
# Gixen cancels a group's remaining bids promptly after a win (its FAQ's
# dual-win caveat is auctions ending within ~2 minutes of each other). The
# margin only needs to absorb auction_end_at estimation error (computed from
# Gixen's minute-granular countdown) plus clock skew — 10 minutes is
# comfortably past both, while real cancel-to-end gaps are hours or days.
_CANCEL_EVIDENCE_MARGIN = timedelta(minutes=10)


def _parse_iso_utc(value: str | None) -> datetime | None:
    """_parse_end_iso, but tolerating SQLite's naive 'YYYY-MM-DD HH:MM:SS'
    (the bids.added_at column default) by assuming UTC — every timestamp this
    server writes is UTC. Needed because comparing a naive datetime against
    the aware ones _parse_end_iso returns raises TypeError."""
    dt = _parse_end_iso(value)
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_snipe_group(value: str | int | None) -> int | None:
    """Parse a Gixen-reported snipe_group to an int, or None when the value
    is absent, blank, or unparseable (a scrape quirk like 'N/A'). Callers
    must treat None as 'unknown' — never coerce it to 0, because group 0 is
    a positive claim ('no group') that clears membership / suppresses
    evidence. Caveat: the scraper itself encodes a regex miss as '0'
    (gixen_client._parse_snipe_table), indistinguishable here from a genuine
    no-group — always in the WON-permissive direction (evidence weakened,
    never fabricated); fixing the encoding is a client-side follow-up."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _vanished_while_live(vanished_at_iso: str | None, end_dt: datetime | None) -> bool:
    """True when the snipe was observed missing from a healthy Gixen list at
    least _CANCEL_EVIDENCE_MARGIN before its auction end — it was cancelled
    while live, not executed at end."""
    vanished_dt = _parse_end_iso(vanished_at_iso)
    if vanished_dt is None or end_dt is None:
        return False
    return vanished_dt <= end_dt - _CANCEL_EVIDENCE_MARGIN


def _group_won_before(
    db: sqlite3.Connection, item_id: str, snipe_group: str | int | None,
    end_dt: datetime | None, added_at_iso: str | None,
) -> bool:
    """True when another snipe in the same non-zero bid group WON an auction
    that ended during this row's lifetime (at or after its added_at) and at
    least _CANCEL_EVIDENCE_MARGIN before this row's end — Gixen had cancelled
    this snipe by then, so no bid was ever placed on it.

    The lifetime lower bound is what makes group-number reuse safe: Gixen
    groups are small integers (1-10) that get recycled across unrelated
    campaigns, and a WON row keeps its group number forever. Without the
    bound, a months-old win in a reused group would count as cancel evidence
    for a brand-new unrelated snipe — worst case suppressing a real win the
    eBay fallback would otherwise recover. A win that predates this snipe's
    creation cannot have group-cancelled it.

    DB-side sibling of gixen_client.find_sibling_cleanup_targets (which finds
    the same won-group siblings on the *live* Gixen list for purge): this one
    works from stored bids rows and adds the timing bounds, because here the
    question is retrospective — was this snipe already cancelled by its own
    auction's end?

    Consults two sources under identical bounds (BUI-381): live WON bids rows,
    and the durable group_wins ledger — which survives the winner row's
    destruction (mark_bids_purged sweeps WON → REMOVED) and covers winners
    that never got a bids row (first seen already-terminal via the web-add
    path). The live-row arm looks redundant now that every WON writer also
    records to the ledger, but it is deliberate skew tolerance (the
    TOMBSTONE_STATUSES_SQL convention): a WON written by an older installed
    gixen-cli lacks a ledger entry until the next server restart backfills
    it, and evidence must not silently weaken in that window."""
    if end_dt is None:
        return False
    group = _parse_snipe_group(snipe_group)
    if not group:
        return False  # no group, or unparseable → no evidence (WON-permissive)
    added_dt = _parse_iso_utc(added_at_iso)
    if added_dt is None:
        return False  # can't scope to a lifetime → no evidence (WON-permissive)
    cutoff = end_dt - _CANCEL_EVIDENCE_MARGIN
    rows = db.execute(
        "SELECT COALESCE(auction_end_at, resolved_at) AS won_end_at FROM bids "
        "WHERE snipe_group = ? AND status = 'WON' AND item_id != ? "
        "UNION ALL "
        "SELECT won_end_at FROM group_wins "
        "WHERE snipe_group = ? AND item_id != ?",
        (group, item_id, group, item_id),
    ).fetchall()
    for row in rows:
        won_end = _parse_iso_utc(row["won_end_at"])
        if won_end is not None and added_dt <= won_end <= cutoff:
            return True
    return False


def _cancelled_before_end(
    db: sqlite3.Connection, item_id: str, row: sqlite3.Row,
    end_dt: datetime | None,
) -> bool:
    """Combined cancel-evidence test used by the vanished-ended resolver and
    the eBay fallback. `row` must carry gixen_vanished_at, snipe_group,
    local_snipe_result, and added_at.

    An 'OK:' local_snipe_result is first-party proof our local sniper fired a
    bid on this auction — whatever the vanish/group signals suggest, we DID
    bid, so 'cancelled, never bid' cannot apply."""
    if (row["local_snipe_result"] or "").startswith("OK:"):
        return False
    return _vanished_while_live(row["gixen_vanished_at"], end_dt) or _group_won_before(
        db, item_id, row["snipe_group"], end_dt, row["added_at"]
    )


def _mark_cancelled_tombstone(db: sqlite3.Connection, row_id: int) -> None:
    """Stamp the BUI-371 marker on a freshly-classified tombstone so it can be
    told apart from user-cancel / completed-sweep tombstones in a later audit
    (the BUI-67 DEDUP_TOMBSTONE_NOTE convention). COALESCE keeps any
    pre-existing note. Caller commits."""
    db.execute(
        "UPDATE bids SET notes=COALESCE(notes, ?) WHERE id=?",
        (CANCELLED_TOMBSTONE_NOTE, row_id),
    )


def _record_vanish_observations(
    db: sqlite3.Connection, gixen_item_ids: set[str], now: str,
    scrape_started_at: str,
) -> None:
    """Track when PENDING rows vanish from Gixen's list (BUI-371).

    Caller must guard on a non-empty snipes list — a row missing from an empty
    list is far more likely a scrape glitch than a removal. A stamp is cleared
    the moment the row reappears, so a transient per-row scrape miss heals on
    the next sync (10-min cadence) and can't later masquerade as cancel
    evidence. Caller commits.

    scrape_started_at guards the stamp against rows added while the scrape was
    in flight: the background _sync_loop holds no _api_lock, so a concurrent
    POST /api/bids can insert a PENDING row that is legitimately absent from a
    list snapshot taken before it existed — that absence is not a vanish.
    """
    if not gixen_item_ids:
        return  # defensive: `item_id IN ()` is a SQLite syntax error
    placeholders = ",".join("?" * len(gixen_item_ids))
    ids = list(gixen_item_ids)
    db.execute(
        "UPDATE bids SET gixen_vanished_at = NULL "
        "WHERE status = 'PENDING' AND gixen_vanished_at IS NOT NULL "
        f"AND item_id IN ({placeholders})",
        ids,
    )
    db.execute(
        "UPDATE bids SET gixen_vanished_at = ? "
        "WHERE status = 'PENDING' AND gixen_vanished_at IS NULL "
        f"AND item_id NOT IN ({placeholders}) "
        "AND datetime(added_at) <= datetime(?)",
        [now, *ids, scrape_started_at],
    )


# ---------------------------------------------------------------------------
# Gixen sync helper (used by api_purge and ended-bid resolution)
# ---------------------------------------------------------------------------

async def _resolve_vanished_null_end_bids(
    db: sqlite3.Connection,
    snipes: list,
    gixen_item_ids: set,
    now_dt: datetime,
    now: str,
) -> None:
    # BUI-85: PENDING rows that vanished from Gixen but never had an end time
    # captured (auction_end_at IS NULL) escape the vanished_ended query above —
    # it requires a non-NULL end. These rows are ambiguous on their own ("the
    # auction ended and Gixen dropped it" vs "the user removed the snipe via
    # Gixen's web UI before any sync ran"), so they can't be blindly marked
    # ENDED. eBay's listing end time is the external signal that disambiguates:
    #   - end in the past  → the auction genuinely ended → ENDED (the eBay
    #     fallback then fills winning_bid). Glitch-safe: a still-live snipe has
    #     a future end, so it can never wrongly land here.
    #   - end in the future → the auction is still live but the snipe is gone →
    #     the user removed it → tombstone REMOVED (never ENDED/WON/LOST). Only
    #     when Gixen returned a non-empty list this sync, so an empty-list
    #     scrape glitch can't mass-cancel live snipes.
    #   - no eBay data      → leave PENDING and retry a later sync.
    # Gated by the eBay cooldown and capped per sync to bound rate-limited I/O.
    if _ebay_fetch_bin() is not None and now_dt.timestamp() >= _ebay_cooldown_until:
        vanished_null_end = db.execute(
            "SELECT item_id FROM bids "
            "WHERE status = 'PENDING' AND auction_end_at IS NULL"
        ).fetchall()
        checked = 0
        for row in vanished_null_end:
            iid = row["item_id"]
            if iid in gixen_item_ids:
                continue  # still live on Gixen; the time_to_end path sets end
            if checked >= _VANISHED_NULL_END_MAX_PER_SYNC:
                break
            checked += 1
            ebay = await asyncio.to_thread(_fetch_ebay_item_sync, iid)
            end_iso = (ebay or {}).get("end_date_iso")
            end_dt = _parse_end_iso(end_iso)
            if end_dt is None:
                continue  # can't disambiguate yet — leave PENDING, retry later
            set_auction_end_time(db, iid, end_iso)
            if end_dt <= now_dt:
                update_bid_status(db, iid, "ENDED", winning_bid=None, resolved_at=now)
                logger.info(
                    "_sync_gixen: %s vanished w/ NULL end; eBay end %s is past → ENDED",
                    iid, end_iso,
                )
            elif snipes:
                update_bid_status(db, iid, "REMOVED", winning_bid=None, resolved_at=now)
                logger.info(
                    "_sync_gixen: %s vanished w/ NULL end; eBay end %s still future "
                    "→ removed from Gixen, tombstoned REMOVED", iid, end_iso,
                )


def _insert_web_added_bids(db: sqlite3.Connection, snipes: list) -> None:
    # Insert any Gixen snipes not yet in the DB (e.g. added via web UI). Use
    # the full bids table — not just PENDING — so a snipe we already
    # transitioned to a terminal status earlier in this same sync run isn't
    # re-inserted as a fresh PENDING duplicate.
    existing_ids = {b["item_id"] for b in get_all_bids(db)}
    for snipe in snipes:
        snipe_terminal = _map_terminal_status(
            snipe.get("status", ""), snipe.get("time_to_end", "")
        )
        if snipe["item_id"] not in existing_ids and snipe_terminal is None:
            try:
                max_bid = float(snipe.get("max_bid") or 0)
            except (ValueError, TypeError):
                max_bid = 0.0
            try:
                insert_bid(
                    db, snipe["item_id"], max_bid,
                    int(snipe.get("bid_offset", 6)),
                    # BUI-381: never int()-crash the sync batch on a scrape
                    # quirk ('N/A'); an unknown group inserts as 0 and the
                    # per-sync refresh corrects it once it parses.
                    _parse_snipe_group(snipe.get("snipe_group")) or 0,
                    snipe.get("seller"),
                )
                logger.info("_sync_gixen: inserted web-added snipe %s", snipe["item_id"])
            except sqlite3.IntegrityError:
                # existing_ids was snapshotted before the list_snipes await; a
                # concurrent api_add_bid can insert this PENDING row in that
                # window. This loop runs unlocked (_sync_loop uses a separate
                # client, no _api_lock), so the partial unique index is what
                # actually prevents the duplicate — catch its violation and skip
                # rather than aborting the whole sync run (BUI-67 U4/KTD6).
                #
                # rollback() scope: the only uncommitted statement here is this
                # failed INSERT — the terminal/cache writes from the earlier loop
                # were committed at the db.commit() above, and each insert_bid
                # self-commits. So this discards just the failed insert, not any
                # batched sibling work.
                db.rollback()
                logger.debug(
                    "_sync_gixen: %s already present (concurrent add); skipping insert",
                    snipe["item_id"],
                )


async def _record_listed_win_evidence(
    db: sqlite3.Connection, snipe: dict, now: str
) -> bool:
    """Record group-win evidence for a listed WON snipe whose win is not in
    the bids table (BUI-381). Returns True when an eBay fetch was performed
    (consuming the caller's per-sync budget), False on any early skip.

    The web-add path never inserts a snipe first seen already-terminal
    (_insert_web_added_bids skips those), so such a winner's WON transition is
    a no-op and the win would otherwise leave no trace for _group_won_before —
    its cancelled siblings would fall through to the fallback's phantom-WON
    window. Winners that DO transition WON on a bids row are recorded inside
    update_bid_status; this covers the row-less (or tombstoned-row) case, and
    the case where the row's stored group diverges from the listed one (the
    WON-row guard is group-aware, so a winner that resolved before its group
    was known still gets eBay-backed evidence under the listed group).

    The win's true end time is fetched from eBay — Gixen's list only says
    'ENDED' — because recording an observation-time proxy would be unsound
    against the classifier's lifetime bound: a win that actually predates a
    sibling's added_at could falsely group-cancel it (the recycled-group
    hazard from the BUI-371 review). No end, no evidence (WON-permissive), and
    the next sync retries naturally: the winner stays on Gixen's list until
    purged, while the already-recorded check keeps this to at most one eBay
    call per (group, item). An end in the future (past the estimation margin)
    is self-contradictory for a WON — eBay is describing a different,
    re-listed auction under the same item id — and records nothing. Caller
    commits.

    Deliberately awaited inline in the sync (same blocking trade as the
    BUI-85 _resolve_vanished_null_end_bids resolver, same cooldown gate,
    same per-sync cap) rather than deferred to the fire-and-forget fallback
    task: the evidence must exist before the sibling's classification runs
    later in this same sync — a deferred write races the fallback's WON
    inference, and a WON stamped with a price is never revisited."""
    group = _parse_snipe_group(snipe.get("snipe_group"))
    if not group:
        return False
    iid = snipe["item_id"]
    if db.execute(
        "SELECT 1 FROM bids WHERE item_id=? AND status='WON' AND snipe_group=? "
        "LIMIT 1",
        (iid, group),
    ).fetchone() is not None:
        return False  # update_bid_status recorded this win at transition time
    if db.execute(
        "SELECT 1 FROM group_wins WHERE snipe_group=? AND item_id=?",
        (group, iid),
    ).fetchone() is not None:
        return False
    if _ebay_fetch_bin() is None:
        return False
    if datetime.now(timezone.utc).timestamp() < _ebay_cooldown_until:
        return False
    ebay = await asyncio.to_thread(_fetch_ebay_item_sync, iid)
    end_iso = (ebay or {}).get("end_date_iso")
    end_dt = _parse_end_iso(end_iso)
    if end_dt is None or end_dt > datetime.now(timezone.utc) + _CANCEL_EVIDENCE_MARGIN:
        return True  # no usable end → record nothing (WON-permissive); fetch spent
    record_group_win(db, iid, group, end_iso, recorded_at=now)
    logger.info(
        "_sync_gixen: recorded group-win evidence for row-less winner %s "
        "(group %d, ended %s)", iid, group, end_iso,
    )
    return True


async def _sync_gixen(db: sqlite3.Connection, client: GixenClient, *, reraise: bool = False) -> list:
    """Pull current Gixen state and update DB. Returns the snipes list.

    For every snipe Gixen returns, refresh the cached title/seller/current_bid
    on the matching DB row (cache_gixen_data) and apply terminal status
    transitions (WON/LOST/...). Insert new snipes that arrived via Gixen's
    web UI. For PENDING DB rows that have vanished from Gixen's response and
    whose auction_end_at is in the past, flip status to ENDED so the eBay
    fallback can backfill winning_bid — unless there is positive evidence the
    snipe was cancelled while still live (BUI-371: vanished from a healthy
    list well before its end, or a bid-group sibling won well before its end),
    in which case it is tombstoned REMOVED so the fallback can't infer a
    phantom WON on an auction we never bid. Vanished-but-still-in-future rows
    stay PENDING, but the first sync that observes one missing from a
    non-empty list stamps gixen_vanished_at — the timestamp that later
    disambiguates "cancelled before end" from "executed at end".

    `reraise` lets a caller (namely `_sync_loop`, BUI-263) distinguish "Gixen
    genuinely unreachable" from "Gixen reached fine, zero live snipes right
    now" — both used to collapse to an empty list, which made a quiet week
    of no active snipes look identical to a sustained outage.
    """
    # Captured before the scrape so vanish stamping can exclude rows added
    # while the (lockless) scrape was in flight — see _record_vanish_observations.
    scrape_started_at = datetime.now(timezone.utc).isoformat()
    try:
        snipes = await asyncio.to_thread(client.list_snipes)
    except GixenConnectionError as e:
        # Gixen unreachable at the network layer (BUI-77) — distinct, honest
        # signal so the operator isn't sent chasing credentials.
        logger.warning("_sync_gixen: Gixen unreachable (connectivity, not creds): %s", e)
        if reraise:
            raise
        return []
    except GixenError as e:
        logger.warning("_sync_gixen: GixenError (suppressed): %s", e)
        if reraise:
            raise
        return []

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    gixen_item_ids = {s["item_id"] for s in snipes}

    # BUI-371: vanish bookkeeping. Only against a non-empty list — an empty
    # scrape is more likely a glitch (BUI-85's guard), and mass-stamping live
    # snipes as vanished could later mislabel them as cancelled.
    if snipes:
        _record_vanish_observations(db, gixen_item_ids, now, scrape_started_at)

    terminal_transitions: list[tuple[dict, str]] = []
    for snipe in snipes:
        iid = snipe["item_id"]

        cache_gixen_data(
            db, iid,
            snipe.get("title") or None,
            snipe.get("seller") or None,
            snipe.get("current_bid") or None,
            snipe.get("dbidid") or None,  # BUI-116: warm the edit fast-path cache
        )

        # BUI-381: mirror the list's snipe_group onto the live row on every
        # sync (see refresh_snipe_group). Runs before the terminal transitions
        # below, so a winner whose group was applied retroactively on Gixen's
        # web UI carries it by the time its WON is recorded as group evidence.
        # An unparseable value (None) is skipped — see _parse_snipe_group.
        listed_group = _parse_snipe_group(snipe.get("snipe_group"))
        if listed_group is not None:
            refresh_snipe_group(db, iid, listed_group)

        gixen_status = snipe.get("status", "")
        time_to_end = snipe.get("time_to_end", "")
        internal_status = _map_terminal_status(gixen_status, time_to_end)
        if internal_status is not None:
            terminal_transitions.append((snipe, internal_status))

        # Refresh auction_end_at from Gixen's relative time string on every
        # sync. Gixen only gives "21 h, 30 m, 43 s" so we compute the absolute
        # end timestamp here. (Originally only eBay populated this — bringing
        # in main's logic so the local-sniper has a current end time without
        # depending on eBay being reachable.)
        time_to_end = snipe.get("time_to_end", "")
        if time_to_end and time_to_end.upper() != "ENDED":
            delta = _parse_time_to_end(time_to_end)
            if delta is not None:
                end_time = (now_dt + delta).isoformat()
                set_auction_end_time(db, iid, end_time)

    # Apply WON transitions before the rest: a WON row in the DB is the
    # group-cancel evidence _group_won_before consults when classifying its
    # siblings' ENDED/LOST below (BUI-371) — after a sync gap, the winner and
    # a cancelled sibling can arrive in the same list pull.
    terminal_transitions.sort(key=lambda pair: pair[1] != "WON")
    listed_win_fetches = 0  # BUI-381: per-sync eBay budget for row-less winners
    for snipe, internal_status in terminal_transitions:
        iid = snipe["item_id"]
        gixen_status = snipe.get("status", "")
        # For WON/LOST, current_bid is the final price (what we paid or
        # what beat us). For ENDED/FAILED with unknown status string,
        # there's no reliable price signal — leave winning_bid None and
        # let the eBay fallback fill it in if it can.
        winning_bid = None
        if internal_status in ("WON", "LOST"):
            current_bid = snipe.get("current_bid", "")
            if current_bid:
                try:
                    winning_bid = float(current_bid.split()[0])
                except (ValueError, IndexError):
                    pass
        # BUI-371: a still-listed snipe reaching its end as ENDED (unrecognized
        # status) or a plain LOST may in fact be a group-cancelled sibling that
        # was never bid on — resolve it REMOVED so the eBay fallback can't
        # phantom-WON it and the calibration report doesn't count a loss we
        # never contested. Exempt: statuses proving Gixen processed our bid
        # (their LOST is genuine), an 'OK:' local snipe result (we bid
        # locally), and rows already tombstoned (a REMOVED sibling stays on
        # Gixen's list until purge — the update below is a no-op for it, and
        # re-running the evidence query would re-log every sync). WON is never
        # reclassified (dual-win within Gixen's ~2-minute group caveat is a
        # real win).
        if (
            internal_status in ("ENDED", "LOST")
            and gixen_status.upper().strip() not in _BID_PROCESSED_STATUSES
        ):
            db_row = get_bid_by_item_id(db, iid)
            if (
                db_row is not None
                and db_row["status"] not in ("PURGED", "REMOVED")
                and not (db_row["local_snipe_result"] or "").startswith("OK:")
            ):
                # No stored end → no evidence test. `now` is only an upper
                # bound on the true end, and substituting it would WIDEN the
                # evidence window (it can only add cancel classifications,
                # including inside the dual-win margin). Skipping is safe:
                # the row resolves ENDED below and the eBay fallback re-runs
                # this check with the true end time fetched from eBay.
                end_dt = _parse_end_iso(db_row["auction_end_at"])
                if end_dt is not None and _group_won_before(
                    db, iid, snipe.get("snipe_group"), end_dt, db_row["added_at"]
                ):
                    update_bid_status(
                        db, iid, "REMOVED", None, now, snipe.get("status_mirror"),
                        only_id=db_row["id"],
                    )
                    _mark_cancelled_tombstone(db, db_row["id"])
                    logger.info(
                        "_sync_gixen: %s group-cancelled before its end → REMOVED "
                        "(Gixen showed %s/%s)", iid, gixen_status or "?", internal_status,
                    )
                    continue
        update_bid_status(
            db, iid, internal_status, winning_bid, now,
            snipe.get("status_mirror"),
        )
        if (
            internal_status == "WON"
            and listed_win_fetches < _LISTED_WIN_FETCH_MAX_PER_SYNC
        ):
            # BUI-381: a winner first seen already-terminal has no DB row
            # (the web-add insert below skips terminal snipes), so the
            # update above was a no-op and its win left no group evidence —
            # record it from the list + eBay's end time. No-op when the
            # update did land on a grouped row (update_bid_status records
            # those). Capped per sync like the BUI-85 resolver.
            if await _record_listed_win_evidence(db, snipe, now):
                listed_win_fetches += 1

    # Vanished + ended → flip to ENDED. The eBay fallback path then picks
    # them up (ENDED rows with NULL winning_bid) and resolves the final
    # selling price when eBay's rate-limit budget allows.
    vanished_ended = db.execute(
        """
        SELECT item_id, id, auction_end_at, gixen_vanished_at, snipe_group,
               local_snipe_result, added_at FROM bids
        WHERE status = 'PENDING'
          AND auction_end_at IS NOT NULL
          AND auction_end_at <= ?
        """,
        (now,),
    ).fetchall()
    for row in vanished_ended:
        iid = row["item_id"]
        if iid in gixen_item_ids:
            continue  # still on Gixen, will resolve via Gixen path
        # BUI-371: disambiguate before flipping ENDED (which feeds the eBay
        # WON inference). Positive evidence the snipe was cancelled while its
        # auction was still live — observed vanished from a healthy Gixen list
        # >= margin before end, or a bid-group sibling won >= margin earlier —
        # means we never bid: tombstone REMOVED. No evidence → ENDED as before.
        end_dt = _parse_end_iso(row["auction_end_at"])
        if _cancelled_before_end(db, iid, row, end_dt):
            update_bid_status(
                db, iid, "REMOVED", winning_bid=None, resolved_at=now,
                only_id=row["id"],
            )
            _mark_cancelled_tombstone(db, row["id"])
            logger.info(
                "_sync_gixen: %s vanished from Gixen while still live "
                "(cancelled, never bid) → REMOVED", iid,
            )
            continue
        update_bid_status(db, iid, "ENDED", winning_bid=None, resolved_at=now)
        logger.info(
            "_sync_gixen: %s vanished from Gixen and auction has ended → ENDED",
            iid,
        )

    await _resolve_vanished_null_end_bids(db, snipes, gixen_item_ids, now_dt, now)

    db.commit()

    _insert_web_added_bids(db, snipes)

    return snipes


# Background sync loop — primarily for the local sniper, which needs fresh
# auction_end_at to fire bids at the right time. The dashboard does its own
# pull-on-visit (_ensure_fresh_sync) and doesn't depend on this loop, but the
# loop keeps state fresh enough that the sniper can act when nobody's looking.
async def _sync_loop() -> None:
    consecutive_failures = 0
    while True:
        try:
            if _sync_client is not None:
                db = _get_db()
                # reraise=True: a call that *returns* (even an empty list —
                # e.g. no live snipes right now) is success. Only a raised
                # GixenConnectionError/GixenError counts as a failure; BUI-263
                # found the old "falsy result == failure" check was mistaking
                # a quiet week of zero snipes for 177+ hours of outage.
                await _sync_gixen(db, _sync_client, reraise=True)
            consecutive_failures = 0
            last_error = None
        except (GixenConnectionError, GixenError) as e:
            # Already logged with the specific reason inside _sync_gixen —
            # don't also dump a full traceback here on every retry.
            consecutive_failures += 1
            last_error = e
        except Exception as e:
            logger.exception("_sync_loop: unexpected error, continuing")
            consecutive_failures += 1
            last_error = e

        # Exponential backoff: SYNC_INTERVAL, 2x, 4x, ..., capped at 1 hour
        delay = min(SYNC_INTERVAL * (2 ** consecutive_failures), _SYNC_BACKOFF_MAX)
        if consecutive_failures:
            logger.warning(
                "_sync_loop: %d consecutive failure(s) (%s: %s), sleeping %ds",
                consecutive_failures, type(last_error).__name__, last_error, delay,
            )
        await asyncio.sleep(delay)


def _parse_time_to_end(s: str) -> timedelta | None:
    """Parse Gixen relative time string like '1 d, 20 h, 59 m' into a timedelta."""
    total = 0
    matched = False
    for part in s.split(","):
        part = part.strip()
        if m := re.match(r"(\d+)\s*d", part):
            total += int(m.group(1)) * 86400
            matched = True
        elif m := re.match(r"(\d+)\s*h", part):
            total += int(m.group(1)) * 3600
            matched = True
        elif m := re.match(r"(\d+)\s*m", part):
            total += int(m.group(1)) * 60
            matched = True
        elif m := re.match(r"(\d+)\s*s", part):
            total += int(m.group(1))
            matched = True
    # BUI-184: gate on "did any part parse", not "is total truthy". A snipe seen
    # at exactly "0 s" parses to 0 seconds (auction about to end) and must yield
    # timedelta(0) so auction_end_at is set and the local sniper fires it — only a
    # genuinely unparseable/empty string returns None.
    return timedelta(seconds=total) if matched else None


SNIPER_INTERVAL = 10  # check every 10 seconds


async def _sniper_loop() -> None:
    while True:
        try:
            if _bidder is not None:
                db = _get_db()
                now_iso = datetime.now(timezone.utc).isoformat()
                ready = get_bids_ready_to_snipe(db, now_iso)
                if ready:
                    fired_at = datetime.now(timezone.utc).isoformat()
                    logger.info("_sniper_loop: firing %d bid(s) concurrently", len(ready))
                    bids = [{"item_id": b["item_id"], "max_bid": b["max_bid"]} for b in ready]
                    results = await _bidder.place_bids_concurrent(bids)
                    for bid, result in zip(ready, results):
                        result_str = ("OK: " if result["success"] else "ERR: ") + result["message"]
                        set_local_snipe_result(db, bid["item_id"], fired_at, result_str)
                        logger.info("_sniper_loop: %s — %s", bid["item_id"], result_str)
        except Exception:
            logger.exception("_sniper_loop: unexpected error, continuing")
        await asyncio.sleep(SNIPER_INTERVAL)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _ensure_fresh_sync() -> None:
    """Pull latest state from Gixen if our last pull was older than _SYNC_TTL.

    Called at the top of /api/snipes. Concurrent dashboard loads share one
    in-flight Gixen scrape via _sync_lock, then return immediately if the
    just-completed pull is still fresh enough.
    """
    global _last_sync_at
    if not _sync_lock or not _api_lock:
        return

    async with _sync_lock:
        now_ts = datetime.now(timezone.utc).timestamp()
        if now_ts - _last_sync_at < _SYNC_TTL:
            return

        db = _get_db()
        try:
            async with _api_lock:
                await _sync_gixen(db, _api_client)
        except Exception:
            logger.exception("_ensure_fresh_sync: gixen pull failed")
            return
        _last_sync_at = datetime.now(timezone.utc).timestamp()


def _ebay_fallback_rows(db: sqlite3.Connection, now_iso: str) -> list:
    """Rows needing eBay price resolution. Two sets:
    1. PENDING/ENDED — auction ended, status not yet terminal.
    2. The soft-delete tombstone (REMOVED, or legacy PURGED) resolved without a
       winning_bid (e.g. bulk-removed before the fallback ran), within 7 days.

    Excludes BUI-67 dedup losers (REMOVED with notes=DEDUP_TOMBSTONE_NOTE): they
    are not real ended auctions, and the 7-day window matches on their freshly-set
    resolved_at, so without this guard they'd burn an eBay call and could get a
    phantom winning_bid/WON stamp. The 'IS NOT' comparison keeps NULL-notes rows.
    """
    return db.execute(
        f"""
        SELECT item_id, id, max_bid, local_snipe_result, auction_end_at,
               gixen_vanished_at, snipe_group, added_at, 0 AS is_purged FROM bids
        WHERE status IN ('PENDING', 'ENDED')
          AND auction_end_at IS NOT NULL
          AND auction_end_at <= ?
          AND winning_bid IS NULL
        UNION ALL
        SELECT item_id, id, max_bid, local_snipe_result, auction_end_at,
               gixen_vanished_at, snipe_group, added_at, 1 AS is_purged FROM bids
        WHERE status IN ({TOMBSTONE_STATUSES_SQL})
          AND winning_bid IS NULL
          AND notes IS NOT ?
          AND datetime(COALESCE(auction_end_at, resolved_at)) >= datetime('now', '-7 days')
        """,
        (now_iso, DEDUP_TOMBSTONE_NOTE),
    ).fetchall()


async def _run_ebay_fallback() -> None:
    """Fire-and-forget: ask eBay for the final selling price of any auction
    that's ended without a captured winning_bid. One eBay call per such item,
    ever — once winning_bid is set, the row no longer matches the filter.

    Skipped if a fallback is already running or if we're in rate-limit
    cooldown from a recent failure storm.
    """
    global _ebay_cooldown_until
    if not _ebay_fallback_lock:
        return
    if _ebay_fallback_lock.locked():
        return
    if datetime.now(timezone.utc).timestamp() < _ebay_cooldown_until:
        return

    async with _ebay_fallback_lock:
        try:
            db = _get_db()
            now_iso = datetime.now(timezone.utc).isoformat()
            rows = _ebay_fallback_rows(db, now_iso)

            if not rows:
                return

            failures = 0
            for row in rows:
                iid = row["item_id"]
                is_purged = bool(row["is_purged"])
                ebay = await asyncio.to_thread(_fetch_ebay_item_sync, iid)
                if not ebay:
                    failures += 1
                    await asyncio.sleep(1.5)
                    continue

                # Write title and end_date_iso for all rows regardless of
                # status. update_bid_status / cache_gixen_data both skip the
                # tombstone (PURGED/REMOVED) rows, so use direct SQL here.
                ebay_title = ebay.get("title") or None
                ebay_end_iso = ebay.get("end_date_iso") or None
                db.execute(
                    "UPDATE bids SET "
                    "ebay_title = COALESCE(?, ebay_title), "
                    "auction_end_at = COALESCE(auction_end_at, ?) "
                    "WHERE item_id = ?",
                    (ebay_title, ebay_end_iso, iid),
                )

                final_amount: float | None = None
                price = ebay.get("current_price")
                if price:
                    try:
                        final_amount = float(str(price).lstrip("$").strip())
                    except (ValueError, TypeError):
                        final_amount = None

                if is_purged:
                    if final_amount is not None and final_amount > 0:
                        db.execute(
                            f"UPDATE bids SET winning_bid = ? WHERE item_id = ? AND status IN ({TOMBSTONE_STATUSES_SQL})",
                            (final_amount, iid),
                        )
                        logger.info(
                            "_run_ebay_fallback: %s (purged) winning_bid=$%.2f",
                            iid, final_amount,
                        )
                    await asyncio.sleep(1.5)
                    continue

                # BUI-371: positive evidence this snipe was cancelled while its
                # auction was still live (vanished from a healthy Gixen list
                # >= margin before end, or a bid-group sibling won >= margin
                # earlier) means we never bid — resolve REMOVED and never feed
                # it to the WON/LOST price inference below. Normally the sync
                # classifies these first, but the fallback can reach a row
                # ahead of a successful sync (Gixen outage), and rows flipped
                # ENDED before this fix landed are healed here too. Recording
                # the final price keeps history parity with the purged branch.
                end_dt = _parse_end_iso(row["auction_end_at"])
                if _cancelled_before_end(db, iid, row, end_dt):
                    update_bid_status(
                        db, iid, "REMOVED",
                        winning_bid=final_amount if final_amount and final_amount > 0 else None,
                        resolved_at=now_iso,
                        only_id=row["id"],
                    )
                    _mark_cancelled_tombstone(db, row["id"])
                    logger.info(
                        "_run_ebay_fallback: %s cancelled before end (never bid) "
                        "→ REMOVED", iid,
                    )
                    await asyncio.sleep(1.5)
                    continue

                if final_amount is None or final_amount <= 0:
                    # eBay returns the high-water bid for reserve-not-met or
                    # unsold listings, which is often 0 or well below our max
                    # — falsely stamping WON. Treat as ENDED with no winning
                    # claim instead. We still mark resolved_at so the row
                    # leaves the fallback queue.
                    update_bid_status(
                        db, iid, "ENDED",
                        winning_bid=None,
                        resolved_at=now_iso,
                    )
                    logger.info(
                        "_run_ebay_fallback: %s -> ENDED (no final price; max=$%.2f)",
                        iid, row["max_bid"],
                    )
                    await asyncio.sleep(1.5)
                    continue

                # Heuristic: 0 < final_price < our max_bid suggests we outbid
                # everyone; final_price >= max_bid means someone matched or beat
                # us. Two additional guards against false positives:
                #   1. Tie at final_price == max_bid → eBay's first-bidder rule
                #      means we likely lost (strict < instead of <=).
                #   2. local_snipe_result starts with "ERR:" → our bid never
                #      landed; mark LOST regardless of price.
                #
                # BUI-146 (do NOT "fix" this inference naively): a snipe
                # cancelled while still live (user removal on Gixen's web UI,
                # or a bid-group auto-cancel — BUI-371) also reaches here once
                # its auction ends, and a final price below max_bid then stamps
                # a phantom WON even though we never bid. Crucially, this same
                # inference is how genuine wins are recovered when Gixen drops
                # an ended snipe before sync reads its WON status — with the
                # local sniper disabled, local_snipe_result is always NULL, so
                # requiring local bid-evidence (or never inferring WON for
                # vanished rows) would SUPPRESS REAL WINS. BUI-371 implemented
                # the sanctioned vanish-time/group-win disambiguation UPSTREAM
                # (positive cancel evidence → REMOVED before a row ever gets
                # here — see _vanished_while_live/_group_won_before and the
                # guard above). The residual evidence-less case (e.g. a live
                # cancel never observed by any sync) remains accepted risk;
                # never gate the inference itself. See BUI-146 for the full
                # analysis.
                local_result = row["local_snipe_result"] or ""
                if local_result.startswith("ERR:") or final_amount >= float(row["max_bid"]):
                    inferred_status = "LOST"
                else:
                    inferred_status = "WON"
                update_bid_status(
                    db, iid, inferred_status,
                    winning_bid=final_amount,
                    resolved_at=now_iso,
                )
                logger.info(
                    "_run_ebay_fallback: %s -> %s @ $%.2f (max=$%.2f)",
                    iid, inferred_status, final_amount, row["max_bid"],
                )
                await asyncio.sleep(1.5)

            db.commit()

            # Threshold is 1 when there's a single ended-unresolved item, else
            # half the batch. Without the floor, a single persistently-failing
            # item is retried on every dashboard load forever.
            if failures >= max(1, len(rows) // 2):
                _ebay_cooldown_until = (
                    datetime.now(timezone.utc).timestamp() + _EBAY_COOLDOWN
                )
                logger.warning(
                    "_run_ebay_fallback: %d/%d failed; cooling %ds",
                    failures, len(rows), int(_EBAY_COOLDOWN),
                )
        except Exception:
            logger.exception("_run_ebay_fallback: error")


def _spawn_fallback_task() -> None:
    """Schedule _run_ebay_fallback as a tracked task. The function itself
    short-circuits if a fallback is already running or the cooldown is
    active, so it's safe to fire on every dashboard load. Tracking the
    reference here lets lifespan teardown cancel + await it cleanly."""
    global _ebay_fallback_task
    if _ebay_fallback_task is not None and not _ebay_fallback_task.done():
        return  # one already in flight; let it finish
    _ebay_fallback_task = asyncio.create_task(_run_ebay_fallback())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _api_client, _sync_client, _api_lock, _sync_lock, _ebay_fallback_lock, _bidder
    if env_file := os.getenv("ENV_FILE"):
        load_dotenv(env_file)
    # Resolve the eBay fallback binary now that the .env (EBAY_FETCH_BIN, PATH)
    # is loaded — a missing script silently disables ENDED-auction winning-bid
    # capture, so log it loudly once at startup (BUI-66).
    if _ebay_fetch_bin() is None:
        logger.warning(
            "ebay-fetch console script not found (EBAY_FETCH_BIN=%r, PATH lookup failed) "
            "— live eBay fallback disabled. Install apps/ebay via scripts/install.sh, "
            "or set EBAY_FETCH_BIN to its absolute path in the server .env.",
            os.getenv("EBAY_FETCH_BIN", "ebay-fetch"),
        )
    db_path = Path(os.getenv("DB_PATH", str(DB_PATH)))
    _db = init_db(db_path)
    app.state.db = _db
    _api_client = GixenClient()
    _api_lock = asyncio.Lock()
    _sync_lock = asyncio.Lock()
    _ebay_fallback_lock = asyncio.Lock()

    # Plugin loading: discover entry-point plugins, then fire startup hooks.
    # Helpers live in gixen/plugins.py (PER-26 M-01); they accept an injected
    # logger so log records appear under the "server.main" logger name that
    # PER-25 regression tests assert on.
    pm = load_plugins()
    app.state.plugin_manager = pm
    _invoke_db_tables_isolated(pm, _db, logger=logger)
    _invoke_register_routes(pm, app, logger=logger)
    app.state.dashboard_tabs = _collect_dashboard_tabs(pm, logger=logger)

    # BUI-257 invariant: the only background tasks started here are the eBay
    # fallback (fire-and-forget, spawned on demand via _spawn_fallback_task),
    # this Gixen snipe-sync loop (_sync_loop, gated by GIXEN_SYNC_ENABLED), and
    # the sniper loop below — all Gixen/eBay, never LOCG. There is intentionally
    # NO automatic/background LOCG access anywhere in this server: LOCG is
    # programmatically inaccessible, and the only path to it is the manual,
    # user-invoked /comic:collection-sync skill (see locg-cli's client.py and
    # collection_io.py).
    sync_task = None
    sniper_task = None
    if os.getenv("GIXEN_SYNC_ENABLED", "true") != "false":
        # Separate client so the loop's long scrape doesn't fight _api_lock.
        _sync_client = GixenClient()
        sync_task = asyncio.create_task(_sync_loop())
    if os.getenv("LOCAL_SNIPER_ENABLED", "true") != "false":
        _bidder = ebay_bidder.EbayBidder()
        await _bidder.start()
        sniper_task = asyncio.create_task(_sniper_loop())

    yield

    # Cancel + await any in-flight eBay fallback so its DB writes complete (or
    # cleanly abort) before we close the connection. Bounded await — if the
    # task is wedged on a slow eBay call we don't want to block shutdown.
    if _ebay_fallback_task is not None and not _ebay_fallback_task.done():
        _ebay_fallback_task.cancel()
        try:
            await asyncio.wait_for(_ebay_fallback_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as e:  # noqa: BLE001  # lifespan shutdown — log any stray error from background task
            logger.warning("lifespan: fallback task raised on cancel: %s", e)

    if sniper_task:
        sniper_task.cancel()
    if _bidder:
        await _bidder.stop()
    if sync_task:
        sync_task.cancel()

    row = _db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row and row[0]:
        logger.warning("WAL checkpoint incomplete: busy=%s", row[0])
    _db.close()
    app.state.db = None


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TabSpec(BaseModel):
    label: str
    path: str


class AddBidRequest(BaseModel):
    model_config = {"extra": "ignore"}

    item_id: str
    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0
    # BUI-78: optional seller + grades captured by the buy flow at add time.
    seller: str | None = None
    seller_grade: float | None = None
    photo_grade: float | None = None

    @field_validator("item_id")
    @classmethod
    def item_id_numeric(cls, v: str) -> str:
        if not re.match(r"^\d+$", v):
            raise ValueError("item_id must be numeric")
        return v

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v

    @field_validator("seller")
    @classmethod
    def normalize_seller(cls, v: str | None) -> str | None:
        # BUI-78: canonical key = lowercased eBay username. Normalize once here so
        # the write key matches the read endpoint (which lowercases too) and the
        # 1-128 char bound is enforced on both sides. Empty/whitespace -> NULL.
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 128:
            raise ValueError("seller must be 1-128 characters")
        return v.lower()


class EditBidRequest(BaseModel):
    model_config = {"extra": "ignore"}

    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v


class PurgeRequest(BaseModel):
    model_config = {"extra": "ignore"}

    sibling_ids: list[str] = []

    @field_validator("sibling_ids")
    @classmethod
    def validate_sibling_ids(cls, v: list[str]) -> list[str]:
        for item_id in v:
            if not re.match(r"^\d+$", item_id):
                raise ValueError(f"sibling_ids contains non-numeric value: {item_id}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# Force browsers to revalidate static files on every load. Without this, a fix
# pushed to the dashboard HTML/CSS can sit invisible behind heuristic caching
# until the user knows to hard-reload. The dashboard is small and fetched
# rarely; the cost of revalidation is negligible.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/")
def root(request: Request):
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    tabs = getattr(request.app.state, "dashboard_tabs", [])
    if tabs:
        tab_links = "".join(
            f'  <a class="seg nav" href="{t["path"]}">{t["label"]}</a>\n'
            for t in tabs
        )
        html = html.replace('  <div class="spacer"></div>', f'{tab_links}  <div class="spacer"></div>', 1)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers=dict(_NO_CACHE_HEADERS))


@app.get("/v2/bids")
def variant_v2_bids():
    return FileResponse(
        Path(__file__).parent / "static" / "v2-bids.html",
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/static/v2.css")
def static_v2_css():
    return FileResponse(
        Path(__file__).parent / "static" / "v2.css",
        media_type="text/css",
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/dashboard-tabs", response_model=list[TabSpec])
def api_dashboard_tabs(request: Request) -> list[dict]:
    return getattr(request.app.state, "dashboard_tabs", [])


async def _modify_and_update_bid(
    db: sqlite3.Connection, item_id: str, max_bid: float,
    bid_offset: int, snipe_group: int,
    seller: str | None = None, seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> sqlite3.Row:
    """Gixen modify_snipe (off-thread) + local update_bid. Re-raises
    GixenSnipeNotFoundError so the caller owns the not-found *policy* (add falls
    back; edit 404s). Caller must already hold _api_lock — this does NOT acquire
    it, so the lookup→Gixen→DB-write sequence stays atomic (BUI-67 KTD6/KTD7).
    """
    await asyncio.to_thread(
        _api_client.modify_snipe,
        item_id, Decimal(str(max_bid)),
        bid_offset=bid_offset, snipe_group=snipe_group,
    )
    update_bid(db, item_id, max_bid, bid_offset, snipe_group)
    # BUI-78 C2: fill any NULL seller/grade columns from this request without
    # overwriting values a prior add already set.
    update_bid_grades(db, item_id, seller=seller, seller_grade=seller_grade,
                      photo_grade=photo_grade)
    return get_pending_bid_by_item_id(db, item_id) or get_bid_by_item_id(db, item_id)


async def _add_bid_row(
    db: sqlite3.Connection, item_id: str, max_bid: float,
    bid_offset: int, snipe_group: int,
    seller: str | None = None, seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> tuple[sqlite3.Row, bool]:
    """Gixen add_snipe (off-thread) + insert_bid; returns (row, created=True).

    On a partial-unique-index collision — a racing unlocked _sync_loop insert for
    the same item landed first (BUI-67 KTD6) — recover by updating the existing
    live row and return (row, created=False) instead of 500. Caller holds
    _api_lock.
    """
    await asyncio.to_thread(
        _api_client.add_snipe,
        item_id, Decimal(str(max_bid)),
        bid_offset=bid_offset, snipe_group=snipe_group,
    )
    try:
        bid_id = insert_bid(
            db, item_id=item_id, max_bid=max_bid,
            bid_offset=bid_offset, snipe_group=snipe_group, seller=seller,
            seller_grade=seller_grade, photo_grade=photo_grade,
        )
        return db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone(), True
    except sqlite3.IntegrityError:
        db.rollback()
        update_bid(db, item_id, max_bid, bid_offset, snipe_group)
        # BUI-78 C2: a racing sync insert won the row; still fill its NULL grades.
        update_bid_grades(db, item_id, seller=seller, seller_grade=seller_grade,
                          photo_grade=photo_grade)
        row = get_pending_bid_by_item_id(db, item_id) or get_bid_by_item_id(db, item_id)
        return row, False


@app.post("/api/bids")
async def api_add_bid(req: AddBidRequest):
    db = _get_db()
    # BUI-78: req.seller is already normalized (lowercased, validated) by
    # AddBidRequest.normalize_seller.
    seller = req.seller
    try:
        # Lookup + Gixen call + DB write all under _api_lock so the add/modify
        # decision is atomic against other request handlers (BUI-67 KTD6). The
        # unlocked background _sync_loop is the remaining concurrent writer; the
        # partial unique index (+ _add_bid_row's recovery) guards that race.
        async with _api_lock:
            existing = get_pending_bid_by_item_id(db, req.item_id)
            if existing is not None:
                # A live snipe exists → update in place. Gixen rejects a re-add of
                # an already-sniped item (code 202), so modify, not add.
                try:
                    row = await _modify_and_update_bid(
                        db, req.item_id, req.max_bid, req.bid_offset, req.snipe_group,
                        seller=seller, seller_grade=req.seller_grade,
                        photo_grade=req.photo_grade,
                    )
                    return {**dict(row), "created": False}
                except GixenSnipeNotFoundError:
                    # DB has a live row but Gixen lost it (state skew). Intent is
                    # "add" → fall back. If Gixen can't confirm the add, keep the
                    # existing row visible rather than a bare 503 that hides it.
                    try:
                        row, created = await _add_bid_row(
                            db, req.item_id, req.max_bid, req.bid_offset, req.snipe_group,
                            seller=seller, seller_grade=req.seller_grade,
                            photo_grade=req.photo_grade,
                        )
                        return {**dict(row), "created": created}
                    except GixenAddNotConfirmedError:
                        return {**dict(existing), "created": False, "applied": False}

            row, created = await _add_bid_row(
                db, req.item_id, req.max_bid, req.bid_offset, req.snipe_group,
                seller=seller, seller_grade=req.seller_grade,
                photo_grade=req.photo_grade,
            )
            return {**dict(row), "created": created}
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except requests.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Gixen HTTP error: {e}") from e


def _serialize_snipe_row(item: dict) -> dict:
    """Shared row shape for /api/snipes and /api/history (BUI-273). The two
    endpoints differ only in their WHERE filter — this is the parity surface
    that BUI-50 drifted on, so keep it as the single source of truth.
    """
    end_date_iso = item.get("auction_end_at")
    return {
        "item_id": item["item_id"],
        "title": item.get("ebay_title") or None,
        "current_bid": item.get("cached_current_bid"),
        "max_bid": f"{item['max_bid']:.2f} USD",
        "bid_offset": item["bid_offset"],
        "snipe_group": item["snipe_group"],
        "time_to_end": iso_to_relative(end_date_iso),
        "end_date_iso": end_date_iso,
        "status": item["status"],
        "status_mirror": item.get("status_mirror"),
        "winning_bid": item.get("winning_bid"),
        "seller": item.get("seller"),
        "cached_at": item.get("cached_at"),
        "local_snipe_at": item.get("local_snipe_at"),
        "local_snipe_result": item.get("local_snipe_result"),
    }


@app.get("/api/snipes")
async def api_get_snipes():
    """Pull-on-visit. Synchronously refreshes from Gixen (deduped within
    _SYNC_TTL across concurrent calls), then returns cached DB rows. eBay is
    invoked only as a fire-and-forget fallback for ended bids that never got
    a winning_bid captured — never blocks this response.
    """
    await _ensure_fresh_sync()
    _spawn_fallback_task()

    db = _get_db()

    rows = db.execute(f"""
        SELECT * FROM bids
        WHERE status NOT IN ({TOMBSTONE_STATUSES_SQL})
        ORDER BY added_at DESC
    """).fetchall()

    return [_serialize_snipe_row(dict(row)) for row in rows]


@app.get("/api/history")
async def api_get_history():
    """Recently ended bids from the DB (past 7 days), including removed
    (REMOVED/PURGED) rows. Pure DB read — no Gixen sync.
    """
    db = _get_db()
    rows = db.execute("""
        SELECT b.* FROM bids b
        INNER JOIN (
            SELECT item_id, MAX(id) AS max_id
            FROM bids
            WHERE (
              auction_end_at IS NOT NULL
              AND datetime(auction_end_at) <= datetime('now')
              AND datetime(auction_end_at) >= datetime('now', '-7 days')
            ) OR (
              auction_end_at IS NULL
              AND resolved_at IS NOT NULL
              AND datetime(resolved_at) >= datetime('now', '-7 days')
            )
            GROUP BY item_id
        ) latest ON b.id = latest.max_id
        ORDER BY COALESCE(b.auction_end_at, b.resolved_at) DESC
    """).fetchall()

    return [_serialize_snipe_row(dict(row)) for row in rows]


@app.get("/api/bids")
async def api_get_all_bids():
    """All bids from the DB, newest first. Pure DB read — no Gixen sync."""
    db = _get_db()
    rows = db.execute("""
        SELECT * FROM bids
        ORDER BY COALESCE(auction_end_at, added_at) DESC
    """).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        result.append({
            "item_id": item["item_id"],
            "title": item.get("ebay_title") or None,
            "max_bid": item["max_bid"],
            "bid_offset": item["bid_offset"],
            "snipe_group": item["snipe_group"],
            "end_date_iso": item.get("auction_end_at"),
            "added_at": item.get("added_at"),
            "status": item["status"],
            "status_mirror": item.get("status_mirror"),
            "winning_bid": item.get("winning_bid"),
            "seller": item.get("seller"),
            "local_snipe_at": item.get("local_snipe_at"),
            "local_snipe_result": item.get("local_snipe_result"),
            # BUI-371: expose the vanish observation + tombstone-cause note so
            # a REMOVED row's classification is auditable over HTTP (agents
            # have no sqlite access to the Mac Mini) — parity with the
            # server-log evidence trail.
            "gixen_vanished_at": item.get("gixen_vanished_at"),
            "notes": item.get("notes"),
        })
    return result


def _cached_dbidid(db: sqlite3.Connection, item_id: str) -> str | None:
    """BUI-116: the cached Gixen dbidid for a bid, or None on a cache miss.

    Reads the live (PENDING) row first, falling back to any row. NULL until a
    sync has warmed the cache, which simply means the edit takes the list path.
    """
    row = get_pending_bid_by_item_id(db, item_id) or get_bid_by_item_id(db, item_id)
    if row is None:
        return None
    try:
        return row["dbidid"]
    except (KeyError, IndexError):
        return None


def _clear_cached_dbidid(db: sqlite3.Connection, item_id: str) -> None:
    db.execute("UPDATE bids SET dbidid=NULL WHERE item_id=?", (item_id,))
    db.commit()


async def _modify_with_cache_fallback(
    db: sqlite3.Connection, item_id: str, max_bid: Decimal,
    bid_offset: int, snipe_group: int,
) -> None:
    """BUI-116: modify using the cached dbidid (fast path, no pre-POST list). If
    a cached id was used but the modify couldn't be confirmed (stale id — the
    snipe was re-created with a new dbidid), clear the cache and retry once via
    the list-based lookup. Holds _api_lock across both attempts so the sequence
    stays atomic. Exceptions propagate to the caller for HTTP mapping."""
    cached = _cached_dbidid(db, item_id)
    async with _api_lock:
        try:
            await asyncio.to_thread(
                _api_client.modify_snipe, item_id, max_bid,
                bid_offset=bid_offset, snipe_group=snipe_group, dbidid=cached,
            )
            return
        except GixenModifyNotConfirmedError:
            if cached is None:
                raise  # already used the list path — genuinely unconfirmable
            logger.warning(
                "modify with cached dbidid for %s unconfirmed; clearing cache "
                "and retrying via list lookup", item_id,
            )
            _clear_cached_dbidid(db, item_id)
            await asyncio.to_thread(
                _api_client.modify_snipe, item_id, max_bid,
                bid_offset=bid_offset, snipe_group=snipe_group,  # dbidid=None
            )


async def _remove_with_cache_fallback(db: sqlite3.Connection, item_id: str) -> None:
    """BUI-116: remove using the cached dbidid, falling back to the list-based
    lookup if a cached id failed (stale id left the item in the list, or a
    transient error). Holds _api_lock across both attempts."""
    cached = _cached_dbidid(db, item_id)
    async with _api_lock:
        try:
            await asyncio.to_thread(_api_client.remove_snipe, item_id, dbidid=cached)
            return
        except GixenError:
            if cached is None:
                raise
            logger.warning(
                "remove with cached dbidid for %s failed; clearing cache and "
                "retrying via list lookup", item_id,
            )
            _clear_cached_dbidid(db, item_id)
            await asyncio.to_thread(_api_client.remove_snipe, item_id)  # dbidid=None


@app.patch("/api/bids/{item_id}")
async def api_edit_bid(item_id: str, req: EditBidRequest):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()
    try:
        await _modify_with_cache_fallback(
            db, item_id, Decimal(str(req.max_bid)),
            req.bid_offset, req.snipe_group,
        )
    except GixenSnipeNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in Gixen") from e
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except requests.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Gixen HTTP error: {e}") from e

    update_bid(db, item_id, req.max_bid, req.bid_offset, req.snipe_group)

    row = get_bid_by_item_id(db, item_id)
    if row is None:
        # Gixen accepted the modify, so this snipe lives there — but our DB
        # has no row, meaning the snipe was added via Gixen's web UI and we
        # haven't ingested it yet. Run one sync (which has the web-added
        # insert path) so the response shape matches every other PATCH.
        async with _api_lock:
            await _sync_gixen(db, _api_client)
        # _sync_gixen ingests with the snipe's existing max_bid from Gixen,
        # but we want the user-supplied value to win. Re-apply locally.
        update_bid(db, item_id, req.max_bid, req.bid_offset, req.snipe_group)
        row = get_bid_by_item_id(db, item_id)
        if row is None:
            raise HTTPException(
                status_code=500,
                detail=f"Item {item_id} not in DB after sync — Gixen state unexpectedly empty",
            )
    return dict(row)


@app.delete("/api/bids/{item_id}")
async def api_remove_bid(item_id: str):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()
    try:
        await _remove_with_cache_fallback(db, item_id)
    except GixenSnipeNotFoundError:
        # BUI-164: the item is already absent from Gixen's list — the desired
        # end state of a remove (snipe gone) is already true. Fall through to
        # tombstone the local row instead of 404ing and leaving it PENDING,
        # where it lingers in /api/snipes and, if never locally sniped, could
        # still be re-fired by the local sniper.
        logger.info(
            "remove: %s already absent from Gixen — tombstoning REMOVED", item_id,
        )
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    delete_bid(db, item_id)
    # Response status mirrors the soft-delete tombstone, renamed PURGED ->
    # REMOVED in BUI-49. No in-repo consumer string-matches the old value.
    return {"item_id": item_id, "status": "REMOVED"}


@app.post("/api/sync")
async def api_sync():
    """Pull live Gixen state and insert any web-added snipes missing from the DB."""
    db = _get_db()
    async with _api_lock:
        snipes = await _sync_gixen(db, _api_client)
    return {"synced": len(snipes)}


@app.post("/api/purge")
async def api_purge(req: PurgeRequest):
    db = _get_db()

    # 1. Sync first to capture any outstanding WON/LOST transitions;
    #    reuse the snipes list for sibling detection (avoids a second Gixen call)
    async with _api_lock:
        gixen_snipes = await _sync_gixen(db, _api_client)

    # 2. Detect siblings server-side (client may also pass explicit IDs)
    server_siblings = find_sibling_cleanup_targets(gixen_snipes)
    all_sibling_ids = list({s["item_id"] for s in server_siblings} | set(req.sibling_ids))

    # 3. Collect completed bid item_ids before purging Gixen
    completed = db.execute(
        "SELECT item_id FROM bids WHERE status IN ('WON','LOST','ENDED','FAILED')"
    ).fetchall()
    completed_ids = [r["item_id"] for r in completed]

    # 4. Purge completed on Gixen
    try:
        async with _api_lock:
            await asyncio.to_thread(_api_client.purge_completed)
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    # 5. Mark completed bids with the soft-delete tombstone (REMOVED) in DB
    mark_bids_purged(db, completed_ids)

    # 6. Remove sibling snipes (best-effort)
    removed = 0
    for sibling_id in all_sibling_ids:
        try:
            async with _api_lock:
                await asyncio.to_thread(_api_client.remove_snipe, sibling_id)
            delete_bid(db, sibling_id)
            removed += 1
        except GixenError:
            pass

    return {"purged_completed": len(completed_ids), "removed_siblings": removed}
