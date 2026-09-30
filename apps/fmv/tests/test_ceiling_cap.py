"""BUI-1028: the sold-comp ceiling cap for raw one_sided/too_wide refusals.

The rule (measured in docs/audit/2026-10-01-sold-comp-ceiling-oos.md): on a
refusal with at least two comps at or above the target grade, cap the bid at
0.60 x the median price of the lowest grade rung of that at-or-above pool,
rounded DOWN to the clean step. Behind FMV_CEILING_CAP, default off.
"""

import copy
import json
from unittest.mock import patch

import pytest

import fmv_math
import fmv_runner


def _c(price, grade, pid):
    return {"product_id": pid, "title": f"comic {pid}", "price": price,
            "grade": grade, "sold_date": "", "buying_format": ""}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("FMV_CEILING_CAP", raising=False)
    monkeypatch.setattr(fmv_runner, "_fetch_first_party_outcomes",
                        lambda *a, **k: [])
    monkeypatch.setattr(fmv_runner, "_post_comps", lambda *a, **k: None)


# Target 6.0; every comp sits above it => one_sided. Lowest rung is 7.0,
# prices 100 and 120 => ceiling $110 => 0.60 x 110 = 66 => floors to $60.
# `clean_round` would give $70, ABOVE the 66 the arithmetic produced.
ONE_SIDED_POOL = [_c(100, 7.0, "a"), _c(120, 7.0, "b"), _c(200, 8.0, "c")]
TARGET = 6.0


def _refused(comps, target=TARGET):
    return fmv_math.compute_fmv(comps, target_grade=target)


# ─── floor_clean ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (66.0, 60),      # step 10 at 50-200
    (14.4, 10),      # step 5 below 50 (clean_round would say 15)
    (49.99, 45),
    (52.0, 50),
    (204.0, 200),    # step 25 from 200
    (224.99, 200),
    (4.99, 0),       # under one step: zero, which callers read as no cap
    (0.0, 0),
    (-3.0, 0),
])
def test_floor_clean_never_rounds_up(value, expected):
    assert fmv_math.floor_clean(value) == expected
    assert fmv_math.floor_clean(value) <= max(value, 0)


def test_floor_clean_beats_clean_round_where_it_rounds_up():
    assert fmv_math.clean_round(66.0) == 70 > 66.0
    assert fmv_math.floor_clean(66.0) == 60


# ─── ceiling_cap: the known pool, exact floored cap ──────────────────────────

def test_known_one_sided_pool_caps_at_the_exact_floored_value():
    fmv = _refused(ONE_SIDED_POOL)
    assert fmv["flag_reason"] == "one_sided"
    out = fmv_math.ceiling_cap(ONE_SIDED_POOL, TARGET, fmv["flag_reason"])
    assert out["reason"] == "capped"
    assert out["ceiling"] == 110
    assert out["rung_grade"] == 7.0 and out["rung_n"] == 2 and out["n_above"] == 3
    assert out["cap"] == 60
    assert out["cap"] <= fmv_math.CEILING_BID_FACTOR * out["ceiling"]


def test_too_wide_pool_uses_the_lowest_rung_at_or_above_the_target():
    comps = [_c(10, 4.0, "a"), _c(100, 6.0, "b"), _c(120, 6.0, "c"),
             _c(200, 7.0, "d")]
    fmv = _refused(comps)
    assert fmv["flag_reason"] == "too_wide"
    out = fmv_math.ceiling_cap(comps, TARGET, fmv["flag_reason"])
    # The 4.0 comp is below the target and never sets the ceiling.
    assert (out["reason"], out["rung_grade"], out["ceiling"], out["cap"]) == (
        "capped", 6.0, 110, 60)


def test_one_comp_at_or_above_gets_no_cap():
    comps = [_c(10, 4.0, "a"), _c(12, 4.0, "b"), _c(100, 7.0, "c")]
    fmv = _refused(comps)
    out = fmv_math.ceiling_cap(comps, TARGET, fmv["flag_reason"])
    assert out["cap"] is None and out["reason"] == "one_comp_above"


