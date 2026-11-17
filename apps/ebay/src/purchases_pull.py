"""ebay-purchases-pull: record every eBay purchase on the comics server (BUI-1415).

Reads the buyer's own orders through the Trading API's GetOrders (the same
pull `ebay-shipped --auto-submit` uses, via `shipped_submit._fetch_page`) and
turns each order line item into one purchase record: item id, title, seller,
purchase date, price, shipping, tax, and total. Auction wins, Buy It Now, and
accepted Best Offers all arrive as orders, so all of them land.

Dry run by default: prints what it would record and writes nothing. `--apply`
POSTs the batch to `/api/comics/purchases`, an idempotent upsert keyed by
`OrderLineItemID`, so overlapping windows never double-count.

GetOrders says nothing about how an item was bought (no listing type, no Best
Offer flag), so every row goes up as `purchase_type=unknown`; the server
refines it from Gixen bids and the offer log at read time.

Read-only against eBay. Never places an offer, buys, or writes the
collection.

Exit codes: 0 ok; 1 eBay or server failure; 2 usage (bad --days, or --apply
with no COMICS_SERVER_URL).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import requests

import ebay_user_token as eut
from shipped_submit import MAX_PAGES, NS, SubmitError, _fetch_page, ping_heartbeat

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2
DEFAULT_DAYS = 30
MAX_DAYS = 90  # GetOrders' CreateTime window limit
POST_CHUNK = 500
HTTP_TIMEOUT_S = 60
# Order states that mean the money did not move. Anything else (including a
# cancel request still pending or rejected) counts against the budget.
CANCELLED_ORDER_STATUSES = {"Cancelled"}
CANCELLED_CANCEL_STATUSES = {"CancelComplete", "CancelClosedWithRefund", "CancelClosedNoRefund"}
# Order/MonetaryDetails/Refunds/Refund/RefundStatus values that mean the money
# did not come back (assumed from the Trading API PaymentStatus enum).
UNSETTLED_REFUND_STATUSES = {"Failed", "Pending"}
HEARTBEAT_PATH = "/api/heartbeat/ebay-purchases-pull"


def _allocate(amount, weights):
    """Split `amount` across `weights` proportionally in cents; the last share
    takes the rounding remainder so the shares sum exactly to `amount`."""
    total_w = sum(weights)
    shares = [round(amount * (w / total_w if total_w else 1 / len(weights)), 2)
              for w in weights]
    if shares:
        shares[-1] = round(amount - sum(shares[:-1]), 2)
    return shares


def _order_refund(order, order_id, warnings):
    """Settled refunds on the order as (amount, currency), or (0.0, None).
    Assumes GetOrders' Order/MonetaryDetails/Refunds/Refund/RefundAmount."""
    total, currency = 0.0, None
    for ref in order.findall("e:MonetaryDetails/e:Refunds/e:Refund", NS):
        if ref.findtext("e:RefundStatus", namespaces=NS) in UNSETTLED_REFUND_STATUSES:
            continue
        amt_el = ref.find("e:RefundAmount", NS)
        try:
            amt = float(amt_el.text)
        except (AttributeError, TypeError, ValueError):
            warnings.append(f"order {order_id}: refund with no readable RefundAmount ignored")
            continue
        cur = amt_el.get("currencyID", "USD")
        if currency not in (None, cur):
            warnings.append(f"order {order_id}: refunds in mixed currencies; ignored a {cur} refund")
            continue
        currency = cur
        total += amt
    return round(total, 2), currency


def _fill_shipping(order_rows, order_shipping, order_id, warnings):
    """Multi-line orders often carry shipping only on the order. Spread what
    the per-line costs do not account for across the lines missing one,
    weighted by price. With no order-level cost either, shipping stays None.
    Either way those lines keep `shipping_estimated` set."""
    missing = [r for r in order_rows if r["shipping_estimated"]]
    if not missing:
        return
    known = sum(r["shipping"] or 0.0 for r in order_rows if not r["shipping_estimated"])
    if order_shipping is None or order_shipping < known - 0.005:
        warnings.append(f"order {order_id}: shipping missing on {len(missing)} line(s) and not "
                        "derivable from the order; spend understates it")
        return
    for r, share in zip(missing, _allocate(round(order_shipping - known, 2),
                                           [r["price"] for r in missing])):
        r["shipping"] = share


