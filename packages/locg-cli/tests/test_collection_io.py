"""Tests for the Excel import / reconciliation pipeline (Unit 2)."""
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


def make_agent_win_row(
    publisher: str = "Marvel",
    series: str = "Amazing Spider-Man",
    full_title: str = "Amazing Spider-Man #300",
    release_date: str = "1988-05-10",
    needs_manual_series: bool = False,
    needs_manual_variant: bool = False,
    gixen_item_id: str | None = "42",
    pushed: str | None = None,
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
        "price_paid": None,
        "date_purchased": None,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": None,
        "signature": None,
        "slabbing": None,
        "grading": None,
        "grading_company": None,
        "local_added_at": "2024-01-01T00:00:00.000000Z",
        "local_added_seq": 1,
        "pushed_to_locg_at": pushed,
        "last_seen_in_export_at": None,
        "source": "agent_win",
        "needs_manual_variant": needs_manual_variant,
        "needs_manual_series_canonical": needs_manual_series,
        "metron_id": None,
        "gixen_item_id": gixen_item_id,
        "previous_full_title": None,
    }


# ---------------------------------------------------------------------------
# parse_xlsx
# ---------------------------------------------------------------------------

def test_parse_xlsx_row_count():
    """parse_xlsx returns rows; count == max_row - 1 (minus header)."""
    from locg.collection_io import parse_xlsx
    rows = parse_xlsx(SAMPLE_XLSX)
    assert len(rows) > 0
    # Sample file has 2353 data rows (2354 including header)
    assert len(rows) == 2353


def test_parse_xlsx_row_shape():
    """Every parsed row has all 21 LOCG column keys."""
    from locg.collection_io import parse_xlsx
    from locg.collection_cache import LOCG_COLUMNS
    rows = parse_xlsx(SAMPLE_XLSX)
    for row in rows[:5]:
        for col in LOCG_COLUMNS:
            assert col in row, f"Missing column: {col}"


def test_parse_xlsx_first_row_values():
    """First data row matches known fixture values."""
    from locg.collection_io import parse_xlsx
    rows = parse_xlsx(SAMPLE_XLSX)
    first = rows[0]
    assert first["publisher_name"] == "Image Comics"
    assert first["series_name"] == "1963 (1993)"
    assert first["full_title"] == "1963 #6"
    assert first["in_collection"] == 1


def test_parse_xlsx_header_mismatch_raises(tmp_path):
    """A file with mismatched column headers raises RuntimeError before any row is read."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Publisher", "Wrong Column"])  # bad headers
    bad_path = tmp_path / "bad.xlsx"
    wb.save(bad_path)

    from locg.collection_io import parse_xlsx
    with pytest.raises(RuntimeError, match="header"):
        parse_xlsx(bad_path)


def test_parse_xlsx_file_too_large_rejected(tmp_path):
    """Files larger than 10 MB are rejected before parsing."""
    big_file = tmp_path / "big.xlsx"
    big_file.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    from locg.collection_io import parse_xlsx
    with pytest.raises(RuntimeError, match="10 MB"):
        parse_xlsx(big_file)


# ---------------------------------------------------------------------------
# import_xlsx — Phase 2 standard merge (happy paths)
# ---------------------------------------------------------------------------

def test_import_xlsx_populates_empty_cache(tmp_path):
    """Importing into an empty cache inserts all rows with source='locg_export'."""
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    result = import_xlsx(SAMPLE_XLSX, cache)
    assert result["added"] > 0
    assert result["updated"] == 0
    payload = cache.load()
    assert len(payload["comics"]) == result["added"]
    assert all(r["source"] == "locg_export" for r in payload["comics"])


def test_import_xlsx_sets_pushed_to_locg_at(tmp_path):
    """Rows imported from LOCG have pushed_to_locg_at set (they're already in LOCG)."""
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    payload = cache.load()
    for row in payload["comics"]:
        assert row["pushed_to_locg_at"] is not None


def test_reimport_same_xlsx_unchanged(tmp_path):
    """Re-importing the same xlsx updates last_seen_in_export_at but adds no new rows."""
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    r1 = import_xlsx(SAMPLE_XLSX, cache)
    r2 = import_xlsx(SAMPLE_XLSX, cache)
    assert r2["added"] == 0
    assert r2["updated"] >= 0  # last_seen_in_export_at updated


def test_import_updates_last_full_import(tmp_path):
    """After import, last_full_import is set in the payload."""
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    payload = cache.load()
    assert payload["last_full_import"] is not None
    assert payload["last_import_source"] == str(SAMPLE_XLSX)


def test_import_builds_series_name_index(tmp_path):
    """After import, series_name_index is non-empty."""
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    payload = cache.load()
    assert len(payload["series_name_index"]) > 0


# ---------------------------------------------------------------------------
# import_xlsx — Phase 2: cache-only rows preserved
# ---------------------------------------------------------------------------

def test_cache_only_agent_win_survives_import(tmp_path):
    """agent_win rows not in the xlsx are preserved, not deleted (v1 preserves)."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    win_row = make_agent_win_row(
        publisher="DC",
        series="Batman (1940 - 2011)",
        full_title="Batman #999 (Not in Export)",
        release_date="2999-01-01",
    )

    def add_win(payload):
        payload["comics"].append(win_row)

    cache.apply(add_win, command="pre-import")
    import_xlsx(SAMPLE_XLSX, cache)

    payload = cache.load()
    titles = {r["full_title"] for r in payload["comics"]}
    assert "Batman #999 (Not in Export)" in titles


def test_pushed_not_in_export_possibly_removed_logged(tmp_path):
    """A pushed row not appearing in the re-export logs a 'possibly_removed' audit record."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    # Add a "pushed" agent_win row that won't appear in the xlsx
    ghost_row = make_agent_win_row(
        publisher="Nonexistent",
        series="Ghost Series (2099)",
        full_title="Ghost #1",
        release_date="2099-01-01",
        pushed="2024-01-01T00:00:00Z",
    )

    def add_ghost(payload):
        payload["comics"].append(ghost_row)

    cache.apply(add_ghost, command="pre-import")
    result = import_xlsx(SAMPLE_XLSX, cache)

    # The row must still be in the cache
    payload = cache.load()
    titles = {r["full_title"] for r in payload["comics"]}
    assert "Ghost #1" in titles

    # Audit log must have a possibly_removed record
    audit_lines = (tmp_path / "import-history.jsonl").read_text().strip().splitlines()
    audit_types = [json.loads(l)["type"] for l in audit_lines]
    assert "possibly_removed" in audit_types


# ---------------------------------------------------------------------------
# import_xlsx — Phase 1 reconciliation (R60)
# ---------------------------------------------------------------------------

def test_reconciliation_best_guess_row_resolved(tmp_path):
    """A best-guess agent_win row matched by reconciliation heuristic gets identity rewritten."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    # Add a row that matches what's in the xlsx — use a known series/issue from fixture
    # The fixture has "1963 #6" by Image Comics
    unresolved = make_agent_win_row(
        publisher="Image Comics",
        series="1963",  # bare name, no year — needs reconciliation
        full_title="1963 #6",
        release_date="1993-11-08",
        needs_manual_series=True,
    )
    unresolved["local_added_at"] = "2024-01-02T00:00:00.000000Z"
    unresolved["local_added_seq"] = 1

    def add_unresolved(payload):
        payload["comics"].append(unresolved)

    cache.apply(add_unresolved, command="pre-import")
    import_xlsx(SAMPLE_XLSX, cache)

    payload = cache.load()
    # Find the reconciled row
    matched = [r for r in payload["comics"] if r["full_title"] == "1963 #6"
               and r.get("gixen_item_id") == "42"]
    assert len(matched) == 1, "Reconciled row not found (expected exactly one)"
    assert matched[0]["needs_manual_series_canonical"] is False
    assert matched[0]["series_name"] == "1963 (1993)"  # LOCG canonical name
    # Tracking fields preserved
    assert matched[0]["gixen_item_id"] == "42"
    assert matched[0]["local_added_at"] == "2024-01-02T00:00:00.000000Z"


def test_reconciliation_vol_mismatch_not_reconciled(tmp_path):
    """A row with mismatching (Vol. N) annotation is NOT reconciled per R60."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    # Force a Vol mismatch: cache says "Amazing Spider-Man (Vol. 2)" but xlsx has Vol. 1
    bad_match = make_agent_win_row(
        publisher="Marvel Comics",
        series="Amazing Spider-Man (Vol. 2)",  # wrong vol
        full_title="Amazing Spider-Man #300",
        release_date="1988-05-10",
        needs_manual_series=True,
    )

    def add_row(payload):
        payload["comics"].append(bad_match)

    cache.apply(add_row, command="pre-import")
    import_xlsx(SAMPLE_XLSX, cache)

    payload = cache.load()
    remaining_manual = [
        r for r in payload["comics"]
        if r.get("gixen_item_id") == "42"
        and r.get("needs_manual_series_canonical") is True
    ]
    assert len(remaining_manual) == 1, "Row should still be flagged needs_manual_series_canonical"


# ---------------------------------------------------------------------------
# import_xlsx — BUI-122: year-tolerant reconciliation for unflagged pending
# agent_win rows (LOCG canonicalizes Release Date on re-export, breaking the
# Phase-2 exact identity match). Uses a deterministic in-test XLSX rather than
# the golden fixture so the date-shift / ambiguity scenarios are exact.
# ---------------------------------------------------------------------------

def _build_export_xlsx(path: Path, rows: list[dict[str, Any]]):
    """Build a 21-column LOCG-format XLSX from row dicts.

    Each row dict needs publisher/series/full_title/release_date; in_collection
    defaults to 1, in_wish_list to 0.
    """
    import openpyxl
    from locg.collection_io import LOCG_XLSX_HEADERS

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(LOCG_XLSX_HEADERS))
    for r in rows:
        ws.append([
            r["publisher"], r["series"], r["full_title"], r["release_date"],
            r.get("in_collection", 1), r.get("in_wish_list", 0), 0, None,
            "Print", None, None, None, None, None, None, None, None,
            None, None, None, None,
        ])
    wb.save(path)


