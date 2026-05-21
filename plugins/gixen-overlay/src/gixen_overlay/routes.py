"""Comic FastAPI routes for the gixen-overlay plugin."""
from __future__ import annotations

import logging
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
    check_reconciliation_conflict,
    ReconciliationConflictError,
)
from gixen_overlay.locg_lookup import resolve_year_and_locg
from gixen_overlay.models import UpsertComicRequest, LocgLinkRequest
from gixen_overlay.title_parser import parse_title
from server.db import get_bid_by_item_id
from server.main import _ensure_fresh_sync, _iso_to_relative, _spawn_fallback_task


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_NUMERIC_RE = re.compile(r"[^0-9.]")

logger = logging.getLogger(__name__)

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
    try:
        comic_id = upsert_comic(
            db,
            title=req.title,
            issue=req.issue,
            year=req.year,
            locg_id=req.locg_id,
            locg_variant_id=req.locg_variant_id,
        )
    except ReconciliationConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
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
            try:
                target_comic_id = upsert_comic(
                    db,
                    title=primary["title"],
                    issue=req.issue,
                    year=primary["year"],
                )
            except ReconciliationConflictError as e:
                raise HTTPException(status_code=409, detail=str(e))
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


def _parse_current_bid(value: str | None) -> float | None:
    """Extract a numeric value from a cached_current_bid string ('10.00 USD' -> 10.0).

    Mirrors the JS `parseAmt` in server/static/index.html so server-side
    value_pct math sees the same numbers the client does. Non-negative
    inputs only — the regex strips minus signs along with everything else,
    so this would silently sign-flip negative values. eBay current bids are
    always non-negative; do not reuse this for refund/credit fields.
    """
    if value is None:
        return None
    cleaned = _NUMERIC_RE.sub("", str(value))
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _build_comics_row(row):
    """Shape one joined snipe row into the /api/comics/{snipes,history} response.

    `row` is the sqlite3.Row produced by the JOIN query below — base `bids.*`
    columns plus the aggregates `primary_grade`, `fmv_low_sum`, `fmv_high_sum`,
    `lot_count`, `fmv_low_null_count`, `fmv_high_null_count`.
    """
    item = dict(row)
    lot_count = item["lot_count"] or 0
    needs_linking = lot_count == 0

    # FMV aggregation rules:
    # - unlinked: both null
    # - lot (N>=2): null both when any component is unpriced — avoids silent
    #   understatement (SQLite SUM ignores NULLs and would produce a partial sum)
    # - single comic (N==1): keep whatever bound exists; value_pct guards
    #   against the partial-bound case separately
    if needs_linking:
        fmv_low = None
        fmv_high = None
    elif lot_count >= 2 and (item["fmv_low_null_count"] or item["fmv_high_null_count"]):
        fmv_low = None
        fmv_high = None
    else:
        fmv_low = item["fmv_low_sum"]
        fmv_high = item["fmv_high_sum"]

    max_bid_numeric = item["max_bid"]
    current_bid_numeric = _parse_current_bid(item.get("cached_current_bid"))

    # value_pct: only meaningful for single-comic linked rows with both bounds.
    value_pct = None
    if (
        lot_count == 1
        and fmv_low is not None
        and fmv_high is not None
        and current_bid_numeric is not None
    ):
        midpoint = (fmv_low + fmv_high) / 2
        if midpoint > 0:
            value_pct = current_bid_numeric / midpoint * 100

    end_date_iso = item.get("auction_end_at")
    return {
        # Base /api/snipes shape — preserved so JS can share render helpers.
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
        # Raw numerics so JS doesn't need to re-parse for math.
        "max_bid_numeric": max_bid_numeric,
        "current_bid_numeric": current_bid_numeric,
        # Comic enrichment.
        "cond_grade": item["primary_grade"],
        "cond_extra_count": max(0, lot_count - 1),
        "fmv_low": fmv_low,
        "fmv_high": fmv_high,
        "value_pct": value_pct,
        "lot_count": lot_count,
        "needs_linking": needs_linking,
    }


_COMICS_AGGREGATES = """
    MAX(CASE WHEN bf.is_primary = 1 THEN f.grade END) AS primary_grade,
    SUM(f.low) AS fmv_low_sum,
    SUM(f.high) AS fmv_high_sum,
    COUNT(bf.fmv_id) AS lot_count,
    SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.low IS NULL THEN 1 ELSE 0 END) AS fmv_low_null_count,
    SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.high IS NULL THEN 1 ELSE 0 END) AS fmv_high_null_count
"""


@router.get("/api/comics/snipes")
async def api_comics_snipes(request: Request):
    """Active snipes joined with comic enrichment.

    Same pull-on-visit + fallback semantics as gixen-cli's /api/snipes:
    triggers _ensure_fresh_sync (deduped within _SYNC_TTL) and spawns the
    fallback task, then returns DB rows.
    """
    await _ensure_fresh_sync()
    _spawn_fallback_task()

    db = request.app.state.db
    rows = db.execute(f"""
        SELECT b.*, {_COMICS_AGGREGATES}
        FROM bids b
        LEFT JOIN bid_fmvs bf ON bf.bid_id = b.id
        LEFT JOIN fmv f ON f.id = bf.fmv_id
        WHERE b.status != 'PURGED'
        GROUP BY b.id
        ORDER BY b.added_at DESC
    """).fetchall()
    return [_build_comics_row(r) for r in rows]


@router.get("/api/comics/history")
async def api_comics_history(request: Request):
    """Recently-ended snipes (past 7 days) joined with comic enrichment.

    Mirrors /api/history's filter (auction_end_at within 7 days OR resolved_at
    fallback for snipes without an end-date) and MAX(id) per item_id dedup,
    so a re-added snipe appears once.
    """
    db = request.app.state.db
    rows = db.execute(f"""
        SELECT b.*, {_COMICS_AGGREGATES}
        FROM bids b
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
        LEFT JOIN bid_fmvs bf ON bf.bid_id = b.id
        LEFT JOIN fmv f ON f.id = bf.fmv_id
        GROUP BY b.id
        ORDER BY COALESCE(b.auction_end_at, b.resolved_at) DESC
    """).fetchall()
    return [_build_comics_row(r) for r in rows]


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
        # Year is opportunistic post-PER-98. LOCG fallback enriches when it works;
        # absence no longer blocks linking. Multi-issue lots: resolved year (if any)
        # applies to all issues — same-era run is the most likely identity. Any
        # LOCG exception (network, Cloudflare, Playwright crash) is swallowed: the
        # bid continues to link with year=None rather than 500-ing the whole batch.
        primary_resolution = None
        if year is None:
            try:
                primary_resolution = resolve_year_and_locg(parsed.series, issues[0])
            except Exception as e:
                logger.warning("resolve_year_and_locg raised for item %s: %s", item_id, e)
                primary_resolution = None
            if primary_resolution is not None:
                year = primary_resolution.year

        # Pre-check every issue in the lot before any writes — upsert_comic
        # commits per-call, so a mid-loop conflict on idx=1 would leave idx=0's
        # writes durably committed as orphan rows. See PER-98 todo 004.
        conflict_reason: str | None = None
        for issue in issues:
            conflict_reason = check_reconciliation_conflict(db, parsed.series, issue, year)
            if conflict_reason is not None:
                break
        if conflict_reason is not None:
            skipped.append({
                "item_id": item_id,
                "reason": f"reboot conflict (manual disambiguation required): {conflict_reason}",
            })
            continue

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
