"""Tests for locg.models module."""
from __future__ import annotations

import json

from bs4 import BeautifulSoup

from locg.models import _safe_int, extract_comic_detail, extract_comic_lists, extract_issue, extract_my_details, extract_series


def test_extract_issue_from_fixture(releases_json):
    html = releases_json["list"]
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li", class_="issue")
    issue = extract_issue(li)
    assert issue["id"] == 9559460
    assert issue["name"] == "Batman #8"
    assert issue["publisher"] == "DC Comics"
    assert issue["price"] == 4.99
    assert issue["pulls"] == 32597
    assert issue["potw"] == 11
    assert issue["community_rating"] == 95
    assert "leagueofcomicgeeks.com" in issue["url"]


def test_extract_series_from_fixture(search_series_json):
    html = search_series_json["list"]
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li")
    series = extract_series(li)
    assert series["id"] == 188765
    assert series["name"] == "100% DC"
    assert series["publisher"] == "Panini Comics"
    assert series["start_year"] == 2005
    assert series["end_year"] == 2011
    assert series["issue_count"] == 4
    assert "leagueofcomicgeeks.com" in series["url"]


def test_extract_series_falls_back_to_data_comic_for_issue_results():
    """Search for a specific issue (e.g. "Amazing Spider-Man #229") returns
    <li class="issue" data-comic="..."> rather than series-style markup.
    extract_series must read data-comic so the id field is the comic ID,
    not 0.
    """
    html = """
    <li class="issue" data-comic="5856184" data-pulls="0" data-potw="0"
        data-community="">
      <div class="cover">
        <a href="/comic/5856184/the-amazing-spider-man-229">
          <img class="lazy"
               data-src="https://example.com/cover.jpg"
               alt="cover"/>
        </a>
      </div>
      <div class="title">
        <a href="/comic/5856184/the-amazing-spider-man-229">
          The Amazing Spider-Man #229
        </a>
      </div>
    </li>
    """
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li")
    series = extract_series(li)
    assert series["id"] == 5856184, (
        "When LOCG returns issue-style search results, extract_series must "
        "fall back to reading data-comic on the <li> instead of returning 0."
    )
    assert "Amazing Spider-Man #229" in series["name"]
    assert "leagueofcomicgeeks.com/comic/5856184" in series["url"]


def test_extract_issue_handles_empty_community(releases_json):
    html = releases_json["list"]
    soup = BeautifulSoup(html, "html.parser")
    # Find a variant with data-community=""
    items = soup.find_all("li", class_="issue")
    empty_found = False
    for li in items:
        if li.get("data-community") == "":
            issue = extract_issue(li)
            assert issue["community_rating"] == 0
            empty_found = True
            break
    if not empty_found:
        # Test with synthetic HTML
        synthetic = '<li class="issue" data-comic="123" data-pulls="0" data-potw="0" data-community="" data-parent="0"><div class="title"><a href="/comic/123/test">Test</a></div><div class="publisher">Test Pub</div></li>'
        li = BeautifulSoup(synthetic, "html.parser").find("li")
        issue = extract_issue(li)
        assert issue["community_rating"] == 0


def test_extract_comic_detail_from_fixture(comic_detail_html):
    soup = BeautifulSoup(comic_detail_html, "html.parser")
    detail = extract_comic_detail(soup)
    assert detail["name"] == "Batman #8"
    assert detail["id"] == 9559460
    assert detail["publisher"] == "DC Comics"
    assert detail["store_date"] == "Apr 1, 2026"
    assert detail["price"] == 4.99
    assert detail["pages"] == 36
    assert detail["cover_date"] == "Jun 2026"
    assert detail["upc"] == "76194139163200811"
    assert detail["sku"] == "0226DC0057"
    assert detail["community_rating"] == 3.8
    assert detail["rating_count"] == 2493
    assert detail["series_id"] == 186408
    # Fixture is unauthenticated, so lists should be None
    assert detail["lists"] is None


def test_extract_comic_detail_lists_authenticated():
    """When authenticated, comic-controller divs have data-list and active class."""
    html = """
    <html><head>
    <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/9559460/batman-8"/>
    <meta property="og:description" content="Test"/>
    <meta property="og:image" content="https://example.com/cover.jpg"/>
    </head><body>
    <h1>Batman #8</h1>
    <div class="comic-controller active" data-list="1"></div>
    <div class="comic-controller active" data-list="2"></div>
    <div class="comic-controller" data-list="3"></div>
    <div class="comic-controller active" data-list="5"></div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = extract_comic_detail(soup)
    assert detail["lists"] is not None
    assert detail["lists"]["pull"] is True
    assert detail["lists"]["collection"] is True
    assert detail["lists"]["wish"] is False
    assert detail["lists"]["read"] is True


def test_extract_comic_detail_lists_authenticated_none_active():
    """When authenticated but comic is on no lists, all values are False."""
    html = """
    <html><head>
    <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/123/test"/>
    </head><body>
    <h1>Test</h1>
    <div class="comic-controller" data-list="1"></div>
    <div class="comic-controller" data-list="2"></div>
    <div class="comic-controller" data-list="3"></div>
    <div class="comic-controller" data-list="5"></div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = extract_comic_detail(soup)
    assert detail["lists"] is not None
    assert detail["lists"]["pull"] is False
    assert detail["lists"]["collection"] is False
    assert detail["lists"]["wish"] is False
    assert detail["lists"]["read"] is False


