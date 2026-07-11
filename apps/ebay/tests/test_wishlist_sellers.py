"""Tests for wishlist_sellers.py — the multi-seller wish-list scan funnel."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import sqlite3

import pytest

import wishlist_sellers as ws


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

def make_match(
    seller: str = "sellerA",
    item_id: str = "111",
    wish_name: str = "Amazing Spider-Man #129",
    title: str = "Amazing Spider-Man #129 VF",
    price: str = "$50.00",
    series: str = "Amazing Spider-Man",
    issue: str = "129",
    score: float = 0.9,
    end_date: str = "2026-07-01 12:00",
    listing_url: str = "https://www.ebay.com/itm/111",
) -> dict:
    """Build a minimal match dict matching the pipeline's internal shape."""
    return {
        "seller": seller,
        "item_id": item_id,
        "title": title,
        "wish_name": wish_name,
        "price": price,
        "end_date": end_date,
        "end_date_iso": "2026-07-01T12:00:00Z",
        "listing_url": listing_url,
        "score": score,
        "_series": series,
        "_issue": issue,
    }


def _two_wish_items() -> tuple[list, list]:
    """Return (wish_list, wish_items) for ASM #129 + X-Men #94."""
    wish_list = [
        {"id": "w1", "name": "Amazing Spider-Man #129"},
        {"id": "w2", "name": "X-Men #94"},
    ]
    wish_items = [
        {
            "id": "w1",
            "name": "Amazing Spider-Man #129",
            "series": "Amazing Spider-Man",
            "issue": "129",
            "_tokens": ["amazing", "spider", "man"],
        },
        {
            "id": "w2",
            "name": "X-Men #94",
            "series": "X-Men",
            "issue": "94",
            "_tokens": ["xmen"],
        },
    ]
    return wish_list, wish_items


def _run_main(
    matches_by_wish: list[list],
    *,
    db_path: Path,
    wish_list: list | None = None,
    wish_items: list | None = None,
    seen: set | None = None,
    server_base: str = "",
    verify_return: list | None = None,
    dropped_return: list | None = None,
    json_output: bool = False,
    extra_args: list | None = None,
) -> tuple[str, int, MagicMock, MagicMock]:
    """Run main() with standard mocks; return (stdout, exit_code, mock_verify,
    mock_record).

    BUI-297/BUI-298: verify_with_claude returns (kept, dropped, filtered).
    `verify_return` is the KEPT list; `dropped_return` (default []) is the
    never-verified list; the third element (model-rejected `filtered`, which
    wishlist-sellers ignores) is always [] here.

    BUI-309: `exit_code` is main()'s return value — non-zero (_EXIT_INCOMPLETE)
    when `dropped_return` produced any never-verified candidates in the final
    survivors set, 0 on a clean run.
    """
    if wish_list is None or wish_items is None:
        wish_list, wish_items = _two_wish_items()
    if seen is None:
        seen = set()
    if verify_return is None:
        # By default verify passes everything through
        verify_return = [m for ms in matches_by_wish for m in ms]
    if dropped_return is None:
        dropped_return = []

    mock_verify = MagicMock(return_value=(verify_return, dropped_return, []))
    mock_record = MagicMock()

    with (
        patch.object(ws, "fetch_wish_list", return_value=wish_list),
        patch.object(ws, "prepare_wish_items", return_value=wish_items),
        patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
        patch("wishlist_sellers.get_token", return_value="tok"),
        patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
        patch("wishlist_sellers.search_by_keyword", return_value=[]),
        patch("wishlist_sellers.ebay_search_cache.put"),
        patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
        patch.object(ws, "match_results_for_wish", side_effect=list(matches_by_wish)),
        patch.object(ws, "get_item_aspects", return_value=None),
        patch.object(ws, "fetch_seen_item_ids", return_value=seen),
        patch.object(ws, "_server_base", return_value=server_base),
        patch.object(ws, "verdict_db_path", return_value=db_path),
        patch.object(ws, "verify_with_claude", mock_verify),
        patch.object(ws, "record_items_seen", mock_record),
    ):
        buf = io.StringIO()
        argv = ["--json"] if json_output else []
        if extra_args:
            argv = argv + extra_args
        with redirect_stdout(buf):
            exit_code = ws.main(argv)
        return buf.getvalue(), exit_code, mock_verify, mock_record


# ─── dedup_matches ────────────────────────────────────────────────────────────

class TestDedupMatches:
    def test_same_seller_item_id_collapsed_to_one(self):
        """Same (seller, item_id) from two wish searches → single entry."""
        m1 = make_match(seller="A", item_id="1", wish_name="ASM #129", score=0.8)
        m2 = make_match(seller="A", item_id="1", wish_name="Spider-Man #129", score=0.9)
        result = ws.dedup_matches([m1, m2])
        assert len(result) == 1
        # Higher score is kept
        assert result[0]["score"] == 0.9

    def test_variant_copies_of_same_book_collapse(self):
        """Two different listings (variant covers) of the SAME wish book from one
        seller collapse to one representative — the cheapest."""
        m1 = make_match(seller="A", item_id="1", wish_name="Aliens vs Avengers #1",
                        price="$15.00")
        m2 = make_match(seller="A", item_id="2", wish_name="Aliens vs Avengers #1",
                        price="$12.00")
        result = ws.dedup_matches([m1, m2])
        assert len(result) == 1
        assert result[0]["item_id"] == "2"  # cheapest representative kept

    def test_different_books_same_seller_both_kept(self):
        """Two DIFFERENT wish books from one seller are both kept (combine-ship)."""
        m1 = make_match(seller="A", item_id="1", wish_name="ASM #129")
        m2 = make_match(seller="A", item_id="2", wish_name="X-Men #94")
        assert len(ws.dedup_matches([m1, m2])) == 2

    def test_different_sellers_same_book_both_kept(self):
        """Same book from two sellers counts separately (per-seller dedup)."""
        m1 = make_match(seller="A", item_id="1", wish_name="ASM #129")
        m2 = make_match(seller="B", item_id="1", wish_name="ASM #129")
        assert len(ws.dedup_matches([m1, m2])) == 2

    def test_empty_input_returns_empty(self):
        assert ws.dedup_matches([]) == []


# ─── group_and_gate ───────────────────────────────────────────────────────────

class TestGroupAndGate:
    def test_single_match_seller_dropped(self):
        """Seller with exactly 1 match is dropped by the ≥2 gate."""
        m = make_match(seller="lone_wolf")
        assert "lone_wolf" not in ws.group_and_gate([m])

    def test_two_match_seller_kept(self):
        m1 = make_match(seller="A", item_id="1", wish_name="ASM #129")
        m2 = make_match(seller="A", item_id="2", wish_name="X-Men #94")
        grouped = ws.group_and_gate([m1, m2])
        assert "A" in grouped
        assert len(grouped["A"]) == 2

    def test_mixed_sellers_correct_split(self):
        """Two-match seller kept; one-match seller dropped."""
        m1 = make_match(seller="keeper", item_id="1")
        m2 = make_match(seller="keeper", item_id="2")
        m3 = make_match(seller="dropper", item_id="3")
        grouped = ws.group_and_gate([m1, m2, m3])
        assert "keeper" in grouped
        assert "dropper" not in grouped

    def test_empty_input_returns_empty_dict(self):
        assert ws.group_and_gate([]) == {}

    def test_none_seller_dropped_before_grouping(self):
        """match_results_for_wish drops items with seller=None so they never reach
        group_and_gate and cannot form a bogus ≥2 group under '_unknown_'."""
        wish_item = {
            "id": "w1",
            "name": "ASM #129",
            "series": "Amazing Spider-Man",
            "issue": "129",
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": None,
            "_release_year": None,
        }
        result_no_seller = {
            "title": "Amazing Spider-Man #129 VF",
            "item_id": "999",
            "seller": None,  # ← the problematic case
            "current_price": "$50.00",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/999",
        }
        with (
            # BUI-245: hard_reject/era_mismatch/reprint/digital/trading-card
            # are now one shared gate — patch the single entry point.
            patch.object(ws, "should_reject", return_value=False),
            patch.object(ws, "match_listing", return_value=(wish_item, 1.0)),
        ):
            matches = ws.match_results_for_wish([result_no_seller], wish_item)
        assert matches == [], "None-seller item must be dropped before the matches list"


# ─── batch_check_owned ────────────────────────────────────────────────────────

