"""ebay-bid-status: eBay's own verdict on ended auctions you bid on (BUI-1346).

Gixen marks a snipe WON when its max bid equals the final price, but eBay
gives an exact tie to the earlier bidder, so a tie is a phantom win. This
script asks eBay directly. The comics server shells out to it
(server/win_confirm.py) and reverses a Gixen WON only on a positive OUTBID.

Two sources, tried in order per item:

1. Auction GraphQL `itemBiddingActivity` (needs the user token to carry
   `sell.auction.read`). It answers WINNING, OUTBID, or null (no bid of ours)
   plus the final price, for about 90 days after an auction ends.
   `userBiddingActivity` is FORBIDDEN on this keyset and is never called.
2. Trading `GetMyeBayBuying` WonList/LostList (DurationInDays 60), which works
   on the base-scope token. A LostList entry is a definitive OUTBID and a
   WonList entry a definitive WINNING. Absence from both is NOT evidence:
   the item stays unresolved.

Output (`--json`): one object per requested item, in request order:

    {"item_id": "...", "resolved": true|false,
     "status": "WINNING"|"OUTBID"|null, "final_price": 12.5|null,
     "currency": "USD"|null, "source": "itemBiddingActivity"|"GetMyeBayBuying"|null}

`resolved: false` means eBay gave no definitive answer (failed or missing
lookup); a caller must change nothing for it. Exit codes:

    0  ran (some items may be unresolved)
    2  usage error
    3  user token missing/expired (run `ebay-auth login`)
    5  every source failed (network, HTTP, or auth/scope error)
"""

import argparse
import importlib.metadata
import json
import sys
import time
import xml.etree.ElementTree as ET

import requests

import ebay_user_token as eut

GRAPHQL_ENDPOINT = "https://graphqlapi.ebay.com/graphql"
TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
COMPAT_LEVEL = "1349"
DURATION_DAYS = 60  # the Trading WonList/LostList maximum
PER_PAGE = 200
MAX_PAGES = 25
GRAPHQL_PAUSE_S = 0.3
EXIT_USAGE = 2
EXIT_NETWORK = 5
SOURCE_GRAPHQL = "itemBiddingActivity"
SOURCE_TRADING = "GetMyeBayBuying"
STATUSES = ("WINNING", "OUTBID")

_AMT = "original { value currency } converted { value currency }"
ITEM_QUERY = (
    "query($id: ID!) { itemBiddingActivity(input: {itemId: $id}) { itemBiddingActivity {"
    f" leadingBid {{ bidAmount {{ {_AMT} }} }}"
    " bidderAuctionDetails { userBiddingStatus }"
    " item { itemId }"
    " } } }"
)


