#!/usr/bin/env python3
"""grade-photos: download eBay listing photos for /comic:grade.

Extracted from .claude/commands/comic/grade.md Step 1 (BUI-279) — the
downloader was ~90 lines of inline Python re-read into every grade run's
context though it executes nothing until invoked. Same OAuth flow, same
Browse API calls (get_item_by_legacy_id), same image-download logic, same
stdout contract the grader depends on — only the item IDs move from a
hardcoded list to argv.

BUI-283: OAuth + Browse-API fetch now reuse ebay_fetch.py's load_config() /
get_token() / fetch_item() instead of a second copy of that logic. Two
deliberate behavior changes from the pre-BUI-283 version:

- Token caching is ADOPTED: ebay_fetch.get_token() writes/reads
  token_cache_{env}.json (5-min expiry buffer) instead of doing a fresh
  OAuth call on every run. This matches seller_scan.py and
  wishlist_sellers.py (the existing reuse idiom in this package) and drops
  a redundant OAuth round-trip per grade run. grade.md's Step 1 contract
  depends only on the per-line stdout format below, not on a fresh token
  each run, so this is safe to adopt.
- Credential loading goes through ebay_fetch.load_config(), which is
  env-var-first with the config file as a fallback — fixing a latent bug
  where this module used to open ~/.config/ebay-fetch/config.json
  unconditionally at import time, raising FileNotFoundError at import on
  any machine that only sets EBAY_CLIENT_ID/EBAY_CLIENT_SECRET env vars.

Usage:
    python src/grade_photos.py ITEM_ID [ITEM_ID ...] [--workdir DIR]

Labels each item comic-1, comic-2, ... in input order (matches the
/tmp/comic-grading/comic-N/ layout the grader agents expect). Prints one
line per item:

    comic-1: FETCH FAILED — <error>
    comic-1: <title> — <N> images — current bid $12.34 (3 bids)
"""

import argparse
import sys
import urllib.request
from pathlib import Path

from ebay_fetch import fetch_item, get_token, load_config


def download_listing(token, item_id, outdir, base_url):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # BUI-283: fetch_item() returns None on failure (already logged detail to
    # stderr) rather than raising. Adapt that back to a raised RuntimeError so
    # the FETCH FAILED contract below (BUI-147: a fetch failure is NOT zero
    # images) stays intact — without this, a None result would need its own
    # empty-image handling and would be indistinguishable from a genuinely
    # photo-less listing.
    data = fetch_item(item_id, token, base_url)
    if data is None:
        raise RuntimeError(f"item {item_id}: fetch failed (see stderr for detail)")
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
    client_id, client_secret, base_url = load_config()
    # BUI-283: reuse ebay_fetch's cached get_token() (writes token_cache_{env}.json,
    # 5-min buffer) instead of a fresh OAuth call every run — see module docstring.
    token = get_token(client_id, client_secret, base_url)
    for label, item_id in items:
        try:
            result = download_listing(token, item_id, f"{args.workdir}/{label}", base_url)
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
