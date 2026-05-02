"""Gixen backend server — FastAPI app with SQLite storage and Gixen proxy."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from gixen_client import GixenClient, GixenError, GixenSnipeNotFoundError, find_sibling_cleanup_targets
from server.db import (
    DB_PATH, init_db, upsert_comic, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged, cache_ebay_data,
)

# Import eBay helpers from the sibling project
_EBAY_CLI_DIR = Path.home() / "Projects" / "ebay-cli"
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


def _get_db() -> sqlite3.Connection:
    assert _db is not None, "DB not initialized"
    return _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERMINAL_GIXEN_STATUSES: frozenset[str] = frozenset({"WON", "LOST", "FAILED", "ENDED"})


def _fetch_ebay_item_sync(item_id: str) -> dict | None:
    if not _EBAY_AVAILABLE:
        return None
    try:
        client_id, client_secret, base_url = _ebay_load_config()
        token = _ebay_get_token(client_id, client_secret, base_url)
        data = _ebay_fetch_item(item_id, token, base_url)
        if data:
            return _ebay_parse_item(data)
    except SystemExit:
        logger.warning("_fetch_ebay_item_sync: eBay credentials not configured")
    except Exception as e:
        logger.warning("_fetch_ebay_item_sync %s: %s", item_id, e)
    return None


def _auction_ended(end_date_iso: str | None) -> bool:
    if not end_date_iso:
        return False
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        return dt <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


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


def _ebay_price_to_bid_str(current_price: str | None) -> str | None:
    """Convert eBay '$12.50' format to '12.50 USD' to match Gixen's format."""
    if not current_price:
        return None
    stripped = current_price.lstrip("$").strip()
    try:
        float(stripped)
        return f"{stripped} USD"
    except ValueError:
        return current_price


# ---------------------------------------------------------------------------
# Gixen sync helper (used by api_purge and ended-bid resolution)
# ---------------------------------------------------------------------------

async def _sync_gixen(db: sqlite3.Connection, client: GixenClient) -> list:
    """Pull current Gixen state and update DB bid statuses. Returns snipes list."""
    try:
        snipes = await asyncio.to_thread(client.list_snipes)
    except GixenError as e:
        logger.warning("_sync_gixen: GixenError (suppressed): %s", e)
        return []

    now = datetime.now(timezone.utc).isoformat()
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
            update_bid_status(
                db, snipe["item_id"], gixen_status, winning_bid, now,
                snipe.get("status_mirror"),
            )

    db.commit()

    pending_bids = get_pending_bids(db)
    vanished = [b["item_id"] for b in pending_bids if b["item_id"] not in gixen_item_ids]
    mark_bids_purged(db, vanished)

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


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _api_client, _api_lock
    if env_file := os.getenv("ENV_FILE"):
        load_dotenv(env_file)
    db_path = Path(os.getenv("DB_PATH", str(DB_PATH)))
    _db = init_db(db_path)
    _api_client = GixenClient()
    _api_lock = asyncio.Lock()

    yield

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

@app.get("/")
def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


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
    db = _get_db()

    rows = db.execute("""
        SELECT b.*, c.title AS comic_title, c.issue AS comic_issue,
               c.year AS comic_year, c.grade AS comic_grade,
               c.fmv_low, c.fmv_high, c.fmv_comps, c.fmv_confidence, c.fmv_notes
        FROM bids b
        LEFT JOIN comics c ON b.comic_id = c.id
        WHERE b.status != 'PURGED'
        ORDER BY b.added_at DESC
    """).fetchall()

    items = [dict(r) for r in rows]

    # Fetch live eBay data for all items in parallel
    ebay_results = await asyncio.gather(
        *[asyncio.to_thread(_fetch_ebay_item_sync, item["item_id"]) for item in items],
        return_exceptions=True,
    )

    needs_resolution = []  # PENDING item_ids whose auctions appear ended

    result = []
    for item, ebay in zip(items, ebay_results):
        if isinstance(ebay, BaseException):
            logger.warning("eBay fetch error for %s: %s", item["item_id"], ebay)
            ebay = None

        if ebay:
            cache_ebay_data(db, item["item_id"], ebay["title"],
                            ebay.get("seller"), ebay.get("end_date_iso"))
            title = ebay["title"]
            current_bid = _ebay_price_to_bid_str(ebay.get("current_price"))
            end_date_iso = ebay.get("end_date_iso")
            seller = ebay.get("seller")
        else:
            title = item.get("ebay_title") or item.get("comic_title") or ""
            current_bid = None
            end_date_iso = item.get("auction_end_at")
            seller = item.get("seller")

        time_to_end = _iso_to_relative(end_date_iso)

        if item["status"] == "PENDING" and _auction_ended(end_date_iso):
            needs_resolution.append(item["item_id"])

        result.append({
            "item_id": item["item_id"],
            "title": title,
            "current_bid": current_bid,
            "max_bid": f"{item['max_bid']:.2f}",
            "bid_offset": item["bid_offset"],
            "snipe_group": item["snipe_group"],
            "time_to_end": time_to_end,
            "end_date_iso": end_date_iso,
            "status": item["status"],
            "status_mirror": item.get("status_mirror"),
            "winning_bid": item.get("winning_bid"),
            "seller": seller,
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
        })

    # For PENDING bids whose auctions have ended, resolve status via a single Gixen call
    if needs_resolution:
        try:
            async with _api_lock:
                gixen_snipes = await asyncio.to_thread(_api_client.list_snipes)
            now = datetime.now(timezone.utc).isoformat()
            gixen_by_item = {s["item_id"]: s for s in gixen_snipes}

            for row in result:
                if row["item_id"] not in needs_resolution:
                    continue
                snipe = gixen_by_item.get(row["item_id"])
                if not snipe:
                    continue
                gixen_status = snipe.get("status", "")
                if gixen_status not in _TERMINAL_GIXEN_STATUSES:
                    continue
                current_bid_str = snipe.get("current_bid", "")
                winning_bid = None
                if current_bid_str:
                    try:
                        winning_bid = float(current_bid_str.split()[0])
                    except (ValueError, IndexError):
                        pass
                status_mirror = snipe.get("status_mirror")
                update_bid_status(db, row["item_id"], gixen_status, winning_bid, now, status_mirror)
                row["status"] = gixen_status
                row["status_mirror"] = status_mirror
                if winning_bid is not None:
                    row["winning_bid"] = winning_bid
        except GixenError as e:
            logger.warning("api_get_snipes: Gixen unavailable for status resolution: %s", e)

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
