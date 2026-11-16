"""ebay-offer-log: log your eBay offers on the comics server by hand (BUI-1415).

Records ask, offer, counteroffer, final price, and outcome in the server's
offer log (`/api/comics/offers`). It only records: it never sends an offer
to eBay or buys anything.

    ebay-offer-log add ITEM_ID --offer 240 [--ask 300] [--seller S] [--title T]
    ebay-offer-log update OFFER_ID [--counter 270] [--final 265] [--outcome accepted]
    ebay-offer-log list [--item-id ITEM_ID] [--seller S] [--json]

Exit codes: 0 ok; 1 server error; 2 usage (no COMICS_SERVER_URL, bad input).
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from purchases_pull import server_base

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2
OUTCOMES = ("pending", "accepted", "declined", "countered", "expired", "withdrawn")
HTTP_TIMEOUT_S = 30
# CLI flag -> API field, for the fields both add and update accept.
_FIELDS = {"ask": "ask", "counter": "counteroffer", "final": "final_price",
           "outcome": "outcome", "seller": "seller", "title": "title", "notes": "notes"}


def _add_common(p):
    p.add_argument("--ask", type=float, help="the listing's asking price")
    p.add_argument("--counter", type=float, help="the seller's counteroffer")
    p.add_argument("--final", type=float, help="the final agreed price")
    p.add_argument("--outcome", choices=OUTCOMES)
    p.add_argument("--seller")
    p.add_argument("--title")
    p.add_argument("--notes")


def _body(args):
    return {api: getattr(args, flag) for flag, api in _FIELDS.items()
            if getattr(args, flag) is not None}


def _fmt(o):
    money = lambda v: "-" if v is None else f"${v:.2f}"  # noqa: E731
    return (f"#{o['id']:<4} {o['offered_at'][:10]}  {o['item_id']}  ask {money(o['ask'])}  "
            f"offer {money(o['offer'])}  counter {money(o['counteroffer'])}  "
            f"final {money(o['final_price'])}  {o['outcome']:<9} {o['seller'] or ''}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ebay-offer-log", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="log a new offer")
    add.add_argument("item_id")
    add.add_argument("--offer", type=float, required=True, help="the amount you offered")
    _add_common(add)
    upd = sub.add_parser("update", help="record a counteroffer, final price, or outcome")
    upd.add_argument("offer_id", type=int)
    upd.add_argument("--offer", type=float, help="a revised offer amount")
    _add_common(upd)
    ls = sub.add_parser("list", help="list logged offers")
    ls.add_argument("--item-id")
    ls.add_argument("--seller")
    ls.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    base = server_base()
    if not base:
        print("error: COMICS_SERVER_URL is not set", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.cmd == "add":
            resp = requests.post(f"{base}/api/comics/offers",
                                 json={"item_id": args.item_id, "offer": args.offer, **_body(args)},
                                 timeout=HTTP_TIMEOUT_S)
        elif args.cmd == "update":
            body = _body(args)
            if args.offer is not None:
                body["offer"] = args.offer
            if not body:
                print("error: update needs at least one field to change", file=sys.stderr)
                return EXIT_USAGE
            resp = requests.patch(f"{base}/api/comics/offers/{args.offer_id}", json=body,
                                  timeout=HTTP_TIMEOUT_S)
        else:
            params = {k: v for k, v in (("item_id", args.item_id), ("seller", args.seller)) if v}
            resp = requests.get(f"{base}/api/comics/offers", params=params, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        print(f"error: comics server unreachable: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if resp.status_code == 422:
        print(f"error: rejected: {resp.text[:500]}", file=sys.stderr)
        return EXIT_USAGE
    if resp.status_code >= 400:
        print(f"error: HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return EXIT_FAILURE
    data = resp.json()
    if args.cmd == "list":
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print("\n".join(_fmt(o) for o in data["offers"]) or "no offers logged")
    else:
        print(_fmt(data))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
