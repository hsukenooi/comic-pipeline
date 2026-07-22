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
from fastapi.encoders import jsonable_encoder
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
    get_first_party_outcomes,
    calibration_report,
    DEFAULT_OUTCOME_GRADE_WINDOW,
    DEFAULT_OUTCOME_RECENCY_DAYS,
    DEFAULT_CALIBRATION_MIN_LOSSES,
)
from gixen_overlay.locg_lookup import resolve_year_and_locg
from gixen_overlay.models import (
    UpsertComicRequest,
    LocgLinkRequest,
    LinkFmvRequest,
    VerifyRequest,
    WishListAddRequest,
    WishListAddBatchRequest,
    RecordWinCommitRequest,
    EraEvidenceRequest,
    CollectionRestoreRequest,
    CollectionRemediateDeleteRequest,
    CollectionRemediateSetCopiesRequest,
    SellerScanSeenRequest,
    CollectionCheckBatchRequest,
    SeriesNameResolveRequest,
)
from gixen_overlay.title_parser import parse_title
from server.db import (
    get_bid_by_item_id,
    resolve_server_dir,
    TOMBSTONE_STATUSES_SQL,
    write_transaction,
)
from server.main import (
    _ensure_fresh_sync,
    iso_to_relative,
    _spawn_fallback_task,
    _get_db_path,
    _write_locked,
)

