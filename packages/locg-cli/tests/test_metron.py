"""Tests for the Metron API wrapper (Unit 5)."""
from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from locg.metron import MetronClient, MetronCredentialError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_series(
    id: int = 1,
    display_name: str = "Fantastic Four",
    year_began: int = 1961,
    year_end: int | None = 1996,
) -> MagicMock:
    s = MagicMock()
    s.id = id
    s.display_name = display_name
    s.year_began = year_began
    s.year_end = year_end
    return s


def _mock_issue(
    id: int = 100,
    cover_date: str | None = "1963-01-01",
    store_date: str | None = None,
) -> MagicMock:
    from datetime import date
    i = MagicMock()
    i.id = id
    i.cover_date = date.fromisoformat(cover_date) if cover_date else None
    i.store_date = date.fromisoformat(store_date) if store_date else None
    return i


def _make_client_with_session(series_list=None, issues_list=None) -> tuple[MetronClient, MagicMock]:
    """Return a MetronClient with a pre-wired mock session."""
    client = MetronClient()
    session = MagicMock()
    session.series_list.return_value = series_list if series_list is not None else []
    session.issues_list.return_value = issues_list if issues_list is not None else []
    client._session = session
    return client, session


# ---------------------------------------------------------------------------
# format_series_name
# ---------------------------------------------------------------------------

def test_format_series_name_finite():
    client = MetronClient()
    result = client.format_series_name({
        "series_name": "Fantastic Four",
        "series_year_began": 1961,
        "series_year_end": 1996,
    })
    assert result == "Fantastic Four (1961 - 1996)"


def test_format_series_name_ongoing():
    client = MetronClient()
    result = client.format_series_name({
        "series_name": "Spawn",
        "series_year_began": 1992,
        "series_year_end": None,
    })
    assert result == "Spawn (1992 - Present)"


# ---------------------------------------------------------------------------
# lookup_issue — happy paths
# ---------------------------------------------------------------------------

def test_lookup_issue_returns_expected_dict():
    client, session = _make_client_with_session(
        series_list=[_mock_series(id=1, display_name="Fantastic Four", year_began=1961, year_end=1996)],
        issues_list=[_mock_issue(id=100, cover_date="1963-01-01", store_date=None)],
    )
    result = client.lookup_issue("Fantastic Four", "1")

    assert result is not None
    assert result["metron_id"] == 100
    assert result["cover_date"] == "1963-01-01"
    assert result["store_date"] is None
    assert result["series_year_began"] == 1961
    assert result["series_year_end"] == 1996
    assert result["series_name"] == "Fantastic Four"
    assert result["series_id"] == 1

    session.series_list.assert_called_once_with({"name": "Fantastic Four"})
    session.issues_list.assert_called_once_with({"series_id": 1, "number": "1"})


def test_lookup_issue_with_store_date():
    client, _ = _make_client_with_session(
        series_list=[_mock_series(year_end=None)],
        issues_list=[_mock_issue(cover_date="1992-05-01", store_date="1992-03-15")],
    )
    result = client.lookup_issue("Spawn", "1")
    assert result["cover_date"] == "1992-05-01"
    assert result["store_date"] == "1992-03-15"


def test_lookup_issue_no_cover_date():
    client, _ = _make_client_with_session(
        series_list=[_mock_series()],
        issues_list=[_mock_issue(cover_date=None, store_date=None)],
    )
    result = client.lookup_issue("Something", "1")
    assert result is not None
    assert result["cover_date"] is None
    assert result["store_date"] is None


# ---------------------------------------------------------------------------
# lookup_issue — series disambiguation by publication year (BUI-32)
# ---------------------------------------------------------------------------

