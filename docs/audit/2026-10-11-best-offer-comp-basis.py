#!/usr/bin/env python3
"""BUI-1234: backtest three bases for best-offer comps on the BUI-1229 slab test set.
DIAGNOSTIC ONLY, read-only. No production code is touched.

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-11-best-offer-comp-basis.py

BUI-1222 proved sold-comps.com `soldPrice` is the LIST price on best-offer sales.
This scores four comp bases with the BUI-1229 harness (same rows, same pricing,
same `same_sale` hiding, same book-clustered bootstrap):

  baseline  ledger comps as stored (best-offer rows at list)
  (a) drop  best-offer-flagged comps removed from the pool
  (b) ratio best-offer-flagged comps scaled by R = median(accepted / list), fitted
            on PC best-offer sales dated BEFORE the scoring window (no leakage)
  (c) pc    a best-offer comp whose eBay item id joins a PC best-offer sale takes
            PC's accepted price; every other comp stays as stored

The best-offer flag is not in the ledger (the parser drops it, BUI-1234), so it
is joined back by item id from the local sold-comps.com raw capture (BUI-614,
`bestOfferAccepted`). Where the capture lacks the item, PC's own `best_offer`
flag is used when the item joins PC; else the comp counts as unflagged, which is
what a production fix would see for pre-capture rows. Each option is also run
with the flag from the capture ONLY (`-cap` rows), the flag a production fix
could actually persist.

Ledger comps come from `comics-api GET /api/comics/comps/all?pool=slab` (never
the DB file), filtered as `pc_slab_test_set.load_ledger` does: CGC universal,
not excluded, price > 0, grade present, deduped by product_id.

Truth is PC's SOLD price (the accepted offer: the real transaction), price
outliers dropped. Paired deltas are scored on sales both bases price.
"""

from __future__ import annotations

import glob
import gzip
import json
import random
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pc_slab_test_set as T  # noqa: E402
from pc_slab_test_set import fmv_math  # noqa: E402

WIN_FROM, WIN_TO = "2026-08-01", "2026-10-10"
REPS, SEED = 2000, 1229
CAPTURE = Path.home() / ".local" / "share" / "ebay-sold-comps-capture"


def load_capture_flags() -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for f in glob.glob(str(CAPTURE / "raw_responses*")):
        op = gzip.open if f.endswith(".gz") else open
        with op(f, "rt") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for it in (rec.get("response") or {}).get("items") or []:
                    if it.get("itemId") and isinstance(it.get("bestOfferAccepted"), bool):
                        flags[str(it["itemId"])] = it["bestOfferAccepted"]
    return flags


def load_slab_ledger() -> dict[int, list[dict]]:
    rows, after = [], 0
    while True:
        r = subprocess.run(["comics-api", "GET", f"/api/comics/comps/all?pool=slab&after_id={after}&limit=2000"],
                           capture_output=True, text=True, check=True)
        page = json.loads(r.stdout)
        if not page:
            break
        rows += page
        after = page[-1]["id"]
    out: dict[int, list[dict]] = defaultdict(list)
    seen: set[tuple[int, str]] = set()
    for r in rows:
        if (r["certifier"] != "cgc" or r["label"] != "universal" or r["excluded_code"] is not None
                or not r["price"] or r["price"] <= 0 or r["grade"] is None):
            continue
        key = (r["comic_id"], r["product_id"])
        if key in seen:
            continue
        seen.add(key)
        d = {k: r[k] for k in ("provider", "product_id", "title", "price", "sold_date", "grade",
                               "page_quality", "first_seen_at")}
        d["_date"] = (fmv_math._parse_sold_date(d["sold_date"])
                      or fmv_math._parse_sold_date(d["first_seen_at"]))
        d["sold_date"] = d["_date"].isoformat() if d["_date"] else None
        out[r["comic_id"]].append(d)
    return out


def price(comps, grade, as_of, pq):
    return fmv_math.graded_fmv(comps, grade, certifier="cgc", label="universal", as_of=as_of,
                               page_quality=None if pq == "unknown" else pq)


