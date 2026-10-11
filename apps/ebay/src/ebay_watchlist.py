"""ebay-watchlist: read the eBay watchlist via the Trading API (BUI-1210).

Calls GetMyeBayBuying with a WatchList container, authenticated by the user
token from ebay_user_token. Exit codes follow the plan's error contract and a
failure never prints a list (an empty list means the watchlist is empty):

    3  token missing/expired/revoked (run `ebay-auth login`)
    4  Trading API Ack=Failure
    5  network or HTTP error
    6  seen-store unreadable or corrupt (BUI-1253): never read as an empty store

Seen-store (BUI-1253): a local JSON file mapping item_id -> {outcome, date}.
/comic:buy records each watchlist item after the user decides at its gate
(--record ID --outcome X); the default listing hides recorded items and
reports a seen count on stderr; --all shows them (annotated with a "seen"
field in --json); --forget ID clears one. A MISSING file is the one legitimate
empty store (first use). An unreadable, non-JSON, or wrong-shape file exits 6.
"""

import argparse
import fcntl
import importlib.metadata
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

import ebay_user_token as eut

ENDPOINT = "https://api.ebay.com/ws/api.dll"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
COMPAT_LEVEL = "1349"
PER_PAGE = 200
MAX_PAGES = 100  # runaway guard; 20,000 watched items
EXIT_FAILURE_ACK = 4
EXIT_NETWORK = 5
EXIT_STORE = 6
OUTCOMES = ("sniped", "skipped", "over_cap", "non_comic")
SEEN_PATH_ENV = "EBAY_WATCHLIST_SEEN_PATH"
DEFAULT_SEEN_PATH = Path.home() / ".config" / "ebay" / "watchlist-seen.json"
BIN_TYPES = {"FixedPriceItem", "StoresFixedPrice"}
# The WatchList reports an auction as "Auction"; other Trading calls (and the
# WonList in this same response) say "Chinese". Accept both (BUI-1214).
AUCTION_TYPES = {"Chinese", "Auction"}
# eBay intermittently answers Ack=Success with the WatchList element missing
# (about every other call on 2026-10-09). That is a flap, never an empty
# watchlist, so retry it and fail loudly if it persists (R4, BUI-1214).
MISSING_WATCHLIST_TRIES = 5
# Trading error codes that mean the user token itself was rejected: point the
# user at `ebay-auth login` (exit 3) rather than a generic Ack failure.
AUTH_ERROR_CODES = {"931", "932", "21916984", "21917053"}


def _version_string() -> str:
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-watchlist {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


class WatchlistError(Exception):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


class MissingWatchlistError(WatchlistError):
    """Ack=Success but no WatchList element: retryable."""


def _request_body(page):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<GetMyeBayBuyingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        "<WatchList><Include>true</Include><Pagination>"
        f"<EntriesPerPage>{PER_PAGE}</EntriesPerPage><PageNumber>{page}</PageNumber>"
        "</Pagination></WatchList><DetailLevel>ReturnAll</DetailLevel>"
        "</GetMyeBayBuyingRequest>"
    )


