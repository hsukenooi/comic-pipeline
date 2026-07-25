#!/usr/bin/env python3
"""ebay-sold-comps: fetch eBay sold listings for a comic via SerpApi.

Wraps SerpApi's eBay engine (with show_only=Sold), caches responses, dedupes
by product_id, applies hard-excludes, parses grades, and returns clean comp
lists. Consumed by comic-pipeline-fmv (apps/fmv) to compute fair market value.

Why this lives in apps/ebay (alongside ebay-fetch):
    All eBay data fetching — live (Browse API, ebay_fetch.py) and sold
    (SerpApi, this file) — belongs in the same app. Comic-specific FMV math
    and DB upsert live in apps/fmv; this file is the eBay side of that pipeline.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

import comic_identity
from ebay_fetch import RetryExhausted, atomic_write_json, retry_request


def _version_string() -> str:
    """BUI-314: staleness signal for a `uv tool install`ed binary.

    `_ebay_build_stamp` is generated at build time by hatch_build.py from the
    git HEAD of the source tree the wheel was built from; it's absent when
    running from an unbuilt checkout (e.g. `uv run` here in tests), so fall
    back to "unknown" rather than failing.
    """
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-sold-comps {pkg_version} (git {GIT_SHA}, {GIT_DATE})"

# ─── Configuration ────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "ebay-sold-comps"
DEFAULT_CACHE_TTL_SEC = 7 * 24 * 3600  # 7 days
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_TIMEOUT_SEC = 30
DEFAULT_MAX_WORKERS = 10

# Tier thresholds (the "tiered query strategy" from the FMV skill)
THIN_RESULTS_THRESHOLD = 5     # auto-broaden (drop year) if base returns fewer
GRADE_TAGGED_THRESHOLD = 10    # add grade-targeted query if base returns fewer

# Retry policy for transient SerpApi failures (network errors, 429/5xx). The
# backoff schedule itself (2 ** attempt seconds) now lives in the shared
# ebay_fetch.retry_request() helper (BUI-333) — only the attempt count stays
# a fetch()-local knob.
FETCH_MAX_RETRIES = 3

# BUI-535: consecutive LIVE (charged) SerpApi errors — since the last live
# success, across the whole batch — before the circuit breaker trips and the
# rest of the batch is served cache-only. Calibrated against the 2026-07-24
# outage replay (~48 charged searches, every one erroring): tripping at 5
# keeps total spend on a full-outage batch in the low tens rather than the
# high forties. Some overshoot beyond exactly 5 is expected and fine — with
# DEFAULT_MAX_WORKERS concurrent workers, several requests can already be in
# flight before any of them observes the newly-tripped state.
CIRCUIT_BREAKER_THRESHOLD = 5


# ─── SERPAPI_KEY loader ──────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE per line, no quoting). Comments + blanks ok."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_serpapi_key() -> str:
    """Resolve SERPAPI_KEY from env, then apps/ebay/.env."""
    key = os.environ.get("SERPAPI_KEY")
    if key:
        return key
    app_root = Path(__file__).parent.parent
    for env_path in (app_root / ".env", app_root / ".env.local"):
        env = _load_dotenv(env_path)
        if env.get("SERPAPI_KEY"):
            return env["SERPAPI_KEY"]
    print(
        "Error: SERPAPI_KEY not found.\n"
        f"Set the env var or put it in {app_root}/.env",
        file=sys.stderr,
    )
    sys.exit(2)


# ─── Cache layer ──────────────────────────────────────────────────────────────

def _cache_path(canonical_url: str) -> Path:
    digest = hashlib.sha256(canonical_url.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _cache_get(path: Path, ttl_sec: int) -> dict | None:
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_sec:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001  # cache read — corrupt/partial file, return None
        return None


def _cache_put(path: Path, data: dict) -> None:
    """Write *data* to the SerpApi response cache (BUI-333: routed through the
    shared ebay_fetch.atomic_write_json() rather than a hand-rolled
    tmp→rename copy)."""
    atomic_write_json(path, data)


# ─── Query construction ──────────────────────────────────────────────────────

_MARVEL_QUALIFIER = "marvel comics"

# BUI-321: known DC/Marvel imprints → their PARENT publisher's gate. Without
# this table these imprints don't match \bmarvel\b/\bdc\b, so they fall through
# to the indie raw-passthrough branch and append the imprint name (e.g.
# "Vertigo", "Epic") as a query keyword — recall noise, since eBay comic
# listings title the parent publisher, not the imprint. Keys are the
# punctuation-stripped, lowercased, whitespace-collapsed publisher string (see
# _normalize_publisher_key). Values are the parent gate: "marvel" → the Marvel
# qualifier, "dc" → no qualifier (Marvel-only gate, BUI-315).
_IMPRINT_PARENT_GATE = {
    # ── Marvel imprints ──
    "epic": "marvel",
    "epic comics": "marvel",
    "icon": "marvel",
    "icon comics": "marvel",
    "max": "marvel",
    "max comics": "marvel",
    "marvel knights": "marvel",
    "star comics": "marvel",
    "timely": "marvel",
    "timely comics": "marvel",
    # NOTE: Malibu is deliberately NOT mapped — it published independently
    # (1986–1994) before Marvel acquired it, so a year-less "marvel comics"
    # qualifier would over-narrow pre-acquisition titles (Ultraverse, Men in
    # Black). It falls to indie passthrough, appending "Malibu" — correct for
    # both eras, since those listings say "Malibu", not "Marvel". (BUI-321)
    # ── DC imprints ──
    "vertigo": "dc",
    "dc vertigo": "dc",
    "wildstorm": "dc",
    "black label": "dc",
    "dc black label": "dc",
    "milestone": "dc",
    "milestone media": "dc",
    "milestone comics": "dc",
    "paradox press": "dc",
    "minx": "dc",
    "helix": "dc",
    "homage": "dc",
    "homage comics": "dc",
    "zuda": "dc",
    "zuda comics": "dc",
}


def _normalize_publisher_key(publisher: str) -> str:
    r"""Lowercase, drop periods, collapse whitespace — a match key.

    Dropping periods is what lets "D.C." reach the \bdc\b gate (BUI-321): the
    raw string "D.C." has no "dc" whole-word token, so it previously missed the
    gate and got appended as a raw "D.C." keyword. Periods are removed (not
    spaced) so "D.C." collapses to "dc", not "d c".
    """
    key = publisher.replace(".", "")
    return re.sub(r"\s+", " ", key).strip().lower()


def _publisher_qualifier(publisher: str | None) -> str | None:
    """Normalize a publisher into the query qualifier keyword to append.

    BUI-304 (issue 2): for Marvel we emit the canonical "marvel comics" — a
    cheap disambiguator that keeps the *year-less* base query (per /comic:buy's
    convention of omitting year to dodge the BUI-129 collection-check
    false-negative) from colliding with modern media that reuses the issue
    number: e.g. "X-Men 97" vs the 2024 "X-Men '97" show's merchandise.

    BUI-315 — Marvel ONLY: a live SerpApi spot-check (BUI-304) showed the Marvel
    qualifier is neutral-to-positive (ASM 300 46→50, X-Men 97 44→49) but the DC
    "dc comics" two-token qualifier MATERIALLY narrows recall (Batman 232 34→12,
    Detective 400 38→21). So DC recognized publishers get NO qualifier (return
    None) — the base query passes through untouched rather than regressing. Any
    "DC Comics" raw passthrough would reintroduce the same two-token narrowing,
    so DC must short-circuit to None, not fall to the indie branch.

    BUI-321: known DC/Marvel imprints (Vertigo, Wildstorm, Epic, …) map to their
    parent's gate via _IMPRINT_PARENT_GATE instead of falling to indie
    passthrough, and punctuation is tolerated so "D.C." is gated (not appended).

    Indie publishers pass through unchanged — the caller already supplies the
    noise-filtering name ("image comics", "dark horse"), which is the primary
    indie noise filter (BUI-161). Returns None for an absent/blank publisher so
    the base query is untouched.
    """
    if not publisher or not publisher.strip():
        return None
    p = publisher.strip()
    key = _normalize_publisher_key(p)
    # BUI-321: resolve a known imprint to its parent gate first; else match the
    # parent name directly on the punctuation-normalized key ("D.C." → "dc").
    gate = _IMPRINT_PARENT_GATE.get(key)
    if gate == "marvel" or re.search(r"\bmarvel\b", key):
        return _MARVEL_QUALIFIER
    if gate == "dc" or re.search(r"\bdc\b", key):
        return None  # BUI-315: DC qualifier regresses recall — no qualifier
    return p


_LEADING_ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)


def _strip_leading_article(title: str) -> str:
    """Strip a leading article ("The"/"A"/"An") from a series title.

    BUI-346 defense-in-depth: `build_query` normalizes its own `title` input
    independent of whatever normalization (or lack of it) happened upstream in
    the buy→FMV handoff (apps/fmv/src/fmv_runner.py does the same strip at the
    working-list boundary). Duplicated rather than shared across apps/ebay and
    apps/fmv per this repo's existing package boundary — comic-fmv shells out
    to the ebay-sold-comps console script rather than importing it (see
    CLAUDE.md's "FMV pipeline shells out across package boundaries").
    """
    return _LEADING_ARTICLE_RE.sub('', title or '').strip()


def _strip_embedded_issue(title: str, issue: str) -> str:
    """Strip an embedded ``#<issue>`` (or a bare trailing issue token) from
    *title* when it duplicates the separate `issue` field.

    BUI-346: without this, a title like "The Amazing Spider-Man #50" combined
    with issue="50" makes the `f'"{title} {issue}"'` phrase double up into
    `"The Amazing Spider-Man #50 50"`, which returns 0 results on every tier
    (real incident: ASM #50, 2026-07-13 buy run). The `(?<!\\d)` guard on the
    trailing-token strip prevents chewing into an unrelated longer number
    (e.g. issue="99" must not touch the "2099" in "X-Men 2099").
    """
    issue = str(issue).strip() if issue else ""
    if not title or not issue:
        return title
    cleaned = re.sub(rf'#\s*{re.escape(issue)}\b', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(rf'(?<!\d){re.escape(issue)}\s*$', '', cleaned.strip())
    return re.sub(r'\s+', ' ', cleaned).strip()


# BUI-347: rebootable mastheads — long-running Marvel/DC titles whose numbering
# (or a same-numbered modern relaunch) collides with a vintage issue's own
# number. List mirrors the one already documented for the analogous
# collection-check ambiguity (.claude/commands/comic/collection-check.md) so
# the two "which titles are rebootable" judgment calls don't drift apart.
_REBOOTABLE_MASTHEADS = (
    "fantastic four", "amazing spider-man", "spider-man", "uncanny x-men",
    "x-men", "avengers", "thor", "iron man", "incredible hulk", "hulk",
    "captain america", "batman", "superman", "wonder woman",
)
# BUI-351: plain `\b` treats a hyphen as a non-word char, so `\bhulk\b` matches
# INSIDE "She-Hulk" (the boundary lands on the "-"). Anchor on a full
# title-token match instead: forbid the masthead from being immediately
# preceded or followed by a hyphen (or any other word char), so it can't be a
# substring of a different hyphenated title. A masthead's OWN internal hyphen
# (e.g. "spider-man") is untouched — re.escape keeps it literal; only the
# match's outer edges get the tightened boundary.
_REBOOTABLE_MASTHEAD_RES = [
    re.compile(rf'(?<![-\w]){re.escape(m)}(?![-\w])', re.IGNORECASE)
    for m in _REBOOTABLE_MASTHEADS
]

# Pre-2000 gate for "vintage" (BUI-347's own example threshold). Deliberately
# simple/conservative — this is a hard gate, not a fuzzy score, so a modern
# book's query is byte-for-byte unaffected.
_VINTAGE_YEAR_CUTOFF = 2000

# Conservative exclusion lexicon — every token here is a modern
# printing/cover-variant convention that could not appear in a genuine 1960s/
# 70s raw comic listing (variant covers, foil covers, "virgin"/no-logo covers,
# and the "Timeless"/"Homage" modern cover programs are all post-1990s
# inventions; "reprint"/"facsimile" describe a later, non-original printing —
# exactly what a vintage-key comp query must exclude). Money-safety
# (BUI-347): do NOT add anything broader than this without re-validating
# against a real vintage sold-comp pool — see
# test_vintage_comp_pool_survives_exclusion_terms.
_VINTAGE_EXCLUSION_TERMS = (
    "-variant", "-foil", "-virgin", "-reprint", "-facsimile", "-homage",
    "-timeless",
)


def _is_rebootable_masthead(title: str) -> bool:
    """True if *title* names a long-running masthead with a modern relaunch
    that reuses low issue numbers (BUI-347)."""
    return any(p.search(title or '') for p in _REBOOTABLE_MASTHEAD_RES)


def build_query(title: str, issue: str, year: int | None = None,
                publisher: str | None = None, variant: str | None = None,
                grade_label: str | None = None,
                exclude_graded: bool = True,
                vintage_year: int | None = None) -> str:
    """Build the _nkw search string. Returns the raw (unencoded) keyword string.

    `vintage_year` (BUI-350): the book's real cover year, used ONLY to gate the
    BUI-347 vintage-masthead exclusion terms — independent of whether `year`
    itself is embedded in the query text. `fetch_book_comps`'s tier-2 "broaden"
    query drops `year` (to widen recall) but must not thereby drop the vintage
    hardening: a rebootable-masthead vintage key's broadened (year-less) query
    could otherwise blend in modern slabbed variants. Callers that broaden pass
    the original year here while passing `year=None` for the query text.
    Defaults to `year` itself, so every existing caller that doesn't pass it
    keeps byte-for-byte pre-BUI-350 behavior.
    """
    # BUI-346: normalize the title before it's ever quoted — strip a leading
    # article, then an embedded/trailing issue number that would otherwise
    # double up with the separate `issue` field below. Guarded on a truthy
    # title so a falsy/absent one (not a real, expected input, but `title` is
    # a plain `str` param with no caller ever passing None in practice) keeps
    # its pre-BUI-346 byte-for-byte behavior rather than silently becoming an
    # empty string.
    if title:
        title = _strip_embedded_issue(_strip_leading_article(title), issue)
    parts = [f'"{title} {issue}"']
    if year:
        parts.append(str(year))
    # BUI-304 (issue 1): append the distribution variant (e.g. "Newsstand",
    # "Direct") as a query keyword, mirroring the publisher mechanism below.
    # Previously `variant` was DB-only (distinct comic_id per BUI-28) and never
    # reached the search — so a plain "X-Men 123" blended newsstand + direct
    # copies, and after grade-parsing losses too few remained attributable to
    # either sub-market. Guard for empty/None so the base query is unchanged
    # (byte-for-byte) when variant is absent.
    variant = variant.strip() if variant else ""
    if variant:
        parts.append(variant)
    # BUI-304 (issue 2): the publisher qualifier — indie passes through, Marvel
    # normalizes to "marvel comics"; DC gets none (BUI-315). See
    # _publisher_qualifier.
    qualifier = _publisher_qualifier(publisher)
    if qualifier:
        parts.append(qualifier)
    if grade_label:
        parts.append(grade_label)
    # BUI-347: harden a vintage key's comp query against its own modern
    # relaunch. Gated HARD on old-year AND a rebootable masthead — a modern
    # book (recent year, or no year at all) or a non-rebootable title is
    # completely untouched by this branch, so its query stays byte-for-byte
    # identical to pre-BUI-347 output. BUI-350: gate on `vintage_year`, not
    # `year` — see the `vintage_year` docstring above for why.
    gate_year = year if vintage_year is None else vintage_year
    if gate_year and gate_year < _VINTAGE_YEAR_CUTOFF and _is_rebootable_masthead(title):
        parts.extend(_VINTAGE_EXCLUSION_TERMS)
    if exclude_graded:
        parts.extend(["-cgc", "-cbcs", "-graded", "-slab"])
    return " ".join(parts)


def canonical_serpapi_url(nkw: str, *, page: int = 1) -> str:
    """Build the SerpApi URL with deterministic param order (for cache key).

    Excludes api_key from the canonical form so we don't tie cache to a
    specific user's key. The actual request URL adds api_key separately.

    BUI-523: `page` selects SerpApi's eBay-engine page-number param (`_pgn`).
    It's omitted entirely for `page=1` — the default, and the only page any
    pre-BUI-523 caller ever requested — so the canonical URL (and therefore
    the cache key) is byte-for-byte unchanged for the common case; the
    existing 7-day cache isn't invalidated by this change. Page 2+ appends
    `_pgn` to the params, giving each page its own cache key so a page-2
    fetch caches independently of page 1 under the same TTL (required so a
    repeat FMV run within the TTL window doesn't re-bill SerpApi for a page
    it already paid for).
    """
    params = {
        "engine": "ebay",
        "_nkw": nkw,
        "show_only": "Sold",
    }
    if page and page > 1:
        params["_pgn"] = page
    canonical = urllib.parse.urlencode(sorted(params.items()))
    return f"{SERPAPI_ENDPOINT}?{canonical}"


def request_url(canonical: str, api_key: str) -> str:
    sep = "&" if "?" in canonical else "?"
    return f"{canonical}{sep}api_key={api_key}"


# ─── Fetch (with cache + URL verification) ────────────────────────────────────

class SerpApiError(Exception):
    pass


class BreakerTrippedError(SerpApiError):
    """BUI-535: raised by fetch() when the batch circuit breaker has already
    tripped and no cache entry covers this query. Subclasses SerpApiError so
    every existing `except (SerpApiError, requests.RequestException)` site
    (fetch_book_comps._run, in particular) keeps catching it unchanged — from
    every caller's point of view this is an ordinary fetch failure, just one
    that never actually reached SerpApi."""


class _CircuitBreaker:
    """BUI-535: batch-scoped SerpApi outage circuit breaker.

    One instance is created per `run_batch()` call and threaded explicitly
    into every `fetch_book_comps()`/`fetch()` call for that batch (via the
    `breaker=` kwarg) — NOT a module-level singleton, so each fresh batch
    starts clean regardless of what happened in a previous run.

    "Consecutive" is defined globally across the whole batch, not per-thread
    or per-book: K live (charged) SerpApi errors with no live success in
    between, counted since the last live success (or since the batch began).
    A cache hit neither trips nor resets it — only a genuine live SerpApi
    round trip counts, whether it's the winning attempt of a `fetch()` call or
    one of that call's own superseded internal retries (see `fetch()`'s
    `record_attempt`/`on_attempt` wiring) — each one is a real SerpApi charge.

    Thread-safety: every read/mutation of the counter and `tripped` happens
    under one lock, so two threads whose live-errors land at the same instant
    can't both independently conclude "I'm the one crossing the threshold" —
    record_error() prints the one-time loud warning INSIDE the same locked
    critical section that detects the crossing, so it is structurally
    impossible for two threads to both print it.

    Once tripped, stays tripped for the rest of this batch — it does not
    un-trip if SerpApi recovers mid-batch (see record_success()). Recovery is
    a fresh process/run_batch() call (a new, untripped breaker), matching the
    "re-run later" guidance in the printed warning.
    """

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, *,
                 total_books: int = 0):
        self.threshold = threshold
        self._lock = threading.Lock()
        self._consecutive_errors = 0
        self.tripped = False
        self._total_books = total_books
        self._completed_books = 0

    def should_skip_live(self) -> bool:
        with self._lock:
            return self.tripped

    def record_success(self) -> None:
        """A genuine LIVE (charged) SerpApi call succeeded — resets the
        streak. Does not clear `tripped`: once tripped, this batch stays
        cache-only for the remainder (see class docstring)."""
        with self._lock:
            self._consecutive_errors = 0

    def record_error(self) -> None:
        """A genuine LIVE (charged) SerpApi call failed. Trips the breaker
        and prints the one-time loud stderr warning the instant the
        threshold is crossed — see class docstring for why this can only
        ever fire once."""
        with self._lock:
            if self.tripped:
                return
            self._consecutive_errors += 1
            if self._consecutive_errors >= self.threshold:
                self.tripped = True
                remaining = max(self._total_books - self._completed_books, 0)
                print(
                    "SerpApi appears down — "
                    f"{self._consecutive_errors} consecutive errors, circuit "
                    "breaker tripped (BUI-535). Skipping live fetches for "
                    f"the remaining ~{remaining} book(s) in this batch "
                    "(cache-only from here); re-run later.",
                    file=sys.stderr,
                )

    def book_completed(self) -> None:
        """Called once per book as its future finishes — feeds the `remaining
        ~N book(s)` estimate above. Approximate by nature under concurrency
        (several books may already be in flight when the trip happens), which
        is fine for an informational warning, not a correctness signal."""
        with self._lock:
            self._completed_books += 1


def fetch(nkw: str, api_key: str, *, force: bool = False,
          ttl_sec: int = DEFAULT_CACHE_TTL_SEC, page: int = 1,
          record_attempt=None, breaker: "_CircuitBreaker | None" = None,
          ) -> tuple[dict, bool]:
    """Fetch a SerpApi response with caching. Returns (data, cache_hit).

    BUI-523: `page` (default 1) selects the SerpApi page — see
    canonical_serpapi_url for why page 1 stays byte-for-byte identical to
    pre-BUI-523 behavior and page 2+ gets its own cache entry.

    BUI-537: `record_attempt`, when given, is called as
    `record_attempt(outcome: str, detail: str)` once for every internal retry
    attempt this call supersedes with a further attempt — i.e. every SerpApi
    charge that would otherwise be invisible to the caller. It is NOT called
    for the terminal attempt (the one this function ultimately returns/raises
    for) — the caller already learns that outcome from the normal return
    value / raised exception, so calling the hook there too would double-
    count that one charge.

    BUI-535: `breaker`, when given, gates every live (charged) attempt through
    the batch-scoped circuit breaker (see `_CircuitBreaker`): every charged
    attempt (interim retry or terminal) reports success/failure to it, and a
    tripped breaker short-circuits this call to a cache-only lookup (raising
    `BreakerTrippedError` on a miss) — even under `force=True`, which
    otherwise means "bypass the cache," but does not mean "bypass the
    breaker."
    """
    canonical = canonical_serpapi_url(nkw, page=page)
    path = _cache_path(canonical)

    cache_checked = False
    if not force:
        cached = _cache_get(path, ttl_sec)
        cache_checked = True
        if cached is not None:
            return cached, True

    if breaker is not None and breaker.should_skip_live():
        # BUI-535: the breaker overrides --force too — once tripped, no more
        # live SerpApi charges for the rest of this batch. Still serve a
        # cache hit if one exists ("breaker-tripped mode must still allow
        # cache reads") — re-check here since the `not force` branch above
        # may have skipped the cache lookup entirely (force=True).
        if not cache_checked:
            cached = _cache_get(path, ttl_sec)
            if cached is not None:
                return cached, True
        raise BreakerTrippedError(
            "SerpApi circuit breaker tripped (BUI-535) — no cache entry for "
            "this query; skipping the live fetch for the rest of this batch."
        )

    def _on_retry_attempt(attempt, resp, exc):
        # BUI-535: every physical charge (interim or terminal) reports to the
        # breaker — retry_request only invokes this hook when about to retry,
        # which by construction is always an error signal.
        if breaker is not None:
            breaker.record_error()
        if record_attempt is None:
            return
        # BUI-537: translate this superseded attempt into a trail entry. A
        # retryable-status response has no exception object yet (retry_request
        # only inspects status_code, never calls raise_for_status() itself) —
        # synthesize the same HTTPError raise_for_status() would raise so the
        # recorded outcome/class matches exactly what the terminal path would
        # have recorded had THIS attempt been the last one.
        if exc is not None:
            record_attempt(f"error:{type(exc).__name__}", str(exc))
        else:
            try:
                resp.raise_for_status()
            except requests.exceptions.RequestException as http_exc:
                record_attempt(f"error:{type(http_exc).__name__}", str(http_exc))

    # BUI-333: retry/backoff routed through the shared ebay_fetch.retry_request()
    # helper rather than the hand-rolled loop this used to have. retry_request()
    # only classifies retryable vs. non-retryable *status codes* — it never calls
    # resp.raise_for_status() itself — so raise_for_status() is still called
    # explicitly below, reproducing the original "raise HTTPError on any non-2xx"
    # behavior exactly, including an un-retried 4xx raising immediately. One
    # intentional widening: the original narrowly caught (requests.Timeout,
    # requests.ConnectionError) as the retryable network-error types;
    # retry_request's retry_network_errors=True catches any
    # requests.exceptions.RequestException, matching the broader catch
    # get_token()/fetch_item_with_status() already use in ebay_fetch.py. The
    # extra types that catch admits (TooManyRedirects, ChunkedEncodingError,
    # ...) can't arise from this internally-built URL in practice; treating
    # them as retryable rather than immediately fatal is strictly safer.
    try:
        resp = retry_request(
            lambda: requests.get(request_url(canonical, api_key), timeout=SERPAPI_TIMEOUT_SEC),
            retries=FETCH_MAX_RETRIES,
            is_retryable_status=lambda code: code == 429 or code >= 500,
            retry_network_errors=True,
            on_attempt=_on_retry_attempt,
        )
    except RetryExhausted as exc:
        if exc.network_error is not None:
            # BUI-535: this IS the terminal attempt (on_attempt above is never
            # called for it) — record it here, exactly once, before re-raising.
            if breaker is not None:
                breaker.record_error()
            raise exc.network_error from exc
        # Retries exhausted on a persistently retryable (429/5xx) status —
        # fall through to the same raise_for_status() call below, which
        # raises the equivalent HTTPError (same status/message a caller
        # would have seen from the original hand-rolled loop). breaker.
        # record_error() for THIS terminal attempt happens there (not here)
        # to avoid double-counting it.
        resp = exc.response

    try:
        resp.raise_for_status()

        data = resp.json()

        if "error" in data:
            raise SerpApiError(f"SerpApi error: {data['error']}")

        # Verify the eBay URL actually has LH_Sold=1 — SerpApi silently drops
        # LH_* params if you pass them directly, and a missing sold filter
        # returns active listings (FMV will be wrong, typically far too low).
        ebay_url = data.get("search_metadata", {}).get("ebay_url", "")
        if "LH_Sold=1" not in ebay_url:
            raise SerpApiError(
                f"Sold filter not applied — eBay URL missing LH_Sold=1.\n"
                f"  ebay_url={ebay_url}\n"
                f"  query={nkw}\n"
                "Use show_only=Sold (LH_Sold=1 / LH_Complete=1 are silently dropped)."
            )
    except (SerpApiError, requests.RequestException):
        if breaker is not None:
            breaker.record_error()
        raise

    if breaker is not None:
        breaker.record_success()

    _cache_put(path, data)
    return data, False


# ─── Hard excludes ────────────────────────────────────────────────────────────
#
# BUI-269: the lot/reprint/foreign-edition/trading-card checks that used to
# live in this regex are now sourced from comic_identity.is_comp_excluded()
# (apps/ebay/src/comic_identity.py) — that module is the single source of
# truth, reconciling this lexicon with the near-identical one seller_scan.py
# used to hand-maintain (BUI-253). What remains here is condition/grading/
# damage exclusion with no analog in comic_identity: it isn't about comic
# *identity* at all, so it stays local to the FMV comp pipeline.

LOCAL_EXCLUDE_RE = re.compile(
    r'''
    coverless | no\s+cover | cover\s+torn | cvr\s+off | detached\s+cover |
    missing\s+pages? | missing\s+pin | missing\s+wrap |
    vol[\s.]?[2-9] | \bv[2-9]\b |
    \bpsa\b | \bpgx\b |
    signed\s+by | stan\s+lee.*sign | signature\s+series |
    ww\s+live\s+sale | space\s+filler | restored | water.?stain
    ''',
    re.IGNORECASE | re.VERBOSE,
)


def hard_exclude(title: str) -> bool:
    return comic_identity.is_comp_excluded(title) or bool(LOCAL_EXCLUDE_RE.search(title))


# ─── Grade parsing ────────────────────────────────────────────────────────────

# Fixed numeric regex: covers the full CGC scale including 9.2/9.4/9.6/9.9.
# The previous form `\b([0-9]\.[058])\b` silently dropped those.
#
# BUI-183: exclude price/measurement context.
#   Negative lookbehinds (fixed-width):
#     (?<!\$)  — reject when preceded by a dollar sign (price: $9.5)
#     (?<!x )(?<!X )  — reject when preceded by "x " (second number in a
#                       dimension pair: 2.5 x 3.5); requires exactly one space
#                       so "X-Men" (hyphen, not space) is unaffected.
#   Negative lookahead:
#     (?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["']))
#     — reject when the number is immediately followed (past optional whitespace)
#       by a measurement or shipping unit.  `x\b` catches the first number in a
#       dimension pair ("2.5 x"); word boundary on each unit prevents false
#       matches inside longer words.
_NUMERIC_GRADE_RE = re.compile(
    r'(?<!\$)(?<!x )(?<!X )'
    r'\b([0-9]\.[02-9])'
    r'(?!\w)'  # restore the original trailing boundary: a digit/letter immediately
               # after (e.g. "9.50", "5.50 dollars") is a price/number, not a grade
    r'(?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["\']))'
)

# Letter combos — most specific first. Order matters: slash-combos (e.g.
# VF/NM) must be checked before their single-letter components (NM), since
# `\bnm\b` would otherwise match inside "VF/NM" and short-circuit the loop.
#
# Boundary note: `\b` requires a word↔non-word transition. For patterns
# ending in non-word characters like `+` or `-`, a trailing `\b` fails when
# the next char is whitespace or end-of-string (both non-word). Use `(?!\w)`
# for trailing boundaries on non-word tails.
_LETTER_PATTERNS = [
    # Tier 1 — slash combos (longest first)
    (re.compile(r'\bnm[/\\]m\b', re.I), 9.6),
    (re.compile(r'\bvf[/\\]nm\b', re.I), 9.0),
    (re.compile(r'\bfn[/\\]vf\b|\bfine[/\\]vf\b|\bfvf\b', re.I), 7.0),
    (re.compile(r'\bvg[/\\]fn\+(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg[/\\]fn\b', re.I), 5.0),
    (re.compile(r'\bgd[/\\]vg\b', re.I), 3.0),
    (re.compile(r'\bfr[/\\]gd\b', re.I), 1.5),

    # Tier 2 — letter + modifier (+ / -)
    (re.compile(r'\bnm\+(?!\w)', re.I), 9.6),
    (re.compile(r'\bnm-(?!\w)', re.I), 9.2),
    (re.compile(r'\bvf\+(?!\w)', re.I), 8.5),
    (re.compile(r'\bvf-(?!\w)', re.I), 7.5),
    (re.compile(r'\bfn\+(?!\w)|\bfine\+(?!\w)', re.I), 6.5),
    (re.compile(r'\bfn-(?!\w)|\bfine-(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg\+(?!\w)', re.I), 4.5),
    (re.compile(r'\bvg-(?!\w)', re.I), 3.5),
    (re.compile(r'\bgd\+(?!\w)', re.I), 2.5),

    # Tier 3 — bare letters (must come last; other patterns would match inside)
    (re.compile(r'\bnm\b(?![+\-/\\])', re.I), 9.4),
    (re.compile(r'\bvf\b(?![+\-/\\])', re.I), 8.0),
    (re.compile(r'\bfn\b(?![+\-/\\])|\bfine\b(?![+\-/\\])', re.I), 6.0),
    (re.compile(r'\bvg\b(?![+\-/\\])|\bvery good\b', re.I), 4.0),
    (re.compile(r'\bgd\b(?![+\-/\\])|\bgood\b', re.I), 2.0),
    (re.compile(r'\bfr\b(?![+\-/\\])|\bfair\b', re.I), 1.0),
    (re.compile(r'\bpoor\b', re.I), 0.5),
]


def parse_grade(title: str) -> float | None:
    """Extract a numeric CGC-scale grade from a listing title, or None."""
    m = _NUMERIC_GRADE_RE.search(title)
    if m:
        v = float(m.group(1))
        if 0.5 <= v <= 10.0:
            return v
    for pattern, value in _LETTER_PATTERNS:
        if pattern.search(title):
            return value
    return None


# ─── Comp parsing ─────────────────────────────────────────────────────────────

def _parse_price(raw) -> float | None:
    if raw is None:
        return None
    s = re.sub(r'[^\d.]', '', str(raw).replace(',', ''))
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0.50 <= v <= 50000 else None


def parse_comp(result: dict) -> dict | None:
    """Convert a SerpApi organic_result into our normalized comp shape."""
    title = result.get("title", "")
    if not title:
        return None
    product_id = str(result.get("product_id") or result.get("item_id") or "")
    price_obj = result.get("price") or {}
    price = _parse_price(price_obj.get("extracted") or price_obj.get("raw"))
    if price is None:
        return None
    return {
        "product_id": product_id,
        "title": title,
        "price": price,
        "grade": parse_grade(title),
        "sold_date": result.get("sold_date", ""),
        "buying_format": result.get("buying_format", ""),
        "link": result.get("link", ""),
    }


def _has_next_page(data: dict) -> bool:
    """True when SerpApi's response indicates a further result page exists.

    BUI-523: SerpApi's eBay engine echoes both eBay's own `pagination` object
    and its own `serpapi_pagination` mirror; the latter's `next` field is the
    authoritative "is page 1 full, i.e. does page 2 exist" signal. This
    trusts SerpApi's own pagination resolution rather than reverse-engineering
    a "full page" guess from a raw result-count threshold (eBay's per-page
    count isn't a fixed constant we control, since `_ipg` is left at its
    default). A missing/empty pagination object fails CLOSED — no page-2
    fetch, no extra SerpApi spend — rather than guessing there's more.
    """
    pagination = data.get("serpapi_pagination") or {}
    return bool(pagination.get("next"))


# A genuine slab listing names its certifier (CGC/CBCS) in the title. BUI-524's
# inclusive tier drops the graded excludes, so its results are a MIX of raw and
# slab listings; this is how the two are told apart afterward. Mirrors
# apps/fmv/src/fmv_runner.py's `_SLAB_TITLE_RE`/`_slab_comps_only` — duplicated
# across the package boundary rather than shared, per this repo's existing
# convention (comic-fmv shells out to ebay-sold-comps rather than importing it;
# see CLAUDE.md's "FMV pipeline shells out across package boundaries").
_SLAB_TITLE_RE = re.compile(r"\b(?:cgc|cbcs)\b", re.IGNORECASE)


def _is_slab_comp(comp: dict) -> bool:
    """True for a genuine CGC/CBCS certified-slab comp: grade + price parsed
    AND the certifier named in the title (BUI-524). A comp that merely carries
    a grade token ("… FN 6.0 …") without a certifier name is raw, not a slab."""
    return (comp.get("grade") is not None and comp.get("price") is not None
            and bool(_SLAB_TITLE_RE.search(comp.get("title") or "")))


# ─── Per-book pipeline (three-tier query strategy) ───────────────────────────

def fetch_book_comps(book: dict, api_key: str, *, force: bool = False,
                     ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
                     breaker: "_CircuitBreaker | None" = None) -> dict:
    """Run the three-tier query strategy for one book.

    1. Base query (always): "title issue" year publisher
    2. Auto-broaden (if base <5 results): drop year
    3. Grade-targeted (if <10 grade-tagged comps after parsing base): add grade label

    Tiers 2 and 3 are conditional. Most modern books need only tier 1.

    BUI-523: after tier 1, a gated page-2 fetch of the SAME base query fires
    ONLY when SerpApi confirms page 1 was full (a next page exists — see
    _has_next_page) AND the comp pool is still short on grade-tagged comps
    (< GRADE_TAGGED_THRESHOLD) — i.e. only when a liquid book's comp supply
    is genuinely capped by the single-page fetch AND the extra page is
    likely to help. A thin (non-full) page 1 — the common vintage-book case
    — never has a next page, so it costs zero extra SerpApi searches. Capped
    at exactly one extra page (never page 3+) to bound spend against the
    250/month SerpApi quota.

    BUI-524: after tiers 1-3, a conditional 4th "inclusive" tier fires ONLY for
    a vintage book (pre-_VINTAGE_YEAR_CUTOFF) whose raw pool is still thin
    (< THIN_RESULTS_THRESHOLD comps) on a normal (non-`include_graded`) call.
    It re-queries WITHOUT the `-cgc -cbcs -graded -slab` excludes and splits the
    results via `_is_slab_comp`: raw candidates join the same `comps` pool
    everything else does, while genuine slab comps are kept separately in
    `slab_comps` (never blended into `comps` — a slab price would drag the raw
    pool's median up). This gets a vintage key BOTH its raw comps and a slab
    ladder from ONE extra query — feeding BUI-529's always-on cross-check
    without a dedicated second graded-only pass in the common case. The common
    case (modern/liquid book, or a vintage book whose first three tiers already
    found >= THIN_RESULTS_THRESHOLD raw comps) fires ZERO extra searches, so
    `comps` composition is byte-identical to pre-BUI-524 output whenever this
    tier doesn't fire.

    BUI-537/535: every tier routes through the same `_run` closure, so every
    fetch attempt — including retry attempts that fire *inside* `fetch()` and
    the always-on cross-check/inclusive tier — gets the same trail recording
    and the same circuit-breaker gating. The whole body below is wrapped in a
    try/except so an unexpected exception (e.g. a malformed `book`) still
    returns the partial `queries_used`/`comps`/`slab_comps` trail gathered so
    far, tagged with `error`, instead of losing it — `run_batch()`'s own
    per-book exception boundary used to zero this to `[]` because a raised
    exception never reached this function's normal `return` below.
    """
    queries_used: list[dict] = []
    seen_ids: set[str] = set()
    comps: list[dict] = []
    # BUI-524: populated only when the tier-4 inclusive pass fires; a genuine
    # CGC/CBCS slab comp lands here instead of `comps` (see `_is_slab_comp`).
    slab_comps: list[dict] = []

    def _breaker_tripped() -> bool:
        return bool(breaker.tripped) if breaker is not None else False

    try:
        title = book["title"]
        issue = str(book["issue"])
        year = book.get("year")
        publisher = book.get("publisher")
        variant = book.get("variant")  # BUI-304: now a query keyword, not DB-only
        self_id = str(book.get("item_id", ""))
        # BUI-348: opt-in graded-comp fetch for the CGC-proxy tier. Default
        # (field absent/falsy) keeps exclude_graded=True — every existing
        # caller's queries stay byte-for-byte identical. Only a book explicitly
        # tagged `include_graded: true` (comic-fmv's second, proxy-only pass)
        # drops the `-cgc -cbcs -graded -slab` terms so the CGC/CBCS slab
        # ladder surfaces.
        exclude_graded = not bool(book.get("include_graded"))

        if self_id:
            seen_ids.add(self_id)

        def _run(tier: str, nkw: str, *, page: int = 1, route_slabs: bool = False) -> dict:
            def _record_retry_attempt(outcome: str, detail: str) -> None:
                # BUI-537: a retry attempt this call's own fetch() call made
                # internally and then superseded (a further attempt followed)
                # — a real SerpApi charge that would otherwise be invisible.
                queries_used.append({
                    "tier": tier,
                    "nkw": nkw,
                    "page": page,
                    "outcome": outcome,
                    "error": detail,
                })

            try:
                # BUI-523 note: page defaults to 1, so always forwarding
                # page=page is byte-identical to the old page==1 branch that
                # omitted the kwarg — collapsed now that this call also needs
                # to thread record_attempt/breaker uniformly.
                data, cache_hit = fetch(
                    nkw, api_key, force=force, ttl_sec=ttl_sec, page=page,
                    record_attempt=_record_retry_attempt, breaker=breaker,
                )
            except (SerpApiError, requests.RequestException) as e:
                # BUI-537: page (int) and outcome are now always present,
                # including on page-1 entries — tier/nkw/error unchanged for
                # back-compat (BUI-536's _is_fetch_error keys on 'error').
                queries_used.append({
                    "tier": tier,
                    "nkw": nkw,
                    "page": page,
                    "outcome": f"error:{type(e).__name__}",
                    "error": str(e),
                })
                return {"added": 0, "has_next_page": False}
            added = 0
            for r in data.get("organic_results", []):
                comp = parse_comp(r)
                if comp is None or not comp["product_id"]:
                    continue
                if comp["product_id"] in seen_ids:
                    continue
                if hard_exclude(comp["title"]):
                    continue
                seen_ids.add(comp["product_id"])
                # BUI-524: only the inclusive tier passes route_slabs=True —
                # every other tier's behavior (add every non-excluded comp to
                # `comps`) is byte-for-byte unchanged. A slab comp is counted
                # toward `added` (queries_used stays an honest "how many new
                # things this query found" signal) but never joins the raw
                # `comps` pool.
                if route_slabs and _is_slab_comp(comp):
                    slab_comps.append(comp)
                    added += 1
                    continue
                comps.append(comp)
                added += 1
            queries_used.append({
                "tier": tier,
                "nkw": nkw,
                "raw_results": len(data.get("organic_results", [])),
                "new_comps": added,
                "cached": cache_hit,
                "ebay_url": data.get("search_metadata", {}).get("ebay_url", ""),
                "page": page,
                "outcome": "hit" if cache_hit else "live",
            })
            return {"added": added, "has_next_page": _has_next_page(data)}

        # Tier 1 — base
        base_nkw = build_query(title, issue, year=year, publisher=publisher,
                               variant=variant, exclude_graded=exclude_graded)
        base_result = _run("base", base_nkw)

        # BUI-523: gated page-2 fetch of the SAME base query — see the
        # fetch_book_comps docstring for the full spend-gate rationale. Placed
        # here (before tiers 2/3) so any comps it adds are already in `comps`
        # when tiers 2/3 recompute their own thin/grade-tagged counts below.
        grade_tagged_after_base = sum(1 for c in comps if c["grade"] is not None)
        if base_result["has_next_page"] and grade_tagged_after_base < GRADE_TAGGED_THRESHOLD:
            _run("base", base_nkw, page=2)

        # Tier 2 — auto-broaden if thin. BUI-350: pass the real `vintage_year`
        # (even though the query text drops `year`) so a rebootable-masthead
        # vintage key's broadened query keeps the BUI-347 exclusion terms —
        # this applies to the CGC-proxy graded pass (`include_graded=True`)
        # just as much as the ordinary raw pass, since both share this same
        # tier.
        if len(comps) < THIN_RESULTS_THRESHOLD and year:
            broader_nkw = build_query(title, issue, year=None, publisher=publisher,
                                      variant=variant, exclude_graded=exclude_graded,
                                      vintage_year=year)
            _run("broader", broader_nkw)

        # Tier 3 — grade-targeted if too few grade-tagged comps in pool so far
        target_grade = book.get("grade")
        if isinstance(target_grade, str):
            target_grade = parse_grade(target_grade)
        grade_tagged = sum(1 for c in comps if c["grade"] is not None)
        if target_grade is not None and grade_tagged < GRADE_TAGGED_THRESHOLD:
            label = _grade_label_for_query(target_grade)
            if label:
                grade_nkw = build_query(title, issue, year=year, publisher=publisher,
                                        variant=variant, grade_label=label,
                                        exclude_graded=exclude_graded)
                _run("grade-targeted", grade_nkw)

        # Tier 4 — conditional inclusive pass (BUI-524). See the
        # fetch_book_comps docstring for the full rationale. Gated tightly
        # against the 250/month SerpApi quota: fires only for a vintage book
        # (own cover year, not the query-text year tiers 2/3 may have
        # dropped) whose raw pool is STILL thin after tiers 1-3, and only on
        # a normal (non-`include_graded`) call — an explicit graded-only pass
        # already runs every tier inclusive, so a 4th inclusive tier there
        # would be pure duplicate spend.
        is_vintage = isinstance(year, (int, float)) and year < _VINTAGE_YEAR_CUTOFF
        if exclude_graded and is_vintage and len(comps) < THIN_RESULTS_THRESHOLD:
            inclusive_nkw = build_query(title, issue, year=year, publisher=publisher,
                                        variant=variant, exclude_graded=False,
                                        vintage_year=year)
            _run("inclusive", inclusive_nkw, route_slabs=True)

        out_input = {
            "item_id": self_id or None,
            "title": title,
            "issue": issue,
            "year": year,
            "publisher": publisher,
            "grade": target_grade,
        }
        # BUI-174/187: echo back the caller's correlation id (when present) so
        # a batch driver can map results to inputs by identity, not list
        # position. A bare item_id is not reliable (may be absent or shared),
        # so the id is a dedicated field threaded by the caller; standalone
        # callers omit it.
        req_id = book.get("_req_id")
        if req_id is not None:
            out_input["_req_id"] = req_id
        return {
            "input": out_input,
            "queries_used": queries_used,
            "comps": comps,
            # BUI-524: always present (shape parity) — empty unless the
            # tier-4 inclusive pass fired and found genuine CGC/CBCS slab
            # comps.
            "slab_comps": slab_comps,
            # BUI-535: whether the batch-scoped breaker had tripped by the
            # time this book finished — surfaced so callers (comic-fmv, a
            # human skimming --out) can distinguish "outage" from "priced".
            "breaker_tripped": _breaker_tripped(),
        }
    except Exception as e:  # noqa: BLE001 — BUI-537: preserve the partial
        # trail rather than losing it; see the docstring above. `book.get(...)`
        # throughout (not the local `title`/`issue`/... names) because those
        # may never have been assigned if the exception fired before they
        # were (e.g. a missing "title"/"issue" key).
        out_input = {
            "item_id": (str(book.get("item_id")) if book.get("item_id") else None),
            "title": book.get("title"),
            "issue": (str(book.get("issue")) if book.get("issue") is not None else None),
            "year": book.get("year"),
            "publisher": book.get("publisher"),
            "grade": book.get("grade"),
        }
        req_id = book.get("_req_id")
        if req_id is not None:
            out_input["_req_id"] = req_id
        return {
            "input": out_input,
            "queries_used": queries_used,
            "comps": comps,
            "slab_comps": slab_comps,
            "breaker_tripped": _breaker_tripped(),
            "error": str(e),
        }


def _grade_label_for_query(grade: float) -> str | None:
    """Pick a coarse letter grade to add to a query. Stays inside the bucket
    that contains `grade` so the search doesn't drift away from the target."""
    if grade >= 9.0:
        return "NM"
    if grade >= 8.0:
        return "VF"
    if grade >= 7.0:
        return "VF"  # FN/VF tagging is rare; VF surfaces upper bracket
    if grade >= 6.0:
        return "FN"
    if grade >= 4.5:
        return "VG"
    if grade >= 3.0:
        return "GD"
    return None


# ─── Batch driver ─────────────────────────────────────────────────────────────

def run_batch(books: list[dict], api_key: str, *, force: bool = False,
              ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
              max_workers: int = DEFAULT_MAX_WORKERS) -> list[dict]:
    """Fan out across books with a thread pool.

    BUI-535: one `_CircuitBreaker` is created here and threaded explicitly
    into every `fetch_book_comps()` call for this batch — scoped to this one
    `run_batch()` call, not a module-level singleton, so a fresh invocation
    (e.g. a re-run after "SerpApi appears down") always starts with a clean,
    untripped breaker. `force` does not exempt a book from it (see fetch()).
    """
    results: list[dict] = [None] * len(books)
    breaker = _CircuitBreaker(CIRCUIT_BREAKER_THRESHOLD, total_books=len(books))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_book_comps, b, api_key, force=force, ttl_sec=ttl_sec,
                       breaker=breaker): i
            for i, b in enumerate(books)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            breaker.book_completed()
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001  # batch boundary — capture per-book errors, continue
                # BUI-537: fetch_book_comps() now catches its own exceptions
                # internally and always returns a dict carrying whatever
                # partial queries_used/comps trail it had gathered (see its
                # docstring) — this branch should be effectively unreachable
                # in normal operation. Kept only as a last-resort guard for a
                # truly catastrophic failure that never even reached
                # fetch_book_comps's own try/except (e.g. the future itself
                # being cancelled) — there's no partial trail to recover here
                # since it lives inside that function's local scope.
                book = books[i]
                results[i] = {
                    "input": book,
                    "queries_used": [],
                    "comps": [],
                    "breaker_tripped": bool(breaker.tripped),
                    "error": str(e),
                }
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _print_human(results: list[dict]) -> None:
    for r in results:
        inp = r["input"]
        label = f"{inp['title']} #{inp['issue']}"
        if inp.get("year"):
            label += f" ({inp['year']})"
        if "error" in r:
            print(f"  {label}: ERROR {r['error']}")
            continue
        n_total = len(r["comps"])
        n_graded = sum(1 for c in r["comps"] if c["grade"] is not None)
        # BUI-523: a page-2 entry shares its tier's name (e.g. "base") with
        # its own page-1 entry — tag it "(pN)" here so a human skimming
        # --quiet=false output can see the gated extra-page fetch fired,
        # without changing the stored "tier"/"page" fields other consumers
        # read. BUI-537 made `page` always present (including page 1) — only
        # tag when it's not the implicit default, or every entry would show
        # "(p1)".
        tiers = ",".join(
            f'{q["tier"]}(p{q["page"]})' if q.get("page", 1) != 1 else q["tier"]
            for q in r["queries_used"]
        )
        cached = sum(1 for q in r["queries_used"] if q.get("cached"))
        breaker_note = " [breaker-tripped]" if r.get("breaker_tripped") else ""
        print(f"  {label}: {n_total} comps ({n_graded} grade-tagged) "
              f"tiers=[{tiers}] cached={cached}/{len(r['queries_used'])}{breaker_note}")

    # BUI-535: aggregate stdout visibility (distinct from the one-time stderr
    # warning _CircuitBreaker.record_error() prints the instant it trips) —
    # so a human skimming --quiet=false output sees the batch was affected
    # even if they missed the stderr line.
    n_breaker_tripped = sum(1 for r in results if r.get("breaker_tripped"))
    if n_breaker_tripped:
        print(
            f"\n  SerpApi circuit breaker tripped during this batch — "
            f"{n_breaker_tripped} book(s) served cache-only or fetch-err; "
            "re-run later."
        )


def _read_batch(path: str) -> list[dict]:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text())


