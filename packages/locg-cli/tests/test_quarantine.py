"""Quarantine state + the ``matchable_rows`` seam (BUI-647).

Three things are locked in here, in ascending order of how much a regression
would cost:

1. **The predicate's edges** — absent / ``None`` / ``{}`` / a bare boolean are
   all *matchable*, so no existing row and no LOCG import row is accidentally
   quarantined and no migration is needed.
2. **Every candidate pool excludes a quarantined row** — table-driven, and the
   table is checked against the source itself, so a pool that starts filtering
   without registering here fails.
3. **The owned-safe export layer does NOT exclude it** — the inverted test.
   This is the load-bearing one: filtering there emits ``In Collection=0`` for
   an owned book and LOCG deletes it (the BUI-122 data-loss path, and the
   BUI-200 incident where 26 owned X-Men went that way).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------
#
# Two rows of the same series, both source='locg_export' so they reach the
# index/candidate pools that filter on that (R61):
#
#   * OWNED_TITLE   — in_collection=1, the ownership-matcher population
#   * WISHED_TITLE  — in_collection=0, the tracked-not-owned population
#
# Both carry a publisher ("Panini Comics") that is deliberately the real-world
# shape this state exists for: a foreign licensed edition our own record-win
# push created (BUI-563), which cannot be deleted (LOCG re-emits it) and cannot
# be un-owned (that runs BUI-122).

SERIES = "Spawn (Vol. 1) (2012 - Present)"
SERIES_KEY = "spawn"
OWNED_TITLE = "Spawn #9"
OWNED_ISSUE = "9"
WISHED_TITLE = "Spawn #11"
WISHED_ISSUE = "11"


def _marker() -> dict[str, str]:
    from locg.collection_cache import quarantine_marker

    return quarantine_marker(
        reason="foreign licensed edition (Panini) minted by our own record-win push",
        ticket="BUI-647",
        by="test",
    )


def _row(full_title: str, in_collection: int, *, quarantined: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "publisher_name": "Panini Comics",
        "series_name": SERIES,
        "full_title": full_title,
        "release_date": "2012-05-01",
        "in_collection": in_collection,
        "in_wish_list": 0 if in_collection else 1,
        "source": "locg_export",
        "pushed_to_locg_at": "2024-01-01T00:00:00.000000Z",
        "local_added_at": "2024-01-01T00:00:00.000000Z",
    }
    if quarantined:
        row["quarantined"] = _marker()
    return row


def _payload(*, quarantined: bool) -> dict[str, Any]:
    return {
        "comics": [
            _row(OWNED_TITLE, 1, quarantined=quarantined),
            _row(WISHED_TITLE, 0, quarantined=quarantined),
        ]
    }


# ---------------------------------------------------------------------------
# 1. The predicate's edges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", [
    pytest.param({}, id="key-absent"),
    pytest.param({"quarantined": None}, id="none"),
    pytest.param({"quarantined": {}}, id="empty-object"),
])
def test_absent_none_and_empty_are_matchable(row):
    """The key's absence (and its two empty spellings) means NOT quarantined —
    this is what makes the state migration-free: every row already on disk and
    every row a LOCG import writes is a full citizen by default."""
    from locg.collection_cache import is_quarantined, matchable_rows

    assert is_quarantined(row) is False
    assert matchable_rows([row]) == [row]


@pytest.mark.parametrize("value", [True, 1, "yes", ["BUI-647"]])
def test_non_object_marker_is_not_honored(value):
    """A bare boolean (or any non-object) does NOT quarantine. The marker is a
    structured object by contract precisely so a hidden row can always name who
    hid it and why — honoring a shape that records neither would repeat the
    ``bids`` mistake (a bare REMOVED that couldn't distinguish a live cancel
    from a completed sweep, which BUI-371 had to bolt a ``notes`` marker onto).
    """
    from locg.collection_cache import is_quarantined

    assert is_quarantined({"quarantined": value}) is False


def test_matchable_rows_drops_only_the_quarantined_ones():
    """A MIXED list — the pool fixtures below are all-or-nothing, so this is the
    only place that proves the seam is a filter and not a switch."""
    from locg.collection_cache import matchable_rows

    clean_a = {"full_title": "A #1"}
    dirty = {"full_title": "B #1", "quarantined": _marker()}
    clean_b = {"full_title": "C #1"}
    rows = [clean_a, dirty, clean_b]

    result = matchable_rows(rows)

    assert result == [clean_a, clean_b], "order and row identity must survive"
    assert result is not rows, (
        "must return a new list — handing back the caller's own list would let "
        "a pool's downstream mutation reach into the store payload"
    )
    assert matchable_rows(None) == []


def test_marker_carries_all_four_fields():
    from locg.collection_cache import QUARANTINE_FIELDS, is_quarantined, quarantine_marker

    marker = quarantine_marker(reason="r", ticket="BUI-647", by="tester")
    assert set(marker) == set(QUARANTINE_FIELDS)
    assert all(marker[field] for field in QUARANTINE_FIELDS)
    assert is_quarantined({"quarantined": marker}) is True


@pytest.mark.parametrize("kwargs", [
    {"reason": "", "ticket": "BUI-647", "by": "tester"},
    {"reason": "r", "ticket": "  ", "by": "tester"},
    {"reason": "r", "ticket": "BUI-647", "by": ""},
])
def test_marker_refuses_an_unattributable_quarantine(kwargs):
    """A marker that can't say why/who/which-ticket is one nobody can safely
    lift, so the constructor refuses to build it."""
    from locg.collection_cache import quarantine_marker

    with pytest.raises(ValueError):
        quarantine_marker(**kwargs)


# ---------------------------------------------------------------------------
# 2. Every candidate pool excludes a quarantined row
# ---------------------------------------------------------------------------
#
# Each probe takes a payload and returns the set of store-derived strings that
# pool surfaced. A pool passes when the CLEAN fixture yields something and the
# QUARANTINED fixture yields nothing — the clean half is what keeps the test
# from passing vacuously on a probe that never matched anything to begin with.

def _probe_owned_series_issue_candidates(payload) -> set[str]:
    from locg.commands import _owned_series_issue_candidates

    rows = _owned_series_issue_candidates(
        payload["comics"], SERIES_KEY, OWNED_ISSUE, OWNED_ISSUE
    )
    return {r["full_title"] for r in rows}


def _probe_match_owned_issue(payload) -> set[str]:
    from locg.commands import _match_owned_issue

    row = _match_owned_issue(
        payload["comics"], SERIES_KEY, OWNED_ISSUE, OWNED_ISSUE, None, None
    )
    return {row["full_title"]} if row else set()


def _probe_match_wishlisted_issue(payload) -> set[str]:
    from locg.commands import _match_wishlisted_issue

    row = _match_wishlisted_issue(
        payload["comics"], SERIES_KEY, WISHED_ISSUE, WISHED_ISSUE, None
    )
    return {row["full_title"]} if row else set()


def _probe_printing_conflict_fields(payload) -> set[str]:
    """Drive the probe so the flag always RAISES, making ``printing_candidates``
    (the store-derived list) the observable output.

    A "3rd Printing" query against a matched "2nd Printing" row differs, and no
    row in the fixture carries ordinal 3, so the escape hatch (some other owned
    row already IS the queried printing) can't fire and short-circuit the list.
    """
    from locg.commands import _printing_conflict_fields

    fields = _printing_conflict_fields(
        payload["comics"],
        "Spawn 3rd Printing",
        {"full_title": f"{OWNED_TITLE} 2nd Printing"},
        frozenset({SERIES_KEY}),
        OWNED_ISSUE,
        OWNED_ISSUE,
        None,
    )
    assert fields["printing_conflict"] is True, "probe must keep the flag raised"
    return {c["full_title"] for c in fields.get("printing_candidates", [])}


def _probe_owned_dedup_index(payload) -> set[str]:
    """BUI-669's ``_owned_dedup_index``: record-win's already-owned skip index,
    and the only pool on a WRITE path. Only ``in_collection`` rows enter it, so
    the observable output is the owned row alone.

    Deliberately not numbered: BUI-669's own ticket body said "seven pools"
    when there were already eight, because BUI-648 had added one and the prose
    did not follow. :data:`POOLS` below is the count."""
    from locg.commands import _owned_dedup_index

    index = _owned_dedup_index(payload["comics"])
    return {row["full_title"] for rows in index.values() for row in rows}


def _probe_rebuild_series_name_index(payload) -> set[str]:
    from locg.collection_cache import rebuild_series_name_index

    return set(rebuild_series_name_index(payload).values())


def _probe_build_volume_candidates(payload) -> set[str]:
    from locg.collection_cache import build_volume_candidates

    return {name for names in build_volume_candidates(payload).values() for name in names}


def _probe_build_series_publishers(payload) -> set[str]:
    from locg.collection_io import build_series_publishers

    return set(build_series_publishers(payload))


class _FakeAuthorityCheckCache:
    """Only ``load()`` is used by ``cmd_collection_authority_check`` (BUI-654)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def load(self) -> dict[str, Any]:
        return self._payload


