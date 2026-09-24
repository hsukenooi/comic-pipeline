"""Paired FMV band-set comparison (BUI-979). DIAGNOSTIC ONLY, read-only.

Scores two FMV band sets against the SAME resolved auctions, side by side,
so a pricing change (old bands vs new bands) can be judged on outcomes. The
fixed-window hit rates of BUI-977 score only the band midpoint; the Winkler
interval score scores the whole band — its width plus a penalty for every
price outside it — so a wider band cannot buy a better score for free.

The auctions scored come from `db._fmv_accuracy_rows`, the exact selection
`fmv_accuracy_report` uses (resolved WON/LOST incl. purge-swept rows,
primary link, single-book bids, `winning_bid` present). There is no second
definition of "resolved" here; only the bands differ.

Run against the live DB (opened read-only):

    uv run python -m gixen_overlay.band_compare --db ~/.comics-server/db.sqlite \\
        --successive-snapshots
    uv run python -m gixen_overlay.band_compare --db ... --a old.json --b new.json

Band-set JSON files map bid id to a band: `{"652": [50, 60]}` or
`{"652": {"low": 50, "high": 60}}`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from statistics import mean, median
from typing import Any

from gixen_overlay.db import _fmv_accuracy_metrics, _fmv_accuracy_rows

Band = tuple[float, float]

# Our bands are built as roughly the pool's weighted Q25..Q75, i.e. a central
# 50% interval, so alpha = 0.5 is the natural default.
DEFAULT_ALPHA = 0.5


def winkler_score(low: float, high: float, y: float, alpha: float = DEFAULT_ALPHA) -> float:
    """Winkler interval score of the central (1 - alpha) interval [low, high]
    for outcome *y* (Gneiting, T. and Raftery, A. E. 2007, "Strictly Proper
    Scoring Rules, Prediction, and Estimation", JASA 102(477):359-378, sec. 6.2).

        S = (high - low)
            + (2 / alpha) * (low - y)   if y < low
            + (2 / alpha) * (y - high)  if y > high

    Lower is better. For a covered y the score is exactly the width; an
    outside y adds 2/alpha times the miss distance (4x at alpha = 0.5), so
    narrowing a band that still covers helps, and a miss costs more than the
    width it would have taken to cover it.
    """
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if low > high:
        raise ValueError(f"low {low} > high {high}")
    score = high - low
    if y < low:
        score += (2 / alpha) * (low - y)
    elif y > high:
        score += (2 / alpha) * (y - high)
    return score


def _valid_band(band: Any) -> Band | None:
    """A band usable for scoring: two positive numbers with low <= high."""
    if band is None:
        return None
    low, high = band
    if low is None or high is None or low <= 0 or high <= 0 or low > high:
        return None
    return float(low), float(high)


def _band_set_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """BUI-977's fixed-window metrics (reused, not re-derived) plus Winkler,
    over rows already carrying their `winkler` score.

    Headline: `winkler_scaled_median` = median over auctions of W / price.
    Prices here span about $3 to $3,000, and the raw Winkler score is in
    dollars, so a raw mean or median is set by the few expensive books. W / y
    expresses each auction's score as a fraction of its own price (the same
    normalization MdAPE uses), and the median resists the long tail that the
    4x outside-penalty creates. Caveat: dividing by y reweights the rule, so
    the scaled score is no longer strictly proper (it mildly favours bands
    set low); the raw median (`winkler_median`) is reported alongside so a
    disagreement between the two is visible rather than hidden.
    """
    metrics = _fmv_accuracy_metrics(rows)
    if not rows:
        metrics.update(winkler_median=None, winkler_scaled_median=None,
                       winkler_scaled_mean=None)
        return metrics
    scores = [r["winkler"] for r in rows]
    scaled = [s / r["price"] for s, r in zip(scores, rows)]
    metrics.update(
        winkler_median=median(scores),
        winkler_scaled_median=median(scaled),
        winkler_scaled_mean=mean(scaled),
    )
    return metrics


def band_comparison(
    conn: sqlite3.Connection,
    band_set_a: Mapping[int, Any],
    band_set_b: Mapping[int, Any],
    *,
    alpha: float = DEFAULT_ALPHA,
    days: float | None = None,
    labels: tuple[str, str] = ("a", "b"),
    include_rows: bool = False,
) -> dict[str, Any]:
    """Score two band sets (bid id -> (low, high)) on the same resolved
    auctions, side by side. Only auctions that are resolved (per BUI-977's
    selection) AND have a valid band in BOTH sets are scored; `n_paired`
    reports that count, and `n_resolved`/`n_a`/`n_b` show what was dropped.

    Per set: `_fmv_accuracy_metrics` (within +/-10%/20% of midpoint, MdAPE,
    in/above/below band, width) plus Winkler (see `_band_set_metrics`).
    Paired: how many auctions each set scores strictly better on (Winkler),
    and the median per-auction difference in scaled score (b minus a;
    negative means b is better).

    Leakage is the CALLER's responsibility: each band must have been
    computed before its auction ended, or it may already contain that
    auction's own price (see `fmv_accuracy_report`). `successive_snapshot_band_sets`
    builds a pair of sets that are both leakage-free by construction.
    """
    label_a, label_b = labels
    if label_a == label_b:
        raise ValueError("labels must differ")
    resolved = _fmv_accuracy_rows(conn, days=days)
    rows_a: list[dict[str, Any]] = []
    rows_b: list[dict[str, Any]] = []
    n_a = n_b = 0
    for r in resolved:
        band_a = _valid_band(band_set_a.get(r["bid_id"]))
        band_b = _valid_band(band_set_b.get(r["bid_id"]))
        n_a += band_a is not None
        n_b += band_b is not None
        if band_a is None or band_b is None:
            continue
        base = {"bid_id": r["bid_id"], "price": r["price"], "status": r["status"]}
        rows_a.append({**base, "low": band_a[0], "high": band_a[1],
                       "winkler": winkler_score(*band_a, r["price"], alpha)})
        rows_b.append({**base, "low": band_b[0], "high": band_b[1],
                       "winkler": winkler_score(*band_b, r["price"], alpha)})

    a_better = b_better = ties = 0
    diffs: list[float] = []
    for ra, rb in zip(rows_a, rows_b):
        wa, wb = ra["winkler"], rb["winkler"]
        diffs.append((wb - wa) / ra["price"])
        if wa < wb:
            a_better += 1
        elif wb < wa:
            b_better += 1
        else:
            ties += 1

    result: dict[str, Any] = {
        "alpha": alpha,
        "days": days,
        "n_resolved": len(resolved),
        "n_a": n_a,
        "n_b": n_b,
        "n_paired": len(rows_a),
        label_a: _band_set_metrics(rows_a),
        label_b: _band_set_metrics(rows_b),
        "paired": {
            f"{label_a}_better": a_better,
            f"{label_b}_better": b_better,
            "ties": ties,
            "median_scaled_diff": median(diffs) if diffs else None,
        },
    }
    if include_rows:
        result["rows"] = [
            {
                "bid_id": ra["bid_id"], "status": ra["status"], "price": ra["price"],
                f"{label_a}_low": ra["low"], f"{label_a}_high": ra["high"],
                f"{label_b}_low": rb["low"], f"{label_b}_high": rb["high"],
                f"{label_a}_winkler": ra["winkler"],
                f"{label_b}_winkler": rb["winkler"],
            }
            for ra, rb in zip(rows_a, rows_b)
        ]
    return result


def successive_snapshot_band_sets(
    conn: sqlite3.Connection, *, days: float | None = None
) -> tuple[dict[int, Band], dict[int, Band]]:
    """(prior, in_force) band sets from `fmv_history`, both leakage-free.

    For each resolved auction whose book was priced at least twice before
    the bid was added: `in_force` is the latest snapshot at-or-before
    `bids.added_at` (the same band BUI-977 scores) and `prior` is the
    snapshot before it. Both predate the bid, so neither can contain the
    auction's own price. The snapshot filter (same comic/grade/certifier/
    label, positive high, normalized timestamps) matches
    `_fmv_accuracy_rows`' in-force lookup exactly.
    """
    prior: dict[int, Band] = {}
    in_force: dict[int, Band] = {}
    for r in _fmv_accuracy_rows(conn, days=days):
        if r["band_source"] != "history":
            continue
        # The in-force band is the one the accuracy row already carries;
        # the prior band is the latest OTHER snapshot under the same filter.
        prev = conn.execute(
            """
            SELECT fh.low, fh.high
            FROM fmv_history fh
            WHERE fh.comic_id = ? AND fh.grade = ? AND fh.certifier = ?
              AND fh.label = ? AND fh.id != ?
              AND fh.high IS NOT NULL AND fh.high > 0
              AND substr(fh.recorded_at, 1, 19) <= replace(?, ' ', 'T')
            ORDER BY fh.recorded_at DESC
            LIMIT 1
            """,
            (r["comic_id"], r["grade"], r["certifier"], r["label"],
             r["fmv_history_id"], r["added_at"]),
        ).fetchone()
        if prev is None:
            continue
        in_force[r["bid_id"]] = (r["low"], r["high"])
        prior[r["bid_id"]] = (prev["low"], prev["high"])
    return prior, in_force


def _load_band_set(path: str) -> dict[int, Any]:
    raw = json.loads(Path(path).read_text())
    out: dict[int, Any] = {}
    for key, band in raw.items():
        if isinstance(band, Mapping):
            band = (band.get("low"), band.get("high"))
        out[int(key)] = tuple(band)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="SQLite path, opened read-only")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--successive-snapshots", action="store_true",
                     help="prior vs in-force fmv_history snapshot, both pre-bid")
    src.add_argument("--a", help="band-set JSON for set a")
    parser.add_argument("--b", help="band-set JSON for set b (with --a)")
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    parser.add_argument("--days", type=float, default=None)
    parser.add_argument("--rows", action="store_true", help="include per-auction rows")
    args = parser.parse_args(argv)
    if args.a and not args.b:
        parser.error("--a requires --b")

    uri = f"file:{Path(args.db).expanduser()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if args.successive_snapshots:
            band_a, band_b = successive_snapshot_band_sets(conn, days=args.days)
            labels = ("prior", "in_force")
        else:
            band_a, band_b = _load_band_set(args.a), _load_band_set(args.b)
            labels = ("a", "b")
        report = band_comparison(conn, band_a, band_b, alpha=args.alpha,
                                 days=args.days, labels=labels,
                                 include_rows=args.rows)
    finally:
        conn.close()
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
