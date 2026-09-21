#!/usr/bin/env python3
"""Shared grade/certification token tables for apps/ebay (BUI-923).

ebay_fetch.py (identify) and sold_comps.py (FMV comp parsing) both need to
recognize a CGC-scale numeric grade, a letter grade, and -- since BUI-923 --
a certifier name, a slab label (Signature Series, Qualified, Restored,
Conserved), and a page-quality token inside free eBay listing text. Both
modules already live in this one package (sold_comps already imports from
ebay_fetch), so this third module holds the shared vocabularies and regex
tables rather than duplicating them -- see plan
docs/plans/2026-09-21-001-feat-cgc-slab-support-plan.md, "One token module,
shared by identify and sold-comps."

`_NUMERIC_GRADE_RE` and `_LETTER_PATTERNS` are relocated verbatim from
sold_comps.py (behavior unchanged -- sold_comps.parse_grade imports them
back from here). Everything else is new for BUI-923.
"""

import re

# ─── Vocabularies (module constants -- BUI-923 KTD2, matching FMV_PROVENANCES
# style: shared with the pydantic validators the later CGC-slab-support units
# add on the fmv/comps tables). ────────────────────────────────────────────

CERTIFIER_VALUES = ("none", "cgc", "cbcs", "other")
LABEL_VALUES = ("universal", "signature_series", "qualified", "restored", "conserved", "other")
PAGE_QUALITY_VALUES = ("white", "ow_w", "ow", "c_ow", "cream", "unknown")


