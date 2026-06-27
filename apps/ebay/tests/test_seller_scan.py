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

    # BUI-226: era-data fields
    def test_carries_series_name(self):
        """_series_name propagates the raw LOCG decorated series name."""
        wish = [{
            "id": 1,
            "name": "Amazing Spider-Man #300",
            "series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        }]
        items = seller_scan.prepare_wish_items(wish)
        assert items[0]["_series_name"] == "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"

    def test_carries_release_year(self):
        """_release_year is the first 4 chars of release_date."""
        wish = [{
            "id": 1,
            "name": "Amazing Spider-Man #300",
            "release_date": "1988-05-10",
        }]
        items = seller_scan.prepare_wish_items(wish)
        assert items[0]["_release_year"] == "1988"

    def test_series_name_none_when_missing(self):
        """Items without series_name (local-only) get _series_name=None."""
        wish = [{"id": 1, "name": "Amazing Spider-Man #300"}]
        items = seller_scan.prepare_wish_items(wish)
        assert items[0]["_series_name"] is None

    def test_release_year_none_when_missing(self):
        """Items without release_date get _release_year=None."""
        wish = [{"id": 1, "name": "Amazing Spider-Man #300"}]
        items = seller_scan.prepare_wish_items(wish)
        assert items[0]["_release_year"] is None


# ─── BUI-226: era disambiguation helpers ─────────────────────────────────────


class TestSeriesYearRange:
    """series_year_range — ported from locg.collection_cache."""

    def test_range(self):
        assert seller_scan.series_year_range(
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
        ) == (1963, 1998)

    def test_range_en_dash(self):
        """En-dash variant in the year range."""
        assert seller_scan.series_year_range(
            "X-Men (Vol. 1) (1963–1981)"
        ) == (1963, 1981)

    def test_open_ended_present(self):
        assert seller_scan.series_year_range(
            "The Amazing Spider-Man (2022 - Present)"
        ) == (2022, 9999)

    def test_bare_single_year(self):
        """A bare (YYYY) is a one-year range, not open-ended."""
        assert seller_scan.series_year_range("Wolverine (1988)") == (1988, 1988)

    def test_no_year_returns_none(self):
        assert seller_scan.series_year_range("Amazing Spider-Man") is None

    def test_empty_string_returns_none(self):
        assert seller_scan.series_year_range("") is None

    def test_none_returns_none(self):
        # Guard against None input
        assert seller_scan.series_year_range(None) is None  # type: ignore[arg-type]


class TestSeriesVolume:
    def test_vol_dot(self):
        assert seller_scan.series_volume(
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
        ) == 1

    def test_volume_word(self):
        assert seller_scan.series_volume("Wolverine (Volume 2) (1982 - 2003)") == 2

    def test_vol_no_dot(self):
        assert seller_scan.series_volume("X-Men (Vol 3) (2010 - 2014)") == 3

    def test_no_vol_returns_none(self):
        assert seller_scan.series_volume("Amazing Spider-Man (1963 - 1998)") is None

    def test_empty_returns_none(self):
        assert seller_scan.series_volume("") is None


class TestTitleParenYear:
    def test_year_in_parens(self):
        assert seller_scan._title_paren_year("Amazing Spider-Man (2022) #7") == 2022

    def test_early_year(self):
        assert seller_scan._title_paren_year("Batman (1940) #100 NM") == 1940

    def test_no_paren_year(self):
        """Bare year without parens is not extracted."""
        assert seller_scan._title_paren_year("Amazing Spider-Man #7 VF 1963") is None

    def test_out_of_range_year_ignored(self):
        """A parenthesized year outside [1930, 2035] is not returned."""
        assert seller_scan._title_paren_year("Batman (1900) #1") is None
        assert seller_scan._title_paren_year("Something (2100) #1") is None

    def test_empty_returns_none(self):
        assert seller_scan._title_paren_year("") is None


class TestTitleVolume:
    def test_vol_n(self):
        assert seller_scan._title_volume("Amazing Spider-Man Vol 3 #7") == 3

    def test_volume_n(self):
        assert seller_scan._title_volume("X-Men Volume 1 #94") == 1

    def test_vol_dot_n(self):
        assert seller_scan._title_volume("Spider-Man Vol. 2 #1") == 2

    def test_no_vol_returns_none(self):
        assert seller_scan._title_volume("Amazing Spider-Man #7 VF") is None

    def test_empty_returns_none(self):
        assert seller_scan._title_volume("") is None


class TestEraMismatch:
    # ─── Fail-open: missing signal → False ───────────────────────────────────

    def test_no_series_name_fail_open(self):
        """No series_name → False (can't discriminate)."""
        assert seller_scan.era_mismatch("Amazing Spider-Man (2022) #7", None) is False

    def test_empty_series_name_fail_open(self):
        assert seller_scan.era_mismatch("Amazing Spider-Man (2022) #7", "") is False

    def test_no_title_year_fail_open(self):
        """Title has no parenthesized year → year check skipped → False."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #7 VF",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_series_name_no_year_range_fail_open(self):
        """Series name has no year decoration → False."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (2022) #7",
            "Amazing Spider-Man",
        ) is False

    def test_no_title_vol_fail_open(self):
        """Title has no explicit 'vol N' → volume check skipped → False."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #7 VF",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_series_no_vol_fail_open(self):
        """Series has no vol. decoration → volume check skipped → False."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man Vol 2 #7",
            "The Amazing Spider-Man (1963 - 1998)",
        ) is False

    # ─── Year check: out-of-range → True ─────────────────────────────────────

    def test_year_out_of_range_rejected(self):
        """(2022) title vs 1963-1998 range → mismatch."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (2022) #7",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is True

    def test_year_in_range_accepted(self):
        """(1984) title vs 1963-1998 range → no mismatch."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1984) #247",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_year_at_lower_boundary_accepted(self):
        """Year exactly at begin is within range (±1 tolerance means begin-1 is also OK)."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1963) #1",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_year_at_begin_minus_1_accepted(self):
        """Year = begin - 1 is within ±1 tolerance (cover-vs-onsale skew, BUI-214)."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1962) #1",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_year_at_end_plus_1_accepted(self):
        """Year = end + 1 is within ±1 tolerance."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1999) #441",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_year_at_end_plus_2_rejected(self):
        """Year = end + 2 is outside the ±1 window → reject."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (2000) #1",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is True

    def test_year_in_open_ended_range_accepted(self):
        """A (2024) title for a (2022 - Present) series is fine."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (2024) #50",
            "The Amazing Spider-Man (2022 - Present)",
        ) is False

    def test_old_year_against_open_ended_rejected(self):
        """A (1965) title for a (2022 - Present) series → reject."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1965) #7",
            "The Amazing Spider-Man (2022 - Present)",
        ) is True

    # ─── Volume check: mismatch → True ───────────────────────────────────────

    def test_vol_mismatch_rejected(self):
        """Title says Vol 2, wish is Vol. 1 → reject."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man Vol 2 #7",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is True

    def test_vol_match_accepted(self):
        """Title says Vol 1, wish is Vol. 1 → no mismatch."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man Vol 1 #7",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False


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
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"id": 1, "name": "Daredevil #1"}]
        with patch("seller_scan.requests.get", return_value=resp) as get:
            assert seller_scan.fetch_wish_list() == [{"id": 1, "name": "Daredevil #1"}]
        # trailing slash is trimmed; endpoint is the provider-neutral path
        get.assert_called_once()
        assert get.call_args[0][0] == "http://mac-mini.example:8080/api/comics/wish-list"

    def test_hard_fails_when_server_url_unset(self, monkeypatch):
        monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
        monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
        with pytest.raises(SystemExit):
            seller_scan.fetch_wish_list()

    def test_deprecated_gixen_server_url_still_resolves(self, monkeypatch):
        # BUI-220: COMICS_SERVER_URL is canonical, but the deprecated
        # GIXEN_SERVER_URL alias must still resolve when it's the only one set.
        monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
        monkeypatch.setenv("GIXEN_SERVER_URL", "http://legacy.example:8080")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"id": 1, "name": "Daredevil #1"}]
        with patch("seller_scan.requests.get", return_value=resp) as get:
            assert seller_scan.fetch_wish_list() == [{"id": 1, "name": "Daredevil #1"}]
        assert get.call_args[0][0] == "http://legacy.example:8080/api/comics/wish-list"

    def test_hard_fails_when_server_unreachable(self, monkeypatch):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        with patch(
            "seller_scan.requests.get",
            side_effect=seller_scan.requests.exceptions.ConnectionError("down"),
        ):
            with pytest.raises(SystemExit):
                seller_scan.fetch_wish_list()


