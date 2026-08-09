---
title: "The ungraded anchor is a liquidity signal, not a price"
date: 2026-08-09
category: best-practices
module: "apps/fmv/src/fmv_math.py (ungraded_market_anchor, interpolate_grade_curve), apps/fmv/src/fmv_runner.py (_build_notes anchor token)"
problem_type: best_practice
component: fmv_pipeline
severity: high
mechanized_by: test
enforced_by_test:
  - apps/fmv/tests/test_fmv_math.py::test_surfaced_in_compute_fmv_output
  - apps/fmv/tests/test_fmv_math.py::test_thin_above_bracket_suppressed
related_components:
  - "fmv"
applies_when:
  - "A ticket proposes pricing a needs_manual book off the BUI-522 ungraded anchor"
  - "A ticket describes a value as 'computed but withheld by the money guards' — check whether it is computed at all"
  - "Proposing to publish an FMV band whose only support is a value/price threshold ('cheap books are safe')"
  - "Choosing a second estimator to triangulate an FMV against"
  - "About to relax MIN_BRACKET_COMPS (the BUI-318 thin-bracket guard) to obtain an estimate"
tags:
  - "fmv"
  - "ungraded-anchor"
  - "oracle-bound"
  - "needs-manual"
  - "thin-bracket"
  - "measurement"
  - "negative-result"
---

# The ungraded anchor is a liquidity signal, not a price

## Context

BUI-713 proposed publishing a LOW-confidence FMV band from the BUI-522 ungraded
anchor for books the graded pool cannot price, gated on the anchor **agreeing**
with an independent estimator: either the §7 interpolation "computed but withheld
by the money guards", or — where no second estimator exists — a value gate for
cheap books (anchor under ~$50, "where grade-blindness is immaterial in dollars").

Measured first, against the prod comps ledger (14,655 raw comps, 417 comics) and
every `fmv` row carrying an `ungraded_anchor=` token. Both gates failed, for
different reasons, and the reasons generalize past this ticket.

## The suppressed §7 value mostly does not exist

Replayed all 115 unpriced anchored rows; 103 stay unpriceable under the shipped
math on the ledger pool (replay fidelity: anchor median deviation 0.0%,
flag_reason reproduced on 100 of 115).

**Of those 103, exactly 0 have a §7 interpolation available under the shipped
BUI-318 thin-bracket guard.** Not withheld — not computable. Relaxing the guard
to admit lone-comp bracket ends yields a value for 15 of 103, and those 15
disagree with the anchor by a median of 42% (max 242%); only 3 land within 10%.

Both of the ticket's motivating datapoints were wrong, and each in an
instructive way:

| ticket claim | measured |
|---|---|
| Invincible #7: "§7 interpolates ~$235 vs anchor $244, 4% apart" | The $235 is real but its bracket is **one comp at 7.0 ($200) and one comp at 9.4 ($257)** — the literal BUI-318 wild-over-bid shape, smeared across a 2.4-grade span |
| Invincible #10: "interpolates ~$322 vs anchor $315, 2% apart" | **No interpolation exists.** The entire graded pool is a single comp at 9.4. `interpolate_grade_curve` returns None with or without the guard |

The lesson is a premise check, not an FMV fact: **when a ticket says a value is
"computed but withheld", confirm it is computed.** A guard that returns `None`
before the arithmetic leaves nothing behind to agree with, and a guard that
returns a value only once you disable it is not withholding — it is refusing.

## The anchor disagrees with known-good prices by ~40%

The value gate has no second estimator by construction, so its whole safety
argument rests on the anchor being a defensible price. Measured directly on the
212 unflagged rows where both an anchor and a real graded price exist:

| cohort | n | median \|anchor/fmv_high − 1\| | within 25% | anchor > graded high |
|---|---|---|---|---|
| all priced + anchored | 212 | 34% | 38% | 8% |
| anchor < $50 (the proposed gate) | 160 | 38% | 34% | 8% |
| anchor < $50 and anchor_n ≥ 8 | 131 | 41% | 32% | 7% |

