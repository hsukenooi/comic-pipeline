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
    # BUI-208: wish rows ship only on the explicit owned-safe opt-in.
    result = cmds.cmd_collection_export(str(out_csv), push_wishes=True)

    assert result["wish_list_count"] == 1
    assert result["ready_count"] == 0
    assert result["pushed_wishes"] is True

    with open(out_csv, newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["Full Title"] == "Saga #1"
    assert rows[0]["In Collection"] == "0"
    assert rows[0]["In Wish List"] == "1"


def test_export_wins_only_by_default_excludes_wishes(tmp_path, monkeypatch):
    """BUI-208 machine gate: the default export is wins-only — no wish rows,
    no In Collection=0 row, wish_list_count==0, pushed_wishes is False."""
    import csv
    import locg.collection_io as cio
    import locg.commands as cmds

    wish_path = cio.wish_list_cache_path()
    wish_path.parent.mkdir(parents=True, exist_ok=True)
    wish_path.write_text(json.dumps({
        "updated_at": "2026-05-22T00:00:00+00:00",
        "items": [{"name": "Saga #1", "id": None}],
    }))

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(full_title="Amazing Spider-Man #300")])

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result["wish_list_count"] == 0
    assert result["pushed_wishes"] is False
    assert result["ready_count"] == 1

    with open(out_csv, newline="") as f:
        rows = list(csv.DictReader(f))

    assert all(r["In Collection"] != "0" for r in rows)
    assert all(r["In Wish List"] != "1" for r in rows)
    titles = {r["Full Title"] for r in rows}
    assert "Saga #1" not in titles
    assert "Amazing Spider-Man #300" in titles


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
    # BUI-208: wish rows ship only on the explicit owned-safe opt-in.
    result = cmds.cmd_collection_export(str(out_csv), push_wishes=True)

    assert result["ready_count"] == 1
    assert result["wish_list_count"] == 1
    assert result["pushed_wishes"] is True

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
# cmd_collection_check year-skew tolerance (BUI-214)
# ---------------------------------------------------------------------------

def test_check_year_minus_one_tolerated_on_cover_vs_on_sale_skew(tmp_path, monkeypatch):
    """BUI-214: `/comic:wishlist-add` passes Metron's cover-date year, but LOCG
    stores the earlier *on-sale* release_date. ASM #238 has cover year 1983 yet
    shipped 1982 — the exact-year gate wrongly returned not_in_cache and the
    owned book got re-wish-listed (BUI-122 data-loss trigger). year−1 must match.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #238",
        release_date="1982-03-01",
    )])

    # The cover year (1983) is one ahead of the stored on-sale year (1982).
    skewed = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="238", year="1983"
    )
    assert skewed["match_status"] == "in_collection"
    assert skewed["full_title_matched"] == "Amazing Spider-Man #238"


def test_check_exact_year_still_matches_no_regression(tmp_path, monkeypatch):
    """BUI-214: widening to year−1 must not break the exact-year hit."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #238",
        release_date="1982-03-01",
    )])

    exact = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="238", year="1982"
    )
    assert exact["match_status"] == "in_collection"
    assert exact["full_title_matched"] == "Amazing Spider-Man #238"


def test_check_far_era_collision_still_rejected(tmp_path, monkeypatch):
    """BUI-214: the ±1 window must not let a relaunch query match a classic run.
    A book owned ONLY as a 1963-shipped issue must stay not_in_cache for a 2018
    relaunch query — 2018 vs 1963 is far outside the tolerance.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #1",
        release_date="1963-03-01",
    )])

    relaunch = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="1", year="2018"
    )
    assert relaunch["match_status"] == "not_in_cache"


def test_check_year_minus_two_not_tolerated(tmp_path, monkeypatch):
    """BUI-214: confirm we widened by EXACTLY one. A query year of 1983 must NOT
    match a row stored as 1981-xx (year−2 is outside the bounded skew).
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #238",
        release_date="1981-03-01",
    )])

    too_far = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="238", year="1983"
    )
    assert too_far["match_status"] == "not_in_cache"


# ---------------------------------------------------------------------------
# cmd_collection_check year gate widened to a SYMMETRIC ±1 window (BUI-251)
# ---------------------------------------------------------------------------

def test_check_year_plus_one_avengers_false_negative_fixed(tmp_path, monkeypatch):
    """BUI-251: reproduces the BUI-247 audit finding — Avengers #1 (2013),
    confirmed owned, returned not_in_cache when queried WITH its year because
    the stored release_date sits ONE YEAR LATER than the query year (the
    opposite skew direction from BUI-214's year-minus-1 case — a late
    solicitation whose actual on-sale slipped into the following January).
    The asymmetric year-OR-year-minus-1 window missed this; the symmetric
    ±1 window must catch it.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Avengers (Vol. 5) (2013 - 2015)",
        full_title="Avengers #1",
        release_date="2014-01-08",
    )])

    result = cmds.cmd_collection_check(series="Avengers", issue="1", year="2013")
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Avengers #1"


def test_check_year_plus_one_thor_false_negative_fixed(tmp_path, monkeypatch):
    """BUI-251: the second reproduced case — Thor #5 (2016), confirmed owned,
    same year+1 skew as the Avengers case above."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 5) (2016 - 2018)",
        full_title="Thor #5",
        release_date="2017-02-01",
    )])

    result = cmds.cmd_collection_check(series="Thor", issue="5", year="2016")
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Thor #5"


def test_check_year_plus_two_not_tolerated(tmp_path, monkeypatch):
    """BUI-251: confirm the widened window is exactly ±1, not wider — mirrors
    test_check_year_minus_two_not_tolerated on the other side. A query year of
    1983 must NOT match a row stored as 1985-xx (year+2 is outside the window)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #238",
        release_date="1985-03-01",
    )])

    too_far = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="238", year="1983"
    )
    assert too_far["match_status"] == "not_in_cache"


def test_check_year_gate_two_year_gap_still_rejected(tmp_path, monkeypatch):
    """BUI-251: the ±1 widening must not reopen cross-volume collisions — the
    entire reason the year gate exists. A 1962 Vol. 1 #1 must NOT satisfy a
    2021 Vol. 5 #1 query, and a 2021 Vol. 5 #1 must NOT satisfy a 1962 query,
    even with the wider window (2021 vs 1962 is far outside ±1 either way)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Hulk (Vol. 1) (1962 - 1963)",
        full_title="Hulk #1",
        release_date="1962-05-01",
        gixen_item_id="hulk-1962",
    )])

    assert cmds.cmd_collection_check(
        series="Hulk", issue="1", year="2021"
    )["match_status"] == "not_in_cache"

    cache2 = make_cache(tmp_path / "vol5")
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache2)
    _seed_cache(cache2, [_agent_win_row(
        series="Hulk (Vol. 5) (2021 - Present)",
        full_title="Hulk #1",
        release_date="2021-06-01",
        gixen_item_id="hulk-2021",
    )])

    assert cmds.cmd_collection_check(
        series="Hulk", issue="1", year="1962"
    )["match_status"] == "not_in_cache"


def test_check_wish_row_year_plus_one_tolerated(tmp_path, monkeypatch):
    """BUI-251: the widened ±1 window applies to the wish-list gate too
    (_match_wishlisted_issue shares _year_gate_accepts with _match_owned_issue)
    — a wishlisted row with the same year+1 skew as the Avengers/Thor cases
    must flag in_wish_list."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    wishlisted = _agent_win_row(
        series="Avengers (Vol. 5) (2013 - 2015)",
        full_title="Avengers #1",
        release_date="2014-01-08",
    )
    wishlisted["in_collection"] = 0
    _seed_cache(cache, [wishlisted])

    result = cmds.cmd_collection_check(series="Avengers", issue="1", year="2013")
    assert result["match_status"] == "not_in_cache"
    assert result["in_wish_list"] is True


def test_check_wish_row_year_plus_two_not_tolerated(tmp_path, monkeypatch):
    """BUI-251: the wish-list gate's window is also exactly ±1, not wider —
    mirrors test_check_year_plus_two_not_tolerated for _match_wishlisted_issue."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    wishlisted = _agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #238",
        release_date="1985-03-01",
    )
    wishlisted["in_collection"] = 0
    _seed_cache(cache, [wishlisted])

    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="238", year="1983"
    )
    assert result["match_status"] == "not_in_cache"
    assert result["in_wish_list"] is False


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


def test_check_distinguishes_untracked_wishlisted_and_owned(tmp_path, monkeypatch):
    """BUI-250: not_in_cache used to conflate 'no row at all' with 'a row exists
    but in_collection == 0' (on the wish list / pull / read, never owned) — the
    BUI-247 audit found Hulk (Vol. 5) #9 in the latter state, indistinguishable
    from a genuinely untracked issue like New Mutants #1. `in_wish_list` makes
    the three states distinguishable: untracked (False), wishlisted-not-owned
    (True, still not_in_cache), and owned (in_collection, in_wish_list False —
    no separate wish-list-only row exists for the same issue)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    wishlisted = _agent_win_row(series="Hulk (Vol. 5) (2023 - Present)", full_title="Hulk #9")
    wishlisted["in_collection"] = 0
    owned = _agent_win_row(series="New Mutants (Vol. 1) (1983 - 1991)", full_title="New Mutants #98")
    _seed_cache(cache, [wishlisted, owned])

    untracked = cmds.cmd_collection_check(series="New Mutants", issue="1")
    assert untracked["match_status"] == "not_in_cache"
    assert untracked["in_wish_list"] is False

    wish_only = cmds.cmd_collection_check(series="Hulk", issue="9")
    assert wish_only["match_status"] == "not_in_cache"
    assert wish_only["in_wish_list"] is True

    owned_result = cmds.cmd_collection_check(series="New Mutants", issue="98")
    assert owned_result["match_status"] == "in_collection"
    assert owned_result["in_wish_list"] is False


def test_check_wish_row_year_gate_prevents_wrong_era_flag(tmp_path, monkeypatch):
    """BUI-250: in_wish_list applies the same accept-year-or-year-minus-1 gate
    as ownership — a wish-list row from a different era must not flag a query
    for a different volume's issue (mirrors the BUI-249 wrong-era concern)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    wishlisted_2008 = _agent_win_row(
        series="Hulk (2008 - 2012)", full_title="Hulk #1", release_date="2008-03-01",
    )
    wishlisted_2008["in_collection"] = 0
    _seed_cache(cache, [wishlisted_2008])

    # Same masthead, different era — the 1962 Hulk #1 query must not be flagged.
    result = cmds.cmd_collection_check(series="Hulk", issue="1", year="1962")
    assert result["match_status"] == "not_in_cache"
    assert result["in_wish_list"] is False

    # The matching era does flag it.
    result2 = cmds.cmd_collection_check(series="Hulk", issue="1", year="2008")
    assert result2["in_wish_list"] is True


def test_check_mighty_thor_masthead_alias(tmp_path, monkeypatch):
    """'The Mighty Thor #154' (cover title) resolves to the owned 'Thor #154'
    via the masthead alias (BUI-46, broadened in BUI-197).

    This was the original BUI-26 false negative — the comic that got sniped
    while owned because identify reports the cover masthead, not the catalog name.

    BUI-197 routes the alias through owned_match_keys, so it now fires WITH or
    WITHOUT a year. The no-year case is the safe direction for the buy path (an
    over-broad "owned" only causes a missed buy, never a duplicate buy) and is
    required for the conflicts audit + owned-safe export, which pass no year.
    Era-collision protection on the buy path is preserved by the year gate when a
    year IS supplied (see test_check_masthead_alias_year_gate_prevents_collision).
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
    direct = cmds.cmd_collection_check(series="Thor", issue="154")
    assert direct["match_status"] == "in_collection"
    # BUI-249: a direct series-key match is "exact", never "alias".
    assert direct["match_kind"] == "exact"
    # The cover/masthead name resolves via the alias, with a matching year:
    r = cmds.cmd_collection_check(series="The Mighty Thor", issue="154", year="1968")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Thor #154"
    # BUI-249: an alias-pass match is flagged so a caller can confirm volume.
    assert r["match_kind"] == "alias"
    assert r["matched_series_name"] == "Thor (Vol. 1) (1966 - 1996)"
    assert r["matched_release_date"] == "1968-05-02"
    # BUI-197: the alias now also fires WITHOUT a year (audit/export path).
    r2 = cmds.cmd_collection_check(series="The Mighty Thor", issue="154")
    assert r2["match_status"] == "in_collection"
    assert r2["full_title_matched"] == "Thor #154"
    assert r2["match_kind"] == "alias"


def test_check_mighty_thor_alias_false_positive_wrong_volume(tmp_path, monkeypatch):
    """BUI-249: the alias pass can land on an owned issue of the WRONG volume.

    Owning 'Thor #5' (Vol.1, 1966) makes a no-year 'The Mighty Thor #5' query
    (the intended Mighty Thor Vol.3, 2015) report in_collection via the
    masthead alias — a silent false positive, since the Vol.3 book is not
    actually owned. match_kind == "alias" (plus the matched row's decorated
    series name / release date) is how a caller detects this instead of
    trusting the bare in_collection verdict.
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 1) (1966 - 1996)",
        full_title="Thor #5",
        release_date="1966-08-01",
    )])

    r = cmds.cmd_collection_check(series="The Mighty Thor", issue="5")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Thor #5"
    assert r["match_kind"] == "alias"
    assert r["matched_series_name"] == "Thor (Vol. 1) (1966 - 1996)"
    assert r["matched_release_date"] == "1966-08-01"


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


