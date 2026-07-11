"""Tests for seller_scan and the ebay_fetch seller search functions."""

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

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

    # ── BUI-311: network-exception guard ────────────────────────────────────

    @patch("ebay_fetch.requests.get")
    def test_network_error_on_second_page_returns_first_page(self, mock_get, capsys):
        """A ConnectionError mid-pagination must degrade gracefully — return
        the (seller-filtered) items collected so far rather than raising,
        mirroring search_by_keyword's RequestException handling."""
        page1 = self._mock_page([self._item(i) for i in range(200)], 400)
        mock_get.side_effect = [page1, requests.exceptions.ConnectionError("no route to host")]
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert len(result) == 200
        assert "Network error" in capsys.readouterr().err

    @patch("ebay_fetch.requests.get")
    def test_network_error_on_first_page_returns_empty(self, mock_get, capsys):
        """A ConnectionError on the very first request must fail this seller
        scan, not crash the run — same contract as an HTTP error response."""
        mock_get.side_effect = requests.exceptions.ConnectionError("no route to host")
        result = ebay_fetch.search_seller_listings("seller", "tok", "https://api.ebay.com")
        assert result == []
        assert "Network error" in capsys.readouterr().err


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

    def test_special_cover_not_rejected_for_regular_series(self):
        # BUI-221 Finding 3: bare \bspecial\b was too broad — "Special Edition
        # cover" / "Holiday Special variant" are legitimate descriptors on
        # original single issues.  Removed from _EDITION_PATTERNS so these
        # titles are not hard-rejected before Claude can verify them.
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man #5 Special Edition Cover", "Amazing Spider-Man", "5"
        )

    def test_holiday_special_variant_not_rejected(self):
        # Another "special" descriptor that is a valid cover variant, not an
        # edition type.
        assert not seller_scan.hard_reject(
            "X-Men #94 Holiday Special Variant Cover", "X-Men", "94"
        )

    def test_special_kept_when_series_contains_special(self):
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man Special #5", "Amazing Spider-Man Special", "5"
        )

    def test_annual_still_rejected_for_regular_series_after_special_removal(self):
        # Confirm Annual (a true edition type) still rejects even after the
        # \bspecial\b removal.
        assert seller_scan.hard_reject(
            "Avengers Annual #10", "The Avengers", "10"
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
        # "#1-#10" issue range is a multi-comic lot; # on the first number anchors it.
        assert seller_scan.hard_reject(
            "X-Men #1-#10 Bronze Age Lot", "X-Men", "1"
        )

    def test_hash_anchored_bare_end_range_rejected(self):
        # "#1-10" (# only on first number) is still a lot.
        assert seller_scan.hard_reject(
            "Amazing Spider-Man #129-150 Bronze Age", "Amazing Spider-Man", "129"
        )

    def test_bare_number_range_not_rejected_as_lot(self):
        # BUI-221 Finding 1: bare digits without a leading '#' (e.g. "129-150")
        # look like a year range or price span — no longer treated as a lot signal
        # to avoid false-rejecting single-issue titles that include run years.
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man 129-150 Bronze Age", "Amazing Spider-Man", "129"
        )

    def test_run_year_range_in_title_not_lot(self):
        # BUI-221 Finding 1 regression: "(YYYY-YYYY)" series run info in a
        # single-issue title must NOT be mistaken for an issue-range lot.
        assert not seller_scan.hard_reject(
            "Uncanny X-Men #266 (1981-2011) 1st Gambit", "Uncanny X-Men", "266"
        )

    # ── BUI-243: quantity-word-prefixed range + complete-set signals ──────────

    def test_batman_dkr_books_range_rejected(self):
        # BUI-243: "Books 1-4" is a full-run bundle, not the single #4.
        title = "Batman: The Dark Knight Returns Books 1-4 issues 2-4=1st print 1=2nd print"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(
            title, "Batman: The Dark Knight Returns", "4"
        )

    def test_lot_re_books_range(self):
        # BUI-243: "Books 1-4" matched by _LOT_RE.
        assert seller_scan._LOT_RE.search("Batman Books 1-4")

    def test_lot_re_issues_range(self):
        # BUI-243: "issues 2-4" matched by _LOT_RE.
        assert seller_scan._LOT_RE.search("Dark Knight issues 2-4 1st print")

    def test_lot_re_issue_range_singular(self):
        # BUI-243: singular "issue 1-6" matched by _LOT_RE.
        assert seller_scan._LOT_RE.search("Spider-Man issue 1-6 lot")

    def test_lot_re_issues_through(self):
        # BUI-243: "issues 1 through 6" matched by _LOT_RE.
        assert seller_scan._LOT_RE.search("X-Men issues 1 through 6 bronze age")

    def test_lot_re_books_through(self):
        # BUI-243: "books 1 through 4" matched by _LOT_RE.
        assert seller_scan._LOT_RE.search("Daredevil books 1 through 4")

    def test_lot_re_complete_set(self):
        # BUI-243: "complete set" matched by _LOT_RE.
        assert seller_scan._LOT_RE.search("Batman Year One complete set NM")

    def test_year_range_carveout_bare_no_quantity_word(self):
        # BUI-243 carve-out: a bare "YYYY-YYYY" span with NO quantity-word prefix
        # must NOT be caught by _LOT_RE (would otherwise hard-reject single issues).
        assert not seller_scan._LOT_RE.search(
            "Amazing Spider-Man #4 1962-1963 Silver Age Marvel"
        )

    def test_year_range_carveout_not_hard_rejected(self):
        # BUI-243 carve-out via hard_reject: the same title must pass through.
        assert not seller_scan.hard_reject(
            "Amazing Spider-Man #4 1962-1963 Silver Age Marvel",
            "Amazing Spider-Man",
            "4",
        )

    def test_existing_hash_range_still_rejected(self):
        # BUI-243 regression: #\d+-#?\d+ branch still fires (BUI-221 baseline).
        assert seller_scan._LOT_RE.search("Amazing Spider-Man #1-#10 Bronze Age")
        assert seller_scan._LOT_RE.search("Amazing Spider-Man #129-150 Bronze Age")

    # ── BUI-243 review fix: a YEAR span after "issue"/"book" is NOT a range ────
    # The \d{1,3} bound stops the quantity-word branch from reading a 4-digit year
    # span (e.g. "First Issue 1962-1963") as an issue range — these are genuine
    # single key issues and must NOT be dropped.

    def test_year_after_issue_word_not_lot_first_issue(self):
        # Exact review-reported title: the year span must NOT be lot-matched.
        # (hard_reject still drops it — but via the unrelated CGC-slab rule, so
        # the lot-path assertion is on a raw variant below.)
        assert not seller_scan._LOT_RE.search(
            "Amazing Spider-Man #1 First Issue 1962-1963 CGC"
        )
        raw = "Amazing Spider-Man #1 First Issue 1962-1963 Marvel"
        assert not seller_scan._LOT_RE.search(raw)
        assert not seller_scan.hard_reject(raw, "Amazing Spider-Man", "1")

    def test_year_after_issue_word_not_lot_key_issue(self):
        title = "Amazing Spider-Man #1 Key Issue 1962-1964 Marvel"
        assert not seller_scan._LOT_RE.search(title)
        assert not seller_scan.hard_reject(title, "Amazing Spider-Man", "1")

    def test_year_after_issue_word_not_lot_xmen(self):
        title = "X-Men #1 issue 1963-1964 Silver Age"
        assert not seller_scan._LOT_RE.search(title)
        assert not seller_scan.hard_reject(title, "X-Men", "1")


# ─── BUI-261: _LOT_RE missed formats (live false-negatives) ──────────────────


class TestLotReMissedFormats:
    """BUI-247 audit dry-runs found real eBay lot titles that slipped past
    _LOT_RE. Each format below is a real observed title shape; every one must
    now be detected as a lot (and therefore hard-rejected)."""

    def test_slash_list_rejected(self):
        title = "STRANGE TALES 164/165/166/168"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(title, "Strange Tales", "164")

    def test_ampersand_pair_rejected(self):
        title = "Dark Knight Returns # 1 & 3"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(
            title, "Batman: The Dark Knight Returns", "1"
        )

    def test_comma_ampersand_mixed_list_rejected(self):
        title = "ASM #64, #65 & #66"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(title, "Amazing Spider-Man", "64")

    def test_bare_comma_list_ending_in_lot_rejected(self):
        title = "Avengers 33,45,50,53,63,81,86 Marvel Silver/Bronze Age Lot"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(title, "The Avengers", "33")

    def test_dash_separated_list_rejected(self):
        title = "AVENGERS 92-93-94-95-96 Marvel Bronze Age Lot"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(title, "The Avengers", "92")

    def test_bare_hash_through_rejected(self):
        # BUI-261: no "issues"/"books" word — the pre-existing through-branch
        # required one; real titles often drop it.
        title = "The Eternals #1 through 10"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(title, "Eternals", "1")

    def test_bare_no_hash_through_rejected(self):
        title = "Eternals 1 through 10 Marvel"
        assert seller_scan._LOT_RE.search(title)
        assert seller_scan.hard_reject(title, "Eternals", "1")

    # ── A 2-member ampersand pair is unambiguous on its own ───────────────────

    def test_two_member_ampersand_pair_rejected(self):
        assert seller_scan._LOT_RE.search("X-Men #94 & #95")

    # ── PR review fix: "/" dropped from the 2-member branch ──────────────────
    # "/" is overloaded with ratio/incentive-variant and half-issue notation in
    # real listings — a bare 2-member slash pair must NOT be treated as a lot
    # (hard_reject must never drop a genuine single-issue copy). A 2-member
    # slash LOT ("STRANGE TALES 164/165") now falls through to Haiku instead —
    # the safe under-reject direction; 3+ member slash chains are still caught
    # by the generic list branch (see test_slash_list_rejected above).

    def test_two_member_slash_pair_not_rejected(self):
        assert not seller_scan._LOT_RE.search("Amazing Spider-Man #1/2")

    def test_ratio_variant_1_of_100_not_rejected(self):
        title = "Amazing Spider-Man #300 1/100 variant"
        assert not seller_scan._LOT_RE.search(title)
        assert not seller_scan.hard_reject(title, "Amazing Spider-Man", "300")

    def test_ratio_variant_1_of_25_not_rejected(self):
        title = "Batman #92 1/25 variant"
        assert not seller_scan._LOT_RE.search(title)
        assert not seller_scan.hard_reject(title, "Batman", "92")

    def test_ratio_variant_1_of_50_not_rejected(self):
        title = "X-Men #1 1/50 Skan variant"
        assert not seller_scan._LOT_RE.search(title)
        assert not seller_scan.hard_reject(title, "X-Men", "1")

    def test_half_issue_slash_not_rejected(self):
        # "Batman #1/2" — a Wizard-style half-issue, not a lot.
        title = "Batman #1/2"
        assert not seller_scan._LOT_RE.search(title)
        assert not seller_scan.hard_reject(title, "Batman", "1")


class TestLotCountMismatch:
    """BUI-261: lot_count_mismatch() flags a stated 'Lot of N' count that
    contradicts an explicit '#start-end' range in the same title — a parsing
    red flag surfaced for a caller to act on (fail-open, not a rejection gate:
    _LOT_RE already hard-rejects any 'lot of N' phrasing regardless)."""

    def test_count_lower_than_range_flagged(self):
        # "Lot of 11" claimed over a #1-10 range (only 10 issues) — BUI-261 example.
        assert seller_scan.lot_count_mismatch(
            "Lot of 11 Comics Amazing Spider-Man #1-10"
        ) is True

    def test_count_matches_range_not_flagged(self):
        assert seller_scan.lot_count_mismatch(
            "Lot of 10 Comics Amazing Spider-Man #1-10"
        ) is False

    def test_no_stated_count_fails_open(self):
        assert seller_scan.lot_count_mismatch(
            "Amazing Spider-Man #1-10 Marvel Lot"
        ) is False

    def test_no_explicit_range_fails_open(self):
        assert seller_scan.lot_count_mismatch(
            "Lot of 11 Amazing Spider-Man Comics"
        ) is False

    def test_malformed_range_end_before_start_fails_open(self):
        assert seller_scan.lot_count_mismatch(
            "Lot of 5 Comics Amazing Spider-Man #10-1"
        ) is False

    def test_empty_title_fails_open(self):
        assert seller_scan.lot_count_mismatch("") is False


class TestLotCountMismatchWiredIntoHardReject:
    """PR review fix: lot_count_mismatch() was defined + tested but had no
    non-test caller (dead code) — the BUI-261 AC wanted it "surfaced (logged/
    flagged), not silently trusted". hard_reject's lot branch (rule 3) is the
    single choke point both should_reject call sites already run through, so
    it now calls lot_count_mismatch() and emits a stderr warning on a hit."""

    def test_mismatch_prints_warning(self, capsys):
        title = "Lot of 11 Comics Amazing Spider-Man #1-10"
        assert seller_scan.hard_reject(title, "Amazing Spider-Man", "1") is True
        err = capsys.readouterr().err
        assert "lot count/range mismatch" in err
        assert title in err

    def test_matching_count_prints_no_warning(self, capsys):
        title = "Lot of 10 Comics Amazing Spider-Man #1-10"
        assert seller_scan.hard_reject(title, "Amazing Spider-Man", "1") is True
        err = capsys.readouterr().err
        assert "lot count/range mismatch" not in err

    def test_non_lot_title_prints_no_warning(self, capsys):
        title = "Amazing Spider-Man #300 NM Marvel 1988"
        assert seller_scan.hard_reject(title, "Amazing Spider-Man", "300") is False
        err = capsys.readouterr().err
        assert "lot count/range mismatch" not in err


