"""Tests for ebay-fetch CLI."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import ebay_fetch

_MODULE = str(Path(__file__).parent.parent / "src" / "ebay_fetch.py")


# ============================================================
# Unit Tests — pure functions, no network
# ============================================================


class TestExtractItemId:
    def test_url_standard(self):
        assert ebay_fetch.extract_item_id("https://www.ebay.com/itm/298217294954") == "298217294954"

    def test_url_with_query_params(self):
        assert ebay_fetch.extract_item_id("https://www.ebay.com/itm/298217294954?hash=item123") == "298217294954"

    def test_raw_numeric_id(self):
        assert ebay_fetch.extract_item_id("298217294954") == "298217294954"

    def test_raw_id_with_whitespace(self):
        assert ebay_fetch.extract_item_id("  298217294954  ") == "298217294954"

    def test_invalid_string(self):
        assert ebay_fetch.extract_item_id("not-an-id") is None

    def test_empty_string(self):
        assert ebay_fetch.extract_item_id("") is None

    def test_url_mobile(self):
        assert ebay_fetch.extract_item_id("https://www.ebay.com/itm/12345") == "12345"


class TestExtractGrade:
    def test_from_title_nm_minus(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "AMAZING SPIDER-MAN #300 (NM-) VENOM")
        assert grade == "NM-"
        assert source == "title"
        assert desc_grade is None

    def test_from_title_fn_vf(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "FANTASTIC FOUR #32 (FN/VF) THING")
        assert grade == "FN/VF"
        assert source == "title"
        assert desc_grade is None

    def test_from_title_vf(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "SPIDER-MAN #109 (VF) DR STRANGE")
        assert grade == "VF"
        assert source == "title"
        assert desc_grade is None

    def test_from_title_fn_plus(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "SPIDER-MAN #112 (FN+) COPS OUT")
        assert grade == "FN+"
        assert source == "title"
        assert desc_grade is None

    def test_from_item_specifics(self):
        specs = [{"name": "Grade", "value": "NM"}]
        grade, source, desc_grade = ebay_fetch.extract_grade(specs, "SPIDER-MAN #300")
        assert grade == "NM"
        assert source == "item_specifics"
        assert desc_grade is None

    def test_item_specifics_cgc_grade(self):
        specs = [{"name": "CGC Grade", "value": "9.4"}]
        grade, source, desc_grade = ebay_fetch.extract_grade(specs, "SPIDER-MAN #300")
        assert grade == "9.4"
        assert source == "item_specifics"
        assert desc_grade is None

    def test_item_specifics_takes_precedence(self):
        specs = [{"name": "Grade", "value": "VF+"}]
        grade, source, desc_grade = ebay_fetch.extract_grade(specs, "SPIDER-MAN (NM-)")
        assert grade == "VF+"
        assert source == "item_specifics"
        assert desc_grade is None

    def test_missing_grade(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "Amazing Spider-Man 300 First Venom")
        assert grade is None
        assert source == "missing"
        assert desc_grade is None

    def test_numeric_grade_in_title(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "SPIDER-MAN #300 (9.4) WHITE PAGES")
        assert grade == "9.4"
        assert source == "title"
        assert desc_grade is None

    def test_case_insensitive_specifics(self):
        specs = [{"name": "grade", "value": "FN"}]
        grade, source, desc_grade = ebay_fetch.extract_grade(specs, "SPIDER-MAN #300")
        assert grade == "FN"
        assert source == "item_specifics"
        assert desc_grade is None

    def test_bare_inline_nm(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "SPIDER-MAN #300 NM Gem Copy")
        assert grade == "NM"
        assert source == "title"

    def test_bare_inline_vf_plus(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "FANTASTIC FOUR #32 VF+ Cond")
        assert grade == "VF+"
        assert source == "title"

    def test_bare_inline_fvf(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "SPIDER-MAN #109 FVF beauty")
        assert grade == "FVF"
        assert source == "title"

    def test_bare_inline_fine_plus(self):
        grade, source, desc_grade = ebay_fetch.extract_grade([], "SPIDER-MAN #112 Fine+ great book")
        assert grade == "Fine+"
        assert source == "title"

    def test_grade_from_description(self):
        grade, source, desc_grade = ebay_fetch.extract_grade(
            [], "Amazing Spider-Man 300 First Venom", "9.6 beautiful edition"
        )
        assert grade is None
        assert source == "missing"
        assert desc_grade == "9.6"

    def test_parenthetical_preferred_over_bare(self):
        """Parenthetical grade should take priority even when bare grade also present."""
        grade, source, _ = ebay_fetch.extract_grade([], "SPIDER-MAN #300 VF (NM-) COPY")
        assert grade == "NM-"
        assert source == "title"

    def test_no_false_positive_venom(self):
        """'VENOM' should not match 'NM' inside the word."""
        grade, source, desc_grade = ebay_fetch.extract_grade([], "AMAZING SPIDER-MAN 300 FIRST VENOM APP")
        assert grade is None
        assert source == "missing"

    def test_no_false_positive_infinity(self):
        """'INFINITY' should not match 'FN' inside the word."""
        grade, source, desc_grade = ebay_fetch.extract_grade([], "INFINITY WAR #1 THANOS COVER")
        assert grade is None
        assert source == "missing"

    def test_no_false_positive_fine_art(self):
        """'FINE ART' should match 'Fine' as a bare grade — this is a known ambiguity."""
        # 'Fine' is a legitimate comic grade, so bare matching will pick it up.
        # This test documents the behavior.
        grade, source, _ = ebay_fetch.extract_grade([], "SPIDER-MAN FINE ART PRINT")
        assert grade.upper() == "FINE"
        assert source == "title"

    def test_description_not_checked_when_title_has_grade(self):
        """Description should not be parsed when title already has a grade."""
        grade, source, desc_grade = ebay_fetch.extract_grade(
            [], "SPIDER-MAN #300 NM COPY", "9.6 beautiful edition"
        )
        assert grade == "NM"
        assert source == "title"
        assert desc_grade is None

    def test_description_not_checked_when_specifics_has_grade(self):
        specs = [{"name": "Grade", "value": "VF+"}]
        grade, source, desc_grade = ebay_fetch.extract_grade(
            specs, "SPIDER-MAN #300", "9.6 beautiful edition"
        )
        assert grade == "VF+"
        assert source == "item_specifics"
        assert desc_grade is None


class TestExtractVariant:
    def test_newsstand_in_title(self):
        assert ebay_fetch.extract_variant([], "Amazing Spider-Man #300 Newsstand Edition") == "Newsstand"

    def test_direct_in_title(self):
        assert ebay_fetch.extract_variant([], "X-Men #1 Direct Edition") == "Direct"

    def test_whitman_in_title(self):
        assert ebay_fetch.extract_variant([], "Star Wars #1 Whitman Variant") == "Whitman"

    def test_from_item_specifics(self):
        specs = [{"name": "Variant", "value": "Newsstand"}]
        assert ebay_fetch.extract_variant(specs, "Spider-Man #300") == "Newsstand"

    def test_edition_specifics(self):
        specs = [{"name": "Edition", "value": "First Edition"}]
        assert ebay_fetch.extract_variant(specs, "Spider-Man #300") == "First Edition"

    def test_no_variant(self):
        assert ebay_fetch.extract_variant([], "Amazing Spider-Man #300 NM") is None

    def test_case_insensitive_title(self):
        assert ebay_fetch.extract_variant([], "ASM #300 NEWSSTAND") == "Newsstand"


class TestFormatEndDate:
    def test_iso_utc(self):
        result = ebay_fetch.format_end_date("2026-04-20T21:00:00.000Z")
        assert result is not None
        # Verify it produces a valid datetime string (date may shift due to local tz)
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", result)

    def test_none(self):
        assert ebay_fetch.format_end_date(None) is None

    def test_empty_string(self):
        assert ebay_fetch.format_end_date("") is None

    def test_iso_with_offset(self):
        result = ebay_fetch.format_end_date("2026-04-20T14:00:00.000-07:00")
        assert result is not None
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", result)


class TestTruncate:
    def test_short_string(self):
        assert ebay_fetch.truncate("hello", 10) == "hello"

    def test_exact_width(self):
        assert ebay_fetch.truncate("hello", 5) == "hello"

    def test_truncated(self):
        result = ebay_fetch.truncate("hello world", 6)
        assert len(result) == 6
        assert result.endswith("\u2026")

    def test_none(self):
        assert ebay_fetch.truncate(None, 10) == ""

    def test_empty(self):
        assert ebay_fetch.truncate("", 10) == ""


class TestParseItem:
    """Test parse_item with synthetic API response data."""

    @pytest.fixture
    def auction_response(self):
        return {
            "itemId": "v1|298217294954|0",
            "title": "AMAZING SPIDER-MAN # 300 - (NM-) -MCFARLANE-VENOM",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "296.00", "currency": "USD"},
            "price": {"value": "500.00", "currency": "USD"},
            "bidCount": 35,
            "itemEndDate": "2026-04-19T19:23:00.000Z",
            "condition": "Like New",
            "conditionId": "2750",
            "localizedAspects": [
                {"name": "Series Title", "value": "Amazing Spider-Man"},
                {"name": "Issue Number", "value": "300"},
            ],
            "shortDescription": "First appearance of Venom",
            "itemWebUrl": "https://www.ebay.com/itm/298217294954",
        }

    @pytest.fixture
    def bin_response(self):
        return {
            "itemId": "v1|999999999|0",
            "title": "X-MEN #1 JIM LEE COVER",
            "buyingOptions": ["FIXED_PRICE"],
            "price": {"value": "50.00", "currency": "USD"},
            "itemEndDate": "2026-05-01T00:00:00.000Z",
            "condition": "Very Good",
            "conditionId": "4000",
            "localizedAspects": [],
            "itemWebUrl": "https://www.ebay.com/itm/999999999",
        }

    def test_auction_item(self, auction_response):
        result = ebay_fetch.parse_item(auction_response)
        assert result["item_id"] == "298217294954"
        assert result["title"] == "AMAZING SPIDER-MAN # 300 - (NM-) -MCFARLANE-VENOM"
        assert result["listing_type"] == "Auction"
        assert result["current_price"] == "$296.00"
        assert result["bid_count"] == 35
        assert result["grade"] == "NM-"
        assert result["grade_source"] == "title"
        assert result["condition"] == "Like New"

    def test_bin_item(self, bin_response):
        result = ebay_fetch.parse_item(bin_response)
        assert result["item_id"] == "999999999"
        assert result["listing_type"] == "BIN"
        assert result["current_price"] == "$50.00"
        assert result["bid_count"] is None
        assert result["grade"] is None
        assert result["grade_source"] == "missing"

    def test_auction_uses_current_bid_price(self, auction_response):
        """Auction should use currentBidPrice, not price."""
        result = ebay_fetch.parse_item(auction_response)
        assert result["current_price"] == "$296.00"  # currentBidPrice, not 500.00

    def test_item_specifics_as_dict(self, auction_response):
        result = ebay_fetch.parse_item(auction_response)
        assert result["item_specifics"]["Series Title"] == "Amazing Spider-Man"
        assert result["item_specifics"]["Issue Number"] == "300"

    def test_description_truncation(self, auction_response):
        auction_response["shortDescription"] = "A" * 600
        result = ebay_fetch.parse_item(auction_response)
        assert len(result["description_snippet"]) == 500

    def test_missing_short_description(self, auction_response):
        del auction_response["shortDescription"]
        result = ebay_fetch.parse_item(auction_response)
        assert result["description_snippet"] is None

    def test_listing_url(self, auction_response):
        result = ebay_fetch.parse_item(auction_response)
        assert result["listing_url"] == "https://www.ebay.com/itm/298217294954"

    def test_variant_detected_in_title(self):
        data = {
            "itemId": "v1|123456|0",
            "title": "Amazing Spider-Man #300 Newsstand Edition (NM-)",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "100.00", "currency": "USD"},
            "price": {"value": "100.00", "currency": "USD"},
            "bidCount": 5,
            "itemEndDate": "2026-04-20T00:00:00.000Z",
            "condition": "Good",
            "localizedAspects": [],
            "itemWebUrl": "https://www.ebay.com/itm/123456",
        }
        result = ebay_fetch.parse_item(data)
        assert result["variant"] == "Newsstand"
        assert result["grade"] == "NM-"

    def test_condition_note_on_generic_label(self, auction_response):
        """Generic eBay conditions like 'Like New' should get a disambiguation note."""
        result = ebay_fetch.parse_item(auction_response)
        assert result["condition"] == "Like New"
        assert result["condition_note"] == "eBay category label, not comic grade"

    def test_condition_note_on_brand_new(self, auction_response):
        auction_response["condition"] = "Brand New"
        result = ebay_fetch.parse_item(auction_response)
        assert result["condition_note"] == "eBay category label, not comic grade"

    def test_no_condition_note_for_non_generic(self, auction_response):
        """Non-generic condition values should not get a note."""
        auction_response["condition"] = "Used - Acceptable"
        result = ebay_fetch.parse_item(auction_response)
        assert result["condition_note"] is None

    def test_no_condition_note_when_missing(self, auction_response):
        del auction_response["condition"]
        result = ebay_fetch.parse_item(auction_response)
        assert result["condition_note"] is None

    def test_grade_from_description_in_parse_item(self, auction_response):
        """grade_from_description should surface grades found only in description."""
        auction_response["title"] = "AMAZING SPIDER-MAN #300 MCFARLANE VENOM"
        auction_response["shortDescription"] = "9.6 beautiful edition copy"
        auction_response["localizedAspects"] = []
        result = ebay_fetch.parse_item(auction_response)
        assert result["grade"] is None
        assert result["grade_source"] == "missing"
        assert result["grade_from_description"] == "9.6"

    def test_grade_from_description_none_when_title_has_grade(self, auction_response):
        """grade_from_description should be None when title already yields a grade."""
        result = ebay_fetch.parse_item(auction_response)
        assert result["grade"] == "NM-"
        assert result["grade_from_description"] is None


class TestLoadConfig:
    def test_env_vars_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EBAY_CLIENT_ID", "env-id")
        monkeypatch.setenv("EBAY_CLIENT_SECRET", "env-secret")
        client_id, client_secret, base_url = ebay_fetch.load_config()
        assert client_id == "env-id"
        assert client_secret == "env-secret"
        assert base_url == ebay_fetch.PRODUCTION_BASE

    def test_config_file_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
        monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "client_id": "file-id",
            "client_secret": "file-secret",
            "environment": "sandbox",
        }))
        monkeypatch.setattr(ebay_fetch, "CONFIG_FILE", config_file)
        client_id, client_secret, base_url = ebay_fetch.load_config()
        assert client_id == "file-id"
        assert client_secret == "file-secret"
        assert base_url == ebay_fetch.SANDBOX_BASE

    def test_missing_credentials_exits(self, monkeypatch):
        monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
        monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
        monkeypatch.setattr(ebay_fetch, "CONFIG_FILE", ebay_fetch.Path("/nonexistent/config.json"))
        with pytest.raises(SystemExit):
            ebay_fetch.load_config()


class TestFetchItem:
    """Test fetch_item with mocked HTTP responses."""

    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"itemId": "v1|123|0", "title": "Test"}

        with patch("ebay_fetch.requests.get", return_value=mock_resp):
            result = ebay_fetch.fetch_item("123", "fake-token", ebay_fetch.PRODUCTION_BASE)
        assert result["title"] == "Test"

    def test_404_returns_none(self, capsys):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("ebay_fetch.requests.get", return_value=mock_resp):
            result = ebay_fetch.fetch_item("999", "fake-token", ebay_fetch.PRODUCTION_BASE)
        assert result is None
        assert "not found" in capsys.readouterr().err

    def test_429_retries(self):
        rate_limited = MagicMock()
        rate_limited.status_code = 429

        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"itemId": "v1|123|0", "title": "Retry OK"}

        with patch("ebay_fetch.requests.get", side_effect=[rate_limited, ok]):
            with patch("ebay_fetch.time.sleep"):  # skip actual sleep
                result = ebay_fetch.fetch_item("123", "fake-token", ebay_fetch.PRODUCTION_BASE, retries=2)
        assert result["title"] == "Retry OK"

    def test_server_error_returns_none(self, capsys):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("ebay_fetch.requests.get", return_value=mock_resp):
            result = ebay_fetch.fetch_item("123", "fake-token", ebay_fetch.PRODUCTION_BASE)
        assert result is None


class TestGetToken:
    def test_uses_cached_token(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "token_cache_production.json"
        cache_file.write_text(json.dumps({
            "access_token": "cached-token",
            "expires_at": time.time() + 3600,
        }))
        monkeypatch.setattr(ebay_fetch, "CONFIG_DIR", tmp_path)
        token = ebay_fetch.get_token("id", "secret", ebay_fetch.PRODUCTION_BASE)
        assert token == "cached-token"

    def test_refreshes_expired_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "token_cache_production.json"
        cache_file.write_text(json.dumps({
            "access_token": "old-token",
            "expires_at": time.time() - 100,
        }))
        monkeypatch.setattr(ebay_fetch, "CONFIG_DIR", tmp_path)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-token", "expires_in": 7200}

        with patch("ebay_fetch.requests.post", return_value=mock_resp):
            token = ebay_fetch.get_token("id", "secret", ebay_fetch.PRODUCTION_BASE)
        assert token == "new-token"

    def test_auth_failure_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ebay_fetch, "CONFIG_DIR", tmp_path)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("ebay_fetch.requests.post", return_value=mock_resp):
            with pytest.raises(SystemExit):
                ebay_fetch.get_token("bad-id", "bad-secret", ebay_fetch.PRODUCTION_BASE)


# ============================================================
# Integration Tests — hit real eBay API
# ============================================================

# Skip integration tests if credentials aren't available
_has_credentials = ebay_fetch.CONFIG_FILE.exists() or (
    os.environ.get("EBAY_CLIENT_ID") and os.environ.get("EBAY_CLIENT_SECRET")
)
skip_integration = pytest.mark.skipif(
    not _has_credentials,
    reason="eBay credentials not configured",
)


@skip_integration
class TestIntegrationAuth:
    def test_get_token_real(self):
        client_id, client_secret, base_url = ebay_fetch.load_config()
        token = ebay_fetch.get_token(client_id, client_secret, base_url)
        assert token is not None
        assert len(token) > 50  # OAuth tokens are long


@skip_integration
class TestIntegrationFetch:
    @pytest.fixture(scope="class")
    def auth(self):
        client_id, client_secret, base_url = ebay_fetch.load_config()
        token = ebay_fetch.get_token(client_id, client_secret, base_url)
        return token, base_url

    def test_fetch_single_item(self, auth):
        token, base_url = auth
        data = ebay_fetch.fetch_item("298217294954", token, base_url)
        assert data is not None
        assert "title" in data
        assert "SPIDER-MAN" in data["title"].upper()

    def test_fetch_and_parse(self, auth):
        token, base_url = auth
        data = ebay_fetch.fetch_item("298217294954", token, base_url)
        parsed = ebay_fetch.parse_item(data)
        assert parsed["item_id"] == "298217294954"
        assert parsed["listing_type"] in ("Auction", "BIN")
        assert parsed["current_price"].startswith("$")
        assert parsed["grade"] is not None

    def test_fetch_invalid_item(self, auth):
        token, base_url = auth
        result = ebay_fetch.fetch_item("1", token, base_url)
        assert result is None

    def test_fetch_multiple_items(self, auth):
        token, base_url = auth
        ids = ["298217294954", "298210880012", "306871783258"]
        results = []
        for item_id in ids:
            data = ebay_fetch.fetch_item(item_id, token, base_url)
            if data:
                results.append(ebay_fetch.parse_item(data))
        assert len(results) == 3
        assert all(r["item_id"] in ids for r in results)


@skip_integration
class TestIntegrationCLI:
    """Test the CLI end-to-end as a subprocess."""

    def test_table_output(self):
        result = subprocess.run(
            [sys.executable, _MODULE, "298217294954"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "SPIDER-MAN" in result.stdout.upper()
        assert "298217294954" in result.stdout

    def test_json_output(self):
        result = subprocess.run(
            [sys.executable, _MODULE, "--json", "298217294954"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["item_id"] == "298217294954"

    def test_multiple_items(self):
        result = subprocess.run(
            [sys.executable, _MODULE, "--json",
             "298217294954", "298210880012"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 2

    def test_url_input(self):
        result = subprocess.run(
            [sys.executable, _MODULE, "--json",
             "https://www.ebay.com/itm/298217294954"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data[0]["item_id"] == "298217294954"

    def test_no_args_shows_help(self):
        result = subprocess.run(
            [sys.executable, _MODULE],
            capture_output=True, text=True, timeout=10,
            input="",  # empty stdin
        )
        assert result.returncode != 0

    def test_fields_filter(self):
        result = subprocess.run(
            [sys.executable, _MODULE, "--json",
             "--fields", "item_id,title", "298217294954"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert set(data[0].keys()) == {"item_id", "title"}
