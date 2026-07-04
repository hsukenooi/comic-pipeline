"""Dated-pool regression baseline for recency weighting (BUI-289 / R7).

The existing golden fixture (test_golden_fmv_math.py) freezes compute_fmv on
pools whose comps carry **no sold_date**, so it only ever exercises the
pre-U2 unweighted path (weights all 1.0 -> _weights_equal short-circuit). The
recency-weighted path (BUI-287 U2) — weighted quantiles + effective-n
confidence — was therefore never regression-guarded. R7 requires exactly that
guard before we rely on U2 moving live bid caps.

This module supplies it. It freezes compute_fmv on **dated** pools (fixtures/
recency_baseline.json) so any future change to the weighted quantiles, the
half-life, or the effective-n confidence rubric diffs visibly against the
baseline instead of silently shifting a bid cap. It also bakes the R7
"every FMV move is explained by recency" review into executable assertions:
direction (recent-high raises FMV, recent-low lowers it), the exact 75-day
half-life, the degenerate all-same-date reduction, and confidence
reconciliation (a stale pool loses HIGH).

All sold_dates are absolute and anchored to a FIXED reference constant (never
date.today()), so the fixture is reproducible whenever the suite runs —
compute_fmv itself ages comps against the newest date *in the pool*, clock-free.

Regenerate the baseline ONLY on an intended change:
    python tests/test_recency_baseline.py --regen
and review the JSON diff before committing.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import fmv_math

BASELINE = Path(__file__).parent / "fixtures" / "recency_baseline.json"

# Fixed anchor — the newest possible sale in any case below. Hardcoded (NOT
# date.today()) so every sold_date, and therefore every frozen output, is
# deterministic across runs.
_REF = date(2026, 7, 4)

_FROZEN = (
    "n", "effective_n", "window", "flag_reason", "fmv_low", "fmv_high",
    "median", "max_bid", "confidence", "bid_factor",
)


def _d(days_ago: int) -> str:
    return (_REF - timedelta(days=days_ago)).isoformat()


def _comp(price, grade, days_ago=None):
    c = {"price": price, "grade": grade, "product_id": f"p{price}", "title": ""}
    if days_ago is not None:
        c["sold_date"] = _d(days_ago)
    return c


def _strip_dates(comps):
    return [{k: v for k, v in c.items() if k != "sold_date"} for c in comps]


# (name, comps, target_grade)
CASES = [
    # Recent sales run higher than old ones -> weighted FMV rises.
    ("direction_up__recent_high_old_low",
     [_comp(100, 9.0, 300), _comp(105, 9.0, 250), _comp(110, 9.0, 200),
      _comp(130, 9.0, 20), _comp(135, 9.0, 10), _comp(140, 9.0, 0)], 9.0),
    # Recent sales run lower than old ones -> weighted FMV falls.
    ("direction_down__recent_low_old_high",
     [_comp(140, 9.0, 300), _comp(135, 9.0, 250), _comp(130, 9.0, 200),
      _comp(110, 9.0, 20), _comp(105, 9.0, 10), _comp(100, 9.0, 0)], 9.0),
    # Realistic gentle uptrend, thick pool: the point estimate / bid cap barely
    # move; recency mostly reconciles confidence downward.
    ("thick_mild_skew__typical",
     [_comp(200, 8.0, 180), _comp(210, 8.0, 150), _comp(205, 8.0, 120),
      _comp(215, 8.0, 90), _comp(220, 8.0, 60), _comp(225, 8.0, 30),
      _comp(230, 8.0, 10), _comp(228, 8.0, 3)], 8.0),
    # One comp exactly one half-life (75d) old -> 0.5 weight vs the fresh comp.
    ("half_life__75d_old_half_weight",
     [_comp(100, 9.0, 75), _comp(200, 9.0, 0)], 9.0),
    # Watch-item (BUI-289): a 2-comp pool whose fresh comp is the HIGH one pulls
    # fmv_high up. The move is bounded by the BUI-179 too_sparse flag (a 1.5x
    # ratio stays under it) and surfaced by the LOW confidence label — which
    # drives the 0.60 haircut in the graded /comic:buy flow (bid_factor only
    # haircuts when grade_confidence is present; a bare manual run stays at 0.80).
    ("thin_2comp_fresh_high__watch_item",
     [_comp(80, 9.0, 200), _comp(120, 9.0, 0)], 9.0),
    # All comps share a date -> must reduce EXACTLY to the unweighted result.
    ("degenerate_same_date",
     [_comp(100, 9.0, 30), _comp(110, 9.0, 30), _comp(120, 9.0, 30),
      _comp(130, 9.0, 30), _comp(140, 9.0, 30)], 9.0),
    # 8 tight comps but ALL ~1yr stale -> effective_n < raw n, so the pool can
    # no longer claim HIGH purely on raw count (R6/R7 confidence reconciliation).
    ("stale_high_count__confidence",
     [_comp(100, 9.0, 400), _comp(102, 9.0, 390), _comp(104, 9.0, 380),
      _comp(106, 9.0, 370), _comp(108, 9.0, 360), _comp(110, 9.0, 350),
      _comp(112, 9.0, 340), _comp(114, 9.0, 330)], 9.0),
]


def _run(case) -> dict:
    _name, comps, grade = case
    out = fmv_math.compute_fmv(comps, target_grade=grade)
    frozen = {k: out[k] for k in _FROZEN}
    # effective_n is the one raw float in the set — round so the baseline is not
    # brittle to float noise while still catching a real weighting change.
    frozen["effective_n"] = round(frozen["effective_n"], 3)
    return frozen


def _by_name(name):
    return next(c for c in CASES if c[0] == name)


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text())


# ─── R7 regression guard: frozen weighted outputs ─────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_weighted_output_matches_baseline(case):
    baseline = _load_baseline()
    name = case[0]
    assert name in baseline, f"no recency baseline for {name!r} — regenerate"
    assert _run(case) == baseline[name]


def test_every_case_has_a_baseline_entry():
    baseline = _load_baseline()
    assert {c[0] for c in CASES} == set(baseline), (
        "CASES and the recency baseline have diverged — regenerate"
    )


# ─── R7 review baked into executable assertions ───────────────────────────────

def test_recent_high_pool_raises_fmv_vs_unweighted():
    _n, comps, grade = _by_name("direction_up__recent_high_old_low")
    w = fmv_math.compute_fmv(comps, target_grade=grade)
    u = fmv_math.compute_fmv(_strip_dates(comps), target_grade=grade)
    # A pool whose recent sales are higher must not price BELOW the unweighted
    # pool on any of the three FMV points.
    assert w["fmv_high"] > u["fmv_high"]
    assert w["median"] >= u["median"]
    assert w["max_bid"] > u["max_bid"]


def test_recent_low_pool_lowers_fmv_vs_unweighted():
    _n, comps, grade = _by_name("direction_down__recent_low_old_high")
    w = fmv_math.compute_fmv(comps, target_grade=grade)
    u = fmv_math.compute_fmv(_strip_dates(comps), target_grade=grade)
    assert w["fmv_high"] < u["fmv_high"]
    assert w["median"] <= u["median"]
    assert w["max_bid"] < u["max_bid"]


def test_same_date_pool_reduces_to_unweighted_exactly():
    _n, comps, grade = _by_name("degenerate_same_date")
    w = fmv_math.compute_fmv(comps, target_grade=grade)
    u = fmv_math.compute_fmv(_strip_dates(comps), target_grade=grade)
    for k in ("fmv_low", "fmv_high", "median", "max_bid", "confidence",
              "effective_n"):
        assert w[k] == u[k], f"{k}: weighted {w[k]!r} != unweighted {u[k]!r}"


def test_half_life_is_exactly_75_days():
    # A comp aged one RECENCY_HALF_LIFE_DAYS contributes half a fresh comp's
    # weight, so effective_n of {fresh, one-half-life-old} == 1.5.
    _n, comps, grade = _by_name("half_life__75d_old_half_weight")
    out = fmv_math.compute_fmv(comps, target_grade=grade)
    assert out["effective_n"] == pytest.approx(1.5, abs=1e-9)
    assert fmv_math.RECENCY_HALF_LIFE_DAYS == 75


def test_thin_fresh_high_pool_stays_low_confidence():
    # Watch-item guard (BUI-289): the freshest comp being the high one raises
    # the bid cap, but the 2-comp pool is still surfaced as LOW confidence — the
    # signal that drives the 0.60 haircut in the graded flow — and its ratio
    # stays under the BUI-179 too_sparse flag. No extra thin-pool guard is
    # warranted; special-casing it would reintroduce the complexity U2 avoided.
    _n, comps, grade = _by_name("thin_2comp_fresh_high__watch_item")
    w = fmv_math.compute_fmv(comps, target_grade=grade)
    u = fmv_math.compute_fmv(_strip_dates(comps), target_grade=grade)
    assert w["fmv_high"] > u["fmv_high"]        # cap does rise...
    assert w["confidence"] == "LOW"             # ...but is surfaced as LOW
    assert w["flag_reason"] is None             # 1.5x ratio is under too_sparse


def test_stale_pool_no_longer_earns_high_confidence():
    # Confidence reconciliation (R6/R7): a raw-count-8 pool that is entirely
    # stale must not claim HIGH — effective_n is what drives the label now.
    _n, comps, grade = _by_name("stale_high_count__confidence")
    w = fmv_math.compute_fmv(comps, target_grade=grade)
    u = fmv_math.compute_fmv(_strip_dates(comps), target_grade=grade)
    assert u["confidence"] == "HIGH"
    assert w["confidence"] != "HIGH"
    assert w["effective_n"] < w["n"]


def _regen() -> None:
    data = {c[0]: _run(c) for c in CASES}
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINE} ({len(data)} cases)")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
    else:
        print("pass --regen to (re)write the baseline")
