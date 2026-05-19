"""Comic FastAPI routes for the gixen-overlay plugin."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from gixen_overlay.db import (
    upsert_comic,
    list_comics,
    get_primary_comic_for_bid,
    link_comic_to_bid,
)
from gixen_overlay.models import UpsertComicRequest, LocgLinkRequest
from gixen_overlay.title_parser import parse_title
from server.db import get_bid_by_item_id


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}

router = APIRouter()


@router.get("/v2/comics")
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


@router.post("/api/bids/{item_id}/comics/locg")
async def api_link_locg(item_id: str, req: LocgLinkRequest, request: Request):
    """Persist a resolved LOCG ID against a specific comic in a bid's set.

    Without `issue`: target the bid's primary comic (`bids.comic_id`).
    With `issue`: find a comic in the bid's junction matching that issue;
    if missing, auto-upsert one using the primary's series/year.
    """
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = request.app.state.db

    bid_row = get_bid_by_item_id(db, item_id)
    if bid_row is None:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in DB")

    target_comic_id: int | None = None

    if req.issue is not None:
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


@router.post("/api/extract-comics")
async def api_extract_comics(request: Request):
    """Parse cached eBay titles for unlinked bids and link them to comics.

    Idempotent: skips bids that already have comic_id set.
    """
    db = request.app.state.db

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

        if not parsed.series:
            skipped.append({"item_id": item_id, "reason": "no series extracted"})
            continue
        issues = parsed.issues or ([parsed.issue] if parsed.issue else [])
        if not issues:
            skipped.append({"item_id": item_id, "reason": "no issue extracted"})
            continue
        if parsed.year is None:
            skipped.append({"item_id": item_id, "reason": "no year extracted"})
            continue

        try:
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
