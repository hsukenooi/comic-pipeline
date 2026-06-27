#!/usr/bin/env python3
"""wishlist-sellers: Discover eBay sellers holding ≥2 wish-list books.

Scans eBay across all wish-list items, groups hits by seller, and surfaces
only sellers holding two or more un-owned, unseen, genuinely-matched copies
— combine-shipping candidates.

Pipeline (per the plan's diagram):
  1.  Fetch wish list (hard-fail on empty/unreachable — plan R1).
  2.  Obtain eBay OAuth token.
  3.  For each wish item: keyword-search eBay (with 7-day disk cache — R3).
  4.  Filter active listings — drop ended ones from cache (R3a).
  5.  Deterministic hard-reject + match_listing (R6/R7).
  6.  Dedup by (seller, item_id) (R5a/C3).
  7.  Drop already-seen via global seen set, seller=None (R11).
  8.  Drop owned via batch collection-check; 409 = hard-fail (R10/R11).
  9.  Group by seller; drop sellers with < 2 distinct matches (R12).
  10. Split by verdict cache; run Haiku verify on uncached survivors (R8/R9).
  11. Write new verdicts; re-apply ≥2 gate.
  12. Record all final item_ids as seen (global, seller=None — R11).
  13. Emit compact table (default) or --json (R15).

Coverage note (plan N3): wish items whose name contains no '#N' issue number
(GNs, HCs, TPBs like "Secret Wars HC") are silently skipped by
prepare_wish_items() — true scan coverage is therefore less than the raw
wish-list count.

Seen tracking (plan R11): uses seller=None so this tool shares the global seen
set with per-seller scans — a listing shown by either tool won't re-surface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import requests

import ebay_search_cache
from ebay_fetch import (
    PRODUCTION_BASE,
    SANDBOX_BASE,
    get_item_aspects,
    get_token,
    load_config,
    search_by_keyword,
)
from seller_scan import (
    _digital_reject,
    _normalize,
    _reprint_reject,
    _server_base,
    _strip_grades,
    _title_paren_years,
    _title_volume,
    era_mismatch,
    fetch_seen_item_ids,
    fetch_wish_list,
    hard_reject,
    match_listing,
    prepare_wish_items,
    publication_year_mismatch,
    record_items_seen,
    verify_with_claude,
)

# ─── Score floor ──────────────────────────────────────────────────────────────
# Mirror seller_scan.match_listing's internal 0.65 threshold; match_listing
# already returns (None, 0.0) below it, so this gate is belt-and-suspenders.
MATCH_SCORE_FLOOR: float = 0.65

# ─── Buying-options map ────────────────────────────────────────────────────────
# Maps the --buying-options CLI choice to the eBay Browse API filter value.
# "auction" is the default: we prefer to find live, time-pressured auctions
# rather than BIN listings, which are lower-urgency and searchable anytime.
_BUYING_OPTIONS_MAP: dict[str, str] = {
    "auction": "AUCTION",
    "bin":     "FIXED_PRICE",
    "all":     "AUCTION|FIXED_PRICE",
}

# ─── Fan-out guard (smoke-test finding) ────────────────────────────────────────
# A wish item whose series tokenizes to a single PURELY-NUMERIC token (e.g.
# "300", "52", "1985") produces a hopelessly broad eBay keyword search and a
# permissive match — one such item matched ~500 junk listings live. These are
# skipped from the automated fan-out and surfaced for manual checking instead.
# (No per-item match cap: a high count for a real series is variant-cover
# saturation, not noise, and is exactly where multi-book sellers live — the
# per-(seller, book) dedup + distinct-book ≥2 gate handle volume correctly.)
def is_degenerate_series(wish_item: dict) -> bool:
    """True if the wish item's series is too generic to fan out automatically.

    Trigger: the series reduces to a single purely-numeric token (the "300"
    class). Single short *alphabetic* tokens (e.g. X-Men → ["men"]) are left
    alone — the issue number constrains those to tight result sets in practice.
    """
    tokens = wish_item.get("_tokens") or []
    return len(tokens) == 1 and tokens[0].isdigit()

# ─── Title-key helper ─────────────────────────────────────────────────────────

def _title_key(title: str) -> str:
    """Stable cache key for a listing title.

    Strip grade tokens (decimal CGC grades and grade-letter+integer combos)
    then normalize to lowercase alphanumeric.  The same comic title from two
    different sellers, or a relisted item with a new item_id, produces the
    same key — enabling cross-seller and relist cache hits.
    """
    return _normalize(_strip_grades(title))


# ─── Pristine-match shortcut ──────────────────────────────────────────────────
# Conservative pre-verify shortcut: a score-1.0 listing whose title is exactly
# "full-series-words + issue + noise" can bypass Haiku entirely.  The false
# positives that Haiku catches arise from ambiguous titles — wrong series,
# wrong edition, cross-over sub-titles.  A title that begins with every
# series word in order and has only condition/era noise after the issue number
# has ~zero false-positive risk.

# Trailing tokens (post-issue) that indicate condition, grade, or printing
# style but NOT a different series or sub-title.  Only purely-alphabetic tokens
# need checking — bare numbers (years, copy counts) are always allowed.
# Two named constants so condition-noise and print/era-noise can be tuned
# independently without restructuring code.
_PRISTINE_COND_TOKENS: frozenset[str] = frozenset({
    # Grade abbreviations (after _normalize → all lowercase)
    "vf", "nm", "fn", "gd", "vg", "vgfn", "fnvf", "vfnm", "nmmt",
    # Grade words
    "fine", "near", "mint", "good",
    # Market / slab descriptors
    "raw", "key", "hot",
})

_PRISTINE_PRINT_TOKENS: frozenset[str] = frozenset({
    # Print / distribution / era noise
    "newsstand", "direct", "pence",
    "1st", "2nd", "3rd", "4th", "5th",
    "first", "second", "third",
    "print", "printing",
})

# Combined allow-list consulted by is_pristine_match for trailing tokens.
_PRISTINE_TRAILING_ALLOW: frozenset[str] = _PRISTINE_COND_TOKENS | _PRISTINE_PRINT_TOKENS


def is_pristine_match(match: dict) -> bool:
    """Return True only if this match is unambiguously a clean single-issue copy.

    A match is pristine iff ALL of:
      1. score == 1.0  (every series token matched).
      2. Exact-prefix shape: the normalized title begins with every word in the
         normalized series name, followed by the issue number — with no tokens
         interspersed — and any tokens that follow the issue are only "noise"
         (condition/grade abbreviations, pure numbers/years, printing/era tokens
         in _PRISTINE_TRAILING_ALLOW).  Any trailing alphabetic token outside
         that set → False.
      3. hard_reject(title, _series, _issue) is False.

    Uses the full normalized series words (``_normalize(series).split()``) for
    the prefix, not the filtered ``_series_tokens()`` list, so single-letter
    words like the "x" in "X-Men" are included in the expected prefix.

    When in doubt returns False — Haiku handles anything ambiguous.  BE STRICT:
    only accept the clearest cases to avoid false positives.
    """
    if match.get("score") != 1.0:
        return False

    title = match.get("title") or ""
    series = match.get("_series") or ""
    issue = match.get("_issue") or ""

    if not title or not series or not issue:
        return False

    # Condition 3: hard_reject should already have filtered these upstream, but
    # confirm defensively — a hard-rejected title is never pristine.
    if hard_reject(title, series, issue):
        return False

    # Condition 2: exact-prefix shape.
    # Build the expected prefix from ALL normalized series words + the issue
    # number.  Using _normalize(series).split() (not _series_tokens) preserves
    # single-letter words like the "x" in "X-Men" so the prefix is complete.
    title_norm = _normalize(_strip_grades(title))
    series_words = _normalize(series).split()
    title_tokens = title_norm.split()

    expected = series_words + [issue]

    if len(title_tokens) < len(expected):
        return False
    if title_tokens[: len(expected)] != expected:
        return False

    # Any tokens after the expected prefix must be harmless noise.
    trailing = title_tokens[len(expected):]
    for t in trailing:
        if t.isdigit():
            continue  # bare number (year, copy count) — always allowed
        if t not in _PRISTINE_TRAILING_ALLOW:
            return False

    return True


# ─── Verdict cache ────────────────────────────────────────────────────────────
# SQLite DB keyed by (title_key, wish_name).  title_key = _normalize(_strip_grades(title)),
# so the same comic title from two different sellers or a relisted item with a
# new item_id still hits the cache.  Compound key is intentional: one listing
# can legitimately match more than one wish item, so the verdict is per (title,
# wish) pair.  Cross-seller dedup in main() ensures one Haiku call per unique
# key per run.  (plan Decision 4 / R9 / BUI-223)

_VERDICT_DB_ENV = "WISHLIST_SELLERS_VERDICT_DB"
_DEFAULT_VERDICT_DB = Path.home() / ".cache" / "wishlist-sellers" / "verdicts.db"


def verdict_db_path() -> Path:
    """Return the SQLite verdict-cache path (overridable via env var for tests)."""
    env = os.environ.get(_VERDICT_DB_ENV, "")
    return Path(env) if env else _DEFAULT_VERDICT_DB


_VERDICT_COLUMNS = {"title_key", "wish_name", "genuine"}


def verdict_init_db(db_path: Path) -> None:
    """Ensure the verdicts DB directory and table exist.

    Auto-heals a stale schema: if the table already exists but its column set
    does not match {title_key, wish_name, genuine} (e.g. an old DB that still
    has `listing_id`), the table is dropped and recreated.  The cache is a
    disposable derived artifact — no migration needed.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        rows = con.execute("PRAGMA table_info(verdicts)").fetchall()
        if rows:
            # Table exists — check column names (column name is index 1 in each row)
            existing_cols = {r[1] for r in rows}
            if existing_cols != _VERDICT_COLUMNS:
                con.execute("DROP TABLE verdicts")
        con.execute(
            """CREATE TABLE IF NOT EXISTS verdicts (
                title_key   TEXT NOT NULL,
                wish_name   TEXT NOT NULL,
                genuine     INTEGER NOT NULL,
                PRIMARY KEY (title_key, wish_name)
            )"""
        )
        con.commit()


