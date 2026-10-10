#!/usr/bin/env python3
"""BUI-1218 spike: does adding PriceCharting sales improve CGC slab FMV accuracy?
DIAGNOSTIC ONLY, read-only. No production code is touched.

Method. Each held-out test sale is a ledger (`comps`, pool='slab') CGC universal-label
sale from the 16 books below, sold 2026-09-10..2026-10-09. For each, price the book
twice with `fmv_math.graded_fmv` as of the day before the sale, scored against the
sale price (the ledger price; BUI-552 says it is the accepted amount, which BUI-1222 questions):

  baseline  ledger slab comps sold strictly before the sale date
  plus-PC   baseline + PriceCharting CGC universal sales, before the sale date,
            deduped against the ledger by eBay item id, else grade + date (+-1 day)
            + price (+-3%)

The test sale is hidden from BOTH sources (by item id, else grade + same date).
PriceCharting pages are scraped once with the firecrawl CLI
(`2026-10-10-pricecharting-slab-spike-fetch.sh`); the tier summary price is ignored,
grade/certifier/label/page quality are parsed from each sale title with the same
resolvers sold-comps uses.

    uv run --project plugins/gixen-overlay --with requests python \\
        docs/audit/2026-10-10-pricecharting-slab-spike.py PAGES_DIR [--json out.json]

Opens ~/.comics-server/db.sqlite with mode=ro.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "fmv" / "src"))
sys.path.insert(0, str(ROOT / "apps" / "ebay" / "src"))
import fmv_math  # noqa: E402
import sold_comps  # noqa: E402

DB = os.path.expanduser("~/.comics-server/db.sqlite")
TEST_FROM, TEST_TO = "2026-09-10", "2026-10-09"
# comic_id -> PriceCharting page file stem (pages saved by the fetch script)
BOOKS = {
    28: "comic-books-amazing-spider-man__amazing-spider-man-194-1979",
    1024: "comic-books-giant-size-x-men__giant-size-x-men-1-1975",
    685: "comic-books-amazing-spider-man__amazing-spider-man-50-1967",
    783: "comic-books-x-men__x-men-101-1976",
    727: "comic-books-fantastic-four__fantastic-four-49-1966",
    745: "comic-books-iron-man__iron-man-55-1973",
    661: "comic-books-x-men__the-x-men-96-1975",
    573: "comic-books-amazing-spider-man__amazing-spider-man-78-1969",
    1023: "comic-books-fantastic-four__fantastic-four-48-1966",
    1021: "comic-books-batman__batman-227-1970",
    717: "comic-books-batman__batman-655-2006",
    1026: "comic-books-ultimate-fallout-facsimile-edition__ultimate-fallout-4-2011",
    878: "comic-books-invincible__invincible-84-2011",
    798: "comic-books-x-force__x-force-11-1992",
    1019: "comic-books-avengers__avengers-87-1971",
    511: "comic-books-x-men__the-x-men-12-1965",
}

ROW_RE = re.compile(r"^\| (\d{4}-\d\d-\d\d) \|")
TITLE_RE = re.compile(r"\[(.+?)\]\((https?://[^)\s]+)\)")
PRICE_RE = re.compile(r"\$([\d,]+\.\d\d)")
ITM_RE = re.compile(r"/itm/(\d+)")


# ─── PriceCharting parsing ──────────────────────────────────────────────────

def parse_pc_page(path: Path) -> tuple[list[dict], dict]:
    """Every sale row on the page (all grade tabs live in one page). Returns CGC
    universal-label slab sales plus drop counts. `price` is the FIRST price in the
    cell (the sold amount); `ask` is the second (the struck-through list price)."""
    sales, seen_ids = [], set()
    stats = {"rows": 0, "not_cgc": 0, "no_grade": 0, "non_universal": 0,
             "excluded_title": 0, "dup_item": 0}
    for line in path.read_text().splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        stats["rows"] += 1
        cells = line.split(" | ")
        # strip the "Time Warp" lock cell so the title link is found reliably
        body = re.sub(r"!\[\]\([^)]*\)<br>Time Warp.*?OK\]\([^)]*\)", "", line)
        tm = TITLE_RE.search(body)
        if not tm:
            continue
        title, url = tm.group(1), tm.group(2)
        # price cell = the cell right after the title cell
        after = body[tm.end():]
        prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(after.split("[Report It]")[0])]
        if not prices:
            continue
        src = "heritage" if "ha.com" in url else ("ebay" if "ebay.com" in url else "other")
        if not re.search(r"\bcgc\b", title, re.I) or re.search(r"\bcbcs\b", title, re.I):
            stats["not_cgc"] += 1
            continue
        grade = sold_comps.parse_grade(title)
        if grade is None:
            stats["no_grade"] += 1
            continue
        fields = sold_comps.parse_slab_fields(title)
        if fields["certifier"] != "cgc" or fields["label"] != "universal":
            stats["non_universal"] += 1
            continue
        if sold_comps.hard_exclude(title) or sold_comps.graded_identity_exclude(title):
            stats["excluded_title"] += 1
            continue
        im = ITM_RE.search(url)
        pid = im.group(1) if im else f"pc:{m.group(1)}:{abs(hash(title)) % 10**8}"
        if pid in seen_ids:
            stats["dup_item"] += 1
            continue
        seen_ids.add(pid)
        sales.append({
            "product_id": pid, "title": title, "price": prices[0],
            "ask": prices[1] if len(prices) > 1 else None,
            "sold_date": m.group(1), "grade": grade,
            "page_quality": fields["page_quality"], "source": src,
            "provider": "pricecharting",
        })
    return sales, stats


# ─── ledger ─────────────────────────────────────────────────────────────────

def load_ledger(conn: sqlite3.Connection, comic_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT provider, product_id, title, price, sold_date, grade, page_quality,
                  first_seen_at
           FROM comps WHERE comic_id=? AND pool='slab' AND certifier='cgc'
             AND label='universal' AND excluded_code IS NULL AND price > 0
             AND grade IS NOT NULL""", (comic_id,)).fetchall()
    out, seen = [], set()
    for r in rows:
        d = dict(zip(("provider", "product_id", "title", "price", "sold_date", "grade",
                      "page_quality", "first_seen_at"), r))
        if d["product_id"] in seen:
            continue
        seen.add(d["product_id"])
        d["_date"] = fmv_math._parse_sold_date(d["sold_date"]) or fmv_math._parse_sold_date(d["first_seen_at"])
        d["_iso"] = d["_date"].isoformat() if d["_date"] else None
        d["sold_date"] = d["_iso"]
        out.append(d)
    return out


