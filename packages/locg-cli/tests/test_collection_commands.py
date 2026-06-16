"""Tests for the collection cache CLI commands (Unit 4)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_XLSX = FIXTURES / "collection_export_sample.xlsx"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cache(tmp_path: Path):
    from locg.collection_cache import CollectionCache
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _agent_win_row(
    publisher: str = "Marvel Comics",
    series: str = "Amazing Spider-Man (1963 - 1998)",
    full_title: str = "Amazing Spider-Man #300",
    release_date: str = "1988-05-10",
    price_paid: float = 42.00,
    date_purchased: str = "2026-05-22",
    pushed: str | None = None,
    needs_variant: bool = False,
    needs_series: bool = False,
    gixen_item_id: str = "99",
) -> dict[str, Any]:
    return {
        "publisher_name": publisher,
        "series_name": series,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": 1,
        "in_wish_list": 0,
        "marked_read": 0,
        "my_rating": None,
        "media_format": "Print",
        "price_paid": price_paid,
        "date_purchased": date_purchased,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": "eBay",
        "signature": 0,
        "slabbing": 0,
        "grading": None,
        "grading_company": None,
        "local_added_at": "2026-05-22T10:00:00.000000Z",
        "local_added_seq": 1,
        "pushed_to_locg_at": pushed,
        "last_seen_in_export_at": None,
        "source": "agent_win",
        "needs_manual_variant": needs_variant,
        "needs_manual_series_canonical": needs_series,
        "metron_id": None,
        "gixen_item_id": gixen_item_id,
        "previous_full_title": None,
    }


def _seed_cache(cache, rows: list[dict[str, Any]]) -> None:
    def mutate(payload):
        payload["comics"].extend(rows)
    cache.apply(mutate, command="seed")


# ---------------------------------------------------------------------------
# cmd_collection_import
# ---------------------------------------------------------------------------

def test_import_success_returns_added_count(tmp_path, monkeypatch):
    from locg.collection_cache import CollectionCache
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    # Import the real fixture
    result = cmds.cmd_collection_import(str(SAMPLE_XLSX))
    assert result["added"] > 0
    assert result["updated"] == 0


def test_import_nonexistent_file_raises(tmp_path, monkeypatch):
    import locg.commands as cmds
    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    with pytest.raises((FileNotFoundError, RuntimeError, OSError)):
        cmds.cmd_collection_import(str(tmp_path / "does_not_exist.xlsx"))


def test_import_migration_in_progress_raises(tmp_path, monkeypatch):
    """Import raises when migration_in_progress=True and last_full_import differs from .bak.0."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    # Seed a first import so .bak.0 has a last_full_import value
    cmds.cmd_collection_import(str(SAMPLE_XLSX))

    # Manually corrupt the cache to simulate a crashed import
    payload = cache.load()
    payload["migration_in_progress"] = True
    payload["last_full_import"] = "9999-01-01T00:00:00Z"  # differs from .bak.0
    import json as _json
    (tmp_path / "collection.json").write_text(
        _json.dumps(payload, separators=(",", ":"))
    )

    with pytest.raises(RuntimeError, match="crashed"):
        cmds.cmd_collection_import(str(SAMPLE_XLSX))


# ---------------------------------------------------------------------------
# cmd_collection_export
# ---------------------------------------------------------------------------

def test_export_returns_paths_and_counts(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result["ready_count"] == 1
    assert result["manual_variant_count"] == 0
    assert result["manual_series_count"] == 0
    assert Path(result["csv_path"]).exists()
    assert Path(result["notes_md_path"]).exists()


def test_export_empty_pending_returns_zero(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    # Import from LOCG — all rows get pushed_to_locg_at set, so nothing pending
    cmds.cmd_collection_import(str(SAMPLE_XLSX))
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))
    assert result["ready_count"] == 0


def test_export_manual_rows_excluded_from_csv(tmp_path, monkeypatch):
    """Rows flagged needs_manual_variant appear in notes.md but not in CSV body."""
    import csv
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(full_title="ASM #300", needs_variant=False),
        _agent_win_row(full_title="ASM #300 Newsstand", needs_variant=True, gixen_item_id="100"),
    ])

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result["ready_count"] == 1
    assert result["manual_variant_count"] == 1

    with open(out_csv, newline="") as f:
        body_rows = list(csv.reader(f))[1:]  # skip header
    titles = [r[2] for r in body_rows]  # Full Title is column index 2
    assert "ASM #300" in titles
    assert "ASM #300 Newsstand" not in titles

    notes_text = Path(result["notes_md_path"]).read_text()
    assert "ASM #300 Newsstand" in notes_text


