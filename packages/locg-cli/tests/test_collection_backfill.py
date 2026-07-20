"""Tests for `locg collection backfill` (BUI-461).

Remediates ALREADY-STORED pending `agent_win` rows that the record-win producer
wrote before BUI-458 (null `publisher_name`) and BUI-210 (a `YYYY-01-01`
placeholder `release_date`). The fixture set mirrors the live 2026-07-19
`/comic:collection-sync` backlog: null publishers, Jan-1 placeholders, GENUINE
January cover dates that must survive untouched, and newsstand-variant titles.

The load-bearing invariants under test:

* the export's placeholder test is an INTENT check (`metron_id is None`), so
  carrying the resolved `metron_id` is what lets a genuine January date survive
  export — no fabricated `YYYY-01-02` day is ever written;
* a non-`agent_win` row, a wish twin, and an already-pushed row are never
  touched;
* a Metron miss (or a reprint the era gate rejects) leaves the field alone;
* dry-run is the default and writes nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from locg.collection_cache import CollectionCache
from locg.collection_io import _is_placeholder_release_date
from locg.commands import cmd_collection_backfill


def make_cache(tmp_path: Path) -> CollectionCache:
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    *,
    full_title: str,
    series_name: str,
    release_date: Optional[str] = None,
    publisher_name: Optional[str] = None,
    source: Optional[str] = "agent_win",
    gixen_item_id: Optional[str] = None,
    in_collection: int = 1,
    in_wish_list: int = 0,
    metron_id: Optional[int] = None,
    pushed_to_locg_at: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "publisher_name": publisher_name,
        "series_name": series_name,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": in_collection,
        "in_wish_list": in_wish_list,
        "marked_read": 0,
        "my_rating": None,
        "media_format": None,
        "price_paid": 42.0,
        "date_purchased": "2026-07-16",
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
        "local_added_at": "2026-07-16T00:00:00.000000Z",
        "local_added_seq": 1,
        "pushed_to_locg_at": pushed_to_locg_at,
        "last_seen_in_export_at": None,
        "source": source,
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": metron_id,
        "gixen_item_id": gixen_item_id,
        "previous_full_title": None,
    }


def _seed(cache: CollectionCache, rows: list[dict[str, Any]]) -> None:
    """Seed comics AND mark the store imported — the R11 `not_imported` gate."""

    def mutate(payload: dict[str, Any]) -> None:
        payload["comics"] = list(rows)
        payload["last_full_import"] = "2026-06-01T00:00:00.000000Z"

    cache.apply(mutate, command="test-seed")


def _stored(cache: CollectionCache) -> list[dict[str, Any]]:
    return cache.load().get("comics", [])


def _by_title(cache: CollectionCache, title: str) -> dict[str, Any]:
    return next(r for r in _stored(cache) if r["full_title"] == title)


class FakeMetron:
    """Stand-in for MetronClient with the two methods the backfill calls."""

    def __init__(
        self,
        issues: Optional[dict[tuple[str, str], Optional[dict[str, Any]]]] = None,
        details: Optional[dict[int, Optional[dict[str, Any]]]] = None,
    ) -> None:
        self.degraded = False
        self.issues = issues or {}
        self.details = details or {}
        self.lookup_calls: list[tuple[str, str, Any]] = []
        self.detail_calls: list[int] = []

    def lookup_issue(self, series_query: str, issue_number: Any, year: Any = None):
        self.lookup_calls.append((series_query, str(issue_number), year))
        return self.issues.get((series_query, str(issue_number)))

    def lookup_issue_detail(self, metron_id: int):
        self.detail_calls.append(metron_id)
        return self.details.get(metron_id)


def _run(cache: CollectionCache, metron: FakeMetron, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("cadence", 0)
    return cmd_collection_backfill(cache=cache, metron=metron, **kwargs)


# --------------------------------------------------------------------------
# The core case: null publisher + Jan-1 placeholder
# --------------------------------------------------------------------------


def test_backfills_publisher_and_replaces_placeholder_date(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="The Amazing Spider-Man #89",
            series_name="The Amazing Spider-Man (1963 - 1998)",
            release_date="1970-01-01",  # BUI-105 placeholder (metron_id is None)
            gixen_item_id="win-1",
        ),
    ])
    metron = FakeMetron(
        issues={("The Amazing Spider-Man", "89"): {
            "metron_id": 5001, "cover_date": "1970-10-01", "store_date": "1970-07-14",
        }},
        details={5001: {"publisher": "Marvel Comics", "variants": [], "credits": []}},
    )

    result = _run(cache, metron, apply=True)

    assert result["status"] == "ok"
    assert result["updated_count"] == 1
    row = _by_title(cache, "The Amazing Spider-Man #89")
    assert row["publisher_name"] == "Marvel Comics"
    assert row["release_date"] == "1970-07-14"
    assert row["metron_id"] == 5001


def test_never_fabricates_a_january_second_day(tmp_path):
    """A GENUINE January cover date is written as-is; the resolved metron_id is
    what makes the export keep it (`_is_placeholder_release_date` is an intent
    check, not a shape check). No `-01-02` workaround is ever written."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Fantastic Four #16",
            series_name="Fantastic Four (1961 - 1996)",
            release_date="1963-01-01",  # placeholder shape, no metron_id
            gixen_item_id="win-jan",
        ),
    ])
    metron = FakeMetron(
        # Metron's real cover date for this book genuinely IS January 1st.
        issues={("Fantastic Four", "16"): {
            "metron_id": 42, "cover_date": "1963-01-01", "store_date": None,
        }},
        details={42: {"publisher": "Marvel Comics", "variants": [], "credits": []}},
    )

    _run(cache, metron, apply=True)

    row = _by_title(cache, "Fantastic Four #16")
    assert row["release_date"] == "1963-01-01"
    assert row["metron_id"] == 42
    # The whole point: the export no longer blanks it.
    assert _is_placeholder_release_date(row) is False


