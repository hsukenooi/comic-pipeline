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

import math
import re
import statistics
from datetime import date, datetime
from typing import Iterable, Mapping


# ─── Pool building ────────────────────────────────────────────────────────────

DEFAULT_GRADE_WINDOW = 0.5   # widen start
WIDE_GRADE_WINDOW = 1.0      # confidence-cap boundary: window > this caps at MEDIUM
GRADE_WINDOW_STEP = 0.5      # progressive widen increment
MAX_GRADE_WINDOW = 2.0       # widen ceiling (BUI-86)
MIN_NARROW_POOL = 5          # widen-stop target: keep widening until this many comps
MIN_PRICEABLE_POOL = 2       # sparse-flag floor: fewer trimmed comps → flag too_sparse
MAX_GRADE_SPAN = 2.0         # too-wide guard: pool grade-span above this → flag
SMALL_POOL_MAX_RATIO = 3.0   # 2-comp dispersion guard: hi/lo above this → flag (BUI-179)
MIN_BRACKET_COMPS = 2        # §7 thin-bracket guard: a bracket bucket with fewer
                             #   than this many comps is too thin to anchor an
                             #   interpolation (a lone mistagged comp → wild
                             #   over-bid), so it can't serve as an anchor (BUI-318)


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


def ungraded_market_anchor(comps: Iterable[dict]) -> dict | None:
    """Median price of the grade-less comps ``build_pool`` drops (BUI-522).

    ``build_pool`` keeps only comps carrying a parsed numeric grade; the rest —
    frequently the MAJORITY of a fetch — are silently discarded (they can't
    anchor the grade-curve guards). Their price median is a cheap
    ungraded-market sanity anchor + liquidity signal (``n`` = how many raw
    copies actually traded), computed entirely from comps ALREADY fetched (zero
    additional SerpApi calls, per BUI-522's first acceptance criterion).

    Returns ``{"median": float, "n": int}`` or ``None`` when the fetch held no
    grade-less priced comp. Purely INFORMATIONAL: this value does NOT enter the
    priced pool and moves no guard (``too_sparse``/``one_sided``/``too_wide``
    stay exactly as ``build_pool``/``_classify_pool`` computed them — the
    low-weight-inclusion lever in the ticket is a separate, still-gated opt-in).
    Only grade-less comps count; a comp with a parsed grade already priced the
    graded pool, and first-party rows always carry a grade, so they never leak
    into this ungraded signal.
    """
    prices = [float(c["price"]) for c in comps
              if c.get("grade") is None
              and isinstance(c.get("price"), (int, float))]
    if not prices:
        return None
    return {"median": statistics.median(prices), "n": len(prices)}


# ─── Pool-vs-anchor divergence flag (BUI-534) ─────────────────────────────────
#
# The ungraded_anchor above proved itself on its very first live run
# (2026-07-24, 64-book recompute): Batman #251 priced $400-425 off a
# 5.0-6.0-grade pool while the anchor — the raw/ungraded median — read $224.8
# off 36 raw sales. The graded pool was pricing a different market than the
# bulk of actual raw trades: precisely the error class the July hand override
# (BUI-533) corrected manually. Until now the anchor was informational only;
# nothing surfaced unless a human happened to read the fmv_notes token. This
# promotes it to an active (flag-only, never re-pricing) check.
#
# ANCHOR_DIVERGES_PCT = 0.5 (a ±50% band around the anchor median) is derived
# from that incident: fmv_low $400 vs anchor $224.8 is a ~1.78x ratio (78%
# over the anchor) — comfortably past a 1.5x (T=0.5) trip-wire — while still
# leaving headroom for an ordinary, healthy grade premium (a high-grade
# CGC-eligible copy commonly trades well above a raw/mixed-condition median
# without that gap alone being an error). ANCHOR_DIVERGES_MIN_N = 8 mirrors
# CGC_PROXY_MIN_LADDER_COMPS's philosophy: an anchor built on a handful of raw
# sales is noise, not signal, so the check is skipped below this floor rather
# than firing on a thin anchor.
ANCHOR_DIVERGES_PCT = 0.5   # T: half-band the priced range may diverge from the anchor
ANCHOR_DIVERGES_MIN_N = 8   # anchor n floor below which the check is skipped as noise


def anchor_diverges(fmv_low: float | None, fmv_high: float | None,
                    anchor: dict | None) -> bool:
    """BUI-534: True when a priced range sits far outside the ungraded-market
    anchor — ``fmv_low > anchor.median * (1 + T)`` or
    ``fmv_high < anchor.median * (1 - T)``, T = ANCHOR_DIVERGES_PCT.

    FLAG ONLY — same philosophy as BUI-529's cross-check and BUI-530's
    hot-market rule: this never changes fmv_low/fmv_high/max_bid, and a caller
    must never re-derive the comp pool to "resolve" it. Returns False when
    there's nothing to compare (no priced range — a flagged/interpolated-only/
    no-comps book — or no anchor at all) or the anchor is too thin to trust
    (fewer than ANCHOR_DIVERGES_MIN_N raw sales).
    """
    if fmv_low is None or fmv_high is None or not anchor:
        return False
    if anchor.get("n", 0) < ANCHOR_DIVERGES_MIN_N:
        return False
    median = anchor.get("median")
    if not median or median <= 0:
        return False
    return (fmv_low > median * (1 + ANCHOR_DIVERGES_PCT)
            or fmv_high < median * (1 - ANCHOR_DIVERGES_PCT))


# ─── Grade buckets + grade-curve checks (§2, §5, §7 — BUI-306) ────────────────

def bucket_medians(comps: Iterable[dict]) -> dict[float, float]:
    """Grade → median price, one bucket per distinct parsed grade (fmv.md §2).

    The bucket value is the plain median of that grade's prices — median is
    outlier-robust, so no per-bucket IQR trim (matching §2's "compute median
    per bucket"). Comps with no parsed grade or price are ignored. Used by both
    the §5 monotonicity check and the §7 interpolation below.
    """
    buckets: dict[float, list[float]] = {}
    for c in comps:
        g = c.get("grade")
        p = c.get("price")
        if g is None or p is None:
            continue
        buckets.setdefault(float(g), []).append(float(p))
    return {g: statistics.median(ps) for g, ps in buckets.items()}


def bucket_counts(comps: Iterable[dict]) -> dict[float, int]:
    """Grade → number of comps in that bucket (companion to bucket_medians).

    A bucket's comp count gates whether it may anchor a §7 interpolation: a
    single-comp bucket is one mistagged listing away from an entire bracket end
    (the BUI-318 wild-over-bid path), so interpolate_grade_curve refuses to
    bracket off buckets thinner than MIN_BRACKET_COMPS. Counts the same comps
    bucket_medians does (grade + price both present) so the two dicts share keys.
    """
    counts: dict[float, int] = {}
    for c in comps:
        g = c.get("grade")
        p = c.get("price")
        if g is None or p is None:
            continue
        counts[float(g)] = counts.get(float(g), 0) + 1
    return counts


def monotonicity_violations(
    medians: dict[float, float],
) -> list[tuple[float, float]]:
    """Adjacent (lower_grade, higher_grade) pairs whose medians invert (§5).

    Bucket medians should rise monotonically with grade. An adjacent pair where
    the lower-grade median EXCEEDS the higher-grade one signals a suspect comp
    (a damaged low-grade copy priced high, or a mis-graded high-grade copy
    priced low — the Nick Fury #17 7×-for-2-grades outlier). Returned so the
    caller can flag those buckets as SUSPECT instead of silently blending them.
    A single-bucket (or empty) curve has no adjacent pair and never violates.
    """
    grades = sorted(medians)
    return [
        (grades[i], grades[i + 1])
        for i in range(len(grades) - 1)
        if medians[grades[i]] > medians[grades[i + 1]]
    ]


# ─── Cross-grade FMV inversion (BUI-583) ──────────────────────────────────────
#
# `monotonicity_violations` above checks the grade curve WITHIN one comp pool.
# This checks it ACROSS the persisted `fmv` rows of a single comic: same
# comic_id, two grades, and the higher grade priced below the lower one. A 7.0
# copy cannot be worth less than a 4.0 copy of the same book, so one of the two
# pools is wrong. It is the first check that compares two priced ROWS against
# each other, which is why the existing per-pool marks (one_sided / too_wide /
# too_sparse / variant_dropped) structurally cannot catch it — measured over
# the whole live `fmv` table (945 rows, 127 multi-grade comics), all 40
# midpoint-inverted pairs carried flag_reason = NULL.
#
# DELIBERATELY NOT a reuse of `monotonicity_violations`. That function is on the
# CGC-ladder money path (it gates `cgc_ladder_price`), takes a single value per
# grade rather than a low/high band, compares only ADJACENT grades, and applies
# NO magnitude threshold. Teaching it bands + thresholds would change ladder
# pricing to serve an advisory check — so the two stay separate.
#
# THRESHOLDS (measured against the live table, 2026-07-31). An inversion counts
# only when the higher grade's midpoint falls below the lower grade's by BOTH a
# relative and an absolute margin. The two floors do different jobs:
#
#   REL = 0.25  kills trivial relative differences. A higher grade sitting a
#               quarter below a lower grade of the same book is not explicable
#               by ordinary comp noise.
#   ABS = $10   kills ROUNDING noise. `clean_round` snaps to $5 below $50, so a
#               midpoint (the mean of two snapped bounds) moves on a $2.50
#               grid; a $10 gap is beyond what rounding alone can manufacture.
#
# Unthresholded, the sweep returns 40 pairs across 35 comics — dominated by
# $2.50-on-$20 artifacts (Detective Comics #576 at 8.5 = $15-30 vs 9.0 =
# $15-25). At these floors it returns 9 pairs across 8 comics (6.3% of the
# multi-grade cohort), every one visibly implausible, and it still keeps the
# motivating case (X-Men #83: 4.0 = $35-70 vs 7.0 = $5-45).
#
# ADVISORY ONLY. The caller must surface these as a notes token and MUST NOT
# route them into `flag_reason` — `compute_fmv` nulls fmv_low/fmv_high/max_bid
# for any book carrying a flag_reason, so using that slot would SUPPRESS a
# price the ticket requires be left untouched. Which of the two rows is wrong
# is not decided here; both grades are named so a human can compare them.
CROSS_GRADE_INVERSION_REL = 0.25   # higher grade must sit this far below, relatively
CROSS_GRADE_INVERSION_ABS = 10.0   # ...and this far below in dollars


def cross_grade_inversions(
    priced: Iterable[tuple[float, float | None, float | None]],
    rel_floor: float = CROSS_GRADE_INVERSION_REL,
    abs_floor: float = CROSS_GRADE_INVERSION_ABS,
) -> list[tuple[float, float]]:
    """(lower_grade, higher_grade) pairs of one comic whose prices invert.

    `priced` is that comic's persisted rows as (grade, fmv_low, fmv_high). A row
    missing either bound is unpriced (the BUI-44 n=0 stub, or a needs-manual
    book) and is skipped rather than compared — an absent price is not a cheap
    price. Grades are compared by MIDPOINT, so two overlapping bands still
    invert when their centres do; that is what the motivating case needs, since
    X-Men #83's 4.0 ($35-70) and 7.0 ($5-45) bands do overlap.

    ALL pairs are compared, not just grade-adjacent ones: a chain whose every
    adjacent step is individually sub-threshold can still invert end to end.
    Returns pairs sorted by (lower_grade, higher_grade); an empty list means the
    comic's priced rows rise monotonically with grade (or it has fewer than two).
    """
    mids: dict[float, float] = {}
    for grade, low, high in priced:
        if low is None or high is None:
            continue
        mids[float(grade)] = (float(low) + float(high)) / 2.0
    grades = sorted(mids)
    out: list[tuple[float, float]] = []
    for i, g_lo in enumerate(grades):
        mid_lo = mids[g_lo]
        if mid_lo <= 0:
            # A zero/negative lower midpoint can't be undercut by a real price,
            # and would divide by zero in the relative test.
            continue
        for g_hi in grades[i + 1:]:
            gap = mid_lo - mids[g_hi]
            if gap >= abs_floor and (gap / mid_lo) >= rel_floor:
                out.append((g_lo, g_hi))
    return out


