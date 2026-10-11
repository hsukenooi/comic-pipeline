#!/usr/bin/env python3
"""BUI-1230: tune graded_fmv band constants on the PriceCharting slab test set.
DIAGNOSTIC ONLY, read-only. No production code is touched.

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-11-graded-fmv-band-tuning.py \\
        [--json out.json]

Same truth rows and pricing setup as the BUI-1229 baseline
(`2026-10-11-pc-slab-test-set.py`): every PC CGC universal sale dated
2026-08-01..2026-10-10 (price outliers dropped) is priced as of the day before
it from LEDGER comps sold strictly before it, with every ledger row that
`same_sale` matches hidden. Truth is PC's SOLD price (the accepted offer on a
best-offer sale); the ledger comps hold LIST price there (BUI-1222).

`band()` below is a parameterized re-implementation of `fmv_math.graded_fmv`
(exact tier, lone-sale tier, ladder tier, the BUI-1186/1204 page-quality widen,
the envelope clamp, and every refusal). It reuses fmv_math's pure primitives
(`graded_pool`, rung medians, `_bracket_interpolate`, the envelope clamp,
`monotonicity_violations`, weighted quantiles). At the CURRENT constants it is
checked row by row against the real `graded_fmv`; the parity line prints first.

Holdout: books, not sales. The split is fixed below BEFORE any result is read:
books sorted by comic_id within each era, shuffled with seed 1230, every third
book held out. Configs are ranked on TRAIN only; the headline is HOLDOUT.

Selection rule (so a config can't win by refusing hard sales): a config is
eligible only if its train coverage is at least the current constants' train
coverage; among those, lowest train Winkler wins. The gain is also scored
PAIRED, on sales both configs price, with a book-clustered bootstrap CI.

Score: Winkler (alpha 0.5) / sale price; CIs as in BUI-1229 (95% percentile
bootstrap, 2000 reps resampling BOOKS, seed 1229). Opens the ledger mode=ro.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pc_slab_test_set as T  # noqa: E402
from pc_slab_test_set import fmv_math as F  # noqa: E402

WIN_FROM, WIN_TO = "2026-08-01", "2026-10-10"
REPS, BOOT_SEED, SPLIT_SEED = 2000, 1229, 1230


@dataclass(frozen=True)
class P:
    q: float = 0.25            # exact-tier band = weighted Q(q)..Q(1-q)
    h_direct: float = 0.0      # exact-tier half-width floor, relative to the median
    h_point: float = 0.0       # half-width given to point tiers (ladder, lone sale)
    exact_min_n: float = F.GRADED_EXACT_MIN_EFFECTIVE_N   # 2.0
    min_rungs: int = F.GRADED_LADDER_MIN_RUNGS            # 3
    window: float = 0.0        # exact bucket reads grades within +/- window
    ratio: float = F.SMALL_POOL_MAX_RATIO                 # 3.0: too_sparse + lone bracket


CURRENT = P()
# The shippable subset: only the two width knobs. `ratio` is the BUI-179 money
# guard (too_sparse, lone-sale bracket), so it stays at 3.0 whatever it scores.
SHIP = P(h_direct=0.15, h_point=0.15)
GRID = {
    "q": (0.25, 0.15),
    "h_direct": (0.0, 0.15, 0.30),
    "h_point": (0.0, 0.15, 0.30, 0.45),
    "exact_min_n": (1.0, 2.0),
    "min_rungs": (2, 3),
    "window": (0.0, 0.5),
    "ratio": (3.0, math.inf),
}
MIN_BUCKET_N = F.GRADED_LADDER_MIN_BUCKET_N  # 1, rung anchor eligibility (not tuned)


# ─── the parameterized band ────────────────────────────────────────────────

def _refuse(reason: str) -> dict:
    return {"low": None, "high": None, "basis": None, "reason": reason}


def _finish(low: float, high: float, med: float, h: float, basis: str) -> dict:
    if h:
        low, high = min(low, med * (1 - h)), max(high, med * (1 + h))
    return {"low": F.clean_round(low), "high": F.clean_round(high), "basis": basis, "reason": None}


def _direct(exact: list[dict], ladder: dict, eff: dict, tg: float, p: P) -> dict:
    prices = [float(c["price"]) for c in exact]
    weights = [float(c["weight"]) for c in exact]
    n = sum(weights)
    low = F.weighted_quartile(prices, weights, p.q)
    high = F.weighted_quartile(prices, weights, 1 - p.q)
    med = F.weighted_median(prices, weights)
    capped, clamped = F._cgc_ladder_price_and_clamp(
        {**ladder, tg: med}, tg, counts={**eff, tg: n}, min_bucket_n=MIN_BUCKET_N)
    if clamped and capped is not None:
        low, high, med = min(low, capped), min(high, capped), min(med, capped)
    elif n < F.OUTLIER_ROBUST_BUCKET_N:
        lo, hi = min(prices), max(prices)
        if lo <= 0 or hi / lo > p.ratio:
            return _refuse("too_sparse")
    return _finish(low, high, med, p.h_direct, "direct")


def _eligible(ladder: dict, eff: dict, tg: float) -> list[float]:
    return [g for g in ladder if g != tg and eff.get(g, 0.0) >= MIN_BUCKET_N]


def _lone(exact: list[dict], ladder: dict, eff: dict, tg: float, p: P) -> dict | None:
    if len(exact) != 1 or float(exact[0]["weight"]) < F.GRADED_LONE_SALE_MIN_WEIGHT:
        return None
    sale = float(exact[0]["price"])
    if len(_eligible(ladder, eff, tg)) < p.min_rungs:
        return None
    below = max((g for g in ladder if g < tg and eff.get(g, 0.0) >= MIN_BUCKET_N), default=None)
    above = min((g for g in ladder if g > tg and eff.get(g, 0.0) >= MIN_BUCKET_N), default=None)
    if below is None or above is None:
        return None
    lo, hi = ladder[below], ladder[above]
    if lo <= 0 or hi / lo > p.ratio or not lo <= sale <= hi:
        return None
    return _finish(sale, sale, sale, p.h_point, "lone_sale")


def _ladder(ladder: dict, eff: dict, tg: float, p: P) -> dict:
    lx = {g: v for g, v in ladder.items() if g != tg}
    ex = {g: v for g, v in eff.items() if g != tg}
    if len(_eligible(lx, ex, tg)) < p.min_rungs:
        return _refuse("ladder_too_thin")
    br = F._bracket_interpolate(lx, tg, ex, MIN_BUCKET_N)
    if br is None:
        return _refuse("outside_ladder")
    if (br["grade_below"], br["grade_above"]) in F.monotonicity_violations(lx):
        return _refuse("ladder_non_monotone")
    price, _ = F._cgc_ladder_price_and_clamp(lx, tg, counts=ex, min_bucket_n=MIN_BUCKET_N)
    if price is None:
        return _refuse("outside_ladder")
    pt = F.clean_round(price)
    return _finish(pt, pt, pt, p.h_point, "ladder")


def band(full: list[dict], tg: float, pq: str | None, p: P) -> dict:
    """graded_fmv's decision tree with its constants as parameters. `full` is
    the already-aged `graded_pool` output (age weights are not tuned here)."""
    if not full:
        return _refuse("no_certifier_pool")
    pool, _, _ = F._graded_page_quality_filter(full, pq)
    tg = float(tg)
    near = lambda c: abs(float(c["grade"]) - tg) <= p.window + 1e-9  # noqa: E731
    exact = [c for c in pool if near(c)]
    exact_n = sum(float(c["weight"]) for c in exact)
    ladder, eff = F.bucket_weighted_medians(full), F.bucket_effective_n(full)
    scoped = len(pool) < len(full)
    if not exact and scoped:  # BUI-1186
        widened = [c for c in full if near(c)]
        if widened:
            exact, exact_n = widened, sum(float(c["weight"]) for c in widened)
    elif (scoped and exact_n < p.exact_min_n
          and sum(1 for c in exact if c.get("page_quality") == pq) <= 1):  # BUI-1204
        widened = [c for c in full if near(c)]
        wn = sum(float(c["weight"]) for c in widened)
        if len(widened) > len(exact) and wn >= p.exact_min_n:
            r = _direct(widened, ladder, eff, tg, p)
            if r["reason"] is None:
                return r
    if exact_n >= p.exact_min_n:
        return _direct(exact, ladder, eff, tg, p)
    lone = _lone(exact, ladder, eff, tg, p)
    if lone is not None:
        return lone
    return _ladder(ladder, eff, tg, p)


# ─── data ─────────────────────────────────────────────────────────────────

def split(books: dict) -> set[int]:
    """Holdout comic_ids. Fixed before any result was read: stratified by era,
    shuffled with SPLIT_SEED, every third book held out."""
    rng, hold = random.Random(SPLIT_SEED), set()
    for e in ("silver", "bronze", "copper", "modern"):
        ids = sorted(c for c, b in books.items() if b["era"] == e)
        rng.shuffle(ids)
        hold.update(ids[i] for i in range(0, len(ids), 3))
    return hold


def load_rows() -> tuple[list[dict], dict, set[int]]:
    ts = T.load_test_set()
    books = {b["comic_id"]: b for b in ts["books"]}
    hold = split(books)
    conn = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True)
    rows = []
    for cid, b in books.items():
        ledger = T.load_ledger(conn, cid)
        for s in T.universal_sales(ts, cid):
            if not (s["sold_date"] and WIN_FROM <= s["sold_date"] <= WIN_TO) or s["price_outlier"] is True:
                continue
            d = date.fromisoformat(s["sold_date"])
            comps = [c for c in ledger if c["_date"] and c["_date"] < d and not T.same_sale(s, c)]
            as_of = d - timedelta(days=1)
            pq = None if s["page_quality"] == "unknown" else s["page_quality"]
            full, _, _ = F.graded_pool(comps, as_of=as_of)
            rows.append({"comic_id": cid, "era": b["era"], "source": s["source"], "grade": s["grade"],
                         "pq": pq, "y": s["price"], "ask": s["ask"], "pool": full, "comps": comps,
                         "as_of": as_of, "holdout": cid in hold})
    return rows, books, hold


# ─── scoring ──────────────────────────────────────────────────────────────

def score(res: dict, y: float) -> dict | None:
    lo, hi = res["low"], res["high"]
    if lo is None or hi is None:
        return None
    return {"winkler": winkler_score(lo, hi, y, DEFAULT_ALPHA) / y, "hit": float(lo <= y <= hi),
            "bias": ((lo + hi) / 2 - y) / y, "width": (hi - lo) / y}


def run(rows: list[dict], p: P) -> list[dict | None]:
    return [score(band(r["pool"], r["grade"], r["pq"], p), r["y"]) for r in rows]


def point(rows: list[dict], sc: list[dict | None]) -> dict:
    got = [s for s in sc if s]
    if not got:
        return {"n": len(rows), "cov": 0.0}
    return {"n": len(rows), "cov": len(got) / len(rows), "winkler": mean(s["winkler"] for s in got),
            "hit": mean(s["hit"] for s in got), "bias": mean(s["bias"] for s in got),
            "width": mean(s["width"] for s in got)}


def boot(rows: list[dict], vals: list[float | None]) -> tuple[float, float] | None:
    """Book-clustered 95% CI of the sale-weighted mean of `vals` (None skipped)."""
    by = defaultdict(list)
    for r, v in zip(rows, vals):
        if v is not None:
            by[r["comic_id"]].append(v)
    ids = list(by)
    if not ids:
        return None
    rng, reps = random.Random(BOOT_SEED), []
    for _ in range(REPS):
        vs = [v for _ in ids for v in by[rng.choice(ids)]]
        reps.append(mean(vs))
    reps.sort()
    return reps[int(0.025 * REPS)], reps[int(0.975 * REPS) - 1]


def full_summary(rows: list[dict], sc: list[dict | None]) -> dict:
    s = point(rows, sc)
    s["books"] = len({r["comic_id"] for r, x in zip(rows, sc) if x})
    if s.get("winkler") is not None:
        for m in ("winkler", "hit", "bias"):
            s[m + "_ci"] = boot(rows, [x[m] if x else None for x in sc])
        s["cov_ci"] = boot(rows, [1.0 if x else 0.0 for x in sc])
    return s


def fmt(s: dict) -> str:
    if not s.get("winkler"):
        return f"n={s['n']} priced=0"
    ci = lambda k: f"[{s[k + '_ci'][0]:.3f}, {s[k + '_ci'][1]:.3f}]"  # noqa: E731
    return (f"n={s['n']} books={s['books']} cov {s['cov']:.0%} {ci('cov')} | "
            f"Winkler {s['winkler']:.3f} {ci('winkler')} | in-band {s['hit']:.0%} {ci('hit')} | "
            f"bias {s['bias']:+.3f} | rel width {s['width']:.2f}")


def paired(rows: list[dict], a: list, b: list) -> dict:
    """Winkler(a) - Winkler(b) on sales both price; negative = a better."""
    d = [(x["winkler"] - y["winkler"]) if x and y else None for x, y in zip(a, b)]
    got = [v for v in d if v is not None]
    return {"n": len(got), "diff": mean(got) if got else None, "ci": boot(rows, d)}


# ─── main ─────────────────────────────────────────────────────────────────

def parity(rows: list[dict]) -> tuple[int, int, list]:
    bad = []
    for r in rows:
        mine = band(r["pool"], r["grade"], r["pq"], CURRENT)
        prod = F.graded_fmv(r["comps"], r["grade"], certifier="cgc", label="universal",
                            as_of=r["as_of"], page_quality=r["pq"])
        if (mine["low"], mine["high"], mine["reason"]) != (prod.get("fmv_low"), prod.get("fmv_high"),
                                                          prod.get("flag_reason")):
            bad.append((r["comic_id"], r["grade"], mine, prod.get("fmv_low"), prod.get("fmv_high"),
                        prod.get("flag_reason")))
    return len(rows) - len(bad), len(rows), bad


def main() -> int:
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    rows, books, hold = load_rows()
    train = [r for r in rows if not r["holdout"]]
    test = [r for r in rows if r["holdout"]]
    ok, n, bad = parity(rows)
    print(f"Parity at current constants: {ok}/{n} rows identical to fmv_math.graded_fmv")
    for b in bad[:5]:
        print("  MISMATCH", b)
    print(f"Split (seed {SPLIT_SEED}, before results): train {len(books) - len(hold)} books / "
          f"{len(train)} sales; holdout {len(hold)} books / {len(test)} sales")
    print("  holdout comic_ids:", sorted(hold))

    keys = list(GRID)
    configs = [P(**dict(zip(keys, v))) for v in itertools.product(*(GRID[k] for k in keys))]
    print(f"\nGrid: {len(configs)} configs ({' x '.join(str(len(GRID[k])) for k in keys)})")
    cur_tr = run(train, CURRENT)
    cur_cov = point(train, cur_tr)["cov"]
    results = []
    for p in configs:
        s = point(train, run(train, p))
        results.append((p, s))
    eligible = [(p, s) for p, s in results if s["cov"] >= cur_cov - 1e-12 and s.get("winkler")]
    eligible.sort(key=lambda ps: ps[1]["winkler"])
    best = eligible[0][0]
    print(f"Eligible (train coverage >= current {cur_cov:.1%}): {len(eligible)} of {len(configs)}")
    print("Top 8 on TRAIN:")
    for p, s in eligible[:8]:
        print(f"  {asdict(p)} -> Winkler {s['winkler']:.3f} cov {s['cov']:.0%} in-band {s['hit']:.0%}")
    unconstrained = min((ps for ps in results if ps[1].get("winkler")), key=lambda ps: ps[1]["winkler"])
    print(f"Unconstrained train best: {asdict(unconstrained[0])} -> "
          f"Winkler {unconstrained[1]['winkler']:.3f} cov {unconstrained[1]['cov']:.0%}")

    # one-at-a-time sensitivity on train: which knob carries the gain
    print("\nOne knob at a time from CURRENT (train Winkler / coverage):")
    for k in keys:
        for v in GRID[k]:
            p = P(**{**asdict(CURRENT), k: v})
            s = point(train, run(train, p))
            print(f"  {k}={v}: {s.get('winkler', float('nan')):.3f} / {s['cov']:.0%}")

    print("\nTop 5 train configs scored on HOLDOUT (stability of the pick):")
    for p, s in eligible[:5]:
        h = point(test, run(test, p))
        print(f"  {asdict(p)} -> train {s['winkler']:.3f}, holdout {h['winkler']:.3f} cov {h['cov']:.0%}")

    out = {"best": asdict(best), "current": asdict(CURRENT), "n_configs": len(configs)}
    print(f"\nBEST (train-selected): {asdict(best)}")
    for lab, rs in (("TRAIN", train), ("HOLDOUT", test), ("ALL", rows)):
        sc_c, sc_b, sc_s = run(rs, CURRENT), run(rs, best), run(rs, SHIP)
        print(f"\n{lab}")
        print("  current :", fmt(full_summary(rs, sc_c)))
        print("  best    :", fmt(full_summary(rs, sc_b)))
        print("  ship    :", fmt(full_summary(rs, sc_s)))
        pr, ps = paired(rs, sc_b, sc_c), paired(rs, sc_s, sc_c)
        for nm, x in (("best", pr), ("ship", ps)):
            print(f"  paired {nm} - current on {x['n']} co-priced sales: {x['diff']:+.3f} "
                  f"[{x['ci'][0]:+.3f}, {x['ci'][1]:+.3f}]")
        out[lab] = {"current": full_summary(rs, sc_c), "best": full_summary(rs, sc_b),
                    "ship": full_summary(rs, sc_s), "paired": pr, "paired_ship": ps}
        if lab == "HOLDOUT":
            for e in ("silver", "bronze", "copper", "modern"):
                er = [r for r in rs if r["era"] == e]
                if er:
                    print(f"  era {e:<7} current:", fmt(full_summary(er, run(er, CURRENT))))
                    print(f"  era {e:<7} best   :", fmt(full_summary(er, run(er, best))))
            for src in ("ebay", "heritage"):
                sr = [r for r in rs if r["source"] == src]
                print(f"  {src:<8} current:", fmt(full_summary(sr, run(sr, CURRENT))))
                print(f"  {src:<8} best   :", fmt(full_summary(sr, run(sr, best))))
            # list basis
            lr = [{**r, "y": r["ask"] or r["y"]} for r in rs]
            print("  list basis current:", fmt(full_summary(lr, run(lr, CURRENT))))
            print("  list basis best   :", fmt(full_summary(lr, run(lr, best))))

    # money side: fmv_high feeds the bid cap (max_bid = factor x fmv_high)
    ratios, under_c, under_b = [], 0, 0
    for r in rows:
        c, b = band(r["pool"], r["grade"], r["pq"], CURRENT), band(r["pool"], r["grade"], r["pq"], SHIP)
        if c["high"] and b["high"]:
            ratios.append(b["high"] / c["high"])
            under_c += r["y"] < c["high"]
            under_b += r["y"] < b["high"]
    ratios.sort()
    print(f"\nfmv_high ship/current on {len(ratios)} co-priced sales: median {ratios[len(ratios) // 2]:.3f}, "
          f"mean {mean(ratios):.3f}; sale below fmv_high: current {under_c / len(ratios):.0%}, "
          f"ship {under_b / len(ratios):.0%}")

    # tier mix: where the current band's misses come from (all rows)
    print("\nBy pricing basis, all rows (current vs best):")
    for p, lab in ((CURRENT, "current"), (best, "best")):
        by = defaultdict(list)
        for r in rows:
            res = band(r["pool"], r["grade"], r["pq"], p)
            by[res["basis"] or "refused:" + res["reason"]].append(score(res, r["y"]))
        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
            got = [s for s in v if s]
            line = (f"Winkler {mean(s['winkler'] for s in got):.3f} in-band {mean(s['hit'] for s in got):.0%}"
                    if got else "")
            print(f"  {lab:<8} {k:<30} n={len(v):<4} {line}")

    if out_json:
        Path(out_json).write_text(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
