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

    def test_too_wide_flags(self):
        # Iron Man #124 shape: target 7.0, pool brackets but spans 4 grade points
        comps = [_comp(50, 5.0), _comp(300, 9.0), _comp(320, 9.0)]
        out = fm.compute_fmv(comps, target_grade=7.0)
        assert out["flag_reason"] == "too_wide"
        assert out["grade_span"] == 4.0
        assert out["fmv_low"] is None

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
