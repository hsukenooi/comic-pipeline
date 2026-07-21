"""Unit tests for record_win_prep.py (BUI-352/353/354)."""

import json
import subprocess
import sys
import os

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from record_win_prep import (
    REASON_MISSING_YEAR,
    REASON_NULL_SERIES_OR_ISSUE,
    REASON_UNPARSEABLE_LOT,
    RecordWinPrepError,
    build_payload,
    entries_for_win,
    fetch_seen_ids,
    filter_ended_won,
    identify_titles,
    subtract_seen,
)


def _snipe(item_id, title="Test Comic #1", status="WON", time_to_end="ENDED",
           current_bid="10.00 USD", end_date_iso="2026-05-24T18:14:48+00:00"):
    return {
        "item_id": item_id,
        "title": title,
        "status": status,
        "time_to_end": time_to_end,
        "current_bid": current_bid,
        "end_date_iso": end_date_iso,
    }


def _identity(series="Ghost Rider", issue="1", year=1973, is_lot=False,
              constituent_issues=None, variant_text="", error=None):
    d = {
        "series": series,
        "issue": issue,
        "year": year,
        "is_lot": is_lot,
        "constituent_issues": constituent_issues or [],
        "variant_text": variant_text,
    }
    if error is not None:
        d["error"] = error
    return d


# ---------------------------------------------------------------------------
# filter_ended_won
# ---------------------------------------------------------------------------


def test_filter_ended_won_excludes_live_snipes():
    snipes = [_snipe("1", status="WON", time_to_end="ENDED"),
              _snipe("2", status="PENDING", time_to_end="2h 3m")]
    result = filter_ended_won(snipes)
    assert [s["item_id"] for s in result] == ["1"]


def test_filter_ended_won_requires_won_status():
    snipes = [_snipe("1", status="WON"), _snipe("2", status="LOST"),
              _snipe("3", status="FAILED")]
    result = filter_ended_won(snipes)
    assert [s["item_id"] for s in result] == ["1"]


def test_filter_ended_won_status_case_insensitive():
    snipes = [_snipe("1", status="won")]
    result = filter_ended_won(snipes)
    assert len(result) == 1


def test_filter_ended_won_dedups_by_item_id_keeping_first():
    snipes = [_snipe("1", title="first"), _snipe("1", title="second")]
    result = filter_ended_won(snipes)
    assert len(result) == 1
    assert result[0]["title"] == "first"


def test_filter_ended_won_skips_missing_item_id():
    snipes = [{"status": "WON", "time_to_end": "ENDED", "title": "no id"}]
    assert filter_ended_won(snipes) == []


# ---------------------------------------------------------------------------
# subtract_seen
# ---------------------------------------------------------------------------


def test_subtract_seen_removes_seen_ids():
    ended_won = [_snipe("1"), _snipe("2"), _snipe("3")]
    new = subtract_seen(ended_won, {"2"})
    assert [s["item_id"] for s in new] == ["1", "3"]


def test_subtract_seen_empty_seen_set_keeps_all():
    ended_won = [_snipe("1"), _snipe("2")]
    assert subtract_seen(ended_won, set()) == ended_won


