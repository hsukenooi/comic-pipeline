"""ezship-bound: report non-eBay orders shipped to the EZShip warehouse (BUI-1376).

Read-only. It finds order-confirmation emails whose shipping address is the
EZShip warehouse, links each order to its shipping email, and proposes the
fields for `ezship new`. It never writes Gmail, the ledger, or EZShip.

Exit codes (same as `ebay-shipped`): 0 ok, 1 Gmail or ledger failure,
2 truncated window, 3 some orders unparsed.

The parser reads the Shopify order template from the `text/plain` part only
(`decode_body` joins every MIME part, so reading all of them would count each
item twice). Any other template is reported `unparsed`; it never guesses.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from shipped_orders import (
    DEFAULT_LEDGER,
    ShippedOrdersError,
    _message_date,
    _plain_text,
    _version_string,
    decode_body,
    extract_shipments,
    fetch_messages,
    headers_of,
    is_shipping_mail,
    load_ledger,
    parse_merchant,
)
from shipped_submit import normalize_carrier, product_name

_EXIT_OK = 0
_EXIT_HARD_FAILURE = 1
_EXIT_TRUNCATED = 2
_EXIT_UNPARSED = 3

CATEGORIES_PATH = Path(__file__).with_name("ezship_categories.json")

# `Order #X`: the word Order first, so `#US-3108` matches and a `#2` in an
# item name does not.
_ORDER_NO = re.compile(
    r"\bOrder\s*(?:#|No\.?|Number)\s*:?\s*#?\s*([A-Z0-9-]*\d[A-Z0-9-]*)", re.I)

# The warehouse. "Portland, OR" alone is not enough: newsletters print a
# Portland office address.
_WAREHOUSE = re.compile(
    r"12826\s*(?:NE|N\.E\.|North\s*east)\s+Airport\s+Way", re.I)

_DEFAULT_SEARCH_MAX = 25
_REPLY_SUBJECT = re.compile(r"\s*(?:re|fwd?)\s*:", re.I)

# Keyword rules: whole words only, so "notebook" stays out of Books.
# (category id, category name, pattern).
CATEGORY_RULES = [
    ("1063", "Books",
     re.compile(r"\b(?:comics?|cvr|batman|books?)\b|\b\d+:\d+\s+ratio\b", re.I)),
    ("875", "Socks", re.compile(r"\bsocks?\b", re.I)),
    ("1013", "Cables", re.compile(r"\bcables?\b", re.I)),
]

# Supplies and apparel that carry a Books keyword ("Comic bags & boards",
# a Batman T-shirt). A name that hits one gets no Books match.
_NOT_BOOKS = re.compile(
    r"\b(?:bags?|boards?|boxes|box|sleeves?|shirts?|t-shirts?|tees?|hoodies?)\b", re.I)

# Row statuses.
READY = "ready_to_submit"
WAITING = "waiting_for_tracking"
SHIPPED_NO_TRACKING = "shipped_no_tracking"
HOLD = "hold"
UNSUPPORTED = "unsupported_carrier"
NO_CATEGORY = "no_category"
ALREADY = "already_submitted"
ORPHAN = "orphan_shipment"
UNPARSED = "unparsed"

_LEDGER_NOTE = ("The ledger covers CLI submissions only. An order submitted on "
                "the EZShip website still reads as ready.")


# ─── Category list ────────────────────────────────────────────────────────────

def load_categories(path: Path = CATEGORIES_PATH) -> dict:
    """Map subcategory id -> {"name", "declared_min"} (declaredMin in cents)."""
    data = json.loads(path.read_text())
    out = {}
    for top in data.get("categories", []):
        for sub in top.get("subCategories") or []:
            out[str(sub["id"])] = {
                "name": sub["name"], "declared_min": int(sub["declaredMin"])}
    return out


def pick_category(item_names):
    """Return (id, name, None) for one category, or (None, None, reason).

    EZShip takes one category per order, so every item must match and all must
    agree. Anything else gets no proposal.
    """
    found = set()
    for name in item_names:
        hits = {(cid, cname) for cid, cname, pat in CATEGORY_RULES
                if pat.search(name) and not (cid == "1063" and _NOT_BOOKS.search(name))}
        if not hits:
            return None, None, f"no category rule matches {name!r}"
        found |= hits
    if len(found) != 1:
        names = ", ".join(sorted(n for _i, n in found))
        return None, None, f"items match several categories ({names})"
    cid, cname = next(iter(found))
    return cid, cname, None


def declared_value_cents(total_cents: int, declared_min: int) -> int:
    """50% of the order total, half up, raised to the category minimum."""
    half = (Decimal(total_cents) / 2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return max(int(half), declared_min)


def _money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


# ─── Parsing ──────────────────────────────────────────────────────────────────

def plain_text_part(payload: dict):
    """The decoded `text/plain` parts only, or None when there is none."""
    import base64
    chunks: list[str] = []

    def walk(part):
        if (part.get("mimeType") or "").lower() == "text/plain":
            data = (part.get("body") or {}).get("data")
            if data:
                try:
                    chunks.append(base64.urlsafe_b64decode(data).decode("utf-8", "replace"))
                except (ValueError, TypeError):
                    pass
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    if not chunks:
        return None
    return "\n".join(chunks).replace("\r\n", "\n")


def sender_address(from_header: str):
    match = re.search(r"<([^<>\s]+@[^<>\s]+)>", from_header or "")
    if match:
        return match.group(1)
    bare = re.search(r"[^\s<>\"]+@[^\s<>\"]+", from_header or "")
    return bare.group(0) if bare else None


_PRICE_LINE = re.compile(r"^\$([\d,]+\.\d{2})(?:\s+[A-Z]{3})?$")
_ITEM_LINE = re.compile(r"^(.*?)\s*×\s*(\d+)$")
_TOTAL_AMOUNT = re.compile(r"^\$([\d,]+)\.(\d{2})(?:\s+([A-Z]{3}))?$")


def _parse_items(lines):
    items, buf, state = [], [], "name"
    for line in lines:
        if state == "name":
            match = _ITEM_LINE.match(line)
            if match:
                buf.append(match.group(1))
                name = re.sub(r"\s+", " ", " ".join(p for p in buf if p)).strip()
                items.append({"name": name, "qty": int(match.group(2))})
                buf, state = [], "detail"
            else:
                buf.append(line)
        elif _PRICE_LINE.match(line):
            state = "name"
        # else: a variant line under the item; not part of its name.
    return items


def parse_order(message: dict):
    """Parse one order-confirmation message. Returns a dict with `ok`.

    ok=False carries a `reason`. The parser reads the Shopify template only.
    """
    hdrs = headers_of(message)
    subject = hdrs.get("Subject", "")
    text = plain_text_part(message.get("payload") or {})
    if text is None:
        return {"ok": False, "reason": "no text/plain part"}
    match = _ORDER_NO.search(subject) or _ORDER_NO.search(text)
    if not match:
        return {"ok": False, "reason": "no order number"}
    order_number = match.group(1)

    lines = [ln.strip() for ln in text.split("\n")]
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.lower() == "order summary")
    except StopIteration:
        return {"ok": False, "order_number": order_number,
                "reason": "not the Shopify order template (no 'Order summary')"}
    body = [ln for ln in lines[start + 1:]]
    # Skip the dashed underline of the heading.
    body = [ln for ln in body if not re.fullmatch(r"-{3,}", ln)]
    try:
        end = next(i for i, ln in enumerate(body) if ln == "Subtotal")
    except StopIteration:
        return {"ok": False, "order_number": order_number,
                "reason": "no Subtotal line after 'Order summary'"}
    items = _parse_items([ln for ln in body[:end] if ln])
    if not items:
        return {"ok": False, "order_number": order_number,
                "reason": "no 'Name × qty' item lines"}

    rest = [ln for ln in body[end:] if ln]
    total = None
    for i, ln in enumerate(rest):
        if ln == "Total":
            if i + 1 < len(rest):
                total = _TOTAL_AMOUNT.match(rest[i + 1])
            break
    if not total:
        return {"ok": False, "order_number": order_number,
                "reason": "no readable 'Total' amount"}
    currency = total.group(3)
    if currency != "USD":
        return {"ok": False, "order_number": order_number,
                "reason": f"total currency is {currency or 'missing'}, not USD"}
    total_cents = int(total.group(1).replace(",", "")) * 100 + int(total.group(2))
    return {"ok": True, "order_number": order_number, "items": items,
            "total_cents": total_cents}


# ─── Proposal ─────────────────────────────────────────────────────────────────

def propose(order: dict, tracking: dict, categories: dict):
    """Return (status, fields, reason) for one order with exactly one tracking."""
    carrier = normalize_carrier(tracking["carrier"])
    if carrier is None:
        return UNSUPPORTED, None, f"carrier {tracking['carrier']!r} is not one EZShip takes"
    names = [i["name"] if i["qty"] == 1 else f"{i['name']} x{i['qty']}"
             for i in order["items"]]
    cid, cname, why = pick_category([i["name"] for i in order["items"]])
    if cid is None:
        return NO_CATEGORY, None, why
    cents = declared_value_cents(order["total_cents"], categories[cid]["declared_min"])
    fields = {
        "tracking": tracking["tracking_number"],
        "carrier": carrier,
        "product": product_name(names),
        "category": cname,
        "category_id": cid,
        "declared_value_cents": cents,
        "declared_value": _money(cents),
        "remark": f"{order['merchant']} {order['order_number']}",
    }
    fields["suggested_command"] = " ".join([
        "ezship new",
        "-t", shlex.quote(fields["tracking"]),
        "-c", shlex.quote(carrier),
        "-d", str(cents),
        "-p", shlex.quote(fields["product"]),
        "--category", shlex.quote(cname),
        "--category-id", cid,
        "-r", shlex.quote(fields["remark"]),
    ])
    return READY, fields, None


# ─── Collection ───────────────────────────────────────────────────────────────

def _in_window(sent_at, cutoff):
    if not sent_at:
        return True
    try:
        return datetime.fromisoformat(sent_at) >= cutoff
    except ValueError:
        return True


def _order_token(order_number: str):
    return re.compile(
        r"Order\s*#\s*" + re.escape(order_number) + r"(?![A-Za-z0-9-])", re.I)


def classify_messages(messages, cutoff=None):
    """Split warehouse mail into orders, shipping mail, unparsed, skipped."""
    orders, shipping, unparsed, skipped = {}, [], [], 0
    for message in messages:
        hdrs = headers_of(message)
        subject = hdrs.get("Subject", "")
        from_header = hdrs.get("From", "")
        # Replies, forwards, and mail we sent are not merchant mail.
        if _REPLY_SUBJECT.match(subject) or "SENT" in (message.get("labelIds") or []):
            skipped += 1
            continue
        text = plain_text_part(message.get("payload") or {})
        if text is None:
            text = _plain_text(decode_body(message.get("payload") or {}))
        if not _WAREHOUSE.search(text):
            skipped += 1
            continue
        sent_at = _message_date(message, hdrs)
        if is_shipping_mail(subject, text):
            shipping.append(message)
            continue
        merchant = parse_merchant(from_header) or from_header
        if re.search(r"ezbuy|ezship", from_header, re.I):
            skipped += 1
            continue
        if not (_ORDER_NO.search(subject) or _ORDER_NO.search(text)):
            # Mail with no order number is judged on the shipment window only.
            if cutoff is not None and not _in_window(sent_at, cutoff):
                skipped += 1
                continue
            unparsed.append({
                "status": UNPARSED, "message_id": message.get("id"),
                "merchant": merchant, "subject": subject, "order_number": None,
                "reason": "mail to the warehouse address with no order number"})
            continue
        address = sender_address(from_header)
        parsed = parse_order(message)
        if not parsed["ok"]:
            # Mail that cannot be read is only worth attention inside the
            # shipment window; older mail is stale either way.
            if cutoff is not None and not _in_window(sent_at, cutoff):
                skipped += 1
                continue
            unparsed.append({
                "status": UNPARSED, "message_id": message.get("id"),
                "merchant": merchant, "subject": subject,
                "order_number": parsed.get("order_number"),
                "reason": parsed["reason"]})
            continue
        key = (address, parsed["order_number"])
        if key in orders and (orders[key]["sent_at"] or "") >= (sent_at or ""):
            continue
        orders[key] = {**parsed, "merchant": merchant, "sender": address,
                       "message_id": message.get("id"), "sent_at": sent_at,
                       "subject": subject}
    return list(orders.values()), shipping, unparsed, skipped


def find_trackings(order: dict, order_days: int, max_results: int):
    """Search Gmail for the order's shipping mail. Returns (trackings, ids, truncated).

    The sender must be the full address: every Shopify store shares
    `t.shopifyemail.com`. A match counts only if its body holds
    `Order #<number>` as a whole token, since Gmail drops the `#`.
    """
    if not order["sender"]:
        raise ShippedOrdersError(
            f"order {order['order_number']} has no sender address to search")
    query = (f'newer_than:{order_days}d from:{order["sender"]} '
             f'"{order["order_number"]}"')
    fetched = fetch_messages(query, max_results)
    token = _order_token(order["order_number"])
    trackings, ids, untracked = {}, set(), []
    for message in fetched.messages:
        if message.get("id") == order["message_id"]:
            continue
        if (_REPLY_SUBJECT.match(headers_of(message).get("Subject", ""))
                or "SENT" in (message.get("labelIds") or [])):
            continue
        body = decode_body(message.get("payload") or {})
        text = _plain_text(body)
        if not token.search(text) and not token.search(body):
            continue
        ids.add(message.get("id"))
        found, _ur, _lr = extract_shipments(body, text)
        sent_at = _message_date(message, headers_of(message))
        if not found and is_shipping_mail(headers_of(message).get("Subject", ""), text):
            untracked.append(headers_of(message).get("Subject", ""))
        for carrier, number, _verified, _source in found:
            trackings.setdefault(number, {
                "tracking_number": number, "carrier": carrier,
                "sent_at": sent_at})
    return list(trackings.values()), ids, fetched.truncated, untracked


def build_report(messages, ledger, days, order_days, search_max=_DEFAULT_SEARCH_MAX,
                 categories=None, now=None):
    categories = categories if categories is not None else load_categories()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    orders, shipping, unparsed, skipped = classify_messages(messages, cutoff)
    truncated = False
    rows, linked_ids, outside_window = [], set(), []

    for order in sorted(orders, key=lambda o: o["sent_at"] or "", reverse=True):
        trackings, ids, trunc, untracked = find_trackings(order, order_days, search_max)
        truncated = truncated or trunc
        linked_ids |= ids
        row = {"merchant": order["merchant"], "order_number": order["order_number"],
               "subject": order["subject"], "total": _money(order["total_cents"]),
               "items": [i["name"] for i in order["items"]],
               "trackings": [t["tracking_number"] for t in trackings],
               "message_id": order["message_id"]}
        if not trackings and untracked:
            rows.append({**row, "status": SHIPPED_NO_TRACKING,
                         "reason": "shipping mail found but no readable tracking "
                                   f"number ({untracked[0]!r})"})
            continue
        if not trackings:
            rows.append({**row, "status": WAITING})
            continue
        if not any(_in_window(t["sent_at"], cutoff) for t in trackings):
            # Named, never just counted: the ledger can't tell an order
            # submitted on the EZShip website from one nobody submitted.
            dates = [t["sent_at"] for t in trackings if t["sent_at"]]
            outside_window.append({
                "merchant": order["merchant"], "order_number": order["order_number"],
                "last_email": max(dates)[:10] if dates else None,
                "trackings": [t["tracking_number"] for t in trackings],
                "in_ledger": [t["tracking_number"] for t in trackings
                              if t["tracking_number"].upper() in ledger]})
            continue
        if len(trackings) > 1:
            rows.append({**row, "status": HOLD,
                         "reason": "several tracking numbers; cannot tell which "
                                   "parcel holds which items"})
            continue
        tracking = trackings[0]
        if tracking["tracking_number"].upper() in ledger:
            rows.append({**row, "status": ALREADY,
                         "tracking": tracking["tracking_number"]})
            continue
        status, fields, reason = propose(order, tracking, categories)
        rows.append({**row, "status": status,
                     **({"proposal": fields} if fields else {}),
                     "tracking": tracking["tracking_number"],
                     **({"reason": reason} if reason else {})})

    # One parcel can carry several orders. Submitting it once per order would
    # create duplicate shipments, so every order sharing a number is held.
    owners = {}
    for row in rows:
        for number in row.get("trackings", []):
            owners.setdefault(number, []).append(row)
    for number, sharing in owners.items():
        if len(sharing) > 1:
            for row in sharing:
                row["status"] = HOLD
                row.pop("proposal", None)
                row["reason"] = (f"tracking {number} is shared by orders "
                                 + ", ".join(f"#{r['order_number']}" for r in sharing))

    orphans = []
    for message in shipping:
        if message.get("id") in linked_ids:
            continue
        hdrs = headers_of(message)
        sent_at = _message_date(message, hdrs)
        if not _in_window(sent_at, cutoff):
            continue
        body = decode_body(message.get("payload") or {})
        found, _ur, _lr = extract_shipments(body, _plain_text(body))
        merchant = parse_merchant(hdrs.get("From", "")) or hdrs.get("From", "")
        if found:
            orphans.append({
                "status": ORPHAN, "merchant": merchant,
                "subject": hdrs.get("Subject", ""), "message_id": message.get("id"),
                "trackings": [t for _c, t, _v, _s in found],
                "carriers": [c for c, _t, _v, _s in found]})
        else:
            unparsed.append({
                "status": UNPARSED, "message_id": message.get("id"),
                "merchant": merchant, "subject": hdrs.get("Subject", ""),
                "order_number": None,
                "reason": "shipping mail to the warehouse with no readable tracking number"})
    return {"orders": rows, "orphans": orphans, "unparsed": unparsed,
            "skipped_not_warehouse": skipped, "outside_window": outside_window,
            "truncated": truncated}


# ─── Output ───────────────────────────────────────────────────────────────────

def _counts(report):
    counts = {}
    for row in report["orders"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    counts[ORPHAN] = len(report["orphans"])
    counts[UNPARSED] = len(report["unparsed"])
    return counts


def render_table(report) -> str:
    out = []
    for row in report["orders"]:
        head = f"{row['status'].upper():<22} {row['merchant']} #{row['order_number']}  {row['total']}"
        out.append(head)
        out.append(f"    items: {'; '.join(row['items'])}")
        if row.get("trackings"):
            out.append(f"    tracking: {', '.join(row['trackings'])}")
        if row.get("reason"):
            out.append(f"    reason: {row['reason']}")
        prop = row.get("proposal")
        if prop:
            out.append(f"    carrier: {prop['carrier']}  category: {prop['category']} "
                       f"({prop['category_id']})  declared: {prop['declared_value']}")
            out.append(f"    product: {prop['product']}")
            out.append(f"    remark: {prop['remark']}")
            out.append(f"    {prop['suggested_command']}")
        out.append("")
    for row in report["orphans"]:
        out.append(f"{ORPHAN.upper():<22} {row['merchant']}  {row['subject']}")
        out.append(f"    tracking: {', '.join(row['trackings'])}  "
                   f"(no matching order confirmation)")
        out.append("")
    for row in report["unparsed"]:
        num = f" #{row['order_number']}" if row.get("order_number") else ""
        out.append(f"{UNPARSED.upper():<22} {row['merchant']}{num}  {row['subject']}")
        out.append(f"    reason: {row['reason']}")
        out.append("")
    counts = ", ".join(f"{k}={v}" for k, v in _counts(report).items() if v) or "nothing found"
    out.append(f"Summary: {counts}")
    if report["outside_window"]:
        out.append("Shipped before the --days window (confirm each one is on the "
                   "EZShip website):")
        for row in report["outside_window"]:
            trackings = ", ".join(
                t + (" (in CLI ledger)" if t in row["in_ledger"] else "")
                for t in row["trackings"])
            out.append(f"    {row['merchant']} #{row['order_number']}  "
                       f"last email {row['last_email'] or '?'}  {trackings}")
    if report["truncated"]:
        out.append("WARNING: a Gmail search was truncated; this report is incomplete.")
    out.append(_LEDGER_NOTE)
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ezship-bound",
        description=("Report non-eBay orders shipped to the EZShip warehouse and "
                     "propose the `ezship new` fields. Read-only: never writes Gmail, "
                     "the ledger, or EZShip."),
        epilog="Exit codes: 0 ok; 1 Gmail or ledger failure; 2 truncated; 3 some unparsed.",
    )
    parser.add_argument("--version", action="version",
                        version=_version_string().replace("ebay-shipped", "ezship-bound"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--days", type=int, default=45, metavar="N",
                        help="Shipment window in days (default: 45).")
    parser.add_argument("--order-days", type=int, default=180, metavar="N",
                        help="Order-confirmation window in days (default: 180); "
                             "presales ship months after the order.")
    parser.add_argument("--max-results", type=int, default=400, metavar="N")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, metavar="PATH")
    args = parser.parse_args(argv)
    if min(args.days, args.order_days, args.max_results) < 1:
        parser.error("--days, --order-days, and --max-results must be at least 1")

    window = max(args.days, args.order_days)
    query = (f'newer_than:{window}d ("12826" OR "Airport Way") '
             "-from:ebay.com -from:members.ebay.com -from:ezbuy.sg")
    try:
        ledger = load_ledger(args.ledger)
        fetched = fetch_messages(query, args.max_results)
        report = build_report(fetched.messages, ledger, args.days, args.order_days)
    except ShippedOrdersError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_HARD_FAILURE
    report["truncated"] = report["truncated"] or fetched.truncated
    report["query"] = query

    if args.json_output:
        print(json.dumps({"counts": _counts(report), **report,
                          "ledger_note": _LEDGER_NOTE}, indent=2))
    else:
        print(render_table(report))
    if report["truncated"]:
        return _EXIT_TRUNCATED
    attention = report["unparsed"] or any(
        r["status"] == SHIPPED_NO_TRACKING for r in report["orders"])
    return _EXIT_UNPARSED if attention else _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
