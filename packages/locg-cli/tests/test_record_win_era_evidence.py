"""BUI-498: cmd_collection_record_win_era_evidence — the null-year auto-record
era gate.

A null-year win auto-records ONLY when the issue's Metron cover year lands
inside the resolved volume's INDEPENDENT publication window; every ambiguous or
failed path holds (era_confirmed=False), degrading onto BUI-475's hold-all. The
four acceptance cases from the ticket are covered explicitly:
  (a) genuine in-window null-year win  -> confirmed
  (b) sole-owned WRONG-era (BUI-421 FF#16 shape) -> HELD
  (c) multiple-owned-volume / ambiguous -> HELD (and never spends a Metron call)
  (d) Metron unavailable / miss / credential error / degraded -> HELD
"""
from __future__ import annotations

from locg.commands import cmd_collection_record_win_era_evidence
from locg.metron import MetronCredentialError


class FakeCache:
    """Only ``load()`` is used by the era-evidence path."""

    def __init__(self, payload):
        self._payload = payload

    def load(self):
        return self._payload


def _payload(series_name_index, export_series_names):
    """A minimal collection payload: an explicit one-to-one ``series_name_index``
    plus the ``locg_export`` rows ``build_volume_candidates`` derives the
    multi-volume map from (source='locg_export' only, R61)."""
    return {
        "series_name_index": series_name_index,
        "comics": [
            {"source": "locg_export", "series_name": sn, "in_collection": 1}
            for sn in export_series_names
        ],
    }


class FakeMetron:
    """MetronClient stand-in for the era-evidence path.

    ``responses`` maps issue-number (str) -> metron_data dict (or None for a
    miss). ``credential_error`` raises MetronCredentialError on every lookup.
    ``cost`` is what ``lookup_issue_request_cost`` reports.
    """

    def __init__(self, responses=None, *, credential_error=False, cost=2):
        self.responses = responses or {}
        self.credential_error = credential_error
        self.cost = cost
        self.degraded = False
        self.lookups: list[tuple] = []
        self.cost_calls: list[str] = []

    def lookup_issue_request_cost(self, series_query, year=None):
        self.cost_calls.append(series_query)
        return self.cost

    def lookup_issue(self, series_query, issue_number, year=None):
        self.lookups.append((series_query, str(issue_number), year))
        if self.credential_error:
            raise MetronCredentialError("no creds")
        return self.responses.get(str(issue_number))


def _run(items, payload, metron, requests_per_minute=0):
    return cmd_collection_record_win_era_evidence(
        items,
        cache=FakeCache(payload),
        metron=metron,
        requests_per_minute=requests_per_minute,
    )["results"]


# ---------------------------------------------------------------------------
# (a) genuine in-window null-year win -> confirmed
# ---------------------------------------------------------------------------


def test_null_year_in_window_confirms():
    payload = _payload(
        {"ghost rider": "Ghost Rider (1990 - 1998)"},
        ["Ghost Rider (1990 - 1998)"],
    )
    metron = FakeMetron({"25": {"cover_date": "1992-05-01", "store_date": None}})
    results = _run(
        [{"item_id": "a", "series": "Ghost Rider", "issue": "25"}], payload, metron
    )
    assert results == [{"item_id": "a", "era_confirmed": True}]
    assert metron.lookups == [("Ghost Rider", "25", None)]


def test_store_date_gets_pm1_slack_matching_the_commit():
    # store_date one year before the window begin — a January-cover first issue
    # ships the prior November; _metron_release_date gives store_date the same
    # ±1 slack the record-win commit uses, so this confirms rather than holds.
    payload = _payload(
        {"ghost rider": "Ghost Rider (1990 - 1998)"},
        ["Ghost Rider (1990 - 1998)"],
    )
    metron = FakeMetron({"1": {"cover_date": "1990-01-01", "store_date": "1989-11-01"}})
    results = _run(
        [{"item_id": "i", "series": "Ghost Rider", "issue": "1"}], payload, metron
    )
    assert results == [{"item_id": "i", "era_confirmed": True}]


