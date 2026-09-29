# Grade oracle bound on raw comps (BUI-1014)

**Date:** 2026-09-29. **Source:** `docs/audit/2026-09-29-grade-oracle-bound.py` (`where`, `bound --seeds 10`, `curve --seeds 5`, `ceiling`), run with `uv run --python 3.12` against the live comics DB opened read-only and pinned to rows first seen before 2026-09-29T09:00 UTC. The slab watch set and wish-list are read from the comics server. No provider calls, no ledger writes. The pricing math is BUI-1005's harness, imported unchanged.

**Verdict: the gate passes. Continue.** Perfect grades on the ungraded comps would price 38% of the refused sales (net of sales they newly refuse), and 44% on the slab watch books, against a gate of 20%. On the sales that price either way, they improve Winkler/price by 0.13 (95% CI 0.02 to 0.21), against a gate of 0.05. The margin belongs to the oracle, not to a realistic grader. At the 40% coverage a selective grader reaches, both numbers sit on the line: 18.6% net resolved (20.9% on watch books) and +0.051 Winkler. A control that assigns random grades from the book's own graded comps, with no grader at all, resolves 16.6%.

## Ledger counts

| Pool | Non-excluded comps | Ungraded |
|---|---|---|
| Raw | 15,815 | 9,692 (61.3%) |
| Slab (certifier present) | 1,002 | 0 |

The ticket's "8,700 of 14,133" is BUI-1006's older count. After the harness's filters (no `no-variant` or `broader` tier, a parsed date, cross-provider dedup), 4,993 raw comps are graded and 7,821 are not: a 61.0% ungraded rate. Every ungraded comp is raw.

## Method

