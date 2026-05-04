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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from gixen_client import GixenClient, GixenError, GixenSnipeNotFoundError, find_sibling_cleanup_targets
from server.db import (
    DB_PATH, init_db, upsert_comic, list_comics, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged, cache_gixen_data,
    set_auction_end_time, get_bids_ready_to_snipe, set_local_snipe_result,
    link_comic_to_bid, get_comics_for_bid, get_primary_comic_for_bid,
)
from server.title_parser import parse_title
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
        if gixen_status in _TERMINAL_GIXEN_STATUSES:
            current_bid = snipe.get("current_bid", "")
            winning_bid = None
            if current_bid:
                try:
                    winning_bid = float(current_bid.split()[0])
                except (ValueError, IndexError):
                    pass
            update_bid_status(
                db, iid, gixen_status, winning_bid, now,
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

    # Insert any Gixen snipes not yet in the DB (e.g. added via web UI)
    existing_ids = {b["item_id"] for b in get_pending_bids(db)}
    for snipe in snipes:
        if snipe["item_id"] not in existing_ids and snipe.get("status", "") not in _TERMINAL_GIXEN_STATUSES:
            try:
                max_bid = float(snipe.get("max_bid") or 0)
            except (ValueError, TypeError):
                max_bid = 0.0
            insert_bid(
                db, snipe["item_id"], max_bid, None,
                int(snipe.get("bid_offset", 6)),
                int(snipe.get("snipe_group", 0)),
                snipe.get("seller"),
            )
            logger.info("_sync_gixen: inserted web-added snipe %s", snipe["item_id"])

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
            # Includes both PENDING (auction ended but no terminal status
            # captured yet) and ENDED (vanished from Gixen, flipped by
            # _sync_gixen). Excludes PURGED / WON / LOST / FAILED. Once
            # winning_bid is set the row exits this set.
            rows = db.execute(
                """
                SELECT item_id, max_bid FROM bids
                WHERE status IN ('PENDING', 'ENDED')
                  AND auction_end_at IS NOT NULL
                  AND auction_end_at <= ?
                  AND winning_bid IS NULL
                """,
                (now_iso,),
            ).fetchall()

            if not rows:
                return

            failures = 0
            for row in rows:
                iid = row["item_id"]
                ebay = await asyncio.to_thread(_fetch_ebay_item_sync, iid)
                if not ebay:
                    failures += 1
                    await asyncio.sleep(1.5)
                    continue

                final_amount: float | None = None
                price = ebay.get("current_price")
                if price:
                    try:
                        final_amount = float(str(price).lstrip("$").strip())
                    except (ValueError, TypeError):
                        final_amount = None

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

                # Heuristic: 0 < final_price <= our max_bid → our snipe would
                # have outbid; > max → we lost. Still imperfect at the boundary
                # (eBay's reported price excludes our offset bump) but strictly
                # better than the original "WON if anything <= max" logic.
                inferred_status = "WON" if final_amount <= float(row["max_bid"]) else "LOST"
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
    _api_client = GixenClient()
    _api_lock = asyncio.Lock()
    _sync_lock = asyncio.Lock()
    _ebay_fallback_lock = asyncio.Lock()

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


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UpsertComicRequest(BaseModel):
    title: str
    issue: str
    year: int
    grade: float | None = None
    fmv_low: float | None = None
    fmv_high: float | None = None
    fmv_comps: int | None = None
    fmv_confidence: str | None = None
    fmv_notes: str | None = None
    locg_id: int | None = None
    locg_variant_id: int | None = None

    @field_validator("fmv_confidence")
    @classmethod
    def validate_confidence(cls, v: str | None) -> str | None:
        if v is not None and v not in ("high", "medium", "low"):
            raise ValueError("fmv_confidence must be high, medium, or low")
        return v


class AddBidRequest(BaseModel):
    item_id: str
    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0
    comic: str | None = None
    issue: str | None = None
    year: int | None = None
    grade: float | None = None
    fmv_low: float | None = None
    fmv_high: float | None = None
    fmv_comps: int | None = None
    fmv_confidence: str | None = None
    fmv_notes: str | None = None
    locg_id: int | None = None
    locg_variant_id: int | None = None

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

    @field_validator("fmv_confidence")
    @classmethod
    def validate_confidence(cls, v: str | None) -> str | None:
        if v is not None and v not in ("high", "medium", "low"):
            raise ValueError("fmv_confidence must be high, medium, or low")
        return v


class EditBidRequest(BaseModel):
    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0
    locg_id: int | None = None
    locg_variant_id: int | None = None

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v


class PurgeRequest(BaseModel):
    sibling_ids: list[str] = []

    @field_validator("sibling_ids")
    @classmethod
    def validate_sibling_ids(cls, v: list[str]) -> list[str]:
        for item_id in v:
            if not re.match(r"^\d+$", item_id):
                raise ValueError(f"sibling_ids contains non-numeric value: {item_id}")
        return v


class LocgLinkRequest(BaseModel):
    locg_id: int
    locg_variant_id: int | None = None
    issue: str | None = None  # if set, target a specific issue within a lot


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/v1")
def variant_v1():
    return FileResponse(Path(__file__).parent / "static" / "v1-crt.html")


@app.get("/v2")
def variant_v2():
    return FileResponse(Path(__file__).parent / "static" / "v2-tui.html")


@app.get("/v2/comics")
def variant_v2_comics():
    return FileResponse(Path(__file__).parent / "static" / "v2-comics.html")


@app.get("/v2/bids")
def variant_v2_bids():
    return FileResponse(Path(__file__).parent / "static" / "v2-bids.html")


@app.get("/v3")
def variant_v3():
    return FileResponse(Path(__file__).parent / "static" / "v3-amber.html")


@app.get("/static/v2.css")
def static_v2_css():
    return FileResponse(
        Path(__file__).parent / "static" / "v2.css",
        media_type="text/css",
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/comics")
async def api_list_comics(
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
):
    db = _get_db()
    rows = list_comics(db, title=title, issue=issue, year=year, grade=grade)
    return [dict(r) for r in rows]


@app.post("/api/comics")
async def api_upsert_comic(req: UpsertComicRequest):
    db = _get_db()
    comic_id = upsert_comic(
        db,
        title=req.title,
        issue=req.issue,
        year=req.year,
        grade=req.grade,
        fmv_low=req.fmv_low,
        fmv_high=req.fmv_high,
        fmv_comps=req.fmv_comps,
        fmv_confidence=req.fmv_confidence,
        fmv_notes=req.fmv_notes,
        locg_id=req.locg_id,
        locg_variant_id=req.locg_variant_id,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    return dict(row)


@app.post("/api/bids")
async def api_add_bid(req: AddBidRequest):
    db = _get_db()

    comic_id = None
    if req.comic and req.issue and req.year is not None:
        comic_id = upsert_comic(
            db,
            title=req.comic,
            issue=req.issue,
            year=req.year,
            grade=req.grade,
            fmv_low=req.fmv_low,
            fmv_high=req.fmv_high,
            fmv_comps=req.fmv_comps,
            fmv_confidence=req.fmv_confidence,
            fmv_notes=req.fmv_notes,
            locg_id=req.locg_id,
            locg_variant_id=req.locg_variant_id,
        )

    try:
        async with _api_lock:
            await asyncio.to_thread(
                _api_client.add_snipe,
                req.item_id,
                Decimal(str(req.max_bid)),
                bid_offset=req.bid_offset,
                snipe_group=req.snipe_group,
            )
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except requests.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Gixen HTTP error: {e}")

    bid_id = insert_bid(
        db,
        item_id=req.item_id,
        max_bid=req.max_bid,
        comic_id=comic_id,
        bid_offset=req.bid_offset,
        snipe_group=req.snipe_group,
        seller=None,
    )
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    return dict(row)


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
        SELECT b.*, c.title AS comic_title, c.issue AS comic_issue,
               c.year AS comic_year, c.grade AS comic_grade,
               c.fmv_low, c.fmv_high, c.fmv_comps,
               c.fmv_confidence, c.fmv_notes,
               c.locg_id, c.locg_variant_id
        FROM bids b
        LEFT JOIN comics c ON b.comic_id = c.id
        WHERE b.status != 'PURGED'
        ORDER BY b.added_at DESC
    """).fetchall()

    # Second query: every comic linked via bid_comics, keyed by bid_id. This
    # gives us the full lot-aware view (1 bid → N comics) without disturbing
    # the flat fields above (still populated from the primary via bids.comic_id).
    bid_ids = [r["id"] for r in rows]
    comics_by_bid: dict[int, list[dict]] = {bid_id: [] for bid_id in bid_ids}
    if bid_ids:
        placeholders = ",".join("?" * len(bid_ids))
        comic_rows = db.execute(
            f"""
            SELECT bc.bid_id, bc.is_primary, c.id AS comic_id,
                   c.title, c.issue, c.year, c.grade,
                   c.locg_id, c.locg_variant_id
            FROM bid_comics bc
            JOIN comics c ON c.id = bc.comic_id
            WHERE bc.bid_id IN ({placeholders})
            ORDER BY bc.bid_id, bc.is_primary DESC,
                     CAST(c.issue AS INTEGER), c.issue
            """,
            bid_ids,
        ).fetchall()
        for cr in comic_rows:
            comics_by_bid[cr["bid_id"]].append({
                "comic_id": cr["comic_id"],
                "title": cr["title"],
                "issue": cr["issue"],
                "year": cr["year"],
                "grade": cr["grade"],
                "locg_id": cr["locg_id"],
                "locg_variant_id": cr["locg_variant_id"],
                "is_primary": bool(cr["is_primary"]),
            })

    result = []
    for row in rows:
        item = dict(row)
        end_date_iso = item.get("auction_end_at")
        title = item.get("ebay_title") or item.get("comic_title") or ""
        result.append({
            "item_id": item["item_id"],
            "title": title,
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
            "comic_title": item.get("comic_title"),
            "comic_issue": item.get("comic_issue"),
            "comic_year": item.get("comic_year"),
            "comic_grade": item.get("comic_grade"),
            "fmv_low": item.get("fmv_low"),
            "fmv_high": item.get("fmv_high"),
            "fmv_comps": item.get("fmv_comps"),
            "fmv_confidence": item.get("fmv_confidence"),
            "fmv_notes": item.get("fmv_notes"),
            "comic_id": item.get("comic_id"),
            "locg_id": item.get("locg_id"),
            "locg_variant_id": item.get("locg_variant_id"),
            "local_snipe_at": item.get("local_snipe_at"),
            "local_snipe_result": item.get("local_snipe_result"),
            "comics": comics_by_bid.get(item["id"], []),
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

    # If locg_id / locg_variant_id provided, persist on the linked comic row.
    # COALESCE preserves existing values when only one of the two is supplied.
    if req.locg_id is not None or req.locg_variant_id is not None:
        bid_row = get_bid_by_item_id(db, item_id)
        if bid_row is not None and bid_row["comic_id"] is not None:
            db.execute(
                """
                UPDATE comics
                SET locg_id = COALESCE(?, locg_id),
                    locg_variant_id = COALESCE(?, locg_variant_id)
                WHERE id = ?
                """,
                (req.locg_id, req.locg_variant_id, bid_row["comic_id"]),
            )
            db.commit()

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


@app.post("/api/bids/{item_id}/comics/locg")
async def api_link_locg(item_id: str, req: LocgLinkRequest):
    """Persist a resolved LOCG ID against a specific comic in a bid's set.

    Without `issue`: target the bid's primary comic (`bids.comic_id`).
    With `issue`: find a comic in the bid's junction matching that issue;
    if missing (e.g., the parser only created issue 1 for a 5-issue lot),
    auto-upsert one using the primary's series/year and link as non-primary.
    """
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()

    bid_row = get_bid_by_item_id(db, item_id)
    if bid_row is None:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in DB")

    target_comic_id: int | None = None

    if req.issue is not None:
        # Look for an existing comic at this issue in the bid's junction set.
        match = db.execute(
            """
            SELECT c.id
            FROM bid_comics bc
            JOIN comics c ON c.id = bc.comic_id
            WHERE bc.bid_id = ? AND c.issue = ?
            LIMIT 1
            """,
            (bid_row["id"], req.issue),
        ).fetchone()
        if match:
            target_comic_id = match["id"]
        else:
            # Auto-create: copy series/year from primary, leave grade/FMV null
            # (we don't know per-issue grades for ad-hoc lot expansions).
            primary = get_primary_comic_for_bid(db, bid_row["id"])
            if primary is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Bid {item_id} has no primary comic; cannot infer series/year "
                        "for auto-create. Run extract-comics first or use cli.py add."
                    ),
                )
            target_comic_id = upsert_comic(
                db,
                title=primary["title"],
                issue=req.issue,
                year=primary["year"],
                grade=None,
                fmv_low=None,
                fmv_high=None,
                fmv_comps=None,
                fmv_confidence=None,
                fmv_notes="auto-linked via locg-link",
            )
            link_comic_to_bid(db, bid_row["id"], target_comic_id, is_primary=False)
    else:
        if bid_row["comic_id"] is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bid {item_id} has no primary comic. Pass --issue to target a "
                    "specific issue or run extract-comics first."
                ),
            )
        target_comic_id = bid_row["comic_id"]

    # Update locg_id (and locg_variant_id if provided). COALESCE on the
    # variant so callers that omit it don't clobber an existing value.
    db.execute(
        """
        UPDATE comics
        SET locg_id = ?,
            locg_variant_id = COALESCE(?, locg_variant_id)
        WHERE id = ?
        """,
        (req.locg_id, req.locg_variant_id, target_comic_id),
    )
    db.commit()

    row = db.execute(
        "SELECT id AS comic_id, title, issue, year, grade, locg_id, locg_variant_id "
        "FROM comics WHERE id = ?",
        (target_comic_id,),
    ).fetchone()
    is_primary = (target_comic_id == bid_row["comic_id"])
    return {**dict(row), "is_primary": is_primary}


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
    return {"item_id": item_id, "status": "PURGED"}


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

    # 5. Mark completed bids as PURGED in DB
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


@app.post("/api/extract-comics")
async def api_extract_comics():
    """Parse cached eBay titles for unlinked bids and link them to comics.

    Idempotent: skips bids that already have comic_id set, and reuses existing
    comics rows via upsert_comic. Does NOT call eBay (works only from cached
    ebay_title values). Skips bids without a confidently parseable issue/year.
    """
    db = _get_db()

    rows = db.execute(
        """
        SELECT id, item_id, ebay_title
        FROM bids
        WHERE comic_id IS NULL
          AND ebay_title IS NOT NULL
          AND ebay_title != ''
          AND status != 'PURGED'
        """
    ).fetchall()

    processed = 0
    linked = 0
    skipped: list[dict] = []
    errors: list[dict] = []

    for row in rows:
        processed += 1
        item_id = row["item_id"]
        title = row["ebay_title"]
        try:
            parsed = parse_title(title)
        except Exception as e:
            errors.append({"item_id": item_id, "error": f"parse failed: {e}"})
            continue

        # Required for upsert_comic: title (series), issue, year. Skip if missing.
        if not parsed.series:
            skipped.append({"item_id": item_id, "reason": "no series extracted"})
            continue
        issues = parsed.issues or ([parsed.issue] if parsed.issue else [])
        if not issues:
            skipped.append({"item_id": item_id, "reason": "no issue extracted"})
            continue

        # year is required by comics.UNIQUE(title, issue, year, grade) — using
        # a 0 sentinel for "unknown" causes two unrelated listings with no
        # parseable year to collide and silently overwrite each other's
        # fmv_notes (the ON CONFLICT path). Skip these rather than corrupt.
        # Items can still be linked manually via `cli.py add --year`.
        if parsed.year is None:
            skipped.append({"item_id": item_id, "reason": "no year extracted"})
            continue

        try:
            # Upsert one comic row per issue. First issue becomes primary;
            # mirror to bids.comic_id via link_comic_to_bid(is_primary=True).
            for idx, issue in enumerate(issues):
                comic_id = upsert_comic(
                    db,
                    title=parsed.series,
                    issue=issue,
                    year=parsed.year,
                    grade=parsed.grade,
                    fmv_low=None,
                    fmv_high=None,
                    fmv_comps=None,
                    fmv_confidence=None,
                    fmv_notes=f"auto-linked from eBay title (confidence={parsed.confidence})",
                )
                link_comic_to_bid(db, row["id"], comic_id, is_primary=(idx == 0))
            linked += 1
        except Exception as e:
            errors.append({"item_id": item_id, "error": f"link failed: {e}"})

    return {
        "processed": processed,
        "linked": linked,
        "skipped": skipped,
        "errors": errors,
    }
