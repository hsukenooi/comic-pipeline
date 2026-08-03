"""Tests for `locg collection authority-check` (BUI-654).

An advisory report: what a PROPOSED `alias`/`relabel` entry would do to the
live collection, run before the entry is ever merged into
`data/authority.json`. Never a CI gate — CI cannot see the live corpus — and
never writes the authority table itself; there is no CLI "add" path (an entry
is a reviewed PR to that JSON file).

The live-shaped fixture below mirrors the actual pair BUI-654's own ticket
names as the worked acceptance check: "The X-Men #118" (1978, US) and the
Panini "X-Men #118" (2010) are both owned today, both fold to the SAME
`_normalize_series_key` ("x men", from `(Vol. N)`/year-range stripping alone)
— quarantining the Panini row is BUI-649's job, not this one's, so as of this
ticket the pair is still live and the report must flag it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from locg.collection_cache import CollectionCache
from locg.commands import cmd_collection_authority_check


def make_cache(tmp_path: Path):
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    *,
    publisher: str,
    series: str,
    full_title: str,
    release_date: str,
    in_collection: int = 1,
) -> dict[str, Any]:
    return {
        "publisher_name": publisher,
        "series_name": series,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": in_collection,
        "in_wish_list": 0,
        "source": "locg_export",
    }


def _seed(cache, rows):
    cache.apply(lambda p: p["comics"].extend(rows), command="seed")


# ---------------------------------------------------------------------------
# Validation — bad kind, missing names, malformed entries. Mirrors the exact
# errors authority.py itself would raise at real merge time (validation
# parity), never a silent no-op.
# ---------------------------------------------------------------------------


def test_rejects_unknown_kind(tmp_path):
    cache = make_cache(tmp_path)
    result = cmd_collection_authority_check(kind="bogus", cache=cache)
    assert result["status"] == "invalid_request"
    assert "kind" in result["error"]


def test_alias_requires_both_names(tmp_path):
    cache = make_cache(tmp_path)
    result = cmd_collection_authority_check(kind="alias", name_a="Foo", cache=cache)
    assert result["status"] == "invalid_request"

    result2 = cmd_collection_authority_check(kind="alias", cache=cache)
    assert result2["status"] == "invalid_request"


def test_relabel_requires_both_from_and_to(tmp_path):
    cache = make_cache(tmp_path)
    result = cmd_collection_authority_check(kind="relabel", from_name="Foo", cache=cache)
    assert result["status"] == "invalid_request"


def test_alias_no_op_entry_is_rejected(tmp_path):
    """Both names deriving to the same normalized key is a caller error, the
    exact rule authority._build_alias_groups enforces at real merge time —
    checked here too, so a malformed entry never gets past authority-check."""
    cache = make_cache(tmp_path)
    result = cmd_collection_authority_check(kind="alias", name_a="Foo", name_b="FOO", cache=cache)
    assert result["status"] == "invalid_request"
    assert "no-op" in result["error"]


def test_relabel_no_op_entry_is_rejected(tmp_path):
    """A `from`/`to` pair the generative folds ALREADY collapse to one key
    (e.g. the two spellings of a volume's end-year decoration BUI-554/560
    already fold) is a no-op relabel — proves the check runs the SAME
    validation the real relabel table would."""
    cache = make_cache(tmp_path)
    result = cmd_collection_authority_check(
        kind="relabel",
        from_name="Zzyzx Ranger (2025)",
        to_name="Zzyzx Ranger (2025 - Present)",
        cache=cache,
    )
    assert result["status"] == "invalid_request"
    assert "no-op" in result["error"]


# ---------------------------------------------------------------------------
# R11: an empty store must report that the corpus was empty, never "clean".
# ---------------------------------------------------------------------------


def test_empty_store_reports_corpus_empty_not_clean(tmp_path):
    cache = make_cache(tmp_path)  # never seeded — zero comics rows
    result = cmd_collection_authority_check(
        kind="alias", name_a="Uncanny X-Men", name_b="X-Men", cache=cache
    )
    assert result["status"] == "ok"
    assert result["corpus_empty"] is True
    assert result["owned_rows_affected"] == []
    assert result["cross_volume_ambiguities"] == []


def test_non_empty_store_with_no_matching_rows_is_not_corpus_empty(tmp_path):
    """Distinguishes "nothing found because the store is empty" from "checked,
    and this entry's pool happens to be empty" — R11 needs BOTH readable, not
    conflated into one "found nothing" signal."""
    cache = make_cache(tmp_path)
    _seed(cache, [_row(
        publisher="DC Comics", series="Batman (Vol. 1) (1940 - 2011)",
        full_title="Batman #400", release_date="1986-10-01",
    )])
    result = cmd_collection_authority_check(
        kind="alias", name_a="Zzyzx Ranger", name_b="Ranger of Zzyzx", cache=cache
    )
    assert result["status"] == "ok"
    assert result["corpus_empty"] is False
    assert result["owned_rows_affected"] == []


# ---------------------------------------------------------------------------
# The live worked example: the X-Men #118 cross-volume ambiguity, on a
# fixture shaped like the live store (BUI-654's own acceptance check).
# ---------------------------------------------------------------------------


def test_uncanny_xmen_alias_flags_the_x118_cross_volume_ambiguity(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            publisher="Marvel Comics", series="The X-Men (Vol. 1) (1963 - 1981)",
            full_title="The X-Men #118", release_date="1978-11-14",
        ),
        _row(
            publisher="Panini Comics", series="X-Men (Vol. 2) (2001 - 2013)",
            full_title="X-Men #118", release_date="2010-10-26",
        ),
    ])
    result = cmd_collection_authority_check(
        kind="alias", name_a="Uncanny X-Men", name_b="X-Men", cache=cache
    )
    assert result["status"] == "ok"
    assert result["corpus_empty"] is False
    assert len(result["owned_rows_affected"]) == 2

    ambiguities = result["cross_volume_ambiguities"]
    assert len(ambiguities) == 1
    assert ambiguities[0]["issue"] == "118"
    dates = {row["release_date"] for row in ambiguities[0]["rows"]}
    assert dates == {"1978-11-14", "2010-10-26"}


def test_uncanny_xmen_alias_quarantined_panini_row_stops_being_flagged(tmp_path):
    """The report and the quarantine set (BUI-649) are meant to reinforce
    each other: once the Panini twin is quarantined, matchable_rows excludes
    it and the ambiguity this same entry used to flag disappears."""
    from locg.collection_cache import quarantine_marker

    cache = make_cache(tmp_path)
    us_row = _row(
        publisher="Marvel Comics", series="The X-Men (Vol. 1) (1963 - 1981)",
        full_title="The X-Men #118", release_date="1978-11-14",
    )
    panini_row = _row(
        publisher="Panini Comics", series="X-Men (Vol. 2) (2001 - 2013)",
        full_title="X-Men #118", release_date="2010-10-26",
    )
    panini_row["quarantined"] = quarantine_marker(
        reason="foreign licensed twin", ticket="BUI-649", by="test"
    )
    _seed(cache, [us_row, panini_row])

    result = cmd_collection_authority_check(
        kind="alias", name_a="Uncanny X-Men", name_b="X-Men", cache=cache
    )
    assert result["cross_volume_ambiguities"] == []
    assert len(result["owned_rows_affected"]) == 1


def test_alias_does_not_flag_legitimately_distinct_series(tmp_path):
    """Two rows that share NOTHING but both being in the alias pool — distinct
    issue numbers — must not be flagged. The check is issue-number
    collision + incompatible dates, not "this pool spans two literal names"."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            publisher="Marvel Comics", series="The X-Men (Vol. 1) (1963 - 1981)",
            full_title="The X-Men #100", release_date="1976-08-01",
        ),
        _row(
            publisher="Marvel Comics", series="Uncanny X-Men (Vol. 1) (1980 - 2011)",
            full_title="Uncanny X-Men #200", release_date="1985-12-01",
        ),
    ])
    result = cmd_collection_authority_check(
        kind="alias", name_a="Uncanny X-Men", name_b="X-Men", cache=cache
    )
    assert result["cross_volume_ambiguities"] == []
    assert len(result["owned_rows_affected"]) == 2


