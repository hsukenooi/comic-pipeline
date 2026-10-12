"""BUI-1242: widen the PUBLISHED slab band without moving the bid.

`graded_fmv` publishes a +/-15% band on the ladder and lone-sale tiers and a
+/-15% floor (around the median) on the exact tier, per the BUI-1230 audit.
`max_bid` must stay byte-identical: it rides `bid_high`, the pre-widening top,
and so does a cache hit re-deriving the cap from the stored row.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

import fmv_math as fm
import fmv_runner

_REF = date(2026, 9, 1)
_GRADES = (4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.2, 9.4,
           9.6, 9.8)


def _comp(price, grade, age=5, pq="unknown", pid=None):
    return {"product_id": pid or f"p{price}-{grade}-{age}", "title": "",
            "price": price, "grade": grade, "page_quality": pq,
            "certifier": "cgc", "label": "universal",
            "sold_date": (_REF - timedelta(days=age)).isoformat()}


def _price(comps, grade, pq=None):
    return fm.graded_fmv(comps, grade, certifier="cgc", label="universal",
                         as_of=_REF, page_quality=pq)


def _unwidened(comps, grade, pq=None):
    """The pre-BUI-1242 answer: the same function with both widths at 0."""
    with patch.object(fm, "GRADED_POINT_BAND_HALF_WIDTH", 0.0), \
         patch.object(fm, "GRADED_DIRECT_BAND_HALF_WIDTH_FLOOR", 0.0):
        return _price(comps, grade, pq)


# ─── (a) the published band widens per tier ──────────────────────────────────

class TestPublishedBand:
    def test_ladder_point_is_published_plus_minus_15(self):
        comps = [_comp(900, 4.0), _comp(1400, 5.5), _comp(1500, 6.0),
                 _comp(2000, 6.5)]
        out = _price(comps, 4.5)
        assert out["pricing_basis"] == "ladder"
        point = out["bid_high"]
        assert out["median"] == point
        assert out["fmv_low"] == fm.clean_round(point * 0.85)
        assert out["fmv_high"] == fm.clean_round(point * 1.15)
        assert out["max_bid"] == fm.clean_round(point * 0.60)

    def test_lone_sale_is_published_plus_minus_15(self):
        comps = [_comp(1000, 4.5), _comp(900, 4.0), _comp(1400, 5.5),
                 _comp(1500, 6.0)]
        out = _price(comps, 4.5)
        assert out["pricing_basis"] == "lone_sale"
        assert (out["fmv_low"], out["median"], out["fmv_high"]) == (850, 1000, 1150)
        assert out["bid_high"] == 1000
        assert out["max_bid"] == fm.clean_round(1000 * 0.70) == 700

    def test_exact_tier_tight_band_gets_the_15_percent_floor(self):
        comps = [_comp(1000, 9.4, age=a, pid=f"e{a}") for a in range(1, 7)]
        out = _price(comps, 9.4)
        assert out["pricing_basis"] == "direct"
        assert (out["fmv_low"], out["fmv_high"]) == (850, 1150)
        assert out["bid_high"] == 1000
        assert out["max_bid"] == fm.clean_round(1000 * out["bid_factor"])

    def test_exact_tier_wide_band_is_left_alone(self):
        """The floor only widens: a Q25..Q75 already wider than +/-15% of the
        median publishes unchanged, and then bid_high == fmv_high."""
        prices = (600, 700, 1000, 1000, 1400, 1600)
        comps = [_comp(p, 9.4, age=i + 1, pid=f"w{i}")
                 for i, p in enumerate(prices)]
        out, before = _price(comps, 9.4), _unwidened(comps, 9.4)
        assert out["pricing_basis"] == "direct"
        assert (out["fmv_low"], out["fmv_high"]) == (
            before["fmv_low"], before["fmv_high"])
        assert out["bid_high"] == out["fmv_high"]

    def test_refusals_publish_nothing(self):
        out = _price([_comp(900, 4.0), _comp(1400, 5.5)], 4.5)
        assert out["flag_reason"] == "ladder_too_thin"
        assert (out["fmv_low"], out["fmv_high"], out["bid_high"],
                out["max_bid"]) == (None, None, None, None)


# ─── (b) max_bid is byte-identical before/after ──────────────────────────────

def _random_case(rng: random.Random):
    base = rng.choice((40, 150, 600, 2500, 9000))
    target = rng.choice(_GRADES)
    comps = []
    for i in range(rng.randint(2, 12)):
        g = rng.choice(_GRADES)
        level = base * (1.25 ** _GRADES.index(g))
        price = round(level * rng.uniform(0.7, 1.4), 2)
        comps.append(_comp(price, g, age=rng.randint(0, 300),
                           pq=rng.choice(("unknown", "white", "ow_w")),
                           pid=f"r{i}"))
    pq = rng.choice((None, "white"))
    return comps, target, pq


_UNCHANGED = ("flag_reason", "pricing_basis", "max_bid", "median", "bid_factor",
              "confidence", "envelope_clamped", "n")


def test_max_bid_is_byte_identical_across_a_random_sweep():
    rng = random.Random(1242)
    tiers: dict[str, int] = {}
    naive_would_move = 0
    for _ in range(4000):
        comps, target, pq = _random_case(rng)
        after, before = _price(comps, target, pq), _unwidened(comps, target, pq)
        for k in _UNCHANGED:
            assert after[k] == before[k], (k, comps, target, pq)
        if after["flag_reason"] is not None:
            continue
        basis = after["pricing_basis"]
        tiers[basis] = tiers.get(basis, 0) + 1
        # The bid basis is exactly the pre-widening top...
        assert after["bid_high"] == before["fmv_high"]
        assert after["max_bid"] == fm.clean_round(
            after["bid_high"] * after["bid_factor"])
        # ...and the published band only ever widened around it.
        assert after["fmv_low"] <= before["fmv_low"]
        assert after["fmv_high"] >= before["fmv_high"]
        if after["max_bid"] != fm.clean_round(
                after["fmv_high"] * after["bid_factor"]):
            naive_would_move += 1
    assert set(tiers) == {"direct", "lone_sale", "ladder"}, tiers
    assert min(tiers.values()) >= 20, tiers
    # A naive implementation (max_bid off the widened fmv_high) moves bids.
    assert naive_would_move > 100


# ─── cache hit + upsert + deploy-order probe ─────────────────────────────────

def _cached_row(**over):
    row = {"fmv_low": 850, "fmv_high": 1150, "fmv_comps": 1,
           "fmv_confidence": "low", "fmv_notes": "basis=lone_sale",
           "certifier": "cgc", "label": "universal",
           "pricing_basis": "lone_sale", "fmv_bid_high": 1000}
    row.update(over)
    return row


class TestCacheHit:
    def test_a_cache_hit_bids_what_the_fresh_run_did(self):
        comps = [_comp(1000, 4.5), _comp(900, 4.0), _comp(1400, 5.5),
                 _comp(1500, 6.0)]
        fresh = _price(comps, 4.5)
        cached = fmv_runner._fmv_from_db_row(_cached_row(
            fmv_low=fresh["fmv_low"], fmv_high=fresh["fmv_high"],
            fmv_bid_high=fresh["bid_high"]))
        assert cached["max_bid"] == fresh["max_bid"] == 700
        assert cached["bid_high"] == 1000

    def test_a_pre_bui1242_slab_row_bids_off_its_high(self):
        cached = fmv_runner._fmv_from_db_row(_cached_row(
            fmv_low=1000, fmv_high=1000, fmv_bid_high=None))
        assert cached["max_bid"] == 700

    def test_bid_high_above_high_is_clamped_to_high(self):
        cached = fmv_runner._fmv_from_db_row(_cached_row(fmv_bid_high=5000))
        assert cached["max_bid"] == fm.clean_round(1150 * 0.70)

    def test_a_raw_row_ignores_bid_high(self):
        cached = fmv_runner._fmv_from_db_row({
            "fmv_low": 80, "fmv_high": 100, "fmv_comps": 5,
            "fmv_confidence": "high", "fmv_notes": "", "certifier": "none",
            "fmv_bid_high": 10})
        assert cached["max_bid"] == 80 and cached["bid_high"] is None

    def test_lone_sale_provenance_names_the_sale_not_the_widened_high(self):
        cached = fmv_runner._fmv_from_db_row(_cached_row())
        row = {"input": {}, "fmv": {**cached, "graded": True}, "source": "cached"}
        assert fmv_runner._provenance(row) == "lone sale $1000"


def _post_capture():
    seen: dict = {}

    def _post(url, body, **kw):
        seen["body"] = body
        return {"id": 1, "comic_id": 1, "fmv_id": 1, "certifier": "cgc"}
    return seen, _post


def test_upsert_sends_bid_high_on_a_slab_row_only():
    comps = [_comp(1000, 4.5), _comp(900, 4.0), _comp(1400, 5.5),
             _comp(1500, 6.0)]
    fmv = _price(comps, 4.5)
    inp = {"title": "X", "issue": "1", "grade": 4.5}
    seen, post = _post_capture()
    with patch("fmv_runner._post_json", post):
        fmv_runner._upsert_fmv("http://s", inp, fmv)
    assert seen["body"]["fmv_bid_high"] == 1000
    assert seen["body"]["fmv_high"] == 1150

    raw = fm.compute_fmv([{"price": p, "grade": 8.0, "product_id": str(i),
                           "title": ""} for i, p in enumerate((80, 90, 100))],
                         8.0)
    seen, post = _post_capture()
    with patch("fmv_runner._post_json", post):
        fmv_runner._upsert_fmv("http://s", {**inp, "grade": 8.0}, raw)
    assert "fmv_bid_high" not in seen["body"]


def test_probe_refuses_a_server_that_does_not_serve_bid_high():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = [{"id": 1, "certifier": "none"}]
    with patch("fmv_runner.requests.get", MagicMock(return_value=resp)):
        assert fmv_runner._probe_certifier_support("http://s") is False
    resp.json.return_value = [{"id": 1, "certifier": "none", "fmv_bid_high": None}]
    with patch("fmv_runner.requests.get", MagicMock(return_value=resp)):
        assert fmv_runner._probe_certifier_support("http://s") is True


@pytest.mark.parametrize("h", ["GRADED_POINT_BAND_HALF_WIDTH",
                               "GRADED_DIRECT_BAND_HALF_WIDTH_FLOOR"])
def test_ship_constants_are_the_audit_pick(h):
    assert getattr(fm, h) == 0.15
    assert fm.SMALL_POOL_MAX_RATIO == 3.0  # BUI-179 money guard, kept
