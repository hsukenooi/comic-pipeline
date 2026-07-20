#!/usr/bin/env python3
"""comic_identity: deterministic freeform-title → comic-identity logic.

BUI-253 Step 1 consolidated the title-parsing / deterministic reject helpers
that used to live in seller_scan.py (grown across BUI-135, BUI-221,
BUI-226..245, BUI-261) so seller_scan.py, wishlist_sellers.py, and
(eventually) a standalone comic-identify CLI + the LLM skills all share ONE
implementation instead of re-deriving these rules. Every symbol from Step 1
is unchanged from its seller_scan.py original and is re-exported from
seller_scan.py so no caller had to change.

BUI-253 Step 2 adds the genuinely new title→identity logic on top of those
helpers: identify_comic() (freeform title → ComicIdentity — series, issue,
year, volume, edition, lot detection + expansion), and score_against_wish()
(the fuzzy-match scorer extracted out of seller_scan.match_listing so the
scoring math lives alongside the identity it scores against). See the
docstrings on identify_comic and ComicIdentity for the extraction rules and
confidence scale.

Co-located with its two code consumers (seller_scan.py, wishlist_sellers.py)
in apps/ebay/src — NOT a packages/ workspace package, since the overlay never
parses freeform eBay titles and there is no cross-repo consumer today.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# BUI-327: paren-year / plausible-year / edition-classification helpers moved
# out to comic_identity_year.py (this file had grown to ~1560 lines after
# BUI-316) — re-exported here (same convention BUI-253 Step 1 used for the
# seller_scan.py -> comic_identity.py move) so no existing caller had to
# change. See comic_identity_year.py's module docstring for the cluster
# boundaries and why _classify_edition_kind's back-reference into this module
# is a deferred (call-time) import rather than a top-level one.
from comic_identity_year import (  # noqa: F401 — re-exported for callers
    _all_title_years,
    _ANNUAL_RE,
    _BARE_YEAR_RE,
    _classify_edition_kind,
    _coerce_publication_year,
    _COLLECTED_EDITION_MARKERS,
    _FACSIMILE_MARKERS,
    _GIANT_SIZE_RE,
    _is_plausible_year,
    _KING_SIZE_RE,
    _MAX_PLAUSIBLE_YEAR,
    _MIN_PLAUSIBLE_YEAR,
    _PROMO_REPRINT_MARKERS,
    _title_paren_years,
    _TREASURY_RE,
    confident_cover_year,
)

# ─── Text normalization ────────────────────────────────────────────────────

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "in", "vol", "comics"})


def _normalize(text):
    """Lowercase and strip non-alphanumeric characters."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


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


def _series_tokens(series):
    """Return significant tokens from a series name."""
    return [t for t in _normalize(series).split() if len(t) >= 2 and t not in _STOPWORDS]


# ─── Edition patterns ──────────────────────────────────────────────────────
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

# BUI-261: shared building block for the generic numeric-list branches below.
# Matches one lot "member": an optional '#' + a 1-3 digit run, bounded so it
# can never bind to part of a longer run (a 4-digit YEAR, or the "6" in a SKU
# token like "X6" — \b requires a non-word char on both sides, and a letter
# immediately touching a digit is NOT a boundary) and never bind to half of a
# decimal grade ("9.4/9.6" — the lookaround pair excludes any digit run that
# is directly preceded or followed by a '.').  This is what makes the generic
# separator-chain branches below safe to add without regressing the BUI-243
# guards (year-span carve-out, SKU tokens) or false-firing on grade ratios.
_LOT_MEMBER = r"#?\s*(?<!\.)\b\d{1,3}\b(?!\.\d)"

# Multi-comic-lot signals.  Any match → listing is a bundle, not a single issue.
_LOT_RE = re.compile(
    r"\blot\s+of\b"           # "lot of"
    r"|\b\d+\s+lot\b"         # "5 lot", "10-comic lot"
    r"|\blot\s+\d+"           # "lot 5", "lot 10"
    # BUI-261: bare "lot" catch-all. Comics titles essentially never use "lot"
    # as anything but a bundle signal, so this alone subsumes the three
    # narrower branches above — they're kept for documentation/history and
    # because their tests assert the exact substrings.
    r"|\blot\b"
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
    r"|\b(?:books?|issues?)\s+\d{1,3}\s+through\s+\d{1,3}\b"
    # BUI-261: bare "#N through M" / "N through M" — the branch above requires
    # a literal "issues"/"books" word; real titles often drop it entirely
    # (e.g. "The Eternals #1 through 10").
    r"|#\d{1,3}\s+through\s+\d{1,3}\b"
    r"|\b\d{1,3}\s+through\s+\d{1,3}\b"
    # BUI-261: generic numeric-list detector — 3+ members chained by any mix
    # of comma/slash/ampersand/dash ("164/165/166/168", "33,45,50,53,63,81,86",
    # "92-93-94-95-96", "#64, #65 & #66"). Requiring 2+ separators (3+ members)
    # for a comma/dash-only chain preserves the BUI-221/BUI-243 carve-out for a
    # bare 2-member dash pair ("129-150", "1962-1963") — those stay ambiguous
    # (year span / price span) and are intentionally left alone.
    rf"|{_LOT_MEMBER}(?:\s*[,/&-]\s*{_LOT_MEMBER}){{2,}}"
    # BUI-261 (PR review fix): a 2-member AMPERSAND pair is unambiguous on its
    # own — "&" is never used to write a single number or a year/price span
    # ("Dark Knight Returns # 1 & 3"). "/" is deliberately EXCLUDED from this
    # 2-member branch (unlike the 3+ member branch above): "/" is overloaded
    # in real listings with ratio/incentive variants and half-issues — "ASM
    # #300 1/100 variant", "Batman #92 1/25 variant", "Batman #1/2" (a Wizard
    # half-issue) are all genuine SINGLE-issue titles, not lots, and hard_reject
    # must never drop a genuine single-issue copy. A genuine 2-member slash
    # LOT ("STRANGE TALES 164/165") falls through to Haiku instead — the safe
    # under-reject direction; a 3+ member slash chain ("164/165/166") is still
    # caught by the branch above.
    rf"|{_LOT_MEMBER}\s*&\s*{_LOT_MEMBER}",
    re.IGNORECASE,
)