def test_a_pool_entirely_below_the_target_gets_no_cap():
    comps = [_c(50, 4.0, "a"), _c(60, 4.0, "b"), _c(70, 4.5, "c")]
    fmv = _refused(comps)
    assert fmv["flag_reason"] == "one_sided"
    out = fmv_math.ceiling_cap(comps, TARGET, fmv["flag_reason"])
    assert out["cap"] is None and out["reason"] == "one_sided_below"


def test_a_cap_that_floors_to_zero_is_no_cap():
    comps = [_c(5, 7.0, "a"), _c(5, 7.0, "b"), _c(6, 7.5, "c")]
    out = fmv_math.ceiling_cap(comps, TARGET, "one_sided")
    assert out["cap"] is None and out["reason"] == "zero_cap"
    assert out["ceiling"] == 5  # 0.60 x 5 = 3 -> floors to 0


@pytest.mark.parametrize("flag", [None, "too_sparse", "variant_dropped"])
def test_only_shape_refusals_qualify(flag):
    out = fmv_math.ceiling_cap(ONE_SIDED_POOL, TARGET, flag)
    assert out["cap"] is None and out["reason"] == "not_shape_refusal"


def test_the_cap_never_exceeds_the_ceiling_or_goes_negative():
    for ceiling in (6, 9, 12, 26, 49, 51, 83, 199, 203, 640, 5001):
        comps = [_c(ceiling, 7.0, "a"), _c(ceiling, 7.0, "b")]
        out = fmv_math.ceiling_cap(comps, TARGET, "one_sided")
        if out["cap"] is not None:
            assert 0 < out["cap"] <= fmv_math.CEILING_BID_FACTOR * ceiling


# ─── Runner: flag off is byte-identical ──────────────────────────────────────

def _result(comps=None, grade=TARGET, year=1970):
    return {"input": {"title": "X", "issue": "1", "year": year, "grade": grade},
            "comps": comps if comps is not None else ONE_SIDED_POOL}


def _compute(monkeypatch, comps=None, **kw):
    bodies = []

    def fake_upsert(server_url, inp, fmv, hard_fail=True):
        bodies.append(copy.deepcopy(fmv))
        return {"id": 7, "fmv_id": 70}

    with patch("fmv_runner._upsert_fmv", fake_upsert):
        out = fmv_runner._compute_and_upsert_one(
            _result(comps), {"title": "X", "issue": "1", "grade": TARGET},
            server_url="http://s", **kw)
    return out, bodies


def test_flag_off_adds_no_key_and_apply_is_a_no_op(monkeypatch):
    out, bodies = _compute(monkeypatch)
    assert "ceiling" not in out["fmv"]
    before = copy.deepcopy(out)
    calls = []
    with patch("fmv_runner._upsert_fmv", lambda *a, **k: calls.append(1)):
        fmv_runner._apply_ceiling_cap({0: out}, server_url="http://s")
    assert calls == []
    assert out == before
    assert out["fmv"]["flag_reason"] == "one_sided"
    assert out["fmv"]["max_bid"] is None
    assert "ceiling" not in fmv_runner._build_notes(out["fmv"])
    assert "refused_reason" not in fmv_runner._brief_row(out)


@pytest.mark.parametrize("raw", ["", "0", "no", "off", "false", "2", "ceiling"])
def test_flag_values_that_are_not_on_stay_off(monkeypatch, raw):
    monkeypatch.setenv("FMV_CEILING_CAP", raw)
    assert fmv_runner._ceiling_cap_enabled() is False
    out, _ = _compute(monkeypatch)
    assert "ceiling" not in out["fmv"]


