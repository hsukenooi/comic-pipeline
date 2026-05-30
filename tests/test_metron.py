"""Tests for the Metron API wrapper (Unit 5)."""
from __future__ import annotations

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
    session.issues_list.assert_called_once_with({"series": 1, "number": "1"})


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
    session.issues_list.assert_called_once_with({"series": 1, "number": "151"})


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
    """Multiple series + no year → cannot disambiguate → None (manual fallback)."""
    client, _ = _make_client_with_session(
        series_list=[
            _mock_series(id=1, year_began=1963, year_end=1998),
            _mock_series(id=2, year_began=1999, year_end=2012),
        ],
        issues_list=[_mock_issue()],
    )
    assert client.lookup_issue("Amazing Spider-Man", "151") is None


def test_lookup_issue_ambiguous_year_in_two_ranges_returns_none():
    """Year falls in two overlapping ranges → still ambiguous → None."""
    client, _ = _make_client_with_session(
        series_list=[
            _mock_series(id=1, year_began=1980, year_end=2011),
            _mock_series(id=2, year_began=2000, year_end=None),
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
    from mokkari.exceptions import RateLimitError
    client, session = _make_client_with_session(
        series_list=[_mock_series()],
    )
    session.issues_list.side_effect = RateLimitError("rate limited", retry_after=60)
    assert client.lookup_issue("Fantastic Four", "1") is None


def test_lookup_issue_swallows_api_error():
    from mokkari.exceptions import ApiError
    client, session = _make_client_with_session()
    session.series_list.side_effect = ApiError("404 not found")
    assert client.lookup_issue("Nonexistent", "1") is None


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
    session.issue.return_value = issue
    client._session = session

    result = client.lookup_issue_detail(5)
    assert result == {"variants": ["Capullo Variant", "Todd McFarlane Cover"]}
    session.issue.assert_called_once_with(5)


def test_lookup_issue_detail_no_variants():
    client = MetronClient()
    session = MagicMock()
    issue = MagicMock()
    issue.variants = []
    session.issue.return_value = issue
    client._session = session
    assert client.lookup_issue_detail(5) == {"variants": []}


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
