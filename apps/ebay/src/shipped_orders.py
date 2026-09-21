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
There are two independent places a number can be read from, and the tool reads
both. Each extracted row is tagged with the `source` it came from, because the
two have different strength.

**Source 1 — a carrier's own tracking URL** (`source: "carrier_url"`). The
number sits in a query parameter on the carrier's own host, e.g.
`wwwapps.ups.com/WebTracking/track?track=yes&trackNums=1Z…`. It is copied
verbatim out of the URL, so no transcription step exists for a slip to enter
through. This is the shape `eBay - <seller> <…@members.ebay.com>` mail uses,
and also what a Shopify-style merchant mail embeds next to its label.

**Source 2 — an explicit `Tracking number:` label** (`source: "label"`). Added
in BUI-916. The number is taken only from directly after a tracking *label* in
the message's rendered text — never from a bare digit run. This is the only
readable position in three shapes that source 1 cannot see at all:

  * `eBay <ebay@ebay.com>` "Delivery Update" mail, which prints the number as
    the text of a link whose href is the authenticated order page;
  * a merchant's own shipping confirmation (Barnes & Noble, a Shopify store);
  * a human-typed seller mail ("Here is your tracking number: 9505…").

The eBay generic "Your package is now with its carrier!" shape remains
genuinely unreadable: it names the carrier and the seller but never the number,
and links only to an authenticated `ebay.com/vod/FetchOrderDetails?itemId=…`
page. Those are surfaced as *named uncovered orders* rather than dropped —
see "Naming what cannot be covered".

