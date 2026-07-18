"""Tests for locg.commands module."""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, call

from locg.client import AuthRequired
from locg.commands import (
    _PAGE_SIZE,
    _fetch_user_list_page,
    _filter_by_list_membership,
    _filter_by_title,
    _get_user_list,
    _get_week_date,
    _normalize_series_name,
    _pick_best_series,
    cmd_add,
    cmd_check_lists,
    cmd_collection,
    cmd_collection_has,
    cmd_find,
    cmd_lookup,
    cmd_pull_list,
    cmd_read_list,
    cmd_releases,
    cmd_remove,
    cmd_search,
    cmd_update,
    cmd_wish_list,
    parse_lookup_spec,
)


def _make_issue_html(comic_id: int, name: str = "Issue") -> str:
    """Build a minimal <li class='issue'> HTML snippet for testing."""
    return (
        f'<li class="issue" data-comic="{comic_id}" data-pulls="0" '
        f'data-potw="0" data-community="0">'
        f'<div class="title"><a href="/comic/{comic_id}/x">{name} #{comic_id}</a></div>'
        f'<div class="publisher">Test Pub</div>'
        f'</li>'
    )


def _make_issue_html_with_lists(
    comic_id: int,
    name: str = "Issue",
    active_lists: list[int] | None = None,
) -> str:
    """Build an <li class='issue'> with comic-controller spans for list membership.

    *active_lists* is a list of LOCG list IDs (1=pull, 2=collection, 3=wish, 5=read)
    that should be marked as active.
    """
    if active_lists is None:
        active_lists = []
    all_lists = [1, 2, 3, 5]
    controllers = ""
    for lid in all_lists:
        active = " active" if lid in active_lists else ""
        controllers += (
            f'<span class="comic-controller{active}" '
            f'data-comic="{comic_id}" data-list="{lid}"></span>'
        )
    return (
        f'<li class="issue" data-comic="{comic_id}" data-pulls="0" '
        f'data-potw="0" data-community="0">'
        f'{controllers}'
        f'<div class="title"><a href="/comic/{comic_id}/x">{name} #{comic_id}</a></div>'
        f'<div class="publisher">Test Pub</div>'
        f'</li>'
    )


def _make_list_response_with_lists(
    items: list[tuple[int, str, list[int]]],
    total_count: int,
) -> str:
    """Build a JSON response with list membership data.

    *items* is a list of (comic_id, name, active_list_ids) tuples.
    """
    html = "".join(
        _make_issue_html_with_lists(cid, name, active)
        for cid, name, active in items
    )
    return json.dumps({"count": total_count, "list": html})


def _make_list_response(comic_ids: list[int], total_count: int) -> str:
    """Build a JSON string mimicking the /comic/get_comics response."""
    html = "".join(_make_issue_html(cid) for cid in comic_ids)
    return json.dumps({"count": total_count, "list": html})


def test_cmd_search_returns_series(mock_client, search_series_json):
    resp = MagicMock()
    resp.text = json.dumps(search_series_json)
    mock_client.get.return_value = resp
    result = cmd_search(mock_client, "batman")
    assert isinstance(result, list)
    assert len(result) > 0
    assert result[0]["name"] == "100% DC"
    mock_client.get.assert_called_once()


def test_cmd_search_no_results(mock_client):
    resp = MagicMock()
    resp.text = json.dumps({"count": 0, "list": "<ul></ul>"})
    mock_client.get.return_value = resp
    result = cmd_search(mock_client, "zzzznonexistent")
    assert result == []


def test_cmd_releases_returns_issues(mock_client, releases_json):
    resp = MagicMock()
    resp.text = json.dumps(releases_json)
    mock_client.get.return_value = resp
    result = cmd_releases(mock_client)
    assert isinstance(result, list)
    assert len(result) > 0
    assert result[0]["id"] == 9559460


def test_cmd_releases_with_date(mock_client, releases_json):
    resp = MagicMock()
    resp.text = json.dumps(releases_json)
    mock_client.get.return_value = resp
    result = cmd_releases(mock_client, "2026-04-01")
    assert isinstance(result, list)
    # Verify the date was formatted correctly in the API call
    call_kwargs = mock_client.get.call_args
    params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs[0][1]
    assert params["date"] == "4/1/2026"


def test_cmd_collection_without_auth_raises(mock_client):
    mock_client.require_auth.side_effect = AuthRequired("Not logged in. Run: locg login")
    try:
        cmd_collection(mock_client)
        assert False, "Should have raised AuthRequired"
    except AuthRequired as e:
        assert "Not logged in" in str(e)


def test_cmd_collection_stale_session_raises(mock_client):
    """When the API returns data-user="0", detect expired session."""
    resp = MagicMock()
    resp.text = json.dumps({
        "count": 0,
        "list": '<ul data-user="0"></ul>',
    })
    mock_client.get.return_value = resp
    try:
        cmd_collection(mock_client)
        assert False, "Should have raised AuthRequired"
    except AuthRequired as e:
        assert "expired" in str(e).lower()


def test_cmd_add_posts_correct_data(mock_client):
    resp = MagicMock()
    resp.json.return_value = {"status": "ok"}
    mock_client.post.return_value = resp
    result = cmd_add(mock_client, "collection", 9559460)
    assert result == {"status": "ok"}
    mock_client.post.assert_called_once_with("/comic/my_list_move", data={
        "comic_id": 9559460,
        "list_id": 2,
        "action_id": 1,
    })


def test_cmd_add_invalid_list(mock_client):
    result = cmd_add(mock_client, "invalid", 123)
    assert "error" in result
    assert "Invalid list" in result["error"]


def test_cmd_remove_posts_correct_data(mock_client):
    resp = MagicMock()
    resp.json.return_value = {"status": "ok"}
    mock_client.post.return_value = resp
    result = cmd_remove(mock_client, "pull", 9559460)
    assert result == {"status": "ok"}
    mock_client.post.assert_called_once_with("/comic/my_list_move", data={
        "comic_id": 9559460,
        "list_id": 1,
        "action_id": 0,
    })


def test_get_week_date_formats_correctly():
    assert _get_week_date("2026-04-01") == "4/1/2026"
    assert _get_week_date("2026-12-25") == "12/25/2026"
    # Without target, returns some date in M/D/YYYY format
    result = _get_week_date()
    parts = result.split("/")
    assert len(parts) == 3


# --- Title filter tests (workaround for LOCG list+title API bug) ---

def test_filter_by_title_case_insensitive():
    issues = [
        {"id": 1, "name": "Batman #1"},
        {"id": 2, "name": "Superman #1"},
        {"id": 3, "name": "Batman/Superman #1"},
    ]
    result = _filter_by_title(issues, "batman")
    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[1]["id"] == 3


def test_filter_by_title_no_match():
    issues = [
        {"id": 1, "name": "Batman #1"},
        {"id": 2, "name": "Superman #1"},
    ]
    result = _filter_by_title(issues, "spider-man")
    assert result == []


def test_filter_by_title_empty_string_returns_all():
    """An empty title string should not filter anything (treated as falsy by caller)."""
    issues = [
        {"id": 1, "name": "Batman #1"},
        {"id": 2, "name": "Superman #1"},
    ]
    # _filter_by_title with empty string matches everything (empty needle)
    result = _filter_by_title(issues, "")
    assert len(result) == 2


def test_filter_by_title_substring_match():
    issues = [
        {"id": 1, "name": "The Amazing Spider-Man #100"},
        {"id": 2, "name": "Spider-Man 2099 #1"},
        {"id": 3, "name": "Batman #50"},
    ]
    result = _filter_by_title(issues, "spider-man")
    assert len(result) == 2
    assert all("Spider-Man" in i["name"] for i in result)


def test_cmd_collection_with_title_filter(mock_client, releases_json):
    """cmd_collection with title filters results client-side."""
    resp = MagicMock()
    resp.text = json.dumps(releases_json)
    mock_client.get.return_value = resp
    # The releases fixture has "Batman #8" as the first issue
    result = cmd_collection(mock_client, title="batman")
    assert isinstance(result, list)
    # All results should contain "batman" (case-insensitive) in the name
    for issue in result:
        assert "batman" in issue["name"].lower()


def test_cmd_collection_without_title_returns_all(mock_client, releases_json):
    """cmd_collection without title returns all issues unfiltered."""
    resp = MagicMock()
    resp.text = json.dumps(releases_json)
    mock_client.get.return_value = resp
    result_no_filter = cmd_collection(mock_client)
    assert isinstance(result_no_filter, list)
    assert len(result_no_filter) > 0


# --- Pagination tests ---

def test_get_user_list_single_page_no_pagination(mock_client):
    """When count <= items returned, no extra requests are made."""
    ids = list(range(1, 51))  # 50 items, well under 140
    resp = MagicMock()
    resp.text = _make_list_response(ids, total_count=50)
    mock_client.get.return_value = resp

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 50
    # Only one GET call (no pagination needed)
    assert mock_client.get.call_count == 1


def test_get_user_list_paginates_two_pages(mock_client):
    """When count > items in first page, a second request is made."""
    page1_ids = list(range(1, 141))   # 140 items
    page2_ids = list(range(141, 201))  # 60 items
    total = 200

    resp1 = MagicMock()
    resp1.text = _make_list_response(page1_ids, total_count=total)
    resp2 = MagicMock()
    resp2.text = _make_list_response(page2_ids, total_count=total)
    mock_client.get.side_effect = [resp1, resp2]

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 200
    assert mock_client.get.call_count == 2
    # Verify second call includes list_mode_offset
    second_call_params = mock_client.get.call_args_list[1][1]["params"]
    assert second_call_params["list_mode_offset"] == "140"


def test_get_user_list_paginates_three_pages(mock_client):
    """Three pages of results are fetched correctly."""
    page1_ids = list(range(1, 141))     # 140 items
    page2_ids = list(range(141, 281))   # 140 items
    page3_ids = list(range(281, 301))   # 20 items
    total = 300

    resp1 = MagicMock()
    resp1.text = _make_list_response(page1_ids, total_count=total)
    resp2 = MagicMock()
    resp2.text = _make_list_response(page2_ids, total_count=total)
    resp3 = MagicMock()
    resp3.text = _make_list_response(page3_ids, total_count=total)
    mock_client.get.side_effect = [resp1, resp2, resp3]

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 300
    assert mock_client.get.call_count == 3
    # Check offsets
    assert "list_mode_offset" not in mock_client.get.call_args_list[0][1]["params"]
    assert mock_client.get.call_args_list[1][1]["params"]["list_mode_offset"] == "140"
    assert mock_client.get.call_args_list[2][1]["params"]["list_mode_offset"] == "280"


def test_get_user_list_stops_on_empty_page(mock_client):
    """If the server returns an empty page, pagination stops gracefully."""
    page1_ids = list(range(1, 141))  # 140 items
    total = 200  # Server claims 200 but second page is empty

    resp1 = MagicMock()
    resp1.text = _make_list_response(page1_ids, total_count=total)
    resp2 = MagicMock()
    resp2.text = _make_list_response([], total_count=total)
    mock_client.get.side_effect = [resp1, resp2]

    result = _get_user_list(mock_client, "collection")
    # Should return what we got (140), not loop forever
    assert len(result) == 140
    assert mock_client.get.call_count == 2


def test_get_user_list_deduplicates_overlapping_pages(mock_client):
    """If pages overlap (duplicate IDs), duplicates are removed."""
    page1_ids = list(range(1, 141))     # 140 items (1-140)
    page2_ids = list(range(131, 201))   # 70 items (131-200), 10 overlap
    total = 200

    resp1 = MagicMock()
    resp1.text = _make_list_response(page1_ids, total_count=total)
    resp2 = MagicMock()
    resp2.text = _make_list_response(page2_ids, total_count=total)
    mock_client.get.side_effect = [resp1, resp2]

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 200  # 140 + 70 - 10 duplicates = 200
    # All IDs should be unique
    ids = [r["id"] for r in result]
    assert len(ids) == len(set(ids))


