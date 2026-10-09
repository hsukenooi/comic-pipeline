#!/usr/bin/env python3
"""slab-deals: which copies of a book are good buys right now? (BUI-1188)

Deterministic steps 1-3 of `/comic:slab-deals`:

  1. The CGC price ladder (Universal / blue label) from the comps ledger,
     `GET /api/comics/comps?pool=slab`, with the sale count per grade.
  2. Active listings (auction and Buy It Now) from the eBay Browse API, minus
     foreign editions, moderns/reprints, lots, signed/qualified/restored/
     conserved copies, and anything that is not a CGC slab with a readable
     grade.
  3. Each survivor ranked by ask / that grade's sale median.

Step 4 (`comic-fmv` graded mode on a short shortlist) lives in the skill; this
script only emits the batch file for it (`--fmv-batch`). It never bids and
never writes to the comics server.

Usage (from apps/ebay):
    uv run python src/slab_deals.py --title "Amazing Spider-Man" --issue 50 \\
        --year 1967 --comic-id 685 [--json] [--fmv-batch PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from typing import Any

import requests

import grade_tokens
from comic_identity import should_reject
from ebay_fetch import get_token, load_config, search_by_keyword
from seller_scan import _server_base

DEFAULT_SHORTLIST = 3
DEFAULT_MAX_RESULTS = 200
COMPS_LIMIT = 5000
_HEALTH_TIMEOUT = 10
_READ_TIMEOUT = 30

# Ladder identity: Universal (blue label) CGC. The only slab identity
# comic-fmv's graded mode auto-prices (fmv.md, Graded (slab) pricing mode).
LADDER_CERTIFIER = "cgc"
LADDER_LABEL = "universal"

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)

# Slab-only extras on top of comic_identity.should_reject (BUI-1201), which
# now rejects foreign-edition publishers/phrases (Williams-Verlag, Panini,
# "Swedish Foreign Ed"), later-era variant covers relative to --year, lots,
# and facsimiles for every caller. What stays here is too broad for the
# purchase path that seller-scan and wishlist-sellers gate with
# should_reject, where a false reject costs a missed purchase (BUI-239):
# bare nationality words (and "Canadian"/"UK"/"pence" price variants of the
# US printing), any variant cover at all, sketch/homage/Sony/foil covers,
# and an explicit Vol. 2-9. Here a false drop only hides a listing, it never
# prices one, and the CGC Universal ladder is for the plain first print.
_NOT_THE_BOOK_RE = re.compile(
    r"""\bforeign\b
    | \b(?:german|swedish|italian|dutch|netherlands|danish|norwegian|finnish
        |spanish|french|brazilian|mexico|mexican|greek|turkish|yugoslavian
        |australian|canadian|uk)\b
    | \bvariants?\b | \bvirgin\b
    | \bincentive\b | \b1:\d+\b | \blgy\b | \bsketch\b | \bhomage\b
    | \bsony\b | \bfoil\b | \bvol(?:ume)?\.?\s*[2-9]\b
    | \bv[2-9]\b | \bpence\b""",
    re.IGNORECASE | re.VERBOSE,
)
_YEAR_RE = re.compile(r"(?<!\d)(19[3-9]\d|20[0-3]\d)(?!\d)")


class SlabDealsError(Exception):
    """A hard failure: never rendered as an empty ladder or a clean zero."""


# --- Step 1: ladder -------------------------------------------------------


def build_ladder(rows: list[dict]) -> list[dict]:
    """Per-grade CGC Universal sale stats from comps-ledger rows.

    Returns rungs highest grade first: {grade, n, median, low, high}. Rows of
    another pool, certifier, or label, and rows missing a grade or price, are
    skipped. Excluded (stamped) rows never arrive: the endpoint hides them.
    """
    by_grade: dict[float, list[float]] = {}
    for r in rows:
        if r.get("pool") != "slab":
            continue
        if r.get("certifier") != LADDER_CERTIFIER:
            continue
        if (r.get("label") or LADDER_LABEL) != LADDER_LABEL:
            continue
        price, grade = r.get("price"), r.get("grade")
        if price is None or grade is None or price <= 0:
            continue
        by_grade.setdefault(float(grade), []).append(float(price))
    return [
        {
            "grade": g,
            "n": len(p),
            "median": round(statistics.median(p), 2),
            "low": min(p),
            "high": max(p),
        }
        for g, p in sorted(by_grade.items(), reverse=True)
    ]


def fetch_comps(base: str, *, comic_id: int | None, title: str,
                issue: str, year: int | None) -> list[dict]:
    """Read the slab comps ledger for one book. Hard-fails on any server
    problem: a failed read must never look like a book with no sales."""
    params: dict[str, Any] = {"pool": "slab", "limit": COMPS_LIMIT}
    if comic_id is not None:
        params["comic_id"] = comic_id
    else:
        params.update(title=title, issue=issue)
        if year is not None:
            params["year"] = year
    try:
        resp = requests.get(f"{base}/api/comics/comps", params=params,
                            timeout=_READ_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise SlabDealsError(f"comps ledger unreachable at {base}: {e}") from e
    if resp.status_code == 400:
        raise SlabDealsError(
            "comps ledger could not resolve this book "
            f"({resp.text[:160]}); pass --comic-id or a title/issue/year "
            "that matches a priced book")
    if resp.status_code != 200:
        raise SlabDealsError(
            f"comps ledger HTTP {resp.status_code}: {resp.text[:160]}")
    return resp.json()


def _health_gate(base: str) -> None:
    try:
        requests.get(f"{base}/health", timeout=_HEALTH_TIMEOUT).raise_for_status()
    except requests.exceptions.RequestException as e:
        raise SlabDealsError(
            f"the comics server at {base} is not responding ({e})") from e


# --- Step 2: active listings ----------------------------------------------


def _price(item: dict) -> float | None:
    """USD price of a parsed itemSummary, or None (no currency blending)."""
    raw = item.get("current_price")
    if not isinstance(raw, str) or not raw.startswith("$"):
        return None
    try:
        return float(raw[1:].replace(",", ""))
    except ValueError:
        return None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _wrong_book(text: str, *, title: str, issue: str, year: int | None) -> bool:
    """True when the title is visibly not this book's own first-print copy:
    the series name or issue number is absent (another series or issue, a
    homage), the series name only follows the issue number (another series
    whose character text names this one: "Detective Comics #227 ...
    Batman/Robin" for Batman #227, BUI-1212), a year more than one after
    the target appears, or one more than one before it follows the issue
    number, a foreign/variant/lot marker appears, or an `N-<issue>`
    range or `1 2 3` run says it is a multi-issue lot."""
    series = _squash(_LEADING_ARTICLE_RE.sub("", title))
    num = re.escape(str(issue))
    hit = next((m for m in re.finditer(
        rf"(?<![\d:/.])(?:#\s*|\s){num}(?![\d:/.])", text)
        if series in _squash(text[:m.start()])), None)
    if hit is None:
        return True
    if re.search(rf"\b\d+\s*-\s*{num}\b|\b1\s+2\s+3\b", text):
        return True
    if _NOT_THE_BOOK_RE.search(text):
        return True
    if year is None:
        return False
    if any(int(y) > year + 1 for y in _YEAR_RE.findall(text)):
        return True
    # Earlier years count only after the issue number: a series start year
    # ("Amazing Spider-Man (1963) # 50") sits before it, and "(1963 series)"
    # names the volume, not the cover date.
    return any(int(m.group(1)) < year - 1 for m in
               _YEAR_RE.finditer(text, hit.end())
               if not re.match(r"\s*series\b", text[m.end():], re.IGNORECASE))


def classify_listing(item: dict, *, title: str, issue: str,
                     year: int | None) -> tuple[dict | None, str | None]:
    """Return (candidate, None) for a like-for-like CGC blue-label listing,
    else (None, drop_reason). Drop reasons: not_cgc, no_grade, label_<x>,
    rejected (foreign/reprint/modern/lot/wrong issue), no_usd_price."""
    text = item.get("title") or ""
    certifier, grade, _bare = grade_tokens.extract_title_certification(text)
    if certifier != LADDER_CERTIFIER:
        if grade_tokens.resolve_certifier_token(text) == LADDER_CERTIFIER:
            return None, "no_grade"
        return None, "not_cgc"
    label = grade_tokens.resolve_label(text)
    if label is not None and label != LADDER_LABEL:
        return None, f"label_{label}"
    if _wrong_book(text, title=title, issue=issue, year=year) or should_reject(
            text, title, issue,
            release_year=str(year) if year else None, include_graded=True):
        return None, "rejected"
    price = _price(item)
    if price is None:
        return None, "no_usd_price"
    return {
        "item_id": item.get("item_id"),
        "title": text,
        "grade": grade,
        "listing_type": item.get("listing_type"),
        "price": price,
        "end_date_iso": item.get("end_date_iso"),
        "seller": item.get("seller"),
        "url": f"https://www.ebay.com/itm/{item.get('item_id')}",
        "page_quality": grade_tokens.resolve_page_quality(text) or "unknown",
    }, None


def screen_listings(items: list[dict], *, title: str, issue: str,
                    year: int | None) -> tuple[list[dict], dict[str, int]]:
    kept: list[dict] = []
    dropped: dict[str, int] = {}
    seen: set = set()
    for item in items:
        cand, reason = classify_listing(item, title=title, issue=issue, year=year)
        if cand is None:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        if cand["item_id"] in seen:
            dropped["duplicate"] = dropped.get("duplicate", 0) + 1
            continue
        seen.add(cand["item_id"])
        kept.append(cand)
    return kept, dropped


def search_listings(keyword: str, *, max_results: int) -> list[dict]:
    """Browse API search over auctions and BIN. Hard-fails on a Browse error:
    a failed search must not read as 'no listings'."""
    client_id, client_secret, base_url = load_config()
    token = get_token(client_id, client_secret, base_url)
    errors: list[str] = []
    items = search_by_keyword(keyword, token, base_url,
                              max_results=max_results,
                              buying_options="AUCTION|FIXED_PRICE",
                              on_error=errors.append)
    if errors:
        raise SlabDealsError(f"eBay Browse search failed: {errors[0]}")
    return items


# --- Step 3: ranking ------------------------------------------------------


def rank_listings(listings: list[dict], ladder: list[dict]) -> list[dict]:
    """Add median/n/ratio (ask / the grade's sale median) and sort ascending.

    A listing whose grade has no rung gets ratio None and sorts last: no
    sales at that grade means no honest comparison, not a cheap one.
    """
    rungs = {r["grade"]: r for r in ladder}
    ranked = []
    for lst in listings:
        rung = rungs.get(lst["grade"])
        row = dict(lst)
        row["median"] = rung["median"] if rung else None
        row["n"] = rung["n"] if rung else 0
        row["ratio"] = round(lst["price"] / rung["median"], 2) if rung else None
        ranked.append(row)
    ranked.sort(key=lambda r: (r["ratio"] is None, r["ratio"] or 0, r["price"]))
    return ranked


def pick_shortlist(ranked: list[dict], cap: int) -> list[dict]:
    return [r for r in ranked if r["ratio"] is not None][:max(cap, 0)]


def fmv_batch(shortlist: list[dict], *, title: str, issue: str,
              year: int | None, publisher: str | None) -> list[dict]:
    """Batch rows for `comic-fmv --batch` graded mode (one per shortlisted
    listing). `locg_id` is null: the comics row is found by title/issue/year."""
    return [{
        "item_id": s["item_id"],
        "title": title,
        "issue": issue,
        "year": year,
        "grade": s["grade"],
        "locg_id": None,
        "publisher": publisher,
        "certifier": LADDER_CERTIFIER,
        "label": LADDER_LABEL,
        "page_quality": s["page_quality"],
        "listing_type": "BIN" if s["listing_type"] == "BIN" else "Auction",
    } for s in shortlist]


# --- Output ---------------------------------------------------------------


def render(book: str, ladder: list[dict], ranked: list[dict],
           dropped: dict[str, int], shortlist: list[dict],
           truncated: bool = False) -> str:
    out = [f"{book} - CGC Universal ladder ({sum(r['n'] for r in ladder)} sales)", ""]
    if ladder:
        out.append("Grade  Sales  Median     Low      High")
        for r in ladder:
            out.append(f"{r['grade']:<6} {r['n']:<6} ${r['median']:<9,.0f} "
                       f"${r['low']:<7,.0f} ${r['high']:,.0f}")
    else:
        out.append("(no CGC Universal sales in the ledger)")
    drop_txt = json.dumps(dropped, sort_keys=True) if dropped else "none"
    out += ["", f"Ranked listings ({len(ranked)} like-for-like; dropped: {drop_txt})", ""]
    if ranked:
        out.append("#    Grade  Type     Ask        Median     Ask/Med  N   Ends/Seller  Link")
        short_ids = {s["item_id"] for s in shortlist}
        for i, r in enumerate(ranked, 1):
            med = f"${r['median']:,.0f}" if r["median"] is not None else "n/a"
            ratio = f"{r['ratio']:.0%}" if r["ratio"] is not None else "n/a"
            if r["listing_type"] == "Auction":
                ends = (r["end_date_iso"] or "")[:16].replace("T", " ")
            else:
                ends = r["seller"] or ""
            star = "*" if r["item_id"] in short_ids else " "
            out.append(f"{i:<3}{star} {r['grade']:<6} {r['listing_type']:<8} "
                       f"${r['price']:<9,.2f} {med:<10} {ratio:<8} {r['n']:<3} "
                       f"{ends}  {r['url']}")
        out += ["", "* = shortlisted for comic-fmv"]
    if truncated:
        out.append("WARNING: the search hit its result cap; raise --max-results "
                   "for full coverage")
    return "\n".join(out)


def run(args: argparse.Namespace) -> dict:
    base = _server_base()
    if not base:
        raise SlabDealsError(
            "COMICS_SERVER_URL is not set; cannot read the comps ledger")
    _health_gate(base)
    rows = fetch_comps(base, comic_id=args.comic_id, title=args.title,
                       issue=args.issue, year=args.year)
    ladder = build_ladder(rows)
    keyword = f"{_LEADING_ARTICLE_RE.sub('', args.title)} #{args.issue} CGC"
    items = search_listings(keyword, max_results=args.max_results)
    kept, dropped = screen_listings(items, title=args.title, issue=args.issue,
                                    year=args.year)
    ranked = rank_listings(kept, ladder)
    short = pick_shortlist(ranked, args.shortlist)
    return {
        "book": f"{args.title} #{args.issue}" + (f" ({args.year})" if args.year else ""),
        "keyword": keyword,
        "searched": len(items),
        "truncated": len(items) >= args.max_results,
        "ladder": ladder,
        "ranked": ranked,
        "dropped": dropped,
        "shortlist": short,
        "fmv_batch": fmv_batch(short, title=args.title, issue=args.issue,
                               year=args.year, publisher=args.publisher),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="slab-deals",
        description="Rank active CGC listings of one book against its sale ladder.")
    p.add_argument("--title", required=True, help="Series title, e.g. 'Amazing Spider-Man'")
    p.add_argument("--issue", required=True)
    p.add_argument("--year", type=int, default=None, help="Cover year")
    p.add_argument("--comic-id", type=int, default=None,
                   help="comics-server comic id (skips ledger title/issue/year resolution)")
    p.add_argument("--publisher", default=None, help="Passed through to the comic-fmv batch")
    p.add_argument("--shortlist", type=int, default=DEFAULT_SHORTLIST,
                   help=f"Max listings handed to comic-fmv (default {DEFAULT_SHORTLIST})")
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    p.add_argument("--json", action="store_true", help="JSON instead of tables")
    p.add_argument("--fmv-batch", metavar="PATH",
                   help="Write the shortlist as a comic-fmv --batch file")
    args = p.parse_args(argv)
    try:
        result = run(args)
    except SlabDealsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.fmv_batch:
        with open(args.fmv_batch, "w") as f:
            json.dump(result["fmv_batch"], f, indent=2)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result["book"], result["ladder"], result["ranked"],
                     result["dropped"], result["shortlist"], result["truncated"]))


if __name__ == "__main__":
    main()