# ─── BUI-113: seen-tracking (best-effort, never fatal) ────────────────────────


class TestFetchSeenItemIds:
    def test_returns_set_on_success(self, monkeypatch):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080/")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"item_ids": ["111", "222"]}
        with patch("seller_scan.requests.get", return_value=resp) as get:
            assert seller_scan.fetch_seen_item_ids("tuners36") == {"111", "222"}
        # trailing slash trimmed; seller passed as a query param
        assert get.call_args[0][0] == (
            "http://mac-mini.example:8080/api/comics/seller-scan/seen"
        )
        assert get.call_args[1]["params"] == {"seller": "tuners36"}

    def test_empty_set_when_server_url_unset(self, monkeypatch):
        monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
        monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
        with patch("seller_scan.requests.get") as get:
            assert seller_scan.fetch_seen_item_ids("tuners36") == set()
        get.assert_not_called()

    def test_soft_fails_to_empty_set_when_unreachable(self, monkeypatch):
        # Unlike the wish-list, a failed seen-read must NOT abort — it falls back
        # to showing all matches (a duplicate is safe; a hidden match is not).
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        with patch(
            "seller_scan.requests.get",
            side_effect=seller_scan.requests.exceptions.ConnectionError("down"),
        ):
            assert seller_scan.fetch_seen_item_ids("tuners36") == set()


