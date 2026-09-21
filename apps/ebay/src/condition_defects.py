"""Seller-disclosed condition defects that disqualify a book outright (BUI-919).

Hsu Ken's standing buy rule (2026-09-16): never carry a comic to purchase when
the seller's own condition text names **moisture damage**, **rust**, or a
**loose / detached / missing staple**. Price and stated grade do not matter —
he has cut $0.99 books under it. All three get worse in storage rather than
merely looking bad: moisture keeps spreading and reaches neighbouring books,
staple rust stains the paper around it and transfers, and a book whose staple
no longer holds will not hold its grade either.

The three codes this module emits:

- ``moisture`` — water/moisture damage ("water damage", "water stain", "water
  ring", "moisture", "mildew", "tide line", ...).
- ``rust`` — any rust ("rusty staple", "rust migration", "rust stains").
- ``loose_staple`` — a staple that is loose, detached, missing or pulling.

**Out of scope, deliberately.** Plain "staining", "foxing" and "tanning" are
*not* triggers. The seller whose listings motivated this rule writes "moisture
damage" / "moisture stains" when they mean water and plain "staining"
otherwise, so the separate vocabulary *is* the signal — collapsing them would
drop every mildly-stained vintage book in the wish list. So are spine tape, a
spine split, and a missing piece of cover: those are graded by eye, not by
this gate.

Precision (BUI-668 is this exact class one path over: a bare ``restored``
token matched inside ``unrestored`` and dropped the comps it most wanted to
keep). Three traps are closed here and pinned by tests:

1. **Substring hits.** Every pattern is word-boundary anchored at both ends,
   so ``rust`` does not fire inside "trust", "crust", "encrusted" or
   "frustrating", and ``pop`` does not fire inside "popular".
2. **Negation.** "no rust", "free of rust", "rust-free", "no moisture damage"
   and "no loose staples" are not hits. The negation lookback is cut at the
   nearest clause boundary, so "no tape, rusty staples" still fires — that
   "no" belongs to a different clause.
3. **Positive assertions.** "staples tight", "staples secure" and "staples
   intact" carry no defect term at all, so the staple rule never fires on
   them; and the defect term has to sit in the *same clause* as the staple
   word, so "pieces missing back cover, staples intact" stays clean too.

The asymmetry that sets the dial: a defective listing kept wrongly costs real
money, a clean listing dropped wrongly costs one book — and every drop is
printed with its matched phrase, so a wrong drop is one the user can override
on sight rather than one that disappears.
"""

import re

__all__ = [
    "DEFECT_CODES",
    "DEFECT_LABELS",
    "detect_condition_defects",
    "listing_defect_findings",
    "format_defect_cell",
]

MOISTURE = "moisture"
RUST = "rust"
LOOSE_STAPLE = "loose_staple"

#: Emission order for codes, so a caller's output is stable regardless of
#: where in the text each hit happened to appear.
DEFECT_CODES = (MOISTURE, RUST, LOOSE_STAPLE)

#: Short human phrases for printing a drop reason.
DEFECT_LABELS = {
    MOISTURE: "moisture damage",
    RUST: "rust",
    LOOSE_STAPLE: "loose/detached staple",
}

