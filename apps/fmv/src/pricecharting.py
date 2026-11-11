"""PriceCharting CGC slab sales for the graded pool (BUI-1282).

BUI-1226 measured appending PriceCharting (PC) CGC universal sales to the slab
comp pool at a Winkler delta of -0.125 [-0.198, -0.071] on the BUI-1229 test
set (docs/audit/2026-10-18-slab-fmv-option-ranking.md). This module is the
production side of that append:

  * `fetch_book_sales` resolves a book's PC product page with no hand list
    (`candidate_urls`), scrapes it with the firecrawl CLI, accepts the page
    only when its H1 names the series, issue, and year (`verify_title`), and
    caches it outside the repo (`cache_dir`). A failure of any kind returns a
    non-"ok" status and a warning; the caller prices ledger-only and reports.
  * `append_rows` turns the CGC universal sales into pool comps, dropping any
    that `same_sale`-matches a live or ledger comp (the ledger copy wins).
  * `keep_appended` is the refusal guard: a row the ledger-only pool refuses
    stays refused unless the appended pool prices it `direct`.

PC sale rows never leave this machine: the caller must not post them to the
comps ledger or any server payload (PC's terms allow personal use only). Title
classification (grade, certifier, label, page quality, exclusion code) shells
out to `ebay-sold-comps --classify-titles`, so the slab title guards keep one
home in apps/ebay.

`FMV_PRICECHARTING=0` turns the whole append off.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path

import fmv_math

ENV_SWITCH = "FMV_PRICECHARTING"
ENV_CACHE_DIR = "FMV_PRICECHARTING_CACHE_DIR"
FIRECRAWL_BIN = "firecrawl"
CLASSIFY_BIN = "ebay-sold-comps"
BASE_URL = "https://www.pricecharting.com/game/"
PAGE_TTL_SEC = 7 * 24 * 3600        # a verified page is reused for a week
NEGATIVE_TTL_SEC = 24 * 3600        # an unresolved book is retried after a day
SCRAPE_TIMEOUT_SEC = 120
CLASSIFY_TIMEOUT_SEC = 60
CACHE_VERSION = 1

# Statuses `fetch_book_sales` returns. "ok" carries sales; the rest price
# ledger-only. FAILED_STATUSES are failures the run must report out loud.
OK = "ok"
DISABLED = "disabled"
NO_IDENTITY = "no_identity"
UNRESOLVED = "unresolved"
FAILED_STATUSES = ("scrape_failed", "classify_failed", "parse_failed", "error")


def enabled() -> bool:
    """The kill switch. On unless FMV_PRICECHARTING is 0/false/no/off."""
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in (
        "0", "false", "no", "off")


def cache_dir() -> Path:
    override = os.environ.get(ENV_CACHE_DIR)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "comic-fmv" / "pricecharting"


# ─── slug resolution ────────────────────────────────────────────────────────

_VOLUME_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def series_name(title: str) -> str:
    """The series as PC names it: a trailing volume year ("The Amazing
    Spider-Man (1963)", the form identify_data carries) is not part of it."""
    return _VOLUME_YEAR_RE.sub("", str(title)).strip()


def slugify(text: str) -> str:
    s = str(text).lower().replace("&", "and")
    s = re.sub(r"['’]", "", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def candidate_urls(series: str, issue: str, year: int) -> list[str]:
    """PC product URLs to try, most likely first. The series directory is the
    series slug without a leading article; the product slug is sometimes
    `the-`-prefixed (X-Men #96 is `x-men/the-x-men-96-1975`). Annuals get
    their own directory (`captain-america-annual/`), which the series name
    already carries."""
    base = re.sub(r"^the-", "", slugify(series))
    iss = slugify(issue)
    if not base or not iss:
        return []
    out = [f"{BASE_URL}comic-books-{base}/{base}-{iss}-{year}",
           f"{BASE_URL}comic-books-{base}/the-{base}-{iss}-{year}"]
    return list(dict.fromkeys(out))


# ─── title verification ─────────────────────────────────────────────────────

H1_RE = re.compile(r"^# (.+)$", re.M)


def page_title(text: str) -> str | None:
    """The page H1, as 'X #194 (1979) | console: X' (firecrawl escapes '#'
    and appends the console link, which is where PC marks a facsimile)."""
    m = H1_RE.search(text or "")
    if not m:
        return None
    h1 = m.group(1).replace("\\#", "#")
    cm = re.search(r"\[([^\]]+)\]\(", h1)
    base = re.sub(r"\s*\[.*$", "", h1).strip()
    return f"{base} | console: {cm.group(1)}" if cm else base


def _norm(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def verify_title(title: str | None, series: str, issue: str,
                 year: int | None) -> str | None:
    """None when the H1 names the book, else a short mismatch reason. A year
    is required: without one a reprint page could pass."""
    if not title:
        return "no_page_or_h1"
    m = re.match(r"(.+?)\s+#(\S+)\s*(?:\((\d{4})\))?", title)
    if not m:
        return f"unparsed_h1:{title[:60]}"
    t_series, t_issue, t_year = m.group(1), m.group(2), m.group(3)
    if _norm(t_series) != _norm(series):
        return f"series:{t_series}"
    if t_issue.lstrip("0") != str(issue).lstrip("0"):
        return f"issue:{t_issue}"
    if year is None or not t_year or int(t_year) != int(year):
        return f"year:{t_year}"
    rest = title[m.end():].lower()
    if re.search(r"\b(2nd|3rd|second|third|facsimile|variant|reprint)\b", rest):
        return f"edition:{title[:80]}"
    return None


# ─── page parse ─────────────────────────────────────────────────────────────

ROW_RE = re.compile(r"^\| (\d{4}-\d\d-\d\d) \|")
TITLE_RE = re.compile(r"\[(.+?)\]\((https?://[^)\s]+)\)")
PRICE_RE = re.compile(r"\$([\d,]+\.\d\d)")
ITM_RE = re.compile(r"/itm/(\d+)")
GRADED_RE = re.compile(r"\b(cgc|cbcs)\b", re.I)
PHOTO_LOCK_RE = re.compile(r"!\[\]\([^)]*\)<br>Time Warp.*?OK\]\([^)]*\)")
PC_SUFFIX_RE = re.compile(r"(?:\s+\d{4})?\s+#\S+\s*$")


def parse_rows(text: str) -> list[dict]:
    """Every graded (CGC or CBCS) sale row on the page, unclassified.

    `price` is the first price in the cell (the sold or best-offer accepted
    amount); `ask` is the second (the struck-through best-offer list price).
    """
    rows, seen = [], set()
    for line in (text or "").splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        body = PHOTO_LOCK_RE.sub("", line)
        tm = TITLE_RE.search(body)
        if not tm:
            continue
        title, url = tm.group(1), tm.group(2)
        after = body[tm.end():].split("[Report It]")[0]
        prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(after)]
        if not prices or not GRADED_RE.search(title):
            continue
        im = ITM_RE.search(url)
        pid = im.group(1) if im else "pc:" + hashlib.sha1(
            f"{m.group(1)}|{title}|{prices[0]}".encode()).hexdigest()[:12]
        if pid in seen:
            continue
        seen.add(pid)
        rows.append({
            "product_id": pid, "title": title, "url": url,
            "sold_date": m.group(1), "price": prices[0],
            "ask": prices[1] if len(prices) > 1 else None,
            "source": ("heritage" if "ha.com" in url
                       else "ebay" if "ebay.com" in url else "other"),
        })
    return rows


def issue_mismatch(title: str, series: str, issue: str) -> bool:
    """True when the title names a DIFFERENT issue right after the series name
    ("Amazing Spider-Man 105 CGC 9.2 ... 1967 #45" filed on the #45 page). PC
    appends "<year> #<issue>" to some titles, so that suffix is stripped
    first. Anchors on the series' last word; a grade or a year never counts."""
    t = PC_SUFFIX_RE.sub("", title.replace("\\", ""))
    last = re.split(r"[\s-]+", series.lower())[-1]
    m = re.search(rf"\b{re.escape(last)}\b\s*(?:#|no\.?)?\s*(\d+)"
                  r"(?![\d.]|(?:st|nd|rd|th)\b)", t, re.I)
    if m is None:
        return False
    return len(m.group(1)) < 4 and m.group(1).lstrip("0") != str(issue).lstrip("0")


def classify(rows: list[dict], issue: str) -> list[dict]:
    """Grade/certifier/label/page_quality/exclude_code per row, from
    `ebay-sold-comps --classify-titles`. Raises RuntimeError on any failure."""
    if not rows:
        return []
    if shutil.which(CLASSIFY_BIN) is None:
        raise RuntimeError(f"{CLASSIFY_BIN} not on PATH")
    payload = json.dumps([{"title": r["title"], "issue": issue} for r in rows])
    try:
        proc = subprocess.run([CLASSIFY_BIN, "--classify-titles", "-"],
                              input=payload, capture_output=True, text=True,
                              timeout=CLASSIFY_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{CLASSIFY_BIN} --classify-titles: {e}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"{CLASSIFY_BIN} --classify-titles exited "
                           f"{proc.returncode}: {tail[0][:160]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CLASSIFY_BIN} --classify-titles: bad JSON") from e
    if not isinstance(out, list) or len(out) != len(rows):
        raise RuntimeError(f"{CLASSIFY_BIN} --classify-titles returned "
                           f"{len(out) if isinstance(out, list) else 'non-list'}"
                           f" results for {len(rows)} titles")
    return out


def build_sales(rows: list[dict], verdicts: list[dict], *, series: str,
                issue: str) -> list[dict]:
    """Join parsed rows with their classification. `universal` marks the rows
    the live CGC universal slab pool would admit."""
    sales = []
    for r, v in zip(rows, verdicts):
        grade = v.get("grade")
        if grade is None or v.get("certifier") is None:
            continue
        code = v.get("exclude_code")
        if code is None and issue_mismatch(r["title"], series, issue):
            code = "issue_mismatch"
        sales.append({
            **r, "grade": float(grade), "certifier": v["certifier"],
            "label": v.get("label"), "page_quality": v.get("page_quality"),
            "best_offer": r["ask"] is not None, "exclude_code": code,
            "universal": (v["certifier"] == "cgc" and v.get("label") == "universal"
                          and code is None),
        })
    return sales


# ─── scrape + cache ─────────────────────────────────────────────────────────

def _cache_path(series: str, issue: str, year: int) -> Path:
    return cache_dir() / f"{slugify(f'{series}-{issue}-{year}')}.json"


def _cache_read(path: Path, now: float) -> dict | None:
    try:
        entry = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("version") != CACHE_VERSION:
        return None
    ttl = PAGE_TTL_SEC if entry.get("status") == OK else NEGATIVE_TTL_SEC
    fetched = entry.get("fetched_at")
    if not isinstance(fetched, (int, float)) or now - fetched > ttl:
        return None
    return entry


def _cache_write(path: Path, entry: dict) -> str | None:
    """Write atomically; a cache that cannot be written is reported, not fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, **entry}))
        tmp.replace(path)
    except OSError as e:
        return f"cache write failed: {e}"
    return None


def scrape(url: str) -> str:
    """The page markdown via `firecrawl scrape`. Raises RuntimeError."""
    if shutil.which(FIRECRAWL_BIN) is None:
        raise RuntimeError(f"{FIRECRAWL_BIN} not on PATH")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "page.md"
        try:
            proc = subprocess.run([FIRECRAWL_BIN, "scrape", url, "-o", str(out)],
                                  capture_output=True, text=True,
                                  timeout=SCRAPE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"firecrawl timed out after {SCRAPE_TIMEOUT_SEC}s") from e
        except OSError as e:
            raise RuntimeError(f"firecrawl failed to start: {e}") from e
        text = out.read_text(errors="replace") if out.exists() else ""
        if not text.strip():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise RuntimeError(f"firecrawl exit {proc.returncode}, empty page: "
                               f"{tail[0][:160]}")
        return text


def _resolve_page(series: str, issue: str, year: int) -> dict:
    """Scrape candidates until one verifies. Returns a cache entry."""
    tried, errors = [], []
    for url in candidate_urls(series, issue, year):
        try:
            text = scrape(url)
        except RuntimeError as e:
            errors.append(f"{url}: {e}")
            continue
        why = verify_title(page_title(text), series, issue, year)
        if why is None:
            return {"status": OK, "url": url, "page": text}
        tried.append(f"{url}: {why}")
    if errors:
        # A failed scrape is not evidence the page is missing: no negative cache.
        return {"status": "scrape_failed", "detail": "; ".join(errors + tried)}
    return {"status": UNRESOLVED, "detail": "; ".join(tried)}


_FAILED_THIS_PROCESS: dict[str, dict] = {}


def fetch_book_sales(series: str | None, issue: str | None, year: object, *,
                     now: float | None = None) -> dict:
    """PC sales for one book: `{"status", "sales", "url", "cache", "warning"}`.

    `sales` is non-empty only for status "ok". Every status in
    FAILED_STATUSES carries a `warning` string the caller must print.
    """
    res: dict = {"status": DISABLED, "sales": [], "url": None, "cache": None,
                 "warning": None}
    if not enabled():
        return res
    yr: int | None = None
    if isinstance(year, (int, float, str)) and not isinstance(year, bool):
        try:
            yr = int(year)
        except ValueError:
            yr = None
    series = series_name(series) if series else ""
    if not series or issue in (None, "") or yr is None:
        res["status"] = NO_IDENTITY
        return res
    issue = str(issue)
    now = time.time() if now is None else now
    path = _cache_path(series, issue, yr)
    entry = _cache_read(path, now)
    res["cache"] = "hit" if entry else "miss"
    label = f"{series} #{issue} ({yr})"
    if entry is None and str(path) in _FAILED_THIS_PROCESS:
        # A scrape that failed earlier in this run is not retried for a
        # second grade of the same book: one failure, one spend.
        entry = _FAILED_THIS_PROCESS[str(path)]
    if entry is None:
        entry = _resolve_page(series, issue, yr)
        if entry["status"] not in (OK, UNRESOLVED):
            _FAILED_THIS_PROCESS[str(path)] = entry
        else:
            werr = _cache_write(path, {**entry, "fetched_at": now})
            if werr:
                res["warning"] = f"PriceCharting {label}: {werr}"
    res["status"] = entry["status"]
    res["url"] = entry.get("url")
    if entry["status"] != OK:
        if entry["status"] in FAILED_STATUSES:
            res["warning"] = (f"PriceCharting {label}: {entry['status']} "
                              f"({entry.get('detail', '')[:300]})")
        return res
    try:
        rows = parse_rows(entry.get("page") or "")
    except (re.error, TypeError, ValueError) as e:
        res.update(status="parse_failed",
                   warning=f"PriceCharting {label}: parse_failed ({e})")
        return res
    try:
        verdicts = classify(rows, issue)
    except RuntimeError as e:
        res.update(status="classify_failed",
                   warning=f"PriceCharting {label}: classify_failed ({e})")
        return res
    res["sales"] = build_sales(rows, verdicts, series=series, issue=issue)
    return res


# ─── append + guard ─────────────────────────────────────────────────────────

def _comp_date(c: dict) -> date | None:
    return (fmv_math._parse_sold_date(c.get("sold_date"))
            or fmv_math._parse_sold_date(c.get("first_seen_at")))


def same_sale(pc: dict, c: dict) -> bool:
    """True when pool comp `c` is the PC sale `pc`: the same eBay item id, or
    the same grade, a date within a day, and a price within 3% of PC's sold OR
    list price (a stored best-offer comp holds the list price, BUI-1222)."""
    if c.get("product_id") not in (None, "") and str(c["product_id"]) == str(pc["product_id"]):
        return True
    try:
        if c.get("grade") is None or float(c["grade"]) != float(pc["grade"]):
            return False
        cp = float(c["price"])
    except (TypeError, ValueError, KeyError):
        return False
    cd = _comp_date(c)
    if cd is None or abs((cd - date.fromisoformat(pc["sold_date"])).days) > 1:
        return False
    return any(p and abs(cp - p) <= 0.03 * max(cp, p)
               for p in (pc["price"], pc.get("ask")))


def append_rows(sales: list[dict], pool: list[dict], *, certifier: str,
                label: str) -> list[dict]:
    """PC CGC universal sales shaped as slab pool comps, minus any sale a
    pool comp already holds. Empty unless the target is CGC universal."""
    if certifier != "cgc" or label != "universal":
        return []
    out = []
    for s in sales:
        if not s.get("universal") or s.get("exclude_code") is not None:
            continue
        if any(same_sale(s, c) for c in pool):
            continue
        out.append({"provider": "pricecharting", "product_id": s["product_id"],
                    "title": s["title"], "price": s["price"],
                    "grade": s["grade"], "sold_date": s["sold_date"],
                    "page_quality": s["page_quality"], "certifier": "cgc",
                    "label": "universal",
                    # The accepted price already: never scale it again.
                    "best_offer": False})
    return out


def _priced(fmv: dict | None) -> bool:
    return (fmv is not None and fmv.get("fmv_low") is not None
            and fmv.get("fmv_high") is not None)


def keep_appended(base: dict, appended: dict) -> bool:
    """The BUI-1218/1226 refusal guard. Keep the appended result when it
    prices AND either the ledger-only result priced too or the appended pool
    prices `direct`. A ledger-only refusal that PC rungs alone would open as a
    ladder or lone sale stays refused, and an appended refusal never replaces
    a ledger-only price."""
    return _priced(appended) and (_priced(base)
                                  or appended.get("pricing_basis") == "direct")