def _money(el, path):
    text = el.findtext(path, namespaces=NS)
    if text in (None, ""):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_purchases(root):
    """One purchase dict per order line item, plus warnings for orders whose
    line totals do not add up to eBay's order total."""
    rows, warnings = [], []
    for order in root.findall("e:OrderArray/e:Order", NS):
        o = lambda p: order.findtext(p, namespaces=NS)  # noqa: E731
        txns = order.findall("e:TransactionArray/e:Transaction", NS)
        cancelled = (o("e:OrderStatus") in CANCELLED_ORDER_STATUSES
                     or o("e:CancelStatus") in CANCELLED_CANCEL_STATUSES)
        order_shipping = _money(order, "e:ShippingServiceSelected/e:ShippingServiceCost")
        order_tax = _money(order, "e:ShippingDetails/e:SalesTax/e:SalesTaxAmount")
        order_rows = []
        for txn in txns:
            t = lambda p: txn.findtext(p, namespaces=NS)  # noqa: E731
            item_id = (t("e:Item/e:ItemID") or "").strip()
            txn_id = (t("e:TransactionID") or "").strip()
            if not item_id:
                warnings.append(f"order {o('e:OrderID')}: line item with no ItemID skipped")
                continue
            qty = int(t("e:QuantityPurchased") or 1)
            unit = _money(txn, "e:TransactionPrice")
            if unit is None:
                warnings.append(f"order {o('e:OrderID')} item {item_id}: no TransactionPrice; skipped")
                continue
            price_el = txn.find("e:TransactionPrice", NS)
            currency = price_el.get("currencyID", "USD") if price_el is not None else "USD"
            shipping = _money(txn, "e:ActualShippingCost")
            handling = _money(txn, "e:ActualHandlingCost") or 0.0
            estimated = False
            if shipping is None and len(txns) == 1:
                shipping = order_shipping
            elif shipping is None:
                estimated = True  # multi-line, no per-line cost: _fill_shipping
            tax = _money(txn, "e:Taxes/e:TotalTaxAmount")
            if tax is None and len(txns) == 1:
                tax = order_tax
            price = round(unit * qty, 2)
            order_rows.append({
                "order_line_item_id": (t("e:OrderLineItemID") or f"{item_id}-{txn_id}").strip(),
                "order_id": o("e:OrderID"),
                "item_id": item_id,
                "transaction_id": txn_id or None,
                "title": t("e:Item/e:Title"),
                "seller": o("e:SellerUserID"),
                "purchased_at": t("e:CreatedDate") or o("e:CreatedTime"),
                "quantity": qty,
                "price": price,
                "shipping": shipping,
                "tax": tax,
                "total": 0.0,
                "currency": currency,
                "purchase_type": "unknown",
                "cancelled": cancelled,
                "refunded": 0.0,
                "shipping_estimated": estimated,
                "_handling": handling,
            })
        order_id = o("e:OrderID")
        _fill_shipping(order_rows, order_shipping, order_id, warnings)
        for r in order_rows:
            if r["shipping"] is not None or r["_handling"]:
                r["shipping"] = round((r["shipping"] or 0.0) + r["_handling"], 2)
            del r["_handling"]
            r["total"] = round(r["price"] + (r["shipping"] or 0.0) + (r["tax"] or 0.0), 2)
        refund, refund_cur = _order_refund(order, order_id, warnings)
        if refund and order_rows:
            if any(r["currency"] != refund_cur for r in order_rows):
                warnings.append(f"order {order_id}: refund currency {refund_cur} differs from "
                                "the line currency; not netted")
            else:
                # eBay reports refunds per order; spread by line total, never past it.
                for r, share in zip(order_rows, _allocate(refund, [r["total"] for r in order_rows])):
                    r["refunded"] = round(min(share, r["total"]), 2)
        order_total = _money(order, "e:Total")
        line_sum = round(sum(r["total"] for r in order_rows), 2)
        if order_rows and order_total is not None and abs(line_sum - order_total) > 0.01:
            warnings.append(
                f"order {o('e:OrderID')}: line totals {line_sum:.2f} != order total "
                f"{order_total:.2f} (shipping or tax not itemized per line)")
        rows.extend(order_rows)
    return rows, warnings


