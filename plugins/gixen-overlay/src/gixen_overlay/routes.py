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
    sweep_orphan_yearless_comics,
)
from gixen_overlay.locg_lookup import resolve_year_and_locg
from gixen_overlay.models import UpsertComicRequest, LocgLinkRequest, LinkFmvRequest, VerifyRequest
from gixen_overlay.title_parser import parse_title
from server.db import get_bid_by_item_id
from server.main import _ensure_fresh_sync, _iso_to_relative, _spawn_fallback_task


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_NUMERIC_RE = re.compile(r"[^0-9.]")

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
    locg_id: int | None = None,
    max_age_days: float | None = None,
):
    """List comics enriched with FMV data.

    `locg_id` + `grade` is the canonical lookup for FMV cache reuse — see
    `comic-fmv` (apps/fmv) and `/comic:fmv`. `max_age_days` excludes rows
    whose `fmv_updated_at` is older than the cutoff so callers can't reuse
    stale FMVs by accident.
    """
    db = request.app.state.db
    rows = list_comics(
        db,
        title=title,
        issue=issue,
        year=year,
        grade=grade,
        locg_id=locg_id,
        max_age_days=max_age_days,
    )
    return [dict(r) for r in rows]


@router.post("/api/comics")
async def api_upsert_comic(req: UpsertComicRequest, request: Request):
    """Upsert a comic (and optional FMV at grade) and return both ids.

    Response includes `comic_id` (alias of `id` — the comics row PK) and
    `fmv_id` (the upserted fmv row, or null when no grade was provided).
    PER-144: callers like `fmv_runner` need both ids in one round-trip so
    they can thread them straight into `gixen-cli add` for link-fmv.
    """
    db = request.app.state.db
    comic_id = upsert_comic(
        db,
        title=req.title,
        issue=req.issue,
        year=req.year,
        locg_id=req.locg_id,
        locg_variant_id=req.locg_variant_id,
        variant=req.variant,
    )
    fmv_id: int | None = None
    if req.grade is not None:
        fmv_id = upsert_fmv(
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
    return {**dict(row), "comic_id": comic_id, "fmv_id": fmv_id}


@router.post("/api/bids/{item_id}/link-fmv")
async def api_link_fmv(item_id: str, req: LinkFmvRequest, request: Request):
    """Link a bid to its FMV row.

    Tries three resolution strategies in order, narrowed by grade:

    1. `comic_id` — direct lookup against `fmv(comic_id, grade)`. Use this
       when the caller already knows the internal DB id (e.g. `fmv_runner`
       threading the id back from `POST /api/comics`).
    2. `locg_id` — JOIN comics on locg_id. The canonical path historically,
       but `comics.locg_id` is NULL after most FMV runs (LOCG cache returns
       null ids; live `locg lookup` is Cloudflare-blocked).
    3. `(series, issue, [year])` — case-insensitive title + issue match,
       optionally narrowed by year. The fallback that keeps link-fmv working
       when locg_id never got populated (PER-140 Gap 2).

    Populates the bid_fmvs junction table and sets bids.fmv_id so the
    /api/comics/snipes dashboard can show cond_grade and fmv_low/fmv_high.
    """
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = request.app.state.db

    bid = get_bid_by_item_id(db, item_id)
    if bid is None:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in DB")

    fmv_row, strategy = _resolve_fmv_for_link(db, req)
    if fmv_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No FMV found (strategies attempted: {', '.join(strategy)})",
        )

    link_fmv_to_bid(db, bid["id"], fmv_row["fmv_id"], is_primary=True)
    return {"item_id": item_id, "fmv_id": fmv_row["fmv_id"], "linked": True}