def test_export_includes_local_only_wish_add(tmp_path, monkeypatch):
    """A local-only wish add that isn't owned appears in the CSV with
    In Collection=0, In Wish List=1 (BUI-122: derived wishes are excluded —
    LOCG already has them — but genuine new local adds still push)."""
    import csv
    import locg.collection_io as cio
    import locg.commands as cmds

    wish_path = cio.wish_list_cache_path()
    wish_path.parent.mkdir(parents=True, exist_ok=True)
    wish_path.write_text(json.dumps({
        "updated_at": "2026-05-22T00:00:00+00:00",
        "items": [
            # local-only add (no series_name) — the diff LOCG doesn't have yet
            {"name": "Saga #1", "id": None},
        ],
    }))

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result["wish_list_count"] == 1
    assert result["ready_count"] == 0

    with open(out_csv, newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["Full Title"] == "Saga #1"
    assert rows[0]["In Collection"] == "0"
    assert rows[0]["In Wish List"] == "1"


def test_export_collection_and_wish_list_combined(tmp_path, monkeypatch):
    """Collection rows and wish-list rows both appear in the same CSV."""
    import csv
    import locg.collection_io as cio
    import locg.commands as cmds

    wish_path = cio.wish_list_cache_path()
    wish_path.parent.mkdir(parents=True, exist_ok=True)
    wish_path.write_text(json.dumps({
        "updated_at": "2026-05-22T00:00:00+00:00",
        "items": [{"name": "Batman #1", "id": None}],
    }))

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(full_title="Amazing Spider-Man #300")])

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result["ready_count"] == 1
    assert result["wish_list_count"] == 1

    with open(out_csv, newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    by_title = {r["Full Title"]: r for r in rows}
    assert by_title["Amazing Spider-Man #300"]["In Collection"] == "1"
    assert by_title["Amazing Spider-Man #300"]["In Wish List"] == "0"
    assert by_title["Batman #1"]["In Collection"] == "0"
    assert by_title["Batman #1"]["In Wish List"] == "1"


# ---------------------------------------------------------------------------
# cmd_collection_status
# ---------------------------------------------------------------------------

def test_status_empty_cache(tmp_path, monkeypatch):
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    result = cmds.cmd_collection_status()
    assert result["last_full_import"] is None
    assert result["row_count"] == 0
    assert "locg_cli_version" in result
    assert "schema_version" in result


def test_status_populated_cache(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    cmds.cmd_collection_import(str(SAMPLE_XLSX))
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_status()
    assert result["last_full_import"] is not None
    assert result["row_count"] > 0


def test_status_verbose_returns_extended_metrics(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    cmds.cmd_collection_import(str(SAMPLE_XLSX))
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_status(verbose=True)
    for key in (
        "agent_win_count",
        "locg_export_count",
        "needs_manual_variant_count",
        "needs_manual_series_canonical_count",
        "median_agent_win_age_days",
        "reconciliation_success_rate_last_5_imports",
        "behavioral_drift_events_last_5_imports",
    ):
        assert key in result


# ---------------------------------------------------------------------------
# cmd_collection_check
# ---------------------------------------------------------------------------

def test_check_hit_returns_in_collection(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])

    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="300"
    )
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Amazing Spider-Man #300"


def test_check_matches_across_leading_article(tmp_path, monkeypatch):
    """A 'The Incredible Hulk' cache row is found by an 'Incredible Hulk' query.

    Regression for BUI-45: identify drops the leading article, so the McFarlane
    run the user already owned slipped past collection-check and got sniped.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Incredible Hulk (Vol. 2) (1968 - 1999)",
        full_title="The Incredible Hulk #341",
        release_date="1987-11-17",
    )])

    result = cmds.cmd_collection_check(series="Incredible Hulk", issue="341")
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "The Incredible Hulk #341"


def test_check_miss_returns_not_in_cache(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="999")
    assert result["match_status"] == "not_in_cache"
    assert result["full_title_matched"] is None


def test_check_year_is_per_issue_cover_year_not_series_start(tmp_path, monkeypatch):
    """`year` gates on the issue's release_date, so passing a long-running
    series' start year (year_began) wrongly filters out owned mid-run issues.

    Regression for BUI-129: forwarding Metron's `year_began` (1963 for X-Men)
    returned a false `not_in_cache` for issues that actually shipped years later.
    The matcher is behaving as designed (per-issue year gate); the fix is that
    callers must pass the per-issue cover year or omit `year` entirely.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    # X-Men #137 shipped 1980, but the series began in 1963.
    _seed_cache(cache, [_agent_win_row(
        series="Uncanny X-Men (1963 - 2011)",
        full_title="Uncanny X-Men #137",
        release_date="1980-09-01",
    )])

    # The wrong-year call (series start year) misses every mid-run issue.
    wrong = cmds.cmd_collection_check(
        series="Uncanny X-Men", issue="137", year="1963"
    )
    assert wrong["match_status"] == "not_in_cache"

    # Omitting year (the BUI-129 caller fix) finds the owned issue.
    omitted = cmds.cmd_collection_check(series="Uncanny X-Men", issue="137")
    assert omitted["match_status"] == "in_collection"
    assert omitted["full_title_matched"] == "Uncanny X-Men #137"

    # Passing the correct per-issue cover year also finds it.
    correct = cmds.cmd_collection_check(
        series="Uncanny X-Men", issue="137", year="1980"
    )
    assert correct["match_status"] == "in_collection"


# ---------------------------------------------------------------------------
# cmd_collection_series_names (BUI-129)
# ---------------------------------------------------------------------------

def test_series_names_returns_sorted_canonical_names(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    def mutate(payload):
        payload["series_name_index"] = {
            "uncanny x-men": "Uncanny X-Men",
            "amazing spider-man": "The Amazing Spider-Man",
            "batman": "Batman",
        }
    cache.apply(mutate, command="seed")

    result = cmds.cmd_collection_series_names()
    # Sorted case-insensitively by the literal name ("Batman" < "The Amazing…").
    assert result["series_names"] == [
        "Batman",
        "The Amazing Spider-Man",
        "Uncanny X-Men",
    ]
    assert result["count"] == 3


def test_series_names_empty_cache_returns_empty(tmp_path, monkeypatch):
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    result = cmds.cmd_collection_series_names()
    assert result == {"series_names": [], "count": 0}


def test_check_empty_cache_returns_not_in_cache(tmp_path, monkeypatch):
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    result = cmds.cmd_collection_check(series="Batman", issue="1")
    assert result["match_status"] == "not_in_cache"
    assert result["cache_age_days"] is None


def test_check_includes_cache_age_days(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    cmds.cmd_collection_import(str(SAMPLE_XLSX))
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_check(series="Nonexistent", issue="1")
    assert result["match_status"] == "not_in_cache"
    # cache_age_days should be 0 (just imported)
    assert result["cache_age_days"] is not None
    assert result["cache_age_days"] >= 0


# ---------------------------------------------------------------------------
# cmd_collection_check — BUI-26 matcher regressions
# ---------------------------------------------------------------------------

def test_check_rejects_substring_issue_match(tmp_path, monkeypatch):
    """Issue '2' must not match '#32'/'#12'/'#222' (BUI-26 bug B).

    The old fallback did `issue in full_title`, so a check for #2 matched any
    title containing a '2' and reported owned books the user did not own.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Fantastic Four (Vol. 1) (1961 - 1996)",
        full_title="Fantastic Four #32",
    )])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="2")
    assert result["match_status"] == "not_in_cache"
    assert result["full_title_matched"] is None


def test_check_rejects_annual_for_base_series_query(tmp_path, monkeypatch):
    """A plain 'Fantastic Four #6' query must not match 'Fantastic Four Annual #6'.

    Annuals are filed under the base series_name with the qualifier in the
    full_title; the matcher must keep them distinct (BUI-26 bug C / GSFF).
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Fantastic Four (Vol. 1) (1961 - 1996)",
        full_title="Fantastic Four Annual #6",
    )])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="6")
    assert result["match_status"] == "not_in_cache"


