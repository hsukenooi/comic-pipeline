"""Command implementations for locg CLI."""
from __future__ import annotations

import getpass
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from bs4 import BeautifulSoup

from locg.cache import IDCache, make_key
from locg.client import AuthRequired, LOCGClient
from locg.collection_cache import CollectionCache, _normalize_series_key
from locg.config import wish_list_cache_path
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
        raise ValueError(f"Invalid price {value!r}: must be numeric") from None
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


def cmd_wish_list_from_cache(title: Optional[str] = None) -> list[dict[str, Any]]:
    """Serve the wish list from the local cache populated by collection import.

    Raises FileNotFoundError if the cache does not exist.
    """
    path = wish_list_cache_path()
    if not path.exists():
        raise FileNotFoundError(f"Wish-list cache not found: {path}. Run: locg collection import")
    with open(path) as f:
        data = json.load(f)
    items: list[dict[str, Any]] = data.get("items", [])
    if title:
        needle = title.lower()
        items = [it for it in items if needle in (it.get("name") or "").lower()]
    return items


def cmd_wish_list_add(title: str) -> dict[str, Any]:
    """Append a manual entry to the local wish-list cache.

    Writes ``{"name": title, "id": None}`` to ``data/locg/wish-list.json``
    using the same atomic write pattern as :func:`collection_io._write_wish_list_cache`
    (tempfile + os.replace + chmod 600).

    Manual adds carry no ``series_name``, which marks them as local-only.
    :func:`collection_io._write_wish_list_cache` preserves such entries across a
    ``locg collection import`` (BUI-47): it rebuilds the export-derived set, then
    re-appends any local-only entry whose name isn't already covered. A manual
    add is deduped out only once it round-trips through a LOCG export and reappears
    in the import. Until then it persists locally but is never pushed to LOCG —
    there is no wish-list push path (cf. the collection's record-win round-trip).
    """
    title = (title or "").strip()
    if not title:
        return {"error": "wish-list add: title must be non-empty"}

    path = wish_list_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        with open(path) as f:
            payload = json.load(f)
        items: list[dict[str, Any]] = payload.get("items") or []
    else:
        items = []

    entry = {"name": title, "id": None}
    items.append(entry)

    new_payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    fd, tmp = tempfile.mkstemp(
        prefix=".wish-list-", suffix=".json.tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(new_payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    return {
        "status": "ok",
        "added": entry,
        "items": len(items),
        "path": str(path),
    }


def cmd_wish_list_remove(title: str) -> dict[str, Any]:
    """Remove the first matching entry from the local wish-list cache.

    Matches on exact ``name`` field.  Uses the same atomic write pattern as
    :func:`cmd_wish_list_add` (tempfile + os.replace + chmod 600).
    """
    title = (title or "").strip()
    if not title:
        return {"error": "wish-list remove: title must be non-empty"}

    path = wish_list_cache_path()
    if not path.exists():
        return {"error": f"Wish-list cache not found: {path}. Run: locg collection import"}

    with open(path) as f:
        payload = json.load(f)
    items: list[dict[str, Any]] = payload.get("items") or []

    removed: Optional[dict[str, Any]] = None
    new_items: list[dict[str, Any]] = []
    for item in items:
        if removed is None and item.get("name") == title:
            removed = item
        else:
            new_items.append(item)

    if removed is None:
        return {"error": f"wish-list remove: '{title}' not found in cache"}

    new_payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": new_items,
    }

    fd, tmp = tempfile.mkstemp(
        prefix=".wish-list-", suffix=".json.tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(new_payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    return {
        "status": "ok",
        "removed": removed,
        "items": len(new_items),
        "path": str(path),
    }


# Wish-list entry names are written as "<Series> #<Issue>" by cmd_wish_list_add
# (and by the /comic:wishlist-add skill). Split on the first "#" so the leading
# text is the series and the token right after "#" is the issue. Trailing variant
# text (e.g. "#300 (Direct)") is ignored for the ownership check.
_WISH_NAME_RE = re.compile(r"^(?P<series>.*?)\s*#\s*(?P<issue>[0-9A-Za-z.\-]+)")


