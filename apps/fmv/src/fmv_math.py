"""FMV math — pure functions, no I/O.

Takes a comp pool from ebay-cli sold_comps and produces an FMV range
(Q25–Q75), median, CV, and a confidence label. Kept in its own module so
the math is independently testable from the CLI orchestration.

Quartile method note: uses statistics.quantiles(method='inclusive') for
both IQR trim and the FMV range step. The default 'exclusive' method
over-dilates IQR on small samples (n=5 IQR can be ~10x the data spread),
which lets clear outliers survive trimming. Inclusive matches Excel's
QUARTILE.INC and behaves predictably on the 5-15 point pools we see.
"""

from __future__ import annotations

import statistics
from typing import Iterable


# ─── Pool building ────────────────────────────────────────────────────────────

DEFAULT_GRADE_WINDOW = 0.5
WIDE_GRADE_WINDOW = 1.0
MIN_NARROW_POOL = 5  # widen to ±1.0 if fewer than this in ±0.5


def build_pool(comps: Iterable[dict], target_grade: float) -> tuple[list[float], float]:
    """Return (prices, window_used) for comps within ±window of target.

    Tries ±0.5 first; widens to ±1.0 if too few. Drops comps with no
    parsed grade (they'd add noise without enabling grade-curve checks).
    """
    def within(window):
        return [c["price"] for c in comps
                if c.get("grade") is not None
                and abs(c["grade"] - target_grade) <= window]

    pool = within(DEFAULT_GRADE_WINDOW)
    if len(pool) < MIN_NARROW_POOL:
        pool = within(WIDE_GRADE_WINDOW)
        return pool, WIDE_GRADE_WINDOW
    return pool, DEFAULT_GRADE_WINDOW


# ─── IQR trim + quartiles (inclusive method) ──────────────────────────────────

def iqr_trim(prices: list[float]) -> list[float]:
    """Drop values outside Q1 - 1.5*IQR to Q3 + 1.5*IQR."""
    if len(prices) < 3:
        return list(prices)
    s = sorted(prices)
    qs = statistics.quantiles(s, n=4, method="inclusive")
    q1, q3 = qs[0], qs[2]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in s if lo <= p <= hi]


def quartile(prices: list[float], q: float) -> float:
    """Inclusive-method quantile at fraction q (0..1)."""
    if len(prices) == 1:
        return prices[0]
    qs = statistics.quantiles(sorted(prices), n=100, method="inclusive")
    # qs has 99 cut points (between n=100 buckets); index i = (i+1)/100 quantile
    idx = max(0, min(98, round(q * 100) - 1))
    return qs[idx]


def cv(prices: list[float]) -> float | None:
    """Coefficient of variation = stdev / median. None if undefined."""
    if len(prices) < 2:
        return None
    med = statistics.median(prices)
    if med == 0:
        return None
    return statistics.stdev(prices) / med


# ─── Confidence rubric ────────────────────────────────────────────────────────

def confidence_label(n: int, cv_value: float | None) -> str:
    """Per the rubric in /comic:fmv § 8."""
    if cv_value is None:
        return "MEDIUM-LOW" if n >= 3 else "LOW"
    pct = cv_value * 100
    if n >= 8 and pct < 25:
        return "HIGH"
    if n >= 6 and pct < 30:
        return "HIGH"
    if n >= 5 and pct < 35:
        return "MEDIUM-HIGH"
    if n >= 4 and pct < 45:
        return "MEDIUM"
    if n >= 3:
        return "MEDIUM-LOW"
    return "LOW"


# ─── Bid-cap factor (confidence haircut) ──────────────────────────────────────

BASE_BID_FACTOR = 0.80  # standard: max_bid = 80% × fmv_high

# Ordinal ranking of confidence labels, lowest = least confident.
_CONF_RANK = {
    "HIGH": 4, "MEDIUM-HIGH": 3, "MEDIUM": 2, "MEDIUM-LOW": 1, "LOW": 0,
}
# /comic:grade emits grade_confidence as high|medium|low (it collapses its
# MEDIUM-LOW onto "low"); tolerate the finer label too.
_GRADE_CONF_NORMALIZE = {
    "high": "HIGH", "medium": "MEDIUM", "medium-low": "MEDIUM-LOW", "low": "LOW",
}