def test_check_matches_annual_by_qualified_name(tmp_path, monkeypatch):
    """The annual is still findable when queried by its qualified name."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Fantastic Four (Vol. 1) (1961 - 1996)",
        full_title="Fantastic Four Annual #6",
    )])

    result = cmds.cmd_collection_check(series="Fantastic Four Annual", issue="6")
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Fantastic Four Annual #6"


def test_check_giant_size_not_confused_with_annual(tmp_path, monkeypatch):
    """Giant-Size Fantastic Four must not match a Fantastic Four Annual row.

    Folds in the GSFF false-positive: collection-check wrongly reported
    Giant-Size Fantastic Four as owned by conflating it with FF Annual.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Fantastic Four (Vol. 1) (1961 - 1996)",
        full_title="Fantastic Four Annual #2",
    )])

    result = cmds.cmd_collection_check(series="Giant-Size Fantastic Four", issue="2")
    assert result["match_status"] == "not_in_cache"


def test_check_ignores_unowned_rows(tmp_path, monkeypatch):
    """in_collection is a copies-owned count; 0 means not owned (BUI-26 bug D).

    A wish-list/pull row (in_collection=0) must not report as in_collection,
    while a multi-copy row (in_collection=2) still counts as owned.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    unowned = _agent_win_row(series="Batman (Vol. 1)", full_title="Batman #100")
    unowned["in_collection"] = 0
    multi = _agent_win_row(series="Detective Comics (Vol. 1)", full_title="Detective Comics #27")
    multi["in_collection"] = 2
    _seed_cache(cache, [unowned, multi])

    assert cmds.cmd_collection_check(series="Batman", issue="100")["match_status"] == "not_in_cache"
    assert cmds.cmd_collection_check(series="Detective Comics", issue="27")["match_status"] == "in_collection"


def test_check_mighty_thor_masthead_alias(tmp_path, monkeypatch):
    """'The Mighty Thor #154' (cover title, 1968) resolves to the owned
    'Thor #154' via the year-gated masthead alias (BUI-46).

    This was the original BUI-26 false negative — the comic that got sniped
    while owned because identify reports the cover masthead, not the catalog name.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 1) (1966 - 1996)",
        full_title="Thor #154",
        release_date="1968-05-02",
    )])

    # The catalog name works directly:
    assert cmds.cmd_collection_check(series="Thor", issue="154")["match_status"] == "in_collection"
    # The cover/masthead name resolves via the year-gated alias:
    r = cmds.cmd_collection_check(series="The Mighty Thor", issue="154", year="1968")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Thor #154"
    # Without a year the alias does NOT fire — offline safety, since the masthead
    # is shared with the distinct 'The Mighty Thor (Vol. 3)' 2015 series:
    assert cmds.cmd_collection_check(
        series="The Mighty Thor", issue="154"
    )["match_status"] == "not_in_cache"


def test_check_masthead_alias_year_gate_prevents_collision(tmp_path, monkeypatch):
    """The year gate stops a wrong-era masthead query from matching the owned
    Vol-1 issue, protecting the distinct Vol-3 series (BUI-46)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 1) (1966 - 1996)",
        full_title="Thor #5",
        release_date="1966-01-02",
    )])

    # Owns Thor (Vol. 1) #5 (1966); a "The Mighty Thor #5" query for the 2016
    # (Vol. 3) era must NOT report it as owned.
    assert cmds.cmd_collection_check(
        series="The Mighty Thor", issue="5", year="2016"
    )["match_status"] == "not_in_cache"


# --- BUI-105: dateless owned rows must survive the year gate ---

def test_check_year_gate_matches_dateless_owned_row(tmp_path, monkeypatch):
    """A year-gated check finds an owned row that has no release_date.

    Regression for BUI-105: an index-resolved record-win written before its
    date was stamped has release_date=None. collection-check always passes
    --year, and the old year filter excluded any row whose release_date did
    not start with that year — so a just-won book read as 'not in collection'
    and risked a duplicate snipe. A dateless row must now pass the year gate.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #300",
        release_date=None,
    )])

    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="300", year="1988"
    )
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Amazing Spider-Man #300"


def test_check_year_gate_still_rejects_wrong_dated_row(tmp_path, monkeypatch):
    """The relaxed year gate only spares *dateless* rows — a row with a
    release_date that disagrees with the queried year is still rejected, so
    BUI-105 doesn't reopen the volume-disambiguation the year gate provides."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #1",
        release_date="1963-03-01",
    )])

    # Owns the 1963 Amazing Spider-Man #1; a 2018-era query must still miss.
    assert cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="1", year="2018"
    )["match_status"] == "not_in_cache"


# ---------------------------------------------------------------------------
# cmd_collection_doctor
# ---------------------------------------------------------------------------

def test_doctor_empty_cache_returns_ready_false(tmp_path, monkeypatch):
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    result = cmds.cmd_collection_doctor()
    assert result["ready"] is False
    assert "setup_steps" in result
    assert len(result["setup_steps"]) > 0
    assert "next_action" in result
    assert result["status"]["last_full_import"] is None


def test_doctor_populated_cache_returns_ready_true(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    cmds.cmd_collection_import(str(SAMPLE_XLSX))
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_doctor()
    assert result["ready"] is True
    assert result["status"]["row_count"] > 0


def test_doctor_steps_have_required_keys(tmp_path, monkeypatch):
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    result = cmds.cmd_collection_doctor()
    for step in result["setup_steps"]:
        assert "step" in step
        assert "title" in step
        assert "instruction" in step


# ---------------------------------------------------------------------------
# Integration: full pipeline — doctor → import → status → check → export
# ---------------------------------------------------------------------------

def test_full_pipeline(tmp_path, monkeypatch):
    """Empty cache → doctor (not ready) → import → status (row_count > 0) →
    check (miss on absent, in_collection on present) → export (empty pending queue)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    # doctor: empty cache
    doc = cmds.cmd_collection_doctor()
    assert doc["ready"] is False

    # import
    imp = cmds.cmd_collection_import(str(SAMPLE_XLSX))
    assert imp["added"] > 0

    # Reset monkeypatch (CollectionCache is stateless; same cache instance)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    # status --verbose
    status = cmds.cmd_collection_status(verbose=True)
    assert status["row_count"] > 0
    assert "locg_export_count" in status

    # check — a title known to be in the fixture
    # "1963 #6" by Image Comics is in the sample xlsx (first row)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    hit = cmds.cmd_collection_check(series="1963", issue="6")
    assert hit["match_status"] == "in_collection"

    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    miss = cmds.cmd_collection_check(series="Nonexistent Series", issue="999")
    assert miss["match_status"] == "not_in_cache"

    # export — all rows were imported from LOCG, so none pending
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    out_csv = tmp_path / "export.csv"
    exp = cmds.cmd_collection_export(str(out_csv))
    assert exp["ready_count"] == 0


