"""ezship-bound: report non-eBay orders shipped to the EZShip warehouse (BUI-1376).

Read-only. It searches personal Gmail for order confirmations addressed to the
EZShip Portland warehouse, links each order to its shipping email, and proposes
the `ezship new` fields. It never submits anything and never writes the ledger.

Plan: docs/plans/2026-11-11-001-feat-ezship-bound-report-plan.md

Exit codes match `ebay-shipped`: 0 ok, 1 Gmail or ledger failure, 2 a Gmail
search was truncated, 3 some orders could not be parsed.
"""
from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from shipped_orders import (
    _EXIT_HARD_FAILURE,
    _EXIT_OK,
    _EXIT_TRUNCATED,
    _EXIT_UNPARSED,
    DEFAULT_LEDGER,
    ShippedOrdersError,
    _plain_text,
    decode_body,
    extract_shipments,
    fetch_messages,
    headers_of,
    is_shipping_mail,
    load_ledger,
    parse_merchant,
)
from shipped_submit import normalize_carrier, product_name

DEFAULT_DAYS = 45
DEFAULT_ORDER_DAYS = 180
DEFAULT_MAX_RESULTS = 500
CATEGORIES_PATH = Path(__file__).with_name("ezship_categories.json")

# EZShip's own mail comes from noreply@ezbuy.com. It never printed the warehouse
# address in the 180 days measured, but excluding it keeps a future template
# from reading as an order. `-from:me` drops Hsu Ken's own replies, which quote
# the merchant's confirmation.
_EXCLUDE = "-from:me -from:ebay.com -from:members.ebay.com -from:ezbuy.com -from:ezbuy.sg"

# "Portland, OR" alone is not enough: Image Comics and adidas newsletters print
# a Portland office address. The street number followed by the street name is.
_WAREHOUSE = re.compile(r"12826\s+(?:NE|N\.E\.|Northeast)\s+Airport\s+Way", re.I)

# Starts at the word Order, so "#2" in an item name never reads as an order
# number. "Order Confirmation #" is Barnes & Noble's subject shape.
_ORDER_NUMBER = re.compile(r"\bOrder(?:\s+Confirmation)?\s*#\s*([A-Z0-9][A-Z0-9-]*)", re.I)

_EMAIL_ADDRESS = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_PRICE = re.compile(r"^\$([0-9][0-9,]*\.[0-9]{2})(?:\s+([A-Z]{3}))?$")
_ITEM = re.compile(r"^(.+?)\s+×\s+(\d+)\b")

# Whole-word keyword rules on item names, checked against the saved category
# list by a test. One category per order: a match on two gives no proposal.
CATEGORY_RULES = [
    ("1063", re.compile(
        r"\b(?:comics?|cvr|covers?|variant|foil|batman|spider-man|books?)\b"
        r"|\b1:\d+\s+ratio\b", re.I)),
    ("875", re.compile(r"\bsocks?\b", re.I)),
    ("1013", re.compile(r"\bcables?\b", re.I)),
]

LEDGER_NOTE = "ledger covers CLI submissions only; check the EZShip website before submitting"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_categories(path: Path = CATEGORIES_PATH) -> dict:
    """Map subcategory id to {name, declared_min_cents}."""
    data = json.loads(path.read_text())
    out = {}
    for parent in data.get("categories") or []:
        for sub in parent.get("subCategories") or []:
            out[str(sub["id"])] = {
                "name": sub["name"],
                "declared_min_cents": int(sub.get("declaredMin") or 0),
            }
    return out


def plain_part(payload: dict):
    """The first text/plain part, or None. Never the joined parts, because
    the HTML part repeats every item and would double the order."""
    stack = [payload or {}]
    while stack:
        part = stack.pop(0)
        data = (part.get("body") or {}).get("data")
        if part.get("mimeType") == "text/plain" and data:
            try:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
            except (ValueError, TypeError):
                return None
        stack[0:0] = part.get("parts") or []
    return None


def sender_address(from_header: str):
    match = _EMAIL_ADDRESS.search(from_header or "")
    if match:
        return match.group(1).lower()
    bare = (from_header or "").strip()
    return bare.lower() if "@" in bare and " " not in bare else None