def _probe_authority_check(payload) -> set[str]:
    """BUI-654's ``cmd_collection_authority_check``: an alias candidate
    between SERIES and a name it does not otherwise share a normalized key
    with, exercised over ``owned_rows_affected`` — the store-derived list."""
    from locg.commands import cmd_collection_authority_check

    result = cmd_collection_authority_check(
        kind="alias",
        name_a="Spawn",
        name_b="Spawn Rebirth",
        cache=_FakeAuthorityCheckCache(payload),
    )
    return {row["full_title"] for row in result["owned_rows_affected"]}


# The registry. Keys are "<module>.<function>" so the source cross-check below
# can compare them against what actually calls ``matchable_rows``. Registering a
# new pool is one line here.
POOLS: dict[str, Callable[[dict[str, Any]], set[str]]] = {
    "commands._owned_series_issue_candidates": _probe_owned_series_issue_candidates,
    "commands._match_owned_issue": _probe_match_owned_issue,
    "commands._match_wishlisted_issue": _probe_match_wishlisted_issue,
    "commands._printing_conflict_fields": _probe_printing_conflict_fields,
    "commands._owned_dedup_index": _probe_owned_dedup_index,
    "commands.cmd_collection_authority_check": _probe_authority_check,
    "collection_cache.rebuild_series_name_index": _probe_rebuild_series_name_index,
    "collection_cache.build_volume_candidates": _probe_build_volume_candidates,
    "collection_io.build_series_publishers": _probe_build_series_publishers,
}


