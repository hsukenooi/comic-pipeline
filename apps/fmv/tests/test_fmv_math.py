"""Tests for fmv_math.py — pure functions, no I/O, no network."""

import statistics
from datetime import date, timedelta

import pytest

import fmv_math as fm


def _comp(price, grade=None):
    return {"price": price, "grade": grade}


def _dated_comp(price, grade, sold_date):
    return {"price": price, "grade": grade, "sold_date": sold_date}


# ─── build_pool ────────────────────────────────────────────────────────────────

def _prices(pool):
    """build_pool now returns comp dicts; pull prices for assertions."""
    return sorted(c["price"] for c in pool)


class TestBuildPool:
    def test_narrow_window_when_dense(self):
        comps = [_comp(p, 9.2) for p in [10, 12, 11, 13, 14, 15]]  # all at 9.2
        pool, window = fm.build_pool(comps, target_grade=9.2)
        assert window == 0.5
        assert _prices(pool) == [10, 11, 12, 13, 14, 15]

    def test_widens_when_sparse(self):
        # Only 2 comps at ±0.5 — should widen to ±1.0
        comps = [_comp(10, 9.2), _comp(11, 9.2),
                 _comp(20, 8.0), _comp(21, 8.0), _comp(22, 8.0)]
        pool, window = fm.build_pool(comps, target_grade=9.0)
        assert window == 1.0
        assert len(pool) == 5

    def test_progressive_widen_past_one(self):
        # 4 comps reachable only at ±1.5 — must keep widening past ±1.0
        comps = [_comp(10, 7.0), _comp(11, 6.0), _comp(12, 8.0),
                 _comp(13, 5.5), _comp(14, 8.5)]
        pool, window = fm.build_pool(comps, target_grade=7.0)
        assert window == 1.5
        assert len(pool) == 5

    def test_stops_at_ceiling(self):
        # Never enough comps — widening stops at MAX_GRADE_WINDOW
        comps = [_comp(10, 7.0), _comp(11, 9.0)]
        pool, window = fm.build_pool(comps, target_grade=7.0)
        assert window == fm.MAX_GRADE_WINDOW

    def test_max_window_override(self):
        # A lower ceiling caps reach
        comps = [_comp(10, 7.0), _comp(11, 8.0)]
        _, window = fm.build_pool(comps, target_grade=7.0, max_window=0.5)
        assert window == 0.5

    def test_non_step_aligned_ceiling_not_overshot(self):
        # max_window=1.3 must cap at ±1.3, never step to ±1.5 and pull in a 1.4-away comp
        comps = [_comp(10, 7.0)] + [_comp(20 + i, 5.6) for i in range(6)]  # 5.6 is 1.4 away
        pool, window = fm.build_pool(comps, target_grade=7.0, max_window=1.3)
        assert window == 1.3
        assert all(c["grade"] != 5.6 for c in pool)  # 1.4-away comp excluded

    def test_sub_default_ceiling_respected(self):
        # max_window below ±0.5 must not silently widen to ±0.5
        comps = [_comp(10, 7.0), _comp(11, 7.4)]  # 7.4 is 0.4 away
        pool, window = fm.build_pool(comps, target_grade=7.0, max_window=0.3)
        assert window == 0.3
        assert all(c["grade"] != 7.4 for c in pool)

    def test_returns_comp_dicts(self):
        comps = [_comp(10, 9.2)]
        pool, _ = fm.build_pool(comps, target_grade=9.2)
        assert pool and isinstance(pool[0], dict) and pool[0]["grade"] == 9.2

    def test_drops_no_grade(self):
        comps = [_comp(10, 9.2), _comp(99, None)]  # 99 has no grade
        pool, _ = fm.build_pool(comps, target_grade=9.2)
        assert 99 not in _prices(pool)


# ─── iqr_trim ──────────────────────────────────────────────────────────────────

class TestIqrTrim:
    def test_drops_high_outlier(self):
        # 5 values ~$10–15 plus one at $200
        prices = [10, 11, 12, 13, 14, 200]
        trimmed = fm.iqr_trim(prices)
        assert 200 not in trimmed
        assert all(p <= 50 for p in trimmed)

    def test_keeps_in_band(self):
        prices = [10, 11, 12, 13, 14]
        assert fm.iqr_trim(prices) == sorted(prices)

    def test_passthrough_for_small_n(self):
        # n<3: can't compute quartiles meaningfully
        assert fm.iqr_trim([5, 100]) == [5, 100]
        assert fm.iqr_trim([42]) == [42]


# ─── quartile ──────────────────────────────────────────────────────────────────

class TestQuartile:
    def test_q25_q75_simple(self):
        # For 1..9 inclusive method gives Q25=2.5, Q75=7.5 approximately
        prices = list(range(1, 10))
        q25 = fm.quartile(prices, 0.25)
        q75 = fm.quartile(prices, 0.75)
        assert 2 <= q25 <= 3
        assert 7 <= q75 <= 8

    def test_median(self):
        prices = [10, 20, 30]
        # 0.50 quantile via 99-cut might land off-center; just check it's within range
        q50 = fm.quartile(prices, 0.50)
        assert 15 <= q50 <= 25


# ─── cv ────────────────────────────────────────────────────────────────────────

class TestCv:
    def test_low_cv(self):
        v = fm.cv([100, 100, 100, 100])
        assert v == 0

    def test_high_cv(self):
        v = fm.cv([10, 20, 30, 100])
        assert v is not None and v > 0.5

    def test_none_for_n1(self):
        assert fm.cv([42]) is None


# ─── confidence_label ─────────────────────────────────────────────────────────

class TestConfidence:
    @pytest.mark.parametrize("n,cv,expected", [
        (10, 0.20, "HIGH"),
        (8, 0.24, "HIGH"),
        (6, 0.28, "HIGH"),
        (5, 0.30, "MEDIUM-HIGH"),
        (4, 0.40, "MEDIUM"),
        (3, 0.99, "MEDIUM-LOW"),
        (2, 0.10, "LOW"),
        (1, None, "LOW"),
    ])
    def test_rubric(self, n, cv, expected):
        assert fm.confidence_label(n, cv) == expected

    def test_high_cv_demotes_high_n(self):
        # n=10 but CV=80% should NOT be HIGH
        assert fm.confidence_label(10, 0.80) != "HIGH"


# ─── clean_round ──────────────────────────────────────────────────────────────

class TestCleanRound:
    @pytest.mark.parametrize("v,expected", [
        # A quartile below half the $5 step rounds to $0 — a stored low of $0 on a
        # cheap book is this step ladder, not comp pollution (BUI-717).
        (2.49, 0),
        (3.51, 5), (4.99, 5), (12.0, 10), (47.5, 50),
        (60, 60), (135, 140), (172, 170),
        (210, 200), (255, 250), (810, 800),
    ])
    def test_rounding(self, v, expected):
        assert fm.clean_round(v) == expected


# ─── compute_fmv (end-to-end) ─────────────────────────────────────────────────

class TestComputeFmv:
    def test_high_confidence_dense_pool(self):
        comps = [_comp(p, 8.0) for p in [100, 110, 120, 130, 140, 150, 160, 170, 180]]
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert out["n"] == 9
        assert out["confidence"] == "HIGH"
        assert out["window"] == 0.5
        assert out["fmv_low"] is not None
        assert out["fmv_high"] is not None
        assert out["max_bid"] is not None
        # max_bid should be 80% of fmv_high, clean-rounded
        assert out["max_bid"] <= out["fmv_high"]

    def test_low_confidence_thin_pool(self):
        comps = [_comp(50, 9.2), _comp(60, 9.2)]
        out = fm.compute_fmv(comps, target_grade=9.2)
        assert out["confidence"] == "LOW"
        assert out["n"] == 2

    def test_outlier_dropped_in_iqr(self):
        # 6 in-band + 1 absurd outlier
        comps = [_comp(p, 8.0) for p in [10, 11, 12, 13, 14, 15, 500]]
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert 500 not in out["trimmed_pool"]

    def test_no_pool(self):
        comps = []
        out = fm.compute_fmv(comps, target_grade=9.0)
        assert out["n"] == 0
        assert out["fmv_low"] is None
        assert out["confidence"] == "LOW"
        assert out["flag_reason"] is None  # n=0 is no-comps, not a manual flag
        assert out["grade_span"] is None

    def test_widens_window_when_sparse(self):
        # 2 comps at exact target, 4 within ±1.0
        comps = [_comp(10, 9.0), _comp(11, 9.0),
                 _comp(15, 8.0), _comp(16, 8.0), _comp(17, 8.0), _comp(18, 8.0)]
        out = fm.compute_fmv(comps, target_grade=9.0)
        assert out["window"] == 1.0
        assert out["n"] == 6

    def test_max_bid_is_80_percent(self):
        # Construct a pool where Q75 is exactly $100 → max_bid should be ~80
        comps = [_comp(p, 8.0) for p in [50, 75, 100, 100, 100, 100]]
        out = fm.compute_fmv(comps, target_grade=8.0)
        # Just verify the relationship holds within rounding
        assert out["max_bid"] is not None
        assert out["fmv_high"] is not None
        assert abs(out["max_bid"] - 0.8 * out["fmv_high"]) <= 10


# ─── Priceability guards (BUI-86) ─────────────────────────────────────────────

