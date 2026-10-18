#!/usr/bin/env python3
"""BUI-1229: the PriceCharting slab test set. Reusable, read-only.

Builds a dated set of PriceCharting (PC) graded sales for 48 approved books and
exposes it to the follow-up audits (BUI-1230..1233) through one function:

    sys.path.insert(0, "<repo>/docs/audit")
    from pc_slab_test_set import load_test_set
    ts = load_test_set()            # {"meta": ..., "books": [...], "sales": [...]}
    universal = [s for s in ts["sales"] if s["universal"]]

`load_test_set()` reads the JSON artifact at `ARTIFACT` (outside the repo: PC
price data is not committed). Build or rebuild it with:

    uv run --project plugins/gixen-overlay python docs/audit/pc_slab_test_set.py fetch
    uv run --project plugins/gixen-overlay python docs/audit/pc_slab_test_set.py build

`fetch` scrapes each page once with the firecrawl CLI into `PAGES_DIR` (skips
pages already cached). `build` parses the cache, verifies every page's title
names the expected series, issue, and year, and writes `ARTIFACT`. A book whose
page is missing or whose title does not match is skipped and listed in
`meta.skipped`. Schema: see `2026-10-11-pc-slab-test-set.md`.

Nothing here touches the comics server DB except `dedup_against_ledger`, which
opens it with `mode=ro`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "fmv" / "src"))
sys.path.insert(0, str(ROOT / "apps" / "ebay" / "src"))
import fmv_math  # noqa: E402
import sold_comps  # noqa: E402

FIXTURES = Path.home() / "comic-grader-fixtures" / "bui-1229-pc-slab-test-set"
PAGES_DIR = FIXTURES / "pages"
ARTIFACT = FIXTURES / "test_set.json"
DB = Path.home() / ".comics-server" / "db.sqlite"
SCHEMA_VERSION = 1

# comic_id -> (PC slug under /game/comic-books-, series, issue, year, known)
# known=True: one of the 16 BUI-1218 books. One obvious fix each for three slugs
# (2026-10-11): 1026 was the Facsimile Edition console in the spike (wrong
# product; now the original `ultimate-fallout/`), 667 needed `the-`, and 720's
# series dir is `captain-america-annual/` (the guessed slugs hit PC's search page).
BOOKS: dict[int, tuple[str, str, str, int | None, bool]] = {
    28: ("amazing-spider-man/amazing-spider-man-194-1979", "Amazing Spider-Man", "194", 1979, True),
    1024: ("giant-size-x-men/giant-size-x-men-1-1975", "Giant-Size X-Men", "1", 1975, True),
    685: ("amazing-spider-man/amazing-spider-man-50-1967", "Amazing Spider-Man", "50", 1967, True),
    783: ("x-men/x-men-101-1976", "X-Men", "101", 1976, True),
    727: ("fantastic-four/fantastic-four-49-1966", "Fantastic Four", "49", 1966, True),
    745: ("iron-man/iron-man-55-1973", "Iron Man", "55", 1973, True),
    661: ("x-men/the-x-men-96-1975", "X-Men", "96", 1975, True),
    573: ("amazing-spider-man/amazing-spider-man-78-1969", "Amazing Spider-Man", "78", 1969, True),
    1023: ("fantastic-four/fantastic-four-48-1966", "Fantastic Four", "48", 1966, True),
    1021: ("batman/batman-227-1970", "Batman", "227", 1970, True),
    717: ("batman/batman-655-2006", "Batman", "655", 2006, True),
    1026: ("ultimate-fallout/ultimate-fallout-4-2011", "Ultimate Fallout", "4", 2011, True),
    878: ("invincible/invincible-84-2011", "Invincible", "84", 2011, True),
    798: ("x-force/x-force-11-1992", "X-Force", "11", 1992, True),
    1019: ("avengers/avengers-87-1971", "Avengers", "87", 1971, True),
    511: ("x-men/the-x-men-12-1965", "X-Men", "12", 1965, True),
    14: ("amazing-spider-man/amazing-spider-man-72-1969", "Amazing Spider-Man", "72", 1969, False),
    762: ("tales-to-astonish/tales-to-astonish-63-1965", "Tales to Astonish", "63", 1965, False),
    9: ("amazing-spider-man/amazing-spider-man-45-1967", "Amazing Spider-Man", "45", 1967, False),
    119: ("amazing-spider-man/amazing-spider-man-44-1967", "Amazing Spider-Man", "44", 1967, False),
    719: ("captain-america/captain-america-100-1968", "Captain America", "100", 1968, False),
    504: ("amazing-spider-man/amazing-spider-man-19-1964", "Amazing Spider-Man", "19", 1964, False),
    859: ("amazing-spider-man/amazing-spider-man-17-1964", "Amazing Spider-Man", "17", 1964, False),
    227: ("fantastic-four/fantastic-four-72-1968", "Fantastic Four", "72", 1968, False),
    723: ("daredevil/daredevil-16-1966", "Daredevil", "16", 1966, False),
    764: ("x-men/the-x-men-13-1965", "X-Men", "13", 1965, False),
    730: ("fantastic-four/fantastic-four-67-1967", "Fantastic Four", "67", 1967, False),
    510: ("amazing-spider-man/amazing-spider-man-20-1965", "Amazing Spider-Man", "20", 1965, False),
    500: ("amazing-spider-man/amazing-spider-man-14-1964", "Amazing Spider-Man", "14", 1964, False),
    494: ("amazing-spider-man/amazing-spider-man-5-1963", "Amazing Spider-Man", "5", 1963, False),
    505: ("amazing-spider-man/amazing-spider-man-6-1963", "Amazing Spider-Man", "6", 1963, False),
    1022: ("silver-surfer/silver-surfer-4-1969", "Silver Surfer", "4", 1969, False),
    972: ("amazing-spider-man/amazing-spider-man-94-1971", "Amazing Spider-Man", "94", 1971, False),
    957: ("amazing-spider-man/amazing-spider-man-87-1970", "Amazing Spider-Man", "87", 1970, False),
    1057: ("amazing-spider-man/amazing-spider-man-96-1971", "Amazing Spider-Man", "96", 1971, False),
    748: ("marvel-feature/marvel-feature-1-1975", "Marvel Feature", "1", 1975, False),
    664: ("x-men/x-men-102-1976", "X-Men", "102", 1976, False),
    667: ("x-men/the-x-men-108-1977", "X-Men", "108", 1977, False),
    705: ("daredevil/daredevil-146-1977", "Daredevil", "146", 1977, False),
    825: ("uncanny-x-men/uncanny-x-men-281-1991", "Uncanny X-Men", "281", 1991, False),
    720: ("captain-america-annual/captain-america-annual-8-1986", "Captain America Annual", "8", 1986, False),
    18: ("invincible/invincible-19-2004", "Invincible", "19", 2004, False),
    1025: ("invincible/invincible-1-2003", "Invincible", "1", 2003, False),
    749: ("new-x-men/new-x-men-128-2002", "New X-Men", "128", 2002, False),
    753: ("spawn/spawn-226-2013", "Spawn", "226", 2013, False),
    30: ("invincible/invincible-61-2009", "Invincible", "61", 2009, False),
    968: ("invincible/invincible-120-2015", "Invincible", "120", 2015, False),
    31: ("invincible/invincible-51-2008", "Invincible", "51", 2008, False),
}

# Books dropped after inspection, with the reason (reported in meta.skipped).
DROPPED: dict[int, str] = {
    748: "mixed_volumes: PC page and ledger both mix Marvel Feature #1 (1971, Defenders)"
         " with Marvel Feature #1 (1975, Red Sonja), so neither side is one book",
}


def era(year: int | None) -> str:
    """Overstreet-style age band of the cover year."""
    if year is None:
        return "unknown"
    if year < 1956:
        return "golden"
    if year < 1970:
        return "silver"
    if year < 1985:
        return "bronze"
    if year < 1992:
        return "copper"
    return "modern"


def page_path(slug: str) -> Path:
    return PAGES_DIR / f"comic-books-{slug.replace('/', '__')}.md"


# ─── fetch ──────────────────────────────────────────────────────────────────

def fetch(only: list[int] | None = None) -> None:
    """Scrape each book page once with the firecrawl CLI (cached; never refetch)."""
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    for cid, (slug, *_rest) in BOOKS.items():
        if only and cid not in only:
            continue
        out = page_path(slug)
        if out.exists() and out.stat().st_size > 0:
            continue
        url = f"https://www.pricecharting.com/game/comic-books-{slug}"
        err = ""
        for attempt in range(3):  # firecrawl rate-limits bursts; back off and retry
            r = subprocess.run(["firecrawl", "scrape", url, "-o", str(out)],
                               capture_output=True, text=True, timeout=180)
            if out.exists() and out.stat().st_size > 0:
                break
            err = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            err = err[0][:120]
            time.sleep(20 * (attempt + 1))
        size = out.stat().st_size if out.exists() else 0
        print(f"{cid} {slug} {size}B {page_title(out) or '-'} {err if not size else ''}", flush=True)


# ─── title verification ─────────────────────────────────────────────────────

H1_RE = re.compile(r"^# (.+)$", re.M)


def page_title(path: Path) -> str | None:
    if not path.exists():
        return None
    m = H1_RE.search(path.read_text(errors="replace"))
    if not m:
        return None
    # firecrawl escapes '#' and appends the console (series) link:
    #   "X \#194 (1979)   [X](...)" -> "X #194 (1979) | console: X"
    # The console name is kept: it is where PC marks a facsimile or variant line.
    h1 = m.group(1).replace("\\#", "#")
    cm = re.search(r"\[([^\]]+)\]\(", h1)
    base = re.sub(r"\s*\[.*$", "", h1).strip()
    return f"{base} | console: {cm.group(1)}" if cm else base


def _norm(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def verify_title(title: str | None, series: str, issue: str, year: int | None) -> str | None:
    """None when the page H1 names the book, else a short mismatch reason.
    `title` is `page_title`'s 'Amazing Spider-Man #194 (1979) | console: ...'."""
    if not title:
        return "no_page_or_h1"
    m = re.match(r"(.+?)\s+#(\S+)\s*(?:\((\d{4})\))?", title)
    if not m:
        return f"unparsed_h1:{title[:60]}"
    t_series, t_issue, t_year = m.group(1), m.group(2), m.group(3)
    if _norm(t_series) != _norm(series):
        return f"series:{t_series}"
    if t_issue.lstrip("0") != issue.lstrip("0"):
        return f"issue:{t_issue}"
    if year is not None and t_year and int(t_year) != year:
        return f"year:{t_year}"
    rest = title[m.end():].lower()
    if re.search(r"\b(2nd|3rd|second|third|facsimile|variant|reprint)\b", rest):
        return f"edition:{title[:80]}"
    return None