class TestBatchCheckOwned:
    def test_in_collection_returned_as_owned(self):
        payload = {"count": 1, "results": [
            {"series": "X-Men", "issue": "94", "match_status": "in_collection",
             "full_title_matched": True, "cache_age_days": 1},
        ]}
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=payload),
            )
            owned = ws.batch_check_owned([("X-Men", "94")], "http://server")
        assert ("X-Men", "94") in owned

    def test_not_in_cache_not_returned(self):
        payload = {"count": 1, "results": [
            {"series": "X-Men", "issue": "94", "match_status": "not_in_cache",
             "full_title_matched": False, "cache_age_days": 0},
        ]}
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=payload),
            )
            owned = ws.batch_check_owned([("X-Men", "94")], "http://server")
        assert owned == set()

    def test_ambiguous_cross_volume_returned_as_owned(self):
        """BUI-302: ambiguous_cross_volume (BUI-284) is owned under >1 volume
        with no year to disambiguate — must be skipped, not surfaced as a buy
        candidate."""
        payload = {"count": 1, "results": [
            {"series": "X-Men", "issue": "94", "match_status": "ambiguous_cross_volume",
             "full_title_matched": True, "cache_age_days": 1},
        ]}
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=payload),
            )
            owned = ws.batch_check_owned([("X-Men", "94")], "http://server")
        assert ("X-Men", "94") in owned

    def test_409_causes_sys_exit(self):
        """409 from server = collection not imported → hard-fail (plan R10/R11)."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=409,
                text="collection not imported",
            )
            with pytest.raises(SystemExit):
                ws.batch_check_owned([("X-Men", "94")], "http://server")

    def test_empty_pairs_makes_no_request(self):
        with patch("requests.post") as mock_post:
            result = ws.batch_check_owned([], "http://server")
        mock_post.assert_not_called()
        assert result == set()

    def test_network_error_causes_sys_exit(self):
        import requests as req
        with patch("requests.post", side_effect=req.exceptions.ConnectionError("down")):
            with pytest.raises(SystemExit):
                ws.batch_check_owned([("X-Men", "94")], "http://server")


# ─── Verdict cache unit tests ─────────────────────────────────────────────────

class TestVerdictCache:
    @pytest.fixture(autouse=True)
    def _db(self, tmp_path):
        self.db_path = tmp_path / "verdicts.db"

    def test_get_returns_none_for_unknown(self):
        assert ws.verdict_get("999", "ASM #129", db_path=self.db_path) is None

    def test_put_then_get_genuine(self):
        ws.verdict_put("111", "ASM #129", True, db_path=self.db_path)
        assert ws.verdict_get("111", "ASM #129", db_path=self.db_path) is True

    def test_put_then_get_false(self):
        ws.verdict_put("222", "ASM #129", False, db_path=self.db_path)
        assert ws.verdict_get("222", "ASM #129", db_path=self.db_path) is False

    def test_compound_key_same_title_key_different_wish(self):
        """Same title_key can have distinct verdicts for different wish items."""
        ws.verdict_put("333", "ASM #129", True, db_path=self.db_path)
        ws.verdict_put("333", "X-Men #94", False, db_path=self.db_path)
        assert ws.verdict_get("333", "ASM #129", db_path=self.db_path) is True
        assert ws.verdict_get("333", "X-Men #94", db_path=self.db_path) is False

    def test_put_overwrites_previous_verdict(self):
        ws.verdict_put("444", "ASM #129", True, db_path=self.db_path)
        ws.verdict_put("444", "ASM #129", False, db_path=self.db_path)
        assert ws.verdict_get("444", "ASM #129", db_path=self.db_path) is False

    def test_get_returns_none_when_db_absent(self, tmp_path):
        missing = tmp_path / "nonexistent.db"
        assert ws.verdict_get("x", "y", db_path=missing) is None

    def test_stale_schema_auto_healed(self, tmp_path):
        """A DB with the old listing_id schema is silently dropped and recreated.

        On the next call to verdict_init_db the table is rebuilt with title_key,
        so verdict_put and verdict_get work without raising OperationalError.
        The stale row from the old schema is gone (cache purge is acceptable).
        """
        stale_db = tmp_path / "stale.db"
        # Seed the OLD schema (listing_id PK instead of title_key)
        with sqlite3.connect(stale_db) as con:
            con.execute(
                """CREATE TABLE verdicts (
                    listing_id  TEXT NOT NULL,
                    wish_name   TEXT NOT NULL,
                    genuine     INTEGER NOT NULL,
                    PRIMARY KEY (listing_id, wish_name)
                )"""
            )
            con.execute(
                "INSERT INTO verdicts VALUES (?,?,?)",
                ("old_item_id", "ASM #129", 1),
            )
            con.commit()

        # verdict_init_db should detect the mismatch and auto-heal (no exception)
        ws.verdict_init_db(stale_db)

        # New-schema round-trip must work
        ws.verdict_put("amazing spider man  129 vf", "ASM #129", True, db_path=stale_db)
        assert ws.verdict_get("amazing spider man  129 vf", "ASM #129", db_path=stale_db) is True

        # Stale row is gone — the old listing_id key no longer exists in the table
        with sqlite3.connect(stale_db) as con:
            row = con.execute(
                "SELECT * FROM verdicts WHERE wish_name=? AND genuine=1", ("ASM #129",)
            ).fetchone()
        # Only the new row should be present (title_key column, not listing_id)
        assert row is not None
        assert row[0] == "amazing spider man  129 vf"  # title_key column


# ─── apply_verdict_cache ──────────────────────────────────────────────────────

class TestApplyVerdictCache:
    @pytest.fixture(autouse=True)
    def _db(self, tmp_path):
        self.db_path = tmp_path / "verdicts.db"

    def test_splits_into_three_buckets_correctly(self):
        # Use different wish_names so that same title with different wish_names
        # produces distinct (title_key, wish_name) cache entries.
        m_genuine = make_match(item_id="1", wish_name="ASM #129")
        m_false = make_match(item_id="2", wish_name="X-Men #94")
        m_uncached = make_match(item_id="3", wish_name="Hulk #181")
        # Pre-seed by title_key (not item_id) — the new cache key
        ws.verdict_put(ws._title_key(m_genuine["title"]), "ASM #129", True, db_path=self.db_path)
        ws.verdict_put(ws._title_key(m_false["title"]), "X-Men #94", False, db_path=self.db_path)
        # m_uncached has wish_name "Hulk #181" — no cache entry → uncached

        genuine, false_, uncached = ws.apply_verdict_cache(
            [m_genuine, m_false, m_uncached], self.db_path
        )
        assert [m["item_id"] for m in genuine] == ["1"]
        assert [m["item_id"] for m in false_] == ["2"]
        assert [m["item_id"] for m in uncached] == ["3"]

    def test_cached_false_lands_in_false_not_uncached(self):
        """Key assertion: a cached-false item must appear in false_, never uncached."""
        m = make_match(item_id="99", wish_name="ASM #129")
        # Pre-seed by title_key so apply_verdict_cache finds the entry
        ws.verdict_put(ws._title_key(m["title"]), "ASM #129", False, db_path=self.db_path)

        _, false_, uncached = ws.apply_verdict_cache([m], self.db_path)
        assert len(false_) == 1
        assert len(uncached) == 0


# ─── Empty wish list ──────────────────────────────────────────────────────────

class TestEmptyWishList:
    def test_empty_list_exits_nonzero(self):
        with patch.object(ws, "fetch_wish_list", return_value=[]):
            with pytest.raises(SystemExit) as exc:
                ws.main([])
            assert exc.value.code != 0

    def test_no_matchable_items_exits_nonzero(self):
        """Items without #N (GNs, HCs, TPBs) produce no wish_items → exit."""
        with (
            patch.object(ws, "fetch_wish_list", return_value=[
                {"id": "1", "name": "Secret Wars HC"},
            ]),
            patch.object(ws, "prepare_wish_items", return_value=[]),
        ):
            with pytest.raises(SystemExit) as exc:
                ws.main([])
            assert exc.value.code != 0


# ─── Owned filter ─────────────────────────────────────────────────────────────

class TestOwnedFilter:
    def test_owned_match_dropped_from_output(self, tmp_path):
        """A match whose (series, issue) is in_collection must not appear in output."""
        m1 = make_match(seller="S", item_id="1", wish_name="Amazing Spider-Man #129",
                        series="Amazing Spider-Man", issue="129")
        m2 = make_match(seller="S", item_id="2", wish_name="X-Men #94",
                        series="X-Men", issue="94")
        # Make m1's series/issue owned; m2 not owned
        owned_payload = {"count": 2, "results": [
            {"series": "Amazing Spider-Man", "issue": "129",
             "match_status": "in_collection", "full_title_matched": True, "cache_age_days": 0},
            {"series": "X-Men", "issue": "94",
             "match_status": "not_in_cache", "full_title_matched": False, "cache_age_days": 0},
        ]}

        mock_verify = MagicMock(return_value=([m2], [], []))
        mock_record = MagicMock()
        wish_list, wish_items = _two_wish_items()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value="http://server"),
            patch("requests.post") as mock_post,
            patch.object(ws, "verdict_db_path", return_value=tmp_path / "v.db"),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen", mock_record),
        ):
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=owned_payload),
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                ws.main([])
            output = buf.getvalue()

        # After dropping ASM #129 (owned), seller S has only X-Men #94 → drops below ≥2
        # Output should say no sellers found
        assert "No sellers found" in output

    def test_409_from_owned_check_hard_fails(self, tmp_path):
        """409 from collection/check/batch → sys.exit (plan R10/R11)."""
        m1 = make_match(seller="S", item_id="1")
        m2 = make_match(seller="S", item_id="2")
        wish_list, wish_items = _two_wish_items()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value="http://server"),
            patch("requests.post") as mock_post,
            patch.object(ws, "verdict_db_path", return_value=tmp_path / "v.db"),
        ):
            mock_post.return_value = MagicMock(
                status_code=409,
                text="collection not imported",
            )
            with pytest.raises(SystemExit):
                ws.main([])


# ─── Verdict cache pipeline integration ───────────────────────────────────────

class TestVerdictCachePipeline:
    def test_cached_false_not_sent_to_verify(self, tmp_path):
        """A cached-false listing is dropped without calling verify_with_claude for it."""
        db_path = tmp_path / "v.db"
        # sellerA has 3 matches: m1/m2 are uncached, m3 is cached-false.
        # After the ≥2 pre-verify gate, all three are candidates.
        # apply_verdict_cache puts m3 in false_, so verify only gets [m1, m2].
        m1 = make_match(seller="sellerA", item_id="1", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="sellerA", item_id="2", wish_name="X-Men #94")
        m3 = make_match(seller="sellerA", item_id="3", wish_name="Amazing Spider-Man #129",
                        title="Amazing Spider-Man #129 Annual-fake")
        # Pre-seed by title_key so apply_verdict_cache classifies m3 as cached_false
        ws.verdict_put(
            ws._title_key("Amazing Spider-Man #129 Annual-fake"),
            "Amazing Spider-Man #129",
            False,
            db_path=db_path,
        )

        wish_list = [
            {"id": "w1", "name": "Amazing Spider-Man #129"},
            {"id": "w2", "name": "X-Men #94"},
            {"id": "w3", "name": "Amazing Spider-Man #129"},  # extra search producing m3
        ]
        wish_items = [
            {"id": "w1", "name": "Amazing Spider-Man #129",
             "series": "Amazing Spider-Man", "issue": "129", "_tokens": ["amazing"]},
            {"id": "w2", "name": "X-Men #94",
             "series": "X-Men", "issue": "94", "_tokens": ["xmen"]},
            {"id": "w3", "name": "Amazing Spider-Man #129",
             "series": "Amazing Spider-Man", "issue": "129", "_tokens": ["amazing"]},
        ]

        mock_verify = MagicMock(return_value=([m1, m2], [], []))
        mock_record = MagicMock()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            # Three wish items → three side-effect values
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2], [m3]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen", mock_record),
        ):
            ws.main([])

        # verify should have been called — but NOT with m3
        mock_verify.assert_called_once()
        sent_ids = {m["item_id"] for m in mock_verify.call_args[0][0]}
        assert "3" not in sent_ids, "cached-false m3 must not be sent to verify"
        assert "1" in sent_ids
        assert "2" in sent_ids

    def test_uncached_listing_sent_to_verify_and_verdict_persisted(self, tmp_path):
        """Uncached listing → sent to verify; verdict is written to cache."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="10", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="S", item_id="11", wish_name="X-Men #94")

        mock_verify = MagicMock(return_value=([m1, m2], [], []))
        wish_list, wish_items = _two_wish_items()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        # Verdicts must be persisted by title_key (not item_id)
        # Both m1 and m2 use the default title "Amazing Spider-Man #129 VF"
        tk = ws._title_key("Amazing Spider-Man #129 VF")
        assert ws.verdict_get(tk, "Amazing Spider-Man #129", db_path=db_path) is True
        assert ws.verdict_get(tk, "X-Men #94", db_path=db_path) is True

    def test_second_run_uses_cache_no_verify_call(self, tmp_path):
        """On a re-run where all survivors are cached, verify_with_claude is not called."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="20", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="S", item_id="21", wish_name="X-Men #94")
        # Pre-seed both as genuine by title_key (both use default title)
        tk = ws._title_key("Amazing Spider-Man #129 VF")
        ws.verdict_put(tk, "Amazing Spider-Man #129", True, db_path=db_path)
        ws.verdict_put(tk, "X-Men #94", True, db_path=db_path)

        wish_list, wish_items = _two_wish_items()
        mock_verify = MagicMock(return_value=([], [], []))

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        mock_verify.assert_not_called()


# ─── BUI-297: never-verified (dropped) candidates ────────────────────────────

