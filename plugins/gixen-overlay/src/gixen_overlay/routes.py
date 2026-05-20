"""Comic FastAPI routes for the gixen-overlay plugin."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from gixen_overlay.db import (
    upsert_comic,
    upsert_fmv,
    link_fmv_to_bid,
    get_primary_fmv_for_bid,
    list_comics,
)
from gixen_overlay.locg_lookup import resolve_year_and_locg
from gixen_overlay.models import UpsertComicRequest, LocgLinkRequest
from gixen_overlay.title_parser import parse_title
from server.db import get_bid_by_item_id


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}

router = APIRouter()


@router.get("/comics")
def variant_v2_comics():
    return FileResponse(
        Path(__file__).parent / "static" / "v2-comics.html",
        headers=_NO_CACHE_HEADERS,
    )


@router.get("/api/comics")
async def api_list_comics(
    request: Request,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
):
    db = request.app.state.db
    rows = list_comics(db, title=title, issue=issue, year=year, grade=grade)
    return [dict(r) for r in rows]


@router.post("/api/comics")
async def api_upsert_comic(req: UpsertComicRequest, request: Request):
    db = request.app.state.db
    comic_id = upsert_comic(
        db,
        title=req.title,
        issue=req.issue,
        year=req.year,
        locg_id=req.locg_id,
        locg_variant_id=req.locg_variant_id,
    )
    if req.grade is not None:
        upsert_fmv(
            db,
            comic_id=comic_id,
            grade=req.grade,
            low=req.fmv_low,
            high=req.fmv_high,
            comps=req.fmv_comps,
            confidence=req.fmv_confidence,
            notes=req.fmv_notes,
        )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    return dict(row)


@router.post("/api/bids/{item_id}/comics/locg")
async def api_link_locg(item_id: str, req: LocgLinkRequest, request: Request):
    """Persist a resolved LOCG ID against a specific comic in a bid's set.

    Without `issue`: target the bid's primary fmv's comic (`bid.fmv_id → fmv.comic_id`).
    With `issue`: find an fmv in the bid's junction matching that issue;
    if missing, auto-upsert one using the primary's series/year.
    """
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = request.app.state.db

    bid_row = get_bid_by_item_id(db, item_id)
    if bid_row is None:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in DB")

    target_fmv_id: int | None = None
    target_comic_id: int | None = None

    if req.issue is not None:
        # Find an fmv whose comic has the requested issue, linked to this bid
        match = db.execute(
            """
            SELECT bf.fmv_id, c.id AS comic_id
            FROM bid_fmvs bf
            JOIN fmv f ON f.id = bf.fmv_id
            JOIN comics c ON c.id = f.comic_id
            WHERE bf.bid_id = ? AND c.issue = ?
            LIMIT 1
            """,
            (bid_row["id"], req.issue),
        ).fetchone()
        if match:
            target_fmv_id = match["fmv_id"]
            target_comic_id = match["comic_id"]
        else:
            primary = get_primary_fmv_for_bid(db, bid_row["id"])
            if primary is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Bid {item_id} has no primary fmv; cannot infer series/year "
                        "for auto-create. Run extract-comics first or use cli.py add."
                    ),
                )
            target_comic_id = upsert_comic(
                db,
                title=primary["title"],
                issue=req.issue,
                year=primary["year"],
            )
            # Create an fmv stub at the primary's grade for the lot issue
            new_fmv_id = upsert_fmv(db, target_comic_id, primary["grade"])
            link_fmv_to_bid(db, bid_row["id"], new_fmv_id, is_primary=False)
            target_fmv_id = new_fmv_id
    else:
        primary_fmv_id = bid_row["fmv_id"]
        if primary_fmv_id is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bid {item_id} has no primary fmv. Pass --issue to target a "
                    "specific issue or run extract-comics first."
                ),
            )
        target_fmv_id = primary_fmv_id
        fmv_row = db.execute(
            "SELECT comic_id FROM fmv WHERE id=?", (primary_fmv_id,)
        ).fetchone()
        target_comic_id = fmv_row["comic_id"] if fmv_row else None

    if target_comic_id is None:
        raise HTTPException(status_code=500, detail="Could not resolve target comic")

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
        "SELECT id AS comic_id, title, issue, year, locg_id, locg_variant_id "
        "FROM comics WHERE id = ?",
        (target_comic_id,),
    ).fetchone()
    # is_primary: target fmv matches the bid's primary fmv_id
    is_primary = (target_fmv_id == bid_row["fmv_id"])
    return {**dict(row), "is_primary": is_primary}


@router.post("/api/extract-comics")
async def api_extract_comics(request: Request):
    """Parse cached eBay titles for unlinked bids and link them via fmv_id.

    Idempotent: skips bids that already have fmv_id set.
    """
    db = request.app.state.db

    rows = db.execute(
        """
        SELECT id, item_id, ebay_title
        FROM bids
        WHERE fmv_id IS NULL
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

        if not parsed.series:
            skipped.append({"item_id": item_id, "reason": "no series extracted"})
            continue
        issues = parsed.issues or ([parsed.issue] if parsed.issue else [])
        if not issues:
            skipped.append({"item_id": item_id, "reason": "no issue extracted"})
            continue
        year = parsed.year
        # Year is only resolved for the primary issue. Multi-issue runs (rare
        # for the year-less case in practice) all get the same year.
        primary_resolution = None
        if year is None:
            primary_resolution = resolve_year_and_locg(parsed.series, issues[0])
            if primary_resolution is None:
                skipped.append({
                    "item_id": item_id,
                    "reason": "no year extracted (locg fallback failed)",
                })
                continue
            year = primary_resolution.year

        try:
            for idx, issue in enumerate(issues):
                comic_id = upsert_comic(
                    db,
                    title=parsed.series,
                    issue=issue,
                    year=year,
                    locg_id=primary_resolution.locg_id if (primary_resolution and idx == 0) else None,
                    locg_variant_id=primary_resolution.locg_variant_id if (primary_resolution and idx == 0) else None,
                )
                if parsed.grade is not None:
                    fmv_id = upsert_fmv(
                        db,
                        comic_id=comic_id,
                        grade=parsed.grade,
                        notes=f"auto-linked from eBay title (confidence={parsed.confidence})",
                    )
                    link_fmv_to_bid(db, row["id"], fmv_id, is_primary=(idx == 0))
                # Bids with no parseable grade cannot get an fmv link (fmv.grade NOT NULL)
            linked += 1
        except Exception as e:
            errors.append({"item_id": item_id, "error": f"link failed: {e}"})

    return {
        "processed": processed,
        "linked": linked,
        "skipped": skipped,
        "errors": errors,
    }
