"""FMV orchestrator — wires DB cache, ebay-sold-comps, math, and DB upsert together.

Lives separately from fmv_cli.py so it can be tested without invoking Click.
The CLI command in fmv_cli.py is a thin wrapper around fmv_runner.run().

Pipeline per book:
  1. Check Gixen DB for a recent FMV (GET /api/comics?locg_id=&grade=&max_age_days=N)
     — skipped if --force or if the book lacks locg_id/grade
  2. For books needing fresh comps: subprocess to ebay-sold-comps
     (apps/ebay; that command itself caches SerpApi responses)
  3. Run IQR + quartiles + confidence rubric (fmv_math, pure functions)
  4. POST /api/comics to upsert FMV; the gixen-overlay route stamps fmv_updated_at
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import requests

import fmv_math


EBAY_SOLD_COMPS_BIN = "ebay-sold-comps"

# BUI-184: ebay-sold-comps subprocess timeout, scaled with batch size. Each book
# may run up to 3 SerpApi queries at a 15s HTTP timeout, fanned out across a
# thread pool, so a generous per-book budget plus a fixed base.
_SUBPROCESS_TIMEOUT_BASE = 60       # seconds
_SUBPROCESS_TIMEOUT_PER_BOOK = 60   # seconds per book in the batch


# Wish-list caches sometimes carry letter grades (e.g. "VF+") while sold_comps
# resolves a numeric grade. fmv_math.build_pool() does `abs(c["grade"] - target)`
# and silently returns n=0 if `target` is a string, so we coerce here.
_LETTER_GRADE_MAP: dict[str, float] = {
    "NM/M": 9.8, "NM+": 9.6, "NM": 9.4, "NM-": 9.2,
    "VF/NM": 9.0, "VFNM": 9.0,
    "VF+": 8.5, "VF": 8.0, "VF-": 7.5,
    "FN/VF": 7.0, "FN+": 6.5, "FN": 6.0, "FN-": 5.5,
    "VG/FN": 5.0, "VG+": 4.5, "VG": 4.0, "VG-": 3.5,
    "GD/VG": 3.0, "GD+": 2.5, "GD": 2.0,
    "FR": 1.0, "PR": 0.5,
}


def _coerce_grade(value) -> float | None:
    """Return a numeric grade, or None if the value can't be interpreted."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        return float(s)
    except ValueError:
        pass
    return _LETTER_GRADE_MAP.get(s.upper())


# ─── Public entry point ──────────────────────────────────────────────────────

def _fail_mapping(detail: str) -> None:
    """Abort the run loudly on a subprocess result/identity mismatch (BUI-174/187).

    Mapping comps to the wrong comic silently computes a bid cap from another
    book's pool, so this is a hard failure, never a best-effort fallback.
    """
    click.echo(f"Error: {detail}", err=True)
    sys.exit(1)


def run(*, batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool,
        quiet: bool, server_url: str | None,
        grade_window: float | None = None) -> None:
    """Driver for `comic-fmv`. Exits with sys.exit on hard failures.

    `grade_window` (BUI-86) caps how far the comp pool may widen; None uses
    fmv_math's default ceiling. It does not bypass the priceability guards.
    """
    if not server_url:
        click.echo("Error: COMICS_SERVER_URL must be set. The fmv command "
                   "needs the server for cache reuse and DB upsert.", err=True)
        sys.exit(1)

    if not batch_path:
        click.echo("Error: --batch is required (path or '-' for stdin).", err=True)
        sys.exit(2)

    books = _read_batch(batch_path)
    if not books:
        click.echo("Empty batch.", err=True)
        sys.exit(0)

    # 1. DB cache reuse (skipped if --force)
    cached, needs_compute = _split_by_db_cache(
        books, server_url=server_url, max_age_days=max_age_days, force=force,
    )

    # 2. Fetch comps for the books that need fresh compute
    fresh_results: list[dict] = []
    if needs_compute:
        fresh_results = _fetch_comps(needs_compute, force=force)

    # 3. Run FMV math + DB upsert for fresh books, mapped back to inputs by an
    #    explicit id — never by list position (BUI-174/187). The subprocess fans
    #    out across a ThreadPoolExecutor and a dropped/reordered result would
    #    otherwise upsert comic A's comps onto comic B (wrong bid cap, silent).
    #    Each result echoes its _req_id (the original input index); we require an
    #    exact 1:1 id round-trip and fail loud on any mismatch rather than guess.
    fresh_fmvs: dict[int, dict] = {}
    if needs_compute:
        sent_ids = [b["_idx"] for b in needs_compute]
        results_by_id: dict[int, dict] = {}
        for result in fresh_results:
            rid = (result.get("input") or {}).get("_req_id")
            if rid in results_by_id:
                _fail_mapping(
                    f"ebay-sold-comps returned a duplicate result id ({rid!r})."
                )
            results_by_id[rid] = result  # type: ignore[index]  # rid is int at runtime; .get() returns Any
        sent_set = set(sent_ids)
        missing = [i for i in sent_ids if i not in results_by_id]
        unexpected = [k for k in results_by_id if k not in sent_set]
        if len(fresh_results) != len(needs_compute) or missing or unexpected:
            _fail_mapping(
                f"ebay-sold-comps result/identity mismatch: sent "
                f"{len(needs_compute)} books, got {len(fresh_results)} results; "
                f"missing ids={missing}, unexpected ids={unexpected}. Refusing to "
                "map comps positionally (would price the wrong comic)."
            )
        for idx in sent_ids:
            fresh_fmvs[idx] = _compute_and_upsert_one(
                results_by_id[idx], books[idx],
                server_url=server_url, grade_window=grade_window,
            )

    # 4. Stitch cached + fresh in input order
    final = _stitch(books, cached, fresh_fmvs)

    if not quiet:
        _print_table(final)

    if out_path:
        _write_json(out_path, final)


