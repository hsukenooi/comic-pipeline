"""Persistent on-disk cache for resolved LOCG comic IDs.

The cache is a single JSON file at ``<repo>/data/locg/ids.json`` when running
from an editable repo checkout (``~/.cache/locg/ids.json`` as a fallback, or
``$LOCG_DATA_DIR/ids.json`` when that override is set — see
``config._cache_dir``). It maps a normalized ``series:issue[:variant]`` key to a
small entry containing the resolved ``locg_id``, ``locg_variant_id``,
``series_id``, and canonical names.

LOCG comic IDs are stable, so entries never auto-expire — call
``cache clear`` if a stale mapping ever needs to go.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ._atomic import atomic_write_json
from .config import _cache_dir

logger = logging.getLogger("locg")

CACHE_VERSION = 1


def cache_path() -> Path:
    """Path to the JSON cache file."""
    return _cache_dir() / "ids.json"


def ensure_cache_dir() -> Path:
    d = _cache_dir()
    if not d.exists():
        d.mkdir(parents=True)
        d.chmod(stat.S_IRWXU)  # 700
    return d


# Slugification: lowercase, drop "the " prefix, strip punctuation, collapse
# whitespace into single hyphens. Stable across small variations in user
# input so equivalent specs hit the same cache entry.
_SLUG_PUNCT_RE = re.compile(r"[^\w\s-]+")
_SLUG_WS_RE = re.compile(r"[\s_-]+")


def _slugify(value: str) -> str:
    s = (value or "").strip().lower()
    if s.startswith("the "):
        s = s[4:]
    s = _SLUG_PUNCT_RE.sub("", s)
    s = _SLUG_WS_RE.sub("-", s).strip("-")
    return s


def make_key(series_name: str, issue_number: str, variant: Optional[str] = None) -> str:
    """Build a stable cache key from a (series, issue[, variant]) triple."""
    parts = [_slugify(series_name), str(issue_number).strip().lower()]
    if variant:
        parts.append(_slugify(variant))
    return ":".join(parts)


def _empty_payload() -> dict[str, Any]:
    return {"version": CACHE_VERSION, "entries": {}}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IDCache:
    """Read-through / write-through JSON cache for resolved LOCG IDs.

    Loads the entire file into memory on first access (~50 bytes per
    entry, so even 10k entries is well under 1 MB). Writes are atomic
    via temp-file + rename.

    Thread-/concurrency-safety: not designed for concurrent writers.
    Single-user CLI assumption.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or cache_path()
        self._data: Optional[dict[str, Any]] = None

    # ----- I/O ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            self._data = _empty_payload()
            return self._data
        try:
            with open(self.path) as f:
                payload = json.load(f)
            if not isinstance(payload, dict) or "entries" not in payload:
                logger.warning("Cache file %s is malformed; ignoring contents", self.path)
                payload = _empty_payload()
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Cache file %s unreadable (%s); starting fresh", self.path, e)
            payload = _empty_payload()
        self._data = payload
        return self._data

    def _save(self) -> None:
        if self._data is None:
            return
        ensure_cache_dir()
        # Atomic write: temp file in same dir, then rename.
        atomic_write_json(
            self.path,
            self._data,
            mode=stat.S_IRUSR | stat.S_IWUSR,  # 600
            compact=True,
            tmp_prefix=".ids-",
        )

    # ----- Public API -----------------------------------------------------

    def get(self, key: str) -> Optional[dict[str, Any]]:
        return self._load()["entries"].get(key)

    def set(self, key: str, entry: dict[str, Any]) -> None:
        """Insert or update ``entry`` under ``key`` and persist immediately."""
        data = self._load()
        stored = dict(entry)
        stored.setdefault("cached_at", _utcnow_iso())
        data["entries"][key] = stored
        self._save()

    def clear(self) -> int:
        """Delete all entries; return how many were removed."""
        data = self._load()
        count = len(data["entries"])
        data["entries"] = {}
        self._save()
        return count

    def stats(self) -> dict[str, Any]:
        data = self._load()
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "version": data.get("version"),
            "entries": len(data.get("entries", {})),
            "size_bytes": size,
        }
