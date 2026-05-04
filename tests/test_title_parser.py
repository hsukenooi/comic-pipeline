"""Tests for server.title_parser — covers reference cases from spec
plus real titles pulled from the live DB."""
from __future__ import annotations

import pytest

from server.title_parser import parse_title, LETTER_GRADE_MAP


def test_reference_case_dark_knight_returns():
    """Item 127831373212 — must extract series ~Batman: The Dark Knight Returns,
    issue=1, year=1986 not present so fall through, grade None."""
    t = "Batman The Dark Knight Returns # 1 & 3 First Prints - Miller story & art"
    p = parse_title(t)
    # Should pick up "1" as the first issue from "# 1 & 3"
    assert p.issue == "1"
    # Series should mention Dark Knight Returns
    assert "Dark Knight Returns" in p.series
    # Grade not in title
    assert p.grade is None


def test_killing_joke_year_extraction():
    t = "Batman The Killing Joke DC Comics 1988 1st Print Joker Shoots/Paralyzes Batgirl"
    p = parse_title(t)
    assert p.year == 1988
    assert "Killing Joke" in p.series
    # No explicit issue; spec says issue=1 OR None acceptable
    assert p.issue is None or p.issue == "1"


def test_ghost_rider_multi_issue_run_picks_first():
    t = "Marvel Spotlight On GHOST RIDER #5,6,7,8,9,10,11 Full Run 1st App Ghost Rider!"
    p = parse_title(t)
    assert p.issue == "5"
    assert p.confidence == "low"  # multi-issue run
    assert "Ghost Rider" in p.series.lower() or "spotlight" in p.series.lower()


def test_akira_bare_run():
    t = "Akira 1,2,3,4,5,6,7,8,9 Marvel Epic Comics 1988 1st Prints Hardcover"
    p = parse_title(t)
    assert p.issue == "1"
    assert p.year == 1988
    assert "Akira" in p.series
    assert p.confidence == "low"


def test_cgc_grade_extraction():
    t = "Amazing Spider-Man #300 CGC 9.4 White Pages 1988"
    p = parse_title(t)
    assert p.grade == 9.4
    assert p.issue == "300"
    assert p.year == 1988


def test_letter_grade_nm_plus():
    t = "Spawn #313 Capullo variant NM Gem Wow Z"
    p = parse_title(t)
    assert p.issue == "313"
    assert p.grade == LETTER_GRADE_MAP["NM"]


def test_vfnm_letter_grade():
    t = "Spawn #227 Incredible ASM 300 Homage cover Key Low Print Run VFNM McFarlane"
    p = parse_title(t)
    assert p.issue == "227"
    # VFNM should map to ~9.0 (VF/NM)
    assert p.grade == 9.0


def test_vf_minus_grade():
    t = "Spawn #222 Incredible ASM 316 Homage cover Key Low Print Run VF- Wow McFarlane"
    p = parse_title(t)
    assert p.issue == "222"
    assert p.grade == 7.5  # VF-


def test_invincible_19():
    t = "INVINCIBLE #19 1st Appearance Battle Beast 1st Print 2004"
    p = parse_title(t)
    assert p.issue == "19"
    assert p.year == 2004
    assert "Invincible" in p.series.title() or "INVINCIBLE" in p.series.upper()


def test_empty_title():
    p = parse_title("")
    assert p.series == ""
    assert p.confidence == "low"


def test_multi_issue_run_low_confidence():
    t = "A.D. After Death 1,2,3 Image Comics 2018 Limited Series Scott Snyder Jeff Lemire"
    p = parse_title(t)
    assert p.issue == "1"
    assert p.year == 2018
    assert p.confidence == "low"


def test_single_issue_with_year_is_high_confidence():
    t = "Amazing Spider-Man #300 1988"
    p = parse_title(t)
    assert p.issue == "300"
    assert p.year == 1988
    assert p.confidence == "high"


# ---------------------------------------------------------------------------
# Multi-issue list / range support
# ---------------------------------------------------------------------------

def test_issues_field_populated_for_single():
    p = parse_title("Amazing Spider-Man #300 1988")
    assert p.issues == ["300"]


def test_comma_run_returns_full_list():
    t = "Daredevil The Man Without Fear 1,2,3,4,5 Marvel Comics 1993 Limited Series"
    p = parse_title(t)
    assert p.issues == ["1", "2", "3", "4", "5"]
    assert p.issue == "1"
    assert p.confidence == "low"


def test_ampersand_pair_returns_both_issues():
    t = "Batman The Dark Knight Returns # 1 & 3 First Prints - Miller story & art"
    p = parse_title(t)
    assert p.issues == ["1", "3"]
    assert p.issue == "1"


def test_hash_range_expands_full_run():
    t = "Akira #1-9 Marvel Epic Comics 1988 1st Prints Hardcover"
    p = parse_title(t)
    assert p.issues == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    assert p.year == 1988


def test_bare_range_expands():
    t = "Sandman 1-10 DC Vertigo 1989"
    p = parse_title(t)
    # Range should expand to issues 1..10
    assert p.issues == [str(i) for i in range(1, 11)]


def test_year_range_not_treated_as_issue_range():
    """A '2010-2019' span must not be mistaken for issues 2010-2019.

    The series cleaner happens to also strip these out, so we just need to
    confirm we don't hallucinate 10 issues with year-shaped numbers.
    """
    t = "Walking Dead 2010-2019 complete series Image Comics"
    p = parse_title(t)
    # Either no issue extracted or a single sane issue — never a 10-element
    # year list.
    if p.issues:
        for issue in p.issues:
            assert int(issue) < 1900, f"Issue {issue} looks like a year"


def test_huge_range_rejected():
    """'#1-200' is suspicious — likely a range marker, not the literal 200 issues."""
    t = "Some Series #1-200 lot"
    p = parse_title(t)
    # Range > 50 → reject; either no issues or fall through to a different match.
    if p.issues:
        assert len(p.issues) <= 1 or len(p.issues) <= 50
