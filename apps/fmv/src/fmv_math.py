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


def _bracket_interpolate(
    medians: dict[float, float], target_grade: float,
    counts: dict[float, int] | None = None,
    min_bucket_n: int = MIN_BRACKET_COMPS,
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
    medians: dict[float, float], target_grade: float,
    counts: dict[float, int] | None = None,
    min_bucket_n: int = MIN_BRACKET_COMPS,
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

def clean_round(value: float) -> int:
    """Round to clean step: $5 below $50, $10 from $50–$200, $25 above."""
    if value < 50:
        step = 5
    elif value < 200:
        step = 10
    else:
        step = 25
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


def cgc_ladder_price(ladder: dict[float, float], target_grade: float,
                     counts: dict[float, int] | None = None,
                     min_bucket_n: int = MIN_BRACKET_COMPS) -> float | None:
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
    extrapolation); the ladder-wide comp-count floor + the monotonicity guard in
    ``cgc_proxy_fmv`` are the money-safety nets against a fluke single-comp
    ladder there.
    """
    if not ladder:
        return None
    if target_grade in ladder:
        return ladder[target_grade]
    bracket = _bracket_interpolate(ladder, target_grade, counts, min_bucket_n)
    return bracket["target_price"] if bracket else None


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
    slab = cgc_ladder_price(ladder, target_grade, counts=counts)
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
        },
    }


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
      "n": int,                        # trimmed pool size (raw count)
      "effective_n": float,            # sum of recency weights (BUI-287 U2);
                                        # == n when every comp has neutral weight
      "window": float,                 # window the pool was built at (≤ max_window)
      "flag_reason": str | None,       # one_sided | too_wide | too_sparse | None (BUI-86)
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
    interpolation = None
    if flag_reason in ("one_sided", "too_wide") and n >= 3:
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
        fmv_low = clean_round(weighted_quartile(trimmed, weights, 0.25))
        fmv_high = clean_round(weighted_quartile(trimmed, weights, 0.75))
        med = clean_round(weighted_median(trimmed, weights))
        max_bid = clean_round(fmv_high * factor)

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
    }
