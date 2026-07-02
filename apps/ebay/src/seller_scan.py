#!/usr/bin/env python3
"""seller-scan: Match an eBay seller's active listings against your LOCG wish list."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import anthropic
import requests

from comic_identity import (  # noqa: F401 — BUI-253 Step 1: re-exported for callers
    _digital_reject,
    _EDITION_PATTERNS,
    _foreign_edition_reject,
    _LOT_MEMBER,
    _LOT_RE,
    _normalize,
    _reprint_reject,
    _second_print_reject,
    _series_tokens,
    _strip_grades,
    _title_paren_year,
    _title_paren_years,
    _title_volume,
    _trading_card_reject,
    era_mismatch,
    hard_reject,
    lot_count_mismatch,
    publication_year_mismatch,
    series_volume,
    series_year_range,
    should_reject,
)
from ebay_fetch import (
    UnknownSellerError,
    get_token,
    load_config,
    load_seller_aliases,
    parse_item_summary,
    resolve_seller_username,
    save_seller_alias,
    search_seller_listings,
)


# ─── Server URL resolution (BUI-220) ──────────────────────────────────────────

_DEPRECATION_WARNED = False


def _server_base():
    """Return the comics server base URL (trailing slash trimmed), or "".

    BUI-220: the canonical env var is COMICS_SERVER_URL; GIXEN_SERVER_URL is a
    deprecated alias still read as a fallback. Using only the old var emits a
    one-line deprecation warning to stderr (once).
    """
    global _DEPRECATION_WARNED
    base = os.environ.get("COMICS_SERVER_URL", "").rstrip("/")
    if base:
        return base
    legacy = os.environ.get("GIXEN_SERVER_URL", "").rstrip("/")
    if legacy and not _DEPRECATION_WARNED:
        print(
            "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
            file=sys.stderr,
        )
        _DEPRECATION_WARNED = True
    return legacy


# ─── Wish list fetching ───────────────────────────────────────────────────────

def fetch_wish_list():
    """Fetch the wish list from the gixen server API. Returns a list of
    {id, name} dicts.

    BUI-88 (R10): seller-scan lives in apps/ebay, which is NOT a uv workspace
    member and cannot import locg-cli — so it fetches the wish-list over HTTP
    from the server's /api/comics/wish-list endpoint instead of shelling out to
    the `locg` CLI. Fails loudly on any unreachable-server / non-200 / bad-JSON
    condition (never returns a partial or empty list silently) so a scan can't
    run against a stale or empty wish list because the server was down.
    """
    base = _server_base()
    if not base:
        print(
            "Error: COMICS_SERVER_URL is not set — cannot reach the wish-list API.\n"
            "Set it in ~/.zshrc (MacBook → http://mac-mini.tail9b7fa5.ts.net:8080; "
            "Mac Mini → http://localhost:8080).",
            file=sys.stderr,
        )
        sys.exit(1)
    url = f"{base}/api/comics/wish-list"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching wish list from {url}: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing wish list JSON from {url}: {e}", file=sys.stderr)
        sys.exit(1)


# ─── Seen-tracking (BUI-113) ──────────────────────────────────────────────────

def fetch_seen_item_ids(seller):
    """Best-effort: return the set of item_ids already surfaced in a prior scan.

    BUI-113: unlike fetch_wish_list (which hard-fails — an empty wish-list would
    cause a wrong "no match"), seen-tracking is non-fatal. A failed read only
    risks re-showing a listing you've seen (mildly annoying, always safe);
    hiding everything because the server is down could silently suppress a real
    buy. So any failure → empty set + a warning, and the scan continues showing
    all matches.
    """
    base = _server_base()
    if not base:
        return set()
    url = f"{base}/api/comics/seller-scan/seen"
    try:
        resp = requests.get(url, params={"seller": seller}, timeout=10)
        resp.raise_for_status()
        return set(resp.json().get("item_ids", []))
    except (requests.exceptions.RequestException, ValueError) as e:
        print(
            f"Warning: could not fetch seen item IDs ({e}); showing all matches",
            file=sys.stderr,
        )
        return set()


def record_items_seen(item_ids, seller):
    """Best-effort: mark surfaced item_ids as seen so future scans skip them.

    Warns on failure but never aborts (see fetch_seen_item_ids for the rationale).
    """
    base = _server_base()
    if not base or not item_ids:
        return
    url = f"{base}/api/comics/seller-scan/seen"
    try:
        resp = requests.post(
            url, json={"item_ids": item_ids, "seller": seller}, timeout=10
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Warning: could not record seen item IDs ({e})", file=sys.stderr)


# ─── Matching ─────────────────────────────────────────────────────────────────
# BUI-253 Step 1: the deterministic title-parsing / reject helpers that used to
# live here (grade-stripping, lot detection, edition patterns, era/volume
# disambiguation, the reject lexicons, hard_reject, should_reject) now live in
# comic_identity.py and are imported above — this module keeps the wish-item
# prep, the fuzzy scorer, the LLM verify layer, and the CLI.

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
        # BUI-226: carry raw LOCG series_name (decorated, e.g.
        # "The Amazing Spider-Man (Vol. 1) (1963 - 1998)") and release year
        # for era-gate disambiguation.  _series_name may be None for ~9% of
        # local-only items that were added without a Metron round-trip.
        release_date = item.get("release_date") or ""
        out.append({
            "id": item.get("id"),
            "name": name,
            "series": series,
            "issue": issue,
            "_tokens": tokens,
            "_series_name": item.get("series_name"),
            "_release_year": release_date[:4] or None,
        })
    return out


def match_listing(title, wish_items):
    """Return (best_wish_item, score) or (None, 0.0) for an eBay listing title.

    Requires:
    - Issue number present in title as #N or as isolated digits
    - At least 50% of series tokens present in title
    """
    # BUI-135: strip decimal grade tokens before normalizing so a slab/raw grade
    # like "7.0" or "9.4" can't orphan its integer into a fake issue-number match.
    title_norm = _normalize(_strip_grades(title))
    best = None
    best_score = 0.0

    for wish in wish_items:
        issue = wish["issue"]
        # Issue number check: look for #N or space-bounded N.
        # BUI-184: add a trailing \b to the #\s*N branch so that "#3" does not
        # prefix-match "#300". In practice _normalize() strips "#" before this
        # runs (so the first branch rarely fires on a normalized title), but the
        # trailing boundary hardens it defensively against any future caller that
        # passes un-normalized input. Both branches now have symmetric boundaries.
        issue_pattern = re.compile(
            r"(?:#\s*" + re.escape(issue) + r"\b|\b" + re.escape(issue) + r"\b)"
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


# Maximum candidates per Claude API call.  At ~30–50 output tokens/verdict,
# the 8 096-token cap limits a single call to ~150–270 candidates before
# silent truncation; 100 keeps a comfortable margin and caps cost per call.
_VERIFY_CHUNK_SIZE = 100


def verify_with_claude(matches):
    """Filter candidates to genuine matches using chunked Claude API calls.

    Candidates are processed in batches of at most _VERIFY_CHUNK_SIZE per call
    so that a large run never silently truncates the response.  Indices in each
    prompt are 1-based and local to the chunk so the correlation logic is
    identical to the single-call version.

    Fail-closed: if a chunk's response cannot be parsed OR the number of
    returned verdicts does not match the number of candidates sent, that chunk
    is REJECTED entirely (those candidates are dropped) and a warning is
    printed to stderr.  Better to miss a borderline listing than to surface
    false positives unattended.  This replaces the previous fail-open fallback
    that returned all candidates on a parse error.
    """
    if not matches:
        return []

    # BUI-241: try .env candidates in order until the key lands in the environment.
    # (1) file-relative path — works under --editable install (primary, Option A fix)
    # (2) $COMIC_PIPELINE_ENV — an explicit full path to a .env file, if set
    # (3) stable user location — ~/.config/comic-pipeline/.env (Option B fallback)
    _dotenv_candidates = [
        Path(__file__).parent.parent / ".env",
        *(
            [Path(os.environ["COMIC_PIPELINE_ENV"])]
            if os.environ.get("COMIC_PIPELINE_ENV")
            else []
        ),
        Path.home() / ".config" / "comic-pipeline" / ".env",
    ]
    for _candidate in _dotenv_candidates:
        if os.environ.get("ANTHROPIC_API_KEY"):
            break
        _load_dotenv(_candidate)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _checked = ", ".join(str(p) for p in _dotenv_candidates)
        _env_hint = (
            " or set $COMIC_PIPELINE_ENV to a .env file path"
            if not os.environ.get("COMIC_PIPELINE_ENV")
            else ""
        )
        print(
            f"Error: ANTHROPIC_API_KEY is not set (checked the environment and "
            f"{_checked}). Claude verification cannot run — refusing to "
            f"surface unverified matches. Export ANTHROPIC_API_KEY, add it to "
            f"one of the files above{_env_hint}, then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    client = anthropic.Anthropic()

    kept = []
    for chunk_start in range(0, len(matches), _VERIFY_CHUNK_SIZE):
        chunk = matches[chunk_start : chunk_start + _VERIFY_CHUNK_SIZE]
        chunk_label = f"{chunk_start + 1}–{chunk_start + len(chunk)}"

        # Build pairs text.  When the candidate carries a decorated series name
        # (_series_name, stripped from output before printing — see the
        # underscore-key filter in main()), include it as a "Correct series:"
        # hint so Haiku knows the exact era the user wants.
        pairs_parts = []
        for idx, cand in enumerate(chunk, 1):
            pair_text = (
                f'{idx}. Listing: "{cand["title"]}"\n'
                f'   Wish item: "{cand["wish_name"]}"'
            )
            sn = cand.get("_series_name")
            if sn:
                pair_text += f"\n   Correct series: {sn}"
            pairs_parts.append(pair_text)
        pairs = "\n".join(pairs_parts)
        prompt = f"""You are a comic book expert. For each listing/wish-item pair, decide if the listing is a genuine match — same series, same issue number, same edition type.

