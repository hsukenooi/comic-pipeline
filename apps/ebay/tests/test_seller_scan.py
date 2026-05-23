"""Tests for seller_scan and the ebay_fetch seller search functions."""

import json
from unittest.mock import MagicMock, patch

import pytest

import ebay_fetch
import seller_scan


# ─── ebay_fetch additions ─────────────────────────────────────────────────────


class TestExtractSellerUsername:
    def test_raw_username(self):
        assert ebay_fetch._extract_seller_username("beatlebluecat") == "beatlebluecat"

    def test_user_url(self):
        assert ebay_fetch._extract_seller_username(
            "https://www.ebay.com/usr/beatlebluecat"
        ) == "beatlebluecat"

    def test_store_url(self):
        assert ebay_fetch._extract_seller_username(
            "https://www.ebay.com/str/beatlebluecat"
        ) == "beatlebluecat"

    def test_url_with_query_params(self):
        assert ebay_fetch._extract_seller_username(
            "https://www.ebay.com/usr/beatlebluecat?page=1"
        ) == "beatlebluecat"

    def test_strips_whitespace(self):
        assert ebay_fetch._extract_seller_username("  beatlebluecat  ") == "beatlebluecat"


class TestParseItemSummary:
    def _make_auction(self, **overrides):
        base = {
            "itemId": "v1|298217294954|0",
            "title": "Amazing Spider-Man #300 NM Marvel 1988",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "150.00", "currency": "USD"},
            "itemEndDate": "2026-05-28T12:00:00.000Z",
            "itemWebUrl": "https://www.ebay.com/itm/298217294954",
            "seller": {"username": "beatlebluecat"},
        }
        base.update(overrides)
        return base

    def test_auction_uses_currentBidPrice(self):
        item = self._make_auction()
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["listing_type"] == "Auction"
        assert parsed["current_price"] == "$150.00"

    def test_bin_uses_price(self):
        item = self._make_auction(
            buyingOptions=["FIXED_PRICE"],
            price={"value": "299.99", "currency": "USD"},
        )
        del item["currentBidPrice"]
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["listing_type"] == "BIN"
        assert parsed["current_price"] == "$299.99"

    def test_strips_item_id_wrapper(self):
        item = self._make_auction()
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["item_id"] == "298217294954"

    def test_raw_item_id_fallback(self):
        item = self._make_auction(itemId="298217294954")
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["item_id"] == "298217294954"

    def test_seller_username_extracted(self):
        item = self._make_auction()
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["seller"] == "beatlebluecat"

    def test_listing_url_from_itemWebUrl(self):
        item = self._make_auction()
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["listing_url"] == "https://www.ebay.com/itm/298217294954"

    def test_listing_url_fallback(self):
        item = self._make_auction()
        del item["itemWebUrl"]
        parsed = ebay_fetch.parse_item_summary(item)
        assert "298217294954" in parsed["listing_url"]

    def test_end_date_formatted(self):
        item = self._make_auction()
        parsed = ebay_fetch.parse_item_summary(item)
        assert parsed["end_date"] is not None
        assert parsed["end_date_iso"] == "2026-05-28T12:00:00.000Z"

    def test_unknown_buying_option(self):
        item = self._make_auction(buyingOptions=["BEST_OFFER"])
        parsed = ebay_fetch.parse_item_summary(item)
        assert "BEST_OFFER" in parsed["listing_type"]


class TestSearchSellerListings:
    def _mock_page(self, items, total):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"itemSummaries": items, "total": total}
        return resp

    @patch("ebay_fetch.requests.get")
    def test_single_page(self, mock_get):
        items = [{"itemId": "v1|1|0", "title": "Comic #1"}]
        mock_get.return_value = self._mock_page(items, 1)
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert len(result) == 1
        assert result[0]["title"] == "Comic #1"

    @patch("ebay_fetch.requests.get")
    def test_paginates(self, mock_get):
        page1 = [{"itemId": f"v1|{i}|0"} for i in range(200)]
        page2 = [{"itemId": "v1|200|0"}]
        mock_get.side_effect = [
            self._mock_page(page1, 201),
            self._mock_page(page2, 201),
        ]
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert len(result) == 201

    @patch("ebay_fetch.requests.get")
    def test_stops_at_max_results(self, mock_get):
        page1 = [{"itemId": f"v1|{i}|0"} for i in range(200)]
        mock_get.return_value = self._mock_page(page1, 500)
        result = ebay_fetch.search_seller_listings(
            "seller", "tok", "https://api.ebay.com", max_results=200
        )
        assert len(result) == 200
        assert mock_get.call_count == 1

    @patch("ebay_fetch.requests.get")
    def test_empty_seller(self, mock_get):
        mock_get.return_value = self._mock_page([], 0)
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert result == []

    @patch("ebay_fetch.requests.get")
    def test_username_extracted_from_url(self, mock_get):
        mock_get.return_value = self._mock_page([], 0)
        ebay_fetch.search_seller_listings(
            "https://www.ebay.com/usr/beatlebluecat", "tok", "https://api.ebay.com"
        )
        call_params = mock_get.call_args[1]["params"]
        assert "beatlebluecat" in call_params["filter"]