def test_pending_agent_win_reconciled_on_within_year_date_shift(tmp_path):
    """BUI-122: an unflagged pending agent_win row whose export counterpart has
    the same year but a different Release Date reconciles instead of duplicating."""
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    win = make_agent_win_row(
        publisher="Marvel Comics",
        series="Amazing Spider-Man",
        full_title="Amazing Spider-Man #309",
        release_date="1988-01-01",  # agent stamped Jan 1
        needs_manual_series=False,  # NOT flagged — the new path
        pushed=None,                # pending
    )

    def add_win(payload):
        payload["comics"].append(win)

    cache.apply(add_win, command="pre-import")

    xlsx = tmp_path / "reexport.xlsx"
    _build_export_xlsx(xlsx, [{
        "publisher": "Marvel Comics",
        "series": "Amazing Spider-Man",
        "full_title": "Amazing Spider-Man #309",
        "release_date": "1988-02-11",  # LOCG canonicalized the date, same year
    }])

    result = import_xlsx(xlsx, cache)
    payload = cache.load()

    rows = [r for r in payload["comics"] if r["full_title"] == "Amazing Spider-Man #309"]
    assert len(rows) == 1, "must reconcile in place, not insert a duplicate"
    row = rows[0]
    assert row["pushed_to_locg_at"] is not None, "pending must clear"
    assert row["source"] == "locg_export"
    assert row["release_date"] == "1988-02-11", "identity rewritten to LOCG canonical"
    assert row["gixen_item_id"] == "42", "tracking field preserved"
    assert result["reconciled"] >= 1
    assert result["added"] == 0, "no new row inserted"


def test_pending_agent_win_with_null_publisher_reconciles(tmp_path):
    """BUI-122 production-faithful: agent_win rows are written with
    publisher_name=None (record-win has no publisher), while LOCG's export
    carries a canonical publisher. The row must still reconcile — a strict
    publisher compare would score it 0 and strand it pending forever."""
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    win = make_agent_win_row(
        publisher=None,  # as written by record-win in production
        series="Daredevil",
        full_title="Daredevil #181",
        release_date="1982-01-01",  # agent-stamped Jan 1
        needs_manual_series=False,
        pushed=None,
    )

    def add_win(payload):
        payload["comics"].append(win)

    cache.apply(add_win, command="pre-import")

    xlsx = tmp_path / "reexport.xlsx"
    _build_export_xlsx(xlsx, [{
        "publisher": "Marvel Comics",      # LOCG populates the publisher
        "series": "Daredevil",
        "full_title": "Daredevil #181",
        "release_date": "1982-04-10",      # ...and canonicalizes the date (same year)
    }])

    result = import_xlsx(xlsx, cache)
    payload = cache.load()

    rows = [r for r in payload["comics"] if r["full_title"] == "Daredevil #181"]
    assert len(rows) == 1, "null-publisher win must reconcile, not duplicate"
    row = rows[0]
    assert row["pushed_to_locg_at"] is not None, "pending must clear"
    assert row["publisher_name"] == "Marvel Comics", "identity adopts LOCG canonical publisher"
    assert result["reconciled"] >= 1
    assert result["added"] == 0


def test_pending_agent_win_exact_match_uses_phase2_not_reconciliation(tmp_path):
    """KTD-2: a pending agent_win row whose EXACT identity is in the export is
    handled by the Phase-2 standard merge, not routed through year-tolerant
    reconciliation — so a same-year variant in the export can't make it ambiguous."""
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    win = make_agent_win_row(
        publisher="Marvel Comics",
        series="Amazing Spider-Man",
        full_title="Amazing Spider-Man #300",
        release_date="1988-05-10",
        needs_manual_series=False,
        pushed=None,
    )

    def add_win(payload):
        payload["comics"].append(win)

    cache.apply(add_win, command="pre-import")

    xlsx = tmp_path / "reexport.xlsx"
    _build_export_xlsx(xlsx, [
        # Exact identity match for the win:
        {"publisher": "Marvel Comics", "series": "Amazing Spider-Man",
         "full_title": "Amazing Spider-Man #300", "release_date": "1988-05-10"},
        # A same-year variant that WOULD make reconciliation ambiguous if the
        # win were routed through it:
        {"publisher": "Marvel Comics", "series": "Amazing Spider-Man",
         "full_title": "Amazing Spider-Man #300 Newsstand", "release_date": "1988-05-10"},
    ])

    result = import_xlsx(xlsx, cache)
    payload = cache.load()

    win_rows = [r for r in payload["comics"]
                if r["full_title"] == "Amazing Spider-Man #300" and r.get("gixen_item_id") == "42"]
    assert len(win_rows) == 1
    row = win_rows[0]
    assert row["pushed_to_locg_at"] is not None, "exact match must clear pending via Phase 2"
    assert row["source"] == "locg_export"
    assert row["needs_manual_series_canonical"] is False
    assert result["updated"] >= 1, "matched via standard merge, not reconciliation"
    assert not result["warnings"], "exact-match primacy must avoid an ambiguity warning"