def test_extract_comic_detail_lists_unauthenticated():
    """When not authenticated, comic-controller divs lack data-list; lists is None."""
    html = """
    <html><head>
    <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/123/test"/>
    </head><body>
    <h1>Test</h1>
    <div class="comic-controller" data-toggle="modal" data-target="#modal-login"></div>
    <div class="comic-controller" data-toggle="modal" data-target="#modal-login"></div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    detail = extract_comic_detail(soup)
    assert detail["lists"] is None


def test_safe_int_handles_empty_and_none():
    assert _safe_int("") == 0
    assert _safe_int(None) == 0
    assert _safe_int("42") == 42
    assert _safe_int("abc") == 0
    assert _safe_int(None, default=5) == 5


# --- Bug: extract_issue doesn't parse list membership from series pages ---
#
# On series pages (e.g., /comics/series/106868/spider-man), each <li class="issue">
# contains comic-controller spans that indicate whether the authenticated user
# has the comic in their collection, wish list, etc. This is the SAME markup
# pattern used on comic detail pages (parsed by extract_comic_detail).
#
# Currently extract_issue ignores these spans entirely, so the only way to
# bulk-check ownership is to call `locg comic {id}` one-by-one (one HTTP
# request per comic). For a series like Batman (1940-2011) with 1,672 issues,
# this is impractical.
#
# The fix: extract_issue should parse comic-controller spans and include a
# "lists" field (same format as extract_comic_detail), or None when
# unauthenticated.
#
# This is critical for the eBay sniper use case: given a list of comics to
# bid on, we need to quickly check which ones are already owned. The series
# page has all the data — we just don't extract it.

def test_extract_issue_includes_lists_when_authenticated():
    """extract_issue should parse comic-controller spans on series pages.

    On series pages, each <li> contains toolbar spans like:
      <span class="comic-controller active" data-comic="4773171" data-list="2">
    where data-list="2" = collection and "active" = user has it on that list.

    This is the same pattern as comic detail pages. extract_issue should
    return a "lists" field with the same structure as extract_comic_detail.
    """
    # Real HTML structure from a series page <li> for an owned comic
    html = """
    <li class="issue" data-comic="4773171" data-pulls="97" data-rating=""
        data-community="96" data-parent="0" data-row="1">
      <div class="cover">
        <a href="/comic/4773171/spider-man-1">
          <img alt="spider-man #1" class="lazy"
               data-src="https://example.com/cover.jpg"
               src="data:image/gif;base64,placeholder"/>
        </a>
      </div>
      <div class="comic-list-toolbar-actions d-flex justify-content-between">
        <div class="comic-list-toolbar-actions-minis ml-1">
          <span class="comic-controller" data-comic="4773171" data-list="1"
                data-placement="top" data-toggle="tooltip" title="Add to Pull List">
            <span class="color1 cg-icon-pull"></span>
          </span>
          <span class="comic-controller active" data-comic="4773171" data-list="2"
                data-placement="top" data-toggle="tooltip" title="In Collection">
            <span class="color2 cg-icon-collect"></span>
          </span>
          <span class="comic-controller" data-comic="4773171" data-list="3"
                data-placement="top" data-toggle="tooltip" title="Add to Wish List">
            <span class="color3 cg-icon-wish"></span>
          </span>
          <span class="comic-controller" data-comic="4773171" data-list="5"
                data-placement="top" data-toggle="tooltip" title="Add to Read List">
            <span class="color5 cg-icon-read"></span>
          </span>
        </div>
      </div>
      <div class="title"><a href="/comic/4773171/spider-man-1">Spider-Man #1</a></div>
      <div class="publisher">Marvel Comics</div>
    </li>
    """
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li", class_="issue")
    issue = extract_issue(li)

    assert issue["id"] == 4773171
    assert issue["name"] == "Spider-Man #1"

    # This is the key assertion: lists should be populated
    assert "lists" in issue, (
        "extract_issue should include a 'lists' field when comic-controller "
        "spans with data-list attributes are present (series page, authenticated user)"
    )
    assert issue["lists"] is not None
    assert issue["lists"]["pull"] is False
    assert issue["lists"]["collection"] is True
    assert issue["lists"]["wish"] is False
    assert issue["lists"]["read"] is False


def test_extract_issue_includes_lists_wish_only():
    """A comic on the wish list but NOT in collection should show that correctly.

    This is the exact scenario that caused a false positive in the eBay sniper:
    Spider-Man #11 was on the wish list, but the CLI reported it as owned
    because it couldn't distinguish collection from wish list.
    """
    html = """
    <li class="issue" data-comic="3376691" data-pulls="37" data-rating=""
        data-community="94" data-parent="0" data-row="53">
      <div class="cover">
        <a href="/comic/3376691/spider-man-11">
          <img alt="spider-man #11" class="lazy"
               data-src="https://example.com/cover.jpg"
               src="data:image/gif;base64,placeholder"/>
        </a>
      </div>
      <div class="comic-list-toolbar-actions d-flex justify-content-between">
        <div class="comic-list-toolbar-actions-minis ml-1">
          <span class="comic-controller" data-comic="3376691" data-list="1">
            <span class="color1 cg-icon-pull"></span>
          </span>
          <span class="comic-controller" data-comic="3376691" data-list="2">
            <span class="color2 cg-icon-collect"></span>
          </span>
          <span class="comic-controller active" data-comic="3376691" data-list="3">
            <span class="color3 cg-icon-wish"></span>
          </span>
          <span class="comic-controller" data-comic="3376691" data-list="5">
            <span class="color5 cg-icon-read"></span>
          </span>
        </div>
      </div>
      <div class="title"><a href="/comic/3376691/spider-man-11">Spider-Man #11</a></div>
      <div class="publisher">Marvel Comics</div>
    </li>
    """
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li", class_="issue")
    issue = extract_issue(li)

    assert issue["lists"] is not None
    assert issue["lists"]["collection"] is False, (
        "Spider-Man #11 is on the wish list, NOT in the collection. "
        "This distinction is critical for avoiding duplicate purchases."
    )
    assert issue["lists"]["wish"] is True


def test_extract_issue_lists_none_when_unauthenticated():
    """When not authenticated, comic-controller spans lack data-list.

    extract_issue should return lists=None (not crash or return empty dict).
    """
    html = """
    <li class="issue" data-comic="4773171" data-pulls="97" data-rating=""
        data-community="96" data-parent="0" data-row="1">
      <div class="title"><a href="/comic/4773171/spider-man-1">Spider-Man #1</a></div>
      <div class="publisher">Marvel Comics</div>
    </li>
    """
    soup = BeautifulSoup(html, "html.parser")
    li = soup.find("li", class_="issue")
    issue = extract_issue(li)

    assert issue.get("lists") is None, (
        "When no comic-controller spans with data-list are present "
        "(unauthenticated or non-series page), lists should be None"
    )


# --- extract_comic_lists tests ---


def test_extract_comic_lists_authenticated():
    """extract_comic_lists returns id, name, and list membership."""
    html = """
    <html><head>
    <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/9559460/batman-8"/>
    </head><body>
    <h1>Batman #8</h1>
    <div class="comic-controller active" data-list="1"></div>
    <div class="comic-controller active" data-list="2"></div>
    <div class="comic-controller" data-list="3"></div>
    <div class="comic-controller" data-list="5"></div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = extract_comic_lists(soup)
    assert result["id"] == 9559460
    assert result["name"] == "Batman #8"
    assert result["lists"]["pull"] is True
    assert result["lists"]["collection"] is True
    assert result["lists"]["wish"] is False
    assert result["lists"]["read"] is False
    # Should NOT contain extra fields like description, creators, etc.
    assert "description" not in result
    assert "creators" not in result
    assert "price" not in result


