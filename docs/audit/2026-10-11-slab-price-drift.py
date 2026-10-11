#!/usr/bin/env python3
"""BUI-1231: slab price drift and recency weighting, on PriceCharting history.
DIAGNOSTIC ONLY, read-only. No production code is touched.

Needs the BUI-1229 test set built. Then:

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-11-slab-price-drift.py \\
        [--json out.json]

1. DRIFT. PC CGC universal sales (price outliers dropped), cell = book x grade x
   page quality. Within each cell, log(price) is regressed on sale time with the
   cell mean removed (fixed effects), so a mix shift across books, grades, or
   page quality cannot read as drift. drift/yr = exp(slope) - 1, pooled over
   cells (each cell weighted by its time variance, the OLS default). Cut by book
   era and by price tier (the cell's median price). 95% CI: percentile bootstrap
   resampling BOOKS (2000 reps, seed 1231). Three windows: 2022-01-01+, 2024-01-01+,
   and the trailing 365 days.
2. WEIGHTING. Each PC universal sale dated in [SCORE_FROM, SCORE_TO] (outliers
   dropped from truth only) is priced with `fmv_math.graded_fmv`, as of the day
   before it, from earlier PC universal sales of the same book only (strictly
   before the sale's date, so the sale and its same-day twins are never comps;
   comps are not outlier-filtered). Truth is PC's SOLD price (accepted offer on a
   best-offer sale). `fmv_math.graded_comp_weight` is patched per scheme to change
   the weights; the rest of graded_fmv (tiers, ladder, clamps) runs as shipped.
   Time-adjusted schemes multiply a comp's price by (1+r)^(age/365) before pricing.
   r comes from a fixed grid, never fitted on the scored sales, so there is no
   leakage. Schemes: prod (shipped: 1.0 to 90d, 0.5 to 365d), w90 (the
   sold-comps.com window: 90d only), flat365, exponential half-lives, and
   r-adjusted variants.
   Score: Winkler (alpha 0.5) / sale price. A difference between schemes is the
   PAIRED difference on sales both priced; the CI resamples books, each replicate
   the sale-weighted mean difference.
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pc_slab_test_set as T  # noqa: E402
from pc_slab_test_set import fmv_math  # noqa: E402

REPS, SEED = 2000, 1231
SCORE_FROM, SCORE_TO = "2025-01-01", "2026-10-10"
TODAY = date(2026, 10, 10)
TIERS = [("<$150", 0, 150), ("$150-500", 150, 500), ("$500-2k", 500, 2000), (">=$2k", 2000, 1e12)]
ERAS = ("silver", "bronze", "copper", "modern")


def tier_of(p: float) -> str:
    return next(n for n, lo, hi in TIERS if lo <= p < hi)


# ------------------------------------------------------------------ drift

def cell_stats(rows: list[dict]) -> dict:
    """(book, grade, page quality) -> (sxx, sxy, n, median price) for demeaned log price on years."""
    cells = defaultdict(list)
    for r in rows:
        cells[(r["comic_id"], r["grade"], r["page_quality"])].append(r)
    out = {}
    for k, rs in cells.items():
        if len(rs) < 2:
            continue
        t = [(date.fromisoformat(r["sold_date"]) - TODAY).days / 365.25 for r in rs]
        y = [math.log(r["price"]) for r in rs]
        tb, yb = mean(t), mean(y)
        sxx = sum((a - tb) ** 2 for a in t)
        if sxx <= 0:
            continue
        out[k] = (sxx, sum((a - tb) * (b - yb) for a, b in zip(t, y)), len(rs),
                  median(r["price"] for r in rs))
    return out


def drift_ci(rows: list[dict]) -> dict:
    cs = cell_stats(rows)
    if not cs:
        return {"cells": 0}
    by_book = defaultdict(lambda: [0.0, 0.0])
    for k, v in cs.items():
        by_book[k[0]][0] += v[0]
        by_book[k[0]][1] += v[1]
    books = list(by_book)
    est = math.expm1(sum(v[1] for v in by_book.values()) / sum(v[0] for v in by_book.values()))
    rng = random.Random(SEED)
    reps = []
    for _ in range(REPS):
        pick = [by_book[rng.choice(books)] for _ in books]
        d = sum(p[0] for p in pick)
        if d > 0:
            reps.append(math.expm1(sum(p[1] for p in pick) / d))
    reps.sort()
    return {"cells": len(cs), "books": len(books), "sales": sum(v[2] for v in cs.values()),
            "drift": est, "ci": (reps[int(0.025 * len(reps))], reps[int(0.975 * len(reps)) - 1])}


def fmt_drift(d: dict) -> str:
    if not d.get("cells"):
        return "no cells"
    return (f"{d['drift']:+.1%}/yr [{d['ci'][0]:+.1%}, {d['ci'][1]:+.1%}] "
            f"(books={d['books']} cells={d['cells']} sales={d['sales']})")


# --------------------------------------------------------------- weighting

FRESH, STALE_MAX = fmv_math.GRADED_FRESH_MAX_AGE_DAYS, fmv_math.GRADED_STALE_MAX_AGE_DAYS
STALE_W = fmv_math.GRADED_STALE_WEIGHT


def prod_w(a):  # the shipped rule, restated; main() asserts it still equals fmv_math's
    return 1.0 if a <= FRESH else STALE_W if a <= STALE_MAX else None


def flat_w(cap):
    return lambda a: 1.0 if a <= cap else None


def exp_w(hl, cap=365):
    return lambda a: (0.5 ** (a / hl)) if a <= cap else None


SCHEMES = {  # name -> (weight fn, r per year, history cap in days)
    "w90 (sold-comps window)": (flat_w(90), 0.0, 365),
    "prod (1/0.5, 365d cap)": (prod_w, 0.0, 365),
    "flat365": (flat_w(365), 0.0, 365),
    "exp hl=45": (exp_w(45), 0.0, 365),
    "exp hl=90": (exp_w(90), 0.0, 365),
    "exp hl=180": (exp_w(180), 0.0, 365),
    "prod + adj r=5%": (prod_w, 0.05, 365),
    "prod + adj r=10%": (prod_w, 0.10, 365),
    "prod + adj r=20%": (prod_w, 0.20, 365),
    "flat365 + adj r=10%": (flat_w(365), 0.10, 365),
    "flat365 + adj r=20%": (flat_w(365), 0.20, 365),
    "flat730 + adj r=10%": (flat_w(730), 0.10, 730),
    "flat730 + adj r=20%": (flat_w(730), 0.20, 730),
}
REF = "prod (1/0.5, 365d cap)"


def price_with(scheme, hist, grade, d, pq):
    wfn, r, cap = SCHEMES[scheme]
    as_of = d - timedelta(days=1)
    comps = []
    for c in hist:
        age = (as_of - c["_date"]).days
        if age > cap:
            continue
        p = c["price"] * (1 + r) ** (max(age, 0) / 365) if r else c["price"]
        comps.append({**c, "price": p})
    orig = fmv_math.graded_comp_weight
    fmv_math.graded_comp_weight = lambda a: None if a is None else wfn(a)
    try:
        return fmv_math.graded_fmv(comps, grade, certifier="cgc", label="universal", as_of=as_of,
                                   page_quality=None if pq == "unknown" else pq)
    finally:
        fmv_math.graded_comp_weight = orig


def score(res, y):
    lo, hi = res.get("fmv_low"), res.get("fmv_high")
    if lo is None or hi is None or not y:
        return None
    return {"w": winkler_score(lo, hi, y, DEFAULT_ALPHA) / y, "hit": lo <= y <= hi,
            "bias": ((lo + hi) / 2 - y) / y}


def boot_mean(vals_by_book: dict) -> tuple[float, tuple[float, float]]:
    """Sale-weighted mean over books, with a book-resampling percentile CI."""
    books = list(vals_by_book)
    est = mean(v for b in books for v in vals_by_book[b])
    rng = random.Random(SEED)
    reps = []
    for _ in range(REPS):
        reps.append(mean(v for b in (rng.choice(books) for _ in books) for v in vals_by_book[b]))
    reps.sort()
    return est, (reps[int(0.025 * REPS)], reps[int(0.975 * REPS) - 1])


def group(rows, f):
    g = defaultdict(list)
    for r in rows:
        g[r["comic_id"]].append(f(r))
    return g


def table(sub, label):
    print(f"  [{label}] n={len(sub)}")
    print(f"    {'scheme':<24}{'priced':>7}  {'Winkler [95% CI]':<22}{'in-band':>8}{'bias':>8}"
          f"   paired dWinkler vs prod [CI] (n)")
    out = {}
    for name in SCHEMES:
        pr = [r for r in sub if r[name]]
        if not pr:
            continue
        w, wci = boot_mean(group(pr, lambda r: r[name]["w"]))
        both = [r for r in sub if r[name] and r[REF]]
        d = dci = None
        dtxt = ""
        if name != REF and both:
            d, dci = boot_mean(group(both, lambda r: r[name]["w"] - r[REF]["w"]))
            dtxt = f"{d:+.3f} [{dci[0]:+.3f}, {dci[1]:+.3f}] ({len(both)})"
        print(f"    {name:<24}{len(pr) / len(sub):>7.0%}  {w:.3f} [{wci[0]:.3f}, {wci[1]:.3f}]"
              f"{mean(r[name]['hit'] for r in pr):>10.0%}{mean(r[name]['bias'] for r in pr):>+9.3f}   {dtxt}")
        out[name] = {"priced": len(pr), "winkler": w, "ci": wci, "dvs_prod": d, "dci": dci}
    return out


def main() -> int:
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    ts = T.load_test_set()
    books = {b["comic_id"]: b for b in ts["books"]}
    clean = [{**s, "_era": books[s["comic_id"]]["era"]} for s in T.universal_sales(ts)
             if s["price_outlier"] is not True]
    m = ts["meta"]
    print(f"Test set built {m['built']}: {len(clean)} CGC universal sales after dropping outliers. "
          f"Bootstrap {REPS} reps over books, seed {SEED}. Reference date {TODAY}.\n")
    assert all(prod_w(a) == fmv_math.graded_comp_weight(a) for a in (0, 60, 90, 91, 200, 365, 366))

    print("1. DRIFT (within book x grade x page quality, log price on time)")
    drift_out = {}
    for wn, wf in [("2022+", "2022-01-01"), ("2024+", "2024-01-01"), ("trailing 365d", "2025-10-10")]:
        rows = [r for r in clean if r["sold_date"] >= wf]
        print(f"\n  Window {wn} ({len(rows)} sales)")
        res = {"all": drift_ci(rows), "ebay": drift_ci([r for r in rows if r["source"] == "ebay"])}
        print("    all                :", fmt_drift(res["all"]))
        print("    eBay only          :", fmt_drift(res["ebay"]))
        for e in ERAS:
            res["era " + e] = drift_ci([r for r in rows if r["_era"] == e])
            print(f"    era {e:<7}        :", fmt_drift(res["era " + e]))
        cmed = {k: v[3] for k, v in cell_stats(rows).items()}
        tagged = [{**r, "_tier": tier_of(cmed[(r["comic_id"], r["grade"], r["page_quality"])])}
                  for r in rows if (r["comic_id"], r["grade"], r["page_quality"]) in cmed]
        for tn, _, _ in TIERS:
            res["tier " + tn] = drift_ci([r for r in tagged if r["_tier"] == tn])
            print(f"    tier {tn:<10}    :", fmt_drift(res["tier " + tn]))
        for e in ERAS:
            for tn, _, _ in TIERS:
                d = drift_ci([r for r in tagged if r["_era"] == e and r["_tier"] == tn])
                if d.get("books", 0) >= 3:
                    res[f"{e}/{tn}"] = d
                    print(f"    {e:<7} x {tn:<9}:", fmt_drift(d))
        drift_out[wn] = res

    print(f"\n2. WEIGHTING, truth = PC sold price, sales {SCORE_FROM}..{SCORE_TO}, "
          "comps = earlier PC universal sales of the same book")
    rows = []
    for cid, b in books.items():
        hist = [{**s, "_date": date.fromisoformat(s["sold_date"])} for s in T.universal_sales(ts, cid)]
        for s in hist:
            if not (SCORE_FROM <= s["sold_date"] <= SCORE_TO) or s["price_outlier"] is True:
                continue
            pool = [c for c in hist if c["_date"] < s["_date"]]
            row = {"comic_id": cid, "era": b["era"], "date": s["sold_date"], "grade": s["grade"],
                   "y": s["price"], "source": s["source"]}
            for name in SCHEMES:
                row[name] = score(price_with(name, pool, s["grade"], s["_date"], s["page_quality"]),
                                  s["price"])
            rows.append(row)
    print(f"  {len(rows)} scored sales, {len({r['comic_id'] for r in rows})} books\n")

    wout = {"all": table(rows, "all")}
    wout["ebay"] = table([r for r in rows if r["source"] == "ebay"], "eBay source only")
    for e in ERAS:
        sub = [r for r in rows if r["era"] == e]
        if sub:
            wout[e] = table(sub, f"era {e}, {len({r['comic_id'] for r in sub})} books (thin)")
    for yr in ("2025", "2026"):
        wout["sold " + yr] = table([r for r in rows if r["date"].startswith(yr)], f"sold in {yr}")
    cands = {n: v for n, v in wout["all"].items() if n != REF and v["dvs_prod"] is not None}
    best = min(cands, key=lambda n: cands[n]["dvs_prod"])
    print(f"\n  Best scheme by paired dWinkler vs prod (lowest of {len(cands)} tried): {best} "
          f"{cands[best]['dvs_prod']:+.3f} [{cands[best]['dci'][0]:+.3f}, {cands[best]['dci'][1]:+.3f}]")
    if out_json:
        Path(out_json).write_text(json.dumps({"drift": drift_out, "weighting": wout}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
