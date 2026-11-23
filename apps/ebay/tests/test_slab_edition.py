"""BUI-1436: comic_identity.slab_edition_reason parity, rate, and rule 8 tests.

The fixture (fixtures/slab_edition_rows.json) is a trimmed copy of the
2026-11-20 edition-rules tune and test sets. `expected` is the output of the
frozen reference classifier (rules_final.classify); absent keys mean None.
"""

import json
from pathlib import Path

import pytest

import comic_identity as ci

_FIXTURE = Path(__file__).parent / "fixtures" / "slab_edition_rows.json"
ROWS = json.loads(_FIXTURE.read_text())


def _specifics(row):
    py = row.get("publication_year")
    return {"Publication Year": py} if py else None


def _reason(row, **kw):
    return ci.slab_edition_reason(row["title"], row["target"], _specifics(row), **kw)


def _score(rows):
    """Scored like the reference score.py: skip unsure/missing labels and
    signature reasons (correct by the labelling rule) in both counts."""
    caught = missed = false_drop = book = 0
    for r in rows:
        lab = r.get("label")
        if lab not in ("book", "not"):
            continue
        v = _reason(r)
        if v and v.startswith("signature:"):
            continue
        if lab == "book":
            book += 1
            false_drop += bool(v)
        else:
            caught += bool(v)
            missed += not v
    return caught, caught + missed, false_drop, book


def test_fixture_size():
    assert len(ROWS) == 2291


def test_parity_with_reference_rules():
    bad = [(r["item_id"], r.get("expected"), _reason(r)) for r in ROWS if _reason(r) != r.get("expected")]
    assert bad == []


def test_tune_rates():
    caught, nots, false_drop, books = _score([r for r in ROWS if r["set"] == "tune"])
    assert (caught, nots) == (45, 47)
    # 1006 labelled books in the set; 3 are signature drops, excluded from both counts.
    assert (false_drop, books) == (4, 1003)
    assert caught / nots >= 0.955
    assert false_drop / books <= 0.005


def test_test_set_scores_are_stable():
    caught, nots, false_drop, books = _score([r for r in ROWS if r["set"] == "test"])
    assert nots > 0 and books > 0
    assert caught <= nots and false_drop <= books


# ─── Rule order and reason strings ─────────────────────────────────────────

T = {"title": "Amazing Spider-Man", "issue": "300", "year": 1988}


def test_clean_listing_passes():
    assert ci.slab_edition_reason("Amazing Spider-Man #300 CGC 9.8", T) is None


def test_adjacency_first():
    assert ci.slab_edition_reason("Hulk #300 CGC 9.8 variant", T) == "series absent"


def test_signature_reason_prefix():
    r = ci.slab_edition_reason("Amazing Spider-Man #300 CGC 9.8 signed", T)
    assert r.startswith("signature:")


def test_marker_reason_prefix():
    r = ci.slab_edition_reason("Amazing Spider-Man #300 CGC 9.8 reprint", T)
    assert r == "marker:reprint"


def test_mmyy_and_pubyear():
    assert ci.slab_edition_reason("Amazing Spider-Man #300 CGC 9.8 05/95", T) == "mmyy 05/95"
    assert (
        ci.slab_edition_reason("Amazing Spider-Man #300 CGC 9.8", T, {"Publication Year": "2019"})
        == "pubyear 2019"
    )


# ─── Rule 8: vintage price floor ───────────────────────────────────────────

V = {"title": "Amazing Spider-Man", "issue": "129", "year": 1974}
VT = "Amazing Spider-Man #129 CGC 9.8"


def _v(**kw):
    base = {"grade": 9.8, "price": 120, "price_floor": 900}
    base.update(kw)
    return ci.slab_edition_reason(VT, V, **base)


def test_rule8_fires_with_reason():
    assert _v() == "vintage_price: 9.8 at $120 under $900 floor"


def test_rule8_off_without_floor():
    assert _v(price_floor=None) is None


@pytest.mark.parametrize("missing", ["grade", "price"])
def test_rule8_off_when_grade_or_price_missing(missing):
    assert _v(**{missing: None}) is None


def test_rule8_off_when_year_missing():
    assert ci.slab_edition_reason(VT, {**V, "year": None}, grade=9.8, price=120, price_floor=900) is None


def test_rule8_price_boundary():
    assert _v(price=900) is None
    assert _v(price=899.99) is not None


def test_rule8_grade_boundary():
    assert _v(grade=9.6) == "vintage_price: 9.6 at $120 under $900 floor"
    assert _v(grade=9.4) is None


def test_rule8_year_boundary():
    assert ci.slab_edition_reason(VT, {**V, "year": 1974}, grade=9.8, price=120, price_floor=900) is not None
    assert ci.slab_edition_reason(VT, {**V, "year": 1975}, grade=9.8, price=120, price_floor=900) is None


def test_rule8_runs_last():
    r = ci.slab_edition_reason("Amazing Spider-Man #129 CGC 9.8 reprint", V, grade=9.8, price=120, price_floor=900)
    assert r == "marker:reprint"