class TestPriceabilityGuards:
    def test_one_sided_flags(self):
        # FF #63 shape: target 9.6, all comps at/below 9.0 even at ceiling
        comps = [_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        out = fm.compute_fmv(comps, target_grade=9.6)
        assert out["flag_reason"] == "one_sided"
        assert out["fmv_low"] is None and out["fmv_high"] is None
        assert out["max_bid"] is None

    def test_too_wide_bracketed_pool_interpolates(self):
        # BUI-306 §7: Iron Man #124 shape — target 7.0, pool brackets but spans
        # 4 grade points. It used to flag too_wide (no price); now it is priced
        # by interpolation between the 5.0 bucket (median $50) and the 9.0 bucket
        # (median $310): 50 + (7-5)/(9-5)*(310-50) = $180. BUI-318: BOTH brackets
        # carry ≥2 comps (a lone-comp bracket now suppresses), and the max_bid is
        # the interpolated-LOW haircut clean_round(180 × 0.60) = 110, not 0.80×.
        comps = [_comp(40, 5.0), _comp(60, 5.0), _comp(300, 9.0), _comp(320, 9.0)]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["interpolated"] is True
        assert out["flag_reason"] is None      # cleared: now emits a bid-able number
        assert out["grade_span"] == 4.0
        assert out["fmv_low"] == 180 and out["fmv_high"] == 180
        assert out["median"] == 180
        assert out["max_bid"] == 110           # clean_round(180 * 0.60) haircut
        assert out["confidence"] == "LOW"      # §7: confidence reduced

    def test_too_sparse_flags_single_comp(self):
        # A lone comp no longer emits a point estimate — it flags
        comps = [_comp(100, 7.0)]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["flag_reason"] == "too_sparse"
        assert out["fmv_low"] is None and out["max_bid"] is None

    def test_two_comp_wild_outlier_is_flagged(self):
        # BUI-179: [$10, $5000-mistagged-slab] at the target grade is neither
        # IQR-trimmed (len<3) nor too_sparse (n=2) — it must flag, not price a
        # wild Q75 into an 0.80×high overpay.
        comps = [_comp(10, 9.0), _comp(5000, 9.0)]
        out = fm.compute_fmv(comps, target_grade=9.0)
        assert out["flag_reason"] == "too_sparse"
        assert out["fmv_high"] is None and out["max_bid"] is None

    def test_two_comp_reasonable_spread_is_priced(self):
        # A tight 2-comp pool (within SMALL_POOL_MAX_RATIO) still prices.
        comps = [_comp(40, 9.0), _comp(55, 9.0)]
        out = fm.compute_fmv(comps, target_grade=9.0)
        assert out["flag_reason"] is None
        assert out["fmv_high"] is not None and out["max_bid"] is not None

    def test_guard_precedence_sparse_before_one_sided(self):
        # A single comp that is also one-sided → sparse wins (documented order)
        comps = [_comp(100, 8.0)]
        out = fm.compute_fmv(comps, target_grade=9.6)
        assert out["flag_reason"] == "too_sparse"

    def test_bracketed_bounded_prices(self):
        # Bracketed, span within threshold, enough comps → priced
        comps = [_comp(p, g) for p, g in
                 [(100, 6.5), (110, 7.0), (120, 7.0), (130, 7.5), (140, 7.0)]]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["flag_reason"] is None
        assert out["fmv_low"] is not None and out["fmv_high"] is not None

    def test_wide_window_caps_confidence_at_medium(self):
        # A dense pool that would score HIGH, but built at ±1.5 → capped MEDIUM
        comps = ([_comp(p, 7.0) for p in [100, 105, 110, 115]] +
                 [_comp(p, 8.5) for p in [120, 125, 130, 135, 140]])
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["window"] == 1.5
        assert out["flag_reason"] is None
        assert out["confidence"] == "MEDIUM"

    def test_narrow_window_keeps_high_confidence(self):
        # Same shape of dense pool but all at target → ±0.5, HIGH allowed
        comps = [_comp(p, 7.0) for p in
                 [100, 105, 110, 115, 120, 125, 130, 135, 140]]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["window"] == 0.5
        assert out["confidence"] == "HIGH"

    def test_flagged_dense_pool_forces_low_confidence(self):
        # One-sided but dense+tight: must NOT persist HIGH — forced to LOW
        comps = [_comp(p, 9.0) for p in [40, 41, 42, 43, 44, 45]]
        out = fm.compute_fmv(comps, target_grade=9.6)
        assert out["flag_reason"] == "one_sided"
        assert out["confidence"] == "LOW"

    def test_grade_window_override_does_not_bypass_guard(self):
        # AE4: a higher ceiling reaches further but a one-sided book stays flagged
        comps = [_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        out = fm.compute_fmv(comps, target_grade=9.6, max_window=2.5)
        assert out["flag_reason"] == "one_sided"


# ─── Recency weighting (BUI-287 U2) ───────────────────────────────────────────

class TestParseSoldDate:
    def test_serpapi_free_text(self):
        assert fm._parse_sold_date("Sold Oct 12, 2026") == date(2026, 10, 12)

    def test_iso_first_party(self):
        assert fm._parse_sold_date("2026-06-01T12:34:56Z") == date(2026, 6, 1)

    def test_iso_date_only(self):
        assert fm._parse_sold_date("2026-06-01") == date(2026, 6, 1)

    def test_missing_or_unparseable_is_none(self):
        assert fm._parse_sold_date(None) is None
        assert fm._parse_sold_date("") is None
        assert fm._parse_sold_date("   ") is None
        assert fm._parse_sold_date("garbage") is None
        assert fm._parse_sold_date(12345) is None  # non-string envelope value


class TestRecencyWeight:
    def test_fresh_comp_is_full_weight(self):
        ref = date(2026, 6, 1)
        assert fm._recency_weight(ref, ref) == 1.0

    def test_no_date_is_neutral_weight(self):
        assert fm._recency_weight(None, date(2026, 6, 1)) == 1.0

    def test_one_half_life_older_is_half_weight(self):
        # KTD-7 test scenario: a comp exactly one half-life older contributes
        # ~half the weight of an identical fresh comp.
        ref = date(2026, 6, 1)
        older = ref - timedelta(days=fm.RECENCY_HALF_LIFE_DAYS)
        assert fm._recency_weight(older, ref) == pytest.approx(0.5, rel=1e-9)

    def test_two_half_lives_older_is_quarter_weight(self):
        ref = date(2026, 6, 1)
        older = ref - timedelta(days=2 * fm.RECENCY_HALF_LIFE_DAYS)
        assert fm._recency_weight(older, ref) == pytest.approx(0.25, rel=1e-9)

    def test_future_date_relative_to_reference_is_not_penalized(self):
        # Shouldn't happen (reference is the max date in the pool) but must
        # not produce a weight > 1.0 on a rounding edge case.
        ref = date(2026, 6, 1)
        assert fm._recency_weight(ref + timedelta(days=1), ref) == 1.0


class TestWeightedQuantiles:
    def test_degenerate_no_weights_matches_quartile_exactly(self):
        prices = [100, 110, 120, 130, 140, 150, 160, 170, 180]
        weights = [1.0] * len(prices)
        assert fm.weighted_quartile(prices, weights, 0.25) == fm.quartile(prices, 0.25)
        assert fm.weighted_quartile(prices, weights, 0.75) == fm.quartile(prices, 0.75)

    def test_degenerate_equal_nonunit_weights_matches_quartile_exactly(self):
        # Equal but not literally 1.0 — still degenerate (all-same-date pool).
        prices = [10, 20, 30, 40]
        weights = [0.4, 0.4, 0.4, 0.4]
        assert fm.weighted_quartile(prices, weights, 0.5) == fm.quartile(prices, 0.5)

    def test_degenerate_median_matches_statistics_median_exactly(self):
        prices = [10, 20, 30, 40]
        weights = [1.0, 1.0, 1.0, 1.0]
        assert fm.weighted_median(prices, weights) == statistics.median(prices)

    def test_weighting_direction_recent_high_beats_unweighted_median(self):
        # A recent high price and an old low price: the weighted median must
        # sit ABOVE the unweighted median (the old low comp is discounted).
        prices = [100, 200]
        weights = [0.05, 1.0]  # 100 is stale, 200 is fresh
        unweighted = statistics.median(prices)
        weighted = fm.weighted_median(prices, weights)
        assert weighted > unweighted

    def test_weighting_direction_quartile(self):
        prices = [100, 110, 120, 130, 140]
        weights = [0.05, 0.05, 0.05, 1.0, 1.0]  # low prices stale, high prices fresh
        unweighted_q75 = fm.quartile(prices, 0.75)
        weighted_q75 = fm.weighted_quartile(prices, weights, 0.75)
        assert weighted_q75 >= unweighted_q75

    def test_weighted_median_exact_value_hand_computed(self):
        """FIX 6 (test hardening): the directional tests above only assert
        `weighted > unweighted` — a sign/fraction bug that still moves the
        right way would sail through. Pin an exact value derived from
        `weighted_quartile`'s OWN documented formula (read from fmv_math.py,
        not guessed), so a wrong-but-monotonic implementation fails.

        weighted_quartile's algorithm (unequal-weight branch):
          1. sort (price, weight) pairs by price
          2. cumulative weight `cum` after each pair; total = sum(weights)
          3. each pair's "position" = (cum - weight/2) / total
          4. linearly interpolate `q` between the two bracketing positions

        Pool: prices=[10, 20, 30], weights=[1.0, 1.0, 0.5] (already sorted by
        price; not all-equal, so this exercises the real weighted branch, not
        the equal-weight passthrough to `quartile`/`statistics.median`).

        Hand computation for q=0.5 (weighted_median):
          total = 1.0 + 1.0 + 0.5 = 2.5
          pair0 (10, 1.0):  cum=1.0  -> position = (1.0 - 1.0/2) / 2.5 = 0.5/2.5 = 0.20
          pair1 (20, 1.0):  cum=2.0  -> position = (2.0 - 1.0/2) / 2.5 = 1.5/2.5 = 0.60
          pair2 (30, 0.5):  cum=2.5  -> position = (2.5 - 0.5/2) / 2.5 = 2.25/2.5 = 0.90
          q=0.5 falls between pair0's 0.20 and pair1's 0.60 (positions[1]=0.60
          is the first >= q):
            frac = (0.5 - 0.20) / (0.60 - 0.20) = 0.30 / 0.40 = 0.75
            value = 10 + 0.75 * (20 - 10) = 10 + 7.5 = 17.5
        """
        prices = [10, 20, 30]
        weights = [1.0, 1.0, 0.5]
        assert not fm._weights_equal(weights)  # confirm this hits the real branch
        assert fm.weighted_median(prices, weights) == pytest.approx(17.5, rel=1e-3)
        assert fm.weighted_quartile(prices, weights, 0.5) == pytest.approx(17.5, rel=1e-3)


class TestConfidenceReconciledToEffectiveN:
    def test_many_stale_comps_no_longer_earn_high_on_raw_count(self):
        # 8 near-identical, low-CV prices would earn HIGH under the old
        # raw-count rubric (n>=8, cv<25%). Only one comp is recent; the
        # other 7 are ~2.8 half-lives stale, so effective sample size
        # collapses well below every confidence tier's floor.
        recent = _dated_comp(100, 8.0, "2026-06-01")
        stale = [_dated_comp(101 + i, 8.0, "2025-11-01") for i in range(7)]
        comps = [recent] + stale
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert out["n"] == 8                 # raw trimmed count unchanged
        assert out["effective_n"] < 4        # far below even the MEDIUM floor
        assert out["confidence"] != "HIGH"

    def test_all_fresh_comps_unaffected(self):
        # Sanity: when every comp is equally fresh (same date), effective_n
        # equals raw n and the confidence rubric is untouched.
        comps = [_dated_comp(100 + i, 8.0, "2026-06-01") for i in range(9)]
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert out["effective_n"] == out["n"] == 9
        assert out["confidence"] == "HIGH"


class TestDegenerateBackCompat:
    """R2-critical (BUI-287 U2): a no-date pool and an all-same-date pool
    must each reduce EXACTLY to the pre-U2 unweighted result."""

    _PRICES = [100, 110, 120, 130, 140, 150, 160, 170, 180]

    def test_no_date_pool_matches_unweighted_math_exactly(self):
        comps = [_comp(p, 8.0) for p in self._PRICES]
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert out["effective_n"] == out["n"] == 9

        trimmed = fm.iqr_trim(self._PRICES)
        assert out["fmv_low"] == fm.clean_round(fm.quartile(trimmed, 0.25))
        assert out["fmv_high"] == fm.clean_round(fm.quartile(trimmed, 0.75))
        assert out["median"] == fm.clean_round(statistics.median(trimmed))

    def test_all_same_date_pool_matches_no_date_pool_exactly(self):
        dated_comps = [_dated_comp(p, 8.0, "2026-06-01") for p in self._PRICES]
        undated_comps = [_comp(p, 8.0) for p in self._PRICES]

        dated_out = fm.compute_fmv(dated_comps, target_grade=8.0)
        undated_out = fm.compute_fmv(undated_comps, target_grade=8.0)

        assert dated_out["effective_n"] == dated_out["n"] == 9
        assert dated_out["fmv_low"] == undated_out["fmv_low"]
        assert dated_out["fmv_high"] == undated_out["fmv_high"]
        assert dated_out["median"] == undated_out["median"]
        assert dated_out["confidence"] == undated_out["confidence"]
        assert dated_out["max_bid"] == undated_out["max_bid"]


class TestComputeFmvRecencyIntegration:
    def test_recent_high_pulls_fmv_up_vs_unweighted(self):
        old_low = _dated_comp(100, 8.0, "2025-01-01")
        recent_high = _dated_comp(200, 8.0, "2026-06-01")
        weighted_out = fm.compute_fmv([old_low, recent_high], target_grade=8.0)

        unweighted_out = fm.compute_fmv(
            [_comp(100, 8.0), _comp(200, 8.0)], target_grade=8.0
        )
        assert weighted_out["median"] > unweighted_out["median"]
        assert weighted_out["fmv_high"] > unweighted_out["fmv_high"]


# ─── bid_factor (BUI-51 confidence haircut) ───────────────────────────────────

class TestBidFactor:
    def test_absent_grade_confidence_no_haircut(self):
        # Back-compat: no grade_confidence → standard factor regardless of fmv conf
        assert fm.bid_factor("LOW", None) == fm.BASE_BID_FACTOR
        assert fm.bid_factor("HIGH", None) == fm.BASE_BID_FACTOR

    def test_both_high_standard(self):
        assert fm.bid_factor("HIGH", "high") == fm.BASE_BID_FACTOR

    def test_low_grade_conf_haircuts_even_with_high_fmv(self):
        assert fm.bid_factor("HIGH", "low") == 0.60

    def test_low_fmv_haircuts_when_grade_present(self):
        # Once the grade pipeline is in play, the comp-confidence path still bites
        assert fm.bid_factor("LOW", "high") == 0.60

    def test_takes_more_conservative_axis(self):
        # Symmetric AND pinned to the actual conservative value (MIN → LOW → 0.60),
        # so a MAX/OR regression that kept symmetry would still fail.
        assert fm.bid_factor("HIGH", "low") == 0.60
        assert fm.bid_factor("LOW", "high") == 0.60

    def test_medium_low_combined(self):
        assert fm.bid_factor("MEDIUM-LOW", "high") == 0.70
        assert fm.bid_factor("HIGH", "medium-low") == 0.70

    def test_medium_grade_with_high_fmv_no_haircut(self):
        # A MEDIUM grade paired with a HIGHER axis must NOT haircut — guards the
        # MIN boundary (combined == MEDIUM-LOW, not <= MEDIUM).
        assert fm.bid_factor("HIGH", "medium") == fm.BASE_BID_FACTOR
        assert fm.bid_factor("MEDIUM", "high") == fm.BASE_BID_FACTOR

    def test_medium_no_haircut(self):
        assert fm.bid_factor("MEDIUM", "medium") == fm.BASE_BID_FACTOR

    def test_unknown_fmv_label_no_overhaircut(self):
        # An unrecognized *fmv* label (code-generated, should always be valid)
        # degrades to neutral MEDIUM — no crash, no over-haircut.
        assert fm.bid_factor("WEIRD", "high") == fm.BASE_BID_FACTOR

    def test_unknown_grade_label_fails_conservative(self):
        # An unrecognized *grade* label (LLM-authored, untrusted) leans LOW —
        # bid less when unsure, not the fail-open MEDIUM.
        assert fm.bid_factor("HIGH", "lo") == 0.60
        assert fm.bid_factor("HIGH", "unknown") == 0.60

    def test_non_string_grade_confidence_no_crash(self):
        # A malformed envelope value (number/bool) must not crash; treat as LOW.
        assert fm.bid_factor("HIGH", 1) == 0.60
        assert fm.bid_factor("HIGH", True) == 0.60

    def test_blank_grade_confidence_is_absent(self):
        # Empty/whitespace string is an "absent" encoding → no haircut, like None.
        assert fm.bid_factor("LOW", "") == fm.BASE_BID_FACTOR
        assert fm.bid_factor("LOW", "   ") == fm.BASE_BID_FACTOR


# ─── compute_fmv grade-confidence haircut (BUI-51) ────────────────────────────

class TestComputeFmvGradeConfidence:
    def _dense_high_fmv(self):
        return [_comp(p, 8.0) for p in
                [100, 110, 120, 130, 140, 150, 160, 170, 180]]

    def test_high_grade_high_fmv_unchanged(self):
        out = fm.compute_fmv(self._dense_high_fmv(), 8.0, grade_confidence="high")
        assert out["confidence"] == "HIGH"
        assert out["bid_factor"] == fm.BASE_BID_FACTOR
        assert out["max_bid"] == fm.clean_round(out["fmv_high"] * fm.BASE_BID_FACTOR)

    def test_low_grade_haircuts_below_baseline(self):
        out = fm.compute_fmv(self._dense_high_fmv(), 8.0, grade_confidence="low")
        assert out["bid_factor"] == 0.60
        assert out["max_bid"] == fm.clean_round(out["fmv_high"] * 0.60)
        baseline = fm.compute_fmv(self._dense_high_fmv(), 8.0)  # no grade conf
        assert out["max_bid"] < baseline["max_bid"]

    def test_absent_grade_confidence_backcompat(self):
        # Thin pool → LOW fmv confidence, but absent grade_confidence keeps 0.80
        thin = [_comp(50, 9.2), _comp(60, 9.2)]
        out = fm.compute_fmv(thin, 9.2)
        assert out["confidence"] == "LOW"
        assert out["bid_factor"] == fm.BASE_BID_FACTOR
        assert out["grade_confidence"] is None

    def test_low_fmv_haircuts_once_grade_present(self):
        thin = [_comp(50, 9.2), _comp(60, 9.2)]
        out = fm.compute_fmv(thin, 9.2, grade_confidence="high")
        assert out["confidence"] == "LOW"
        assert out["bid_factor"] == 0.60

    def test_n0_no_crash(self):
        out = fm.compute_fmv([], 9.0, grade_confidence="low")
        assert out["max_bid"] is None
        assert out["fmv_high"] is None
        assert out["bid_factor"] == 0.60  # factor still computed; just no bid to apply it to

    def test_grade_confidence_echoed(self):
        out = fm.compute_fmv(self._dense_high_fmv(), 8.0, grade_confidence="medium")
        assert out["grade_confidence"] == "medium"

    def test_medium_low_grade_haircuts_to_070(self):
        # MEDIUM-LOW now survives the handoff (not collapsed to low) → 0.70 tier.
        out = fm.compute_fmv(self._dense_high_fmv(), 8.0,
                             grade_confidence="medium-low")
        assert out["bid_factor"] == 0.70
        assert out["max_bid"] == fm.clean_round(out["fmv_high"] * 0.70)


# ─── Grade-curve interpolation + monotonicity (BUI-306, fmv.md §5/§7) ─────────

class TestBucketMedians:
    def test_one_median_per_grade(self):
        comps = [_comp(100, 7.0), _comp(120, 7.0), _comp(300, 9.0)]
        assert fm.bucket_medians(comps) == {7.0: 110.0, 9.0: 300.0}

    def test_ignores_gradeless_and_priceless(self):
        comps = [_comp(100, 7.0), _comp(None, 7.0), {"grade": 8.0}]
        assert fm.bucket_medians(comps) == {7.0: 100.0}


class TestBucketCounts:
    def test_counts_per_grade(self):
        comps = [_comp(100, 7.0), _comp(120, 7.0), _comp(300, 9.0)]
        assert fm.bucket_counts(comps) == {7.0: 2, 9.0: 1}

    def test_ignores_gradeless_and_priceless(self):
        # Same drop rule as bucket_medians so the two dicts share keys.
        comps = [_comp(100, 7.0), _comp(None, 7.0), {"grade": 8.0}]
        assert fm.bucket_counts(comps) == {7.0: 1}


class TestMonotonicityViolations:
    def test_rising_curve_has_no_violation(self):
        assert fm.monotonicity_violations({4.0: 50, 6.0: 80, 9.0: 300}) == []

    def test_single_bucket_never_violates(self):
        assert fm.monotonicity_violations({7.0: 100}) == []

    def test_inversion_is_flagged(self):
        # 7.0 median exceeds 8.5 median — the Nick Fury #17 shape.
        assert fm.monotonicity_violations({7.0: 300, 8.5: 200}) == [(7.0, 8.5)]


class TestCrossGradeInversions:
    def test_motivating_case_x_men_83(self):
        """BUI-583's own example: 4.0 = $35-70 (mid 52.5) vs 7.0 = $5-45
        (mid 25). The bands OVERLAP, so only a midpoint comparison catches it."""
        assert fm.cross_grade_inversions(
            [(4.0, 35.0, 70.0), (7.0, 5.0, 45.0)]) == [(4.0, 7.0)]

    def test_rising_prices_never_invert(self):
        assert fm.cross_grade_inversions(
            [(4.0, 35.0, 70.0), (7.0, 90.0, 140.0), (9.0, 200.0, 260.0)]) == []

    def test_fewer_than_two_priced_rows(self):
        assert fm.cross_grade_inversions([]) == []
        assert fm.cross_grade_inversions([(7.0, 10.0, 20.0)]) == []

    def test_unpriced_rows_are_skipped_not_treated_as_cheap(self):
        """A needs-manual / n=0 stub has no price. An absent price must not
        read as $0 and manufacture an inversion against every other grade."""
        assert fm.cross_grade_inversions(
            [(4.0, 35.0, 70.0), (7.0, None, None), (9.0, None, 45.0)]) == []

    def test_rounding_noise_is_below_the_floor(self):
        """Detective Comics #576 from the live table: 8.5 = $15-30 (mid 22.5)
        vs 9.0 = $15-25 (mid 20). A $2.50 gap is one clean_round half-step —
        exactly the artifact the absolute floor exists to drop."""
        assert fm.cross_grade_inversions(
            [(8.5, 15.0, 30.0), (9.0, 15.0, 25.0)]) == []

    def test_absolute_floor_alone_is_not_enough(self):
        """A $12 gap clears the $10 absolute floor but is only 6% of a $200
        midpoint — under the relative floor, so it does not report."""
        assert fm.cross_grade_inversions(
            [(6.0, 180.0, 220.0), (7.0, 168.0, 208.0)]) == []

    def test_relative_floor_alone_is_not_enough(self):
        """A 40% shortfall on a $10 book is a $4 gap — under the absolute
        floor, because at that price $4 is inside clean_round's $5 step."""
        assert fm.cross_grade_inversions(
            [(6.0, 5.0, 15.0), (7.0, 1.0, 11.0)]) == []

    def test_reports_non_adjacent_pairs(self):
        """Each adjacent step is sub-threshold, but 4.0 -> 9.0 inverts by 28%
        / $28 end to end. Adjacent-only checking would miss this entirely."""
        got = fm.cross_grade_inversions(
            [(4.0, 90.0, 110.0), (6.0, 76.0, 94.0), (9.0, 62.0, 82.0)])
        assert got == [(4.0, 9.0)]

    def test_pairs_are_sorted_and_deduplicated_per_pair(self):
        # Thor #127's live shape: 7.0 undercuts BOTH 5.5 and 6.5.
        got = fm.cross_grade_inversions(
            [(5.5, 40.0, 50.0), (6.5, 30.0, 50.0), (7.0, 25.0, 30.0)])
        assert got == [(5.5, 7.0), (6.5, 7.0)]

    def test_zero_priced_lower_grade_never_divides_by_zero(self):
        assert fm.cross_grade_inversions(
            [(4.0, 0.0, 0.0), (7.0, 0.0, 0.0)]) == []

    def test_thresholds_are_overridable(self):
        rows = [(8.5, 15.0, 30.0), (9.0, 15.0, 25.0)]
        assert fm.cross_grade_inversions(rows) == []
        assert fm.cross_grade_inversions(
            rows, rel_floor=0.05, abs_floor=1.0) == [(8.5, 9.0)]

    def test_advisory_contract_holds_no_price_is_returned(self):
        """The function reports GRADES, never prices — it structurally cannot
        alter or suppress a band, which is BUI-583's hard constraint."""
        got = fm.cross_grade_inversions([(4.0, 35.0, 70.0), (7.0, 5.0, 45.0)])
        assert all(isinstance(g, float) for pair in got for g in pair)
        assert got == [(4.0, 7.0)]


class TestInterpolateGradeCurve:
    def test_exact_linear_interpolation(self):
        # midpoint bracket: 100 + (6-4)/(8-4)*(200-100) = 150 exactly
        got = fm.interpolate_grade_curve({4.0: 100.0, 8.0: 200.0}, 6.0)
        assert got is not None
        assert got["target_price"] == pytest.approx(150.0)
        assert got["grade_below"] == 4.0 and got["grade_above"] == 8.0

    def test_uses_nearest_bracketing_buckets(self):
        # target 7.0 must interpolate off 6.0→9.0 (the nearest bracket), not 4.0.
        got = fm.interpolate_grade_curve({4.0: 100.0, 6.0: 140.0, 9.0: 300.0}, 7.0)
        assert got["grade_below"] == 6.0 and got["grade_above"] == 9.0
        assert got["target_price"] == pytest.approx(140.0 + (1 / 3) * 160.0)

    def test_no_bracket_below_returns_none(self):
        # all buckets above target → extrapolation, not allowed
        assert fm.interpolate_grade_curve({8.0: 100.0, 9.0: 200.0}, 7.0) is None

    def test_no_bracket_above_returns_none(self):
        assert fm.interpolate_grade_curve({5.0: 100.0, 6.0: 200.0}, 7.0) is None

    def test_direct_target_bucket_returns_none(self):
        # A bucket exactly AT the target must not be interpolated across —
        # direct comps beat a smeared bracket (guards the 6× over-bid).
        assert fm.interpolate_grade_curve(
            {5.0: 400.0, 7.0: 105.0, 9.0: 900.0}, 7.0) is None

    # ─── BUI-318 thin-bracket (≥2 comps per bracket) guard ────────────────────

    def test_counts_none_disables_guard(self):
        # Back-compat: no counts map → no thin-bracket filtering (BUI-306 shape).
        got = fm.interpolate_grade_curve({4.0: 100.0, 8.0: 200.0}, 6.0)
        assert got is not None and got["target_price"] == pytest.approx(150.0)

    def test_thin_below_bracket_suppressed(self):
        # 4.0 bracket has a single comp → too thin to anchor → suppress entirely.
        got = fm.interpolate_grade_curve(
            {4.0: 100.0, 8.0: 200.0}, 6.0, counts={4.0: 1, 8.0: 3})
        assert got is None

    def test_thin_above_bracket_suppressed(self):
        got = fm.interpolate_grade_curve(
            {4.0: 100.0, 8.0: 200.0}, 6.0, counts={4.0: 3, 8.0: 1})
        assert got is None

    def test_both_brackets_thick_interpolates(self):
        got = fm.interpolate_grade_curve(
            {4.0: 100.0, 8.0: 200.0}, 6.0, counts={4.0: 2, 8.0: 2})
        assert got is not None and got["target_price"] == pytest.approx(150.0)

    def test_skips_thin_bucket_for_next_eligible(self):
        # Nearest below (6.0) is thin; a thicker 4.0 bucket sits further below.
        # The guard skips the thin bucket and anchors off the eligible 4.0.
        got = fm.interpolate_grade_curve(
            {4.0: 100.0, 6.0: 140.0, 9.0: 300.0}, 7.0,
            counts={4.0: 3, 6.0: 1, 9.0: 3})
        assert got is not None
        assert got["grade_below"] == 4.0 and got["grade_above"] == 9.0

    def test_custom_min_bucket_n_threshold(self):
        got = fm.interpolate_grade_curve(
            {4.0: 100.0, 8.0: 200.0}, 6.0, counts={4.0: 2, 8.0: 2},
            min_bucket_n=3)
        assert got is None


class TestInterpolationInComputeFmv:
    def test_too_wide_bracketed_interpolates_with_exact_value(self):
        # target 6.0, grades 4.0(median $100) & 8.0(median $200): span 4.0 →
        # too_wide, but bracketed → 100 + (6-4)/(8-4)*(200-100) = $150. Both
        # brackets carry ≥2 comps (BUI-318), so it interpolates. max_bid is the
        # interpolated-LOW haircut: clean_round(0.60×150) = 90 (not 0.80×=120).
        comps = [_comp(90, 4.0), _comp(110, 4.0), _comp(190, 8.0), _comp(210, 8.0)]
        out = fm.compute_fmv(comps, target_grade=6.0)
        assert out["interpolated"] is True
        assert out["flag_reason"] is None
        assert out["fmv_low"] == 150 and out["fmv_high"] == 150
        assert out["median"] == 150
        assert out["bid_factor"] == fm.INTERPOLATED_BID_FACTOR
        assert out["max_bid"] == 90
        assert out["confidence"] == "LOW"
        assert out["interpolation"]["grade_below"] == 4.0
        assert out["interpolation"]["grade_above"] == 8.0
        assert out["interpolation"]["target_price"] == pytest.approx(150.0)

    def test_one_sided_pool_stays_needs_manual(self):
        # FF #63 shape: target 9.6, all comps at 9.0 — no bucket above target,
        # so interpolation is impossible and it must stay needs_manual (§7 is
        # never allowed to extrapolate off a one-sided pool).
        comps = [_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        out = fm.compute_fmv(comps, target_grade=9.6)
        assert out["interpolated"] is False
        assert out["interpolation"] is None
        assert out["flag_reason"] == "one_sided"
        assert out["fmv_low"] is None and out["max_bid"] is None

    def test_interpolation_marked_in_output(self):
        # A downstream JSON reader must be able to tell an interpolated value
        # from a real direct comp: the flag + provenance ride the output dict.
        comps = [_comp(90, 4.0), _comp(110, 4.0), _comp(200, 8.0), _comp(200, 8.0)]
        out = fm.compute_fmv(comps, target_grade=6.0)
        assert out["interpolated"] is True
        assert set(out["interpolation"]) == {
            "grade_below", "grade_above", "median_below", "median_above",
            "target_price",
        }

    def test_monotonic_priced_pool_has_no_suspect_and_prices(self):
        # Rising two-bucket curve within span 2.0 → auto-prices normally, no
        # interpolation, no suspect flag (byte-identical behavior to pre-BUI-306).
        comps = ([_comp(p, 7.5) for p in [100, 105, 110]]
                 + [_comp(p, 8.0) for p in [120, 125, 130]])
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert out["interpolated"] is False
        assert out["suspect_buckets"] == []
        assert out["flag_reason"] is None
        assert out["fmv_low"] is not None

    def test_too_wide_with_direct_target_comps_stays_manual(self):
        # MONEY-SAFETY: a too_wide pool that HAS direct comps at the target grade
        # ($100/$110 @7.0) must NOT be re-priced off the distant 5.0/9.0 bracket
        # (that produced a 6× over-bid). It stays needs_manual for the direct /
        # manual path.
        comps = [_comp(400, 5.0), _comp(100, 7.0), _comp(110, 7.0), _comp(900, 9.0)]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["interpolated"] is False
        assert out["flag_reason"] == "too_wide"
        assert out["fmv_high"] is None and out["max_bid"] is None

    def test_two_comp_too_wide_pool_does_not_interpolate(self):
        # MONEY-SAFETY: a 2-comp too_wide pool ([$50@5.0, $5000@9.0]) is never
        # IQR-vetted and its points may be mistagged — interpolating it produced
        # a $2k+ wild cap. The n>=3 floor keeps it manual (BUI-179 parity).
        comps = [_comp(50, 5.0), _comp(5000, 9.0)]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["interpolated"] is False
        assert out["flag_reason"] == "too_wide"
        assert out["fmv_high"] is None and out["max_bid"] is None

    def test_monotonicity_violation_flags_suspect_without_dropping(self):
        # Nick Fury #17 shape: a 7.0 bucket priced ABOVE the 8.5 bucket. The pool
        # still auto-prices (span 1.5, bracketed) — the suspect bucket is FLAGGED,
        # not silently dropped, and the priced number is unaffected by the check.
        comps = ([_comp(p, 7.0) for p in [290, 300, 310]]
                 + [_comp(p, 8.5) for p in [190, 200, 210]])
        out = fm.compute_fmv(comps, target_grade=8.0)
        assert out["suspect_buckets"] == [(7.0, 8.5)]
        assert out["flag_reason"] is None
        assert out["interpolated"] is False
        # price is the blended trimmed-pool quartile, unchanged by the §5 check
        assert out["fmv_low"] is not None and out["fmv_high"] is not None
        assert out["n"] == 6

    # ─── BUI-318 money-safety: thin-bracket suppression + interpolated haircut ──

    def test_single_comp_bracket_suppressed_stays_manual(self):
        # MONEY-SAFETY (BUI-318): a too_wide pool with n>=3 that brackets the
        # target, but whose BELOW bracket is a lone comp ($100 @4.0), must NOT
        # interpolate — a single mistagged comp forming a bracket end is the
        # wild-over-bid path. It stays needs_manual instead of emitting a
        # trusted interpolated value. (The above bucket is thick, so the n>=3
        # floor is satisfied — suppression here is the ≥2-per-bracket rule.)
        comps = [_comp(100, 4.0)] + [_comp(p, 8.0) for p in [190, 200, 210]]
        out = fm.compute_fmv(comps, target_grade=6.0)
        assert out["interpolated"] is False
        assert out["interpolation"] is None
        assert out["flag_reason"] == "too_wide"
        assert out["fmv_high"] is None and out["max_bid"] is None

    def test_thick_brackets_interpolate_and_haircut_without_grade_conf(self):
        # MONEY-SAFETY (BUI-318 residual 1): both brackets carry ≥2 comps so the
        # book interpolates ($150) and is marked LOW/interpolated — and even with
        # NO photo grade_confidence the bid factor is the interpolated-LOW
        # haircut (0.60), not the full 0.80×. A thin single-point estimate never
        # sets a full-confidence bid cap.
        comps = [_comp(90, 4.0), _comp(110, 4.0), _comp(190, 8.0), _comp(210, 8.0)]
        out = fm.compute_fmv(comps, target_grade=6.0, grade_confidence=None)
        assert out["interpolated"] is True
        assert out["confidence"] == "LOW"
        assert out["bid_factor"] == fm.INTERPOLATED_BID_FACTOR
        assert out["bid_factor"] < fm.BASE_BID_FACTOR
        assert out["max_bid"] == fm.clean_round(150 * fm.INTERPOLATED_BID_FACTOR)

    def test_haircut_is_interpolation_specific_not_generic_low(self):
        # Contrast to the above: a NON-interpolated LOW-confidence book with no
        # grade_confidence still bids at the full BASE factor (BUI-51 semantics —
        # fmv-confidence alone never haircuts). This proves the BUI-318 haircut
        # is scoped to interpolated books, not all LOW pools. A tight 2-comp pool
        # (ratio < SMALL_POOL_MAX_RATIO so it isn't flagged) prices at LOW (n<3)
        # yet keeps the 0.80× base factor.
        comps = [_comp(100, 9.0), _comp(120, 9.0)]
        out = fm.compute_fmv(comps, target_grade=9.0, grade_confidence=None)
        assert out["interpolated"] is False
        assert out["flag_reason"] is None          # priced, not needs_manual
        assert out["confidence"] == "LOW"          # n=2, cv defined → LOW rung
        assert out["bid_factor"] == fm.BASE_BID_FACTOR


# ─── CGC-proxy tier (BUI-348) ─────────────────────────────────────────────────

def _slab(price, grade):
    """A graded (slab) comp — same shape as a raw comp, just a certified grade."""
    return {"price": price, "grade": grade}


# The ASM #50 (1967, 1st Kingpin) ladder from the ticket: the incident that
# motivated the tier. eBay CGC sold: 4.0→$636, 5.0→$780-880, 6.5→$1200,
# 7.0→$1800-2143. Hand-priced raw 6.5 band was $600-680.
_ASM50_LADDER = [
    _slab(636, 4.0),
    _slab(780, 5.0), _slab(880, 5.0),
    _slab(1200, 6.5),
    _slab(1800, 7.0), _slab(2143, 7.0),
]


class TestCgcLadderPrice:
    def test_exact_bucket_returned_directly(self):
        ladder = {4.0: 636.0, 6.5: 1200.0, 7.0: 1971.5}
        assert fm.cgc_ladder_price(ladder, 6.5) == 1200.0

    def test_exact_match_preferred_over_interpolation(self):
        # Unlike the raw §7 interpolate_grade_curve (which returns None on an
        # exact match), the ladder USES the exact slab bucket — it's the anchor.
        ladder = {5.0: 800.0, 6.5: 1200.0, 7.0: 2000.0}
        assert fm.cgc_ladder_price(ladder, 6.5) == 1200.0

    def test_interpolates_between_brackets(self):
        ladder = {5.0: 800.0, 7.0: 2000.0}  # target 6.0 → midpoint 1400
        assert fm.cgc_ladder_price(ladder, 6.0) == pytest.approx(1400.0)

    def test_below_ladder_returns_none_no_extrapolation(self):
        ladder = {4.0: 636.0, 6.5: 1200.0}
        assert fm.cgc_ladder_price(ladder, 3.0) is None

    def test_above_ladder_returns_none_no_extrapolation(self):
        ladder = {4.0: 636.0, 6.5: 1200.0}
        assert fm.cgc_ladder_price(ladder, 9.8) is None

    def test_empty_ladder_returns_none(self):
        assert fm.cgc_ladder_price({}, 6.5) is None

    # ── BUI-349 envelope-sanity clamp on a THIN exact bucket ─────────────────
    def test_thin_exact_offtrend_high_clamped_to_envelope(self):
        # Lone (n=1) 6.5 slab at $1900 sits BETWEEN its trustworthy neighbors
        # 5.0 ($830, n=2) and 7.0 ($1971.5, n=2), so it passes the monotonicity
        # guard (830 < 1900 < 1971.5) — yet it is above the linear 5.0–7.0
        # envelope at 6.5 ($1686.125). The clamp caps it at that envelope.
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 1, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == pytest.approx(1686.125)

    def test_thin_exact_below_envelope_unchanged_asm50_class(self):
        # The ASM #50 sparse-key case: a lone 6.5 slab BELOW its bracketing
        # envelope must be used AS-IS (not lifted to the envelope) — the clamp
        # only ever lowers, never raises.
        ladder = {5.0: 830.0, 6.5: 1200.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 1, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 1200.0

    def test_thin_exact_no_eligible_bracket_stays_direct_anchor(self):
        # Off-trend-high lone 6.5, but no trustworthy bucket BELOW it → no
        # envelope to sanity-check against, so the lone slab is used directly
        # (the irreducible sparse-key case the tier must still serve).
        ladder = {6.5: 1900.0, 7.0: 1971.5}
        counts = {6.5: 1, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 1900.0

    def test_thin_exact_bracket_present_but_ineligible_stays_direct_anchor(self):
        # Distinct from the no-bucket case: a below-neighbor EXISTS (5.0) but is
        # itself thin (n=1), so it is ineligible to anchor the envelope → no
        # eligible bracket → the off-trend-high lone 6.5 is used directly. The
        # clamp only bites when TRUSTWORTHY neighbors bracket the target.
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        counts = {5.0: 1, 6.5: 1, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 1900.0

    def test_thin_exact_top_edge_stays_direct_anchor(self):
        # Symmetric to the bottom-edge case: a thin exact bucket at the TOP of
        # the ladder (no bucket above) has no envelope to check against → the
        # lone slab is used directly.
        ladder = {5.0: 830.0, 6.0: 1200.0, 6.5: 1900.0}
        counts = {5.0: 2, 6.0: 2, 6.5: 1}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 1900.0

    def test_robust_exact_bucket_not_clamped(self):
        # A ≥3-comp exact bucket has a genuinely outlier-robust median (n>=3
        # discards an extreme; BUI-355 raised the bar from >=2); leave it as a
        # direct anchor even above the neighbor envelope.
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 3, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 1900.0

    # ── BUI-355: n=2 exact bucket (median-of-2 = mean, zero robustness) ──────
    def test_n2_exact_offtrend_high_clamped_to_envelope(self):
        # The BUI-355 ticket scenario: a monotone ladder (830 < 3100 < 3250
        # passes the monotonicity guard) whose n=2 exact 6.5 bucket hides a
        # $5000 mistag — statistics.median([1200, 5000]) == 3100 is just the
        # MEAN of two, with zero outlier robustness. The trustworthy n=2
        # neighbors imply a 5.0–7.0 envelope of 830 + 0.75*(3250-830) = $2645
        # at 6.5; the clamp bounds the exact value there.
        ladder = {5.0: 830.0, 6.5: 3100.0, 7.0: 3250.0}
        counts = {5.0: 2, 6.5: 2, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == pytest.approx(2645.0)

    def test_n2_exact_below_envelope_unchanged_clamp_only_lowers(self):
        # min(exact, envelope) can only LOWER a cap, never raise one: an n=2
        # exact bucket BELOW its neighbor envelope ($2645 at 6.5) is used
        # as-is, not lifted toward the envelope.
        ladder = {5.0: 830.0, 6.5: 2000.0, 7.0: 3250.0}
        counts = {5.0: 2, 6.5: 2, 7.0: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 2000.0

    def test_raised_min_bucket_n_keeps_clamp_tracking_it(self):
        # The trigger is max(min_bucket_n, OUTLIER_ROBUST_BUCKET_N): a caller
        # demanding stricter anchors (min_bucket_n=4) still clamps an n=3
        # exact bucket, exactly as the pre-BUI-355 `< min_bucket_n` trigger
        # did — the widened threshold must never RAISE a price for ANY caller.
        ladder = {5.0: 830.0, 6.5: 3100.0, 7.0: 3250.0}
        counts = {5.0: 4, 6.5: 3, 7.0: 4}
        assert fm.cgc_ladder_price(
            ladder, 6.5, counts=counts, min_bucket_n=4
        ) == pytest.approx(2645.0)

    def test_n2_exact_no_eligible_bracket_stays_direct_anchor(self):
        # An n=2 exact bucket at the ladder edge (nothing above) has no
        # envelope to check against → used directly, same as the n=1 edge
        # case — BUI-355 must not push unbracketed sparse keys to needs_manual.
        ladder = {5.0: 830.0, 6.5: 3100.0}
        counts = {5.0: 2, 6.5: 2}
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == 3100.0

    def test_no_counts_disables_clamp(self):
        # Without a counts map the caller can't tell thin from thick, so the
        # exact bucket stays a direct anchor (BUI-348 back-compat behavior).
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        assert fm.cgc_ladder_price(ladder, 6.5) == 1900.0


class TestCgcLadderPriceAndClamp:
    """BUI-369: the (price, envelope_clamped) tuple `cgc_proxy_fmv` consumes
    to surface the clamp in notes. `cgc_ladder_price` itself stays a scalar
    (unchanged, still covered by TestCgcLadderPrice above); these tests pin
    the second element of the shared private helper directly."""

    def test_clamp_fires_flag_true(self):
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 1, 7.0: 2}
        price, clamped = fm._cgc_ladder_price_and_clamp(
            ladder, 6.5, counts=counts
        )
        assert price == pytest.approx(1686.125)
        assert clamped is True

    def test_clamp_does_not_fire_below_envelope_flag_false(self):
        # ASM #50 sparse-key shape: thin exact bucket, but BELOW the envelope
        # → min(exact, envelope) returns exact unchanged → nothing to flag.
        ladder = {5.0: 830.0, 6.5: 1200.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 1, 7.0: 2}
        price, clamped = fm._cgc_ladder_price_and_clamp(
            ladder, 6.5, counts=counts
        )
        assert price == 1200.0
        assert clamped is False

    def test_clamp_does_not_fire_robust_bucket_flag_false(self):
        # n>=3 exact bucket is never subject to the clamp check at all.
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 3, 7.0: 2}
        price, clamped = fm._cgc_ladder_price_and_clamp(
            ladder, 6.5, counts=counts
        )
        assert price == 1900.0
        assert clamped is False

    def test_clamp_does_not_fire_no_counts_flag_false(self):
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        price, clamped = fm._cgc_ladder_price_and_clamp(ladder, 6.5)
        assert price == 1900.0
        assert clamped is False

    def test_clamp_does_not_fire_interpolated_path_flag_false(self):
        # No exact bucket at all → interpolation path, never the clamp.
        ladder = {5.0: 800.0, 7.0: 2000.0}
        price, clamped = fm._cgc_ladder_price_and_clamp(ladder, 6.0)
        assert price == pytest.approx(1400.0)
        assert clamped is False

    def test_empty_ladder_flag_false(self):
        assert fm._cgc_ladder_price_and_clamp({}, 6.5) == (None, False)

    def test_cgc_ladder_price_matches_first_element(self):
        # cgc_ladder_price must stay the scalar wrapper — same price either way.
        ladder = {5.0: 830.0, 6.5: 1900.0, 7.0: 1971.5}
        counts = {5.0: 2, 6.5: 1, 7.0: 2}
        price, _ = fm._cgc_ladder_price_and_clamp(ladder, 6.5, counts=counts)
        assert fm.cgc_ladder_price(ladder, 6.5, counts=counts) == price


class TestCgcProxyFmv:
    def test_asm50_band_matches_hand_price(self):
        """AC: ASM #50 raw 6.5 produces a ~$600-680 band from the CGC ladder
        (validated via this synthetic fixture, NOT a live SerpApi pull)."""
        out = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.5)
        assert out is not None
        assert out["cgc_proxy"] is True
        # slab 6.5 = $1200; band = [0.50, 0.55] × 1200 = $600-660 (clean-rounded)
        assert out["fmv_low"] == 600
        assert out["fmv_high"] == 650
        assert 600 <= out["fmv_low"] <= out["fmv_high"] <= 680
        assert out["median"] == 625
        assert out["confidence"] == "MEDIUM-LOW"
        assert out["cgc_ladder"]["slab_price"] == 1200.0

    def test_confidence_capped_medium_low_regardless_of_ladder_size(self):
        big = [_slab(1200, 6.5) for _ in range(40)]
        out = fm.cgc_proxy_fmv(big, target_grade=6.5)
        assert out["confidence"] == "MEDIUM-LOW"  # never HIGH, however many comps

    def test_bid_factor_capped_at_proxy_rung(self):
        out = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.5)
        # No grade_confidence: MEDIUM-LOW label alone would bid 0.80×; the proxy
        # cap pulls it to 0.70 so the label actually constrains the bid.
        assert out["bid_factor"] == fm.CGC_PROXY_BID_FACTOR
        assert out["max_bid"] == fm.clean_round(out["fmv_high"] * fm.CGC_PROXY_BID_FACTOR)

    def test_lower_grade_confidence_wins_over_proxy_cap(self):
        # A present-and-lower photo grade_confidence must still win (min of the
        # two), exactly like the §7 interpolated haircut.
        out = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.5,
                               grade_confidence="low")
        assert out["bid_factor"] == 0.60  # LOW grade_conf < 0.70 proxy cap

    def test_interpolated_target_grade(self):
        # Target 6.0 not in ladder → interpolate the slab price between the
        # nearest bracketing buckets that hold >=2 comps (the thin-bracket money
        # guard). In _ASM50_LADDER only 5.0 (n=2) and 7.0 (n=2) qualify — the
        # single-comp 4.0 and 6.5 buckets can't anchor — so 6.0 brackets 5.0–7.0.
        out = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.0)
        assert out is not None
        slab = out["cgc_ladder"]["slab_price"]
        assert 830.0 < slab < 1971.5  # strictly between the 5.0 and 7.0 medians
        # BUI-369: the clamp only ever applies to an EXACT-bucket price; an
        # interpolated target grade never triggers it.
        assert out["cgc_ladder"]["envelope_clamped"] is False

    def test_interpolation_requires_two_comp_anchors(self):
        # A ladder whose only bracket for the target rests on single-comp buckets
        # can't interpolate (BUI-318 thin-bracket guard) and stays needs_manual.
        # 5.0 (n=1) below, 8.0 (n=1) above, plus a filler 9.8 (n=1) → total>=3 but
        # no >=2-comp bracket around target 6.0.
        thin_brackets = [_slab(800, 5.0), _slab(2000, 8.0), _slab(9000, 9.8)]
        assert fm.cgc_proxy_fmv(thin_brackets, target_grade=6.0) is None

    def test_non_monotonic_ladder_refused(self):
        # A lower grade priced ABOVE a higher grade (a premium/variant/mistagged
        # bucket) makes the ladder non-monotonic → refuse to price (needs_manual),
        # never emit a suspect bid cap. 6.0 median ($1500) > 6.5 median ($1200).
        inverted = [_slab(636, 4.0), _slab(1500, 6.0), _slab(1550, 6.0),
                    _slab(1200, 6.5), _slab(1250, 6.5)]
        assert fm.cgc_proxy_fmv(inverted, target_grade=6.5) is None

    def test_none_when_ladder_too_thin(self):
        assert fm.cgc_proxy_fmv([_slab(1200, 6.5)], target_grade=6.5) is None

    def test_none_when_below_value_floor(self):
        cheap = [_slab(300, 6.5), _slab(310, 6.5), _slab(305, 6.5)]
        assert fm.cgc_proxy_fmv(cheap, target_grade=6.5) is None

    def test_none_when_target_outside_ladder(self):
        # High-value ladder but target grade below the whole ladder → no extrapolation.
        assert fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=2.0) is None

    def test_none_when_no_graded_comps(self):
        assert fm.cgc_proxy_fmv([], target_grade=6.5) is None

    def test_bucket_median_is_outlier_robust(self):
        # Three comps at 6.5 ($1200, $1250, wild $5000) → median $1250, ignoring
        # the outlier (the mean would be $2483 → a wild over-price).
        comps = [_slab(1200, 6.5), _slab(1250, 6.5), _slab(5000, 6.5),
                 _slab(636, 4.0)]
        out = fm.cgc_proxy_fmv(comps, target_grade=6.5)
        assert out["cgc_ladder"]["slab_price"] == 1250.0

    def test_lone_offtrend_exact_slab_clamped_end_to_end(self):
        # BUI-349: a LONE (n=1) 6.5 slab priced off-trend high ($1900) but still
        # below the next bucket (7.0=$1971.5) passes the monotonicity guard, yet
        # the envelope clamp bounds it at the trustworthy 5.0–7.0 trend
        # ($1686.125) instead of setting a too-high cap off the lone outlier.
        offtrend = [_slab(800, 5.0), _slab(860, 5.0),      # 5.0 median 830, n=2
                    _slab(1900, 6.5),                        # 6.5 lone, off-trend
                    _slab(1800, 7.0), _slab(2143, 7.0)]      # 7.0 median 1971.5
        out = fm.cgc_proxy_fmv(offtrend, target_grade=6.5)
        assert out is not None
        assert out["cgc_ladder"]["slab_price"] == pytest.approx(1686.125)
        # Band + cap derive from the CLAMPED slab ($1686.125), not the $1900
        # outlier. Pin the exact money-facing output (as the ASM-#50 test does),
        # not a loose inequality — the unclamped band would be $950-$1050.
        assert out["fmv_low"] == 850
        assert out["fmv_high"] == 925
        assert out["median"] == 875
        assert out["max_bid"] == 650
        # BUI-369: the clamp fired, so the observability marker must be set —
        # otherwise the $1686.125 vs. ladder['ladder'][6.5]==1900.0 mismatch
        # would read as unexplained.
        assert out["cgc_ladder"]["envelope_clamped"] is True
        assert out["cgc_ladder"]["ladder"][6.5] == 1900.0

    def test_n2_offtrend_exact_bucket_clamped_end_to_end(self):
        # BUI-355: an n=2 exact 6.5 bucket holding one genuine comp ($1200) and
        # one $5000 mistag medians at $3100 (mean-of-2, zero robustness). The
        # ladder is monotone (830 < 3100 < 3250) so the monotonicity guard
        # passes it, and under BUI-349's n=1-only trigger the $3100 anchor
        # sailed through. The widened clamp bounds it at the trustworthy
        # 5.0–7.0 envelope ($2645). Pin the exact money-facing output — the
        # unclamped band would be $1550–$1700 with a $1200 max bid.
        mistagged = [_slab(800, 5.0), _slab(860, 5.0),       # 5.0 median 830, n=2
                     _slab(1200, 6.5), _slab(5000, 6.5),     # 6.5 median 3100, n=2
                     _slab(3200, 7.0), _slab(3300, 7.0)]     # 7.0 median 3250, n=2
        out = fm.cgc_proxy_fmv(mistagged, target_grade=6.5)
        assert out is not None
        assert out["cgc_ladder"]["slab_price"] == pytest.approx(2645.0)
        assert out["fmv_low"] == 1325
        assert out["fmv_high"] == 1450
        assert out["median"] == 1400
        assert out["max_bid"] == 1025
        # BUI-369: same marker requirement for the n=2 (BUI-355) clamp path.
        assert out["cgc_ladder"]["envelope_clamped"] is True
        assert out["cgc_ladder"]["ladder"][6.5] == 3100.0

    def test_lone_plausible_exact_slab_still_priced_asm50(self):
        # The sparse-key case the tier exists for is preserved end-to-end: ASM
        # #50's lone 6.5 ($1200), which sits BELOW its bracketing envelope, is
        # used as-is and still yields a bid-able band (not pushed to
        # needs_manual by the clamp).
        out = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.5)
        assert out is not None
        assert out["cgc_ladder"]["slab_price"] == 1200.0
        assert out["fmv_low"] == 600 and out["fmv_high"] == 650
        # BUI-369: the lone 6.5 is below its envelope, so no clamp fired —
        # the flag must be False (never True just because the bucket is thin).
        assert out["cgc_ladder"]["envelope_clamped"] is False