class TestVerifyDroppedCandidates:
    """verify_with_claude returns (kept, dropped). A `dropped` candidate was
    NEVER verified (claude CLI timeout/transport failure) — it must NOT be
    cached as a verdict (a False cache would make it a permanent silent
    rejection) and must NOT flow into the survivors that get recorded as seen,
    so it resurfaces and re-verifies on the next scheduled run."""

    def test_dropped_is_not_cached_not_returned_and_warned(self, tmp_path, capsys):
        db_path = tmp_path / "v.db"
        ws.verdict_init_db(db_path)

        m_genuine = make_match(
            wish_name="Amazing Spider-Man #129", title="Amazing Spider-Man #129 VF"
        )
        m_dropped = make_match(wish_name="X-Men #94", title="X-Men #94 NM")
        m_rejected = make_match(wish_name="Daredevil #1", title="Daredevil #1 VG")
        uncached = [m_genuine, m_dropped, m_rejected]

        with (
            patch.object(ws, "is_pristine_match", return_value=False),
            patch.object(
                ws, "verify_with_claude",
                return_value=([m_genuine], [m_dropped], []),  # m_rejected: neither
            ),
        ):
            pristine_direct, verified, dropped = ws._verify_uncached_matches(uncached, db_path)

        # Dropped candidate is never returned as a survivor, but IS reported
        # separately (BUI-309) so the caller can surface it in --json /
        # exit code without treating it as genuine.
        assert pristine_direct == []
        assert verified == [m_genuine]
        assert dropped == [m_dropped]

        gk = ws._title_key(m_genuine["title"])
        rk = ws._title_key(m_rejected["title"])
        dk = ws._title_key(m_dropped["title"])
        # Genuine cached True, rejected cached False, dropped NOT cached at all.
        assert ws.verdict_get(gk, "Amazing Spider-Man #129", db_path=db_path) is True
        assert ws.verdict_get(rk, "Daredevil #1", db_path=db_path) is False
        assert ws.verdict_get(dk, "X-Men #94", db_path=db_path) is None

        # Loud stderr warning so an unattended scheduled run isn't silent.
        err = capsys.readouterr().err
        assert "never verified" in err
        assert "resurface" in err

    def test_dropped_survivor_not_recorded_as_seen(self, tmp_path):
        """End-to-end: a seller's second book is dropped (never verified) →
        seller falls below the ≥2 gate and NONE of its item_ids are recorded as
        seen, so the dropped book resurfaces next run."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="sellerZ", item_id="1", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="sellerZ", item_id="2", wish_name="X-Men #94")
        wish_list, wish_items = _two_wish_items()

        _, _exit, _, mock_record = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1],       # only m1 verified genuine
            dropped_return=[m2],      # m2 never verified
        )

        # m2 (dropped) must never be marked seen. sellerZ falls below 2 with only
        # m1, so nothing is recorded at all — and critically not item_id "2".
        recorded_ids = [i for call in mock_record.call_args_list for i in call.args[0]]
        assert "2" not in recorded_ids


# ─── BUI-309: observability contract (--json object + non-zero exit) ────────

class TestBui309ObservabilityContract:
    """wishlist-sellers runs UNATTENDED on a schedule, so a stderr WARNING
    for never-verified ('dropped') candidates is too weak a signal. BUI-309
    mirrors seller_scan.py's BUI-298 contract: `--json` is always a top-level
    OBJECT (never a bare array) carrying a `dropped_candidates` field, and the
    process exits non-zero (_EXIT_INCOMPLETE, reused from seller_scan.py) when
    any candidate was never verified — 0 on a clean run either way."""

    @staticmethod
    def _two_seller_setup():
        """sellerA and sellerB each have 2 raw candidates (one per wish item),
        so both clear the pre-verify ≥2 gate. mB2 is the one BUI-309 tests
        drop (never verify). Titles are all distinct so the cross-seller
        (title_key, wish_name) dedup in _verify_uncached_matches never
        collapses two of these into the same representative — each match
        must get its own, independently mockable verify verdict."""
        mA1 = make_match(seller="sellerA", item_id="a1",
                        wish_name="Amazing Spider-Man #129",
                        title="Amazing Spider-Man #129 VF (seller A copy)")
        mA2 = make_match(seller="sellerA", item_id="a2", wish_name="X-Men #94",
                        title="X-Men #94 VF (seller A copy)")
        mB1 = make_match(seller="sellerB", item_id="b1",
                        wish_name="Amazing Spider-Man #129",
                        title="Amazing Spider-Man #129 NM (seller B copy)")
        mB2 = make_match(seller="sellerB", item_id="b2", wish_name="X-Men #94",
                        title="X-Men #94 NM (seller B copy)")
        return mA1, mA2, mB1, mB2

    def test_exit_code_nonzero_when_candidates_dropped(self, tmp_path):
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        _, exit_code, _, _ = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1],
            dropped_return=[mB2],
        )

        assert exit_code == ws._EXIT_INCOMPLETE
        assert exit_code == 3, "exit 3 mirrors seller_scan.py's BUI-298 drop signal"

    def test_exit_code_zero_on_clean_run(self, tmp_path):
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        _, exit_code, _, _ = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1, mB2],
            dropped_return=[],
        )

        assert exit_code == 0

    def test_json_is_object_with_dropped_candidates(self, tmp_path):
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        output, exit_code, _, _ = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1],
            dropped_return=[mB2],
            json_output=True,
        )

        data = json.loads(output)
        assert isinstance(data, dict), "BUI-309: --json is always an object, never a bare array"
        assert data["incomplete"] is True
        assert len(data["dropped_candidates"]) == 1
        dropped = data["dropped_candidates"][0]
        assert dropped["item_id"] == "b2"
        assert "_series" not in dropped
        assert "_issue" not in dropped
        # sellerB's second candidate never verified → falls below the ≥2 gate
        # post-verify and drops out of `sellers`; sellerA is unaffected.
        seller_names = {s["seller"] for s in data["sellers"]}
        assert seller_names == {"sellerA"}
        assert exit_code == ws._EXIT_INCOMPLETE

    def test_json_dropped_candidates_empty_on_clean_run(self, tmp_path):
        """`sellers`/kept-match behavior is unchanged under the new object
        envelope — only the top-level shape changed (array → object)."""
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        output, exit_code, _, _ = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1, mB2],
            dropped_return=[],
            json_output=True,
        )

        data = json.loads(output)
        assert data["incomplete"] is False
        assert data["dropped_candidates"] == []
        seller_names = {s["seller"] for s in data["sellers"]}
        assert seller_names == {"sellerA", "sellerB"}
        assert exit_code == 0

    def test_human_readable_output_signals_partial_run(self, tmp_path):
        """The non-JSON table path also signals an incomplete run — not just
        the stderr WARNING already printed during verification."""
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        output, exit_code, _, _ = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1],
            dropped_return=[mB2],
        )

        assert "INCOMPLETE" in output
        assert "never verified" in output.lower()
        assert exit_code == ws._EXIT_INCOMPLETE

    def test_human_readable_output_silent_on_clean_run(self, tmp_path):
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        output, exit_code, _, _ = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1, mB2],
            dropped_return=[],
        )

        assert "INCOMPLETE" not in output
        assert exit_code == 0

    def test_dropped_candidate_stays_uncached_and_unseen(self, tmp_path):
        """Data-safety invariant (BUI-297) is unchanged by BUI-309's
        observability upgrade: a dropped candidate is never cached as a
        verdict and never recorded as seen, so it resurfaces next run."""
        db_path = tmp_path / "v.db"
        mA1, mA2, mB1, mB2 = self._two_seller_setup()
        wish_list, wish_items = _two_wish_items()

        _, _exit, _, mock_record = _run_main(
            [[mA1, mB1], [mA2, mB2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[mA1, mA2, mB1],
            dropped_return=[mB2],
        )

        recorded_ids = [i for call in mock_record.call_args_list for i in call.args[0]]
        assert "b2" not in recorded_ids
        assert ws.verdict_get(
            ws._title_key(mB2["title"]), mB2["wish_name"], db_path=db_path
        ) is None

    @staticmethod
    def _three_wish_items():
        wish_list = [
            {"id": "w1", "name": "Amazing Spider-Man #129"},
            {"id": "w2", "name": "X-Men #94"},
            {"id": "w3", "name": "Fantastic Four #48"},
        ]
        wish_items = [
            {"id": "w1", "name": "Amazing Spider-Man #129",
             "series": "Amazing Spider-Man", "issue": "129",
             "_tokens": ["amazing", "spider", "man"]},
            {"id": "w2", "name": "X-Men #94", "series": "X-Men", "issue": "94",
             "_tokens": ["xmen"]},
            {"id": "w3", "name": "Fantastic Four #48",
             "series": "Fantastic Four", "issue": "48",
             "_tokens": ["fantastic", "four"]},
        ]
        return wish_list, wish_items

    def test_seller_survives_while_contributing_a_dropped_candidate(self, tmp_path):
        """A 3-book seller loses ONE candidate to the never-verified path but
        still clears the >=2 gate — so `sellers` is non-empty AND contains that
        seller, while `dropped_candidates` also references it. Exit is still 3.
        (The other BUI-309 tests only cover a drop pushing the seller below the
        gate; this pins the seller-survives-with-a-drop composition.)"""
        db_path = tmp_path / "v.db"
        wish_list, wish_items = self._three_wish_items()
        m1 = make_match(seller="big", item_id="1", wish_name="Amazing Spider-Man #129",
                        title="Amazing Spider-Man #129 VF")
        m2 = make_match(seller="big", item_id="2", wish_name="X-Men #94",
                        title="X-Men #94 VF")
        m3 = make_match(seller="big", item_id="3", wish_name="Fantastic Four #48",
                        title="Fantastic Four #48 VF")

        output, exit_code, _, mock_record = _run_main(
            [[m1], [m2], [m3]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],   # 2 kept -> seller stays above the gate
            dropped_return=[m3],      # 1 never verified
            json_output=True,
        )

        data = json.loads(output)
        assert data["incomplete"] is True
        assert exit_code == ws._EXIT_INCOMPLETE
        # Seller survives (>=2 verified) AND appears in the output...
        assert {s["seller"] for s in data["sellers"]} == {"big"}
        # ...while its dropped book is reported separately, attributable via its
        # own `seller` key (the flat dropped_candidates has no per-seller nesting).
        assert len(data["dropped_candidates"]) == 1
        assert data["dropped_candidates"][0]["item_id"] == "3"
        assert data["dropped_candidates"][0]["seller"] == "big"
        # The dropped book (item 3) is never recorded as seen; the kept ones are.
        recorded_ids = [i for call in mock_record.call_args_list for i in call.args[0]]
        assert "3" not in recorded_ids
        assert {"1", "2"} <= set(recorded_ids)

    def test_one_dropped_key_fans_out_to_every_sharing_listing(self, tmp_path):
        """The whole reason `dropped` is fanned out (not one representative per
        key): two sellers list the SAME book (same title + wish_name -> one
        verdict key). verify_with_claude sees ONE representative and drops it;
        BOTH listings must land in dropped_candidates (BUI-309), not just the
        representative. All other BUI-309 tests use distinct titles so the key
        never collapses and dropped_candidates length is always 1."""
        db_path = tmp_path / "v.db"
        wish_list, wish_items = self._three_wish_items()
        # Two sellers, identical ASM #129 listing -> same (title_key, wish_name).
        asm_s1 = make_match(seller="s1", item_id="a1",
                            wish_name="Amazing Spider-Man #129",
                            title="Amazing Spider-Man #129 VF")
        asm_s2 = make_match(seller="s2", item_id="a2",
                            wish_name="Amazing Spider-Man #129",
                            title="Amazing Spider-Man #129 VF")
        # A distinct 2nd book per seller so each clears the pre-verify >=2 gate.
        xmen_s1 = make_match(seller="s1", item_id="x1", wish_name="X-Men #94",
                             title="X-Men #94 VF")
        ff_s2 = make_match(seller="s2", item_id="f2", wish_name="Fantastic Four #48",
                           title="Fantastic Four #48 VF")

        output, exit_code, _, _ = _run_main(
            [[asm_s1, asm_s2], [xmen_s1], [ff_s2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            # verify sees one representative per key: keep the X-Men + FF reps,
            # drop the shared ASM representative.
            verify_return=[xmen_s1, ff_s2],
            dropped_return=[asm_s1],
            json_output=True,
        )

        data = json.loads(output)
        assert data["incomplete"] is True
        assert exit_code == ws._EXIT_INCOMPLETE
        # The single dropped KEY fans out to BOTH listings that share it.
        dropped_ids = {d["item_id"] for d in data["dropped_candidates"]}
        assert dropped_ids == {"a1", "a2"}


# ─── Post-verify re-gate ──────────────────────────────────────────────────────

class TestPostVerifyReGate:
    def test_group_and_gate_drops_seller_below_two_post_verify(self):
        """group_and_gate on a 1-match set correctly drops the seller."""
        m = make_match(seller="X", item_id="99")
        assert "X" not in ws.group_and_gate([m])

    def test_pipeline_drops_seller_that_falls_below_two_after_verify(self, tmp_path):
        """End-to-end: seller starts with 2 candidates; verify keeps 1 → dropped."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="sellerX", item_id="1", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="sellerX", item_id="2", wish_name="X-Men #94")
        # verify keeps only m1
        wish_list, wish_items = _two_wish_items()

        buf = io.StringIO()
        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", return_value=([m1], [], [])),  # only m1 kept
            patch.object(ws, "record_items_seen"),
        ):
            with redirect_stdout(buf):
                ws.main([])

        output = buf.getvalue()
        # sellerX falls below 2 after verify → not in output
        assert "sellerX" not in output
        assert "No sellers found" in output