# --- BUI-197: broader masthead aliases on the buy-path check ---

def test_check_uncanny_xmen_masthead_alias_headline_case(tmp_path, monkeypatch):
    """BUI-197 headline: query 'Uncanny X-Men #137' resolves to the owned
    'The X-Men #137'. The classic split already covers #137 (≤141), but this
    confirms the masthead equivalence end-to-end on the buy-path check, both
    directions and without a year."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The X-Men (Vol. 1) (1963 - 1981)",
        full_title="The X-Men #137",
        release_date="1980-09-01",
    )])

    # Query masthead 'Uncanny X-Men' finds the owned 'The X-Men' copy (no year):
    r = cmds.cmd_collection_check(series="Uncanny X-Men", issue="137")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "The X-Men #137"


def test_check_xmen_masthead_alias_reverse_direction(tmp_path, monkeypatch):
    """Reverse direction: collection holds 'Uncanny X-Men', query uses 'X-Men'.
    Symmetric alias equivalence must resolve regardless of which side holds the
    masthead. Uses a #142+ issue so the classic split would file it under
    Uncanny — the alias still covers a base 'X-Men' query."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Uncanny X-Men (Vol. 1) (1980 - 2011)",
        full_title="Uncanny X-Men #200",
        release_date="1985-12-01",
    )])

    r = cmds.cmd_collection_check(series="X-Men", issue="200")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "Uncanny X-Men #200"


def test_check_incredible_hulk_masthead_alias_both_directions(tmp_path, monkeypatch):
    """BUI-197: Incredible Hulk ↔ Hulk masthead alias resolves both directions,
    with and without a year."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="The Incredible Hulk (1968 - 1999)",
            full_title="The Incredible Hulk #181",
            release_date="1974-11-01",
            gixen_item_id="hulk-181",
        ),
        _agent_win_row(
            series="Hulk (2008 - 2012)",
            full_title="Hulk #1",
            release_date="2008-03-01",
            gixen_item_id="hulk-1",
        ),
    ])

    # Collection holds 'The Incredible Hulk', query uses 'Hulk':
    r1 = cmds.cmd_collection_check(series="Hulk", issue="181")
    assert r1["match_status"] == "in_collection"
    assert r1["full_title_matched"] == "The Incredible Hulk #181"

    # Collection holds 'Hulk', query uses 'Incredible Hulk':
    r2 = cmds.cmd_collection_check(series="Incredible Hulk", issue="1", year="2008")
    assert r2["match_status"] == "in_collection"
    assert r2["full_title_matched"] == "Hulk #1"


def test_check_alias_does_not_match_unowned_issue(tmp_path, monkeypatch):
    """An alias must not over-match: owning 'Thor #154' does not make an unowned
    'The Mighty Thor #999' report as in_collection (issue-level precision)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 1) (1966 - 1996)",
        full_title="Thor #154",
        release_date="1968-05-02",
    )])

    assert cmds.cmd_collection_check(
        series="The Mighty Thor", issue="999"
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
    edition: str | None = None,
) -> dict[str, Any]:
    identify_data: dict[str, Any] = {
        "series": series,
        "issue": issue,
        "year": year,
        "variant_text": variant_text,
    }
    # BUI-426: the annual/giant-size/king-size qualifier the win pipeline now
    # carries through so an annual isn't filed as the regular same-numbered
    # issue. Omitted by default so the existing regular-issue tests exercise the
    # pre-BUI-426 payload shape unchanged.
    if edition is not None:
        identify_data["edition"] = edition
    return {
        "item_id": item_id,
        "current_bid": current_bid,
        "end_date_iso": end_date_iso,
        "identify_data": identify_data,
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
    # BUI-210: the index path no longer resolves the SERIES via Metron, but it
    # does attempt a Metron *issue* lookup to backfill a real release_date. Here
    # the stub returns None (Metron miss), so we fall back to the placeholder.
    metron.lookup_issue.assert_called_once()

    payload = cache.load()
    row = payload["comics"][-1]
    assert row["series_name"] == "Amazing Spider-Man (1963 - 1998)"
    assert row["source"] == "agent_win"
    # BUI-105/BUI-210: Metron missed (stub returns None), so stamp a best-effort
    # release_date from the identify year (Jan 1) instead of leaving it None.
    # The index-resolved series_name is preserved (not overwritten by Metron).
    assert row["release_date"] == "1988-01-01"
    assert row["metron_id"] is None


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

    # BUI-199: full_title is built from the BASE series name (no parenthetical
    # decoration) so LOCG Bulk Import can match it. series_name keeps the
    # decoration; full_title does not.
    assert cache.load()["comics"][-1]["full_title"] == "Amazing Spider-Man #300"

    # collection-check always passes --year; the just-won book must be found.
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="300", year="1988"
    )
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Amazing Spider-Man #300"


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


def test_record_win_metron_series_resolution_reprint_guard(tmp_path):
    """BUI-268: metron_data from the FIRST Metron call (series resolution, no
    series_name_index entry) can carry a reprint/collected-edition date — the
    reported Infinity Gauntlet #1 case, where Metron correctly resolved the
    series to 'The Infinity Gauntlet (1991) (1991 - 1991)' but its cover_date
    was a 2022 reprint's. Left unguarded, that date got written verbatim, so a
    later year-gated collection-check for the real 1991 issue rejected the row
    as a different era. The date must be dropped (left blank, R66) when its
    year disagrees with the win's own year."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 999,
        "cover_date": "2022-09-14",  # a 2022 reprint's date, not the 1991 original
        "store_date": None,
        "series_year_began": 1991,
        "series_year_end": 1991,
        "series_name": "Infinity Gauntlet",
        "series_id": 42,
    }
    metron.format_series_name.return_value = "The Infinity Gauntlet (1991) (1991 - 1991)"
    metron.degraded = False

    result = cmd_collection_record_win(
        [_make_win(series="Infinity Gauntlet", issue="1", year=1991)],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The Infinity Gauntlet (1991) (1991 - 1991)"
    assert row["release_date"] is None


def test_record_win_metron_series_resolution_matching_date_kept(tmp_path):
    """The reprint guard (BUI-268) only rejects a MISMATCHED year — a Metron
    date that agrees with the win's year is still written normally."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)")

    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", year=1988)],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1988-05-10"


def test_check_infinity_gauntlet_no_article_matches_stored_the_prefixed(tmp_path, monkeypatch):
    """BUI-268 regression: a bare 'Infinity Gauntlet' query matches an owned
    row stored under 'The Infinity Gauntlet ...', and a year-gated query for
    the issue's real year still finds it once the reprint-date guard (above)
    keeps release_date from being corrupted by a reprint hit."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Infinity Gauntlet (1991) (1991 - 1991)",
        full_title="The Infinity Gauntlet #1",
        release_date=None,  # BUI-268: reprint guard leaves this blank
    )])

    result = cmds.cmd_collection_check(series="Infinity Gauntlet", issue="1", year="1991")
    assert result["match_status"] == "in_collection"
    assert result["matched_series_name"] == "The Infinity Gauntlet (1991) (1991 - 1991)"


# --- BUI-199: full_title is built from the BASE (undecorated) series name ---

def _seed_export_series(cache, series_names: list[str]) -> None:
    """Seed locg_export rows for each decorated series name and rebuild the index."""
    from locg.collection_cache import (
        build_volume_candidates,
        rebuild_series_name_index,
    )
    rows = [
        {
            **_agent_win_row(series=sn, full_title=f"{sn} #1"),
            "source": "locg_export",
            "gixen_item_id": f"seed-{i}",
        }
        for i, sn in enumerate(series_names)
    ]
    _seed_cache(cache, rows)

    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
        payload["_volume_candidates"] = build_volume_candidates(payload)

    cache.apply(rebuild, command="test-rebuild")


def test_record_win_full_title_strips_decoration_index_path(tmp_path):
    """BUI-199 Cause 1 (index path): full_title carries NO parenthetical
    decoration even though the canonical series_name does."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, ["Fantastic Four (Vol. 3) (1997 - 2012)"])

    cmd_collection_record_win(
        [_make_win(series="Fantastic Four", issue="72", year=2003)],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "Fantastic Four (Vol. 3) (1997 - 2012)"
    assert row["full_title"] == "Fantastic Four #72"
    assert "(" not in row["full_title"]


def test_record_win_full_title_strips_decoration_metron_path(tmp_path):
    """BUI-199 Cause 1 (Metron path): full_title from format_series_name is
    also stripped of its (year - year) decoration."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)")
    cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300")],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "Amazing Spider-Man (1963 - 1998)"
    assert row["full_title"] == "Amazing Spider-Man #300"
    assert "(" not in row["full_title"]


# --- BUI-199 Cause 3 + X-Men split: volume/series resolved by issue + era ---

def test_record_win_xmen_split_early_issue(tmp_path):
    """BUI-199 split: X-Men #107 (<=141) resolves to The X-Men, not Uncanny."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    # The index happens to hold the LATE volume under the shared "x-men" key.
    _seed_export_series(cache, ["Uncanny X-Men (Vol. 1) (1980 - 2011)"])

    cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="107", year=1977)],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The X-Men (Vol. 1) (1963 - 1981)"
    assert row["full_title"] == "The X-Men #107"


def test_record_win_xmen_split_late_issue(tmp_path):
    """BUI-199 split: X-Men #142 (>141) resolves to Uncanny X-Men."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, ["The X-Men (Vol. 1) (1963 - 1981)"])

    cmd_collection_record_win(
        [_make_win(series="X-Men", issue="142", year=1981)],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "Uncanny X-Men (Vol. 1) (1980 - 2011)"
    assert row["full_title"] == "Uncanny X-Men #142"


def test_record_win_xmen_modern_relaunch_uses_metron(tmp_path):
    """BUI-199 finding 1: a modern X-Men #1 (2019) must NOT be forced into
    The X-Men (Vol. 1). With no local volume it falls through to Metron, whose
    canonical name is used."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    # Empty index/candidates for the x-men key: the classic split must NOT fire.
    metron = _metron_hit("X-Men (2019 - 2021)", year_began=2019, year_end=2021)
    result = cmd_collection_record_win(
        [_make_win(series="X-Men", issue="1", year=2019)],
        cache=cache,
        metron=metron,
    )

    # The classic split short-circuit would have prevented any Metron call.
    metron.lookup_issue.assert_called_once()
    assert result["metron_lookups_attempted"] == 1
    row = cache.load()["comics"][-1]
    assert row["series_name"] == "X-Men (2019 - 2021)"
    assert row["full_title"] == "X-Men #1"


# --- BUI-426: annual/king-size/giant-size qualifier must survive resolution ---

def test_record_win_annual_not_filed_as_regular_xmen_issue(tmp_path):
    """BUI-426 headline: "Uncanny X-Men Annual 6" (1982) must file as
    "Uncanny X-Men Annual #6", NEVER as the Silver-Age regular "The X-Men #6"
    (1964) — a different, valuable book the buyer does NOT own. Without the
    edition qualifier the classic X-Men split (#6 <= 141 -> The X-Men) filed it
    as the regular issue, falsely claiming ownership."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    # Both classic regular volumes AND the dedicated annual series are present,
    # so the resolver has every chance to mis-file to a regular volume.
    _seed_export_series(cache, [
        "The X-Men (Vol. 1) (1963 - 1981)",
        "Uncanny X-Men (Vol. 1) (1980 - 2011)",
        "Uncanny X-Men Annual (1980 - 2011)",
    ])

    cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="6", year=1982, edition="annual")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Uncanny X-Men Annual #6"
    assert row["series_name"] == "Uncanny X-Men Annual (1980 - 2011)"
    # The false-ownership guard: it must NOT be the Silver-Age regular volume.
    assert row["series_name"] != "The X-Men (Vol. 1) (1963 - 1981)"
    assert "Annual" in row["full_title"]


def test_record_win_annual_10_ticket_case(tmp_path):
    """BUI-426 ticket case 2: Uncanny X-Men Annual 10 (1986)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, [
        "The X-Men (Vol. 1) (1963 - 1981)",
        "Uncanny X-Men Annual (1980 - 2011)",
    ])

    cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="10", year=1986, edition="annual")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Uncanny X-Men Annual #10"
    assert row["series_name"] == "Uncanny X-Men Annual (1980 - 2011)"


