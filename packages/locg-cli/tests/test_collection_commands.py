"""Tests for the collection cache CLI commands (Unit 4)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_XLSX = FIXTURES / "collection_export_sample.xlsx"


@pytest.fixture(autouse=True)
def metron_sleeps(monkeypatch):
    """Capture every ``locg.commands`` sleep instead of serving it (BUI-465).

    ``cmd_collection_record_win`` now paces itself against Metron's 20 req/min
    budget and cools down after a transient trip, so an unpatched run of this
    module would spend minutes of real wall-clock inside ``time.sleep``. Autouse
    so no existing test has to opt in; the returned list is the assertion surface
    for the tests that check the pacing itself.
    """
    slept: list[float] = []
    monkeypatch.setattr("locg.commands.time.sleep", slept.append)
    return slept


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


# ---------------------------------------------------------------------------
# cmd_collection_series_names_resolve (BUI-449)
# ---------------------------------------------------------------------------

def _seed_series_name_index(tmp_path, index: dict[str, str]):
    import locg.commands as cmds

    cache = make_cache(tmp_path)

    def mutate(payload):
        payload["series_name_index"] = index
    cache.apply(mutate, command="seed")
    return cache


def test_resolve_exact_match_strips_vol_suffix(tmp_path, monkeypatch):
    """Metron's '(Vol. 1)' decoration is already neutralized by
    _normalize_series_key, so this resolves via the exact-key pass — no
    fuzzy fallback needed."""
    import locg.commands as cmds

    cache = _seed_series_name_index(tmp_path, {"uncanny x-men": "Uncanny X-Men"})
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(["Uncanny X-Men (Vol. 1)"])
    assert result == {
        "results": [
            {
                "query": "Uncanny X-Men (Vol. 1)",
                "resolved": "Uncanny X-Men",
                "match_kind": "exact",
            }
        ]
    }


def test_resolve_exact_match_strips_leading_article(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = _seed_series_name_index(tmp_path, {"incredible hulk": "The Incredible Hulk"})
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(["Incredible Hulk"])
    assert result["results"][0]["resolved"] == "The Incredible Hulk"
    assert result["results"][0]["match_kind"] == "exact"


def test_resolve_fuzzy_match_for_punctuation_alt_spelling(tmp_path, monkeypatch):
    """The narrow BUI-171 residual: a genuine alt-spelling (here, a dropped
    period) that survives the exact-key pass unmatched but is an unambiguous
    fuzzy match."""
    import locg.commands as cmds

    cache = _seed_series_name_index(tmp_path, {"ms. marvel": "Ms. Marvel"})
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(["Ms Marvel"])
    assert result["results"][0]["resolved"] == "Ms. Marvel"
    assert result["results"][0]["match_kind"] == "fuzzy"


def test_resolve_no_confident_match_returns_none(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = _seed_series_name_index(tmp_path, {"batman": "Batman"})
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(["Aquaman"])
    assert result["results"][0] == {
        "query": "Aquaman",
        "resolved": None,
        "match_kind": None,
    }


def test_resolve_never_conflates_annual_with_base_series(tmp_path, monkeypatch):
    """BUI-26 guard: a query for the base masthead must never fuzzy-match a
    distinct line-extension that merely shares the masthead (Annual /
    Giant-Size / King-Size / Special) — the Jaccard threshold is high enough
    that the extra disambiguating token always sinks the score."""
    import locg.commands as cmds

    cache = _seed_series_name_index(
        tmp_path, {"fantastic four annual": "Fantastic Four Annual"}
    )
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(["Fantastic Four"])
    assert result["results"][0]["resolved"] is None
    assert result["results"][0]["match_kind"] is None


def test_resolve_ambiguous_fuzzy_match_returns_none(tmp_path, monkeypatch):
    """Two catalog names both clear the fuzzy threshold for the same query —
    genuinely ambiguous, so refuse to guess rather than picking one."""
    import locg.commands as cmds

    cache = _seed_series_name_index(
        tmp_path,
        {
            "foo bar": "Foo Bar",
            "foo-bar": "Foo-Bar",
        },
    )
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(["Foo Bar!"])
    assert result["results"][0]["resolved"] is None
    assert result["results"][0]["match_kind"] is None


def test_resolve_multiple_names_preserves_order(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = _seed_series_name_index(
        tmp_path,
        {"uncanny x-men": "Uncanny X-Men", "batman": "Batman"},
    )
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    result = cmds.cmd_collection_series_names_resolve(
        ["Uncanny X-Men (Vol. 1)", "Aquaman", "Batman"]
    )
    queries = [r["query"] for r in result["results"]]
    resolved = [r["resolved"] for r in result["results"]]
    assert queries == ["Uncanny X-Men (Vol. 1)", "Aquaman", "Batman"]
    assert resolved == ["Uncanny X-Men", None, "Batman"]


def test_resolve_empty_cache_returns_no_match(tmp_path, monkeypatch):
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))
    result = cmds.cmd_collection_series_names_resolve(["Batman"])
    assert result == {"results": [{"query": "Batman", "resolved": None, "match_kind": None}]}


# --- BUI-449: the _fuzzy_series_name_match helper directly ---

def test_fuzzy_series_name_match_confident_hit():
    from locg.commands import _fuzzy_series_name_match

    assert _fuzzy_series_name_match("Ms Marvel", ["Ms. Marvel"]) == "Ms. Marvel"


def test_fuzzy_series_name_match_rejects_low_similarity():
    from locg.commands import _fuzzy_series_name_match

    assert _fuzzy_series_name_match("Fantastic Four", ["Fantastic Four Annual"]) is None


def test_fuzzy_series_name_match_no_candidates():
    from locg.commands import _fuzzy_series_name_match

    assert _fuzzy_series_name_match("Batman", []) is None


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


def _metron_hit(
    series_name: str = "Amazing Spider-Man (1963 - 1998)",
    year_began: int = 1963,
    year_end: int | None = 1998,
    publisher: str | None = "Marvel Comics",
):
    """MetronClient stub that returns a successful lookup.

    BUI-458: also stubs ``lookup_issue_detail`` (the full-issue detail fetch
    record-win now makes to capture the publisher) so a recorded win carries a
    real ``publisher_name``. Pass ``publisher=None`` to simulate a Metron issue
    with no publisher (the row must then keep ``publisher_name`` null)."""
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
    m.lookup_issue_detail.return_value = {
        "variants": [],
        "credits": [],
        "publisher": publisher,
    }
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
    # does attempt a Metron *issue* lookup to backfill a real release_date.
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


def _seed_index_for(cache, series: str, full_title: str):
    """Seed one locg_export row and rebuild series_name_index off it, so a win
    for `series` resolves via the index with no Metron series call."""
    from locg.collection_cache import rebuild_series_name_index

    _seed_cache(cache, [{
        **_agent_win_row(series=series, full_title=full_title),
        "source": "locg_export",
    }])

    def rebuild(payload):
        payload["series_name_index"] = rebuild_series_name_index(payload)
    cache.apply(rebuild, command="test-rebuild")
    return cache


def _dated_metron(store_date: str, *, metron_id: int = 77):
    from unittest.mock import MagicMock

    m = MagicMock()
    m.lookup_issue.return_value = {
        "metron_id": metron_id,
        "cover_date": None,
        "store_date": store_date,
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "X-Men",
        "series_id": 5,
    }
    m.format_series_name.return_value = "X-Men (1963 - 1981)"
    m.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel Comics",
    }
    m.degraded = False
    return m


def test_record_win_null_year_dates_from_series_range(tmp_path):
    """BUI-464 AC: a win comic-identify could not date at all ("X-Men 109 - 1st
    Weapon Alpha VG/Fine Cond" carries no year) no longer ships dateless. The
    index-resolved volume's own window supplies the era evidence the win lacks,
    so the BUI-210 date-only lookup may run and its hit is gated on that window."""
    from locg.commands import cmd_collection_record_win

    cache = _seed_index_for(
        make_cache(tmp_path), "The X-Men (Vol. 1) (1963 - 1981)", "The X-Men #1"
    )
    metron = _dated_metron("1977-11-08")

    cmd_collection_record_win(
        [_make_win(series="X-Men", issue="109", year=None, item_id="NY1")],
        cache=cache, metron=metron,
    )

    metron.lookup_issue.assert_called_once()
    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The X-Men (Vol. 1) (1963 - 1981)"
    assert row["release_date"] == "1977-11-08"
    assert row["metron_id"] == 77
    assert row["publisher_name"] == "Marvel Comics"


def test_record_win_null_year_rejects_out_of_era_hit(tmp_path):
    """The guard is real, not a rubber stamp: a null-year win whose Metron hit
    is a modern reprint of the same masthead keeps its correct index-resolved
    series but takes NO date, id, or publisher from the wrong-era hit (BUI-467's
    drop applied on era evidence that is not the win's own year)."""
    from locg.commands import cmd_collection_record_win

    cache = _seed_index_for(
        make_cache(tmp_path), "The X-Men (Vol. 1) (1963 - 1981)", "The X-Men #1"
    )
    metron = _dated_metron("2005-03-09")

    cmd_collection_record_win(
        [_make_win(series="X-Men", issue="109", year=None, item_id="NY2")],
        cache=cache, metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The X-Men (Vol. 1) (1963 - 1981)"
    assert row["release_date"] is None
    assert row["metron_id"] is None
    # A loud null beats a quiet wrong value (BUI-458) — a reprint under another
    # imprint must never import its publisher onto a vintage row.
    assert row["publisher_name"] is None


def test_record_win_null_year_no_era_evidence_makes_no_metron_call(tmp_path):
    """Unchanged where there is nothing to guard with: a null-year win whose
    series is NOT in the index has no independent era evidence, so BUI-464 must
    not open the ungated lookup for it. It stays dateless and manual — never
    auto-dated against a guessed volume."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _null_metron()

    result = cmd_collection_record_win(
        [_make_win(series="Spawn Directors Cut", issue="1", year=None, item_id="NY3")],
        cache=cache, metron=metron,
    )

    assert result["manual_series_count"] == 1
    row = cache.load()["comics"][-1]
    assert row["needs_manual_series_canonical"] is True
    assert row["release_date"] is None
    # No placeholder is fabricated from the volume's start year — that would
    # stamp a wrong, undeletable date (a placeholder fails _reconcile_score
    # open, so a wrong one silently drops the win).


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
    as a different era. The reprint date must be dropped when its year
    disagrees with the win's own year; the BUI-105 {year}-01-01 placeholder
    is stamped in its place (metron_data is now fully dropped — see below —
    so this row lands on the same placeholder path as a plain Metron miss,
    matching the sibling BUI-210 index-path test).

    BUI-467: a rejected hit is positive evidence of the WRONG issue, so its
    issue-level metadata (metron_id, publisher_name) must be dropped too —
    not just the date. Before the fix this reprint hit's publisher ("Marvel
    Comics" here, but a real reprint under a DIFFERENT imprint would import
    silently to LOCG) and metron_id rode along on the row despite the guard
    rejecting the very date that flagged it as a reprint. The independently
    corroborated series resolution (canonical_series) is the one thing this
    path exists to produce and is kept."""
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
    # A different imprint than the genuine 1991 Marvel original — if this
    # leaked onto the row it would import a wrong publisher to LOCG silently.
    metron.lookup_issue_detail.return_value = {"variants": [], "credits": [], "publisher": "2022 Reprint Imprint"}
    metron.degraded = False

    result = cmd_collection_record_win(
        [_make_win(series="Infinity Gauntlet", issue="1", year=1991)],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["series_name"] == "The Infinity Gauntlet (1991) (1991 - 1991)"
    # The rejected date is not kept verbatim; BUI-105 stamps the placeholder
    # (blanked again on export, per collection_io._is_placeholder_release_date).
    assert row["release_date"] == "1991-01-01"
    # BUI-467: the rejected hit's issue-level metadata must not survive —
    # null publisher trips audit-pending's backstop instead of importing the
    # reprint's (wrong) imprint silently.
    assert row["publisher_name"] is None
    assert row["metron_id"] is None


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
    # BUI-458: record-win reuses this same detail fetch to also capture the
    # publisher, so mirror the real client's dict shape (variants + publisher).
    m.lookup_issue_detail.return_value = {
        "variants": variants,
        "credits": [],
        "publisher": "Image Comics",
    }
    return m


def test_record_win_metron_variant_match(tmp_path):
    """Unknown variant text resolves via Metron issue-detail fuzzy match."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_with_variants("Spawn (1992 - Present)", ["Capullo Variant", "Direct Edition"])
    # BUI-467: year must agree with _metron_with_variants' 1992-05-01 cover_date
    # (±1) or the reprint guard drops metron_data before variant matching runs.
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", year=1992, variant_text="Capullo Variant")],
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
    # BUI-467: year must agree with _metron_with_variants' 1992-05-01 cover_date
    # (±1) or the reprint guard drops metron_data before variant matching runs.
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", year=1992, variant_text="Capullo Variant")],
        cache=cache,
        metron=metron,
    )

    assert result["metron_variant_lookups_attempted"] == 1
    assert result["metron_variant_matches"] == 0
    assert result["manual_variant_count"] == 1
    assert cache.load()["comics"][-1]["needs_manual_variant"] is True


def test_record_win_no_variant_skips_variant_detail_match(tmp_path):
    """A known-suffix variant resolves via the suffix map, not a fuzzy match
    against Metron variant names, so it must not count as a variant lookup.

    BUI-458: the issue-detail call itself IS now made (once) to capture the
    publisher, but the variant-lookup counter stays 0 because no variant fuzzy
    match was attempted (the suffix map already produced the full_title)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_with_variants("Spawn (1992 - Present)", ["Capullo Variant"])
    # BUI-467: year must agree with _metron_with_variants' 1992-05-01 cover_date
    # (±1) or the reprint guard drops metron_data before the detail fetch runs.
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", year=1992, variant_text="newsstand")],
        cache=cache,
        metron=metron,
    )
    assert result["metron_variant_lookups_attempted"] == 0
    # BUI-458: detail fetched once (for publisher), not repeatedly.
    metron.lookup_issue_detail.assert_called_once()
    assert cache.load()["comics"][-1]["full_title"].endswith("Newsstand Edition")


# --- BUI-458: record-win captures a real publisher from Metron ---

def test_record_win_persists_metron_publisher(tmp_path):
    """BUI-458: a recorded win carries the Metron issue's publisher (not None),
    so the wins-only export imports to LOCG with a publisher instead of
    Not Found."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)", publisher="Marvel Comics")
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", year=1988)],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["publisher_name"] == "Marvel Comics"


def test_record_win_publisher_null_on_metron_miss(tmp_path):
    """BUI-458 data safety: when Metron never resolves an id (a plain miss), the
    win row's publisher stays null — never a fabricated or defaulted guess — and
    no wasted issue-detail call is made."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _null_metron()
    result = cmd_collection_record_win(
        [_make_win(series="Totally Unknown Series", issue="1", year=1995)],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["publisher_name"] is None
    # No metron_id resolved → no issue-detail fetch spent on a miss.
    metron.lookup_issue_detail.assert_not_called()


def test_record_win_publisher_null_when_metron_detail_has_no_publisher(tmp_path):
    """BUI-458 data safety: Metron resolves the issue but carries no publisher on
    it → the row keeps a null publisher (never guessed from the masthead/series).
    A missing publisher is caught by the pre-upload audit, which is the intended
    backstop; a wrong one would import silently."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)", publisher=None)
    result = cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", year=1988)],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    assert row["publisher_name"] is None


def test_record_win_variant_win_captures_publisher_with_single_detail_fetch(tmp_path):
    """BUI-458 no-latency-regression: a variant win captures the publisher AND
    resolves its variant cover from a SINGLE shared issue-detail fetch (the
    publisher capture reuses the same detail the variant match needs)."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    # _metron_with_variants stubs a publisher of "Image Comics" (Spawn).
    metron = _metron_with_variants("Spawn (1992 - Present)", ["Capullo Variant", "Direct Edition"])
    # BUI-467: year must agree with _metron_with_variants' 1992-05-01 cover_date
    # (±1) or the reprint guard drops metron_data before variant matching runs.
    result = cmd_collection_record_win(
        [_make_win(series="Spawn", issue="313", year=1992, variant_text="Capullo Variant")],
        cache=cache, metron=metron,
    )

    assert result["rows_written"] == 1
    assert result["metron_variant_matches"] == 1
    row = cache.load()["comics"][-1]
    assert row["publisher_name"] == "Image Comics"
    assert row["full_title"] == "Spawn #313 Capullo Variant"
    # Exactly one detail call served both publisher capture and variant match.
    metron.lookup_issue_detail.assert_called_once()


def test_record_win_publisher_flows_to_export_csv_and_passes_audit(tmp_path):
    """BUI-458 end-to-end: the stored publisher flows through the CSV export
    column the audit reads, so `audit-pending` no longer flags "no publisher"
    for a freshly recorded win."""
    import csv as _csv
    from locg.commands import cmd_collection_record_win, cmd_collection_audit_pending
    from locg.collection_io import _row_to_csv_dict, LOCG_XLSX_HEADERS

    cache = make_cache(tmp_path)
    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)", publisher="Marvel Comics")
    cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man", issue="300", year=1988)],
        cache=cache, metron=metron,
    )
    row = cache.load()["comics"][-1]

    # The export maps publisher_name → the "Publisher Name" column faithfully.
    csv_row = _row_to_csv_dict(row)
    assert csv_row["Publisher Name"] == "Marvel Comics"

    # Write a real wins CSV and audit it — no "no publisher" flag.
    csv_path = tmp_path / "wins.csv"
    with csv_path.open("w", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=LOCG_XLSX_HEADERS)
        writer.writeheader()
        writer.writerow(csv_row)

    audit = cmd_collection_audit_pending(str(csv_path))
    assert audit["row_count"] == 1
    publisher_flags = [
        r for r in audit["flagged_rows"] if "no publisher" in r["issues"]
    ]
    assert publisher_flags == []


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
    # BUI-458: record-win now fetches issue detail for the publisher.
    metron.lookup_issue_detail.return_value = {"variants": [], "credits": [], "publisher": "Marvel Comics"}

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
    # BUI-458: record-win now fetches issue detail for the publisher.
    metron.lookup_issue_detail.return_value = {"variants": [], "credits": [], "publisher": "Marvel Comics"}

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
    # BUI-458: record-win now fetches issue detail for the publisher.
    metron.lookup_issue_detail.return_value = {"variants": [], "credits": [], "publisher": "Marvel Comics"}

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

def _index_seeded_cache(
    tmp_path,
    *,
    series="The X-Men (1963 - 1981)",
    full_title="The X-Men #1",
):
    """Cache with a locg_export row so the series resolves via series_name_index
    (the no-Metron-for-series path) — the BUI-210 scenario."""
    from locg.collection_cache import rebuild_series_name_index

    cache = make_cache(tmp_path)
    _seed_cache(cache, [{
        **_agent_win_row(series=series, full_title=full_title),
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
    # BUI-458: record-win now fetches issue detail for the publisher.
    metron.lookup_issue_detail.return_value = {"variants": [], "credits": [], "publisher": "Marvel Comics"}

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
    """BUI-210 reprint guard: a Metron date decades from the win's year (a
    collected-edition/reprint, e.g. The X-Men #59 → 2005) is rejected, and the
    {year}-01-01 placeholder is kept.

    The ±1 widening (BUI-210 reopen) must NOT weaken this: 2005 vs 1970 is
    35 years, so the guard still rejects it outright."""
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
    # Reprint rejected → placeholder kept (never a wrong 2005 date), metron_id
    # stays None so the export blanks the placeholder, and the whole
    # metron_data is dropped: the id/publisher came from the same suspect
    # issue match, so none of it is trustworthy on its own.
    assert row["release_date"] == "1970-01-01"
    assert row["metron_id"] is None
    assert row["publisher_name"] is None


def test_record_win_clean_year_cover_date_seller_era_skew_accepted(tmp_path):
    """BUI-486 (threading): a CLEAN-year win resolved via series_name_index now
    receives the resolved volume's INDEPENDENT window, so a cover_date one year
    off a seller-supplied era claim is accepted instead of thrown away.

    Batman #427 ("… DC 1989 … A Death in the Family") is cover-dated 1988-12,
    with no store_date (vintage). Before this change the clean year 1989 meant
    index_series_range was never computed, the exact cover_date gate rejected
    1988 vs 1989, the whole hit was dropped, and the row shipped a 1989-01-01
    placeholder. Now the window Batman (1940 - 2011) contains 1988, so the real
    date is kept."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(
        tmp_path, series="Batman (1940 - 2011)", full_title="Batman #1"
    )
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 427,
        "cover_date": "1988-12-01",
        "store_date": None,  # vintage: forces the cover_date gate to run
        "series_year_began": 1940,
        "series_year_end": 2011,
        "series_name": "Batman",
        "series_id": 42,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "DC Comics"
    }

    result = cmd_collection_record_win(
        [_make_win(series="Batman (1940 - 2011)", issue="427", year=1989)],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
    row = cache.load()["comics"][-1]
    # Real cover date kept (not the 1989-01-01 placeholder), metron_id + the
    # BUI-458 publisher survive because the hit was accepted, not dropped.
    assert row["release_date"] == "1988-12-01"
    assert row["metron_id"] == 427
    assert row["publisher_name"] == "DC Comics"


def test_record_win_clean_year_cover_date_reprint_still_rejected(tmp_path):
    """BUI-486 safety: threading the window on the clean-year path must NOT
    weaken the reprint guard. A 2005 collected edition of a 1989 Batman is
    rejected even though 2005 sits INSIDE Batman (1940 - 2011) — the ±1 bound
    (16 years off year_raw), not the window, is what rejects it, and the
    placeholder is kept."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(
        tmp_path, series="Batman (1940 - 2011)", full_title="Batman #1"
    )
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 999,
        "cover_date": "2005-03-09",  # reprint year, inside the wide window
        "store_date": None,
        "series_year_began": 1940,
        "series_year_end": 2011,
        "series_name": "Batman",
        "series_id": 42,
    }

    cmd_collection_record_win(
        [_make_win(series="Batman (1940 - 2011)", issue="427", year=1989)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1989-01-01"  # placeholder kept
    assert row["metron_id"] is None  # whole hit dropped
    assert row["publisher_name"] is None


def test_record_win_step2_metron_path_does_not_fire_pm1_exception(tmp_path):
    """BUI-486 anti-circularity guard (black-box): the ±1 cover_date exception
    must fire ONLY against an INDEPENDENT volume window. On the step-2 path —
    where the series is NOT in series_name_index and the volume is resolved from
    the very Metron hit being judged — index_series_range stays None, so a clean
    year gets the EXACT gate and a ±1-off cover_date is rejected, not adopted.

    The hit here even carries series_year_began/end = 1940/2011 (a window that
    WOULD contain 1988): if a future refactor sourced the window from the hit,
    the exception would fire and this row would wrongly keep 1988-12-01 + a
    metron_id. That regression fails this test."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)  # empty index -> series never resolves locally
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 427,
        "cover_date": "1988-12-01",
        "store_date": None,
        "series_year_began": 1940,  # would-be circular window, must be ignored
        "series_year_end": 2011,
        "series_name": "Batman",
        "series_id": 42,
    }
    metron.format_series_name.return_value = "Batman (1940 - 2011)"

    cmd_collection_record_win(
        [_make_win(series="Batman", issue="427", year=1989)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    # No independent window -> exact gate -> ±1-off hit dropped -> placeholder.
    assert row["release_date"] == "1989-01-01"
    assert row["metron_id"] is None
    assert row["publisher_name"] is None


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


def test_record_win_index_path_accepts_prior_year_store_date(tmp_path):
    """BUI-210 (reopen), the production-evidence case: a January-cover book
    ships the PREVIOUS November, so Metron's store_date year is year−1.

    The old exact-year guard called that a reprint and threw the whole hit
    away, leaving a placeholder; the ±1 era window (shared with
    _year_gate_accepts) keeps LOCG's real on-sale date — and with it the
    metron_id and BUI-458 publisher that came on the same hit."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 901,
        "cover_date": "1970-01-01",   # genuine January cover
        "store_date": "1969-11-10",   # actual on-sale, the PRIOR year
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel Comics",
    }

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1969-11-10"
    assert row["metron_id"] == 901
    assert row["publisher_name"] == "Marvel Comics"


def test_record_win_accepts_following_year_store_date(tmp_path):
    """BUI-210 (reopen): the era window is SYMMETRIC (BUI-251), so a
    late-in-year cover whose on-sale slipped into the next January is kept
    too — not just the year−1 direction."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 904,
        "cover_date": "1970-12-01",
        "store_date": "1971-01-05",  # slipped into the FOLLOWING year
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel Comics",
    }

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    assert cache.load()["comics"][-1]["release_date"] == "1971-01-05"


def test_record_win_out_of_era_store_date_is_not_rescued_by_cover_date(tmp_path):
    """BUI-210 (reopen): the guard judges ONE candidate, on purpose.

    A modern store_date beside an original cover_date is the reprint
    fingerprint, so falling back to the cover_date would weaken the guard
    from "the best date is in era" to "some date is in era" and re-admit the
    very hit BUI-268 exists to reject."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 902,
        "cover_date": "1970-08-01",  # original cover date, in era
        "store_date": "2005-03-09",  # reprint-era store date
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel Comics",
    }

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1970-01-01"  # rejected → placeholder
    assert row["metron_id"] is None
    assert row["publisher_name"] is None


def test_record_win_retry_does_not_downgrade_a_prior_year_dated_row(tmp_path):
    """BUI-210 (reopen): a batch retry must not overwrite a good win row.

    `/comic:collection-add` re-submits the WHOLE batch after a
    partial_failure, and `write_wins` overwrites by `gixen_item_id` — so if
    the BUI-34 dedup fails to recognise the row it already wrote, the retry
    rebuilds it. With a prior-year store_date and an undecorated series name
    (no parseable `(YYYY - YYYY)` range), `_dedup_era_compatible`'s exact
    year compare did exactly that, silently downgrading a real date +
    publisher back to a placeholder + null. Worse, the retry is most likely
    when Metron is down, which is when the rebuild has nothing to restore."""
    from locg.collection_cache import rebuild_series_name_index
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    _seed_cache(cache, [{
        **_agent_win_row(series="Tomb of Dracula", full_title="Tomb of Dracula #1"),
        "source": "locg_export",
    }])
    cache.apply(
        lambda payload: payload.__setitem__(
            "series_name_index", rebuild_series_name_index(payload)
        ),
        command="test-rebuild",
    )

    win = _make_win(series="Tomb of Dracula", issue="10", year=1973)

    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 901,
        "cover_date": "1973-07-01",
        "store_date": "1972-11-10",  # prior-year on-sale date
        "series_year_began": 1972,
        "series_year_end": 1979,
        "series_name": "Tomb of Dracula",
        "series_id": 11,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel Comics",
    }
    cmd_collection_record_win([win], cache=cache, metron=metron)

    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1972-11-10"
    assert row["publisher_name"] == "Marvel Comics"

    # Retry the identical win with Metron unreachable — the common case.
    dead_metron = MagicMock()
    dead_metron.lookup_issue.return_value = None
    result = cmd_collection_record_win([win], cache=cache, metron=dead_metron)

    assert result["skipped_already_owned"] == 1
    assert result["rows_written"] == 0
    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1972-11-10"
    assert row["publisher_name"] == "Marvel Comics"


def test_record_win_off_by_one_cover_date_in_window_now_accepted(tmp_path):
    """BUI-486 supersedes the BUI-210-reopen exact-cover_date guard for the
    in-window ±1 case.

    This row is structurally IDENTICAL to Batman #427 (cover_date one year off a
    clean ``year_raw``, no store_date, inside the resolved volume window): the
    old exact gate rejected it and kept the placeholder, but ``year_raw`` may be
    a seller era claim skewed one year off the printed cover date, so BUI-486
    now accepts the hit when the cover year lands inside the resolved volume's
    INDEPENDENT window (1963-1981 contains 1969). There is no year-based test
    that could accept Batman #427 while rejecting this row — they are the same
    shape.

    Tradeoff recorded deliberately: the residual "a genuinely wrong-book hit one
    year off is now adopted" risk this used to guard is accepted, exactly as it
    already is on the ±1 store_date path (BUI-210) — a real reprint is decades
    off, which the ±1 bound still rejects (see
    test_record_win_clean_year_cover_date_reprint_still_rejected). The date is
    now Metron-backed (``metron_id`` set), so it survives export rather than
    being blanked as a placeholder."""
    import csv as _csv
    from locg.collection_io import generate_csv
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 999,
        "cover_date": "1969-01-01",
        "store_date": None,
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel UK",
    }

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    # Accepted: the real cover date is kept, the hit's metron_id + publisher come
    # with it (the whole hit is trusted, not dropped).
    assert row["release_date"] == "1969-01-01"
    assert row["metron_id"] == 999
    assert row["publisher_name"] == "Marvel UK"

    out = tmp_path / "wins.csv"
    generate_csv([row], out)
    with out.open(newline="") as f:
        exported = list(_csv.DictReader(f))
    # Metron-backed, so it survives export (not blanked as a placeholder shape).
    assert exported[0]["Release Date"] == "1969-01-01"


def test_metron_release_date_input_shapes():
    """Direct unit coverage of the one helper both date sites now share."""
    from locg.commands import _metron_release_date

    hit = {"store_date": "1970-06-09", "cover_date": "1970-08-01"}

    # No data / no dates → None.
    assert _metron_release_date(None, 1970) is None
    assert _metron_release_date({}, 1970) is None
    assert _metron_release_date({"store_date": None, "cover_date": None}, 1970) is None

    # store_date wins; cover_date is the fallback only when store_date is absent.
    assert _metron_release_date(hit, 1970) == "1970-06-09"
    assert _metron_release_date({"cover_date": "1970-08-01"}, 1970) == "1970-08-01"

    # store_date: year_raw accepted as int or str; ±1 both ways (the real
    # cover-vs-on-sale skew); anything further rejected.
    assert _metron_release_date(hit, "1970") == "1970-06-09"
    assert _metron_release_date({"store_date": "1969-11-10"}, 1970) == "1969-11-10"
    assert _metron_release_date({"store_date": "1971-01-05"}, 1970) == "1971-01-05"
    assert _metron_release_date({"store_date": "1972-01-05"}, 1970) is None
    assert _metron_release_date({"store_date": "2005-03-09"}, 1970) is None

    # cover_date: EXACT year only. It is the same quantity as year_raw, so a
    # one-year gap is a wrong-book signal, not skew — and on the cover_date
    # path this is the only era check the whole Metron result gets.
    assert _metron_release_date({"cover_date": "1970-08-01"}, 1970) == "1970-08-01"
    assert _metron_release_date({"cover_date": "1969-11-10"}, 1970) is None
    assert _metron_release_date({"cover_date": "1971-03-01"}, 1970) is None

    # Not a clean 4-digit year AND no series_range → nothing to guard against,
    # candidate returned ungated (the R36 series-resolution path's long-standing
    # behavior).
    for bad_year in (None, "", "197", "19700", "n/a"):
        assert _metron_release_date({"store_date": "2005-03-09"}, bad_year) == "2005-03-09"


def test_metron_release_date_series_range_gates_a_null_year():
    """BUI-464: with no identify year, the LOCG volume's own publication window
    stands in as the era guard — so a null year is no longer ungated."""
    from locg.commands import _metron_release_date

    xmen_vol1 = (1963, 1981)

    # In-window store_date for a genuinely 1977 X-Men #109 — accepted.
    assert _metron_release_date({"store_date": "1977-11-08"}, None, xmen_vol1) == "1977-11-08"
    # The reprint/collected-edition trap BUI-268 exists to catch. Before this
    # change a null year returned it ungated and poisoned the row.
    assert _metron_release_date({"store_date": "2005-03-09"}, None, xmen_vol1) is None
    # A modern-volume hit for the same masthead is likewise out of era.
    assert _metron_release_date({"cover_date": "2019-10-01"}, None, xmen_vol1) is None

    # store_date gets one year of slack at each edge: a volume's January-cover
    # first issue shipped the previous November.
    assert _metron_release_date({"store_date": "1962-11-05"}, None, xmen_vol1) == "1962-11-05"
    assert _metron_release_date({"store_date": "1961-11-05"}, None, xmen_vol1) is None
    # cover_date gets none — a cover date is by definition inside the run.
    assert _metron_release_date({"cover_date": "1962-11-05"}, None, xmen_vol1) is None
    assert _metron_release_date({"cover_date": "1963-09-01"}, None, xmen_vol1) == "1963-09-01"

    # An open-ended "(1980 - Present)" window still rejects a pre-run date.
    assert _metron_release_date({"cover_date": "2030-01-01"}, None, (1980, 9999)) == "2030-01-01"
    assert _metron_release_date({"cover_date": "1975-01-01"}, None, (1980, 9999)) is None

    # An undated candidate cannot be gated, so it is not accepted.
    assert _metron_release_date({"store_date": "not-a-date"}, None, xmen_vol1) is None

    # A clean year on the store_date path still wins outright — series_range
    # never loosens the ±1 store_date gate. (The cover_date path DOES consult it
    # for the BUI-486 exception; see test_metron_release_date_seller_era_claim.)
    assert _metron_release_date({"store_date": "1977-11-08"}, 1970, xmen_vol1) is None


def test_metron_release_date_seller_era_claim():
    """BUI-486: on the clean-year cover_date path, ``year_raw`` may be a
    seller-supplied ERA claim skewed ±1 off the printed cover date. A candidate
    one year off is accepted ONLY when it also sits inside the resolved volume's
    INDEPENDENT publication window — never by widening cover_date to a bare ±1.
    """
    from locg.commands import _metron_release_date

    batman = (1940, 2011)  # Batman (1940 - 2011), from series_name_index
    thor = (1966, 1996)  # Thor (1966 - 1996)

    # (a) The two measured BUI-474 rows: a correct hit one year off a seller
    # era claim, inside the resolved volume window — ACCEPTED (was rejected).
    assert _metron_release_date({"cover_date": "1988-12-01"}, 1989, batman) == "1988-12-01"
    assert _metron_release_date({"cover_date": "1979-06-01"}, 1978, thor) == "1979-06-01"
    # Skew in the other direction (cover year one AFTER the seller claim) is the
    # same cover-dating skew and is likewise accepted when in-window.
    assert _metron_release_date({"cover_date": "1990-01-01"}, 1989, batman) == "1990-01-01"

    # (b) Reprint protection is NOT weakened. A 2005 collected edition of a 1970
    # book stays rejected. Crucially this holds even when the window is WIDE
    # enough to CONTAIN 2005 — the ±1 bound, not the window, is what rejects it,
    # proving the bound is load-bearing (Batman #427 -> a 2005 reprint would sit
    # inside Batman (1940 - 2011)).
    assert _metron_release_date({"cover_date": "2005-03-09"}, 1970, (1963, 2011)) is None
    # And when the window also excludes it, still rejected (both bounds fail).
    assert _metron_release_date({"cover_date": "2005-03-09"}, 1970, (1963, 1981)) is None
    # A two-year gap is not cover-dating skew — rejected even inside the window.
    assert _metron_release_date({"cover_date": "1991-01-01"}, 1989, batman) is None

    # (c) A degenerate one-year window (a bare start-year decoration parsed to
    # (Y, Y)) cannot confirm a ±1-off candidate, so the row stays DATELESS — a
    # missed improvement, never a wrong date written. Here the resolver placed
    # the volume at only (1989, 1989), so a genuine 1988 cover is not accepted.
    assert _metron_release_date({"cover_date": "1988-12-01"}, 1989, (1989, 1989)) is None
    # With NO window at all the exact gate still governs — no ±1 slack, so the
    # off-by-one cover_date is rejected exactly as before BUI-486.
    assert _metron_release_date({"cover_date": "1988-12-01"}, 1989) is None
    assert _metron_release_date({"cover_date": "1988-12-01"}, 1989, None) is None

    # An exact-year cover_date still wins with no window consulted (unchanged).
    assert _metron_release_date({"cover_date": "1989-05-01"}, 1989, batman) == "1989-05-01"
    assert _metron_release_date({"cover_date": "1989-05-01"}, 1989) == "1989-05-01"

    # Window edges are INCLUSIVE, and the boundary is the one place the window
    # (not the ±1 bound) actually discriminates on the clean-year path — so pin
    # both bounds against a silent `<=` -> `<` regression that would discard a
    # genuine first/last-year-of-run issue.
    xmen = (1963, 1981)
    assert _metron_release_date({"cover_date": "1981-06-01"}, 1982, xmen) == "1981-06-01"  # upper edge in
    assert _metron_release_date({"cover_date": "1982-06-01"}, 1983, xmen) is None          # upper edge + 1 out
    assert _metron_release_date({"cover_date": "1963-06-01"}, 1962, xmen) == "1963-06-01"  # lower edge in
    assert _metron_release_date({"cover_date": "1962-06-01"}, 1961, xmen) is None          # lower edge - 1 out

    # An open-ended "(YYYY - Present)" window (end sentinel 9999) accepts a ±1
    # clean-year skew, and the ±1 bound still rejects a decades-off reprint that
    # the unbounded upper edge would otherwise admit.
    assert _metron_release_date({"cover_date": "2030-06-01"}, 2031, (1980, 9999)) == "2030-06-01"
    assert _metron_release_date({"cover_date": "2030-01-01"}, 1985, (1980, 9999)) is None

    # An undated/uncoercible cover_date on the ±1 branch cannot be placed, so it
    # is rejected even with a window present (never a wrong date).
    assert _metron_release_date({"cover_date": "not-a-date"}, 1989, batman) is None


def test_record_win_genuine_january_metron_date_survives_export(tmp_path):
    """BUI-210 (reopen) part (c): a REAL January cover date is not re-blanked.

    ``_is_placeholder_release_date`` keys on intent (agent_win AND no
    metron_id), not on the ``YYYY-01-01`` shape, so a Metron-sourced January
    date on a metron_id-bearing row exports intact. Locked end-to-end here —
    record-win write through CSV export — because the store writes both kinds
    of Jan-1 value, so the shape alone can never tell them apart."""
    import csv as _csv
    from locg.collection_io import generate_csv
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 903,
        "cover_date": "1970-01-01",
        "store_date": None,  # vintage issue: Metron has no on-sale date
        "series_year_began": 1963,
        "series_year_end": 1981,
        "series_name": "The X-Men",
        "series_id": 7,
    }
    metron.lookup_issue_detail.return_value = {
        "variants": [], "credits": [], "publisher": "Marvel Comics",
    }

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1970-01-01"
    assert row["metron_id"] == 903

    out = tmp_path / "wins.csv"
    generate_csv([row], out)
    with out.open(newline="") as f:
        exported = list(_csv.DictReader(f))
    assert exported[0]["Release Date"] == "1970-01-01"


