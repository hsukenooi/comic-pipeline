"""Tests for locg.parser module."""
from __future__ import annotations

import json

from bs4 import BeautifulSoup, Tag

from locg.parser import (
    extract_date_timestamp,
    extract_id_from_href,
    extract_price,
    get_text_clean,
    parse_list_response,
)


def test_parse_list_response(releases_json):
    text = json.dumps(releases_json)
    count, soup = parse_list_response(text)
    assert count == 170
    assert isinstance(soup, BeautifulSoup)
    issues = soup.find_all("li", class_="issue")
    assert len(issues) > 0


def test_extract_id_from_href():
    assert extract_id_from_href("/comic/9559460/batman-8") == 9559460
    assert extract_id_from_href("/comics/series/186408/batman") == 186408
    assert extract_id_from_href("/no-id-here") is None


def test_extract_price():
    assert extract_price(" \u00b7 $4.99") == 4.99
    assert extract_price("$12.50") == 12.50
    assert extract_price("no price") is None


def test_extract_date_timestamp():
    html = '<div><span class="date" data-date="1775016000">Apr 1st, 2026</span></div>'
    tag = BeautifulSoup(html, "html.parser").find("div")
    result = extract_date_timestamp(tag)
    assert result == "2026-04-01"


def test_get_text_clean_returns_empty_for_none():
    assert get_text_clean(None) == ""