class TestLotReBui243GuardsPreserved:
    """BUI-261 regression: re-assert every BUI-243 false-reject carve-out
    (year spans, SKU-adjacent digits) still passes after extending _LOT_RE
    with the new generic numeric-list/through/bare-lot branches."""

    def test_year_span_after_issue_number_still_not_lot(self):
        assert not seller_scan._LOT_RE.search(
            "Amazing Spider-Man #4 1962-1963 Silver Age Marvel"
        )

    def test_year_span_after_first_issue_word_still_not_lot(self):
        assert not seller_scan._LOT_RE.search(
            "Amazing Spider-Man #1 First Issue 1962-1963 Marvel"
        )

    def test_year_span_after_key_issue_word_still_not_lot(self):
        assert not seller_scan._LOT_RE.search(
            "Amazing Spider-Man #1 Key Issue 1962-1964 Marvel"
        )

    def test_bare_two_member_dash_range_still_not_lot(self):
        # BUI-221 carve-out: a bare 2-member dash pair with no '#' anchor and
        # no quantity word stays ambiguous (ended run years / price span).
        assert not seller_scan._LOT_RE.search(
            "Amazing Spider-Man 129-150 Bronze Age"
        )

    def test_series_run_year_range_in_parens_still_not_lot(self):
        assert not seller_scan._LOT_RE.search(
            "Uncanny X-Men #266 (1981-2011) 1st Gambit"
        )

    def test_decimal_grade_ratio_not_mistaken_for_slash_list(self):
        # BUI-261: a decimal-grade comparison ("9.4/9.6") must not be misread
        # as a 2-member slash list ("4/9") by the new generic detector.
        assert not seller_scan._LOT_RE.search(
            "CGC 9.4/9.6 slab pair Amazing Spider-Man #300"
        )

    def test_sku_letter_digit_token_not_mistaken_for_lot_member(self):
        # BUI-261: a SKU token like "X6" must not be read as a lone lot member
        # (no word boundary between a letter and an immediately-adjacent digit).
        assert not seller_scan._LOT_RE.search("Moon Knight X6 Marvel 1982")

    def test_sku_paren_code_not_mistaken_for_lot_member(self):
        assert not seller_scan._LOT_RE.search("Fantastic Four (CZ) 48 VF")


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


class TestVerifyWithClaudeChunking:
    """Verify chunked calling and fail-closed behaviour of verify_with_claude.

    BUI-270: transport is now the `claude` CLI (subscription auth), not the
    Anthropic SDK — tests monkeypatch seller_scan._verify_via_claude_cli
    directly rather than an SDK client.
    """

    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch):
        # BUI-270: verify_with_claude preflights shutil.which("claude"); stub it
        # truthy so the chunking tests exercise the loop, not the preflight.
        # (BUI-301's rejected-candidate cache is redirected globally by the
        # conftest.py autouse fixture.)
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )

    def _make_matches(self, n):
        return [
            {"title": f"Comic Series #{i}", "wish_name": f"Comic Series #{i}"}
            for i in range(1, n + 1)
        ]

    def test_candidates_chunked_at_30(self, monkeypatch):
        """BUI-297: chunk size is 30 → 250 candidates split into 9 CLI calls
        ([30]*8 + [10]), and every candidate survives a clean run."""
        matches = self._make_matches(250)
        calls = []

        def fake_verify(prompt):
            calls.append(prompt)
            return "[]"

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert len(calls) == 9  # ceil(250 / 30)
        assert len(kept) == 250
        assert dropped == []

    def test_chunk_indices_are_local_not_global(self, monkeypatch):
        """Each chunk's prompt uses 1-based indices local to that chunk, not
        global position, so verdicts correlate correctly across chunk boundaries."""
        matches = self._make_matches(60)  # 2 chunks: [30, 30]

        # Chunk 2: reject ids 2-30 (local), keep only id=1 (local) →
        # corresponds to global match #31.
        rejected_chunk2 = [{"id": i, "reason": "test"} for i in range(2, 31)]
        responses = iter(["[]", json.dumps(rejected_chunk2)])

        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: next(responses)
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        # 30 from chunk 1 + 1 from chunk 2 = 31
        assert len(kept) == 31
        # The surviving item from chunk 2 is global index 31 (title "Comic Series #31")
        assert kept[-1]["title"] == "Comic Series #31"
        assert dropped == []

    def test_unparseable_chunk_is_dropped_loudly(self, monkeypatch, capsys):
        """BUI-297: a chunk whose response contains no JSON array yielded no
        usable verdict — its candidates are NEVER-VERIFIED, so they go to
        `dropped` (loud), not silently discarded."""
        matches = self._make_matches(3)
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: "I cannot help with that request.",
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert kept == []
        assert len(dropped) == 3
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_out_of_range_id_is_dropped_loudly(self, monkeypatch, capsys):
        """A rejected-id outside 1..len(chunk) is an unparseable/invalid verdict
        → the whole chunk is dropped (never verified), not silently discarded."""
        matches = self._make_matches(5)
        # id=6 is out of range for a 5-candidate chunk.
        rejected = [{"id": 6, "reason": "phantom id"}]
        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: json.dumps(rejected)
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert kept == []
        assert len(dropped) == 5
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_non_int_id_is_dropped_loudly(self, monkeypatch, capsys):
        """A rejected-id that is not an integer is an invalid verdict → the
        whole chunk is dropped (never verified)."""
        matches = self._make_matches(5)
        rejected = [{"id": "two", "reason": "string id"}]
        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: json.dumps(rejected)
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert kept == []
        assert len(dropped) == 5
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_missing_id_key_is_dropped_loudly(self, monkeypatch, capsys):
        """A rejected object with no 'id' key is an invalid verdict → the whole
        chunk is dropped (never verified)."""
        matches = self._make_matches(5)
        rejected = [{"reason": "forgot the id"}]
        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: json.dumps(rejected)
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert kept == []
        assert len(dropped) == 5
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_empty_array_keeps_all_candidates(self, monkeypatch):
        """[] response means nothing rejected — all candidates kept, none dropped."""
        matches = self._make_matches(5)
        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", lambda prompt: "[]")

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert len(kept) == 5
        assert dropped == []

    def test_good_chunk_after_bad_chunk_still_kept(self, monkeypatch, capsys):
        """A bad first chunk drops its candidates but a good second chunk is kept."""
        matches = self._make_matches(60)  # 2 chunks: [30, 30]
        responses = iter(["no json here", "[]"])  # 2nd chunk: nothing rejected → all 30 kept

        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: next(responses)
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        # First 30 dropped (bad chunk); last 30 kept.
        assert len(kept) == 30
        assert kept[0]["title"] == "Comic Series #31"
        assert len(dropped) == 30
        err = capsys.readouterr().err
        assert "fail-closed" in err

    def test_all_chunks_transport_fail_hard_exits(self, monkeypatch, capsys):
        """BUI-270 safety net: if EVERY chunk fails at the transport layer (zero
        successful model calls) the verifier is globally broken (bad auth / all
        timeouts). Hard-fail with SystemExit(1) rather than return an empty list
        the caller would render as a genuine "no matches" table."""
        matches = self._make_matches(150)  # 2 chunks — both fail transport

        def fake_verify(prompt):
            raise RuntimeError("claude CLI failed: not logged in")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        with pytest.raises(SystemExit) as exc:
            seller_scan.verify_with_claude(matches)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "every candidate chunk" in err

    def test_single_chunk_transport_fail_hard_exits(self, monkeypatch, capsys):
        """A one-chunk run where that only chunk fails transport is still an
        all-chunks-failed run → SystemExit(1), not an empty return."""
        matches = self._make_matches(3)  # 1 chunk

        def fake_verify(prompt):
            raise RuntimeError("claude CLI timed out: ...")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        with pytest.raises(SystemExit) as exc:
            seller_scan.verify_with_claude(matches)
        assert exc.value.code == 1

    def test_missing_claude_cli_preflight_exits(self, monkeypatch, capsys):
        """BUI-270 preflight: `claude` absent from PATH → SystemExit(1) with an
        actionable message, before any chunk is attempted — never an empty table
        that reads as no-match."""
        # Override the autouse truthy stub: simulate claude not installed.
        monkeypatch.setattr(seller_scan.shutil, "which", lambda name: None)
        called = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: called.append(prompt) or "[]",
        )

        with pytest.raises(SystemExit) as exc:
            seller_scan.verify_with_claude(self._make_matches(3))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "`claude` CLI was not found on PATH" in err
        # Preflight fires before the loop — the transport is never called.
        assert called == []


# ─── Rejected-candidate cache (BUI-301) ────────────────────────────────────────


class TestRejectedCandidateCache:
    """BUI-301: a model-REJECTED (listing, wish) pair is cached so a repeat
    scan skips re-verifying it until the cache entry's TTL expires. Separate
    concerns from the genuine seen-set: a genuine match must never be cached
    here, and a never-verified (dropped) candidate must never be cached either
    — it has to resurface for verification on the very next run. The cache is
    opt-in (use_rejected_cache=True); seller-scan opts in.
    """

    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch):
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )
        # The conftest.py autouse fixture already redirected the rejected-cache
        # to a per-test tmp path; capture that path for this class's direct
        # cache writes rather than patching it a second time.
        self.cache_path = seller_scan._REJECTED_CACHE_PATH

    def _make_match(self, item_id, n=1, wish_name=None):
        return {
            "item_id": item_id,
            "title": f"Comic Series #{n}",
            "wish_name": wish_name if wish_name is not None else f"Comic Series #{n}",
        }

    def _key(self, item_id, n=1, wish_name=None):
        return seller_scan._rejected_cache_key(
            item_id, wish_name if wish_name is not None else f"Comic Series #{n}"
        )

    def _write_cache(self, entries):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(entries))

    def _verify(self, matches):
        return seller_scan.verify_with_claude(matches, use_rejected_cache=True)

    def test_rejected_candidate_within_ttl_is_skipped(self, monkeypatch):
        """A pair cached as rejected less than the TTL ago is skipped entirely
        — no CLI call, and it appears in none of kept/dropped/filtered."""
        now = datetime.now(timezone.utc)
        self._write_cache({self._key("111"): now.isoformat()})

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        kept, dropped, filtered = self._verify([self._make_match("111")])

        assert calls == []
        assert kept == []
        assert dropped == []
        assert filtered == []

    def test_rejected_candidate_just_under_ttl_is_skipped(self, monkeypatch):
        """TTL boundary (just-under side): an entry aged 1h short of the TTL is
        still within window and skipped — guards the `<=` prune against a
        `<`/`<=` off-by-one that the far-over-TTL test alone wouldn't catch."""
        fresh = datetime.now(timezone.utc) - timedelta(
            seconds=seller_scan._REJECTED_CACHE_TTL_SEC - 3600
        )
        self._write_cache({self._key("111"): fresh.isoformat()})

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        kept, dropped, filtered = self._verify([self._make_match("111")])

        assert calls == []
        assert kept == []

    def test_rejected_candidate_past_ttl_is_reverified(self, monkeypatch):
        """A cache entry older than _REJECTED_CACHE_TTL_SEC no longer
        suppresses verification — the candidate is sent to the CLI again,
        giving a transiently-misjudged or since-edited listing another chance."""
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=seller_scan._REJECTED_CACHE_TTL_SEC + 3600
        )
        self._write_cache({self._key("111"): stale.isoformat()})

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        kept, dropped, filtered = self._verify([self._make_match("111")])

        assert len(calls) == 1
        assert len(kept) == 1
        assert kept[0]["item_id"] == "111"
        assert dropped == []
        assert filtered == []

    def test_future_dated_entry_is_evicted_not_wedged(self, monkeypatch):
        """A future-dated timestamp (clock skew / hand-edit) has a negative age;
        it must be treated as expired and re-verified, never suppress a
        candidate forever (adversarial: no lower age bound would wedge it)."""
        future = datetime.now(timezone.utc) + timedelta(days=365)
        self._write_cache({self._key("111"): future.isoformat()})

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        kept, dropped, filtered = self._verify([self._make_match("111")])

        assert len(calls) == 1
        assert len(kept) == 1

    def test_rejection_of_one_wish_does_not_suppress_a_different_wish(
        self, monkeypatch
    ):
        """Cross-pairing invariant (correctness + adversarial): a listing
        rejected against wish A must NOT be suppressed when a later run re-pairs
        the SAME item_id to a genuine wish B — the cache key is the (item_id,
        wish_name) pair, not item_id alone."""
        now = datetime.now(timezone.utc)
        # item 111 was rejected against wish "Wrong Series #5" last run.
        self._write_cache(
            {self._key("111", wish_name="Wrong Series #5"): now.isoformat()}
        )

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        # This run, the same listing best-matches a DIFFERENT (genuine) wish.
        kept, dropped, filtered = self._verify(
            [self._make_match("111", wish_name="Right Series #5")]
        )

        assert len(calls) == 1  # re-verified, not skipped
        assert len(kept) == 1
        assert kept[0]["item_id"] == "111"

    def test_genuine_match_is_unaffected(self, monkeypatch):
        """A candidate with no rejected-cache entry is verified normally, and a
        kept genuine match must never be written into the rejected cache."""
        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", lambda prompt: "[]")

        kept, dropped, filtered = self._verify([self._make_match("222")])

        assert len(kept) == 1
        assert dropped == []
        assert filtered == []
        assert seller_scan._load_rejected_cache() == {}

    def test_model_rejection_is_cached_for_next_run(self, monkeypatch):
        """A candidate the model explicitly rejects this run is written to the
        rejected cache (keyed by the (item_id, wish_name) pair) so a subsequent
        scan can skip it."""
        rejected = [{"id": 1, "reason": "wrong series"}]
        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: json.dumps(rejected)
        )

        kept, dropped, filtered = self._verify([self._make_match("333")])

        assert kept == []
        assert dropped == []
        assert len(filtered) == 1
        assert self._key("333") in seller_scan._load_rejected_cache()

    def test_partial_cache_skips_only_the_cached_pair(self, monkeypatch):
        """A mixed batch: one pair is cache-skipped while the other is verified
        — exercises the skip/verify partition loop with a real split."""
        now = datetime.now(timezone.utc)
        self._write_cache({self._key("111", n=1): now.isoformat()})

        prompts = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: prompts.append(prompt) or "[]",
        )

        kept, dropped, filtered = self._verify(
            [self._make_match("111", n=1), self._make_match("222", n=2)]
        )

        # Only the uncached candidate reaches the CLI and is kept.
        assert len(prompts) == 1
        assert [c["item_id"] for c in kept] == ["222"]
        assert "Comic Series #2" in prompts[0]
        assert "Comic Series #1" not in prompts[0]

    def test_stats_out_param_reports_skipped_count(self, monkeypatch):
        """BUI-317: when a `stats` dict is passed, verify_with_claude sets
        stats["skipped"] to the number of candidates the rejected cache
        skipped — the count a --json caller needs for coverage visibility.
        """
        now = datetime.now(timezone.utc)
        self._write_cache({self._key("111", n=1): now.isoformat()})

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", lambda prompt: "[]")

        stats = {}
        kept, dropped, filtered = seller_scan.verify_with_claude(
            [self._make_match("111", n=1), self._make_match("222", n=2)],
            use_rejected_cache=True,
            stats=stats,
        )

        assert stats["skipped"] == 1
        assert [c["item_id"] for c in kept] == ["222"]

    def test_stats_out_param_on_all_cached_early_return(self, monkeypatch):
        """BUI-317: when EVERY candidate is cache-skipped, verify_with_claude
        takes the `if not to_verify: return [], [], []` early return — a
        distinct path from the empty-`matches` one. stats["skipped"] must
        still carry the real count there (a reordering that set it after the
        early return would silently report 0 for a fully-cached seller)."""
        now = datetime.now(timezone.utc)
        self._write_cache({
            self._key("111", n=1): now.isoformat(),
            self._key("222", n=2): now.isoformat(),
        })

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        stats = {}
        result = seller_scan.verify_with_claude(
            [self._make_match("111", n=1), self._make_match("222", n=2)],
            use_rejected_cache=True,
            stats=stats,
        )

        assert calls == []  # every candidate skipped → no CLI call
        assert result == ([], [], [])
        assert stats["skipped"] == 2

    def test_stats_out_param_zero_when_nothing_cached(self, monkeypatch):
        """No cache hits → stats["skipped"] is 0, not omitted — a caller must
        always find a value, including the common healthy-scan case."""
        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", lambda prompt: "[]")

        stats = {}
        seller_scan.verify_with_claude(
            [self._make_match("222")], use_rejected_cache=True, stats=stats
        )

        assert stats["skipped"] == 0

    def test_stats_out_param_zero_on_empty_matches(self):
        """The `if not matches` early return also populates stats["skipped"]
        rather than leaving the dict unset."""
        stats = {}
        result = seller_scan.verify_with_claude([], use_rejected_cache=True, stats=stats)

        assert result == ([], [], [])
        assert stats["skipped"] == 0

    def test_stats_out_param_defaults_to_none_no_error(self, monkeypatch):
        """Omitting `stats` (the default, and every pre-BUI-317 call site)
        must not raise — it's a pure opt-in out-parameter."""
        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", lambda prompt: "[]")
        kept, dropped, filtered = self._verify([self._make_match("222")])
        assert len(kept) == 1

    def test_disabled_by_default_does_not_read_or_write_cache(self, monkeypatch):
        """use_rejected_cache defaults False (the wishlist_sellers path): a
        pre-existing rejection must NOT skip, and a fresh rejection must NOT be
        persisted — the shared verifier's other caller is unaffected."""
        now = datetime.now(timezone.utc)
        self._write_cache({self._key("111"): now.isoformat()})

        rejected = [{"id": 1, "reason": "wrong series"}]
        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or json.dumps(rejected),
        )

        # Default call (no use_rejected_cache) — item 111 is NOT skipped despite
        # the cache entry, and the new rejection does NOT overwrite the cache.
        kept, dropped, filtered = seller_scan.verify_with_claude(
            [self._make_match("111")]
        )

        assert len(calls) == 1  # verified despite the cached rejection
        assert len(filtered) == 1
        # Cache file untouched: still only the original entry, no new write.
        assert seller_scan._load_rejected_cache() == {
            self._key("111"): now.isoformat()
        }

    def test_corrupt_cache_file_is_treated_as_empty(self, monkeypatch):
        """A corrupt / non-JSON cache file degrades to {} (never raises) so
        verification proceeds normally."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text("{ this is not valid json")

        calls = []
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: calls.append(prompt) or "[]",
        )

        kept, dropped, filtered = self._verify([self._make_match("111")])

        assert seller_scan._load_rejected_cache() == {}
        assert len(calls) == 1  # not skipped — corrupt cache = empty
        assert len(kept) == 1

    def test_non_dict_cache_file_is_treated_as_empty(self):
        """A JSON file that parses to a non-dict (e.g. a list) is ignored."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(["not", "a", "dict"]))
        assert seller_scan._load_rejected_cache() == {}

    def test_unparseable_timestamp_is_evicted(self):
        """An entry with a non-ISO timestamp is treated as infinitely old and
        dropped on load — a corrupt entry can't wedge a pair out of
        re-verification forever."""
        self._write_cache({self._key("111"): "not-a-timestamp"})
        assert seller_scan._load_rejected_cache() == {}

    def test_save_helper_swallows_oserror(self, monkeypatch, capsys):
        """_save_rejected_cache warns and returns on an OSError (disk full,
        read-only FS, permission) — never propagates. The cache is a cost
        optimization; a failed write must fall open to normal re-verification."""
        def raise_oserror(*a, **k):
            raise OSError("read-only file system")

        monkeypatch.setattr(seller_scan.Path, "write_text", raise_oserror)

        # Must not raise.
        seller_scan._save_rejected_cache({self._key("111"): "2026-07-11T00:00:00+00:00"})

        err = capsys.readouterr().err
        assert "could not persist rejected-candidate cache" in err

    def test_save_failure_does_not_crash_verify(self, monkeypatch):
        """End-to-end: an OSError persisting a fresh rejection must not abort
        verify_with_claude — the rejection is still returned in `filtered`, the
        scan continues (a multi-seller batch must not crash before printing)."""
        rejected = [{"id": 1, "reason": "wrong series"}]
        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: json.dumps(rejected)
        )
        monkeypatch.setattr(
            seller_scan.Path,
            "write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        kept, dropped, filtered = self._verify([self._make_match("444")])

        assert kept == []
        assert len(filtered) == 1


class TestVerifyViaClaudeCli:
    """BUI-270: the `claude` CLI transport helper itself — nonzero exit,
    timeout, and empty output must all raise RuntimeError so verify_with_claude's
    fail-closed except clause catches them."""

    def test_success_returns_stdout(self, monkeypatch):
        def fake_run(cmd, input, capture_output, text, timeout):
            assert cmd[0] == "claude"
            assert "--model" in cmd
            assert "claude-haiku-4-5-20251001" in cmd
            assert input == "some prompt"
            return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        result = seller_scan._verify_via_claude_cli("some prompt")

        assert result == "[]\n"

    def test_nonzero_exit_raises_runtime_error(self, monkeypatch):
        def fake_run(cmd, input, capture_output, text, timeout):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="not logged in"
            )

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="not logged in"):
            seller_scan._verify_via_claude_cli("some prompt")

    def test_timeout_raises_verify_timeout(self, monkeypatch):
        """BUI-297: a timeout raises the _VerifyTimeout subclass (still a
        RuntimeError) so the caller can bisect-retry only on timeouts."""
        def fake_run(cmd, input, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        with pytest.raises(seller_scan._VerifyTimeout, match="timed out"):
            seller_scan._verify_via_claude_cli("some prompt")

    def test_nonzero_exit_is_not_a_verify_timeout(self, monkeypatch):
        """BUI-297: a nonzero exit raises a plain RuntimeError, NOT _VerifyTimeout
        — so it is dropped without a (useless, load-amplifying) bisection retry."""
        def fake_run(cmd, input, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError) as exc:
            seller_scan._verify_via_claude_cli("some prompt")
        assert not isinstance(exc.value, seller_scan._VerifyTimeout)

    def test_empty_stdout_raises_runtime_error(self, monkeypatch):
        def fake_run(cmd, input, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 0, stdout="   \n", stderr="")

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="empty output"):
            seller_scan._verify_via_claude_cli("some prompt")