# ─── parse ──────────────────────────────────────────────────────────────────

ROW_RE = re.compile(r"^\| (\d{4}-\d\d-\d\d) \|")
TITLE_RE = re.compile(r"\[(.+?)\]\((https?://[^)\s]+)\)")
PRICE_RE = re.compile(r"\$([\d,]+\.\d\d)")
ITM_RE = re.compile(r"/itm/(\d+)")
GRADED_RE = re.compile(r"\b(cgc|cbcs)\b", re.I)


PC_SUFFIX_RE = re.compile(r"(?:\s+\d{4})?\s+#\S+\s*$")


def issue_mismatch(title: str, series: str, issue: str) -> bool:
    """True when the title names a DIFFERENT issue right after the series name
    ("Amazing Spider-Man 105 CGC 9.2 ... 1967 #45" filed on the #45 page). PC
    appends "<year> #<issue>" to some titles, so that suffix is stripped first.
    Anchors on the series' last word; a grade ("9.8") or a year never counts."""
    t = PC_SUFFIX_RE.sub("", title.replace("\\", ""))
    last = re.split(r"[\s-]+", series.lower())[-1]
    m = re.search(rf"\b{re.escape(last)}\b\s*(?:#|no\.?)?\s*(\d+)(?![\d.]|(?:st|nd|rd|th)\b)", t, re.I)
    return bool(m) and len(m.group(1)) < 4 and m.group(1).lstrip("0") != issue.lstrip("0")


