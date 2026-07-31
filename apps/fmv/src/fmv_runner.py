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


def _coerce_year(value) -> int | None:
    """Return a cover year as an int, or None if the value isn't a usable year.

    BUI-565: `/comic:identify` emits `year` as a STRING (`"1976"`), but every
    year test downstream compares it against an int — `_is_vintage`'s
    `isinstance(year, (int, float))` here, and `ebay-sold-comps`' vintage gate
    in the subprocess. Duplicated from `apps/ebay/src/sold_comps.py` rather
    than shared, since comic-fmv shells out to ebay-sold-comps rather than
    importing it (same rationale as `_strip_leading_article` below).

    Returns None on anything unreadable, matching `_coerce_grade`'s contract;
    the caller (`_normalize_book_year`) decides what a None means.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):  # NaN / inf
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return int(float(s))
    except ValueError:
        return None


# ─── BUI-346: title normalization at the buy→FMV handoff ─────────────────────
#
# The working-list `title` field is passed through from the eBay listing name
# (built by the /comic:buy orchestration, not by deterministic code), so it
# routinely carries a leading article ("The Amazing Spider-Man") and/or an
# embedded "#<issue>" that duplicates the separate `issue` field. Left as-is,
# `ebay-sold-comps`' `build_query` doubles it into a malformed phrase like
# `"The Amazing Spider-Man #50 50"`, which returns 0 results on every tier
# (real incident: ASM #50, 2026-07-13 buy run — see BUI-346).
#
# Normalizing HERE, at the top of `run()` before anything else touches the
# batch, is the deterministic enforcement point for the handoff: every book
# in the working list is cleaned up before it reaches DB-cache lookup, the
# ebay-sold-comps subprocess, or the DB upsert (whose `title` column is
# documented as "series name only, no issue number" — see fmv.md). This is
# belt-and-suspenders with `build_query`'s own defense-in-depth normalization
# (apps/ebay/src/sold_comps.py) — duplicated rather than shared, since
# comic-fmv shells out to ebay-sold-comps rather than importing it (see
# CLAUDE.md's "FMV pipeline shells out across package boundaries").

_LEADING_ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)


def _strip_leading_article(title: str) -> str:
    """Strip a leading article ("The"/"A"/"An") from a series title."""
    return _LEADING_ARTICLE_RE.sub('', title or '').strip()


def _strip_embedded_issue(title: str, issue) -> str:
    """Strip an embedded ``#<issue>`` (or a bare trailing issue token) from
    *title* when it duplicates the separate `issue` field. The `(?<!\\d)`
    guard on the trailing-token strip prevents chewing into an unrelated
    longer number (e.g. issue="99" must not touch the "2099" in "X-Men 2099")."""
    issue_str = str(issue).strip() if issue else ""
    if not title or not issue_str:
        return title
    cleaned = re.sub(rf'#\s*{re.escape(issue_str)}\b', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(rf'(?<!\d){re.escape(issue_str)}\s*$', '', cleaned.strip())
    return re.sub(r'\s+', ' ', cleaned).strip()


def _normalize_book_title(book: dict) -> None:
    """Normalize `book["title"]` in place — strip a leading article, then an
    embedded/trailing issue number matching `book["issue"]`. No-op if title
    or issue is missing (fails open: an un-normalizable book is left as-is,
    not dropped)."""
    title = book.get("title")
    issue = book.get("issue")
    if not title or issue is None:
        return
    book["title"] = _strip_embedded_issue(_strip_leading_article(title), issue)


def _normalize_book_year(book: dict) -> None:
    """Coerce `book["year"]` to an int in place (BUI-565).

    Same handoff boundary, and same reasoning, as `_normalize_book_title`: the
    working-list `year` is passed through from `/comic:identify`, which emits
    it as a string. Normalizing here means every downstream consumer — the DB
    cache split, the ebay-sold-comps subprocess payload, the `/api/comics`
    upsert body, and `_is_vintage`'s CGC-proxy gate — sees one type.

    A value that can't be read as a year is deliberately LEFT AS-IS rather
    than dropped. ebay-sold-comps raises on it, and that raise now surfaces as
    a per-book fetch error (see `_compute_and_upsert_one`) — a loud
    needs-manual instead of a silently year-less, silently different search.
    """
    if book.get("year") is None:
        return
    coerced = _coerce_year(book["year"])
    if coerced is not None:
        book["year"] = coerced


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
        grade_window: float | None = None,
        brief: bool = False) -> None:
    """Driver for `comic-fmv`. Exits with sys.exit on hard failures.

    `grade_window` (BUI-86) caps how far the comp pool may widen; None uses
    fmv_math's default ceiling. It does not bypass the priceability guards.

    `brief` (BUI-362) additionally prints one compact JSON object per row
    (see `_brief_row`) after the human table — the linkage/pricing projection
    an orchestrator needs, without re-reading the full `--out` file.
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

    # BUI-346: normalize each book's title at the handoff boundary, before
    # anything downstream (DB cache lookup, subprocess, DB upsert) sees it.
    # BUI-565: same for `year` — /comic:identify emits it as a string.
    for book in books:
        _normalize_book_title(book)
        _normalize_book_year(book)

    # 1. DB cache reuse (skipped if --force). Also separates out hand-priced
    #    rows (BUI-533): a default run must skip them entirely (skipped_hand),
    #    while a --force run proceeds but reports what it's about to overwrite
    #    (force_overwrite_notes). BUI-544: when the provenance lookup itself
    #    FAILS we can't tell hand-priced from not, so the book is skipped too —
    #    but into its own bucket (skipped_lookup_error), never folded into the
    #    hand-priced count.
    (cached, needs_compute, skipped_hand, force_overwrite_notes,
     skipped_lookup_error) = _split_by_db_cache(
        books, server_url=server_url, max_age_days=max_age_days, force=force,
    )
    if force_overwrite_notes:
        _echo_hand_override_notes(force_overwrite_notes, books)

    # 2. Fetch comps for the books that need fresh compute
    fresh_results: list[dict] = []
    if needs_compute:
        # Default hard_fail=True: any fetch failure sys.exits inside _fetch_comps,
        # so a return here is always a real list (the None branch is soft-fail
        # only, used by the BUI-348 proxy rescue).
        fresh_results = _fetch_comps(needs_compute, force=force) or []

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

        # 3b. BUI-348 CGC-proxy rescue: a freshly-computed book that produced NO
        # bid-able number (raw pool too sparse to price, no §7 interpolation) may
        # be a vintage key priceable off the CGC slab ladder. Fetch graded comps
        # for just those books and, where the ladder is trustworthy and
        # high-value, replace the needs_manual result with a proxy band. Books
        # that already priced are never touched (regression-safe by construction).
        _apply_cgc_proxy_rescue(
            fresh_fmvs, books, server_url=server_url, force=force,
        )

        # 3c. BUI-529: promote the CGC-proxy heuristic from rescue-only to an
        # always-on cross-check — also runs on a vintage book that DID price,
        # when the price is thin/uncertain (n<5 or LOW confidence). Never
        # replaces a priced fmv; only flags a large raw-vs-slab divergence.
        # Must run AFTER the rescue so a book the rescue just promoted
        # (source == "cgc-proxy") is correctly skipped.
        _apply_cgc_cross_check(
            fresh_fmvs, books, server_url=server_url, force=force,
        )

    # 4. Stitch cached + fresh + hand-priced-skipped + lookup-error-skipped
    final = _stitch(books, cached, fresh_fmvs, skipped_hand,
                    skipped_lookup_error)

    if not quiet:
        _print_table(final)

    # BUI-533: report the skip count unconditionally (not gated by --quiet) so
    # it surfaces whether the caller reads the human table/summary or only
    # consumes --brief — an operator relying on --brief alone must still learn
    # that N hand-priced rows were left untouched, not silently dropped.
    # BUI-549: stderr, not stdout — stdout is the machine-read JSON-Lines
    # surface under --brief, and this line is not JSON.
    if skipped_hand:
        click.echo(
            f"skipped {len(skipped_hand)} hand-priced row(s) "
            "(use --force to overwrite)",
            err=True,
        )

    # BUI-544: the OTHER skip — reported separately, and worded so it can never
    # be read as the hand-priced skip above. Fail-closed turns a comics-server
    # outage into skipped books; that has to be loud, or a transient failure
    # silently under-computes a batch while looking like normal protection.
    # Goes to stderr (unconditional, like the count above — both routed there
    # since BUI-549 closed the gap on the count above): it is a failure, and
    # stdout is the machine-read surface under --brief.
    if skipped_lookup_error:
        click.echo(
            f"⚠️  skipped {len(skipped_lookup_error)} book(s) because the "
            "comics-server FMV lookup FAILED — NOT because they were "
            "hand-priced. Hand-priced provenance could not be verified, so "
            "these rows were left completely untouched (not priced, not "
            "recomputed, not overwritten) rather than risk overwriting a "
            "hand-priced row. Check the comics server and re-run; --force "
            "does NOT bypass this.",
            err=True,
        )

    if brief:
        _print_brief(final)

    if out_path:
        _write_json(out_path, final)


