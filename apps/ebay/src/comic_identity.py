#!/usr/bin/env python3
"""comic_identity: deterministic freeform-title → comic-identity logic.

BUI-253 Step 1: this module consolidates the title-parsing / deterministic
reject helpers that used to live in seller_scan.py (grown across BUI-135,
BUI-221, BUI-226..245, BUI-261) so seller_scan.py, wishlist_sellers.py, and
(eventually) a standalone comic-identify CLI + the LLM skills all share ONE
implementation instead of re-deriving these rules. Step 1 is a pure move —
every symbol below is unchanged from its seller_scan.py original and is
re-exported from seller_scan.py so no caller (including the existing test
suite) had to change. Genuinely new identity logic (identify_comic(), lot
expansion, bare-year handling, etc.) lands in later BUI-253 steps.

Co-located with its two code consumers (seller_scan.py, wishlist_sellers.py)
in apps/ebay/src — NOT a packages/ workspace package, since the overlay never
parses freeform eBay titles and there is no cross-repo consumer today.
"""

from __future__ import annotations

import re
import sys

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