def _bracket_interpolate(
    medians: Mapping[float, float], target_grade: float,
    counts: Mapping[float, float] | None = None,
    min_bucket_n: float = MIN_BRACKET_COMPS,
) -> dict | None:
    """Linear-interpolate a price at ``target_grade`` between the nearest
    BRACKETING buckets (one strictly below, one strictly above), or None if the
    target is not bracketed by eligible buckets.

    The shared, money-critical core of ``interpolate_grade_curve`` (§7 raw
    grade-curve) and ``cgc_ladder_price`` (BUI-348 CGC ladder): the
    interpolation formula must live in exactly ONE place so a future correction
    can't silently apply to one pricing path but not the other. ``counts`` /
    ``min_bucket_n`` gate which buckets may anchor (a bucket thinner than
    ``min_bucket_n`` is skipped); ``counts=None`` disables the guard.

    Returns the interpolation inputs (``grade_below``/``grade_above``/
    ``median_below``/``median_above``/``target_price``) so callers can state
    which buckets were used, or None when no eligible bracket exists on a side.
    """
    def _eligible(g: float) -> bool:
        return counts is None or counts.get(g, 0) >= min_bucket_n

    below = [g for g in medians if g < target_grade and _eligible(g)]
    above = [g for g in medians if g > target_grade and _eligible(g)]
    if not below or not above:
        return None
    grade_below = max(below)
    grade_above = min(above)
    median_below = medians[grade_below]
    median_above = medians[grade_above]
    target_price = (
        median_below
        + (target_grade - grade_below) / (grade_above - grade_below)
        * (median_above - median_below)
    )
    return {
        "grade_below": grade_below,
        "grade_above": grade_above,
        "median_below": median_below,
        "median_above": median_above,
        "target_price": target_price,
    }


def interpolate_grade_curve(
    medians: Mapping[float, float], target_grade: float,
    counts: Mapping[float, float] | None = None,
    min_bucket_n: float = MIN_BRACKET_COMPS,
) -> dict | None:
    """§7 linear interpolation between the nearest BRACKETING bucket medians.

    Returns None unless there is at least one bucket strictly BELOW and one
    strictly ABOVE ``target_grade`` — a genuine bracket, never extrapolation
    (so a one-sided pool, whose comps all sit on one side of the target, yields
    None and stays needs_manual). Also returns None when a bucket exists exactly
    AT ``target_grade``: direct comps at the target are strictly better evidence
    than a bracket smeared across it, so a pool holding them must NOT be
    silently re-priced off distant grades (that mispriced X-Men #96 6× high in
    testing) — it stays flagged for the direct-comp / manual path instead. Uses
    the nearest bracketing bucket on each side and applies the exact formula
    from fmv.md §7:

        target_price = median_below
            + (target_grade - grade_below) / (grade_above - grade_below)
              * (median_above - median_below)

    ``counts`` (BUI-318 thin-bracket money guard): when supplied (grade → comp
    count, e.g. from ``bucket_counts``), only buckets holding at least
    ``min_bucket_n`` comps are eligible to serve as a bracketing anchor. A
    single-comp bucket is one mistagged listing away from being an entire
    bracket end, which can smear a wild over-bid across the target; such a
    bucket is skipped, and if that leaves no eligible bracket on a side the
    whole interpolation is suppressed (returns None → the pool stays
    needs_manual). ``counts=None`` disables the guard entirely — the back-compat
    path for callers that pass raw medians without a matching count map.

    Returns the interpolation inputs alongside ``target_price`` so the caller
    can state EXPLICITLY which buckets were used (§7's state-explicitly rule).
    """
    if target_grade in medians:
        return None
    return _bracket_interpolate(medians, target_grade, counts, min_bucket_n)


# ─── IQR trim + quartiles (inclusive method) ──────────────────────────────────

def _iqr_bounds(prices: list[float]) -> tuple[float, float] | None:
    """Q1 - 1.5*IQR / Q3 + 1.5*IQR bounds, or None if too few points (n<3)."""
    if len(prices) < 3:
        return None
    s = sorted(prices)
    qs = statistics.quantiles(s, n=4, method="inclusive")
    q1, q3 = qs[0], qs[2]
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


def iqr_trim(prices: list[float]) -> list[float]:
    """Drop values outside Q1 - 1.5*IQR to Q3 + 1.5*IQR."""
    bounds = _iqr_bounds(prices)
    if bounds is None:
        return list(prices)
    lo, hi = bounds
    return [p for p in sorted(prices) if lo <= p <= hi]


def quartile(prices: list[float], q: float) -> float:
    """Inclusive-method quantile at fraction q (0..1)."""
    if len(prices) == 1:
        return prices[0]
    qs = statistics.quantiles(sorted(prices), n=100, method="inclusive")
    # qs has 99 cut points (between n=100 buckets); index i = (i+1)/100 quantile
    idx = max(0, min(98, round(q * 100) - 1))
    return qs[idx]


# ─── Recency weighting (BUI-287 U2) ───────────────────────────────────────────
#
# fmv_math stays a pure, clock-free function: the reference date used to age
# every comp is the NEWEST `sold_date` found *within the pool being priced*
# (never datetime.now()/date.today()). The newest comp always gets weight 1.0;
# older comps decay by exp(-ln2 * age_days / RECENCY_HALF_LIFE_DAYS). A comp
# with a missing or unparseable `sold_date` gets NEUTRAL weight 1.0 — most
# existing comps/tests carry no date at all, so an all-neutral-weight pool
# must price byte-for-byte identically to the pre-U2 unweighted math.

RECENCY_HALF_LIFE_DAYS = 75  # empirical starting point (60-90 day range)

_SOLD_DATE_PREFIX_RE = re.compile(r"^\s*sold\s+", re.IGNORECASE)
_SOLD_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y")  # SerpApi free text, e.g. "Oct 12, 2026"


def _parse_sold_date(value: object) -> date | None:
    """Parse a comp's `sold_date` into a comparable date, or None.

    Handles the two known shapes in this codebase: SerpApi's "Sold Mon DD,
    YYYY" free text (apps/ebay/src/sold_comps.py `parse_comp`) and first-party
    comps' ISO-8601 `resolved_at` timestamp (apps/fmv/src/fmv_runner.py).
    Missing, blank, or unparseable values return None so the caller can fall
    back to neutral weight rather than crash or guess.
    """
    if not isinstance(value, str):
        return None
    text = _SOLD_DATE_PREFIX_RE.sub("", value).strip()
    if not text:
        return None
    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass
    for fmt in _SOLD_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _recency_weight(sold_date: date | None, reference: date) -> float:
    """Exponential-decay weight for one comp relative to `reference`.

    `reference` is the newest sold_date already found in the pool (see
    `_recency_weights`) — never a live clock. A comp with no parseable date
    gets weight 1.0 (neutral); a comp dated after `reference` (shouldn't
    happen since reference is the max, but guards float/rounding edge cases)
    also gets 1.0 rather than a weight > 1.0.
    """
    if sold_date is None:
        return 1.0
    age_days = (reference - sold_date).days
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)


def _recency_weights(pool_comps: list[dict]) -> list[float]:
    """Return one weight per comp in `pool_comps`, in the same order.

    Reference date = newest parseable `sold_date` in THIS pool (fully
    deterministic, no clock). If no comp in the pool has a parseable date,
    every weight is the neutral 1.0 — the degenerate no-date case required
    to reduce exactly to the pre-U2 unweighted result.
    """
    parsed = [_parse_sold_date(c.get("sold_date")) for c in pool_comps]
    known = [d for d in parsed if d is not None]
    if not known:
        return [1.0] * len(pool_comps)
    reference = max(known)
    return [_recency_weight(d, reference) for d in parsed]


def _weights_equal(weights: list[float]) -> bool:
    """True if every weight is (approximately) the same value.

    Covers both the no-date pool (all neutral 1.0) and an all-same-date pool
    (all comps age-0 relative to the newest → all 1.0) — the two degenerate
    cases that must reduce EXACTLY to the unweighted quantile functions.
    """
    if not weights:
        return True
    first = weights[0]
    return all(abs(w - first) < 1e-9 for w in weights)


def weighted_quartile(prices: list[float], weights: list[float], q: float) -> float:
    """Weighted analog of `quartile`.

    When every weight is equal (degenerate: no dates, or all-same-date pool),
    delegates to the exact unweighted `quartile` — guaranteed byte-identical,
    not just numerically close, which is what keeps the existing golden
    fixture and no-date test suite green (BUI-287 U2).

    Otherwise uses the standard "weighted percentile" midpoint interpolation:
    for sorted (price, weight) pairs, comp i sits at cumulative-weight
    fraction (S_i - w_i/2) / W, where S_i is the cumulative weight through i
    and W is the total weight; `q` is linearly interpolated between the two
    bracketing comps (flat-extrapolated past the first/last comp's position).
    Unlike a knot placement anchored at exactly 0/1 (e.g. a naive weighted
    generalization of the type-7 method used for the unweighted case), this
    lets a 2-comp pool's weight *ratio* actually move the estimate — required
    for the recency-weighting direction test (a fresher, higher-weighted comp
    must pull the quantile toward it even with only two comps).
    """
    if len(prices) == 1:
        return prices[0]
    if _weights_equal(weights):
        return quartile(prices, q)
    pairs = sorted(zip(prices, weights), key=lambda pw: pw[0])
    vals = [p for p, _ in pairs]
    ws = [w for _, w in pairs]
    n = len(vals)
    total = sum(ws)
    cum = 0.0
    positions = []
    for w in ws:
        cum += w
        positions.append((cum - w / 2) / total)
    if q <= positions[0]:
        return vals[0]
    if q >= positions[-1]:
        return vals[-1]
    for i in range(1, n):
        if positions[i] >= q:
            p0, p1 = positions[i - 1], positions[i]
            v0, v1 = vals[i - 1], vals[i]
            frac = (q - p0) / (p1 - p0) if p1 > p0 else 0.0
            return v0 + frac * (v1 - v0)
    return vals[-1]


def weighted_median(prices: list[float], weights: list[float]) -> float:
    """Weighted analog of statistics.median.

    Degenerates EXACTLY to `statistics.median` when every weight is equal
    (mirrors why `compute_fmv` calls statistics.median directly rather than
    `quartile(prices, 0.5)` for the unweighted case — quartile's 99-cutpoint
    grid can disagree with the true median on small/even-n pools).
    """
    if len(prices) == 1:
        return prices[0]
    if _weights_equal(weights):
        return statistics.median(prices)
    return weighted_quartile(prices, weights, 0.5)


def cv(prices: list[float]) -> float | None:
    """Coefficient of variation = stdev / median. None if undefined."""
    if len(prices) < 2:
        return None
    med = statistics.median(prices)
    if med == 0:
        return None
    return statistics.stdev(prices) / med


# ─── Confidence rubric ────────────────────────────────────────────────────────

