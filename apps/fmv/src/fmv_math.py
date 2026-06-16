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

DEFAULT_GRADE_WINDOW = 0.5   # widen start
WIDE_GRADE_WINDOW = 1.0      # confidence-cap boundary: window > this caps at MEDIUM
GRADE_WINDOW_STEP = 0.5      # progressive widen increment
MAX_GRADE_WINDOW = 2.0       # widen ceiling (BUI-86)
MIN_NARROW_POOL = 5          # widen-stop target: keep widening until this many comps
MIN_PRICEABLE_POOL = 2       # sparse-flag floor: fewer trimmed comps → flag too_sparse
MAX_GRADE_SPAN = 2.0         # too-wide guard: pool grade-span above this → flag
SMALL_POOL_MAX_RATIO = 3.0   # 2-comp dispersion guard: hi/lo above this → flag (BUI-179)


def build_pool(comps: Iterable[dict], target_grade: float,
               max_window: float = MAX_GRADE_WINDOW) -> tuple[list[dict], float]:
    """Return (comps_in_window, window_used) for comps within ±window of target.

    Progressive widening (BUI-86): starts at ±0.5 and widens in GRADE_WINDOW_STEP
    increments up to ``max_window``, stopping at the first window holding at least
    MIN_NARROW_POOL grade-bearing comps. Returns the selected comp dicts (carrying
    grade, so compute_fmv can evaluate the one-sided/span guards) and the window
    used. Comps with no parsed grade are dropped (they'd add noise without
    enabling grade-curve checks).
    """
    comps = list(comps)

    def within(window):
        return [c for c in comps
                if c.get("grade") is not None
                and abs(c["grade"] - target_grade) <= window]

    # Honor max_window exactly: start no wider than the ceiling, and never step
    # past it (a non-0.5-aligned ceiling like 1.3 must cap at ±1.3, not ±1.5).
    window = min(DEFAULT_GRADE_WINDOW, max_window)
    pool = within(window)
    while len(pool) < MIN_NARROW_POOL and window < max_window:
        window = round(min(window + GRADE_WINDOW_STEP, max_window), 4)
        pool = within(window)
    return pool, window