class TestCgcProxyShapeParity:
    def test_compute_fmv_marks_non_proxy(self):
        # A normal raw result must carry cgc_proxy=False so downstream readers
        # (table, notes, cache) can iterate the dict uniformly.
        comps = [_comp(p, 9.2) for p in [100, 110, 120, 130, 140]]
        out = fm.compute_fmv(comps, target_grade=9.2)
        assert out["cgc_proxy"] is False
        assert out["cgc_ladder"] is None

    def test_proxy_dict_has_all_compute_fmv_keys(self):
        raw = fm.compute_fmv([_comp(100, 9.2)], target_grade=9.2)
        proxy = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.5)
        # Every key compute_fmv emits must exist on the proxy dict (drop-in shape).
        for key in raw:
            assert key in proxy, f"proxy dict missing key {key!r}"

    def test_compute_fmv_carries_cgc_cross_check_none(self):
        # BUI-529: compute_fmv never runs the cross-check itself (it has no
        # graded ladder to compare against) — always None, key present for
        # shape parity so downstream readers can iterate uniformly.
        out = fm.compute_fmv([_comp(100, 9.2)], target_grade=9.2)
        assert out["cgc_cross_check"] is None

    def test_proxy_dict_carries_cgc_cross_check_none(self):
        # A proxy band IS the slab-derived price already — comparing it
        # against itself is meaningless, so this always stays None too.
        proxy = fm.cgc_proxy_fmv(_ASM50_LADDER, target_grade=6.5)
        assert proxy["cgc_cross_check"] is None