def test_get_user_list_exact_page_boundary(mock_client):
    """When count == _PAGE_SIZE exactly, a speculative fetch is needed.

    The LOCG API lies about total count (always reports 140), so when we
    get exactly 140 items we can't tell if there are more.  We must
    speculatively fetch page 2 to find out.  If it's empty, we stop.
    """
    ids = list(range(1, _PAGE_SIZE + 1))  # exactly 140
    resp1 = MagicMock()
    resp1.text = _make_list_response(ids, total_count=_PAGE_SIZE)
    resp2 = MagicMock()
    resp2.text = _make_list_response([], total_count=0)  # empty page 2
    mock_client.get.side_effect = [resp1, resp2]

    result = _get_user_list(mock_client, "collection")
    assert len(result) == _PAGE_SIZE
    assert mock_client.get.call_count == 2  # speculative fetch for page 2


def test_get_user_list_first_request_no_offset_param(mock_client):
    """The first request should NOT include list_mode_offset."""
    ids = list(range(1, 11))
    resp = MagicMock()
    resp.text = _make_list_response(ids, total_count=10)
    mock_client.get.return_value = resp

    _get_user_list(mock_client, "collection", order="alpha-asc")
    params = mock_client.get.call_args[1]["params"]
    assert "list_mode_offset" not in params
    assert params["list"] == "collection"
    assert params["order"] == "alpha-asc"


# --- Bug: LOCG API returns count == page size even when more items exist ---
#
# The real LOCG API returns count=140 and 140 items on every page, even when
# the user's collection has 500+ comics. The current pagination logic compares
# `offset < total_count` which is `140 < 140` → False, so it never fetches
# page 2.
#
# The fix should detect that count == _PAGE_SIZE (140) AND items == _PAGE_SIZE
# as a signal that there MAY be more pages, and speculatively fetch the next
# page. If the next page returns items, keep going. If empty, stop.
#
# This is the actual behavior observed against the live LOCG API on 2026-04-08.
# A collection with 500+ comics returns count=140 on every request. The
# list_mode_offset parameter DOES return different items per page — the server
# just reports count=140 regardless.

def test_get_user_list_paginates_when_count_equals_page_size(mock_client):
    """Bug: LOCG API reports count=140 even when more items exist.

    When the API returns count == _PAGE_SIZE (140) and exactly 140 items,
    the client should speculatively request the next page, because the
    server may have more data.  It should keep paginating until a page
    returns fewer than _PAGE_SIZE items.

    Real-world scenario: user has 350 comics. The API returns:
      Page 1: count=140, 140 items  (comics 1-140)
      Page 2: count=140, 140 items  (comics 141-280)
      Page 3: count=70,   70 items  (comics 281-350)
    Current code stops after page 1 because 140 < 140 is False.
    """
    page1_ids = list(range(1, 141))      # 140 items
    page2_ids = list(range(141, 281))    # 140 items
    page3_ids = list(range(281, 351))    # 70 items (final page)

    resp1 = MagicMock()
    resp1.text = _make_list_response(page1_ids, total_count=140)  # Bug: count=140
    resp2 = MagicMock()
    resp2.text = _make_list_response(page2_ids, total_count=140)  # Bug: count=140
    resp3 = MagicMock()
    resp3.text = _make_list_response(page3_ids, total_count=70)   # Final page
    mock_client.get.side_effect = [resp1, resp2, resp3]

    result = _get_user_list(mock_client, "collection")

    # Should have fetched ALL 350 items across 3 pages
    assert len(result) == 350, (
        f"Expected 350 items but got {len(result)}. "
        f"Pagination likely stopped after page 1 because count ({_PAGE_SIZE}) "
        f"== items returned ({_PAGE_SIZE}), but the server had more data. "
        f"Fix: when count == _PAGE_SIZE and items == _PAGE_SIZE, speculatively "
        f"fetch the next page."
    )
    assert mock_client.get.call_count == 3


def test_get_user_list_no_speculative_fetch_when_under_page_size(mock_client):
    """When count < _PAGE_SIZE, do NOT speculatively fetch another page.

    If a user has 50 comics, the API returns count=50, 50 items.
    No extra request should be made.
    """
    ids = list(range(1, 51))
    resp = MagicMock()
    resp.text = _make_list_response(ids, total_count=50)
    mock_client.get.return_value = resp

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 50
    assert mock_client.get.call_count == 1


def test_get_user_list_speculative_fetch_stops_on_empty_page(mock_client):
    """If count == _PAGE_SIZE but the next page is empty, stop gracefully.

    Edge case: user has exactly 140 comics. API returns count=140, 140 items.
    We speculatively fetch page 2, get 0 items, and stop.
    """
    page1_ids = list(range(1, 141))

    resp1 = MagicMock()
    resp1.text = _make_list_response(page1_ids, total_count=140)
    resp2 = MagicMock()
    resp2.text = _make_list_response([], total_count=0)
    mock_client.get.side_effect = [resp1, resp2]

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 140
    assert mock_client.get.call_count == 2  # One speculative fetch that returned empty


# --- List membership filtering tests (workaround for LOCG list param bug) ---


