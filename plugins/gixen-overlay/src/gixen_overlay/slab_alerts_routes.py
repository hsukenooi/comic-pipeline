"""BUI-1419: `/api/comics/slab-alerts*`.

A separate router (same `LedgerRoute` class as `routes.py`, like
`purchases_routes.py`) so these endpoints do not grow `routes.py`.
`plugin.register_routes` includes it. Storage lives in
`gixen_overlay.slab_alerts`. `ebay-slab-alerts` writes here; BUI-1420's
dashboard reads it. Nothing here bids, buys, or sends a message.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from gixen_overlay.ledger import LedgerRoute
from gixen_overlay.slab_alerts import list_slab_alerts, record_ratings, upsert_slab_alerts
from server.db import write_transaction
from server.main import _get_db_path, _write_locked

router = APIRouter(route_class=LedgerRoute)

_ITEM_ID_RE = re.compile(r"^\d{6,20}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _check_item_id(v: str) -> str:
    v = v.strip()
    if not _ITEM_ID_RE.match(v):
        raise ValueError("item_id must be a numeric eBay item id")
    return v


def _check_iso(v: str | None) -> str | None:
    if v is not None and not _ISO_RE.match(v):
        raise ValueError("timestamps must be ISO 8601")
    return v


class SlabAlertIn(BaseModel):
    """One copy. Only the fields sent change (`model_fields_set`), so an
    observation-only write never clears the alert state."""
    item_id: str
    comic_id: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=300)
    grade: float | None = Field(default=None, ge=0.5, le=10.0)
    listing_type: str | None = Field(default=None, max_length=20)
    best_offer: bool | None = None
    price: float | None = Field(default=None, ge=0)
    shipping: float | None = Field(default=None, ge=0)
    expected: float | None = Field(default=None, ge=0)
    market: float | None = Field(default=None, gt=0)
    confidence: str | None = Field(default=None, max_length=20)
    end_date: str | None = Field(default=None, max_length=40)
    first_seen_at: str | None = Field(default=None, max_length=40)
    last_seen_at: str | None = Field(default=None, max_length=40)
    ended_at: str | None = Field(default=None, max_length=40)
    last_alerted_at: str | None = Field(default=None, max_length=40)
    alerted_expected: float | None = Field(default=None, ge=0)
    alerted_best_offer: bool | None = None
    alerted_last_day: bool | None = None
    dropped_reason: str | None = Field(default=None, max_length=40)
    dropped_at: str | None = Field(default=None, max_length=40)
    dropped_price: float | None = Field(default=None, ge=0)

    _item = field_validator("item_id")(_check_item_id)
    _ts = field_validator(
        "end_date", "first_seen_at", "last_seen_at", "ended_at", "last_alerted_at",
        "dropped_at",
    )(_check_iso)


class SlabAlertBatch(BaseModel):
    copies: list[SlabAlertIn] = Field(max_length=5000)


class RatingIn(BaseModel):
    item_id: str
    rating: Literal["act", "pass"]
    rated_at: str | None = Field(default=None, max_length=40)

    _item = field_validator("item_id")(_check_item_id)
    _ts = field_validator("rated_at")(_check_iso)


class RatingBatch(BaseModel):
    ratings: list[RatingIn] = Field(max_length=1000)


@router.post("/api/comics/slab-alerts")
async def api_upsert_slab_alerts(req: SlabAlertBatch) -> dict[str, Any]:
    """Batch upsert keyed by `item_id`. Idempotent. 422 when a new item has
    no `comic_id`. Returns `{inserted, updated}`."""
    rows = [c.model_dump(include=c.model_fields_set | {"item_id"}) for c in req.copies]
    try:
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                return upsert_slab_alerts(wconn, rows)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/comics/slab-alerts")
async def api_list_slab_alerts(
    request: Request,
    comic_id: int | None = None,
    open: bool = False,
    alerted: bool = False,
) -> dict[str, Any]:
    """Copies newest first. `?comic_id=`, `?open=true` (not ended), and
    `?alerted=true` (alerted at least once) filter."""
    rows = list_slab_alerts(
        request.app.state.db, comic_id=comic_id, open_only=open, alerted_only=alerted
    )
    return {"count": len(rows), "copies": rows}


@router.post("/api/comics/slab-alerts/ratings")
async def api_rate_slab_alerts(req: RatingBatch) -> dict[str, Any]:
    """Record Act/Pass ratings from the Telegram buttons. Idempotent.
    Returns `{recorded, unknown}` (unknown item ids are not stored)."""
    rows = [r.model_dump() for r in req.ratings]
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            return record_ratings(wconn, rows)