# BUI-91/92: the overlay wraps locg-cli's existing collection + wish-list logic
# (the accumulated matcher with its four documented bugfixes, plus the three
# write paths) behind /api/comics/* instead of porting any of it to SQL. These
# imports prove the locg workspace dependency resolves (exercised by the
# workspace-imports canary).
from locg.collection_cache import CollectionCache, collection_backups_root
from locg.collection_io import MAX_XLSX_BYTES
from locg.commands import (
    _decrement_or_remove,
    _split_wish_list_name,
    cmd_collection_check,
    collection_check_reports_owned,
    cmd_collection_export,
    cmd_collection_import,
    cmd_collection_record_win,
    cmd_collection_record_win_era_evidence,
    cmd_collection_remediate_delete,
    cmd_collection_remediate_set_copies,
    cmd_collection_series_names,
    cmd_collection_series_names_resolve,
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
    for any machine that hasn't run the BUI-220/BUI-463 dir migration yet —
    see ``resolve_server_dir``). The
    directory is named neutrally, not "locg" (R1: "the path is not named for
    LOCG"). An explicitly-set ``LOCG_DATA_DIR`` always wins, so the Mac Mini
    launch env and the tests (which point it at a tmp dir) both override this
    default.

    BUI-490: the early return below never re-derives the store from a changed
    ``DB_PATH`` once ``LOCG_DATA_DIR`` is set — this is safe because ``DB_PATH``
    is immutable for the life of a server process. ``server.main.lifespan()``
    reads ``os.getenv("DB_PATH", ...)`` exactly once at startup into the
    module-global ``_db_path`` (see BUI-408's comment there), and no request
    handler anywhere in this codebase ever writes ``os.environ["DB_PATH"]`` —
    it is read-only after process start (confirmed via
    ``grep -rn "DB_PATH"`` across the workspace: every non-test write site is
    the one-time lifespan assignment; all others are reads). A single server
    process therefore only ever resolves one store directory, computed on the
    first collection request and reused for the rest of the process's life —
    there is no "mid-process DB_PATH change" for this function to miss. (Test
    suites use ``monkeypatch.setenv``/``delenv``, which pytest scopes to one
    test and reverts afterward — never a live mutation visible to a running
    app instance.) A future multi-DB or config-reload mode would need to
    revisit this.
    """
    if os.environ.get("LOCG_DATA_DIR", "").strip():
        return
    db_path = Path(os.environ.get("DB_PATH") or (resolve_server_dir() / "db.sqlite"))
    store = db_path.parent / "collection-store"
    store.mkdir(parents=True, exist_ok=True)
    os.environ["LOCG_DATA_DIR"] = str(store)


def _collection_backups_root() -> Path:
    """Directory holding durable named collection-store snapshots (BUI-433).

    Formula lifted into ``locg.collection_cache.collection_backups_root``
    (BUI-471) — it used to be hand-duplicated here and in
    ``locg.commands._backfill_backup_dir``. Can only be shared in this
    direction (the overlay depends on locg-cli as a workspace package, never
    the reverse), so locg-cli is where the shared formula lives.
    """
    _ensure_collection_store()
    return collection_backups_root(Path(os.environ["LOCG_DATA_DIR"]))


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


@router.get("/api/comics/outcomes")
async def api_comics_outcomes(
    request: Request,
    grade: float,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
    window: float = DEFAULT_OUTCOME_GRADE_WINDOW,
    days: float = DEFAULT_OUTCOME_RECENCY_DAYS,
):
    """BUI-286: the user's own resolved auctions for a (comic, grade) window.

    Feeds `apps/fmv`'s first-party-comp merge (Issue A) — comic-fmv calls this
    over HTTP (it has no direct DB access; the DB lives on the comics server)
    the same way it already round-trips `/api/comics` for cache reuse and
    upsert. Deliberately provider-neutral (never `/locg/*`) per the overlay's
    endpoint convention, and deliberately reusable as-is by the later
    loss-vs-FMV calibration report (Issue C) so "a resolved auction" is
    defined exactly once.

    Comic resolution: `locg_id` (+ optional `locg_variant_id`) when given,
    else `title` + `issue` (+ optional `year`) — same as `/api/comics`.

    Always both WON and LOST (R2/KTD-3) — see `get_first_party_outcomes` for
    why there is no parameter to narrow this to wins alone.
    """
    db = request.app.state.db
    rows = get_first_party_outcomes(
        db,
        grade=grade,
        title=title,
        issue=issue,
        year=year,
        locg_id=locg_id,
        locg_variant_id=locg_variant_id,
        window=window,
        days=days,
    )
    return [dict(r) for r in rows]


@router.get("/api/comics/calibration")
async def api_comics_calibration(
    request: Request,
    days: float = DEFAULT_OUTCOME_RECENCY_DAYS,
    min_losses: int = DEFAULT_CALIBRATION_MIN_LOSSES,
):
    """BUI-288 (Issue C): loss-vs-FMV calibration report — DIAGNOSTIC ONLY.

    Ranks priced (comic, grade) books whose recent LOSSES are clearing above
    `fmv.high`, so a human can decide which ones to recompute `comic-fmv` for.
    This endpoint performs **no writes** — it is a read-only aggregate over
    `get_first_party_outcomes`'s "a resolved auction" definition (see
    `calibration_report` in `db.py`) plus each book's own `fmv.high`.

    **The ranking key is overshoot vs `fmv.high`, never raw win/loss rate** —
    losing is the intended outcome of the bid haircut, so a high loss count
    alone is not surfaced; only *persistently clearing above fmv.high* is.
    See `calibration_report`'s docstring for the full rationale (R4/R5 in the
    auction-outcome-feedback plan) before changing this endpoint's shape.

    `min_losses` (default 2, FIX 3) requires a (comic, grade) to have lost at
    least this many times in-window before it can surface at all — a single
    high-overshoot loss is a bidding-war outlier, not a persistent pattern.

    Consumed by the `/comic:calibration-report` skill, which curls this
    endpoint and renders the ranked table — mirroring how `wishlist-sellers`
    and `seller-scan` are thin CLI/skill layers over server-owned aggregates.
    """
    db = request.app.state.db
    return calibration_report(db, days=days, min_losses=min_losses)


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

    # BUI-408 (Stage 1 of BUI-400's shared-connection isolation rollout): the
    # whole function is await-free (no network call — see BUI-24/25's
    # docstring), so the entire resolve-then-write sequence below — including
    # the auto-create upserts (upsert_comic/upsert_fmv/link_fmv_to_bid), which
    # used to land + self-commit directly on the shared singleton `db`
    # (app.state.db) with NO lock, same as the final UPDATE — now runs on ONE
    # short-lived write_transaction() connection under the same app-wide
    # _write_lock every gixen-cli writer uses (design doc finding 1's second
    # lock-free writer, alongside _sync_loop -> _sync_gixen). Bundling
    # read-decide-write into one transaction is also strictly safer than the
    # split that used to exist: upsert_comic/upsert_fmv/link_fmv_to_bid each
    # self-commit independently (gixen_overlay/db.py), so a failure partway
    # through used to leave a created comic/fmv committed even though the
    # overall link never completed; a raised HTTPException inside this block
    # now rolls back the whole partial sequence instead.
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            if req.issue is not None:
                # Find an fmv whose comic has the requested issue, linked to this bid
                match = wconn.execute(
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
                    primary = get_primary_fmv_for_bid(wconn, bid_row["id"])
                    if primary is None:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Bid {item_id} has no primary fmv; cannot infer series/year "
                                "for auto-create. Run extract-comics first or use cli.py add."
                            ),
                        )
                    target_comic_id = upsert_comic(
                        wconn,
                        title=primary["title"],
                        issue=req.issue,
                        year=primary["year"],
                    )
                    # Create an fmv stub at the primary's grade for the lot issue
                    new_fmv_id = upsert_fmv(wconn, target_comic_id, primary["grade"])
                    link_fmv_to_bid(wconn, bid_row["id"], new_fmv_id, is_primary=False)
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
                fmv_row = wconn.execute(
                    "SELECT comic_id FROM fmv WHERE id=?", (primary_fmv_id,)
                ).fetchone()
                target_comic_id = fmv_row["comic_id"] if fmv_row else None

            if target_comic_id is None:
                raise HTTPException(status_code=500, detail="Could not resolve target comic")

            wconn.execute(
                """
                UPDATE comics
                SET locg_id = ?,
                    locg_variant_id = COALESCE(?, locg_variant_id)
                WHERE id = ?
                """,
                (req.locg_id, req.locg_variant_id, target_comic_id),
            )

            # Read-back also happens on `wconn`, INSIDE the block — not on
            # the shared `db` afterward: `db` can have its own open
            # transaction from an unrelated in-flight _sync_gixen cycle
            # (commit-free DML, then an await, per its own design), which
            # pins `db` to a snapshot that predates this commit — a `db`
            # read right after would risk returning stale data for the
            # comic this just wrote.
            row = wconn.execute(
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
    verdict → user-visible message themselves. Likewise (BUI-507) each result
    carries a `guidance` string — the one-line, per-verdict advice that used
    to live only in `verify.md`'s ORCHESTRATOR NOTES — so callers (the
    standalone `/comic:verify` skill and `/comic:buy` Step 6 via add-batch's
    `--verify`) can surface it without reading that file or re-deriving their
    own copy of the mapping.
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


# BUI-507: single source of truth for per-verdict guidance. Previously this
# mapping was duplicated in verify.md's ORCHESTRATOR NOTES § Per-verdict
# guidance for /comic:buy Step 6 to read; now the endpoint emits it directly
# and both /comic:buy Step 6 and standalone /comic:verify render the same
# server-provided string instead of maintaining their own copy.
_VERDICT_GUIDANCE: dict[str, str] = {
    "fmv_stub": "Run `/comic:fmv` for this comic at the missing grade(s).",
    "no_fmv_at_grade": (
        "The bid's grade doesn't have an FMV row yet. Run `/comic:fmv` at "
        "this grade."
    ),
    "no_comic": (
        "No comic linked. Run `POST /api/extract-comics` or re-run "
        "`/comic:snipe-add` with `--locg-id` set."
    ),
    "partial": (
        "Junction or `bids.fmv_id` is out of sync. Surface to user for "
        "manual reconciliation."
    ),
    "no_bid": (
        "Snipe never landed in the DB. Confirm `COMICS_SERVER_URL` was set "
        "during `/comic:snipe-add` and the snipe is on Gixen."
    ),
    "fully_linked": "",
}


def _guidance_for(verdict: str, flag_reason: str | None = None) -> str:
    """Return the one-line guidance string for a verdict.

    `needs_manual` is templated (it embeds the row's `flag_reason`), so it's
    built here rather than stored in `_VERDICT_GUIDANCE`. Every other verdict
    `_verify_one` can emit — including `fully_linked`, which maps to `""` —
    has a static entry in that dict; there is no verdict this function can
    silently return `None` for.
    """
    if verdict == "needs_manual":
        return (
            f"This book is flagged `needs_manual` (reason: `{flag_reason}`) — "
            "its comp pool can't be auto-priced. Hand-price it via grade-curve "
            "interpolation or the CGC proxy (see `docs/conventions/fmv-math-spec.md` "
            "§7/§7a), or skip. Do NOT re-run `/comic:fmv` — it will just re-flag it."
        )
    return _VERDICT_GUIDANCE[verdict]


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
        return {**base, "verdict": "no_bid", "missing": ["bids row"],
                "guidance": _guidance_for("no_bid")}

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
                "bid_fmv_id": bid["fmv_id"],
                "guidance": _guidance_for("no_comic")}

    # We have a candidate comic. Did we match on grade?
    if grade is not None and match["grade"] != grade:
        return {**base, "verdict": "no_fmv_at_grade",
                "missing": [f"fmv row at grade {grade}"],
                "comic_id": match["comic_id"],
                "bid_fmv_id": bid["fmv_id"],
                "guidance": _guidance_for("no_fmv_at_grade")}

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
                "bid_fmv_id": bid["fmv_id"],
                "guidance": _guidance_for("needs_manual", match["flag_reason"])}

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
                "bid_fmv_id": bid["fmv_id"],
                "guidance": _guidance_for("fmv_stub")}

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
                "bid_fmv_id": bid["fmv_id"],
                "guidance": _guidance_for("partial")}

    return {**base, "verdict": "fully_linked",
            "comic_id": match["comic_id"],
            "fmv_id": match["fmv_id"],
            "bid_fmv_id": bid["fmv_id"],
            "guidance": _guidance_for("fully_linked")}


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

    BUI-284: with no ``year``, ``match_status`` may be ``ambiguous_cross_volume``
    (the issue is owned under more than one masthead volume and can't be
    disambiguated without a cover year). It passes through as a 200 — the caller
    must flag it and re-check WITH a year, never read it as owned or not-owned.

    BUI-364: every verdict carries ``printing_conflict`` (bool). True means the
    ownership verdict was satisfied by a row whose full_title names a printing
    ("2nd Printing", …) the query never asked for — printings are distinct
    collectibles, so treat the verdict as qualified, not as a plain "owned";
    ``printing_candidates`` lists the same-issue rows across printings with
    their owned/wish state. Advisory only: ``match_status`` is unchanged, and
    the caller must flag it for the user, never auto-flip the verdict (R11).
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

    BUI-284: a per-item verdict may be ``ambiguous_cross_volume`` (owned under
    more than one masthead volume, no year to disambiguate) — same passthrough
    semantics as the single-item endpoint; the caller flags and re-checks it.

    BUI-364: per-item verdicts carry the same ``printing_conflict`` (and, when
    True, ``printing_candidates``) fields the single-item endpoint returns —
    the batch is a fan-out of the same matcher, so the printing-conflation
    surfacing cannot drift between the two. Advisory only; flag, never flip.
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


@router.post("/api/comics/collection/series-names/resolve")
async def api_collection_series_names_resolve(req: SeriesNameResolveRequest):
    """Reconcile one or more query series names to the LOCG catalog spelling
    (BUI-449).

    Thin wrapper over locg-cli's `cmd_collection_series_names_resolve` — the
    ONE tested place the reconciliation (an exact normalized-key match, then a
    confidence-gated fuzzy fallback) lives, beside `cmd_collection_series_names`
    itself. Replaces the `/comic:collection-check` Pattern C /
    `/comic:wishlist-add` Step 3 pattern of pulling the whole catalog array
    into model context and hand-matching it in-model.

    Returns ``{"results": [{"query", "resolved", "match_kind"}, ...]}`` in the
    same order as the request `names`. `resolved` is null (and `match_kind`
    null) when there is no confident match — treat that as "not found", never
    guess a volume from it.
    """
    _ensure_collection_store()
    try:
        return cmd_collection_series_names_resolve(req.names)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc


@router.get("/api/comics/wish-list")
async def api_wish_list(title: str | None = None):
    """Wish-list read (R3) for seller-scan to match against. Returns
    ``[{name, id, ...}]``. A never-imported wish-list (FileNotFoundError) yields
    an empty list: an empty wish-list is a correct, non-dangerous answer (a miss
    only fails to surface a wanted book; it cannot buy a dupe).

    BUI-387: entries are returned verbatim, so a year-scoped wish now also
    carries its per-issue ``year`` (Cover Year) field — seller-scan can read it
    to narrow a match to the wanted volume. It is absent on unstamped (pre-387)
    wishes; a consumer must treat a missing ``year`` as year-blind, never as a
    signal to reject."""
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


def _raise_if_explicit_store_required(result: dict[str, Any], action: str) -> None:
    """BUI-489 backstop, shared by every wish-list write endpoint added/
    touched in this ticket (``api_wish_list_add``, ``api_wish_list_add_
    batch``, ``api_wish_list_remove``, ``api_wish_list_remove_conflicts``).

    ``_ensure_collection_store()`` sets ``LOCG_DATA_DIR`` before every one of
    these calls, so the underlying ``cmd_wish_list_*`` guard this checks for
    is unreachable from here in practice — but a refusal served as a plain
    200 (or folded into a generic per-item/per-title error branch) would read
    as "nothing to report" instead of "the store couldn't be resolved,
    nothing was written". ``action`` is a short present-tense clause
    describing what was refused, e.g. ``"add"``/``"remove"``/``"remove
    conflicts"`` — folded into the 500 detail's ``message``.

    Kept separate from ``_REMEDIATION_STATUS_CODES``/``_remediation_
    response``: the remediation endpoints pass the refusal dict through as
    ``detail`` VERBATIM (a caller reads ``detail["status"]``), while this
    helper reshapes it (a caller reads ``detail["error"]``) — an established,
    pre-existing shape difference (see ``api_collection_import``'s and
    ``api_record_win_commit``'s identical reshaping, BUI-476) this helper
    only consolidates, not unifies across both families.
    """
    if result.get("status") != "explicit_store_required":
        return
    raise HTTPException(
        status_code=500,
        detail=jsonable_encoder({
            **result,
            "error": "explicit_store_required",
            "refusal_detail": result.get("error"),
            "message": f"wish-list {action} refused to resolve a store; nothing was written",
        }),
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
    id, full_title_matched, series_name, release_date}], printing_conflicts:[
    {..., printing_candidates}]}``. ``series_name``/``release_date`` (BUI-266)
    are the matched owned row's BUI-249 provenance — this audit is
    year/variant-blind by necessity (a wish-list name carries no per-issue
    year), so review these before scoping a ``POST .../remove-conflicts`` call
    to catch a decoy cross-era/cross-edition match.

    BUI-372: ``printing_conflicts`` holds matches where the owned row is a
    DIFFERENT printing than the wished title (printings are distinct
    collectibles) — NOT genuine conflicts, so they're never in ``conflicts``
    and ``POST .../remove-conflicts`` can never remove one, scoped or
    unscoped.

    409 when the store was never imported (R11); an absent wish-list yields an
    empty (zero-conflict) result.
    """
    _ensure_collection_store()
    _require_imported_collection()
    try:
        return cmd_wish_list_conflicts()
    except FileNotFoundError:
        return {"total": 0, "checked": 0, "unparseable": [], "conflicts": [], "printing_conflicts": []}


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
    unparseable, scoped, printing_conflicts}`` (mutating calls) or the audit
    shape plus ``dry_run: true`` (preview). Same 409 never-imported guard as
    the audit (R11). Each removed entry carries ``matched_series_name`` /
    ``matched_release_date`` — the BUI-249 provenance of the owned row it was
    matched against.

    BUI-372: ``printing_conflicts`` (never removed — see the audit endpoint)
    is echoed back so a caller can see what was excluded as a distinct-printing
    decoy rather than silently dropped. Naming one in ``names`` is reported as
    an error distinct from "not a conflict at all" — see
    :func:`cmd_wish_list_remove_conflicts`.
    """
    _ensure_collection_store()
    _require_imported_collection()
    names = payload.get("names")
    if names is not None and (
        not isinstance(names, list) or not all(isinstance(n, str) for n in names)
    ):
        raise HTTPException(status_code=422, detail="'names' must be a list of strings")
    confirm = bool(payload.get("confirm", False))

    def _checked(result: dict[str, Any]) -> dict[str, Any]:
        # cmd_wish_list_remove_conflicts checks LOCG_DATA_DIR only (no
        # cache= override — see its docstring); _raise_if_explicit_store_
        # required 500s rather than letting a refusal read as "nothing was
        # a conflict".
        _raise_if_explicit_store_required(result, "remove-conflicts")
        return result

    try:
        if names:
            return _checked(cmd_wish_list_remove_conflicts(names=names))
        if not confirm:
            audit = cmd_wish_list_conflicts()
            return {**audit, "dry_run": True, "removed": [], "removed_count": 0}
        return _checked(cmd_wish_list_remove_conflicts())
    except FileNotFoundError:
        return {
            "removed": [],
            "removed_count": 0,
            # Pre-existing gap (found in review): the normal-path response
            # always includes "scoped"; this fallback previously omitted it.
            "scoped": names is not None,
            "errors": [],
            "remaining": 0,
            "checked": 0,
            "unparseable": [],
            "printing_conflicts": [],
        }


@router.get("/api/comics/collection/export")
async def api_collection_export(push_wishes: bool = False):
    """Export pending-push rows to a LOCG-bulk-import CSV (+ .notes.md), read
    from the server store, for the collection-add round-trip. Returns the file
    *contents* (``csv``, ``notes_md``) plus the counts so the caller can save
    them locally and upload to LOCG.

    Wins-only by default — the default export can never emit ``In Collection=0``
    (the LOCG-delete trigger). ``push_wishes=true`` is the opt-in, owned-safe
    wish mirror (deferred per BUI-208 OQ-3).

    BUI-489: 409s when ``cmd_collection_export`` reports its distinct
    not-imported signal (a store empty on every axis — no comics, no
    wish-list entries, no completed import) rather than reading
    ``result["csv_path"]`` unconditionally, which the not-imported branch
    never populates. Never fires for a legitimately-imported store, nor for
    the record-win-only or wish-only-add flows (both populate real,
    exportable data with ``last_full_import`` still unset) — see
    ``cmd_collection_export``'s own docstring for the exact condition."""
    _ensure_collection_store()
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "locg-bulk-import.csv"
        try:
            result = cmd_collection_export(out_path=str(csv_path), push_wishes=push_wishes)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
        if result.get("status") == "not_imported":
            raise HTTPException(status_code=409, detail=result)
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


@router.post("/api/comics/collection/backup")
async def api_collection_backup():
    """Durable, named snapshot of the collection store, taken locally on the
    Mac Mini (BUI-433).

    Replaces `/comic:collection-sync` Step 1's client-orchestrated
    `cp -r` + `ssh` + `case "$(hostname)"` MacBook/Mac-Mini branching — the
    store lives on the server host, so the server copies it locally instead
    of a client guessing at a hostname pattern to decide whether to SSH in.

    Distinct from `CollectionCache`'s in-store rotating `.bak.0/1/2` ring
    (`_rotate_backups`) that every `apply()` write cycle rotates through and
    evicts after 3 generations — see `_collection_backups_root`. Each call
    creates a new timestamped subdirectory holding a verified byte-for-byte
    copy of `collection.json` + `wish-list.json` (whichever exist).

    Returns `{status, backup_path, files, comics_count, wish_list_count}`.
    Hard-fails with 500 rather than reporting success when the copy
    verification fails OR when the backup captured zero rows across both
    files — an empty backup is indistinguishable from a broken one and must
    never be mistaken for "the store is safely backed up", the invariant
    `/comic:collection-sync` Step 1 depends on before any destructive write.
    """
    _ensure_collection_store()
    backups_root = _collection_backups_root()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest_dir = backups_root / ts
    cache = CollectionCache()
    try:
        result = cache.backup_store(dest_dir)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"backup failed: {exc}") from exc

    if result["comics_count"] == 0 and result["wish_list_count"] == 0:
        raise HTTPException(
            status_code=500,
            detail=(
                f"backup at {result['backup_path']} captured zero rows across "
                "collection.json and wish-list.json — refusing to report "
                "success on an empty/failed backup"
            ),
        )

    return {"status": "ok", **result}