def test_genuine_january_date_on_a_metron_backed_row_is_never_a_target(tmp_path):
    """A row that already carries a metron_id + Jan-1 date is NOT a placeholder,
    so its date is never re-resolved or overwritten — only its publisher."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Batman #240",
            series_name="Batman (1940 - 2011)",
            release_date="1972-01-01",
            metron_id=777,
            gixen_item_id="win-batman",
        ),
    ])
    metron = FakeMetron(
        issues={("Batman", "240"): {
            "metron_id": 999, "cover_date": "1972-03-01", "store_date": "1972-01-11",
        }},
        details={777: {"publisher": "DC Comics", "variants": [], "credits": []}},
    )

    _run(cache, metron, apply=True)

    row = _by_title(cache, "Batman #240")
    assert row["release_date"] == "1972-01-01"  # untouched
    assert row["metron_id"] == 777  # untouched
    assert row["publisher_name"] == "DC Comics"
    # Publisher came from an exact fetch by the row's own id — no re-resolution.
    assert metron.lookup_calls == []
    assert metron.detail_calls == [777]


def test_newsstand_variant_title_parses_its_issue_number(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="The Incredible Hulk #181 Newsstand Edition",
            series_name="The Incredible Hulk (1968 - 1999)",
            release_date="1974-01-01",
            gixen_item_id="win-hulk",
        ),
    ])
    metron = FakeMetron(
        issues={("The Incredible Hulk", "181"): {
            "metron_id": 181181, "cover_date": "1974-11-01", "store_date": "1974-08-06",
        }},
        details={181181: {"publisher": "Marvel Comics", "variants": [], "credits": []}},
    )

    _run(cache, metron, apply=True)

    assert metron.lookup_calls == [("The Incredible Hulk", "181", "1974")]
    row = _by_title(cache, "The Incredible Hulk #181 Newsstand Edition")
    assert row["release_date"] == "1974-08-06"
    assert row["publisher_name"] == "Marvel Comics"


# --------------------------------------------------------------------------
# Safety envelope: rows this command must never touch
# --------------------------------------------------------------------------


def test_locg_export_row_is_never_touched(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Thor #154",
            series_name="Thor (1966 - 1996)",
            release_date="1968-01-01",
            source="locg_export",
        ),
    ])
    metron = FakeMetron(
        issues={("Thor", "154"): {"metron_id": 1, "cover_date": "1968-07-01", "store_date": None}},
        details={1: {"publisher": "Marvel Comics"}},
    )

    result = _run(cache, metron, apply=True)

    assert result["candidate_count"] == 0
    assert result["updated_count"] == 0
    assert metron.lookup_calls == []
    row = _by_title(cache, "Thor #154")
    assert row["publisher_name"] is None
    assert row["release_date"] == "1968-01-01"


def test_wish_twin_is_never_touched(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="New Gods #7",
            series_name="New Gods (1971 - 1972)",
            release_date="1971-01-01",
            in_collection=0,
            in_wish_list=1,
        ),
    ])
    metron = FakeMetron(
        issues={("New Gods", "7"): {"metron_id": 2, "cover_date": "1971-03-01", "store_date": None}},
        details={2: {"publisher": "DC Comics"}},
    )

    result = _run(cache, metron, apply=True)

    assert result["candidate_count"] == 0
    assert _by_title(cache, "New Gods #7")["publisher_name"] is None


def test_already_pushed_row_is_never_touched(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Daredevil #168",
            series_name="Daredevil (1964 - 1998)",
            release_date="1981-01-01",
            pushed_to_locg_at="2026-07-01T00:00:00.000000Z",
        ),
    ])
    metron = FakeMetron(
        issues={("Daredevil", "168"): {"metron_id": 3, "cover_date": "1981-01-01", "store_date": None}},
    )

    result = _run(cache, metron, apply=True)

    assert result["candidate_count"] == 0
    assert _by_title(cache, "Daredevil #168")["metron_id"] is None


def test_identity_and_copy_count_are_never_mutated(tmp_path):
    cache = make_cache(tmp_path)
    original = _row(
        full_title="X-Factor #6",
        series_name="X-Factor (1986 - 1998)",
        release_date="1986-01-01",
        gixen_item_id="win-xf",
        in_collection=3,
    )
    _seed(cache, [dict(original)])
    metron = FakeMetron(
        issues={("X-Factor", "6"): {"metron_id": 60, "cover_date": "1986-07-01", "store_date": "1986-04-08"}},
        details={60: {"publisher": "Marvel Comics"}},
    )

    _run(cache, metron, apply=True)

    row = _by_title(cache, "X-Factor #6")
    mutable = {"publisher_name", "release_date", "metron_id"}
    # Positive assertions first: without these the loop below would pass
    # vacuously on a command that did nothing at all.
    assert row["publisher_name"] == "Marvel Comics"
    assert row["release_date"] == "1986-04-08"
    assert row["metron_id"] == 60
    for key, value in original.items():
        if key in mutable:
            continue
        assert row[key] == value, f"{key} was mutated"
    assert row["in_collection"] == 3


# --------------------------------------------------------------------------
# Dry-run is the default and writes nothing
# --------------------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_nothing(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
            gixen_item_id="win-ss",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )
    before = cache.path.read_bytes()

    result = _run(cache, metron)

    assert result["status"] == "preview"
    assert result["applied"] is False
    assert result["planned_count"] == 1
    assert result["updated_count"] == 0
    assert result["backup"] is None
    assert cache.path.read_bytes() == before
    row = _by_title(cache, "Silver Surfer #4")
    assert row["publisher_name"] is None
    assert row["release_date"] == "1969-01-01"


def test_dry_run_reports_the_exact_field_diff(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
            gixen_item_id="win-ss",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    result = _run(cache, metron)

    (change,) = result["changes"]
    assert change["full_title"] == "Silver Surfer #4"
    assert change["gixen_item_id"] == "win-ss"
    assert change["fields"]["publisher_name"] == {"from": None, "to": "Marvel Comics"}
    assert change["fields"]["release_date"] == {"from": "1969-01-01", "to": "1968-11-26"}
    assert change["fields"]["metron_id"] == {"from": None, "to": 44}


# --------------------------------------------------------------------------
# Misses: leave the field alone, never fabricate
# --------------------------------------------------------------------------


def test_metron_miss_leaves_both_fields_alone(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="The X-Men #59",
            series_name="The X-Men (1963 - 1981)",
            release_date="1969-01-01",
        ),
    ])
    metron = FakeMetron(issues={})  # no match

    result = _run(cache, metron, apply=True)

    assert result["planned_count"] == 0
    assert result["unresolved"][0]["full_title"] == "The X-Men #59"
    row = _by_title(cache, "The X-Men #59")
    assert row["publisher_name"] is None
    assert row["release_date"] == "1969-01-01"
    assert row["metron_id"] is None


def test_reprint_hit_rejected_by_the_era_gate_writes_nothing(tmp_path):
    """The reprint trap: a naive lookup for a 1969 X-Men issue returns a 2005
    collected edition. The shared `_metron_release_date` guard rejects it, and
    the WHOLE hit is dropped — no date, no metron_id, and no publisher (which
    would have come from the same wrong issue)."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="The X-Men #59",
            series_name="The X-Men (1963 - 1981)",
            release_date="1969-01-01",
        ),
    ])
    metron = FakeMetron(
        issues={("The X-Men", "59"): {
            "metron_id": 9999, "cover_date": "2005-05-01", "store_date": "2005-03-09",
        }},
        details={9999: {"publisher": "Marvel Comics"}},
    )

    result = _run(cache, metron, apply=True)

    assert result["planned_count"] == 0
    assert metron.detail_calls == []
    row = _by_title(cache, "The X-Men #59")
    assert row["release_date"] == "1969-01-01"
    assert row["metron_id"] is None
    assert row["publisher_name"] is None


