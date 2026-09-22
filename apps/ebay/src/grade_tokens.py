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


# ─── Label tokens (BUI-923; autograph split BUI-941) ────────────────────────
#
# "signed" reuses the exact "not" guard that sold_comps.LOCAL_EXCLUDE_RE
# already relies on (BUI-668): the corpus's one residual false positive was a
# seller advertising a book as "NOT signed", and the guard is what keeps that
# from being read as a signature at all. BUI-969 widened both copies from the
# fixed-width `(?<!not\s)` to `(?<!\s)(?<!not)\s*`, so "not  signed" with
# any run of whitespace is guarded too (see LOCAL_EXCLUDE_RE's comment for
# the mechanism and the corpus measurement: 0 titles move).
#
# BUI-941 split the one signature pattern into three, because a single
# `-> signature_series` mapping was wrong in both directions:
#
#   * `ULTIMATE FALLOUT #4 CGC 7.5 W/ AUTOGRAPHS` matched NOTHING -- the old
#     `\bautograph(?:ed)?\b` has no plural branch, so the trailing "s" killed
#     the trailing `\b` -- and an autographed slab therefore entered the
#     UNIVERSAL comp pool at 7.5 (fmv-math-spec.md 7b pools on
#     `(comic_id, certifier, label)`, and `parse_slab_fields` defaults a
#     label-less slab to "universal").
#   * `CGC AA SS 4.5 ... Signed by Stan Lee` matched `\bss\b` and read as
#     Signature Series, although that listing's description says the
#     autograph was authenticated after the fact by JSA -- which is exactly
#     what CGC's yellow Signature Series label is NOT (it certifies a
#     signature a CGC witness saw applied). An unwitnessed or after-market
#     signature is what CGC's green Qualified label is for, so `qualified`
#     is the vocabulary member that reading belongs to.
#
# The rule, in precedence order (see `resolve_label`):
#   1. an explicit non-signature label word wins outright (unchanged);
#   2. AFTER-MARKET authentication evidence (`AA` = Authentic Autograph, or
#      JSA/PSA alongside a signature word) -> `qualified`, BEATING any
#      witnessed token in the same text -- a seller who writes both "AA" and
#      "SS" is describing an authenticated autograph, and the conservative
#      reading is the one that does not claim CGC witnessed the signing;
#   3. an explicit WITNESSED token (`Signature Series`, `SS`, `Yellow Label`)
#      -> `signature_series`;
#   4. a bare signature word with no witnessed token -> `qualified`.
#
# Step 4 is a deliberate behavior change and a deliberately COARSE reading: a
# title alone cannot tell a witnessed signature from an unwitnessed one, so
# the choice is which claim to make without evidence, and "CGC witnessed this"
# is the stronger one. Both values are non-Universal, so for every consumer
# that exists today the two are interchangeable -- 7b punts EVERY
# non-Universal target to `needs_manual` before any fetch, and the comp pool
# only ever filters `label == "universal"`. What step 4 buys is an honest
# `label_qualified` punt reason and an honest ledger row; what it costs is
# that a genuine yellow-label slab whose title only says "Signed by <name>"
# is archived as `qualified`. `Yellow Label` is in step 3 precisely because
# the corpus contains that case (`Batman #227 (1970) CGC 5.0 Signed By Neal
# Adams Yellow Label`), and it is the one title-side evidence that settles it.
#
# MEASURED over the offline corpus at ~/.cache/ebay-sold-comps (1,116 cached
# provider responses, 23,488 comp rows, 16,033 unique titles, 426 of them
# naming CGC/CBCS), old `resolve_label` vs new:
#
#   * 17 slab titles carry any signature/autograph token. 243 titles change
#     label, 11 of them slab titles, and 0 of the 426 slab titles move the
#     other way (non-Universal -> Universal).
#   * Exactly ONE title changes in the way that moves a pool: the ticket's own
#     `... ULTIMATE FALLOUT #4 CGC 7.5 W/ AUTOGRAPHS` goes None -> `qualified`,
#     i.e. `universal` -> `qualified` once `parse_slab_fields` applies its
#     default. It is the lone sale in Ultimate Fallout #4's CGC 7.5 rung
#     ($222.22, against genuine rungs 9.0 $349.95 / 9.2 $392.50 / 9.4 $383.50 /
#     9.6 $537.00 / 9.8 $707.48). Per the graded-ladder section of
#     docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-
#     classifier.md the question on a ladder is inversion, not which side of
#     the median: this comp does not invert the ladder, it IS the bottom rung,
#     so removing it does not move a cap -- it withdraws the only anchor below
#     9.0, and a Universal 8.0/8.5 target refuses `outside_ladder` instead of
#     interpolating off an autographed sale. A refusal, not a wrong number.
#   * The other 10 slab flips are `signature_series` -> `qualified` (step 4).
#     All 10 were ALREADY non-Universal, so not one of them changes pool
#     membership anywhere; only the punt reason and the ledger label move.
#   * The remaining 232 flips are on RAW titles, where no caller applies the
#     result at all -- `ebay_fetch.extract_certification`,
#     `sold_comps.parse_slab_fields` and `seller_scan.title_certification_fields`
#     each resolve a label only after a certifier is confirmed.
#
# Three boundary decisions, each measured rather than assumed:
#   * `AA` is bounded `(?<![-\w])aa(?![-\w])`, not `\baa\b`. `\b` fires inside
#     a hyphen-glued run, and the corpus's only `\baa\b` hit is exactly that:
#     `2026 Topps Chrome Marvel Andy Kubert Auto #AA-AK Ultimate X-Men /99`,
#     a trading card. The bounded form rejects it -- 1 measured false positive
#     killed, and 0 remaining corpus hits of either sign.
#   * `SS` gets the same bounds for the same reason: `\bss\b` matches the
#     seller stock numbers `SS-254` and `SS-12` (2 corpus titles, both raw);
#     the bounded form drops both and loses no true positive.
#   * JSA and PSA only count ALONGSIDE a signature word. Bare `\bjsa\b` is a
#     trap -- JSA is the Justice Society of America, a DC series, so a
#     `JSA #1 CGC 9.8` slab would be thrown out of its own Universal pool and
#     its target punted. (Corpus: 0 JSA titles, so this costs nothing
#     measurable and avoids an out-of-sample hazard. 82 titles carry PSA, 2 of
#     them alongside a signature word, 0 of those naming CGC/CBCS -- and
#     `\bpsa\b` is in both LOCAL_EXCLUDE_RE and _GRADED_MODE_EXCLUDE_RE
#     anyway, so a PSA comp never reaches a pool in either mode. That is by
#     design, not a leak: a PSA slab is neither a raw comp nor a CGC/CBCS
#     comp -- fmv-math-spec.md lists "psa" under "Other graders" beside the
#     raw fetch's `-cgc -cbcs -graded -slab` -- so BUI-969 declined to make
#     the raw-side exclusion target-side only.)
#     The gate narrows the JSA collision without dissolving it: a SIGNED
#     Justice Society slab (`JSA #1 CGC 9.8 SS Signed by Geoff Johns`) still
#     reads `qualified` off its own series name. That residual is bounded to
#     which non-Universal label it gets, never to Universal, so it cannot put
#     a signed slab back in a blue-label pool.
#
# Two spellings measured and deliberately NOT added:
#   * `\bautograph` as a bare PREFIX (the form LOCAL_EXCLUDE_RE itself uses).
#     Over the corpus it adds exactly one title over the bounded form, and it
#     is a seller's shop name -- `... READ FREE SHIP AutographDen` on a raw
#     baseball-card listing. The bounded form's trailing `\b` rejects it, so
#     "autograph(s|ed)" stays bounded here even though its sibling in
#     LOCAL_EXCLUDE_RE is not.
#   * bare `\bauto\b`, the card-hobby abbreviation. 9 corpus titles, every one
#     a trading card and not one naming CGC/CBCS -- no slab evidence to
#     support it and a word too common to spend on nothing.
#
# Not a trap, checked anyway: `\bsigned\b` cannot match inside "designed",
# "consigned", "assigned" or "unsigned" -- the character before "signed" is a
# word character in each, so no `\b` exists there (and the corpus has 0
# occurrences of any of the four). A publisher's "signed edition" variant and
# a title naming the signer both read as a signature, which is correct: on a
# SLAB either one is a signed copy and so not Universal, and on a raw listing
# the label is never applied. A title that merely NAMES the artist
# (`X-Men #1 CGC 9.6 Jim Lee cover`) carries no signature word and stays
# unlabelled, i.e. Universal.
_EXPLICIT_LABEL_PATTERNS = (
    # "Green label" is CGC's own name for the Qualified label. Spelled in
    # full deliberately -- bare `\bgreen\b` hits 185 corpus titles (Green
    # Goblin, Green Lantern, Green Arrow).
    (re.compile(r'\bqualified\b|\(q\)|\bgreen\s+label\b', re.I), "qualified"),
    (re.compile(r'\brestored\b|\(r\)', re.I), "restored"),
    (re.compile(r'\bconserved\b', re.I), "conserved"),
)