# ─── End-to-end happy path ────────────────────────────────────────────────────

class TestEndToEndHappyPath:
    def test_table_output_contains_seller_and_wish_items(self, tmp_path):
        """Full pipeline: two genuine matches → compact table with seller and wish items."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="comicseller1", item_id="100",
                        wish_name="Amazing Spider-Man #129", price="$120.00")
        m2 = make_match(seller="comicseller1", item_id="101",
                        wish_name="X-Men #94", price="$80.00")
        wish_list, wish_items = _two_wish_items()

        output, _exit, _, mock_record = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
        )

        assert "comicseller1" in output
        assert "Amazing Spider-Man #129" in output
        assert "X-Men #94" in output

    def test_json_output_valid_and_no_private_fields(self, tmp_path):
        """--json emits a top-level OBJECT (BUI-309) with a `sellers` array;
        private keys _series/_issue are stripped from each match."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="comicseller1", item_id="200",
                        wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="comicseller1", item_id="201", wish_name="X-Men #94")
        wish_list, wish_items = _two_wish_items()

        output, exit_code, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
            json_output=True,
        )

        data = json.loads(output)
        assert isinstance(data, dict), "BUI-309: --json is always an object, never a bare array"
        assert data["incomplete"] is False
        assert data["dropped_candidates"] == []
        assert len(data["sellers"]) == 1
        assert data["sellers"][0]["seller"] == "comicseller1"
        assert len(data["sellers"][0]["matches"]) == 2
        for match in data["sellers"][0]["matches"]:
            assert "_series" not in match
            assert "_issue" not in match
        assert exit_code == 0

    def test_seen_items_dropped_before_output(self, tmp_path):
        """Items already in the global seen set are hidden from the report."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="300", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="S", item_id="301", wish_name="X-Men #94")
        wish_list, wish_items = _two_wish_items()

        output, _exit, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            seen={"300", "301"},  # both already seen
            verify_return=[],
        )
        # No survivors → no sellers
        assert "S" not in output

    def test_record_items_seen_called_with_final_ids(self, tmp_path):
        """record_items_seen is called with the final genuine item IDs."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="400", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="S", item_id="401", wish_name="X-Men #94")
        wish_list, wish_items = _two_wish_items()

        _, _exit, _, mock_record = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
        )

        mock_record.assert_called_once()
        recorded_ids = mock_record.call_args[0][0]
        assert set(recorded_ids) == {"400", "401"}
        # Global seen set: seller arg must be None (plan R11)
        assert mock_record.call_args[0][1] is None


class TestRunFlags:
    """--limit and --no-record-seen (bounded / repeatable smoke runs)."""

    def test_no_record_seen_skips_seen_write(self, tmp_path):
        """--no-record-seen surfaces sellers but never writes to the seen-store."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="s1", item_id="100", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="s1", item_id="101", wish_name="X-Men #94")

        output, _exit, _, mock_record = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            verify_return=[m1, m2],
            extra_args=["--no-record-seen"],
        )

        assert "s1" in output            # seller still surfaced
        mock_record.assert_not_called()  # but nothing written to seen-store

    def test_records_seen_without_flag(self, tmp_path):
        """Default (no flag) DOES write to the seen-store — guards the flag's effect."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="s1", item_id="100", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="s1", item_id="101", wish_name="X-Men #94")

        _, _exit, _, mock_record = _run_main(
            [[m1], [m2]], db_path=db_path, verify_return=[m1, m2],
        )

        mock_record.assert_called_once()

    def test_limit_processes_only_first_n_wish_items(self, tmp_path):
        """--limit 1 stops after the first wish item, so the same-seller pair
        never both appear → the ≥2 gate drops the lone match."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="s1", item_id="100", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="s1", item_id="101", wish_name="X-Men #94")

        output, _exit, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            verify_return=[m1, m2],
            extra_args=["--limit", "1"],
        )

        # Only the first wish item is processed → a single match for s1 →
        # below the ≥2 threshold → no sellers surfaced.
        assert "s1" not in output
        assert "No sellers" in output


class TestBuyingOptionsFlag:
    """BUI-225: --buying-options flag mapping, default, and funnel wiring."""

    def _run_capturing_search(
        self,
        argv: list,
        *,
        tmp_path,
    ) -> tuple:
        """Run main() with minimal mocks; return (mock_search, mock_cache_get, mock_cache_put)."""
        wish_list = [{"id": "w1", "name": "Amazing Spider-Man #129"}]
        wish_items = [
            {
                "id": "w1",
                "name": "Amazing Spider-Man #129",
                "series": "Amazing Spider-Man",
                "issue": "129",
                "_tokens": ["amazing", "spider", "man"],
            }
        ]
        mock_search = MagicMock(return_value=[])
        mock_cache_get = MagicMock(return_value=None)
        mock_cache_put = MagicMock()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", mock_cache_get),
            patch("wishlist_sellers.search_by_keyword", mock_search),
            patch("wishlist_sellers.ebay_search_cache.put", mock_cache_put),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", return_value=[]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=tmp_path / "v.db"),
            patch.object(ws, "verify_with_claude", return_value=([], [], [])),
            patch.object(ws, "record_items_seen"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ws.main(argv)

        return mock_search, mock_cache_get, mock_cache_put

    def test_map_covers_all_choices(self):
        """_BUYING_OPTIONS_MAP has the correct eBay filter string for each choice."""
        assert ws._BUYING_OPTIONS_MAP["auction"] == "AUCTION"
        assert ws._BUYING_OPTIONS_MAP["bin"] == "FIXED_PRICE"
        assert ws._BUYING_OPTIONS_MAP["all"] == "AUCTION|FIXED_PRICE"

    def test_default_is_auction(self, tmp_path):
        """No --buying-options flag → default 'auction' → AUCTION sent to search_by_keyword."""
        mock_search, _, _ = self._run_capturing_search([], tmp_path=tmp_path)
        kwargs = mock_search.call_args[1]
        assert kwargs.get("buying_options") == "AUCTION"

    def test_auction_flag_maps_to_AUCTION(self, tmp_path):
        """--buying-options auction → buying_options='AUCTION' passed to search."""
        mock_search, _, _ = self._run_capturing_search(
            ["--buying-options", "auction"], tmp_path=tmp_path
        )
        assert mock_search.call_args[1].get("buying_options") == "AUCTION"

    def test_bin_flag_maps_to_FIXED_PRICE(self, tmp_path):
        """--buying-options bin → buying_options='FIXED_PRICE' passed to search."""
        mock_search, _, _ = self._run_capturing_search(
            ["--buying-options", "bin"], tmp_path=tmp_path
        )
        assert mock_search.call_args[1].get("buying_options") == "FIXED_PRICE"

    def test_all_flag_maps_to_AUCTION_FIXED_PRICE(self, tmp_path):
        """--buying-options all → buying_options='AUCTION|FIXED_PRICE' passed to search."""
        mock_search, _, _ = self._run_capturing_search(
            ["--buying-options", "all"], tmp_path=tmp_path
        )
        assert mock_search.call_args[1].get("buying_options") == "AUCTION|FIXED_PRICE"

    def test_funnel_passes_mode_to_cache_get(self, tmp_path):
        """cache.get is called with mode='auction' when --buying-options auction (default)."""
        _, mock_cache_get, _ = self._run_capturing_search([], tmp_path=tmp_path)
        kwargs = mock_cache_get.call_args[1]
        assert kwargs.get("mode") == "auction"

    def test_funnel_passes_mode_to_cache_put(self, tmp_path):
        """cache.put is called with mode='all' when --buying-options all."""
        _, _, mock_cache_put = self._run_capturing_search(
            ["--buying-options", "all"], tmp_path=tmp_path
        )
        kwargs = mock_cache_put.call_args[1]
        assert kwargs.get("mode") == "all"

    def test_funnel_cache_mode_matches_flag(self, tmp_path):
        """cache.get and cache.put both receive the same mode as the --buying-options flag."""
        _, mock_cache_get, mock_cache_put = self._run_capturing_search(
            ["--buying-options", "bin"], tmp_path=tmp_path
        )
        assert mock_cache_get.call_args[1].get("mode") == "bin"
        assert mock_cache_put.call_args[1].get("mode") == "bin"


class TestFanoutGuards:
    """Skip degenerate numeric-series names + cap per-item noise (smoke finding)."""

    def test_is_degenerate_series(self):
        assert ws.is_degenerate_series({"name": "300 #1", "_tokens": ["300"]}) is True
        assert ws.is_degenerate_series({"name": "52 #1", "_tokens": ["52"]}) is True
        assert ws.is_degenerate_series({"name": "1985 #1", "_tokens": ["1985"]}) is True
        # multi-token / alpha series are NOT degenerate
        assert ws.is_degenerate_series(
            {"name": "Amazing Spider-Man #1", "_tokens": ["amazing", "spider", "man"]}
        ) is False
        # single short ALPHA token (X-Men → ["men"]) is kept — issue # constrains it
        assert ws.is_degenerate_series({"name": "X-Men #1", "_tokens": ["men"]}) is False
        assert ws.is_degenerate_series({"name": "x", "_tokens": []}) is False

    def test_degenerate_series_skipped_from_search(self, tmp_path):
        """A numeric-series wish item is skipped, so match is never invoked and
        the run completes clean (empty side_effect would StopIteration if not)."""
        db_path = tmp_path / "v.db"
        wl = [{"id": "d", "name": "300 #1"}]
        wi = [{"id": "d", "name": "300 #1", "series": "300", "issue": "1",
               "_tokens": ["300"]}]
        output, _exit, _, _ = _run_main([], db_path=db_path, wish_list=wl, wish_items=wi)
        assert "No sellers" in output

    def test_variant_saturation_is_not_a_combine_signal(self, tmp_path):
        """A seller with many variant copies of ONE wish book (and no second
        book) must NOT pass the ≥2 gate — distinct books, not listings."""
        db_path = tmp_path / "v.db"
        wl, wi = _two_wish_items()
        # First wish item: 50 variant listings of the SAME book from one seller.
        variants = [make_match(seller="s1", item_id=str(3000 + i),
                               wish_name="Amazing Spider-Man #129",
                               price=f"${10 + i}.00") for i in range(50)]
        output, _exit, _, _ = _run_main([variants, []], db_path=db_path,
                                 wish_list=wl, wish_items=wi,
                                 verify_return=variants)
        # 50 variants collapse to 1 distinct book → below the ≥2 gate.
        assert "s1" not in output
        assert "No sellers" in output


# ─── BUI-223: title-keyed cache + cross-seller dedup ─────────────────────────

class TestTitleKey:
    """Unit tests for the _title_key helper."""

    def test_is_stable(self):
        """Same title produces the same key on every call."""
        title = "Amazing Spider-Man #129 VF/NM 9.0"
        assert ws._title_key(title) == ws._title_key(title)

    def test_strips_decimal_grade(self):
        """Decimal grade tokens (CGC slab grades) are removed from the key."""
        key = ws._title_key("X-Men #94 9.8")
        # The decimal grade "9.8" must not survive as a continuous digit run
        assert "9.8" not in key.replace(" ", "")
        # But the issue number 94 is preserved
        assert "94" in key

    def test_normalizes_punctuation(self):
        """Non-alphanumeric characters become spaces (hash, hyphen, parens)."""
        key = ws._title_key("X-Men #94 (1977)")
        assert "-" not in key
        assert "#" not in key
        assert "(" not in key

    def test_lowercased(self):
        """Output is always lowercase."""
        assert ws._title_key("Amazing Spider-Man #129 VF") == ws._title_key(
            "amazing spider-man #129 vf"
        )

    def test_same_title_different_item_ids(self):
        """Two listings with identical titles produce the same key (relist scenario)."""
        title = "Incredible Hulk #181 FN/VF"
        assert ws._title_key(title) == ws._title_key(title)


class TestTitleKeyedCache:
    """BUI-223: cache is keyed by (title_key, wish_name), not (item_id, wish_name)."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path):
        self.db_path = tmp_path / "verdicts.db"

    def test_different_item_id_same_title_is_cache_hit(self):
        """A relist with a new item_id but same title → cache HIT."""
        title = "X-Men #94 NM"
        wish_name = "X-Men #94"
        # Seed the cache as if a previous item was verified
        ws.verdict_put(ws._title_key(title), wish_name, True, db_path=self.db_path)
        # New listing — different item_id, same title
        relisted = make_match(item_id="new_999", wish_name=wish_name, title=title)
        genuine, _false, uncached = ws.apply_verdict_cache([relisted], self.db_path)
        assert len(genuine) == 1
        assert genuine[0]["item_id"] == "new_999"
        assert len(uncached) == 0

    def test_different_title_is_cache_miss(self):
        """A listing with a different title is NOT a cache hit even if it matches
        the same wish item."""
        title_a = "X-Men #94 NM"
        title_b = "X-Men #94 FN"  # different condition → different title → different key
        wish_name = "X-Men #94"
        ws.verdict_put(ws._title_key(title_a), wish_name, True, db_path=self.db_path)
        m = make_match(item_id="1", wish_name=wish_name, title=title_b)
        _genuine, _false, uncached = ws.apply_verdict_cache([m], self.db_path)
        # title_b has a different key from title_a → cache miss
        # (title_b not seeded, so it lands in uncached)
        assert len(uncached) == 1


