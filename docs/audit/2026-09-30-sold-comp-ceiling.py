"""Measure a sold-comp CEILING bid cap for one_sided / too_wide refusals (BUI-1025).

DIAGNOSTIC ONLY. Opens ~/.comics-server/db.sqlite with mode=ro; nothing is
written to it and no provider is called.

The question: when `compute_fmv` refuses a raw book because its comps sit on
the wrong side of the target grade (one_sided) or span too many grades
(too_wide), can the cheapest sale at a HIGHER grade set a safe bid cap? This is
a cap, not a band, so it is scored on money, not on Winkler:

  overpay    cap > realized sale price (we would have paid more than the market)
  room lost  cap < realized sale price (we would have lost the auction)

Population: BUI-1005's leave-one-out sales (graded raw ledger sales with a
same-book comp within +/-2.0 in the prior 90 days), built by that harness's own
load_comps / training / price_current, imported unchanged. The refused cut is
the sales the current path flags one_sided or too_wide.

Ceiling pool, per refused sale at grade g: the comps `fmv_math.build_pool`
selects for g from the training comps (the same widened window compute_fmv
saw), keeping only those at grade >= g.
  one_sided: only when EVERY pool comp is above g (a pool entirely below g has
             no ceiling and gets no cap).
  too_wide:  the comps at or above g.
Either way at least MIN_PRICEABLE_POOL (2) such comps are required; a pool
with one comp above gets no cap.

  A (min)    ceiling = lowest sale price among the ceiling pool
  B (rung)   ceiling = median price of the lowest grade rung in the pool
  cap        clean_round(HAIRCUT x ceiling), HAIRCUT = 0.60, fmv_math's LOW
             rung (== INTERPOLATED_BID_FACTOR, what a LOW-confidence
             interpolated band gets). Arms at 0.80 (BASE_BID_FACTOR) and 1.00
             (control: the same comps, no haircut) are reported alongside.

Yardstick: the accepted path's own max_bid on the accepted sales, from the
unchanged compute_fmv with no photo grade_confidence (0.80 x fmv_high, or 0.60
on a bracket-interpolated band).

Leakage: training is strictly before the sale's date (harness `training`),
with the harness's same-grade near-duplicate drop. The ceiling pool
additionally drops any comp priced within $0.01 of the held-out sale and sold
within 7 days of it at ANY grade, so a provider copy with a drifted grade can
never set its own ceiling.

Pin: every ledger read is limited to comps first seen before AS_OF
(2026-09-30T01:00 UTC), because comic-fmv was re-running on live rows (and
appending comps) during this measurement.

    uv run --python 3.12 docs/audit/2026-09-30-sold-comp-ceiling.py [--json out.json]
"""

from __future__ import annotations