def score(res, y):
    lo, hi = res.get("fmv_low"), res.get("fmv_high")
    if lo is None or hi is None or not y:
        return None
    return {"winkler": winkler_score(lo, hi, y, DEFAULT_ALPHA) / y, "hit": lo <= y <= hi,
            "bias": ((lo + hi) / 2 - y) / y, "high": hi}


def boot(by_book: dict, rng: random.Random) -> tuple[float, float]:
    books = list(by_book)
    reps = []
    for _ in range(REPS):
        vals = [v for _ in books for v in by_book[rng.choice(books)]]
        reps.append(mean(vals))
    reps.sort()
    return reps[int(0.025 * REPS)], reps[int(0.975 * REPS) - 1]


def summarize(rows, key):
    sc = [r for r in rows if r[key]]
    out = {"n": len(rows), "priced": len(sc)}
    for m in ("winkler", "hit", "bias"):
        by_book = defaultdict(list)
        for r in sc:
            by_book[r["comic_id"]].append(float(r[key][m]))
        out[m] = mean(v for vs in by_book.values() for v in vs)
        out[m + "_ci"] = boot(by_book, random.Random(SEED))
    return out


def paired(rows, key, ref="baseline"):
    by_book = defaultdict(list)
    for r in rows:
        if r[key] and r[ref]:
            by_book[r["comic_id"]].append(r[key]["winkler"] - r[ref]["winkler"])
    vals = [v for vs in by_book.values() for v in vs]
    return len(vals), mean(vals), boot(by_book, random.Random(SEED))