def _split_wish_list_name(name: str) -> Optional[tuple[str, str]]:
    """Split a wish-list entry name into ``(series, issue)``.

    Returns ``None`` when the name has no ``#<issue>`` token or no series text
    before it — those entries can't be ownership-checked and are reported as
    ``unparseable`` rather than silently dropped.
    """
    m = _WISH_NAME_RE.match(name or "")
    if m is None:
        return None
    series = m.group("series").strip()
    issue = m.group("issue").strip()
    if not series or not issue:
        return None
    return series, issue


def cmd_wish_list_conflicts() -> dict[str, Any]:
    """Audit the wish-list cache for items already in the collection (BUI-130).

    Cross-references every wish-list entry against the collection cache — the
    same per-item check ``/comic:wishlist-add`` runs at add time, applied
    retroactively to the full list. A wish-listed book you already own is the
    BUI-122 data-loss risk: ``/comic:collection-sync`` exports it with
    ``In Collection=0``, which tells LOCG to *remove* it from the collection.

    ``year`` is deliberately NOT passed to :func:`cmd_collection_check`: a
    wish-list name carries only series + issue, never a per-issue cover date,
    and forwarding a series start-year is exactly the BUI-129 bug (it filters
    out every owned row whose release year differs).

    Raises ``FileNotFoundError`` if the wish-list cache does not exist.
    """
    items = cmd_wish_list_from_cache()
    conflicts: list[dict[str, Any]] = []
    unparseable: list[str] = []
    checked = 0
    for it in items:
        name = it.get("name") or ""
        parsed = _split_wish_list_name(name)
        if parsed is None:
            unparseable.append(name)
            continue
        series, issue = parsed
        checked += 1
        result = cmd_collection_check(series=series, issue=issue)
        if result["match_status"] == "in_collection":
            conflicts.append({
                "name": name,
                "series": series,
                "issue": issue,
                "id": it.get("id"),
                "full_title_matched": result["full_title_matched"],
            })
    return {
        "total": len(items),
        "checked": checked,
        "unparseable": unparseable,
        "conflicts": conflicts,
    }


def cmd_wish_list_remove_conflicts() -> dict[str, Any]:
    """Remove every wish-list entry already in the collection (BUI-130).

    Re-derives the conflict set via :func:`cmd_wish_list_conflicts`, then removes
    each by exact name with :func:`cmd_wish_list_remove`. Returns the removed
    entries plus the remaining count, so the caller can report what was cleared
    without a second audit. The GET audit is the dry-run preview; this performs
    the removal.

    Raises ``FileNotFoundError`` if the wish-list cache does not exist.
    """
    audit = cmd_wish_list_conflicts()
    removed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for conflict in audit["conflicts"]:
        result = cmd_wish_list_remove(conflict["name"])
        if "error" in result:
            errors.append({"name": conflict["name"], "error": result["error"]})
        else:
            removed.append(result["removed"])
    try:
        remaining = len(cmd_wish_list_from_cache())
    except FileNotFoundError:
        remaining = 0
    return {
        "removed": removed,
        "removed_count": len(removed),
        "errors": errors,
        "remaining": remaining,
        "checked": audit["checked"],
        "unparseable": audit["unparseable"],
    }


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
            except Exception:  # noqa: BLE001  # best-effort collection membership fetch; failure → assume False
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


# ---------------------------------------------------------------------------
# Collection cache commands (Unit 4)
# ---------------------------------------------------------------------------

def cmd_collection_import(path_str: str) -> dict[str, Any]:
    """Import a LOCG Excel export into the local collection cache."""
    from pathlib import Path as _Path
    from locg.collection_io import import_xlsx

    path = _Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    cache = CollectionCache()
    # Pre-flight load triggers crash detection (migration_in_progress guard)
    # before we start parsing the xlsx. Raises RuntimeError on corrupt state.
    cache.load()
    return import_xlsx(path, cache)