def test_pending_agent_win_collision_with_owned_row_left_pending(tmp_path):
    """A pending agent_win win for a book already owned under LOCG's canonical
    identity must NOT reconcile onto that existing row (which would create a
    duplicate-identity pair). It is left pending and surfaced. This is the
    pre-existing duplicate-records condition, resolved out-of-band."""
    from locg.collection_io import import_xlsx, make_identity

    cache = make_cache(tmp_path)
    # Already-owned canonical row (from a prior import).
    owned = make_agent_win_row(
        publisher="Marvel Comics",
        series="Daredevil",
        full_title="Daredevil #181",
        release_date="1982-04-10",
        gixen_item_id=None,
        pushed="2024-01-01T00:00:00.000000Z",
    )
    owned["source"] = "locg_export"
    # A pending win for the SAME book, recorded with no publisher + fabricated date.
    win = make_agent_win_row(
        publisher=None, series="Daredevil", full_title="Daredevil #181",
        release_date="1982-01-01", gixen_item_id="99", pushed=None,
    )

    def add_rows(payload):
        payload["comics"].extend([owned, win])

    cache.apply(add_rows, command="pre-import")

    xlsx = tmp_path / "reexport.xlsx"
    # LOCG carries ONE canonical Daredevil #181 (it collapses the win into the
    # owned copy on its side).
    _build_export_xlsx(xlsx, [{
        "publisher": "Marvel Comics", "series": "Daredevil",
        "full_title": "Daredevil #181", "release_date": "1982-04-10",
    }])

    result = import_xlsx(xlsx, cache)
    payload = cache.load()

    dd = [r for r in payload["comics"] if r["full_title"] == "Daredevil #181"]
    win_row = next(r for r in dd if r.get("gixen_item_id") == "99")
    assert win_row["pushed_to_locg_at"] is None, "win must stay pending, not merge onto owned row"
    # No duplicate-identity pair created.
    idents = [make_identity(r) for r in payload["comics"]]
    assert len(idents) == len(set(idents)), "no duplicate-identity rows"
    assert result["reconciled"] == 0
    assert result["warnings"], "collision must be surfaced as a warning"


def test_pending_agent_win_cross_year_not_reconciled(tmp_path):
    """A different YEAR (volume reboot) is not tolerated: the win stays pending and
    the export row inserts as a genuinely new row."""
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    win = make_agent_win_row(
        publisher="DC Comics",
        series="Action Comics Annual",
        full_title="Action Comics Annual #1",
        release_date="1987-01-01",
        needs_manual_series=False,
        pushed=None,
    )

    def add_win(payload):
        payload["comics"].append(win)

    cache.apply(add_win, command="pre-import")

    xlsx = tmp_path / "reexport.xlsx"
    _build_export_xlsx(xlsx, [{
        "publisher": "DC Comics", "series": "Action Comics Annual",
        "full_title": "Action Comics Annual #1", "release_date": "2012-06-01",  # 2012 reboot
    }])

    result = import_xlsx(xlsx, cache)
    payload = cache.load()

    rows = sorted(
        (r for r in payload["comics"] if r["full_title"] == "Action Comics Annual #1"),
        key=lambda r: r["release_date"],
    )
    assert len(rows) == 2, "different-year rows are distinct, not reconciled"
    original = next(r for r in rows if r.get("gixen_item_id") == "42")
    assert original["pushed_to_locg_at"] is None, "1987 win stays pending"
    assert result["added"] == 1, "2012 export row inserts as new"
    assert result["reconciled"] == 0


def test_pending_agent_win_ambiguous_left_pending(tmp_path):
    """When a pending agent_win row (no exact match) year-matches multiple export
    rows, it is left pending with an ambiguous_reconciliation audit/warning rather
    than guessing (duplicate-row policy: visible non-clear over silent wrong merge)."""
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    win = make_agent_win_row(
        publisher="Image Comics",
        series="Spawn",
        full_title="Spawn #300",
        release_date="2019-01-01",  # agent date; export will not carry this exact date
        needs_manual_series=False,
        pushed=None,
    )

    def add_win(payload):
        payload["comics"].append(win)

    cache.apply(add_win, command="pre-import")

    # Two printings: same publisher/series/issue-token/year, different dates.
    # Both score against the win (issue "300" + year 2019), so reconciliation is
    # ambiguous. (A variant whose Full Title doesn't end in "#300" wouldn't score,
    # since _reconcile_score requires an exact issue-token match.)
    xlsx = tmp_path / "reexport.xlsx"
    _build_export_xlsx(xlsx, [
        {"publisher": "Image Comics", "series": "Spawn",
         "full_title": "Spawn #300", "release_date": "2019-08-28"},
        {"publisher": "Image Comics", "series": "Spawn",
         "full_title": "Spawn #300", "release_date": "2019-11-13"},
    ])

    result = import_xlsx(xlsx, cache)
    payload = cache.load()

    win_rows = [r for r in payload["comics"] if r.get("gixen_item_id") == "42"]
    assert len(win_rows) == 1
    assert win_rows[0]["pushed_to_locg_at"] is None, "ambiguous match stays pending"
    assert result["reconciled"] == 0
    assert result["warnings"], "an ambiguity warning must be surfaced"
    audit_types = [
        json.loads(line)["type"]
        for line in (tmp_path / "import-history.jsonl").read_text().strip().splitlines()
    ]
    assert "ambiguous_reconciliation" in audit_types


# ---------------------------------------------------------------------------
# import_xlsx — renamed full_title persistence (R67)
# ---------------------------------------------------------------------------

