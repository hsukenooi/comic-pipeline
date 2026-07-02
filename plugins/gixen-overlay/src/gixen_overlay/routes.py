"""Comic FastAPI routes for the gixen-overlay plugin."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from gixen_overlay.db import (
    upsert_comic,
    upsert_fmv,
    link_fmv_to_bid,
    get_primary_fmv_for_bid,
    list_comics,
    sweep_orphan_yearless_comics,
    get_seen_item_ids,
    mark_items_seen,
    get_collection_wins_seen,
    mark_collection_wins_seen,
)
from gixen_overlay.locg_lookup import resolve_year_and_locg
from gixen_overlay.models import (
    UpsertComicRequest,
    LocgLinkRequest,
    LinkFmvRequest,
    VerifyRequest,
    WishListAddRequest,
    RecordWinRequest,
    SellerScanSeenRequest,
    CollectionWinsSeenRequest,
    CollectionCheckBatchRequest,
)
from gixen_overlay.title_parser import parse_title
from server.db import get_bid_by_item_id, resolve_server_dir, TOMBSTONE_STATUSES_SQL
from server.main import _ensure_fresh_sync, iso_to_relative, _spawn_fallback_task

# BUI-91/92: the overlay wraps locg-cli's existing collection + wish-list logic
# (the accumulated matcher with its four documented bugfixes, plus the three
# write paths) behind /api/comics/* instead of porting any of it to SQL. These
# imports prove the locg workspace dependency resolves (exercised by the
# workspace-imports canary).
from locg.collection_cache import CollectionCache
from locg.collection_io import MAX_XLSX_BYTES
from locg.commands import (
    _split_wish_list_name,
    cmd_collection_check,
    cmd_collection_export,
    cmd_collection_import,
    cmd_collection_record_win,
    cmd_collection_series_names,
    cmd_collection_status,
    cmd_wish_list_add,
    cmd_wish_list_conflicts,
    cmd_wish_list_from_cache,
    cmd_wish_list_remove,
    cmd_wish_list_remove_conflicts,
)
from openpyxl.utils.exceptions import InvalidFileException


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}
_NUMERIC_RE = re.compile(r"[^0-9.]")

# BUI-106: stream uploads to the tempfile in bounded chunks so an over-cap POST
# is rejected before its whole body is buffered into memory/disk. 1 MB keeps
# peak memory to one chunk while staying well under the MAX_XLSX_BYTES cap.
_UPLOAD_CHUNK_BYTES = 1024 * 1024

router = APIRouter()


def _ensure_collection_store() -> None:
    """Point locg-cli's cache resolver at a server-owned, provider-neutral store.

    ``locg.config._cache_dir()`` resolves ``LOCG_DATA_DIR`` env →
    ``<repo>/data/locg`` → ``~/.cache/locg``. On the Mac Mini's editable checkout
    the default would be the repo's ``data/locg/`` — the very location BUI-93
    retires as the source of truth. So when ``LOCG_DATA_DIR`` is unset we point
    it at a server-owned directory beside the comics-server DB
    (``<dir(DB_PATH)>/collection-store``, default
    ``~/.comics-server/collection-store``, with a ``~/.gixen-server`` fallback
    for the not-yet-migrated live server — see ``resolve_server_dir``). The
    directory is named neutrally, not "locg" (R1: "the path is not named for
    LOCG"). An explicitly-set ``LOCG_DATA_DIR`` always wins, so the Mac Mini
    launch env and the tests (which point it at a tmp dir) both override this
    default.
    """
    if os.environ.get("LOCG_DATA_DIR", "").strip():
        return
    db_path = Path(os.environ.get("DB_PATH") or (resolve_server_dir() / "db.sqlite"))
    store = db_path.parent / "collection-store"
    store.mkdir(parents=True, exist_ok=True)
    os.environ["LOCG_DATA_DIR"] = str(store)


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
    locg_variant_id: int | None = None,
    max_age_days: float | None = None,
):
    """List comics enriched with FMV data.

    `locg_id` + `grade` is the canonical lookup for FMV cache reuse — see
    `comic-fmv` (apps/fmv) and `/comic:fmv`. `locg_variant_id` (BUI-139) scopes
    the lookup to one variant so a base cover and a Newsstand variant of the
    same issue (same `locg_id`) don't reuse each other's FMV. `max_age_days`
    excludes rows whose `fmv_updated_at` is older than the cutoff so callers
    can't reuse stale FMVs by accident.
    """
    db = request.app.state.db
    rows = list_comics(
        db,
        title=title,
        issue=issue,
        year=year,
        grade=grade,
        locg_id=locg_id,
        locg_variant_id=locg_variant_id,
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
            flag_reason=req.fmv_flag_reason,
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


def _resolve_fmv_for_link(db, req: LinkFmvRequest) -> tuple[Any | None, list[str]]:
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
      - `needs_manual`   — fmv flagged (BUI-86/BUI-132): intentionally unpriceable, hand-price it
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
    base: dict[str, Any] = {
        "item_id": item_id,
        "grade": grade,
        "locg_id": locg_id,
        "missing": [],
    }

    # BUI-152: an item_id can have multiple bids rows — a prior terminal
    # (LOST/ENDED) or tombstoned (REMOVED) row plus a fresh PENDING re-add, or
    # BUI-67 dedup losers. Without ORDER BY, fetchone() returns the OLDEST
    # (lowest rowid) row, whose bid_fmvs/fmv_id were never populated for the new
    # snipe — so verify would mis-verdict a fully-linked live bid as
    # no_comic/fmv_stub. Resolve the freshest row, matching the convention in
    # server.db.get_bid_by_item_id (ORDER BY id DESC LIMIT 1).
    bid = db.execute(
        "SELECT id, fmv_id FROM bids WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if bid is None:
        return {**base, "verdict": "no_bid", "missing": ["bids row"]}

    # All comics linked to this bid via the junction, plus optional grade filter.
    fmv_query = (
        "SELECT bf.fmv_id, bf.is_primary, "
        "       f.grade, f.low, f.high, f.flag_reason, "
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

    # fmv exists at the right grade. Is it an intentionally-unpriceable book?
    # BUI-132: a needs_manual book (BUI-86) carries a structured flag_reason and
    # has NULL low/high *by design* — it would otherwise fall through to the
    # fmv_stub branch below and wrongly advise "re-run /comic:fmv" (a no-op,
    # since re-running just re-flags it). Emit a DISTINCT verdict so the caller
    # tells the user to hand-price it instead.
    if match["flag_reason"] is not None:
        return {**base, "verdict": "needs_manual",
                "missing": [],
                "flag_reason": match["flag_reason"],
                "comic_id": match["comic_id"],
                "fmv_id": match["fmv_id"],
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
        "time_to_end": iso_to_relative(end_date_iso),
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
        -- Active = live (PENDING) snipes only. Terminal outcomes
        -- (WON/LOST/ENDED/FAILED) belong to /api/comics/history, not here, and
        -- the soft-delete tombstone ('PURGED' legacy + 'REMOVED' BUI-49 rename)
        -- is excluded too. Making the server authoritative (BUI-83) stops a
        -- resolved snipe with no auction_end_at from being pinned in Active when
        -- the front-end isEnded() heuristic can't detect the end (no end-date and
        -- a status string it doesn't treat as ended).
        WHERE b.status NOT IN ({TOMBSTONE_STATUSES_SQL}, 'WON', 'LOST', 'ENDED', 'FAILED')
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
            WHERE status NOT IN ({TOMBSTONE_STATUSES_SQL})
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
        f"""
        SELECT AVG(seller_grade - photo_grade) AS avg_dev, COUNT(*) AS n
        FROM bids
        WHERE LOWER(seller) = ?
          AND seller_grade IS NOT NULL
          AND photo_grade IS NOT NULL
          AND status NOT IN ({TOMBSTONE_STATUSES_SQL})
        """,
        (key,),
    ).fetchone()
    n = row["n"] or 0
    return {
        "seller": key,
        "avg_deviation": round(row["avg_dev"], 4) if n else None,
        "sample_size": n,
    }


def _link_issue_to_bid(
    db,
    *,
    bid_id: int,
    series: str,
    issue: str,
    year,
    grade,
    confidence,
    locg_id,
    locg_variant_id,
    is_primary: bool,
) -> bool:
    """Upsert one comic/issue and link it to `bid_id` via fmv_id.

    Returns True iff a bids -> bid_fmvs junction row was written for this issue.
    """
    comic_id = upsert_comic(
        db,
        title=series,
        issue=issue,
        year=year,
        locg_id=locg_id,
        locg_variant_id=locg_variant_id,
    )
    if grade is not None:
        # BUI-144/145: scope to the comic_id upsert_comic just
        # returned, not a title/issue re-match. The old query
        # ignored year AND variant, so a bid could link to a valued
        # FMV of a DIFFERENT edition (e.g. ASM 1963 #1 priced at the
        # 2018 reprint's FMV) — comic_id already encodes year+variant,
        # so this is correct for free and mirrors the no-grade branch.
        existing_valued = db.execute(
            "SELECT f.id FROM fmv f "
            "WHERE f.comic_id=? AND f.grade=? AND f.low IS NOT NULL "
            "LIMIT 1",
            (comic_id, grade),
        ).fetchone()
        if existing_valued:
            fmv_id = existing_valued["id"]
        else:
            fmv_id = upsert_fmv(
                db,
                comic_id=comic_id,
                grade=grade,
                notes=f"auto-linked from eBay title (confidence={confidence})",
            )
        link_fmv_to_bid(db, bid_id, fmv_id, is_primary=is_primary)
        return True
    else:
        # No parseable grade — link to any existing valued FMV for this comic.
        any_valued = db.execute(
            "SELECT f.id FROM fmv f WHERE f.comic_id=? AND f.low IS NOT NULL LIMIT 1",
            (comic_id,),
        ).fetchone()
        if any_valued:
            link_fmv_to_bid(db, bid_id, any_valued["id"], is_primary=is_primary)
            return True
    return False


@router.post("/api/extract-comics")
async def api_extract_comics(request: Request):
    """Parse cached eBay titles for unlinked bids and link them via fmv_id.

    Idempotent: skips bids that already have fmv_id set.
    """
    db = request.app.state.db

    rows = db.execute(
        f"""
        SELECT id, item_id, ebay_title
        FROM bids
        WHERE fmv_id IS NULL
          AND ebay_title IS NOT NULL
          AND ebay_title != ''
          AND status NOT IN ({TOMBSTONE_STATUSES_SQL})
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
        except Exception as e:  # noqa: BLE001  # per-snipe parse — capture error, continue batch
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
                if _link_issue_to_bid(
                    db,
                    bid_id=row["id"],
                    series=parsed.series,
                    issue=issue,
                    year=year,
                    grade=parsed.grade,
                    confidence=parsed.confidence,
                    locg_id=primary_resolution.locg_id if (primary_resolution and idx == 0) else None,
                    locg_variant_id=primary_resolution.locg_variant_id if (primary_resolution and idx == 0) else None,
                    is_primary=(idx == 0),
                ):
                    wrote_junction = True
            if wrote_junction:
                linked += 1
            else:
                skipped.append({"item_id": item_id, "reason": "no grade parsed"})
        except Exception as e:  # noqa: BLE001  # per-snipe FMV link — capture error, continue batch
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


# ---------------------------------------------------------------------------
# BUI-91: server-authoritative collection + wish-list READ endpoints.
#
# These wrap the existing locg-cli functions against the server-owned canonical
# store (see _ensure_collection_store). Provider-neutral names — the served data
# is a *collection* and a *wish-list*, not "the locg collection" (LOCG is one
# import source, not the data's identity). None of these touch the SQLite DB, so
# they take no `request` / `app.state.db`.
# ---------------------------------------------------------------------------


@router.get("/api/comics/collection/check")
async def api_collection_check(
    series: str,
    issue: str,
    year: str | None = None,
    variant: str | None = None,
):
    """Collection-ownership check (R2). Returns the exact verdict shape the
    `locg collection check` CLI returns today:
    ``{match_status, full_title_matched, cache_age_days}``.

    A corrupt/crashed store raises RuntimeError from CollectionCache.load(); we
    surface it as HTTP 500 rather than letting it 500 unhandled. The consumer
    MUST treat any non-200 (or an unreachable server) as a hard error and never
    render "not owned" from it (R11) — a silent miss buys a duplicate.
    """
    _ensure_collection_store()
    try:
        result = cmd_collection_check(series=series, issue=issue, variant=variant, year=year)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
    # R11 defense-in-depth at the endpoint, not just in the skill: a store that
    # was never imported answers `not_in_cache` for EVERY comic. Returning that
    # as a 200 lets any caller that skips the bootstrap status guard read it as
    # "not owned → safe to buy" and buy duplicates. cache_age_days is None iff
    # last_full_import is null (never imported), so refuse with 409 instead of
    # emitting a "not owned" verdict we cannot actually stand behind.
    if result["match_status"] == "not_in_cache" and result["cache_age_days"] is None:
        raise HTTPException(
            status_code=409,
            detail="collection store has no import yet — cannot determine ownership "
            "(refusing to report 'not owned', R11)",
        )
    return result


@router.post("/api/comics/collection/check/batch")
async def api_collection_check_batch(req: CollectionCheckBatchRequest):
    """Batch collection-ownership check (BUI-204).

    Accepts a list of ``{series, issue, year?, variant?}`` pairs and returns a
    per-item result list, each entry carrying the same verdict shape the
    single-item ``GET /api/comics/collection/check`` returns
    (``{match_status, full_title_matched, cache_age_days}``) plus its echoed
    ``series``/``issue`` so the caller can correlate without relying on order.
    Eliminates the per-issue HTTP fan-out ``/comic:wishlist-add`` used to do.

    Each pair is run through the *same* ``cmd_collection_check`` matcher, so the
    `match_status` semantics (``in_collection`` / ``not_in_cache``, leading-article
    normalization, the year-gated masthead fallback) are identical to the
    single-item endpoint — this is a fan-out, not a reimplementation.

    R11 is preserved as a whole-batch guard: a never-imported store would answer
    ``not_in_cache`` for every item, so rather than returning a list of
    confident-looking "not owned" verdicts we 409 the entire call (the same
    refusal the single-item endpoint makes, lifted to the batch boundary). A
    corrupt/crashed store surfaces as 500. An empty ``items`` list is a 422.
    """
    if not req.items:
        raise HTTPException(status_code=422, detail="items must be a non-empty list")
    _ensure_collection_store()

    # R11 whole-batch guard: refuse the entire call against a never-imported
    # store instead of emitting a per-item "not owned" we cannot stand behind.
    # cache_age_days is None iff last_full_import is null, so the cheapest probe
    # is the status read used by the other guards in this module.
    _require_imported_collection()

    results: list[dict[str, Any]] = []
    for item in req.items:
        try:
            result = cmd_collection_check(
                series=item.series,
                issue=item.issue,
                variant=item.variant,
                year=item.year,
            )
        except RuntimeError as exc:
            # A store that crashes mid-batch is a hard failure for the whole
            # call (R11): never let one item's failure read as "not owned".
            raise HTTPException(
                status_code=500, detail=f"collection store unavailable: {exc}"
            ) from exc
        results.append({"series": item.series, "issue": item.issue, **result})

    return {"count": len(results), "results": results}


@router.get("/api/comics/collection/status")
async def api_collection_status():
    """Collection cache status metrics (last_full_import, row_count,
    cache_age_days, pending_push_count, locg_cli_version, ...). Used by the
    collection-check bootstrap guard."""
    _ensure_collection_store()
    try:
        return cmd_collection_status()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc


@router.get("/api/comics/collection/series-names")
async def api_collection_series_names():
    """Series names present in the collection cache (BUI-129).

    Lets a caller resolve a Metron/identify series name to the exact LOCG
    catalog spelling before calling `/check` — or surface a "not found — did you
    mean X?" hint — instead of trusting a silent `not_in_cache` that may just be
    an exact-match miss. Provider-neutral, read-only."""
    _ensure_collection_store()
    try:
        return cmd_collection_series_names()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc


@router.get("/api/comics/wish-list")
async def api_wish_list(title: str | None = None):
    """Wish-list read (R3) for seller-scan to match against. Returns
    ``[{name, id, ...}]``. A never-imported wish-list (FileNotFoundError) yields
    an empty list: an empty wish-list is a correct, non-dangerous answer (a miss
    only fails to surface a wanted book; it cannot buy a dupe)."""
    _ensure_collection_store()
    try:
        return cmd_wish_list_from_cache(title)
    except (FileNotFoundError, json.JSONDecodeError):
        # BUI-184: a missing OR corrupt wish-list cache yields an empty list, not
        # a 500. An empty wish-list is a correct, non-dangerous answer (a miss
        # only fails to surface a wanted book; it cannot buy a dupe), and a 500
        # here would break seller-scan entirely on a single bad write.
        return []


def _require_imported_collection() -> None:
    """409 if the collection store has never been imported (BUI-130, R11).

    An un-imported store answers ``not_in_cache`` for every comic, so a
    wish-list conflict audit run against it would report zero conflicts — a
    false "all clear" that hides real owned-but-wished books. Refuse rather than
    emit an ownership answer we cannot stand behind. Mirrors the guard on
    ``GET /api/comics/collection/check``.
    """
    try:
        status = cmd_collection_status()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
    if status.get("last_full_import") is None:
        raise HTTPException(
            status_code=409,
            detail="collection store has no import yet — cannot determine ownership "
            "(refusing to audit the wish-list against an empty collection, R11)",
        )


@router.get("/api/comics/wish-list/conflicts")
async def api_wish_list_conflicts():
    """Audit the wish-list for items already in the collection (BUI-130, Part 1).

    Cross-references every wish-list entry against the collection — the same
    per-item check ``/comic:wishlist-add`` does at add time, applied
    retroactively. Owned-but-wished books are the BUI-122 data-loss risk: a
    sync exports them with ``In Collection=0`` and LOCG removes them. This is
    the dry-run preview; ``POST .../remove-conflicts`` performs the removal.

    Returns ``{total, checked, unparseable, conflicts:[{name, series, issue,
    id, full_title_matched, series_name, release_date}]}``. ``series_name``/
    ``release_date`` (BUI-266) are the matched owned row's BUI-249 provenance
    — this audit is year/variant-blind by necessity (a wish-list name carries
    no per-issue year), so review these before scoping a
    ``POST .../remove-conflicts`` call to catch a decoy cross-era/cross-edition
    match. 409 when the store was never imported (R11); an absent wish-list
    yields an empty (zero-conflict) result.
    """
    _ensure_collection_store()
    _require_imported_collection()
    try:
        return cmd_wish_list_conflicts()
    except FileNotFoundError:
        return {"total": 0, "checked": 0, "unparseable": [], "conflicts": []}


@router.post("/api/comics/wish-list/remove-conflicts")
async def api_wish_list_remove_conflicts(payload: dict = Body(default={})):
    """Remove wish-list item(s) already in the collection (BUI-130, Part 2).

    BUI-266 (P1 data foot-gun): this endpoint used to unconditionally sweep
    the ENTIRE conflict set — a caller clearing a handful of just-discovered
    conflicts had no way to avoid also removing every OTHER pre-existing
    conflict already in the wish-list. The BUI-259 incident removed 114 wishes
    when ~6 were intended, including decoy cross-volume/cross-edition false
    matches (a UK-reprint "The Avengers (1973 - 1976)" #52 pulled against an
    owned 1968 Vol. 1; a base "Uncanny X-Men #201" pulled against an owned
    Newsstand copy) — the audit is year/variant-blind by necessity (a
    wish-list name carries no per-issue year), so a caller MUST be able to
    review each match's provenance before it's removed.

    Body (all optional, JSON): ``{"names": [...], "confirm": bool}``.

    * ``names``: scope the removal to exactly these wish-list ``name`` values
      (as returned by ``GET .../conflicts``). Each is re-checked against a
      FRESH audit — a name that is no longer a genuine conflict is reported as
      an error, never silently removed. This is the RECOMMENDED path: run the
      GET audit, review each conflict's ``series_name``/``release_date``
      provenance for a decoy, then submit only the reviewed names.
    * No ``names`` and ``confirm`` is not ``true``: returns the SAME preview
      the GET audit returns (with ``dry_run: true``) and mutates nothing —
      the safe default for an unscoped call.
    * No ``names`` and ``confirm: true``: removes every current conflict (the
      original global-sweep behavior), for a caller that has already reviewed
      the audit and explicitly wants everything cleared.

    Returns ``{removed, removed_count, errors, remaining, checked,
    unparseable, scoped}`` (mutating calls) or the audit shape plus
    ``dry_run: true`` (preview). Same 409 never-imported guard as the audit
    (R11). Each removed entry carries ``matched_series_name`` /
    ``matched_release_date`` — the BUI-249 provenance of the owned row it was
    matched against.
    """
    _ensure_collection_store()
    _require_imported_collection()
    names = payload.get("names")
    if names is not None and (
        not isinstance(names, list) or not all(isinstance(n, str) for n in names)
    ):
        raise HTTPException(status_code=422, detail="'names' must be a list of strings")
    confirm = bool(payload.get("confirm", False))

    try:
        if names:
            return cmd_wish_list_remove_conflicts(names=names)
        if not confirm:
            audit = cmd_wish_list_conflicts()
            return {**audit, "dry_run": True, "removed": [], "removed_count": 0}
        return cmd_wish_list_remove_conflicts()
    except FileNotFoundError:
        return {
            "removed": [],
            "removed_count": 0,
            "errors": [],
            "remaining": 0,
            "checked": 0,
            "unparseable": [],
        }


@router.get("/api/comics/collection/export")
async def api_collection_export(push_wishes: bool = False):
    """Export pending-push rows to a LOCG-bulk-import CSV (+ .notes.md), read
    from the server store, for the collection-add round-trip. Returns the file
    *contents* (``csv``, ``notes_md``) plus the counts so the caller can save
    them locally and upload to LOCG.

    Wins-only by default — the default export can never emit ``In Collection=0``
    (the LOCG-delete trigger). ``push_wishes=true`` is the opt-in, owned-safe
    wish mirror (deferred per BUI-208 OQ-3)."""
    _ensure_collection_store()
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "locg-bulk-import.csv"
        try:
            result = cmd_collection_export(out_path=str(csv_path), push_wishes=push_wishes)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
        csv_text = Path(result["csv_path"]).read_text()
        notes_path = Path(result["notes_md_path"])
        notes_text = notes_path.read_text() if notes_path.exists() else ""
    return {**result, "csv": csv_text, "notes_md": notes_text}


# ---------------------------------------------------------------------------
# BUI-92: server-authoritative collection + wish-list WRITE endpoints.
#
# The three write paths apply to the server-owned canonical store using the
# EXISTING locg-cli write functions, serialized server-side (CollectionCache
# already does flock + atomic tempfile-rename). After any write lands, both
# machines see it on their next API read — no git commit/push/pull (R8).
# ---------------------------------------------------------------------------


@router.post("/api/comics/collection/import")
async def api_collection_import(file: UploadFile = File(...)):
    """Apply a full LOCG XLSX import to the server store (R5).

    The interactive Playwright login + XLSX download stay local; the client
    POSTs the .xlsx *file* here and the server runs the existing merge
    (`cmd_collection_import` → agent_win reconciliation, identity-tuple upsert,
    rename detection, wish-list rebuild). Uploading the file the server already
    knows how to import reuses that battle-tested path wholesale rather than
    re-deriving it from structured rows.
    """
    _ensure_collection_store()
    suffix = Path(file.filename or "import.xlsx").suffix or ".xlsx"
    tmp_path: str | None = None
    try:
        # BUI-106: stream to disk in bounded chunks and abort the moment the
        # running total exceeds MAX_XLSX_BYTES, so a multi-GB POST can't exhaust
        # memory/disk before locg-cli's stat()-based 10 MB guard fires on the
        # already-buffered file. At most one chunk past the cap is read/written.
        total = 0
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name  # set before write so a read failure still cleans up
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_XLSX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Upload exceeds the "
                            f"{MAX_XLSX_BYTES // (1024 * 1024)} MB limit."
                        ),
                    )
                tmp.write(chunk)
        return cmd_collection_import(tmp_path)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
    except (InvalidFileException, zipfile.BadZipFile, ValueError, KeyError) as exc:
        # Bad/corrupt/wrong-shape upload -> client error. Anything else (OSError,
        # an internal merge bug) is NOT caught here, so it surfaces as a 500
        # rather than being mislabeled a client error.
        raise HTTPException(status_code=422, detail=f"import failed: bad upload ({exc})") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@router.post("/api/comics/collection/record-win")
