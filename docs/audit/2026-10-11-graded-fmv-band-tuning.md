# BUI-1230: Tune graded_fmv band constants on PriceCharting history

Date: 2026-10-11. Analysis only, no production code. Script: `2026-10-11-graded-fmv-band-tuning.py`. Test set: BUI-1229 artifact, `meta.built` as of 2026-10-11.

## Verdict

- **The band is too narrow.** 44% of priced sales (322 of 735) come from the ladder and lone-sale tiers, which print a zero-width band (`fmv_low == fmv_high`). They hit 2% and 3% of the time. The exact tier's Q25..Q75 band is roughly 10% of price wide and hits 24%.
- **Best constants (train-selected):** a ±15% half-width on the point tiers, a ±15% floor on the exact tier's band, and `SMALL_POOL_MAX_RATIO` off. The quantiles (Q25/Q75), the exact gate (2.0), the rung minimum (3), and the strict exact grade stay as they are.
- **Holdout gain is real.** Winkler falls from 0.843 [0.723, 0.972] to 0.667 [0.582, 0.761]. The paired gain on the 259 co-priced holdout sales is **-0.176 [-0.226, -0.126]**. In-band rises from 10% to 50%. Coverage is unchanged at 77%.
- **Go/no-go: conditional GO for the two width knobs only.** Ship `h_point = h_direct = 0.15` (the "ship" row) with `max_bid` still computed from today's unwidened `fmv_high`. Keep `SMALL_POOL_MAX_RATIO` at 3.0: it is the BUI-179 money guard, and it buys only 0.019 of holdout Winkler. **NO-GO** on shipping the widened `fmv_high` into the bid cap. It lifts `fmv_high` by a median 13.6%, and the band already sits 12% above the sale on holdout (bias +0.122).

## Method

- **Rows.** These match the BUI-1229 baseline: 904 PC CGC universal sales in 2026-08-01..2026-10-10, minus price outliers. Each sale is priced as of the day before, from ledger comps sold strictly before it, with every `same_sale` twin hidden. Truth is PC's SOLD price (the accepted offer on best-offer sales). The ledger comps hold list price there (BUI-1222). A list-basis line runs beside the holdout.
- **Re-implementation.** `band()` is `graded_fmv`'s decision tree with its constants as parameters. It covers all three tiers, the BUI-1186/1204 page-quality widen, the envelope clamp, and every refusal. At current constants it matches `fmv_math.graded_fmv` on **904 of 904** rows (low, high, and refusal reason).
- **Split.** The split is by book and was fixed before any result was read. Books are stratified by era, shuffled with seed 1230, and every third book is held out. That gives 30 train books (568 sales) and 17 holdout books (336 sales). Holdout comic ids: 18, 119, 227, 494, 500, 505, 661, 685, 720, 745, 749, 783, 798, 968, 972, 1021, 1023.
- **Grid.** 384 configs, every one tried once, none refined afterwards:

| Knob | Values | Current |
|---|---|---|
| Exact-tier quantile `q` (band = Q(q)..Q(1-q)) | 0.25, 0.15 | 0.25 |
| `h_direct` (exact-band half-width floor, relative to median) | 0, 0.15, 0.30 | 0 |
| `h_point` (half-width for ladder and lone-sale points) | 0, 0.15, 0.30, 0.45 | 0 |
| `GRADED_EXACT_MIN_EFFECTIVE_N` | 1.0, 2.0 | 2.0 |
| `GRADED_LADDER_MIN_RUNGS` | 2, 3 | 3 |
| Exact grade window (±) | 0, 0.5 | 0 |
| `SMALL_POOL_MAX_RATIO` (`too_sparse`, lone-sale bracket) | 3.0, off | 3.0 |

- **Refusal guard.** A config is eligible only if its train coverage is at least the current 83.8%. Train Winkler ranks the eligible configs. All 384 cleared the bar: no config won by refusing. The gain is also scored paired, on sales both configs price.
- **CIs.** These are 95% percentile bootstraps (2,000 reps, seed 1229) that resample books, as in BUI-1229.

## Results (sold basis)