# ---------------------------------------------------------------------------
# fetch_seen_ids — BUI-352 hardening
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def get(self, url, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._response


def test_fetch_seen_ids_ok():
    session = _FakeSession(response=_FakeResponse(200, {"item_ids": ["1", "2"]}))
    result = fetch_seen_ids("http://example.test", session=session)
    assert result == {"1", "2"}


def test_fetch_seen_ids_connection_error_hard_stops():
    """BUI-352: a local/connectivity failure must hard-stop, never fall back
    to an empty seen-set (which would silently reprocess every past win)."""
    session = _FakeSession(exc=requests.ConnectionError("refused"))
    with pytest.raises(RecordWinPrepError, match="cannot reach"):
        fetch_seen_ids("http://example.test", session=session)


def test_fetch_seen_ids_timeout_hard_stops():
    session = _FakeSession(exc=requests.Timeout("timed out"))
    with pytest.raises(RecordWinPrepError, match="timed out"):
        fetch_seen_ids("http://example.test", session=session)


def test_fetch_seen_ids_5xx_falls_back_to_empty_set():
    """BUI-352: a genuine server 5xx is the ONE case that safely falls back —
    BUI-34's already-owned dedup on the server is the safety net."""
    session = _FakeSession(response=_FakeResponse(503))
    result = fetch_seen_ids("http://example.test", session=session)
    assert result == set()


def test_fetch_seen_ids_4xx_hard_stops():
    """A 4xx is neither a connectivity failure nor a genuine 5xx — falling
    back here could mask a real bug, so this hard-stops too."""
    session = _FakeSession(response=_FakeResponse(404))
    with pytest.raises(RecordWinPrepError, match="404"):
        fetch_seen_ids("http://example.test", session=session)


def test_fetch_seen_ids_unparseable_body_hard_stops():
    session = _FakeSession(response=_FakeResponse(200, body=None))
    with pytest.raises(RecordWinPrepError, match="unparseable"):
        fetch_seen_ids("http://example.test", session=session)


def test_fetch_seen_ids_missing_item_ids_key_hard_stops():
    session = _FakeSession(response=_FakeResponse(200, body={"oops": []}))
    with pytest.raises(RecordWinPrepError):
        fetch_seen_ids("http://example.test", session=session)


@pytest.mark.parametrize("status_code", [500, 599])
def test_fetch_seen_ids_5xx_boundary_falls_back(status_code):
    """Exact range boundaries, not just a representative 503 — an off-by-one
    in `500 <= status_code < 600` would slip past a single-value test."""
    session = _FakeSession(response=_FakeResponse(status_code))
    result = fetch_seen_ids("http://example.test", session=session)
    assert result == set()


def test_fetch_seen_ids_600_is_not_a_5xx_hard_stops():
    """600 is outside the 5xx range (there is no real HTTP 600, but the
    boundary itself must be exact) — must hard-stop, not fall back."""
    session = _FakeSession(response=_FakeResponse(600))
    with pytest.raises(RecordWinPrepError, match="600"):
        fetch_seen_ids("http://example.test", session=session)


def test_fetch_seen_ids_other_request_exception_hard_stops():
    """BUI-352: the hard-stop must cover more than just ConnectionError/Timeout
    — any requests-shaped failure (SSL error, malformed URL, etc.) is still a
    local/connectivity-class failure, not a server 5xx."""
    session = _FakeSession(exc=requests.exceptions.MissingSchema("no schema"))
    with pytest.raises(RecordWinPrepError, match="error contacting"):
        fetch_seen_ids("http://example.test", session=session)


# ---------------------------------------------------------------------------
# identify_titles — subprocess wrapper
# ---------------------------------------------------------------------------


def test_identify_titles_empty_list_skips_subprocess():
    assert identify_titles([]) == []


def test_identify_titles_happy_path(monkeypatch):
    def fake_run(argv, input, capture_output, text, timeout):
        assert argv == ["comic-identify", "--batch"]
        lines = input.strip("\n").split("\n")
        assert lines == ["Title A", "Title B"]
        stdout = "\n".join(
            json.dumps(_identity(series=f"Series {i}")) for i in range(len(lines))
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = identify_titles(["Title A", "Title B"])
    assert len(results) == 2
    assert results[0]["series"] == "Series 0"
    assert results[1]["series"] == "Series 1"


def test_identify_titles_line_count_mismatch_hard_stops(monkeypatch):
    """This is the exact foot-gun BUI-353 exists to eliminate — never trust
    the subprocess blindly, verify the 1:1 alignment."""
    def fake_run(argv, input, capture_output, text, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(_identity()), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RecordWinPrepError, match="1 line"):
        identify_titles(["Title A", "Title B"])


def test_identify_titles_nonzero_exit_hard_stops(monkeypatch):
    def fake_run(argv, input, capture_output, text, timeout):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RecordWinPrepError, match="boom"):
        identify_titles(["Title A"])


def test_identify_titles_binary_not_found_hard_stops(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RecordWinPrepError, match="scripts/install.sh"):
        identify_titles(["Title A"])


def test_identify_titles_other_os_error_hard_stops(monkeypatch):
    """Not just a missing binary — any subprocess launch failure (e.g. a
    permission error) means comic-identify never ran."""
    def fake_run(*a, **k):
        raise PermissionError("not executable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RecordWinPrepError, match="could not launch"):
        identify_titles(["Title A"])


def test_identify_titles_timeout_hard_stops(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="comic-identify", timeout=120)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RecordWinPrepError, match="timed out"):
        identify_titles(["Title A"])


# ---------------------------------------------------------------------------
# entries_for_win — BUI-354 gate is null series/issue only, no confidence
# ---------------------------------------------------------------------------


def test_entries_for_win_clean_parse():
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue="1", variant_text="Newsstand")
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert entries == [{
        "item_id": "1",
        "current_bid": "10.00 USD",
        "end_date_iso": "2026-05-24T18:14:48+00:00",
        "identify_data": {
            "series": "Ghost Rider",
            "issue": "1",
            "year": 1973,
            "variant_text": "Newsstand",
        },
    }]