def confidence_label(n: float, cv_value: float | None) -> str:
    """Per the rubric in /comic:fmv § 8.

    `n` is the EFFECTIVE sample size (sum of recency weights, BUI-287 U2),
    not necessarily the raw trimmed-pool count — a pool of many stale comps
    can no longer claim HIGH purely on raw count. When every comp carries
    neutral weight 1.0 (no dates, or all-same-date), effective n equals the
    raw count exactly, so every pre-U2 caller/test is unaffected.
    """
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
# BUI-318 interpolated-LOW haircut: a §7 interpolated price is a single point
# estimate off a bracket (never a real direct comp) and is always carried at LOW
# fmv-confidence, but fmv-confidence alone never haircuts the bid (BUI-51: only a
# photo grade_confidence does). So absent a grade_confidence an interpolated book
# would still bid at 0.80× — the residual over-bid path flagged in BUI-318. Cap
# the factor for interpolated books at this LOW-tier value (== bid_factor's LOW
# rung) so a thin interpolated estimate never sets a full-confidence bid cap.
INTERPOLATED_BID_FACTOR = 0.60

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

def _clean_step(value: float) -> int:
    """The clean-rounding step at ``value``: $5 below $50, $10 from $50–$200,
    $25 above. Factored out of ``clean_round`` so the minimum-range-width guard
    (BUI-528) can size its last-resort one-step nudge off the SAME step ladder
    the rounding uses — the money-critical step definition stays in one place."""
    if value < 50:
        return 5
    if value < 200:
        return 10
    return 25


def clean_round(value: float) -> int:
    """Round to clean step: $5 below $50, $10 from $50–$200, $25 above."""
    step = _clean_step(value)
    return int(round(value / step) * step)


# ─── CGC-proxy tier (BUI-348) ─────────────────────────────────────────────────
#
# For a vintage KEY, genuine raw sold comps are sparse and rarely carry a
# parseable numeric grade, so the raw grade-window pool comes up n=0 and the
# book returns needs_manual. The single most reliable signal for a key is the
# CGC/CBCS slab-price ladder: slab listings ALWAYS carry a certified numeric
# grade in the title (parse_grade("… CGC 6.5 …") == 6.5), so a graded comp pool
# builds a clean grade→price ladder. A raw copy trades at a discount to the
# equivalent slab (no certification premium + grade risk), so:
#
#     raw_fmv ≈ proxy_factor × CGC_slab_price[target_grade]
#
# PROXY FACTOR (money-critical). Empirically anchored on the ASM #50 (1967, 1st
# Kingpin) incident that motivated this tier: eBay CGC 6.5 sold ~$1,200 while
# genuine raw 6.5 copies sold $635–$700 → an observed raw/slab ratio of ~0.53–
# 0.58, cross-checked by the hand-priced $600–$680 band. We publish a BAND
# (fmv_low = LOW factor × slab, fmv_high = HIGH factor × slab) rather than a
# single point because the discount itself is uncertain, and we lean
# conservative (0.50–0.55) so the bid cap never rides the top of the observed
# ratio.
#
# IMPORTANT — this factor is calibrated to eBay CGC *sold* prices, which is what
# the graded ebay-sold-comps query returns. It is NOT the same reference price
# as fmv.md §7a's manual ladder, which reads Heritage/GoCollect *realized*
# prices and applies a smaller 10–25% discount (raw ≈ 0.75–0.90 × realized).
# The two are different multipliers for two different slab-price sources, not a
# contradiction: eBay CGC "sold" asks tend to sit above Heritage hammer, so the
# raw discount off eBay CGC is correspondingly larger. This tier automates the
# eBay-CGC basis; §7a documents the Heritage basis.
CGC_PROXY_FACTOR_LOW = 0.50
CGC_PROXY_FACTOR_HIGH = 0.55

# Value floor: the CGC-proxy method is unreliable for cheap books (fmv.md §7a —
# "< $200 do not use": certification cost is proportionally large and raw buyers
# discount heavily and erratically). Require the ladder's slab price at the
# target grade to clear this floor before pricing off it; below it the book
# stays needs_manual rather than getting a shaky proxy number. At the LOW proxy
# factor a $400 slab implies a $200 raw — the documented floor.
CGC_PROXY_MIN_SLAB_PRICE = 400.0

# Ladder-trust floor: a ladder built from one or two stray slab listings is too
# thin to anchor a price. Require at least this many graded comps across the
# whole ladder before trusting it (a single-grade exact match is still allowed —
# certified grades are firm — but the ladder as a whole must not be a fluke).
CGC_PROXY_MIN_LADDER_COMPS = 3

# A proxy-derived price is inherently uncertain (the discount is an estimate off
# a different market), so confidence is capped at MEDIUM-LOW (fmv.md §7a step 3)
# regardless of how many slab comps were found …
CGC_PROXY_CONFIDENCE = "MEDIUM-LOW"
# … and the bid cap is haircut to the MEDIUM-LOW rung. bid_factor()'s haircut
# only engages when a photo grade_confidence is present (BUI-51); a proxy book
# usually has none, so the MEDIUM-LOW *label* alone would still bid at the full
# 0.80×. Capping the factor here makes the MEDIUM-LOW confidence actually
# constrain the bid — mirrors the BUI-318 interpolated-LOW haircut, one rung up.
CGC_PROXY_BID_FACTOR = 0.70

# Within-bucket outlier robustness floor (BUI-355): the smallest bucket size
# whose median actually resists one outlier. median-of-1 IS the comp and
# median-of-2 is just the mean of two — one $5000 mistag in an n=2 bucket
# drags the "median" to the midpoint, with zero robustness. Only at n>=3 does
# the median discard an extreme value. Exact target-grade buckets thinner than
# this are subject to the BUI-349 envelope-sanity clamp below. Distinct from
# MIN_BRACKET_COMPS (=2), which gates which buckets may ANCHOR an
# interpolation — that semantic is unchanged.
OUTLIER_ROBUST_BUCKET_N = 3


def _cgc_ladder_price_and_clamp(
    ladder: Mapping[float, float], target_grade: float,
    counts: Mapping[float, float] | None = None,
    min_bucket_n: float = MIN_BRACKET_COMPS,
) -> tuple[float | None, bool]:
    """Shared core of ``cgc_ladder_price``: returns ``(price, envelope_clamped)``.

    ``envelope_clamped`` (BUI-369) is True only when the BUI-349/BUI-355
    envelope-sanity clamp actually LOWERED the exact-bucket price (the
    envelope bound was strictly below the raw exact value). When the
    envelope is at or above the exact value, ``min(exact, envelope)``
    returns ``exact`` unchanged, so there is nothing to flag — the notes
    would otherwise falsely suggest a clamp happened when the price is
    simply the direct exact-bucket anchor.

    Split out from ``cgc_ladder_price`` so ``cgc_proxy_fmv`` can surface the
    flag in ``cgc_ladder`` for the notes builder (fmv.md §7a: an auditor
    seeing ``slab_price`` disagree with ``ladder[target]`` needs to know
    that's the clamp, not a bug, or they could "fix" the cap upward and
    defeat the guard) — without changing ``cgc_ladder_price``'s public
    scalar return shape. See that function's docstring for the full clamp
    rationale.
    """
    if not ladder:
        return None, False
    if target_grade in ladder:
        exact = ladder[target_grade]
        if (counts is not None
                and counts.get(target_grade, 0)
                < max(min_bucket_n, OUTLIER_ROBUST_BUCKET_N)):
            envelope = _bracket_interpolate(
                ladder, target_grade, counts, min_bucket_n
            )
            if envelope is not None:
                clamped_price = min(exact, envelope["target_price"])
                return clamped_price, clamped_price < exact
        return exact, False
    bracket = _bracket_interpolate(ladder, target_grade, counts, min_bucket_n)
    return (bracket["target_price"], False) if bracket else (None, False)


def cgc_ladder_price(ladder: Mapping[float, float], target_grade: float,
                     counts: Mapping[float, float] | None = None,
                     min_bucket_n: float = MIN_BRACKET_COMPS) -> float | None:
    """Slab price at ``target_grade`` from a CGC/CBCS grade→price ladder.

    ``ladder`` is grade → median slab price (build it with ``bucket_medians``
    over a GRADED comp pool). Resolution:

    * EXACT bucket present → return it directly (a certified slab comp at the
      exact target grade is the strongest possible anchor — unlike the raw §7
      interpolation, which deliberately returns None on an exact match because a
      direct RAW comp there is better evidence than a bracket smeared across it;
      here the slab ladder IS the evidence).
    * Else LINEAR INTERPOLATION between the nearest bracketing buckets (via the
      shared ``_bracket_interpolate`` — one formula for both pricing paths).
    * Else (target below the whole ladder or above it) → None. The proxy NEVER
      extrapolates past the observed ladder — pricing a raw 6.5 off a 9.6-only
      ladder would be a wild guess — so such a book stays needs_manual.

    ``counts``/``min_bucket_n`` gate which buckets may anchor an INTERPOLATION
    (a bucket thinner than ``min_bucket_n`` is skipped), mirroring
    ``interpolate_grade_curve``; the default ``MIN_BRACKET_COMPS`` (≥2) matches
    the raw §7 thin-bracket money guard (BUI-318) — a lone slab is one premium/
    mistagged listing away from smearing a wild over-bid across an interpolated
    span. The EXACT-match bucket is exempt from ``min_bucket_n`` (a single
    certified slab AT the target grade is a direct anchor, not a span
    extrapolation) so a genuinely sparse key (ASM #50's lone 6.5) can still be
    priced; the ladder-wide comp-count floor + the monotonicity guard in
    ``cgc_proxy_fmv`` are the ladder-wide money-safety nets.

    BUI-349 envelope-sanity clamp: an exact bucket too thin to have
    within-bucket outlier protection (fewer than ``OUTLIER_ROBUST_BUCKET_N``
    comps — BUI-355 widened this from ``< min_bucket_n``, i.e. n=1, to also
    cover n=2, whose median-of-2 is just the mean of two and has zero
    robustness) can let one off-trend-high listing set a too-high cap that the
    monotonicity guard misses (that guard only refuses a bucket priced above
    the NEXT one; a comp sitting BETWEEN its neighbors yet above the linear
    trend between them stays monotone). So when trustworthy (≥ ``min_bucket_n``)
    buckets BRACKET the target, bound a non-robust exact value from ABOVE by
    the linear envelope those neighbors imply — ``min(exact, envelope)``. This
    can only LOWER the price, never raise it (money-safe), and preserves the
    sparse-key case: ASM #50's lone $1200 6.5 sits below its $1686 5.0–7.0
    envelope, so it is unchanged; only an off-trend-high thin comp is clamped
    down. A robust (n≥3) exact bucket (its median already discards an extreme)
    and a thin exact bucket with no eligible bracket (no envelope to check —
    the irreducible sparse-key case) both stay direct anchors. Note the two
    thresholds stay distinct on purpose: ``min_bucket_n`` (≥2) still gates
    which buckets may ANCHOR the envelope; the exact bucket NEEDS the envelope
    check when thinner than ``max(min_bucket_n, OUTLIER_ROBUST_BUCKET_N)`` —
    the ``max`` keeps the trigger tracking a caller-raised ``min_bucket_n``
    (>3), so BUI-355 can never RAISE a price vs the old ``< min_bucket_n``
    trigger for ANY caller (a no-op for the default; only-lowers by
    construction). ``counts=None`` disables the clamp (a caller passing bare
    medians can't tell a thin bucket from a thick one), preserving the
    exact-anchor behavior.
    """
    price, _ = _cgc_ladder_price_and_clamp(
        ladder, target_grade, counts, min_bucket_n
    )
    return price


