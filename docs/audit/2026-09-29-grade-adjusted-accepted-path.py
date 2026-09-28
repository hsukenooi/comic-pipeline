#!/usr/bin/env python3
"""Grade-adjusted pool vs the current band on the ACCEPTED path (BUI-1007).
DIAGNOSTIC ONLY, read-only.

BUI-1005 (docs/audit/2026-09-28-grade-adjusted-pool-backtest.py) found that on
held-out sales the current method prices, a pool of every same-book comp within
+/-2.0 grades shifted to the target grade at +15%/pt beat the current band on
median Winkler/price. This script re-runs that leave-one-out (same ledger load,
same training window, same near-duplicate rule, same compute_fmv, imported
from the BUI-1005 module) and asks whether the accepted-path win is real:

  * paired gap per held-out sale = (W_adjusted - W_current) / price, on the
    rows BOTH methods price (negative = adjusted better);
  * book-clustered bootstrap (resample comic_ids, REPS reps) CI on the mean
    and median paired gap, and on median(W_adj) - median(W_cur);
  * strata: era by comic year, and price quartile of the TRAINING pool's
    median price (never the held-out price, which conditions on the outcome);
  * slope sensitivity: fixed +15%, unshifted +/-2.0 control (0%), a pooled
    within-book slope fit only on comps sold before the held-out sale, the
    book's own slope fit only on its earlier comps (raw), and that slope
    shrunk toward the time-respecting pooled slope and toward +15%;
  * grade sensitivity: stored ledger grades vs grades re-parsed from the
    title with the current (BUI-1003-fixed) sold_comps.parse_grade.

    uv run --project plugins/gixen-overlay python \\
        docs/audit/2026-09-29-grade-adjusted-accepted-path.py [--rows-json out.json]

Opens ~/.comics-server/db.sqlite with mode=ro. Nothing is written to the DB.
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sqlite3
import sys
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, quantiles

from gixen_overlay.band_compare import winkler_score

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "apps" / "ebay" / "src"))
import sold_comps  # noqa: E402  (parse_grade, BUI-1003 fixed)

_spec = importlib.util.spec_from_file_location(
    "bt1005", HERE / "2026-09-28-grade-adjusted-pool-backtest.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)
fmv_math = bt.fmv_math

REPS = 2000
SEED = 1007


# ─── Loading (BUI-1005's loader + title, optional grade re-parse) ───────────

def load_comps(conn: sqlite3.Connection, regrade: bool) -> tuple[list[dict], dict]:
    rows = conn.execute(
        f"""
        SELECT c.id, c.comic_id, c.provider, c.product_id, c.price, c.sold_date,
               c.grade, c.title AS comp_title, c.tier, c.first_seen_at,
               k.title AS book, k.issue, k.year, k.variant
        FROM comps c JOIN comics k ON k.id = c.comic_id
        WHERE c.pool = 'raw' AND c.grade IS NOT NULL AND c.excluded_code IS NULL
          AND c.price > 0
          AND COALESCE(c.tier, '') NOT IN ({",".join("?" * len(bt.EXCLUDED_TIERS))})
        ORDER BY c.id
        """, bt.EXCLUDED_TIERS).fetchall()
    stats = {"loaded": len(rows), "regrade_changed": 0, "regrade_to_none": 0,
             "no_date": 0, "dup_product": 0, "dup_value": 0}
    seen_pid: set = set()
    seen_val: set = set()
    out = []
    for r in rows:
        r = dict(r)
        if regrade:
            g = sold_comps.parse_grade(r["comp_title"] or "")
            if g is None:
                stats["regrade_to_none"] += 1
                continue
            if g != r["grade"]:
                stats["regrade_changed"] += 1
                r["grade"] = g
        d = fmv_math._parse_sold_date(r["sold_date"])
        if d is None:
            stats["no_date"] += 1
            continue
        if r["product_id"]:
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
        out.append({**r, "d": d, "sold_date": d.isoformat()})
    stats["kept"] = len(out)
    return out, stats


def era4(year) -> str:
    if year is None:
        return "unknown"
    if year < 1970:
        return "pre-1970"
    if year < 1985:
        return "1970-84"
    if year < 2000:
        return "1985-99"
    return "2000+"


# ─── Time-respecting slopes ─────────────────────────────────────────────────

def _suff(cs):
    """Sufficient stats (n, sg, sy, sgg, sgy) of ln(price) on grade."""
    n = len(cs)
    sg = sum(c["grade"] for c in cs)
    sy = sum(math.log(c["price"]) for c in cs)
    sgg = sum(c["grade"] ** 2 for c in cs)
    sgy = sum(c["grade"] * math.log(c["price"]) for c in cs)
    return n, sg, sy, sgg, sgy


def _sxx_sxy(n, sg, sy, sgg, sgy):
    if n < 2:
        return 0.0, 0.0
    return sgg - sg * sg / n, sgy - sg * sy / n


class PooledBefore:
    """Within-book pooled OLS log-slope using only comps sold strictly before
    a date, across every book. Built incrementally in date order."""

    def __init__(self, comps):
        self.comps = sorted(comps, key=lambda c: c["d"])
        self.dates = [c["d"] for c in self.comps]
        self.i = 0
        self.book = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
        self.sxx = self.sxy = 0.0
        self.cache: dict = {}

    def at(self, d) -> float | None:
        if d in self.cache:
            return self.cache[d]
        stop = bisect_left(self.dates, d)
        assert stop >= self.i, "query dates must be non-decreasing"
        while self.i < stop:
            c = self.comps[self.i]
            s = self.book[c["comic_id"]]
            ox, oy = _sxx_sxy(*s)
            y = math.log(c["price"])
            s[0] += 1; s[1] += c["grade"]; s[2] += y
            s[3] += c["grade"] ** 2; s[4] += c["grade"] * y
            nx, ny = _sxx_sxy(*s)
            self.sxx += nx - ox
            self.sxy += ny - oy
            self.i += 1
        b = self.sxy / self.sxx if self.sxx > 1e-9 else None
        self.cache[d] = b
        return b


def shrink_k(comps) -> tuple[float, dict]:
    """Empirical-Bayes prior weight k = sigma^2 / tau^2 for shrinking a
    per-book log-slope: b_shrunk = (sxy_b + k * b_prior) / (sxx_b + k).
    Estimated once on the full ledger (one hyperparameter; noted as a mild
    look-ahead in the report). Books need >=4 comps and sxx >= 1."""
    by = defaultdict(list)
    for c in comps:
        by[c["comic_id"]].append(c)
    bs, vs, res, dof = [], [], 0.0, 0
    for cs in by.values():
        n, sg, sy, sgg, sgy = _suff(cs)
        sxx, sxy = _sxx_sxy(n, sg, sy, sgg, sgy)
        if n < 4 or sxx < 1.0:
            continue
        b = sxy / sxx
        ym = sy / n
        syy = sum((math.log(c["price"]) - ym) ** 2 for c in cs)
        rss = max(syy - b * sxy, 0.0)
        res += rss
        dof += n - 2
        bs.append((b, sxx))
    sigma2 = res / dof
    bm = mean(b for b, _ in bs)
    var_b = sum((b - bm) ** 2 for b, _ in bs) / (len(bs) - 1)
    noise = mean(sigma2 / sxx for _, sxx in bs)
    tau2 = max(var_b - noise, 1e-6)
    return sigma2 / tau2, {"books": len(bs), "sigma2": sigma2, "var_b": var_b,
                           "noise": noise, "tau2": tau2,
                           "k_half": median(sxx for _, sxx in bs)}


# ─── Leave-one-out ──────────────────────────────────────────────────────────

LOG15 = math.log(1.15)


def run(comps: list[dict], k: float, k_half: float) -> list[dict]:
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    pooled = PooledBefore(comps)
    rows = []
    for c in sorted(comps, key=lambda c: (c["d"], c["id"])):
        train, _ = bt.training(by_book[c["comic_id"]], c)
        if not any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train):
            continue
        cur = bt.price_current(train, c["grade"])
        if cur["flag"] is not None or not cur["high"]:
            continue  # accepted path only
        # Slopes use only comps sold strictly before d (the book's whole
        # history, not just the 90-day window), minus the held-out sale's
        # own near-duplicates exactly as training() drops them.
        hist = [t for t in by_book[c["comic_id"]] if t["d"] < c["d"] and not (
            t["grade"] == c["grade"] and abs(t["price"] - c["price"]) < 0.01
            and (c["d"] - t["d"]).days <= 7)]
        bp = pooled.at(c["d"])
        bp = 0.0 if bp is None else bp
        sxx, sxy = _sxx_sxy(*_suff(hist))
        b_raw = sxy / sxx if sxx >= 1.0 and len(hist) >= 3 else bp
        slopes = {
            "adj15": LOG15,
            "adj0": 0.0,
            "pooled_t": bp,
            "book_raw": b_raw,
            "shrunk_pooled": (sxy + k * bp) / (sxx + k),
            "shrunk_15": (sxy + k * LOG15) / (sxx + k),
            "half_shrunk": (sxy + k_half * bp) / (sxx + k_half),
        }
        r = {"comp_id": c["id"], "comic_id": c["comic_id"], "grade": c["grade"],
             "price": c["price"], "d": c["sold_date"], "era": era4(c["year"]),
             "train_median": median(t["price"] for t in train), "current": cur,
             "slopes": {n: math.exp(b) - 1 for n, b in slopes.items()},
             "n_train": len(train),
             "n_near": sum(abs(t["grade"] - c["grade"]) <= bt.ADJ_MAX_DIST + 1e-9
                           for t in train),
             "n_same_grade": sum(t["grade"] == c["grade"] for t in train)}
        for name, b in slopes.items():
            r[name] = bt.price_adjusted(train, c["grade"], math.exp(b) - 1)
        rows.append(r)
    return rows


# ─── Scoring ────────────────────────────────────────────────────────────────

def w(b: dict, y: float) -> float:
    return winkler_score(float(b["low"]), float(b["high"]), y) / y


def priced(b: dict) -> bool:
    return bool(b["high"]) and b["low"] is not None


def boot(pairs: list[tuple[int, float, float, float]], reps: int = REPS,
         seed: int = SEED) -> dict:
    """pairs: (comic_id, W_cur, W_adj, gap). Book-clustered bootstrap."""
    by = defaultdict(list)
    for p in pairs:
        by[p[0]].append(p)
    books = sorted(by)
    gaps = [p[3] for p in pairs]

    def stats(ps):
        g = [p[3] for p in ps]
        nt = sum(x != 0 for x in g)
        return (mean(g), median(g), median(p[2] for p in ps) - median(p[1] for p in ps),
                trimmed_mean(g), sum(x < 0 for x in g) / nt if nt else 0.5)
    point = stats(pairs)
    rng = random.Random(seed)
    draws = []
    for _ in range(reps):
        ps = [p for b in (rng.choice(books) for _ in books) for p in by[b]]
        draws.append(stats(ps))

    def ci(i):
        v = sorted(x[i] for x in draws)
        return v[int(0.025 * reps)], v[int(0.975 * reps) - 1]
    return {"n": len(pairs), "books": len(books),
            "adj_wins": sum(g < 0 for g in gaps), "cur_wins": sum(g > 0 for g in gaps),
            "ties": sum(g == 0 for g in gaps),
            "w_cur": median(p[1] for p in pairs), "w_adj": median(p[2] for p in pairs),
            "mean_gap": point[0], "mean_ci": ci(0),
            "median_gap": point[1], "median_ci": ci(1),
            "dmed": point[2], "dmed_ci": ci(2),
            "tmean": point[3], "tmean_ci": ci(3), "winshare": point[4], "winshare_ci": ci(4)}


TRIM = 0.05


def trimmed_mean(g: list[float]) -> float:
    """Mean after dropping the TRIM share of pairs from EACH tail. A handful of
    pairs with Winkler/price in the hundreds (sub-$1 sales on a polluted book)
    otherwise decide the plain mean."""
    v = sorted(g)
    k = int(TRIM * len(v))
    v = v[k:len(v) - k] if len(v) > 2 * k else v
    return mean(v)


def pairs_for(rows, key):
    out = []
    for r in rows:
        wc, wa = w(r["current"], r["price"]), w(r[key], r["price"])
        out.append((r["comic_id"], wc, wa, wa - wc))
    return out


METHODS = [("adj15", "+15% fixed"), ("adj0", "0% (unshifted +/-2.0 control)"),
           ("pooled_t", "pooled within-book, time-respecting"),
           ("book_raw", "per-book raw, time-respecting"),
           ("shrunk_pooled", "per-book shrunk to pooled"),
           ("shrunk_15", "per-book shrunk to +15%"),
           ("half_shrunk", "per-book half-shrunk to pooled (k = median book sxx)")]


def ci_s(ci):
    return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def line(label, s):
    return (f"| {label} | {s['n']} | {s['books']} | {s['w_cur']:.3f} | {s['w_adj']:.3f} | "
            f"{s['adj_wins']}/{s['cur_wins']}/{s['ties']} | "
            f"{s['winshare']:.1%} [{s['winshare_ci'][0]:.1%}, {s['winshare_ci'][1]:.1%}] | "
            f"{s['tmean']:+.3f} {ci_s(s['tmean_ci'])} | {s['mean_gap']:+.3f} {ci_s(s['mean_ci'])} | "
            f"{s['median_gap']:+.3f} {ci_s(s['median_ci'])} | {s['dmed']:+.3f} {ci_s(s['dmed_ci'])} |")


HDR = ("| {0} | Pairs | Books | Cur W/p (med) | Adj W/p (med) | Adj/cur/tie wins | "
       "Adj win share of non-ties [95% CI] | 5%-trimmed mean gap [95% CI] | "
       "Mean gap [95% CI] | Median gap [95% CI] | Diff of medians [95% CI] |\n"
       "|---|---|---|---|---|---|---|---|---|---|---|")


def analyse(label: str, rows: list[dict], report: dict) -> None:
    print(f"\n# {label}\n")
    common = [r for r in rows if all(priced(r[m]) for m, _ in METHODS)]
    print(f"accepted held-out sales: {len(rows)}; priced by every adjusted variant "
          f"(the common pair set): {len(common)} "
          f"({len(rows) - len(common)} accepted sales fall to the >=3-comp floor)")
    out = {"accepted": len(rows), "common": len(common)}

    print("\n## Slope sensitivity (common pair set; gap = adjusted - current, "
          "negative favours adjusted)\n")
    print(HDR.format("Slope"))
    out["methods"] = {}
    for m, name in METHODS:
        s = boot(pairs_for(common, m))
        out["methods"][m] = s
        print(line(name, s))
    top_book = min(((cid, sum(p[3] for p in pairs_for(
        [r for r in common if r["comic_id"] == cid], "adj15")))
        for cid in {r["comic_id"] for r in common}), key=lambda kv: kv[1])[0]
    s = boot(pairs_for([r for r in common if r["comic_id"] != top_book], "adj15"))
    out["adj15_minus_top_book"] = {"book": top_book, **s}
    print(line(f"+15% fixed, without book {top_book} (largest summed gap)", s))
    sl = defaultdict(list)
    for r in common:
        for m, v in r["slopes"].items():
            sl[m].append(v)
    print("\nslope actually applied (per held-out sale), median [Q1, Q3]:")
    for m, name in METHODS:
        q1, q2, q3 = quantiles(sl[m], n=4, method="inclusive")
        print(f"  {name}: {q2:+.1%} [{q1:+.1%}, {q3:+.1%}]")

    # Ties: are they genuine (the same band from the same pool)?
    same = sum(1 for r in common if (r["adj15"]["low"], r["adj15"]["high"])
               == (r["current"]["low"], r["current"]["high"]))
    tie = [r for r in common if w(r["adj15"], r["price"]) == w(r["current"], r["price"])]
    tie_same = sum(1 for r in tie if (r["adj15"]["low"], r["adj15"]["high"])
                   == (r["current"]["low"], r["current"]["high"]))
    all_same_grade = sum(1 for r in common if r["n_near"] == r["n_same_grade"])
    print(f"\nties (+15%): {len(tie)}; of them identical bands: {tie_same}; identical "
          f"bands overall: {same}; sales whose +/-2.0 training pool is all at the target "
          f"grade (shift is a no-op): {all_same_grade}")
    out["ties"] = {"ties": len(tie), "tie_identical_band": tie_same,
                   "identical_band": same, "pool_all_target_grade": all_same_grade}

    # Strata
    qs = quantiles([r["train_median"] for r in common], n=4, method="inclusive")
    edges = [0.0] + qs + [float("inf")]
    strata = {}
    for e in ("pre-1970", "1970-84", "1985-99", "2000+", "unknown"):
        strata[f"era {e}"] = [r for r in common if r["era"] == e]
    for i in range(4):
        lo, hi = edges[i], edges[i + 1]
        strata[f"price Q{i + 1} (${lo:.0f}-{'' if hi == float('inf') else f'${hi:.0f}'})"] = [
            r for r in common if lo <= r["train_median"] < hi]
    # Differing pairs only: drop sales where both methods produce the same band.
    strata["non-identical bands only"] = [
        r for r in common if (r["adj15"]["low"], r["adj15"]["high"])
        != (r["current"]["low"], r["current"]["high"])]
    out["strata"] = {}
    for m in ("adj15", "shrunk_pooled", "adj0"):
        print(f"\n## Strata, {dict(METHODS)[m]}\n")
        print(HDR.format("Stratum"))
        out["strata"][m] = {}
        for name, rs in strata.items():
            if len({r["comic_id"] for r in rs}) < 5:
                continue
            s = boot(pairs_for(rs, m))
            out["strata"][m][name] = s
            print(line(name, s))
    # Concentration: the share of the total gap each stratum carries.
    tot = sum(p[3] for p in pairs_for(common, "adj15"))
    print("\nshare of the total +15% gap (sum of per-sale gaps) by stratum:")
    for name, rs in strata.items():
        if name.startswith("non-"):
            continue
        g = sum(p[3] for p in pairs_for(rs, "adj15"))
        print(f"  {name}: {g:+.1f} of {tot:+.1f} ({(g / tot if tot else 0):.0%})")
    # Book concentration: top books by summed gap.
    per_book = defaultdict(float)
    for p in pairs_for(common, "adj15"):
        per_book[p[0]] += p[3]
    top = sorted(per_book.items(), key=lambda kv: kv[1])[:5]
    print(f"five books carrying the most adjusted-favourable gap: "
          f"{[(b, round(g, 1)) for b, g in top]} of total {tot:+.1f}")
    out["book_top5"] = top
    out["total_gap"] = tot
    report[label] = out


def main() -> int:
    conn = sqlite3.connect(f"file:{bt.DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report: dict = {}
    for regrade, label in ((False, "stored grades"), (True, "grades re-parsed from title")):
        comps, stats = load_comps(conn, regrade)
        k, kinfo = shrink_k(comps)
        print(f"\n\n==== {label}: {stats}; shrinkage k = {k:.1f} ({kinfo})")
        rows = run(comps, k, kinfo["k_half"])
        report[label + " load"] = {"stats": stats, "k": k, "kinfo": kinfo}
        analyse(label, rows, report)
    conn.close()
    if "--rows-json" in sys.argv:
        with open(sys.argv[sys.argv.index("--rows-json") + 1], "w") as fh:
            json.dump(report, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
