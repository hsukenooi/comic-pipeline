"""Tests for ebay_search_cache.py."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ebay_search_cache as cache


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_cache_dir(tmp_path, monkeypatch):
    """Point CACHE_DIR at a per-test temp directory."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "searches")


# ─── cache_key ────────────────────────────────────────────────────────────────

def test_cache_key_is_stable():
    """Same keyword always produces the same key."""
    assert cache.cache_key("Amazing Spider-Man #1") == cache.cache_key("Amazing Spider-Man #1")


def test_cache_key_case_insensitive():
    """Keyword comparison is case-insensitive."""
    assert cache.cache_key("amazing spider-man #1") == cache.cache_key("AMAZING SPIDER-MAN #1")


def test_cache_key_strips_whitespace():
    """Leading/trailing whitespace is ignored."""
    assert cache.cache_key("  Amazing Spider-Man #1  ") == cache.cache_key("Amazing Spider-Man #1")


# ─── cache_key mode namespacing (BUI-225) ─────────────────────────────────────

def test_cache_key_different_modes_produce_different_keys():
    """Same keyword with different modes must produce different keys."""
    kw = "Amazing Spider-Man #129"
    assert cache.cache_key(kw, "auction") != cache.cache_key(kw, "all")
    assert cache.cache_key(kw, "auction") != cache.cache_key(kw, "bin")
    assert cache.cache_key(kw, "bin") != cache.cache_key(kw, "all")


def test_cache_key_mode_stable():
    """Same keyword + same mode always produces the same key."""
    kw = "X-Men #94"
    assert cache.cache_key(kw, "auction") == cache.cache_key(kw, "auction")


def test_cache_path_different_modes_produce_different_paths():
    """Same keyword with different modes must resolve to different file paths."""
    kw = "Fantastic Four #48"
    assert cache.cache_path(kw, "auction") != cache.cache_path(kw, "all")


def test_no_cross_contamination_auction_put_not_visible_to_all_get():
    """put(..., mode='auction') must NOT be visible to get(..., mode='all')."""
    keyword = "Hulk #181"
    items = [{"title": "Hulk #181 VG"}]
    cache.put(keyword, items, mode="auction")
    assert cache.get(keyword, mode="all") is None


def test_no_cross_contamination_all_put_not_visible_to_auction_get():
    """put(..., mode='all') must NOT be visible to get(..., mode='auction')."""
    keyword = "Daredevil #168"
    items = [{"title": "Daredevil #168 FN"}]
    cache.put(keyword, items, mode="all")
    assert cache.get(keyword, mode="auction") is None


def test_round_trip_with_explicit_mode():
    """put + get with the same explicit mode returns the stored items."""
    keyword = "Iron Fist #14"
    items = [{"title": "Iron Fist #14 NM"}]
    cache.put(keyword, items, mode="auction")
    assert cache.get(keyword, mode="auction") == items


def test_default_mode_backward_compatible():
    """Omitting mode uses the 'all' namespace; existing code paths keep working."""
    keyword = "Captain America #100"
    items = [{"title": "Cap #100"}]
    # put without mode → "all" namespace
    cache.put(keyword, items)
    # get without mode → "all" namespace → should hit
    assert cache.get(keyword) == items
    # get with explicit "all" → same namespace → should also hit
    assert cache.get(keyword, mode="all") == items


# ─── put / get round-trip ─────────────────────────────────────────────────────

def test_put_then_get_returns_same_items():
    """put followed by get returns the identical list."""
    keyword = "X-Men #94 CGC"
    items = [{"title": "X-Men #94", "price": 150.0, "end_date_iso": "2099-01-01T00:00:00Z"}]
    cache.put(keyword, items)
    result = cache.get(keyword)
    assert result == items


def test_get_returns_none_on_cache_miss():
    """get returns None when no cache file exists."""
    assert cache.get("Fantastic Four #1") is None


def test_put_writes_atomically_and_leaves_no_tmp_file():
    """BUI-333: put() routes through the shared ebay_fetch._atomic_write_json()
    helper rather than a hand-rolled tmp→rename copy."""
    keyword = "Amazing Spider-Man #300"
    cache.put(keyword, [{"title": "ASM #300"}])
    path = cache.cache_path(keyword)
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_get_returns_none_when_expired(tmp_path):
    """get returns None when the cached file is older than ttl_sec."""
    keyword = "Daredevil #1"
    items = [{"title": "Daredevil #1"}]
    cache.put(keyword, items)

    # Backdate the mtime by 10 seconds into the past
    path = cache.cache_path(keyword)
    old_mtime = time.time() - 10
    os.utime(path, (old_mtime, old_mtime))

    # A ttl of 5 seconds should see the file as expired
    assert cache.get(keyword, ttl_sec=5) is None


def test_get_returns_none_on_corrupt_file():
    """get returns None (does not raise) when the cache file contains invalid JSON."""
    keyword = "Hulk #181"
    path = cache.cache_path(keyword)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json !!!}")

    assert cache.get(keyword) is None


def test_get_returns_none_when_file_is_not_a_list():
    """get returns None when the JSON is valid but not a list (wrong shape)."""
    keyword = "Iron Man #128"
    path = cache.cache_path(keyword)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": "not a list"}))

    assert cache.get(keyword) is None


# ─── filter_active ────────────────────────────────────────────────────────────

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)

FUTURE = "2026-07-01T00:00:00Z"
PAST   = "2026-06-25T00:00:00Z"


def test_filter_active_drops_past_listing():
    items = [{"title": "Past auction", "end_date_iso": PAST}]
    assert cache.filter_active(items, now=NOW) == []


def test_filter_active_keeps_future_listing():
    item = {"title": "Future auction", "end_date_iso": FUTURE}
    assert cache.filter_active([item], now=NOW) == [item]


def test_filter_active_keeps_missing_end_date():
    """Fail-open: item with no end_date_iso key is kept."""
    item = {"title": "No date"}
    assert cache.filter_active([item], now=NOW) == [item]


def test_filter_active_keeps_none_end_date():
    """Fail-open: item with end_date_iso=None is kept."""
    item = {"title": "None date", "end_date_iso": None}
    assert cache.filter_active([item], now=NOW) == [item]


def test_filter_active_keeps_garbage_end_date():
    """Fail-open: item with unparseable end_date_iso is kept."""
    item = {"title": "Garbage date", "end_date_iso": "not-a-date"}
    assert cache.filter_active([item], now=NOW) == [item]


def test_filter_active_mixed_list():
    """Mixed list: past dropped, future kept, missing kept."""
    past_item   = {"title": "Past",    "end_date_iso": PAST}
    future_item = {"title": "Future",  "end_date_iso": FUTURE}
    no_date     = {"title": "No date"}

    result = cache.filter_active([past_item, future_item, no_date], now=NOW)
    assert result == [future_item, no_date]


def test_filter_active_handles_offset_format():
    """ISO offset (+00:00) is handled in addition to Z suffix."""
    item = {"title": "Offset", "end_date_iso": "2026-07-01T00:00:00+00:00"}
    assert cache.filter_active([item], now=NOW) == [item]


def test_filter_active_default_now_is_utc(monkeypatch):
    """Without injected now, filter_active uses current UTC time (smoke test)."""
    # A listing ending far in the future should survive against real wall-clock now
    item = {"title": "Far future", "end_date_iso": "2099-12-31T23:59:59Z"}
    assert cache.filter_active([item]) == [item]
