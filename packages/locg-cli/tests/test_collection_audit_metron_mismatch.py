"""Tests for `locg collection audit-metron-mismatch` (BUI-501, a BUI-500
follow-up).

BUI-500's data fix cleared a wrong `metron_id` (52529) on a row whose date
was CORRECT (Godzilla: The Half-Century War #1, 2012-08-08). BUI-493's local,
date-only audit (`audit-unscoped-lookup`) cannot catch this class: its
predicate flags rows whose release_date year falls OUTSIDE the resolved
series' window, but here the date is INSIDE the window — only the metron_id
is wrong. This detector closes that gap the only way it can be closed: a
LIVE Metron lookup per stamped metron_id, comparing Metron's own cover year
for that id against the row's own release_date year.

CI has no live Metron access, so every test here passes a fake Metron client
(mirrors test_record_win_era_evidence.py's FakeMetron pattern) — the real
MetronClient.lookup_issue_by_id is covered separately in test_metron.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from locg.collection_cache import CollectionCache
from locg.commands import cmd_collection_audit_metron_mismatch
from locg.metron import MetronCredentialError


def _make_cache(tmp_path: Path) -> CollectionCache:
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    *,
    series_name: str = "Godzilla: The Half-Century War (2012 - 2013)",
    full_title: str = "Godzilla: The Half-Century War #1",
    release_date: Optional[str] = "2012-08-08",
    metron_id: Optional[int] = 52529,
    gixen_item_id: Optional[str] = "w-1",
) -> dict[str, Any]:
    return {
        "publisher_name": "IDW Publishing",
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
        "source": "agent_win",
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


class FakeMetron:
    """MetronClient stand-in exposing only `lookup_issue_by_id` (BUI-501).

    `responses` maps metron_id (int) -> a detail dict (or None for a miss).
    `credential_error` raises MetronCredentialError on every lookup.
    """

    def __init__(self, responses=None, *, credential_error=False):
        self.responses = responses or {}
        self.credential_error = credential_error
        self.degraded = False
        self.lookups: list[int] = []

    def lookup_issue_by_id(self, metron_id):
        self.lookups.append(metron_id)
        if self.credential_error:
            raise MetronCredentialError("no creds")
        return self.responses.get(metron_id)


def _run(cache, metron, **kwargs):
    return cmd_collection_audit_metron_mismatch(
        cache=cache, metron=metron, requests_per_minute=0, **kwargs
    )


# ---------------------------------------------------------------------------
# Core flag/pass behavior
# ---------------------------------------------------------------------------

def test_flags_a_far_year_row(tmp_path):
    """The BUI-500 fingerprint: metron_id 52529 resolves to a book whose
    cover year (1975) is far from the row's own correct 2012 release_date."""
    cache = _make_cache(tmp_path)
    _seed(cache, [_row()])
    metron = FakeMetron({52529: {
        "metron_id": 52529, "cover_date": "1975-01-01",
        "series_id": 999, "series_name": "Some Unrelated Book",
    }})

    result = _run(cache, metron)

    assert result["row_count"] == 1
    assert result["eligible_count"] == 1
    assert result["checked_count"] == 1
    assert result["flagged_count"] == 1
    [flagged] = result["flagged_rows"]
    assert flagged["full_title"] == "Godzilla: The Half-Century War #1"
    assert flagged["series_name"] == "Godzilla: The Half-Century War (2012 - 2013)"
    assert flagged["metron_id"] == 52529
    assert flagged["release_date"] == "2012-08-08"
    assert flagged["metron_cover_year"] == 1975
    assert flagged["delta_years"] == 2012 - 1975
    assert flagged["gixen_item_id"] == "w-1"
    assert "CollectionCache.apply" in result["remediation_rule"]
    assert "gixen_item_id" in result["remediation_rule"]


def test_passes_a_near_year_row(tmp_path):
    """A genuinely correct row: Metron's cover year for the stamped metron_id
    matches (within tolerance) the row's own release_date year."""
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(metron_id=52530, release_date="2012-09-01")])
    metron = FakeMetron({52530: {
        "metron_id": 52530, "cover_date": "2012-11-01",
        "series_id": 1, "series_name": "Godzilla: The Half-Century War",
    }})

    result = _run(cache, metron)

    assert result["checked_count"] == 1
    assert result["flagged_count"] == 0
    assert result["flagged_rows"] == []


