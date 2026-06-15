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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import requests

import fmv_math


EBAY_SOLD_COMPS_BIN = "ebay-sold-comps"


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

def run(*, batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool,
        quiet: bool, server_url: str | None,
        grade_window: float | None = None) -> None:
    """Driver for `comic-fmv`. Exits with sys.exit on hard failures.

    `grade_window` (BUI-86) caps how far the comp pool may widen; None uses
    fmv_math's default ceiling. It does not bypass the priceability guards.
    """
    if not server_url:
        click.echo("Error: GIXEN_SERVER_URL must be set. The fmv command "
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

    # 3. Run FMV math + DB upsert for fresh books, keyed by original input idx
    needs_indices = [b["_idx"] for b in needs_compute]
    fresh_fmvs: dict[int, dict] = {}
    for ordinal, result in enumerate(fresh_results):
        if ordinal >= len(needs_indices):
            break
        idx = needs_indices[ordinal]
        fresh_fmvs[idx] = _compute_and_upsert_one(
            result, books[idx], server_url=server_url, grade_window=grade_window,
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
    try:
        resp = requests.get(
            f"{server_url}/api/comics",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        click.echo(f"Warning: DB cache lookup failed (locg_id={locg_id}): {e}",
                   err=True)
        return None
    rows = resp.json()
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

    # Strip the orchestrator's _idx field; ebay-sold-comps doesn't expect it
    payload = [{k: v for k, v in b.items() if k != "_idx"} for b in books]

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as ftmp:
        json.dump(payload, ftmp)
        in_path = ftmp.name
    out_path = in_path + ".out.json"

    try:
        cmd = [EBAY_SOLD_COMPS_BIN, "--batch", in_path, "--out", out_path, "--quiet"]
        if force:
            cmd.append("--force")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} failed (exit {result.returncode}):\n"
                f"{result.stderr}",
                err=True,
            )
            sys.exit(1)
        return json.loads(Path(out_path).read_text())
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ─── Step 3 — Math + DB upsert ────────────────────────────────────────────────

def _compute_and_upsert_one(result: dict, original_book: dict, *,
                            server_url: str,
                            grade_window: float | None = None) -> dict:
    """Run FMV math + DB upsert for a single book. Returns the assembled result."""
    inp = result.get("input") or {}
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

    # BUI-51: grade_confidence (photo-coverage confidence from /comic:grade)
    # rides the batch envelope and haircuts the bid cap when low. Absent → no
    # haircut (back-compat for manual / already-graded books).
    # grade_window is None when --grade-window is omitted; compute_fmv treats
    # None as "use the default ceiling", so it threads straight through.
    fmv = fmv_math.compute_fmv(
        comps, target_grade=target_grade,
        grade_confidence=inp.get("grade_confidence"),
        max_window=grade_window,
    )
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


def _upsert_fmv(server_url: str, inp: dict, fmv: dict) -> dict | None:
    """POST /api/comics with the computed FMV. Returns the row JSON or None."""
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
    }
    if inp.get("locg_id"):
        body["locg_id"] = inp["locg_id"]
    if inp.get("locg_variant_id"):
        body["locg_variant_id"] = inp["locg_variant_id"]
    if inp.get("variant"):
        # BUI-28: variant is part of the comic identity, so base vs Newsstand
        # (etc.) get distinct comic_ids instead of being conflated.
        body["variant"] = inp["variant"]

    try:
        resp = requests.post(f"{server_url}/api/comics", json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        click.echo(f"Warning: DB upsert failed for {inp.get('title')} "
                   f"#{inp.get('issue')}: {e}", err=True)
        return None


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
    factor = fmv_math.bid_factor(fmv_conf, grade_confidence)
    return {
        "n": row.get("fmv_comps") or 0,
        "window": None,
        # BUI-86: shape parity with compute_fmv output. Flagged books are never
        # cache hits (_db_lookup filters null fmv_low), so a cached row is always
        # a priced book — flag_reason stays None — but the keys must exist for
        # downstream readers that iterate the fmv dict uniformly.
        "flag_reason": None,
        "grade_span": None,
        "fmv_low": row.get("fmv_low"),
        "fmv_high": fmv_high,
        "median": None,
        "max_bid": fmv_math.clean_round(fmv_high * factor) if fmv_high else None,
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