def test_renamed_full_title_persists_previous(tmp_path):
    """When LOCG renames a full_title, previous_full_title is set for one cycle."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    # Seed cache with a row using the "old" title
    old_title = "1963 #6 (Old Name)"
    old_row = {
        "publisher_name": "Image Comics",
        "series_name": "1963 (1993)",
        "full_title": old_title,
        "release_date": "1993-11-08",
        "in_collection": 1,
        "in_wish_list": 0,
        "marked_read": 0,
        "my_rating": None,
        "media_format": "Print",
        "price_paid": None,
        "date_purchased": None,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": None,
        "signature": None,
        "slabbing": None,
        "grading": None,
        "grading_company": None,
        "local_added_at": "2024-01-01T00:00:00.000000Z",
        "local_added_seq": 1,
        "pushed_to_locg_at": "2024-01-01T00:00:00Z",
        "last_seen_in_export_at": None,
        "source": "locg_export",
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": None,
        "gixen_item_id": None,
        "previous_full_title": None,
    }

    def seed(payload):
        payload["comics"].append(old_row)

    cache.apply(seed, command="seed")
    import_xlsx(SAMPLE_XLSX, cache)

    payload = cache.load()
    # The import should have matched by (publisher, series, release_date) and updated title
    renamed = [r for r in payload["comics"]
               if r.get("previous_full_title") == old_title]
    assert len(renamed) == 1, "Expected exactly one row with previous_full_title set"
    assert renamed[0]["full_title"] == "1963 #6"  # LOCG's canonical title


# ---------------------------------------------------------------------------
# import_xlsx — behavioral drift detection (F5)
# ---------------------------------------------------------------------------

def test_behavioral_drift_detected(tmp_path):
    """A changed user-managed column logs a behavioral_drift audit record."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    # Seed with a row that has a non-null my_rating that the import will clear
    row_with_rating = {
        "publisher_name": "Image Comics",
        "series_name": "1963 (1993)",
        "full_title": "1963 #6",
        "release_date": "1993-11-08",
        "in_collection": 1,
        "in_wish_list": 0,
        "marked_read": 0,
        "my_rating": 9.0,  # user set this; xlsx returns None → drift
        "media_format": "Print",
        "price_paid": None,
        "date_purchased": None,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": None,
        "signature": None,
        "slabbing": None,
        "grading": None,
        "grading_company": None,
        "local_added_at": "2024-01-01T00:00:00.000000Z",
        "local_added_seq": 1,
        "pushed_to_locg_at": "2024-01-01T00:00:00Z",
        "last_seen_in_export_at": None,
        "source": "locg_export",
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": None,
        "gixen_item_id": None,
        "previous_full_title": None,
    }

    def seed(payload):
        payload["comics"].append(row_with_rating)

    cache.apply(seed, command="seed")
    import_xlsx(SAMPLE_XLSX, cache)

    audit_lines = (tmp_path / "import-history.jsonl").read_text().strip().splitlines()
    audit_types = [json.loads(l)["type"] for l in audit_lines]
    assert "behavioral_drift" in audit_types


# ---------------------------------------------------------------------------
# import_xlsx — series_name_index rebuilt from locg_export only
# ---------------------------------------------------------------------------

def test_series_name_index_after_import(tmp_path):
    """series_name_index is rebuilt from locg_export rows only after import."""
    from locg.collection_cache import CollectionCache
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    # Add an agent_win row — its series must NOT appear in the index
    win = make_agent_win_row(series="Totally Made Up Series (2099)")

    def add_win(payload):
        payload["comics"].append(win)

    cache.apply(add_win, command="pre-import")
    import_xlsx(SAMPLE_XLSX, cache)

    payload = cache.load()
    index = payload["series_name_index"]
    assert not any("totally made up" in k.lower() for k in index)
    # But fixture series should be present
    assert any("1963" in k for k in index)


# ---------------------------------------------------------------------------
# import_xlsx — crash recovery: migration_in_progress stays False
# ---------------------------------------------------------------------------

def test_import_never_leaves_migration_flag(tmp_path):
    """After a successful import, migration_in_progress must be False."""
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    payload = cache.load()
    assert payload["migration_in_progress"] is False


# ---------------------------------------------------------------------------
# import_xlsx — error paths
# ---------------------------------------------------------------------------

def test_import_nonexistent_file_raises(tmp_path):
    from locg.collection_io import import_xlsx
    cache = make_cache(tmp_path)
    with pytest.raises((RuntimeError, FileNotFoundError, OSError)):
        import_xlsx(tmp_path / "does_not_exist.xlsx", cache)


def test_import_bad_header_raises_before_merge(tmp_path):
    """Header mismatch must raise before any cache mutation."""
    import openpyxl
    from locg.collection_io import import_xlsx

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Wrong", "Headers", "Here"])
    bad_path = tmp_path / "bad_headers.xlsx"
    wb.save(bad_path)

    cache = make_cache(tmp_path)
    with pytest.raises(RuntimeError, match="header"):
        import_xlsx(bad_path, cache)

    # Cache should be untouched — no file created
    assert not (tmp_path / "collection.json").exists()


# ---------------------------------------------------------------------------
# Unit 3: generate_csv
# ---------------------------------------------------------------------------

RECIPE_CSV = FIXTURES / "locg_import_test_recipe.csv"


def _make_ready_row(
    publisher: str = "Marvel Comics",
    series: str = "The Amazing Spider-Man (Vol. 1) (1962 - 1998)",
    full_title: str = "The Amazing Spider-Man #84",
    release_date: str = "1970-05-01",
    price_paid: Any = 27.86,
    date_purchased: Any = "2026-05-22",
) -> dict[str, Any]:
    """Build a minimal agent_win row suitable for CSV export."""
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
        "pushed_to_locg_at": None,
        "last_seen_in_export_at": None,
        "source": "agent_win",
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": None,
        "gixen_item_id": "12345",
        "previous_full_title": None,
    }


def test_generate_csv_row_count(tmp_path):
    """10 ready rows produce a 10-row CSV (plus header)."""
    import csv
    from locg.collection_io import generate_csv
    rows = [_make_ready_row(full_title=f"ASM #{i}") for i in range(10)]
    out = tmp_path / "out.csv"
    generate_csv(rows, out)
    with open(out, newline="") as f:
        reader = list(csv.reader(f))
    assert len(reader) == 11  # 1 header + 10 data rows