def test_dateless_row_with_no_year_makes_no_lookup(tmp_path):
    """No year means no era gate, and an ungated lookup is the reprint trap.
    Report it for the documented web fallback instead of guessing."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="The X-Men #12", series_name="The X-Men", release_date=None),
    ])
    metron = FakeMetron(
        issues={("The X-Men", "12"): {"metron_id": 12, "cover_date": "2005-01-01", "store_date": None}},
    )

    result = _run(cache, metron, apply=True)

    assert metron.lookup_calls == []
    assert result["planned_count"] == 0
    assert "no year" in result["unresolved"][0]["reason"]
    assert _by_title(cache, "The X-Men #12")["release_date"] is None


def test_metron_without_a_publisher_leaves_publisher_null(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Weird Book #1",
            series_name="Weird Book (1975 - 1976)",
            release_date="1975-01-01",
        ),
    ])
    metron = FakeMetron(
        issues={("Weird Book", "1"): {"metron_id": 70, "cover_date": "1975-06-01", "store_date": None}},
        details={70: {"publisher": None, "variants": [], "credits": []}},
    )

    result = _run(cache, metron, apply=True)

    row = _by_title(cache, "Weird Book #1")
    assert row["publisher_name"] is None
    assert row["release_date"] == "1975-06-01"
    assert "no publisher" in result["unresolved"][0]["reason"]


# --------------------------------------------------------------------------
# Rate limiting, filters, idempotence, gates
# --------------------------------------------------------------------------


def test_degraded_metron_stops_further_calls(tmp_path):
    """BUI-255/BUI-465: one rate-limit trip latches Metron off for the rest of
    the run rather than repeating a capped-but-real sleep on every row."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="B #1", series_name="B (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="C #1", series_name="C (1970 - 1971)", release_date="1970-01-01"),
    ])

    class Throttling(FakeMetron):
        def lookup_issue(self, series_query, issue_number, year=None):
            super().lookup_issue(series_query, issue_number, year)
            self.degraded = True
            return None

    metron = Throttling()
    result = _run(cache, metron, apply=True)

    assert len(metron.lookup_calls) == 1
    assert result["metron_degraded"] is True
    assert result["planned_count"] == 0


