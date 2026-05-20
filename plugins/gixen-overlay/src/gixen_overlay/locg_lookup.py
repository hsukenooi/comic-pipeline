"""LOCG year fallback for eBay titles that omit a year.

`title_parser.parse_title()` requires a year, but real eBay listings often
don't include one. Rather than skipping such bids during `extract-comics`,
shell out to the `locg` CLI to resolve (series, issue) → (locg_id, year).

This module is intentionally subprocess-based: gixen-overlay declares no
dependencies, so importing `locg` directly would pull a real Python package
into the plugin's install footprint. The CLI is the existing integration
boundary (every comic skill calls it via shell), and subprocess is easy to
mock in tests.

Behaviour is fail-soft: on any error (CLI missing, network failure, ambiguous
match, missing date on the detail page) the resolver returns None so that
`extract-comics` falls through to its existing skip path with a clearer
reason. Set ``LOCG_FALLBACK_DISABLED=1`` to short-circuit the resolver.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Resolved at module load so tests can monkeypatch the constant if needed.
# The CLI installs as `locg` on PATH; PYTHONPATH/site-packages handle the
# import side.
LOCG_CMD = os.environ.get("LOCG_CMD", "locg")

# Bounded so a hung CLI invocation can't stall `extract-comics`.
LOCG_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class LocgResolution:
    """Result of a successful LOCG fallback lookup."""
    year: int
    locg_id: int
    locg_variant_id: int | None


def resolve_year_and_locg(series: str, issue: str) -> LocgResolution | None:
    """Resolve (series, issue) → (year, locg_id, locg_variant_id) via the LOCG CLI.

    Returns None on any failure — the caller is expected to treat None as
    "skip this bid" with an informative reason (see ``last_error()`` for the
    most recent failure detail when needed for logging).
    """
    if os.environ.get("LOCG_FALLBACK_DISABLED") == "1":
        return None
    if not series or not issue:
        return None

    spec = f"{series}:{issue}"
    lookup = _run_locg(["lookup", spec, "--no-collection"])
    if lookup is None:
        return None

    # `locg lookup` returns a list; we asked for one spec, so take the head.
    if not isinstance(lookup, list) or not lookup:
        return None
    row = lookup[0]
    if not isinstance(row, dict) or row.get("error"):
        logger.info("locg lookup error for %r: %s", spec, row.get("error") if isinstance(row, dict) else "non-dict response")
        return None
    locg_id = row.get("locg_id")
    if not isinstance(locg_id, int):
        return None
    locg_variant_id = row.get("locg_variant_id") if isinstance(row.get("locg_variant_id"), int) else None

    detail = _run_locg(["comic", str(locg_id)])
    if not isinstance(detail, dict) or detail.get("error"):
        return None

    year = _year_from_detail(detail)
    if year is None:
        return None

    return LocgResolution(year=year, locg_id=locg_id, locg_variant_id=locg_variant_id)


def _run_locg(args: list[str]) -> object | None:
    """Run `locg <args>` and return parsed JSON, or None on any failure."""
    try:
        proc = subprocess.run(
            [LOCG_CMD, *args],
            capture_output=True,
            text=True,
            timeout=LOCG_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.info("locg %s failed: %s", args, e)
        return None
    if proc.returncode != 0:
        logger.info("locg %s exited %d: %s", args, proc.returncode, proc.stderr.strip())
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        logger.info("locg %s returned non-JSON: %s", args, e)
        return None


def _year_from_detail(detail: dict) -> int | None:
    """Pull a 4-digit year out of a `locg comic` detail response.

    Prefers ``store_date`` (publication date) over ``cover_date`` because
    store_date matches what users would write in an eBay title parenthetical.
    Both fields are human-formatted strings like ``"July 6, 1988"``.
    """
    for field in ("store_date", "cover_date"):
        value = detail.get(field)
        if isinstance(value, str):
            m = re.search(r"\b(19[3-9]\d|20\d{2})\b", value)
            if m:
                return int(m.group(1))
    return None
