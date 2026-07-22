"""Command implementations for locg CLI."""
from __future__ import annotations

import getpass
import json
import logging
import math
import os
import re
import stat
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from locg._atomic import atomic_write_json
from locg.cache import IDCache, make_key
from locg.client import AuthRequired, LOCGClient
from locg.collection_cache import (
    CollectionCache,
    _coerce_year,
    _next_seq,
    _normalize_series_key,
    _utcnow_iso,
    base_full_title,
    base_series_name,
    collection_backups_root,
    owned_match_keys,
    resolve_series_for_win,
    series_year_range,
)
from locg.config import wish_list_cache_path
from locg.models import extract_comic_detail, extract_comic_lists, extract_issue, extract_my_details, extract_series
from locg.parser import parse_list_response, parse_page
from locg.parsing import (
    ISSUE_TOKEN_RE,
    normalize_issue_key,
    split_full_title as _split_full_title,
    split_series_issue_for_ownership,
)

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
        raise FileNotFoundError(
            f"Wish-list cache not found: {path}. "
            f"Run: {SERVER_STORE_EXPORT_HINT} && locg collection import <export.xlsx>"
        )
    with open(path) as f:
        data = json.load(f)
    items: list[dict[str, Any]] = data.get("items", [])
    if title:
        needle = title.lower()
        items = [it for it in items if needle in (it.get("name") or "").lower()]
    return items


def _normalize_wish_year(year: Optional[str]) -> Optional[str]:
    """Normalize + validate an optional per-issue Cover Year for a wish entry.

    Returns the 4-digit year string (stripped) when valid, ``None`` when the
    input is None/empty (an unstamped wish — the safe year-blind default).
    Raises ``ValueError`` for a non-4-digit value (e.g. a ``"1963 - 2011"``
    year-RANGE paste — the BUI-129 ``year_began`` trap — or other garbage).

    BUI-387: shared by EVERY write path that PERSISTS a year — the CLI/endpoint
    add (:func:`_wish_list_add_to_items`) and the backfill
    (:func:`cmd_wish_list_set_year`) — so a malformed year is rejected
    identically at all of them, never silently stored to later mis-scope the
    conflicts audit. It CANNOT catch a valid-but-wrong 4-digit year (a start
    year that happens to be 4 digits); that stays the caller's BUI-129
    responsibility (pass the issue's OWN cover year). A mis-scoped stamp is not
    a data-loss risk regardless — the export-side owned-safe filter
    (``wish_rows_for_export``) is year-blind and independently refuses to emit
    an ``In Collection=0`` row for any owned book — it only means the audit
    fails to surface that wish for cleanup.
    """
    if year is None:
        return None
    year = str(year).strip()
    if not year:
        return None
    if not re.fullmatch(r"\d{4}", year):
        raise ValueError(
            f"year must be a 4-digit Cover Year (got {year!r}); pass the issue's "
            "own cover year, never a series start year or range (BUI-129)."
        )
    return year


def _wish_list_add_to_items(
    title: str, items: list[dict[str, Any]], force: bool = False,
    year: Optional[str] = None,
) -> dict[str, Any]:
    """In-memory core of a single wish-list add: dedup-check + append, no I/O.

    Extracted from :func:`cmd_wish_list_add` (BUI-325) so a caller resolving
    MANY titles against the SAME in-memory ``items`` list — the creator-run
    batch write — can do so without a disk read+write per title. Mutates
    ``items`` in place (appends) when ``title`` is not a duplicate (or when
    ``force=True``); returns the same per-title result shape
    ``cmd_wish_list_add`` returns for its dedup/append branches, minus the
    ``path`` key (no write happened here — the caller writes once, after
    resolving every title).

    BUI-387: ``year`` is the entry's optional per-issue **Cover Year** — the
    publication year printed on THIS issue's cover, never the series' start
    year (``year_began`` — the BUI-129 trap that hides owned mid-run books).
    When supplied it is stamped as a separate ``year`` field on the entry (NOT
    encoded into ``name`` — the name parse surface, :func:`_split_wish_list_name`,
    is left untouched), so the conflicts audit can year-scope its ownership
    check and stop matching a vintage want against an owned modern volume.
    When absent the entry carries no ``year`` key at all — byte-for-byte the
    pre-387 schema — and every year-blind consumer behaves exactly as before.
    """
    title = (title or "").strip()
    if not title:
        return {"error": "wish-list add: title must be non-empty"}
    # BUI-387: validate the year up front (before dedup) so a malformed year
    # fails loudly here — at the shared chokepoint the CLI add, the endpoint add,
    # and creator-run all flow through — rather than being silently persisted.
    try:
        year = _normalize_wish_year(year)
    except ValueError as exc:
        return {"error": f"wish-list add: {exc}"}

    if not force:
        duplicate = _find_duplicate_wish_entry(title, items)
        if duplicate is not None:
            return {"status": "exists", "existing": duplicate, "items": len(items)}

    entry: dict[str, Any] = {"name": title, "id": None, "source": "local"}
    # BUI-387: only stamp `year` when a valid Cover Year was supplied — an
    # unstamped add keeps the exact pre-387 entry shape (no `year` key), so a
    # never-year-stamped wish stays year-blind in the conflicts audit.
    if year:
        entry["year"] = year
    items.append(entry)
    return {"status": "ok", "added": entry, "items": len(items)}