def test_record_win_fantastic_four_annual_ticket_case(tmp_path):
    """BUI-426 ticket case 3: Fantastic Four Annual #4 must file as
    "Fantastic Four Annual #4", not the modern regular "Fantastic Four #4"."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, [
        "Fantastic Four (Vol. 7) (2022 - Present)",
        "Fantastic Four Annual (1963 - 1974)",
    ])

    cmd_collection_record_win(
        [_make_win(series="Fantastic Four", issue="4", year=1966, edition="annual")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Fantastic Four Annual #4"
    assert row["series_name"] == "Fantastic Four Annual (1963 - 1974)"
    assert "(Vol. 7)" not in row["series_name"]


def test_record_win_annual_falls_through_to_manual_when_unseeded(tmp_path):
    """When the store has no annual volume, an annual win must NOT resolve to a
    regular volume — it falls through to manual (the safe, flagged direction)
    with the qualifier preserved in the full_title, never a false regular
    claim. This is the worst case (no local annual reference)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    # Only the regular X-Men volumes exist locally — the tempting mis-file.
    _seed_export_series(cache, [
        "The X-Men (Vol. 1) (1963 - 1981)",
        "Uncanny X-Men (Vol. 1) (1980 - 2011)",
    ])

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="6", year=1982, edition="annual")],
        cache=cache,
        metron=_null_metron(),
    )

    assert result["manual_series_count"] == 1
    row = cache.load()["comics"][-1]
    assert row["needs_manual_series_canonical"] is True
    assert "Annual" in row["full_title"]
    # Never the false Silver-Age regular claim.
    assert row["series_name"] != "The X-Men (Vol. 1) (1963 - 1981)"
    assert row["full_title"] != "The X-Men #6"


def test_record_win_annual_no_year_still_not_regular_claim(tmp_path):
    """Adversarial (BUI-426): even with NO year, an annual must never collapse
    into the classic X-Men issue-number split. The split keys off the exact
    norm_key, which is now "uncanny x-men annual" (not a split key), so the
    #<=141 boundary can't fire — no year needed. (record-win-prep's BUI-422
    gate separately reviews high-value null-year wins upstream; this asserts the
    resolver itself is safe if one reaches it.)"""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, [
        "The X-Men (Vol. 1) (1963 - 1981)",
        "Uncanny X-Men (Vol. 1) (1980 - 2011)",
        "Uncanny X-Men Annual (1980 - 2011)",
    ])

    cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="6", year=None, edition="annual")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert "Annual" in row["full_title"]
    assert row["full_title"] != "The X-Men #6"
    assert row["series_name"] != "The X-Men (Vol. 1) (1963 - 1981)"


def test_record_win_giant_size_not_filed_as_regular_issue(tmp_path):
    """BUI-426: Giant-Size keeps its qualifier in the series text (LOCG's own
    series), so it resolves as a distinct identity — never regular X-Men #1.
    Worst case here: only the regular volume is seeded locally, so the
    giant-size win falls through to manual with its qualifier intact rather than
    collapsing into the tempting regular "The X-Men #1"."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, ["The X-Men (Vol. 1) (1963 - 1981)"])

    result = cmd_collection_record_win(
        [_make_win(series="Giant-Size X-Men", issue="1", year=1975,
                   edition="giant-size")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Giant-Size X-Men #1"
    assert row["series_name"] != "The X-Men (Vol. 1) (1963 - 1981)"
    assert result["manual_series_count"] == 1


def test_record_win_giant_size_resolves_to_own_series_when_seeded(tmp_path):
    """BUI-426: when the store DOES hold the Giant-Size series, the win resolves
    to it (decoration stripped in full_title) — a distinct row from regular
    X-Men. The seed uses a non-#1 issue so it doesn't dedup the #1 win away."""
    from locg.commands import cmd_collection_record_win
    from locg.collection_cache import rebuild_series_name_index

    cache = make_cache(tmp_path)
    _seed_cache(cache, [{
        **_agent_win_row(
            series="Giant-Size X-Men (1975 - 1975)",
            full_title="Giant-Size X-Men #2",
        ),
        "source": "locg_export",
        "gixen_item_id": "gs-seed",
    }])

    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")

    cmd_collection_record_win(
        [_make_win(series="Giant-Size X-Men", issue="1", year=1975,
                   edition="giant-size")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Giant-Size X-Men #1"
    assert row["series_name"] == "Giant-Size X-Men (1975 - 1975)"


def test_record_win_king_size_special_not_filed_as_regular_issue(tmp_path):
    """BUI-426: a King-Size Special win must never collapse into the regular
    same-numbered issue. King-Size stays in the series text, so it resolves as
    a distinct identity (or manual) — the safe direction, no false claim."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, ["The Amazing Spider-Man (Vol. 1) (1963 - 1998)"])

    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man King-Size Special", issue="1",
                   year=1964, edition="king-size")],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    # It must NOT be filed as the regular ASM #1 (a $30k+ book).
    assert row["full_title"] != "The Amazing Spider-Man #1"
    assert row["series_name"] != "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
    assert "King-Size" in row["full_title"]
    assert result["manual_series_count"] == 1


def test_record_win_regular_xmen_issue_still_splits_no_edition(tmp_path):
    """BUI-426 must NOT regress the classic X-Men split for GENUINE regular
    issues: a plain "Uncanny X-Men #6" (no edition) still resolves to the
    Silver-Age "The X-Men #6" exactly as before."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, ["Uncanny X-Men (Vol. 1) (1980 - 2011)"])

    cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="6", year=1966)],  # no edition
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The X-Men (Vol. 1) (1963 - 1981)"
    assert row["full_title"] == "The X-Men #6"


def test_record_win_annual_dedups_against_owned_annual_not_regular(tmp_path):
    """BUI-426 + BUI-34: an annual win must dedup against an owned ANNUAL row,
    and must NOT be swallowed by (or swallow) an owned REGULAR same-numbered
    issue — the two are distinct identities."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    # Own the regular The X-Men #6 already; the annual win is genuinely new.
    _seed_cache(cache, [_agent_win_row(
        series="The X-Men (Vol. 1) (1963 - 1981)",
        full_title="The X-Men #6",
    )])
    _seed_export_series(cache, ["Uncanny X-Men Annual (1980 - 2011)"])

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="6", year=1982, edition="annual")],
        cache=cache,
        metron=_null_metron(),
    )

    # The annual is NOT skipped as already-owned (regular #6 must not shadow it).
    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["full_title"] == "Uncanny X-Men Annual #6"


def test_record_win_volume_resolved_by_year_iron_man(tmp_path):
    """BUI-199 Cause 3: a 1979 Iron Man #124 win picks the 1968 volume, not the
    collapsed (Vol. 8) (2026 - Present) index entry."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, [
        "Iron Man (Vol. 1) (1968 - 1996)",
        "Iron Man (Vol. 8) (2026 - Present)",
    ])

    cmd_collection_record_win(
        [_make_win(series="Iron Man", issue="124", year=1979)],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "Iron Man (Vol. 1) (1968 - 1996)"
    assert row["full_title"] == "Iron Man #124"


def test_record_win_volume_resolved_by_year_avengers(tmp_path):
    """BUI-199 Cause 3: a 1968 Avengers #52 win picks the 1963 volume, not the
    2018 volume that LOCG's wrong-issue add came from."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_export_series(cache, [
        "The Avengers (Vol. 8) (2018 - 2023)",
        "The Avengers (Vol. 1) (1963 - 1996)",
    ])

    cmd_collection_record_win(
        [_make_win(series="Avengers", issue="52", year=1968)],
        cache=cache,
        metron=_null_metron(),
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The Avengers (Vol. 1) (1963 - 1996)"
    assert row["full_title"] == "The Avengers #52"


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
    # BUI-199: full_title uses the BASE series name (decoration stripped), with
    # the matched variant suffix appended. series_name keeps the decoration.
    assert row["full_title"] == "Spawn #313 Capullo Variant"
    assert row["series_name"] == "Spawn (1992 - Present)"
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


def _seed_owned_row(cache, series, full_title, in_collection=1, release_date="1988-05-10"):
    """Seed a single owned locg_export row under an arbitrary decorated
    ``series`` (BUI-267 cross-era/volume test support — ``_seed_owned_spawn``
    hardcodes the Spawn series name)."""
    from locg.collection_cache import rebuild_series_name_index
    _seed_cache(cache, [{
        **_agent_win_row(series=series, full_title=full_title, release_date=release_date),
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
        # year within the seeded "Spawn (1992 - Present)" range (BUI-267 era gate).
        [_make_win(series="Spawn", issue="98", year=1999)],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0
    assert result["skipped_already_owned_titles"] == ["Spawn (1992 - Present) #98"]
    assert result["skipped_already_owned_detail"] == [{
        "win": "Spawn (1992 - Present) #98",
        "matched_series_name": "Spawn (1992 - Present)",
        "matched_release_date": "1988-05-10",
    }]
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
        # year within the seeded "Spawn (1992 - Present)" range (BUI-267 era gate).
        [_make_win(series="Spawn", issue="313", year=2004, variant_text="Capullo Variant")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0


# --- BUI-267: era/volume-aware dedup + surfaced skip provenance ---

def test_record_win_dedup_does_not_conflate_cross_era_volume(tmp_path):
    """New Gods #7 (1971 Kirby) must NOT be deduped against an owned
    'The New Gods (Vol. 5) (2024 - 2025)' #7 — same masthead+issue, unrelated
    era/volume (the reported BUI-267 false skip)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(cache, "The New Gods (Vol. 5) (2024 - 2025)", "New Gods #7")

    result = cmd_collection_record_win(
        [_make_win(series="New Gods", issue="7", year=1971)],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


def test_record_win_dedup_surfaces_matched_row_on_skip(tmp_path):
    """A genuine skip surfaces which owned row it matched (series_name + year),
    so a caller can catch a cross-era/variant false match (BUI-267)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_spawn(cache, "Spawn #98")

    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="98", year=1999)],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    detail = result["skipped_already_owned_detail"]
    assert len(detail) == 1
    assert detail[0]["matched_series_name"] == "Spawn (1992 - Present)"
    assert detail[0]["matched_release_date"] == "1988-05-10"


def test_record_win_dedup_newsstand_vs_base_not_conflated(tmp_path):
    """A base-edition win must not be deduped against an owned Newsstand copy
    (the reported Uncanny X-Men #201 false skip, BUI-267)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(
        cache, "Uncanny X-Men (Vol. 1) (1980 - 2011)", "Uncanny X-Men #201 Newsstand Edition",
    )

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="201", year=1985)],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


def test_record_win_dedup_newsstand_still_dedupes_newsstand(tmp_path):
    """A Newsstand win IS deduped against an owned Newsstand copy of the same
    issue (BUI-267 regression: the edition gate must not over-block genuine
    matches)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(
        cache, "Uncanny X-Men (Vol. 1) (1980 - 2011)", "Uncanny X-Men #201 Newsstand Edition",
    )

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="201", year=1985, variant_text="newsstand")],
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


# --- BUI-210: real release date on the series_name_index path ---

def _index_seeded_cache(tmp_path):
    """Cache with a locg_export row so the series resolves via series_name_index
    (the no-Metron-for-series path) — the BUI-210 scenario."""
    from locg.collection_cache import rebuild_series_name_index

    cache = make_cache(tmp_path)
    _seed_cache(cache, [{
        **_agent_win_row(series="The X-Men (1963 - 1981)", full_title="The X-Men #1"),
        "source": "locg_export",
    }])
    cache.apply(
        lambda payload: payload.__setitem__(
            "series_name_index", rebuild_series_name_index(payload)
        ),
        command="test-rebuild",
    )
    return cache


def test_record_win_index_path_backfills_real_metron_date(tmp_path):
    """BUI-210: a win whose series is in series_name_index gets a REAL
    release_date from a Metron issue lookup (not a {year}-01-01 placeholder),
    with a non-null metron_id, while keeping the index-resolved series_name."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 777,
        "cover_date": "1970-08-01",
        "store_date": "1970-06-09",
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }

    result = cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    metron.lookup_issue.assert_called_once()
    # format_series_name must NOT be consulted — the index-resolved series wins.
    metron.format_series_name.assert_not_called()

    row = cache.load()["comics"][-1]
    # store_date preferred over cover_date; this is a real date, not 1970-01-01.
    assert row["release_date"] == "1970-06-09"
    assert row["metron_id"] == 777
    # Index-resolved series name is preserved (the decorated volume form from
    # resolve_series_for_win), NOT overwritten by Metron's bare "The X-Men".
    assert row["series_name"].startswith("The X-Men (")
    assert row["series_name"] != "The X-Men"


def test_record_win_index_path_rejects_reprint_year_mismatch(tmp_path):
    """BUI-210 reprint guard: a Metron date whose year ≠ the win's year (a
    collected-edition/reprint, e.g. The X-Men #59 → 2005) is rejected, and the
    {year}-01-01 placeholder is kept."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 888,
        "cover_date": "2005-03-09",  # reprint year, not 1970
        "store_date": "2005-03-09",
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    # Reprint rejected → placeholder kept, metron_id stays None so the export
    # blanks the placeholder (rather than shipping a wrong 2005 date).
    assert row["release_date"] == "1970-01-01"
    assert row["metron_id"] is None


def test_record_win_index_path_credential_error_keeps_placeholder(tmp_path):
    """BUI-210: a MetronCredentialError during the index-path date backfill
    degrades gracefully to the placeholder — no crash."""
    from locg.commands import cmd_collection_record_win
    from locg.metron import MetronCredentialError
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.side_effect = MetronCredentialError("no creds")

    result = cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1970-01-01"
    assert row["metron_id"] is None


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


# --- BUI-255: throttle/timeout trips the batch breaker like credential errors ---

def test_record_win_metron_degraded_disables_metron(tmp_path):
    """A throttled/unreachable Metron (MetronClient.degraded) trips the same
    per-batch breaker as MetronCredentialError: after the first call reports
    degraded, remaining rows fall back to manual and Metron is never called
    again — instead of retrying (and sleeping) on every remaining row."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.degraded = False

    def _throttled_lookup(*_args, **_kwargs):
        # Simulates a real MetronClient after its BUI-260 rate-limit retry
        # is exhausted: returns None (no exception) but flips .degraded True.
        metron.degraded = True
        return None

    metron.lookup_issue.side_effect = _throttled_lookup

    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", series="Ghost Rider", issue="1"),
            _make_win(item_id="2", series="Ghost Rider", issue="2"),
        ],
        cache=cache,
        metron=metron,
    )

    # Both rows still get written (the batch completes and commits); Metron
    # is called exactly once — the breaker trips before row 2 ever asks it.
    assert result["rows_written"] == 2
    assert result["manual_series_count"] == 2
    assert result["partial_failure"] is False
    assert metron.lookup_issue.call_count == 1