def test_filter_by_list_membership_keeps_matching_items():
    """Items with lists[list_name]=True are kept."""
    issues = [
        {"id": 1, "name": "A", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
        {"id": 2, "name": "B", "lists": {"pull": False, "collection": False, "wish": True, "read": False}},
        {"id": 3, "name": "C", "lists": {"pull": False, "collection": True, "wish": False, "read": True}},
    ]
    result = _filter_by_list_membership(issues, "collection")
    assert len(result) == 2
    assert [r["id"] for r in result] == [1, 3]


def test_filter_by_list_membership_filters_wish_list():
    """Filtering for wish list keeps only wish=True items."""
    issues = [
        {"id": 1, "name": "A", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
        {"id": 2, "name": "B", "lists": {"pull": False, "collection": False, "wish": True, "read": False}},
        {"id": 3, "name": "C", "lists": {"pull": True, "collection": False, "wish": True, "read": False}},
    ]
    result = _filter_by_list_membership(issues, "wish")
    assert len(result) == 2
    assert [r["id"] for r in result] == [2, 3]


def test_filter_by_list_membership_keeps_items_with_none_lists():
    """Items with lists=None (unauthenticated) are kept, not dropped."""
    issues = [
        {"id": 1, "name": "A", "lists": None},
        {"id": 2, "name": "B", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
        {"id": 3, "name": "C", "lists": None},
    ]
    result = _filter_by_list_membership(issues, "collection")
    assert len(result) == 3  # All kept: 2 with None + 1 matching


def test_filter_by_list_membership_removes_all_non_matching():
    """When no items match the list, result is empty."""
    issues = [
        {"id": 1, "name": "A", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
        {"id": 2, "name": "B", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
    ]
    result = _filter_by_list_membership(issues, "wish")
    assert result == []


def test_filter_by_list_membership_noop_when_all_match():
    """When all items are on the requested list, nothing is removed (no-op)."""
    issues = [
        {"id": 1, "name": "A", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
        {"id": 2, "name": "B", "lists": {"pull": False, "collection": True, "wish": False, "read": False}},
    ]
    result = _filter_by_list_membership(issues, "collection")
    assert len(result) == 2


def test_filter_by_list_membership_empty_input():
    """Empty input list returns empty output."""
    result = _filter_by_list_membership([], "collection")
    assert result == []


def test_get_user_list_filters_by_list_membership(mock_client):
    """_get_user_list should filter results to only include items on the requested list.

    This is the core fix for the LOCG API bug where the list parameter is ignored.
    """
    # Simulate API returning comics from ALL lists (the bug)
    items = [
        (1, "Batman", [2]),        # collection only
        (2, "Superman", [3]),      # wish only
        (3, "Flash", [2, 5]),      # collection + read
        (4, "Aquaman", [1]),       # pull only
        (5, "Wonder Woman", [2]),  # collection only
    ]
    resp = MagicMock()
    resp.text = _make_list_response_with_lists(items, total_count=5)
    mock_client.get.return_value = resp

    result = _get_user_list(mock_client, "collection")
    assert len(result) == 3
    result_ids = [r["id"] for r in result]
    assert 1 in result_ids   # Batman - in collection
    assert 3 in result_ids   # Flash - in collection
    assert 5 in result_ids   # Wonder Woman - in collection
    assert 2 not in result_ids  # Superman - wish only
    assert 4 not in result_ids  # Aquaman - pull only


def test_get_user_list_filters_wish_list(mock_client):
    """cmd_wish_list should only return items on the wish list."""
    items = [
        (1, "Batman", [2]),        # collection only
        (2, "Superman", [3]),      # wish only
        (3, "Flash", [3, 2]),      # wish + collection
    ]
    resp = MagicMock()
    resp.text = _make_list_response_with_lists(items, total_count=3)
    mock_client.get.return_value = resp

    result = cmd_wish_list(mock_client)
    assert len(result) == 2
    result_ids = [r["id"] for r in result]
    assert 2 in result_ids   # Superman - on wish
    assert 3 in result_ids   # Flash - on wish
    assert 1 not in result_ids  # Batman - collection only


def test_get_user_list_filters_pull_list(mock_client):
    """cmd_pull_list should only return items on the pull list."""
    items = [
        (1, "Batman", [1, 2]),     # pull + collection
        (2, "Superman", [2]),      # collection only
        (3, "Flash", [1]),         # pull only
    ]
    resp = MagicMock()
    resp.text = _make_list_response_with_lists(items, total_count=3)
    mock_client.get.return_value = resp

    result = cmd_pull_list(mock_client)
    assert len(result) == 2
    result_ids = [r["id"] for r in result]
    assert 1 in result_ids
    assert 3 in result_ids


def test_get_user_list_filters_read_list(mock_client):
    """cmd_read_list should only return items on the read list."""
    items = [
        (1, "Batman", [5]),        # read only
        (2, "Superman", [2]),      # collection only
        (3, "Flash", [5, 2]),      # read + collection
    ]
    resp = MagicMock()
    resp.text = _make_list_response_with_lists(items, total_count=3)
    mock_client.get.return_value = resp

    result = cmd_read_list(mock_client)
    assert len(result) == 2
    result_ids = [r["id"] for r in result]
    assert 1 in result_ids
    assert 3 in result_ids


def test_get_user_list_title_filter_applies_after_list_filter(mock_client):
    """Title filter should apply AFTER list membership filter.

    If we have Batman in collection and Batman on wish list,
    filtering collection + title=batman should only return the collection one.
    """
    items = [
        (1, "Batman", [2]),        # collection only
        (2, "Batman Wish", [3]),   # wish only (has "batman" in name)
        (3, "Superman", [2]),      # collection only
    ]
    resp = MagicMock()
    resp.text = _make_list_response_with_lists(items, total_count=3)
    mock_client.get.return_value = resp

    result = cmd_collection(mock_client, title="batman")
    # Should get only Batman #1 (in collection AND matches title)
    # NOT Batman Wish #2 (matches title but NOT in collection)
    assert len(result) == 1
    assert result[0]["id"] == 1


# --- cmd_check_lists tests ---


def _make_comic_detail_html(comic_id: int, name: str, list_states: dict[int, bool] | None = None) -> str:
    """Build minimal comic detail HTML for testing cmd_check_lists.

    list_states maps list_id (1=pull, 2=collection, 3=wish, 5=read) to active bool.
    If None, simulates unauthenticated (no data-list attributes).
    """
    controllers = ""
    if list_states is not None:
        for list_id, active in list_states.items():
            cls = "comic-controller active" if active else "comic-controller"
            controllers += f'<div class="{cls}" data-list="{list_id}"></div>\n'
    else:
        controllers = '<div class="comic-controller" data-toggle="modal" data-target="#modal-login"></div>'

    return f"""
    <html><head>
    <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/{comic_id}/{name.lower().replace(' ', '-')}"/>
    <meta property="og:description" content="Test"/>
    <meta property="og:image" content="https://example.com/cover.jpg"/>
    </head><body>
    <h1>{name}</h1>
    {controllers}
    </body></html>
    """


def test_cmd_check_lists_single_comic(mock_client):
    """Check list membership for a single comic."""
    html = _make_comic_detail_html(9559460, "Batman #8", {1: False, 2: True, 3: False, 5: False})
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    mock_client.get.return_value = resp

    result = cmd_check_lists(mock_client, [9559460])
    assert len(result) == 1
    assert result[0]["id"] == 9559460
    assert result[0]["name"] == "Batman #8"
    assert result[0]["lists"]["collection"] is True
    assert result[0]["lists"]["pull"] is False
    mock_client.require_auth.assert_called_once()


def test_cmd_check_lists_multiple_comics(mock_client):
    """Check list membership for multiple comics."""
    html1 = _make_comic_detail_html(100, "Batman #1", {1: False, 2: True, 3: False, 5: False})
    html2 = _make_comic_detail_html(200, "Superman #1", {1: False, 2: False, 3: True, 5: False})
    html3 = _make_comic_detail_html(300, "Flash #1", {1: True, 2: True, 3: False, 5: True})

    resp1 = MagicMock(status_code=200, text=html1)
    resp2 = MagicMock(status_code=200, text=html2)
    resp3 = MagicMock(status_code=200, text=html3)
    mock_client.get.side_effect = [resp1, resp2, resp3]

    result = cmd_check_lists(mock_client, [100, 200, 300])
    assert len(result) == 3

    assert result[0]["id"] == 100
    assert result[0]["lists"]["collection"] is True

    assert result[1]["id"] == 200
    assert result[1]["lists"]["wish"] is True
    assert result[1]["lists"]["collection"] is False

    assert result[2]["id"] == 300
    assert result[2]["lists"]["pull"] is True
    assert result[2]["lists"]["collection"] is True
    assert result[2]["lists"]["read"] is True

    assert mock_client.get.call_count == 3


def test_cmd_check_lists_handles_404(mock_client):
    """Invalid comic IDs should return error entries, not crash."""
    html = _make_comic_detail_html(100, "Batman #1", {1: False, 2: True, 3: False, 5: False})
    resp_ok = MagicMock(status_code=200, text=html)
    resp_404 = MagicMock(status_code=404)
    mock_client.get.side_effect = [resp_ok, resp_404]

    result = cmd_check_lists(mock_client, [100, 999999])
    assert len(result) == 2

    assert result[0]["id"] == 100
    assert result[0]["lists"]["collection"] is True

    assert result[1]["id"] == 999999
    assert result[1]["name"] is None
    assert result[1]["lists"] is None
    assert result[1]["error"] == "not found"


def test_cmd_check_lists_requires_auth(mock_client):
    """cmd_check_lists should require authentication."""
    mock_client.require_auth.side_effect = AuthRequired("Not logged in. Run: locg login")
    try:
        cmd_check_lists(mock_client, [100])
        assert False, "Should have raised AuthRequired"
    except AuthRequired as e:
        assert "Not logged in" in str(e)
    # Should not have made any HTTP requests
    mock_client.get.assert_not_called()


def test_cmd_check_lists_empty_ids(mock_client):
    """Empty list of IDs should return empty results."""
    result = cmd_check_lists(mock_client, [])
    assert result == []
    mock_client.get.assert_not_called()


def test_validate_grade_accepts_cgc_scale():
    """All LOCG CGC grades must validate."""
    from locg.commands import _validate_grade
    for g in ("0", "0.1", "0.3", "0.5", "1.0", "1.5", "1.8", "2.0", "2.5",
              "3.0", "3.5", "4.0", "4.5", "5.0", "5.5", "6.0", "6.5",
              "7.0", "7.5", "8.0", "8.5", "9.0", "9.2", "9.4", "9.6",
              "9.8", "9.9", "10.0"):
        assert _validate_grade(g) == g


def test_validate_grade_rejects_invalid():
    """Non-CGC values raise ValueError with a clear message."""
    from locg.commands import _validate_grade
    with pytest.raises(ValueError, match="Invalid grade"):
        _validate_grade("11.0")
    with pytest.raises(ValueError, match="Invalid grade"):
        _validate_grade("nine")
    with pytest.raises(ValueError, match="Invalid grade"):
        _validate_grade("9.3")  # not on LOCG's CGC scale


def test_validate_price_formats_cleanly():
    from locg.commands import _validate_price
    assert _validate_price("390") == "390"
    assert _validate_price("390.00") == "390"
    assert _validate_price("9.99") == "9.99"
    assert _validate_price("0") == "0"


def test_validate_price_rejects_non_numeric():
    from locg.commands import _validate_price
    with pytest.raises(ValueError, match="Invalid price"):
        _validate_price("free")


def test_validate_price_rejects_negative():
    from locg.commands import _validate_price
    with pytest.raises(ValueError, match="non-negative"):
        _validate_price("-5")


def test_validate_price_rejects_non_finite():
    from locg.commands import _validate_price
    with pytest.raises(ValueError, match="finite"):
        _validate_price("inf")
    with pytest.raises(ValueError, match="finite"):
        _validate_price("nan")


def test_cmd_add_with_grade_and_price_calls_both_endpoints(mock_client):
    """cmd_add with grade and price must POST both my_list_move then post_my_details."""
    # First POST: my_list_move (success)
    move_resp = MagicMock()
    move_resp.json.return_value = {"status": "ok"}
    move_resp.status_code = 200
    # Second POST: post_my_details (success)
    detail_resp = MagicMock()
    detail_resp.json.return_value = {"type": "success", "text": "This comic has been updated."}
    detail_resp.status_code = 200
    mock_client.post.side_effect = [move_resp, detail_resp]

    result = cmd_add(mock_client, "collection", 12345, grade="8.5", price="390")

    # Two POSTs in order: move then details
    assert mock_client.post.call_count == 2
    first_call = mock_client.post.call_args_list[0]
    assert first_call[0][0] == "/comic/my_list_move"
    assert first_call[1]["data"] == {"comic_id": 12345, "list_id": 2, "action_id": 1}

    second_call = mock_client.post.call_args_list[1]
    assert second_call[0][0] == "/comic/post_my_details"
    # Minimum payload: comic_id plus only the supplied fields
    assert second_call[1]["data"] == {
        "comic_id": 12345,
        "grading": "8.5",
        "price_paid": "390",
    }

    assert result == {
        "status": "ok",
        "added": True,
        "details_saved": True,
        "text": "This comic has been updated.",
    }


def test_cmd_add_details_failure_surfaces_partial(mock_client):
    """If post_my_details fails after the comic is added, cmd_add returns partial."""
    move_resp = MagicMock()
    move_resp.json.return_value = {"status": "ok"}
    move_resp.status_code = 200
    detail_resp = MagicMock()
    detail_resp.json.return_value = {"type": "error", "text": "Something went wrong."}
    detail_resp.status_code = 500
    mock_client.post.side_effect = [move_resp, detail_resp]

    result = cmd_add(mock_client, "collection", 12345, grade="8.5")

    assert result["status"] == "partial"
    assert result["added"] is True
    assert result["details_saved"] is False
    assert "Something went wrong" in result["details_error"]


def test_cmd_add_without_details_only_calls_move(mock_client):
    """cmd_add without grade/price must behave exactly like the old version."""
    move_resp = MagicMock()
    move_resp.json.return_value = {"status": "ok"}
    move_resp.status_code = 200
    mock_client.post.return_value = move_resp

    result = cmd_add(mock_client, "collection", 12345)

    assert mock_client.post.call_count == 1
    assert mock_client.post.call_args[0][0] == "/comic/my_list_move"
    assert result == {"status": "ok"}


def test_cmd_add_rejects_grade_on_non_collection(mock_client):
    result = cmd_add(mock_client, "pull", 12345, grade="8.5")
    assert "error" in result
    assert "collection" in result["error"].lower()
    mock_client.post.assert_not_called()


def test_cmd_add_move_failure_http200_app_error_does_not_call_details(mock_client):
    """If my_list_move returns HTTP 200 with type=error, details must not be called."""
    move_resp = MagicMock()
    move_resp.json.return_value = {"type": "error", "text": "Already in list."}
    move_resp.status_code = 200
    mock_client.post.return_value = move_resp

    result = cmd_add(mock_client, "collection", 12345, grade="8.5")

    # Only one POST — the move. Details must not be called.
    assert mock_client.post.call_count == 1
    assert result == {"type": "error", "text": "Already in list."}


# --- cmd_update tests ---


def test_cmd_update_fetches_then_merges(mock_client, comic_detail_my_details_html):
    """cmd_update must fetch the page, parse data-initial, merge flags, then POST."""
    # GET returns the detail page (collected version)
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = comic_detail_my_details_html
    mock_client.get.return_value = get_resp

    # POST succeeds
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {"type": "success", "text": "Updated."}
    mock_client.post.return_value = post_resp

    result = cmd_update(mock_client, 6512949, grade="9.2", price="500", condition="pristine")

    mock_client.get.assert_called_once_with("/comic/6512949/x")
    assert mock_client.post.call_count == 1
    post_call = mock_client.post.call_args
    assert post_call[0][0] == "/comic/post_my_details"
    payload = post_call[1]["data"]

    # User's flags win
    assert payload["grading"] == "9.2"
    assert payload["price_paid"] == "500"
    assert payload["condition"] == "pristine"

    # Other fields preserved from data-initial
    assert payload["comic_id"] == "6512949"
    assert payload["date_purchased"] == "4/1/2026"
    assert payload["media"] == "1"
    assert payload["grading_company"] == "CGC"
    assert payload["notes"] == "private note"
    assert payload["storage_box"] == "Box A"

    assert result == {"type": "success", "text": "Updated."}


def test_cmd_update_only_condition_preserves_grade(mock_client, comic_detail_my_details_html):
    """Supplying only --condition must leave grading untouched (from data-initial)."""
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = comic_detail_my_details_html
    mock_client.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {"type": "success", "text": "Updated."}
    mock_client.post.return_value = post_resp

    cmd_update(mock_client, 6512949, condition="new note")

    payload = mock_client.post.call_args[1]["data"]
    assert payload["condition"] == "new note"
    assert payload["grading"] == "8.5"  # preserved from data-initial
    assert payload["price_paid"] == "99.99"  # preserved


def test_cmd_update_rejects_non_collection_comic(
    mock_client, comic_detail_my_details_not_collected_html
):
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = comic_detail_my_details_not_collected_html
    mock_client.get.return_value = get_resp

    result = cmd_update(mock_client, 6512949, grade="8.5")

    assert "error" in result
    assert "not in your collection" in result["error"]
    mock_client.post.assert_not_called()


def test_cmd_update_no_flags_errors(mock_client):
    result = cmd_update(mock_client, 12345)

    assert "error" in result
    assert "at least one" in result["error"]
    mock_client.get.assert_not_called()


def test_cmd_update_comic_not_found(mock_client):
    get_resp = MagicMock()
    get_resp.status_code = 404
    mock_client.get.return_value = get_resp

    result = cmd_update(mock_client, 99999, grade="8.5")
    assert "error" in result
    assert "not found" in result["error"]
    mock_client.post.assert_not_called()


def test_cmd_update_rejects_invalid_grade(mock_client):
    """cmd_update must validate grade before making any network calls."""
    result = cmd_update(mock_client, 12345, grade="11.0")
    assert "error" in result
    assert "Invalid grade" in result["error"]
    mock_client.get.assert_not_called()


def test_cmd_update_unexpected_http_error(mock_client):
    get_resp = MagicMock()
    get_resp.status_code = 500
    mock_client.get.return_value = get_resp

    result = cmd_update(mock_client, 12345, grade="8.5")
    assert "error" in result
    assert "500" in result["error"]
    mock_client.post.assert_not_called()




def _make_find_issue_html(comic_id: int, title: str) -> str:
    """Build a series-page-style <li class='issue'> with a given title."""
    return (
        f'<li class="issue" data-comic="{comic_id}" data-pulls="0" '
        f'data-potw="0" data-community="0">'
        f'<div class="title"><a href="/comic/{comic_id}/x">{title}</a></div>'
        f'<div class="publisher">Marvel Comics</div>'
        f'</li>'
    )


def _make_find_response(issues: list[tuple[int, str]], total_count: int) -> str:
    """Build a JSON response of issues for cmd_find tests."""
    html = "".join(_make_find_issue_html(cid, title) for cid, title in issues)
    return json.dumps({"count": total_count, "list": html})


def test_cmd_find_matches_word_boundary(mock_client):
    """find --issue 42 must match #42 but not #420 or #421."""
    issues = [
        (1, "Amazing Spider-Man #42"),
        (2, "Amazing Spider-Man #420"),
        (3, "Amazing Spider-Man #421"),
        (4, "Amazing Spider-Man #43"),
    ]
    resp = MagicMock()
    resp.text = _make_find_response(issues, total_count=len(issues))
    mock_client.get.return_value = resp

    result = cmd_find(mock_client, series_id=100043, issue="42")
    ids = [r["id"] for r in result]
    assert ids == [1], f"Expected [1] but got {ids} (#42 should not match #420 or #421)"
    assert result[0]["title"].endswith("#42")


def test_cmd_find_filters_by_variant(mock_client):
    """find --variant filters titles by case-insensitive substring."""
    issues = [
        (1, "Amazing Spider-Man #229"),
        (2, "Amazing Spider-Man #229 Newsstand Edition"),
        (3, "Amazing Spider-Man #229 Direct Edition"),
    ]
    resp = MagicMock()
    resp.text = _make_find_response(issues, total_count=len(issues))
    mock_client.get.return_value = resp

    result = cmd_find(mock_client, series_id=100043, issue="229", variant="newsstand")
    assert len(result) == 1
    assert result[0]["id"] == 2
    assert "Newsstand" in result[0]["title"]


def test_cmd_find_exact_excludes_variants(mock_client):
    """find --exact returns only titles ending in #<N> (no variant suffix)."""
    issues = [
        (1, "Amazing Spider-Man #229"),
        (2, "Amazing Spider-Man #229 Newsstand Edition"),
        (3, "Amazing Spider-Man #229 Variant"),
    ]
    resp = MagicMock()
    resp.text = _make_find_response(issues, total_count=len(issues))
    mock_client.get.return_value = resp

    result = cmd_find(mock_client, series_id=100043, issue="229", exact=True)
    assert len(result) == 1
    assert result[0]["id"] == 1


def test_cmd_find_paginates_when_count_equals_page_size(mock_client):
    """find should page through a series like the user-list pagination logic.

    Looking up issue #145 in a 200-issue series should still find it even when
    the API caps responses at 140 per page.  Pagination continues as long as
    each page yields new items, and stops when an empty page is returned.
    """
    page1 = [(i, f"Series #{i}") for i in range(1, 141)]   # 140 items
    page2 = [(i, f"Series #{i}") for i in range(141, 201)]  # 60 items, includes #145

    resp1 = MagicMock()
    resp1.text = _make_find_response(page1, total_count=_PAGE_SIZE)  # API lies: 140
    resp2 = MagicMock()
    resp2.text = _make_find_response(page2, total_count=60)
    resp3 = MagicMock()
    resp3.text = _make_find_response([], total_count=0)  # terminator
    mock_client.get.side_effect = [resp1, resp2, resp3]

    result = cmd_find(mock_client, series_id=42, issue="145")
    assert len(result) == 1
    assert result[0]["id"] == 145
    # Second request should have list_mode_offset=140
    second_call_params = mock_client.get.call_args_list[1][1]["params"]
    assert second_call_params["list_mode_offset"] == "140"


def test_cmd_find_does_not_filter_format(mock_client):
    """find must NOT pass format[]=1 — annuals/giant-size should be findable."""
    issues = [(1, "Amazing Spider-Man Annual #1")]
    resp = MagicMock()
    resp.text = _make_find_response(issues, total_count=1)
    mock_client.get.return_value = resp

    cmd_find(mock_client, series_id=100043, issue="1")
    params = mock_client.get.call_args[1]["params"]
    assert "format[]" not in params, (
        "cmd_find must not filter by format — annuals and special-format "
        "issues must be findable."
    )


def test_cmd_find_returns_empty_when_no_match(mock_client):
    issues = [(1, "Amazing Spider-Man #1"), (2, "Amazing Spider-Man #2")]
    resp = MagicMock()
    resp.text = _make_find_response(issues, total_count=2)
    mock_client.get.return_value = resp

    result = cmd_find(mock_client, series_id=100043, issue="999")
    assert result == []


# --- POST retry on JSON parse error ---


def test_cmd_add_retries_on_json_decode_error(mock_client, monkeypatch):
    """cmd_add must retry once when the move POST returns non-JSON, then succeed."""
    # Patch the sleep so the test stays fast.
    import locg.commands as commands_mod
    monkeypatch.setattr(commands_mod, "_RETRY_SLEEP_SECONDS", 0)

    # First response: HTML body — .json() raises JSONDecodeError.
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.side_effect = json.JSONDecodeError(
        "Expecting value", "line 1 column 1 (char 0)", 0,
    )
    # Second response: valid JSON success.
    good_resp = MagicMock()
    good_resp.status_code = 200
    good_resp.json.return_value = {"status": "ok"}

    mock_client.post.side_effect = [bad_resp, good_resp]

    result = cmd_add(mock_client, "collection", 12345)

    assert mock_client.post.call_count == 2, (
        "cmd_add should retry once on JSONDecodeError, then succeed on the "
        "second attempt."
    )
    # Both calls should have the same payload (same endpoint + data).
    first_call = mock_client.post.call_args_list[0]
    second_call = mock_client.post.call_args_list[1]
    assert first_call[0][0] == "/comic/my_list_move"
    assert second_call[0][0] == "/comic/my_list_move"
    assert first_call[1]["data"] == second_call[1]["data"]

    assert result == {"status": "ok"}


def test_cmd_add_returns_error_after_two_json_failures(mock_client, monkeypatch):
    """If both attempts fail to return JSON, cmd_add must return a clean error."""
    import locg.commands as commands_mod
    monkeypatch.setattr(commands_mod, "_RETRY_SLEEP_SECONDS", 0)

    bad_resp = MagicMock()
    bad_resp.status_code = 503
    bad_resp.json.side_effect = json.JSONDecodeError(
        "Expecting value", "line 1 column 1 (char 0)", 0,
    )
    mock_client.post.side_effect = [bad_resp, bad_resp]

    result = cmd_add(mock_client, "collection", 12345)

    assert mock_client.post.call_count == 2
    assert "error" in result
    # Should not crash with a JSONDecodeError — that was the original bug.


def test_cmd_remove_retries_on_json_decode_error(mock_client, monkeypatch):
    """cmd_remove must retry once on JSONDecodeError, then succeed."""
    import locg.commands as commands_mod
    monkeypatch.setattr(commands_mod, "_RETRY_SLEEP_SECONDS", 0)

    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.side_effect = json.JSONDecodeError(
        "Expecting value", "line 1 column 1 (char 0)", 0,
    )
    good_resp = MagicMock()
    good_resp.status_code = 200
    good_resp.json.return_value = {"status": "ok"}

    mock_client.post.side_effect = [bad_resp, good_resp]

    result = cmd_remove(mock_client, "collection", 12345)
    assert mock_client.post.call_count == 2
    assert result == {"status": "ok"}

# --- lookup ---------------------------------------------------------------


def _make_series_li(
    series_id: int,
    name: str,
    publisher: str = "Marvel Comics",
    start_year: int = 1980,
    end_year: int = 2011,
    issue_count: int = 100,
) -> str:
    """Build an <li> matching what extract_series() expects."""
    years = f"{start_year} - {end_year}" if end_year else str(start_year)
    return (
        f'<li>'
        f'<a class="link-collection-series" data-id="{series_id}" '
        f'href="/comics/series/{series_id}/{name.lower().replace(" ", "-")}">link</a>'
        f'<div class="title"><a href="/comics/series/{series_id}/{name.lower().replace(" ", "-")}">{name}</a></div>'
        f'<span class="count-issues">{issue_count}</span>'
        f'<div class="copy-really-small">'
        f'<span>{publisher}</span><span>· {years}</span>'
        f'</div>'
        f'</li>'
    )


def _make_search_response(series_html_items: list[str], count: int) -> str:
    return json.dumps({"count": count, "list": "".join(series_html_items)})


def _make_issue_li(comic_id: int, name: str) -> str:
    """Issue HTML matching the search-by-series response format (no list membership)."""
    return (
        f'<li data-comic="{comic_id}">'
        f'<div class="title"><a href="/comic/{comic_id}/x">{name}</a></div>'
        f'</li>'
    )


def _make_issue_li_with_lists(
    comic_id: int,
    name: str,
    active_lists: list[int] | None = None,
) -> str:
    """Issue HTML for lookup search responses that includes comic-controller spans.

    *active_lists* is a list of LOCG list IDs (1=pull, 2=collection, 3=wish, 5=read)
    that should be marked as active.  When non-empty the authenticated response
    shape is produced so extract_issue returns a non-None lists dict.
    """
    if active_lists is None:
        active_lists = []
    all_lists = [1, 2, 3, 5]
    controllers = ""
    for lid in all_lists:
        active = " active" if lid in active_lists else ""
        controllers += (
            f'<span class="comic-controller{active}" '
            f'data-comic="{comic_id}" data-list="{lid}"></span>'
        )
    return (
        f'<li data-comic="{comic_id}">'
        f'{controllers}'
        f'<div class="title"><a href="/comic/{comic_id}/x">{name}</a></div>'
        f'</li>'
    )


def _make_comic_detail_page(
    comic_id: int,
    name: str,
    active_lists: list[int] | None = None,
) -> str:
    """Minimal HTML page matching what extract_comic_lists() expects.

    Used to mock the per-comic GET (/comic/<id>/x) for cache-hit collection checks.
    """
    if active_lists is None:
        active_lists = []
    all_lists = [1, 2, 3, 5]
    controllers = ""
    for lid in all_lists:
        active = " active" if lid in active_lists else ""
        controllers += (
            f'<span class="comic-controller{active}" '
            f'data-comic="{comic_id}" data-list="{lid}"></span>'
        )
    return (
        f'<html><head>'
        f'<link rel="canonical" href="https://leagueofcomicgeeks.com/comic/{comic_id}/x"/>'
        f'</head><body>'
        f'<h1>{name}</h1>'
        f'{controllers}'
        f'</body></html>'
    )


# --- parse_lookup_spec ----


def test_parse_lookup_spec_simple():
    assert parse_lookup_spec("Batman:224") == ("Batman", "224", None)


def test_parse_lookup_spec_with_variant():
    assert parse_lookup_spec("Uncanny X-Men:179:Newsstand") == (
        "Uncanny X-Men",
        "179",
        "Newsstand",
    )


def test_parse_lookup_spec_series_with_internal_colon():
    """A colon inside the series name must not be mistaken for the issue delimiter."""
    assert parse_lookup_spec("Batman: The Long Halloween:9") == (
        "Batman: The Long Halloween",
        "9",
        None,
    )


def test_parse_lookup_spec_series_with_colon_and_variant():
    assert parse_lookup_spec(
        "Batman: Legends of the Dark Knight Halloween Special:1:Newsstand"
    ) == ("Batman: Legends of the Dark Knight Halloween Special", "1", "Newsstand")


def test_parse_lookup_spec_decimal_issue():
    assert parse_lookup_spec("Saga:1.5") == ("Saga", "1.5", None)


def test_parse_lookup_spec_rejects_no_colon():
    with pytest.raises(ValueError):
        parse_lookup_spec("Batman")


def test_parse_lookup_spec_rejects_empty_parts():
    with pytest.raises(ValueError):
        parse_lookup_spec(":224")
    with pytest.raises(ValueError):
        parse_lookup_spec("Batman:")


# --- normalize / picker ----


def test_normalize_strips_the_prefix_and_collapses_whitespace():
    assert _normalize_series_name("The Amazing  Spider-Man") == "amazing spider-man"
    assert _normalize_series_name("Uncanny X-Men") == "uncanny x-men"


def test_pick_best_series_prefers_exact_name_then_publisher_then_year():
    series_list = [
        # Reprint, recent
        {"id": 1, "name": "Batman", "publisher": "Other", "start_year": 2020, "issue_count": 5},
        # Canonical 1940 DC run
        {"id": 2, "name": "Batman", "publisher": "DC Comics", "start_year": 1940, "issue_count": 700},
        # Loose match (different name)
        {"id": 3, "name": "Batman: Year One", "publisher": "DC Comics", "start_year": 1987, "issue_count": 4},
    ]
    best = _pick_best_series(series_list, "Batman")
    assert best["id"] == 2


def test_pick_best_series_handles_the_prefix():
    series_list = [
        {"id": 1, "name": "The Uncanny X-Men", "publisher": "Marvel Comics", "start_year": 1980, "issue_count": 1000},
        {"id": 2, "name": "Uncanny X-Men", "publisher": "Panini Comics", "start_year": 2013, "issue_count": 5},
    ]
    best = _pick_best_series(series_list, "Uncanny X-Men")
    # Both match exactly after normalization; Marvel/oldest wins.
    assert best["id"] == 1


def test_pick_best_series_returns_none_when_no_match():
    series_list = [
        {"id": 1, "name": "Spawn", "publisher": "Image Comics", "start_year": 1992, "issue_count": 350},
    ]
    assert _pick_best_series(series_list, "Daredevil") is None


def test_pick_best_series_skips_entries_without_id():
    series_list = [
        {"id": 0, "name": "Batman", "publisher": "DC Comics", "start_year": 1940, "issue_count": 700},
        {"id": 5, "name": "Batman", "publisher": "DC Comics", "start_year": 2011, "issue_count": 50},
    ]
    best = _pick_best_series(series_list, "Batman")
    assert best["id"] == 5


# --- cmd_lookup integration ----


def _setup_lookup_mock(mock_client, series_response, issue_responses, detail_pages=None):
    """Wire mock_client.get to return a sequence of responses based on URL/params.

    *detail_pages* maps comic_id (int) to HTML page text, used for per-comic
    GET requests (/comic/<id>/x) issued for cache-hit collection checks.
    """
    if detail_pages is None:
        detail_pages = {}

    def side_effect(url, params=None, **kwargs):
        params = params or {}
        resp = MagicMock()
        resp.status_code = 200
        # Series search (list_option=series)
        if params.get("list_option") == "series":
            resp.text = series_response
            return resp
        # Title-filtered issue search (has both series_id and title)
        if params.get("series_id") and params.get("title"):
            key = (params.get("series_id"), params.get("title"))
            resp.text = issue_responses.get(key, json.dumps({"count": 0, "list": ""}))
            return resp
        # Per-comic detail page (/comic/<id>/x) for cache-hit collection checks
        import re as _re
        m = _re.match(r"^/comic/(\d+)/x$", url)
        if m:
            comic_id = int(m.group(1))
            resp.text = detail_pages.get(
                comic_id, _make_comic_detail_page(comic_id, "Unknown", [])
            )
            return resp
        resp.text = json.dumps({"count": 0, "list": ""})
        return resp

    mock_client.get.side_effect = side_effect


def test_cmd_lookup_resolves_single_request_no_collection(mock_client):
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1,
            "list": _make_issue_li(1081721, "Uncanny X-Men #185"),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "185", None)], check_collection=False)

    assert len(result) == 1
    row = result[0]
    assert row["series_id"] == 108806
    assert row["locg_id"] == 1081721
    assert row["locg_variant_id"] is None
    assert row["issue_name"] == "Uncanny X-Men #185"
    assert "in_collection" not in row
    assert "error" not in row


def test_cmd_lookup_groups_by_series(mock_client):
    """Two issues from one series should produce a single search call."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1, "list": _make_issue_li(1081721, "Uncanny X-Men #185"),
        }),
        ("108806", "Uncanny X-Men #188"): json.dumps({
            "count": 1, "list": _make_issue_li(5907584, "Uncanny X-Men #188"),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None), ("Uncanny X-Men", "188", None)],
        check_collection=False,
    )

    assert [r["locg_id"] for r in result] == [1081721, 5907584]
    # 1 series search + 2 issue searches = 3 calls
    series_calls = [
        c for c in mock_client.get.call_args_list
        if (c.kwargs.get("params") or {}).get("list_option") == "series"
    ]
    assert len(series_calls) == 1


def test_cmd_lookup_marks_in_collection(mock_client):
    """Fresh lookup: in_collection comes from the issue search response (no collection fetch)."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Issue search response includes list membership (collection=active)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1,
            "list": _make_issue_li_with_lists(1081721, "Uncanny X-Men #185", [2]),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "185", None)], check_collection=True)

    assert result[0]["in_collection"] is True
    assert result[0]["locg_id"] == 1081721
    # No /comic/get_comics?list=collection call was made — membership came from
    # the issue search response directly.
    collection_calls = [
        c for c in mock_client.get.call_args_list
        if (c.kwargs.get("params") or {}).get("list") == "collection"
    ]
    assert len(collection_calls) == 0


