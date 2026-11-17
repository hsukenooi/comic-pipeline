"""ezship-bound: report non-eBay orders shipped to the EZShip warehouse (BUI-1376).

`--submit` (BUI-1425) also submits the ready ones; see the Submit section.
Without it the command is read-only. It finds order-confirmation emails whose shipping address is the
EZShip warehouse, links each order to its shipping email, and proposes the
fields for `ezship new`. It never writes Gmail, the ledger, or EZShip.

An order counts as already submitted when its tracking number is in the CLI
ledger or in the EZShip account's own order list (`ezship orders --json`,
BUI-1378), so an order entered on the EZShip website is caught too. A failed
EZShip lookup is a hard failure: it never reports an order as ready on the
ledger alone.

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
import os
import shlex
import subprocess
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
import shipped_submit as ss
from shipped_submit import (
    DEFAULT_EZSHIP_CMD,
    DEFAULT_EZSHIP_DIR,
    EZSHIP_CMD_ENV,
    EZSHIP_DIR_ENV,
    normalize_carrier,
    product_name,
)

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

# Curated keyword rules: whole words only, so "notebook" stays out of Books.
# (category id, category name, pattern). They run alongside the names in the
# full category list (BUI-1425); Books needs its own rule because comic titles
# rarely say "book".
CATEGORY_RULES = [
    ("1063", "Books",
     re.compile(r"\b(?:comics?|cvr|batman|books?)\b|\b\d+:\d+\s+ratio\b", re.I)),
    ("875", "Socks", re.compile(r"\bsocks?\b", re.I)),
    ("1013", "Cables", re.compile(r"\bcables?\b", re.I)),
]

# Supplies, apparel, and merchandise that carry a Books keyword ("Comic bags
# & boards", a Batman T-shirt, a Batman mug). A name that hits one gets no
# Books match, so it is held unless another category claims it (BUI-1425).
_NOT_BOOKS = re.compile(
    r"\b(?:bags?|boards?|boxes|box|sleeves?|shirts?|t-shirts?|tees?|hoodies?|"
    r"figures?|figurines?|funko|statues?|busts?|mugs?|cups?|posters?|prints?|"
    r"pins?|stickers?|masks?|jackets?|frames?|bins?|replicas?|plush|toys?|"
    r"vinyl|hats?|caps?|socks?|keychains?|magnets?|cards?)\b", re.I)

# A title that reads as a comic or a book ("Mickey Mouse #1", "Saga Vol. 2",
# "Umbrella Academy TPB"). Such an item may only be Books: any other category
# hit is a title word, not the product, and the order is held (BUI-1425).
_BOOK_SIGNAL = re.compile(
    r"#\s*\d|\bvol(?:ume)?\.?\s*\d|(?<![\w-])(?:tpb?|hc|hardcover|paperback|"
    r"omnibus|graphic\s+novel|trade\s+paperback|variant|cvr|cover\s+[a-z])(?![\w-])",
    re.I)

_BOOKS_ID = "1063"

# Words a store uses that are not in EZShip's category names (BUI-1425).
# phrase -> subcategory id. Each id is checked against the saved list by a test.
CATEGORY_ALIASES = {
    "t-shirt": "869", "tshirt": "869", "tee": "869",   # Tops & Tee
    "notebook": "1060",                                  # Note Book
    "plush": "1094",                                     # soft toys
    "lego": "1096",                                      # Building Blocks & lego
    "cd": "1221",                                        # compact disc
    "keychain": "1207", "key ring": "1207",              # Key chain
    "enamel pin": "1055",                                # Badge
    "tote bag": "1075",                                  # Handbag / Totebag
    "phone case": "1017",                                # Phone Casing
    "exercise book": "1066",                             # EZShip spells it Excercise
    "computer mouse": "1178", "wireless mouse": "1178",  # Mouse
}

# Category-name words too generic to match on their own: each turns up in
# titles of something else ("printing plate", "Lord of the Rings", "Mickey
# Mouse", "The Umbrella Academy", "Black Mirror", DC's Monitor) or comes from
# splitting a compound name ("Storage Container / Box"). An alias can still map
# to them. An item that matches nothing is held, which is the safe side.
_NAME_STOPWORDS = {
    "light", "glass", "ring", "nut", "zip", "rope", "pipe", "socket", "tape",
    "pen", "lock", "ball", "tissue", "fabric", "badge", "staple", "basket",
    "plate", "hand", "box", "house", "machine", "chop", "glue", "folder",
    "rack", "watch", "tea", "monitor", "tablet", "bed", "apparel", "balm",
    "mouse", "umbrella", "mirror", "uniform", "coat", "shirt", "belt", "lamp",
    "tops", "pant", "chair", "battery", "camera", "speaker", "pendant",
    "necklace", "helmet", "lens", "bicycle", "tent", "mask", "game console",
}

_WORD_EDGE_L, _WORD_EDGE_R = r"(?<![a-z0-9])", r"(?![a-z0-9])"


def _phrase_pattern(phrase: str):
    """Whole-word, case-blind pattern. A singular name also matches its plural.
    Only a multi-word plural name matches its singular ("Board Games" ->
    "board game"): a one-word one would turn "Tops" into "top" (Top Cow)."""
    words = phrase.split()
    last = words[-1]
    if last.endswith("s") and len(last) > 3:
        forms = {last, last[:-1]} if len(words) > 1 else {last}
    else:
        forms = {last, last + "s", last + "es"}
    tail = "(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + ")"
    head = "".join(re.escape(w) + r"[\s-]+" for w in words[:-1])
    return re.compile(_WORD_EDGE_L + head + tail + _WORD_EDGE_R, re.I)


def build_name_index(categories: dict):
    """[(pattern, {ids})] from every subcategory name plus CATEGORY_ALIASES.

    A compound name ("Hat / Cap", "Tops & Tee") also matches each part, unless
    that part is another subcategory's whole name ("Adapters & Cables" does not
    claim "cables" from Cables). Names shared by two subcategories ("Hand Soap")
    map to both, which the caller holds as ambiguous.
    """
    def norm(text):
        return re.sub(r"\s+", " ", text.strip().lower())

    full, parts = {}, {}
    for cid, info in categories.items():
        if cid == _BOOKS_ID:
            continue  # Books matches through its curated rule and guard only.
        name = norm(info["name"])
        full.setdefault(name, set()).add(cid)
        pieces = [norm(x) for x in re.split(r"/|&|,|\band\b", name) if x.strip()]
        if len(pieces) > 1:
            for piece in pieces:
                parts.setdefault(piece, set()).add(cid)
    phrases = dict(full)
    for piece, cids in parts.items():
        if piece not in full:
            phrases.setdefault(piece, set()).update(cids)
    phrases = {k: v for k, v in phrases.items() if k not in _NAME_STOPWORDS}
    for alias, cid in CATEGORY_ALIASES.items():
        phrases.setdefault(norm(alias), set()).add(cid)
    return [(_phrase_pattern(k), v) for k, v in phrases.items()]


def item_category_ids(name: str, index) -> set:
    """Category ids one item name matches. A match inside a longer match is
    dropped, so "Bath Towel" is not also "Towel" and "Exercise Book" not Books."""
    spans = []
    for cid, _cname, pat in CATEGORY_RULES:
        if cid == _BOOKS_ID and _NOT_BOOKS.search(name):
            continue
        spans += [(m.start(), m.end(), cid) for m in pat.finditer(name)]
    for pat, cids in index:
        spans += [(m.start(), m.end(), cid) for m in pat.finditer(name) for cid in cids]
    kept = [s for s in spans if not any(
        o[0] <= s[0] and s[1] <= o[1] and o[1] - o[0] > s[1] - s[0] for o in spans)]
    return {cid for _a, _b, cid in kept}


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

# A cancelled EZShip order never shipped onward, so its parcel still needs
# submitting.
_EZSHIP_NOT_SUBMITTED = {"Cancelled_Code"}
_EZSHIP_TIMEOUT_S = 180


class EzshipSessionExpired(ShippedOrdersError):
    """The ezship CLI exited 3: the cookie needs a human to refresh it."""


def _ezship_launch():
    # Same launcher as ebay-shipped-submit: the ezship CLI is not installed on
    # PATH, it runs from its source tree (EZSHIP_CMD / EZSHIP_DIR override).
    launcher = shlex.split(os.environ.get(EZSHIP_CMD_ENV, "")) or list(DEFAULT_EZSHIP_CMD)
    return launcher, os.environ.get(EZSHIP_DIR_ENV) or str(DEFAULT_EZSHIP_DIR)


def _run_ezship_read(args, run=subprocess.run):
    """Run one read-only ezship command and return its stdout."""
    launcher, cwd = _ezship_launch()
    cmd = launcher + args
    try:
        done = run(cmd, cwd=cwd, capture_output=True, text=True,
                   timeout=_EZSHIP_TIMEOUT_S)
    except OSError as exc:
        raise ShippedOrdersError(
            f"could not launch the ezship CLI ({' '.join(cmd)} in {cwd}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ShippedOrdersError(
            f"`ezship {args[0]}` timed out after {_EZSHIP_TIMEOUT_S}s") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        err = EzshipSessionExpired if done.returncode == 3 else ShippedOrdersError
        raise err(f"`ezship {args[0]}` failed (exit {done.returncode}): {detail}")
    return done.stdout


def load_live_categories(run=subprocess.run) -> dict:
    """EZShip's category list right now (`ezship categories --json`, BUI-1425).

    The submit path uses this rather than the saved copy, so a changed minimum
    reaches EZShip as EZShip states it.
    """
    out = _run_ezship_read(["categories", "--json"], run)
    try:
        cats = parse_categories(json.loads(out))
    except (ValueError, KeyError, TypeError) as exc:
        raise ShippedOrdersError("`ezship categories --json` printed no category list") from exc
    if not cats:
        raise ShippedOrdersError("`ezship categories --json` printed an empty list")
    return cats


def load_ezship_trackings(run=subprocess.run) -> set:
    """Tracking numbers (upper-case) of every live order on the EZShip account."""
    out = _run_ezship_read(["orders", "--json"], run)
    try:
        orders = json.loads(out)["orders"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ShippedOrdersError("`ezship orders --json` printed no order list") from exc
    return {str(o.get("trackingNo") or "").strip().upper() for o in orders
            if o.get("trackingNo") and o.get("status") not in _EZSHIP_NOT_SUBMITTED}


# ─── Category list ────────────────────────────────────────────────────────────

def load_categories(path: Path = CATEGORIES_PATH) -> dict:
    """The saved category list. Map subcategory id -> {"name", "declared_min"}."""
    return parse_categories(json.loads(path.read_text()))


def parse_categories(data: dict) -> dict:
    """Map subcategory id -> {"name", "declared_min"} (declaredMin in cents)."""
    out = {}
    for top in data["categories"]:
        for sub in top.get("subCategories") or []:
            out[str(sub["id"])] = {
                "name": sub["name"], "declared_min": int(sub["declaredMin"])}
    return out


def pick_category(item_names, categories=None):
    """Return (id, name, None) for one category, or (None, None, reason).

    EZShip takes one category per order, so every item must match and all must
    agree. Anything else gets no proposal: an order is held, never guessed.
    """
    categories = categories if categories is not None else load_categories()
    index = build_name_index(categories)
    found = set()
    for name in item_names:
        hits = item_category_ids(name, index)
        if not hits:
            return None, None, f"no category matches {name!r}"
        if hits - {_BOOKS_ID} and _BOOK_SIGNAL.search(name):
            other = ", ".join(sorted(categories.get(i, {}).get("name", i)
                                     for i in hits - {_BOOKS_ID}))
            return None, None, (f"{name!r} reads as a comic or book title but "
                                f"matched {other}")
        found |= hits
    if len(found) != 1:
        names = ", ".join(sorted(categories.get(i, {}).get("name", i) for i in found))
        return None, None, f"items match several categories ({names})"
    cid = next(iter(found))
    if cid not in categories:
        return None, None, f"category {cid} is not in EZShip's list"
    return cid, categories[cid]["name"], None


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
    cid, cname, why = pick_category([i["name"] for i in order["items"]], categories)
    if cid is None:
        return NO_CATEGORY, None, why
    declared_min = categories[cid]["declared_min"]
    if declared_min > order["total_cents"]:
        # A minimum above what was paid means the match is almost surely
        # wrong (a $30 order is not a $500 tablet). Hold it.
        return NO_CATEGORY, None, (
            f"{cname} minimum {_money(declared_min)} is above the order total "
            f"{_money(order['total_cents'])}")
    cents = declared_value_cents(order["total_cents"], declared_min)
    fields = {
        "tracking": tracking["tracking_number"],
        "carrier": carrier,
        "product": product_name(names),
        "category": cname,
        "category_id": cid,
        "declared_min_cents": declared_min,
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
        "--declared-min", str(declared_min),
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
                 categories=None, now=None, ezship=frozenset()):
    categories = categories if categories is not None else load_categories()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    orders, shipping, unparsed, skipped = classify_messages(messages, cutoff)
    truncated = False
    rows, linked_ids = [], set()

    def submitted_via(number):
        number = number.upper()
        return [name for name, known in (("ledger", ledger), ("ezship", ezship))
                if number in known]

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
        # Every order is checked against the ledger and EZShip, however old
        # its shipping mail: the EZShip list replaces the old --days cut-off.
        vias = [submitted_via(t["tracking_number"]) for t in trackings]
        if all(vias):
            rows.append({**row, "status": ALREADY,
                         "tracking": trackings[0]["tracking_number"],
                         "submitted_via": sorted({v for via in vias for v in via})})
            continue
        if len(trackings) > 1:
            rows.append({**row, "status": HOLD,
                         "reason": "several tracking numbers; cannot tell which "
                                   "parcel holds which items"})
            continue
        tracking = trackings[0]
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
            "skipped_not_warehouse": skipped, "truncated": truncated}


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
        if row.get("submitted_via"):
            out.append(f"    found in: {', '.join(row['submitted_via'])}")
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
    if report["truncated"]:
        out.append("WARNING: a Gmail search was truncated; this report is incomplete.")
    if "ezship_orders" in report:
        out.append(f"Submitted check: the CLI ledger plus {report['ezship_orders']} "
                   "tracking numbers on EZShip.")
    return "\n".join(out)


# ─── Submit (BUI-1425) ────────────────────────────────────────────────────────
#
# `--submit` sends each ready_to_submit row to `ezship new` with its proposed
# fields. It shares ebay-shipped --auto-submit's run lock and in-flight
# quarantine file (both are keyed by tracking number), so the two jobs can
# never submit one parcel twice or race each other. Rules:
#
# * Only ready_to_submit rows submit. Every row that needs a person (hold,
#   no_category, unsupported_carrier, shipped_no_tracking, orphan_shipment,
#   unparsed) is notified once and never submitted.
# * The category list is read live from EZShip, so the minimum sent is the one
#   EZShip states today.
# * An in-flight marker is written before each `ezship new` and cleared only on
#   a confirmed ledger entry, or on an outcome that provably sent nothing (exit
#   3, or ezship failing to launch). Anything else quarantines the tracking
#   number until a person checks EZShip and runs `ebay-shipped --clear-inflight`.
# * Exit 3 (expired cookie), and any other failure, stops the batch.
# * The heartbeat pings only on a run with no failure. A held order is a
#   correct outcome and still pings.

SUBMIT_EXIT_OK = 0
SUBMIT_EXIT_FAILURE = 1
SUBMIT_EXIT_TRUNCATED = 2
SUBMIT_EXIT_SESSION_EXPIRED = 3
SUBMIT_EXIT_SOME_REJECTED = 4

DEFAULT_ATTENTION_STATE = Path.home() / ".config" / "ezship" / "ezship-bound-attention.json"
ATTENTION_STATUSES = (HOLD, NO_CATEGORY, UNSUPPORTED, SHIPPED_NO_TRACKING)
_SUBMIT_TIMEOUT_S = 120


def submit_command(fields: dict) -> list:
    launcher, _cwd = _ezship_launch()
    return launcher + [
        "new", "-t", fields["tracking"], "-c", fields["carrier"],
        "-d", str(fields["declared_value_cents"]), "-p", fields["product"],
        "--category", fields["category"], "--category-id", fields["category_id"],
        "--declared-min", str(fields["declared_min_cents"]), "-r", fields["remark"],
    ]


# Not an int, so a child killed by a signal (a negative returncode) can never
# be mistaken for "never launched" and have its in-flight marker cleared.
LAUNCH_FAILED = "launch-failed"


def run_submit_command(fields: dict):
    """Run one `ezship new`. Returns (exit code, last output line); the code is
    None on a timeout and LAUNCH_FAILED when ezship could not start."""
    _launcher, cwd = _ezship_launch()
    try:
        proc = subprocess.run(submit_command(fields), cwd=cwd, capture_output=True,
                              text=True, timeout=_SUBMIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as exc:
        return LAUNCH_FAILED, f"could not launch ezship: {exc}"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
    return proc.returncode, tail[0]


def attention_items(report) -> list:
    """(key, one-line description) for every row a person must look at."""
    items = []
    for row in report["orders"]:
        if row["status"] in ATTENTION_STATUSES:
            items.append((f"{row['status']}:{row['merchant']}#{row['order_number']}",
                          f"{row['merchant']} #{row['order_number']} {row['status']}: "
                          f"{row.get('reason', '')}"))
    for row in report["orphans"]:
        items.append((f"{ORPHAN}:{','.join(row['trackings'])}",
                      f"{row['merchant']} {ORPHAN}: {', '.join(row['trackings'])}"))
    for row in report["unparsed"]:
        items.append((f"{UNPARSED}:{row['message_id']}",
                      f"{row['merchant']} {UNPARSED}: {row['reason']}"))
    return items


def ping_submit_heartbeat(server_url, ok, detail):
    """Best-effort POST to /api/heartbeat/ezship-bound-submit (or /failure)."""
    ss.ping_heartbeat(server_url, ok, detail, path="/api/heartbeat/ezship-bound-submit")


def _submit_fail(message, server_url, code=SUBMIT_EXIT_FAILURE):
    print(f"Error: {message}", file=sys.stderr)
    ping_submit_heartbeat(server_url, False, message)
    return code


def run_submit(args, server_url, inflight_path=ss.DEFAULT_INFLIGHT_STATE,
               attention_path=DEFAULT_ATTENTION_STATE):
    try:
        lock = ss.acquire_run_lock(inflight_path)
    except BlockingIOError:
        print("an EZShip auto-submit run holds the lock; nothing done", file=sys.stderr)
        return SUBMIT_EXIT_OK
    except OSError as exc:
        return _submit_fail(f"cannot take run lock: {exc}", server_url)
    try:
        return _run_submit_locked(args, server_url, inflight_path, attention_path)
    except Exception as exc:  # noqa: BLE001  # any crash must still alert, never fail green
        return _submit_fail(f"unexpected {type(exc).__name__}: {exc}", server_url)
    finally:
        lock.close()


def _run_submit_locked(args, server_url, inflight_path, attention_path):
    window = max(args.days, args.order_days)
    try:
        ledger = load_ledger(args.ledger)
        inflight = ss.load_inflight(inflight_path)
        ezship = load_ezship_trackings()
        categories = load_live_categories()
        fetched = fetch_messages(_search_query(window), args.max_results)
        report = build_report(fetched.messages, ledger, args.days, args.order_days,
                              categories=categories, ezship=ezship)
    except EzshipSessionExpired as exc:
        ss.notify("ezship-bound: EZShip session expired. Run: ezship set-cookie.")
        return _submit_fail(str(exc), server_url, SUBMIT_EXIT_SESSION_EXPIRED)
    except (ShippedOrdersError, ss.InflightError) as exc:
        return _submit_fail(str(exc), server_url)
    report["truncated"] = report["truncated"] or fetched.truncated
    report["ezship_orders"] = len(ezship)

    # A quarantined tracking number is held until a person clears it.
    for row in report["orders"]:
        mark = inflight.get(str(row.get("tracking", "")).upper())
        if mark and row["status"] == READY:
            row["status"] = HOLD
            row.pop("proposal", None)
            row["reason"] = (f"QUARANTINED since {mark.get('since', '?')} "
                             f"({mark.get('state', '?')}): check EZShip, then run: "
                             f"{ss.clear_command(row['tracking'])}")

    print(render_table(report))
    if report["truncated"]:
        return _submit_fail("a Gmail search was truncated; nothing submitted",
                            server_url, SUBMIT_EXIT_TRUNCATED)

    items = attention_items(report)
    seen = ss.load_holds_state(attention_path)
    new = [(k, text) for k, text in items if k not in seen]
    if new:
        ss.notify(("ezship-bound needs you: " + "; ".join(t for _k, t in new))[:200])
    # Keep only keys still present, so a row that clears and returns alerts again.
    ss.save_holds_state(attention_path, {k for k, _t in items})

    done = {"submitted": 0}
    try:
        code = _submit_ready(report, args, server_url, inflight, inflight_path, done)
    finally:
        # The wrapper reads this line on every exit to raise the Telegram hold
        # alert, so a failed submit never swallows a new hold.
        print(f"ezship-bound: submitted={done['submitted']} attention={len(items)} "
              f"new_attention={len(new)}")
    if code == SUBMIT_EXIT_OK:
        ping_submit_heartbeat(server_url, True,
                              f"submitted={done['submitted']} attention={len(items)}")
    return code


def _submit_ready(report, args, server_url, inflight, inflight_path, done):
    """Submit every READY row. Any failure stops the batch: the rows after it
    stay READY and unmarked for the next run, instead of each being tried
    against an EZShip that is failing for every order."""
    for row in report["orders"]:
        if row["status"] != READY:
            continue
        fields = row["proposal"]
        tracking = fields["tracking"]
        key = tracking.upper()
        inflight[key] = {"since": ss._now_iso(), "state": "in-flight"}
        try:
            ss.save_inflight(inflight_path, inflight)  # BEFORE the call: crash-safe
        except ss.InflightError as exc:
            return _submit_fail(f"cannot record in-flight marker, not submitting "
                                f"{tracking}: {exc}", server_url)
        code, detail = run_submit_command(fields)
        if code in (SUBMIT_EXIT_SESSION_EXPIRED, LAUNCH_FAILED):
            # Treated as nothing sent: exit 3 is EZShip refusing the session,
            # and LAUNCH_FAILED means ezship never started.
            inflight.pop(key, None)
            ss._save_or_warn(inflight_path, inflight)
        if code == SUBMIT_EXIT_SESSION_EXPIRED:
            ss.notify("ezship-bound: EZShip session expired. Run: ezship set-cookie. "
                      "Batch stopped.")
            return _submit_fail(f"EZShip session expired (exit 3); stopped before "
                                f"{tracking}", server_url, SUBMIT_EXIT_SESSION_EXPIRED)
        if code == LAUNCH_FAILED:
            ss.notify(f"ezship-bound: EZShip could not be launched: {detail}"[:200])
            return _submit_fail(f"ezship launch failure: {detail}", server_url)
        try:
            landed = key in load_ledger(args.ledger)
        except ShippedOrdersError as exc:
            return _submit_fail(f"ledger unreadable after submitting {tracking} "
                                f"(left quarantined: {ss.clear_command(tracking)}): "
                                f"{exc}", server_url)
        if code == 0 and landed:
            inflight.pop(key, None)
            ss._save_or_warn(inflight_path, inflight)
            done["submitted"] += 1
            print(f"submitted {tracking} {row['merchant']} "
                  f"#{row['order_number']} as {fields['category']} "
                  f"({fields['category_id']}) declared {fields['declared_value']}")
            continue
        unconfirmed = code == 0 or code is None or landed
        state = "unconfirmed" if unconfirmed else "failed"
        inflight[key] = {"since": inflight[key]["since"], "state": f"{state} exit={code}"}
        ss._save_or_warn(inflight_path, inflight)
        ss.notify(f"ezship-bound: {tracking} {state} (exit {code}) and quarantined; "
                  f"batch stopped. Check EZShip, then: {ss.clear_command(tracking)}"[:250])
        if unconfirmed:
            # Exit 0 with no ledger entry, a timeout, or a failure that still
            # wrote the ledger: the outcome is unknown.
            return _submit_fail(f"unconfirmed submission of {tracking} (exit={code}, "
                                f"ledger={'yes' if landed else 'no'}): {detail}", server_url)
        return _submit_fail(f"EZShip rejected {tracking} (exit {code}); quarantined: "
                            f"{detail}", server_url, SUBMIT_EXIT_SOME_REJECTED)
    return SUBMIT_EXIT_OK


def _search_query(window: int) -> str:
    return (f'newer_than:{window}d ("12826" OR "Airport Way") '
            "-from:ebay.com -from:members.ebay.com -from:ezbuy.sg")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ezship-bound",
        description=("Report non-eBay orders shipped to the EZShip warehouse and "
                     "propose the `ezship new` fields. Read-only unless --submit: "
                     "it never writes Gmail."),
        epilog=("Exit codes (report): 0 ok; 1 Gmail or ledger failure; 2 truncated; "
                "3 some unparsed. With --submit: 0 ok (holds included); 1 failure; "
                "2 truncated; 3 EZShip cookie expired; 4 some orders rejected."),
    )
    parser.add_argument("--version", action="version",
                        version=_version_string().replace("ebay-shipped", "ezship-bound"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--days", type=int, default=45, metavar="N",
                        help="Window in days for unmatched shipping mail and "
                             "unreadable mail (default: 45). Orders are checked "
                             "against EZShip however old.")
    parser.add_argument("--order-days", type=int, default=180, metavar="N",
                        help="Order-confirmation window in days (default: 180); "
                             "presales ship months after the order.")
    parser.add_argument("--max-results", type=int, default=400, metavar="N")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, metavar="PATH")
    parser.add_argument("--submit", action="store_true",
                        help="Submit every ready_to_submit order to EZShip and notify "
                             "on rows that need a person (BUI-1425).")
    args = parser.parse_args(argv)
    if min(args.days, args.order_days, args.max_results) < 1:
        parser.error("--days, --order-days, and --max-results must be at least 1")
    if args.submit:
        if args.json_output:
            parser.error("--submit prints a table; --json is for the report")
        server_url = (os.environ.get("COMICS_SERVER_URL")
                      or os.environ.get("GIXEN_SERVER_URL") or "").rstrip("/")
        return run_submit(args, server_url)

    window = max(args.days, args.order_days)
    query = _search_query(window)
    try:
        ledger = load_ledger(args.ledger)
        ezship = load_ezship_trackings()
        fetched = fetch_messages(query, args.max_results)
        report = build_report(fetched.messages, ledger, args.days, args.order_days,
                              ezship=ezship)
    except ShippedOrdersError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_HARD_FAILURE
    report["truncated"] = report["truncated"] or fetched.truncated
    report["query"] = query
    report["ezship_orders"] = len(ezship)

    if args.json_output:
        print(json.dumps({"counts": _counts(report), **report}, indent=2))
    else:
        print(render_table(report))
    if report["truncated"]:
        return _EXIT_TRUNCATED
    attention = report["unparsed"] or any(
        r["status"] == SHIPPED_NO_TRACKING for r in report["orders"])
    return _EXIT_UNPARSED if attention else _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