def _cache_age_days(last_full_import: Optional[str]) -> Optional[int]:
    """Return days since last_full_import, or None if never imported."""
    if not last_full_import:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(last_full_import.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def _oldest_pending_days(rows: list[dict[str, Any]]) -> Optional[int]:
    """Return age in days of the oldest pending row, or None if no pending rows."""
    if not rows:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    oldest = None
    for row in rows:
        added = row.get("local_added_at") or ""
        if added:
            try:
                dt = datetime.fromisoformat(added.replace("Z", "+00:00"))
                if oldest is None or dt < oldest:
                    oldest = dt
            except ValueError:
                pass
    return (now - oldest).days if oldest is not None else None


def cmd_collection_export(out_path: Optional[str] = None) -> dict[str, Any]:
    """Export pending-push rows to a LOCG-compatible CSV + .notes.md companion.

    Returns {csv_path, notes_md_path, ready_count, manual_variant_count,
    manual_series_count, wish_list_count, oldest_pending_days}.
    """
    from datetime import datetime
    from pathlib import Path as _Path
    from locg.collection_io import _pending_push_rows, generate_csv, generate_notes_md, wish_rows_for_export

    cache = CollectionCache()
    payload = cache.load()
    ready, manual_variant, manual_series = _pending_push_rows(payload)
    # BUI-122: only push local-only wish adds that aren't already owned. Re-dumping
    # the whole wish list re-uploaded LOCG-derived wishes and, because wish rows
    # carry In Collection=0, deleted owned-but-wished books from the collection.
    wish_rows = wish_rows_for_export(payload)

    if out_path is None:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        dest = _Path.home() / "Downloads" / f"locg-bulk-import-{ts}.csv"
    else:
        dest = _Path(out_path)

    notes_dest = dest.with_suffix(".notes.md")

    generate_csv(ready, dest, wish_rows=wish_rows)
    generate_notes_md(ready, manual_variant, manual_series, notes_dest)

    all_pending = ready + manual_variant + manual_series
    return {
        "csv_path": str(dest),
        "notes_md_path": str(notes_dest),
        "ready_count": len(ready),
        "manual_variant_count": len(manual_variant),
        "manual_series_count": len(manual_series),
        "wish_list_count": len(wish_rows),
        "oldest_pending_days": _oldest_pending_days(all_pending),
    }


def cmd_collection_status(verbose: bool = False) -> dict[str, Any]:
    """Return cache status metrics.

    With verbose=True also returns agent_win/locg_export counts, median win age,
    reconciliation success rate, and behavioral drift event count.
    """
    from locg import __version__
    from locg.collection_io import _pending_push_rows

    cache = CollectionCache()
    payload = cache.load()

    comics = payload.get("comics", [])
    ready, manual_variant, manual_series = _pending_push_rows(payload)
    all_pending = ready + manual_variant + manual_series

    last_full_import = payload.get("last_full_import")

    result: dict[str, Any] = {
        "last_full_import": last_full_import,
        "last_import_source": payload.get("last_import_source"),
        "row_count": len(comics),
        "cache_age_days": _cache_age_days(last_full_import),
        "pending_push_count": len(all_pending),
        "oldest_pending_days": _oldest_pending_days(all_pending),
        "locg_cli_version": __version__,
        "schema_version": payload.get("schema_version"),
    }

    if not verbose:
        return result

    # Verbose metrics (F9 observability)
    from datetime import datetime, timezone

    agent_wins = [r for r in comics if r.get("source") == "agent_win"]
    locg_exports = [r for r in comics if r.get("source") == "locg_export"]

    median_win_age: Optional[int] = None
    if agent_wins:
        now = datetime.now(timezone.utc)
        ages = []
        for row in agent_wins:
            added = row.get("local_added_at") or ""
            if added:
                try:
                    dt = datetime.fromisoformat(added.replace("Z", "+00:00"))
                    ages.append((now - dt).days)
                except ValueError:
                    pass
        if ages:
            ages.sort()
            mid = len(ages) // 2
            median_win_age = ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) // 2

    recon_success_rate: Optional[float] = None
    drift_events: Optional[int] = None
    # Held ownership downgrades (BUI-124): imports that declined to silently
    # un-own a book because LOCG reported in_collection=0 over an owned row.
    # Surfaced here so they can be reviewed (a real un-collect applied, a stale
    # LOCG state ignored) rather than silently dropping ownership.
    ownership_downgrades_held: Optional[int] = None
    if cache.audit_path.exists():
        try:
            lines = cache.audit_path.read_text().strip().splitlines()
            types = []
            for line in lines:
                try:
                    types.append(json.loads(line).get("type", ""))
                except (json.JSONDecodeError, AttributeError):
                    pass
            recon = sum(1 for t in types if t == "reconciliation")
            ambiguous = sum(1 for t in types if t == "ambiguous_reconciliation")
            total = recon + ambiguous
            if total > 0:
                recon_success_rate = round(recon / total, 2)
            drift_events = sum(1 for t in types if t == "behavioral_drift")
            ownership_downgrades_held = sum(
                1 for t in types if t == "ownership_downgrade_held"
            )
        except OSError:
            pass

    result.update({
        "agent_win_count": len(agent_wins),
        "locg_export_count": len(locg_exports),
        "needs_manual_variant_count": len(manual_variant),
        "needs_manual_series_canonical_count": len(manual_series),
        "median_agent_win_age_days": median_win_age,
        "reconciliation_success_rate_last_5_imports": recon_success_rate,
        "behavioral_drift_events_last_5_imports": drift_events,
        "ownership_downgrades_held": ownership_downgrades_held,
    })
    return result


