"""BUI-1219: the five bid tiers and the median-anchored raw bid cap.

Covers the tier boundaries (VERY HIGH vs HIGH on window and cv, MEDIUM vs
LOW), the photo grade-confidence haircut fallback, the NULL-median fallback
on a cache hit, the fmv_high clamp, the path gate (interpolated, proxy,
graded never take the median rule), and cached-vs-fresh equality through the
real `_upsert_fmv` body and `_fmv_from_db_row` projection.
"""
from __future__ import annotations

import itertools

import pytest

import fmv_math as fm
import fmv_runner


def _comps(prices, grade=8.0):
    return [{"price": p, "grade": grade, "product_id": f"id{i}", "title": ""}
            for i, p in enumerate(prices)]


# Pools whose tiers are pinned by the tests below.
VERY_HIGH_POOL = [100, 105, 110, 115, 120, 125, 130]          # HIGH, ±0.5, cv 9%
MEDIUM_POOL = [50, 75, 100, 100, 100, 100]                    # MEDIUM-HIGH
LOW_POOL = [60, 100, 140]                                     # MEDIUM-LOW


# ─── bid_tier boundaries ─────────────────────────────────────────────────────

@pytest.mark.parametrize("label, window, cv, tier", [
    ("HIGH", 0.5, 0.2499, "VERY HIGH"),
    ("HIGH", 0.5, 0.25, "HIGH"),        # cv boundary is strict (< 25%)
    ("HIGH", 1.0, 0.10, "HIGH"),        # window past ±0.5
    ("HIGH", 0.5, None, "HIGH"),        # unknown cv cannot prove a tight pool
    ("HIGH", None, 0.10, "HIGH"),       # unknown window likewise
    ("MEDIUM-HIGH", 0.5, 0.10, "MEDIUM"),
    ("MEDIUM", 0.5, 0.10, "MEDIUM"),
    ("MEDIUM-LOW", 0.5, 0.10, "LOW"),
    ("LOW", 0.5, 0.10, "VERY LOW"),
    ("", 0.5, 0.10, "VERY LOW"),
    (None, 0.5, 0.10, "VERY LOW"),
    ("BOGUS", 0.5, 0.10, "VERY LOW"),   # unknown fails toward today's rule
])
def test_bid_tier_boundaries(label, window, cv, tier):
    assert fm.bid_tier(label, window, cv) == tier


# ─── median_bid_cap ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", ["VERY HIGH", "HIGH", "MEDIUM"])
def test_median_tiers_cap_at_the_median(tier):
    assert fm.median_bid_cap(120, 140, tier, fm.BASE_BID_FACTOR) == 120


@pytest.mark.parametrize("tier", ["LOW", "VERY LOW", None, "bogus"])
def test_low_tiers_take_no_median_cap(tier):
    assert fm.median_bid_cap(120, 140, tier, fm.BASE_BID_FACTOR) is None


@pytest.mark.parametrize("factor", [0.70, 0.60, 0.79])
def test_engaged_haircut_keeps_todays_rule(factor):
    assert fm.median_bid_cap(120, 140, "VERY HIGH", factor) is None


def test_missing_median_or_high_takes_no_median_cap():
    assert fm.median_bid_cap(None, 140, "HIGH", 0.80) is None
    assert fm.median_bid_cap(120, None, "HIGH", 0.80) is None


def test_median_cap_is_clamped_to_fmv_high():
    # A median above the band's top (only possible on a hand-written row)
    # can never lift the cap past fmv_high.
    assert fm.median_bid_cap(150, 140, "HIGH", 0.80) == 140
    # clean_round rounding UP past fmv_high is clamped too.
    assert fm.median_bid_cap(197, 195, "HIGH", 0.80) == 195


def test_median_cap_uses_production_clean_round():
    assert fm.median_bid_cap(117, 300, "MEDIUM", 0.80) == fm.clean_round(117)


# ─── compute_fmv: tiers, path gate, haircut fallback ─────────────────────────

def test_very_high_pool_bids_the_median():
    out = fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0)
    assert (out["confidence"], out["bid_tier"]) == ("HIGH", "VERY HIGH")
    assert out["bid_basis"] == "median"
    assert out["max_bid"] == out["median"] == 120
    assert out["max_bid"] > fm.clean_round(0.8 * out["fmv_high"])


def test_medium_vs_low_boundary():
    medium = fm.compute_fmv(_comps(MEDIUM_POOL), 8.0)
    low = fm.compute_fmv(_comps(LOW_POOL), 8.0)
    assert (medium["bid_tier"], medium["bid_basis"]) == ("MEDIUM", "median")
    assert (low["bid_tier"], low["bid_basis"]) == ("LOW", "high")
    assert low["max_bid"] == fm.clean_round(0.8 * low["fmv_high"])