async def api_record_win(req: RecordWinRequest):
    """Append won auctions to the collection on the server (R6).

    Mirrors `locg collection record-win`; the Metron series resolution and
    BUI-34 already-owned dedup are unchanged (owned by locg-cli). Returns the
    same summary metrics the CLI does.

    BUI-255: `cmd_collection_record_win` is synchronous and can block for a
    while — each new series costs a Metron lookup, and a throttled Metron can
    add a capped-but-real 60s sleep per call (BUI-260's rate-limit retry;
    BUI-255's own batch breaker stops it from repeating that on every row,
    but the FIRST throttled row still sleeps once). Calling it directly on
    this coroutine would block the single-worker event loop for that whole
    stretch — every other endpoint (e.g. GET .../status) would hang until the
    batch finished, which is exactly what happened during the BUI-247 audit.
    `asyncio.to_thread` runs it on a worker thread so the loop stays
    responsive; this matches the pattern gixen-cli's own server/main.py
    already uses for its blocking calls.
    """
    _ensure_collection_store()
    try:
        result = await asyncio.to_thread(cmd_collection_record_win, req.wins)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail=f"collection store unavailable: {exc}"
        ) from exc
    except Exception as exc:
        # BUI-184: record-win previously translated only RuntimeError, so any
        # other mid-batch exception surfaced as an opaque 500 with no signal
        # about commit state. Translate it to a 500 the caller can act on — the
        # commit state is uncertain, so the user must re-verify before trusting
        # it. (cmd_collection_record_win chunk-commits and flags partial_failure
        # for handled failures; this is the unhandled-exception backstop.)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "record_win_failed",
                "message": (
                    "record-win raised mid-batch; the commit state is uncertain "
                    "— re-check the collection / re-import before treating any "
                    "of these wins as recorded"
                ),
                "exception": f"{type(exc).__name__}: {exc}",
            },
        ) from exc

    # BUI-137: cmd_collection_record_win commits in chunks of 25 and, if a chunk
    # raises, logs the error, sets partial_failure=True, and CONTINUES — so a
    # later chunk's wins are silently dropped while the function still returns
    # normally. Returning that dict with HTTP 200 let `curl -sf` (the skill's
    # only failure signal) read a partial commit as full success, silently
    # losing recorded purchases. Raise a non-200 so any HTTP caller halts; carry
    # the partial result in the detail so the user sees what was/wasn't written.
    if result.get("partial_failure"):
        raise HTTPException(
            status_code=500,
            detail={
                "error": "partial_failure",
                "message": (
                    "one or more chunks failed to commit; some wins were NOT "
                    "recorded — do not treat this as success"
                ),
                **result,
            },
        )
    return result


