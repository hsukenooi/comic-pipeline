"""Tests for the BUI-427 matcher-bypassing remediation commands.

cmd_collection_remediate_delete / cmd_collection_remediate_set_copies locate
their target row by STABLE IDENTITY (gixen_item_id, or full_title +
release_date + source) — never via cmd_collection_check's masthead-alias /
X-Men-split / leading-article matcher, which is exactly what can't
disambiguate a volume-mis-filed row (BUI-424). Both reuse
CollectionCache.apply (flock + .bak rotation + audit trail).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from locg.collection_cache import CollectionCache
from locg.commands import (
    cmd_collection_remediate_delete,
    cmd_collection_remediate_set_copies,
)


def make_cache(tmp_path: Path) -> CollectionCache:
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    *,
    full_title: str = "Amazing Spider-Man #300",
    series_name: str = "Amazing Spider-Man",
    release_date: str | None = "1988-05-01",
    source: str | None = "agent_win",
    gixen_item_id: str | None = "99",
    in_collection: int = 1,
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
    }


def _seed(cache: CollectionCache, rows: list[dict[str, Any]]) -> None:
    """Seed comics AND mark the store as imported (last_full_import set) —
    the R11-style `not_imported` gate both remediation commands enforce."""

    def mutate(payload: dict[str, Any]) -> None:
        payload["comics"] = list(rows)
        payload["last_full_import"] = "2026-06-01T00:00:00.000000Z"
        payload["last_import_source"] = "seed.xlsx"

    cache.apply(mutate, command="seed")


def _rows(cache: CollectionCache) -> list[dict[str, Any]]:
    return cache.load().get("comics", [])


# ---------------------------------------------------------------------------
# cmd_collection_remediate_delete — identity resolution
# ---------------------------------------------------------------------------


def test_delete_by_gixen_item_id_targets_exact_row_not_fuzzy_twin(tmp_path):
    """Two rows share the SAME masthead-alias series+issue (Thor #127) but
    have distinct gixen_item_id — a check-based matcher could resolve either
    one. Deleting by gixen_item_id must hit ONLY the requested row."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="The Mighty Thor #127", series_name="The Mighty Thor (Vol. 3)",
             gixen_item_id="mighty-thor-127", release_date="2015-01-01"),
        _row(full_title="Thor #127", series_name="Thor (Vol. 1)",
             gixen_item_id="thor-127", release_date="1966-04-01"),
    ])

    result = cmd_collection_remediate_delete(gixen_item_id="mighty-thor-127", cache=cache)

    assert result["status"] == "ok"
    assert result["action"] == "removed"
    assert result["removed"]["gixen_item_id"] == "mighty-thor-127"

    remaining = _rows(cache)
    assert len(remaining) == 1
    assert remaining[0]["gixen_item_id"] == "thor-127"