def test_wide_window_high_pool_is_medium_tier_not_very_high():
    # Comps only at ±1.5 → the BUI-86 R7 cap labels it MEDIUM.
    pairs = [(100, 7.0), (105, 7.0), (110, 7.0), (115, 7.0),
             (120, 8.5), (125, 8.5), (130, 8.5), (135, 8.5), (140, 8.5)]
    out = fm.compute_fmv(
        [{"price": p, "grade": g, "product_id": str(i), "title": ""}
         for i, (p, g) in enumerate(pairs)], 7.0)
    assert out["window"] > fm.WIDE_GRADE_WINDOW
    assert (out["confidence"], out["bid_tier"]) == ("MEDIUM", "MEDIUM")


@pytest.mark.parametrize("grade_conf, factor", [("medium-low", 0.70), ("low", 0.60),
                                                ("typo", 0.60)])
def test_grade_haircut_falls_back_to_factor_times_high(grade_conf, factor):
    out = fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0, grade_confidence=grade_conf)
    assert out["bid_tier"] == "VERY HIGH"
    assert out["bid_factor"] == factor
    assert out["bid_basis"] == "high"
    assert out["max_bid"] == fm.clean_round(factor * out["fmv_high"])


@pytest.mark.parametrize("grade_conf", ["high", "medium", None, ""])
def test_no_haircut_grade_confidence_keeps_the_median_rule(grade_conf):
    out = fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0, grade_confidence=grade_conf)
    assert out["bid_basis"] == "median"


def test_interpolated_book_never_takes_the_median_rule():
    pairs = [(40, 5.0), (60, 5.0), (300, 9.0), (320, 9.0)]
    out = fm.compute_fmv(
        [{"price": p, "grade": g, "product_id": str(i), "title": ""}
         for i, (p, g) in enumerate(pairs)], 7.0)
    assert out["interpolated"]
    assert (out["bid_tier"], out["bid_basis"]) == ("VERY LOW", "high")
    assert out["max_bid"] == fm.clean_round(fm.INTERPOLATED_BID_FACTOR * out["fmv_high"])


def test_flagged_book_has_no_tier_and_no_basis():
    out = fm.compute_fmv(_comps([40, 42, 44, 45, 41], grade=9.0), 9.6)
    assert out["flag_reason"] is not None
    assert (out["bid_tier"], out["bid_basis"], out["max_bid"]) == (None, None, None)


def test_median_cap_never_exceeds_fmv_high_over_a_pool_sweep():
    """The clamp is a backstop: across a sweep of pool shapes no max_bid,
    median-based or not, ever exceeds fmv_high."""
    prices = [20, 35, 50, 80, 120, 200, 260, 400, 900]
    for size in (3, 4, 5, 6, 8):
        for pool in itertools.combinations_with_replacement(prices, size):
            out = fm.compute_fmv(_comps(list(pool)), 8.0)
            if out["max_bid"] is None:
                continue
            assert out["max_bid"] <= out["fmv_high"], pool


# ─── persistence + cache-hit parity ──────────────────────────────────────────

def _posted_body(fmv: dict, monkeypatch) -> dict:
    captured = {}

    def fake_post(url, body, **_kw):
        captured.update(body)
        return {"id": 1, "comic_id": 1, "fmv_id": 1,
                "certifier": body.get("certifier", "none")}

    monkeypatch.setattr(fmv_runner, "_post_json", fake_post)
    fmv_runner._upsert_fmv("http://x", {"title": "T", "issue": "1",
                                        "grade": 8.0}, fmv)
    return captured


def _served_row(body: dict) -> dict:
    """What GET /api/comics serves back for that write on a raw row."""
    return {
        "fmv_low": body["fmv_low"], "fmv_high": body["fmv_high"],
        "fmv_comps": body["fmv_comps"],
        "fmv_confidence": body["fmv_confidence"],
        "fmv_notes": body["fmv_notes"],
        "pricing_basis": "direct", "certifier": "none", "label": "universal",
        # A REAL column comes back as a float.
        "fmv_median": (float(body["fmv_median"])
                       if body.get("fmv_median") is not None else None),
        "fmv_bid_tier": body.get("fmv_bid_tier"),
    }


POOLS = [VERY_HIGH_POOL, MEDIUM_POOL, LOW_POOL,
         [100, 110, 120, 130, 140, 150, 160, 170, 180, 190],
         [30, 32, 34, 36, 38, 40, 90, 95],
         [200, 210, 260, 300, 310, 330]]