def _is_pinned_collection_row(
    row: dict[str, Any], full_title: str, release_date: str | None
) -> bool:
    """Pin predicate (BUI-254 S1): is `row` an owned row matching BOTH
    `full_title` and `release_date`? See `_pinned_collection_rows` for the
    full rationale. Shared by the dry-run preview, the pre-check, and the
    locked `_mutate` closure in `api_collection_delete` so all three apply
    the identical classification.
    """
    return (
        row.get("in_collection")
        and row.get("full_title") == full_title
        and (row.get("release_date") or None) == release_date
    )


def _pinned_collection_rows(
    comics: list[dict[str, Any]], full_title: str, release_date: str | None
) -> list[dict[str, Any]]:
    """Owned rows matching BOTH `full_title` and `release_date` (BUI-254 S1).

    `full_title` alone is not a unique key: LOCG's `full_title` carries no year
    (BUI-199 — "Hulk #1", not "Hulk (2008) #1"), so a collection owning both the
    1962 and 2008 "Hulk #1" has TWO owned rows with an identical `full_title`.
    `cmd_collection_check`'s year gate already resolved that ambiguity down to
    ONE row (its `matched_release_date` is that exact row's `release_date`, or
    `None` when the matched row is itself dateless) — pin on it too so the row
    this endpoint touches is provably the SAME row `cmd_collection_check`
    reported as in_collection, not just "the first row with this title".
    """
    return [row for row in comics if _is_pinned_collection_row(row, full_title, release_date)]


