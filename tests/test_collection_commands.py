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


def test_export_includes_wish_list_items(tmp_path, monkeypatch):
    """Wish-list cache items appear in the CSV with In Collection=0, In Wish List=1."""
    import csv
    import locg.collection_io as cio
    import locg.commands as cmds

    wish_path = cio.wish_list_cache_path()
    wish_path.parent.mkdir(parents=True, exist_ok=True)
    wish_path.write_text(json.dumps({
        "updated_at": "2026-05-22T00:00:00+00:00",
        "items": [
            {
                "name": "Batman #1",
                "id": None,
                "series_name": "Batman (1940 - 2011)",
                "publisher_name": "DC Comics",
                "release_date": "1940-04-25",
                "media_format": "Print",
            }
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
    assert rows[0]["Full Title"] == "Batman #1"
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


def test_check_miss_returns_not_in_cache(tmp_path, monkeypatch):
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed_cache(cache, [_agent_win_row()])

    result = cmds.cmd_collection_check(series="Amazing Spider-Man", issue="999")
    assert result["match_status"] == "not_in_cache"
    assert result["full_title_matched"] is None


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
    def metron_lookup(series_query, issue_number):
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
