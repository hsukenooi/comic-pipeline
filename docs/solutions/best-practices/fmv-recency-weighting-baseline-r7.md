---
title: "Validating FMV recency weighting against a dated-comp baseline (R7)"
date: 2026-07-04
category: best-practices
module: comic-fmv (apps/fmv)
problem_type: best_practice
component: service_object
severity: medium
related_components:
  - comic-fmv
  - gixen-overlay
applies_when:
  - "Changing RECENCY_HALF_LIFE_DAYS or the weighted-quantile math in fmv_math"
  - "Deciding whether a thin-pool or estimator change needs a new guard"
  - "Reviewing whether a live FMV move is explained or anomalous"
tags: [fmv, recency-weighting, baseline, regression, money-path, half-life, calibration]
---

# Validating FMV recency weighting against a dated-comp baseline (R7)

## Context

The FMV auction-outcome feedback loop (BUI-286/287/288, PR #122) shipped
recency weighting (U2) — the estimator change that makes recent sales dominate
FMV, which sets `max_bid = 0.80 x fmv_high`. Plan requirement **R7** gates that
change on a before/after diff against a frozen baseline: *"the fixture diff is
reviewed and every FMV move is explained by recency."* The code was proven
correct by unit tests, but R7 could not be closed by them — the existing golden
fixture (`test_golden_fmv_math.py`) uses **dateless** comps, so it only ever
exercises the pre-U2 unweighted path. Nothing regression-guarded the weighted
path, and the *magnitude* of live moves (and whether the 75-day half-life is
right) was unquantified. BUI-289 closes that gap.

## Guidance

**1. Guard the weighted path with a DATED baseline — the golden fixture doesn't.**

`apps/fmv/tests/test_recency_baseline.py` + `fixtures/recency_baseline.json`
freeze `compute_fmv` on dated pools so any future change to the weighted
quantiles, the half-life, or the effective-n confidence rubric diffs visibly
instead of silently shifting a bid cap. All `sold_date`s are anchored to a
**fixed constant** (`_REF = date(2026,7,4)`), never `date.today()` — the fixture
must be reproducible, and `compute_fmv` already ages comps against the newest
date *in the pool*, clock-free. Regenerate with `--regen` and review the JSON
diff, exactly like the golden fixture.

**2. Keep RECENCY_HALF_LIFE_DAYS = 75 — the typical case is the evidence.**

The weighting-off vs weighting-on diff across representative pools:

| Pool | max_bid move | Why |
| --- | --- | --- |
| Thick pool, gentle uptrend (typical) | **0%** | Point estimate/cap stable; recency only tightened `fmv_low` and dropped confidence HIGH→MEDIUM |
| Recent sales higher | +10% | Correct direction — recent = clearest |
| Recent sales lower | -10% | Correct direction |
| 8 tight comps, all ~1yr stale | 0% (cap), HIGH→MEDIUM-HIGH | Confidence reconciliation: stale count no longer buys HIGH |
| Thin 2-comp, fresh comp high | +11% | LOW confidence; see residual below |

The decisive result is the **typical thick pool moving the cap 0%**: a 75-day
half-life does not over-react in normal conditions — it moves the bid cap only
when there is a genuine recent trend, and always in the correct direction. The
half-life is also verified *exact*: a comp aged 75 days contributes precisely
half a fresh comp's weight (`effective_n` 2.0 → 1.5). 60–90 days was the plan's
candidate band; 75 sits in the middle and behaves well. No change.

**3. Do NOT add a thin-pool recency guard — but know its real mitigation.**

A 2-comp pool whose fresher comp is the higher one prices `fmv_high` toward that
comp (+11% `max_bid` in the fixture). This is **intended** — a recent sale is
the better market read — and special-casing it would reintroduce exactly the
complexity U2 removed. It is mitigated by (a) the **BUI-179 `too_sparse` flag**,
which rejects any 2-comp pool whose hi/lo ratio exceeds `SMALL_POOL_MAX_RATIO`,
and (b) the pool being surfaced as **LOW confidence**.

Correcting a common misread: LOW confidence does **not** by itself haircut the
bid factor. `bid_factor` only steps 0.80 → 0.70 → 0.60 when a real
`grade_confidence` is present (the `/comic:grade` opt-in). So a *bare manual*
`comic-fmv` run on a thin, stale-split pool prices at the full 0.80 with no
automatic cushion — the LOW label is the honest signal, and in the graded
`/comic:buy` flow it does drive the 0.60 haircut. **Residual:** on a 2-comp pool
with a large date gap, the older comp is nearly zero-weighted (a 200-day gap →
weight 0.16), so the pool effectively prices off the single fresh comp. That is
defensible (the old sale is stale) but means "2 comps" can behave like "1 comp"
without tripping `too_sparse`. Treat a LOW-confidence thin FMV with the caution
the label implies rather than adding code.

**4. Calibration report `min_losses = 2` — no change; the fields to weigh are
already exposed.**

The documented caveat (median of two losses = their mean) is real but the report
already returns `loss_count`, `above_fmv_loss_count`, and `above_fmv_loss_rate`
alongside `overshoot`, so the guidance "weigh the loss spread, not overshoot
alone" is actionable today. Raising the default to 3 would make the median
outlier-robust but starve the report (few books accumulate 3 losses). Keep 2 as
the default; the human reads `loss_count`.

## Why This Matters

FMV sets real bid caps, and an estimator change that moves them silently is the
deflation-spiral failure mode's cousin. The dated baseline turns "trust me, the
math is right" into "any future move diffs loudly against a reviewed snapshot."
The half-life decision is now evidence-backed rather than a guess, and the two
watch-items are closed with reasons instead of left as vague unease.

## When to Apply

- Before changing `RECENCY_HALF_LIFE_DAYS`, the weighted quantiles, or the
  effective-n confidence rubric — regenerate the baseline and explain every move.
- When tempted to add a thin-pool or "cap the recency influence" guard — re-read
  §3 first; the mitigation is the `too_sparse` flag + the LOW label, not a clamp.
- When a live FMV looks surprising — check whether it's one of the explained
  moves (recent trend, stale-pool confidence drop) before treating it as a bug.

## Examples

Reproduce the diff (weighted vs the same pool with dates stripped):

```
cd apps/fmv && uv run --with pytest pytest tests/test_recency_baseline.py -q
# regenerate the frozen baseline on an intended change:
uv run --with pytest python tests/test_recency_baseline.py --regen
```

The direction review is baked into executable assertions
(`test_recent_high_pool_raises_fmv_vs_unweighted`,
`test_same_date_pool_reduces_to_unweighted_exactly`,
`test_half_life_is_exactly_75_days`,
`test_stale_pool_no_longer_earns_high_confidence`), so R7's "every move is
explained by recency" cannot silently regress.

## Related

- `docs/solutions/best-practices/fmv-self-referential-feedback-deflation-guard.md`
  — the deflation guard the same feedback loop rests on.
- Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md`
  (U2, R6, R7, KTD-7).
- Follow-up worth noting: this baseline uses constructed representative dated
  pools, not the user's live eBay comps (not reachable offline). Final half-life
  tuning against real dated comps remains an optional refinement — the harness
  is structured so real pools drop straight in.