def test_extract_comic_lists_unauthenticated():
    """extract_comic_lists returns lists=None when unauthenticated."""
    html = """
    <html><head>
    <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/123/test"/>
    </head><body>
    <h1>Test Comic</h1>
    <div class="comic-controller" data-toggle="modal" data-target="#modal-login"></div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = extract_comic_lists(soup)
    assert result["id"] == 123
    assert result["name"] == "Test Comic"
    assert result["lists"] is None


# --- extract_my_details tests ---


def test_extract_my_details_reads_data_initial(comic_detail_my_details_html):
    """extract_my_details must read data-initial attributes, NOT value or selected."""
    soup = BeautifulSoup(comic_detail_my_details_html, "html.parser")
    details = extract_my_details(soup)

    # Populated fields
    assert details["comic_id"] == "6512949"
    assert details["price_paid"] == "99.99"
    assert details["condition"] == "white pages"
    assert details["grading"] == "8.5"
    assert details["grading_company"] == "CGC"
    assert details["media"] == "1"  # select with data-initial
    assert details["slabbing"] == "0"
    assert details["notes"] == "private note"
    assert details["owner"] == "me"
    assert details["storage_box"] == "Box A"
    assert details["purchase_store"] == "LCS"
    assert details["date_purchased"] == "4/1/2026"
    assert details["copy_num"] == "1"
    assert details["quantity"] == "1"
    assert details["signature"] == ""


def test_extract_my_details_ignores_visible_selected_markup(comic_detail_my_details_html):
    """The HTML template marks 'None' as selected even when data-initial is 8.5.
    extract_my_details must trust data-initial, not <option selected>."""
    soup = BeautifulSoup(comic_detail_my_details_html, "html.parser")
    details = extract_my_details(soup)

    # If the parser incorrectly read <option selected>, grading would be "0".
    # The correct value (from data-initial on the <select>) is "8.5".
    assert details["grading"] != "0"
    assert details["grading"] == "8.5"