def _classify_pool(pool: list[dict], target_grade: float,
                   trimmed_n: int) -> tuple[str | None, float | None]:
    """Return (flag_reason, grade_span) for a widened pool (BUI-86).

    flag_reason is one of "one_sided" / "too_wide" / "too_sparse" or None.
    one_sided / too_wide are evaluated on the untrimmed grade-bearing pool (grade
    coverage is a property of the comps we found, not of price-outlier trimming);
    too_sparse is evaluated on the post-IQR-trim count. Precedence when more than
    one applies: too_sparse → one_sided → too_wide. An empty pool (n=0) is the
    existing no-comps stub, not a manual flag, so returns (None, None).
    """
    grades = [c["grade"] for c in pool if c.get("grade") is not None]
    if not grades:
        return None, None
    lo, hi = min(grades), max(grades)
    grade_span = hi - lo
    if 0 < trimmed_n < MIN_PRICEABLE_POOL:
        return "too_sparse", grade_span
    if not (lo <= target_grade <= hi):       # not bracketed → one-sided
        return "one_sided", grade_span
    if grade_span > MAX_GRADE_SPAN:
        return "too_wide", grade_span
    return None, grade_span


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
# /comic:grade emits grade_confidence as high|medium|medium-low|low (all four
# levels preserved through the handoff so MEDIUM-LOW haircuts at 0.70, not 0.60).
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

    Back-compat (BUI-51): when grade_confidence is None or blank — a manual run
    or an already-graded comic that never went through the photo grader — the
    haircut does NOT engage and the bid stays at BASE_BID_FACTOR, exactly as
    before. The presence of a real grade_confidence is the opt-in switch.

    The grade_confidence value is authored by the /comic:grade LLM and reaches
    here via a JSON envelope, so it is untrusted: a non-string or a typo'd label
    must neither crash nor silently skip the haircut. A present-but-unrecognized
    value is treated as LOW — the conservative direction for a bid cap (bid less
    when we're unsure), not MEDIUM (which would fail open).
    """
    if grade_confidence is None:
        return BASE_BID_FACTOR
    if isinstance(grade_confidence, str):
        gc = grade_confidence.strip().lower()
        if gc == "":
            return BASE_BID_FACTOR              # blank == absent
        g = _GRADE_CONF_NORMALIZE.get(gc, "LOW")
    else:
        g = "LOW"                               # non-string envelope value → conservative
    combined = min(_rank(fmv_confidence), _CONF_RANK[g])
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
                grade_confidence: str | None = None,
                max_window: float | None = None) -> dict:
    """Take a deduped, hard-excluded comp list and return the FMV summary.

    `grade_confidence` (BUI-51) is the photo-coverage confidence from
    /comic:grade (high|medium|low). When present, the max bid is haircut by
    the more conservative of it and the comp-pool confidence (see bid_factor).
    When None, the bid stays at BASE_BID_FACTOR — back-compat for manual or
    already-graded books.

    `max_window` (BUI-86) caps how far the pool widens; the caller threads
    `--grade-window` through here. It only changes reach, never the guards.

    Output shape:
    {
      "n": int,                        # trimmed pool size
      "window": float,                 # window the pool was built at (≤ max_window)
      "flag_reason": str | None,       # one_sided | too_wide | too_sparse | None (BUI-86)
      "grade_span": float | None,      # max(grade) - min(grade) over the pool
      "fmv_low": int | None,           # Q25, clean-rounded (None if flagged/no-comps)
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

    A `flag_reason` book is "needs manual pricing" (BUI-86): it emits no
    bid-able number and its confidence is forced to LOW so the persisted
    fmv_confidence stays `low`. Priceability is derived downstream from
    `flag_reason is not None` — there is no separate `priceable` field.
    """
    if max_window is None:
        max_window = MAX_GRADE_WINDOW
    pool, window = build_pool(comps, target_grade, max_window=max_window)
    trimmed = iqr_trim([c["price"] for c in pool])
    n = len(trimmed)
    cv_val = cv(trimmed)
    flag_reason, grade_span = _classify_pool(pool, target_grade, n)

    # BUI-179: a 2-comp pool is never IQR-trimmed (len<3) and isn't too_sparse
    # (n>=2), so a single mistagged slab ([$10, $5000]) would price at a wild Q75
    # → 0.80×high overpay. Flag a tiny pool whose two prices diverge implausibly
    # (hi/lo beyond SMALL_POOL_MAX_RATIO) as needs-manual rather than pricing it.
    if flag_reason is None and n == 2:
        lo, hi = min(trimmed), max(trimmed)
        if lo <= 0 or hi / lo > SMALL_POOL_MAX_RATIO:
            flag_reason = "too_sparse"

    label = confidence_label(n, cv_val)
    if flag_reason is not None:
        label = "LOW"  # a needs_manual book never claims priceable confidence
    elif window > WIDE_GRADE_WINDOW and _rank(label) > _CONF_RANK["MEDIUM"]:
        label = "MEDIUM"  # wide-window pools can't claim HIGH/MEDIUM-HIGH (BUI-86 R7)
    factor = bid_factor(label, grade_confidence)

    if flag_reason is not None or n == 0:
        fmv_low = fmv_high = med = max_bid = None
    else:
        fmv_low = clean_round(quartile(trimmed, 0.25))
        fmv_high = clean_round(quartile(trimmed, 0.75))
        med = clean_round(statistics.median(trimmed))
        max_bid = clean_round(fmv_high * factor)

    return {
        "n": n,
        "window": window,
        "flag_reason": flag_reason,
        "grade_span": grade_span,
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