def test_record_win_metron_5xx_disables_metron(tmp_path):
    """BUI-342: a Metron 5xx trips the SAME per-batch breaker as a rate-limit /
    connection failure. A real MetronClient returns None (no exception) after
    its single capped 5xx retry but flips .degraded True; once tripped, the
    remaining rows fall back to manual and Metron is never called again —
    instead of hammering a down server and silently recording every win as
    'not in Metron' (the exact failure the ticket describes)."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.degraded = False

    def _server_error_lookup(*_args, **_kwargs):
        # Mirrors a real MetronClient after an exhausted 5xx retry: None + degraded.
        metron.degraded = True
        return None

    metron.lookup_issue.side_effect = _server_error_lookup

    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", series="Ghost Rider", issue="1"),
            _make_win(item_id="2", series="Ghost Rider", issue="2"),
        ],
        cache=cache,
        metron=metron,
    )

    # Both rows still get written; Metron is called exactly once — the breaker
    # trips before row 2 ever asks it.
    assert result["rows_written"] == 2
    assert result["manual_series_count"] == 2
    assert result["partial_failure"] is False
    assert metron.lookup_issue.call_count == 1


def test_record_win_metron_degraded_false_does_not_disable(tmp_path):
    """A genuine, exception-free 'no match' (degraded stays False) must NOT
    trip the breaker — every row still gets its own Metron attempt."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.degraded = False
    metron.lookup_issue.return_value = None  # plain miss, not a throttle

    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", series="Ghost Rider", issue="1"),
            _make_win(item_id="2", series="Ghost Rider", issue="2"),
        ],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 2
    assert result["manual_series_count"] == 2
    # Each row gets its own series-resolution attempt AND its own BUI-210
    # date-backfill attempt (metron_data stays None both times) — 2 calls per
    # row, 4 total — proving the breaker never tripped and skipped none of them.
    assert metron.lookup_issue.call_count == 4


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


# --- rows_written vs rows_inserted/rows_overwritten split (BUI-367) ---

def test_record_win_rows_inserted_pure_insert_batch(tmp_path):
    """A batch of wholly new item_ids: rows_written == rows_inserted, zero overwrites."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id="1", series="Amazing Spider-Man", issue="300"),
        _make_win(item_id="2", series="Amazing Spider-Man", issue="301"),
        _make_win(item_id="3", series="Amazing Spider-Man", issue="302"),
    ]

    result = cmd_collection_record_win(wins, cache=cache, metron=_null_metron())

    assert result["rows_written"] == 3
    assert result["rows_inserted"] == 3
    assert result["rows_overwritten"] == 0


def test_record_win_rows_overwritten_only_batch(tmp_path):
    """Re-recording the same item_ids with CORRECTED identify_data is all overwrites.

    Modeling a real correction flow: the user re-runs record-win for the same
    Gixen auctions with fixed series/issue values. Note the corrected issue
    numbers (997/998/999) are disjoint from ALL three originally-owned issues
    (300/301/302), not just their own row's original issue — reusing any of
    300/301/302 would instead hit the BUI-34 "already owned" skip in
    _build_win_row (keyed on (series, issue) across the whole owned set,
    checked BEFORE write_wins is ever called), which is a different code path
    than the write_wins-level gixen_item_id dedup this test targets. The
    pre-BUI-367 behavior reported
    rows_written=3 on the second call too, as though 3 new rows had been
    added — misleading a caller who reads rows_written as "new rows recorded".
    rows_inserted must reflect the truth: nothing new landed.
    """
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id="1", series="Amazing Spider-Man", issue="300"),
        _make_win(item_id="2", series="Amazing Spider-Man", issue="301"),
        _make_win(item_id="3", series="Amazing Spider-Man", issue="302"),
    ]

    first = cmd_collection_record_win(wins, cache=cache, metron=_null_metron())
    assert first["rows_inserted"] == 3
    assert first["rows_overwritten"] == 0

    corrected_wins = [
        _make_win(item_id="1", series="Amazing Spider-Man", issue="997"),
        _make_win(item_id="2", series="Amazing Spider-Man", issue="998"),
        _make_win(item_id="3", series="Amazing Spider-Man", issue="999"),
    ]
    second = cmd_collection_record_win(corrected_wins, cache=cache, metron=_null_metron())

    assert second["rows_written"] == 3
    assert second["rows_inserted"] == 0
    assert second["rows_overwritten"] == 3

    payload = cache.load()
    # Still exactly 3 rows on disk — the second call overwrote in place.
    assert len([r for r in payload["comics"] if r.get("gixen_item_id")]) == 3


def test_record_win_rows_mixed_batch_splits_inserted_and_overwritten(tmp_path):
    """A batch mixing brand-new item_ids with a corrected existing one splits accurately."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    seed_win = _make_win(item_id="EXISTING", series="Amazing Spider-Man", issue="300")
    cmd_collection_record_win([seed_win], cache=cache, metron=_null_metron())

    mixed_wins = [
        # Corrected issue for the same auction item_id — not caught by the
        # BUI-34 already-owned skip (issue differs from the owned #300), so it
        # reaches write_wins and overwrites the existing gixen_item_id row.
        _make_win(item_id="EXISTING", series="Amazing Spider-Man", issue="399"),
        _make_win(item_id="NEW-1", series="Amazing Spider-Man", issue="301"),  # insert
        _make_win(item_id="NEW-2", series="Amazing Spider-Man", issue="302"),  # insert
    ]
    result = cmd_collection_record_win(mixed_wins, cache=cache, metron=_null_metron())

    assert result["rows_written"] == 3
    assert result["rows_inserted"] == 2
    assert result["rows_overwritten"] == 1


def test_record_win_rows_intra_batch_duplicate_counts_as_overwrite(tmp_path):
    """Two wins sharing gixen_item_id in ONE call (BUI-356 lineage): one insert, one overwrite.

    Distinct from the cross-call overwrite case above — this exercises the
    intra-batch collision path where the duplicate never touches the on-disk
    store until this same commit.
    """
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id="DUPE", series="Amazing Spider-Man", issue="300"),
        _make_win(item_id="DUPE", series="Amazing Spider-Man", issue="300", current_bid=150.00),
    ]

    result = cmd_collection_record_win(wins, cache=cache, metron=_null_metron())

    assert result["rows_written"] == 2
    assert result["rows_inserted"] == 1
    assert result["rows_overwritten"] == 1

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


def test_wish_list_conflicts_finds_xmen_masthead_split(tmp_path, monkeypatch):
    """BUI-200 data-loss case: owned as 'The X-Men #107', wished as
    'Uncanny X-Men #107'. LOCG files #1-141 under 'The X-Men' and #142+ under
    'Uncanny X-Men', so a literal-series match misses the conflict and the export
    emits In Collection=0 — deleting the owned copy (the 26-deleted-X-Men bug).
    The audit passes NO year, so the issue-number masthead split must match
    without one."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The X-Men (Vol. 1) (1963 - 1981)",
        full_title="The X-Men #107",
        release_date="1977-10-01",
    )])
    _seed_wish_list([{"name": "Uncanny X-Men #107", "id": 207}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Uncanny X-Men #107"]
    assert result["conflicts"][0]["full_title_matched"] == "The X-Men #107"


def test_wish_list_conflicts_finds_leading_article_variant(tmp_path, monkeypatch):
    """BUI-200: owned under a leading-article variant ('The Incredible Hulk')
    must be flagged when wished without the article ('Incredible Hulk'). The
    normalized series key strips the article so the conflict is caught."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Incredible Hulk (1968 - 1999)",
        full_title="The Incredible Hulk #181",
        release_date="1974-11-01",
    )])
    _seed_wish_list([{"name": "Incredible Hulk #181", "id": 181}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Incredible Hulk #181"]


def test_wish_list_conflicts_finds_thor_masthead_alias(tmp_path, monkeypatch):
    """BUI-197: 'The Mighty Thor #300' wished, owned as 'Thor #300'. The audit
    passes NO year, so the masthead alias must resolve year-free (the old
    year-gated _SERIES_ALIASES path was dead in the audit)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 1) (1966 - 1996)",
        full_title="Thor #300",
        release_date="1980-10-01",
    )])
    _seed_wish_list([{"name": "The Mighty Thor #300", "id": 300}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["The Mighty Thor #300"]
    assert result["conflicts"][0]["full_title_matched"] == "Thor #300"


def test_wish_list_conflicts_finds_hulk_masthead_alias(tmp_path, monkeypatch):
    """BUI-197: 'Incredible Hulk #377' wished, owned as 'The Incredible Hulk
    #377' — masthead alias caught in the no-year audit."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Incredible Hulk (1968 - 1999)",
        full_title="The Incredible Hulk #377",
        release_date="1991-01-01",
    )])
    _seed_wish_list([{"name": "Incredible Hulk #377", "id": 377}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Incredible Hulk #377"]


def test_wish_list_conflicts_finds_annual_masthead_alias(tmp_path, monkeypatch):
    """BUI-197: an annual under one masthead, wished under another. Owned as
    'Uncanny X-Men Annual #9', wished as 'X-Men Annual #9'. The annual qualifier
    is stripped, the base series alias-expanded, and the qualifier re-applied, so
    the cross-masthead annual is found (outside the #141/#142 split)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Uncanny X-Men Annual (1980 - 2011)",
        full_title="Uncanny X-Men Annual #9",
        release_date="1985-12-01",
    )])
    _seed_wish_list([{"name": "X-Men Annual #9", "id": 909}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["X-Men Annual #9"]
    assert result["conflicts"][0]["full_title_matched"] == "Uncanny X-Men Annual #9"


def test_wish_list_conflicts_finds_non_digit_issue_token_alias(tmp_path, monkeypatch):
    """BUI-197 MUST-FIX 1 (deletion hole): a wish whose issue token is NOT
    digit-led ('#A1') must still be ownership-checked, not bucketed as
    'unparseable' and skipped. Owned 'Thor Annual #A1', wished 'The Mighty Thor
    Annual #A1' (masthead alias + non-digit token) — the audit must flag it so the
    export doesn't emit In Collection=0 over the owned copy."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor Annual (1966 - 1994)",
        full_title="Thor Annual #A1",
        release_date="1966-01-01",
    )])
    _seed_wish_list([{"name": "The Mighty Thor Annual #A1", "id": 11}])

    result = cmds.cmd_wish_list_conflicts()

    assert result["unparseable"] == [], "non-digit token must not be skipped"
    assert [c["name"] for c in result["conflicts"]] == ["The Mighty Thor Annual #A1"]
    assert result["conflicts"][0]["full_title_matched"] == "Thor Annual #A1"


def test_check_dateless_alias_row_rejected_when_year_known(tmp_path, monkeypatch):
    """BUI-197 MUST-FIX 2: a DATELESS owned row matched only via an ALIAS key is
    rejected when the query carries a year — so a dateless classic
    'The Incredible Hulk #1' (1962) does NOT falsely satisfy a year-bearing
    'Hulk #1' (2021 relaunch) query, which would skip a legitimate buy."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Incredible Hulk (1962 - 1962)",
        full_title="The Incredible Hulk #1",
        release_date=None,  # dateless owned classic
    )])

    r = cmds.cmd_collection_check(series="Hulk", issue="1", year="2021")
    assert r["match_status"] == "not_in_cache", "dateless alias row must not block a year-bearing buy"


def test_check_dated_alias_row_matches_with_correct_year(tmp_path, monkeypatch):
    """BUI-197 MUST-FIX 2 (positive): a DATED owned row still matches via an alias
    key when the query year agrees — 'The Incredible Hulk #1' (1962) is owned for
    a 'Hulk #1' year=1962 query."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Incredible Hulk (1962 - 1962)",
        full_title="The Incredible Hulk #1",
        release_date="1962-05-01",
    )])

    r = cmds.cmd_collection_check(series="Hulk", issue="1", year="1962")
    assert r["match_status"] == "in_collection"
    assert r["full_title_matched"] == "The Incredible Hulk #1"


def test_check_dateless_alias_row_still_matches_with_no_year(tmp_path, monkeypatch):
    """BUI-197 MUST-FIX 2 (documented tradeoff): with NO year the alias over-match
    is kept on purpose — the audit/export path passes no year, and an over-broad
    'owned' there only over-EXCLUDES a wish (the safe direction), never deletes a
    book. A dateless classic 'The Incredible Hulk #1' DOES match a no-year
    'Hulk #1' query. (The buy path always passes a year, so it gets MUST-FIX 2's
    stricter dateless handling instead.)"""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Incredible Hulk (1962 - 1962)",
        full_title="The Incredible Hulk #1",
        release_date=None,
    )])

    r = cmds.cmd_collection_check(series="Hulk", issue="1")
    assert r["match_status"] == "in_collection"


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
    assert result["scoped"] is False

    # The non-owned item survives; the owned one is gone.
    remaining = cmds.cmd_wish_list_from_cache()
    assert [it["name"] for it in remaining] == ["X-Men #1"]