# ─── Always-on vintage cross-check (BUI-529) ──────────────────────────────────

class TestCgcCrossCheck:
    def test_diverges_when_raw_median_far_below_slab_implied(self):
        # Ghost Rider #3 / Moon Knight #12 / Thor #149 shape: a thin raw pool
        # priced far below what the slab ladder implies.
        out = fm.cgc_cross_check(_ASM50_LADDER, target_grade=6.5, raw_median=100.0)
        assert out is not None
        assert out["implied_raw"] == 625  # same 0.525 midpoint as cgc_proxy_fmv's median
        assert out["diverges"] is True

    def test_no_divergence_flag_when_raw_median_close(self):
        out = fm.cgc_cross_check(_ASM50_LADDER, target_grade=6.5, raw_median=600.0)
        assert out is not None
        assert out["diverges"] is False

    def test_divergence_boundary_is_strictly_greater_than(self):
        # implied_raw=625; a raw_median making divergence_pct exactly the
        # threshold must NOT flag (only strictly beyond the threshold does).
        raw_median = 625 / (1 + fm.CGC_CROSS_CHECK_DIVERGENCE_PCT)
        out = fm.cgc_cross_check(_ASM50_LADDER, target_grade=6.5,
                                 raw_median=raw_median)
        assert out["divergence_pct"] == pytest.approx(
            fm.CGC_CROSS_CHECK_DIVERGENCE_PCT, abs=1e-6)
        assert out["diverges"] is False

    def test_none_when_raw_median_missing_or_zero(self):
        assert fm.cgc_cross_check(_ASM50_LADDER, target_grade=6.5,
                                  raw_median=None) is None
        assert fm.cgc_cross_check(_ASM50_LADDER, target_grade=6.5,
                                  raw_median=0.0) is None

    def test_none_when_ladder_too_thin(self):
        assert fm.cgc_cross_check([_slab(1200, 6.5)], target_grade=6.5,
                                  raw_median=100.0) is None

    def test_none_when_target_outside_ladder(self):
        # No extrapolation past the observed range — same guard as cgc_proxy_fmv.
        assert fm.cgc_cross_check(_ASM50_LADDER, target_grade=2.0,
                                  raw_median=100.0) is None

    def test_none_when_non_monotonic_ladder(self):
        inverted = [_slab(636, 4.0), _slab(1500, 6.0), _slab(1550, 6.0),
                    _slab(1200, 6.5), _slab(1250, 6.5)]
        assert fm.cgc_cross_check(inverted, target_grade=6.5,
                                  raw_median=100.0) is None

    def test_no_value_floor_unlike_proxy_pricing(self):
        # BUI-529's explicit "drop the $400 slab floor in cross-check mode":
        # this exact ladder makes cgc_proxy_fmv return None (below the $400
        # PRICING floor), but the cross-check must still compare — it's a
        # read-only comparison, not a price, so the floor doesn't apply.
        cheap = [_slab(300, 6.5), _slab(310, 6.5), _slab(305, 6.5)]
        assert fm.cgc_proxy_fmv(cheap, target_grade=6.5) is None  # floor blocks pricing
        out = fm.cgc_cross_check(cheap, target_grade=6.5, raw_median=150.0)
        assert out is not None  # cross-check is NOT floor-gated
        assert out["slab_price"] == 305.0

    def test_result_shape(self):
        out = fm.cgc_cross_check(_ASM50_LADDER, target_grade=6.5, raw_median=100.0)
        for key in ("slab_price", "target_grade", "implied_raw", "raw_median",
                    "divergence_pct", "diverges", "n", "ladder",
                    "envelope_clamped"):
            assert key in out, f"cgc_cross_check result missing key {key!r}"
        assert out["n"] == len(_ASM50_LADDER)