def test_cmd_lookup_in_collection_false_when_not_in_search_response(mock_client):
    """Fresh lookup: in_collection is False when search response shows comic not in collection."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Issue search response: comic is NOT in the collection (no active list)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1,
            "list": _make_issue_li_with_lists(1081721, "Uncanny X-Men #185", []),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "185", None)], check_collection=True)
    assert result[0]["in_collection"] is False


def test_cmd_lookup_in_collection_false_when_id_absent(mock_client):
    """Unauthenticated response (no comic-controller spans): in_collection defaults to False."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Plain issue HTML without list membership spans (unauthenticated / legacy)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1, "list": _make_issue_li(1081721, "Uncanny X-Men #185"),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "185", None)], check_collection=True)
    assert result[0]["in_collection"] is False


def test_cmd_lookup_series_not_found(mock_client):
    series_response = _make_search_response([], 0)
    _setup_lookup_mock(mock_client, series_response, {})

    result = cmd_lookup(
        mock_client, [("Made Up Series", "1", None)], check_collection=False
    )
    assert result[0]["error"].startswith("Series ")
    assert result[0]["locg_id"] is None


def test_cmd_lookup_issue_not_found_in_series(mock_client):
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Empty issue response
    issue_responses = {("108806", "Uncanny X-Men #999"): json.dumps({"count": 0, "list": ""})}
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "999", None)], check_collection=False)
    assert "Issue #999 not found" in result[0]["error"]
    assert result[0]["series_id"] == 108806