@pytest.mark.parametrize("pool", POOLS)
@pytest.mark.parametrize("grade_conf", [None, "high", "medium", "medium-low", "low"])
def test_cached_row_bids_exactly_what_a_fresh_run_did(pool, grade_conf, monkeypatch):
    fresh = fm.compute_fmv(_comps(pool), 8.0, grade_confidence=grade_conf)
    body = _posted_body(fresh, monkeypatch)
    cached = fmv_runner._fmv_from_db_row(_served_row(body), grade_conf)
    assert cached["bid_tier"] == fresh["bid_tier"]
    if fresh["confidence"] == "MEDIUM-LOW" and grade_conf:
        # Pre-existing, out of scope: a MEDIUM-LOW label stores as 'low', so
        # a cache hit WITH a photo grade haircuts at the LOW rung (0.60)
        # where the fresh run used 0.70. Conservative direction only.
        assert cached["max_bid"] <= fresh["max_bid"]
        return
    assert cached["max_bid"] == fresh["max_bid"]
    assert cached["bid_basis"] == fresh["bid_basis"]


def test_raw_direct_row_posts_median_and_tier(monkeypatch):
    fresh = fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0)
    body = _posted_body(fresh, monkeypatch)
    assert body["fmv_median"] == 120
    assert body["fmv_bid_tier"] == "VERY HIGH"


def test_interpolated_and_proxy_rows_post_neither(monkeypatch):
    pairs = [(40, 5.0), (60, 5.0), (300, 9.0), (320, 9.0)]
    interp = fm.compute_fmv(
        [{"price": p, "grade": g, "product_id": str(i), "title": ""}
         for i, (p, g) in enumerate(pairs)], 7.0)
    assert "fmv_median" not in _posted_body(interp, monkeypatch)
    proxy = {**fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0), "cgc_proxy": True}
    assert "fmv_median" not in _posted_body(proxy, monkeypatch)
    graded = {**fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0), "graded": True,
              "certifier": "cgc", "label": "universal", "pricing_basis": "direct"}
    assert "fmv_median" not in _posted_body(graded, monkeypatch)


def test_null_median_cached_row_keeps_todays_rule():
    row = {"fmv_low": 110, "fmv_high": 140, "fmv_comps": 7,
           "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=9% | label=HIGH",
           "pricing_basis": "direct", "certifier": "none"}
    # Pre-BUI-1219 row (old server: keys absent) and post-migration NULLs.
    for extra in ({}, {"fmv_median": None, "fmv_bid_tier": None}):
        out = fmv_runner._fmv_from_db_row({**row, **extra})
        assert out["max_bid"] == fm.clean_round(0.8 * 140) == 110
        assert out["bid_basis"] == "high"


@pytest.mark.parametrize("override", [
    {"fmv_confidence": "low"},              # median tier beside a 'low' label
    {"pricing_basis": "proxy"},
    {"pricing_basis": "interpolated"},
    {"certifier": "cgc"},
    {"fmv_bid_tier": "LOW"},
    {"fmv_bid_tier": "nonsense"},
    {"fmv_notes": "CGC proxy (raw ~ 0.50-0.55x slab) | window=±0.5"},
    # An external writer's un-capped HIGH past ±1.0 (BUI-182): the cache hit
    # caps the confidence to MEDIUM, which contradicts a stored HIGH tier.
    {"fmv_notes": "window=±1.5 | cv=9% | label=HIGH"},
])
def test_cached_row_outside_the_gate_keeps_todays_rule(override):
    row = {"fmv_low": 110, "fmv_high": 140, "fmv_comps": 7,
           "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=9% | label=HIGH",
           "pricing_basis": "direct", "certifier": "none",
           "fmv_median": 130.0, "fmv_bid_tier": "VERY HIGH", **override}
    out = fmv_runner._fmv_from_db_row(row)
    assert out["bid_basis"] == "high"
    assert out["max_bid"] <= fm.clean_round(0.8 * 140)


def test_cached_median_tier_with_grade_haircut_keeps_todays_rule():
    row = {"fmv_low": 110, "fmv_high": 140, "fmv_comps": 7,
           "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=9% | label=HIGH",
           "pricing_basis": "direct", "certifier": "none",
           "fmv_median": 120.0, "fmv_bid_tier": "VERY HIGH"}
    assert fmv_runner._fmv_from_db_row(row, "high")["max_bid"] == 120
    out = fmv_runner._fmv_from_db_row(row, "medium-low")
    assert out["max_bid"] == fm.clean_round(0.7 * 140)
    assert out["bid_basis"] == "high"


def test_table_prints_tier_and_median_basis(capsys):
    fresh = fm.compute_fmv(_comps(VERY_HIGH_POOL), 8.0)
    fmv_runner._print_table([{
        "input": {"title": "ASM", "issue": "50", "grade": 8.0},
        "fmv": fresh, "source": "fresh",
    }])
    out = capsys.readouterr().out
    assert "VERY HIGH" in out
    assert "$120 med" in out