def test_entries_for_win_omits_empty_variant_text():
    """A year-bearing win still omits an empty variant_text from the built
    entry. (A null year is covered separately below — BUI-475 — since it now
    always gates to needs_review rather than building an entry.)"""
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue="1", variant_text="")
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert "variant_text" not in entries[0]["identify_data"]


def test_entries_for_win_low_confidence_is_not_a_gate():
    """BUI-354: comic-identify's baseline confidence of 0.5 must NOT trigger
    needs_review by itself — only null series/issue does."""
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue="1")
    identity["confidence"] = 0.5
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert len(entries) == 1


def test_entries_for_win_null_series_needs_review():
    win = _snipe("1")
    identity = _identity(series=None, issue="1")
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_NULL_SERIES_OR_ISSUE
    assert review["item_id"] == "1"
    assert review["current_bid"] == "10.00 USD"


def test_entries_for_win_null_issue_needs_review():
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue=None)
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_NULL_SERIES_OR_ISSUE


def test_entries_for_win_error_row_needs_review():
    win = _snipe("1")
    identity = _identity(error="boom")
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert "boom" in review["reason"]


def test_entries_for_win_lot_expands_constituent_issues():
    win = _snipe("1", title="X-Men lot #1-3")
    identity = _identity(series="X-Men", issue=None, is_lot=True,
                          constituent_issues=["1", "2", "3"])
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert [e["identify_data"]["issue"] for e in entries] == ["1", "2", "3"]
    assert all(e["identify_data"]["series"] == "X-Men" for e in entries)
    assert all(e["item_id"] == "1" for e in entries)


def test_entries_for_win_lot_with_no_constituents_needs_review():
    win = _snipe("1", title="Marvel Silver Age Lot")
    identity = _identity(series="Marvel", issue=None, is_lot=True,
                          constituent_issues=[])
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_UNPARSEABLE_LOT


def test_entries_for_win_lot_with_partially_null_constituent_needs_review():
    """A lot where comic-identify only partially parsed the issue list (e.g.
    ["1", None, "3"]) is exactly as untrustworthy as an empty list — must NOT
    silently produce a win entry with a null issue."""
    win = _snipe("1", title="X-Men lot #1, ?, #3")
    identity = _identity(series="X-Men", issue=None, is_lot=True,
                          constituent_issues=["1", None, "3"])
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_UNPARSEABLE_LOT


def test_entries_for_win_lot_dedupes_repeated_issue_numbers():
    """A literal repeated issue number in the title (e.g. a seller typo like
    "100, 100, 101") must not produce two win entries sharing the same
    item_id + issue — that would silently double-record the same book."""
    win = _snipe("1", title="X-Men Lot #100, 100, 101")
    identity = _identity(series="X-Men", issue=None, is_lot=True,
                          constituent_issues=["100", "100", "101"])
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert [e["identify_data"]["issue"] for e in entries] == ["100", "101"]


