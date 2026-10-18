#!/usr/bin/env python3
"""BUI-1226: score the PriceCharting append option against the POST-SHIP slab baseline.
DIAGNOSTIC ONLY, read-only. No production code is touched.

    uv run --project plugins/gixen-overlay python docs/audit/2026-10-18-slab-fmv-option-ranking.py \\
        [--json out.json]

Rows. The BUI-1229 baseline rows unchanged: every PC CGC universal sale dated
2026-08-01..2026-10-10, `price_outlier` rows dropped, priced as of the day before
with `fmv_math.graded_fmv` imported live (so BUI-1242's published band is in).
Truth is PC's SOLD price (the accepted offer on best-offer sales).

Arms (same rows, same truth, paired):
  a0  ledger comps, best-offer flag stripped. Reproduces the BUI-1229 harness,
      which never selected `comps.best_offer` (10-12 rebuild: 0.780).
  a_db ledger comps with the STORED `comps.best_offer` only (what production
      reads today: 1,442 of 1,557 CGC universal slab rows are still NULL).
  a   POST-SHIP BASELINE: stored flag, else the BUI-614 raw-capture
      `bestOfferAccepted` by item id (BUI-1234's "shipped code path" flag).
      `graded_pool` applies the x0.89 scale itself.
  b   a + PC universal sales of the same book dated STRICTLY before the test
      sale, at PC's accepted price (`best_offer` False, so never scaled twice).
      Hidden: the test sale itself and any PC row `same_sale` matches to it.
      Deduped against the ledger pool the BUI-1218 way (a PC row a ledger comp
      `same_sale`-matches is dropped; the ledger copy stays). No extra window:
      `graded_pool` already drops comps past 365 days and halves 91..365. This
      is BUI-1218's definition (it used all prior PC sales), now on 47 books.
  b90 b with PC sales limited to the 90 days before the test sale.
  b_ebay b with eBay-source PC sales only (BUI-1229: Heritage scores worse).
  b_guard b, but a row `a` refuses stays refused unless b prices it `direct`
      (BUI-1218's build guard: PC rungs alone never open ladder rows).
  b_list control: b with each PC best-offer sale at its LIST price, flagged so
      `graded_pool` scales it x0.89 like a ledger comp. Tests whether the gain
      is depth or PC's accepted-price basis matching PC truth.

Ledger-truth check (L_a, L_b). The truth is not PC: each sold-comps.com ledger
sale in the window whose best-offer flag is False (so its price is the real
transaction) is priced by `a` and `b` as of the day before. Hidden: the sale's
own row, ledger rows with the same grade + date + price, and every PC row
`same_sale` matches to it.

Score: Winkler (alpha 0.5) / sale price, in-band, signed bias, as BUI-1229.
CIs: 95% percentile bootstrap over BOOKS (2000 reps, seed 1229), each replicate
the sale-weighted mean over resampled books. Paired deltas use sales both arms
price. Drop-top-book: the book whose summed paired delta is most negative is
removed and the paired delta re-bootstrapped. Opens the DB with mode=ro.
"""

from __future__ import annotations

import glob
import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
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
ARMS = ("a0", "a_db", "a", "b", "b90", "b_ebay", "b_guard", "b_list")


def load_capture_flags() -> dict[str, bool]:
    """item id -> bestOfferAccepted from the BUI-614 raw capture (as BUI-1234)."""
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


def load_ledger(conn: sqlite3.Connection, cid: int, cap: dict[str, bool]) -> list[dict]:
    """T.load_ledger plus the `comps.best_offer` column it never selects."""
    rows = T.load_ledger(conn, cid)
    stored = {}
    for pid, bo in conn.execute(
            """SELECT product_id, best_offer FROM comps WHERE comic_id=? AND pool='slab'
               AND certifier='cgc' AND label='universal' AND excluded_code IS NULL""", (cid,)):
        if bo is not None:
            stored[pid] = stored.get(pid) or bool(bo)
    for r in rows:
        db = stored.get(r["product_id"])
        r["_bo_db"] = db is True
        r["_bo"] = db if db is not None else (cap.get(str(r["product_id"])) is True)
    return rows


def price(comps: list[dict], grade: float, as_of: date, pq: str) -> dict:
    return fmv_math.graded_fmv(comps, grade, certifier="cgc", label="universal", as_of=as_of,
                               page_quality=None if pq == "unknown" else pq)