def test_record_win_metron_miss_exports_blank_release_date(tmp_path):
    """BUI-210: a Metron miss ships a BLANK Release Date to LOCG.

    End-to-end (record-win write → CSV) so the two halves stay honest: the
    store keeps the placeholder for the era guards, and the export drops it
    because a wrong Jan-1 reads as "Not Found" on import. This is why
    removing the placeholder would not have made a single row less dateless
    — dateless CSV rows come from Metron misses, not from the stamp."""
    import csv as _csv
    from locg.collection_io import generate_csv
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = _index_seeded_cache(tmp_path)
    metron = MagicMock()
    metron.lookup_issue.return_value = None  # genuine miss

    cmd_collection_record_win(
        [_make_win(series="The X-Men (1963 - 1981)", issue="59", year=1970)],
        cache=cache,
        metron=metron,
    )

    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1970-01-01"

    out = tmp_path / "wins.csv"
    generate_csv([row], out)
    with out.open(newline="") as f:
        exported = list(_csv.DictReader(f))
    assert exported[0]["Release Date"] == ""


def test_placeholder_year_is_the_only_wrong_volume_reconcile_guard():
    """BUI-210 (reopen): why the BUI-105 placeholder must NOT be removed.

    Two volumes of one masthead normalize to the same series key and neither
    carries a "(Vol. N)", so `_reconcile_score`'s year comparison is the last
    thing standing between them. Dateless, that comparison fails OPEN and the
    wrong-volume candidate scores a match — which `_reconcile_phase`'s BUI-122
    collision guard then auto-heals (deletes) the pending win away, silently.

    A win stuck pending is recoverable; a win dropped on import is not."""
    from locg.collection_io import _reconcile_score

    win = {
        "publisher_name": "Marvel Comics",
        "series_name": "The X-Men (1963 - 1981)",
        "full_title": "The X-Men #59",
    }
    wrong_volume = {
        "publisher_name": "Marvel Comics",
        "series_name": "X-Men (1991 - 2011)",
        "full_title": "X-Men #59",
        "release_date": "1996-12-01",
    }

    assert _reconcile_score({**win, "release_date": "1970-01-01"}, wrong_volume) == 0
    # The counterfactual this test exists to forbid:
    assert _reconcile_score({**win, "release_date": None}, wrong_volume) > 0


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