# ---------------------------------------------------------------------------
# entries_for_win — BUI-426 edition qualifier forwarding
# ---------------------------------------------------------------------------


def test_entries_for_win_forwards_annual_edition():
    """BUI-426: the annual qualifier must survive into identify_data so the
    resolver files "<Series> Annual #N", not the same-numbered regular issue in
    the wrong volume (which falsely claimed a different, valuable book)."""
    win = _snipe("1", title="Uncanny X-men Annual 6 Marvel 1982")
    identity = _identity(series="Uncanny X-Men", issue="6", year=1982)
    identity["edition"] = "annual"
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert entries[0]["identify_data"]["edition"] == "annual"
    assert entries[0]["identify_data"]["series"] == "Uncanny X-Men"


def test_entries_for_win_omits_single_issue_edition():
    """The default "single-issue" edition carries no information — it must NOT
    bloat identify_data (keeps the common-case payload unchanged)."""
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue="1")
    identity["edition"] = "single-issue"
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert "edition" not in entries[0]["identify_data"]


def test_entries_for_win_omits_edition_when_absent():
    """A comic-identify result predating the edition field (or any caller that
    omits it) must still build a clean identify_data with no `edition` key."""
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue="1")  # no edition key
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert "edition" not in entries[0]["identify_data"]


def test_entries_for_win_forwards_giant_size_edition():
    """Giant-size is forwarded too (fidelity); the resolver leaves it in the
    series text since LOCG catalogs it as its own series."""
    win = _snipe("1", title="Giant-Size X-Men 1")
    identity = _identity(series="Giant-Size X-Men", issue="1", year=1975)
    identity["edition"] = "giant-size"
    entries, _ = entries_for_win(win, identity)
    assert entries[0]["identify_data"]["edition"] == "giant-size"


def test_entries_for_win_annual_lot_forwards_edition_per_constituent():
    """Each constituent of an annual lot inherits the edition qualifier."""
    win = _snipe("1", title="X-Men Annual lot 1-2")
    identity = _identity(series="X-Men", issue=None, is_lot=True,
                         constituent_issues=["1", "2"])
    identity["edition"] = "annual"
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert [e["identify_data"]["edition"] for e in entries] == ["annual", "annual"]


# ---------------------------------------------------------------------------
# entries_for_win — BUI-475 missing-year gate (holds ALL null-year wins,
# regardless of price; replaces BUI-422's price-gated version after the
# server-side era-evidence redesign, Option A, was proven to fail open)
# ---------------------------------------------------------------------------


def test_entries_for_win_null_year_cheap_still_needs_review():
    """A cheap null-year win — previously auto-recorded under BUI-422's price
    gate — must now be held for review. Its era can't be confirmed without a
    year, and price is not a reliable proxy for that."""
    win = _snipe("1", current_bid="$5.00")
    identity = _identity(series="Ghost Rider", issue="1", year=None)
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_MISSING_YEAR
    assert review["item_id"] == "1"


def test_entries_for_win_null_year_expensive_still_needs_review():
    """An expensive null-year win was already held under BUI-422; the outcome
    is unchanged, but now for the year reason alone, not price."""
    win = _snipe("1", current_bid="$999.00")
    identity = _identity(series="FANTASTIC FOUR", issue="1", year=None)
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_MISSING_YEAR
    assert review["identity"]["series"] == "FANTASTIC FOUR"


def test_entries_for_win_null_year_needs_review_numeric_bid():
    """A null year still gates even when current_bid is a plain float/int,
    not just a string — price no longer matters, but the gate must not
    depend on current_bid's shape either."""
    win = _snipe("1", current_bid=233.0)
    identity = _identity(series="Amazing Spider-Man", issue="89", year=None)
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_MISSING_YEAR


