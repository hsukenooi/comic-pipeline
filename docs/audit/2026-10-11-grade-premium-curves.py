#!/usr/bin/env python3
"""BUI-1232: retest pooled grade-premium curves for thin slab rungs.
DIAGNOSTIC ONLY, read-only. No production code is touched.

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-11-grade-premium-curves.py \\
        [--json out.json]

Question. The CGC Slab Support depth brainstorm (2026-09-21) measured ladder
curve fitting, neighbour-rung scaling and one-rung extrapolation NO-GO on the
thin comps ledger (per-book curves; no pool fit R^2 0.9, 11/12 ladders
non-monotone; neighbour scaling >15% off the observed rung in 59% of cases;
BUI-974: one-step carry median miss 18% down, 44-49% up). Does a curve POOLED
across books by era x price tier, fitted on PriceCharting's deep history,
price thin rungs better than the live ladder tier?

Method (PRE-REGISTERED before the first run; see the .md for the verdict).
  Curve. log price = book-quarter level + f_cell(grade). Observations are the
    median log price of one (book, calendar quarter, grade); weight min(n, 3).
    Fixed effects by alternating least squares, then weighted isotonic
    regression so f never falls with grade. Grades seen in fewer than two
    books are dropped; between kept grades f is linear; outside them, none.
  Leakage. A sale in quarter Q is priced by a curve fitted only on PC
    universal sales from the 12 complete quarters before Q (strictly before
    the sale). Two variants: LOBO (the scored book is excluded from its own
    pooled curve, primary) and OWN (its prior quarters are included).
  Cells. Era (pc_slab_test_set.era) x price tier, tier = the book's median PC
    universal sale over the 4 quarters before Q: low < $200 <= mid < $700 <=
    high. A cell needs 3+ training books covering both grades, else the
    curve falls back to era only, then to all books.
  Pricing. A thin rung is a sale whose current graded_fmv does NOT price
    `direct` (ladder, lone_sale, or a refusal). The anchor is the nearest
    DENSE rung (effective n >= 2 under graded_pool's calendar aging, the
    exact tier's own gate; ties go to the larger effective n) in the same
    pool graded_fmv saw. Point = rung weighted median x exp(f(g) - f(d)).
    Band = rung weighted IQR x the same ratio (a secondary variant).
  Populations. (1) LEDGER, the production-shaped test: PC universal sales in
    2026-08-01..10-10, anchors and the current price from ledger comps
    exactly as the BUI-1229 baseline builds them (twin hidden by same_sale).
    (2) PC-HISTORY, the deep test: PC universal sales 2024-01-01..2026-10-10,
    anchors and the current price from the book's earlier PC sales only.
    Truth is PC SOLD price (accepted offer) on non-outlier rows; the ledger
    holds LIST price on best-offer sales (BUI-1222), list basis printed too.
  Score. Winkler (alpha 0.5) / sale price via the BUI-1229 scorer. A point
    band's Winkler is 4 x |error| / price. Also median |error| and the share
    of sales >15% off (the earlier NO-GO's metric, but per sale, not per
    rung median). CIs: the BUI-1229 book-clustered bootstrap (2000 reps,
    seed 1229); paired differences bootstrap the same way.
  GO iff, on population (1), LOBO: (a) on sales both price, the curve's mean
    Winkler minus the ladder tier's has a CI wholly below 0, AND (b) on
    rescued refusals the curve's Winkler is no worse than the ladder tier's
    own Winkler point estimate. (2) must agree in sign. Otherwise NO-GO.
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pc_slab_test_set as T  # noqa: E402
from pc_slab_test_set import fmv_math  # noqa: E402

_spec = importlib.util.spec_from_file_location("bui1229", HERE / "2026-10-11-pc-slab-test-set.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)  # reuse: price(), score(), summarize(), REPS, SEED

WIN_FROM, WIN_TO = B.WIN_FROM, B.WIN_TO
HIST_FROM = "2024-01-01"
TRAIN_QUARTERS = 12
TIER_EDGES = (200.0, 700.0)
MIN_CELL_BOOKS = 3
MIN_GRADE_BOOKS = 2
LEVELS = ("era_tier", "era", "all")


# ---------------------------------------------------------------- quarters
def qkey(d: date) -> int:
    return d.year * 4 + (d.month - 1) // 3


def tier(med: float | None) -> str | None:
    if med is None:
        return None
    return "low" if med < TIER_EDGES[0] else "mid" if med < TIER_EDGES[1] else "high"


# ---------------------------------------------------------------- curve fit
def isotonic(xs: list[float], ys: list[float], ws: list[float]) -> list[float]:
    """Weighted pool-adjacent-violators, non-decreasing in x (xs sorted)."""
    blocks = [[y, w, 1] for y, w in zip(ys, ws)]
    out: list[list[float]] = []
    for b in blocks:
        out.append(b)
        while len(out) > 1 and out[-2][0] > out[-1][0]:
            y2, w2, n2 = out.pop()
            y1, w1, n1 = out.pop()
            out.append([(y1 * w1 + y2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    res: list[float] = []
    for y, _, n in out:
        res.extend([y] * n)
    return res


def fit_curve(obs: list[tuple]) -> dict | None:
    """obs: (book, quarter, grade, log_median, weight). Returns
    {"f": {grade: f}, "raw_monotone": bool, "books": n} or None."""
    books_at = defaultdict(set)
    for b, _, g, _, _ in obs:
        books_at[g].add(b)
    keep = {g for g, s in books_at.items() if len(s) >= MIN_GRADE_BOOKS}
    obs = [o for o in obs if o[2] in keep]
    if len(keep) < 2:
        return None
    f = {g: 0.0 for g in keep}
    units = defaultdict(list)
    for o in obs:
        units[(o[0], o[1])].append(o)
    alpha = {}
    for _ in range(40):
        for u, os_ in units.items():
            alpha[u] = (sum(w * (y - f[g]) for _, _, g, y, w in os_) / sum(o[4] for o in os_))
        num, den = defaultdict(float), defaultdict(float)
        for b, q, g, y, w in obs:
            num[g] += w * (y - alpha[(b, q)])
            den[g] += w
        f = {g: num[g] / den[g] for g in keep}
    gs = sorted(keep)
    raw = [f[g] for g in gs]
    iso = isotonic(gs, raw, [den[g] for g in gs])
    return {"f": dict(zip(gs, iso)), "raw_monotone": all(a <= b for a, b in zip(raw, raw[1:])),
            "books": len({o[0] for o in obs})}


def f_at(curve: dict, g: float) -> float | None:
    f = curve["f"]
    if g in f:
        return f[g]
    gs = sorted(f)
    if g < gs[0] or g > gs[-1]:
        return None
    for lo, hi in zip(gs, gs[1:]):
        if lo < g < hi:
            return f[lo] + (f[hi] - f[lo]) * (g - lo) / (hi - lo)
    return None


class Curves:
    """Lazily fitted, cached pooled curves keyed by (quarter, level, cell, excluded book)."""

    def __init__(self, ts: dict, books: dict):
        self.books = books
        self.obs_by_book = defaultdict(lambda: defaultdict(list))   # book -> q -> [(g, logp)]
        self.price_by_book = defaultdict(lambda: defaultdict(list))
        for s in T.universal_sales(ts):
            d = date.fromisoformat(s["sold_date"])
            self.obs_by_book[s["comic_id"]][qkey(d)].append((float(s["grade"]), math.log(s["price"])))
            self.price_by_book[s["comic_id"]][qkey(d)].append(s["price"])
        self.cache: dict = {}
        self.fits = {"n": 0, "raw_monotone": 0}

    def tier_at(self, cid: int, q: int) -> str | None:
        p = [x for qq in range(q - 4, q) for x in self.price_by_book[cid].get(qq, [])]
        return tier(median(p)) if len(p) >= 5 else None

    def cell_books(self, q: int, level: str, era: str, tr: str | None, exclude: int | None) -> list[int]:
        out = []
        for cid, b in self.books.items():
            if cid == exclude:
                continue
            if level in ("era_tier", "era") and b["era"] != era:
                continue
            if level == "era_tier" and (tr is None or self.tier_at(cid, q) != tr):
                continue
            out.append(cid)
        return out

    def get(self, q: int, level: str, era: str, tr: str | None, exclude: int | None) -> dict | None:
        key = (q, level, era if level != "all" else None, tr if level == "era_tier" else None, exclude)
        if key in self.cache:
            return self.cache[key]
        cids = self.cell_books(q, level, era, tr, exclude)
        obs = []
        for cid in cids:
            for qq in range(q - TRAIN_QUARTERS, q):  # complete quarters strictly before q
                byg = defaultdict(list)
                for g, lp in self.obs_by_book[cid].get(qq, []):
                    byg[g].append(lp)
                for g, lps in byg.items():
                    obs.append((cid, qq, g, median(lps), float(min(len(lps), 3))))
        cur = fit_curve(obs) if len({o[0] for o in obs}) >= MIN_CELL_BOOKS else None
        if cur:
            self.fits["n"] += 1
            self.fits["raw_monotone"] += cur["raw_monotone"]
        self.cache[key] = cur
        return cur

    def ratio(self, q: int, era: str, tr: str | None, exclude: int | None,
              g: float, d: float) -> tuple[float, str] | None:
        """exp(f(g) - f(d)) from the most specific cell that covers both grades."""
        for level in LEVELS:
            if level == "era_tier" and tr is None:
                continue
            c = self.get(q, level, era, tr, exclude)
            if not c:
                continue
            fg, fd = f_at(c, g), f_at(c, d)
            if fg is not None and fd is not None:
                return math.exp(fg - fd), level
        return None


# ---------------------------------------------------------------- pricing
def nearest_dense(comps: list[dict], g: float, as_of: date) -> dict | None:
    pool, _, _ = fmv_math.graded_pool(comps, as_of=as_of)
    eff = fmv_math.bucket_effective_n(pool)
    dense = [x for x, n in eff.items() if n >= fmv_math.GRADED_EXACT_MIN_EFFECTIVE_N and x != g]
    if not dense:
        return None
    d = min(dense, key=lambda x: (abs(x - g), -eff[x]))
    at = [c for c in pool if float(c["grade"]) == d]
    ps, ws = [float(c["price"]) for c in at], [float(c["weight"]) for c in at]
    return {"grade": d, "eff_n": eff[d], "median": fmv_math.weighted_median(ps, ws),
            "q1": fmv_math.weighted_quartile(ps, ws, 0.25), "q3": fmv_math.weighted_quartile(ps, ws, 0.75)}


def point(p: float) -> dict:
    return {"fmv_low": p, "fmv_high": p}


def abserr(res: dict | None, y: float) -> float | None:
    if not res or res.get("fmv_low") is None:
        return None
    return abs((res["fmv_low"] + res["fmv_high"]) / 2 - y) / y


def price_thin(curves: Curves, cid: int, era: str, s: dict, comps: list[dict], d0: date) -> dict:
    """Score one sale: current graded_fmv vs pooled-curve pricing (LOBO and OWN)."""
    y, ylist, g = s["price"], s["ask"] or s["price"], float(s["grade"])
    as_of = d0 - timedelta(days=1)
    cur = B.price(comps, g, as_of, s["page_quality"])
    basis = cur.get("pricing_basis") if cur.get("flag_reason") is None else None
    row = {"comic_id": cid, "era": era, "date": s["sold_date"], "grade": g, "y": y,
           "source": s["source"], "basis": basis, "reason": cur.get("flag_reason"),
           "thin": basis != "direct"}
    row["cur"], row["cur_list"] = B.score(cur, y), B.score(cur, ylist)
    row["cur_err"] = abserr(cur if basis else None, y)
    if not row["thin"]:
        return row
    q = qkey(d0)
    tr = curves.tier_at(cid, q)
    row["tier"] = tr
    anc = nearest_dense(comps, g, as_of)
    row["dist"] = abs(anc["grade"] - g) if anc else None
    for tag, excl in (("lobo", cid), ("own", None)):
        r = curves.ratio(q, era, tr, excl, g, anc["grade"]) if anc else None
        if not r:
            row[tag] = row[tag + "_list"] = row[tag + "_band"] = row[tag + "_err"] = None
            continue
        ratio, level = r
        pt = point(anc["median"] * ratio)
        band = {"fmv_low": anc["q1"] * ratio, "fmv_high": anc["q3"] * ratio}
        row[tag], row[tag + "_list"] = B.score(pt, y), B.score(pt, ylist)
        row[tag + "_band"], row[tag + "_err"] = B.score(band, y), abserr(pt, y)
        row[tag + "_level"] = level
    return row


# ---------------------------------------------------------------- reporting
def boot(rows: list[dict], stat) -> tuple[float, float, float] | None:
    """Point estimate + BUI-1229-style book-clustered percentile CI for any statistic."""
    if not rows:
        return None
    by = defaultdict(list)
    for r in rows:
        by[r["comic_id"]].append(r)
    books = list(by)
    rng = random.Random(B.SEED)
    reps = []
    for _ in range(B.REPS):
        pick = [r for _ in books for r in by[rng.choice(books)]]
        reps.append(stat(pick))
    reps.sort()
    return stat(rows), reps[int(0.025 * B.REPS)], reps[int(0.975 * B.REPS) - 1]


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def f3(t, pct=False):
    if t is None:
        return "n/a"
    if pct:
        return f"{t[0]:.0%} [{t[1]:.0%}, {t[2]:.0%}]"
    return f"{t[0]:.3f} [{t[1]:.3f}, {t[2]:.3f}]"


def method_line(rows: list[dict], key: str, errkey: str) -> str:
    sc = [r for r in rows if r.get(key)]
    if not sc:
        return f"n={len(rows)} priced=0"
    w = boot(sc, lambda rs: mean(r[key]["winkler"] for r in rs))
    mw = boot(sc, lambda rs: median(r[key]["winkler"] for r in rs))
    me = boot(sc, lambda rs: median(r[errkey] for r in rs))
    off = boot(sc, lambda rs: mean(r[errkey] > 0.15 for r in rs))
    bias = mean(r[key]["bias"] for r in sc)
    return (f"priced {len(sc)}/{len(rows)} ({len(sc) / len(rows):.0%}) books={len({r['comic_id'] for r in sc})}"
            f" | Winkler {f3(w)} | median Winkler {f3(mw)} | median|err| {f3(me, True)} | >15% off {f3(off, True)} | bias {bias:+.3f}")


def paired_line(rows: list[dict], a: str, b: str) -> str:
    pr = [r for r in rows if r.get(a) and r.get(b)]
    if not pr:
        return "paired n=0"
    d = boot(pr, lambda rs: mean(r[a]["winkler"] - r[b]["winkler"] for r in rs))
    wa = mean(r[a]["winkler"] for r in pr)
    wb = mean(r[b]["winkler"] for r in pr)
    return (f"paired n={len(pr)} books={len({r['comic_id'] for r in pr})} | {a} {wa:.3f} vs {b} {wb:.3f}"
            f" | diff {f3(d)}")


def report(name: str, rows: list[dict]) -> dict:
    clean = rows
    thin = [r for r in clean if r["thin"]]
    direct = [r for r in clean if not r["thin"]]
    print(f"\n=== {name}: {len(clean)} sales, {len(direct)} direct, {len(thin)} thin rungs, "
          f"{len({r['comic_id'] for r in thin})} books ===")
    reasons = defaultdict(int)
    for r in thin:
        reasons[r["basis"] or r["reason"]] += 1
    print("  thin-rung current outcome:", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
    cov_cur = mean(bool(r["cur"]) for r in thin)
    cov_l = mean(bool(r["lobo"]) for r in thin)
    cov_u = mean(bool(r["cur"] or r["lobo"]) for r in thin)
    print(f"  coverage on thin rungs: current {cov_cur:.0%} | curve LOBO {cov_l:.0%} | either {cov_u:.0%}")
    print("  reference, direct tier (its IQR band)     :", method_line(direct, "cur", "cur_err"))
    print("  current ladder/lone tier on thin rungs   :", method_line(thin, "cur", "cur_err"))
    print("  curve LOBO point (all thin)               :", method_line(thin, "lobo", "lobo_err"))
    print("  curve OWN point (all thin)                :", method_line(thin, "own", "own_err"))
    print("  curve LOBO band (IQR x ratio)             :", method_line(thin, "lobo_band", "lobo_err"))
    print("  [a] same sales, LOBO point vs current     :", paired_line(thin, "lobo", "cur"))
    print("      same sales, OWN point vs current      :", paired_line(thin, "own", "cur"))
    print("      same sales, LOBO band vs current      :", paired_line(thin, "lobo_band", "cur"))
    print("      list basis, LOBO point vs current     :", paired_line(thin, "lobo_list", "cur_list"))
    resc = [r for r in thin if not r["cur"]]
    print("  [b] rescued refusals, curve LOBO          :", method_line(resc, "lobo", "lobo_err"))
    ladder = [r for r in thin if r["basis"] == "ladder"]
    print("      ladder-tier rows only, current        :", method_line(ladder, "cur", "cur_err"))
    print("      ladder-tier rows only, curve LOBO     :", method_line(ladder, "lobo", "lobo_err"))
    print("      ladder-tier rows, LOBO vs current     :", paired_line(ladder, "lobo", "cur"))
    for e in ("silver", "bronze", "copper", "modern"):
        er = [r for r in thin if r["era"] == e]
        if er:
            print(f"  era {e:<7}: cur {method_line(er, 'cur', 'cur_err')}")
            print(f"              LOBO {method_line(er, 'lobo', 'lobo_err')}")
            print(f"              {paired_line(er, 'lobo', 'cur')}")
    for src in ("ebay", "heritage"):
        sr = [r for r in thin if r["source"] == src]
        print(f"  source {src:<8}: {paired_line(sr, 'lobo', 'cur')} | LOBO {method_line(sr, 'lobo', 'lobo_err')}")
    for lo, hi, lab in ((0, 0.6, "<=0.5"), (0.6, 1.1, "1.0"), (1.1, 99, ">1.0")):
        dr = [r for r in thin if r.get("dist") is not None and lo < r["dist"] <= hi]
        print(f"  anchor distance {lab:<5}: LOBO {method_line(dr, 'lobo', 'lobo_err')}")
    lv = defaultdict(int)
    for r in thin:
        if r.get("lobo"):
            lv[r["lobo_level"]] += 1
    print("  LOBO curve level used:", dict(lv))
    return {"thin": len(thin)}


def main() -> int:
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    ts = T.load_test_set()
    books = {b["comic_id"]: b for b in ts["books"]}
    curves = Curves(ts, books)
    conn = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True)
    led_rows, hist_rows = [], []
    for cid, b in books.items():
        ledger = T.load_ledger(conn, cid)
        pcs = T.universal_sales(ts, cid)
        dated = [{**s, "_date": date.fromisoformat(s["sold_date"])} for s in pcs]
        for s in dated:
            if s["price_outlier"] is True:  # truth hygiene only; comps and curves stay unfiltered
                continue
            d0 = s["_date"]
            if WIN_FROM <= s["sold_date"] <= WIN_TO:
                comps = [c for c in ledger if c["_date"] and c["_date"] < d0 and not T.same_sale(s, c)]
                led_rows.append(price_thin(curves, cid, b["era"], s, comps, d0))
            if HIST_FROM <= s["sold_date"] <= WIN_TO:
                comps = [c for c in dated if c["_date"] < d0]
                if comps:
                    hist_rows.append(price_thin(curves, cid, b["era"], s, comps, d0))

    m = ts["meta"]
    print(f"Test set built {m['built']}: {m['n_books']} books, {m['n_universal']} CGC universal sales.")
    print(f"Curves: {TRAIN_QUARTERS} prior quarters, tiers <{TIER_EDGES[0]:.0f}/<{TIER_EDGES[1]:.0f}/higher, "
          f"cell >= {MIN_CELL_BOOKS} books, grade >= {MIN_GRADE_BOOKS} books. Bootstrap {B.REPS} reps over "
          f"books, seed {B.SEED}. Truth = PC sold (accepted) price, outliers dropped.")
    report(f"(1) LEDGER anchors, {WIN_FROM}..{WIN_TO}", led_rows)
    report(f"(2) PC-HISTORY anchors, {HIST_FROM}..{WIN_TO}", hist_rows)
    print(f"\nFits: {curves.fits['n']} non-empty curve fits; raw (pre-isotonic) f monotone in "
          f"{curves.fits['raw_monotone']} ({curves.fits['raw_monotone'] / max(curves.fits['n'], 1):.0%}).")
    if out_json:
        Path(out_json).write_text(json.dumps({"ledger": led_rows, "pc_history": hist_rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