class TestRecordItemsSeen:
    def test_posts_item_ids(self, monkeypatch):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080/")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        with patch("seller_scan.requests.post", return_value=resp) as post:
            seller_scan.record_items_seen(["111", "222"], "tuners36")
        assert post.call_args[0][0] == (
            "http://mac-mini.example:8080/api/comics/seller-scan/seen"
        )
        assert post.call_args[1]["json"] == {
            "item_ids": ["111", "222"],
            "seller": "tuners36",
        }

    def test_noop_on_empty_item_ids(self, monkeypatch):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        with patch("seller_scan.requests.post") as post:
            seller_scan.record_items_seen([], "tuners36")
        post.assert_not_called()

    def test_noop_when_server_url_unset(self, monkeypatch):
        monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
        monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
        with patch("seller_scan.requests.post") as post:
            seller_scan.record_items_seen(["111"], "tuners36")
        post.assert_not_called()

    def test_swallows_post_failure(self, monkeypatch):
        # Best-effort: a failed record must not raise.
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        with patch(
            "seller_scan.requests.post",
            side_effect=seller_scan.requests.exceptions.ConnectionError("down"),
        ):
            seller_scan.record_items_seen(["111"], "tuners36")  # no exception


# ─── BUI-184 robustness fixes ─────────────────────────────────────────────────


class TestNullTitleScanLoop:
    """BUI-184 Item 2 (seller_scan side): a null/empty title from parse_item_summary
    must be skipped gracefully instead of crashing the scan with AttributeError."""

    def test_match_listing_with_empty_title_returns_no_match(self):
        """match_listing('', ...) must not raise and must return (None, 0.0)."""
        wish_items = seller_scan.prepare_wish_items([
            {"id": 1, "name": "Amazing Spider-Man #300"},
        ])
        wish, score = seller_scan.match_listing("", wish_items)
        assert wish is None
        assert score == 0.0

    def test_scan_loop_skips_null_title_listing(self, monkeypatch):
        """If a listing has title=None (null from the API), the scan loop must skip
        it rather than raising AttributeError on .lower()."""
        # Two raw listings: one with null title, one genuine match
        null_listing = {
            "itemId": "v1|1|0",
            "title": None,
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "5.00", "currency": "USD"},
            "itemEndDate": "2026-06-01T12:00:00.000Z",
            "itemWebUrl": "https://www.ebay.com/itm/1",
            "seller": {"username": "testseller"},
        }
        good_listing = {
            "itemId": "v1|2|0",
            "title": "Amazing Spider-Man #300 NM Marvel 1988",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "100.00", "currency": "USD"},
            "itemEndDate": "2026-06-01T12:00:00.000Z",
            "itemWebUrl": "https://www.ebay.com/itm/2",
            "seller": {"username": "testseller"},
        }
        wish_items = seller_scan.prepare_wish_items([
            {"id": 1, "name": "Amazing Spider-Man #300"},
        ])

        candidates = []
        # Replicate the scan-loop body from seller_scan.main() so we can exercise
        # the null-title guard without spinning up the full CLI.
        for raw in [null_listing, good_listing]:
            listing = ebay_fetch.parse_item_summary(raw)
            # The guard added in BUI-184: skip title-less listings.
            if not listing.get("title"):
                continue
            wish, score = seller_scan.match_listing(listing["title"], wish_items)
            if wish:
                candidates.append(listing)

        # The null-title listing is skipped; the good listing matches.
        assert len(candidates) == 1
        assert candidates[0]["item_id"] == "2"


class TestIssueBoundaryRegex:
    """BUI-184 Item 3: the #N branch of issue_pattern must not prefix-match #300
    when the wish item is issue '3'."""

    def _items(self, names):
        return seller_scan.prepare_wish_items(
            [{"id": i, "name": n} for i, n in enumerate(names)]
        )

    def test_issue_3_does_not_match_title_with_300(self):
        """'#3' must not match a title that only contains '300' or '#300'.
        _normalize strips '#', so the bare-number boundary is the meaningful check."""
        items = self._items(["Some Series #3"])
        # After _normalize: "some series 300 ..." — issue "3" must NOT match "300"
        wish, score = seller_scan.match_listing("Some Series #300 VF Marvel", items)
        assert wish is None, "issue #3 must not match a title containing only '#300'"

    def test_issue_3_does_not_match_bare_300(self):
        """Guard the bare-number \b branch: '300' must not match issue '3'."""
        items = self._items(["Some Series #3"])
        wish, score = seller_scan.match_listing("Some Series 300 VF Marvel", items)
        assert wish is None, "issue '3' must not match bare '300' in title"

    def test_issue_3_still_matches_real_3(self):
        """A title containing exactly '#3' (normalized to ' 3 ') must still match."""
        items = self._items(["Some Series #3"])
        wish, score = seller_scan.match_listing("Some Series #3 VF Marvel", items)
        assert wish is not None, "issue #3 must match a title that contains '#3'"

    def test_issue_3_still_matches_bare_3(self):
        """A title with bare '3' at a word boundary must still match."""
        items = self._items(["Some Series #3"])
        wish, score = seller_scan.match_listing("Some Series 3 VF Marvel", items)
        assert wish is not None, "issue '3' must match bare '3' at a word boundary"

    def test_issue_30_does_not_match_300(self):
        """Same boundary check for a two-digit issue: '30' vs '300'."""
        items = self._items(["Some Series #30"])
        wish, score = seller_scan.match_listing("Some Series #300 VF Marvel", items)
        assert wish is None, "issue #30 must not match a title containing only '#300'"

    def test_issue_1_does_not_match_10(self):
        """Issue '1' must not match '10' or '#10'."""
        items = self._items(["X-Men #1"])
        wish, score = seller_scan.match_listing("X Men 10 NM 1963 Marvel", items)
        assert wish is None, "issue '1' must not match '10'"