def test_breaker_tripping_mid_row_still_reports_the_missing_publisher(tmp_path):
    """The date lands, the breaker trips, the publisher fetch never happens —
    the row must still say why, or the run reports nothing to retry."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
        ),
    ])

    class TripsAfterLookup(FakeMetron):
        def lookup_issue(self, series_query, issue_number, year=None):
            hit = super().lookup_issue(series_query, issue_number, year)
            self.degraded = True
            return hit

    metron = TripsAfterLookup(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    result = _run(cache, metron, apply=True)

    assert metron.detail_calls == []
    assert result["unresolved"][0]["reason"] == "Metron unavailable before the publisher fetch"
    row = _by_title(cache, "Silver Surfer #4")
    assert row["release_date"] == "1968-11-26"
    assert row["publisher_name"] is None


def test_a_row_needing_only_a_date_makes_no_detail_call(tmp_path):
    """Publisher already present -> the detail fetch is pure waste against a
    ~20 req/min budget. One call, not two."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
            publisher_name="Marvel Comics",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    _run(cache, metron, apply=True)

    assert len(metron.lookup_calls) == 1
    assert metron.detail_calls == []
    assert _by_title(cache, "Silver Surfer #4")["release_date"] == "1968-11-26"


def test_already_degraded_publisher_only_row_is_still_reported(tmp_path):
    """A row that needs no lookup (it has a metron_id) and only wants a
    publisher must still explain itself when the breaker tripped on an EARLIER
    row — otherwise it vanishes from the report with nothing to retry."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        # Row 1 trips the breaker.
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        # Row 2 needs only a publisher and has an exact id to fetch it with.
        _row(
            full_title="Batman #240",
            series_name="Batman (1940 - 2011)",
            release_date="1972-03-01",
            metron_id=777,
        ),
    ])

    class TripsImmediately(FakeMetron):
        def lookup_issue(self, series_query, issue_number, year=None):
            super().lookup_issue(series_query, issue_number, year)
            self.degraded = True
            return None

    metron = TripsImmediately(details={777: {"publisher": "DC Comics"}})
    result = _run(cache, metron, apply=True)

    assert metron.detail_calls == []
    reported = {u["full_title"]: u["reason"] for u in result["unresolved"]}
    assert reported["Batman #240"] == "Metron unavailable before the publisher fetch"
    assert _by_title(cache, "Batman #240")["publisher_name"] is None


def test_filters_narrow_the_candidate_set(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="Thor #154", series_name="Thor (1966 - 1996)", release_date="1968-01-01"),
        _row(full_title="Batman #240", series_name="Batman (1940 - 2011)", release_date="1972-01-01"),
    ])
    metron = FakeMetron()

    result = _run(cache, metron, series="batman")

    assert result["candidate_count"] == 1
    assert metron.lookup_calls == [("Batman", "240", "1972")]

    metron2 = FakeMetron()
    result2 = _run(cache, metron2, full_title="#154")
    assert result2["candidate_count"] == 1
    assert metron2.lookup_calls == [("Thor", "154", "1968")]


def test_limit_caps_rows_per_run_but_reports_the_full_backlog(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="B #1", series_name="B (1970 - 1971)", release_date="1970-01-01"),
    ])
    metron = FakeMetron()

    result = _run(cache, metron, limit=1)

    assert result["candidate_count"] == 2
    assert len(metron.lookup_calls) == 1


def test_rerunning_after_apply_is_a_no_op(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
            gixen_item_id="win-ss",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )
    _run(cache, metron, apply=True)
    after_first = cache.path.read_bytes()

    metron2 = FakeMetron()
    second = _run(cache, metron2, apply=True)

    assert second["status"] == "ok"  # completed, nothing to do — not a "preview"
    assert second["candidate_count"] == 0
    assert second["planned_count"] == 0
    assert second["updated_count"] == 0
    assert metron2.lookup_calls == []
    assert cache.path.read_bytes() == after_first


def test_never_imported_store_is_refused(tmp_path):
    cache = make_cache(tmp_path)

    def mutate(payload):
        payload["comics"] = [
            _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01")
        ]

    cache.apply(mutate, command="test-seed")

    result = _run(cache, FakeMetron(), apply=True)

    assert result["status"] == "not_imported"


def test_apply_takes_a_durable_backup_before_writing(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    result = _run(cache, metron, apply=True, backup_dir=str(tmp_path / "snap"))

    assert result["backup"]["comics_count"] == 1
    snapshot = json.loads((tmp_path / "snap" / "collection.json").read_text())
    # The snapshot is the PRE-write state — that is what makes it a rollback.
    assert snapshot["comics"][0]["release_date"] == "1969-01-01"
    assert snapshot["comics"][0]["publisher_name"] is None


def test_a_failed_backup_refuses_to_write(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    def boom(dest_dir):
        raise RuntimeError("disk full")

    cache.backup_store = boom  # type: ignore[method-assign]
    before = cache.path.read_bytes()

    result = _run(cache, metron, apply=True)

    assert result["status"] == "backup_failed"
    assert result["updated_count"] == 0
    assert cache.path.read_bytes() == before


def _racing_cache(cache: CollectionCache, concurrent) -> None:
    """Make the next `cache.apply` run `concurrent` first, under the same lock
    discipline — a stand-in for another writer landing between this command's
    pre-lock Metron resolution and its write."""
    real_apply = cache.apply

    def racing_apply(mutate_fn, command="unknown", timeout=30.0):
        real_apply(concurrent, command="concurrent-writer")
        return real_apply(mutate_fn, command=command, timeout=timeout)

    cache.apply = racing_apply  # type: ignore[method-assign]


def _surfer_cache(tmp_path: Path) -> tuple[CollectionCache, FakeMetron]:
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
            gixen_item_id="win-ss",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )
    return cache, metron


def test_row_that_left_the_target_set_under_the_lock_is_skipped_whole(tmp_path):
    """A concurrent writer that stamped a metron_id makes the row's Jan-1 date
    GENUINE (`_is_placeholder_release_date` is an intent check), so the row is
    no longer a backfill target and is skipped entirely — the pre-lock plan is
    never forced onto a row that changed meaning."""
    cache, metron = _surfer_cache(tmp_path)

    def concurrent(payload):
        payload["comics"][0]["publisher_name"] = "Marvel Comics (concurrent)"
        payload["comics"][0]["metron_id"] = 12345

    _racing_cache(cache, concurrent)
    result = _run(cache, metron, apply=True)

    row = _by_title(cache, "Silver Surfer #4")
    assert row["publisher_name"] == "Marvel Comics (concurrent)"
    assert row["metron_id"] == 12345
    assert row["release_date"] == "1969-01-01"
    assert result["updated_count"] == 0
    assert "no longer a pending agent_win backfill target" in result["skipped_at_write"][0]["reason"]


def test_field_changed_under_the_lock_is_not_clobbered(tmp_path):
    """A concurrent writer that filled ONLY the publisher must win on that
    field; the still-untouched date field still applies."""
    cache, metron = _surfer_cache(tmp_path)

    def concurrent(payload):
        payload["comics"][0]["publisher_name"] = "Marvel Comics (concurrent)"

    _racing_cache(cache, concurrent)
    result = _run(cache, metron, apply=True)

    row = _by_title(cache, "Silver Surfer #4")
    assert row["publisher_name"] == "Marvel Comics (concurrent)"
    assert row["release_date"] == "1968-11-26"
    assert row["metron_id"] == 44
    assert result["updated_count"] == 1
    assert set(result["changes"][0]["fields"]) == {"release_date", "metron_id"}


def test_ambiguous_duplicate_twins_are_skipped_not_guessed(tmp_path):
    """Two rows sharing an identity and carrying no gixen_item_id (the known
    duplicate-twin shape) cannot be told apart — refuse both rather than write
    to whichever one enumerate() reached first."""
    cache = make_cache(tmp_path)
    twin = dict(
        full_title="Batman #240",
        series_name="Batman (1940 - 2011)",
        release_date="1972-01-01",
        gixen_item_id=None,
    )
    _seed(cache, [_row(**twin), _row(**twin)])
    metron = FakeMetron(
        issues={("Batman", "240"): {"metron_id": 8, "cover_date": "1972-03-01", "store_date": "1972-01-11"}},
        details={8: {"publisher": "DC Comics"}},
    )

    result = _run(cache, metron, apply=True)

    # Both rows planned a change, and BOTH were refused under the lock —
    # asserted by count so an empty skip list can't pass this vacuously.
    assert result["planned_count"] == 2
    assert result["updated_count"] == 0
    assert len(result["skipped_at_write"]) == 2
    assert all("2 rows match" in s["reason"] for s in result["skipped_at_write"])
    assert all(r["publisher_name"] is None for r in _stored(cache))
    assert all(r["release_date"] == "1972-01-01" for r in _stored(cache))


def test_metron_id_is_withheld_when_the_row_regains_a_placeholder_under_the_lock(tmp_path):
    """The BF-1 hole: a plan can carry a metron_id and NO release_date (the row
    had a real date when it was resolved). If a concurrent record-win retry
    rebuilds the row and downgrades that date back to a `{year}-01-01`
    placeholder, writing the metron_id would flip `_is_placeholder_release_date`
    to False and ship the FABRICATED date to LOCG. The per-field staleness check
    cannot catch it — metron_id's own `from` (None) is still accurate."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-08-12",  # a REAL date -> no release_date delta planned
            gixen_item_id="win-ss",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 555, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={555: {"publisher": "Marvel Comics"}},
    )

    def concurrent(payload):
        # A record-win retry rebuilds the row by gixen_item_id with Metron
        # unreachable, downgrading it to a placeholder + null publisher.
        payload["comics"][0]["release_date"] = "1969-01-01"
        payload["comics"][0]["publisher_name"] = None

    _racing_cache(cache, concurrent)
    result = _run(cache, metron, apply=True)

    row = _by_title(cache, "Silver Surfer #4")
    assert row["metron_id"] is None, "a fabricated placeholder must never be blessed"
    assert _is_placeholder_release_date(row) is True
    # The export still blanks it — no fabricated date reaches LOCG.
    from locg.collection_io import _row_to_csv_dict
    assert _row_to_csv_dict(row)["Release Date"] == ""
    assert any("withheld metron_id under the write lock" in s["reason"]
               for s in result["skipped_at_write"])


