#!/usr/bin/env python3
"""grade-photos: download eBay listing photos for /comic:grade.

Extracted from .claude/commands/comic/grade.md Step 1 (BUI-279) — the
downloader was ~90 lines of inline Python re-read into every grade run's
context though it executes nothing until invoked. Same OAuth flow, same
Browse API calls (get_item_by_legacy_id), same image-download logic, same
stdout contract the grader depends on — only the item IDs move from a
hardcoded list to argv.

Usage:
    python src/grade_photos.py ITEM_ID [ITEM_ID ...] [--workdir DIR]

Labels each item comic-1, comic-2, ... in input order (matches the
/tmp/comic-grading/comic-N/ layout the grader agents expect). Prints one
line per item:

    comic-1: FETCH FAILED — <error>
    comic-1: <title> — <N> images — current bid $12.34 (3 bids)
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import requests

with open(Path("~/.config/ebay-fetch/config.json").expanduser()) as _f:
    _cfg = json.load(_f)
APP_ID = os.environ.get("EBAY_CLIENT_ID") or _cfg.get("client_id")
CERT_ID = os.environ.get("EBAY_CLIENT_SECRET") or _cfg.get("client_secret")
BASE_URL = "https://api.ebay.com"


def _request_json(method, url, *, headers=None, params=None, data=None, what="request"):
    # BUI-147: mirror ebay_fetch.py's contract — retry 429 with backoff and
    # abort LOUDLY on a persistent non-200. Without this, a down/429/404 API
    # returns an error body, .json() yields no image keys, and the listing is
    # reported with image_count=0 — indistinguishable from a genuinely
    # photo-less listing, so the Step 1.5 triage silently DROPs a real book.
    for attempt in range(4):
        resp = requests.request(method, url, headers=headers, params=params, data=data, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
    raise RuntimeError(f"{what}: still rate-limited (429) after retries")


def get_token():
    creds = base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    tok = _request_json(
        "POST", f"{BASE_URL}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        what="oauth token",
    )
    if "access_token" not in tok:  # guard: don't KeyError on a malformed body
        raise RuntimeError(f"oauth token response missing access_token: {tok}")
    return tok["access_token"]


def download_listing(token, item_id, outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    data = _request_json(
        "GET", f"{BASE_URL}/buy/browse/v1/item/get_item_by_legacy_id",
        headers=headers, params={"legacy_item_id": item_id}, what=f"item {item_id}",
    )
    imgs = []
    if "image" in data:
        imgs.append(data["image"]["imageUrl"])
    for ai in data.get("additionalImages", []):
        imgs.append(ai["imageUrl"])
    for i, url in enumerate(imgs, 1):
        urllib.request.urlretrieve(url, outdir / f"img-{i:02d}.jpg")
    # Auction value signal for the value gate (Step 2): currentBidPrice/bidCount are
    # already in the Browse API response — capture them, no extra request needed.
    # BUI-165: for a fixed-price (BIN) listing currentBidPrice is absent, so this
    # falls back to the BIN price. current_price is therefore the live bid OR the
    # BIN price (None only when no price field is present at all), and it counts
    # toward the Step 2 value gate just like an auction bid does.
    price_node = data.get("currentBidPrice") or data.get("price") or {}
    try:
        current_price = float(price_node.get("value")) if price_node.get("value") is not None else None
    except (TypeError, ValueError):
        current_price = None
    return {
        "title": data.get("title", item_id),
        "image_count": len(imgs),
        "current_price": current_price,            # USD float: live bid or BIN price; None only if absent
        "bid_count": data.get("bidCount"),         # int for auctions, None otherwise
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="grade-photos",
        description="Download eBay listing photos for /comic:grade via the Browse API.",
    )
    parser.add_argument("item_ids", nargs="+", help="eBay legacy item IDs (numeric).")
    parser.add_argument(
        "--workdir", default="/tmp/comic-grading",
        help="Output root; each item lands in <workdir>/comic-N/ (default: /tmp/comic-grading).",
    )
    args = parser.parse_args(argv)

    items = [(f"comic-{i}", item_id) for i, item_id in enumerate(args.item_ids, 1)]
    token = get_token()
    for label, item_id in items:
        try:
            result = download_listing(token, item_id, f"{args.workdir}/{label}")
        except RuntimeError as e:
            # BUI-147: a fetch failure is NOT zero images. Surface it loudly so the
            # Step 1.5 triage doesn't DROP a real book as "un-gradeable".
            print(f"{label}: FETCH FAILED — {e}")
            continue
        price = result["current_price"]
        price_str = f"${price:.2f}" if price is not None else "n/a"
        print(f"{label}: {result['title']} — {result['image_count']} images — current bid {price_str} ({result['bid_count']} bids)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