def verdict_get(title_key: str, wish_name: str, *, db_path: Path | None = None) -> bool | None:
    """Return cached verdict (True/False) or None if not in cache.

    Returns None for any DB error so callers treat it as a cache miss.
    """
    path = db_path or verdict_db_path()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path) as con:
            row = con.execute(
                "SELECT genuine FROM verdicts WHERE title_key=? AND wish_name=?",
                (title_key, wish_name),
            ).fetchone()
        return bool(row[0]) if row is not None else None
    except sqlite3.Error:
        return None


def verdict_put(
    title_key: str,
    wish_name: str,
    genuine: bool,
    *,
    db_path: Path | None = None,
) -> None:
    """Persist a verify verdict.  INSERT OR REPLACE so re-runs overwrite."""
    path = db_path or verdict_db_path()
    verdict_init_db(path)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT OR REPLACE INTO verdicts (title_key, wish_name, genuine) VALUES (?,?,?)",
            (title_key, wish_name, int(genuine)),
        )
        con.commit()


# ─── Pipeline functions ───────────────────────────────────────────────────────


def match_results_for_wish(results: list, wish_item: dict) -> list:
    """Apply hard_reject + match_listing to *results* for a single *wish_item*.

    Returns a list of match dicts each carrying:
    {seller, item_id, title, wish_name, price, end_date, end_date_iso,
     listing_url, score, _series, _issue, _series_name}
    """
    series = wish_item["series"]
    issue = wish_item["issue"]
    wish_name = wish_item["name"]
    wish_series_name = wish_item.get("_series_name")
    matches = []
    for item in results:
        title = item.get("title") or ""
        if not title:
            continue
        if hard_reject(title, series, issue):
            continue
        # BUI-226: deterministic era-gate — reject listings whose parenthesized
        # year or explicit volume number clearly contradicts the wish series era.
        # Fail-open (era_mismatch returns False) when any signal is missing.
        if era_mismatch(title, wish_series_name):
            continue
        # BUI-227: conservative reprint/non-original-format reject.
        if _reprint_reject(title):
            continue
        # BUI-230: digital-code / no-physical-comic reject.
        if _digital_reject(title):
            continue
        wish, score = match_listing(title, [wish_item])
        if wish is not None and score >= MATCH_SCORE_FLOOR:
            matches.append({
                "seller": item.get("seller"),
                "item_id": item.get("item_id"),
                "title": title,
                "wish_name": wish_name,
                "price": item.get("current_price"),
                "end_date": item.get("end_date"),
                "end_date_iso": item.get("end_date_iso"),
                "listing_url": item.get("listing_url"),
                "score": round(score, 2),
                # Private fields for Haiku context + dedup; stripped before output
                "_series": series,
                "_issue": issue,
                "_series_name": wish_series_name,
                "_release_year": wish_item.get("_release_year"),
            })
    return matches


