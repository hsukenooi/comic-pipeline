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
