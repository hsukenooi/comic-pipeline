#!/usr/bin/env python3
"""seller-scan: Match an eBay seller's active listings against your LOCG wish list."""

import argparse
import json
import re
import subprocess
import sys

from ebay_fetch import load_config, get_token, parse_item_summary, search_seller_listings


# ─── Wish list fetching ───────────────────────────────────────────────────────

def fetch_wish_list():
    """Fetch LOCG wish list via locg CLI. Returns list of dicts with id and name."""
    result = subprocess.run(
        ["python3", "-m", "locg", "wish-list"],
        cwd="/Users/hsukenooi/Projects/locg-cli",
        env={**__import__("os").environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error fetching wish list: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Error parsing wish list JSON: {e}", file=sys.stderr)
        sys.exit(1)


# ─── Matching ─────────────────────────────────────────────────────────────────

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "in", "vol", "comics"})


def _normalize(text):
    """Lowercase and strip non-alphanumeric characters."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


def _series_tokens(series):
    """Return significant tokens from a series name."""
    return [t for t in _normalize(series).split() if len(t) >= 2 and t not in _STOPWORDS]


def _parse_wish_name(name):
    """Parse 'Series Name #N' into (series, issue_number) or (name, None)."""
    m = re.match(r"^(.*?)\s*#(\d+\w*)\s*$", name.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return name.strip(), None


def prepare_wish_items(wish_list):
    """Augment wish list items with parsed series + issue for matching."""
    out = []
    for item in wish_list:
        name = item.get("name", "")
        series, issue = _parse_wish_name(name)
        tokens = _series_tokens(series)
        if not tokens or issue is None:
            continue
        out.append({
            "id": item.get("id"),
            "name": name,
            "series": series,
            "issue": issue,
            "_tokens": tokens,
        })
    return out


def match_listing(title, wish_items):
    """Return (best_wish_item, score) or (None, 0.0) for an eBay listing title.

    Requires:
    - Issue number present in title as #N or as isolated digits
    - At least 50% of series tokens present in title
    """
    title_norm = _normalize(title)
    best = None
    best_score = 0.0

    for wish in wish_items:
        issue = wish["issue"]
        # Issue number check: look for #N or space-bounded N
        issue_pattern = re.compile(
            r"(?:#\s*" + re.escape(issue) + r"|\b" + re.escape(issue) + r"\b)"
        )
        if not issue_pattern.search(title_norm):
            continue

        tokens = wish["_tokens"]
        title_words = set(title_norm.split())
        matched = sum(1 for t in tokens if t in title_words)
        score = matched / len(tokens)

        if score > best_score:
            best_score = score
            best = wish

    if best_score >= 0.65:
        return best, best_score
    return None, 0.0


# ─── Output ───────────────────────────────────────────────────────────────────

def _trunc(text, width):
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def print_matches(matches):
    """Print match results as a human-readable table."""
    if not matches:
        print("No matches found.")
        return

    cols = [
        ("Listing Title", "title", 40),
        ("Wish List Item", "wish_name", 28),
        ("Price", "current_price", 9),
        ("Ends", "end_date", 12),
        ("URL", "listing_url", 45),
    ]

    header = "  ".join(h.ljust(w) for h, _, w in cols)
    print(header)
    print("-" * len(header))

    for m in matches:
        row = "  ".join(
            _trunc(str(m.get(k) or ""), w).ljust(w)
            for _, k, w in cols
        )
        print(row)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="seller-scan",
        description="Match an eBay seller's listings against your LOCG wish list.",
    )
    parser.add_argument(
        "seller",
        help="eBay seller username or store URL",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output matches as JSON array",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1000,
        help="Maximum listings to fetch from seller (default: 1000)",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )
    args = parser.parse_args(argv)

    # Auth
    client_id, client_secret, base_url = load_config()
    if args.env:
        from ebay_fetch import PRODUCTION_BASE, SANDBOX_BASE
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
    token = get_token(client_id, client_secret, base_url)

    # Fetch wish list
    print("Fetching LOCG wish list...", file=sys.stderr)
    wish_list = fetch_wish_list()
    wish_items = prepare_wish_items(wish_list)
    print(f"  {len(wish_items)} matchable wish list items", file=sys.stderr)

    # Fetch seller listings
    print(f"Fetching listings for seller '{args.seller}'...", file=sys.stderr)
    raw_listings = search_seller_listings(
        args.seller, token, base_url, max_results=args.max_results
    )
    print(f"  {len(raw_listings)} listings fetched", file=sys.stderr)

    # Match
    matches = []
    for raw in raw_listings:
        listing = parse_item_summary(raw)
        if "cgc" in listing["title"].lower():
            continue
        wish, score = match_listing(listing["title"], wish_items)
        if wish:
            matches.append({
                **listing,
                "wish_id": wish["id"],
                "wish_name": wish["name"],
                "match_score": round(score, 2),
            })

    print(f"  {len(matches)} match(es) found", file=sys.stderr)

    if args.json_output:
        print(json.dumps(matches, indent=2))
    else:
        print_matches(matches)

    return 0


if __name__ == "__main__":
    sys.exit(main())
