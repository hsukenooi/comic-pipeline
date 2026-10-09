#!/usr/bin/env python3
"""Backtest merging GRADED raw-pool ledger comps into the raw FMV pool
(BUI-1213). DIAGNOSTIC ONLY, read-only, no production change.

Question: the raw path prices only from the live provider window (~90 days)
and never reads the comps ledger. The slab path already merges ledger comps
with step age weights (fmv_math.graded_comp_weight: 1.0 to 90 days, 0.5 to
365, excluded after). Would the same merge help the raw path?

Reconstruction assumption (stated plainly): no provider is called. Both pools
are rebuilt from the comps ledger, which archives every past live fetch.
  * live-only = the book's graded raw ledger comps sold in [D - 90, D)
  * merged    = live-only + the book's graded raw ledger comps sold in
                [D - 365, D - 90), each at age weight 0.5
where D is the evaluation date (the auction's end date, the held-out sale's
date, or the refused FMV row's last update). Strictly before D, so no comp
sold on or after the outcome enters its pool. The ledger is only a proxy for
what the live fetch returned on D (it holds every fetch's comps for the book,
so it can hold MORE than one fetch did).

Weights: compute_fmv has no per-comp weight input, so `_recency_weights` is
wrapped to multiply its exponential recency weight by each comp's age weight
(`_age_w`, 1.0 for live-window comps). A live-only pool therefore prices
exactly as compute_fmv does today. The grade guards (build_pool widening,
_classify_pool) are unweighted in production and stay unweighted here.

Ledger load (same rules as BUI-1005's backtest, plus exclusion stamps):
  * pool='raw', a parsed grade, price > 0, a parseable sold date
  * `excluded_code` stamped -> dropped (BUI-947/BUI-1018); counted
  * tier 'no-variant'/'broader' -> dropped (identity not guaranteed); counted
  * the CURRENT sold_comps.hard_exclude(title) re-applied -> dropped; counted
    (ledger rows predate later guards)
  * cross_title / store_variant (graded_identity_exclude) -> dropped; counted
  * dedupe by (comic_id, product_id), then (comic_id, grade, price, date)
  * an auction's own listing (product_id == bid item_id) never enters its pool

Three measures:
  1. Rescue: stored raw FMV rows refused one_sided/too_wide/too_sparse,
     re-priced at D = fmv_updated_at, using only ledger rows first seen by D.
  2. Accuracy: our resolved raw auctions (GET /api/comics/accuracy rows)
     and, secondary, a leave-one-out over ledger graded sales (BUI-1005's
     shape). Winkler score (BUI-979, alpha 0.5) / price, mean and median.
  3. Pollution: among the ledger rows the merge would ADD (91-365 days,
     same book, graded), how many carry an exclusion stamp, an unsafe tier,
     or fail a current title guard.

    uv run --project plugins/gixen-overlay python \\
        docs/audit/2026-10-10-raw-ledger-merge-backtest.py [--cache DIR] [--rows-json out.json]

Reads the comics server over HTTP via the `comics-api` wrapper only
(`/api/comics/comps/all?pool=raw`, `/api/comics/accuracy`, `/api/comics`).
Never opens the DB file. `--cache DIR` reuses/stores the three JSON reads.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "fmv" / "src"))
sys.path.insert(0, str(ROOT / "apps" / "ebay" / "src"))
import fmv_math  # noqa: E402
import sold_comps  # noqa: E402

LIVE_DAYS = fmv_math.GRADED_FRESH_MAX_AGE_DAYS      # 90
STALE_DAYS = fmv_math.GRADED_STALE_MAX_AGE_DAYS     # 365
EXCLUDED_TIERS = ("no-variant", "broader")
REFUSALS = ("one_sided", "too_wide", "too_sparse")


# ─── Weight hook ────────────────────────────────────────────────────────────

_orig_recency_weights = fmv_math._recency_weights


def _merged_recency_weights(pool_comps: list[dict]) -> list[float]:
    base = _orig_recency_weights(pool_comps)
    return [w * c.get("_age_w", 1.0) for w, c in zip(base, pool_comps)]


fmv_math._recency_weights = _merged_recency_weights


# ─── Loading over HTTP ──────────────────────────────────────────────────────

def api(path: str):
    out = subprocess.run(["comics-api", "GET", path], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def cached(cache: Path | None, name: str, fn):
    if cache is not None and (cache / name).exists():
        return json.loads((cache / name).read_text())
    data = fn()
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / name).write_text(json.dumps(data))
    return data


def page_ledger() -> list[dict]:
    rows, after = [], 0
    while True:
        page = api(f"/api/comics/comps/all?pool=raw&after_id={after}&limit=5000")
        if not page:
            return rows
        rows += page
        after = page[-1]["id"]


def to_date(s) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return fmv_math._parse_sold_date(s)


def load_comps(raw: list[dict], issues: dict[int, str]) -> tuple[list[dict], Counter, list[dict]]:
    """Clean graded raw comps, plus every DROPPED row with its reason (so the
    pollution count can see what the merge would have added without guards)."""
    stats: Counter = Counter()
    dropped: list[dict] = []
    # An exclusion stamp is honored for EVERY copy of that listing on that book
    # (cross-provider / re-fetched copies share a product_id but not a row).
    stamped = {(r["comic_id"], str(r["product_id"])) for r in raw
               if r.get("excluded_code") and r.get("product_id")}
    seen_pid: set = set()
    seen_val: set = set()
    out = []
    for r in sorted(raw, key=lambda r: r["id"]):
        if r.get("grade") is None or not r.get("price") or r["price"] <= 0:
            stats["ungraded_or_no_price"] += 1
            continue
        d = fmv_math._parse_sold_date(r.get("sold_date"))
        if d is None:
            stats["no_date"] += 1
            continue
        title = r.get("title") or ""
        reason = None
        if r.get("excluded_code") or (r["comic_id"], str(r.get("product_id"))) in stamped:
            reason = "exclusion_stamp"
        elif (r.get("tier") or "") in EXCLUDED_TIERS:
            reason = "unsafe_tier"
        elif sold_comps.hard_exclude(title):
            reason = "hard_exclude_now"
        else:
            reason = sold_comps.graded_identity_exclude(title, issue=issues.get(r["comic_id"]))
        row = {**r, "d": d, "sold_date": d.isoformat(),
               "seen": to_date(r.get("first_seen_at"))}
        if reason:
            stats[reason] += 1
            dropped.append({**row, "reason": reason})
            continue
        if r.get("product_id"):
            k = (r["comic_id"], str(r["product_id"]))
            if k in seen_pid:
                stats["dup_product"] += 1
                continue
            seen_pid.add(k)
        kv = (r["comic_id"], r["grade"], round(r["price"], 2), d)
        if kv in seen_val:
            stats["dup_value"] += 1
            continue
        seen_val.add(kv)
        out.append(row)
    stats["kept"] = len(out)
    return out, stats, dropped


# ─── Pools and pricing ──────────────────────────────────────────────────────

def pools(book: list[dict], D: date, *, exclude_pid: str | None = None,
          seen_by: date | None = None, skip: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(live, added) ledger comps for one book at evaluation date D.
    live = sold in [D-90, D); added = sold in [D-365, D-90). Never on/after D."""
    live, added = [], []
    for c in book:
        if c is skip or c["d"] >= D:
            continue
        if exclude_pid and str(c.get("product_id") or "") == exclude_pid:
            continue
        if seen_by is not None and (c["seen"] is None or c["seen"] > seen_by):
            continue
        age = (D - c["d"]).days
        w = fmv_math.graded_comp_weight(age)
        if w is None:
            continue
        (live if w == 1.0 else added).append(c)
    return live, added