# ---------------------------------------------------------------------------
# cmd_collection_record_win (Unit 6)
# ---------------------------------------------------------------------------

def _make_win(
    item_id: str = "1001",
    series: str = "Amazing Spider-Man",
    issue: str = "300",
    year: int | None = 1988,
    variant_text: str | None = None,
    current_bid: float = 42.00,
    end_date_iso: str = "2026-05-20T15:00:00Z",
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "current_bid": current_bid,
        "end_date_iso": end_date_iso,
        "identify_data": {
            "series": series,
            "issue": issue,
            "year": year,
            "variant_text": variant_text,
        },
    }


def _null_metron():
    """MetronClient stub that always returns None (no Metron hits)."""
    from unittest.mock import MagicMock
    m = MagicMock()
    m.lookup_issue.return_value = None
    return m


def _metron_hit(series_name: str = "Amazing Spider-Man (1963 - 1998)", year_began: int = 1963, year_end: int | None = 1998):
    """MetronClient stub that returns a successful lookup."""
    from unittest.mock import MagicMock
    m = MagicMock()
    m.lookup_issue.return_value = {
        "metron_id": 999,
        "cover_date": "1988-05-10",
        "store_date": None,
        "series_year_began": year_began,
        "series_year_end": year_end,
        "series_name": series_name.split(" (")[0],
        "series_id": 42,
    }
    m.format_series_name.return_value = series_name
    return m


# --- R36: series resolution chain ---

def test_record_win_series_from_index(tmp_path):
    """Series in series_name_index → no Metron call, correct canonical name."""
    from locg.commands import cmd_collection_record_win
    from locg.collection_cache import CollectionCache

    cache = make_cache(tmp_path)
    # Seed a locg_export row so the series_name_index gets built
    _seed_cache(cache, [{
        **_agent_win_row(series="Amazing Spider-Man (1963 - 1998)", full_title="Amazing Spider-Man #1"),
        "source": "locg_export",
    }])
    # Rebuild index (import triggers this; here we do it manually)
    from locg.collection_cache import rebuild_series_name_index
    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")

    metron = _null_metron()
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man (1963 - 1998)", issue="300")],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    assert result["manual_series_count"] == 0
    assert result["metron_lookups_attempted"] == 0  # index hit, no Metron needed
    metron.lookup_issue.assert_not_called()

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["series_name"] == "Amazing Spider-Man (1963 - 1998)"
    assert row["source"] == "agent_win"
    # BUI-105: no Metron date on the index path, so stamp a best-effort
    # release_date from the identify year (Jan 1) instead of leaving it None.
    assert row["release_date"] == "1988-01-01"


def test_record_win_index_path_found_by_year_gated_check(tmp_path, monkeypatch):
    """Acceptance (BUI-105): a record-win resolved via series_name_index is
    reported in_collection by a subsequent year-gated collection-check."""
    import locg.commands as cmds
    from locg.collection_cache import rebuild_series_name_index

    cache = make_cache(tmp_path)
    # Seed a locg_export row so the series_name_index resolves the series
    # without any Metron call (the no-Metron path that drops the date).
    _seed_cache(cache, [{
        **_agent_win_row(series="Amazing Spider-Man (1963 - 1998)", full_title="Amazing Spider-Man #1"),
        "source": "locg_export",
    }])
    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")

    cmds.cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man (1963 - 1998)", issue="300", year=1988)],
        cache=cache,
        metron=_null_metron(),
    )

    # The win's title carries the canonical series name (with its date range),
    # as record-win builds it from the index value; the matcher strips the
    # parenthetical via _normalize_series_key, so the bare-series query matches.
    assert cache.load()["comics"][-1]["full_title"] == "Amazing Spider-Man (1963 - 1998) #300"

    # collection-check always passes --year; the just-won book must be found.
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="300", year="1988"
    )
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Amazing Spider-Man (1963 - 1998) #300"


def test_record_win_index_path_dateless_when_no_year(tmp_path):
    """When the win carries no identify year, the index path leaves the row
    dateless — the relaxed year gate (BUI-105) still lets a later check match."""
    import locg.commands as cmds
    from locg.collection_cache import rebuild_series_name_index

    cache = make_cache(tmp_path)
    _seed_cache(cache, [{
        **_agent_win_row(series="Amazing Spider-Man (1963 - 1998)", full_title="Amazing Spider-Man #1"),
        "source": "locg_export",
    }])
    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")

    cmds.cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man (1963 - 1998)", issue="300", year=None)],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["release_date"] is None


def test_record_win_series_from_metron(tmp_path):
    """Series not in index but Metron succeeds → canonical name from Metron."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)")
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300")],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    assert result["manual_series_count"] == 0
    assert result["metron_lookups_attempted"] == 1
    assert result["metron_lookups_succeeded"] == 1

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["series_name"] == "Amazing Spider-Man (1963 - 1998)"
    assert row["needs_manual_series_canonical"] is False


def test_record_win_series_manual_fallback(tmp_path):
    """Series not in index, Metron returns None → manual flag set."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    result = cmd_collection_record_win(
        [_make_win(series="Obscure Series", issue="1")],
        cache=cache,
        metron=_null_metron(),
    )

    assert result["rows_written"] == 1
    assert result["manual_series_count"] == 1

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["series_name"] == "Obscure Series"
    assert row["needs_manual_series_canonical"] is True