def cgc_proxy_fmv(graded_comps: list[dict], target_grade: float,
                  grade_confidence: str | None = None) -> dict | None:
    """Price a raw copy off a CGC/CBCS slab ladder (BUI-348), or None.

    Returns a pricing dict shaped like ``compute_fmv``'s output (same keys, so
    the caller can drop it in place of a raw needs_manual result) with an added
    ``cgc_proxy: True`` marker and a ``cgc_ladder`` summary for the notes. The
    band is ``[LOW, HIGH] factor × slab_price[target_grade]``, confidence is
    forced to MEDIUM-LOW, and the bid factor is capped at CGC_PROXY_BID_FACTOR.

    Returns None (caller keeps the raw needs_manual result) when the proxy can't
    be trusted:
      * fewer than CGC_PROXY_MIN_LADDER_COMPS graded comps (ladder too thin),
      * the target grade is outside the ladder's observed range (no extrapolation),
      * or the slab price is below CGC_PROXY_MIN_SLAB_PRICE (cheap book — the
        method doesn't apply, fmv.md §7a's >$200 floor).
    """
    ladder = bucket_medians(graded_comps)
    counts = bucket_counts(graded_comps)
    total = sum(counts.values())
    if total < CGC_PROXY_MIN_LADDER_COMPS:
        return None
    # Money guard: a NON-MONOTONIC ladder (a lower grade priced at or above a
    # higher grade) signals a polluted bucket — a premium/qualified/variant slab
    # or a mistagged price — and interpolating across an inversion yields a
    # nonsense anchor. The raw §7 path only WARNS (suspect_buckets) because it
    # still prices off real direct comps; the proxy prices the WHOLE band off
    # this ladder, so an inverted ladder is not safe to price from — refuse it
    # and leave the book needs_manual rather than emit a suspect bid cap.
    if monotonicity_violations(ladder):
        return None
    slab, envelope_clamped = _cgc_ladder_price_and_clamp(
        ladder, target_grade, counts=counts
    )
    if slab is None or slab < CGC_PROXY_MIN_SLAB_PRICE:
        return None

    fmv_low = clean_round(slab * CGC_PROXY_FACTOR_LOW)
    fmv_high = clean_round(slab * CGC_PROXY_FACTOR_HIGH)
    med = clean_round(slab * (CGC_PROXY_FACTOR_LOW + CGC_PROXY_FACTOR_HIGH) / 2)
    # Take the MORE conservative of the proxy cap and any grade_confidence
    # haircut, so a present-and-lower photo confidence still wins (as with §7).
    factor = min(bid_factor(CGC_PROXY_CONFIDENCE, grade_confidence),
                 CGC_PROXY_BID_FACTOR)
    max_bid = clean_round(fmv_high * factor)

    return {
        # n reflects the GRADED ladder's comp count — not a raw pool. It is the
        # evidence behind the proxy, surfaced for traceability; it does not (and
        # must not) lift the capped MEDIUM-LOW confidence.
        "n": total,
        "effective_n": float(total),
        "window": None,
        "flag_reason": None,          # priced (a bid-able band), not needs_manual
        "grade_span": None,
        "fmv_low": fmv_low,
        "fmv_high": fmv_high,
        "median": med,
        "max_bid": max_bid,
        "cv": None,
        "cv_pct": "n/a",
        "confidence": CGC_PROXY_CONFIDENCE,
        "grade_confidence": grade_confidence,
        "bid_factor": factor,
        "trimmed_pool": [],
        "interpolated": False,
        "interpolation": None,
        "suspect_buckets": [],
        # BUI-348 markers: distinguish a proxy band from a real raw range and
        # carry the ladder anchor for the "CGC proxy" notes token.
        "cgc_proxy": True,
        "cgc_ladder": {
            "slab_price": slab,
            "target_grade": target_grade,
            "factor_low": CGC_PROXY_FACTOR_LOW,
            "factor_high": CGC_PROXY_FACTOR_HIGH,
            "ladder": dict(sorted(ladder.items())),
            # BUI-369: True when the BUI-349/BUI-355 envelope-sanity clamp
            # lowered `slab_price` below the raw `ladder[target_grade]`
            # value — so `_build_notes` can state it explicitly rather than
            # leaving an unexplained contradiction an auditor might "fix"
            # upward (defeating the guard). See
            # `_cgc_ladder_price_and_clamp`'s docstring for when this fires.
            "envelope_clamped": envelope_clamped,
        },
        # BUI-522: a proxy band is priced off the slab ladder, not raw comps, so
        # it carries no ungraded-market anchor. Key present (== None) for shape
        # parity with compute_fmv's output.
        "ungraded_anchor": None,
        # BUI-534: no anchor above → nothing to diverge from. Key present
        # (== False) for shape parity with compute_fmv's output.
        "anchor_diverges": False,
        # BUI-529: the always-on cross-check only runs on a book the RAW math
        # priced (see cgc_cross_check below) — a proxy band already IS the
        # slab-derived price, so a raw-vs-slab comparison is meaningless here.
        # Key present (== None) for shape parity with compute_fmv's output.
        "cgc_cross_check": None,
    }


# ─── Always-on vintage cross-check (BUI-529) ─────────────────────────────────
#
# Promotes the CGC-proxy heuristic from a rescue tier (fires only when the raw
# math produced NO number at all — cgc_proxy_fmv above) to a validation
# cross-check that ALSO runs on a vintage book that DID price, when the price
# is thin or uncertain (n<5 or LOW confidence — see fmv_runner's
# _is_thin_or_low_confidence_priced). The worst real misses in the 2026-07-24
# FMV review were exactly this shape: a confident-LOOKING raw number a slab
# ladder would have flagged as way off (Ghost Rider #3 cleared 7.8x fmv_high,
# Moon Knight #12 5.3x, Thor #149 3.1x).
#
# Unlike cgc_proxy_fmv, this NEVER replaces the priced fmv — it only computes
# what the slab ladder implies and reports how far that sits from the raw
# median, so the caller can surface a structured flag. Money-safety: a
# cross-check that silently re-priced a book would itself become a new
# mis-pricing vector with none of cgc_proxy_fmv's own guards (envelope clamp,
# monotonicity, no-extrapolation) re-derived for a blend; flagging is the
# ticket's explicit, unambiguous acceptance criterion, blending is not
# specified, so this deliberately stops at "flag".
#
# The $400 CGC_PROXY_MIN_SLAB_PRICE floor is a PRICING guard (a proxy price
# below it is too shaky to bid off) — a cross-check only compares two numbers
# that already exist, so it does NOT apply that floor (BUI-529's explicit
# "drop the $400 slab floor in cross-check mode" instruction). Every other
# ladder-trust guard (min comp count, no extrapolation past the observed
# range, no monotonicity violations) is reused unchanged from cgc_proxy_fmv's
# own path, via the same shared `_cgc_ladder_price_and_clamp`.
CGC_CROSS_CHECK_DIVERGENCE_PCT = 0.40


def cgc_cross_check(graded_comps: list[dict], target_grade: float,
                    raw_median: float | None) -> dict | None:
    """Compare a priced vintage book's raw median against its CGC/CBCS ladder.

    Returns None when there's nothing to compare (``raw_median`` is None/0) or
    the ladder can't be trusted (fewer than ``CGC_PROXY_MIN_LADDER_COMPS``
    graded comps, a non-monotonic ladder, or the target grade falls outside the
    ladder's observed range — the proxy factor never extrapolates). Otherwise
    returns::

        {"slab_price": float, "target_grade": float, "implied_raw": float,
         "raw_median": float, "divergence_pct": float, "diverges": bool,
         "n": int, "ladder": dict, "envelope_clamped": bool}

    ``implied_raw`` is the slab price at the midpoint of the 0.50-0.55 proxy
    factor (matching cgc_proxy_fmv's ``median`` field). ``diverges`` is True
    when ``implied_raw`` differs from ``raw_median`` by more than
    ``CGC_CROSS_CHECK_DIVERGENCE_PCT``. This function never mutates or
    replaces a priced fmv — the caller decides what (if anything) to do with
    the result.
    """
    if not raw_median:
        return None
    ladder = bucket_medians(graded_comps)
    counts = bucket_counts(graded_comps)
    total = sum(counts.values())
    if total < CGC_PROXY_MIN_LADDER_COMPS:
        return None
    # Same money guard as cgc_proxy_fmv: an inverted ladder is not safe evidence
    # to compare against, even just for a flag.
    if monotonicity_violations(ladder):
        return None
    slab, envelope_clamped = _cgc_ladder_price_and_clamp(
        ladder, target_grade, counts=counts
    )
    if slab is None:
        return None
    implied_raw = clean_round(
        slab * (CGC_PROXY_FACTOR_LOW + CGC_PROXY_FACTOR_HIGH) / 2
    )
    divergence_pct = abs(implied_raw - raw_median) / raw_median
    return {
        "slab_price": slab,
        "target_grade": target_grade,
        "implied_raw": implied_raw,
        "raw_median": raw_median,
        "divergence_pct": round(divergence_pct, 4),
        "diverges": divergence_pct > CGC_CROSS_CHECK_DIVERGENCE_PCT,
        "n": total,
        "ladder": dict(sorted(ladder.items())),
        "envelope_clamped": envelope_clamped,
    }


# ─── Minimum range width on collapsed pools (BUI-528) ────────────────────────
#
# 118/862 persisted FMV rows have fmv_low == fmv_high — a point estimate off a
# pool whose real dispersion was swallowed by clean_round, masquerading as a
# range. Several of the worst real-outcome misses are exactly these: a $5/$5
# book cleared $39, a $10/$10 cleared $28.50. A degenerate range makes
# max_bid = 0.80 × point, which systematically under-bids.
#
# The fix is deliberately scoped to the actual harm — a ZERO-WIDTH range on a
# non-degenerate pool — rather than a blanket floor on every pool. An earlier
# always-widen version disturbed healthy pools sitting near a clean-round step
# boundary and, worse, pushed a thin pool's fmv_high ABOVE its priciest real
# comp (and erased the recency direction signal on 2-comp pools). So we only
# act when clean_round has actually COLLAPSED fmv_low == fmv_high while the
# pool's prices genuinely dispersed (cv > 0), and we bound the widened band by
# the observed [min, max] so it never claims a value beyond a real sale. The
# width we open is scaled by the pool's own uncertainty:
#
#   * CV     — a genuinely dispersed pool earns a wider split; a tight one less.
#   * window — comps gathered across a wider grade window carry more
#              grade-mismatch uncertainty, so widen a little per step.
#   * n      — a thinner trimmed pool is less reliable, so widen a little per
#              comp below the narrow-pool target.
#
# A GENUINELY DEGENERATE pool — a single comp (cv is None) or two+ identical
# prices (cv == 0) — is left a true point: there is no dispersion to reveal and
# fabricating one would be dishonest (the ticket's "unless the pool is genuinely
# degenerate" carve-out, which falls out of the cv > 0 gate for free).
MIN_RANGE_CV_COEF = 0.5          # half the CV becomes the min half-width fraction
MIN_RANGE_WINDOW_COEF = 0.015    # + per ±0.5 grade-window step beyond the narrow default
MIN_RANGE_THIN_COEF = 0.015      # + per comp the trimmed pool sits below MIN_NARROW_POOL
MIN_RANGE_HALF_WIDTH_CAP = 0.30  # never open the band beyond ±30% of the median


def _min_range_half_width_fraction(cv_value: float, window: float, n: int) -> float:
    """Half-width, as a fraction of the median, to open a collapsed band to.

    Combines the CV, grade-window, and thinness signals (see the section
    comment) and caps the total so the split stays a sane fraction of median.
    Caller guarantees ``cv_value`` is a real positive float (a degenerate pool
    is filtered upstream), so the fraction is always ≥ the CV term > 0.
    """
    window_excess = max(0.0, (window - DEFAULT_GRADE_WINDOW) / GRADE_WINDOW_STEP)
    thin_deficit = max(0, MIN_NARROW_POOL - n)
    frac = (MIN_RANGE_CV_COEF * cv_value
            + MIN_RANGE_WINDOW_COEF * window_excess
            + MIN_RANGE_THIN_COEF * thin_deficit)
    return min(frac, MIN_RANGE_HALF_WIDTH_CAP)


