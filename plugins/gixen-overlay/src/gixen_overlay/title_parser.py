"""Heuristic parser for eBay comic listing titles.

Extracts (series, issue, year, grade, confidence) from raw eBay titles.

Design choice: pragmatic regex-based parser. The Anthropic SDK is not in
requirements.txt and not installed on either host, so per the spec we fall
back to deterministic regex extraction. Output schema mirrors what an LLM
JSON response would have produced, so a future LLM-backed implementation is
a drop-in replacement.

Confidence scale:
  - high   : issue + (grade or year) cleanly extracted, single issue
  - medium : issue extracted, but ambiguous run/missing year/grade
  - low    : multi-issue run with first-issue heuristic, or weak series
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Letter grade -> representative numeric (per spec)
LETTER_GRADE_MAP: dict[str, float] = {
    "NM/M": 9.8,
    "NM+": 9.6,
    "NM": 9.4,
    "NM-": 9.2,
    "VF/NM": 9.0,
    "VFNM": 9.0,  # spotted in real titles, e.g. "VFNM"
    "VF+": 8.5,
    "VF": 8.0,
    "VF-": 7.5,
    "FN/VF": 7.0,
    "FN+": 6.5,
    "FN": 6.0,
    "FN-": 5.5,
    "VG/FN": 5.0,
    "VG+": 4.5,
    "VG": 4.0,
    "VG-": 3.5,
    "GD/VG": 3.0,
    "GD+": 2.5,
    "GD": 2.0,
    "FR": 1.0,
    "PR": 0.5,
}

# Order matters: longer/more specific tokens first so e.g. "NM+" matches before "NM".
# Use lookaround instead of \b because \b doesn't treat '+' or '-' as word chars,
# which breaks tokens like "VF-" or "NM+".
_LETTER_GRADE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])("
    + "|".join(re.escape(k) for k in sorted(LETTER_GRADE_MAP.keys(), key=len, reverse=True))
    + r")(?![A-Za-z0-9/+\-])"
)

# CGC/CBCS 9.4 etc. — if present, this beats letter grades.
_NUMERIC_GRADE_PATTERN = re.compile(
    r"\b(?:CGC|CBCS|PGX)\s+(\d+(?:\.\d)?)\b",
    re.IGNORECASE,
)

# Year: 1930-2099. Reject years that look like price/issue context.
_YEAR_PATTERN = re.compile(r"\b(19[3-9]\d|20\d{2})\b")

# Issue tokens: handle "#5", "#5,6,7", "#1-9", "Issue 5", "No. 5", etc.
# Order of evaluation matters — see _extract_issue.
_ISSUE_HASH_RANGE = re.compile(r"#\s*(\d+)\s*-\s*(\d+)\b")
_ISSUE_HASH_RUN = re.compile(r"#\s*(\d+(?:\s*[,&]\s*\d+)*)")
_ISSUE_BARE_RANGE = re.compile(r"\b(\d{1,3})\s*-\s*(\d{1,3})\b")
_ISSUE_BARE_RUN = re.compile(r"\b(\d+(?:\s*[,&]\s*\d+){2,})\b")  # bare run like "1,2,3,4,5"
_ISSUE_HASH_SINGLE = re.compile(r"#\s*(\d{1,4})\b")
_ISSUE_NO_KEYWORD = re.compile(r"\b(?:Issue|No|Number)\.?\s*#?\s*(\d{1,4})\b", re.IGNORECASE)
# Last-resort fallback for titles that omit "#" entirely, e.g.
# "UNCANNY X-MEN   211 - (NM+)". Conservative: requires whitespace before, must
# not look like a decimal (rejects the "9" in "9.8"). Year filtering happens at
# the call site so we can pick the *next* candidate when the first is a year.
_ISSUE_BARE_SINGLE = re.compile(r"(?<=\s)(\d{1,4})(?!\.\d)\b")

# Tokens to scrub from series text.
_PUBLISHER_WORDS = {
    "marvel", "dc", "image", "dark horse", "darkhorse", "idw", "valiant",
    "boom", "boom!", "epic", "vertigo", "dynamite", "oni", "archie",
    "comics", "comic",
}

_EDITION_TAGS = [
    r"1st\s+print(?:ing)?s?",
    r"first\s+print(?:ing)?s?",
    r"2nd\s+print(?:ing)?s?",
    r"3rd\s+print(?:ing)?s?",
    r"4th\s+print(?:ing)?s?",
    r"variants?",
    r"virgin\s+variant",
    r"sketch\s+variant",
    r"director'?s?\s+cut",
    r"limited\s+series",
    r"full\s+run",
    r"mini[-\s]?series",
    r"hardcover",
    r"paperback",
    r"trade\s+paperback",
    r"tpb",
    r"omnibus",
    r"key(?:\s+issue)?",
    r"low\s+print\s+run",
    r"high\s+grades?",
    r"\bcondition\b",
    r"homage(?:\s+(?:cover|variant))?",
    r"beauty",
    r"gem",
    r"wow",
    r"rare",
    r"incredible",
    r"newsstand",
    r"direct\s+edition",
    r"\bnm\+?\b", r"\bnm-?\b", r"\bnm/m\b",
    r"\bvf\+?\b", r"\bvf-?\b", r"\bvf/nm\b", r"\bvfnm\b",
    r"\bfn\+?\b", r"\bfn-?\b", r"\bfn/vf\b",
    r"\bvg\+?\b", r"\bvg-?\b", r"\bvg/fn\b",
    r"\bgd\+?\b", r"\bgd/vg\b",
    r"\bfr\b", r"\bpr\b",
    r"cgc\s*\d+(?:\.\d)?",
    r"cbcs\s*\d+(?:\.\d)?",
    r"pgx\s*\d+(?:\.\d)?",
]

_EDITION_RE = re.compile(r"|".join(_EDITION_TAGS), re.IGNORECASE)


@dataclass
class ParsedTitle:
    series: str
    issue: str | None  # primary issue (first in run); kept for backward compat
    grade: float | None
    year: int | None
    confidence: str  # 'high' | 'medium' | 'low'
    issues: list[str] | None = None  # all issues in a run/range; None for parser failures

    def to_dict(self) -> dict:
        return {
            "series": self.series,
            "issue": self.issue,
            "issues": self.issues,
            "grade": self.grade,
            "year": self.year,
            "confidence": self.confidence,
        }


def _extract_grade(text: str) -> tuple[float | None, str | None]:
    """Return (numeric_grade, matched_token) or (None, None)."""
    m = _NUMERIC_GRADE_PATTERN.search(text)
    if m:
        try:
            return float(m.group(1)), m.group(0)
        except ValueError:
            pass
    m = _LETTER_GRADE_PATTERN.search(text)
    if m:
        token = m.group(1)
        return LETTER_GRADE_MAP.get(token.upper()), token
    return None, None


def _extract_year(text: str) -> int | None:
    m = _YEAR_PATTERN.search(text)
    if m:
        return int(m.group(1))
    return None


def _expand_range(low_str: str, high_str: str) -> list[str] | None:
    """Expand 'low-high' into a list of issue strings, or None if the range
    looks like a year/price/garbage rather than an issue range.

    Heuristics: reject year ranges (1930-2099 endpoints), reverse/zero ranges,
    and ranges spanning more than 50 (catches prices, decade spans).
    """
    try:
        low = int(low_str)
        high = int(high_str)
    except ValueError:
        return None
    if low >= high:
        return None
    if (high - low) > 50:
        return None
    if 1930 <= low <= 2099 or 1930 <= high <= 2099:
        return None
    return [str(i) for i in range(low, high + 1)]


def _extract_issue(text: str) -> tuple[list[str], bool]:
    """Return (issues, is_run).

    issues: ordered list of every issue mentioned. Empty if nothing matched.
    is_run: True when the title implied a multi-issue run (comma list,
    ampersand pair, or expanded range). Used to dampen confidence.
    """
    # 1. Hash + range, e.g. "#1-9". Try this before HASH_RUN because the run
    # regex would otherwise match the leading "#1" and stop.
    m = _ISSUE_HASH_RANGE.search(text)
    if m:
        expanded = _expand_range(m.group(1), m.group(2))
        if expanded:
            return expanded, True

    # 2. Hash + run/single, e.g. "#5,6,7,8" or "#300"
    m = _ISSUE_HASH_RUN.search(text)
    if m:
        parts = [p.strip() for p in re.split(r"[,&]", m.group(1)) if p.strip()]
        is_run = len(parts) > 1
        return parts, is_run

    # 3. "Issue 5" / "No. 5"
    m = _ISSUE_NO_KEYWORD.search(text)
    if m:
        return [m.group(1)], False

    # 4. Bare run, e.g. "1,2,3,4,5,6,7,8,9" (3+ items so we don't catch random numbers)
    m = _ISSUE_BARE_RUN.search(text)
    if m:
        parts = [p.strip() for p in re.split(r"[,&]", m.group(1)) if p.strip()]
        return parts, True

    # 5. Bare range, e.g. "1-5". Only accepted when _expand_range's heuristics
    # confirm it's not a year span.
    m = _ISSUE_BARE_RANGE.search(text)
    if m:
        expanded = _expand_range(m.group(1), m.group(2))
        if expanded:
            return expanded, True

    # 6. Bare single number — last-resort fallback for hashless titles like
    # "X-MEN 211 - (NM+)". Walks matches in order and returns the first that
    # isn't a year, so "Spider-Man 300 1988 NM" returns "300" rather than 1988.
    for m in _ISSUE_BARE_SINGLE.finditer(text):
        n = int(m.group(1))
        if 1930 <= n <= 2099:
            continue
        return [m.group(1)], False

    return [], False


def _clean_series(text: str) -> str:
    """Strip publisher names, edition tags, condition tokens, and trailing
    descriptors. Returns cleaned series candidate."""
    t = text

    # Remove issue tokens (#5,6,7 / #1-9 / #300 / "Issue 5" / bare runs / bare ranges).
    # Order: scrub more-specific ranges before generic single/run patterns,
    # and bare-range last so it doesn't eat year tokens (year is removed below).
    t = _ISSUE_HASH_RANGE.sub(" ", t)
    t = _ISSUE_HASH_RUN.sub(" ", t)
    t = _ISSUE_HASH_SINGLE.sub(" ", t)
    t = _ISSUE_NO_KEYWORD.sub(" ", t)
    t = _ISSUE_BARE_RUN.sub(" ", t)

    # Remove publisher context parens like "(DC Comics September 1983)".
    # Must run before edition-tag removal so the year inside is still present
    # to anchor the match (avoids partial matches on grade-only parens).
    t = re.sub(
        r"\([^)]*\b(?:marvel|dc|image|dark\s+horse|idw|valiant|boom|vertigo|comics|comic)\b[^)]*\)",
        " ", t, flags=re.IGNORECASE,
    )

    # Remove edition tags / condition tokens. Then strip orphaned parens left
    # behind by scrubbing tokens like "(NM+)" → "(  )".
    t = _EDITION_RE.sub(" ", t)
    t = re.sub(r"\(\s*[^A-Za-z0-9]*\s*\)", " ", t)
    # Truncate at the first stray ")" that has no matching "(" — listings often
    # have "Series Issue - (Grade) -extra noise" and scrubbing the grade leaves
    # "Series Issue - ) -extra noise" with the trailing junk we don't want in
    # the dedup key.
    t = re.sub(r"\s+[^A-Za-z0-9\s]*\).*", "", t)

    # Remove year
    t = _YEAR_PATTERN.sub(" ", t)

    # Strip any remaining bare 1-4 digit numbers. Years and prices are gone by
    # this point, so leftover digits are issue-context (matches what
    # _ISSUE_BARE_SINGLE picks up). Keeps the series clean for dedup.
    t = re.sub(r"(?<=\s)\d{1,4}\b", " ", t)

    # Token-level publisher scrub
    tokens = re.split(r"\s+", t)
    cleaned: list[str] = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        low = tok.lower().strip(".,!?;:()")
        # "Dark Horse" 2-token publisher
        if low == "dark" and i + 1 < len(tokens) and tokens[i + 1].lower().strip(".,!?;:") == "horse":
            skip_next = True
            continue
        if low in _PUBLISHER_WORDS:
            continue
        cleaned.append(tok)
    t = " ".join(cleaned)

    # Seller format is typically "Series Issue - (Grade) - Description". After
    # the issue/grade/year scrubs above, a standalone " - " (with spaces on
    # both sides — hyphenated "X-Men" doesn't match) is the boundary between
    # series and listing-specific description. Truncate there. Runs before the
    # single-letter token filter, which would otherwise eat the bare "-".
    m = re.search(r"\s+-\s+", t)
    if m and m.start() > 2:
        t = t[: m.start()]

    # Truncate at common descriptor markers (creator names, app/key markers, etc.)
    cut_markers = [
        r"\b1st\s+app\w*",
        r"\bfirst\s+app\w*",
        # "1st Jason Todd as Robin", "1st Dick Grayson", etc. — content notes,
        # not edition tags. Exclude "class/series/issue/edition/vol" so
        # series like "Spider-Man 1st Class" are not truncated.
        r"\b1st\s+(?!print|class|series|issue|edition|vol)[A-Za-z]",
        r"\bauction\b",
        r"\bhuge\b",
        r"\bfull\s+run\b",
        r"\bkey\b",
        r"\bmcfarlane\b",
        r"\bcapullo\b",
        r"\bsnyder\b",
        r"\blemire\b",
        r"\bmiller\b",
        r"\bjoker\s+shoots?\b",
        r"\bbatgirl\b",
        r"\basm\b",  # "ASM 300" homage references
        r"\bhomage\b",
        r"\bcover\b",
    ]
    for pat in cut_markers:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            t = t[: m.start()]

    # Drop stray single-letter / decorative tokens like "Z", "+"
    tokens = [tok for tok in t.split() if len(tok) > 1 or tok.isalpha() and tok.lower() in {"a", "i"}]
    t = " ".join(tokens)

    # Collapse whitespace and strip stray punctuation
    t = re.sub(r"\s+", " ", t).strip(" -.,!?&|/")

    # Heuristic: if "X The Y" pattern (e.g. "Batman The Dark Knight Returns"),
    # convert to "X: The Y" — common for famous DC/Marvel titles.
    m = re.match(r"^(\w+)\s+The\s+(.+)$", t)
    if m and m.group(1).lower() in {"batman", "superman", "wolverine", "daredevil", "spider-man"}:
        t = f"{m.group(1)}: The {m.group(2)}"

    return t


def parse_title(title: str) -> ParsedTitle:
    """Parse an eBay comic listing title into structured fields.

    Returns a ParsedTitle with series, issue, grade, year, confidence.
    Series is best-effort — empty string if nothing usable remains.
    """
    if not title or not title.strip():
        return ParsedTitle(series="", issue=None, grade=None, year=None, confidence="low")

    raw = title.strip()

    grade, _grade_tok = _extract_grade(raw)
    year = _extract_year(raw)
    issues, is_run = _extract_issue(raw)
    series = _clean_series(raw)

    # Primary issue (first one) is what existing call sites expect.
    primary_issue = issues[0] if issues else None

    # Confidence heuristic
    if not series:
        confidence = "low"
    elif primary_issue and not is_run and (grade is not None or year is not None):
        confidence = "high"
    elif primary_issue and not is_run:
        confidence = "medium"
    elif primary_issue and is_run:
        confidence = "low"
    else:
        # No issue: usable for series-only matches but uncertain
        confidence = "low"

    return ParsedTitle(
        series=series,
        issue=primary_issue,
        issues=issues if issues else None,
        grade=grade,
        year=year,
        confidence=confidence,
    )
