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
import sqlite3
import sys
from pathlib import Path

import requests

import ebay_search_cache
from ebay_fetch import (
    PRODUCTION_BASE,
    SANDBOX_BASE,
    get_token,
    load_config,
    search_by_keyword,
)
from seller_scan import (
    _server_base,
    fetch_seen_item_ids,
    fetch_wish_list,
    hard_reject,
    match_listing,
    prepare_wish_items,
    record_items_seen,
    verify_with_claude,
)

# ─── Score floor ──────────────────────────────────────────────────────────────
# Mirror seller_scan.match_listing's internal 0.65 threshold; match_listing
# already returns (None, 0.0) below it, so this gate is belt-and-suspenders.
MATCH_SCORE_FLOOR: float = 0.65

# ─── Verdict cache ────────────────────────────────────────────────────────────
# SQLite DB keyed by (listing_id, wish_name).  Compound key is intentional:
# one listing can legitimately match more than one wish item, so the verdict is
# per (listing, wish-item) pair while the ≥2 count dedups by listing.
# (plan Decision 4 / R9)

_VERDICT_DB_ENV = "WISHLIST_SELLERS_VERDICT_DB"
_DEFAULT_VERDICT_DB = Path.home() / ".cache" / "wishlist-sellers" / "verdicts.db"


def verdict_db_path() -> Path:
    """Return the SQLite verdict-cache path (overridable via env var for tests)."""
    env = os.environ.get(_VERDICT_DB_ENV, "")
    return Path(env) if env else _DEFAULT_VERDICT_DB


def verdict_init_db(db_path: Path) -> None:
    """Ensure the verdicts DB directory and table exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS verdicts (
                listing_id  TEXT NOT NULL,
                wish_name   TEXT NOT NULL,
                genuine     INTEGER NOT NULL,
                PRIMARY KEY (listing_id, wish_name)
            )"""
        )
        con.commit()


def verdict_get(listing_id: str, wish_name: str, *, db_path: Path | None = None) -> bool | None:
    """Return cached verdict (True/False) or None if not in cache.

    Returns None for any DB error so callers treat it as a cache miss.
    """
    path = db_path or verdict_db_path()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path) as con:
            row = con.execute(
                "SELECT genuine FROM verdicts WHERE listing_id=? AND wish_name=?",
                (listing_id, wish_name),
            ).fetchone()
        return bool(row[0]) if row is not None else None
    except sqlite3.Error:
        return None


def verdict_put(
    listing_id: str,
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
            "INSERT OR REPLACE INTO verdicts (listing_id, wish_name, genuine) VALUES (?,?,?)",
            (listing_id, wish_name, int(genuine)),
        )
        con.commit()


# ─── Pipeline functions ───────────────────────────────────────────────────────


def match_results_for_wish(results: list, wish_item: dict) -> list:
    """Apply hard_reject + match_listing to *results* for a single *wish_item*.

    Returns a list of match dicts each carrying:
    {seller, item_id, title, wish_name, price, end_date, end_date_iso,
     listing_url, score, _series, _issue}
    """
    series = wish_item["series"]
    issue = wish_item["issue"]
    wish_name = wish_item["name"]
    matches = []
    for item in results:
        title = item.get("title") or ""
        if not title:
            continue
        if hard_reject(title, series, issue):
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
                # Private fields for owned-check dedup; stripped before output
                "_series": series,
                "_issue": issue,
            })
    return matches


def dedup_matches(matches: list) -> list:
    """Dedup by (seller, item_id), keeping the entry with the highest score.

    A single listing surfaced under multiple wish-item searches counts at most
    once per seller toward the ≥2 gate (plan R5a/C3).
    """
    best: dict[tuple, dict] = {}
    for m in matches:
        key = (m.get("seller"), m.get("item_id"))
        if key not in best or m["score"] > best[key]["score"]:
            best[key] = m
    return list(best.values())


def group_and_gate(matches: list, *, min_matches: int = 2) -> dict:
    """Group matches by seller; drop sellers with < min_matches entries.

    Returns {seller_id: [match, ...]} for qualifying sellers only.
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
    """
    cached_genuine: list = []
    cached_false: list = []
    uncached: list = []
    for m in matches:
        v = verdict_get(m["item_id"], m["wish_name"], db_path=db_path)
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
    args = parser.parse_args(argv)

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

    # ── Step 2: eBay OAuth token ──────────────────────────────────────────────
    client_id, client_secret, base_url = load_config()
    if args.env:
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
    token = get_token(client_id, client_secret, base_url)

    # ── Steps 3–5: per-wish search → active filter → match ───────────────────
    all_matches: list = []
    for wish_item in wish_items:
        keyword = f'{wish_item["series"]} #{wish_item["issue"]}'
        results = ebay_search_cache.get(keyword)
        if results is None:
            print(f"  Searching eBay: {keyword}", file=sys.stderr)
            results = search_by_keyword(keyword, token, base_url)
            ebay_search_cache.put(keyword, results)
        else:
            print(f"  Cache hit: {keyword}", file=sys.stderr)
        # Drop ended listings from cache before matching (R3a)
        results = ebay_search_cache.filter_active(results)
        hits = match_results_for_wish(results, wish_item)
        all_matches.extend(hits)

    print(f"  {len(all_matches)} raw match(es) before dedup", file=sys.stderr)

    # ── Step 6: dedup by (seller, item_id) ───────────────────────────────────
    all_matches = dedup_matches(all_matches)
    print(f"  {len(all_matches)} match(es) after dedup (by seller+listing)", file=sys.stderr)

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
        print(
            f"  Verifying {len(uncached)} uncached candidate(s) with Claude Haiku...",
            file=sys.stderr,
        )
        verified = verify_with_claude(uncached)
        # Persist new verdicts
        verified_keys = {(m["item_id"], m["wish_name"]) for m in verified}
        for m in uncached:
            genuine = (m["item_id"], m["wish_name"]) in verified_keys
            verdict_put(m["item_id"], m["wish_name"], genuine, db_path=db_path)
    else:
        verified = []

    survivors = cached_genuine + verified

    # ── Step 11: re-apply ≥2 gate post-verify ────────────────────────────────
    # A seller can fall below 2 if some of its matches are rejected by verify.
    grouped = group_and_gate(survivors)
    print(
        f"  {len(grouped)} seller(s) with ≥2 genuine matches after verify",
        file=sys.stderr,
    )

    # ── Step 12: record seen (global, seller=None — R11) ─────────────────────
    final_item_ids = [m["item_id"] for ms in grouped.values() for m in ms]
    if final_item_ids:
        record_items_seen(final_item_ids, None)

    # ── Step 13: emit ─────────────────────────────────────────────────────────
    _emit(grouped=grouped, json_output=args.json_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