def test_generate_csv_header_order(tmp_path):
    """CSV header matches the canonical 21-column LOCG order."""
    import csv
    from locg.collection_io import LOCG_XLSX_HEADERS, generate_csv
    generate_csv([_make_ready_row()], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        header = next(csv.reader(f))
    assert tuple(header) == LOCG_XLSX_HEADERS


def test_generate_csv_my_rating_blank_in_body(tmp_path):
    """My Rating column is present in header AND body as an empty string (R27)."""
    from locg.collection_io import generate_csv
    generate_csv([_make_ready_row()], tmp_path / "out.csv")
    raw = (tmp_path / "out.csv").read_text()
    lines = raw.splitlines()
    # Header must contain My Rating
    assert "My Rating" in lines[0]
    # Body row: the My Rating field position must be empty
    import csv, io
    reader = list(csv.reader(io.StringIO(raw)))
    header = reader[0]
    my_rating_idx = header.index("My Rating")
    body_row = reader[1]
    assert body_row[my_rating_idx] == ""


def test_generate_csv_omits_placeholder_release_date(tmp_path):
    """BUI-199 Cause 2: a placeholder-dated (YYYY-01-01) agent_win row exports
    with a BLANK Release Date, while a real-dated row keeps its date."""
    import csv, io
    from locg.collection_io import generate_csv

    placeholder = _make_ready_row(
        full_title="Placeholder Book #1", release_date="1988-01-01"
    )
    real = _make_ready_row(
        full_title="Real Book #1", release_date="1988-05-10"
    )
    out = tmp_path / "out.csv"
    generate_csv([placeholder, real], out)

    reader = list(csv.reader(io.StringIO(out.read_text())))
    header = reader[0]
    rd_idx = header.index("Release Date")
    ft_idx = header.index("Full Title")
    by_title = {row[ft_idx]: row for row in reader[1:]}

    assert by_title["Placeholder Book #1"][rd_idx] == ""
    assert by_title["Real Book #1"][rd_idx] == "1988-05-10"


def test_generate_csv_keeps_real_metron_jan1_date(tmp_path):
    """BUI-199 finding 5: a real Metron-sourced YYYY-01-01 cover_date (metron_id
    set) is KEPT on export; only a metron_id-less placeholder is blanked."""
    import csv, io
    from locg.collection_io import generate_csv

    metron_jan = _make_ready_row(
        full_title="Metron Jan Book #1", release_date="1988-01-01"
    )
    metron_jan["metron_id"] = 12345  # real Metron-backed date

    placeholder = _make_ready_row(
        full_title="Placeholder Book #1", release_date="1988-01-01"
    )
    placeholder["metron_id"] = None  # BUI-105 placeholder

    out = tmp_path / "out.csv"
    generate_csv([metron_jan, placeholder], out)

    reader = list(csv.reader(io.StringIO(out.read_text())))
    header = reader[0]
    rd_idx = header.index("Release Date")
    ft_idx = header.index("Full Title")
    by_title = {row[ft_idx]: row for row in reader[1:]}

    assert by_title["Metron Jan Book #1"][rd_idx] == "1988-01-01"
    assert by_title["Placeholder Book #1"][rd_idx] == ""


def test_generate_csv_keeps_placeholder_date_for_non_agent_win(tmp_path):
    """The placeholder-date omission is scoped to agent_win rows only — a
    locg_export row with a Jan-1 date keeps it (it is LOCG's real date)."""
    import csv, io
    from locg.collection_io import generate_csv

    row = _make_ready_row(full_title="Export Book #1", release_date="1988-01-01")
    row["source"] = "locg_export"
    out = tmp_path / "out.csv"
    generate_csv([row], out)

    reader = list(csv.reader(io.StringIO(out.read_text())))
    header = reader[0]
    rd_idx = header.index("Release Date")
    assert reader[1][rd_idx] == "1988-01-01"


def test_generate_csv_empty_queue_header_only(tmp_path):
    """Zero ready rows produces a CSV with only the header line."""
    import csv
    from locg.collection_io import generate_csv
    generate_csv([], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 1  # header only


def test_generate_csv_price_format(tmp_path):
    """Price Paid is formatted as NN.NN with no currency suffix."""
    import csv
    from locg.collection_io import generate_csv
    generate_csv([_make_ready_row(price_paid=27.8600001)], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    price_idx = header.index("Price Paid")
    assert rows[1][price_idx] == "27.86"


def test_generate_csv_negative_price_defaults_to_zero(tmp_path):
    """Negative price_paid defaults to 0.00 (R29)."""
    import csv
    from locg.collection_io import generate_csv
    generate_csv([_make_ready_row(price_paid=-5.0)], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        rows = list(csv.reader(f))
    price_idx = rows[0].index("Price Paid")
    assert rows[1][price_idx] == "0.00"


def test_generate_csv_missing_price_defaults_to_zero(tmp_path):
    """None price_paid defaults to 0.00."""
    import csv
    from locg.collection_io import generate_csv
    generate_csv([_make_ready_row(price_paid=None)], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        rows = list(csv.reader(f))
    price_idx = rows[0].index("Price Paid")
    assert rows[1][price_idx] == "0.00"


def test_generate_csv_date_iso_format(tmp_path):
    """Date Purchased is output as ISO date (YYYY-MM-DD) (R30)."""
    import csv
    from locg.collection_io import generate_csv
    generate_csv([_make_ready_row(date_purchased="2026-05-22")], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        rows = list(csv.reader(f))
    date_idx = rows[0].index("Date Purchased")
    assert rows[1][date_idx] == "2026-05-22"


def test_generate_csv_fixed_fields(tmp_path):
    """In Collection=1, In Wish List=0, Marked Read=0, Media Format=Print, Purchase Store=eBay."""
    import csv
    from locg.collection_io import generate_csv
    generate_csv([_make_ready_row()], tmp_path / "out.csv")
    with open(tmp_path / "out.csv", newline="") as f:
        rows = list(csv.reader(f))
    h, d = rows[0], rows[1]
    assert d[h.index("In Collection")] == "1"
    assert d[h.index("In Wish List")] == "0"
    assert d[h.index("Marked Read")] == "0"
    assert d[h.index("Media Format")] == "Print"
    assert d[h.index("Purchase Store")] == "eBay"
    assert d[h.index("Signature")] == "0"
    assert d[h.index("Slabbing")] == "0"


def test_generate_csv_bitforbit_recipe(tmp_path):
    """CSV output for the validated golden fixture rows matches the recipe bit-for-bit."""
    import csv as _csv
    from locg.collection_io import generate_csv

    # Build the same rows as in locg_import_test_recipe.csv
    with open(RECIPE_CSV, newline="") as f:
        reader = _csv.DictReader(f)
        recipe_rows = list(reader)

    # Reconstruct cache rows from the recipe CSV
    cache_rows = []
    for r in recipe_rows:
        cache_rows.append({
            "publisher_name": r["Publisher Name"],
            "series_name": r["Series Name"],
            "full_title": r["Full Title"],
            "release_date": r["Release Date"],
            "price_paid": float(r["Price Paid"]) if r["Price Paid"] else None,
            "date_purchased": r["Date Purchased"] or None,
            "needs_manual_variant": False,
            "needs_manual_series_canonical": False,
            # All other fields not needed for CSV output
        })

    out = tmp_path / "test_out.csv"
    generate_csv(cache_rows, out)

    generated = out.read_text()
    expected = RECIPE_CSV.read_text()
    assert generated == expected


# ---------------------------------------------------------------------------
# Unit 3: generate_notes_md
# ---------------------------------------------------------------------------

def test_notes_md_ready_count(tmp_path):
    """notes.md correctly counts ready rows."""
    from locg.collection_io import generate_notes_md
    ready = [_make_ready_row() for _ in range(5)]
    out = tmp_path / "out.notes.md"
    generate_notes_md(ready, [], [], out)
    text = out.read_text()
    assert "Ready to upload (5 rows)" in text


def test_notes_md_empty_queue(tmp_path):
    """Zero pending rows produces notes.md noting empty queue."""
    from locg.collection_io import generate_notes_md
    out = tmp_path / "out.notes.md"
    generate_notes_md([], [], [], out)
    text = out.read_text()
    assert "Ready to upload (0 rows)" in text


def test_notes_md_manual_variant_section(tmp_path):
    """Variant rows appear in the variants section, not ready section."""
    from locg.collection_io import generate_notes_md
    variant_row = _make_ready_row(full_title="ASM #300 Newsstand")
    variant_row["needs_manual_variant"] = True
    out = tmp_path / "out.notes.md"
    generate_notes_md([], [variant_row], [], out)
    text = out.read_text()
    assert "Needs manual handling — variants (1 rows)" in text
    assert "ASM #300 Newsstand" in text


def test_notes_md_manual_series_section(tmp_path):
    """Series-canonical rows appear in the series canonical section."""
    from locg.collection_io import generate_notes_md
    series_row = _make_ready_row(series="Unknown Series")
    series_row["needs_manual_series_canonical"] = True
    out = tmp_path / "out.notes.md"
    generate_notes_md([], [], [series_row], out)
    text = out.read_text()
    assert "Needs manual handling — series canonical (1 rows)" in text
    assert "Unknown Series" in text


# ---------------------------------------------------------------------------
# Unit 3: _pending_push_rows
# ---------------------------------------------------------------------------

def test_pending_push_rows_partitions(tmp_path):
    """_pending_push_rows correctly partitions ready / manual_variant / manual_series."""
    from locg.collection_io import _pending_push_rows

    r = _make_ready_row()
    v = _make_ready_row(full_title="ASM #300 Newsstand")
    v["needs_manual_variant"] = True
    s = _make_ready_row(series="Unknown")
    s["needs_manual_series_canonical"] = True
    already_pushed = _make_ready_row(full_title="Pushed #1")
    already_pushed["pushed_to_locg_at"] = "2030-01-01T00:00:00Z"  # future; not pending
    already_pushed["local_added_at"] = "2026-01-01T00:00:00Z"

    payload = {"comics": [r, v, s, already_pushed]}
    ready, mv, ms = _pending_push_rows(payload)
    assert len(ready) == 1
    assert len(mv) == 1
    assert len(ms) == 1
    assert ready[0]["full_title"] == _make_ready_row()["full_title"]


def test_pending_push_already_pushed_excluded(tmp_path):
    """Rows where local_added_at <= pushed_to_locg_at are not pending."""
    from locg.collection_io import _pending_push_rows

    row = _make_ready_row()
    row["pushed_to_locg_at"] = "2030-01-01T00:00:00Z"  # pushed far in the future
    row["local_added_at"] = "2026-01-01T00:00:00Z"  # added before push timestamp

    ready, mv, ms = _pending_push_rows({"comics": [row]})
    assert len(ready) == 0
    assert len(mv) == 0
    assert len(ms) == 0


# ---------------------------------------------------------------------------
# import_xlsx — wish-list cache
# ---------------------------------------------------------------------------

def test_import_xlsx_writes_wish_list_cache(tmp_path, monkeypatch):
    """import_xlsx writes wish-list.json alongside collection.json."""
    from locg.collection_io import import_xlsx, wish_list_cache_path
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    wl_path = wish_list_cache_path()
    assert wl_path.exists()
    data = json.loads(wl_path.read_text())
    assert "updated_at" in data
    assert "items" in data


def test_wish_list_cache_item_count(tmp_path):
    """wish-list.json contains exactly the rows where in_wish_list == 1."""
    from locg.collection_io import import_xlsx, wish_list_cache_path
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    payload = cache.load()
    expected = sum(1 for r in payload["comics"] if r.get("in_wish_list") == 1)
    data = json.loads(wish_list_cache_path().read_text())
    assert len(data["items"]) == expected
    assert expected > 0  # fixture has wish-list rows


def test_wish_list_cache_item_shape(tmp_path):
    """Each wish-list cache entry has name (from full_title) and id: null."""
    from locg.collection_io import import_xlsx, wish_list_cache_path
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    payload = cache.load()
    xl_wish = {r["full_title"] for r in payload["comics"] if r.get("in_wish_list") == 1}
    data = json.loads(wish_list_cache_path().read_text())
    for item in data["items"]:
        assert item["name"] in xl_wish
        assert item["id"] is None


def test_wish_list_cache_updated_at_is_iso_timestamp(tmp_path):
    """wish-list.json envelope contains a valid ISO 8601 UTC timestamp."""
    from datetime import datetime, timezone
    from locg.collection_io import import_xlsx, wish_list_cache_path
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    data = json.loads(wish_list_cache_path().read_text())
    ts = data["updated_at"]
    # Should parse without error and be timezone-aware
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None


def test_wish_list_cache_overwritten_on_reimport(tmp_path):
    """Re-importing overwrites wish-list.json rather than appending."""
    from locg.collection_io import import_xlsx, wish_list_cache_path
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)
    first_mtime = wish_list_cache_path().stat().st_mtime_ns
    import_xlsx(SAMPLE_XLSX, cache)
    second_mtime = wish_list_cache_path().stat().st_mtime_ns
    # Second import must have produced a fresh file
    assert second_mtime >= first_mtime
    data = json.loads(wish_list_cache_path().read_text())
    assert isinstance(data["items"], list)


def test_import_preserves_local_wish_list_add(tmp_path):
    """A local `wish-list add` survives a subsequent import (BUI-47).

    Regression: import used to overwrite wish-list.json from the export's
    in_wish_list rows, silently dropping local-only adds.
    """
    from locg.collection_io import import_xlsx, wish_list_cache_path
    import locg.commands as cmds

    cmds.cmd_wish_list_add("Saga #1")  # local-only add, not in the fixture export
    cache = make_cache(tmp_path)
    import_xlsx(SAMPLE_XLSX, cache)

    items = json.loads(wish_list_cache_path().read_text())["items"]
    saga = [i for i in items if i["name"] == "Saga #1"]
    assert len(saga) == 1, "local wish-list add must survive the import"
    assert saga[0].get("series_name") is None  # still a local-only entry
    assert any(i.get("series_name") for i in items)  # export rows present too


def test_write_wish_list_cache_dedups_local_when_in_export(tmp_path):
    """A local add that now appears in the export is not duplicated (BUI-47)."""
    from locg.collection_io import _write_wish_list_cache, wish_list_cache_path

    p = wish_list_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "updated_at": "2026-05-30T00:00:00+00:00",
        "items": [{"name": "Batman #1", "id": None}],  # local-only add
    }))

    # The same title now arrives from the LOCG export (carries a series_name).
    _write_wish_list_cache([{
        "full_title": "Batman #1",
        "series_name": "Batman (1940 - 2011)",
        "publisher_name": "DC Comics",
        "release_date": "1940-04-25",
        "media_format": "Print",
    }])

    items = json.loads(p.read_text())["items"]
    batman = [i for i in items if i["name"] == "Batman #1"]
    assert len(batman) == 1, "must dedupe, not duplicate"
    assert batman[0]["series_name"] == "Batman (1940 - 2011)"  # export version wins


def test_wish_list_cache_empty_when_no_wish_list_rows(tmp_path, monkeypatch):
    """An XLSX with zero wish-list rows writes items: [] without error."""
    import openpyxl
    from locg.collection_io import import_xlsx, wish_list_cache_path, LOCG_XLSX_HEADERS

    # Build a minimal XLSX with one collection-only row (in_wish_list=0)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(LOCG_XLSX_HEADERS))
    ws.append([
        "Marvel", "X-Men", "X-Men #1", "1963-09-01",
        1, 0, 0, None, "Print", None, None, None, None,
        None, None, None, None, None, None, None, None,
    ])
    xlsx_path = tmp_path / "no_wish.xlsx"
    wb.save(xlsx_path)

    cache = make_cache(tmp_path)
    import_xlsx(xlsx_path, cache)
    data = json.loads(wish_list_cache_path().read_text())
    assert data["items"] == []
    assert "updated_at" in data


# ---------------------------------------------------------------------------
# wish_rows_for_export (BUI-122): export must never emit In Collection=0 for an
# owned book, and pushes only the local-only (diff) adds, not the full list.
# ---------------------------------------------------------------------------

def _seed_wish(items):
    from locg.collection_io import wish_list_cache_path
    p = wish_list_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"updated_at": "2026-01-01T00:00:00+00:00", "items": items}))