def _widen_collapsed_range(
    median: float, cv_value: float, window: float, n: int,
    price_min: float, price_max: float,
) -> tuple[int, int]:
    """Split a clean-round-collapsed but non-degenerate priced band (BUI-528).

    The weighted quartiles rounded to a single clean point, yet the pool's
    prices genuinely dispersed (cv > 0). Reveal a real range: widen
    symmetrically around ``median`` to the CV/window/n-scaled minimum half-width
    (computed in raw dollars, i.e. before the final clean_round), BOUNDED by the
    observed ``[price_min, price_max]`` so the band never claims a value beyond
    a real sale. If a coarse price step still collapses the widened band, force
    exactly one clean step of separation, toward the side with more observed
    room — capped at the priciest observed comp on the up side, and defaulting
    to the money-safe down side (which never lifts the bid cap). Guarantees the
    returned fmv_low < fmv_high.
    """
    min_half = median * _min_range_half_width_fraction(cv_value, window, n)
    fmv_low = clean_round(max(price_min, median - min_half))
    fmv_high = clean_round(min(price_max, median + min_half))
    if fmv_low < fmv_high:
        return fmv_low, fmv_high
    # Sub-one-step spread at a coarse price step: force one clean step apart.
    v = fmv_high  # == fmv_low here
    step = _clean_step(v)
    up, down = v + step, v - step
    if (price_max - median) > (median - price_min) and up <= clean_round(price_max):
        return v, up            # dispersion sits above the median → reveal upside
    if down >= 0:
        return down, v          # money-safe: lower the low, bid cap unchanged
    return v, up


# ─── End-to-end: comps → FMV summary ─────────────────────────────────────────

def compute_fmv(comps: list[dict], target_grade: float,
                grade_confidence: str | None = None,
                max_window: float | None = None,
                forced_flag_reason: str | None = None) -> dict:
    """Take a deduped, hard-excluded comp list and return the FMV summary.

    `grade_confidence` (BUI-51) is the photo-coverage confidence from
    /comic:grade (high|medium|low). When present, the max bid is haircut by
    the more conservative of it and the comp-pool confidence (see bid_factor).
    When None, the bid stays at BASE_BID_FACTOR — back-compat for manual or
    already-graded books.

    `max_window` (BUI-86) caps how far the pool widens; the caller threads
    `--grade-window` through here. It only changes reach, never the guards.

    `forced_flag_reason` (BUI-588) is a needs-manual reason the POOL cannot
    show, supplied by the caller: something about how the comps were FETCHED
    makes them not directly comparable to the book being priced. The one such
    reason today is `"variant_dropped"` — ebay-sold-comps found nothing until it
    dropped the book's variant term, so the pool prices the base cover instead.
    It applies only when the pool's own diagnostics found nothing (a real
    `_classify_pool` reason is more actionable and keeps precedence), and from
    there it flows through the ordinary flagged-book path, so the invariant
    "flag_reason set ⇒ fmv_low/fmv_high/max_bid are None and confidence is LOW"
    holds for it exactly as for one_sided/too_wide/too_sparse. None — every
    existing caller — is byte-for-byte pre-BUI-588 behavior.

    Output shape:
    {
      "n": int,                        # trimmed pool size (raw count)
      "effective_n": float,            # sum of recency weights (BUI-287 U2);
                                        # == n when every comp has neutral weight
      "window": float,                 # window the pool was built at (≤ max_window)
      "flag_reason": str | None,       # one_sided | too_wide | too_sparse (BUI-86)
                                        # | variant_dropped (BUI-588) | None
      "grade_span": float | None,      # max(grade) - min(grade) over the pool
      "fmv_low": int | None,           # weighted Q25, clean-rounded (None if flagged/no-comps)
      "fmv_high": int | None,          # weighted Q75, clean-rounded
      "median": int | None,            # weighted median, clean-rounded
      "max_bid": int | None,           # bid_factor × fmv_high, clean-rounded
      "cv": float | None,              # raw CV (not %)
      "cv_pct": str,                   # human "27%" or "n/a"
      "confidence": str,               # HIGH | MEDIUM-HIGH | MEDIUM | MEDIUM-LOW | LOW
      "grade_confidence": str | None,  # echoed back for traceability
      "bid_factor": float,             # the multiplier actually applied
      "trimmed_pool": list[float],     # for debugging / display
      "interpolated": bool,            # §7 grade-curve interp was applied (BUI-306)
      "interpolation": dict | None,    # {grade/median_below, _above, target_price}
      "suspect_buckets": list,         # §5 monotonicity violations [[lo,hi],...]
    }

    A `flag_reason` book is "needs manual pricing" (BUI-86): it emits no
    bid-able number and its confidence is forced to LOW so the persisted
    fmv_confidence stays `low`. Priceability is derived downstream from
    `flag_reason is not None` — there is no separate `priceable` field.

    BUI-306 (fmv.md §7): a `one_sided`/`too_wide` pool that still has real
    grade buckets bracketing the target is priced by LINEAR INTERPOLATION
    between the nearest bracketing bucket medians instead of being punted to
    manual. When that happens `interpolated` is True, `flag_reason` is CLEARED
    (the book now emits a bid-able number, so the upsert must not wipe it as
    needs_manual), fmv_low == fmv_high == median == the interpolated point
    (a single estimate, no dispersion), and confidence is forced to LOW (§7:
    "confidence is reduced").

    BUI-318 money-safety hardening of §7: (a) a bracket may only be anchored on
    a bucket holding ≥ MIN_BRACKET_COMPS comps — a single-comp bracket is one
    mistagged listing away from a wild over-bid, so a pool that can't muster a
    ≥2-comp bracket on BOTH sides stays needs_manual instead of emitting a
    trusted interpolated value; (b) the interpolated bid factor is capped at
    INTERPOLATED_BID_FACTOR (the interpolated-LOW haircut) so a thin
    single-point estimate never sets a full-0.80× bid cap even when no photo
    grade_confidence is present. `suspect_buckets` (§5) lists any adjacent
    grade-bucket median inversions so a monotonicity violation is flagged
    rather than silently blended — this is informational and never changes the
    priced number for a monotonic pool.
    """
    if max_window is None:
        max_window = MAX_GRADE_WINDOW
    pool, window = build_pool(comps, target_grade, max_window=max_window)

    # BUI-522: the ungraded-market anchor is a read-only side signal off the
    # grade-less comps build_pool just dropped — it never touches the priced
    # pool or any guard below, only the returned dict (→ fmv_notes token).
    anchor = ungraded_market_anchor(comps)

    # IQR-trim by price but keep the surviving comps as dicts (not just a
    # price list) so recency weighting (below) can still read their
    # sold_date. Bounds are computed the same way iqr_trim() does internally.
    bounds = _iqr_bounds([c["price"] for c in pool])
    if bounds is None:
        trimmed_comps = list(pool)
    else:
        lo_bound, hi_bound = bounds
        trimmed_comps = [c for c in pool if lo_bound <= c["price"] <= hi_bound]

    trimmed = [c["price"] for c in trimmed_comps]
    n = len(trimmed)
    cv_val = cv(trimmed)  # dispersion stays unweighted — only the point/quartile
                          # estimate and the confidence sample-size are recency-aware
    weights = _recency_weights(trimmed_comps)
    effective_n = sum(weights)
    flag_reason, grade_span = _classify_pool(pool, target_grade, n)

    # BUI-179: a 2-comp pool is never IQR-trimmed (len<3) and isn't too_sparse
    # (n>=2), so a single mistagged slab ([$10, $5000]) would price at a wild Q75
    # → 0.80×high overpay. Flag a tiny pool whose two prices diverge implausibly
    # (hi/lo beyond SMALL_POOL_MAX_RATIO) as needs-manual rather than pricing it.
    if flag_reason is None and n == 2:
        lo, hi = min(trimmed), max(trimmed)
        if lo <= 0 or hi / lo > SMALL_POOL_MAX_RATIO:
            flag_reason = "too_sparse"

    # BUI-588: the caller's fetch-level reason, applied only where the pool
    # itself is clean. Set here — above the §7 interpolation gate below, which
    # is scoped to one_sided/too_wide — so a forced flag can never be silently
    # cleared by interpolating a price for a pool whose comparability, not whose
    # shape, is the problem.
    if flag_reason is None and forced_flag_reason:
        flag_reason = forced_flag_reason

    # BUI-306 §5: check the grade-bucket median curve for monotonicity on the
    # widened grade-bearing pool. Violations are surfaced (SUSPECT) but never
    # alter the priced number for a monotonic pool — a non-monotonic pool keeps
    # its today-behavior price and just gains a warning.
    curve = bucket_medians(pool)
    counts = bucket_counts(pool)
    suspect_buckets = monotonicity_violations(curve)

    # BUI-306 §7: a one_sided/too_wide pool that still brackets the target with
    # real grade buckets gets a bid-able number via linear interpolation between
    # the nearest bracketing bucket medians, instead of always going manual. A
    # genuinely one-sided pool has no bracket → interpolate_grade_curve returns
    # None → it stays needs_manual. too_sparse is never interpolated (§7 needs a
    # bracket, which a lone-comp/1-grade pool cannot supply).
    #
    # The n>=3 floor is a money-safety guard mirroring BUI-179: a 2-comp pool is
    # never IQR-trimmable (len<3) and its two points may be wildly mistagged
    # (the [$50 @5.0, $5000 @9.0] shape). too_wide is classified before the
    # BUI-179 wild-ratio guard can fire, so without this floor such a pool would
    # interpolate a wild cap ($2k+ off two points). Too thin to vet → stays
    # manual, consistent with the module's "<3 comps is not a reliable price".
    # BUI-318 thin-bracket guard: pass per-bucket comp counts so a bracket
    # anchored on a lone comp is suppressed (returns None → stays needs_manual)
    # rather than smearing a wild over-bid across the target.
    # BUI-588: `forced_flag_reason` also SUPPRESSES §7. Interpolation is the one
    # path that clears a flag and emits a bid-able number, and interpolating
    # between buckets of a base-cover pool yields a base-cover price just the
    # same — it would hand back exactly the unflagged variant-blind number this
    # parameter exists to withhold. So a forced reason means no auto-price by
    # any route, whether it won the `flag_reason` slot or lost it to a
    # pool-shape reason above.
    interpolation = None
    if (flag_reason in ("one_sided", "too_wide") and n >= 3
            and not forced_flag_reason):
        interpolation = interpolate_grade_curve(curve, target_grade, counts=counts)

    label = confidence_label(effective_n, cv_val)
    if interpolation is not None:
        label = "LOW"  # §7: interpolation reduces confidence
    elif flag_reason is not None:
        label = "LOW"  # a needs_manual book never claims priceable confidence
    elif window > WIDE_GRADE_WINDOW and _rank(label) > _CONF_RANK["MEDIUM"]:
        label = "MEDIUM"  # wide-window pools can't claim HIGH/MEDIUM-HIGH (BUI-86 R7)
    factor = bid_factor(label, grade_confidence)
    if interpolation is not None:
        # BUI-318 interpolated-LOW haircut: a thin interpolated estimate never
        # sets a full-confidence bid cap. Take the MORE conservative of the
        # normal factor and the interpolated cap (min, so a present-and-lower
        # grade_confidence haircut still wins).
        factor = min(factor, INTERPOLATED_BID_FACTOR)

    # Declared Optional up front so mypy keeps all three pricing branches
    # consistent (the interpolation branch assigns non-None clean_round ints
    # first, which would otherwise pin these as non-Optional). clean_round
    # returns int; a flagged/no-comps book punts to None.
    fmv_low: int | None
    fmv_high: int | None
    med: int | None
    max_bid: int | None
    if interpolation is not None:
        # Priced by §7 interpolation: a single point estimate (no dispersion),
        # so fmv_low == fmv_high == median. Clearing flag_reason is REQUIRED —
        # a non-null flag makes the upsert wipe this price as needs_manual.
        price = clean_round(interpolation["target_price"])
        fmv_low = fmv_high = med = price
        max_bid = clean_round(price * factor)
        flag_reason = None
    elif flag_reason is not None or n == 0:
        fmv_low = fmv_high = med = max_bid = None
    else:
        med_raw = weighted_median(trimmed, weights)
        fmv_low = clean_round(weighted_quartile(trimmed, weights, 0.25))
        fmv_high = clean_round(weighted_quartile(trimmed, weights, 0.75))
        med = clean_round(med_raw)
        # BUI-528: when clean_round has collapsed the range to a single point
        # (fmv_low == fmv_high) but the pool's prices genuinely dispersed
        # (cv > 0), open it back into a real range instead of emitting a point
        # estimate that would set max_bid = 0.80 × point (the systematic
        # under-bid — a $5/$5 book cleared $39). A genuinely degenerate pool
        # (cv_val None → single comp, or == 0 → identical prices) is left a true
        # point. Only ever touches an already-zero-width band; a pool with any
        # real range is unchanged.
        if cv_val is not None and cv_val > 0 and fmv_low == fmv_high:
            fmv_low, fmv_high = _widen_collapsed_range(
                med_raw, cv_val, window, n, min(trimmed), max(trimmed))
        max_bid = clean_round(fmv_high * factor)

    # BUI-534: flag-only pool-vs-anchor divergence — computed from fields
    # already resolved above (fmv_low/fmv_high, the anchor from BUI-522),
    # never altering them. False for any flagged/no-comps book (fmv_low/high
    # are None there) without a separate flag_reason check.
    diverges = anchor_diverges(fmv_low, fmv_high, anchor)

    return {
        "n": n,
        "effective_n": effective_n,
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
        # BUI-306 §7/§5: interpolation + monotonicity, marked so a downstream
        # consumer can tell an interpolated value from a real direct comp and
        # spot a suspect grade bucket.
        "interpolated": interpolation is not None,
        "interpolation": interpolation,
        "suspect_buckets": suspect_buckets,
        # BUI-348: shape parity with cgc_proxy_fmv. A raw-pool result is never a
        # proxy; the CGC-proxy tier (fmv_runner) only fires on a raw result that
        # produced no bid-able number, replacing this dict wholesale.
        "cgc_proxy": False,
        "cgc_ladder": None,
        # BUI-522: ungraded-market anchor ({median, n}) off the dropped
        # grade-less comps, or None. Informational only — surfaced in fmv_notes,
        # never priced. A cached row can't reconstruct it (the raw comps aren't
        # persisted), so _build_notes reads it via .get and the db-row shape
        # parity test exempts it (unlike the bid-affecting keys).
        "ungraded_anchor": anchor,
        # BUI-534: True when fmv_low/fmv_high sit far outside the anchor above
        # (see anchor_diverges's docstring for the threshold + derivation).
        # Flag only — never changes fmv_low/fmv_high/max_bid. Recovered from
        # fmv_notes on a cache hit the same lossy way as ungraded_anchor
        # itself, via _fmv_from_db_row / _anchor_diverges_from_notes.
        "anchor_diverges": diverges,
        # BUI-529: populated by fmv_runner._apply_cgc_cross_check AFTER this
        # function returns (it needs the graded ladder, which compute_fmv never
        # fetches) — always None here. Key present for shape parity so every
        # consumer of this dict can read `.get("cgc_cross_check")` uniformly
        # whether or not the cross-check ran.
        "cgc_cross_check": None,
    }