def test_metron_confirmed_january_date_is_still_blessed_under_the_lock(tmp_path):
    """The mirror of the guard above: when the placeholder-shaped date under the
    lock IS the date Metron confirmed, the metron_id must still be written —
    otherwise the guard would break the genuine-January case it exists beside."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Fantastic Four #16",
            series_name="Fantastic Four (1961 - 1996)",
            release_date="1963-01-01",
            gixen_item_id="win-ff",
        ),
    ])
    metron = FakeMetron(
        issues={("Fantastic Four", "16"): {"metron_id": 42, "cover_date": "1963-01-01", "store_date": None}},
        details={42: {"publisher": "Marvel Comics"}},
    )

    _run(cache, metron, apply=True)

    row = _by_title(cache, "Fantastic Four #16")
    assert row["metron_id"] == 42
    assert _is_placeholder_release_date(row) is False


def test_row_that_vanished_under_the_lock_is_skipped(tmp_path):
    cache, metron = _surfer_cache(tmp_path)

    def concurrent(payload):
        payload["comics"] = []

    _racing_cache(cache, concurrent)
    result = _run(cache, metron, apply=True)

    assert result["updated_count"] == 0
    assert "0 rows match" in result["skipped_at_write"][0]["reason"]


def test_an_empty_backup_refuses_to_write(tmp_path):
    """A backup that captured zero rows is indistinguishable from a broken one
    and must never read as 'the store is safely backed up'."""
    cache, metron = _surfer_cache(tmp_path)
    cache.backup_store = lambda dest: {  # type: ignore[method-assign]
        "backup_path": str(dest), "files": {}, "comics_count": 0, "wish_list_count": 0,
    }
    before = cache.path.read_bytes()

    result = _run(cache, metron, apply=True)

    assert result["status"] == "backup_failed"
    assert "zero rows" in result["error"]
    assert result["updated_count"] == 0
    assert cache.path.read_bytes() == before


def test_publisher_only_row_with_no_metron_id_keeps_its_real_date(tmp_path):
    """The one path that writes a metron_id WITHOUT writing a date: a row with a
    real date, no publisher and no metron_id. Pinned so the tradeoff is
    deliberate — the existing date is left exactly as found."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-08-12",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    result = _run(cache, metron, apply=True)

    row = _by_title(cache, "Silver Surfer #4")
    assert row["release_date"] == "1969-08-12"  # never overwritten
    assert row["publisher_name"] == "Marvel Comics"
    assert row["metron_id"] == 44
    assert set(result["changes"][0]["fields"]) == {"publisher_name", "metron_id"}


