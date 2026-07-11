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
from urllib.parse import quote

# --- Configuration ---

CONFIG_DIR = Path.home() / ".config" / "ebay-fetch"
CONFIG_FILE = CONFIG_DIR / "config.json"
# Maps eBay *store names* (what a human types) to the seller's login *username*
# (what the Browse API filter actually needs). See BUI-68.
# Committed to the repo (next to the modules, so it resolves in both the dev
# checkout and the installed wheel where `sources=["src"]` flattens the layout)
# so the seller list travels with the code — no per-machine setup.
SELLER_ALIASES_FILE = Path(__file__).resolve().parent / "seller_aliases.json"

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
        except Exception:  # noqa: BLE001  # malformed/wrong-shape cache → cache miss
            pass  # e.g. non-dict JSON, non-numeric expires_at, missing access_token

    # Request new token — bounded retry loop mirrors the pattern in fetch_item().
    # 429 (rate-limited) and 5xx (transient server errors) are retried with
    # exponential backoff. Non-retryable 4xx errors (e.g. 401 bad credentials)
    # exit immediately. BUI-184: a one-shot sys.exit on the first non-200 killed
    # the whole run on a transient auth hiccup.
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    retries = 3
    for attempt in range(retries):
        try:
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
        except requests.exceptions.RequestException as exc:
            # Transient network error — same retry/backoff budget as 429/5xx below.
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(
                    f"Network error requesting token: {exc}, retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            print(
                f"Error: Authentication failed (network error after {retries} attempts): {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if resp.status_code == 200:
            break
        elif resp.status_code == 429 or resp.status_code >= 500:
            # Transient — back off and retry if budget remains.
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(
                    f"Token request failed ({resp.status_code}), retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                print(
                    f"Error: Authentication failed ({resp.status_code}) after {retries} attempts",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            # Non-retryable 4xx (bad credentials, etc.) — exit immediately.
            print(f"Error: Authentication failed ({resp.status_code})", file=sys.stderr)
            sys.exit(1)

    # eBay can return 200 with a malformed body (truncated proxy response, a WAF
    # interstitial served with a 200 status) — guard the same way get_item_aspects()
    # guards resp.json() below.
    try:
        token_data = resp.json()
        access_token = token_data["access_token"]
    except (ValueError, KeyError) as exc:
        print(f"Error: Malformed token response from eBay: {exc}", file=sys.stderr)
        sys.exit(1)
    expires_in = token_data.get("expires_in", 7200)

    # Cache token with restrictive permissions. Best-effort: an OSError here
    # (e.g. disk full, permission denied) must not discard the token we already
    # got from eBay — log and fall through, still returning the live token.
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(cache_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"access_token": access_token, "expires_at": time.time() + expires_in},
                f,
            )
    except OSError as exc:
        print(f"Warning: could not write token cache: {exc}", file=sys.stderr)

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
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
        except requests.exceptions.RequestException as exc:
            print(f"Network error fetching item {item_id}: {exc}", file=sys.stderr)
            return None

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                print(f"Error: Malformed response for item {item_id}: {exc}", file=sys.stderr)
                return None
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
    """Normalize an eBay store/user URL or raw username to a plain token.

    Low-level: just pulls a token out of a URL. It does NOT distinguish a real
    login username from a store slug — that judgement lives in
    resolve_seller_username(). Used by search_seller_listings to know which
    seller to filter/verify against once a clean value has been chosen.
    """
    # A seller-search URL carries the real login username in _ssn=
    m = re.search(r"[?&]_ssn=([^&]+)", arg)
    if m:
        return m.group(1)
    # https://www.ebay.com/usr/beatlebluecat or /str/beatlebluecat
    m = re.search(r"/(?:usr|str)/([^/?&]+)", arg)
    if m:
        return m.group(1)
    return arg.strip()


class UnknownSellerError(Exception):
    """Raised when a store name can't be resolved to an eBay login username."""

    def __init__(self, store):
        self.store = store
        super().__init__(store)


def load_seller_aliases():
    """Load the store-name → username map. Returns {} if the file is absent.

    Keys are lowercased so lookups are case-insensitive.
    """
    if not SELLER_ALIASES_FILE.exists():
        return {}
    try:
        with open(SELLER_ALIASES_FILE) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if v}


def save_seller_alias(store, username):
    """Add/update one store-name → username mapping and persist it."""
    aliases = {}
    if SELLER_ALIASES_FILE.exists():
        try:
            with open(SELLER_ALIASES_FILE) as f:
                aliases = json.load(f)
        except (json.JSONDecodeError, OSError):
            aliases = {}
    aliases[store.strip().lower()] = username.strip()
    SELLER_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SELLER_ALIASES_FILE, "w") as f:
        json.dump(aliases, f, indent=2, sort_keys=True)
    return aliases