Measured coverage (BUI-916)
───────────────────────────
Against the 17 entries the user actually submitted to EZShip, over the mail
window those entries span (2026-06-15 … 2026-08-25, 1704 messages scanned
across every sender, each of the 17 numbers grepped for in every decoded body):

  ==================================================  =====
  source                                              of 17
  ==================================================  =====
  carrier URL, `from:ebay.com` only (BUI-807's tool)      6
  carrier URL, any sender                                 7
  + labelled field (BUI-916, source 2)                   10
  present in no email at all                              7
  ==================================================  =====

The remaining 7 are all USPS numbers from eBay sellers who send no
`members.ebay.com` notification. They exist in no message in the mailbox, so
no email-based source can reach them; they need the authenticated eBay order
page, which this tool deliberately does not touch. Three of the ten are not
eBay orders at all (a Shopify store, Barnes & Noble, a direct seller) — the
EZShip ledger is the user's consolidation ledger across every merchant, not an
eBay-only ledger, which is why the Gmail query is no longer `from:ebay.com`.

eBay's own APIs cannot close the gap, and this was checked rather than
assumed: our credentials are an app-level `client_credentials` grant scoped to
`https://api.ebay.com/oauth/api_scope` (see `ebay_fetch.py`), which reaches the
Browse API only. `ebay-fetch <item-id>` does still resolve a sold listing, but
Browse returns listing data — title, price, condition, seller — and has no
fulfillment surface whatsoever. Buyer-side tracking needs a user-authorised
token against the Buy Order API, which is limited-release.

Naming what cannot be covered
─────────────────────────────
A missing order must never look like no order. Every eBay shipping mail
carries `itemId` and `transactionId` in its order-page URL (83/83 in the
measured window), so a shipment whose number cannot be read is reported as a
*named* order — item id, transaction id, date, and subject — one line per
order rather than one per message, and never collapsed into a count.

Precision was checked against ground truth: every UPS number this parser
extracts from mail (5 distinct) appears in the EZShip ledger the user typed by
hand, exactly — 5/5 match, no extras, no misses. Coverage is a different
number: 5 of the ledger's 17 entries are recoverable this way; the other 12
(USPS/FedEx) arrived only in the generic shape and were never in any email.

Never guess a tracking number
─────────────────────────────
A wrong number is worse than a missing one — it would ship a real package to
the wrong consolidation record. Two guards, in order:

  1. **Positional.** The number is read only from a position the sender marked
     as a tracking number: a tracking-named query parameter on a *carrier's
     own domain* (source 1), or text immediately following an explicit
     `Tracking number:` label (source 2). It is never scraped out of free
     text, where item ids, order numbers, and marketing ids are all long digit
     runs — a naive digit regex on a real message yields four item ids and a
     campaign id before it finds anything shippable.
  2. **Carrier rule.** The candidate must then pass a carrier's
     format/check-digit rule. Anything that fails degrades to `unparsed` with a
     reason — it is never emitted as a row and never silently dropped.

Guard 1 is the load-bearing one. It is strictly stronger for source 1, where
the number is copied verbatim out of a URL; source 2 passes through HTML→text
rendering first, which is why the two are tagged apart in the output. Guard 2
catches a mangled or truncated extraction — a weighted mod-10 sum catches every
adjacent transposition and ~88% of single-character substitutions, not 100%,
and the tests pin that measured figure rather than a flattering claim.

Source 2's precision was measured on the same 1704-message corpus. Run in
isolation it extracted 9 distinct numbers, every one of them a number its own
sender had labelled a tracking number and zero fabricated; 5 of those 9 are
reachable *only* this way, the other 4 also appearing in a carrier URL and so
reported as `carrier_url`. Prose after a label ("all tracking numbers are
forwarded at time of shipping") is rejected before validation by requiring at
least 8 digits in the candidate, so it does not even become a warning. A
labelled number for a carrier with no rule here (Shopee's `SPXSG…`) is reported
as `unknown_carrier` rather than emitted unvalidated. And a number shaped like a
check-digit carrier's is that carrier's to accept or refuse — no looser,
shape-only rule may rescue it (see `classify_tracking`).

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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import NamedTuple

# Exit codes (mirroring seller_scan's convention of distinct, documented codes)
_EXIT_OK = 0
_EXIT_HARD_FAILURE = 1  # Gmail or the ledger could not be read — never "nothing new"
_EXIT_TRUNCATED = 2     # the Gmail window was cut short — coverage is incomplete
_EXIT_UNPARSED = 3      # ran fine, but some shipping mail had no readable number

# Why a shipping mail yielded no number. The classes get very different
# treatment (a named-order list vs. an itemised warning), so they are named
# rather than re-derived from the human-readable reason string.
UNPARSED_NO_TRACKING_URL = "no_tracking_url"
UNPARSED_VALIDATION_FAILED = "validation_failed"
UNPARSED_UNKNOWN_CARRIER = "unknown_carrier"

# Where a validated number was read from. Kept on every row because the two
# positions are not equally strong — see "Never guess a tracking number".
SOURCE_CARRIER_URL = "carrier_url"
SOURCE_LABEL = "label"

DEFAULT_LEDGER = Path.home() / ".config" / "ezship" / "submitted-orders.json"
DEFAULT_GWS_CONFIG_DIR = Path.home() / ".config" / "gws-personal"

# eBay shipping mail lands in the personal Gmail account; the default `gws`
# profile is a different (work) account and returns nothing for these queries.
GWS_CONFIG_DIR_ENV = "EBAY_SHIPPED_GWS_CONFIG_DIR"

# Two clauses, deliberately. The first is BUI-807's eBay clause, kept verbatim
# so nothing it used to reach is lost. The second admits any sender whose mail
# spells out a tracking label — the only position source 2 can read, and the
# reason three non-eBay merchants' numbers became recoverable at all. Measured
# over the ledger's 70-day window: eBay clause alone 109 messages / 7 of 17
# ledger numbers present; both clauses 338 messages / 10 of 17.
DEFAULT_QUERY = (
    '(from:ebay.com (shipped OR package OR tracking OR delivered OR carrier)) '
    'OR "tracking number" OR "tracking no" OR "tracking #"'
)

# Bounded concurrency for the per-message Gmail fetch. Gmail exposes no batch
# read for messages (`gws gmail users messages` offers only batchDelete and
# batchModify, both writes), and grouping by thread does not help — the measured
# window held 109 messages across 100 distinct threads. So the fetch is
# parallelised instead: 703 messages took ~4 min serially and 33 s at 8 workers.
DEFAULT_MAX_WORKERS = 8


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


# The second source (BUI-916): an explicit tracking *label*, which is the only
# readable position in eBay's own "Delivery Update" mail and in a merchant's or
# a human seller's shipping note. Anchored on the label, never on a digit run.
#
# Two gaps have to be tolerated, and both are bounded rather than open-ended.
# Between the label and the number: whitespace including newlines, because eBay
# renders `Tracking number:` and the digits in separate table cells — but at most
# a short run, so the match cannot leap a blank region into an unrelated number.
# The bound is 8; the widest gap measured across 1704 real messages was 5.
# Inside the number: spaces and hyphens, because USPS numbers are often printed
# in groups (`9405 5118 9922 3197 4284 90`). `_label_candidates` then re-tries
# shorter token prefixes, so trailing prose ("… arriving Monday") cannot swallow
# the number either.
_TRACKING_LABEL = re.compile(
    r"tracking\s*(?:numbers?|nos?\.?|#|id)\s*(?:is)?\s*[:\-#]*[\s ]{0,8}"
    r"((?:[0-9A-Za-z][0-9A-Za-z\-]*[  \n]{0,3}){1,8})",
    re.I,
)
_CANDIDATE_SHAPE = re.compile(r"[0-9A-Z]{10,35}")

# Carriers whose numbers carry a real check digit, and the format that says the
# number *belongs* to them. This pairing exists to stop a shape-only rule from
# rescuing a number a check-digit rule has already rejected: a 22-digit USPS
# number with a corrupted check digit also satisfies FedEx's `\d{22}`, so
# trying the rules in order would quietly relabel a mangled USPS number as an
# unverified FedEx one and emit it. The URL path cannot hit this — each rule
# there is bound to its own carrier's host — but the label path tries them all.
_CHECKED_FORMATS = [
    ("UPS", re.compile(r"1Z[0-9A-Z]{16}"), ups_check_digit_ok),
    ("USPS", re.compile(r"\d{20}|\d{22}|\d{26}"), usps_check_digit_ok),
]


def classify_tracking(candidate: str):
    """Return (carrier, carrier_verified), or None if nothing may claim it.

    A candidate shaped like a check-digit carrier's number is that carrier's to
    accept or refuse; no looser rule gets a second opinion on it.
    """
    for carrier, shape, validator in _CHECKED_FORMATS:
        if shape.fullmatch(candidate):
            return (carrier, True) if validator(candidate) else None
    for carrier, verified, validator, _pattern in _CARRIER_RULES:
        if validator(candidate):
            return carrier, verified
    return None

# A tracking number is mostly digits. Requiring this many keeps prose out of
# the warning list entirely: "all tracking numbers are forwarded at time of
# shipping" is a real sentence in this mailbox, and it must not read as a
# shipment whose number could not be parsed.
_MIN_DIGITS = 8


def _label_candidates(capture: str):
    """Yield plausible numbers from one label capture, longest form first.

    Longest-first matters: `9405 5118 9922 3197 4284 90` is one number in six
    tokens, so the full join has to be tried before any prefix of it.
    """
    tokens = [t for t in re.split(r"[\s ]+", capture) if t][:8]
    for end in range(len(tokens), 0, -1):
        candidate = "".join(tokens[:end]).replace("-", "").upper()
        if not _CANDIDATE_SHAPE.fullmatch(candidate):
            continue
        if sum(ch.isdigit() for ch in candidate) < _MIN_DIGITS:
            continue
        yield candidate


def extract_labeled_tracking(text: str):
    """Return (found, rejected) from a message's *rendered text*.

    `found` is a list of (carrier, tracking, carrier_verified) in the same shape
    as `extract_tracking`; `rejected` is a list of candidates that looked like a
    tracking number and were labelled as one, but matched no carrier rule here
    (a real Shopee `SPXSG…` number, for instance). Those become a visible
    `unknown_carrier` warning rather than an unvalidated row.

    Takes rendered text, not raw HTML, because the label and the number are
    routinely separated by markup — eBay wraps the number in nested spans with
    an inline `<style>` block between the label and the digits.
    """
    found, rejected, seen = [], [], set()
    for capture in _TRACKING_LABEL.findall(text):
        matched = None
        shaped = []
        for candidate in _label_candidates(capture):
            shaped.append(candidate)
            carrier = classify_tracking(candidate)
            if carrier is not None:
                matched = (carrier[0], candidate, carrier[1])
                break
        if matched:
            if matched[:2] not in seen:
                seen.add(matched[:2])
                found.append(matched)
        elif shaped:
            # Report the *shortest* qualifying form. Candidates arrive
            # longest-first, and for an unvalidatable number the long forms are
            # the label's number with following prose glued on; the short one is
            # the number as printed. A multipart message renders the same label
            # twice, so this also makes the two copies dedupe to one warning.
            hint = min(shaped, key=len)
            if hint not in rejected:
                rejected.append(hint)
    return found, rejected


def extract_shipments(body: str, text: str):
    """Both sources, provenance-tagged, URL first.

    Returns (found, url_rejected, label_rejected) where `found` is a list of
    (carrier, tracking, carrier_verified, source). A number reachable both ways
    is reported once, as `carrier_url` — the stronger of the two positions.
    """
    url_found, url_rejected = extract_tracking(body)
    label_found, label_rejected = extract_labeled_tracking(text)

    found = [(c, t, v, SOURCE_CARRIER_URL) for c, t, v in url_found]
    known = {t for _c, t, _v in url_found}
    for carrier, tracking, verified in label_found:
        if tracking not in known:
            known.add(tracking)
            found.append((carrier, tracking, verified, SOURCE_LABEL))
    # A number read successfully from a label is not also a failure, even if a
    # mangled copy of it appeared in a URL somewhere in the same message.
    url_rejected = [(c, raw) for c, raw in url_rejected if raw not in known]
    label_rejected = [raw for raw in label_rejected if raw not in known]
    return found, url_rejected, label_rejected


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


_DISPLAY_NAME = re.compile(r"^\s*\"?([^\"<]+?)\"?\s*<")


def parse_seller(from_header: str):
    """Seller login from the From display name (`eBay - timemachinecomics <…>`).

    Only the `members.ebay.com` shape names the seller this way; eBay's own
    mail is just `eBay <ebay@ebay.com>`, so this returns None there.
    """
    match = _SELLER_FROM.match(from_header or "")
    return match.group(1).strip() if match else None


def parse_merchant(from_header: str):
    """Fallback vendor name for mail that is not the `eBay - <seller>` shape.

    Once the query reaches beyond eBay (BUI-916), three of the ten recoverable
    shipments come from a merchant's own mail — a Shopify store, Barnes & Noble,
    a seller typing by hand. Submitting one to EZShip needs a vendor name, and
    the From display name is the only one those messages carry. Returns None for
    eBay's own generic mail, whose display name is just "eBay" and says nothing
    about who shipped.
    """
    header = from_header or ""
    if re.search(r"<[^>]*@(?:\w+\.)*ebay\.com>", header, re.I):
        return None
    match = _DISPLAY_NAME.match(header)
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


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

# eBay's order identity, lifted from the authenticated order-page link that every
# eBay shipping mail carries (83/83 in the measured window). This is what lets an
# uncovered order be *named* instead of counted — see "Naming what cannot be
# covered". It is an identity only; the page itself is never fetched.
# `&amp;` is matched as well as `&`: these read the raw body, which is HTML in
# the shape that carries them, and an html-only message escapes every separator
# after the first. Missing that would silently drop the transaction id and leave
# an uncovered order half-named.
_EBAY_ITEM_ID = re.compile(r"[?&](?:amp;)?itemId=(\d{9,15})", re.I)
_EBAY_TXN_ID = re.compile(r"[?&](?:amp;)?transactionId=(\d{9,18})", re.I)

# Subjects that assert a package physically moved. Kept deliberately narrow:
# "You got it!" (an auction win) and saved-search blasts must not qualify.
# "delivery update" was added in BUI-916: it is eBay's own class, it carries a
# labelled tracking number, and the old pattern classified it `ignore` — the
# whole message was dropped before extraction ever ran.
_SHIPPING_SUBJECT = re.compile(
    r"package|shipped|shipment|shipping confirmation|out for delivery|delivered|"
    r"with its carrier|order is being prepared|estimated to arrive|"
    r"an update on your order|delivery update|on the way",
    re.I,
)


def is_shipping_mail(subject: str, body_text: str) -> bool:
    """Whether a message *asserts a shipment*, independent of any number.

    This is only ever asked of a message from which no number could be read: a
    validated tracking number is itself conclusive evidence of a shipment, and
    `parse_message` extracts before it asks this. That ordering is load-bearing —
    Barnes & Noble's "Your Barnes & Noble Shipping Confirmation #…" and a
    seller's "Re: Want List For …" both carry a real labelled number and both
    used to fail this test, so gating extraction on it lost them outright.
    """
    if _SHIPPING_SUBJECT.search(subject or ""):
        return True
    return bool(re.search(r"Shipped\s*Via|Track My Package|Track package|Track order",
                          body_text or "", re.I))


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

    # Extraction comes first, deliberately. A validated number proves a shipment
    # outright, so it must not be gated on the subject heuristic — see
    # `is_shipping_mail`. The heuristic only decides whether a *number-less*
    # message is worth warning about.
    found, url_rejected, label_rejected = extract_shipments(body, text)

    if not found and not url_rejected and not label_rejected \
            and not is_shipping_mail(subject, text):
        return {"kind": "ignore", "message_id": message.get("id"), "subject": subject}

    item_match = _EBAY_ITEM_ID.search(body)
    txn_match = _EBAY_TXN_ID.search(body)
    order_match = _ORDER_NO.search(text)
    via_match = _SHIPPED_VIA.search(text)
    sent_at = _message_date(message, hdrs)

    base = {
        "message_id": message.get("id"),
        "subject": subject,
        "seller": parse_seller(from_header) or parse_merchant(from_header),
        "order_number": order_match.group(1) if order_match else None,
        "shipped_via": via_match.group(1).strip() if via_match else None,
        "item_id": item_match.group(1) if item_match else None,
        "transaction_id": txn_match.group(1) if txn_match else None,
        "sent_at": sent_at,
    }

    if not found:
        if url_rejected:
            # The alarming class: the sender's own carrier URL held a number
            # that does not check out. Something mangled it.
            unparsed_class = UNPARSED_VALIDATION_FAILED
            reason = (
                "tracking URL present but the number failed "
                + ", ".join(f"{c} validation ({v})" for c, v in url_rejected)
            )
        elif label_rejected:
            unparsed_class = UNPARSED_UNKNOWN_CARRIER
            reason = (
                "a tracking number is named (" + ", ".join(label_rejected)
                + ") but matches no carrier rule here — likely a carrier this "
                "tool does not model, so it is reported rather than trusted"
            )
            base["candidates"] = label_rejected
        else:
            unparsed_class = UNPARSED_NO_TRACKING_URL
            reason = (
                "the number appears nowhere in this message — no carrier "
                "tracking URL and no tracking label. eBay's generic shipping "
                "mail links to an authenticated order page instead of naming it"
            )
        return {**base, "kind": "unparsed",
                "unparsed_class": unparsed_class, "reason": reason}

    return {
        **base,
        "kind": "rows",
        "tracking": [
            {"tracking_number": tracking, "carrier": carrier,
             "carrier_verified": verified, "source": source}
            for carrier, tracking, verified, source in found
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


class Fetched(NamedTuple):
    """The result of one Gmail sweep.

    `truncated` is not a detail — it is the difference between "no new orders"
    and "I stopped looking". A silently short window is the exact failure this
    tool exists to prevent, so it is carried out to the caller and costs the run
    a distinct exit code rather than being logged and forgotten.
    """

    messages: list
    truncated: bool
    listed: int


_GMAIL_PAGE_SIZE = 500  # the Gmail API's own ceiling for messages.list


def list_message_ids(query: str, max_results: int):
    """Return (ids, truncated) for `query`, newest first, following pages.

    `messages.list` caps a page at 500, so anything above that needs the
    pageToken loop — without it a `--max-results 600` would silently return 500.
    """
    ids: list[str] = []
    token = None
    while len(ids) < max_results:
        params = {
            "userId": "me",
            "q": query,
            "maxResults": min(_GMAIL_PAGE_SIZE, max_results - len(ids)),
        }
        if token:
            params["pageToken"] = token
        listing = _run_gws("list", params)
        page = listing.get("messages") or []
        ids.extend(stub["id"] for stub in page)
        token = listing.get("nextPageToken")
        if not token or not page:
            return ids[:max_results], False
    # More matched than we were allowed to look at.
    return ids[:max_results], token is not None


def fetch_messages(query: str, max_results: int, max_workers: int = DEFAULT_MAX_WORKERS):
    """Fetch full Gmail messages matching `query`.

    The per-message `get` is the expensive part and Gmail exposes no batch read
    for it, so the calls run on a bounded thread pool (see DEFAULT_MAX_WORKERS
    for the measurement). Concurrency must not soften failure: the pool is
    drained inside this function with `list(...)`, so a `ShippedOrdersError` from
    any single worker propagates and aborts the whole run. Returning the
    messages that happened to succeed would be indistinguishable from a quiet
    week, which is the one outcome this tool may never fake.
    """
    ids, truncated = list_message_ids(query, max_results)
    if not ids:
        return Fetched([], truncated, 0)

    def one(message_id):
        return _run_gws("get", {"userId": "me", "id": message_id, "format": "full"})

    workers = max(1, min(max_workers, len(ids)))
    if workers == 1:
        messages = [one(message_id) for message_id in ids]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            messages = list(pool.map(one, ids))
    return Fetched(messages, truncated, len(ids))


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
            "item_id": record["item_id"],
            "transaction_id": record["transaction_id"],
            "subject": subject,
            "sent_at": sent_at,
            "first_seen_at": sent_at,
            "message_ids": [record["message_id"]],
        }
        return

    if record["message_id"] not in existing["message_ids"]:
        existing["message_ids"].append(record["message_id"])

    # If any message reached this number through a carrier URL, record that: the
    # stronger provenance wins, whichever mail happened to arrive first.
    if shipment.get("source") == SOURCE_CARRIER_URL:
        existing["source"] = SOURCE_CARRIER_URL
        existing["carrier"] = shipment["carrier"]
        existing["carrier_verified"] = shipment["carrier_verified"]

    # sent_at is None only if both internalDate and the Date header were unusable.
    if sent_at is not None:
        if existing["first_seen_at"] is None or sent_at < existing["first_seen_at"]:
            existing["first_seen_at"] = sent_at
        if existing["sent_at"] is None or sent_at > existing["sent_at"]:
            existing["sent_at"] = sent_at
            existing["subject"] = subject

    # A later mail in the lifecycle often fills a field an earlier one omitted.
    for key in ("seller", "order_number", "shipped_via", "item_id", "transaction_id"):
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


def group_uncovered(unparsed):
    """Collapse number-less shipping mail into one entry per *order*.

    The ticket's bar is that an order this tool cannot cover is named rather
    than omitted, and the unit of that promise is the order, not the message: a
    single eBay purchase generates up to four mails, and listing it four times
    reads as four unsolved problems. eBay's `itemId`/`transactionId` pair is the
    identity where present (83/83 of the measured eBay shipping mails carry it);
    a message without one can only be identified by itself.
    """
    orders: dict[tuple, dict] = {}
    for record in unparsed:
        item_id, txn_id = record.get("item_id"), record.get("transaction_id")
        key = ("ebay", item_id, txn_id) if item_id else ("message", record.get("message_id"))
        entry = orders.get(key)
        if entry is None:
            orders[key] = {
                "item_id": item_id,
                "transaction_id": txn_id,
                "seller": record.get("seller"),
                "order_number": record.get("order_number"),
                "shipped_via": record.get("shipped_via"),
                "subject": record.get("subject"),
                "first_seen_at": record.get("sent_at"),
                "last_seen_at": record.get("sent_at"),
                "unparsed_class": record.get("unparsed_class"),
                "reason": record.get("reason"),
                "message_ids": [record.get("message_id")],
            }
            continue
        if record.get("message_id") not in entry["message_ids"]:
            entry["message_ids"].append(record.get("message_id"))
        sent_at = record.get("sent_at")
        if sent_at is not None:
            if entry["first_seen_at"] is None or sent_at < entry["first_seen_at"]:
                entry["first_seen_at"] = sent_at
            if entry["last_seen_at"] is None or sent_at > entry["last_seen_at"]:
                entry["last_seen_at"] = sent_at
                entry["subject"] = record.get("subject")
        for field in ("seller", "order_number", "shipped_via"):
            if not entry.get(field) and record.get(field):
                entry[field] = record[field]

    grouped = list(orders.values())
    grouped.sort(key=lambda r: (r.get("first_seen_at") or "", r.get("item_id") or ""))
    return grouped


# ─── Output ───────────────────────────────────────────────────────────────────

def _name_order(order) -> str:
    """One operator-readable identity for an order with no readable number."""
    if order.get("item_id"):
        identity = f"eBay item {order['item_id']}"
        if order.get("transaction_id"):
            identity += f" txn {order['transaction_id']}"
    elif order.get("order_number"):
        identity = f"order {order['order_number']}"
    else:
        identity = f"gmail message {order.get('message_ids', [None])[0] or '?'}"
    if order.get("seller"):
        identity += f" — {order['seller']}"
    return identity


def render_table(new_rows, skipped, unparsed, ledger_path, fetched=None) -> str:
    lines = []
    if not new_rows:
        lines.append("No new shipped orders with a readable tracking number.")
    else:
        lines.append(f"{len(new_rows)} new shipped order(s) not yet in the EZShip ledger:")
        lines.append("")
        # 26 wide: a USPS impb number runs to 26 digits, and a narrower column
        # pushes every later field out of alignment on exactly the rows that
        # matter most.
        header = (f"{'TRACKING NUMBER':<26} {'CARRIER':<8} {'SOURCE':<11} "
                  f"{'SELLER':<22} {'SHIPPED':<12} STATUS")
        lines.append(header)
        lines.append("-" * len(header))
        for row in new_rows:
            date = (row.get("first_seen_at") or "")[:10] or "—"
            carrier = row["carrier"] + ("" if row.get("carrier_verified", True) else "*")
            lines.append(
                f"{row['tracking_number']:<26} {carrier:<8} "
                f"{row.get('source', SOURCE_CARRIER_URL):<11} "
                f"{(row.get('seller') or '—'):<22} {date:<12} {row.get('subject', '')[:40]}"
            )
        if any(not r.get("carrier_verified", True) for r in new_rows):
            lines.append("")
            lines.append("  * carrier pattern not yet verified against a real shipment "
                         "— confirm the number before submitting.")

    # Three very different classes hide in `unparsed`, and flattening them buries
    # the ones that matter. A number that failed its own carrier's rule is rare
    # and alarming; a labelled number for an unmodelled carrier is readable by a
    # human right now; a shipment whose number is in no message at all is the
    # structural ceiling. All three are itemised — the whole point of BUI-916 is
    # that an order this tool cannot cover is *named*, never counted away.
    suspicious = [r for r in unparsed if r["unparsed_class"] == UNPARSED_VALIDATION_FAILED]
    unknown = [r for r in unparsed if r["unparsed_class"] == UNPARSED_UNKNOWN_CARRIER]
    structural = [r for r in unparsed if r["unparsed_class"] == UNPARSED_NO_TRACKING_URL]

    if suspicious:
        lines.append("")
        lines.append(f"{len(suspicious)} message(s) had a tracking URL whose number "
                     "FAILED validation — not guessed, check by hand:")
        for row in suspicious:
            date = (row.get("sent_at") or "")[:10] or "—"
            lines.append(f"  - {date}  {row.get('seller') or 'eBay'}: "
                         f"{row.get('subject', '')[:52]}")
            lines.append(f"      {row['reason']}")

    if unknown:
        # One entry per number, not per message: a lifecycle sends the same
        # unreadable number three times and listing it three times reads as
        # three problems.
        by_number: dict[str, dict] = {}
        for row in unknown:
            for candidate in row.get("candidates") or ["?"]:
                entry = by_number.setdefault(
                    candidate, {"seller": row.get("seller"),
                                "sent_at": row.get("sent_at"),
                                "subject": row.get("subject", "")})
                # Guarded against None on both sides: an undated message must
                # not blank out a date another message already supplied.
                sent_at = row.get("sent_at")
                if sent_at is not None and (entry["sent_at"] is None
                                            or sent_at < entry["sent_at"]):
                    entry["sent_at"] = sent_at
        lines.append("")
        lines.append(f"{len(by_number)} tracking number(s) are named for a carrier this "
                     "tool cannot validate — readable by hand, not guessed:")
        for candidate, entry in sorted(by_number.items(), key=lambda kv: kv[1]["sent_at"] or ""):
            date = (entry.get("sent_at") or "")[:10] or "—"
            lines.append(f"  - {date}  {candidate}  "
                         f"({entry.get('seller') or 'unknown sender'}: "
                         f"{(entry.get('subject') or '')[:44]})")

    if structural:
        uncovered = group_uncovered(structural)
        # Split on whether the order has an eBay identity. Only those are
        # recoverable from the eBay order page, and telling an operator to look
        # up an Amazon shipment there is advice that cannot be followed.
        ebay_orders = [o for o in uncovered if o.get("item_id")]
        other = [o for o in uncovered if not o.get("item_id")]

        if ebay_orders:
            lines.append("")
            lines.append(f"{len(ebay_orders)} shipped eBay order(s) CANNOT be covered — "
                         "the number appears in no message. Named here, not omitted; "
                         "recover each from its eBay order page by hand:")
            for order in ebay_orders:
                date = (order.get("first_seen_at") or "")[:10] or "—"
                lines.append(f"  - {date}  {_name_order(order)}")
                lines.append(f"      {(order.get('subject') or '(no subject)')[:70]}")
                if order.get("shipped_via"):
                    lines.append(f"      shipped via {order['shipped_via']}")

        if other:
            lines.append("")
            lines.append(f"{len(other)} other shipping mail(s) carried no tracking number "
                         "and no order id — named, but not eBay orders:")
            for order in other:
                date = (order.get("first_seen_at") or "")[:10] or "—"
                lines.append(f"  - {date}  {order.get('seller') or 'unknown sender'}: "
                             f"{(order.get('subject') or '(no subject)')[:56]}")

    if skipped:
        lines.append("")
        lines.append(f"{len(skipped)} already in the ledger ({ledger_path}) — skipped.")

    if fetched is not None and fetched.truncated:
        lines.append("")
        lines.append(f"WARNING: the Gmail window was TRUNCATED at {fetched.listed} "
                     "message(s) — more matched than were examined, so orders may be "
                     "missing from this report. Raise --max-results or narrow --days.")
    return "\n".join(lines)


def build_payload(new_rows, skipped, unparsed, ledger_path, query, fetched=None):
    uncovered = group_uncovered(
        [r for r in unparsed if r["unparsed_class"] == UNPARSED_NO_TRACKING_URL])
    return {
        "query": query,
        "ledger_path": str(ledger_path),
        # `truncated` is in the payload, not just the prose, so an unattended
        # caller can refuse to act on a short window without parsing English.
        "truncated": bool(fetched.truncated) if fetched is not None else False,
        "messages_examined": fetched.listed if fetched is not None else None,
        "counts": {
            "new": len(new_rows),
            "already_submitted": len(skipped),
            "unparsed": len(unparsed),
            "uncovered_orders": len(uncovered),
        },
        "orders": new_rows,
        "already_submitted": [r["tracking_number"] for r in skipped],
        "unparsed": unparsed,
        "uncovered_orders": uncovered,
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ebay-shipped",
        description=(
            "List shipped orders whose tracking number is not yet in the EZShip "
            "ledger, and name every shipped order whose number cannot be read. "
            "Read-only: never modifies Gmail or the ledger."
        ),
        epilog=(
            "Exit codes: 0 every shipment covered; "
            "1 hard failure (Gmail or the ledger unreadable — never treat as "
            "'no new orders'); "
            "2 the Gmail window was truncated, so coverage is incomplete; "
            "3 ran fine, but some shipping mail carried no readable number "
            "(those orders are named in the output). "
            "Note this tool never talks to EZShip: it only reads the ledger "
            "file, so an expired EZShip cookie cannot be detected here — that "
            "check belongs in apps/ezship (see api.ts's SESSION_EXPIRED_MSG)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
             "with `counts`, `orders`, `already_submitted`, `unparsed`, "
             "`uncovered_orders`, and `truncated`.",
    )
    parser.add_argument(
        "--days", type=int, default=45, metavar="N",
        help="How far back to search Gmail (default: 45).",
    )
    parser.add_argument(
        "--max-results", type=int, default=400, metavar="N",
        help="Maximum Gmail messages to examine (default: 400). The default "
             "query reaches every sender that spells out a tracking label, "
             "not just eBay, which measured ~220 messages over 45 days — so a "
             "cap in the dozens would silently cut the window short. A run that "
             "does hit this cap says so and exits 2.",
    )
    parser.add_argument(
        "--max-workers", type=int, default=DEFAULT_MAX_WORKERS, metavar="N",
        help=f"Concurrent Gmail fetches (default: {DEFAULT_MAX_WORKERS}). Gmail "
             "has no batch read for messages, so the per-message fetch is "
             "parallelised instead; 703 messages took ~4 min at 1 and 33 s at 8. "
             "Any single fetch failure still aborts the whole run.",
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
    if args.max_workers < 1:
        parser.error("--max-workers must be at least 1")
    if args.max_results < 1:
        parser.error("--max-results must be at least 1")

    # The parentheses are load-bearing: DEFAULT_QUERY is a top-level OR chain,
    # and Gmail binds a bare trailing `newer_than:` to the last OR branch only.
    # Without them the date filter silently applies to one clause out of four.
    query = args.query or f"({DEFAULT_QUERY}) newer_than:{args.days}d"

    try:
        submitted = set() if args.all else load_ledger(args.ledger)
        fetched = fetch_messages(query, args.max_results, args.max_workers)
    except ShippedOrdersError as exc:
        # Hard-fail rather than render an empty result: "no new orders" and
        # "could not reach Gmail" must never look the same to a caller.
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_HARD_FAILURE

    new_rows, skipped, unparsed = collect(fetched.messages, submitted)

    if args.json_output:
        print(json.dumps(build_payload(
            new_rows, skipped, unparsed, args.ledger, query, fetched), indent=2))
    else:
        print(render_table(new_rows, skipped, unparsed, args.ledger, fetched))

    # Truncation outranks unparsed mail: an incomplete window means the report
    # cannot even enumerate what it missed, so it is the louder failure.
    if fetched.truncated:
        return _EXIT_TRUNCATED
    return _EXIT_UNPARSED if unparsed else _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
