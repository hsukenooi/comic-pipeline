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
    # BUI-671: R11 must survive whatever shape the diff takes — an empty
    # corpus is empty under EITHER closure, never "newly created" noise.
    assert result["cross_volume_ambiguities_newly_created"] == []
    assert result["cross_volume_ambiguities_already_present"] == []


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


# ---------------------------------------------------------------------------
# BUI-671: newly-created vs. already-present ambiguity attribution. The
# ticket's own premise — the X-Men #118 pair collides via the bare
# normalizer alone, so a strict before/after diff would report NOTHING for
# the pair the ticket names as its acceptance example — is asserted first,
# directly against the fixture already used above (BUI-654's worked
# example).
# ---------------------------------------------------------------------------


def test_x118_ambiguity_is_already_present_not_newly_created(tmp_path):
    """BUI-654's own worked example, re-asserted for BUI-671's split: the pair
    is equivalent under bare `_normalize_series_key` alone (also — as it
    happens — already joined by the SHIPPED `Uncanny X-Men`/`X-Men` alias
    entry itself), so it must report as already-present, never as something
    this (redundant) candidate newly creates."""
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
    assert result["cross_volume_ambiguities_newly_created"] == []
    already = result["cross_volume_ambiguities_already_present"]
    assert len(already) == 1
    assert already[0]["issue"] == "118"
    # Partition: pool view == newly_created + already_present, no drops, no
    # double-counting.
    assert result["cross_volume_ambiguities"] == already


def test_alias_candidate_creates_a_genuinely_new_cross_volume_ambiguity(tmp_path):
    """Two owned rows under names that share NOTHING today — no bare-normalizer
    collision, no already-shipped alias edge — so each is alone in its own
    singleton bucket without the candidate (no ambiguity possible with only
    one row per bucket). Only THIS candidate's edge puts them in the same
    pool, so the resulting issue-number collision must report as
    newly_created, not already_present. Also the regression guard for
    _authority_check_ambiguity_diff's "never union without_buckets before
    checking" design: a buggy union-of-rows implementation would find this
    same collision by combining the two buckets and misreport it as
    already-present."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(publisher="Indie Press", series="Zorro Prime (1950 - 1955)",
             full_title="Zorro Prime #5", release_date="1950-03-01"),
        _row(publisher="Indie Press", series="Zorro Omega (2018 - 2020)",
             full_title="Zorro Omega #5", release_date="2020-05-01"),
    ])
    result = cmd_collection_authority_check(
        kind="alias", name_a="Zorro Prime", name_b="Zorro Omega", cache=cache
    )
    assert len(result["owned_rows_affected"]) == 2
    assert len(result["cross_volume_ambiguities"]) == 1
    assert result["cross_volume_ambiguities"][0]["issue"] == "5"
    assert result["cross_volume_ambiguities_already_present"] == []
    newly = result["cross_volume_ambiguities_newly_created"]
    assert len(newly) == 1
    assert newly[0]["issue"] == "5"


def test_alias_diff_fields_partition_the_pool_view(tmp_path):
    """One candidate producing BOTH kinds at once: newly_created and
    already_present together must reconstruct cross_volume_ambiguities
    exactly — no ambiguity silently dropped from either bucket, and none
    double-counted in both."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        # Issue 118: both already fold into the SAME "x men" bucket today
        # (bare normalizer + the shipped Uncanny X-Men/X-Men entry) — this
        # candidate does not touch either name, so it must land as
        # already_present.
        _row(
            publisher="Marvel Comics", series="The X-Men (Vol. 1) (1963 - 1981)",
            full_title="The X-Men #118", release_date="1978-11-14",
        ),
        _row(
            publisher="Panini Comics", series="X-Men (Vol. 2) (2001 - 2013)",
            full_title="X-Men #118", release_date="2010-10-26",
        ),
        # Issue 5: one row in the "x men" bucket above, one row in "Zorro
        # Prime"'s own bucket — the two buckets are unrelated until THIS
        # candidate joins them, so this collision must be newly_created.
        _row(
            publisher="Marvel Comics", series="Uncanny X-Men (Vol. 1) (1980 - 2011)",
            full_title="Uncanny X-Men #5", release_date="1985-01-01",
        ),
        _row(publisher="Indie Press", series="Zorro Prime (1950 - 1955)",
             full_title="Zorro Prime #5", release_date="1950-03-01"),
    ])
    result = cmd_collection_authority_check(
        kind="alias", name_a="X-Men", name_b="Zorro Prime", cache=cache
    )
    pool_issues = {amb["issue"] for amb in result["cross_volume_ambiguities"]}
    assert pool_issues == {"118", "5"}
    assert {amb["issue"] for amb in result["cross_volume_ambiguities_already_present"]} == {"118"}
    assert {amb["issue"] for amb in result["cross_volume_ambiguities_newly_created"]} == {"5"}

    split_issues = {amb["issue"] for amb in result["cross_volume_ambiguities_newly_created"]}
    split_issues |= {amb["issue"] for amb in result["cross_volume_ambiguities_already_present"]}
    assert pool_issues == split_issues
    newly_issues = {amb["issue"] for amb in result["cross_volume_ambiguities_newly_created"]}
    already_issues = {amb["issue"] for amb in result["cross_volume_ambiguities_already_present"]}
    assert newly_issues.isdisjoint(already_issues)


# ---------------------------------------------------------------------------
# Same distinction on the relabel branch — the two branches are disjoint
# readers, so the diff has to be proven independently on each.
# ---------------------------------------------------------------------------