def test_edition_qualifier_applied_to_series():
    # An Annual win must resolve against the DISTINCT "<Series> Annual" identity
    # (BUI-426), the same series text the record-win commit will use — so the
    # Metron lookup is issued for "Fantastic Four Annual", not bare "Fantastic
    # Four".
    payload = _payload(
        {"fantastic four annual": "Fantastic Four Annual (1963 - 2001)"},
        ["Fantastic Four Annual (1963 - 2001)"],
    )
    metron = FakeMetron({"5": {"cover_date": "1967-11-01"}})
    results = _run(
        [{"item_id": "j", "series": "Fantastic Four", "issue": "5", "edition": "annual"}],
        payload,
        metron,
    )
    assert results == [{"item_id": "j", "era_confirmed": True}]
    assert metron.lookups == [("Fantastic Four Annual", "5", None)]


# ---------------------------------------------------------------------------
# (b) sole-owned WRONG-era null-year win -> HELD (the BUI-421 mis-file)
# ---------------------------------------------------------------------------


def test_sole_owned_wrong_era_holds():
    # The BUI-421 case: the collection owns ONLY a modern Fantastic Four volume,
    # but the win is the $233 vintage FF #16 (cover-dated 1963).
    # resolve_series_for_win returns the sole owned (wrong-era) volume
    # unconditionally — the fail-open this gate exists to close.
    payload = _payload(
        {"fantastic four": "Fantastic Four (Vol. 3) (1998 - 2003)"},
        ["Fantastic Four (Vol. 3) (1998 - 2003)"],
    )
    metron = FakeMetron({"16": {"cover_date": "1963-07-01", "store_date": None}})
    results = _run(
        [{"item_id": "b", "series": "Fantastic Four", "issue": "16"}], payload, metron
    )
    assert results == [{"item_id": "b", "era_confirmed": False}]
    # Metron WAS consulted (a sole volume resolved locally), but its cover year
    # placed the issue outside the window -> HOLD, never auto-record.
    assert metron.lookups == [("Fantastic Four", "16", None)]


# ---------------------------------------------------------------------------
# (c) multiple-owned-volume / ambiguous -> HELD, and no Metron call at all
# ---------------------------------------------------------------------------


def test_ambiguous_multi_volume_holds_without_metron_call():
    payload = _payload(
        {"daredevil": "Daredevil (Vol. 2) (1998 - 2011)"},  # last-writer index
        [
            "Daredevil (Vol. 1) (1964 - 1998)",
            "Daredevil (Vol. 2) (1998 - 2011)",
        ],
    )
    metron = FakeMetron({"1": {"cover_date": "1964-04-01"}})
    results = _run(
        [{"item_id": "c", "series": "Daredevil", "issue": "1"}], payload, metron
    )
    assert results == [{"item_id": "c", "era_confirmed": False}]
    # resolve_series_for_win returns None (>1 volume, no year to disambiguate,
    # BUI-421 Fix A), so the LOCAL gate short-circuits and never spends a
    # Metron request.
    assert metron.lookups == []


def test_unknown_series_holds_without_metron_call():
    metron = FakeMetron({"5": {"cover_date": "1975-01-01"}})
    results = _run(
        [{"item_id": "e", "series": "Nonesuch", "issue": "5"}], _payload({}, []), metron
    )
    assert results == [{"item_id": "e", "era_confirmed": False}]
    assert metron.lookups == []


def test_sole_volume_no_parseable_window_holds_without_metron_call():
    # A sole owned volume with no "(YYYY - YYYY)" decoration has no independent
    # window to gate against — gating against a Metron-derived window would be
    # tautological (BUI-496), so this HOLDS and never asks Metron.
    payload = _payload({"some indie": "Some Indie"}, ["Some Indie"])
    metron = FakeMetron({"3": {"cover_date": "1999-01-01"}})
    results = _run(
        [{"item_id": "f", "series": "Some Indie", "issue": "3"}], payload, metron
    )
    assert results == [{"item_id": "f", "era_confirmed": False}]
    assert metron.lookups == []


# ---------------------------------------------------------------------------
# (d) Metron unavailable -> HELD
# ---------------------------------------------------------------------------


def test_metron_miss_holds():
    payload = _payload(
        {"ghost rider": "Ghost Rider (1990 - 1998)"}, ["Ghost Rider (1990 - 1998)"]
    )
    metron = FakeMetron({})  # lookup returns None
    results = _run(
        [{"item_id": "d", "series": "Ghost Rider", "issue": "25"}], payload, metron
    )
    assert results == [{"item_id": "d", "era_confirmed": False}]
    assert metron.lookups == [("Ghost Rider", "25", None)]