# ─── Numeric + letter grade parsing (moved verbatim from sold_comps.py) ────
#
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
#
# Word-form "near mint": the abbreviations NM / NM- were handled from the
# start, but the spelled-out forms were not — even though "very good", "good",
# "fair" and "poor" all were. Measured over the stored comps corpus (15,235
# rows, 9,339 of them grade-less): 57 grade-less titles say "near mint", 27 of
# those "near mint minus". Recovering them is the entire upside — the other
# 98.6% of grade-less titles carry no condition signal of any kind, so this is
# a parser-consistency fix, NOT an FMV-coverage improvement (per-book effect is
# 0-1 comps and will not clear a `too_sparse` flag).
#
# These two patterns bound with `(?<![a-z0-9])`/`(?![a-z0-9])` instead of `\b`
# because every one of those 57 titles comes from a seller format delimited by
# UNDERSCORES ("UNCANNY X-MEN #190_FEBRUARY 1985_NEAR MINT MINUS_AMAZING..."),
# and `_` is a word character — so `\b` never fires at either edge. The
# lookarounds treat `_` as the separator it visually is, while still refusing
# to match inside a longer alphanumeric run.
#
# Bare "mint" is deliberately NOT mapped. Of the 35 grade-less titles that
# contain it without "near", the set includes a Hasbro action figure ("MINT IN
# BOX MIB"), two graded trading cards ("BGS 9 Mint POP 1", a Panini football
# card), a silver coin ("Mintage 250") and a hedged "NM/ (Mint?)". It is seller
# vocabulary, not a grade, and mapping it to 9.9 would inject the scale's most
# inflated value off its least reliable signal.
_LETTER_PATTERNS = [
    # Tier 1 — slash combos (longest first)
    (re.compile(r'\bnm[/\\]m\b', re.I), 9.6),
    (re.compile(r'\bvf[/\\]nm\b', re.I), 9.0),
    (re.compile(r'\bfn[/\\]vf\b|\bfine[/\\]vf\b|\bfvf\b', re.I), 7.0),
    (re.compile(r'\bvg[/\\]fn\+(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg[/\\]fn\b', re.I), 5.0),
    (re.compile(r'\bgd[/\\]vg\b', re.I), 3.0),
    (re.compile(r'\bfr[/\\]gd\b', re.I), 1.5),

    # Tier 1b — the same slash combos SPELLED OUT. These must sit in Tier 1
    # for the identical reason the abbreviations do: a combo's second half is
    # itself a valid grade word, so a Tier 3 bare-word pattern would match the
    # component and short-circuit the loop. "VERY FINE/NEAR MINT" read as plain
    # "near mint" yields 9.4 instead of VF/NM's 9.0 — a 0.4-grade overstatement
    # on a book we might bid against.
    #
    # Two of these were already wrong before word-form near mint existed:
    # "FINE/VERY FINE" fell through to `\bfine\b` -> 6.0 (FN/VF is 7.0) and
    # "VERY GOOD/FINE" to `\bvery good\b` -> 4.0 (VG/FN is 5.0). Corpus counts
    # at the time of writing: very fine/near mint 8, fine/very fine 9, near
    # mint/mint 3, very good/fine 2.
    #
    # Separator class is `[/\\-]` with optional surrounding whitespace or
    # underscores, covering every form observed: "VERY FINE/NEAR MINT",
    # "VERY FINE-NEAR MINT", and "VERY FINE - NEAR MINT".
    (re.compile(r'(?<![a-z0-9])near[\s_]*mint[\s_]*[/\\-][\s_]*mint(?![a-z0-9])', re.I), 9.6),
    (re.compile(r'(?<![a-z0-9])very[\s_]*fine[\s_]*[/\\-][\s_]*near[\s_]*mint(?![a-z0-9])', re.I), 9.0),
    (re.compile(r'(?<![a-z0-9])fine[\s_]*[/\\-][\s_]*very[\s_]*fine(?![a-z0-9])', re.I), 7.0),
    (re.compile(r'(?<![a-z0-9])very[\s_]*good[\s_]*[/\\-][\s_]*fine(?![a-z0-9])', re.I), 5.0),
    (re.compile(r'(?<![a-z0-9])good[\s_]*[/\\-][\s_]*very[\s_]*good(?![a-z0-9])', re.I), 3.0),
    (re.compile(r'(?<![a-z0-9])fair[\s_]*[/\\-][\s_]*good(?![a-z0-9])', re.I), 1.5),

    # Tier 2 — letter + modifier (+ / -)
    (re.compile(r'\bnm\+(?!\w)', re.I), 9.6),
    (re.compile(r'\bnm-(?!\w)', re.I), 9.2),
    # Must precede the bare "near mint" in Tier 3: that pattern also matches
    # the "NEAR MINT MINUS" prefix, and the loop returns on first hit.
    (re.compile(r'(?<![a-z0-9])near[\s_-]+mint[\s_-]+minus(?![a-z0-9])', re.I), 9.2),
    (re.compile(r'\bvf\+(?!\w)', re.I), 8.5),
    (re.compile(r'\bvf-(?!\w)', re.I), 7.5),
    (re.compile(r'\bfn\+(?!\w)|\bfine\+(?!\w)', re.I), 6.5),
    (re.compile(r'\bfn-(?!\w)|\bfine-(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg\+(?!\w)', re.I), 4.5),
    (re.compile(r'\bvg-(?!\w)', re.I), 3.5),
    (re.compile(r'\bgd\+(?!\w)', re.I), 2.5),

    # Tier 3 — bare letters (must come last; other patterns would match inside)
    (re.compile(r'\bnm\b(?![+\-/\\])', re.I), 9.4),
    (re.compile(r'(?<![a-z0-9])near[\s_-]+mint(?![a-z0-9])', re.I), 9.4),
    (re.compile(r'\bvf\b(?![+\-/\\])', re.I), 8.0),
    (re.compile(r'\bfn\b(?![+\-/\\])|\bfine\b(?![+\-/\\])', re.I), 6.0),
    (re.compile(r'\bvg\b(?![+\-/\\])|\bvery good\b', re.I), 4.0),
    (re.compile(r'\bgd\b(?![+\-/\\])|\bgood\b', re.I), 2.0),
    (re.compile(r'\bfr\b(?![+\-/\\])|\bfair\b', re.I), 1.0),
    (re.compile(r'\bpoor\b', re.I), 0.5),
]