@router.post("/api/comics/collection/restore")
async def api_collection_restore(req: CollectionRestoreRequest):
    """Restore the collection store from a named backup (BUI-433).

    Makes `/comic:collection-sync`'s abort path EXECUTABLE: Steps 3/3b used
    to say "restore from the Step 1 backup" with no actual command behind
    it. `backup_path` must resolve under this server's backups root
    (`_collection_backups_root` — i.e. a path this server itself returned
    from `POST .../collection/backup`); anything else is refused with 422,
    closing off a path-traversal read/write from request input.

    Restores `collection.json` + `wish-list.json` (whichever are present in
    the backup) back onto the live store, each verified byte-for-byte, under
    the same exclusive lock `CollectionCache.apply()` uses so a restore can
    never race a concurrent write. Returns the same shape `backup` does,
    read back from the just-restored live files.
    """
    _ensure_collection_store()
    backups_root = _collection_backups_root().resolve()
    candidate = Path(req.backup_path).expanduser().resolve()
    if backups_root not in candidate.parents:
        raise HTTPException(
            status_code=422,
            detail=(
                "backup_path must be a backup created by POST "
                f".../collection/backup under {backups_root} — refusing to "
                "restore from an arbitrary path"
            ),
        )

    cache = CollectionCache()
    try:
        result = cache.restore_from(candidate)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"restore failed: {exc}") from exc

    return {"status": "ok", **result}


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
        import_result = cmd_collection_import(tmp_path)
        if import_result.get("status") == "explicit_store_required":
            # BUI-476 backstop, symmetric with api_record_win_commit's. Also
            # unreachable (`_ensure_collection_store()` above sets
            # LOCG_DATA_DIR, outside this try), but a refusal served as a 200
            # would slip past `/comic:collection-sync`'s `curl -sf` check and
            # read as a successful reconcile that in fact imported nothing.
            # HTTPException is not in the except tuple below, so this escapes.
            # BUI-491: jsonable_encoder guards against a future non-JSON value
            # (Path/datetime) landing in import_result and raising TypeError at
            # response-render time — which would abort the response instead of
            # delivering this 500 body.
            raise HTTPException(
                status_code=500,
                detail=jsonable_encoder({
                    **import_result,
                    "error": "explicit_store_required",
                    "refusal_detail": import_result.get("error"),
                    "message": (
                        "import refused to resolve a collection store; "
                        "nothing was imported"
                    ),
                }),
            )
        return import_result
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


