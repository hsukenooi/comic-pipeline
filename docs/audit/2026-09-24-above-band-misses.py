#!/usr/bin/env python3
"""Reproduce the BUI-978 above-band miss analysis (read-only).

Input 1: the JSON body of `comics-api GET /api/comics/accuracy?include_rows=true`
(pass its path as argv[1]).
Input 2: the live comics DB, opened read-only (`mode=ro`) to join book year,
grade source, pricing basis, max_bid, and the FMV notes each band was built with.
Nothing is written anywhere.

    python3 docs/audit/2026-09-24-above-band-misses.py acc-rows.json
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

DB = os.path.expanduser("~/.comics-server/db.sqlite")


def load(path: str) -> list[dict]:
    rows = json.load(open(path))["rows"]
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ids = ",".join(str(r["bid_id"]) for r in rows)
    extra = {
        r["id"]: dict(r)
        for r in conn.execute(
            f"""
            SELECT b.id, b.seller_grade, b.photo_grade, b.max_bid,
                   f.pricing_basis, f.provenance, f.notes AS fmv_notes, c.year
            FROM bids b
            JOIN bid_fmvs bf ON bf.bid_id = b.id AND bf.is_primary = 1
            JOIN fmv f ON f.id = bf.fmv_id
            JOIN comics c ON c.id = f.comic_id
            WHERE b.id IN ({ids})
            """
        )
    }
    hist = {
        r["id"]: (r["notes"], r["recorded_at"])
        for r in conn.execute("SELECT id, notes, recorded_at FROM fmv_history")
    }
    conn.close()
    missing = [r["bid_id"] for r in rows if r["bid_id"] not in extra]
    if missing:
        sys.exit(f"join failed for {len(missing)} bids: {missing[:10]}")
    for r in rows:
        x = extra[r["bid_id"]]
        r.update(x)
        # The notes that describe the band actually scored: the snapshot's
        # when band_source='history', else the current fmv row's.
        snap = hist.get(r["fmv_history_id"]) if r["fmv_history_id"] else None
        r["band_notes"] = (snap[0] if snap else x["fmv_notes"]) or ""
        r["band_recorded_at"] = snap[1] if snap else None
        r["outcome"] = (
            "above" if r["price"] > r["high"] else "below" if r["price"] < r["low"] else "in"
        )
    return rows


# ---- slicing dimensions (all known before the auction ends) ----------------

def comps(r):
    n = r["comps"]
    if n is None:
        return "unknown"
    return "0-2" if n <= 2 else "3-5" if n <= 5 else "6-10" if n <= 10 else "11+"


def confidence(r):
    return r["confidence"] or "unknown"


def basis(r):
    # fmv.pricing_basis is the CURRENT row's basis (fmv_history has no basis
    # column); notes format separates the machine runner from hand pricing.
    if r["pricing_basis"] and r["pricing_basis"] != "direct":
        return r["pricing_basis"]
    return "direct, machine notes" if "window=" in r["band_notes"] else "direct, hand notes"


def window(r):
    m = re.search(r"window=±([\d.]+)", r["band_notes"])
    return f"±{m.group(1)}" if m else "not recorded"


def era(r):
    y = r["year"]
    if y is None:
        return "unknown"
    return "<1970" if y < 1970 else "1970-84" if y < 1985 else "1985-99" if y < 2000 else "2000+"


def grade_source(r):
    if r["photo_grade"] is not None:
        return "photo grade"
    if r["seller_grade"] is not None:
        return "seller-stated"
    return "not recorded"


def tier(r):
    mid = (r["low"] + r["high"]) / 2  # the band midpoint, not the price
    return "<$20" if mid < 20 else "$20-50" if mid < 50 else "$50-150" if mid < 150 else "$150+"


def width(r):
    if r["low"] == r["high"]:
        return "zero (low = high)"
    w = (r["high"] - r["low"]) / ((r["low"] + r["high"]) / 2)
    return "<30%" if w < 0.30 else "30-50%" if w < 0.50 else "50-80%" if w < 0.80 else "80%+"


def status(r):
    return r["status"]


DIMS = [
    ("Comp count", comps),
    ("Confidence (stored)", confidence),
    ("Pricing basis", basis),
    ("Grade window", window),
    ("Era (book year)", era),
    ("Grade source", grade_source),
    ("Price tier (band midpoint)", tier),
    ("Band width", width),
    ("Status", status),
]


def overshoot(r):
    x = r["price"] / r["high"] - 1
    return "0-10%" if x <= 0.10 else "10-25%" if x <= 0.25 else "25-50%" if x <= 0.50 else "50-100%" if x <= 1.0 else ">100%"


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def table(rows, label):
    n = len(rows)
    inb = sum(r["outcome"] == "in" for r in rows)
    above = [r for r in rows if r["outcome"] == "above"]
    print(f"\n## {label}: n={n}, in band {pct(inb, n)}, above {pct(len(above), n)}\n")
    for name, fn in DIMS:
        groups = defaultdict(list)
        for r in rows:
            groups[fn(r)].append(r)
        print(f"### {name}\n")
        print("| Slice | n | In band | Above | Share of above misses | Oracle in-band |")
        print("|---|---|---|---|---|---|")
        for k in sorted(groups, key=lambda k: -len(groups[k])):
            g = groups[k]
            a = sum(r["outcome"] == "above" for r in g)
            i = sum(r["outcome"] == "in" for r in g)
            print(f"| {k} | {len(g)} | {pct(i, len(g))} | {pct(a, len(g))} | {pct(a, len(above))} | {pct(inb + a, n)} |")
        print()
    mag = defaultdict(int)
    for r in above:
        mag[overshoot(r)] += 1
    print("### How far above the band (price / high - 1)\n")
    print("| Overshoot | Misses | Share |\n|---|---|---|")
    for k in ["0-10%", "10-25%", "25-50%", "50-100%", ">100%"]:
        print(f"| {k} | {mag[k]} | {pct(mag[k], len(above))} |")


def shift_test(rows, label, pred):
    """Best in-sample multiplier on the slice's band, and the net in-band change.

    Scaling low and high by k pulls above-band misses in but can push in-band
    rows out below, so this is the net gain, not the oracle. It is in-sample,
    so it is an upper bound on what a fixed per-slice multiplier could do.
    """
    n = len(rows)
    base = sum(r["outcome"] == "in" for r in rows)
    best = (0, 1.0)
    for k100 in range(100, 301, 5):
        k = k100 / 100
        hits = sum(
            (r["low"] * k <= r["price"] <= r["high"] * k) if pred(r) else r["outcome"] == "in"
            for r in rows
        )
        if hits - base > best[0]:
            best = (hits - base, k)
    print(f"- {label}: best k={best[1]:.2f}, net +{best[0]} in band, in-band {pct(base, n)} -> {pct(base + best[0], n)}")


def width_floor_test(rows, floor):
    """Widen every band narrower than *floor* (relative to its midpoint) to
    exactly *floor*, same midpoint. Reports in-band, above, below, and the new
    median width, so a gain from width alone is visible as such."""
    from statistics import median

    n = len(rows)
    ib = ab = be = 0
    widths = []
    for r in rows:
        lo, hi = r["low"], r["high"]
        mid = (lo + hi) / 2
        if (hi - lo) / mid < floor:
            lo, hi = mid * (1 - floor / 2), mid * (1 + floor / 2)
        widths.append((hi - lo) / mid)
        if r["price"] > hi:
            ab += 1
        elif r["price"] < lo:
            be += 1
        else:
            ib += 1
    print(f"- floor {floor:.0%}: in band {pct(ib, n)}, above {pct(ab, n)}, below {pct(be, n)}, median width {100 * median(widths):.0f}%")


def main():
    rows = load(sys.argv[1])
    hist = [r for r in rows if r["band_source"] == "history"]
    table(hist, "Primary cut: band_source='history'")
    # BUI-528 (0e15cde, 2026-07-24) widens zero-width bands on dispersed pools;
    # bands recorded after it show what is left once that class is gone.
    post = [r for r in hist if r["band_recorded_at"] >= "2026-07-25"]
    table(post, "History cut, band recorded after BUI-528 (2026-07-25+)")
    table(rows, "All auctions (includes 141 current_fmv rows that can contain their own price)")
    print("\n## Band-shift test (history cut, in-sample)\n")
    for label, pred in [
        ("all rows", lambda r: True),
        ("LOW confidence", lambda r: r["confidence"] == "low"),
        ("0-2 comps", lambda r: comps(r) == "0-2"),
        ("price tier <$20", lambda r: tier(r) == "<$20"),
        ("band width <30%", lambda r: width(r) == "<30%"),
        ("zero-width band", lambda r: r["low"] == r["high"]),
        ("era 1985-99", lambda r: era(r) == "1985-99"),
    ]:
        shift_test(hist, label, pred)
    print("\n## Width-floor test (history cut)\n")
    for floor in (0.0, 0.30, 0.40, 0.50):
        width_floor_test(hist, floor)
    print("\n## Width-floor test (post-BUI-528 history rows)\n")
    for floor in (0.0, 0.30, 0.40):
        width_floor_test(post, floor)
    print("\n## LOST above-band rows set by our own bid (history cut)\n")
    lost = [r for r in hist if r["status"] == "LOST" and r["outcome"] == "above"]
    ours = [
        r for r in lost
        if r["max_bid"] and 0 <= r["price"] - r["max_bid"] <= max(1.0, 0.05 * r["max_bid"])
    ]
    print(f"- {len(lost)} LOST above band; {sum((r['max_bid'] or 0) > r['high'] for r in lost)} had max_bid above the band top;")
    print(f"  {len(ours)} cleared within one increment of our max_bid (price = our bid + increment)")


if __name__ == "__main__":
    main()