# ─── Lot count vs. parsed-range mismatch (BUI-261) ───────────────────────────

_LOT_STATED_COUNT_RE = re.compile(r"\blot\s+of\s+(\d{1,3})\b", re.IGNORECASE)
_LOT_ISSUE_RANGE_RE = re.compile(r"#\s*(\d{1,3})\s*-\s*#?\s*(\d{1,3})\b")


def lot_count_mismatch(title: str) -> bool:
    """Return True if a stated "Lot of N" count contradicts the size of an
    explicit "#start-end" issue range also present in the title.

    Example: "Lot of 11 Comics ... #1-10" claims 11 books over a range that
    only spans 10 issues — a parsing red flag worth surfacing rather than
    silently trusting either number.

    FAIL-OPEN (BUI-261): this is a diagnostic flag for a caller to act on
    (e.g. log a warning, or double-check before expanding a lot into
    constituent issues) — not a rejection gate; _LOT_RE already hard-rejects
    anything phrased as "lot of N" regardless of what this returns. Returns
    False whenever the stated count or an explicit range is missing, or the
    range is malformed (end < start) — ambiguous input is never reported as
    a mismatch.
    """
    stated_m = _LOT_STATED_COUNT_RE.search(title or "")
    if not stated_m:
        return False
    stated = int(stated_m.group(1))

    range_m = _LOT_ISSUE_RANGE_RE.search(title)
    if not range_m:
        return False
    start, end = int(range_m.group(1)), int(range_m.group(2))
    if end < start:
        return False
    return (end - start + 1) != stated


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


# BUI-327: the plausible cover-year window (_is_plausible_year and its two
# bounds) and _title_paren_years now live in comic_identity_year.py (imported
# and re-exported above) — see that module's docstring for the full cluster
# this belongs to.