# --- R32: variant handling ---

def test_record_win_newsstand_suffix(tmp_path):
    """Known variant text 'newsstand' → 'Newsstand Edition' suffix, no flag."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)")
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", variant_text="newsstand")],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    assert result["manual_variant_count"] == 0

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["full_title"].endswith("Newsstand Edition")
    assert row["needs_manual_variant"] is False


def test_record_win_unknown_variant_flags(tmp_path):
    """Unknown variant text → needs_manual_variant=True."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", variant_text="mark jeweler")],
        cache=cache,
        metron=_null_metron(),
    )

    assert result["manual_variant_count"] == 1
    payload = cache.load()
    row = payload["comics"][-1]
    assert row["needs_manual_variant"] is True


def test_record_win_no_variant_no_flag(tmp_path):
    """No variant text → no manual flag."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="1", variant_text=None)],
        cache=cache,
        metron=_null_metron(),
    )

    assert result["manual_variant_count"] == 0
    payload = cache.load()
    row = payload["comics"][-1]
    assert row["needs_manual_variant"] is False


# --- BUI-33: Metron variant resolution ---

def test_fuzzy_variant_match_exact_ish():
    from locg.commands import _fuzzy_variant_match
    names = ["Capullo Variant", "Todd McFarlane Cover"]
    assert _fuzzy_variant_match("capullo variant", names) == "Capullo Variant"


def test_fuzzy_variant_match_across_abbreviation():
    from locg.commands import _fuzzy_variant_match
    # auction text uses "ASM 299"; Metron spells out the series
    names = ["Amazing Spider-Man #299 Homage Virgin Variant", "Direct Edition"]
    assert _fuzzy_variant_match(
        "asm 299 homage virgin variant", names
    ) == "Amazing Spider-Man #299 Homage Virgin Variant"


def test_fuzzy_variant_match_rejects_generic_only_overlap():
    from locg.commands import _fuzzy_variant_match
    # only the generic word "variant"/"cover" overlaps — must not match
    assert _fuzzy_variant_match("capullo variant", ["Skan Cover Variant"]) is None


def test_fuzzy_variant_match_no_names():
    from locg.commands import _fuzzy_variant_match
    assert _fuzzy_variant_match("capullo variant", []) is None


def _metron_with_variants(series_name: str, variants: list[str]):
    """Metron stub: series lookup hits, and issue-detail returns variant names."""
    from unittest.mock import MagicMock
    m = MagicMock()
    m.lookup_issue.return_value = {
        "metron_id": 777,
        "cover_date": "1992-05-01",
        "store_date": None,
        "series_year_began": 1992,
        "series_year_end": None,
        "series_name": series_name.split(" (")[0],
        "series_id": 5,
    }
    m.format_series_name.return_value = series_name
    m.lookup_issue_detail.return_value = {"variants": variants}
    return m


def test_record_win_metron_variant_match(tmp_path):
    """Unknown variant text resolves via Metron issue-detail fuzzy match."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_with_variants("Spawn (1992 - Present)", ["Capullo Variant", "Direct Edition"])
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", variant_text="Capullo Variant")],
        cache=cache,
        metron=metron,
    )

    assert result["metron_variant_lookups_attempted"] == 1
    assert result["metron_variant_matches"] == 1
    assert result["manual_variant_count"] == 0

    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Spawn (1992 - Present) #313 Capullo Variant"
    assert row["needs_manual_variant"] is False


def test_record_win_metron_variant_no_match_flags(tmp_path):
    """Metron has the issue but no matching variant → still flagged manual."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_with_variants("Spawn (1992 - Present)", ["Some Unrelated Cover"])
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", variant_text="Capullo Variant")],
        cache=cache,
        metron=metron,
    )

    assert result["metron_variant_lookups_attempted"] == 1
    assert result["metron_variant_matches"] == 0
    assert result["manual_variant_count"] == 1
    assert cache.load()["comics"][-1]["needs_manual_variant"] is True


def test_record_win_no_variant_skips_detail_lookup(tmp_path):
    """A known-suffix variant must not trigger a Metron issue-detail call."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_with_variants("Spawn (1992 - Present)", ["Capullo Variant"])
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", variant_text="newsstand")],
        cache=cache,
        metron=metron,
    )
    assert result["metron_variant_lookups_attempted"] == 0
    metron.lookup_issue_detail.assert_not_called()
    assert cache.load()["comics"][-1]["full_title"].endswith("Newsstand Edition")


# --- BUI-34: dedup already-owned wins ---

def _seed_owned_spawn(cache, full_title="Spawn #98", in_collection=1):
    from locg.collection_cache import rebuild_series_name_index
    _seed_cache(cache, [{
        **_agent_win_row(series="Spawn (1992 - Present)", full_title=full_title),
        "in_collection": in_collection,
        "source": "locg_export",
    }])
    cache.apply(
        lambda p: p.__setitem__("series_name_index", rebuild_series_name_index(p)),
        command="test-rebuild",
    )


def test_record_win_skips_already_owned(tmp_path):
    """A win for an issue already owned in the cache is skipped, not duplicated."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_spawn(cache, "Spawn #98")

    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="98")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0
    assert result["skipped_already_owned_titles"] == ["Spawn (1992 - Present) #98"]
    # No new agent_win row written
    assert [r for r in cache.load()["comics"] if r["source"] == "agent_win"] == []


def test_record_win_writes_genuinely_new_issue(tmp_path):
    """A win for a different issue of an owned series is still written."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_spawn(cache, "Spawn #98")

    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="99")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


def test_record_win_unowned_row_not_skipped(tmp_path):
    """A cache row with in_collection=0 (wish-list/not owned) does not block a win."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_spawn(cache, "Spawn #98", in_collection=0)

    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="98")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


def test_record_win_dedup_ignores_variant(tmp_path):
    """A variant win for an already-owned issue is still deduped (series+issue)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_spawn(cache, "Spawn #313")

    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", variant_text="Capullo Variant")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0