def test_cmd_lookup_distinguishes_canonical_from_variant(mock_client):
    """When a variant is requested, canonical and variant entries get separate IDs."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Title-filter returns BOTH canonical and newsstand variant
    list_html = (
        _make_issue_li(7480697, "Uncanny X-Men #179") +
        _make_issue_li(8888888, "Uncanny X-Men #179 Newsstand Edition")
    )
    issue_responses = {
        ("108806", "Uncanny X-Men #179"): json.dumps({"count": 2, "list": list_html}),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(
        mock_client, [("Uncanny X-Men", "179", "Newsstand")], check_collection=False
    )
    assert result[0]["locg_id"] == 7480697
    assert result[0]["locg_variant_id"] == 8888888


def test_cmd_lookup_issue_match_is_exact_not_prefix(mock_client):
    """'#15' should NOT match '#150' or '#155'."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    list_html = (
        _make_issue_li(99001, "Uncanny X-Men #150") +
        _make_issue_li(99002, "Uncanny X-Men #155")
    )
    issue_responses = {("108806", "Uncanny X-Men #15"): json.dumps({"count": 2, "list": list_html})}
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "15", None)], check_collection=False)
    # No exact #15 entry → not found
    assert result[0]["locg_id"] is None
    assert "not found" in result[0].get("error", "")


# --- lookup + cache integration ----


def test_cmd_lookup_writes_to_cache_on_resolve(tmp_path, mock_client):
    """A successful resolution should populate the cache."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1, "list": _make_issue_li(1081721, "Uncanny X-Men #185"),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None)],
        check_collection=False,
        cache=cache,
    )

    entry = cache.get(make_key("Uncanny X-Men", "185"))
    assert entry is not None
    assert entry["locg_id"] == 1081721
    assert entry["series_id"] == 108806


def test_cmd_lookup_cache_hit_skips_api_calls(tmp_path, mock_client):
    """If every request is in the cache, neither search nor issue queries fire."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    cache.set(make_key("Uncanny X-Men", "185"), {
        "series_id": 108806,
        "locg_id": 1081721,
        "locg_variant_id": None,
        "series_name": "Uncanny X-Men",
        "issue_name": "Uncanny X-Men #185",
    })

    result = cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None)],
        check_collection=False,
        cache=cache,
    )

    assert result[0]["locg_id"] == 1081721
    assert result[0]["from_cache"] is True
    # No HTTP calls were made — cache served everything
    mock_client.get.assert_not_called()


def test_cmd_lookup_partial_cache_hit_only_resolves_misses(tmp_path, mock_client):
    """Cached items skip the API; only misses trigger searches."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    # Pre-populate one of two requests
    cache.set(make_key("Uncanny X-Men", "185"), {
        "series_id": 108806, "locg_id": 1081721, "locg_variant_id": None,
        "series_name": "Uncanny X-Men", "issue_name": "Uncanny X-Men #185",
    })

    # Mock for the OTHER (uncached) request
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    issue_responses = {
        ("108806", "Uncanny X-Men #188"): json.dumps({
            "count": 1, "list": _make_issue_li(5907584, "Uncanny X-Men #188"),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None), ("Uncanny X-Men", "188", None)],
        check_collection=False,
        cache=cache,
    )

    # Cached row first, fresh row second
    assert result[0]["locg_id"] == 1081721
    assert result[0]["from_cache"] is True
    assert result[1]["locg_id"] == 5907584
    assert result[1]["from_cache"] is False
    # Only one series search (for the cache miss), not two
    series_calls = [
        c for c in mock_client.get.call_args_list
        if (c.kwargs.get("params") or {}).get("list_option") == "series"
    ]
    assert len(series_calls) == 1


def test_cmd_lookup_no_cache_skips_reads_and_writes(tmp_path, mock_client):
    """use_cache=False bypasses cache regardless of pre-populated entries."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    cache.set(make_key("Uncanny X-Men", "185"), {
        "series_id": 999999,  # Wrong on purpose to verify cache is bypassed
        "locg_id": 999999,
        "locg_variant_id": None,
        "series_name": "Uncanny X-Men",
        "issue_name": "Uncanny X-Men #185",
    })

    # API returns the correct ID
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1, "list": _make_issue_li(1081721, "Uncanny X-Men #185"),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None)],
        check_collection=False,
        use_cache=False,
    )

    # Got the API result, not the cached (stale) value
    assert result[0]["locg_id"] == 1081721
    assert result[0]["from_cache"] is False


def test_cmd_lookup_cache_hit_still_checks_collection(tmp_path, mock_client):
    """Cache hits re-check collection via per-comic GET, not a full collection fetch."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    cache.set(make_key("Uncanny X-Men", "185"), {
        "series_id": 108806, "locg_id": 1081721, "locg_variant_id": None,
        "series_name": "Uncanny X-Men", "issue_name": "Uncanny X-Men #185",
    })

    # Per-comic detail page for ID 1081721 — comic IS in collection.
    detail_pages = {
        1081721: _make_comic_detail_page(1081721, "Uncanny X-Men #185", [2]),
    }
    _setup_lookup_mock(mock_client, "{}", {}, detail_pages=detail_pages)

    result = cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None)],
        check_collection=True,
        cache=cache,
    )

    assert result[0]["from_cache"] is True
    assert result[0]["in_collection"] is True
    # Exactly one GET was made — the per-comic detail page (no collection list fetch).
    assert mock_client.get.call_count == 1
    called_url = mock_client.get.call_args_list[0][0][0]
    assert called_url == "/comic/1081721/x"


def test_cmd_lookup_does_not_cache_failed_resolutions(tmp_path, mock_client):
    """Errors (series-not-found, issue-not-found) must not be cached."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    series_response = _make_search_response([], 0)
    _setup_lookup_mock(mock_client, series_response, {})

    cmd_lookup(
        mock_client,
        [("Made Up Series", "1", None)],
        check_collection=False,
        cache=cache,
    )
    assert cache.get(make_key("Made Up Series", "1")) is None


