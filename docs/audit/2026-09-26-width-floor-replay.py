#!/usr/bin/env python3
"""Replay a minimum FMV band-width floor against the bands in force at bid
time (BUI-982). DIAGNOSTIC ONLY, read-only.

For each resolved auction (BUI-977's selection, via `_fmv_accuracy_rows`), a
band narrower than the floor (width = (high - low) / midpoint) is widened to
exactly the floor around the SAME midpoint; wider bands are left alone. Each
floored band set is scored against the unfloored in-force bands with BUI-979's
`band_comparison` (Winkler, alpha = module default), on three cuts: all rows,
band_source='history', and history bands recorded on or after 2026-07-25
(post-BUI-528).

Money cost ("overpay"): a higher fmv_high raises max_bid proportionally
(max_bid = bid_factor x fmv_high; the replay scales each bid's ACTUAL max_bid
by new_high / old_high, so haircuts and manual caps carry over). eBay proxy
bidding (and a last-second Gixen snipe) means a WON auction's price is set by
the runner-up, not by our max, so a raised max_bid changes nothing on an
auction we already won. The cost lands on LOST auctions whose final price sits
above our real max_bid but at or below the replayed one: those flip to WON. We
would then pay at least the observed final price (the old winner's max is at
least that) and at most the replayed max_bid.

Also checks BUI-528's concern: how often the floored fmv_high, and the
replayed max_bid, sit above the highest real comp. "Highest comp" is the
highest non-excluded comp in the comps ledger for the same book and pool,
within the band's recorded grade window, sold on or before the bid was added.
That is a superset of the pool the band was built from (no recency window),
so the count of floors above it is a LOWER bound.

    uv run --project plugins/gixen-overlay python \\
        docs/audit/2026-09-26-width-floor-replay.py [--rows-json out.json]

Opens ~/.comics-server/db.sqlite with mode=ro. Nothing is written to the DB.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

from gixen_overlay.band_compare import DEFAULT_ALPHA, band_comparison
from gixen_overlay.db import _fmv_accuracy_rows

DB = os.path.expanduser("~/.comics-server/db.sqlite")
FLOORS = (0.30, 0.40)
POST_528 = "2026-07-25"


def floored(low: float, high: float, floor: float) -> tuple[float, float]:
    mid = (low + high) / 2
    if (high - low) / mid >= floor:
        return low, high
    return mid * (1 - floor / 2), mid * (1 + floor / 2)


def load(conn: sqlite3.Connection) -> list[dict]:
    rows = _fmv_accuracy_rows(conn)
    for r in rows:
        # _fmv_accuracy_rows requires exactly one bid_fmvs link, so this join
        # yields one row per bid.
        x = conn.execute(
            """
            SELECT b.max_bid,
                   (SELECT recorded_at FROM fmv_history WHERE id = ?) AS band_recorded_at,
                   COALESCE((SELECT notes FROM fmv_history WHERE id = ?), f.notes) AS band_notes
            FROM bids b
            JOIN bid_fmvs bf ON bf.bid_id = b.id
            JOIN fmv f ON f.id = bf.fmv_id
            WHERE b.id = ?
            """,
            (r["fmv_history_id"], r["fmv_history_id"], r["bid_id"]),
        ).fetchone()
        r.update(dict(x))
        r["max_comp"] = max_comp(conn, r)
    return rows


def max_comp(conn: sqlite3.Connection, r: dict) -> float | None:
    m = re.search(r"window=±([\d.]+)", r["band_notes"] or "")
    if not m:
        return None
    win = float(m.group(1))
    pool = "raw" if r["certifier"] == "none" else "slab"
    row = conn.execute(
        """
        SELECT MAX(price) FROM comps
        WHERE comic_id = ? AND pool = ? AND certifier = ? AND excluded_code IS NULL
          AND grade IS NOT NULL AND grade BETWEEN ? AND ?
          AND price > 0 AND sold_date <= substr(?, 1, 10)
        """,
        (r["comic_id"], pool, r["certifier"], r["grade"] - win, r["grade"] + win,
         r["added_at"]),
    ).fetchone()
    return row[0]


def cuts(rows: list[dict]) -> dict[str, list[dict]]:
    hist = [r for r in rows if r["band_source"] == "history"]
    return {
        "all": rows,
        "history": hist,
        "post-528 (history, band >= 2026-07-25)": [
            r for r in hist if (r["band_recorded_at"] or "") >= POST_528
        ],
    }


def overpay(rows: list[dict], floor: float) -> dict:
    """Flips (LOST -> WON) and exposure on WON rows, under the replayed max_bid."""
    flips = []
    won_raised = 0
    won_added_exposure = 0.0
    for r in rows:
        new_low, new_high = floored(r["low"], r["high"], floor)
        if new_high == r["high"]:
            continue
        new_max = r["max_bid"] * new_high / r["high"]
        if r["status"] == "WON":
            won_raised += 1
            won_added_exposure += new_max - r["max_bid"]
        elif r["status"] == "LOST" and r["max_bid"] < r["price"] <= new_max:
            flips.append((r, new_max, new_high))
    paid_lo = sum(r["price"] for r, _, _ in flips)
    paid_hi = sum(nm for _, nm, _ in flips)
    above_top = [r for r, _, _ in flips if r["price"] > r["high"]]
    return {
        "flips": len(flips),
        "paid_low": paid_lo,
        "paid_high": paid_hi,
        # overpay = dollars paid above the UNFLOORED band top (today's evidence)
        "over_top_low": sum(max(0.0, r["price"] - r["high"]) for r, _, _ in flips),
        "over_top_high": sum(max(0.0, nm - r["high"]) for r, nm, _ in flips),
        "flips_price_above_top": len(above_top),
        "over_max_low": sum(r["price"] - r["max_bid"] for r, _, _ in flips),
        "over_max_high": sum(nm - r["max_bid"] for r, nm, _ in flips),
        "won_raised": won_raised,
        "won_added_exposure": won_added_exposure,
        "flip_ids": [r["bid_id"] for r, _, _ in flips],
    }


def comp_check(rows: list[dict], floor: float) -> dict:
    changed = [r for r in rows if floored(r["low"], r["high"], floor)[1] != r["high"]]
    known = [r for r in changed if r["max_comp"] is not None]
    hi_over = [r for r in known if floored(r["low"], r["high"], floor)[1] > r["max_comp"]]
    mb_over = [
        r for r in known
        if r["max_bid"] * floored(r["low"], r["high"], floor)[1] / r["high"] > r["max_comp"]
    ]
    base_hi_over = [r for r in known if r["high"] > r["max_comp"]]
    return {"changed": len(changed), "known": len(known), "high_over": len(hi_over),
            "max_bid_over": len(mb_over), "base_high_over": len(base_hi_over)}


def fmt(x, p=0, suf=""):
    return "-" if x is None else f"{x:.{p}f}{suf}"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = load(conn)
    out: dict = {"alpha": DEFAULT_ALPHA, "cuts": {}}
    print(f"alpha={DEFAULT_ALPHA}, resolved rows={len(rows)}")
    for cut_name, cut_rows in cuts(rows).items():
        base = {r["bid_id"]: (r["low"], r["high"]) for r in cut_rows}
        print(f"\n## {cut_name}: n={len(cut_rows)}\n")
        print("| Set | Changed | Median W/price | Median W | ±10% | ±20% | MdAPE | In | Above | Below | Width | Better/worse/tie vs base |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|")
        cut_out = {}
        for floor in (None, *FLOORS):
            fl = base if floor is None else {
                k: floored(lo, hi, floor) for k, (lo, hi) in base.items()
            }
            res = band_comparison(conn, base, fl, labels=("base", "floor"))
            m = res["floor"]
            changed = sum(fl[k] != base[k] for k in base)
            p = res["paired"]
            name = "base" if floor is None else f"floor {floor:.0%}"
            print(f"| {name} | {changed} | {fmt(m['winkler_scaled_median'], 3)} | "
                  f"${fmt(m['winkler_median'], 2)} | {fmt(m['share_within_10pct'], 1, '%')} | "
                  f"{fmt(m['share_within_20pct'], 1, '%')} | {fmt(m['mdape_pct'], 1, '%')} | "
                  f"{fmt(m['in_band_pct'], 1, '%')} | {fmt(m['above_band_pct'], 1, '%')} | "
                  f"{fmt(m['below_band_pct'], 1, '%')} | {fmt(m['median_band_width_pct'], 0, '%')} | "
                  f"{p['floor_better']}/{p['base_better']}/{p['ties']} |")
            cut_out[name] = {"metrics": m, "paired": p, "changed": changed}
            # The same comparison restricted to the rows the floor changed.
            if floor is not None:
                sub_b = {k: base[k] for k in base if fl[k] != base[k]}
                sub_f = {k: fl[k] for k in sub_b}
                sr = band_comparison(conn, sub_b, sub_f, labels=("base", "floor"))
                cut_out[name]["changed_only"] = {
                    "base": sr["base"], "floor": sr["floor"], "paired": sr["paired"]}
        for floor in FLOORS:
            name = f"floor {floor:.0%}"
            c = cut_out[name]["changed_only"]
            print(f"- {name}, changed rows only (n={c['floor']['n']}): median W/price "
                  f"{fmt(c['base']['winkler_scaled_median'], 3)} -> "
                  f"{fmt(c['floor']['winkler_scaled_median'], 3)}, mean "
                  f"{fmt(c['base']['winkler_scaled_mean'], 3)} -> "
                  f"{fmt(c['floor']['winkler_scaled_mean'], 3)}, in band "
                  f"{fmt(c['base']['in_band_pct'], 1, '%')} -> {fmt(c['floor']['in_band_pct'], 1, '%')}, "
                  f"paired floor/base/tie {c['paired']['floor_better']}/"
                  f"{c['paired']['base_better']}/{c['paired']['ties']}, "
                  f"median diff {fmt(c['paired']['median_scaled_diff'], 3)}")
        for floor in FLOORS:
            o = overpay(cut_rows, floor)
            cc = comp_check(cut_rows, floor)
            cut_out[f"floor {floor:.0%}"].update(overpay=o, comp_check=cc)
            print(f"- overpay {floor:.0%}: {o['flips']} LOST->WON flips, pay "
                  f"${o['paid_low']:.0f}-${o['paid_high']:.0f}; above unfloored top "
                  f"${o['over_top_low']:.0f}-${o['over_top_high']:.0f} "
                  f"({o['flips_price_above_top']} flips clear above it); above real max_bid "
                  f"${o['over_max_low']:.0f}-${o['over_max_high']:.0f}; WON rows with raised "
                  f"max_bid {o['won_raised']} (+${o['won_added_exposure']:.0f} unspent exposure); "
                  f"flip ids {o['flip_ids']}")
            print(f"- comps {floor:.0%}: {cc['changed']} changed bands, {cc['known']} with a "
                  f"known max comp; floored high > max comp on {cc['high_over']} "
                  f"(unfloored high already > max comp on {cc['base_high_over']}); "
                  f"replayed max_bid > max comp on {cc['max_bid_over']}")
        out["cuts"][cut_name] = cut_out
    conn.close()
    if "--rows-json" in sys.argv:
        path = sys.argv[sys.argv.index("--rows-json") + 1]
        with open(path, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