def test_delete_by_full_title_release_date_source_hits_only_matching_twin(tmp_path):
    """BUI-424 duplicate-twin case: a buggy agent_win row and its clean
    locg_export re-resolution share full_title + release_date but differ in
    source. Deleting must target ONLY the specified source."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="Batman #1", release_date="1940-04-25", source="agent_win",
             gixen_item_id="win-1"),
        _row(full_title="Batman #1", release_date="1940-04-25", source="locg_export",
             gixen_item_id="export-1"),
    ])

    result = cmd_collection_remediate_delete(
        full_title="Batman #1", release_date="1940-04-25", source="agent_win", cache=cache,
    )

    assert result["status"] == "ok"
    assert result["removed"]["source"] == "agent_win"
    assert result["removed"]["gixen_item_id"] == "win-1"

    remaining = _rows(cache)
    assert len(remaining) == 1
    assert remaining[0]["source"] == "locg_export"


def test_delete_decrements_multi_copy_row_instead_of_removing(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="two-copies", in_collection=2)])

    result = cmd_collection_remediate_delete(gixen_item_id="two-copies", cache=cache)

    assert result["status"] == "ok"
    assert result["action"] == "decremented"
    assert result["remaining_copies"] == 1
    remaining = _rows(cache)
    assert len(remaining) == 1
    assert remaining[0]["in_collection"] == 1


def test_delete_not_found_is_a_clean_no_op(tmp_path):
    """A non-existent identity is an error, never a silent wrong-row delete."""
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])

    result = cmd_collection_remediate_delete(gixen_item_id="does-not-exist", cache=cache)

    assert result["status"] == "not_found"
    assert len(_rows(cache)) == 1  # untouched


def test_delete_ambiguous_when_multiple_rows_share_identity(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="X #1", release_date=None, source=None, gixen_item_id="a"),
        _row(full_title="X #1", release_date=None, source=None, gixen_item_id="b"),
    ])

    result = cmd_collection_remediate_delete(full_title="X #1", cache=cache)

    assert result["status"] == "ambiguous"
    assert result["count"] == 2
    assert len(_rows(cache)) == 2  # untouched — refused, not guessed


def test_delete_invalid_request_when_neither_identity_given(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row()])
    result = cmd_collection_remediate_delete(cache=cache)
    assert result["status"] == "invalid_request"


def test_delete_invalid_request_when_both_identities_given(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row()])
    result = cmd_collection_remediate_delete(
        gixen_item_id="99", full_title="Amazing Spider-Man #300", cache=cache,
    )
    assert result["status"] == "invalid_request"


def test_delete_not_imported_gate(tmp_path):
    cache = make_cache(tmp_path)  # never seeded — no last_full_import
    result = cmd_collection_remediate_delete(gixen_item_id="99", cache=cache)
    assert result["status"] == "not_imported"


def test_delete_dry_run_previews_without_mutating(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])
    before = cache.path.read_bytes()

    result = cmd_collection_remediate_delete(gixen_item_id="99", dry_run=True, cache=cache)

    assert result["status"] == "preview"
    assert result["action"] == "would_remove"
    assert result["row"]["gixen_item_id"] == "99"
    assert cache.path.read_bytes() == before
    assert len(_rows(cache)) == 1
    # No audit entry either — a preview is a pure read.
    assert not cache.audit_path.exists()


def test_delete_dry_run_previews_decrement_for_multi_copy(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=3)])

    result = cmd_collection_remediate_delete(gixen_item_id="99", dry_run=True, cache=cache)

    assert result["action"] == "would_decrement"
    assert result["remaining_copies"] == 2
    assert _rows(cache)[0]["in_collection"] == 3  # untouched


def test_delete_logs_to_audit_trail(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])

    cmd_collection_remediate_delete(gixen_item_id="99", cache=cache)

    lines = cache.audit_path.read_text().strip().splitlines()
    import json as _json
    records = [_json.loads(line) for line in lines]
    delete_records = [r for r in records if r["type"] == "collection_remediate_delete"]
    assert len(delete_records) == 1
    assert delete_records[0]["details"]["removed"]["gixen_item_id"] == "99"
    assert delete_records[0]["details"]["identity"]["gixen_item_id"] == "99"


def test_delete_self_verify_catches_row_vanishing_before_lock(tmp_path):
    """Simulates a concurrent writer removing the target row between the
    cheap pre-check read and cache.apply()'s locked re-resolve. The self-
    verify inside _mutate must catch the mismatch (1 candidate pre-lock, 0
    under the lock) and report not_found rather than silently no-op'ing a
    phantom success or touching an unrelated row."""
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="race-me")])

    class RacyCache(CollectionCache):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._raced = False

        def load(self):
            payload = super().load()
            if not self._raced:
                self._raced = True

                def _concurrent_removal(p):
                    p["comics"] = [
                        r for r in p["comics"] if r.get("gixen_item_id") != "race-me"
                    ]

                # A separate process's write, landing strictly between our
                # caller's pre-check load() and its later apply() call.
                self.apply(_concurrent_removal, command="race-simulation")
            return payload

    racy = RacyCache(path=cache.path, lock_path=cache.lock_path, audit_path=cache.audit_path)

    result = cmd_collection_remediate_delete(gixen_item_id="race-me", cache=racy)

    assert result["status"] == "not_found"
    # No collection_remediate_delete audit entry (the race-simulation's own
    # apply() doesn't append_audit at all, so the file may not even exist).
    if cache.audit_path.exists():
        import json as _json
        records = [_json.loads(line) for line in cache.audit_path.read_text().strip().splitlines()]
        assert not any(r["type"] == "collection_remediate_delete" for r in records)


# ---------------------------------------------------------------------------
# cmd_collection_remediate_set_copies
# ---------------------------------------------------------------------------


def test_set_copies_to_explicit_value(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=2, cache=cache)

    assert result["status"] == "ok"
    assert result["previous_in_collection"] == 1
    assert result["new_in_collection"] == 2
    assert _rows(cache)[0]["in_collection"] == 2


def test_set_copies_by_full_title_release_date_source_twin(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="Batman #1", release_date="1940-04-25", source="agent_win",
             gixen_item_id="win-1", in_collection=1),
        _row(full_title="Batman #1", release_date="1940-04-25", source="locg_export",
             gixen_item_id="export-1", in_collection=1),
    ])

    result = cmd_collection_remediate_set_copies(
        full_title="Batman #1", release_date="1940-04-25", source="agent_win",
        in_collection=5, cache=cache,
    )

    assert result["status"] == "ok"
    rows_by_id = {r["gixen_item_id"]: r for r in _rows(cache)}
    assert rows_by_id["win-1"]["in_collection"] == 5
    assert rows_by_id["export-1"]["in_collection"] == 1  # untouched


def test_set_copies_zero_does_not_delete_the_row(tmp_path):
    """Unlike remediate-delete, in_collection=0 is a valid tracked-but-not-
    owned state — the row must survive."""
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=0, cache=cache)

    assert result["status"] == "ok"
    assert result["new_in_collection"] == 0
    remaining = _rows(cache)
    assert len(remaining) == 1
    assert remaining[0]["in_collection"] == 0


def test_set_copies_with_positive_delta(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", delta=2, cache=cache)

    assert result["status"] == "ok"
    assert result["previous_in_collection"] == 1
    assert result["new_in_collection"] == 3


def test_set_copies_with_negative_delta(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=3)])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", delta=-2, cache=cache)

    assert result["status"] == "ok"
    assert result["new_in_collection"] == 1


def test_set_copies_negative_delta_refused_not_clamped(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", delta=-5, cache=cache)

    assert result["status"] == "invalid_request"
    assert _rows(cache)[0]["in_collection"] == 1  # untouched


def test_set_copies_negative_absolute_value_rejected(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=-1, cache=cache)

    assert result["status"] == "invalid_request"


def test_set_copies_invalid_when_both_in_collection_and_delta_given(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])

    result = cmd_collection_remediate_set_copies(
        gixen_item_id="99", in_collection=2, delta=1, cache=cache,
    )
    assert result["status"] == "invalid_request"


def test_set_copies_invalid_when_neither_in_collection_nor_delta_given(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", cache=cache)
    assert result["status"] == "invalid_request"


def test_set_copies_dry_run_previews_without_mutating(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])
    before = cache.path.read_bytes()

    result = cmd_collection_remediate_set_copies(
        gixen_item_id="99", in_collection=9, dry_run=True, cache=cache,
    )

    assert result["status"] == "preview"
    assert result["current_in_collection"] == 1
    assert result["new_in_collection"] == 9
    assert cache.path.read_bytes() == before
    assert _rows(cache)[0]["in_collection"] == 1
    assert not cache.audit_path.exists()


def test_set_copies_not_found_is_a_clean_no_op(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])

    result = cmd_collection_remediate_set_copies(
        gixen_item_id="does-not-exist", in_collection=5, cache=cache,
    )

    assert result["status"] == "not_found"
    assert _rows(cache)[0]["in_collection"] == 1  # untouched


def test_set_copies_not_imported_gate(tmp_path):
    cache = make_cache(tmp_path)
    result = cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=1, cache=cache)
    assert result["status"] == "not_imported"


def test_set_copies_logs_to_audit_trail(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])

    cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=4, cache=cache)

    import json as _json
    lines = cache.audit_path.read_text().strip().splitlines()
    records = [_json.loads(line) for line in lines]
    set_records = [r for r in records if r["type"] == "collection_remediate_set_copies"]
    assert len(set_records) == 1
    assert set_records[0]["details"]["previous_in_collection"] == 1
    assert set_records[0]["details"]["new_in_collection"] == 4


def test_set_copies_self_verify_catches_race_going_negative(tmp_path):
    """A concurrent writer decrements the row to 0 between the pre-check and
    the locked re-resolve; a delta of -1 (valid pre-race) would now go
    negative. The in-lock self-verify must refuse rather than clamp/guess."""
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="race-me", in_collection=1)])

    class RacyCache(CollectionCache):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._raced = False

        def load(self):
            payload = super().load()
            if not self._raced:
                self._raced = True

                def _concurrent_decrement(p):
                    for r in p["comics"]:
                        if r.get("gixen_item_id") == "race-me":
                            r["in_collection"] = 0

                self.apply(_concurrent_decrement, command="race-simulation")
            return payload

    racy = RacyCache(path=cache.path, lock_path=cache.lock_path, audit_path=cache.audit_path)

    result = cmd_collection_remediate_set_copies(gixen_item_id="race-me", delta=-1, cache=racy)

    assert result["status"] == "invalid_request"
    assert _rows(cache)[0]["in_collection"] == 0  # the race's write stands; ours refused