def _write_out(path: str | None, data) -> None:
    if path is None:
        return
    if path == "-":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    Path(path).write_text(json.dumps(data, indent=2))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="sold-comps",
        description="Fetch eBay sold listings for a comic via SerpApi.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Print the installed version and the git SHA/date it was built "
             "from, then exit. Use this to check for a stale `uv tool install` "
             "(see scripts/install.sh).",
    )
    p.add_argument("--batch", help="Path to JSON batch file ('-' for stdin).")
    p.add_argument("--title", help="Series title (single-query mode).")
    p.add_argument("--issue", help="Issue number (single-query mode).")
    p.add_argument("--year", type=int, help="Cover year (single-query mode).")
    p.add_argument("--publisher", help="Publisher (recommended for indie titles).")
    p.add_argument("--variant", help="Distribution variant keyword (e.g. Newsstand).")
    p.add_argument("--grade", type=float, help="Target grade (single-query mode).")
    p.add_argument("--item-id", help="Self-exclude this product_id from comps.")
    p.add_argument("--include-graded", action="store_true",
                   help="Include CGC/CBCS graded (slab) comps instead of "
                        "excluding them (BUI-348, for the CGC-proxy tier). "
                        "Default: graded copies are excluded.")
    p.add_argument("--out", help="Write full JSON to this path ('-' for stdout).")
    p.add_argument("--force", action="store_true",
                   help="Bypass cache and refetch.")
    p.add_argument("--cache-ttl-days", type=float, default=7.0,
                   help="Cache TTL in days (default: 7).")
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                   help="Thread pool size for batch mode (default: 10).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress human summary on stdout.")
    args = p.parse_args(argv)

    api_key = load_serpapi_key()
    ttl_sec = int(args.cache_ttl_days * 24 * 3600)

    if args.batch:
        books = _read_batch(args.batch)
        if not isinstance(books, list):
            print("Error: batch file must contain a JSON array.", file=sys.stderr)
            return 2
    elif args.title and args.issue:
        books = [{
            "title": args.title,
            "issue": args.issue,
            "year": args.year,
            "publisher": args.publisher,
            "variant": args.variant,
            "grade": args.grade,
            "item_id": args.item_id,
            "include_graded": args.include_graded,
        }]
    else:
        p.error("provide --batch <file> or (--title and --issue)")

    results = run_batch(books, api_key, force=args.force, ttl_sec=ttl_sec,
                        max_workers=args.max_workers)

    if not args.quiet:
        _print_human(results)

    if args.out:
        _write_out(args.out, results)
    elif args.quiet:
        # Quiet + no --out is a misuse; emit JSON to stdout so callers get something.
        _write_out("-", results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