def _version_string() -> str:
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-bid-status {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


class SourceUnavailable(Exception):
    """The whole source is unusable this run (auth/scope error, network, HTTP)."""


def _unresolved(item_id):
    return {
        "item_id": item_id, "resolved": False, "status": None,
        "final_price": None, "currency": None, "source": None,
    }


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_auth_error(errors):
    for err in errors or []:
        code = ((err or {}).get("extensions") or {}).get("errorCode") or ""
        if code.upper() in ("FORBIDDEN", "UNAUTHENTICATED", "UNAUTHORIZED"):
            return True
    return False


def parse_graphql_item(item_id, body):
    """Return a result dict for one itemBiddingActivity response body.

    Raises SourceUnavailable on an auth/scope error (it applies to every item,
    so the caller stops asking GraphQL). Any other error, a null payload, or
    an unknown status string is an unresolved item, never a guess.
    """
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors and _is_auth_error(errors):
        raise SourceUnavailable("itemBiddingActivity refused the token (auth/scope)")
    data = (body or {}).get("data") if isinstance(body, dict) else None
    act = ((data or {}).get("itemBiddingActivity") or {}).get("itemBiddingActivity")
    if errors or not isinstance(act, dict):
        return _unresolved(item_id)
    status = (act.get("bidderAuctionDetails") or {}).get("userBiddingStatus")
    if status is not None and status not in STATUSES:
        return _unresolved(item_id)
    amount = ((act.get("leadingBid") or {}).get("bidAmount") or {})
    money = amount.get("converted") or amount.get("original") or {}
    return {
        "item_id": item_id, "resolved": True, "status": status,
        "final_price": _to_float(money.get("value")), "currency": money.get("currency"),
        "source": SOURCE_GRAPHQL,
    }


def fetch_graphql_item(token, item_id):
    try:
        resp = requests.post(
            GRAPHQL_ENDPOINT,
            json={"query": ITEM_QUERY, "variables": {"id": item_id}},
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise SourceUnavailable(f"network error calling {GRAPHQL_ENDPOINT}: {type(exc).__name__}") from None
    if resp.status_code in (401, 403):
        raise SourceUnavailable(f"HTTP {resp.status_code} from {GRAPHQL_ENDPOINT} (auth/scope)")
    try:
        body = resp.json()
    except ValueError:
        return _unresolved(item_id)
    if resp.status_code != 200 and not (isinstance(body, dict) and body.get("errors")):
        return _unresolved(item_id)
    return parse_graphql_item(item_id, body)


def _trading_body(won_page, lost_page):
    """Request each list at its own page; a None page omits that list."""
    def lst(tag, page):
        if page is None:
            return ""
        return (
            f"<{tag}><Include>true</Include><DurationInDays>{DURATION_DAYS}</DurationInDays>"
            f"<Pagination><EntriesPerPage>{PER_PAGE}</EntriesPerPage>"
            f"<PageNumber>{page}</PageNumber></Pagination></{tag}>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<GetMyeBayBuyingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"{lst('WonList', won_page)}{lst('LostList', lost_page)}"
        "<DetailLevel>ReturnAll</DetailLevel></GetMyeBayBuyingRequest>"
    )


def _money(el):
    """(value, currency) for a money element. A missing or non-positive value
    is (None, None), never 0.0: a sold item has a price or none is known."""
    if el is None:
        return None, None
    value = _to_float(el.text)
    if value is None or value <= 0:
        return None, None
    return value, el.get("currencyID")


def _list_entries(container):
    """{item_id: (price, currency)} for every Item under a Won/LostList.

    Price is the Item's SellingStatus/CurrentPrice when positive, else the
    enclosing Transaction's TransactionPrice/TotalPrice/AmountPaid, else None.
    """
    out = {}
    if container is None:
        return out
    parents = {c: p for p in container.iter() for c in p}
    for item in container.iter(f"{{{NS['e']}}}Item"):
        iid = item.findtext("e:ItemID", namespaces=NS)
        if not iid:
            continue
        price, currency = _money(item.find("e:SellingStatus/e:CurrentPrice", NS))
        parent = parents.get(item)
        if price is None and parent is not None:
            for path in ("e:TransactionPrice", "e:TotalPrice", "e:AmountPaid"):
                price, currency = _money(parent.find(path, NS))
                if price is not None:
                    break
        out[iid] = (price, currency)
    return out


def _pages(container):
    if container is None:
        return 1
    try:
        return int(container.findtext("e:PaginationResult/e:TotalNumberOfPages", "1", NS) or 1)
    except ValueError:
        return 1


def parse_trading_page(content):
    """Return (won, lost, won_pages, lost_pages) for one GetMyeBayBuying response."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise SourceUnavailable("unparseable XML from GetMyeBayBuying") from None
    ack = root.findtext("e:Ack", namespaces=NS)
    if ack not in ("Success", "Warning"):
        codes = [e.findtext("e:ErrorCode", namespaces=NS) for e in root.findall("e:Errors", NS)]
        raise SourceUnavailable(f"GetMyeBayBuying Ack={ack} (errors {codes})")
    won_el, lost_el = root.find("e:WonList", NS), root.find("e:LostList", NS)
    return _list_entries(won_el), _list_entries(lost_el), _pages(won_el), _pages(lost_el)


def fetch_trading_lists(token):
    """Return (won, lost), each list paged to its own end. Raises SourceUnavailable."""
    won, lost, page = {}, {}, 1
    won_pages = lost_pages = 1  # real totals are learned from page 1
    while True:
        try:
            resp = requests.post(
                TRADING_ENDPOINT,
                data=_trading_body(
                    page if page <= won_pages else None,
                    page if page <= lost_pages else None,
                ).encode(),
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
            raise SourceUnavailable(f"network error calling {TRADING_ENDPOINT}: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise SourceUnavailable(f"HTTP {resp.status_code} from {TRADING_ENDPOINT}")
        w, lo, wp, lp = parse_trading_page(resp.content)
        won.update(w)
        lost.update(lo)
        if page == 1:
            won_pages, lost_pages = wp, lp
        if page >= min(max(won_pages, lost_pages), MAX_PAGES):
            return won, lost
        page += 1


def trading_result(item_id, won, lost):
    """A LostList entry is OUTBID, a WonList entry WINNING. Absence from both,
    or presence in both, is unresolved: absence is never evidence."""
    in_won, in_lost = item_id in won, item_id in lost
    if in_won == in_lost:
        return _unresolved(item_id)
    price, currency = (lost if in_lost else won)[item_id]
    return {
        "item_id": item_id, "resolved": True,
        "status": "OUTBID" if in_lost else "WINNING",
        "final_price": price, "currency": currency, "source": SOURCE_TRADING,
    }


def lookup(token, item_ids, *, sleep=time.sleep, log=lambda m: print(m, file=sys.stderr)):
    """Return (results in request order, any_source_worked)."""
    results = {iid: _unresolved(iid) for iid in item_ids}
    graphql_ok = trading_ok = False
    graphql_live = True
    for i, iid in enumerate(item_ids):
        if not graphql_live:
            break
        try:
            results[iid] = fetch_graphql_item(token, iid)
            graphql_ok = True
        except SourceUnavailable as exc:
            log(f"ebay-bid-status: itemBiddingActivity unavailable, using the Trading fallback: {exc}")
            graphql_live = False
        if graphql_live and i + 1 < len(item_ids):
            sleep(GRAPHQL_PAUSE_S)
    pending = [iid for iid in item_ids if not results[iid]["resolved"]]
    if pending:
        try:
            won, lost = fetch_trading_lists(token)
            trading_ok = True
            for iid in pending:
                results[iid] = trading_result(iid, won, lost)
        except SourceUnavailable as exc:
            log(f"ebay-bid-status: GetMyeBayBuying unavailable: {exc}")
    return [results[iid] for iid in item_ids], (graphql_ok or trading_ok or not pending)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ebay-bid-status", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=_version_string())
    ap.add_argument("item_ids", nargs="+", help="eBay item IDs of ended auctions")
    ap.add_argument("--json", action="store_true", help="print JSON (the only output format)")
    args = ap.parse_args(argv)
    item_ids = list(dict.fromkeys(i.strip() for i in args.item_ids if i.strip()))
    if not item_ids or not all(i.isdigit() for i in item_ids):
        print("ebay-bid-status: item IDs must be numeric", file=sys.stderr)
        return EXIT_USAGE
    try:
        token = eut.get_user_access_token()
    except eut.UserTokenError as exc:
        print(f"ebay-bid-status: {exc}", file=sys.stderr)
        return exc.exit_code
    results, ok = lookup(token, item_ids)
    if not ok:
        print("ebay-bid-status: every eBay source failed", file=sys.stderr)
        return EXIT_NETWORK
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
