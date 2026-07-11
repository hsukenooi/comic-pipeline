#!/usr/bin/env python3
"""comic_identity_year: paren-year / plausible-year / edition-classification
helpers extracted out of comic_identity.py (BUI-327 — comic_identity.py had
grown to ~1560 lines after BUI-316).

Pure move, no behavior change: every symbol here is unchanged from its
comic_identity.py original and is re-exported from comic_identity.py so no
caller had to change — the same convention BUI-253 Step 1 used when it moved
symbols out of seller_scan.py (see that module's docstring / this module's
sibling comic_identity.py for the precedent).

Three cohesive pieces live here:
  - Plausible cover-year window (_is_plausible_year + its two bounds) — the
    single gate _title_paren_years, _all_title_years, and
    _coerce_publication_year all defer to so they can never drift apart on
    what counts as a real comic cover year vs. SKU/price/OCR noise.
  - Paren-year / bare-year extraction from freeform titles
    (_title_paren_years, _all_title_years), plus the BUI-316 confidence-gated
    per-issue cover year built on top of them (_coerce_publication_year,
    confident_cover_year).
  - Edition-kind classification (_classify_edition_kind) — annual vs
    giant-size vs king-size vs treasury vs collected vs facsimile vs reprint
    vs single-issue — consumed by identify_comic() and confident_cover_year()
    in comic_identity.py.

Import direction: comic_identity.py imports FROM this module at its own top
level (era_mismatch needs _title_paren_years; identify_comic needs
_classify_edition_kind/_ANNUAL_RE/_TREASURY_RE/_all_title_years). This module
is deliberately kept a leaf at import time — it does NOT import
comic_identity.py at module scope. The one place it needs something back
(_classify_edition_kind calling _marker_hit and _second_print_reject, both of
which have other consumers that stay in comic_identity.py — the
_reprint_reject/_digital_reject/_trading_card_reject/_foreign_edition_reject
family for _marker_hit, should_reject for _second_print_reject) uses a
deferred, call-time import instead of a top-level one. A top-level import in
both directions would be a genuine circular import; deferring the
back-reference to call time — by which point both modules have finished
loading regardless of which one a caller imports first — breaks it cleanly
without duplicating either helper.
"""

from __future__ import annotations

import re

# Plausible cover-year window: a 4-digit token outside this range is noise (a
# SKU, a price, a garbage OCR number), not a real comic year. Defined once so
# the three validators below — _title_paren_years, _all_title_years, and
# _coerce_publication_year (BUI-316) — can never drift out of sync.
_MIN_PLAUSIBLE_YEAR = 1930
_MAX_PLAUSIBLE_YEAR = 2035


