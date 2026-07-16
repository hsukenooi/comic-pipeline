"""Unit tests for record_win_prep.py (BUI-352/353/354)."""

import json
import subprocess
import sys
import os

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from record_win_prep import (
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


def test_entries_for_win_omits_null_year_and_empty_variant():
    win = _snipe("1")
    identity = _identity(series="Ghost Rider", issue="1", year=None, variant_text="")
    entries, review = entries_for_win(win, identity)
    assert review is None
    assert "year" not in entries[0]["identify_data"]
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