# ─── Hand-priced row protection (BUI-533) ─────────────────────────────────────

# A hand-priced row's fmv_notes starts with one of these markers — the de-facto
# provenance convention an operator already uses when overriding the CLI's own
# comp-pool result (e.g. Batman #251, 2026-07-24: hand $250-300 anchored on the
# lone real 4.0 sale, after the CLI's pooled $400-425 off 5.0-6.0 comps was
# rejected as pricing the wrong market).
_HAND_PRICE_MARKERS = ("hand §", "hand OVERRIDE")


def _is_hand_priced(notes: str | None) -> bool:
    """True when `notes` (a row's persisted fmv_notes) carries a hand-priced
    provenance marker. A default batch run must never silently recompute over
    a row this returns True for (see `_split_by_db_cache`)."""
    return notes is not None and notes.startswith(_HAND_PRICE_MARKERS)


# ─── Step 1 — DB cache reuse ──────────────────────────────────────────────────

def _split_by_db_cache(books: list[dict], *, server_url: str,
                       max_age_days: float, force: bool
                       ) -> tuple[dict[int, dict], list[dict],
                                  dict[int, dict], dict[int, str],
                                  dict[int, str]]:
    """Bucket each book into (cached, needs_compute, skipped_hand,
    force_overwrite_notes, skipped_lookup_error).

    - cached_by_idx: original input index → a fresh (within max_age_days) DB row.
    - needs_compute_list: books that need a fresh fetch+compute (carries `_idx`).
    - skipped_hand_by_idx (BUI-533): original input index → the existing DB row
      of a hand-priced book that a default (non-`--force`) run must leave
      completely untouched — reused verbatim, like a cache hit, regardless of
      how stale it is. A book only lands here when there was NO fresh
      (max_age_days-eligible) row to reuse normally; a fresh cache hit is
      already never recomputed, hand-priced or not, so it stays in `cached`.
    - force_overwrite_notes_by_idx (BUI-533): original input index → the OLD
      fmv_notes of a hand-priced row that `--force` is about to overwrite, so
      the caller can echo them into the run log before they're lost.
    - skipped_lookup_error_by_idx (BUI-544): original input index → why the
      hand-priced provenance check could not be answered. See below.

    BUI-544 — the provenance check fails CLOSED. The age-unbounded lookup is
    the single chokepoint that decides whether a book is hand-priced, and
    `_db_lookup`'s normal soft-fail posture makes a FAILED lookup look exactly
    like "no row exists" — so a transient comics-server error during that GET
    used to fall through to recompute, and if the server recovered by upsert
    time a default run would overwrite the very hand-priced row the guard
    exists to protect. So a failure of that lookup now means "might be
    hand-priced": the book is skipped outright, never recomputed and never
    overwritten, and reported as its OWN category — a book skipped because the
    server was unreachable is not a book skipped because it was hand-priced,
    and an operator must never mistake an outage for normal protection.

    Fail-closed applies under `--force` too. `--force` is licensed to overwrite
    a hand-priced row, but BUI-533's contract is "overwrite AND echo the old
    notes first" — with the lookup dead we cannot echo, so the overwrite would
    destroy provenance unlogged. Nothing is lost by waiting: a server that
    can't serve the GET generally can't take the upsert either, so the skip
    only bites in exactly the dangerous window (GET fails, POST succeeds).

    The FIRST (freshness-gated) lookup deliberately keeps the soft-fail
    posture: when it fails we simply fall through to the age-unbounded lookup,
    which sees a superset of the same rows, so hand-priced protection is still
    decided correctly. The only cost is possibly recomputing a still-fresh
    machine-priced row — wasteful, never unsafe.
    """
    cached: dict[int, dict] = {}
    needs: list[dict] = []
    skipped_hand: dict[int, dict] = {}
    force_overwrite_notes: dict[int, str] = {}
    skipped_lookup_error: dict[int, str] = {}
    for i, book in enumerate(books):
        eligible = bool(book.get("locg_id")) and book.get("grade") is not None
        if not eligible:
            needs.append({"_idx": i, **book})
            continue

        if not force:
            row = _db_lookup(server_url, locg_id=book["locg_id"],
                             grade=book["grade"],
                             locg_variant_id=book.get("locg_variant_id"),
                             max_age_days=max_age_days)
            if row:
                cached[i] = row
                continue

        # Either --force (cache reuse always bypassed) or the fresh lookup
        # above missed (no row yet, or the existing one is stale enough that
        # normal logic would recompute). Before sending to compute, check
        # whether an EXISTING row — regardless of age — is hand-priced: the
        # protection must hold even on a stale row (that's precisely the
        # 2026-07-24 incident: a batch refresh silently overwrote 10
        # hand-priced rows), and a --force run still needs the OLD notes to
        # echo even though it proceeds to overwrite.
        try:
            existing = _db_lookup(server_url, locg_id=book["locg_id"],
                                  grade=book["grade"],
                                  locg_variant_id=book.get("locg_variant_id"),
                                  max_age_days=None, strict=True)
        except _DbLookupFailed as exc:
            # BUI-544: "don't know" is not "not hand-priced" — fail closed.
            skipped_lookup_error[i] = str(exc)
            continue
        if existing is not None:
            existing_notes = existing.get("fmv_notes")
            if existing_notes is not None and _is_hand_priced(existing_notes):
                if force:
                    force_overwrite_notes[i] = existing_notes
                else:
                    skipped_hand[i] = existing
                    continue

        needs.append({"_idx": i, **book})
    return (cached, needs, skipped_hand, force_overwrite_notes,
            skipped_lookup_error)


def _echo_hand_override_notes(force_overwrite_notes: dict[int, str],
                              books: list[dict]) -> None:
    """BUI-533: `--force` is allowed to overwrite a hand-priced row, but the
    old hand notes must not be silently lost — echo them into the run log
    (stderr, like the other loud BUI-143/529 signals) before the recompute
    that follows replaces them.

    Worded "about to overwrite", not "overwriting": this fires BEFORE the
    fetch+compute step runs, so it's a statement of intent, not of fact. A
    fetch-err on this same book (BUI-536) still leaves the row untouched
    despite this message — the fetch-err guard in `_compute_and_upsert_one`
    always wins, --force notwithstanding.
    """
    for i, old_notes in force_overwrite_notes.items():
        book = books[i]
        label = f"{book.get('title', '?')} #{book.get('issue', '?')}"
        click.echo(
            f"--force: about to overwrite hand-priced row for {label} "
            f"(previous notes: {old_notes!r})",
            err=True,
        )


# BUI-544: `_get_json_or_warn` collapses "the GET failed" into its `default`,
# which for `_db_lookup` is the same None a genuine cache MISS returns. This
# private sentinel keeps the two apart inside `_db_lookup` long enough to raise
# on the failure case — it never escapes the function.
_LOOKUP_FAILED = object()


class _DbLookupFailed(Exception):
    """BUI-544: the comics-server FMV GET itself FAILED (transport error, HTTP
    error, or a non-JSON body) — as opposed to succeeding and finding no row.

    Raised only by `_db_lookup(..., strict=True)`. The default (soft-fail) path
    still collapses both outcomes into None, per the convention documented on
    `_get_json_or_warn`. A caller that must fail CLOSED — i.e. one for which
    "I don't know" is NOT safely equivalent to "there's nothing there" — opts
    in with `strict=True` and handles this exception.
    """


