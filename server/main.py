"""Gixen backend server — FastAPI app with SQLite storage and Gixen proxy."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from gixen_client import GixenClient, GixenError, GixenSnipeNotFoundError, find_sibling_cleanup_targets
from server.db import (
    DB_PATH, init_db, upsert_comic, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged,
    set_auction_end_time, get_bids_ready_to_snipe, set_local_snipe_result,
)
import ebay_bidder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

_db: sqlite3.Connection | None = None
_api_client: GixenClient | None = None
_sync_client: GixenClient | None = None
_api_lock: asyncio.Lock | None = None
_bidder: ebay_bidder.EbayBidder | None = None
_cached_snipes: list = []          # last successful Gixen snipe list
_cached_snipes_at: float = 0.0     # monotonic timestamp of that fetch


def _get_db() -> sqlite3.Connection:
    assert _db is not None, "DB not initialized"
    return _db


# ---------------------------------------------------------------------------
# Sync helpers (defined before lifespan so api_purge can reference them)
# ---------------------------------------------------------------------------

_TERMINAL_GIXEN_STATUSES: frozenset[str] = frozenset({"WON", "LOST", "FAILED", "ENDED"})

SYNC_INTERVAL = int(os.getenv("GIXEN_SYNC_INTERVAL", "600"))
_SYNC_BACKOFF_MAX = 3600  # cap backoff at 1 hour


async def _sync_gixen(db: sqlite3.Connection, client: GixenClient) -> list:
    """Pull current Gixen state and update DB bid statuses. Returns snipes list."""
    global _cached_snipes, _cached_snipes_at
    try:
        snipes = await asyncio.to_thread(client.list_snipes)
    except GixenError as e:
        logger.warning("_sync_gixen: GixenError (suppressed): %s", e)
        return []  # sync is best-effort; don't crash if Gixen is down

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    gixen_item_ids = {s["item_id"] for s in snipes}

    for snipe in snipes:
        gixen_status = snipe.get("status", "")
        if gixen_status in _TERMINAL_GIXEN_STATUSES:
            current_bid = snipe.get("current_bid", "")
            winning_bid = None
            if current_bid:
                try:
                    winning_bid = float(current_bid.split()[0])
                except (ValueError, IndexError):
                    pass
            update_bid_status(db, snipe["item_id"], gixen_status, winning_bid, now)

        if snipe.get("seller"):
            db.execute(
                "UPDATE bids SET seller=? WHERE item_id=? AND status='PENDING'",
                (snipe["seller"], snipe["item_id"]),
            )

        # Refresh auction_end_at from Gixen's relative time on every sync
        time_to_end = snipe.get("time_to_end", "")
        if time_to_end and time_to_end.upper() != "ENDED":
            delta = _parse_time_to_end(time_to_end)
            if delta is not None:
                end_time = (now_dt + delta).isoformat()
                set_auction_end_time(db, snipe["item_id"], end_time)

    db.commit()

    pending_bids = get_pending_bids(db)
    vanished = [b["item_id"] for b in pending_bids if b["item_id"] not in gixen_item_ids]
    mark_bids_purged(db, vanished)

    _cached_snipes = snipes
    _cached_snipes_at = datetime.now(timezone.utc).timestamp()
    return snipes


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

        # Exponential backoff: 10min, 20min, 40min, ..., capped at 1 hour
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _api_client, _sync_client, _api_lock, _bidder
    if env_file := os.getenv("ENV_FILE"):
        load_dotenv(env_file)
    db_path = Path(os.getenv("DB_PATH", str(DB_PATH)))
    _db = init_db(db_path)
    _api_client = GixenClient()
    _sync_client = GixenClient()
    _api_lock = asyncio.Lock()

    sync_task = None
    sniper_task = None
    if os.getenv("GIXEN_SYNC_ENABLED", "true") != "false":
        sync_task = asyncio.create_task(_sync_loop())
    if os.getenv("LOCAL_SNIPER_ENABLED", "true") != "false":
        _bidder = ebay_bidder.EbayBidder()
        await _bidder.start()
        sniper_task = asyncio.create_task(_sniper_loop())

    yield

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


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
    global _cached_snipes, _cached_snipes_at
    db = _get_db()

    # Try a live fetch; fall back to the last successful snapshot if Gixen is down.
    try:
        async with _api_lock:
            gixen_snipes = await asyncio.to_thread(_api_client.list_snipes)
        _cached_snipes = gixen_snipes
        _cached_snipes_at = datetime.now(timezone.utc).timestamp()
    except GixenError as e:
        if _cached_snipes:
            age = int(datetime.now(timezone.utc).timestamp() - _cached_snipes_at)
            logger.warning("api_get_snipes: Gixen unavailable (%s), returning cached data (%ds old)", e, age)
            gixen_snipes = _cached_snipes
        else:
            raise HTTPException(status_code=503, detail=str(e))

    db_rows = db.execute("""
        SELECT b.*, c.title AS comic_title, c.issue AS comic_issue,
               c.year AS comic_year, c.grade AS comic_grade,
               c.fmv_low, c.fmv_high, c.fmv_comps,
               c.fmv_confidence, c.fmv_notes
        FROM bids b
        LEFT JOIN comics c ON b.comic_id = c.id
        WHERE b.status = 'PENDING'
    """).fetchall()
    db_by_item = {row["item_id"]: dict(row) for row in db_rows}

    result = []
    for snipe in gixen_snipes:
        merged = dict(snipe)
        db_data = db_by_item.get(snipe["item_id"], {})
        for key in ("fmv_low", "fmv_high", "fmv_comps", "fmv_confidence",
                    "fmv_notes", "comic_title", "comic_issue",
                    "comic_year", "comic_grade", "comic_id"):
            merged[key] = db_data.get(key)
        result.append(merged)

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
        return {"item_id": item_id, "max_bid": req.max_bid, "status": "PENDING"}
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
    return {"item_id": item_id, "status": "PURGED"}


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