def fetch_purchases(token, now=None, days=DEFAULT_DAYS):
    now = now or dt.datetime.now(dt.timezone.utc)
    rows, warnings = [], []
    for page in range(1, MAX_PAGES + 1):
        root, has_more = _fetch_page(token, page, now, days)
        r, w = parse_purchases(root)
        rows.extend(r)
        warnings.extend(w)
        if not has_more:
            return rows, warnings
    raise SubmitError(f"GetOrders still had more pages after {MAX_PAGES}; refusing a partial window")


def server_base():
    """COMICS_SERVER_URL (canonical) or the deprecated GIXEN_SERVER_URL."""
    return (os.environ.get("COMICS_SERVER_URL") or os.environ.get("GIXEN_SERVER_URL") or "").rstrip("/")


def post_purchases(base, rows):
    inserted = updated = 0
    for i in range(0, len(rows), POST_CHUNK):
        resp = requests.post(f"{base}/api/comics/purchases",
                             json={"purchases": rows[i:i + POST_CHUNK]}, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json()
        inserted += body["inserted"]
        updated += body["updated"]
    return {"inserted": inserted, "updated": updated}


def render(rows, warnings, dry_run):
    lines = [f"{'DRY RUN: ' if dry_run else ''}{len(rows)} purchase line item(s)"]
    for r in sorted(rows, key=lambda r: r["purchased_at"] or ""):
        flag = " CANCELLED" if r["cancelled"] else ""
        if r["refunded"]:
            flag += f" REFUNDED ${r['refunded']:.2f}"
        if r["shipping_estimated"]:
            flag += " SHIPPING-ESTIMATED" if r["shipping"] is not None else " SHIPPING-MISSING"
        lines.append(
            f"  {(r['purchased_at'] or '')[:10]}  {r['item_id']}  ${r['total']:>8.2f}  "
            f"{r['seller'] or '?':<20.20} {(r['title'] or '')[:60]}{flag}")
    by_month = {}
    for r in rows:
        if not r["cancelled"]:
            m = (r["purchased_at"] or "")[:7]
            by_month[m] = round(by_month.get(m, 0.0) + r["total"] - r["refunded"], 2)
    for m in sorted(by_month):
        lines.append(f"  spend {m}: ${by_month[m]:.2f}")
    lines.extend(f"  warning: {w}" for w in warnings)
    return "\n".join(lines)


def _fail(args, base, detail):
    """Exit 1; an --apply run also tells the watchdog (a dry run never pings)."""
    if args.apply:
        ping_heartbeat(base, False, detail, path=HEARTBEAT_PATH)
    return EXIT_FAILURE


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ebay-purchases-pull", description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"GetOrders window in days (1-{MAX_DAYS}, default {DEFAULT_DAYS})")
    ap.add_argument("--apply", action="store_true", help="POST to the comics server (default: dry run)")
    ap.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = ap.parse_args(argv)
    if not 1 <= args.days <= MAX_DAYS:
        print(f"error: --days must be 1-{MAX_DAYS}", file=sys.stderr)
        return EXIT_USAGE
    base = server_base()
    if args.apply and not base:
        print("error: COMICS_SERVER_URL is not set; --apply has nowhere to write", file=sys.stderr)
        return EXIT_USAGE
    try:
        token = eut.get_user_access_token()
        rows, warnings = fetch_purchases(token, days=args.days)
    except eut.UserTokenError as exc:
        print(f"error: eBay user token: {exc}", file=sys.stderr)
        return _fail(args, base, f"eBay user token: {exc}")
    except SubmitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _fail(args, base, str(exc))
    if args.json:
        print(json.dumps({"dry_run": not args.apply, "purchases": rows, "warnings": warnings}, indent=2))
    else:
        print(render(rows, warnings, dry_run=not args.apply))
    if not args.apply:
        return EXIT_OK
    try:
        result = post_purchases(base, rows)
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"error: posting purchases to {base} failed: {exc}", file=sys.stderr)
        return _fail(args, base, f"posting purchases failed: {exc}")
    print(f"recorded: {result['inserted']} new, {result['updated']} refreshed", file=sys.stderr)
    ping_heartbeat(base, True, f"{result['inserted']} new, {result['updated']} refreshed",
                   path=HEARTBEAT_PATH)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