# Any assertion that the book carries a signature, of any provenance.
_SIGNATURE_RE = re.compile(
    r'\bautograph(?:s|ed)?\b|(?<!\s)(?<!not)\s*\bsigned\b|\bsignature\b', re.I)

# An assertion that CGC/CBCS WITNESSED the signing -- the yellow label.
_WITNESSED_SIGNATURE_RE = re.compile(
    r'\bsignature\s+series\b|\byellow\s+label\b|(?<![-\w])ss(?![-\w])', re.I)

# "AA" -- Authentic Autograph: a signature authenticated after the fact
# rather than witnessed at signing. Self-sufficient (it names the thing
# outright), unlike the third-party authenticators below.
_AUTHENTIC_AUTOGRAPH_RE = re.compile(r'(?<![-\w])aa(?![-\w])', re.I)

# Third-party autograph authenticators. Only meaningful next to a signature
# word -- see the JSA/Justice Society trap in the block comment above.
_THIRD_PARTY_AUTHENTICATOR_RE = re.compile(r'\b(?:jsa|psa)\b', re.I)


def resolve_label(text):
    """Extract a label token (Signature Series / Qualified / Restored /
    Conserved) from free text -- a title, or a title plus an item-specifics
    grade value. Returns a LABEL_VALUES member, or None when nothing is
    found (the caller applies the "universal" default only once it has
    already decided the listing is certified -- a raw listing should stay
    blank, not "universal").

    A signature of ANY provenance resolves to a non-Universal label, so an
    autographed slab can never join a Universal comp pool (BUI-941). Which
    non-Universal label depends on whether the text claims the signing was
    witnessed; see the block comment above for the precedence and the corpus
    measurement behind it.
    """
    if not text:
        return None
    for pattern, value in _EXPLICIT_LABEL_PATTERNS:
        if pattern.search(text):
            return value
    signed = bool(_SIGNATURE_RE.search(text))
    if _AUTHENTIC_AUTOGRAPH_RE.search(text) or (
            signed and _THIRD_PARTY_AUTHENTICATOR_RE.search(text)):
        return "qualified"
    if _WITNESSED_SIGNATURE_RE.search(text):
        return "signature_series"
    if signed:
        return "qualified"
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