# ─── Ungraded-market anchor (BUI-522) ─────────────────────────────────────────

class TestUngradedMarketAnchor:
    def test_median_and_count_of_gradeless_comps(self):
        comps = [_comp(40, None), _comp(60, None), _comp(50, None)]
        assert fm.ungraded_market_anchor(comps) == {"median": 50.0, "n": 3}

    def test_none_when_every_comp_is_graded(self):
        comps = [_comp(100, 9.0), _comp(110, 9.2)]
        assert fm.ungraded_market_anchor(comps) is None

    def test_ignores_graded_comps_and_missing_prices(self):
        # Only grade-less, priced comps count: a graded comp already priced the
        # graded pool, and a comp with no price can't anchor anything.
        comps = [_comp(100, 9.0), _comp(40, None), _comp(60, None),
                 {"grade": None, "price": None}, {"grade": None}]
        assert fm.ungraded_market_anchor(comps) == {"median": 50.0, "n": 2}

    def test_non_numeric_price_is_skipped(self):
        # SerpApi text can leak a non-numeric price; it must not crash the anchor.
        comps = [_comp(40, None), {"grade": None, "price": "n/a"}]
        assert fm.ungraded_market_anchor(comps) == {"median": 40.0, "n": 1}

    def test_surfaced_in_compute_fmv_output(self):
        comps = [_comp(100, 9.0), _comp(105, 9.0), _comp(110, 9.0),
                 _comp(40, None), _comp(50, None), _comp(60, None)]
        out = fm.compute_fmv(comps, target_grade=9.0)
        assert out["ungraded_anchor"] == {"median": 50.0, "n": 3}
        # The anchor is informational: it did NOT enter the priced graded pool.
        assert out["n"] == 3
        assert out["trimmed_pool"] == [100, 105, 110]

    def test_absent_anchor_is_none_in_output(self):
        comps = [_comp(p, 9.0) for p in [100, 105, 110, 115, 120]]
        out = fm.compute_fmv(comps, target_grade=9.0)
        assert out["ungraded_anchor"] is None


# ─── Pool-vs-anchor divergence flag (BUI-534) ─────────────────────────────────

class TestAnchorDivergesFunction:
    """Direct unit tests of the pure `anchor_diverges` helper."""

    def test_named_constants_exist(self):
        assert fm.ANCHOR_DIVERGES_PCT == 0.5
        assert fm.ANCHOR_DIVERGES_MIN_N == 8

    def test_batman_251_numbers_diverge(self):
        # The actual 2026-07-24 incident: pool $400-425, anchor $224.8 n=36.
        anchor = {"median": 224.8, "n": 36}
        assert fm.anchor_diverges(400, 425, anchor) is True

    def test_healthy_priced_range_does_not_diverge(self):
        # A normal grade premium over the anchor, comfortably inside T=0.5.
        anchor = {"median": 200.0, "n": 20}
        assert fm.anchor_diverges(220, 260, anchor) is False

    def test_low_side_exactly_at_boundary_does_not_diverge(self):
        anchor = {"median": 200.0, "n": 20}
        assert fm.anchor_diverges(300, 320, anchor) is False  # 200*1.5 == 300

    def test_low_side_just_past_boundary_diverges(self):
        anchor = {"median": 200.0, "n": 20}
        assert fm.anchor_diverges(300.01, 320, anchor) is True

    def test_high_side_diverges_symmetrically(self):
        # fmv_high sits far BELOW the anchor's lower band.
        anchor = {"median": 1000.0, "n": 20}
        assert fm.anchor_diverges(350, 400, anchor) is True  # 400 < 1000*0.5

    def test_thin_anchor_below_min_n_is_skipped(self):
        # Same wild ratio as the Batman #251 case, but n=7 < MIN_N=8 — must
        # not fire; a thin anchor is noise, not signal.
        anchor = {"median": 224.8, "n": 7}
        assert fm.anchor_diverges(400, 425, anchor) is False

    def test_anchor_at_exactly_min_n_is_checked(self):
        anchor = {"median": 224.8, "n": 8}
        assert fm.anchor_diverges(400, 425, anchor) is True

    def test_no_anchor_never_diverges(self):
        assert fm.anchor_diverges(400, 425, None) is False

    def test_no_priced_range_never_diverges(self):
        # A flagged/no-comps/interpolated-absent book has no fmv_low/high.
        anchor = {"median": 224.8, "n": 36}
        assert fm.anchor_diverges(None, None, anchor) is False
        assert fm.anchor_diverges(400, None, anchor) is False
        assert fm.anchor_diverges(None, 425, anchor) is False

    def test_degenerate_zero_median_never_diverges(self):
        assert fm.anchor_diverges(400, 425, {"median": 0, "n": 36}) is False


class TestAnchorDivergesInComputeFmv:
    def test_batman_251_shaped_pool_fires_in_compute_fmv(self):
        # Graded pool (grades 5.0/6.0 bracket the 5.5 target, within the
        # default ±0.5 window) pricing well above a raw/ungraded market.
        graded = [_comp(390, 5.0), _comp(400, 5.0), _comp(405, 5.0),
                  _comp(415, 6.0), _comp(425, 6.0), _comp(430, 6.0)]
        grade_less = [_comp(220 + (i % 3) * 5) for i in range(36)]  # ~$220-230, n=36
        out = fm.compute_fmv(graded + grade_less, target_grade=5.5)
        assert out["ungraded_anchor"]["n"] == 36
        assert out["fmv_low"] is not None and out["fmv_high"] is not None
        assert out["anchor_diverges"] is True

    def test_pricing_is_byte_identical_with_and_without_the_flag(self):
        """BUI-534 acceptance: adding grade-less comps that push the anchor
        far enough to trip the flag must NOT change fmv_low/fmv_high/max_bid
        at all — same pure-flag philosophy as BUI-529/530."""
        graded = [_comp(390, 5.0), _comp(400, 5.0), _comp(405, 5.0),
                  _comp(415, 6.0), _comp(425, 6.0), _comp(430, 6.0)]
        grade_less = [_comp(220 + (i % 3) * 5) for i in range(36)]
        with_anchor = fm.compute_fmv(graded + grade_less, target_grade=5.5)
        without_anchor = fm.compute_fmv(graded, target_grade=5.5)
        assert with_anchor["anchor_diverges"] is True
        assert without_anchor["anchor_diverges"] is False  # no anchor at all
        assert with_anchor["fmv_low"] == without_anchor["fmv_low"]
        assert with_anchor["fmv_high"] == without_anchor["fmv_high"]
        assert with_anchor["max_bid"] == without_anchor["max_bid"]

    def test_unchanged_healthy_book_does_not_flag(self):
        # A normal priced book whose range sits close to its own raw anchor —
        # must NOT flag, mirroring "the 46 unchanged books from the same run".
        graded = [_comp(p, 9.0) for p in [100, 105, 110, 115, 120]]
        grade_less = [_comp(90 + i) for i in range(10)]  # anchor ~ $94.5, n=10
        out = fm.compute_fmv(graded + grade_less, target_grade=9.0)
        assert out["ungraded_anchor"]["n"] == 10
        assert out["anchor_diverges"] is False

    def test_flagged_needs_manual_book_never_flags(self):
        # A one-sided pool has no fmv_low/high at all — anchor_diverges must
        # be False (nothing priced to compare), even with a wild anchor.
        graded = [_comp(p, 9.0) for p in [100, 105, 110]]  # all one-sided
        grade_less = [_comp(1000 + i) for i in range(10)]
        out = fm.compute_fmv(graded, target_grade=8.0)
        assert out["flag_reason"] is not None
        assert out["fmv_low"] is None
        assert out["anchor_diverges"] is False

    def test_cgc_proxy_dict_never_flags(self):
        # A proxy band has no ungraded_anchor at all (priced off the slab
        # ladder, not raw comps) — anchor_diverges must default False, not
        # raise, and shape-parity with compute_fmv's key must hold.
        ladder_comps = ([_comp(1200, 6.5)] * 3 + [_comp(200, 5.0)] * 3
                        + [_comp(2000, 7.0)] * 3)
        proxy = fm.cgc_proxy_fmv(ladder_comps, target_grade=6.5)
        assert proxy is not None
        assert proxy["anchor_diverges"] is False


# ─── Minimum range width on collapsed pools (BUI-528) ─────────────────────────

class TestMinRangeWidth:
    def test_collapsed_nondegenerate_pool_is_reopened(self):
        # [49,50,51] clean-rounds to a $50/$50 point, but the prices differ
        # (cv>0), so it must NOT emit a zero-width range.
        out = fm.compute_fmv([_comp(p, 9.0) for p in [49, 50, 51]],
                             target_grade=9.0)
        assert out["fmv_low"] < out["fmv_high"]
        assert out["fmv_low"] is not None

    def test_identical_prices_stay_a_true_point(self):
        # Genuinely degenerate (cv==0): the carve-out — no fabricated range.
        out = fm.compute_fmv([_comp(50, 9.0), _comp(50, 9.0), _comp(50, 9.0)],
                             target_grade=9.0)
        assert out["fmv_low"] == out["fmv_high"] == 50

    def test_healthy_ranged_pool_is_untouched(self):
        # A pool with a real (non-collapsed) range must be byte-identical to the
        # pre-BUI-528 behavior — the guard only ever fires on a zero-width band.
        out = fm.compute_fmv(
            [_comp(p, 9.0) for p in [100, 105, 110, 115, 120, 125, 130]],
            target_grade=9.0)
        assert out["fmv_low"] < out["fmv_high"]          # a real, non-zero range
        assert (out["fmv_low"], out["fmv_high"], out["max_bid"]) == (110, 120, 100)

    def test_reopen_never_lifts_bid_cap_when_dispersion_is_below_median(self):
        # thick_mild_skew shape: median sits near the TOP of the observed range,
        # so the reopen lowers fmv_low and leaves fmv_high (→ max_bid) untouched.
        low, high = fm._widen_collapsed_range(
            median=223.0, cv_value=0.05, window=0.5, n=8,
            price_min=200.0, price_max=230.0)
        assert (low, high) == (200, 225)

    def test_reopen_reveals_upside_when_dispersion_is_above_median(self):
        # Ghost-Rider shape: a cheap median with pricier copies above it — the
        # reopen lifts fmv_high toward the observed top, never past it.
        low, high = fm._widen_collapsed_range(
            median=5.0, cv_value=1.0, window=0.5, n=10,
            price_min=2.0, price_max=12.0)
        assert low < high
        assert high <= fm.clean_round(12.0)   # never claims a value beyond a real sale

    def test_reopen_stays_within_observed_prices(self):
        # The widened band is bounded by [price_min, price_max] — fmv_high can
        # never exceed the priciest real comp (money-safety: no invented upside).
        low, high = fm._widen_collapsed_range(
            median=100.0, cv_value=0.9, window=2.0, n=2,
            price_min=90.0, price_max=110.0)
        assert low >= 0
        assert high <= fm.clean_round(110.0)

    def test_reopened_max_bid_does_not_exceed_top_comp(self):
        # End-to-end money-safety: even after a reopen, max_bid stays at or below
        # the priciest observed comp.
        prices = [49, 50, 51]
        out = fm.compute_fmv([_comp(p, 9.0) for p in prices], target_grade=9.0)
        assert out["max_bid"] <= max(prices)