def _classify_seller_input(raw):
    """Return (value, kind): kind is 'username' (a trustworthy login name) or
    'store' (a store name/slug that must be resolved via the alias map).

    An eBay *store name* is NOT a seller username — the Browse API silently
    rejects it and returns every seller's listings (BUI-68). Only values we
    know to be real usernames (/usr/ paths, _ssn= query params) are trusted.
    """
    s = raw.strip()
    if re.search(r"[?&]_ssn=([^&]+)", s):
        return re.search(r"[?&]_ssn=([^&]+)", s).group(1), "username"
    m = re.search(r"/usr/([^/?&]+)", s)
    if m:
        return m.group(1), "username"
    m = re.search(r"/str/([^/?&]+)", s)
    if m:
        return m.group(1), "store"
    return s, "store"


def resolve_seller_username(seller, aliases, *, username_override=None):
    """Resolve a user-supplied seller arg to an eBay login username.

    - username_override (from --username) is trusted verbatim.
    - /usr/ and _ssn= URLs carry a real username and are trusted.
    - Everything else is treated as a store name and looked up in `aliases`;
      an unknown store raises UnknownSellerError rather than silently scanning
      every seller.
    """
    if username_override:
        return username_override.strip()
    value, kind = _classify_seller_input(seller)
    if kind == "username":
        return value
    key = value.lower()
    if key in aliases:
        return aliases[key]
    raise UnknownSellerError(value)


def parse_item_summary(item):
    """Parse a Browse API itemSummary into structured output.

    itemSummary (from search results) differs from a full item detail:
    no localizedAspects, price/currentBidPrice are top-level dicts.
    """
    item_id_raw = item.get("itemId", "")
    m = re.search(r"\|(\d+)\|", item_id_raw)
    item_id = m.group(1) if m else item_id_raw

    # BUI-184: use `or ""` rather than `.get("title", "")` so that an explicit
    # null value in the API response ("title": null) is coerced to an empty
    # string at the source — preventing AttributeError on .lower() downstream.
    title = item.get("title") or ""
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


def _seller_filter_rejected(data):
    """True if eBay's response warns that the sellers filter was invalid.

    When the filter is rejected, eBay falls back to returning *all* sellers'
    listings — the BUI-68 bug. We detect that and abort instead.
    """
    for w in data.get("warnings", []):
        msg = f"{w.get('message', '')} {w.get('longMessage', '')}".lower()
        if "seller" in msg and "invalid" in msg:
            return True
    return False


def _filter_by_seller(items, expected_username):
    """Keep only itemSummaries whose seller matches expected_username.

    Belt-and-suspenders against a silently-dropped sellers filter: even if the
    Browse API ever falls back to all sellers, we never surface someone else's
    listing (which could lead to a bad snipe).
    """
    expected = expected_username.lower()
    out = []
    for it in items:
        s = it.get("seller") or {}
        uname = s.get("username") if isinstance(s, dict) else None
        if uname and uname.lower() == expected:
            out.append(it)
    return out


def search_seller_listings(seller, token, base_url, *, max_results=1000, retries=3):
    """Fetch a seller's active auction listings via Browse API item_summary/search.

    Paginates automatically. Returns raw itemSummary dicts, filtered to the
    target seller. `seller` should be a resolved login username (or a /usr/ or
    _ssn= URL); a bare store name will not match eBay's seller filter — resolve
    it via resolve_seller_username() first.
    """
    username = _extract_seller_username(seller)
    url = f"{base_url}/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    all_items = []
    offset = 0
    page_size = 200
    # Braces in the filter must reach eBay literally. requests percent-encodes
    # dict params ({ -> %7B), which eBay silently rejects, dropping the filter
    # (BUI-68). Build the query string by hand, encoding only the username.
    safe_user = quote(username, safe="")

    while True:
        filter_val = f"sellers:{{{safe_user}}},buyingOptions:{{AUCTION}}"
        query = f"q=comic&filter={filter_val}&limit={page_size}&offset={offset}"

        for attempt in range(retries):
            resp = requests.get(url, headers=headers, params=query, timeout=15)
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
                return _filter_by_seller(all_items, username)

        data = resp.json()
        if _seller_filter_rejected(data):
            print(
                f"Error: eBay rejected the seller filter for '{username}' "
                "(not a valid eBay login username). Aborting to avoid returning "
                "other sellers' listings. Find the username via the seller's "
                "'See other items' URL (_ssn= value) and pass --username, or "
                "register it with --add-alias.",
                file=sys.stderr,
            )
            return []

        page_items = data.get("itemSummaries", [])
        all_items.extend(page_items)
        offset += len(page_items)
        total = data.get("total", 0)

        if not page_items or offset >= total or offset >= max_results:
            break

    kept = _filter_by_seller(all_items, username)
    dropped = len(all_items) - len(kept)
    if dropped:
        print(
            f"  ⚠️  Dropped {dropped} listing(s) from other sellers "
            "(seller filter mismatch)",
            file=sys.stderr,
        )
    return kept