def _always_degrading_metron():
    """MetronClient stub whose every lookup trips ``.degraded`` and returns None.

    Mirrors a real client that has exhausted its single capped retry — for a
    rate limit (BUI-260), a 5xx (BUI-342), or a connection error (BUI-255): no
    exception escapes, but ``.degraded`` flips True.
    """
    from unittest.mock import MagicMock

    metron = MagicMock()
    metron.degraded = False

    def _degrading_lookup(*_args, **_kwargs):
        metron.degraded = True
        return None

    metron.lookup_issue.side_effect = _degrading_lookup
    metron.lookup_issue_detail.side_effect = _degrading_lookup
    return metron


def test_record_win_metron_degraded_stops_this_win_not_the_batch(tmp_path):
    """BUI-465: a throttled Metron stops THIS win, then the batch recovers.

    Before BUI-465 the ``degraded`` breaker latched for the whole batch, so one
    transient trip on row 1 downgraded every remaining win to a `{year}-01-01`
    placeholder with a null publisher and no Metron call at all. That is what
    produced 40 of the 58 placeholder rows in the 2026-07-19 store. Now each
    trip buys a cooldown and a reset, so row 2 gets its own genuine attempt.
    """
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _always_degrading_metron()

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
    assert result["partial_failure"] is False
    # One call per win: the trip still short-circuits the rest of ITS OWN win
    # (no point re-asking a throttled server three times for one row), but row 2
    # is asked again rather than silently skipped.
    assert metron.lookup_issue.call_count == 2
    assert result["metron_transient_trips"] == 2
    assert result["metron_credentials_missing"] is False