class TestVerifyWithClaudeNoSilentDrop:
    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch):
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )

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
        monkeypatch.setattr(
            seller_scan, "_verify_via_claude_cli", lambda prompt: verdict_json
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        # Only the genuine match is returned (unchanged behaviour).
        assert [m["title"] for m in kept] == ["Amazing Spider-Man #300 NM Marvel 1988"]
        # BUI-297: a genuine model rejection is NOT a dropped/never-verified
        # candidate — it stays a safe silent drop and never resurfaces.
        assert dropped == []
        # The rejected one is surfaced to stderr with its reason.
        err = capsys.readouterr().err
        assert "Filtered 1 likely false positive" in err
        assert "Daredevil Annual #1" in err
        assert "Annual, not the regular series" in err


# ─── BUI-297: bisection retry + dropped (never-verified) candidates ──────────


class TestVerifyBisectionAndDrops:
    """BUI-297: a chunk-level transport timeout must (a) bisect-retry before
    giving up and (b) surface any candidate it still can't verify as `dropped`
    (loud), distinct from a genuine model rejection (silent, safe)."""

    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch):
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )

    def _make_matches(self, n):
        return [
            {"title": f"Comic Series #{i}", "wish_name": f"Comic Series #{i}"}
            for i in range(1, n + 1)
        ]

    def test_bisection_recovers_a_chunk_that_times_out_whole(self, monkeypatch, capsys):
        """A single 30-candidate chunk times out as a whole, but both halves
        succeed on retry → all 30 kept, none dropped, no SystemExit."""
        matches = self._make_matches(30)  # 1 chunk
        state = {"first": True}

        def fake_verify(prompt):
            # Fail only the very first (full-chunk) call; the two halves succeed.
            if state["first"]:
                state["first"] = False
                raise seller_scan._VerifyTimeout("claude CLI timed out: chunk too big")
            return "[]"

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        assert len(kept) == 30
        assert dropped == []
        err = capsys.readouterr().err
        assert "bisecting and retrying each half" in err

    def test_bisection_isolates_a_poison_candidate_and_keeps_the_rest(
        self, monkeypatch
    ):
        """BUI-297 headline feature: when only part of a chunk keeps timing out,
        bisection isolates the failing region and KEEPS the rest of the chunk —
        it does not drop all 30.  Here candidate #1 is a 'poison pill' that times
        out at every size; bisection narrows the drop to a small group bounded by
        the recursion-depth cap, and the other ~27 candidates survive."""
        matches = self._make_matches(30)  # 1 chunk

        def fake_verify(prompt):
            # Any (sub-)chunk still containing candidate #1 times out; every
            # other sub-chunk verifies fine.
            if 'Comic Series #1"' in prompt:
                raise seller_scan._VerifyTimeout("claude CLI timed out: poison #1")
            return "[]"

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        # The poison candidate is isolated into the dropped group; the rest keep.
        assert any(c["title"] == "Comic Series #1" for c in dropped)
        kept_titles = {c["title"] for c in kept}
        assert "Comic Series #4" in kept_titles
        # Depth-bounded isolation: the drop is a small group, not the whole chunk.
        assert len(dropped) <= 4
        assert len(kept) + len(dropped) == 30

    def test_persistent_timeout_bisects_to_floor_then_drops(self, monkeypatch, capsys):
        """A chunk that times out at EVERY size bisects down to the depth cap and
        counts each candidate as dropped (never verified). Because another
        chunk succeeds (transport reachable), there is no global SystemExit."""
        matches = self._make_matches(60)  # 2 chunks of 30

        call_count = {"n": 0}

        def fake_verify(prompt):
            call_count["n"] += 1
            # Chunk 1 (first full-size call) succeeds; chunk 2 always times out.
            if call_count["n"] == 1:
                return "[]"
            raise seller_scan._VerifyTimeout("claude CLI timed out")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        # Chunk 1 fully kept; chunk 2 entirely dropped (never verified).
        assert len(kept) == 30
        assert len(dropped) == 30
        # Dropped candidates are exactly the second chunk's global range.
        assert {c["title"] for c in dropped} == {
            f"Comic Series #{i}" for i in range(31, 61)
        }
        err = capsys.readouterr().err
        assert "counting as DROPPED" in err

    def test_non_timeout_transport_error_is_not_bisected(self, monkeypatch, capsys):
        """BUI-297 reliability: a non-timeout transport failure (nonzero exit /
        auth / rate-limit, surfaced as a plain RuntimeError) is dropped WITHOUT
        bisection — retrying smaller chunks can't help and only amplifies load.
        The single 30-candidate chunk makes exactly ONE CLI call, then drops."""
        matches = self._make_matches(30)  # 1 chunk
        calls = []

        def fake_verify(prompt):
            calls.append(prompt)
            raise RuntimeError("claude CLI failed: not logged in")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        with pytest.raises(SystemExit) as exc:  # all-failed → global safety net
            seller_scan.verify_with_claude(matches)
        assert exc.value.code == 1
        # No bisection: exactly one attempt for the whole chunk.
        assert len(calls) == 1

    def test_circuit_breaker_skips_remaining_chunks_when_transport_dead(
        self, monkeypatch
    ):
        """BUI-297 efficiency guard: once a whole chunk yields zero successful
        calls, the verifier is globally broken — remaining chunks are NOT
        attempted, and the run hard-exits.  Without the breaker a dead transport
        would repeat the per-chunk fan-out across every chunk."""
        matches = self._make_matches(90)  # 3 chunks of 30
        prompts = []

        def fake_verify(prompt):
            prompts.append(prompt)
            raise seller_scan._VerifyTimeout("claude CLI timed out")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        with pytest.raises(SystemExit) as exc:
            seller_scan.verify_with_claude(matches)
        assert exc.value.code == 1

        # Only chunk 1 (candidates #1–#30) was ever attempted; chunks 2 and 3
        # (which contain "#61") were skipped by the circuit breaker.
        assert not any("Comic Series #61" in p for p in prompts)

    def test_bisection_fan_out_is_depth_bounded(self, monkeypatch):
        """BUI-297 reliability: a verifier that times out on EVERY call cannot
        fan out unbounded — the recursion-depth cap limits a single 30-candidate
        chunk to at most 2^(depth+1)-1 = 15 CLI calls before dropping the rest."""
        matches = self._make_matches(30)  # 1 chunk
        calls = []

        def fake_verify(prompt):
            calls.append(prompt)
            raise seller_scan._VerifyTimeout("claude CLI timed out")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        with pytest.raises(SystemExit):  # all-failed → global safety net
            seller_scan.verify_with_claude(matches)
        # Depth cap 3 → at most 1 + 2 + 4 + 8 = 15 calls for the one chunk.
        assert len(calls) <= 15

    def test_model_rejection_and_drop_tracked_separately(self, monkeypatch, capsys):
        """AC #1: in one run, a genuine model rejection (silent) and a
        never-verified timeout (loud drop) are tracked separately — the reject
        reduces `kept` but stays out of `dropped`; the timeout populates
        `dropped` only."""
        matches = self._make_matches(60)  # 2 chunks of 30

        call_count = {"n": 0}

        def fake_verify(prompt):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Chunk 1: model rejects local id 5 (a real judgement) → silent.
                return '[{"id": 5, "reason": "wrong series"}]'
            # Chunk 2 (and its bisected retries): always times out.
            raise seller_scan._VerifyTimeout("claude CLI timed out")

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)

        # Chunk 1: 30 - 1 rejected = 29 kept, and the rejected one is NOT dropped.
        assert len(kept) == 29
        # Chunk 2: all 30 never verified → dropped.
        assert len(dropped) == 30
        err = capsys.readouterr().err
        assert "Filtered 1 likely false positive" in err  # silent model reject
        assert "counting as DROPPED" in err                # loud never-verified