def test_wish_export_keeps_local_only_unowned(tmp_path):
    """A local-only add for a book not in the collection is exported."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{"name": "Saga #1", "id": None}])
    rows = wish_rows_for_export({"comics": []})
    assert [r["full_title"] for r in rows] == ["Saga #1"]


def test_wish_export_excludes_derived_wishes(tmp_path):
    """A derived wish (carries series_name → LOCG already has it) is NOT exported."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{
        "name": "Batman #1", "id": None,
        "series_name": "Batman (1940 - 2011)", "publisher_name": "DC Comics",
        "release_date": "1940-04-25",
    }])
    assert wish_rows_for_export({"comics": []}) == []


def test_wish_export_excludes_owned_book(tmp_path):
    """CRITICAL safety: a local-only add for a book that IS owned is excluded, so
    the CSV can never carry In Collection=0 for it (the deletion bug)."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([
        {"name": "Marvel Tales #228", "id": None},   # owned -> must be excluded
        {"name": "Hellboy: Wake the Devil #1", "id": None},  # not owned -> kept
    ])
    payload = {"comics": [
        {"full_title": "Marvel Tales #228", "in_collection": 1},
    ]}
    rows = wish_rows_for_export(payload)
    titles = [r["full_title"] for r in rows]
    assert "Marvel Tales #228" not in titles, "owned book must never be a wish row"
    assert "Hellboy: Wake the Devil #1" in titles


def test_wish_export_excludes_owned_xmen_masthead_split(tmp_path):
    """BUI-200 REGRESSION (the 26-deleted-books bug): a wish written under one
    X-Men masthead must NOT be exported as In Collection=0 when the owned copy is
    filed under the OTHER masthead. LOCG files #1-141 under 'The X-Men' and #142+
    under 'Uncanny X-Men'; a literal-title match misses this and the resulting
    In Collection=0 row deletes the owned copy."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([
        {"name": "Uncanny X-Men #107", "id": None},  # owned as "The X-Men #107"
        {"name": "Uncanny X-Men #142", "id": None},  # genuinely not owned -> kept
    ])
    payload = {"comics": [
        {"full_title": "The X-Men #107", "in_collection": 1},
    ]}
    titles = [r["full_title"] for r in wish_rows_for_export(payload)]
    assert "Uncanny X-Men #107" not in titles, "owned cross-masthead book must never export"
    assert "Uncanny X-Men #142" in titles