def test_lookup_issue_disambiguates_multiple_series_by_year():
    """Multiple series match the name; year picks the one whose range covers it."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(id=1, display_name="The Amazing Spider-Man", year_began=1963, year_end=1998),
            _mock_series(id=2, display_name="The Amazing Spider-Man", year_began=1999, year_end=2012),
            _mock_series(id=3, display_name="The Amazing Spider-Man", year_began=2018, year_end=None),
        ],
        issues_list=[_mock_issue(id=151, cover_date="1975-12-01")],
    )
    result = client.lookup_issue("Amazing Spider-Man", "151", year=1975)

    assert result is not None
    assert result["series_id"] == 1  # the 1963–1998 volume
    # The issue lookup must target the disambiguated series, not series_list[0]
    session.issues_list.assert_called_once_with({"series_id": 1, "number": "151"})


def test_lookup_issue_disambiguates_into_ongoing_series():
    """year_end=None (ongoing) series is matched when year >= year_began."""
    client, _ = _make_client_with_session(
        series_list=[
            _mock_series(id=1, display_name="Daredevil", year_began=1964, year_end=1998),
            _mock_series(id=2, display_name="Daredevil", year_began=2019, year_end=None),
        ],
        issues_list=[_mock_issue(id=5)],
    )
    result = client.lookup_issue("Daredevil", "5", year=2023)
    assert result is not None
    assert result["series_id"] == 2


def test_lookup_issue_ambiguous_without_year_returns_none():
    """Multiple series + no year → cannot disambiguate → None (manual fallback).

    Both candidates share the query's exact (normalized) name — BUI-485's
    name-exactness filter must NOT be what produces the None here, or this
    stops exercising the no-year branch it's named for.
    """
    client, _ = _make_client_with_session(
        series_list=[
            _mock_series(id=1, display_name="Amazing Spider-Man", year_began=1963, year_end=1998),
            _mock_series(id=2, display_name="Amazing Spider-Man", year_began=1999, year_end=2012),
        ],
        issues_list=[_mock_issue()],
    )
    assert client.lookup_issue("Amazing Spider-Man", "151") is None


def test_lookup_issue_ambiguous_year_in_two_ranges_returns_none():
    """Year falls in two overlapping ranges → still ambiguous → None.

    Both candidates share the query's exact (normalized) name, so both
    survive BUI-485's name-exactness filter — this pins the year-window's
    own "still ambiguous" branch (matches has more than one entry), which
    none of the BUI-485 name-filter tests reach (they resolve to 0 or 1
    exact-name survivors).
    """
    client, _ = _make_client_with_session(
        series_list=[
            _mock_series(id=1, display_name="Uncanny X-Men", year_began=1980, year_end=2011),
            _mock_series(id=2, display_name="Uncanny X-Men", year_began=2000, year_end=None),
        ],
        issues_list=[_mock_issue()],
    )
    assert client.lookup_issue("Uncanny X-Men", "200", year=2005) is None


def test_lookup_issue_single_series_ignores_year_mismatch():
    """A sole series match is trusted even if year is outside its range."""
    client, _ = _make_client_with_session(
        series_list=[_mock_series(id=7, year_began=1961, year_end=1996)],
        issues_list=[_mock_issue(id=100)],
    )
    result = client.lookup_issue("Fantastic Four", "1", year=2050)
    assert result is not None
    assert result["series_id"] == 7


# ---------------------------------------------------------------------------
# lookup_issue — name-exactness pre-filter (BUI-485)
#
# Metron's series_list({"name": q}) is a substring (icontains) search, so a
# common masthead like "Batman" returns hundreds of off-topic candidates
# ("Absolute Batman", "Batman Annual", ...). These cover the two measured
# failure shapes from BUI-474's diagnosis: a large decoy-heavy candidate set,
# and the small Annual-sibling case a year window alone can never separate.
# ---------------------------------------------------------------------------

def test_lookup_issue_exact_name_filters_large_decoy_set():
    """The 433-candidate shape: decoys sharing the "Batman" substring must
    not compete with the genuine "Batman" volume just because their year
    range also covers 2003."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(id=1, display_name="Batman (1940)", year_began=1940, year_end=2011),
            _mock_series(id=2, display_name="Absolute Batman", year_began=2024, year_end=None),
            _mock_series(
                id=3, display_name="Tangent Comics / The Batman",
                year_began=1998, year_end=1998,
            ),
            _mock_series(
                id=4, display_name="Punisher / Batman: Deadly Knights",
                year_began=1996, year_end=1996,
            ),
        ],
        issues_list=[_mock_issue(id=240)],
    )
    result = client.lookup_issue("Batman", "1", year=2003)

    assert result is not None
    assert result["series_id"] == 1
    session.issues_list.assert_called_once_with({"series_id": 1, "number": "1"})


def test_lookup_issue_exact_name_separates_annual_sibling():
    """A year window can never separate "Batman" from "Batman Annual" — both
    ran 1940s-2011 — only the exact-name filter can. Batman #240 (1972) is
    the real BUI-474 case: exactly 2 candidates, both survive the year gate."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(id=1, display_name="Batman (1940)", year_began=1940, year_end=2011),
            _mock_series(
                id=2, display_name="Batman Annual (1961)",
                year_began=1961, year_end=2011,
            ),
        ],
        issues_list=[_mock_issue(id=240)],
    )
    result = client.lookup_issue("Batman", "240", year=1972)

    assert result is not None
    assert result["series_id"] == 1
    session.issues_list.assert_called_once_with({"series_id": 1, "number": "240"})


def test_lookup_issue_exact_name_strips_leading_article_both_ways():
    """Query "Amazing Spider-Man" must still exact-match Metron's
    "The Amazing Spider-Man (1963)" — article stripping is two-sided."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=1, display_name="The Amazing Spider-Man (1963)",
                year_began=1963, year_end=1998,
            ),
            _mock_series(
                id=2, display_name="Web of Spider-Man (1985)",
                year_began=1985, year_end=1995,
            ),
        ],
        issues_list=[_mock_issue(id=151)],
    )
    result = client.lookup_issue("Amazing Spider-Man", "151", year=1975)

    assert result is not None
    assert result["series_id"] == 1
    session.issues_list.assert_called_once_with({"series_id": 1, "number": "151"})


def test_lookup_issue_no_exact_name_match_returns_none_not_a_guess():
    """Zero wrong picks: when no candidate's name exact-matches the query —
    even though both fall inside the queried year — the function must return
    None rather than guess between them."""
    client, _ = _make_client_with_session(
        series_list=[
            _mock_series(
                id=1, display_name="Batman Beyond (1999)",
                year_began=1999, year_end=2001,
            ),
            _mock_series(
                id=2, display_name="Batman: Gotham Knights (2000)",
                year_began=2000, year_end=2006,
            ),
        ],
        issues_list=[_mock_issue(id=1)],
    )
    assert client.lookup_issue("Batman", "1", year=2000) is None


# ---------------------------------------------------------------------------
# lookup_issue — Annual/Giant-Size/Special masthead mapping (BUI-487)
#
# Metron sometimes files an Annual under a masthead our data doesn't use --
# "Uncanny X-Men Annual" is Metron's "X-Men Annual (1970)". _ANNUAL_MASTHEAD_TO_METRON
# must translate the query BEFORE both the series_list search and the
# BUI-485 exact-name filter, and a missing/wrong mapping must never resolve
# to a different real volume -- only fail closed to None.
# ---------------------------------------------------------------------------