class TestCrossSellerDedup:
    """BUI-223: same title from multiple sellers → one Haiku call per unique (title, wish)."""

    def test_two_sellers_same_title_one_verify_call(self, tmp_path):
        """Same title from sellers A and B → verify called once with one representative."""
        db_path = tmp_path / "v.db"
        shared_title = "Amazing Spider-Man #129 VF"
        m_a = make_match(seller="sellerA", item_id="a1", wish_name="Amazing Spider-Man #129",
                         title=shared_title)
        m_b = make_match(seller="sellerB", item_id="b1", wish_name="Amazing Spider-Man #129",
                         title=shared_title)
        # Second distinct wish book so both sellers pass the ≥2 gate
        m_a2 = make_match(seller="sellerA", item_id="a2", wish_name="X-Men #94",
                          title="X-Men #94 NM")
        m_b2 = make_match(seller="sellerB", item_id="b2", wish_name="X-Men #94",
                          title="X-Men #94 NM")
        wish_list, wish_items = _two_wish_items()

        mock_verify = MagicMock(side_effect=lambda reps: (reps, [], []))

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m_a, m_b], [m_a2, m_b2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        # verify_with_claude must be called exactly once
        mock_verify.assert_called_once()
        sent = mock_verify.call_args[0][0]
        # 4 uncached listings but only 2 unique (title_key, wish_name) pairs →
        # only 2 representatives sent to verify (one per pair)
        sent_keys = {(ws._title_key(m["title"]), m["wish_name"]) for m in sent}
        assert len(sent) == 2, f"Expected 2 representatives, got {len(sent)}"
        assert len(sent_keys) == 2

    def test_cross_seller_dedup_both_sellers_genuine_in_output(self, tmp_path):
        """After dedup, both sellers still appear genuine in the final output."""
        db_path = tmp_path / "v.db"
        shared_title = "Amazing Spider-Man #129 VF"
        m_a = make_match(seller="sellerA", item_id="a1", wish_name="Amazing Spider-Man #129",
                         title=shared_title)
        m_b = make_match(seller="sellerB", item_id="b1", wish_name="Amazing Spider-Man #129",
                         title=shared_title)
        m_a2 = make_match(seller="sellerA", item_id="a2", wish_name="X-Men #94",
                          title="X-Men #94 NM")
        m_b2 = make_match(seller="sellerB", item_id="b2", wish_name="X-Men #94",
                          title="X-Men #94 NM")
        wish_list, wish_items = _two_wish_items()

        output, _exit, _, _ = _run_main(
            [[m_a, m_b], [m_a2, m_b2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=None,  # uses default: all matches returned genuine
        )

        # Both sellers have 2 genuine matches → both surface
        assert "sellerA" in output
        assert "sellerB" in output

    def test_warm_rerun_makes_zero_verify_calls(self, tmp_path):
        """A fully warm re-run (all title_keys cached) makes no Haiku calls."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="100", wish_name="Amazing Spider-Man #129",
                        title="Amazing Spider-Man #129 VF")
        m2 = make_match(seller="S", item_id="101", wish_name="X-Men #94",
                        title="X-Men #94 NM")
        # Pre-warm the cache by title_key
        ws.verdict_put(ws._title_key(m1["title"]), "Amazing Spider-Man #129", True, db_path=db_path)
        ws.verdict_put(ws._title_key(m2["title"]), "X-Men #94", True, db_path=db_path)
        wish_list, wish_items = _two_wish_items()
        mock_verify = MagicMock(return_value=([], [], []))

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        mock_verify.assert_not_called()


# ─── BUI-224: pristine-match shortcut ────────────────────────────────────────

class TestIsPristineMatch:
    """Unit tests for the is_pristine_match predicate (BUI-224)."""

    def test_clean_grade_noise_is_pristine(self):
        """A clean title with only a grade abbreviation trailing is pristine."""
        m = make_match(
            title="X-Men #94 NM",
            series="X-Men",
            issue="94",
            score=1.0,
            wish_name="X-Men #94",
        )
        assert ws.is_pristine_match(m) is True

    def test_condition_and_era_noise_is_pristine(self):
        """Condition abbreviation + newsstand era token trailing → pristine."""
        m = make_match(
            title="Amazing Spider-Man #129 VF newsstand",
            series="Amazing Spider-Man",
            issue="129",
            score=1.0,
            wish_name="Amazing Spider-Man #129",
        )
        assert ws.is_pristine_match(m) is True

    def test_annual_in_title_not_pristine(self):
        """Annual in title (but not in series) → hard_reject catches it → not pristine."""
        m = make_match(
            title="X-Men #94 Annual",
            series="X-Men",
            issue="94",
            score=1.0,
            wish_name="X-Men #94",
        )
        assert ws.is_pristine_match(m) is False

    def test_subtitle_token_not_pristine(self):
        """Trailing subtitle token outside allow-list → not pristine (routes to Haiku)."""
        m = make_match(
            title="Spider-Man #129 Noir",
            series="Spider-Man",
            issue="129",
            score=1.0,
            wish_name="Spider-Man #129",
        )
        assert ws.is_pristine_match(m) is False

    def test_score_below_one_not_pristine(self):
        """Score below 1.0 → not pristine regardless of title shape."""
        m = make_match(
            title="X-Men #94 NM",
            series="X-Men",
            issue="94",
            score=0.9,
            wish_name="X-Men #94",
        )
        assert ws.is_pristine_match(m) is False

    def test_wrong_series_tokens_interrupt_prefix(self):
        """'Aliens vs Avengers #1' against wish series 'Aliens': 'vs' sits where
        the issue should be → prefix fails → not pristine (routes to Haiku)."""
        m = make_match(
            title="Aliens vs Avengers #1",
            series="Aliens",
            issue="1",
            score=1.0,
            wish_name="Aliens #1",
        )
        assert ws.is_pristine_match(m) is False

    def test_per132_wrong_series_not_pristine(self):
        """PER-132-style wrong-series match: 'Wolverine Origins #1 VF' for wish
        'Wolverine #1' — 'Origins' interrupts the expected prefix → not pristine."""
        m = make_match(
            title="Wolverine Origins #1 VF",
            series="Wolverine",
            issue="1",
            score=1.0,
            wish_name="Wolverine #1",
        )
        assert ws.is_pristine_match(m) is False

    def test_title_year_trailing_is_not_pristine(self):
        """A bare year in 1930–2035 after the issue disqualifies pristine (relaunch risk)."""
        m = make_match(
            title="Amazing Spider-Man #129 1974",
            series="Amazing Spider-Man",
            issue="129",
            score=1.0,
            wish_name="Amazing Spider-Man #129",
        )
        assert ws.is_pristine_match(m) is False

    def test_modern_relaunch_year_not_pristine(self):
        """'X-Men #1 2019 NM' — bare year 2019 in 1930-2035 → not pristine."""
        m = make_match(
            title="X-Men #1 2019 NM",
            series="X-Men",
            issue="1",
            score=1.0,
            wish_name="X-Men #1",
        )
        assert ws.is_pristine_match(m) is False

    def test_non_year_number_trailing_is_pristine(self):
        """A bare non-year integer (e.g. copy count '3') is harmless → pristine."""
        m = make_match(
            title="X-Men #1 NM 3",
            series="X-Men",
            issue="1",
            score=1.0,
            wish_name="X-Men #1",
        )
        assert ws.is_pristine_match(m) is True

    def test_first_print_noise_is_pristine(self):
        """Printing-era token '1st' trailing the issue → in allow-list → pristine."""
        m = make_match(
            title="Amazing Spider-Man #300 VF 1st",
            series="Amazing Spider-Man",
            issue="300",
            score=1.0,
            wish_name="Amazing Spider-Man #300",
        )
        assert ws.is_pristine_match(m) is True


class TestPristineMatchFunnel:
    """BUI-224: pristine shortcut integration in the main() pipeline."""

    def test_pristine_not_sent_to_verify_appears_genuine_and_persisted(self, tmp_path):
        """A pristine match is not sent to verify_with_claude, yet it appears in
        the final output as genuine and its verdict is persisted to the cache."""
        db_path = tmp_path / "v.db"
        # m1: score=1.0, grade-only trailing token → pristine
        m1 = make_match(
            seller="sellerA", item_id="1", wish_name="Amazing Spider-Man #129",
            title="Amazing Spider-Man #129 NM",
            series="Amazing Spider-Man", issue="129", score=1.0,
        )
        # m2: score=1.0 but 'Noir' trailing token → not pristine → sent to verify
        m2 = make_match(
            seller="sellerA", item_id="2", wish_name="X-Men #94",
            title="X-Men #94 Noir",
            series="X-Men", issue="94", score=1.0,
        )
        wish_list, wish_items = _two_wish_items()

        # verify_with_claude returns m2 as genuine (m1 never reaches it)
        output, _exit, mock_verify, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m2],
        )

        # verify must be called — but NOT with m1 (pristine)
        mock_verify.assert_called_once()
        sent_ids = {m["item_id"] for m in mock_verify.call_args[0][0]}
        assert "1" not in sent_ids, "pristine m1 must not be sent to verify"
        assert "2" in sent_ids, "ambiguous m2 must be sent to verify"

        # Both matches are genuine → sellerA has 2 → appears in output
        assert "sellerA" in output

        # Pristine verdict for m1 must be persisted as genuine by title_key
        tk_m1 = ws._title_key("Amazing Spider-Man #129 NM")
        assert ws.verdict_get(tk_m1, "Amazing Spider-Man #129", db_path=db_path) is True

    def test_all_pristine_no_verify_call(self, tmp_path):
        """When all uncached candidates are pristine, verify_with_claude is not called."""
        db_path = tmp_path / "v.db"
        m1 = make_match(
            seller="sellerA", item_id="1", wish_name="Amazing Spider-Man #129",
            title="Amazing Spider-Man #129 NM",
            series="Amazing Spider-Man", issue="129", score=1.0,
        )
        m2 = make_match(
            seller="sellerA", item_id="2", wish_name="X-Men #94",
            title="X-Men #94 VF",
            series="X-Men", issue="94", score=1.0,
        )
        wish_list, wish_items = _two_wish_items()

        _, _exit, mock_verify, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[],  # never called
        )

        mock_verify.assert_not_called()

    def test_pristine_warm_rerun_hits_cache_no_verify(self, tmp_path):
        """A re-run where the pristine verdict is already cached never calls verify
        (it goes through apply_verdict_cache as cached_genuine, not through is_pristine)."""
        db_path = tmp_path / "v.db"
        m1 = make_match(
            seller="sellerA", item_id="1", wish_name="Amazing Spider-Man #129",
            title="Amazing Spider-Man #129 NM",
            series="Amazing Spider-Man", issue="129", score=1.0,
        )
        m2 = make_match(
            seller="sellerA", item_id="2", wish_name="X-Men #94",
            title="X-Men #94 VF",
            series="X-Men", issue="94", score=1.0,
        )
        # Pre-seed both verdicts as genuine (simulating a prior pristine run)
        ws.verdict_put(ws._title_key(m1["title"]), "Amazing Spider-Man #129", True, db_path=db_path)
        ws.verdict_put(ws._title_key(m2["title"]), "X-Men #94", True, db_path=db_path)
        wish_list, wish_items = _two_wish_items()

        _, _exit, mock_verify, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[],
        )

        mock_verify.assert_not_called()


# ─── BUI-226: era-gate in match_results_for_wish ─────────────────────────────

class TestMatchResultsForWishEraGate:
    """match_results_for_wish drops era-mismatched listings via era_mismatch,
    but keeps titles with no high-precision signal (fail-open)."""

    def _wish_item(
        self,
        series: str = "Amazing Spider-Man",
        issue: str = "7",
        series_name: "str | None" = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
    ) -> dict:
        return {
            "id": "w1",
            "name": f"{series} #{issue}",
            "series": series,
            "issue": issue,
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": series_name,
            "_release_year": "1964",
        }

    def _result(self, title: str, item_id: str = "1") -> dict:
        return {
            "title": title,
            "item_id": item_id,
            "seller": "comicseller",
            "current_price": "$10.00",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/" + item_id,
        }

    def test_era_mismatched_title_dropped(self):
        """Listing title "(2022)" for a 1963-range wish item is rejected."""
        wish = self._wish_item()
        results = [self._result("Amazing Spider-Man (2022) #7", "1")]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "era-mismatched listing should be dropped"

    def test_plain_title_no_year_kept(self):
        """Listing with no parenthesized year passes the era gate (fail-open)."""
        wish = self._wish_item()
        results = [self._result("Amazing Spider-Man #7 VF", "2")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "plain title with no year should pass era gate"

    def test_in_era_paren_year_kept(self):
        """Listing with (1964) for a 1963-1998 range passes the era gate."""
        wish = self._wish_item()
        results = [self._result("Amazing Spider-Man (1964) #7 FN", "3")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "in-range paren year should pass"

    def test_no_series_name_on_wish_fail_open(self):
        """If wish has _series_name=None the era gate fails open (no signal)."""
        wish = self._wish_item(series_name=None)
        results = [self._result("Amazing Spider-Man (2022) #7", "4")]
        matches = ws.match_results_for_wish(results, wish)
        # era_mismatch returns False when series_name is None → listing reaches
        # match_listing, which may or may not match; we only assert no crash.
        assert isinstance(matches, list)


# ─── BUI-227: match_results_for_wish era + reprint + _series_name ─────────────


class TestMatchResultsForWishBui227:
    """BUI-227 additions: compound era reject, reprint reject, _series_name propagation."""

    def _wish_item_1963(
        self,
        series: str = "Amazing Spider-Man",
        issue: str = "7",
    ) -> dict:
        return {
            "id": "w1",
            "name": f"{series} #{issue}",
            "series": series,
            "issue": issue,
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
            "_release_year": "1964",
        }

    def _result(self, title: str, item_id: str = "1") -> dict:
        return {
            "title": title,
            "item_id": item_id,
            "seller": "comicseller",
            "current_price": "$10.00",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/" + item_id,
        }

    def test_compound_paren_out_of_era_dropped(self):
        """'(Marvel Comics December 2014)' for a 1963-range wish is rejected."""
        wish = self._wish_item_1963()
        results = [self._result(
            "Amazing Spider-Man #7 (Marvel Comics December 2014)", "1"
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "compound out-of-era paren should be rejected"

    def test_compound_paren_in_era_kept(self):
        """BUI-240: '(Marvel Comics October 1984)' for an ASM #7 wish (release_year=1964)
        is NOW REJECTED by the per-issue-year gate.

        Previously this listing was kept because 1984 falls within the series-range
        [1962, 1999] (±1 of 1963-1998).  With the tighter per-issue-year check
        (iy=1964, acceptance band [1963, 1965]), 1984 is outside the band and the
        listing is correctly rejected — a 1963/1964 issue would not appear in a
        1984 compound parenthetical on a genuine first-print copy.
        """
        wish = self._wish_item_1963(issue="7")
        results = [self._result(
            "Amazing Spider-Man #7 (Marvel Comics October 1984)", "2"
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], (
            "BUI-240: 1984 compound paren for a 1964 wish is rejected by per-issue-year gate"
        )

    def test_facsimile_listing_dropped(self):
        """A facsimile listing is rejected by _reprint_reject before match_listing."""
        wish = self._wish_item_1963()
        results = [self._result("Amazing Spider-Man #7 Facsimile Edition", "3")]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "facsimile listing should be dropped"

    def test_tpb_listing_dropped(self):
        """A TPB listing is rejected by _reprint_reject."""
        wish = self._wish_item_1963()
        results = [self._result("Amazing Spider-Man TPB vol 1", "4")]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "tpb listing should be dropped"

    def test_omnibus_listing_dropped(self):
        """An omnibus listing is rejected by _reprint_reject."""
        wish = self._wish_item_1963()
        results = [self._result("Amazing Spider-Man Omnibus vol 1 HC", "5")]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "omnibus listing should be dropped"

    def test_in_era_plain_listing_kept(self):
        """A normal in-era listing passes both era and reprint gates."""
        wish = self._wish_item_1963()
        results = [self._result("Amazing Spider-Man #7 VF Marvel 1964", "6")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "plain in-era listing should be kept"

    def test_digital_code_only_listing_dropped(self):
        """BUI-230: a 'DIGITAL CODE ONLY [NO PHYSICAL COMIC BOOK]' listing is dropped."""
        wish = self._wish_item_1963()
        results = [self._result(
            "Amazing Spider-Man #7 DIGITAL CODE ONLY!!! [NO PHYSICAL COMIC BOOK]", "9"
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "digital-code-only listing should be dropped"

    def test_with_digital_code_bonus_listing_kept(self):
        """BUI-230: a physical comic sold WITH a digital code bonus is NOT dropped."""
        wish = self._wish_item_1963()
        results = [self._result("Amazing Spider-Man #7 VF Marvel 1964 with Digital Code", "10")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "physical comic with bonus digital code should be kept"

    def test_series_name_propagated_to_match_dict(self):
        """Emitted match dicts carry _series_name from the wish item."""
        wish = self._wish_item_1963()
        results = [self._result("Amazing Spider-Man #7 VF", "7")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1
        assert matches[0]["_series_name"] == "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"

    def test_series_name_none_propagated(self):
        """If the wish item has _series_name=None, match dicts carry None."""
        wish = {
            "id": "w2",
            "name": "Amazing Spider-Man #7",
            "series": "Amazing Spider-Man",
            "issue": "7",
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": None,
        }
        results = [self._result("Amazing Spider-Man #7 VF", "8")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1
        assert matches[0]["_series_name"] is None

    def test_series_name_stripped_from_json_output(self, tmp_path):
        """_series_name (a private field) is not present in --json output."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="1", wish_name="Amazing Spider-Man #129")
        m1["_series_name"] = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
        m2 = make_match(seller="S", item_id="2", wish_name="X-Men #94")
        m2["_series_name"] = None
        wish_list, wish_items = _two_wish_items()

        output, _exit, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
            json_output=True,
        )
        data = json.loads(output)
        for match in data["sellers"][0]["matches"]:
            assert "_series_name" not in match


# ─── BUI-232: trading-card reject in match_results_for_wish ─────────────────


class TestMatchResultsForWishTradingCardReject:
    """BUI-232: Fleer / Topps / MTG listings are dropped before match_listing."""

    def _wish_item(
        self,
        series: str = "Amazing Spider-Man",
        issue: str = "4",
    ) -> dict:
        return {
            "id": "w1",
            "name": f"{series} #{issue}",
            "series": series,
            "issue": issue,
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
            "_release_year": "1964",
        }

    def _result(self, title: str, item_id: str = "1") -> dict:
        return {
            "title": title,
            "item_id": item_id,
            "seller": "dcsports87",
            "current_price": "$5.00",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/" + item_id,
        }

    def test_fleer_card_dropped(self):
        """The confirmed dcsports87 Fleer card false positive is rejected."""
        wish = self._wish_item()
        results = [self._result(
            "1994 Fleer The Amazing Spider-Man Venom Suspended Animation #4", "1"
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "Fleer trading card should be dropped"

    def test_normal_in_era_comic_kept(self):
        """A normal in-era comic listing for the same wish item is NOT dropped."""
        wish = self._wish_item()
        results = [self._result("Amazing Spider-Man #4 VF Marvel 1963", "2")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "genuine comic listing should be kept"


# ─── BUI-239: foreign-edition reject in match_results_for_wish ───────────────


class TestMatchResultsForWishForeignEditionReject:
    """BUI-239: La Prensa / foreign-edition listings are dropped before match_listing."""

    def _wish_item(
        self,
        series: str = "Amazing Spider-Man",
        issue: str = "4",
    ) -> dict:
        return {
            "id": "w1",
            "name": f"{series} #{issue}",
            "series": series,
            "issue": issue,
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
            "_release_year": "1963",
        }

    def _result(self, title: str, item_id: str = "1") -> dict:
        return {
            "title": title,
            "item_id": item_id,
            "seller": "la_prensa_seller",
            "current_price": "$349.99",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/" + item_id,
        }

    def test_la_prensa_listing_dropped(self):
        """BUI-239 repro: La Prensa listing is dropped from match results."""
        wish = self._wish_item(issue="4")
        results = [self._result(
            "AMAZING SPIDER-MAN #4 VARIANT 1963 Sandman First appearance mexican la prensa",
            "1",
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "La Prensa foreign-edition listing should be dropped"

    def test_edicion_listing_dropped(self):
        """BUI-239 repro: 'EDICION mexican la prensa' title is dropped."""
        wish = self._wish_item(issue="10")
        results = [self._result(
            "AMAZING SPIDER-MAN #10 VARIANT 1963 EDICION mexican la prensa SPANISH",
            "2",
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "EDICION foreign-edition listing should be dropped"

    def test_genuine_us_copy_kept(self):
        """A genuine US copy with no foreign-edition markers is NOT dropped."""
        wish = self._wish_item(issue="4")
        results = [self._result("Amazing Spider-Man #4 1963 Sandman 1st appearance VF", "3")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "genuine US copy should be kept"


# ─── BUI-244: later-printing reject in match_results_for_wish ────────────────


class TestMatchResultsForWishSecondPrintReject:
    """BUI-244: later-printing listings (second print, reprint) are dropped
    before match_listing, mirroring TestMatchResultsForWishBui227."""

    def _wish_item(
        self,
        series: str = "Batman: Vengeance of Bane",
        issue: str = "1",
    ) -> dict:
        return {
            "id": "w1",
            "name": f"{series} #{issue}",
            "series": series,
            "issue": issue,
            "_tokens": ["batman", "vengeance", "bane"],
            "_series_name": "Batman: Vengeance of Bane (1993)",
            "_release_year": "1993",
        }

    def _result(self, title: str, item_id: str = "1") -> dict:
        return {
            "title": title,
            "item_id": item_id,
            "seller": "dc_seller",
            "current_price": "$49.99",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/" + item_id,
        }

    def test_vengeance_of_bane_second_print_dropped(self):
        """BUI-244 repro: the reported second-print listing is dropped."""
        wish = self._wish_item()
        results = [self._result(
            "Batman Vengeance Of Bane #1 1993 II DC comics Second Print 1st Appearance Smith",
            "1",
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "Second Print listing should be dropped by _second_print_reject"

    def test_2nd_printing_dropped(self):
        """'2nd printing' listing is rejected before match_listing."""
        wish = self._wish_item()
        results = [self._result("Batman Vengeance of Bane #1 2nd printing", "2")]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "2nd printing listing should be dropped"

    def test_reprint_listing_dropped(self):
        """'reprint' listing is rejected before match_listing."""
        wish = self._wish_item()
        results = [self._result("Batman Vengeance of Bane #1 reprint 1993 NM", "3")]
        matches = ws.match_results_for_wish(results, wish)
        assert matches == [], "reprint listing should be dropped"

    def test_genuine_first_print_kept(self):
        """A genuine first-print listing is NOT dropped by _second_print_reject."""
        wish = self._wish_item()
        results = [self._result("Batman Vengeance of Bane #1 1993 NM 1st Bane", "4")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "genuine first-print listing should be kept"

    def test_newsstand_copy_kept(self):
        """A newsstand copy is NOT dropped (original distribution variant)."""
        wish = self._wish_item(series="Amazing Spider-Man", issue="129")
        wish["_tokens"] = ["amazing", "spider", "man"]
        wish["_series_name"] = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
        wish["_release_year"] = "1974"
        results = [self._result("Amazing Spider-Man #129 newsstand VF 1st Punisher", "5")]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "newsstand copy should be kept"


# ─── BUI-244: pristine-match token cleanup ────────────────────────────────────


class TestIsPristineMatchBui244:
    """BUI-244: after removing '2nd', '3rd', 'print', 'printing', 'second',
    'third' from _PRISTINE_PRINT_TOKENS, a title like 'X-Men #1 2nd Print'
    must NOT qualify as pristine."""

    def test_2nd_print_not_pristine(self):
        """'X-Men #1 2nd Print' — '2nd' and 'print' no longer in allow-list → not pristine."""
        m = make_match(
            title="X-Men #1 2nd Print",
            series="X-Men",
            issue="1",
            score=1.0,
            wish_name="X-Men #1",
        )
        assert ws.is_pristine_match(m) is False

    def test_second_print_not_pristine(self):
        """'Amazing Spider-Man #300 second print' — 'second' and 'print' removed → not pristine."""
        m = make_match(
            title="Amazing Spider-Man #300 second print",
            series="Amazing Spider-Man",
            issue="300",
            score=1.0,
            wish_name="Amazing Spider-Man #300",
        )
        assert ws.is_pristine_match(m) is False

    def test_first_print_still_pristine(self):
        """'Amazing Spider-Man #300 1st print' — '1st' is kept → still pristine."""
        m = make_match(
            title="Amazing Spider-Man #300 1st",
            series="Amazing Spider-Man",
            issue="300",
            score=1.0,
            wish_name="Amazing Spider-Man #300",
        )
        assert ws.is_pristine_match(m) is True

    def test_newsstand_still_pristine(self):
        """'Amazing Spider-Man #129 VF newsstand' — 'newsstand' is kept → still pristine."""
        m = make_match(
            title="Amazing Spider-Man #129 VF newsstand",
            series="Amazing Spider-Man",
            issue="129",
            score=1.0,
            wish_name="Amazing Spider-Man #129",
        )
        assert ws.is_pristine_match(m) is True

    def test_direct_still_pristine(self):
        """'X-Men #94 NM direct' — 'direct' is kept → still pristine."""
        m = make_match(
            title="X-Men #94 NM direct",
            series="X-Men",
            issue="94",
            score=1.0,
            wish_name="X-Men #94",
        )
        assert ws.is_pristine_match(m) is True


# ─── BUI-229: item-specifics era gate ────────────────────────────────────────


def _make_bare_title_match_with_series_name(
    seller: str = "sellerA",
    item_id: str = "500",
    wish_name: str = "Amazing Spider-Man #7",
    title: str = "Amazing Spider-Man #7 VF",
    series: str = "Amazing Spider-Man",
    issue: str = "7",
    series_name: "str | None" = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
    release_year: "str | None" = None,
) -> dict:
    """Build a bare-title match (no paren year, no vol) with _series_name set.

    This is the class of match that qualifies for an item-specifics lookup in the
    BUI-229 gate: _title_paren_years(title)==[] and _title_volume(title) is None
    and _series_name is truthy.
    """
    m = make_match(
        seller=seller,
        item_id=item_id,
        wish_name=wish_name,
        title=title,
        series=series,
        issue=issue,
    )
    m["_series_name"] = series_name
    m["_release_year"] = release_year
    return m


class TestItemSpecificsGate:
    """BUI-229: item-specifics Publication Year gate for bare-title residual."""

    def test_bare_title_out_of_era_pub_year_dropped(self, tmp_path):
        """A bare-title match with Publication Year=2014 for a 1963-range wish is dropped."""
        db_path = tmp_path / "v.db"
        # m1: bare title, Publication Year=2014 → outside 1963-1998 range → dropped
        m1 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="501", wish_name="Amazing Spider-Man #7",
        )
        # m2: second match so seller passes the ≥2 gate
        m2 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="502", wish_name="X-Men #94",
            title="X-Men #94 VF",
            series="X-Men", issue="94",
            series_name="X-Men (Vol. 1) (1963 - 1981)",
        )

        mock_aspects = MagicMock(
            side_effect=lambda item_id, tok, base_url: (
                {"Publication Year": "2014"} if item_id == "501"
                else {"Publication Year": "1977"}  # in-era for X-Men 1963-1981
            )
        )
        mock_verify = MagicMock(return_value=([m2], [], []))
        wish_list, wish_items = _two_wish_items()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "get_item_aspects", mock_aspects),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ws.main([])

        # m1 dropped by item-specifics gate → sellerA has only m2 → below ≥2
        output = buf.getvalue()
        assert "sellerA" not in output
        assert "No sellers" in output

    def test_bare_title_in_era_pub_year_kept(self, tmp_path):
        """A bare-title match with an in-era Publication Year is kept."""
        db_path = tmp_path / "v.db"
        m1 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="601", wish_name="Amazing Spider-Man #7",
        )
        m2 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="602", wish_name="X-Men #94",
            title="X-Men #94 VF",
            series="X-Men", issue="94",
            series_name="X-Men (Vol. 1) (1963 - 1981)",
        )

        # Both in-era: ASM 1963-1998 → pub year 1964; X-Men 1963-1981 → pub year 1977
        mock_aspects = MagicMock(
            side_effect=lambda item_id, tok, base_url: (
                {"Publication Year": "1964"} if item_id == "601"
                else {"Publication Year": "1977"}
            )
        )
        mock_verify = MagicMock(return_value=([m1, m2], [], []))
        wish_list, wish_items = _two_wish_items()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "get_item_aspects", mock_aspects),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ws.main([])

        output = buf.getvalue()
        # Both matches survived → sellerA has ≥2 → appears in output
        assert "sellerA" in output

    def test_paren_year_title_not_looked_up(self, tmp_path):
        """A match whose title already has a parenthesized year is NOT sent to get_item_aspects."""
        db_path = tmp_path / "v.db"
        # Title has "(1964)" — _title_paren_years returns [1964] → not bare-title
        m1 = make_match(
            seller="sellerA", item_id="701", wish_name="Amazing Spider-Man #7",
            title="Amazing Spider-Man (1964) #7 VF",
        )
        m1["_series_name"] = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
        m2 = make_match(
            seller="sellerA", item_id="702", wish_name="X-Men #94",
            title="X-Men (1977) #94 FN",
        )
        m2["_series_name"] = None
        wish_list, wish_items = _two_wish_items()
        mock_aspects = MagicMock()
        mock_verify = MagicMock(return_value=([m1, m2], [], []))

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "get_item_aspects", mock_aspects),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        # get_item_aspects must not have been called for either match (both have paren years)
        mock_aspects.assert_not_called()

    def test_no_item_specifics_flag_disables_gate(self, tmp_path):
        """--no-item-specifics prevents get_item_aspects from being called at all."""
        db_path = tmp_path / "v.db"
        # Bare-title match with _series_name set → would normally trigger the gate
        m1 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="801", wish_name="Amazing Spider-Man #7",
        )
        m2 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="802", wish_name="X-Men #94",
            title="X-Men #94 VF", series="X-Men", issue="94",
            series_name="X-Men (Vol. 1) (1963 - 1981)",
        )
        wish_list, wish_items = _two_wish_items()
        mock_aspects = MagicMock()
        mock_verify = MagicMock(return_value=([m1, m2], [], []))

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "get_item_aspects", mock_aspects),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main(["--no-item-specifics"])

        mock_aspects.assert_not_called()

    def test_fail_open_on_none_aspects(self, tmp_path):
        """get_item_aspects returning None (fetch error) keeps the listing."""
        db_path = tmp_path / "v.db"
        m1 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="901", wish_name="Amazing Spider-Man #7",
        )
        m2 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="902", wish_name="X-Men #94",
            title="X-Men #94 VF", series="X-Men", issue="94",
            series_name="X-Men (Vol. 1) (1963 - 1981)",
        )
        wish_list, wish_items = _two_wish_items()
        mock_verify = MagicMock(return_value=([m1, m2], [], []))

        output, _exit, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
        )
        # _run_main patches get_item_aspects to return None → fail-open → both kept
        assert "sellerA" in output

    def test_singleton_seller_skips_item_specifics_gate(self, tmp_path):
        """get_item_aspects is NOT called for a singleton seller's bare-title listing
        because group_and_gate drops that seller before the item-specifics gate runs.
        It IS called for bare-title listings belonging to a qualifying (≥2) seller.
        """
        db_path = tmp_path / "v.db"
        # sellerA: 2 bare-title matches → passes group_and_gate → item-specifics gate runs
        m1 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="5001", wish_name="Amazing Spider-Man #7",
        )
        m2 = _make_bare_title_match_with_series_name(
            seller="sellerA", item_id="5002", wish_name="X-Men #94",
            title="X-Men #94 VF", series="X-Men", issue="94",
            series_name="X-Men (Vol. 1) (1963 - 1981)",
        )
        # sellerB: only 1 bare-title match → singleton, dropped by group_and_gate
        m3 = _make_bare_title_match_with_series_name(
            seller="sellerB", item_id="5003", wish_name="Amazing Spider-Man #7",
        )
        wish_list, wish_items = _two_wish_items()
        # in-era pub year for both sellerA matches so they survive the gate
        mock_aspects = MagicMock(return_value={"Publication Year": "1964"})
        mock_verify = MagicMock(return_value=([m1, m2], [], []))

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            # First wish (ASM #7) yields m1 (sellerA) + m3 (sellerB); second yields m2 (sellerA)
            patch.object(ws, "match_results_for_wish", side_effect=[[m1, m3], [m2]]),
            patch.object(ws, "get_item_aspects", mock_aspects),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", mock_verify),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        called_ids = {call[0][0] for call in mock_aspects.call_args_list}
        # sellerB's singleton (5003) must NOT reach get_item_aspects — dropped by group_and_gate
        assert "5003" not in called_ids, (
            "singleton seller's listing must not reach the item-specifics gate"
        )
        # sellerA's bare-title matches are in the ≥2 group — at least one was checked
        assert called_ids & {"5001", "5002"}, (
            "≥2 seller's bare-title matches must be checked by the item-specifics gate"
        )


