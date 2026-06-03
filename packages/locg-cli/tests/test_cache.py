"""Tests for locg.cache (persistent ID cache)."""
from __future__ import annotations

import json

import pytest

from locg.cache import IDCache, cache_path, make_key


# --- key normalization ----


def test_make_key_basic():
    assert make_key("Uncanny X-Men", "185") == "uncanny-x-men:185"


def test_make_key_strips_the_prefix():
    assert make_key("The Amazing Spider-Man", "142") == "amazing-spider-man:142"


def test_make_key_collapses_whitespace_and_punctuation():
    assert make_key("Batman: The Long Halloween", "9") == "batman-the-long-halloween:9"


def test_make_key_with_variant():
    assert make_key("Uncanny X-Men", "179", "Newsstand") == "uncanny-x-men:179:newsstand"


def test_make_key_variant_normalized_too():
    # "Newsstand Edition" and "newsstand-edition" collapse to the same key.
    assert make_key("Uncanny X-Men", "179", "Newsstand Edition") == \
        make_key("Uncanny X-Men", "179", "newsstand-edition")


def test_make_key_issue_number_lowercased():
    # "1AU" → "1au" (variant suffixes sometimes appear cased)
    assert make_key("X-Men", "1AU") == "x-men:1au"


# --- IDCache I/O ----


def test_cache_get_returns_none_when_file_missing(tmp_path):
    c = IDCache(path=tmp_path / "ids.json")
    assert c.get("anything") is None


def test_cache_set_and_get_roundtrip(tmp_path):
    p = tmp_path / "ids.json"
    c = IDCache(path=p)
    c.set("uncanny-x-men:185", {"locg_id": 1081721, "series_id": 108806})

    assert p.exists()
    # New instance reads from disk
    c2 = IDCache(path=p)
    entry = c2.get("uncanny-x-men:185")
    assert entry is not None
    assert entry["locg_id"] == 1081721
    assert entry["series_id"] == 108806
    assert "cached_at" in entry  # timestamp added automatically


def test_cache_set_preserves_user_cached_at(tmp_path):
    """If the caller supplies cached_at, don't overwrite it."""
    c = IDCache(path=tmp_path / "ids.json")
    c.set("k", {"locg_id": 1, "cached_at": "2020-01-01T00:00:00Z"})
    assert c.get("k")["cached_at"] == "2020-01-01T00:00:00Z"


def test_cache_clear_returns_count_and_empties_file(tmp_path):
    c = IDCache(path=tmp_path / "ids.json")
    c.set("a:1", {"locg_id": 1})
    c.set("b:1", {"locg_id": 2})
    assert c.clear() == 2
    assert c.get("a:1") is None
    # File still exists, but entries dict is empty
    with open(tmp_path / "ids.json") as f:
        assert json.load(f)["entries"] == {}


def test_cache_clear_on_empty_returns_zero(tmp_path):
    c = IDCache(path=tmp_path / "ids.json")
    assert c.clear() == 0


def test_cache_stats_reports_entries_and_size(tmp_path):
    p = tmp_path / "ids.json"
    c = IDCache(path=p)
    c.set("a:1", {"locg_id": 1})
    c.set("b:2", {"locg_id": 2})

    stats = c.stats()
    assert stats["entries"] == 2
    assert stats["exists"] is True
    assert stats["size_bytes"] > 0
    assert stats["version"] == 1
    assert stats["path"] == str(p)


def test_cache_stats_when_file_missing(tmp_path):
    c = IDCache(path=tmp_path / "absent.json")
    stats = c.stats()
    assert stats["entries"] == 0
    assert stats["exists"] is False
    assert stats["size_bytes"] == 0


def test_cache_handles_corrupt_file(tmp_path):
    """A garbled cache file must not crash; treat as empty and rewrite cleanly."""
    p = tmp_path / "ids.json"
    p.write_text("not valid json {{{")
    c = IDCache(path=p)
    assert c.get("anything") is None
    c.set("k", {"locg_id": 1})
    # The new contents are valid JSON
    with open(p) as f:
        data = json.load(f)
    assert "k" in data["entries"]


def test_cache_handles_wrong_shape(tmp_path):
    """If the file is valid JSON but doesn't have an 'entries' key, treat as empty."""
    p = tmp_path / "ids.json"
    p.write_text(json.dumps({"unrelated": "data"}))
    c = IDCache(path=p)
    assert c.get("anything") is None


def test_cache_atomic_write_no_orphan_tmpfile_on_success(tmp_path):
    """Successful writes leave only the target file, no temp residue."""
    c = IDCache(path=tmp_path / "ids.json")
    c.set("k", {"locg_id": 1})
    files = list(tmp_path.iterdir())
    # Only the target file should exist
    assert files == [tmp_path / "ids.json"]


def test_cache_path_respects_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path))
    p = cache_path()
    assert p == tmp_path / "ids.json"