class TestGradeDigitNotIssueNumber:
    """BUI-135: the integer part of a numeric grade ('7.0', '9.4') must NOT
    satisfy the issue-number match. The repro titles carry RAW grades with no
    literal 'cgc', so the main()-level cgc-skip guard does not catch them —
    match_listing itself must strip the grade token."""

    def _items(self, names):
        return seller_scan.prepare_wish_items(
            [{"id": i, "name": n} for i, n in enumerate(names)]
        )

    def test_xmen_7_does_not_match_raw_grade_7_0(self):
        """wish 'The X-Men #7' must NOT match 'Uncanny X-men #145 ... F/VF 7.0'."""
        items = self._items(["The X-Men #7"])
        wish, score = seller_scan.match_listing(
            "Uncanny X-men #145 Marvel 1981 F/VF 7.0 Off White Pages", items
        )
        assert wish is None, "grade '7.0' must not satisfy issue #7"

    def test_xmen_9_does_not_match_raw_grade_9_0(self):
        """wish 'The X-Men #9' must NOT match a 'VF/NM 9.0' title."""
        items = self._items(["The X-Men #9"])
        wish, score = seller_scan.match_listing(
            "Uncanny X-men #142 Marvel 1981 VF/NM 9.0 White Pages", items
        )
        assert wish is None, "grade '9.0' must not satisfy issue #9"

    def test_series_300_issue_4_does_not_match_grade_9_4(self):
        """wish '300 #4' must NOT match '#300 ... 9.4' (the 4 is the grade)."""
        items = self._items(["300 #4"])
        wish, score = seller_scan.match_listing(
            "300 #1 Dark Horse 1998 CGC-style slab 9.4 White Pages", items
        )
        assert wish is None, "grade '9.4' must not satisfy issue #4"

    def test_cgc_slab_grades_do_not_match(self):
        """The 8.5 / 9.2 slab forms must not orphan their integer either."""
        items = self._items(["Daredevil #8", "Hulk #9"])
        wish, score = seller_scan.match_listing(
            "Daredevil #181 Marvel 1982 graded 8.5 VF+", items
        )
        assert wish is None, "grade '8.5' must not satisfy issue #8"
        wish, score = seller_scan.match_listing(
            "Hulk #340 Marvel 1988 graded 9.2 NM-", items
        )
        assert wish is None, "grade '9.2' must not satisfy issue #9"

    def test_genuine_match_survives_grade_strip(self):
        """A real #N in the title must still match even alongside a grade token."""
        items = self._items(["Moon Knight #15"])
        wish, score = seller_scan.match_listing(
            "Moon Knight #15 Marvel 1982 VF/NM 9.0 White Pages", items
        )
        assert wish is not None, "genuine issue #15 must still match"
        assert wish["name"] == "Moon Knight #15"
        assert score == 1.0

    def test_comichunterlv_batch_returns_only_genuine(self):
        """Repro: a batch of false-positive grade titles + one genuine match
        should surface only the genuine one (Moon Knight #15)."""
        items = self._items(
            ["The X-Men #7", "The X-Men #9", "Moon Knight #15", "300 #4"]
        )
        titles = [
            "Uncanny X-men #145 Marvel 1981 F/VF 7.0 Off White Pages",
            "Uncanny X-men #142 Marvel 1981 VF/NM 9.0 White Pages",
            "Moon Knight #15 Marvel 1982 VF 7.0 White Pages",
            "300 #1 Dark Horse graded 9.4 White Pages",
        ]
        matched = [
            seller_scan.match_listing(t, items)[0] for t in titles
        ]
        matched = [m for m in matched if m is not None]
        assert len(matched) == 1, f"expected only the genuine match, got {matched}"
        assert matched[0]["name"] == "Moon Knight #15"

    # BUI-135 code-review follow-up: grade written WITHOUT a decimal.

    def test_vf_9_does_not_match_issue_9(self):
        """A bare 'VF 9' grade must NOT orphan into wish issue #9."""
        items = self._items(["The X-Men #9"])
        wish, score = seller_scan.match_listing(
            "Uncanny X-men #142 Marvel 1981 VF 9 White Pages", items
        )
        assert wish is None, "grade 'VF 9' must not satisfy issue #9"

    def test_vf_nm_9_does_not_match_issue_9(self):
        """The combined 'VF/NM 9' grade (no decimal) must NOT match issue #9."""
        items = self._items(["The X-Men #9"])
        wish, score = seller_scan.match_listing(
            "Uncanny X-men #142 Marvel 1981 VF/NM 9 White Pages", items
        )
        assert wish is None, "grade 'VF/NM 9' must not satisfy issue #9"

    def test_nm_8_does_not_match_issue_8(self):
        """'NM 8' must NOT orphan into wish issue #8."""
        items = self._items(["Daredevil #8"])
        wish, score = seller_scan.match_listing(
            "Daredevil #181 Marvel 1982 NM 8 White Pages", items
        )
        assert wish is None, "grade 'NM 8' must not satisfy issue #8"

    def test_fn_plus_6_does_not_match_issue_6(self):
        """'FN+ 6' must NOT orphan into wish issue #6."""
        items = self._items(["Hulk #6"])
        wish, score = seller_scan.match_listing(
            "Hulk #181 Marvel 1974 FN+ 6 Off White", items
        )
        assert wish is None, "grade 'FN+ 6' must not satisfy issue #6"

    def test_genuine_hash_9_still_matches_with_grade_word_elsewhere(self):
        """A real '#9' must still match even when a grade word sits elsewhere."""
        items = self._items(["The X-Men #9"])
        wish, score = seller_scan.match_listing(
            "The X-Men #9 Marvel VF condition White Pages", items
        )
        assert wish is not None, "genuine issue #9 must still match"
        assert wish["name"] == "The X-Men #9"

    def test_no_grade_prefix_bare_9_still_matches(self):
        """A bare issue number with NO grade-letter prefix must still match —
        the matcher's loose bias is preserved."""
        items = self._items(["The X-Men #9"])
        wish, score = seller_scan.match_listing(
            "The X-Men 9 Marvel 1965 White Pages", items
        )
        assert wish is not None, "bare '9' with no grade prefix must match issue 9"
        assert wish["name"] == "The X-Men #9"