# ─── BUI-231: era fallback by issue year ─────────────────────────────────────


class TestMatchResultsForWishReleaseYear:
    """match_results_for_wish propagates _release_year onto the match dict (BUI-231)."""

    def _wish_item(self) -> dict:
        return {
            "id": "w1",
            "name": "Amazing Spider-Man #7",
            "series": "Amazing Spider-Man",
            "issue": "7",
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
            "_release_year": "1964",
        }

    def _result(self, title: str = "Amazing Spider-Man #7 VF") -> dict:
        return {
            "title": title,
            "item_id": "1",
            "seller": "comicseller",
            "current_price": "$10.00",
            "end_date": "2026-07-01",
            "end_date_iso": "2026-07-01T12:00:00Z",
            "listing_url": "https://www.ebay.com/itm/1",
        }

    def test_release_year_carried_on_match_dict(self):
        """_release_year from the wish item is propagated into the match dict."""
        matches = ws.match_results_for_wish([self._result()], self._wish_item())
        assert len(matches) == 1
        assert matches[0].get("_release_year") == "1964"

    def test_release_year_none_when_wish_has_no_release_year(self):
        """_release_year is None in the match dict when the wish item has no release year."""
        wish = self._wish_item()
        wish["_release_year"] = None
        matches = ws.match_results_for_wish([self._result()], wish)
        assert len(matches) == 1
        assert matches[0].get("_release_year") is None


