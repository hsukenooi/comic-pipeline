#!/usr/bin/env python3
"""ebay-fetch: Fetch structured listing data from eBay Browse API."""

import argparse
import base64
import importlib.metadata
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from urllib.parse import quote

from comic_identity import confident_cover_year


def _version_string() -> str:
    """BUI-314: staleness signal for a `uv tool install`ed binary.

    `_ebay_build_stamp` is generated at build time by hatch_build.py from the
    git HEAD of the source tree the wheel was built from; it's absent when
    running from an unbuilt checkout (e.g. `uv run` here in tests), so fall
    back to "unknown" rather than failing.
    """
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-fetch {pkg_version} (git {GIT_SHA}, {GIT_DATE})"

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


# ─── Shared network retry/backoff + atomic write helpers (BUI-323) ──────────
# get_token(), fetch_item_with_status(), search_seller_listings(),
# search_by_keyword(), and get_item_aspects() each drove their own
# hand-rolled "for attempt in range(retries): try/except RequestException,
# check status, backoff, retry" loop. BUI-299/300/310/311/312 kept hardening
# them one function at a time by copying whichever sibling already handled a
# given failure mode best (see docs/solutions/design-patterns/
# oauth-token-refresh-retry-pattern.md, pattern 4) — which worked, but left
# five hand-copied variants that could drift again (and one did: see
# fetch_item_with_status()'s network-error branch below). retry_request() is
# now the single place the retry/backoff shape lives; atomic_write_json() is
# the equivalent consolidation of the tmp-file-then-.replace() write idiom
# used by the OAuth token cache and the item-aspects disk cache.
#
# BUI-333: a reuse review after BUI-323 found the same two idioms hand-copied
# elsewhere in apps/ebay/src/ — sold_comps.py's SerpApi retry loop + JSON
# cache write, ebay_search_cache.py's JSON cache write, and seller_scan.py's
# rejected-candidate cache write. Those now call retry_request()/
# atomic_write_json() too, so apps/ebay has one retry/backoff and one
# atomic-write implementation. (seller_scan.py's Claude-CLI verification
# bisection retry and its single-shot `requests` calls without a retry budget
# were left alone — neither matches this helper's shape; see that module for
# why.)


class RetryExhausted(Exception):
    """Raised by retry_request() when the retry budget runs out.

    Exactly one of the two attributes is set, mirroring the two ways a call
    can keep failing:
    - `response`: the last response with a retryable status code (e.g. a
      429 that never let up).
    - `network_error`: the last requests.exceptions.RequestException (only
      possible when retry_network_errors=True was passed in).

    Callers catch this and choose their own fallback (sys.exit, None, a
    status-tagged return, partial results...) — that terminal reaction is
    exactly the part that differs enough between callers that unifying it
    would just relocate the drift, not remove it.
    """

    def __init__(self, *, response=None, network_error=None):
        self.response = response
        self.network_error = network_error
        super().__init__("retry budget exhausted")