def search_by_keyword(keyword, token, base_url, *, max_results=500, buying_options="AUCTION|FIXED_PRICE", retries=3):
    """Search eBay Browse API by keyword, returning parsed item summaries.

    The item→sellers counterpart to search_seller_listings(): instead of
    filtering by seller, this searches across all sellers by keyword. Returns
    a list of dicts from parse_item_summary(), each already carrying the
    `seller` field. Useful for cross-seller wish-list scans.

    Encoding: the keyword is URL-encoded via quote(keyword, safe="") so that
    special characters like '#' (URL fragment separator) and spaces survive the
    query string intact ('#129' → '%23129'; ' ' → '%20'). The buyingOptions
    filter braces must reach eBay literally — passing a dict to requests would
    percent-encode them and eBay would silently reject the filter (same BUI-68
    issue as search_seller_listings). The query string is therefore built by hand,
    encoding only the keyword.

    Paginates in pages of up to 200 until results exhausted or max_results
    reached. Sleeps 2 s after each successful page to respect eBay's ~1 call/2 s
    recommendation across potentially hundreds of keyword searches.
    """
    url = f"{base_url}/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    safe_kw = quote(keyword, safe="")
    all_items = []
    offset = 0
    page_size = 200

    while True:
        filter_val = f"buyingOptions:{{{buying_options}}}"
        query = f"q={safe_kw}&filter={filter_val}&limit={page_size}&offset={offset}"

        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers, params=query, timeout=15)
            except requests.exceptions.RequestException as exc:
                print(
                    f"Network error searching by keyword '{keyword}': {exc}",
                    file=sys.stderr,
                )
                return all_items[:max_results]
            if resp.status_code == 200:
                break
            if resp.status_code == 429 and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                print(
                    f"Error searching by keyword: HTTP {resp.status_code}: {resp.text[:200]}",
                    file=sys.stderr,
                )
                return all_items[:max_results]

        data = resp.json()
        page_items = data.get("itemSummaries", [])
        all_items.extend(parse_item_summary(item) for item in page_items)
        offset += len(page_items)
        total = data.get("total", 0)

        time.sleep(2)

        if not page_items or offset >= total or offset >= max_results:
            break

    return all_items[:max_results]


# ─── Aspects disk cache (BUI-229) ─────────────────────────────────────────────
# Per-item disk cache for localizedAspects (get_item_by_legacy_id responses).
# Keyed by numeric item_id; 7-day TTL matches the search-cache default — aspects
# data is stable for in-flight listings.

_ASPECTS_CACHE_DIR: Path = Path.home() / ".cache" / "ebay-fetch" / "aspects"
_ASPECTS_CACHE_TTL_SEC: int = 7 * 24 * 3600  # 7 days


def _aspects_cache_path(item_id: str) -> Path:
    """Return the path where item aspects would be cached."""
    return _ASPECTS_CACHE_DIR / f"{item_id}.json"


def _aspects_cache_get(item_id: str) -> "dict | None":
    """Return cached aspects dict if present and fresh, else None."""
    path = _aspects_cache_path(item_id)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _ASPECTS_CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001  # corrupt/partial file → cache miss
        return None


def _aspects_cache_put(item_id: str, aspects: dict) -> None:
    """Write aspects dict to the item-level disk cache (atomic tmp→rename)."""
    path = _aspects_cache_path(item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(aspects))
    tmp.replace(path)


def get_item_aspects(legacy_item_id: str, token: str, base_url: str, *, retries: int = 3) -> "dict | None":
    """Fetch a flat {name: value} dict of item aspects from eBay.

    Calls get_item_by_legacy_id and parses ``localizedAspects`` into a flat dict
    (e.g. {"Publication Year": "2014", "Era": "Modern Age (1992-Now)", ...}).
    Results are cached on disk for 7 days (keyed by item_id) so re-runs are cheap.

    Fail-open: returns None on any HTTP/network/parse error.  Errors are silently
    swallowed — the aspects gate is advisory, not load-bearing.  The caller treats
    None as "no signal" and keeps the listing.

    Request/retry/pacing style mirrors search_by_keyword.
    """
    cached = _aspects_cache_get(legacy_item_id)
    if cached is not None:
        return cached

    url = f"{base_url}/buy/browse/v1/item/get_item_by_legacy_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {"legacy_item_id": legacy_item_id}

    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
        except requests.exceptions.RequestException:
            return None  # network error → fail-open
        if resp.status_code == 200:
            break
        if resp.status_code == 429 and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue
        return None  # non-retryable or last retry → fail-open

    try:
        data = resp.json()
    except (ValueError, AttributeError):
        return None

    raw_aspects = data.get("localizedAspects")
    if not isinstance(raw_aspects, list):
        return None

    aspects: dict = {
        item.get("name"): item.get("value")
        for item in raw_aspects
        if item.get("name")
    }
    _aspects_cache_put(legacy_item_id, aspects)
    return aspects


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