def _title_volume(title: str) -> "int | None":
    """Extract an explicit volume number from a listing title, or None.

    Matches "vol N", "vol. N", "volume N" (case-insensitive, word-boundary).
    Parenthesized years are handled separately by _title_paren_years.

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
    "retold",  # BUI-253 PR-review fix (S1): sold_comps.py's manual-fallback
    # lexicon already flagged "retold" as a reprint marker; comic_identity's
    # lexicon was missing it entirely.
})


def _reprint_reject(title: str) -> bool:
    """Return True if the listing title contains a non-original-format marker.

    Case-insensitive; each marker is matched as a whole word or phrase (not a
    substring of another word) using look-around assertions.
    """
    return _marker_hit(title, _REPRINT_MARKERS)


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
    return _marker_hit(title, _DIGITAL_MARKERS)


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
    # BUI-253 PR-review fix (S1): these four brand names were in
    # sold_comps.py's manual-fallback lexicon but never made it into this
    # deterministic lexicon — a real coverage gap on the primary path.
    "donruss",
    "impel",
    "keepsake",
    "signagraph",
})


def _trading_card_reject(title: str) -> bool:
    """Return True if the listing title is a trading card or TCG product.

    Case-insensitive whole-word/phrase match (same mechanism as _reprint_reject
    and _digital_reject).  Only categorically trading-card/TCG terms are in the
    marker set — ambiguous terms are excluded.
    """
    return _marker_hit(title, _TRADING_CARD_MARKERS)


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
    return _marker_hit(title, _FOREIGN_EDITION_MARKERS)


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


# ─── Distribution-variant extraction (BUI-295) ───────────────────────────────
# Surface the original-print *distribution* variant named in a listing title as
# a short canonical label, so callers (comic-identify -> /comic:collection-add's
# identify_data.variant_text) don't re-derive it with ad-hoc regex each run.
# These are the same variants the _second_print_reject keep-list protects
# (Newsstand / Direct — identical content, different market channel), plus
# Whitman (the bagged direct-market variant). They are NOT reprints — those are
# rejected upstream. First match wins in listed order; "" when none is present,
# matching the identify_data.variant_text "omit or empty" contract.
#
# Newsstand: allow "newsstand", "news stand", "news-stand".
# Direct: require an explicit qualifier (edition/market/sales). Bare "direct" is
# common listing filler ("ships direct from estate", "buy direct") and a false
# "Direct Edition" label corrupts the recorded collection — precision over
# recall, so an unqualified "Direct" stays "". "director"/"directed" can't match
# either way (a whitespace-bounded "direct" must be followed by the qualifier).
_VARIANT_PATTERNS = [
    ("Newsstand", re.compile(r"(?<!\w)news[\s-]?stand(?!\w)", re.IGNORECASE)),
    (
        "Direct Edition",
        re.compile(r"(?<!\w)direct\s+(?:edition|market|sales)(?!\w)", re.IGNORECASE),
    ),
    ("Whitman", re.compile(r"(?<!\w)whitman(?!\w)", re.IGNORECASE)),
]


def extract_variant_text(title: str) -> str:
    """Return a short canonical distribution-variant label for a listing title.

    Detects the original-print distribution variants collectors distinguish
    (Newsstand / Direct Edition / Whitman) and returns "" when none is present.
    First match wins in listed order. Case-insensitive. "Direct" requires an
    edition/market/sales qualifier so bare filler ("ships direct") does not
    false-match; this is a precision-first extractor, not an exhaustive one —
    non-distribution variants (price/cover/sketch) are left to the caller.
    """
    for label, pat in _VARIANT_PATTERNS:
        if pat.search(title or ""):
            return label
    return ""


# ─── hard_reject / should_reject (BUI-221/245) ───────────────────────────────


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
        # BUI-261 (PR review fix): surface a stated-count vs parsed-range
        # contradiction instead of silently trusting either number — e.g.
        # "Lot of 11 Comics ... #1-10" claims 11 books over a 10-issue range.
        # hard_reject is the single choke point both should_reject call sites
        # (seller_scan.main() and wishlist_sellers.match_results_for_wish)
        # already run through, so this is the cheapest place to flag it live.
        if lot_count_mismatch(title):
            print(
                f"Warning: lot count/range mismatch in title: {title!r}",
                file=sys.stderr,
            )
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


# ─── FMV comp-exclusion (BUI-269) ────────────────────────────────────────────
# sold_comps.py (apps/ebay/src/sold_comps.py, run automatically by comic-fmv
# via ebay-sold-comps) carried a THIRD, independently hand-maintained copy of
# the lot/reprint/foreign-edition/trading-card exclude logic above, and it had
# drifted: sold_comps knew about UK/pence/Norway/Australia/Italian/Spain
# market markers that never made it into this module, and vice versa. BUI-269
# reconciles the two so comic_identity is the single source of truth.
#
# The bare geographic/market markers below are deliberately kept OUT of
# _FOREIGN_EDITION_MARKERS above rather than merged into it: that lexicon
# feeds hard_reject/should_reject, which gate a real purchase decision
# (wishlist-sellers, identify_comic) and are intentionally conservative about
# bare nationality words (BUI-239) — a false reject there costs a missed
# purchase. is_comp_excluded() below only decides whether to drop ONE
# comparable listing from an FMV price average; a false exclude there just
# shrinks the comp pool slightly, a much cheaper mistake. So the two paths
# share the same reprint/trading-card/foreign-edition base but the
# comp-exclusion path additionally unions in these higher-risk markers.
_FMV_FOREIGN_MARKET_MARKERS: frozenset[str] = frozenset({
    "uk",
    "pence",
    "9d variant",
    "rare brazil",
    "rare mexico",
    "norway",
    "australia",
    "italian",
    "spain",
    "ebal",
})

# Non-comic collectibles sold_comps also excluded alongside its trading-card
# markers (action figures, die-cast-scale toys) — precise phrases with no
# bare-adjective false-positive risk, so no split-lexicon caveat needed here.
#
# "johnny lightning" (a die-cast toy brand) lives here, NOT in
# _TRADING_CARD_MARKERS: that trading-card set feeds should_reject/hard_reject
# (the purchase-decision path), and BUI-269's scope is comp-pool exclusion, so
# a comp-only marker belongs in this comp-only set — not a widening of the
# conservative purchase-reject lexicon.
_FMV_COLLECTIBLE_MARKERS: frozenset[str] = frozenset({
    "action figure",
    "1:6 scale",
    "collectible figure",
    "johnny lightning",
})

# Multi-issue lot shapes that the pre-BUI-269 sold_comps HARD_EXCLUDE_RE caught
# but the shared _LOT_RE (purchase-decision path) does NOT: a SPACE-separated
# hash-issue list ("#1 #2", "#1 #2 #3") and a 2-MEMBER COMMA pair ("#64, #65").
# _LOT_RE's comma branch needs 3+ members and it has no space separator, so
# without this a 2-3 book lot would leak into the FMV comp pool and inflate the
# average (a false-INCLUDE — the expensive direction). Kept comp-only (checked
# by is_comp_excluded, never merged into _LOT_RE) so it can't make the
# conservative purchase path reject more (BUI-239). The comma member is bounded
# to 1-3 digits so "#1, 2018" (a hash then a YEAR) is not mistaken for a lot.
_FMV_LOT_RE = re.compile(
    r"""
    \#\d+\s+\#\d+                    # space-separated hash issues: "#1 #2" (any length)
    | \#\d{1,4}\s*,\s*\#?\d{1,3}(?!\.\d)\b   # 2-member comma pair: "#64, #65" / "#64, 65"
                                             # (?!\.\d): don't match a decimal grade
                                             # after a comma, e.g. "#300, 9.8 CGC"
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_comp_excluded(title: str) -> bool:
    """Return True if *title* should be excluded as an eBay-sold-listing FMV
    comparable (BUI-269 — the single source of truth for
    sold_comps.hard_exclude()'s lot/reprint/foreign-edition/trading-card
    checks).

    Unlike should_reject/hard_reject, there is no candidate wish (series,
    issue) to compare against here — sold_comps only has a bare sold-listing
    title — so this reuses the WISH-INDEPENDENT reject helpers (lot
    detection, reprint/trading-card/foreign-edition lexicons) plus the
    comp-exclusion-only market/collectible markers above. It does NOT run
    hard_reject's issue-number-missing or edition-vs-series checks (both
    require a wish item) or era_mismatch/_digital_reject (sold_comps has its
    own separate condition/grading excludes for concerns those don't cover).
    """
    if _LOT_RE.search(title or ""):
        return True
    if _FMV_LOT_RE.search(title or ""):  # BUI-269: comp-only lot shapes _LOT_RE misses
        return True
    if _reprint_reject(title):
        return True
    if _trading_card_reject(title):
        return True
    if _foreign_edition_reject(title):
        return True
    if _marker_hit(title, _FMV_FOREIGN_MARKET_MARKERS):
        return True
    if _marker_hit(title, _FMV_COLLECTIBLE_MARKERS):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# BUI-253 Step 2: standalone freeform-title → comic-identity extraction.
