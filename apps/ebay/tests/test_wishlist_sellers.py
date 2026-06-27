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
    json_output: bool = False,
    extra_args: list | None = None,
) -> tuple[str, MagicMock, MagicMock]:
    """Run main() with standard mocks; return (stdout, mock_verify, mock_record)."""
    if wish_list is None or wish_items is None:
        wish_list, wish_items = _two_wish_items()
    if seen is None:
        seen = set()
    if verify_return is None:
        # By default verify passes everything through
        verify_return = [m for ms in matches_by_wish for m in ms]

    mock_verify = MagicMock(return_value=verify_return)
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
            ws.main(argv)
        return buf.getvalue(), mock_verify, mock_record


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

        mock_verify = MagicMock(return_value=[m2])
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

        mock_verify = MagicMock(return_value=[m1, m2])
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

        mock_verify = MagicMock(return_value=[m1, m2])
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
        mock_verify = MagicMock()

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
            patch.object(ws, "verify_with_claude", return_value=[m1]),  # only m1 kept
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

        output, _, mock_record = _run_main(
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
        """--json emits valid JSON; private keys _series/_issue are stripped."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="comicseller1", item_id="200",
                        wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="comicseller1", item_id="201", wish_name="X-Men #94")
        wish_list, wish_items = _two_wish_items()

        output, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
            json_output=True,
        )

        data = json.loads(output)
        assert len(data) == 1
        assert data[0]["seller"] == "comicseller1"
        assert len(data[0]["matches"]) == 2
        for match in data[0]["matches"]:
            assert "_series" not in match
            assert "_issue" not in match

    def test_seen_items_dropped_before_output(self, tmp_path):
        """Items already in the global seen set are hidden from the report."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="S", item_id="300", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="S", item_id="301", wish_name="X-Men #94")
        wish_list, wish_items = _two_wish_items()

        output, _, _ = _run_main(
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

        _, _, mock_record = _run_main(
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

        output, _, mock_record = _run_main(
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

        _, _, mock_record = _run_main(
            [[m1], [m2]], db_path=db_path, verify_return=[m1, m2],
        )

        mock_record.assert_called_once()

    def test_limit_processes_only_first_n_wish_items(self, tmp_path):
        """--limit 1 stops after the first wish item, so the same-seller pair
        never both appear → the ≥2 gate drops the lone match."""
        db_path = tmp_path / "v.db"
        m1 = make_match(seller="s1", item_id="100", wish_name="Amazing Spider-Man #129")
        m2 = make_match(seller="s1", item_id="101", wish_name="X-Men #94")

        output, _, _ = _run_main(
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
            patch.object(ws, "verify_with_claude", return_value=[]),
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
        output, _, _ = _run_main([], db_path=db_path, wish_list=wl, wish_items=wi)
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
        output, _, _ = _run_main([variants, []], db_path=db_path,
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

        mock_verify = MagicMock(side_effect=lambda reps: reps)

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

        output, _, _ = _run_main(
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
        mock_verify = MagicMock()

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

    def test_title_year_trailing_is_pristine(self):
        """A bare year after the issue is a pure number → always allowed → pristine."""
        m = make_match(
            title="Amazing Spider-Man #129 1974",
            series="Amazing Spider-Man",
            issue="129",
            score=1.0,
            wish_name="Amazing Spider-Man #129",
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
        output, mock_verify, _ = _run_main(
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

        _, mock_verify, _ = _run_main(
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

        _, mock_verify, _ = _run_main(
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
        """'(Marvel Comics October 1984)' for a 1963-1998 range passes the gate."""
        wish = self._wish_item_1963(issue="7")
        results = [self._result(
            "Amazing Spider-Man #7 (Marvel Comics October 1984)", "2"
        )]
        matches = ws.match_results_for_wish(results, wish)
        assert len(matches) == 1, "compound in-era paren should pass the era gate"

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

        output, _, _ = _run_main(
            [[m1], [m2]],
            db_path=db_path,
            wish_list=wish_list,
            wish_items=wish_items,
            verify_return=[m1, m2],
            json_output=True,
        )
        data = json.loads(output)
        for match in data[0]["matches"]:
            assert "_series_name" not in match