# ─── Certifier resolution (BUI-923) ────────────────────────────────────────
#
# "Uncertified", "None", "Not Graded", "Raw", and blank are the sentinel
# values a raw (unslabbed) listing routinely carries in a Certification /
# Professional Grader item-specifics field -- they must never be read as a
# real certification (ticket BUI-923 outcome text).
_ABSENT_CERTIFICATION_VALUES = frozenset({"uncertified", "none", "not graded", "raw", ""})

# Known certifier names, most-specific lookup first isn't needed here since
# each pattern is checked independently and returns its own value -- order
# only matters for which certifier wins when a text improbably names two.
_CERTIFIER_NAME_PATTERNS = (
    (re.compile(r'\bcgc\b', re.I), "cgc"),
    (re.compile(r'\bcbcs\b', re.I), "cbcs"),
    (re.compile(r'\bpgx\b', re.I), "other"),  # a third-party grader LOCAL_EXCLUDE_RE already excludes from raw comp pools
)


def resolve_certifier_token(text):
    """Find a known certifier name (CGC/CBCS/PGX) anywhere in free text.

    Returns the canonical certifier value, or None when no recognized name
    is present. Does not consider the absent-sentinel values -- those only
    apply to a structured Certification/Professional Grader field value; a
    caller matching against a title should not treat "Not Graded" as a
    certifier mention in the first place (it doesn't contain a certifier
    name token to match).
    """
    if not text:
        return None
    for pattern, value in _CERTIFIER_NAME_PATTERNS:
        if pattern.search(text):
            return value
    return None


def resolve_certifier_from_specifics_value(value):
    """Map a Certification/Professional Grader item-specifics VALUE to a
    canonical certifier, or None when the value is absent or unrecognized.

    "Uncertified", "None", "Not Graded", "Raw", and blank all count as
    absent -- raw listings routinely carry them (BUI-923 outcome). Any other
    non-blank value is a genuine certification field the seller filled in:
    an unrecognized third-party name still resolves to "other" rather than
    being silently dropped, since the field itself (unlike free title text)
    is a structured assertion of certification.
    """
    if not value:
        return None
    stripped = str(value).strip()
    if not stripped or stripped.lower() in _ABSENT_CERTIFICATION_VALUES:
        return None
    return resolve_certifier_token(stripped) or "other"


# ─── Label tokens (BUI-923) ─────────────────────────────────────────────────
#
# "signed" reuses the exact `(?<!not\s)` fixed-width negative lookbehind that
# sold_comps.LOCAL_EXCLUDE_RE already relies on (BUI-668): the corpus's one
# residual false positive was a seller advertising a book as "NOT signed",
# and the guard is what keeps that from being read as Signature Series.
_LABEL_PATTERNS = (
    (re.compile(r'\bqualified\b|\(q\)', re.I), "qualified"),
    (re.compile(r'\brestored\b|\(r\)', re.I), "restored"),
    (re.compile(r'\bconserved\b', re.I), "conserved"),
    (
        re.compile(
            r'\bss\b|\bsignature\s+series\b|(?<!not\s)\bsigned\b|\bautograph(?:ed)?\b|\bsignature\b',
            re.I,
        ),
        "signature_series",
    ),
)


def resolve_label(text):
    """Extract a label token (Signature Series / Qualified / Restored /
    Conserved) from free text -- a title, or a title plus an item-specifics
    grade value. Returns a LABEL_VALUES member, or None when nothing is
    found (the caller applies the "universal" default only once it has
    already decided the listing is certified -- a raw listing should stay
    blank, not "universal")."""
    if not text:
        return None
    for pattern, value in _LABEL_PATTERNS:
        if pattern.search(text):
            return value
    return None


