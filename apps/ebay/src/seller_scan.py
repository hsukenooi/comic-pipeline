#!/usr/bin/env python3
"""seller-scan: Match an eBay seller's active listings against your LOCG wish list."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import anthropic

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


# ─── Claude verification ──────────────────────────────────────────────────────

def _load_dotenv(path):
    """Load key=value pairs from a .env file into os.environ (if not already set)."""
    import os
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip()
    except FileNotFoundError:
        pass


def verify_with_claude(matches):
    """Filter candidates to genuine matches using a single Claude API call."""
    if not matches:
        return []

    _load_dotenv(Path(__file__).parent.parent / ".env")
    client = anthropic.Anthropic()
    pairs = "\n".join(
        f'{i+1}. Listing: "{m["title"]}"\n   Wish item: "{m["wish_name"]}"'
        for i, m in enumerate(matches)
    )
    prompt = f"""You are a comic book expert. For each listing/wish-item pair, decide if the listing is a genuine match — same series, same issue number, same edition type.

Reject if:
- Different series sharing words (Spider-Man Noir vs Amazing Spider-Man, X-Factor vs X-Men, Superior/Ultimate Spider-Man vs Amazing Spider-Man)
- Annual, Giant-Size, or special edition matching a regular series issue (and vice versa)
- Lot listing where the issue number appears in the lot size
- Promotional reprint (Trick or Read, LCSD, Amazon promo, Undeluxe)
- Modern renumbered issue matching an original issue number (e.g. #10 (811))
- Series name only in a subtitle or story description, not the actual series

Respond with only a JSON array, one object per pair in order:
{{"id": 1, "genuine": true}}
or
{{"id": 1, "genuine": false, "reason": "brief reason"}}

Pairs:
{pairs}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        print("Warning: could not parse Claude response, returning all candidates", file=sys.stderr)
        return matches

    verdicts = json.loads(m.group())
    verdict_map = {v["id"]: v.get("genuine", False) for v in verdicts}
    return [m for i, m in enumerate(matches, 1) if verdict_map.get(i, False)]


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
    seen_ids = set()
    candidates = []
    for raw in raw_listings:
        listing = parse_item_summary(raw)
        if "cgc" in listing["title"].lower():
            continue
        if listing["item_id"] in seen_ids:
            continue
        seen_ids.add(listing["item_id"])
        wish, score = match_listing(listing["title"], wish_items)
        if wish:
            candidates.append({
                **listing,
                "wish_id": wish["id"],
                "wish_name": wish["name"],
                "match_score": round(score, 2),
            })

    print(f"  {len(candidates)} candidate(s) — verifying with Claude...", file=sys.stderr)
    matches = verify_with_claude(candidates)
    print(f"  {len(matches)} genuine match(es) found", file=sys.stderr)

    if args.json_output:
        print(json.dumps(matches, indent=2))
    else:
        print_matches(matches)

    return 0


if __name__ == "__main__":
    sys.exit(main())
