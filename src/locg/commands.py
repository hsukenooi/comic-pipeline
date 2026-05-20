"""Command implementations for locg CLI."""
from __future__ import annotations

import getpass
import json
import logging
import math
import re
import time
from datetime import date, timedelta
from typing import Any, Optional

from bs4 import BeautifulSoup

from locg.cache import IDCache, make_key
from locg.client import AuthRequired, LOCGClient
from locg.models import extract_comic_detail, extract_comic_lists, extract_issue, extract_my_details, extract_series
from locg.parser import parse_list_response, parse_page

logger = logging.getLogger("locg")

# The LOCG API returns at most this many items per request.
_PAGE_SIZE = 140

# List ID mapping for add/remove operations
LIST_IDS = {
    "pull": 1,
    "collection": 2,
    "wish": 3,
    "read": 5,
}

VALID_LISTS = list(LIST_IDS.keys())

# LOCG CGC scale values accepted by POST /comic/post_my_details.
# "0" is an explicit "None" (no grade assigned); others match CGC's
# official grade points.  Stored as strings because the server stores
# and returns them as strings.
VALID_GRADES = frozenset({
    "0", "0.1", "0.3", "0.5", "1.0", "1.5", "1.8", "2.0", "2.5",
    "3.0", "3.5", "4.0", "4.5", "5.0", "5.5", "6.0", "6.5",
    "7.0", "7.5", "8.0", "8.5", "9.0", "9.2", "9.4", "9.6",
    "9.8", "9.9", "10.0",
})


def _validate_grade(value: str) -> str:
    """Return *value* if it is on the LOCG CGC scale, else raise ValueError."""
    if value not in VALID_GRADES:
        valid = ", ".join(sorted(VALID_GRADES, key=lambda s: float(s)))
        raise ValueError(
            f"Invalid grade {value!r}. Valid grades: {valid}"
        )
    return value


def _validate_price(value: str) -> str:
    """Coerce *value* via float(); return the canonical string form.

    LOCG stores price_paid as a free-text string but truncates to two
    decimal places in the UI.  We reformat with ``f"{float(v):g}"`` which
    keeps integers tidy (``"390"`` not ``"390.0"``) and decimals readable.
    """
    try:
        f = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid price {value!r}: must be numeric")
    if not math.isfinite(f):
        raise ValueError(f"Invalid price {value!r}: must be a finite number")
    if f < 0:
        raise ValueError(f"Invalid price {value!r}: must be non-negative")
    return f"{f:g}"


# Sleep duration before retrying a POST that returned non-JSON. Small
# enough not to hurt batch throughput, large enough to clear a transient
# rate limit. Module-level so tests can monkeypatch it to 0.
_RETRY_SLEEP_SECONDS = 2.0


def _post_json_with_retry(
    client: LOCGClient,
    path: str,
    data: dict[str, Any],
) -> tuple[Any, Any]:
    """POST ``data`` to ``path`` and return ``(response, parsed_json)``.

    The LOCG API occasionally returns HTML (a Cloudflare interstitial or a
    rate-limit page) on otherwise-successful POSTs.  Hitting that with a
    raw ``response.json()`` raises ``json.JSONDecodeError`` and aborts the
    whole batch.  Retry once after a short sleep, which empirically clears
    the transient case.

    On the second failure we return ``(response, None)`` so callers can
    fall back to their existing error path (e.g. ``{"error": ...}``)
    instead of letting the exception bubble.
    """
    resp = client.post(path, data=data)
    try:
        return resp, resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            "POST %s returned non-JSON (%s); retrying in %ss",
            path, type(e).__name__, _RETRY_SLEEP_SECONDS,
        )
        time.sleep(_RETRY_SLEEP_SECONDS)
        resp = client.post(path, data=data)
        try:
            return resp, resp.json()
        except (json.JSONDecodeError, ValueError) as e2:
            logger.warning(
                "POST %s still returned non-JSON after retry (%s)",
                path, type(e2).__name__,
            )
            return resp, None


def _get_week_date(target: Optional[str] = None) -> str:
    """Return the date formatted as M/D/YYYY for LOCG API.

    If target is given (YYYY-MM-DD), use that date.
    Otherwise, find the most recent Wednesday (LOCG release day).
    """
    if target:
        parts = target.split("-")
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{m}/{d}/{y}"

    today = date.today()
    # Find the most recent Wednesday (weekday 2)
    days_since_wed = (today.weekday() - 2) % 7
    wed = today - timedelta(days=days_since_wed)
    return f"{wed.month}/{wed.day}/{wed.year}"


def cmd_search(client: LOCGClient, query: str) -> list[dict[str, Any]]:
    """Search for comic series by title."""
    resp = client.get("/comic/get_comics", params={
        "list": "search",
        "list_option": "series",
        "view": "thumbs",
        "title": query,
        "order": "alpha-asc",
    })
    count, soup = parse_list_response(resp.text)
    items = soup.find_all("li")
    return [extract_series(li) for li in items]