def _db_lookup(server_url: str, *, locg_id: int, grade: float,
               locg_variant_id: int | None = None,
               max_age_days: float | None,
               strict: bool = False) -> dict | None:
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

    BUI-533: `max_age_days=None` disables the freshness filter entirely (the
    `requests` library drops a None-valued param, so the server sees no
    `max_age_days` at all and returns the row regardless of age) — used to
    check an existing row's hand-priced marker even when it's too stale for
    the normal cache-reuse path to consider.

    BUI-544: `strict=True` raises `_DbLookupFailed` when the GET itself fails,
    instead of returning the None a genuine miss also returns. Only the
    hand-priced provenance check uses it — see `_split_by_db_cache`.
    """
    params: dict = {"locg_id": locg_id, "grade": grade,
                    "max_age_days": max_age_days}
    if locg_variant_id is not None:
        params["locg_variant_id"] = locg_variant_id
    rows = _get_json_or_warn(
        f"{server_url}/api/comics", params=params,
        warn=f"DB cache lookup failed (locg_id={locg_id})",
        default=_LOOKUP_FAILED,
    )
    if rows is _LOOKUP_FAILED:
        if strict:
            raise _DbLookupFailed(
                f"comics-server FMV lookup failed for locg_id={locg_id} "
                f"grade={grade}"
            )
        return None
    # A literal JSON `null` body is a successful (if odd) response, not a
    # transport failure — keep treating it as "no row" on both paths.
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

def _fetch_abort(hard_fail: bool) -> None:
    """Abort a ``_fetch_comps`` call after its error was already echoed.

    Hard path (``hard_fail=True``): ``sys.exit(1)`` as before. Soft path
    (the BUI-348 proxy rescue): return None so the caller degrades instead of
    killing an otherwise-complete run.
    """
    if hard_fail:
        sys.exit(1)
    return None


def _fetch_comps(books: list[dict], *, force: bool,
                 hard_fail: bool = True) -> list[dict] | None:
    """Subprocess to ebay-sold-comps. Returns the parsed result list,
    in the same order as `books`.

    ``hard_fail`` (default True) preserves the original behavior: any failure
    (binary missing, timeout, non-zero exit, unreadable/empty/unparseable
    output) aborts the run via ``sys.exit``. The BUI-348 proxy rescue passes
    ``hard_fail=False`` so a failure on that best-effort second pass returns
    None instead — the caller then leaves the candidate books as needs_manual
    rather than nuking a run whose primary (raw) results already succeeded.
    """
    if shutil.which(EBAY_SOLD_COMPS_BIN) is None:
        click.echo(
            f"Error: '{EBAY_SOLD_COMPS_BIN}' not found on PATH.\n"
            f"Install apps/ebay (e.g. `pip install -e apps/ebay`) so the "
            f"{EBAY_SOLD_COMPS_BIN} entry point is available.",
            err=True,
        )
        _fetch_abort(hard_fail)
        return None

    # Strip the orchestrator's _idx but thread a stable correlation id (_req_id,
    # = the original input index) the subprocess echoes back, so run() can map
    # results to inputs by identity instead of fragile list position (BUI-174/187).
    #
    # BUI-315: the passthrough forwards every book field except _idx — crucially
    # `publisher` — so ebay-sold-comps' build_query can activate the Marvel
    # "marvel comics" qualifier (DC/indie handled by _publisher_qualifier). Keep
    # publisher in the payload; dropping it silently disables the qualifier.
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
            _fetch_abort(hard_fail)
            return None
        if result.returncode != 0:
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} failed (exit {result.returncode}):\n"
                f"{result.stderr}",
                err=True,
            )
            _fetch_abort(hard_fail)
            return None
        # BUI-184: a returncode-0 child can still leave an empty/partial out file
        # (killed between create and write, disk-full). Guard the read+parse and
        # fail loud rather than crash with an opaque JSONDecodeError mid-batch.
        try:
            raw = Path(out_path).read_text()
        except OSError as e:
            click.echo(f"Error: could not read {EBAY_SOLD_COMPS_BIN} output: {e}",
                       err=True)
            _fetch_abort(hard_fail)
            return None
        if not raw.strip():
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} exited 0 but wrote no output "
                "(empty results file).",
                err=True,
            )
            _fetch_abort(hard_fail)
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            click.echo(
                f"Error: {EBAY_SOLD_COMPS_BIN} produced unparseable output: {e}",
                err=True,
            )
            _fetch_abort(hard_fail)
            return None
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
    except (ValueError, requests.exceptions.JSONDecodeError):
        # A malformed (non-JSON) body must not degrade silently into an
        # indistinguishable cache-miss / no-outcomes result. `resp.json()`
        # raises requests.exceptions.JSONDecodeError, which subclasses BOTH
        # ValueError AND requests.RequestException — so this branch must come
        # FIRST, before the generic RequestException handler below, or the
        # decode case would be swallowed there with a less specific message.
        click.echo(f"Warning: {warn}: invalid JSON response", err=True)
        return default
    except requests.RequestException as e:
        click.echo(f"Warning: {warn}: {e}", err=True)
        return default


# ─── Step 2b — First-party outcomes (BUI-286) ─────────────────────────────────

# Recency window for pulling the user's own resolved auctions back in as
# first-party comps. 365 days (BUI-526, up from the BUI-286 starting default
# of 180): our own resolved clears are the highest-quality comps we have, and
# the BUI-287 recency weighting (75-day half-life) already discounts the older
# tail, so the wider window adds supply without letting stale prices dominate.
FIRST_PARTY_RECENCY_DAYS = 365


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

    # Adversarial-review fix (post-BUI-286): R2/KTD-3 make wins-and-losses a
    # structural invariant at the QUERY level (the server has no wins-only
    # entry point), but that is not the same as a per-book GUARANTEE. A
    # (comic, grade) that has only ever WON, or whose losses aged past
    # FIRST_PARTY_RECENCY_DAYS while a win stayed in-window, still yields a
    # wins-only `rows` here even though the query itself asked for both. Merging
    # that truncated-from-above set into the pool would drag FMV down over
    # successive runs (the deflation spiral the plan's Problem Frame warns
    # about) — so re-check the actual composition of what came back, not just
    # how it was asked for, and drop the contribution entirely rather than
    # merge a wins-only pool.
    statuses = {r.get("status") for r in rows}
    if "WON" in statuses and "LOST" not in statuses:
        click.echo(
            "Note: first-party outcomes skipped (wins-only, no in-window "
            f"losses) for locg_id={locg_id}, title={title!r} issue={issue!r}",
            err=True,
        )
        return []

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

    # BUI-535: whether the ebay-sold-comps circuit breaker had tripped by the
    # time this book's fetch finished — threaded through every return path
    # below so comic-fmv's own --out/stdout can surface it (see run()'s
    # summary and _print_table).
    breaker_tripped = bool(result.get("breaker_tripped"))

    # BUI-565: ebay-sold-comps tags a per-book `error` when its fetch RAISED
    # (sold_comps.py's broad BUI-537 handler) instead of completing its tiers.
    # When the raise happens before any tier records a query — a malformed
    # book, or the BUI-565 string-`year` TypeError — `queries_used` comes back
    # EMPTY, and `_is_fetch_error`'s "no queries means nothing was attempted"
    # guard reads that as a genuine no-comps book rather than a failure. So
    # the BUI-536 bail below never fired, the book fell through to the BUI-44
    # unconditional upsert, and it surfaced as a clean `n=0` carrying a real
    # `comic_id` and a null `flag_reason` — invisible to BOTH of /comic:buy
    # Step 3's guards, i.e. a silent wrong answer on a money path. Bail here
    # the same way BUI-536 does: no upsert, `source: "error"`, null comic_id.
    #
    # Deliberately bails even when `comps` is non-empty: a raise mid-tier
    # leaves a TRUNCATED pool of unknown size, and pricing off a pool that
    # lost an unknown fraction of its comps is the same wrong answer, just
    # quieter.
    #
    # `is not None`, not a truthiness test: sold_comps tags the key with
    # `str(e)`, and an exception raised with no message stringifies to "" —
    # which would fall straight back through to the very upsert this guard
    # exists to prevent.
    fetch_error = result.get("error")
    if fetch_error is not None:
        return {
            "input": inp, "fmv": None, "comp_count_total": len(comps),
            "queries_used": result.get("queries_used", []),
            "db_row": None, "comic_id": None, "fmv_id": None,
            "source": "error", "breaker_tripped": breaker_tripped,
            # Explicit signal for `_is_fetch_error` — an empty `queries_used`
            # cannot carry one, and sniffing the message text would be brittle
            # (the other `source: "error"` returns below are NOT fetch errors).
            "fetch_error": True,
            "error": f"fetch-err: ebay-sold-comps failed for this book: "
                     f"{fetch_error or '<no message>'}",
        }

    target_grade = inp.get("grade")
    if target_grade is None:
        return {
            "input": inp, "fmv": None, "comp_count_total": len(comps),
            "queries_used": result.get("queries_used", []),
            "db_row": None, "comic_id": None, "fmv_id": None,
            "source": "error", "breaker_tripped": breaker_tripped,
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
                "source": "error", "breaker_tripped": breaker_tripped,
                "error": f"unrecognized grade string: {target_grade!r}",
            }
        target_grade = coerced
        inp["grade"] = coerced

    # BUI-536: a fetch-err (every tier ERRORED, not merely zero comps found)
    # must NEVER touch the fmv DB row — no upsert, no partial field writes.
    # Confirmed 2026-07-24: X-Men #39/FF #46/X-Men #104 kept their prior
    # low/high/updated_at but had `comps` zeroed and `notes` overwritten with a
    # stub (`window=±2.0 | cv=n/a | label=LOW`) by the unconditional BUI-44
    # upsert below, destroying real provenance (including a hand-price marker).
    # Reuses the exact BUI-143 signal (`_is_fetch_error`): comp_count_total==0
    # AND every entry in queries_used carries an 'error' key. A GENUINE n=0
    # (queries succeeded, zero comps found) has an empty/error-free
    # queries_used and must still upsert unconditionally per BUI-44 — this
    # check does not touch that path (see _is_fetch_error's own guards).
    if _is_fetch_error({"comp_count_total": len(comps),
                       "queries_used": result.get("queries_used", [])}):
        return {
            "input": inp, "fmv": None, "comp_count_total": len(comps),
            "queries_used": result.get("queries_used", []),
            "db_row": None, "comic_id": None, "fmv_id": None,
            "source": "error", "breaker_tripped": breaker_tripped,
            "error": "fetch-err: all tiers failed; fmv DB row left "
                     "untouched (BUI-536)",
        }

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
    # BUI-588: ebay-sold-comps reports when it could only find comps by DROPPING
    # this book's variant term — a collector/catalog descriptor ("White Logo 1st
    # Print") that appears in no listing title zeroes every query tier, and the
    # resulting `comps=0` / `flag_reason=NULL` row is invisible to both of
    # /comic:buy Step 3's guards, reading as "illiquid" rather than "our query
    # was impossible". Comps found after the drop price the BASE cover, which is
    # a defensible floor for most variants and a wrong anchor for a scarce few —
    # a judgment this pipeline must not make silently. Route it into the same
    # needs-manual channel the pool-shape guards use, so the row lands in front
    # of a human with a bid cap withheld instead of a variant-blind number.
    dropped_variant = result.get("variant_dropped")
    fmv = fmv_math.compute_fmv(
        pool_comps, target_grade=target_grade,
        grade_confidence=inp.get("grade_confidence"),
        max_window=grade_window,
        forced_flag_reason="variant_dropped" if dropped_variant else None,
    )
    # Informational, for `_build_notes` only — never touches the priced number.
    # `variant_dropped` is recorded even when a pool-shape guard already claimed
    # `flag_reason`, so the notes still say WHICH pool was measured (BUI-588),
    # and `masthead_swapped_to` records that BUI-581's alt-masthead probe priced
    # this book under the other name its series carried.
    fmv["variant_dropped"] = dropped_variant
    fmv["masthead_swapped_to"] = result.get("masthead_swapped_to")
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
        "source": "fresh", "breaker_tripped": breaker_tripped,
        # BUI-529: thread through any slab comps ebay-sold-comps' BUI-524
        # inclusive tier already fetched (empty when that tier never fired),
        # so the cross-check below can reuse them instead of a dedicated
        # second graded-only fetch in the common case.
        "slab_comps": result.get("slab_comps") or [],
    }


# ─── Step 3b — CGC-proxy rescue (BUI-348) ─────────────────────────────────────

# The literal notes marker for a CGC-proxy band. Shared by `_build_notes`
# (writer) and `_cgc_proxy_from_notes` (cache-reuse reader) so the round-trip
# can't drift on a reword — a mismatch would silently drop the proxy bid-cap
# haircut on a cache hit.
_CGC_PROXY_NOTE_TOKEN = "CGC proxy"

# The 0.50–0.55 proxy factor is calibrated on VINTAGE keys (ASM #50, 1967),
# where a slab carries a large certification premium over raw. For modern books
# the raw/slab ratio is far lower (everyone slabs, raw grade-risk is severe), so
# applying the vintage factor to a modern high-grade slab would over-price the
# raw badly. Gate the tier to pre-cutoff books (mirrors sold_comps'
# _VINTAGE_YEAR_CUTOFF); a book with no year, or a modern one, stays
# needs_manual rather than getting a mis-calibrated proxy band.
_CGC_PROXY_VINTAGE_YEAR_CUTOFF = 2000

# A genuine slab listing names its certifier (CGC/CBCS) in the title. The
# graded pass drops the `-cgc -cbcs -graded -slab` exclusion, so its pool is a
# MIX of slab AND raw listings that merely carry a grade token ("… FN 6.0 …").
# Only certified-slab comps may seed the ladder: a raw copy blended in would
# drag the per-grade slab median DOWN, under-pricing the proxy (a too-low bid
# cap, or a false below-floor rejection). CGC/CBCS are unambiguous certifier
# names; "graded"/"slab" are deliberately NOT matched (raw listings say
# "ungraded"/"not graded", which would false-positive on a bare substring).
_SLAB_TITLE_RE = re.compile(r"\b(?:cgc|cbcs)\b", re.IGNORECASE)


def _slab_comps_only(comps: list[dict]) -> list[dict]:
    """Keep only genuine CGC/CBCS slab comps (grade + price + certifier in title)."""
    return [c for c in comps
            if c.get("grade") is not None and c.get("price") is not None
            and _SLAB_TITLE_RE.search(c.get("title") or "")]


def _is_unpriced_raw(result: dict) -> bool:
    """True if a fresh raw result produced no bid-able number and a numeric grade
    is known — the precondition for the CGC-proxy tier.

    Deliberately narrow: it fires ONLY on a book the raw pipeline could not price
    (n=0, too_sparse/one_sided/too_wide with no §7 interpolation). A book that
    got ANY number — a real range, or a §7 interpolated point — is excluded, so
    the proxy tier can never change a book the raw math already priced.

    BUI-536: also excludes ``source == "error"`` (a fetch-err row never even
    ran ``compute_fmv`` — its ``fmv`` is ``None``, which would otherwise satisfy
    ``fmv_high is None`` here just like a genuine thin-pool needs_manual book).
    Without this, a vintage fetch-err book would still be handed to the rescue
    tier's own second (graded-only) fetch — and, if THAT one happened to
    succeed, upserted via it, defeating BUI-536's "no upsert at all" guarantee
    through a side door the main fetch-err guard in
    ``_compute_and_upsert_one`` doesn't cover.
    """
    if result.get("source") == "error":
        return False
    fmv = result.get("fmv") or {}
    grade = (result.get("input") or {}).get("grade")
    return (fmv.get("fmv_high") is None
            and not fmv.get("interpolated")
            and isinstance(grade, (int, float)))


def _is_vintage(result: dict) -> bool:
    """True if the book's cover year is known and pre-cutoff — the CGC-proxy
    factor is vintage-calibrated, so a modern (or year-unknown) book must not be
    priced off it. Conservative: a missing year means no proxy, not a proxy."""
    year = (result.get("input") or {}).get("year")
    return isinstance(year, (int, float)) and year < _CGC_PROXY_VINTAGE_YEAR_CUTOFF


def _apply_cgc_proxy_rescue(fresh_fmvs: dict[int, dict], books: list[dict], *,
                            server_url: str, force: bool) -> None:
    """BUI-348: re-price sparse-raw needs_manual books off the CGC slab ladder.

    Mutates ``fresh_fmvs`` in place. For each candidate (see ``_is_unpriced_raw``)
    it fetches a GRADED-only comp pool in one second batch pass, builds the slab
    ladder, and — when the ladder is trustworthy and clears the value floor
    (``fmv_math.cgc_proxy_fmv``) — replaces the result's ``fmv`` with a CGC-proxy
    band (MEDIUM-LOW, capped bid) and re-upserts it. A book whose ladder is too
    thin / cheap / out-of-range keeps its raw needs_manual result untouched.

    The graded fetch soft-fails (a SerpApi outage on this second pass leaves the
    candidates as needs_manual rather than aborting an otherwise-complete run —
    the proxy tier is a best-effort rescue, never a hard dependency).
    """
    candidates = [idx for idx in fresh_fmvs
                  if _is_unpriced_raw(fresh_fmvs[idx])
                  and _is_vintage(fresh_fmvs[idx])]
    if not candidates:
        return

    # Second, graded-only pass. Thread _idx so _fetch_comps can round-trip a
    # _req_id and we map results back by identity, never position (BUI-174/187).
    # `books` is the raw batch (never carries _idx — _split_by_db_cache builds
    # separate _idx-tagged dicts), so we add _idx here rather than strip it.
    graded_books = [
        {**books[idx], "_idx": idx, "include_graded": True}
        for idx in candidates
    ]
    graded_results = _fetch_comps(graded_books, force=force, hard_fail=False)
    if graded_results is None:  # soft fetch failure — leave candidates as-is
        click.echo(
            "Note: CGC-proxy graded fetch failed; "
            f"{len(candidates)} book(s) left needs_manual.",
            err=True,
        )
        return

    by_id: dict[int, dict] = {}
    for r in graded_results:
        rid = (r.get("input") or {}).get("_req_id")
        if isinstance(rid, int):
            by_id[rid] = r

    for idx in candidates:
        result = by_id.get(idx)
        if result is None:
            continue  # no graded result for this book — leave needs_manual
        # BUI-535: this graded-only pass is its own separate ebay-sold-comps
        # subprocess invocation with its own fresh circuit breaker — it can
        # trip independently of the primary raw-pass breaker. OR it into the
        # book's flag (never clear an already-True flag from the primary
        # pass) so a trip on EITHER pass surfaces in the final result.
        if result.get("breaker_tripped"):
            fresh_fmvs[idx]["breaker_tripped"] = True
        graded_comps = _slab_comps_only(result.get("comps") or [])
        inp = fresh_fmvs[idx].get("input") or {}
        proxy = fmv_math.cgc_proxy_fmv(
            graded_comps, target_grade=inp["grade"],
            grade_confidence=inp.get("grade_confidence"),
        )
        if proxy is None:
            continue  # ladder too thin / cheap / non-monotonic / out of range
        # Match compute_fmv's post-processing: _build_notes reads first_party_count.
        proxy["first_party_count"] = 0
        # Re-upsert (soft): overwrite the n=0 stub row with the proxy band so the
        # persisted comic carries a real (proxy) price + "CGC proxy" notes. This
        # is the best-effort rescue tier, so a server blip on THIS write must not
        # abort a run whose raw results already succeeded (hard_fail=False). Only
        # promote the in-memory result to the proxy band AFTER a successful
        # write, so the emitted result never shows a price the DB doesn't hold.
        upserted = _upsert_fmv(server_url, inp, proxy, hard_fail=False)
        if upserted is None:
            click.echo(
                f"Note: CGC-proxy upsert failed for {inp.get('title')} "
                f"#{inp.get('issue')}; left needs_manual.",
                err=True,
            )
            continue
        comic_id, fmv_id = _extract_ids(upserted)
        fresh_fmvs[idx]["fmv"] = proxy
        fresh_fmvs[idx]["source"] = "cgc-proxy"
        # Refresh the persisted-row fields so the emitted result is consistent
        # with the proxy write (not the stale pre-proxy n=0 stub upsert).
        fresh_fmvs[idx]["db_row"] = upserted
        fresh_fmvs[idx]["comic_id"] = comic_id
        fresh_fmvs[idx]["fmv_id"] = fmv_id


# ─── Step 3c — Always-on vintage cross-check (BUI-529) ────────────────────────

def _is_thin_or_low_confidence_priced(result: dict) -> bool:
    """True for a book the raw math DID price, but thinly (BUI-529): the
    cross-check's ADDITIONAL candidate population beyond the rescue's
    unpriced-raw candidates (``_is_unpriced_raw``) — the worst real misses in
    the 2026-07-24 FMV review were exactly this shape: a confident-LOOKING
    priced number the raw pool was actually too thin to earn (Ghost Rider #3
    cleared 7.8x fmv_high, Moon Knight #12 5.3x, Thor #149 3.1x).

    Deliberately excludes:
      * an unpriced book (``fmv_high`` is None) — that's the rescue's job, so
        the two candidate sets never overlap.
      * an interpolated result — BUI-306 §7 is its own already-reduced-
        confidence tier with its own guards, out of scope here.
    """
    fmv = result.get("fmv") or {}
    if fmv.get("fmv_high") is None:
        return False
    if fmv.get("interpolated"):
        return False
    n = fmv.get("n")
    confidence = fmv.get("confidence")
    return (isinstance(n, (int, float)) and n < 5) or confidence == "LOW"


def _apply_cgc_cross_check(fresh_fmvs: dict[int, dict], books: list[dict], *,
                           server_url: str, force: bool) -> None:
    """BUI-529: flag a large raw-vs-slab divergence on a vintage book that DID
    price, but thinly (see ``_is_thin_or_low_confidence_priced``) — the
    always-on promotion of the BUI-348 rescue tier from unpriced-only to a
    validation cross-check.

    Mutates ``fresh_fmvs`` in place, setting ``fmv["cgc_cross_check"]`` — never
    ``fmv_low``/``fmv_high``/``max_bid``/``confidence``/``flag_reason``. This is
    a FLAG, never a re-price (see ``fmv_math.cgc_cross_check``'s docstring for
    why blending is out of scope): a cross-check that silently altered a
    number the raw math already priced would itself become a new mis-pricing
    vector, on a money path, with none of the rescue tier's own guards
    re-derived for a blend. Re-upserts (best-effort) so the divergence token
    reaches persisted ``fmv_notes`` for a downstream audit
    (``/comic:calibration-report`` and a human reviewer).

    Reuses the slab comps ebay-sold-comps' BUI-524 inclusive tier may already
    have fetched (``fresh_fmvs[idx]["slab_comps"]``) — zero extra SerpApi spend
    in the common case where that tier already fired. Falls back to a
    dedicated graded-only fetch (mirroring ``_apply_cgc_proxy_rescue``'s own
    second pass) only for a candidate BUI-524 didn't already supply a
    sufficiently-sized ladder for (it didn't fire — raw pool wasn't thin at
    fetch time even though the final priced pool ended up thin/LOW-confidence
    — or it fired but found too few slab comps).

    A book already rescued by ``_apply_cgc_proxy_rescue`` (``source ==
    "cgc-proxy"``) is skipped — its price IS the slab ladder, so comparing it
    against itself is meaningless. This function must therefore run AFTER the
    rescue so it can see the promoted ``source``.
    """
    candidates = [idx for idx in fresh_fmvs
                  if fresh_fmvs[idx].get("source") != "cgc-proxy"
                  and _is_vintage(fresh_fmvs[idx])
                  and _is_thin_or_low_confidence_priced(fresh_fmvs[idx])]
    if not candidates:
        return

    # Split by whether BUI-524's inclusive tier already supplied a usable
    # ladder — those candidates need no further fetch at all.
    have_ladder: dict[int, list[dict]] = {}
    need_fetch: list[int] = []
    for idx in candidates:
        slabs = fresh_fmvs[idx].get("slab_comps") or []
        if len(slabs) >= fmv_math.CGC_PROXY_MIN_LADDER_COMPS:
            have_ladder[idx] = slabs
        else:
            need_fetch.append(idx)

    if need_fetch:
        graded_books = [
            {**books[idx], "_idx": idx, "include_graded": True}
            for idx in need_fetch
        ]
        graded_results = _fetch_comps(graded_books, force=force, hard_fail=False)
        if graded_results is None:  # soft fetch failure — leave un-cross-checked
            click.echo(
                "Note: CGC cross-check graded fetch failed; "
                f"{len(need_fetch)} book(s) left un-cross-checked.",
                err=True,
            )
            graded_results = []
        by_id: dict[int, dict] = {}
        for r in graded_results:
            rid = (r.get("input") or {}).get("_req_id")
            if isinstance(rid, int):
                by_id[rid] = r
        for idx in need_fetch:
            result = by_id.get(idx)
            if result is not None:
                have_ladder[idx] = _slab_comps_only(result.get("comps") or [])
                # BUI-535: same rationale as _apply_cgc_proxy_rescue — this is
                # its own separate ebay-sold-comps subprocess/breaker, OR it
                # into the book's flag rather than overwrite.
                if result.get("breaker_tripped"):
                    fresh_fmvs[idx]["breaker_tripped"] = True

    for idx, graded_comps in have_ladder.items():
        inp = fresh_fmvs[idx].get("input") or {}
        fmv = fresh_fmvs[idx]["fmv"]
        check = fmv_math.cgc_cross_check(
            graded_comps, target_grade=inp["grade"],
            raw_median=fmv.get("median"),
        )
        if check is None:
            continue  # ladder untrustworthy for a comparison — nothing to flag
        fmv["cgc_cross_check"] = check
        # Best-effort re-upsert: the divergence token is informational only
        # (never changes fmv_low/high/max_bid/confidence), so a write failure
        # here must not abort a run whose price already succeeded.
        upserted = _upsert_fmv(server_url, inp, fmv, hard_fail=False)
        if upserted is None:
            click.echo(
                f"Note: CGC cross-check notes update failed for "
                f"{inp.get('title')} #{inp.get('issue')}; divergence flag "
                "kept in-memory only.",
                err=True,
            )
            continue
        fresh_fmvs[idx]["db_row"] = upserted


def _extract_ids(row: dict | None) -> tuple[int | None, int | None]:
    """Pull comic_id and fmv_id from a /api/comics response.

    Old server versions return only the comics row (no fmv_id) — surface None
    rather than fail, so the orchestrator can still proceed without IDs.
    """
    if not row:
        return None, None
    return row.get("comic_id"), row.get("fmv_id")


def _post_json(url: str, body: dict, *, what: str,
               hard_fail: bool = True) -> dict | None:
    """POST JSON and return the parsed response, failing LOUD (BUI-186 / R11).

    A timeout, connection error, or non-2xx aborts the run with a clear message
    rather than returning None and letting the pipeline proceed on missing data:
    an un-persisted FMV silently breaks the downstream snipe-add FMV link (the
    book is priced but never linked, and verify reports it missing).

    ``hard_fail=False`` (the BUI-348 proxy re-upsert): return None on failure
    instead of aborting, so a server blip on the best-effort proxy write leaves
    the book at its already-persisted raw needs_manual result rather than nuking
    a run whose raw results all succeeded. The caller must then NOT promote the
    in-memory result to a price the DB doesn't hold.
    """
    try:
        resp = requests.post(url, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        click.echo(f"Error: {what} failed (POST {url}): {e}", err=True)
        if hard_fail:
            sys.exit(1)
        return None


def _upsert_fmv(server_url: str, inp: dict, fmv: dict,
                hard_fail: bool = True) -> dict | None:
    """POST /api/comics with the computed FMV. Returns the row JSON, or aborts
    the run on a failed call (BUI-186) rather than silently returning None.

    ``hard_fail=False`` propagates to ``_post_json`` for the best-effort proxy
    re-upsert (returns None on failure instead of ``sys.exit``)."""
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
        hard_fail=hard_fail,
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
    # A CGC-proxy band has no grade window (it's priced off the slab ladder, not
    # a raw ±window pool), so render "window=n/a" rather than "window=±None".
    win = fmv.get("window")
    win_str = f"±{win}" if win is not None else "n/a"
    parts = [f"window={win_str}", f"cv={fmv['cv_pct']}",
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
    # BUI-522: surface the ungraded-market anchor — the median price of the
    # grade-less comps build_pool drops, plus how many raw copies traded — as a
    # cheap sanity check + liquidity signal beside the graded FMV. Informational
    # only: it never entered the priced pool. `.get` tolerates its absence on a
    # cached row (the raw comps aren't persisted, so it can't be reconstructed).
    anchor = fmv.get("ungraded_anchor")
    if anchor:
        parts.append(
            f"ungraded_anchor=${anchor['median']:g} (n={anchor['n']} raw)")
    # BUI-534: flag (never re-price) when fmv_low/fmv_high sit far outside the
    # anchor above (fmv_math.anchor_diverges). Same philosophy as the
    # cgc_cross_check token below — a human (or /comic:calibration-report)
    # reads this as "look at this one", not as an instruction already acted on.
    if fmv.get("anchor_diverges"):
        parts.append("anchor_diverges=1")
    # BUI-529: surface the always-on vintage cross-check's divergence signal.
    # Informational only — this NEVER changes fmv_low/fmv_high/max_bid for a
    # book the raw math already priced (see _apply_cgc_cross_check); a human
    # (or /comic:calibration-report) reads DIVERGES as "look at this one",
    # not as an instruction the pipeline itself already acted on.
    cross_check = fmv.get("cgc_cross_check")
    if cross_check:
        verdict = "DIVERGES" if cross_check["diverges"] else "ok"
        parts.append(
            f"cgc_cross_check={verdict} slab_implied="
            f"${cross_check['implied_raw']:g} vs raw_median="
            f"${cross_check['raw_median']:g} "
            f"({cross_check['divergence_pct'] * 100:.0f}%)"
        )
    # BUI-588: name the variant term that had to be dropped before any comp was
    # found. The `manual_review=variant_dropped` token below says the row needs a
    # human; this says WHAT was traded away to get a pool at all, which is the
    # part a human needs to judge whether the base-cover price is a fair floor
    # for this particular variant. Written even when a pool-shape guard won the
    # flag, so the substitution is never lost.
    dropped_variant = fmv.get("variant_dropped")
    if dropped_variant:
        parts.append(f"variant_dropped={dropped_variant}")
    # BUI-581: name the masthead these comps actually came from when it isn't the
    # title we were asked about — a renamed series (X-Men → Uncanny X-Men) lists
    # its vintage issues under the ORIGINAL name, so a pool found under the other
    # masthead is the same book, and saying so keeps the number auditable.
    swapped = fmv.get("masthead_swapped_to")
    if swapped:
        parts.append(f"masthead={swapped}")
    flag = fmv.get("flag_reason")
    if flag:
        parts.append(f"manual_review={flag}")
    # BUI-348: state EXPLICITLY that the price is a CGC-proxy band (raw priced
    # off the slab ladder, not off raw comps), naming the slab anchor and the
    # discount so a downstream reader can see how the number was derived. The
    # literal "CGC proxy" token is the documented marker (fmv.md §7a / Notes).
    #
    # BUI-350 (issue 2): also name `n` explicitly as the graded-ladder comp
    # count, not raw-market depth. `fmv_comps`/`n` is written to the DB
    # unconditionally (same field a raw-priced book uses for its SerpApi/
    # first-party comp count — see fmv.md's `fmv_comps` doc), so a machine
    # consumer reading it in isolation (e.g. the calibration report) would
    # otherwise misread a proxy row's `n` as raw liquidity. A human already
    # gets the "CGC proxy" token; this makes the `n` caveat legible to a
    # parser too, without adding a new DB column for a distinction only this
    # one tier needs (`fmv_notes` is already the documented lossy-projection
    # channel for tier-specific detail — see `first_party=`/`interpolated=`
    # above).
    ladder = fmv.get("cgc_ladder")
    if fmv.get("cgc_proxy") and ladder:
        parts.append(
            f"{_CGC_PROXY_NOTE_TOKEN}: slab {ladder['target_grade']:g}="
            f"${ladder['slab_price']:g} "
            f"× {ladder['factor_low']:g}-{ladder['factor_high']:g} raw; "
            "confidence capped MEDIUM-LOW; "
            f"n={fmv.get('n')} is graded-ladder comps, not raw-market depth"
        )
    # BUI-369: when the BUI-349/BUI-355 envelope-sanity clamp lowered the
    # exact-bucket slab price, `slab_price` above will disagree with
    # `ladder['ladder'][target_grade]` (the raw, un-clamped exact value) —
    # with no marker, that reads as a bug, and an auditor could "correct"
    # the cap upward, defeating the guard. State it explicitly so the
    # contradiction is legible as an intentional, money-safe clamp rather
    # than silent drift.
    if fmv.get("cgc_proxy") and ladder and ladder.get("envelope_clamped"):
        raw_exact = ladder["ladder"].get(ladder["target_grade"])
        parts.append(
            "envelope_clamped=slab price bounded by envelope-sanity guard "
            f"(BUI-349/355): raw exact ${raw_exact:g} → clamped "
            f"${ladder['slab_price']:g}; do not raise to match ladder[target]"
        )
    # BUI-306 §7: state EXPLICITLY that the price was interpolated (not a direct
    # comp) and that confidence is reduced, naming the bracketing buckets used.
    interp = fmv.get("interpolation")
    if fmv.get("interpolated") and interp:
        parts.append(
            f"interpolated=grade {interp['grade_below']:g}→{interp['grade_above']:g} "
            f"(median ${interp['median_below']:g}→${interp['median_above']:g}); "
            "confidence reduced"
        )
    # BUI-306 §5: surface any grade-curve monotonicity violation so a suspect
    # bucket is flagged for review rather than silently blended into the pool.
    suspect = fmv.get("suspect_buckets")
    if suspect:
        pairs = ", ".join(f"{lo:g}>{hi:g}" for lo, hi in suspect)
        parts.append(f"suspect_grade_curve={pairs}")
    # BUI-51: surface the bid haircut so the lowered max bid is explained. Skip
    # it for a flagged book — it has no max bid, so a haircut token would be
    # misleading (its LOW label is forced by the flag, not a real haircut).
    factor = fmv.get("bid_factor")
    if not flag and factor is not None and factor < fmv_math.BASE_BID_FACTOR:
        # BUI-318/348: attribute the haircut to its cause. An interpolated or
        # CGC-proxy book's cap comes from its own tier-specific haircut (not
        # grade_conf, which is typically absent here), so naming grade_conf=None
        # would misread.
        if fmv.get("cgc_proxy"):
            cause = "cgc_proxy"
        elif fmv.get("interpolated"):
            cause = "interpolated"
        else:
            cause = f"grade_conf={fmv.get('grade_confidence')}"
        parts.append(f"bid_haircut={factor:.2f} ({cause})")
    return " | ".join(parts)


# ─── Step 4 — Stitch + present ────────────────────────────────────────────────

def _stitch(books: list[dict], cached: dict[int, dict],
            fresh: dict[int, dict],
            skipped_hand: dict[int, dict] | None = None,
            skipped_lookup_error: dict[int, str] | None = None) -> list[dict]:
    """Combine cached, fresh, and both kinds of skipped result back into the
    input order. `skipped_hand` (BUI-533) reuses the existing DB row exactly
    like `cached` does (never recomputed), tagged with a distinct `source` so
    the table/summary/--brief can tell a protected hand-priced row apart from
    an ordinary cache hit.

    `skipped_lookup_error` (BUI-544) is the opposite case: the lookup FAILED,
    so there is no row to reuse and no price at all this run. It gets its own
    `source` too — never `skipped_hand_priced` (that would let an outage be
    read as protection) and never the bare `error` source (that reads as a
    compute failure, when in fact nothing was even attempted)."""
    skipped_hand = skipped_hand or {}
    skipped_lookup_error = skipped_lookup_error or {}
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
                "breaker_tripped": False,
            })
        elif i in skipped_hand:
            row = skipped_hand[i]
            out.append({
                "input": _input_summary(book),
                "fmv": _fmv_from_db_row(row, book.get("grade_confidence")),
                "comp_count_total": row.get("fmv_comps") or 0,
                "queries_used": [],
                "db_row": row,
                "source": "skipped_hand_priced",
                "breaker_tripped": False,
            })
        elif i in skipped_lookup_error:
            out.append({
                "input": _input_summary(book),
                "fmv": None,
                "comp_count_total": 0,
                "queries_used": [],
                "db_row": None,
                "source": "skipped_lookup_error",
                "breaker_tripped": False,
                "error": (
                    "BUI-544: skipped, hand-priced provenance unverifiable — "
                    f"{skipped_lookup_error[i]}. Row left untouched; this is a "
                    "server failure, not a hand-priced skip and not zero comps."
                ),
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
                "breaker_tripped": False,
                "error": "no comps fetched and no cache",
            })
    return out


def _input_summary(book: dict) -> dict:
    return {k: book.get(k) for k in
            ("item_id", "title", "issue", "year", "publisher", "grade",
             "grade_confidence", "locg_id", "locg_variant_id", "notes")
            if book.get(k) is not None}


_WINDOW_RE = re.compile(r"window=±\s*([0-9.]+)")


def _interpolated_from_notes(notes: str | None) -> bool:
    """Recover whether a cached FMV was priced by §7 interpolation (BUI-306).

    `_build_notes` writes an `interpolated=grade …` token for an interpolated
    book, so the cached path can re-mark it without a structured column. Used to
    keep a cache-reuse row's `interpolated` flag honest for the table + JSON.
    """
    return notes is not None and "interpolated=" in notes


def _cgc_proxy_from_notes(notes: str | None) -> bool:
    """Recover whether a cached FMV was priced by the BUI-348 CGC-proxy tier.

    `_build_notes` writes a `CGC proxy: …` token for a proxy book, so the cached
    path can re-mark it (and re-apply the proxy bid-factor cap) without a
    dedicated DB column — same lossy-projection recovery as `interpolated`.
    """
    return notes is not None and _CGC_PROXY_NOTE_TOKEN in notes


def _anchor_diverges_from_notes(notes: str | None) -> bool:
    """Recover the BUI-534 anchor-divergence flag from persisted fmv_notes.

    The anchor itself (median/n) isn't reconstructible on a cache hit — same
    lossy projection as `ungraded_anchor` (the raw grade-less comps aren't
    persisted) — but `_build_notes` writes a plain `anchor_diverges=1` token
    when the flag fired, so a re-served cached row can still surface it.
    """
    return notes is not None and "anchor_diverges=1" in notes


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
    # BUI-318: an interpolated book is cache-reusable, but its persisted
    # fmv_confidence is a plain "low" — bid_factor("LOW", None) would return the
    # full 0.80×, silently undoing the interpolated-LOW haircut on reuse. Recover
    # the interpolated marker from notes and re-apply the cap so a cached
    # interpolated row bids at the same haircut a fresh recompute would.
    interpolated = _interpolated_from_notes(row.get("fmv_notes"))
    if interpolated:
        factor = min(factor, fmv_math.INTERPOLATED_BID_FACTOR)
    # BUI-348: a cached CGC-proxy row persists a plain "low" fmv_confidence, so
    # bid_factor would return the full 0.80× and silently undo the proxy cap on
    # reuse. Recover the proxy marker from notes and re-apply the MEDIUM-LOW-rung
    # cap so a cached proxy row bids at the same haircut a fresh recompute would.
    cgc_proxy = _cgc_proxy_from_notes(row.get("fmv_notes"))
    if cgc_proxy:
        factor = min(factor, fmv_math.CGC_PROXY_BID_FACTOR)
    return {
        "n": row.get("fmv_comps") or 0,
        # Shape parity with compute_fmv (effective_n exists there for the
        # recency-weighted confidence). A cached row has no per-comp weights, so
        # it degenerates to the raw count — enough to keep the dict shape uniform
        # for downstream readers that iterate it.
        "effective_n": float(row.get("fmv_comps") or 0),
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
        # BUI-306: shape parity with compute_fmv. An interpolated book persists
        # a real number (flag cleared) so it IS cache-reusable — recover the
        # interpolated marker from fmv_notes so a re-displayed / re-served row
        # still tells a downstream consumer it's interpolated, not a direct comp
        # (§7 "state explicitly"). The full interpolation detail dict isn't
        # reconstructed (same lossy projection as median/cv/trimmed_pool).
        "interpolated": interpolated,
        "interpolation": None,
        "suspect_buckets": [],
        # BUI-348: shape parity + recovered proxy marker so a re-displayed /
        # re-served cached row still reads as a proxy band, not a raw range. The
        # full ladder detail isn't reconstructed (same lossy projection as
        # median/cv/trimmed_pool); the marker is enough for the table + bid cap.
        "cgc_proxy": cgc_proxy,
        "cgc_ladder": None,
        # BUI-534: recover the flag (not the anchor itself, which isn't
        # reconstructible — same lossy projection as ungraded_anchor) from the
        # persisted `anchor_diverges=1` notes token, so a re-displayed /
        # re-served cached row still surfaces the divergence signal.
        "anchor_diverges": _anchor_diverges_from_notes(row.get("fmv_notes")),
        # BUI-529: shape parity with compute_fmv. The cross-check result itself
        # isn't persisted (no DB column — same lossy projection as median/cv),
        # so a cache-reused row always reads as "not cross-checked" here; the
        # key must still exist for downstream readers that iterate uniformly.
        "cgc_cross_check": None,
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


def _brief_row(r: dict) -> dict:
    """BUI-362: project one result row down to the linkage + pricing fields an
    orchestrator (/comic:buy Step 3→5) actually threads forward. The full
    `--out` row is dominated by `queries_used` and `trimmed_pool`; this is the
    ~10-field subset that used to be extracted from it after the fact.

    Field sources:
      - item_id            → the row's input echo.
      - comic_id / fmv_id  → top-level keys on fresh rows (the POST /api/comics
        response ids); a cached row has no top-level keys — its ids live on the
        GET /api/comics `db_row` as `id` / `fmv_id`, so fall back there. A row
        that never upserted (source == "error") projects null for both.
      - max_bid / flag_reason / confidence / fmv_low / fmv_high → the row's
        `fmv` dict (null when `fmv` itself is null, e.g. an error row).
      - fmv_notes (BUI-505) → the haircut/range explanation string. A fresh (or
        cgc-proxy) row's `db_row` is the bare `comics` table row (the POST
        /api/comics response) — it has no `fmv_notes` column, since that lives
        on the separate `fmv` table, which only the GET endpoint joins. So for
        those rows, rebuild the identical string `_upsert_fmv` already sent the
        server, straight from this same in-memory `fmv` dict: `_build_notes` is
        a pure function of it, so there's no drift from what was persisted. A
        cached row's `db_row` (the GET /api/comics join) already carries the
        real persisted `fmv_notes` verbatim, so read it directly instead.
        `_build_notes` reads a few `fmv` keys (`cv_pct`/`confidence`) directly
        rather than via `.get`; a real fmv dict (from `compute_fmv`/
        `cgc_proxy_fmv`) always has them, but a caller passing a partial dict
        (e.g. a test double) shouldn't crash the whole projection over one
        cosmetic field, so a KeyError there degrades to a null note instead.
      - source (BUI-549) → the row's own `source` verbatim (`"fresh"`,
        `"cached"`, `"cgc-proxy"`, `"skipped_hand_priced"`,
        `"skipped_lookup_error"`, or `"error"`). Without this, a
        `skipped_lookup_error` row (comics-server lookup FAILED — hand-priced
        provenance unverifiable, row left completely untouched) projects
        identically to an ordinary unpriced/no-comps row: both have every
        pricing field null. A `--brief`-only consumer (no table, no summary)
        otherwise has no way to tell "priced honestly, no comps" apart from
        "the lookup failed" — see BUI-549.
    """
    fmv = r.get("fmv") or {}
    db_row = r.get("db_row") or {}
    if "comic_id" in r:
        comic_id = r.get("comic_id")
        fmv_id = r.get("fmv_id")
        try:
            fmv_notes = _build_notes(fmv) if fmv else None
        except KeyError:
            fmv_notes = None
    else:
        comic_id = db_row.get("id")
        fmv_id = db_row.get("fmv_id")
        fmv_notes = db_row.get("fmv_notes")
    return {
        "item_id": (r.get("input") or {}).get("item_id"),
        "comic_id": comic_id,
        "fmv_id": fmv_id,
        "max_bid": fmv.get("max_bid"),
        "flag_reason": fmv.get("flag_reason"),
        "confidence": fmv.get("confidence"),
        "fmv_low": fmv.get("fmv_low"),
        "fmv_high": fmv.get("fmv_high"),
        "fmv_notes": fmv_notes,
        "source": r.get("source"),
    }


def _print_brief(rows: list[dict]) -> None:
    """BUI-362: one compact JSON object per row (JSON Lines) on stdout."""
    for r in rows:
        click.echo(json.dumps(_brief_row(r)))


def _is_fetch_error(r: dict) -> bool:
    """BUI-143: distinguish a SerpApi fetch FAILURE (quota exhausted / outage)
    from a book that genuinely has zero comps. Both yield n=0 and otherwise look
    identical, so without this an operator can mistake an API outage for "these
    books are illiquid" and bid blind. A fetch error leaves comps empty while
    every query in queries_used carries an 'error'; a real no-comps book ran its
    queries cleanly (no 'error' key).

    BUI-565: a book whose fetch RAISED before any tier ran has an EMPTY
    queries_used, which the `if not queries` guard below reads as "nothing was
    attempted" — a genuine no-comps book. `_compute_and_upsert_one` sets an
    explicit `fetch_error` flag on those rows precisely because the
    queries_used trail cannot carry the signal; honour it first so they render
    as 'fetch-err' and land in the run's fetch-err warning count rather than
    as a bland 'n/a'."""
    if r.get("fetch_error"):
        return True
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
        elif fmv.get("cgc_proxy"):
            # BUI-348: CGC-proxy band (raw priced off the slab ladder) — mark it
            # so it's never conflated with a real raw-comp range.
            fmv_str = f"${fmv['fmv_low']}–${fmv['fmv_high']} cgc"
            med_str = f"${fmv.get('median') or '?'}"
            mb_str = f"${fmv.get('max_bid') or '?'}"
        elif fmv.get("interpolated"):
            # BUI-306 §7: interpolated point estimate (fmv_low == fmv_high) — mark
            # it so it's never conflated with a real direct-comp range.
            fmv_str = f"${fmv['fmv_high']} interp"
            med_str = f"${fmv.get('median') or '?'}"
            mb_str = f"${fmv.get('max_bid') or '?'}"
        elif fmv.get("fmv_low") is not None:
            fmv_str = f"${fmv['fmv_low']}–${fmv['fmv_high']}"
            med_str = f"${fmv.get('median') or '?'}"
            mb_str = f"${fmv.get('max_bid') or '?'}"
        elif r.get("source") == "skipped_lookup_error":
            # BUI-544: never priced this run — the comics-server lookup behind
            # the hand-price guard failed, so the book was skipped. It has no
            # comps and no queries, so without this branch it would render as
            # a bland 'n/a' and read as "illiquid" (the BUI-143 trap) or be
            # mistaken for the hand-priced skip. Distinct token, distinct
            # source column.
            fmv_str = "skip:db-err"
            med_str = "—"
            mb_str = "—"
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
            f"\n⚠️  {n_fetch_err} book(s) marked 'fetch-err': the comp fetch "
            f"FAILED (quota exhausted, outage, or — BUI-565 — a per-book error "
            f"in ebay-sold-comps), NOT zero comps. Check each row's `error` "
            f"plus the SerpApi key/quota and re-run — do not treat these as "
            f"illiquid.",
            err=True,
        )

    # BUI-535: surface a tripped ebay-sold-comps circuit breaker on STDOUT
    # (distinct from that subprocess's own one-time stderr warning) so an
    # operator reading only the human table/summary still learns the batch
    # was affected — "outage", not "priced".
    n_breaker_tripped = sum(1 for r in rows if r.get("breaker_tripped"))
    if n_breaker_tripped:
        click.echo(
            f"\nSerpApi circuit breaker tripped during this run — "
            f"{n_breaker_tripped} book(s) affected (served cache-only or "
            "fetch-err). Re-run later once SerpApi recovers."
        )
