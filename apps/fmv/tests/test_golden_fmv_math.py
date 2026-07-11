"""Golden-fixture regression for fmv_math.compute_fmv (BUI-190).

Freezes (comps, grade, window, grade_confidence) → (n, window, flag_reason,
fmv_low, fmv_high, median, max_bid, confidence, bid_factor) against a baseline
committed in fixtures/fmv_math_golden.json, so any change to the IQR/quartile
math, the confidence rubric, the wide-window cap, the BUI-179 2-comp guard, or
the BUI-51 haircut diffs visibly against the baseline instead of silently
shifting a bid cap.

Regenerate the baseline ONLY on an intended change:
    python tests/test_golden_fmv_math.py --regen
and review the JSON diff before committing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import fmv_math

BASELINE = Path(__file__).parent / "fixtures" / "fmv_math_golden.json"

# The frozen keys (discrete / clean-rounded — no raw floats whose precision
# would make the baseline brittle).
_FROZEN = (
    "n", "window", "flag_reason", "grade_span", "fmv_low", "fmv_high",
    "median", "max_bid", "confidence", "bid_factor",
)


def _comps(pairs):
    return [{"price": p, "grade": g, "product_id": f"id{i}", "title": ""}
            for i, (p, g) in enumerate(pairs)]


# (name, comps[(price, grade)], target_grade, max_window, grade_confidence)
CASES = [
    ("narrow_high",
     [(100, 9.0), (105, 9.0), (110, 9.0), (115, 9.0), (120, 9.0),
      (125, 9.0), (130, 9.0), (135, 9.0), (140, 9.0)], 9.0, None, None),
    ("wide_window_caps_medium",
     [(100, 7.0), (105, 7.0), (110, 7.0), (115, 7.0)]
     + [(120, 8.5), (125, 8.5), (130, 8.5), (135, 8.5), (140, 8.5)], 7.0, None, None),
    ("one_sided_flag",
     [(40, 9.0), (42, 9.0), (44, 9.0), (45, 9.0), (41, 9.0)], 9.6, None, None),
    # BUI-318: a single-comp bracket (5.0 has one comp) is too thin to anchor an
    # interpolation → suppressed, stays too_wide/needs_manual (no bid).
    ("too_wide_flag",
     [(50, 5.0), (300, 9.0), (320, 9.0)], 7.0, None, None),
    # BUI-318: both brackets carry ≥2 comps → interpolates to $180, carried at
    # the interpolated-LOW haircut (0.60× → max_bid 110). Freezes the §7 priced
    # path so the interpolated bid cap can't silently drift.
    ("interpolated_thick_brackets",
     [(40, 5.0), (60, 5.0), (300, 9.0), (320, 9.0)], 7.0, None, None),
    ("too_sparse_single_comp",
     [(100, 7.0)], 7.0, None, None),
    ("two_comp_wild_outlier",
     [(10, 9.0), (5000, 9.0)], 9.0, None, None),
    ("two_comp_tight",
     [(40, 9.0), (55, 9.0)], 9.0, None, None),
    ("grade_haircut_low_confidence",
     [(100, 9.0), (105, 9.0), (110, 9.0), (115, 9.0), (120, 9.0),
      (125, 9.0), (130, 9.0), (135, 9.0)], 9.0, None, "low"),
    ("bracketed_priced",
     [(100, 6.5), (110, 7.0), (120, 7.0), (130, 7.5), (140, 7.0)], 7.0, None, None),
]


def _run(case) -> dict:
    _name, pairs, grade, window, gconf = case
    out = fmv_math.compute_fmv(
        _comps(pairs), target_grade=grade,
        grade_confidence=gconf, max_window=window,
    )
    return {k: out[k] for k in _FROZEN}


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_fmv_math_matches_golden(case):
    baseline = _load_baseline()
    name = case[0]
    assert name in baseline, f"no golden baseline for {name!r} — regenerate"
    assert _run(case) == baseline[name]


def test_every_case_has_a_baseline_entry():
    """Guard against a silently-dropped golden entry."""
    baseline = _load_baseline()
    assert {c[0] for c in CASES} == set(baseline), (
        "CASES and the golden baseline have diverged — regenerate"
    )


def _regen() -> None:
    data = {c[0]: _run(c) for c in CASES}
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINE} ({len(data)} cases)")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
    else:
        print("pass --regen to (re)write the baseline")
