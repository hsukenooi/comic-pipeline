"""Authority table: schema, loader, and validation for provider naming facts.

Provider naming facts — "the provider renamed this run" — used to live only
in Python: ``_MASTHEAD_ALIAS_PAIRS``, a five-entry tuple in
``collection_cache.py``. Recording a fact that way costs a branch, tests,
review, and a deploy, so facts that are expensive to record do not get
recorded (six identity tickets shipped in one week in late July, all one
class). This module is the schema, loader, and validator for
``data/authority.json``, a git-versioned JSON file that holds the same kind
of fact as data instead of code — a reviewed PR touching no matcher logic.

BUI-652 (plan unit U6 of BUI-611) shipped the table, the loader, and the
validation with the table EMPTY. BUI-653 wired this module into
``owned_match_keys`` (``_ALIAS_GROUPS = authority.build_alias_table(
_normalize_series_key).groups`` in ``collection_cache.py``) — a data-only PR
to ``data/authority.json`` now widens the matcher, no code change. BUI-654
wired the other reader: ``identity_series_key`` composes ``_identity_folds``
(the generative half, unchanged) with ``authority.build_relabel_table(
_identity_folds).relabel(...)`` (the directed half, this module's ``relabel``
entries) — see ``collection_cache.py`` for both. The table still ships with
zero ``relabel`` entries; the reader is live, waiting for the first one.

Two entry kinds, two disjoint readers, opposite pressures
-----------------------------------------------------------
* ``alias`` — SYMMETRIC: ``{"kind": "alias", "names": [a, b], "evidence",
  "added"}``. Meant to be read only by the matcher path (``owned_match_keys``
  in ``collection_cache.py``), which WIDENS: "these two names are the same
  run, so a query under either name should find a copy owned under the
  other."
* ``relabel`` — DIRECTED: ``{"kind": "relabel", "from", "to", "evidence",
  "added"}``. Meant to be read only by the identity path
  (``identity_series_key``), which NARROWS: "this literal string is the same
  identity as that canonical one, so collapse it rather than filing a second
  row." **Adding a ``relabel`` entry owes a re-key sweep.** Merging the entry
  changes what ``identity_series_key`` computes for every row whose folded
  series name equals ``from``, but existing rows do not retroactively re-key
  themselves — run ``locg.collection_io.rekey_sweep`` (BUI-650) under
  ``CollectionCache.apply`` right after the entry lands, or the rows the
  entry was meant to merge sit uncollapsed in the store exactly like the
  three live collisions that motivated the sweep in the first place. Run
  ``locg collection authority-check`` (BUI-654, ``commands.py``) against the
  live store first — it reports what the candidate entry would do before it
  is ever merged.

Matcher normalization and identity normalization have OPPOSITE pressures —
widening finds more candidates, narrowing keeps fewer identities — and must
never share a function. ``AliasTable`` and ``RelabelTable`` are separate
frozen types with separate accessors, no shared base class, and no combined
view: this is the mechanism that keeps the two readers apart, not a
convention documented in a comment (see
``test_authority.py::test_readers_are_structurally_disjoint`` and
``test_alias_and_relabel_entries_never_cross_tables``). One file and one
schema serve both normalizations; they never share a code path.

Derived-key discipline
-----------------------
Entries hold DISPLAY strings, never normalized ones. Every key populated
into ``AliasTable``/``RelabelTable`` is derived at BUILD time by running the
entry's display string(s) through a normalizer callable the CALLER injects —
never stored pre-normalized in the JSON, and never computed at this module's
own import. Two reasons:

1. Circular import. ``identity_series_key``/``_normalize_series_key`` live in
   ``collection_cache.py``; this module must not import that module (next
   wave, ``collection_cache.py`` imports THIS module instead). Taking the
   normalizer as a parameter is the only cycle-free shape.
2. The BUI-546 lesson. A hand-normalized table drifts silently out from
   under its own normalizer: BUI-546's punctuation fold turned ``x-men``
   into ``x men``, and every hardcoded normalized key in
   ``collection_cache.py`` (``_XMEN_SPLIT_KEYS``, ``_MASTHEAD_ALIAS_PAIRS``)
   is derived by calling the real normalizer rather than hand-written, for
   exactly this reason. Loading pre-normalized strings from JSON would
   reintroduce the trap this module exists to close.

Because of this, validation is layered by what it depends on. The
normalizer-INDEPENDENT half — unknown ``kind``, unknown ``version``, missing
``evidence``/``added``, wrong shape — is checked once, at THIS module's own
import, against the packaged ``data/authority.json``: a structurally
malformed table raises before any caller ever asks for a normalizer-derived
view. The normalizer-DEPENDENT half — an ``alias`` whose two names derive to
the same key, a ``relabel`` whose ``from``/``to`` derive to the same key, an
entry whose derived key is empty — runs inside ``build_alias_table``/
``build_relabel_table`` every time a caller builds a table, because it
cannot run any earlier than the normalizer is known.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Callable, Mapping, Sequence, Union

SCHEMA_VERSION = 1

# A normalizer takes a display string and returns its normalized key —
# `_normalize_series_key` or `identity_series_key` in `collection_cache.py`,
# injected by the caller so this module never imports that one.
Normalizer = Callable[[str], str]

# The two entry kinds. Named once so `_parse_entry` and its error message
# can't drift out of sync with each other via a hand-typed literal.
_KIND_ALIAS = "alias"
_KIND_RELABEL = "relabel"


class AuthorityTableError(ValueError):
    """Raised when authority.json (or a fixture) fails schema validation."""


@dataclass(frozen=True)
class AliasEntry:
    """A parsed, structurally-valid ``alias`` entry. Display strings, not keys."""

    names: tuple[str, str]
    evidence: str
    added: str


@dataclass(frozen=True)
class RelabelEntry:
    """A parsed, structurally-valid ``relabel`` entry. Display strings, not keys."""

    from_name: str
    to_name: str
    evidence: str
    added: str


def _require_nonempty_str(value: object, field: str, desc: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityTableError(
            f"{desc}: {field!r} must be a non-empty string, got {value!r}"
        )
    return value


def _parse_entry(raw: object, index: int) -> Union[AliasEntry, RelabelEntry]:
    if not isinstance(raw, dict):
        raise AuthorityTableError(
            f"entries[{index}]: expected an object, got {type(raw).__name__}"
        )
    kind = raw.get("kind")
    desc = f"entries[{index}] (kind={kind!r})"
    evidence = _require_nonempty_str(raw.get("evidence"), "evidence", desc)
    added = _require_nonempty_str(raw.get("added"), "added", desc)
    if kind == _KIND_ALIAS:
        names = raw.get("names")
        if not (isinstance(names, list) and len(names) == 2):
            raise AuthorityTableError(
                f"{desc}: 'names' must be a 2-element list, got {names!r}"
            )
        a = _require_nonempty_str(names[0], "names[0]", desc)
        b = _require_nonempty_str(names[1], "names[1]", desc)
        return AliasEntry(names=(a, b), evidence=evidence, added=added)
    if kind == _KIND_RELABEL:
        from_name = _require_nonempty_str(raw.get("from"), "from", desc)
        to_name = _require_nonempty_str(raw.get("to"), "to", desc)
        return RelabelEntry(
            from_name=from_name, to_name=to_name, evidence=evidence, added=added
        )
    raise AuthorityTableError(
        f"{desc}: unknown kind {kind!r} (expected {_KIND_ALIAS!r} or {_KIND_RELABEL!r})"
    )


def parse_table(raw: object) -> tuple[tuple[AliasEntry, ...], tuple[RelabelEntry, ...]]:
    """Structurally validate a raw ``{"version", "entries"}`` dict and split by kind.

    This is the normalizer-INDEPENDENT half of validation: unknown
    ``version``, wrong root/entry shape, unknown ``kind``, and missing
    ``evidence``/``added``. It does not know about derived keys and cannot —
    that half needs a normalizer, which only ``build_alias_table``/
    ``build_relabel_table`` receive. Exposed (not underscored) so tests can
    feed it fixtures directly rather than only exercising the packaged file.
    """
    if not isinstance(raw, dict):
        raise AuthorityTableError(
            f"authority table root must be an object, got {type(raw).__name__}"
        )
    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise AuthorityTableError(
            f"unknown authority table version {version!r} (known: {SCHEMA_VERSION!r})"
        )
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise AuthorityTableError(
            f"'entries' must be a list, got {type(entries).__name__}"
        )
    aliases: list[AliasEntry] = []
    relabels: list[RelabelEntry] = []
    for i, raw_entry in enumerate(entries):
        parsed = _parse_entry(raw_entry, i)
        if isinstance(parsed, AliasEntry):
            aliases.append(parsed)
        else:
            relabels.append(parsed)
    return tuple(aliases), tuple(relabels)


def _read_packaged_json() -> dict:
    """Read and JSON-parse the packaged ``data/authority.json``.

    Anchored on the ``locg`` package itself (which is always a real,
    importable package) rather than on ``locg.data``, so this does not
    depend on ``data/`` being importable as its own subpackage.
    """
    data_file = resources.files("locg") / "data" / "authority.json"
    text = data_file.read_text(encoding="utf-8")
    return json.loads(text)


# Loaded once at import (KTD5 / the ticket's own acceptance criterion): a
# structurally malformed packaged table raises here, at `import
# locg.authority`, before any caller can build a normalizer-derived view.
_ALIAS_ENTRIES, _RELABEL_ENTRIES = parse_table(_read_packaged_json())


@dataclass(frozen=True)
class AliasTable:
    """Symmetric name-equivalence groups, derived from ``alias`` entries.

    Meant to be read ONLY by the matcher path (``owned_match_keys``), which
    widens a query to every name the same run has been filed under. Built by
    :func:`build_alias_table`; never construct directly — a ``@dataclass``
    field is visible through ``__init__``/``__repr__`` regardless of naming,
    so ``groups`` is named plainly rather than with a leading underscore
    that would only pretend to hide it.
    """

    groups: Mapping[str, frozenset[str]]

    def equivalents(self, key: str) -> frozenset[str]:
        """Normalized keys equivalent to ``key`` (always includes ``key`` itself)."""
        return self.groups.get(key, frozenset({key}))


@dataclass(frozen=True)
class RelabelTable:
    """Directed key rewrites, derived from ``relabel`` entries.

    Meant to be read ONLY by the identity path (``identity_series_key``),
    which narrows a literal spelling onto its canonical identity key. Built
    by :func:`build_relabel_table`; never construct directly (see
    :class:`AliasTable` for why ``mapping`` has no leading underscore).
    """

    mapping: Mapping[str, str]

    def relabel(self, key: str) -> str:
        """The canonical identity key for ``key``, or ``key`` unchanged if none applies."""
        return self.mapping.get(key, key)


def _build_alias_groups(
    entries: Sequence[AliasEntry], normalize: Normalizer
) -> dict[str, frozenset[str]]:
    """Close ``alias`` entries into symmetric, transitive equivalence groups.

    Mirrors ``collection_cache._build_alias_groups``'s BFS-over-adjacency
    shape (that function closes a hardcoded tuple; this one closes table
    entries), returning norm_key -> frozenset of every key equivalent to it,
    including itself.
    """
    adj: dict[str, set[str]] = {}
    for entry in entries:
        a = normalize(entry.names[0])
        b = normalize(entry.names[1])
        desc = f"alias entry {entry.names!r} (added {entry.added})"
        if not a or not b:
            raise AuthorityTableError(
                f"{desc}: derived key must not be empty (got {a!r}, {b!r})"
            )
        if a == b:
            raise AuthorityTableError(
                f"{desc}: both names derive to the same key {a!r} — a no-op alias"
            )
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    groups: dict[str, frozenset[str]] = {}
    for node in adj:
        if node in groups:
            continue
        seen: set[str] = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adj.get(cur, ()))
        frozen = frozenset(seen)
        for member in seen:
            groups[member] = frozen
    return groups


def _build_relabel_map(
    entries: Sequence[RelabelEntry], normalize: Normalizer
) -> dict[str, str]:
    """Derive the directed ``from`` -> ``to`` key map from ``relabel`` entries."""
    mapping: dict[str, str] = {}
    for entry in entries:
        from_key = normalize(entry.from_name)
        to_key = normalize(entry.to_name)
        desc = f"relabel entry {entry.from_name!r} -> {entry.to_name!r} (added {entry.added})"
        if not from_key or not to_key:
            raise AuthorityTableError(
                f"{desc}: derived key must not be empty (got {from_key!r}, {to_key!r})"
            )
        if from_key == to_key:
            raise AuthorityTableError(
                f"{desc}: 'from' and 'to' derive to the same key {from_key!r} — a no-op relabel"
            )
        mapping[from_key] = to_key
    return mapping


def build_alias_table(normalize: Normalizer) -> AliasTable:
    """Build the symmetric :class:`AliasTable` by running every ``alias`` entry's
    names through ``normalize``.

    Called by the matcher path with the real ``_normalize_series_key``, and
    by tests with a stub normalizer. Raises :class:`AuthorityTableError` if
    any entry's derived keys collide or are empty. Reads only
    ``alias``-kind entries — ``relabel`` entries never reach this function.
    """
    return AliasTable(_build_alias_groups(_ALIAS_ENTRIES, normalize))


def build_relabel_table(normalize: Normalizer) -> RelabelTable:
    """Build the directed :class:`RelabelTable` by running every ``relabel``
    entry's ``from``/``to`` through ``normalize``.

    Called by the identity path with ``collection_cache._identity_folds`` —
    deliberately NOT ``identity_series_key`` itself, which reads the table
    this call builds and would therefore read it before this assignment
    finishes binding (a ``NameError`` an empty table hides completely, since
    the loop that would trigger it never runs over zero entries). Also called
    by tests with a stub normalizer. Raises :class:`AuthorityTableError` if
    any entry's derived keys collide, are empty, or form a no-op. Reads only
    ``relabel``-kind entries — ``alias`` entries never reach this function.
    """
    return RelabelTable(_build_relabel_map(_RELABEL_ENTRIES, normalize))