#
# Everything above this line is Step 1 (moved verbatim from seller_scan.py).
# Everything below is genuinely new: nothing in this codebase previously
# turned a bare eBay title into a structured identity without a KNOWN wish
# item to score it against — match_listing/hard_reject/should_reject all
# require a candidate wish (series, issue) to compare a title to. identify_comic
# is the reverse direction: title alone in, best-effort structured guess out.
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ComicIdentity:
    """Best-effort structured identity extracted from a freeform eBay title.

    Fields:
      title               — the original, unmodified input title. Kept on the
                             object so score_against_wish() can score this
                             identity against a wish item without needing the
                             raw string passed around separately.
      series              — extracted series name (literal substring of the
                             title, NOT normalized against any canonical
                             series list — see "X-Men vs Uncanny X-Men" below).
                             None if extraction failed entirely.
      issue               — the single extracted issue number as a string
                             (matches the "#N"/"N" convention used throughout
                             this module), or None for a lot (see is_lot) or
                             an unparseable title.
      year                — best-guess 4-digit publication year, or None.
                             Parenthesized years are preferred (high
                             precision); a bare/embedded year is used only
                             when no parenthesized one is present.
      volume              — explicit "Vol N"/"Volume N" number from the
                             title, or None.
      edition             — one of "single-issue" (default), "annual",
                             "giant-size", "king-size", "treasury",
                             "collected" (HC/TPB/Omnibus/Epic Collection —a
                             single purchasable SKU, not a multi-issue lot),
                             "facsimile", or "reprint" (later
                             printing / promotional reprint). See
                             _classify_edition_kind for the priority order.
      is_lot              — True if the title matches the deterministic lot
                             signals in _LOT_RE (BUI-243/BUI-261).
      constituent_issues  — for a lot, the best-effort list of individual
                             issue numbers the lot appears to bundle (see
                             _expand_lot_issues). [] means "known bundle,
                             contents not parseable" — NOT "not a lot".
      reject_reasons      — human-readable notes on why a caller might not
                             want this listing as a genuine single raw issue
                             (CGC slab, lot, digital-only, trading card,
                             foreign edition, collected edition, facsimile,
                             later printing, lot count/range mismatch,
                             ambiguous year, or failed extraction). Additive,
                             never mutually exclusive with a populated series
                             /issue — e.g. a facsimile still gets a normal
                             series/issue extraction AND a reject_reasons note.
      confidence          — float in [0.0, 1.0], confidence in the EXTRACTED
                             FIELDS being correct (not a judgment about
                             whether the listing is "wanted" — that's what
                             reject_reasons is for). Tiers, highest to
                             lowest:
                               1.0  — explicit "#N" issue + non-empty series
                                      + at most one distinct year mention.
                               0.8  — same as 1.0 but multiple DISTINCT year
                                      mentions in the title (ambiguous era —
                                      e.g. a "(1962-1963)" run-year span).
                               0.6  — a "collected" edition with no issue
                                      number — expected/correct for a
                                      HC/TPB/Omnibus, not a parse failure.
                               0.5  — a lot whose constituent_issues WERE
                                      parsed; or a single issue whose number
                                      was only found via the bare-digit
                                      fallback (no explicit "#").
                               0.35 — a lot whose constituent_issues were
                                      parsed BUT also flagged with a
                                      count/range mismatch (contradictory
                                      signals in the same title).
                               0.3  — a lot whose constituent_issues could
                                      NOT be parsed at all; or a non-lot
                                      title with no resolvable issue number.
                               0.0  — empty/blank title.

    X-Men vs Uncanny X-Men: identify_comic never canonicalizes or reduces the
    extracted series text — "X-Men #142" and "Uncanny X-Men #142" produce
    series="X-Men" and series="Uncanny X-Men" respectively, because these are
    two different LOCG series with different histories/volumes, not the same
    series with an ignorable adjective. Any future fuzzy series-matching layer
    must compare on the literal extracted text, not a "core" reduced form.
    """

    title: str
    series: "str | None" = None
    issue: "str | None" = None
    year: "int | None" = None
    volume: "int | None" = None
    edition: str = "single-issue"
    is_lot: bool = False
    constituent_issues: "list[str]" = field(default_factory=list)
    reject_reasons: "list[str]" = field(default_factory=list)
    confidence: float = 0.0
    # Internal cache — the grade-stripped + normalized title, computed once by
    # identify_comic() so score_against_wish() never has to recompute it per
    # wish item (match_listing scores ONE title against potentially hundreds
    # of wish items). Not part of the public field contract above.
    _title_norm: str = ""


# BUI-327: the edition-kind classification cluster (_ANNUAL_RE, _TREASURY_RE,
# _GIANT_SIZE_RE, _KING_SIZE_RE, _COLLECTED_EDITION_MARKERS,
# _FACSIMILE_MARKERS, _PROMO_REPRINT_MARKERS, _classify_edition_kind) now
# lives in comic_identity_year.py (imported and re-exported above).
# _classify_edition_kind calls back into _marker_hit (below) and
# _second_print_reject via a deferred import — see that module's docstring.


def _marker_hit(title: str, markers: "frozenset[str]") -> bool:
    """Whole-word/phrase, case-insensitive membership check (same mechanism
    as _reprint_reject et al.)."""
    t = (title or "").lower()
    return any(
        re.search(r"(?<!\w)" + re.escape(m) + r"(?!\w)", t) for m in markers
    )


# ─── Issue-number extraction ──────────────────────────────────────────────