# ─── Step 1 — DB cache reuse ──────────────────────────────────────────────────

def _split_by_db_cache(books: list[dict], *, server_url: str,
                       max_age_days: float, force: bool
                       ) -> tuple[dict[int, dict], list[dict]]:
    """Bucket each book into (cached, needs_compute).

    Returns (cached_by_idx, needs_compute_list). cached_by_idx maps the
    original input index → DB row dict. needs_compute_list preserves only
    the books that need a fresh fetch+compute.
    """
    cached: dict[int, dict] = {}
    needs: list[dict] = []
    for i, book in enumerate(books):
        if force or not book.get("locg_id") or book.get("grade") is None:
            needs.append({"_idx": i, **book})
            continue
        row = _db_lookup(server_url, locg_id=book["locg_id"],
                         grade=book["grade"],
                         locg_variant_id=book.get("locg_variant_id"),
                         max_age_days=max_age_days)
        if row:
            cached[i] = row
        else:
            needs.append({"_idx": i, **book})
    return cached, needs


def _db_lookup(server_url: str, *, locg_id: int, grade: float,
               locg_variant_id: int | None = None,
               max_age_days: float) -> dict | None:
    """Return the freshest matching FMV row, or None if not cached/stale.

    Defensive verification: even if the server returns rows, re-check
    locg_id, grade, AND locg_variant_id match what we asked for. Older server
    versions silently ignore unknown query params (FastAPI behavior), so
    without this check a stale server would happily return ANY row at the
    matching grade and we'd write the wrong comic's FMV onto the input book.

    BUI-139: two variant rows of one issue share the same issue-level locg_id
    (only locg_variant_id differs), so a locg_id+grade match is variant-blind.
    A base cover (locg_variant_id=None) must reuse only a NULL-variant row, and
    a specific variant only its own — re-check it here because an absent query
    param can't express "NULL variant" to the server, only "no filter".
    """
    params: dict = {"locg_id": locg_id, "grade": grade,
                    "max_age_days": max_age_days}
    if locg_variant_id is not None:
        params["locg_variant_id"] = locg_variant_id
    rows = _get_json_or_warn(
        f"{server_url}/api/comics", params=params,
        warn=f"DB cache lookup failed (locg_id={locg_id})", default=None,
    )
    if rows is None:
        return None
    # A stub fmv row (null fmv_low, written by BUI-44 when n=0 comps) links the
    # comic but has no pricing to reuse — don't count it as a cache hit, so the
    # book falls through to a fresh fetch+compute instead of reusing the stub.
    rows = [r for r in rows
            if r.get("locg_id") == locg_id and r.get("grade") == grade
            and r.get("locg_variant_id") == locg_variant_id
            and r.get("fmv_low") is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("fmv_updated_at") or "", reverse=True)
    return rows[0]


# ─── Step 2 — Fetch comps via ebay-sold-comps ─────────────────────────────────

