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
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib.parse import quote

import grade_tokens
from comic_identity import confident_cover_year, identify_comic
from condition_defects import format_defect_cell, listing_defect_findings


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

# BUI-923: "CGC" was here until it was found to be the cause of the bug this
# ticket exists to fix — GRADE_PATTERN/GRADE_BARE_PATTERN's alternation tries
# each alternative left-to-right, so on a title like "CGC AA SS 4.5" or
# "2003 CGC 9.4" the bare "CGC" alternative matched at an earlier position
# than the actual numeric grade and won, returning the literal string "CGC"
# as the "grade" and silently dropping the number. Certifier detection (and
# the numeric grade that goes with it) now lives in grade_tokens.py /
# ebay_fetch.extract_certification instead — see that module's
# extract_title_certification().
_GRADE_ABBREVS = (
    r"NM[\-\+]?|VF[\-\+]?|FN[\-\+]?|VG[\-\+]?|GD[\-\+]?|FR[\-\+]?|PR[\-\+]?"
    r"|Near Mint[\-\+]?|Very Fine[\-\+]?|Fine[\-\+]?"
    r"|NM/M|NM/MT|FN/VF|VG/FN|GD/VG|FR/GD"
    r"|FVF|VF/NM|Gem"
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

# BUI-923: item specifics that name the certifier/cert number for a slab
# listing. Distinct from GRADE_SPECIFICS_KEYS above (which names the *grade*
# field) — a listing can carry either set independently of the other.
CERTIFICATION_SPECIFICS_KEYS = {"Certification", "Professional Grader", "Certification Number"}
CERTIFICATION_SPECIFICS_KEYS_LOWER = frozenset(k.lower() for k in CERTIFICATION_SPECIFICS_KEYS)

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
    on_attempt=None,
    backoff_seconds=None,
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

    BUI-537: `on_attempt(attempt, resp, exc)` (optional; default None, so
    every pre-existing caller is unaffected) is called once for each attempt
    that this loop is about to SUPERSEDE with a further attempt — i.e. every
    charged-but-invisible-to-the-caller retry (a retryable-status response, or
    a network exception about to be retried). It is deliberately NOT called
    for the terminal attempt (the one ultimately returned or raised) — that
    one is already visible to the caller via this function's normal return
    value / raised exception, so calling the hook there too would double
    report the same charge. Exactly one of `resp`/`exc` is non-None per call.

    BUI-701: `backoff_seconds(attempt, resp, exc)` (optional; default None
    keeps every pre-existing caller's `2 ** attempt` schedule byte-for-byte
    unchanged) computes the wait before the next attempt instead of the
    default. Called with the same `(resp, exc)` shape as `on_attempt` —
    exactly one of the two is non-None. sold_comps.fetch_sold_comps() passes
    one so a 429 specifically gets a much longer, jittered wait than an
    ordinary transient (5xx / network error) gets — see that module's
    `_sold_comps_backoff_seconds` for why.
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
                wait = (backoff_seconds(attempt, None, exc)
                        if backoff_seconds is not None else 2 ** attempt)
                if on_attempt is not None:
                    on_attempt(attempt, None, exc)
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
            wait = (backoff_seconds(attempt, resp, None)
                    if backoff_seconds is not None else 2 ** attempt)
            if on_attempt is not None:
                on_attempt(attempt, resp, None)
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


def fetch_item_description(item_id, token, base_url, retries=3):
    """Fetch one item's description text from the Browse API (BUI-929).

    Built for sold_comps.py's printing guard: it needs one listing's free-
    text description to look for an ordinal printing / facsimile token, not
    the full parsed item shape parse_item() produces. Reuses
    fetch_item_with_status() (not fetch_item()) so the "not found at all"
    (404/network error, data is None) case is indistinguishable from any
    other total-fetch-failure and both collapse to the same return value —
    the caller (the printing guard) already treats "no text" as one
    uniform "couldn't verify" outcome regardless of *why* the text is
    missing, so there is nothing for a status code to add here.

    Returns the item's `description` field (the Browse API's full listing
    HTML/text) when present and non-empty, else its `shortDescription`
    field, else None. None is also returned when the fetch itself failed —
    a caller cannot (and per the guard's own contract, must not try to)
    distinguish "fetched successfully but the item has no description text"
    from "the fetch failed"; both mean "no printing-token evidence available
    for this comp".
    """
    data, _status = fetch_item_with_status(item_id, token, base_url, retries=retries)
    if data is None:
        return None
    text = data.get("description") or data.get("shortDescription")
    return text or None


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


class CertificationResult:
    """Result of extract_certification() — a slab's certifier, numeric grade,
    cert number, label, and page quality, or all-None when the listing isn't
    certified. `title_bare_mention` is True when the title names a certifier
    (e.g. "CGC") without it being adjacent to a grade — the ambiguous "CGC
    ready, would grade 9.6" case (BUI-923) — so parse_item knows to suppress
    a legacy raw-title grade fallback rather than trust a stray nearby
    number.
    """

    __slots__ = (
        "certifier", "grade", "cert_number", "label", "page_quality",
        "mismatch_note", "title_bare_mention",
    )

    def __init__(
        self, certifier=None, grade=None, cert_number=None, label=None,
        page_quality=None, mismatch_note=None, title_bare_mention=False,
    ):
        self.certifier = certifier
        self.grade = grade
        self.cert_number = cert_number
        self.label = label
        self.page_quality = page_quality
        self.mismatch_note = mismatch_note
        self.title_bare_mention = title_bare_mention


def extract_certification(item_specifics, title):
    """Resolve certifier, numeric grade, cert number, label, and page
    quality for a certified (slab) listing (BUI-923).

    Precedence is item specifics, then title (plan U1 KTD "Item specifics
    win over title"). A specifics-vs-title grade disagreement keeps the
    specifics value and records a mismatch note. Only a recognized
    certifier value counts — "Uncertified"/"None"/"Not Graded"/"Raw"/blank
    specifics values, and a title mentioning a certifier name with no grade
    adjacent to it, both come back with certifier=None (see
    CertificationResult.title_bare_mention for the latter).
    """
    specifics_certifier = None
    cert_number = None
    specifics_grade_raw = None
    for spec in item_specifics:
        name = spec.get("name", "").strip().lower()
        if name not in CERTIFICATION_SPECIFICS_KEYS_LOWER and name not in GRADE_SPECIFICS_KEYS_LOWER:
            continue
        if name == "certification number":
            if cert_number is None and spec.get("value"):
                cert_number = spec.get("value")
        elif name in CERTIFICATION_SPECIFICS_KEYS_LOWER:  # "certification" / "professional grader"
            if specifics_certifier is None:
                specifics_certifier = grade_tokens.resolve_certifier_from_specifics_value(spec.get("value"))
        elif name in GRADE_SPECIFICS_KEYS_LOWER:
            if specifics_grade_raw is None and spec.get("value"):
                specifics_grade_raw = str(spec.get("value"))

    specifics_grade = None
    if specifics_certifier and specifics_grade_raw:
        m = grade_tokens._NUMERIC_GRADE_RE.search(specifics_grade_raw)
        if m:
            specifics_grade = float(m.group(1))

    title_certifier, title_grade, title_bare_mention = grade_tokens.extract_title_certification(title)

    if specifics_certifier:
        certifier = specifics_certifier
        # A structured Certification/Professional Grader value can be
        # present without a parseable numeric Grade specifics field
        # alongside it; the title's adjacency-matched grade is still a
        # legitimate source for the number in that case. No mismatch is
        # recorded when specifics_grade is None — there's nothing to
        # disagree with.
        grade = specifics_grade if specifics_grade is not None else title_grade
        mismatch_note = None
        if title_grade is not None and specifics_grade is not None and title_grade != specifics_grade:
            mismatch_note = f"grade mismatch: item specifics {specifics_grade} vs title {title_grade}"
    elif title_certifier:
        # cert_number is left as whatever the specifics loop already found —
        # a Certification Number can be present even when the Certification/
        # Professional Grader value itself didn't resolve, so the title
        # supplying the certifier shouldn't discard real specifics data.
        certifier, grade, mismatch_note = title_certifier, title_grade, None
    else:
        return CertificationResult(title_bare_mention=title_bare_mention)

    label_text = f"{title} {specifics_grade_raw or ''}"
    label = grade_tokens.resolve_label(label_text) or "universal"
    page_quality = grade_tokens.resolve_page_quality(label_text) or "unknown"

    return CertificationResult(
        certifier=certifier, grade=grade, cert_number=cert_number, label=label,
        page_quality=page_quality, mismatch_note=mismatch_note,
        title_bare_mention=title_bare_mention,
    )


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


# eBay's Comics category "Era" item specific is a free-text range like
# "Silver Age (1956-69)" or "Modern Age (1992-Now)" — the parenthesized start
# year is always 4 digits; the end is either "Now"/"Present" (Modern Age is
# still running) or a 2- or 4-digit year.
_ERA_RANGE_RE = re.compile(r"\((\d{4})\s*-\s*(now|present|\d{2,4})\)", re.IGNORECASE)


def _parse_era_range(era):
    """Parse an ``Era`` item specific (BUI-958) into a ``(start, end)`` year
    tuple for confident_cover_year's Era-corroboration signal, or None if
    *era* is missing/unparseable.

    ``end`` is None for an open-ended era ("...-Now"/"...-Present"). A
    2-digit end year ("1956-69") is expanded into the same century as the
    start year (-> 1969, never the literal 69), with a defensive rollover for
    the rare case where the 2-digit value is numerically less than the
    start year's own remainder (e.g. a hypothetical "1998-05" -> 2005, not
    1905 — eras never run backwards).

    This is a corroborating signal only — a decade-scale range, never a
    fabricated single year — so failing open (None) here just means the Era
    can't help; it never causes a wrong year to be forwarded.
    """
    if not era:
        return None
    m = _ERA_RANGE_RE.search(str(era))
    if not m:
        return None
    start = int(m.group(1))
    end_raw = m.group(2).lower()
    if end_raw in ("now", "present"):
        return (start, None)
    if len(end_raw) == 2:
        end = (start // 100) * 100 + int(end_raw)
        if end < start:
            end += 100
        return (start, end)
    return (start, int(end_raw))


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

    # BUI-919: the seller's own free-text condition note. `conditionDescription`
    # is the Browse API key behind eBay's "Seller Notes" block — verified live
    # on 2026-09-21 against timemachinecomics listings whose titles hide the
    # grade behind "see condition description" (e.g. item 137743677922 returns
    # "cover and 1st 6 wraps detached bottom staple"). It is absent on most
    # listings, which is a genuine "the seller wrote nothing", not an error.
    # `shortDescription` (`description_snippet` below) is NOT a substitute: it
    # returns the store's return-policy boilerplate, identical across every
    # listing of a store, which is what made the BUI-919 investigation think
    # the text was unreachable.
    condition_description = data.get("conditionDescription")
    # Classify the FULL text, then truncate only what is echoed out — a cap
    # applied first could hide a defect named at the end of a long note.
    condition_defects = listing_defect_findings(
        condition_description=condition_description, title=title,
    )
    if isinstance(condition_description, str) and len(condition_description) > 1000:
        condition_description = condition_description[:1000]

    item_specifics_raw = data.get("localizedAspects", [])
    item_specifics = {s.get("name", ""): s.get("value") for s in item_specifics_raw if s.get("name")}

    description_snippet = data.get("shortDescription")
    if description_snippet and len(description_snippet) > 500:
        description_snippet = description_snippet[:500]

    grade, grade_source, grade_from_description = extract_grade(
        item_specifics_raw, title, description_snippet,
    )
    certification = extract_certification(item_specifics_raw, title)
    if certification.certifier:
        # BUI-923: item specifics win over title, and a slab's grade is
        # numeric, never the raw "CGC"/"NM-"-style string extract_grade
        # returns for a raw book.
        grade = certification.grade
        grade_source = "certified"
    elif grade_source == "title" and certification.title_bare_mention:
        # The title names a certifier (e.g. "CGC ready, would grade 9.6")
        # without it sitting next to a grade — raw with no stated grade, not
        # a self-reported number (BUI-923 outcome text). Only suppresses a
        # TITLE-sourced legacy grade; a genuine item-specifics Grade field
        # is untouched even when Certification/Professional Grader resolves
        # to absent on the same listing.
        grade = None
        grade_source = "missing"
        grade_from_description = None
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
        # BUI-919: the seller's free-text condition note, and the standing-rule
        # defects found in it (plus the title). `condition_defects` is a list of
        # {code, phrase, source}; an EMPTY list means "scanned, nothing found",
        # never "not checked" — the field is always present.
        "condition_description": condition_description,
        "condition_defects": condition_defects,
        "grade": grade,
        "grade_source": grade_source,
        "grade_from_description": grade_from_description,
        # BUI-923: blank (None) on a raw listing — only ever populated when
        # grade_source == "certified".
        "certifier": certification.certifier,
        "cert_number": certification.cert_number,
        "label": certification.label,
        "page_quality": certification.page_quality,
        "grade_mismatch_note": certification.mismatch_note,
        "variant": variant,
        # BUI-316: a per-issue cover year to forward to /comic:collection-check,
        # but ONLY when the title's parenthesized year and item-specifics
        # Publication Year corroborate it (and it's not a facsimile/reprint).
        # None when not confident — the check then stays year-agnostic (never
        # forwards a wrong year, so it can't reintroduce BUI-129).
        # BUI-942: on a CERTIFIED listing a bare title year also corroborates,
        # and a "3/66"-style cover date may stand alone — slab titles spend
        # their parens on the cert, so the paren-only gate left 6 of the 8
        # spike slabs yearless. certified is keyed off the same signal that
        # sets grade_source == "certified" above.
        # BUI-958: eBay's "Era" item specific (e.g. "Silver Age (1956-69)")
        # is a second source that lets a Publication Year resolve even when
        # the title states no year at all (spike listings 377507539790 /
        # Silver Surfer #4 and 407184193219 / Ultimate Fallout #4 — both
        # carry a Publication Year but zero title-stated years). Parsed
        # unconditionally; confident_cover_year only consults it when
        # certified=True, so a raw listing is unaffected regardless.
        "cover_year": confident_cover_year(
            title, item_specifics,
            certified=bool(certification.certifier),
            era_range=_parse_era_range(item_specifics.get("Era")),
        ),
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
    """Add/update one store-name → username mapping and persist it.

    BUI-540: seller_aliases.json is a *tracked* file the skill tells you to
    commit after an add, so its formatting is part of the repo's diff
    surface. json.dump() alone leaves no trailing newline, which makes a
    one-line addition look like a two-line change (the closing `}` line
    flips from "no newline at end of file" to normal) — so every subsequent
    add re-touches that line too. Writing the newline explicitly keeps the
    file POSIX-text-file-shaped and the diff to exactly the changed key.
    """
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
        f.write("\n")
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


def search_by_keyword(keyword, token, base_url, *, max_results=500, buying_options="AUCTION|FIXED_PRICE", retries=3, on_error=None):
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

    `on_error(message)` (BUI-971; optional, default None leaves every
    pre-existing caller byte-for-byte unchanged) is called exactly once, with
    the same one-line message already printed to stderr, when the search bails
    out early on a Browse failure — a transport error, or a non-200 status
    (which includes a 429 that outlasted the retry budget). The return value
    stays what it has always been (whatever items were collected before the
    failure, possibly []), because the two existing callers want partial
    results; the hook exists so a caller that CANNOT tell a failed search from
    an empty one — `search_active_asks`, whose whole output is a count — can.
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
            msg = f"Network error searching by keyword '{keyword}': {exc}"
            print(msg, file=sys.stderr)
            if on_error is not None:
                on_error(msg)
            return all_items[:max_results]
        except RetryExhausted as exc:
            resp = exc.response

        if resp.status_code != 200:
            msg = (f"Error searching by keyword: HTTP {resp.status_code}: "
                   f"{resp.text[:200]}")
            print(msg, file=sys.stderr)
            if on_error is not None:
                on_error(msg)
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


# ─── Active-ask ceiling (BUI-954) ──────────────────────────────────────────
#
# `comic-fmv` shells out to this mode (`--active-asks`) for a REFUSED
# (needs_manual) row: it never imports eBay code, so the search itself, and
# the identity filtering that keeps a mismatched listing from being counted,
# both have to live here. Display only on the caller's side — see
# fmv_runner._maybe_attach_active_ask_ceiling — but the filtering below is
# what makes the number trustworthy enough to show at all: a search by
# title+issue alone returns every grade and every certifier, and an
# unfiltered lowest price would as often be a wrong-grade or wrong-certifier
# listing as the row's own identity.


def _parse_active_ask_price(item: dict) -> "float | None":
    """The numeric USD price of a parsed itemSummary (BUI-954), or None.

    `parse_item_summary` already formats `current_price` as a currency-symbol
    string (`"$25.00"` for USD, `"GBP 25.00"` otherwise — see its docstring).
    Only a `$`-prefixed price is accepted: a display-only ceiling must not
    blend currencies (the same posture BUI-675's raw-comp currency gate
    takes), and there is no FX conversion here to make a non-USD number
    comparable.
    """
    raw = item.get("current_price")
    if not isinstance(raw, str) or not raw.startswith("$"):
        return None
    try:
        return float(raw[1:].replace(",", ""))
    except ValueError:
        return None


def _title_matches_ask_identity(title: str, *, grade: float,
                                certifier: "str | None",
                                label: "str | None") -> bool:
    """True if `title` (an active BIN listing) names the SAME identity as the
    refused row it's a ceiling candidate for (BUI-954).

    Conservative by design — a display-only ceiling that mislabels an
    unrelated book's ask as this one's is worse than a ceiling that misses a
    real match, so every ambiguous case (no readable grade, a slab title on
    a raw target, a raw title on a slab target, a certifier/label mismatch)
    is dropped rather than guessed at.

    ``certifier``/``label`` are None for a RAW target: a slab ask (any
    certifier named in the title) is a different market and is dropped
    outright, mirroring `_SLAB_TITLE_RE`'s role on the sold-comps side. For a
    CERTIFIED target both must match exactly — a CGC 4.5 row never counts a
    CGC 6.0 ask (wrong grade) or a CBCS 4.5 ask (wrong certifier).
    """
    ask_certifier = grade_tokens.resolve_certifier_token(title)
    if certifier:
        if ask_certifier != certifier:
            return False
        ask_label = grade_tokens.resolve_label(title) or "universal"
        if ask_label != label:
            return False
    elif ask_certifier is not None:
        return False

    m = grade_tokens._NUMERIC_GRADE_RE.search(title)
    if not m:
        return False
    try:
        ask_grade = float(m.group(1))
    except ValueError:
        return False
    return abs(ask_grade - grade) < 0.05


def search_active_asks(keyword, token, base_url, *, grade, certifier=None,
                       label=None, max_results=200):
    """Search active (Buy It Now) listings and return the lowest ask + count
    for the same title/issue/grade[/certifier/label] identity (BUI-954).

    Wraps `search_by_keyword` with `buying_options="FIXED_PRICE"` — the same
    Browse API path `seller-scan` already uses — then filters to listings
    `_title_matches_ask_identity` confirms share this book's identity.
    Returns ``{"low": float | None, "n": int}``; ``n == 0`` (and
    ``low is None``) when nothing survived the search or the filter, never
    an exception — a caller (this module's own `main`) prints it as-is and
    lets the SUBPROCESS caller (`comic-fmv`) decide how to fail soft.

    BUI-971 — the errored-search case. `search_by_keyword` fails soft: a
    Browse HTTP status, an exhausted rate-limit budget, or a transport error
    prints to stderr and returns whatever it had, which for the usual
    single-page search is ``[]``. Filtering that produced ``{"low": None,
    "n": 0}`` — byte-for-byte what a book with no live asks produces — and
    the process still exited 0, so `comic-fmv` could not tell an outage from
    a genuine zero and the refused row showed no ceiling either way, with no
    warning. That is the BUI-565 shape (an errored fetch reading as a clean
    zero), here on a display-only path where it costs no money but hides an
    outage. So an errored search now returns ``{"low": None, "n": None,
    "error": "<the same one-line message>"}``: the `error` key is the signal
    `_fetch_active_asks` branches on, and ``n`` is **null rather than 0** so
    that a consumer that ignores the key still cannot read an outage as a
    genuine zero. A genuine zero is unchanged, and stays silent.

    The exit code stays 0 in both cases (see `main`) — the JSON is the whole
    contract, and a non-zero exit would collapse the error back into
    `_fetch_active_asks`'s generic "exit N" branch, losing which failure it
    was.
    """
    errors: list[str] = []
    items = search_by_keyword(keyword, token, base_url,
                              max_results=max_results,
                              buying_options="FIXED_PRICE",
                              on_error=errors.append)
    if errors:
        return {"low": None, "n": None, "error": errors[0]}
    prices = []
    for item in items:
        title = item.get("title") or ""
        if not _title_matches_ask_identity(title, grade=grade,
                                           certifier=certifier, label=label):
            continue
        price = _parse_active_ask_price(item)
        if price is not None:
            prices.append(price)
    if not prices:
        return {"low": None, "n": 0}
    return {"low": min(prices), "n": len(prices)}


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


# Per-item disk cache for the raw `conditionDescription` text (BUI-968: the
# condition-defect gate, extended from /comic:buy Step 1.5 to seller-scan and
# wishlist-sellers). Same endpoint and cache shape as the aspects cache above,
# but a SEPARATE namespace and a SEPARATE cache semantic: this caches the
# seller's raw words, not a computed verdict, so a later condition_defects.py
# pattern fix reclassifies a cached item immediately on its next call instead
# of waiting out the 7-day TTL. The cached payload is `{"condition_description":
# <str-or-None>}` rather than a bare value — unlike the aspects cache (which
# only ever caches a successful non-empty result), a listing with NO seller
# note is a common, legitimate, cacheable outcome here (most listings carry no
# conditionDescription at all), and a bare `None` on disk would be
# indistinguishable from "no cache file" to _condition_cache_get() below.
_CONDITION_CACHE_DIR: Path = Path.home() / ".cache" / "ebay-fetch" / "condition"
_CONDITION_CACHE_TTL_SEC: int = 7 * 24 * 3600  # 7 days


def _condition_cache_path(item_id: str) -> Path:
    """Return the path where a condition description would be cached."""
    return _CONDITION_CACHE_DIR / f"{item_id}.json"


def _condition_cache_get(item_id: str) -> "dict | None":
    """Return the cached `{"condition_description": ...}` payload if present
    and fresh, else None (cache miss — never fetched, or expired/corrupt)."""
    path = _condition_cache_path(item_id)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _CONDITION_CACHE_TTL_SEC:
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — corrupt/partial file → cache miss
        return None


def _condition_cache_put(item_id: str, payload: dict) -> None:
    """Write `{"condition_description": ...}` to the item-level disk cache
    (atomic tmp→rename via the shared atomic_write_json(), BUI-323)."""
    atomic_write_json(_condition_cache_path(item_id), payload)


def get_condition_description(legacy_item_id: str, token: str, base_url: str, *, retries: int = 3) -> "str | None":
    """Fetch one item's raw `conditionDescription` text (BUI-968).

    Same `get_item_by_legacy_id` call as get_item_aspects() above, but reads a
    different field. Disk-cached 7 days, keyed by item_id, in a namespace
    separate from the aspects cache (see the comment above _CONDITION_CACHE_DIR).

    Fail-open: returns None on any HTTP/network/parse error, AND when the
    seller simply wrote no condition note at all (the common case). A caller
    cannot and must not treat None as "verified clean" — see
    condition_defects.py's own warning that a blank Defects cell is not a
    clean bill of health, only "nothing to read".
    """
    cached = _condition_cache_get(legacy_item_id)
    if cached is not None:
        return cached.get("condition_description")

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

    condition_description = data.get("conditionDescription")
    _condition_cache_put(legacy_item_id, {"condition_description": condition_description})
    return condition_description


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


# ---------------------------------------------------------------------------
# BUI-900: the /comic:identify table, emitted by the CLI instead of an agent.
# Everything the comic-identifier agent used to do by hand is deterministic:
# series/issue via comic_identity.identify_comic (the one canonical parser),
# the grade verdict ebay-fetch already computed, the confidence-gated cover
# year, and time-to-end from a caller-supplied UTC reference.  The agent now
# runs one `ebay-fetch --identify` call and returns this table verbatim.
# ---------------------------------------------------------------------------

IDENTIFY_COLUMNS = (
    "#", "Comic", "Issue", "Year", "Grade", "Variant", "Type",
    "Current Price", "Bids", "Seller", "Ends", "Notes",
    # BUI-919: inserted between Notes and Cert, NOT appended, so both the
    # positive positional assertions (cells[11] == Notes) and the negative ones
    # (cells[-2] == Cert, cells[-1] == PQ) in test_ebay_fetch.py stay valid.
    # Carries the seller-disclosed standing-rule defect (moisture / rust /
    # loose staple) with the phrase that fired; blank on a clean listing.
    "Defects",
    # BUI-923: appended at the end, not inserted after Grade, so every
    # existing positional cells[N] assertion in test_ebay_fetch.py stays
    # valid \u2014 only test_clean_auction_row's full-row literal needed updating.
    "Cert", "PQ",
)
_DASH = "\u2014"
_WARN = "\u26a0\ufe0f"

# BUI-923: identify-table display strings for the new certifier/label/
# page-quality vocab. Deliberately terser than the raw token values (e.g.
# "SS" not "signature_series") to keep the markdown table narrow; "universal"
# and "unknown" render as nothing \u2014 a slab with no non-default label/page
# quality just shows the certifier name alone / a blank PQ cell.
_CERTIFIER_DISPLAY = {"cgc": "CGC", "cbcs": "CBCS", "other": "Other"}
_LABEL_DISPLAY = {
    "signature_series": "SS", "qualified": "Q", "restored": "R", "conserved": "C", "other": "Other",
}
_PAGE_QUALITY_DISPLAY = {
    "white": "White", "ow_w": "OW/W", "ow": "OW", "c_ow": "C/OW", "cream": "Cream",
}


def _cert_cell(certifier, label):
    """The Cert column: certifier plus label, e.g. "CGC SS"; blank for a raw
    (uncertified) listing; just the certifier name when the label is the
    "universal" default."""
    if not certifier:
        return None
    parts = [_CERTIFIER_DISPLAY.get(certifier, certifier.upper())]
    if label and label != "universal":
        parts.append(_LABEL_DISPLAY.get(label, label))
    return " ".join(parts)


def parse_utc_timestamp(text):
    """Parse an ISO-8601 timestamp (``Z`` or offset) into an aware datetime.

    Naive input is taken as UTC.  Raises ValueError on anything unparseable so
    a bad --now fails the call instead of silently computing wrong deadlines.
    """
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_time_remaining(end_iso, now):
    """Relative time-to-end for the Ends column, per the identify contract:
    ``<60 min -> "47m"``, ``<24h -> "18h"``, ``>=1 day -> "2d"``; anything
    under 24h carries the warning mark; an already-ended auction says so.
    Returns None when there is no usable end date (the caller renders a dash).
    """
    if not end_iso:
        return None
    try:
        end = parse_utc_timestamp(end_iso)
    except (ValueError, TypeError, AttributeError):
        return None
    secs = (end - now).total_seconds()
    if secs <= 0:
        return f"{_WARN} ended"
    if secs < 3600:
        return f"{_WARN} {max(1, int(secs // 60))}m"
    if secs < 86400:
        return f"{_WARN} {int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _cell(value):
    """Render a table cell: None/empty -> dash, pipes escaped so a title or a
    reason can never break the markdown row."""
    if value is None or value == "":
        return _DASH
    return str(value).replace("|", "\\|")


def _lot_issue_cell(constituents):
    """Issue cell for a lot: ``#48-50`` for a contiguous numeric run, the
    listed members (``#33, #45, #50``) otherwise, None when unknown."""
    nums = [str(n) for n in (constituents or [])]
    if not nums:
        return None
    if len(nums) == 1:
        return f"#{nums[0]}"
    try:
        first, last = int(nums[0]), int(nums[-1])
        contiguous = nums == [str(n) for n in range(first, last + 1)]
    except ValueError:
        contiguous = False
    if contiguous:
        return f"#{nums[0]}-{nums[-1]}"
    return ", ".join(f"#{n}" for n in nums)


def identify_row(index, item, now):
    """One markdown row of the identification table for a parsed listing."""
    notes = []
    ident = identify_comic(item.get("title"))

    series = ident.series
    if ident.is_lot:
        # A lot is never a single-issue identification, even when its contents
        # could not be parsed (constituent_issues == [] means "known bundle,
        # contents unknown", not "not a lot").
        notes.append(f"{_WARN} Lot listing, not a single-issue identification")
        issue = _lot_issue_cell(ident.constituent_issues)
    elif ident.issue:
        issue = f"#{ident.issue}"
    else:
        issue = None
    if not series or not issue or (ident.confidence is not None and ident.confidence <= 0.3):
        notes.append(f"{_WARN} Could not parse series/issue from title")

    grade = item.get("grade")
    if grade:
        grade_cell = grade
        if item.get("grade_source") == "title":
            notes.append("grade from title")
    elif item.get("grade_from_description"):
        grade_cell = item.get("grade_from_description")
        notes.append("grade from description only")
    else:
        grade_cell = None
        notes.append(f"{_WARN} Grade not stated")

    if item.get("grade_mismatch_note"):
        notes.append(item["grade_mismatch_note"])

    listing_type = item.get("listing_type")
    if listing_type == "BIN":
        notes.append(f"{_WARN} Buy It Now")
        ends = None
    else:
        ends = format_time_remaining(item.get("end_date_iso"), now)

    bids = item.get("bid_count")
    bids_cell = _DASH if bids is None else str(bids)

    item_id = item.get("item_id")
    link = f"[{index}](https://www.ebay.com/itm/{item_id})"
    cert_cell = _cert_cell(item.get("certifier"), item.get("label"))
    pq_cell = _PAGE_QUALITY_DISPLAY.get(item.get("page_quality"))
    # BUI-919: the standing-rule drop reason, phrase included. It is rendered
    # in its own column rather than folded into Notes so /comic:buy's gate can
    # read one cell and never has to parse a semicolon-joined note list.
    defect_cell = format_defect_cell(item.get("condition_defects"))
    cells = [
        link, _cell(series), _cell(issue), _cell(item.get("cover_year")),
        _cell(grade_cell), _cell(item.get("variant")), _cell(listing_type),
        _cell(item.get("current_price")), bids_cell, _cell(item.get("seller")),
        _cell(ends), _cell("; ".join(notes)),
        _cell(f"{_WARN} {defect_cell}" if defect_cell else None),
        _cell(cert_cell), _cell(pq_cell),
    ]
    return "| " + " | ".join(cells) + " |"


def format_identify_table(items, now, failed=()):
    """The full /comic:identify markdown table plus one warning line per input
    that produced no row (never silently dropped, BUI-166). ``failed`` holds
    ``(item, reason)`` pairs; a bare string is a fetch failure."""
    lines = []
    if items:
        lines.append("| " + " | ".join(IDENTIFY_COLUMNS) + " |")
        lines.append("|" + "---|" * len(IDENTIFY_COLUMNS))
        for i, item in enumerate(items, 1):
            lines.append(identify_row(i, item, now))
    for entry in failed:
        item, reason = entry if isinstance(entry, tuple) else (entry, None)
        reason = reason or "fetch failed \u2014 see the ebay-fetch error line on stderr"
        lines.append(f"{_WARN} Item {item}: {reason}")
    return "\n".join(lines)


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
        "--identify",
        action="store_true",
        help="Print the /comic:identify markdown table (series/issue via "
             "comic-identify, cover year, grade verdict, relative time to end) "
             "instead of the plain listing table (BUI-900)",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="ISO-8601 UTC reference time for the Ends column of --identify "
             "(default: the current time)",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )
    parser.add_argument(
        "--active-asks",
        type=str,
        default=None,
        metavar="KEYWORD",
        help="BUI-954: search active Buy-It-Now listings for KEYWORD "
             "(typically '<title> #<issue>') and print the lowest ask + "
             "count for the same identity as JSON ({\"low\": ..., \"n\": "
             "...}), instead of fetching the positional item ids. Requires "
             "--grade; --certifier/--label narrow the match to a certified "
             "target (omit both for a raw target, which excludes any "
             "listing naming a certifier). If the Browse search itself "
             "fails, the JSON instead carries {\"low\": null, \"n\": null, "
             "\"error\": \"...\"} (BUI-971) — never a zero count that would "
             "read as 'no live asks'. Used by comic-fmv to show a "
             "display-only ceiling on refused rows — see "
             "docs/conventions/fmv-math-spec.md §7.",
    )
    parser.add_argument(
        "--grade",
        type=float,
        default=None,
        help="Target CGC-scale grade to match for --active-asks (required "
             "with --active-asks).",
    )
    parser.add_argument(
        "--certifier",
        choices=["cgc", "cbcs", "other"],
        default=None,
        help="Certifier to match for --active-asks on a CERTIFIED target "
             "(omit for a raw target).",
    )
    parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="Slab label to match for --active-asks on a CERTIFIED target "
             "(e.g. 'universal', 'signature_series'); ignored for a raw "
             "target.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=200,
        help="Max active listings to fetch for --active-asks (default 200: one Browse page; a lower cap truncates the page before the graded listings, which sit anywhere in best-match order).",
    )

    args = parser.parse_args(argv)

    if args.active_asks is not None:
        if args.grade is None:
            print("Error: --active-asks requires --grade.", file=sys.stderr)
            sys.exit(2)
        client_id, client_secret, base_url = load_config()
        if args.env:
            base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
        token = get_token(client_id, client_secret, base_url)
        result = search_active_asks(
            args.active_asks, token, base_url, grade=args.grade,
            certifier=args.certifier, label=args.label,
            max_results=args.max_results,
        )
        print(json.dumps(result))
        return

    # Collect item args from CLI, falling back to stdin only when none were
    # given (BUI-538). Reading stdin whenever it merely isn't a TTY hung
    # forever in every non-interactive caller — an agent shell, a `for` loop,
    # CI, cron — because an inherited pipe never reaches EOF, so `for line in
    # sys.stdin` blocked even though the item IDs were already in argv.
    # isatty() answers "is this interactive?", which is not the question that
    # matters here: whether the caller intended to pipe IDs in. Absence of
    # positional args is that signal, and it's the same rule comic_identify.py
    # already uses. Both supported invocations still work — `ebay-fetch <id>`
    # (stdin untouched) and `echo <id> | ebay-fetch` (stdin read) — at the cost
    # of combining args *and* piped IDs in one call, which was undocumented and
    # unusable anyway since it hung wherever piping actually happens.
    raw_items = list(args.items) if args.items else []
    if not raw_items and not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line:
                raw_items.append(line)

    if not raw_items:
        parser.print_help()
        sys.exit(1)

    # Extract item IDs
    item_ids = []
    unparseable = []
    for arg in raw_items:
        item_id = extract_item_id(arg)
        if item_id:
            item_ids.append(item_id)
        else:
            unparseable.append(arg)

    if not item_ids:
        print("Error: No valid item IDs found.", file=sys.stderr)
        sys.exit(1)

    # --identify's reference time: validate before spending any API call.
    now = None
    if args.identify:
        try:
            now = parse_utc_timestamp(args.now) if args.now else datetime.now(timezone.utc)
        except ValueError:
            print(f"Error: --now is not an ISO-8601 timestamp: {args.now!r}", file=sys.stderr)
            sys.exit(2)

    # Auth
    client_id, client_secret, base_url = load_config()
    if args.env:
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE

    token = get_token(client_id, client_secret, base_url)

    # Fetch items
    results = []
    failed = [(arg, "could not parse an item id from this input") for arg in unparseable]
    for item_id in item_ids:
        data = fetch_item(item_id, token, base_url)
        if data:
            parsed = parse_item(data)
            results.append(parsed)
        else:
            failed.append((item_id, None))

    # Output
    if args.identify:
        print(format_identify_table(results, now, failed))
        if not results:
            # Every fetch failed: a hard failure, never an empty table (BUI-166).
            sys.exit(1)
    elif args.json_output:
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