class TestItemSpecificsEraFallbackByIssueYear:
    """BUI-231: item-specifics Era fallback fires on per-issue release year, not series-end."""

    def test_era_modern_issue_year_pre1992_drops_listing(self, tmp_path):
        """Bare-title listing with Era=Modern Age is dropped when _release_year='1964'.

        ASM Vol.1 ran 1963-1998 so the old series-end gate (end=1998 ≥ 1992) would
        NOT fire.  The per-issue year (1964 < 1992) correctly rejects the listing.
        Also asserts _release_year is carried on the match dict and passed to
        publication_year_mismatch via the Step 7.5 call.
        """
        db_path = tmp_path / "v.db"
        # m1: 1964 wish + bare title → Era=Modern Age → dropped by issue-year gate
        m1 = _make_bare_title_match_with_series_name(
            seller="sellerA",
            item_id="1001",
            wish_name="Amazing Spider-Man #7",
            release_year="1964",
        )
        # m2: title with explicit paren year → bypasses item-specifics gate entirely
        m2 = make_match(
            seller="sellerA",
            item_id="1002",
            wish_name="X-Men #94",
            title="X-Men (1977) #94 FN",
        )
        m2["_series_name"] = "X-Men (Vol. 1) (1963 - 1981)"
        m2["_release_year"] = "1977"

        # Assert _release_year is carried on the match dict before passing to main
        assert m1.get("_release_year") == "1964"

        wish_list, wish_items = _two_wish_items()
        mock_aspects = MagicMock(return_value={"Era": "Modern Age (1992-Now)"})

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", side_effect=[[m1], [m2]]),
            patch.object(ws, "get_item_aspects", mock_aspects),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", MagicMock(return_value=([], [], []))),
            patch.object(ws, "record_items_seen"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ws.main([])

        # m1 (1964 wish + Era=Modern Age) dropped → sellerA has only m2 (paren-year
        # title bypasses gate) → count=1 < 2 → no sellers in output.
        output = buf.getvalue()
        assert "sellerA" not in output
        assert "No sellers" in output

        # get_item_aspects called exactly once — only for m1 (bare title); m2
        # (paren year) is excluded from the gate check.
        mock_aspects.assert_called_once()


# ─── BUI-242: keyword fan-out deduplication ───────────────────────────────────


class TestKeywordDedup:
    """BUI-242: multi-volume wish items that share a keyword get ONE eBay search."""

    def _make_avengers_v1(self) -> dict:
        return {
            "id": "w1", "name": "The Avengers #1",
            "series": "The Avengers", "issue": "1",
            "_tokens": ["avengers"],
            "_series_name": "The Avengers (Vol. 1) (1963 - 1996)",
            "_release_year": "1963",
        }

    def _make_avengers_v8(self) -> dict:
        return {
            "id": "w2", "name": "The Avengers #1",
            "series": "The Avengers", "issue": "1",
            "_tokens": ["avengers"],
            "_series_name": "The Avengers (Vol. 8) (2018 - 2023)",
            "_release_year": "2018",
        }

    def _base_patches(self, tmp_path):
        """Return the common context-manager patches for BUI-242 tests."""
        return {
            "load_config": ("id", "sec", "https://api.ebay.com"),
        }

    def test_identical_keyword_produces_one_search(self, tmp_path):
        """Two wish items with the same series+issue produce exactly ONE eBay search.

        BUI-242 regression: before the fix, two Avengers #1 wish items (different
        volumes) each triggered a separate search_by_keyword call, burning a --limit
        slot and double-logging 'Searching eBay: The Avengers #1'.
        """
        db_path = tmp_path / "v.db"
        avengers_v1 = self._make_avengers_v1()
        avengers_v8 = self._make_avengers_v8()
        wish_list = [
            {"id": "w1", "name": "The Avengers #1"},
            {"id": "w2", "name": "The Avengers #1"},
        ]
        wish_items = [avengers_v1, avengers_v8]

        mock_search = MagicMock(return_value=[])
        mock_cache_get = MagicMock(return_value=None)

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", mock_cache_get),
            patch("wishlist_sellers.search_by_keyword", mock_search),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", return_value=[]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", return_value=([], [], [])),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        # BUI-242: only ONE search for the shared keyword
        mock_search.assert_called_once()
        assert mock_search.call_args[0][0] == "The Avengers #1"
        # Only ONE cache lookup for the shared keyword
        mock_cache_get.assert_called_once()
        assert mock_cache_get.call_args[0][0] == "The Avengers #1"

    def test_identical_keyword_cache_hit_produces_one_cache_get(self, tmp_path):
        """A warm cache for two same-keyword wish items → one cache.get call, not two."""
        db_path = tmp_path / "v.db"
        avengers_v1 = self._make_avengers_v1()
        avengers_v8 = self._make_avengers_v8()
        wish_list = [
            {"id": "w1", "name": "The Avengers #1"},
            {"id": "w2", "name": "The Avengers #1"},
        ]
        wish_items = [avengers_v1, avengers_v8]

        # Return cached results on get → no search_by_keyword call
        mock_cache_get = MagicMock(return_value=[])
        mock_search = MagicMock()

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", mock_cache_get),
            patch("wishlist_sellers.search_by_keyword", mock_search),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", return_value=[]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", return_value=([], [], [])),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        # Only ONE cache lookup (not one per wish item)
        mock_cache_get.assert_called_once()
        # No search needed — cache hit
        mock_search.assert_not_called()

    def test_identical_keyword_both_wish_items_run_through_match(self, tmp_path):
        """Two wish items sharing a keyword are BOTH run through match_results_for_wish.

        BUI-242: the shared eBay results must be matched against each wish item's
        distinct era gates (e.g. 1963-era vs 2018-era Avengers #1).
        """
        db_path = tmp_path / "v.db"
        avengers_v1 = self._make_avengers_v1()
        avengers_v8 = self._make_avengers_v8()
        wish_list = [
            {"id": "w1", "name": "The Avengers #1"},
            {"id": "w2", "name": "The Avengers #1"},
        ]
        wish_items = [avengers_v1, avengers_v8]

        mock_match = MagicMock(return_value=[])

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", return_value=[]),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", mock_match),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", return_value=([], [], [])),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main([])

        # BUI-242: match_results_for_wish called TWICE — once per wish item
        assert mock_match.call_count == 2
        called_wish_items = [call[0][1] for call in mock_match.call_args_list]
        assert avengers_v1 in called_wish_items
        assert avengers_v8 in called_wish_items

    def test_limit_bounds_distinct_keywords_not_raw_items(self, tmp_path):
        """--limit N processes N distinct search keywords, not N raw wish items.

        BUI-242: with wish items producing keywords [Avengers #1, Avengers #1,
        X-Men #1, Thor #1] (3 distinct), --limit 2 runs exactly 2 eBay searches
        (Avengers #1 and X-Men #1), leaving Thor #1 unsearched.
        """
        db_path = tmp_path / "v.db"
        # Two Avengers #1 (same keyword), one X-Men #1, one Thor #1
        avengers_v1 = self._make_avengers_v1()
        avengers_v8 = self._make_avengers_v8()
        xmen = {
            "id": "w3", "name": "X-Men #1",
            "series": "X-Men", "issue": "1",
            "_tokens": ["xmen"],
            "_series_name": None, "_release_year": None,
        }
        thor = {
            "id": "w4", "name": "Thor #1",
            "series": "Thor", "issue": "1",
            "_tokens": ["thor"],
            "_series_name": None, "_release_year": None,
        }
        wish_list = [{"id": x, "name": "..."} for x in ["w1", "w2", "w3", "w4"]]
        wish_items = [avengers_v1, avengers_v8, xmen, thor]

        mock_search = MagicMock(return_value=[])

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", mock_search),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", return_value=[]),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", return_value=([], [], [])),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main(["--limit", "2"])

        # BUI-242: --limit 2 → only 2 distinct keyword searches
        assert mock_search.call_count == 2
        searched_keywords = [call[0][0] for call in mock_search.call_args_list]
        assert "The Avengers #1" in searched_keywords
        assert "X-Men #1" in searched_keywords
        assert "Thor #1" not in searched_keywords

    def test_limit_one_with_duplicate_keyword_runs_both_items(self, tmp_path):
        """--limit 1 where the only keyword is shared by two wish items → both
        items are run through match_results_for_wish (1 search, 2 match calls)."""
        db_path = tmp_path / "v.db"
        avengers_v1 = self._make_avengers_v1()
        avengers_v8 = self._make_avengers_v8()
        wish_list = [
            {"id": "w1", "name": "The Avengers #1"},
            {"id": "w2", "name": "The Avengers #1"},
        ]
        wish_items = [avengers_v1, avengers_v8]

        mock_search = MagicMock(return_value=[])
        mock_match = MagicMock(return_value=[])

        with (
            patch.object(ws, "fetch_wish_list", return_value=wish_list),
            patch.object(ws, "prepare_wish_items", return_value=wish_items),
            patch("wishlist_sellers.load_config", return_value=("id", "sec", "https://api.ebay.com")),
            patch("wishlist_sellers.get_token", return_value="tok"),
            patch("wishlist_sellers.ebay_search_cache.get", return_value=None),
            patch("wishlist_sellers.search_by_keyword", mock_search),
            patch("wishlist_sellers.ebay_search_cache.put"),
            patch("wishlist_sellers.ebay_search_cache.filter_active", return_value=[]),
            patch.object(ws, "match_results_for_wish", mock_match),
            patch.object(ws, "fetch_seen_item_ids", return_value=set()),
            patch.object(ws, "_server_base", return_value=""),
            patch.object(ws, "verdict_db_path", return_value=db_path),
            patch.object(ws, "verify_with_claude", return_value=([], [], [])),
            patch.object(ws, "record_items_seen"),
        ):
            ws.main(["--limit", "1"])

        # 1 unique keyword → 1 search
        mock_search.assert_called_once()
        # But BOTH wish items run through match (the keyword covers both volumes)
        assert mock_match.call_count == 2
