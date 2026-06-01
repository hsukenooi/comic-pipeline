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


def _finding_item(item_id, title="Comic", listing_type="Chinese", price="10.00"):
    """Build a Finding API item dict (single-element arrays per field)."""
    return {
        "itemId": [item_id],
        "title": [title],
        "viewItemURL": [f"https://www.ebay.com/itm/{item_id}"],
        "listingInfo": [{
            "listingType": [listing_type],
            "endTime": ["2026-06-15T18:30:00.000Z"],
        }],
        "sellingStatus": [{
            "currentPrice": [{"@currencyId": "USD", "__value__": price}],
        }],
    }


class TestSearchSellerListings:
    def _mock_page(self, items, total_pages=1, ack="Success", error=None):
        """Build a Finding API findItemsIneBayStores JSON response."""
        resp = MagicMock()
        resp.status_code = 200
        response = {"ack": [ack]}
        if error is not None:
            response["errorMessage"] = [{"error": [{"message": [error]}]}]
        else:
            response["searchResult"] = [{"item": items}]
            response["paginationOutput"] = [{"totalPages": [str(total_pages)]}]
        resp.json.return_value = {"findItemsIneBayStoresResponse": [response]}
        return resp

    @patch("ebay_fetch.requests.get")
    def test_single_page(self, mock_get):
        items = [_finding_item("1", title="Comic #1")]
        mock_get.return_value = self._mock_page(items, total_pages=1)
        result = ebay_fetch.search_seller_listings(
            "seller", "tok", "https://api.ebay.com", app_id="APP"
        )
        assert len(result) == 1
        # Returned dicts are Browse-API-shaped (reshaped from the Finding API).
        assert result[0]["title"] == "Comic #1"
        assert result[0]["itemId"] == "1"
        assert result[0]["buyingOptions"] == ["AUCTION"]

    @patch("ebay_fetch.requests.get")
    def test_paginates(self, mock_get):
        page1 = [_finding_item(str(i)) for i in range(100)]
        page2 = [_finding_item("100")]
        mock_get.side_effect = [
            self._mock_page(page1, total_pages=2),
            self._mock_page(page2, total_pages=2),
        ]
        result = ebay_fetch.search_seller_listings(
            "seller", "tok", "https://api.ebay.com", app_id="APP"
        )
        assert len(result) == 101
        assert mock_get.call_count == 2
        # Second request asks for page 2.
        second_params = mock_get.call_args_list[1][1]["params"]
        assert second_params["paginationInput.pageNumber"] == 2

    @patch("ebay_fetch.requests.get")
    def test_stops_at_max_results(self, mock_get):
        page1 = [_finding_item(str(i)) for i in range(100)]
        mock_get.return_value = self._mock_page(page1, total_pages=5)
        result = ebay_fetch.search_seller_listings(
            "seller", "tok", "https://api.ebay.com", app_id="APP", max_results=50
        )
        assert len(result) == 50
        assert mock_get.call_count == 1

    @patch("ebay_fetch.requests.get")
    def test_empty_store(self, mock_get):
        mock_get.return_value = self._mock_page([], total_pages=1)
        result = ebay_fetch.search_seller_listings(
            "seller", "tok", "https://api.ebay.com", app_id="APP"
        )
        assert result == []

    @patch("ebay_fetch.requests.get")
    def test_store_name_from_url_passed_as_storeName(self, mock_get):
        mock_get.return_value = self._mock_page([], total_pages=1)
        ebay_fetch.search_seller_listings(
            "https://www.ebay.com/str/tunerscomics",
            "tok",
            "https://api.ebay.com",
            app_id="APP",
        )
        params = mock_get.call_args[1]["params"]
        assert params["storeName"] == "tunerscomics"
        assert params["OPERATION-NAME"] == "findItemsIneBayStores"
        assert params["SECURITY-APPNAME"] == "APP"

    @patch("ebay_fetch.requests.get")
    def test_error_ack_returns_partial_and_logs(self, mock_get, capsys):
        mock_get.return_value = self._mock_page(
            [], ack="Failure", error="Invalid app ID"
        )
        result = ebay_fetch.search_seller_listings(
            "seller", "tok", "https://api.ebay.com", app_id="APP"
        )
        assert result == []
        assert "Invalid app ID" in capsys.readouterr().err


class TestFindingItemToBrowse:
    def _item(self, **overrides):
        base = _finding_item("298217294954", title="Amazing Spider-Man #300 NM Marvel 1988")
        # Allow overriding nested listingInfo via a listing_type shortcut.
        listing_type = overrides.pop("listing_type", None)
        if listing_type is not None:
            base["listingInfo"] = [{
                "listingType": [listing_type],
                "endTime": ["2026-05-28T12:00:00.000Z"],
            }]
        base.update(overrides)
        return base

    def test_auction_chinese(self):
        d = ebay_fetch._finding_item_to_browse(self._item(), "tunerscomics")
        assert d["buyingOptions"] == ["AUCTION"]
        assert d["currentBidPrice"] == {"value": "10.00", "currency": "USD"}
        assert d["itemId"] == "298217294954"
        assert d["seller"] == {"username": "tunerscomics"}

    def test_fixed_price(self):
        d = ebay_fetch._finding_item_to_browse(
            self._item(listing_type="FixedPrice"), "tunerscomics"
        )
        assert d["buyingOptions"] == ["FIXED_PRICE"]
        assert d["currentBidPrice"] is None
        assert d["price"] == {"value": "10.00", "currency": "USD"}

    def test_auction_with_bin_classified_as_auction(self):
        # Regression: an auction with a live Buy It Now reports "AuctionWithBIN".
        d = ebay_fetch._finding_item_to_browse(
            self._item(listing_type="AuctionWithBIN"), "tunerscomics"
        )
        assert d["buyingOptions"] == ["AUCTION"]
        assert d["currentBidPrice"] == {"value": "10.00", "currency": "USD"}

    def test_store_inventory_classified_as_fixed_price(self):
        d = ebay_fetch._finding_item_to_browse(
            self._item(listing_type="StoreInventory"), "tunerscomics"
        )
        assert d["buyingOptions"] == ["FIXED_PRICE"]

    def test_plain_item_id_survives_parse(self):
        d = ebay_fetch._finding_item_to_browse(self._item(), "tunerscomics")
        parsed = ebay_fetch.parse_item_summary(d)
        assert parsed["item_id"] == "298217294954"
        assert parsed["listing_type"] == "Auction"
        assert parsed["current_price"] == "$10.00"

    def test_missing_view_item_url_fallback(self):
        item = self._item()
        del item["viewItemURL"]
        d = ebay_fetch._finding_item_to_browse(item, "tunerscomics")
        assert "298217294954" in d["itemWebUrl"]

    def test_end_time_round_trips_through_parse(self):
        d = ebay_fetch._finding_item_to_browse(self._item(), "tunerscomics")
        parsed = ebay_fetch.parse_item_summary(d)
        assert parsed["end_date_iso"] == "2026-06-15T18:30:00.000Z"
        assert parsed["end_date"] is not None

    def test_seller_round_trips_through_parse(self):
        d = ebay_fetch._finding_item_to_browse(self._item(), "tunerscomics")
        parsed = ebay_fetch.parse_item_summary(d)
        assert parsed["seller"] == "tunerscomics"


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