def test_wish_export_excludes_owned_leading_article_variant(tmp_path):
    """BUI-200: owned under a leading-article + decorated series name, wished
    without the article. The normalized (series, issue) match excludes it so no
    In Collection=0 row is emitted for the owned book."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{"name": "Incredible Hulk #181", "id": None}])
    payload = {"comics": [
        {"full_title": "The Incredible Hulk #181", "in_collection": 1},
    ]}
    assert wish_rows_for_export(payload) == []


def test_wish_export_owned_match_is_dash_and_article_insensitive(tmp_path):
    """Owned-exclusion normalizes en-dash/hyphen and a leading article, so an
    owned 'Batman: One Bad Day – Two-Face #1' (en-dash) still excludes a wish add
    written with a hyphen / 'The' prefix."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{"name": "Batman: One Bad Day - Two-Face #1", "id": None}])  # hyphen
    payload = {"comics": [
        {"full_title": "Batman: One Bad Day – Two-Face #1", "in_collection": 1},  # en-dash
    ]}
    assert wish_rows_for_export(payload) == []


# --- BUI-197: owned-safe export must be masthead-alias aware (delete-prevention) ---

def test_wish_export_excludes_owned_thor_masthead_alias(tmp_path):
    """CRITICAL (BUI-197): a wish written 'The Mighty Thor #300' must NOT be
    exported as In Collection=0 when the owned copy is filed 'Thor #300'. Routing
    the export through the alias-aware owned_match_keys closes the masthead-alias
    variant of the delete bug."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([
        {"name": "The Mighty Thor #300", "id": None},   # owned as Thor #300
        {"name": "The Mighty Thor #999", "id": None},   # genuinely not owned
    ])
    payload = {"comics": [
        {"full_title": "Thor #300", "in_collection": 1},
    ]}
    titles = [r["full_title"] for r in wish_rows_for_export(payload)]
    assert "The Mighty Thor #300" not in titles, "owned alias book must never export"
    assert "The Mighty Thor #999" in titles


def test_wish_export_excludes_owned_hulk_masthead_alias(tmp_path):
    """BUI-197: wished 'Incredible Hulk #181', owned 'The Incredible Hulk #181'
    (masthead + leading article). Must never emit In Collection=0."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{"name": "Incredible Hulk #181", "id": None}])
    payload = {"comics": [
        {"full_title": "The Incredible Hulk #181", "in_collection": 1},
    ]}
    assert wish_rows_for_export(payload) == []


