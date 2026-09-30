"""Out-of-sample re-test of the sold-comp ceiling cap, with exposure gates (BUI-1028).

DIAGNOSTIC ONLY. Opens ~/.comics-server/db.sqlite with mode=ro; nothing is
written and no provider is called. Gates are pre-registered in
docs/audit/2026-10-01-sold-comp-ceiling-oos.md (committed before this script).

Reuses BUI-1025's script unchanged for the ceiling pool (`ceiling_pool`,
`ceilings`) and BUI-1005's harness (via that script) for the population,
training, and the accepted path. Changes from BUI-1025: variant B only, at
0.60 / 0.80 / 1.00, every cap FLOORED to the clean step; gates on
unconditional dollar exposure.

    uv run --python 3.12 docs/audit/2026-10-01-sold-comp-ceiling-oos.py [--json out.json]
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "c1025", ROOT / "docs" / "audit" / "2026-09-30-sold-comp-ceiling.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
H, fmv_math = C.H, C.fmv_math

CUTOFF = C.AS_OF  # "2026-09-30T01:00"
ARMS = (("B@0.60", 0.60), ("B@0.80", 0.80), ("B@1.00", 1.00))
SEED, REPS = 1028, 1000
MIN_OOS_CAPPED, MIN_OOS_BOOKS, MIN_OOS_ACCEPTED = 100, 30, 100
E1_UB_REL_MARGIN = 0.10
T1_MIN_CAPPED_SHARE, T5_MIN_MEDIAN_CAP_RATIO = 0.20, 0.50


def floor_clean(v: float) -> int:
    step = fmv_math._clean_step(v)
    return int(math.floor(v / step) * step)


def over_usd(r: dict, key: str) -> float:
    return max(0.0, r[key] - r["price"])


def exposure(rows: list[dict], key: str) -> dict:
    capped = [r for r in rows if r[key] is not None]
    k = len(capped)
    ov = [over_usd(r, key) for r in capped]
    return {"capped": k, "books": len({r["comic_id"] for r in capped}),
            "mean_over_usd": sum(ov) / k if k else None,
            "p90_over_usd": C.pct(ov, 0.9),
            "overpay_rate": sum(o > 0 for o in ov) / k if k else None,
            "lost_rate": sum(r[key] < r["price"] for r in capped) / k if k else None,
            "cond_over_p90": C.pct([(r[key] - r["price"]) / r["price"]
                                    for r in capped if r[key] > r["price"]], 0.9),
            "cap_ratio_median": median(r[key] / r["price"] for r in capped) if k else None,
            "zero_caps": sum(1 for r in capped if r[key] == 0)}


def boot_mean_gap(a: list[dict], ka: str, b: list[dict], kb: str) -> tuple:
    """Mean-overpay-$ gap (a - b), 95% CI resampling books (seeded)."""
    def grp(rows, k):
        g = defaultdict(list)
        for r in rows:
            if r[k] is not None:
                g[r["comic_id"]].append(over_usd(r, k))
        return g
    ga, gb = grp(a, ka), grp(b, kb)
    books = sorted(set(ga) | set(gb))
    rng = random.Random(SEED)

    def mean(g, pick):
        xs = [v for bk in pick for v in g.get(bk, [])]
        return sum(xs) / len(xs) if xs else None
    point = mean(ga, books) - mean(gb, books)
    gaps = []
    for _ in range(REPS):
        pick = [rng.choice(books) for _ in books]
        ma, mb = mean(ga, pick), mean(gb, pick)
        if ma is not None and mb is not None:
            gaps.append(ma - mb)
    gaps.sort()
    return point, gaps[int(0.025 * len(gaps))], gaps[int(0.975 * len(gaps)) - 1]


def build_rows(comps: list[dict], targets: list[dict]) -> tuple[list[dict], Counter]:
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    rows, reasons = [], Counter()
    for c in targets:
        train, _ = H.training(by_book[c["comic_id"]], c)
        if not any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train):
            continue
        full = fmv_math.compute_fmv([H.as_comp(t) for t in train], c["grade"])
        cur = H.price_current(train, c["grade"])
        r = {"comic_id": c["comic_id"], "grade": c["grade"], "price": c["price"],
             "sold_date": c["sold_date"], "first_seen_at": c["first_seen_at"],
             "flag": cur["flag"], "accepted": full["max_bid"] if cur["flag"] is None else None,
             "max_train_first_seen": max((t["first_seen_at"] or "") for t in train)}
        for name, _ in ARMS:
            r[name] = None
        if cur["flag"] in H.SHAPE_REFUSALS:
            pool, why = C.ceiling_pool(train, c, cur["flag"])
            reasons[(cur["flag"], why)] += 1
            if pool:
                ceil_b = C.ceilings(pool)["B"]
                r["ceil_B"] = ceil_b
                for name, fac in ARMS:
                    r[name] = floor_clean(fac * ceil_b)
                    assert r[name] <= ceil_b + 1e-9, (r, name)  # the round-down money trap
        rows.append(r)
    return rows, reasons


def half(label: str, rows: list[dict], reasons: Counter) -> dict:
    refused = [r for r in rows if r["flag"] in H.SHAPE_REFUSALS]
    accepted = [r for r in rows if r["flag"] is None and r["accepted"] is not None]
    acc = exposure(accepted, "accepted")
    out = {"n_sales": len(rows), "flags": dict(Counter(r["flag"] or "priced" for r in rows)),
           "refused": len(refused), "reasons": {f"{a}/{b}": n for (a, b), n in reasons.items()},
           "accepted": acc, "arms": {}}
    print(f"\n## {label}: sales {len(rows)}, outcome {out['flags']}, refused {len(refused)}")
    print(f"ceiling-pool outcome: {out['reasons']}")
    print("\n| Arm | Capped | Books | Mean overpay $ / capped sale | Overpay $ P90 (all capped) "
          "| Overpay rate | Room lost rate | Cond. overpay P90 | Median cap/price | $0 caps |")
    print("|---|---|---|---|---|---|---|---|---|---|")

    def line(nm, s):
        print(f"| {nm} | {s['capped']} | {s['books']} | {C.f(s['mean_over_usd'], 2, False)} | "
              f"{C.f(s['p90_over_usd'], 2, False)} | {C.f(s['overpay_rate'])} | "
              f"{C.f(s['lost_rate'])} | {C.f(s['cond_over_p90'], 0)} | "
              f"{C.f(s['cap_ratio_median'], 2, False)} | {s['zero_caps']} |")
    for name, _ in ARMS:
        s = exposure(refused, name)
        s["capped_share"] = s["capped"] / len(refused) if refused else 0
        if s["capped"] and acc["capped"]:
            gap = boot_mean_gap(refused, name, accepted, "accepted")
            s["e1_gap"] = gap
            s["gates"] = {
                "E1 mean $": (s["mean_over_usd"] <= acc["mean_over_usd"]
                              and gap[2] <= E1_UB_REL_MARGIN * acc["mean_over_usd"]),
                "E2 P90 $": s["p90_over_usd"] <= acc["p90_over_usd"],
                "T1 coverage": s["capped_share"] >= T1_MIN_CAPPED_SHARE,
                "T5 cap/price": s["cap_ratio_median"] >= T5_MIN_MEDIAN_CAP_RATIO}
        out["arms"][name] = s
        line(name, s)
    line("Accepted path", acc)
    print()
    for name, s in out["arms"].items():
        if "gates" in s:
            g = s["e1_gap"]
            print(f"{name}: E1 gap {g[0]:+.2f} [{g[1]:+.2f}, {g[2]:+.2f}] "
                  f"(UB limit {E1_UB_REL_MARGIN * acc['mean_over_usd']:+.2f}); "
                  f"coverage {C.f(s['capped_share'])}; gates {s['gates']} -> "
                  f"{'PASS' if all(s['gates'].values()) else 'FAIL'}")
    return out


def main() -> int:
    run_as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    conn = sqlite3.connect(f"file:{H.DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    comps, stats = H.load_comps(conn)
    conn.close()
    comps = [c for c in comps if (c["first_seen_at"] or "") < run_as_of]
    print(f"CUTOFF={CUTOFF}; RUN_AS_OF={run_as_of}; graded raw comps: {stats}")
    res: dict = {"cutoff": CUTOFF, "run_as_of": run_as_of, "stats": stats}

    # In-sample: BUI-1025's pinned population.
    pre = [c for c in comps if (c["first_seen_at"] or "") < CUTOFF]
    rows_in, reasons_in = build_rows(pre, pre)
    res["in_sample"] = half("In-sample (first seen < cutoff)", rows_in, reasons_in)

    # OOS: targets first seen at/after the cutoff; drop provider copies of pre-cutoff sales.
    pre_idx: dict[int, list[dict]] = defaultdict(list)
    for c in pre:
        pre_idx[c["comic_id"]].append(c)
    post = [c for c in comps if (c["first_seen_at"] or "") >= CUTOFF]
    copies = [c for c in post if any(abs(p["price"] - c["price"]) < 0.01
                                     and abs((p["d"] - c["d"]).days) <= 7
                                     for p in pre_idx[c["comic_id"]])]
    copy_ids = {c["id"] for c in copies}
    targets = [c for c in post if c["id"] not in copy_ids]
    rows_oos, reasons_oos = build_rows(comps, targets)
    assert all((r["first_seen_at"] or "") >= CUTOFF for r in rows_oos)
    days = Counter(r["first_seen_at"][:10] for r in rows_oos)
    sold = sorted(r["sold_date"] for r in rows_oos)
    print(f"\nOOS: post-cutoff comps {len(post)}, dropped as copies of pre-cutoff sales "
          f"{len(copies)}, held-out OOS sales {len(rows_oos)}; first-seen days {dict(days)}; "
          f"sold-date range {sold[0] if sold else '-'}..{sold[-1] if sold else '-'}")
    # Minimum-sample check BEFORE any OOS scoring, per the pre-registered gate.
    refused = [r for r in rows_oos if r["flag"] in H.SHAPE_REFUSALS]
    capped = [r for r in refused if r["B@0.60"] is not None]  # every arm shares the capped set
    n_acc = sum(1 for r in rows_oos if r["flag"] is None and r["accepted"] is not None)
    n_books = len({r["comic_id"] for r in capped})
    met = len(capped) >= MIN_OOS_CAPPED and n_books >= MIN_OOS_BOOKS and n_acc >= MIN_OOS_ACCEPTED
    res["oos_counts"] = {"post_comps": len(post), "copies_dropped": len(copies),
                         "sales": len(rows_oos), "refused": len(refused), "capped": len(capped),
                         "books": n_books, "accepted": n_acc, "first_seen_days": dict(days),
                         "flags": dict(Counter(r["flag"] or "priced" for r in rows_oos)),
                         "reasons": {f"{a}/{b}": n for (a, b), n in reasons_oos.items()},
                         "met": met}
    print(f"OOS counts: {res['oos_counts']}")
    print(f"OOS minimum: capped {len(capped)}/{MIN_OOS_CAPPED}, books {n_books}/"
          f"{MIN_OOS_BOOKS}, accepted {n_acc}/{MIN_OOS_ACCEPTED} -> "
          f"{'MET' if met else 'NOT MET: OOS not scored, INCONCLUSIVE'}")
    if met:
        oos = half("Out-of-sample (first seen >= cutoff)", rows_oos, reasons_oos)
        res["oos"] = oos

    if met:
        passing = [nm for nm, _ in reversed(ARMS)
                   if all(res["in_sample"]["arms"][nm].get("gates", {"x": False}).values())
                   and all(oos["arms"][nm].get("gates", {"x": False}).values())]
        res["verdict"] = f"SHIP {passing[0]}" if passing else "CANCEL"
    else:
        res["verdict"] = "INCONCLUSIVE"
    print(f"verdict: {res['verdict']}")
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as fh:
            json.dump(res, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
