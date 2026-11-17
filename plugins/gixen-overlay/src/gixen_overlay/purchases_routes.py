"""BUI-1415: `/api/comics/purchases*` and `/api/comics/offers*`.

A separate router (same `LedgerRoute` class as `routes.py`, so rejected
requests still land in the rejected-writes ledger) so these endpoints do not
grow `routes.py`. `plugin.register_routes` includes both routers. Storage and
the read-time refinement rules live in `gixen_overlay.purchases`.

Nothing here places an offer, buys, or writes the collection.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from gixen_overlay.ledger import LedgerRoute
from gixen_overlay.purchases import (
    get_offer,
    insert_offer,
    list_offers,
    list_purchases,
    monthly_spend,
    set_purchase_collection_status,
    update_offer,
    upsert_purchases,
)
from server.db import write_transaction
from server.main import _get_db_path, _write_locked

router = APIRouter(route_class=LedgerRoute)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ITEM_ID_RE = re.compile(r"^\d{6,20}$")

PurchaseType = Literal["auction", "buy_it_now", "best_offer", "unknown"]
OfferOutcome = Literal["pending", "accepted", "declined", "countered", "expired", "withdrawn"]


def _check_item_id(v: str) -> str:
    v = v.strip()
    if not _ITEM_ID_RE.match(v):
        raise ValueError("item_id must be a numeric eBay item id")
    return v


class PurchaseIn(BaseModel):
    order_line_item_id: str = Field(min_length=1, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    item_id: str
    transaction_id: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=300)
    seller: str | None = Field(default=None, max_length=100)
    purchased_at: str = Field(min_length=10, max_length=40)
    quantity: int = Field(default=1, ge=1)
    price: float = Field(ge=0)
    shipping: float | None = Field(default=None, ge=0)
    tax: float | None = Field(default=None, ge=0)
    total: float = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    purchase_type: PurchaseType = "unknown"
    cancelled: bool = False
    # None = the client did not say (older wheel); the stored refund is kept.
    refunded: float | None = Field(default=None, ge=0)
    shipping_estimated: bool = False

    _item = field_validator("item_id")(_check_item_id)

    @field_validator("purchased_at")
    @classmethod
    def _iso_date(cls, v: str) -> str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}", v):
            raise ValueError("purchased_at must be an ISO 8601 timestamp")
        return v

    @model_validator(mode="after")
    def _refund_within_total(self) -> "PurchaseIn":
        if self.refunded is not None and self.refunded > self.total + 0.005:
            raise ValueError("refunded cannot exceed the line total")
        return self


class PurchaseBatch(BaseModel):
    purchases: list[PurchaseIn] = Field(max_length=5000)


class CollectionStatusIn(BaseModel):
    status: Literal["pending", "recorded", "skipped"]


class OfferIn(BaseModel):
    item_id: str
    title: str | None = Field(default=None, max_length=300)
    seller: str | None = Field(default=None, max_length=100)
    ask: float | None = Field(default=None, gt=0)
    offer: float = Field(gt=0)
    counteroffer: float | None = Field(default=None, gt=0)
    final_price: float | None = Field(default=None, gt=0)
    outcome: OfferOutcome = "pending"
    notes: str | None = Field(default=None, max_length=1000)
    offered_at: str | None = Field(default=None, max_length=40)

    _item = field_validator("item_id")(_check_item_id)

    @field_validator("offered_at")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^\d{4}-\d{2}-\d{2}", v):
            raise ValueError("offered_at must be an ISO 8601 timestamp")
        return v


class OfferPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    seller: str | None = Field(default=None, max_length=100)
    ask: float | None = Field(default=None, gt=0)
    offer: float | None = Field(default=None, gt=0)
    counteroffer: float | None = Field(default=None, gt=0)
    final_price: float | None = Field(default=None, gt=0)
    outcome: OfferOutcome | None = None
    notes: str | None = Field(default=None, max_length=1000)


def _check_month(month: str | None) -> None:
    if month is not None and not _MONTH_RE.match(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")


@router.post("/api/comics/purchases")
async def api_upsert_purchases(req: PurchaseBatch) -> dict[str, Any]:
    """Batch upsert keyed by `order_line_item_id`. A re-pull of the same
    window is idempotent: known rows are refreshed, never duplicated.
    Returns `{inserted, updated}`."""
    rows = [p.model_dump() for p in req.purchases]
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            return upsert_purchases(wconn, rows)


@router.get("/api/comics/purchases")
async def api_list_purchases(
    request: Request,
    month: str | None = None,
    collection_status: Literal["pending", "recorded", "skipped", "snipe_win"] | None = None,
) -> dict[str, Any]:
    """Purchases newest first. `?month=YYYY-MM` (UTC) and
    `?collection_status=` filter. `purchase_type` and `collection_status`
    are refined at read time (see `gixen_overlay.purchases`)."""
    _check_month(month)
    rows = list_purchases(
        request.app.state.db, month=month, collection_status=collection_status
    )
    return {"count": len(rows), "purchases": rows}


@router.get("/api/comics/purchases/spend")
async def api_purchase_spend(
    request: Request, months: int | None = Query(default=None, ge=1, le=120)
) -> dict[str, Any]:
    """Monthly spend (price + shipping + tax), newest month first, cancelled
    line items excluded. `gross` is what was charged, `refunded` what eBay
    gave back, `total` the net (gross - refunded). `shipping_estimated_count`
    counts lines whose shipping was allocated from the order or is missing. `?months=N` keeps the newest N."""
    return {"months": monthly_spend(request.app.state.db, months=months)}


@router.post("/api/comics/purchases/{order_line_item_id}/collection-status")
async def api_set_purchase_collection_status(
    order_line_item_id: str, req: CollectionStatusIn
) -> dict[str, Any]:
    """Store `pending` / `recorded` / `skipped` by hand (for a purchase that
    is not a comic, or one recorded outside the record-win flow). 404 when
    the line item is unknown."""
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            result = set_purchase_collection_status(wconn, order_line_item_id, req.status)
    if result is None:
        raise HTTPException(status_code=404, detail=f"purchase {order_line_item_id} not found")
    return result


@router.post("/api/comics/offers", status_code=201)
async def api_add_offer(req: OfferIn) -> dict[str, Any]:
    """Log one offer by hand (`source='manual'`). Records only; never sends
    an offer to eBay."""
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            return insert_offer(wconn, req.model_dump())


@router.patch("/api/comics/offers/{offer_id}")
async def api_update_offer(offer_id: int, req: OfferPatch) -> dict[str, Any]:
    """Record a counteroffer, final price, or outcome. Only fields present
    in the body change; an explicit `null` clears an optional field."""
    changes = {k: getattr(req, k) for k in req.model_fields_set}
    if changes.get("offer", 0) is None or changes.get("outcome", "x") is None:
        raise HTTPException(status_code=422, detail="offer and outcome cannot be cleared")
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            result = update_offer(wconn, offer_id, changes)
    if result is None:
        raise HTTPException(status_code=404, detail=f"offer {offer_id} not found")
    return result


@router.get("/api/comics/offers")
async def api_list_offers(
    request: Request, item_id: str | None = None, seller: str | None = None
) -> dict[str, Any]:
    rows = list_offers(request.app.state.db, item_id=item_id, seller=seller)
    return {"count": len(rows), "offers": rows}


@router.get("/api/comics/offers/{offer_id}")
async def api_get_offer(offer_id: int, request: Request) -> dict[str, Any]:
    row = get_offer(request.app.state.db, offer_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"offer {offer_id} not found")
    return row
