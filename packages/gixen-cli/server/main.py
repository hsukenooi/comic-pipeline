"""Gixen backend server — FastAPI app with SQLite storage and Gixen proxy."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
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
    GixenAddNotConfirmedError, find_sibling_cleanup_targets,
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
    set_auction_end_time, get_bids_ready_to_snipe, set_local_snipe_result,
)
import ebay_bidder

# Import eBay helpers from the sibling project. Path is overridable via
# EBAY_CLI_PATH so the server isn't pinned to a specific developer's home
# directory layout.
_EBAY_CLI_DIR = Path(os.getenv("EBAY_CLI_PATH", str(Path.home() / "Projects" / "ebay-cli")))
sys.path.insert(0, str(_EBAY_CLI_DIR))
try:
    from ebay_fetch import (  # type: ignore[import]
        load_config as _ebay_load_config,
        get_token as _ebay_get_token,
        fetch_item as _ebay_fetch_item,
        parse_item as _ebay_parse_item,
    )
    _EBAY_AVAILABLE = True
except ImportError as _ebay_import_err:
    _EBAY_AVAILABLE = False

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

if not _EBAY_AVAILABLE:
    logger.warning("ebay_fetch not importable from %s — live eBay data disabled", _EBAY_CLI_DIR)

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
    if not _EBAY_AVAILABLE:
        return None
    if not _ebay_creds_available():
        return None
    try:
        client_id, client_secret, base_url = _ebay_load_config()
        token = _ebay_get_token(client_id, client_secret, base_url)
        data = _ebay_fetch_item(item_id, token, base_url)
        if data:
            return _ebay_parse_item(data)
    except Exception as e:
        logger.warning("_fetch_ebay_item_sync %s: %s", item_id, e)
    return None


def _iso_to_relative(end_date_iso: str | None) -> str:
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
# Gixen sync helper (used by api_purge and ended-bid resolution)
# ---------------------------------------------------------------------------

async def _sync_gixen(db: sqlite3.Connection, client: GixenClient) -> list:
    """Pull current Gixen state and update DB. Returns the snipes list.

    For every snipe Gixen returns, refresh the cached title/seller/current_bid
    on the matching DB row (cache_gixen_data) and apply terminal status
    transitions (WON/LOST/...). Insert new snipes that arrived via Gixen's
    web UI. For PENDING DB rows that have vanished from Gixen's response and
    whose auction_end_at is in the past, flip status to ENDED so the eBay
    fallback can backfill winning_bid. (Vanished-but-still-in-future rows are
    left as PENDING — that's the "user removed via Gixen web UI before
    auction end" case, where we have no signal to act on yet.)
    """
    try:
        snipes = await asyncio.to_thread(client.list_snipes)
    except GixenConnectionError as e:
        # Gixen unreachable at the network layer (BUI-77) — distinct, honest
        # signal so the operator isn't sent chasing credentials.
        logger.warning("_sync_gixen: Gixen unreachable (connectivity, not creds): %s", e)
        return []
    except GixenError as e:
        logger.warning("_sync_gixen: GixenError (suppressed): %s", e)
        return []

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    gixen_item_ids = {s["item_id"] for s in snipes}

    for snipe in snipes:
        iid = snipe["item_id"]

        cache_gixen_data(
            db, iid,
            snipe.get("title") or None,
            snipe.get("seller") or None,
            snipe.get("current_bid") or None,
        )

        gixen_status = snipe.get("status", "")
        time_to_end = snipe.get("time_to_end", "")
        internal_status = _map_terminal_status(gixen_status, time_to_end)
        if internal_status is not None:
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
            update_bid_status(
                db, iid, internal_status, winning_bid, now,
                snipe.get("status_mirror"),
            )

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

    # Vanished + ended → flip to ENDED. The eBay fallback path then picks
    # them up (ENDED rows with NULL winning_bid) and resolves the final
    # selling price when eBay's rate-limit budget allows.
    vanished_ended = db.execute(
        """
        SELECT item_id FROM bids
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
        update_bid_status(db, iid, "ENDED", winning_bid=None, resolved_at=now)
        logger.info(
            "_sync_gixen: %s vanished from Gixen and auction has ended → ENDED",
            iid,
        )

    db.commit()

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
                    int(snipe.get("snipe_group", 0)),
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
                result = await _sync_gixen(db, _sync_client)
                if result:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
        except Exception:
            logger.exception("_sync_loop: unexpected error, continuing")
            consecutive_failures += 1

        # Exponential backoff: SYNC_INTERVAL, 2x, 4x, ..., capped at 1 hour
        delay = min(SYNC_INTERVAL * (2 ** consecutive_failures), _SYNC_BACKOFF_MAX)
        if consecutive_failures:
            logger.warning("_sync_loop: %d consecutive failure(s), sleeping %ds", consecutive_failures, delay)
        await asyncio.sleep(delay)


def _parse_time_to_end(s: str) -> timedelta | None:
    """Parse Gixen relative time string like '1 d, 20 h, 59 m' into a timedelta."""
    total = 0
    for part in s.split(","):
        part = part.strip()
        if m := re.match(r"(\d+)\s*d", part):
            total += int(m.group(1)) * 86400
        elif m := re.match(r"(\d+)\s*h", part):
            total += int(m.group(1)) * 3600
        elif m := re.match(r"(\d+)\s*m", part):
            total += int(m.group(1)) * 60
        elif m := re.match(r"(\d+)\s*s", part):
            total += int(m.group(1))
    return timedelta(seconds=total) if total else None


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
        """
        SELECT item_id, max_bid, local_snipe_result, 0 AS is_purged FROM bids
        WHERE status IN ('PENDING', 'ENDED')
          AND auction_end_at IS NOT NULL
          AND auction_end_at <= ?
          AND winning_bid IS NULL
        UNION ALL
        SELECT item_id, max_bid, local_snipe_result, 1 AS is_purged FROM bids
        WHERE status IN ('PURGED', 'REMOVED')
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
                            "UPDATE bids SET winning_bid = ? WHERE item_id = ? AND status IN ('PURGED', 'REMOVED')",
                            (final_amount, iid),
                        )
                        logger.info(
                            "_run_ebay_fallback: %s (purged) winning_bid=$%.2f",
                            iid, final_amount,
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
        except Exception as e:
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
        raise HTTPException(status_code=503, detail=str(e))
    except requests.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Gixen HTTP error: {e}")


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

    rows = db.execute("""
        SELECT * FROM bids
        WHERE status NOT IN ('PURGED', 'REMOVED')
        ORDER BY added_at DESC
    """).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        end_date_iso = item.get("auction_end_at")
        result.append({
            "item_id": item["item_id"],
            "title": item.get("ebay_title") or None,
            "current_bid": item.get("cached_current_bid"),
            "max_bid": f"{item['max_bid']:.2f} USD",
            "bid_offset": item["bid_offset"],
            "snipe_group": item["snipe_group"],
            "time_to_end": _iso_to_relative(end_date_iso),
            "end_date_iso": end_date_iso,
            "status": item["status"],
            "status_mirror": item.get("status_mirror"),
            "winning_bid": item.get("winning_bid"),
            "seller": item.get("seller"),
            "cached_at": item.get("cached_at"),
            "local_snipe_at": item.get("local_snipe_at"),
            "local_snipe_result": item.get("local_snipe_result"),
        })

    return result


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

    result = []
    for row in rows:
        item = dict(row)
        end_date_iso = item.get("auction_end_at")
        result.append({
            "item_id": item["item_id"],
            "title": item.get("ebay_title") or None,
            "current_bid": item.get("cached_current_bid"),
            "max_bid": f"{item['max_bid']:.2f} USD",
            "bid_offset": item["bid_offset"],
            "snipe_group": item["snipe_group"],
            "time_to_end": _iso_to_relative(end_date_iso),
            "end_date_iso": end_date_iso,
            "status": item["status"],
            "status_mirror": item.get("status_mirror"),
            "winning_bid": item.get("winning_bid"),
            "seller": item.get("seller"),
            "cached_at": item.get("cached_at"),
            "local_snipe_at": item.get("local_snipe_at"),
            "local_snipe_result": item.get("local_snipe_result"),
        })
    return result


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
        })
    return result


@app.patch("/api/bids/{item_id}")
async def api_edit_bid(item_id: str, req: EditBidRequest):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()
    try:
        async with _api_lock:
            await asyncio.to_thread(
                _api_client.modify_snipe,
                item_id,
                Decimal(str(req.max_bid)),
                bid_offset=req.bid_offset,
                snipe_group=req.snipe_group,
            )
    except GixenSnipeNotFoundError:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in Gixen")
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except requests.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Gixen HTTP error: {e}")

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
        async with _api_lock:
            await asyncio.to_thread(_api_client.remove_snipe, item_id)
    except GixenSnipeNotFoundError:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in Gixen")
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))

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
        raise HTTPException(status_code=503, detail=str(e))

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