def test_record_win_metron_5xx_recovers_between_wins(tmp_path):
    """BUI-342 + BUI-465: a 5xx trips the same breaker, and is equally transient.

    A server error is exactly the kind of failure that can clear between two
    rows, so it must not condemn the rest of the batch either. The BUI-342
    property this replaces — don't hammer a down server — is preserved by the
    per-win short-circuit plus the BUI-465 trip cap, not by a permanent latch.
    """
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _always_degrading_metron()

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
    assert result["partial_failure"] is False
    assert metron.lookup_issue.call_count == 2


def test_record_win_recovered_trip_still_resolves_later_wins(tmp_path):
    """BUI-465, the property that matters: a ONE-OFF trip costs one win, not all.

    The regression signature this pins is the 2026-07-19 one — row 1 carries a
    metron_id and every later row carries none, including further issues of a
    series row 1 had just resolved.
    """
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.degraded = False
    metron.format_series_name.return_value = "Ghost Rider (1990 - 1998)"
    hit = {
        "metron_id": 77,
        "cover_date": "1990-05-01",
        "store_date": "1990-03-14",
        "series_year_began": 1990,
        "series_year_end": 1998,
        "series_name": "Ghost Rider",
        "series_id": 5,
    }
    calls = {"n": 0}

    def _trips_once_then_works(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            metron.degraded = True
            return None
        metron.degraded = False
        return hit

    metron.lookup_issue.side_effect = _trips_once_then_works
    metron.lookup_issue_detail.return_value = {"variants": [], "publisher": "Marvel Comics"}

    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", series="Ghost Rider", issue="1", year=1990),
            _make_win(item_id="2", series="Ghost Rider", issue="2", year=1990),
            _make_win(item_id="3", series="Ghost Rider", issue="3", year=1990),
        ],
        cache=cache,
        metron=metron,
    )

    assert result["metron_transient_trips"] == 1
    rows = {r["gixen_item_id"]: r for r in cache.load()["comics"]}
    # Win 1 pays for the trip: no Metron data, and it says so.
    assert rows["1"]["release_date"] == "1990-01-01"
    assert rows["1"]["publisher_name"] is None
    assert rows["1"]["metron_unavailable"] is True
    # Wins 2 and 3 are fully resolved — the whole point.
    for item_id in ("2", "3"):
        assert rows[item_id]["release_date"] == "1990-03-14"
        assert rows[item_id]["publisher_name"] == "Marvel Comics"
        assert rows[item_id]["metron_unavailable"] is False
    assert result["metron_unavailable_rows"] == 1