def _rank(label: str | None) -> int:
    """Rank a confidence label; unknown/blank → MEDIUM (neutral, no haircut)."""
    return _CONF_RANK.get((label or "").strip().upper(), _CONF_RANK["MEDIUM"])


def bid_factor(fmv_confidence: str | None, grade_confidence: str | None) -> float:
    """Multiplier applied to fmv_high to get the max bid.

    Defaults to BASE_BID_FACTOR (0.80). The two confidence axes are orthogonal
    (BUI-51 KTD2): `fmv_confidence` reflects comp-pool quality, `grade_confidence`
    reflects photo coverage from /comic:grade. When grade_confidence is present
    we take the MORE CONSERVATIVE of the two and haircut a low combined level.

    Back-compat (BUI-51): when grade_confidence is None — a manual run or an
    already-graded comic that never went through the photo grader — the haircut
    does NOT engage and the bid stays at BASE_BID_FACTOR, exactly as before. The
    presence of grade_confidence is the opt-in switch for the haircut behavior.
    """
    if grade_confidence is None:
        return BASE_BID_FACTOR
    g = _GRADE_CONF_NORMALIZE.get(grade_confidence.strip().lower(), grade_confidence)
    combined = min(_rank(fmv_confidence), _rank(g))
    if combined <= _CONF_RANK["LOW"]:          # LOW
        return 0.60
    if combined == _CONF_RANK["MEDIUM-LOW"]:   # MEDIUM-LOW
        return 0.70
    return BASE_BID_FACTOR                      # MEDIUM and above


# ─── Clean rounding ───────────────────────────────────────────────────────────

def clean_round(value: float) -> int:
    """Round to clean step: $5 below $50, $10 from $50–$200, $25 above."""
    if value < 50:
        step = 5
    elif value < 200:
        step = 10
    else:
        step = 25
    return int(round(value / step) * step)


# ─── End-to-end: comps → FMV summary ─────────────────────────────────────────

def compute_fmv(comps: list[dict], target_grade: float,
                grade_confidence: str | None = None) -> dict:
    """Take a deduped, hard-excluded comp list and return the FMV summary.

    `grade_confidence` (BUI-51) is the photo-coverage confidence from
    /comic:grade (high|medium|low). When present, the max bid is haircut by
    the more conservative of it and the comp-pool confidence (see bid_factor).
    When None, the bid stays at BASE_BID_FACTOR — back-compat for manual or
    already-graded books.

    Output shape:
    {
      "n": int,                        # trimmed pool size
      "window": float,                 # 0.5 or 1.0
      "fmv_low": int | None,           # Q25, clean-rounded
      "fmv_high": int | None,          # Q75, clean-rounded
      "median": int | None,            # median, clean-rounded
      "max_bid": int | None,           # bid_factor × fmv_high, clean-rounded
      "cv": float | None,              # raw CV (not %)
      "cv_pct": str,                   # human "27%" or "n/a"
      "confidence": str,               # HIGH | MEDIUM-HIGH | MEDIUM | MEDIUM-LOW | LOW
      "grade_confidence": str | None,  # echoed back for traceability
      "bid_factor": float,             # the multiplier actually applied
      "trimmed_pool": list[float],     # for debugging / display
    }
    """
    pool, window = build_pool(comps, target_grade)
    trimmed = iqr_trim(pool)
    n = len(trimmed)
    cv_val = cv(trimmed)
    label = confidence_label(n, cv_val)
    factor = bid_factor(label, grade_confidence)

    if n >= 2:
        fmv_low = clean_round(quartile(trimmed, 0.25))
        fmv_high = clean_round(quartile(trimmed, 0.75))
        med = clean_round(statistics.median(trimmed))
        max_bid = clean_round(fmv_high * factor)
    elif n == 1:
        v = clean_round(trimmed[0])
        fmv_low = fmv_high = med = v
        max_bid = clean_round(trimmed[0] * factor)
    else:
        fmv_low = fmv_high = med = max_bid = None

    return {
        "n": n,
        "window": window,
        "fmv_low": fmv_low,
        "fmv_high": fmv_high,
        "median": med,
        "max_bid": max_bid,
        "cv": cv_val,
        "cv_pct": f"{cv_val * 100:.0f}%" if cv_val is not None else "n/a",
        "confidence": label,
        "grade_confidence": grade_confidence,
        "bid_factor": factor,
        "trimmed_pool": sorted(trimmed),
    }