def test_credential_error_holds_this_and_every_later_win():
    payload = _payload(
        {
            "ghost rider": "Ghost Rider (1990 - 1998)",
            "spawn": "Spawn (1992 - 1999)",
        },
        ["Ghost Rider (1990 - 1998)", "Spawn (1992 - 1999)"],
    )
    metron = FakeMetron(
        {"25": {"cover_date": "1992-01-01"}, "1": {"cover_date": "1992-05-01"}},
        credential_error=True,
    )
    items = [
        {"item_id": "g1", "series": "Ghost Rider", "issue": "25"},
        {"item_id": "g2", "series": "Spawn", "issue": "1"},
    ]
    results = _run(items, payload, metron)
    assert results == [
        {"item_id": "g1", "era_confirmed": False},
        {"item_id": "g2", "era_confirmed": False},
    ]
    # The credential error latched metron_disabled after the first win, so the
    # second win never called Metron.
    assert len(metron.lookups) == 1


def test_degraded_holds_this_and_every_later_win():
    payload = _payload(
        {
            "ghost rider": "Ghost Rider (1990 - 1998)",
            "spawn": "Spawn (1992 - 1999)",
        },
        ["Ghost Rider (1990 - 1998)", "Spawn (1992 - 1999)"],
    )

    class DegradingMetron(FakeMetron):
        def lookup_issue(self, series_query, issue_number, year=None):
            self.lookups.append((series_query, str(issue_number), year))
            self.degraded = True  # throttled/unreachable after this call
            return None

    metron = DegradingMetron({})
    items = [
        {"item_id": "h1", "series": "Ghost Rider", "issue": "25"},
        {"item_id": "h2", "series": "Spawn", "issue": "1"},
    ]
    results = _run(items, payload, metron)
    assert all(r["era_confirmed"] is False for r in results)
    # Degradation latched after the first lookup — the batch does not keep
    # hammering a throttled Metron (BUI-465), it just holds the rest.
    assert len(metron.lookups) == 1


# ---------------------------------------------------------------------------
# Shape / pacing
# ---------------------------------------------------------------------------


def test_empty_items_returns_empty_results():
    assert cmd_collection_record_win_era_evidence(
        [], cache=FakeCache(_payload({}, [])), metron=FakeMetron(), requests_per_minute=0
    ) == {"results": []}


def test_pacing_charges_previous_win_cost_and_reuses_series_cache(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("locg.commands.time.sleep", slept.append)
    payload = _payload(
        {"ghost rider": "Ghost Rider (1990 - 1998)"}, ["Ghost Rider (1990 - 1998)"]
    )

    class CostMetron(FakeMetron):
        # First same-series lookup pays both halves (2), the second reuses the
        # cached series handle and pays only issue_in_series (1) — BUI-473.
        def lookup_issue_request_cost(self, series_query, year=None):
            self.cost_calls.append(series_query)
            return 2 if len(self.cost_calls) == 1 else 1

    metron = CostMetron(
        {"25": {"cover_date": "1992-01-01"}, "26": {"cover_date": "1992-02-01"}}
    )
    items = [
        {"item_id": "p1", "series": "Ghost Rider", "issue": "25"},
        {"item_id": "p2", "series": "Ghost Rider", "issue": "26"},
    ]
    res = cmd_collection_record_win_era_evidence(
        items, cache=FakeCache(payload), metron=metron, requests_per_minute=60.0
    )["results"]
    assert [r["era_confirmed"] for r in res] == [True, True]
    # 60 req/min -> 1s/request. Win 1 pays nothing up front; win 2 pays win 1's
    # cost (2 -> 2.0s); the batch does not trail-sleep after the final lookup.
    assert slept == [2.0]


def test_open_ended_present_window_confirms_modern_issue():
    # A "(YYYY - Present)" volume parses to an open-ended window (end sentinel
    # 9999); a modern null-year issue whose cover year is >= begin confirms.
    payload = _payload(
        {"the walking dead": "The Walking Dead (2003 - Present)"},
        ["The Walking Dead (2003 - Present)"],
    )
    metron = FakeMetron({"193": {"cover_date": "2019-07-01", "store_date": None}})
    results = _run(
        [{"item_id": "w", "series": "The Walking Dead", "issue": "193"}], payload, metron
    )
    assert results == [{"item_id": "w", "era_confirmed": True}]