def parse_page(path: Path, issue: str, series: str) -> tuple[list[dict], dict]:
    """Every graded (CGC or CBCS) sale row on the page, all labels kept.

    Unlike the BUI-1218 spike's `parse_pc_page`, nothing graded is dropped for
    its label or title: each row carries `certifier`, `label`, `page_quality`,
    and `exclude_code` (None, or the reason the live slab pool would drop it),
    and `universal` marks the rows the live CGC universal pool admits.
    `price` is the first price in the cell (PC's sold or best-offer accepted
    amount); `ask` is the second (the struck-through best-offer list price).
    """
    sales, seen = [], set()
    st = {"rows": 0, "no_price": 0, "not_graded": 0, "no_grade": 0, "dup_item": 0}
    for line in path.read_text(errors="replace").splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        st["rows"] += 1
        body = re.sub(r"!\[\]\([^)]*\)<br>Time Warp.*?OK\]\([^)]*\)", "", line)
        tm = TITLE_RE.search(body)
        if not tm:
            st["no_price"] += 1
            continue
        title, url = tm.group(1), tm.group(2)
        after = body[tm.end():].split("[Report It]")[0]
        prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(after)]
        if not prices:
            st["no_price"] += 1
            continue
        if not GRADED_RE.search(title):
            st["not_graded"] += 1
            continue
        grade = sold_comps.parse_grade(title)
        if grade is None:
            st["no_grade"] += 1
            continue
        f = sold_comps.parse_slab_fields(title)
        im = ITM_RE.search(url)
        # stable across runs (the spike's builtin hash() is per-process salted)
        pid = im.group(1) if im else "pc:" + hashlib.sha1(
            f"{m.group(1)}|{title}|{prices[0]}".encode()).hexdigest()[:12]
        if pid in seen:
            st["dup_item"] += 1
            continue
        seen.add(pid)
        code = None
        if sold_comps.hard_exclude(title, graded_target=f["certifier"]):
            code = "hard_exclude"
        else:
            code = sold_comps.graded_identity_exclude(title, issue=issue)
        if code is None and issue_mismatch(title, series, issue):
            code = "issue_mismatch"
        sales.append({
            "product_id": pid, "title": title, "url": url,
            "sold_date": m.group(1), "price": prices[0],
            "ask": prices[1] if len(prices) > 1 else None,
            "best_offer": len(prices) > 1,
            "grade": grade, "certifier": f["certifier"], "label": f["label"],
            "page_quality": f["page_quality"], "exclude_code": code,
            "universal": f["certifier"] == "cgc" and f["label"] == "universal" and code is None,
            "source": "heritage" if "ha.com" in url else ("ebay" if "ebay.com" in url else "other"),
        })
    return sales, st


