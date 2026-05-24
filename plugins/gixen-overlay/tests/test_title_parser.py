"""Tests for the bare-number issue extraction fallback (PER-90)."""
from __future__ import annotations

import pytest

from gixen_overlay.title_parser import parse_title


# Real eBay titles pulled from the Mac Mini's `bids` table (2026-05-21).
# All hashless; all 18 production rows broke at issue extraction before PER-90.
@pytest.mark.parametrize("title,expected_issue,expected_series", [
    ("UNCANNY X-MEN   211 - (NM+) -MARAUDERS-WOLVERINE",   "211", "UNCANNY X-MEN"),
    ("Ghost Rider   3 - 1st Fire Motorcycle Fine/VF Cond", "3",   "Ghost Rider"),
    ("Godzilla   2 - Godzilla attacking Seattle",          "2",   "Godzilla"),
    ("Giant-Size X-Men   2 - Neal Adams art Fine/VF Cond", "2",   "Giant-Size X-Men"),
])
def test_bare_issue_extracted_from_hashless_titles(title, expected_issue, expected_series):
    r = parse_title(title)
    assert r.issue == expected_issue
    assert r.series == expected_series


def test_hash_path_still_wins_when_present():
    """Bare-single is a fallback — hash extraction must take precedence."""
    r = parse_title("Amazing Spider-Man #300 1988 NM")
    assert r.issue == "300"
    assert r.year == 1988
    assert r.series == "Amazing Spider-Man"


def test_hashless_title_with_year_extracts_both():
    """Year in title still wins; bare-single picks the non-year number."""
    r = parse_title("Amazing Spider-Man 300 1988 NM")
    assert r.issue == "300"
    assert r.year == 1988


def test_bare_single_skips_year_lookalike_numbers():
    """A 4-digit number in the year range (1930–2099) is never an issue."""
    r = parse_title("Comic Lot 1985 Various")
    assert r.issue is None


def test_bare_single_does_not_match_decimal_grade():
    """'9.8' in 'CGC 9.8' must not be extracted as issue '9'."""
    r = parse_title("CGC 9.8 X-Men")
    assert r.issue is None
    assert r.grade == 9.8


def test_title_with_no_digits_returns_none():
    """Empty issue is the right answer for grade-only titles like signed comics."""
    r = parse_title("Ghost Rider Wolverine Punisher Signed by John Romita Jr w/COA NM- Cond")
    assert r.issue is None


def test_series_truncates_at_standalone_dash_separator():
    """Listing descriptions after ' - ' belong to the listing, not the series.

    Different listings of the same comic should dedup to one comics row, so
    'Series Issue - (Grade) - Description' must clean to just 'Series'.
    """
    r1 = parse_title("UNCANNY X-MEN   211 - (NM+) -MARAUDERS-WOLVERINE")
    r2 = parse_title("UNCANNY X-MEN   211 - (NM) -DIFFERENT DESCRIPTION")
    assert r1.series == r2.series == "UNCANNY X-MEN"


def test_hyphenated_series_name_preserved():
    """'X-Men' has no spaces around its hyphen, so it must survive the
    ' - ' truncation that only triggers on space-dash-space."""
    r = parse_title("Giant-Size X-Men 2")
    assert r.series == "Giant-Size X-Men"
    assert r.issue == "2"


# Real eBay titles from a Batman lot listed by a seller who uses
# "(DC Comics Month Year) Grade condition" format. All 7 broke before the
# publisher-paren-removal and condition/1st-content-note fixes.
@pytest.mark.parametrize("title,expected_issue", [
    ("Batman  363 (DC Comics September 1983) VF/NM condition",                         "363"),
    ("Batman  368 (DC Comics February 1984) 1st Jason Todd as Robin VF/NM condition",  "368"),
    ("Batman  376 (DC Comics October 1984) VF+ condition",                             "376"),
    ("Batman  426 (DC Comics December 1988) VF- condition",                            "426"),
    ("Batman  427 (DC Comics Winter 1988) VF/NM condition",                            "427"),
    ("Batman  428 VF/NM condition Huge auction going on now!",                         "428"),
    ("Batman  429 (DC Comics January 1989) FN- condition",                             "429"),
])
def test_publisher_paren_and_condition_noise_stripped(title, expected_issue):
    """Series must be 'Batman', not 'Batman (DC September condition' or similar."""
    r = parse_title(title)
    assert r.series == "Batman", f"got {r.series!r} from {title!r}"
    assert r.issue == expected_issue
