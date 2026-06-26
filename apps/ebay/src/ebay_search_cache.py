"""
ebay_search_cache.py — SHA-256-keyed disk cache for eBay keyword-search results.

Mirrors the cache layer in sold_comps.py (same pattern: _cache_path, _cache_get,
_cache_put) but keyed on a keyword string rather than a canonical URL, and exposed
as a clean public API instead of private helpers.

Public API
----------
cache_key(keyword)            -> str          SHA-256 hex of the canonical keyword
cache_path(keyword)           -> Path         CACHE_DIR / f"{cache_key}.json"
get(keyword, *, ttl_sec=...)  -> list | None  fresh cache hit or None
put(keyword, items)           -> None         write items list to cache
filter_active(items, *, now)  -> list         drop listings whose end_date_iso is past
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR: Path = Path.home() / ".cache" / "wishlist-sellers" / "searches"
DEFAULT_CACHE_TTL_SEC: int = 7 * 24 * 3600  # 7 days


# ─── Key / path helpers ───────────────────────────────────────────────────────

def cache_key(keyword: str) -> str:
    """Return the SHA-256 hex digest of the canonical (lowercased, stripped) keyword."""
    canonical = keyword.strip().lower()
    return hashlib.sha256(canonical.encode()).hexdigest()


def cache_path(keyword: str) -> Path:
    """Return the Path where the cached results for *keyword* would be stored."""
    return CACHE_DIR / f"{cache_key(keyword)}.json"


# ─── Cache read / write ───────────────────────────────────────────────────────

def get(keyword: str, *, ttl_sec: int = DEFAULT_CACHE_TTL_SEC) -> list | None:
    """Return the cached list of result dicts if present and fresh, else None.

    "Fresh" means the file's mtime is within *ttl_sec* seconds of now.
    A missing, expired, or corrupt file all return None (never raise).
    """
    path = cache_path(keyword)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_sec:
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            return None
        return data
    except Exception:  # noqa: BLE001  # corrupt/partial file — return None
        return None


def put(keyword: str, items: list) -> None:
    """Write *items* (list of result dicts) to the cache for *keyword*.

    Uses an atomic tmp→rename write (mirrors sold_comps._cache_put).
    """
    path = cache_path(keyword)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(path)


# ─── Active-listing filter ────────────────────────────────────────────────────

def filter_active(items: list, *, now: datetime | None = None) -> list:
    """Return *items* with ended/expired listings removed.

    A listing is considered ended if its ``end_date_iso`` field is a parseable
    ISO-8601 timestamp that is strictly in the past relative to *now*.

    Fail-open rule: items whose ``end_date_iso`` is missing, None, or
    unparseable are KEPT — we never silently drop a listing we cannot date.

    Parameters
    ----------
    items:
        List of result dicts (each may have an ``end_date_iso`` key).
    now:
        Reference UTC datetime for "now".  Defaults to ``datetime.now(timezone.utc)``.
        Injectable for deterministic testing.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    active = []
    for item in items:
        end_raw = item.get("end_date_iso")
        if not end_raw:
            # Missing or None → keep (fail-open)
            active.append(item)
            continue
        try:
            # Handle trailing 'Z' (not supported by fromisoformat before Python 3.11)
            end_str = end_raw
            if isinstance(end_str, str) and end_str.endswith("Z"):
                end_str = end_str[:-1] + "+00:00"
            end_dt = datetime.fromisoformat(end_str)
            # If the parsed datetime is naive, assume UTC
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= now:
                # Past or exactly now → ended, drop it
                continue
        except (ValueError, TypeError, AttributeError):
            # Unparseable → keep (fail-open)
            active.append(item)
            continue
        active.append(item)
    return active