@router.delete("/api/comics/collection")
async def api_collection_delete(
    series: str,
    issue: str,
    year: str | None = None,
    variant: str | None = None,
    dry_run: bool = False,
):
    """Remove a single erroneous entry from the collection (BUI-254).

    The collection's only write paths were `import` (bulk) and `record-win`
    (append) — there was no way to undo one mistaken entry short of exporting it
    with `In Collection=0` and letting LOCG delete it on sync, the exact
    mechanism that caused the BUI-122 data-loss incident. This is a deliberate
    HARD delete instead, not a tombstone: unlike the `bids` table (BUI-49),
    where the tombstone protects against an *automated* sweep/live-cancel, this
    endpoint is only ever invoked manually for a single confirmed mistake, so a
    tombstone would just be dead weight the read paths have to keep filtering
    (the BUI-50 lesson: an added filter is an added way to forget the filter).
    The full removed record is returned AND logged to the import-history audit
    log, so a mistaken removal can still be manually reversed via `record-win`
    or a fresh import even with no live row to restore from.

    Locates the row with the SAME matcher `cmd_collection_check` uses (masthead
    aliases, the X-Men issue-number split, leading-zero/leading-article
    normalization, the year gate) — not a reimplementation. The row is then
    pinned by `full_title` AND `release_date` together (`_pinned_collection_rows`,
    BUI-254 S1): `full_title` alone can collide across eras of the same issue
    number (e.g. two owned "Hulk #1" rows, 1962 and 2008), so a bare
    `full_title` match could silently touch the wrong one. If the pin still
    matches more than one row (e.g. two genuinely dateless rows sharing a
    title), that's an unresolvable ambiguity — refuse with 409 rather than
    guess via first-match. `in_collection` is a copies-owned count
    (BUI-249/250/251): a row with more than one copy is decremented, a
    single-copy row is removed outright, so deleting one erroneous copy never
    un-owns the others. Pass `dry_run=true` to preview the record that WOULD be
    removed/decremented without mutating the store (the same dry-run-then-
    confirm shape `/comic:wishlist-add` and `/comic:collection-sync` use) — the
    preview and the real delete share the exact same pinning predicate, so what
    you preview is what you'd delete.

    404 when no owned row matches; 409 when the store was never imported (R11)
    or when the pin is ambiguous.
    """
    series = series.strip()
    issue = issue.strip()
    if not series or not issue:
        raise HTTPException(status_code=422, detail="series and issue must both be non-empty")

    _ensure_collection_store()
    _require_imported_collection()

    try:
        check = cmd_collection_check(series=series, issue=issue, variant=variant, year=year)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
    if check["match_status"] != "in_collection":
        raise HTTPException(
            status_code=404,
            detail=f"'{series} #{issue}' is not in the collection — nothing to remove",
        )
    full_title = check["full_title_matched"]
    # Normalize "" to None here, once: cmd_collection_check sets
    # matched_release_date straight from the matched row's release_date, which
    # can be "" (not just missing) for an owned-but-dateless row — the year
    # gate fail-opens on an empty date, so such a row still reports
    # in_collection. _pinned_collection_rows folds the ROW side's
    # release_date the same "" -> None way; if this side stayed unnormalized
    # ("" != None), the pin would reject the very row cmd_collection_check
    # just confirmed is owned, 404ing a legitimately deletable dateless row.
    matched_release_date = check.get("matched_release_date") or None

    cache = CollectionCache()

    if dry_run:
        # Read-only preview: no lock, no mutation, no audit entry. Re-reads
        # rather than reusing the check above so the previewed record reflects
        # the store at read time, not a snapshot from before this request.
        payload = cache.load()
        candidates = _pinned_collection_rows(payload.get("comics", []), full_title, matched_release_date)
        if not candidates:
            raise HTTPException(
                status_code=404,
                detail=f"'{series} #{issue}' is not in the collection — nothing to remove",
            )
        if len(candidates) > 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"ambiguous match: {len(candidates)} owned rows share full_title="
                    f"{full_title!r} and release_date={matched_release_date!r} — "
                    "refusing to guess which one to remove"
                ),
            )
        row = candidates[0]
        copies = row.get("in_collection") or 0
        return {
            "status": "preview",
            "action": "would_decrement" if copies > 1 else "would_remove",
            "would_remove": dict(row),
            "remaining_copies": copies - 1 if copies > 1 else 0,
        }

    # Cheap no-lock pre-check: refuse an ambiguous pin BEFORE touching
    # cache.apply(), so a refused (409) delete is a true no-op — it doesn't
    # rotate the .bak ring or rewrite last_writer metadata for a delete that
    # never actually happened. Repeated ambiguous calls would otherwise churn
    # the backup ring and could evict older, distinct backups. The in-`_mutate`
    # ambiguity guard below stays as the authoritative, race-safe backstop: if
    # the store changes between this pre-check and the lock (e.g. a second
    # copy of the same dateless row is imported in between), that guard still
    # catches it before any row is touched.
    if len(_pinned_collection_rows(cache.load().get("comics", []), full_title, matched_release_date)) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"ambiguous match: multiple owned rows share full_title="
                f"{full_title!r} and release_date={matched_release_date!r} — "
                "refusing to guess which one to remove"
            ),
        )

    removed: dict[str, Any] | None = None
    action: str | None = None
    remaining_copies = 0
    ambiguous_count = 0

    def _mutate(payload: dict[str, Any]) -> None:
        nonlocal removed, action, remaining_copies, ambiguous_count
        comics = payload.get("comics", [])
        candidates = [
            i for i, row in enumerate(comics)
            if _is_pinned_collection_row(row, full_title, matched_release_date)
        ]
        if len(candidates) > 1:
            ambiguous_count = len(candidates)
            return
        if not candidates:
            return
        i = candidates[0]
        row = comics[i]
        removed = dict(row)
        copies = row.get("in_collection") or 0
        if copies > 1:
            row["in_collection"] = copies - 1
            action = "decremented"
            remaining_copies = copies - 1
        else:
            del comics[i]
            action = "removed"

    cache.apply(_mutate, command="collection-delete")

    if ambiguous_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"ambiguous match: {ambiguous_count} owned rows share full_title="
                f"{full_title!r} and release_date={matched_release_date!r} — "
                "refusing to guess which one to remove"
            ),
        )

    if removed is None:
        # Lost a race with a concurrent write between the check above and the
        # locked apply() call — the row was already gone by the time the lock
        # was acquired.
        raise HTTPException(
            status_code=404,
            detail=f"'{series} #{issue}' is not in the collection — nothing to remove",
        )

    # Log the full removed record so a mistaken deletion can be manually
    # reversed via record-win/re-import even though this is a hard delete with
    # no tombstone row to restore from.
    cache.append_audit({
        "type": "collection_delete",
        "ts": datetime.now(timezone.utc).isoformat(),
        "command": "collection-delete",
        "details": {"query": {"series": series, "issue": issue}, "action": action, "removed": removed},
    })

    return {"status": "ok", "action": action, "removed": removed, "remaining_copies": remaining_copies}


