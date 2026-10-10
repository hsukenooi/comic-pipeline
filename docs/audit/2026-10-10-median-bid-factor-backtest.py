#!/usr/bin/env python3
"""Backtest a median-anchored bid cap stepped by confidence (BUI-1219 phase 1).
DIAGNOSTIC ONLY, read-only, no production change.

Question: today max_bid = 0.80 x fmv_high (0.60 x fmv_high for a section-7
interpolated book; fmv confidence alone never haircuts, only a photo
grade_confidence does, BUI-51). Would `factor x median`, with the factor
stepped by confidence tier, win more books at an acceptable overpay?

Reconstruction (same as BUI-1213's harness, which this imports): no provider
is called. Each held-out sale is priced from the book's graded raw ledger
comps sold in [D - 90, D), D = the sale's date, with the sale itself (and, for
our own auctions, our own listing) left out. `fmv_math.compute_fmv` runs
unchanged and returns the median, fmv_high, the fine confidence label and the
interpolation marker that production would have produced on that pool.

Two populations:
  * LOO: every ledger auction sale with a priced live pool (BUI-1005 shape).
  * OURS: our resolved raw auctions (GET /api/comics/accuracy rows).

Scoring, per tier and per rule (proxy bidding treated as second price):
  * win%      share of sales with cap >= sale price
  * paid/med  mean (price / median - 1) over wins, i.e. overpay against our
              own median (negative = bought below median)
  * +won      sales the rule wins that today's cap loses, with their mean
              (price - today_cap) / median (the real extra spend) and their
              mean price / median - 1
  * lost      sales today's cap wins that the rule loses
  * worst     mean (cap - today_cap) / median over today's wins: the extra a
              book we win anyway could cost if the runner-up bid to our cap
              (0 under pure second price, so this is an upper bound)

Tiers: the fine label compute_fmv assigns (HIGH .. LOW, plus INTERP), the
stored collapse (high / medium = MEDIUM-HIGH+MEDIUM / low = MEDIUM-LOW+LOW),
and candidate "Strong" definitions built only from fields the fmv row stores
(fmv_comps, and the notes tokens window= / cv= / label=).

    uv run --project plugins/gixen-overlay python \\
        docs/audit/2026-10-10-median-bid-factor-backtest.py [--cache DIR]

Reads the comics server over HTTP via `comics-api` only. `--cache DIR` reuses
the raw_comps.json / acc.json / comics.json reads (same names as BUI-1213's).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "ledger_bt", HERE / "2026-10-10-raw-ledger-merge-backtest.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)  # type: ignore[union-attr]
fmv_math = bt.fmv_math  # comps carry no _age_w, so its weight hook is a no-op

FACTORS = (0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 1.00, 1.10)
STORED = {"HIGH": "high", "MEDIUM-HIGH": "medium", "MEDIUM": "medium",
          "MEDIUM-LOW": "low", "LOW": "low"}


def evaluate(book: list[dict], D, grade: float, *, skip=None, exclude_pid=None) -> dict | None:
    live, _ = bt.pools(book, D, skip=skip, exclude_pid=exclude_pid)
    if not live:
        return None
    comps = [bt.as_comp(c, 1.0) for c in live]
    r = fmv_math.compute_fmv(comps, grade)
    if r["flag_reason"] is not None or not r["fmv_high"] or not r["median"]:
        return None
    pool, _ = fmv_math.build_pool(comps, grade)
    return {
        "high": r["fmv_high"], "low": r["fmv_low"], "median": r["median"],
        "label": r["confidence"], "interp": r["interpolated"], "n": r["n"],
        "cv": r["cv"], "window": r["window"], "today": r["max_bid"],
        "n_exact": sum(1 for c in pool if c["grade"] == grade),
        "width": (r["fmv_high"] - r["fmv_low"]) / r["median"],
    }


def tier_fine(e: dict) -> str:
    return "INTERP" if e["interp"] else e["label"]


def tier_stored(e: dict) -> str:
    return "low(interp)" if e["interp"] else STORED[e["label"]]


def strong_defs() -> dict:
    """Candidate Strong rules readable off a STORED row (comps, notes)."""
    return {
        "S1 label=HIGH": lambda e: not e["interp"] and e["label"] == "HIGH",
        "S2 HIGH & window=0.5 & cv<25%": lambda e: (
            not e["interp"] and e["label"] == "HIGH" and e["window"] <= 0.5
            and e["cv"] is not None and e["cv"] < 0.25),
        "S3 S2 & comps>=8": lambda e: (
            not e["interp"] and e["label"] == "HIGH" and e["window"] <= 0.5
            and e["cv"] is not None and e["cv"] < 0.25 and e["n"] >= 8),
    }


def score(rows: list[dict], f: float | None) -> dict:
    """f=None -> today's cap. Otherwise clean_round(f x median)."""
    out = {"n": len(rows), "win": 0, "paid": [], "new": [], "lost": 0, "worst": []}
    for r in rows:
        e, p = r["e"], r["price"]
        today = e["today"]
        cap = today if f is None else fmv_math.clean_round(f * e["median"])
        win, twin = cap >= p, today >= p
        if win:
            out["win"] += 1
            out["paid"].append(p / e["median"] - 1)
        if win and not twin:
            out["new"].append(((p - today) / e["median"], p / e["median"] - 1))
        if twin and not win:
            out["lost"] += 1
        if twin and f is not None:
            out["worst"].append((cap - today) / e["median"])
    return out