def test_relabel_already_present_via_base_identity_fold(tmp_path):
    """The candidate's `to` name folds (bare `_identity_folds`, no relabel
    needed — BUI-560's bare-year/end-year fold) onto the SAME identity key
    as two already-owned rows that already collide on issue number with
    incompatible years. The candidate's `from` name is unrelated and owns
    nothing. The collision must report as already-present: it exists in the
    `to`-bucket alone, with no candidate in the picture."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(publisher="Boom Studios", series="Riverrun (2025)",
             full_title="Riverrun #5", release_date="2025-02-01"),
        _row(publisher="Boom Studios", series="Riverrun (2025 - Present)",
             full_title="Riverrun #5", release_date="2027-04-01"),
    ])
    result = cmd_collection_authority_check(
        kind="relabel",
        from_name="Riverrun Vol One",
        to_name="Riverrun (2025 - Present)",
        cache=cache,
    )
    assert len(result["owned_rows_affected"]) == 2
    assert len(result["cross_volume_ambiguities"]) == 1
    assert result["cross_volume_ambiguities_newly_created"] == []
    already = result["cross_volume_ambiguities_already_present"]
    assert len(already) == 1
    assert already[0]["issue"] == "5"


def test_relabel_candidate_creates_a_genuinely_new_cross_volume_ambiguity(tmp_path):
    """`from` and `to` name two owned rows with no pre-existing identity
    relationship — each alone in its own singleton bucket without the
    candidate. Only the candidate's own directed collapse puts them
    together, so the resulting issue-number collision is newly_created."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(publisher="Indie Press", series="Solar Flare (1988 - 1990)",
             full_title="Solar Flare #9", release_date="1988-09-01"),
        _row(publisher="Indie Press", series="Lunar Tide (2015 - 2017)",
             full_title="Lunar Tide #9", release_date="2016-11-01"),
    ])
    result = cmd_collection_authority_check(
        kind="relabel",
        from_name="Solar Flare (1988 - 1990)",
        to_name="Lunar Tide (2015 - 2017)",
        cache=cache,
    )
    assert len(result["owned_rows_affected"]) == 2
    assert len(result["cross_volume_ambiguities"]) == 1
    assert result["cross_volume_ambiguities_already_present"] == []
    newly = result["cross_volume_ambiguities_newly_created"]
    assert len(newly) == 1
    assert newly[0]["issue"] == "9"


# ---------------------------------------------------------------------------
# A direct unit test of the diff helper itself, isolated from the cache/
# fixture plumbing — locks down the set-difference/intersection semantics.
# ---------------------------------------------------------------------------


def test_authority_check_ambiguity_diff_set_semantics():
    from locg.commands import _authority_check_ambiguity_diff

    owned_rows = [
        _row(publisher="P", series="A (1990 - 1992)", full_title="A #1", release_date="1990-01-01"),
        _row(publisher="P", series="B (2020 - 2022)", full_title="B #1", release_date="2021-01-01"),
        _row(publisher="P", series="C (1970 - 1972)", full_title="C #2", release_date="1970-06-01"),
        _row(publisher="P", series="D (2010 - 2012)", full_title="D #2", release_date="2011-06-01"),
    ]
    # Issue "1": incompatible years (1990 vs 2021) -> an ambiguity in the
    # WITH pool. Issue "2": incompatible years (1970 vs 2011) -> also an
    # ambiguity in the WITH pool, but ALSO reproduced inside without_bucket_a
    # below (already present independent of any candidate).
    without_bucket_a = [owned_rows[2], owned_rows[3]]  # issue "2" pair, alone
    without_bucket_b = [owned_rows[0]]  # issue "1"'s first half, alone — no partner

    cross_volume_ambiguities, newly_created, already_present = _authority_check_ambiguity_diff(
        owned_rows, [without_bucket_a, without_bucket_b]
    )

    issues = {amb["issue"] for amb in cross_volume_ambiguities}
    assert issues == {"1", "2"}
    assert {amb["issue"] for amb in newly_created} == {"1"}
    assert {amb["issue"] for amb in already_present} == {"2"}
    # Exact partition — every element of the pool view lands in exactly one
    # of the two output lists.
    assert len(newly_created) + len(already_present) == len(cross_volume_ambiguities)


def test_authority_check_ambiguity_diff_attributes_by_issue_key_not_by_row():
    """Documents the chosen granularity (flagged in BUI-671 review): when a
    candidate adds a THIRD row to an issue number that already had an
    ambiguous PAIR in one without-bucket alone, the whole group — including
    the candidate's own contributed row — is classified already_present, not
    split row-by-row. This is intentional (the ticket asks for ambiguity
    GROUPS, attributed by issue key, not per-row provenance) and is locked
    down here so a future reader does not mistake it for a bug."""
    from locg.commands import _authority_check_ambiguity_diff

    row_old_1 = _row(publisher="P", series="E (1970 - 1972)", full_title="E #3", release_date="1970-01-01")
    row_old_2 = _row(publisher="P", series="F (2010 - 2012)", full_title="F #3", release_date="2011-01-01")
    row_new = _row(publisher="P", series="G (1950 - 1952)", full_title="G #3", release_date="1950-01-01")

    owned_rows = [row_old_1, row_old_2, row_new]
    without_bucket_already_ambiguous_alone = [row_old_1, row_old_2]  # issue "3" pair, alone

    _, newly_created, already_present = _authority_check_ambiguity_diff(
        owned_rows, [without_bucket_already_ambiguous_alone]
    )
    assert newly_created == []
    assert len(already_present) == 1
    assert already_present[0]["issue"] == "3"
    # All three rows — including row_new, which only reached this issue
    # number via the candidate — are folded into the single already_present
    # group.
    titles = {row["full_title"] for row in already_present[0]["rows"]}
    assert titles == {"E #3", "F #3", "G #3"}
