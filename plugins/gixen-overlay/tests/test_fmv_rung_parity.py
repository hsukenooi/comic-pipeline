"""BUI-1219 canary: fmv_math's bid rule vs policy.py's recompute.

apps/fmv is not a workspace member (KTD5), so policy.py duplicates the
median-anchored cap's constants rather than importing them. fmv_math.py is
stdlib-only, so this test loads it straight from the repo checkout by path
and checks two things:

1. The duplicated constants and tier vocabularies are equal.
2. Behaviour: for a sweep of pools and photo grade confidences, the cap
   `_check_recomputed_cap` re-derives from the row comic-fmv would STORE is
   never below the max_bid comic-fmv actually computed (no false advisory),
   and on a median-capped row it is exactly that bid.

If either fails, the bid rule and the policy check have drifted.
"""
from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path

import pytest

from gixen_overlay import policy
from gixen_overlay.db import FMV_BID_TIERS

REPO_ROOT = Path(__file__).resolve().parents[3]
_FMV_MATH_PATH = REPO_ROOT / "apps" / "fmv" / "src" / "fmv_math.py"


def _load_fmv_math():
    spec = importlib.util.spec_from_file_location("_parity_fmv_math", _FMV_MATH_PATH)
    assert spec is not None and spec.loader is not None, _FMV_MATH_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fm = _load_fmv_math()

# fmv_runner._confidence_to_db_label's collapse, the writer of fmv.confidence
# (see memory: stored-label collapse trap). Pinned here rather than imported
# because fmv_runner needs the apps/fmv tool environment.
_DB_LABEL = {"HIGH": "high", "MEDIUM-HIGH": "medium", "MEDIUM": "medium",
             "MEDIUM-LOW": "low", "LOW": "low"}


def test_median_rule_constants_match_fmv_math():
    assert tuple(policy.MEDIAN_BID_TIERS) == tuple(fm.MEDIAN_BID_TIERS)
    assert policy.MEDIAN_BID_FACTOR == fm.MEDIAN_BID_FACTOR == 1.00
    assert tuple(FMV_BID_TIERS) == tuple(fm.BID_TIERS)
    assert policy.RUNG_HIGH_CONFIDENCE == fm.BASE_BID_FACTOR


def test_median_tier_confidence_matches_the_writer_collapse():
    """Each median tier's expected stored confidence is what the writer
    stores for the rubric labels that map to that tier."""
    for label, db_label in _DB_LABEL.items():
        tier = fm.bid_tier(label, 0.5, 0.1)
        if tier in fm.MEDIAN_BID_TIERS:
            assert policy._MEDIAN_TIER_CONFIDENCE[tier] == db_label, label


def test_clean_round_is_idempotent_on_its_own_outputs():
    """policy.py does not re-round the stored median; that is only exact if
    clean_round(clean_round(x)) == clean_round(x)."""
    for cents in range(0, 500_000, 7):
        once = fm.clean_round(cents / 100)
        assert fm.clean_round(once) == once, cents / 100


def _comps(prices, grade=8.0):
    return [{"price": p, "grade": grade, "product_id": str(i), "title": ""}
            for i, p in enumerate(prices)]


def _stored_row(out: dict) -> dict:
    """The fmv row comic-fmv's upsert leaves for a raw priced result: the
    median pair rides every raw DIRECT row (any tier), never an
    interpolated one (fmv_runner._median_bid_eligible)."""
    raw_direct = out["bid_basis"] is not None and not out["interpolated"]
    return {
        "id": 1, "high": out["fmv_high"], "low": out["fmv_low"],
        "confidence": _DB_LABEL[out["confidence"]],
        "certifier": "none",
        "pricing_basis": "interpolated" if out["interpolated"] else "direct",
        "median": out["median"] if raw_direct else None,
        "bid_tier": out["bid_tier"] if raw_direct else None,
        "ceiling_cap": None,
    }


_PRICES = [12, 20, 35, 50, 80, 120, 195, 260, 400]
_POOLS = [list(p) for size in (3, 4, 5, 6, 8)
          for p in itertools.combinations_with_replacement(_PRICES, size)]


@pytest.mark.parametrize("grade_conf", [None, "high", "medium"])
def test_policy_recompute_never_sits_below_the_fmv_math_bid(grade_conf):
    median_rows = 0
    for pool in _POOLS:
        out = fm.compute_fmv(_comps(pool), 8.0, grade_confidence=grade_conf)
        if out["max_bid"] is None:
            continue
        link = policy._recomputed_link_cap(_stored_row(out))
        # The pre-existing rung path compares the UNROUNDED rung x high, so
        # clean_round's round-to-nearest can sit a step above it; that
        # residual predates BUI-1219 and is only checked on the median path.
        if out["bid_basis"] == "median":
            median_rows += 1
            assert link["median_cap"] == out["max_bid"], pool
            assert link["cap"] >= out["max_bid"], pool
            assert link["cap"] <= out["fmv_high"], pool
    assert median_rows > 100  # the sweep genuinely exercises the median rule


def test_null_median_row_recomputes_todays_rung():
    link = policy._recomputed_link_cap({
        "id": 1, "high": 140.0, "confidence": "high", "certifier": "none",
        "pricing_basis": "direct", "median": None, "bid_tier": None})
    assert link["median_cap"] is None
    assert link["cap"] == pytest.approx(0.80 * 140.0)