def _resolve_fmv_for_link(db, req: LinkFmvRequest) -> tuple[object | None, list[str]]:
    """Resolve fmv_id for link-fmv. Returns (row, list-of-strategies-attempted).

    Strategies are tried in order and short-circuit on first hit. The
    attempted list is reported back in the 404 detail so callers can tell
    which inputs were too sparse.
    """
    attempted: list[str] = []

    if req.comic_id is not None:
        attempted.append(f"comic_id={req.comic_id}+grade={req.grade}")
        row = db.execute(
            "SELECT id AS fmv_id FROM fmv WHERE comic_id=? AND grade=? LIMIT 1",
            (req.comic_id, req.grade),
        ).fetchone()
        if row is not None:
            return row, attempted

    if req.locg_id is not None:
        attempted.append(f"locg_id={req.locg_id}+grade={req.grade}")
        row = db.execute(
            """
            SELECT f.id AS fmv_id
            FROM fmv f
            JOIN comics c ON c.id = f.comic_id
            WHERE c.locg_id = ? AND f.grade = ?
            LIMIT 1
            """,
            (req.locg_id, req.grade),
        ).fetchone()
        if row is not None:
            return row, attempted

    if req.series and req.issue:
        if req.year is not None:
            attempted.append(
                f"series={req.series!r}+issue={req.issue!r}+year={req.year}+grade={req.grade}"
            )
            row = db.execute(
                """
                SELECT f.id AS fmv_id
                FROM fmv f
                JOIN comics c ON c.id = f.comic_id
                WHERE LOWER(c.title) = LOWER(?) AND c.issue = ?
                  AND c.year = ? AND f.grade = ?
                LIMIT 1
                """,
                (req.series, req.issue, req.year, req.grade),
            ).fetchone()
            if row is not None:
                return row, attempted
        attempted.append(
            f"series={req.series!r}+issue={req.issue!r}+grade={req.grade}"
        )
        row = db.execute(
            """
            SELECT f.id AS fmv_id
            FROM fmv f
            JOIN comics c ON c.id = f.comic_id
            WHERE LOWER(c.title) = LOWER(?) AND c.issue = ?
              AND f.grade = ?
            LIMIT 1
            """,
            (req.series, req.issue, req.grade),
        ).fetchone()
        if row is not None:
            return row, attempted

    if not attempted:
        attempted.append("none — provide comic_id, locg_id, or series+issue")
    return None, attempted