class TestForcedFlagReason:
    """BUI-588: a needs-manual reason the POOL cannot show — the comps are
    fine-shaped but were fetched for a different thing than the book asked
    about."""

    def _healthy(self):
        # Bracketed, tight, plenty deep: nothing here flags on its own.
        return [_comp(p, g) for p, g in
                [(100, 8.5), (105, 9.0), (110, 9.0), (108, 9.5), (112, 9.5)]]

    def test_none_is_a_no_op(self):
        comps = self._healthy()
        assert (fm.compute_fmv(comps, target_grade=9.0, forced_flag_reason=None)
                == fm.compute_fmv(comps, target_grade=9.0))

    def test_forced_reason_withholds_the_bid_on_an_otherwise_clean_pool(self):
        out = fm.compute_fmv(self._healthy(), target_grade=9.0,
                             forced_flag_reason="variant_dropped")
        assert out["flag_reason"] == "variant_dropped"
        # The whole point: a flagged book emits NO bid-able number, so a
        # variant-blind pool can't quietly set a bid cap.
        assert out["fmv_low"] is None
        assert out["fmv_high"] is None
        assert out["median"] is None
        assert out["max_bid"] is None
        assert out["confidence"] == "LOW"

    def test_a_real_pool_reason_keeps_precedence(self):
        """one_sided/too_wide/too_sparse describe the comps themselves and are
        more actionable, so they must not be masked."""
        one_sided = [_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        out = fm.compute_fmv(one_sided, target_grade=9.6,
                             forced_flag_reason="variant_dropped")
        assert out["flag_reason"] == "one_sided"
        assert out["max_bid"] is None

    def test_forced_reason_suppresses_interpolation(self):
        """§7 is the ONE path that clears a flag and emits a bid-able number.
        Interpolating between buckets of a base-cover pool still yields a
        base-cover price, so a forced reason must close that exit too —
        including when a pool-shape reason won the `flag_reason` slot."""
        # The TestInterpolationInComputeFmv too_wide-but-bracketed shape.
        comps = [_comp(90, 4.0), _comp(110, 4.0), _comp(190, 8.0), _comp(210, 8.0)]
        interpolated = fm.compute_fmv(comps, target_grade=6.0)
        assert interpolated["interpolated"] is True
        assert interpolated["flag_reason"] is None
        assert interpolated["max_bid"] == 90

        forced = fm.compute_fmv(comps, target_grade=6.0,
                                forced_flag_reason="variant_dropped")
        assert forced["interpolated"] is False
        assert forced["flag_reason"] == "too_wide"
        assert forced["fmv_low"] is None
        assert forced["max_bid"] is None

    def test_empty_pool_gains_a_reason_instead_of_a_silent_stub(self):
        """The BUI-588 defect state: n=0 with a null flag_reason reads as
        "illiquid" and is invisible to /comic:buy Step 3's guards."""
        plain = fm.compute_fmv([], target_grade=9.0)
        assert plain["flag_reason"] is None
        forced = fm.compute_fmv([], target_grade=9.0,
                                forced_flag_reason="variant_dropped")
        assert forced["flag_reason"] == "variant_dropped"


# ─── Graded (slab) pricing mode — BUI-930 ────────────────────────────────────

_GREF = date(2026, 9, 1)


def _slab_comp(price, grade, *, age=0, page_quality="unknown",
               product_id=None, first_seen_age=None):
    """One slab comp. `age=None` leaves it UNDATED unless `first_seen_age`
    supplies the ledger's own `first_seen_at` fallback."""
    comp = {"product_id": product_id or f"p{price}-{grade}",
            "title": f"CGC {grade}", "price": price, "grade": grade,
            "page_quality": page_quality, "certifier": "cgc",
            "label": "universal"}
    if age is not None:
        comp["sold_date"] = (_GREF - timedelta(days=age)).isoformat()
    if first_seen_age is not None:
        comp["first_seen_at"] = (_GREF - timedelta(days=first_seen_age)).isoformat()
    return comp


def _graded(comps, grade, **kw):
    kw.setdefault("certifier", "cgc")
    kw.setdefault("label", "universal")
    # BUI-948: `as_of` is required, and every fixture in this file builds its
    # comps as an age relative to `_GREF`, so pinning it to `_GREF` is what
    # keeps `age=N` meaning "N days old" — and keeps every pre-BUI-948
    # expectation in this file exactly as it was.
    kw.setdefault("as_of", _GREF)
    return fm.graded_fmv(comps, grade, **kw)


def _pool(comps, as_of=_GREF):
    return fm.graded_pool(comps, as_of=as_of)


class TestGradedCompWeights:
    def test_fresh_stale_and_excluded_bands(self):
        assert fm.graded_comp_weight(0) == 1.0
        assert fm.graded_comp_weight(90) == 1.0
        assert fm.graded_comp_weight(91) == 0.5
        assert fm.graded_comp_weight(365) == 0.5
        assert fm.graded_comp_weight(366) is None
        assert fm.graded_comp_weight(None) is None

    def test_pool_reference_is_the_supplied_as_of_not_the_newest_comp(self):
        """BUI-948. This pool's two comps are 30 days apart and BOTH more than
        a year old. The pre-BUI-948 rule aged them against each other, so the
        newer one weighed 1.0 and a decade-old sale could anchor a four-figure
        cap; against the calendar both are simply gone."""
        old = [_slab_comp(100, 9.0, age=4000), _slab_comp(110, 9.0, age=4030)]
        kept, undated, stale = _pool(old)
        assert kept == []
        assert (undated, stale) == (0, 2)

    def test_as_of_is_required_so_no_caller_can_inherit_a_clock(self):
        """The parameter has no default on purpose: a default would let a new
        call site silently pick a reference nobody chose."""
        with pytest.raises(TypeError):
            fm.graded_pool([_slab_comp(100, 9.0, age=0)])
        with pytest.raises(TypeError):
            fm.graded_fmv([_slab_comp(100, 9.0, age=0)], 9.0,
                          certifier="cgc", label="universal")

    def test_moving_as_of_forward_demotes_a_comp_it_does_not_move_the_pool(self):
        """The same fixed pool, read on two different days. A 60-day sale is
        full weight today and half weight 60 days later — the behaviour the
        newest-comp rule could not express, because the pool's own newest date
        never moves."""
        comps = [_slab_comp(100, 9.0, age=60)]
        assert [c["weight"] for c in _pool(comps)[0]] == [1.0]
        later, _, _ = _pool(comps, as_of=_GREF + timedelta(days=60))
        assert [c["weight"] for c in later] == [0.5]

    def test_a_comp_past_365_days_from_as_of_is_excluded_not_demoted(self):
        """The hard cut BUI-948 measured and kept. The pool's own newest comp
        is this same 300-day sale, which under the old rule made it the
        reference and weighted it 1.0."""
        comps = [_slab_comp(100, 9.0, age=300)]
        assert [c["weight"] for c in _pool(comps)[0]] == [0.5]
        gone, undated, stale = _pool(comps, as_of=_GREF + timedelta(days=66))
        assert gone == []
        assert (undated, stale) == (0, 1)

    def test_a_future_sold_date_clamps_to_full_weight_never_negative(self):
        comps = [_slab_comp(100, 9.0, age=-30)]
        assert [c["weight"] for c in _pool(comps)[0]] == [1.0]

    def test_undated_comp_is_excluded_not_weighted_one(self):
        comps = [_slab_comp(100, 9.0, age=0), _slab_comp(999, 9.0, age=None)]
        kept, undated, stale = _pool(comps)
        assert [c["price"] for c in kept] == [100]
        assert (undated, stale) == (1, 0)

    def test_an_all_undated_pool_reports_every_comp_undated(self):
        """The early return BUI-948 deleted used to produce this count; the
        ordinary loop must still produce it."""
        comps = [_slab_comp(100, 9.0, age=None),
                 _slab_comp(200, 9.4, age=None)]
        kept, undated, stale = _pool(comps)
        assert kept == []
        assert (undated, stale) == (2, 0)

    def test_first_seen_at_is_the_fallback_age_basis(self):
        comps = [_slab_comp(100, 9.0, age=0),
                 _slab_comp(200, 9.0, age=None, first_seen_age=120)]
        kept, undated, stale = _pool(comps)
        assert (undated, stale) == (0, 0)
        assert [c["weight"] for c in kept] == [1.0, 0.5]

    def test_first_seen_at_ages_against_as_of_like_any_other_date(self):
        """A ledger row with no `sold_date` is aged on `first_seen_at`, and
        BUI-948 moved that basis onto the calendar too — otherwise the one
        class of comp most likely to be old would be the one still measured
        against the pool."""
        comps = [_slab_comp(100, 9.0, age=0),
                 _slab_comp(200, 9.0, age=None, first_seen_age=80)]
        assert [c["weight"] for c in _pool(comps)[0]] == [1.0, 1.0]
        later, _, _ = _pool(comps, as_of=_GREF + timedelta(days=20))
        assert [c["weight"] for c in later] == [1.0, 0.5]

    def test_two_hundred_day_comp_joins_at_half_weight(self):
        comps = [_slab_comp(100, 9.0, age=0), _slab_comp(200, 9.0, age=200)]
        kept, _, _ = _pool(comps)
        assert sum(c["weight"] for c in kept) == 1.5

    def test_pool_copies_rather_than_mutating_the_callers_comps(self):
        """The same list is POSTed to the comps ledger, where `weight` is not
        a `CompItem` field."""
        comps = [_slab_comp(100, 9.0, age=0)]
        _pool(comps)
        assert "weight" not in comps[0]

    def test_bool_price_never_enters_the_pool(self):
        comps = [{"product_id": "x", "price": True, "grade": 9.0,
                  "sold_date": "2026-09-01"}]
        kept, _, _ = _pool(comps)
        assert kept == []


class TestGradedBuckets:
    def test_weighted_medians_reduce_to_bucket_medians_when_unweighted(self):
        comps = [{"price": 10, "grade": 9.0}, {"price": 30, "grade": 9.0},
                 {"price": 50, "grade": 9.4}]
        assert fm.bucket_weighted_medians(comps) == fm.bucket_medians(comps)

    def test_weighted_median_moves_toward_the_heavier_comp(self):
        comps = [{"price": 100, "grade": 9.0, "weight": 1.0},
                 {"price": 200, "grade": 9.0, "weight": 0.5}]
        assert fm.bucket_weighted_medians(comps)[9.0] < 150

    def test_effective_n_is_the_weight_sum(self):
        comps = [{"price": 100, "grade": 9.0, "weight": 1.0},
                 {"price": 200, "grade": 9.0, "weight": 0.5},
                 {"price": 300, "grade": 9.4, "weight": 1.0}]
        assert fm.bucket_effective_n(comps) == {9.0: 1.5, 9.4: 1.0}


class TestGradedExactTier:
    def test_the_tier_gate_reads_the_as_of_weights_not_the_pool_relative_ones(self):
        """BUI-948's money consequence, at the gate that decides the haircut.

        One fixed pool, two as-of dates. Its two 9.4 sales are 50 days apart;
        read on the day the newer one sold they are both fresh, effective n is
        2.0 and the book prices DIRECT. Read 100 days later they are 100 and
        150 days old, effective n is 1.0, and the same two sales send the book
        to the ladder at 0.60. The pre-BUI-948 reference could only ever
        produce the first answer, whatever the calendar said — which is the
        mechanism behind the corpus-wide drop from 71 effective-n-2 rungs to
        44.
        """
        comps = [_slab_comp(1000, 9.4, age=100), _slab_comp(1100, 9.4, age=150),
                 _slab_comp(800, 9.2, age=10), _slab_comp(1400, 9.6, age=10),
                 _slab_comp(1600, 9.8, age=10)]
        fresh = _graded(comps, 9.4, as_of=_GREF - timedelta(days=100))
        assert fresh["exact_effective_n"] == 2.0
        assert fresh["pricing_basis"] == "direct"

        aged = _graded(comps, 9.4)
        assert aged["exact_effective_n"] == 1.0
        assert aged["pricing_basis"] == "ladder"
        assert aged["bid_factor"] == 0.60

    def test_two_live_sales_price_directly(self):
        comps = [_slab_comp(1000, 9.4, age=1), _slab_comp(1100, 9.4, age=2),
                 _slab_comp(800, 9.2, age=3), _slab_comp(1400, 9.6, age=4)]
        out = _graded(comps, 9.4)
        assert out["pricing_basis"] == "direct"
        assert out["flag_reason"] is None
        assert out["fmv_high"] is not None

    def test_a_96_sale_never_enters_a_94_targets_exact_bucket(self):
        """R30 — strictly exact. The 9.6 sales are three times the price and
        must not move the 9.4 band by a cent; they may only anchor the
        ladder."""
        comps = [_slab_comp(1000, 9.4, age=1), _slab_comp(1100, 9.4, age=2),
                 _slab_comp(800, 9.2, age=3)]
        narrow = _graded(comps, 9.4)
        wide = _graded(comps + [_slab_comp(3000, 9.6, age=4),
                                _slab_comp(3200, 9.6, age=5)], 9.4)
        assert narrow["fmv_low"] == wide["fmv_low"]
        assert narrow["fmv_high"] == wide["fmv_high"]
        assert narrow["exact_effective_n"] == wide["exact_effective_n"] == 2.0

    def test_grade_confidence_is_ignored_for_a_certified_row(self):
        """A certified grade is not a photo judgement (R20), so there is no
        photo-coverage haircut to take — `graded_fmv` has no parameter for one
        and pins `grade_confidence` to None on the way out."""
        comps = [_slab_comp(1000, 9.4, age=1), _slab_comp(1010, 9.4, age=2),
                 _slab_comp(1020, 9.4, age=3),
                 _slab_comp(800, 9.2, age=3), _slab_comp(1400, 9.6, age=4)]
        out = _graded(comps, 9.4)
        assert out["grade_confidence"] is None
        assert out["bid_factor"] == fm.BASE_BID_FACTOR

    def test_the_clamp_only_lowers_and_is_reported(self):
        comps = [_slab_comp(5000, 9.4, age=1), _slab_comp(5000, 9.4, age=2),
                 _slab_comp(1000, 9.2, age=3), _slab_comp(1400, 9.6, age=4)]
        out = _graded(comps, 9.4)
        assert out["envelope_clamped"] is True
        assert out["fmv_high"] < 5000
        assert out["fmv_low"] <= out["fmv_high"]

    def test_a_robust_bucket_is_not_clamped(self):
        comps = [_slab_comp(5000, 9.4, age=1), _slab_comp(5000, 9.4, age=2),
                 _slab_comp(5000, 9.4, age=3),
                 _slab_comp(1000, 9.2, age=3), _slab_comp(1400, 9.6, age=4)]
        out = _graded(comps, 9.4)
        assert out["envelope_clamped"] is False
        assert out["fmv_high"] == 5000


class TestGradedLadderTier:
    def _rungs(self):
        return [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                _slab_comp(1500, 6.0, age=14), _slab_comp(500, 2.5, age=16)]

    def test_a_lone_exact_sale_is_recorded_but_never_priced(self):
        lone = _slab_comp(700, 4.5, age=1)
        out = _graded(self._rungs() + [lone], 4.5)
        assert out["pricing_basis"] == "ladder"
        assert out["exact_sales"] == [700.0]
        assert out["exact_effective_n"] == 1.0
        assert out["fmv_high"] != 700
        assert 900 < out["fmv_high"] < 1400

    def test_removing_the_target_rung_is_what_makes_it_an_interpolation(self):
        """Regression guard for the one line that carries the whole rule: with
        the rung LEFT IN, `_cgc_ladder_price_and_clamp` returns the lone sale
        (merely bounded from above), which is the outcome this tier exists to
        prevent."""
        comps = self._rungs() + [_slab_comp(700, 4.5, age=1)]
        ladder = fm.bucket_weighted_medians(_pool(comps)[0])
        counts = fm.bucket_effective_n(_pool(comps)[0])
        with_rung, _ = fm._cgc_ladder_price_and_clamp(
            ladder, 4.5, counts=counts, min_bucket_n=1)
        assert with_rung == 700.0          # the trap
        assert _graded(comps, 4.5)["fmv_high"] != 700   # the guard

    def test_ladder_is_low_confidence_and_the_sixty_percent_cap(self):
        out = _graded(self._rungs() + [_slab_comp(700, 4.5, age=1)], 4.5)
        assert out["confidence"] == "LOW"
        assert out["bid_factor"] == 0.60
        assert out["max_bid"] == fm.clean_round(out["fmv_high"] * 0.60)

    def test_no_exact_sale_at_all_still_interpolates(self):
        out = _graded(self._rungs(), 4.5)
        assert out["pricing_basis"] == "ladder"
        assert out["exact_sales"] == []

    def test_refuses_below_three_remaining_rungs(self):
        comps = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(700, 4.5, age=1)]
        assert _graded(comps, 4.5)["flag_reason"] == "ladder_too_thin"

    def test_refuses_outside_the_observed_ladder(self):
        comps = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(1500, 6.0, age=14)]
        assert _graded(comps, 9.8)["flag_reason"] == "outside_ladder"

    def test_refuses_when_the_neighbours_invert(self):
        comps = [_slab_comp(2000, 8.5, age=10), _slab_comp(3000, 9.2, age=12),
                 _slab_comp(2500, 9.6, age=14), _slab_comp(4000, 9.8, age=16)]
        assert _graded(comps, 9.4)["flag_reason"] == "ladder_non_monotone"

    def test_a_violation_away_from_the_target_does_not_refuse(self):
        """Scoped to the NEIGHBOURS on purpose. A slab ladder is one sale per
        rung, so a violation somewhere is the norm — a whole-ladder rule
        refused all four ladder books in the 2026-09-21 spike corpus, i.e. it
        would not be a guard, it would be an off switch."""
        comps = [_slab_comp(2000, 8.5, age=10), _slab_comp(2500, 9.2, age=12),
                 _slab_comp(3500, 9.6, age=14), _slab_comp(3000, 9.8, age=16)]
        out = _graded(comps, 9.4)
        assert out["flag_reason"] is None
        assert out["pricing_basis"] == "ladder"

    def test_a_stale_only_rung_is_skipped_as_an_anchor(self):
        """A rung whose only sale is 91-365 days old sums to 0.5 effective
        sales, below `GRADED_LADDER_MIN_BUCKET_N`, so it cannot be a bracket
        END — the bracket widens past it to the next eligible rung, exactly as
        `interpolate_grade_curve` does with a thin one on the raw path. The
        4.0 rung here is the nearest below the 4.5 target and is skipped in
        favour of 2.5."""
        fresh = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(1500, 6.0, age=14), _slab_comp(500, 2.5, age=16)]
        stale = [_slab_comp(900, 4.0, age=200)] + fresh[1:]
        assert _graded(fresh, 4.5)["graded_ladder"]["grade_below"] == 4.0
        out = _graded(stale, 4.5)
        assert out["graded_ladder"]["grade_below"] == 2.5
        assert out["pricing_basis"] == "ladder"

    def test_rungs_over_a_year_before_as_of_are_dropped_not_weighted(self):
        """A pool whose older rungs sit more than a year before the as-of date
        loses them outright, and a ladder that loses too many is refused
        rather than interpolated across the gap. Since BUI-948 "everything is
        stale" is also reachable — the whole pool can be past 365 days at once
        — and `TestGradedCompWeights` covers that case; here one fresh rung
        survives so the assertion is about the ladder, not the pool."""
        comps = [_slab_comp(2000, 7.0, age=0), _slab_comp(900, 4.0, age=400),
                 _slab_comp(1400, 5.5, age=410), _slab_comp(500, 2.5, age=420)]
        out = _graded(comps, 4.5)
        assert out["pool_stale_dropped"] == 3
        assert out["pool_n"] == 1
        assert out["flag_reason"] == "ladder_too_thin"