def retry_request(
    make_request,
    *,
    retries,
    is_retryable_status,
    retry_network_errors,
    network_error_context=None,
    status_retry_message=None,
):
    """Drive the exponential-backoff retry loop shared by every network call
    in this module.

    make_request() performs one HTTP call and returns a requests.Response; it
    may raise requests.exceptions.RequestException. Returns the first response
    whose status code is NOT retryable per is_retryable_status(status_code)
    (including an immediate 200) — callers branch on resp.status_code
    themselves (200 vs 404 vs 401 vs ...) since that per-status reaction is
    where callers genuinely differ.

    A retryable status backs off `2 ** attempt` seconds between attempts,
    printing f"{status_retry_message(status)}, retrying in {wait}s..." when
    status_retry_message is given (pass None to retry silently, as
    get_item_aspects() has always done). Once `retries` is exhausted, raises
    RetryExhausted(response=<last response>).

    A RequestException is handled one of two ways, matching the two shapes
    that existed independently before this helper:
    - retry_network_errors=True (get_token(), fetch_item_with_status()):
      retried with the same backoff — printing
      f"Network error {network_error_context}: {exc}, retrying in {wait}s..."
      when network_error_context is given — raising
      RetryExhausted(network_error=<last exc>) once exhausted.
    - retry_network_errors=False (search_seller_listings(),
      search_by_keyword(), get_item_aspects()): re-raised immediately on the
      first occurrence. These callers have always failed fast on a network
      error rather than spending the retry budget on it; BUI-323 preserves
      that existing drift as-is rather than changing behavior beyond its one
      intended fix (fetch_item_with_status(), below).
    """
    if retries < 1:
        raise ValueError("retries must allow at least one attempt")
    resp = None
    for attempt in range(retries):
        try:
            resp = make_request()
        except requests.exceptions.RequestException as exc:
            if not retry_network_errors:
                raise
            if attempt < retries - 1:
                wait = 2 ** attempt
                if network_error_context:
                    print(
                        f"Network error {network_error_context}: {exc}, "
                        f"retrying in {wait}s...",
                        file=sys.stderr,
                    )
                time.sleep(wait)
                continue
            raise RetryExhausted(network_error=exc) from exc

        if not is_retryable_status(resp.status_code):
            return resp

        if attempt < retries - 1:
            wait = 2 ** attempt
            if status_retry_message:
                print(
                    f"{status_retry_message(resp.status_code)}, retrying in {wait}s...",
                    file=sys.stderr,
                )
            time.sleep(wait)
        else:
            raise RetryExhausted(response=resp)

    raise RetryExhausted(response=resp)  # pragma: no cover — unreachable for retries >= 1


# BUI-338: how long an orphaned `<name>.<uuid4>.tmp` must sit untouched before
# _sweep_orphan_tmp_files() will remove it. Must comfortably exceed the
# longest realistic in-flight write (write_text()/json.dump() of a small
# cache file completes in well under a second even under load) so a live
# concurrent writer's tmp is never mistaken for an orphan — see
# _sweep_orphan_tmp_files()'s docstring for the full reasoning.
_ORPHAN_TMP_TTL_SECONDS = 3600


def _sweep_orphan_tmp_files(path, *, ttl_seconds=_ORPHAN_TMP_TTL_SECONDS):
    """Best-effort cleanup of stale `<name>.<uuid4>.tmp` orphans next to
    `path` (BUI-338).

    BUI-335 made atomic_write_json()'s tmp filename per-call-unique
    specifically so concurrent writers to the same `path` (e.g.
    sold_comps._cache_put() under run_batch()'s ThreadPoolExecutor) never
    share — and therefore never clobber — one deterministic tmp name. The
    tradeoff: unlike the old deterministic name, a tmp orphaned by a mid-write
    crash is never reused/overwritten by a later write, and nothing swept it —
    so orphans accumulate a few stray KB per crash, forever.

    This can't be a one-time "sweep at process startup" — apps/ebay has
    several independent console-script entry points (ebay-fetch,
    ebay-sold-comps, seller-scan) that can run as separate OS processes at the
    same time, each with its own ThreadPoolExecutor of concurrent writers to
    a *shared* cache path. There is no point in time that is guaranteed to be
    "before any concurrent writer anywhere is spawned." So instead this is
    gated purely by age: a tmp file is only removed once its mtime is older
    than `ttl_seconds` (default one hour) — far longer than any real write
    takes — so a genuinely in-flight tmp from another process/thread is never
    a candidate no matter when this function happens to run. It's invoked
    from atomic_write_json() itself (see below), so every write is also an
    opportunistic sweep of its own directory.

    Every failure is swallowed and never propagates: a missing directory, a
    tmp file that vanished between being listed and being unlinked (another
    process's writer finished, or another sweep raced this one), or any other
    OSError. This is opportunistic cleanup, not the write the caller asked
    for — it must never turn into a new way for atomic_write_json() to fail.
    """
    try:
        candidates = list(path.parent.glob(f"{path.name}.*.tmp"))
    except OSError:
        return
    now = time.time()
    for candidate in candidates:
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            continue  # vanished (or unreadable) between glob() and stat() — not ours to worry about
        if age < ttl_seconds:
            continue  # too young to be confidently orphaned — could be a live concurrent writer
        try:
            candidate.unlink()
        except OSError:
            pass  # another sweep/writer already removed it, or a transient FS error — best-effort only