def test_wish_list_conflicts_surface_matched_row_provenance(tmp_path, monkeypatch):
    """BUI-266: each conflict carries the matched owned row's series_name +
    release_date (BUI-249 provenance), so a caller can spot a decoy
    cross-era/cross-edition match before removing it."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Avengers (1963 - 1996)",
        full_title="Avengers #52",
        release_date="1968-05-01",
    )])
    _seed_wish_list([{"name": "Avengers #52", "id": 52}])

    result = cmds.cmd_wish_list_conflicts()

    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["series_name"] == "The Avengers (1963 - 1996)"
    assert conflict["release_date"] == "1968-05-01"


def test_wish_list_remove_conflicts_scoped_touches_only_named_set(tmp_path, monkeypatch):
    """BUI-266: passing ``names`` scopes removal to that set only — a
    pre-existing conflict NOT named stays untouched (the BUI-259 incident:
    114 removed when ~6 were intended)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(full_title="Amazing Spider-Man #300"),
        _agent_win_row(series="X-Men (2019 - 2021)", full_title="X-Men #1"),
    ])
    _seed_wish_list([
        {"name": "Amazing Spider-Man #300", "id": 1},
        {"name": "X-Men #1", "id": 2},
    ])

    result = cmds.cmd_wish_list_remove_conflicts(names=["Amazing Spider-Man #300"])

    assert result["scoped"] is True
    assert result["removed_count"] == 1
    assert [r["name"] for r in result["removed"]] == ["Amazing Spider-Man #300"]
    assert result["errors"] == []
    # The un-named "X-Men #1" conflict must survive untouched.
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"X-Men #1"}


def test_wish_list_remove_conflicts_scoped_rejects_stale_name(tmp_path, monkeypatch):
    """BUI-266: a name that is NOT a current conflict (already removed, never
    one, or a typo) is reported as an error, never silently accepted — the
    scoped path re-checks against a fresh audit rather than trusting the
    caller's list."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(full_title="Amazing Spider-Man #300")])
    _seed_wish_list([
        {"name": "Amazing Spider-Man #300", "id": 1},
        {"name": "Not A Conflict #1", "id": 2},  # not owned — not a conflict
    ])

    result = cmds.cmd_wish_list_remove_conflicts(names=["Not A Conflict #1"])

    assert result["removed_count"] == 0
    assert result["removed"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "Not A Conflict #1"
    # Nothing was mutated — both wishes remain.
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"Amazing Spider-Man #300", "Not A Conflict #1"}


def test_wish_list_remove_conflicts_scoped_carries_provenance(tmp_path, monkeypatch):
    """A scoped removal's returned entry carries the same matched-row
    provenance the audit surfaced (BUI-266)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Avengers (1963 - 1996)",
        full_title="Avengers #52",
        release_date="1968-05-01",
    )])
    _seed_wish_list([{"name": "Avengers #52", "id": 52}])

    result = cmds.cmd_wish_list_remove_conflicts(names=["Avengers #52"])

    assert result["removed_count"] == 1
    entry = result["removed"][0]
    assert entry["matched_series_name"] == "The Avengers (1963 - 1996)"
    assert entry["matched_release_date"] == "1968-05-01"


def test_wish_list_remove_conflicts_surfaces_owner_and_spares_collection(tmp_path, monkeypatch):
    """BUI-208 U2: fulfillment-drop surfaces the matched owned identity and
    mutates ONLY wish state — the collection file is never touched."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])  # owns Amazing Spider-Man #300
    _seed_wish_list([
        {"name": "Amazing Spider-Man #300", "id": None, "source": "local"},
        {"name": "X-Men #1", "id": None, "source": "local"},
    ])

    collection_path = tmp_path / "collection.json"
    before = collection_path.read_bytes()

    result = cmds.cmd_wish_list_remove_conflicts()

    # The matched owned identity is surfaced on each drop (also logged at INFO).
    assert result["removed_count"] == 1
    assert result["removed"][0]["name"] == "Amazing Spider-Man #300"
    assert result["removed"][0]["matched_owned"] == "Amazing Spider-Man #300"

    # Fulfillment-drop touches ONLY wish state: the collection file is byte-unchanged.
    assert collection_path.read_bytes() == before


# ---------------------------------------------------------------------------
# BUI-372: printing-conflict exclusion from the conflicts audit
# ---------------------------------------------------------------------------
#
# Reuses the AMM #1 incident fixture (_amm_rows, defined below in the BUI-364
# printing-marker-conflict section): an owned "2nd Printing" row and a
# wishlisted base row for the same series+issue. A wish-list entry for the
# BASE title ("Absolute Martian Manhunter #1") must not be treated as a
# removable conflict — the owned row is a DIFFERENT printing, so the wished
# base genuinely isn't owned yet.

def test_wish_list_conflicts_excludes_printing_conflict_decoy(tmp_path, monkeypatch):
    """BUI-372: an owned reprint matching a wishlisted BASE printing is not a
    genuine conflict — it goes into printing_conflicts, not conflicts, so
    remove-conflicts can never sweep it (the BUI-249/BUI-259 incident class
    through a new door)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows())
    _seed_wish_list([{"name": "Absolute Martian Manhunter #1", "id": 1}])

    result = cmds.cmd_wish_list_conflicts()

    assert result["conflicts"] == []
    assert len(result["printing_conflicts"]) == 1
    decoy = result["printing_conflicts"][0]
    assert decoy["name"] == "Absolute Martian Manhunter #1"
    assert decoy["full_title_matched"] == "Absolute Martian Manhunter #1 2nd Printing"
    assert decoy["printing_candidates"]


def test_wish_list_remove_conflicts_unscoped_never_removes_printing_decoy(tmp_path, monkeypatch):
    """BUI-372: the unscoped sweep (names=None) takes every entry in
    `conflicts` — since the printing decoy was never added there, it survives
    an unscoped remove-conflicts call untouched."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows())
    _seed_wish_list([{"name": "Absolute Martian Manhunter #1", "id": 1}])

    result = cmds.cmd_wish_list_remove_conflicts()

    assert result["removed_count"] == 0
    assert result["removed"] == []
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"Absolute Martian Manhunter #1"}
    assert len(result["printing_conflicts"]) == 1


def test_wish_list_remove_conflicts_scoped_rejects_printing_decoy_name(tmp_path, monkeypatch):
    """BUI-372: explicitly naming a printing-conflict decoy in a scoped
    removal gets a SPECIFIC error (distinct printing, not a genuine
    duplicate) rather than either silently removing it or the generic
    "not a conflict at all" message."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows())
    _seed_wish_list([{"name": "Absolute Martian Manhunter #1", "id": 1}])

    result = cmds.cmd_wish_list_remove_conflicts(names=["Absolute Martian Manhunter #1"])

    assert result["removed_count"] == 0
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["name"] == "Absolute Martian Manhunter #1"
    assert "printing" in error["error"].lower()
    assert "genuine duplicate" in error["error"].lower()
    # Nothing mutated.
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"Absolute Martian Manhunter #1"}


def test_wish_list_conflicts_genuine_conflict_alongside_printing_decoy(tmp_path, monkeypatch):
    """BUI-372: a genuine conflict (no printing marker involved) and a
    printing-conflict decoy can coexist in one audit — only the genuine one
    lands in `conflicts` and is removable; the decoy never is."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows() + [_agent_win_row(full_title="Amazing Spider-Man #300")])
    _seed_wish_list([
        {"name": "Absolute Martian Manhunter #1", "id": 1},  # printing decoy
        {"name": "Amazing Spider-Man #300", "id": 2},         # genuine conflict
    ])

    audit = cmds.cmd_wish_list_conflicts()
    assert [c["name"] for c in audit["conflicts"]] == ["Amazing Spider-Man #300"]
    assert [c["name"] for c in audit["printing_conflicts"]] == ["Absolute Martian Manhunter #1"]

    result = cmds.cmd_wish_list_remove_conflicts()
    assert result["removed_count"] == 1
    assert [r["name"] for r in result["removed"]] == ["Amazing Spider-Man #300"]
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"Absolute Martian Manhunter #1"}


# ---------------------------------------------------------------------------
# BUI-379: printing marker carried in the WISH NAME itself (reverse of BUI-372)
# ---------------------------------------------------------------------------
#
# BUI-372 catches the case where the OWNED row's full_title carries the
# printing marker. This is the reverse incident direction: the wish-list
# entry's own stored NAME carries the marker (e.g. literally wish-listing
# "Foo #1 2nd Printing"), and only the BASE printing is owned.
# _split_wish_list_name drops everything after the issue token — including
# that marker — before cmd_wish_list_conflicts ever sees it, so unless the
# marker is re-detected from the raw name, this reads as a plain (removable)
# conflict even though the wished 2nd printing genuinely isn't owned.

def test_split_wish_list_name_drops_trailing_printing_marker():
    """Documents the actual (unchanged) behavior _wish_list_name_printing_variant
    exists to work around: the plain split silently loses a trailing printing
    marker, same as it loses any other trailing variant text."""
    from locg.commands import _split_wish_list_name

    assert _split_wish_list_name("Absolute Martian Manhunter #1 2nd Printing") == (
        "Absolute Martian Manhunter",
        "1",
    )


def test_wish_list_name_printing_variant_detects_marker():
    from locg.commands import _wish_list_name_printing_variant

    assert _wish_list_name_printing_variant("Absolute Martian Manhunter #1 2nd Printing") == \
        "2nd Printing"
    assert _wish_list_name_printing_variant("Amazing Spider-Man #300") is None
    assert _wish_list_name_printing_variant("Amazing Spider-Man #300 (Direct)") is None


def _amm_reverse_rows() -> list[dict[str, Any]]:
    """BUI-379 reproduction state: only the BASE printing is owned. The 2nd
    printing exists only as a wish-list NAME, not as any cache row — the
    reverse of `_amm_rows`' incident direction."""
    owned_base = _agent_win_row(
        publisher="DC Comics",
        series="Absolute Martian Manhunter (2025)",
        full_title="Absolute Martian Manhunter #1",
        release_date="2025-03-19",
        gixen_item_id="147000000003",
    )
    return [owned_base]


def test_wish_list_conflicts_wish_name_printing_marker_not_plain_conflict(tmp_path, monkeypatch):
    """BUI-379: a wish-list entry named "...2nd Printing" must not be treated
    as a plain conflict just because the BASE printing is owned — it belongs
    in printing_conflicts (held), never conflicts (removable)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_reverse_rows())
    _seed_wish_list([{"name": "Absolute Martian Manhunter #1 2nd Printing", "id": 1}])

    result = cmds.cmd_wish_list_conflicts()

    assert result["conflicts"] == []
    assert len(result["printing_conflicts"]) == 1
    decoy = result["printing_conflicts"][0]
    assert decoy["name"] == "Absolute Martian Manhunter #1 2nd Printing"
    assert decoy["full_title_matched"] == "Absolute Martian Manhunter #1"
    assert decoy["printing_candidates"]


def test_wish_list_remove_conflicts_never_removes_wish_name_printing_marker(tmp_path, monkeypatch):
    """BUI-379: the unscoped sweep must never delete a wish whose own name
    carries a printing marker just because the base is owned — this is the
    data-loss direction the ticket exists to close."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_reverse_rows())
    _seed_wish_list([{"name": "Absolute Martian Manhunter #1 2nd Printing", "id": 1}])

    result = cmds.cmd_wish_list_remove_conflicts()

    assert result["removed_count"] == 0
    assert result["removed"] == []
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"Absolute Martian Manhunter #1 2nd Printing"}
    assert len(result["printing_conflicts"]) == 1


def test_wish_list_remove_conflicts_scoped_rejects_wish_name_printing_marker(tmp_path, monkeypatch):
    """BUI-379: explicitly naming the wish-name-marker decoy in a scoped
    removal gets the same specific "printing conflict" error as the BUI-372
    owned-side decoy, not a silent removal."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_reverse_rows())
    _seed_wish_list([{"name": "Absolute Martian Manhunter #1 2nd Printing", "id": 1}])

    result = cmds.cmd_wish_list_remove_conflicts(
        names=["Absolute Martian Manhunter #1 2nd Printing"]
    )

    assert result["removed_count"] == 0
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["name"] == "Absolute Martian Manhunter #1 2nd Printing"
    assert "printing" in error["error"].lower()
    remaining_names = {it["name"] for it in cmds.cmd_wish_list_from_cache()}
    assert remaining_names == {"Absolute Martian Manhunter #1 2nd Printing"}


# ---------------------------------------------------------------------------
# BUI-387: per-issue year-scoped wish entries
# ---------------------------------------------------------------------------

def _owned_modern_ff18_row():
    """Owned MODERN Fantastic Four #18 (Vol. 7, 2022) — the cross-volume decoy a
    year-blind vintage FF #18 wish falsely conflicts against (ticket example)."""
    return _agent_win_row(
        series="Fantastic Four (2018 - 2022)",
        full_title="Fantastic Four #18",
        release_date="2022-11-01",
    )


