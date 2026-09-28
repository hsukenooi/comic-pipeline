#!/usr/bin/env python3
"""Backtest a grade-adjusted raw comp pool against the one_sided / too_wide
refusals (BUI-1005). DIAGNOSTIC ONLY, read-only.

Current method: `fmv_math.compute_fmv` on the training comps as they are
(progressive build_pool widening to +/-2.0, _classify_pool guards, the BUI-306
bracket rescue, BUI-528 collapse split, BUI-990 width floor).

Proposed method: every training comp within +/-2.0 grades of the target g is
shifted to g at a constant per-grade slope s (price x (1 + s)^(g - grade)),
at least 3 shifted comps are required, and the shifted comps (all now at
grade g) go through the SAME compute_fmv (IQR trim, recency-weighted Q25/Q75,
collapse split, width floor). With every comp at g the pool guards cannot
fire, so the only refusals left are the >=3 floor and BUI-179's 2-comp guard.

Leave-one-out: each held-out sale is a deduplicated graded raw ledger comp
(book, grade g, date d, price y). Training = the same comic_id's other graded
raw comps sold in [d - 90, d) (strictly before d, so no later sale and no
same-day sale), minus near-duplicates of the held-out sale itself (same grade,
price within $0.01, sold within 7 days: a cross-provider copy of the same sale
with a drifted date). Scored with BUI-979's Winkler score (alpha 0.5),
scaled by the sale price.

Also: per-book price-vs-grade slope (OLS of log price on grade), and the
2026-09-26 batch's unpriced books re-priced from ledger comps as of that day.

    uv run --project plugins/gixen-overlay python \\
        docs/audit/2026-09-28-grade-adjusted-pool-backtest.py [--rows-json out.json]

Opens ~/.comics-server/db.sqlite with mode=ro. Nothing is written to the DB.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median, quantiles

from gixen_overlay.band_compare import DEFAULT_ALPHA, winkler_score

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "fmv" / "src"))
import fmv_math  # noqa: E402

DB = os.path.expanduser("~/.comics-server/db.sqlite")
WINDOW_DAYS = 90
ADJ_MAX_DIST = 2.0
ADJ_MIN_N = 3
# Tiers whose query dropped identity terms: their comps can be the base cover
# of a variant book, so same-comic_id identity is not guaranteed. Excluded.
EXCLUDED_TIERS = ("no-variant", "broader")
BATCH_DAY = "2026-09-26"
SHAPE_REFUSALS = ("one_sided", "too_wide")


# ─── Loading ────────────────────────────────────────────────────────────────

def load_comps(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    rows = conn.execute(
        f"""
        SELECT c.id, c.comic_id, c.provider, c.product_id, c.price, c.sold_date,
               c.grade, c.tier, c.first_seen_at, k.title AS book, k.issue, k.year,
               k.variant
        FROM comps c JOIN comics k ON k.id = c.comic_id
        WHERE c.pool = 'raw' AND c.grade IS NOT NULL AND c.excluded_code IS NULL
          AND c.price > 0
          AND COALESCE(c.tier, '') NOT IN ({",".join("?" * len(EXCLUDED_TIERS))})
        ORDER BY c.id
        """,
        EXCLUDED_TIERS,
    ).fetchall()
    stats = {"loaded": len(rows), "no_date": 0, "dup_product": 0, "dup_value": 0}
    seen_pid: set = set()
    seen_val: set = set()
    out = []
    for r in rows:
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
        out.append({**dict(r), "d": d, "sold_date": d.isoformat()})
    stats["kept"] = len(out)
    return out, stats


def era(year: int | None) -> str:
    if year is None:
        return "unknown"
    if year < 1980:
        return "pre-1980"
    if year < 2000:
        return "1980-1999"
    return "2000+"


# ─── Slope ──────────────────────────────────────────────────────────────────

def book_slopes(comps: list[dict]) -> list[dict]:
    """OLS of ln(price) on grade per book; books with >=6 comps, >=3 distinct
    grades, and a grade span >= 2.0. Slope reported as exp(b) - 1 per point."""
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    out = []
    for cid, cs in by_book.items():
        gs = [c["grade"] for c in cs]
        if len(cs) < 6 or len(set(gs)) < 3 or max(gs) - min(gs) < 2.0:
            continue
        ys = [math.log(c["price"]) for c in cs]
        gm, ym = sum(gs) / len(gs), sum(ys) / len(ys)
        sxx = sum((g - gm) ** 2 for g in gs)
        b = sum((g - gm) * (y - ym) for g, y in zip(gs, ys)) / sxx
        out.append({"comic_id": cid, "n": len(cs), "slope": math.exp(b) - 1,
                    "era": era(cs[0]["year"]), "median_price": median(c["price"] for c in cs)})
    return out


def pooled_slope(comps: list[dict]) -> float:
    """Within-book (book-demeaned) pooled OLS slope, exp(b) - 1."""
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    sxy = sxx = 0.0
    for cs in by_book.values():
        if len(cs) < 2:
            continue
        gm = sum(c["grade"] for c in cs) / len(cs)
        ym = sum(math.log(c["price"]) for c in cs) / len(cs)
        for c in cs:
            sxx += (c["grade"] - gm) ** 2
            sxy += (c["grade"] - gm) * (math.log(c["price"]) - ym)
    return math.exp(sxy / sxx) - 1


def summarize_slopes(sl: list[dict]) -> list[tuple]:
    rows = []
    groups = [("all", sl)] + [(e, [s for s in sl if s["era"] == e])
                              for e in ("pre-1980", "1980-1999", "2000+")]
    groups += [("median comp < $20", [s for s in sl if s["median_price"] < 20]),
               ("$20-$100", [s for s in sl if 20 <= s["median_price"] < 100]),
               (">= $100", [s for s in sl if s["median_price"] >= 100])]
    for name, g in groups:
        v = [s["slope"] for s in g]
        if len(v) < 4:
            rows.append((name, len(v), None, None, None))
            continue
        q1, q2, q3 = quantiles(v, n=4, method="inclusive")
        rows.append((name, len(v), q2, q1, q3))
    return rows


# ─── Pricing ────────────────────────────────────────────────────────────────

def as_comp(c: dict, price: float | None = None, grade: float | None = None) -> dict:
    return {"price": c["price"] if price is None else price,
            "grade": c["grade"] if grade is None else grade,
            "sold_date": c["sold_date"]}


def price_current(train: list[dict], g: float) -> dict:
    r = fmv_math.compute_fmv([as_comp(c) for c in train], g)
    return {"low": r["fmv_low"], "high": r["fmv_high"], "median": r["median"],
            "flag": r["flag_reason"], "interpolated": r["interpolated"],
            "n": r["n"]}


def price_adjusted(train: list[dict], g: float, slope: float) -> dict:
    near = [c for c in train if abs(c["grade"] - g) <= ADJ_MAX_DIST + 1e-9]
    if len(near) < ADJ_MIN_N:
        return {"low": None, "high": None, "median": None, "flag": "adj_lt3", "n": len(near)}
    shifted = [as_comp(c, c["price"] * (1 + slope) ** (g - c["grade"]), g) for c in near]
    r = fmv_math.compute_fmv(shifted, g)
    return {"low": r["fmv_low"], "high": r["fmv_high"], "median": r["median"],
            "flag": r["flag_reason"], "n": r["n"]}


def training(book: list[dict], target: dict) -> tuple[list[dict], int]:
    lo = target["d"] - timedelta(days=WINDOW_DAYS)
    out, dropped = [], 0
    for c in book:
        if c is target or not (lo <= c["d"] < target["d"]):
            continue
        if (c["grade"] == target["grade"] and abs(c["price"] - target["price"]) < 0.01
                and (target["d"] - c["d"]).days <= 7):
            dropped += 1
            continue
        out.append(c)
    return out, dropped


# ─── Scoring ────────────────────────────────────────────────────────────────

def metrics(rows: list[dict], key: str) -> dict:
    priced = [r for r in rows if r[key]["high"] is not None and r[key]["low"] is not None
              and r[key]["high"] > 0]
    n = len(rows)
    if not priced:
        return {"n": n, "priced": 0}
    w, ratio, logr, width, inb, above, below = [], [], [], [], 0, 0, 0
    for r in priced:
        b, y = r[key], r["price"]
        lo, hi = float(b["low"]), float(b["high"])
        w.append(winkler_score(lo, hi, y, DEFAULT_ALPHA) / y)
        m = float(b["median"]) if b["median"] else (lo + hi) / 2
        ratio.append(max(m / y, y / m))
        logr.append(math.log(m / y))
        mid = (lo + hi) / 2
        width.append((hi - lo) / mid if mid else 0.0)
        if y < lo:
            below += 1
        elif y > hi:
            above += 1
        else:
            inb += 1
    k = len(priced)
    return {"n": n, "priced": k, "priced_pct": 100 * k / n,
            "wink_scaled_median": median(w), "err_ratio_median": median(ratio),
            "bias_median": math.exp(median(logr)),  # median est/price
            "in_band_pct": 100 * inb / k, "above_pct": 100 * above / k,
            "below_pct": 100 * below / k, "width_median": median(width)}


def paired(rows: list[dict], a: str, b: str) -> dict:
    both = [r for r in rows if r[a]["high"] and r[b]["high"] and r[a]["low"] is not None
            and r[b]["low"] is not None]
    aw = bw = ties = 0
    diffs = []
    for r in both:
        wa = winkler_score(float(r[a]["low"]), float(r[a]["high"]), r["price"])
        wb = winkler_score(float(r[b]["low"]), float(r[b]["high"]), r["price"])
        diffs.append((wb - wa) / r["price"])
        aw += wa < wb
        bw += wb < wa
        ties += wa == wb
    return {"n": len(both), f"{a}_better": aw, f"{b}_better": bw, "ties": ties,
            "median_scaled_diff": median(diffs) if diffs else None}


def scaled_w(rows: list[dict], key: str) -> list[tuple[int, float]]:
    return [(r["comic_id"], winkler_score(float(r[key]["low"]), float(r[key]["high"]),
                                          r["price"]) / r["price"])
            for r in rows if r[key]["high"] and r[key]["low"] is not None]


def cluster_bootstrap_gap(a: list[tuple[int, float]], b: list[tuple[int, float]],
                          reps: int = 1000, seed: int = 1005) -> tuple[float, float, float]:
    """median(a) - median(b), with a 95% interval from resampling BOOKS (a
    book's sales are correlated, so resampling sales would overstate certainty)."""
    import random
    rng = random.Random(seed)
    ga: dict[int, list[float]] = defaultdict(list)
    gb: dict[int, list[float]] = defaultdict(list)
    for k, v in a:
        ga[k].append(v)
    for k, v in b:
        gb[k].append(v)
    books = sorted(set(ga) | set(gb))
    gaps = []
    for _ in range(reps):
        pick = [rng.choice(books) for _ in books]
        xa = [v for k in pick for v in ga.get(k, [])]
        xb = [v for k in pick for v in gb.get(k, [])]
        if xa and xb:
            gaps.append(median(xa) - median(xb))
    gaps.sort()
    return (median(v for _, v in a) - median(v for _, v in b),
            gaps[int(0.025 * len(gaps))], gaps[int(0.975 * len(gaps)) - 1])


# ─── Main ───────────────────────────────────────────────────────────────────

def fmt(x, p=3, suf=""):
    return "-" if x is None else f"{x:.{p}f}{suf}"


def print_table(title: str, cuts: dict[str, list[dict]], keys: list[tuple[str, str]]) -> dict:
    print(f"\n## {title}\n")
    print("| Cut | Method | Sales | Priced | Winkler/price (median) | Error ratio (median) "
          "| Median est/price | In band | Above | Below | Width (median) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    out = {}
    for cut, rows in cuts.items():
        for key, label in keys:
            m = metrics(rows, key)
            out[f"{cut}|{label}"] = m
            if not m["priced"]:
                print(f"| {cut} | {label} | {m['n']} | 0 | - | - | - | - | - | - | - |")
                continue
            print(f"| {cut} | {label} | {m['n']} | {m['priced']} ({m['priced_pct']:.0f}%) | "
                  f"{fmt(m['wink_scaled_median'])} | x{fmt(m['err_ratio_median'], 2)} | "
                  f"x{fmt(m['bias_median'], 2)} | {fmt(m['in_band_pct'], 1, '%')} | "
                  f"{fmt(m['above_pct'], 1, '%')} | {fmt(m['below_pct'], 1, '%')} | "
                  f"{fmt(100 * m['width_median'], 0, '%')} |")
    return out


def fresh_batch(conn: sqlite3.Connection, comps: list[dict], slopes: dict[str, float]) -> list[dict]:
    day = date.fromisoformat(BATCH_DAY)
    books = conn.execute(
        """
        SELECT h.comic_id, h.grade, h.flag_reason, h.comps, h.notes,
               k.title, k.issue, k.year, k.variant
        FROM fmv_history h JOIN comics k ON k.id = h.comic_id
        WHERE substr(h.recorded_at, 1, 10) = ? AND h.high IS NULL
        ORDER BY k.title, CAST(k.issue AS INTEGER)
        """, (BATCH_DAY,)).fetchall()
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    out = []
    cutoff = BATCH_DAY + "T23:59:59"
    for b in books:
        cs = by_book.get(b["comic_id"], [])
        known = [c for c in cs if (c["first_seen_at"] or "") <= cutoff]
        train = [c for c in known if day - timedelta(days=WINDOW_DAYS) <= c["d"] <= day]
        later = [c for c in cs if c["d"] > day]
        bids = conn.execute(
            "SELECT status, winning_bid, grade, max_bid FROM bids WHERE comic_id = ? "
            "AND added_at >= ?", (b["comic_id"], BATCH_DAY)).fetchall()
        anchor = None
        for tok in (b["notes"] or "").split(" | "):
            if tok.startswith("ungraded_anchor="):
                anchor = tok.split("=", 1)[1]
        row = {"comic_id": b["comic_id"], "book": f"{b['title']} #{b['issue']}",
               "year": b["year"], "variant": b["variant"], "grade": b["grade"],
               "flag": b["flag_reason"] or "no comps", "anchor": anchor,
               "train": [(c["grade"], c["price"]) for c in sorted(train, key=lambda c: c["grade"])],
               "current": price_current(train, b["grade"]),
               "later": [(c["grade"], c["price"], c["sold_date"]) for c in later],
               "bids": [dict(x) for x in bids]}
        for name, s in slopes.items():
            row[name] = price_adjusted(train, b["grade"], s)
        out.append(row)
    return out


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    comps, stats = load_comps(conn)
    print(f"alpha={DEFAULT_ALPHA}; graded raw comps: {stats}")

    # 1. Slope
    sl = book_slopes(comps)
    pooled = pooled_slope(comps)
    print(f"\n## Per-book slope (exp(b)-1 per grade point); pooled within-book = {pooled:+.1%}\n")
    print("| Group | Books | Median | Q1 | Q3 |")
    print("|---|---|---|---|---|")
    slope_rows = summarize_slopes(sl)
    for name, n, q2, q1, q3 in slope_rows:
        print(f"| {name} | {n} | {fmt(q2 and 100 * q2, 1, '%')} | "
              f"{fmt(q1 and 100 * q1, 1, '%')} | {fmt(q3 and 100 * q3, 1, '%')} |")
    measured = slope_rows[0][2]
    # adj0 = the same +/-2.0 pool with NO shift: isolates what the slope adds
    # over simply pooling every comp within +/-2.0 grades.
    slopes = {"adj15": 0.15, "adj_meas": measured, "adj0": 0.0}
    ids = {s["comic_id"] for s in sl}
    pooled_q = pooled_slope([c for c in comps if c["comic_id"] in ids])
    print(f"\nmeasured median slope used for 'adj_meas': {measured:+.2%}; books with "
          f"slope <= 0: {sum(s['slope'] <= 0 for s in sl)}/{len(sl)}; pooled within-book "
          f"slope on the same books: {pooled_q:+.1%}")

    # 2. Leave-one-out
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    rows, no_train, near_dup = [], 0, 0
    for c in comps:
        train, dropped = training(by_book[c["comic_id"]], c)
        near_dup += dropped
        if not any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train):
            no_train += 1
            continue
        r = {"comp_id": c["id"], "comic_id": c["comic_id"], "grade": c["grade"],
             "price": c["price"], "d": c["sold_date"], "era": era(c["year"]),
             "train_median": median(t["price"] for t in train),
             "current": price_current(train, c["grade"])}
        for name, s in slopes.items():
            r[name] = price_adjusted(train, c["grade"], s)
        # Hybrid = what shipping behind a flag would do: current where it
        # prices, adjusted only where the current guards refuse on shape.
        r["hybrid15"] = r["adj15"] if r["current"]["flag"] in SHAPE_REFUSALS else r["current"]
        rows.append(r)
    flags = defaultdict(int)
    for r in rows:
        f = r["current"]["flag"]
        flags[f if f else ("interpolated" if r["current"]["interpolated"] else "priced")] += 1
    print(f"\nheld-out sales N={len(rows)} (excluded: {no_train} with no same-book comp "
          f"within +/-2.0 in the prior {WINDOW_DAYS} days; {near_dup} near-duplicate "
          f"training comps dropped)")
    print(f"current outcome: {dict(flags)}")

    refused = [r for r in rows if r["current"]["flag"] in SHAPE_REFUSALS]
    sparse = [r for r in rows if r["current"]["flag"] == "too_sparse"]
    accepted = [r for r in rows if r["current"]["flag"] is None and r["current"]["high"]]
    cuts = {"refused (one_sided/too_wide)": refused, "accepted": accepted,
            "too_sparse": sparse, "all": rows}
    keys = [("current", "current"), ("adj15", "adjusted +15%"),
            ("adj_meas", f"adjusted {measured:+.1%}"), ("adj0", "unshifted +/-2.0 pool"),
            ("hybrid15", "hybrid (+15% on refusals)")]
    table = print_table("Leave-one-out, all eras", cuts, keys)
    by_era = {}
    for e in ("pre-1980", "1980-1999", "2000+"):
        by_era[e] = print_table(f"Era {e}", {
            "refused": [r for r in refused if r["era"] == e],
            "accepted": [r for r in accepted if r["era"] == e]}, keys[:2] + keys[3:4])
    # Refused cut by the book's price level. Bucketed on the TRAINING pool's
    # median price, never on the held-out price y: bucketing on y conditions on
    # the outcome (a sale under $20 is a low draw by selection, so every
    # estimator reads as overpriced there and underpriced above $100).
    by_price = print_table("Refused cut by training-pool median price", {
        "< $20": [r for r in refused if r["train_median"] < 20],
        "$20-$100": [r for r in refused if 20 <= r["train_median"] < 100],
        ">= $100": [r for r in refused if r["train_median"] >= 100]}, [keys[1], keys[3]])
    # The matching accepted-cut baseline per price level: a refused-cut score
    # is only "no worse" against the accepted books of the same price level.
    by_price_acc = print_table("Accepted cut by training-pool median price", {
        "< $20": [r for r in accepted if r["train_median"] < 20],
        "$20-$100": [r for r in accepted if 20 <= r["train_median"] < 100],
        ">= $100": [r for r in accepted if r["train_median"] >= 100]}, keys[:2])
    # Same-rows check on the accepted cut: the adjusted method prices only 91%
    # of it, so compare current on exactly the rows adjusted priced.
    adj_rows = [r for r in accepted if r["adj15"]["high"]]
    same = print_table("Accepted cut, rows both methods price", {
        "accepted & adjusted priced": adj_rows,
        "accepted & adjusted refused": [r for r in accepted if not r["adj15"]["high"]]},
        keys[:2])
    gap = cluster_bootstrap_gap(scaled_w(refused, "adj15"), scaled_w(accepted, "current"))
    print(f"\ndecision gap, median W/price (adjusted +15% on refused) - (current on "
          f"accepted): {gap[0]:+.3f}, 95% book-clustered bootstrap [{gap[1]:+.3f}, {gap[2]:+.3f}]")
    pa = paired(accepted, "current", "adj15")
    print(f"\npaired on accepted cut (current vs adjusted +15%): {pa}")

    # 3. Fresh batch
    fb = fresh_batch(conn, comps, slopes)
    print(f"\n## Fresh batch {BATCH_DAY}: {len(fb)} unpriced books, ledger comps as of that day\n")
    print("| Book | Grade | Batch flag | Ledger comps in window (grade:$) | Current replay | "
          "Adjusted +15% | Adjusted measured | Ungraded anchor | Later sale / bid |")
    print("|---|---|---|---|---|---|---|---|---|")

    def band(b):
        return f"${b['low']}-{b['high']}" if b["high"] else (b["flag"] or "no comps")
    for r in fb:
        tr = ", ".join(f"{g:g}:{p:.0f}" for g, p in r["train"]) or "none"
        later = "; ".join(f"{g:g} ${p:.2f} {d}" for g, p, d in r["later"])
        bids = "; ".join(f"{b['status']} {b['winning_bid']}" for b in r["bids"])
        print(f"| {r['book']} ({r['year']}{', ' + r['variant'] if r['variant'] else ''}) | "
              f"{r['grade']:g} | {r['flag']} | {tr} | {band(r['current'])} | {band(r['adj15'])} | "
              f"{band(r['adj_meas'])} | {r['anchor'] or '-'} | {later or bids or '-'} |")
    priced15 = sum(1 for r in fb if r["adj15"]["high"])
    print(f"\nfresh batch priced by adjusted +15% from ledger comps: {priced15}/{len(fb)}")
    conn.close()

    if "--rows-json" in sys.argv:
        path = sys.argv[sys.argv.index("--rows-json") + 1]
        with open(path, "w") as fh:
            json.dump({"stats": stats, "slopes": slope_rows, "pooled": pooled,
                       "table": table, "by_era": by_era, "by_price": by_price, "by_price_accepted": by_price_acc,
                       "same_rows": same,
                       "paired_accepted": pa, "decision_gap": gap, "fresh_batch": fb}, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