def score(res: dict, y: float) -> dict | None:
    lo, hi = res.get("fmv_low"), res.get("fmv_high")
    if lo is None or hi is None or not y:
        return None
    return {"winkler": winkler_score(lo, hi, y, DEFAULT_ALPHA) / y, "hit": lo <= y <= hi,
            "bias": ((lo + hi) / 2 - y) / y, "basis": res.get("pricing_basis")}


def boot(by_book: dict, seed: int = SEED) -> tuple[float, float]:
    rng, books, reps = random.Random(seed), list(by_book), []
    for _ in range(REPS):
        vals = [v for _ in books for v in by_book[rng.choice(books)]]
        reps.append(mean(vals))
    reps.sort()
    return reps[int(0.025 * REPS)], reps[int(0.975 * REPS) - 1]


def summarize(rows: list[dict], key: str) -> str:
    sc = [r for r in rows if r[key]]
    if not sc:
        return f"n={len(rows)} priced=0"
    parts = [f"n={len(rows)} priced={len(sc)} ({len(sc) / len(rows):.0%}) books={len({r['comic_id'] for r in sc})}"]
    for m, f in (("winkler", ".3f"), ("hit", ".0%"), ("bias", "+.3f")):
        bb = defaultdict(list)
        for r in sc:
            bb[r["comic_id"]].append(float(r[key][m]))
        lo, hi = boot(bb)
        parts.append(f"{m} {mean(v for vs in bb.values() for v in vs):{f}} [{lo:{f}}, {hi:{f}}]")
    return " | ".join(parts)


def paired(rows: list[dict], key: str, ref: str, drop: set[int] = frozenset()) -> dict:
    bb = defaultdict(list)
    for r in rows:
        if r[key] and r[ref] and r["comic_id"] not in drop:
            bb[r["comic_id"]].append(r[key]["winkler"] - r[ref]["winkler"])
    vals = [v for vs in bb.values() for v in vs]
    if not vals:
        return {"n": 0}
    per_book = {c: mean(vs) for c, vs in bb.items()}
    eps = 1e-9
    return {"n": len(vals), "books": len(bb), "delta": mean(vals), "ci": boot(bb),
            "ref_w": mean(r[ref]["winkler"] for r in rows if r[key] and r[ref] and r["comic_id"] not in drop),
            "key_w": mean(r[key]["winkler"] for r in rows if r[key] and r[ref] and r["comic_id"] not in drop),
            "sale_wins": sum(v < -eps for v in vals), "sale_losses": sum(v > eps for v in vals),
            "book_wins": sum(v < -eps for v in per_book.values()),
            "book_losses": sum(v > eps for v in per_book.values()),
            "sum_by_book": {c: sum(vs) for c, vs in bb.items()}}


def fmt_paired(p: dict) -> str:
    if not p["n"]:
        return "n=0"
    return (f"n={p['n']} books={p['books']} | Winkler {p['ref_w']:.3f} -> {p['key_w']:.3f} | "
            f"delta {p['delta']:+.3f} [{p['ci'][0]:+.3f}, {p['ci'][1]:+.3f}] | sales better/worse "
            f"{p['sale_wins']}/{p['sale_losses']} | books better/worse {p['book_wins']}/{p['book_losses']}")


