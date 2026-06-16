"""Centralized, tested parsing of money-affecting strings (BUI-189).

One source of truth for the issue-token pattern and price extraction, so the
fragile parsers that feed money decisions can't re-diverge across the matcher
(``commands.py``), the reconciler (``collection_io.py``), and the HTML scraper
(``parser.py``):

* The issue-token CORE pattern is defined once (``_ISSUE_CORE``). The matcher
  takes the FIRST ``#N`` in a title; the reconciler takes the TRAILING one.
  Both compose the same core, so the BUI-175 truncation (``#1.MU`` → ``1``)
  cannot reappear in one place after being fixed in the other.
* ``extract_price`` keeps thousands separators and the full decimal (the old
  ``\\$(\\d+\\.?\\d*)`` read ``$1,234.56`` as ``1.0``).

Property-based coverage lives in ``tests/test_parsing.py``.
"""
from __future__ import annotations

import re
from typing import Optional

# Issue-token core: digits, an optional ``.decimal``/``.suffix`` group, and an
# optional trailing letter run. Captures #1, #1A, #1AU, #1.MU, #20.1, #1.5
# without truncation (BUI-175). Callers add their own anchoring.
_ISSUE_CORE = r"\d+(?:\.[A-Za-z0-9]+)?[A-Za-z]*"

# First ``#N`` anywhere in the title (the matcher's series/issue split).
ISSUE_TOKEN_RE = re.compile(r"#\s*(" + _ISSUE_CORE + r")")
# ``#N`` anchored to the end (the reconciler's trailing-token extractor).
ISSUE_TOKEN_TRAILING_RE = re.compile(r"#\s*(" + _ISSUE_CORE + r")\s*$")

# Price: a ``$`` then a (possibly comma-grouped) integer part and optional
# decimals. The comma group is stripped before float().
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def split_full_title(full_title: str) -> tuple[str, Optional[str]]:
    """Split a cached ``full_title`` into ``(series_portion, issue_token)``.

    ``"Thor #154"``                -> ``("Thor", "154")``
    ``"Fantastic Four Annual #6"`` -> ``("Fantastic Four Annual", "6")``
    ``"Watchmen"`` (no ``#N``)     -> ``("Watchmen", None)``

    The series portion is everything before the first ``#N`` token, so qualifier
    words like ``Annual`` stay attached to the series identity instead of being
    collapsed into the base series (BUI-26).
    """
    m = ISSUE_TOKEN_RE.search(full_title)
    if m:
        return full_title[: m.start()].strip(), m.group(1)
    return full_title.strip(), None


def trailing_issue_token(full_title: str) -> Optional[str]:
    """Return the issue token only when ``#N`` is the TAIL of the title, else None.

    Used by the reconciler, which matches an export row to a cache row by a
    trailing issue token; a title with trailing variant words is intentionally
    not matched here.
    """
    m = ISSUE_TOKEN_TRAILING_RE.search(full_title)
    return m.group(1).lower() if m else None


def normalize_issue_key(token: str) -> str:
    """Canonical comparison key for an issue token: strip leading zeros, lower."""
    return (token.strip().lstrip("0") or token.strip()).lower()


def extract_price(text: str) -> Optional[float]:
    """Extract a price from text like ``' · $4.99'`` or ``'$1,234.56'``.

    Keeps thousands separators and the full decimal (the old form dropped both,
    reading ``$1,234.56`` as ``1.0``). Returns None when no ``$`` price is found.
    """
    m = _PRICE_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))