def _fetch_comps(books: list[dict], *, force: bool) -> list[dict]:
    """Subprocess to ebay-sold-comps. Returns the parsed result list,
    in the same order as `books`."""
    if shutil.which(EBAY_SOLD_COMPS_BIN) is None:
        click.echo(
            f"Error: '{EBAY_SOLD_COMPS_BIN}' not found on PATH.\n"
            f"Install apps/ebay (e.g. `pip install -e apps/ebay`) so the "
            f"{EBAY_SOLD_COMPS_BIN} entry point is available.",
            err=True,
        )
        sys.exit(1)

    # Strip the orchestrator's _idx but thread a stable correlation id (_req_id,
    # = the original input index) the subprocess echoes back, so run() can map
    # results to inputs by identity instead of fragile list position (BUI-174/187).
    payload = [
        {**{k: v for k, v in b.items() if k != "_idx"}, "_req_id": b["_idx"]}
        for b in books
    ]

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as ftmp:
        json.dump(payload, ftmp)
        in_path = ftmp.name
    out_path = in_path + ".out.json"

    try:
        cmd = [EBAY_SOLD_COMPS_BIN, "--batch", in_path, "--out", out_path, "--quiet"]
        if force:
            cmd.append("--force")
        # BUI-184: bound the child so a hung ebay-sold-comps can't hang comic-fmv
        # forever. Scale with batch size (each book may run up to 3 SerpApi
        # queries at a 15s HTTP timeout, fanned out across a thread pool).
        timeout = _SUBPROCESS_TIMEOUT_BASE + _SUBPROCESS_TIMEOUT_PER_BOOK * len(books)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} timed out after {timeout}s on "
                f"{len(books)} book(s).",
                err=True,
            )
            sys.exit(1)
        if result.returncode != 0:
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} failed (exit {result.returncode}):\n"
                f"{result.stderr}",
                err=True,
            )
            sys.exit(1)
        # BUI-184: a returncode-0 child can still leave an empty/partial out file
        # (killed between create and write, disk-full). Guard the read+parse and
        # fail loud rather than crash with an opaque JSONDecodeError mid-batch.
        try:
            raw = Path(out_path).read_text()
        except OSError as e:
            click.echo(f"Error: could not read {EBAY_SOLD_COMPS_BIN} output: {e}",
                       err=True)
            sys.exit(1)
        if not raw.strip():
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} exited 0 but wrote no output "
                "(empty results file).",
                err=True,
            )
            sys.exit(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} produced unparseable output: {e}",
                err=True,
            )
            sys.exit(1)
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _get_json_or_warn(url: str, *, params: dict, warn: str, default,
                      timeout: int = 15):
    """GET `url` and return parsed JSON, or `default` on any failure.

    The shared soft-fail HTTP shape for the comics-server read helpers
    (`_db_lookup`, `_fetch_first_party_outcomes`): a transport/HTTP error or a
    non-JSON body warns to stderr (`Warning: {warn}: {err}`) and returns
    `default`, so the caller degrades gracefully (prices as if the lookup found
    nothing) rather than raising.
    """
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        click.echo(f"Warning: {warn}: {e}", err=True)
        return default
    except ValueError:
        return default


# ─── Step 2b — First-party outcomes (BUI-286) ─────────────────────────────────

# Recency window for pulling the user's own resolved auctions back in as
# first-party comps. 180 days is a starting default (per the plan's KTD-2:
# start unweighted, revisit once U2 lands time-decay) — long enough to catch
# a slow-moving collector's last few relevant snipes, short enough that a
# hammer price from years ago doesn't masquerade as "current."
FIRST_PARTY_RECENCY_DAYS = 180