@router.post("/api/comics/collection/record-win/commit")
async def api_record_win_commit(request: Request, req: RecordWinCommitRequest):
    """Collapse /comic:collection-add Steps 3/3b/5 into one atomic call (BUI-428).

    Takes the SAME two lists the skill used to hand-merge inline
    (`gixen record-win-prep`'s ``wins`` plus Step 2's user-resolved
    ``resolved_reviews``) and concatenates them here — the merge leaves the
    LLM entirely, so it can never again silently drop resolved rows the way
    the skill's old inline ``a + b`` once did. Then, in one call:

      1. Records the merged list via `cmd_collection_record_win` (unchanged
         Metron resolution + BUI-34 already-owned dedup), off the event loop
         via `asyncio.to_thread` — BUI-255 rationale: a throttled Metron
         lookup can sleep up to ~60s, and running that directly on this
         coroutine would stall every other endpoint.
      2. On an unhandled exception or a BUI-137 `partial_failure`, raises a
         500 (same detail shape as the BUI-137/BUI-184 handling below) and
         returns *before* the mark-seen step below runs. Record and mark-seen
         are therefore atomic: nothing is ever marked seen on a partial or
         failed commit.
      3. On full success (no exception, `partial_failure` is falsy), marks
         seen exactly the item_ids THIS call merged and submitted — never a
         re-derivation from a client-side file that might disagree with what
         was actually recorded (the BUI-428 bug: a bad client merge keyed
         record-win and mark-seen off two different sets).
      4. Reads a fresh `cmd_collection_status()` and folds `pending_push_count`
         / `oldest_pending_days` into the response, so the skill's old
         separate Step 5 status re-fetch is no longer needed — `cmd_collection_export`
         (a separate, unavoidably client-side call — see `api_collection_export`)
         never mutates pushed/pending state, so this status read stays accurate
         even before the caller exports.

    Export stays a separate client call (`GET /api/comics/collection/export`):
    it returns `csv`/`notes_md` file contents for the human LOCG upload, which
    cannot move server-side.
    """
    _ensure_collection_store()
    merged_wins = [*req.wins, *req.resolved_reviews]

    try:
        result = await asyncio.to_thread(cmd_collection_record_win, merged_wins)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500, detail=f"collection store unavailable: {exc}"
        ) from exc
    except Exception as exc:
        # BUI-184 backstop. Raised BEFORE any mark-seen call, so an
        # unhandled mid-batch exception — commit state uncertain — never
        # marks anything seen.
        raise HTTPException(
            status_code=500,
            detail={
                "error": "record_win_failed",
                "message": (
                    "record-win raised mid-batch; the commit state is uncertain "
                    "— re-check the collection / re-import before treating any "
                    "of these wins as recorded. Nothing was marked seen."
                ),
                "exception": f"{type(exc).__name__}: {exc}",
            },
        ) from exc

    if result.get("status") == "explicit_store_required":
        # BUI-476 backstop. `_ensure_collection_store()` above guarantees
        # LOCG_DATA_DIR is set, so record-win's store guard is unreachable from
        # here — but the mark-seen below keys off `merged_wins`, NOT off what
        # was actually written, so a refusal that slipped through would mark
        # every win seen having recorded none of them, and BUI-121's seen-set
        # makes that unrecoverable by re-running. Raise before mark-seen rather
        # than trust the reachability argument to survive a future refactor.
        raise HTTPException(
            status_code=500,
            # `result` is spread FIRST: the refusal payload carries its own
            # `error` (the explanatory sentence), and the endpoint's own
            # `error` is the machine-readable code the caller branches on,
            # so ours must win the key collision — but its text is the only
            # thing that says WHICH var is unset and how to set it, so keep
            # it under a non-colliding key rather than dropping it.
            # BUI-491: jsonable_encoder guards against a future non-JSON value
            # (Path/datetime) landing in result and raising TypeError at
            # response-render time — an aborted response instead of this 500
            # body.
            detail=jsonable_encoder({
                **result,
                "error": "explicit_store_required",
                "refusal_detail": result.get("error"),
                "message": (
                    "record-win refused to resolve a collection store; nothing "
                    "was recorded and nothing was marked seen"
                ),
            }),
        )

    if result.get("partial_failure"):
        # BUI-137: some chunk failed to commit partway through. Raised BEFORE
        # any mark-seen call below — never mark seen on a partial commit, so a
        # later re-run still sees these item_ids as unprocessed and retries
        # them rather than silently skipping lost wins.
        # BUI-491: jsonable_encoder guards against a future non-JSON value
        # (Path/datetime) landing in result and raising TypeError at
        # response-render time — an aborted response instead of this 500 body.
        raise HTTPException(
            status_code=500,
            detail=jsonable_encoder({
                "error": "partial_failure",
                "message": (
                    "one or more chunks failed to commit; some wins were NOT "
                    "recorded — nothing was marked seen"
                ),
                **result,
            }),
        )

    # Full success only past this point. The committed set is exactly the
    # item_ids of THIS call's own merged_wins (deduped, preserving order) —
    # not a re-read of any client file — so mark-seen can never key off a
    # different set than what cmd_collection_record_win just processed.
    # Includes skipped-already-owned item_ids on purpose: they were fully
    # processed (matched an owned row), just not re-written, matching the
    # existing BUI-121 "don't reprocess" semantics the old client-side
    # dict.fromkeys(...) derivation already relied on.
    committed_item_ids = list(
        dict.fromkeys(
            item_id
            for w in merged_wins
            if (item_id := str(w.get("item_id") or "").strip())
        )
    )
    db = request.app.state.db
    marked = mark_collection_wins_seen(db, committed_item_ids)

    status = cmd_collection_status()
    return {
        **result,
        "committed_item_ids": committed_item_ids,
        "marked_seen": marked,
        "pending_push_count": status.get("pending_push_count"),
        "oldest_pending_days": status.get("oldest_pending_days"),
    }