def fmt(s: dict) -> str:
    if not s["n"]:
        return "n=0"
    w = 100 * s["win"] / s["n"]
    paid = f"{100 * mean(s['paid']):+.0f}%" if s["paid"] else "  n/a"
    new = (f"+{len(s['new'])} (over today cap {100 * mean(a for a, _ in s['new']):+.0f}%, "
           f"paid/med {100 * mean(b for _, b in s['new']):+.0f}%)" if s["new"] else "+0")
    worst = f"{100 * mean(s['worst']):+.0f}%" if s["worst"] else "-"
    return f"win {w:3.0f}% paid/med {paid} | {new} lost {s['lost']} worst {worst}"


def report(name: str, rows: list[dict], keyfn, order=None) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = keyfn(r["e"])
        if k is not None:
            groups[k].append(r)
    print(f"\n### {name}")
    for k in (order or sorted(groups)):
        g = groups.get(k, [])
        if not g:
            continue
        hm = mean(r["e"]["high"] / r["e"]["median"] for r in g)
        print(f"\n{k}: n={len(g)}  mean high/median={hm:.2f}")
        print(f"  today ({'0.60' if k in ('INTERP', 'low(interp)') else '0.80'} x high): {fmt(score(g, None))}")
        for f in FACTORS:
            print(f"  {f:.2f} x median: {fmt(score(g, f))}")


def policy_hits(rows: list[dict]) -> None:
    """Share of rows where a median cap would trip today's overlay checks:
    recomputed_cap (rung x high; stored high/medium -> 0.80, low -> 0.70,
    policy.py _RUNG_BY_CONFIDENCE) and over_fmv (1.0 x high)."""
    rung = {"high": 0.80, "medium": 0.80, "low": 0.70, "low(interp)": 0.70}
    print("\n### Overlay advisories a median cap would trip (today's policy.py)")
    for t in ("high", "medium", "low"):
        g = [r["e"] for r in rows if tier_stored(r["e"]) == t]
        for f in (0.90, 1.00):
            caps = [(fmv_math.clean_round(f * e["median"]), e) for e in g]
            rc = sum(c > rung[t] * e["high"] for c, e in caps)
            of = sum(c > e["high"] for c, e in caps)
            print(f"- {t} {f:.2f} x median: recomputed_cap {rc}/{len(g)}, over_fmv {of}/{len(g)}")