def _price_value(m: dict) -> float:
    """Numeric price for choosing the cheapest representative; inf if unparseable."""
    raw = m.get("price")
    if not raw:
        return float("inf")
    digits = re.sub(r"[^0-9.]", "", str(raw))
    try:
        return float(digits) if digits else float("inf")
    except ValueError:
        return float("inf")


def dedup_matches(matches: list) -> list:
    """Collapse matches to one representative per distinct wish *book* per seller.

    Two passes:
      1. (seller, item_id) — a single physical listing surfaced under multiple
         wish searches is one comic, so keep its highest-score attribution
         (plan R5a/C3): one listing → one wish book.
      2. (seller, wish_name) — collapse variant copies of the SAME wish book
         from one seller (e.g. a dozen variant covers of Aliens vs Avengers #1)
         into ONE representative, the cheapest. (smoke-test finding)

    After this, each seller has at most one entry per distinct wish book, so the
    downstream ≥2 gate counts distinct *books* — the true combine-shipping
    signal — not duplicate variant listings of a single book.
    """
    # Pass 1: one entry per physical listing, best attribution.
    by_listing: dict[tuple, dict] = {}
    for m in matches:
        key = (m.get("seller"), m.get("item_id"))
        if key not in by_listing or m["score"] > by_listing[key]["score"]:
            by_listing[key] = m
    # Pass 2: one representative (cheapest) per (seller, wish book).
    by_book: dict[tuple, dict] = {}
    for m in by_listing.values():
        key = (m.get("seller"), m.get("wish_name"))
        if key not in by_book or _price_value(m) < _price_value(by_book[key]):
            by_book[key] = m
    return list(by_book.values())


