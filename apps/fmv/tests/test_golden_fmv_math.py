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
from datetime import date, timedelta
from pathlib import Path

import pytest

import fmv_math

BASELINE = Path(__file__).parent / "fixtures" / "fmv_math_golden.json"
# BUI-930: the graded (slab) mode's own baseline, in its OWN file. Kept apart
# from the raw one on purpose — the ticket's acceptance criterion is "no raw
# golden fixture changes", and a shared file makes every graded regeneration a
# diff against the raw rows an operator then has to read past.
GRADED_BASELINE = Path(__file__).parent / "fixtures" / "fmv_math_graded_golden.json"

# The frozen keys (discrete / clean-rounded — no raw floats whose precision
# would make the baseline brittle). ungraded_anchor (BUI-522) is a small
# {median, n} dict of clean values, frozen so the anchor is regression-guarded
# alongside the priced range.
_FROZEN = (
    "n", "window", "flag_reason", "grade_span", "fmv_low", "fmv_high",
    "median", "max_bid", "confidence", "bid_factor", "ungraded_anchor",
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
    # BUI-528: a non-degenerate pool whose quartiles clean-round to a single
    # point ($50/$50) is reopened into a real range instead of a false point
    # estimate. Prices differ (cv>0), so the range is split one clean step apart.
    ("collapsed_range_reopened",
     [(49, 9.0), (50, 9.0), (51, 9.0)], 9.0, None, None),
    # BUI-528 carve-out: a GENUINELY degenerate pool (identical prices, cv==0) is
    # left a true point — no dispersion to reveal, so no fabricated range.
    ("degenerate_identical_priced",
     [(50, 9.0), (50, 9.0), (50, 9.0)], 9.0, None, None),
    # BUI-522: grade-less comps (grade None) are dropped from the priced pool but
    # surface as the ungraded-market anchor ({median, n}); the graded pool prices
    # normally off the (100,105,110) comps.
    ("ungraded_anchor_from_dropped_comps",
     [(100, 9.0), (105, 9.0), (110, 9.0),
      (40, None), (50, None), (60, None)], 9.0, None, None),
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


# ─── Graded (slab) mode — BUI-930 ────────────────────────────────────────────
#
# Pinned BEFORE the runner was wired (plan U7's execution note), so the money
# decision — which tier priced the book, and what it capped the bid at — is
# frozen independently of the fetch that feeds it. Several rows are the real
# 2026-09-21 spike pools, named so.

# Every comp age is measured from this fixed date, so the fixture weighs the
# same today as in a year. Since BUI-948 that holds because `_run_graded`
# passes this same date as `graded_pool`'s `as_of` — the module reads no clock
# of its own, so pinning both ends of the subtraction pins the weights.
_SLAB_REF = date(2026, 9, 1)

_GRADED_FROZEN = (
    "n", "effective_n", "flag_reason", "fmv_low", "fmv_high", "median",
    "max_bid", "confidence", "bid_factor", "pricing_basis",
    "envelope_clamped", "page_quality_fallback", "pool_n",
    "pool_undated_dropped", "pool_stale_dropped", "exact_effective_n",
)


def _slab(rows):
    """(price, grade, age_days, page_quality) -> slab comp dicts.

    `age_days is None` means an UNDATED comp (neither `sold_date` nor
    `first_seen_at`), which the graded pool excludes outright. A negative
    marker is not needed: an age past 365 days is simply given as such.
    """
    out = []
    for i, row in enumerate(rows):
        price, grade, age = row[0], row[1], row[2]
        pq = row[3] if len(row) > 3 else "unknown"
        comp = {"product_id": f"slab{i}", "title": f"CGC {grade}",
                "price": price, "grade": grade, "page_quality": pq,
                "certifier": "cgc", "label": "universal"}
        if age is not None:
            comp["sold_date"] = (_SLAB_REF - timedelta(days=age)).isoformat()
        out.append(comp)
    return out


# (name, comps, target_grade, page_quality)
GRADED_CASES = [
    # AE2 — six live sales at the exact grade. Direct, rubric confidence, no
    # clamp (effective n is past OUTLIER_ROBUST_BUCKET_N).
    ("ae2_direct_six_exact_sales",
     _slab([(1200, 9.8, 5), (1199.95, 9.8, 12), (1269.49, 9.8, 20),
            (1225, 9.8, 8), (1250, 9.8, 30), (1180, 9.8, 2),
            (605, 9.6, 5), (550, 9.6, 9), (524, 9.6, 14),
            (387, 9.4, 20), (380, 9.4, 25)]), 9.8, None),

    # AE4, first half — the real Fantastic Four #48 spike pool. Two $2,000
    # sales at 7.0 with $1,425 (6.5) and $2,000 (7.5) rungs: direct, and the
    # envelope clamp pulls the band BELOW both actual sales.
    ("ae4_direct_two_sales_envelope_clamped",
     _slab([(2000, 7.0, 18), (2000, 7.0, 1), (1425, 6.5, 12),
            (2000, 7.5, 41), (1152, 6.0, 12), (755, 4.5, 46),
            (650, 5.0, 28), (649, 3.5, 12), (600, 3.5, 50),
            (402, 2.5, 12), (365, 1.5, 12), (10500, 9.4, 18)]), 7.0, None),

    # AE4, second half — THE money case. One 4.5 sale at $700, priced BELOW
    # the $900 4.0 rung. The answer must be the 4.0->5.5 interpolation
    # ($1,067 -> $1,075 clean), never the $700 sale, and it must carry LOW
    # and the 0.60 cap.
    ("ae4_ladder_lone_exact_sale_is_never_the_price",
     _slab([(700, 4.5, 3), (500, 2.5, 60), (900, 4.0, 40),
            (1400, 5.5, 50), (1500, 6.0, 55)]), 4.5, None),

    # Effective n 1.5 (one live sale + one 120-day ledger sale) is BELOW the
    # exact tier's floor, so the same pool that would price directly on two
    # live sales goes to the ladder instead.
    ("effective_n_one_and_a_half_goes_to_ladder",
     _slab([(2000, 7.0, 0), (1900, 7.0, 120), (1425, 6.5, 10),
            (2000, 7.5, 10), (1150, 6.0, 10)]), 7.0, None),

    # Effective n 2.5 (two live + one 120-day) clears the floor: direct, and
    # still clamped because 2.5 < OUTLIER_ROBUST_BUCKET_N.
    ("effective_n_two_and_a_half_is_direct_clamped",
     _slab([(2000, 7.0, 0), (2000, 7.0, 3), (1900, 7.0, 120),
            (1425, 6.5, 10), (2000, 7.5, 10), (1150, 6.0, 10)]), 7.0, None),

    # An UNDATED comp and a 400-day comp are both dropped before anything is
    # counted; a 200-day one joins at half weight.
    ("pool_drops_undated_and_over_a_year",
     _slab([(3000, 9.4, 5), (3100, 9.4, 10), (9999, 9.4, None),
            (8888, 9.4, 400), (2800, 9.4, 200),
            (2500, 9.2, 10), (3500, 9.6, 10)]), 9.4, None),

    # R30 — a 9.4 target is interpolated between 9.2 and 9.6 and is NEVER
    # pooled with the 9.6 sales, however many of them there are.
    ("target_94_interpolates_never_pools_96",
     _slab([(2000, 8.5, 20), (2500, 9.2, 15), (3500, 9.6, 10),
            (3600, 9.6, 12), (5000, 9.8, 8)]), 9.4, None),

    # The three ladder refusals.
    ("refuses_outside_ladder",
     _slab([(1000, 9.0, 10), (1200, 9.2, 10), (1400, 9.4, 10),
            (1800, 9.6, 10)]), 9.8, None),
    ("refuses_ladder_too_thin",
     _slab([(2500, 9.2, 10), (3500, 9.6, 10)]), 9.4, None),
    ("refuses_ladder_non_monotone",
     _slab([(2000, 8.5, 20), (3000, 9.2, 15), (2500, 9.6, 10),
            (4000, 9.8, 8)]), 9.4, None),

    # Page quality: two same-quality comps are enough to scope the pool to
    # them; one is not, and the fallback is recorded. BUI-937 moved the first
    # row's money: the band is still the two white sales ($3,000/$3,200), but
    # the clamp now reads the rungs scoping excluded (a $900 9.2 and a $1,500
    # 9.6), whose envelope bounds 9.4 at $1,200 — so the cap fell from $2,525
    # to $950. That is the point of the change, not a side effect: before it,
    # a scoped pool one rung wide had no envelope at all and a two-sale bucket
    # set a four-figure cap on nothing but its own two listings.
    ("page_quality_prefers_two_matching",
     _slab([(3000, 9.4, 5, "white"), (3200, 9.4, 6, "white"),
            (1000, 9.4, 7, "ow_w"), (1100, 9.4, 8, "ow_w"),
            (900, 9.2, 9, "ow_w"), (1500, 9.6, 10, "ow_w")]), 9.4, "white"),
    ("page_quality_falls_back_on_a_single_match",
     _slab([(3000, 9.4, 5, "white"),
            (1000, 9.4, 7, "ow_w"), (1100, 9.4, 8, "ow_w"),
            (900, 9.2, 9, "ow_w"), (1500, 9.6, 10, "ow_w")]), 9.4, "white"),

    # Nothing survives the age filter -> no pool at all, not a thin one.
    ("refuses_no_certifier_pool_when_every_comp_is_undated",
     _slab([(3000, 9.4, None), (2500, 9.2, None)]), 9.4, None),

    # The gap the envelope clamp cannot reach: a 2-sale exact bucket at the
    # TOP of the ladder has no rung above it to be bounded by. Two sales five
    # times apart are not one market, so the book is refused rather than
    # capped off the higher one (BUI-179's guard, BUI-930's placement).
    ("refuses_too_sparse_unclamped_divergent_pair",
     _slab([(1000, 9.8, 5), (5000, 9.8, 9), (2000, 9.6, 10),
            (1500, 9.4, 10)]), 9.8, None),
    # ... and the same shape with the two sales in agreement still prices,
    # so the guard is a dispersion test and not a ban on thin top rungs.
    ("unclamped_pair_in_agreement_still_prices_direct",
     _slab([(4800, 9.8, 5), (5000, 9.8, 9), (2000, 9.6, 10),
            (1500, 9.4, 10)]), 9.8, None),
]


def _run_graded(case) -> dict:
    _name, comps, grade, pq = case
    # BUI-948: `as_of` is pinned to `_SLAB_REF`, the same date `_slab` measures
    # every case's `age_days` back from. That is what makes the baseline a
    # frozen artifact rather than one that drifts with the calendar — and it
    # holds each case's answer exactly where it was before as_of existed,
    # because no case spans more than 90 days between its newest comp and
    # `_SLAB_REF`.
    out = fmv_math.graded_fmv(comps, grade, certifier="cgc", label="universal",
                              page_quality=pq, as_of=_SLAB_REF)
    return {k: out[k] for k in _GRADED_FROZEN}


def _load_graded_baseline() -> dict:
    return json.loads(GRADED_BASELINE.read_text())


@pytest.mark.parametrize("case", GRADED_CASES, ids=[c[0] for c in GRADED_CASES])
def test_graded_fmv_matches_golden(case):
    baseline = _load_graded_baseline()
    name = case[0]
    assert name in baseline, f"no graded golden baseline for {name!r} — regenerate"
    assert _run_graded(case) == baseline[name]


def test_every_graded_case_has_a_baseline_entry():
    baseline = _load_graded_baseline()
    assert {c[0] for c in GRADED_CASES} == set(baseline), (
        "GRADED_CASES and the graded golden baseline have diverged — regenerate"
    )


def test_the_lone_exact_sale_is_not_the_golden_price():
    """The one assertion the golden dict cannot make on its own.

    `ae4_ladder_lone_exact_sale_is_never_the_price` would still pass its
    frozen comparison if a regression made the lone $700 sale the answer and
    the baseline were regenerated alongside it. Naming the forbidden value
    here means the regeneration cannot quietly bless it.
    """
    baseline = _load_graded_baseline()["ae4_ladder_lone_exact_sale_is_never_the_price"]
    assert baseline["pricing_basis"] == "ladder"
    assert baseline["fmv_high"] != 700
    assert baseline["fmv_high"] > 900, (
        "the interpolated price must sit between the 4.0 and 5.5 rungs, not "
        "at or below the lone 4.5 sale"
    )
    assert baseline["confidence"] == "LOW"
    assert baseline["bid_factor"] == 0.60
    assert baseline["max_bid"] == fmv_math.clean_round(
        baseline["fmv_high"] * 0.60)


def _regen() -> None:
    data = {c[0]: _run(c) for c in CASES}
    BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINE} ({len(data)} cases)")
    graded = {c[0]: _run_graded(c) for c in GRADED_CASES}
    GRADED_BASELINE.write_text(json.dumps(graded, indent=2, sort_keys=True) + "\n")
    print(f"wrote {GRADED_BASELINE} ({len(graded)} cases)")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
    else:
        print("pass --regen to (re)write the baseline")
