"""Extract structured data from LOCG HTML."""
from __future__ import annotations

import re
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag

from locg.parser import (
    extract_date_timestamp,
    extract_id_from_href,
    extract_price,
    get_text_clean,
)

BASE_URL = "https://leagueofcomicgeeks.com"

# Mapping from LOCG list IDs to human-readable names.
_LIST_ID_TO_NAME = {1: "pull", 2: "collection", 3: "wish", 5: "read"}


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert a value to int, returning default if empty or invalid."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_list_membership(tag: Tag) -> Optional[dict[str, bool]]:
    """Parse comic-controller spans to determine list membership.

    Returns a dict like ``{"pull": False, "collection": True, ...}`` when
    authenticated (i.e. spans carry ``data-list`` attributes), or ``None``
    when unauthenticated.
    """
    controllers = tag.find_all(class_="comic-controller")
    has_list_data = any(c.get("data-list") for c in controllers)
    if not has_list_data:
        return None
    lists: dict[str, bool] = {name: False for name in _LIST_ID_TO_NAME.values()}
    for ctrl in controllers:
        data_list = ctrl.get("data-list")
        if data_list is not None:
            list_id = _safe_int(data_list)
            list_name = _LIST_ID_TO_NAME.get(list_id)
            if list_name:
                classes = ctrl.get("class", [])
                lists[list_name] = "active" in classes
    return lists


def extract_issue(li: Tag) -> dict[str, Any]:
    """Extract issue data from an <li class="issue"> in a releases/list response."""
    comic_id = _safe_int(li.get("data-comic"))
    pulls = _safe_int(li.get("data-pulls"))
    potw = _safe_int(li.get("data-potw"))
    community = _safe_int(li.get("data-community"))

    # Title and URL
    title_div = li.find("div", class_="title")
    title_link = title_div.find("a") if title_div else None
    name = get_text_clean(title_link)
    url = title_link["href"] if title_link and title_link.get("href") else ""

    # Publisher
    publisher = get_text_clean(li.find("div", class_="publisher"))

    # Cover image
    img = li.find("img", class_="lazy")
    cover_url = img.get("data-src", "") if img else ""

    # Date and price from details div
    details = li.find("div", class_="details")
    store_date = ""
    price = None
    if details:
        store_date = extract_date_timestamp(details) or ""
        price_span = details.find("span", class_="price")
        if price_span:
            price = extract_price(price_span.get_text())

    return {
        "id": comic_id,
        "name": name,
        "publisher": publisher,
        "price": price,
        "store_date": store_date,
        "cover_url": cover_url,
        "pulls": pulls,
        "potw": potw,
        "community_rating": community,
        "url": f"{BASE_URL}{url}" if url else "",
        "lists": _parse_list_membership(li),
    }


def extract_series(li: Tag) -> dict[str, Any]:
    """Extract series data from a <li> in a search series response.

    Search results are heterogeneous: when the query matches a series name
    LOCG returns a series-style ``<li>`` with a ``link-collection-series``
    anchor carrying ``data-id``. When the query matches a specific issue
    (e.g. "Amazing Spider-Man #229") LOCG instead returns an issue-style
    ``<li class="issue" data-comic="...">``. We accept both shapes and put
    the relevant numeric ID (series ID or comic ID) into the ``id`` field
    so callers always get a usable identifier.
    """
    # Series link and ID
    link = li.find("a", class_="link-collection-series")
    series_id = int(link.get("data-id", 0)) if link else 0
    url = link["href"] if link and link.get("href") else ""

    # Issue-style result fallback: <li class="issue" data-comic="...">
    # carries the comic ID rather than a series ID. Use it when no
    # link-collection-series anchor was present.
    if series_id == 0:
        comic_id_raw = li.get("data-comic")
        if comic_id_raw:
            series_id = _safe_int(comic_id_raw)

    # Title
    title_div = li.find("div", class_="title")
    title_link = title_div.find("a") if title_div else None
    name = get_text_clean(title_link) if title_link else get_text_clean(title_div)
    # When url wasn't set from a series link, fall back to the title link's href
    if not url and title_link and title_link.get("href"):
        url = title_link["href"]

    # Cover image
    img = li.find("img", class_="lazy")
    cover_url = img.get("data-src", "") if img else ""

    # Issue count
    count_span = li.find("span", class_="count-issues")
    issue_count = 0
    if count_span:
        text = count_span.get_text(strip=True)
        if text.isdigit():
            issue_count = int(text)

    # Publisher and years from the info line
    info_div = li.find("div", class_="copy-really-small")
    publisher = ""
    years = ""
    if info_div:
        spans = info_div.find_all("span")
        if spans:
            publisher = get_text_clean(spans[0])
        if len(spans) > 1:
            years = get_text_clean(spans[1]).strip("· ").strip()

    start_year = None
    end_year = None
    if years:
        m = re.match(r"(\d{4})\s*-\s*(\d{4})?", years)
        if m:
            start_year = int(m.group(1))
            end_year = int(m.group(2)) if m.group(2) else None
        elif years.isdigit():
            start_year = int(years)

    return {
        "id": series_id,
        "name": name,
        "publisher": publisher,
        "start_year": start_year,
        "end_year": end_year,
        "issue_count": issue_count,
        "cover_url": cover_url,
        "url": f"{BASE_URL}{url}" if url else "",
    }