def test_cmd_lookup_no_collection_suppresses_in_collection(mock_client):
    """check_collection=False must not produce in_collection on any row."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Even with membership data in the response, in_collection must be absent.
    issue_responses = {
        ("108806", "Uncanny X-Men #185"): json.dumps({
            "count": 1,
            "list": _make_issue_li_with_lists(1081721, "Uncanny X-Men #185", [2]),
        }),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(mock_client, [("Uncanny X-Men", "185", None)], check_collection=False)

    assert "in_collection" not in result[0]
    # No per-comic detail page fetched either.
    detail_calls = [
        c for c in mock_client.get.call_args_list
        if c[0] and "/comic/" in c[0][0] and "/x" in c[0][0]
    ]
    assert len(detail_calls) == 0


def test_cmd_lookup_variant_reads_membership_from_variant_item(mock_client):
    """When a variant is matched, in_collection reflects the variant item's membership."""
    series_html = _make_series_li(108806, "Uncanny X-Men", "Marvel Comics", 1980, 2011, 1247)
    series_response = _make_search_response([series_html], 1)
    # Canonical item NOT in collection; variant IS in collection.
    list_html = (
        _make_issue_li_with_lists(7480697, "Uncanny X-Men #179", [])  # canonical, not collected
        + _make_issue_li_with_lists(8888888, "Uncanny X-Men #179 Newsstand Edition", [2])  # variant, collected
    )
    issue_responses = {
        ("108806", "Uncanny X-Men #179"): json.dumps({"count": 2, "list": list_html}),
    }
    _setup_lookup_mock(mock_client, series_response, issue_responses)

    result = cmd_lookup(
        mock_client, [("Uncanny X-Men", "179", "Newsstand")], check_collection=True
    )

    assert result[0]["locg_id"] == 7480697
    assert result[0]["locg_variant_id"] == 8888888
    # Membership should come from the VARIANT item (in collection), not the canonical.
    assert result[0]["in_collection"] is True


def test_cmd_lookup_cache_hit_not_in_collection(tmp_path, mock_client):
    """Cache hit where the per-comic check shows the comic is NOT in collection."""
    from locg.cache import IDCache, make_key

    cache = IDCache(path=tmp_path / "ids.json")
    cache.set(make_key("Uncanny X-Men", "185"), {
        "series_id": 108806, "locg_id": 1081721, "locg_variant_id": None,
        "series_name": "Uncanny X-Men", "issue_name": "Uncanny X-Men #185",
    })

    # Per-comic detail page: comic is NOT in collection.
    detail_pages = {
        1081721: _make_comic_detail_page(1081721, "Uncanny X-Men #185", []),
    }
    _setup_lookup_mock(mock_client, "{}", {}, detail_pages=detail_pages)

    result = cmd_lookup(
        mock_client,
        [("Uncanny X-Men", "185", None)],
        check_collection=True,
        cache=cache,
    )

    assert result[0]["from_cache"] is True
    assert result[0]["in_collection"] is False


# ---------------------------------------------------------------------------
# cmd_wish_list_from_cache
# ---------------------------------------------------------------------------

def _make_wish_list_cache(tmp_path, items=None):
    """Write a wish-list.json fixture to the tmp_path-isolated cache location."""
    from datetime import datetime, timezone
    from locg.commands import wish_list_cache_path
    if items is None:
        items = [
            {"name": "Amazing Spider-Man #300", "id": None, "series_name": "Amazing Spider-Man"},
            {"name": "X-Men #1", "id": None, "series_name": "X-Men"},
            {"name": "Batman #500", "id": None, "series_name": "Batman"},
        ]
    import json
    cache_path = wish_list_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }))
    return items


def test_cmd_wish_list_from_cache_returns_all_items(tmp_path):
    """cmd_wish_list_from_cache returns the full items list from cache."""
    from locg.commands import cmd_wish_list_from_cache
    items = _make_wish_list_cache(tmp_path)
    result = cmd_wish_list_from_cache()
    assert len(result) == len(items)
    assert result[0]["name"] == "Amazing Spider-Man #300"
    assert result[0]["id"] is None


def test_cmd_wish_list_from_cache_title_filter_case_insensitive(tmp_path):
    """Title filter is case-insensitive substring match on name."""
    from locg.commands import cmd_wish_list_from_cache
    _make_wish_list_cache(tmp_path)
    result = cmd_wish_list_from_cache(title="spider-man")
    assert len(result) == 1
    assert result[0]["name"] == "Amazing Spider-Man #300"


def test_cmd_wish_list_from_cache_empty_cache(tmp_path):
    """An empty items list returns [] without error."""
    from locg.commands import cmd_wish_list_from_cache
    _make_wish_list_cache(tmp_path, items=[])
    result = cmd_wish_list_from_cache()
    assert result == []


def test_cmd_wish_list_from_cache_missing_file(tmp_path):
    """Raises FileNotFoundError when cache does not exist."""
    from locg.commands import cmd_wish_list_from_cache
    import pytest
    with pytest.raises(FileNotFoundError):
        cmd_wish_list_from_cache()


# ---------------------------------------------------------------------------
# cmd_wish_list_add
# ---------------------------------------------------------------------------

def test_cmd_wish_list_add_creates_cache_when_missing(tmp_path):
    """Adding to an absent cache creates the file with one entry."""
    import json as _json
    from locg.commands import cmd_wish_list_add, wish_list_cache_path

    assert not wish_list_cache_path().exists()

    result = cmd_wish_list_add("Amazing Spider-Man #300")

    assert result["status"] == "ok"
    assert result["added"] == {"name": "Amazing Spider-Man #300", "id": None, "source": "local"}
    assert result["items"] == 1

    payload = _json.loads(wish_list_cache_path().read_text())
    assert payload["items"] == [{"name": "Amazing Spider-Man #300", "id": None, "source": "local"}]
    assert "updated_at" in payload


def test_cmd_wish_list_add_appends_to_existing_cache(tmp_path):
    """Adding to an existing cache preserves prior entries."""
    import json as _json
    from locg.commands import cmd_wish_list_add, wish_list_cache_path

    seeded = _make_wish_list_cache(tmp_path)
    cmd_wish_list_add("Batman #224")

    payload = _json.loads(wish_list_cache_path().read_text())
    names = [item["name"] for item in payload["items"]]
    assert names == [item["name"] for item in seeded] + ["Batman #224"]
    assert payload["items"][-1] == {"name": "Batman #224", "id": None, "source": "local"}


def test_cmd_wish_list_add_then_read_returns_new_entry(tmp_path):
    """A subsequent wish-list read surfaces the manually-added entry."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    cmd_wish_list_add("Uncanny X-Men #185")
    items = cmd_wish_list_from_cache()
    assert any(it["name"] == "Uncanny X-Men #185" and it["id"] is None for it in items)


def test_cmd_wish_list_add_stamps_year(tmp_path):
    """BUI-387: a `year` argument is persisted as a separate `year` field on the
    new entry (the per-issue Cover Year), never encoded into `name`."""
    import json as _json
    from locg.commands import cmd_wish_list_add, wish_list_cache_path

    result = cmd_wish_list_add("The X-Men #1", year="1963")

    assert result["added"] == {
        "name": "The X-Men #1", "id": None, "source": "local", "year": "1963",
    }
    payload = _json.loads(wish_list_cache_path().read_text())
    assert payload["items"][-1]["year"] == "1963"


def test_cmd_wish_list_add_without_year_omits_field(tmp_path):
    """BUI-387: an add with no `year` keeps the exact pre-387 entry shape — no
    `year` key at all — so a never-stamped wish stays year-blind."""
    from locg.commands import cmd_wish_list_add

    result = cmd_wish_list_add("Batman #224")

    assert "year" not in result["added"]
    assert result["added"] == {"name": "Batman #224", "id": None, "source": "local"}


def test_cmd_wish_list_add_rejects_malformed_year(tmp_path):
    """BUI-387: the persisted add path validates the year with the SAME 4-digit
    guard as set-year — a range paste / garbage is rejected loudly (never stored
    to later mis-scope the audit), at the shared chokepoint the endpoint add also
    flows through."""
    from locg.commands import cmd_wish_list_add, wish_list_cache_path

    for bad in ["1963 - 2011", "63", "banana"]:
        result = cmd_wish_list_add("The X-Men #1", year=bad)
        assert "error" in result, bad

    # A rejected add writes nothing.
    assert not wish_list_cache_path().exists()


def test_cmd_wish_list_add_file_mode_is_600(tmp_path):
    """Atomic write leaves the cache file mode set to 600."""
    import stat as _stat
    from locg.commands import cmd_wish_list_add, wish_list_cache_path

    cmd_wish_list_add("Daredevil #181")
    mode = _stat.S_IMODE(wish_list_cache_path().stat().st_mode)
    assert mode == _stat.S_IRUSR | _stat.S_IWUSR


def test_cmd_wish_list_add_rejects_empty_title(tmp_path):
    """Empty or whitespace-only titles are refused without writing the cache."""
    from locg.commands import cmd_wish_list_add, wish_list_cache_path

    result = cmd_wish_list_add("   ")
    assert "error" in result
    assert not wish_list_cache_path().exists()


def test_cmd_wish_list_add_dedups_against_existing_entry(tmp_path):
    """BUI-313: the plain CLI add path (previously unguarded) now dedups
    through the same shared ``_find_duplicate_wish_entry`` as the endpoint
    (BUI-285) and the creator-run (BUI-303) — a duplicate is a no-op, not a
    second appended row."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    seeded = _make_wish_list_cache(tmp_path)  # includes "X-Men #1"
    result = cmd_wish_list_add("X-Men #1")

    assert result["status"] == "exists"
    assert result["existing"]["name"] == "X-Men #1"
    items = cmd_wish_list_from_cache()
    assert len(items) == len(seeded)  # no duplicate row appended


def test_cmd_wish_list_add_dedup_normalizes_issue_and_case(tmp_path):
    """BUI-313: dedup keys on the normalized issue token (leading zeros
    stripped) and is case-insensitive on the series, same as the endpoint's
    contract (BUI-285) — 'x-men #001' duplicates the seeded 'X-Men #1'."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    seeded = _make_wish_list_cache(tmp_path)
    result = cmd_wish_list_add("x-men #001")

    assert result["status"] == "exists"
    names = [it["name"] for it in cmd_wish_list_from_cache()]
    assert len(names) == len(seeded)
    assert "x-men #001" not in names


def test_cmd_wish_list_add_distinct_volume_still_appends(tmp_path):
    """BUI-313/BUI-284: a volume-decorated name is a distinct entry from a bare
    masthead of the same issue — dedup must not collapse it."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    _make_wish_list_cache(tmp_path)  # includes bare "X-Men #1"
    result = cmd_wish_list_add("X-Men (Vol. 2) #1")

    assert result["status"] == "ok"
    names = {it["name"] for it in cmd_wish_list_from_cache()}
    assert "X-Men (Vol. 2) #1" in names
    assert "X-Men #1" in names


def test_cmd_wish_list_add_second_printing_not_deduped_against_base(tmp_path):
    """BUI-403: wish-listing a 2nd printing when the base is already wished is
    NOT a duplicate — they're distinct printings, both genuinely wanted. Prior
    to BUI-403 this silently no-opped (the base's (series, issue) key matched),
    dropping the printing the user actually asked for — the BUI-122-class
    data-loss direction (a wanted book never gets added)."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    _make_wish_list_cache(tmp_path)  # includes bare "Amazing Spider-Man #300"
    result = cmd_wish_list_add("Amazing Spider-Man #300 2nd Printing")

    assert result["status"] == "ok"
    names = {it["name"] for it in cmd_wish_list_from_cache()}
    assert "Amazing Spider-Man #300 2nd Printing" in names
    assert "Amazing Spider-Man #300" in names


def test_cmd_wish_list_add_base_not_deduped_against_existing_second_printing(tmp_path):
    """BUI-403 reverse direction: wish-listing the base when a 2nd printing is
    already wished is also NOT a duplicate."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    _make_wish_list_cache(
        tmp_path,
        items=[{"name": "Amazing Spider-Man #300 2nd Printing", "id": None,
                "series_name": "Amazing Spider-Man"}],
    )
    result = cmd_wish_list_add("Amazing Spider-Man #300")

    assert result["status"] == "ok"
    names = {it["name"] for it in cmd_wish_list_from_cache()}
    assert "Amazing Spider-Man #300" in names
    assert "Amazing Spider-Man #300 2nd Printing" in names