def test_dateless_row_with_a_metron_id_gets_its_publisher_and_stays_dateless(tmp_path):
    """A Metron hit that carried no usable date leaves a dateless row WITH an
    id: the publisher is fetchable by exact id, but there is still no year to
    era-gate a date lookup with."""
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Weird Book #1",
            series_name="Weird Book (1975 - 1976)",
            release_date=None,
            metron_id=70,
        ),
    ])
    metron = FakeMetron(details={70: {"publisher": "Marvel Comics"}})

    result = _run(cache, metron, apply=True)

    assert metron.lookup_calls == []
    assert metron.detail_calls == [70]
    row = _by_title(cache, "Weird Book #1")
    assert row["publisher_name"] == "Marvel Comics"
    assert row["release_date"] is None
    assert "no year" in result["unresolved"][0]["reason"]


def test_limit_zero_processes_nothing_but_still_reports_the_backlog(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="B #1", series_name="B (1970 - 1971)", release_date="1970-01-01"),
    ])
    metron = FakeMetron()

    result = _run(cache, metron, apply=True, limit=0)

    assert result["candidate_count"] == 2
    assert result["planned_count"] == 0
    assert metron.lookup_calls == []


def test_plan_time_invariant_guard_withholds_an_unconfirmed_metron_id():
    """Direct coverage of the defensive plan-time guard. It is unreachable
    through the command (accepting a hit requires a confirmed date), so it is
    exercised at the helper — an untested safety net rots silently."""
    from locg.commands import _backfill_resolve_row

    row = _row(
        full_title="Thor #154",
        series_name="Thor (1966 - 1996)",
        release_date="1968-01-01",
        source="locg_export",  # not agent_win -> its Jan-1 date is NOT a placeholder
    )
    metron = FakeMetron(
        issues={("Thor", "154"): {"metron_id": 9, "cover_date": "1968-01-01", "store_date": "1967-11-02"}},
        details={9: {"publisher": "Marvel Comics"}},
    )

    outcome = _backfill_resolve_row(row, metron, False)

    # needs_date is False (locg_export -> not a placeholder), so no release_date
    # delta is planned; the row's Jan-1 date is not the date Metron confirmed
    # (1967-11-02), so the metron_id must be withheld rather than bless it.
    assert "metron_id" not in outcome["fields"]
    assert "withheld metron_id" in outcome["reason"]