@router.post("/api/bids/{item_id}/comics/locg")
async def api_link_locg(item_id: str, req: LocgLinkRequest, request: Request):
    """Persist a resolved LOCG ID against a specific comic in a bid's set.

    DEPRECATED (BUI-24, BUI-25): LOCG IDs are no longer resolved live — the LOCG
    API 403s everything and the tool pivoted to a local-first model. The canonical
    flow is now `locg collection record-win` -> `locg collection export` -> manual
    LOCG Bulk Import -> `locg collection import` (see
    docs/solutions/integration-issues/locg-bulk-import-recipe-2026-05-22.md). This
    route stays functional for legacy snipes — it preserves existing values (R47)
    — but should not be used by new flows; v2 may remove it.

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


@router.post("/api/comics/verify")
async def api_verify(req: VerifyRequest, request: Request):
    """Verify each working-list item's bid → fmv → comic linkage is complete.

    PER-99: `/comic:buy` and `/comic:snipe-add` write across `bids`, `comics`,
    `fmv`, and `bid_fmvs` but no single step asserts every link landed. This
    endpoint walks the chain for each input item_id + grade (+ optional locg_id)
    and assigns a verdict so the caller can surface gaps in the run summary.

    Verdicts (ladder — first failure wins):
      - `no_bid`         — no bids row for item_id
      - `no_comic`       — no comic linked via bid_fmvs (or via locg_id if given)
      - `no_fmv_at_grade`— comic exists but no fmv row at the requested grade
      - `fmv_stub`       — fmv row exists but low/high are NULL (`/comic:fmv` never ran)
      - `partial`        — fmv populated but bid_fmvs junction or bids.fmv_id is missing
      - `fully_linked`   — all five checks pass

    `missing` lists the specific failed checks so callers don't have to map
    verdict → user-visible message themselves.
    """
    db = request.app.state.db
    results = []

    for item in req.items:
        result = _verify_one(db, item.item_id, item.grade, item.locg_id)
        results.append(result)

    summary = {
        "total": len(results),
        "fully_linked": sum(1 for r in results if r["verdict"] == "fully_linked"),
        "issues": sum(1 for r in results if r["verdict"] != "fully_linked"),
    }
    return {"summary": summary, "results": results}


def _verify_one(db, item_id: str, grade: float | None, locg_id: int | None) -> dict:
    """Walk bid → bid_fmvs → fmv → comics for one working-list item.

    The grade-matched fmv is the canonical pivot: we look for a row whose
    grade exactly matches the requested grade (when given). `bids.fmv_id` is
    a denormalized pointer that should agree with the primary `bid_fmvs` row
    — we check both because past incidents (PER-90) showed they can drift.
    """
    base = {
        "item_id": item_id,
        "grade": grade,
        "locg_id": locg_id,
        "missing": [],
    }

    bid = db.execute(
        "SELECT id, fmv_id FROM bids WHERE item_id = ?", (item_id,)
    ).fetchone()
    if bid is None:
        return {**base, "verdict": "no_bid", "missing": ["bids row"]}

    # All comics linked to this bid via the junction, plus optional grade filter.
    fmv_query = (
        "SELECT bf.fmv_id, bf.is_primary, "
        "       f.grade, f.low, f.high, "
        "       c.id AS comic_id, c.title, c.issue, c.year, c.locg_id "
        "FROM bid_fmvs bf "
        "JOIN fmv f ON f.id = bf.fmv_id "
        "JOIN comics c ON c.id = f.comic_id "
        "WHERE bf.bid_id = ?"
    )
    fmv_rows = db.execute(fmv_query, (bid["id"],)).fetchall()

    # Match strategy: prefer locg_id (canonical), fall back to grade.
    match = None
    if locg_id is not None:
        match = next((r for r in fmv_rows if r["locg_id"] == locg_id
                      and (grade is None or r["grade"] == grade)), None)
    if match is None and grade is not None:
        match = next((r for r in fmv_rows if r["grade"] == grade), None)
    if match is None and fmv_rows:
        # Last resort: take the primary, so we can still report partial states.
        match = next((r for r in fmv_rows if r["is_primary"]), fmv_rows[0])

    if match is None:
        # No comic linked at all. If locg_id was given, check whether the
        # comic exists in the table — useful to distinguish "linkage missing"
        # from "we don't know this comic".
        missing = ["bid_fmvs junction"]
        if locg_id is not None:
            comic_exists = db.execute(
                "SELECT 1 FROM comics WHERE locg_id = ?", (locg_id,)
            ).fetchone()
            if comic_exists is None:
                missing = ["comics row", "fmv row", "bid_fmvs junction"]
        return {**base, "verdict": "no_comic", "missing": missing,
                "bid_fmv_id": bid["fmv_id"]}

    # We have a candidate comic. Did we match on grade?
    if grade is not None and match["grade"] != grade:
        return {**base, "verdict": "no_fmv_at_grade",
                "missing": [f"fmv row at grade {grade}"],
                "comic_id": match["comic_id"],
                "bid_fmv_id": bid["fmv_id"]}

    # fmv exists at the right grade. Is it stubbed?
    if match["low"] is None or match["high"] is None:
        missing = []
        if match["low"] is None:
            missing.append("fmv.low")
        if match["high"] is None:
            missing.append("fmv.high")
        return {**base, "verdict": "fmv_stub",
                "missing": missing,
                "comic_id": match["comic_id"],
                "fmv_id": match["fmv_id"],
                "bid_fmv_id": bid["fmv_id"]}

    # fmv populated. Check bids.fmv_id agrees with the matched fmv. The
    # junction row is implicit (the match came from bid_fmvs), but
    # bids.fmv_id can still be NULL or point at a different fmv.
    partial_missing = []
    if bid["fmv_id"] is None:
        partial_missing.append("bids.fmv_id")
    elif bid["fmv_id"] != match["fmv_id"]:
        partial_missing.append(
            f"bids.fmv_id={bid['fmv_id']} mismatches matched fmv_id={match['fmv_id']}"
        )
    # Locg sanity check, only when caller passed locg_id.
    if locg_id is not None and match["locg_id"] != locg_id:
        partial_missing.append(
            f"comic.locg_id={match['locg_id']} mismatches expected {locg_id}"
        )

    if partial_missing:
        return {**base, "verdict": "partial",
                "missing": partial_missing,
                "comic_id": match["comic_id"],
                "fmv_id": match["fmv_id"],
                "bid_fmv_id": bid["fmv_id"]}

    return {**base, "verdict": "fully_linked",
            "comic_id": match["comic_id"],
            "fmv_id": match["fmv_id"],
            "bid_fmv_id": bid["fmv_id"]}


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

    end_date_iso = item.get("auction_end_at") or item.get("resolved_at")
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
        -- 'PURGED' (legacy) and 'REMOVED' (BUI-49 rename) are the same
        -- soft-delete tombstone. Filter both so removed snipes are excluded
        -- regardless of whether the gixen-cli rename migration has run yet.
        WHERE b.status NOT IN ('PURGED', 'REMOVED')
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
            -- Exclude the soft-delete tombstone ('PURGED' legacy + 'REMOVED'
            -- BUI-49 rename) so removed snipes never leak into "recently ended"
            -- (BUI-50). Filtering inside the dedup subquery — not just the outer
            -- query — means MAX(id) picks the latest *non-tombstone* row, so a
            -- legit LOST/WON row still shows even if a later same-item snipe was
            -- added then removed (higher id, tombstone). Mirrors
            -- api_comics_snipes' status filter.
            WHERE status NOT IN ('PURGED', 'REMOVED')
            AND (
              (
                auction_end_at IS NOT NULL
                AND datetime(auction_end_at) <= datetime('now')
                AND datetime(auction_end_at) >= datetime('now', '-7 days')
              ) OR (
                auction_end_at IS NULL
                AND resolved_at IS NOT NULL
                AND datetime(resolved_at) >= datetime('now', '-7 days')
              )
            )
            GROUP BY item_id
        ) latest ON b.id = latest.max_id
        LEFT JOIN bid_fmvs bf ON bf.bid_id = b.id
        LEFT JOIN fmv f ON f.id = bf.fmv_id
        GROUP BY b.id
        ORDER BY COALESCE(b.auction_end_at, b.resolved_at) DESC
    """).fetchall()
    return [_build_comics_row(r) for r in rows]