_ISSUE_TOKEN_RE = re.compile(r"#\s*(\d+(?:\.[A-Za-z0-9]+)?[A-Za-z]*)")


def _split_full_title(full_title: str) -> tuple[str, Optional[str]]:
    """Split a cached ``full_title`` into ``(series_portion, issue_token)``.

    ``"Thor #154"``                -> ``("Thor", "154")``
    ``"Fantastic Four Annual #6"`` -> ``("Fantastic Four Annual", "6")``
    ``"Watchmen"`` (no ``#N``)     -> ``("Watchmen", None)``

    The series portion is everything before the ``#N`` token, so qualifier words
    like ``Annual`` / ``King-Size Annual`` stay attached to the series identity
    instead of being silently collapsed into the base series (BUI-26).
    """
    m = _ISSUE_TOKEN_RE.search(full_title)
    if m:
        return full_title[: m.start()].strip(), m.group(1)
    return full_title.strip(), None


# Cover/masthead title (normalized) -> LOCG catalog base series (normalized),
# for series whose catalog name drops a masthead adjective. Used as a year-gated
# fallback in cmd_collection_check (BUI-46): "The Mighty Thor #154" (1968) should
# resolve to the owned "Thor #154" without colliding with the distinct
# "The Mighty Thor (Vol. 3)" 2015 series. Verified against the real catalog; extend
# conservatively (only when the LOCG series name genuinely drops the adjective).
_SERIES_ALIASES: dict[str, str] = {
    "mighty thor": "thor",
    "invincible iron man": "iron man",
}


def _match_owned_issue(
    comics: list[dict[str, Any]],
    series_key: str,
    issue_stripped: str,
    issue: str,
    variant: Optional[str],
    year: Optional[str],
) -> Optional[str]:
    """Return the full_title of an owned cache row matching the series key +
    issue (+ optional variant/year), or None. Shared by the direct and the
    alias-fallback passes of cmd_collection_check.

    `variant` is a SOFT preference, not a hard filter (BUI-176): an owned row
    that matches series + issue (+ year) still counts as in-collection even when
    its stored title lacks the variant word — otherwise a variant-qualified
    query (e.g. "newsstand") hides the owned base issue and the pipeline
    re-buys it. When a variant-bearing owned row does exist, it is preferred.
    """
    fallback: Optional[str] = None
    for row in comics:
        # in_collection is a copies-owned count (0 = wish-list / pull / read but
        # not owned). Only owned rows count as "in collection" (BUI-26 bug D).
        if not row.get("in_collection"):
            continue

        full_title = row.get("full_title") or ""
        title_series, title_issue = _split_full_title(full_title)

        # Series identity comes from the title prefix, so "Fantastic Four Annual"
        # does not satisfy a plain "Fantastic Four" query (BUI-26 bug C).
        if _normalize_series_key(title_series) != series_key:
            continue

        if title_issue is not None:
            # Exact issue-token equality (leading zeros ignored). No substring
            # fallback, so issue "2" no longer matches "#32" (BUI-26 bug B).
            norm_title_issue = title_issue.lstrip("0") or title_issue
            if norm_title_issue.lower() != issue_stripped.lower():
                continue
        else:
            # Title carries no "#N" (TPB / OGN / special): require the issue
            # token to appear verbatim.
            if issue.strip().lower() not in full_title.lower():
                continue

        # BUI-105: only reject on a year mismatch when the row actually carries a
        # release_date. A dateless owned row (e.g. an index-resolved record-win
        # written before its date was stamped) must not be silently excluded by
        # the year gate — treat absent dates as a year match, not a miss.
        release_date = row.get("release_date") or ""
        if year and release_date and not release_date.startswith(str(year)):
            continue

        # BUI-176: variant is a soft preference. With no variant requested, the
        # first series+issue match wins (unchanged behavior). With a variant
        # requested, a variant-bearing row wins immediately; an otherwise-correct
        # base row is held as a fallback so ownership is still reported rather
        # than a false not_in_cache that triggers a duplicate buy.
        if not variant:
            return full_title
        if variant.lower() in full_title.lower():
            return full_title
        if fallback is None:
            fallback = full_title

    return fallback