def order_number(subject: str, text: str):
    for source in (subject or "", text or ""):
        match = _ORDER_NUMBER.search(source)
        if match:
            return match.group(1).upper()
    return None


def mentions_order(number: str, subject: str, text: str) -> bool:
    """`Order #<number>` as a whole token. Gmail search drops the `#`, so a
    bare `1702` can match a price or another order."""
    pattern = re.compile(
        r"\bOrder\s*#\s*" + re.escape(number) + r"(?![A-Z0-9-])", re.I)
    return bool(pattern.search(subject or "") or pattern.search(text or ""))


def message_time(message: dict):
    try:
        return datetime.fromtimestamp(int(message.get("internalDate")) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def declared_cents(total: Decimal, minimum_cents: int = 0) -> int:
    """50% of the order total in integer cents, rounded half up, raised to
    the category's declared minimum."""
    half = (total * Decimal("0.5")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(int(half * 100), minimum_cents)


def propose_category(item_names, categories: dict):
    """Return (id, name) when every match points at one category, else None."""
    hits = set()
    for name in item_names:
        for cat_id, pattern in CATEGORY_RULES:
            if pattern.search(name):
                hits.add(cat_id)
    if len(hits) != 1:
        return None
    cat_id = hits.pop()
    return cat_id, categories[cat_id]["name"]


# ─── Shopify order template ───────────────────────────────────────────────────

class Unparsed(Exception):
    pass


def _section(lines, start_heading, stop):
    """Lines after `start_heading` (and its dashed underline) up to `stop`."""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == start_heading)
    except StopIteration:
        return None
    out = []
    for ln in lines[start + 1:]:
        if stop(ln.strip()):
            break
        out.append(ln.strip())
    return [ln for ln in out if ln and not set(ln) <= {"-"}]


def parse_shopify_order(text: str) -> dict:
    """Read items and the total from a Shopify order-confirmation text part.

    Raises Unparsed with a reason rather than guessing a field.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    summary = _section(lines, "Order summary", lambda s: s == "Subtotal")
    if summary is None:
        raise Unparsed("no Shopify 'Order summary' section")

    items, block = [], []
    for ln in summary:
        if ln.startswith("("):  # a link on its own line
            continue
        price = _PRICE.match(ln)
        if price is None:
            block.append(ln)
            continue
        joined = " ".join(block)
        block = []
        item = _ITEM.match(joined)
        if item:
            items.append({
                "name": re.sub(r"\s+", " ", item.group(1)).strip(),
                "quantity": int(item.group(2)),
                "price": price.group(1).replace(",", ""),
            })
    if not items:
        raise Unparsed("no 'Name × qty' item lines in the order summary")

    total = None
    for i, ln in enumerate(lines):
        if ln.strip() == "Total":
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    total = _PRICE.match(nxt.strip())
                    break
            break
    if total is None:
        raise Unparsed("no 'Total' line with an amount")
    currency = total.group(2)
    if currency != "USD":
        raise Unparsed(f"total currency is {currency or 'missing'}, not USD")
    return {"items": items, "total": total.group(1).replace(",", ""), "currency": currency}


# ─── Collection ───────────────────────────────────────────────────────────────

def _query(days: int, extra: str) -> str:
    return f"newer_than:{days}d {extra} {_EXCLUDE}"


def address_query(order_days: int) -> str:
    return _query(order_days, '("12826" OR "Airport Way")')


def link_query(order_days: int, sender: str, number: str) -> str:
    return _query(order_days, f'from:{sender} "{number}"')


def _summarize(message: dict) -> dict:
    hdrs = headers_of(message)
    body = decode_body(message.get("payload") or {})
    return {
        "message": message,
        "id": message.get("id"),
        "subject": hdrs.get("Subject", ""),
        "from": hdrs.get("From", ""),
        "body": body,
        "text": _plain_text(body),
        "sent_at": message_time(message),
    }


def _tracking_of(msg: dict):
    found, _url_rej, _label_rej = extract_shipments(msg["body"], msg["text"])
    return [(carrier, tracking) for carrier, tracking, _v, _s in found]


def collect(order_days: int, days: int, max_results: int, ledger: set, categories: dict,
            fetch=None, now=None) -> dict:
    """Build the report payload. `fetch(query, max_results)` returns a Fetched."""
    fetch = fetch or fetch_messages
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    truncated = False

    fetched = fetch(address_query(order_days), max_results)
    truncated |= fetched.truncated

    orders, shipping, skipped = {}, [], []
    for message in fetched.messages:
        msg = _summarize(message)
        if not _WAREHOUSE.search(msg["text"]):
            continue
        if _tracking_of(msg) or is_shipping_mail(msg["subject"], msg["text"]):
            shipping.append(msg)
            continue
        number = order_number(msg["subject"], msg["text"])
        if number is None:
            skipped.append({"message_id": msg["id"], "from": msg["from"],
                            "subject": msg["subject"], "reason": "no order number"})
            continue
        key = (sender_address(msg["from"]), number)
        row = _order_row(msg, number)
        # A second email for the same order (a reminder, an edit) never
        # replaces a parsed row with an unparsed one.
        if key not in orders or orders[key]["status"] == "unparsed":
            orders[key] = row

    rows, linked_tracking, stale = [], set(), 0
    for (sender, number), row in orders.items():
        if row["status"] == "unparsed":
            rows.append(row)
            continue
        if sender is None:
            row.update(status="unparsed", reason="no sender address to search from")
            rows.append(row)
            continue
        found = fetch(link_query(order_days, sender, number), max_results)
        truncated |= found.truncated
        tracking, latest = {}, None
        for message in found.messages:
            msg = _summarize(message)
            if not mentions_order(number, msg["subject"], msg["text"]):
                continue
            pairs = _tracking_of(msg)
            for carrier, number_ in pairs:
                tracking.setdefault(number_, carrier)
            if pairs and msg["sent_at"] and (latest is None or msg["sent_at"] > latest):
                latest = msg["sent_at"]
        linked_tracking.update(tracking)
        if tracking and latest is not None and latest < cutoff:
            stale += 1  # shipped before the window: treated as handled
            continue
        _propose(row, tracking, ledger, categories)
        rows.append(row)

    for msg in shipping:
        if msg["sent_at"] is not None and msg["sent_at"] < cutoff:
            continue
        pairs = [(c, t) for c, t in _tracking_of(msg) if t not in linked_tracking]
        for carrier, tracking in pairs:
            linked_tracking.add(tracking)
            rows.append({
                "status": "orphan_shipment",
                "merchant": parse_merchant(msg["from"]),
                "order_number": order_number(msg["subject"], msg["text"]),
                "message_id": msg["id"],
                "subject": msg["subject"],
                "tracking": [{"tracking_number": tracking, "carrier": carrier}],
                "reason": "shipping email with no matching order confirmation",
            })

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "days": days,
        "order_days": order_days,
        "truncated": truncated,
        "listed": fetched.listed,
        "shipped_before_window": stale,
        "ledger_note": LEDGER_NOTE,
        "rows": rows,
        "skipped": skipped,
    }


def _order_row(msg: dict, number: str) -> dict:
    row = {
        "status": None,
        "merchant": parse_merchant(msg["from"]),
        "sender": sender_address(msg["from"]),
        "order_number": number,
        "message_id": msg["id"],
        "subject": msg["subject"],
        "ordered_at": msg["sent_at"].date().isoformat() if msg["sent_at"] else None,
    }
    text = plain_part(msg["message"].get("payload") or {})
    if text is None:
        row.update(status="unparsed", reason="no text/plain part")
        return row
    try:
        row.update(parse_shopify_order(text))
    except Unparsed as exc:
        row.update(status="unparsed", reason=str(exc))
    return row


def _propose(row: dict, tracking: dict, ledger: set, categories: dict) -> None:
    row["tracking"] = [{"tracking_number": t, "carrier": c} for t, c in tracking.items()]
    names = [item["name"] for item in row["items"]]
    category = propose_category(names, categories)
    if category:
        row["category"] = {"id": category[0], "name": category[1]}
    minimum = categories[category[0]]["declared_min_cents"] if category else 0
    row["declared_value_cents"] = declared_cents(Decimal(row["total"]), minimum)
    row["product"] = product_name(names)
    row["remark"] = f"{row['merchant'] or row['sender']} {row['order_number']}"

    if not tracking:
        row["status"] = "waiting_for_tracking"
        return
    if len(tracking) > 1:
        row["status"] = "hold"
        row["reason"] = "more than one tracking number; the split by parcel is unknown"
        row.pop("declared_value_cents")
        return
    (number, carrier), = tracking.items()
    if number in ledger:
        row["status"] = "already_submitted"
        return
    ezship_carrier = normalize_carrier(carrier)
    if ezship_carrier is None:
        row["status"] = "unsupported_carrier"
        row["reason"] = f"carrier {carrier} is not an EZShip carrier"
        return
    row["ezship_carrier"] = ezship_carrier
    if not category:
        row["status"] = "needs_category"
        row["reason"] = "item names match no category rule, or more than one"
        return
    row["status"] = "ready"
    row["note"] = LEDGER_NOTE
    row["command"] = shlex.join([
        "ezship", "new", "-t", number, "-c", ezship_carrier,
        "-d", str(row["declared_value_cents"]), "-p", row["product"],
        "--category", category[1], "--category-id", category[0], "-r", row["remark"],
    ])


# ─── Output ───────────────────────────────────────────────────────────────────

def _money(cents):
    return "-" if cents is None else f"${cents / 100:,.2f}"


def render_text(payload: dict) -> str:
    out = [f"EZShip-bound orders (shipments in the last {payload['days']} days, "
           f"orders in the last {payload['order_days']} days)"]
    if payload["truncated"]:
        out.append("WARNING: a Gmail search was truncated; this list is incomplete.")
    if not payload["rows"]:
        out.append("No orders found.")
    for row in payload["rows"]:
        tracking = ", ".join(f"{t['carrier']} {t['tracking_number']}"
                             for t in row.get("tracking") or []) or "-"
        category = row.get("category")
        out += [
            "",
            f"[{row['status']}] {row.get('merchant') or '?'} #{row.get('order_number') or '?'}",
            f"  Tracking:  {tracking}",
        ]
        if row.get("items"):
            out.append(f"  Product:   {row.get('product')}")
            out.append(f"  Total:     ${Decimal(row['total']):,.2f} {row['currency']}")
            out.append(f"  Category:  {category['name']} ({category['id']})" if category
                       else "  Category:  -")
            out.append(f"  Declared:  {_money(row.get('declared_value_cents'))}")
            out.append(f"  Remark:    {row.get('remark')}")
        if row.get("reason"):
            out.append(f"  Reason:    {row['reason']}")
        if row.get("command"):
            out.append(f"  Command:   {row['command']}")
    if payload["skipped"]:
        out += ["", "Skipped (warehouse address, no order number):"]
        out += [f"  - {s['from']} | {s['subject']}" for s in payload["skipped"]]
    out += ["", f"Note: {payload['ledger_note']}."]
    if payload["shipped_before_window"]:
        out.append(f"{payload['shipped_before_window']} order(s) shipped before the "
                   f"{payload['days']}-day window and are not listed.")
    return "\n".join(out)


def _version_string() -> str:
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ezship-bound {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ezship-bound",
        description="Report non-eBay orders shipped to the EZShip warehouse. Read-only.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"shipment window in days (default {DEFAULT_DAYS})")
    parser.add_argument("--order-days", type=int, default=DEFAULT_ORDER_DAYS,
                        help=f"order-confirmation window in days (default {DEFAULT_ORDER_DAYS})")
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--json", action="store_true", help="print the full payload as JSON")
    parser.add_argument("--version", action="version", version=_version_string())
    args = parser.parse_args(argv)
    order_days = max(args.order_days, args.days)

    try:
        ledger = load_ledger(args.ledger)
        payload = collect(order_days, args.days, args.max_results, ledger, load_categories())
    except ShippedOrdersError as exc:
        print(f"ezship-bound: {exc}", file=sys.stderr)
        return _EXIT_HARD_FAILURE

    print(json.dumps(payload, indent=2) if args.json else render_text(payload))
    if payload["truncated"]:
        return _EXIT_TRUNCATED
    if any(row["status"] == "unparsed" for row in payload["rows"]):
        return _EXIT_UNPARSED
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
