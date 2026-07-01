#!/usr/bin/env python3
"""seller-scan: Match an eBay seller's active listings against your LOCG wish list."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import anthropic
import requests

from ebay_fetch import (
    UnknownSellerError,
    get_token,
    load_config,
    load_seller_aliases,
    parse_item_summary,
    resolve_seller_username,
    save_seller_alias,
    search_seller_listings,
)


# ─── Server URL resolution (BUI-220) ──────────────────────────────────────────

_DEPRECATION_WARNED = False


def _server_base():
    """Return the comics server base URL (trailing slash trimmed), or "".

    BUI-220: the canonical env var is COMICS_SERVER_URL; GIXEN_SERVER_URL is a
    deprecated alias still read as a fallback. Using only the old var emits a
    one-line deprecation warning to stderr (once).
    """
    global _DEPRECATION_WARNED
    base = os.environ.get("COMICS_SERVER_URL", "").rstrip("/")
    if base:
        return base
    legacy = os.environ.get("GIXEN_SERVER_URL", "").rstrip("/")
    if legacy and not _DEPRECATION_WARNED:
        print(
            "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
            file=sys.stderr,
        )
        _DEPRECATION_WARNED = True
    return legacy


# ─── Wish list fetching ───────────────────────────────────────────────────────

def fetch_wish_list():
    """Fetch the wish list from the gixen server API. Returns a list of
    {id, name} dicts.

    BUI-88 (R10): seller-scan lives in apps/ebay, which is NOT a uv workspace
    member and cannot import locg-cli — so it fetches the wish-list over HTTP
    from the server's /api/comics/wish-list endpoint instead of shelling out to
    the `locg` CLI. Fails loudly on any unreachable-server / non-200 / bad-JSON
    condition (never returns a partial or empty list silently) so a scan can't
    run against a stale or empty wish list because the server was down.
    """
    base = _server_base()
    if not base:
        print(
            "Error: COMICS_SERVER_URL is not set — cannot reach the wish-list API.\n"
            "Set it in ~/.zshrc (MacBook → http://mac-mini.tail9b7fa5.ts.net:8080; "
            "Mac Mini → http://localhost:8080).",
            file=sys.stderr,
        )
        sys.exit(1)
    url = f"{base}/api/comics/wish-list"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching wish list from {url}: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing wish list JSON from {url}: {e}", file=sys.stderr)
        sys.exit(1)


# ─── Seen-tracking (BUI-113) ──────────────────────────────────────────────────

def fetch_seen_item_ids(seller):
    """Best-effort: return the set of item_ids already surfaced in a prior scan.

    BUI-113: unlike fetch_wish_list (which hard-fails — an empty wish-list would
    cause a wrong "no match"), seen-tracking is non-fatal. A failed read only
    risks re-showing a listing you've seen (mildly annoying, always safe);
    hiding everything because the server is down could silently suppress a real
    buy. So any failure → empty set + a warning, and the scan continues showing
    all matches.
    """
    base = _server_base()
    if not base:
        return set()
    url = f"{base}/api/comics/seller-scan/seen"
    try:
        resp = requests.get(url, params={"seller": seller}, timeout=10)
        resp.raise_for_status()
        return set(resp.json().get("item_ids", []))
    except (requests.exceptions.RequestException, ValueError) as e:
        print(
            f"Warning: could not fetch seen item IDs ({e}); showing all matches",
            file=sys.stderr,
        )
        return set()


def record_items_seen(item_ids, seller):
    """Best-effort: mark surfaced item_ids as seen so future scans skip them.

    Warns on failure but never aborts (see fetch_seen_item_ids for the rationale).
    """
    base = _server_base()
    if not base or not item_ids:
        return
    url = f"{base}/api/comics/seller-scan/seen"
    try:
        resp = requests.post(
            url, json={"item_ids": item_ids, "seller": seller}, timeout=10
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Warning: could not record seen item IDs ({e})", file=sys.stderr)


# ─── Matching ─────────────────────────────────────────────────────────────────

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "in", "vol", "comics"})

# Edition words that signal a special printing type.  If a listing title
# contains one but the wish-item series does NOT, the listing is an obvious
# non-match and can be rejected before fuzzy scoring (hard_reject rule 2).
# Giant[-\s]Size and King[-\s]Size match both hyphenated and spaced forms.
# "special" is intentionally excluded: it's a common cover/variant descriptor
# ("Special Edition cover", "Holiday Special variant") and a hard-reject on
# bare \bspecial\b causes too many false negatives (BUI-221 Finding 3).
_EDITION_PATTERNS = [
    re.compile(r"\bannual\b", re.IGNORECASE),
    re.compile(r"\bgiant[\s-]size\b", re.IGNORECASE),
    re.compile(r"\bking[\s-]size\b", re.IGNORECASE),
    re.compile(r"\btreasury\b", re.IGNORECASE),
]

# Multi-comic-lot signals.  Any match → listing is a bundle, not a single issue.
_LOT_RE = re.compile(
    r"\blot\s+of\b"           # "lot of"
    r"|\b\d+\s+lot\b"         # "5 lot", "10-comic lot"
    r"|\blot\s+\d+"           # "lot 5", "lot 10"
    r"|\bcollection\b"         # "complete collection", "run collection"
    r"|\bcomplete\s+run\b"     # "complete run"
    r"|\bcomplete\s+set\b"     # BUI-243: "complete set" (full-run bundles)
    r"|\bset\s+of\b"          # "set of 10"
    r"|#\d+\s*-\s*#?\d+"      # issue range: "#1-#10" or "#1-10" (# required on
                               # the first number so bare "YYYY-YYYY" run ranges
                               # in single-issue titles don't false-reject)
    # BUI-243: quantity-word-prefixed ranges — "Books 1-4", "issues 2-4", "issue 1-6".
    # The bare-number range branch requires a quantity word as prefix so that a bare
    # "YYYY-YYYY" year-span in a single-issue title (e.g. "ASM #4 1962-1963") never
    # matches.  '#' is handled by the existing #\d+-#?\d+ branch above.
    # The \d{1,3} bound (NOT \d+) is load-bearing: it stops a 4-digit YEAR span that
    # follows the quantity word — e.g. "First Issue 1962-1963", "Key Issue 1962-1964",
    # "issue 1963-1964" — from being read as an issue range. Issue numbers in a range
    # are 1-3 digits; a >999 issue in a range is vanishingly rare (and the failure
    # direction there is a missed lot-reject, which is the safe side).
    r"|\b(?:books?|issues?)\s*\d{1,3}\s*-\s*#?\d{1,3}\b"
    # BUI-243: spelled-out range — "issues 1 through 6", "books 1 through 4"
    r"|\b(?:books?|issues?)\s+\d{1,3}\s+through\s+\d{1,3}\b",
    re.IGNORECASE,
)

# BUI-135: numeric grade tokens (CGC/CBCS slab grades and raw grade shorthand)
# look like "7.0", "8.5", "9.2", "9.4". _normalize() replaces the "." with a
# space, orphaning the integer part ("7.0" -> "7 0"); match_listing's bare-\bN\b
# issue branch then matched that orphaned "7" as wish issue #7, producing
# false positives at score 1.00 (seller comichunterlv: 61 of 62 false). Strip
# the whole decimal-grade token *before* normalizing so neither half survives.
# This catches the raw forms too ("F/VF 7.0", "VF/NM 9.0") which the literal
# "cgc"-skip guard in main() does NOT — they carry no "cgc" string.
_GRADE_RE = re.compile(r"\b\d{1,2}\.\d\b")

# BUI-135 (code-review follow-up): a grade written WITHOUT a decimal still
# orphans into a fake issue number ("VF/NM 9" -> bare "9" matched wish #9).
# Strip a bare integer ONLY when it's prefixed by a grade-letter token
# (VF, NM, FN, GD, VG, F, G, optionally combined like "F/VF" or "NM+"), so we
# kill grade-shorthand digits without touching a plain "\bN\b" that has no
# grade word in front of it (e.g. "X-Men 9" or "#9" must still match issue 9 —
# the matcher's loose bias is preserved).
_GRADE_LETTER_RE = re.compile(
    r"\b(?:VF|NM|FN|GD|VG|F|G)(?:[/+-]*(?:VF|NM|FN|GD|VG|F|G))*[/+-]*\s*\d{1,2}\b",
    re.IGNORECASE,
)


def _strip_grades(text):
    """Remove grade tokens so their digits can't be mistaken for an issue
    number. Covers decimal grades ('7.0', '9.4') and grade-letter-prefixed
    bare integers ('VF 9', 'VF/NM 9', 'NM 8', 'FN+ 6'). A plain bare integer
    with no grade-letter prefix is deliberately left alone so a real issue
    number ('X-Men 9', '#9') still matches. See BUI-135."""
    text = _GRADE_RE.sub(" ", text)
    text = _GRADE_LETTER_RE.sub(" ", text)
    return text


def _normalize(text):
    """Lowercase and strip non-alphanumeric characters."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