| Set | Config | Coverage | Winkler [95% CI] | In band | Bias | Paired vs current |
|---|---|---|---|---|---|---|
| Train | current | 84% | 0.723 [0.610, 0.932] | 17% | +0.060 | |
| Train | best | 84% | 0.622 [0.539, 0.771] | 60% | +0.053 | -0.101 [-0.180, -0.059] |
| **Holdout** | current | 77% | 0.843 [0.723, 0.972] | 10% | +0.122 | |
| **Holdout** | **best** | 77% | **0.667 [0.582, 0.761]** | 50% | +0.113 | **-0.176 [-0.226, -0.126]** |
| **Holdout** | ship (ratio kept) | 77% | 0.686 [0.587, 0.789] | 50% | +0.122 | -0.157 [-0.193, -0.119] |
| All | current | 81% | 0.765 [0.670, 0.901] | 15% | +0.081 | |
| All | ship | 81% | 0.655 [0.584, 0.754] | 56% | +0.082 | -0.110 [-0.151, -0.077] |

The pick is stable. The top five train configs all share `h_direct = h_point = 0.15`, and their holdout Winkler spans 0.667 to 0.686. One knob at a time from current (train): `h_point` 0.15 gives 0.659, `h_direct` 0.15 gives 0.703, and ratio off gives 0.703. Raising coverage costs Winkler. The ±0.5 window lifts coverage to 92% and Winkler to 0.892. An exact gate of 1.0 gives 88% and 0.728. Two rungs give 85% and 0.744.

**Holdout cuts, best config.** Every era cell is thin, and Copper is 1 book, so its CI is meaningless.

| Cut | Sales | Current Winkler | Best Winkler [95% CI] | Best in band |
|---|---|---|---|---|
| Silver (7 books) | 166 | 0.910 | 0.688 [0.534, 0.886] | 44% |
| Bronze (5 books) | 119 | 0.778 | 0.631 [0.592, 0.693] | 57% |
| Copper (1 book) | 12 | 0.571 | 0.532 (1 book) | 73% |
| Modern (4 books) | 39 | 0.832 | 0.737 [0.570, 0.801] | 52% |
| eBay | 274 | 0.753 | 0.618 [0.530, 0.733] | 54% |
| Heritage | 62 | 1.194 | 0.857 [0.609, 1.065] | 36% |
| List basis (truth = ask) | 336 | 0.820 | 0.653 [0.569, 0.746] | 51% |

**By tier, all rows (current to best):** direct 0.618 to 0.570 (in band 24% to 61%), ladder 1.011 to 0.758 (2% to 51%), lone sale 0.755 to 0.626 (3% to 53%). Refusals are unchanged: `outside_ladder` 103, `ladder_too_thin` 33, `ladder_non_monotone` 26, and `no_certifier_pool` 7.

## Why the bid cap must not follow

- `max_bid` is `bid_factor × fmv_high`. On the 735 co-priced sales, the ship config raises `fmv_high` by a median 13.6% (mean 12.2%). The share of sales that cleared below `fmv_high` rises from 63% to 81%.
- The `h_direct` floor can lift `fmv_high` above the envelope clamp's cap, which today bounds it. The clamp's job is to keep a thin exact bucket from setting a high cap.
- The band is already biased high: +0.122 on holdout, +0.289 on Heritage. Widening is symmetric, so it fixes calibration, not centring. A centring fix (sold vs list basis, Heritage) is a separate question.
- Implementing it means widening only the published `fmv_low`/`fmv_high`, deriving `max_bid` from the pre-widening high (or the median), and updating the `recomputed_cap` parity test. That is a follow-up ticket, not this audit.

## Limits

- 46 books priced, 17 in holdout. Holdout books are harder than train ones (current 0.843 vs 0.723), so the split is not flattering.
- The grid is coarse. The true width optimum may sit anywhere from 0.10 to 0.25. Refining it on 17 books would overfit, so this audit doesn't.
- Age weights (90/365 days, 0.5) were not tuned. That's BUI-1231's drift question.
- Only ledger-only pricing over a 10-week window is scored, because ledger capture began 2026-08-04.
