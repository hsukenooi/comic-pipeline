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

# Issue tokens: handle "#5", "#5,6,7", "Issue 5", "No. 5", etc.
_ISSUE_HASH_RUN = re.compile(r"#\s*(\d+(?:\s*[,&]\s*\d+)*)")
_ISSUE_BARE_RUN = re.compile(r"\b(\d+(?:\s*[,&]\s*\d+){2,})\b")  # bare run like "1,2,3,4,5"
_ISSUE_HASH_SINGLE = re.compile(r"#\s*(\d{1,4})\b")
_ISSUE_NO_KEYWORD = re.compile(r"\b(?:Issue|No|Number)\.?\s*#?\s*(\d{1,4})\b", re.IGNORECASE)

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
    issue: str | None
    grade: float | None
    year: int | None
    confidence: str  # 'high' | 'medium' | 'low'

    def to_dict(self) -> dict:
        return {
            "series": self.series,
            "issue": self.issue,
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
        return LETTER_GRADE_MAP.get(token.upper()) or LETTER_GRADE_MAP.get(token), token
    return None, None


def _extract_year(text: str) -> int | None:
    m = _YEAR_PATTERN.search(text)
    if m:
        return int(m.group(1))
    return None


def _extract_issue(text: str) -> tuple[str | None, bool]:
    """Return (first_issue_str, is_run).

    For multi-issue runs ("#5,6,7" or bare "1,2,3,4,5"), return the first
    issue and is_run=True. Otherwise the single issue with is_run=False.
    """
    # 1. Hash + run, e.g. "#5,6,7,8"
    m = _ISSUE_HASH_RUN.search(text)
    if m:
        first = re.split(r"[,&]", m.group(1))[0].strip()
        is_run = "," in m.group(1) or "&" in m.group(1)
        return first, is_run

    # 2. Hash + single, e.g. "#300"
    m = _ISSUE_HASH_SINGLE.search(text)
    if m:
        return m.group(1), False

    # 3. "Issue 5" / "No. 5"
    m = _ISSUE_NO_KEYWORD.search(text)
    if m:
        return m.group(1), False

    # 4. Bare run, e.g. "1,2,3,4,5,6,7,8,9" (3+ items so we don't catch random numbers)
    m = _ISSUE_BARE_RUN.search(text)
    if m:
        first = re.split(r"[,&]", m.group(1))[0].strip()
        return first, True

    return None, False


def _clean_series(text: str) -> str:
    """Strip publisher names, edition tags, condition tokens, and trailing
    descriptors. Returns cleaned series candidate."""
    t = text

    # Remove issue tokens (#5,6,7 / #300 / "Issue 5" / bare runs)
    t = _ISSUE_HASH_RUN.sub(" ", t)
    t = _ISSUE_HASH_SINGLE.sub(" ", t)
    t = _ISSUE_NO_KEYWORD.sub(" ", t)
    t = _ISSUE_BARE_RUN.sub(" ", t)

    # Remove edition tags / condition tokens
    t = _EDITION_RE.sub(" ", t)

    # Remove year
    t = _YEAR_PATTERN.sub(" ", t)

    # Token-level publisher scrub
    tokens = re.split(r"\s+", t)
    cleaned: list[str] = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        low = tok.lower().strip(".,!?;:")
        # "Dark Horse" 2-token publisher
        if low == "dark" and i + 1 < len(tokens) and tokens[i + 1].lower().strip(".,!?;:") == "horse":
            skip_next = True
            continue
        if low in _PUBLISHER_WORDS:
            continue
        cleaned.append(tok)
    t = " ".join(cleaned)

    # Truncate at common descriptor markers (creator names, app/key markers, etc.)
    cut_markers = [
        r"\b1st\s+app\w*",
        r"\bfirst\s+app\w*",
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
    issue, is_run = _extract_issue(raw)
    series = _clean_series(raw)

    # Confidence heuristic
    if not series:
        confidence = "low"
    elif issue and not is_run and (grade is not None or year is not None):
        confidence = "high"
    elif issue and not is_run:
        confidence = "medium"
    elif issue and is_run:
        confidence = "low"
    else:
        # No issue: usable for series-only matches but uncertain
        confidence = "low"

    return ParsedTitle(
        series=series,
        issue=issue,
        grade=grade,
        year=year,
        confidence=confidence,
    )