def test_wish_list_conflicts_year_scoped_wish_skips_modern_owned_volume(tmp_path, monkeypatch):
    """BUI-387: a wish stamped with its vintage Cover Year (1963) no longer
    conflicts with an owned MODERN volume of the same issue number (2022) — the
    year gate rejects the wrong-era owned row, clearing the permanent decoy."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_owned_modern_ff18_row()])  # own ONLY the 2022 volume
    _seed_wish_list([{"name": "Fantastic Four #18", "id": 1, "year": "1963"}])

    result = cmds.cmd_wish_list_conflicts()

    assert result["checked"] == 1
    assert result["conflicts"] == []          # year-scoped → not the owned volume
    assert result["printing_conflicts"] == []


def test_wish_list_conflicts_unstamped_wish_keeps_year_blind_match(tmp_path, monkeypatch):
    """BUI-387: an UNSTAMPED wish (no `year` field — the pre-387 shape) keeps
    today's year-blind behavior: it still flags against the owned modern volume,
    preserving the BUI-122-safe over-flagging default so an un-backfilled wish
    can never silently miss an owned book."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_owned_modern_ff18_row()])
    _seed_wish_list([{"name": "Fantastic Four #18", "id": 1}])  # no year field

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Fantastic Four #18"]


def test_wish_list_conflicts_year_scoped_wish_still_flags_matching_volume(tmp_path, monkeypatch):
    """BUI-387: year scoping narrows the match, it doesn't suppress real ones —
    a wish stamped 1963 against an owned 1963 copy of that issue IS owned, so it
    must stay a conflict (else an owned book gets exported In Collection=0 and
    deleted, BUI-122)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Fantastic Four (1961 - 1996)",
        full_title="Fantastic Four #18",
        release_date="1963-09-01",
    )])
    _seed_wish_list([{"name": "Fantastic Four #18", "id": 1, "year": "1963"}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Fantastic Four #18"]


def test_wish_list_conflicts_year_scoped_tolerates_one_year_skew(tmp_path, monkeypatch):
    """BUI-387 reuses the existing ±1 year-gate tolerance (BUI-214/BUI-251):
    a wish stamped 1982 against an owned copy whose release_date is 1983 (the
    cover-vs-onsale skew — the ASM #238 incident class) still conflicts. Year
    scoping must NOT tighten the gate into a strict equality that false-negates
    a 1-year skew and hides an owned book."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="The Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #238",
        release_date="1983-01-01",   # onsale slipped past the 1982 cover year
    )])
    _seed_wish_list([{"name": "Amazing Spider-Man #238", "id": 1, "year": "1982"}])

    result = cmds.cmd_wish_list_conflicts()

    assert [c["name"] for c in result["conflicts"]] == ["Amazing Spider-Man #238"]


def test_wish_list_set_year_stamps_named_entry_only(tmp_path):
    """BUI-387 backfill: set-year stamps the given Cover Year onto the EXACT
    named entry and no other, and the value is stored verbatim."""
    from locg.commands import cmd_wish_list_set_year, cmd_wish_list_from_cache

    _seed_wish_list([
        {"name": "The X-Men #1", "id": 1},
        {"name": "The X-Men #2", "id": 2},
    ])

    result = cmd_wish_list_set_year("The X-Men #1", "1963")

    assert result["status"] == "ok"
    assert result["matched"] == 1
    assert result["year"] == "1963"
    items = {it["name"]: it for it in cmd_wish_list_from_cache()}
    assert items["The X-Men #1"]["year"] == "1963"
    assert "year" not in items["The X-Men #2"]     # only the named entry stamped


def test_wish_list_set_year_rejects_non_four_digit_year(tmp_path):
    """BUI-387/BUI-129 guard: set-year refuses anything that isn't a 4-digit
    year — including the ``1963 - 2011`` series year-RANGE form, a common
    year_began paste that would reintroduce the wrong-year data-loss bug."""
    from locg.commands import cmd_wish_list_set_year, cmd_wish_list_from_cache

    _seed_wish_list([{"name": "The X-Men #1", "id": 1}])

    for bad in ["1963 - 2011", "63", "", "nineteen"]:
        result = cmd_wish_list_set_year("The X-Men #1", bad)
        assert "error" in result, bad

    # Nothing was written on rejection.
    assert "year" not in cmd_wish_list_from_cache()[0]


def test_wish_list_set_year_unknown_name_is_error(tmp_path):
    """BUI-387: naming an entry not present is an explicit error, never a silent
    no-op that a backfill script could mistake for success."""
    from locg.commands import cmd_wish_list_set_year

    _seed_wish_list([{"name": "The X-Men #1", "id": 1}])

    result = cmd_wish_list_set_year("Uncanny X-Men #500", "2008")

    assert "error" in result
    assert "not found" in result["error"]


def test_wish_list_set_year_backfill_clears_decoy_end_to_end(tmp_path, monkeypatch):
    """BUI-387 end-to-end: an unstamped vintage wish flags a modern-owned decoy;
    after the set-year backfill stamps its Cover Year, a fresh audit no longer
    flags it — the structural fix the ticket asks for."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_owned_modern_ff18_row()])
    _seed_wish_list([{"name": "Fantastic Four #18", "id": 1}])  # unstamped

    before = cmds.cmd_wish_list_conflicts()
    assert [c["name"] for c in before["conflicts"]] == ["Fantastic Four #18"]

    stamp = cmds.cmd_wish_list_set_year("Fantastic Four #18", "1963")
    assert stamp["status"] == "ok"

    after = cmds.cmd_wish_list_conflicts()
    assert after["conflicts"] == []


def test_wish_list_year_survives_collection_import(tmp_path, monkeypatch):
    """BUI-387 forward-compat: a stamped `year` is durable across a
    `collection import` (BUI-208: import no longer touches wish-list.json), so
    the backfill isn't silently undone on the next sync."""
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    _seed_wish_list([{"name": "The X-Men #1", "id": None, "source": "local", "year": "1963"}])

    cmds.cmd_collection_import(str(SAMPLE_XLSX))  # rewrites collection cache only

    items = {it["name"]: it for it in cmds.cmd_wish_list_from_cache()}
    assert items["The X-Men #1"]["year"] == "1963"     # stamp preserved


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


def test_record_win_dedup_does_not_collapse_annual_into_base(tmp_path):
    """BUI-184 (record-win owned_index): the dedup must NOT collapse an Annual
    into its base issue.

    Real LOCG exports file annuals/specials under the BASE 'Series Name' with the
    'Annual' qualifier living only in 'Full Title' (e.g. Series Name
    "The Amazing Spider-Man" / Full Title "The Amazing Spider-Man Annual #14").
    owned_index therefore keys on the Full Title PREFIX (which carries the
    qualifier), not series_name — so an owned "... Annual #6" does not shadow a
    genuine base "#6". Keying dedup on series_name (the tempting "shared series
    key" fix) would false-skip the base issue → a re-won book later reads as
    owned → duplicate buy. This test locks the safe direction.
    """
    from locg.commands import cmd_collection_record_win
    from locg.collection_cache import rebuild_series_name_index

    cache = make_cache(tmp_path)
    # Mirrors real LOCG shape: base series_name, qualifier only in full_title.
    _seed_cache(cache, [{
        **_agent_win_row(
            series="Fantastic Four",
            full_title="Fantastic Four Annual #6",
            release_date="1968-11-01",
        ),
        "source": "locg_export",
    }])
    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")

    # A base Fantastic Four #6 win is a different comic — it must be recorded.
    result = cmd_collection_record_win(
        [_make_win(series="Fantastic Four", issue="6", year=1962)],
        cache=cache,
        metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


# ---------------------------------------------------------------------------
# cmd_collection_check cross-volume ambiguity (BUI-284)
# ---------------------------------------------------------------------------

def test_check_no_year_cross_volume_returns_ambiguous(tmp_path, monkeypatch):
    """BUI-284: with no `year`, the same issue number owned under two distinct
    volumes of the same masthead must surface `ambiguous_cross_volume`, not a
    silent (arbitrary-volume) `in_collection`.

    Fantastic Four #18 exists in both the 1961 Vol. 1 and the 2022 Vol. 7. The
    normalized series key collapses the volume decoration, so with no year the
    matcher can't tell which one the caller meant — guessing the first row is a
    dangerous false positive (tells the caller they own a book they may not).
    """
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Fantastic Four (Vol. 7) (2022 - 2025)",
            full_title="Fantastic Four #18",
            release_date="2024-02-14",
        ),
        _agent_win_row(
            series="Fantastic Four (Vol. 1) (1961 - 1996)",
            full_title="Fantastic Four #18",
            release_date="1963-09-01",
        ),
    ])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="18")
    assert result["match_status"] == "ambiguous_cross_volume"
    assert result["match_kind"] == "cross_volume"
    # Both colliding volumes are surfaced for the caller to disambiguate.
    names = {c["series_name"] for c in result["candidates"]}
    assert names == {
        "Fantastic Four (Vol. 7) (2022 - 2025)",
        "Fantastic Four (Vol. 1) (1961 - 1996)",
    }


def test_check_no_year_single_era_still_in_collection(tmp_path, monkeypatch):
    """BUI-284 no-regression: a single owned era with no year still resolves to
    `in_collection` (the guard only fires when >1 distinct volume/era matches)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Fantastic Four (Vol. 1) (1961 - 1996)",
        full_title="Fantastic Four #18",
        release_date="1963-09-01",
    )])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="18")
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Fantastic Four #18"
    assert "candidates" not in result


def test_check_year_supplied_resolves_cross_volume(tmp_path, monkeypatch):
    """BUI-284: supplying the per-issue cover year resolves the collision via the
    release-date gate — the year-supplied path is unchanged (no ambiguity)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Fantastic Four (Vol. 7) (2022 - 2025)",
            full_title="Fantastic Four #18",
            release_date="2024-02-14",
        ),
        _agent_win_row(
            series="Fantastic Four (Vol. 1) (1961 - 1996)",
            full_title="Fantastic Four #18",
            release_date="1963-09-01",
        ),
    ])

    # The 1963 volume is owned → year resolves to that specific row.
    result = cmds.cmd_collection_check(
        series="Fantastic Four", issue="18", year="1963"
    )
    assert result["match_status"] == "in_collection"
    assert result["matched_series_name"] == "Fantastic Four (Vol. 1) (1961 - 1996)"


def test_check_no_year_undecorated_two_eras_ambiguous_by_release_year(tmp_path, monkeypatch):
    """BUI-284: even when two eras share a bare (undecorated) series_name, a
    difference in release_date year alone trips the ambiguity guard."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Fantastic Four",
            full_title="Fantastic Four #1",
            release_date="1961-11-08",
        ),
        _agent_win_row(
            series="Fantastic Four",
            full_title="Fantastic Four #1",
            release_date="1998-01-14",
        ),
    ])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="1")
    assert result["match_status"] == "ambiguous_cross_volume"


def test_check_no_year_same_volume_dated_and_dateless_not_ambiguous(tmp_path, monkeypatch):
    """BUI-284 no-regression: a dateless record-win row (BUI-105) sharing a
    volume with a dated row is NOT ambiguous — an empty release_date/series_name
    is not counted as a distinct era, so this still resolves to `in_collection`."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Amazing Spider-Man (1963 - 1998)",
            full_title="Amazing Spider-Man #300",
            release_date="1988-05-10",
        ),
        # Same volume, second copy written before its date was stamped.
        _agent_win_row(
            series="Amazing Spider-Man (1963 - 1998)",
            full_title="Amazing Spider-Man #300",
            release_date="",
        ),
    ])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="300")
    assert result["match_status"] == "in_collection"


def test_check_no_year_two_copies_same_row_not_ambiguous(tmp_path, monkeypatch):
    """BUI-284 no-regression: two identical owned rows (same volume, same date)
    are a single era — not cross-volume ambiguity."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Amazing Spider-Man (1963 - 1998)",
            full_title="Amazing Spider-Man #300",
            release_date="1988-05-10",
        ),
        _agent_win_row(
            series="Amazing Spider-Man (1963 - 1998)",
            full_title="Amazing Spider-Man #300",
            release_date="1988-05-10",
        ),
    ])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="300")
    assert result["match_status"] == "in_collection"


def test_check_no_year_same_volume_year_skew_not_ambiguous(tmp_path, monkeypatch):
    """BUI-284 no-regression: two owned rows sharing one (undecorated) volume
    whose release years differ only by the ±1 cover-vs-on-sale skew are NOT
    misread as two eras — the release-year branch requires a gap wider than 1."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Amazing Spider-Man",
            full_title="Amazing Spider-Man #238",
            release_date="1982-11-01",
        ),
        _agent_win_row(
            series="Amazing Spider-Man",
            full_title="Amazing Spider-Man #238",
            release_date="1983-01-01",  # one-year cover-vs-onsale skew
        ),
    ])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="238")
    assert result["match_status"] == "in_collection"


