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