def is_dup(pc: dict, ledger: list[dict]) -> bool:
    """PC sale already present in the ledger: same eBay item id, else same grade,
    date within a day, and price within 3%."""
    pd = date.fromisoformat(pc["sold_date"])
    for c in ledger:
        if c["product_id"] == pc["product_id"]:
            return True
        if (c["grade"] == pc["grade"] and c["_date"] and abs((c["_date"] - pd).days) <= 1
                and abs(c["price"] - pc["price"]) <= 0.03 * max(c["price"], pc["price"])):
            return True
    return False


# ─── pricing and scoring ────────────────────────────────────────────────────

def price(comps: list[dict], grade: float, as_of: date, pq: str) -> dict:
    return fmv_math.graded_fmv(
        comps, grade, certifier="cgc", label="universal", as_of=as_of,
        page_quality=None if pq == "unknown" else pq)


def score(res: dict, y: float) -> dict | None:
    lo, hi = res.get("fmv_low"), res.get("fmv_high")
    if lo is None or hi is None:
        return None
    return {"low": lo, "high": hi, "mid": (lo + hi) / 2,
            "winkler": winkler_score(lo, hi, y, DEFAULT_ALPHA) / y,
            "hit": lo <= y <= hi, "bias": ((lo + hi) / 2 - y) / y,
            "basis": res.get("pricing_basis")}


def summarize(label: str, rows: list[dict], key: str) -> dict:
    sc = [r[key] for r in rows if r[key]]
    return {"label": label, "n_tests": len(rows), "priced": len(sc),
            "coverage": len(sc) / len(rows) if rows else 0,
            "winkler": mean(s["winkler"] for s in sc) if sc else None,
            "in_band": mean(s["hit"] for s in sc) if sc else None,
            "bias": mean(s["bias"] for s in sc) if sc else None,
            "median_bias": median(s["bias"] for s in sc) if sc else None}