def test_lookup_issue_maps_annual_masthead_to_metron_naming():
    """Uncanny X-Men Annual #6 must resolve to Metron's X-Men Annual (1970),
    not come back empty just because our masthead differs from Metron's."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=42, display_name="X-Men Annual (1970)",
                year_began=1970, year_end=2007,
            ),
            _mock_series(
                id=99, display_name="Astonishing X-Men Annual (2005)",
                year_began=2005, year_end=2005,
            ),
        ],
        issues_list=[_mock_issue(id=600)],
    )
    result = client.lookup_issue("Uncanny X-Men Annual", "6", year=1982)

    assert result is not None
    assert result["series_id"] == 42
    assert result["series_name"] == "X-Men Annual (1970)"
    # The mapped name -- not the literal query -- is what must reach Metron,
    # both for the search itself and (via the decoy above sharing the
    # "X-Men Annual" substring) for the exact-name filter to pick correctly.
    session.series_list.assert_called_once_with({"name": "X-Men Annual"})
    session.issues_list.assert_called_once_with({"series_id": 42, "number": "6"})


def test_lookup_issue_annual_masthead_mapping_is_case_insensitive():
    """identify_data casing isn't guaranteed; the table lookup must not be
    defeated by it."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=42, display_name="X-Men Annual (1970)",
                year_began=1970, year_end=2007,
            ),
        ],
        issues_list=[_mock_issue(id=600)],
    )
    result = client.lookup_issue("UNCANNY x-men ANNUAL", "6", year=1982)

    assert result is not None
    assert result["series_id"] == 42
    session.series_list.assert_called_once_with({"name": "X-Men Annual"})


def test_lookup_issue_annual_masthead_mapping_strips_leading_article():
    """identify_data isn't guaranteed to omit a leading article either --
    "The Uncanny X-Men Annual" must hit the same table entry as the bare
    "Uncanny X-Men Annual" key (_map_annual_masthead normalizes via
    _normalize_metron_display_name, not a raw casefold)."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=42, display_name="X-Men Annual (1970)",
                year_began=1970, year_end=2007,
            ),
        ],
        issues_list=[_mock_issue(id=600)],
    )
    result = client.lookup_issue("The Uncanny X-Men Annual", "6", year=1982)

    assert result is not None
    assert result["series_id"] == 42
    session.series_list.assert_called_once_with({"name": "X-Men Annual"})


def test_lookup_issue_mapped_masthead_single_correct_candidate_resolves():
    """Pins the intended (common) interaction between the mapping and
    _disambiguate_series's pre-existing len(series_list) == 1 shortcut: when
    Metron's live search for the MAPPED name returns exactly one candidate
    and it genuinely is the right one, the lookup still resolves it (via
    that shortcut, same as any other single-candidate query already would).

    This is deliberately NOT a test of the shortcut's known, pre-existing,
    out-of-scope risk (a single WRONG candidate would also be trusted
    unfiltered by name/year -- see the code comment above
    _ANNUAL_MASTHEAD_TO_METRON) -- that risk belongs to _disambiguate_series
    itself (a separately measured BUI-474/BUI-485 decision this ticket does
    not revisit), not to the mapping table this ticket adds."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=42, display_name="X-Men Annual (1970)",
                year_began=1970, year_end=2007,
            ),
        ],
        issues_list=[_mock_issue(id=600)],
    )
    result = client.lookup_issue("Uncanny X-Men Annual", "6", year=1982)

    assert result is not None
    assert result["series_id"] == 42
    session.series_list.assert_called_once_with({"name": "X-Men Annual"})


def test_lookup_issue_unmapped_masthead_divergence_fails_closed():
    """A masthead with NO table entry is searched verbatim (pass-through).
    Metron's real substring search can return more than one in-window
    candidate for it (hence the second decoy below -- with only one
    candidate, ``_disambiguate_series`` trusts it unfiltered by name, which
    would mask the very failure mode this test targets); the un-translated
    query must fail the exact-name filter against both -- None, never a
    guessed volume."""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=1, display_name="X-Men Annual (1970)",
                year_began=1970, year_end=2007,
            ),
            _mock_series(
                id=2, display_name="Giant-Size X-Men (1975)",
                year_began=1975, year_end=1975,
            ),
        ],
        issues_list=[_mock_issue(id=1)],
    )
    assert client.lookup_issue("Giant-Size X-Men Annual", "1", year=1975) is None
    session.series_list.assert_called_once_with({"name": "Giant-Size X-Men Annual"})


def test_lookup_issue_wrong_masthead_mapping_fails_closed_not_wrong_volume():
    """Even a WRONG table entry can't cause a wrong-volume resolve (AC):
    it only changes what string is searched/exact-matched, and a mapped
    name that doesn't line up with any real candidate's display_name still
    fails the exact-name filter -- None, not series_id 1. (Two candidates,
    same reasoning as above: a lone candidate would bypass the name filter
    entirely and this test wouldn't be exercising the mapping's fail-closed
    behavior at all.)"""
    client, session = _make_client_with_session(
        series_list=[
            _mock_series(
                id=1, display_name="X-Men Annual (1970)",
                year_began=1970, year_end=2007,
            ),
            _mock_series(
                id=2, display_name="Something Else Entirely (1970)",
                year_began=1970, year_end=2007,
            ),
        ],
        issues_list=[_mock_issue(id=1)],
    )
    with patch.dict(
        "locg.metron._ANNUAL_MASTHEAD_TO_METRON",
        {"uncanny x-men annual": "Totally Wrong Series"},
    ):
        assert client.lookup_issue("Uncanny X-Men Annual", "6", year=1982) is None
    session.series_list.assert_called_once_with({"name": "Totally Wrong Series"})


# ---------------------------------------------------------------------------
# lookup_issue — no-match cases return None
# ---------------------------------------------------------------------------

def test_lookup_issue_no_series_match():
    client, _ = _make_client_with_session(series_list=[])
    assert client.lookup_issue("Unknown Series", "1") is None


def test_lookup_issue_no_issue_match():
    client, _ = _make_client_with_session(
        series_list=[_mock_series()],
        issues_list=[],
    )
    assert client.lookup_issue("Fantastic Four", "999") is None


# ---------------------------------------------------------------------------
# lookup_issue — error swallowing
# ---------------------------------------------------------------------------

def test_lookup_issue_swallows_generic_exception():
    client = MetronClient()
    session = MagicMock()
    session.series_list.side_effect = ConnectionError("network down")
    client._session = session
    assert client.lookup_issue("X-Men", "1") is None


