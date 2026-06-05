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

    def test_ssn_search_url(self):
        assert ebay_fetch._extract_seller_username(
            "https://www.ebay.com/sch/i.html?_ssn=tuners_comics_2011&_sop=1"
        ) == "tuners_comics_2011"


class TestResolveSellerUsername:
    def test_username_override_wins(self):
        assert ebay_fetch.resolve_seller_username(
            "tunerscomics", {}, username_override="tuners_comics_2011"
        ) == "tuners_comics_2011"

    def test_usr_url_is_trusted_username(self):
        assert ebay_fetch.resolve_seller_username(
            "https://www.ebay.com/usr/beatlebluecat", {}
        ) == "beatlebluecat"

    def test_ssn_url_is_trusted_username(self):
        assert ebay_fetch.resolve_seller_username(
            "https://www.ebay.com/sch/i.html?_ssn=tuners_comics_2011", {}
        ) == "tuners_comics_2011"

    def test_store_name_resolved_via_alias(self):
        aliases = {"tunerscomics": "tuners_comics_2011"}
        assert ebay_fetch.resolve_seller_username("tunerscomics", aliases) == "tuners_comics_2011"

    def test_alias_lookup_case_insensitive(self):
        aliases = {"tunerscomics": "tuners_comics_2011"}
        assert ebay_fetch.resolve_seller_username("TunersComics", aliases) == "tuners_comics_2011"

    def test_str_url_needs_alias(self):
        aliases = {"tunerscomics": "tuners_comics_2011"}
        assert ebay_fetch.resolve_seller_username(
            "https://www.ebay.com/str/tunerscomics", aliases
        ) == "tuners_comics_2011"

    def test_unknown_store_raises(self):
        with pytest.raises(ebay_fetch.UnknownSellerError) as exc:
            ebay_fetch.resolve_seller_username("mysteryshop", {})
        assert exc.value.store == "mysteryshop"


class TestSellerAliases:
    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ebay_fetch, "SELLER_ALIASES_FILE", tmp_path / "nope.json")
        assert ebay_fetch.load_seller_aliases() == {}

    def test_load_lowercases_keys(self, tmp_path, monkeypatch):
        f = tmp_path / "aliases.json"
        f.write_text(json.dumps({"TunersComics": "tuners_comics_2011"}))
        monkeypatch.setattr(ebay_fetch, "SELLER_ALIASES_FILE", f)
        assert ebay_fetch.load_seller_aliases() == {"tunerscomics": "tuners_comics_2011"}

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        f = tmp_path / "sub" / "aliases.json"  # parent created by save
        monkeypatch.setattr(ebay_fetch, "SELLER_ALIASES_FILE", f)
        ebay_fetch.save_seller_alias("TunersComics", "tuners_comics_2011")
        assert ebay_fetch.load_seller_aliases() == {"tunerscomics": "tuners_comics_2011"}

    def test_corrupt_file_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "aliases.json"
        f.write_text("{not json")
        monkeypatch.setattr(ebay_fetch, "SELLER_ALIASES_FILE", f)
        assert ebay_fetch.load_seller_aliases() == {}

    def test_committed_seed_file_present(self):
        # The alias map ships in the repo so it needs no per-machine setup.
        aliases = ebay_fetch.load_seller_aliases()
        assert aliases.get("tunerscomics") == "tuners36"


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
    def _item(self, i, seller="seller"):
        return {
            "itemId": f"v1|{i}|0",
            "title": f"Comic #{i}",
            "seller": {"username": seller},
        }

    def _mock_page(self, items, total, warnings=None):
        resp = MagicMock()
        resp.status_code = 200
        body = {"itemSummaries": items, "total": total}
        if warnings:
            body["warnings"] = warnings
        resp.json.return_value = body
        return resp

    @patch("ebay_fetch.requests.get")
    def test_single_page(self, mock_get):
        mock_get.return_value = self._mock_page([self._item(1)], 1)
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert len(result) == 1
        assert result[0]["title"] == "Comic #1"

    @patch("ebay_fetch.requests.get")
    def test_paginates(self, mock_get):
        page1 = [self._item(i) for i in range(200)]
        page2 = [self._item(200)]
        mock_get.side_effect = [
            self._mock_page(page1, 201),
            self._mock_page(page2, 201),
        ]
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert len(result) == 201

    @patch("ebay_fetch.requests.get")
    def test_stops_at_max_results(self, mock_get):
        page1 = [self._item(i) for i in range(200)]
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
    def test_filter_braces_survive_in_query(self, mock_get):
        # Braces must reach eBay literally, not percent-encoded (BUI-68).
        mock_get.return_value = self._mock_page([], 0)
        ebay_fetch.search_seller_listings(
            "https://www.ebay.com/usr/beatlebluecat", "tok", "https://api.ebay.com"
        )
        query = mock_get.call_args[1]["params"]
        assert isinstance(query, str)
        assert "sellers:{beatlebluecat}" in query
        assert "%7B" not in query

    @patch("ebay_fetch.requests.get")
    def test_other_sellers_filtered_out(self, mock_get):
        # Even if eBay returns foreign sellers, we never surface them.
        items = [self._item(1, seller="seller"), self._item(2, seller="someoneelse")]
        mock_get.return_value = self._mock_page(items, 2)
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert len(result) == 1
        assert result[0]["seller"]["username"] == "seller"

    @patch("ebay_fetch.requests.get")
    def test_invalid_seller_warning_aborts(self, mock_get):
        items = [self._item(i, seller="randomseller") for i in range(200)]
        mock_get.return_value = self._mock_page(
            items, 260000,
            warnings=[{"message": "A seller 'username' provided in the request filters is invalid."}],
        )
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert result == []
        assert mock_get.call_count == 1


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


# ─── BUI-88: wish-list fetched over HTTP from the gixen server API ────────────


class TestFetchWishList:
    def test_returns_parsed_items(self, monkeypatch):
        monkeypatch.setenv("GIXEN_SERVER_URL", "http://mac-mini.example:8080")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"id": 1, "name": "Daredevil #1"}]
        with patch("seller_scan.requests.get", return_value=resp) as get:
            assert seller_scan.fetch_wish_list() == [{"id": 1, "name": "Daredevil #1"}]
        # trailing slash is trimmed; endpoint is the provider-neutral path
        get.assert_called_once()
        assert get.call_args[0][0] == "http://mac-mini.example:8080/api/comics/wish-list"

    def test_hard_fails_when_server_url_unset(self, monkeypatch):
        monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
        with pytest.raises(SystemExit):
            seller_scan.fetch_wish_list()

    def test_hard_fails_when_server_unreachable(self, monkeypatch):
        monkeypatch.setenv("GIXEN_SERVER_URL", "http://mac-mini.example:8080")
        with patch(
            "seller_scan.requests.get",
            side_effect=seller_scan.requests.exceptions.ConnectionError("down"),
        ):
            with pytest.raises(SystemExit):
                seller_scan.fetch_wish_list()