# --- moisture -------------------------------------------------------------
# Bare "water" never fires on its own ("Waterworld", "water colour cover"); it
# only counts compounded with a damage noun. Bare "stain" never fires at all —
# see the out-of-scope note in the module docstring. "watermark" written solid
# is excluded (a seller saying their photos are watermarked would otherwise
# lose their whole inventory); "water mark" / "water-mark" still fires.
_MOISTURE_RE = re.compile(
    r"""
    \b(?:
        water[\s-]*(?:damag\w*|stain\w*|ring\w*|spot\w*)
      | water[\s-]+mark\w*
      | waterlogg\w*
      | moisture\w*
      | mildew\w*
      | mould\w* | moldy | mold
      | tide[\s-]*(?:line|mark)\w*
      | damp(?:ness)?
      | humidity[\s-]*damag\w*
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- rust -----------------------------------------------------------------
# The leading \b is what keeps "trust", "crust", "encrusted" and "frustrating"
# out — the BUI-668 substring trap, which is why this is one anchored
# alternation rather than a bare `rust` token.
_RUST_RE = re.compile(r"\brust(?:y|ed|ing|s|iness)?\b", re.IGNORECASE)

#: "rust colored logo" / "rust-coloured cover" names a colour, not corrosion.
_RUST_COLOUR_RE = re.compile(r"^[\s-]*colou?r\w*", re.IGNORECASE)

# --- loose / detached / missing staple -------------------------------------
# Anchor token. The rule is staple-scoped on purpose: "pieces missing back
# cover" is a hand-grading call, not a standing-rule drop, and broadening the
# anchor to `cover` would fire on it.
_STAPLE_RE = re.compile(r"\bstaples?\b", re.IGNORECASE)

# The defect term that has to share a clause with the staple word. "attached"
# is unreachable from `detach\w*` (different letters), so a reassuring "cover
# attached at both staples" stays clean.
_STAPLE_DEFECT_RE = re.compile(
    r"""
    \b(?:
        loose | loosen\w* | loosely
      | detach\w*
      | missing | absent | gone
      | pop | popped | popping
      | pull | pulls | pulled | pulling     # "staple pull", "pulling away"
      | separat\w*
      | broken | broke
      | torn[\s-]*(?:out|away|through|from|free|loose)
      | fallen | falling | fell[\s-]+(?:out|off)
      | (?:come|came)[\s-]*(?:out|away|off)
    )\b
    # NOTE: a bare `off` is deliberately absent — "off center staple" /
    # "off-centre staples" is a manufacturing note on a perfectly sound book,
    # and it is common enough in vintage listings to matter.
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- negation --------------------------------------------------------------
_NEGATOR_RE = re.compile(
    r"""
    \b(?:
        no | not | none | never | nothing | without | zero
      | free\s+(?:of|from)
      | devoid\s+of | lack(?:s|ing)?
      | cannot | can't | couldn't
      | isn't | aren't | doesn't | don't | didn't
      | wasn't | weren't | hasn't | haven't
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: A trailing "-free" / " free" negates the token it follows ("rust-free").
_TRAILING_FREE_RE = re.compile(r"^[\s-]*free\b", re.IGNORECASE)

#: Where a negation stops reaching. Punctuation ends it, and so does a
#: polarity-reversing conjunction ("no water damage BUT rust present" is a
#: rust hit). "and"/"or" deliberately do NOT end it, because a negator
#: distributes across them ("free of rust and mildew" negates both) — that
#: asymmetry is the whole reason this is a separate pattern from the clause
#: splitter below.
_NEGATION_BOUNDARY_RE = re.compile(
    r"[,;:.!?()\[\]{}\n\r|/]|\s[-–—]\s|\b(?:but|though|however|yet|except)\b",
    re.IGNORECASE,
)

#: Where a clause ends, for the staple/defect co-occurrence rule. Same as
#: above plus "and"/"or", which keeps "pieces missing back cover, staples
#: intact" and "spine split and staples tight" non-hits while "cover and 1st
#: wrap detached top staple" still fires (the defect term and the staple word
#: share the post-"and" clause).
_CLAUSE_BOUNDARY_RE = re.compile(
    _NEGATION_BOUNDARY_RE.pattern + r"|\band\b|\bor\b",
    re.IGNORECASE,
)

#: How far back a negator may sit and still bind to the defect term.
_NEGATION_LOOKBACK = 36


def _is_negated(text, start, end):
    """True when the match at ``text[start:end]`` is negated by its context.

    Looks back at most ``_NEGATION_LOOKBACK`` characters, cut at the nearest
    clause boundary, so a negator in a *previous* clause ("no tape, rusty
    staples") cannot reach across. Also treats a trailing "-free" as a
    negation ("rust-free", "moisture free").
    """
    window = text[max(0, start - _NEGATION_LOOKBACK):start]
    last_boundary = -1
    for match in _NEGATION_BOUNDARY_RE.finditer(window):
        last_boundary = match.end()
    if last_boundary >= 0:
        window = window[last_boundary:]
    if _NEGATOR_RE.search(window):
        return True
    return bool(_TRAILING_FREE_RE.match(text[end:end + 12]))


def _clause_spans(text):
    """Split ``text`` into ``(start, end)`` spans at clause boundaries."""
    spans = []
    cursor = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


#: Longest phrase reported back. A seller note is normally one short clause;
#: a title can be a whole line, and an unbounded phrase would blow out the
#: identify table's Defects column.
_MAX_PHRASE = 90


def _phrase(clause, match_start, match_end):
    """The seller's own words to quote back, taken from the whole clause.

    The clause, not the bare token, is what makes a printed drop reason
    readable: ``rust: "rusty staple"`` instead of ``rust: "rusty"``. A clause
    too long to quote (usually a title) is trimmed to a window around the
    match, marked with ellipses so nobody reads it as the full text.
    """
    clause = clause.rstrip()
    lead = len(clause) - len(clause.lstrip())
    clause = clause[lead:]
    match_start -= lead
    match_end -= lead
    if len(clause) <= _MAX_PHRASE:
        return clause
    slack = (_MAX_PHRASE - (match_end - match_start)) // 2
    start = max(0, match_start - slack)
    end = min(len(clause), start + _MAX_PHRASE)
    snippet = clause[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(clause) else "")


def _findings(text):
    """Every (code, phrase) hit in ``text``, in ``DEFECT_CODES`` order.

    Everything is scanned clause by clause. For moisture and rust that only
    shapes the reported phrase; for the staple rule it IS the rule (a defect
    term and a staple word have to share a clause), and it is the precision
    lever there: "GD+ condition, cover detached both staples" is one clause and
    fires, while '2" spine split, pieces missing back cover, staples intact'
    splits into three and no clause holds both a staple word and a live defect
    term. Negation is still judged against the full text, so a trailing
    "-free" is seen even when it lands past a boundary.
    """
    if not text or not isinstance(text, str):
        return []

    hits = {code: [] for code in DEFECT_CODES}

    for span_start, span_end in _clause_spans(text):
        clause = text[span_start:span_end]

        for code, pattern in ((MOISTURE, _MOISTURE_RE), (RUST, _RUST_RE)):
            for match in pattern.finditer(clause):
                start, end = span_start + match.start(), span_start + match.end()
                if _is_negated(text, start, end):
                    continue
                if code is RUST and _RUST_COLOUR_RE.match(text[end:end + 12]):
                    continue
                hits[code].append(_phrase(clause, match.start(), match.end()))

        if not _STAPLE_RE.search(clause):
            continue
        for match in _STAPLE_DEFECT_RE.finditer(clause):
            if _is_negated(text, span_start + match.start(), span_start + match.end()):
                continue
            hits[LOOSE_STAPLE].append(_phrase(clause, match.start(), match.end()))
            break

    ordered = []
    seen = set()
    for code in DEFECT_CODES:
        for phrase in hits[code]:
            key = (code, phrase.lower())
            if not phrase or key in seen:
                continue
            seen.add(key)
            ordered.append((code, phrase))
    return ordered


def detect_condition_defects(text):
    """Classify one blob of condition text.

    Returns ``(defect_codes, matched_phrases)`` — both tuples, both empty when
    the text is clean. ``defect_codes`` is ordered by :data:`DEFECT_CODES`;
    ``matched_phrases`` holds the literal text that fired, deduplicated, in
    the same order, so a printed drop reason can quote the seller back to the
    user instead of asserting a verdict.
    """
    ordered = _findings(text)
    codes = []
    phrases = []
    for code, phrase in ordered:
        if code not in codes:
            codes.append(code)
        if phrase not in phrases:
            phrases.append(phrase)
    return tuple(codes), tuple(phrases)


def listing_defect_findings(condition_description=None, title=None):
    """Per-listing findings, as a JSON-ready list of dicts.

    Scans the seller's free-text condition note first, then the title — a
    seller who hides the grade behind "see condition description" still
    sometimes names the defect in the title, and vice versa. Each entry is
    ``{"code", "phrase", "source"}``; ``source`` is ``"condition_description"``
    or ``"title"``, so a drop can say where the evidence came from and the
    user can judge an override on sight (a title hit on "Rusty" the New
    Mutants character reads very differently from a condition note reading
    "rusty staple").

    ``shortDescription`` is deliberately NOT scanned: on the listings that
    motivated this rule it returns the store's return-policy boilerplate,
    identical across every listing, so one template sentence mentioning water
    damage would drop that seller's whole inventory at once.
    """
    findings = []
    seen = set()
    for source, text in (
        ("condition_description", condition_description),
        ("title", title),
    ):
        for code, phrase in _findings(text):
            key = (code, phrase.lower())
            if key in seen:
                continue
            seen.add(key)
            findings.append({"code": code, "phrase": phrase, "source": source})
    return findings


def format_defect_cell(findings):
    """One compact cell/line for a table: ``rust: "rusty staple"``.

    Returns None when there is nothing to show, so a caller renders its own
    blank marker.
    """
    if not findings:
        return None
    parts = []
    for finding in findings:
        label = DEFECT_LABELS.get(finding["code"], finding["code"])
        phrase = (finding.get("phrase") or "").strip()
        part = f'{label}: "{phrase}"' if phrase else label
        if part not in parts:
            parts.append(part)
    return "; ".join(parts)