# ─── Page-quality tokens (BUI-923) ──────────────────────────────────────────
#
# Ordered most-specific-first: "OW/W"/"OWW"/"OW-W" and "C/OW"/"CR/OW" must be
# checked before bare "OW", or `\bow\b` would match the "OW" component of
# each compound token first (e.g. inside "C/OW", the boundary before "OW" is
# the "/" — a non-word char — so `\bow\b` matches there too).
_PAGE_QUALITY_PATTERNS = (
    (re.compile(r'\bow[/\-]w\b|\boww\b', re.I), "ow_w"),
    (re.compile(r'\b(?:c|cr)[/\-]ow\b', re.I), "c_ow"),
    (re.compile(r'\bow\b', re.I), "ow"),
    (re.compile(r'\bwhite\s+pages\b|\bw\b', re.I), "white"),
    (re.compile(r'\bcream\b', re.I), "cream"),
)


def resolve_page_quality(text):
    """Extract a page-quality token from free text. Returns a
    PAGE_QUALITY_VALUES member, or None when nothing is found (see
    resolve_label's docstring for the same "blank unless certified" rule)."""
    if not text:
        return None
    for pattern, value in _PAGE_QUALITY_PATTERNS:
        if pattern.search(text):
            return value
    return None


# ─── Title certifier+grade adjacency (BUI-923) ──────────────────────────────
#
# A title asserts a real slab grade only when the certifier name sits right
# next to the numeric grade -- "CGC 9.4", "CGC AA SS 4.5" (0-2 short
# all-letter tokens like AA/SS between the two) -- not merely somewhere in
# the same title. "CGC ready, would grade 9.6" must NOT read as a 9.6 slab:
# the words between "CGC" and "9.6" aren't short all-letter connector
# tokens, so the adjacency match fails, and the ticket's own outcome text
# ("'CGC ready' or 'CGC it' in a title with no certification aspect is raw
# with grade_source: missing") says that case is deliberately raw, not a
# grade to fall back on. The numeric half mirrors `_NUMERIC_GRADE_RE`'s
# price/unit-suffix guards (a "CGC $9.80" title doesn't read as a 9.8 slab
# either) but deliberately DROPS its `(?<!x )(?<!X )` dimension-pair
# lookbehind: that guard exists to catch the second number in "2.5 x 3.5",
# but "PGX" ends in "X", so gluing it on unmodified made "PGX 9.8" false-
# reject ("X " immediately precedes the grade, tripping the same lookbehind
# meant for a bare dimension "x"). Right after a certifier name there is no
# dimension-pair ambiguity to guard against in the first place.
_CERTIFIER_TITLE_TOKEN_VALUES = {"cgc": "cgc", "cbcs": "cbcs", "pgx": "other"}

_TITLE_CERT_GRADE_RE = re.compile(
    r'\b(CGC|CBCS|PGX)\b'
    r'(?:\s+[A-Za-z]{1,4}\b){0,2}'
    r'\s+'
    r'(?<!\$)'
    r'\b([0-9]\.[02-9])'
    r'(?!\w)'
    r'(?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["\']))',
    re.IGNORECASE,
)


def extract_title_certification(title):
    """Look for a certifier name adjacent to a numeric grade in a listing
    title. Returns (certifier, grade, bare_mention):

    - certifier/grade are set (grade as a float) when the adjacency pattern
      matches -- the genuine slab case.
    - bare_mention is True when a certifier name appears in the title but
      not adjacent to a grade -- signals the caller to suppress any legacy
      raw-title grade fallback rather than treat a stray nearby number as a
      self-reported grade (see module docstring above).
    """
    if not title:
        return None, None, False
    m = _TITLE_CERT_GRADE_RE.search(title)
    if m:
        certifier = _CERTIFIER_TITLE_TOKEN_VALUES.get(m.group(1).lower())
        grade = float(m.group(2))
        return certifier, grade, False
    bare_mention = resolve_certifier_token(title) is not None
    return None, None, bare_mention