class TestMainDroppedCandidatesExit:
    """BUI-297/BUI-298 end-to-end through main(): a run with never-verified
    candidates exits non-zero and reports incompleteness per-seller inside
    the always-object --json shape; only kept matches are ever recorded seen.
    """

    def _wire_main(
        self, monkeypatch, verify_return_by_username, record_sink,
        wish_fetch_calls=None, token_calls=None,
    ):
        """Stub out every external seam so main() reaches verify_with_claude
        for each seller with two per-seller candidates (item_ids
        "<username>-A1"/"<username>-A2"), then returns
        `verify_return_by_username[username](cands)` from verify_with_claude.

        `wish_fetch_calls`/`token_calls`, if given, are lists appended to on
        each fetch_wish_list()/get_token() call — used to assert single-fetch
        behavior across a multi-seller batch (BUI-298 requirement #1).
        """
        monkeypatch.setattr(seller_scan, "load_seller_aliases", lambda: {})
        # BUI-298: echo the seller arg back as the "resolved" username so a
        # multi-seller test can address each seller's stubbed listings/verify
        # behavior by name.
        monkeypatch.setattr(
            seller_scan,
            "resolve_seller_username",
            lambda seller, aliases, username_override=None: seller,
        )
        monkeypatch.setattr(
            seller_scan, "load_config", lambda: ("id", "secret", "http://x")
        )

        def fake_get_token(*a, **k):
            if token_calls is not None:
                token_calls.append(1)
            return "tok"

        monkeypatch.setattr(seller_scan, "get_token", fake_get_token)

        def fake_fetch_wish_list():
            if wish_fetch_calls is not None:
                wish_fetch_calls.append(1)
            return [{"id": 1, "name": "Amazing Spider-Man #300"}]

        monkeypatch.setattr(seller_scan, "fetch_wish_list", fake_fetch_wish_list)

        def fake_search(username, token, base_url, max_results=1000):
            return [
                {"item_id": f"{username}-A1", "title": "Amazing Spider-Man #300 one"},
                {"item_id": f"{username}-A2", "title": "Amazing Spider-Man #300 two"},
            ]

        monkeypatch.setattr(seller_scan, "search_seller_listings", fake_search)
        monkeypatch.setattr(seller_scan, "parse_item_summary", lambda raw: dict(raw))
        monkeypatch.setattr(
            seller_scan, "match_listing", lambda title, wish_items: (wish_items[0], 0.9)
        )
        monkeypatch.setattr(seller_scan, "should_reject", lambda *a, **k: False)
        monkeypatch.setattr(seller_scan, "fetch_seen_item_ids", lambda seller: set())

        def fake_verify(cands, **kwargs):
            # BUI-301: _scan_one_seller passes use_rejected_cache=True; accept
            # and ignore it here (the cache is unit-tested separately).
            username = cands[0]["item_id"].rsplit("-", 1)[0]
            return verify_return_by_username[username](cands)

        monkeypatch.setattr(seller_scan, "verify_with_claude", fake_verify)
        monkeypatch.setattr(
            seller_scan,
            "record_items_seen",
            lambda ids, seller: record_sink.setdefault(seller, []).extend(ids),
        )

    def test_dropped_run_exits_3_object_json_and_seen_invariant(
        self, monkeypatch, capsys
    ):
        recorded = {}
        # First candidate kept (verified), second dropped (never verified).
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "seller1": lambda cands: (cands[:1], cands[1:], [])
            },
            record_sink=recorded,
        )

        code = seller_scan.main(["seller1", "--json"])

        # AC #2: a distinct non-zero exit code.
        assert code == 3

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        # BUI-298 fold-in A: --json is ALWAYS a top-level object.
        assert isinstance(payload, dict)
        assert payload["incomplete"] is True
        assert len(payload["sellers"]) == 1
        seller_result = payload["sellers"][0]
        assert seller_result["seller"] == "seller1"
        assert seller_result["username"] == "seller1"
        assert seller_result["incomplete"] is True
        assert seller_result["error"] is None
        assert len(seller_result["matches"]) == 1
        assert len(seller_result["dropped_candidates"]) == 1
        assert seller_result["matches"][0]["item_id"] == "seller1-A1"
        assert seller_result["dropped_candidates"][0]["item_id"] == "seller1-A2"
        # Private pipeline fields are stripped from the dropped side too.
        assert not any(
            k.startswith("_") for k in seller_result["dropped_candidates"][0]
        )

        # AC #2: the loud INCOMPLETE banner tells the operator to re-run.
        assert "INCOMPLETE" in captured.err
        assert "resurface on re-run" in captured.err

        # AC #5 / BUI-298: only the kept match is recorded as seen — the
        # dropped one is never marked, so it resurfaces on re-run.
        assert recorded == {"seller1": ["seller1-A1"]}

    def test_clean_run_exits_0_with_object_json(self, monkeypatch, capsys):
        recorded = {}
        # Both candidates kept, none dropped.
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "seller1": lambda cands: (list(cands), [], [])
            },
            record_sink=recorded,
        )

        code = seller_scan.main(["seller1", "--json"])

        assert code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        # BUI-298: always an object, even on a fully clean run (no more
        # backward-compat bare-array shape).
        assert isinstance(payload, dict)
        assert payload["incomplete"] is False
        assert len(payload["sellers"]) == 1
        seller_result = payload["sellers"][0]
        assert seller_result["incomplete"] is False
        assert {row["item_id"] for row in seller_result["matches"]} == {
            "seller1-A1", "seller1-A2",
        }
        assert recorded == {"seller1": ["seller1-A1", "seller1-A2"]}

    def test_filtered_reasons_included_inline_in_json(self, monkeypatch, capsys):
        """BUI-298 requirement #2: the "Filtered N false positive(s)" reasons
        (model-rejected candidates) are available in --json output, not just
        stderr."""
        recorded = {}
        filtered_info = [{
            "item_id": "seller1-A2", "title": "Daredevil Annual #1",
            "wish_name": "Daredevil #1", "reason": "annual, not regular series",
        }]
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "seller1": lambda cands: (cands[:1], [], filtered_info)
            },
            record_sink=recorded,
        )

        code = seller_scan.main(["seller1", "--json"])
        assert code == 0

        payload = json.loads(capsys.readouterr().out)
        seller_result = payload["sellers"][0]
        assert seller_result["filtered"] == filtered_info

    def test_multi_seller_fetches_wish_list_and_token_exactly_once(
        self, monkeypatch, capsys
    ):
        """BUI-298 requirement #1: a batch of 3 sellers fetches the wish list
        + OAuth token ONCE, not once per seller."""
        recorded = {}
        wish_fetch_calls = []
        token_calls = []
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "sellerA": lambda cands: (list(cands), [], []),
                "sellerB": lambda cands: (list(cands), [], []),
                "sellerC": lambda cands: (list(cands), [], []),
            },
            record_sink=recorded,
            wish_fetch_calls=wish_fetch_calls,
            token_calls=token_calls,
        )

        code = seller_scan.main(["sellerA", "sellerB", "sellerC", "--json"])

        assert code == 0
        assert len(wish_fetch_calls) == 1
        assert len(token_calls) == 1
        payload = json.loads(capsys.readouterr().out)
        assert [s["seller"] for s in payload["sellers"]] == [
            "sellerA", "sellerB", "sellerC",
        ]
        # Each seller's seen-recording stays independent — never merged.
        assert set(recorded.keys()) == {"sellerA", "sellerB", "sellerC"}

    def test_per_seller_incomplete_propagation_overall_exit_3(
        self, monkeypatch, capsys
    ):
        """BUI-298 fold-in B: one seller in a batch is incomplete (dropped
        candidates) while another is clean — that seller's slot is flagged,
        the other stays clean, and the OVERALL exit code is still 3."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "sellerClean": lambda cands: (list(cands), [], []),
                "sellerBad": lambda cands: (cands[:1], cands[1:], []),
            },
            record_sink=recorded,
        )

        code = seller_scan.main(["sellerClean", "sellerBad", "--json"])

        assert code == 3
        payload = json.loads(capsys.readouterr().out)
        assert payload["incomplete"] is True
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        assert by_seller["sellerClean"]["incomplete"] is False
        assert by_seller["sellerClean"]["dropped_candidates"] == []
        assert by_seller["sellerBad"]["incomplete"] is True
        assert len(by_seller["sellerBad"]["dropped_candidates"]) == 1
        # Seen-invariant preserved per-seller: sellerBad's dropped candidate
        # never recorded seen; sellerClean unaffected by sellerBad's drop.
        assert recorded["sellerClean"] == ["sellerClean-A1", "sellerClean-A2"]
        assert recorded["sellerBad"] == ["sellerBad-A1"]

    def test_unknown_seller_in_batch_records_error_and_continues(
        self, monkeypatch, capsys
    ):
        """BUI-298: one seller failing alias resolution does not abort the
        rest of the batch."""
        recorded = {}
        wish_fetch_calls = []

        def resolve(seller, aliases, username_override=None):
            if seller == "badseller":
                raise ebay_fetch.UnknownSellerError("badseller")
            return seller

        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "goodseller": lambda cands: (list(cands), [], []),
            },
            record_sink=recorded,
            wish_fetch_calls=wish_fetch_calls,
        )
        monkeypatch.setattr(seller_scan, "resolve_seller_username", resolve)

        code = seller_scan.main(["badseller", "goodseller", "--json"])

        # No seller was incomplete, but one had a resolution error → exit 2.
        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        assert by_seller["badseller"]["error"] == "unknown seller 'badseller'"
        assert by_seller["badseller"]["username"] is None
        assert by_seller["badseller"]["incomplete"] is False
        assert by_seller["goodseller"]["error"] is None
        assert {row["item_id"] for row in by_seller["goodseller"]["matches"]} == {
            "goodseller-A1", "goodseller-A2",
        }
        # The wish list is still fetched once even though one seller errored.
        assert len(wish_fetch_calls) == 1

    def test_listing_fetch_error_in_batch_records_error_and_continues(
        self, monkeypatch, capsys
    ):
        """BUI-298: a per-seller listing-fetch RequestException isolates to that
        seller's slot (error recorded) and does NOT abort the batch."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "goodseller": lambda cands: (list(cands), [], []),
            },
            record_sink=recorded,
        )

        def fake_search(username, token, base_url, max_results=1000):
            if username == "badfetch":
                raise seller_scan.requests.exceptions.ConnectionError("eBay down")
            return [
                {"item_id": f"{username}-A1", "title": "Amazing Spider-Man #300 one"},
            ]

        monkeypatch.setattr(seller_scan, "search_seller_listings", fake_search)

        code = seller_scan.main(["badfetch", "goodseller", "--json"])

        # A fetch failure is a seller error (exit 2), not incomplete.
        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        assert "listing fetch failed" in by_seller["badfetch"]["error"]
        assert by_seller["badfetch"]["matches"] == []
        # The good seller after it still ran.
        assert by_seller["goodseller"]["error"] is None
        assert len(by_seller["goodseller"]["matches"]) == 1

    def test_multi_seller_non_json_output_has_per_seller_sections(
        self, monkeypatch, capsys
    ):
        """BUI-298: multi-seller non-JSON output prints a per-seller header for
        each seller and an `Error:` line for an erroring seller."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "goodseller": lambda cands: (list(cands), [], []),
            },
            record_sink=recorded,
        )

        def resolve(seller, aliases, username_override=None):
            if seller == "badseller":
                raise ebay_fetch.UnknownSellerError("badseller")
            return seller

        monkeypatch.setattr(seller_scan, "resolve_seller_username", resolve)

        seller_scan.main(["goodseller", "badseller"])  # no --json

        out = capsys.readouterr().out
        # Per-seller headers distinguish the two sellers' sections.
        assert "=== goodseller (goodseller) ===" in out
        assert "=== badseller ===" in out
        assert "Error: unknown seller 'badseller'" in out
        # The good seller's match table rendered (wish_name is a printed column).
        assert "Amazing Spider-Man #300" in out

    def test_incomplete_wins_over_error_when_batch_has_both(
        self, monkeypatch, capsys
    ):
        """BUI-298 exit-code priority: a batch with BOTH an unresolved seller
        (would be exit 2) AND an incomplete seller (exit 3) exits 3 — the
        documented precedence."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                # sellerBad is incomplete (one dropped candidate).
                "sellerBad": lambda cands: (cands[:1], cands[1:], []),
            },
            record_sink=recorded,
        )

        def resolve(seller, aliases, username_override=None):
            if seller == "unresolvable":
                raise ebay_fetch.UnknownSellerError("unresolvable")
            return seller

        monkeypatch.setattr(seller_scan, "resolve_seller_username", resolve)

        code = seller_scan.main(["unresolvable", "sellerBad", "--json"])

        # Both an error slot AND an incomplete slot exist; exit 3 wins.
        assert code == 3
        payload = json.loads(capsys.readouterr().out)
        assert payload["incomplete"] is True
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        assert by_seller["unresolvable"]["error"] is not None
        assert by_seller["sellerBad"]["incomplete"] is True

    def test_global_verifier_failure_mid_batch_still_prints_prior_sellers(
        self, monkeypatch, capsys
    ):
        """BUI-298 reliability (preserved under BUI-307 concurrency): a GLOBAL
        verifier failure (verify_with_claude sys.exit(1)) on one seller must NOT
        discard another seller's already-scanned, already-recorded-seen result —
        the batch prints what it has and exits 1 (verifier-down), the most
        severe code. BUI-307: the fix drains every future that actually ran
        (never break-and-discard), so a completed seller is always shown."""
        recorded = {}

        def verify(cands, **kwargs):
            username = cands[0]["item_id"].rsplit("-", 1)[0]
            if username == "sellerDead":
                # Simulate verify_with_claude's global-failure hard-exit.
                raise SystemExit(1)
            return (list(cands), [], [])

        self._wire_main(
            monkeypatch,
            verify_return_by_username={},  # unused; we override verify below
            record_sink=recorded,
        )
        monkeypatch.setattr(seller_scan, "verify_with_claude", verify)
        # BUI-307: pin ONE worker so completion order is deterministic
        # (sellerOk completes and is recorded seen before sellerDead's failure).
        monkeypatch.setattr(seller_scan, "_SELLER_SCAN_MAX_WORKERS", 1)

        code = seller_scan.main(["sellerOk", "sellerDead", "sellerNever", "--json"])

        # Verifier-down is the most severe exit.
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        # The seller that completed before the failure was NOT discarded — it
        # printed with its matches (the reliability invariant: a seller that
        # recorded its matches seen is always shown, never silently lost).
        assert by_seller["sellerOk"]["error"] is None
        assert len(by_seller["sellerOk"]["matches"]) == 2
        assert recorded["sellerOk"] == ["sellerOk-A1", "sellerOk-A2"]
        # The dead seller carries the global-failure error.
        assert "globally unavailable" in by_seller["sellerDead"]["error"]

    def test_username_and_add_alias_reject_multi_seller(self, monkeypatch, capsys):
        """BUI-298: --username/--add-alias are single-seller conveniences —
        passing them with 2+ sellers is a usage error, not a silent
        first-seller-only application."""
        with pytest.raises(SystemExit) as exc:
            seller_scan.main(["seller1", "seller2", "--username", "realuser"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "single seller" in err.lower() or "--username" in err

    def test_single_seller_non_json_output_still_prints_table(
        self, monkeypatch, capsys
    ):
        """Backward-compat: a single-seller, non-JSON invocation still prints
        the plain table (no forced seller header for N=1)."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "seller1": lambda cands: (list(cands), [], []),
            },
            record_sink=recorded,
        )

        code = seller_scan.main(["seller1"])

        assert code == 0
        out = capsys.readouterr().out
        assert "=== seller1" not in out
        assert "seller1-A1" not in out  # item_id isn't a printed column...
        assert "Amazing Spider-Man #300" in out  # ...but the wish_name is

    def test_add_alias_register_then_scan_uses_fresh_alias(
        self, tmp_path, monkeypatch, capsys
    ):
        """BUI-298 regression: `--add-alias` must persist AND be honored in the
        same invocation. The batching refactor hoisted load_seller_aliases()
        out of the per-seller loop; if the save stayed inside the loop, the
        alias map read for resolution would be stale and the run would
        false-fail as "unknown seller" despite writing the alias to disk.

        Uses the REAL save/load/resolve alias path (only network seams are
        mocked) so it exercises the exact code the earlier main() tests mock
        away.
        """
        monkeypatch.setattr(
            ebay_fetch, "SELLER_ALIASES_FILE", tmp_path / "aliases.json"
        )
        monkeypatch.setattr(
            seller_scan, "load_config", lambda: ("id", "secret", "http://x")
        )
        monkeypatch.setattr(seller_scan, "get_token", lambda *a, **k: "tok")
        monkeypatch.setattr(
            seller_scan, "fetch_wish_list",
            lambda: [{"id": 1, "name": "Amazing Spider-Man #300"}],
        )
        searched = {}

        def fake_search(username, token, base_url, max_results=1000):
            searched["username"] = username
            return [{"item_id": "X1", "title": "Amazing Spider-Man #300"}]

        monkeypatch.setattr(seller_scan, "search_seller_listings", fake_search)
        monkeypatch.setattr(seller_scan, "parse_item_summary", lambda raw: dict(raw))
        monkeypatch.setattr(
            seller_scan, "match_listing", lambda title, wish_items: (wish_items[0], 0.9)
        )
        monkeypatch.setattr(seller_scan, "should_reject", lambda *a, **k: False)
        monkeypatch.setattr(seller_scan, "fetch_seen_item_ids", lambda seller: set())
        monkeypatch.setattr(
            seller_scan, "verify_with_claude", lambda cands, **kwargs: (list(cands), [], [])
        )
        monkeypatch.setattr(seller_scan, "record_items_seen", lambda ids, seller: None)

        # A brand-new store name, registered in the SAME call via --add-alias.
        code = seller_scan.main(
            ["newstore", "--add-alias", "newstore_login", "--json"]
        )

        # It resolved to the freshly-saved username and actually scanned —
        # NOT a false "unknown seller" / exit 2.
        assert code == 0
        assert searched["username"] == "newstore_login"
        payload = json.loads(capsys.readouterr().out)
        assert payload["sellers"][0]["error"] is None
        assert payload["sellers"][0]["username"] == "newstore_login"
        # The alias was persisted for next time.
        assert ebay_fetch.load_seller_aliases()["newstore"] == "newstore_login"


class TestSkippedCountAndForceReverify:
    """BUI-317: the rejected-cache 'skipped' count is surfaced in --json
    output, and --all force-re-verifies by bypassing that cache. Reuses
    TestMainDroppedCandidatesExit's `_wire_main` stubbing helper via
    composition (an instance, not a base class) so this class contributes
    only its own tests — subclassing would also re-collect and re-run every
    unrelated test already covered there.
    """

    def _wire_main(self, *args, **kwargs):
        return TestMainDroppedCandidatesExit()._wire_main(*args, **kwargs)

    def test_json_reports_skipped_cached_candidates_count(self, monkeypatch, capsys):
        """--json surfaces sellers[*].skipped_cached_candidates (BUI-317) —
        the count verify_with_claude reports via its `stats` out-param."""
        recorded = {}

        def fake_verify(cands, **kwargs):
            stats = kwargs.get("stats")
            if stats is not None:
                stats["skipped"] = 1
            # One candidate cache-skipped, one verified+kept.
            return cands[:1], [], []

        self._wire_main(
            monkeypatch,
            verify_return_by_username={},  # unused; fake_verify below is used directly
            record_sink=recorded,
        )
        monkeypatch.setattr(seller_scan, "verify_with_claude", fake_verify)

        code = seller_scan.main(["seller1", "--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sellers"][0]["skipped_cached_candidates"] == 1

    def test_json_skipped_count_zero_when_nothing_cached(self, monkeypatch, capsys):
        """The common case: no rejected-cache hits → the field is present and
        0, not omitted."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={
                "seller1": lambda cands: (list(cands), [], [])
            },
            record_sink=recorded,
        )

        code = seller_scan.main(["seller1", "--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sellers"][0]["skipped_cached_candidates"] == 0

    def test_skipped_count_zero_when_no_candidates_match(self, monkeypatch, capsys):
        """A seller whose listings match nothing exercises
        _scan_one_seller_impl's empty-candidates branch (verify is never
        called): skipped_cached_candidates is still present and 0, not omitted."""
        recorded = {}
        self._wire_main(
            monkeypatch,
            verify_return_by_username={},  # verify must never be reached
            record_sink=recorded,
        )
        # Override the always-match stub so no candidate survives matching.
        monkeypatch.setattr(
            seller_scan, "match_listing", lambda title, wish_items: (None, 0.0)
        )

        code = seller_scan.main(["seller1", "--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sellers"][0]["matches"] == []
        assert payload["sellers"][0]["skipped_cached_candidates"] == 0

    def test_all_flag_passes_use_rejected_cache_false(self, monkeypatch, capsys):
        """BUI-317: --all bypasses the BUI-301 rejected cache (force-re-verify),
        in addition to its existing BUI-113 already-seen-match behavior."""
        recorded = {}
        seen_kwargs = {}

        def fake_verify(cands, **kwargs):
            seen_kwargs.update(kwargs)
            return list(cands), [], []

        self._wire_main(
            monkeypatch,
            verify_return_by_username={},
            record_sink=recorded,
        )
        monkeypatch.setattr(seller_scan, "verify_with_claude", fake_verify)

        code = seller_scan.main(["seller1", "--all", "--json"])

        assert code == 0
        assert seen_kwargs["use_rejected_cache"] is False

    def test_without_all_flag_use_rejected_cache_stays_true(self, monkeypatch, capsys):
        """Baseline (no --all): the rejected cache stays opt-in-active, as
        before BUI-317 — only --all changes this."""
        recorded = {}
        seen_kwargs = {}

        def fake_verify(cands, **kwargs):
            seen_kwargs.update(kwargs)
            return list(cands), [], []

        self._wire_main(
            monkeypatch,
            verify_return_by_username={},
            record_sink=recorded,
        )
        monkeypatch.setattr(seller_scan, "verify_with_claude", fake_verify)

        code = seller_scan.main(["seller1", "--json"])

        assert code == 0
        assert seen_kwargs["use_rejected_cache"] is True


class TestSellerLevelParallelism:
    """BUI-307: sellers are scanned in a bounded ThreadPoolExecutor instead of a
    sequential loop. These tests prove the parallelism is:

    - deterministic + byte-for-byte identical to sequential aggregation
      (output ordering follows input order, not completion order),
    - genuinely concurrent (a slow/stuck seller can't serialize the rest),
    - bounded to _SELLER_SCAN_MAX_WORKERS,
    - keeps BUI-297's circuit-breaker state per-invocation (not a shared global
      that concurrent sellers could corrupt),
    - keeps BUI-301's rejected cache consistent under concurrent writers.
    """

    def _wire(self, monkeypatch, verify):
        """Stub every external seam so main() reaches `verify` (the injected
        verify_with_claude) once per seller with two candidates
        (``<username>-A1``/``-A2``). The seller arg is echoed back as the
        resolved username so a test can address each seller by name.
        """
        monkeypatch.setattr(seller_scan, "load_seller_aliases", lambda: {})
        monkeypatch.setattr(
            seller_scan, "resolve_seller_username",
            lambda seller, aliases, username_override=None: seller,
        )
        monkeypatch.setattr(
            seller_scan, "load_config", lambda: ("id", "secret", "http://x")
        )
        monkeypatch.setattr(seller_scan, "get_token", lambda *a, **k: "tok")
        monkeypatch.setattr(
            seller_scan, "fetch_wish_list",
            lambda: [{"id": 1, "name": "Amazing Spider-Man #300"}],
        )

        def fake_search(username, token, base_url, max_results=1000):
            return [
                {"item_id": f"{username}-A1", "title": "Amazing Spider-Man #300 one"},
                {"item_id": f"{username}-A2", "title": "Amazing Spider-Man #300 two"},
            ]

        monkeypatch.setattr(seller_scan, "search_seller_listings", fake_search)
        monkeypatch.setattr(seller_scan, "parse_item_summary", lambda raw: dict(raw))
        monkeypatch.setattr(
            seller_scan, "match_listing", lambda title, wish_items: (wish_items[0], 0.9)
        )
        monkeypatch.setattr(seller_scan, "should_reject", lambda *a, **k: False)
        monkeypatch.setattr(seller_scan, "fetch_seen_item_ids", lambda seller: set())
        monkeypatch.setattr(seller_scan, "verify_with_claude", verify)
        # record_items_seen runs on worker threads; a no-op stub keeps it off the
        # real server (tests here assert on main()'s payload, not the seen-set).
        monkeypatch.setattr(seller_scan, "record_items_seen", lambda ids, seller: None)

    def test_output_deterministic_and_identical_to_sequential(
        self, monkeypatch, capsys
    ):
        """The concurrent aggregate is ordered by INPUT position (not completion
        order) and is byte-for-byte identical to a single-worker sequential run.
        """
        def verify(cands, **kwargs):
            return (list(cands), [], [])

        # Concurrent (default _SELLER_SCAN_MAX_WORKERS).
        self._wire(monkeypatch, verify)
        code_par = seller_scan.main(["sB", "sA", "sC", "--json"])
        payload_par = json.loads(capsys.readouterr().out)

        # Sequential (1 worker) — same inputs.
        self._wire(monkeypatch, verify)
        monkeypatch.setattr(seller_scan, "_SELLER_SCAN_MAX_WORKERS", 1)
        code_seq = seller_scan.main(["sB", "sA", "sC", "--json"])
        payload_seq = json.loads(capsys.readouterr().out)

        assert code_par == code_seq == 0
        # Ordering follows the CLI arg order regardless of which seller's verify
        # finished first — never nondeterministic completion order.
        assert [s["seller"] for s in payload_par["sellers"]] == ["sB", "sA", "sC"]
        # Concurrency changes only the EXECUTION, never the aggregated result.
        assert payload_par == payload_seq

    def test_sellers_run_concurrently_not_serialized(self, monkeypatch, capsys):
        """A Barrier sized to the worker count only releases when all sellers'
        verify calls are in flight AT ONCE. Under the old sequential loop the
        first seller would block here forever (the others never start) and the
        5s timeout would raise BrokenBarrierError out of main(); passing proves
        the verifications overlap — a slow/stuck seller can't serialize the rest.
        """
        n = seller_scan._SELLER_SCAN_MAX_WORKERS
        barrier = threading.Barrier(n, timeout=5)

        def verify(cands, **kwargs):
            barrier.wait()
            return (list(cands), [], [])

        self._wire(monkeypatch, verify)
        sellers = [f"s{i}" for i in range(n)]
        code = seller_scan.main(sellers + ["--json"])

        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert {s["seller"] for s in payload["sellers"]} == set(sellers)
        assert all(len(s["matches"]) == 2 for s in payload["sellers"])

    def test_worker_count_is_bounded(self, monkeypatch, capsys):
        """With more sellers than workers, observed peak concurrency equals the
        cap and never exceeds it. A Barrier sized to the cap forces each wave to
        reach exactly the cap at once; the pool guarantees it never goes higher.
        """
        cap = seller_scan._SELLER_SCAN_MAX_WORKERS
        barrier = threading.Barrier(cap, timeout=5)
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def verify(cands, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            barrier.wait()  # hold exactly `cap` workers here simultaneously
            with lock:
                state["active"] -= 1
            return (list(cands), [], [])

        self._wire(monkeypatch, verify)
        sellers = [f"s{i}" for i in range(cap * 2)]  # two clean waves
        code = seller_scan.main(sellers + ["--json"])

        assert code == 0
        # Reached the cap (real parallelism) and never exceeded it (bounded).
        assert state["peak"] == cap

    def test_circuit_breaker_state_is_per_invocation_not_shared(self, monkeypatch):
        """Two verify_with_claude invocations run CONCURRENTLY: one whose every
        CLI call transport-fails (breaker trips → the global-failure safety net
        sys.exit(1)s) and one whose every call succeeds. Because transport_ok is
        a LOCAL of each verify_with_claude call — not a shared module global —
        the healthy invocation returns its match and the broken one exits, with
        no cross-talk. If the breaker state were shared, concurrent mutation
        would let one invocation mask the other.
        """
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )

        def fake_cli(prompt):
            if "ZZDEADZZ" in prompt:
                raise RuntimeError("transport boom")
            return "[]"  # reject nothing → all candidates kept

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_cli)

        healthy = [{"item_id": "ok-1", "title": "OK Book #1", "wish_name": "OK #1"}]
        broken = [
            {"item_id": "dead-1", "title": "ZZDEADZZ Book #1", "wish_name": "DEAD #1"}
        ]
        results = {}
        barrier = threading.Barrier(2, timeout=5)

        def run(key, cands):
            barrier.wait()  # ensure the two invocations genuinely overlap
            try:
                results[key] = ("ok", seller_scan.verify_with_claude(cands))
            except SystemExit as e:
                results[key] = ("exit", e.code)

        t1 = threading.Thread(target=run, args=("healthy", healthy))
        t2 = threading.Thread(target=run, args=("broken", broken))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["healthy"][0] == "ok"
        kept, dropped, filtered = results["healthy"][1]
        assert [c["item_id"] for c in kept] == ["ok-1"]
        assert dropped == []
        # The broken invocation tripped its OWN breaker + safety net.
        assert results["broken"] == ("exit", 1)

    def test_rejected_cache_no_lost_entries_under_concurrent_writers(
        self, monkeypatch
    ):
        """Many threads each record a DISTINCT rejection concurrently. A plain
        last-writer-wins overwrite (load {X}; add mine; write whole dict) would
        drop entries; _record_rejections re-reads and merges under a lock, so
        every entry survives. (The autouse conftest fixture points the cache at
        a per-test tmp path, so this never touches the real ~/.cache.)
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        n = 25
        barrier = threading.Barrier(n, timeout=5)

        def writer(i):
            barrier.wait()  # maximize contention on the critical section
            seller_scan._record_rejections({f"pair-{i}": now_iso})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        cache = seller_scan._load_rejected_cache()
        assert set(cache) == {f"pair-{i}" for i in range(n)}

    def test_concurrent_completed_seller_shown_despite_global_verifier_failure(
        self, monkeypatch, capsys
    ):
        """BUI-307 regression (found in review): when one seller trips the global
        verifier failure (SystemExit) while another seller is running CONCURRENTLY
        and verifies a real match, the completed seller MUST still be shown — it
        already recorded its matches as seen, so dropping it would hide a genuine
        wish-list match on the seen-filtered re-run (the BUI-297 lost-match class).

        A Barrier(2) forces both sellers in-flight at once; the loop must keep
        draining after the SystemExit rather than break-and-discard.
        """
        barrier = threading.Barrier(2, timeout=5)

        def verify(cands, **kwargs):
            username = cands[0]["item_id"].rsplit("-", 1)[0]
            barrier.wait()  # both sellers reach verify concurrently
            if username == "sellerDead":
                raise SystemExit(1)  # verify_with_claude's global-failure exit
            return (list(cands), [], [])

        self._wire(monkeypatch, verify)
        # Pin 2 workers so both sellers are genuinely in-flight together (the
        # Barrier would otherwise deadlock under a single worker).
        monkeypatch.setattr(seller_scan, "_SELLER_SCAN_MAX_WORKERS", 2)

        code = seller_scan.main(["sellerOk", "sellerDead", "--json"])

        # Verifier-down is the most severe exit.
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        # The concurrently-completed seller was NOT discarded — its real matches
        # are shown (the fix: drain in-flight futures instead of breaking).
        assert by_seller["sellerOk"]["error"] is None
        assert {row["item_id"] for row in by_seller["sellerOk"]["matches"]} == {
            "sellerOk-A1", "sellerOk-A2",
        }
        # The dead seller carries the global-failure error.
        assert "globally unavailable" in by_seller["sellerDead"]["error"]


class TestWorkerCrashIsolation:
    """BUI-319: an unexpected (non-SystemExit) exception inside a
    _scan_one_seller worker must not propagate out of main() and abort the
    whole multi-seller batch — it's isolated as an ordinary per-seller error.
    Reuses TestSellerLevelParallelism's `_wire` stubbing helper via
    composition (an instance, not a base class) so this class contributes
    only its own tests — subclassing would also re-collect and re-run every
    unrelated test already covered there.
    """

    def _wire(self, *args, **kwargs):
        return TestSellerLevelParallelism()._wire(*args, **kwargs)

    def test_scan_one_seller_isolates_unexpected_exception(self, monkeypatch):
        """A crash anywhere in the scan body (simulated via
        search_seller_listings raising a plain ValueError — not the
        requests.exceptions.RequestException the impl already handles
        inline) returns an ordinary error result instead of propagating."""
        def boom(*a, **k):
            raise ValueError("simulated crash")

        monkeypatch.setattr(seller_scan, "search_seller_listings", boom)

        result = seller_scan._scan_one_seller(
            "seller1", "seller1", "tok", "http://x", [], 1000, False
        )

        assert result["error"] is not None
        assert "crashed" in result["error"]
        assert "simulated crash" in result["error"]
        assert result["matches"] == []
        assert result["seller"] == "seller1"
        assert result["username"] == "seller1"

    def test_scan_one_seller_reraises_systemexit(self, monkeypatch):
        """SystemExit (verify_with_claude's global-verifier-down signal) must
        NOT be swallowed by the crash-isolation wrapper — main()'s
        as_completed loop depends on it propagating to trigger the BUI-307
        drain-then-cancel-the-tail path, not being masked as an ordinary
        per-seller error."""
        def raise_system_exit(*a, **k):
            raise SystemExit(1)

        monkeypatch.setattr(seller_scan, "search_seller_listings", raise_system_exit)

        with pytest.raises(SystemExit):
            seller_scan._scan_one_seller(
                "seller1", "seller1", "tok", "http://x", [], 1000, False
            )

    def test_crash_while_building_result_does_not_poison_seen_set(
        self, monkeypatch
    ):
        """BUI-319 (adversarial): a crash while BUILDING the result must not
        leave the seen-set committed with the matches dropped (the BUI-297
        lost-match class). The impl records seen AFTER constructing the result,
        so a crash in result-building happens BEFORE record_items_seen — this
        pins that ordering: if _strip_private blows up, record_items_seen is
        never reached, and the wrapper isolates the crash as an error slot.
        """
        recorded = []
        monkeypatch.setattr(
            seller_scan, "search_seller_listings",
            lambda username, token, base_url, max_results=1000: [
                {"item_id": "X1", "title": "Amazing Spider-Man #300"}
            ],
        )
        monkeypatch.setattr(seller_scan, "parse_item_summary", lambda raw: dict(raw))
        monkeypatch.setattr(
            seller_scan, "match_listing",
            lambda title, wish_items: (wish_items[0], 0.9),
        )
        monkeypatch.setattr(seller_scan, "should_reject", lambda *a, **k: False)
        monkeypatch.setattr(seller_scan, "fetch_seen_item_ids", lambda seller: set())
        monkeypatch.setattr(
            seller_scan, "verify_with_claude",
            lambda cands, **kwargs: (list(cands), [], []),
        )
        monkeypatch.setattr(
            seller_scan, "record_items_seen",
            lambda ids, seller: recorded.append((seller, list(ids))),
        )
        # Blow up during result construction — AFTER matching/verify, but the
        # impl builds the result before recording seen, so this precedes record.
        def boom(rows):
            raise RuntimeError("result build exploded")

        monkeypatch.setattr(seller_scan, "_strip_private", boom)

        wish = [{
            "id": 1, "name": "Amazing Spider-Man #300",
            "series": "Amazing Spider-Man", "issue": "300",
            "_tokens": ["amazing", "spider", "man"],
            "_series_name": None, "_release_year": None,
        }]
        result = seller_scan._scan_one_seller(
            "seller1", "seller1", "tok", "http://x", wish, 1000, False
        )

        # The crash was isolated as an error slot...
        assert result["error"] is not None
        assert "crashed" in result["error"]
        # ...and critically, the seen-set was NEVER written — no poisoned
        # seen-then-dropped window.
        assert recorded == []

    def test_one_seller_crash_does_not_abort_the_batch(self, monkeypatch, capsys):
        """End-to-end through main(): one seller's verify_with_claude call
        raises an unexpected (non-SystemExit) exception simulating a bug —
        the batch still completes, that seller's slot carries an error, and
        the OTHER seller's genuine matches are intact. Before BUI-319 this
        exception would propagate out of main() uncaught and lose every
        seller's output, not just the crashed one.
        """
        def verify(cands, **kwargs):
            username = cands[0]["item_id"].rsplit("-", 1)[0]
            if username == "sellerCrash":
                raise KeyError("simulated bug, not a global verifier failure")
            return (list(cands), [], [])

        self._wire(monkeypatch, verify)
        monkeypatch.setattr(seller_scan, "_SELLER_SCAN_MAX_WORKERS", 2)

        code = seller_scan.main(["sellerOk", "sellerCrash", "--json"])

        payload = json.loads(capsys.readouterr().out)
        by_seller = {s["seller"]: s for s in payload["sellers"]}
        # The healthy seller's matches are untouched by the other's crash.
        assert by_seller["sellerOk"]["error"] is None
        assert {row["item_id"] for row in by_seller["sellerOk"]["matches"]} == {
            "sellerOk-A1", "sellerOk-A2",
        }
        # The crashed seller carries an error, not a hard batch abort. The
        # error carries the exception repr (BUI-319 `{e!r}`) so an unattended
        # caller can triage the crash TYPE, not just a generic message.
        assert by_seller["sellerCrash"]["error"] is not None
        assert "crashed" in by_seller["sellerCrash"]["error"]
        assert "KeyError" in by_seller["sellerCrash"]["error"]
        # A crashed seller has error set but incomplete=False (dropped=[]), so
        # per main()'s documented exit priority (verifier-down 1 > incomplete 3
        # > seller-error 2 > 0) the batch exits EXACTLY 2 — pin the precise code
        # so a regression to 3 (incomplete) or any other value is caught.
        assert code == seller_scan._EXIT_SELLER_ERROR


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


# ─── BUI-240: era_mismatch per-issue release year ────────────────────────────


class TestEraMismatchReleaseYear:
    """BUI-240: era_mismatch with per-issue release_year (tighter than series-range)."""

    _ASM_V1 = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"

    # ── Headline regression: Vol.1-end vs Vol.2-relaunch boundary ────────────

    def test_asm_1999_vol2_relaunch_rejected_for_1963_wish(self):
        """ASM Vol.2-relaunch 1999 listing is rejected for an ASM #2 wish with release_year=1963.

        The 1999 parenthesized year lands on the old series-range end+1 boundary
        (which used to fail-open); the per-issue year gate (iy=1963, band [1962, 1964])
        correctly rejects it.  (BUI-240 headline regression)
        """
        assert seller_scan.era_mismatch(
            "The Amazing Spider-Man #2 (Marvel Comics February 1999)",
            self._ASM_V1,
            "1963",
        ) is True

    def test_asm_1999_compound_paren_rejected_for_1963_wish(self):
        """Another 1999 compound-paren ASM listing correctly rejected (BUI-240)."""
        assert seller_scan.era_mismatch(
            "The Amazing Spider-Man #4 (Marvel Comics April 1999)",
            self._ASM_V1,
            "1963",
        ) is True

    # ── Genuine in-era copies accepted ───────────────────────────────────────

    def test_genuine_in_era_copy_accepted(self):
        """A listing with a (1963) paren for a release_year=1963 wish item is kept."""
        assert seller_scan.era_mismatch(
            "The Amazing Spider-Man #2 (1963)",
            self._ASM_V1,
            "1963",
        ) is False

    # ── ±1 tolerance around the per-issue release year ────────────────────────

    def test_title_year_at_release_plus_1_accepted(self):
        """Title year = release_year + 1 is within ±1 tolerance → kept."""
        assert seller_scan.era_mismatch(
            "The Amazing Spider-Man #2 (1964)",
            self._ASM_V1,
            "1963",
        ) is False

    def test_title_year_at_release_minus_1_accepted(self):
        """Title year = release_year - 1 is within ±1 tolerance → kept."""
        assert seller_scan.era_mismatch(
            "The Amazing Spider-Man #2 (1962)",
            self._ASM_V1,
            "1963",
        ) is False

    def test_title_year_outside_plus_1_rejected(self):
        """Title year = release_year + 3 is outside ±1 → rejected."""
        assert seller_scan.era_mismatch(
            "The Amazing Spider-Man #2 (1966)",
            self._ASM_V1,
            "1963",
        ) is True

    # ── release_year=None falls back to the series-range check ───────────────

    def test_no_release_year_fallback_in_range_accepted(self):
        """release_year=None + title year 1999 (=end+1) for 1963-1998 range → False.

        With no per-issue year, the series-range fallback applies and
        1999=end+1 is within the ±1 tolerance band → kept.
        """
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1999) #441",
            self._ASM_V1,
            None,
        ) is False

    def test_no_release_year_fallback_out_of_range_rejected(self):
        """release_year=None + title year 2022 for 1963-1998 range → True (rejected)."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (2022) #7",
            self._ASM_V1,
            None,
        ) is True

    # ── No parenthesized year in title → fail-open regardless of release_year ─

    def test_no_title_paren_year_fail_open_with_release_year(self):
        """Bare year (not parenthesized) in title → False even when release_year is set."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man #2 VF 1963",
            self._ASM_V1,
            "1963",
        ) is False

    # ── series_name=None → False (fail-open) ─────────────────────────────────

    def test_series_name_none_fail_open_with_release_year(self):
        """series_name=None → False even when release_year is set."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1999) #2",
            None,
            "1963",
        ) is False

    # ── Backward compat: 2-arg form still works ───────────────────────────────

    def test_two_arg_form_still_works_reject(self):
        """Existing 2-arg callers (release_year defaults to None) still rejected correctly."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (2022) #7",
            self._ASM_V1,
        ) is True

    def test_two_arg_form_still_works_accept(self):
        """Existing 2-arg callers (release_year defaults to None) still accepted correctly."""
        assert seller_scan.era_mismatch(
            "Amazing Spider-Man (1984) #247",
            self._ASM_V1,
        ) is False


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


# ─── BUI-230: digital-code / no-physical reject ──────────────────────────────


class TestDigitalReject:
    def test_no_physical_rejected(self):
        assert seller_scan._digital_reject(
            "Amazing Spider-Man #7 DIGITAL CODE ONLY!!! [NO PHYSICAL COMIC BOOK]"
        ) is True

    def test_code_only_rejected(self):
        assert seller_scan._digital_reject("Amazing Spider-Man #4 Code Only") is True

    def test_digital_only_rejected(self):
        assert seller_scan._digital_reject("X-Men #94 Digital Only Edition") is True

    def test_digital_copy_only_rejected(self):
        assert seller_scan._digital_reject("Daredevil #1 Digital Copy Only") is True

    def test_case_insensitive(self):
        assert seller_scan._digital_reject("ASM #7 no physical") is True

    def test_with_bonus_digital_code_not_rejected(self):
        """A physical comic sold WITH a digital code bonus must NOT be dropped."""
        assert seller_scan._digital_reject(
            "Amazing Spider-Man #1 NM with Digital Code"
        ) is False

    def test_bare_digital_code_not_rejected(self):
        """Bare 'digital code' (no 'only'/'no physical') is a bonus, not a reject."""
        assert seller_scan._digital_reject("Amazing Spider-Man #1 includes digital code") is False

    def test_normal_title_not_rejected(self):
        assert seller_scan._digital_reject("Amazing Spider-Man #7 VF") is False

    def test_empty_title_not_rejected(self):
        assert seller_scan._digital_reject("") is False


# ─── BUI-232: trading-card / TCG reject ──────────────────────────────────────


class TestTradingCardReject:
    def test_fleer_real_listing_rejected(self):
        """Confirmed dcsports87 false positive: Fleer card matching ASM issue number."""
        assert seller_scan._trading_card_reject(
            "1994 Fleer The Amazing Spider-Man Venom Suspended Animation #4"
        ) is True

    def test_fleer_carnage_rejected(self):
        """Second dcsports87 false positive: Fleer Carnage card."""
        assert seller_scan._trading_card_reject(
            "1994 Fleer The Amazing Spider-Man Carnage Suspended Animation #5"
        ) is True

    def test_upper_deck_rejected(self):
        assert seller_scan._trading_card_reject(
            "2024 UPPER DECK MARVEL Avengers #7 trading card"
        ) is True

    def test_topps_rejected(self):
        assert seller_scan._trading_card_reject(
            "2004 Topps Amazing Spider-Man #129 base card"
        ) is True

    def test_skybox_rejected(self):
        assert seller_scan._trading_card_reject(
            "1993 SkyBox Marvel Universe Series IV card #129"
        ) is True

    def test_panini_comic_not_rejected(self):
        # BUI-221 Finding 7: Panini is also a comic publisher (Panini Comics /
        # Marvel UK reprints) so "panini" alone is not an unambiguous trading-
        # card marker.  Removed from _TRADING_CARD_MARKERS.
        assert seller_scan._trading_card_reject(
            "Panini Comics Amazing Spider-Man #300 UK Reprint"
        ) is False

    def test_mtg_rejected(self):
        assert seller_scan._trading_card_reject(
            "2025 MTG Magic: The Gathering Spider-Man Secret Lair #4"
        ) is True

    def test_magic_the_gathering_rejected(self):
        assert seller_scan._trading_card_reject(
            "Magic the Gathering Amazing Spider-Man Secret Lair Drop #4"
        ) is True

    def test_trading_card_phrase_rejected(self):
        assert seller_scan._trading_card_reject(
            "Amazing Spider-Man #300 trading card NM"
        ) is True

    def test_trading_cards_plural_rejected(self):
        assert seller_scan._trading_card_reject(
            "Marvel trading cards lot Amazing Spider-Man"
        ) is True

    def test_case_insensitive(self):
        assert seller_scan._trading_card_reject("FLEER Amazing Spider-Man #4") is True
        assert seller_scan._trading_card_reject("upper deck marvel #7") is True

    def test_normal_comic_title_not_rejected(self):
        assert (
            seller_scan._trading_card_reject("Amazing Spider-Man #300 NM Marvel 1988") is False
        )

    def test_trading_alone_not_rejected(self):
        """'trading' without 'card(s)' must NOT trigger — fails on substring."""
        assert seller_scan._trading_card_reject("Amazing Spider-Man #1 trading post") is False

    def test_empty_title_not_rejected(self):
        assert seller_scan._trading_card_reject("") is False


# ─── BUI-239: foreign-edition / foreign-market reprint reject ────────────────


class TestForeignEditionReject:
    """BUI-239: deterministic pre-verify reject for foreign-language/market reprints.

    Three La Prensa titles drove this: all must be caught before any API call.
    Conservative: bare nationality/language adjectives ("mexican", "spanish")
    are intentionally NOT in the set — only unambiguous publisher names and
    edition phrases are included.
    """

    # ── Observed La Prensa repro titles ──────────────────────────────────────

    def test_la_prensa_sandman_rejected(self):
        """BUI-239 repro: ASM #4 La Prensa listing → True."""
        assert seller_scan._foreign_edition_reject(
            "AMAZING SPIDER-MAN #4 VARIANT 1963 Sandman First appearance mexican la prensa"
        ) is True

    def test_la_prensa_electro_rejected(self):
        """BUI-239 repro: ASM #9 La Prensa listing → True."""
        assert seller_scan._foreign_edition_reject(
            "AMAZING SPIDER-MAN #9 VARIANT 1963 ELECTRO FIRST appearance mexican la prensa SPANISH"
        ) is True

    def test_la_prensa_edicion_rejected(self):
        """BUI-239 repro: ASM #10 La Prensa listing (contains 'EDICION') → True."""
        assert seller_scan._foreign_edition_reject(
            "AMAZING SPIDER-MAN #10 VARIANT 1963 EDICION mexican la prensa SPANISH"
        ) is True

    # ── Individual markers ────────────────────────────────────────────────────

    def test_la_prensa_marker(self):
        assert seller_scan._foreign_edition_reject("Amazing Spider-Man #4 la prensa") is True

    def test_novedades_marker(self):
        assert seller_scan._foreign_edition_reject("Amazing Spider-Man #1 novedades edition") is True

    def test_ediciones_marker(self):
        assert seller_scan._foreign_edition_reject("X-Men #1 ediciones mexico") is True

    def test_edicion_marker(self):
        assert seller_scan._foreign_edition_reject("Amazing Spider-Man #10 edicion 1963") is True

    def test_en_espanol_with_accent_rejected(self):
        """'en español' (with ñ, U+00F1) must be caught — accent form (BUI-239)."""
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 1963 en español la prensa"
        ) is True

    def test_espanol_ascii_rejected(self):
        """'espanol' (ASCII, no tilde) must also be caught (BUI-239)."""
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 1963 espanol edition"
        ) is True

    def test_spanish_edition_phrase_rejected(self):
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 Spanish Edition 1963"
        ) is True

    def test_foreign_edition_phrase_rejected(self):
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 foreign edition 1963"
        ) is True

    def test_case_insensitive(self):
        assert seller_scan._foreign_edition_reject("ASM #4 LA PRENSA MEXICAN") is True
        assert seller_scan._foreign_edition_reject("ASM #4 EDICION MEXICAN") is True
        assert seller_scan._foreign_edition_reject("ASM #4 SPANISH EDITION") is True

    # ── Genuine US copies must pass (return False) ────────────────────────────

    def test_genuine_us_copy_not_rejected(self):
        """A plain US copy with no foreign marker must pass → False."""
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 1963 Sandman 1st appearance VF"
        ) is False

    def test_normal_comic_title_not_rejected(self):
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #300 NM Marvel 1988"
        ) is False

    def test_empty_title_not_rejected(self):
        assert seller_scan._foreign_edition_reject("") is False

    # ── Conservative carve-out: bare adjectives NOT caught deterministically ──

    def test_bare_mexican_adjective_not_rejected(self):
        """BUI-239 conservative design: bare 'mexican' alone is NOT deterministically
        rejected — left to Haiku.  Documents the intentional carve-out."""
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 mexican villain story arc"
        ) is False

    def test_bare_spanish_adjective_not_rejected(self):
        """BUI-239 conservative design: bare 'spanish' alone is NOT deterministically
        rejected — e.g. 'Spanish Harlem' or character descriptor."""
        assert seller_scan._foreign_edition_reject(
            "Amazing Spider-Man #4 spanish story arc"
        ) is False

    # ── Boundary check: 'espanol' must not match inside longer words ──────────

    def test_espanol_not_substring_matched(self):
        """'espanol' must only match at a word boundary, not inside a longer word."""
        # A contrived string where 'espanol' appears as a substring — must NOT match.
        assert seller_scan._foreign_edition_reject("myespanolfoo #1") is False