@pytest.mark.parametrize("pool_name", sorted(POOLS))
def test_pool_surfaces_the_row_when_not_quarantined(pool_name):
    """Control half: without the marker every pool DOES surface the row, so the
    exclusion test below can't pass for the wrong reason."""
    assert POOLS[pool_name](_payload(quarantined=False)), (
        f"{pool_name} surfaced nothing even for an unquarantined row — the "
        "exclusion assertion for this pool would pass vacuously"
    )


@pytest.mark.parametrize("pool_name", sorted(POOLS))
def test_pool_excludes_a_quarantined_row(pool_name):
    assert POOLS[pool_name](_payload(quarantined=True)) == set(), (
        f"{pool_name} still surfaced a quarantined row — it must filter at its "
        "own entry point via collection_cache.matchable_rows"
    )


def _called_name(node: ast.AST) -> str | None:
    """Callee name of ``node`` if it is a call, for both the bare
    ``matchable_rows(...)`` and the qualified ``collection_cache.matchable_rows(...)``
    spellings — otherwise a module that imported the seam qualified would slip
    past the coverage check below."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _functions_calling_matchable_rows() -> set[str]:
    """``"<module>.<function>"`` for every function in the ``locg`` package
    whose body calls ``matchable_rows``.

    Scans the package directory rather than a hardcoded module list: a NEW
    module that starts filtering has to register in :data:`POOLS` too, and a
    fixed list would silently stop covering the package as it grows.
    """
    import locg

    found: set[str] = set()
    for path in sorted(Path(locg.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(_called_name(c) == "matchable_rows" for c in ast.walk(node)):
                found.add(f"{path.stem}.{node.name}")
    return found


def test_pool_table_matches_the_source():
    """The registry above must name EXACTLY the functions that filter.

    Without this, "registering a new pool is one line" degrades into "one line
    somebody may forget": a new pool could start filtering with no test proving
    it does, and — the direction that actually costs — a pool could quietly
    STOP filtering and no test would notice, because the table only ever
    exercises what it already knows about.
    """
    from_source = _functions_calling_matchable_rows()
    assert from_source == set(POOLS), (
        "the matchable_rows call sites and the POOLS table have diverged; "
        f"only in source: {sorted(from_source - set(POOLS))}; "
        f"only in table: {sorted(set(POOLS) - from_source)}"
    )


def test_commands_side_pools_are_reached_only_from_expected_call_sites():
    """The helper-shaped ``commands.py`` pools take their rows from known call sites.

    Asserted rather than assumed (the point of filtering each pool at its own
    entry): the moment an unlisted caller appears, this fails and whoever added
    it has to confirm the new path is a candidate pool and not an enforcement
    layer. It is not a licence to move the filter to the call site — that would
    make the exclusion depend on the caller remembering.

    BUI-648 added the second caller, and it went through exactly that check:
    ``_owned_rows_covering`` is the last-owned-row guard's predicate, whose job
    is to predict what ``cmd_collection_check`` will answer once a quarantine
    lands. Reading the check's OWN pool is what makes the two provably agree —
    a guard consulting a different population could permit a quarantine the
    check then reports as not-owned, and the buy path would re-buy the book.

    BUI-669 added ``_owned_dedup_index`` on the same terms from the other side:
    record-win's already-owned skip must not answer with a row
    ``cmd_collection_check`` has stopped answering with, or the win is dropped
    AND the book still reads not-owned. It is a named function listed here,
    rather than the inline loop it used to be inside
    ``cmd_collection_record_win``, precisely so a second caller has to justify
    itself — it is keyed on the full_title PREFIX (BUI-184) and is NOT
    interchangeable with the check's ``owned_match_keys`` population.
    """
    import importlib

    module = importlib.import_module("locg.commands")
    tree = ast.parse(Path(module.__file__).read_text())
    expected: dict[str, set[str]] = {
        "_owned_series_issue_candidates": {"cmd_collection_check", "_owned_rows_covering"},
        "_match_owned_issue": {"cmd_collection_check"},
        "_match_wishlisted_issue": {"cmd_collection_check"},
        "_printing_conflict_fields": {"cmd_collection_check"},
        "_owned_dedup_index": {"cmd_collection_record_win"},
    }
    callers: dict[str, set[str]] = {name: set() for name in expected}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            name = _called_name(child)
            if name in expected:
                callers[name].add(node.name)

    assert callers == expected


# ---------------------------------------------------------------------------
# 3. The inverted test — the owned-safe export layer must NOT filter
# ---------------------------------------------------------------------------

_DELETION_WARNING = (
    "REGRESSION: the owned-safe export layer is filtering quarantined rows. "
    "That is not a consistency improvement, it is the BUI-122 data-loss path: "
    "an owned row missing from the owned index means the export emits a wish "
    "row with In Collection=0 for it, and LOCG DELETES the book from the "
    "collection (the BUI-200 incident deleted 26 owned X-Men exactly this "
    "way). Quarantine means 'not a match candidate'; it never means 'safe to "
    "delete'. Revert the matchable_rows call in collection_io."
)


def _seed_wish(items: list[dict[str, Any]]) -> None:
    from locg.collection_io import wish_list_cache_path

    path = wish_list_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": "2026-01-01T00:00:00+00:00", "items": items}))


def test_quarantined_owned_row_still_in_owned_series_issue_index():
    from locg.collection_io import _owned_series_issue_index
    from locg.parsing import normalize_issue_key

    index = _owned_series_issue_index(_payload(quarantined=True))
    assert (SERIES_KEY, normalize_issue_key(OWNED_ISSUE)) in index, _DELETION_WARNING


def test_quarantined_owned_row_still_suppresses_its_wish(tmp_path):
    """End-to-end, and the test both call-site comments name by path.

    Deliberately a CROSS-MASTHEAD wish (BUI-200: owned as "The X-Men #107",
    wished as "Uncanny X-Men #107"). ``wish_rows_for_export`` has two
    independent owned-safe paths, and the verbatim-title one
    (``owned_titles``/``_normalize_title``) would catch a same-title wish even
    with the index filtered — so a same-title fixture here would survive the
    very edit this test exists to catch. Only the ``(series, issue)`` index
    spans mastheads, which makes this the one wish shape that isolates it.
    """
    from locg.collection_io import wish_rows_for_export

    _seed_wish([
        {"name": "Uncanny X-Men #107", "id": None},  # owned under the other masthead
        {"name": "Saga #1", "id": None},             # genuinely unowned -> must export
    ])
    payload = {"comics": [{
        "full_title": "The X-Men #107",
        "series_name": "The X-Men (Vol. 1) (1963 - 1981)",
        "publisher_name": "Marvel Comics",
        "release_date": "1977-10-01",
        "in_collection": 1,
        "source": "locg_export",
        "quarantined": _marker(),
    }]}

    exported = {r["full_title"] for r in wish_rows_for_export(payload)}
    assert "Uncanny X-Men #107" not in exported, _DELETION_WARNING
    assert "Saga #1" in exported, (
        "control: an unowned wish must still export, or the assertion above "
        "would pass even if the export were broken outright"
    )


def test_quarantined_owned_row_still_suppresses_a_verbatim_title_wish(tmp_path):
    """The other owned-safe path (``_normalize_title``, BUI-122) must not start
    filtering either — it is the backstop that holds when the wish carries a
    ``source`` that defeats the derived-wish gate."""
    from locg.collection_io import wish_rows_for_export

    _seed_wish([{"name": OWNED_TITLE, "id": None}])
    assert wish_rows_for_export(_payload(quarantined=True)) == [], _DELETION_WARNING


# ---------------------------------------------------------------------------
# 4. BUI-669 — the record-win skip the quarantined row must no longer cause
# ---------------------------------------------------------------------------


def _record_win_skipped(payload: dict[str, Any]) -> bool:
    """Does ``_build_win_row`` skip a genuine US win of ``OWNED_TITLE`` against
    ``payload``'s owned rows?

    Driven at ``_build_win_row`` rather than at the index, because the skip is
    what costs: the index probe above proves the row is filtered out, this
    proves the filtering actually changes the win's fate. Metron is shut off
    (``metron_disabled=True``) so the series resolves purely through
    ``series_name_index`` and no network path is reachable.
    """
    from locg.commands import _build_win_row, _normalize_series_key, _owned_dedup_index

    result = _build_win_row(
        {
            "item_id": "us-win-1",
            "current_bid": "40.00",
            "end_date_iso": "2026-08-01T00:00:00Z",
            "identify_data": {"series": "Spawn", "issue": OWNED_ISSUE},
        },
        series_name_index={_normalize_series_key("Spawn"): "Spawn"},
        volume_candidates={},
        existing_titles=set(),
        owned_index=_owned_dedup_index(payload["comics"]),
        metron=None,
        metron_disabled=True,
    )
    return bool(result["skipped"])


def test_quarantined_owned_row_does_not_dedup_skip_a_genuine_win():
    """BUI-669's named case, both halves.

    A quarantined Panini row swallowing a genuine US win is backwards twice
    over: the row was hidden precisely because it mis-matches, and since
    BUI-647 ``cmd_collection_check`` no longer answers "owned" with it — so an
    unfiltered skip here drops the win AND leaves the book reading not-owned,
    which is a duplicate PURCHASE, not a duplicate row.

    The control half (clean row => skip) is what keeps this from passing for
    the wrong reason: without it, a ``_build_win_row`` that stopped skipping
    altogether would look like a pass.
    """
    assert _record_win_skipped(_payload(quarantined=False)) is True, (
        "control: an unquarantined owned row must still dedup-skip the win "
        "(BUI-34), or the assertion below proves nothing about quarantine"
    )
    assert _record_win_skipped(_payload(quarantined=True)) is False, (
        "a quarantined row still dedup-skipped a genuine win — the win is then "
        "never recorded while collection check reports the book not-owned, and "
        "the buy path re-buys it (BUI-669)"
    )


# ---------------------------------------------------------------------------
# 5. BUI-670 — the conflicts audit's deliberate under-report
# ---------------------------------------------------------------------------


def _seed_collection(payload: dict[str, Any]) -> None:
    from locg.collection_cache import CollectionCache

    CollectionCache().apply(
        lambda stored: stored["comics"].extend(payload["comics"]),
        command="test-seed",
    )


def test_conflicts_audit_omits_a_quarantined_owned_wish_but_export_still_holds():
    """BUI-670, DECIDED as no behavior change — and this test is the decision.

    The audit reaches ownership entirely through ``cmd_collection_check``,
    which BUI-647 made quarantine-aware, so a book whose only owned row is
    quarantined is not named as a conflict. That is deliberate:
    ``cmd_wish_list_remove_conflicts`` DELETES every ``conflicts`` entry it is
    handed, and deleting a genuine want on the authority of a row we formally
    disowned (the Panini case) is the ticket's own bar failing in its worse
    form — the action exists and is wrong.

    The second assertion is what makes "no change" defensible rather than
    negligent: the owned-safe export layer is NOT quarantine-filtered, so the
    wish is still suppressed and the BUI-122 ``In Collection=0`` deletion path
    is unreachable. The under-report is cosmetic; if that ever stops being
    true, this test — not a later incident — is what says so.
    """
    from locg.collection_io import wish_rows_for_export
    from locg.commands import cmd_wish_list_conflicts

    _seed_collection(_payload(quarantined=True))
    _seed_wish([{"name": OWNED_TITLE, "id": 4242}])

    audit = cmd_wish_list_conflicts()
    assert audit["checked"] == 1
    assert audit["conflicts"] == [], (
        "the conflicts audit named a quarantined-owned book — "
        "cmd_wish_list_remove_conflicts would then delete a wish the user "
        "genuinely still wants, on the authority of a row we disowned (BUI-670)"
    )
    assert audit["printing_conflicts"] == [], (
        "nor may it arrive through the printing-decoy list"
    )

    assert wish_rows_for_export({"comics": _payload(quarantined=True)["comics"]}) == [], (
        _DELETION_WARNING
    )


def test_conflicts_audit_still_reports_the_same_wish_when_not_quarantined():
    """Control for the test above: the audit is not simply broken for this
    fixture. Same store, same wish, marker removed => a reported conflict."""
    from locg.commands import cmd_wish_list_conflicts

    _seed_collection(_payload(quarantined=False))
    _seed_wish([{"name": OWNED_TITLE, "id": 4242}])

    audit = cmd_wish_list_conflicts()
    assert [c["name"] for c in audit["conflicts"]] == [OWNED_TITLE]


# ---------------------------------------------------------------------------
# Import round-trip
# ---------------------------------------------------------------------------

def test_import_preserves_the_marker_and_inserts_no_twin(tmp_path):
    """A LOCG re-export of a quarantined row updates it in place and leaves the
    marker alone. This is what makes the state survive the one operation that
    rewrites every row — and it is why a quarantined foreign edition can't be
    "fixed" by re-importing.
    """
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )
    marker = _marker()

    def seed(payload):
        row = _row(OWNED_TITLE, 1, quarantined=False)
        row["quarantined"] = marker
        payload["comics"].append(row)

    cache.apply(seed, command="pre-import")

    import openpyxl

    from locg.collection_io import LOCG_XLSX_HEADERS

    xlsx = tmp_path / "reexport.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(LOCG_XLSX_HEADERS))
    ws.append([
        "Panini Comics", SERIES, OWNED_TITLE, "2012-05-01",
        1, 0, 0, None, "Print", None, None,
        None, None, None, None, None, None, None, None, None, None,
    ])
    wb.save(xlsx)

    import_xlsx(xlsx, cache)
    rows = [r for r in cache.load()["comics"] if r["full_title"] == OWNED_TITLE]

    assert len(rows) == 1, "import must update the quarantined row in place, not twin it"
    assert rows[0]["quarantined"] == marker