# ─── hard_reject pre-filter (BUI-221 Part A) ─────────────────────────────────


class TestHardRejectCGC:
    def test_cgc_in_title_rejected(self):
        assert seller_scan.hard_reject(
            "CGC Amazing Spider-Man #300 9.8 NM", "Amazing Spider-Man", "300"
        )

    def test_cgc_lowercase_rejected(self):
        assert seller_scan.hard_reject(
            "amazing spider-man #300 cgc 9.4", "Amazing Spider-Man", "300"
        )

    def test_no_cgc_not_rejected_by_rule1(self):
        # A raw ungraded title must NOT be rejected on account of rule 1.
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man #300 NM Marvel 1988", "Amazing Spider-Man", "300"
        )


class TestHardRejectEditionMismatch:
    def test_annual_rejected_for_regular_series(self):
        # "Avengers Annual #1" must not satisfy a wish for "The Avengers".
        assert seller_scan.hard_reject(
            "Avengers Annual #1", "The Avengers", "1"
        )

    def test_annual_kept_for_annual_series(self):
        # Wish series IS "Avengers Annual" — title is a genuine match.
        assert not seller_scan.hard_reject(
            "Avengers Annual #1", "Avengers Annual", "1"
        )

    def test_giant_size_hyphenated_rejected(self):
        assert seller_scan.hard_reject(
            "Giant-Size X-Men #1 Marvel 1975", "X-Men", "1"
        )

    def test_giant_size_spaced_rejected(self):
        assert seller_scan.hard_reject(
            "Giant Size X-Men #1 Marvel 1975", "X-Men", "1"
        )

    def test_giant_size_kept_for_giant_size_series(self):
        assert not seller_scan.hard_reject(
            "Giant-Size X-Men #1", "Giant-Size X-Men", "1"
        )

    def test_king_size_hyphenated_rejected(self):
        assert seller_scan.hard_reject(
            "King-Size Spider-Man #1", "Amazing Spider-Man", "1"
        )

    def test_king_size_spaced_rejected(self):
        assert seller_scan.hard_reject(
            "King Size Spider-Man #1", "Amazing Spider-Man", "1"
        )

    def test_special_rejected_for_regular_series(self):
        assert seller_scan.hard_reject(
            "Amazing Spider-Man Special #5", "Amazing Spider-Man", "5"
        )

    def test_special_kept_when_series_contains_special(self):
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man Special #5", "Amazing Spider-Man Special", "5"
        )

    def test_treasury_rejected_for_regular_series(self):
        assert seller_scan.hard_reject(
            "Superman vs Muhammad Ali Treasury Edition", "Superman", "1"
        )

    def test_treasury_kept_for_treasury_series(self):
        # Treasury editions are typically unnumbered one-shots; pass issue=None
        # so rule 4 (missing issue number) does not apply, isolating rule 2.
        assert not seller_scan.hard_reject(
            "Superman vs Muhammad Ali Treasury Edition", "Superman Treasury", None
        )


class TestHardRejectLot:
    def test_lot_of_rejected(self):
        assert seller_scan.hard_reject(
            "Amazing Spider-Man lot of 5 comics", "Amazing Spider-Man", "300"
        )

    def test_lot_with_leading_count_rejected(self):
        assert seller_scan.hard_reject(
            "10 lot X-Men Marvel Comics", "X-Men", "1"
        )

    def test_lot_with_trailing_count_rejected(self):
        assert seller_scan.hard_reject(
            "X-Men lot 5 books", "X-Men", "1"
        )

    def test_collection_rejected(self):
        assert seller_scan.hard_reject(
            "X-Men Complete Collection 1-50", "X-Men", "1"
        )

    def test_complete_run_rejected(self):
        assert seller_scan.hard_reject(
            "Daredevil #1-50 Complete Run Marvel Bronze Age", "Daredevil", "1"
        )

    def test_set_of_rejected(self):
        assert seller_scan.hard_reject(
            "Set of 4 Avengers Marvel comics", "The Avengers", "4"
        )

    def test_issue_range_rejected(self):
        # "#1-#10" issue range is a multi-comic lot.
        assert seller_scan.hard_reject(
            "X-Men #1-#10 Bronze Age Lot", "X-Men", "1"
        )

    def test_bare_issue_range_rejected(self):
        assert seller_scan.hard_reject(
            "Amazing Spider-Man 129-150 Bronze Age", "Amazing Spider-Man", "129"
        )