def test_wish_export_excludes_owned_annual_masthead_alias(tmp_path):
    """BUI-197: an annual owned under one masthead, wished under another. Owned
    'Uncanny X-Men Annual #9', wished 'X-Men Annual #9' — must not export."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{"name": "X-Men Annual #9", "id": None}])
    payload = {"comics": [
        {"full_title": "Uncanny X-Men Annual #9", "in_collection": 1},
    ]}
    assert wish_rows_for_export(payload) == []


def test_wish_export_excludes_owned_non_digit_issue_token_alias(tmp_path):
    """BUI-197 MUST-FIX 1 (deletion hole): a wish with a NON-digit-led issue token
    ('#A1') owned under an alias name must NOT be emitted In Collection=0. The
    digit-led parser dropped the token, the title-string fallback doesn't alias
    mastheads, so the owned copy would have been deleted. The permissive ownership
    split + alias-aware (series,issue) index now exclude it."""
    from locg.collection_io import wish_rows_for_export
    _seed_wish([{"name": "The Mighty Thor Annual #A1", "id": None}])
    payload = {"comics": [
        {"full_title": "Thor Annual #A1", "in_collection": 1},  # owned under alias name
    ]}
    assert wish_rows_for_export(payload) == [], "owned non-digit-token book must never export"


# --- BUI-197: audit ↔ export parser parity ---

def test_audit_export_parser_parity(tmp_path, monkeypatch):
    """The conflicts audit and the owned-safe export must agree on EVERY wish
    title: an audit conflict ⇒ the export does NOT emit that owned book as
    In Collection=0 (and a non-conflict local-only wish IS emitted). Both now go
    through the single shared split_series_issue_for_ownership parser, so a clean
    audit proves an owned-safe CSV. Crucially this covers NON-digit-led tokens
    (#A1, #annual, #1-A) — the BUI-197 deletion hole, where the digit-led parser
    made such a wish 'unparseable', skipped the ownership check, and exported it
    In Collection=0 over an owned copy filed under an alias name."""
    import locg.commands as cmds

    # A shared owned corpus + a wish set whose tokens stressed the old divergence,
    # including the non-digit-led tokens that reopened the deletion hole.
    owned = [
        ("Thor (Vol. 1) (1966 - 1996)", "Thor #300", "1980-10-01"),
        ("The Incredible Hulk (1968 - 1999)", "The Incredible Hulk #181", "1974-11-01"),
        ("Uncanny X-Men Annual (1980 - 2011)", "Uncanny X-Men Annual #9", "1985-12-01"),
        ("The X-Men (Vol. 1) (1963 - 1981)", "The X-Men #137", "1980-09-01"),
        ("Thor Annual (1966 - 1994)", "Thor Annual #A1", "1966-01-01"),   # non-digit token
        ("The Incredible Hulk (1968 - 1999)", "The Incredible Hulk #annual", "1978-01-01"),
        ("Thor (Vol. 1) (1966 - 1996)", "Thor #1-A", "1966-01-01"),       # hyphen-suffix token
    ]
    wishes = [
        {"name": "The Mighty Thor #300", "id": 1},        # owned via alias
        {"name": "Incredible Hulk #181", "id": 2},        # owned via alias
        {"name": "X-Men Annual #9", "id": 3},             # owned via annual alias
        {"name": "Uncanny X-Men #137", "id": 4},          # owned via split
        {"name": "The Mighty Thor Annual #A1", "id": 7},  # owned via alias + #A1 token
        {"name": "Incredible Hulk #annual", "id": 8},     # owned via alias + word token
        {"name": "The Mighty Thor #1-A", "id": 9},        # owned via alias + #1-A token
        {"name": "The Mighty Thor #999", "id": 5},        # genuinely not owned
        {"name": "Saga #1", "id": 6},                     # genuinely not owned
    ]

    # --- audit side (uses the conftest-isolated cache via CollectionCache) ---
    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    owned_rows = [
        make_agent_win_row(series=s, full_title=ft, release_date=rd, gixen_item_id=str(i))
        for i, (s, ft, rd) in enumerate(owned)
    ]
    cache.apply(lambda p: p["comics"].extend(owned_rows), command="seed")
    _seed_wish(wishes)  # the audit reads the same wish-list cache the export does
    audit = cmds.cmd_wish_list_conflicts()
    assert audit["unparseable"] == [], "no wish (incl. non-digit tokens) may be skipped"
    conflict_names = {c["name"] for c in audit["conflicts"]}
    assert conflict_names == {
        "The Mighty Thor #300", "Incredible Hulk #181",
        "X-Men Annual #9", "Uncanny X-Men #137",
        "The Mighty Thor Annual #A1", "Incredible Hulk #annual",
        "The Mighty Thor #1-A",
    }

    # --- export side: same wish-list cache (no series_name → local-only adds) and
    # the same owned corpus, so the two paths are compared on identical input.
    from locg.collection_io import wish_rows_for_export
    payload = {"comics": [
        {"full_title": ft, "in_collection": 1} for (_s, ft, _rd) in owned
    ]}
    exported = {r["full_title"] for r in wish_rows_for_export(payload)}

    # PARITY: every audited conflict must be absent from the export (owned-safe).
    for name in conflict_names:
        assert name not in exported, f"audit flagged {name!r} owned but export emitted it"
    # And the genuinely-unowned local-only wishes ARE exported.
    assert "The Mighty Thor #999" in exported
    assert "Saga #1" in exported


# ---------------------------------------------------------------------------
# import_xlsx — BUI-124: hold ownership downgrades in the Phase-2 standard merge.
# The gixen server is the source of truth (BUI-87), so a LOCG export reporting
# In Collection=0 over an owned row must NOT silently un-own the book (which
# would make collection-check buy a duplicate). The downgrade is held (existing
# in_collection preserved), flagged as ownership_downgrade_held, and counted.
# ---------------------------------------------------------------------------

def _import_owned_then(tmp_path: Path, first: dict[str, Any], second: dict[str, Any]):
    """Import `first` (establishes an owned locg_export row), then `second`
    (the candidate downgrade). Returns (cache, second_result)."""
    from locg.collection_io import import_xlsx

    cache = make_cache(tmp_path)
    xlsx1 = tmp_path / "first.xlsx"
    _build_export_xlsx(xlsx1, [first])
    import_xlsx(xlsx1, cache)

    xlsx2 = tmp_path / "second.xlsx"
    _build_export_xlsx(xlsx2, [second])
    result = import_xlsx(xlsx2, cache)
    return cache, result


def _audit_types(tmp_path: Path) -> list[str]:
    path = tmp_path / "import-history.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    return [json.loads(l)["type"] for l in lines if l]


def test_exact_identity_downgrade_held(tmp_path):
    """Exact-identity update: an owned row stays owned when the re-export says
    In Collection=0; the hold is flagged and counted."""
    base = {
        "publisher": "Marvel Comics", "series": "Daredevil",
        "full_title": "Daredevil #181", "release_date": "1982-04-10",
    }
    cache, result = _import_owned_then(
        tmp_path,
        {**base, "in_collection": 1},
        {**base, "in_collection": 0},
    )

    rows = [r for r in cache.load()["comics"] if r["full_title"] == "Daredevil #181"]
    assert len(rows) == 1
    assert rows[0]["in_collection"] == 1, "ownership must be preserved, not downgraded"
    assert result["ownership_downgrades_held"] == 1
    assert "ownership_downgrade_held" in _audit_types(tmp_path)


def test_rename_branch_downgrade_held(tmp_path):
    """Rename branch (same publisher/series/release_date, new full_title): an
    owned row stays owned when the renamed export row says In Collection=0."""
    cache, result = _import_owned_then(
        tmp_path,
        {"publisher": "Marvel Comics", "series": "Amazing Spider-Man",
         "full_title": "Amazing Spider-Man #300", "release_date": "1988-05-10",
         "in_collection": 1},
        {"publisher": "Marvel Comics", "series": "Amazing Spider-Man",
         "full_title": "Amazing Spider-Man #300 Direct", "release_date": "1988-05-10",
         "in_collection": 0},
    )

    rows = cache.load()["comics"]
    renamed = [r for r in rows if r["full_title"] == "Amazing Spider-Man #300 Direct"]
    assert len(renamed) == 1, "rename must update in place, not insert"
    assert renamed[0]["in_collection"] == 1, "ownership preserved across the rename"
    assert renamed[0]["previous_full_title"] == "Amazing Spider-Man #300"
    assert result["ownership_downgrades_held"] == 1
    assert "ownership_downgrade_held" in _audit_types(tmp_path)


def test_non_downgrade_copies_in_collection_normally(tmp_path):
    """A re-export that keeps the book owned (In Collection=1) copies through and
    is NOT flagged as a held downgrade."""
    base = {
        "publisher": "Marvel Comics", "series": "Daredevil",
        "full_title": "Daredevil #181", "release_date": "1982-04-10",
    }
    cache, result = _import_owned_then(
        tmp_path,
        {**base, "in_collection": 1},
        {**base, "in_collection": 1},
    )

    rows = [r for r in cache.load()["comics"] if r["full_title"] == "Daredevil #181"]
    assert rows[0]["in_collection"] == 1
    assert result["ownership_downgrades_held"] == 0
    assert "ownership_downgrade_held" not in _audit_types(tmp_path)


def test_count_decrease_that_stays_owned_copies_normally(tmp_path):
    """in_collection is a copies-owned count; a decrease that stays truthy
    (2 -> 1) is a normal update, not a downgrade — it copies through unflagged."""
    base = {
        "publisher": "Marvel Comics", "series": "Daredevil",
        "full_title": "Daredevil #181", "release_date": "1982-04-10",
    }
    cache, result = _import_owned_then(
        tmp_path,
        {**base, "in_collection": 2},
        {**base, "in_collection": 1},
    )

    rows = [r for r in cache.load()["comics"] if r["full_title"] == "Daredevil #181"]
    assert rows[0]["in_collection"] == 1, "count change that stays owned applies"
    assert result["ownership_downgrades_held"] == 0
    assert "ownership_downgrade_held" not in _audit_types(tmp_path)