# ─── Graded (slab) pricing mode (BUI-930) ─────────────────────────────────────
#
# Everything above prices a RAW copy. This section prices the slab ITSELF —
# the certified book in the listing is the thing being bought, so its comps
# are that certifier's own sales at that label, and none of the raw machinery
# applies: no `build_pool` grade-window widening (R30 — the exact tier is
# strictly exact, a 9.6 target never pools 9.8 sales), no CGC-proxy discount
# (it would price a slab at 0.5x itself), no ungraded anchor, no first-party
# merge. What IS reused is the ladder: `bucket_medians`' weighted twin below,
# `_bracket_interpolate` via `_cgc_ladder_price_and_clamp`, the BUI-349/355
# envelope clamp, and `monotonicity_violations`. One interpolation formula,
# two callers — the money-critical invariant this module has held since
# BUI-318.
#
# THE ONE RULE THIS SECTION EXISTS TO ENFORCE: a lone exact sale is never the
# price. The 2026-09-19 spike measured vintage slabs at ONE eBay sale per
# grade in 90 days, so the exact bucket is routinely n=1. Handing that single
# listing back as the FMV would make one seller's ask the bid cap on a
# four-figure book. Instead the target rung is REMOVED from the ladder and the
# neighbours interpolate across the gap (see `graded_fmv`).

# Age weighting (plan KTD "The ledger becomes a pricing input for slab targets
# only"). Live provider comps span eBay's ~90-day sold window; the comps
# ledger reaches further back, and those older observations are worth having
# on a market this thin — at half weight, and never past a year.
GRADED_FRESH_MAX_AGE_DAYS = 90     # weight 1.0 up to here
GRADED_STALE_MAX_AGE_DAYS = 365    # weight GRADED_STALE_WEIGHT up to here
GRADED_STALE_WEIGHT = 0.5          # 91–365 days
# Undated comps are EXCLUDED, not weighted 1.0 (the raw path's neutral
# default). The two paths differ because the raw pool is all live, ~90-day
# data where "no date" means the parser missed a field; the slab pool merges a
# ledger that genuinely reaches back years, so an undated ledger comp could be
# any age at all and a neutral weight would silently make the oldest rows the
# strongest evidence.

# The exact tier's gate, on EFFECTIVE n (the weight sum), not raw count. Two
# full-weight sales, or one full plus two stale, price directly; anything less
# goes to the ladder.
GRADED_EXACT_MIN_EFFECTIVE_N = 2.0

# Below this many remaining rungs the ladder is refused (plan R10 /
# `ladder_too_thin`). Counted over ANCHOR-ELIGIBLE rungs — see `_graded_ladder`.
GRADED_LADDER_MIN_RUNGS = 3

# A slab priced off its neighbours is a single point estimate off a bracket,
# exactly like the §7 raw interpolation, so it carries the same LOW label and
# the same 0.60 cap. Aliased rather than reused by name so the two can be
# tuned apart without one silently dragging the other.
GRADED_LADDER_CONFIDENCE = "LOW"
GRADED_LADDER_BID_FACTOR = INTERPOLATED_BID_FACTOR  # 0.60

# `min_bucket_n` for every slab ladder call. The raw path keeps
# MIN_BRACKET_COMPS (2) because a lone RAW listing is one mistag away from
# smearing a wild over-bid; a lone CERTIFIED sale is a graded, authenticated
# observation of the exact thing being bought, and requiring two would refuse
# essentially every vintage key (the spike measured one sale per rung). The
# money guard that replaces it is that the TARGET rung is dropped before the
# interpolation runs, so no single sale can ever be the answer.
GRADED_LADDER_MIN_BUCKET_N = 1

# The graded mode's own `fmv_flag_reason` vocabulary. Must stay a subset of
# `gixen_overlay.models.FMV_FLAG_REASONS` — a value that validator rejects
# 422s and the server discards the WHOLE upsert (the BUI-588 failure mode).
# The label/certifier punts live in fmv_runner (they fire before any fetch);
# these four are the ones this module can reach.
GRADED_FLAG_REASONS = (
    "no_certifier_pool", "ladder_too_thin", "ladder_non_monotone",
    "outside_ladder", "too_sparse",
)