# The primary, high-confidence issue signal: an explicit "#N" (optionally with
# a trailing letter/suffix, e.g. "#1A") — same shape as _parse_wish_name's
# issue group so the two conventions line up.
#
# BUI-253 PR-review nit (N1): also captures a decimal POINT-ISSUE ("#700.1",
# a real Marvel numbering convention for interstitial issues) instead of
# silently truncating to "700". Safe to add: the "#" anchor means this can
# never collide with a grade decimal ("VF 9.4") — no seller ever hash-prefixes
# a raw grade — and no existing test title contains a "#N.N" shape.
#
# Deliberately did NOT extend this to also capture a HALF-issue slash
# ("#1/2") — "Batman #1/2" is a locked-in BUI-261 PR-review test case
# (test_half_issue_slash_not_misread_as_lot) asserting issue=="1" (matching
# convention: a half-issue scores against the WHOLE-number wish item), and
# capturing "1/2" instead would break that existing, deliberate behavior.
_ISSUE_HASH_RE = re.compile(r"#\s*(\d+(?:\.\d+)?\w*)")

# A parenthetical group that is PURELY a year or year range — "(2022)",
# "(1963 - 1998)", "(2022 - Present)" — stripped out of the extracted series
# text (the year itself is reported separately via ComicIdentity.year/
# _all_title_years). Reuses _ERA_DASH_CLASS (the same dash-variant class
# series_year_range uses) so en/em-dash decorated ranges are handled too.
_PAREN_YEAR_ONLY_RE = re.compile(
    rf"\(\s*\d{{4}}\s*(?:{_ERA_DASH_CLASS}\s*(?:\d{{4}}|Present)\s*)?\)",
    re.IGNORECASE,
)

# A "Vol N"/"Volume N" mention must be masked out before the bare-digit
# fallback scans for a plausible issue number, or the volume number gets
# mistaken for the issue (e.g. "Amazing Spider-Man Vol 3 175" bare-issue
# fallback must find "175", not the "3" from "Vol 3").
_VOLUME_MASK_RE = re.compile(r"\bvol(?:ume)?\.?\s*\d+\b", re.IGNORECASE)

# BUI-460: BUI-456 broadened the separator _ANNUAL_EDITION_RE accepts between
# "annual" and its issue number to ":", "-", "#", and "No."/"No" (e.g. "X-Men
# Annual: 1", "Avengers Annual-#5", "Annual No. 1" all now correctly classify
# edition="annual"). But the old word-only strip (`_ANNUAL_RE.sub("", ...)`)
# only deleted the word "annual" itself, leaving that separator orphaned at
# the end of the series text — "X-Men Annual: 1" -> series "X-Men :" instead
# of "X-Men".
#
# This regex removes "annual" TOGETHER WITH the separator that immediately
# FOLLOWS it, reusing the identical separator character class
# _ANNUAL_EDITION_RE already validated (as a classification precondition)
# sits between "annual" and the issue digit. It is anchored to the WORD
# "annual" itself (\bannual\b) and only ever extends FORWARD from there to
# the end of the already issue-number-truncated string — never backward past
# "annual".
#
# That directionality is load-bearing, not cosmetic: an earlier draft of this
# fix instead stripped separator-looking characters from wherever the string
# happened to END, with no anchor to "annual" at all. That silently ate a
# real trailing word out of a series name whenever it happened to be "No" —
# e.g. "Just Say No Annual #1" -> "Just Say" — because after the word
# "annual" is deleted, "No" that legitimately PRECEDES "annual" in the title
# and "No." that is genuine separator residue AFTER "annual" (as in "Annual
# No. 1") become indistinguishable from the end of the string alone. Anchoring
# to "\bannual\b" and only matching forward removes that ambiguity: only text
# actually adjacent to (i.e. after) the word being deleted is ever touched.
_ANNUAL_WORD_AND_SEP_RE = re.compile(r"\bannual\b[\s#:.no-]*$", re.IGNORECASE)


def _bare_issue_match(stripped_title: str) -> "tuple[re.Match | None, str]":
    """Best-effort issue-number fallback when no explicit "#N" is present.

    Returns (match, masked_text) where match is the FIRST standalone 1-3
    digit token in the (grade-stripped, volume-masked) title, or (None,
    masked_text) if nothing plausible is found. A 4-digit token is never
    returned — the codebase convention throughout (_LOT_MEMBER, the BUI-243
    year-span guards) treats any 4-digit run as a year/noise signal, never a
    plausible issue number. This is a LOW-CONFIDENCE guess (see
    ComicIdentity.confidence); callers should never treat it with the same
    certainty as an explicit "#N" match.

    The masked text is also returned (not just the match) so the caller can
    slice out the series text preceding match.start() from the SAME string
    the match was found in — the volume-masking replaces "Vol N" with a
    single space, which shifts positions relative to the original title, so
    slicing the original stripped_title at match.start() would be wrong
    (e.g. "Amazing Spider-Man Vol 3 175" — the series text must exclude
    "Vol 3" entirely, not just stop before wherever "175" landed pre-mask).
    """
    masked = _VOLUME_MASK_RE.sub(" ", stripped_title)
    for m in re.finditer(r"\b\d{1,4}\b", masked):
        if len(m.group(0)) == 4:
            continue
        return m, masked
    return None, masked


# BUI-327: _BARE_YEAR_RE and _all_title_years now live in
# comic_identity_year.py (imported and re-exported above).


# ─── Lot expansion (BUI-261 formats → constituent issue numbers) ──────────