def population(name: str, rows: list[dict]) -> None:
    print(f"\n## {name}: priced N={len(rows)}")
    fine_order = ["HIGH", "MEDIUM-HIGH", "MEDIUM", "MEDIUM-LOW", "LOW", "INTERP"]
    report("Fine label (as compute_fmv assigns it)", rows, tier_fine, fine_order)
    report("Stored label (collapsed)", rows, tier_stored, ["high", "medium", "low", "low(interp)"])
    for nm, fn in strong_defs().items():
        report(f"Strong candidate {nm}", rows, lambda e, fn=fn: "in" if fn(e) else None)
    policy_hits(rows)
    # Does a stored-field Strong rule capture "several exact-grade sales"?
    for nm, fn in strong_defs().items():
        sel = [r["e"] for r in rows if fn(r["e"])]
        if sel:
            ex = [e["n_exact"] for e in sel]
            print(f"- {nm}: {len(sel)} rows, exact-grade comps median "
                  f"{sorted(ex)[len(ex) // 2]}, share with >=3 exact "
                  f"{100 * sum(x >= 3 for x in ex) / len(ex):.0f}%")


def stored_strong(comics: list[dict]) -> None:
    """How many CURRENT stored raw priced rows fall in each Strong rule."""
    def tok(notes: str, key: str):
        m = re.search(rf"(?:^|\| ){key}=([^ |]+)", notes or "")
        return m.group(1) if m else None
    rows = [c for c in comics if c.get("certifier") in (None, "none")
            and c.get("fmv_high") and not c.get("fmv_flag_reason")]
    es = []
    for c in rows:
        n = c.get("fmv_notes") or ""
        cv = tok(n, "cv")
        win = tok(n, "window")
        es.append({
            "interp": c.get("pricing_basis") == "interpolated" or "interpolated" in n,
            "label": tok(n, "label") or "", "n": c.get("fmv_comps") or 0,
            "cv": float(cv.rstrip("%")) / 100 if cv and cv.endswith("%") else None,
            "window": (float(m.group(1)) if win and (m := re.match(r"±?(\d+(?:\.\d+)?)", win))
                       else 99.0),
            "stored": c.get("fmv_confidence"),
        })
    print(f"\n## Stored raw priced fmv rows now: {len(es)} "
          f"(stored confidence {dict(Counter(e['stored'] for e in es))}; "
          f"notes label {dict(Counter(e['label'] for e in es))})")
    for nm, fn in strong_defs().items():
        print(f"- {nm}: {sum(1 for e in es if e['label'] and fn(e))}")


def main() -> int:
    cache = Path(sys.argv[sys.argv.index("--cache") + 1]) if "--cache" in sys.argv else None
    raw = bt.cached(cache, "raw_comps.json", bt.page_ledger)
    acc = bt.cached(cache, "acc.json", lambda: bt.api("/api/comics/accuracy?days=365&include_rows=true"))
    comics = bt.cached(cache, "comics.json", lambda: bt.api("/api/comics"))
    issues = {c["id"]: c["issue"] for c in comics}
    for r in acc["rows"]:
        issues.setdefault(r["comic_id"], r["issue"])
    comps, stats, _ = bt.load_comps(raw, issues)
    by_book: dict[int, list[dict]] = defaultdict(list)
    for c in comps:
        by_book[c["comic_id"]].append(c)
    print(f"graded raw ledger load: {dict(stats)}")

    loo = []
    for c in comps:
        if c.get("buying_format") != "auction":
            continue
        e = evaluate(by_book[c["comic_id"]], c["d"], c["grade"], skip=c)
        if e:
            loo.append({"price": c["price"], "e": e})

    ours = []
    for a in acc["rows"]:
        if a.get("certifier") not in (None, "none") or a.get("grade") is None or not a.get("price"):
            continue
        e = evaluate(by_book.get(a["comic_id"], []), bt.to_date(a["ended"]), a["grade"],
                     exclude_pid=str(a["item_id"]))
        if e:
            ours.append({"price": float(a["price"]), "e": e, "status": a.get("status")})

    population("LOO ledger auction sales", loo)
    population("Our resolved raw auctions", ours)
    stored_strong(comics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
