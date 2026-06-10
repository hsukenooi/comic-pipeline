"""Pydantic models for comic overlay API endpoints."""
from __future__ import annotations

from pydantic import BaseModel, field_validator


class UpsertComicRequest(BaseModel):
    title: str
    issue: str
    year: int | None = None
    variant: str | None = None
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


class LocgLinkRequest(BaseModel):
    locg_id: int
    locg_variant_id: int | None = None
    issue: str | None = None  # if set, target a specific issue within a lot


class LinkFmvRequest(BaseModel):
    """Inputs for POST /api/bids/{item_id}/link-fmv.

    Three resolution strategies, tried in order: `comic_id` (internal DB id),
    `locg_id`, then `(series, issue, [year])`. `grade` is always required —
    each strategy narrows by grade. At least one of comic_id/locg_id/series
    must be provided.
    """

    grade: float
    comic_id: int | None = None
    locg_id: int | None = None
    series: str | None = None
    issue: str | None = None
    year: int | None = None


class VerifyItem(BaseModel):
    """One entry of a working list, as fed to POST /api/comics/verify."""

    item_id: str
    grade: float | None = None
    locg_id: int | None = None


class VerifyRequest(BaseModel):
    items: list[VerifyItem]


class WishListAddRequest(BaseModel):
    """POST /api/comics/wish-list — append one issue to the wish-list (BUI-92)."""

    title: str

    @field_validator("title")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must be non-empty")
        return v


class SellerScanSeenRequest(BaseModel):
    """POST /api/comics/seller-scan/seen — mark item_ids as already surfaced (BUI-113)."""

    item_ids: list[str]
    seller: str | None = None


class RecordWinRequest(BaseModel):
    """POST /api/comics/collection/record-win — append won auctions (BUI-92).

    Each win mirrors the shape /comic:collection-add builds:
    ``{item_id, current_bid, end_date_iso,
       identify_data: {series, issue, year?, variant_text?}}``.
    The list is passed straight to locg-cli's cmd_collection_record_win, which
    owns the Metron series resolution + BUI-34 already-owned dedup.
    """

    wins: list[dict]