def as_comp(c: dict, w: float) -> dict:
    return {"price": c["price"], "grade": c["grade"], "sold_date": c["sold_date"], "_age_w": w}


def price(live: list[dict], added: list[dict], g: float) -> dict:
    comps = [as_comp(c, 1.0) for c in live] + [
        as_comp(c, fmv_math.GRADED_STALE_WEIGHT) for c in added]
    r = fmv_math.compute_fmv(comps, g)
    return {"low": r["fmv_low"], "high": r["fmv_high"], "flag": r["flag_reason"], "n": r["n"]}


def priced(b: dict) -> bool:
    return b["flag"] is None and b["high"] is not None and b["low"] is not None and b["high"] > 0


def wsc(b: dict, y: float) -> float:
    return winkler_score(float(b["low"]), float(b["high"]), y, DEFAULT_ALPHA) / y


def summarize(rows: list[dict], key: str) -> str:
    ws = [wsc(r[key], r["price"]) for r in rows if priced(r[key])]
    if not ws:
        return "n=0"
    inb = sum(1 for r in rows if priced(r[key]) and r[key]["low"] <= r["price"] <= r[key]["high"])
    return (f"n={len(ws)} mean W/price={mean(ws):.3f} median={median(ws):.3f} "
            f"in-band={100 * inb / len(ws):.0f}%")