def test_record_win_dedup_does_not_conflate_annual(tmp_path):
    """Owning 'Spawn Annual #1' must not skip a plain 'Spawn #1' win."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_spawn(cache, "Spawn Annual #1")

    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="1")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


# --- Tracking fields ---

def test_record_win_tracking_fields(tmp_path):
    """Written row has expected tracking fields."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    cmd_collection_record_win(
        [_make_win(item_id="XYZ", current_bid=42.50, end_date_iso="2026-05-20T15:00:00Z")],
        cache=cache,
        metron=_null_metron(),
    )

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["source"] == "agent_win"
    assert row["gixen_item_id"] == "XYZ"
    assert row["price_paid"] == 42.50
    assert row["date_purchased"] == "2026-05-20"
    assert row["pushed_to_locg_at"] is None
    assert row["in_collection"] == 1
    assert row["in_wish_list"] == 0
    assert row["marked_read"] == 0
    assert row["my_rating"] is None
    assert row["local_added_at"] is not None
    assert row["local_added_seq"] is not None


def test_record_win_metron_release_date_store_date_preferred(tmp_path):
    """store_date takes priority over cover_date for release_date."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 1,
        "cover_date": "1988-05-01",
        "store_date": "1988-03-15",
        "series_year_began": 1963,
        "series_year_end": 1998,
        "series_name": "Amazing Spider-Man",
        "series_id": 1,
    }
    metron.format_series_name.return_value = "Amazing Spider-Man (1963 - 1998)"

    cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300")],
        cache=cache,
        metron=metron,
    )

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["release_date"] == "1988-03-15"


def test_record_win_metron_no_store_date_uses_cover_date(tmp_path):
    """When store_date is absent, cover_date is used for release_date."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 1,
        "cover_date": "1988-05-01",
        "store_date": None,
        "series_year_began": 1963,
        "series_year_end": 1998,
        "series_name": "Amazing Spider-Man",
        "series_id": 1,
    }
    metron.format_series_name.return_value = "Amazing Spider-Man (1963 - 1998)"

    cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300")],
        cache=cache,
        metron=metron,
    )

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["release_date"] == "1988-05-01"


def test_record_win_metron_no_dates_blank_release_date(tmp_path):
    """When Metron returns no dates, release_date is None (no needs_manual_variant)."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 1,
        "cover_date": None,
        "store_date": None,
        "series_year_began": 1963,
        "series_year_end": 1998,
        "series_name": "Amazing Spider-Man",
        "series_id": 1,
    }
    metron.format_series_name.return_value = "Amazing Spider-Man (1963 - 1998)"

    cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300")],
        cache=cache,
        metron=metron,
    )

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["release_date"] is None
    assert row["needs_manual_variant"] is False  # R66: blank date → no variant flag


# --- Duplicate detection ---

def test_record_win_metron_credential_error_disables_metron(tmp_path):
    """MetronCredentialError on first call disables Metron for rest of batch → all manual."""
    from locg.commands import cmd_collection_record_win
    from locg.metron import MetronCredentialError
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.side_effect = MetronCredentialError("no creds")

    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", series="Ghost Rider", issue="1"),
            _make_win(item_id="2", series="Ghost Rider", issue="2"),
        ],
        cache=cache,
        metron=metron,
    )

    # Both fall through to manual; Metron called only once (disabled after first error)
    assert result["rows_written"] == 2
    assert result["manual_series_count"] == 2
    assert metron.lookup_issue.call_count == 1


def test_record_win_duplicate_gixen_id_updates_not_inserts(tmp_path):
    """Same gixen_item_id recorded twice → second write updates, not duplicates."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    win = _make_win(item_id="DUPE", series="Spawn", issue="1")

    cmd_collection_record_win([win], cache=cache, metron=_null_metron())
    cmd_collection_record_win([win], cache=cache, metron=_null_metron())

    payload = cache.load()
    dupes = [r for r in payload["comics"] if r.get("gixen_item_id") == "DUPE"]
    assert len(dupes) == 1


# --- Monotonic seq ---

def test_record_win_same_timestamp_gets_distinct_seq(tmp_path):
    """Two wins in the same batch have distinct local_added_seq values."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id="A", series="X-Men", issue="1"),
        _make_win(item_id="B", series="X-Men", issue="2"),
    ]
    cmd_collection_record_win(wins, cache=cache, metron=_null_metron())

    payload = cache.load()
    rows = [r for r in payload["comics"] if r.get("gixen_item_id") in ("A", "B")]
    seqs = [r["local_added_seq"] for r in rows]
    assert len(set(seqs)) == 2  # distinct


# --- Chunked commit ---

def test_record_win_chunks_60_rows_into_3_commits(tmp_path, monkeypatch):
    """60-row batch commits in 3 chunks of 25/25/10."""
    from locg.commands import cmd_collection_record_win, RECORD_WIN_CHUNK_SIZE

    assert RECORD_WIN_CHUNK_SIZE == 25

    cache = make_cache(tmp_path)
    wins = [_make_win(item_id=str(i), series="X-Men", issue=str(i)) for i in range(60)]

    result = cmd_collection_record_win(wins, cache=cache, metron=_null_metron())

    assert result["rows_written"] == 60
    assert result["chunks_committed"] == 3
    assert result["partial_failure"] is False


def test_record_win_partial_failure_marks_flag(tmp_path):
    """If a chunk commit raises, partial_failure=True and other chunks still commit."""
    from locg.commands import cmd_collection_record_win, RECORD_WIN_CHUNK_SIZE

    cache = make_cache(tmp_path)

    call_count = 0
    original_write_wins = cache.write_wins

    def patched_write_wins(rows, command="record-win"):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated crash")
        return original_write_wins(rows, command=command)

    cache.write_wins = patched_write_wins

    wins = [_make_win(item_id=str(i), series="X-Men", issue=str(i)) for i in range(50)]
    result = cmd_collection_record_win(wins, cache=cache, metron=_null_metron())

    assert result["partial_failure"] is True
    # First chunk (25) committed, second (25) failed
    assert result["chunks_committed"] == 1
    assert result["rows_written"] == 25


# --- Integration: 5-win batch ---

def test_record_win_integration_mix(tmp_path):
    """5-win batch: cache-hit / metron-hit / manual-fallback mix."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)

    # Seed a locg_export row so index has one entry
    _seed_cache(cache, [{
        **_agent_win_row(series="Amazing Spider-Man (1963 - 1998)", full_title="Amazing Spider-Man #1"),
        "source": "locg_export",
    }])
    from locg.collection_cache import rebuild_series_name_index
    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")

    # Metron returns hit for Spawn, None for unknown
    metron = MagicMock()
    def metron_lookup(series_query, issue_number, year=None):
        if "Spawn" in series_query:
            return {
                "metron_id": 5,
                "cover_date": "1992-05-01",
                "store_date": None,
                "series_year_began": 1992,
                "series_year_end": None,
                "series_name": "Spawn",
                "series_id": 5,
            }
        return None
    metron.lookup_issue.side_effect = metron_lookup
    metron.format_series_name.side_effect = lambda d: f"{d['series_name']} ({d['series_year_began']} - Present)"

    wins = [
        _make_win(item_id="1", series="Amazing Spider-Man (1963 - 1998)", issue="300"),  # index hit
        _make_win(item_id="2", series="Spawn", issue="1"),                               # metron hit
        _make_win(item_id="3", series="Obscure Series A", issue="1"),                   # manual
        _make_win(item_id="4", series="Amazing Spider-Man (1963 - 1998)", issue="301"), # index hit
        _make_win(item_id="5", series="Obscure Series B", issue="2"),                   # manual
    ]

    result = cmd_collection_record_win(wins, cache=cache, metron=metron)

    assert result["rows_written"] == 5
    assert result["chunks_committed"] == 1
    assert result["manual_series_count"] == 2   # series A and B
    assert result["metron_lookups_attempted"] >= 1
    assert result["partial_failure"] is False

    payload = cache.load()
    agent_rows = [r for r in payload["comics"] if r["source"] == "agent_win"]
    assert len(agent_rows) == 5

    manual_rows = [r for r in agent_rows if r["needs_manual_series_canonical"]]
    assert len(manual_rows) == 2