import importlib.util
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "gixen-overlay" / "src"))
_spec = importlib.util.spec_from_file_location(
    "bt1005", ROOT / "docs" / "audit" / "2026-09-28-grade-adjusted-pool-backtest.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)  # also puts apps/fmv/src on sys.path
fmv_math = H.fmv_math

AS_OF = "2026-09-30T01:00"
HAIRCUT = fmv_math.INTERPOLATED_BID_FACTOR  # 0.60, the LOW rung
ARMS = (("A_min", HAIRCUT), ("B_rung", HAIRCUT), ("A_min@0.80", fmv_math.BASE_BID_FACTOR),
        ("A_min@1.00", 1.0), ("B_rung@1.00", 1.0))
PRICE_CUTS = (("< $20", 0, 20), ("$20-$100", 20, 100), (">= $100", 100, float("inf")))
SEED = 1025
REPS = 1000

# ─── Ship thresholds, declared before scoring ──────────────────────────────
# T1 coverage: the arm caps at least this share of the refused sales.
T1_MIN_CAPPED_SHARE = 0.20
# T2 overpay rate: arm minus accepted-path rate, upper bound of the 95%
#   book-clustered CI, at most this many points.
T2_MAX_OVERPAY_GAP_UB = 0.02
# T3 overpay depth: arm's overpay P90 (among overpaid sales) no worse than the
#   accepted path's overpay P90.
# T4 cells: in every era and price cell with >= 30 capped sales, the arm's
#   overpay rate is at most accepted-path's rate in that cell + this.
T4_CELL_SLACK = 0.05
T4_MIN_CELL = 30
# T5 usefulness: the cap reaches at least this share of the realized price at
#   the median (median cap / price), or the cap wins almost nothing.
T5_MIN_MEDIAN_CAP_RATIO = 0.50


def price_level(p: float) -> str:
    for name, lo, hi in PRICE_CUTS:
        if lo <= p < hi:
            return name
    raise ValueError(p)


def ceiling_pool(train: list[dict], target: dict, flag: str) -> tuple[list[dict], str]:
    g, y, d = target["grade"], target["price"], target["d"]
    clean = [c for c in train
             if not (abs(c["price"] - y) < 0.01 and abs((d - c["d"]).days) <= 7)]
    pool, _ = fmv_math.build_pool([H.as_comp(c) for c in clean], g)
    above = [c for c in pool if c["grade"] >= g]
    if flag == "one_sided":
        if any(c["grade"] < g for c in pool):
            return [], "one_sided_below"
        above = [c for c in pool if c["grade"] > g]
    if len(above) < fmv_math.MIN_PRICEABLE_POOL:
        return [], ("one_comp_above" if len(above) == 1 else "none_above")
    return above, "capped"


def ceilings(pool: list[dict]) -> dict[str, float]:
    lowest = min(c["grade"] for c in pool)
    rung = [c["price"] for c in pool if c["grade"] == lowest]
    return {"A": min(c["price"] for c in pool), "B": median(rung)}


def cap_for(ceil: float, factor: float) -> float:
    return ceil if factor == 1.0 else fmv_math.clean_round(ceil * factor)


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def score(rows: list[dict], key: str) -> dict:
    """rows carry r[key] = cap (or None) and r['price']. A cap that clean_round
    takes to $0 counts as capped: a $0 cap loses the auction (room lost)."""
    capped = [r for r in rows if r[key] is not None]
    over = [(r[key] - r["price"]) / r["price"] for r in capped if r[key] > r["price"]]
    lost = [(r["price"] - r[key]) / r["price"] for r in capped if r[key] < r["price"]]
    ratio = [r[key] / r["price"] for r in capped]
    k = len(capped)
    return {"n": len(rows), "capped": k, "capped_share": k / len(rows) if rows else 0,
            "overpay_rate": len(over) / k if k else None,
            "overpay_p50": pct(over, 0.5), "overpay_p90": pct(over, 0.9),
            "lost_rate": len(lost) / k if k else None,
            "lost_p50": pct(lost, 0.5), "lost_p90": pct(lost, 0.9),
            "cap_ratio_median": median(ratio) if ratio else None,
            "zero_caps": sum(1 for r in capped if r[key] == 0),
            "over_usd_p50": pct([r[key] - r["price"] for r in capped if r[key] > r["price"]], 0.5),
            "over_usd_p90": pct([r[key] - r["price"] for r in capped if r[key] > r["price"]], 0.9),
            # expected overpay per capped sale: rate x depth in one number
            "over_mean_uncond": (sum(max(0.0, (r[key] - r["price"]) / r["price"]) for r in capped) / k
                                 if k else None)}


def boot_rate_gap(a: list[dict], ka: str, b: list[dict], kb: str) -> tuple:
    """Overpay-rate gap (a minus b), 95% CI resampling BOOKS (seeded)."""
    def pts(rows, k):
        g = defaultdict(list)
        for r in rows:
            if r[k] is not None:
                g[r["comic_id"]].append(1.0 if r[k] > r["price"] else 0.0)
        return g
    ga, gb = pts(a, ka), pts(b, kb)
    books = sorted(set(ga) | set(gb))
    rng = random.Random(SEED)

    def rate(g, pick):
        xs = [v for k in pick for v in g.get(k, [])]
        return sum(xs) / len(xs) if xs else None
    point = rate(ga, books) - rate(gb, books)
    gaps = []
    for _ in range(REPS):
        pick = [rng.choice(books) for _ in books]
        ra, rb = rate(ga, pick), rate(gb, pick)
        if ra is not None and rb is not None:
            gaps.append(ra - rb)
    gaps.sort()
    return point, gaps[int(0.025 * len(gaps))], gaps[int(0.975 * len(gaps)) - 1]


def f(x, p=1, pc=True):
    if x is None:
        return "-"
    return f"{100 * x:.{p}f}%" if pc else f"{x:.{p}f}"


def table(title: str, cuts: dict[str, list[dict]], keys: list[tuple[str, str]]) -> dict:
    print(f"\n## {title}\n")
    print("| Cut | Arm | Sales | Capped | Overpay rate | Overpay P50 | Overpay P90 "
          "| Room lost rate | Lost P50 | Lost P90 | Median cap/price |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    out = {}
    for cut, rows in cuts.items():
        for key, label in keys:
            s = score(rows, key)
            out[f"{cut}|{label}"] = s
            print(f"| {cut} | {label} | {s['n']} | {s['capped']} ({f(s['capped_share'], 0)}) | "
                  f"{f(s['overpay_rate'])} | {f(s['overpay_p50'], 0)} | {f(s['overpay_p90'], 0)} | "
                  f"{f(s['lost_rate'])} | {f(s['lost_p50'], 0)} | {f(s['lost_p90'], 0)} | "
                  f"{f(s['cap_ratio_median'], 2, False)} |")
    return out


def main() -> int:
    conn = sqlite3.connect(f"file:{H.DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    comps, stats = H.load_comps(conn)
    conn.close()
    comps = [c for c in comps if (c["first_seen_at"] or "") < AS_OF]
    stats["kept_as_of"] = len(comps)
    print(f"AS_OF={AS_OF}; haircut={HAIRCUT}; graded raw comps: {stats}")

    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    rows, reasons = [], Counter()
    for c in comps:
        train, _ = H.training(by_book[c["comic_id"]], c)
        if not any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train):
            continue
        full = fmv_math.compute_fmv([H.as_comp(t) for t in train], c["grade"])
        cur = H.price_current(train, c["grade"])
        r = {"comic_id": c["comic_id"], "grade": c["grade"], "price": c["price"],
             "era": H.era(c["year"]), "level": price_level(median(t["price"] for t in train)),
             "flag": cur["flag"], "accepted": full["max_bid"] if cur["flag"] is None else None}
        for name, _ in ARMS:
            r[name] = None
        if cur["flag"] in H.SHAPE_REFUSALS:
            pool, why = ceiling_pool(train, c, cur["flag"])
            reasons[(cur["flag"], why)] += 1
            if pool:
                ce = ceilings(pool)
                for name, fac in ARMS:
                    r[name] = cap_for(ce[name[0]], fac)
                r["pool_n"] = len(pool)
                r["ceil_A"], r["ceil_B"] = ce["A"], ce["B"]
        rows.append(r)

    flags = Counter(r["flag"] or "priced" for r in rows)
    refused = [r for r in rows if r["flag"] in H.SHAPE_REFUSALS]
    accepted = [r for r in rows if r["flag"] is None and r["accepted"] is not None]
    print(f"\nheld-out sales N={len(rows)}; outcome {dict(flags)}")
    print(f"refused (one_sided/too_wide) = {len(refused)}; ceiling-pool outcome: "
          f"{ {f'{a}/{b}': n for (a, b), n in sorted(reasons.items())} }")

    arm_keys = [(n, n) for n, _ in ARMS]
    acc_key = [("accepted", "accepted path max_bid")]
    res = {"stats": stats, "flags": dict(flags),
           "reasons": {f"{a}/{b}": n for (a, b), n in reasons.items()}}
    res["overall"] = table("Overall", {"refused": refused}, arm_keys)
    res["overall"].update(table("Yardstick", {"accepted": accepted}, acc_key))
    res["by_flag"] = table("Refused by flag", {
        fl: [r for r in refused if r["flag"] == fl] for fl in H.SHAPE_REFUSALS},
        arm_keys[:2] + arm_keys[3:4])
    era_cuts, lvl_cuts = {}, {}
    for e in ("pre-1980", "1980-1999", "2000+"):
        era_cuts[f"{e} refused"] = [r for r in refused if r["era"] == e]
        era_cuts[f"{e} accepted"] = [r for r in accepted if r["era"] == e]
    for name, _, _ in PRICE_CUTS:
        lvl_cuts[f"{name} refused"] = [r for r in refused if r["level"] == name]
        lvl_cuts[f"{name} accepted"] = [r for r in accepted if r["level"] == name]
    keys_cell = arm_keys[:2] + acc_key
    res["by_era"] = table("By era (arms on refused rows, accepted-path on accepted rows)",
                          era_cuts, keys_cell)
    res["by_level"] = table("By training-pool median price", lvl_cuts, keys_cell)

    # Money check: clean_round rounds to the nearest $5 below $50, so a small
    # ceiling can round UP past the ceiling itself (0.60 x $5 -> $5).
    for arm in ("A_min", "B_rung"):
        ck = "ceil_" + arm[0]
        over_ceil = [r for r in refused if r[arm] is not None and r[arm] > r[ck]]
        print(f"{arm}: caps above their own ceiling after clean_round: {len(over_ceil)}; "
              f"of those overpaying the sale: {sum(r[arm] > r['price'] for r in over_ceil)}")
        res[f"{arm}_cap_above_ceiling"] = len(over_ceil)

    # ─── Decision ──────────────────────────────────────────────────────────
    acc = score(accepted, "accepted")
    print("\n## Gates\n")
    res["gates"] = {}
    for arm in ("A_min", "B_rung"):
        s = score(refused, arm)
        gap = boot_rate_gap(refused, arm, accepted, "accepted")
        cells = []
        for cuts in (era_cuts, lvl_cuts):
            names = sorted({k.rsplit(" ", 1)[0] for k in cuts})
            for nm in names:
                sa = score(cuts[f"{nm} refused"], arm)
                sb = score(cuts[f"{nm} accepted"], "accepted")
                if sa["capped"] >= T4_MIN_CELL and sb["overpay_rate"] is not None:
                    cells.append((nm, sa["overpay_rate"], sb["overpay_rate"],
                                  sa["overpay_rate"] <= sb["overpay_rate"] + T4_CELL_SLACK))
        g = {"T1 coverage": s["capped_share"] >= T1_MIN_CAPPED_SHARE,
             "T2 overpay-rate gap UB": gap[2] <= T2_MAX_OVERPAY_GAP_UB,
             "T3 overpay P90": (s["overpay_p90"] or 0) <= (acc["overpay_p90"] or 0),
             "T4 cells": all(c[3] for c in cells),
             "T5 median cap/price": (s["cap_ratio_median"] or 0) >= T5_MIN_MEDIAN_CAP_RATIO}
        print(f"{arm}: capped {s['capped']}/{s['n']} ({f(s['capped_share'])}); overpay "
              f"{f(s['overpay_rate'])} vs accepted {f(acc['overpay_rate'])}, gap "
              f"{gap[0]:+.3f} [{gap[1]:+.3f}, {gap[2]:+.3f}]; P90 {f(s['overpay_p90'], 0)} vs "
              f"{f(acc['overpay_p90'], 0)}; median cap/price {f(s['cap_ratio_median'], 2, False)}")
        for nm, a, b, ok in cells:
            print(f"   cell {nm}: {f(a)} vs {f(b)} {'ok' if ok else 'FAIL'}")
        print(f"   post-hoc, not gated: $0 caps {s['zero_caps']}; overpay $ P50/P90 "
              f"{s['over_usd_p50']}/{s['over_usd_p90']} vs accepted {acc['over_usd_p50']}/"
              f"{acc['over_usd_p90']}; mean overpay per capped sale "
              f"{f(s['over_mean_uncond'])} vs {f(acc['over_mean_uncond'])}")
        print(f"   gates: {g} -> {'SHIP' if all(g.values()) else 'CANCEL'}")
        res["gates"][arm] = {"gap": gap, "cells": cells, "gates": g}
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as fh:
            json.dump(res, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