def _fetch_first_party_outcomes(server_url: str, *, target_grade: float,
                                locg_id: int | None = None,
                                locg_variant_id: int | None = None,
                                title: str | None = None,
                                issue: str | None = None,
                                year: int | None = None) -> list[dict]:
    """Pull the user's own resolved auctions for this (comic, grade) window.

    BUI-286 (Issue A) / KTD-1: this is the ONLY call site that reads
    bids→bid_fmvs→fmv→comics for pricing purposes. It always asks the server
    for WON-and-LOST rows together in one request (R2/KTD-3) — the server-side
    query (`get_first_party_outcomes` in gixen-overlay) has no wins-only mode,
    so there is no way for this function to accidentally request wins alone.

    The server scans a generous grade band (matching fmv_math.MAX_GRADE_WINDOW,
    passed explicitly here so the coupling is visible rather than silently
    assumed); the *effective* grade window a first-party comp survives at is
    whatever build_pool's own ±0.5→±2.0 progressive widening lands on once
    these rows are merged into the comp pool (KTD-5) — this function does not
    do its own narrower filtering.

    Returns [] (never raises) on any lookup failure — same soft-fail posture
    as `_db_lookup`: a book with no resolved auctions, or an unreachable
    outcomes endpoint, must price identically to today, not error out.
    """
    if not locg_id and not (title and issue):
        return []
    params: dict = {
        "grade": target_grade,
        "window": fmv_math.MAX_GRADE_WINDOW,
        "days": FIRST_PARTY_RECENCY_DAYS,
    }
    if locg_id is not None:
        params["locg_id"] = locg_id
        if locg_variant_id is not None:
            params["locg_variant_id"] = locg_variant_id
    else:
        params["title"] = title
        params["issue"] = str(issue)
        if year is not None:
            params["year"] = year
    rows = _get_json_or_warn(
        f"{server_url}/api/comics/outcomes", params=params,
        warn=(f"first-party outcomes lookup failed "
              f"(locg_id={locg_id}, title={title!r} issue={issue!r})"),
        default=[],
    )
    return [
        {"price": r["price"], "grade": r["grade"],
         "sold_date": r.get("sold_date", ""), "source": "first_party"}
        for r in rows
        if r.get("price") is not None and r.get("grade") is not None
    ]


# ─── Step 3 — Math + DB upsert ────────────────────────────────────────────────

def _compute_and_upsert_one(result: dict, original_book: dict, *,
                            server_url: str,
                            grade_window: float | None = None) -> dict:
    """Run FMV math + DB upsert for a single book. Returns the assembled result."""
    inp = result.get("input") or {}
    # _req_id is an internal correlation key (BUI-174/187); drop it before it can
    # leak into the upsert body or the emitted result.
    inp.pop("_req_id", None)
    overrides = {k: v for k, v in original_book.items()
                 if k not in ("_idx",) and v is not None}
    # Don't let a wish-list string grade clobber a numeric grade resolved by
    # sold_comps; fmv_math.build_pool() silently returns n=0 on string grades.
    if isinstance(inp.get("grade"), (int, float)) and isinstance(overrides.get("grade"), str):
        overrides.pop("grade", None)
    inp = {**inp, **overrides}
    comps = result.get("comps", [])

    target_grade = inp.get("grade")
    if target_grade is None:
        return {
            "input": inp, "fmv": None, "comp_count_total": len(comps),
            "queries_used": result.get("queries_used", []),
            "db_row": None, "comic_id": None, "fmv_id": None,
            "source": "error",
            "error": "no target grade in input",
        }
    if isinstance(target_grade, str):
        coerced = _coerce_grade(target_grade)
        if coerced is None:
            click.echo(
                f"Warning: could not coerce grade {target_grade!r} to numeric "
                f"for {inp.get('title')} #{inp.get('issue')}; skipping.",
                err=True,
            )
            return {
                "input": inp, "fmv": None, "comp_count_total": len(comps),
                "queries_used": result.get("queries_used", []),
                "db_row": None, "comic_id": None, "fmv_id": None,
                "source": "error",
                "error": f"unrecognized grade string: {target_grade!r}",
            }
        target_grade = coerced
        inp["grade"] = coerced

    # BUI-286: merge the user's own resolved auctions in as first-party comps
    # (KTD-1 — the merge happens at the pool stage, right before compute_fmv,
    # never inside fmv_math's pure math). `comps` itself is left untouched so
    # `comp_count_total` below still reflects the SerpApi/ebay-sold-comps pool
    # only (BUI-143's fetch-error signal keys off that count); first-party rows
    # are added only to the pool actually priced. A book with no resolved
    # auctions gets `first_party == []`, so `pool_comps == comps` and pricing
    # is byte-for-byte what it was before this feature existed.
    first_party = _fetch_first_party_outcomes(
        server_url, target_grade=target_grade,
        locg_id=inp.get("locg_id"), locg_variant_id=inp.get("locg_variant_id"),
        title=inp.get("title"), issue=inp.get("issue"), year=inp.get("year"),
    )
    pool_comps = comps + first_party

    # BUI-51: grade_confidence (photo-coverage confidence from /comic:grade)
    # rides the batch envelope and haircuts the bid cap when low. Absent → no
    # haircut (back-compat for manual / already-graded books).
    # grade_window is None when --grade-window is omitted; compute_fmv treats
    # None as "use the default ceiling", so it threads straight through.
    fmv = fmv_math.compute_fmv(
        pool_comps, target_grade=target_grade,
        grade_confidence=inp.get("grade_confidence"),
        max_window=grade_window,
    )
    # BUI-286: surface the first-party contribution on the returned dict so
    # `_build_notes` can mention it — informational only, fmv_math's output
    # shape is otherwise untouched.
    fmv["first_party_count"] = len(first_party)
    # BUI-44: upsert unconditionally — even with n=0 comps (fmv_low/high None),
    # so the comics row + a stub fmv row are written and comic_id is returned.
    # This lets snipe-add thread --comic-id and verify report no_fmv_at_grade
    # (linked comic, missing pricing) instead of the more severe no_comic.
    upserted = _upsert_fmv(server_url, inp, fmv)
    comic_id, fmv_id = _extract_ids(upserted)

    return {
        "input": inp, "fmv": fmv, "comp_count_total": len(comps),
        "queries_used": result.get("queries_used", []),
        "db_row": upserted, "comic_id": comic_id, "fmv_id": fmv_id,
        "source": "fresh",
    }