def atomic_write_json(path, data, *, mode=None):
    """Write `data` as JSON to `path` atomically (tmp file + Path.replace()),
    so a crash mid-write never leaves a partial/corrupted file for a
    concurrent reader — the pattern _aspects_cache_put() established first and
    get_token()'s token-cache write later copied by hand.

    When `mode` is given, the tmp file is created with exactly those
    permissions from the start via os.open(O_CREAT, mode) instead of the
    process umask — used for the OAuth token cache, which holds a credential.
    Raises OSError on failure (disk full, permission denied, an interrupted
    rename...); callers decide whether that's fatal or best-effort.

    BUI-333: if the write or the replace fails partway, the .tmp file is
    best-effort unlinked before the exception propagates — a pre-existing gap
    (BUI-323 finding d) where a failed write used to leave an orphaned .tmp
    file behind for the next writer to trip over. The cleanup itself never
    masks the original failure: an unlink error is swallowed, and the
    triggering exception always re-raises unchanged.

    BUI-335: the tmp filename is unique per call (`<name>.<uuid4>.tmp`) rather
    than the fixed `path.with_suffix(".tmp")` it used to be. Two concurrent
    writers to the same `path` (e.g. sold_comps._cache_put() under
    run_batch()'s ThreadPoolExecutor, when two workers fetch duplicate cache
    keys in one batch) used to share that one deterministic tmp name, so they
    could clobber each other's in-flight tmp file (a silent lost write), and
    — since BUI-333 added the cleanup unlink above — one writer's failure
    cleanup could delete a *different* writer's still-in-flight tmp, turning
    the silent race into an active FileNotFoundError for that other writer.
    The unique name lives in the same directory as `path` so the final
    replace stays a same-filesystem atomic rename, and the cleanup `unlink`
    here only ever removes *this call's own* tmp file, never a sibling's.

    BUI-338: before creating its own tmp file, best-effort sweeps any
    `<name>.*.tmp` siblings older than an hour — orphans left behind by some
    earlier call that crashed mid-write (see _sweep_orphan_tmp_files() for why
    this is age-gated rather than a one-time startup sweep).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_orphan_tmp_files(path)
    tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
    wrote = False
    try:
        if mode is not None:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            # BUI-341: os.fdopen(fd) can itself raise (e.g. OOM) before the
            # `with` takes ownership of fd, which would otherwise leak the
            # raw descriptor — the outer finally only unlinks the tmp path,
            # it never closes a fd that was never wrapped. Guard the handoff
            # explicitly.
            try:
                f = os.fdopen(fd, "w")
            except Exception:
                os.close(fd)
                raise
            with f:
                json.dump(data, f)
        else:
            tmp.write_text(json.dumps(data))
        tmp.replace(path)
        wrote = True
    finally:
        if not wrote:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def get_token(client_id, client_secret, base_url, *, force_refresh=False):
    """Get a valid OAuth app token, using cache if available.

    force_refresh=True skips the cache-freshness check and always requests a
    new token from eBay. BUI-310: a 401 mid-batch isn't provably caused by the
    cache's own TTL running out (server-side revocation, clock skew) — a
    caller retrying after a 401 needs a token guaranteed to differ from the
    one that was just rejected, not "whatever the cache currently says is
    still valid."
    """
    cache_file = _token_cache_file(base_url)

    # Check cache
    if not force_refresh and cache_file.exists():
        try:
            with open(cache_file) as f:
                cache = json.load(f)
            expires_at = cache.get("expires_at", 0)
            if time.time() < expires_at - 300:  # 5-minute buffer
                return cache["access_token"]
        except Exception:  # noqa: BLE001  # malformed/wrong-shape cache → cache miss
            pass  # e.g. non-dict JSON, non-numeric expires_at, missing access_token

    # Request new token via the shared retry_request() helper (BUI-323) —
    # the same one fetch_item_with_status() uses below, so a network error is
    # now retried with backoff exactly like a 429/5xx on both. Non-retryable
    # 4xx errors (e.g. 401 bad credentials) exit immediately. BUI-184: a
    # one-shot sys.exit on the first non-200 killed the whole run on a
    # transient auth hiccup.
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    retries = 3

    try:
        resp = retry_request(
            lambda: requests.post(
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
            ),
            retries=retries,
            is_retryable_status=lambda code: code == 429 or code >= 500,
            retry_network_errors=True,
            network_error_context="requesting token",
            status_retry_message=lambda code: f"Token request failed ({code})",
        )
    except RetryExhausted as exc:
        if exc.network_error is not None:
            print(
                f"Error: Authentication failed (network error after {retries} attempts): {exc.network_error}",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: Authentication failed ({exc.response.status_code}) after {retries} attempts",
                file=sys.stderr,
            )
        sys.exit(1)

    if resp.status_code != 200:
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

    # Cache token with restrictive permissions, atomically (tmp→rename via the
    # shared atomic_write_json(), BUI-323) so a crash mid-write never leaves
    # a partial cache file. Best-effort: an OSError here (e.g. disk full,
    # permission denied) must not discard the token we already got from
    # eBay — log and fall through, still returning the live token.
    try:
        atomic_write_json(
            cache_file,
            {"access_token": access_token, "expires_at": time.time() + expires_in},
            mode=0o600,
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


def fetch_item_with_status(item_id, token, base_url, retries=3):
    """Fetch a single item from the Browse API, also returning the HTTP status.

    Returns (data, status_code):
    - data: the parsed JSON dict on success, else None.
    - status_code: the HTTP status of the terminal response (e.g. 401, 404,
      429), or None when the failure was a network error with no response at
      all. On success (data is not None), status_code is always 200.

    BUI-310: fetch_item() collapsed every non-200/404/429 response (including
    401) to a bare `return None`, so a caller couldn't tell "token expired"
    apart from any other failure. This is the status-aware version; fetch_item()
    is now a thin wrapper that keeps the historical None-on-failure contract for
    its two existing callers (this module's own CLI, grade_photos.py). Callers
    that need to react to the status — e.g. refreshing an OAuth token on a 401
    mid-batch — should call this directly instead.

    BUI-323: a network error now consumes the retries budget with backoff via
    the shared retry_request() helper, the same as get_token() — it used to
    return (None, None) on the very first RequestException, spending none of
    the retry budget a 429 gets (a real drift this fix removes).
    """
    url = f"{base_url}/buy/browse/v1/item/get_item_by_legacy_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {"legacy_item_id": item_id}

    try:
        resp = retry_request(
            lambda: requests.get(url, headers=headers, params=params, timeout=10),
            retries=retries,
            is_retryable_status=lambda code: code == 429,
            retry_network_errors=True,
            network_error_context=f"fetching item {item_id}",
            status_retry_message=lambda code: "Rate limited",
        )
    except RetryExhausted as exc:
        if exc.network_error is not None:
            print(
                f"Network error fetching item {item_id}: {exc.network_error}, "
                f"giving up after {retries} attempts.",
                file=sys.stderr,
            )
            return None, None
        print(f"Error: Failed to fetch item {item_id} after {retries} retries.", file=sys.stderr)
        return None, 429

    if resp.status_code == 200:
        try:
            return resp.json(), 200
        except ValueError as exc:
            print(f"Error: Malformed response for item {item_id}: {exc}", file=sys.stderr)
            return None, 200
    elif resp.status_code == 404:
        print(f"Error: Item {item_id} not found (404).", file=sys.stderr)
        return None, 404
    else:
        print(
            f"Error fetching item {item_id}: HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
        )
        return None, resp.status_code


def fetch_item(item_id, token, base_url, retries=3):
    """Fetch a single item from the Browse API. Returns None on any failure.

    Thin wrapper over fetch_item_with_status() that discards the status code,
    preserving the original contract for existing callers. Use
    fetch_item_with_status() directly to distinguish failure modes (e.g. a 401
    from any other error).
    """
    data, _status = fetch_item_with_status(item_id, token, base_url, retries=retries)
    return data


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
        # BUI-316: a per-issue cover year to forward to /comic:collection-check,
        # but ONLY when the title's parenthesized year and item-specifics
        # Publication Year corroborate it (and it's not a facsimile/reprint).
        # None when not confident — the check then stays year-agnostic (never
        # forwards a wrong year, so it can't reintroduce BUI-129).
        "cover_year": confident_cover_year(title, item_specifics),
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

        try:
            resp = retry_request(
                lambda: requests.get(url, headers=headers, params=query, timeout=15),
                retries=retries,
                is_retryable_status=lambda code: code == 429,
                retry_network_errors=False,
                status_retry_message=lambda code: "Rate limited",
            )
        except requests.exceptions.RequestException as exc:
            print(
                f"Network error fetching seller listings for '{username}': {exc}",
                file=sys.stderr,
            )
            return _filter_by_seller(all_items, username)
        except RetryExhausted as exc:
            resp = exc.response

        if resp.status_code != 200:
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

        try:
            resp = retry_request(
                lambda: requests.get(url, headers=headers, params=query, timeout=15),
                retries=retries,
                is_retryable_status=lambda code: code == 429,
                retry_network_errors=False,
                status_retry_message=lambda code: "Rate limited",
            )
        except requests.exceptions.RequestException as exc:
            print(
                f"Network error searching by keyword '{keyword}': {exc}",
                file=sys.stderr,
            )
            return all_items[:max_results]
        except RetryExhausted as exc:
            resp = exc.response

        if resp.status_code != 200:
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
    """Write aspects dict to the item-level disk cache (atomic tmp→rename via
    the shared atomic_write_json(), BUI-323)."""
    atomic_write_json(_aspects_cache_path(item_id), aspects)


def get_item_aspects(legacy_item_id: str, token: str, base_url: str, *, retries: int = 3) -> "dict | None":
    """Fetch a flat {name: value} dict of item aspects from eBay.

    Calls get_item_by_legacy_id and parses ``localizedAspects`` into a flat dict
    (e.g. {"Publication Year": "2014", "Era": "Modern Age (1992-Now)", ...}).
    Results are cached on disk for 7 days (keyed by item_id) so re-runs are cheap.

    Fail-open: returns None on any HTTP/network/parse error.  Errors are silently
    swallowed — the aspects gate is advisory, not load-bearing.  The caller treats
    None as "no signal" and keeps the listing.

    Request/retry style shares the module's retry_request() helper (BUI-323),
    matching search_by_keyword's fail-fast-on-network-error / retry-silently-
    on-429 shape.
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

    try:
        resp = retry_request(
            lambda: requests.get(url, headers=headers, params=params, timeout=10),
            retries=retries,
            is_retryable_status=lambda code: code == 429,
            retry_network_errors=False,
        )
    except (requests.exceptions.RequestException, RetryExhausted):
        return None  # network error or retry-exhausted → fail-open

    if resp.status_code != 200:
        return None  # non-retryable status → fail-open

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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch structured listing data from eBay.",
        prog="ebay-fetch",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Print the installed version and the git SHA/date it was built "
             "from, then exit. Use this to check for a stale `uv tool install` "
             "(see scripts/install.sh).",
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

    args = parser.parse_args(argv)

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