# ─── BUI-244: later-printing / non-first-print reject ────────────────────────


class TestSecondPrintReject:
    """BUI-244: _second_print_reject catches later-pressing listings that the
    existing _REPRINT_MARKERS set misses (e.g. "second print" without "-ing").

    Mirrors the structure of TestReprintReject above.
    """

    # ── Reject cases (returns True) ───────────────────────────────────────────

    def test_vengeance_of_bane_second_print_rejected(self):
        """BUI-244 repro: the exact Batman: Vengeance of Bane #1 second-print title."""
        assert seller_scan._second_print_reject(
            "Batman Vengeance Of Bane #1 1993 II DC comics Second Print 1st Appearance Smith"
        ) is True

    def test_second_print_rejected(self):
        """Bare 'second print' (no -ing) → True."""
        assert seller_scan._second_print_reject("X-Men #1 second print") is True

    def test_2nd_print_rejected(self):
        """'2nd print' → True."""
        assert seller_scan._second_print_reject("Amazing Spider-Man #300 2nd print NM") is True

    def test_2nd_printing_rejected(self):
        """'2nd printing' → True (handles the -ing suffix)."""
        assert seller_scan._second_print_reject("Amazing Spider-Man #300 2nd printing NM") is True

    def test_3rd_print_rejected(self):
        """'3rd print' → True."""
        assert seller_scan._second_print_reject("Spawn #1 3rd print") is True

    def test_third_printing_rejected(self):
        """'third printing' → True."""
        assert seller_scan._second_print_reject("Batman #1 third printing") is True

    def test_later_printing_rejected(self):
        """'later printing' → True."""
        assert seller_scan._second_print_reject("X-Men #94 later printing VF") is True

    def test_reprint_rejected(self):
        """'reprint' → True."""
        assert seller_scan._second_print_reject("Amazing Fantasy #15 reprint VF") is True

    def test_reprints_plural_rejected(self):
        """'reprints' (plural) → True."""
        assert seller_scan._second_print_reject("Marvel reprints Amazing Spider-Man #1") is True

    def test_case_insensitive(self):
        """Match is case-insensitive."""
        assert seller_scan._second_print_reject("Batman #1 SECOND PRINT") is True
        assert seller_scan._second_print_reject("X-Men #1 2ND PRINTING") is True
        assert seller_scan._second_print_reject("Amazing Fantasy #15 REPRINT") is True

    # ── Keep cases (returns False) — must NOT be filtered ─────────────────────

    def test_first_print_not_rejected(self):
        """'first print' is an original copy — must return False."""
        assert seller_scan._second_print_reject("X-Men #1 first print NM") is False

    def test_1st_print_not_rejected(self):
        """'1st print' is an original copy — must return False."""
        assert seller_scan._second_print_reject("Amazing Spider-Man #300 1st print") is False

    def test_newsstand_not_rejected(self):
        """Newsstand edition is an original-copy distribution variant — must return False."""
        assert seller_scan._second_print_reject("X-Men #94 newsstand VF") is False

    def test_direct_not_rejected(self):
        """Direct edition is an original-copy distribution variant — must return False."""
        assert seller_scan._second_print_reject("Amazing Spider-Man #129 direct VF") is False

    def test_plain_title_not_rejected(self):
        """A plain title with no print token → False."""
        assert seller_scan._second_print_reject("Amazing Spider-Man #4 VF") is False

    def test_bare_printing_without_ordinal_not_rejected(self):
        """Bare 'printing' without an ordinal prefix is NOT caught (left to Haiku)."""
        assert seller_scan._second_print_reject("Fine printing condition Batman #1") is False

    def test_empty_title_not_rejected(self):
        assert seller_scan._second_print_reject("") is False

    def test_none_title_not_rejected(self):
        assert seller_scan._second_print_reject(None) is False  # type: ignore[arg-type]

    def test_facsimile_not_rejected(self):
        """'Facsimile' is handled by _reprint_reject, not _second_print_reject — returns False."""
        assert seller_scan._second_print_reject("Amazing Spider-Man #1 Facsimile Edition") is False