@router.post("/api/comics/collection/record-win/era-evidence")
async def api_record_win_era_evidence(req: EraEvidenceRequest):
    """BUI-498: confirm a null-year win's era before auto-recording it.

    ``record_win_prep`` holds EVERY null-year win for review by default
    (BUI-475) because a null-year win's era cannot be confirmed from local
    collection evidence (``resolve_series_for_win`` fails open to the sole
    owned volume — the BUI-421 mis-file). This read-only endpoint recovers the
    safe auto-record path: for each null-year win it returns ``era_confirmed``
    from the ONLY signal that can confirm the era — the issue's Metron cover
    year vs the resolved volume's INDEPENDENT window (see
    ``cmd_collection_record_win_era_evidence``). The client moves an
    era-confirmed win from ``needs_review`` into ``wins``; an unconfirmed one
    stays held.

    Runs off the event loop via ``asyncio.to_thread`` (BUI-255/BUI-428): the
    Metron lookups it makes can sleep on the BUI-465 pacer, and blocking this
    coroutine would stall the single-worker comics server.

    Fails CLOSED end to end: this handler never fabricates a confirmation, and
    ``cmd_collection_record_win_era_evidence`` returns ``era_confirmed=False``
    on every ambiguous/failed path, so a stale Mini (this route absent -> 404),
    an unreachable server, or a Metron outage all degrade to the client's
    hold-all default rather than to a wrong-era auto-record.
    """
    _ensure_collection_store()
    items = [w.model_dump() for w in req.wins]
    return await asyncio.to_thread(cmd_collection_record_win_era_evidence, items)