# ---------------------------------------------------------------------------
# BUI-130: wish-list / collection conflict audit + bulk removal
# ---------------------------------------------------------------------------

def _seed_wish_list(items: list[dict[str, Any]]) -> None:
    """Write a wish-list.json to the conftest-isolated cache dir."""
    from locg.config import wish_list_cache_path

    path = wish_list_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": "2026-06-01T00:00:00Z", "items": items}))


def test_split_wish_list_name_parses_series_and_issue():
    from locg.commands import _split_wish_list_name

    assert _split_wish_list_name("Uncanny X-Men #148") == ("Uncanny X-Men", "148")
    # Trailing variant text after the issue token is ignored.
    assert _split_wish_list_name("Amazing Spider-Man #300 (Direct)") == (
        "Amazing Spider-Man",
        "300",
    )
    # No issue token or no series → unparseable.
    assert _split_wish_list_name("Uncanny X-Men") is None
    assert _split_wish_list_name("#148") is None
    assert _split_wish_list_name("") is None


def test_wish_list_conflicts_flags_owned_items(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])  # owns Amazing Spider-Man #300
    _seed_wish_list([
        {"name": "Amazing Spider-Man #300", "id": 111},
        {"name": "X-Men #1", "id": None},
    ])

    result = cmds.cmd_wish_list_conflicts()

    assert result["total"] == 2
    assert result["checked"] == 2
    assert result["unparseable"] == []
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["name"] == "Amazing Spider-Man #300"
    assert conflict["series"] == "Amazing Spider-Man"
    assert conflict["issue"] == "300"
    assert conflict["id"] == 111
    assert conflict["full_title_matched"] == "Amazing Spider-Man #300"


def test_wish_list_conflicts_ignores_series_start_year(tmp_path, monkeypatch):
    """BUI-129 workaround: the audit never passes a series start-year, so an
    owned issue whose release year differs from the run's first year is still
    flagged. (Forwarding year_began was the bug that hid 16 owned X-Men.)"""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Uncanny X-Men (1963 - 2011)",
        full_title="Uncanny X-Men #148",
        release_date="1981-08-01",  # run began 1963, issue released 1981
    )])
    _seed_wish_list([{"name": "Uncanny X-Men #148", "id": None}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Uncanny X-Men #148"]


def test_wish_list_conflicts_reports_unparseable(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])
    _seed_wish_list([
        {"name": "Amazing Spider-Man #300", "id": None},
        {"name": "Just A Series Name", "id": None},  # no #issue
    ])

    result = cmds.cmd_wish_list_conflicts()

    assert result["checked"] == 1
    assert result["unparseable"] == ["Just A Series Name"]
    assert len(result["conflicts"]) == 1


def test_wish_list_conflicts_missing_cache_raises(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])
    with pytest.raises(FileNotFoundError):
        cmds.cmd_wish_list_conflicts()


def test_wish_list_remove_conflicts_removes_only_owned(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])  # owns Amazing Spider-Man #300
    _seed_wish_list([
        {"name": "Amazing Spider-Man #300", "id": None},
        {"name": "X-Men #1", "id": None},
    ])

    result = cmds.cmd_wish_list_remove_conflicts()

    assert result["removed_count"] == 1
    assert result["errors"] == []
    assert result["remaining"] == 1
    assert [r["name"] for r in result["removed"]] == ["Amazing Spider-Man #300"]

    # The non-owned item survives; the owned one is gone.
    remaining = cmds.cmd_wish_list_from_cache()
    assert [it["name"] for it in remaining] == ["X-Men #1"]


# ---------------------------------------------------------------------------
# BUI-175: decimal / point-issue token regressions
# ---------------------------------------------------------------------------

def test_split_full_title_decimal_issue_tokens():
    """_split_full_title must parse decimal and multi-letter point issues."""
    from locg.commands import _split_full_title

    assert _split_full_title("X #1.MU") == ("X", "1.MU")
    assert _split_full_title("Amazing Spider-Man #20.1") == ("Amazing Spider-Man", "20.1")
    assert _split_full_title("Amazing Spider-Man #1.5") == ("Amazing Spider-Man", "1.5")