# ─── ledger dedup (read-only) ───────────────────────────────────────────────

def load_ledger(conn: sqlite3.Connection, comic_id: int) -> list[dict]:
    """Ledger CGC universal slab comps for one book (same filter as the spike)."""
    rows = conn.execute(
        """SELECT provider, product_id, title, price, sold_date, grade, page_quality,
                  first_seen_at, best_offer
           FROM comps WHERE comic_id=? AND pool='slab' AND certifier='cgc'
             AND label='universal' AND excluded_code IS NULL AND price > 0
             AND grade IS NOT NULL""", (comic_id,)).fetchall()
    # BUI-1281: carry the stored `comps.best_offer` flag so `fmv_math.graded_pool`
    # applies its x0.89 scale as production does. A product_id seen twice keeps the
    # flag if any copy has it (upsert fills it on a later sighting); NULL is unflagged.
    flagged = {r[1] for r in rows if r[8]}
    out, seen = [], set()
    for r in rows:
        d = dict(zip(("provider", "product_id", "title", "price", "sold_date", "grade",
                      "page_quality", "first_seen_at", "best_offer"), r))
        if d["product_id"] in seen:
            continue
        seen.add(d["product_id"])
        d["best_offer"] = d["product_id"] in flagged
        d["_date"] = (fmv_math._parse_sold_date(d["sold_date"])
                      or fmv_math._parse_sold_date(d["first_seen_at"]))
        d["sold_date"] = d["_date"].isoformat() if d["_date"] else None
        out.append(d)
    return out


