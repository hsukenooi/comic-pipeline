"""Tests for fmv_math.py — pure functions, no I/O, no network."""

import pytest

import fmv_math as fm


def _comp(price, grade=None):
    return {"price": price, "grade": grade}


# ─── build_pool ────────────────────────────────────────────────────────────────

class TestBuildPool:
    def test_narrow_window_when_dense(self):
        comps = [_comp(p, 9.2) for p in [10, 12, 11, 13, 14, 15]]  # all at 9.2
        pool, window = fm.build_pool(comps, target_grade=9.2)
        assert window == 0.5
        assert sorted(pool) == [10, 11, 12, 13, 14, 15]

    def test_widens_when_sparse(self):
        # Only 2 comps at ±0.5 — should widen to ±1.0
        comps = [_comp(10, 9.2), _comp(11, 9.2),
                 _comp(20, 8.0), _comp(21, 8.0), _comp(22, 8.0)]
        pool, window = fm.build_pool(comps, target_grade=9.0)
        assert window == 1.0
        assert len(pool) == 5

    def test_drops_no_grade(self):
        comps = [_comp(10, 9.2), _comp(99, None)]  # 99 has no grade
        pool, _ = fm.build_pool(comps, target_grade=9.2)
        assert 99 not in pool


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