def extract_comic_lists(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract only comic ID, name, and list membership from a comic detail page.

    This is a lightweight alternative to :func:`extract_comic_detail` for
    batch list-membership checks where we don't need full comic metadata.
    """
    result: dict[str, Any] = {}

    # Title
    h1 = soup.find("h1")
    result["name"] = get_text_clean(h1)

    # ID from canonical URL
    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href"):
        result["id"] = extract_id_from_href(canon["href"])

    # List membership (user-specific, requires authentication)
    result["lists"] = _parse_list_membership(soup)

    return result


def extract_comic_detail(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract full comic details from a comic detail page."""
    result: dict[str, Any] = {}

    # Title
    h1 = soup.find("h1")
    result["name"] = get_text_clean(h1)

    # ID from canonical URL
    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href"):
        result["id"] = extract_id_from_href(canon["href"])
        result["url"] = canon["href"]

    # Publisher and release date from header-intro
    header_intro = soup.find("div", class_="header-intro")
    if header_intro:
        pub_link = header_intro.find("a")
        result["publisher"] = get_text_clean(pub_link) if pub_link else ""
        # Release date link
        date_links = header_intro.find_all("a")
        for dl in date_links:
            if dl.get("href", "").startswith("/comics/new-comics/"):
                result["store_date"] = get_text_clean(dl)
                break

    # Format, pages, price from the col div
    for div in soup.find_all("div", class_="col"):
        text = div.get_text()
        if "$" in text and "page" in text.lower():
            parts = [p.strip() for p in text.split("·")]
            if parts:
                result["format"] = parts[0].strip()
            if len(parts) > 1:
                m = re.search(r"(\d+)\s*page", parts[1])
                if m:
                    result["pages"] = int(m.group(1))
            result["price"] = extract_price(text)
            break

    # Additional details (cover date, UPC, SKU, FOC)
    for block in soup.find_all(class_="details-addtl-block"):
        name_el = block.find(class_="name")
        value_el = block.find(class_="value")
        if name_el and value_el:
            key = get_text_clean(name_el).lower().replace(" ", "_")
            value = get_text_clean(value_el)
            if key == "cover_date":
                result["cover_date"] = value
            elif key == "upc":
                result["upc"] = value
            elif key == "distributor_sku":
                result["sku"] = value
            elif key == "final_order_cutoff":
                # Strip material icon text artifacts
                foc = re.sub(r"event_busy\s*", "", value).strip()
                result["foc_date"] = foc

    # Description from og:description meta tag
    og_desc = soup.find("meta", property="og:description")
    if og_desc:
        result["description"] = og_desc.get("content", "")

    # Cover image from og:image
    og_img = soup.find("meta", property="og:image")
    if og_img:
        result["cover_url"] = og_img.get("content", "")

    # Series link
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.match(r"/comics/series/\d+/", href) and "submit" not in href:
            result["series_id"] = extract_id_from_href(href)
            result["series_name"] = get_text_clean(a) if get_text_clean(a) != "Series" else result.get("name", "").split("#")[0].strip()
            result["series_url"] = f"{BASE_URL}{href}"
            break

    # Creators with roles
    creators = []
    for role_el in soup.find_all(class_="role"):
        role = get_text_clean(role_el)
        sibling = role_el.find_next_sibling()
        if sibling:
            name = get_text_clean(sibling)
            if name:
                creators.append({"role": role, "name": name})
    result["creators"] = creators

    # Community score
    score_el = soup.find(class_="comic-score")
    if score_el:
        stat = score_el.find(class_="stat")
        if stat:
            try:
                result["community_rating"] = float(get_text_clean(stat))
            except ValueError:
                pass
        text_el = score_el.find(class_="text")
        if text_el:
            text = get_text_clean(text_el)
            m = re.search(r"([\d,]+)\s*Rating", text)
            if m:
                result["rating_count"] = int(m.group(1).replace(",", ""))

    # List membership (user-specific, requires authentication)
    result["lists"] = _parse_list_membership(soup)

    return result


# Fields from the #my-details tab that POST /comic/post_my_details will
# accept. Missing fields on the POST get wiped to server defaults, so
# extract_my_details captures every one we want to round-trip.
_MY_DETAILS_FIELDS = (
    "comic_id", "copy_num", "quantity", "date_purchased", "price_paid",
    "purchase_store", "media", "signature", "storage_box", "slabbing",
    "grading", "grading_company", "condition", "notes", "owner",
)


def extract_my_details(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the currently-stored My Details form values from a comic page.

    Reads the ``data-initial`` attribute on each named form field in the
    ``#my-details`` tab. The HTML template bakes in defaults
    (e.g. the ``grading`` select always marks "None" as selected regardless
    of true state), so the visible ``value``/``selected`` markup is unreliable.
    Only ``data-initial`` reflects the stored server state.

    Missing fields are returned as empty strings so that a subsequent
    round-trip POST does not inadvertently wipe them.
    """
    container = soup.find(id="my-details")
    if container is None:
        return {field: "" for field in _MY_DETAILS_FIELDS}

    result: dict[str, str] = {}
    for field in _MY_DETAILS_FIELDS:
        tag = container.find(attrs={"name": field})
        if tag is None:
            result[field] = ""
            continue
        # data-initial wins. Fall back to value (for <input>) or text (for
        # <textarea>) only when data-initial is entirely absent.
        initial = tag.get("data-initial")
        if initial is None:
            if tag.name == "textarea":
                initial = tag.get_text()
            else:
                initial = tag.get("value", "")
        result[field] = initial
    return result