def group_and_gate(matches: list, *, min_matches: int = 2) -> dict:
    """Group matches by seller; drop sellers with < min_matches DISTINCT books.

    Expects matches already collapsed by dedup_matches (one entry per
    (seller, wish book)), so each seller's entry count equals its distinct-book
    count. Returns {seller_id: [match, ...]} for qualifying sellers only.
    """
    groups: dict[str, list] = {}
    for m in matches:
        seller = m.get("seller") or "_unknown_"
        groups.setdefault(seller, []).append(m)
    return {s: ms for s, ms in groups.items() if len(ms) >= min_matches}


def batch_check_owned(wish_pairs: list[tuple[str, str]], base: str) -> set:
    """POST to /api/comics/collection/check/batch; return set of owned (series, issue).

    A 409 means the collection store was never imported → hard-fail (sys.exit).
    Any other network / HTTP error also hard-fails: we must never treat a failed
    call as "not owned" (plan R10/R11).
    """
    if not wish_pairs:
        return set()
    url = f"{base}/api/comics/collection/check/batch"
    payload = {"items": [{"series": s, "issue": i} for s, i in wish_pairs]}
    try:
        resp = requests.post(url, json=payload, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"Error: owned-check request failed: {e}", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 409:
        print(
            "Error: collection store was never imported on the server (HTTP 409). "
            "Hard-failing — never assume 'not owned' on a missing store. "
            "Import the collection first via POST /api/comics/collection/import.",
            file=sys.stderr,
        )
        sys.exit(1)
    if resp.status_code != 200:
        print(
            f"Error: owned-check returned HTTP {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error: owned-check response not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    owned: set[tuple[str, str]] = set()
    for r in data.get("results", []):
        if r.get("match_status") == "in_collection":
            owned.add((r.get("series", ""), r.get("issue", "")))
    return owned


def apply_verdict_cache(matches: list, db_path: Path) -> tuple[list, list, list]:
    """Split *matches* into (cached_genuine, cached_false, uncached) by verdict cache.

    cached_genuine  — previously verified as genuine; keep them.
    cached_false    — previously rejected; discard (caller never sends to verify).
    uncached        — no prior verdict; caller sends to verify_with_claude.

    Cache lookup uses _title_key(m["title"]) so relists and cross-seller
    duplicates of the same comic hit the cache regardless of their item_id.
    """
    cached_genuine: list = []
    cached_false: list = []
    uncached: list = []
    for m in matches:
        v = verdict_get(_title_key(m["title"]), m["wish_name"], db_path=db_path)
        if v is True:
            cached_genuine.append(m)
        elif v is False:
            cached_false.append(m)
        else:
            uncached.append(m)
    return cached_genuine, cached_false, uncached


# ─── Output helpers ───────────────────────────────────────────────────────────

def _trunc(text: str, width: int) -> str:
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def format_table(grouped: dict) -> str:
    """Format grouped seller results as a compact human-readable table."""
    lines: list[str] = []
    # Sort sellers by match count descending so the most promising come first
    for seller, matches in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        count = len(matches)
        lines.append(f"Seller: {seller}  ({count} match{'es' if count != 1 else ''})")
        header = (
            f"  {'Wish Item':<30}  {'Title':<45}  {'Price':<10}  {'Ends':<12}  URL"
        )
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for m in matches:
            wish = _trunc(m.get("wish_name") or "", 30)
            title = _trunc(m.get("title") or "", 45)
            price = _trunc(m.get("price") or "", 10)
            end = _trunc(m.get("end_date") or "", 12)
            url = m.get("listing_url") or ""
            lines.append(f"  {wish:<30}  {title:<45}  {price:<10}  {end:<12}  {url}")
        lines.append("")
    return "\n".join(lines)


def _emit(*, grouped: dict, json_output: bool) -> None:
    """Emit the final compact result to stdout.

    CRITICAL (R15): only the compact final result ever reaches stdout.
    Private pipeline fields (_series, _issue) are stripped from JSON output.
    """
    if not grouped:
        if json_output:
            print("[]")
        else:
            print("No sellers found with ≥2 genuine matches.")
        return

    if json_output:
        out = [
            {
                "seller": seller,
                "matches": [
                    {k: v for k, v in m.items() if not k.startswith("_")}
                    for m in matches
                ],
            }
            for seller, matches in sorted(grouped.items(), key=lambda kv: -len(kv[1]))
        ]
        print(json.dumps(out))
    else:
        print(format_table(grouped))


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main(argv=None):  # noqa: C901 — the pipeline is inherently linear/long
    parser = argparse.ArgumentParser(
        prog="wishlist-sellers",
        description=(
            "Discover eBay sellers holding ≥2 wish-list books "
            "(combine-shipping candidates)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit compact JSON instead of a human table",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N matchable wish items (for quick/bounded runs)",
    )
    parser.add_argument(
        "--no-record-seen",
        action="store_true",
        help="Do not write surfaced listings to the seen-store (repeatable/dry runs)",
    )
    parser.add_argument(
        "--buying-options",
        choices=["auction", "bin", "all"],
        default="auction",
        dest="buying_options",
        help=(
            "eBay buying-options filter (default: auction — auction listings only). "
            "Use 'bin' for Buy It Now only, or 'all' for both auction and BIN."
        ),
    )
    parser.add_argument(
        "--no-item-specifics",
        action="store_true",
        dest="no_item_specifics",
        help=(
            "Skip the eBay item-specifics era filter for bare-title matches (BUI-229). "
            "By default, bare-title matches (no parenthesized year, no 'vol N') are "
            "checked against the Publication Year aspect to catch modern-series "
            "false positives that the title-based gates cannot see."
        ),
    )
    args = parser.parse_args(argv)

    mode = args.buying_options
    ebay_buying = _BUYING_OPTIONS_MAP[mode]
    print(f"  Buying options: {mode} (eBay filter {ebay_buying})", file=sys.stderr)

    # ── Step 1: fetch wish list ───────────────────────────────────────────────
    print("Fetching wish list...", file=sys.stderr)
    wish_list = fetch_wish_list()
    # Hard-fail on empty: the endpoint returns [] (HTTP 200) for a missing/
    # corrupt wish-list file.  An empty list would silently yield zero sellers
    # and look like a successful run (plan R1 / review C-fix).
    if not wish_list:
        print(
            "Error: wish list is empty (server returned []). "
            "The wish-list file may be missing or corrupt on the server. "
            "Hard-failing: will not run a scan against an empty list.",
            file=sys.stderr,
        )
        sys.exit(1)

    wish_items = prepare_wish_items(wish_list)
    print(
        f"  {len(wish_list)} wish-list item(s); "
        f"{len(wish_items)} matchable (items with #N issue number)",
        file=sys.stderr,
    )
    if not wish_items:
        print(
            "Error: no matchable wish-list items after filtering. "
            "Items without an issue number (#N) are skipped — see module docstring.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.limit is not None:
        wish_items = wish_items[: args.limit]
        print(
            f"  --limit {args.limit}: processing {len(wish_items)} wish item(s)",
            file=sys.stderr,
        )

    # ── Step 2: eBay OAuth token ──────────────────────────────────────────────
    client_id, client_secret, base_url = load_config()
    if args.env:
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
    token = get_token(client_id, client_secret, base_url)

    # ── Steps 3–5: per-wish search → active filter → match ───────────────────
    # Skip degenerate series (e.g. "300") from the automated fan-out; surface
    # them for manual checking instead of exploding the search.
    skipped_degenerate = [w["name"] for w in wish_items if is_degenerate_series(w)]
    searchable = [w for w in wish_items if not is_degenerate_series(w)]
    if skipped_degenerate:
        print(
            f"  Skipping {len(skipped_degenerate)} generic-name item(s) "
            f"(numeric series — search by hand): {', '.join(skipped_degenerate)}",
            file=sys.stderr,
        )

    all_matches: list = []
    for wish_item in searchable:
        keyword = f'{wish_item["series"]} #{wish_item["issue"]}'
        results = ebay_search_cache.get(keyword, mode=mode)
        if results is None:
            print(f"  Searching eBay: {keyword}", file=sys.stderr)
            results = search_by_keyword(keyword, token, base_url, buying_options=ebay_buying)
            ebay_search_cache.put(keyword, results, mode=mode)
        else:
            print(f"  Cache hit: {keyword}", file=sys.stderr)
        # Drop ended listings from cache before matching (R3a)
        results = ebay_search_cache.filter_active(results)
        hits = match_results_for_wish(results, wish_item)
        all_matches.extend(hits)

    print(f"  {len(all_matches)} raw match(es) before dedup", file=sys.stderr)

    # ── Step 6: dedup by (seller, item_id) ───────────────────────────────────
    all_matches = dedup_matches(all_matches)
    print(
        f"  {len(all_matches)} distinct (seller, book) match(es) after dedup",
        file=sys.stderr,
    )

    if not all_matches:
        print("No wish-list matches found on eBay.", file=sys.stderr)
        _emit(grouped={}, json_output=args.json_output)
        return 0

    # ── Step 7: drop already-seen (global seen set, seller=None — R11) ───────
    seen = fetch_seen_item_ids(None)
    before = len(all_matches)
    all_matches = [m for m in all_matches if m.get("item_id") not in seen]
    hidden = before - len(all_matches)
    if hidden:
        print(f"  {hidden} already-seen match(es) dropped", file=sys.stderr)

    # ── Step 7.5: item-specifics era gate for bare-title residual (BUI-229) ─────
    # Apply only to matches whose title carries no era signal (no parenthesized
    # year, no "vol N") — the cases era_mismatch cannot catch.  Fetches
    # localizedAspects from the Browse API for each qualifying match and drops
    # those whose Publication Year (or Era fallback) contradicts the wish series
    # era.  Fail-open: missing aspects / network errors → keep the listing.
    if not args.no_item_specifics and all_matches:
        checked = 0
        dropped_is = 0
        filtered: list = []
        for m in all_matches:
            sn = m.get("_series_name")
            title = m.get("title") or ""
            if sn and not _title_paren_years(title) and _title_volume(title) is None:
                checked += 1
                aspects = get_item_aspects(m["item_id"], token, base_url)
                if publication_year_mismatch(aspects, sn, m.get("_release_year")):
                    dropped_is += 1
                    continue
            filtered.append(m)
        all_matches = filtered
        if checked:
            print(
                f"  Item-specifics era filter: checked {checked} bare-title"
                f" candidate(s), dropped {dropped_is}",
                file=sys.stderr,
            )

    # ── Step 8: drop owned via batch endpoint (R10) ───────────────────────────
    base_server = _server_base()
    if base_server:
        distinct_pairs = list({(m["_series"], m["_issue"]) for m in all_matches})
        print(
            f"  Checking {len(distinct_pairs)} distinct (series, issue) pair(s) "
            "for ownership...",
            file=sys.stderr,
        )
        owned_pairs = batch_check_owned(distinct_pairs, base_server)
        before = len(all_matches)
        all_matches = [
            m for m in all_matches
            if (m["_series"], m["_issue"]) not in owned_pairs
        ]
        dropped_owned = before - len(all_matches)
        if dropped_owned:
            print(f"  {dropped_owned} already-owned match(es) dropped", file=sys.stderr)
    else:
        print(
            "Warning: COMICS_SERVER_URL not set — skipping owned-filter. "
            "Owned books may appear in the results. "
            "Set COMICS_SERVER_URL to enable ownership checks.",
            file=sys.stderr,
        )

    # ── Step 9: group by seller; apply ≥2 gate ────────────────────────────────
    grouped = group_and_gate(all_matches)
    print(
        f"  {len(grouped)} seller(s) with ≥2 matches before verify",
        file=sys.stderr,
    )

    if not grouped:
        print("No sellers with ≥2 matches found.", file=sys.stderr)
        _emit(grouped={}, json_output=args.json_output)
        return 0

    # Flatten post-gate survivors for verdict-cache split
    candidates = [m for ms in grouped.values() for m in ms]

    # ── Step 10: verdict cache split → verify uncached (R8/R9) ───────────────
    db_path = verdict_db_path()
    verdict_init_db(db_path)
    cached_genuine, _cached_false, uncached = apply_verdict_cache(candidates, db_path)
    print(
        f"  Verdict cache: {len(cached_genuine)} genuine / "
        f"{len(_cached_false)} false / {len(uncached)} uncached",
        file=sys.stderr,
    )

    if uncached:
        # BUI-224: deterministic shortcut — pristine score-1.0 listings skip Haiku.
        # Split before cross-seller dedup so only ambiguous candidates go to verify.
        pristine_direct = [m for m in uncached if is_pristine_match(m)]
        needs_verify = [m for m in uncached if not is_pristine_match(m)]

        if pristine_direct:
            print(
                f"  Deterministic shortcut: {len(pristine_direct)} pristine match(es) skipped Haiku",
                file=sys.stderr,
            )
            # Persist as genuine by (title_key, wish_name) so re-runs hit the cache.
            persisted_pristine: set[tuple] = set()
            for m in pristine_direct:
                key = (_title_key(m["title"]), m["wish_name"])
                if key not in persisted_pristine:
                    verdict_put(key[0], m["wish_name"], True, db_path=db_path)
                    persisted_pristine.add(key)

        if needs_verify:
            # Cross-seller dedup: same normalized title + wish_name → one Haiku call.
            # Build one representative per (title_key, wish_name) pair so the verify
            # model receives each distinct comic exactly once across all sellers.
            rep_map: dict[tuple, dict] = {}
            for m in needs_verify:
                key = (_title_key(m["title"]), m["wish_name"])
                if key not in rep_map:
                    rep_map[key] = m
            representatives = list(rep_map.values())
            deduped_count = len(needs_verify) - len(representatives)
            if deduped_count:
                print(
                    f"  Cross-seller dedup: {len(needs_verify)} uncached → "
                    f"{len(representatives)} unique (title, wish) pair(s) to verify",
                    file=sys.stderr,
                )
            print(
                f"  Verifying {len(representatives)} uncached candidate(s) with Claude Haiku...",
                file=sys.stderr,
            )
            verified_reps = verify_with_claude(representatives)
            # Keys that came back as genuine from verify
            genuine_keys = {(_title_key(m["title"]), m["wish_name"]) for m in verified_reps}
            # Fan the verdict back out to ALL needs_verify listings sharing each key
            verified = [
                m for m in needs_verify
                if (_title_key(m["title"]), m["wish_name"]) in genuine_keys
            ]
            # Persist one verdict row per (title_key, wish_name) — not per listing
            persisted: set[tuple] = set()
            for m in needs_verify:
                key = (_title_key(m["title"]), m["wish_name"])
                if key not in persisted:
                    verdict_put(key[0], m["wish_name"], key in genuine_keys, db_path=db_path)
                    persisted.add(key)
        else:
            verified = []
    else:
        pristine_direct = []
        verified = []

    survivors = cached_genuine + pristine_direct + verified

    # ── Step 11: re-apply ≥2 gate post-verify ────────────────────────────────
    # A seller can fall below 2 if some of its matches are rejected by verify.
    grouped = group_and_gate(survivors)
    print(
        f"  {len(grouped)} seller(s) with ≥2 genuine matches after verify",
        file=sys.stderr,
    )

    # ── Step 12: record seen (global, seller=None — R11) ─────────────────────
    final_item_ids = [m["item_id"] for ms in grouped.values() for m in ms]
    if final_item_ids and not args.no_record_seen:
        record_items_seen(final_item_ids, None)
    elif args.no_record_seen and final_item_ids:
        print(
            f"  --no-record-seen: not writing {len(final_item_ids)} item(s) to seen-store",
            file=sys.stderr,
        )

    # ── Step 13: emit ─────────────────────────────────────────────────────────
    _emit(grouped=grouped, json_output=args.json_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