def same_sale(pc: dict, c: dict) -> bool:
    """True when ledger row `c` is the PC sale `pc`: same eBay item id, else same
    grade, date within a day, and price within 3% of PC's sold OR list price
    (the ledger holds list price on best-offer sales, BUI-1222). The spike's
    `is_dup` compared only the sold price, so it missed best-offer twins."""
    if c["product_id"] == pc["product_id"]:
        return True
    if c["grade"] != pc["grade"] or not c["_date"]:
        return False
    if abs((c["_date"] - date.fromisoformat(pc["sold_date"])).days) > 1:
        return False
    return any(p and abs(c["price"] - p) <= 0.03 * max(c["price"], p)
               for p in (pc["price"], pc["ask"]))


def ledger_match(pc: dict, ledger: list[dict]) -> dict | None:
    """The ledger row this PC sale duplicates (item-id match preferred), or None."""
    by_id = [c for c in ledger if c["product_id"] == pc["product_id"]]
    if by_id:
        return by_id[0]
    return next((c for c in ledger if same_sale(pc, c)), None)


# ─── truth hygiene ──────────────────────────────────────────────────────────

OUTLIER_LO, OUTLIER_HI, OUTLIER_DAYS, OUTLIER_MIN_N = 0.25, 4.0, 365, 3


# BUI-1243 fallback, used only when a row has fewer than OUTLIER_MIN_N same-grade
# neighbours. Price rises with grade, so each direction is one-sided: a row under
# ADJ_LO x the median of the book's sales within ADJ_SPAN grades below it, or over
# ADJ_HI x the median within ADJ_SPAN grades above it, is junk. Big grade jumps at
# the top (9.6 -> 9.8 can be 2.5x) only loosen this check, never falsely tighten it.
ADJ_SPAN, ADJ_LO, ADJ_HI = 1.0, 0.5, 2.0


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def flag_price_outliers(rows: list[dict]) -> None:
    """Set `price_outlier` on each universal row: True when its price is below
    0.25x or above 4x the median of the OTHER same-grade universal sales of the
    book within 365 days (at least 3 of them), False when inside. It catches
    gross mis-reads (a raw copy titled "CGC likely 9.6" sold for $0.71) without
    trimming real spread.

    With fewer than 3 same-grade neighbours (BUI-1243) it falls back to the
    adjacent-grade check (ADJ_*): True when the price is under 0.5x the median
    of the book's other sales within 1.0 grade below, or over 2x the median
    within 1.0 grade above (each side needs 3 sales in the same 365 days).
    False when neither fires and a side had enough sales, None when neither did.
    Rows are kept; scorers decide whether to drop them."""
    uni = [(r, date.fromisoformat(r["sold_date"]), float(r["grade"]))
           for r in rows if r["universal"]]
    for r in rows:
        r["price_outlier"] = None
    for r, d, g in uni:
        near = [(o["price"], og) for o, od, og in uni
                if o is not r and abs((od - d).days) <= OUTLIER_DAYS]
        nb = [p for p, og in near if og == g]
        if len(nb) >= OUTLIER_MIN_N:
            med = _median(nb)
            r["price_outlier"] = not (OUTLIER_LO * med <= r["price"] <= OUTLIER_HI * med)
            continue
        below = [p for p, og in near if g - ADJ_SPAN <= og <= g]
        above = [p for p, og in near if g <= og <= g + ADJ_SPAN]
        verdict = None
        if len(below) >= OUTLIER_MIN_N:
            verdict = r["price"] < ADJ_LO * _median(below)
        if len(above) >= OUTLIER_MIN_N:
            verdict = bool(verdict) or r["price"] > ADJ_HI * _median(above)
        r["price_outlier"] = verdict