def test_record_win_repeated_trips_eventually_latch_metron_off(tmp_path):
    """BUI-255 survives BUI-465: a genuinely down Metron is stopped asking.

    Recovery is capped at ``METRON_MAX_TRANSIENT_TRIPS``. Past that the breaker
    stays shut for the rest of the batch, so an unreachable Metron cannot buy a
    fresh capped sleep on every one of N remaining rows — the wedge BUI-255
    exists to prevent.
    """
    from locg.commands import METRON_MAX_TRANSIENT_TRIPS, cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _always_degrading_metron()
    wins = [_make_win(item_id=str(i), series="Ghost Rider", issue=str(i)) for i in range(1, 9)]

    result = cmd_collection_record_win(wins, cache=cache, metron=metron)

    assert result["rows_written"] == 8
    assert result["metron_transient_trips"] == METRON_MAX_TRANSIENT_TRIPS
    assert result["metron_degraded"] is True
    # One call for each forgiven trip, plus the one that exhausted the budget.
    assert metron.lookup_issue.call_count == METRON_MAX_TRANSIENT_TRIPS + 1
    # Every row after the latch is marked never-asked, not asked-and-missed.
    assert result["metron_unavailable_rows"] == 8


def test_record_win_credential_error_still_latches_for_the_whole_batch(tmp_path):
    """BUI-465 explicitly does NOT soften the credential path.

    Missing credentials are not transient — they are absent from the process
    environment and will still be absent on the next row — so this latches on
    the first failure and is never retried or cooled down.
    """
    from locg.commands import cmd_collection_record_win
    from locg.metron import MetronCredentialError
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.degraded = False
    metron.lookup_issue.side_effect = MetronCredentialError("no creds")

    result = cmd_collection_record_win(
        [_make_win(item_id=str(i), series="Ghost Rider", issue=str(i)) for i in range(1, 6)],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 5
    assert result["metron_credentials_missing"] is True
    assert result["metron_transient_trips"] == 0
    assert result["metron_degraded"] is True
    assert metron.lookup_issue.call_count == 1


def test_record_win_credential_error_costs_no_paced_requests(tmp_path, metron_sleeps):
    """A credential error never reaches the network, so it must not be paced.

    ``MetronCredentialError`` comes out of ``_get_session()`` before any HTTP
    request leaves the process. Charging it against the per-minute budget would
    make an unconfigured batch sleep for traffic Metron never saw.
    """
    from locg.commands import cmd_collection_record_win
    from locg.metron import MetronCredentialError
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)
    metron = MagicMock()
    metron.degraded = False
    metron.lookup_issue.side_effect = MetronCredentialError("no creds")

    result = cmd_collection_record_win(
        [_make_win(item_id=str(i), series="Ghost Rider", issue=str(i)) for i in range(1, 4)],
        cache=cache,
        metron=metron,
    )

    assert result["metron_requests_spent"] == 0
    assert metron_sleeps == []


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
    # Each row gets its own series-resolution attempt, proving the breaker never
    # tripped and skipped none of them. One call per row, not two: BUI-465 drops
    # the BUI-210 date-only retry when step-2 has already asked Metron this exact
    # question and been told no.
    assert metron.lookup_issue.call_count == 2
    # BUI-465: asked-and-missed, NOT never-asked. The breaker never shut, so the
    # missing publisher/date is Metron's real answer and a backfill retry would
    # only get the same one.
    assert all(not row["metron_unavailable"] for row in cache.load()["comics"])
    assert result["metron_unavailable_rows"] == 0