def test_boundary_delta_within_default_tolerance_not_flagged(tmp_path):
    """Default year_tolerance=1: a one-year delta must NOT flag."""
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(metron_id=1, release_date="2012-01-01")])
    metron = FakeMetron({1: {"metron_id": 1, "cover_date": "2013-01-01"}})

    result = _run(cache, metron)

    assert result["flagged_count"] == 0


def test_boundary_delta_beyond_default_tolerance_flagged(tmp_path):
    """Default year_tolerance=1: a two-year delta MUST flag."""
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(metron_id=1, release_date="2012-01-01")])
    metron = FakeMetron({1: {"metron_id": 1, "cover_date": "2014-01-01"}})

    result = _run(cache, metron)

    assert result["flagged_count"] == 1
    assert result["flagged_rows"][0]["delta_years"] == 2


def test_custom_year_tolerance_widens_the_pass_band(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(metron_id=1, release_date="2012-01-01")])
    metron = FakeMetron({1: {"metron_id": 1, "cover_date": "2015-01-01"}})  # delta=3

    result = _run(cache, metron, year_tolerance=5)

    assert result["flagged_count"] == 0


# ---------------------------------------------------------------------------
# Eligibility: missing metron_id / missing or unparseable release_date
# ---------------------------------------------------------------------------

def test_missing_metron_id_is_skipped_not_checked(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(metron_id=None)])
    metron = FakeMetron()

    result = _run(cache, metron)

    assert result["row_count"] == 1
    assert result["eligible_count"] == 0
    assert result["checked_count"] == 0
    assert result["flagged_count"] == 0
    assert metron.lookups == []


def test_missing_release_date_is_skipped_not_checked(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(release_date=None)])
    metron = FakeMetron({52529: {"metron_id": 52529, "cover_date": "1975-01-01"}})

    result = _run(cache, metron)

    assert result["eligible_count"] == 0
    assert result["checked_count"] == 0
    assert metron.lookups == []


def test_unparseable_release_date_is_skipped(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row(release_date="unknown")])
    metron = FakeMetron()

    result = _run(cache, metron)

    assert result["eligible_count"] == 0


def test_metron_miss_is_skipped_never_flagged(tmp_path):
    """An eligible row whose metron_id Metron can't resolve is COUNTED as
    checked but never flagged — fail-closed, no verdict from an uncertain
    call."""
    cache = _make_cache(tmp_path)
    _seed(cache, [_row()])
    metron = FakeMetron({52529: None})

    result = _run(cache, metron)

    assert result["eligible_count"] == 1
    assert result["checked_count"] == 1
    assert result["flagged_count"] == 0


def test_metron_result_with_no_cover_date_is_skipped(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row()])
    metron = FakeMetron({52529: {"metron_id": 52529, "cover_date": None}})

    result = _run(cache, metron)

    assert result["flagged_count"] == 0


# ---------------------------------------------------------------------------
# Credential error: latch and stop the sweep early
# ---------------------------------------------------------------------------

def test_credential_error_stops_the_sweep_and_leaves_later_rows_unchecked(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(gixen_item_id="w-1", metron_id=1),
        _row(gixen_item_id="w-2", metron_id=2, full_title="Godzilla #2"),
    ])
    metron = FakeMetron(credential_error=True)

    result = _run(cache, metron)

    assert result["eligible_count"] == 2
    assert result["checked_count"] == 0
    assert result["flagged_count"] == 0
    # Only the FIRST row's lookup was even attempted before the credential
    # error latched and stopped the sweep.
    assert len(metron.lookups) == 1


def test_degraded_client_stops_the_sweep_early(tmp_path):
    """A throttled/unreachable Metron (BUI-255's `degraded` signal) latches
    metron_disabled after the first lookup — the second row's lookup never
    runs (BUI-465 discipline)."""
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(gixen_item_id="w-1", metron_id=1),
        _row(gixen_item_id="w-2", metron_id=2, full_title="Godzilla #2"),
    ])

    class DegradingMetron(FakeMetron):
        def lookup_issue_by_id(self, metron_id):
            result = super().lookup_issue_by_id(metron_id)
            self.degraded = True
            return result

    metron = DegradingMetron({1: None, 2: None})

    result = _run(cache, metron)

    assert result["checked_count"] == 1
    assert len(metron.lookups) == 1