def test_wish_list_conflicts_flags_cross_volume_owned(tmp_path, monkeypatch):
    """BUI-284/BUI-130: a wish-listed book owned under >1 volume must still be
    flagged as a conflict. The audit is year-free, so the owned book returns
    `ambiguous_cross_volume`; treating that as not-owned would let the owned copy
    be exported In Collection=0 and deleted (BUI-122)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Fantastic Four (Vol. 1) (1961 - 1996)",
            full_title="Fantastic Four #18",
            release_date="1963-09-01",
        ),
        _agent_win_row(
            series="Fantastic Four (Vol. 7) (2022 - 2025)",
            full_title="Fantastic Four #18",
            release_date="2024-02-14",
        ),
    ])
    # Wish-list contains the same issue (owned under two volumes).
    _seed_wish_list([{"name": "Fantastic Four #18", "id": None, "source": "local"}])

    audit = cmds.cmd_wish_list_conflicts()
    assert audit["checked"] == 1
    assert len(audit["conflicts"]) == 1
    assert audit["conflicts"][0]["name"] == "Fantastic Four #18"


def test_collection_check_reports_owned_helper():
    """BUI-284: the owned-guard helper treats in_collection AND
    ambiguous_cross_volume as owned, but not not_in_cache."""
    import locg.commands as cmds

    assert cmds.collection_check_reports_owned({"match_status": "in_collection"})
    assert cmds.collection_check_reports_owned({"match_status": "ambiguous_cross_volume"})
    assert not cmds.collection_check_reports_owned({"match_status": "not_in_cache"})
    assert not cmds.collection_check_reports_owned({})


# ---------------------------------------------------------------------------
# cmd_collection_check printing-marker conflict (BUI-364)
# ---------------------------------------------------------------------------

def _amm_rows() -> list[dict[str, Any]]:
    """The confirmed BUI-364 incident state: the 2nd printing is owned while the
    base printing is tracked wish-list-only (in_collection=0, in_wish_list=1)."""
    owned_reprint = _agent_win_row(
        publisher="DC Comics",
        series="Absolute Martian Manhunter (2025)",
        full_title="Absolute Martian Manhunter #1 2nd Printing",
        release_date="2025-06-18",
        gixen_item_id="147000000001",
    )
    wished_base = _agent_win_row(
        publisher="DC Comics",
        series="Absolute Martian Manhunter (2025)",
        full_title="Absolute Martian Manhunter #1",
        release_date="2025-03-19",
        gixen_item_id="147000000002",
    )
    wished_base["in_collection"] = 0
    wished_base["in_wish_list"] = 1
    return [owned_reprint, wished_base]


def test_check_owned_reprint_flags_printing_conflict(tmp_path, monkeypatch):
    """BUI-364 (the required AMM #1 case): a query WITHOUT a printing marker
    matched by an owned '2nd Printing' row must not read as an unqualified
    `in_collection` — the verdict carries printing_conflict=True plus the
    conflicting rows, showing the base printing is wish-listed, not owned."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows())

    result = cmds.cmd_collection_check(series="Absolute Martian Manhunter", issue="1")
    # match_status is unchanged (the reprint IS owned) — the flag qualifies it.
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Absolute Martian Manhunter #1 2nd Printing"
    assert result["in_wish_list"] is True
    assert result["printing_conflict"] is True

    by_title = {c["full_title"]: c for c in result["printing_candidates"]}
    base = by_title["Absolute Martian Manhunter #1"]
    assert base["in_collection"] is False
    assert base["in_wish_list"] is True
    assert base["printing_ordinal"] == 1
    reprint = by_title["Absolute Martian Manhunter #1 2nd Printing"]
    assert reprint["in_collection"] is True
    assert reprint["printing_ordinal"] == 2


def test_check_owned_reprint_flags_printing_conflict_with_year(tmp_path, monkeypatch):
    """BUI-364: the flag also fires on a year-bearing query (the buy path
    forwards the identify cover year, BUI-316) — the year gate tolerates the
    reprint's later same-year release_date, so the conflation survives it."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows())

    result = cmds.cmd_collection_check(
        series="Absolute Martian Manhunter", issue="1", year="2025"
    )
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is True


def test_check_query_with_printing_variant_no_conflict(tmp_path, monkeypatch):
    """BUI-364: a query that DOES carry the printing marker (variant='2nd
    Printing') matches the owned reprint row cleanly — no conflict, no
    candidates payload."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, _amm_rows())

    result = cmds.cmd_collection_check(
        series="Absolute Martian Manhunter", issue="1", variant="2nd Printing"
    )
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Absolute Martian Manhunter #1 2nd Printing"
    assert result["printing_conflict"] is False
    assert "printing_candidates" not in result


def test_check_base_owned_no_conflict(tmp_path, monkeypatch):
    """BUI-364 no-regression: a plain owned base row with no printing marker
    stays an unflagged in_collection."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="300")
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is False
    assert "printing_candidates" not in result


def test_check_owned_first_printing_row_no_conflict(tmp_path, monkeypatch):
    """BUI-364: an explicit '1st Printing' row is the base printing — an
    unmarked query means printing 1, so this is a clean match, not a conflict."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Amazing Spider-Man #300 1st Printing",
    )])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="300")
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is False


def test_check_word_ordinal_printing_marker_flagged(tmp_path, monkeypatch):
    """BUI-364: word-form markers ('Second Printing') are detected too."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        full_title="Amazing Spider-Man #300 Second Printing",
    )])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="300")
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is True


def test_check_both_printings_owned_no_conflict(tmp_path, monkeypatch):
    """BUI-364: owning BOTH the base and the 2nd printing must not flag a base
    query, regardless of which row the matcher happens to return first — the
    queried printing is genuinely owned."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    # Reprint row FIRST so the matcher returns it before the base row.
    reprint = _agent_win_row(
        full_title="Amazing Spider-Man #300 2nd Printing",
        release_date="1988-08-10",
        gixen_item_id="101",
    )
    base = _agent_win_row(gixen_item_id="102")
    _seed_cache(cache, [reprint, base])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="300")
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is False


def test_check_printing_query_satisfied_by_base_row_flagged(tmp_path, monkeypatch):
    """BUI-364 (reverse direction): a '2nd Printing' query matched only by the
    owned BASE row (the BUI-176 variant soft-preference fallback) is the same
    distinct-collectible conflation — flag it rather than reading 'owns the
    base' as 'owns the reprint'."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])  # base ASM #300 only

    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="300", variant="2nd Printing"
    )
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Amazing Spider-Man #300"
    assert result["printing_conflict"] is True


def test_check_not_in_cache_printing_conflict_false(tmp_path, monkeypatch):
    """BUI-364: the field is present (False) on a not_in_cache verdict so
    callers can read it unconditionally."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="999")
    assert result["match_status"] == "not_in_cache"
    assert result["printing_conflict"] is False
    assert "printing_candidates" not in result


def test_check_alias_match_printing_conflict_flagged(tmp_path, monkeypatch):
    """BUI-364: the probe follows the alias pass — an owned 'Thor #154 2nd
    Printing' satisfying a year-bearing 'The Mighty Thor #154' query is flagged,
    and the candidate scan covers the alias key the match landed on."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row(
        series="Thor (Vol. 1) (1966 - 1996)",
        full_title="Thor #154 2nd Printing",
        release_date="1968-07-01",
    )])

    result = cmds.cmd_collection_check(
        series="The Mighty Thor", issue="154", year="1968"
    )
    assert result["match_status"] == "in_collection"
    assert result["match_kind"] == "alias"
    assert result["printing_conflict"] is True
    titles = {c["full_title"] for c in result["printing_candidates"]}
    assert "Thor #154 2nd Printing" in titles


def test_check_dateless_cross_era_row_does_not_clear_flag(tmp_path, monkeypatch):
    """BUI-364 review fix (BUI-197 MUST-FIX 2 mirrored): with a year supplied,
    a DATELESS owned base row must not clear the printing-conflict flag — the
    year gate fails open on it, so a dateless copy from a different era would
    silently reproduce the missed-purchase incident. It still appears in the
    candidates list (display fail-open)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    reprint = _agent_win_row(
        series="Fantastic Four (Vol. 7) (2022 - 2025)",
        full_title="Fantastic Four #18 2nd Printing",
        release_date="2024-04-10",
        gixen_item_id="301",
    )
    dateless_base = _agent_win_row(
        series="Fantastic Four (Vol. 1) (1961 - 1996)",
        full_title="Fantastic Four #18",
        release_date="",  # e.g. an index-resolved record-win, date not stamped
        gixen_item_id="302",
    )
    _seed_cache(cache, [reprint, dateless_base])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="18", year="2024")
    assert result["match_status"] == "in_collection"
    assert result["full_title_matched"] == "Fantastic Four #18 2nd Printing"
    assert result["printing_conflict"] is True
    titles = {c["full_title"] for c in result["printing_candidates"]}
    assert "Fantastic Four #18" in titles  # dateless row still visible


def test_check_printing_candidates_are_year_gated(tmp_path, monkeypatch):
    """BUI-364 review fix: a wrong-era same-masthead row must not pollute the
    candidates list — a wished 2008 'Hulk #1' would otherwise render as 'the
    2021 query's own printing is explicitly wanted'."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    reprint = _agent_win_row(
        series="Hulk (2021 - 2023)",
        full_title="Hulk #1 2nd Printing",
        release_date="2021-12-15",
        gixen_item_id="401",
    )
    wrong_era_wish = _agent_win_row(
        series="Hulk (2008 - 2012)",
        full_title="Hulk #1",
        release_date="2008-01-09",
        gixen_item_id="402",
    )
    wrong_era_wish["in_collection"] = 0
    wrong_era_wish["in_wish_list"] = 1
    _seed_cache(cache, [reprint, wrong_era_wish])

    result = cmds.cmd_collection_check(series="Hulk", issue="1", year="2021")
    assert result["printing_conflict"] is True
    titles = {c["full_title"] for c in result["printing_candidates"]}
    assert titles == {"Hulk #1 2nd Printing"}  # 2008 row filtered out


def test_check_both_printings_owned_with_year_no_conflict(tmp_path, monkeypatch):
    """BUI-364: the owned-escape's year-gate arm — a DATED same-era owned base
    row clears the flag on a year-bearing query (contrast with the dateless
    and wrong-era cases above)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    reprint = _agent_win_row(
        full_title="Amazing Spider-Man #300 2nd Printing",
        release_date="1988-08-10",
        gixen_item_id="501",
    )
    base = _agent_win_row(gixen_item_id="502")  # 1988-05-10 base row
    _seed_cache(cache, [reprint, base])

    result = cmds.cmd_collection_check(
        series="Amazing Spider-Man", issue="300", year="1988"
    )
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is False


def test_check_alias_filed_owned_base_clears_flag(tmp_path, monkeypatch):
    """BUI-364 review fix: the probe scans the FULL masthead-equivalence key
    set — an owned base filed under an alias masthead (BUI-200's X-Men split:
    reprint under 'Uncanny X-Men', base under 'The X-Men') clears the flag
    instead of being misstated as untracked."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    reprint = _agent_win_row(
        series="Uncanny X-Men (Vol. 1) (1963 - 2011)",
        full_title="Uncanny X-Men #107 2nd Printing",
        release_date="1977-10-01",
        gixen_item_id="601",
    )
    alias_base = _agent_win_row(
        series="The X-Men (Vol. 1) (1963 - 1981)",
        full_title="The X-Men #107",
        release_date="1977-10-01",
        gixen_item_id="602",
    )
    _seed_cache(cache, [reprint, alias_base])

    result = cmds.cmd_collection_check(series="Uncanny X-Men", issue="107", year="1977")
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is False


def test_check_ambiguous_cross_volume_carries_printing_field(tmp_path, monkeypatch):
    """BUI-364 shape parity: the ambiguous_cross_volume verdict also carries
    printing_conflict, so callers can read the field on every verdict."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [
        _agent_win_row(
            series="Fantastic Four (Vol. 7) (2022 - 2025)",
            full_title="Fantastic Four #18",
            release_date="2024-02-14",
            gixen_item_id="201",
        ),
        _agent_win_row(
            series="Fantastic Four (Vol. 1) (1961 - 1996)",
            full_title="Fantastic Four #18",
            release_date="1963-09-01",
            gixen_item_id="202",
        ),
    ])

    result = cmds.cmd_collection_check(series="Fantastic Four", issue="18")
    assert result["match_status"] == "ambiguous_cross_volume"
    assert result["printing_conflict"] is False


# ---------------------------------------------------------------------------
# BUI-373: unified printing-marker detector (extended spellings + drift guard)
# ---------------------------------------------------------------------------

_PRINTING_MARKER_SPELLINGS = [
    ("2nd Printing", 2),
    ("2nd Print", 2),
    ("2nd Ptg", 2),
    ("2nd Ptgs", 2),
    ("Second Printing", 2),
    ("Second Print", 2),
    ("3rd Printing", 3),
    ("Third Printing", 3),
    ("3rd Ptg", 3),
    ("4th Printing", 4),
    ("Fourth Printing", 4),
]


@pytest.mark.parametrize("text,expected_ordinal", _PRINTING_MARKER_SPELLINGS)
def test_printing_ordinal_spelling_coverage(text, expected_ordinal):
    """BUI-373: every spelling the ticket calls out ("Ptg", digit AND word
    ordinals) is recognized by the shared detector, with the correct
    ordinal — not just recognized as *a* marker, but as the RIGHT one."""
    from locg.commands import _printing_ordinal

    assert _printing_ordinal(f"Amazing Spider-Man #300 {text}") == expected_ordinal