# ─── BUI-227: verify_with_claude prompt enrichment ───────────────────────────


class TestVerifyWithClaudePromptEnrichment:
    """_series_name carried on candidates lands in the prompt as 'Correct series:' line."""

    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch):
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )

    def _capture_prompt(self, matches, monkeypatch):
        """Run verify_with_claude with a mocked CLI transport; return the prompt sent."""
        prompts_seen = []

        def fake_verify(prompt):
            prompts_seen.append(prompt)
            return "[]"

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)
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
        # Reject id=2 (Daredevil Annual)
        monkeypatch.setattr(
            seller_scan,
            "_verify_via_claude_cli",
            lambda prompt: '[{"id":2,"reason":"annual vs regular"}]',
        )

        kept, dropped, filtered = seller_scan.verify_with_claude(matches)
        assert len(kept) == 1
        assert kept[0]["title"] == "Amazing Spider-Man #7"
        assert dropped == []

    def test_foreign_edition_bullet_in_prompt(self, monkeypatch):
        """BUI-239: the foreign-edition reject bullet is present in the Haiku prompt.

        Kept loose (substring check only) so sibling tickets adding their own
        bullets can land without breaking this assertion.
        """
        matches = [{"title": "Amazing Spider-Man #4", "wish_name": "Amazing Spider-Man #4"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "Foreign-language or foreign-market reprint/edition" in prompt

    def test_sequential_run_bullet_in_prompt(self, monkeypatch):
        """BUI-243: the sequential-run / complete-set reject bullet is in the Haiku prompt."""
        matches = [{"title": "Batman #4", "wish_name": "Batman #4"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "Numbered sequential run or complete multi-issue set" in prompt

    def test_later_printing_bullet_in_prompt(self, monkeypatch):
        """BUI-244: the later-printing / reprint reject bullet is in the Haiku prompt."""
        matches = [{"title": "Batman: Vengeance of Bane #1", "wish_name": "Batman: Vengeance of Bane #1"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "Later printing / reprint of a key issue" in prompt
        assert "Newsstand and Direct editions are NOT reprints" in prompt


class TestVerifyWithClaudeRejectBulletsFromLexicons:
    """BUI-253 Step 5: the reject-list bullets that duplicate a deterministic
    comic_identity lexicon are BUILT from that lexicon (comic_identity.
    EDITION_LABELS / foreign_edition_examples() / later_printing_examples())
    instead of hand-typed prose — so the prompt can't silently drift from
    what should_reject actually checks. These assert the ACTUAL lexicon
    contents land in the prompt, not just the stable lead-in phrase (already
    covered above)."""

    @pytest.fixture(autouse=True)
    def _claude_on_path(self, monkeypatch):
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )

    def _capture_prompt(self, matches, monkeypatch):
        prompts_seen = []

        def fake_verify(prompt):
            prompts_seen.append(prompt)
            return "[]"

        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)
        seller_scan.verify_with_claude(matches)
        return prompts_seen[0]

    def test_edition_bullet_includes_all_four_edition_labels(self, monkeypatch):
        """Before BUI-253 this bullet only said "Annual, Giant-Size" — King-Size
        and Treasury were silently missing from Haiku's reminder even though
        hard_reject's _EDITION_PATTERNS has always checked all four. Sourcing
        the bullet from comic_identity.EDITION_LABELS fixes that gap."""
        matches = [{"title": "X #1", "wish_name": "X #1"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        for label in seller_scan.EDITION_LABELS:
            assert label in prompt
        assert seller_scan.EDITION_LABELS == ("Annual", "Giant-Size", "King-Size", "Treasury")

    def test_foreign_edition_bullet_contains_actual_lexicon_markers(self, monkeypatch):
        """The bullet must contain the REAL _FOREIGN_EDITION_MARKERS entries,
        not just a hand-picked example — proving it's generated, not typed."""
        matches = [{"title": "X #1", "wish_name": "X #1"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "la prensa" in prompt
        assert "novedades" in prompt
        assert "espanol" in prompt

    def test_later_printing_bullet_contains_actual_lexicon_markers(self, monkeypatch):
        matches = [{"title": "X #1", "wish_name": "X #1"}]
        prompt = self._capture_prompt(matches, monkeypatch)
        assert "2nd printing" in prompt
        assert "second printing" in prompt

    def test_reject_bullets_are_stable_across_calls(self, monkeypatch):
        """The rendered bullets don't depend on the candidates passed in —
        confirms they're computed from the module-level lexicons, not
        per-candidate data."""
        matches_a = [{"title": "X #1", "wish_name": "X #1"}]
        matches_b = [{"title": "Y #99", "wish_name": "Y #99"}]
        prompt_a = self._capture_prompt(matches_a, monkeypatch)
        prompt_b = self._capture_prompt(matches_b, monkeypatch)
        bullets_a = prompt_a.split("Reject if:")[1].split("Respond with")[0]
        bullets_b = prompt_b.split("Reject if:")[1].split("Respond with")[0]
        assert bullets_a == bullets_b


# ─── BUI-229: publication_year_mismatch ──────────────────────────────────────


class TestPublicationYearMismatch:
    """publication_year_mismatch uses eBay item-specifics to reject wrong-era listings."""

    # Helper: a 1963-range series (Amazing Spider-Man Vol 1)
    _ASM_1963 = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
    # Helper: a pre-1992 series (Wolverine Vol 1)
    _WOL_1982 = "Wolverine (Vol. 1) (1982 - 2003)"
    # Helper: pre-1992 series that definitively ended pre-1992
    _FF_1961 = "Fantastic Four (Vol. 1) (1961 - 1996)"
    # Silver-age only (ended 1969)
    _SA_1963 = "X-Men (Vol. 1) (1963 - 1981)"

    def test_pub_year_out_of_range_returns_true(self):
        """Publication Year clearly outside the series range → True (reject)."""
        aspects = {"Publication Year": "2014", "Era": "Modern Age (1992-Now)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is True

    def test_pub_year_in_range_returns_false(self):
        """Publication Year within the series range → False (keep)."""
        aspects = {"Publication Year": "1964", "Era": "Silver Age (1956-1969)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is False

    def test_pub_year_within_plus_one_tolerance_returns_false(self):
        """Publication Year == end+1 is kept (±1 tolerance, same as era_mismatch)."""
        # ASM Vol 1 ends 1998; pub year 1999 is within ±1
        aspects = {"Publication Year": "1999"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is False

    def test_pub_year_within_minus_one_tolerance_returns_false(self):
        """Publication Year == begin-1 is kept (±1 tolerance)."""
        # ASM Vol 1 begins 1963; pub year 1962 is within ±1
        aspects = {"Publication Year": "1962"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is False

    def test_pub_year_just_outside_tolerance_returns_true(self):
        """Publication Year == end+2 is rejected (outside ±1 tolerance)."""
        # ASM Vol 1 ends 1998; pub year 2000 is outside
        aspects = {"Publication Year": "2000"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is True

    def test_missing_aspects_fail_open(self):
        """aspects=None → False (fail-open)."""
        assert seller_scan.publication_year_mismatch(None, self._ASM_1963) is False

    def test_empty_aspects_fail_open(self):
        """Empty aspects dict → False (fail-open; series_year_range might return a range
        but no pub-year signal is present → Era fallback also absent)."""
        assert seller_scan.publication_year_mismatch({}, self._ASM_1963) is False

    def test_missing_series_name_fail_open(self):
        """series_name=None → False (fail-open)."""
        aspects = {"Publication Year": "2014"}
        assert seller_scan.publication_year_mismatch(aspects, None) is False

    def test_empty_series_name_fail_open(self):
        """Empty series_name → False (fail-open)."""
        aspects = {"Publication Year": "2014"}
        assert seller_scan.publication_year_mismatch(aspects, "") is False

    def test_series_name_without_year_range_fail_open(self):
        """Series name with no decorated year range → series_year_range returns None → False."""
        aspects = {"Publication Year": "2014"}
        assert seller_scan.publication_year_mismatch(aspects, "Amazing Spider-Man") is False

    def test_pub_year_absent_era_modern_series_ended_pre_1992_returns_true(self):
        """Publication Year absent + Era=Modern Age + series ended before 1992 → True."""
        aspects = {"Era": "Modern Age (1992-Now)"}
        # X-Men Vol 1 ran 1963-1981 — definitively pre-1992
        assert seller_scan.publication_year_mismatch(aspects, self._SA_1963) is True

    def test_pub_year_absent_era_modern_series_1992_run_returns_false(self):
        """Publication Year absent + Era=Modern Age + series ended 2003 → False.

        A 1982-2003 series overlaps Modern Age, so Era alone cannot reject it.
        """
        aspects = {"Era": "Modern Age (1992-Now)"}
        # Wolverine Vol 1 ran 1982-2003 — overlaps Modern Age → cannot reject
        assert seller_scan.publication_year_mismatch(aspects, self._WOL_1982) is False

    def test_pub_year_absent_era_silver_not_modern_returns_false(self):
        """Publication Year absent + Era is NOT Modern Age → False (fail-open)."""
        aspects = {"Era": "Silver Age (1956-1969)"}
        assert seller_scan.publication_year_mismatch(aspects, self._SA_1963) is False

    def test_pub_year_absent_no_era_aspect_returns_false(self):
        """Publication Year absent + no Era key → False (fail-open)."""
        aspects = {"Series Title": "X-Men"}
        assert seller_scan.publication_year_mismatch(aspects, self._SA_1963) is False

    def test_unparseable_pub_year_fail_open(self):
        """Non-integer Publication Year → False (fail-open)."""
        aspects = {"Publication Year": "circa 2014"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is False

    def test_non_four_digit_pub_year_fail_open(self):
        """A 3-digit or 5-digit Publication Year → False (fail-open; not a valid year)."""
        aspects = {"Publication Year": "14"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963) is False

    # ── BUI-231: Era fallback gated on per-issue release_year ─────────────────

    def test_era_fallback_issue_year_pre1992_returns_true(self):
        """No Pub Year + Era=Modern Age + release_year='1964' (< 1992) → True.

        ASM Vol.1 ran 1963-1998 so the old series-end fallback (end ≥ 1992)
        would NOT fire.  The per-issue year (1964) correctly rejects a modern
        listing for a pre-Modern Age wish.
        """
        aspects = {"Era": "Modern Age (1992-Now)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963, "1964") is True

    def test_era_fallback_issue_year_post1992_returns_false(self):
        """No Pub Year + Era=Modern Age + release_year='1995' (≥ 1992) → False.

        A 1995 wished issue falls within Modern Age; can't reject on Era alone.
        """
        aspects = {"Era": "Modern Age (1992-Now)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963, "1995") is False

    def test_era_fallback_release_year_none_series_end_1998_returns_false(self):
        """No Pub Year + Era=Modern Age + release_year=None + series end=1998 → False.

        Old series-end fallback: end=1998 ≥ 1992 → doesn't fire even without
        per-issue year.
        """
        aspects = {"Era": "Modern Age (1992-Now)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963, None) is False

    def test_era_fallback_release_year_none_series_ended_pre1992_returns_true(self):
        """No Pub Year + Era=Modern Age + release_year=None + series end 1981 → True.

        Old series-end fallback is preserved when no per-issue year is available.
        """
        aspects = {"Era": "Modern Age (1992-Now)"}
        # _SA_1963 ends 1981 < 1992 → old fallback still fires
        assert seller_scan.publication_year_mismatch(aspects, self._SA_1963, None) is True

    def test_era_fallback_not_modern_age_returns_false(self):
        """No Pub Year + Era=Silver Age + release_year='1964' → False (not modern age)."""
        aspects = {"Era": "Silver Age (1956-1969)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963, "1964") is False

    def test_pub_year_present_takes_priority_out_of_range(self):
        """Publication Year present and out-of-range → True, regardless of release_year."""
        aspects = {"Publication Year": "2014", "Era": "Modern Age (1992-Now)"}
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963, "1964") is True

    def test_pub_year_present_takes_priority_in_range(self):
        """Publication Year present and in-range → False, regardless of release_year."""
        aspects = {"Publication Year": "1964", "Era": "Modern Age (1992-Now)"}
        # release_year="2020" would normally be post-1992 (keep), but pub year wins
        assert seller_scan.publication_year_mismatch(aspects, self._ASM_1963, "2020") is False


# ─── BUI-245: should_reject (shared deterministic gate chain) ────────────────


class TestShouldReject:
    """should_reject() is the single gate chain shared by seller_scan.main() and
    wishlist_sellers.match_results_for_wish(). Each underlying gate already has
    its own thorough test class (TestHardRejectCGC, TestReprintReject, etc.) —
    these tests just confirm should_reject wires every one of them in, so a
    True from any single gate propagates."""

    _ASM_1963 = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"

    def test_clean_match_not_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 VF Marvel", "Amazing Spider-Man", "15",
        ) is False

    def test_cgc_slab_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 CGC 9.4", "Amazing Spider-Man", "15",
        ) is True

    def test_edition_mismatch_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man Annual #15", "Amazing Spider-Man", "15",
        ) is True

    def test_lot_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 lot of 5", "Amazing Spider-Man", "15",
        ) is True

    def test_missing_issue_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #16 VF Marvel", "Amazing Spider-Man", "15",
        ) is True

    def test_era_mismatch_rejected(self):
        """A parenthesized year outside the wish's release-year window rejects."""
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 (1999) VF", "Amazing Spider-Man",
            "15", self._ASM_1963, "1964",
        ) is True

    def test_era_mismatch_fails_open_without_series_name(self):
        """Same title, but no series_name passed → era gate can't fire."""
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 (1999) VF", "Amazing Spider-Man", "15",
        ) is False

    def test_reprint_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 Omnibus", "Amazing Spider-Man", "15",
        ) is True

    def test_digital_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 Digital Only", "Amazing Spider-Man", "15",
        ) is True

    def test_trading_card_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 Topps card", "Amazing Spider-Man", "15",
        ) is True

    def test_foreign_edition_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 La Prensa", "Amazing Spider-Man", "15",
        ) is True

    def test_second_print_rejected(self):
        assert seller_scan.should_reject(
            "Amazing Spider-Man #15 2nd Printing", "Amazing Spider-Man", "15",
        ) is True


# ─── BUI-245: seller_scan.main() candidate loop — deterministic gate + hint ──


class TestMainCandidateLoopGateChain:
    """Regression coverage for BUI-245: seller_scan.main()'s candidate loop must
    run the same deterministic reject chain as wishlist_sellers.match_results_for_wish()
    and must carry _series_name onto candidates so verify_with_claude's
    "Correct series:" era hint activates.

    Repro (BUI-245): "Spectacular Spider-Man #15" scores 0.67 against wish item
    "The Amazing Spider-Man #15" (spider + man = 2/3 tokens) — high enough to
    clear match_listing's 0.65 floor, so none of the deterministic gates catch
    it (no edition/lot/reprint/digital/trading-card/foreign-edition marker, and
    no parenthesized year for era_mismatch to compare). The fix relies on the
    "Correct series:" hint reaching Haiku, which is exactly what these tests
    verify is now wired through from the main() loop.
    """

    def _wish_items(self):
        return seller_scan.prepare_wish_items([{
            "id": "w1",
            "name": "The Amazing Spider-Man #15",
            "series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
            "release_date": "1964-08-01",
        }])

    def _run_scan_loop_body(self, raw_titles, wish_items):
        """Replicate the scan-loop body from seller_scan.main() (mirrors
        TestNullTitleScanLoop's approach) so the fix can be exercised without
        spinning up the full CLI."""
        candidates = []
        seen_ids = set()
        for item_id, title in raw_titles:
            if not title:
                continue
            if "cgc" in title.lower():
                continue
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            wish, score = seller_scan.match_listing(title, wish_items)
            if not wish:
                continue
            if seller_scan.should_reject(
                title, wish["series"], wish["issue"],
                wish.get("_series_name"), wish.get("_release_year"),
            ):
                continue
            candidates.append({
                "item_id": item_id,
                "title": title,
                "wish_id": wish["id"],
                "wish_name": wish["name"],
                "match_score": round(score, 2),
                "_series_name": wish.get("_series_name"),
                "_release_year": wish.get("_release_year"),
            })
        return candidates

    def test_cross_series_candidate_not_deterministically_rejected(self):
        """should_reject alone does not catch this pair — confirms the failure
        mode described in BUI-245 (the case must reach Haiku, not be dropped
        deterministically)."""
        wish_items = self._wish_items()
        candidates = self._run_scan_loop_body(
            [("1", "Spectacular Spider-Man #15 VF Marvel 1979")], wish_items
        )
        assert len(candidates) == 1

    def test_cross_series_candidate_carries_series_name_hint(self):
        """The candidate dict built by the main()-loop replica carries
        _series_name so verify_with_claude's era hint activates (BUI-245 fix)."""
        wish_items = self._wish_items()
        candidates = self._run_scan_loop_body(
            [("1", "Spectacular Spider-Man #15 VF Marvel 1979")], wish_items
        )
        assert candidates[0]["_series_name"] == "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"

    def test_cross_series_candidate_rejected_end_to_end_via_claude_hint(self, monkeypatch):
        """End-to-end: the candidate built by main()'s loop is sent to
        verify_with_claude with the "Correct series:" hint present in the
        prompt, and a Claude response rejecting it (as the hint enables) drops
        it from the final matches — the BUI-245 false positive is gone."""
        wish_items = self._wish_items()
        candidates = self._run_scan_loop_body(
            [("1", "Spectacular Spider-Man #15 VF Marvel 1979")], wish_items
        )

        prompts_seen = []

        def fake_verify(prompt):
            prompts_seen.append(prompt)
            return '[{"id":1,"reason":"Spectacular Spider-Man not Amazing Spider-Man"}]'

        # BUI-270: stub the preflight truthy so verify_with_claude reaches the
        # (mocked) transport instead of exiting on a missing `claude` binary.
        monkeypatch.setattr(
            seller_scan.shutil, "which", lambda name: "/usr/bin/claude"
        )
        monkeypatch.setattr(seller_scan, "_verify_via_claude_cli", fake_verify)

        kept, dropped, filtered = seller_scan.verify_with_claude(candidates)

        assert "Correct series: The Amazing Spider-Man (Vol. 1) (1963 - 1998)" in prompts_seen[0]
        assert kept == []
        assert dropped == []