def test_cmd_wish_list_add_same_printing_still_deduped(tmp_path):
    """BUI-403 guard against under-dedup: a genuine same-printing duplicate —
    including a differently-SPELLED marker for the same ordinal ("2nd Ptg" vs
    "2nd Printing") — must still be caught as a duplicate, not appended as a
    second row."""
    from locg.commands import cmd_wish_list_add, cmd_wish_list_from_cache

    seeded = _make_wish_list_cache(
        tmp_path,
        items=[{"name": "Amazing Spider-Man #300 2nd Printing", "id": None,
                "series_name": "Amazing Spider-Man"}],
    )
    result = cmd_wish_list_add("Amazing Spider-Man #300 2nd Ptg")

    assert result["status"] == "exists"
    assert result["existing"]["name"] == "Amazing Spider-Man #300 2nd Printing"
    items = cmd_wish_list_from_cache()
    assert len(items) == len(seeded)  # no duplicate row appended

    # And the exact same marker/spelling is caught too.
    result2 = cmd_wish_list_add("Amazing Spider-Man #300 2nd Printing")
    assert result2["status"] == "exists"
    assert len(cmd_wish_list_from_cache()) == len(seeded)


# ---------------------------------------------------------------------------
# cmd_wish_list_add_creator_run (BUI-134)
# ---------------------------------------------------------------------------

def _mark_collection_imported(tmp_path):
    """Stamp the isolated collection cache as imported (last_full_import set).

    The R11 guard in cmd_wish_list_add_creator_run refuses to write when the
    collection cache was never imported (last_full_import is None). Tests that
    exercise the *owned-filter* (not the guard itself) must mark the cache as
    initialized first, mirroring a real `locg collection import`.
    """
    from datetime import datetime, timezone
    from locg.collection_cache import CollectionCache
    cache = CollectionCache()
    cache.load()

    def mutate(payload):
        payload["last_full_import"] = datetime.now(timezone.utc).isoformat()

    cache.apply(mutate, command="seed")


def _seed_collection_owned(tmp_path, rows):
    """Seed the isolated collection cache with owned rows for ownership checks.

    Also stamps last_full_import so the R11 never-imported guard is satisfied.
    """
    from datetime import datetime, timezone
    from locg.collection_cache import CollectionCache
    cache = CollectionCache()
    cache.load()

    def mutate(payload):
        payload["comics"].extend(rows)
        payload["last_full_import"] = datetime.now(timezone.utc).isoformat()
        # Rebuild series_name_index so the matcher's exact key check passes.
        from locg.commands import _normalize_series_key
        for r in rows:
            key = _normalize_series_key(r["series_name"].split(" (")[0])
            payload.setdefault("series_name_index", {})[key] = r["series_name"].split(" (")[0]

    cache.apply(mutate, command="seed")


def _owned_row(series_name, full_title, release_date):
    return {
        "series_name": series_name,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": 1,
        "source": "locg_export",
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
    }


def _patch_metron_run(monkeypatch, *, creator, run):
    """Patch MetronClient so resolve_creator/resolve_creator_run return canned data."""
    import locg.metron as metron_mod
    from unittest.mock import MagicMock

    inst = MagicMock()
    inst.resolve_creator.return_value = creator
    inst.resolve_creator_run.return_value = run
    monkeypatch.setattr(metron_mod, "MetronClient", lambda: inst)
    return inst


def test_creator_run_adds_gap_issues_filtering_owned_and_wishlisted(tmp_path, monkeypatch):
    """Owned + already-wishlisted issues are filtered out before any write."""
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    # Own #176; already wish-listed #177.
    _seed_collection_owned(tmp_path, [
        _owned_row("Uncanny X-Men", "Uncanny X-Men #176", "1983-12-01"),
    ])
    _make_wish_list_cache(tmp_path, items=[
        {"name": "Uncanny X-Men #177", "id": None},
    ])

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [
                {"number": "175", "metron_id": 1, "cover_date": "1983-11-01"},
                {"number": "176", "metron_id": 2, "cover_date": "1983-12-01"},  # owned
                {"number": "177", "metron_id": 3, "cover_date": "1984-01-01"},  # wished
                {"number": "287", "metron_id": 4, "cover_date": "1992-04-01"},  # stint 2
            ],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, role="penciller",
    )

    assert result["status"] == "ok"
    assert result["added"] == ["Uncanny X-Men #175", "Uncanny X-Men #287"]
    assert result["already_owned"] == ["Uncanny X-Men #176"]
    assert result["already_wishlisted"] == ["Uncanny X-Men #177"]

    names = {it["name"] for it in cmd_wish_list_from_cache()}
    assert "Uncanny X-Men #175" in names
    assert "Uncanny X-Men #287" in names  # discontinuous second stint added
    assert "Uncanny X-Men #176" not in names  # owned never written


def test_creator_run_stamps_per_issue_cover_year(tmp_path, monkeypatch):
    """BUI-387: creator-run adds carry each issue's OWN Metron cover year (never
    year_began), so the run's wishes are year-scoped from the start. An issue
    Metron has no cover_date for is added UNSTAMPED (safe year-blind default)."""
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    _mark_collection_imported(tmp_path)  # R11 guard satisfied, nothing owned
    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [
                {"number": "175", "metron_id": 1, "cover_date": "1983-11-01"},
                {"number": "287", "metron_id": 4, "cover_date": None},  # no date
            ],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, role="penciller",
    )
    assert result["status"] == "ok"

    by_name = {it["name"]: it for it in cmd_wish_list_from_cache()}
    assert by_name["Uncanny X-Men #175"]["year"] == "1983"       # stamped cover year
    assert "year" not in by_name["Uncanny X-Men #287"]           # no cover_date → unstamped


def test_creator_run_dedup_catches_leading_zero_that_exact_string_match_would_miss(tmp_path, monkeypatch):
    """BUI-303: creator-run dedup goes through the same series+issue-token
    comparison as the /api/comics/wish-list endpoint (BUI-285, now also backed
    by the shared ``_find_duplicate_wish_entry`` per BUI-313), not the old
    exact-lowercased-string match.

    The cache already has "Uncanny X-Men #001" (zero-padded); Metron reports the
    same issue as plain "1". The old inline dedup compared
    ``"uncanny x-men #1" == "uncanny x-men #001"`` — false, so it would have
    wish-listed a duplicate. The shared token dedup normalizes both issue tokens
    via ``normalize_issue_key`` (leading zeros stripped) and correctly treats
    them as the same issue.
    """
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    _mark_collection_imported(tmp_path)
    _make_wish_list_cache(tmp_path, items=[
        {"name": "Uncanny X-Men #001", "id": None, "series_name": "Uncanny X-Men"},
    ])

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "1", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["added"] == []
    assert result["already_wishlisted"] == ["Uncanny X-Men #1"]
    names = {it["name"] for it in cmd_wish_list_from_cache()}
    assert "Uncanny X-Men #1" not in names  # no duplicate row written


def test_creator_run_dedup_does_not_collapse_distinct_volumes(tmp_path, monkeypatch):
    """The token dedup keys on the DECORATED series text, not the bare masthead
    (BUI-284 trap) — so it stays exactly as strict as the old exact-string match
    when the series text genuinely differs (a volume-decorated cache entry vs.
    a bare-masthead run title), and the issue is still added.
    """
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    _mark_collection_imported(tmp_path)
    _make_wish_list_cache(tmp_path, items=[
        {"name": "Uncanny X-Men (Vol. 2) #1", "id": None, "series_name": "Uncanny X-Men (Vol. 2)"},
    ])

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "1", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["already_wishlisted"] == []
    assert result["added"] == ["Uncanny X-Men #1"]
    names = {it["name"] for it in cmd_wish_list_from_cache()}
    assert "Uncanny X-Men #1" in names


def test_creator_run_dedup_skips_unparseable_cache_entries(tmp_path, monkeypatch):
    """A cache entry with no '#' issue token (e.g. a TPB/OGN title) can't be
    compared and must not blow up or falsely match — the dedup just skips it
    and still catches the real duplicate alongside it.
    """
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    _mark_collection_imported(tmp_path)
    _make_wish_list_cache(tmp_path, items=[
        {"name": "Uncanny X-Men Omnibus", "id": None, "series_name": "Uncanny X-Men"},
        {"name": "Uncanny X-Men #1", "id": None, "series_name": "Uncanny X-Men"},
    ])

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "1", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["added"] == []
    assert result["already_wishlisted"] == ["Uncanny X-Men #1"]
    names = [it["name"] for it in cmd_wish_list_from_cache()]
    assert names.count("Uncanny X-Men #1") == 1  # no duplicate row appended


def test_creator_run_dedups_two_run_issues_normalizing_to_same_token(tmp_path, monkeypatch):
    """BUI-313: two issues within the SAME creator run that normalize to the
    same series+issue token (Metron reporting both "1" and "001") must not
    both pass the dedup check and both get written — the second is caught as
    already-wishlisted even though neither was on the wish-list when the run
    started (the `existing` list loaded once up front is stale otherwise)."""
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    _mark_collection_imported(tmp_path)

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [
                {"number": "1", "metron_id": 1, "cover_date": "1983-11-01"},
                {"number": "001", "metron_id": 2, "cover_date": "1983-11-01"},
            ],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["added"] == ["Uncanny X-Men #1"]
    assert result["already_wishlisted"] == ["Uncanny X-Men #001"]
    names = [it["name"] for it in cmd_wish_list_from_cache()]
    assert names.count("Uncanny X-Men #1") == 1  # only one row written, not two


def test_creator_run_batches_into_a_single_read_and_write(tmp_path, monkeypatch):
    """BUI-325: the creator-run write path does ONE atomic read+write for the
    whole run, not one read/scan/write per issue — and the resulting
    added/already_owned/already_wishlisted accounting is identical to what
    replaying the same titles through the serial per-issue path
    (``cmd_wish_list_add``) produces, including the BUI-313 intra-run
    same-token dedup (two issues normalizing to the same "#300" token).
    """
    import locg.commands as cmds
    from locg.commands import (
        cmd_wish_list_add,
        cmd_wish_list_add_creator_run,
        cmd_wish_list_from_cache,
    )

    # Own #176; already wish-listed #177.
    _seed_collection_owned(tmp_path, [
        _owned_row("Uncanny X-Men", "Uncanny X-Men #176", "1983-12-01"),
    ])
    _make_wish_list_cache(tmp_path, items=[
        {"name": "Uncanny X-Men #177", "id": None},
    ])

    write_calls: list[list[dict]] = []
    original_write = cmds._write_wish_list_cache

    def spy_write(items):
        write_calls.append([dict(it) for it in items])
        return original_write(items)

    monkeypatch.setattr(cmds, "_write_wish_list_cache", spy_write)

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [
                {"number": "175", "metron_id": 1, "cover_date": "1983-11-01"},
                {"number": "176", "metron_id": 2, "cover_date": "1983-12-01"},  # owned
                {"number": "177", "metron_id": 3, "cover_date": "1984-01-01"},  # wished
                {"number": "287", "metron_id": 4, "cover_date": "1992-04-01"},  # stint 2
                {"number": "300", "metron_id": 5, "cover_date": "1993-05-01"},
                {"number": "300", "metron_id": 6, "cover_date": "1993-05-01"},  # same-token dup within run
            ],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, role="penciller",
    )

    assert result["status"] == "ok"
    assert result["added"] == [
        "Uncanny X-Men #175", "Uncanny X-Men #287", "Uncanny X-Men #300",
    ]
    assert result["already_owned"] == ["Uncanny X-Men #176"]
    assert result["already_wishlisted"] == ["Uncanny X-Men #177", "Uncanny X-Men #300"]

    # Exactly one atomic write for the whole run, not one per added issue.
    assert len(write_calls) == 1

    names = [it["name"] for it in cmd_wish_list_from_cache()]
    assert names.count("Uncanny X-Men #175") == 1
    assert names.count("Uncanny X-Men #287") == 1
    assert names.count("Uncanny X-Men #300") == 1  # dup within run not double-written
    assert "Uncanny X-Men #176" not in names  # owned never written

    # Equivalence check: replay the same to-add titles through the serial
    # single-issue path against a fresh cache and confirm it reaches the
    # identical added/skipped outcome the batched path did.
    _make_wish_list_cache(tmp_path, items=[{"name": "Uncanny X-Men #177", "id": None}])
    serial_added: list[str] = []
    serial_skipped: list[str] = []
    for title in [
        "Uncanny X-Men #175", "Uncanny X-Men #287",
        "Uncanny X-Men #300", "Uncanny X-Men #300",
    ]:
        r = cmd_wish_list_add(title)
        if r.get("status") == "exists":
            serial_skipped.append(title)
        else:
            serial_added.append(title)

    assert serial_added == ["Uncanny X-Men #175", "Uncanny X-Men #287", "Uncanny X-Men #300"]
    assert serial_skipped == ["Uncanny X-Men #300"]


