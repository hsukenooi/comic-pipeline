---
title: "Sold-comp ceiling for one-sided and too-wide refusals (BUI-1025)"
date: 2026-09-30
---

# Sold-comp ceiling for one-sided and too-wide refusals (BUI-1025)

**Date:** 2026-09-30. **Source:** `uv run --python 3.12 docs/audit/2026-09-30-sold-comp-ceiling.py`, against the live comics DB opened read-only. Every ledger read is pinned to comps first seen before **2026-09-30T01:00 UTC**, because `comic-fmv` was re-running on live rows during the session (the ledger grew from 5,390 to 5,676 raw graded rows between two runs; the pinned set stayed at 5,000 and the results did not move). No provider calls, no writes. BUI-1005's harness is imported unchanged (`load_comps`, `training`, `price_current`, `era`, `as_comp`).

**Decision: cancel.** Both variants fail the thresholds declared before scoring. The ceiling overpays far less often than the accepted path (13% and 26% of capped sales against 57%), but the variant that bids low enough to be safe loses 86% of the auctions it caps, and both variants fail the overpay-depth gate. That gate turned out to be ill-posed (see below), so the failure is not evidence of harm, but changing a gate after seeing the scores is the move this repo has canceled eleven times. Implementation would also need a server schema change (the `pricing_basis` CHECK), so nothing ships here.

## Method

- **Population:** BUI-1005's leave-one-out sales. N = 4,216. Current outcome: 2,398 priced, 584 `one_sided`, 566 `too_wide`, 668 `too_sparse`. The refused cut is the 1,150 shape refusals.
- **Ceiling pool:** the comps `fmv_math.build_pool` picks for the target grade g from the training comps, keeping those at grade ≥ g. A `one_sided` pool gets a cap only when every comp is above g. A `too_wide` pool uses its comps at or above g. Both need at least `MIN_PRICEABLE_POOL` (2), so a pool with one comp above gets no cap.
- **Variants:** A is the lowest sale in the ceiling pool. B is the median of the lowest grade rung in it.
- **Cap:** `clean_round(0.60 × ceiling)`. 0.60 is `bid_factor`'s LOW rung and `INTERPOLATED_BID_FACTOR`, the factor a LOW-confidence interpolated band gets. Arms at 0.80 and 1.00 (the no-haircut control) are reported. A cap that rounds to $0 counts as capped and loses the auction.
- **Yardstick:** the accepted path's own `max_bid` from `compute_fmv` on the accepted sales, with no photo `grade_confidence` (0.80 × `fmv_high`, or 0.60 on an interpolated band).
- **Scoring:** overpay means cap above the realized sale price. Room lost means cap below it. Percentiles are among the overpaid (or lost) sales. Price level buckets on the training pool's median, never on the sale price.
- **Declared gates:** T1 caps ≥ 20% of refused sales; T2 upper bound of the 95% book-clustered CI (seed 1025, 1,000 reps) on the overpay-rate gap ≤ +2 points; T3 overpay P90 ≤ the accepted path's; T4 every era and price cell with ≥ 30 capped sales within 5 points of its accepted-path rate; T5 median cap/price ≥ 0.50.

## Results

1,150 refused sales. 752 (65%) get a ceiling pool: 310 of 584 `one_sided` and 442 of 566 `too_wide`. The rest: 274 `one_sided` pools sit entirely below g, 122 `too_wide` pools have one comp at or above g, and 2 have none.

| Arm | Capped | Overpay rate | Overpay P50 / P90 | Room lost rate | Lost P50 / P90 | Median cap/price |
|---|---|---|---|---|---|---|
| A, 0.60 | 752 (65%) | 13.3% | 43% / 410% | 86.2% | 64% / 100% | 0.43 |
| B, 0.60 | 752 (65%) | 26.3% | 54% / 654% | 72.5% | 49% / 100% | 0.65 |
| A, 0.80 | 752 | 24.2% | 43% / 233% | 74.3% | 57% / 100% | 0.59 |
| A, 1.00 (control) | 752 | 33.8% | 48% / 210% | 65.0% | 48% / 93% | 0.75 |
| B, 1.00 (control) | 752 | 55.7% | 63% / 318% | 42.3% | 35% / 80% | 1.12 |
| **Accepted path, accepted sales** | 2,398 | **56.9%** | 56% / **385%** | 40.7% | 30% / 63% | 1.07 |

- **Overpay rate gap:** A −43.6 points (95% CI −47.0 to −40.2), B −30.6 (−34.4 to −26.7). Every era and price cell passes T4. By era, A overpays 16.5% (pre-1980), 11.5% (1980–1999), and 14.4% (2000+); B 28.5%, 24.9%, and 24.4%. The accepted path is 52–60% in every cell.
- **Room lost:** A caps 178 sales at $0 (cheap books where 0.60 × the ceiling rounds to nothing). Its median cap/price is 0.21 for 1980–1999 books and 0.24 under $20, so A is a no-bid for cheap books.
- **Gates:** A passes T1, T2, and T4 and fails T3 (410% against 385%) and T5 (0.43). B passes T1, T2, T4, and T5 and fails T3 (654% against 385%).

### Why T3 is ill-posed

A lower cap can never overpay more on any sale, yet the conditional P90 rises as the haircut deepens: 210% at 1.00, 233% at 0.80, 410% at 0.60 (variant A). Cutting the cap shrinks the overpaid set to the sales with the lowest realized prices, often a $1–$5 sale of a book whose other copies trade far higher. The metric measures selection, not exposure. The unconditional numbers (post-hoc, not gated) favor the ceiling: overpay in dollars P90 is $23 (A) and $37 (B) against $46, and mean overpay per capped sale is 60% and 103% against 241%.

The yardstick has the same problem in another form. In a second-price auction a cap above the realized price mostly means winning at that price, so the accepted path's 57% "overpay" rate is largely its win rate.

## If this is re-opened

Re-opening needs a fresh ticket with gates written before scoring: an unconditional exposure gate (dollar P90 or mean overpay per capped sale) instead of T3, and scoring on sales first seen after 2026-09-30T01:00 so the result is out of sample. B at 0.60 is the candidate; its rule in one sentence: on a `one_sided`-above or `too_wide` refusal with at least two comps at or above the target grade, cap the bid at 0.60 × the median of the lowest grade rung at or above the target, rounded down to the clean step (this run measured nearest rounding). Implementation needs a `ceiling` value in the `pricing_basis` CHECK (a server migration like `lone_sale`'s) and a flag, default off, and a pool change is proven by a live re-run after deploy, not by tests.

## Review notes

- **Leakage:** training is strictly before the sale (the harness's `training`). The ceiling pool also drops any comp priced within $0.01 of the held-out sale and sold within 7 days at any grade, so a provider copy with a drifted grade cannot set its own ceiling.
- **Money, cap above the ceiling:** `clean_round` rounds to the nearest $5 below $50, so 0.60 × a $5–$8 ceiling rounds up to $5. 35 A caps and 23 B caps land above their own ceiling, and 10 of each overpay the sale. Any shipped rule must round down.
- **Money, one comp above:** a `too_wide` pool with one comp at or above g gets no cap (122 sales). A mistagged comp can only lower the minimum, which costs room, not money.
- **Harness untouched:** the population, outcomes, and yardstick caps come from the imported functions and the unchanged `compute_fmv`. The population matches BUI-1014's (4,213; 585 / 567 / 668) within the pin's drift.

## Out of scope

- **Production share:** 274 of 584 `one_sided` refusals (47%) sit entirely below the target, close to the live split (29 of 59 `one_sided` rows are not all-above). No ceiling rule reaches them.