def test_writes_an_audit_record(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(
            full_title="Silver Surfer #4",
            series_name="Silver Surfer (1968 - 1970)",
            release_date="1969-01-01",
        ),
    ])
    metron = FakeMetron(
        issues={("Silver Surfer", "4"): {"metron_id": 44, "cover_date": "1969-02-01", "store_date": "1968-11-26"}},
        details={44: {"publisher": "Marvel Comics"}},
    )

    _run(cache, metron, apply=True)

    records = [json.loads(line) for line in cache.audit_path.read_text().splitlines() if line.strip()]
    backfills = [r for r in records if r["type"] == "collection_backfill"]
    assert len(backfills) == 1
    assert backfills[0]["details"]["updated_count"] == 1


@pytest.mark.parametrize("cadence", [0, 0.0])
def test_zero_cadence_makes_no_sleep(tmp_path, monkeypatch, cadence):
    slept: list[float] = []
    monkeypatch.setattr("locg.commands.time.sleep", lambda s: slept.append(s))
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="B #1", series_name="B (1970 - 1971)", release_date="1970-01-01"),
    ])

    cmd_collection_backfill(cache=cache, metron=FakeMetron(), cadence=cadence)

    assert slept == []


def test_cadence_throttles_per_metron_call_not_per_row(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("locg.commands.time.sleep", lambda s: slept.append(s))
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="B #1", series_name="B (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="C #1", series_name="C (1970 - 1971)", release_date="1970-01-01"),
    ])
    # Every row resolves AND fetches detail -> two calls each.
    metron = FakeMetron(
        issues={
            (name, "1"): {"metron_id": i, "cover_date": "1970-06-01", "store_date": None}
            for i, name in enumerate(["A", "B", "C"], start=1)
        },
        details={i: {"publisher": "Marvel Comics"} for i in (1, 2, 3)},
    )

    cmd_collection_backfill(cache=cache, metron=metron, cadence=3.0)

    # Two calls per row must buy two cadence units of sleep, not one.
    assert slept == [6.0, 6.0]


def test_cadence_scales_with_a_single_call_row(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("locg.commands.time.sleep", lambda s: slept.append(s))
    cache = make_cache(tmp_path)
    _seed(cache, [
        _row(full_title="A #1", series_name="A (1970 - 1971)", release_date="1970-01-01"),
        _row(full_title="B #1", series_name="B (1970 - 1971)", release_date="1970-01-01"),
    ])

    # No issue match -> one call per row, no detail fetch.
    cmd_collection_backfill(cache=cache, metron=FakeMetron(), cadence=3.0)

    assert slept == [3.0]


def test_negative_cadence_and_limit_are_refused(tmp_path):
    cache = make_cache(tmp_path)
    _seed(cache, [])

    assert cmd_collection_backfill(cache=cache, metron=FakeMetron(), cadence=-1)["status"] == "invalid_request"
    assert cmd_collection_backfill(
        cache=cache, metron=FakeMetron(), cadence=0, limit=-1
    )["status"] == "invalid_request"