def _is_plausible_year(year: int) -> bool:
    """True if *year* falls in the plausible comic cover-year window."""
    return _MIN_PLAUSIBLE_YEAR <= year <= _MAX_PLAUSIBLE_YEAR


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
      "Batman (1940) #100 NM"                            → [1940]
      "Some Book (1963) (CGC 2024)"                      → [1963, 2024]
      "Amazing Spider-Man #7 VF"                         → []
    """
    years: list[int] = []
    for group in re.findall(r"\([^)]*\)", title or ""):
        for m in re.finditer(r"\b(\d{4})\b", group):
            yr = int(m.group(1))
            if _is_plausible_year(yr):
                years.append(yr)
    return years


# ─── Bare/embedded-year extraction ─────────────────────────────────────────

_BARE_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _all_title_years(title: str) -> "list[int]":
    """Every plausible year (1930-2035) mentioned in *title*, parenthesized
    ones first (high precision, via _title_paren_years) followed by any bare/
    embedded ones (lower precision — a run-year span like "1962-1963" or a
    trailing "Marvel 1988" with no parens). Order is preference order, not
    necessarily title order across the two groups.
    """
    paren_years = _title_paren_years(title)
    no_parens = re.sub(r"\([^)]*\)", " ", title or "")
    bare_years = []
    for m in _BARE_YEAR_RE.finditer(no_parens):
        yr = int(m.group(1))
        if _is_plausible_year(yr):
            bare_years.append(yr)
    return paren_years + bare_years


# ─── Edition classification (annual-nests-in-parent vs Giant-Size-own-series) ─
# These duplicate the regex BODIES already in _EDITION_PATTERNS (kept generic
# there for hard_reject's "does title contain any edition word" check) because
# identify_comic needs to treat each edition word differently: Annual and
# Treasury nest inside the PARENT series' full_title in LOCG's catalog (an
# annual is "The Amazing Spider-Man" with issue "Annual N", not its own
# series), so that word is stripped out of the extracted series text. Giant-
# Size and King-Size are the opposite — LOCG catalogs them as their OWN
# series (e.g. "Giant-Size X-Men" is a distinct series_name from "X-Men"), so
# that word STAYS in the extracted series text. This is the sharp edge BUI-253
# called out explicitly.
_ANNUAL_RE = re.compile(r"\bannual\b", re.IGNORECASE)
# Matches an optional trailing "Edition" too ("Treasury Edition") so
# stripping it from the series text doesn't leave a dangling "Edition" word
# behind (e.g. "Superman Treasury Edition #1" -> series "Superman", not
# "Superman Edition").
_TREASURY_RE = re.compile(r"\btreasury(?:\s+edition)?\b", re.IGNORECASE)
_GIANT_SIZE_RE = re.compile(r"\bgiant[\s-]size\b", re.IGNORECASE)
_KING_SIZE_RE = re.compile(r"\bking[\s-]size\b", re.IGNORECASE)

# Collected-edition / reprint markers reused from the BUI-227 lexicon, split
# out by kind so identify_comic can report WHICH kind of non-original-format
# this is (edition="collected" for a HC/TPB/Omnibus single-SKU vs
# edition="facsimile" vs edition="reprint" for a later-printing/promo
# reprint) instead of _reprint_reject's single boolean.
_COLLECTED_EDITION_MARKERS: frozenset[str] = frozenset({
    "omnibus", "trade paperback", "tpb", "epic collection",
})
_FACSIMILE_MARKERS: frozenset[str] = frozenset({"facsimile"})
_PROMO_REPRINT_MARKERS: frozenset[str] = frozenset({
    "true believers", "marvel tales", "2nd printing", "second printing",
    "retold",  # BUI-253 PR-review fix (S1): also added to _REPRINT_MARKERS
    # above so should_reject's deterministic path and identify_comic's
    # edition classification both recognize it — one lexicon addition,
    # kept in both places since they're intentionally split by kind.
})


def _classify_edition_kind(title: str) -> str:
    """Return the edition kind for *title* — checked against the WHOLE title
    (these markers can appear anywhere, not just between series and issue).

    Priority order (first match wins): facsimile > collected > later-printing
    reprint > annual > giant-size > king-size > treasury > single-issue.
    Facsimile/collected/reprint take priority over annual/giant-size/etc.
    because a title can combine them (e.g. "X-Men Annual #1 Facsimile
    Edition") and the printing-format distinction is the more actionable
    reject signal.
    """
    # BUI-327: deferred (call-time) import — see the module docstring's
    # "Import direction" note. _marker_hit and _second_print_reject stay in
    # comic_identity.py (both have other consumers there), while
    # comic_identity.py imports _classify_edition_kind FROM this module at
    # its own top level. Resolving this one back-reference at call time
    # rather than at module-load time avoids a real circular import.
    from comic_identity import _marker_hit, _second_print_reject

    if _marker_hit(title, _FACSIMILE_MARKERS):
        return "facsimile"
    if _marker_hit(title, _COLLECTED_EDITION_MARKERS):
        return "collected"
    if _marker_hit(title, _PROMO_REPRINT_MARKERS) or _second_print_reject(title):
        return "reprint"
    if _ANNUAL_RE.search(title):
        return "annual"
    if _GIANT_SIZE_RE.search(title):
        return "giant-size"
    if _KING_SIZE_RE.search(title):
        return "king-size"
    if _TREASURY_RE.search(title):
        return "treasury"
    return "single-issue"


# ─── Confidence-gated per-issue cover year (BUI-316) ────────────────────────
# The proper fix for BUI-308's single-owned-wrong-volume false positive: a
# no-year /comic:collection-check query for a rebootable masthead (Fantastic
# Four, ASM, X-Men, …) can return a confident `in_collection` against the WRONG
# volume, because the matcher's year gate fails open with no year. Forwarding
# the CORRECT per-issue cover year lets that gate (locg _year_gate_accepts,
# release_date.startswith within ±1) reject the wrong-volume row.
#
# The hazard being avoided is BUI-129: forwarding the WRONG year — a series
# `year_began` — filtered out every owned mid-run issue and returned a false
# `not_in_cache` for the whole run. The key reframing (BUI-308 design): BUI-129
# came from the wrong year, NOT from supplying a year at all. So a year we are
# confident is the issue's ACTUAL cover year is Pareto-better — it can only
# ever help — while an uncertain one must never be forwarded (the check then
# behaves exactly as today, year-agnostic).


def _coerce_publication_year(item_specifics: "dict | None") -> "int | None":
    """Parse item-specifics ``Publication Year`` to a plausible 4-digit int, or None.

    Reuses the same 1930–2035 plausibility window as _title_paren_years so the
    two signals are compared on the same footing. Fails open (None) on any
    missing/unparseable/out-of-range value.
    """
    if not item_specifics:
        return None
    raw = item_specifics.get("Publication Year")
    if raw is None:
        return None
    try:
        year = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    return year if _is_plausible_year(year) else None


def confident_cover_year(
    title: "str | None",
    item_specifics: "dict | None",
) -> "int | None":
    """Return a per-issue cover year to forward to /comic:collection-check ONLY
    when two independent signals corroborate it — else None (year-agnostic).

    STRICT confidence gate (BUI-316). A year is emitted only when BOTH:
      1. eBay's item-specifics ``Publication Year`` parses to a plausible year, and
      2. a parenthesized year in the listing title (_title_paren_years) agrees
         with it within ±1 — the same cover-vs-onsale tolerance the matcher's
         own year gate uses (BUI-214/251).

    The Publication Year is the authoritative per-issue cover year and is what
    we return; the title's parenthesized year is the corroborating check. This
    corroboration is what makes a WRONG year overwhelmingly unlikely: for a
    long-running series whose title paren carries a VOLUME start year (e.g.
    "Amazing Spider-Man (1963) #238", an issue that shipped 1983 with
    Publication Year 1983), the two signals DISAGREE by 20 years, so nothing is
    emitted and the check stays year-agnostic.

    NEVER emit for a facsimile / reprint edition: its Publication Year is the
    ORIGINAL issue's year while the physical book is a modern reprint, so the
    year would falsely match — and thereby confirm ownership of — the original
    volume the buyer does not actually own.

    Accepted residual (BUI-316 design, matcher-softening rejected): the two
    signals are both seller-entered, so they are only *usually* independent. In
    the rare case where a seller stamps the SAME volume-start year in both the
    title paren AND the Publication Year aspect for a mid-run issue (e.g. both
    "1963" for ASM #238), the gate fires and forwards the wrong year — the
    residual this cannot detect at the listing level. That is strictly narrower
    than BUI-129 (which forwarded year_began for the WHOLE run unconditionally),
    it needs a genuine double-mis-tag rather than the normal volume-decoration
    convention, and the direction is a missed match (duplicate-buy risk), not
    silent data loss. See test_correlated_wrong_year_is_the_accepted_residual.
    """
    # A reprint format's Publication Year is the original issue's year, not the
    # copy the buyer is holding — forwarding it would falsely match the owned
    # original. Refuse outright, before any corroboration.
    if _classify_edition_kind(title or "") in ("facsimile", "reprint"):
        return None

    pub_year = _coerce_publication_year(item_specifics)
    if pub_year is None:
        return None

    if any(abs(py - pub_year) <= 1 for py in _title_paren_years(title or "")):
        return pub_year
    return None
