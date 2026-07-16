---
title: "Median of n≤2 Is Just the Mean — Require n≥3 for Money-Path Statistics"
date: 2026-07-17
category: best-practices
module: apps/fmv
problem_type: best_practice
component: service_object
severity: high
applies_when:
  - "Computing a bid cap, FMV, or slab price from a median over graded/raw comps"
  - "A bucket's sample size is small (n<=2) before a monotonicity or envelope guard runs"
  - "Adding a new small-n trust threshold alongside an existing sibling knob (e.g. min_bucket_n)"
  - "Any money-decision path assumes a median is robust without checking n"
  - "Reviewing a change that widens or narrows a guard threshold near money"
symptoms:
  - "A monotonic-looking ladder passes validation despite one outlier-corrupted bucket"
  - "Bid cap or price jumps far above neighboring grade buckets"
  - "An n=1 envelope clamp exists but doesn't fire for n=2 buckets"
  - "A single mistagged outlier comp swings a 2-sample median significantly"
  - "Guard threshold is a bare constant instead of tracking a sibling config knob"
root_cause: logic_error
resolution_type: code_fix
related_components:
  - cgc_ladder_price
  - "BUI-349 envelope-sanity clamp"
  - "BUI-318 min_bucket_n threshold"
tags:
  - fmv
  - outlier-robustness
  - median
  - bid-cap
  - money-path
  - bucket-sample-size
  - monotonicity-guard
  - cgc-ladder
---

# Median of n≤2 Is Just the Mean — Require n≥3 for Money-Path Statistics

## Context

The money path in `apps/fmv/src/fmv_math.py` prices CGC slabs off a grade→price ladder (`cgc_ladder_price`), where each bucket's price is `statistics.median(bucket)` over the graded comps at that grade. This rule closes a three-incident lineage, each of which patched the ladder's small-sample trust boundary at one specific `n` and left the next one open:

- **BUI-318** — established the founding principle: don't trust a statistic in the money path below `n=2` (`MIN_BRACKET_COMPS`), because a lone mistagged/premium comp could smear a wild over-bid across an *interpolated* span. This gated which buckets may *anchor* a bracket interpolation.
- **BUI-349** — found that an *exact*-match bucket (exempt from the anchor gate, since a direct hit is stronger evidence than an interpolation) could still go wrong at `n=1`: a single off-trend-high slab passes both the monotonicity guard and the ladder-wide count floor, yet has no comp to average against. Fix: an envelope-sanity clamp bounding the thin exact value by what its trustworthy neighbors imply.
- **BUI-355** — found the BUI-349 clamp's trigger (`< min_bucket_n`, i.e., only `n=1`) missed `n=2`. `statistics.median([1200, 5000]) == 3100` — median-of-2 *is* the mean of two, with zero outlier resistance. A single mistagged $5000 comp in an n=2 bucket set a $3100 bid cap. Both existing guards passed it clean: the ladder `{5.0: 830 (n=2), 6.5: 3100 (n=2), 7.0: 3250 (n=2)}` is monotone, and the envelope clamp didn't fire because `counts[6.5]=2` was not `< min_bucket_n` (2).

## Guidance

**1. A median needs `n≥3` before it's trusted un-clamped in a money path.** `apps/fmv/src/fmv_math.py`:

```python
# Within-bucket outlier robustness floor (BUI-355): the smallest bucket size
# whose median actually resists one outlier. median-of-1 IS the comp and
# median-of-2 is just the mean of two — one $5000 mistag in an n=2 bucket
# drags the "median" to the midpoint, with zero robustness. Only at n>=3 does
# the median discard an extreme value. Exact target-grade buckets thinner than
# this are subject to the BUI-349 envelope-sanity clamp below. Distinct from
# MIN_BRACKET_COMPS (=2), which gates which buckets may ANCHOR an
# interpolation — that semantic is unchanged.
OUTLIER_ROBUST_BUCKET_N = 3
```

**2. `n≤2` isn't rejected outright — it gets an independent bound instead (the envelope-clamp pattern).** Rather than discarding a thin exact-match bucket (which would lose real signal — e.g., a genuinely sparse key like ASM #50's lone 6.5 slab), bound it from above by what its trustworthy bracketing neighbors imply, and take the min:

```python
if target_grade in ladder:
    exact = ladder[target_grade]
    if (counts is not None
            and counts.get(target_grade, 0)
            < max(min_bucket_n, OUTLIER_ROBUST_BUCKET_N)):
        envelope = _bracket_interpolate(
            ladder, target_grade, counts, min_bucket_n
        )
        if envelope is not None:
            return min(exact, envelope["target_price"])
    return exact
```

This is strictly conservative by construction: `min(exact, envelope)` can only lower a price, never raise one. When no envelope is available (edge of the ladder, or the only neighbor is itself too thin to anchor), the thin exact value is used directly — the irreducible sparse-key case still gets served, it just isn't sanity-checked.

**3. A guard threshold must compose with its sibling knobs via `max()`, not freeze into a bare constant.** The first draft used a bare `< OUTLIER_ROBUST_BUCKET_N`. Code review caught that this silently stopped tracking `min_bucket_n`: a caller passing `min_bucket_n=4` (a stricter anchor requirement than the default 2) would, under the bare-constant version, get the clamp check firing at `< 3` instead of `< 4` — a case where the fix would have *raised* the price relative to the pre-BUI-355 behavior for that caller. The fix composes the two thresholds: `< max(min_bucket_n, OUTLIER_ROBUST_BUCKET_N)`, guaranteeing the new trigger is always at least as wide as the old one for every caller. Locked in by a regression test.