Reject if:
- Different series sharing words (Spider-Man Noir vs Amazing Spider-Man, X-Factor vs X-Men, Superior/Ultimate Spider-Man vs Amazing Spider-Man)
- Annual, Giant-Size, or special edition matching a regular series issue (and vice versa)
- Lot listing where the issue number appears in the lot size
- Promotional reprint (Trick or Read, LCSD, Amazon promo, Undeluxe)
- Modern renumbered issue matching an original issue number (e.g. #10 (811))
- Series name only in a subtitle or story description, not the actual series
- Different series VOLUME / relaunch: if the 'Correct series' line shows a specific era, reject listings where 'vol N' or a (YYYY) indicates a different era than the one shown
- Foreign-language or foreign-market reprint/edition (e.g. Mexican La Prensa, Spanish/French/German/Italian edition) when the wish item is the original US edition
- Numbered sequential run or complete multi-issue set (e.g. "Books 1-4", "Issues 1 through 6", "complete set", "full run") when the wish item is a single specific issue
- Later printing / reprint of a key issue (e.g. "2nd print", "Second Printing", "Reprint") when the wish item means the original first print. Newsstand and Direct editions are NOT reprints — keep those

Respond with a JSON array containing ONLY the ids you are REJECTING, each with a brief reason:
[{{"id": 3, "reason": "X-Factor not X-Men"}}, {{"id": 7, "reason": "annual vs regular"}}]

If nothing is rejected, return [].

Any candidate id NOT present in your response is treated as genuine.

Pairs:
{pairs}"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        json_match = re.search(r"\[.*\]", text, re.DOTALL)
        if not json_match:
            print(
                f"Warning: could not parse Claude response for candidates "
                f"{chunk_label}; dropping chunk (fail-closed)",
                file=sys.stderr,
            )
            continue

        try:
            rejected_list = json.loads(json_match.group())
        except json.JSONDecodeError:
            print(
                f"Warning: invalid JSON in Claude response for candidates "
                f"{chunk_label}; dropping chunk (fail-closed)",
                file=sys.stderr,
            )
            continue

        # Validate: every returned object must have an int id strictly within
        # the chunk's 1-based range.  A missing key, non-int id, or out-of-range
        # id indicates a malformed response → reject the whole chunk (fail-closed).
        valid_ids = set(range(1, len(chunk) + 1))
        try:
            rejected_ids: dict[int, str] = {}
            for v in rejected_list:
                rid = v["id"]  # KeyError if missing
                if not isinstance(rid, int):
                    raise ValueError(f"non-int id: {rid!r}")
                if rid not in valid_ids:
                    raise ValueError(f"id {rid} out of range 1..{len(chunk)}")
                rejected_ids[rid] = v.get("reason", "")
        except (KeyError, ValueError, TypeError) as exc:
            print(
                f"Warning: invalid rejected-ids in Claude response for candidates "
                f"{chunk_label} ({exc}); dropping chunk (fail-closed)",
                file=sys.stderr,
            )
            continue

        # BUI-149: surface each rejected candidate (with the model's reason) to
        # stderr so the user can override if the verifier was wrong.
        if rejected_ids:
            print(
                f"Filtered {len(rejected_ids)} likely false positive(s) "
                f"(Claude verification):",
                file=sys.stderr,
            )
            for idx, cand in enumerate(chunk, 1):
                if idx in rejected_ids:
                    reason = rejected_ids[idx]
                    line = f"  - {cand.get('title', '?')}  ↮  {cand.get('wish_name', '?')}"
                    if reason:
                        line += f"  — {reason}"
                    print(line, file=sys.stderr)

        kept.extend(
            cand for idx, cand in enumerate(chunk, 1) if idx not in rejected_ids
        )

    return kept


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
        help="eBay store name (resolved via your alias map), a /usr/ or _ssn= URL, "
             "or use --username for a raw login username",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="eBay login username to scan directly, bypassing the alias map "
             "(for a one-off seller not yet in your aliases)",
    )
    parser.add_argument(
        "--add-alias",
        default=None,
        metavar="USERNAME",
        help="Register the given login USERNAME for the store name in the "
             "positional arg, persist it, then scan",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output matches as JSON array",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show every wish-list match, including ones surfaced in a prior "
             "scan. By default (BUI-113) already-seen matches are hidden. "
             "Newly-surfaced matches are recorded as seen either way.",
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

    # Resolve the store name to an eBay login username before anything else —
    # a store name is NOT a username and would silently scan every seller (BUI-68).
    if args.add_alias:
        save_seller_alias(args.seller, args.add_alias)
        print(
            f"Registered alias '{args.seller.strip().lower()}' → '{args.add_alias.strip()}'",
            file=sys.stderr,
        )
    aliases = load_seller_aliases()
    try:
        username = resolve_seller_username(
            args.seller, aliases, username_override=args.username
        )
    except UnknownSellerError as e:
        print(
            f"Error: unknown seller '{e.store}'. A store name is not an eBay "
            "login username, so the scan can't run safely.\n"
            "  Find the username: open one of the seller's listings, click "
            "'See other items', and copy the _ssn= value from the URL.\n"
            f"  Then either:\n"
            f"    seller-scan {e.store} --add-alias <username>   (saves it for next time)\n"
            f"    seller-scan {e.store} --username <username>     (one-off)",
            file=sys.stderr,
        )
        return 2

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
    print(f"Fetching listings for seller '{username}'...", file=sys.stderr)
    raw_listings = search_seller_listings(
        username, token, base_url, max_results=args.max_results
    )
    print(f"  {len(raw_listings)} listings fetched", file=sys.stderr)

    # Match
    seen_ids = set()
    candidates = []
    for raw in raw_listings:
        listing = parse_item_summary(raw)
        # BUI-184: defense in depth — parse_item_summary already coerces null
        # titles to "" via `or ""`, but skip explicitly here too so that a
        # title-less listing never reaches .lower() or match_listing at all.
        title = listing.get("title")
        if not title:
            continue
        if "cgc" in title.lower():
            continue
        if listing["item_id"] in seen_ids:
            continue
        seen_ids.add(listing["item_id"])
        wish, score = match_listing(title, wish_items)
        if not wish:
            continue
        # BUI-245: run the same deterministic reject chain wishlist_sellers uses
        # (hard_reject, era_mismatch, reprint/digital/trading-card/foreign-edition/
        # second-print) against the wish item match_listing resolved, so a
        # cross-series false positive (e.g. "Spectacular Spider-Man #15" vs wish
        # "The Amazing Spider-Man #15") is dropped here rather than reaching Haiku
        # with no era hint.
        if should_reject(
            title, wish["series"], wish["issue"],
            wish.get("_series_name"), wish.get("_release_year"),
        ):
            continue
        candidates.append({
            **listing,
            "wish_id": wish["id"],
            "wish_name": wish["name"],
            "match_score": round(score, 2),
            # Private fields (BUI-245): carry the decorated series name so
            # verify_with_claude's "Correct series:" era hint activates for this
            # candidate too. Stripped before output — see the json_output block.
            "_series_name": wish.get("_series_name"),
            "_release_year": wish.get("_release_year"),
        })

    # BUI-113: drop matches already surfaced in a prior scan (default). --all
    # skips the filter (and its server fetch); the short-circuit on an empty
    # candidate list skips the fetch/Claude/record entirely.
    if candidates and not args.show_all:
        seen = fetch_seen_item_ids(username)
        before = len(candidates)
        candidates = [c for c in candidates if c["item_id"] not in seen]
        hidden = before - len(candidates)
        if hidden:
            print(
                f"  {hidden} already-seen match(es) hidden (use --all to show)",
                file=sys.stderr,
            )

    if candidates:
        print(f"  {len(candidates)} candidate(s) — verifying with Claude...", file=sys.stderr)
        matches = verify_with_claude(candidates)
    else:
        matches = []
    print(f"  {len(matches)} genuine match(es) found", file=sys.stderr)

    # BUI-245: strip private pipeline fields (_series_name, _release_year) —
    # they exist only to feed verify_with_claude's era hint and were never
    # meant to reach the user-facing table/JSON output.
    matches = [{k: v for k, v in m.items() if not k.startswith("_")} for m in matches]

    if args.json_output:
        print(json.dumps(matches, indent=2))
    else:
        print_matches(matches)

    # BUI-113: record surfaced matches (best-effort) so future scans skip them.
    # Runs under --all too — --all means "show me everything again", not "forget".
    if matches:
        record_items_seen([m["item_id"] for m in matches], username)

    return 0


if __name__ == "__main__":
    sys.exit(main())