# Explicit RANGES — a start/end pair meant to be expanded INCLUSIVELY into
# every issue number in between. Order matters: whichever pattern matches
# EARLIEST in the title wins (see _expand_lot_issues / _lot_series_text).
_LOT_RANGE_PATTERNS = [
    # "#1-#10" / "#1-10" (hash-anchored — mirrors _LOT_RE's own range branch)
    re.compile(r"#\s*(\d{1,3})\s*-\s*#?\s*(\d{1,3})\b"),
    # "Books 1-4" / "issues 2-4" / "issue 1-6" (quantity-word-prefixed range)
    re.compile(r"\b(?:books?|issues?)\s*(\d{1,3})\s*-\s*#?\s*(\d{1,3})\b", re.IGNORECASE),
    # "#1 through 10" / "1 through 10" (spelled-out range, hash optional)
    re.compile(r"(?:#\s*)?(\d{1,3})\s+through\s+(\d{1,3})\b", re.IGNORECASE),
    # BUI-253 PR-review fix (B1, blocking): a BARE 2-member dash range with no
    # '#'/quantity-word/'through' anchor at all — "ASM 48-50 lot", "Amazing
    # Spider-Man 48-50 comic lot". Without this, such a title fell through to
    # _LOT_LIST_RE (which also accepts '-' as a separator, for the 3+-member
    # chain case) and got parsed as the literal LIST ["48","50"] — silently
    # DROPPING #49 (a real data-loss bug: collection-add records one entry
    # per constituent, so the missing issue never gets recorded as owned).
    #
    # Must be tried LAST (after the more specific anchored patterns above)
    # and must NEVER fire on a 3+-member dash CHAIN ("92-93-94-95-96") — that
    # is a literal LIST (a real title can skip numbers within a chain), not a
    # range, and naively range-computing any adjacent pair out of a longer
    # chain would silently invent numbers that were never in the title —
    # e.g. for "92-94-96", a naive 2-number match on "94-96" would wrongly
    # imply a "95". The lookbehind/lookahead pair below reject any pair that has
    # ANOTHER dash-number directly attached on either side, so only a truly
    # ISOLATED "A-B" pair (no third chain member touching it) can match —
    # _LOT_LIST_RE remains the sole path for anything longer. Reuses the same
    # decimal-grade guard _LOT_MEMBER uses (a CGC-style "9.4-9.6" grade span
    # must never be misread as an issue range) and the same \d{1,3} bound
    # (4-digit year spans like "1962-1963" can never match — same BUI-243
    # guard as everywhere else in this module).
    re.compile(
        r"(?<!\d-)(?<!\.)\b(\d{1,3})\b(?!\.\d)\s*-\s*"
        r"(?<!\.)\b(\d{1,3})\b(?!\.\d)(?!\s*-\s*#?\d)"
    ),
]

# Explicit LISTS — separator-delimited individual members that must NOT be
# range-expanded: a real title can skip numbers ("164/165/166/168" omits
# 167), so the literal listed members are returned as-is. Reuses _LOT_MEMBER
# (the same bounded digit-token building block _LOT_RE uses) so this can
# never misread a 4-digit year or a SKU-adjacent digit as a lot member.
_LOT_LIST_RE = re.compile(
    rf"{_LOT_MEMBER}(?:\s*[,/&-]\s*{_LOT_MEMBER})+", re.IGNORECASE
)
_LOT_LIST_MEMBER_RE = re.compile(r"\d{1,3}")

# Boilerplate that surrounds the real series name in a "Lot of N ..." title —
# stripped from the extracted series text wherever it appears (leading, e.g.
# "Lot of 11 Comics Amazing Spider-Man #1-10", OR mid-string, e.g. "Avengers
# Lot of 11 Comics #1-10") so either shape yields a clean series name instead
# of one contaminated with the lot-count phrase. NOT anchored to the start —
# a real series name is never going to coincidentally contain "lot of N
# comics", so matching anywhere in the (already-sliced) candidate text is
# safe.
_LOT_BOILERPLATE_RE = re.compile(
    r"\s*(?:huge\s+)?lot\s+of\s+\d+\s*(?:comics?|books?|issues?)?\s*[:\-]?\s*",
    re.IGNORECASE,
)


def _expand_lot_issues(title: str) -> "list[str]":
    """Best-effort parse of the individual issue numbers a lot title bundles.

    Tries, in order:
      1. An explicit range ("#1-#10", "Books 1-4", "#1 through 10") —
         expanded to every issue number in [start, end] inclusive. A sanity
         bound (span <= 500) guards against a garbage/inverted range.
      2. An explicit separator-delimited list ("164/165/166/168",
         "33,45,50,53,63,81,86", "#64, #65 & #66") — the literal listed
         members, NOT range-expanded (see _LOT_LIST_RE docstring above).

    Returns [] when nothing parseable is found (is_lot may still be True —
    the title matched _LOT_RE on a signal word like "lot"/"collection" with
    no extractable numbers, e.g. "Huge Spider-Man Comic Lot!!"). Callers must
    treat an empty list as "known bundle, unknown contents", never as "not a
    lot".
    """
    for pat in _LOT_RANGE_PATTERNS:
        m = pat.search(title)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if 0 <= end - start <= 500:
                return [str(n) for n in range(start, end + 1)]
    m = _LOT_LIST_RE.search(title)
    if m:
        return [d.group(0) for d in _LOT_LIST_MEMBER_RE.finditer(m.group(0))]
    return []


def _lot_series_text(stripped_title: str) -> str:
    """Best-effort series text for a lot title: everything before the
    earliest numeric range/list signal, with "Lot of N ..." boilerplate
    stripped wherever it falls (leading, e.g. "Lot of 11 Comics Amazing
    Spider-Man #1-10", or mid-string, e.g. "Avengers Lot of 11 Comics
    #1-10"). Falls back to the whole (stripped) title if no range/list
    pattern is found at all (the bare-"lot"-word-only case)."""
    earliest = None
    for pat in (*_LOT_RANGE_PATTERNS, _LOT_LIST_RE):
        m = pat.search(stripped_title)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    text = stripped_title[:earliest].strip() if earliest is not None else stripped_title.strip()
    return _LOT_BOILERPLATE_RE.sub("", text).strip()


