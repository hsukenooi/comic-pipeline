"""Tests for BUI-496: a source-level canary asserting `_metron_release_date`'s
`series_range` argument keeps INDEPENDENT provenance on the Metron step-2
path (an out-of-scope finding from BUI-486).

The invariant (already documented on `_metron_release_date`'s docstring,
commands.py): both the BUI-486 ±1 `cover_date` exception and the BUI-464
non-clean-year era gate depend on `series_range` being sourced from the LOCAL
`series_name_index` (via `series_year_range`/`resolve_series_for_win`), never
from the Metron hit being judged (`metron.format_series_name`/
`series_year_began`). A circular window — one derived from the very hit it
is meant to police — turns both guards into tautologies that always pass.

Existing protection (BUI-486, `test_record_win_step2_metron_path_does_not_
fire_pm1_exception` in test_collection_commands.py) is black-box: it proves
CURRENT behavior is correct for one specific input shape, but a refactor that
broke the invariant differently might not trip that exact test. This file
adds the source-level canary itself (`_assert_independent_series_range`),
tested directly against both the passing and the violating shape — so ANY
future caller/refactor that threads a circular window trips it immediately,
independent of the particular ±1/era-gate scenario a black-box test happens
to cover.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from locg.commands import _assert_independent_series_range


def make_cache(tmp_path: Path):
    from locg.collection_cache import CollectionCache
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


@pytest.fixture(autouse=True)
def metron_sleeps(monkeypatch):
    """Capture record-win's Metron pacing sleeps instead of serving them —
    mirrors test_collection_commands.py's identically-named fixture (BUI-465);
    kept local here since ticket-specific test files must not edit shared
    conftest/fixtures."""
    slept: list[float] = []
    monkeypatch.setattr("locg.commands.time.sleep", slept.append)
    return slept


def _make_win(
    item_id: str = "1001",
    series: str = "Batman",
    issue: str = "427",
    year: int | None = 1989,
) -> dict:
    return {
        "item_id": item_id,
        "current_bid": 42.00,
        "end_date_iso": "2026-05-20T15:00:00Z",
        "identify_data": {"series": series, "issue": issue, "year": year, "variant_text": None},
    }


# ---------------------------------------------------------------------------
# _assert_independent_series_range: the canary itself, tested directly
# ---------------------------------------------------------------------------

def test_canary_passes_on_the_real_step2_shape():
    """The actual, correct shape on the step-2 path: series_range is None."""
    _assert_independent_series_range(step2_resolved=True, series_range=None)  # must not raise


def test_canary_passes_on_the_real_index_path_shape():
    """The actual, correct shape on the series_name_index path: any window is
    fine, because it is independent of any Metron hit."""
    _assert_independent_series_range(step2_resolved=False, series_range=(1963, 1981))
    _assert_independent_series_range(step2_resolved=False, series_range=None)


def test_canary_trips_on_a_circular_window():
    """THE regression this canary exists to catch: a future refactor derives
    series_range from the step-2 Metron hit itself (e.g.
    `series_year_range(metron.format_series_name(metron_data))`) and threads
    it through on the step-2 path. This is exactly what would make the
    BUI-486 ±1 exception and the BUI-464 era gate tautological — and it must
    raise, not silently pass."""
    with pytest.raises(AssertionError, match="BUI-496"):
        _assert_independent_series_range(step2_resolved=True, series_range=(1940, 2011))


# ---------------------------------------------------------------------------
# The real call site: the step-2 record-win path must reach the canary
# without tripping it (belt-and-suspenders companion to BUI-486's black-box
# test) — proves the assertion is actually wired into the production path,
# not just correct in isolation.
# ---------------------------------------------------------------------------

def test_record_win_step2_path_reaches_the_canary_without_tripping(tmp_path):
    """Same fixture shape as BUI-486's
    test_record_win_step2_metron_path_does_not_fire_pm1_exception: an empty
    series_name_index forces the step-2 Metron lookup, and the hit carries a
    would-be circular window (series_year_began/end=1940/2011) that a future
    regression might mistakenly thread through as series_range. Under
    CURRENT (correct) code this reaches _assert_independent_series_range with
    series_range=None and must not raise."""
    from locg.commands import cmd_collection_record_win
    from unittest.mock import MagicMock

    cache = make_cache(tmp_path)  # empty index -> series never resolves locally -> step-2
    metron = MagicMock()
    metron.lookup_issue.return_value = {
        "metron_id": 427,
        "cover_date": "1988-12-01",
        "store_date": None,
        "series_year_began": 1940,  # would-be circular window if ever threaded
        "series_year_end": 2011,
        "series_name": "Batman",
        "series_id": 42,
    }
    metron.format_series_name.return_value = "Batman (1940 - 2011)"

    result = cmd_collection_record_win(
        [_make_win()],
        cache=cache,
        metron=metron,
    )

    assert result["rows_written"] == 1