@router.post("/api/comics/wish-list")
async def api_wish_list_add(req: WishListAddRequest):
    """Append an issue to the wish-list on the server (R7).

    BUI-47 semantics carry over: a server-side wish-list append is still
    overwritten by the next full import unless exported to LOCG first.

    BUI-130 (Part 3): reject a title already in the collection with 409 — the
    same guard ``/comic:wishlist-add`` enforces, now at the API boundary so
    anything that bypasses the skill can't create the BUI-122 conflict. Parsing
    failures and stores that can't verify ownership fail open (the export-side
    BUI-122 fix is the real safety net); pass ``force=true`` to override
    intentionally (a different printing/variant).
    """
    _ensure_collection_store()
    if not req.force:
        parsed = _split_wish_list_name(req.title)
        if parsed is not None:
            series, issue = parsed
            # BUI-184/BUI-197: forward the caller-supplied per-issue cover year
            # (req.year) so cmd_collection_check can catch a book stored under its
            # base masthead. As of BUI-197 the masthead alias is resolved via
            # owned_match_keys, which is year-free, so the guard now fires WITH or
            # WITHOUT a year (e.g. "The Mighty Thor #154" → owned "Thor #154"
            # blocks even when req.year is None) — strictly safer. When a year IS
            # supplied it still tightens the match (a wrong-era owned row is
            # rejected by the release-date filter, so a wrong year fails OPEN). The
            # BUI-129 trap (forwarding a series START year that hides owned mid-run
            # issues) is the CALLER's responsibility: WishListAddRequest.year is
            # documented as the per-issue cover year, never year_began.
            try:
                check = cmd_collection_check(series=series, issue=issue, year=req.year)
            except RuntimeError:
                check = None  # store unavailable → fail open, don't block the add
            if check is not None and check["match_status"] == "in_collection":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{req.title}' is already in the collection "
                        f"({check['full_title_matched']}). Wish-listing an owned book "
                        "risks deleting it on the next sync (BUI-122). Pass force=true "
                        "to override."
                    ),
                )
    result = cmd_wish_list_add(req.title)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.delete("/api/comics/wish-list")