def cmd_releases(client: LOCGClient, target_date: Optional[str] = None) -> list[dict[str, Any]]:
    """Get new releases for a given week."""
    week_date = _get_week_date(target_date)
    resp = client.get("/comic/get_comics", params={
        "list": "releases",
        "view": "thumbs",
        "date_type": "week",
        "date": week_date,
        "order": "pulls",
    })
    count, soup = parse_list_response(resp.text)
    items = soup.find_all("li", class_="issue")
    return [extract_issue(li) for li in items]


def cmd_comic(client: LOCGClient, comic_id: int) -> dict[str, Any]:
    """Get full details for a specific comic."""
    resp = client.get(f"/comic/{comic_id}/x")
    if resp.status_code == 404:
        return {"error": f"Comic {comic_id} not found"}
    soup = parse_page(resp.text)
    return extract_comic_detail(soup)


def cmd_series(client: LOCGClient, series_id: int) -> dict[str, Any]:
    """Get series info and issue list."""
    resp = client.get("/comic/get_comics", params={
        "list": "search",
        "view": "thumbs",
        "format[]": "1",
        "series_id": str(series_id),
        "order": "date-desc",
    })
    count, soup = parse_list_response(resp.text)
    items = soup.find_all("li", class_="issue")
    issues = [extract_issue(li) for li in items]

    # If no issue-class items, try generic li (series search format)
    if not issues:
        items = soup.find_all("li")
        issues = [extract_issue(li) for li in items]

    return {
        "series_id": series_id,
        "issue_count": count,
        "issues": issues,
    }


def cmd_find(
    client: LOCGClient,
    series_id: int,
    issue: str,
    variant: Optional[str] = None,
    exact: bool = False,
) -> list[dict[str, Any]]:
    """Find issues in a series matching ``#<issue>``.

    Paginates through the series internally (no ``format[]=1`` filter, so
    annuals/giant-size/etc. are findable) and returns issues whose title
    contains ``#<issue>`` as a word boundary.  Optional refinements:

    * ``variant``: case-insensitive substring filter on the title
      (e.g. ``"newsstand"``, ``"homage"``).
    * ``exact``: only keep titles that look like ``<series> #<N>`` with no
      variant suffix.  Implemented by requiring the title to end in
      ``#<N>`` once whitespace is collapsed.
    """
    # Build a word-boundary matcher for "#<issue>" so #42 doesn't match #420.
    issue_pattern = re.compile(rf"#\s*{re.escape(str(issue))}(?!\d)")
    variant_needle = variant.lower() if variant else None

    matches: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    offset = 0
    page_index = 0

    while True:
        params: dict[str, Any] = {
            "list": "search",
            "view": "thumbs",
            "series_id": str(series_id),
            "order": "date-desc",
        }
        if offset > 0:
            params["list_mode_offset"] = str(offset)
        resp = client.get("/comic/get_comics", params=params)
        count, soup = parse_list_response(resp.text)
        items = soup.find_all("li", class_="issue")
        if not items:
            # Try generic <li> for series-search HTML variant.
            items = soup.find_all("li")
        page_issues = [extract_issue(li) for li in items]
        logger.debug(
            "find: series=%d page=%d offset=%d got=%d count=%d",
            series_id, page_index, offset, len(page_issues), count,
        )

        if not page_issues:
            break

        new_count = 0
        for entry in page_issues:
            cid = entry.get("id", 0)
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            new_count += 1

            name = entry.get("name", "") or ""
            if not issue_pattern.search(name):
                continue
            if variant_needle and variant_needle not in name.lower():
                continue
            if exact:
                # Collapse whitespace and require the title to end with
                # "#<issue>" exactly — anything after means a variant tag.
                normalized = " ".join(name.split())
                if not re.search(
                    rf"#\s*{re.escape(str(issue))}\s*$",
                    normalized,
                ):
                    continue

            result: dict[str, Any] = {
                "id": cid,
                "title": name,
            }
            store_date = entry.get("store_date")
            if store_date:
                result["store_date"] = store_date
            matches.append(result)

        # Pagination: stop when the page returned no new items.  The
        # server's `count` field is unreliable for series listings (it
        # reflects only the items in the current page, not the series
        # total), so we keep paging as long as each page yields fresh
        # items.  This is the same approach as ``_get_user_list`` for
        # the ``count == _PAGE_SIZE`` lying-server case, generalised:
        # any page with new items might be followed by another page.
        if new_count == 0:
            break
        offset += len(page_issues)
        page_index += 1

    return matches