# ─── seller_scan matching ─────────────────────────────────────────────────────


class TestParseWishName:
    def test_standard(self):
        assert seller_scan._parse_wish_name("Amazing Spider-Man #300") == (
            "Amazing Spider-Man", "300"
        )

    def test_no_issue(self):
        series, issue = seller_scan._parse_wish_name("Some Omnibus Edition")
        assert issue is None

    def test_alphanumeric_issue(self):
        series, issue = seller_scan._parse_wish_name("Batman #1A")
        assert issue == "1A"

    def test_strips_whitespace(self):
        series, issue = seller_scan._parse_wish_name("  X-Men #142  ")
        assert series == "X-Men"
        assert issue == "142"


class TestPrepareWishItems:
    def test_parses_items(self):
        wish = [
            {"id": 1, "name": "Amazing Spider-Man #300"},
            {"id": 2, "name": "Fantastic Four #48"},
        ]
        items = seller_scan.prepare_wish_items(wish)
        assert len(items) == 2
        assert items[0]["issue"] == "300"
        assert items[1]["series"] == "Fantastic Four"

    def test_skips_items_without_issue(self):
        wish = [
            {"id": 1, "name": "Amazing Spider-Man #300"},
            {"id": 2, "name": "Some Omnibus"},
        ]
        items = seller_scan.prepare_wish_items(wish)
        assert len(items) == 1

    def test_tokens_exclude_stopwords(self):
        wish = [{"id": 1, "name": "The Amazing Spider-Man #300"}]
        items = seller_scan.prepare_wish_items(wish)
        assert "the" not in items[0]["_tokens"]
        assert "amazing" in items[0]["_tokens"]


class TestMatchListing:
    def _items(self, names):
        return seller_scan.prepare_wish_items(
            [{"id": i, "name": n} for i, n in enumerate(names)]
        )

    def test_exact_match(self):
        items = self._items(["Amazing Spider-Man #300"])
        wish, score = seller_scan.match_listing(
            "AMAZING SPIDER-MAN #300 NM Marvel 1988 VENOM", items
        )
        assert wish is not None
        assert wish["name"] == "Amazing Spider-Man #300"
        assert score >= 0.5

    def test_no_match_wrong_issue(self):
        items = self._items(["Amazing Spider-Man #300"])
        wish, score = seller_scan.match_listing(
            "AMAZING SPIDER-MAN #299 NM Marvel 1988", items
        )
        assert wish is None

    def test_no_match_wrong_series(self):
        items = self._items(["Amazing Spider-Man #300"])
        wish, score = seller_scan.match_listing("Batman #300 NM DC", items)
        assert wish is None

    def test_multiple_items_picks_best(self):
        items = self._items([
            "Amazing Spider-Man #300",
            "Spectacular Spider-Man #300",
        ])
        wish, score = seller_scan.match_listing(
            "AMAZING SPIDER-MAN #300 NM Marvel 1988", items
        )
        assert wish is not None
        assert "Amazing" in wish["name"]

    def test_case_insensitive(self):
        items = self._items(["Fantastic Four #48"])
        wish, _ = seller_scan.match_listing(
            "fantastic four #48 vf silver surfer galactus", items
        )
        assert wish is not None

    def test_issue_without_hash_sign(self):
        items = self._items(["X-Men #1"])
        wish, _ = seller_scan.match_listing("X MEN 1 NM 1963 MARVEL", items)
        assert wish is not None