async def api_wish_list_remove(title: str | None = None):
    """Remove an issue from the wish-list on the server (BUI-128).

    Mirrors `locg wish-list remove`. Uses a `title` query param rather than a
    request body — cleaner REST for DELETE, and lets the caller hit
    `DELETE /api/comics/wish-list?title=...`. Replaces the old SSH-into-the-Mac-
    Mini-and-run-`locg wish-list remove` workaround that broke the
    server-as-source-of-truth model.

    Status codes: 422 for a blank title, 404 when the title isn't present (or the
    wish-list was never imported), 200 on a successful removal. Like the POST
    append, a removal is overwritten by the next full import unless pushed to
    LOCG first.
    """
    _ensure_collection_store()
    result = cmd_wish_list_remove(title or "")
    if "error" in result:
        # A blank title is a malformed request (422); every other error here
        # means the title — or the wish-list cache itself — wasn't found (404).
        is_blank = "non-empty" in result["error"]
        raise HTTPException(
            status_code=422 if is_blank else 404, detail=result["error"]
        )
    return result


# ---------------------------------------------------------------------------
# BUI-113: seller-scan seen-tracking. Lets seller_scan.py default to surfacing
# only wish-list matches it hasn't shown before. Provider-neutral, server-owned
# so the MacBook and Mac Mini share one memory (a JSON cache would diverge —
# the exact failure that motivated the ticket). Best-effort on the client: a
# failed call only costs a duplicate, never a hidden match.
# ---------------------------------------------------------------------------