def main() -> int:
    ts = T.load_test_set()
    books = {b["comic_id"]: b for b in ts["books"]}
    cap = load_capture_flags()
    ledger = load_slab_ledger()
    pc_by_id = {s["product_id"]: s for s in ts["sales"]}

    # R: accepted / list on PC best-offer sales before the window (fit set, no leakage)
    fit = [s["price"] / s["ask"] for s in ts["sales"]
           if s["best_offer"] and s["ask"] and s["price"] and s["sold_date"] < WIN_FROM
           and s["source"] == "ebay" and 0 < s["price"] <= s["ask"]]
    R = median(fit)
    inwin = [s["price"] / s["ask"] for s in ts["sales"]
             if s["best_offer"] and s["ask"] and s["price"] and WIN_FROM <= s["sold_date"] <= WIN_TO
             and s["source"] == "ebay" and 0 < s["price"] <= s["ask"]]

    flag_src = Counter()
    for cid in books:
        for c in ledger.get(cid, []):
            pid = str(c["product_id"])
            pc = pc_by_id.get(pid)
            c["_bo_cap"] = cap.get(pid) is True
            if pid in cap:
                c["_bo"] = cap[pid]
                flag_src["capture " + str(cap[pid])] += 1
            elif pc is not None:
                c["_bo"] = bool(pc["best_offer"])
                flag_src["pc " + str(c["_bo"])] += 1
            else:
                c["_bo"] = False
                flag_src["unknown"] += 1
            c["_pc_acc"] = pc["price"] if (pc and pc["best_offer"] and pc["price"]) else None

    all_c = [c for cid in books for c in ledger.get(cid, [])]
    SHIFT = 1 - (1 - R) * sum(c["_bo"] for c in all_c) / len(all_c)

    def variant(comps, opt, flag):
        if opt == "baseline":
            return comps
        if opt == "drop":
            return [c for c in comps if not c[flag]]
        if opt == "ratio":
            return [{**c, "price": round(c["price"] * R, 2)} if c[flag] else c for c in comps]
        if opt == "placebo":  # same R on the UNFLAGGED comps: is the gain about best offer at all?
            return [c if c[flag] else {**c, "price": round(c["price"] * R, 2)} for c in comps]
        if opt == "shift":  # uniform shift of every comp, same average size as (b)
            return [{**c, "price": round(c["price"] * SHIFT, 2)} for c in comps]
        if opt == "pc":
            return [{**c, "price": c["_pc_acc"]} if (c[flag] and c["_pc_acc"]) else c for c in comps]
        if opt == "shipped":  # the production path: flag on the comp, fmv_math scales it
            return [{**c, "best_offer": c[flag]} for c in comps]
        raise ValueError(opt)

    opts = [("baseline", "baseline", "_bo"), ("drop", "drop", "_bo"), ("ratio", "ratio", "_bo"),
            ("pc", "pc", "_bo"), ("drop-cap", "drop", "_bo_cap"), ("ratio-cap", "ratio", "_bo_cap"),
            ("placebo", "placebo", "_bo"), ("shift", "shift", "_bo")]
    if hasattr(fmv_math, "BEST_OFFER_ACCEPTED_RATIO"):
        opts.append(("shipped", "shipped", "_bo_cap"))
    rows, pool_share = [], Counter()
    for cid, b in books.items():
        led = ledger.get(cid, [])
        for s in T.universal_sales(ts, cid):
            if not (s["sold_date"] and WIN_FROM <= s["sold_date"] <= WIN_TO) or s["price_outlier"] is True:
                continue
            d = date.fromisoformat(s["sold_date"])
            comps = [c for c in led if c["_date"] and c["_date"] < d and not T.same_sale(s, c)]
            pool_share["comps"] += len(comps)
            pool_share["flagged"] += sum(c["_bo"] for c in comps)
            pool_share["pc_joined"] += sum(bool(c["_bo"] and c["_pc_acc"]) for c in comps)
            row = {"comic_id": cid, "era": b["era"], "source": s["source"], "y": s["price"]}
            for name, opt, flag in opts:
                res = price(variant(comps, opt, flag), s["grade"], d - timedelta(days=1), s["page_quality"])
                row[name] = score(res, s["price"])
            rows.append(row)

    print(f"Test set built {ts['meta']['built']}; window {WIN_FROM}..{WIN_TO}; {len(rows)} sales (outliers dropped)")
    print(f"R (PC eBay best-offer accepted/list, before window) = {R:.3f} on n={len(fit)};"
          f" in-window median {median(inwin):.3f} on n={len(inwin)}")
    print(f"Uniform-shift control factor = {SHIFT:.3f}")
    print("Ledger flag source (test-set books):", dict(flag_src))
    print(f"Comp-pool rows seen by the scorer: {pool_share['comps']}, best-offer flagged"
          f" {pool_share['flagged'] / pool_share['comps']:.1%}, PC-joined accepted price on"
          f" {pool_share['pc_joined'] / max(pool_share['flagged'], 1):.1%} of flagged")
    print()
    for name, _, _ in opts:
        s = summarize(rows, name)
        line = (f"{name:<10} priced {s['priced']}/{s['n']} ({s['priced'] / s['n']:.0%}) | Winkler {s['winkler']:.3f}"
                f" [{s['winkler_ci'][0]:.3f}, {s['winkler_ci'][1]:.3f}] | in-band {s['hit']:.0%} | bias {s['bias']:+.3f}"
                f" [{s['bias_ci'][0]:+.3f}, {s['bias_ci'][1]:+.3f}]")
        if name != "baseline":
            n, dlt, ci = paired(rows, name)
            line += f" | paired dWinkler {dlt:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}] on {n}"
        print(line)
    names = {name for name, _, _ in opts}
    for a, b in (("ratio", "shift"), ("ratio-cap", "shift"), ("ratio", "pc"), ("shipped", "ratio-cap")):
        if a in names:
            n, dlt, ci = paired(rows, a, b)
            print(f"paired {a} - {b}: dWinkler {dlt:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}] on {n}")
    if "shipped" in names:  # fmv_high feeds the bid cap: how far does it move?
        moves = [r["shipped"]["high"] / r["baseline"]["high"] for r in rows if r["shipped"] and r["baseline"]]
        print(f"shipped fmv_high / baseline fmv_high: median {median(moves):.3f},"
              f" lowered on {sum(m < 1 for m in moves)}/{len(moves)}, raised on {sum(m > 1 for m in moves)}")
    print()
    for cut, sel in (("eBay", lambda r: r["source"] == "ebay"),
                     *((e, (lambda e: lambda r: r["era"] == e)(e)) for e in ("silver", "bronze", "copper", "modern"))):
        sub = [r for r in rows if sel(r)]
        parts = []
        for name, _, _ in opts:
            s = summarize(sub, name)
            parts.append(f"{name} {s['winkler']:.3f}")
        print(f"  {cut:<7} n={len(sub)}: " + " | ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