class TestGradedLoneSaleTier:
    """BUI-952: one fresh sale at the exact grade, inside a bracket its
    neighbouring rungs agree on, IS the price.

    The tier's whole surface is a set of fall-throughs, so most of these
    tests assert `pricing_basis == "ladder"` — that is the point. Read them
    against `TestGradedLadderTier` above, whose fixture is deliberately the
    near-miss of this one: the same four rungs with the lone 4.5 sale at $700,
    which sits BELOW its $900 bracket floor and therefore stays a ladder row.
    """

    def _rungs(self):
        # 2.5 -> $500, 4.0 -> $900, 5.5 -> $1,400, 6.0 -> $1,500. Four
        # anchor-eligible rungs, so `ladder_too_thin` never fires and every
        # test below is about the bracket rather than the rung count.
        return [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                _slab_comp(1500, 6.0, age=14), _slab_comp(500, 2.5, age=16)]

    def test_a_bracketed_lone_sale_is_the_price_at_seventy_percent(self):
        out = _graded(self._rungs() + [_slab_comp(1000, 4.5, age=1)], 4.5)
        assert out["flag_reason"] is None
        assert out["pricing_basis"] == "lone_sale"
        # The band is the sale, flat — no quartiles, because there is nothing
        # to take quartiles of.
        assert out["fmv_low"] == out["fmv_high"] == out["median"] == 1000
        assert out["confidence"] == "LOW"
        assert out["bid_factor"] == 0.70
        assert out["max_bid"] == fm.clean_round(1000 * 0.70)
        assert out["lone_sale_bracket"] == {
            "lo": 900.0, "hi": 1400.0, "lo_grade": 4.0, "hi_grade": 5.5}
        # `n`/`effective_n` describe the exact bucket, not the pool: this
        # price rests on one sale and must not report five.
        assert out["n"] == 1 and out["effective_n"] == 1.0
        assert out["trimmed_pool"] == [1000.0]

    def test_it_beats_the_ladder_cap_it_replaces(self):
        """The trade the ticket is making, in one assertion: the same book
        priced by the ladder (sale moved below its bracket) caps lower than
        the observed sale does at 0.70."""
        priced = _graded(self._rungs() + [_slab_comp(1000, 4.5, age=1)], 4.5)
        laddered = _graded(self._rungs() + [_slab_comp(700, 4.5, age=1)], 4.5)
        assert laddered["pricing_basis"] == "ladder"
        assert priced["max_bid"] > laddered["max_bid"]

    def test_a_sale_below_its_bracket_falls_to_the_ladder(self):
        out = _graded(self._rungs() + [_slab_comp(700, 4.5, age=1)], 4.5)
        assert out["pricing_basis"] == "ladder"
        assert out["lone_sale_bracket"] is None
        assert out["bid_factor"] == 0.60

    def test_a_sale_above_its_bracket_falls_to_the_ladder(self):
        """The other side, and the expensive one: a lone $2,000 sale above a
        $1,400 ceiling is exactly the outlier this tier must not print."""
        out = _graded(self._rungs() + [_slab_comp(2000, 4.5, age=1)], 4.5)
        assert out["pricing_basis"] == "ladder"
        assert out["fmv_high"] != 2000

    def test_a_bracket_wider_than_three_times_falls_to_the_ladder(self):
        """Inside the bracket is not enough. $400 -> $1,400 is 3.5x, and two
        rungs that far apart are not one market to be inside of."""
        rungs = [_slab_comp(400, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(1500, 6.0, age=14), _slab_comp(500, 2.5, age=16)]
        out = _graded(rungs + [_slab_comp(1000, 4.5, age=1)], 4.5)
        assert out["pricing_basis"] == "ladder"
        # ...and the same pool one dollar inside the ratio does price, so the
        # assertion above is about the ratio and not about the pool.
        ok = [_slab_comp(467, 4.0, age=10)] + rungs[1:]
        assert _graded(ok + [_slab_comp(1000, 4.5, age=1)], 4.5)[
            "pricing_basis"] == "lone_sale"

    def test_a_half_weight_lone_sale_does_not_qualify(self):
        """A 91-365-day sale weighs 0.5. `bucket_effective_n` already refuses
        it the right to ANCHOR a bracket, so letting it BE one would
        contradict the module's own rule about the same observation."""
        out = _graded(self._rungs() + [_slab_comp(1000, 4.5, age=200)], 4.5)
        assert out["exact_effective_n"] == 0.5
        assert out["pricing_basis"] == "ladder"

    def test_two_sales_at_the_target_grade_are_not_a_lone_sale(self):
        """Effective n 1.0 and 1.5 both sit below the exact tier's floor, and
        both are still MORE than one observation. The basis is called
        `lone_sale` because the number is one observed price; a weighted
        median of two under that name would be a different claim."""
        two_stale = self._rungs() + [_slab_comp(1000, 4.5, age=200,
                                                product_id="a"),
                                     _slab_comp(1020, 4.5, age=210,
                                                product_id="b")]
        out = _graded(two_stale, 4.5)
        assert out["exact_effective_n"] == 1.0
        assert out["pricing_basis"] == "ladder"

        fresh_plus_stale = self._rungs() + [
            _slab_comp(1000, 4.5, age=1, product_id="a"),
            _slab_comp(1020, 4.5, age=200, product_id="b")]
        out = _graded(fresh_plus_stale, 4.5)
        assert out["exact_effective_n"] == 1.5
        assert out["pricing_basis"] == "ladder"

    def test_it_never_rescues_a_refusal(self):
        """The safety property the tier is built to have. Each refusal below
        is reached by removing exactly one of the tier's preconditions from an
        otherwise-qualifying pool, so the tier declines every row the ladder
        refuses and can only ever take rows the ladder would have priced."""
        sale = _slab_comp(1000, 4.5, age=1)

        # ladder_too_thin — two eligible rungs, bracketing, within 3x. This is
        # BUI-953's case and stays BUI-953's to decide.
        thin = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12)]
        assert _graded(thin + [sale], 4.5)["flag_reason"] == "ladder_too_thin"

        # outside_ladder — no eligible rung above the target.
        one_sided = [_slab_comp(900, 4.0, age=10), _slab_comp(500, 2.5, age=12),
                     _slab_comp(300, 1.5, age=14)]
        assert _graded(one_sided + [_slab_comp(1000, 4.5, age=1)],
                       4.5)["flag_reason"] == "outside_ladder"

        # ladder_non_monotone — the bracket inverts, so `lo <= sale <= hi` can
        # never hold (lo is the BELOW rung, not min(lo, hi)).
        inverted = [_slab_comp(2000, 8.5, age=10), _slab_comp(3000, 9.2, age=12),
                    _slab_comp(2500, 9.6, age=14), _slab_comp(4000, 9.8, age=16)]
        out = _graded(inverted + [_slab_comp(2800, 9.4, age=1)], 9.4)
        assert out["flag_reason"] == "ladder_non_monotone"

    def test_the_bracket_rungs_are_the_ladder_s_own(self):
        """Not recomputed here: the same `_nearest_rungs` call the ladder tier
        makes, at the same eligibility bar, so the two can never disagree
        about which rungs surround a target."""
        comps = self._rungs() + [_slab_comp(1000, 4.5, age=1)]
        pool, _, _ = _pool(comps)
        nearest = fm._nearest_rungs(
            fm.bucket_weighted_medians(pool), fm.bucket_effective_n(pool),
            4.5, fm.GRADED_LADDER_MIN_BUCKET_N)
        bracket = _graded(comps, 4.5)["lone_sale_bracket"]
        assert bracket["lo_grade"] == nearest["below"]["grade"]
        assert bracket["hi_grade"] == nearest["above"]["grade"]
        assert bracket["lo"] == nearest["below"]["median"]
        assert bracket["hi"] == nearest["above"]["median"]

    def test_a_stale_rung_cannot_be_a_bracket_end(self):
        """The mirror of `TestGradedLadderTier`'s anchor test: a 0.5-weight
        rung is skipped, and the bracket widens past it — which here moves the
        floor from $900 (4.0) to $500 (2.5) and lets a $700 sale in that the
        4.0 rung would have excluded."""
        stale_40 = [_slab_comp(900, 4.0, age=200), _slab_comp(1400, 5.5, age=12),
                    _slab_comp(1500, 6.0, age=14), _slab_comp(500, 2.5, age=16)]
        out = _graded(stale_40 + [_slab_comp(700, 4.5, age=1)], 4.5)
        assert out["pricing_basis"] == "lone_sale"
        assert out["lone_sale_bracket"]["lo_grade"] == 2.5

    def test_clean_rounding_can_print_just_past_the_bracket_top(self):
        """A known, bounded edge, pinned so nobody 'fixes' it silently. The
        price is the SALE, clean-rounded exactly as the ladder rounds its own
        point, so the printed number can land up to half a clean step above
        `hi`. `hi` is a neighbouring rung's median, not a cap — the bracket is
        what admitted the sale, not a bound on it.
        """
        rungs = [_slab_comp(500, 4.0, age=10), _slab_comp(1015, 5.5, age=12),
                 _slab_comp(1500, 6.0, age=14), _slab_comp(450, 2.5, age=16)]
        out = _graded(rungs + [_slab_comp(1013, 4.5, age=1)], 4.5)
        assert out["pricing_basis"] == "lone_sale"
        assert out["fmv_high"] == 1025                    # the sale, rounded
        assert out["lone_sale_bracket"]["hi"] == 1015.0
        assert out["fmv_high"] - out["lone_sale_bracket"]["hi"] < fm._clean_step(
            out["fmv_high"])

    def test_the_exact_tier_still_wins_at_effective_n_two(self):
        """Ordering: a bucket that clears the exact tier's gate never reaches
        this one, even when its sales sit neatly inside a bracket."""
        comps = self._rungs() + [_slab_comp(1000, 4.5, age=1, product_id="a"),
                                 _slab_comp(1020, 4.5, age=2, product_id="b")]
        assert _graded(comps, 4.5)["pricing_basis"] == "direct"

    def test_a_non_universal_target_never_reaches_any_tier(self):
        """The identity filter runs FIRST and off the listing alone (R31), so
        a Signature Series or non-CGC/CBCS slab is refused before a comp is
        fetched — there is no pool for this tier to find a lone sale in."""
        punt = fm.graded_punt("label_signature_series", certifier="cgc",
                              label="signature_series")
        assert punt["pricing_basis"] is None
        assert punt["lone_sale_bracket"] is None
        assert punt["max_bid"] is None


class TestGradedPuntEvidence:
    """BUI-940: a refused (or ladder-priced) row still names the exact-grade
    sale(s) and the nearest anchor-eligible rung on each side, so a human
    reading a punt doesn't have to reopen the results file. Additive only —
    `exact_sales` stays a bare price list, `flag_reason`/`max_bid`/
    `fmv_low`/`fmv_high` are untouched by any of this."""

    def test_exact_sales_detail_carries_price_and_sold_date(self):
        rungs = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(1500, 6.0, age=14), _slab_comp(500, 2.5, age=16)]
        lone = _slab_comp(700, 4.5, age=1)
        out = _graded(rungs + [lone], 4.5)
        assert out["exact_sales"] == [700.0]  # unchanged
        assert out["exact_sales_detail"] == [
            {"price": 700.0, "sold_date": (_GREF - timedelta(days=1)).isoformat()}]

    def test_nearest_rungs_reports_both_sides_on_non_monotone_refusal(self):
        """The BUI-940 ticket's own fixture shape: Batman #227-style — the
        two bracketing rungs a human needs are exactly what tripped the
        refusal, so they must ride along with it, not just the reason."""
        comps = [_slab_comp(2000, 8.5, age=10), _slab_comp(3000, 9.2, age=12),
                 _slab_comp(2500, 9.6, age=14), _slab_comp(4000, 9.8, age=16)]
        out = _graded(comps, 9.4)
        assert out["flag_reason"] == "ladder_non_monotone"
        assert out["nearest_rungs"] == {
            "below": {"grade": 9.2, "median": 3000.0, "n": 1.0},
            "above": {"grade": 9.6, "median": 2500.0, "n": 1.0},
        }

    def test_nearest_rungs_is_one_sided_outside_the_ladder(self):
        """`outside_ladder` is exactly the case `_bracket_interpolate` can't
        express (it needs both sides) — `nearest_rungs` still reports the
        side that exists rather than going blank because the other doesn't."""
        comps = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(1500, 6.0, age=14)]
        out = _graded(comps, 9.8)
        assert out["flag_reason"] == "outside_ladder"
        assert out["nearest_rungs"] == {
            "below": {"grade": 6.0, "median": 1500.0, "n": 1.0},
            "above": None,
        }

    def test_nearest_rungs_still_populated_on_ladder_too_thin(self):
        comps = [_slab_comp(900, 4.0, age=10), _slab_comp(1400, 5.5, age=12),
                 _slab_comp(700, 4.5, age=1)]
        out = _graded(comps, 4.5)
        assert out["flag_reason"] == "ladder_too_thin"
        assert out["nearest_rungs"] == {
            "below": {"grade": 4.0, "median": 900.0, "n": 1.0},
            "above": {"grade": 5.5, "median": 1400.0, "n": 1.0},
        }

    def test_no_evidence_on_a_pre_fetch_punt(self):
        """`graded_punt` refuses before any comps are fetched (a Signature
        Series slab, say) — there is nothing to report, and this must not
        fabricate any."""
        out = fm.graded_punt("label_signature_series", certifier="cgc",
                             label="signature_series")
        assert out["exact_sales_detail"] == []
        assert out["nearest_rungs"] is None

    def test_no_evidence_when_the_pool_is_empty(self):
        out = _graded([], 4.5)  # nothing survives the identity + age filters
        assert out["flag_reason"] == "no_certifier_pool"
        assert out["exact_sales_detail"] == []
        assert out["nearest_rungs"] is None


class TestGradedPageQuality:
    def _pool(self, whites):
        pool = [_slab_comp(1000 + i, 9.4, age=i, page_quality="ow_w",
                           product_id=f"o{i}") for i in range(4)]
        pool += [_slab_comp(3000 + i, 9.4, age=i, page_quality="white",
                            product_id=f"w{i}") for i in range(whites)]
        return pool

    def test_two_matching_comps_scope_the_pool(self):
        out = _graded(self._pool(2), 9.4, page_quality="white")
        assert out["page_quality_fallback"] is False
        assert out["pool_n"] == 2
        assert out["fmv_low"] >= 3000

    def test_one_matching_comp_falls_back_and_says_so(self):
        out = _graded(self._pool(1), 9.4, page_quality="white")
        assert out["page_quality_fallback"] is True
        assert out["page_quality_fallback_reason"] == "too_few_matches"
        assert out["pool_n"] == 5

    def test_unknown_target_quality_is_not_a_filter(self):
        """`unknown` is the ABSENCE of a reading. Preferring the comps whose
        quality we also failed to read is a filter on parser coverage, not a
        quality match."""
        for value in (None, "unknown"):
            out = _graded(self._pool(2), 9.4, page_quality=value)
            assert out["page_quality_fallback"] is False
            assert out["page_quality_fallback_reason"] is None
            assert out["pool_n"] == 6