def _is_graded_price(value: object) -> bool:
    """True for a usable price. `bool` excluded explicitly (it is an `int`
    subclass, so a stray `true` would otherwise enter the pool as $1.00)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def graded_comp_weight(age_days: float | None) -> float | None:
    """Age weight for one slab comp, or None when the comp is EXCLUDED.

    `age_days` is measured against the pool's own newest comp (see
    `graded_pool`), never a wall clock — the same determinism rule
    `_recency_weights` follows, so a fixture pool weighs the same today and
    next year. None (undated) is excluded; see GRADED_STALE_WEIGHT's comment.
    """
    if age_days is None:
        return None
    if age_days <= GRADED_FRESH_MAX_AGE_DAYS:
        return 1.0
    if age_days <= GRADED_STALE_MAX_AGE_DAYS:
        return GRADED_STALE_WEIGHT
    return None


def _graded_comp_date(comp: dict) -> date | None:
    """A slab comp's age basis: `sold_date`, else the ledger's `first_seen_at`.

    A live provider comp always carries `sold_date`. A ledger row may not (the
    provider did not report one), and for those `first_seen_at` — when this
    ledger first observed the listing — is a strict UPPER bound on the sale's
    recency, i.e. the conservative direction: it can only make a comp look
    NEWER than it is by the lag between sale and first observation, which is
    days, and it can never resurrect a comp older than the 365-day cutoff.
    """
    return (_parse_sold_date(comp.get("sold_date"))
            or _parse_sold_date(comp.get("first_seen_at")))


def graded_pool(comps: Iterable[dict]) -> tuple[list[dict], int, int]:
    """Weight and filter a slab comp pool.

    Returns `(kept, undated_dropped, stale_dropped)`. Each kept comp is a
    SHALLOW COPY carrying a `weight` key — copies so the caller's own list
    (which it also posts to the comps ledger) is never mutated with a field
    that is not part of the `CompItem` contract.

    The reference date is the pool's newest parseable comp date, matching
    `_recency_weights` (deterministic, no clock). A pool whose comps are ALL
    old therefore weighs them all 1.0 relative to each other — correct, and
    the same property the raw path has: recency weighting answers "which of
    these is the freshest evidence", never "is this market stale".
    """
    usable = [c for c in comps
              if _is_graded_price(c.get("price")) and c.get("grade") is not None]
    dated = [(c, _graded_comp_date(c)) for c in usable]
    known = [d for _, d in dated if d is not None]
    if not known:
        return [], len(usable), 0
    reference = max(known)
    kept: list[dict] = []
    undated = stale = 0
    for comp, when in dated:
        if when is None:
            undated += 1
            continue
        weight = graded_comp_weight(max((reference - when).days, 0))
        if weight is None:
            stale += 1
            continue
        kept.append({**comp, "weight": weight})
    return kept, undated, stale


def bucket_weighted_medians(comps: Iterable[dict]) -> dict[float, float]:
    """Grade → WEIGHT-AWARE median price, the slab ladder's rung values.

    The weighted twin of `bucket_medians`: same bucketing (one bucket per
    distinct parsed grade, comps missing grade or price ignored), but the
    rung value is `weighted_median`, so a 200-day-old ledger sale counts half
    as much as a live one inside its own rung. A comp with no `weight` key
    counts 1.0, which makes this reduce EXACTLY to `bucket_medians` on an
    unweighted pool (`weighted_median` delegates to `statistics.median` when
    every weight is equal) — so a test may pass bare comps.
    """
    buckets: dict[float, list[tuple[float, float]]] = {}
    for c in comps:
        g = c.get("grade")
        p = c.get("price")
        if g is None or p is None:
            continue
        buckets.setdefault(float(g), []).append(
            (float(p), float(c.get("weight", 1.0))))
    out: dict[float, float] = {}
    for g, pairs in buckets.items():
        out[g] = weighted_median([p for p, _ in pairs], [w for _, w in pairs])
    return out


def bucket_effective_n(comps: Iterable[dict]) -> dict[float, float]:
    """Grade → EFFECTIVE sample size (the weight sum) in that bucket.

    The weighted twin of `bucket_counts`, and the dict every slab-ladder call
    passes as `counts`. One consequence is deliberate and worth stating: with
    `GRADED_LADDER_MIN_BUCKET_N == 1`, a rung whose only sale is 91–365 days
    old sums to 0.5 and is NOT eligible to anchor an interpolation. That is
    the conservative direction — it can only refuse a price, never raise one —
    and it keeps a single stale observation from defining a bracket end on a
    four-figure book.
    """
    out: dict[float, float] = {}
    for c in comps:
        g = c.get("grade")
        p = c.get("price")
        if g is None or p is None:
            continue
        out[float(g)] = out.get(float(g), 0.0) + float(c.get("weight", 1.0))
    return out


def _exact_sales_detail(exact_comps: Iterable[dict]) -> list[dict]:
    """Price + sold-date detail for the exact-grade bucket (BUI-940).

    Sorted by price, same order as `exact_sales`. `exact_comps` is always a
    subset of `graded_pool`'s output, whose `known`-date guard means every
    kept comp already has a parseable date — `_graded_comp_date` is never
    None here.
    """
    detail = []
    for c in exact_comps:
        sold = _graded_comp_date(c)
        detail.append({"price": float(c["price"]),
                       "sold_date": sold.isoformat() if sold is not None else None})
    return sorted(detail, key=lambda d: d["price"])


def _nearest_rungs(
    ladder: Mapping[float, float], eff_n: Mapping[float, float],
    target_grade: float, min_bucket_n: float,
) -> dict:
    """The nearest anchor-eligible rung below and above `target_grade` (BUI-940).

    Unlike `_bracket_interpolate` — which returns nothing unless BOTH sides
    have an eligible rung, since a one-sided bracket can't interpolate —
    this reports whichever side has one, independently. That is the whole
    point: it is evidence for a human reading a refused row, including the
    one-sided `outside_ladder` case and the possibly-thin `ladder_too_thin`
    case, not an input to the pricing math. Eligibility uses the SAME bar
    `_graded_ladder` anchors on (`eff_n >= min_bucket_n`), so this can never
    surface a rung the pricing math itself would have refused to use.
    """
    def _side(grades: list[float]) -> dict | None:
        if not grades:
            return None
        g = grades[0]
        return {"grade": g, "median": ladder[g], "n": eff_n.get(g, 0.0)}

    below = sorted((g for g in ladder
                    if g < target_grade and eff_n.get(g, 0.0) >= min_bucket_n),
                   reverse=True)
    above = sorted(g for g in ladder
                   if g > target_grade and eff_n.get(g, 0.0) >= min_bucket_n)
    return {"below": _side(below), "above": _side(above)}


def _graded_result(**over) -> dict:
    """The graded mode's output dict, shaped like `compute_fmv`'s.

    Every key `compute_fmv`/`cgc_proxy_fmv` emit is present so a graded row
    can flow through `_build_notes`, `_print_table`, `_brief_row` and the
    upsert without a single `.get` special case, plus the graded-only keys the
    persistence path needs (`certifier`/`label`/`pricing_basis`).
    """
    base: dict = {
        "n": 0,
        "effective_n": 0.0,
        "window": None,
        "flag_reason": None,
        "grade_span": None,
        "fmv_low": None,
        "fmv_high": None,
        "median": None,
        "max_bid": None,
        "cv": None,
        "cv_pct": "n/a",
        "confidence": "LOW",
        # R20/KTD: a certified grade is not a photo judgement, so the BUI-51
        # photo-coverage haircut has nothing to say about it. Pinned None here
        # (not merely ignored by the caller) so `_build_notes`' haircut
        # attribution and `bid_factor` both see the same absence.
        "grade_confidence": None,
        "bid_factor": BASE_BID_FACTOR,
        "trimmed_pool": [],
        # Shape parity with compute_fmv. `interpolated` stays False even on a
        # `ladder` row: `pricing_basis` is the carrier now (plan KTD "Pricing
        # basis is a column"), and setting both would make `_build_notes` and
        # `_print_table` announce the same fact twice in two vocabularies.
        "interpolated": False,
        "interpolation": None,
        "suspect_buckets": [],
        "cgc_proxy": False,
        "cgc_ladder": None,
        "ungraded_anchor": None,
        "anchor_diverges": False,
        "cgc_cross_check": None,
        # ── graded-only ──────────────────────────────────────────────────
        "graded": True,
        "certifier": None,
        "label": None,
        # 'direct' | 'ladder' | None (nothing was priced). Never
        # 'interpolated'/'proxy' — those are the raw path's bases.
        "pricing_basis": None,
        "graded_ladder": None,
        "page_quality": None,
        "page_quality_fallback": False,
        # None while `page_quality_fallback` is False; otherwise
        # "too_few_matches" (fewer than 2 same-quality comps — BUI-930) or
        # "ladder_starved" (2+ same-quality comps, but scoping to them would
        # leave the ladder tier too thin — BUI-939). Additive: a reader that
        # only checks the boolean sees the same thing it always has.
        "page_quality_fallback_reason": None,
        "exact_effective_n": 0.0,
        "exact_sales": [],
        # BUI-940: price+date detail for the exact-grade bucket, additive
        # alongside `exact_sales` (which stays a bare sorted price list so no
        # existing reader/golden row moves). Same sort order as `exact_sales`.
        "exact_sales_detail": [],
        # BUI-940: the nearest anchor-eligible rung below/above the target,
        # `{"below": {"grade", "median", "n"} | None, "above": {...} | None}`
        # or None when never computed (e.g. a pre-fetch `graded_punt`, or
        # `no_certifier_pool` — nothing to show a neighbour from). Populated
        # by `_graded_ladder` for every ladder-tier outcome, refusal or not.
        "nearest_rungs": None,
        "pool_n": 0,
        "pool_undated_dropped": 0,
        "pool_stale_dropped": 0,
        "envelope_clamped": False,
    }
    base.update(over)
    return base


def _graded_page_quality_filter(
    pool: list[dict], page_quality: str | None,
) -> tuple[list[dict], bool, str | None]:
    """Prefer comps whose page quality matches the target's.

    Returns `(pool, fell_back, reason)`. Applied ONLY when the target's page
    quality is a real reading — `None`/`"unknown"` is the ABSENCE of one, and
    "prefer the comps whose page quality we also failed to read" is not a
    quality match, it is a filter on parser coverage (on the spike corpus it
    would have thrown away the single graded-white rung of half the books).
    Falls back to the whole pool, with `fell_back=True` and
    `reason="too_few_matches"`, whenever fewer than two comps match — one
    match is a single listing, not a market.

    This is the ONLY fallback this function decides. A second, independent
    one — scoping to the matched quality leaves the LADDER tier too thin
    (BUI-939) — can't be decided here: it depends on the target grade and on
    whether the scoped pool would even reach the ladder tier at all (a scoped
    pool that prices DIRECTLY must never fall back), neither of which this
    function has in scope. `graded_fmv` decides that one itself, once it
    knows the tier.
    """
    if not page_quality or page_quality == "unknown":
        return pool, False, None
    matched = [c for c in pool if c.get("page_quality") == page_quality]
    if len(matched) >= 2:
        return matched, False, None
    return pool, True, "too_few_matches"


def graded_punt(reason: str, *, certifier: str, label: str,
                page_quality: str | None = None) -> dict:
    """A graded needs-manual result for a book refused BEFORE any fetch.

    The label/certifier refusals (plan R31) are decided from the listing
    alone — a Signature Series slab does not need its comps queried to be
    known unpriceable — so they never reach `graded_fmv`. This keeps their
    output dict identical in shape to one that did, so `_build_notes`,
    `_print_table`, `_brief_row` and the upsert have exactly one graded shape
    to handle.
    """
    return _graded_result(flag_reason=reason, certifier=certifier, label=label,
                          page_quality=page_quality,
                          # None, not 0: nothing was ever fetched, and
                          # `slab_pool=0` in the notes would read as "we
                          # looked at this certifier's sales and found none",
                          # which is a different (and wrong) claim.
                          pool_n=None)


def graded_fmv(comps: list[dict], target_grade: float, *,
               certifier: str, label: str,
               page_quality: str | None = None) -> dict:
    """Price a CERTIFIED slab from same-certifier, same-label slab sales.

    `comps` is the already-identity-filtered pool (live provider slab comps
    plus ledger `pool='slab'` rows for the same comic/certifier/label, deduped
    on `product_id` — the caller does that merge; this function does the
    math). Each comp needs `price` and `grade`, plus `sold_date` or
    `first_seen_at` for its age weight and optionally `page_quality`.

    Returns a `compute_fmv`-shaped dict. A refusal is the ordinary needs-manual
    shape (`flag_reason` set, every price None, confidence LOW) — the graded
    mode never returns None, so the caller has exactly one result shape.

    The decision, per the plan's diagram:

      * EXACT tier — effective n >= 2 at EXACTLY `target_grade` prices
        directly: weighted Q25/median/Q75 of that one bucket, the standard
        `confidence_label` rubric on its effective n and CV, and — below
        `OUTLIER_ROBUST_BUCKET_N` — the BUI-349/355 envelope clamp bounding
        the whole band from above by what the neighbours imply. R30 is why the
        bucket is strictly exact: a 9.6 and a 9.8 slab are two different
        products at two different prices, and pooling them is precisely the
        error the raw ±window exists to make (usefully) for raw copies.
      * LADDER tier — otherwise the target rung is DROPPED and the neighbours
        interpolate across the gap, at LOW/0.60. Dropping it is the whole
        mechanism: `_cgc_ladder_price_and_clamp` returns an exact bucket
        directly when one is present, so calling it with the rung in place
        would hand back the lone sale (merely bounded from above) rather than
        an interpolation.

        **Page quality scopes the EXACT bucket and nothing else (BUI-937).**
        The preference exists so the EXACT tier prefers a same-quality match;
        it was never meant to be a second, stricter pool for anything else to
        read. So exactly one thing reads the page-quality-scoped pool — the
        exact bucket: its effective n decides the tier above, and its sales
        are the band when it prices. Everything else reads the WHOLE
        same-label pool: the ladder tier's rungs, and the exact tier's own
        envelope clamp.

        Two things follow. BUI-939's conditional widen becomes structural —
        a pool the ladder never reads cannot starve it, so there is no rung
        count to check and no way for scoping alone to turn a priceable book
        into `ladder_too_thin`. And the gap BUI-930 named but could not reach
        closes: a scoped pool whose only rung IS the exact bucket used to
        leave a thin band with no envelope to bound it, and now the whole
        pool's neighbours bound it. That envelope is an all-quality bound on
        a same-quality band, so it can clamp a genuine page-quality premium
        away — the intended direction, since `min()` only ever lowers a cap
        and the alternative was a two-sale bucket setting a four-figure cap
        unbounded.

        A ladder-tier row whose scoped pool WAS narrower reports the widen in
        the two fields BUI-939 introduced for it (`page_quality_fallback=True`,
        `page_quality_fallback_reason="ladder_starved"`, distinguishable from
        the pre-existing `"too_few_matches"` fallback) — now on every such
        row, not only the ones a rung count would have rescued.
      * REFUSALS — `no_certifier_pool` (nothing survived the identity + age
        filters), `ladder_too_thin` (< 3 anchor-eligible rungs in the whole
        same-label pool), `outside_ladder` (no rung on one side — the proxy's
        never-extrapolate rule), `ladder_non_monotone` (the two rungs the
        interpolation would actually use invert).

    The `ladder_non_monotone` check is scoped to the NEIGHBOURS, not the whole
    ladder, and that scope is measured rather than assumed. `cgc_proxy_fmv`
    refuses on ANY violation anywhere in its ladder, which is right for a
    ladder built from a 3+-comp raw-market query; on the 2026-09-21 spike
    corpus, where every rung is one sale, all 4 ladder-tier books carried a
    violation SOMEWHERE and a whole-ladder rule refused all 4 — while the
    neighbour rule refused exactly the 2 whose own bracket inverted (Batman
    #227 at 4.5, whose 4.0 rung sold for $1,400 against a $899 5.5; and
    Invincible #1 at 9.4). A guard that refuses everything is not a guard, and
    the rungs a straight line is drawn between are the ones whose order
    decides whether that line means anything.
    """
    full_pool, undated_dropped, stale_dropped = graded_pool(comps)
    pool, pq_fallback, pq_reason = _graded_page_quality_filter(full_pool, page_quality)
    identity: dict = {
        "certifier": certifier,
        "label": label,
        "page_quality": page_quality,
        "page_quality_fallback": pq_fallback,
        "page_quality_fallback_reason": pq_reason,
        "pool_n": len(pool),
        "pool_undated_dropped": undated_dropped,
        "pool_stale_dropped": stale_dropped,
    }
    if not pool:
        return _graded_result(flag_reason="no_certifier_pool", **identity)

    target_grade = float(target_grade)
    # The EXACT bucket is the one thing the page-quality preference scopes
    # (BUI-937): the scoped pool decides the tier, and — when it prices — it
    # is the band.
    scoped_eff_n = bucket_effective_n(pool)
    exact_comps = [c for c in pool if float(c["grade"]) == target_grade]
    identity["exact_effective_n"] = scoped_eff_n.get(target_grade, 0.0)
    identity["exact_sales"] = sorted(float(c["price"]) for c in exact_comps)
    identity["exact_sales_detail"] = _exact_sales_detail(exact_comps)

    # Every RUNG comes from the whole same-label pool, whatever its page
    # quality — the ladder tier's neighbours and the exact tier's envelope
    # clamp alike. Built from `full_pool` unconditionally, which is what makes
    # BUI-939's widen structural rather than a rung count that has to be
    # remembered.
    ladder = bucket_weighted_medians(full_pool)
    eff_n = bucket_effective_n(full_pool)

    if identity["exact_effective_n"] >= GRADED_EXACT_MIN_EFFECTIVE_N:
        return _graded_direct(exact_comps, ladder, eff_n, target_grade, identity)

    if len(pool) < len(full_pool):
        # Scoping was applied and the book is priced off the ladder, which
        # reads every quality — so the row says so, in the two fields BUI-939
        # introduced for exactly this disclosure.
        identity["page_quality_fallback"] = True
        identity["page_quality_fallback_reason"] = "ladder_starved"
        identity["pool_n"] = len(full_pool)
        identity["exact_effective_n"] = eff_n.get(target_grade, 0.0)
        widened_exact = [c for c in full_pool if float(c["grade"]) == target_grade]
        identity["exact_sales"] = sorted(float(c["price"]) for c in widened_exact)
        identity["exact_sales_detail"] = _exact_sales_detail(widened_exact)

    return _graded_ladder(full_pool, ladder, eff_n, target_grade, identity)


def _graded_direct(exact_comps: list[dict], ladder: dict[float, float],
                   eff_n: dict[float, float], target_grade: float,
                   identity: dict) -> dict:
    """The EXACT tier: price the band off the target-grade bucket alone.

    `exact_comps` is the page-quality-SCOPED bucket (it is the band, and its
    effective n already decided this tier), while `ladder`/`eff_n` are the
    WHOLE same-label pool's rungs (BUI-937) — the envelope that bounds the
    band must exist even when scoping left the scoped pool one rung wide.
    """
    prices = [float(c["price"]) for c in exact_comps]
    weights = [float(c["weight"]) for c in exact_comps]
    effective_n = sum(weights)
    cv_val = cv(prices)

    fmv_low = weighted_quartile(prices, weights, 0.25)
    fmv_high = weighted_quartile(prices, weights, 0.75)
    med = weighted_median(prices, weights)

    # BUI-349/355 envelope clamp, reused verbatim: with `eff_n` as `counts`,
    # the helper's own `counts[target] < max(min_bucket_n, 3)` trigger fires
    # exactly on an exact bucket below OUTLIER_ROBUST_BUCKET_N effective
    # sales, and bounds it by the linear envelope its neighbours imply. The
    # cap is applied to the WHOLE band (low/median/high alike) rather than to
    # the midpoint only: `max_bid` rides `fmv_high`, so clamping the median
    # and leaving the high would leave the bid cap exactly where the guard
    # says it must not be. `min()` everywhere means this can only LOWER a
    # number, never raise one.
    #
    # BUI-937: the TARGET rung is overridden with this bucket's own median and
    # effective n, every other rung left as the whole pool's. What is being
    # bounded is the scoped bucket — so the helper's thin-bucket trigger has to
    # read the SCOPED n, or a fat all-quality rung at the target grade would
    # wave a two-sale same-quality band through unbounded — while what bounds
    # it is the whole market's neighbours. With no scoping applied the override
    # is a no-op: `ladder[target_grade]` already IS `med`.
    clamp_ladder = {**ladder, target_grade: med}
    clamp_counts = {**eff_n, target_grade: effective_n}
    capped, envelope_clamped = _cgc_ladder_price_and_clamp(
        clamp_ladder, target_grade, counts=clamp_counts,
        min_bucket_n=GRADED_LADDER_MIN_BUCKET_N,
    )
    if envelope_clamped and capped is not None:
        fmv_low = min(fmv_low, capped)
        fmv_high = min(fmv_high, capped)
        med = min(med, capped)
    elif effective_n < OUTLIER_ROBUST_BUCKET_N:
        # The gap the clamp above cannot cover, and the ONE place a thin exact
        # bucket can still set a four-figure cap unbounded. `capped` comes
        # back unclamped whenever no eligible rung BRACKETS the target — the
        # target sits at an end of the whole pool's ladder, or the envelope
        # came back at or above the bucket's own median — so there is no
        # (binding) envelope to bound it with. Since BUI-937 the scoped-pool
        # case is no longer one of those: the neighbours are read from every
        # same-label comp, so scoping alone can no longer leave a thin band
        # unbounded, and this guard is left holding only the ends of the
        # ladder. A 2-sale bucket is never IQR-trimmable
        # and its median-of-2 is just a midpoint, so one mistagged or premium
        # listing sets `fmv_high` and, at 0.80x, the bid. This is BUI-179's
        # guard, applied where BUI-349's cannot reach: refuse rather than
        # price a pair that disagree implausibly. Same `too_sparse` reason
        # BUI-179 uses, and for the same reason — what is wrong is not the
        # count but that two observations this far apart are not one market.
        lo, hi = min(prices), max(prices)
        if lo <= 0 or hi / lo > SMALL_POOL_MAX_RATIO:
            return _graded_result(flag_reason="too_sparse", **identity)

    conf = confidence_label(effective_n, cv_val)
    # `grade_confidence` is pinned None for a certified row (see
    # `_graded_result`), so this is BASE_BID_FACTOR today. Routed through
    # `bid_factor` anyway so the graded path cannot drift from the one
    # function that owns the haircut ladder.
    factor = bid_factor(conf, None)
    low_i, high_i, med_i = (clean_round(fmv_low), clean_round(fmv_high),
                            clean_round(med))
    return _graded_result(
        n=len(exact_comps),
        effective_n=effective_n,
        fmv_low=low_i,
        fmv_high=high_i,
        median=med_i,
        max_bid=clean_round(high_i * factor),
        cv=cv_val,
        cv_pct=f"{cv_val * 100:.0f}%" if cv_val is not None else "n/a",
        confidence=conf,
        bid_factor=factor,
        trimmed_pool=sorted(prices),
        pricing_basis="direct",
        envelope_clamped=envelope_clamped,
        graded_ladder={
            # The ladder the clamp actually read, target rung and all, so an
            # auditor comparing `fmv_high` against the rungs is looking at the
            # same numbers the guard was (BUI-937: whole-pool neighbours, this
            # bucket's own target rung).
            "ladder": dict(sorted(clamp_ladder.items())),
            "effective_n": dict(sorted(clamp_counts.items())),
            "target_grade": target_grade,
            "envelope_price": capped if envelope_clamped else None,
        },
        **identity,
    )


def _graded_ladder(pool: list[dict], ladder: dict[float, float],
                   eff_n: dict[float, float], target_grade: float,
                   identity: dict) -> dict:
    """The LADDER tier: drop the target rung and interpolate its neighbours."""
    ladder_ex = {g: v for g, v in ladder.items() if g != target_grade}
    eff_ex = {g: v for g, v in eff_n.items() if g != target_grade}
    # BUI-940: computed once, up front, so every return below — refusal or
    # priced — carries the same evidence. Independent of `bracket` below (see
    # `_nearest_rungs`'s docstring): a one-sided `outside_ladder` case still
    # gets whichever side exists, which `bracket` alone cannot express.
    identity["nearest_rungs"] = _nearest_rungs(
        ladder_ex, eff_ex, target_grade, GRADED_LADDER_MIN_BUCKET_N)
    # Counted over ANCHOR-ELIGIBLE rungs (effective n >= min_bucket_n) rather
    # than over every surviving key: a rung that cannot anchor cannot hold up
    # a bracket either, so counting it would only swap this honest
    # `ladder_too_thin` for a misleading `outside_ladder` one branch later.
    eligible = [g for g in ladder_ex
                if eff_ex.get(g, 0.0) >= GRADED_LADDER_MIN_BUCKET_N]
    if len(eligible) < GRADED_LADDER_MIN_RUNGS:
        return _graded_result(flag_reason="ladder_too_thin", **identity)

    bracket = _bracket_interpolate(
        ladder_ex, target_grade, eff_ex, GRADED_LADDER_MIN_BUCKET_N)
    if bracket is None:
        # No eligible rung on one side. The proxy's never-extrapolate rule,
        # for the same reason: a straight line run off the end of the observed
        # ladder is a guess, and on a slab that guess is four figures.
        return _graded_result(flag_reason="outside_ladder", **identity)

    pair = (bracket["grade_below"], bracket["grade_above"])
    if pair in monotonicity_violations(ladder_ex):
        return _graded_result(flag_reason="ladder_non_monotone", **identity)

    price, _clamped = _cgc_ladder_price_and_clamp(
        ladder_ex, target_grade, counts=eff_ex,
        min_bucket_n=GRADED_LADDER_MIN_BUCKET_N,
    )
    if price is None:  # pragma: no cover — `bracket` above already proved one
        return _graded_result(flag_reason="outside_ladder", **identity)

    point = clean_round(price)
    factor = min(bid_factor(GRADED_LADDER_CONFIDENCE, None),
                 GRADED_LADDER_BID_FACTOR)
    return _graded_result(
        # `n` is LADDER depth, not exact-grade depth — the same caveat
        # `cgc_proxy_fmv` carries, and `_build_notes` states it in words so a
        # machine reading `fmv_comps` in isolation cannot mistake it for
        # liquidity at the target grade.
        n=len(pool),
        effective_n=sum(eff_n.values()),
        fmv_low=point,
        fmv_high=point,
        median=point,
        max_bid=clean_round(point * factor),
        confidence=GRADED_LADDER_CONFIDENCE,
        bid_factor=factor,
        pricing_basis="ladder",
        graded_ladder={
            "ladder": dict(sorted(ladder_ex.items())),
            "effective_n": dict(sorted(eff_ex.items())),
            "target_grade": target_grade,
            "grade_below": bracket["grade_below"],
            "grade_above": bracket["grade_above"],
            "median_below": bracket["median_below"],
            "median_above": bracket["median_above"],
            "target_price": bracket["target_price"],
            "envelope_price": None,
        },
        **identity,
    )