def _read_wish_list_cache_items(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Read the wish-list cache's ``items`` list from disk, or ``[]`` if the
    cache file does not exist yet.

    The read half of the tempfile+os.replace atomic pair :func:`_write_wish_list_cache`
    writes. Shared by :func:`cmd_wish_list_add` and the batch write in
    :func:`cmd_wish_list_add_creator_run` (BUI-325), so both go through the
    same read logic rather than each hand-rolling the ``path.exists()`` +
    ``json.load`` dance.

    ``path`` defaults to :func:`wish_list_cache_path` (env/cwd-governed) when
    omitted; BUI-489's guarded writers pass an explicit path derived from a
    caller-supplied ``cache`` so the read and the eventual write always agree
    on which store they mean (see :func:`_resolve_wish_list_path`).

    Deliberately does NOT catch ``json.JSONDecodeError``: this is the
    pre-write read, so a corrupt cache must fail loudly here rather than be
    silently treated as empty — degrading to ``[]`` and then writing would
    permanently erase whatever survived the corruption. Contrast
    :func:`cmd_wish_list_from_cache`'s read-only dedup callers, which DO
    tolerate a corrupt cache (BUI-313): degrading to "dedup against nothing"
    is safe there because nothing on disk gets overwritten as a result.
    """
    path = path if path is not None else wish_list_cache_path()
    if not path.exists():
        return []
    with open(path) as f:
        payload = json.load(f)
    return payload.get("items") or []


def _write_wish_list_cache(items: list[dict[str, Any]], path: Optional[Path] = None) -> Path:
    """Atomically write ``items`` as the wish-list cache.

    Uses :func:`locg._atomic.atomic_write_json` (tempfile + os.replace +
    chmod 600) — the same atomic write pattern used by every wish-list
    cache writer. Shared by :func:`cmd_wish_list_add` (one entry) and
    :func:`cmd_wish_list_add_creator_run` (BUI-325: the whole run's entries
    in one call), so there is exactly one atomic write per call site rather
    than a bespoke copy of the tempfile dance at each.

    ``path`` defaults to :func:`wish_list_cache_path` when omitted — see
    :func:`_read_wish_list_cache_items`'s matching note.
    """
    path = path if path is not None else wish_list_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    new_payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    atomic_write_json(
        path,
        new_payload,
        mode=stat.S_IRUSR | stat.S_IWUSR,  # 600
        tmp_prefix=".wish-list-",
    )

    return path


def _resolve_wish_list_path(cache: Optional[CollectionCache]) -> Path:
    """Resolve the wish-list cache path, honoring an explicitly-passed store.

    BUI-489: ``CollectionCache``'s own directory convention already treats
    ``collection.json`` and ``wish-list.json`` as co-located in ONE store
    directory (see ``CollectionCache.backup_store``/``restore_from_backup``,
    and the one-time server seed in CLAUDE.md that copies both files
    together) — so a ``cache`` passed to redirect the collection store
    redirects the wish-list store identically: ``cache.path.parent``. Falls
    back to :func:`wish_list_cache_path` (env/cwd-governed) when ``cache`` is
    ``None``, matching every other read/write in this module.
    """
    if cache is not None:
        return cache.path.parent / "wish-list.json"
    return wish_list_cache_path()


def cmd_wish_list_add(
    title: str, force: bool = False, year: Optional[str] = None,
    cache: Optional[CollectionCache] = None,
) -> dict[str, Any]:
    """Append a manual entry to the local wish-list cache.

    Writes ``{"name": title, "id": None, "source": "local"}`` to
    ``data/locg/wish-list.json`` using the same atomic write pattern as the rest
    of the wish-list cache writers (tempfile + os.replace + chmod 600, via
    :func:`_write_wish_list_cache`).

    BUI-489: refuses with ``{"status": "explicit_store_required"}`` when no
    ``cache`` is passed and ``LOCG_DATA_DIR`` is unset/unexpanded — see
    :func:`_needs_explicit_store`. Same wrong-store trap as
    ``cmd_collection_import``/``cmd_collection_record_win`` (BUI-476):
    ``LOCG_DATA_DIR`` governs the WHOLE store directory (both
    ``collection.json`` and ``wish-list.json``), so a bare default on a box
    where that resolves to a different, possibly non-empty store would
    silently append into the wrong wish-list.

    BUI-387: ``year`` is the optional per-issue **Cover Year** stamped on the
    new entry (a separate ``year`` field, never encoded into ``name``). It lets
    the conflicts audit year-scope this wish's ownership check so a vintage want
    no longer flags against an owned modern volume. It MUST be the issue's own
    cover year — never a series START year (``year_began``, the BUI-129 trap);
    an unstamped add (``year=None``) keeps the exact pre-387 entry shape and
    today's year-blind audit behavior.

    Manual adds carry ``source: "local"`` (BUI-208), which marks them as the
    local diff LOCG doesn't have yet. Since the LOCG import no longer rewrites
    ``wish-list.json`` (BUI-208), a local add — or a server-side removal — is
    durable across a ``locg collection import``. There is no wish-list push path
    (cf. the collection's record-win round-trip), so a local entry persists
    until it is removed.

    BUI-313: dedups against the existing cache via :func:`_find_duplicate_wish_entry`
    — the same series+issue-token comparison the ``/api/comics/wish-list``
    endpoint (BUI-285) and ``cmd_wish_list_add_creator_run`` (BUI-303) use, so
    "already wishlisted" means the same thing regardless of entry point. A
    duplicate is a 200 no-op (``{"status": "exists", "existing": ..., "items": ...}``)
    rather than a second appended row — this CLI path previously had no dedup
    guard at all. Pass ``force=True`` to bypass the dedup and append anyway (the
    escape hatch the endpoint exposes via ``WishListAddRequest.force`` for a
    genuinely distinct printing/variant that shares series + issue).
    """
    if _needs_explicit_store(cache):
        return _explicit_store_required_error("locg wish-list add <title>")

    path = _resolve_wish_list_path(cache)
    items = _read_wish_list_cache_items(path)

    result = _wish_list_add_to_items(title, items, force=force, year=year)
    if "error" in result or result.get("status") == "exists":
        return result

    written_path = _write_wish_list_cache(items, path)
    result["path"] = str(written_path)
    return result


def cmd_wish_list_add_creator_run(
    series: str,
    creator: str,
    series_id: int,
    role: str = "penciller",
    year: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve a creator's run on a series from Metron and wish-list the gaps (BUI-134).

    "Add creator X's run on series Y to the wish-list" has no ground-truth source
    in model memory, which silently drops DISCONTINUOUS runs (e.g. John Romita
    Jr.'s Uncanny X-Men #175–211 AND his ~1993 #287/#300–311 second stint).
    This grounds run membership in Metron's per-issue creator credits.

    Flow:
      1. Pin the creator's Metron id (``resolve_creator``) so "John Romita Jr."
         and "Sr." never collide. A name that can't be unambiguously resolved
         is an error, not a silent guess.
      2. Resolve the EXACT issue set the creator pencils (``resolve_creator_run``,
         ``role`` default ``"penciller"``). Both stints, gaps and all.
      3. Filter out issues already **owned** (per-issue collection check, by
         that issue's cover year — never a series start year, BUI-129) and
         already **wish-listed** (the local cache), reusing the same filtering
         the numeric-range path applies.
      4. Append the remaining ``"<series> #<N>"`` titles to the wish-list cache.

    ``series`` is the title used for the ``"<series> #<N>"`` wish entries (the
    LOCG-searchable form). ``series_id`` is the Metron series id (resolved by the
    caller / skill). Returns added / skipped breakdowns plus any low-confidence
    warnings for issues Metron had no credits for.
    """
    from locg.metron import MetronClient

    series = (series or "").strip()
    creator = (creator or "").strip()
    role = (role or "penciller").strip().lower() or "penciller"
    if not series:
        return {"error": "wish-list add: series must be non-empty"}
    if not creator:
        return {"error": "wish-list add: --creator must be non-empty"}

    # R11 guard (BUI-122 footgun): the owned filter below treats a `not_in_cache`
    # verdict as "not owned → safe to wish-list". On an uninitialized collection
    # cache (`last_full_import` null), `cmd_collection_check` answers `not_in_cache`
    # for EVERY issue, so the whole run would be wish-listed including books the
    # user already owns — and a wished owned book gets pushed to LOCG with
    # `In Collection=0`, deleting the collection row (BUI-122). The MacBook's local
    # store is uninitialized (the gixen server is the source of truth), so this is
    # the common case there. Refuse the write rather than silently mis-filter,
    # mirroring the server endpoint's 409 never-imported guard
    # (routes.py /api/comics/collection/check, R11).
    coll_payload = CollectionCache().load()
    if coll_payload.get("last_full_import") is None:
        return {
            "error": (
                "Collection cache never imported — refusing to wish-list a creator "
                "run, because every issue would falsely check as 'not owned' (R11) "
                "and an owned-but-wished book is deleted from the collection on the "
                "next sync (BUI-122). Run `" + SERVER_STORE_EXPORT_HINT + " && locg "
                "collection import <export.xlsx>` "
                "first, or point the store at the gixen server (LOCG_DATA_DIR), "
                "then retry."
            )
        }

    metron = MetronClient()

    resolved = metron.resolve_creator(creator)
    if resolved is None:
        return {
            "error": (
                f"Could not unambiguously resolve creator {creator!r} on Metron "
                "(zero or multiple matches). Use the exact Metron creator name "
                "to disambiguate (e.g. 'John Romita Jr.' vs 'John Romita')."
            )
        }

    run = metron.resolve_creator_run(
        series_id=series_id,
        creator_id=resolved["id"],
        creator_name=resolved["name"],
        role=role,
    )
    if run is None:
        return {
            "error": (
                f"Metron creator-run lookup failed for {resolved['name']!r} "
                f"(id={resolved['id']}) on series_id={series_id}."
            )
        }

    run_issues = run["issues"]
    warnings = run["warnings"]

    # Load existing wish-list items once for already-wishlisted dedup. A missing
    # OR corrupt cache degrades to an empty list (BUI-313: same tolerance the
    # overlay's wish-list reads apply — a bad cache shouldn't crash the run, and
    # dedup against "nothing" is safe; the owned-guard is the real safety net).
    try:
        existing = cmd_wish_list_from_cache()
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []

    to_add: list[dict[str, Any]] = []
    already_owned: list[str] = []
    already_wishlisted: list[str] = []

    for issue in run_issues:
        number = issue.get("number")
        if number is None:
            continue
        title = f"{series} #{number}"
        # Per-issue cover YEAR (never a series start year — BUI-129/BUI-184).
        cover = issue.get("cover_date") or ""
        cover_year = cover[:4] if cover else None

        owned = cmd_collection_check(series=series, issue=str(number), year=cover_year)
        # BUI-284: ambiguous_cross_volume (owned under >1 volume, no year to
        # disambiguate) counts as owned — skip it rather than wish-list an owned
        # book (BUI-122). cover_year is None when Metron has no cover date.
        if collection_check_reports_owned(owned):
            already_owned.append(title)
            continue

        if _find_duplicate_wish_entry(title, existing) is not None:
            already_wishlisted.append(title)
            continue

        # BUI-387: carry the per-issue cover_year (already the issue's OWN cover
        # year here — never year_began, BUI-129) so the wish entry is stamped
        # year-scoped, matching the numeric-range path's Step 5 behavior. Metron
        # issues with no cover_date leave `year` None → an unstamped, year-blind
        # entry (safe default).
        to_add.append({"title": title, "number": number, "year": cover_year})
        # BUI-313: track this queued title in `existing` too, not just the
        # on-disk cache — two run issues that normalize to the SAME
        # series+issue token (e.g. Metron reporting both "1" and "001") would
        # otherwise both pass this loop's dedup check, since `existing` is
        # loaded once up front and neither is written to disk until the write
        # loop below runs.
        existing.append({"name": title})

    added: list[str] = []
    errors: list[dict[str, Any]] = []

    if to_add:
        # BUI-325: ONE batched read + append + write for the whole run,
        # instead of the N reads/scans/writes that resulted from calling
        # cmd_wish_list_add once per issue. This is a fresh read of the
        # on-disk cache (not a reuse of the `existing` snapshot loaded before
        # the owned/dedup loop above) — defense in depth in case the cache
        # changed between that pass and this write, same posture as the
        # "exists" branch below. `_wish_list_add_to_items` mutates
        # `write_items` in place and is the SAME dedup+append helper
        # `cmd_wish_list_add` uses, so per-issue added/skipped accounting
        # matches the serial path exactly. The atomic tempfile+os.replace
        # write happens exactly once, at the end, via `_write_wish_list_cache`
        # — never a partial/torn file, and never once per issue.
        write_items = _read_wish_list_cache_items()

        for item in to_add:
            result = _wish_list_add_to_items(
                item["title"], write_items, year=item.get("year")
            )
            if "error" in result:
                errors.append({"title": item["title"], "error": result["error"]})
            elif result.get("status") == "exists":
                # Defense in depth: the intra-run tracking above already
                # prevents this in practice, but report it accurately rather
                # than double-counting as newly added if it ever slips
                # through.
                already_wishlisted.append(item["title"])
            else:
                added.append(item["title"])

        if added:
            _write_wish_list_cache(write_items)

    return {
        "status": "ok" if not errors else "partial",
        "series": series,
        "creator": resolved["name"],
        "creator_id": resolved["id"],
        "role": role,
        "series_id": series_id,
        "run_issue_count": len(run_issues),
        "added": added,
        "added_count": len(added),
        "already_owned": already_owned,
        "already_wishlisted": already_wishlisted,
        "warnings": warnings,
        "errors": errors,
    }


def cmd_creator_run_lookup(
    series: str,
    creator: str,
    series_id: int,
    role: str = "penciller",
) -> dict[str, Any]:
    """Resolve a creator's run on a series from Metron and report it (BUI-340).

    Read-only counterpart to :func:`cmd_wish_list_add_creator_run`: same
    creator-id-pinning + per-issue role confirmation via
    ``MetronClient.resolve_creator``/``resolve_creator_run``, but stops at
    reporting the resolved issue list — no collection check, no wish-list
    dedup, no cache read/write. Exists so a plain question ("what was X's run
    on Y?") has a ground-truthed answer that doesn't require invoking the
    wish-list-add write path (BUI-340: a Claude session answered such a
    question from model memory instead, and got Erik Larsen's Spider-Man run
    wrong — #19-43 instead of the Metron-credited #18-23 — because reaching
    for `wish-list add --creator` felt like the wrong tool for a bare
    question).

    ``series`` is carried through only for the response's ``series`` field
    (display / echo); it does not affect resolution, which is keyed entirely
    off ``series_id``. Returns ``{status, series, creator, creator_id, role,
    series_id, run_issue_count, issues, issue_numbers, warnings}`` on success,
    or ``{"error": ...}`` if the series/creator can't be resolved unambiguously.
    """
    from locg.metron import MetronClient

    series = (series or "").strip()
    creator = (creator or "").strip()
    role = (role or "penciller").strip().lower() or "penciller"
    if not series:
        return {"error": "creator-run: series must be non-empty"}
    if not creator:
        return {"error": "creator-run: --creator must be non-empty"}

    metron = MetronClient()

    resolved = metron.resolve_creator(creator)
    if resolved is None:
        return {
            "error": (
                f"Could not unambiguously resolve creator {creator!r} on Metron "
                "(zero or multiple matches). Use the exact Metron creator name "
                "to disambiguate (e.g. 'John Romita Jr.' vs 'John Romita')."
            )
        }

    run = metron.resolve_creator_run(
        series_id=series_id,
        creator_id=resolved["id"],
        creator_name=resolved["name"],
        role=role,
    )
    if run is None:
        return {
            "error": (
                f"Metron creator-run lookup failed for {resolved['name']!r} "
                f"(id={resolved['id']}) on series_id={series_id}."
            )
        }

    run_issues = run["issues"]
    warnings = run["warnings"]
    issue_numbers = [
        issue.get("number") for issue in run_issues if issue.get("number") is not None
    ]

    return {
        "status": "ok",
        "series": series,
        "creator": resolved["name"],
        "creator_id": resolved["id"],
        "role": role,
        "series_id": series_id,
        "run_issue_count": len(run_issues),
        "issues": run_issues,
        "issue_numbers": issue_numbers,
        "warnings": warnings,
    }


def cmd_wish_list_remove(title: str, cache: Optional[CollectionCache] = None) -> dict[str, Any]:
    """Remove the first matching entry from the local wish-list cache.

    Matches on exact ``name`` field. Writes via the shared atomic writer
    :func:`_write_wish_list_cache` (tempfile + os.replace + chmod 600), the
    same one every other wish-list write path uses (BUI-329).

    BUI-489: same wrong-store guard as :func:`cmd_wish_list_add` — refuses
    with ``{"status": "explicit_store_required"}`` when no ``cache`` is
    passed and ``LOCG_DATA_DIR`` is unset/unexpanded (see
    :func:`_needs_explicit_store`). A wrong-store REMOVE is the worse
    direction: it can silently drop an entry the caller never meant to touch
    while leaving the (different) store they actually intended untouched.
    """
    title = (title or "").strip()
    if not title:
        return {"error": "wish-list remove: title must be non-empty"}

    if _needs_explicit_store(cache):
        return _explicit_store_required_error("locg wish-list remove <title>")

    path = _resolve_wish_list_path(cache)
    if not path.exists():
        return {
            "error": (
                f"Wish-list cache not found: {path}. "
                f"Run: {SERVER_STORE_EXPORT_HINT} && locg collection import <export.xlsx>"
            )
        }

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

    written_path = _write_wish_list_cache(new_items, path)

    return {
        "status": "ok",
        "removed": removed,
        "items": len(new_items),
        "path": str(written_path),
    }


def cmd_wish_list_migrate_source() -> dict[str, Any]:
    """Backfill an explicit ``source`` field onto every wish-list entry (BUI-208).

    Thin wrapper over :func:`collection_io.migrate_wish_list_source` — a
    backup-gated, idempotent field-stamp. Returns its result dict for JSON output.
    """
    from locg.collection_io import migrate_wish_list_source
    return migrate_wish_list_source()


# Wish-list entry names are written as "<Series> #<Issue>" by cmd_wish_list_add
# (and by the /comic:wishlist-add skill).
#
# BUI-197 parser parity: this used to parse with its OWN regex
# (``#\s*([0-9A-Za-z.\-]+)``) while the owned-safe export parsed titles with the
# digit-led ``split_full_title``. The two disagreed on tokens like ``#A1`` /
# ``#annual`` / ``#1-A``, so a clean conflicts audit did NOT prove the exported
# CSV was owned-safe — worse, the digit-led parser made such a wish "unparseable",
# SKIPPING the ownership check, so an owned copy under an alias name got exported
# In Collection=0 and deleted. Both the audit and the export now go through the
# SINGLE shared :func:`split_series_issue_for_ownership`, which falls back to a
# permissive ``#token`` for non-digit-led tokens, so they stay in lockstep AND
# never silently skip a wish.


def _split_wish_list_name(name: str) -> Optional[tuple[str, str]]:
    """Split a wish-list entry name into ``(series, issue)``.

    Uses the shared :func:`split_series_issue_for_ownership` so the audit and the
    owned-safe export agree on every issue token, including non-digit-led ones
    (``#A1``, ``#annual``, ``#1-A``) that the digit-led parser would drop
    (BUI-197 parser parity + deletion-hole fix). Returns ``None`` only when the
    name has no ``#`` token at all or no series text before it — those entries
    can't be ownership-checked and are reported as ``unparseable`` rather than
    silently dropped.

    NOTE (BUI-379): everything AFTER the issue token — including a trailing
    printing marker like "2nd Printing" — is dropped here, same as any other
    trailing variant text (see the ``(Direct)`` case in this function's test).
    That's fine for the plain series/issue split, but a CALLER doing a
    printing-aware ownership comparison must not treat that silence as "no
    marker" — see :func:`_wish_list_name_printing_variant`, which re-derives
    the marker from the same raw ``name`` via the shared BUI-373 detector.
    Kept as a 2-tuple deliberately (not widened here). BUI-387 (per-issue year
    scoping) deliberately did NOT widen this either: a wish's Cover Year is a
    SEPARATE stored ``year`` field on the entry dict, never encoded into the
    ``name`` this parser splits — so the audit reads ``it.get("year")`` directly
    and this parse surface (and BUI-379's printing-marker sibling) stays
    untouched. The split's only two call sites (:func:`_find_duplicate_wish_entry`'s
    dedup and this docstring's own test) therefore stay unchanged.
    """
    series, issue = split_series_issue_for_ownership(name or "")
    series = series.strip()
    if not series or not issue:
        return None
    return series, issue


def _wish_list_name_printing_variant(name: str) -> Optional[str]:
    """Printing-marker substring (e.g. ``"2nd Printing"``) found anywhere in a
    raw wish-list entry ``name``, or ``None`` when it carries none.

    BUI-379: ``_split_wish_list_name`` throws away everything after the issue
    token, so a wish literally named "Foo #1 2nd Printing" loses that marker
    before :func:`cmd_wish_list_conflicts` ever sees it — the conflicts audit
    would then match it against an owned BASE printing as a plain conflict,
    and ``remove-conflicts`` could delete a wanted 2nd-printing wish because
    only the base is owned. Re-detecting straight from the raw name (rather
    than threading it through the split) uses the SAME shared detector
    (:data:`_PRINTING_MARKER_RE`, BUI-373) so a spelling recognized anywhere
    else in the package is recognized here too, without touching
    ``_split_wish_list_name``'s return shape or its other call site.

    The result is meant to be forwarded as :func:`cmd_collection_check`'s
    ``variant`` argument — a SOFT preference (BUI-176), never a hard filter —
    so it restores the marker to the printing-conflict probe's
    ``printing_query_text`` without risking a false ``not_in_cache`` on the
    owned-match itself.
    """
    m = _PRINTING_MARKER_RE.search(name or "")
    return m.group(0) if m else None


def _find_duplicate_wish_entry(title: str, items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the wish-list item among ``items`` that duplicates ``title``.

    Dedup key is the DECORATED series portion + issue token (via
    :func:`_split_wish_list_name`), compared case-insensitively with leading
    zeros stripped (:func:`normalize_issue_key`) — never ``_normalize_series_key``,
    which collapses ``(Vol. N)``/year decoration and would merge genuinely
    different volumes of the same masthead (the BUI-284 trap) — PLUS the
    printing ordinal (BUI-403, see below). A same-(series, issue) pair is only
    a duplicate when its printing also matches.

    BUI-403: ``_split_wish_list_name`` drops everything after the issue
    token, including a trailing printing marker like "2nd Printing" (same
    BUI-379 blind spot on this path). Left unguarded, wish-listing "Foo #1
    2nd Printing" when base "Foo #1" is already wished (or vice versa) would
    silently no-op as a "duplicate" even though it's a distinct, wanted book
    — the BUI-122-class data-loss direction (a wanted printing never gets
    added). Printing equality is decided by the SAME shared ordinal
    comparator every other printing-aware call site in this module uses
    (:func:`_printing_ordinal`, built on the BUI-373 :data:`_PRINTING_MARKER_RE`
    detector and BUI-379's ``_wish_list_name_printing_variant`` re-detection
    approach) — run directly against the raw, unsplit ``title``/``name`` text
    so a marker anywhere after the issue token is still seen. An unmarked
    title and an explicit "1st Printing" both resolve to ordinal 1 (still a
    duplicate); "2nd Printing" and "2nd Ptg" both resolve to ordinal 2 (also
    still a duplicate — same printing, different spelling) — only a genuine
    ordinal mismatch (e.g. unmarked vs "2nd Printing") escapes the dedup.

    BUI-313: this is the SINGLE shared dedup implementation. Both local
    wish-list-add entry points call it — ``cmd_wish_list_add`` (plain CLI/local
    add) and ``cmd_wish_list_add_creator_run`` (BUI-303/BUI-134) — and the
    ``POST /api/comics/wish-list`` endpoint (BUI-285) converges by calling
    ``cmd_wish_list_add`` rather than re-running the dedup itself, so it inherits
    this exact comparison. "Already wishlisted" therefore means the same thing at
    every entry point. It takes the caller's already-loaded ``items`` rather than
    reading the cache itself, so a caller checking many titles in one call (the
    creator-run) isn't forced into one cache read per title.

    An unparseable ``title`` (no ``#`` token) can't be compared, so it is
    treated as non-duplicate.
    """
    parsed = _split_wish_list_name(title)
    if parsed is None:
        return None
    series, issue = parsed
    series_cmp = series.strip().lower()
    issue_cmp = normalize_issue_key(issue)
    printing_cmp = _printing_ordinal(title)
    for item in items:
        item_name = item.get("name") or ""
        item_parsed = _split_wish_list_name(item_name)
        if item_parsed is None:
            continue
        item_series, item_issue = item_parsed
        if (
            item_series.strip().lower() == series_cmp
            and normalize_issue_key(item_issue) == issue_cmp
            and _printing_ordinal(item_name) == printing_cmp
        ):
            return item
    return None


def cmd_wish_list_conflicts() -> dict[str, Any]:
    """Audit the wish-list cache for items already in the collection (BUI-130).

    Cross-references every wish-list entry against the collection cache — the
    same per-item check ``/comic:wishlist-add`` runs at add time, applied
    retroactively to the full list. A wish-listed book you already own is the
    BUI-122 data-loss risk: ``/comic:collection-sync`` exports it with
    ``In Collection=0``, which tells LOCG to *remove* it from the collection.

    BUI-387: ``year`` is now forwarded to :func:`cmd_collection_check` PER WISH
    — but only a wish's OWN stamped per-issue Cover Year (``it.get("year")``,
    written at add time from the issue's cover date, never a series start year
    — the BUI-129 trap). A year-scoped wish therefore only conflicts with the
    matching-volume owned copy: a vintage grail (e.g. "The X-Men #1" stamped
    1963) no longer flags against an owned modern volume (a 1991/2018 copy),
    clearing the permanent cross-volume decoys structurally. An UNSTAMPED wish
    (no ``year`` field — the pre-387 shape) forwards ``year=None`` and keeps the
    exact year-blind behavior below, so an un-backfilled wish still matches
    ANY owned volume of its issue number (the safe, BUI-122-preserving default:
    it can over-flag a cross-era decoy, never miss an owned book). This audit
    can therefore still land on the WRONG volume/era of a same-numbered
    *unstamped* issue (BUI-266: a decoy UK-reprint "The Avengers (1973 - 1976)"
    #52 matched against an owned 1968 Vol. 1, and a base "Uncanny X-Men #201"
    wish matched an owned Newsstand copy). Each conflict therefore carries the
    SAME BUI-249 provenance fields ``cmd_collection_check`` returns
    (``series_name``, ``release_date`` of the matched row) so a caller can
    visually catch a cross-era/cross-edition false match before removing it —
    see :func:`cmd_wish_list_remove_conflicts`, which is the removal half of
    this audit and never removes anything not surfaced here first.

    BUI-372: a match whose ``printing_conflict`` is True is NOT a genuine
    conflict — it means the matched owned row is a DIFFERENT printing than
    the wished title (printings are distinct collectibles; owning a reprint
    is not owning the base printing being wished for). Left unhandled, this
    is the BUI-249/BUI-259 incident class through a new door: an owned
    reprint would produce a removable "conflict" for a wishlisted base
    printing that is, in fact, still genuinely wanted and not owned. These
    matches are therefore split into a separate ``printing_conflicts`` list
    (same provenance fields plus ``printing_candidates``, the BUI-364 shape)
    rather than into ``conflicts`` —
    :func:`cmd_wish_list_remove_conflicts` only ever derives its removal set
    from ``conflicts``, so a printing decoy can never be swept (unscoped or
    scoped) through this audit.

    BUI-379: BUI-372's split only catches the case where the OWNED row's
    ``full_title`` carries the printing marker. The reverse also happens: a
    wish-list entry can itself be named with a marker (e.g. literally
    "Foo #1 2nd Printing"), and :func:`_split_wish_list_name` silently drops
    that marker before this loop ever sees it (it isn't part of ``series`` or
    ``issue``). Left alone, an owned BASE printing would then satisfy the
    query with matching (unmarked) ordinals, ``printing_conflict`` would read
    False, and the wanted 2nd printing would land in the removable
    ``conflicts`` list. :func:`_wish_list_name_printing_variant` re-derives
    the marker from the raw ``name`` and forwards it as ``variant`` so the
    printing-conflict probe inside :func:`cmd_collection_check` sees the
    query's true (marked) ordinal and correctly routes this into
    ``printing_conflicts`` instead.

    Raises ``FileNotFoundError`` if the wish-list cache does not exist.
    """
    items = cmd_wish_list_from_cache()
    conflicts: list[dict[str, Any]] = []
    printing_conflicts: list[dict[str, Any]] = []
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
        variant = _wish_list_name_printing_variant(name)
        # BUI-387: forward this wish's OWN stamped Cover Year (or None if never
        # stamped — the pre-387 year-blind path). A stamped year resolves the
        # volume via the release-date gate; an unstamped wish stays year-blind.
        wish_year = it.get("year")
        result = cmd_collection_check(
            series=series, issue=issue, variant=variant, year=wish_year,
        )
        # BUI-284: ambiguous_cross_volume counts as owned here. For an UNSTAMPED
        # wish (year=None) the audit is year-free, so an owned-under-multiple-
        # volumes book returns ambiguous and missing it would let the owned copy
        # get exported In Collection=0 and deleted (BUI-122). For a STAMPED wish
        # (BUI-387) the cover year resolves the collision via the release-date
        # gate, so ambiguous won't fire — but treating it as owned when it does
        # stays the safe direction either way.
        if collection_check_reports_owned(result):
            entry = {
                "name": name,
                "series": series,
                "issue": issue,
                "id": it.get("id"),
                "full_title_matched": result["full_title_matched"],
                # BUI-266: matched-row provenance, so a decoy/cross-era match
                # is visible before this conflict is removed.
                "series_name": result["matched_series_name"],
                "release_date": result["matched_release_date"],
            }
            if result.get("printing_conflict"):
                # BUI-372: a printing decoy, not a genuine duplicate — see the
                # docstring above. Kept out of `conflicts` entirely so it can
                # never be removed by this audit's own removal half.
                printing_conflicts.append({
                    **entry,
                    "printing_candidates": result.get("printing_candidates"),
                })
            else:
                conflicts.append(entry)
    return {
        "total": len(items),
        "checked": checked,
        "unparseable": unparseable,
        "conflicts": conflicts,
        "printing_conflicts": printing_conflicts,
    }


def cmd_wish_list_remove_conflicts(names: Optional[list[str]] = None) -> dict[str, Any]:
    """Remove wish-list entries already in the collection (BUI-130).

    BUI-266: this used to unconditionally sweep the ENTIRE conflict set in one
    call — a caller intending to clear a handful of just-discovered conflicts
    had no way to avoid ALSO removing every other pre-existing conflict
    already sitting in the wish-list (the BUI-259 incident: 114 removed when
    ~6 were intended, including decoy cross-volume/cross-edition false
    matches — see :func:`cmd_wish_list_conflicts`). Pass ``names`` (the
    wish-list entry ``name`` field, exactly as returned by
    :func:`cmd_wish_list_conflicts`) to SCOPE the removal to that set — a name
    re-checked against a FRESH audit, not just echoed back, so a name that is
    no longer a genuine conflict (already removed, or never one — a stale or
    hand-typed name) is reported as an error rather than silently accepted.
    Omit ``names`` to remove every current conflict, matching the original
    global-sweep behavior; the HTTP layer (``api_wish_list_remove_conflicts``)
    gates that unscoped path behind an explicit ``confirm=true``, returning a
    non-mutating preview otherwise.

    Re-derives the conflict set via :func:`cmd_wish_list_conflicts`, then removes
    each by exact name with :func:`cmd_wish_list_remove`. Returns the removed
    entries plus the remaining count, so the caller can report what was cleared
    without a second audit. The GET audit is the dry-run preview; this performs
    the removal.

    BUI-372: :func:`cmd_wish_list_conflicts` keeps printing-conflict decoys
    (an owned reprint matching a wishlisted base printing, or vice versa) out
    of ``conflicts`` entirely, in its own ``printing_conflicts`` list — so
    both the unscoped sweep (``names=None``, which takes every current
    ``conflicts`` entry) and a scoped call naturally never remove one. An
    explicit ``names`` entry that only matches a printing-conflict decoy gets
    a specific error explaining why, distinct from "not a conflict at all".
    The audit's ``printing_conflicts`` is also echoed back here (never
    removed, purely informational) so a caller sees what was excluded and why.

    Raises ``FileNotFoundError`` if the wish-list cache does not exist.

    BUI-489: refuses with ``{"status": "explicit_store_required"}`` when
    ``LOCG_DATA_DIR`` is unset/unexpanded (see :func:`_needs_explicit_store`),
    checked BEFORE re-deriving the conflict audit so a refusal never spends
    that work first. Unlike the other wish-list writers this does NOT accept
    a ``cache`` override: its audit half (:func:`cmd_wish_list_conflicts`,
    unguarded per BUI-476/489 scope — it's a read) has no such override
    either, so threading one through only the removal half here would let the
    audit and the removals silently disagree on which store they mean — a
    worse trap than the one being closed. The only escape is
    ``LOCG_DATA_DIR``, which is what the comics server always sets before
    this call anyway (``routes._ensure_collection_store()``).
    """
    if _needs_explicit_store(None):
        return _explicit_store_required_error(
            "<no CLI form for remove-conflicts — set LOCG_DATA_DIR before "
            "calling cmd_wish_list_remove_conflicts() or POST "
            "/api/comics/wish-list/remove-conflicts>"
        )

    audit = cmd_wish_list_conflicts()
    conflicts_by_name: dict[str, dict[str, Any]] = {c["name"]: c for c in audit["conflicts"]}
    printing_conflicts_by_name: dict[str, dict[str, Any]] = {
        c["name"]: c for c in audit["printing_conflicts"]
    }

    errors: list[dict[str, Any]] = []
    if names is None:
        targets = list(audit["conflicts"])
    else:
        targets = []
        for name in names:
            conflict = conflicts_by_name.get(name)
            if conflict is not None:
                targets.append(conflict)
            elif name in printing_conflicts_by_name:
                errors.append({
                    "name": name,
                    "error": (
                        "printing conflict, not a genuine duplicate — the matched "
                        "owned row is a DIFFERENT printing than the wished title "
                        "(printings are distinct collectibles, BUI-372); remove it "
                        "via DELETE /api/comics/wish-list if you no longer want it, "
                        "not remove-conflicts"
                    ),
                })
            else:
                errors.append({
                    "name": name,
                    "error": "not a current wish-list/collection conflict — skipped, nothing removed",
                })

    removed: list[dict[str, Any]] = []
    for conflict in targets:
        result = cmd_wish_list_remove(conflict["name"])
        if "error" in result:
            errors.append({"name": conflict["name"], "error": result["error"]})
        else:
            # BUI-208 U2: fulfillment-drop touches ONLY wish state (cmd_wish_list_remove
            # rewrites wish-list.json and nothing else — never a collection row). Log and
            # surface the matched owned identity so the drop is visible in the sync plan,
            # never silent.
            matched = conflict.get("full_title_matched")
            logger.info(
                "fulfillment-drop: removed wish %r — owned as %r",
                conflict["name"], matched,
            )
            entry = dict(result["removed"]) if isinstance(result.get("removed"), dict) \
                else {"name": conflict["name"]}
            entry["matched_owned"] = matched
            # BUI-266: same provenance the audit surfaced, carried onto the
            # actual removal record so a reviewer can see exactly what era/
            # edition each removed wish was matched against.
            entry["matched_series_name"] = conflict.get("series_name")
            entry["matched_release_date"] = conflict.get("release_date")
            removed.append(entry)
    try:
        remaining = len(cmd_wish_list_from_cache())
    except FileNotFoundError:
        remaining = 0
    return {
        "removed": removed,
        "removed_count": len(removed),
        "scoped": names is not None,
        "errors": errors,
        "remaining": remaining,
        "checked": audit["checked"],
        "unparseable": audit["unparseable"],
        # BUI-372: never removed (see above) — surfaced so a caller sees what
        # was excluded as a printing decoy rather than silently dropped.
        "printing_conflicts": audit["printing_conflicts"],
    }


def cmd_wish_list_set_year(
    name: str, year: str, cache: Optional[CollectionCache] = None
) -> dict[str, Any]:
    """Stamp a per-issue **Cover Year** onto an existing wish-list entry (BUI-387).

    The one-time backfill primitive for the 33 permanent cross-volume decoy
    holds (vintage grail wishes that flag every audit against an owned modern
    volume): stamp each with its issue's own cover year and it only conflicts
    with the matching-volume copy thereafter (see :func:`cmd_wish_list_conflicts`).

    Matches on the EXACT ``name`` field (as returned by the conflicts audit /
    ``GET /api/comics/wish-list``) and sets a ``year`` field on every matching
    row — idempotent (re-stamping the same year is a no-op-shaped success), and
    a re-stamp with a different year overwrites. Writes via the shared atomic
    writer (:func:`_write_wish_list_cache`), the same one every wish-list write
    path uses.

    CRITICAL (BUI-129): ``year`` MUST be the issue's own **Cover Year**, never
    the series START year (``year_began``). This primitive only sanity-checks
    that ``year`` is a 4-digit year — it CANNOT tell a cover year from a start
    year, so the caller (the documented Metron-per-issue backfill pass) owns
    that distinction. Stamping a start year would reintroduce the exact bug that
    hid 16 owned X-Men. When in doubt, leave the wish UNSTAMPED (it keeps
    today's safe year-blind behavior) rather than guess.

    Returns ``{status, name, year, matched}`` on success (``matched`` = rows
    stamped, 0 if the name isn't present) or ``{error}`` for a blank name, a
    non-4-digit year, or a missing cache.

    BUI-489: refuses with ``{"status": "explicit_store_required"}`` when no
    ``cache`` is passed and ``LOCG_DATA_DIR`` is unset/unexpanded — same
    wrong-store guard as :func:`cmd_wish_list_add`/:func:`cmd_wish_list_remove`
    (see :func:`_needs_explicit_store`).
    """
    name = (name or "").strip()
    if not name:
        return {"error": "wish-list set-year: name must be non-empty"}
    # Same 4-digit guard as the add paths (shared helper) — rejects a range
    # paste / garbage; a valid-but-wrong 4-digit year stays the caller's BUI-129
    # responsibility (see the backfill process doc).
    try:
        year = _normalize_wish_year(year)
    except ValueError as exc:
        return {"error": f"wish-list set-year: {exc}"}
    if year is None:
        return {"error": "wish-list set-year: year is required (a 4-digit Cover Year)."}

    if _needs_explicit_store(cache):
        return _explicit_store_required_error("locg wish-list set-year <title> <year>")

    path = _resolve_wish_list_path(cache)
    if not path.exists():
        return {
            "error": (
                f"Wish-list cache not found: {path}. "
                f"Run: {SERVER_STORE_EXPORT_HINT} && locg collection import <export.xlsx>"
            )
        }

    with open(path) as f:
        payload = json.load(f)
    items: list[dict[str, Any]] = payload.get("items") or []

    matched = 0
    for item in items:
        if item.get("name") == name:
            item["year"] = year
            matched += 1

    if matched == 0:
        return {"error": f"wish-list set-year: '{name}' not found in cache"}

    written_path = _write_wish_list_cache(items, path)
    return {
        "status": "ok",
        "name": name,
        "year": year,
        "matched": matched,
        "path": str(written_path),
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

# BUI-476: the store every mutating collection command must be pointed at on
# the Mac Mini. Prefixed onto the remediation line in the refusal payload AND
# onto the user-facing "run this next" instructions (cmd_collection_doctor's
# walkthrough, the wish-list-cache-missing errors), which would otherwise hand
# the operator an `import` invocation the guard now refuses.
SERVER_STORE_ENV_PREFIX = "LOCG_DATA_DIR=$HOME/.comics-server/collection-store"

# The same store as a SESSION export. Use this — not the one-shot prefix — in
# any multi-step instruction: `VAR=val cmd` scopes to one command, so a
# walkthrough that prefixes only its write step would import into the server
# store and then verify the default one, reporting "cache is empty" right after
# a successful import. Reads are deliberately unguarded (BUI-476 scope), so
# nothing would catch that split.
SERVER_STORE_EXPORT_HINT = f"export {SERVER_STORE_ENV_PREFIX}"


def _unexpanded_store_path(value: str) -> bool:
    """True if ``value`` still contains a shell variable a shell never expanded.

    ``SERVER_STORE_ENV_PREFIX`` is written for a human to paste into a shell,
    where ``$HOME`` expands. A caller that copies it MECHANICALLY instead —
    into ``os.environ``, a ``subprocess`` env dict, or a quoted heredoc — gets
    the literal string, and ``CollectionCache`` would happily ``mkdir -p`` a
    directory named ``$HOME`` under the cwd and report a full-collection import
    into it as a success. That is precisely the wrong-store write this guard
    exists to refuse, so treat an unexpanded value as "no store named".
    """
    return "$" in value


def _needs_explicit_store(cache: Optional[Any]) -> bool:
    """True when a MUTATING collection command has no idea which store to write.

    BUI-471 (backfill) / BUI-476 (import, record-win): a command that only ever
    read could fall back to ``locg.config._cache_dir()``'s resolution
    (``LOCG_DATA_DIR`` env → ``<repo>/data/locg`` → ``~/.cache/locg``) and at
    worst report a misleading "nothing here". A command that WRITES cannot: run
    bare on the Mac Mini, that fallback lands on the repo's local ``data/locg``
    — a *different* store from the server-owned one at
    ``$HOME/.comics-server/collection-store`` that actually holds the
    collection. An empty local store hard-fails loudly (R11's not-imported
    guard), but a NON-empty one silently mutates the wrong data, which is the
    failure mode worth refusing outright.

    Two escapes, both of which are the caller explicitly naming a store:
    an explicitly-passed ``cache`` (how the tests and any in-process caller
    work) or a usable ``LOCG_DATA_DIR``. The comics server always satisfies
    the second — ``routes._ensure_collection_store()`` sets ``LOCG_DATA_DIR``
    before every collection call — so this never fires on a server path.

    "Usable" means non-blank AND actually expanded — see
    :func:`_unexpanded_store_path`.

    **Scope (BUI-476 + BUI-489):** every collection/wish-list MUTATOR now
    consults this — ``import``, ``record-win``, ``backfill``,
    ``remediate-delete``, ``remediate-set-copies``, and the wish-list writers
    ``wish-list add``/``remove``/``set-year`` (the wish-list writers pass
    ``cache`` straight through to this same check even though they resolve
    their OWN path via :func:`_resolve_wish_list_path`, not a
    ``CollectionCache`` — ``LOCG_DATA_DIR`` governs the whole store
    directory, collection AND wish-list alike, so one check covers both).
    ``cmd_wish_list_remove_conflicts`` also consults this (called with
    ``cache=None`` always — see its own docstring for why it does not accept
    a ``cache`` override: its audit half has none either, and a partial
    override would let the audit and the removal it drives silently disagree
    on which store they mean).

    The read commands (``check``, ``status``, ``export``) are unguarded on
    purpose so legitimate local-store CLI use still works — ``export``
    instead gets its OWN, narrower not-imported signal (BUI-489, see
    :func:`cmd_collection_export`) for the read-appropriate version of this
    problem: a silently empty result rather than a refused write.

    ``cmd_wish_list_add_creator_run`` is the one remaining mutator that does
    NOT consult this (out of BUI-489's scope — flagged, not fixed). Do not
    read the absence of a name from this docstring as a judgement that it is
    safe.
    """
    if cache is not None:
        return False
    value = os.environ.get("LOCG_DATA_DIR", "").strip()
    return not value or _unexpanded_store_path(value)


def _explicit_store_required_error(invocation: str) -> dict[str, Any]:
    """The BUI-471/BUI-476 refusal payload. ``invocation`` is the working
    command line to suggest, e.g. ``"locg collection backfill [--apply]"``."""
    return {
        "status": "explicit_store_required",
        "error": (
            "LOCG_DATA_DIR is not set (or still holds an unexpanded '$VAR') — "
            "refusing to default to locg.config._cache_dir()'s resolution, "
            "which may be a different (and possibly non-empty) store than the "
            "one you mean to write to. Set it explicitly, e.g. on the Mac "
            f"Mini: {SERVER_STORE_ENV_PREFIX} {invocation} — or, for a "
            f"multi-step session (the reads are unguarded and would otherwise "
            f"resolve a different store): {SERVER_STORE_EXPORT_HINT}"
        ),
    }


def cmd_collection_import(
    path_str: str, cache: Optional[Any] = None
) -> dict[str, Any]:
    """Import a LOCG Excel export into the local collection cache.

    BUI-476: refuses with ``{"status": "explicit_store_required"}`` when no
    ``cache`` is passed and ``LOCG_DATA_DIR`` is unset — see
    :func:`_needs_explicit_store`. This is a full-collection rewrite (identity
    upsert, rename detection, agent_win reconciliation, wish-list rebuild), so
    running it against a store the caller did not mean is the single most
    destructive wrong-store outcome in this module.
    """
    from pathlib import Path as _Path
    from locg.collection_io import import_xlsx

    # Argument validation first: a bad path is a caller error that is true of
    # every store, so it keeps raising FileNotFoundError rather than being
    # masked by the store refusal. Both are checked before any write.
    path = _Path(path_str)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if _needs_explicit_store(cache):
        return _explicit_store_required_error("locg collection import <path>")

    if cache is None:
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


def cmd_collection_export(
    out_path: Optional[str] = None, push_wishes: bool = False
) -> dict[str, Any]:
    """Export pending-push rows to a LOCG-compatible CSV + .notes.md companion.

    Wins-only by default (BUI-208 machine gate): wish rows carry
    ``In Collection=0``, which tells LOCG to *delete* the title from the
    collection on upload, so the default export emits only owned wins and can
    never produce an ``In Collection=0`` row. Pass ``push_wishes=True`` for the
    explicit, owned-safe wish mirror (deferred per BUI-208 OQ-3) — that is the
    only path that includes the local-only wish adds.

    Returns {csv_path, notes_md_path, ready_count, manual_variant_count,
    manual_series_count, wish_list_count, oldest_pending_days, pushed_wishes}
    — or the BUI-489 not-imported signal below instead, when the store looks
    genuinely untouched.

    BUI-489: export is a READ (BUI-476 left `check`/`status`/`export`
    unguarded on purpose, so legitimate local-store CLI use keeps working),
    so this does NOT consult `_needs_explicit_store` / refuse a bare default
    store the way the mutators do. What it DOES refuse is silently reporting
    a fabricated zero-row "success" when the store is genuinely untouched —
    the R11 "silence read as truth" shape: on the wrong (or a fresh, never-
    seeded) store this would otherwise write a technically-valid, empty CSV
    and exit 0, which reads exactly like "nothing new to push" and invites
    uploading it to LOCG believing wins were included.

    The signal fires ONLY when the store is empty on EVERY axis — zero
    comics, zero wish-list entries, AND no completed full import
    (`last_full_import is None`) — never merely "zero rows pending right
    now" (a legitimately-imported store can genuinely have nothing new) and
    never merely "`last_full_import` is None" alone: the record-win-only
    workflow (`/comic:collection-add` records wins via
    `cmd_collection_record_win` and exports before any `collection import`
    has ever run — see `test_audit_integrates_with_real_export_headers`,
    which documents this as "the real export pipeline") and a local-only
    wish add on a never-imported collection (push_wishes=True) both populate
    real, exportable data with `last_full_import` still `None`. Using that
    field alone would refuse both of those legitimate, already-tested flows.
    A store with real rows anywhere is not the wrong-store trap this closes.
    """
    from datetime import datetime
    from pathlib import Path as _Path
    from locg.collection_io import _pending_push_rows, generate_csv, generate_notes_md, wish_rows_for_export

    cache = CollectionCache()
    payload = cache.load()

    if not payload.get("comics") and payload.get("last_full_import") is None:
        # Cheap enough to always check regardless of push_wishes: a
        # wish-only store (never-imported collection, but real local wish
        # adds) is also legitimate — see the docstring above.
        #
        # This is a pure presence PROBE, not the write path — unlike
        # _read_wish_list_cache_items's other (write-side) callers, a corrupt
        # wish-list.json here must NOT raise. A corrupt file is itself
        # evidence the store isn't the genuinely-untouched "nothing here"
        # case this check exists to catch, so treat "can't tell" the same as
        # "assume real data" and fall through to the normal export path,
        # which reads wish-list.json again (only when push_wishes=True) via
        # collection_io's tolerant loader — the same corrupt-file tolerance
        # every other wish-list READ path in this module already has.
        try:
            wish_list_has_items = bool(_read_wish_list_cache_items())
        except json.JSONDecodeError:
            wish_list_has_items = True
        if not wish_list_has_items:
            return {
                "status": "not_imported",
                "error": (
                    "collection store is empty and has never been imported "
                    "(no comics, no wish-list entries, no last_full_import) — "
                    "refusing to export a silently empty CSV that could be "
                    "mistaken for 'nothing pending' (R11). If you expect real "
                    "data here, this is probably the wrong store — check "
                    "LOCG_DATA_DIR."
                ),
            }

    ready, manual_variant, manual_series = _pending_push_rows(payload)
    # BUI-122: only push local-only wish adds that aren't already owned. Re-dumping
    # the whole wish list re-uploaded LOCG-derived wishes and, because wish rows
    # carry In Collection=0, deleted owned-but-wished books from the collection.
    # BUI-208: wins-only by default — wish rows ship only on the explicit opt-in.
    wish_rows = wish_rows_for_export(payload) if push_wishes else []

    if out_path is None:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        dest = _Path.home() / "Downloads" / f"locg-bulk-import-{ts}.csv"
    else:
        dest = _Path(out_path)

    notes_dest = dest.with_suffix(".notes.md")

    generate_csv(ready, dest, wish_rows=wish_rows, allow_uncollect=push_wishes)
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
        "pushed_wishes": push_wishes,
    }


def cmd_collection_audit_pending(
    csv_path: str, cache: Optional[CollectionCache] = None
) -> dict[str, Any]:
    """Audit an already-exported wins CSV for data-quality issues before upload (BUI-432).

    Read-only: only opens and parses the CSV at ``csv_path``, plus a read-only
    ``cache.load()`` for the ``placeholder_date`` intent check below (BUI-466).
    Never writes to the collection store and never re-exports (re-exporting
    re-blanks placeholder dates — the caller must pass the path to a CSV a
    prior `collection export` already produced).

    This replaces the ~30 lines of inline Python `/comic:collection-sync` Step 2b
    used to re-author every run (BUI-199's pre-sync audit) with tested code.
    Per row, flags:

    - ``missing_publisher`` / ``missing_series`` / ``missing_full_title``: a
      required column (``Publisher Name`` / ``Series Name`` / ``Full Title``)
      is blank.
    - ``decorated_full_title``: ``Full Title`` carries ``(Vol.`` or a
      parenthesized 4-digit year — LOCG's own full_title never carries this
      decoration, so it signals an un-canonicalized record-win row.
    - ``placeholder_date``: ``Release Date`` is ``YYYY-01-01``. This is a pure
      SHAPE check — a legacy BUI-105 placeholder and a genuine January cover
      date are the same string, and the CSV itself carries no ``metron_id`` to
      tell them apart (BUI-122 forbids widening the uploaded artifact's
      columns to carry one). So this flag alone can never decide the row.

      **BUI-466**: whenever a shape match is found, this command additionally
      looks the row up in the collection STORE (by exact ``Series Name`` +
      ``Full Title``, matched against the same "ready to push" set the export
      itself drew from — see :func:`locg.collection_io._pending_push_rows`)
      and, if exactly one store row matches, applies the INTENT test
      ``_is_placeholder_release_date`` makes there (``source == "agent_win"``
      AND ``metron_id is None``). Three outcomes, recorded in
      ``placeholder_date_confirmed``:

        * ``True`` — confirmed BUI-105 placeholder. Stays a HARD-STOP flag in
          ``flagged_rows``/``flagged_count`` (run `locg collection backfill`
          or re-run record-win to fix it at the source).
        * ``False`` — confirmed genuine January cover date. Demoted to
          ADVISORY: reported in ``advisory_rows``/``advisory_count`` but
          excluded from ``flagged_count``, so it no longer hard-stops
          `/comic:collection-sync`. Never "corrected" — that would overwrite
          real data.
        * ``None`` — no store row matched (zero or more than one), so the
          intent can't be confirmed either way (also the fallback if the store
          itself can't be read at all, e.g. never imported). Also demoted to
          ADVISORY, not hard-stop: a genuine BUI-105 placeholder is always
          blanked before it reaches the CSV (see ``_row_to_csv_dict``), so a
          non-blank ``YYYY-01-01`` that DOES reach an exported wins CSV is, by
          construction, almost always a real cover date — treating an
          unconfirmable match as advisory errs on the side that matches
          reality far more often, and never silently drops a genuine
          BUI-105 placeholder either (it is still surfaced, just not blocking).

    Only rows with at least one HARD-STOP flag appear in ``flagged_rows``; rows
    whose only issue demoted to advisory (an unconfirmed or confirmed-genuine
    placeholder date) appear in ``advisory_rows`` instead. Both carry the same
    per-row shape (``row_index``, ``full_title``, the flag booleans, and a
    human-readable ``issues`` list for display) — a row with BOTH a hard-stop
    issue and an advisory-only placeholder note still lands in ``flagged_rows``
    (for the hard-stop issue), with the placeholder note included in its
    ``issues`` list.

    Also returns a dateless summary: a blank ``Release Date`` matches fine for
    a single row, but a batch that is all (or nearly all) dateless hangs LOCG's
    importer at 0% — ``dateless_count``/``dateless_titles`` list every dateless
    row, ``all_dateless`` is True when every row in the CSV is dateless, and
    ``dateless_warning`` is a ready-to-surface string whenever any row lacks a
    date (None when every row has one).

    Returns {csv_path, row_count, flagged_count, flagged_rows, advisory_count,
    advisory_rows, dateless_count, dateless_titles, all_dateless,
    dateless_warning}.
    """
    import csv as _csv

    from locg.collection_io import _is_placeholder_release_date, _pending_push_rows

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open(newline="") as f:
        rows = list(_csv.DictReader(f))

    # BUI-466: index the store's current "ready to push" rows (the exact set
    # the export drew the CSV from) by (Series Name, Full Title) so a
    # placeholder-shaped CSV row can be matched back to its store row for the
    # intent check. Best-effort and read-only: any failure to load the store
    # (never imported, corrupt, unreadable) degrades to "can't confirm" for
    # every row rather than raising — a store-read hiccup must never break the
    # CSV-only checks above it.
    placeholder_lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    try:
        store = cache if cache is not None else CollectionCache()
        payload = store.load()
        ready, _manual_variant, _manual_series = _pending_push_rows(payload)
        for sr in ready:
            key = (sr.get("series_name") or "", sr.get("full_title") or "")
            placeholder_lookup.setdefault(key, []).append(sr)
    except Exception:  # noqa: BLE001  # best-effort store read; failure -> "can't confirm", never a crash
        placeholder_lookup = {}

    def _confirm_placeholder(series: str, full_title: str) -> Optional[bool]:
        matches = placeholder_lookup.get((series, full_title), [])
        if len(matches) != 1:
            return None
        return _is_placeholder_release_date(matches[0])

    flagged_rows: list[dict[str, Any]] = []
    advisory_rows: list[dict[str, Any]] = []
    for idx, r in enumerate(rows):
        pub = r.get("Publisher Name", "")
        ser = r.get("Series Name", "")
        ft = r.get("Full Title", "")
        dt = r.get("Release Date", "")

        missing_publisher = not pub.strip()
        missing_series = not ser.strip()
        missing_full_title = not ft.strip()
        decorated_full_title = bool("(Vol." in ft or re.search(r"\(\d{4}", ft))
        placeholder_date = bool(dt and re.match(r"^\d{4}-01-01$", dt))

        placeholder_confirmed: Optional[bool] = None
        if placeholder_date:
            placeholder_confirmed = _confirm_placeholder(ser, ft)

        hard_stop_issues: list[str] = []
        advisory_issues: list[str] = []
        if missing_publisher:
            hard_stop_issues.append("no publisher")
        if missing_series:
            hard_stop_issues.append("no series")
        if missing_full_title:
            hard_stop_issues.append("no full_title")
        if decorated_full_title:
            hard_stop_issues.append("decorated full_title (LOCG full_title carries no (Vol.)/(year))")
        if placeholder_date:
            if placeholder_confirmed is True:
                hard_stop_issues.append(
                    "confirmed BUI-105 placeholder (store row: agent_win, no metron_id) — "
                    "run `locg collection backfill` or re-run record-win"
                )
            else:
                verified = (
                    "verified against the store as a genuine cover date"
                    if placeholder_confirmed is False
                    else "could not be matched to a store row to verify"
                )
                advisory_issues.append(
                    f"Jan-1 date {verified} — advisory only, not a hard stop; a genuine "
                    "January cover date must never be overwritten"
                )

        issues = hard_stop_issues + advisory_issues
        if issues:
            row_entry = {
                "row_index": idx,
                "full_title": ft or "(blank)",
                "missing_publisher": missing_publisher,
                "missing_series": missing_series,
                "missing_full_title": missing_full_title,
                "decorated_full_title": decorated_full_title,
                "placeholder_date": placeholder_date,
                "placeholder_date_confirmed": placeholder_confirmed,
                "issues": issues,
            }
            if hard_stop_issues:
                flagged_rows.append(row_entry)
            else:
                advisory_rows.append(row_entry)

    row_count = len(rows)
    dateless_titles = [r.get("Full Title", "") for r in rows if not r.get("Release Date", "").strip()]
    dateless_count = len(dateless_titles)
    dateless_warning = (
        f"DATELESS: {dateless_count}/{row_count} rows lack a Release Date — backfill "
        "before upload (an all- or nearly-all-dateless batch hangs LOCG's importer at 0%)"
        if dateless_count
        else None
    )

    return {
        "csv_path": str(path),
        "row_count": row_count,
        "flagged_count": len(flagged_rows),
        "flagged_rows": flagged_rows,
        "advisory_count": len(advisory_rows),
        "advisory_rows": advisory_rows,
        "dateless_count": dateless_count,
        "dateless_titles": dateless_titles,
        "all_dateless": row_count > 0 and dateless_count == row_count,
        "dateless_warning": dateless_warning,
    }


# BUI-493: the BUI-256 unscoped-lookup mis-stamp fingerprint (BUI-488 finding).
#
# Pre-BUI-256, `lookup_issue` queried mokkari with the wrong param key
# (`{"series": id}` instead of `{"series_id": id}`), which mokkari silently
# ignored — degrading the issue query to an UNSCOPED `number=N` search across
# all of Metron. `issues[0]` then came from a wrong, unrelated book:
# `series_name` stayed correct (it came from LOCAL series resolution, not from
# the Metron hit) but `metron_id`/`release_date` were stamped from whatever
# unrelated issue #N Metron happened to return first. The bug is fixed
# (BUI-256), but rows written before the fix are latent — and because the
# mis-stamped `metron_id` is a LIVE Metron id (just for the wrong book), no
# placeholder-hunting audit (e.g. audit-pending) catches them.
def _is_unscoped_lookup_mismatch(row: dict[str, Any]) -> bool:
    """True for an agent_win row carrying the BUI-256 unscoped-lookup fingerprint.

    All three required (validated zero-false-positive against the 2026-07-19
    backup, BUI-488):

    * ``source == "agent_win"`` — an ``locg_export`` row's publisher/date/id
      came from LOCG itself, never from this bug. This scoping is also what
      excludes LOCG's own legitimate reprint rows (Facsimile / HC / TP /
      Deluxe / Nth Printing / Compendium editions LOCG files under the
      ORIGINAL masthead with a later release_date, ``metron_id=null``) —
      title-keyword logic is deliberately NOT needed; the source scope alone
      is the clean signal.
    * ``metron_id`` present — the bug's exact fingerprint. A row with no
      metron_id never went through the unscoped lookup that stamped one (or
      never resolved a Metron hit at all), so it cannot carry this defect.
    * the row's ``release_date`` year falls OUTSIDE the resolved series'
      publication window (:func:`series_year_range` on ``series_name``) by
      more than the standard ±1 tolerance. A row whose series window can't be
      parsed (unparseable or absent decoration) can't be judged either way and
      is NOT flagged — an audit that cannot tell should stay silent rather
      than guess. Same for a row with no parseable release_date year.
    """
    if row.get("source") != "agent_win":
        return False
    if not row.get("metron_id"):
        return False
    window = series_year_range(str(row.get("series_name") or ""))
    if window is None:
        return False
    year = _coerce_year(row.get("release_date"))
    if year is None:
        return False
    begin, end = window
    return not (begin - 1 <= year <= end + 1)


def cmd_collection_audit_unscoped_lookup(cache: Optional[CollectionCache] = None) -> dict[str, Any]:
    """Read-only audit: agent_win rows carrying the BUI-256 unscoped-lookup
    mis-stamp fingerprint (BUI-493, a BUI-488 follow-up).

    BUI-256 fixed a bug where `lookup_issue` degraded to an UNSCOPED
    Metron-wide `number=N` search whenever mokkari silently dropped a
    malformed series filter — `issues[0]` then came from a wrong, unrelated
    book. Rows written before the fix carry a live-but-wrong `metron_id` and
    `release_date`, with `series_name` still correct (it came from local
    resolution, not the bad hit) — see :func:`_is_unscoped_lookup_mismatch`
    for the exact fingerprint this flags.

    Read-only: only a ``cache.load()`` (or a supplied test double). Never
    writes to the collection store. Remediation is manual (e.g.
    ``collection remediate-delete`` / ``remediate-set-copies``, or a fresh
    ``record-win``) — this command only surfaces candidates.

    Returns ``{row_count, agent_win_count, flagged_count, flagged_rows}``,
    where each flagged row is ``{full_title, series_name, metron_id,
    release_date, delta_years, gixen_item_id}`` — ``delta_years`` is the
    row's release_date year's distance outside the series window (e.g. a
    window of ``(1991, 1991)`` and a release_date year of 2022 reports 31).
    """
    store = cache if cache is not None else CollectionCache()
    payload = store.load()
    comics = payload.get("comics", [])
    agent_win_count = sum(1 for r in comics if r.get("source") == "agent_win")

    flagged_rows: list[dict[str, Any]] = []
    for row in comics:
        if not _is_unscoped_lookup_mismatch(row):
            continue
        window = series_year_range(str(row.get("series_name") or ""))
        year = _coerce_year(row.get("release_date"))
        # Both non-None here: _is_unscoped_lookup_mismatch already verified it.
        assert window is not None and year is not None
        begin, end = window
        delta_years = (begin - year) if year < begin else (year - end)
        flagged_rows.append({
            "full_title": row.get("full_title"),
            "series_name": row.get("series_name"),
            "metron_id": row.get("metron_id"),
            "release_date": row.get("release_date"),
            "delta_years": delta_years,
            "gixen_item_id": row.get("gixen_item_id"),
        })

    return {
        "row_count": len(comics),
        "agent_win_count": agent_win_count,
        "flagged_count": len(flagged_rows),
        "flagged_rows": flagged_rows,
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


# BUI-189: _split_full_title (the matcher's series/issue split) is the canonical
# issue-token parser in locg.parsing, imported as _split_full_title above. Kept
# here only as a reference to where it lives.


# BUI-197: the cover/masthead alias table that used to live here
# (``_SERIES_ALIASES``, a one-directional, year-gated, check-only fallback) was
# removed. Cross-series masthead equivalence is now the SINGLE responsibility of
# :func:`locg.collection_cache.owned_match_keys`, which is symmetric, year-free,
# and consulted identically by the buy-path check, the conflicts audit, and the
# owned-safe export. The buy-path era-collision protection that the year gate
# provided survives because the ``owned_match_keys`` loop below still forwards
# ``year`` to :func:`_match_owned_issue` (a wrong-era owned row is rejected by the
# release-date filter), so no separate year-gated alias path is needed.


def _year_gate_accepts(year: Optional[str], release_date: str) -> bool:
    """Return True when `release_date`'s year is within the accepted window of
    `year`, or when either is missing (no gating possible — fail open, same as
    always). Shared by `_match_owned_issue` and `_match_wishlisted_issue` so the
    two matchers can never drift on what "same era" means.

    BUI-214 introduced a year-OR-year-minus-1 tolerance for the cover-vs-onsale
    skew (`/comic:wishlist-add` passes Metron's `cover_date` year, but LOCG
    stores the earlier on-sale `release_date` — a January-cover issue often
    ships the previous December). BUI-251 widened this to a SYMMETRIC ±1 window
    (`year-1`, `year`, `year+1`): the BUI-247 purchase-history audit found
    confirmed-owned books (Avengers #1 2013, Thor #5 2016) whose stored
    `release_date` sits ONE YEAR LATER than the query year — the opposite skew
    direction, seen on late-in-year cover dates whose actual on-sale slipped
    into the following January. The one-directional window covered only the
    first skew, producing false `not_in_cache` negatives on stock that was
    actually owned. ±1 (not wider) is deliberate: the gate's real job is
    rejecting a DIFFERENT VOLUME published years apart (a 1962 Hulk #1 vs. a
    2008 Hulk #1) — see test_check_year_gate_two_year_gap_still_rejected for
    the boundary this must keep holding.
    """
    if not year or not release_date:
        return True
    year_str = str(year)
    candidates = {year_str}
    try:
        year_int = int(year_str)
        candidates.add(str(year_int - 1))
        candidates.add(str(year_int + 1))
    except (TypeError, ValueError):
        pass
    return any(release_date.startswith(candidate) for candidate in candidates)


def _row_matches_series_issue(
    row: dict[str, Any],
    series_key: str,
    issue_stripped: str,
    issue: str,
) -> bool:
    """Return True when ``row``'s series key + issue token match the query,
    ignoring ownership/variant/year.

    The single series+issue matching core, shared by BOTH the owned matcher
    (``_owned_row_matches_series_issue``) and the wish-list matcher
    (``_match_wishlisted_issue``) so they can never drift on what "same
    series + issue" means. Encodes BUI-26 bugs B/C:

    * C — series identity comes from the ``full_title`` prefix (via the shared
      ownership split), so "Fantastic Four Annual" does not satisfy a plain
      "Fantastic Four" query.
    * B — exact issue-token equality (leading zeros ignored), no substring
      fallback, so issue "2" no longer matches "#32". A dateless/no-``#N`` row
      (TPB/OGN) still requires the issue token to appear verbatim.
    """
    full_title = row.get("full_title") or ""
    # BUI-197: permissive ownership split so an owned row with a non-digit-led
    # token ("Thor Annual #A1") parses to (series, issue) instead of (whole
    # title, None) — otherwise its series key carries the "#A1" and never
    # matches, so the audit silently misses an owned book under an alias name.
    title_series, title_issue = split_series_issue_for_ownership(full_title)
    if _normalize_series_key(title_series) != series_key:
        return False

    if title_issue is not None:
        norm_title_issue = title_issue.lstrip("0") or title_issue
        return norm_title_issue.lower() == issue_stripped.lower()
    # Title carries no "#N" (TPB / OGN / special): require the issue token to
    # appear verbatim.
    return issue.strip().lower() in full_title.lower()


def _owned_row_matches_series_issue(
    row: dict[str, Any],
    series_key: str,
    issue_stripped: str,
    issue: str,
) -> bool:
    """Return True when ``row`` is an OWNED cache row whose series key + issue
    token match the query (BUI-26 bug D layered on the shared series+issue core).

    ``in_collection`` is a copies-owned count (0 = wish/pull/read, not owned);
    only a truthy count is "owned". Used by ``_match_owned_issue`` (which adds
    variant preference + the year gate) and the BUI-284 cross-volume ambiguity
    probe (``_owned_series_issue_candidates``).
    """
    if not row.get("in_collection"):
        return False
    return _row_matches_series_issue(row, series_key, issue_stripped, issue)


def _owned_series_issue_candidates(
    comics: list[dict[str, Any]],
    series_key: str,
    issue_stripped: str,
    issue: str,
) -> list[dict[str, Any]]:
    """All owned rows matching the series key + issue token (no variant/year
    gate). Used by the BUI-284 cross-volume ambiguity probe."""
    return [
        row
        for row in comics
        if _owned_row_matches_series_issue(row, series_key, issue_stripped, issue)
    ]


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Verdict-payload projection of a cache row — the three provenance fields
    shared by the BUI-284 cross-volume ``candidates`` and the BUI-364
    ``printing_candidates`` lists."""
    return {
        "full_title": row.get("full_title") or "",
        "series_name": row.get("series_name"),
        "release_date": row.get("release_date"),
    }


def _has_cross_volume_ambiguity(candidates: list[dict[str, Any]]) -> bool:
    """Return True when owned ``candidates`` (all sharing a normalized series
    key + issue token) span more than one distinct volume/era (BUI-284).

    The normalized series key deliberately collapses ``(Vol. N)`` /
    ``(YYYY - YYYY)`` decoration, so two genuinely-different volumes of the same
    masthead (e.g. ``Fantastic Four (Vol. 1) (1961 - 1996)`` vs.
    ``Fantastic Four (Vol. 7) (2022 - 2025)``) share a key and can't be told
    apart without a ``year``. When a caller supplies NO year and owns the same
    issue number under two such rows, silently returning the first is a
    dangerous false positive (reports a book owned that the caller doesn't have
    in the volume they meant). We detect that here by looking for either:

    * more than one distinct **decorated** ``series_name`` (the usual signal —
      different LOCG volumes carry different ``(Vol. N)``/year decoration), or
    * more than one distinct **present** ``release_date`` year (covers the rare
      undecorated case where two eras are both stored as a bare masthead name).

    Empty ``series_name`` and empty ``release_date`` are ignored rather than
    counted as their own "era", so a dateless record-win row (BUI-105) sharing a
    volume with a dated row does NOT read as ambiguous — only genuinely
    distinct, populated eras trip the guard.
    """
    series_names = {
        (row.get("series_name") or "").strip()
        for row in candidates
    }
    series_names.discard("")
    if len(series_names) > 1:
        return True
    release_years = {
        year for year in (_coerce_year(row.get("release_date")) for row in candidates)
        if year is not None
    }
    # Require a gap WIDER than the ±1 cover-vs-on-sale skew that _year_gate_accepts
    # tolerates, so two copies of ONE volume that differ only by that skew (a
    # January-cover issue that shipped the prior December, BUI-214/251) aren't
    # misread as two eras. Genuinely different volumes sharing an undecorated
    # masthead name are years apart, so real cross-volume detection is preserved.
    return bool(release_years) and (max(release_years) - min(release_years) > 1)


def _match_owned_issue(
    comics: list[dict[str, Any]],
    series_key: str,
    issue_stripped: str,
    issue: str,
    variant: Optional[str],
    year: Optional[str],
    require_dated: bool = False,
) -> Optional[dict[str, Any]]:
    """Return the owned cache ROW matching the series key + issue (+ optional
    variant/year), or None. Shared by the direct and the alias-fallback passes
    of cmd_collection_check.

    BUI-249: returns the full row (not just its full_title) so the caller can
    surface match provenance — the alias pass can land on an owned issue of
    the WRONG volume/era (e.g. "The Mighty Thor #5" resolving to an owned
    Thor Vol.1/Vol.4/Vol.6 row when the intended Mighty Thor Vol.3 #5 is not
    owned), and `matched_series_name`/`matched_release_date` are how a caller
    detects that.

    `variant` is a SOFT preference, not a hard filter (BUI-176): an owned row
    that matches series + issue (+ year) still counts as in-collection even when
    its stored title lacks the variant word — otherwise a variant-qualified
    query (e.g. "newsstand") hides the owned base issue and the pipeline
    re-buys it. When a variant-bearing owned row does exist, it is preferred.

    `require_dated` (BUI-197 MUST-FIX 2): when True AND a `year` is supplied, a
    DATELESS owned row is rejected instead of fail-open-matched. This is set ONLY
    for the alias/cross-masthead passes — a year-free alias makes
    ``owned_match_keys('Hulk','1')`` overlap a classic ``Incredible Hulk #1``, so
    a dateless classic copy would otherwise falsely satisfy a year-bearing modern
    relaunch query (``Hulk #1`` 2021) and skip a legitimate buy. The exact-key
    pass keeps the BUI-105 fail-open behavior (``require_dated=False``): a
    dateless same-series record-win must still match.
    """
    fallback: Optional[dict[str, Any]] = None
    for row in comics:
        # Series-key + issue-token core (BUI-26 bugs B/C/D), shared with the
        # cross-volume ambiguity probe via _owned_row_matches_series_issue.
        if not _owned_row_matches_series_issue(row, series_key, issue_stripped, issue):
            continue

        full_title = row.get("full_title") or ""

        # BUI-105: only reject on a year mismatch when the row actually carries a
        # release_date. A dateless owned row (e.g. an index-resolved record-win
        # written before its date was stamped) must not be silently excluded by
        # the year gate — treat absent dates as a year match, not a miss.
        # BUI-197 MUST-FIX 2: but an ALIAS-derived match (require_dated) on a
        # dateless row, when a year IS known, IS rejected — the year-free alias
        # would otherwise collide two genuinely-different same-masthead eras.
        release_date = row.get("release_date") or ""
        if year and not release_date and require_dated:
            continue
        # BUI-214/BUI-251: tolerate the cover-vs-on-sale year skew (see
        # _year_gate_accepts) — accept year−1, year, or year+1, not an exact
        # match, so a genuine one-year skew in either direction doesn't read
        # as a wrong-era row.
        if not _year_gate_accepts(year, release_date):
            continue

        # BUI-176: variant is a soft preference. With no variant requested, the
        # first series+issue match wins (unchanged behavior). With a variant
        # requested, a variant-bearing row wins immediately; an otherwise-correct
        # base row is held as a fallback so ownership is still reported rather
        # than a false not_in_cache that triggers a duplicate buy.
        if not variant:
            return row
        if variant.lower() in full_title.lower():
            return row
        if fallback is None:
            fallback = row

    return fallback


def _match_wishlisted_issue(
    comics: list[dict[str, Any]],
    series_key: str,
    issue_stripped: str,
    issue: str,
    year: Optional[str],
) -> Optional[dict[str, Any]]:
    """Return a TRACKED-BUT-NOT-OWNED cache row (``in_collection == 0``)
    matching the series key + issue (+ optional year), or None.

    BUI-250: `_match_owned_issue` skips these rows entirely (BUI-26 bug D — a
    copies-owned count of 0 must never read as "owned"), which is correct for
    ownership but means `cmd_collection_check` cannot tell "genuinely
    untracked" from "catalogued on the wish list, just not owned yet" — both
    collapsed into the same `not_in_cache` verdict. Confirmed via the BUI-247
    audit: Hulk (Vol. 5) #9 has a real `in_collection=0` row, indistinguishable
    from a never-added issue like New Mutants #1 before this function existed.

    Deliberately narrower than `_match_owned_issue`: no variant preference (a
    wish-list row has no meaningful variant qualifier to prefer between
    candidates) and no masthead-alias fallback (`owned_match_keys`) — this is
    an advisory signal, not a duplicate-buy gate, so it isn't worth stacking a
    second kind of cross-masthead guessing on top of BUI-249's. The SAME year
    gate as ownership applies when `year` is supplied (`_year_gate_accepts`):
    without it, a same-masthead wish row from a different era (e.g. a
    wishlisted 2008 "Hulk #1" flagging a query about the 1962 "Hulk #1") would
    reproduce the exact wrong-era false-positive BUI-249 addressed for
    ownership — so year-gating here mirrors the ownership pass exactly.
    """
    for row in comics:
        if row.get("in_collection"):
            continue  # owned rows are _match_owned_issue's job, not this one

        # Same series+issue core as the owned matcher (BUI-26 bugs B/C), shared
        # via _row_matches_series_issue so the two matchers can't drift.
        if not _row_matches_series_issue(row, series_key, issue_stripped, issue):
            continue

        # Same _year_gate_accepts window as _match_owned_issue (BUI-214/BUI-251)
        # — kept in one shared function so the two matchers can't drift.
        release_date = row.get("release_date") or ""
        if not _year_gate_accepts(year, release_date):
            continue

        return row

    return None


# BUI-364: printing-marker conflict detection. LOCG catalogs each printing as
# its own row whose full_title carries a trailing marker ("… #1 2nd Printing"),
# and printings are DISTINCT collectibles — but the matcher's series+issue core
# deliberately ignores everything after the issue token, so an owned reprint row
# silently satisfies a query about the base printing. The confirmed incident
# (Absolute Martian Manhunter #1, eBay 147434010581): the owned "2nd Printing"
# row answered `in_collection` while the base printing — explicitly wish-listed —
# was the book actually being bought, and the orchestrator skipped it (a missed
# purchase, the BUI-308 danger direction).
#
# BUI-373: this is now the ONE printing-marker detector for the whole package.
# It used to have a second, independent implementation living in
# VARIANT_SUFFIX_MAP's "2nd print"/"second print"/"2nd printing" dict keys
# (consumed by the record-win dedup guard and the full_title builder further
# below) — an exact-string lookup that recognized fewer spellings than this
# regex and could silently drift from it (neither recognized "2nd Ptg" or a
# bare "Reprint", so a reprint filed under either spelling produced NO
# conflict flag AND could wrongly dedup-skip a genuinely new win — see
# _dedup_variant_compatible). Both call sites now compute ordinals through
# _printing_ordinal()/_PRINTING_MARKER_RE; VARIANT_SUFFIX_MAP keeps only the
# non-printing edition suffixes (Newsstand/Direct/Facsimile) it was always
# right to own.
_PRINTING_ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}

# "Reprint"/"Re-Print"/"Re Print" — the hyphen/space is a common real-world
# spelling variant (LOCG and eBay listings are inconsistent about it). Shared
# by the bare-marker branch below AND embedded in _PRINT_WORD, so both read
# the same set of spellings.
_REPRINT_WORD = r"re[\s-]?prints?"
_REPRINT_WORD_RE = re.compile(_REPRINT_WORD, re.IGNORECASE)

# The "print word" itself, in any of the spellings a listing/catalog title
# uses: "Print"/"Printing"/"Prints"/"Printings", the "Ptg"/"Ptgs" abbreviation,
# or "Reprint"/"Reprints" as the noun after an ordinal ("2nd Reprint" reads the
# same as "2nd Printing" — see _explicit_ordinal_from_match's +1 adjustment).
_PRINT_WORD_ALTS = rf"ptgs?|{_REPRINT_WORD}|print(?:ing)?s?"

# An ordinal (digit "2nd"/"3rd"/… or word "second"/"third"/…) immediately
# followed by one of the _PRINT_WORD_ALTS spellings (named groups pw1/pw2),
# OR a bare "Reprint"/"Reprints" with NO ordinal at all (named group bare) —
# a real, if imprecise, printing marker: every reprint is SOME printing after
# the first, just not a specific numbered one. Requiring an ordinal for the
# other spellings keeps a series whose NAME merely contains "printing" (or a
# variant like "Art Print"/"Printing Error") from reading as a marker.
_PRINTING_MARKER_RE = re.compile(
    r"\b(?:"
    rf"(?P<digit>\d+)\s*(?:st|nd|rd|th)[\s-]+(?P<pw1>{_PRINT_WORD_ALTS})|"
    rf"(?P<word>{'|'.join(_PRINTING_ORDINAL_WORDS)})[\s-]+(?P<pw2>{_PRINT_WORD_ALTS})|"
    rf"(?P<bare>{_REPRINT_WORD})"
    r")\b",
    re.IGNORECASE,
)

# Sentinel ordinal for a bare "Reprint"/"Reprints" marker with no explicit
# number attached. Deliberately NOT collapsed to a guessed integer (e.g. 2):
# doing so would falsely equate an unspecified reprint with a specifically
# labeled "2nd Printing" row that might actually be a 3rd-or-later printing
# (a false dedup-compatible / false conflict-cleared result in either
# direction), and would reproduce the very ordinal-1 base collision this
# detector exists to prevent if the guess were ever wrong. -1 can never equal
# a real (1-based) ordinal, so a bare "Reprint" is always treated as "some
# printing, not the base, not provably the SAME printing as a
# specifically-numbered one" — the safe default everywhere this detector is
# used (conflict-flagging and dedup alike).
_UNSPECIFIED_REPRINT_ORDINAL = -1


def _explicit_ordinal_from_match(m: "re.Match[str]") -> Optional[int]:
    """Ordinal named by an ordinal-PREFIXED match (the digit or word branch
    of :data:`_PRINTING_MARKER_RE`), or ``None`` when only the bare branch
    matched (no explicit number to report).

    Adds 1 when the matched print-word is itself "Reprint(s)" (in any of its
    spelling variants): "Nth Reprint" names the Nth print run AFTER the
    original, i.e. absolute printing #(N+1) — "1st Reprint" is the second
    print run overall (equivalent to "2nd Printing"), "2nd Reprint" the
    third, and so on. Without this adjustment "1st Reprint"/"First Reprint"
    would compute to ordinal 1 and be silently indistinguishable from an
    unmarked base query or an explicit "1st Printing" row — reproducing,
    for this one spelling, the exact ordinal-1 collision _PRINTING_MARKER_RE
    exists to prevent (found in BUI-373 review).
    """
    if m.group("digit") is not None:
        ordinal = int(m.group("digit"))
        printword = m.group("pw1")
    elif m.group("word") is not None:
        ordinal = _PRINTING_ORDINAL_WORDS[m.group("word").lower()]
        printword = m.group("pw2")
    else:
        return None
    if _REPRINT_WORD_RE.fullmatch(printword):
        ordinal += 1
    return ordinal


def _printing_ordinal(text: Optional[str]) -> int:
    """Printing ordinal named in ``text``: 2 for "2nd Printing"/"2nd Ptg", 3
    for "Third Printing"/"1st Reprint", :data:`_UNSPECIFIED_REPRINT_ORDINAL`
    for a bare "Reprint"/"Re-Print"/"Reprints" with no number attached.
    Unmarked text and an explicit "1st Printing" are both 1 — a query
    without a marker means the base (first) printing, and an owned row
    labeled "1st Printing" genuinely satisfies it.

    BUI-373: this is the single shared printing-marker detector — every
    caller (the collection-check printing_conflict probe, the record-win
    dedup guard via :func:`_dedup_variant_compatible`, the full_title suffix
    builder via :func:`_printing_marker_suffix`) computes ordinals through
    this one function, so a spelling recognized in one place is recognized
    everywhere.
    """
    m = _PRINTING_MARKER_RE.search(text or "")
    if not m:
        return 1
    ordinal = _explicit_ordinal_from_match(m)
    return ordinal if ordinal is not None else _UNSPECIFIED_REPRINT_ORDINAL


def _ordinal_suffix(n: int) -> str:
    """"2nd"/"3rd"/"11th"/"21st"/… — standard English ordinal spelling of ``n``."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _public_printing_ordinal(full_title: Optional[str]) -> Optional[int]:
    """``printing_ordinal`` value safe for the public JSON API (the
    ``printing_candidates`` shape every collection-check/wish-list consumer
    reads): the mechanical ordinal (1 = base), or ``None`` for a bare
    "Reprint" with no explicit number.

    BUI-373 review: :data:`_UNSPECIFIED_REPRINT_ORDINAL` (``-1``) is an
    internal sentinel meaningful only to this module's own equality checks
    (``_printing_ordinal(a) == _printing_ordinal(b)``) — it must never leak
    into the JSON API as a raw ``-1``, which no consumer/doc describes and
    which a naive caller could misread as a real (negative) ordinal.
    """
    ordinal = _printing_ordinal(full_title)
    return None if ordinal == _UNSPECIFIED_REPRINT_ORDINAL else ordinal


def _printing_marker_suffix(text: Optional[str]) -> Optional[str]:
    """Canonical "<N>th Printing" suffix for ``text`` when it carries an
    EXPLICIT printing ordinal (digit or word), else ``None``.

    BUI-373: the record-win-side consumer of the shared detector — it used to
    be VARIANT_SUFFIX_MAP's "2nd print"/"second print"/"2nd printing" keys, an
    exact-match dict that missed "2nd Ptg" and any ordinal past 2nd.
    Deliberately returns ``None`` (rather than a guessed ordinal) for the bare
    "Reprint"/"Reprints" marker (:data:`_UNSPECIFIED_REPRINT_ORDINAL`) —
    writing a specific printing number into a LOCG-facing title on a guess
    risks minting a title that doesn't match the real catalog row; the
    caller falls back to its existing manual-variant path for that ambiguous
    case, same as it does for an unrecognized cover variant.
    """
    m = _PRINTING_MARKER_RE.search(text or "")
    if not m:
        return None
    ordinal = _explicit_ordinal_from_match(m)
    if ordinal is None:
        return None
    return f"{_ordinal_suffix(ordinal)} Printing"


def _printing_conflict_fields(
    comics: list[dict[str, Any]],
    query_text: str,
    matched_row: dict[str, Any],
    series_keys: frozenset[str],
    issue_stripped: str,
    issue: str,
    year: Optional[str],
) -> dict[str, Any]:
    """BUI-364 verdict fields: ``{"printing_conflict": bool}`` plus, when the
    flag is raised, ``"printing_candidates"`` — every same-era cache row
    (owned or not) for the same series + issue across all printings, so the
    caller can see e.g. that the base printing is wish-listed while only a
    reprint is owned.

    ``series_keys`` must be the FULL masthead-equivalence set
    (:func:`owned_match_keys`), not just the key the match landed on — an
    owned copy of the queried printing filed under an alias masthead (e.g.
    the reprint under "Uncanny X-Men" while the base sits under "The X-Men",
    BUI-200's split) must both clear the flag and appear in the candidates,
    or the note would misstate an owned base as untracked.

    The flag raises when the matched row's printing ordinal differs from the
    query's (an unmarked query means printing 1), UNLESS some OTHER owned,
    year-compatible row DOES carry the query's ordinal — owning both the base
    and the 2nd printing must not flag a base query just because the matcher
    happened to return the reprint row first. Two year rules keep that escape
    honest:

    * Candidates are year-gated (``_year_gate_accepts``, dateless rows
      fail-open) so a wrong-era same-masthead row (a wished 2008 "Hulk #1"
      against a 2021 query) can't render as "the query's own printing".
    * When a ``year`` IS known, a DATELESS owned row cannot clear the flag
      (mirrors BUI-197 MUST-FIX 2): ``_year_gate_accepts`` fails open on it,
      so a dateless copy from a DIFFERENT era would otherwise silently
      reproduce the missed-purchase incident. The noise direction (a dateless
      same-era base flags falsely) is the safe one; the row still shows in
      the candidates list.

    Advisory only (R11): the flag qualifies the verdict, it never flips
    ``match_status`` — every owned-guard consumer (the wish-list 409, the
    conflicts audit, record-win dedup) behaves exactly as before.
    """
    query_ordinal = _printing_ordinal(query_text)
    matched_ordinal = _printing_ordinal(matched_row.get("full_title") or "")
    if matched_ordinal == query_ordinal:
        return {"printing_conflict": False}

    candidates = [
        row
        for row in comics
        if any(
            _row_matches_series_issue(row, key, issue_stripped, issue)
            for key in series_keys
        )
        and _year_gate_accepts(year, row.get("release_date") or "")
    ]
    # The queried printing IS owned by another row — the verdict is not being
    # satisfied by a foreign printing, so there is nothing to flag.
    if any(
        row.get("in_collection")
        and _printing_ordinal(row.get("full_title") or "") == query_ordinal
        and (not year or row.get("release_date"))
        for row in candidates
    ):
        return {"printing_conflict": False}

    return {
        "printing_conflict": True,
        "printing_candidates": [
            {
                **_row_summary(row),
                # Raw store columns, coerced to bools (in_collection is a
                # copies-owned count; truthy == owned).
                "in_collection": bool(row.get("in_collection")),
                "in_wish_list": bool(row.get("in_wish_list")),
                # Mechanical ordinal (1 = base printing; null = an
                # unspecified bare "Reprint", BUI-373) so a caller can pick
                # out the query's own printing among 3+ candidates without
                # re-parsing full_title.
                "printing_ordinal": _public_printing_ordinal(row.get("full_title") or ""),
            }
            for row in candidates
        ],
    }


def cmd_collection_check(
    series: str,
    issue: str,
    variant: Optional[str] = None,
    year: Optional[str] = None,
) -> dict[str, Any]:
    """Check whether a comic is in the local collection cache.

    Returns {match_status, full_title_matched, matched_series_name,
    matched_release_date, match_kind, in_wish_list, cache_age_days}.
    match_status: "in_collection" | "not_in_cache" | "ambiguous_cross_volume".

    BUI-284: "ambiguous_cross_volume" is returned when NO year was supplied and
    the same issue number is owned under more than one distinct volume/era of
    the same masthead (the normalized series key can't tell them apart). It is
    NEITHER "owned" nor "not owned" — the caller must re-check WITH a per-issue
    cover year to resolve it, and must never treat it as "owned" (skip) or "not
    owned" (buy). The verdict adds a `candidates` list of the colliding volumes
    ({full_title, series_name, release_date}) and sets match_kind="cross_volume".

    BUI-249: matched_series_name/matched_release_date/match_kind surface the
    matched row's provenance so a caller can detect an alias match that landed
    on the wrong volume/era (see _match_owned_issue). They are null whenever
    match_status is "not_in_cache" (R11 — no verdict, no provenance to report).
    match_kind is "exact" when series_key matched directly, "alias" when it
    only matched via owned_match_keys' cross-masthead fallback — "alias" is
    the signal a caller should treat as "confirm volume before trusting this".

    BUI-250: `not_in_cache` was overloading two distinct states — a genuinely
    untracked issue vs. a row that exists but is catalogued with
    `in_collection == 0` (on the wish list / pull / read but never owned).
    `in_wish_list` (always a bool, computed independently of match_status via
    _match_wishlisted_issue) distinguishes them: false means no tracked row was
    found at all, true means one was found. It's reported for every verdict —
    including "in_collection" — rather than only for "not_in_cache", since a
    duplicate row (one owned edition, one wish-list-only edition of the same
    issue) is a real, if rare, possibility.

    BUI-364: `printing_conflict` (always a bool, present on every verdict) is
    True when the ownership verdict is satisfied by a row whose full_title
    carries a printing marker ("2nd Printing", "Third Printing", …) the query
    never asked for — printings are distinct collectibles, so an owned reprint
    must not silently read as owning the base printing (the Absolute Martian
    Manhunter #1 missed-purchase incident: owned 2nd printing answered
    `in_collection` while the base printing was wish-listed). When True, the
    verdict adds `printing_candidates` — every same-series+issue row across
    all printings with its owned/wish state. ADVISORY ONLY: match_status is
    unchanged (the reprint IS owned), so every owned-guard consumer behaves
    exactly as before; the caller flags, the user decides (R11).
    """
    cache = CollectionCache()
    payload = cache.load()
    cache_age = _cache_age_days(payload.get("last_full_import"))
    comics = payload.get("comics", [])

    series_key = _normalize_series_key(series)
    # Strip leading zeros for the issue token comparison
    issue_stripped = str(issue).strip().lstrip("0") or str(issue).strip()

    matched_row = _match_owned_issue(comics, series_key, issue_stripped, issue, variant, year)
    match_kind: Optional[str] = "exact" if matched_row is not None else None
    in_wish_list = _match_wishlisted_issue(comics, series_key, issue_stripped, issue, year) is not None

    # BUI-364: the printing-conflict probe scans the FULL masthead-equivalence
    # key set (exact + alias + the X-Men split), regardless of which key the
    # match landed on — the ownership model's equivalence is symmetric, so an
    # owned base filed under an alias masthead must be visible to the probe.
    printing_keys = owned_match_keys(series, issue)
    # The query's own printing wording lives in `series` (rarely) and `variant`
    # (the normal carrier, e.g. variant="2nd Printing").
    printing_query_text = f"{series} {variant}" if variant else series

    # BUI-284: cross-volume ambiguity guard (exact-key pass, no year). With no
    # `year`, the normalized series key can't distinguish two owned volumes of
    # the same masthead that share this issue number (e.g. Fantastic Four #18 in
    # both the 1961 Vol. 1 and the 2022 Vol. 7). _match_owned_issue would return
    # an ARBITRARY one and report `in_collection` — a dangerous false positive
    # that tells the caller to skip a book they may not own in the volume they
    # meant. Instead surface an explicit `ambiguous_cross_volume` verdict listing
    # the colliding volumes so the caller (skill/human) can re-check WITH a year.
    # Only fires when a year was NOT supplied (a year resolves the collision via
    # the release-date gate) and the exact-key pass hit (the alias pass is
    # already flagged as match_kind="alias"); a single owned era is unaffected.
    if matched_row is not None and not year:
        candidates = _owned_series_issue_candidates(comics, series_key, issue_stripped, issue)
        if _has_cross_volume_ambiguity(candidates):
            return {
                "match_status": "ambiguous_cross_volume",
                "full_title_matched": matched_row.get("full_title") or "",
                "matched_series_name": matched_row.get("series_name"),
                "matched_release_date": matched_row.get("release_date"),
                "match_kind": "cross_volume",
                "in_wish_list": in_wish_list,
                "cache_age_days": cache_age,
                "candidates": [_row_summary(row) for row in candidates],
                # BUI-364: same printing probe as the in_collection verdict, so
                # the field is present (and meaningful) on every verdict shape.
                **_printing_conflict_fields(
                    comics, printing_query_text, matched_row,
                    printing_keys, issue_stripped, issue, year,
                ),
            }

    # BUI-200/BUI-197: an owned copy can be filed under a DIFFERENT series-name
    # variant for the same run — the classic X-Men issue-number split
    # ("Uncanny X-Men #107" wished vs owned "The X-Men #107") AND broader masthead
    # aliases (Incredible Hulk↔Hulk, Mighty Thor↔Thor, Invincible Iron Man↔Iron
    # Man, Uncanny X-Men↔X-Men for annuals/relaunches). owned_match_keys is the
    # single source of that equivalence: it folds article/Vol/year decoration, the
    # issue-number split, the symmetric alias table, and annual-aware keys. ``year``
    # is still forwarded to _match_owned_issue, so on the buy path a wrong-era
    # owned row is rejected by the release-date filter (era-collision protection);
    # with no year (the conflicts audit) the alias keys still resolve so the
    # cross-masthead owned book is found and the export can't emit an
    # In Collection=0 row that deletes it (the 26-deleted-X-Men data-loss bug).
    if matched_row is None:
        for alt_key in owned_match_keys(series, issue):
            if alt_key == series_key:
                continue
            # require_dated=True: an alias/cross-masthead match on a DATELESS
            # owned row is rejected when a year is known (MUST-FIX 2 — stops a
            # dateless classic "Incredible Hulk #1" from falsely satisfying a
            # year-bearing "Hulk #1" 2021 relaunch query). With NO year the alias
            # over-matches (the safe over-exclusion direction for the audit/export).
            matched_row = _match_owned_issue(
                comics, alt_key, issue_stripped, issue, variant, year,
                require_dated=True,
            )
            if matched_row is not None:
                match_kind = "alias"
                break

    if matched_row is not None:
        return {
            "match_status": "in_collection",
            "full_title_matched": matched_row.get("full_title") or "",
            "matched_series_name": matched_row.get("series_name"),
            "matched_release_date": matched_row.get("release_date"),
            "match_kind": match_kind,
            "in_wish_list": in_wish_list,
            "cache_age_days": cache_age,
            **_printing_conflict_fields(
                comics, printing_query_text, matched_row,
                printing_keys, issue_stripped, issue, year,
            ),
        }

    return {
        "match_status": "not_in_cache",
        "full_title_matched": None,
        "matched_series_name": None,
        "matched_release_date": None,
        "match_kind": None,
        "in_wish_list": in_wish_list,
        "cache_age_days": cache_age,
        # BUI-364: no matched title, so no printing to conflate — but the field
        # is present on every verdict so callers can read it unconditionally.
        "printing_conflict": False,
    }


# BUI-284: match statuses that mean "owned in at least one volume". The
# ambiguous_cross_volume verdict IS an ownership signal (the book is owned, just
# under an undetermined volume because no year was supplied) — it is only the
# BUY path (the collection-check skill) that must treat it as "flag, don't skip".
# Every OWNED-GUARD consumer (the wish-list-add 409, the conflicts audit, the
# record-win/creator-run skip) must count it as owned; treating it as not-owned
# would fail those guards open and re-open the BUI-122 data-loss path (an owned
# book wish-listed → exported In Collection=0 → deleted from LOCG).
_OWNED_MATCH_STATUSES = frozenset({"in_collection", "ambiguous_cross_volume"})


def collection_check_reports_owned(result: dict[str, Any]) -> bool:
    """True when a :func:`cmd_collection_check` result indicates the book is
    owned in at least one volume — ``in_collection`` OR the year-unresolvable
    ``ambiguous_cross_volume`` (BUI-284). The owned-guards use this so the new
    ambiguous verdict can never silently fail them open (BUI-122)."""
    return result.get("match_status") in _OWNED_MATCH_STATUSES


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


# BUI-449: series-name reconciliation was duplicated in two skills, each of
# which pulled cmd_collection_series_names' FULL catalog array into model
# context and hand-rolled its own normalized/fuzzy matching. This resolver is
# the one tested place that logic lives now; the overlay endpoint is a thin
# wrapper (routes.py) and both skills call it instead of reimplementing it.
_SERIES_NAME_FUZZY_THRESHOLD = 0.8


def _series_name_tokens(text: str) -> set[str]:
    """Tokenize a series name for fuzzy comparison.

    The exact-key pass (`_normalize_series_key`) already neutralizes leading
    articles, `(Vol. N)`, and year suffixes/ranges — this tokenizer is only
    reached for names that survive THAT pass unmatched, so it only needs to
    smooth punctuation/spacing choices (hyphens, ampersands, casing), e.g.
    "Spider-Man" vs "Spider Man".
    """
    return {t for t in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if t}


def _fuzzy_series_name_match(query: str, catalog_names: list[str]) -> Optional[str]:
    """Best confident fuzzy match of ``query`` against catalog series names.

    Confirmed (BUI-171 verification): the only residual false-negative class
    once `_normalize_series_key` handles article/Vol/year drift is a genuine
    alt-spelling — punctuation, abbreviation, or Metron-vs-LOCG word choice —
    which is real but narrow. Token-Jaccard similarity catches that class
    while a high threshold (0.8) protects the BUI-26 conflation trap this
    same module guards elsewhere: "Fantastic Four" vs "Fantastic Four Annual"
    (or "Giant-Size ..." / "King-Size ..." / "... Special") always scores well
    below 0.8 because Jaccard penalizes the extra/missing disambiguating
    token, so a shared-masthead line-extension can never masquerade as its
    base series here.

    Also requires the winning match be UNIQUE at/above threshold — if two
    catalog names both clear it, the query is genuinely ambiguous and this
    returns ``None`` ("no confident match") rather than guessing between them.
    """
    want = _series_name_tokens(query)
    if not want:
        return None

    winners: list[str] = []
    for name in catalog_names:
        have = _series_name_tokens(name)
        if not have:
            continue
        score = len(want & have) / len(want | have)
        if score >= _SERIES_NAME_FUZZY_THRESHOLD:
            winners.append(name)

    return winners[0] if len(winners) == 1 else None


def cmd_collection_series_names_resolve(names: list[str]) -> dict[str, Any]:
    """Reconcile one or more query series names to the LOCG catalog spelling.

    For each name in ``names``, returns the reconciled catalog spelling — via
    an exact `_normalize_series_key` hit first (covers leading-article/
    `(Vol. N)`/year-suffix drift, the SAME normalization `cmd_collection_check`
    gates ownership on), then a confidence-gated fuzzy fallback
    (:func:`_fuzzy_series_name_match`, covers the narrower genuine-alt-spelling
    residual, BUI-171) — or a "no confident match" verdict (``resolved: None``)
    when neither pass finds one.

    Returns ``{"results": [{"query", "resolved", "match_kind"}, ...]}`` in the
    same order as ``names``. ``match_kind`` is ``"exact"``, ``"fuzzy"``, or
    ``None`` (no confident match — ``resolved`` is also ``None`` in that case).
    This is the ONE tested place the reconciliation lives (BUI-449); the
    overlay's `/api/comics/collection/series-names/resolve` endpoint is a thin
    wrapper over this function, and both `/comic:collection-check` and
    `/comic:wishlist-add` call it instead of pulling the whole catalog array
    into model context and hand-matching it in-model.
    """
    cache = CollectionCache()
    payload = cache.load()
    index: dict[str, str] = payload.get("series_name_index", {})
    catalog_names = sorted(set(index.values()), key=str.lower)

    results: list[dict[str, Any]] = []
    for query in names:
        key = _normalize_series_key(query)
        if key in index:
            results.append({"query": query, "resolved": index[key], "match_kind": "exact"})
            continue

        fuzzy = _fuzzy_series_name_match(query, catalog_names)
        if fuzzy is not None:
            results.append({"query": query, "resolved": fuzzy, "match_kind": "fuzzy"})
        else:
            results.append({"query": query, "resolved": None, "match_kind": None})

    return {"results": results}


# BUI-373: printing suffixes ("2nd print"/"second print"/"2nd printing") used
# to live here as exact-match keys — a second, independent, less-complete
# printing-marker detector that could drift from _PRINTING_MARKER_RE (it never
# recognized "2nd Ptg" or "3rd Printing"/"Third Printing", and had no bare
# "Reprint" entry). Printing recognition now routes entirely through
# _printing_ordinal()/_printing_marker_suffix() (see their consumers below);
# this map keeps only the non-printing edition suffixes it was always right
# to own.
VARIANT_SUFFIX_MAP: dict[str, str] = {
    "newsstand": "Newsstand Edition",
    "newsstand edition": "Newsstand Edition",
    "direct": "Direct Edition",
    "direct edition": "Direct Edition",
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


def _owned_row_variant_suffix(full_title: str) -> Optional[str]:
    """Text trailing the issue token in a full_title, e.g. ``"Newsstand Edition"``.

    Empty/whitespace-only trailing text (the common case — no print-edition
    qualifier) normalizes to ``None``.
    """
    m = ISSUE_TOKEN_RE.search(full_title or "")
    if not m:
        return None
    tail = full_title[m.end():].strip()
    return tail or None


def _dedup_variant_compatible(variant_text: str, candidate_suffix: Optional[str]) -> bool:
    """True unless the win and an owned row are provably DISTINCT print editions.

    BUI-373: printing-ordinal recognition routes through the single shared
    detector (:func:`_printing_ordinal`) — the same one the collection-check
    printing_conflict probe uses — so a spelling recognized by one is
    recognized by both. Either side carrying a printing marker (an ordinal
    != 1, including the "2nd Ptg"/"Reprint" spellings a bare
    VARIANT_SUFFIX_MAP lookup used to miss entirely) makes them distinct
    printings unless the ordinals agree. A bare "Reprint" (no explicit
    number, :data:`_UNSPECIFIED_REPRINT_ORDINAL`) is therefore only
    "compatible" with another bare "Reprint" — never with a specifically
    numbered printing it might not actually match, and never with the base
    — the safe direction here (record a possibly-duplicate win rather than
    silently skip a genuinely new one, per BUI-34's original bias).

    BUI-267: a known non-printing edition suffix (Newsstand/Direct/Facsimile —
    :data:`VARIANT_SUFFIX_MAP`) names a genuinely separate LOCG catalog entry,
    so a base win must not be deduped against an owned Newsstand copy (or vice
    versa) — the reported Uncanny X-Men #201 base win incorrectly skipped
    against an owned Newsstand #201. An unrecognized ``variant_text`` (e.g. a
    cover-artist variant like "Capullo Variant") can't be reliably normalized
    against a suffix, so it stays permissive — preserving the pre-existing
    BUI-34 behavior of deduping through cosmetic cover variants.

    Known limitation (safe direction): non-printing recognition is by EXACT
    :data:`VARIANT_SUFFIX_MAP` key, so a novel phrasing like
    ``"newsstand variant"`` (not a map key) reads as ``None`` and stays
    permissive — a newsstand win against an owned newsstand row then dedups
    through, at worst producing a duplicate owned row (never hiding a new
    win). The load-bearing direction — a *base* win must NOT dedup against an
    owned Newsstand row — always holds, because the owned row's parsed
    ``candidate_suffix`` ("Newsstand Edition") IS a map key.
    """
    win_printing_ordinal = _printing_ordinal(variant_text)
    candidate_printing_ordinal = _printing_ordinal(candidate_suffix)
    if win_printing_ordinal != 1 or candidate_printing_ordinal != 1:
        return win_printing_ordinal == candidate_printing_ordinal

    known_win_suffix = VARIANT_SUFFIX_MAP.get(variant_text) if variant_text else None
    known_candidate_suffix = (
        VARIANT_SUFFIX_MAP.get(candidate_suffix.lower()) if candidate_suffix else None
    )
    if known_win_suffix is None and known_candidate_suffix is None:
        return True
    return known_win_suffix == known_candidate_suffix


def _metron_release_date(
    metron_data: Optional[dict[str, Any]],
    year_raw: Any,
    series_range: Optional[tuple[int, int]] = None,
) -> Optional[str]:
    """The trustworthy ``release_date`` for a win from a Metron lookup, or None.

    ONE helper for the whole record-win date decision, so the site that decides
    whether to *accept* a Metron hit (the BUI-210 date-only lookup) and the site
    that *stores* the date (the BUI-268 guard on the row build) can never drift
    on which date they picked or how they judged it. They were duplicated
    before, and the duplication is exactly how the two ended up examining
    different candidate dates.

    Candidate — ``store_date`` when Metron has one, else ``cover_date``. LOCG's
    own ``release_date`` is the ON-SALE date, so ``store_date`` is the closer
    analogue; ``cover_date`` covers the many vintage issues Metron has no store
    date for. Exactly ONE candidate is considered, deliberately: falling back to
    ``cover_date`` when ``store_date`` fails the guard below would weaken the
    guard from "the best date is in era" to "some date is in era", and a modern
    ``store_date`` beside an original ``cover_date`` is the reprint fingerprint
    itself, not a case worth rescuing.

    Reprint guard — a naive ``lookup_issue("The X-Men", "59", 1970)`` can
    return a collected-edition/reprint date (observed: 2005-03-09), and writing
    that onto a 1970 row poisons the year-gated collection-check (BUI-268). So
    the candidate is only accepted when its year sits in the same era as the
    win's identified year. **How much slack that allows depends on WHICH date
    we are holding, and the two are not interchangeable:**

    * ``store_date`` gets the shared :func:`_year_gate_accepts` window — the
      symmetric ±1 that ``_match_owned_issue``, ``_match_wishlisted_issue`` and
      ``_dedup_era_compatible`` use, so this write gate and the gates that
      later read the row back agree about what "same era" means. (One does
      not: ``_reconcile_score`` in ``collection_io`` still compares years
      exactly. That is deliberate and in our favour — matching LOCG's own
      on-sale date is precisely what lets a January-cover win reconcile at
      all.) The slack is earned: ``year_raw`` is the COVER
      year comic-identify reads off the book while ``store_date`` is the
      on-sale date, and a January-cover issue ships the previous November.
      BUI-210's reopen is largely this — the old exact match called that
      genuine 1969-11-10 a reprint and discarded the whole hit, leaving the
      row dateless and (since BUI-458) publisher-less too. A real reprint is
      decades away, so ±1 still rejects it.
    * ``cover_date`` gets an EXACT year match, with ONE window-gated exception
      (BUI-486). ``year_raw`` is NOT reliably the same quantity as
      ``cover_date``: it may be a printed cover year, but it may equally be a
      seller-supplied ERA claim lifted off an eBay title (*"Batman #427 DC 1989
      … A Death in the Family"*), and cover-dating convention (books cover-dated
      ahead of on-sale) makes such a claim systematically ±1 off the printed
      cover year. So a one-year gap is sometimes skew, not a wrong-book signal.
      It still must not be forgiven blindly: ``lookup_issue`` trusts a sole
      name-search hit with no year check of its own (``_disambiguate_series``)
      and takes ``issues_list()[0]`` unfiltered, which on vintage/aliased series
      makes this gate the only era check on the whole result, and a bare ±1
      window would admit UK/foreign reprint editions and one-year Metron
      data-entry slips. The exception is therefore gated on the INDEPENDENT
      volume window (below): a cover_date one year off ``year_raw`` is accepted
      only when it also lands inside the resolved volume's own ``series_range``.
      The ±1 bound is what rejects a decades-off reprint the wide window would
      otherwise contain (a 2005 collected edition of a 1989 book is 16 years off
      ``year_raw``); the window is what rejects a ±1 candidate the resolver could
      not actually place.

    ``series_range`` (BUI-464) is the ``(begin, end)`` publication window
    parsed off the LOCG canonical series name that
    :func:`resolve_series_for_win` matched this win to (e.g. ``"The X-Men
    (Vol. 1) (1963 - 1981)"`` -> ``(1963, 1981)``). It serves two gates: it is
    the substitute era evidence when ``year_raw`` is not a clean 4-digit year,
    AND (BUI-486) it is the containment gate for the clean-year cover_date ±1
    exception above. That window is INDEPENDENT of the Metron hit being judged —
    it comes from the local collection's own ``series_name_index`` — which is
    what makes it a real guard rather than a tautology. Pass it ONLY when it has
    that independent provenance; a range derived from the same hit
    (``metron.format_series_name``) would gate the candidate against itself and
    always pass.

    The store_date/cover_date asymmetry above is preserved against the window:
    a ``store_date`` may sit one year outside it (a volume's first issue with a
    January cover shipped the previous November), a ``cover_date`` may not — a
    cover date is by definition inside its own volume's run.

    Known, deliberate: a bare single-year decoration (``"The Amazing
    Spider-Man (1963)"``) parses to the degenerate one-year window
    ``(1963, 1963)``, so a genuine 1988 issue's date is rejected. The outcome
    is a dateless row — exactly what a null year produced before this
    parameter existed — so the failure is a missed improvement, never a wrong
    date written.

    With neither a clean year nor a ``series_range`` there is nothing to guard
    against, so the candidate is returned ungated — unchanged behavior for the
    R36 series-resolution path, which is the only caller that can reach here
    with no era evidence of any kind.
    """
    if not metron_data:
        return None
    store_date = metron_data.get("store_date")
    candidate = str(store_date) if store_date else None
    if candidate is None:
        cover_date = metron_data.get("cover_date")
        candidate = str(cover_date) if cover_date else None
    if candidate is None:
        return None

    year_str = str(year_raw).strip() if year_raw is not None else ""
    if not re.fullmatch(r"\d{4}", year_str):
        if series_range is None:
            return candidate
        candidate_year = _coerce_year(candidate)
        if candidate_year is None:
            return None
        begin, end = series_range
        if store_date:
            begin, end = begin - 1, end + 1
        return candidate if begin <= candidate_year <= end else None
    if store_date:
        return candidate if _year_gate_accepts(year_str, candidate) else None
    # cover_date, clean year.
    if candidate.startswith(year_str):
        return candidate
    # BUI-486: ``year_raw`` may be a seller-supplied ERA claim lifted off an
    # eBay title rather than a printed cover year, and cover-dating convention
    # (books cover-dated ahead of on-sale) skews such a claim systematically ±1
    # off the printed cover date. So a one-year gap here is sometimes that skew,
    # not a wrong-book signal. Accept a cover_date exactly one year off
    # ``year_raw`` ONLY when it also sits inside the resolved volume's INDEPENDENT
    # publication window. Both bounds carry weight: the ±1 rejects a decades-off
    # reprint even when the window is wide enough to contain it (a 2005 collected
    # edition of a 1989 Batman is 16 years off ``year_raw``, yet 2005 is inside
    # ``Batman (1940 - 2011)``), and the window rejects a ±1 candidate the
    # resolver could not actually place (a degenerate one-year window). On the
    # clean-year path ``resolve_series_for_win`` already picked this volume USING
    # ``year_raw``, so a non-degenerate window is guaranteed to contain
    # ``year_raw`` — meaning the ±1 bound is the operative reprint guard here and
    # the window's discriminating power is confined to its edges. A cover_date is
    # never given the store_date's edge-widening — it is by definition inside its
    # own volume's run.
    if series_range is not None:
        candidate_year = _coerce_year(candidate)
        if (
            candidate_year is not None
            and abs(candidate_year - int(year_str)) == 1
            and series_range[0] <= candidate_year <= series_range[1]
        ):
            return candidate
    return None


def _dedup_era_compatible(win_year: Optional[int], candidate_row: dict[str, Any]) -> bool:
    """True unless ``candidate_row``'s era provably conflicts with ``win_year``.

    BUI-267: the bare (series, issue) dedup key collides across unrelated
    volumes/eras that happen to share a masthead and issue number — reported:
    New Gods #7 (1971 Kirby) skipped against an owned "The New Gods (Vol. 5)
    (2024 - 2025)" #7. Permissive when either side's year is unknown/
    unparseable, matching BUI-34's original bias toward never hiding a
    genuinely-new win behind an uncertain match.

    Known tradeoff (deliberately unchanged — safe direction): a bare
    single-year owned decoration like ``"The Amazing Spider-Man (1963)"``
    parses to the degenerate range ``(1963, 1963)`` (a start-year, not a true
    one-year series), so a later-issue win for that same title (e.g. year
    1988) reads as era-INCOMPATIBLE and records a DUPLICATE owned row rather
    than deduping. That is the safe direction (a dup, never hiding ownership —
    BUI-34's bias), and it is why we do NOT collapse ``start == end`` to
    "permissive": doing so would re-open the exact cross-era false-SKIP this
    function exists to close (New Gods #7 1971 vs an owned 2024 Vol. 5 #7)
    whenever the owned row happened to carry only a bare start-year. Genuine
    cross-era matches we must catch carry full ``(YYYY - YYYY)`` ranges, so the
    strict check still fires correctly for them.
    """
    if win_year is None:
        return True
    rng = series_year_range(candidate_row.get("series_name") or "")
    if rng is not None:
        return rng[0] <= win_year <= rng[1]
    release_date = candidate_row.get("release_date") or ""
    if _coerce_year(release_date) is not None:
        # ±1, not an exact match, and specifically the SAME window the write
        # gate uses (_metron_release_date -> _year_gate_accepts). A win row's
        # stored release_date is Metron's on-sale date, which for a
        # January-cover book is the previous November — so an exact compare
        # here stopped recognising a row this same function had just written.
        # That mattered on the retry path: /comic:collection-add re-submits a
        # whole batch after a partial_failure, the dedup missed, and because
        # write_wins overwrites by gixen_item_id the good row was REBUILT —
        # silently downgrading a real date + publisher back to a placeholder
        # and null whenever Metron was unreachable on the retry (which is a
        # common reason the first attempt failed at all).
        #
        # This does not weaken BUI-267: the cross-era false-SKIP it exists to
        # close (New Gods #7 1971 vs an owned 2024 Vol. 5 #7) is decades wide,
        # nowhere near ±1.
        return _year_gate_accepts(str(win_year), str(release_date))
    return True


RECORD_WIN_CHUNK_SIZE = 25


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split ``items`` into consecutive slices of at most ``size``.

    Shared by ``cmd_collection_record_win`` and ``cmd_collection_backfill``
    (BUI-471) — both commit a Metron-heavy batch in chunks of
    ``RECORD_WIN_CHUNK_SIZE`` so a crash mid-run only rolls back the in-flight
    chunk, and both need the exact same slicing.
    """
    return [items[i : i + size] for i in range(0, len(items), size)]


# BUI-465: how a record-win batch survives a transient Metron trip.
#
# The breaker below is per-batch and monotonic by design (BUI-255) — but the
# batch driver used to have no way to tell WHY it tripped, so a single 429
# partway through downgraded every remaining win to a `{year}-01-01` placeholder
# and a null publisher. Forensics on the 2026-07-19 store: of the 58 placeholder
# rows, 40 came from ONE 41-row run whose first row carried a metron_id and whose
# other 40 carried none — including four more issues of a series the first row
# had just resolved. That is the latch, not a data miss.
#
# Two changes close it, and they are ordered: PREVENT, then RECOVER.
#
#   * Prevent — `cmd_collection_record_win` paces itself to
#     ``METRON_REQUESTS_PER_MINUTE`` HTTP requests per minute (Metron's
#     documented burst budget), counting requests rather than calls because
#     `lookup_issue` spends two of them. Nothing throttled the batch before;
#     at ~3 requests per win it blew the budget after ~7 wins.
#   * Recover — a trip that is NOT a credential error earns a cooldown and a
#     reset, up to ``METRON_MAX_TRANSIENT_TRIPS`` times. Past that the breaker
#     latches for the rest of the batch, which is what preserves BUI-255's real
#     protection: a genuinely unreachable Metron must not buy a fresh capped
#     sleep on every one of 44 remaining rows.
#
# A `MetronCredentialError` is exempt from recovery on purpose. It is not
# transient — the credentials are absent from the process environment and will
# still be absent on the next row — so it latches immediately and permanently.
METRON_REQUESTS_PER_MINUTE = 20.0
METRON_MAX_TRANSIENT_TRIPS = 3
# One full rate window, so the burst allowance has actually refilled before the
# next win asks. The client's own retry already slept once (capped at 60s) before
# tripping, so this is the SECOND wait, not the first.
METRON_TRANSIENT_COOLDOWN_SEC = 60.0


def _check_metron_degraded(metron: Any, metron_disabled: bool) -> bool:
    """BUI-255: trip the per-batch Metron breaker on throttle/timeout.

    ``metron.degraded`` is set by ``MetronClient``'s retry decorator (BUI-260,
    exhausted a single capped rate-limit retry; BUI-342, exhausted a single
    capped 5xx retry) or by a connection-error ``ApiError`` — i.e. Metron itself
    signaling "this call failed because I'm throttled/unreachable/erroring",
    never a genuine, exception-free "no match". Once tripped, the caller stops
    calling Metron for the rest of THIS win instead of repeating a
    capped-but-still-real sleep on every remaining lookup — a 44-row batch could
    otherwise stack dozens of 60s sleeps and wedge the single-worker server
    that runs this synchronously (see BUI-247's record-win incident).

    BUI-465 narrowed the blast radius from the batch to the win. This trip is
    transient by construction — throttled, unreachable, erroring — so
    ``cmd_collection_record_win`` now cools down and reopens the breaker rather
    than condemning every later win to a placeholder date and a null publisher.
    The "stop asking" protection above survives as a cap: after
    ``METRON_MAX_TRANSIENT_TRIPS`` trips the batch stops reopening it. A
    ``MetronCredentialError`` is still a permanent, immediate latch — it is the
    one cause that cannot clear itself between rows.

    ``is True`` (identity, not truthiness) so an unconfigured ``MagicMock``
    metron stub in a test — whose ``.degraded`` auto-vivifies as a truthy
    ``Mock`` — never falsely trips this without the test explicitly setting
    ``metron.degraded = True``.
    """
    if metron_disabled:
        return True
    if getattr(metron, "degraded", False) is True:
        logger.warning(
            "Metron throttled/unreachable/erroring; disabling for the rest of "
            "this record-win batch (remaining rows fall back to "
            "needs_manual_series_canonical)."
        )
        return True
    return False


def _metron_lookup_issue_cost(metron: Any, series_query: str, year: Any) -> int:
    """How many Metron HTTP requests the upcoming ``lookup_issue`` call will
    actually spend (BUI-473).

    Asks the real ``MetronClient``'s own ``lookup_issue_request_cost`` —
    which knows whether this (masthead-mapped) series+year has already been
    resolved and cached this batch (see ``MetronClient.resolve_series``) — so
    a run of N wins from the SAME series paces itself on what will really hit
    the network (``REQUESTS_ISSUE_IN_SERIES`` on a cache hit) rather than the
    flat, always-pessimistic ``REQUESTS_LOOKUP_ISSUE``.

    Must be called BEFORE the paired ``metron.lookup_issue(...)`` — the
    prediction reads the cache as it stands right now, and that call is what
    populates it for the next win.

    Falls back to ``REQUESTS_LOOKUP_ISSUE`` when ``metron`` doesn't expose
    this (any test double that isn't the real client). ``isinstance(..., int)``
    — not a truthiness/callability check — is the guard, for the same reason
    ``_check_metron_degraded`` above uses ``is True`` rather than truthiness:
    a bare, unconfigured ``MagicMock`` auto-vivifies ANY attribute access as a
    fresh, callable, truthy ``Mock``, so both ``hasattr`` and ``callable``
    would be satisfied by a stub that never actually costed anything — only a
    real ``int`` return proves the client really answered.
    """
    from locg.metron import REQUESTS_LOOKUP_ISSUE

    get_cost = getattr(metron, "lookup_issue_request_cost", None)
    if callable(get_cost):
        predicted = get_cost(series_query, year)
        if isinstance(predicted, int):
            return predicted
    return REQUESTS_LOOKUP_ISSUE


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


# BUI-426: editions LOCG nests INSIDE the parent series' full_title — "Annual"
# and "Treasury" — are stripped out of comic-identify's extracted series text
# (an annual's series is "Uncanny X-Men"; the qualifier survives only in the
# identity's `edition` field). The win pipeline then DROPPED `edition`, so a win
# like "Uncanny X-Men Annual 6" reached resolution as bare series "Uncanny
# X-Men" + issue "6", and the classic X-Men issue-number split (#<=141 ->
# The X-Men) filed it as the Silver-Age "The X-Men #6" (1964) — a completely
# different, often valuable book falsely claimed as owned (and the far cheaper
# annual hidden). Re-attaching the qualifier to the series BEFORE resolution
# restores the distinct identity: the norm_key becomes "uncanny x-men annual"
# (outside _XMEN_SPLIT_KEYS, so the #141 boundary never fires), base_full_title
# yields the canonical "Uncanny X-Men Annual #6", and the existing annual-aware
# alias/owned-match machinery (_alias_keys_for, owned_match_keys) resolves it to
# the right volume — or falls through to Metron/manual, the safe flagged
# direction, never a false regular-issue claim.
#
# Giant-Size and King-Size are deliberately NOT re-attached: comic-identify
# KEEPS those words in the series text because LOCG catalogs them as their OWN
# series (e.g. "Giant-Size X-Men" is distinct from "X-Men"), so they already
# resolve as distinct identities without help here.
_EDITION_QUALIFIERS: dict[str, str] = {"annual": "Annual", "treasury": "Treasury"}


def _edition_qualified_series(series_raw: str, edition: str) -> str:
    """Re-attach a stripped Annual/Treasury qualifier to the series text.

    Returns *series_raw* unchanged for any other edition (single-issue,
    giant-size, king-size, collected, ...) or an empty series. Idempotent: a
    manually-built identify_data may already carry the qualifier in the series
    (e.g. series="Uncanny X-Men Annual"), so the qualifier is appended only when
    it is not already present as a whole word.
    """
    qualifier = _EDITION_QUALIFIERS.get((edition or "").strip().lower())
    if not qualifier or not series_raw:
        return series_raw
    if re.search(rf"\b{re.escape(qualifier)}\b", series_raw, re.IGNORECASE):
        return series_raw
    return f"{series_raw} {qualifier}"


def _assert_independent_series_range(
    *, step2_resolved: bool, series_range: Optional[tuple[int, int]]
) -> None:
    """BUI-496 source-level canary for the anti-tautology invariant documented
    on :func:`_metron_release_date`.

    Both the BUI-486 ±1 ``cover_date`` exception and the BUI-464 non-clean-year
    era gate inside ``_metron_release_date`` depend on the ``series_range`` it
    is given having INDEPENDENT provenance — sourced from the local
    ``series_name_index`` (via :func:`series_year_range` /
    :func:`resolve_series_for_win`), never derived from the Metron hit being
    judged. ``step2_resolved`` is true exactly when the win's series was NOT
    found in ``series_name_index``, so Metron's own
    ``lookup_issue``/``format_series_name`` supplied ``canonical_series`` (the
    "R36 step-2" fallback) — on that path a ``series_range`` computed from
    that same hit would gate the candidate against itself and ALWAYS pass,
    silently turning both guards into dead code (see the production incident
    in ``_build_win_row``'s release_date comment: Infinity Gauntlet #1
    stamped 2022-09-14 from a 2022 reprint hit, BUI-488).

    This must never fire. If it does, a new caller or a refactor threaded a
    window derived from the judged hit into the step-2 path.
    """
    assert not (step2_resolved and series_range is not None), (
        "BUI-496: series_range must be None on the Metron step-2 path (series "
        "resolved from the very Metron hit being judged) — a non-None value "
        "here would make _metron_release_date's era gates tautological. See "
        "_metron_release_date's docstring for the independent-provenance "
        "contract."
    )


def _build_win_row(
    win: dict[str, Any],
    *,
    series_name_index: dict[str, str],
    volume_candidates: Any,
    existing_titles: set[str],
    owned_index: dict[tuple[str, str], list[dict[str, Any]]],
    metron: Any,
    metron_disabled: bool,
) -> dict[str, Any]:
    """Build one collection row for a single Gixen win.

    This is the per-win body of cmd_collection_record_win's per-chunk loop,
    lifted verbatim into a helper so the loop mutates outer counters/flags via
    the returned dict instead of directly. See cmd_collection_record_win's
    docstring for the overall R36/BUI-199/BUI-34/BUI-267/BUI-210/BUI-105
    resolution chain this implements.

    Returns a dict with:
      - "skipped": bool — True if the win matched an already-owned row (BUI-34/BUI-267)
      - "skip_detail": dict | None — present when skipped
      - "row": dict | None — the built collection row, present when not skipped
      - "metron_disabled": bool — updated metron_disabled flag to thread into the next call
      - "metron_credential_error": bool — the breaker tripped on missing credentials.
        BUI-465: the caller needs the CAUSE, not just the flag. A credential error is
        permanent for the process and must latch for the whole batch; a throttle/outage
        trip is transient and must not.
      - "metron_requests": int — HTTP requests this win spent against Metron's
        per-minute budget (BUI-465), weighted per lookup by the constants in
        ``locg.metron``. The caller paces the batch on this.
      - "metron_attempted" / "metron_succeeded" / "manual_series" / "manual_variant" /
        "variant_detail_attempted" / "variant_matches": int deltas for this win
    """
    from locg.metron import (
        REQUESTS_LOOKUP_ISSUE_DETAIL,
        MetronCredentialError,
    )

    metron_attempted = 0
    metron_succeeded = 0
    metron_requests = 0
    metron_credential_error = False
    manual_series = 0
    manual_variant = 0
    variant_detail_attempted = 0
    variant_matches = 0

    identify = win.get("identify_data") or {}
    series_raw = str(identify.get("series") or "").strip()
    # BUI-426: re-attach the Annual/Treasury qualifier that comic-identify
    # strips out of the series text, so an annual resolves as its DISTINCT
    # "<Series> Annual #N" identity rather than the same-numbered regular issue
    # in the wrong volume (a false ownership claim). No-op for regular issues
    # and for giant-size/king-size (whose qualifier already lives in the series
    # text), so genuine regular-issue resolution and the classic X-Men split are
    # untouched. See _edition_qualified_series.
    series_raw = _edition_qualified_series(series_raw, str(identify.get("edition") or ""))
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
    # BUI-465: has `lookup_issue(series_raw, issue_num, year_raw)` — this row's
    # ONE possible issue query — already been asked? The R36 step-2 resolution
    # below and the BUI-210 date-only lookup further down issue byte-identical
    # calls, so on a step-2 miss the second one re-asked a question Metron had
    # just answered "no" to and spent a quarter of the batch's request budget
    # doing it. Every pending win in the 2026-07-19 backlog was on that path.
    issue_lookup_done = False

    # BUI-199: resolve the canonical series by issue-number boundary
    # (the LOCG X-Men split) and by year/era (the right volume), not by
    # blindly taking the single series_name_index entry.
    resolved_series = resolve_series_for_win(
        norm_key, issue_num, year_raw, series_name_index, volume_candidates
    )
    # BUI-464: era evidence for a win comic-identify could not date at all.
    # Most eBay comic titles carry no year ("X-Men 109 - 1st Weapon Alpha
    # (Vindicator) VG/Fine Cond"), so `year_raw` is null and BOTH the BUI-210
    # date-only lookup and the BUI-105 placeholder stamp — each of which
    # demands a clean 4-digit year — are skipped, shipping the row dateless.
    # But when the win resolved through `series_name_index`, the LOCG canonical
    # name it matched carries the volume's own publication window, and THAT is
    # era evidence the win itself lacks. It is independent of anything Metron
    # is about to say, so it can gate a Metron hit (see _metron_release_date).
    # Captured only on the index path: the Metron step-2 fallback below derives
    # its series name from the very hit we would be judging, so its range would
    # be circular and is deliberately left None.
    index_series_range: Optional[tuple[int, int]] = None
    if resolved_series is not None:
        canonical_series = resolved_series
        # BUI-464 + BUI-486: thread the resolved volume's own publication window
        # whenever the win resolved through series_name_index, for BOTH gates in
        # _metron_release_date. When year_raw is not a clean 4-digit year it is
        # the sole era guard (BUI-464). When year_raw IS a clean year it is the
        # containment gate for the cover_date ±1 exception (BUI-486) — where
        # year_raw may be a seller ERA claim skewed one year off the printed
        # cover date, so the clean year no longer captures the window's job on
        # its own. (Formerly captured only on the non-clean branch, when the two
        # gates were mutually exclusive; BUI-486 makes them cooperate on the
        # clean-year cover_date path.) Captured ONLY on the index path: the
        # Metron step-2 fallback below derives its series name from the very hit
        # we would be judging, so its range would be circular and stays None.
        index_series_range = series_year_range(resolved_series)
    elif not metron_disabled:
        try:
            metron_attempted += 1
            # BUI-473: predicted BEFORE the call — it reads the series cache
            # as it stands right now, and lookup_issue is what populates it
            # for the next win of this series.
            lookup_cost = _metron_lookup_issue_cost(metron, series_raw, year_raw)
            metron_data = metron.lookup_issue(series_raw, issue_num, year_raw)
            issue_lookup_done = True
            if metron_data:
                metron_succeeded += 1
                canonical_series = metron.format_series_name(metron_data)
        except MetronCredentialError:
            metron_disabled = True
            metron_credential_error = True
            logger.warning("Metron credentials not configured; falling back to manual series resolution.")
        else:
            # BUI-465: charged in the no-exception branch only. A
            # MetronCredentialError is raised by `_get_session()` before any HTTP
            # request leaves the process, so charging it would make the batch pace
            # itself against traffic Metron never saw.
            # BUI-473: `lookup_cost` (not the flat REQUESTS_LOOKUP_ISSUE) so a
            # series already resolved earlier in this batch is charged only
            # for the issue lookup it actually spent.
            metron_requests += lookup_cost
            metron_disabled = _check_metron_degraded(metron, metron_disabled)

    # BUI-496: index_series_range is fixed from here on (set at most once,
    # above) and is what every downstream _metron_release_date call in this
    # function (the BUI-210 date-only lookup, the step-2 hit re-check, and the
    # final release_date assignment) receives. Check the anti-tautology
    # invariant exactly once, here, rather than at each call site.
    _assert_independent_series_range(
        step2_resolved=issue_lookup_done, series_range=index_series_range
    )

    if canonical_series is None:
        canonical_series = series_raw
        needs_manual_series = True
        manual_series += 1

    # BUI-34: skip wins already owned in the cache (series + issue),
    # before any variant lookup or row construction.
    #
    # BUI-267: the bare (series, issue) key collides across unrelated
    # volumes/eras (New Gods #7 1971 vs an owned Vol. 5 2024 #7) and
    # across distinct print editions (a base win vs an owned Newsstand
    # copy), so a key collision alone is no longer sufficient — each
    # candidate owned row must also be era- and edition-compatible.
    # Cosmetic cover variants (e.g. "Capullo Variant") stay ignored,
    # matching the original BUI-34 behavior.
    owned_candidates = owned_index.get((
        _normalize_series_key(canonical_series),
        normalize_issue_key(issue_num),
    ), []) if issue_num else []
    matched_owned_row: Optional[dict[str, Any]] = None
    if owned_candidates:
        win_year = _coerce_year(year_raw)
        for candidate_row in owned_candidates:
            candidate_suffix = _owned_row_variant_suffix(candidate_row.get("full_title") or "")
            if _dedup_era_compatible(win_year, candidate_row) and _dedup_variant_compatible(
                variant_text, candidate_suffix
            ):
                matched_owned_row = candidate_row
                break
    if matched_owned_row is not None:
        return {
            "skipped": True,
            "skip_detail": {
                "win": f"{canonical_series} #{issue_num}",
                "matched_series_name": matched_owned_row.get("series_name"),
                "matched_release_date": matched_owned_row.get("release_date"),
            },
            "row": None,
            "metron_disabled": metron_disabled,
            "metron_credential_error": metron_credential_error,
            "metron_requests": metron_requests,
            "metron_attempted": metron_attempted,
            "metron_succeeded": metron_succeeded,
            "manual_series": manual_series,
            "manual_variant": manual_variant,
            "variant_detail_attempted": variant_detail_attempted,
            "variant_matches": variant_matches,
        }

    # BUI-210: when the series resolved via series_name_index (the common
    # case — metron_data stays None), we still have no release_date, so
    # the row would ship dateless, and an all-dateless batch hangs LOCG's
    # importer at 0%. Do a Metron *issue* lookup purely to populate a real
    # date; do NOT touch canonical_series (the index-resolved value is more
    # reliable than Metron's format_series_name here). Runs after the BUI-34
    # dedup continue so we never spend a Metron call on a skipped
    # already-owned win, and before the variant block so variant resolution
    # can reuse metron_id.
    #
    # The accept test is _metron_release_date itself — the SAME call that
    # later picks the stored date — so a hit taken here is accepted if and
    # only if it yields a date this row will actually keep. See that helper
    # for the reprint guard and why its year window is ±1 rather than exact
    # (BUI-210 reopen: the exact match discarded genuine on-sale dates for
    # January-cover books).
    #
    # On THIS path a rejected hit drops the whole metron_data, not just its
    # date — the id, series and (BUI-458) publisher all came from the same
    # possibly-wrong issue match.
    #
    # BUI-465: skipped when the R36 step-2 lookup above already issued this exact
    # query. It only reaches here having produced no metron_data, and the query
    # is byte-identical, so re-asking spends two more requests to be told "no" a
    # second time. (Step-2 doesn't run at all on the common index-resolved path,
    # which is the path this block exists to serve.)
    #
    # BUI-464: a null year no longer blocks this lookup outright — it blocks it
    # only when there is no era evidence at all. `index_series_range` is that
    # evidence when the series resolved through series_name_index, and it gates
    # the hit in the clean year's place, so the "no era guard whatsoever"
    # trap this block's year requirement existed to avoid never opens. The
    # request weight is charged in the same place as before, so BUI-465's
    # pacing already covers the extra lookups this admits.
    if metron_data is None and issue_num and not metron_disabled and not issue_lookup_done:
        year_str = str(year_raw).strip() if year_raw is not None else ""
        if re.fullmatch(r"\d{4}", year_str) or index_series_range is not None:
            try:
                metron_attempted += 1
                # BUI-473: predicted BEFORE the call, same as the R36 step-2
                # lookup above — a series already resolved earlier in this
                # batch (by this lookup or step-2's, same cache) costs only
                # the issue half here.
                lookup_cost = _metron_lookup_issue_cost(metron, series_raw, year_raw)
                looked_up = metron.lookup_issue(series_raw, issue_num, year_raw)
                if looked_up and _metron_release_date(looked_up, year_raw, index_series_range) is not None:
                    metron_succeeded += 1
                    metron_data = looked_up
            except MetronCredentialError:
                metron_disabled = True
                metron_credential_error = True
                logger.warning(
                    "Metron credentials not configured; falling back to placeholder release date."
                )
            else:
                metron_requests += lookup_cost
                metron_disabled = _check_metron_degraded(metron, metron_disabled)

    # BUI-467: the R36 step-2 lookup above (used when series_name_index has
    # no entry) assigns metron_data BEFORE any date is judged, unlike the
    # BUI-210 date-only lookup just above — which only ever keeps a hit that
    # has already passed this exact guard, so re-testing it here is a no-op
    # for that path. For a step-2 hit, though, the guard rejecting a REAL
    # candidate date is positive evidence the hit is the WRONG issue
    # (reprint / collected edition), so treat it the same way the BUI-210
    # path treats a rejection: drop metron_data entirely rather than let a
    # wrong-issue hit go on to donate its metron_id, publisher, and variant
    # list to the row. A reprint under a different imprint would otherwise
    # import a wrong publisher to LOCG silently, whereas a null publisher
    # trips audit-pending's "no publisher" backstop first (the BUI-458
    # principle: a loud null beats a quiet wrong value). canonical_series is
    # a separate variable captured earlier and is NOT reverted — series
    # resolution is the one thing the step-2 path exists to produce, and
    # BUI-199 volume boundaries mean it can be right even when the specific
    # issue hit was a reprint.
    #
    # Gated on has_candidate_date so this only fires on an ACTUAL rejection
    # (a store_date/cover_date existed and disagreed with the win's year),
    # never on a plain Metron miss on dates (R66: a hit that simply has no
    # date at all is not evidence of a wrong issue — it stays and the row
    # ships dateless, same as before).
    has_candidate_date = bool(metron_data and (metron_data.get("store_date") or metron_data.get("cover_date")))
    if (
        metron_data is not None
        and has_candidate_date
        and _metron_release_date(metron_data, year_raw, index_series_range) is None
    ):
        metron_data = None

    # BUI-458: capture the issue's publisher (e.g. "Marvel Comics") from
    # Metron's full-issue detail so the win row carries a real publisher_name
    # instead of null. The lightweight lookup_issue (series_list/issues_list)
    # carries NO publisher, so this full-issue detail fetch is the only place
    # it is available — one fetch per win that resolved a metron_id, reused by
    # the variant block below so a variant win still makes just one detail
    # call. Runs after the BUI-34 dedup return above, so a skipped
    # already-owned win never spends a Metron call on this.
    #
    # Data safety: on any Metron miss / None publisher / network error the
    # publisher stays null (never a fabricated or defaulted value). A missing
    # publisher fails the pre-upload audit (audit-pending "no publisher"),
    # which is the intended backstop, whereas a wrong publisher would import
    # silently to LOCG.
    publisher_name: Optional[str] = None
    issue_detail: Optional[dict[str, Any]] = None
    detail_metron_id = metron_data.get("metron_id") if metron_data else None
    if detail_metron_id is not None and not metron_disabled:
        try:
            issue_detail = metron.lookup_issue_detail(detail_metron_id)
            if issue_detail:
                publisher_name = issue_detail.get("publisher") or None
        except MetronCredentialError:
            metron_disabled = True
            metron_credential_error = True
            logger.warning(
                "Metron credentials not configured; skipping publisher/variant resolution."
            )
        else:
            metron_requests += REQUESTS_LOOKUP_ISSUE_DETAIL
            metron_disabled = _check_metron_degraded(metron, metron_disabled)

    # R32: variant handling
    needs_manual_variant = False
    # BUI-199 Cause 1: full_title must use the BASE series name, not the
    # decorated canonical_series. LOCG's full_title carries no
    # "(Vol. N) (YYYY - YYYY)" decoration (e.g. "Fantastic Four #72",
    # not "Fantastic Four (Vol. 3) (1997 - 2012) #72"), so a decorated
    # full_title is unmatchable by LOCG Bulk Import. The stored
    # series_name stays decorated/unchanged.
    base_title = base_full_title(canonical_series, issue_num or None)
    if variant_text:
        # BUI-373: an explicit printing ordinal ("2nd Ptg", "Third Printing", …)
        # is recognized by the shared detector first, so this canonicalizes the
        # SAME spellings the dedup guard above and the collection-check
        # printing_conflict probe recognize — no second, narrower lexicon.
        # VARIANT_SUFFIX_MAP is still consulted for non-printing suffixes
        # (Newsstand/Direct/Facsimile); an ambiguous bare "Reprint" (no
        # ordinal) is deliberately left to the Metron/manual-variant path
        # below rather than guessing a specific printing number.
        suffix = _printing_marker_suffix(variant_text) or VARIANT_SUFFIX_MAP.get(variant_text)
        if suffix:
            full_title = f"{base_title} {suffix}"
        else:
            # BUI-33: Metron variant resolution. The lightweight
            # lookup_issue has no variants, so the full-issue detail already
            # fetched above (BUI-458) is reused to fuzzy-match the auction
            # variant text against Metron's variant cover names — no second
            # network call. (LOCG title-search fallback is dead per the
            # local-first pivot, ADR 0001 / BUI-25.)
            matched_variant: Optional[str] = None
            if detail_metron_id is not None:
                # A metron_id resolved, so a variant lookup was attempted
                # (the detail fetch is shared with publisher capture above).
                variant_detail_attempted += 1
                if issue_detail is not None:
                    matched_variant = _fuzzy_variant_match(
                        variant_text, issue_detail.get("variants") or []
                    )

            if matched_variant:
                full_title = f"{base_title} {matched_variant}"
                variant_matches += 1
            elif base_title in existing_titles:
                # Base issue already owned — attach to the canonical entry.
                full_title = base_title
            else:
                full_title = base_title
                needs_manual_variant = True
                manual_variant += 1
    else:
        full_title = base_title

    # release_date: the one trustworthy Metron date, or nothing.
    #
    # The BUI-268 reprint guard lives inside _metron_release_date, which is
    # also what the BUI-210 date-only lookup above used to accept this hit —
    # one definition, so the accepted date and the stored date are the same
    # date by construction. (This metron_data can also come from the FIRST
    # Metron call — the R36 step-2 series-resolution path, used when
    # series_name_index has no entry — which has no year filter of its own.
    # Left unguarded, a reprint store_date got written onto an
    # otherwise-correct row: Infinity Gauntlet #1 stamped 2022-09-14 from a
    # 2022 reprint hit, which then made a year-gated collection-check reject
    # the genuinely-owned 1991 copy as a mismatched era.)
    #
    release_date: Optional[str] = _metron_release_date(metron_data, year_raw, index_series_range)

    # BUI-105: when no Metron data backs this win (the series_name_index
    # path, or the bare-series manual fallback), there is no Metron date,
    # so the row would be written dateless and miss a year-gated
    # collection-check. Stamp a best-effort release_date from the identify
    # year (Jan 1 — year precision is all the year gate in _match_owned_issue
    # needs) so a just-won book reads as in-collection. A Metron hit that
    # simply lacks dates stays blank (R66) — the relaxed year gate already
    # lets that row match.
    #
    # DO NOT REMOVE THIS. BUI-210's reopen asked for it ("Metron misses
    # degrade gracefully to blank — never a placeholder"), on the premise
    # that the placeholder is what ships rows dateless to LOCG. That premise
    # is false, and removing the stamp was implemented, reviewed, and
    # reverted. Three findings, each reproduced against this code:
    #
    #   * It changes NOTHING about the export. _row_to_csv_dict already
    #     blanks the placeholder (_is_placeholder_release_date), so a
    #     placeholder row and a dateless row emit the same empty Release
    #     Date. Dateless rows come from Metron misses, not from this stamp.
    #   * It silently DELETES wins. The year comparison in _reconcile_score
    #     is the only discriminator left for two volumes of one masthead
    #     ("The X-Men (1963 - 1981)" vs "X-Men (1991 - 2011)" normalize to
    #     the same key and neither carries a "(Vol. N)"). Dateless, that
    #     comparison fails OPEN, the wrong-volume candidate scores a match,
    #     and _reconcile_phase's BUI-122 collision guard auto-heals the
    #     pending win away — no warning. A win stuck pending is recoverable;
    #     a win dropped on import is not.
    #   * It buys duplicates. _match_owned_issue's alias/cross-masthead pass
    #     (require_dated, BUI-197) rejects a dateless owned row outright, so
    #     a check for "Hulk #181" stops finding the just-won "The Incredible
    #     Hulk #181" and reports not_in_cache — the R11 failure direction.
    #     The exact-key pass is not a fallback here; it is exactly what has
    #     already failed when the alias pass runs.
    #
    # The year in this stamp is real (it is the identified cover year); only
    # its month/day are fabricated, which is why the export drops it and the
    # era guards keep it. Making a miss legible without losing the year needs
    # a separate provenance field plus the four guards above taught to read
    # it — not this deletion.
    if release_date is None and metron_data is None and year_raw is not None:
        year_str = str(year_raw).strip()
        if re.fullmatch(r"\d{4}", year_str):
            release_date = f"{year_str}-01-01"

    row: dict[str, Any] = {
        # BUI-458: real publisher from Metron's full-issue detail (null on any
        # Metron miss — never a fabricated guess).
        "publisher_name": publisher_name,
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
        # BUI-465: WHY this row lacks Metron data — "we never asked" vs "we asked
        # and missed". True only when something Metron-sourced is actually absent
        # AND the breaker was off/down at the time, so a row that resolved fully
        # despite a late trip is not falsely flagged. `audit-pending` and the
        # BUI-461 backfill can then tell a retryable never-asked row apart from a
        # genuine Metron miss instead of re-deriving it forensically from the
        # ordering of `local_added_seq` (which is how BUI-465 was diagnosed at
        # all). Absent on rows written before BUI-465, which reads as "unknown".
        "metron_unavailable": bool(metron_disabled)
        and (metron_data is None or publisher_name is None),
    }

    return {
        "skipped": False,
        "skip_detail": None,
        "row": row,
        "metron_disabled": metron_disabled,
        "metron_credential_error": metron_credential_error,
        "metron_requests": metron_requests,
        "metron_attempted": metron_attempted,
        "metron_succeeded": metron_succeeded,
        "manual_series": manual_series,
        "manual_variant": manual_variant,
        "variant_detail_attempted": variant_detail_attempted,
        "variant_matches": variant_matches,
    }


def cmd_collection_record_win(
    wins: list[dict[str, Any]],
    cache: Optional[Any] = None,
    metron: Optional[Any] = None,
    requests_per_minute: float = METRON_REQUESTS_PER_MINUTE,
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
    rolls back the in-flight chunk.  Returns summary metrics, including the
    BUI-367 rows_written/rows_inserted/rows_overwritten split: rows_written is
    every row submitted to write_wins, while rows_inserted counts only the
    ones that added a genuinely new row (rows_overwritten replaced an existing
    row sharing its gixen_item_id and added none).

    ``requests_per_minute`` is the BUI-465 pacing budget (default: Metron's
    documented 20/min burst); ``0`` disables pacing, which is only appropriate
    for tests and for a caller that has already metered the traffic itself.
    The sleeps are real, so a long batch is slow on purpose — the record-win
    endpoint runs this off the event loop via ``asyncio.to_thread`` (BUI-428)
    precisely so a Metron wait cannot stall the single-worker comics server.
    See ``METRON_REQUESTS_PER_MINUTE`` for the pace/recover rationale.

    BUI-476: refuses with ``{"status": "explicit_store_required"}`` when no
    ``cache`` is passed and ``LOCG_DATA_DIR`` is unset — see
    :func:`_needs_explicit_store`. The refusal is returned BEFORE any Metron
    traffic or any write, and it is the ONLY return shape that carries a
    ``status`` key, so a caller can tell it apart from a real run's metrics
    without inspecting counters. The comics server cannot reach it:
    ``routes.api_record_win_commit`` calls ``_ensure_collection_store()``
    first, which guarantees ``LOCG_DATA_DIR`` is set.
    """
    from datetime import datetime, timezone
    from locg.collection_cache import (
        CollectionCache,
        _normalize_series_key,
        build_volume_candidates,
    )
    from locg.metron import MetronClient

    # BUI-476: refuse before spending a single Metron call or touching a store
    # the caller never named. record-win WRITES new owned rows, so the
    # wrong-store outcome here is silently seeding a local cache with wins the
    # real collection never learns about — and (worse) the operator believing
    # they were recorded.
    if _needs_explicit_store(cache):
        # The suggested line must be one argparse actually accepts: `record-win`
        # has no positional path, only --from-gixen-json.
        return _explicit_store_required_error(
            "locg collection record-win --from-gixen-json <path>"
        )
    if cache is None:
        cache = CollectionCache()
    if metron is None:
        metron = MetronClient()

    payload = cache.load()
    series_name_index: dict[str, str] = payload.get("series_name_index", {})
    # BUI-199: a one-to-one series_name_index collapses every volume of a series
    # onto whichever one was indexed last. Build the full per-key volume map so a
    # year-bearing win can be matched to the volume whose range contains its era.
    volume_candidates = build_volume_candidates(payload)
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
    # direction — so we intentionally do NOT broaden the key here. The token key
    # is normalize_issue_key from locg.parsing (BUI-189, shared with the probe).
    #
    # BUI-267: keyed to a LIST of owned rows (not a bare presence set) so the
    # dedup check below can compare era (series_name/release_date year) and
    # print edition (Newsstand/Direct suffix) before treating a bare (series,
    # issue) collision as a genuine duplicate — an unrelated same-numbered
    # issue from another volume/era, or the opposite print edition, must not
    # silently swallow a genuinely-new win.
    owned_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in payload.get("comics", []):
        if not r.get("in_collection"):
            continue
        prefix, token = _split_full_title(r.get("full_title") or "")
        if token is None:
            continue
        key = (_normalize_series_key(prefix), normalize_issue_key(token))
        owned_index.setdefault(key, []).append(r)

    rows_written = 0
    rows_inserted = 0
    rows_overwritten = 0
    chunks_committed = 0
    skipped_already_owned = 0
    skipped_already_owned_titles: list[str] = []
    # BUI-267: which owned row each skip matched (series_name + release year),
    # so a caller can catch a cross-era/variant false match instead of trusting
    # a bare skip count.
    skipped_already_owned_detail: list[dict[str, Any]] = []
    manual_variant_count = 0
    manual_series_count = 0
    metron_lookups_attempted = 0
    metron_lookups_succeeded = 0
    metron_variant_lookups_attempted = 0
    metron_variant_matches = 0
    partial_failure = False
    # BUI-465: three distinct pieces of Metron state, where there used to be one
    # bool doing all three jobs.
    #   metron_disabled       — the breaker as _build_win_row sees it, now RESET
    #                           after a recovered transient trip.
    #   credentials_missing   — permanent; once set the breaker never reopens.
    #   transient_trips       — how many throttle/outage trips this batch has
    #                           already forgiven, capped at
    #                           METRON_MAX_TRANSIENT_TRIPS.
    metron_disabled = False
    metron_credentials_missing = False
    metron_transient_trips = 0
    metron_requests_spent = 0
    metron_unavailable_rows = 0
    # Requests spent on the previous win, i.e. what the next win must pay for
    # before it may ask Metron anything. Charged BEFORE the win rather than
    # after, so a batch whose last win needed Metron does not end on a pointless
    # sleep.
    pending_paced_requests = 0
    # A non-positive budget means "don't pace" (0 opts out; a negative value is
    # nonsense and must not become a negative sleep).
    seconds_per_request = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0

    chunks = _chunked(wins, RECORD_WIN_CHUNK_SIZE)

    for chunk in chunks:
        built_rows: list[dict[str, Any]] = []
        chunk_manual_variant = 0
        chunk_manual_series = 0
        chunk_metron_attempted = 0
        chunk_metron_succeeded = 0
        chunk_variant_detail_attempted = 0
        chunk_variant_matches = 0

        for win in chunk:
            # Pace: pay for the previous win's Metron traffic before spending any
            # more. Skipped while the breaker is shut, because a shut breaker
            # spends nothing — the only wait a disabled row deserves is the
            # cooldown below.
            if pending_paced_requests and seconds_per_request and not metron_disabled:
                time.sleep(seconds_per_request * pending_paced_requests)
            pending_paced_requests = 0

            breaker_open_before = metron_disabled
            result = _build_win_row(
                win,
                series_name_index=series_name_index,
                volume_candidates=volume_candidates,
                existing_titles=existing_titles,
                owned_index=owned_index,
                metron=metron,
                metron_disabled=metron_disabled,
            )
            metron_disabled = result["metron_disabled"]
            pending_paced_requests = result["metron_requests"]
            metron_requests_spent += result["metron_requests"]
            if result["metron_credential_error"]:
                metron_credentials_missing = True
            if (result["row"] or {}).get("metron_unavailable"):
                metron_unavailable_rows += 1

            # BUI-465: a transient trip must not silently downgrade the rest of
            # the batch. Wait out one rate window and reopen the breaker, up to
            # METRON_MAX_TRANSIENT_TRIPS times; past that, treat Metron as
            # genuinely down and keep it shut (BUI-255). A credential error is
            # never forgiven — it cannot fix itself between rows.
            #
            # Gated on the breaker having been CLOSED before this win, so this
            # runs once per trip: an exhausted budget leaves the breaker open, and
            # on every later win `breaker_open_before` is then True, which both
            # silences the log and stops the trip counter from running away.
            if metron_disabled and not breaker_open_before and not metron_credentials_missing:
                if metron_transient_trips < METRON_MAX_TRANSIENT_TRIPS:
                    metron_transient_trips += 1
                    logger.warning(
                        "Metron breaker tripped mid-batch (trip %d/%d); cooling down "
                        "%.0fs and retrying rather than downgrading the remaining wins.",
                        metron_transient_trips, METRON_MAX_TRANSIENT_TRIPS,
                        METRON_TRANSIENT_COOLDOWN_SEC,
                    )
                    time.sleep(METRON_TRANSIENT_COOLDOWN_SEC)
                    metron_disabled = False
                    # The cooldown is a whole rate window — far more than the pace
                    # debt this win ran up — so the next win owes nothing further.
                    pending_paced_requests = 0
                else:
                    logger.warning(
                        "Metron tripped %d times in this batch; leaving it disabled for "
                        "the remaining wins, which record without a publisher or a real "
                        "release date (metron_unavailable=True on each such row).",
                        metron_transient_trips,
                    )

            chunk_metron_attempted += result["metron_attempted"]
            chunk_metron_succeeded += result["metron_succeeded"]
            chunk_manual_series += result["manual_series"]
            chunk_manual_variant += result["manual_variant"]
            chunk_variant_detail_attempted += result["variant_detail_attempted"]
            chunk_variant_matches += result["variant_matches"]

            if result["skipped"]:
                skipped_already_owned += 1
                skipped_already_owned_titles.append(result["skip_detail"]["win"])
                skipped_already_owned_detail.append(result["skip_detail"])
                continue

            built_rows.append(result["row"])

        try:
            write_result = cache.write_wins(built_rows, command="record-win")
            rows_written += len(built_rows)
            rows_inserted += write_result.inserted
            rows_overwritten += write_result.overwritten
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
        # BUI-367: rows_written counts every row SUBMITTED to write_wins, which
        # over-reports "new rows added" whenever a duplicate gixen_item_id (vs
        # the store, or intra-batch per BUI-356) overwrites an existing row in
        # place instead of appending. rows_inserted/rows_overwritten are the
        # accurate split; rows_inserted + rows_overwritten == rows_written.
        # Additive fields (rows_written is unchanged) so existing consumers
        # (the overlay's record-win endpoint, the /comic:collection-add skill)
        # keep working without modification.
        "rows_inserted": rows_inserted,
        "rows_overwritten": rows_overwritten,
        "chunks_committed": chunks_committed,
        "skipped_already_owned": skipped_already_owned,
        "skipped_already_owned_titles": skipped_already_owned_titles,
        "skipped_already_owned_detail": skipped_already_owned_detail,
        "manual_variant_count": manual_variant_count,
        "manual_series_count": manual_series_count,
        "metron_lookups_attempted": metron_lookups_attempted,
        "metron_lookups_succeeded": metron_lookups_succeeded,
        "metron_variant_lookups_attempted": metron_variant_lookups_attempted,
        "metron_variant_matches": metron_variant_matches,
        # BUI-465: batch-level Metron health, so a downgraded run is visible in
        # the response instead of only in the server log. `metron_degraded` uses
        # the same key name cmd_collection_backfill already reports.
        "metron_degraded": metron_disabled,
        "metron_credentials_missing": metron_credentials_missing,
        "metron_transient_trips": metron_transient_trips,
        "metron_requests_spent": metron_requests_spent,
        "metron_unavailable_rows": metron_unavailable_rows,
        "partial_failure": partial_failure,
    }


def cmd_collection_doctor() -> dict[str, Any]:
    """Return first-run walkthrough and current cache status.

    Runs the same checks as status and explains the next remediation (F2).
    """
    status = cmd_collection_status(verbose=False)

    steps = [
        {
            # BUI-476: an EXPORT, not a one-shot `VAR=val cmd` prefix. The
            # prefix form scopes to a single command, which would import into
            # the server store and then verify the DEFAULT one — the walkthrough
            # would report "cache is empty" straight after a successful import
            # and loop forever. Exporting once makes every later step (import,
            # status, and anything the operator runs next) name the same store.
            "step": 1,
            "title": "Point the CLI at your collection store",
            "instruction": (
                f"Run: {SERVER_STORE_EXPORT_HINT}  "
                "(the comics server on the Mac Mini owns this store; the "
                "mutating commands refuse to guess it. Skip only if you "
                "deliberately mean a different, local store.)"
            ),
        },
        {
            "step": 2,
            "title": "Export your collection from LOCG",
            "instruction": (
                "Go to https://leagueofcomicgeeks.com/profile (logged in) "
                "and click 'Export My Comics'. An Excel file will download to ~/Downloads."
            ),
        },
        {
            "step": 3,
            "title": "Import the Excel file",
            "instruction": (
                "Run: locg collection import "
                "~/Downloads/<ComicGeeks-YYYY-MM-DD-HH-MM-SS>.xlsx"
            ),
        },
        {
            "step": 4,
            "title": "Verify the import",
            "instruction": "Run: locg collection status --pretty",
        },
        {
            "step": 5,
            "title": "(Optional) Set up Metron credentials for series name resolution",
            "instruction": (
                "Add METRON_USERNAME and METRON_PASSWORD to ~/.config/locg/.env "
                "to enable automatic canonical series name lookup via Metron API."
            ),
        },
    ]

    if status["last_full_import"] is None:
        next_action = (
            "Cache is empty. Start with Step 1: point the CLI at your "
            "collection store — an empty cache here often just means "
            "LOCG_DATA_DIR is unset and this read resolved a DIFFERENT store "
            "than the one you imported into (BUI-476)."
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


# --- BUI-427: matcher-bypassing remediation (delete-by-identity, set-copies) ---
#
# The BUI-254 DELETE endpoint locates its target row via cmd_collection_check
# — the SAME masthead-alias / X-Men-split / leading-article matcher that
# breaks on the exact class of bug this exists to fix: a volume-mis-filed row.
# That matcher can resolve to the WRONG row (a correct twin at the same
# series/issue), or refuse a legitimate deletion outright with
# ambiguous_cross_volume -> 404. BUI-424's remediation had to hand-roll a
# one-off CollectionCache.apply script keyed on gixen_item_id to work around
# both failure modes.
#
# _stable_identity_candidates below is the single shared resolver both
# cmd_collection_remediate_delete and cmd_collection_remediate_set_copies use
# — it NEVER falls back to cmd_collection_check or any of its normalization.
# It supports exactly two identity modes (never combined, never a fuzzy
# fallback):
#   1. gixen_item_id — the stable id stamped on a row at record-win time
#      (the BUI-367 write_wins dedup key).
#   2. full_title + release_date + source, matched EXACTLY on all three
#      (missing release_date/source match a null/empty field on the row,
#      never wildcarded) — source is what disambiguates the BUI-424
#      "duplicate-twin" case, where a buggy agent_win row and its clean
#      locg_export re-resolution share the same full_title + release_date.


def _validate_remediation_identity(
    gixen_item_id: Optional[str], full_title: Optional[str]
) -> Optional[str]:
    """None if exactly one identity mode is selected; else an error message."""
    has_id = bool((gixen_item_id or "").strip())
    has_title = bool((full_title or "").strip())
    if has_id == has_title:
        return (
            "supply EXACTLY ONE identity: gixen_item_id, OR full_title "
            "(+release_date, +source)"
        )
    return None


def _stable_identity_candidates(
    comics: list[dict[str, Any]],
    *,
    gixen_item_id: Optional[str],
    full_title: Optional[str],
    release_date: Optional[str],
    source: Optional[str],
) -> list[int]:
    """Row indices matching a STABLE identity — never a fuzzy/masthead matcher.

    Assumes the caller already validated exactly one identity mode is
    selected (:func:`_validate_remediation_identity`); this is called again
    UNDER LOCK by both remediation ops' `_mutate` closures with the same
    already-validated args, purely to re-resolve row indices against the
    freshly-loaded payload — never to re-decide which mode applies.

    Returns EVERY matching index (not just the first) so the caller can
    treat more than one match as an unresolvable ambiguity rather than
    silently guessing via first-match.
    """
    gixen_item_id = (gixen_item_id or "").strip() or None
    if gixen_item_id:
        return [i for i, row in enumerate(comics) if row.get("gixen_item_id") == gixen_item_id]
    full_title = (full_title or "").strip() or None
    norm_release_date = release_date or None
    norm_source = source or None
    return [
        i for i, row in enumerate(comics)
        if row.get("full_title") == full_title
        and (row.get("release_date") or None) == norm_release_date
        and (row.get("source") or None) == norm_source
    ]


def _identity_match_error(candidates: list[int], verb: str) -> dict[str, Any]:
    """Shared not_found/ambiguous shape for both remediation ops."""
    if not candidates:
        return {
            "status": "not_found",
            "error": f"no row matches the given identity — nothing to {verb}",
        }
    return {
        "status": "ambiguous",
        "count": len(candidates),
        "error": (
            f"{len(candidates)} rows match the given identity — refusing to "
            f"guess which one to {verb}"
        ),
    }


def _not_imported_error() -> dict[str, Any]:
    """Shared R11 not_imported shape for both remediation ops."""
    return {
        "status": "not_imported",
        "error": "collection store has no import yet — cannot remediate (R11)",
    }


def _decrement_or_remove(copies: int) -> tuple[bool, int]:
    """BUI-249/250/251 copies-owned semantics (mirrors BUI-254's
    `api_collection_delete`): a row with more than one copy DECREMENTS,
    a single-copy row is REMOVED outright. Returns (decrements, remaining) —
    shared by the dry-run preview and the locked mutate so both derive the
    same decision from one place instead of re-deriving it independently.
    """
    return (True, copies - 1) if copies > 1 else (False, 0)


def cmd_collection_remediate_delete(
    *,
    gixen_item_id: Optional[str] = None,
    full_title: Optional[str] = None,
    release_date: Optional[str] = None,
    source: Optional[str] = None,
    dry_run: bool = False,
    cache: Optional[CollectionCache] = None,
) -> dict[str, Any]:
    """Delete (or decrement) ONE collection row by STABLE IDENTITY (BUI-427).

    Matcher-BYPASSING remediation for a volume-mis-filed row: locates the
    target by `gixen_item_id` OR by (`full_title`, `release_date`, `source`)
    — see :func:`_stable_identity_candidates` — NEVER via
    `cmd_collection_check`'s masthead-alias / X-Men-split / leading-article
    matcher, which is exactly what can't disambiguate a mis-file (the BUI-424
    case this replaces a one-off script for).

    Same copies-owned semantics as the BUI-254 endpoint: a row with
    `in_collection > 1` is decremented; a single-copy row is removed outright.
    `dry_run=True` previews the op without mutating (no lock, no audit entry).
    The real delete re-resolves the identity INSIDE `cache.apply()`'s
    exclusive lock and self-verifies exactly one row still matches at that
    point — a TOCTOU-safe guard against the store changing shape between the
    cheap pre-check and the lock (mirrors the BUI-254/BUI-417 pattern).

    Returns `{"status": "invalid_request", "error"}` for a malformed identity
    (neither or both modes given). `{"status": "not_imported", "error"}` if
    the store has never been imported (R11). `{"status": "not_found"}` when
    nothing matches — a true no-op, never a silent wrong-row touch.
    `{"status": "ambiguous", "count"}` when more than one row matches — a
    data-quality problem surfaced rather than guessed at. On success:
    `{"status": "ok"|"preview", "action": "removed"|"decremented"|
    "would_remove"|"would_decrement", "removed"|"row", "remaining_copies"}`.

    BUI-489: refuses with `{"status": "explicit_store_required"}` when no
    `cache` is passed and `LOCG_DATA_DIR` is unset/unexpanded — see
    `_needs_explicit_store`. Checked BEFORE `dry_run` is even consulted (same
    shape as `cmd_collection_backfill`'s guard): a `dry_run=True` preview
    against the WRONG store would look believable and confirm a delete that,
    run for real, lands on a different collection entirely. BUI-424 flagged
    this command as the single highest-risk case in the module — a wrong-store
    delete on a volume-mis-filed row can't be told apart from a correct one
    without independently checking which store answered.
    """
    error = _validate_remediation_identity(gixen_item_id, full_title)
    if error:
        return {"status": "invalid_request", "error": error}

    if _needs_explicit_store(cache):
        return _explicit_store_required_error(
            "locg collection remediate-delete --gixen-item-id <id>"
        )

    if cache is None:
        cache = CollectionCache()
    payload = cache.load()
    if payload.get("last_full_import") is None:
        return _not_imported_error()

    candidates = _stable_identity_candidates(
        payload.get("comics", []),
        gixen_item_id=gixen_item_id,
        full_title=full_title,
        release_date=release_date,
        source=source,
    )
    if len(candidates) != 1:
        return _identity_match_error(candidates, "remove")

    row = payload["comics"][candidates[0]]
    decrements, remaining = _decrement_or_remove(row.get("in_collection") or 0)
    if dry_run:
        return {
            "status": "preview",
            "action": "would_decrement" if decrements else "would_remove",
            "row": dict(row),
            "remaining_copies": remaining,
        }

    outcome: dict[str, Any] = {}

    def _mutate(locked_payload: dict[str, Any]) -> None:
        comics = locked_payload.get("comics", [])
        locked_candidates = _stable_identity_candidates(
            comics,
            gixen_item_id=gixen_item_id,
            full_title=full_title,
            release_date=release_date,
            source=source,
        )
        if len(locked_candidates) != 1:
            # Self-verify: what matched pre-lock no longer matches exactly
            # one row under the lock (BUI-417 TOCTOU precedent) — record the
            # mismatch and leave the store untouched rather than guess.
            outcome.update(_identity_match_error(locked_candidates, "remove"))
            return
        i = locked_candidates[0]
        locked_row = comics[i]
        outcome["removed_row"] = dict(locked_row)
        locked_decrements, locked_remaining = _decrement_or_remove(locked_row.get("in_collection") or 0)
        if locked_decrements:
            locked_row["in_collection"] = locked_remaining
        else:
            del comics[i]
        outcome["action"] = "decremented" if locked_decrements else "removed"
        outcome["remaining_copies"] = locked_remaining

    cache.apply(_mutate, command="collection-remediate-delete")

    if outcome.get("status"):
        return outcome

    cache.append_audit({
        "type": "collection_remediate_delete",
        "ts": _utcnow_iso(),
        "command": "collection-remediate-delete",
        "details": {
            "identity": {
                "gixen_item_id": gixen_item_id,
                "full_title": full_title,
                "release_date": release_date,
                "source": source,
            },
            "action": outcome["action"],
            "removed": outcome["removed_row"],
        },
    })

    return {
        "status": "ok",
        "action": outcome["action"],
        "removed": outcome["removed_row"],
        "remaining_copies": outcome["remaining_copies"],
    }


def cmd_collection_remediate_set_copies(
    *,
    gixen_item_id: Optional[str] = None,
    full_title: Optional[str] = None,
    release_date: Optional[str] = None,
    source: Optional[str] = None,
    in_collection: Optional[int] = None,
    delta: Optional[int] = None,
    dry_run: bool = False,
    cache: Optional[CollectionCache] = None,
) -> dict[str, Any]:
    """Set or adjust `in_collection` on ONE row by STABLE IDENTITY (BUI-427).

    Matcher-BYPASSING copy-count remediation — same identity resolution as
    :func:`cmd_collection_remediate_delete` (never the fuzzy check-matcher).
    Supply EXACTLY ONE of `in_collection` (an explicit absolute value, must be
    `>= 0`) or `delta` (a signed adjustment relative to the row's CURRENT
    count); a delta that would take the count below 0 is refused, not
    clamped.

    UNLIKE remediate-delete, this never removes the row even at 0 —
    `in_collection == 0` is itself a valid tracked-but-not-owned state
    (BUI-249/250/251), distinct from row absence. Pair with remediate-delete
    to actually remove a mis-filed row.

    `dry_run=True` previews the op without mutating. The real write
    re-resolves the identity INSIDE `cache.apply()`'s exclusive lock and
    self-verifies exactly one row still matches (and the delta still holds
    non-negative) at that point — the same TOCTOU-safe shape
    `cmd_collection_remediate_delete` uses.

    Return shape mirrors `cmd_collection_remediate_delete`'s status values
    (`invalid_request`, `not_imported`, `not_found`, `ambiguous`); on success:
    `{"status": "ok"|"preview", "row", "previous_in_collection"|
    "current_in_collection", "new_in_collection"}`.

    BUI-489: same wrong-store guard as `cmd_collection_remediate_delete` —
    refuses with `{"status": "explicit_store_required"}` when no `cache` is
    passed and `LOCG_DATA_DIR` is unset/unexpanded (see
    `_needs_explicit_store`), checked before `dry_run` is consulted.
    """
    error = _validate_remediation_identity(gixen_item_id, full_title)
    if error:
        return {"status": "invalid_request", "error": error}
    if (in_collection is None) == (delta is None):
        return {
            "status": "invalid_request",
            "error": "supply EXACTLY ONE of in_collection (absolute) or delta (+/- adjustment)",
        }
    if in_collection is not None and in_collection < 0:
        return {"status": "invalid_request", "error": "in_collection must be >= 0"}

    if _needs_explicit_store(cache):
        return _explicit_store_required_error(
            "locg collection remediate-set-copies --gixen-item-id <id> --in-collection 0"
        )

    if cache is None:
        cache = CollectionCache()
    payload = cache.load()
    if payload.get("last_full_import") is None:
        return _not_imported_error()

    candidates = _stable_identity_candidates(
        payload.get("comics", []),
        gixen_item_id=gixen_item_id,
        full_title=full_title,
        release_date=release_date,
        source=source,
    )
    if len(candidates) != 1:
        return _identity_match_error(candidates, "update")

    row = payload["comics"][candidates[0]]
    current = row.get("in_collection") or 0
    new_value = in_collection if in_collection is not None else current + delta
    if new_value < 0:
        return {
            "status": "invalid_request",
            "error": f"delta {delta} would take in_collection below 0 (current: {current})",
        }
    if dry_run:
        return {
            "status": "preview",
            "row": dict(row),
            "current_in_collection": current,
            "new_in_collection": new_value,
        }

    outcome: dict[str, Any] = {}

    def _mutate(locked_payload: dict[str, Any]) -> None:
        comics = locked_payload.get("comics", [])
        locked_candidates = _stable_identity_candidates(
            comics,
            gixen_item_id=gixen_item_id,
            full_title=full_title,
            release_date=release_date,
            source=source,
        )
        if len(locked_candidates) != 1:
            outcome.update(_identity_match_error(locked_candidates, "update"))
            return
        i = locked_candidates[0]
        locked_row = comics[i]
        locked_current = locked_row.get("in_collection") or 0
        locked_new = in_collection if in_collection is not None else locked_current + delta
        if locked_new < 0:
            # Self-verify: the count moved between the pre-check and the
            # lock (a concurrent writer) such that this delta would now go
            # negative — refuse rather than clamp or guess.
            outcome["status"] = "invalid_request"
            outcome["error"] = (
                f"delta {delta} would take in_collection below 0 "
                f"(current: {locked_current})"
            )
            return
        outcome["previous_row"] = dict(locked_row)
        locked_row["in_collection"] = locked_new
        outcome["current_in_collection"] = locked_current
        outcome["new_in_collection"] = locked_new

    cache.apply(_mutate, command="collection-remediate-set-copies")

    if outcome.get("status"):
        return outcome

    cache.append_audit({
        "type": "collection_remediate_set_copies",
        "ts": _utcnow_iso(),
        "command": "collection-remediate-set-copies",
        "details": {
            "identity": {
                "gixen_item_id": gixen_item_id,
                "full_title": full_title,
                "release_date": release_date,
                "source": source,
            },
            "previous_in_collection": outcome["current_in_collection"],
            "new_in_collection": outcome["new_in_collection"],
            "row": outcome["previous_row"],
        },
    })

    return {
        "status": "ok",
        "row": outcome["previous_row"],
        "previous_in_collection": outcome["current_in_collection"],
        "new_in_collection": outcome["new_in_collection"],
    }


# ---------------------------------------------------------------------------
# BUI-461: backfill publisher_name / release_date on ALREADY-STORED pending wins
# ---------------------------------------------------------------------------
#
# BUI-458 and BUI-210 fix the PRODUCER (`_build_win_row`) so newly recorded wins
# carry a real publisher and a real Metron date. Neither touches the rows already
# sitting in the store, and on 2026-07-19 that was 77/78 pending `agent_win` rows
# with a null publisher and 58 carrying the BUI-105 `{year}-01-01` placeholder —
# remediated by three hand-written, untested one-off scripts. This is that
# remediation as tested code, and a standing safety net for any future producer
# regression.
#
# It is deliberately NOT a second producer: it never re-derives identity
# (series_name / full_title / in_collection / gixen_item_id / price / source are
# never written), only the two fields the audit blocks a sync on.


def _backfill_needs_publisher(row: dict[str, Any]) -> bool:
    """True when the row carries no usable publisher_name."""
    return not str(row.get("publisher_name") or "").strip()


# BUI-471 residual #3: the 2026-07-19 manual backfill (three hand-written,
# untested scripts — see the BUI-461 block comment above) dodged the export's
# `YYYY-01-01` placeholder regex by writing `YYYY-01-02` instead. That day is
# fabricated, exactly the thing this whole command exists to never do (see the
# metron_id invariant in `_backfill_resolve_row` below) — it just predates the
# metron_id mechanism that makes it unnecessary. `-01-02` is a SAFE shape to
# retarget: record-win's own placeholder stamp is hardcoded to `-01-01`
# (`collection_io`'s BUI-105 block), and any Metron-sourced date always
# carries a metron_id the moment it's accepted (see `_backfill_resolve_row`),
# so the only way a row reaches `metron_id is None` AND a literal `-01-02`
# date is exactly this manual dodge (or an equivalent hand-edit) — never the
# normal record-win/backfill pipeline. That makes the shape itself a positive
# identification, not a guess.
_HAND_REMEDIATED_DATE_RE = re.compile(r"^\d{4}-01-02$")


def _is_hand_remediated_date(row: dict[str, Any]) -> bool:
    """True for the BUI-471 legacy `YYYY-01-02` dodge-date signature.

    Same source/metron_id intent gate as ``_is_placeholder_release_date`` —
    only a row still carrying no ``metron_id`` needs correcting; one that
    already has a real Metron-confirmed `-01-02` (a genuine cover date, however
    unlikely) is left alone.
    """
    if row.get("source") != "agent_win":
        return False
    if row.get("metron_id") is not None:
        return False
    return bool(_HAND_REMEDIATED_DATE_RE.match(str(row.get("release_date") or "")))


def _backfill_needs_date(row: dict[str, Any]) -> bool:
    """True when the row will reach LOCG dateless, a BUI-105 placeholder, or a
    legacy hand-remediated `-01-02` dodge date (BUI-471 residual #3).

    The placeholder test is ``collection_io._is_placeholder_release_date``
    itself, imported rather than re-expressed. Parity is load-bearing in the
    unsafe direction: this predicate decides which stored dates this command may
    OVERWRITE, so a local copy that drifted wider than the export's would let a
    backfill destroy a genuine cover date the export was happily keeping. That
    helper is an INTENT check (``metron_id is None``), not a shape check, so a
    real January date on a Metron-backed row is correctly not a target here.

    Widening this to also retarget a confirmed `_is_hand_remediated_date` row
    is safe in the same way: resolution below never blanks or guesses, it only
    writes a date Metron itself confirmed, so a Metron miss just leaves the
    fabricated `-01-02` day in place — no worse than before, never a new loss.
    """
    from locg.collection_io import _is_placeholder_release_date

    if not str(row.get("release_date") or "").strip():
        return True
    if _is_placeholder_release_date(row):
        return True
    return _is_hand_remediated_date(row)


def _is_placeholder_shaped(release_date: Any) -> bool:
    """True for the bare ``YYYY-01-01`` SHAPE, ignoring intent.

    Shares the export's compiled pattern rather than re-typing it, for the same
    reason :func:`_backfill_needs_date` shares its predicate: if the placeholder
    shape ever widens, a hand-copied literal here would silently stop matching
    and the safety guard built on it would quietly stop firing. Distinct from
    ``_is_placeholder_release_date`` — that one needs a whole row to judge
    INTENT; this asks only about a date string, which is all the write-time
    guard has once it is deciding whether a metron_id may bless it.
    """
    from locg.collection_io import _PLACEHOLDER_DATE_RE

    return bool(_PLACEHOLDER_DATE_RE.match(str(release_date or "")))


def _is_backfill_target(row: dict[str, Any]) -> bool:
    """True for a pending win row this command is allowed to touch.

    The safety envelope, all four conditions required:

    * ``source == "agent_win"`` — an ``locg_export`` row's publisher/date came
      from LOCG itself and is authoritative; overwriting it with a Metron guess
      would corrupt the reconciliation baseline.
    * ``in_collection >= 1`` — a tracked-but-not-owned row (BUI-249/250/251) is
      not part of the pending upload.
    * pending push (:func:`locg.collection_io._is_pending_push_row`) — the
      SAME "pushed_to_locg_at IS NULL OR local_added_at > pushed_to_locg_at"
      test the export uses to build its row set (BUI-471). This used to be the
      stricter ``pushed_to_locg_at is None`` alone, which silently orphaned a
      row RE-PENDED after an earlier push: the export would ship it dateless
      or publisher-less, but the backfill's narrower target set would never
      pick it up to remediate. Widening this to match the export closes that
      gap; an already-pushed, never-re-pended row is still excluded (LOCG is
      that row's copy of record now — editing it locally would desync the
      two sides).
    * ``in_wish_list`` falsy — never a wish twin. (Redundant with the
      ``in_collection`` gate for rows this codebase writes, kept explicit
      because a wish twin is the BUI-122 data-loss shape and the cost of the
      extra check is zero.)

    ...and at least one of the two backfillable fields must actually be empty,
    so a re-run is a no-op rather than a rewrite (idempotence).
    """
    from locg.collection_io import _is_pending_push_row

    if row.get("source") != "agent_win":
        return False
    if (row.get("in_collection") or 0) < 1:
        return False
    if not _is_pending_push_row(row):
        return False
    if row.get("in_wish_list"):
        return False
    return _backfill_needs_publisher(row) or _backfill_needs_date(row)


def _backfill_row_matches_filters(
    row: dict[str, Any], series: Optional[str], full_title: Optional[str]
) -> bool:
    """Case-insensitive substring filters on series_name / full_title.

    Substring, not exact: these are SELECTION filters for narrowing a run
    ("just the X-Men rows"), never row identity — identity resolution under the
    write lock goes through :func:`_stable_identity_candidates`, which is exact.
    """
    if series:
        if series.strip().lower() not in str(row.get("series_name") or "").lower():
            return False
    if full_title:
        if full_title.strip().lower() not in str(row.get("full_title") or "").lower():
            return False
    return True


def _backfill_row_identity(row: dict[str, Any]) -> dict[str, Any]:
    """The stable identity used to re-find this row under the write lock."""
    return {
        "gixen_item_id": (str(row.get("gixen_item_id") or "").strip() or None),
        "full_title": row.get("full_title"),
        "release_date": row.get("release_date"),
        "source": row.get("source"),
    }


def _backfill_resolve_row(
    row: dict[str, Any],
    metron: Any,
    metron_disabled: bool,
) -> dict[str, Any]:
    """Resolve one row's publisher/date from Metron. No writes, no lock.

    Returns ``{"fields": {name: {"from", "to"}}, "reason": str|None,
    "metron_disabled": bool, "metron_calls": int}``.

    Resolution rules, and why each is the conservative one:

    * **The year gate needs a year — or the row's own series_range.**
      ``_metron_release_date`` returns its candidate UNGATED when it has
      neither, and an ungated ``lookup_issue`` is exactly the reprint trap (a
      naive ``lookup_issue("The X-Men", "59", None)`` can return a 2005
      collected edition). The row's own ``release_date`` year is the primary
      era evidence, and on a BUI-105 placeholder that year is REAL — only the
      month/day are fabricated. A genuinely DATELESS row has no such year, but
      (BUI-471, adopting BUI-464's mechanism) its STORED ``series_name`` is
      already the canonical, resolved volume the row belongs to — an
      independent ``(begin, end)`` publication window
      (:func:`series_year_range`) that gates the candidate the same way a real
      year would, without a second lookup. Only when NEITHER is available is
      the row reported for the documented web fallback
      (``references/date-backfill.md``) instead of being guessed at.
    * **One accept test, shared with the producer.** A hit is trusted only when
      ``_metron_release_date`` yields a date for it — byte-identical to the
      test ``_build_win_row`` applies at its own date-only lookup. A hit whose
      date the era guard rejects is dropped WHOLE (id and publisher too): they
      all came from the same possibly-wrong issue match, and a reprint under a
      different imprint would otherwise contribute a wrong publisher that
      imports to LOCG silently.
    * **An existing metron_id is exact.** A row that already carries one needs
      no re-resolution for its publisher — ``lookup_issue_detail(id)`` is an
      unambiguous fetch by primary key, and it costs one call instead of two.
    * **Misses leave the field alone.** Never a fabricated publisher, never a
      fabricated date. A null publisher is the intended ``audit-pending``
      backstop; a wrong one imports silently.
    """
    from locg.metron import MetronCredentialError

    fields: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    metron_calls = 0

    needs_publisher = _backfill_needs_publisher(row)
    needs_date = _backfill_needs_date(row)
    existing_metron_id = row.get("metron_id")

    series_query = base_series_name(str(row.get("series_name") or "")).strip()
    _, issue_num = _split_full_title(str(row.get("full_title") or ""))
    year = _coerce_year(row.get("release_date"))
    year_str = str(year) if year is not None else None

    resolved: Optional[dict[str, Any]] = None
    # A fresh issue resolution is needed for a date, and for a publisher on a
    # row with no metron_id to fetch detail by.
    wants_lookup = needs_date or (needs_publisher and existing_metron_id is None)

    # BUI-471: independent era evidence for a row with no year at all — the
    # row's OWN series_name was already resolved and stored at record-win
    # time, so this needs no extra lookup (unlike record-win's
    # series_name_index consult). Scoped to existing_metron_id is None: a row
    # that already carries an id should never re-search by NAME for a date —
    # "An existing metron_id is exact" above applies to a date fetch the same
    # way it applies to the publisher fetch. (In practice this scoping is the
    # only thing standing between year_str is None and a real lookup ever
    # firing for such a row: needs_date + an existing metron_id is only ever a
    # genuinely BLANK dateless row, since a placeholder or `-01-02` shape both
    # require metron_id is None by construction — see
    # _is_placeholder_release_date / _is_hand_remediated_date.) Computed only
    # when it might actually be consulted (a year-bearing row never reaches
    # the no-year branch that would use it).
    series_range: Optional[tuple[int, int]] = None
    if wants_lookup and year_str is None and existing_metron_id is None:
        series_range = series_year_range(str(row.get("series_name") or ""))

    if wants_lookup and not metron_disabled:
        if year_str is None and series_range is None:
            reasons.append(
                "no year available to era-gate a Metron lookup (dateless row) — "
                "resolve via the documented web fallback"
            )
        elif not issue_num:
            reasons.append("no issue number in full_title")
        elif not series_query:
            reasons.append("no series_name to search Metron with")
        else:
            try:
                metron_calls += 1
                looked_up = metron.lookup_issue(series_query, issue_num, year_str)
                if looked_up and _metron_release_date(looked_up, year_str, series_range) is not None:
                    resolved = looked_up
                else:
                    era_evidence = year_str if year_str is not None else f"series_range={series_range}"
                    reasons.append(
                        "Metron returned no era-compatible issue match "
                        f"({series_query} #{issue_num}, {era_evidence})"
                    )
            except MetronCredentialError:
                metron_disabled = True
                reasons.append("Metron credentials not configured")
            else:
                metron_disabled = _check_metron_degraded(metron, metron_disabled)
    elif wants_lookup:
        reasons.append("Metron disabled for this run")

    # The date Metron backs for this row, whether or not it differs from what is
    # stored. Non-None exactly when a hit was accepted (accepting `resolved`
    # REQUIRES this call to have produced a date), so it doubles as "Metron
    # confirmed this row's date" for the invariant guard below — a placeholder
    # that happens to coincide with the genuine cover date (Fantastic Four #16:
    # stamped 1963-01-01, and 1963-01-01 really is the cover date) is confirmed,
    # not merely unchanged.
    metron_date = (
        _metron_release_date(resolved, year_str, series_range) if resolved is not None else None
    )
    if needs_date and metron_date and metron_date != (row.get("release_date") or None):
        fields["release_date"] = {"from": row.get("release_date"), "to": metron_date}

    # Carrying the metron_id is what makes a genuine January cover date survive
    # export: `_is_placeholder_release_date` blanks a `YYYY-01-01` date only
    # while `metron_id is None`, so a Metron-backed January row keeps its real
    # date with no fabricated `-01-02` day needed. Only ever set alongside a
    # Metron date — see the invariant guard below.
    if resolved is not None and existing_metron_id is None and resolved.get("metron_id"):
        fields["metron_id"] = {"from": None, "to": resolved["metron_id"]}

    detail_id = resolved.get("metron_id") if resolved else existing_metron_id
    if needs_publisher and detail_id is not None and not metron_disabled:
        try:
            metron_calls += 1
            detail = metron.lookup_issue_detail(detail_id)
            publisher = (detail or {}).get("publisher")
            if isinstance(publisher, str) and publisher.strip():
                fields["publisher_name"] = {
                    "from": row.get("publisher_name"),
                    "to": publisher.strip(),
                }
            else:
                reasons.append(f"Metron has no publisher on issue {detail_id}")
        except MetronCredentialError:
            metron_disabled = True
            reasons.append("Metron credentials not configured")
        else:
            metron_disabled = _check_metron_degraded(metron, metron_disabled)
    elif needs_publisher and not reasons:
        # Invariant: every publisher we decline to fetch gets a reason. Reaching
        # here with none already recorded means the breaker tripped between a
        # usable metron_id (freshly accepted, or the row's own) and the detail
        # call — otherwise that row silently gains a date but no publisher, and
        # the run reports nothing to retry. Every other way of arriving without
        # a publisher (no year, no issue number, no series, no era-compatible
        # match, missing credentials) has already recorded its own reason above.
        reasons.append("Metron unavailable before the publisher fetch")

    # INVARIANT (defensive): a metron_id must never land on a row whose Jan-1
    # date Metron did not confirm. Setting one flips
    # `_is_placeholder_release_date` to False, so the export would ship a
    # FABRICATED Jan-1 date to LOCG instead of blanking it — turning a merely
    # dateless row into a wrongly-dated one, which is the more expensive
    # mistake. Unreachable as written (a metron_id is only planned alongside an
    # accepted hit, and accepting one requires `_metron_release_date` to have
    # produced the very date being kept), and kept anyway because it is the one
    # way this command could corrupt data rather than just fail to improve it.
    if "metron_id" in fields:
        final_date = str(
            fields.get("release_date", {}).get("to", row.get("release_date")) or ""
        )
        if _is_placeholder_shaped(final_date) and final_date != (metron_date or ""):
            del fields["metron_id"]
            reasons.append(
                "withheld metron_id: row would keep an unconfirmed Jan-1 placeholder "
                "the export must still blank"
            )

    return {
        "fields": fields,
        "metron_date": metron_date,
        "reason": "; ".join(reasons) if reasons else None,
        "metron_disabled": metron_disabled,
        "metron_calls": metron_calls,
    }


def _backfill_backup_dir(cache: CollectionCache, backup_dir: Optional[str]) -> Path:
    """Destination for the pre-write durable snapshot.

    Defaults to a timestamped subdir of ``<store>-backups/`` — the SAME sibling
    root the BUI-433 backup endpoint uses (``collection_backups_root``,
    BUI-471 — previously hand-duplicated here), deliberately outside the store
    so ``CollectionCache._rotate_backups``' 3-deep ``.bak.0/1/2`` ring can never
    evict it. A bulk multi-row write needs a snapshot that survives however many
    writes happen before someone decides to restore it.
    """
    if backup_dir:
        return Path(backup_dir)
    root = collection_backups_root(cache.path.parent)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return root / f"backfill-{ts}"


def _backfill_plan_changes(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pre-lock ``changes`` shape for a list of resolved plans.

    Shared by every place ``cmd_collection_backfill`` needs to describe
    planned-but-not-yet-written intent: the base preview/backup-failed result
    and, before BUI-471 chunked the write path, the single post-resolution
    report — all three used to hand-roll this same 5-line comprehension.
    """
    return [
        {
            "full_title": p["full_title"],
            "series_name": p["series_name"],
            "gixen_item_id": p["identity"]["gixen_item_id"],
            "fields": p["fields"],
        }
        for p in plans
    ]


def _backfill_write_chunk(
    cache: CollectionCache,
    chunk_plans: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply one chunk's worth of resolved plans under a single lock cycle.

    Returns ``(applied_changes, skipped)`` for JUST this chunk. Identical
    per-row logic to the original single-cycle ``_mutate`` (TOCTOU
    re-resolution, per-field staleness check, the metron_id invariant
    re-checked under the lock) — only the SCOPE changed (BUI-471 residual #2):
    one chunk's plans instead of the whole backlog's, so a chunk boundary is
    where a crash's blast radius stops.
    """
    applied_changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def _mutate(locked_payload: dict[str, Any]) -> None:
        comics = locked_payload.get("comics", [])
        for plan in chunk_plans:
            identity = plan["identity"]
            idxs = _stable_identity_candidates(comics, **identity)
            if len(idxs) != 1:
                # Re-resolved under the lock (BUI-417 TOCTOU precedent): the row
                # moved, vanished, or now collides. Skip it — the next run will
                # pick it up if it still needs backfilling.
                skipped.append({
                    "full_title": plan["full_title"],
                    "reason": f"{len(idxs)} rows match the identity under the write lock",
                })
                continue
            locked_row = comics[idxs[0]]
            if not _is_backfill_target(locked_row):
                skipped.append({
                    "full_title": plan["full_title"],
                    "reason": "row no longer a pending agent_win backfill target under the lock",
                })
                continue
            written: dict[str, dict[str, Any]] = {}
            for field, delta in plan["fields"].items():
                if field == "metron_id":
                    continue  # deferred below, once release_date has settled
                if (locked_row.get(field) or None) != (delta["from"] or None):
                    # A concurrent writer already changed this exact field —
                    # theirs wins; never clobber a fresher value with a value
                    # resolved before the lock.
                    continue
                locked_row[field] = delta["to"]
                written[field] = delta

            # The metron_id invariant, re-run against the row AS IT IS under the
            # lock. Checking it only at plan time is not enough: a plan can carry
            # a metron_id and NO release_date (the row had a real date when it
            # was resolved), and a concurrent record-win retry can meanwhile
            # rebuild that row by gixen_item_id and downgrade its date back to a
            # `{year}-01-01` placeholder — the exact regression
            # `_dedup_era_compatible` documents. The per-field staleness check
            # cannot catch it, because metron_id's own `from` (None) is still
            # accurate. Writing it then would flip
            # `_is_placeholder_release_date` to False and ship that FABRICATED
            # date to LOCG. Only a date Metron actually confirmed may be blessed.
            metron_id_delta = plan["fields"].get("metron_id")
            if metron_id_delta is not None and (locked_row.get("metron_id") or None) is None:
                locked_date = locked_row.get("release_date")
                if not _is_placeholder_shaped(locked_date) or str(locked_date or "") == str(
                    plan["metron_date"] or ""
                ):
                    locked_row["metron_id"] = metron_id_delta["to"]
                    written["metron_id"] = metron_id_delta
                else:
                    skipped.append({
                        "full_title": plan["full_title"],
                        "reason": (
                            "withheld metron_id under the write lock: the row now "
                            f"carries an unconfirmed {locked_date} placeholder the "
                            "export must still blank"
                        ),
                    })
            if written:
                applied_changes.append({
                    "full_title": plan["full_title"],
                    "series_name": plan["series_name"],
                    "gixen_item_id": identity["gixen_item_id"],
                    "fields": written,
                })
            else:
                skipped.append({
                    "full_title": plan["full_title"],
                    "reason": "all planned fields changed under the lock; nothing written",
                })

    cache.apply(_mutate, command="collection-backfill")
    return applied_changes, skipped


def cmd_collection_backfill(
    *,
    series: Optional[str] = None,
    full_title: Optional[str] = None,
    apply: bool = False,
    cadence: float = 3.0,
    limit: Optional[int] = None,
    backup_dir: Optional[str] = None,
    cache: Optional[CollectionCache] = None,
    metron: Optional[Any] = None,
) -> dict[str, Any]:
    """Backfill publisher_name / release_date on stored pending wins (BUI-461).

    Read-only by default: ``apply=False`` resolves every candidate against
    Metron and prints the exact field diff, writing NOTHING.

    ``apply=True`` resolves and writes in chunks of ``RECORD_WIN_CHUNK_SIZE``
    (BUI-471 residual #2 — mirrors ``cmd_collection_record_win``'s chunking,
    same rationale): a durable ``backup_store`` snapshot is taken once, before
    the FIRST chunk that has anything to write, and refused if that snapshot
    captured zero rows. Each chunk is then resolved and committed in its own
    ``CollectionCache.apply`` cycle — locked, ``.bak``-rotated, atomic per
    CHUNK. This bounds a crash's blast radius: every chunk committed before
    the crash stays durably backfilled, and only the in-flight (uncommitted)
    chunk's Metron work is lost — never the whole backlog, as a single-cycle
    write would. A chunk whose write raises is logged and skipped; later
    chunks still get a chance (mirrors ``cmd_collection_record_win``'s
    per-chunk try/except).

    Only ``publisher_name``, ``release_date`` and ``metron_id`` are ever
    written, and only onto rows passing :func:`_is_backfill_target`. Identity
    and copy counts are never touched. Re-running is a no-op: a row whose
    fields are now populated no longer matches the target predicate.

    Every network resolution happens BEFORE that chunk's lock is taken — a
    Metron batch can run for minutes at ``cadence`` and must not hold an
    exclusive flock the comics server also needs (``apply``'s own timeout is
    30s per chunk).

    ``cadence`` seconds are slept per Metron call spent (default 3.0 — Metron
    allows ~20 req/min, so ~3s per call). Per CALL, not per row: a row needing
    both a date and a publisher costs TWO calls, and pacing per row would run a
    backlog of those at double the intended rate — straight into the rate limit
    whose one trip latches Metron off for the rest of the run (BUI-465). The
    pacing state (``cadence`` debt and the Metron degraded breaker) carries
    ACROSS chunk boundaries — chunking only changes when work is committed,
    never how Metron traffic is paced.
    Once Metron trips its degraded breaker (BUI-255) the remaining rows make no
    further calls and are reported unresolved; ``metron_degraded`` in the result
    says the run was partial, so a follow-up run picks up what was left. Pair
    with ``limit`` to work through a large backlog across consecutive runs.

    BUI-471 residual #5: when ``cache`` is not passed explicitly (i.e. the
    caller wants the REAL store, not a test double), this refuses to run
    unless ``LOCG_DATA_DIR`` is explicitly set in the environment. Silently
    defaulting to ``locg.config._cache_dir()``'s resolution order
    (``LOCG_DATA_DIR`` → ``<repo>/data/locg`` → ``~/.cache/locg``) is the
    "wrong-store trap": on an empty local store this hard-fails harmlessly
    (R11 ``not_imported``), but on a NON-EMPTY local store it would silently
    remediate the wrong collection — the server-owned store lives elsewhere
    (see ``LOCG_DATA_DIR=$HOME/.comics-server/collection-store`` below).

    BUI-471 residual #6: ``metron_unavailable`` (BUI-465) tells a row that was
    never successfully asked apart from one Metron genuinely missed, but only
    on rows written from that commit forward — every earlier row simply lacks
    the field ("unknown" provenance, not "asked and missed"). This command
    does NOT retry those differently; ``legacy_unknown_availability_count`` in
    the result reports how many of this run's candidates are from that unknown
    population, so a caller can decide knowingly rather than this command
    silently guessing.

    BUI-471 (adopting BUI-464): a genuinely DATELESS candidate (no
    ``release_date`` at all — as opposed to a BUI-105 placeholder, which
    already has a real year) is no longer automatically unresolvable. Its
    stored ``series_name`` is already the canonical, resolved volume the row
    belongs to, so :func:`_backfill_resolve_row` uses that name's own
    ``(begin, end)`` publication window as era evidence in place of a missing
    year — the same mechanism BUI-464 gave record-win, needing no extra
    lookup here since the row already carries its resolved series_name. Only
    applies when the row has no ``metron_id`` yet (a row that already has one
    never re-searches by name for a date — see ``_backfill_resolve_row``'s
    "An existing metron_id is exact").

    Returns ``{status, applied, candidate_count, planned_count, updated_count,
    changes, unresolved, skipped_at_write, metron_degraded, backup,
    legacy_unknown_availability_count, chunk_count, chunks_committed}``.
    ``status`` is ``"preview"`` for a dry run, ``"ok"`` after a write (or an
    ``--apply`` run that found no work), ``"invalid_request"``,
    ``"not_imported"`` (R11), ``"explicit_store_required"`` (BUI-471) or
    ``"backup_failed"`` when it refused to run. ``candidate_count`` counts
    every row matching the filters BEFORE ``limit``, so a chunked run still
    reports the size of the remaining backlog.
    """
    from locg.metron import MetronClient

    if cadence < 0:
        return {"status": "invalid_request", "error": "cadence must be >= 0"}
    if limit is not None and limit < 0:
        return {"status": "invalid_request", "error": "limit must be >= 0"}

    # BUI-471 residual #5: never silently fall back to whatever
    # locg.config._cache_dir() resolves to — that can be a non-empty LOCAL
    # store on the very machine (e.g. the Mac Mini) whose SERVER-owned
    # store is what actually needs remediating. Require the caller to say
    # which store they mean. Shared with import/record-win since BUI-476.
    if _needs_explicit_store(cache):
        return _explicit_store_required_error("locg collection backfill [--apply]")
    if cache is None:
        cache = CollectionCache()
    if metron is None:
        metron = MetronClient()

    payload = cache.load()
    if payload.get("last_full_import") is None:
        return _not_imported_error()

    candidates = [
        row
        for row in payload.get("comics", [])
        if _is_backfill_target(row) and _backfill_row_matches_filters(row, series, full_title)
    ]
    candidate_count = len(candidates)
    if limit is not None:
        candidates = candidates[:limit]

    # BUI-471 residual #6: tally, don't act on. See the docstring.
    legacy_unknown_availability_count = sum(
        1 for row in candidates if "metron_unavailable" not in row
    )

    chunks = _chunked(candidates, RECORD_WIN_CHUNK_SIZE)

    plans: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    applied_changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    metron_disabled = False
    calls_since_sleep = 0
    backup: Optional[dict[str, Any]] = None
    chunks_committed = 0

    for chunk in chunks:
        chunk_plans: list[dict[str, Any]] = []
        for row in chunk:
            if calls_since_sleep and cadence > 0 and not metron_disabled:
                time.sleep(cadence * calls_since_sleep)
            outcome = _backfill_resolve_row(row, metron, metron_disabled)
            metron_disabled = outcome["metron_disabled"]
            calls_since_sleep = outcome["metron_calls"]
            if outcome["fields"]:
                chunk_plans.append({
                    "identity": _backfill_row_identity(row),
                    "full_title": row.get("full_title"),
                    "series_name": row.get("series_name"),
                    "fields": outcome["fields"],
                    # Carried so the write can re-run the metron_id invariant
                    # against the row as it ACTUALLY is under the lock, not as
                    # it looked when this plan was made. See _mutate.
                    "metron_date": outcome["metron_date"],
                })
            if outcome["reason"]:
                unresolved.append({
                    "full_title": row.get("full_title"),
                    "gixen_item_id": row.get("gixen_item_id"),
                    "reason": outcome["reason"],
                })
        plans.extend(chunk_plans)

        if not apply or not chunk_plans:
            continue

        # The durable pre-write snapshot: taken exactly once, right before the
        # FIRST chunk that actually has something to write — never for a
        # chunk (or a whole run) with nothing to commit.
        if backup is None:

            def _backup_failed_result(error: str) -> dict[str, Any]:
                # Plans resolved SO FAR (this chunk and any earlier ones) —
                # the pre-lock intent, since nothing has been written yet.
                return {
                    "status": "backup_failed",
                    "error": error,
                    "applied": False,
                    "candidate_count": candidate_count,
                    "planned_count": len(plans),
                    "updated_count": 0,
                    "changes": _backfill_plan_changes(plans),
                    "unresolved": unresolved,
                    "skipped_at_write": [],
                    "metron_degraded": metron_disabled,
                    "backup": None,
                    "legacy_unknown_availability_count": legacy_unknown_availability_count,
                    "chunk_count": len(chunks),
                    "chunks_committed": 0,
                }

            dest = _backfill_backup_dir(cache, backup_dir)
            try:
                backup = cache.backup_store(dest)
            except (RuntimeError, OSError) as exc:
                return _backup_failed_result(f"backup to {dest} failed: {exc} — refusing to write")
            if backup["comics_count"] == 0 and backup["wish_list_count"] == 0:
                return _backup_failed_result(
                    f"backup at {backup['backup_path']} captured zero rows — an "
                    "empty backup is indistinguishable from a broken one; "
                    "refusing to write"
                )

        try:
            chunk_applied, chunk_skipped = _backfill_write_chunk(cache, chunk_plans)
        except Exception as exc:  # noqa: BLE001 — chunked write: log and let later chunks try
            logger.error("collection-backfill: chunk commit failed: %s", exc)
            continue

        applied_changes.extend(chunk_applied)
        skipped.extend(chunk_skipped)
        chunks_committed += 1
        cache.append_audit({
            "type": "collection_backfill",
            "ts": _utcnow_iso(),
            "command": "collection-backfill",
            "details": {
                "filters": {"series": series, "full_title": full_title, "limit": limit},
                "backup_path": backup["backup_path"],
                "updated_count": len(chunk_applied),
                "changes": chunk_applied,
                "skipped_at_write": chunk_skipped,
            },
        })

    base_result: dict[str, Any] = {
        "candidate_count": candidate_count,
        "planned_count": len(plans),
        "changes": _backfill_plan_changes(plans),
        "unresolved": unresolved,
        "metron_degraded": metron_disabled,
        "legacy_unknown_availability_count": legacy_unknown_availability_count,
        "chunk_count": len(chunks),
    }

    if not apply:
        # `preview` means "this was a dry run" — no chunk write was ever
        # attempted, so `backup` stays None and nothing was applied.
        return {
            "status": "preview",
            "applied": False,
            "updated_count": 0,
            "skipped_at_write": [],
            "backup": None,
            "chunks_committed": 0,
            **base_result,
        }

    # `ok` covers both "found no work" (backup stays None — no chunk ever had
    # anything to write) and "wrote at least one chunk". `changes` here
    # reports what was ACTUALLY written (applied_changes, post-lock) rather
    # than base_result's pre-lock plan — the two can differ per-field (a
    # concurrent writer can win a field the plan intended to touch; see
    # _backfill_write_chunk's staleness check), and the post-write response
    # should describe reality, not the pre-lock intent.
    #
    # `applied` mirrors the pre-chunking contract: True whenever a write CYCLE
    # was attempted (chunks_committed > 0), not whether any row survived to be
    # written — a chunk that ran to completion but had every row raced out
    # from under it (see test_row_that_left_the_target_set_under_the_lock_is_
    # skipped_whole) still counts as "applied", same as the single-cycle
    # write this replaced always reported once it got that far.
    return {
        "status": "ok",
        "applied": chunks_committed > 0,
        "updated_count": len(applied_changes),
        "skipped_at_write": skipped,
        "backup": backup,
        "chunks_committed": chunks_committed,
        **base_result,
        "changes": applied_changes,
    }