def _series_tokens(series):
    """Return significant tokens from a series name."""
    return [t for t in _normalize(series).split() if len(t) >= 2 and t not in _STOPWORDS]


def _parse_wish_name(name):
    """Parse 'Series Name #N' into (series, issue_number) or (name, None)."""
    m = re.match(r"^(.*?)\s*#(\d+\w*)\s*$", name.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return name.strip(), None


def prepare_wish_items(wish_list):
    """Augment wish list items with parsed series + issue for matching."""
    out = []
    for item in wish_list:
        name = item.get("name", "")
        series, issue = _parse_wish_name(name)
        tokens = _series_tokens(series)
        if not tokens or issue is None:
            continue
        # BUI-226: carry raw LOCG series_name (decorated, e.g.
        # "The Amazing Spider-Man (Vol. 1) (1963 - 1998)") and release year
        # for era-gate disambiguation.  _series_name may be None for ~9% of
        # local-only items that were added without a Metron round-trip.
        release_date = item.get("release_date") or ""
        out.append({
            "id": item.get("id"),
            "name": name,
            "series": series,
            "issue": issue,
            "_tokens": tokens,
            "_series_name": item.get("series_name"),
            "_release_year": release_date[:4] or None,
        })
    return out


def match_listing(title, wish_items):
    """Return (best_wish_item, score) or (None, 0.0) for an eBay listing title.

    Requires:
    - Issue number present in title as #N or as isolated digits
    - At least 50% of series tokens present in title
    """
    # BUI-135: strip decimal grade tokens before normalizing so a slab/raw grade
    # like "7.0" or "9.4" can't orphan its integer into a fake issue-number match.
    title_norm = _normalize(_strip_grades(title))
    best = None
    best_score = 0.0

    for wish in wish_items:
        issue = wish["issue"]
        # Issue number check: look for #N or space-bounded N.
        # BUI-184: add a trailing \b to the #\s*N branch so that "#3" does not
        # prefix-match "#300". In practice _normalize() strips "#" before this
        # runs (so the first branch rarely fires on a normalized title), but the
        # trailing boundary hardens it defensively against any future caller that
        # passes un-normalized input. Both branches now have symmetric boundaries.
        issue_pattern = re.compile(
            r"(?:#\s*" + re.escape(issue) + r"\b|\b" + re.escape(issue) + r"\b)"
        )
        if not issue_pattern.search(title_norm):
            continue

        tokens = wish["_tokens"]
        title_words = set(title_norm.split())
        matched = sum(1 for t in tokens if t in title_words)
        score = matched / len(tokens)

        if score > best_score:
            best_score = score
            best = wish

    if best_score >= 0.65:
        return best, best_score
    return None, 0.0


def hard_reject(title, series, issue):
    """Return True if the listing title is an obvious non-match for this wish item.

    Conservative pre-filter: only drops clear mismatches — never rejects a
    genuine single-issue copy.  Callers (e.g. the wishlist-sellers funnel)
    should call this before match_listing to shrink the candidate pool cheaply.

    Rules applied in order:
      1. CGC slab — "cgc" in title.  This scan is raw/ungraded only; mirrors
         the existing ``if "cgc" in ...`` skip in main() so callers using
         hard_reject get that guard for free.
      2. Edition mismatch — title contains Annual / Giant-Size / Giant Size /
         King-Size / King Size / Special / Treasury (word-boundary,
         case-insensitive) but the wish-item ``series`` does NOT contain that
         same word.  Example: "Avengers Annual #1" is rejected for a wish item
         whose series is "The Avengers", but kept for "Avengers Annual".
      3. Multi-comic lot — title matches a lot/collection/complete-run/set-of
         or issue-range pattern (e.g. "#1–#10").
      4. Missing issue number — if ``issue`` is not None, the normalised title
         must contain it as a bounded token (#N or bare N), using the same
         word-boundary regex as match_listing so the two are consistent.
    """
    # Rule 1: CGC slab
    if "cgc" in title.lower():
        return True

    # Rule 2: edition mismatch
    for pat in _EDITION_PATTERNS:
        if pat.search(title) and not pat.search(series):
            return True

    # Rule 3: multi-comic lot
    if _LOT_RE.search(title):
        return True

    # Rule 4: missing issue number (same logic as match_listing)
    if issue is not None:
        title_norm = _normalize(_strip_grades(title))
        issue_pat = re.compile(
            r"(?:#\s*" + re.escape(issue) + r"\b|\b" + re.escape(issue) + r"\b)"
        )
        if not issue_pat.search(title_norm):
            return True

    return False


# ─── Series-volume (era) disambiguation (BUI-226) ────────────────────────────
# Helpers for matching listings against the correct series era/volume.
#
# series_year_range is ported from
# packages/locg-cli/src/locg/collection_cache.py — keep in sync if the locg
# source changes.  The locg version uses the same _DASH_CLASS / regex shapes;
# they are inlined here because apps/ebay is NOT a uv workspace member and
# cannot import locg.

_ERA_DASH_CLASS = r"[-–—−]"  # ASCII hyphen, en-dash, em-dash, minus
_ERA_YEAR_RANGE_RE = re.compile(
    rf"\((\d{{4}})\s*{_ERA_DASH_CLASS}\s*(\d{{4}}|Present)\)", re.IGNORECASE
)
_ERA_BARE_YEAR_RE = re.compile(r"\s*\(\d{4}\)")
_ERA_OPEN_END = 9999  # sentinel for "(YYYY - Present)" open-ended ranges


def series_year_range(series_name: str) -> "tuple[int, int] | None":
    """Extract (begin_year, end_year) from a decorated LOCG series name, or None.

    Ported from packages/locg-cli/src/locg/collection_cache.py.
    ``(YYYY - Present)`` → open-ended (begin_year, 9999).
    A bare single year ``(YYYY)`` → (YYYY, YYYY) — not open-ended.
    Dash variants (en/em-dash) are accepted.

    Examples:
      "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"  → (1963, 1998)
      "The Amazing Spider-Man (2022 - Present)"         → (2022, 9999)
      "Wolverine (1988)"                                → (1988, 1988)
    """
    if not series_name:
        return None
    m = _ERA_YEAR_RANGE_RE.search(series_name)
    if m:
        begin = int(m.group(1))
        end_tok = m.group(2)
        end = _ERA_OPEN_END if end_tok.lower() == "present" else int(end_tok)
        return (begin, end)
    bare = _ERA_BARE_YEAR_RE.search(series_name)
    if bare:
        yr = int(bare.group(0).strip().strip("()"))
        return (yr, yr)
    return None


def series_volume(series_name: str) -> "int | None":
    """Extract the volume number from a decorated LOCG series name, or None.

    Handles "(Vol. N)" and "(Volume N)" (case-insensitive).

    Examples:
      "The Amazing Spider-Man (Vol. 1) (1963 - 1998)" → 1
      "Wolverine (Volume 2) (1982 - 2003)"             → 2
      "Batman #100"                                    → None
    """
    m = re.search(r"\(Vol(?:ume)?\.?\s*(\d+)\)", series_name or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def _title_paren_years(title: str) -> "list[int]":
    """Extract every 4-digit year (1930–2035) from every parenthetical group in a title.

    Only parenthesized years are extracted (high precision).  Bare years in
    listing titles are too ambiguous for deterministic rejection and are left to
    Haiku.

    Handles compound parentheticals like "(Marvel Comics December 2014)" as well
    as multiple groups like "(1963) (CGC 2024)".

    Examples:
      "Amazing Spider-Man (2022) #7"                    → [2022]
      "Amazing Spider-Man #7 (Marvel Comics Dec 2014)"  → [2014]
      "Batman (1940) #100 NM"                           → [1940]
      "Some Book (1963) (CGC 2024)"                     → [1963, 2024]
      "Amazing Spider-Man #7 VF"                        → []
    """
    years: list[int] = []
    for group in re.findall(r"\([^)]*\)", title or ""):
        for m in re.finditer(r"\b(\d{4})\b", group):
            yr = int(m.group(1))
            if 1930 <= yr <= 2035:
                years.append(yr)
    return years


def _title_paren_year(title: str) -> "int | None":
    """Extract the first parenthesized 4-digit year (1930–2035) from a title.

    Thin wrapper around _title_paren_years for callers that only need the first
    year.  Kept for backward compatibility.

    Examples:
      "Amazing Spider-Man (2022) #7"  → 2022
      "Amazing Spider-Man #7 VF"      → None
      "Batman (1940) #100 NM"         → 1940
    """
    yrs = _title_paren_years(title)
    return yrs[0] if yrs else None


def _title_volume(title: str) -> "int | None":
    """Extract an explicit volume number from a listing title, or None.

    Matches "vol N", "vol. N", "volume N" (case-insensitive, word-boundary).
    Parenthesized years are handled separately by _title_paren_year.

    Examples:
      "Amazing Spider-Man Vol 3 #7"  → 3
      "X-Men Volume 1 #94"           → 1
      "Amazing Spider-Man #7 VF"     → None
    """
    m = re.search(r"\bvol(?:ume)?\.?\s*(\d+)\b", title or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def era_mismatch(
    title: str,
    series_name: "str | None",
    release_year: "str | None" = None,  # BUI-240
) -> bool:
    """Return True (reject) only when the listing title clearly contradicts the wish era.

    FAIL-OPEN design (BUI-226): a false reject (dropping a genuine in-era copy
    the user wants) is worse than a false accept.  Returns False whenever a
    needed signal is missing from either side.  Only two high-precision signals:

      1. Parenthesized years in the title vs. the wish-item issue year or,
         as fallback, the series year range:

         - Signal 1 (BUI-240 — takes priority when ``release_year`` available):
           If ``release_year`` parses to a 4-digit int ``iy`` AND the title
           has at least one parenthesized year, reject if NONE of the title's
           years fall within ``[iy-1, iy+1]``.  This is strictly tighter than
           the range check: a 1999 listing for ASM Vol. 1 is rejected when the
           wished issue has release_year=1963 (iy±1=[1962,1964] excludes 1999),
           even though 1999=end+1 was within the series-range tolerance band.

         - Signal 1-fallback (existing BUI-226 behavior): if ``release_year``
           is absent or unparseable, compare against the series year range with
           ±1 tolerance (BUI-214 cover-vs-onsale skew).

         - Titles with NO parenthesized year fail-open regardless of
           ``release_year`` (bare years are left to Haiku — unchanged).

      2. Explicit "vol N" in the title vs. the series volume number — e.g.
         "X-Men Vol 2 #94" vs. a Vol. 1 wish item.

    Bare/ambiguous years (no parentheses) and any other signals are left to
    the Haiku verify step.
    """
    if not series_name:
        return False

    # Signal 1: parenthesized years in title vs. issue release year or series year range.
    yrs = _title_paren_years(title)
    if yrs:
        # BUI-240: per-issue release year takes priority over series-range (tighter gate).
        iy: "int | None" = None
        if release_year is not None:
            try:
                parsed = int(str(release_year).strip())
                if len(str(parsed)) == 4:
                    iy = parsed
            except (ValueError, TypeError):
                pass

        if iy is not None:
            # Per-issue year available: reject if NONE of the title years are within ±1.
            if not any(iy - 1 <= yr <= iy + 1 for yr in yrs):
                return True
        else:
            # No per-issue year: fall back to series-range check (BUI-226).
            yr_range = series_year_range(series_name)
            if yr_range is not None:
                begin, end = yr_range
                if not any(begin - 1 <= yr <= end + 1 for yr in yrs):
                    return True

    # Signal 2: explicit volume in title vs. series volume.
    title_vol = _title_volume(title)
    if title_vol is not None:
        wish_vol = series_volume(series_name)
        if wish_vol is not None and title_vol != wish_vol:
            return True

    return False


def publication_year_mismatch(
    aspects: "dict | None",
    series_name: "str | None",
    release_year: "str | None" = None,
) -> bool:
    """Return True (reject) when item-specifics clearly contradict the wish series era.

    FAIL-OPEN design (BUI-229): returns False when any needed signal is missing.

    Priority:
      1. ``Publication Year`` present and parseable as a 4-digit int: reject iff
         outside ``[begin-1, end+1]`` (same ±1 tolerance as era_mismatch).
      2. ``Publication Year`` absent: conservative fallback using the ``Era`` aspect
         and the per-issue release year (BUI-231):
         - Parse ``release_year`` (4-char string from prepare_wish_items) into
           ``iy`` (int) if possible.
         - If ``Era`` contains "modern age":
           * ``iy is not None`` and ``iy < 1992`` → return True (reject: the wished
             issue predates Modern Age but the listing's Era says Modern Age).
           * ``iy is None`` and series range ``end < 1992`` → return True (old
             series-end fallback: preserves behavior when no per-issue year).
         - Otherwise → return False (fail-open).

    Reuses ``series_year_range`` for the year range.
    """
    if not aspects or not series_name:
        return False
    yr_range = series_year_range(series_name)
    if yr_range is None:
        return False
    begin, end = yr_range

    pub_year_raw = aspects.get("Publication Year")
    if pub_year_raw is not None:
        try:
            pub_year = int(str(pub_year_raw).strip())
        except (ValueError, TypeError):
            return False  # unparseable → fail-open
        if len(str(pub_year)) != 4:
            return False  # not a 4-digit year → fail-open
        return not (begin - 1 <= pub_year <= end + 1)

    # Publication Year absent — Era fallback (BUI-231: gate on per-issue year).
    era = str(aspects.get("Era", "")).lower()
    if "modern age" in era:
        # Parse per-issue release year if provided.
        iy: "int | None" = None
        if release_year is not None:
            try:
                parsed = int(str(release_year).strip())
                if len(str(parsed)) == 4:
                    iy = parsed
            except (ValueError, TypeError):
                pass
        if iy is not None:
            # Issue year known: reject only when it predates Modern Age.
            return iy < 1992
        else:
            # No per-issue year: fall back to series-end (pre-BUI-231 behavior).
            return end < 1992
    return False


# ─── Reprint / non-original-format lexicon (BUI-227) ────────────────────────
# Unambiguous markers that indicate a reprint, collected edition, or otherwise
# non-original single-issue format.  Conservative: only terms that would NEVER
# appear in a genuine first-print single-issue listing.  Do NOT add "variant"
# or other terms that legitimately appear on original copies.

_REPRINT_MARKERS: frozenset[str] = frozenset({
    "facsimile",
    "true believers",
    "marvel tales",
    "epic collection",
    "omnibus",
    "trade paperback",
    "tpb",
    "2nd printing",
    "second printing",
})


def _reprint_reject(title: str) -> bool:
    """Return True if the listing title contains a non-original-format marker.

    Case-insensitive; each marker is matched as a whole word or phrase (not a
    substring of another word) using look-around assertions.
    """
    t = (title or "").lower()
    for marker in _REPRINT_MARKERS:
        if re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", t):
            return True
    return False


# ─── Digital-code / no-physical-comic lexicon (BUI-230) ──────────────────────
# Markers that indicate the listing is a DIGITAL REDEEM CODE or otherwise has no
# physical comic — never what a raw-comic buyer wants.  Conservative: only
# phrases that mean "this IS just a code / there is no physical book".  Do NOT
# add a bare "digital code": many genuine physical modern comics are sold "with
# digital code" as a bonus, and rejecting those would be a false negative.
_DIGITAL_MARKERS: frozenset[str] = frozenset({
    "no physical",
    "code only",
    "digital only",
    "digital copy only",
})


def _digital_reject(title: str) -> bool:
    """Return True if the listing is a digital code / no-physical-comic offer.

    Case-insensitive whole-word/phrase match (same mechanism as _reprint_reject).
    Conservative by design — see _DIGITAL_MARKERS: a title that merely says it
    "includes/with digital code" alongside a physical book is NOT rejected.
    """
    t = (title or "").lower()
    for marker in _DIGITAL_MARKERS:
        if re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", t):
            return True
    return False


# ─── Trading-card / TCG lexicon (BUI-232) ────────────────────────────────────
# Markers that categorically identify a trading card or TCG product rather than
# a comic book.  Conservative: only terms that would NEVER appear in a genuine
# comic listing.  "psa", "card", and "marvel" are intentionally excluded as too
# ambiguous.  "panini" is also excluded because Panini Comics / Marvel UK is a
# legitimate comic publisher, so "panini" alone is not unambiguous (BUI-221 F7).

_TRADING_CARD_MARKERS: frozenset[str] = frozenset({
    "fleer",
    "topps",
    "upper deck",
    "skybox",
    "mtg",
    "magic the gathering",
    "trading card",
    "trading cards",
})


def _trading_card_reject(title: str) -> bool:
    """Return True if the listing title is a trading card or TCG product.

    Case-insensitive whole-word/phrase match (same mechanism as _reprint_reject
    and _digital_reject).  Only categorically trading-card/TCG terms are in the
    marker set — ambiguous terms are excluded.
    """
    t = (title or "").lower()
    for marker in _TRADING_CARD_MARKERS:
        if re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", t):
            return True
    return False


# ─── Foreign-edition / foreign-market reprint lexicon (BUI-239) ──────────────
# Markers that categorically identify a foreign-language or foreign-market
# reprint rather than an original US edition — never what a collector buying
# original first-print copies wants.
#
# CONSERVATIVE DESIGN (BUI-239): bare nationality / language ADJECTIVES
# ("mexican", "spanish", "french", "german", "italian") are intentionally
# excluded from this deterministic set.  They carry a small but real risk of
# false-positives: "Spanish" can be a story-arc adjective, a character name or
# descriptor, or a variant-cover language label that does not indicate a foreign
# reprint.  Those bare-adjective cases are delegated to the Haiku layer (Layer 2)
# for semantic judgment.  Only HIGH-PRECISION, unambiguous publisher names and
# edition phrases are included here:
#   - "la prensa" / "novedades" — well-known Mexican reprint publishers
#     (BUI-239 repro titles all contain "la prensa")
#   - "ediciones" / "edicion" — Spanish-language "editions"/"edition" words;
#     when they appear in an English-language eBay title the context is
#     unambiguously foreign (e.g. "AMAZING SPIDER-MAN #10 EDICION mexican la prensa")
#   - "en español" — "in Spanish" with the ñ character (U+00F1).  The
#     (?<!\w)…(?!\w) boundary regex is Unicode-aware (Python 3 default), so ñ
#     is treated as a word character and boundaries are correct.
#   - "espanol" — ASCII variant without the tilde; common in eBay titles that
#     drop accent marks.  Both forms are included to catch either spelling.
#   - "spanish edition" / "foreign edition" — explicit English-language phrases
#     that unambiguously name the edition type.

_FOREIGN_EDITION_MARKERS: frozenset[str] = frozenset({
    "la prensa",       # Mexican reprint publisher (BUI-239 primary repro)
    "novedades",       # Mexican reprint publisher (Novedades Editores)
    "ediciones",       # Spanish "editions" — unambiguous in English eBay titles
    "edicion",         # Spanish "edition" — BUI-239 repro: "EDICION mexican la prensa"
    "en español",      # "in Spanish" with ñ (U+00F1); Unicode-boundary-safe
    "espanol",         # ASCII variant (no accent) — common in eBay titles
    "spanish edition", # explicit English phrase
    "foreign edition", # generic foreign-edition marker
})


def _foreign_edition_reject(title: str) -> bool:
    """Return True if the listing title indicates a foreign-language or
    foreign-market reprint (e.g. Mexican La Prensa, Spanish-language edition).

    Case-insensitive whole-word/phrase match (same mechanism as _reprint_reject,
    _digital_reject, and _trading_card_reject).  Conservative by design — see
    _FOREIGN_EDITION_MARKERS: bare nationality/language adjectives ("mexican",
    "spanish", "french", etc.) are intentionally excluded to avoid false
    positives on story-arc titles or character descriptors.  Those bare-adjective
    cases are handled by the Haiku semantic layer.  BUI-239.
    """
    t = (title or "").lower()
    for marker in _FOREIGN_EDITION_MARKERS:
        if re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", t):
            return True
    return False


# ─── Later-printing / non-first-print reject (BUI-244) ────────────────────────
# Rejects "Second Print", "2nd Printing", "3rd Print", "later printing",
# "reprint", "reprints", etc. — later pressings that carry only a fraction of
# the first-print value (e.g. Batman: Vengeance of Bane #1 first print = $150+;
# second print = $5-$20).
#
# Conservative keep-list — these must NOT be filtered:
#   - "Newsstand" / "Direct" — original print-run distribution variants,
#     identical content to the regular first print.
#   - "First Print" / "1st Print" — explicitly an original copy.
# Bare "printing" (no ordinal prefix) is left to Haiku to avoid false positives
# on condition descriptors.  Facsimile is already handled by _reprint_reject —
# leave it.  BUI-244.

_LATER_PRINTING_RE = re.compile(
    r"(?<!\w)(?:2nd|3rd|4th|5th|second|third|fourth|fifth|later)\s+print(?:ing|s)?(?!\w)"
    r"|(?<!\w)reprints?(?!\w)",
    re.IGNORECASE,
)


def _second_print_reject(title: str) -> bool:
    """Return True if the listing title indicates a later printing / non-first-press copy.

    Catches "Second Print", "2nd Printing", "3rd Print", "later printing",
    "reprint", "reprints" — all of which signal a later pressing with a fraction
    of the first-print value.

    Conservative keep-list (returns False for these — must NOT be filtered):
      - "Newsstand" / "Direct" — original print-run distribution variants.
      - "First Print" / "1st Print" — explicitly an original copy.
    Facsimile and "Second Printing" (full word) are also handled by
    _reprint_reject; both functions may fire on the same title — that is fine
    (belt-and-suspenders).  BUI-244.
    """
    return bool(_LATER_PRINTING_RE.search(title or ""))


# ─── Shared deterministic reject chain (BUI-245) ──────────────────────────────
# Single source of truth for the deterministic gate chain, consulted by both
# seller_scan.main() and wishlist_sellers.match_results_for_wish() so the two
# candidate loops can never drift apart on what counts as an obvious non-match.


def should_reject(
    title: str,
    series: str,
    issue: "str | None",
    series_name: "str | None" = None,
    release_year: "str | None" = None,
) -> bool:
    """Return True if *title* is a deterministic non-match for this wish item.

    Runs the full gate chain in order — the first gate to fire wins:
      1. hard_reject       — CGC slab, edition mismatch, multi-comic lot,
                              missing issue number.
      2. era_mismatch      — parenthesized year or explicit volume clearly
                              contradicts the wish series era (BUI-226/BUI-240).
      3. _reprint_reject   — facsimile / omnibus / TPB / etc. (BUI-227).
      4. _digital_reject   — digital-code / no-physical-comic offer (BUI-230).
      5. _trading_card_reject — trading card / TCG product (BUI-232).
      6. _foreign_edition_reject — foreign-language/-market reprint (BUI-239).
      7. _second_print_reject   — later printing / non-first-print (BUI-244).

    *series_name* and *release_year* are optional — pass them whenever the
    caller has a decorated LOCG series name / per-issue release year so the
    era-gate signals can fire; omitting them just fails those checks open.
    """
    if hard_reject(title, series, issue):
        return True
    if era_mismatch(title, series_name, release_year):
        return True
    if _reprint_reject(title):
        return True
    if _digital_reject(title):
        return True
    if _trading_card_reject(title):
        return True
    if _foreign_edition_reject(title):
        return True
    if _second_print_reject(title):
        return True
    return False


# ─── Claude verification ──────────────────────────────────────────────────────

def _load_dotenv(path):
    """Load key=value pairs from a .env file into os.environ (if not already set)."""
    import os
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip()
    except FileNotFoundError:
        pass


# Maximum candidates per Claude API call.  At ~30–50 output tokens/verdict,
# the 8 096-token cap limits a single call to ~150–270 candidates before
# silent truncation; 100 keeps a comfortable margin and caps cost per call.
_VERIFY_CHUNK_SIZE = 100


def verify_with_claude(matches):
    """Filter candidates to genuine matches using chunked Claude API calls.

    Candidates are processed in batches of at most _VERIFY_CHUNK_SIZE per call
    so that a large run never silently truncates the response.  Indices in each
    prompt are 1-based and local to the chunk so the correlation logic is
    identical to the single-call version.

    Fail-closed: if a chunk's response cannot be parsed OR the number of
    returned verdicts does not match the number of candidates sent, that chunk
    is REJECTED entirely (those candidates are dropped) and a warning is
    printed to stderr.  Better to miss a borderline listing than to surface
    false positives unattended.  This replaces the previous fail-open fallback
    that returned all candidates on a parse error.
    """
    if not matches:
        return []

    # BUI-241: try .env candidates in order until the key lands in the environment.
    # (1) file-relative path — works under --editable install (primary, Option A fix)
    # (2) $COMIC_PIPELINE_ENV — an explicit full path to a .env file, if set
    # (3) stable user location — ~/.config/comic-pipeline/.env (Option B fallback)
    _dotenv_candidates = [
        Path(__file__).parent.parent / ".env",
        *(
            [Path(os.environ["COMIC_PIPELINE_ENV"])]
            if os.environ.get("COMIC_PIPELINE_ENV")
            else []
        ),
        Path.home() / ".config" / "comic-pipeline" / ".env",
    ]
    for _candidate in _dotenv_candidates:
        if os.environ.get("ANTHROPIC_API_KEY"):
            break
        _load_dotenv(_candidate)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _checked = ", ".join(str(p) for p in _dotenv_candidates)
        _env_hint = (
            " or set $COMIC_PIPELINE_ENV to a .env file path"
            if not os.environ.get("COMIC_PIPELINE_ENV")
            else ""
        )
        print(
            f"Error: ANTHROPIC_API_KEY is not set (checked the environment and "
            f"{_checked}). Claude verification cannot run — refusing to "
            f"surface unverified matches. Export ANTHROPIC_API_KEY, add it to "
            f"one of the files above{_env_hint}, then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    client = anthropic.Anthropic()

    kept = []
    for chunk_start in range(0, len(matches), _VERIFY_CHUNK_SIZE):
        chunk = matches[chunk_start : chunk_start + _VERIFY_CHUNK_SIZE]
        chunk_label = f"{chunk_start + 1}–{chunk_start + len(chunk)}"

        # Build pairs text.  When the candidate carries a decorated series name
        # (_series_name, stripped from output by _emit), include it as a
        # "Correct series:" hint so Haiku knows the exact era the user wants.
        pairs_parts = []
        for idx, cand in enumerate(chunk, 1):
            pair_text = (
                f'{idx}. Listing: "{cand["title"]}"\n'
                f'   Wish item: "{cand["wish_name"]}"'
            )
            sn = cand.get("_series_name")
            if sn:
                pair_text += f"\n   Correct series: {sn}"
            pairs_parts.append(pair_text)
        pairs = "\n".join(pairs_parts)
        prompt = f"""You are a comic book expert. For each listing/wish-item pair, decide if the listing is a genuine match — same series, same issue number, same edition type.

Reject if:
- Different series sharing words (Spider-Man Noir vs Amazing Spider-Man, X-Factor vs X-Men, Superior/Ultimate Spider-Man vs Amazing Spider-Man)
- Annual, Giant-Size, or special edition matching a regular series issue (and vice versa)
- Lot listing where the issue number appears in the lot size
- Promotional reprint (Trick or Read, LCSD, Amazon promo, Undeluxe)
- Modern renumbered issue matching an original issue number (e.g. #10 (811))
- Series name only in a subtitle or story description, not the actual series
- Different series VOLUME / relaunch: if the 'Correct series' line shows a specific era, reject listings where 'vol N' or a (YYYY) indicates a different era than the one shown
- Foreign-language or foreign-market reprint/edition (e.g. Mexican La Prensa, Spanish/French/German/Italian edition) when the wish item is the original US edition
- Numbered sequential run or complete multi-issue set (e.g. "Books 1-4", "Issues 1 through 6", "complete set", "full run") when the wish item is a single specific issue
- Later printing / reprint of a key issue (e.g. "2nd print", "Second Printing", "Reprint") when the wish item means the original first print. Newsstand and Direct editions are NOT reprints — keep those

Respond with a JSON array containing ONLY the ids you are REJECTING, each with a brief reason:
[{{"id": 3, "reason": "X-Factor not X-Men"}}, {{"id": 7, "reason": "annual vs regular"}}]

If nothing is rejected, return [].

Any candidate id NOT present in your response is treated as genuine.

Pairs:
{pairs}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        json_match = re.search(r"\[.*\]", text, re.DOTALL)
        if not json_match:
            print(
                f"Warning: could not parse Claude response for candidates "
                f"{chunk_label}; dropping chunk (fail-closed)",
                file=sys.stderr,
            )
            continue

        try:
            rejected_list = json.loads(json_match.group())
        except json.JSONDecodeError:
            print(
                f"Warning: invalid JSON in Claude response for candidates "
                f"{chunk_label}; dropping chunk (fail-closed)",
                file=sys.stderr,
            )
            continue

        # Validate: every returned object must have an int id strictly within
        # the chunk's 1-based range.  A missing key, non-int id, or out-of-range
        # id indicates a malformed response → reject the whole chunk (fail-closed).
        valid_ids = set(range(1, len(chunk) + 1))
        try:
            rejected_ids: dict[int, str] = {}
            for v in rejected_list:
                rid = v["id"]  # KeyError if missing
                if not isinstance(rid, int):
                    raise ValueError(f"non-int id: {rid!r}")
                if rid not in valid_ids:
                    raise ValueError(f"id {rid} out of range 1..{len(chunk)}")
                rejected_ids[rid] = v.get("reason", "")
        except (KeyError, ValueError, TypeError) as exc:
            print(
                f"Warning: invalid rejected-ids in Claude response for candidates "
                f"{chunk_label} ({exc}); dropping chunk (fail-closed)",
                file=sys.stderr,
            )
            continue

        # BUI-149: surface each rejected candidate (with the model's reason) to
        # stderr so the user can override if the verifier was wrong.
        if rejected_ids:
            print(
                f"Filtered {len(rejected_ids)} likely false positive(s) "
                f"(Claude verification):",
                file=sys.stderr,
            )
            for idx, cand in enumerate(chunk, 1):
                if idx in rejected_ids:
                    reason = rejected_ids[idx]
                    line = f"  - {cand.get('title', '?')}  ↮  {cand.get('wish_name', '?')}"
                    if reason:
                        line += f"  — {reason}"
                    print(line, file=sys.stderr)

        kept.extend(
            cand for idx, cand in enumerate(chunk, 1) if idx not in rejected_ids
        )

    return kept


# ─── Output ───────────────────────────────────────────────────────────────────

def _trunc(text, width):
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def print_matches(matches):
    """Print match results as a human-readable table."""
    if not matches:
        print("No matches found.")
        return

    cols = [
        ("Listing Title", "title", 40),
        ("Wish List Item", "wish_name", 28),
        ("Price", "current_price", 9),
        ("Ends", "end_date", 12),
        ("URL", "listing_url", 45),
    ]

    header = "  ".join(h.ljust(w) for h, _, w in cols)
    print(header)
    print("-" * len(header))

    for m in matches:
        row = "  ".join(
            _trunc(str(m.get(k) or ""), w).ljust(w)
            for _, k, w in cols
        )
        print(row)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="seller-scan",
        description="Match an eBay seller's listings against your LOCG wish list.",
    )
    parser.add_argument(
        "seller",
        help="eBay store name (resolved via your alias map), a /usr/ or _ssn= URL, "
             "or use --username for a raw login username",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="eBay login username to scan directly, bypassing the alias map "
             "(for a one-off seller not yet in your aliases)",
    )
    parser.add_argument(
        "--add-alias",
        default=None,
        metavar="USERNAME",
        help="Register the given login USERNAME for the store name in the "
             "positional arg, persist it, then scan",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output matches as JSON array",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show every wish-list match, including ones surfaced in a prior "
             "scan. By default (BUI-113) already-seen matches are hidden. "
             "Newly-surfaced matches are recorded as seen either way.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1000,
        help="Maximum listings to fetch from seller (default: 1000)",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )
    args = parser.parse_args(argv)

    # Resolve the store name to an eBay login username before anything else —
    # a store name is NOT a username and would silently scan every seller (BUI-68).
    if args.add_alias:
        save_seller_alias(args.seller, args.add_alias)
        print(
            f"Registered alias '{args.seller.strip().lower()}' → '{args.add_alias.strip()}'",
            file=sys.stderr,
        )
    aliases = load_seller_aliases()
    try:
        username = resolve_seller_username(
            args.seller, aliases, username_override=args.username
        )
    except UnknownSellerError as e:
        print(
            f"Error: unknown seller '{e.store}'. A store name is not an eBay "
            "login username, so the scan can't run safely.\n"
            "  Find the username: open one of the seller's listings, click "
            "'See other items', and copy the _ssn= value from the URL.\n"
            f"  Then either:\n"
            f"    seller-scan {e.store} --add-alias <username>   (saves it for next time)\n"
            f"    seller-scan {e.store} --username <username>     (one-off)",
            file=sys.stderr,
        )
        return 2

    # Auth
    client_id, client_secret, base_url = load_config()
    if args.env:
        from ebay_fetch import PRODUCTION_BASE, SANDBOX_BASE
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
    token = get_token(client_id, client_secret, base_url)

    # Fetch wish list
    print("Fetching LOCG wish list...", file=sys.stderr)
    wish_list = fetch_wish_list()
    wish_items = prepare_wish_items(wish_list)
    print(f"  {len(wish_items)} matchable wish list items", file=sys.stderr)

    # Fetch seller listings
    print(f"Fetching listings for seller '{username}'...", file=sys.stderr)
    raw_listings = search_seller_listings(
        username, token, base_url, max_results=args.max_results
    )
    print(f"  {len(raw_listings)} listings fetched", file=sys.stderr)

    # Match
    seen_ids = set()
    candidates = []
    for raw in raw_listings:
        listing = parse_item_summary(raw)
        # BUI-184: defense in depth — parse_item_summary already coerces null
        # titles to "" via `or ""`, but skip explicitly here too so that a
        # title-less listing never reaches .lower() or match_listing at all.
        title = listing.get("title")
        if not title:
            continue
        if "cgc" in title.lower():
            continue
        if listing["item_id"] in seen_ids:
            continue
        seen_ids.add(listing["item_id"])
        wish, score = match_listing(title, wish_items)
        if not wish:
            continue
        # BUI-245: run the same deterministic reject chain wishlist_sellers uses
        # (hard_reject, era_mismatch, reprint/digital/trading-card/foreign-edition/
        # second-print) against the wish item match_listing resolved, so a
        # cross-series false positive (e.g. "Spectacular Spider-Man #15" vs wish
        # "The Amazing Spider-Man #15") is dropped here rather than reaching Haiku
        # with no era hint.
        if should_reject(
            title, wish["series"], wish["issue"],
            wish.get("_series_name"), wish.get("_release_year"),
        ):
            continue
        candidates.append({
            **listing,
            "wish_id": wish["id"],
            "wish_name": wish["name"],
            "match_score": round(score, 2),
            # Private fields (BUI-245): carry the decorated series name so
            # verify_with_claude's "Correct series:" era hint activates for this
            # candidate too. Stripped before output — see the json_output block.
            "_series_name": wish.get("_series_name"),
            "_release_year": wish.get("_release_year"),
        })

    # BUI-113: drop matches already surfaced in a prior scan (default). --all
    # skips the filter (and its server fetch); the short-circuit on an empty
    # candidate list skips the fetch/Claude/record entirely.
    if candidates and not args.show_all:
        seen = fetch_seen_item_ids(username)
        before = len(candidates)
        candidates = [c for c in candidates if c["item_id"] not in seen]
        hidden = before - len(candidates)
        if hidden:
            print(
                f"  {hidden} already-seen match(es) hidden (use --all to show)",
                file=sys.stderr,
            )

    if candidates:
        print(f"  {len(candidates)} candidate(s) — verifying with Claude...", file=sys.stderr)
        matches = verify_with_claude(candidates)
    else:
        matches = []
    print(f"  {len(matches)} genuine match(es) found", file=sys.stderr)

    # BUI-245: strip private pipeline fields (_series_name, _release_year) —
    # they exist only to feed verify_with_claude's era hint and were never
    # meant to reach the user-facing table/JSON output.
    matches = [{k: v for k, v in m.items() if not k.startswith("_")} for m in matches]

    if args.json_output:
        print(json.dumps(matches, indent=2))
    else:
        print_matches(matches)

    # BUI-113: record surfaced matches (best-effort) so future scans skip them.
    # Runs under --all too — --all means "show me everything again", not "forget".
    if matches:
        record_items_seen([m["item_id"] for m in matches], username)

    return 0


if __name__ == "__main__":
    sys.exit(main())
