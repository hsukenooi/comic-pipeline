"""Tests for `locg collection audit-unscoped-lookup` (BUI-493, a BUI-488
follow-up).

Pre-BUI-256, `lookup_issue` queried mokkari with the wrong param key
(`{"series": id}` instead of `{"series_id": id}`), which mokkari silently
ignored — degrading the issue query to an UNSCOPED `number=N` search across
all of Metron. `issues[0]` then came from a wrong, unrelated book:
`series_name` stayed correct (it came from LOCAL series resolution) but
`metron_id`/`release_date` were stamped from whatever unrelated issue #N
Metron happened to return first. The bug is fixed (BUI-256), but rows written
before the fix are latent, and because the mis-stamped `metron_id` is a LIVE
Metron id (just for the wrong book), no placeholder-hunting audit (e.g.
audit-pending) catches them.

BUI-488 found exactly one such row in the live 2026-07-19 backup: The
Infinity Gauntlet #1 (series window 1991, metron_id 52529, release_date
2022-09-14 — a 2022 AfterShock book's date). CI has no live store, so these
tests exercise the predicate/command against small fixture rows built to
mirror that fingerprint plus the documented non-matches (a legitimate
locg_export reprint, an in-window win, and a metron_id-less win whose year is
simply unresolved).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from locg.collection_cache import CollectionCache
from locg.commands import _is_unscoped_lookup_mismatch, cmd_collection_audit_unscoped_lookup


def _make_cache(tmp_path: Path) -> CollectionCache:
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    *,
    series_name: str,
    full_title: str,
    release_date: Optional[str],
    metron_id: Optional[int] = None,
    source: str = "agent_win",
    gixen_item_id: Optional[str] = "w-1",
) -> dict[str, Any]:
    return {
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
        "gixen_item_id": gixen_item_id,
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": metron_id,
        "pushed_to_locg_at": None,
        "local_added_at": "2026-07-01T00:00:00.000000Z",
        "previous_full_title": None,
    }


def _seed(cache: CollectionCache, rows: list[dict[str, Any]]) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        payload["comics"] = list(rows)

    cache.apply(mutate, command="test-seed")


# ---------------------------------------------------------------------------
# _is_unscoped_lookup_mismatch: the predicate, unit-tested directly
# (BUI-493 requires it be a named predicate, not control-flow-buried logic)
# ---------------------------------------------------------------------------

def test_predicate_flags_self_inconsistent_agent_win_row():
    """(a) The BUI-488 fingerprint: agent_win, year outside the series window,
    metron_id present. This MUST flag."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date="2022-09-14",
        metron_id=52529,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is True