def test_creator_run_skips_read_and_write_when_nothing_to_add(tmp_path, monkeypatch):
    """BUI-325: when every run issue is filtered out (owned or already
    wishlisted) before the write stage, the batch path must not touch the
    cache file at all -- no fresh read, no write. This is the complementary
    edge of the single-read/single-write optimization: a bug that always
    reads+writes regardless of `to_add` would slip past a test that only
    checks the non-empty case.
    """
    import locg.commands as cmds
    from locg.commands import cmd_wish_list_add_creator_run, cmd_wish_list_from_cache

    # Own #176; already wish-listed #177 -- the run's only two issues.
    _seed_collection_owned(tmp_path, [
        _owned_row("Uncanny X-Men", "Uncanny X-Men #176", "1983-12-01"),
    ])
    seeded = _make_wish_list_cache(tmp_path, items=[
        {"name": "Uncanny X-Men #177", "id": None},
    ])

    write_calls: list[list[dict]] = []
    monkeypatch.setattr(
        cmds, "_write_wish_list_cache",
        lambda items: write_calls.append(list(items)),
    )

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [
                {"number": "176", "metron_id": 1, "cover_date": "1983-12-01"},  # owned
                {"number": "177", "metron_id": 2, "cover_date": "1984-01-01"},  # wished
            ],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, role="penciller",
    )

    assert result["status"] == "ok"
    assert result["added"] == []
    assert result["already_owned"] == ["Uncanny X-Men #176"]
    assert result["already_wishlisted"] == ["Uncanny X-Men #177"]

    # Nothing to add -> the write helper is never invoked.
    assert write_calls == []
    # The cache on disk is untouched (still exactly the pre-run seed).
    assert cmd_wish_list_from_cache() == seeded


def test_creator_run_surfaces_warnings(tmp_path, monkeypatch):
    """No-credit warnings from the resolver are passed through to the caller."""
    from locg.commands import cmd_wish_list_add_creator_run

    _mark_collection_imported(tmp_path)
    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "175", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [{"number": "200", "metron_id": 9, "reason": "no credits in Metron"}],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )
    assert result["added"] == ["Uncanny X-Men #175"]
    assert result["warnings"][0]["number"] == "200"


def test_creator_run_unresolvable_creator_errors(tmp_path, monkeypatch):
    """An ambiguous / unknown creator is a hard error, not a silent guess."""
    from locg.commands import cmd_wish_list_add_creator_run

    _mark_collection_imported(tmp_path)
    _patch_metron_run(monkeypatch, creator=None, run=None)
    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="Romita", series_id=99,
    )
    assert "error" in result
    assert "resolve creator" in result["error"]


def test_creator_run_requires_creator(tmp_path):
    from locg.commands import cmd_wish_list_add_creator_run
    result = cmd_wish_list_add_creator_run(series="X", creator="  ", series_id=1)
    assert "error" in result


def test_creator_run_refuses_when_collection_never_imported(tmp_path, monkeypatch):
    """R11 / BUI-122 footgun: an uninitialized collection cache must REFUSE the
    write, not wish-list every issue as a false 'not owned'."""
    from locg.commands import cmd_wish_list_add_creator_run, wish_list_cache_path

    # Cache never imported (last_full_import is None) — the default empty state.
    inst = _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "175", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert "error" in result
    assert "never imported" in result["error"]
    # Refused BEFORE any Metron call or write — nothing wish-listed.
    inst.resolve_creator.assert_not_called()
    inst.resolve_creator_run.assert_not_called()
    assert not wish_list_cache_path().exists()


def test_creator_run_empty_role_falls_back_to_penciller(tmp_path, monkeypatch):
    """An empty --role string falls back to penciller (no silent empty run)."""
    from locg.commands import cmd_wish_list_add_creator_run

    _mark_collection_imported(tmp_path)
    inst = _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={"issues": [], "warnings": []},
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, role="",
    )
    assert result["role"] == "penciller"
    # The resolver was called with the defaulted role, not the empty string.
    _, kwargs = inst.resolve_creator_run.call_args
    assert kwargs["role"] == "penciller"


# ---------------------------------------------------------------------------
# cmd_creator_run_lookup (BUI-340) — read-only counterpart to
# cmd_wish_list_add_creator_run: resolves via the same Metron methods but
# never touches the wish-list or collection cache.
# ---------------------------------------------------------------------------

def test_creator_run_lookup_resolves_via_existing_metron_methods(tmp_path, monkeypatch):
    """Calls the same resolve_creator/resolve_creator_run Metron methods the
    write path uses, and reports the resolved run (including a discontinuous
    second stint) without filtering owned/wishlisted issues."""
    from locg.commands import cmd_creator_run_lookup

    inst = _patch_metron_run(
        monkeypatch,
        creator={"id": 42, "name": "Erik Larsen"},
        run={
            "issues": [
                {"number": "18", "metron_id": 1, "cover_date": "1990-11-01"},
                {"number": "19", "metron_id": 2, "cover_date": "1990-12-01"},
                {"number": "23", "metron_id": 3, "cover_date": "1991-04-01"},
            ],
            "warnings": [],
        },
    )

    result = cmd_creator_run_lookup(
        series="The Amazing Spider-Man", creator="Erik Larsen", series_id=7, role="penciller",
    )

    assert result["status"] == "ok"
    assert result["creator"] == "Erik Larsen"
    assert result["creator_id"] == 42
    assert result["run_issue_count"] == 3
    assert result["issue_numbers"] == ["18", "19", "23"]
    inst.resolve_creator.assert_called_once_with("Erik Larsen")
    inst.resolve_creator_run.assert_called_once_with(
        series_id=7, creator_id=42, creator_name="Erik Larsen", role="penciller",
    )


def test_creator_run_lookup_writes_nothing(tmp_path, monkeypatch):
    """Zero cache/file writes: no collection-imported guard, no wish-list
    read/write — unlike cmd_wish_list_add_creator_run, this path never even
    checks whether the collection cache was imported."""
    from locg.commands import cmd_creator_run_lookup, wish_list_cache_path

    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "175", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    # Collection cache deliberately left un-imported (last_full_import is None,
    # the R11 state that would hard-refuse the write path) — the lookup must
    # succeed anyway, since it never consults the collection cache at all.
    result = cmd_creator_run_lookup(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["status"] == "ok"
    assert result["issue_numbers"] == ["175"]

    # No wish-list cache file was created, and no other file appeared under
    # the isolated LOCG_DATA_DIR (tmp_path) as a side effect of the lookup.
    assert not wish_list_cache_path().exists()
    assert list(tmp_path.rglob("*")) == []


def test_creator_run_lookup_unresolvable_creator_errors(tmp_path, monkeypatch):
    """An ambiguous/unknown creator is a hard error, not a silent guess —
    same contract as the write path."""
    from locg.commands import cmd_creator_run_lookup

    _patch_metron_run(monkeypatch, creator=None, run=None)
    result = cmd_creator_run_lookup(series="Uncanny X-Men", creator="Romita", series_id=99)
    assert "error" in result
    assert "resolve creator" in result["error"]


def test_creator_run_lookup_requires_series_and_creator(tmp_path):
    from locg.commands import cmd_creator_run_lookup

    assert "error" in cmd_creator_run_lookup(series="  ", creator="Erik Larsen", series_id=1)
    assert "error" in cmd_creator_run_lookup(series="X", creator="  ", series_id=1)


def test_creator_run_lookup_empty_role_falls_back_to_penciller(tmp_path, monkeypatch):
    from locg.commands import cmd_creator_run_lookup

    inst = _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={"issues": [], "warnings": []},
    )

    result = cmd_creator_run_lookup(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, role="",
    )
    assert result["role"] == "penciller"
    _, kwargs = inst.resolve_creator_run.call_args
    assert kwargs["role"] == "penciller"


# ---------------------------------------------------------------------------
# cmd_wish_list_remove
# ---------------------------------------------------------------------------

def test_cmd_wish_list_remove_removes_matching_entry(tmp_path):
    """Remove returns ok and the entry is gone from the cache."""
    import json as _json
    from locg.commands import cmd_wish_list_remove, wish_list_cache_path

    _make_wish_list_cache(tmp_path)
    result = cmd_wish_list_remove("X-Men #1")

    assert result["status"] == "ok"
    assert result["removed"]["name"] == "X-Men #1"
    assert result["items"] == 2

    payload = _json.loads(wish_list_cache_path().read_text())
    names = [it["name"] for it in payload["items"]]
    assert "X-Men #1" not in names
    assert "Amazing Spider-Man #300" in names
    assert "Batman #500" in names


def test_cmd_wish_list_remove_removes_first_match_only(tmp_path):
    """When a title appears twice, only the first occurrence is removed."""
    import json as _json
    from locg.commands import cmd_wish_list_remove, wish_list_cache_path

    _make_wish_list_cache(tmp_path, items=[
        {"name": "Batman #1", "id": None},
        {"name": "Batman #1", "id": 99},
    ])
    result = cmd_wish_list_remove("Batman #1")

    assert result["status"] == "ok"
    assert result["items"] == 1

    payload = _json.loads(wish_list_cache_path().read_text())
    assert payload["items"] == [{"name": "Batman #1", "id": 99}]


def test_cmd_wish_list_remove_not_found_returns_error(tmp_path):
    """Removing a title that doesn't exist returns an error dict."""
    from locg.commands import cmd_wish_list_remove

    _make_wish_list_cache(tmp_path)
    result = cmd_wish_list_remove("Daredevil #181")

    assert "error" in result
    assert "not found" in result["error"]


def test_cmd_wish_list_remove_missing_cache_returns_error(tmp_path):
    """Removing when no cache exists returns an error dict (no crash)."""
    from locg.commands import cmd_wish_list_remove

    result = cmd_wish_list_remove("Amazing Spider-Man #300")

    assert "error" in result
    assert "not found" in result["error"].lower()


def test_cmd_wish_list_remove_rejects_empty_title(tmp_path):
    """Empty or whitespace-only titles are refused."""
    from locg.commands import cmd_wish_list_remove

    result = cmd_wish_list_remove("   ")
    assert "error" in result


def test_cmd_wish_list_remove_file_mode_is_600(tmp_path):
    """Atomic remove write leaves the cache file mode set to 600."""
    import stat as _stat
    from locg.commands import cmd_wish_list_remove, wish_list_cache_path

    _make_wish_list_cache(tmp_path)
    cmd_wish_list_remove("X-Men #1")
    mode = _stat.S_IMODE(wish_list_cache_path().stat().st_mode)
    assert mode == _stat.S_IRUSR | _stat.S_IWUSR
