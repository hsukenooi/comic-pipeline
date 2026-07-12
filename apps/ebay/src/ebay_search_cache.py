"""
ebay_search_cache.py — SHA-256-keyed disk cache for eBay keyword-search results.

Mirrors the cache layer in sold_comps.py (same pattern: _cache_path, _cache_get,
_cache_put) but keyed on a keyword string rather than a canonical URL, and exposed
as a clean public API instead of private helpers. put() writes through
ebay_fetch.atomic_write_json() (BUI-333) rather than hand-rolling the
tmp-file-then-.replace() idiom, so the two caches share one atomic-write
implementation.

Public API
----------
cache_key(keyword, mode="all")            -> str          SHA-256 hex of the canonical (mode, keyword) pair
cache_path(keyword, mode="all")           -> Path         CACHE_DIR / f"{cache_key}.json"
get(keyword, *, mode="all", ttl_sec=...)  -> list | None  fresh cache hit or None
put(keyword, items, *, mode="all")        -> None         write items list to cache
filter_active(items, *, now)              -> list         drop listings whose end_date_iso is past

The *mode* parameter namespaces the cache so that auction-only results never
cross-contaminate mixed (AUCTION|FIXED_PRICE) results.  Recognised values mirror
the --buying-options CLI choices: "auction", "bin", "all".  Callers that omit
mode get the legacy "all" namespace — backward-compatible with existing caches.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ebay_fetch import atomic_write_json

CACHE_DIR: Path = Path.home() / ".cache" / "wishlist-sellers" / "searches"
DEFAULT_CACHE_TTL_SEC: int = 7 * 24 * 3600  # 7 days


# ─── Key / path helpers ───────────────────────────────────────────────────────

def cache_key(keyword: str, mode: str = "all") -> str:
    """Return the SHA-256 hex digest of the canonical (mode, keyword) pair.

    *mode* namespaces the key so that auction-only and mixed results never
    share the same cache slot.  Callers that omit *mode* get the "all"
    namespace, which matches the legacy single-mode behaviour.
    """
    canonical = f"{mode.strip().lower()}\n{keyword.strip().lower()}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def cache_path(keyword: str, mode: str = "all") -> Path:
    """Return the Path where the cached results for *keyword* / *mode* would be stored."""
    return CACHE_DIR / f"{cache_key(keyword, mode)}.json"


# ─── Cache read / write ───────────────────────────────────────────────────────

def get(keyword: str, *, mode: str = "all", ttl_sec: int = DEFAULT_CACHE_TTL_SEC) -> list | None:
    """Return the cached list of result dicts if present and fresh, else None.

    "Fresh" means the file's mtime is within *ttl_sec* seconds of now.
    A missing, expired, or corrupt file all return None (never raise).

    *mode* must match the value used when the results were stored; different
    modes map to different cache files (no cross-contamination).
    """
    path = cache_path(keyword, mode)
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


def put(keyword: str, items: list, *, mode: str = "all") -> None:
    """Write *items* (list of result dicts) to the cache for *keyword* / *mode*.

    Uses the shared atomic tmp→rename write (ebay_fetch.atomic_write_json(),
    BUI-333 — was a hand-rolled copy of the same idiom).

    *mode* namespaces the file so that auction-only and mixed results never
    overwrite each other.
    """
    path = cache_path(keyword, mode)
    atomic_write_json(path, items)


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