def test_record_win_paces_metron_against_the_documented_request_budget(tmp_path, metron_sleeps):
    """BUI-465: the batch throttles itself to Metron's 20 req/min burst budget.

    Nothing throttled record-win before. At ~3 HTTP requests per win (a
    two-request ``lookup_issue`` plus a one-request ``lookup_issue_detail``,
    BUI-458) a batch blew the 20/min allowance after ~7 wins and then tripped
    the breaker — which is what BUI-465's recovery path then has to clean up.
    Pacing is the prevention half; the recovery is the backstop.
    """
    from locg.commands import METRON_REQUESTS_PER_MINUTE, cmd_collection_record_win
    from locg.metron import REQUESTS_LOOKUP_ISSUE, REQUESTS_LOOKUP_ISSUE_DETAIL

    cache = make_cache(tmp_path)
    result = cmd_collection_record_win(
        [
            _make_win(item_id="1", issue="300"),
            _make_win(item_id="2", issue="301"),
            _make_win(item_id="3", issue="302"),
        ],
        cache=cache,
        metron=_metron_hit(),
    )

    per_win = REQUESTS_LOOKUP_ISSUE + REQUESTS_LOOKUP_ISSUE_DETAIL
    assert result["metron_requests_spent"] == 3 * per_win
    # Three wins, but only two waits: each win pays for the PREVIOUS win's
    # traffic, so the batch neither opens nor closes on a pointless sleep.
    seconds_per_request = 60.0 / METRON_REQUESTS_PER_MINUTE
    assert metron_sleeps == [seconds_per_request * per_win] * 2
    # The pace really is the documented budget, not merely nonzero.
    assert sum(metron_sleeps) / (result["metron_requests_spent"] - per_win) == seconds_per_request


def test_record_win_zero_requests_per_minute_disables_pacing(tmp_path, metron_sleeps):
    """``requests_per_minute=0`` opts out entirely, mirroring backfill's cadence=0."""
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    result = cmd_collection_record_win(
        [_make_win(item_id="1", issue="300"), _make_win(item_id="2", issue="301")],
        cache=cache,
        metron=_metron_hit(),
        requests_per_minute=0,
    )

    assert result["metron_requests_spent"] > 0
    assert metron_sleeps == []


def test_record_win_pacing_counts_requests_not_calls(tmp_path):
    """A two-request ``lookup_issue`` must cost twice a one-request detail fetch.

    Metering per CALL is the trap: it silently under-counts ``lookup_issue`` by
    half and lets a "paced" batch run at 40 req/min against a 20 req/min budget.
    """
    from locg.commands import cmd_collection_record_win
    from locg.metron import REQUESTS_LOOKUP_ISSUE, REQUESTS_LOOKUP_ISSUE_DETAIL

    assert REQUESTS_LOOKUP_ISSUE == 2 * REQUESTS_LOOKUP_ISSUE_DETAIL

    cache = make_cache(tmp_path)
    # A miss spends ONE lookup_issue (step-2 series resolution; BUI-465 drops the
    # byte-identical BUI-210 retry) and no detail fetch, because no metron_id
    # resolved to fetch detail for.
    missed = cmd_collection_record_win(
        [_make_win(item_id="1", issue="300")], cache=make_cache(tmp_path / "miss"),
        metron=_null_metron(),
    )
    assert missed["metron_requests_spent"] == REQUESTS_LOOKUP_ISSUE

    # A hit spends one lookup_issue plus one detail fetch.
    hit = cmd_collection_record_win(
        [_make_win(item_id="1", issue="300")], cache=cache, metron=_metron_hit(),
    )
    assert hit["metron_requests_spent"] == REQUESTS_LOOKUP_ISSUE + REQUESTS_LOOKUP_ISSUE_DETAIL


# ---------------------------------------------------------------------------
# cmd_collection_record_win — BUI-473 per-series reuse (real MetronClient)
#
# Unlike _null_metron/_metron_hit above (a bare MagicMock standing in for the
# WHOLE MetronClient), these wire a REAL MetronClient to a mocked mokkari
# session — the only way to exercise the actual resolve_series/issue_in_series
# split and its per-instance cache, rather than a stub that always answers the
# same canned dict regardless of what "series" was asked.
# ---------------------------------------------------------------------------

def _real_metron_with_mock_session(series_list=None, issues_list=None, issue_detail=None):
    """A real ``MetronClient`` wired to a mocked ``mokkari`` session."""
    from unittest.mock import MagicMock
    from locg.metron import MetronClient

    client = MetronClient()
    session = MagicMock()
    session.series_list.return_value = series_list if series_list is not None else []
    session.issues_list.return_value = issues_list if issues_list is not None else []
    detail = issue_detail
    if detail is None:
        detail = MagicMock()
        detail.variants = []
        detail.credits = []
        detail.publisher = None
    session.issue.return_value = detail
    client._session = session
    return client, session


def test_record_win_same_series_batch_spends_one_series_list_call(tmp_path):
    """BUI-473's acceptance criterion, end-to-end: N wins of the SAME series
    (none seeded in series_name_index, so all fall to the manual/Metron path
    where the whole pending backlog sits) spend exactly ONE series_list
    request against a real MetronClient, not N."""
    from datetime import date
    from unittest.mock import MagicMock
    from locg.commands import cmd_collection_record_win

    issue = MagicMock()
    issue.id = 100
    issue.cover_date = date(1963, 1, 1)
    issue.store_date = None

    series = MagicMock()
    series.id = 1
    series.display_name = "Fantastic Four"
    series.year_began = 1961
    series.year_end = 1996

    metron, session = _real_metron_with_mock_session(series_list=[series], issues_list=[issue])
    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id=str(i), series="Fantastic Four", issue=str(i), year=1963)
        for i in range(1, 6)
    ]

    result = cmd_collection_record_win(wins, cache=cache, metron=metron)

    assert result["rows_written"] == 5
    session.series_list.assert_called_once_with({"name": "Fantastic Four"})
    assert session.issues_list.call_count == 5


def test_record_win_same_series_batch_pacing_counter_reflects_reuse(tmp_path, metron_sleeps):
    """BUI-473: the pacing counter must charge the amortized cost, not the
    flat per-call constant, so a same-series run actually finishes faster —
    otherwise the real network saving above never turns into a wall-clock one.

    5 wins of one series: win 1 pays REQUESTS_LOOKUP_ISSUE (series miss) +
    REQUESTS_LOOKUP_ISSUE_DETAIL; wins 2-5 each pay only
    REQUESTS_ISSUE_IN_SERIES (series already cached) + REQUESTS_LOOKUP_ISSUE_DETAIL.
    """
    from datetime import date
    from unittest.mock import MagicMock
    from locg.commands import cmd_collection_record_win
    from locg.metron import (
        REQUESTS_ISSUE_IN_SERIES,
        REQUESTS_LOOKUP_ISSUE,
        REQUESTS_LOOKUP_ISSUE_DETAIL,
    )

    issue = MagicMock()
    issue.id = 100
    issue.cover_date = date(1963, 1, 1)
    issue.store_date = None

    series = MagicMock()
    series.id = 1
    series.display_name = "Fantastic Four"
    series.year_began = 1961
    series.year_end = 1996

    metron, _ = _real_metron_with_mock_session(series_list=[series], issues_list=[issue])
    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id=str(i), series="Fantastic Four", issue=str(i), year=1963)
        for i in range(1, 6)
    ]

    result = cmd_collection_record_win(wins, cache=cache, metron=metron)

    expected = (
        (REQUESTS_LOOKUP_ISSUE + REQUESTS_LOOKUP_ISSUE_DETAIL)
        + 4 * (REQUESTS_ISSUE_IN_SERIES + REQUESTS_LOOKUP_ISSUE_DETAIL)
    )
    assert result["metron_requests_spent"] == expected
    # Strictly less than the old flat-cost total (5 x 3 = 15) — the whole
    # point of the ticket.
    assert expected < 5 * (REQUESTS_LOOKUP_ISSUE + REQUESTS_LOOKUP_ISSUE_DETAIL)


