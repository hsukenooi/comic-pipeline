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
    issue.credits = []
    session.issue.return_value = issue
    client._session = session

    result = client.lookup_issue_detail(5)
    assert result == {
        "variants": ["Capullo Variant", "Todd McFarlane Cover"],
        "credits": [],
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
    assert client.lookup_issue_detail(5) == {"variants": [], "credits": []}


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