class TestHardRejectMissingIssue:
    def test_title_missing_issue_number_rejected(self):
        # Title has no "300" — obvious wrong listing.
        assert seller_scan.hard_reject(
            "Amazing Spider-Man NM Marvel 1988", "Amazing Spider-Man", "300"
        )

    def test_title_has_wrong_issue_number_rejected(self):
        assert seller_scan.hard_reject(
            "Amazing Spider-Man #299 NM Marvel", "Amazing Spider-Man", "300"
        )

    def test_issue_none_skips_rule4(self):
        # issue=None → rule 4 does not apply; only rules 1-3 can trigger.
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man NM Marvel 1988", "Amazing Spider-Man", None
        )


class TestHardRejectCleanMatch:
    def test_clean_match_not_rejected(self):
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man #300 NM Marvel 1988", "Amazing Spider-Man", "300"
        )

    def test_clean_match_with_publisher_info(self):
        assert not seller_scan.hard_reject(
            "Fantastic Four #48 VF Silver Surfer Marvel", "Fantastic Four", "48"
        )

    def test_clean_match_with_grade_stripped(self):
        # Grade digits must not confuse the issue check (BUI-135 integration).
        assert not seller_scan.hard_reject(
            "Moon Knight #15 Marvel 1982 VF/NM 9.0", "Moon Knight", "15"
        )


# ─── verify_with_claude chunking (BUI-221 Part B) ────────────────────────────


