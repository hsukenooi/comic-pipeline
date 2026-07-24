#!/usr/bin/env python3
"""BUI-527 — calibrate ``fmv_high`` against resolved auction outcomes.

WHAT THIS IS
------------
A read-only, re-runnable derivation tool that back-tests whether ``fmv_high``
is mis-calibrated as a Q75 of realized clearing prices, and — if so — derives
per-confidence-tier / per-comp-count calibration factors for it. It reads the
production comics-server DB (``bids × bid_fmvs × fmv``), never writes, and is
NOT imported by the live FMV pipeline (``fmv_math`` / ``fmv_runner``). Run it
against the Mac Mini DB; the data grows every week, so re-run before revisiting
this ticket.

    python3 apps/fmv/scripts/fmv_high_calibration.py [--db PATH]

DECISION (2026-07-24, ~376 resolved outcomes): **NO calibration factor was
applied to the live bid path.** The headline "39% cleared above ``fmv_high``
vs the ~25% a true Q75 implies" does NOT survive de-confounding. It is
explained by two effects that are NOT "``fmv_high`` is systematically too low":

  1. The BUI-528 collapsed-point bug (``fmv_low == fmv_high`` on non-degenerate
     pools), which THIS BATCH already fixed. Priceable collapsed rows (77 with
     ``high > 0``; 82 incl. 5 zero-high) exceed at ~65%, vs 31.3% for the other
     294 non-collapsed rows. 61 of those 77 have >=2 comps, i.e. the new
     ``_widen_collapsed_range`` math would re-open them going forward — so those
     rows over-state go-forward exceedance.

  2. Right-censoring / bidding-mechanics contamination. On a LOST auction
     ``winning_bid`` is a FLOOR (often our ``max_bid`` + one bid increment), and
     26% of rows carry ``max_bid >= fmv_high`` (manual overrides or an
     ``fmv_high`` recomputed downward AFTER the snipe). Any loss with
     ``max_bid >= fmv_high`` exceeds ``fmv_high`` by construction — a tautology,
     not market evidence.

Restricting to the honest go-forward population — non-collapsed rows priced
under the standard 0.80x haircut (``max_bid < fmv_high``) — exceedance is
**19.4%, already BELOW the 25% target**. The UNCENSORED evidence (auctions we
WON, where the true clearing price is observed exactly) is even more direct:
those clear at a MEDIAN of 0.57x ``fmv_high`` with only 4.4% exceedance. There
is no honest signal that ``fmv_high`` is too low in the regime we actually bid.

Why no factor was shipped anyway:
  * Per-confidence-tier Q75(ratio) is statistically FLAT (high 1.036 / medium
    1.05 / low 1.05) and thin (high-tier non-collapsed n=18) — no honest
    per-tier differentiation exists.
  * The naive Q75-derived factor (~1.05) is a clean-round NO-OP: only ~6-9% of
    rows change value after ``clean_round``, because the 1.05 ratio is just the
    losing-increment landing one clean step above ``fmv_high``, not a market gap.
  * Any factor large enough to move numbers (>=1.10) pushes honest-regime
    exceedance to ~14%, i.e. it would systematically OVER-bid into a live money
    path, justified by the censored loss tail and contradicted by the clean win
    data.

If a future re-run (more data, post-BUI-528 rows accumulated) shows the honest
population (2) drifting materially above 25% with a tier gradient that survives
the confound layers below, THEN a factor is warranted — apply it in
``fmv_math.compute_fmv`` at the ``fmv_high`` step and surface it as an
``fmv_notes`` token (e.g. ``calib_high=1.10 (tier=low)``). Until then, applying
one is speculative.

CENSORING NOTE (the ticket's crux): LOST prices are right-censored, so this
tool calibrates against the above-Q75 EXCEEDANCE RATE (measurable on wins,
observable-floor on losses), never raw loss ratios. Because loss ``winning_bid``
under-states the true clearing price, every exceedance number here is a LOWER
bound on the true rate — the conservative (money-safe) direction.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
from collections import Counter

DEFAULT_DB = os.path.expanduser("~/.comics-server/db.sqlite")


def _q(vals: list[float], p: float) -> float | None:
    """Inclusive-method quantile at fraction ``p`` (matches fmv_math.quartile)."""
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    cuts = statistics.quantiles(sorted(vals), n=100, method="inclusive")
    return cuts[max(0, min(98, round(p * 100) - 1))]


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
        SELECT b.status, b.max_bid, b.winning_bid,
               f.low, f.high, f.comps, f.confidence
        FROM bids b
        JOIN bid_fmvs bf ON bf.bid_id = b.id
        JOIN fmv f       ON f.id = bf.fmv_id
        WHERE bf.is_primary = 1
          AND b.status IN ('WON', 'LOST')
          AND b.winning_bid IS NOT NULL
          AND f.high IS NOT NULL
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _exceed(rs: list[dict]) -> float | None:
    """Exceedance rate = fraction whose clearing price cleared above fmv_high.

    On a WON row winning_bid is the exact price; on a LOST row it is a floor
    (see the module docstring's censoring note), so this is a lower bound on
    the true rate.
    """
    return sum(1 for r in rs if r["winning_bid"] > r["high"]) / len(rs) if rs else None


def _ratios(rs: list[dict]) -> list[float]:
    return [r["winning_bid"] / r["high"] for r in rs if r["high"] and r["high"] > 0]


def _is_collapsed(r: dict) -> bool:
    return r["low"] is not None and r["low"] == r["high"]


def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser(description="BUI-527 fmv_high calibration back-test")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"comics-server DB (default {DEFAULT_DB})")
    args = ap.parse_args()

    rows = _load(args.db)
    priceable = [r for r in rows if r["high"] and r["high"] > 0]
    nc = [r for r in priceable if not _is_collapsed(r)]
    honest = [r for r in nc if r["max_bid"] < r["high"]]  # real 0.80x-haircut regime

    print(f"Resolved linked outcomes (non-null fmv_high): {len(rows)}  "
          f"(high>0: {len(priceable)})\n")

    print("=== Exceedance, peeling one confound per layer (crux of the STOP) ===")
    print(f"  (0) all high>0                          : {_pct(_exceed(priceable))}  n={len(priceable)}")
    print(f"  (1) - BUI-528 collapsed points (fixed)  : {_pct(_exceed(nc))}  n={len(nc)}")
    print(f"  (2) - also max_bid>=high (0.80x regime) : {_pct(_exceed(honest))}  n={len(honest)}   <- target is 25%")
    won = [r for r in honest if r["status"] == "WON"]
    lost = [r for r in honest if r["status"] == "LOST"]
    print(f"        within (2): won-exceedance (uncensored) {_pct(_exceed(won))} n={len(won)}; "
          f"lost-floor {_pct(_exceed(lost))} n={len(lost)}")

    print("\n=== Uncensored win-only clearing distribution (the cleanest evidence) ===")
    wr = _ratios([r for r in nc if r["status"] == "WON"])
    print(f"  won wb/fmv_high: median={statistics.median(wr):.3f}  Q75={_q(wr, 0.75):.3f}  "
          f"exceedance={_pct(sum(1 for x in wr if x > 1) / len(wr))}  n={len(wr)}")

    print("\n=== Per confidence tier (non-collapsed): is a per-tier factor supportable? ===")
    for tier in ("high", "medium", "low"):
        rs = [r for r in nc if r["confidence"] == tier]
        rr = _ratios(rs)
        if rr:
            print(f"  conf={tier:<6} n={len(rs):<4} exceedance={_pct(_exceed(rs))}  "
                  f"Q75(ratio)={_q(rr, 0.75):.3f}  median={statistics.median(rr):.3f}")
    print("  -> tiers are flat (~1.036/1.05/1.05) and thin: no honest per-tier factor.")

    print("\n=== Per comp-count bucket (non-collapsed) ===")
    def bucket(n: object) -> str:
        if not isinstance(n, int):
            return "None"
        return "1-2" if n <= 2 else "3-4" if n <= 4 else "5-7" if n <= 7 else "8+"
    for b in ("1-2", "3-4", "5-7", "8+", "None"):
        rs = [r for r in nc if bucket(r["comps"]) == b]
        rr = _ratios(rs)
        if rr:
            print(f"  comps={b:<4} n={len(rs):<4} exceedance={_pct(_exceed(rs))}  "
                  f"Q75(ratio)={_q(rr, 0.75):.3f}")

    print("\n=== Collapsed-row breakdown (BUI-528 confound size) ===")
    col = [r for r in priceable if _is_collapsed(r)]
    widenable = sum(1 for r in col if isinstance(r["comps"], int) and r["comps"] >= 2)
    cc = Counter((r["comps"] if isinstance(r["comps"], int) else "None") for r in col)
    print(f"  collapsed high>0: {len(col)}  exceedance={_pct(_exceed(col))}")
    print(f"  comps>=2 (new math would widen going forward): {widenable}; "
          f"comps<=1/None (genuinely degenerate): {len(col) - widenable}")
    print(f"  comp-count histogram: {dict(sorted(cc.items(), key=lambda kv: str(kv[0])))}")

    print("\n=== Factor sweep on honest population (2): clean_round makes small factors a no-op ===")
    for fac in (1.00, 1.05, 1.10, 1.15, 1.20):
        e = sum(1 for r in honest if r["winning_bid"] > _clean_round(r["high"] * fac)) / len(honest)
        changed = sum(1 for r in honest if _clean_round(r["high"] * fac) != r["high"])
        print(f"  x{fac:.2f}: exceedance={_pct(e)}  rows-changed={changed}/{len(honest)} "
              f"({changed / len(honest) * 100:.0f}%)")

    print("\nDECISION: no factor applied to the live bid path — the de-confounded honest\n"
          "exceedance (19.4% at the last run) is already below the 25% Q75 target, the\n"
          "per-tier signal is flat/thin, and the uncensored win data (median 0.57x) does\n"
          "not support raising fmv_high. See this file's module docstring for the full\n"
          "rationale and the re-open criteria.")


if __name__ == "__main__":
    main()
