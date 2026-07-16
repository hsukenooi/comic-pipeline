"""Command implementations for locg CLI."""
from __future__ import annotations

import getpass
import json
import logging
import math
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
        raise FileNotFoundError(f"Wish-list cache not found: {path}. Run: locg collection import")
    with open(path) as f:
        data = json.load(f)
    items: list[dict[str, Any]] = data.get("items", [])
    if title:
        needle = title.lower()
        items = [it for it in items if needle in (it.get("name") or "").lower()]
    return items


def _wish_list_add_to_items(
    title: str, items: list[dict[str, Any]], force: bool = False
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
    """
    title = (title or "").strip()
    if not title:
        return {"error": "wish-list add: title must be non-empty"}

    if not force:
        duplicate = _find_duplicate_wish_entry(title, items)
        if duplicate is not None:
            return {"status": "exists", "existing": duplicate, "items": len(items)}

    entry = {"name": title, "id": None, "source": "local"}
    items.append(entry)
    return {"status": "ok", "added": entry, "items": len(items)}


def _read_wish_list_cache_items() -> list[dict[str, Any]]:
    """Read the wish-list cache's ``items`` list from disk, or ``[]`` if the
    cache file does not exist yet.

    The read half of the tempfile+os.replace atomic pair :func:`_write_wish_list_cache`
    writes. Shared by :func:`cmd_wish_list_add` and the batch write in
    :func:`cmd_wish_list_add_creator_run` (BUI-325), so both go through the
    same read logic rather than each hand-rolling the ``path.exists()`` +
    ``json.load`` dance.

    Deliberately does NOT catch ``json.JSONDecodeError``: this is the
    pre-write read, so a corrupt cache must fail loudly here rather than be
    silently treated as empty — degrading to ``[]`` and then writing would
    permanently erase whatever survived the corruption. Contrast
    :func:`cmd_wish_list_from_cache`'s read-only dedup callers, which DO
    tolerate a corrupt cache (BUI-313): degrading to "dedup against nothing"
    is safe there because nothing on disk gets overwritten as a result.
    """
    path = wish_list_cache_path()
    if not path.exists():
        return []
    with open(path) as f:
        payload = json.load(f)
    return payload.get("items") or []


def _write_wish_list_cache(items: list[dict[str, Any]]) -> Path:
    """Atomically write ``items`` as the wish-list cache.

    Uses :func:`locg._atomic.atomic_write_json` (tempfile + os.replace +
    chmod 600) — the same atomic write pattern used by every wish-list
    cache writer. Shared by :func:`cmd_wish_list_add` (one entry) and
    :func:`cmd_wish_list_add_creator_run` (BUI-325: the whole run's entries
    in one call), so there is exactly one atomic write per call site rather
    than a bespoke copy of the tempfile dance at each.
    """
    path = wish_list_cache_path()
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


def cmd_wish_list_add(title: str, force: bool = False) -> dict[str, Any]:
    """Append a manual entry to the local wish-list cache.

    Writes ``{"name": title, "id": None, "source": "local"}`` to
    ``data/locg/wish-list.json`` using the same atomic write pattern as the rest
    of the wish-list cache writers (tempfile + os.replace + chmod 600, via
    :func:`_write_wish_list_cache`).

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
    items = _read_wish_list_cache_items()

    result = _wish_list_add_to_items(title, items, force=force)
    if "error" in result or result.get("status") == "exists":
        return result

    written_path = _write_wish_list_cache(items)
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
                "next sync (BUI-122). Run `locg collection import <export.xlsx>` "
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

        to_add.append({"title": title, "number": number})
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
            result = _wish_list_add_to_items(item["title"], write_items)
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


def cmd_wish_list_remove(title: str) -> dict[str, Any]:
    """Remove the first matching entry from the local wish-list cache.

    Matches on exact ``name`` field. Writes via the shared atomic writer
    :func:`_write_wish_list_cache` (tempfile + os.replace + chmod 600), the
    same one every other wish-list write path uses (BUI-329).
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

    written_path = _write_wish_list_cache(new_items)

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
    """
    series, issue = split_series_issue_for_ownership(name or "")
    series = series.strip()
    if not series or not issue:
        return None
    return series, issue


def _find_duplicate_wish_entry(title: str, items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the wish-list item among ``items`` that duplicates ``title``.

    Dedup key is the DECORATED series portion + issue token (via
    :func:`_split_wish_list_name`), compared case-insensitively with leading
    zeros stripped (:func:`normalize_issue_key`) — never ``_normalize_series_key``,
    which collapses ``(Vol. N)``/year decoration and would merge genuinely
    different volumes of the same masthead (the BUI-284 trap).

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
    for item in items:
        item_parsed = _split_wish_list_name(item.get("name") or "")
        if item_parsed is None:
            continue
        item_series, item_issue = item_parsed
        if item_series.strip().lower() == series_cmp and normalize_issue_key(item_issue) == issue_cmp:
            return item
    return None


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
    out every owned row whose release year differs). Consequently this audit
    can land on the WRONG volume/era of a same-numbered issue (BUI-266: a
    decoy UK-reprint "The Avengers (1973 - 1976)" #52 matched against an owned
    1968 Vol. 1, and a base "Uncanny X-Men #201" wish matched an owned
    Newsstand copy). Each conflict therefore carries the SAME BUI-249
    provenance fields ``cmd_collection_check`` returns (``series_name``,
    ``release_date`` of the matched row) so a caller can visually catch a
    cross-era/cross-edition false match before removing it — see
    :func:`cmd_wish_list_remove_conflicts`, which is the removal half of this
    audit and never removes anything not surfaced here first.

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
        # BUI-284: ambiguous_cross_volume counts as owned here — this audit is
        # always year-free (a wish name has no cover date), so an owned-under-
        # multiple-volumes book returns ambiguous, and missing it would let the
        # owned copy get exported In Collection=0 and deleted (BUI-122).
        if collection_check_reports_owned(result):
            conflicts.append({
                "name": name,
                "series": series,
                "issue": issue,
                "id": it.get("id"),
                "full_title_matched": result["full_title_matched"],
                # BUI-266: matched-row provenance, so a decoy/cross-era match
                # is visible before this conflict is removed.
                "series_name": result["matched_series_name"],
                "release_date": result["matched_release_date"],
            })
    return {
        "total": len(items),
        "checked": checked,
        "unparseable": unparseable,
        "conflicts": conflicts,
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

    Raises ``FileNotFoundError`` if the wish-list cache does not exist.
    """
    audit = cmd_wish_list_conflicts()
    conflicts_by_name: dict[str, dict[str, Any]] = {c["name"]: c for c in audit["conflicts"]}

    errors: list[dict[str, Any]] = []
    if names is None:
        targets = list(audit["conflicts"])
    else:
        targets = []
        for name in names:
            conflict = conflicts_by_name.get(name)
            if conflict is None:
                errors.append({
                    "name": name,
                    "error": "not a current wish-list/collection conflict — skipped, nothing removed",
                })
            else:
                targets.append(conflict)

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
    manual_series_count, wish_list_count, oldest_pending_days, pushed_wishes}.
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

# An ordinal (digit "2nd"/"3rd"/… or word "second"/"third"/…) immediately
# followed by "print"/"printing(s)". Requiring the ordinal keeps a series whose
# NAME merely contains "printing" from reading as a marker.
_PRINTING_MARKER_RE = re.compile(
    r"\b(?:(\d+)\s*(?:st|nd|rd|th)|("
    + "|".join(_PRINTING_ORDINAL_WORDS)
    + r"))[\s-]+print(?:ing)?s?\b",
    re.IGNORECASE,
)


def _printing_ordinal(text: Optional[str]) -> int:
    """Printing ordinal named in ``text``: 2 for "2nd Printing", 3 for "Third
    Printing", … Unmarked text and an explicit "1st Printing" are both 1 — a
    query without a marker means the base (first) printing, and an owned row
    labeled "1st Printing" genuinely satisfies it."""
    m = _PRINTING_MARKER_RE.search(text or "")
    if not m:
        return 1
    if m.group(1):
        return int(m.group(1))
    return _PRINTING_ORDINAL_WORDS[m.group(2).lower()]


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
                # Mechanical ordinal (1 = base printing) so a caller can pick
                # out the query's own printing among 3+ candidates without
                # re-parsing full_title.
                "printing_ordinal": _printing_ordinal(row.get("full_title") or ""),
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

    BUI-267: a known edition suffix (Newsstand/Direct/2nd Printing/Facsimile —
    :data:`VARIANT_SUFFIX_MAP`) names a genuinely separate LOCG catalog entry,
    so a base win must not be deduped against an owned Newsstand copy (or vice
    versa) — the reported Uncanny X-Men #201 base win incorrectly skipped
    against an owned Newsstand #201. An unrecognized ``variant_text`` (e.g. a
    cover-artist variant like "Capullo Variant") can't be reliably normalized
    against a suffix, so it stays permissive — preserving the pre-existing
    BUI-34 behavior of deduping through cosmetic cover variants.

    Known limitation (safe direction): recognition is by EXACT
    :data:`VARIANT_SUFFIX_MAP` key, so a novel phrasing like
    ``"newsstand variant"`` (not a map key) reads as ``None`` and stays
    permissive — a newsstand win against an owned newsstand row then dedups
    through, at worst producing a duplicate owned row (never hiding a new
    win). The load-bearing direction — a *base* win must NOT dedup against an
    owned Newsstand row — always holds, because the owned row's parsed
    ``candidate_suffix`` ("Newsstand Edition") IS a map key. VARIANT_SUFFIX_MAP
    isn't widened here on purpose: it also feeds the full_title builder
    (see its other consumer), so new keys would change more than this check.
    """
    known_win_suffix = VARIANT_SUFFIX_MAP.get(variant_text) if variant_text else None
    known_candidate_suffix = (
        VARIANT_SUFFIX_MAP.get(candidate_suffix.lower()) if candidate_suffix else None
    )
    if known_win_suffix is None and known_candidate_suffix is None:
        return True
    return known_win_suffix == known_candidate_suffix


def _date_matches_year(date: Optional[str], year_raw: Any) -> bool:
    """True if ``date`` starts with the 4-digit year encoded in ``year_raw``.

    Shared reprint-date check for the BUI-210/268 Metron guards, which only
    accept a looked-up date when its year matches the win's identified year
    (a naive lookup can otherwise return a reprint/collected-edition date).
    False if ``date`` is falsy — callers still validate ``year_raw`` is a
    clean 4-digit year themselves before relying on this, since each site
    gates a different thing (attempting a lookup vs. rejecting a result) on
    that validity.
    """
    if not date:
        return False
    year_str = str(year_raw).strip() if year_raw is not None else ""
    return str(date).startswith(year_str)


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
    candidate_year = _coerce_year(candidate_row.get("release_date"))
    if candidate_year is not None:
        return candidate_year == win_year
    return True


RECORD_WIN_CHUNK_SIZE = 25


def _check_metron_degraded(metron: Any, metron_disabled: bool) -> bool:
    """BUI-255: trip the per-batch Metron breaker on throttle/timeout.

    ``metron.degraded`` is set by ``MetronClient``'s retry decorator (BUI-260,
    exhausted a single capped rate-limit retry; BUI-342, exhausted a single
    capped 5xx retry) or by a connection-error ``ApiError`` — i.e. Metron itself
    signaling "this call failed because I'm throttled/unreachable/erroring",
    never a genuine, exception-free "no match". Once
    tripped, the caller stops calling Metron for the rest of the batch (same
    fallback ``MetronCredentialError`` already uses) instead of repeating a
    capped-but-still-real sleep on every remaining row — a 44-row batch could
    otherwise stack dozens of 60s sleeps and wedge the single-worker server
    that runs this synchronously (see BUI-247's record-win incident).

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
      - "metron_attempted" / "metron_succeeded" / "manual_series" / "manual_variant" /
        "variant_detail_attempted" / "variant_matches": int deltas for this win
    """
    from locg.metron import MetronCredentialError

    metron_attempted = 0
    metron_succeeded = 0
    manual_series = 0
    manual_variant = 0
    variant_detail_attempted = 0
    variant_matches = 0

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

    # BUI-199: resolve the canonical series by issue-number boundary
    # (the LOCG X-Men split) and by year/era (the right volume), not by
    # blindly taking the single series_name_index entry.
    resolved_series = resolve_series_for_win(
        norm_key, issue_num, year_raw, series_name_index, volume_candidates
    )
    if resolved_series is not None:
        canonical_series = resolved_series
    elif not metron_disabled:
        try:
            metron_attempted += 1
            metron_data = metron.lookup_issue(series_raw, issue_num, year_raw)
            if metron_data:
                metron_succeeded += 1
                canonical_series = metron.format_series_name(metron_data)
        except MetronCredentialError:
            metron_disabled = True
            logger.warning("Metron credentials not configured; falling back to manual series resolution.")
        else:
            metron_disabled = _check_metron_degraded(metron, metron_disabled)

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
            "metron_attempted": metron_attempted,
            "metron_succeeded": metron_succeeded,
            "manual_series": manual_series,
            "manual_variant": manual_variant,
            "variant_detail_attempted": variant_detail_attempted,
            "variant_matches": variant_matches,
        }

    # BUI-210: when the series resolved via series_name_index (the common
    # case — metron_data stays None), we still have no release_date, so
    # the row would fall through to the {year}-01-01 placeholder, which
    # the export blanks → the row ships dateless and an all-dateless
    # batch hangs LOCG's importer. Do a Metron *issue* lookup purely to
    # populate a real date; do NOT touch canonical_series (the
    # index-resolved value is more reliable than Metron's
    # format_series_name here). Runs after the BUI-34 dedup continue so
    # we never spend a Metron call on a skipped already-owned win, and
    # before the variant block so variant resolution can reuse metron_id.
    #
    # Reprint guard: a naive lookup_issue("The X-Men", "59", 1970) can
    # return a collected-edition/reprint date (observed: 2005-03-09).
    # Only accept the result if the returned store/cover date's YEAR
    # matches the win's year_raw; otherwise reject it and keep the
    # placeholder fallback below.
    if metron_data is None and issue_num and not metron_disabled:
        year_str = str(year_raw).strip() if year_raw is not None else ""
        if re.fullmatch(r"\d{4}", year_str):
            try:
                metron_attempted += 1
                looked_up = metron.lookup_issue(series_raw, issue_num, year_raw)
                if looked_up:
                    looked_date = (
                        looked_up.get("store_date")
                        or looked_up.get("cover_date")
                    )
                    if _date_matches_year(looked_date, year_raw):
                        metron_succeeded += 1
                        metron_data = looked_up
            except MetronCredentialError:
                metron_disabled = True
                logger.warning(
                    "Metron credentials not configured; falling back to placeholder release date."
                )
            else:
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
        suffix = VARIANT_SUFFIX_MAP.get(variant_text)
        if suffix:
            full_title = f"{base_title} {suffix}"
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
                    variant_detail_attempted += 1
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
                else:
                    metron_disabled = _check_metron_degraded(metron, metron_disabled)

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

    # release_date: prefer store_date, fall back to cover_date.
    #
    # BUI-268: same reprint guard as the BUI-210 date-only lookup below
    # — this metron_data can come from the FIRST Metron call (the R36
    # step-2 series-resolution path a few lines up, used when
    # series_name_index has no entry), which has no year filter of its
    # own. Left unguarded, a reprint/collected-edition store_date
    # (observed: Infinity Gauntlet #1 stamped 2022-09-14 from a 2022
    # reprint hit, despite series_name correctly resolving to "The
    # Infinity Gauntlet (1991) (1991 - 1991)") got written onto an
    # otherwise-correct 1991 row, so a later year-gated
    # collection-check for the real 1991 issue found the row and
    # rejected it as a mismatched era. Only accept the Metron date when
    # its year matches year_raw; a mismatch is dropped (R66: a Metron
    # hit that lacks a trustworthy date stays blank — the relaxed year
    # gate then fail-opens on this row rather than falsely rejecting a
    # genuinely-owned copy).
    release_date: Optional[str] = None
    if metron_data:
        candidate_date = metron_data.get("store_date") or metron_data.get("cover_date")
        year_str = str(year_raw).strip() if year_raw is not None else ""
        if (
            re.fullmatch(r"\d{4}", year_str)
            and candidate_date
            and not _date_matches_year(candidate_date, year_raw)
        ):
            candidate_date = None
        release_date = candidate_date

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

    return {
        "skipped": False,
        "skip_detail": None,
        "row": row,
        "metron_disabled": metron_disabled,
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
    """
    from datetime import datetime, timezone
    from locg.collection_cache import (
        CollectionCache,
        _normalize_series_key,
        build_volume_candidates,
    )
    from locg.metron import MetronClient

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
