#!/usr/bin/env python3
"""BUI-1233: slab label and page-quality premiums from PriceCharting history.
DIAGNOSTIC ONLY, read-only. No production code is touched.

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-11-slab-label-pq-premiums.py

Source: the BUI-1229 test set (`test_set.json` keeps every graded row with
`label`, `page_quality`, `exclude_code`, `price_outlier`, so no re-parse is
needed). Rows used: CGC, `exclude_code is None`, `price_outlier is not True`.

Model (within-cell, so grade and date mix cannot leak in):
    ln(price) = cell(book, grade, WINDOW_DAYS-day bin [, venue]) + sum_k b_k * D_k
Each cell is demeaned, so only variation WITHIN a book at one grade in one
window identifies b_k. multiplier = exp(b_k). Two regressions:
  * page quality: universal-label rows only, reference = `ow` (off-white),
    dummies white / ow_w / c_ow / cream / unknown (unknown is not a quality).
  * label: all rows, reference = universal label, dummies for qualified,
    restored (restored + conserved), signature_series, with page-quality dummies
    (universal rows, by their quality) as controls.
CIs: 95% percentile bootstrap resampling BOOKS (2000 reps, seed 1233; a book
drawn twice just doubles its weight). A coefficient is reported only with
>= MIN_BOOKS books and >= MIN_ROWS treated rows in cells where the treatment
varies; otherwise it is DROPPED as too thin.
Basis: PC `price` (the accepted amount on best-offer sales); the list-price
basis (`ask` when present) is printed as a sensitivity. Venue: pooled, eBay
only, and venue fixed effects.
"""

from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pc_slab_test_set as T  # noqa: E402

WINDOW_DAYS, REPS, SEED = 90, 2000, 1233
MIN_BOOKS, MIN_ROWS = 5, 20
EPOCH = date(2020, 1, 1)
PQ_TERMS = ["white", "ow_w", "c_ow", "cream", "unknown"]  # ref: ow
LABEL_TERMS = ["qualified", "restored", "signature_series"]  # ref: universal
LABEL_MAP = {"conserved": "restored"}


def solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-9:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit(rows: list[dict], terms: list[str], tkey: str, cellkey) -> dict[str, float] | None:
    """Within-cell OLS of ln(y) on treatment dummies. Returns {term: coef}."""
    cells = defaultdict(list)
    for r in rows:
        cells[cellkey(r)].append(r)
    xs, ys = [], []
    for grp in cells.values():
        if len(grp) < 2:
            continue
        my = sum(r["ly"] for r in grp) / len(grp)
        mx = [sum(r[tkey] == t for r in grp) / len(grp) for t in terms]
        for r in grp:
            xs.append([(r[tkey] == t) - mx[i] for i, t in enumerate(terms)])
            ys.append(r["ly"] - my)
    live = [i for i in range(len(terms)) if any(abs(x[i]) > 1e-12 for x in xs)]
    if not live:
        return None
    a = [[sum(x[i] * x[j] for x in xs) for j in live] for i in live]
    b = [sum(x[i] * y for x, y in zip(xs, ys)) for i in live]
    sol = solve(a, b)
    return None if sol is None else {terms[i]: s for i, s in zip(live, sol)}


def support(rows: list[dict], term: str, tkey: str, cellkey) -> tuple[int, int, int]:
    """(treated rows, books, other rows) in cells where the treatment varies."""
    cells = defaultdict(list)
    for r in rows:
        cells[cellkey(r)].append(r)
    n, ref = 0, 0
    bk = set()
    for grp in cells.values():
        t = [r for r in grp if r[tkey] == term]
        if t and len(t) < len(grp):
            n += len(t)
            ref += len(grp) - len(t)
            bk.update(r["comic_id"] for r in t)
    return n, len(bk), ref


def estimate(rows, terms, tkey, cellkey, only=None) -> dict[str, dict]:
    pt = fit(rows, terms, tkey, cellkey) or {}
    by_book = defaultdict(list)
    for r in rows:
        by_book[r["comic_id"]].append(r)
    books = list(by_book)
    rng = random.Random(SEED)
    reps = defaultdict(list)
    if books and pt:
        for _ in range(REPS):
            pick = [r for _ in books for r in by_book[rng.choice(books)]]
            f = fit(pick, terms, tkey, cellkey)
            if f:
                for k, v in f.items():
                    reps[k].append(v)
    out = {}
    for t in (only or terms):
        n, nb, nref = support(rows, t, tkey, cellkey)
        d = {"n": n, "books": nb, "ref": nref}
        if t in pt and n >= MIN_ROWS and nb >= MIN_BOOKS and len(reps[t]) > REPS * 0.8:
            rs = sorted(reps[t])
            d.update(mult=math.exp(pt[t]), lo=math.exp(rs[int(0.025 * len(rs))]),
                     hi=math.exp(rs[int(0.975 * len(rs)) - 1]))
        out[t] = d
    return out