def test_printing_ordinal_bare_reprint_is_unspecified_not_base():
    """BUI-373: a bare "Reprint"/"Reprints" (no ordinal) reads as SOME later
    printing, not the base (1) — this is the previously-unrecognized spelling
    that used to silently produce no printing_conflict flag."""
    from locg.commands import _printing_ordinal, _UNSPECIFIED_REPRINT_ORDINAL

    assert _printing_ordinal("Amazing Spider-Man #300 Reprint") == _UNSPECIFIED_REPRINT_ORDINAL
    assert _printing_ordinal("Amazing Spider-Man #300 Reprints") == _UNSPECIFIED_REPRINT_ORDINAL
    assert _UNSPECIFIED_REPRINT_ORDINAL != 1


@pytest.mark.parametrize("text", [
    "Amazing Spider-Man #300 Reprint",
    "Amazing Spider-Man #300 Reprints",
    "Amazing Spider-Man #300 Re-Print",
    "Amazing Spider-Man #300 Re Print",
    "Amazing Spider-Man #300 RE-PRINT",
    "Amazing Spider-Man #300 re print",
])
def test_printing_ordinal_bare_reprint_spelling_variants(text):
    """BUI-373 review (adversarial pass): "Reprint" is commonly spelled with a
    hyphen or space ("Re-Print"/"Re Print") in real eBay/LOCG titles — all
    variants must resolve to the same unspecified-reprint sentinel, not
    silently read as the base (ordinal 1)."""
    from locg.commands import _printing_ordinal, _UNSPECIFIED_REPRINT_ORDINAL

    assert _printing_ordinal(text) == _UNSPECIFIED_REPRINT_ORDINAL


@pytest.mark.parametrize("text,expected_ordinal", [
    ("1st Reprint", 2),
    ("First Reprint", 2),
    ("2nd Reprint", 3),
    ("Second Reprint", 3),
    ("3rd Reprint", 4),
])
def test_printing_ordinal_reprint_with_ordinal_offsets_by_one(text, expected_ordinal):
    """BUI-373 review (adversarial pass): "Nth Reprint" names the Nth print run
    AFTER the original — "1st Reprint" is the SECOND print run overall
    (equivalent to "2nd Printing"), not the first. Without this +1 offset,
    "1st Reprint"/"First Reprint" would compute to ordinal 1 and be silently
    indistinguishable from an unmarked base query or an explicit "1st
    Printing" row — reproducing the exact collision this detector exists to
    prevent, for one specific spelling."""
    from locg.commands import _printing_ordinal

    assert _printing_ordinal(f"Amazing Spider-Man #300 {text}") == expected_ordinal


@pytest.mark.parametrize("text", [
    "Amazing Spider-Man #300",
    "Amazing Spider-Man #300 1st Printing",
    "Amazing Spider-Man #300 Art Print Variant",
    "Amazing Spider-Man #300 Printing Error Variant",
    "Blueprint Comics #1",
    "Fine Print Publishing #1",
    "Amazing Spider-Man #300 Newsstand Edition",
    "Pre-Print Ashcan #1",
    "More Prints Available #1",
])
def test_printing_ordinal_false_positive_guard(text):
    """BUI-373: bare "print"/"printing" with no ordinal, and words that merely
    CONTAIN "print" ("Blueprint", "Art Print", "Printing Error"), must NOT read
    as a printing marker — the danger direction the ticket calls out (a false
    marker on a genuinely-owned base could suppress a legitimate conflict
    verdict). An explicit "1st Printing" is the base printing (ordinal 1).
    "Pre-Print"/"More Prints" (adversarial pass) must not falsely trigger the
    hyphen/space-tolerant bare-"Reprint" spelling either — "re" only counts as
    the reprint prefix when it starts its own word (a \\b boundary), not when
    it's the tail of "Pre" or the head of "prints" inside "More Prints"."""
    from locg.commands import _printing_ordinal

    assert _printing_ordinal(text) == 1


def test_printing_marker_suffix_canonicalizes_explicit_ordinals():
    """BUI-373: the record-win full_title builder's consumer of the shared
    detector produces the canonical "<N>th Printing" suffix for any recognized
    EXPLICIT-ordinal spelling — including ones VARIANT_SUFFIX_MAP never had a
    key for ("2nd Ptg", "Third Printing")."""
    from locg.commands import _printing_marker_suffix

    assert _printing_marker_suffix("2nd ptg") == "2nd Printing"
    assert _printing_marker_suffix("2nd Printing") == "2nd Printing"
    assert _printing_marker_suffix("third printing") == "3rd Printing"
    assert _printing_marker_suffix("Fourth Ptg") == "4th Printing"


def test_printing_marker_suffix_canonicalizes_reprint_with_ordinal():
    """BUI-373 review: "1st Reprint"/"2nd Reprint" carry an EXPLICIT ordinal
    (just spelled with "Reprint" instead of "Printing"), so — unlike a bare
    "Reprint" — the full_title builder DOES canonicalize them, using the
    +1-adjusted ordinal ("1st Reprint" = the second print run = "2nd
    Printing")."""
    from locg.commands import _printing_marker_suffix

    assert _printing_marker_suffix("1st reprint") == "2nd Printing"
    assert _printing_marker_suffix("second reprint") == "3rd Printing"


@pytest.mark.parametrize("n,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (10, "10th"), (11, "11th"), (12, "12th"), (13, "13th"),
    (20, "20th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (24, "24th"),
    (100, "100th"), (101, "101st"), (111, "111th"), (113, "113th"),
])
def test_ordinal_suffix_boundary_cases(n, expected):
    """Coverage gap flagged independently by 3 reviewers (correctness,
    maintainability, testing): the 11th-13th vs 21st-23rd English ordinal
    exception was only exercised for n in {2, 3, 4} before this test."""
    from locg.commands import _ordinal_suffix

    assert _ordinal_suffix(n) == expected


def test_printing_conflict_candidates_translate_sentinel_to_null(tmp_path, monkeypatch):
    """BUI-373 review (maintainability): _UNSPECIFIED_REPRINT_ORDINAL (-1) is
    an internal sentinel — it must never leak into the public
    printing_candidates JSON as a raw -1 (undocumented, could be misread as a
    real negative ordinal). A same-era bare-"Reprint" candidate reports
    printing_ordinal: null instead."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    owned_reprint = _agent_win_row(
        series="Absolute Martian Manhunter (2025)",
        full_title="Absolute Martian Manhunter #1 Reprint",
        release_date="2025-06-18",
    )
    wished_base = _agent_win_row(
        series="Absolute Martian Manhunter (2025)",
        full_title="Absolute Martian Manhunter #1",
        release_date="2025-03-19",
    )
    wished_base["in_collection"] = 0
    wished_base["in_wish_list"] = 1
    _seed_cache(cache, [owned_reprint, wished_base])

    result = cmds.cmd_collection_check(series="Absolute Martian Manhunter", issue="1")
    assert result["printing_conflict"] is True
    by_title = {c["full_title"]: c for c in result["printing_candidates"]}
    assert by_title["Absolute Martian Manhunter #1 Reprint"]["printing_ordinal"] is None
    assert by_title["Absolute Martian Manhunter #1"]["printing_ordinal"] == 1


def test_printing_marker_suffix_declines_ambiguous_bare_reprint():
    """BUI-373: a bare "Reprint" has no explicit ordinal, so the full_title
    builder deliberately does NOT guess one — writing a specific printing
    number into a LOCG-facing title on a guess risks minting a title that
    mismatches the real catalog row. Falls through to the existing
    manual-variant path instead (same as an unrecognized cover variant)."""
    from locg.commands import _printing_marker_suffix

    assert _printing_marker_suffix("reprint") is None
    assert _printing_marker_suffix("Reprints") is None
    assert _printing_marker_suffix("newsstand") is None  # not a printing marker at all


def test_variant_suffix_map_no_longer_encodes_printing_markers():
    """BUI-373 drift guard: VARIANT_SUFFIX_MAP used to carry its OWN printing
    keys ("2nd print"/"second print"/"2nd printing") — a second, independent,
    less-complete detector. They must never come back; printing recognition
    lives ONLY in _printing_ordinal/_PRINTING_MARKER_RE. Guards against a
    future edit silently reintroducing the drift this ticket fixes."""
    from locg.commands import VARIANT_SUFFIX_MAP

    printing_keys = {"2nd print", "second print", "2nd printing", "2nd ptg", "reprint"}
    assert not (set(VARIANT_SUFFIX_MAP.keys()) & printing_keys)
    # Every remaining key is a genuinely non-printing edition suffix.
    assert set(VARIANT_SUFFIX_MAP.keys()) == {
        "newsstand", "newsstand edition", "direct", "direct edition",
        "facsimile", "facsimile edition",
    }


@pytest.mark.parametrize("text,_expected", _PRINTING_MARKER_SPELLINGS)
def test_printing_marker_detection_agrees_across_call_sites(text, _expected):
    """BUI-373 drift guard: the two real call sites — the record-win dedup
    guard (_dedup_variant_compatible) and the full_title suffix builder
    (_printing_marker_suffix) — must agree that each spelling in the coverage
    corpus is a printing marker distinct from the base. If a future change
    forked either one back onto its own lexicon, one of these would silently
    stop agreeing with the other (and with the collection-check
    printing_conflict probe, which shares the same _printing_ordinal call)."""
    from locg.commands import _dedup_variant_compatible, _printing_marker_suffix

    # Dedup guard: this spelling must NOT be treated as compatible with an
    # unmarked (base) owned row — they are distinct printings.
    assert _dedup_variant_compatible(text.lower(), None) is False
    # full_title builder: every spelling here carries an EXPLICIT ordinal, so
    # it must canonicalize to a suffix rather than falling through unresolved.
    assert _printing_marker_suffix(text) is not None


def test_dedup_variant_compatible_cross_ordinal_mismatch():
    """BUI-373 review (testing gap): two DIFFERENT specifically-numbered
    printings must not be treated as dedup-compatible with each other either
    — not just each against the base. "2nd Printing" vs "3rd Printing"."""
    from locg.commands import _dedup_variant_compatible

    assert _dedup_variant_compatible("2nd printing", "3rd printing") is False
    assert _dedup_variant_compatible("2nd printing", "2nd printing") is True


# ---------------------------------------------------------------------------
# BUI-373: record-win integration — full_title suffix + dedup, extended spellings
# ---------------------------------------------------------------------------

def test_record_win_2nd_ptg_suffix(tmp_path):
    """BUI-373: the "Ptg" abbreviation, previously unrecognized by
    VARIANT_SUFFIX_MAP, now canonicalizes the same way "newsstand" does."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)")
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", variant_text="2nd ptg")],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    assert result["manual_variant_count"] == 0
    row = cache.load()["comics"][-1]
    assert row["full_title"].endswith("2nd Printing")
    assert row["needs_manual_variant"] is False


def test_record_win_dedup_2nd_ptg_matches_owned_2nd_printing(tmp_path):
    """BUI-373: a "2nd Ptg" win IS deduped against an owned "2nd Printing" row
    of the same issue — same printing, different spelling, recognized as the
    same ordinal by the shared detector."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(
        cache, "Uncanny X-Men (Vol. 1) (1980 - 2011)", "Uncanny X-Men #201 2nd Printing",
    )

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="201", year=1985, variant_text="2nd ptg")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0


def test_record_win_dedup_base_not_conflated_with_owned_2nd_ptg_reprint(tmp_path):
    """BUI-373 (the bug this ticket fixes): a base-edition win must not be
    deduped against an owned "2nd Ptg" row — the exact BUI-267 Newsstand bug
    pattern, but for a printing spelling VARIANT_SUFFIX_MAP never recognized.
    Before this fix, both sides read as an unrecognized/unknown suffix and the
    dedup guard permissively (and wrongly) skipped recording the base win."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(
        cache, "Uncanny X-Men (Vol. 1) (1980 - 2011)", "Uncanny X-Men #201 2nd Ptg",
    )

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="201", year=1985)],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1


def test_record_win_dedup_bare_reprint_matches_bare_reprint(tmp_path):
    """BUI-373: two independently-unspecified "Reprint" labels (no ordinal on
    either side) ARE treated as the same printing — the safe reading of two
    identically-ambiguous labels."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(
        cache, "Uncanny X-Men (Vol. 1) (1980 - 2011)", "Uncanny X-Men #201 Reprint",
    )

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="201", year=1985, variant_text="reprint")],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0


def test_record_win_dedup_base_not_conflated_with_owned_bare_reprint(tmp_path):
    """BUI-373 review (testing/correctness gap): the bare-"Reprint" sibling of
    test_record_win_dedup_base_not_conflated_with_owned_2nd_ptg_reprint — a
    base-edition win must not dedup against an owned bare "Reprint" row
    either. Confirms the asymmetric direction (unspecified reprint vs base)
    stays safe end-to-end through record-win, not just at the
    _dedup_variant_compatible unit level."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_owned_row(
        cache, "Uncanny X-Men (Vol. 1) (1980 - 2011)", "Uncanny X-Men #201 Reprint",
    )

    result = cmd_collection_record_win(
        [_make_win(series="Uncanny X-Men", issue="201", year=1985)],
        cache=cache, metron=_null_metron(),
    )

    assert result["skipped_already_owned"] == 0
    assert result["rows_written"] == 1
