"""BUI-653: the masthead aliases move from code (`_MASTHEAD_ALIAS_PAIRS`, a
five-entry tuple in `collection_cache.py`) into data (`alias` entries in
`data/authority.json`, loaded via `locg.authority`).

This is a **behavior-preserving migration** — the proof lives in
`test_collection_cache.py`/`test_collection_commands.py`/`test_collection_io.py`
passing UNMODIFIED (see those files' BUI-197/BUI-200 alias, annual, and split
tests). This file covers the two things that proof can't: that the dead
Python constant is actually gone (so a future reader can't add a sixth pair
to it and have it silently do nothing), and that the whole point of the
migration — a data-only edit widening the matcher — actually works.
"""
from __future__ import annotations

from locg import authority, collection_cache


def test_masthead_alias_pairs_tuple_no_longer_exists():
    """The five pairs are `alias` entries in `data/authority.json` now (see
    `test_authority.py::test_packaged_authority_json_has_the_five_migrated_masthead_aliases`).
    If a future reader added a sixth pair to `_MASTHEAD_ALIAS_PAIRS`, nothing
    would read it — this asserts there is no such constant left to add to."""
    assert not hasattr(collection_cache, "_MASTHEAD_ALIAS_PAIRS")


def test_collection_cache_local_alias_group_builder_no_longer_exists():
    """`collection_cache.py` used to have its own `_build_alias_groups` — the
    same symmetric/transitive BFS closure `locg.authority._build_alias_groups`
    implements. The migration deletes the local copy and routes
    `_ALIAS_GROUPS` through the loader instead of keeping both (the ticket's
    own resolution of the name collision between the two functions)."""
    assert not hasattr(collection_cache, "_build_alias_groups")


def test_alias_groups_now_sourced_from_the_authority_loader():
    """`_ALIAS_GROUPS` is built by `authority.build_alias_table`, not a
    hand-rolled closure over a hardcoded tuple."""
    rebuilt = authority.build_alias_table(collection_cache._normalize_series_key)
    assert collection_cache._ALIAS_GROUPS == dict(rebuilt.groups)


def test_fixture_authority_entry_widens_owned_match_keys_with_no_code_change(
    monkeypatch,
):
    """The payoff: an entry that exists ONLY in a fixture table (never in the
    real packaged `data/authority.json`, never in any pair `collection_cache.py`
    used to hardcode) widens `owned_match_keys` the moment it is loaded —
    demonstrating a sixth alias is now a data edit, not a code change.

    Mechanics: `authority.build_alias_table` always reads the module-global
    `authority._ALIAS_ENTRIES` (populated once, at `locg.authority` import,
    from the packaged file). We monkeypatch that global to a fixture table's
    parsed entries — standing in for "someone merged a reviewed PR to
    `data/authority.json`" — then rebuild `collection_cache._ALIAS_GROUPS`
    the exact same way `collection_cache.py`'s own module body does
    (`authority.build_alias_table(_normalize_series_key).groups`), and call
    the real, unmodified `owned_match_keys`.
    """
    query_series = "Zzyzx Ranger"
    owned_series = "Last Ranger of Zzyzx"
    query_key = collection_cache._normalize_series_key(query_series)
    owned_key = collection_cache._normalize_series_key(owned_series)

    # Sanity: before the fixture entry is loaded, these two names are NOT
    # equivalent — the widening we're about to prove doesn't already exist.
    assert owned_key not in collection_cache.owned_match_keys(query_series, "1")

    fixture_table = {
        "version": 1,
        "entries": [
            {
                "kind": "alias",
                "names": [query_series, owned_series],
                "evidence": "test fixture — BUI-653 payoff demonstration",
                "added": "2026-08-03",
            }
        ],
    }
    fixture_aliases, _ = authority.parse_table(fixture_table)
    monkeypatch.setattr(authority, "_ALIAS_ENTRIES", fixture_aliases)

    rebuilt_groups = authority.build_alias_table(
        collection_cache._normalize_series_key
    ).groups
    monkeypatch.setattr(collection_cache, "_ALIAS_GROUPS", rebuilt_groups)

    widened = collection_cache.owned_match_keys(query_series, "1")
    assert owned_key in widened
    assert query_key in widened
