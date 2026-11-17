"""Tests for slab_checks.py (BUI-1418): the pure check_listing verdicts."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import slab_checks as sc


def item(**over):
    base = {
        "legacyItemId": "123",
        "title": "Amazing Spider-Man #50 CGC 6.0",
        "seller": {"username": "s", "feedbackPercentage": "99.5", "feedbackScore": 1200},
        "returnTerms": {"returnsAccepted": True},
        "localizedAspects": [
            {"name": "Professional Grader", "value": "Certified Guaranty Company (CGC)"},
            {"name": "Grade", "value": "6"},
            {"name": "Certification Number", "value": "4012345001"},
        ],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SLAB_MIN_FEEDBACK_PCT", raising=False)
    monkeypatch.delenv("SLAB_MIN_FEEDBACK_SCORE", raising=False)


def test_pass():
    r = sc.check_listing(item())
    assert r["verdict"] == "pass" and r["reasons"] == []
    assert r["cert"] == {"number": "4012345001", "grader": "Certified Guaranty Company (CGC)",
                         "grade": "6", "verified": True}


def test_pass_unverified_no_cert_number():
    r = sc.check_listing(item(localizedAspects=[]))
    assert r["verdict"] == "pass_unverified"
    assert r["cert"]["grader"] == "CGC"  # title fallback
    assert r["cert"]["number"] is None


def test_non_numeric_cert_is_unverified():
    asp = [{"name": "Certification Number", "value": "N/A"}]
    assert sc.check_listing(item(localizedAspects=asp))["verdict"] == "pass_unverified"


def test_no_localized_aspects_key_and_no_title_grader():
    d = item(title="Spider-Man 50")
    del d["localizedAspects"]
    r = sc.check_listing(d)
    assert r["verdict"] == "pass_unverified" and r["cert"]["grader"] is None


def test_no_returns_is_flagged_not_dropped():
    r = sc.check_listing(item(returnTerms={"returnsAccepted": False}))
    assert r["verdict"] == "pass" and r["reasons"] == []
    assert r["flags"] == ["returns not accepted"]


def test_missing_return_terms_is_not_accepted():
    d = item()
    del d["returnTerms"]
    r = sc.check_listing(d)
    assert r["verdict"] == "pass" and r["returns"]["accepted"] is None
    assert r["flags"] == ["returns unknown"]


def test_returns_accepted_must_be_literal_true():
    for terms in ({"returnsAccepted": "false"}, {}, {"returnsAccepted": "true"}):
        r = sc.check_listing(item(returnTerms=terms))
        assert r["verdict"] == "pass" and r["flags"] == ["returns unknown"]


def test_returns_accepted_has_no_flags():
    assert sc.check_listing(item())["flags"] == []


def test_weak_feedback_pct():
    r = sc.check_listing(item(seller={"feedbackPercentage": "97.9", "feedbackScore": 900}))
    assert r["verdict"] == "drop" and r["seller"]["weak"]


def test_weak_feedback_score():
    r = sc.check_listing(item(seller={"feedbackPercentage": "100.0", "feedbackScore": 49}))
    assert r["verdict"] == "drop"


def test_threshold_boundaries_pass():
    r = sc.check_listing(item(seller={"feedbackPercentage": "98.0", "feedbackScore": 50}))
    assert r["verdict"] == "pass"


def test_missing_seller_is_weak():
    d = item()
    del d["seller"]
    r = sc.check_listing(d)
    assert r["verdict"] == "drop" and len(r["reasons"]) == 2


def test_multiple_reasons():
    r = sc.check_listing(item(seller={"feedbackPercentage": "90", "feedbackScore": 5},
                              returnTerms={"returnsAccepted": False}))
    assert r["verdict"] == "drop" and len(r["reasons"]) == 2
    assert r["flags"] == ["returns not accepted"]


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SLAB_MIN_FEEDBACK_PCT", "99.9")
    assert sc.check_listing(item())["verdict"] == "drop"
    monkeypatch.setenv("SLAB_MIN_FEEDBACK_PCT", "90")
    monkeypatch.setenv("SLAB_MIN_FEEDBACK_SCORE", "5000")
    assert sc.check_listing(item())["verdict"] == "drop"


def test_bad_env_exits(monkeypatch):
    monkeypatch.setenv("SLAB_MIN_FEEDBACK_PCT", "abc")
    with pytest.raises(SystemExit):
        sc.thresholds()


def test_non_dict_input_drops():
    assert sc.check_listing(None)["verdict"] == "drop"


def test_render_smoke():
    out = sc.render([sc.check_listing(item()), {"item_id": "9", "verdict": None, "error": "x"}])
    assert "pass" in out and "error" in out