# ---------------------------------------------------------------------------
# --series filter and --limit
# ---------------------------------------------------------------------------

def test_series_filter_restricts_the_sweep(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(series_name="Godzilla: The Half-Century War (2012 - 2013)", metron_id=1, gixen_item_id="w-1"),
        _row(series_name="Some Other Series (1990 - 1995)", metron_id=2, gixen_item_id="w-2",
             full_title="Some Other Series #1"),
    ])
    metron = FakeMetron({1: {"metron_id": 1, "cover_date": "1975-01-01"}})

    result = _run(cache, metron, series="Godzilla: The Half-Century War")

    assert result["row_count"] == 2
    assert result["eligible_count"] == 1
    assert result["checked_count"] == 1
    assert metron.lookups == [1]


def test_limit_caps_eligible_rows_sent_to_metron(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [
        _row(metron_id=1, gixen_item_id="w-1"),
        _row(metron_id=2, gixen_item_id="w-2", full_title="Godzilla #2"),
        _row(metron_id=3, gixen_item_id="w-3", full_title="Godzilla #3"),
    ])
    metron = FakeMetron({
        1: {"metron_id": 1, "cover_date": "2012-01-01"},
        2: {"metron_id": 2, "cover_date": "2012-01-01"},
        3: {"metron_id": 3, "cover_date": "2012-01-01"},
    })

    result = _run(cache, metron, limit=2)

    assert result["eligible_count"] == 3  # eligible is counted BEFORE the cap
    assert result["checked_count"] == 2
    assert len(metron.lookups) == 2


def test_negative_limit_is_ignored(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row()])
    metron = FakeMetron({52529: {"metron_id": 52529, "cover_date": "2012-01-01"}})

    result = _run(cache, metron, limit=-1)

    assert result["checked_count"] == 1


# ---------------------------------------------------------------------------
# Read-only + empty store
# ---------------------------------------------------------------------------

def test_command_is_read_only(tmp_path):
    cache = _make_cache(tmp_path)
    _seed(cache, [_row()])
    before = cache.load()
    metron = FakeMetron({52529: {"metron_id": 52529, "cover_date": "1975-01-01"}})

    _run(cache, metron)

    after = cache.load()
    assert after == before


def test_empty_store_flags_nothing_and_calls_metron_never(tmp_path):
    cache = _make_cache(tmp_path)
    metron = FakeMetron()

    result = _run(cache, metron)

    assert result["row_count"] == 0
    assert result["eligible_count"] == 0
    assert result["checked_count"] == 0
    assert result["flagged_count"] == 0
    assert metron.lookups == []


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_parses_audit_metron_mismatch_subcommand():
    from locg.cli import create_parser

    parser = create_parser()
    args = parser.parse_args([
        "collection", "audit-metron-mismatch",
        "--series", "Godzilla", "--limit", "10", "--year-tolerance", "2",
    ])
    assert args.command == "collection"
    assert args.collection_command == "audit-metron-mismatch"
    assert args.series == "Godzilla"
    assert args.limit == 10
    assert args.year_tolerance == 2


def test_cli_dispatches_without_client(monkeypatch):
    """Mirrors the other local collection subcommands: a pure local-store
    read (+ Metron over its own HTTP client) never constructs a
    Playwright/LOCG client."""
    import sys

    import locg.cli

    client_constructed = []

    class FakeClient:
        def __init__(self):
            client_constructed.append(True)
        def close(self):
            pass

    calls = []

    def fake_audit(**kwargs):
        calls.append(kwargs)
        return {
            "row_count": 0, "eligible_count": 0, "checked_count": 0,
            "flagged_count": 0, "flagged_rows": [], "remediation_rule": "x",
        }

    monkeypatch.setattr(locg.cli, "LOCGClient", FakeClient)
    monkeypatch.setattr(locg.cli, "cmd_collection_audit_metron_mismatch", fake_audit)
    monkeypatch.setattr(sys, "argv", ["locg", "collection", "audit-metron-mismatch"])

    try:
        locg.cli.main()
    except SystemExit as e:
        assert e.code in (None, 0)

    assert not client_constructed, "audit-metron-mismatch must not construct LOCGClient"
    assert len(calls) == 1
    assert calls[0]["series"] is None
    assert calls[0]["limit"] is None
    assert calls[0]["year_tolerance"] == 1