class TestGradedPageQualityLadderFallback:
    """BUI-939/937: page-quality scoping cannot starve the ladder tier.

    The exact tier's "prefer same-quality comps" rule (`TestGradedPageQuality`
    above) is a DIFFERENT rule from this one. That rule decides which pool
    the EXACT bucket is read from; this one says which pool the LADDER's
    neighbour rungs are read from — since BUI-937, always the whole same-label
    pool, so there is no rung count left to get wrong.
    """

    def test_real_invincible_1_94_white_case_widens_and_prices(self):
        """The BUI-939 case: Invincible #1 CGC 9.4 white pages, 9 live comps
        of which 3 are white (matching >= 2, so the OLD `too_few_matches`
        fallback never fires — this is the starvation-only bug). Scoped to
        white the ladder sees only the 9.2 and 9.8 rungs (2, one short of
        `GRADED_LADDER_MIN_RUNGS`) and used to refuse `ladder_too_thin`
        outright, even though the unscoped pool has a 9.6 rung too."""
        white = [_slab_comp(610, 9.2, age=5, page_quality="white",
                            product_id="w1"),
                 _slab_comp(3609, 9.4, age=6, page_quality="white",
                            product_id="w2"),
                 _slab_comp(5000, 9.8, age=7, page_quality="white",
                            product_id="w3")]
        other = [_slab_comp(700, 9.2, age=8, page_quality="cream",
                            product_id="c1"),
                 _slab_comp(750, 9.2, age=9, page_quality="cream",
                            product_id="c2"),
                 _slab_comp(3400, 9.4, age=10, page_quality="cream",
                           product_id="c3"),
                 _slab_comp(4200, 9.6, age=11, page_quality="cream",
                            product_id="c4"),
                 _slab_comp(4300, 9.6, age=12, page_quality="cream",
                            product_id="c5"),
                 _slab_comp(4800, 9.8, age=13, page_quality="cream",
                            product_id="c6")]
        pool = white + other
        assert len(pool) == 9  # matches the real 9-live-comp pool

        # Proof the bug is real: scoped to white alone, only 2 rungs remain
        # (9.2, 9.8) besides the target — one short of GRADED_LADDER_MIN_RUNGS.
        scoped, fell_back, _ = fm._graded_page_quality_filter(white + other,
                                                               "white")
        assert fell_back is False  # 3 matches >= 2, old rule doesn't fire
        rungs = {c["grade"] for c in scoped} - {9.4}
        assert rungs == {9.2, 9.8}

        out = _graded(pool, 9.4, page_quality="white")
        assert out["flag_reason"] is None
        assert out["pricing_basis"] == "ladder"
        assert out["page_quality_fallback"] is True
        assert out["page_quality_fallback_reason"] == "ladder_starved"
        assert out["pool_n"] == 9
        # The bracket comes from the WIDENED (9.2/9.6) pair, not the
        # scoped-only (9.2/9.8) pair — proof the full pool, not just the two
        # white-only rungs, fed the ladder.
        assert out["graded_ladder"]["grade_below"] == 9.2
        assert out["graded_ladder"]["grade_above"] == 9.6

    def test_scoped_pool_pricing_direct_is_never_widened(self):
        """Direct pricing still prefers same-quality comps even when the
        UNSCOPED pool's ladder rungs (irrelevant here) would be starved: the
        exact tier is decided, and satisfied, before the starvation check
        ever runs."""
        white = [_slab_comp(3000, 9.4, age=1, page_quality="white",
                            product_id="w1"),
                 _slab_comp(3200, 9.4, age=2, page_quality="white",
                            product_id="w2")]
        # Only one other rung exists at all, so the unscoped pool's ladder
        # (were it ever consulted) would itself be starved.
        other = [_slab_comp(900, 9.2, age=3, page_quality="cream",
                            product_id="c1")]
        out = _graded(white + other, 9.4, page_quality="white")
        assert out["pricing_basis"] == "direct"
        assert out["page_quality_fallback"] is False
        assert out["page_quality_fallback_reason"] is None
        assert out["pool_n"] == 2
        assert out["fmv_low"] >= 3000

    def test_a_scoped_ladder_row_reads_every_quality_not_just_enough_rungs(self):
        """BUI-937 replaced BUI-939's rung count: the ladder reads the whole
        same-label pool even when the SCOPED pool had rungs to spare.

        The scoped pool here has exactly `GRADED_LADDER_MIN_RUNGS` (3) white
        rungs — 9.0/9.6/9.8 — so the old conditional widen would have left it
        alone and bracketed the 9.4 target across the 9.0-to-9.6 gap. A cream
        9.2 sale sits inside that gap, and reading it TIGHTENS the bracket,
        which is the whole argument for never scoping the ladder: a nearer rung
        is better evidence than a same-quality one two grades away.
        """
        white = [_slab_comp(500, 9.0, age=1, page_quality="white",
                            product_id="w1"),
                 _slab_comp(4200, 9.6, age=3, page_quality="white",
                            product_id="w2"),
                 _slab_comp(5000, 9.8, age=4, page_quality="white",
                            product_id="w3")]
        other = [_slab_comp(900, 9.2, age=5, page_quality="cream",
                            product_id="c1")]

        scoped_only = _graded(white, 9.4, page_quality="white")
        assert scoped_only["graded_ladder"]["grade_below"] == 9.0

        out = _graded(white + other, 9.4, page_quality="white")
        assert out["flag_reason"] is None
        assert out["pricing_basis"] == "ladder"
        assert out["page_quality_fallback"] is True
        assert out["page_quality_fallback_reason"] == "ladder_starved"
        assert out["pool_n"] == 4
        assert out["graded_ladder"]["grade_below"] == 9.2  # the cream rung
        assert out["graded_ladder"]["grade_above"] == 9.6
        # And the tighter bracket happens to lower the cap here — stated so a
        # regression that silently restores the 9.0 anchor is visible as money.
        assert out["max_bid"] < scoped_only["max_bid"]

    def test_still_refuses_when_the_full_pool_is_also_starved(self):
        """Widening can't invent rungs that don't exist: if the WHOLE pool
        (not just the scoped one) has fewer than `GRADED_LADDER_MIN_RUNGS`
        anchor-eligible rungs, the book still refuses `ladder_too_thin` —
        just after trying the wider pool first, not instead of it."""
        white = [_slab_comp(610, 9.2, age=1, page_quality="white",
                            product_id="w1"),
                 _slab_comp(5000, 9.8, age=2, page_quality="white",
                            product_id="w2")]
        # Adds no new rung (same 9.2 grade as an existing white comp), so the
        # unscoped pool still only has 2 rungs besides the target.
        other = [_slab_comp(700, 9.2, age=3, page_quality="cream",
                            product_id="c1")]
        out = _graded(white + other, 9.4, page_quality="white")
        assert out["flag_reason"] == "ladder_too_thin"
        assert out["page_quality_fallback"] is True
        assert out["page_quality_fallback_reason"] == "ladder_starved"
        assert out["pool_n"] == 3  # widened to the full 3-comp pool

    def test_target_rung_is_the_only_matching_rung(self):
        """All the same-quality comps happen to sit AT the target grade —
        zero eligible rungs remain in the scoped pool once it's dropped, not
        merely too few. Still widens and still prices, off the wider pool's
        rungs alone."""
        # All three white comps are stale (weight 0.5 each -> effective n
        # 1.5, below the exact tier's floor) and all at the target grade, so
        # scoping to white leaves NOTHING to anchor a ladder with.
        white = [_slab_comp(3000 + i, 9.4, age=200, page_quality="white",
                            product_id=f"w{i}") for i in range(3)]
        other = [_slab_comp(900, 9.2, age=1, page_quality="cream",
                            product_id="c1"),
                 _slab_comp(4200, 9.6, age=2, page_quality="cream",
                            product_id="c2"),
                 _slab_comp(4800, 9.8, age=3, page_quality="cream",
                            product_id="c3")]
        scoped, fell_back, _ = fm._graded_page_quality_filter(
            white + other, "white")
        assert fell_back is False
        assert {c["grade"] for c in scoped} == {9.4}  # zero other rungs

        out = _graded(white + other, 9.4, page_quality="white")
        assert out["flag_reason"] is None
        assert out["pricing_basis"] == "ladder"
        assert out["page_quality_fallback"] is True
        assert out["page_quality_fallback_reason"] == "ladder_starved"
        assert out["graded_ladder"]["grade_below"] == 9.2
        assert out["graded_ladder"]["grade_above"] == 9.6


class TestGradedExactTierIsNotReRunAfterAWiden:
    """BUI-943: a widened pool's exact-grade sales do not reopen the tier.

    The pool here is a white 9.4 target over white 9.2/9.6/9.8 rungs plus TWO
    full-weight cream 9.4 sales — enough effective n at the exact grade to
    clear the exact tier's gate, if the gate were ever asked a second time on
    the wider pool. It is not: the gate reads the page-quality-scoped bucket,
    once.
    """

    _WHITE_RUNGS = [_slab_comp(610, 9.2, age=3, page_quality="white",
                               product_id="w1"),
                    _slab_comp(4200, 9.6, age=7, page_quality="white",
                               product_id="w2"),
                    _slab_comp(5000, 9.8, age=6, page_quality="white",
                               product_id="w3")]
    _CREAM_EXACTS = [_slab_comp(3400, 9.4, age=9, page_quality="cream",
                                product_id="c1"),
                     _slab_comp(3600, 9.4, age=12, page_quality="cream",
                                product_id="c2")]

    def test_two_exact_sales_of_another_quality_still_price_by_ladder(self):
        out = _graded(self._WHITE_RUNGS + self._CREAM_EXACTS, 9.4,
                      page_quality="white")
        assert out["pricing_basis"] == "ladder"
        assert out["confidence"] == "LOW"
        assert out["bid_factor"] == 0.60
        assert out["page_quality_fallback"] is True
        assert out["page_quality_fallback_reason"] == "ladder_starved"
        # The two cream sales are dropped with the rest of the target rung and
        # the white 9.2/9.6 neighbours interpolate across the gap.
        assert out["graded_ladder"]["grade_below"] == 9.2
        assert out["graded_ladder"]["grade_above"] == 9.6
        assert out["fmv_high"] == 2400
        assert out["max_bid"] == 1450

    def test_the_dropped_exact_sales_are_still_reported_as_evidence(self):
        """`exact_effective_n` reads 2.0 on a `basis=ladder` row on purpose —
        it is the widened pool's evidence, printed by `_build_notes` as
        "recorded, NOT used as the price", and never an input to the tier."""
        out = _graded(self._WHITE_RUNGS + self._CREAM_EXACTS, 9.4,
                      page_quality="white")
        assert out["exact_effective_n"] == 2.0
        assert out["exact_sales"] == [3400.0, 3600.0]
        assert [d["price"] for d in out["exact_sales_detail"]] == [3400.0, 3600.0]

    def test_re_running_the_gate_would_have_raised_the_cap_not_the_band(self):
        """Why the ladder wins the decision, measured rather than asserted.

        Reading the same pool with NO page-quality reading is exactly what
        re-running the gate on the widened pool would do — the same bucket, the
        same envelope clamp. It lands on the identical $2,400 band and differs
        only in the haircut, 0.80 against the ladder's 0.60. Equal evidence,
        higher cap, paid for with the comps the preference declined.
        """
        pool = self._WHITE_RUNGS + self._CREAM_EXACTS
        ladder_row = _graded(pool, 9.4, page_quality="white")
        exact_rerun = _graded(pool, 9.4, page_quality=None)
        assert exact_rerun["pricing_basis"] == "direct"
        assert exact_rerun["fmv_high"] == ladder_row["fmv_high"] == 2400
        assert exact_rerun["bid_factor"] == 0.80
        assert exact_rerun["max_bid"] == 1925
        assert ladder_row["max_bid"] < exact_rerun["max_bid"]


class TestGradedPageQualityScopesTheExactBucketOnly:
    """BUI-937: the scoped pool is the exact BAND; the envelope is the market.

    Before this, a scoped pool whose only rung was the exact bucket left the
    thin-bucket band with no envelope to bound it — the gap BUI-930's own code
    comment named and BUI-179's dispersion guard was left holding alone. The
    clamp now reads every same-label comp, so the bound exists whenever the
    market has rungs either side of the target.
    """

    # Two white 9.6 sales in close agreement; the only other rungs in the pool
    # are a cream 9.4 and a cream 9.8, which imply ~$1,200 at 9.6.
    _WHITE_PAIR = [_slab_comp(5000, 9.6, age=3, page_quality="white",
                              product_id="w1"),
                   _slab_comp(5200, 9.6, age=6, page_quality="white",
                              product_id="w2")]
    _CREAM_RUNGS = [_slab_comp(1000, 9.4, age=9, page_quality="cream",
                               product_id="c1"),
                    _slab_comp(1400, 9.8, age=12, page_quality="cream",
                               product_id="c2")]

    def test_white_pair_is_clamped_by_the_unscoped_neighbour_rungs(self):
        """The ticket's case: the band prices from the two WHITE sales, and the
        cap is bounded by rungs the scoped pool does not contain."""
        out = _graded(self._WHITE_PAIR + self._CREAM_RUNGS, 9.6,
                      page_quality="white")
        assert out["pricing_basis"] == "direct"
        # Scoping was honoured — the band is the white pair's, not a widen.
        assert out["page_quality_fallback"] is False
        assert out["pool_n"] == 2
        assert out["exact_sales"] == [5000.0, 5200.0]
        # ... and the unscoped rungs bounded it.
        assert out["envelope_clamped"] is True
        assert out["fmv_low"] == out["median"] == out["fmv_high"] == 1200
        assert out["max_bid"] == 950
        assert sorted(out["graded_ladder"]["ladder"]) == [9.4, 9.6, 9.8]

    def test_the_same_pair_is_unclamped_when_the_market_has_no_other_rung(self):
        """The control: with nothing either side of 9.6 there is no envelope,
        so the pair prices itself — proof the clamp above came from the comps
        scoping had excluded and not from some new haircut."""
        out = _graded(self._WHITE_PAIR, 9.6, page_quality="white")
        assert out["envelope_clamped"] is False
        assert out["fmv_high"] == 5150
        assert out["max_bid"] == 4125

    def test_a_fat_other_quality_target_rung_does_not_disable_the_clamp(self):
        """The clamp's thin-bucket trigger reads the SCOPED bucket's effective
        n, not the whole pool's at that grade.

        Four cream 9.6 sales put the unscoped 9.6 rung past
        `OUTLIER_ROBUST_BUCKET_N`, which would wave the two-sale white band
        through unbounded if the trigger read the wider count. The band being
        bounded is the white pair's, so the count that decides whether it needs
        bounding is the white pair's too.
        """
        fat = [_slab_comp(900 + i, 9.6, age=10 + i, page_quality="cream",
                          product_id=f"f{i}") for i in range(4)]
        out = _graded(self._WHITE_PAIR + fat + self._CREAM_RUNGS, 9.6,
                      page_quality="white")
        assert out["exact_effective_n"] == 2.0  # the white pair, not 6.0
        assert out["envelope_clamped"] is True
        assert out["fmv_high"] == 1200

    def test_a_divergent_scoped_pair_with_no_envelope_still_refuses(self):
        """BUI-930's dispersion guard is not regressed: at the TOP of the
        ladder there is still no envelope, and two sales five times apart are
        still not one market."""
        white = [_slab_comp(1000, 9.8, age=3, page_quality="white",
                            product_id="w1"),
                 _slab_comp(5000, 9.8, age=6, page_quality="white",
                            product_id="w2")]
        below = [_slab_comp(2000, 9.6, age=9, page_quality="cream",
                            product_id="c1"),
                 _slab_comp(1500, 9.4, age=12, page_quality="cream",
                            product_id="c2")]
        out = _graded(white + below, 9.8, page_quality="white")
        assert out["flag_reason"] == "too_sparse"
        assert out["max_bid"] is None

    def test_a_divergent_scoped_pair_is_clamped_rather_than_refused(self):
        """The deliberate trade this change makes, stated as a test.

        The same divergent pair one rung lower IS bracketed by the whole pool,
        so the envelope binds and the book prices instead of refusing — which
        is what the guard's own comment always said it was for ("applied where
        BUI-349's cannot reach"). The cap that results is the neighbours'
        envelope, well under either sale, so nothing rises: the dispersion
        guard stands down only when something stricter has taken over.
        """
        white = [_slab_comp(1000, 9.6, age=3, page_quality="white",
                            product_id="w1"),
                 _slab_comp(5000, 9.6, age=6, page_quality="white",
                            product_id="w2")]
        around = [_slab_comp(900, 9.4, age=9, page_quality="cream",
                             product_id="c1"),
                  _slab_comp(1500, 9.8, age=12, page_quality="cream",
                             product_id="c2")]
        out = _graded(white + around, 9.6, page_quality="white")
        assert out["flag_reason"] is None
        assert out["envelope_clamped"] is True
        assert out["fmv_high"] == 1200
        assert out["max_bid"] == 950


class TestGradedRefusalShape:
    @pytest.mark.parametrize("reason,comps,grade", [
        ("no_certifier_pool", [], 9.4),
        ("ladder_too_thin",
         [{"price": 100, "grade": 9.2, "sold_date": "2026-09-01"},
          {"price": 200, "grade": 9.6, "sold_date": "2026-09-01"}], 9.4),
    ])
    def test_a_refusal_is_the_ordinary_needs_manual_shape(self, reason, comps,
                                                          grade):
        out = _graded(comps, grade)
        assert out["flag_reason"] == reason
        assert out["fmv_low"] is out["fmv_high"] is out["median"] is None
        assert out["max_bid"] is None
        assert out["confidence"] == "LOW"
        assert out["pricing_basis"] is None

    def test_every_reason_is_declared(self):
        """The runner posts these to `POST /api/comics`, where an unlisted
        value 422s and the server discards the WHOLE upsert."""
        assert set(fm.GRADED_FLAG_REASONS) == {
            "no_certifier_pool", "ladder_too_thin", "ladder_non_monotone",
            "outside_ladder", "too_sparse"}

    def test_a_punt_reports_no_pool_rather_than_an_empty_one(self):
        """`slab_pool=0` in the notes would read as "we looked at this
        certifier's sales and found none", which is a different claim from
        "we never looked"."""
        punt = fm.graded_punt("certifier_other", certifier="other",
                              label="universal")
        assert punt["pool_n"] is None

    def test_punt_matches_the_priced_shape(self):
        punt = fm.graded_punt("label_signature_series", certifier="cgc",
                              label="signature_series")
        priced = _graded([_slab_comp(1000, 9.4, age=1),
                          _slab_comp(1100, 9.4, age=2),
                          _slab_comp(900, 9.2, age=3),
                          _slab_comp(1400, 9.6, age=4)], 9.4)
        assert set(punt) == set(priced)
        assert punt["flag_reason"] == "label_signature_series"
        assert punt["label"] == "signature_series"


class TestGradedNeverTouchesTheRawMachinery:
    def test_graded_output_declares_no_raw_tier(self):
        out = _graded([_slab_comp(1000, 9.4, age=1),
                       _slab_comp(1100, 9.4, age=2),
                       _slab_comp(900, 9.2, age=3),
                       _slab_comp(1400, 9.6, age=4)], 9.4)
        assert out["graded"] is True
        assert out["cgc_proxy"] is False
        assert out["cgc_ladder"] is None
        assert out["ungraded_anchor"] is None
        assert out["anchor_diverges"] is False
        assert out["cgc_cross_check"] is None
        # `pricing_basis` is the carrier now; setting `interpolated` too would
        # make the notes and the table announce the same fact twice.
        assert out["interpolated"] is False
        assert out["window"] is None

    def test_graded_never_calls_build_pool(self, monkeypatch):
        def _boom(*a, **k):  # pragma: no cover - the assertion is that it is
            raise AssertionError("graded_fmv must never widen a grade window")
        monkeypatch.setattr(fm, "build_pool", _boom)
        out = _graded([_slab_comp(1000, 9.4, age=1),
                       _slab_comp(1100, 9.4, age=2),
                       _slab_comp(900, 9.2, age=3),
                       _slab_comp(1400, 9.6, age=4)], 9.4)
        assert out["fmv_high"] is not None