def test_record_win_distinct_series_batch_still_pays_full_cost_each(tmp_path):
    """Control case: wins from genuinely DIFFERENT series must each pay their
    own series_list request — the cache must never falsely collapse them."""
    from datetime import date
    from unittest.mock import MagicMock
    from locg.commands import cmd_collection_record_win
    from locg.metron import REQUESTS_LOOKUP_ISSUE, REQUESTS_LOOKUP_ISSUE_DETAIL

    def _series(id_, name, began, end):
        s = MagicMock()
        s.id = id_
        s.display_name = name
        s.year_began = began
        s.year_end = end
        return s

    def _issue(id_, cover_year):
        i = MagicMock()
        i.id = id_
        i.cover_date = date(cover_year, 1, 1)
        i.store_date = None
        return i

    metron, session = _real_metron_with_mock_session()
    session.series_list.side_effect = [
        [_series(1, "Fantastic Four", 1961, 1996)],
        [_series(2, "Daredevil", 1964, 1998)],
        [_series(3, "Iron Man", 1968, 1996)],
    ]
    # Each issue's cover_date matches ITS win's year so every hit clears the
    # era gate (_metron_release_date) and reaches the detail fetch — the
    # request-count assertion below isolates series_list reuse, not the
    # unrelated date-gate accept/reject decision.
    session.issues_list.side_effect = [
        [_issue(101, 1963)],
        [_issue(102, 1964)],
        [_issue(103, 1968)],
    ]

    cache = make_cache(tmp_path)
    wins = [
        _make_win(item_id="1", series="Fantastic Four", issue="1", year=1963),
        _make_win(item_id="2", series="Daredevil", issue="1", year=1964),
        _make_win(item_id="3", series="Iron Man", issue="1", year=1968),
    ]

    result = cmd_collection_record_win(wins, cache=cache, metron=metron)

    assert session.series_list.call_count == 3
    assert result["metron_requests_spent"] == 3 * (REQUESTS_LOOKUP_ISSUE + REQUESTS_LOOKUP_ISSUE_DETAIL)


def test_record_win_does_not_repeat_an_identical_issue_lookup(tmp_path):
    """BUI-465: one issue query per win, not two identical ones.

    The R36 step-2 series lookup and the BUI-210 date-only lookup pass the same
    three arguments. On a step-2 miss the second call could only ever get the
    same answer, so it was two wasted requests against a 20/min budget — on the
    exact path (needs_manual_series_canonical) that every pending win in the
    2026-07-19 backlog was sitting on.
    """
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    metron = _null_metron()
    cmd_collection_record_win(
        [_make_win(item_id="1", series="Nowhere Comic", issue="7", year=1988)],
        cache=cache,
        metron=metron,
    )

    assert metron.lookup_issue.call_count == 1
    # Still the manual-series fallback with the BUI-105 placeholder — the dropped
    # call changed the cost, not the outcome.
    row = cache.load()["comics"][-1]
    assert row["needs_manual_series_canonical"] is True
    assert row["release_date"] == "1988-01-01"


def test_record_win_index_path_still_makes_its_date_lookup(tmp_path):
    """The BUI-465 dedup must not disarm BUI-210 on the path it exists for.

    When series_name_index resolves the series, step-2 never runs, so nothing has
    asked Metron anything yet — the date-only lookup is the row's ONLY chance at a
    real release date and must still fire.
    """
    from locg.collection_cache import rebuild_series_name_index
    from locg.commands import cmd_collection_record_win

    cache = make_cache(tmp_path)
    _seed_cache(cache, [{
        **_agent_win_row(series="Amazing Spider-Man (1963 - 1998)", full_title="Amazing Spider-Man #1"),
        "source": "locg_export",
    }])
    cache.apply(
        lambda payload: payload.__setitem__("series_name_index", rebuild_series_name_index(payload)),
        command="test-rebuild",
    )

    metron = _metron_hit("Amazing Spider-Man (1963 - 1998)")
    cmd_collection_record_win(
        [_make_win(series="Amazing Spider-Man (1963 - 1998)", issue="300", year=1988)],
        cache=cache,
        metron=metron,
    )

    assert metron.lookup_issue.call_count == 1
    row = cache.load()["comics"][-1]
    assert row["release_date"] == "1988-05-10"
    assert row["metron_unavailable"] is False


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
    # BUI-458: the Spawn metron-hit win triggers an issue-detail fetch for the publisher.
    metron.lookup_issue_detail.return_value = {"variants": [], "credits": [], "publisher": "Image Comics"}

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


# ---------------------------------------------------------------------------
# BUI-476: the wrong-store guard on the MUTATING commands (import, record-win)
#
# Same semantics as BUI-471's guard on `backfill`: fire only when `cache` is
# None AND LOCG_DATA_DIR is unset/blank. Read-only commands (check, status,
# export) are deliberately NOT guarded — that scope was declined.
#
# Every test below that unsets LOCG_DATA_DIR also installs `_no_default_store`.
# The autouse `_isolate_cache_dir` fixture (conftest.py) is what normally keeps
# a test off the REAL repo store, and `delenv` removes exactly that protection:
# if the guard ever regresses, `_cache_dir()` falls through to
# `<repo>/data/locg` and these tests would write the developer's live working
# cache *before* failing. The stub turns that into a loud AssertionError.
# ---------------------------------------------------------------------------


def _no_default_store(monkeypatch):
    """Make constructing the DEFAULT CollectionCache a hard failure.

    Both names must be patched: `cmd_collection_import` uses the module-global
    `commands.CollectionCache`, while `cmd_collection_record_win` re-imports it
    function-locally from `locg.collection_cache` (which shadows the global), so
    patching only one leaves the other live.
    """
    def _boom(*args, **kwargs):
        raise AssertionError(
            "BUI-476 guard regressed: the default store was constructed"
        )

    monkeypatch.setattr("locg.commands.CollectionCache", _boom)
    monkeypatch.setattr("locg.collection_cache.CollectionCache", _boom)


def test_record_win_no_explicit_cache_and_no_locg_data_dir_is_refused(monkeypatch):
    """Run bare (the real CLI path) with no LOCG_DATA_DIR, record-win must
    refuse rather than silently write wins into whatever
    locg.config._cache_dir() resolves to."""
    from locg.commands import cmd_collection_record_win

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)

    result = cmd_collection_record_win([_make_win()])

    assert result["status"] == "explicit_store_required"
    assert "LOCG_DATA_DIR" in result["error"]


def test_record_win_explicit_cache_bypasses_the_locg_data_dir_guard(tmp_path, monkeypatch):
    """An explicitly-passed cache= is the caller naming its store, so the env
    guard must not fire — this is the shape every in-process caller uses."""
    from locg.commands import cmd_collection_record_win

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    cache = make_cache(tmp_path)
    _no_default_store(monkeypatch)

    result = cmd_collection_record_win(
        [_make_win()], cache=cache, metron=_null_metron()
    )

    assert "status" not in result
    assert result["rows_written"] == 1


def test_record_win_blank_locg_data_dir_is_refused(monkeypatch):
    """A whitespace-only LOCG_DATA_DIR (`export LOCG_DATA_DIR="$DIR "` with an
    unset DIR) resolves to nothing useful — treat it as unset, matching
    config._cache_dir's own blank handling."""
    from locg.commands import cmd_collection_record_win

    monkeypatch.setenv("LOCG_DATA_DIR", "   ")
    _no_default_store(monkeypatch)

    result = cmd_collection_record_win([_make_win()])

    assert result["status"] == "explicit_store_required"


def test_record_win_guard_refuses_before_spending_any_metron_call(monkeypatch):
    """The refusal must come before resolution starts, not after a batch of
    Metron traffic has already been paid for."""
    from locg.commands import cmd_collection_record_win

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)
    metron = _null_metron()

    result = cmd_collection_record_win([_make_win()], metron=metron)

    assert result["status"] == "explicit_store_required"
    metron.lookup_issue.assert_not_called()


def test_record_win_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    """The SERVER-shaped call: no cache= is passed, so the store is resolved
    SOLELY from LOCG_DATA_DIR — exactly what routes._ensure_collection_store()
    guarantees before every record-win. The guard must NOT fire; a guard that
    500s /api/comics/collection/record-win/commit is worse than the bug it
    fixes.

    Points LOCG_DATA_DIR at a dir the autouse fixture did NOT pick, so the
    write landing there proves the env var is what resolved the store.
    """
    from locg.commands import cmd_collection_record_win

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))

    result = cmd_collection_record_win([_make_win()], metron=_null_metron())

    assert "status" not in result
    assert result["rows_written"] == 1
    assert (store / "collection.json").exists()