def _check_session_valid(soup: BeautifulSoup) -> None:
    """Raise AuthRequired if the API response indicates an anonymous session.

    LOCG returns 200 even for expired sessions, but the HTML contains
    data-user="0" when the user is not actually logged in.
    """
    tag = soup.find(attrs={"data-user": "0"})
    if tag is not None:
        raise AuthRequired(
            "Session expired. Run: locg login"
        )


def _filter_by_list_membership(
    issues: list[dict[str, Any]],
    list_name: str,
) -> list[dict[str, Any]]:
    """Filter issues to only those belonging to the requested list.

    Works around an upstream LOCG API bug where the ``list`` query parameter
    is silently ignored — ``GET /comic/get_comics?list=collection`` and
    ``?list=wish`` return identical results containing ALL user comics.

    Each issue's ``lists`` field (populated by :func:`models.extract_issue`)
    contains a dict like ``{"pull": False, "collection": True, ...}``.
    We keep only items where ``lists[list_name]`` is ``True``.

    When ``lists`` is ``None`` (e.g. unauthenticated markup, though
    ``_get_user_list`` already calls ``require_auth``), the item is kept
    to avoid silently dropping data we cannot verify.

    If the upstream API is ever fixed, every returned item will already
    have the correct membership flag set, making this filter a no-op.
    """
    filtered: list[dict[str, Any]] = []
    skipped = 0
    for issue in issues:
        membership = issue.get("lists")
        if membership is None:
            # Cannot determine membership — keep the item.
            filtered.append(issue)
            continue
        if membership.get(list_name, False):
            filtered.append(issue)
        else:
            skipped += 1
    if skipped:
        logger.debug(
            "List membership filter %r: kept %d, removed %d of %d issues",
            list_name, len(filtered), skipped, len(issues),
        )
    return filtered