def test_entries_for_win_null_year_needs_review_missing_or_unparseable_price():
    """A null year still gates even when current_bid is missing or an
    unparseable shape — the gate no longer looks at price at all."""
    win = _snipe("1", current_bid=None)
    identity = _identity(series="Ghost Rider", issue="1", year=None)
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_MISSING_YEAR


def test_entries_for_win_year_present_records_regardless_of_price():
    """A year-bearing win still records normally, even at a high price — the
    gate only ever fires on a null year."""
    win = _snipe("1", current_bid="$999.00")
    identity = _identity(series="Ghost Rider", issue="1", year=1973)
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert entries[0]["identify_data"]["year"] == 1973


def test_entries_for_win_lot_null_year_needs_review_regardless_of_price():
    """The BUI-475 gate applies to the lot path too — a lot's constituent
    issues parsed fine, but a null year still holds it for review before
    expanding into per-issue win entries, cheap or expensive."""
    for current_bid in ("$10.00", "$150.00"):
        win = _snipe("1", title="X-Men lot #1-3", current_bid=current_bid)
        identity = _identity(series="X-Men", issue=None, year=None, is_lot=True,
                              constituent_issues=["1", "2", "3"])
        entries, review = entries_for_win(win, identity)
        assert entries == []
        assert review["reason"] == REASON_MISSING_YEAR


def test_entries_for_win_lot_unparseable_lot_takes_priority_over_year_gate():
    """When a lot's constituents are unparseable AND its year is null, the
    existing REASON_UNPARSEABLE_LOT gate must still fire — there's no need to
    (and no benefit to) reclassifying that as a missing-year review."""
    win = _snipe("1", title="Marvel Silver Age Lot", current_bid="$150.00")
    identity = _identity(series="Marvel", issue=None, year=None, is_lot=True,
                          constituent_issues=[])
    entries, review = entries_for_win(win, identity)
    assert entries == []
    assert review["reason"] == REASON_UNPARSEABLE_LOT


# ---------------------------------------------------------------------------
# build_payload — the single entry point
# ---------------------------------------------------------------------------


def test_build_payload_no_ended_won_at_all():
    snipes = [_snipe("1", status="PENDING", time_to_end="1h")]
    payload = build_payload(
        snipes, "http://example.test",
        fetch_seen=lambda url: set(), identify=lambda titles: [],
    )
    assert payload["total_ended_won"] == 0
    assert payload["new_win_count"] == 0
    assert payload["wins"] == []
    assert payload["needs_review"] == []


def test_build_payload_all_wins_already_seen():
    snipes = [_snipe("1")]
    payload = build_payload(
        snipes, "http://example.test",
        fetch_seen=lambda url: {"1"}, identify=lambda titles: [],
    )
    assert payload["total_ended_won"] == 1
    assert payload["new_win_count"] == 0
    assert payload["wins"] == []


def test_build_payload_happy_path_builds_wins_and_skips_identify_call_count():
    snipes = [_snipe("1", title="Ghost Rider #1"), _snipe("2", title="???")]
    seen_calls = []

    def fake_identify(titles):
        seen_calls.append(titles)
        return [_identity(series="Ghost Rider", issue="1"), _identity(series=None, issue=None)]

    payload = build_payload(
        snipes, "http://example.test",
        fetch_seen=lambda url: set(), identify=fake_identify,
    )
    assert payload["total_ended_won"] == 2
    assert payload["new_win_count"] == 2
    assert len(payload["wins"]) == 1
    assert payload["wins"][0]["item_id"] == "1"
    assert len(payload["needs_review"]) == 1
    assert payload["needs_review"][0]["item_id"] == "2"
    # identify is called exactly once, with both new-win titles in order —
    # this is the "ONE batch call" BUI-353 requires.
    assert seen_calls == [["Ghost Rider #1", "???"]]


def test_build_payload_propagates_hard_stop_from_fetch_seen():
    snipes = [_snipe("1")]

    def failing_fetch(url):
        raise RecordWinPrepError("boom")

    with pytest.raises(RecordWinPrepError):
        build_payload(snipes, "http://example.test", fetch_seen=failing_fetch,
                       identify=lambda titles: [])
