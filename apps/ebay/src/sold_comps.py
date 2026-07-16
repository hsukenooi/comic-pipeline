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


def canonical_serpapi_url(nkw: str) -> str:
    """Build the SerpApi URL with deterministic param order (for cache key).

    Excludes api_key from the canonical form so we don't tie cache to a
    specific user's key. The actual request URL adds api_key separately.
    """
    params = {
        "engine": "ebay",
        "_nkw": nkw,
        "show_only": "Sold",
    }
    canonical = urllib.parse.urlencode(sorted(params.items()))
    return f"{SERPAPI_ENDPOINT}?{canonical}"


def request_url(canonical: str, api_key: str) -> str:
    sep = "&" if "?" in canonical else "?"
    return f"{canonical}{sep}api_key={api_key}"


# ─── Fetch (with cache + URL verification) ────────────────────────────────────

class SerpApiError(Exception):
    pass


def fetch(nkw: str, api_key: str, *, force: bool = False,
          ttl_sec: int = DEFAULT_CACHE_TTL_SEC) -> tuple[dict, bool]:
    """Fetch a SerpApi response with caching. Returns (data, cache_hit)."""
    canonical = canonical_serpapi_url(nkw)
    path = _cache_path(canonical)

    if not force:
        cached = _cache_get(path, ttl_sec)
        if cached is not None:
            return cached, True

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
        )
    except RetryExhausted as exc:
        if exc.network_error is not None:
            raise exc.network_error from exc
        # Retries exhausted on a persistently retryable (429/5xx) status —
        # fall through to the same raise_for_status() call below, which
        # raises the equivalent HTTPError (same status/message a caller
        # would have seen from the original hand-rolled loop).
        resp = exc.response
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


# ─── Per-book pipeline (three-tier query strategy) ───────────────────────────

def fetch_book_comps(book: dict, api_key: str, *, force: bool = False,
                     ttl_sec: int = DEFAULT_CACHE_TTL_SEC) -> dict:
    """Run the three-tier query strategy for one book.

    1. Base query (always): "title issue" year publisher
    2. Auto-broaden (if base <5 results): drop year
    3. Grade-targeted (if <10 grade-tagged comps after parsing base): add grade label

    Tiers 2 and 3 are conditional. Most modern books need only tier 1.
    """
    title = book["title"]
    issue = str(book["issue"])
    year = book.get("year")
    publisher = book.get("publisher")
    variant = book.get("variant")  # BUI-304: now a query keyword, not DB-only
    self_id = str(book.get("item_id", ""))
    # BUI-348: opt-in graded-comp fetch for the CGC-proxy tier. Default (field
    # absent/falsy) keeps exclude_graded=True — every existing caller's queries
    # stay byte-for-byte identical. Only a book explicitly tagged
    # `include_graded: true` (comic-fmv's second, proxy-only pass) drops the
    # `-cgc -cbcs -graded -slab` terms so the CGC/CBCS slab ladder surfaces.
    exclude_graded = not bool(book.get("include_graded"))

    queries_used: list[dict] = []
    seen_ids: set[str] = set()
    if self_id:
        seen_ids.add(self_id)
    comps: list[dict] = []

    def _run(tier: str, nkw: str) -> int:
        try:
            data, cache_hit = fetch(nkw, api_key, force=force, ttl_sec=ttl_sec)
        except (SerpApiError, requests.RequestException) as e:
            queries_used.append({"tier": tier, "nkw": nkw, "error": str(e)})
            return 0
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
            comps.append(comp)
            added += 1
        queries_used.append({
            "tier": tier,
            "nkw": nkw,
            "raw_results": len(data.get("organic_results", [])),
            "new_comps": added,
            "cached": cache_hit,
            "ebay_url": data.get("search_metadata", {}).get("ebay_url", ""),
        })
        return added

    # Tier 1 — base
    base_nkw = build_query(title, issue, year=year, publisher=publisher,
                           variant=variant, exclude_graded=exclude_graded)
    _run("base", base_nkw)

    # Tier 2 — auto-broaden if thin. BUI-350: pass the real `vintage_year`
    # (even though the query text drops `year`) so a rebootable-masthead
    # vintage key's broadened query keeps the BUI-347 exclusion terms — this
    # applies to the CGC-proxy graded pass (`include_graded=True`) just as
    # much as the ordinary raw pass, since both share this same tier.
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

    out_input = {
        "item_id": self_id or None,
        "title": title,
        "issue": issue,
        "year": year,
        "publisher": publisher,
        "grade": target_grade,
    }
    # BUI-174/187: echo back the caller's correlation id (when present) so a
    # batch driver can map results to inputs by identity, not list position.
    # A bare item_id is not reliable (may be absent or shared), so the id is a
    # dedicated field threaded by the caller; standalone callers omit it.
    req_id = book.get("_req_id")
    if req_id is not None:
        out_input["_req_id"] = req_id
    return {
        "input": out_input,
        "queries_used": queries_used,
        "comps": comps,
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
    """Fan out across books with a thread pool."""
    results: list[dict] = [None] * len(books)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_book_comps, b, api_key, force=force, ttl_sec=ttl_sec): i
            for i, b in enumerate(books)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001  # batch boundary — capture per-book errors, continue
                book = books[i]
                results[i] = {
                    "input": book,
                    "queries_used": [],
                    "comps": [],
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
        tiers = ",".join(q["tier"] for q in r["queries_used"])
        cached = sum(1 for q in r["queries_used"] if q.get("cached"))
        print(f"  {label}: {n_total} comps ({n_graded} grade-tagged) "
              f"tiers=[{tiers}] cached={cached}/{len(r['queries_used'])}")


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
