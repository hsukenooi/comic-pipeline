#!/usr/bin/env python3
"""ebay-fetch: Fetch structured listing data from eBay Browse API."""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# --- Configuration ---

CONFIG_DIR = Path.home() / ".config" / "ebay-fetch"
CONFIG_FILE = CONFIG_DIR / "config.json"

PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"

_GRADE_ABBREVS = (
    r"NM[\-\+]?|VF[\-\+]?|FN[\-\+]?|VG[\-\+]?|GD[\-\+]?|FR[\-\+]?|PR[\-\+]?"
    r"|Near Mint[\-\+]?|Very Fine[\-\+]?|Fine[\-\+]?"
    r"|NM/M|NM/MT|FN/VF|VG/FN|GD/VG|FR/GD"
    r"|FVF|VF/NM|Gem|CGC"
    r"|[0-9]{1,2}\.[0-9]"
)

# Matches grades inside parentheses: (NM-), (VF+), (9.4)
GRADE_PATTERN = re.compile(
    r"\((" + _GRADE_ABBREVS + r")\)",
    re.IGNORECASE,
)

# Matches bare inline grades: "NM Gem", "VF+ Cond", "FVF beauty", "Fine+"
# Uses word boundary to avoid false positives inside other words.
GRADE_BARE_PATTERN = re.compile(
    r"(?<!\w)(" + _GRADE_ABBREVS + r")(?!\w)",
    re.IGNORECASE,
)

VARIANT_SPECIFICS_KEYS = {"Variant", "Edition", "Printing"}
VARIANT_SPECIFICS_KEYS_LOWER = frozenset(k.lower() for k in VARIANT_SPECIFICS_KEYS)
VARIANT_TITLE_KEYWORDS = [
    "Newsstand", "Direct", "Whitman", "Price Variant",
    "Type 1A", "Type 1B", "Collectors Edition",
]

GRADE_SPECIFICS_KEYS = {"Grade", "CGC Grade", "CBCS Grade", "Condition"}
GRADE_SPECIFICS_KEYS_LOWER = frozenset(k.lower() for k in GRADE_SPECIFICS_KEYS)

_GENERIC_EBAY_CONDITIONS = frozenset({"Brand New", "Like New", "New", "Very Good", "Good", "Acceptable"})