# ─── identify_comic() ──────────────────────────────────────────────────────


def identify_comic(title: "str | None") -> ComicIdentity:
    """Extract a best-effort ComicIdentity from a freeform eBay listing title.

    This is the "title alone in, structured guess out" direction — the
    opposite of hard_reject/should_reject/score_against_wish, which all
    require a KNOWN candidate wish item to compare a title against. Be
    conservative: populate confidence/reject_reasons rather than guessing
    wildly (BUI-253). See ComicIdentity's docstring for the field semantics
    and the confidence tier table.
    """
    raw_title = title or ""
    identity = ComicIdentity(title=raw_title)

    if not raw_title.strip():
        identity.reject_reasons.append("empty title")
        return identity

    stripped = _strip_grades(raw_title)
    identity._title_norm = _normalize(stripped)

    # --- deterministic reject signals (independent of series/issue) -------
    if "cgc" in raw_title.lower():
        identity.reject_reasons.append("CGC slab")
    if _digital_reject(raw_title):
        identity.reject_reasons.append("digital-only listing")
    if _trading_card_reject(raw_title):
        identity.reject_reasons.append("trading card / TCG product")
    if _foreign_edition_reject(raw_title):
        identity.reject_reasons.append("foreign-language/-market edition")

    # --- edition classification ---------------------------------------------
    # BUI-253 PR-review fix (S2): classified BEFORE lot detection now (used
    # to run after) so the lot check below can consult it — see the S2 note
    # there for why the ordering matters.
    identity.edition = _classify_edition_kind(raw_title)
    if identity.edition == "facsimile":
        identity.reject_reasons.append("facsimile reprint, not an original")
    elif identity.edition == "collected":
        identity.reject_reasons.append(
            "collected edition (HC/TPB/Omnibus), not a single floppy issue"
        )
    elif identity.edition == "reprint":
        identity.reject_reasons.append(
            "later printing / promotional reprint, not an original first print"
        )
    # annual/giant-size/king-size/treasury/single-issue are classification
    # only, not inherently a reject signal (hard_reject already handles the
    # "mismatches the WISHED series' own edition" case; identify_comic has no
    # wish item to compare against here).

    # --- lot detection + expansion (BUI-243/BUI-261) -----------------------
    # BUI-253 PR-review fix (S2, should-fix): a collected/facsimile/reprint
    # edition is always a single purchasable SKU, never a multi-issue bundle
    # — but _LOT_RE's bare \bcollection\b branch (from "complete collection"/
    # "run collection") also fires on "Epic Collection" (a _COLLECTED_EDITION
    # _MARKERS entry), so "X-Men Epic Collection Vol 3" was getting
    # is_lot=True AND edition="collected" simultaneously — a contradictory
    # identity (a lot with no constituent_issues but ALSO the wrong 0.3/0.5
    # confidence tier instead of the "collected" 0.6 tier). Force is_lot=
    # False for these three edition kinds regardless of what _LOT_RE finds;
    # none of them should ever expand into constituent_issues.
    identity.is_lot = (
        identity.edition not in ("collected", "facsimile", "reprint")
        and bool(_LOT_RE.search(raw_title))
    )
    count_mismatch = False
    if identity.is_lot:
        identity.constituent_issues = _expand_lot_issues(raw_title)
        count_mismatch = lot_count_mismatch(raw_title)
        if count_mismatch:
            identity.reject_reasons.append(
                "lot count/range mismatch: stated count disagrees with parsed range"
            )
        if not identity.constituent_issues:
            identity.reject_reasons.append(
                "lot detected but constituent issues could not be parsed"
            )

    # --- series + issue extraction ------------------------------------------
    hash_m = _ISSUE_HASH_RE.search(stripped)
    bare_m = None
    if not identity.is_lot and not hash_m:
        bare_m, masked_for_bare = _bare_issue_match(stripped)

    if identity.is_lot:
        pre_issue_text = _lot_series_text(stripped)
    elif hash_m:
        pre_issue_text = stripped[: hash_m.start()]
    elif bare_m:
        pre_issue_text = masked_for_bare[: bare_m.start()]
    else:
        pre_issue_text = stripped

    if identity.edition == "annual":
        # BUI-460: first, strip "annual" together with its trailing separator
        # residue in one anchored pass (see _ANNUAL_WORD_AND_SEP_RE).
        pre_issue_text = _ANNUAL_WORD_AND_SEP_RE.sub("", pre_issue_text)
        # Then always also run the original global bare-word strip. This is a
        # no-op in the common single-"annual" case (already removed above),
        # but it's still needed for two shapes the anchored pass alone can't
        # reach: (1) the accepted BUI-129/BUI-456 bare-number-before-annual
        # non-goal, e.g. "ASM 252 annual 2024", where the issue digit — and
        # so pre_issue_text's truncation point — comes BEFORE "annual" even
        # appears, so the anchored pass above is a no-op and this is the only
        # thing that removes the word; and (2) a pathological double mention
        # ("X-Men Annual Annual #1"), where the anchored pass only reaches
        # the trailing occurrence and this catches the other one — matching
        # the original global-strip behavior, which this fix must not narrow.
        pre_issue_text = _ANNUAL_RE.sub("", pre_issue_text)
    elif identity.edition == "treasury":
        pre_issue_text = _TREASURY_RE.sub("", pre_issue_text)
    # Strip a purely-parenthetical year or year-range from the series text —
    # "Amazing Spider-Man (2022) #7" must yield series="Amazing Spider-Man"
    # with year=2022 reported separately (below), not folded into the series
    # string. A paren group with OTHER content (e.g. "(CGC 2024)") is left
    # alone — it isn't unambiguously just an era marker.
    pre_issue_text = _PAREN_YEAR_ONLY_RE.sub(" ", pre_issue_text)
    series_text = re.sub(r"\s+", " ", pre_issue_text).strip()
    identity.series = series_text or None

    if not identity.is_lot:
        if hash_m:
            identity.issue = hash_m.group(1)
        elif bare_m:
            identity.issue = bare_m.group(0)

    # --- year / volume --------------------------------------------------------
    identity.volume = _title_volume(raw_title)
    years = _all_title_years(raw_title)
    identity.year = years[0] if years else None
    ambiguous_year = len(set(years)) > 1

    # --- confidence -------------------------------------------------------
    if identity.is_lot:
        if not identity.constituent_issues:
            identity.confidence = 0.3
        elif count_mismatch:
            identity.confidence = 0.35
        else:
            identity.confidence = 0.5
    elif identity.issue is not None and identity.series:
        if hash_m:
            identity.confidence = 0.8 if ambiguous_year else 1.0
        else:
            identity.confidence = 0.5  # bare-digit fallback, no explicit "#"
    elif identity.issue is not None:
        # Issue found but series text was empty (e.g. title == "#5 NM").
        identity.confidence = 0.5
        identity.reject_reasons.append("no series text before issue number")
    elif identity.edition == "collected":
        # Expected shape for a HC/TPB/Omnibus — not an extraction failure.
        identity.confidence = 0.6
    else:
        identity.confidence = 0.3
        identity.reject_reasons.append("no issue number found")

    return identity


