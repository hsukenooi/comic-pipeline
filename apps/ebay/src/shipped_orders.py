#!/usr/bin/env python3
"""ebay-shipped: list shipped eBay orders whose tracking number is not yet in
the EZShip ledger (BUI-807).

`/comic:ezship-add` is hand-fed a tracking number, carrier, and seller on every
run. This module is the source that removes the typing: it reads eBay shipping
notifications out of Gmail (read-only, via the `gws` CLI), pulls the tracking
number straight out of the carrier's own tracking URL, and subtracts the
numbers already recorded in `~/.config/ezship/submitted-orders.json`.

It only reads. It never sends, labels, archives, or otherwise mutates Gmail,
and it never writes the ledger — submitting to EZShip is a separate follow-up.

Where the tracking number actually lives
────────────────────────────────────────
eBay sends two different shipping mails, and only one of them contains a
tracking number at all. Measured over a 180-day window of this account's mail
(60 messages), then again over 365 days:

  * `eBay - <seller> <…@members.ebay.com>` — the seller's own notification,
    sent through their shipping platform. **15/15 carried a carrier tracking
    URL** with the number in a query parameter, e.g.
    `wwwapps.ups.com/WebTracking/track?track=yes&trackNums=1Z…`.
  * `eBay <ebay@ebay.com>` — eBay's own "Your package is now with its
    carrier!". **0/45 carried a tracking number anywhere.** The only link is
    an authenticated `ebay.com/vod/FetchOrderDetails?itemId=…` click-through;
    the body names the carrier and the seller but never the number.

So the number is recoverable, but only from the seller-sent shape. The generic
shape is surfaced as an *unparsed* row rather than dropped, because it is
positive evidence that a package shipped whose number this tool cannot see.

Precision was checked against ground truth: every UPS number this parser
extracts from mail (5 distinct) appears in the EZShip ledger the user typed by
hand, exactly — 5/5 match, no extras, no misses. Coverage is a different
number: 5 of the ledger's 17 entries are recoverable this way; the other 12
(USPS/FedEx) arrived only in the generic shape and were never in any email.

Never guess a tracking number
─────────────────────────────
A wrong number is worse than a missing one — it would ship a real package to
the wrong consolidation record. Two guards, in order:

  1. The number is taken only from a tracking-parameter on a *carrier's own
     domain*. It is never scraped out of free text, where item ids, order
     numbers, and marketing ids are all long digit runs (a naive digit regex
     on a real message yields four item ids and a campaign id before it finds
     anything shippable).
  2. The extracted number must then pass that carrier's format/check-digit
     rule. Anything that fails degrades to `unparsed` with a reason — it is
     never emitted as a row and never silently dropped.

Guard 1 is the load-bearing one: the number is copied verbatim out of a URL, so
there is no transcription step for a slip to enter through. Guard 2 catches a
mangled or truncated extraction — a weighted mod-10 sum catches every adjacent
transposition and ~88% of single-character substitutions, not 100%, and the
tests pin that measured figure rather than a flattering claim.

UPS 1Z and USPS impb check digits are both verified against this account's real
ledger (5/5 and 11/11 respectively). FedEx and DHL are validated on shape only
and have not been seen in this account's mail — they are wired up but
unverified, which the `--json` output states per row via `carrier_verified`.
"""

from __future__ import annotations

import argparse
import base64
import html as html_mod
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# Exit codes (mirroring seller_scan's convention of distinct, documented codes)
_EXIT_OK = 0
_EXIT_HARD_FAILURE = 1  # Gmail or the ledger could not be read — never "nothing new"
_EXIT_UNPARSED = 3      # ran fine, but some shipping mail had no readable number

# Why a shipping mail yielded no number. The two classes get very different
# treatment (a count vs. an itemised warning), so they are named rather than
# re-derived from the human-readable reason string.
UNPARSED_NO_TRACKING_URL = "no_tracking_url"
UNPARSED_VALIDATION_FAILED = "validation_failed"

