"""Tests for slab_deals.py (BUI-1188): ladder build, listing exclusion filters,
ranking, and the shortlist/comic-fmv batch. Real grade_tokens/comic_identity
parsers; the comps ledger payload and Browse items are fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import slab_deals as sd


def comp(grade, price, *, certifier="cgc", label="universal", pool="slab"):
    return {"pool": pool, "certifier": certifier, "label": label,
            "grade": grade, "price": price}


def item(title, price=100.0, *, kind="BIN", item_id="1"):
    return {"item_id": item_id, "title": title, "listing_type": kind,
            "current_price": f"${price:.2f}", "end_date_iso": None,
            "seller": "s", "listing_url": "u"}


def classify(title, **kw):
    return sd.classify_listing(item(title, **kw), title="Amazing Spider-Man",
                               issue="50", year=1967)


class TestBuildLadder:
    ROWS = [
        comp(6.5, 1200), comp(6.5, 1400), comp(6.5, 1300),
        comp(4.5, 700), comp(4.5, 720),
        comp(6.5, 9999, label="signature_series"),
        comp(6.5, 9999, certifier="cbcs"),
        comp(6.5, 9999, pool="raw"),
        comp(None, 500), comp(5.0, None),
    ]

    def test_median_count_and_order(self):
        ladder = sd.build_ladder(self.ROWS)
        assert [r["grade"] for r in ladder] == [6.5, 4.5]
        assert ladder[0] == {"grade": 6.5, "n": 3, "median": 1300.0,
                             "low": 1200.0, "high": 1400.0}
        assert ladder[1]["n"] == 2 and ladder[1]["median"] == 710.0

    def test_empty(self):
        assert sd.build_ladder([]) == []


class TestClassifyListing:
    def test_like_for_like_kept(self):
        cand, reason = classify("Amazing Spider-Man #50 CGC 6.5 OW/W 1967 1st Kingpin")
        assert reason is None
        assert cand["grade"] == 6.5 and cand["price"] == 100.0
        assert cand["url"] == "https://www.ebay.com/itm/1"

    @pytest.mark.parametrize("title,reason", [
        ("Amazing Spider-Man #50 CGC 6.5 Signature Series Stan Lee", "label_signature_series"),
        ("Amazing Spider-Man #50 CGC 6.5 Qualified", "label_qualified"),
        ("Amazing Spider-Man #50 CGC 6.5 Restored", "label_restored"),
        ("Amazing Spider-Man #50 1967 Marvel Raw VF", "not_cgc"),
        ("Amazing Spider-Man #50 1967 CGC graded nice", "no_grade"),
        ("Amazing Spider-Man #50 CBCS 6.5 1967", "not_cgc"),
    ])
    def test_label_and_certifier_drops(self, title, reason):
        assert classify(title)[1] == reason

    @pytest.mark.parametrize("title", [
        "Williams-Verlag 1976 German Amazing Spider-Man #50 CGC 9.0",
        "Amazing Spider-Man 50, CGC 6.0, RARE 1968 SWEDISH Foreign Ed",
        "Amazing Spider-Man #50 Capullo Variant Marvel Comics 2024 CGC 9.8",
        "Amazing Spider-Man #50, 2024, CGC 9.8",
        "Amazing Spider-man Vol. 2 #50 CGC 9.6",
        "Amazing Spider-Man #49 CGC 8.5 1967",
        "Fantastic Four 50 CGC 8.5 WP 1966 Marvel",
        "Amazing Spider-Man #49 (850) CGC 8.5 1:50 Variant Edition",
        "Amazing Spider-man 1 2 3 4 5 6 7 8 9 10 11 12-50 All CGC 4.0",
        "Amazing Spider-Man #50 CGC 9.2 Sony Pictures Edition",
    ])
    def test_wrong_book_dropped(self, title):
        assert classify(title)[1] == "rejected"

    def test_series_year_in_paren_is_kept(self):
        assert classify("Amazing Spider-Man (1963) # 50 CGC 5.0 VG/FN")[1] is None

    def test_non_usd_price_dropped(self):
        it = item("Amazing Spider-Man #50 CGC 6.5 1967")
        it["current_price"] = "GBP 500.00"
        assert sd.classify_listing(it, title="Amazing Spider-Man", issue="50",
                                   year=1967)[1] == "no_usd_price"


class TestScreenListings:
    def test_dedupes_and_counts_drops(self):
        items = [
            item("Amazing Spider-Man #50 CGC 6.5 1967", item_id="a"),
            item("Amazing Spider-Man #50 CGC 6.5 1967", item_id="a"),
            item("Amazing Spider-Man #50 Raw 1967", item_id="b"),
        ]
        kept, dropped = sd.screen_listings(items, title="Amazing Spider-Man",
                                           issue="50", year=1967)
        assert [k["item_id"] for k in kept] == ["a"]
        assert dropped == {"duplicate": 1, "not_cgc": 1}


class TestRanking:
    LADDER = sd.build_ladder([comp(6.5, 1300), comp(6.5, 1300), comp(4.5, 700)])

    def listing(self, grade, price, kind="BIN", item_id=None):
        return {"item_id": item_id or f"{grade}-{price}", "grade": grade,
                "price": price, "listing_type": kind, "page_quality": "ow_w"}

    def test_ranked_by_ratio_ascending_unmatched_last(self):
        ranked = sd.rank_listings([
            self.listing(6.5, 1560.0), self.listing(4.5, 350.0),
            self.listing(9.4, 20000.0), self.listing(6.5, 1300.0)],
            self.LADDER)
        assert [r["ratio"] for r in ranked] == [0.5, 1.0, 1.2, None]
        assert ranked[0]["median"] == 700.0 and ranked[0]["n"] == 1
        assert ranked[-1]["n"] == 0

    def test_shortlist_skips_unmatched_and_caps(self):
        ranked = sd.rank_listings([
            self.listing(9.4, 1.0), self.listing(6.5, 100.0),
            self.listing(6.5, 200.0), self.listing(6.5, 300.0),
            self.listing(6.5, 400.0)], self.LADDER)
        short = sd.pick_shortlist(ranked, 3)
        assert [s["price"] for s in short] == [100.0, 200.0, 300.0]
        assert sd.pick_shortlist(ranked, 0) == []

    def test_fmv_batch_graded_identity(self):
        short = [self.listing(6.5, 100.0, kind="Auction", item_id="x")]
        rows = sd.fmv_batch(short, title="Amazing Spider-Man", issue="50",
                            year=1967, publisher="marvel")
        assert rows == [{
            "item_id": "x", "title": "Amazing Spider-Man", "issue": "50",
            "year": 1967, "grade": 6.5, "locg_id": None, "publisher": "marvel",
            "certifier": "cgc", "label": "universal", "page_quality": "ow_w",
            "listing_type": "Auction"}]


def test_render_has_ladder_and_ranked_rows():
    ladder = sd.build_ladder([comp(6.5, 1300)])
    ranked = sd.rank_listings([{
        "item_id": "a", "grade": 6.5, "price": 650.0, "listing_type": "BIN",
        "page_quality": "unknown", "seller": "sel", "end_date_iso": None,
        "url": "https://www.ebay.com/itm/a"}], ladder)
    text = sd.render("ASM #50", ladder, ranked, {}, ranked)
    assert "6.5    1" in text and "50%" in text and "itm/a" in text