def _extract_ids(row: dict | None) -> tuple[int | None, int | None]:
    """Pull comic_id and fmv_id from a /api/comics response.

    Old server versions return only the comics row (no fmv_id) — surface None
    rather than fail, so the orchestrator can still proceed without IDs.
    """
    if not row:
        return None, None
    return row.get("comic_id"), row.get("fmv_id")


def _post_json(url: str, body: dict, *, what: str) -> dict:
    """POST JSON and return the parsed response, failing LOUD (BUI-186 / R11).

    A timeout, connection error, or non-2xx aborts the run with a clear message
    rather than returning None and letting the pipeline proceed on missing data:
    an un-persisted FMV silently breaks the downstream snipe-add FMV link (the
    book is priced but never linked, and verify reports it missing).
    """
    try:
        resp = requests.post(url, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        click.echo(f"Error: {what} failed (POST {url}): {e}", err=True)
        sys.exit(1)


def _upsert_fmv(server_url: str, inp: dict, fmv: dict) -> dict:
    """POST /api/comics with the computed FMV. Returns the row JSON, or aborts
    the run on a failed call (BUI-186) rather than silently returning None."""
    body = {
        "title": inp["title"],
        "issue": str(inp["issue"]),
        "year": inp.get("year"),
        "grade": inp.get("grade"),
        "fmv_low": fmv["fmv_low"],
        "fmv_high": fmv["fmv_high"],
        "fmv_comps": fmv["n"],
        "fmv_confidence": _confidence_to_db_label(fmv["confidence"]),
        "fmv_notes": _build_notes(fmv),
        # BUI-132: post the needs_manual reason as a structured column (not just
        # the `manual_review=<reason>` notes token) so /comic:verify can verdict
        # `needs_manual` and the upsert clears any stale price on a flagged book.
        # None for an auto-priced book → server stores NULL (not flagged) and, on
        # a re-price, clears any prior flag.
        "fmv_flag_reason": fmv.get("flag_reason"),
    }
    if inp.get("locg_id"):
        body["locg_id"] = inp["locg_id"]
    if inp.get("locg_variant_id"):
        body["locg_variant_id"] = inp["locg_variant_id"]
    if inp.get("variant"):
        # BUI-28: variant is part of the comic identity, so base vs Newsstand
        # (etc.) get distinct comic_ids instead of being conflated.
        body["variant"] = inp["variant"]

    return _post_json(
        f"{server_url}/api/comics",
        body,
        what=f"FMV upsert for {inp.get('title')} #{inp.get('issue')}",
    )


def _confidence_to_db_label(label: str) -> str:
    """The DB schema constrains fmv_confidence to {'high','medium','low'}.
    Collapse the finer-grained label set onto that."""
    if label == "HIGH":
        return "high"
    if label in {"MEDIUM-HIGH", "MEDIUM"}:
        return "medium"
    return "low"  # MEDIUM-LOW and LOW


def _build_notes(fmv: dict) -> str:
    parts = [f"window=±{fmv['window']}", f"cv={fmv['cv_pct']}",
             f"label={fmv['confidence']}"]
    # BUI-86: surface grade-span and the manual-pricing flag so a needs_manual
    # stub is legible (distinct from a never-filled n=0 stub) on inspection.
    span = fmv.get("grade_span")
    if span is not None:
        parts.append(f"span={span:g}")
    # BUI-286: surface how many of the priced comps came from the user's own
    # resolved auctions, so a visible FMV shift is traceable to first-party
    # data rather than looking like an unexplained SerpApi swing.
    fp_count = fmv.get("first_party_count") or 0
    if fp_count:
        parts.append(f"first_party={fp_count}")
    flag = fmv.get("flag_reason")
    if flag:
        parts.append(f"manual_review={flag}")
    # BUI-51: surface the bid haircut so the lowered max bid is explained. Skip
    # it for a flagged book — it has no max bid, so a haircut token would be
    # misleading (its LOW label is forced by the flag, not a real haircut).
    factor = fmv.get("bid_factor")
    if not flag and factor is not None and factor < fmv_math.BASE_BID_FACTOR:
        parts.append(
            f"bid_haircut={factor:.2f} (grade_conf={fmv.get('grade_confidence')})"
        )
    return " | ".join(parts)


# ─── Step 4 — Stitch + present ────────────────────────────────────────────────

def _stitch(books: list[dict], cached: dict[int, dict],
            fresh: dict[int, dict]) -> list[dict]:
    """Combine cached and fresh results back into the input order."""
    out: list[dict] = []
    for i, book in enumerate(books):
        if i in cached:
            row = cached[i]
            out.append({
                "input": _input_summary(book),
                "fmv": _fmv_from_db_row(row, book.get("grade_confidence")),
                "comp_count_total": row.get("fmv_comps") or 0,
                "queries_used": [],
                "db_row": row,
                "source": "cached",
            })
        elif i in fresh:
            out.append(fresh[i])
        else:
            out.append({
                "input": _input_summary(book),
                "fmv": None,
                "comp_count_total": 0,
                "queries_used": [],
                "db_row": None,
                "source": "error",
                "error": "no comps fetched and no cache",
            })
    return out


def _input_summary(book: dict) -> dict:
    return {k: book.get(k) for k in
            ("item_id", "title", "issue", "year", "publisher", "grade",
             "grade_confidence", "locg_id", "locg_variant_id", "notes")
            if book.get(k) is not None}


_WINDOW_RE = re.compile(r"window=±\s*([0-9.]+)")


def _window_from_notes(notes: str | None) -> float | None:
    """Recover the grade window the FMV was built at from persisted fmv_notes.

    `_build_notes` writes `window=±{w}` into fmv_notes, so the cached path can
    re-apply the wide-window confidence cap (BUI-182) without trusting the stored
    label alone. Returns None when notes are absent or unparseable.
    """
    if not notes:
        return None
    m = _WINDOW_RE.search(notes)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _fmv_from_db_row(row: dict, grade_confidence: str | None = None) -> dict:
    """Project a gixen-overlay `comics` row back into the fmv dict shape.

    BUI-51: grade_confidence is not *persisted* (KTD6 — no DB column), but a
    cache hit on a freshly photo-graded comic still gets the haircut: we combine
    the request's grade_confidence with the row's stored fmv_confidence at read
    time. Without this, reusing a recent FMV would silently bid a low-confidence
    grade at full 80% — the exact case the haircut exists to prevent.
    """
    fmv_high = row.get("fmv_high")
    fmv_conf = (row.get("fmv_confidence") or "low").upper()
    # BUI-182: re-apply the wide-grade-window confidence cap on reuse. A pool
    # built past WIDE_GRADE_WINDOW can't claim HIGH/MEDIUM-HIGH (fmv_math R7).
    # The fresh path caps the label before it's stored, but a row written by
    # older code (or an external writer) may carry an un-capped confidence; the
    # cached path used to trust the stored label alone, so reuse could bid above
    # what a fresh recompute would allow. Recover the persisted window from
    # fmv_notes and cap to MEDIUM before deriving the bid factor.
    window = _window_from_notes(row.get("fmv_notes"))
    if (window is not None and window > fmv_math.WIDE_GRADE_WINDOW
            and fmv_math._rank(fmv_conf) > fmv_math._CONF_RANK["MEDIUM"]):
        fmv_conf = "MEDIUM"
    factor = fmv_math.bid_factor(fmv_conf, grade_confidence)
    return {
        "n": row.get("fmv_comps") or 0,
        "window": window,
        # BUI-86: shape parity with compute_fmv output. Flagged books are never
        # cache hits (_db_lookup filters null fmv_low), so a cached row is always
        # a priced book — flag_reason stays None — but the keys must exist for
        # downstream readers that iterate the fmv dict uniformly.
        "flag_reason": None,
        "grade_span": None,
        "fmv_low": row.get("fmv_low"),
        "fmv_high": fmv_high,
        "median": None,
        # BUI-182: `is not None`, not a falsy check — a legitimate fmv_high of 0
        # must round to a 0 max_bid, not be nulled out.
        "max_bid": (fmv_math.clean_round(fmv_high * factor)
                    if fmv_high is not None else None),
        "cv": None,
        "cv_pct": "n/a",
        "confidence": fmv_conf,
        "grade_confidence": grade_confidence,
        "bid_factor": factor,
        "trimmed_pool": [],
    }


# ─── I/O helpers ──────────────────────────────────────────────────────────────

def _read_batch(path: str) -> list[dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise click.UsageError("Batch file must contain a JSON array.")
    return data


def _write_json(path: str, data) -> None:
    if path == "-":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        Path(path).write_text(json.dumps(data, indent=2))


def _is_fetch_error(r: dict) -> bool:
    """BUI-143: distinguish a SerpApi fetch FAILURE (quota exhausted / outage)
    from a book that genuinely has zero comps. Both yield n=0 and otherwise look
    identical, so without this an operator can mistake an API outage for "these
    books are illiquid" and bid blind. A fetch error leaves comps empty while
    every query in queries_used carries an 'error'; a real no-comps book ran its
    queries cleanly (no 'error' key)."""
    if r.get("comp_count_total"):
        return False
    queries = r.get("queries_used") or []
    if not queries:
        return False
    return all(q.get("error") for q in queries)


def _print_table(rows: list[dict]) -> None:
    click.echo(f"{'#':>3}  {'Comic':<30} {'Grade':>5}  "
               f"{'FMV':<14} {'Med':>5}  {'n':>3}  {'CV':>5}  "
               f"{'Conf':<12} {'Max bid':>7}  Source")
    click.echo("-" * 110)
    for i, r in enumerate(rows, 1):
        inp = r["input"]
        label = f"{inp.get('title','?')} #{inp.get('issue','?')}"
        grade = inp.get("grade")
        fmv = r.get("fmv") or {}
        if fmv.get("flag_reason"):
            # BUI-86: needs manual pricing — distinct from a genuine no-comps row.
            fmv_str = f"manual:{fmv['flag_reason']}"
            med_str = "—"
            mb_str = "manual"
        elif fmv.get("fmv_low") is not None:
            fmv_str = f"${fmv['fmv_low']}–${fmv['fmv_high']}"
            med_str = f"${fmv.get('median') or '?'}"
            mb_str = f"${fmv.get('max_bid') or '?'}"
        elif _is_fetch_error(r):
            # BUI-143: the SerpApi fetch FAILED for this book — not zero comps.
            # Render distinctly so it isn't mistaken for a legitimately empty pool.
            fmv_str = "fetch-err"
            med_str = "—"
            mb_str = "—"
        else:
            fmv_str = "n/a"
            med_str = "n/a"
            mb_str = "n/a"
        click.echo(
            f"{i:>3}  {label[:30]:<30} {str(grade):>5}  "
            f"{fmv_str:<14} {med_str:>5}  {fmv.get('n','?'):>3}  "
            f"{fmv.get('cv_pct','?'):>5}  "
            f"{fmv.get('confidence','?'):<12} {mb_str:>7}  {r['source']}"
        )

    # BUI-143: a whole batch run during a SerpApi outage/quota-exhaustion would
    # otherwise print every row as a bland 'n/a'. Surface the failure loudly so
    # the operator checks the key/quota and re-runs instead of bidding blind.
    n_fetch_err = sum(1 for r in rows if _is_fetch_error(r))
    if n_fetch_err:
        click.echo(
            f"\n⚠️  {n_fetch_err} book(s) marked 'fetch-err': the SerpApi fetch "
            f"FAILED (quota exhausted or outage), NOT zero comps. Check the "
            f"SerpApi key/quota and re-run — do not treat these as illiquid.",
            err=True,
        )
