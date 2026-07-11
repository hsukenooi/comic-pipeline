#!/usr/bin/env python3
"""ebay-sold-comps: fetch eBay sold listings for a comic via SerpApi.

Wraps SerpApi's eBay engine (with show_only=Sold), caches responses, dedupes
by product_id, applies hard-excludes, parses grades, and returns clean comp
lists. Consumed by comic-pipeline-fmv (apps/fmv) to compute fair market value.

Why this lives in apps/ebay (alongside ebay-fetch):
    All eBay data fetching — live (Browse API, ebay_fetch.py) and sold
    (SerpApi, this file) — belongs in the same app. Comic-specific FMV math
    and DB upsert live in apps/fmv; this file is the eBay side of that pipeline.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

import comic_identity

# ─── Configuration ────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "ebay-sold-comps"
DEFAULT_CACHE_TTL_SEC = 7 * 24 * 3600  # 7 days
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_TIMEOUT_SEC = 30
DEFAULT_MAX_WORKERS = 10

# Tier thresholds (the "tiered query strategy" from the FMV skill)
THIN_RESULTS_THRESHOLD = 5     # auto-broaden (drop year) if base returns fewer
GRADE_TAGGED_THRESHOLD = 10    # add grade-targeted query if base returns fewer

# Retry policy for transient SerpApi failures (Timeout, ConnectionError, 429/5xx)
FETCH_MAX_RETRIES = 3
FETCH_BACKOFF_BASE = 2  # seconds: sleep(FETCH_BACKOFF_BASE ** attempt)


# ─── SERPAPI_KEY loader ──────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE per line, no quoting). Comments + blanks ok."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_serpapi_key() -> str:
    """Resolve SERPAPI_KEY from env, then apps/ebay/.env."""
    key = os.environ.get("SERPAPI_KEY")
    if key:
        return key
    app_root = Path(__file__).parent.parent
    for env_path in (app_root / ".env", app_root / ".env.local"):
        env = _load_dotenv(env_path)
        if env.get("SERPAPI_KEY"):
            return env["SERPAPI_KEY"]
    print(
        "Error: SERPAPI_KEY not found.\n"
        f"Set the env var or put it in {app_root}/.env",
        file=sys.stderr,
    )
    sys.exit(2)


# ─── Cache layer ──────────────────────────────────────────────────────────────

def _cache_path(canonical_url: str) -> Path:
    digest = hashlib.sha256(canonical_url.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _cache_get(path: Path, ttl_sec: int) -> dict | None:
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_sec:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001  # cache read — corrupt/partial file, return None
        return None


def _cache_put(path: Path, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


# ─── Query construction ──────────────────────────────────────────────────────

def _publisher_qualifier(publisher: str | None) -> str | None:
    """Normalize a publisher into the query qualifier keyword to append.

    BUI-304 (issue 2): for the "big two" we emit the canonical "marvel comics"
    / "dc comics" — a cheap disambiguator that keeps the *year-less* base query
    (per /comic:buy's convention of omitting year to dodge the BUI-129
    collection-check false-negative) from colliding with modern media that
    reuses the issue number: e.g. "X-Men 97" vs the 2024 "X-Men '97" show's
    merchandise. Indie publishers pass through unchanged — the caller already
    supplies the noise-filtering name ("image comics", "dark horse"), which is
    the primary indie noise filter (BUI-161). Returns None for an absent/blank
    publisher so the base query is untouched.
    """
    if not publisher or not publisher.strip():
        return None
    p = publisher.strip()
    if re.search(r"\bmarvel\b", p, re.IGNORECASE):
        return "marvel comics"
    if re.search(r"\bdc\b", p, re.IGNORECASE):
        return "dc comics"
    return p


def build_query(title: str, issue: str, year: int | None = None,
                publisher: str | None = None, variant: str | None = None,
                grade_label: str | None = None,
                exclude_graded: bool = True) -> str:
    """Build the _nkw search string. Returns the raw (unencoded) keyword string."""
    parts = [f'"{title} {issue}"']
    if year:
        parts.append(str(year))
    # BUI-304 (issue 1): append the distribution variant (e.g. "Newsstand",
    # "Direct") as a query keyword, mirroring the publisher mechanism below.
    # Previously `variant` was DB-only (distinct comic_id per BUI-28) and never
    # reached the search — so a plain "X-Men 123" blended newsstand + direct
    # copies, and after grade-parsing losses too few remained attributable to
    # either sub-market. Guard for empty/None so the base query is unchanged
    # (byte-for-byte) when variant is absent.
    variant = variant.strip() if variant else ""
    if variant:
        parts.append(variant)
    # BUI-304 (issue 2): the publisher qualifier — indie passes through, Marvel/
    # DC normalize to "marvel comics"/"dc comics" (see _publisher_qualifier).
    qualifier = _publisher_qualifier(publisher)
    if qualifier:
        parts.append(qualifier)
    if grade_label:
        parts.append(grade_label)
    if exclude_graded:
        parts.extend(["-cgc", "-cbcs", "-graded", "-slab"])
    return " ".join(parts)


def canonical_serpapi_url(nkw: str) -> str:
    """Build the SerpApi URL with deterministic param order (for cache key).

    Excludes api_key from the canonical form so we don't tie cache to a
    specific user's key. The actual request URL adds api_key separately.
    """
    params = {
        "engine": "ebay",
        "_nkw": nkw,
        "show_only": "Sold",
    }
    canonical = urllib.parse.urlencode(sorted(params.items()))
    return f"{SERPAPI_ENDPOINT}?{canonical}"


def request_url(canonical: str, api_key: str) -> str:
    sep = "&" if "?" in canonical else "?"
    return f"{canonical}{sep}api_key={api_key}"


# ─── Fetch (with cache + URL verification) ────────────────────────────────────

class SerpApiError(Exception):
    pass


def fetch(nkw: str, api_key: str, *, force: bool = False,
          ttl_sec: int = DEFAULT_CACHE_TTL_SEC) -> tuple[dict, bool]:
    """Fetch a SerpApi response with caching. Returns (data, cache_hit)."""
    canonical = canonical_serpapi_url(nkw)
    path = _cache_path(canonical)

    if not force:
        cached = _cache_get(path, ttl_sec)
        if cached is not None:
            return cached, True

    last_exc: Exception | None = None
    for attempt in range(FETCH_MAX_RETRIES):
        try:
            resp = requests.get(request_url(canonical, api_key), timeout=SERPAPI_TIMEOUT_SEC)
            resp.raise_for_status()
            break  # success — exit retry loop
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and (status == 429 or status >= 500):
                last_exc = exc
            else:
                raise  # non-retryable 4xx — propagate immediately
        if attempt < FETCH_MAX_RETRIES - 1:
            time.sleep(FETCH_BACKOFF_BASE ** attempt)
    else:
        # All attempts exhausted — re-raise the last transient exception
        raise last_exc  # type: ignore[misc]

    data = resp.json()

    if "error" in data:
        raise SerpApiError(f"SerpApi error: {data['error']}")

    # Verify the eBay URL actually has LH_Sold=1 — SerpApi silently drops
    # LH_* params if you pass them directly, and a missing sold filter
    # returns active listings (FMV will be wrong, typically far too low).
    ebay_url = data.get("search_metadata", {}).get("ebay_url", "")
    if "LH_Sold=1" not in ebay_url:
        raise SerpApiError(
            f"Sold filter not applied — eBay URL missing LH_Sold=1.\n"
            f"  ebay_url={ebay_url}\n"
            f"  query={nkw}\n"
            "Use show_only=Sold (LH_Sold=1 / LH_Complete=1 are silently dropped)."
        )

    _cache_put(path, data)
    return data, False


# ─── Hard excludes ────────────────────────────────────────────────────────────
#
# BUI-269: the lot/reprint/foreign-edition/trading-card checks that used to
# live in this regex are now sourced from comic_identity.is_comp_excluded()
# (apps/ebay/src/comic_identity.py) — that module is the single source of
# truth, reconciling this lexicon with the near-identical one seller_scan.py
# used to hand-maintain (BUI-253). What remains here is condition/grading/
# damage exclusion with no analog in comic_identity: it isn't about comic
# *identity* at all, so it stays local to the FMV comp pipeline.

LOCAL_EXCLUDE_RE = re.compile(
    r'''
    coverless | no\s+cover | cover\s+torn | cvr\s+off | detached\s+cover |
    missing\s+pages? | missing\s+pin | missing\s+wrap |
    vol[\s.]?[2-9] | \bv[2-9]\b |
    \bpsa\b | \bpgx\b |
    signed\s+by | stan\s+lee.*sign | signature\s+series |
    ww\s+live\s+sale | space\s+filler | restored | water.?stain
    ''',
    re.IGNORECASE | re.VERBOSE,
)


def hard_exclude(title: str) -> bool:
    return comic_identity.is_comp_excluded(title) or bool(LOCAL_EXCLUDE_RE.search(title))


# ─── Grade parsing ────────────────────────────────────────────────────────────

# Fixed numeric regex: covers the full CGC scale including 9.2/9.4/9.6/9.9.
# The previous form `\b([0-9]\.[058])\b` silently dropped those.
#
# BUI-183: exclude price/measurement context.
#   Negative lookbehinds (fixed-width):
#     (?<!\$)  — reject when preceded by a dollar sign (price: $9.5)
#     (?<!x )(?<!X )  — reject when preceded by "x " (second number in a
#                       dimension pair: 2.5 x 3.5); requires exactly one space
#                       so "X-Men" (hyphen, not space) is unaffected.
#   Negative lookahead:
#     (?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["']))
#     — reject when the number is immediately followed (past optional whitespace)
#       by a measurement or shipping unit.  `x\b` catches the first number in a
#       dimension pair ("2.5 x"); word boundary on each unit prevents false
#       matches inside longer words.
_NUMERIC_GRADE_RE = re.compile(
    r'(?<!\$)(?<!x )(?<!X )'
    r'\b([0-9]\.[02-9])'
    r'(?!\w)'  # restore the original trailing boundary: a digit/letter immediately
               # after (e.g. "9.50", "5.50 dollars") is a price/number, not a grade
    r'(?!\s*(?:in(?:ch(?:es?)?)?\b|cm\b|mm\b|lbs?\b|oz\b|x\b|ship(?:ping)?\b|["\']))'
)

# Letter combos — most specific first. Order matters: slash-combos (e.g.
# VF/NM) must be checked before their single-letter components (NM), since
# `\bnm\b` would otherwise match inside "VF/NM" and short-circuit the loop.
#
# Boundary note: `\b` requires a word↔non-word transition. For patterns
# ending in non-word characters like `+` or `-`, a trailing `\b` fails when
# the next char is whitespace or end-of-string (both non-word). Use `(?!\w)`
# for trailing boundaries on non-word tails.
_LETTER_PATTERNS = [
    # Tier 1 — slash combos (longest first)
    (re.compile(r'\bnm[/\\]m\b', re.I), 9.6),
    (re.compile(r'\bvf[/\\]nm\b', re.I), 9.0),
    (re.compile(r'\bfn[/\\]vf\b|\bfine[/\\]vf\b|\bfvf\b', re.I), 7.0),
    (re.compile(r'\bvg[/\\]fn\+(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg[/\\]fn\b', re.I), 5.0),
    (re.compile(r'\bgd[/\\]vg\b', re.I), 3.0),
    (re.compile(r'\bfr[/\\]gd\b', re.I), 1.5),

    # Tier 2 — letter + modifier (+ / -)
    (re.compile(r'\bnm\+(?!\w)', re.I), 9.6),
    (re.compile(r'\bnm-(?!\w)', re.I), 9.2),
    (re.compile(r'\bvf\+(?!\w)', re.I), 8.5),
    (re.compile(r'\bvf-(?!\w)', re.I), 7.5),
    (re.compile(r'\bfn\+(?!\w)|\bfine\+(?!\w)', re.I), 6.5),
    (re.compile(r'\bfn-(?!\w)|\bfine-(?!\w)', re.I), 5.5),
    (re.compile(r'\bvg\+(?!\w)', re.I), 4.5),
    (re.compile(r'\bvg-(?!\w)', re.I), 3.5),
    (re.compile(r'\bgd\+(?!\w)', re.I), 2.5),

    # Tier 3 — bare letters (must come last; other patterns would match inside)
    (re.compile(r'\bnm\b(?![+\-/\\])', re.I), 9.4),
    (re.compile(r'\bvf\b(?![+\-/\\])', re.I), 8.0),
    (re.compile(r'\bfn\b(?![+\-/\\])|\bfine\b(?![+\-/\\])', re.I), 6.0),
    (re.compile(r'\bvg\b(?![+\-/\\])|\bvery good\b', re.I), 4.0),
    (re.compile(r'\bgd\b(?![+\-/\\])|\bgood\b', re.I), 2.0),
    (re.compile(r'\bfr\b(?![+\-/\\])|\bfair\b', re.I), 1.0),
    (re.compile(r'\bpoor\b', re.I), 0.5),
]


def parse_grade(title: str) -> float | None:
    """Extract a numeric CGC-scale grade from a listing title, or None."""
    m = _NUMERIC_GRADE_RE.search(title)
    if m:
        v = float(m.group(1))
        if 0.5 <= v <= 10.0:
            return v
    for pattern, value in _LETTER_PATTERNS:
        if pattern.search(title):
            return value
    return None


# ─── Comp parsing ─────────────────────────────────────────────────────────────

def _parse_price(raw) -> float | None:
    if raw is None:
        return None
    s = re.sub(r'[^\d.]', '', str(raw).replace(',', ''))
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0.50 <= v <= 50000 else None


def parse_comp(result: dict) -> dict | None:
    """Convert a SerpApi organic_result into our normalized comp shape."""
    title = result.get("title", "")
    if not title:
        return None
    product_id = str(result.get("product_id") or result.get("item_id") or "")
    price_obj = result.get("price") or {}
    price = _parse_price(price_obj.get("extracted") or price_obj.get("raw"))
    if price is None:
        return None
    return {
        "product_id": product_id,
        "title": title,
        "price": price,
        "grade": parse_grade(title),
        "sold_date": result.get("sold_date", ""),
        "buying_format": result.get("buying_format", ""),
        "link": result.get("link", ""),
    }


# ─── Per-book pipeline (three-tier query strategy) ───────────────────────────

def fetch_book_comps(book: dict, api_key: str, *, force: bool = False,
                     ttl_sec: int = DEFAULT_CACHE_TTL_SEC) -> dict:
    """Run the three-tier query strategy for one book.

    1. Base query (always): "title issue" year publisher
    2. Auto-broaden (if base <5 results): drop year
    3. Grade-targeted (if <10 grade-tagged comps after parsing base): add grade label

    Tiers 2 and 3 are conditional. Most modern books need only tier 1.
    """
    title = book["title"]
    issue = str(book["issue"])
    year = book.get("year")
    publisher = book.get("publisher")
    variant = book.get("variant")  # BUI-304: now a query keyword, not DB-only
    self_id = str(book.get("item_id", ""))

    queries_used: list[dict] = []
    seen_ids: set[str] = set()
    if self_id:
        seen_ids.add(self_id)
    comps: list[dict] = []

    def _run(tier: str, nkw: str) -> int:
        try:
            data, cache_hit = fetch(nkw, api_key, force=force, ttl_sec=ttl_sec)
        except (SerpApiError, requests.RequestException) as e:
            queries_used.append({"tier": tier, "nkw": nkw, "error": str(e)})
            return 0
        added = 0
        for r in data.get("organic_results", []):
            comp = parse_comp(r)
            if comp is None or not comp["product_id"]:
                continue
            if comp["product_id"] in seen_ids:
                continue
            if hard_exclude(comp["title"]):
                continue
            seen_ids.add(comp["product_id"])
            comps.append(comp)
            added += 1
        queries_used.append({
            "tier": tier,
            "nkw": nkw,
            "raw_results": len(data.get("organic_results", [])),
            "new_comps": added,
            "cached": cache_hit,
            "ebay_url": data.get("search_metadata", {}).get("ebay_url", ""),
        })
        return added

    # Tier 1 — base
    base_nkw = build_query(title, issue, year=year, publisher=publisher,
                           variant=variant)
    _run("base", base_nkw)

    # Tier 2 — auto-broaden if thin
    if len(comps) < THIN_RESULTS_THRESHOLD and year:
        broader_nkw = build_query(title, issue, year=None, publisher=publisher,
                                  variant=variant)
        _run("broader", broader_nkw)

    # Tier 3 — grade-targeted if too few grade-tagged comps in pool so far
    target_grade = book.get("grade")
    if isinstance(target_grade, str):
        target_grade = parse_grade(target_grade)
    grade_tagged = sum(1 for c in comps if c["grade"] is not None)
    if target_grade is not None and grade_tagged < GRADE_TAGGED_THRESHOLD:
        label = _grade_label_for_query(target_grade)
        if label:
            grade_nkw = build_query(title, issue, year=year, publisher=publisher,
                                    variant=variant, grade_label=label)
            _run("grade-targeted", grade_nkw)

    out_input = {
        "item_id": self_id or None,
        "title": title,
        "issue": issue,
        "year": year,
        "publisher": publisher,
        "grade": target_grade,
    }
    # BUI-174/187: echo back the caller's correlation id (when present) so a
    # batch driver can map results to inputs by identity, not list position.
    # A bare item_id is not reliable (may be absent or shared), so the id is a
    # dedicated field threaded by the caller; standalone callers omit it.
    req_id = book.get("_req_id")
    if req_id is not None:
        out_input["_req_id"] = req_id
    return {
        "input": out_input,
        "queries_used": queries_used,
        "comps": comps,
    }


def _grade_label_for_query(grade: float) -> str | None:
    """Pick a coarse letter grade to add to a query. Stays inside the bucket
    that contains `grade` so the search doesn't drift away from the target."""
    if grade >= 9.0:
        return "NM"
    if grade >= 8.0:
        return "VF"
    if grade >= 7.0:
        return "VF"  # FN/VF tagging is rare; VF surfaces upper bracket
    if grade >= 6.0:
        return "FN"
    if grade >= 4.5:
        return "VG"
    if grade >= 3.0:
        return "GD"
    return None