def test_import_no_explicit_cache_and_no_locg_data_dir_is_refused(monkeypatch):
    """import rewrites the WHOLE collection, so the wrong-store outcome is the
    most destructive one in the module — refuse rather than guess."""
    import locg.commands as cmds

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)

    result = cmds.cmd_collection_import(str(SAMPLE_XLSX))

    assert result["status"] == "explicit_store_required"
    assert "LOCG_DATA_DIR" in result["error"]


def test_import_guard_does_not_write_to_the_default_store(tmp_path, monkeypatch):
    """The point of the refusal: the store it would otherwise have resolved to
    is left untouched."""
    import locg.commands as cmds

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    would_be_store = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: would_be_store)

    result = cmds.cmd_collection_import(str(SAMPLE_XLSX))

    assert result["status"] == "explicit_store_required"
    assert not (tmp_path / "collection.json").exists()


def test_import_explicit_cache_bypasses_the_locg_data_dir_guard(tmp_path, monkeypatch):
    """cache= is the caller naming its store; the env guard must not fire."""
    import locg.commands as cmds

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    cache = make_cache(tmp_path)
    _no_default_store(monkeypatch)

    result = cmds.cmd_collection_import(str(SAMPLE_XLSX), cache=cache)

    assert result.get("status") != "explicit_store_required"
    assert result["added"] > 0


def test_import_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    """The SERVER-shaped call: routes._ensure_collection_store() sets
    LOCG_DATA_DIR and passes no cache=, so /api/comics/collection/import must
    keep working. Uses a dir the autouse fixture did not pick, so the written
    collection.json proves the env var resolved the store."""
    import locg.commands as cmds

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))

    result = cmds.cmd_collection_import(str(SAMPLE_XLSX))

    assert result.get("status") != "explicit_store_required"
    assert result["added"] > 0
    assert (store / "collection.json").exists()


def test_import_bad_path_still_raises_regardless_of_store_guard(tmp_path, monkeypatch):
    """Argument validation is checked first: a nonexistent path is a caller
    error true of every store, so it must not be masked by the store refusal."""
    import locg.commands as cmds

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)

    with pytest.raises(FileNotFoundError):
        cmds.cmd_collection_import(str(tmp_path / "does_not_exist.xlsx"))


def test_read_only_collection_commands_are_not_guarded(tmp_path, monkeypatch):
    """BUI-476 scope boundary: only the MUTATING commands are guarded. status
    (and the other read paths) must keep working bare so legitimate local-store
    CLI use is not broken — that scope was explicitly declined."""
    import locg.commands as cmds

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))

    result = cmds.cmd_collection_status()

    assert result.get("status") != "explicit_store_required"


@pytest.mark.parametrize(
    "argv",
    [
        ["locg", "collection", "import", "IGNORED"],
        ["locg", "collection", "record-win", "--from-gixen-json", "IGNORED"],
    ],
)
def test_cli_exits_nonzero_on_the_store_refusal(argv, tmp_path, monkeypatch, capsys):
    """The refusal must exit NON-ZERO. A caller that chains (`... && next`,
    `set -e`, a skill branching on $?) reads exit 0 as "it worked" — and the
    refusal recorded nothing, which is exactly the false-success the guard
    exists to prevent. Backfill already exits 1; import/record-win must match.
    """
    import locg.cli as cli

    payload = tmp_path / "payload.json"
    payload.write_text("[]")
    argv = [a if a != "IGNORED" else str(payload) for a in argv]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "explicit_store_required" in capsys.readouterr().out


@pytest.mark.parametrize(
    "invocation",
    [
        "locg collection import <path>",
        "locg collection record-win --from-gixen-json <path>",
        "locg collection backfill [--apply]",
        # BUI-489
        "locg collection remediate-delete --gixen-item-id <id>",
        "locg collection remediate-set-copies --gixen-item-id <id> --in-collection 0",
        "locg wish-list add <title>",
        "locg wish-list remove <title>",
        "locg wish-list set-year <title> <year>",
    ],
)
def test_refusal_suggests_an_invocation_argparse_actually_accepts(invocation):
    """The refusal's whole value is the working command line it hands the
    operator — an invocation argparse rejects (e.g. `record-win <path>`, which
    has no positional) is worse than no suggestion at all.
    """
    from locg.cli import create_parser
    from locg.commands import _explicit_store_required_error

    error = _explicit_store_required_error(invocation)
    assert invocation in error["error"]

    argv = [tok for tok in invocation.split()[1:] if not tok.startswith("[")]
    argv = ["value" if tok.startswith("<") else tok for tok in argv]
    create_parser().parse_args(argv)


def _no_default_wish_list_store(monkeypatch):
    """BUI-489: the wish-list writers don't go through CollectionCache — they
    resolve wish_list_cache_path() directly — so a guard regression needs its
    OWN stub, distinct from `_no_default_store`'s CollectionCache patch."""

    def _boom(*args, **kwargs):
        raise AssertionError("BUI-489 guard regressed: default wish-list path resolved")

    monkeypatch.setattr("locg.commands.wish_list_cache_path", _boom)


@pytest.mark.parametrize(
    "argv",
    [
        ["locg", "collection", "remediate-delete", "--gixen-item-id", "99"],
        ["locg", "collection", "remediate-set-copies", "--gixen-item-id", "99", "--in-collection", "0"],
        ["locg", "wish-list", "add", "Amazing Spider-Man #300"],
        ["locg", "wish-list", "remove", "Amazing Spider-Man #300"],
        ["locg", "wish-list", "set-year", "The X-Men #1", "1963"],
    ],
)
def test_cli_exits_nonzero_on_the_store_refusal_bui489(argv, monkeypatch, capsys):
    """BUI-489: same non-zero-exit contract `test_cli_exits_nonzero_on_the_
    store_refusal` established for import/record-win, extended to the
    remaining guarded mutators (remediate-delete/set-copies, the wish-list
    writers)."""
    import locg.cli as cli

    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)
    _no_default_wish_list_store(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "explicit_store_required" in capsys.readouterr().out


def test_cli_exits_nonzero_on_export_not_imported(monkeypatch, capsys):
    """BUI-489 Part 2: cmd_collection_export's distinct not-imported signal
    must also exit non-zero — a chained caller (`... && upload-to-locg`,
    `set -e`) reading exit 0 as "exported" is exactly the false-success this
    signal exists to prevent. The autouse _isolate_cache_dir fixture already
    points LOCG_DATA_DIR at a fresh, genuinely-empty per-test tmp_path, so no
    explicit env manipulation is needed to hit the never-touched-store case.
    """
    import locg.cli as cli

    monkeypatch.setattr("sys.argv", ["locg", "collection", "export"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "not_imported" in capsys.readouterr().out


def test_unexpanded_locg_data_dir_is_refused(tmp_path, monkeypatch):
    """A literal, unexpanded `$HOME/...` is NOT a store the caller named.

    The refusal's remediation line is written for a shell, where `$HOME`
    expands. A caller that copies it mechanically — into os.environ, a
    subprocess env dict, or a quoted heredoc — gets the literal string, and
    CollectionCache would happily mkdir a directory named `$HOME` under the cwd
    and report a full-collection import into it as success. That is the exact
    wrong-store write this guard exists to refuse.
    """
    import locg.commands as cmds

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCG_DATA_DIR", "$HOME/.comics-server/collection-store")
    _no_default_store(monkeypatch)

    result = cmds.cmd_collection_import(str(SAMPLE_XLSX))

    assert result["status"] == "explicit_store_required"
    assert not (tmp_path / "$HOME").exists()


def test_cli_refusal_is_not_erased_by_fields_filtering(tmp_path, monkeypatch, capsys):
    """`--fields added,updated` must not filter the refusal down to `{}`.

    The refusal carries only status/error — never a key a caller narrows to —
    so field-filtering it leaves an unexplained exit 1 with no remediation text
    on stdout or stderr, discarding the guard's entire value.
    """
    import locg.cli as cli

    monkeypatch.setattr(
        "sys.argv",
        ["locg", "collection", "import", str(SAMPLE_XLSX), "--fields", "added,updated"],
    )
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_store(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "explicit_store_required" in out
    assert "LOCG_DATA_DIR" in out


def test_doctor_walkthrough_steps_all_name_the_same_store(tmp_path, monkeypatch):
    """The walkthrough must not tell the operator to WRITE to one store and
    then VERIFY another.

    A one-shot `LOCG_DATA_DIR=... locg collection import` prefix scopes to that
    single command, so the following `locg collection status` — deliberately
    unguarded — would resolve the DEFAULT store and report "cache is empty"
    immediately after a successful import, looping the walkthrough forever.
    An `export` is what makes every later step name the same store.
    """
    import locg.commands as cmds

    monkeypatch.setattr(cmds, "CollectionCache", lambda: make_cache(tmp_path))

    steps = cmds.cmd_collection_doctor()["setup_steps"]
    instructions = [s["instruction"] for s in steps]

    assert any(cmds.SERVER_STORE_EXPORT_HINT in i for i in instructions)
    # No step may carry the one-shot prefix form, which is what splits the
    # write store from the read store.
    assert not any(
        f"{cmds.SERVER_STORE_ENV_PREFIX} locg" in i for i in instructions
    )
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
