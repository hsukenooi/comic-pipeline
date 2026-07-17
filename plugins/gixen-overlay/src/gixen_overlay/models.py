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
    fmv_flag_reason: str | None = None
    locg_id: int | None = None
    locg_variant_id: int | None = None

    @field_validator("fmv_confidence")
    @classmethod
    def validate_confidence(cls, v: str | None) -> str | None:
        if v is not None and v not in ("high", "medium", "low"):
            raise ValueError("fmv_confidence must be high, medium, or low")
        return v

    @field_validator("fmv_flag_reason")
    @classmethod
    def validate_flag_reason(cls, v: str | None) -> str | None:
        # BUI-132: the BUI-86 needs_manual reasons. Empty string is normalized
        # to None (no flag) so callers can post "" to mean "not flagged".
        if v is not None and v.strip() == "":
            return None
        if v is not None and v not in ("one_sided", "too_wide", "too_sparse"):
            raise ValueError(
                "fmv_flag_reason must be one_sided, too_wide, or too_sparse"
            )
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
    """POST /api/comics/wish-list — append one issue to the wish-list (BUI-92).

    ``force`` (BUI-130) bypasses the already-owned guard. By default the endpoint
    rejects a title already in the collection with 409, because wish-listing an
    owned book is the BUI-122 data-loss trigger; ``force=true`` is the escape
    hatch for the rare intentional case (a different printing/variant).

    ``year`` (BUI-184) is the **per-issue cover year** of the book being
    wish-listed (e.g. 1968 for ``"The Mighty Thor #154"``). Supplying it lets the
    owned-guard's year-gated masthead fallback catch a book stored under its base
    masthead (an owned ``"Thor #154"``). It is OPTIONAL and defaults to omitted —
    when absent, the guard behaves exactly as before. CRITICAL (BUI-129): pass the
    issue's *cover* year, never a series START year (``year_began``), or the
    matcher's per-issue year gate falsely reports owned mid-run issues as
    not-owned and the guard fails open.

    BUI-387: ``year`` is now also PERSISTED on the created wish entry (a separate
    ``year`` field), not only consumed by the add-time owned-guard. The stored
    Cover Year lets the later conflicts audit year-scope this wish so a vintage
    want stops re-flagging against an owned modern volume on every audit. Same
    BUI-129 rule: it must be the issue's cover year, never ``year_began``.
    """

    title: str
    force: bool = False
    year: str | None = None

    @field_validator("title")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must be non-empty")
        return v


class CollectionCheckItem(BaseModel):
    """One (series, issue, [year], [variant]) pair for the batch check.

    Mirrors the query params of the single-item `GET
    /api/comics/collection/check`. ``year`` is the issue's **cover year**
    (never a series start year — the BUI-129 trap); it is gated on
    ``release_date.startswith(year)`` and is optional.
    """

    series: str
    issue: str
    year: str | None = None
    variant: str | None = None

    @field_validator("series", "issue")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("series and issue must be non-empty")
        return v


class CollectionCheckBatchRequest(BaseModel):
    """POST /api/comics/collection/check/batch (BUI-204).

    A list of (series, issue, year?, variant?) pairs checked in one call,
    eliminating the per-issue HTTP fan-out `/comic:wishlist-add` used to do.
    Each item is verified with the exact same matcher the single-item endpoint
    uses, and the per-item result shape is identical (R11 preserved: an
    un-imported store fails the whole call rather than reporting every item
    'not owned').
    """

    items: list[CollectionCheckItem]


class SellerScanSeenRequest(BaseModel):
    """POST /api/comics/seller-scan/seen — mark item_ids as already surfaced (BUI-113)."""

    item_ids: list[str]
    seller: str | None = None


class CollectionWinsSeenRequest(BaseModel):
    """POST /api/comics/collection/record-win/seen — mark win item_ids as processed (BUI-121)."""

    item_ids: list[str]


class RecordWinRequest(BaseModel):
    """POST /api/comics/collection/record-win — append won auctions (BUI-92).

    Each win mirrors the shape /comic:collection-add builds:
    ``{item_id, current_bid, end_date_iso,
       identify_data: {series, issue, year?, variant_text?}}``.
    The list is passed straight to locg-cli's cmd_collection_record_win, which
    owns the Metron series resolution + BUI-34 already-owned dedup.
    """

    wins: list[dict]