# ─── Batch driver ─────────────────────────────────────────────────────────────

def run_batch(books: list[dict], api_key: str, *, force: bool = False,
              ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
              max_workers: int = DEFAULT_MAX_WORKERS) -> list[dict]:
    """Fan out across books with a thread pool."""
    results: list[dict] = [None] * len(books)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_book_comps, b, api_key, force=force, ttl_sec=ttl_sec): i
            for i, b in enumerate(books)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001  # batch boundary — capture per-book errors, continue
                book = books[i]
                results[i] = {
                    "input": book,
                    "queries_used": [],
                    "comps": [],
                    "error": str(e),
                }
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _print_human(results: list[dict]) -> None:
    for r in results:
        inp = r["input"]
        label = f"{inp['title']} #{inp['issue']}"
        if inp.get("year"):
            label += f" ({inp['year']})"
        if "error" in r:
            print(f"  {label}: ERROR {r['error']}")
            continue
        n_total = len(r["comps"])
        n_graded = sum(1 for c in r["comps"] if c["grade"] is not None)
        tiers = ",".join(q["tier"] for q in r["queries_used"])
        cached = sum(1 for q in r["queries_used"] if q.get("cached"))
        print(f"  {label}: {n_total} comps ({n_graded} grade-tagged) "
              f"tiers=[{tiers}] cached={cached}/{len(r['queries_used'])}")


