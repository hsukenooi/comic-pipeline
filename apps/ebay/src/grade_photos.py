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

BUI-322: BUI-310's per-item self-healing has no memory across items, so a
systemic/permanent 401 (revoked creds, an app-level API restriction) was
masked as ordinary per-item self-healing — every remaining item burned its
own force-refresh OAuth POST before FETCH FAILED, hammering the OAuth
endpoint for the whole batch instead of surfacing that the failure isn't
per-item at all. main() now tracks `consecutive_post_refresh_401s` across
items: a 401 that survives a force-refresh retry (a "post-refresh 401")
increments it; a second straight one is corroborating evidence the refresh
isn't helping, so main() sets `give_up_refreshing` and stops issuing
force-refresh OAuth POSTs for the rest of the batch — remaining items still
get a FETCH FAILED line (grade.md's per-line contract holds), just without
paying for a refresh that's already proven useless. A refresh that raises
SystemExit (get_token's own hard-failure exit) counts toward that SAME
two-in-a-row threshold rather than latching immediately on one occurrence —
get_token's sys.exit(1) fires on bad/revoked credentials but ALSO on a
network error/429/5xx that merely outlasted its own retry budget, so one
SystemExit alone isn't reliable proof of a permanent failure (caught in
review: an earlier draft gave up after a single SystemExit, which would
have let one transient network blip during a refresh starve the rest of an
otherwise-healthy batch). Any successful fetch — including a 401 that DOES
self-heal after refresh — resets the counter to 0, so an isolated/transient
expiry still self-heals exactly as BUI-310 intended.

BUI-331: the give-up counter's reset is now "since last confirmed-healthy
auth", not strictly "since last full success". A failure on a fetch that
reached a real HTTP-200 — a 200 body that didn't parse, a parsed-but-malformed
body, or an image-CDN download error — raises `ListingContentError` (a
RuntimeError subclass), and main() resets `consecutive_post_refresh_401s` on
it: the token demonstrably worked, so two post-refresh 401s separated by such a
failure are not "consecutive". Crucially a `data is None` fetch failure with a
non-200 status (a plain 500/404/network exhaustion) is NOT proof of 200 and
does NOT reset — so a genuine revoked-creds streak still counts toward give-up
and can't be kept alive forever by an unrelated non-auth failure defeating the
circuit breaker. (A revoked-creds run never yields a 200, so no proof-of-200
reset can launder its streak.)

Usage:
    python src/grade_photos.py ITEM_ID [ITEM_ID ...] [--workdir DIR]

Labels each item comic-1, comic-2, ... in input order (matches the
<workdir>/comic-N/ layout the grader agents expect). Prints one line per
item:

    comic-1: FETCH FAILED — <error>
    comic-1: <title> — <N> images — current bid $12.34 (3 bids) — tier: cheap

BUI-511: the trailing `tier: cheap|not-cheap` is this script's value-gate
verdict (see VALUE_THRESHOLD below) — cheap when current_price is below the
threshold or unknown, not-cheap when at/above it. grade.md's Step 2 reads
this field directly rather than re-deriving the split from current_price.

BUI-440: when --workdir is not given, each run gets its own fresh directory
under /tmp/comic-grading (via tempfile.mkdtemp) instead of writing straight
into that fixed root. Previously the root was never cleared and only the
per-label comic-N dir was wiped (BUI-300, download_listing() below) — so a
prior larger run's comic-3..comic-N dirs survived into a later smaller run,
and two overlapping runs (e.g. a /comic:buy run + a standalone /comic:grade
run) collided on comic-1/comic-2 and could rmtree each other's in-flight
images. main() prints the resolved directory as `WORKDIR: <path>` (only
when auto-generated — an explicit --workdir is echoed by the caller, not by
this script) so grade.md's Step 2 can address each comic's images at
<workdir>/comic-N.
"""

import argparse
import importlib.metadata
import shutil
import sys
import tempfile
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

# BUI-440: parent directory for auto-generated (--workdir not given) run
# directories. It is only ever mkdir'd and used as the `dir=` argument to
# tempfile.mkdtemp() — never written into or rmtree'd directly — so it is
# safe for it to accumulate across runs. A module-level constant (rather than
# inlined in main()) so tests can monkeypatch it to a tmp_path instead of
# touching the real /tmp.
_DEFAULT_WORKDIR_ROOT = Path("/tmp/comic-grading")

# BUI-511: this script is the single owner of the value-gate threshold.
# grade.md's Step 2 used to re-derive the cheap/not-cheap split from prose
# comparing `current_price` against a `VALUE_THRESHOLD` restated in the doc;
# now main() prints the tier directly (see the per-comic line below) and
# grade.md just reads it. cheap = current_price below this, OR unknown
# (current_price is None); not-cheap = current_price at/above this. Keep this
# in sync with grade.md's escalation value trigger if it ever changes.
VALUE_THRESHOLD = 25.0


class TokenExpiredError(RuntimeError):
    """Raised by download_listing() when fetch_item_with_status() returns 401.

    BUI-310: a subclass of RuntimeError (not a sibling exception type) so any
    caller that only knows about the pre-existing FETCH FAILED contract still
    catches it via `except RuntimeError`; main() adds a more specific
    `except TokenExpiredError` first to get one refresh-and-retry before
    falling back to that same FETCH FAILED handling.
    """


class ListingContentError(RuntimeError):
    """Raised by download_listing() for a failure on a fetch that is known to
    have reached a real HTTP-200 — i.e. the auth token demonstrably worked.
    Three cases, all keyed on an observed 200: a 200 body that did not parse
    (`fetch_item_with_status()` returns `(None, 200)`), a parsed-but-malformed
    body (missing imageUrl), or an image-CDN download error (the item body was
    already fetched over a 200; only the CDN failed).

    BUI-331: the systemic-401 give-up counter in main() must reset on any
    confirmed-healthy auth, not only on a full item success. The invariant is
    "raised only when a 200 was observed": the parsed-body / image-download
    paths run only after `fetch_item_with_status()` returned a non-None body
    (its contract guarantees that means status 200), and the unparseable-body
    path is raised explicitly under `status == 200`. A subclass of RuntimeError
    (like TokenExpiredError) so the existing `except RuntimeError` FETCH FAILED
    contract still catches it; main() adds a more specific
    `except ListingContentError` first that resets the counter.

    Deliberately NOT raised on a `data is None` fetch failure with a non-200
    status (a plain 500, 404, 401-already-handled, or network-budget
    exhaustion): that path has no proof the auth layer is healthy, so it must
    NOT reset a genuine systemic-401 streak — resetting there is exactly the
    over-eager reset that would let a truly-broken-auth batch keep hammering
    the OAuth endpoint forever. Because a revoked-creds run never yields a 200,
    no ListingContentError case can launder a broken-auth streak.
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
        # BUI-331: a 200 whose body did not parse (WAF interstitial / truncated
        # proxy response — fetch_item_with_status returns (None, 200)) is still
        # proof the token worked: eBay accepted it and returned 200. Surface it
        # as a proof-of-200 content failure so main() resets the systemic-401
        # counter, exactly like the malformed-dict / image-CDN paths below.
        # Only a non-200 fetch failure (below) is uninformative about auth
        # health and must leave the give-up streak counting.
        if status == 200:
            raise ListingContentError(f"item {item_id}: malformed 200 response (unparseable body)")
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
        # BUI-331: proof-of-200 failure (body already fetched) — ListingContentError.
        raise ListingContentError(f"item {item_id}: malformed image data (missing {e})") from e
    for i, url in enumerate(imgs, 1):
        try:
            _download_image(url, outdir / f"img-{i:02d}.jpg")
        except requests.exceptions.RequestException as e:
            # Covers connection errors, HTTP error status, and a timed-out
            # host — a hung image host must fail only this item, not stall
            # the whole sequential batch.
            # BUI-331: proof-of-200 failure (item body already fetched over a
            # successful 200; only the image CDN failed) — ListingContentError.
            raise ListingContentError(f"item {item_id}: image download failed ({e})") from e
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
        "--workdir", default=None,
        help="Output root; each item lands in <workdir>/comic-N/. Default: a "
             "fresh per-run directory created under /tmp/comic-grading "
             "(BUI-440) — printed as `WORKDIR: <path>` on stdout so a caller "
             "that didn't pass --workdir can discover it.",
    )
    args = parser.parse_args(argv)

    # BUI-440: an explicit --workdir is used as-is (the caller owns that path
    # and is responsible for not colliding with another run). When omitted,
    # namespace this run under its own tempfile.mkdtemp() directory rather
    # than the bare fixed root — that's what actually stops a prior run's
    # stale comic-N dirs from leaking into this one AND stops two concurrent
    # runs from ever sharing a root (each gets a distinct, unpredictable
    # directory name). Just clearing the fixed root at batch start would not
    # be safe for the concurrent case: two runs starting close together could
    # each clear the other's in-flight files.
    workdir = args.workdir
    if workdir is None:
        _DEFAULT_WORKDIR_ROOT.mkdir(parents=True, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix="run-", dir=_DEFAULT_WORKDIR_ROOT)
        print(f"WORKDIR: {workdir}")

    items = [(f"comic-{i}", item_id) for i, item_id in enumerate(args.item_ids, 1)]
    client_id, client_secret, base_url = load_config()
    # BUI-283: reuse ebay_fetch's cached get_token() (writes token_cache_{env}.json,
    # 5-min buffer) instead of a fresh OAuth call every run — see module docstring.
    token = get_token(client_id, client_secret, base_url)
    # BUI-322: cross-item state so a systemic/permanent 401 (revoked creds)
    # fails the batch fast instead of every remaining item burning its own
    # force-refresh OAuth POST. `consecutive_post_refresh_401s` counts 401s
    # that survived a force-refresh retry (i.e. the refresh did NOT help);
    # two in a row is corroborating evidence the failure isn't per-item, so
    # `give_up_refreshing` latches on and main() stops POSTing refreshes for
    # the rest of the batch — see module docstring for the full rationale.
    consecutive_post_refresh_401s = 0
    give_up_refreshing = False
    for label, item_id in items:
        # BUI-310: a long batch can outlive the token fetched above. Two
        # attempts max — on a 401, force-refresh the token once (keeping it
        # for the rest of the batch) and retry this item; any other failure,
        # or a second straight 401, is genuine and falls through to FETCH
        # FAILED (BUI-147: a fetch failure is NOT zero images).
        result = None
        for attempt in range(2):
            try:
                result = download_listing(token, item_id, f"{workdir}/{label}", base_url)
                break
            except TokenExpiredError as e:
                if attempt == 0:
                    if give_up_refreshing:
                        # BUI-322: a prior item already proved refreshing
                        # doesn't help. Don't POST another refresh just to
                        # watch it fail again — fail this item fast too.
                        print(f"{label}: FETCH FAILED — {e} (refresh already failed earlier this batch)")
                        break
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
                        # BUI-322 (post-review correction): get_token()'s
                        # sys.exit(1) fires on THREE different conditions —
                        # bad/revoked credentials, a network error/429/5xx
                        # that outlasted its own retry budget, or a malformed
                        # token body — and only the first is truly permanent.
                        # Treating every SystemExit as immediate, one-shot
                        # proof of a systemic failure would let a single
                        # transient network blip during one item's refresh
                        # permanently starve the rest of the batch of ever
                        # refreshing again. So this counts toward the SAME
                        # "two consecutive" threshold as a post-refresh 401
                        # below, rather than latching give_up_refreshing on
                        # its own after just one occurrence.
                        consecutive_post_refresh_401s += 1
                        if consecutive_post_refresh_401s >= 2:
                            give_up_refreshing = True
                        print(f"{label}: FETCH FAILED — token refresh failed after 401 (see stderr)")
                        break
                    continue
                # BUI-322: this is a "post-refresh 401" — attempt 0 got a 401,
                # we force-refreshed, and attempt 1 (this one) still got a
                # 401. One of these is inconclusive (could be a fluke); two
                # in a row means the refresh isn't fixing anything, so stop
                # spending an OAuth POST on every remaining item.
                consecutive_post_refresh_401s += 1
                if consecutive_post_refresh_401s >= 2:
                    give_up_refreshing = True
                print(f"{label}: FETCH FAILED — {e}")
                break
            except ListingContentError as e:
                # BUI-331: this item's fetch reached a real HTTP-200 (an
                # unparseable 200 body, a malformed parsed body, or an image-CDN
                # error after the body was fetched) — the auth token
                # demonstrably worked. That is the same quality of evidence as a
                # full success, so reset the systemic-401 streak: two
                # post-refresh 401s separated by one of these are NOT
                # "consecutive" (the give-up counter's semantics are 'since last
                # confirmed-healthy auth', which this is). A `data is None`
                # non-200 fetch failure (plain RuntimeError below) is NOT proof
                # of 200, so it deliberately does NOT reset — keeping a genuine
                # revoked-creds streak counting toward give-up rather than
                # letting an unrelated 500 defeat the circuit breaker.
                consecutive_post_refresh_401s = 0
                print(f"{label}: FETCH FAILED — {e}")
                break
            except RuntimeError as e:
                print(f"{label}: FETCH FAILED — {e}")
                break
        if result is None:
            continue
        # BUI-322: any successful fetch — including a 401 that DID self-heal
        # after the force-refresh above — proves the token (or a fresh one)
        # works, so the streak of unhelpful refreshes is over. Reset the
        # counter so a later isolated/transient 401 still gets its own
        # refresh-and-retry per BUI-310, rather than inheriting a stale count
        # from unrelated earlier failures.
        consecutive_post_refresh_401s = 0
        price = result["current_price"]
        price_str = f"${price:.2f}" if price is not None else "n/a"
        # BUI-511: cheap = below VALUE_THRESHOLD or unknown price; not-cheap =
        # at/above it. Printed so grade.md's Step 2 value gate reads the tier
        # directly instead of re-deriving the split from current_price itself.
        tier = "cheap" if price is None or price < VALUE_THRESHOLD else "not-cheap"
        print(
            f"{label}: {result['title']} — {result['image_count']} images — "
            f"current bid {price_str} ({result['bid_count']} bids) — tier: {tier}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