def main() -> int:
    pages = Path(sys.argv[1])
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tests: list[dict] = []
    pc_stats = {}
    for cid, stem in BOOKS.items():
        title = conn.execute("SELECT title||' #'||issue FROM comics WHERE id=?", (cid,)).fetchone()[0]
        ledger = load_ledger(conn, cid)
        pc_all, st = parse_pc_page(pages / f"{stem}.md")
        pc_stats[title] = {**st, "kept": len(pc_all),
                           "new_vs_ledger": sum(not is_dup(p, ledger) for p in pc_all)}
        held = [c for c in ledger if c["provider"] == "sold-comps.com" and c["_iso"]
                and TEST_FROM <= c["_iso"] <= TEST_TO]
        for t in sorted(held, key=lambda c: c["_iso"]):
            d, y, g = t["_date"], t["price"], t["grade"]
            as_of = d - timedelta(days=1)
            hide = lambda c: (c["product_id"] == t["product_id"]  # noqa: E731
                              or (c["grade"] == g and c["_iso"] == t["_iso"] and c["price"] == y))
            base = [c for c in ledger if c["_date"] and c["_date"] < d and not hide(c)]
            pcs = []
            for p in pc_all:
                pdt = date.fromisoformat(p["sold_date"])
                if pdt >= d or p["product_id"] == t["product_id"]:
                    continue
                if p["grade"] == g and p["sold_date"] == t["_iso"]:
                    continue
                if is_dup(p, base):
                    continue
                pcs.append({**p, "_date": pdt})
            pcs_ebay = [p for p in pcs if p["source"] == "ebay"]
            r0 = price(base, g, as_of, t["page_quality"])
            r1 = price(base + pcs, g, as_of, t["page_quality"])
            r2 = price(base + pcs_ebay, g, as_of, t["page_quality"])
            # same PC sales, but at the best-offer LIST price (the ledger's basis)
            pcs_list = [{**p, "price": p["ask"] or p["price"]} for p in pcs]
            r3 = price(base + pcs_list, g, as_of, t["page_quality"])
            tests.append({
                "book": title, "comic_id": cid, "grade": g, "date": t["_iso"], "y": y,
                "item": t["product_id"], "pq": t["page_quality"],
                "n_base": len(base), "n_pc_added": len(pcs), "n_pc_ebay": len(pcs_ebay),
                "base": score(r0, y), "base_reason": r0.get("flag_reason"),
                "pc": score(r1, y), "pc_reason": r1.get("flag_reason"),
                "pce": score(r2, y), "pce_reason": r2.get("flag_reason"),
                "pcl": score(r3, y), "pcl_reason": r3.get("flag_reason"),
            })

    print("PC parse stats:", json.dumps(pc_stats, indent=1))
    print(f"\n{len(tests)} test sales over {len(BOOKS)} books\n")
    for key, lab in (("base", "ledger only"), ("pc", "ledger + PC (all)"), ("pce", "ledger + PC (eBay only)"),
                     ("pcl", "ledger + PC at list price (ledger basis)")):
        print(json.dumps(summarize(lab, tests, key)))
    # paired: both priced
    for key in ("pc", "pce", "pcl"):
        both = [t for t in tests if t["base"] and t[key]]
        newly = [t for t in tests if not t["base"] and t[key]]
        lost = [t for t in tests if t["base"] and not t[key]]
        print(f"\n[{key}] paired n={len(both)}: base winkler {mean(t['base']['winkler'] for t in both):.3f}"
              f" vs {mean(t[key]['winkler'] for t in both):.3f}; base bias "
              f"{mean(t['base']['bias'] for t in both):+.3f} vs {mean(t[key]['bias'] for t in both):+.3f}; "
              f"in-band {mean(t['base']['hit'] for t in both):.2f} vs {mean(t[key]['hit'] for t in both):.2f}")
        if newly:
            print(f"[{key}] newly priced n={len(newly)}: winkler {mean(t[key]['winkler'] for t in newly):.3f}, "
                  f"in-band {mean(t[key]['hit'] for t in newly):.2f}, bias {mean(t[key]['bias'] for t in newly):+.3f}")
        for t in newly:
            print(f"   newly priced: {t['book']} {t['grade']} y={t['y']:.0f} band {t[key]['low']:.0f}-{t[key]['high']:.0f}"
                  f" ({t[key]['basis']}) bias {t[key]['bias']:+.2f}")
        wins = sum(t[key]["winkler"] < t["base"]["winkler"] - 1e-9 for t in both)
        losses = sum(t[key]["winkler"] > t["base"]["winkler"] + 1e-9 for t in both)
        print(f"[{key}] paired Winkler: PC better {wins}, worse {losses}, tied {len(both) - wins - losses};"
              f" median bias {median(t['base']['bias'] for t in both):+.3f} -> {median(t[key]['bias'] for t in both):+.3f}")
        print(f"[{key}] priced->refused n={len(lost)}")
        changed = [t for t in both if (t['base']['low'], t['base']['high']) != (t[key]['low'], t[key]['high'])]
        print(f"[{key}] bands that changed among paired: {len(changed)}/{len(both)}")

    print("\nPer-sale rows (book, grade, date, y, n_base, +pc, base band, +pc band, reasons)")
    for t in tests:
        f = lambda s: f"{s['low']:.0f}-{s['high']:.0f}" if s else "refused"  # noqa: E731
        print(f"{t['book']:<28} {t['grade']:>4} {t['date']} y={t['y']:>7.0f} nb={t['n_base']:>2} +pc={t['n_pc_added']:>2}"
              f" | {f(t['base']):>11} {t['base_reason'] or ''} | {f(t['pc']):>11} {t['pc_reason'] or ''}")

    # price conflicts: same eBay item id in both sources with different prices
    print("\nPrice conflicts (same eBay item id, PC sold price vs ledger price)")
    conf = []
    for cid, stem in BOOKS.items():
        led = {c["product_id"]: c for c in load_ledger(conn, cid)}
        pc_all, _ = parse_pc_page(pages / f"{stem}.md")
        for p in pc_all:
            c = led.get(p["product_id"])
            if c:
                conf.append({"item": p["product_id"], "date": p["sold_date"], "grade": p["grade"],
                             "pc": p["price"], "pc_ask": p["ask"], "ledger": c["price"], "src": p["source"],
                             "title": p["title"][:60]})
    same = sum(1 for c in conf if abs(c["pc"] - c["ledger"]) < 0.5)
    print(f"overlap {len(conf)}, equal {same}, differ {len(conf) - same}")
    ratios = [c["pc"] / c["ledger"] for c in conf if c["src"] == "ebay" and abs(c["pc"] - c["ledger"]) >= 0.5]
    if ratios:
        print(f"eBay-source differing: n={len(ratios)}, PC/ledger median {median(ratios):.3f}, "
              f"PC lower in {sum(r < 1 for r in ratios)}")
    asks_match = [c for c in conf if c["pc_ask"] is not None]
    print(f"overlap rows where PC shows two prices: {len(asks_match)}; ledger == PC ask in "
          f"{sum(abs(c['ledger'] - c['pc_ask']) < 0.5 for c in asks_match)}, == PC sold in "
          f"{sum(abs(c['ledger'] - c['pc']) < 0.5 for c in asks_match)}")
    for c in conf:
        if abs(c["pc"] - c["ledger"]) >= 0.5:
            print(json.dumps(c))
    bo = [c for c in conf if c["pc_ask"] is not None and abs(c["pc"] - c["ledger"]) >= 0.5]
    if bo:
        rr = [c["pc"] / c["ledger"] for c in bo]
        print(f"best-offer rows (n={len(bo)}): accepted/list ratio median {median(rr):.3f}, "
              f"min {min(rr):.3f}, max {max(rr):.3f}; round-dollar share: PC accepted "
              f"{mean(c['pc'] % 1 == 0 for c in bo):.2f}, ledger list {mean(c['ledger'] % 1 == 0 for c in bo):.2f}; "
              f"multiple-of-$5: PC {mean(c['pc'] % 5 == 0 for c in bo):.2f}, ledger {mean(c['ledger'] % 5 == 0 for c in bo):.2f}")

    # per-book table and a seeded bootstrap of the paired Winkler difference
    import random
    print("\nPer-book (tests, base/PC Winkler, base/PC in-band, base/PC bias, PC sales kept, new vs ledger)")
    for cid in BOOKS:
        ts = [t for t in tests if t["comic_id"] == cid]
        if not ts:
            continue
        bt = ts[0]["book"]
        sb, sp = summarize("b", ts, "base"), summarize("p", ts, "pc")
        f = lambda v, p=2: "n/a" if v is None else f"{v:.{p}f}"  # noqa: E731
        print(f"{bt:<26} n={len(ts):>2} wink {f(sb['winkler'])}/{f(sp['winkler'])} inband {f(sb['in_band'])}/{f(sp['in_band'])}"
              f" bias {f(sb['bias'])}/{f(sp['bias'])} priced {sb['priced']}/{sp['priced']}"
              f" pc_kept={pc_stats[bt]['kept']} new={pc_stats[bt]['new_vs_ledger']}")
    rng = random.Random(1218)
    pairs = [(t["base"]["winkler"], t["pc"]["winkler"]) for t in tests if t["base"] and t["pc"]]
    diffs = []
    for _ in range(4000):
        samp = [rng.choice(pairs) for _ in pairs]
        diffs.append(mean(b - p for b, p in samp))
    diffs.sort()
    print(f"\nPaired Winkler improvement (base - PC), n={len(pairs)}: mean {mean(b - p for b, p in pairs):.3f}, "
          f"bootstrap 95% CI [{diffs[100]:.3f}, {diffs[3899]:.3f}]")
    if out_json:
        Path(out_json).write_text(json.dumps({"tests": tests, "conflicts": conf, "pc_stats": pc_stats}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