def test_lookup_issue_swallows_rate_limit():
    """After a rate limit AND its retry both fail, the caller still gets None."""
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session(
        series_list=[_mock_series()],
    )
    session.issues_list.side_effect = RateLimitError("rate limited", retry_after=60)
    with patch("locg.metron.time.sleep"):
        assert client.lookup_issue("Fantastic Four", "1") is None
    # One retry attempt -> the call happens twice, not just once.
    assert session.issues_list.call_count == 2


def test_lookup_issue_swallows_api_error():
    from mokkari.exceptions import ApiError
    client, session = _make_client_with_session()
    session.series_list.side_effect = ApiError("404 not found")
    assert client.lookup_issue("Nonexistent", "1") is None


# ---------------------------------------------------------------------------
# lookup_issue — rate-limit retry (BUI-260)
# ---------------------------------------------------------------------------

def test_lookup_issue_retries_once_after_rate_limit_and_succeeds():
    """A RateLimitError on the first attempt is retried once and can still succeed."""
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session(
        issues_list=[_mock_issue(id=100)],
    )
    session.series_list.side_effect = [
        RateLimitError("rate limited", retry_after=5),
        [_mock_series(id=1)],
    ]

    with patch("locg.metron.time.sleep") as mock_sleep:
        result = client.lookup_issue("Fantastic Four", "1")

    assert result is not None
    assert result["metron_id"] == 100
    assert session.series_list.call_count == 2
    mock_sleep.assert_called_once_with(5)