DEFAULT_LEDGER = Path.home() / ".config" / "ezship" / "submitted-orders.json"
DEFAULT_GWS_CONFIG_DIR = Path.home() / ".config" / "gws-personal"

# eBay shipping mail lands in the personal Gmail account; the default `gws`
# profile is a different (work) account and returns nothing for these queries.
GWS_CONFIG_DIR_ENV = "EBAY_SHIPPED_GWS_CONFIG_DIR"

DEFAULT_QUERY = (
    "from:ebay.com (shipped OR package OR tracking OR delivered OR carrier)"
)


def _version_string() -> str:
    """BUI-314: staleness signal for a `uv tool install`ed binary."""
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-shipped {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


# ─── Tracking-number validation ───────────────────────────────────────────────

def ups_check_digit_ok(tracking: str) -> bool:
    """UPS 1Z check digit: 1Z + 16 chars, the last being the check digit.

    Letters map to (ord - 63) % 10; positions alternate weight 1 and 2 across
    the 15 characters between the `1Z` prefix and the check digit.
    Verified against all 5 UPS numbers in this account's real EZShip ledger.
    """
    if not re.fullmatch(r"1Z[0-9A-Z]{16}", tracking):
        return False
    body, check = tracking[2:-1], tracking[-1]
    if not check.isdigit():
        return False
    total = 0
    for i, ch in enumerate(body):
        value = int(ch) if ch.isdigit() else (ord(ch) - 63) % 10
        total += value if i % 2 == 0 else value * 2
    return (10 - total % 10) % 10 == int(check)


def usps_check_digit_ok(tracking: str) -> bool:
    """USPS impb check digit: weights 3 and 1 alternating from the right.

    Accepts the 20/22/26-digit domestic forms. Verified against all 11
    USPS-shaped numbers in this account's real EZShip ledger.
    """
    if not re.fullmatch(r"\d{20}|\d{22}|\d{26}", tracking):
        return False
    total = 0
    for i, ch in enumerate(reversed(tracking[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == int(tracking[-1])


def fedex_shape_ok(tracking: str) -> bool:
    """FedEx: 12, 15, 20, or 22 digits. Shape only — no sample to verify a
    check digit against, so this deliberately does not claim one."""
    return bool(re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", tracking))


def dhl_shape_ok(tracking: str) -> bool:
    """DHL: 10-11 digits, or the S10 form (2 letters, 9 digits, 2 letters)."""
    return bool(re.fullmatch(r"\d{10,11}|[A-Z]{2}\d{9}[A-Z]{2}", tracking))


# (carrier, verified-against-ground-truth, validator, url pattern)
# The pattern must anchor on the carrier's own host and read a
# tracking-named query parameter — never a bare digit run in body text.
_CARRIER_RULES = [
    ("UPS", True, ups_check_digit_ok,
     re.compile(r"\bups\.com/[^\s\"'<>]*?[?&]tracknums?=([0-9A-Za-z]+)", re.I)),
    ("USPS", True, usps_check_digit_ok,
     re.compile(r"\busps\.com/[^\s\"'<>]*?[?&](?:qtc_)?t?labels1?=([0-9A-Za-z]+)", re.I)),
    ("FEDEX", False, fedex_shape_ok,
     re.compile(r"\bfedex\.com/[^\s\"'<>]*?[?&](?:trackingnumber|tracknumbers)=([0-9A-Za-z]+)",
                re.I)),
    ("DHL", False, dhl_shape_ok,
     re.compile(r"\bdhl\.[a-z.]+/[^\s\"'<>]*?[?&](?:tracking-id|awb)=([0-9A-Za-z]+)", re.I)),
]


def extract_tracking(body: str):
    """Return (found, rejected) from a decoded message body.

    `found` is a list of (carrier, tracking, carrier_verified) that passed
    validation; `rejected` is a list of (carrier, raw) that appeared in a
    carrier tracking URL but failed that carrier's rule — surfaced so a
    mangled number becomes a visible warning instead of a silent miss.
    """
    text = html_mod.unescape(body)
    found, rejected, seen = [], [], set()
    for carrier, verified, validator, pattern in _CARRIER_RULES:
        for raw in pattern.findall(text):
            candidate = raw.strip().upper()
            if (carrier, candidate) in seen:
                continue
            seen.add((carrier, candidate))
            if validator(candidate):
                found.append((carrier, candidate, verified))
            else:
                rejected.append((carrier, candidate))
    return found, rejected


# ─── Message parsing ──────────────────────────────────────────────────────────

def decode_body(payload: dict) -> str:
    """Concatenate every base64url-decoded part of a Gmail message payload."""
    chunks: list[str] = []

    def walk(part):
        data = (part.get("body") or {}).get("data")
        if data:
            try:
                chunks.append(base64.urlsafe_b64decode(data).decode("utf-8", "replace"))
            except (ValueError, TypeError):
                pass
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return "\n".join(chunks)


def headers_of(message: dict) -> dict:
    return {
        h.get("name", ""): h.get("value", "")
        for h in ((message.get("payload") or {}).get("headers") or [])
    }


_SELLER_FROM = re.compile(r"^\s*eBay\s*-\s*(.+?)\s*<", re.I)


def parse_seller(from_header: str):
    """Seller login from the From display name (`eBay - timemachinecomics <…>`).

    Only the `members.ebay.com` shape names the seller this way; eBay's own
    mail is just `eBay <ebay@ebay.com>`, so this returns None there.
    """
    match = _SELLER_FROM.match(from_header or "")
    return match.group(1).strip() if match else None


def _plain_text(body: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    # Zero-width joiners and combining marks are eBay's inbox-preview padding;
    # drop those outright, but turn nbsp into a real space so words stay apart.
    text = re.sub("[\u034f\u200b-\u200d\u2060\ufeff]", "", text)
    return re.sub(r"[^\S\n]+", " ", text)


_ORDER_NO = re.compile(r"Order\s*(?:No\.?|number)\s*:?\s*([0-9]{2}-[0-9]{5}-[0-9]{5})", re.I)
_SHIPPED_VIA = re.compile(r"Shipped\s*Via\s*([^\n<]{1,40})", re.I)

# Subjects that assert a package physically moved. Kept deliberately narrow:
# "You got it!" (an auction win) and saved-search blasts must not qualify.
_SHIPPING_SUBJECT = re.compile(
    r"package|shipped|out for delivery|delivered|with its carrier|"
    r"order is being prepared|estimated to arrive|an update on your order",
    re.I,
)


def is_shipping_mail(subject: str, body_text: str) -> bool:
    if _SHIPPING_SUBJECT.search(subject or ""):
        return True
    return bool(re.search(r"Shipped\s*Via|Track My Package|Track package", body_text or "", re.I))


def parse_message(message: dict) -> dict:
    """Turn one Gmail message into a classified record.

    kind is one of:
      "rows"     — one or more validated tracking numbers, ready to submit
      "unparsed" — shipping mail whose number could not be read confidently
      "ignore"   — not shipping mail at all

    A "rows" record carries a `tracking` list, not a single number: the unit
    here is the *shipment*, not the message. A split order (common on a
    14-item lot) puts two carrier URLs in one mail, and collapsing that to a
    single number would silently drop the second package — the same class of
    miss this tool exists to eliminate.
    """
    hdrs = headers_of(message)
    subject = hdrs.get("Subject", "")
    from_header = hdrs.get("From", "")
    body = decode_body(message.get("payload") or {})
    text = _plain_text(body)

    if not is_shipping_mail(subject, text):
        return {"kind": "ignore", "message_id": message.get("id"), "subject": subject}

    found, rejected = extract_tracking(body)
    seller = parse_seller(from_header)
    order_match = _ORDER_NO.search(text)
    via_match = _SHIPPED_VIA.search(text)
    order_no = order_match.group(1) if order_match else None
    shipped_via = via_match.group(1).strip() if via_match else None
    sent_at = _message_date(message, hdrs)

    base = {
        "message_id": message.get("id"),
        "subject": subject,
        "seller": seller,
        "order_number": order_no,
        "shipped_via": shipped_via,
        "sent_at": sent_at,
    }

    if not found:
        if rejected:
            unparsed_class = UNPARSED_VALIDATION_FAILED
            reason = (
                "tracking URL present but the number failed "
                + ", ".join(f"{c} validation ({v})" for c, v in rejected)
            )
        else:
            unparsed_class = UNPARSED_NO_TRACKING_URL
            reason = (
                "no carrier tracking URL in the message — eBay's own shipping "
                "mail links to an authenticated order page instead of naming "
                "the number"
            )
        return {**base, "kind": "unparsed",
                "unparsed_class": unparsed_class, "reason": reason}

    return {
        **base,
        "kind": "rows",
        "tracking": [
            {"tracking_number": tracking, "carrier": carrier, "carrier_verified": verified}
            for carrier, tracking, verified in found
        ],
    }


def _message_date(message: dict, hdrs: dict):
    """Message timestamp as a UTC ISO string, or None.

    Always normalized to UTC: `internalDate` is epoch-UTC while the `Date`
    header carries the sender's offset, and these timestamps are later
    compared to pick a shipment's earliest sighting. Comparing ISO strings
    that carry different offsets would order them wrongly.
    """
    raw = message.get("internalDate")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            pass
    try:
        parsed = parsedate_to_datetime(hdrs.get("Date", ""))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# ─── Gmail access (read-only) ─────────────────────────────────────────────────

class ShippedOrdersError(RuntimeError):
    """A hard failure: Gmail unreachable, or the ledger unreadable.

    Both must stop the run rather than degrade to an empty result —
    "no new orders" and "could not check" are different answers, and
    only one of them is safe to act on.
    """


def _gws_env():
    config_dir = os.environ.get(GWS_CONFIG_DIR_ENV) or str(DEFAULT_GWS_CONFIG_DIR)
    return {**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": config_dir}


def _run_gws(method: str, params: dict) -> dict:
    """Invoke `gws gmail users messages <method>` and return parsed JSON.

    Only `list` and `get` are ever passed here — this tool has no reason to
    mutate a mailbox, and routing every call through one read-only helper keeps
    it that way.
    """
    if method not in ("list", "get"):
        raise ShippedOrdersError(f"refusing non-read Gmail method: {method}")
    try:
        proc = subprocess.run(
            ["gws", "gmail", "users", "messages", method, "--params", json.dumps(params)],
            capture_output=True, text=True, env=_gws_env(), timeout=120,
        )
    except FileNotFoundError as exc:
        raise ShippedOrdersError(
            "the `gws` CLI is not on PATH — it is what reads Gmail for this tool"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ShippedOrdersError(f"gws timed out running messages.{method}") from exc

    # gws prints a "Using keyring backend: …" banner on stdout before the JSON.
    start = proc.stdout.find("{")
    if start < 0:
        raise ShippedOrdersError(
            f"gws messages.{method} returned no JSON "
            f"(exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    try:
        return json.loads(proc.stdout[start:])
    except json.JSONDecodeError as exc:
        raise ShippedOrdersError(f"gws messages.{method} returned malformed JSON: {exc}") from exc


def fetch_messages(query: str, max_results: int):
    """Yield full Gmail messages matching `query`, newest first."""
    listing = _run_gws("list", {"userId": "me", "q": query, "maxResults": max_results})
    for stub in listing.get("messages") or []:
        yield _run_gws("get", {"userId": "me", "id": stub["id"], "format": "full"})


# ─── Ledger (read-only) ───────────────────────────────────────────────────────

def load_ledger(path: Path) -> set[str]:
    """Return the tracking numbers already submitted to EZShip.

    The ledger (BUI-180) is a flat object keyed by tracking number. This is
    live user state: it is read here and never written. A missing file means
    nothing has been submitted yet, which is a legitimate empty set — but a
    file that exists and cannot be parsed is an error, because treating it as
    empty would re-offer every order already sent.
    """
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ShippedOrdersError(f"could not read the EZShip ledger at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ShippedOrdersError(f"unexpected EZShip ledger shape at {path}: expected an object")
    return {str(k).strip().upper() for k in data}


# ─── Collection ───────────────────────────────────────────────────────────────

def _merge_shipment(rows: dict, record: dict, shipment: dict) -> None:
    """Fold one (message, shipment) pair into the row for its tracking number."""
    tracking = shipment["tracking_number"]
    sent_at, subject = record["sent_at"], record["subject"]
    existing = rows.get(tracking)

    if existing is None:
        rows[tracking] = {
            **shipment,
            "seller": record["seller"],
            "order_number": record["order_number"],
            "shipped_via": record["shipped_via"],
            "subject": subject,
            "sent_at": sent_at,
            "first_seen_at": sent_at,
            "message_ids": [record["message_id"]],
        }
        return

    if record["message_id"] not in existing["message_ids"]:
        existing["message_ids"].append(record["message_id"])

    # sent_at is None only if both internalDate and the Date header were unusable.
    if sent_at is not None:
        if existing["first_seen_at"] is None or sent_at < existing["first_seen_at"]:
            existing["first_seen_at"] = sent_at
        if existing["sent_at"] is None or sent_at > existing["sent_at"]:
            existing["sent_at"] = sent_at
            existing["subject"] = subject

    # A later mail in the lifecycle often fills a field an earlier one omitted.
    for key in ("seller", "order_number", "shipped_via"):
        if not existing.get(key) and record.get(key):
            existing[key] = record[key]


def collect(messages, submitted: set[str]):
    """Fold messages into deduped new rows, skipped rows, and unparsed warnings.

    Rows are keyed by tracking number, not by message, in both directions:

      * one shipment generates up to four mails (being prepared → estimated to
        arrive → out for delivery → delivered) that collapse into one row,
        keeping the earliest sighting as `first_seen_at` and the latest
        subject as the current status;
      * one mail can announce several shipments (a split order), and each
        becomes its own row rather than being folded into the first.
    """
    rows: dict[str, dict] = {}
    unparsed: list[dict] = []
    skipped: list[dict] = []

    for message in messages:
        record = parse_message(message)
        if record["kind"] == "ignore":
            continue
        if record["kind"] == "unparsed":
            unparsed.append(record)
            continue

        for shipment in record["tracking"]:
            _merge_shipment(rows, record, shipment)

    new_rows = []
    for tracking, row in rows.items():
        if tracking in submitted:
            skipped.append(row)
        else:
            new_rows.append(row)

    new_rows.sort(key=lambda r: (r.get("first_seen_at") or "", r["tracking_number"]))
    unparsed.sort(key=lambda r: (r.get("sent_at") or "", r.get("message_id") or ""))
    return new_rows, skipped, unparsed


# ─── Output ───────────────────────────────────────────────────────────────────

def render_table(new_rows, skipped, unparsed, ledger_path) -> str:
    lines = []
    if not new_rows:
        lines.append("No new shipped orders with a readable tracking number.")
    else:
        lines.append(f"{len(new_rows)} new shipped order(s) not yet in the EZShip ledger:")
        lines.append("")
        header = f"{'TRACKING NUMBER':<24} {'CARRIER':<8} {'SELLER':<22} {'SHIPPED':<12} STATUS"
        lines.append(header)
        lines.append("-" * len(header))
        for row in new_rows:
            date = (row.get("first_seen_at") or "")[:10] or "—"
            carrier = row["carrier"] + ("" if row.get("carrier_verified", True) else "*")
            lines.append(
                f"{row['tracking_number']:<24} {carrier:<8} "
                f"{(row.get('seller') or '—'):<22} {date:<12} {row.get('subject', '')[:40]}"
            )
        if any(not r.get("carrier_verified", True) for r in new_rows):
            lines.append("")
            lines.append("  * carrier pattern not yet verified against a real shipment "
                         "— confirm the number before submitting.")

    # Two very different classes hide in `unparsed`, and flattening them buries
    # the one that matters. "No carrier URL" is the known, structural eBay-own
    # shape — dozens per run, nothing actionable per message — so it collapses
    # to a count. A number that failed validation is rare and alarming, so
    # every one of those is listed. --json always carries the full list.
    structural = [r for r in unparsed if r["unparsed_class"] == UNPARSED_NO_TRACKING_URL]
    suspicious = [r for r in unparsed if r["unparsed_class"] == UNPARSED_VALIDATION_FAILED]

    if suspicious:
        lines.append("")
        lines.append(f"{len(suspicious)} message(s) had a tracking URL whose number "
                     "FAILED validation — not guessed, check by hand:")
        for row in suspicious:
            date = (row.get("sent_at") or "")[:10] or "—"
            lines.append(f"  - {date}  {row.get('seller') or 'eBay'}: "
                         f"{row.get('subject', '')[:52]}")
            lines.append(f"      {row['reason']}")

    if structural:
        dates = sorted((r.get("sent_at") or "")[:10] for r in structural if r.get("sent_at"))
        span = f" ({dates[0]} to {dates[-1]})" if dates else ""
        lines.append("")
        lines.append(
            f"{len(structural)} eBay-sent shipping mail(s){span} carry no tracking "
            "number at all — eBay links to an authenticated order page instead of "
            "naming it. Nothing to extract; use --json for the message list.")

    if skipped:
        lines.append("")
        lines.append(f"{len(skipped)} already in the ledger ({ledger_path}) — skipped.")
    return "\n".join(lines)


def build_payload(new_rows, skipped, unparsed, ledger_path, query):
    return {
        "query": query,
        "ledger_path": str(ledger_path),
        "counts": {
            "new": len(new_rows),
            "already_submitted": len(skipped),
            "unparsed": len(unparsed),
        },
        "orders": new_rows,
        "already_submitted": [r["tracking_number"] for r in skipped],
        "unparsed": unparsed,
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ebay-shipped",
        description=(
            "List shipped eBay orders whose tracking number is not yet in the "
            "EZShip ledger. Read-only: never modifies Gmail or the ledger."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=_version_string(),
        help="Print the installed version and the git SHA/date it was built "
             "from, then exit. Use this to check for a stale `uv tool install` "
             "(see scripts/install.sh).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit JSON instead of a human table. Always a top-level object "
             "with `counts`, `orders`, `already_submitted`, and `unparsed`.",
    )
    parser.add_argument(
        "--days", type=int, default=45, metavar="N",
        help="How far back to search Gmail (default: 45).",
    )
    parser.add_argument(
        "--max-results", type=int, default=60, metavar="N",
        help="Maximum Gmail messages to examine (default: 60).",
    )
    parser.add_argument(
        "--ledger", type=Path, default=DEFAULT_LEDGER, metavar="PATH",
        help=f"EZShip dedup ledger to read (default: {DEFAULT_LEDGER}). Read-only.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Include orders already present in the ledger instead of skipping them.",
    )
    parser.add_argument(
        "--query", default=None,
        help="Override the Gmail search query entirely (advanced).",
    )
    args = parser.parse_args(argv)

    query = args.query or f"{DEFAULT_QUERY} newer_than:{args.days}d"

    try:
        submitted = set() if args.all else load_ledger(args.ledger)
        messages = list(fetch_messages(query, args.max_results))
    except ShippedOrdersError as exc:
        # Hard-fail rather than render an empty result: "no new orders" and
        # "could not reach Gmail" must never look the same to a caller.
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_HARD_FAILURE

    new_rows, skipped, unparsed = collect(messages, submitted)

    if args.json_output:
        print(json.dumps(
            build_payload(new_rows, skipped, unparsed, args.ledger, query), indent=2))
    else:
        print(render_table(new_rows, skipped, unparsed, args.ledger))

    return _EXIT_UNPARSED if unparsed else _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
