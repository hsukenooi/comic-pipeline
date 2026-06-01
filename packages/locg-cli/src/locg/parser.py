"""HTML parsing helpers for League of Comic Geeks responses."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag


def parse_list_response(text: str) -> tuple[int, BeautifulSoup]:
    """Parse a JSON response from /comic/get_comics.

    Returns (count, soup of the HTML list).
    """
    data = json.loads(text)
    count = int(data.get("count", 0))
    html = data.get("list", "")
    soup = BeautifulSoup(html, "html.parser")
    return count, soup


def parse_page(html: str) -> BeautifulSoup:
    """Parse a full HTML page."""
    return BeautifulSoup(html, "html.parser")


def extract_id_from_href(href: str) -> Optional[int]:
    """Extract numeric ID from a URL like /comic/9559460/batman-8."""
    m = re.search(r"/(\d+)/", href)
    return int(m.group(1)) if m else None


def extract_price(text: str) -> Optional[float]:
    """Extract price from text like ' · $4.99'."""
    m = re.search(r"\$(\d+\.?\d*)", text)
    return float(m.group(1)) if m else None


def extract_date_timestamp(tag: Tag) -> Optional[str]:
    """Extract date from a span with data-date attribute (unix timestamp)."""
    import datetime
    date_el = tag.find(class_="date")
    if date_el and date_el.get("data-date"):
        ts = int(date_el["data-date"])
        return datetime.date.fromtimestamp(ts).isoformat()
    # Fallback: parse text
    if date_el:
        return date_el.get_text(strip=True)
    return None


def get_text_clean(tag: Optional[Tag]) -> str:
    """Get cleaned text from a tag, or empty string if None."""
    if tag is None:
        return ""
    return tag.get_text(strip=True)
