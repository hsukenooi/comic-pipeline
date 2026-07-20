"""Tests for `locg collection audit-pending` (BUI-432).

Moves the ~30-line inline Python that `/comic:collection-sync` Step 2b used to
re-author every run (`.claude/commands/comic/collection-sync.md`) into tested
code. These tests assert flag parity with that inline script's behavior on the
same rows, plus the dateless summary and the all-dateless-batch hang warning.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

import pytest

from locg.collection_cache import CollectionCache
from locg.commands import cmd_collection_audit_pending

# The 21-column LOCG CSV header (collection_io.LOCG_XLSX_HEADERS) — only the
# four columns the audit reads are populated per-row here; the rest exist so
# fixtures resemble a real export and DictReader sees a realistic header row.
_HEADER = [
    "Publisher Name", "Series Name", "Full Title", "Release Date",
    "In Collection", "In Wish List", "Marked Read", "My Rating",
    "Media Format", "Price Paid", "Date Purchased", "Condition", "Notes",
    "Tags", "Storage Box", "Owner", "Purchase Store", "Signature",
    "Slabbing", "Grading", "Grading Company",
]


def _row(
    publisher: str = "Marvel Comics",
    series: str = "Amazing Spider-Man (1963 - 1998)",
    full_title: str = "Amazing Spider-Man #300",
    release_date: str = "1988-05-10",
) -> dict[str, str]:
    row = {h: "" for h in _HEADER}
    row.update({
        "Publisher Name": publisher,
        "Series Name": series,
        "Full Title": full_title,
        "Release Date": release_date,
        "In Collection": "1",
        "In Wish List": "0",
        "Media Format": "Print",
        "Purchase Store": "eBay",
    })
    return row


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _make_cache(tmp_path: Path, name: str = "collection") -> CollectionCache:
    return CollectionCache(
        path=tmp_path / f"{name}.json",
        lock_path=tmp_path / f"{name}.lock",
        audit_path=tmp_path / f"{name}-history.jsonl",
    )


def _seed_store_row(
    cache: CollectionCache,
    *,
    series_name: str,
    full_title: str,
    release_date: str,
    metron_id: Optional[int] = None,
    source: str = "agent_win",
) -> None:
    """Seed one pending store row (BUI-466 store-correlation tests).

    Fields mirror what ``_row_to_csv_dict`` reads and what
    ``_is_placeholder_release_date`` gates on: ``source`` and ``metron_id``
    decide the placeholder's INTENT, ``series_name``/``full_title`` are the
    correlation key, ``pushed_to_locg_at is None`` keeps it in the "ready to
    push" set the audit's store lookup draws from.
    """
    def mutate(payload: dict[str, Any]) -> None:
        payload["comics"].append({
            "publisher_name": "Marvel Comics",
            "series_name": series_name,
            "full_title": full_title,
            "release_date": release_date,
            "in_collection": 1,
            "in_wish_list": 0,
            "marked_read": 0,
            "my_rating": None,
            "media_format": "Print",
            "price_paid": 10.0,
            "date_purchased": "2026-07-01",
            "source": source,
            "gixen_item_id": "w-1",
            "needs_review": None,
            "metron_id": metron_id,
            "pushed_to_locg_at": None,
            "local_added_at": "2026-07-01T00:00:00.000000Z",
        })
    cache.apply(mutate, command="test-seed")


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_audit_missing_csv_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        cmd_collection_audit_pending(str(tmp_path / "does-not-exist.csv"))


# ---------------------------------------------------------------------------
# Clean rows — no flags
# ---------------------------------------------------------------------------

def test_audit_clean_row_is_not_flagged(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [_row()])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["row_count"] == 1
    assert result["flagged_count"] == 0
    assert result["flagged_rows"] == []
    assert result["dateless_count"] == 0
    assert result["dateless_titles"] == []
    assert result["all_dateless"] is False
    assert result["dateless_warning"] is None
    assert result["csv_path"] == str(csv_path)


# ---------------------------------------------------------------------------
# Per-row flag parity with the inline checks (collection-sync.md ~:155-180)
# ---------------------------------------------------------------------------

def test_audit_flags_missing_publisher(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(publisher="")])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["flagged_count"] == 1
    flagged = result["flagged_rows"][0]
    assert flagged["missing_publisher"] is True
    assert "no publisher" in flagged["issues"]


def test_audit_flags_missing_series(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(series="")])
    result = cmd_collection_audit_pending(str(csv_path))

    flagged = result["flagged_rows"][0]
    assert flagged["missing_series"] is True
    assert "no series" in flagged["issues"]


def test_audit_flags_missing_full_title(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(full_title="")])
    result = cmd_collection_audit_pending(str(csv_path))

    flagged = result["flagged_rows"][0]
    assert flagged["missing_full_title"] is True
    assert "no full_title" in flagged["issues"]
    # ft is falsy ("") so the inline `ft or "(blank)"` display fallback applies.
    assert flagged["full_title"] == "(blank)"


def test_audit_flags_decorated_full_title_vol(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(full_title="Fantastic Four (Vol. 3) #1"),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    flagged = result["flagged_rows"][0]
    assert flagged["decorated_full_title"] is True
    assert any("decorated full_title" in i for i in flagged["issues"])


def test_audit_flags_decorated_full_title_year(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(full_title="Fantastic Four #1 (1997 - 2012)"),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    flagged = result["flagged_rows"][0]
    assert flagged["decorated_full_title"] is True


def test_audit_undecorated_full_title_with_parenthesized_non_year_not_flagged(tmp_path):
    """The decoration regex only trips on `(Vol.` or `(<4 digits>` — a
    parenthesized non-year, non-Vol. aside (e.g. a variant note) must not
    false-positive, matching the inline regex exactly."""
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(full_title="Amazing Spider-Man #300 (Newsstand)"),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["flagged_count"] == 0


def test_audit_confirmed_placeholder_stays_hard_stop(tmp_path):
    """BUI-466: a store row that is source=agent_win with no metron_id confirms
    the shape match as a genuine BUI-105 placeholder — it must still hard-stop
    (flagged_count), since that IS the case the flag exists to catch."""
    cache = _make_cache(tmp_path)
    _seed_store_row(
        cache,
        series_name="Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #300",
        release_date="1988-01-01",
        metron_id=None,
    )
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(release_date="1988-01-01")])

    result = cmd_collection_audit_pending(str(csv_path), cache=cache)

    assert result["flagged_count"] == 1
    assert result["advisory_count"] == 0
    flagged = result["flagged_rows"][0]
    assert flagged["placeholder_date"] is True
    assert flagged["placeholder_date_confirmed"] is True
    assert any("confirmed BUI-105 placeholder" in i for i in flagged["issues"])


def test_audit_confirmed_genuine_january_date_is_advisory_not_hard_stop(tmp_path):
    """BUI-466 acceptance criterion: a pending set whose only anomaly is a
    genuine January cover date (store row carries a metron_id) must NOT
    hard-stop the sync — demoted to advisory, excluded from flagged_count."""
    cache = _make_cache(tmp_path)
    _seed_store_row(
        cache,
        series_name="Amazing Spider-Man (1963 - 1998)",
        full_title="Amazing Spider-Man #300",
        release_date="1988-01-01",
        metron_id=5001,
    )
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(release_date="1988-01-01")])

    result = cmd_collection_audit_pending(str(csv_path), cache=cache)

    assert result["flagged_count"] == 0
    assert result["flagged_rows"] == []
    assert result["advisory_count"] == 1
    advisory = result["advisory_rows"][0]
    assert advisory["placeholder_date"] is True
    assert advisory["placeholder_date_confirmed"] is False
    assert any("genuine cover date" in i for i in advisory["issues"])


def test_audit_unconfirmable_placeholder_is_advisory_not_hard_stop(tmp_path):
    """BUI-466: no matching store row (0 or >1 matches, or an unreadable/never
    -imported store) can't confirm intent either way. Must still NOT hard-stop
    — a genuine BUI-105 placeholder is always blanked before it reaches the
    CSV, so an unconfirmed non-blank Jan-1 date is, by construction, almost
    always a real date. The row is still surfaced (advisory_rows), just not
    blocking."""
    empty_cache = _make_cache(tmp_path, name="never-imported")  # store file never written
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(release_date="1988-01-01")])

    result = cmd_collection_audit_pending(str(csv_path), cache=empty_cache)

    assert result["flagged_count"] == 0
    assert result["advisory_count"] == 1
    advisory = result["advisory_rows"][0]
    assert advisory["placeholder_date"] is True
    assert advisory["placeholder_date_confirmed"] is None
    assert any("could not be matched" in i for i in advisory["issues"])


def test_audit_non_jan1_date_not_flagged_as_placeholder(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(release_date="1988-05-10"),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["flagged_count"] == 0


def test_audit_blank_date_not_flagged_as_placeholder(tmp_path):
    """A blank Release Date is a *dateless* row, not a placeholder-date row —
    the inline script's `if dt and re.match(...)` short-circuits on a falsy
    (blank) dt, so it must not also appear in flagged_rows for this reason."""
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(release_date="")])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["flagged_count"] == 0
    assert result["dateless_count"] == 1


def test_audit_row_with_multiple_issues_lists_all(tmp_path):
    """The unconfirmable Jan-1 date is demoted to advisory (BUI-466), but the
    row still lands in flagged_rows because of its OTHER hard-stop issues —
    and its issues list still carries all 4 messages (3 hard-stop + the
    advisory placeholder note)."""
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(publisher="", series="", full_title="Fantastic Four (Vol. 3) #1", release_date="1998-01-01"),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    flagged = result["flagged_rows"][0]
    assert flagged["missing_publisher"] is True
    assert flagged["missing_series"] is True
    assert flagged["decorated_full_title"] is True
    assert flagged["placeholder_date"] is True
    assert flagged["placeholder_date_confirmed"] is None
    assert len(flagged["issues"]) == 4


def test_audit_row_index_and_full_title_recorded(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(full_title="Amazing Spider-Man #300"),
        _row(full_title="", publisher=""),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["row_count"] == 2
    assert result["flagged_count"] == 1
    flagged = result["flagged_rows"][0]
    assert flagged["row_index"] == 1
    assert flagged["full_title"] == "(blank)"


# ---------------------------------------------------------------------------
# Dateless summary + all-dateless-batch hang warning
# ---------------------------------------------------------------------------

def test_audit_no_dateless_rows_no_warning(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(), _row(full_title="Batman #1")])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["dateless_count"] == 0
    assert result["dateless_titles"] == []
    assert result["all_dateless"] is False
    assert result["dateless_warning"] is None


def test_audit_partial_dateless_batch_warns_but_not_all_dateless(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(full_title="Amazing Spider-Man #300", release_date="1988-05-10"),
        _row(full_title="Batman #1", release_date=""),
        _row(full_title="Saga #1", release_date=""),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["dateless_count"] == 2
    assert set(result["dateless_titles"]) == {"Batman #1", "Saga #1"}
    assert result["all_dateless"] is False
    assert result["dateless_warning"] is not None
    assert "2/3" in result["dateless_warning"]


def test_audit_all_dateless_batch_triggers_hang_warning(tmp_path):
    """A fully-dateless batch is the importer-hang scenario the inline script's
    comment calls out explicitly — must be flagged distinctly (all_dateless)
    as well as via the same dateless_warning text."""
    csv_path = _write_csv(tmp_path / "wins.csv", [
        _row(full_title="Batman #1", release_date=""),
        _row(full_title="Saga #1", release_date=""),
        _row(full_title="Fantastic Four #1", release_date=""),
    ])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["dateless_count"] == 3
    assert result["row_count"] == 3
    assert result["all_dateless"] is True
    assert result["dateless_warning"] is not None
    assert "3/3" in result["dateless_warning"]
    assert "backfill" in result["dateless_warning"].lower()


def test_audit_empty_csv_no_rows(tmp_path):
    """A header-only CSV (zero pending rows) must not report all_dateless=True
    vacuously — there is nothing to hang the importer."""
    csv_path = _write_csv(tmp_path / "wins.csv", [])
    result = cmd_collection_audit_pending(str(csv_path))

    assert result["row_count"] == 0
    assert result["flagged_count"] == 0
    assert result["dateless_count"] == 0
    assert result["all_dateless"] is False
    assert result["dateless_warning"] is None


# ---------------------------------------------------------------------------
# Read-only: never mutates the input CSV
# ---------------------------------------------------------------------------

def test_audit_does_not_mutate_csv_file(tmp_path):
    csv_path = _write_csv(tmp_path / "wins.csv", [_row(release_date="")])
    before = csv_path.read_text()
    before_mtime = csv_path.stat().st_mtime_ns

    cmd_collection_audit_pending(str(csv_path))

    assert csv_path.read_text() == before
    assert csv_path.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# Integration: audits the real column names cmd_collection_export produces
# ---------------------------------------------------------------------------

def test_audit_integrates_with_real_export_headers(tmp_path, monkeypatch):
    """Guards the exact foot-gun BUI-432 calls out: a CSV column rename in the
    real export path must be caught here, not silently mis-audit. Runs the
    real export pipeline (record-win -> export) and audits its own CSV."""
    from locg.collection_cache import CollectionCache
    import locg.commands as cmds

    cache = CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    def mutate(payload):
        payload["comics"].append({
            "publisher_name": "Marvel Comics",
            "series_name": "Amazing Spider-Man (1963 - 1998)",
            "full_title": "Amazing Spider-Man #300",
            "release_date": "1988-05-10",
            "in_collection": 1,
            "in_wish_list": 0,
            "marked_read": 0,
            "my_rating": None,
            "media_format": "Print",
            "price_paid": 42.0,
            "date_purchased": "2026-05-22",
            "source": "agent_win",
            "gixen_item_id": "1001",
            "needs_review": None,
            "pushed_to_locg_at": None,
        })
    cache.apply(mutate, command="seed")

    out_csv = tmp_path / "export.csv"
    export_result = cmds.cmd_collection_export(str(out_csv))
    assert export_result["ready_count"] == 1

    audit = cmd_collection_audit_pending(export_result["csv_path"])
    assert audit["row_count"] == 1
    assert audit["flagged_count"] == 0
    assert audit["dateless_count"] == 0