def _is_pinned_collection_row(
    row: dict[str, Any], full_title: str, release_date: str | None
) -> bool:
    """Pin predicate (BUI-254 S1): is `row` an owned row matching BOTH
    `full_title` and `release_date`? See `_pinned_collection_rows` for the
    full rationale. Shared by the dry-run preview, the pre-check, and the
    locked `_mutate` closure in `api_collection_delete` so all three apply
    the identical classification.
    """
    return bool(
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
        # BUI-454: same copies-owned math as the real mutate below, shared via
        # BUI-427's `_decrement_or_remove` (locg-cli) instead of re-deriving
        # the > 1 check independently here.
        decrements, remaining = _decrement_or_remove(row.get("in_collection") or 0)
        return {
            "status": "preview",
            "action": "would_decrement" if decrements else "would_remove",
            "would_remove": dict(row),
            "remaining_copies": remaining,
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
        # BUI-454: same helper as the dry-run preview above (BUI-427's
        # `_decrement_or_remove`) — one place derives the decrement-vs-remove
        # decision instead of two independent copies-math sites.
        decrements, remaining = _decrement_or_remove(row.get("in_collection") or 0)
        if decrements:
            row["in_collection"] = remaining
            action = "decremented"
            remaining_copies = remaining
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


# ---------------------------------------------------------------------------
# BUI-454: `api_collection_delete` above was backported onto BUI-427's
# `_decrement_or_remove` for the copies-owned math (both the dry-run preview
# and the locked mutate now call it), removing that duplication. It was
# deliberately NOT backported onto `_REMEDIATION_STATUS_CODES`/
# `_remediation_response` below, even though the four HTTP codes happen to
# line up numerically (422/409/404/409): that mapper raises with
# `detail=result` — the WHOLE `cmd_collection_remediate_*` status dict, e.g.
# `{"status": "not_found"}` — whereas `api_collection_delete` raises with a
# human-readable string detail carrying endpoint-specific context (series,
# issue, full_title, release_date, candidate count). Routing this endpoint's
# errors through `_remediation_response` would silently swap that detail
# shape from string to dict and drop the diagnostic content, a real response-
# body change for callers/log-scrapers even though no test currently pins the
# string. It would also couple two endpoints' status vocabularies that are
# only coincidentally numeric matches today (`_REMEDIATION_STATUS_CODES` has
# no entry for `api_collection_delete`'s 500-on-`RuntimeError` case, and any
# future edit to that table for remediate's needs would silently reach into
# this delete path too). Kept as its own inline `raise HTTPException(...)`
# per branch instead.
#
# BUI-427: matcher-BYPASSING remediation. `api_collection_delete` above
# locates its target via `cmd_collection_check` — the SAME masthead-alias /
# X-Men-split / leading-article matcher that breaks down on a volume-mis-
# filed row (it can resolve to the WRONG row, or refuse a legitimate
# deletion with ambiguous_cross_volume -> 404). BUI-424's remediation had to
# hand-roll a one-off `CollectionCache.apply` script keyed on `gixen_item_id`
# to work around both failure modes. These two endpoints are the supported,
# reviewable replacement: they locate the target row by STABLE IDENTITY only
# (`gixen_item_id`, or `full_title`+`release_date`+`source`) via
# `cmd_collection_remediate_delete`/`cmd_collection_remediate_set_copies` —
# never through `cmd_collection_check` — and reuse `CollectionCache.apply`'s
# flock + `.bak` rotation + audit trail exactly as `api_collection_delete`
# does. Status codes are a plain dict->HTTPException mapping so both routes
# share the exact same translation.
# ---------------------------------------------------------------------------

_REMEDIATION_STATUS_CODES: dict[str, int] = {
    "invalid_request": 422,
    "not_imported": 409,
    "not_found": 404,
    "ambiguous": 409,
    # BUI-489 backstop, symmetric with api_collection_import's /
    # api_record_win_commit's explicit_store_required handling. Also
    # "unreachable" — _ensure_collection_store() above sets LOCG_DATA_DIR
    # before either remediation call — but a refusal served as a 200 would
    # read as a successful delete/set-copies that in fact touched nothing.
    "explicit_store_required": 500,
}


def _remediation_response(result: dict[str, Any]) -> dict[str, Any]:
    """Translate a `cmd_collection_remediate_*` result into the HTTP shape.

    `"ok"`/`"preview"` pass through as the 200 body; every other `status`
    value raises the matching `HTTPException` (detail = the result dict, so
    `count`/etc. survive alongside the message) rather than returning 200
    with a body a caller could mistake for success.
    """
    status = result.get("status")
    code = _REMEDIATION_STATUS_CODES.get(status) if isinstance(status, str) else None
    if code is not None:
        raise HTTPException(status_code=code, detail=result)
    return result


@router.post("/api/comics/collection/remediate/delete")
async def api_collection_remediate_delete(req: CollectionRemediateDeleteRequest):
    """Matcher-bypassing delete/decrement of ONE row by stable identity (BUI-427).

    See `CollectionRemediateDeleteRequest` for the identity modes and
    `cmd_collection_remediate_delete` for the full TOCTOU-safe apply shape
    (cheap pre-check, then a locked re-resolve + self-verify inside
    `CollectionCache.apply()`, mirroring `api_collection_delete`/BUI-417).
    """
    _ensure_collection_store()
    try:
        result = cmd_collection_remediate_delete(
            gixen_item_id=req.gixen_item_id,
            full_title=req.full_title,
            release_date=req.release_date,
            source=req.source,
            dry_run=req.dry_run,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
    return _remediation_response(result)


@router.post("/api/comics/collection/remediate/set-copies")
async def api_collection_remediate_set_copies(req: CollectionRemediateSetCopiesRequest):
    """Matcher-bypassing copy-count set/adjust on ONE row by stable identity (BUI-427).

    See `CollectionRemediateSetCopiesRequest` for the identity modes and the
    `in_collection`/`delta` semantics; `cmd_collection_remediate_set_copies`
    for the TOCTOU-safe apply shape. Unlike delete, this never removes the
    row even when the result is 0 copies — `in_collection == 0` is itself a
    valid tracked-but-not-owned state (BUI-249/250/251).
    """
    _ensure_collection_store()
    try:
        result = cmd_collection_remediate_set_copies(
            gixen_item_id=req.gixen_item_id,
            full_title=req.full_title,
            release_date=req.release_date,
            source=req.source,
            in_collection=req.in_collection,
            delta=req.delta,
            dry_run=req.dry_run,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"collection store unavailable: {exc}") from exc
    return _remediation_response(result)


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

    BUI-372: the 409 ``detail`` is a dict (``{error, message, full_title_matched,
    printing_conflict, printing_candidates}``), additive over the prior plain
    string so an existing consumer reading ``detail`` as text still finds it at
    ``detail["message"]``. ``full_title_matched`` reuses the name
    ``cmd_collection_check`` itself uses for this value (also the name the
    batch-check response and the wish-list conflicts audit use) rather than
    minting a new spelling for the same concept. ``printing_conflict``/
    ``printing_candidates`` (BUI-364 shape, straight from ``cmd_collection_check``)
    let a caller tell a genuine duplicate apart from a distinct-printing decoy —
    where ``force=true`` is the
    CORRECT next action, not an override of a real duplicate.

    BUI-285: idempotent. After the owned-guard, an add whose series + issue token
    already exists in the wish-list is a no-op — it returns ``{status: "exists",
    existing, items}`` with 200 instead of appending a duplicate row. A duplicate
    would be double-pushed to LOCG (``wish_rows_for_export``) and would defeat the
    BUI-266 scoped conflict-removal (its name-keyed dict collapses a dup pair,
    leaving one behind). The dedup is name-based (decorated series + issue), so a
    distinct volume of the same masthead still appends, and ``force=true`` bypasses
    it (the escape hatch for a genuinely distinct printing/variant).
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
            # BUI-284: ambiguous_cross_volume counts as owned — with no year
            # (req.year is often None) an owned-under-multiple-volumes book returns
            # ambiguous, and treating it as not-owned would fail this guard open
            # and re-open the BUI-122 data-loss path.
            if check is not None and collection_check_reports_owned(check):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "already_owned",
                        "message": (
                            f"'{req.title}' is already in the collection "
                            f"({check['full_title_matched']}). Wish-listing an owned book "
                            "risks deleting it on the next sync (BUI-122). Pass force=true "
                            "to override."
                        ),
                        "full_title_matched": check["full_title_matched"],
                        # BUI-372: additive — a caller can tell a genuine
                        # duplicate apart from a distinct-printing decoy (where
                        # force=true is the CORRECT action, not an override of
                        # a real duplicate). printing_candidates is None when
                        # printing_conflict is False (BUI-364 shape).
                        "printing_conflict": check.get("printing_conflict", False),
                        "printing_candidates": check.get("printing_candidates"),
                    },
                )

    # BUI-285/BUI-313: idempotency now lives entirely in cmd_wish_list_add's
    # shared _find_duplicate_wish_entry dedup — a series+issue already on the
    # wish-list returns a 200 {status: "exists", ...} no-op instead of a second
    # appended row (which would double-push to LOCG and defeat the BUI-266 scoped
    # conflict-removal). force=req.force threads through so a force=true request
    # bypasses that dedup too — the escape hatch for a genuinely distinct
    # printing/variant that shares series + issue (mirrors the owned-guard's own
    # force bypass above). Passing it is load-bearing: without it, a force add
    # would still be silently no-op'd by the callee's dedup.
    #
    # BUI-387: req.year is now also PERSISTED on the new entry (a separate `year`
    # field, the issue's per-issue Cover Year — never year_began, BUI-129), not
    # just used for the owned-guard above. Stamping it lets the conflicts audit
    # year-scope this wish so a vintage want no longer re-flags against an owned
    # modern volume every audit. req.year is None for an issue with no cover date
    # → an unstamped, year-blind entry (today's behavior, unchanged).
    result = cmd_wish_list_add(req.title, force=req.force, year=req.year)
    # BUI-489 backstop: _ensure_collection_store() above sets LOCG_DATA_DIR
    # before this call, making the guard unreachable from here — but a
    # refusal served as a 200 (or folded into the generic 422 below) would
    # read as a successful add that in fact wrote nothing.
    _raise_if_explicit_store_required(result, "add")
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.post("/api/comics/wish-list/batch")
async def api_wish_list_add_batch(req: WishListAddBatchRequest):
    """Batch wish-list add (BUI-447).

    Accepts a list of ``{title, year?, force?}`` entries — the same shape as
    the single-item ``POST /api/comics/wish-list`` — and adds each one in a
    single HTTP call. Eliminates the per-issue fan-out ``/comic:wishlist-add``
    Step 5 used to do (a 40-issue run was previously 40 sequential POSTs).
    Returns ``{"count": <n>, "results": [{title, status, ...}, ...]}`` in
    request order.

    Each item is run through the EXACT SAME two checks
    ``api_wish_list_add`` applies, in the same order — this is a fan-out, not a
    reimplementation:

    1. The BUI-130 owned-guard (unless the item itself sets ``force=true``): a
       title already in the collection is rejected. Wish-listing a book you
       already own is the BUI-122 data-loss trigger (a later sync exports it
       with ``In Collection=0``, telling LOCG to delete it from the
       collection), so this guard runs PER ITEM and is never bypassed at the
       batch boundary or by any other item's ``force`` flag. Because a batch
       response is a single 200 covering N items, a rejected item can't use
       the HTTP status line the single endpoint uses (409) — it is reported
       inline instead as ``{"status": "owned-409", "message",
       "full_title_matched", "printing_conflict", "printing_candidates"}``
       (same fields the single endpoint's 409 detail carries) so a caller
       distinguishes it from a real add exactly as it would the standalone
       409.
    2. ``cmd_wish_list_add``'s BUI-285 idempotency dedup: a title whose
       series+issue token already exists on the wish-list is a no-op,
       reported as ``{"status": "exists", "existing", "items"}``.

    A successful add is ``{"status": "ok", "added": {...}, "items": <total>}``.
    A per-item failure from ``cmd_wish_list_add`` itself (e.g. a malformed
    non-4-digit ``year``) is reported as ``{"status": "error", "error":
    <message>}`` rather than aborting the whole batch — one bad item must not
    block the other 39.

    Each item goes through ``cmd_wish_list_add``'s own read-check-write (the
    same call the single endpoint makes), so an owned/duplicate title earlier
    in the SAME batch is already reflected on disk by the time a later item is
    checked — no intra-batch blind spot.
    """
    _ensure_collection_store()

    results: list[dict[str, Any]] = []
    for item in req.items:
        if not item.force:
            parsed = _split_wish_list_name(item.title)
            if parsed is not None:
                series, issue = parsed
                try:
                    check = cmd_collection_check(series=series, issue=issue, year=item.year)
                except RuntimeError:
                    check = None  # store unavailable → fail open, don't block the add
                # BUI-284: ambiguous_cross_volume counts as owned — same rule the
                # single endpoint's owned-guard applies.
                if check is not None and collection_check_reports_owned(check):
                    results.append({
                        "title": item.title,
                        "status": "owned-409",
                        "message": (
                            f"'{item.title}' is already in the collection "
                            f"({check['full_title_matched']}). Wish-listing an owned book "
                            "risks deleting it on the next sync (BUI-122). Pass force=true "
                            "to override."
                        ),
                        "full_title_matched": check["full_title_matched"],
                        "printing_conflict": check.get("printing_conflict", False),
                        "printing_candidates": check.get("printing_candidates"),
                    })
                    continue

        result = cmd_wish_list_add(item.title, force=item.force, year=item.year)
        # BUI-489 backstop. Found in code review: unlike every other guarded
        # call site, the plain `"error" in result` branch below would have
        # folded a refusal into an ordinary per-item `{"status": "error",
        # ...}` entry, indistinguishable from e.g. a malformed year — losing
        # the loud "the store couldn't be resolved, nothing was written"
        # signal. Every item in the batch would refuse identically (the
        # guard is a function of env state, not per-item), so this raises
        # once for the whole request rather than reporting N duplicate
        # per-item failures.
        _raise_if_explicit_store_required(result, "batch add")
        if "error" in result:
            results.append({"title": item.title, "status": "error", "error": result["error"]})
        elif result.get("status") == "exists":
            results.append({
                "title": item.title,
                "status": "exists",
                "existing": result["existing"],
                "items": result["items"],
            })
        else:
            results.append({
                "title": item.title,
                "status": "ok",
                "added": result["added"],
                "items": result["items"],
            })

    return {"count": len(results), "results": results}


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
    # BUI-489 backstop. Checked BEFORE the generic "error" in result branch
    # below: that check's blank-vs-not-found split would otherwise misfile
    # this as a plain 404 "not found", which reads as "no such wish", not
    # "the store couldn't be resolved".
    _raise_if_explicit_store_required(result, "remove")
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
# relying on the server's already-owned dedup.
#
# BUI-453: the write side used to be a standalone POST endpoint
# (`api_collection_wins_seen_add`), called by the skill as a separate step
# after recording. BUI-428's `POST .../record-win/commit` replaced that with
# an in-process call to `mark_collection_wins_seen` (see above) keyed off the
# exact item_ids it just committed, so the standalone POST route had no
# remaining HTTP caller and was removed. The GET route below is still live:
# `gixen record-win-prep` (packages/gixen-cli/record_win_prep.py,
# `fetch_seen_ids`) calls it directly to fetch the seen-set BEFORE building a
# new commit payload — do not remove it.
# ---------------------------------------------------------------------------


@router.get("/api/comics/collection/record-win/seen")
async def api_collection_wins_seen(request: Request):
    """Return item_ids already processed by /comic:collection-add as ``{"item_ids": [...]}``.

    Used at the start of a collection-add run (`gixen record-win-prep`) so the
    skill can skip wins that were recorded on a previous run. Still live —
    see the BUI-453 note above before removing.
    """
    db = request.app.state.db
    return {"item_ids": sorted(get_collection_wins_seen(db))}