def test_alias_same_issue_close_dates_not_flagged():
    """Same issue number, dates within the ±1 cover/on-sale skew tolerance
    this project already uses elsewhere (BUI-214/251) — a January-cover issue
    that shipped the prior December must not misread as two eras."""
    from locg.commands import _authority_check_cross_volume_ambiguities

    rows = [
        _row(publisher="Marvel Comics", series="Foo (Vol. 1) (1999 - 2001)",
             full_title="Foo #5", release_date="1999-12-20"),
        _row(publisher="Marvel Comics", series="Foo (Vol. 2) (2000 - 2003)",
             full_title="Foo #5", release_date="2000-01-15"),
    ]
    assert _authority_check_cross_volume_ambiguities(rows) == []


# ---------------------------------------------------------------------------
# owned_rows_affected: transitive closure for alias, from/to keys for relabel.
# ---------------------------------------------------------------------------


def test_alias_pool_is_the_full_transitive_closure_not_just_the_two_names(tmp_path):
    """A candidate joining two names must report every row reachable through
    the ALREADY-SHIPPED alias graph too, not just rows under the two literal
    names — the true blast radius. Uses the real shipped Mighty Thor/Thor
    alias as the pre-existing edge the candidate extends."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(publisher="Marvel Comics", series="Thor (Vol. 1) (1966 - 1996)",
             full_title="Thor #300", release_date="1980-10-01"),
        _row(publisher="Marvel Comics", series="The Mighty Thor (Vol. 2) (1998 - 2004)",
             full_title="The Mighty Thor #30", release_date="2000-06-01"),
        _row(publisher="Marvel Comics", series="Thor God of Thunder (2013 - 2014)",
             full_title="Thor God of Thunder #1", release_date="2013-01-01"),
    ])
    # Candidate: a THIRD name joined onto the existing Mighty Thor/Thor edge.
    result = cmd_collection_authority_check(
        kind="alias", name_a="Thor God of Thunder", name_b="Thor", cache=cache
    )
    titles = {row["full_title"] for row in result["owned_rows_affected"]}
    # All three rows are reachable through the closure: Thor <-> Mighty Thor
    # (already shipped) and Thor <-> Thor God of Thunder (this candidate).
    assert titles == {"Thor #300", "The Mighty Thor #30", "Thor God of Thunder #1"}


def test_relabel_pool_is_rows_under_either_literal_spelling(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(publisher="DC Comics", series="Absolute Martian Manhunter (2025 - 2026)",
             full_title="Absolute Martian Manhunter #1", release_date="2025-03-26"),
        _row(publisher="DC Comics", series="Batman (Vol. 1) (1940 - 2011)",
             full_title="Batman #400", release_date="1986-10-01"),
    ])
    result = cmd_collection_authority_check(
        kind="relabel",
        from_name="Absolute Martian Manhunter Vol One",
        to_name="Absolute Martian Manhunter (2025 - Present)",
        cache=cache,
    )
    assert result["status"] == "ok"
    titles = {row["full_title"] for row in result["owned_rows_affected"]}
    assert titles == {"Absolute Martian Manhunter #1"}


def test_wish_list_only_rows_are_not_owned_rows_affected(tmp_path):
    """in_collection == 0 (wish/pull/read, never owned) must not count as an
    'owned row this entry makes equivalent' — this report is about
    ownership-equivalence, the buy-path concern."""
    cache = make_cache(tmp_path)
    _seed(cache, [_row(
        publisher="Marvel Comics", series="X-Men (Vol. 2) (2001 - 2013)",
        full_title="X-Men #118", release_date="2010-10-26", in_collection=0,
    )])
    result = cmd_collection_authority_check(
        kind="alias", name_a="Uncanny X-Men", name_b="X-Men", cache=cache
    )
    assert result["owned_rows_affected"] == []