## Why This Matters

- **Direction of the money risk is asymmetric.** A too-high bid cap means overpaying (real money out); a too-low cap only means a missed auction (opportunity cost, recoverable). Every guard in this chain — `min()`, never `max()` — is built to only ever move the price down, never up.
- **Median-of-2 is a trap specifically because it *looks* robust.** "We used the median, not the mean" reads as a safety statement, but at `n=2` they're the identical formula. A guard that gates on "is this a median" without also gating on sample size lets exactly the failure case (one bad tag) through undetected.
- **Monotonicity checks don't save you here.** The monotonicity guard only rejects a bucket priced *above the next rung* — it says nothing about whether a value sitting *between* two neighbors is still off the linear trend connecting them. `{5.0: 830, 6.5: 3100, 7.0: 3250}` is perfectly monotone while the 6.5 value sits far above the straight line between 830 and 3250. Monotonicity catches inversions, not off-trend bulges.
- **The threshold counter-example generalizes past this one file.** Any place a threshold is copied into an "at least this robust" check needs to ask: what happens when a caller strengthens the underlying knob this threshold is supposed to be *at least as strict as*? A bare constant silently decouples from that knob and can flip a strictly-conservative fix into a regression for stricter callers.

## When to Apply

- Any median, percentile, or other order-statistic computed over a small (`n≤3`ish) sample that feeds a bid cap, FMV band, price floor, or price ceiling — anywhere "we took the median so it's robust" is relied on without checking sample size.
- Code review of any change that widens or narrows a guard threshold near money: check whether the new threshold is a bare constant or whether it composes (`max`/`min`) with any sibling parameter a caller could already be varying. If a caller-supplied knob exists, prove the new threshold can never make that caller's guard weaker than before.
- Whenever a monotonicity/sanity check on a sequence is the *sole* validation for an individual point — it validates ordering, not deviation from trend.

**Scope note:** this rule governs **money-path** statistics (values that feed bid caps or prices the system acts on). Diagnostic-only statistics may deliberately keep lower sample floors for coverage — the Calibration Report's minimum-loss gate stays at 2 by design (see `fmv-recency-weighting-baseline-r7.md`, Guidance #4); that is the sanctioned exception, not a violation.

## Examples

**Incident ladder — before (BUI-349 only) vs. after (BUI-355):**

```python
ladder = {5.0: 830.0, 6.5: 3100.0, 7.0: 3250.0}
counts = {5.0: 2,      6.5: 2,      7.0: 2}

# Pre-BUI-355: counts[6.5]=2 is NOT < min_bucket_n (2), so the BUI-349 clamp
# never fires. Monotonicity passes (830 < 3100 < 3250). Returned price: 3100.

# Post-BUI-355: counts[6.5]=2 IS < max(min_bucket_n=2, OUTLIER_ROBUST_BUCKET_N=3),
# so the envelope clamp fires. Envelope at 6.5 = 830 + 0.75*(3250-830) = 2645.
fm.cgc_ladder_price(ladder, 6.5, counts=counts) == pytest.approx(2645.0)
```

**The `min_bucket_n=4` counter-example the review caught** (`apps/fmv/tests/test_fmv_math.py`) — proving the `max()` wrapper, not a bare constant, is required:

```python
def test_raised_min_bucket_n_keeps_clamp_tracking_it(self):
    # The trigger is max(min_bucket_n, OUTLIER_ROBUST_BUCKET_N): a caller
    # demanding stricter anchors (min_bucket_n=4) still clamps an n=3
    # exact bucket, exactly as the pre-BUI-355 `< min_bucket_n` trigger
    # did — the widened threshold must never RAISE a price for ANY caller.
    ladder = {5.0: 830.0, 6.5: 3100.0, 7.0: 3250.0}
    counts = {5.0: 4, 6.5: 3, 7.0: 4}
    assert fm.cgc_ladder_price(
        ladder, 6.5, counts=counts, min_bucket_n=4
    ) == pytest.approx(2645.0)
```

Under a bare `< OUTLIER_ROBUST_BUCKET_N` (3), `counts[6.5]=3` would *not* trigger the clamp, so this caller would get the unclamped `3100` — higher than the `2645` the pre-BUI-355 `< min_bucket_n=4` trigger would have produced.

## Related

- `docs/solutions/best-practices/fmv-grade-curve-interpolation-overbid-guards.md` — the strongest sibling: BUI-306's Trap #2 ("require n≥3 comps before interpolating at all") is the same n≥3 floor applied to `interpolate_grade_curve`; a 2-comp bracket there produced a ~$2k over-bid. Different guard shape (hard floor + reject vs this doc's independent-bound clamp), same principle.
- `docs/solutions/best-practices/fmv-recency-weighting-baseline-r7.md` — Guidance #4 documents the *deliberate* min_losses=2 in the calibration report (diagnostic-only; raising to 3 would starve the report). See the scope note above — that decision stands.
- `docs/solutions/best-practices/fmv-self-referential-feedback-deflation-guard.md` — sibling doc carrying the same median-of-two-losses caveat in the calibration context.
- `docs/solutions/best-practices/fmv-7a-cgc-proxy-not-safely-automatable.md` — documents the ladder-based CGC proxy tier this guard hardens (BUI-348 built `cgc_ladder_price`).
- Linear: BUI-318 (n≥3 precedent), BUI-348 (built the ladder), BUI-349 (n=1 envelope clamp), BUI-355 (this fix, PR #193), BUI-369 (follow-up: surface the clamp in FMV notes — the clamp is currently silent).