def _fetch_page(token, page):
    """POST one page. Returns (items, total_pages, warnings). Raises WatchlistError."""
    try:
        resp = requests.post(
            ENDPOINT,
            data=_request_body(page).encode(),
            headers={
                "X-EBAY-API-CALL-NAME": "GetMyeBayBuying",
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
                "X-EBAY-API-IAF-TOKEN": token,
                "Content-Type": "text/xml",
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        raise WatchlistError(
            f"network error calling {ENDPOINT}: {type(exc).__name__}", EXIT_NETWORK
        ) from None
    if resp.status_code != 200:
        raise WatchlistError(f"HTTP {resp.status_code} from {ENDPOINT}", EXIT_NETWORK)
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        raise WatchlistError(f"unparseable XML response from {ENDPOINT}", EXIT_NETWORK) from None

    ack = root.findtext("e:Ack", namespaces=NS)
    errors = [
        (err.findtext("e:ErrorCode", namespaces=NS), err.findtext("e:LongMessage", namespaces=NS))
        for err in root.findall("e:Errors", NS)
    ]
    if ack not in ("Success", "Warning"):
        detail = "; ".join(f"{code}: {msg}" for code, msg in errors) or "no error detail"
        if any(code in AUTH_ERROR_CODES for code, _ in errors):
            raise WatchlistError(
                f"eBay rejected the user token ({detail}); {eut.RELOGIN_HINT}", eut.EXIT_RELOGIN
            )
        raise WatchlistError(f"eBay Trading API Ack={ack}: {detail}", EXIT_FAILURE_ACK)
    warnings = [f"{code}: {msg}" for code, msg in errors] if ack == "Warning" else []

    wl = root.find("e:WatchList", NS)
    if wl is None:
        raise MissingWatchlistError(
            f"eBay returned Ack={ack} with no WatchList element", EXIT_NETWORK
        )
    items = []
    for it in wl.findall("e:ItemArray/e:Item", NS):
        f = lambda p: it.findtext(p, namespaces=NS)  # noqa: E731
        bids = f("e:SellingStatus/e:BidCount")
        items.append(
            {
                "item_id": f("e:ItemID"),
                "title": f("e:Title"),
                "listing_type": f("e:ListingType"),
                "end_time": f("e:ListingDetails/e:EndTime"),
                "current_price": f("e:SellingStatus/e:CurrentPrice"),
                "bid_count": int(bids) if bids and bids.isdigit() else None,
                "seller": f("e:Seller/e:UserID"),
                "url": f("e:ListingDetails/e:ViewItemURL"),
            }
        )
    try:
        total = int(wl.findtext("e:PaginationResult/e:TotalNumberOfPages", "1", NS) or 1)
    except ValueError:
        total = 1
    return items, total, warnings


def _fetch_page_retrying(token, page):
    for attempt in range(1, MISSING_WATCHLIST_TRIES + 1):
        try:
            return _fetch_page(token, page)
        except MissingWatchlistError as exc:
            if attempt == MISSING_WATCHLIST_TRIES:
                raise MissingWatchlistError(
                    f"{exc} on page {page}, {attempt} tries in a row; refusing to report "
                    "an empty watchlist",
                    EXIT_NETWORK,
                ) from None


def fetch_watchlist(token):
    """Return (all items across every page, warnings). Raises WatchlistError."""
    items, warnings, page = [], [], 1
    while True:
        page_items, total, warns = _fetch_page_retrying(token, page)
        items.extend(page_items)
        warnings.extend(warns)
        if page >= total:
            break
        if page >= MAX_PAGES:
            raise WatchlistError(
                f"watchlist has {total} pages, more than the {MAX_PAGES}-page cap", EXIT_NETWORK
            )
        page += 1
    return items, warnings


def _parse_end(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def filter_items(items, item_type="all", include_ended=False, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for it in items:
        if item_type == "auction" and it["listing_type"] not in AUCTION_TYPES:
            continue
        if item_type == "bin" and it["listing_type"] not in BIN_TYPES:
            continue
        if not include_ended:
            end = _parse_end(it["end_time"])
            if end is not None and end <= now:
                continue
        out.append(it)
    return out


# --- Seen-store (BUI-1253) ---------------------------------------------------


class SeenStoreError(WatchlistError):
    def __init__(self, message):
        super().__init__(message, EXIT_STORE)


def seen_path():
    override = os.environ.get(SEEN_PATH_ENV)
    return Path(override) if override else DEFAULT_SEEN_PATH


def load_seen(path):
    """{item_id: {outcome, date}}. A missing file is {} (first use). Anything
    unreadable or malformed raises SeenStoreError: unknown seen state must stop
    the run, never read as "nothing has been seen"."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SeenStoreError(f"cannot read seen-store {path}: {exc}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise SeenStoreError(f"seen-store {path} is not valid JSON") from None
    if not isinstance(data, dict) or not all(
        isinstance(v, dict) and v.get("outcome") in OUTCOMES for v in data.values()
    ):
        raise SeenStoreError(f"seen-store {path} has an unexpected shape")
    return data


def _save_seen(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _update_seen(path, mutate):
    """Locked read-modify-write; returns mutate's result. Raises SeenStoreError."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = load_seen(path)
            result = mutate(data)
            _save_seen(path, data)
            return result
    except OSError as exc:
        raise SeenStoreError(f"cannot write seen-store {path}: {exc}") from None


def record_seen(path, item_id, outcome, today=None):
    stamp = (today or datetime.now(timezone.utc).date()).isoformat()
    _update_seen(path, lambda d: d.__setitem__(item_id, {"outcome": outcome, "date": stamp}))


def forget_seen(path, item_id):
    """Returns True if the item was in the store."""
    return _update_seen(path, lambda d: d.pop(item_id, None) is not None)


def _table(items):
    cols = ["item_id", "type", "ends", "price", "bids", "seller", "title"]
    rows = [
        [
            it["item_id"] or "",
            it["listing_type"] or "",
            (it["end_time"] or "")[:16].replace("T", " "),
            it["current_price"] or "",
            "" if it["bid_count"] is None else str(it["bid_count"]),
            it["seller"] or "",
            (it["title"] or "")
            + (f" [seen: {it['seen']['outcome']} {it['seen']['date']}]" if it.get("seen") else ""),
        ]
        for it in items
    ]
    widths = [
        max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)
    ]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, widths))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ebay-watchlist", description="Read the eBay watchlist")
    parser.add_argument("--version", action="version", version=_version_string())
    parser.add_argument("--type", choices=["auction", "bin", "all"], default="all")
    parser.add_argument("--include-ended", action="store_true", help="include ended listings")
    parser.add_argument("--all", action="store_true", help="show items already seen")
    parser.add_argument("--record", metavar="ITEM_ID", help="record ITEM_ID as seen (no fetch)")
    parser.add_argument("--outcome", choices=OUTCOMES, help="outcome for --record")
    parser.add_argument("--forget", metavar="ITEM_ID", help="clear ITEM_ID from the seen-store")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON list of objects")
    fmt.add_argument("--urls", action="store_true", help="one URL per line")
    args = parser.parse_args(argv)

    path = seen_path()
    if args.record is not None or args.forget is not None:
        if args.record is not None and args.forget is not None:
            parser.error("--record and --forget are mutually exclusive")
        if not (args.record or args.forget or "").strip():
            parser.error("item id must not be empty")
        if args.record is not None and not args.outcome:
            parser.error("--record requires --outcome")
        if args.forget is not None and args.outcome:
            parser.error("--outcome only applies to --record")
        try:
            if args.record is not None:
                record_seen(path, args.record.strip(), args.outcome)
                print(f"ebay-watchlist: recorded {args.record.strip()} as {args.outcome}")
            elif forget_seen(path, args.forget.strip()):
                print(f"ebay-watchlist: forgot {args.forget.strip()}")
            else:
                print(f"ebay-watchlist: {args.forget.strip()} was not in the seen-store")
        except SeenStoreError as exc:
            print(f"ebay-watchlist: {exc}", file=sys.stderr)
            return exc.exit_code
        return 0
    if args.outcome:
        parser.error("--outcome only applies to --record")

    try:
        seen = {} if args.all else load_seen(path)
        token = eut.get_user_access_token()
        items, warnings = fetch_watchlist(token)
    except eut.UserTokenError as exc:
        print(f"ebay-watchlist: {exc}", file=sys.stderr)
        return exc.exit_code
    except WatchlistError as exc:
        print(f"ebay-watchlist: {exc}", file=sys.stderr)
        return exc.exit_code

    for w in warnings:
        print(f"ebay-watchlist: warning: {w}", file=sys.stderr)
    items = filter_items(items, args.type, args.include_ended)
    if args.all:
        try:
            seen = load_seen(path)
        except SeenStoreError as exc:
            print(f"ebay-watchlist: {exc}", file=sys.stderr)
            return exc.exit_code
        items = [{**it, "seen": seen.get(it["item_id"])} for it in items]
    else:
        hidden = [it for it in items if it["item_id"] in seen]
        items = [it for it in items if it["item_id"] not in seen]
        print(
            f"ebay-watchlist: seen_hidden={len(hidden)} (--all shows them)", file=sys.stderr
        )

    if args.json:
        print(json.dumps(items, indent=2))
    elif args.urls:
        for it in items:
            if it["url"]:
                print(it["url"])
    else:
        print(_table(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