def self_check_outliers() -> None:
    """BUI-1243 guard, run by `build`: a 9.8 at $5 (Silver Surfer #4) and a 9.8 at
    $415 (Invincible #1) over a thousands-dollar lower-grade ladder must flag, and a
    plausible 9.8 over the same ladder must not."""
    def mk(grade, price, day):
        return {"universal": True, "grade": grade, "price": price, "sold_date": day}
    ladder = [mk(9.0, 2900.0, "2024-05-14"), mk(9.0, 3000.0, "2024-08-16"),
              mk(9.2, 3100.0, "2024-06-01"), mk(9.2, 3300.0, "2024-07-01"),
              mk(9.4, 5000.0, "2024-04-01")]
    junk, ok = mk(9.8, 5.0, "2024-04-22"), mk(9.8, 7767.5, "2024-04-22")
    flag_price_outliers(ladder + [junk, ok])
    assert junk["price_outlier"] is True, "9.8 at $5 over a $3k ladder must flag"
    assert ok["price_outlier"] is False, "a plausible 9.8 over the same ladder must not"
    # $415 is 0.30x the lower-grade median (inside the 0.25x same-grade band)
    # but under the 0.5x adjacent floor
    inv = [mk(9.0, 1400.0, "2025-08-05"), mk(9.0, 1200.0, "2025-06-18"),
           mk(9.0, 995.87, "2025-03-02"), mk(9.2, 1500.0, "2025-04-13")]
    low = mk(9.8, 415.0, "2024-08-26")
    flag_price_outliers(inv + [low])
    assert low["price_outlier"] is True, "9.8 at $415 over a ~$1.2k ladder must flag"


# ─── build / load ───────────────────────────────────────────────────────────

def build() -> dict:
    self_check_outliers()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    books, sales, skipped = [], [], []
    for cid, (slug, series, issue, year, known) in BOOKS.items():
        path = page_path(slug)
        title = page_title(path)
        why = DROPPED.get(cid) or verify_title(title, series, issue, year)
        if why:
            skipped.append({"comic_id": cid, "slug": slug, "reason": why, "h1": title})
            continue
        rows, st = parse_page(path, issue, series)
        flag_price_outliers(rows)
        ledger = load_ledger(conn, cid)
        n_dup = 0
        for s in rows:
            hit = ledger_match(s, ledger) if s["universal"] else None
            s["comic_id"] = cid
            s["in_ledger"] = hit is not None
            s["ledger_product_id"] = hit["product_id"] if hit else None
            s["ledger_price"] = hit["price"] if hit else None
            n_dup += hit is not None
        books.append({
            "comic_id": cid, "series": series, "issue": issue, "year": year,
            "era": era(year), "known": known, "slug": slug, "pc_h1": title,
            "n_sales": len(rows), "n_universal": sum(s["universal"] for s in rows),
            "n_in_ledger": n_dup, "n_ledger_universal": len(ledger),
            "first_sale": min((s["sold_date"] for s in rows), default=None),
            "last_sale": max((s["sold_date"] for s in rows), default=None),
            "parse_stats": st,
        })
        sales.extend(rows)
    ts = {"meta": {"schema_version": SCHEMA_VERSION, "built": date.today().isoformat(),
                   "ticket": "BUI-1229", "pages_dir": str(PAGES_DIR),
                   "n_books": len(books), "n_sales": len(sales),
                   "n_universal": sum(s["universal"] for s in sales),
                   "skipped": skipped},
          "books": books, "sales": sales}
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(ts, indent=1))
    return ts


def load_test_set(path: Path | str | None = None) -> dict:
    """The built test set (read-only). Raises if it has not been built."""
    p = Path(path) if path else ARTIFACT
    if not p.exists():
        raise FileNotFoundError(f"{p} missing: run `pc_slab_test_set.py fetch` then `build`")
    ts = json.loads(p.read_text())
    if ts["meta"]["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema {ts['meta']['schema_version']} != {SCHEMA_VERSION}")
    return ts


def universal_sales(ts: dict, comic_id: int | None = None) -> list[dict]:
    """CGC universal-label sales the live slab pool would admit."""
    return [s for s in ts["sales"] if s["universal"]
            and (comic_id is None or s["comic_id"] == comic_id)]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "fetch":
        fetch([int(a) for a in sys.argv[2:]] or None)
    elif cmd == "build":
        t = build()
        print(json.dumps(t["meta"], indent=1))
        for b in t["books"]:
            print(f"{b['comic_id']:>5} {b['series']} #{b['issue']} ({b['year']}) {b['era']:<7} "
                  f"sales={b['n_sales']} universal={b['n_universal']} in_ledger={b['n_in_ledger']} "
                  f"span={b['first_sale']}..{b['last_sale']}")
    else:
        raise SystemExit(f"unknown command {cmd}")