def _filter_by_title(issues: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    """Filter issues by case-insensitive substring match on the name field.

    This exists as a workaround for an upstream LOCG API bug: when both
    ``list`` and ``title`` params are sent to ``/comic/get_comics``, the
    ``list`` param is silently ignored and results span all lists.  We
    therefore fetch the full list first, then filter client-side.
    """
    needle = title.lower()
    filtered = [issue for issue in issues if needle in issue.get("name", "").lower()]
    logger.debug(
        "Title filter %r: %d of %d issues matched",
        title, len(filtered), len(issues),
    )
    return filtered


def _fetch_user_list_page(
    client: LOCGClient,
    list_name: str,
    order: str,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Fetch a single page of a user's list starting at *offset*.

    Returns ``(total_count, issues)`` where *total_count* is the server-
    reported total and *issues* are the items in this page.
    """
    params: dict[str, Any] = {
        "list": list_name,
        "view": "thumbs",
        "order": order,
    }
    if offset > 0:
        params["list_mode_offset"] = str(offset)
    resp = client.get("/comic/get_comics", params=params)
    count, soup = parse_list_response(resp.text)
    _check_session_valid(soup)
    items = soup.find_all("li", class_="issue")
    return count, [extract_issue(li) for li in items]


def _get_user_list(
    client: LOCGClient,
    list_name: str,
    order: str = "alpha-asc",
    title: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch a user's list (collection, pull, wish, read).

    Automatically paginates using ``list_mode_offset`` when the server
    reports more items than a single response can carry (140-item cap).

    If *title* is provided the full list is fetched and then filtered
    client-side (see :func:`_filter_by_title` for rationale).
    """
    client.require_auth()

    # First page (offset 0)
    total_count, issues = _fetch_user_list_page(client, list_name, order)
    logger.debug(
        "List %r page 0: got %d items, server total %d",
        list_name, len(issues), total_count,
    )

    # Track seen IDs for deduplication during pagination so we can
    # detect when a speculative fetch yields no new items.
    seen: set[int] = set()
    for issue in issues:
        seen.add(issue.get("id", 0))

    # Determine whether more pages may exist.  The normal signal is
    # offset < total_count, but the LOCG API sometimes lies: it
    # reports count == _PAGE_SIZE (140) on every page regardless of
    # the true total.  When we receive a full page AND the server
    # reports count == _PAGE_SIZE, speculatively fetch the next page.
    last_page_full = len(issues) == _PAGE_SIZE
    offset = len(issues)

    def _should_fetch_more() -> bool:
        # Normal case: server honestly reports a higher total.
        if offset < total_count and len(issues) < total_count:
            return True
        # Speculative case: server may be lying (count == _PAGE_SIZE
        # on every page).  Keep going while pages are full.
        if last_page_full and total_count == _PAGE_SIZE:
            return True
        return False

    while _should_fetch_more():
        page_count, page_issues = _fetch_user_list_page(
            client, list_name, order, offset=offset,
        )
        logger.debug(
            "List %r offset %d: got %d items",
            list_name, offset, len(page_issues),
        )
        if not page_issues:
            # Server returned no items — pagination not supported or
            # we've exhausted the list.  Stop to avoid infinite loop.
            logger.debug(
                "List %r: empty page at offset %d, stopping pagination "
                "(fetched %d of %d reported items)",
                list_name, offset, len(issues), total_count,
            )
            break

        # Count how many genuinely new items this page contributed.
        new_count = 0
        for issue in page_issues:
            cid = issue.get("id", 0)
            if cid not in seen:
                seen.add(cid)
                new_count += 1
        issues.extend(page_issues)
        offset += len(page_issues)
        last_page_full = len(page_issues) == _PAGE_SIZE

        if new_count == 0:
            # Every item on this page was a duplicate — we've looped
            # back to already-seen data, so stop.
            logger.debug(
                "List %r: page at offset %d had no new items, stopping",
                list_name, offset,
            )
            break

    # Deduplicate by comic ID while preserving order, in case the
    # server returns overlapping results across pages.
    seen_dedup: set[int] = set()
    unique: list[dict[str, Any]] = []
    for issue in issues:
        cid = issue.get("id", 0)
        if cid not in seen_dedup:
            seen_dedup.add(cid)
            unique.append(issue)
    if len(unique) < len(issues):
        logger.debug(
            "List %r: removed %d duplicate items",
            list_name, len(issues) - len(unique),
        )
    issues = unique

    # Filter by list membership to work around the upstream API bug where
    # the ``list`` parameter is silently ignored and all lists return
    # identical results.  This must run before the title filter.
    issues = _filter_by_list_membership(issues, list_name)

    if title:
        issues = _filter_by_title(issues, title)
    return issues


def cmd_collection(client: LOCGClient, title: Optional[str] = None) -> list[dict[str, Any]]:
    """Get the user's collection."""
    return _get_user_list(client, "collection", title=title)


def cmd_collection_has(client: LOCGClient, title_query: str) -> dict[str, Any]:
    """Check if a title is in the user's collection without fetching everything.

    Searches for matching comics via the search API, then checks list
    membership for each match individually.  Much faster than fetching
    the entire collection when you just need to know if one title is there.
    """
    client.require_auth()

    # Search for series matching the query
    resp = client.get("/comic/get_comics", params={
        "list": "search",
        "list_option": "series",
        "view": "thumbs",
        "title": title_query,
        "order": "alpha-asc",
    })
    count, soup = parse_list_response(resp.text)
    series_items = soup.find_all("li")
    series_list = [extract_series(s) for s in series_items]
    logger.debug("Search for %r found %d series", title_query, len(series_list))

    # For each series, fetch issues and find title matches
    needle = title_query.lower()
    matches: list[dict[str, Any]] = []

    for series in series_list:
        series_id = series.get("id")
        if not series_id:
            continue
        resp = client.get("/comic/get_comics", params={
            "list": "search",
            "view": "thumbs",
            "format[]": "1",
            "series_id": str(series_id),
            "order": "date-desc",
        })
        _, issue_soup = parse_list_response(resp.text)
        issue_items = issue_soup.find_all("li", class_="issue")
        for li in issue_items:
            title_div = li.find("div", class_="title")
            title_link = title_div.find("a") if title_div else None
            name = title_link.get_text(strip=True) if title_link else ""
            if needle in name.lower():
                comic_id_raw = li.get("data-comic")
                if comic_id_raw:
                    comic_id = int(comic_id_raw)
                    # Check list membership via detail page
                    logger.info("Checking collection membership for %r (id=%d)", name, comic_id)
                    detail_resp = client.get(f"/comic/{comic_id}/x")
                    if detail_resp.status_code == 404:
                        continue
                    detail_soup = parse_page(detail_resp.text)
                    entry = extract_comic_lists(detail_soup)
                    if "id" not in entry:
                        entry["id"] = comic_id
                    in_collection = bool(
                        entry.get("lists", {}).get("collection", False)
                    )
                    matches.append({
                        "id": comic_id,
                        "name": entry.get("name", name),
                        "in_collection": in_collection,
                        "lists": entry.get("lists"),
                    })

    return {
        "query": title_query,
        "matches": matches,
        "found_in_collection": any(m["in_collection"] for m in matches),
    }


def cmd_pull_list(client: LOCGClient, title: Optional[str] = None) -> list[dict[str, Any]]:
    """Get the user's pull list."""
    return _get_user_list(client, "pull", order="date-asc", title=title)


def cmd_wish_list(client: LOCGClient, title: Optional[str] = None) -> list[dict[str, Any]]:
    """Get the user's wish list."""
    return _get_user_list(client, "wish", title=title)


def cmd_read_list(client: LOCGClient, title: Optional[str] = None) -> list[dict[str, Any]]:
    """Get the user's read list."""
    return _get_user_list(client, "read", title=title)


def cmd_add(
    client: LOCGClient,
    list_name: str,
    comic_id: int,
    grade: Optional[str] = None,
    price: Optional[str] = None,
) -> dict[str, Any]:
    """Add a comic to a list, optionally recording grade and price."""
    client.require_auth()
    if list_name not in LIST_IDS:
        return {"error": f"Invalid list '{list_name}'. Valid lists: {', '.join(VALID_LISTS)}"}

    # grade/price only meaningful for collection
    if (grade is not None or price is not None) and list_name != "collection":
        return {"error": "--grade and --price are only valid when adding to collection"}

    # Step 1: add to list
    move_resp, parsed_move = _post_json_with_retry(
        client,
        "/comic/my_list_move",
        data={
            "comic_id": comic_id,
            "list_id": LIST_IDS[list_name],
            "action_id": 1,
        },
    )
    if parsed_move is None:
        # Two consecutive non-JSON responses — surface a clean error
        # rather than letting the JSONDecodeError abort the batch.
        return {
            "error": (
                f"LOCG API returned non-JSON for /comic/my_list_move "
                f"(HTTP {move_resp.status_code})"
            )
        }
    move_body = parsed_move

    # If move failed, return unchanged — no point attempting details.
    is_move_ok = (
        move_body.get("status") == "ok"
        or move_body.get("type") == "success"
    )
    if not is_move_ok:
        return move_body

    # Step 2: if no details supplied, done.
    if grade is None and price is None:
        return move_body

    # Step 3: POST details (minimum payload — comic is new, nothing to preserve).
    payload: dict[str, Any] = {"comic_id": comic_id}
    if grade is not None:
        payload["grading"] = grade
    if price is not None:
        payload["price_paid"] = price

    detail_resp, parsed_detail = _post_json_with_retry(
        client, "/comic/post_my_details", data=payload,
    )
    if parsed_detail is None:
        detail_body = {"type": "error", "text": f"HTTP {detail_resp.status_code}"}
    else:
        detail_body = parsed_detail

    if detail_resp.status_code == 200 and detail_body.get("type") == "success":
        return {
            "status": "ok",
            "added": True,
            "details_saved": True,
            "text": detail_body.get("text", "This comic has been updated."),
        }

    return {
        "status": "partial",
        "added": True,
        "details_saved": False,
        "details_error": detail_body.get("text", f"HTTP {detail_resp.status_code}"),
    }


def cmd_update(
    client: LOCGClient,
    comic_id: int,
    grade: Optional[str] = None,
    price: Optional[str] = None,
    condition: Optional[str] = None,
) -> dict[str, Any]:
    """Update grade / price / condition on a comic already in the user's collection.

    Because POST /comic/post_my_details wipes any field it does not receive,
    we must fetch the current server state first, merge the user's flags on
    top, then POST the full dict.
    """
    client.require_auth()

    if grade is None and price is None and condition is None:
        return {"error": "update: at least one of --grade, --price, --condition is required"}

    if grade is not None:
        try:
            grade = _validate_grade(grade)
        except ValueError as e:
            return {"error": str(e)}
    if price is not None:
        try:
            price = _validate_price(price)
        except ValueError as e:
            return {"error": str(e)}

    resp = client.get(f"/comic/{comic_id}/x")
    if resp.status_code == 404:
        return {"error": f"Comic {comic_id} not found"}
    if resp.status_code != 200:
        return {"error": f"Unexpected HTTP {resp.status_code} fetching comic {comic_id}"}

    soup = parse_page(resp.text)

    # Reject update on comics not in the user's collection.  The server
    # accepts a POST for any comic_id and returns success, which would
    # create orphan detail records.
    entry = extract_comic_lists(soup)
    lists = entry.get("lists") or {}
    if not lists.get("collection"):
        return {
            "error": (
                f"Comic {comic_id} is not in your collection. "
                f"Use: locg add collection {comic_id}"
            )
        }

    # Fetch current server state, merge flags on top.
    payload = extract_my_details(soup)
    if grade is not None:
        payload["grading"] = grade
    if price is not None:
        payload["price_paid"] = price
    if condition is not None:
        payload["condition"] = condition

    post_resp, parsed = _post_json_with_retry(
        client, "/comic/post_my_details", data=payload,
    )
    if parsed is None:
        return {"type": "error", "text": f"HTTP {post_resp.status_code}"}
    return parsed


def cmd_remove(client: LOCGClient, list_name: str, comic_id: int) -> dict[str, Any]:
    """Remove a comic from a list."""
    client.require_auth()
    if list_name not in LIST_IDS:
        return {"error": f"Invalid list '{list_name}'. Valid lists: {', '.join(VALID_LISTS)}"}
    resp, parsed = _post_json_with_retry(
        client,
        "/comic/my_list_move",
        data={
            "comic_id": comic_id,
            "list_id": LIST_IDS[list_name],
            "action_id": 0,
        },
    )
    if parsed is not None:
        return parsed
    return {"status": "ok" if resp.status_code == 200 else "error"}


def cmd_check_lists(client: LOCGClient, comic_ids: list[int]) -> list[dict[str, Any]]:
    """Check list membership for one or more comics.

    Fetches each comic's detail page and extracts only the ID, name, and
    list membership booleans.  This is lighter than :func:`cmd_comic` because
    it skips parsing creators, description, scores, etc.

    Requires authentication (list membership is user-specific).
    """
    client.require_auth()
    results: list[dict[str, Any]] = []
    for comic_id in comic_ids:
        logger.info("Checking lists for comic %d (%d/%d)", comic_id, len(results) + 1, len(comic_ids))
        resp = client.get(f"/comic/{comic_id}/x")
        if resp.status_code == 404:
            results.append({"id": comic_id, "name": None, "lists": None, "error": "not found"})
            continue
        soup = parse_page(resp.text)
        entry = extract_comic_lists(soup)
        # Ensure the requested ID is always present (fallback if canonical URL parsing fails)
        if "id" not in entry:
            entry["id"] = comic_id
        results.append(entry)
    return results


def cmd_login(client: LOCGClient, username: Optional[str] = None, password: Optional[str] = None) -> dict[str, Any]:
    """Log in to LOCG. Prompts for credentials if not provided."""
    if not username:
        username = input("Username: ")
    if not password:
        password = getpass.getpass("Password: ")
    success = client.login(username, password)
    if success:
        return {"status": "ok", "username": username}
    return {"error": "Login failed. Check your username and password."}


# --- lookup ---------------------------------------------------------------
#
# `lookup` resolves LOCG comic IDs in batch from "Series:Issue[:Variant]" specs.
# It groups requests by series, resolves the canonical series_id once per
# unique series, then uses a title-filtered query to pinpoint each issue.
# Optionally checks collection membership by fetching the collection once.

# Publishers ranked first when picking the canonical series for a given name.
_PREFERRED_PUBLISHERS: tuple[str, ...] = (
    "Marvel Comics",
    "DC Comics",
    "Dark Horse Comics",
    "Image Comics",
    "IDW Publishing",
    "BOOM! Studios",
    "Valiant",
    "Vertigo",
)

# Issue numbers are typically integers, optionally with a decimal or short
# alphabetic suffix (e.g. "1", "1.MU", "1AU"). Used to disambiguate
# "Series:Issue[:Variant]" specs where the series name may itself contain ":".
_ISSUE_NUMBER_RE = re.compile(r"^\d+(\.\w+)?[A-Za-z]*$")


def _normalize_series_name(name: str) -> str:
    """Lowercase, strip leading 'The ', collapse internal whitespace."""
    n = (name or "").lower().strip()
    if n.startswith("the "):
        n = n[4:]
    return re.sub(r"\s+", " ", n)


def _looks_like_issue_number(s: str) -> bool:
    return bool(_ISSUE_NUMBER_RE.match(s.strip()))


def parse_lookup_spec(spec: str) -> tuple[str, str, Optional[str]]:
    """Parse a 'Series:Issue[:Variant]' spec into ``(series, issue, variant)``.

    Series names may contain colons (e.g. "Batman: The Long Halloween:9").
    To disambiguate, we treat the trailing token as the variant only when
    the second-to-last token looks like an issue number; otherwise the
    trailing token IS the issue.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(
            f"Invalid spec {spec!r}: expected 'Series:Issue' or 'Series:Issue:Variant'"
        )

    last = parts[-1].strip()
    if len(parts) >= 3 and _looks_like_issue_number(parts[-2].strip()):
        # Series:Issue:Variant
        series = ":".join(parts[:-2]).strip()
        issue = parts[-2].strip()
        variant: Optional[str] = last
    else:
        # Series:Issue (series may contain internal colons)
        series = ":".join(parts[:-1]).strip()
        issue = last
        variant = None

    if not series or not issue:
        raise ValueError(f"Invalid spec {spec!r}: series and issue are required")
    return series, issue, variant


def _pick_best_series(
    series_list: list[dict[str, Any]],
    target: str,
) -> Optional[dict[str, Any]]:
    """Pick the canonical series from a search result.

    Heuristic:
      1. Filter to entries whose normalized name equals the target name
         (case-insensitive, ignoring leading 'The ').
      2. If none match exactly, fall back to entries that contain the target
         as a substring.
      3. Among candidates, sort by (preferred-publisher rank, oldest start
         year, highest issue count) and return the best.
    """
    target_norm = _normalize_series_name(target)

    exact = [s for s in series_list if _normalize_series_name(s.get("name", "")) == target_norm]
    candidates = exact or [
        s for s in series_list if target_norm and target_norm in _normalize_series_name(s.get("name", ""))
    ]
    candidates = [s for s in candidates if s.get("id")]
    if not candidates:
        return None

    def score(s: dict[str, Any]) -> tuple[int, int, int]:
        publisher = s.get("publisher") or ""
        try:
            pub_rank = _PREFERRED_PUBLISHERS.index(publisher)
        except ValueError:
            pub_rank = len(_PREFERRED_PUBLISHERS)
        start_year = s.get("start_year") or 9999
        issue_count = s.get("issue_count") or 0
        return (pub_rank, start_year, -issue_count)

    candidates.sort(key=score)
    return candidates[0]


def _find_issue_in_series(
    client: LOCGClient,
    series_id: int,
    series_name: str,
    issue_number: str,
    variant: Optional[str] = None,
) -> tuple[Optional[int], Optional[int], Optional[str], Optional[dict], Optional[dict]]:
    """Find canonical and variant comic IDs for an issue within a series.

    Uses a title-filtered query against the series so the result set stays
    small (a handful of items) regardless of series length, sidestepping
    the 140-issue page limit on plain ``series`` fetches.

    Returns ``(canonical_id, variant_id, canonical_name, canonical_lists,
    variant_lists)`` where ``canonical_lists`` and ``variant_lists`` are the
    ``lists`` membership dicts parsed from the search response (``None`` when
    unauthenticated or when the respective item was not found).
    """
    title_query = f"{series_name} #{issue_number}"
    resp = client.get(
        "/comic/get_comics",
        params={
            "list": "search",
            "view": "thumbs",
            "format[]": "1",
            "series_id": str(series_id),
            "title": title_query,
            "order": "date-desc",
        },
    )
    _, soup = parse_list_response(resp.text)
    items = soup.find_all("li")

    target_series_norm = _normalize_series_name(series_name)
    variant_norm = (variant or "").lower().strip()

    canonical_id: Optional[int] = None
    canonical_name: Optional[str] = None
    canonical_lists: Optional[dict] = None
    variant_id: Optional[int] = None
    variant_lists: Optional[dict] = None

    for li in items:
        comic_id_raw = li.get("data-comic")
        if not comic_id_raw:
            continue
        try:
            comic_id = int(comic_id_raw)
        except (TypeError, ValueError):
            continue

        title_div = li.find("div", class_="title")
        link = title_div.find("a") if title_div else None
        name = link.get_text(strip=True) if link else ""
        if not name:
            continue

        # Issue token must match exactly. Without word-boundary checks
        # "Spider-Man #15" would falsely match "#150".
        m = re.search(r"#(\S+)", name)
        if not m or m.group(1) != issue_number:
            continue

        # Series part must align with the requested series.
        if target_series_norm not in _normalize_series_name(name.split("#")[0]):
            continue

        issue_data = extract_issue(li)
        is_variant_entry = bool(re.search(r"#\S+\s+\S", name))  # has text after "#N "
        if variant_norm and is_variant_entry and variant_norm in name.lower():
            variant_id = comic_id
            variant_lists = issue_data.get("lists")
        elif not is_variant_entry and canonical_id is None:
            canonical_id = comic_id
            canonical_name = name
            canonical_lists = issue_data.get("lists")

    return canonical_id, variant_id, canonical_name, canonical_lists, variant_lists


def cmd_lookup(
    client: LOCGClient,
    requests: list[tuple[str, str, Optional[str]]],
    check_collection: bool = True,
    use_cache: bool = True,
    cache: Optional[IDCache] = None,
) -> list[dict[str, Any]]:
    """Resolve LOCG IDs for a batch of (series, issue[, variant]) requests.

    Groups requests by series so we hit the search endpoint at most once per
    unique series. Each issue is then resolved with one title-filtered query
    against that series_id (small payload, no pagination dance).

    If ``use_cache`` is true (default), the on-disk cache is consulted first;
    misses fall through to the API and are written back. ``cache`` is
    primarily for tests — production code uses the default :class:`IDCache`.

    If ``check_collection`` is true, populates ``in_collection`` on each
    result row.  For fresh lookups the membership data comes directly from
    the title-filtered issue search response (no extra request).  For cache
    hits a single per-comic GET (``/comic/<id>/x``) is issued so membership
    stays current even when IDs are served from cache.
    """
    if use_cache and cache is None:
        cache = IDCache()
    elif not use_cache:
        cache = None

    # Optimistically read all cache entries up front. Anything we can serve
    # from cache doesn't need a series search OR an issue search.
    cached_results: dict[int, dict[str, Any]] = {}  # request_index -> result row
    misses: list[tuple[int, str, str, Optional[str]]] = []
    for i, (series_name, issue_number, variant) in enumerate(requests):
        hit = None
        if cache is not None:
            hit = cache.get(make_key(series_name, issue_number, variant))
        if hit:
            row: dict[str, Any] = {
                "series_name": series_name,
                "issue_number": issue_number,
                "variant": variant,
                "series_id": hit.get("series_id"),
                "locg_id": hit.get("locg_id"),
                "locg_variant_id": hit.get("locg_variant_id"),
                "issue_name": hit.get("issue_name"),
                "from_cache": True,
            }
            cached_results[i] = row
        else:
            misses.append((i, series_name, issue_number, variant))

    # Resolve each unique series among the cache misses, exactly once.
    unique_series: dict[str, Optional[dict[str, Any]]] = {}
    for _, series_name, _, _ in misses:
        if series_name not in unique_series:
            results = cmd_search(client, series_name)
            unique_series[series_name] = _pick_best_series(results, series_name)

    # Build out the result list in original request order.
    out: list[Optional[dict[str, Any]]] = [None] * len(requests)

    # Slot in cache hits.
    for i, row in cached_results.items():
        out[i] = row

    # Resolve and slot in cache misses.
    for i, series_name, issue_number, variant in misses:
        result: dict[str, Any] = {
            "series_name": series_name,
            "issue_number": issue_number,
            "variant": variant,
            "series_id": None,
            "locg_id": None,
            "locg_variant_id": None,
            "issue_name": None,
            "from_cache": False,
        }

        series = unique_series.get(series_name)
        if not series:
            result["error"] = f"Series {series_name!r} not found"
            out[i] = result
            continue

        result["series_id"] = series.get("id")
        canonical_series_name = series.get("name") or series_name

        canonical_id, variant_id, issue_name, canonical_lists, variant_lists = (
            _find_issue_in_series(
                client,
                int(result["series_id"]),
                canonical_series_name,
                issue_number,
                variant,
            )
        )
        if canonical_id is None:
            result["error"] = (
                f"Issue #{issue_number} not found in series {canonical_series_name!r}"
            )
            out[i] = result
            continue

        result["locg_id"] = canonical_id
        result["locg_variant_id"] = variant_id
        result["issue_name"] = issue_name
        # Stash list membership for use in the in_collection pass below.
        # These keys are internal and removed before returning.
        result["_canonical_lists"] = canonical_lists
        result["_variant_lists"] = variant_lists
        out[i] = result

        # Write back to cache (best-effort — never fail a lookup over a
        # cache write error).
        if cache is not None:
            try:
                cache.set(
                    make_key(series_name, issue_number, variant),
                    {
                        "series_id": result["series_id"],
                        "locg_id": canonical_id,
                        "locg_variant_id": variant_id,
                        "series_name": canonical_series_name,
                        "issue_name": issue_name,
                    },
                )
            except OSError as e:
                logger.warning("Failed to write cache entry: %s", e)

    # Populate in_collection for every row.
    #
    # Fresh results: membership comes directly from the title-filtered issue
    # search response (no extra request needed — the data was already parsed
    # by extract_issue inside _find_issue_in_series).
    #
    # Cache hits: issue data is not re-fetched, so we do a lightweight
    # per-comic GET (/comic/<id>/x) to get current membership.  Cost is
    # 1 GET per cache hit, which is acceptable.
    for row in out:
        if row is None:  # defensive — every slot should be filled
            continue

        # Remove internal stash keys regardless of check_collection.
        canonical_lists = row.pop("_canonical_lists", None)
        variant_lists = row.pop("_variant_lists", None)

        if not check_collection or not row.get("locg_id"):
            if check_collection:
                row["in_collection"] = False
            continue

        check_id = (
            row.get("locg_variant_id")
            if (row.get("variant") and row.get("locg_variant_id"))
            else row.get("locg_id")
        )

        if row.get("from_cache"):
            # Cache hit: fetch current membership via a lightweight comic page.
            try:
                detail_resp = client.get(f"/comic/{check_id}/x")
                detail_soup = parse_page(detail_resp.text)
                entry = extract_comic_lists(detail_soup)
                lists = entry.get("lists") or {}
                row["in_collection"] = bool(lists.get("collection", False))
            except Exception:
                row["in_collection"] = False
        else:
            # Fresh result: membership already parsed from search response.
            lists_for_id = (
                variant_lists
                if (row.get("variant") and row.get("locg_variant_id"))
                else canonical_lists
            )
            if lists_for_id is not None:
                row["in_collection"] = bool(lists_for_id.get("collection", False))
            else:
                row["in_collection"] = False

    return [r for r in out if r is not None]


def cmd_cache_stats(_client: Optional[LOCGClient] = None) -> dict[str, Any]:
    """Return file path, entry count, size for the on-disk ID cache."""
    return IDCache().stats()


def cmd_cache_clear(_client: Optional[LOCGClient] = None) -> dict[str, Any]:
    """Delete every entry from the on-disk ID cache. Returns count removed."""
    removed = IDCache().clear()
    return {"cleared": removed}
