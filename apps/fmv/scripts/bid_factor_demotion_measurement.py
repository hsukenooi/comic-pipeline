#!/usr/bin/env python3
"""BUI-622 (plan unit U8) — measure rung-demotion candidates for the bid cap.

WHAT THIS IS
------------
A read-only, re-runnable measurement of whether any FMV-pool-shape input
(comp count, FMV range width, grade range width) should demote ``bid_factor``
from its 0.80 base to an existing lower rung (0.70 / 0.60). It reads the
production comics-server DB (``bids × bid_fmvs × fmv``), never writes, and is
NOT imported by the live FMV pipeline (``fmv_math`` / ``fmv_runner``).

    python3 apps/fmv/scripts/bid_factor_demotion_measurement.py [--db PATH]

Sibling of ``fmv_high_calibration.py`` (BUI-527), which back-tests ``fmv_high``
itself. This one back-tests the *haircut applied to* ``fmv_high``. Do not fork a
third copy of the bids×fmv join; extend one of these two.

DECISION (2026-08-03, 483 resolved linked outcomes / 168 wins): **NO demotion
signal shipped. All candidates FALSIFIED, and the ticket is closed at the
oracle bound — before threshold design mattered.**

THE ORACLE BOUND (run first, per
``docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md``):
a *perfect* demotion detector — one that fires on exactly the wins that were
confirmed overpays and on nothing else — would have saved **$30.01** at the
0.70 rung and **$61.01** at the 0.60 rung, across the entire history. The same
rung, applied imprecisely, puts **$907 / $1,874** of bargain surplus at risk.
Best case is ~1/30th of the collateral. There is no ceiling here worth
optimizing toward.

THE MECHANISM (why the ceiling is that low): **a rung demotion cannot prevent
an overpay in the regime where the cap actually binds.** ``max_bid`` is already
``clean_round(0.80 × fmv_high)`` and eBay proxy bidding makes
``winning_bid <= max_bid``, so every honest-regime win clears at or below
0.80× fair value *before any demotion exists*. Confirmed overpays are
structurally impossible there, and the data agrees exactly: **0 of the 109 wins
with ``max_bid < fmv_high`` cleared at or above ``fmv_high``.** All 16 wins that
did clear above ``fmv_high`` carry ``max_bid >= fmv_high`` — the frozen-max_bid
artifact BUI-527/BUI-532 already documented (``fmv_high`` recomputed *downward*
after the snipe, or a manual override). Neither cause is addressable by a
bid-factor rung: the rung scales the cap, it does not re-price a stale
``fmv_high`` or override a human. Moving 0.80 → 0.70 does not cut into
overpricing; it cuts into the bargain margin the 0.80 haircut already creates.

THE SHARP TEST (the direction check that kills the premise outright): the
confirmed overpays came from **denser and tighter** pools than the other wins —
median comps 6.0 vs 5.0, median FMV width 0.100 vs 0.500. Both candidate
signals point the **wrong way**. ``comps < n₀`` and ``(high−low)/high > w₀``
select for the *safe* wins, not the risky ones. Measured precision tops out at
**0.167** (``comps < 3``) and **0.077** (``width > 0.70``) against the 0.80 bar,
and the result is stable across three different overpay labels.

THE EXISTING FLAG CLASS IS ALREADY TOO RARE TO CARRY A RUNG: ``fmv.flag_reason``
(``too_sparse`` / ``too_wide`` / ``one_sided``) is the pool-quality advisory R10
forbids duplicating, and it lands on only **3 of 168 wins** — none of them in
the priceable population this measurement can score. Even a perfectly
trustworthy pool-shape flag has almost no wins to act on.

THE THIRD CANDIDATE IS NOT MEASURABLE AT ALL: grade *range width* from
``/comic:grade`` is never persisted. ``fmv.grade`` is a single point, ``bids``
carries single ``seller_grade``/``photo_grade`` values, and only 4 of 168 win
rows leave a ``±N window`` token in ``fmv.notes``. Measuring ``grade range
width > g₀`` would first require a schema change to store the grader's range —
which the oracle bound above says is not worth making.

RE-OPEN CRITERIA: this closes on a structural argument, not a thin sample, so
more rows will not reverse it on their own. Re-open only if the *mechanism*
changes — e.g. ``max_bid`` stops being derived from ``fmv_high`` (a manual-cap
mode that bids above it), or ``fmv_high`` staleness is fixed such that a
material number of honest-regime wins begin clearing above it. Check the
"honest-regime overpays" line below: while it reads 0, there is nothing here
for a rung to prevent.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import statistics

DEFAULT_DB = os.path.expanduser("~/.comics-server/db.sqlite")

BASE_FACTOR = 0.80          # fmv_math.BASE_BID_FACTOR
RUNGS = (0.70, 0.60)        # the only demotion targets R10 permits
PRECISION_BAR = 0.80        # KTD9, mirroring the BUI-578→594 bar

_GRADE_WINDOW_RE = re.compile(r"±\s*([0-9.]+)\s*window")


def _clean_step(v: float) -> int:
    return 5 if v < 50 else (10 if v < 200 else 25)


def _clean_round(v: float) -> int:
    s = _clean_step(v)
    return int(round(v / s) * s)


def _load(db: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT b.status, b.max_bid, b.winning_bid, b.photo_grade,
               f.low, f.high, f.comps, f.confidence, f.notes, f.flag_reason
        FROM bids b
        JOIN bid_fmvs bf ON bf.bid_id = b.id AND bf.is_primary = 1
        JOIN fmv f       ON f.id = bf.fmv_id
        WHERE b.status IN ('WON', 'LOST')
          AND b.winning_bid IS NOT NULL
          AND b.max_bid > 0
          AND f.high IS NOT NULL AND f.high > 0
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _demoted_cap(max_bid: float, rung: float) -> int:
    """Counterfactual cap on the FROZEN snipe-time price basis.

    ``max_bid`` was ``clean_round(0.80 × fmv_high_at_snipe_time)``; today's
    ``f.high`` may have been recomputed since (the BUI-527 confound). Rescaling
    the frozen ``max_bid`` by ``rung / 0.80`` keeps the counterfactual on the
    price basis the bid was actually placed against.
    """
    return _clean_round(max_bid * (rung / BASE_FACTOR))


def _width(r: dict) -> float | None:
    """FMV range width as a fraction of high: ``(high - low) / high``."""
    if r["low"] is None or not r["high"]:
        return None
    return (r["high"] - r["low"]) / r["high"]


# Three overpay labels, weakest evidence last. The headline is the calibration
# report's confirmed win-based exceedance (BUI-532): on a WON row winning_bid is
# the exact price paid, so `winning_bid >= fmv_high` is uncensored evidence the
# purchase was not a bargain. The looser two exist to prove the falsification is
# not an artifact of one strict label.
LABELS: dict[str, object] = {
    "winning_bid >= fmv_high  (confirmed, BUI-532)": lambda r: r["winning_bid"] >= r["high"],
    "winning_bid >= 0.90 x fmv_high  (near)": lambda r: r["winning_bid"] >= 0.90 * r["high"],
    "winning_bid >= 0.80 x fmv_high  (at the cap)": lambda r: r["winning_bid"] >= 0.80 * r["high"],
}


def _candidates(won: list[dict]) -> list[tuple[str, object]]:
    """The candidate demotion predicates named in U8, plus two controls."""
    out: list[tuple[str, object]] = []
    for n0 in (2, 3, 4, 5, 6):
        out.append((f"comps < {n0}",
                    lambda r, n0=n0: isinstance(r["comps"], int) and r["comps"] < n0))
    for w0 in (0.40, 0.50, 0.60, 0.70, 0.80):
        out.append((f"(high-low)/high > {w0:.2f}",
                    lambda r, w0=w0: (_width(r) or -1) > w0))
    # Controls: signals that already exist, to show the bar is reachable by
    # nothing here rather than merely unreachable by new thresholds.
    out.append(("[control] any flag_reason", lambda r: bool(r["flag_reason"])))
    out.append(("[control] fmv confidence = low", lambda r: r["confidence"] == "low"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="BUI-622 rung-demotion measurement")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"comics-server DB (default {DEFAULT_DB})")
    args = ap.parse_args()

    rows = _load(args.db)
    won = [r for r in rows if r["status"] == "WON"]
    lost = [r for r in rows if r["status"] == "LOST"]
    print(f"Resolved linked outcomes: {len(rows)}  (WON={len(won)}, LOST={len(lost)})")
    print("A demotion can only ever change a WON row: on a LOST auction bidding "
          "less still loses, and\nunder proxy bidding a win that stays a win pays "
          "the same price. Wins are the whole population.\n")

    # ── 0. The structural fact, before any threshold ────────────────────────
    print("=== (0) Can a rung demotion prevent an overpay at all? ===")
    honest = [r for r in won if r["max_bid"] < r["high"]]
    honest_over = [r for r in honest if r["winning_bid"] >= r["high"]]
    stale = [r for r in won if r["max_bid"] >= r["high"]]
    stale_over = [r for r in stale if r["winning_bid"] >= r["high"]]
    print(f"  wins in the honest 0.80x regime (max_bid < fmv_high) : {len(honest)}/{len(won)}")
    print(f"    ... that cleared at/above fmv_high                 : {len(honest_over)}   "
          f"<- structurally impossible: winning_bid <= max_bid < fmv_high")
    print(f"  wins with max_bid >= fmv_high (frozen/override)      : {len(stale)}/{len(won)}")
    print(f"    ... that cleared at/above fmv_high                 : {len(stale_over)}   "
          f"<- every confirmed overpay lives here, and a rung cannot fix a stale fmv_high")

    # ── 1. The oracle bound ─────────────────────────────────────────────────
    print("\n=== (1) ORACLE: what a PERFECT demotion detector could ever buy ===")
    for rung in RUNGS:
        flip = [r for r in won if r["winning_bid"] > _demoted_cap(r["max_bid"], rung)]
        over = [r for r in flip if r["winning_bid"] >= r["high"]]
        saved = sum(r["winning_bid"] - r["high"] for r in over)
        bargains = [r for r in flip if r["winning_bid"] < 0.90 * r["high"]]
        forgone = sum(r["high"] - r["winning_bid"] for r in bargains)
        print(f"  rung {rung:.2f}: outcome flips (win -> loss) on {len(flip)}/{len(won)} wins "
              f"(${sum(r['winning_bid'] for r in flip):,.0f} of purchases)")
        print(f"    perfect detector avoids {len(over)} confirmed overpays "
              f"-> saves ${saved:,.2f}")
        print(f"    an imprecise one also drops {len(bargains)} bargain wins "
              f"-> ${forgone:,.2f} of forgone surplus")
        print(f"    CEILING ${saved:,.2f} vs COLLATERAL ${forgone:,.2f} "
              f"(1:{forgone / saved if saved else float('inf'):.0f})")

    # ── 2. The sharp test: which way do the candidate signals point? ────────
    print("\n=== (2) SHARP TEST: do overpay wins even come from worse-shaped pools? ===")
    over_all = [r for r in won if r["winning_bid"] >= r["high"]]
    rest = [r for r in won if r["winning_bid"] < r["high"]]
    for tag, rs in (("confirmed overpays", over_all), ("all other wins", rest)):
        cs = [r["comps"] for r in rs if isinstance(r["comps"], int)]
        ws = [w for w in (_width(r) for r in rs) if w is not None]
        print(f"  {tag:<20} n={len(rs):<4} comps median={statistics.median(cs):.1f}  "
              f"FMV width median={statistics.median(ws):.3f}")
    print("  -> overpays come from DENSER, TIGHTER pools. Both candidates select "
          "the safe wins, not the risky ones.")

    # ── 3. Candidate sweep ──────────────────────────────────────────────────
    print(f"\n=== (3) Candidate precision sweep (bar = {PRECISION_BAR:.2f}) ===")
    for label_name, label in LABELS.items():
        hits = [r for r in won if label(r)]
        print(f"\n  label: {label_name}   base rate={len(hits) / len(won):.3f} "
              f"({len(hits)}/{len(won)})")
        for name, pred in _candidates(won):
            fired = [r for r in won if pred(r)]
            if not fired:
                # Never silently drop a zero-firing candidate: "it cannot fire"
                # is a result, not an absence of one.
                print(f"    {name:<28} fired=  0  precision=n/a    FALSIFIED (never fires)")
                continue
            prec = sum(1 for r in fired if label(r)) / len(fired)
            verdict = "SHIP" if prec >= PRECISION_BAR else "FALSIFIED"
            print(f"    {name:<28} fired={len(fired):3d}  precision={prec:.3f}  {verdict}")

    # ── 4. Input availability ───────────────────────────────────────────────
    print("\n=== (4) Is the third candidate (grade range width) even measurable? ===")
    gw = sum(1 for r in won if _GRADE_WINDOW_RE.search(r["notes"] or ""))
    pg = sum(1 for r in won if r["photo_grade"] is not None)
    print(f"  '± N window' token in fmv.notes : {gw}/{len(won)}")
    print(f"  bids.photo_grade present        : {pg}/{len(won)}")
    print("  -> the grader's RANGE is never persisted (fmv.grade and bids.*_grade are "
          "single points).\n     Measuring 'grade range width > g0' needs a schema change "
          "the oracle bound does not justify.")

    print("\nDECISION: no demotion shipped — the ticket closes at the oracle bound in (1), "
          "and (2)\nshows both measurable candidates point the wrong way. See this file's "
          "module docstring\nfor the mechanism and the re-open criteria.")


if __name__ == "__main__":
    main()
