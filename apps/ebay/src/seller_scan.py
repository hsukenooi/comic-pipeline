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

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "in", "vol", "comics"})

# Edition words that signal a special printing type.  If a listing title
# contains one but the wish-item series does NOT, the listing is an obvious
# non-match and can be rejected before fuzzy scoring (hard_reject rule 2).
# Giant[-\s]Size and King[-\s]Size match both hyphenated and spaced forms.
_EDITION_PATTERNS = [
    re.compile(r"\bannual\b", re.IGNORECASE),
    re.compile(r"\bgiant[\s-]size\b", re.IGNORECASE),
    re.compile(r"\bking[\s-]size\b", re.IGNORECASE),
    re.compile(r"\bspecial\b", re.IGNORECASE),
    re.compile(r"\btreasury\b", re.IGNORECASE),
]

# Multi-comic-lot signals.  Any match → listing is a bundle, not a single issue.
_LOT_RE = re.compile(
    r"\blot\s+of\b"           # "lot of"
    r"|\b\d+\s+lot\b"         # "5 lot", "10-comic lot"
    r"|\blot\s+\d+"           # "lot 5", "lot 10"
    r"|\bcollection\b"         # "complete collection", "run collection"
    r"|\bcomplete\s+run\b"     # "complete run"
    r"|\bset\s+of\b"          # "set of 10"
    r"|#?\d+\s*-\s*#?\d+",    # issue range: "#1-#10" or "1-10"
    re.IGNORECASE,
)

# BUI-135: numeric grade tokens (CGC/CBCS slab grades and raw grade shorthand)
# look like "7.0", "8.5", "9.2", "9.4". _normalize() replaces the "." with a
# space, orphaning the integer part ("7.0" -> "7 0"); match_listing's bare-\bN\b
# issue branch then matched that orphaned "7" as wish issue #7, producing
# false positives at score 1.00 (seller comichunterlv: 61 of 62 false). Strip
# the whole decimal-grade token *before* normalizing so neither half survives.
# This catches the raw forms too ("F/VF 7.0", "VF/NM 9.0") which the literal
# "cgc"-skip guard in main() does NOT — they carry no "cgc" string.
_GRADE_RE = re.compile(r"\b\d{1,2}\.\d\b")

# BUI-135 (code-review follow-up): a grade written WITHOUT a decimal still
# orphans into a fake issue number ("VF/NM 9" -> bare "9" matched wish #9).
# Strip a bare integer ONLY when it's prefixed by a grade-letter token
# (VF, NM, FN, GD, VG, F, G, optionally combined like "F/VF" or "NM+"), so we
# kill grade-shorthand digits without touching a plain "\bN\b" that has no
# grade word in front of it (e.g. "X-Men 9" or "#9" must still match issue 9 —
# the matcher's loose bias is preserved).
_GRADE_LETTER_RE = re.compile(
    r"\b(?:VF|NM|FN|GD|VG|F|G)(?:[/+-]*(?:VF|NM|FN|GD|VG|F|G))*[/+-]*\s*\d{1,2}\b",
    re.IGNORECASE,
)


def _strip_grades(text):
    """Remove grade tokens so their digits can't be mistaken for an issue
    number. Covers decimal grades ('7.0', '9.4') and grade-letter-prefixed
    bare integers ('VF 9', 'VF/NM 9', 'NM 8', 'FN+ 6'). A plain bare integer
    with no grade-letter prefix is deliberately left alone so a real issue
    number ('X-Men 9', '#9') still matches. See BUI-135."""
    text = _GRADE_RE.sub(" ", text)
    text = _GRADE_LETTER_RE.sub(" ", text)
    return text


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


def hard_reject(title, series, issue):
    """Return True if the listing title is an obvious non-match for this wish item.

    Conservative pre-filter: only drops clear mismatches — never rejects a
    genuine single-issue copy.  Callers (e.g. the wishlist-sellers funnel)
    should call this before match_listing to shrink the candidate pool cheaply.

    Rules applied in order:
      1. CGC slab — "cgc" in title.  This scan is raw/ungraded only; mirrors
         the existing ``if "cgc" in ...`` skip in main() so callers using
         hard_reject get that guard for free.
      2. Edition mismatch — title contains Annual / Giant-Size / Giant Size /
         King-Size / King Size / Special / Treasury (word-boundary,
         case-insensitive) but the wish-item ``series`` does NOT contain that
         same word.  Example: "Avengers Annual #1" is rejected for a wish item
         whose series is "The Avengers", but kept for "Avengers Annual".
      3. Multi-comic lot — title matches a lot/collection/complete-run/set-of
         or issue-range pattern (e.g. "#1–#10").
      4. Missing issue number — if ``issue`` is not None, the normalised title
         must contain it as a bounded token (#N or bare N), using the same
         word-boundary regex as match_listing so the two are consistent.
    """
    # Rule 1: CGC slab
    if "cgc" in title.lower():
        return True

    # Rule 2: edition mismatch
    for pat in _EDITION_PATTERNS:
        if pat.search(title) and not pat.search(series):
            return True

    # Rule 3: multi-comic lot
    if _LOT_RE.search(title):
        return True

    # Rule 4: missing issue number (same logic as match_listing)
    if issue is not None:
        title_norm = _normalize(_strip_grades(title))
        issue_pat = re.compile(
            r"(?:#\s*" + re.escape(issue) + r"\b|\b" + re.escape(issue) + r"\b)"
        )
        if not issue_pat.search(title_norm):
            return True

    return False


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

    _load_dotenv(Path(__file__).parent.parent / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Error: ANTHROPIC_API_KEY is not set (checked the environment and "
            "apps/ebay/.env). Claude verification cannot run — refusing to "
            "surface unverified matches. Export ANTHROPIC_API_KEY or add it to "
            "apps/ebay/.env, then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    client = anthropic.Anthropic()

    kept = []
    for chunk_start in range(0, len(matches), _VERIFY_CHUNK_SIZE):
        chunk = matches[chunk_start : chunk_start + _VERIFY_CHUNK_SIZE]
        chunk_label = f"{chunk_start + 1}–{chunk_start + len(chunk)}"

        pairs = "\n".join(
            f'{idx}. Listing: "{cand["title"]}"\n   Wish item: "{cand["wish_name"]}"'
            for idx, cand in enumerate(chunk, 1)
        )
        prompt = f"""You are a comic book expert. For each listing/wish-item pair, decide if the listing is a genuine match — same series, same issue number, same edition type.

Reject if:
- Different series sharing words (Spider-Man Noir vs Amazing Spider-Man, X-Factor vs X-Men, Superior/Ultimate Spider-Man vs Amazing Spider-Man)
- Annual, Giant-Size, or special edition matching a regular series issue (and vice versa)
- Lot listing where the issue number appears in the lot size
- Promotional reprint (Trick or Read, LCSD, Amazon promo, Undeluxe)
- Modern renumbered issue matching an original issue number (e.g. #10 (811))
- Series name only in a subtitle or story description, not the actual series

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
        if not listing.get("title"):
            continue
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