def test_split_full_title_existing_letter_suffix_not_regressed():
    """Single and multi-letter alpha suffixes (e.g. #1A, #1AU) still parse."""
    from locg.commands import _split_full_title

    assert _split_full_title("Web of Spider-Man #1A") == ("Web of Spider-Man", "1A")
    assert _split_full_title("Marvel #1AU") == ("Marvel", "1AU")


def test_split_full_title_normal_issues_unchanged():
    """Plain numeric issues and series with qualifier words stay correct."""
    from locg.commands import _split_full_title

    assert _split_full_title("Thor #154") == ("Thor", "154")
    assert _split_full_title("Fantastic Four Annual #6") == ("Fantastic Four Annual", "6")
    assert _split_full_title("Watchmen") == ("Watchmen", None)


def test_split_full_title_trailing_dot_not_captured():
    """A trailing period after the issue number must not be consumed."""
    from locg.commands import _split_full_title

    series, token = _split_full_title("Thor #154.")
    assert token == "154"


def test_split_full_title_trailing_word_not_swallowed():
    """A word after the issue token (e.g. 'Newsstand') must not extend the token."""
    from locg.commands import _split_full_title

    series, token = _split_full_title("Spider-Man #1 Newsstand")
    assert token == "1"


def test_check_matches_decimal_issue_token_mu(tmp_path, monkeypatch):
    """#1.MU stored title must match issue='1.MU' query (BUI-175)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="The Amazing Spider-Man #1.MU",
        series="The Amazing Spider-Man (1963 - 1998)",
    )])

    r = cmds.cmd_collection_check(series="The Amazing Spider-Man", issue="1.MU")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "The Amazing Spider-Man #1.MU"


def test_check_matches_decimal_issue_token_numeric(tmp_path, monkeypatch):
    """#20.1 stored title must match issue='20.1' query (BUI-175)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Amazing Spider-Man #20.1",
        series="Amazing Spider-Man (1963 - 1998)",
    )])

    r = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="20.1")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Amazing Spider-Man #20.1"


def test_check_matches_decimal_issue_token_15(tmp_path, monkeypatch):
    """#1.5 stored title must match issue='1.5' query (BUI-175)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Uncanny X-Men #1.5",
        series="Uncanny X-Men (1963 - 2011)",
    )])

    r = cmds.cmd_collection_check(series="Uncanny X-Men", issue="1.5")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Uncanny X-Men #1.5"


def test_check_single_letter_suffix_not_regressed(tmp_path, monkeypatch):
    """#1A still matches issue='1A' after the regex change (non-regression)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Web of Spider-Man #1A",
        series="Web of Spider-Man (1985 - 1995)",
    )])

    r = cmds.cmd_collection_check(series="Web of Spider-Man", issue="1A")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Web of Spider-Man #1A"


def test_check_issue_1_does_not_match_stored_1_5(tmp_path, monkeypatch):
    """issue='1' must NOT match a stored '#1.5' — no false positive (BUI-175)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Uncanny X-Men #1.5",
        series="Uncanny X-Men (1963 - 2011)",
    )])

    r = cmds.cmd_collection_check(series="Uncanny X-Men", issue="1")
    assert r["match_status"] == "not_in_cache"


# ---------------------------------------------------------------------------
# BUI-176: variant qualifier must not hide an owned base issue
# ---------------------------------------------------------------------------

def test_check_variant_supplied_matches_owned_base_issue(tmp_path, monkeypatch):
    """A variant qualifier on the query must NOT hide an owned base issue.

    Regression for BUI-176: when `variant` was a hard filter, a newsstand query
    against a stored plain "#1" reported not_in_cache and the pipeline re-bought
    a comic already owned. Variant is now a soft preference.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Spawn #1",
        series="Spawn (1992 - Present)",
    )])

    r = cmds.cmd_collection_check(series="Spawn", issue="1", variant="newsstand")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Spawn #1"


def test_check_variant_prefers_variant_bearing_row(tmp_path, monkeypatch):
    """When both a base and a variant-bearing owned row exist, prefer the variant
    one — regardless of cache order (BUI-176)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    # Base row stored first, variant row second: the variant row must still win.
    _seed_cache(cache, [
        _agent_win_row(full_title="Spawn #1", series="Spawn (1992 - Present)",
                       gixen_item_id="1"),
        _agent_win_row(full_title="Spawn #1 Newsstand", series="Spawn (1992 - Present)",
                       gixen_item_id="2"),
    ])

    r = cmds.cmd_collection_check(series="Spawn", issue="1", variant="newsstand")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Spawn #1 Newsstand"


def test_check_variant_does_not_loosen_issue_match(tmp_path, monkeypatch):
    """The soft-variant change must not make a wrong issue count as owned: a
    variant query for an un-owned issue still reports not_in_cache (BUI-176)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Spawn #1",
        series="Spawn (1992 - Present)",
    )])

    r = cmds.cmd_collection_check(series="Spawn", issue="9", variant="newsstand")
    assert r["match_status"] == "not_in_cache"


# ---------------------------------------------------------------------------
# BUI-184: _resolve_price must not IndexError / abort the record-win batch
# ---------------------------------------------------------------------------

def test_resolve_price_empty_or_whitespace_returns_none():
    """An empty / whitespace current_bid must yield None, not raise IndexError."""
    from locg.commands import _resolve_price

    assert _resolve_price("") is None
    assert _resolve_price("   ") is None
    assert _resolve_price(None) is None
    # Normal paths still parse.
    assert _resolve_price("12.50 USD") == 12.50
    assert _resolve_price(42) == 42.0
    assert _resolve_price("not-a-price") is None


def test_record_win_empty_current_bid_does_not_abort_batch(tmp_path):
    """One win with an empty current_bid must not IndexError and abort the whole
    batch; it is recorded with no price and the other wins still commit (BUI-184).
    """
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", series="Spawn", issue="1", year=1992, current_bid=""),
            _make_win(item_id="2", series="Spawn", issue="7", year=1993, current_bid=12.50),
        ],
        cache=cache,
        metron=_null_metron(),
    )

    # Both rows committed — the malformed win did not abort the batch.
    assert result["rows_written"] == 2

    payload = cache.load()
    by_title = {r["full_title"]: r for r in payload["comics"]}
    assert by_title["Spawn #1"]["price_paid"] is None
    assert by_title["Spawn #7"]["price_paid"] == 12.50