def test_predicate_does_not_flag_locg_export_reprint():
    """(b) A legitimate locg_export reprint: metron_id null, title says
    Facsimile Edition, year IN the window. Must NOT flag — and specifically
    because of the source scope, not any title-keyword check."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1 Facsimile Edition",
        release_date="1991-08-01",
        metron_id=None,
        source="locg_export",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_does_not_flag_in_window_agent_win_row():
    """(c) A genuinely correct agent_win row: year inside the series window."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #2",
        release_date="1991-09-01",
        metron_id=52530,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_does_not_flag_out_of_window_row_with_no_metron_id():
    """(d) Year outside the window but NO metron_id: the bug's fingerprint
    requires a live-but-wrong metron_id, so this must NOT flag even though
    the year alone looks suspicious."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date="2022-09-14",
        metron_id=None,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_does_not_flag_unparseable_series_window():
    """A row can't be judged when its series window can't be parsed at all —
    stay silent rather than guess (undecorated series name)."""
    row = _row(
        series_name="Some Bare Series Name",  # no (YYYY) or (YYYY - YYYY) decoration
        full_title="Some Bare Series Name #1",
        release_date="2022-09-14",
        metron_id=999,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_does_not_flag_row_with_no_parseable_year():
    """A dateless (or unparseable) release_date also can't be judged."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date=None,
        metron_id=999,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_boundary_one_year_outside_is_not_flagged():
    """The ±1 tolerance matches the rest of the codebase's era-gate
    convention (BUI-486/BUI-464): a single year outside a bare one-year
    window is within tolerance and must NOT flag."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date="1992-01-01",  # +1, still tolerated
        metron_id=999,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_boundary_two_years_outside_is_flagged():
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date="1993-01-01",  # +2, outside tolerance
        metron_id=999,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is True


def test_predicate_boundary_one_year_before_window_is_not_flagged():
    """The ±1 tolerance is symmetric — one year BEFORE the window (not just
    after) is also within tolerance."""
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date="1990-01-01",  # -1, still tolerated
        metron_id=999,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is False


def test_predicate_boundary_two_years_before_window_is_flagged():
    row = _row(
        series_name="The Infinity Gauntlet (1991)",
        full_title="The Infinity Gauntlet #1",
        release_date="1989-01-01",  # -2, outside tolerance
        metron_id=999,
        source="agent_win",
    )
    assert _is_unscoped_lookup_mismatch(row) is True


def test_command_reports_delta_years_for_a_row_before_the_window(tmp_path):
    """Exercises the OTHER branch of delta_years' computation
    (`begin - year` when the release_date predates the window) — every other
    fixture in this file has a release_date AFTER the window, which only
    exercises the `year - end` branch."""
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(
            series_name="The Infinity Gauntlet (1991)",
            full_title="The Infinity Gauntlet #1",
            release_date="1985-01-01",  # 6 years BEFORE the (1991, 1991) window
            metron_id=999,
            source="agent_win",
            gixen_item_id="w-before",
        ),
    ])

    result = cmd_collection_audit_unscoped_lookup(cache=cache)

    assert result["flagged_count"] == 1
    [flagged] = result["flagged_rows"]
    assert flagged["delta_years"] == 1991 - 1985


# ---------------------------------------------------------------------------
# cmd_collection_audit_unscoped_lookup: the command, against a small store
# ---------------------------------------------------------------------------

def test_command_flags_exactly_the_fingerprinted_row(tmp_path):
    """All four documented cases (a)-(d) together in one store: only the
    self-inconsistent row is flagged, with the exact reported fields."""
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(  # (a) the BUI-488 fingerprint — MUST flag
            series_name="The Infinity Gauntlet (1991)",
            full_title="The Infinity Gauntlet #1",
            release_date="2022-09-14",
            metron_id=52529,
            source="agent_win",
            gixen_item_id="w-1",
        ),
        _row(  # (b) legitimate reprint — must NOT flag
            series_name="The Infinity Gauntlet (1991)",
            full_title="The Infinity Gauntlet #1 Facsimile Edition",
            release_date="1991-08-01",
            metron_id=None,
            source="locg_export",
            gixen_item_id=None,
        ),
        _row(  # (c) in-window win — must NOT flag
            series_name="The Infinity Gauntlet (1991)",
            full_title="The Infinity Gauntlet #2",
            release_date="1991-09-01",
            metron_id=52530,
            source="agent_win",
            gixen_item_id="w-2",
        ),
        _row(  # (d) out-of-window, no metron_id — must NOT flag
            series_name="The Infinity Gauntlet (1991)",
            full_title="The Infinity Gauntlet #3",
            release_date="2022-09-14",
            metron_id=None,
            source="agent_win",
            gixen_item_id="w-3",
        ),
    ])

    result = cmd_collection_audit_unscoped_lookup(cache=cache)

    assert result["row_count"] == 4
    assert result["agent_win_count"] == 3
    assert result["flagged_count"] == 1
    [flagged] = result["flagged_rows"]
    assert flagged["full_title"] == "The Infinity Gauntlet #1"
    assert flagged["series_name"] == "The Infinity Gauntlet (1991)"
    assert flagged["metron_id"] == 52529
    assert flagged["release_date"] == "2022-09-14"
    assert flagged["delta_years"] == 2022 - 1991  # year outside a (1991, 1991) window
    assert flagged["gixen_item_id"] == "w-1"


def test_command_is_read_only(tmp_path):
    """Running the audit must never mutate the store — compare the payload
    before and after."""
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(
            series_name="The Infinity Gauntlet (1991)",
            full_title="The Infinity Gauntlet #1",
            release_date="2022-09-14",
            metron_id=52529,
            source="agent_win",
        ),
    ])
    before = cache.load()

    cmd_collection_audit_unscoped_lookup(cache=cache)

    after = cache.load()
    assert after == before


def test_command_empty_store_flags_nothing(tmp_path):
    cache = _make_cache(tmp_path)
    result = cmd_collection_audit_unscoped_lookup(cache=cache)

    assert result["row_count"] == 0
    assert result["agent_win_count"] == 0
    assert result["flagged_count"] == 0
    assert result["flagged_rows"] == []


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_parses_audit_unscoped_lookup_subcommand():
    from locg.cli import create_parser

    parser = create_parser()
    args = parser.parse_args(["collection", "audit-unscoped-lookup"])
    assert args.command == "collection"
    assert args.collection_command == "audit-unscoped-lookup"


def test_cli_dispatches_without_client(monkeypatch, tmp_path, capsys):
    """Mirrors the other local collection subcommands (status/doctor/
    audit-pending): a pure local-store read never constructs a Playwright/
    LOCG client."""
    import sys

    import locg.cli

    client_constructed = []

    class FakeClient:
        def __init__(self):
            client_constructed.append(True)
        def close(self):
            pass

    calls = []

    def fake_audit(cache=None):
        calls.append(True)
        return {"row_count": 0, "agent_win_count": 0, "flagged_count": 0, "flagged_rows": []}

    monkeypatch.setattr(locg.cli, "LOCGClient", FakeClient)
    monkeypatch.setattr(locg.cli, "cmd_collection_audit_unscoped_lookup", fake_audit)
    monkeypatch.setattr(sys, "argv", ["locg", "collection", "audit-unscoped-lookup"])

    try:
        locg.cli.main()
    except SystemExit as e:
        assert e.code in (None, 0)

    assert not client_constructed, "audit-unscoped-lookup must not construct LOCGClient"
    assert calls == [True]
    import json
    assert json.loads(capsys.readouterr().out)["flagged_count"] == 0