def accuracy_block(name: str, rows: list[dict]) -> dict:
    both = [r for r in rows if priced(r["live"]) and priced(r["merged"])]
    rescued = [r for r in rows if not priced(r["live"]) and priced(r["merged"])]
    lost = [r for r in rows if priced(r["live"]) and not priced(r["merged"])]
    changed = [r for r in both if (r["live"]["low"], r["live"]["high"]) != (r["merged"]["low"], r["merged"]["high"])]
    dw = [wsc(r["merged"], r["price"]) - wsc(r["live"], r["price"]) for r in changed]
    print(f"\n## {name}: N={len(rows)}")
    print(f"- live-only priced: {summarize(rows, 'live')}")
    print(f"- merged priced:    {summarize(rows, 'merged')}")
    print(f"- both price ({len(both)}): live {summarize(both, 'live')} | merged {summarize(both, 'merged')}")
    print(f"- band changed by merge on {len(changed)} of {len(both)}; paired dW/price mean "
          f"{mean(dw) if dw else 0:+.3f}, merged better {sum(d < 0 for d in dw)}, worse {sum(d > 0 for d in dw)}")
    print(f"- rescued (live refuses, merged prices) {len(rescued)}: merged {summarize(rescued, 'merged')}")
    print(f"- lost (live prices, merged refuses) {len(lost)}")
    live_flags = Counter(r["live"]["flag"] for r in rows if not priced(r["live"]))
    print(f"- live-only refusals: {dict(live_flags)}")
    return {"n": len(rows), "both": len(both), "rescued": len(rescued), "lost": len(lost),
            "changed": len(changed),
            "live_w": [wsc(r["live"], r["price"]) for r in rows if priced(r["live"])],
            "merged_w": [wsc(r["merged"], r["price"]) for r in rows if priced(r["merged"])],
            "rescued_w": [wsc(r["merged"], r["price"]) for r in rescued],
            "dw_changed": dw}


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    cache = Path(sys.argv[sys.argv.index("--cache") + 1]) if "--cache" in sys.argv else None
    raw = cached(cache, "raw_comps.json", page_ledger)
    acc = cached(cache, "acc.json", lambda: api("/api/comics/accuracy?days=365&include_rows=true"))
    comics = cached(cache, "comics.json", lambda: api("/api/comics"))
    issues = {c["id"]: c["issue"] for c in comics}
    for r in acc["rows"]:
        issues.setdefault(r["comic_id"], r["issue"])

    comps, stats, dropped = load_comps(raw, issues)
    print(f"alpha={DEFAULT_ALPHA}; raw ledger rows={len(raw)}; graded raw load: {dict(stats)}")
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    drop_by_book: dict[int, list[dict]] = defaultdict(list)
    for c in dropped:
        drop_by_book[c["comic_id"]].append(c)

    out: dict = {"stats": dict(stats)}

    # 1. Rescue on stored refused raw rows
    refused = [c for c in comics if c.get("certifier") in (None, "none") and c.get("fmv_id")
               and c.get("fmv_flag_reason") in REFUSALS and c.get("grade") is not None]
    resc, repro, rescue_rows = Counter(), Counter(), []
    for c in refused:
        D = to_date(c["fmv_updated_at"])
        # first seen by D (inclusive): only what the ledger held when the row was refused.
        live, added = pools(by_book.get(c["id"], []), D + timedelta(days=1), seen_by=D)
        lv, mg = price(live, [], c["grade"]), price(live, added, c["grade"])
        repro[(c["fmv_flag_reason"], "refuses" if not priced(lv) else "prices")] += 1
        if priced(mg) and not priced(lv):
            resc[c["fmv_flag_reason"]] += 1
            rescue_rows.append({"comic_id": c["id"], "book": f"{c['title']} #{c['issue']} ({c['year']})",
                                "grade": c["grade"], "flag": c["fmv_flag_reason"],
                                "live": lv, "merged": mg, "n_added": len(added)})
        elif priced(mg) and priced(lv):
            resc["live_recon_already_prices"] += 1
    print(f"\n## Rescue: {len(refused)} stored refused raw rows "
          f"({dict(Counter(c['fmv_flag_reason'] for c in refused))})")
    print(f"- live-only reconstruction vs stored flag: {dict(repro)}")
    print(f"- merged prices where live-only recon refuses: {sum(v for k, v in resc.items() if k in REFUSALS)} "
          f"by flag {dict(resc)}")
    for r in rescue_rows:
        print(f"  - {r['book']} @{r['grade']:g} [{r['flag']}] live={r['live']['flag']} "
              f"-> merged ${r['merged']['low']}-{r['merged']['high']} (+{r['n_added']} ledger)")
    out["rescue"] = {"refused": len(refused), "repro": {f"{a}|{b}": v for (a, b), v in repro.items()},
                     "rescued": dict(resc), "rows": rescue_rows}

    # 2a. Accuracy on our resolved raw auctions
    arows = []
    for a in acc["rows"]:
        if a.get("certifier") not in (None, "none") or a.get("grade") is None or not a.get("price"):
            continue
        D = to_date(a["ended"])
        live, added = pools(by_book.get(a["comic_id"], []), D, exclude_pid=str(a["item_id"]))
        arows.append({"bid_id": a["bid_id"], "comic_id": a["comic_id"], "price": float(a["price"]),
                      "grade": a["grade"], "ended": D.isoformat(), "n_added": len(added),
                      "live": price(live, [], a["grade"]), "merged": price(live, added, a["grade"])})
    out["auctions"] = accuracy_block("Accuracy: resolved raw auctions (ours)", arows)
    out["auctions"]["with_added"] = sum(1 for r in arows if r["n_added"])

    # 2b. Leave-one-out over ledger graded sales (BUI-1005 shape), auctions only
    lrows = []
    for c in comps:
        if c.get("buying_format") != "auction":
            continue
        live, added = pools(by_book[c["comic_id"]], c["d"], skip=c)
        if not live and not added:
            continue
        lrows.append({"comic_id": c["comic_id"], "price": c["price"], "grade": c["grade"],
                      "n_added": len(added),
                      "live": price(live, [], c["grade"]), "merged": price(live, added, c["grade"])})
    out["loo"] = accuracy_block("Accuracy: leave-one-out ledger auction sales", lrows)

    # 3. Pollution among the ledger rows the merge would ADD, over every pool
    # evaluated above (auctions + refused rows). Unique ledger rows: a row added
    # to several pools of one book counts once. Dropped rows are counted by
    # their guard; KEPT rows get one more wrong-book heuristic: every 19xx/20xx
    # year in the title sits more than 2 years from the book's year (a 2022
    # "6th series" #89 in a 1970 #89 pool). The same heuristic over the
    # live-window rows of the same pools is the baseline.
    years = {c["id"]: c.get("year") for c in comics}

    def year_mismatch(c: dict) -> bool:
        y = years.get(c["comic_id"])
        found = [int(m) for m in re.findall(r"(?<!\d)(19[3-9]\d|20[0-2]\d)(?!\d)", c.get("title") or "")]
        return bool(y and found and all(abs(f - y) > 2 for f in found))

    evals = [(a["comic_id"], to_date(a["ended"]), str(a["item_id"])) for a in acc["rows"]
             if a.get("certifier") in (None, "none") and a.get("grade") is not None]
    evals += [(c["id"], to_date(c["fmv_updated_at"]) + timedelta(days=1), "") for c in refused]
    added_ids: dict[int, dict] = {}
    live_ids: dict[int, dict] = {}
    for cid, D, pid in evals:
        for c in by_book.get(cid, []) + drop_by_book.get(cid, []):
            if c["d"] >= D or (pid and str(c.get("product_id") or "") == pid):
                continue
            age = (D - c["d"]).days
            if LIVE_DAYS < age <= STALE_DAYS:
                added_ids[c["id"]] = c
            elif age <= LIVE_DAYS:
                live_ids[c["id"]] = c
    pol = Counter(c.get("reason", "kept") for c in added_ids.values())
    kept = [c for c in added_ids.values() if "reason" not in c]
    live_kept = [c for c in live_ids.values() if "reason" not in c]
    ym_add = [c for c in kept if year_mismatch(c)]
    ym_live = sum(year_mismatch(c) for c in live_kept)
    print(f"\n## Pollution: unique ledger rows the merge would add = {len(added_ids)}")
    print(f"- by guard (dropped) / kept: {dict(pol)}")
    print(f"- year-mismatch (likely wrong book) among kept: {len(ym_add)}/{len(kept)} "
          f"({100 * len(ym_add) / max(1, len(kept)):.1f}%); live-window baseline "
          f"{ym_live}/{len(live_kept)} ({100 * ym_live / max(1, len(live_kept)):.1f}%)")
    for c in ym_add:
        print(f"  - book {c['comic_id']} ({years.get(c['comic_id'])}) | {c['grade']:g} "
              f"${c['price']:.2f} | {c['title']}")
    out["pollution"] = {"added_unique": len(added_ids), "by_reason": dict(pol),
                        "kept": len(kept), "year_mismatch_added": len(ym_add),
                        "live_kept": len(live_kept), "year_mismatch_live": ym_live}

    if "--rows-json" in sys.argv:
        Path(sys.argv[sys.argv.index("--rows-json") + 1]).write_text(
            json.dumps({"out": out, "auction_rows": arows}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