def test_lookup_issue_gives_up_after_second_rate_limit(caplog):
    """A RateLimitError on both the original call and the retry gives up -> None.

    Also verifies the wait is capped at _RATE_LIMIT_MAX_SLEEP (60s) even
    though Metron reported a longer retry_after, and that the event is
    logged at WARNING (not the DEBUG level used for a genuine no-match).
    """
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session()
    session.series_list.side_effect = RateLimitError("rate limited", retry_after=90)

    with patch("locg.metron.time.sleep") as mock_sleep:
        with caplog.at_level(logging.WARNING, logger="locg"):
            result = client.lookup_issue("Fantastic Four", "1")

    assert result is None
    assert session.series_list.call_count == 2
    mock_sleep.assert_called_once_with(60)  # capped, not the reported 90
    assert any("rate limit" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# MetronClient.degraded — throttle/timeout signal for batch callers (BUI-255)
# ---------------------------------------------------------------------------

def test_degraded_false_by_default():
    client = MetronClient()
    assert client.degraded is False


def test_degraded_false_on_genuine_no_match():
    """An exception-free 'no match' must NOT trip the degraded flag."""
    client, _ = _make_client_with_session(series_list=[])
    assert client.lookup_issue("Unknown Series", "1") is None
    assert client.degraded is False


def test_degraded_true_after_rate_limit_retry_exhausted():
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session()
    session.series_list.side_effect = RateLimitError("rate limited", retry_after=5)

    with patch("locg.metron.time.sleep"):
        result = client.lookup_issue("Fantastic Four", "1")

    assert result is None
    assert client.degraded is True


def test_degraded_false_after_rate_limit_retry_succeeds():
    """A retry that succeeds must NOT leave the batch breaker tripped."""
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session(
        issues_list=[_mock_issue(id=100)],
    )
    session.series_list.side_effect = [
        RateLimitError("rate limited", retry_after=5),
        [_mock_series(id=1)],
    ]

    with patch("locg.metron.time.sleep"):
        result = client.lookup_issue("Fantastic Four", "1")

    assert result is not None
    assert client.degraded is False


def test_degraded_true_on_connection_error():
    """A transport-level ApiError (mokkari's wrapped ConnectionError/ReadTimeout)
    trips the breaker; a plain data-shape ApiError does not."""
    from mokkari.exceptions import ApiError
    client, session = _make_client_with_session()
    session.series_list.side_effect = ApiError("Connection error: timed out")

    assert client.lookup_issue("Fantastic Four", "1") is None
    assert client.degraded is True


def test_degraded_false_on_non_connection_api_error():
    from mokkari.exceptions import ApiError
    client, session = _make_client_with_session()
    session.series_list.side_effect = ApiError("404 not found")

    assert client.lookup_issue("Fantastic Four", "1") is None
    assert client.degraded is False


def test_degraded_resets_on_next_successful_call():
    """degraded reflects only the MOST RECENT call, not a sticky latch."""
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session(
        series_list=[_mock_series()],
        issues_list=[_mock_issue(id=100)],
    )
    session.series_list.side_effect = RateLimitError("rate limited", retry_after=5)
    with patch("locg.metron.time.sleep"):
        assert client.lookup_issue("Fantastic Four", "1") is None
    assert client.degraded is True

    # A fresh, healthy call clears it.
    session.series_list.side_effect = None
    session.series_list.return_value = [_mock_series(id=1)]
    result = client.lookup_issue("Fantastic Four", "1")
    assert result is not None
    assert client.degraded is False


# ---------------------------------------------------------------------------
# MetronClient.degraded — Metron 5xx server error (BUI-342)
# ---------------------------------------------------------------------------

def _server_error_api_error(status: int = 500) -> Any:
    """Build an ``ApiError`` shaped exactly like mokkari's on a Metron 5xx.

    mokkari's ``_handle_http_response`` does ``raise ApiError(msg) from err``
    where ``err`` is the ``requests`` ``HTTPError`` from ``raise_for_status()``,
    so the ``ApiError.__cause__`` carries ``.response.status_code``. We rebuild
    that exact chain so the test exercises real detection (via ``__cause__``),
    not a hand-set attribute.
    """
    import requests
    from mokkari.exceptions import ApiError

    resp = requests.Response()
    resp.status_code = status
    resp._content = b"Server Error"
    resp.url = "https://metron.cloud/api/issue/"
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as err:
        try:
            raise ApiError(f"HTTP error: {err!r} | Response body: {resp.text}") from err
        except ApiError as api_exc:
            return api_exc
    raise AssertionError("raise_for_status did not raise")  # pragma: no cover


def test_degraded_true_on_server_error_5xx():
    """A Metron 5xx (ApiError wrapping a 500) trips the batch breaker."""
    client, session = _make_client_with_session()
    session.series_list.side_effect = _server_error_api_error(500)

    with patch("locg.metron.time.sleep"):
        assert client.lookup_issue("Fantastic Four", "1") is None
    assert client.degraded is True


def test_server_error_5xx_retries_once_then_succeeds():
    """A single 5xx is retried once and can still succeed with degraded=False."""
    client, session = _make_client_with_session(issues_list=[_mock_issue(id=100)])
    session.series_list.side_effect = [
        _server_error_api_error(503),
        [_mock_series(id=1)],
    ]

    with patch("locg.metron.time.sleep") as mock_sleep:
        result = client.lookup_issue("Fantastic Four", "1")

    assert result is not None
    assert result["metron_id"] == 100
    assert session.series_list.call_count == 2
    assert client.degraded is False
    mock_sleep.assert_called_once()  # the single capped 5xx retry sleep


def test_server_error_5xx_gives_up_after_second_5xx(caplog):
    """A 5xx on both the call and its retry gives up -> None, degraded True,
    and is logged at WARNING (not the DEBUG level of a genuine no-match)."""
    client, session = _make_client_with_session()
    session.series_list.side_effect = _server_error_api_error(500)

    with patch("locg.metron.time.sleep") as mock_sleep:
        with caplog.at_level(logging.WARNING, logger="locg"):
            result = client.lookup_issue("Fantastic Four", "1")

    assert result is None
    assert client.degraded is True
    assert session.series_list.call_count == 2  # one retry
    mock_sleep.assert_called_once()
    assert any("5xx" in rec.message for rec in caplog.records)


def test_degraded_false_on_4xx_api_error():
    """A 4xx (client error, e.g. 404 wrapped with a chained response) is NOT a
    5xx and must NOT trip the breaker — it is a genuine miss, not an outage."""
    client, session = _make_client_with_session()
    session.series_list.side_effect = _server_error_api_error(404)

    assert client.lookup_issue("Fantastic Four", "1") is None
    assert client.degraded is False
    # No retry for a 4xx — it hits the blanket handler and returns None directly.
    assert session.series_list.call_count == 1


def test_degraded_false_on_data_shape_api_error_no_cause():
    """A data-shape ApiError (no chained HTTP response) is not a 5xx — a genuine
    no-match must never look like an outage (the core BUI-342 regression guard)."""
    from mokkari.exceptions import ApiError
    client, session = _make_client_with_session()
    session.series_list.side_effect = ApiError("1 validation error for Issue")

    assert client.lookup_issue("Fantastic Four", "1") is None
    assert client.degraded is False
    assert session.series_list.call_count == 1


def test_is_server_error_detection_matrix():
    """Unit-level: only a 5xx-with-recoverable-status is a server error."""
    from locg.metron import _is_server_error
    from mokkari.exceptions import ApiError

    assert _is_server_error(_server_error_api_error(500)) is True
    assert _is_server_error(_server_error_api_error(599)) is True
    assert _is_server_error(_server_error_api_error(404)) is False
    assert _is_server_error(_server_error_api_error(429)) is False
    assert _is_server_error(ApiError("Connection error: timed out")) is False
    assert _is_server_error(ApiError("no cause")) is False
    assert _is_server_error(ConnectionError("not even an ApiError")) is False


def test_is_server_error_against_real_mokkari_raise_path():
    """Contract guard: detection is coupled to mokkari raising ``ApiError from
    HTTPError`` on a 5xx (the ``__cause__`` chain we read). Drive mokkari's OWN
    ``_handle_http_response`` (not our hand-built chain) so that if a future
    mokkari upgrade stops chaining the HTTPError, this fails LOUDLY here instead
    of silently regressing the breaker to never-trips (the pre-BUI-342 bug)."""
    import mokkari
    import requests
    from locg.metron import _is_server_error

    session = mokkari.api("u", "p", user_agent="locg-cli-test")  # offline; no network at construction
    for status, expected in ((500, True), (503, True), (404, False)):
        resp = requests.Response()
        resp.status_code = status
        resp._content = b"body"
        resp.url = "https://metron.cloud/api/issue/"
        try:
            session._handle_http_response(resp)
            raise AssertionError(f"mokkari did not raise on {status}")
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 — pin whatever mokkari raises
            assert _is_server_error(exc) is expected, (
                f"mokkari 5xx-detection contract broke for {status}: {exc!r}"
            )


def test_5xx_then_rate_limit_on_retry_trips_degraded_no_escape():
    """Cross-class retry (BUI-342): a 5xx that becomes a 429 on the retry must
    trip the breaker and return None — NOT let an exception escape the decorator
    into the batch caller (which only catches MetronCredentialError)."""
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session()
    session.series_list.side_effect = [
        _server_error_api_error(500),
        RateLimitError("rate limited", retry_after=5),
    ]

    with patch("locg.metron.time.sleep"):
        result = client.lookup_issue("Fantastic Four", "1")  # must not raise

    assert result is None
    assert client.degraded is True
    assert session.series_list.call_count == 2


def test_rate_limit_then_5xx_on_retry_trips_degraded_no_escape():
    """The mirror case: a 429 that becomes a 5xx on the retry also trips the
    breaker and returns None rather than escaping the decorator."""
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session()
    session.series_list.side_effect = [
        RateLimitError("rate limited", retry_after=5),
        _server_error_api_error(500),
    ]

    with patch("locg.metron.time.sleep"):
        result = client.lookup_issue("Fantastic Four", "1")  # must not raise

    assert result is None
    assert client.degraded is True
    assert session.series_list.call_count == 2


# ---------------------------------------------------------------------------
# lookup_issue_detail — variant cover names (BUI-33)
# ---------------------------------------------------------------------------

def _mock_variant(name: str) -> MagicMock:
    v = MagicMock()
    v.name = name
    return v


def test_lookup_issue_detail_returns_variant_names():
    client = MetronClient()
    session = MagicMock()
    issue = MagicMock()
    issue.variants = [_mock_variant("Capullo Variant"), _mock_variant("Todd McFarlane Cover")]
    issue.credits = []
    session.issue.return_value = issue
    client._session = session

    result = client.lookup_issue_detail(5)
    assert result == {
        "variants": ["Capullo Variant", "Todd McFarlane Cover"],
        "credits": [],
        # BUI-458: a bare MagicMock issue has no real ``publisher.name`` string,
        # so the isinstance guard keeps publisher null (no mock injection).
        "publisher": None,
    }
    session.issue.assert_called_once_with(5)


def test_lookup_issue_detail_no_variants():
    client = MetronClient()
    session = MagicMock()
    issue = MagicMock()
    issue.variants = []
    issue.credits = []
    session.issue.return_value = issue
    client._session = session
    assert client.lookup_issue_detail(5) == {
        "variants": [],
        "credits": [],
        "publisher": None,
    }


def test_lookup_issue_detail_swallows_exception():
    client = MetronClient()
    session = MagicMock()
    session.issue.side_effect = ConnectionError("down")
    client._session = session
    assert client.lookup_issue_detail(5) is None


def test_lookup_issue_detail_raises_credential_error(monkeypatch):
    monkeypatch.delenv("METRON_USERNAME", raising=False)
    monkeypatch.delenv("METRON_PASSWORD", raising=False)
    client = MetronClient()
    with pytest.raises(MetronCredentialError):
        client.lookup_issue_detail(5)


# ---------------------------------------------------------------------------
# lookup_issue_detail — publisher (BUI-458)
# ---------------------------------------------------------------------------

def _mock_publisher(name: Any) -> MagicMock:
    p = MagicMock()
    p.name = name
    return p


def test_lookup_issue_detail_extracts_publisher():
    """BUI-458: the full Issue's publisher display name is surfaced on the
    detail dict (it already fetches the full Issue, so no extra network call)."""
    client = MetronClient()
    session = MagicMock()
    issue = MagicMock()
    issue.variants = []
    issue.credits = []
    issue.publisher = _mock_publisher("Marvel Comics")
    session.issue.return_value = issue
    client._session = session

    assert client.lookup_issue_detail(5)["publisher"] == "Marvel Comics"


def test_lookup_issue_detail_publisher_null_on_miss_never_guesses():
    """BUI-458 data safety: a Metron issue with no usable publisher yields a
    null publisher (never a fabricated/defaulted value). Covers a missing
    publisher object, a None name, and a blank/whitespace name — plus a bare
    MagicMock publisher (whose ``.name`` is not a real str)."""
    client = MetronClient()
    session = MagicMock()
    for pub in (None, _mock_publisher(None), _mock_publisher(""), _mock_publisher("   "), MagicMock()):
        issue = MagicMock()
        issue.variants = []
        issue.credits = []
        issue.publisher = pub
        session.issue.return_value = issue
        client._session = session
        assert client.lookup_issue_detail(5)["publisher"] is None


# ---------------------------------------------------------------------------
# Credits extraction + creator-run resolver (BUI-134)
# ---------------------------------------------------------------------------

def _mock_role(name: str, id: int = 1) -> MagicMock:
    r = MagicMock()
    r.id = id
    r.name = name
    return r


def _mock_credit(creator: str, roles: list[str], id: int = 1) -> MagicMock:
    c = MagicMock()
    c.id = id
    c.creator = creator
    c.role = [_mock_role(n) for n in roles]
    return c


def _mock_detail_issue(variants: list[str], credits: list[Any]) -> MagicMock:
    issue = MagicMock()
    issue.variants = [_mock_variant(v) for v in variants]
    issue.credits = credits
    return issue


def _mock_base_issue(id: int, number: str, cover_date: str | None = "1984-01-01") -> MagicMock:
    from datetime import date
    i = MagicMock()
    i.id = id
    i.number = number
    i.cover_date = date.fromisoformat(cover_date) if cover_date else None
    return i


def test_lookup_issue_detail_extracts_credits():
    client = MetronClient()
    session = MagicMock()
    session.issue.return_value = _mock_detail_issue(
        variants=["Direct Edition"],
        credits=[
            _mock_credit("John Romita Jr.", ["Penciller"]),
            _mock_credit("Chris Claremont", ["Writer"]),
        ],
    )
    client._session = session

    result = client.lookup_issue_detail(42)
    assert result["variants"] == ["Direct Edition"]
    assert result["credits"] == [
        {"creator": "John Romita Jr.", "creator_id": None, "roles": ["penciller"]},
        {"creator": "Chris Claremont", "creator_id": None, "roles": ["writer"]},
    ]


def test_lookup_issue_detail_credits_empty_when_none():
    client = MetronClient()
    session = MagicMock()
    session.issue.return_value = _mock_detail_issue(variants=[], credits=[])
    client._session = session
    assert client.lookup_issue_detail(1)["credits"] == []


# --- resolve_creator: pin Metron id, disambiguate JR vs Sr -----------------

def _mock_creator(id: int, name: str) -> MagicMock:
    c = MagicMock()
    c.id = id
    c.name = name
    return c


def test_resolve_creator_single_match():
    client = MetronClient()
    session = MagicMock()
    session.creators_list.return_value = [_mock_creator(355, "John Romita Jr.")]
    client._session = session
    assert client.resolve_creator("John Romita Jr.") == {"id": 355, "name": "John Romita Jr."}


def test_resolve_creator_disambiguates_jr_from_sr_by_exact_name():
    """JR and Sr both surface on a loose 'John Romita' query; exact name wins."""
    client = MetronClient()
    session = MagicMock()
    session.creators_list.return_value = [
        _mock_creator(10, "John Romita"),       # Sr.
        _mock_creator(355, "John Romita Jr."),
    ]
    client._session = session
    # Exact 'John Romita Jr.' pins the Jr. id, never the Sr.
    assert client.resolve_creator("John Romita Jr.") == {"id": 355, "name": "John Romita Jr."}


def test_resolve_creator_ambiguous_returns_none():
    client = MetronClient()
    session = MagicMock()
    session.creators_list.return_value = [
        _mock_creator(10, "John Romita"),
        _mock_creator(355, "John Romita Jr."),
    ]
    client._session = session
    # Loose query matches two, neither equals it exactly -> ambiguous -> None
    assert client.resolve_creator("Romita") is None


def test_resolve_creator_no_match_returns_none():
    client = MetronClient()
    session = MagicMock()
    session.creators_list.return_value = []
    client._session = session
    assert client.resolve_creator("Nobody") is None


# --- resolve_creator_run: BOTH stints, role filter, no-credit warning ------

def test_resolve_creator_run_returns_both_stints():
    """JR JR's Uncanny X-Men: #175–177 (stint 1) AND #287 + #300 (stint 2).

    The candidate set from the issue-list creator filter spans both stints
    (it's pinned by creator id, not memory), and each is confirmed as Penciller.
    """
    client = MetronClient()
    session = MagicMock()
    # issue-list creator filter returns the union of both stints
    session.issues_list.return_value = [
        _mock_base_issue(1001, "175"),
        _mock_base_issue(1002, "176"),
        _mock_base_issue(1003, "177"),
        _mock_base_issue(2001, "287"),
        _mock_base_issue(2002, "300"),
    ]
    # Every one of these has JR JR as Penciller
    session.issue.side_effect = lambda _id: _mock_detail_issue(
        variants=[],
        credits=[_mock_credit("John Romita Jr.", ["Penciller"])],
    )
    client._session = session

    run = client.resolve_creator_run(
        series_id=99, creator_id=355, creator_name="John Romita Jr.", role="penciller",
    )
    numbers = [i["number"] for i in run["issues"]]
    # Both stints present — the discontinuous #287/#300 are NOT dropped
    assert numbers == ["175", "176", "177", "287", "300"]
    assert run["warnings"] == []


def test_resolve_creator_run_filters_by_role():
    """An issue where the creator only WROTE (never pencilled) is excluded."""
    client = MetronClient()
    session = MagicMock()
    session.issues_list.return_value = [
        _mock_base_issue(1, "10"),  # penciller
        _mock_base_issue(2, "11"),  # writer only — must be dropped
    ]

    def _detail(_id):
        if _id == 1:
            return _mock_detail_issue([], [_mock_credit("John Romita Jr.", ["Penciller"])])
        return _mock_detail_issue([], [_mock_credit("John Romita Jr.", ["Writer"])])

    session.issue.side_effect = _detail
    client._session = session

    run = client.resolve_creator_run(
        series_id=1, creator_id=355, creator_name="John Romita Jr.", role="penciller",
    )
    assert [i["number"] for i in run["issues"]] == ["10"]


def test_resolve_creator_run_warns_on_missing_credits():
    """An issue Metron has no credits for is a low-confidence WARNING, not a silent drop."""
    client = MetronClient()
    session = MagicMock()
    session.issues_list.return_value = [
        _mock_base_issue(1, "10"),
        _mock_base_issue(2, "11"),  # no credits at all
    ]

    def _detail(_id):
        if _id == 1:
            return _mock_detail_issue([], [_mock_credit("John Romita Jr.", ["Penciller"])])
        return _mock_detail_issue([], [])  # thin Silver/Bronze book

    session.issue.side_effect = _detail
    client._session = session

    run = client.resolve_creator_run(
        series_id=1, creator_id=355, creator_name="John Romita Jr.", role="penciller",
    )
    assert [i["number"] for i in run["issues"]] == ["10"]
    assert len(run["warnings"]) == 1
    assert run["warnings"][0]["number"] == "11"
    assert "no credits" in run["warnings"][0]["reason"]


def test_resolve_creator_run_role_is_explicit_no_layouts_by_default():
    """Default penciller does NOT auto-include 'breakdowns'/'layouts'."""
    client = MetronClient()
    session = MagicMock()
    session.issues_list.return_value = [_mock_base_issue(1, "10")]
    session.issue.side_effect = lambda _id: _mock_detail_issue(
        [], [_mock_credit("John Romita Jr.", ["Breakdowns"])]
    )
    client._session = session
    run = client.resolve_creator_run(
        series_id=1, creator_id=355, creator_name="John Romita Jr.", role="penciller",
    )
    assert run["issues"] == []


def test_resolve_creator_run_hard_failure_returns_none():
    client = MetronClient()
    session = MagicMock()
    session.issues_list.side_effect = ConnectionError("down")
    client._session = session
    assert client.resolve_creator_run(
        series_id=1, creator_id=1, creator_name="X", role="penciller",
    ) is None


def test_resolve_creator_run_same_name_collision_is_warned_not_included():
    """Same-name different-id namesake must NOT silently add an issue to the run (BUI-198).

    Scenario: we are resolving "John Romita Jr." (id=355) as Penciller.  The
    issue-list creator filter returns issue #42 because creator 355 holds a Writer
    credit on it.  But a different Metron creator with the identical canonical name
    (a namesake, id=400) also credits issue #42 as Penciller.  mokkari's Credit
    carries no creator id — only the name string — so these two credits appear as
    two separate entries both with creator="John Romita Jr.".

    Without the guard, the name+role check would wrongly confirm #42 as penciller
    (matching the namesake's credit).  The guard detects two entries sharing the
    same creator name and demotes the issue to a warning instead of adding it to
    the run.
    """
    client = MetronClient()
    session = MagicMock()
    # Candidate set: issue #42 was returned by the id-pinned issue-list filter
    # (creator 355 has a Writer credit on it), plus issue #10 which is legitimately
    # pencilled by creator 355 (single credit entry, no collision).
    session.issues_list.return_value = [
        _mock_base_issue(101, "10"),   # clean penciller credit — should be in run
        _mock_base_issue(142, "42"),   # same-name collision — must NOT be in run
    ]

    def _detail(_id: int) -> MagicMock:
        if _id == 101:
            # Normal case: one credit entry, creator pencilled it.
            return _mock_detail_issue(
                [], [_mock_credit("John Romita Jr.", ["Penciller"])]
            )
        # Issue #42: two separate credit entries both named "John Romita Jr." —
        # one is the resolved creator (Writer, id=355) and one is the namesake
        # (Penciller, id=400).  mokkari Credit exposes only the name string, so
        # both entries look identical to the name-based check.
        return _mock_detail_issue(
            [],
            [
                _mock_credit("John Romita Jr.", ["Writer"],    id=901),
                _mock_credit("John Romita Jr.", ["Penciller"], id=902),
            ],
        )

    session.issue.side_effect = _detail
    client._session = session

    run = client.resolve_creator_run(
        series_id=1, creator_id=355, creator_name="John Romita Jr.", role="penciller",
    )

    # Issue #10 is confirmed (single credit, no collision).
    assert [i["number"] for i in run["issues"]] == ["10"]

    # Issue #42 is flagged as a warning, NOT silently included in the run.
    assert len(run["warnings"]) == 1
    w = run["warnings"][0]
    assert w["number"] == "42"
    assert w["metron_id"] == 142
    assert "same-name collision" in w["reason"]
    assert "BUI-198" in w["reason"]


def test_resolve_creator_run_name_drift_is_warned_not_silently_dropped():
    """A candidate whose credit name differs from the canonical name is WARNED (BUI-198).

    The issue is in the id-pinned candidate set (the resolved creator has *some*
    credit on it), but the credit's name string is a punctuation variant
    ("John Romita, Jr." vs the resolved "John Romita Jr.").  No credit matches the
    canonical name, so the issue can't be confirmed for the run — but it must be
    surfaced as a warning so a truncated run is visible, not a silent drop.
    """
    client = MetronClient()
    session = MagicMock()
    session.issues_list.return_value = [
        _mock_base_issue(101, "10"),   # clean match — should be in run
        _mock_base_issue(155, "15"),   # name drift — must be warned, not in run
    ]

    def _detail(_id: int) -> MagicMock:
        if _id == 101:
            return _mock_detail_issue(
                [], [_mock_credit("John Romita Jr.", ["Penciller"])]
            )
        # Comma-variant name string: no match against the resolved canonical name.
        return _mock_detail_issue(
            [], [_mock_credit("John Romita, Jr.", ["Penciller"])]
        )

    session.issue.side_effect = _detail
    client._session = session

    run = client.resolve_creator_run(
        series_id=1, creator_id=355, creator_name="John Romita Jr.", role="penciller",
    )

    assert [i["number"] for i in run["issues"]] == ["10"]
    assert len(run["warnings"]) == 1
    w = run["warnings"][0]
    assert w["number"] == "15"
    assert w["metron_id"] == 155
    assert "no credit name matched" in w["reason"]
    assert "BUI-198" in w["reason"]


def test_resolve_creator_run_stops_after_breaker_trips_mid_loop():
    """BUI-344: once the breaker trips on an early candidate's issue-detail
    fetch, the remaining candidates must NOT each pay their own capped retry
    sleep against a down Metron — the loop stops and surfaces the rest as
    explicitly-skipped warnings instead of silently grinding through them.
    """
    client = MetronClient()
    session = MagicMock()
    session.issues_list.return_value = [
        _mock_base_issue(1, "10"),
        _mock_base_issue(2, "11"),
        _mock_base_issue(3, "12"),
    ]
    # Candidate #10's issue-detail fetch takes a genuine 5xx twice in a row —
    # exhausts lookup_issue_detail's own capped retry and trips self.degraded,
    # exactly as BUI-342 wired it.
    session.issue.side_effect = [
        _server_error_api_error(500),
        _server_error_api_error(500),
    ]
    client._session = session

    with patch("locg.metron.time.sleep"):
        run = client.resolve_creator_run(
            series_id=1, creator_id=355, creator_name="John Romita Jr.", role="penciller",
        )

    # Only candidate #10's lookup_issue_detail was attempted — its own inner
    # retry consumes both side_effect entries. #11 and #12 were never queried
    # once the breaker tripped: no capped retry sleep paid per remaining
    # candidate against a down Metron.
    assert session.issue.call_count == 2
    assert client.degraded is True

    assert run["issues"] == []
    reasons_by_number = {w["number"]: w["reason"] for w in run["warnings"]}
    assert "issue detail fetch failed" in reasons_by_number["10"]
    assert "breaker tripped" in reasons_by_number["11"]
    assert "breaker tripped" in reasons_by_number["12"]


# ---------------------------------------------------------------------------
# Credential error — raised, not swallowed
# ---------------------------------------------------------------------------

def test_lookup_issue_raises_credential_error_when_no_env(monkeypatch):
    monkeypatch.delenv("METRON_USERNAME", raising=False)
    monkeypatch.delenv("METRON_PASSWORD", raising=False)
    client = MetronClient()
    with pytest.raises(MetronCredentialError, match="METRON_USERNAME"):
        client.lookup_issue("Batman", "1")


def test_credential_error_not_at_import():
    """MetronCredentialError must not be raised at import or MetronClient construction."""
    # If we get here without error, the test passes.
    from locg.metron import MetronClient as _MC  # noqa: F401 (reimport to verify)
    _MC()


# ---------------------------------------------------------------------------
# Integration — real Metron API (skipped unless credentials present)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (os.environ.get("METRON_USERNAME") and os.environ.get("METRON_PASSWORD")),
    reason="METRON_USERNAME/PASSWORD not set",
)
def test_integration_real_lookup():
    client = MetronClient()
    # Fantastic Four #1 (1961) — well-known Metron entry
    result = client.lookup_issue("Fantastic Four", "1")
    assert result is not None
    assert result["metron_id"] is not None
    assert result["cover_date"] is not None
    assert result["series_year_began"] == 1961