def load_config():
    """Load credentials from config file or environment variables."""
    client_id = os.environ.get("EBAY_CLIENT_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")
    environment = os.environ.get("EBAY_ENVIRONMENT", "production")

    if not client_id or not client_secret:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            client_id = client_id or cfg.get("client_id")
            client_secret = client_secret or cfg.get("client_secret")
            environment = cfg.get("environment", environment)

    if not client_id or not client_secret:
        print(
            "Error: eBay credentials not found.\n"
            f"Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET env vars, or create {CONFIG_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = PRODUCTION_BASE if environment == "production" else SANDBOX_BASE
    return client_id, client_secret, base_url


def _token_cache_file(base_url):
    """Return environment-keyed token cache path."""
    env = "production" if "api.ebay.com" in base_url else "sandbox"
    return CONFIG_DIR / f"token_cache_{env}.json"


def get_token(client_id, client_secret, base_url):
    """Get a valid OAuth app token, using cache if available."""
    cache_file = _token_cache_file(base_url)

    # Check cache
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cache = json.load(f)
            expires_at = cache.get("expires_at", 0)
            if time.time() < expires_at - 300:  # 5-minute buffer
                return cache["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass  # Treat corrupted cache as a cache miss

    # Request new token
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        f"{base_url}/identity/v1/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=10,
    )

    if resp.status_code != 200:
        print(f"Error: Authentication failed ({resp.status_code})", file=sys.stderr)
        sys.exit(1)

    token_data = resp.json()
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 7200)

    # Cache token with restrictive permissions
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(cache_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(
            {"access_token": access_token, "expires_at": time.time() + expires_in},
            f,
        )

    return access_token


def extract_item_id(arg):
    """Extract numeric item ID from a URL or raw ID string."""
    # URL pattern: https://www.ebay.com/itm/298217294954
    m = re.search(r"/itm/(\d+)", arg)
    if m:
        return m.group(1)
    # Raw numeric ID
    if arg.strip().isdigit():
        return arg.strip()
    print(f"Warning: Could not parse item ID from '{arg}', skipping.", file=sys.stderr)
    return None


def fetch_item(item_id, token, base_url, retries=3):
    """Fetch a single item from the Browse API."""
    url = f"{base_url}/buy/browse/v1/item/get_item_by_legacy_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {"legacy_item_id": item_id}

    for attempt in range(retries):
        resp = requests.get(url, headers=headers, params=params, timeout=10)

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            print(f"Error: Item {item_id} not found (404).", file=sys.stderr)
            return None
        elif resp.status_code == 429:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
        else:
            print(
                f"Error fetching item {item_id}: HTTP {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
            return None

    print(f"Error: Failed to fetch item {item_id} after {retries} retries.", file=sys.stderr)
    return None


def _grade_from_text(text):
    """Try to extract a comic grade from arbitrary text.

    Returns the matched grade string or None.
    """
    if not text:
        return None
    # Prefer parenthetical grades (more intentional)
    m = GRADE_PATTERN.search(text)
    if m:
        return m.group(1)
    # Fall back to bare inline grades
    m = GRADE_BARE_PATTERN.search(text)
    if m:
        return m.group(1)
    return None


def extract_grade(item_specifics, title, description=None):
    """Extract grade from item specifics, title, or description."""
    # Check item specifics first
    for spec in item_specifics:
        if spec.get("name", "").strip().lower() in GRADE_SPECIFICS_KEYS_LOWER:
            val = spec.get("value")
            if val:
                return val, "item_specifics", None

    # Parse from title
    grade = _grade_from_text(title)
    if grade:
        return grade, "title", None

    # Parse from description as last resort
    grade = _grade_from_text(description)
    if grade:
        return None, "missing", grade

    return None, "missing", None


def extract_variant(item_specifics, title):
    """Extract variant from item specifics or title."""
    # Check item specifics
    for spec in item_specifics:
        if spec.get("name", "").strip().lower() in VARIANT_SPECIFICS_KEYS_LOWER:
            val = spec.get("value")
            if val:
                return val

    # Scan title
    title_upper = title.upper()
    for kw in VARIANT_TITLE_KEYWORDS:
        if kw.upper() in title_upper:
            return kw

    return None


def format_end_date(iso_str):
    """Convert ISO 8601 date to local time formatted string."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str


def parse_item(data):
    """Parse Browse API response into structured output."""
    item_id_raw = data.get("itemId", "")
    # Strip v1|...|0 wrapper
    m = re.search(r"\|(\d+)\|", item_id_raw)
    item_id = m.group(1) if m else item_id_raw

    title = data.get("title", "")
    buying_options = data.get("buyingOptions", [])

    # Listing type
    if "AUCTION" in buying_options:
        listing_type = "Auction"
    elif "FIXED_PRICE" in buying_options:
        listing_type = "BIN"
    else:
        listing_type = ", ".join(buying_options) if buying_options else "Unknown"

    # Price
    if listing_type == "Auction" and "currentBidPrice" in data:
        price_data = data["currentBidPrice"]
    else:
        price_data = data.get("price", {})

    price_value = price_data.get("value", "0")
    currency = price_data.get("currency", "USD")
    currency_symbol = "$" if currency == "USD" else currency + " "
    try:
        current_price = f"{currency_symbol}{float(price_value):.2f}"
    except (ValueError, TypeError):
        current_price = f"{currency_symbol}{price_value}"

    bid_count = data.get("bidCount") if listing_type == "Auction" else None

    end_date = format_end_date(data.get("itemEndDate"))

    condition = data.get("condition", None)
    condition_id = data.get("conditionId", None)

    item_specifics_raw = data.get("localizedAspects", [])
    item_specifics = {s.get("name", ""): s.get("value") for s in item_specifics_raw if s.get("name")}

    description_snippet = data.get("shortDescription")
    if description_snippet and len(description_snippet) > 500:
        description_snippet = description_snippet[:500]

    grade, grade_source, grade_from_description = extract_grade(
        item_specifics_raw, title, description_snippet,
    )
    variant = extract_variant(item_specifics_raw, title)

    # eBay's generic condition labels (e.g. "Brand New", "Like New") are
    # misleading for collectibles like comics where actual grading applies.
    # Suppress them when a real grade is available or when the generic label
    # clearly doesn't match a vintage/used item.
    condition_note = None
    if condition in _GENERIC_EBAY_CONDITIONS:
        condition_note = "eBay category label, not comic grade"

    listing_url = data.get("itemWebUrl", f"https://www.ebay.com/itm/{item_id}")

    seller_data = data.get("seller", {})
    seller = seller_data.get("username") if isinstance(seller_data, dict) else None

    end_date_iso = data.get("itemEndDate")  # raw ISO 8601 with timezone, e.g. "2025-05-01T12:34:56.000Z"

    return {
        "item_id": item_id,
        "title": title,
        "listing_type": listing_type,
        "current_price": current_price,
        "bid_count": bid_count,
        "end_date": end_date,
        "end_date_iso": end_date_iso,
        "condition": condition,
        "condition_id": condition_id,
        "condition_note": condition_note,
        "grade": grade,
        "grade_source": grade_source,
        "grade_from_description": grade_from_description,
        "variant": variant,
        "item_specifics": item_specifics,
        "description_snippet": description_snippet,
        "listing_url": listing_url,
        "seller": seller,
    }


def _extract_seller_username(arg):
    """Normalize an eBay store/user URL or raw username to a plain username."""
    # https://www.ebay.com/usr/beatlebluecat or /str/beatlebluecat
    m = re.search(r"/(?:usr|str)/([^/?&]+)", arg)
    if m:
        return m.group(1)
    return arg.strip()


def parse_item_summary(item):
    """Parse a Browse API itemSummary into structured output.

    itemSummary (from search results) differs from a full item detail:
    no localizedAspects, price/currentBidPrice are top-level dicts.
    """
    item_id_raw = item.get("itemId", "")
    m = re.search(r"\|(\d+)\|", item_id_raw)
    item_id = m.group(1) if m else item_id_raw

    title = item.get("title", "")
    buying_options = item.get("buyingOptions", [])

    if "AUCTION" in buying_options:
        listing_type = "Auction"
        price_data = item.get("currentBidPrice") or item.get("price", {})
    elif "FIXED_PRICE" in buying_options:
        listing_type = "BIN"
        price_data = item.get("price", {})
    else:
        listing_type = ", ".join(buying_options) if buying_options else "Unknown"
        price_data = item.get("price", {})

    price_val = price_data.get("value", "0") if isinstance(price_data, dict) else "0"
    currency = price_data.get("currency", "USD") if isinstance(price_data, dict) else "USD"
    currency_symbol = "$" if currency == "USD" else currency + " "
    try:
        current_price = f"{currency_symbol}{float(price_val):.2f}"
    except (ValueError, TypeError):
        current_price = f"{currency_symbol}{price_val}"

    end_date = format_end_date(item.get("itemEndDate"))
    end_date_iso = item.get("itemEndDate")

    seller_data = item.get("seller", {})
    seller = seller_data.get("username") if isinstance(seller_data, dict) else None

    return {
        "item_id": item_id,
        "title": title,
        "listing_type": listing_type,
        "current_price": current_price,
        "end_date": end_date,
        "end_date_iso": end_date_iso,
        "listing_url": item.get("itemWebUrl", f"https://www.ebay.com/itm/{item_id}"),
        "seller": seller,
    }


def search_seller_listings(seller, token, base_url, *, app_id=None, max_results=1000, retries=3):
    """Fetch active auction listings from a seller via the eBay Finding API.

    Uses findItemsIneBayStores so the seller arg can be a store name or username.
    The Browse API sellers filter requires an exact username and is unreliable;
    findItemsIneBayStores accepts the store name directly.
    Returns a list of Browse-API-shaped dicts compatible with parse_item_summary.
    """
    store_name = _extract_seller_username(seller)

    if "sandbox" in base_url:
        find_url = "https://svcs.sandbox.ebay.com/services/search/FindingService/v1"
    else:
        find_url = "https://svcs.ebay.com/services/search/FindingService/v1"

    all_items = []
    page = 1
    page_size = min(100, max_results)

    while True:
        params = {
            "OPERATION-NAME": "findItemsIneBayStores",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "storeName": store_name,
            "itemFilter(0).name": "ListingType",
            "itemFilter(0).value": "Auction",
            "paginationInput.entriesPerPage": page_size,
            "paginationInput.pageNumber": page,
        }

        for attempt in range(retries):
            resp = requests.get(find_url, params=params, timeout=15)
            if resp.status_code == 200:
                break
            if resp.status_code == 429 and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                print(
                    f"Error fetching seller listings: HTTP {resp.status_code}: {resp.text[:200]}",
                    file=sys.stderr,
                )
                return all_items

        data = resp.json()
        response = data.get("findItemsIneBayStoresResponse", [{}])[0]
        ack = response.get("ack", [""])[0]
        if ack not in ("Success", "Warning"):
            err = (response.get("errorMessage", [{}])[0]
                      .get("error", [{}])[0]
                      .get("message", ["unknown"])[0])
            print(f"Finding API error: {err}", file=sys.stderr)
            return all_items

        search_result = response.get("searchResult", [{}])[0]
        raw_items = search_result.get("item", [])

        for item in raw_items:
            all_items.append(_finding_item_to_browse(item, store_name))

        pagination = response.get("paginationOutput", [{}])[0]
        total_pages = int(pagination.get("totalPages", ["1"])[0])

        if page >= total_pages or len(all_items) >= max_results or not raw_items:
            break
        page += 1

    return all_items[:max_results]


def _finding_item_to_browse(item, seller_username):
    """Reshape a Finding API item dict into a Browse API itemSummary-shaped dict."""
    item_id = item.get("itemId", [""])[0]
    title = item.get("title", [""])[0]
    url = item.get("viewItemURL", [f"https://www.ebay.com/itm/{item_id}"])[0]

    listing_info = item.get("listingInfo", [{}])[0]
    listing_type = listing_info.get("listingType", ["Unknown"])[0]
    end_time = listing_info.get("endTime", [None])[0]

    selling = item.get("sellingStatus", [{}])[0]
    price_data = selling.get("currentPrice", [{}])[0]
    price_val = price_data.get("__value__", "0")
    currency = price_data.get("@currencyId", "USD")

    # Finding API returns "Chinese" for a plain auction and "AuctionWithBIN" for an
    # auction that still has a live Buy It Now (it flips to "Chinese" once a bid lands).
    # "Auction" is the request-side itemFilter value; keep it for defensiveness.
    auction_types = ("Chinese", "AuctionWithBIN", "Auction")
    buying_options = ["AUCTION"] if listing_type in auction_types else ["FIXED_PRICE"]

    price_dict = {"value": price_val, "currency": currency}
    return {
        "itemId": item_id,
        "title": title,
        "buyingOptions": buying_options,
        "currentBidPrice": price_dict if "AUCTION" in buying_options else None,
        "price": price_dict,
        "itemEndDate": end_time,
        "itemWebUrl": url,
        "seller": {"username": seller_username},
    }


def truncate(text, width):
    """Truncate text to width with ellipsis."""
    if not text:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "\u2026"


def print_table(items, fields=None):
    """Print items as a human-readable table."""
    if not items:
        print("No items to display.")
        return

    # Define columns: (header, key, width)
    all_columns = [
        ("#", "_index", 3),
        ("Item ID", "item_id", 14),
        ("Title", "title", 45),
        ("Grade", "grade", 8),
        ("Variant", "variant", 12),
        ("Type", "listing_type", 7),
        ("Price", "current_price", 10),
        ("Ends", "end_date", 12),
    ]

    if fields:
        field_set = {f.strip() for f in fields}
        columns = [c for c in all_columns if c[1] in field_set or c[1] == "_index"]
    else:
        columns = all_columns

    # Header
    header = "  ".join(col[0].ljust(col[2]) for col in columns)
    print(header)

    for i, item in enumerate(items, 1):
        warnings = []
        if item.get("grade") is None:
            warnings.append("Grade not stated")
        if item.get("listing_type") == "BIN":
            warnings.append("BIN")

        row_parts = []
        for _header, key, width in columns:
            if key == "_index":
                row_parts.append(str(i).ljust(width))
            else:
                val = item.get(key)
                if val is None:
                    val = "\u2014"
                else:
                    val = str(val)
                row_parts.append(truncate(val, width).ljust(width))

        line = "  ".join(row_parts)
        if warnings:
            line += "  \u26a0\ufe0f " + ", ".join(warnings)
        print(line)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch structured listing data from eBay.",
        prog="ebay-fetch",
    )
    parser.add_argument(
        "items",
        nargs="*",
        help="eBay item IDs or URLs",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON array",
    )
    parser.add_argument(
        "--fields",
        type=str,
        default=None,
        help="Comma-separated fields to include",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )

    args = parser.parse_args()

    # Collect item args from CLI and stdin
    raw_items = list(args.items) if args.items else []
    if not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line:
                raw_items.append(line)

    if not raw_items:
        parser.print_help()
        sys.exit(1)

    # Extract item IDs
    item_ids = []
    for arg in raw_items:
        item_id = extract_item_id(arg)
        if item_id:
            item_ids.append(item_id)

    if not item_ids:
        print("Error: No valid item IDs found.", file=sys.stderr)
        sys.exit(1)

    # Auth
    client_id, client_secret, base_url = load_config()
    if args.env:
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE

    token = get_token(client_id, client_secret, base_url)

    # Fetch items
    results = []
    for item_id in item_ids:
        data = fetch_item(item_id, token, base_url)
        if data:
            parsed = parse_item(data)
            results.append(parsed)

    # Output
    if args.json_output:
        # Filter fields for JSON output only
        if args.fields:
            field_set = {f.strip() for f in args.fields.split(",")}
            results = [{k: v for k, v in item.items() if k in field_set} for item in results]
        print(json.dumps(results, indent=2))
    else:
        # Table mode: pass full results, let print_table handle column filtering
        print_table(results, fields=args.fields.split(",") if args.fields else None)


if __name__ == "__main__":
    main()