def test_flag_is_read_per_call_not_cached(monkeypatch):
    assert fmv_runner._ceiling_cap_enabled() is False
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    assert fmv_runner._ceiling_cap_enabled() is True
    monkeypatch.delenv("FMV_CEILING_CAP")
    assert fmv_runner._ceiling_cap_enabled() is False


# ─── Runner: flag on ─────────────────────────────────────────────────────────

def _apply(monkeypatch, out):
    posted = []

    def fake_upsert(server_url, inp, fmv, hard_fail=True):
        posted.append(copy.deepcopy(fmv))
        return {"id": 7, "fmv_id": 71}

    with patch("fmv_runner._upsert_fmv", fake_upsert):
        fmv_runner._apply_ceiling_cap({0: out}, server_url="http://s")
    return posted


def test_flag_on_caps_a_refused_row_and_keeps_it_refused(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    out, _ = _compute(monkeypatch)
    assert out["fmv"]["flag_reason"] == "one_sided"  # not capped yet
    posted = _apply(monkeypatch, out)
    assert len(posted) == 1

    fmv = out["fmv"]
    assert fmv["pricing_basis"] == "ceiling"
    assert fmv["max_bid"] == 60 and fmv["ceiling_cap"] == 60
    # STILL refused, and no price: nothing that reads low/high as a fair value
    # can mistake the cap for one.
    assert fmv["flag_reason"] == "one_sided"
    assert fmv["fmv_low"] is None and fmv["fmv_high"] is None
    assert out["fmv_id"] == 71
    notes = fmv_runner._build_notes(fmv)
    assert "manual_review=one_sided" in notes
    assert "ceiling_cap=$60" in notes and "a cap, not an FMV" in notes
    assert "interpolated=" not in notes and "CGC proxy" not in notes


def test_the_posted_body_is_flagged_with_the_cap_in_its_own_field(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    out, _ = _compute(monkeypatch)
    bodies = []

    def fake_post(url, body, what, hard_fail=True):
        bodies.append(body)
        return {"id": 7, "fmv_id": 71}

    with patch("fmv_runner._post_json", fake_post):
        fmv_runner._apply_ceiling_cap({0: out}, server_url="http://s")
    (body,) = bodies
    assert body["fmv_low"] is None and body["fmv_high"] is None
    assert body["fmv_flag_reason"] == "one_sided"
    assert body["pricing_basis"] == "ceiling"
    assert body["fmv_ceiling_cap"] == 60
    assert "certifier" not in body  # still the raw row


def test_the_ordinary_refused_body_is_unchanged_by_this_change(monkeypatch):
    """Flag off: the refused row's POST body has none of the new keys."""
    out, _ = _compute(monkeypatch)
    bodies = []
    with patch("fmv_runner._post_json",
               lambda url, body, what, hard_fail=True: bodies.append(body)
               or {"id": 7}):
        fmv_runner._upsert_fmv("http://s", out["input"], out["fmv"])
    (body,) = bodies
    assert "fmv_ceiling_cap" not in body and "pricing_basis" not in body
    assert body["fmv_flag_reason"] == "one_sided"


def test_a_failed_write_leaves_the_row_refused(monkeypatch, capsys):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    out, _ = _compute(monkeypatch)
    before = copy.deepcopy(out)
    with patch("fmv_runner._upsert_fmv", lambda *a, **k: None):
        fmv_runner._apply_ceiling_cap({0: out}, server_url="http://s")
    assert out == before
    assert out["fmv"]["max_bid"] is None
    assert "left refused (one_sided)" in capsys.readouterr().err


def test_a_row_the_rescue_priced_is_never_capped(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    out, _ = _compute(monkeypatch)
    out["fmv"].update(fmv_low=500, fmv_high=600, max_bid=420, flag_reason=None,
                      cgc_proxy=True)
    out["source"] = "cgc-proxy"
    before = copy.deepcopy(out)
    assert _apply(monkeypatch, out) == []
    assert out == before


def test_variant_dropped_books_never_get_a_ceiling(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    result = _result()
    result["variant_dropped"] = "Newsstand"
    with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
        out = fmv_runner._compute_and_upsert_one(
            result, {"title": "X", "issue": "1", "grade": TARGET},
            server_url="http://s")
    assert "ceiling" not in out["fmv"]


def test_a_one_comp_pool_stays_refused_with_the_flag_on(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    comps = [_c(10, 4.0, "a"), _c(12, 4.0, "b"), _c(100, 7.0, "c")]
    out, _ = _compute(monkeypatch, comps)
    assert out["fmv"]["ceiling"]["reason"] == "one_comp_above"
    assert _apply(monkeypatch, out) == []
    assert out["fmv"]["flag_reason"] is not None and out["fmv"]["max_bid"] is None


def test_a_zero_cap_keeps_the_row_refused_and_emits_no_bid(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    comps = [_c(5, 7.0, "a"), _c(5, 7.0, "b"), _c(6, 7.5, "c")]
    out, _ = _compute(monkeypatch, comps)
    assert out["fmv"]["ceiling"]["reason"] == "zero_cap"
    assert _apply(monkeypatch, out) == []
    assert out["fmv"]["max_bid"] is None


def test_the_graded_path_never_carries_a_ceiling(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    row = {"input": {"grade": 4.5}, "source": "fresh",
           "fmv": {"graded": True, "flag_reason": "one_sided", "fmv_high": None,
                   "ceiling": {"reason": "capped", "cap": 60}}}
    assert fmv_runner._ceiling_applicable(row) is False


# ─── Rendering (BUI-928 class: a render test on the new row shape) ───────────

def _capped_row(monkeypatch):
    monkeypatch.setenv("FMV_CEILING_CAP", "1")
    out, _ = _compute(monkeypatch)
    _apply(monkeypatch, out)
    out["input"] = {**out["input"], "item_id": "123"}
    return out


def test_print_table_renders_a_ceiling_row_as_a_refusal_with_a_cap(monkeypatch, capsys):
    row = _capped_row(monkeypatch)
    fmv_runner._print_table([row])
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if "X #1" in ln)
    assert "manual:one_sided" in line
    assert "one_sided, ceiling cap $60" in line   # provenance cell
    assert "$60 cap" in line                      # max-bid cell
    assert "$60–$60" not in line                  # never rendered as a range


def test_print_table_renders_an_ordinary_refusal_unchanged(monkeypatch, capsys):
    out, _ = _compute(monkeypatch)
    fmv_runner._print_table([out])
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if "X #1" in ln)
    assert "manual:one_sided" in line
    assert "cap" not in line


def test_brief_line_carries_the_cap_the_basis_and_the_refusal(monkeypatch):
    row = _capped_row(monkeypatch)
    brief = json.loads(json.dumps(fmv_runner._brief_row(row)))
    assert brief["max_bid"] == 60
    assert brief["pricing_basis"] == "ceiling"
    assert brief["flag_reason"] == "one_sided"
    assert brief["fmv_low"] is None and brief["fmv_high"] is None
    assert brief["provenance"] == "one_sided, ceiling cap $60"
    assert "ceiling_cap=$60" in brief["fmv_notes"]


def test_a_refused_row_with_a_stored_cap_is_not_a_price_to_protect():
    """A flagged row has a null fmv_low, so the BUI-1029 demote already passes
    over it — asserted, not assumed."""
    fmv = {"n": 0, "flag_reason": "too_sparse"}
    with patch("fmv_runner._hand_price_candidates", lambda *a, **k: [
            {"fmv_low": None, "fmv_high": None, "pricing_basis": "ceiling",
             "fmv_ceiling_cap": 60.0}]):
        fmv_runner._demote_empty_pool_flag_on_priced_row(
            "http://s", {"grade": 6.0}, fmv)
    assert fmv["flag_reason"] == "too_sparse"