- **Population:** BUI-1005's leave-one-out sales. Each is a graded raw sale with at least one same-book comp within ±2.0 grades in the prior 90 days. N = 4,213 (up from 4,114 as the ledger grew). Today 1,820 are refused: 585 `one_sided`, 567 `too_wide`, and 668 `too_sparse`.
- **Blanking (direction 1):** a random share of the graded comps loses its grade and goes into `compute_fmv` with `grade=None`, which is exactly what an ungraded comp is today. `build_pool` drops it, and only the informational ungraded anchor sees it. Two designs:
  - **`strat` (the ticket's):** per era × comp-price stratum, at that stratum's real ungraded rate.
  - **`book`:** each book at its own ungraded rate, which keeps the per-pool loss realistic.
- **Arms:**
  - **Blanked:** stands in for today.
  - **Full:** every comp keeps its true grade (the oracle).
  - **Select40:** true grades restored on a random 40% of the blanked comps.
  - **Random:** every blanked comp gets a grade drawn from the book's known-grade comps sold before the sale. It has the oracle's comp count but no grade information.
- **Scoring:** the harness's `winkler_score` / price, medians, and book-clustered bootstrap CIs (1,000 reps). Ten seeds. Price levels are bucketed on the training-pool median, never on the sale price.
- **Checks:** with no blanks, the harness's `price_current` result matches on every row of all 4,213. Passing a comp as `grade=None` gives the same band as removing it on every row. Each stratum's blank rate matches its ungraded rate within rounding.

## Results

Refusals resolved are net: sales the arm newly prices, minus sales it newly refuses, as a share of the blanked arm's refusals. The Winkler gap is blanked minus arm on the rows both price, so positive means the arm is better. Values are medians over 10 seeds.

| Cut (`strat`) | Refused (blanked) | Net resolved: full | Net resolved: select40 | Net resolved: random | W/price gap: full [CI] | W/price gap: select40 [CI] |
|---|---|---|---|---|---|---|
| All | 2,935 | **38.0%** (36.9–39.0) | **18.6%** (17.6–19.8) | 16.6% | **+0.129** [+0.020, +0.206] | **+0.051** [−0.001, +0.119] |
| Under $20 | 1,080 | 37.5% | 18.9% | 14.2% | +0.113 [+0.003, +0.258] | +0.052 |
| $20–$100 | 1,557 | 39.1% | 18.2% | 18.4% | +0.066 [−0.043, +0.191] | +0.037 |
| $100 and up | 302 | 34.9% | 15.8% | 13.1% | +0.041 [−0.130, +0.192] | +0.002 |
| Slab watch books | 430 | 44.1% | 20.9% | 19.4% | +0.109 [−0.124, +0.279] | +0.043 |

The `book` design agrees. Overall it resolves 35.5% for full, 17.6% for select40, and 16.9% for random, with a full Winkler gap of +0.104 [+0.025, +0.183] and a select40 gap of +0.032. On watch books it resolves 41.7% for full and 21.1% for select40. Seed ranges are narrow, within ±2 points on the resolved shares.

- **Grades carry information, not just count:** on the same rows, random grades score +0.107 [+0.039, +0.166] W/price worse than true grades. The random arm prices nearly as many sales as select40 but with worse bands.
- **Rescued sales price worse than accepted ones:** the full arm's rescues score a median W/price of 1.17, against 1.10 for its accepted sales. The pattern matches BUI-1005 (hard sales stay hard), but the gap is smaller.
- **Price cut:** the full oracle clears 20% at every price level. Select40 falls below 20% at $100 and up (15.8%) in both designs, so the selective verdict depends on the price level.

### The proxy runs at the wrong density, conservatively

Blanking compares a pool at 39% of today's graded density with one at 100%. Grading the real ungraded comps compares 100% with 257%. Guards are count-driven, so `curve` measures the same ×2.57 step from several base densities (nested subsets, true grades):

| Base density | Refused at base | Resolved at ×2.57 | Resolved at 40% selective |
|---|---|---|---|
| 0.10 | 92.5% | 15.1% | 6.3% |
| 0.20 | 84.1% | 25.4% | 12.6% |
| 0.30 | 76.1% | 34.6% | 16.9% |
| 0.35 | 72.6% | 38.1% | 19.2% |

The resolved share rises with the base, so the proxy likely understates the real-density effect rather than overstating it. On real data (`ceiling`), 92.6% of today's 1,820 refusals have an ungraded same-book comp in their 90-day window, and 79.2% have three or more. The upside isn't capped by missing sales. The 780 sales the harness excludes for having no graded comp are a different case: imputation from the book's graded history rescues almost none of them (0.3%), because the book has never shown a grade near theirs.

### No-grader imputation on real data

`ceiling` imputes a grade for each real ungraded comp in the window from same-book graded comps sold before the sale. The price-matched variant draws from the 3 comps nearest in log price. The random variant draws from all of them. The price-matched variant rescues 27.5% of today's refusals and the random one 27.9%, with a same-row W/price change of −0.003 [−0.061, +0.054] and −0.022. The rescues score 1.20 and 1.25, worse than accepted sales. Imputation buys count but not band quality. Measured grades are what improve Winkler.

## Where the ungraded comps sit (direction 2)

- **Spread:** even across eras (57% to 66% ungraded) and price levels (61% to 63%). The median price is $24.95 ungraded against $24.99 graded. On the same book, ungraded sales run ×0.89 of graded ones, so they skew slightly lower-grade.
- **Concentration:** 463 books hold ungraded comps, and only 39 comps sit on books with no graded comp. The top 10% of books hold 33%, led by X-Men #97 (202), Wolverine #50 (158), and Spawn #1 (108).
- **Photo window:** 38.5% (3,013) sold within the last 90 days. That matches the 90-day pricing window, so a grader running on new comps sees every comp a live pool uses.
- **Watched books:**
  - 16.0% sit on 47 of the 53 slab watch books (571 in the last 90 days).
  - 36.8% sit on wish-listed books, matched by name.
  - 20.6% sit on the 141 books with a refused raw FMV row today.
  - 86.0% sit on books we have bid on.

## Deviations and caveats

- **Watch set:** the "51-book watch set" is the slab watch set (53 books today). It is slab-only, so the bound runs on the raw sales of those books (594 sales), not on slab pools.
- **Perfect grades:** select40 assumes perfect grades on a random 40%. A real selective grader picks its confident comps, which aren't random, and still makes errors (BUI-1013: 77% at 40% coverage). The select40 numbers are an upper bound for that grader.
- **Live data:** the ledger and `comics.year` changed during the session (30 comps arrived at 09:42). The pin fixes the comps. The era split shifted by about 50 books as years were backfilled, which isn't material here.

## Review notes

- **Leakage:** training stays strictly before each sale. The imputation donors are the book's graded comps sold before the sale, excluding the sale and its copies. An earlier draft drew donors from the book's whole history, and that leak inflated imputation to 58% rescued and +0.09 Winkler. The corrected numbers are the ones above. 187 grade-less rows are provider copies of a graded held-out sale (same book, price within $0.01, within 7 days) and are dropped from `ceiling`.
- **Harness untouched:** the full arm is the harness's own `price_current` call, and `load_comps`, `training`, `metrics`, and `cluster_bootstrap_gap` are imported.

## Out of scope

- **`bids.comic_id` is NULL on all 812 rows:** BUI-1005's fresh-batch query (`FROM bids WHERE comic_id = ?`) can never return a row. Its "no bid yet" statement for the 24 books was unverifiable, not verified. Bids reach a book through `bid_fmvs` or `bids.fmv_id` → `fmv.comic_id`.
- **Imputation lever:** the no-grader imputation is a pool-shape lever (28% of refusals priced). Per the standing rule it is not a proposal. Its rescues score worse than accepted sales.