@router.get("/api/seller-reliability")
async def api_seller_reliability(request: Request, seller: str):
    """Average grade deviation for one seller (BUI-78).

    `avg_deviation = AVG(seller_grade - photo_grade)` over the seller's bids that
    have BOTH grades and are not tombstoned (status NOT IN PURGED/REMOVED — the
    BUI-50 parity rule). Positive = the seller over-states condition. The key is
    the lowercased eBay username (matching what the buy flow writes at INSERT);
    auction outcome is irrelevant to grading accuracy, so all live/terminal
    statuses count. No min-sample cutoff — the caller (/comic:buy) decides.

    Deliberately does NOT trigger `_ensure_fresh_sync`: it reads locally written
    historical grades, not live Gixen state, so a sync would add latency for no
    freshness gain.
    """
    seller = seller.strip()
    if not seller or len(seller) > 128:
        raise HTTPException(status_code=422, detail="seller must be 1-128 characters")
    key = seller.lower()
    db = request.app.state.db
    row = db.execute(
        """
        SELECT AVG(seller_grade - photo_grade) AS avg_dev, COUNT(*) AS n
        FROM bids
        WHERE seller = ?
          AND seller_grade IS NOT NULL
          AND photo_grade IS NOT NULL
          AND status NOT IN ('PURGED', 'REMOVED')
        """,
        (key,),
    ).fetchone()
    n = row["n"] or 0
    return {
        "seller": key,
        "avg_deviation": round(row["avg_dev"], 4) if n else None,
        "sample_size": n,
    }


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
          AND status NOT IN ('PURGED', 'REMOVED')
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
        # PER-98: year is optional. Try LOCG only as a best-effort enrichment
        # for the locg_id (and a real year if available). When it fails, fall
        # through with year=None — upsert_comic handles yearless rows and
        # promotes them to yeared rows later if LOCG becomes reachable.
        primary_resolution = None
        if year is None:
            primary_resolution = resolve_year_and_locg(parsed.series, issues[0])
            if primary_resolution is not None:
                year = primary_resolution.year

        try:
            wrote_junction = False
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
                    existing_valued = db.execute(
                        "SELECT f.id FROM fmv f JOIN comics c ON c.id = f.comic_id "
                        "WHERE LOWER(c.title)=LOWER(?) AND c.issue=? AND f.grade=? AND f.low IS NOT NULL "
                        "LIMIT 1",
                        (parsed.series, issue, parsed.grade),
                    ).fetchone()
                    if existing_valued:
                        fmv_id = existing_valued["id"]
                    else:
                        fmv_id = upsert_fmv(
                            db,
                            comic_id=comic_id,
                            grade=parsed.grade,
                            notes=f"auto-linked from eBay title (confidence={parsed.confidence})",
                        )
                    link_fmv_to_bid(db, row["id"], fmv_id, is_primary=(idx == 0))
                    wrote_junction = True
                else:
                    # No parseable grade — link to any existing valued FMV for this comic.
                    any_valued = db.execute(
                        "SELECT f.id FROM fmv f WHERE f.comic_id=? AND f.low IS NOT NULL LIMIT 1",
                        (comic_id,),
                    ).fetchone()
                    if any_valued:
                        link_fmv_to_bid(db, row["id"], any_valued["id"], is_primary=(idx == 0))
                        wrote_junction = True
            if wrote_junction:
                linked += 1
            else:
                skipped.append({"item_id": item_id, "reason": "no grade parsed"})
        except Exception as e:
            errors.append({"item_id": item_id, "error": f"link failed: {e}"})

    return {
        "processed": processed,
        "linked": linked,
        "skipped": skipped,
        "errors": errors,
    }


@router.post("/api/sweep-orphans")
async def api_sweep_orphans(request: Request, dry_run: bool = True):
    """Merge yearless comics rows that have a yeared sibling.

    Safe to call repeatedly — idempotent. Defaults to dry_run=True so a
    plain POST returns a preview without touching data. Pass ?dry_run=false
    to perform the actual merge.
    """
    db = request.app.state.db
    return sweep_orphan_yearless_comics(db, dry_run=dry_run)
