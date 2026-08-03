"""Tests for the authority table schema, loader, and validation (BUI-652).

Two concerns run through this file, mirroring the two halves of validation
`authority.py` itself documents:

* Structural validation (`parse_table`) — normalizer-independent: kind,
  version, shape, required fields. Every rule gets a fixture that violates
  ONLY that rule (self-test discipline, the `scripts/solutions-lint
  --self-test` precedent), so no rule can silently go dead.
* Derived-key validation (`_build_alias_groups` / `_build_relabel_map`) —
  normalizer-dependent: same-key aliases, no-op relabels, empty derived
  keys. Exercised with stub normalizers (never `collection_cache`'s real
  ones — this module must not import that one; see the module docstring).

Wiring `authority.py` into `owned_match_keys`/`identity_series_key` is
BUI-653/BUI-654; nothing here touches `collection_cache.py`.
"""
from __future__ import annotations

import json

import pytest

from locg import authority


def _entry(kind: str, **fields) -> dict:
    base = {"kind": kind, "evidence": "issue #1234", "added": "2026-08-03"}
    base.update(fields)
    return base


def _alias(a: str, b: str, **overrides) -> dict:
    return _entry("alias", names=[a, b], **overrides)


def _relabel(a: str, b: str, **overrides) -> dict:
    return _entry("relabel", **{"from": a, "to": b}, **overrides)


def _table(*entries) -> dict:
    return {"version": 1, "entries": list(entries)}


# ---------------------------------------------------------------------------
# The packaged file itself
# ---------------------------------------------------------------------------


def test_packaged_authority_json_ships_empty():
    """v1 ships `{"version": 1, "entries": []}` — the five masthead aliases
    are BUI-653's migration, not this ticket's."""
    from importlib import resources

    text = (resources.files("locg") / "data" / "authority.json").read_text(
        encoding="utf-8"
    )
    raw = json.loads(text)
    assert raw == {"version": 1, "entries": []}


def test_module_import_parses_the_packaged_file_without_raising():
    # If this collection succeeded, the module-level `parse_table(...)` call
    # in authority.py already ran; assert its result directly too.
    assert authority._ALIAS_ENTRIES == ()
    assert authority._RELABEL_ENTRIES == ()


def test_build_tables_from_the_packaged_empty_file():
    alias_table = authority.build_alias_table(str.lower)
    relabel_table = authority.build_relabel_table(str.lower)
    assert alias_table.equivalents("anything") == frozenset({"anything"})
    assert relabel_table.relabel("anything") == "anything"


# ---------------------------------------------------------------------------
# Structural validation (parse_table) — one fixture per rule
# ---------------------------------------------------------------------------


def test_parse_table_accepts_a_well_formed_mixed_table():
    raw = _table(
        _alias("Mighty Thor", "Thor"),
        _relabel("Absolute Martian Manhunter (2025)", "Absolute Martian Manhunter (2025 - 2026)"),
    )
    aliases, relabels = authority.parse_table(raw)
    assert len(aliases) == 1
    assert len(relabels) == 1
    assert aliases[0].names == ("Mighty Thor", "Thor")
    assert relabels[0].from_name == "Absolute Martian Manhunter (2025)"
    assert relabels[0].to_name == "Absolute Martian Manhunter (2025 - 2026)"


def test_parse_table_rejects_unknown_version():
    with pytest.raises(authority.AuthorityTableError, match="version"):
        authority.parse_table({"version": 2, "entries": []})


def test_parse_table_rejects_non_dict_root():
    with pytest.raises(authority.AuthorityTableError):
        authority.parse_table(["not", "a", "dict"])


def test_parse_table_rejects_entries_not_a_list():
    with pytest.raises(authority.AuthorityTableError, match="entries"):
        authority.parse_table({"version": 1, "entries": "nope"})


def test_parse_table_rejects_unknown_kind():
    raw = _table(_entry("masthead-swap", names=["A", "B"]))
    with pytest.raises(authority.AuthorityTableError, match="kind"):
        authority.parse_table(raw)


def test_parse_table_rejects_missing_evidence():
    raw = _table({"kind": "alias", "names": ["A", "B"], "added": "2026-08-03"})
    with pytest.raises(authority.AuthorityTableError, match="evidence"):
        authority.parse_table(raw)