The anchor sits systematically **below** the graded `fmv_high` (median ratio
0.60–0.67), which is the money-safe direction — but being biased low is not the
same as being right, and the error is **grade-dependent rather than random**:
median ratio 0.43 at target grades 6.5–7.5 versus 0.70 at 9.0–9.2. Grade-blindness
is not immaterial for cheap books; it is merely cheap to be wrong about. Against
first-party outcomes the gap shows again from the other side: on 72 resolved LOST
auctions the winning price cleared at a median **1.49× the anchor** (57 of 72
above it, p90 2.60×).

**A conservative wrong number is still a wrong number**, and publishing one is not
free: it lands in `fmv.high`, which the overlay's `over_fmv` and `recomputed_cap`
policy checks, `/comic:calibration-report`, and any future slab-ladder comparison
all read as a measured value. Turning "we do not know" into a stored number that
reads exactly like a priced one is the cost the value gate was not charged for.

## The one live lead, and why it did not ship either

Anchor **precision predicts anchor accuracy**, monotonically — the only genuine
data-derived signal the measurement turned up:

| dispersion of the grade-less prices | n | median \|err\| | within 25% | anchor > graded high |
|---|---|---|---|---|
| cv < 0.30 | 38 | 18% | 71% | **0%** |
| cv 0.30–0.50 | 42 | 26% | 50% | 7% |
| cv 0.50–0.80 | 50 | 41% | 30% | 10% |
| cv ≥ 0.80 | 73 | 47% | 22% | 8% |

Tightened all the way (anchor < $50, n ≥ 8, cv < 0.30) the validation cohort has
median error 12%, 74% within 25%, and 0 of 23 rows where the anchor exceeded the
graded high. It was still not shipped, for three reasons worth keeping:

1. **The threshold was chosen by looking at the data it would be validated on.**
   No held-out set, n = 23 in the deciding cell — 0 of 23 is a ≤12% over-rate at
   95% confidence, not 0%.
2. **The validation cohort is rows that priced; the candidates are rows that did
   not.** That boundary is exactly what cannot be validated, because a candidate
   has no graded truth by definition.
3. **Self-dispersion is not an independent estimator.** "The raw market is tight"
   is a statement about precision; the gate needs accuracy. The two correlate here,
   on one collection sweep, which is a lead for a measured follow-up and not a
   licence to price money off it.

The oracle for that tightened gate is **14 rows (9 distinct books) of 103
candidates** — 17 rows if the price cap is dropped and only the tightness and
depth gates are kept — and every one of them comes from a single Invincible
sweep, not from a cross-section of the collection.

## Guidance

- **The ungraded anchor answers "did this book trade, and roughly where", not
  "what is a copy at grade G worth".** Keep it informational (BUI-522) and
  flag-only (BUI-534). It is enforced by
  `test_surfaced_in_compute_fmv_output`, which asserts the anchor never enters
  the priced pool — do not weaken that test to publish a band.
- **Do not obtain a second estimator by relaxing a money guard.** An estimate
  that only exists once `MIN_BRACKET_COMPS` is disabled carries exactly the risk
  the guard was measured into existence to stop; agreement with it is not
  triangulation, it is two readings of the same thin data.
- **A price threshold is not an agreement signal.** "Cheap books are safe" is a
  pool-shape hypothesis stated in dollars — the same class as the FMV signal
  tickets that keep dying at the oracle bound. Cross-check the estimator against
  known-good prices before the threshold is even worth discussing.

## See also

- `docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md`
  — the discipline this measurement applied, including the direction half.
- `docs/solutions/best-practices/fmv-grade-curve-interpolation-overbid-guards.md`
  — why the §7 thin-bracket guard exists.
- BUI-714 (the slab ladder as a genuinely independent second estimator) is the
  place the triangulation idea can still be tested; it needs its own oracle first.