@router.get("/api/comics/seller-scan/seen")
async def api_seller_scan_seen(request: Request, seller: str | None = None):
    """Return item_ids already surfaced by a seller scan as ``{"item_ids": [...]}``.

    ``seller`` is an optional filter; omitted returns every seen id (item_ids
    are globally unique on eBay, so a default scan filters across all sellers).
    """
    db = request.app.state.db
    return {"item_ids": sorted(get_seen_item_ids(db, seller))}


@router.post("/api/comics/seller-scan/seen")
async def api_seller_scan_seen_add(request: Request, req: SellerScanSeenRequest):
    """Mark item_ids as surfaced. Idempotent — re-marking keeps the first
    first_seen_at. Returns ``{"marked": <newly-inserted count>}``."""
    db = request.app.state.db
    inserted = mark_items_seen(db, req.item_ids, req.seller)
    return {"marked": inserted}


# ---------------------------------------------------------------------------
# BUI-121: collection-wins seen-tracking. Lets /comic:collection-add skip WON
# snipes already processed in a prior run rather than re-POSTing them all and
# relying on the server's already-owned dedup. Best-effort on the client: a
# failed call only costs a duplicate POST (dedup still catches it), never a
# skipped win.
# ---------------------------------------------------------------------------


@router.get("/api/comics/collection/record-win/seen")
async def api_collection_wins_seen(request: Request):
    """Return item_ids already processed by /comic:collection-add as ``{"item_ids": [...]}``.

    Used at the start of a collection-add run so the skill can skip wins that
    were recorded on a previous run.
    """
    db = request.app.state.db
    return {"item_ids": sorted(get_collection_wins_seen(db))}


@router.post("/api/comics/collection/record-win/seen")
async def api_collection_wins_seen_add(request: Request, req: CollectionWinsSeenRequest):
    """Mark win item_ids as processed. Idempotent — re-marking keeps the first
    first_seen_at. Returns ``{"marked": <newly-inserted count>}``."""
    db = request.app.state.db
    inserted = mark_collection_wins_seen(db, req.item_ids)
    return {"marked": inserted}