def test_parse_table_rejects_missing_added():
    raw = _table({"kind": "alias", "names": ["A", "B"], "evidence": "e"})
    with pytest.raises(authority.AuthorityTableError, match="added"):
        authority.parse_table(raw)


def test_parse_table_rejects_alias_names_wrong_shape():
    raw = _table(_entry("alias", names=["OnlyOne"]))
    with pytest.raises(authority.AuthorityTableError, match="names"):
        authority.parse_table(raw)


def test_parse_table_rejects_relabel_missing_from():
    raw = _table({"kind": "relabel", "to": "B", "evidence": "e", "added": "2026-08-03"})
    with pytest.raises(authority.AuthorityTableError, match="from"):
        authority.parse_table(raw)


def test_parse_table_rejects_relabel_missing_to():
    raw = _table({"kind": "relabel", "from": "A", "evidence": "e", "added": "2026-08-03"})
    with pytest.raises(authority.AuthorityTableError, match="to"):
        authority.parse_table(raw)


def test_parse_table_rejects_non_dict_entry():
    raw = _table("not-a-dict")
    with pytest.raises(authority.AuthorityTableError, match="entries\\[0\\]"):
        authority.parse_table(raw)


# ---------------------------------------------------------------------------
# Derived-key validation — one fixture per rule, exercised with stub
# normalizers (never collection_cache's real ones; see module docstring)
# ---------------------------------------------------------------------------


def test_alias_group_rejects_names_deriving_to_the_same_key():
    aliases, _ = authority.parse_table(_table(_alias("Foo", "FOO")))
    with pytest.raises(authority.AuthorityTableError, match="no-op alias"):
        authority._build_alias_groups(aliases, str.lower)


def test_alias_group_rejects_empty_derived_key():
    aliases, _ = authority.parse_table(_table(_alias("Foo", "***")))
    normalize = lambda s: s.strip("*").lower()  # noqa: E731 - test-local stub
    with pytest.raises(authority.AuthorityTableError, match="empty"):
        authority._build_alias_groups(aliases, normalize)


def test_relabel_map_rejects_from_and_to_deriving_to_the_same_key():
    _, relabels = authority.parse_table(_table(_relabel("Foo", "FOO")))
    with pytest.raises(authority.AuthorityTableError, match="no-op relabel"):
        authority._build_relabel_map(relabels, str.lower)


def test_relabel_map_rejects_empty_derived_key():
    _, relabels = authority.parse_table(_table(_relabel("Foo", "***")))
    normalize = lambda s: s.strip("*").lower()  # noqa: E731 - test-local stub
    with pytest.raises(authority.AuthorityTableError, match="empty"):
        authority._build_relabel_map(relabels, normalize)


# ---------------------------------------------------------------------------
# Correct behavior of a non-empty table (not just its failure modes)
# ---------------------------------------------------------------------------


def test_alias_groups_are_symmetric_and_transitively_closed():
    # A chain (Mighty Thor <-> Thor), (Thor <-> Thor God of Thunder) must
    # close into one 3-member group reachable from ANY member — mirrors
    # collection_cache._build_alias_groups's own BFS contract.
    raw = _table(
        _alias("Mighty Thor", "Thor"),
        _alias("Thor", "Thor God of Thunder"),
    )
    aliases, _ = authority.parse_table(raw)
    groups = authority._build_alias_groups(aliases, str.lower)
    expected = frozenset({"mighty thor", "thor", "thor god of thunder"})
    assert groups["mighty thor"] == expected
    assert groups["thor"] == expected
    assert groups["thor god of thunder"] == expected


def test_alias_table_equivalents_falls_back_to_singleton_for_unknown_key():
    table = authority.AliasTable({"a": frozenset({"a", "b"})})
    assert table.equivalents("a") == frozenset({"a", "b"})
    assert table.equivalents("never-seen") == frozenset({"never-seen"})


def test_relabel_map_is_directed_not_symmetric():
    raw = _table(_relabel("Old Name", "New Name"))
    _, relabels = authority.parse_table(raw)
    mapping = authority._build_relabel_map(relabels, str.lower)
    assert mapping == {"old name": "new name"}
    # Directed: the reverse key is absent, not auto-added.
    assert "new name" not in mapping


