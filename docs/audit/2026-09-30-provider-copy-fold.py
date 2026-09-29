#!/usr/bin/env python3
"""Test the premise of folding "ungraded provider copies" into their graded
twins (BUI-1020). DIAGNOSTIC ONLY, read-only.

BUI-1014 dropped 187 grade-less raw ledger rows from its `ceiling` arm as
provider copies of a graded held-out sale: same book, price within $0.01,
sold within the 7 days before the sale. This script reproduces that count
with BUI-1014's own loaders and pin, then asks whether they are copies at all:

  1. Identity evidence: does the pair share a product_id, a title, or come
     from two different providers (a cross-provider copy needs both providers)?
  2. Chance control: the price-match rate between a book's grade-less rows and
     its graded sales, by lag in 7-day bins. A real copy class piles up at a
     short lag; a coincidence class is flat across lags.
  3. The same control for the graded-graded near-duplicate rule the BUI-1005
     harness applies in `training` (same grade, price within $0.01, 7 days).
  4. Production effect of the fold: every held-out sale on an affected book
     priced by the production `compute_fmv` with the book's grade-less comps
     passed as grade=None (what a live fetch hands it), with and without the
     matched rows. Bands, flags, and Winkler/price are compared; the
     ungraded anchor (flag-only) is reported separately.

    uv run --project plugins/gixen-overlay python \\
        docs/audit/2026-09-30-provider-copy-fold.py

Opens ~/.comics-server/db.sqlite with mode=ro through BUI-1014's `connect`.
Nothing is written and no provider is called.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "ob1014", ROOT / "docs" / "audit" / "2026-09-29-grade-oracle-bound.py")
O = importlib.util.module_from_spec(_spec)
sys.modules["ob1014"] = O
_spec.loader.exec_module(O)
H = O.H                      # the BUI-1005 harness, imported by BUI-1014
fmv_math = H.fmv_math

LAG_BINS = [(1, 7)] + [(lo, lo + 6) for lo in range(8, 85, 7)]


def is_match(u: dict, c: dict) -> bool:
    """BUI-1014 `ceiling`'s rule, verbatim: grade-less u sold in the 7 days
    before graded c, price within $0.01."""
    return (u["d"] < c["d"] and abs(u["price"] - c["price"]) < 0.01
            and (c["d"] - u["d"]).days <= 7)


def norm_title(s: str | None) -> str:
    return " ".join((s or "").split()).casefold()


def lag_rates(pairs_iter, cond) -> list[tuple[str, int, int, float]]:
    counts = {b: [0, 0] for b in LAG_BINS}
    for a, b in pairs_iter:
        lag = (b["d"] - a["d"]).days
        for lo, hi in LAG_BINS:
            if lo <= lag <= hi:
                counts[(lo, hi)][0] += 1
                counts[(lo, hi)][1] += cond(a, b)
                break
    return [(f"{lo}-{hi}", n, m, m / n if n else 0.0) for (lo, hi), (n, m) in counts.items()]


def main() -> int:
    conn = O.connect()
    comps, _ = O.load_graded(conn)
    ung, _ = O.load_ungraded(conn)
    meta = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, provider, product_id, title FROM comps")}
    conn.close()

    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    ub: dict[int, list[dict]] = defaultdict(list)
    for u in ung:
        ub[u["comic_id"]].append(u)

    # ── 1. Reproduce and inspect the matched pairs ────────────────────────
    pairs = [(u, c) for c in comps for u in ub.get(c["comic_id"], []) if is_match(u, c)]
    urows = {u["id"] for u, _ in pairs}
    books = {c["comic_id"] for _, c in pairs}
    print(f"graded {len(comps)}  grade-less {len(ung)}  (BUI-1014 loaders, AS_OF {O.AS_OF})")
    print(f"matched pairs {len(pairs)}  distinct grade-less rows {len(urows)}  books {len(books)}")
    prov = Counter((meta[u['id']]['provider'], meta[c['id']]['provider']) for u, c in pairs)
    same_pid = sum(meta[u["id"]]["product_id"] == meta[c["id"]]["product_id"] for u, c in pairs)
    same_title = sum(norm_title(meta[u["id"]]["title"]) == norm_title(meta[c["id"]]["title"])
                     for u, c in pairs)
    print(f"providers (grade-less, graded): {dict(prov)}")
    print(f"same product_id: {same_pid}/{len(pairs)}   same title: {same_title}/{len(pairs)}")
    print(f"top prices: {Counter(round(c['price'], 2) for _, c in pairs).most_common(6)}")

    # ── 2. Chance control: grade-less vs graded price-match rate by lag ────
    def ug_pairs():
        for c in comps:
            for u in ub.get(c["comic_id"], []):
                if u["d"] < c["d"]:
                    yield u, c
    print("\ngrade-less vs graded, same book: price-match rate by lag (days)")
    rows = lag_rates(ug_pairs(), lambda u, c: abs(u["price"] - c["price"]) < 0.01)
    for lab, n, m, r in rows:
        print(f"  {lab:>6}  pairs {n:6d}  matches {m:4d}  rate {r:.2%}")
    base_n = sum(n for lab, n, _, _ in rows[1:])
    base_m = sum(m for lab, _, m, _ in rows[1:])
    expected = rows[0][1] * base_m / base_n
    print(f"  lag 1-7 observed {rows[0][2]} vs expected at the 8-84 day rate "
          f"{expected:.0f} (excess {rows[0][2] - expected:+.0f})")

    # ── 3. Same control for the harness's graded near-duplicate rule ───────
    def gg_pairs():
        for cs in by_book.values():
            for a in cs:
                for b in cs:
                    if a is not b and a["d"] < b["d"]:
                        yield a, b
    print("\ngraded vs graded, same book: same-grade AND price-match rate by lag")
    rows_g = lag_rates(gg_pairs(), lambda a, b: a["grade"] == b["grade"]
                       and abs(a["price"] - b["price"]) < 0.01)
    for lab, n, m, r in rows_g[:4]:
        print(f"  {lab:>6}  pairs {n:6d}  matches {m:4d}  rate {r:.3%}")
    bn = sum(n for _, n, _, _ in rows_g[1:])
    bm = sum(m for _, _, m, _ in rows_g[1:])
    print(f"  lag 1-7 observed {rows_g[0][2]} vs expected {rows_g[0][1] * bm / bn:.0f}")

    # ── 4. Production effect of the fold on affected books ────────────────
    diff_band = diff_flag = n_sales = anchor_moved = diverge_flip = 0
    w_before, w_after = [], []
    for c in comps:
        if c["comic_id"] not in books:
            continue
        train, _ = H.training(by_book[c["comic_id"]], c)
        if not any(abs(t["grade"] - c["grade"]) <= fmv_math.MAX_GRADE_WINDOW for t in train):
            continue                       # outside the BUI-1005 population
        lo = c["d"] - timedelta(days=H.WINDOW_DAYS)
        uw = [u for u in ub[c["comic_id"]] if lo <= u["d"] < c["d"]]
        base = [H.as_comp(t) for t in train]
        before = fmv_math.compute_fmv(
            base + [{"price": u["price"], "grade": None, "sold_date": u["sold_date"]} for u in uw],
            c["grade"])
        after = fmv_math.compute_fmv(
            base + [{"price": u["price"], "grade": None, "sold_date": u["sold_date"]}
                    for u in uw if u["id"] not in urows],
            c["grade"])
        n_sales += 1
        if (before["fmv_low"], before["fmv_high"], before["median"]) != \
                (after["fmv_low"], after["fmv_high"], after["median"]):
            diff_band += 1
        if before["flag_reason"] != after["flag_reason"]:
            diff_flag += 1
        if before["ungraded_anchor"] != after["ungraded_anchor"]:
            anchor_moved += 1
        if before["anchor_diverges"] != after["anchor_diverges"]:
            diverge_flip += 1
        if before["fmv_low"] is not None:
            w_before.append(H.winkler_score(before["fmv_low"], before["fmv_high"], c["price"],
                                            H.DEFAULT_ALPHA) / c["price"])
        if after["fmv_low"] is not None:
            w_after.append(H.winkler_score(after["fmv_low"], after["fmv_high"], c["price"],
                                           H.DEFAULT_ALPHA) / c["price"])
    print(f"\nfold on affected books: {n_sales} held-out sales")
    print(f"  bands changed {diff_band}  flags changed {diff_flag}  "
          f"ungraded anchor changed {anchor_moved}  anchor_diverges flipped {diverge_flip}")
    if w_before:
        print(f"  median W/price before {median(w_before):.3f} (n={len(w_before)})  "
              f"after {median(w_after):.3f} (n={len(w_after)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