def cmd_collection_check(
    series: str,
    issue: str,
    variant: Optional[str] = None,
    year: Optional[str] = None,
) -> dict[str, Any]:
    """Check whether a comic is in the local collection cache.

    Returns {match_status, full_title_matched, cache_age_days}.
    match_status: "in_collection" | "not_in_cache".
    """
    cache = CollectionCache()
    payload = cache.load()
    cache_age = _cache_age_days(payload.get("last_full_import"))
    comics = payload.get("comics", [])

    series_key = _normalize_series_key(series)
    # Strip leading zeros for the issue token comparison
    issue_stripped = str(issue).strip().lstrip("0") or str(issue).strip()

    matched = _match_owned_issue(comics, series_key, issue_stripped, issue, variant, year)

    # BUI-46: masthead/cover-title fallback. If the cover title ("The Mighty
    # Thor") missed, retry against the LOCG catalog base ("Thor"). Year-gated:
    # only attempt with a year, and the year filter in _match_owned_issue then
    # ensures the era matches, so it can't collide with a distinct same-masthead
    # series (e.g. The Mighty Thor (Vol. 3), 2015).
    if matched is None and year:
        alias_key = _SERIES_ALIASES.get(series_key)
        if alias_key is not None:
            matched = _match_owned_issue(
                comics, alias_key, issue_stripped, issue, variant, year
            )

    if matched is not None:
        return {
            "match_status": "in_collection",
            "full_title_matched": matched,
            "cache_age_days": cache_age,
        }

    return {
        "match_status": "not_in_cache",
        "full_title_matched": None,
        "cache_age_days": cache_age,
    }


def cmd_collection_series_names() -> dict[str, Any]:
    """Return the canonical series names present in the collection cache.

    The matcher gates a `not_in_cache` verdict behind an *exact* normalized
    series-key match (BUI-26, to keep "Fantastic Four Annual" from satisfying a
    "Fantastic Four" query). That exactness means a caller using a slightly-off
    series name (Metron's "Uncanny X-Men (Vol. 1)" vs. the LOCG catalog's
    "Uncanny X-Men") gets a silent miss with no hint why. This endpoint surfaces
    the cache's actual series names so a caller can offer a "did you mean X?"
    correction (BUI-129) instead of reporting a false "not owned".

    The names come straight from `series_name_index`, which is rebuilt from
    `source='locg_export'` rows on every import (R61), so it reflects the real
    LOCG catalog spelling. Returns the canonical names sorted case-insensitively.
    """
    cache = CollectionCache()
    payload = cache.load()
    index: dict[str, str] = payload.get("series_name_index", {})
    names = sorted(set(index.values()), key=str.lower)
    return {"series_names": names, "count": len(names)}


VARIANT_SUFFIX_MAP: dict[str, str] = {
    "newsstand": "Newsstand Edition",
    "newsstand edition": "Newsstand Edition",
    "direct": "Direct Edition",
    "direct edition": "Direct Edition",
    "2nd print": "2nd Printing",
    "second print": "2nd Printing",
    "2nd printing": "2nd Printing",
    "facsimile": "Facsimile Edition",
    "facsimile edition": "Facsimile Edition",
}

# Generic variant words that, on their own, are too weak to anchor a match.
_VARIANT_STOPWORDS = frozenset({"variant", "cover", "edition", "the", "a", "an"})
_VARIANT_MATCH_THRESHOLD = 0.5


def _variant_tokens(text: str) -> set[str]:
    """Normalize a variant label to a set of alphanumeric tokens."""
    return {t for t in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if t}