class TestVerifyWithClaudeMissingKey:
    def test_missing_api_key_exits_cleanly(self, capsys, monkeypatch):
        """No ANTHROPIC_API_KEY → clean error + exit, not a raw traceback.
        Matters for the unattended wishlist-sellers scheduled run."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Make _load_dotenv a no-op so it can't pull a key from a real .env.
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: {})
        with pytest.raises(SystemExit) as exc:
            seller_scan.verify_with_claude([{"title": "X #1", "wish_name": "X #1"}])
        assert exc.value.code == 1
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


class TestVerifyWithClaudeChunking:
    """Verify chunked calling and fail-closed behaviour of verify_with_claude."""

    @pytest.fixture(autouse=True)
    def _set_api_key(self, monkeypatch):
        # The key-presence guard runs before the (mocked) client; tests must
        # provide a dummy key so they exercise the chunking logic, not the guard.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    def _make_matches(self, n):
        return [
            {"title": f"Comic Series #{i}", "wish_name": f"Comic Series #{i}"}
            for i in range(1, n + 1)
        ]

    def _genuine_response(self, count):
        """Mock response where all ``count`` candidates are genuine (empty rejects list)."""
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="[]")]
        return fake_resp

    def test_250_candidates_produce_3_api_calls(self, monkeypatch):
        """250 candidates → 3 chunks: [100, 100, 50] → 3 messages.create calls."""
        matches = self._make_matches(250)
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            self._genuine_response(100),
            self._genuine_response(100),
            self._genuine_response(50),
        ]
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        assert fake_client.messages.create.call_count == 3
        assert len(result) == 250

    def test_chunk_indices_are_local_not_global(self, monkeypatch):
        """Each chunk's prompt uses 1-based indices local to that chunk, not
        global position, so verdicts correlate correctly across chunk boundaries."""
        matches = self._make_matches(150)  # 2 chunks: [100, 50]

        # Chunk 2: reject ids 2-50 (local), keep only id=1 (local) →
        # corresponds to global match #101.
        rejected_chunk2 = [{"id": i, "reason": "test"} for i in range(2, 51)]

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [
            MagicMock(content=[MagicMock(text="[]")]),
            MagicMock(content=[MagicMock(text=json.dumps(rejected_chunk2))]),
        ]
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        # 100 from chunk 1 + 1 from chunk 2 = 101
        assert len(result) == 101
        # The surviving item from chunk 2 is global index 101 (title "Comic Series #101")
        assert result[-1]["title"] == "Comic Series #101"

    def test_unparseable_chunk_fails_closed(self, monkeypatch, capsys):
        """A chunk whose response contains no JSON array is dropped entirely."""
        matches = self._make_matches(3)
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="I cannot help with that request.")]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        assert result == []
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_out_of_range_id_fails_closed(self, monkeypatch, capsys):
        """A rejected-id outside 1..len(chunk) causes the whole chunk to drop."""
        matches = self._make_matches(5)
        # id=6 is out of range for a 5-candidate chunk.
        rejected = [{"id": 6, "reason": "phantom id"}]
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text=json.dumps(rejected))]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        assert result == []
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_non_int_id_fails_closed(self, monkeypatch, capsys):
        """A rejected-id that is not an integer causes the whole chunk to drop."""
        matches = self._make_matches(5)
        rejected = [{"id": "two", "reason": "string id"}]
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text=json.dumps(rejected))]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        assert result == []
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_missing_id_key_fails_closed(self, monkeypatch, capsys):
        """A rejected object with no 'id' key causes the whole chunk to drop."""
        matches = self._make_matches(5)
        rejected = [{"reason": "forgot the id"}]
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text=json.dumps(rejected))]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        assert result == []
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_empty_array_keeps_all_candidates(self, monkeypatch):
        """[] response means nothing rejected — all candidates returned."""
        matches = self._make_matches(5)
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="[]")]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        assert len(result) == 5

    def test_good_chunk_after_bad_chunk_still_kept(self, monkeypatch, capsys):
        """A bad first chunk drops its candidates but a good second chunk is kept."""
        matches = self._make_matches(150)  # 2 chunks: [100, 50]

        bad_resp = MagicMock()
        bad_resp.content = [MagicMock(text="no json here")]
        good_resp = MagicMock()
        good_resp.content = [MagicMock(text="[]")]  # nothing rejected → all 50 kept

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [bad_resp, good_resp]
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        result = seller_scan.verify_with_claude(matches)

        # First 100 dropped (bad chunk); last 50 kept.
        assert len(result) == 50
        assert result[0]["title"] == "Comic Series #101"
        err = capsys.readouterr().err
        assert "fail-closed" in err


class TestVerifyWithClaudeNoSilentDrop:
    @pytest.fixture(autouse=True)
    def _set_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    def test_dropped_candidates_logged_to_stderr(self, capsys, monkeypatch):
        """BUI-149: the script's internal Claude pass is the single verification
        gate, so a rejected candidate must be surfaced (stderr) with its reason,
        not silently dropped — the seller-scan skill no longer runs a second
        verifier and relies on this audit trail."""
        matches = [
            {"title": "Amazing Spider-Man #300 NM Marvel 1988",
             "wish_name": "Amazing Spider-Man #300"},
            {"title": "Daredevil Annual #1", "wish_name": "Daredevil #1"},
        ]
        verdict_json = '[{"id":2,"reason":"Annual, not the regular series"}]'
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text=verdict_json)]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        kept = seller_scan.verify_with_claude(matches)

        # Only the genuine match is returned (unchanged behaviour).
        assert [m["title"] for m in kept] == ["Amazing Spider-Man #300 NM Marvel 1988"]
        # The rejected one is surfaced to stderr with its reason.
        err = capsys.readouterr().err
        assert "Filtered 1 likely false positive" in err
        assert "Daredevil Annual #1" in err
        assert "Annual, not the regular series" in err


# ─── BUI-227: _title_paren_years ─────────────────────────────────────────────


class TestTitleParenYears:
    def test_compound_paren_extracts_year(self):
        """Compound group like "(Marvel Comics December 2014)" yields [2014]."""
        assert seller_scan._title_paren_years(
            "Amazing Spider-Man #7 (Marvel Comics December 2014)"
        ) == [2014]

    def test_bare_paren_year(self):
        assert seller_scan._title_paren_years("Batman (1940) #100 NM") == [1940]

    def test_multiple_groups_multiple_years(self):
        """Two parenthetical groups each with a year → both returned in order."""
        result = seller_scan._title_paren_years("Some Book (1963) (CGC 2024)")
        assert result == [1963, 2024]

    def test_out_of_range_years_ignored(self):
        """Years outside [1930, 2035] are silently dropped."""
        assert seller_scan._title_paren_years("Book (1900) #1") == []
        assert seller_scan._title_paren_years("Book (2100) #1") == []

    def test_no_paren_year_returns_empty(self):
        """Bare (un-parenthesized) year is not extracted."""
        assert seller_scan._title_paren_years("Amazing Spider-Man #7 VF 1963") == []

    def test_empty_title_returns_empty(self):
        assert seller_scan._title_paren_years("") == []

    def test_mixed_range_and_out_of_range(self):
        """Only in-range years survive when the group has multiple 4-digit numbers."""
        result = seller_scan._title_paren_years("Book (Marvel 2014 1900) #1")
        assert result == [2014]

    # _title_paren_year backward compat (thin wrapper)
    def test_paren_year_wrapper_returns_first(self):
        assert seller_scan._title_paren_year(
            "Some Book (1963) (CGC 2024)"
        ) == 1963

    def test_paren_year_wrapper_none_on_empty(self):
        assert seller_scan._title_paren_year("") is None


# ─── BUI-227: era_mismatch compound / multi-year cases ───────────────────────


class TestEraMismatchCompound:
    def test_compound_paren_out_of_era_rejected(self):
        """Compound "(Marvel Comics December 2014)" for a 1963-range series → reject."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #7 (Marvel Comics December 2014)",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is True

    def test_compound_paren_in_era_kept(self):
        """Compound "(Marvel Comics October 1984)" for a 1963-1998 series → keep."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #247 (Marvel Comics October 1984)",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_multi_year_one_in_era_kept(self):
        """"(1963) (CGC 2024)" — 1963 is in range → keep (don't reject just because
        2024 is also present)."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #1 (1963) (CGC 2024)",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is False

    def test_multi_year_none_in_era_rejected(self):
        """Both years out of range → reject."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #7 (2014) (CGC 2022)",
            "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        ) is True


# ─── BUI-227: _reprint_reject ─────────────────────────────────────────────────


class TestReprintReject:
    def test_facsimile_rejected(self):
        assert seller_scan._reprint_reject(
            "Amazing Spider-Man #1 Facsimile Edition"
        ) is True

    def test_true_believers_rejected(self):
        assert seller_scan._reprint_reject(
            "True Believers Amazing Spider-Man #1"
        ) is True

    def test_marvel_tales_rejected(self):
        assert seller_scan._reprint_reject("Marvel Tales #100 reprinting ASM") is True

    def test_epic_collection_rejected(self):
        assert seller_scan._reprint_reject(
            "Amazing Spider-Man Epic Collection vol 1"
        ) is True

    def test_omnibus_rejected(self):
        assert seller_scan._reprint_reject("Avengers Omnibus vol 1 HC") is True

    def test_trade_paperback_rejected(self):
        assert seller_scan._reprint_reject(
            "X-Men Trade Paperback Days of Future Past"
        ) is True

    def test_tpb_rejected(self):
        assert seller_scan._reprint_reject("X-Men TPB NM condition") is True

    def test_2nd_printing_rejected(self):
        assert seller_scan._reprint_reject("Amazing Spider-Man #300 2nd Printing") is True

    def test_second_printing_rejected(self):
        assert seller_scan._reprint_reject(
            "Amazing Spider-Man #300 Second Printing NM"
        ) is True

    def test_case_insensitive(self):
        assert seller_scan._reprint_reject("FACSIMILE EDITION ASM #1") is True
        assert seller_scan._reprint_reject("asm #1 OMNIBUS") is True

    def test_normal_title_not_rejected(self):
        assert (
            seller_scan._reprint_reject("Amazing Spider-Man #300 NM Marvel 1988") is False
        )

    def test_variant_not_rejected(self):
        """'variant' is a legitimate first-print term — must NOT be in the lexicon."""
        assert (
            seller_scan._reprint_reject("Amazing Spider-Man #1 Variant Cover NM") is False
        )

    def test_tpb_as_substring_not_rejected(self):
        """'tpb' must only match as a whole word; 'atpb' should not trigger."""
        assert seller_scan._reprint_reject("atpbbook #1") is False

    def test_empty_title_not_rejected(self):
        assert seller_scan._reprint_reject("") is False


# ─── BUI-227: verify_with_claude prompt enrichment ───────────────────────────


class TestVerifyWithClaudePromptEnrichment:
    """_series_name carried on candidates lands in the prompt as 'Correct series:' line."""

    @pytest.fixture(autouse=True)
    def _set_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    def _capture_prompt(self, matches, monkeypatch):
        """Run verify_with_claude with mocked client; return the prompt string sent."""
        prompts_seen = []

        def fake_create(**kwargs):
            prompts_seen.append(kwargs["messages"][0]["content"])
            resp = MagicMock()
            resp.content = [MagicMock(text="[]")]
            return resp

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = fake_create
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)
        seller_scan.verify_with_claude(matches)
        return prompts_seen[0]

    def test_series_name_present_adds_correct_series_line(self, monkeypatch):
        """When _series_name is non-empty the prompt block includes the line."""
        matches = [{
            "title": "Amazing Spider-Man #7",
            "wish_name": "Amazing Spider-Man #7",
            "_series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
        }]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "Correct series: The Amazing Spider-Man (Vol. 1) (1963 - 1998)" in prompt

    def test_series_name_absent_no_correct_series_line(self, monkeypatch):
        """When _series_name is missing the 'Correct series:' line is omitted."""
        matches = [{"title": "Amazing Spider-Man #7", "wish_name": "Amazing Spider-Man #7"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "Correct series:" not in prompt

    def test_series_name_none_no_correct_series_line(self, monkeypatch):
        """Explicit _series_name=None also omits the line."""
        matches = [{
            "title": "Amazing Spider-Man #7",
            "wish_name": "Amazing Spider-Man #7",
            "_series_name": None,
        }]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "Correct series:" not in prompt

    def test_rejects_only_parsing_still_works(self, monkeypatch):
        """The enriched prompt doesn't break the JSON rejects-only parsing contract."""
        matches = [
            {
                "title": "Amazing Spider-Man #7",
                "wish_name": "Amazing Spider-Man #7",
                "_series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
            },
            {
                "title": "Daredevil Annual #1",
                "wish_name": "Daredevil #1",
                "_series_name": "Daredevil (1964 - 1998)",
            },
        ]
        fake_client = MagicMock()
        # Reject id=2 (Daredevil Annual)
        fake_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text='[{"id":2,"reason":"annual vs regular"}]')]
        )
        monkeypatch.setattr(seller_scan.anthropic, "Anthropic", lambda: fake_client)
        monkeypatch.setattr(seller_scan, "_load_dotenv", lambda *a, **k: None)

        kept = seller_scan.verify_with_claude(matches)
        assert len(kept) == 1
        assert kept[0]["title"] == "Amazing Spider-Man #7"