# BUI-327: the confidence-gated per-issue cover year cluster
# (_coerce_publication_year, confident_cover_year — BUI-316) now lives in
# comic_identity_year.py (imported and re-exported above).


# ─── score_against_wish() ──────────────────────────────────────────────────


def score_against_wish(identity: "ComicIdentity", wish_item: dict) -> float:
    """Return the fuzzy match score of *identity* against a single wish item.

    Extracted out of seller_scan.match_listing's inner loop (BUI-253 Step 2)
    so the scoring math lives next to the identity it scores — match_listing
    now reads as identify_comic(title) -> score_against_wish(identity, wish)
    per wish item. The math itself is UNCHANGED from the pre-Step-2
    match_listing: same issue_pattern regex, same token-overlap ratio, same
    0.0 return for a non-match (the 0.65 floor and best-of-all-wishes
    selection stay in match_listing, same as before).

    Uses identity._title_norm (computed once by identify_comic) rather than
    recomputing _normalize(_strip_grades(...)) per call — match_listing calls
    this once per wish item, so recomputing per call would be O(len(wish_items))
    redundant string processing for what used to be O(1) shared work.
    """
    issue = wish_item["issue"]
    issue_pattern = re.compile(
        r"(?:#\s*" + re.escape(issue) + r"\b|\b" + re.escape(issue) + r"\b)"
    )
    if not issue_pattern.search(identity._title_norm):
        return 0.0

    tokens = wish_item["_tokens"]
    if not tokens:
        return 0.0
    title_words = set(identity._title_norm.split())
    matched = sum(1 for t in tokens if t in title_words)
    return matched / len(tokens)


# ═══════════════════════════════════════════════════════════════════════════
# BUI-253 Step 5: Haiku verify-prompt bullet rendering.
#
# seller_scan.verify_with_claude's reject-list previously hand-typed example
# markers ("Mexican La Prensa", "2nd print") that already exist verbatim in
# the deterministic lexicons above (_FOREIGN_EDITION_MARKERS, _REPRINT_MARKERS)
# — two independent copies of the same words that could silently drift apart.
# These helpers render the ACTUAL lexicon contents into prompt-ready text so
# there is one source of truth; verify_with_claude just consumes the strings.
# ═══════════════════════════════════════════════════════════════════════════


def _joined_markers(markers: "frozenset[str]") -> str:
    """Sorted, comma-joined marker list for embedding in an LLM prompt."""
    return ", ".join(sorted(markers))


# _EDITION_PATTERNS (Step 1, untouched — hard_reject rule 2) is a list of
# compiled regexes, not a flat lexicon, so its words can't be safely
# recovered by introspecting regex source. This tuple supplies the
# human-readable labels for the SAME edition words and must be kept in sync
# with _EDITION_PATTERNS by hand — the same convention series_year_range's
# "ported from locg-cli, keep in sync" comment already uses elsewhere in this
# file. A smaller, honest solution given _EDITION_PATTERNS itself can't move
# or change (Step 1 byte-identity guarantee).
EDITION_LABELS: "tuple[str, ...]" = ("Annual", "Giant-Size", "King-Size", "Treasury")


def foreign_edition_examples() -> str:
    """Comma-joined _FOREIGN_EDITION_MARKERS (BUI-239), for the Haiku prompt's
    foreign-edition reject bullet. Deliberately narrower than the bullet's
    full semantic scope — bare nationality adjectives like "Spanish"/
    "French"/"German"/"Italian" are intentionally excluded from the
    deterministic lexicon (BUI-239 design: too ambiguous to hard-reject) and
    left to Haiku's judgment, so the caller should still name those broader
    cases explicitly alongside these concrete markers.
    """
    return _joined_markers(_FOREIGN_EDITION_MARKERS)


def later_printing_examples() -> str:
    """The subset of _REPRINT_MARKERS (BUI-227) that describes a LATER
    PRINTING specifically (not a facsimile/collected-edition format change,
    which should_reject already gates deterministically pre-Haiku) — for the
    Haiku prompt's "later printing / reprint" bullet.
    """
    later_printing = {m for m in _REPRINT_MARKERS if "printing" in m}
    return _joined_markers(later_printing)