def test_relabel_table_relabel_passes_through_unmapped_keys():
    table = authority.RelabelTable({"old name": "new name"})
    assert table.relabel("old name") == "new name"
    assert table.relabel("untouched") == "untouched"


# ---------------------------------------------------------------------------
# Derived-key discipline: keys are computed fresh from whichever normalizer
# is injected, never cached from an earlier build (the BUI-546 lesson: a
# hand-normalized table would have silently emptied itself when the
# punctuation fold turned "x-men" into "x men").
# ---------------------------------------------------------------------------


def test_alias_table_keys_change_when_the_injected_normalizer_changes():
    raw = _table(_alias("Foo-Bar", "Baz"))
    aliases, _ = authority.parse_table(raw)

    plain = lambda s: s.strip().lower()  # noqa: E731
    dash_folding = lambda s: s.strip().lower().replace("-", " ")  # noqa: E731

    before = authority._build_alias_groups(aliases, plain)
    assert before["foo-bar"] == frozenset({"foo-bar", "baz"})

    after = authority._build_alias_groups(aliases, dash_folding)
    assert "foo-bar" not in after
    assert after["foo bar"] == frozenset({"foo bar", "baz"})


def test_relabel_table_keys_change_when_the_injected_normalizer_changes():
    """Same build-fresh-every-time discipline as the alias case, tested
    through its no-op check: a normalizer swap can turn a previously-valid
    relabel into a no-op, and that must be caught on THIS build rather than
    reused from an earlier successful one."""
    raw = _table(_relabel("Foo-Bar (2025)", "Foo-Bar (2025 - 2026)"))
    _, relabels = authority.parse_table(raw)

    plain = lambda s: s.strip().lower()  # noqa: E731
    year_folding = lambda s: s.strip().lower().split(" (")[0]  # noqa: E731

    before = authority._build_relabel_map(relabels, plain)
    assert before == {"foo-bar (2025)": "foo-bar (2025 - 2026)"}

    # Under a normalizer that folds the year decoration away entirely, the
    # entry becomes a no-op (both sides derive to "foo-bar") — rejected
    # fresh on this build, proving the earlier successful build wasn't cached.
    with pytest.raises(authority.AuthorityTableError, match="no-op relabel"):
        authority._build_relabel_map(relabels, year_folding)


# ---------------------------------------------------------------------------
# Two disjoint readers — asserted in a test, not left to a future reader's
# discipline (the ticket's own doctrine).
# ---------------------------------------------------------------------------


def test_readers_are_structurally_disjoint():
    alias_table = authority.AliasTable({})
    relabel_table = authority.RelabelTable({})

    assert hasattr(alias_table, "equivalents")
    assert not hasattr(alias_table, "relabel")
    assert hasattr(relabel_table, "relabel")
    assert not hasattr(relabel_table, "equivalents")

    # No shared base beyond object — a combined view has nowhere to live.
    assert authority.AliasTable.__bases__ == (object,)
    assert authority.RelabelTable.__bases__ == (object,)


def test_no_combined_accessor_exists_on_the_module():
    # There is no function that returns both structures together — the two
    # paths out of authority.json never meet in one call.
    public_names = [n for n in dir(authority) if not n.startswith("_")]
    for name in public_names:
        assert "combined" not in name.lower()
        assert "both" not in name.lower()


def test_alias_and_relabel_entries_never_cross_tables():
    # A table with one entry of EACH kind: building the alias table must
    # see only the alias entry's names, and building the relabel table must
    # see only the relabel entry's from/to. Mislabeling either reader would
    # leak a display string into the wrong table's key space.
    raw = _table(
        _alias("Mighty Thor", "Thor"),
        _relabel("Old Run Name", "New Run Name"),
    )
    aliases, relabels = authority.parse_table(raw)
    assert len(aliases) == 1 and len(relabels) == 1

    normalize = lambda s: s.strip().lower()  # noqa: E731
    alias_groups = authority._build_alias_groups(aliases, normalize)
    relabel_map = authority._build_relabel_map(relabels, normalize)

    assert "old run name" not in alias_groups
    assert "new run name" not in alias_groups
    assert "mighty thor" not in relabel_map
    assert "thor" not in relabel_map
