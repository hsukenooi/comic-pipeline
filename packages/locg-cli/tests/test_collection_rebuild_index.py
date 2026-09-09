"""Tests for BUI-771's `cmd_collection_rebuild_index`.

`series_name_index` is only rebuilt by
`locg.collection_cache.rebuild_series_name_index`, which normally runs at the
tail of `collection import`. A normalizer change (BUI-546's punctuation fold)
leaves the on-disk keys stale until the next import — but that function
builds the index purely from `source='locg_export'` rows already sitting in
`payload["comics"]` (R61), so the rebuild needs no fresh LOCG export. This
command reruns that derivation in place, through `CollectionCache.apply()`'s
exclusive lock, and must touch ONLY `series_name_index`.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from locg.collection_cache import CollectionCache, _normalize_series_key
from locg.commands import cmd_collection_rebuild_index


def make_cache(tmp_path: Path) -> CollectionCache:
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    *,
    full_title: str = "2001: A Space Odyssey (1976)",
    series_name: str = "2001: A Space Odyssey (1976)",
    release_date: str | None = "1976-01-01",
    source: str | None = "locg_export",
    gixen_item_id: str | None = None,
    in_collection: int = 1,
    quarantined: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "publisher_name": "Marvel Comics",
        "series_name": series_name,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": in_collection,
        "in_wish_list": 0,
        "marked_read": 0,
        "my_rating": None,
        "media_format": None,
        "price_paid": None,
        "date_purchased": None,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": None,
        "signature": 0,
        "slabbing": 0,
        "grading": None,
        "grading_company": None,
        "local_added_at": None,
        "local_added_seq": None,
        "pushed_to_locg_at": None,
        "last_seen_in_export_at": None,
        "source": source,
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": None,
        "gixen_item_id": gixen_item_id,
        "previous_full_title": None,
        "quarantined": quarantined,
    }


def _seed(
    cache: CollectionCache,
    rows: list[dict[str, Any]],
    *,
    stale_index: dict[str, str] | None = None,
) -> None:
    """Seed comics, mark the store imported, and optionally install a
    pre-BUI-546-shaped (stale) series_name_index."""

    def mutate(payload: dict[str, Any]) -> None:
        payload["comics"] = list(rows)
        payload["last_full_import"] = "2026-06-01T00:00:00.000000Z"
        payload["last_import_source"] = "seed.xlsx"
        if stale_index is not None:
            payload["series_name_index"] = dict(stale_index)

    cache.apply(mutate, command="seed")


# ---------------------------------------------------------------------------
# Core rebuild behavior
# ---------------------------------------------------------------------------


def test_rebuild_clears_stale_keys_built_by_the_old_normalizer(tmp_path):
    """BUI-546's exact live case: a key built pre-punctuation-fold no longer
    matches _normalize_series_key. The rebuild must replace it with a key the
    CURRENT normalizer produces."""
    cache = make_cache(tmp_path)
    row = _row(series_name="2001: A Space Odyssey (1976)")
    # Pre-BUI-546 spelling: colon kept, not folded to a space.
    _seed(cache, [row], stale_index={
        "2001: a space odyssey": "2001: A Space Odyssey (1976)",
    })

    result = cmd_collection_rebuild_index(cache=cache)

    assert result["status"] == "ok"
    assert result["total_before"] == 1
    assert result["stale_before"] == 1
    assert result["total_after"] == 1
    # A renamed key is 1 removal + 1 addition.
    assert result["changed"] == 2

    new_index = cache.load().get("series_name_index")
    current_key = _normalize_series_key("2001: A Space Odyssey (1976)")
    assert new_index == {current_key: "2001: A Space Odyssey (1976)"}
    assert "2001: a space odyssey" not in new_index


def test_rebuild_is_idempotent(tmp_path):
    """Running it twice in a row must leave the second run reporting zero
    changes — the exact idempotency the ticket requires."""
    cache = make_cache(tmp_path)
    row = _row(series_name="2001: A Space Odyssey (1976)")
    _seed(cache, [row], stale_index={
        "2001: a space odyssey": "2001: A Space Odyssey (1976)",
    })

    first = cmd_collection_rebuild_index(cache=cache)
    assert first["status"] == "ok"
    assert first["changed"] == 2

    second = cmd_collection_rebuild_index(cache=cache)
    assert second["status"] == "ok"
    assert second["stale_before"] == 0
    assert second["changed"] == 0
    assert second["total_before"] == second["total_after"] == first["total_after"]


def test_rebuild_dry_run_reports_but_does_not_mutate(tmp_path):
    cache = make_cache(tmp_path)
    row = _row(series_name="2001: A Space Odyssey (1976)")
    stale_index = {"2001: a space odyssey": "2001: A Space Odyssey (1976)"}
    _seed(cache, [row], stale_index=stale_index)

    result = cmd_collection_rebuild_index(cache=cache, dry_run=True)

    assert result["status"] == "preview"
    assert result["stale_before"] == 1
    assert result["changed"] == 2

    # Nothing was written — the on-disk index is exactly what was seeded.
    assert cache.load().get("series_name_index") == stale_index


def test_rebuild_on_already_fresh_index_reports_zero_stale_and_zero_changed(tmp_path):
    cache = make_cache(tmp_path)
    series_name = "2001: A Space Odyssey (1976)"
    row = _row(series_name=series_name)
    fresh_index = {_normalize_series_key(series_name): series_name}
    _seed(cache, [row], stale_index=fresh_index)

    result = cmd_collection_rebuild_index(cache=cache)

    assert result["status"] == "ok"
    assert result["stale_before"] == 0
    assert result["changed"] == 0
    assert cache.load().get("series_name_index") == fresh_index


def test_rebuild_touches_only_series_name_index(tmp_path):
    """Requirement: comics rows, copy counts, and quarantine state must pass
    through untouched — this asserts the whole payload except
    series_name_index (and apply()'s own last_writer/migration_in_progress
    bookkeeping, which every mutator sets) is byte-for-byte identical."""
    cache = make_cache(tmp_path)
    rows = [
        _row(full_title="2001: A Space Odyssey (1976)", series_name="2001: A Space Odyssey (1976)",
             gixen_item_id="a1", in_collection=2),
        _row(full_title="Some Quarantined Book #1", series_name="Some Quarantined Book",
             gixen_item_id="a2", in_collection=1, quarantined={
                 "at": "2026-06-01T00:00:00Z", "by": "tester",
                 "reason": "test fixture", "ticket": "BUI-771",
             }),
    ]
    _seed(cache, rows, stale_index={
        "2001: a space odyssey": "2001: A Space Odyssey (1976)",
    })

    before = cache.load()
    before_comics = copy.deepcopy(before["comics"])
    before_last_full_import = before["last_full_import"]
    before_last_import_source = before["last_import_source"]

    result = cmd_collection_rebuild_index(cache=cache)
    assert result["status"] == "ok"

    after = cache.load()
    assert after["comics"] == before_comics
    assert after["last_full_import"] == before_last_full_import
    assert after["last_import_source"] == before_last_import_source
    # Copy counts and quarantine markers specifically survive untouched.
    assert after["comics"][0]["in_collection"] == 2
    assert after["comics"][1]["quarantined"]["ticket"] == "BUI-771"


def test_rebuild_not_imported(tmp_path):
    cache = make_cache(tmp_path)
    # No _seed call — last_full_import stays None.
    result = cmd_collection_rebuild_index(cache=cache)
    assert result["status"] == "not_imported"


# ---------------------------------------------------------------------------
# BUI-489-style wrong-store guard (same pattern as remediate-set-copies)
# ---------------------------------------------------------------------------


def _no_default_collection_store(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("BUI-771 guard regressed: the default store was constructed")

    monkeypatch.setattr("locg.commands.CollectionCache", _boom)


def test_rebuild_no_cache_no_env_is_refused(monkeypatch):
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_rebuild_index()

    assert result["status"] == "explicit_store_required"
    assert "LOCG_DATA_DIR" in result["error"]


def test_rebuild_guard_fires_even_for_dry_run(monkeypatch):
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_rebuild_index(dry_run=True)

    assert result["status"] == "explicit_store_required"


def test_rebuild_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    """The SERVER-shaped call: no cache= is passed, so the store is resolved
    SOLELY from LOCG_DATA_DIR — exactly what routes._ensure_collection_store()
    guarantees before every collection call."""
    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))
    cache = CollectionCache(
        path=store / "collection.json",
        lock_path=store / "collection.lock",
        audit_path=store / "import-history.jsonl",
    )
    series_name = "2001: A Space Odyssey (1976)"
    _seed(cache, [_row(series_name=series_name)], stale_index={
        "2001: a space odyssey": series_name,
    })

    result = cmd_collection_rebuild_index()

    assert result["status"] == "ok"
    assert result["changed"] == 2