def main() -> int:
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    ts = T.load_test_set()
    books = {b["comic_id"]: b for b in ts["books"]}
    cap = load_capture_flags()
    conn = sqlite3.connect(f"file:{T.DB}?mode=ro", uri=True)
    rows: list[dict] = []
    ltruth: list[dict] = []
    flag_n = defaultdict(int)

    for cid, b in books.items():
        ledger = load_ledger(conn, cid, cap)
        flag_n["ledger"] += len(ledger)
        flag_n["db_true"] += sum(c["_bo_db"] for c in ledger)
        flag_n["any_true"] += sum(bool(c["_bo"]) for c in ledger)
        pcs = [{**s, "_date": date.fromisoformat(s["sold_date"])} for s in T.universal_sales(ts, cid)]
        for s in pcs:
            if not (WIN_FROM <= s["sold_date"] <= WIN_TO) or s["price_outlier"] is True:
                continue
            d, y, g, pq = s["_date"], s["price"], s["grade"], s["page_quality"]
            as_of = d - timedelta(days=1)
            led = [c for c in ledger if c["_date"] and c["_date"] < d and not T.same_sale(s, c)]
            pool_a0 = [{**c, "best_offer": False} for c in led]
            pool_db = [{**c, "best_offer": c["_bo_db"]} for c in led]
            pool_a = [{**c, "best_offer": bool(c["_bo"])} for c in led]
            add = []
            for p in pcs:
                if p["_date"] >= d or p["product_id"] == s["product_id"] or T.same_sale(s, p):
                    continue
                if any(T.same_sale(p, c) for c in led):
                    continue
                add.append({"provider": "pricecharting", "product_id": p["product_id"],
                            "title": p["title"], "price": p["price"], "sold_date": p["sold_date"],
                            "_date": p["_date"], "grade": p["grade"], "page_quality": p["page_quality"],
                            "best_offer": False, "source": p["source"], "ask": p["ask"]})
            add90 = [p for p in add if (d - p["_date"]).days <= 90]
            adde = [p for p in add if p["source"] == "ebay"]
            addl = [{**p, "price": p["ask"], "best_offer": True} if p["ask"] else p for p in add]
            res = {"a0": price(pool_a0, g, as_of, pq), "a_db": price(pool_db, g, as_of, pq),
                   "a": price(pool_a, g, as_of, pq), "b": price(pool_a + add, g, as_of, pq),
                   "b90": price(pool_a + add90, g, as_of, pq), "b_ebay": price(pool_a + adde, g, as_of, pq),
                   "b_list": price(pool_a + addl, g, as_of, pq)}
            row = {"comic_id": cid, "book": f"{b['series']} #{b['issue']}", "era": b["era"],
                   "date": s["sold_date"], "grade": g, "y": y, "n_ledger": len(led), "n_pc": len(add),
                   "reason_a": res["a"].get("flag_reason"), "reason_b": res["b"].get("flag_reason")}
            for k, r in res.items():
                row[k] = score(r, y)
            row["b_guard"] = row["b"] if (row["a"] or (row["b"] and row["b"]["basis"] == "direct")) else None
            rows.append(row)

        # ledger truth: unflagged sold-comps.com sales in the window
        for t in ledger:
            if (t["provider"] != "sold-comps.com" or not t["sold_date"] or t["_bo"]
                    or not (WIN_FROM <= t["sold_date"] <= WIN_TO)):
                continue
            d, y, g = t["_date"], t["price"], t["grade"]
            pool = [{**c, "best_offer": bool(c["_bo"])} for c in ledger
                    if c["_date"] and c["_date"] < d and c["product_id"] != t["product_id"]
                    and not (c["grade"] == g and c["sold_date"] == t["sold_date"] and c["price"] == y)]
            add = []
            for p in pcs:
                if p["_date"] >= d or T.same_sale(p, t) or any(T.same_sale(p, c) for c in pool):
                    continue
                add.append({"provider": "pricecharting", "product_id": p["product_id"],
                            "price": p["price"], "sold_date": p["sold_date"], "_date": p["_date"],
                            "grade": p["grade"], "page_quality": p["page_quality"],
                            "best_offer": False})
            as_of = d - timedelta(days=1)
            ltruth.append({"comic_id": cid, "y": y,
                           "L_a": score(price(pool, g, as_of, t["page_quality"]), y),
                           "L_b": score(price(pool + add, g, as_of, t["page_quality"]), y)})

    m = ts["meta"]
    print(f"Test set built {m['built']}: {m['n_books']} books, {m['n_universal']} CGC universal sales.")
    print(f"Ledger CGC universal slab comps on these books: {flag_n['ledger']}; best-offer flag stored "
          f"True on {flag_n['db_true']}, True after capture fill on {flag_n['any_true']}.")
    print(f"Window {WIN_FROM}..{WIN_TO}, outliers dropped: {len(rows)} sales, "
          f"{len({r['comic_id'] for r in rows})} books. Bootstrap {REPS} reps over books, seed {SEED}.\n")

    print("ARMS (each on the sales it prices)")
    for k in ARMS:
        print(f"  {k:<8}:", summarize(rows, k))
    eb = [r for r in rows if r["b"] is not None or r["a"] is not None]
    print(f"  median PC sales appended per test sale: {median(r['n_pc'] for r in rows):.0f}"
          f" (ledger comps: {median(r['n_ledger'] for r in rows):.0f}); rows scored either way: {len(eb)}")

    print("\nPAIRED (Winkler delta, negative = better; sales both arms price)")
    out = {}
    for k, ref in (("a_db", "a0"), ("a", "a0"), ("a", "a_db"), ("b", "a"), ("b90", "a"),
                   ("b_ebay", "a"), ("b_guard", "a"), ("b_list", "a")):
        p = paired(rows, k, ref)
        out[f"{k}-{ref}"] = p
        print(f"  {k:<7} vs {ref:<5}:", fmt_paired(p))

    print("\nDROP-TOP-BOOK (the book with the most negative summed paired delta removed)")
    for k in ("b", "b90", "b_ebay"):
        p = out[f"{k}-a"]
        top = min(p["sum_by_book"], key=p["sum_by_book"].get)
        share = p["sum_by_book"][top] / sum(p["sum_by_book"].values())
        q = paired(rows, k, "a", drop={top})
        print(f"  {k:<7}: drop {books[top]['series']} #{books[top]['issue']} ({share:.0%} of the summed gain):",
              fmt_paired(q))

    p = out["b-a"]
    top2 = set(sorted(p["sum_by_book"], key=p["sum_by_book"].get)[:2])
    print(f"  b      : drop top two ({', '.join(books[c]['series'] + ' #' + books[c]['issue'] for c in top2)}):",
          fmt_paired(paired(rows, "b", "a", drop=top2)))
    worst = max((paired(rows, "b", "a", drop={c})["ci"][1], c) for c in p["sum_by_book"])
    print(f"  b      : leave-one-book-out, least favourable CI upper bound {worst[0]:+.3f}"
          f" (dropping {books[worst[1]]['series']} #{books[worst[1]]['issue']})")

    print("\nLEDGER TRUTH (unflagged sold-comps.com sales in the window; truth is not PC)")
    print("  L_a     :", summarize(ltruth, "L_a"))
    print("  L_b     :", summarize(ltruth, "L_b"))
    lp = paired(ltruth, "L_b", "L_a")
    print("  L_b vs L_a:", fmt_paired(lp))
    if lp["n"]:
        top = min(lp["sum_by_book"], key=lp["sum_by_book"].get)
        print(f"  drop {books[top]['series']} #{books[top]['issue']}:", fmt_paired(paired(ltruth, "L_b", "L_a", drop={top})))
    print(f"  newly priced {sum(1 for r in ltruth if not r['L_a'] and r['L_b'])}, priced->refused "
          f"{sum(1 for r in ltruth if r['L_a'] and not r['L_b'])}")

    print("\nCOVERAGE CHANGES vs a")
    for k in ("b", "b90", "b_ebay", "b_guard"):
        newly = [r for r in rows if not r["a"] and r[k]]
        lost = [r for r in rows if r["a"] and not r[k]]
        line = f"  {k:<7}: newly priced {len(newly)}"
        if newly:
            line += (f" (Winkler {mean(r[k]['winkler'] for r in newly):.3f}, in-band "
                     f"{mean(r[k]['hit'] for r in newly):.0%}, bias {mean(r[k]['bias'] for r in newly):+.3f},"
                     f" {len({r['comic_id'] for r in newly})} books)")
        print(line + f"; priced->refused {len(lost)}")

    print("\nPER BOOK (b vs a, paired mean Winkler delta, sales)")
    p = out["b-a"]
    per = sorted(p["sum_by_book"].items(), key=lambda kv: kv[1])
    for c, s in per:
        n = sum(1 for r in rows if r["comic_id"] == c and r["a"] and r["b"])
        print(f"  {books[c]['series']} #{books[c]['issue']:<5} {books[c]['era']:<7} n={n:>3} mean {s / n:+.3f} sum {s:+.2f}")

    print("\nBY ERA (b vs a)")
    for e in ("silver", "bronze", "copper", "modern"):
        sub = [r for r in rows if r["era"] == e]
        print(f"  {e:<7}:", fmt_paired(paired(sub, "b", "a")))

    if out_json:
        Path(out_json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