def _fuzzy_variant_match(variant_text: str, names: list[str]) -> Optional[str]:
    """Best fuzzy match of ``variant_text`` against Metron variant ``names``.

    Metron and the auction text rarely match verbatim ("Capullo Variant" vs
    "capullo variant", "ASM 299 Homage Cover" vs "Amazing Spider-Man #299 Homage
    Cover"), so compare by token-set Jaccard similarity. Requires the overlap to
    include at least one non-generic token (not just "variant"/"cover") and a
    similarity >= the threshold. Returns the best-matching name or ``None``.
    """
    want = _variant_tokens(variant_text)
    if not want:
        return None

    best_name: Optional[str] = None
    best_score = 0.0
    for name in names:
        have = _variant_tokens(name)
        if not have:
            continue
        shared = want & have
        if not (shared - _VARIANT_STOPWORDS):
            continue  # only generic words in common — too weak
        score = len(shared) / len(want | have)
        if score > best_score:
            best_score = score
            best_name = name

    return best_name if best_score >= _VARIANT_MATCH_THRESHOLD else None


RECORD_WIN_CHUNK_SIZE = 25


def _resolve_price(raw: Any) -> Optional[float]:
    """Parse price from a float or a '12.50 USD' string."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    # BUI-184: an empty / whitespace-only current_bid yields [] from .split(),
    # so the old str(raw).split()[0] raised IndexError (not caught by the
    # ValueError guard) and one malformed win aborted the whole record-win
    # batch. Treat an empty value as "no price" instead of raising.
    parts = str(raw).split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def cmd_collection_record_win(
    wins: list[dict[str, Any]],
    cache: Optional[Any] = None,
    metron: Optional[Any] = None,
) -> dict[str, Any]:
    """Record a batch of Gixen auction wins into the local collection cache.

    Accepts a list of dicts with keys:
      item_id, current_bid, end_date_iso,
      identify_data: {series, issue, year?, variant_text?}

    Resolves canonical Series Name via the R36 chain:
      1. series_name_index (high confidence, no Metron call)
      2. Metron lookup → format_series_name (medium confidence)
      3. Bare series name, needs_manual_series_canonical=True (fallback)

    Commits in chunks of 25 rows (RECORD_WIN_CHUNK_SIZE) so a crash only
    rolls back the in-flight chunk.  Returns summary metrics.
    """
    from datetime import datetime, timezone
    from locg.collection_cache import (
        CollectionCache,
        _normalize_series_key,
        _next_seq,
        _utcnow_iso,
    )
    from locg.metron import MetronClient, MetronCredentialError

    if cache is None:
        cache = CollectionCache()
    if metron is None:
        metron = MetronClient()

    payload = cache.load()
    series_name_index: dict[str, str] = payload.get("series_name_index", {})
    existing_titles: set[str] = {
        row.get("full_title", "") for row in payload.get("comics", [])
    }

    # BUI-34: index of (normalized series, issue token) already owned in the
    # cache, so wins for back-issues won before the last import aren't written as
    # duplicate pending rows. Uses the full_title prefix for series identity so
    # an Annual doesn't shadow the base issue (consistent with collection-check).
    #
    # BUI-184: this keys on the full_title PREFIX, not series_name, by design.
    # Real LOCG exports file annuals/specials under the BASE series_name with the
    # qualifier only in Full Title (88/98 of the sample's post-normalize series
    # divergences are this shape, e.g. "The Amazing Spider-Man" / "...Annual #14";
    # zero are the inverse masthead shape). Also keying on series_name would
    # therefore collapse "...Annual #N" into base "#N" and false-skip a genuine
    # base win — a skipped win later reads as owned and triggers a duplicate buy.
    # The prefix basis errs toward recording (never hiding ownership) — the safe
    # direction — so we intentionally do NOT broaden the key here.
    def _issue_key(token: str) -> str:
        return (token.strip().lstrip("0") or token.strip()).lower()

    owned_index: set[tuple[str, str]] = set()
    for r in payload.get("comics", []):
        if not r.get("in_collection"):
            continue
        prefix, token = _split_full_title(r.get("full_title") or "")
        if token is None:
            continue
        owned_index.add((_normalize_series_key(prefix), _issue_key(token)))

    rows_written = 0
    chunks_committed = 0
    skipped_already_owned = 0
    skipped_already_owned_titles: list[str] = []
    manual_variant_count = 0
    manual_series_count = 0
    metron_lookups_attempted = 0
    metron_lookups_succeeded = 0
    metron_variant_lookups_attempted = 0
    metron_variant_matches = 0
    partial_failure = False
    metron_disabled = False

    chunks = [
        wins[i : i + RECORD_WIN_CHUNK_SIZE]
        for i in range(0, len(wins), RECORD_WIN_CHUNK_SIZE)
    ]

    for chunk in chunks:
        built_rows: list[dict[str, Any]] = []
        chunk_manual_variant = 0
        chunk_manual_series = 0
        chunk_metron_attempted = 0
        chunk_metron_succeeded = 0
        chunk_variant_detail_attempted = 0
        chunk_variant_matches = 0

        for win in chunk:
            identify = win.get("identify_data") or {}
            series_raw = str(identify.get("series") or "").strip()
            issue_num = str(identify.get("issue") or "").strip()
            year_raw = identify.get("year")
            variant_text = str(identify.get("variant_text") or "").strip().lower()
            end_date = str(win.get("end_date_iso") or "").strip()
            item_id = str(win.get("item_id") or "").strip()
            price = _resolve_price(win.get("current_bid"))

            # date_purchased: date portion of end_date_iso
            date_purchased: Optional[str] = None
            if end_date:
                try:
                    dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    date_purchased = dt.date().isoformat()
                except ValueError:
                    date_purchased = end_date[:10] if len(end_date) >= 10 else end_date

            # R36: series resolution
            norm_key = _normalize_series_key(series_raw)
            canonical_series: Optional[str] = None
            needs_manual_series = False
            metron_data: Optional[dict[str, Any]] = None

            if norm_key in series_name_index:
                canonical_series = series_name_index[norm_key]
            elif not metron_disabled:
                try:
                    chunk_metron_attempted += 1
                    metron_data = metron.lookup_issue(series_raw, issue_num, year_raw)
                    if metron_data:
                        chunk_metron_succeeded += 1
                        canonical_series = metron.format_series_name(metron_data)
                except MetronCredentialError:
                    metron_disabled = True
                    logger.warning("Metron credentials not configured; falling back to manual series resolution.")

            if canonical_series is None:
                canonical_series = series_raw
                needs_manual_series = True
                chunk_manual_series += 1

            # BUI-34: skip wins already owned in the cache (series + issue),
            # before any variant lookup or row construction. Dedup is by issue
            # identity and ignores variant, matching the reported duplicates
            # (variant Spawn back-issues already owned).
            if issue_num and (
                _normalize_series_key(canonical_series),
                _issue_key(issue_num),
            ) in owned_index:
                skipped_already_owned += 1
                skipped_already_owned_titles.append(f"{canonical_series} #{issue_num}")
                continue

            # R32: variant handling
            needs_manual_variant = False
            base_full_title = f"{canonical_series} #{issue_num}" if issue_num else canonical_series
            if variant_text:
                suffix = VARIANT_SUFFIX_MAP.get(variant_text)
                if suffix:
                    full_title = f"{base_full_title} {suffix}"
                else:
                    # BUI-33: Metron variant resolution. The lightweight
                    # lookup_issue has no variants, so fetch issue detail and
                    # fuzzy-match the auction variant text against Metron's
                    # variant cover names. (LOCG title-search fallback is dead
                    # per the local-first pivot, ADR 0001 / BUI-25.)
                    matched_variant: Optional[str] = None
                    metron_id = metron_data.get("metron_id") if metron_data else None
                    if metron_id is not None and not metron_disabled:
                        try:
                            chunk_variant_detail_attempted += 1
                            detail = metron.lookup_issue_detail(metron_id)
                            if detail:
                                matched_variant = _fuzzy_variant_match(
                                    variant_text, detail.get("variants") or []
                                )
                        except MetronCredentialError:
                            metron_disabled = True
                            logger.warning(
                                "Metron credentials not configured; skipping variant resolution."
                            )

                    if matched_variant:
                        full_title = f"{base_full_title} {matched_variant}"
                        chunk_variant_matches += 1
                    elif base_full_title in existing_titles:
                        # Base issue already owned — attach to the canonical entry.
                        full_title = base_full_title
                    else:
                        full_title = base_full_title
                        needs_manual_variant = True
                        chunk_manual_variant += 1
            else:
                full_title = base_full_title

            # release_date: prefer store_date, fall back to cover_date
            release_date: Optional[str] = None
            if metron_data:
                release_date = metron_data.get("store_date") or metron_data.get("cover_date")

            # BUI-105: when no Metron data backs this win (the series_name_index
            # path, or the bare-series manual fallback), there is no Metron date,
            # so the row would be written dateless and miss a year-gated
            # collection-check. Stamp a best-effort release_date from the identify
            # year (Jan 1 — year precision is all the year.startswith() gate in
            # _match_owned_issue needs) so a just-won book reads as in-collection.
            # A Metron hit that simply lacks dates stays blank (R66) — the relaxed
            # year gate already lets that row match.
            if release_date is None and metron_data is None and year_raw is not None:
                year_str = str(year_raw).strip()
                if re.fullmatch(r"\d{4}", year_str):
                    release_date = f"{year_str}-01-01"

            row: dict[str, Any] = {
                "publisher_name": None,
                "series_name": canonical_series,
                "full_title": full_title,
                "release_date": release_date,
                "in_collection": 1,
                "in_wish_list": 0,
                "marked_read": 0,
                "my_rating": None,
                "media_format": None,
                "price_paid": price,
                "date_purchased": date_purchased,
                "condition": None,
                "notes": None,
                "tags": None,
                "storage_box": None,
                "owner": None,
                "purchase_store": "eBay",
                "signature": 0,
                "slabbing": 0,
                "grading": None,
                "grading_company": None,
                "local_added_at": _utcnow_iso(),
                "local_added_seq": _next_seq(),
                "pushed_to_locg_at": None,
                "last_seen_in_export_at": None,
                "source": "agent_win",
                "needs_manual_variant": needs_manual_variant,
                "needs_manual_series_canonical": needs_manual_series,
                "metron_id": metron_data.get("metron_id") if metron_data else None,
                "gixen_item_id": item_id or None,
                "previous_full_title": None,
            }
            built_rows.append(row)

        try:
            cache.write_wins(built_rows, command="record-win")
            rows_written += len(built_rows)
            chunks_committed += 1
            manual_variant_count += chunk_manual_variant
            manual_series_count += chunk_manual_series
            metron_lookups_attempted += chunk_metron_attempted
            metron_lookups_succeeded += chunk_metron_succeeded
            metron_variant_lookups_attempted += chunk_variant_detail_attempted
            metron_variant_matches += chunk_variant_matches
        except Exception as exc:  # noqa: BLE001  # batch import — log chunk failure, continue remaining chunks
            logger.error("Chunk commit failed: %s", exc)
            partial_failure = True

    return {
        "rows_written": rows_written,
        "chunks_committed": chunks_committed,
        "skipped_already_owned": skipped_already_owned,
        "skipped_already_owned_titles": skipped_already_owned_titles,
        "manual_variant_count": manual_variant_count,
        "manual_series_count": manual_series_count,
        "metron_lookups_attempted": metron_lookups_attempted,
        "metron_lookups_succeeded": metron_lookups_succeeded,
        "metron_variant_lookups_attempted": metron_variant_lookups_attempted,
        "metron_variant_matches": metron_variant_matches,
        "partial_failure": partial_failure,
    }


def cmd_collection_doctor() -> dict[str, Any]:
    """Return first-run walkthrough and current cache status.

    Runs the same checks as status and explains the next remediation (F2).
    """
    status = cmd_collection_status(verbose=False)

    steps = [
        {
            "step": 1,
            "title": "Export your collection from LOCG",
            "instruction": (
                "Go to https://leagueofcomicgeeks.com/profile (logged in) "
                "and click 'Export My Comics'. An Excel file will download to ~/Downloads."
            ),
        },
        {
            "step": 2,
            "title": "Import the Excel file",
            "instruction": (
                "Run: locg collection import ~/Downloads/<ComicGeeks-YYYY-MM-DD-HH-MM-SS>.xlsx"
            ),
        },
        {
            "step": 3,
            "title": "Verify the import",
            "instruction": "Run: locg collection status --pretty",
        },
        {
            "step": 4,
            "title": "(Optional) Set up Metron credentials for series name resolution",
            "instruction": (
                "Add METRON_USERNAME and METRON_PASSWORD to ~/.config/locg/.env "
                "to enable automatic canonical series name lookup via Metron API."
            ),
        },
    ]

    if status["last_full_import"] is None:
        next_action = (
            "Cache is empty. Start with Step 1: export your collection from LOCG."
        )
        ready = False
    elif (status["cache_age_days"] or 0) > 14:
        next_action = (
            f"Cache is {status['cache_age_days']} days old — consider re-exporting "
            "from LOCG and re-importing."
        )
        ready = True
    else:
        next_action = "Cache is up to date."
        ready = True

    return {
        "status": status,
        "ready": ready,
        "next_action": next_action,
        "setup_steps": steps,
    }
