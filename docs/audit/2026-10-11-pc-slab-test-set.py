#!/usr/bin/env python3
"""BUI-1229: baseline and overlap check on the PriceCharting slab test set.
DIAGNOSTIC ONLY, read-only. No production code is touched.

Needs the built test set (`pc_slab_test_set.py fetch` then `build`). Then:

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-11-pc-slab-test-set.py \\
        [--json out.json]

1. Baseline. Every PC CGC universal sale dated in [WIN_FROM, WIN_TO] is priced
   with `fmv_math.graded_fmv` as of the day before it, from LEDGER comps only
   (CGC universal slab, sold strictly before the sale date). The sale's own
   ledger twin, if any, is hidden (`same_sale`: item id, else grade + date
   within a day + price within 3% of PC's sold or list price). Truth is PC's
   SOLD price, which on a best-offer sale is the ACCEPTED offer. The ledger
   comps feeding the band hold LIST price on best-offer sales (BUI-1222), so
   this is the real-transaction basis and carries that basis gap; a list-basis
   score (truth = PC `ask` when present) is printed beside it.
2. Overlap check, on the 16 BUI-1218 books over the same window: the same
   ledger-only pricing scored three ways: PC sales as truth (sold basis), PC
   sales as truth (list basis), and ledger sales as truth (the spike's method:
   sold-comps.com sales, the sale hidden by item id or grade + date + price).
3. Secondary (not the spec baseline, for the follow-ups): PC-history pricing,
   every PC universal sale priced from earlier PC universal sales only.

Score: Winkler (alpha 0.5) / sale price, in-band rate, signed bias
((mid - y) / y). CIs: 95% percentile bootstrap, resampling BOOKS (2000 reps,
seed 1229), each replicate the sale-weighted mean over the resampled books.
Opens ~/.comics-server/db.sqlite with mode=ro.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pc_slab_test_set as T  # noqa: E402
from pc_slab_test_set import fmv_math  # noqa: E402

WIN_FROM, WIN_TO = "2026-08-01", "2026-10-10"
REPS, SEED = 2000, 1229


def price(comps: list[dict], grade: float, as_of: date, pq: str) -> dict:
    return fmv_math.graded_fmv(comps, grade, certifier="cgc", label="universal", as_of=as_of,
                               page_quality=None if pq == "unknown" else pq)


def score(res: dict, y: float) -> dict | None:
    lo, hi = res.get("fmv_low"), res.get("fmv_high")
    if lo is None or hi is None or not y:
        return None
    return {"low": lo, "high": hi, "winkler": winkler_score(lo, hi, y, DEFAULT_ALPHA) / y,
            "hit": lo <= y <= hi, "bias": ((lo + hi) / 2 - y) / y}


def summarize(rows: list[dict], key: str) -> dict:
    """Point estimates plus book-clustered bootstrap CIs over priced rows."""
    sc = [r for r in rows if r.get(key)]
    out = {"n": len(rows), "priced": len(sc), "books": len({r["comic_id"] for r in sc})}
    if not sc:
        return out
    by_book = defaultdict(list)
    for r in sc:
        by_book[r["comic_id"]].append(r[key])
    books = list(by_book)
    rng = random.Random(SEED)
    out["winkler_median"] = median(r[key]["winkler"] for r in sc)
    for m in ("winkler", "hit", "bias"):
        out[m] = mean(float(s[m]) for s in (r[key] for r in sc))
        reps = []
        for _ in range(REPS):
            pick = [by_book[rng.choice(books)] for _ in books]
            vals = [float(s[m]) for grp in pick for s in grp]
            reps.append(mean(vals))
        reps.sort()
        out[m + "_ci"] = (reps[int(0.025 * REPS)], reps[int(0.975 * REPS) - 1])
    return out


def fmt(s: dict) -> str:
    if not s.get("priced"):
        return f"n={s['n']} priced=0"
    ci = lambda k: f"[{s[k + '_ci'][0]:.3f}, {s[k + '_ci'][1]:.3f}]"  # noqa: E731
    return (f"n={s['n']} priced={s['priced']} ({s['priced'] / s['n']:.0%}) books={s['books']} | "
            f"Winkler {s['winkler']:.3f} {ci('winkler')} | in-band {s['hit']:.0%} {ci('hit')} | "
            f"bias {s['bias']:+.3f} {ci('bias')} | median Winkler {s['winkler_median']:.3f}")


def in_window(d: str | None) -> bool:
    return bool(d) and WIN_FROM <= d <= WIN_TO


def main() -> int:
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    ts = T.load_test_set()
    books = {b["comic_id"]: b for b in ts["books"]}
    conn = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True)
    pc_rows, ledger_rows, hist_rows = [], [], []

    for cid, b in books.items():
        ledger = T.load_ledger(conn, cid)
        pcs = T.universal_sales(ts, cid)

        # 1. baseline: PC sales as truth, ledger-only comps
        for s in pcs:
            if not in_window(s["sold_date"]):
                continue
            d = date.fromisoformat(s["sold_date"])
            # hide every ledger row that could be this same sale, not only the stored twin
            comps = [c for c in ledger if c["_date"] and c["_date"] < d and not T.same_sale(s, c)]
            res = price(comps, s["grade"], d - timedelta(days=1), s["page_quality"])
            pc_rows.append({
                "comic_id": cid, "era": b["era"], "known": b["known"], "date": s["sold_date"],
                "grade": s["grade"], "y": s["price"], "ask": s["ask"], "in_ledger": s["in_ledger"],
                "source": s["source"], "outlier": s["price_outlier"] is True,
                "n_comps": len(comps), "reason": res.get("flag_reason"),
                "sold": score(res, s["price"]), "list": score(res, s["ask"] or s["price"]),
            })

        # 2. ledger sales as truth (the spike's held-out method), same window
        for t in ledger:
            if t["provider"] != "sold-comps.com" or not in_window(t["sold_date"]):
                continue
            d, y, g = t["_date"], t["price"], t["grade"]
            comps = [c for c in ledger if c["_date"] and c["_date"] < d
                     and c["product_id"] != t["product_id"]
                     and not (c["grade"] == g and c["sold_date"] == t["sold_date"] and c["price"] == y)]
            res = price(comps, g, d - timedelta(days=1), t["page_quality"])
            ledger_rows.append({"comic_id": cid, "era": b["era"], "known": b["known"],
                                "date": t["sold_date"], "grade": g, "y": y,
                                "reason": res.get("flag_reason"), "sold": score(res, y)})

        # 3. secondary: PC-history pricing (prior PC universal sales only)
        dated = [{**s, "_date": date.fromisoformat(s["sold_date"])} for s in pcs]
        for s in dated:
            if s["price_outlier"] is True:  # truth hygiene only; comps stay unfiltered
                continue
            comps = [c for c in dated if c["_date"] < s["_date"]]
            if not comps:
                continue
            res = price(comps, s["grade"], s["_date"] - timedelta(days=1), s["page_quality"])
            hist_rows.append({"comic_id": cid, "era": b["era"], "year": s["sold_date"][:4],
                              "y": s["price"], "sold": score(res, s["price"])})

    m = ts["meta"]
    print(f"Test set: {m['n_books']} books, {m['n_sales']} graded sales, {m['n_universal']} CGC universal"
          f" (built {m['built']}); skipped {len(m['skipped'])}: "
          + "; ".join(f"{k['comic_id']} {k['slug']} ({k['reason']})" for k in m["skipped"]))
    print(f"\nWindow {WIN_FROM}..{WIN_TO}. Bootstrap: {REPS} reps over books, seed {SEED}.\n")

    clean = [r for r in pc_rows if not r["outlier"]]
    print("1. BASELINE: ledger-only graded_fmv vs PC CGC universal sales (price outliers dropped)")
    print("  truth = PC sold/accepted :", fmt(summarize(clean, "sold")))
    print("  truth = PC list (ask)    :", fmt(summarize(clean, "list")))
    print("  incl. price outliers     :", fmt(summarize(pc_rows, "sold")))
    for e in ("silver", "bronze", "copper", "modern"):
        rows = [r for r in clean if r["era"] == e]
        if rows:
            print(f"  era {e:<7} (sold basis) :", fmt(summarize(rows, "sold")))
    for lab, sel in (("PC-only (not in ledger)", lambda r: not r["in_ledger"]),
                     ("also in ledger", lambda r: r["in_ledger"]),
                     ("eBay-source", lambda r: r["source"] == "ebay"),
                     ("Heritage-source", lambda r: r["source"] == "heritage")):
        print(f"  {lab:<25}:", fmt(summarize([r for r in clean if sel(r)], "sold")))
    reasons = defaultdict(int)
    for r in clean:
        if not r["sold"]:
            reasons[r["reason"] or "none"] += 1
    print("  refusals:", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))

    print("\n2. OVERLAP CHECK on the 16 BUI-1218 books (same ledger-only pricing, same window)")
    kp = [r for r in clean if r["known"]]
    kpe = [r for r in kp if r["source"] == "ebay"]
    kl = [r for r in ledger_rows if r["known"]]
    print("  PC truth, sold basis     :", fmt(summarize(kp, "sold")))
    print("  PC truth, list basis     :", fmt(summarize(kp, "list")))
    print("  PC truth, eBay, list     :", fmt(summarize(kpe, "list")))
    print("  PC truth, PC-only sales  :", fmt(summarize([r for r in kp if not r["in_ledger"]], "sold")))
    print("  PC truth, in-ledger only :", fmt(summarize([r for r in kp if r["in_ledger"]], "list")))
    print("  ledger truth (list basis):", fmt(summarize(kl, "sold")))
    print("  ledger truth, all books  :", fmt(summarize(ledger_rows, "sold")))

    print("\n3. SECONDARY: PC-history pricing (prior PC sales only), all dates")
    print("  all                      :", fmt(summarize(hist_rows, "sold")))
    for e in ("silver", "bronze", "copper", "modern"):
        rows = [r for r in hist_rows if r["era"] == e]
        if rows:
            print(f"  era {e:<7}              :", fmt(summarize(rows, "sold")))
    for yr in sorted({r["year"] for r in hist_rows}):
        rows = [r for r in hist_rows if r["year"] == yr]
        s = summarize(rows, "sold")
        if s.get("priced"):
            print(f"  {yr}: n={s['n']} priced={s['priced']} Winkler {s['winkler']:.3f} in-band {s['hit']:.0%}")

    if out_json:
        Path(out_json).write_text(json.dumps(
            {"baseline": pc_rows, "ledger_truth": ledger_rows, "pc_history": hist_rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