def fmt(d: dict) -> str:
    base = f"n={d['n']} books={d['books']} ref={d['ref']}"
    if "mult" not in d:
        return base + " | DROPPED (thin)"
    return f"{base} | x{d['mult']:.3f} [{d['lo']:.3f}, {d['hi']:.3f}]"


def tier(p: float) -> str:
    return "<$100" if p < 100 else ("$100-400" if p < 400 else ">=$400")


def prep(ts: dict, basis: str) -> list[dict]:
    books = {b["comic_id"]: b for b in ts["books"]}
    rows = []
    for s in ts["sales"]:
        if s["certifier"] != "cgc" or s["exclude_code"] or s["price_outlier"] is True:
            continue
        p = (s["ask"] or s["price"]) if basis == "list" else s["price"]
        if not p or p <= 0:
            continue
        d = date.fromisoformat(s["sold_date"])
        label = LABEL_MAP.get(s["label"], s["label"])
        rows.append({
            "comic_id": s["comic_id"], "era": books[s["comic_id"]]["era"], "grade": s["grade"],
            "bin": (d - EPOCH).days // WINDOW_DAYS, "source": s["source"], "ly": math.log(p),
            "price": p, "pq": s["page_quality"], "label": label,
            # label regression treatment: non-universal label, else the page quality
            "lp": label if label != "universal" else "pq:" + s["page_quality"],
        })
    mid = defaultdict(list)
    for r in rows:
        mid[(r["comic_id"], r["grade"])].append(r["price"])
    for r in rows:
        r["tier"] = tier(median(mid[(r["comic_id"], r["grade"])]))
    return rows


def main() -> int:
    ts = T.load_test_set()
    cell = lambda r: (r["comic_id"], r["grade"], r["bin"])  # noqa: E731
    cell_v = lambda r: (r["comic_id"], r["grade"], r["bin"], r["source"])  # noqa: E731
    pq_ctl = ["pq:white", "pq:ow_w", "pq:c_ow", "pq:cream", "pq:unknown"]

    for basis in ("accepted", "list"):
        rows = prep(ts, basis)
        uni = [r for r in rows if r["label"] == "universal"]
        print(f"\n##### basis={basis}: {len(rows)} CGC rows, {len(uni)} universal, "
              f"{len({r['comic_id'] for r in rows})} books; labels "
              + ", ".join(f"{k}={sum(r['label'] == k for r in rows)}"
                          for k in ("universal", "qualified", "restored", "signature_series")))
        groups = [("ALL", rows)]
        if basis == "accepted":
            groups += [(f"era={e}", [r for r in rows if r["era"] == e])
                       for e in ("silver", "bronze", "copper", "modern")]
            groups += [(f"tier={t}", [r for r in rows if r["tier"] == t])
                       for t in ("<$100", "$100-400", ">=$400")]
        for name, allr in groups:
            unir = [r for r in allr if r["label"] == "universal"]
            print(f"\n== {name}  (rows={len(allr)}, books={len({r['comic_id'] for r in allr})})")
            res = estimate(unir, PQ_TERMS, "pq", cell, only=["white", "ow_w", "c_ow", "cream", "unknown"])
            for t, d in res.items():
                print(f"  pq {t:<8} vs ow : {fmt(d)}")
            if name == "ALL" and basis == "accepted":
                for vn, vr, ck in (("ebay only", [r for r in unir if r["source"] == "ebay"], cell),
                                   ("venue FE", unir, cell_v)):
                    for t, d in estimate(vr, PQ_TERMS, "pq", ck, only=["white", "ow_w", "c_ow"]).items():
                        print(f"  pq {t:<8} vs ow [{vn}]: {fmt(d)}")
            lab = estimate(allr, LABEL_TERMS + pq_ctl, "lp", cell, only=LABEL_TERMS)
            for t, d in lab.items():
                print(f"  label {t:<16} vs universal: {fmt(d)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