def _read_batch(path: str) -> list[dict]:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text())


def _write_out(path: str | None, data) -> None:
    if path is None:
        return
    if path == "-":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    Path(path).write_text(json.dumps(data, indent=2))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="sold-comps",
        description="Fetch eBay sold listings for a comic via SerpApi.",
    )
    p.add_argument("--batch", help="Path to JSON batch file ('-' for stdin).")
    p.add_argument("--title", help="Series title (single-query mode).")
    p.add_argument("--issue", help="Issue number (single-query mode).")
    p.add_argument("--year", type=int, help="Cover year (single-query mode).")
    p.add_argument("--publisher", help="Publisher (recommended for indie titles).")
    p.add_argument("--variant", help="Distribution variant keyword (e.g. Newsstand).")
    p.add_argument("--grade", type=float, help="Target grade (single-query mode).")
    p.add_argument("--item-id", help="Self-exclude this product_id from comps.")
    p.add_argument("--out", help="Write full JSON to this path ('-' for stdout).")
    p.add_argument("--force", action="store_true",
                   help="Bypass cache and refetch.")
    p.add_argument("--cache-ttl-days", type=float, default=7.0,
                   help="Cache TTL in days (default: 7).")
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS,
                   help="Thread pool size for batch mode (default: 10).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress human summary on stdout.")
    args = p.parse_args(argv)

    api_key = load_serpapi_key()
    ttl_sec = int(args.cache_ttl_days * 24 * 3600)

    if args.batch:
        books = _read_batch(args.batch)
        if not isinstance(books, list):
            print("Error: batch file must contain a JSON array.", file=sys.stderr)
            return 2
    elif args.title and args.issue:
        books = [{
            "title": args.title,
            "issue": args.issue,
            "year": args.year,
            "publisher": args.publisher,
            "variant": args.variant,
            "grade": args.grade,
            "item_id": args.item_id,
        }]
    else:
        p.error("provide --batch <file> or (--title and --issue)")

    results = run_batch(books, api_key, force=args.force, ttl_sec=ttl_sec,
                        max_workers=args.max_workers)

    if not args.quiet:
        _print_human(results)

    if args.out:
        _write_out(args.out, results)
    elif args.quiet:
        # Quiet + no --out is a misuse; emit JSON to stdout so callers get something.
        _write_out("-", results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