# ─── Title-key stripping (BUI-932) ──────────────────────────────────────────
#
# seller-scan and wishlist-sellers build a title-matching string / cache key
# by stripping grade tokens (comic_identity._strip_grades) so a decimal grade
# can't orphan into a false issue-number match. BUI-932 lets a certified
# (CGC/CBCS) title reach that same matching path behind the new
# include_graded flag, so its certification tokens need stripping too --
# otherwise a slab and the same book's raw listing key differently
# ("Ultimate Fallout #4 CGC 9.8 OW/W" must key identically to "Ultimate
# Fallout #4"), and a stray "CGC"/"SS"/page-quality word sits in the scored
# text for no reason.
#
# Deliberately narrower than resolve_label's full label vocabulary (no
# Qualified/Restored/Conserved) and leaves PGX alone -- this is a
# title-matching aid for the two scan tools, not price-identity label
# resolution (see resolve_label for that).
#
# `SS` uses the same hyphen-aware bounds as _WITNESSED_SIGNATURE_RE (BUI-969):
# a loose `\bss\b` fires on the "SS" half of a seller stock number like
# "SS-254", stripping it to a dangling "-254". Measured over the offline
# corpus (16,688 unique titles): the loose form's only extra hits are the two
# stock-number titles resolve_label's block comment names; no genuine SS
# token is lost.
_TITLE_KEY_CERT_PATTERNS = (
    re.compile(r'\bcgc\b', re.IGNORECASE),
    re.compile(r'\bcbcs\b', re.IGNORECASE),
    re.compile(r'(?<![-\w])ss(?![-\w])', re.IGNORECASE),
)


def strip_certification_tokens(text):
    """Remove certifier names (CGC/CBCS), the Signature Series abbreviation
    (SS), and page-quality tokens (OW/W, OW, C/OW, White (Pages), Cream)
    from free text.

    Used by comic_identity._strip_grades and wishlist_sellers._title_key
    (BUI-932) so a slab title's certification tokens don't depress a wish
    match score or split a title-key cache hit from its raw counterpart.
    Reuses _PAGE_QUALITY_PATTERNS verbatim, most-specific-first, for the
    same reason resolve_page_quality does (see its comment above).
    """
    if not text:
        return text
    for pattern in _TITLE_KEY_CERT_PATTERNS:
        text = pattern.sub(" ", text)
    for pattern, _value in _PAGE_QUALITY_PATTERNS:
        text = pattern.sub(" ", text)
    return text
