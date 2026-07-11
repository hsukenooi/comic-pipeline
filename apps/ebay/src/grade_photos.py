#!/usr/bin/env python3
"""grade-photos: download eBay listing photos for /comic:grade.

Extracted from .claude/commands/comic/grade.md Step 1 (BUI-279) — the
downloader was ~90 lines of inline Python re-read into every grade run's
context though it executes nothing until invoked. Same OAuth flow, same
Browse API calls (get_item_by_legacy_id), same image-download logic, same
stdout contract the grader depends on — only the item IDs move from a
hardcoded list to argv.

BUI-283: OAuth + Browse-API fetch now reuse ebay_fetch.py's load_config() /
get_token() / fetch_item_with_status() instead of a second copy of that
logic. Two deliberate behavior changes from the pre-BUI-283 version:

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

BUI-310: a token that expires mid-batch now self-heals. On a 401,
download_listing() raises TokenExpiredError; main() force-refreshes the
token once (ebay_fetch.get_token(force_refresh=True)) and retries that one
item, keeping the fresh token for the rest of the batch. A second straight
401, any non-401 failure, or a failed refresh still prints FETCH FAILED for
that item and continues — a token outliving a long batch no longer turns
every remaining item into FETCH FAILED.

Usage:
    python src/grade_photos.py ITEM_ID [ITEM_ID ...] [--workdir DIR]

Labels each item comic-1, comic-2, ... in input order (matches the
/tmp/comic-grading/comic-N/ layout the grader agents expect). Prints one
line per item:

    comic-1: FETCH FAILED — <error>
    comic-1: <title> — <N> images — current bid $12.34 (3 bids)
"""

import argparse
import importlib.metadata
import shutil
import sys
from pathlib import Path

import requests

from ebay_fetch import fetch_item_with_status, get_token, load_config


def _version_string() -> str:
    """BUI-314: staleness signal for a `uv tool install`ed binary.

    `_ebay_build_stamp` is generated at build time by hatch_build.py from the
    git HEAD of the source tree the wheel was built from; it's absent when
    running from an unbuilt checkout (e.g. `uv run` here in tests), so fall
    back to "unknown" rather than failing.
    """
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"grade-photos {pkg_version} (git {GIT_SHA}, {GIT_DATE})"

# BUI-300: a hung image host must not stall the sequential batch indefinitely.
# 15s per image is generous for a single comic-cover-sized JPEG while still
# bounding the worst case. Uses `requests` (not urllib.request.urlretrieve,
# which takes no timeout) — matching the timeout idiom every other network
# call in this package already uses (ebay_fetch.py, seller_scan.py, etc.).
_DOWNLOAD_TIMEOUT_SECONDS = 15


class TokenExpiredError(RuntimeError):
    """Raised by download_listing() when fetch_item_with_status() returns 401.

    BUI-310: a subclass of RuntimeError (not a sibling exception type) so any
    caller that only knows about the pre-existing FETCH FAILED contract still
    catches it via `except RuntimeError`; main() adds a more specific
    `except TokenExpiredError` first to get one refresh-and-retry before
    falling back to that same FETCH FAILED handling.
    """


def _download_image(url, dest, timeout=_DOWNLOAD_TIMEOUT_SECONDS):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def download_listing(token, item_id, outdir, base_url):
    outdir = Path(outdir)
    # BUI-300: a re-run of this label (e.g. a listing that now has fewer
    # images than a prior attempt) must not leave higher-numbered
    # img-NN.jpg files behind — mkdir(exist_ok=True) alone never clears an
    # existing dir, so a stale image from a previous listing could leak
    # into the grade. Wipe it before writing this listing's images.
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # BUI-283: fetch_item()/fetch_item_with_status() return None on failure
    # (already logged detail to stderr) rather than raising. Adapt that back
    # to a raised RuntimeError so the FETCH FAILED contract below (BUI-147: a
    # fetch failure is NOT zero images) stays intact — without this, a None
    # result would need its own empty-image handling and would be
    # indistinguishable from a genuinely photo-less listing.
    # BUI-310: a 401 specifically raises TokenExpiredError so main() can
    # refresh the token and retry once before giving up.
    data, status = fetch_item_with_status(item_id, token, base_url)
    if data is None:
        if status == 401:
            raise TokenExpiredError(f"item {item_id}: fetch failed (401 unauthorized)")
        raise RuntimeError(f"item {item_id}: fetch failed (see stderr for detail)")
    # BUI-300: a malformed Browse-API response (e.g. missing imageUrl) must
    # fail only this item via the same FETCH FAILED contract as a None
    # fetch_item() result above — not raise a bare KeyError that aborts the
    # whole batch.
    try:
        imgs = []
        if "image" in data:
            imgs.append(data["image"]["imageUrl"])
        for ai in data.get("additionalImages", []):
            imgs.append(ai["imageUrl"])
    except KeyError as e:
        raise RuntimeError(f"item {item_id}: malformed image data (missing {e})") from e
    for i, url in enumerate(imgs, 1):
        try:
            _download_image(url, outdir / f"img-{i:02d}.jpg")
        except requests.exceptions.RequestException as e:
            # Covers connection errors, HTTP error status, and a timed-out
            # host — a hung image host must fail only this item, not stall
            # the whole sequential batch.
            raise RuntimeError(f"item {item_id}: image download failed ({e})") from e
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
    parser.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Print the installed version and the git SHA/date it was built "
             "from, then exit. Use this to check for a stale `uv tool install` "
             "(see scripts/install.sh).",
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
        # BUI-310: a long batch can outlive the token fetched above. Two
        # attempts max — on a 401, force-refresh the token once (keeping it
        # for the rest of the batch) and retry this item; any other failure,
        # or a second straight 401, is genuine and falls through to FETCH
        # FAILED (BUI-147: a fetch failure is NOT zero images).
        result = None
        for attempt in range(2):
            try:
                result = download_listing(token, item_id, f"{args.workdir}/{label}", base_url)
                break
            except TokenExpiredError as e:
                if attempt == 0:
                    # BUI-310: get_token() calls sys.exit(1) on a hard auth
                    # failure (bad/revoked credentials, or transient errors that
                    # outlast its own retry budget). SystemExit is a
                    # BaseException, so it would sail past the except clauses
                    # here and abort the whole process mid-batch — silently
                    # dropping the FETCH FAILED lines for every remaining item
                    # and breaking grade.md Step 1's per-line contract. Catch it
                    # and degrade to this item's FETCH FAILED, preserving the
                    # BUI-147/BUI-300 invariant (one item's failure never aborts
                    # the batch).
                    try:
                        token = get_token(client_id, client_secret, base_url, force_refresh=True)
                    except SystemExit:
                        print(f"{label}: FETCH FAILED — token refresh failed after 401 (see stderr)")
                        break
                    continue
                print(f"{label}: FETCH FAILED — {e}")
                break
            except RuntimeError as e:
                print(f"{label}: FETCH FAILED — {e}")
                break
        if result is None:
            continue
        price = result["current_price"]
        price_str = f"${price:.2f}" if price is not None else "n/a"
        print(f"{label}: {result['title']} — {result['image_count']} images — current bid {price_str} ({result['bid_count']} bids)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
