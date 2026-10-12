# BUI-1231: slab price drift and recency weighting

Date: 2026-10-11. Analysis only, no production code. Script: `2026-10-11-slab-price-drift.py` (reads the BUI-1229 test set, built 2026-10-11; 9,442 CGC universal sales after dropping price outliers).

**Re-run 2026-10-13 (BUI-1260).** Every number below is re-stated against the rebuilt test set (`meta.built` 2026-10-13; 9,484 CGC universal sales after dropping 32 price outliers, up from 18 with BUI-1243's adjacent-grade rule). The rebuild also picks up main since 2026-10-11, including best-offer comp scaling (BUI-1234, BUI-1247) and the published-band widening (BUI-1242), so the weighting table moved with code as well as data. Drift rates do not touch `fmv_math`, so their small moves come from data and outlier flags only. **No headline conclusion flips: the verdict stays NO-GO.**

## Verdict: NO-GO on a time adjustment or a stronger recency weight

- **Drift is real but not stable.** Within book, grade, and page quality, prices fell **-4.6%/yr [-7.0%, -2.1%]** over 2022 to 2026, rose **+3.3%/yr [+0.5%, +6.8%]** from 2024, and rose **+21.6%/yr [+15.3%, +29.1%]** over the trailing 365 days. The sign depends on the window, so no single rate can correct old comps.
- **Adjusting older comps for drift makes Winkler worse at every rate tried (5%, 10%, 20%/yr).** On the shipped weights, +5% costs +0.008 [+0.005, +0.012] and +20% costs +0.038 [+0.027, +0.049]. Bias is already positive (+0.029) in the unadjusted pool, so lifting old comps pushes the band further above the sale.
- **A stronger recency weight is also worse.** Half-lives of 45, 90, and 180 days cost +0.110, +0.068, and +0.025 Winkler against the shipped weights. They also cut coverage (66%, 83%, and 89% priced, against 93%).
- **The best alternative is flat365, no weighting at all: -0.018 [-0.033, -0.006] against the shipped weights.** That is a 2.6% relative change, picked as the best of 12 schemes, and it is not stable across cuts (see below). Not enough to change the shipped rule.

## Method

- **Drift.** Cell = book x grade x page quality. Within each cell, log price is regressed on sale time with the cell mean removed, so a shift in which books, grades, or page qualities sold cannot read as drift. The pooled slope weights each cell by its time variance (OLS default). Drift per year is `exp(slope) - 1`. Tier is the cell's median price. The CI is a 95% percentile bootstrap that resamples BOOKS (2,000 reps, seed 1231).
- **Weighting.** Each PC CGC universal sale dated 2025-01-01..2026-10-10 (6,372 sales, 47 books; price outliers dropped from truth only) is priced with `fmv_math.graded_fmv` as of the day before. Comps are earlier PC universal sales of the same book, strictly before the sale's date, so the sale and its same-day sales never price it. Truth is PC's SOLD price (the accepted offer on a best-offer sale). PC carries the real transaction, so there is no list-price basis gap here, unlike the BUI-1229 ledger comps.
- **How the schemes run.** `fmv_math.graded_comp_weight` is patched per scheme and the rest of `graded_fmv` runs as shipped. A time adjustment multiplies a comp's price by `(1+r)^(age/365)`. The rates come from a fixed grid and are never fitted on the scored sales, so nothing leaks from the scored sale into its own estimate.
- **Score.** Winkler (alpha 0.5) divided by sale price. Scheme differences are PAIRED (sales both schemes priced), and the CI resamples books.
- **The "current pool shape".** sold-comps.com returns about 90 days, so `w90` (90-day comps only, weight 1.0) stands in for that pool. `prod` is the shipped rule: weight 1.0 to 90 days, 0.5 to 365 days, dropped after.

## Drift rates (per year, 95% CI clustered by book)

| Cut | 2022+ | 2024+ | Trailing 365d |
|---|---|---|---|
| All | -4.6% [-7.0, -2.1] | +3.3% [+0.5, +6.8] | +21.6% [+15.3, +29.1] |
| eBay only | -3.4% [-5.9, -0.8] | +3.9% [+0.7, +7.9] | +23.0% [+16.1, +30.6] |
| Silver (21 books) | -6.8% [-10.7, -3.2] | +0.9% [-1.9, +3.6] | +16.8% [+11.0, +22.8] |
| Bronze (13) | -4.2% [-6.2, -1.7] | +2.6% [+0.0, +6.1] | +21.7% [+9.4, +34.0] |
| Copper (2) | -0.6% [-1.8, +2.9] | +7.1% [+6.2, +7.5] | +46.4% [+18.9, +69.5] |
| Modern (11) | +4.0% [-5.7, +26.1] | +15.6% [-3.4, +44.1] | +37.0% [+14.2, +70.9] |
| < $150 | -5.1% [-7.5, -2.7] | -1.2% [-5.0, +2.6] | +13.3% [-1.0, +31.4] |
| $150 to $500 | -3.1% [-7.0, +0.9] | +5.3% [+2.5, +8.6] | +23.2% [+15.8, +30.9] |
| $500 to $2k | -1.6% [-7.1, +5.4] | +7.7% [+1.1, +16.7] | +19.8% [+11.6, +28.3] |
| >= $2k | -8.8% [-13.0, -3.9] | -0.3% [-6.4, +8.1] | +35.7% [+11.0, +74.1] |

- **The market turned.** Every large cell fell in 2022 to 2023 and rose in the last year. Under a 2022+ window, a drift correction would push old comps down. Under the trailing year it would push them up about 20%.
- **Era x tier cells are thin.** The script prints them (3+ books each), and most CIs span zero. Copper is 2 books. Modern x $500 to $2k reads +55% to +69%/yr from 4 books and 22 cells, a key-run effect, not a rate to use. The only tier that looks different is >= $2k, which fell hardest (-8.8%) and rose hardest (+35.7%), but its CIs are wide.
- **The set has no Silver book above about $2k a copy in the ledger.** Read tier rows as drift among the books and grades PC covers, not the market.

## Winkler by scheme (2025-01-01..2026-10-10, PC truth, PC-history comps)

| Scheme | Priced | Winkler [95% CI] | Paired change vs shipped [95% CI] |
|---|---|---|---|
| w90 (sold-comps window) | 86% | 0.796 [0.696, 0.956] | +0.133 [+0.060, +0.261] |
| **prod (1.0 / 0.5, 365d cap)** | 93% | **0.683 [0.640, 0.737]** | reference |
| flat365 (no weighting) | 96% | 0.686 [0.644, 0.737] | **-0.018 [-0.033, -0.006]** |
| exp half-life 45d | 66% | 0.704 [0.654, 0.772] | +0.110 [+0.075, +0.149] |
| exp half-life 90d | 83% | 0.693 [0.646, 0.760] | +0.068 [+0.040, +0.107] |
| exp half-life 180d | 89% | 0.672 [0.635, 0.717] | +0.025 [+0.013, +0.039] |
| prod + adj 5%/yr | 93% | 0.691 [0.647, 0.746] | +0.008 [+0.005, +0.012] |
| prod + adj 10%/yr | 93% | 0.700 [0.655, 0.756] | +0.018 [+0.012, +0.024] |
| prod + adj 20%/yr | 93% | 0.720 [0.671, 0.780] | +0.038 [+0.027, +0.049] |
| flat365 + adj 10%/yr | 96% | 0.705 [0.660, 0.762] | -0.001 [-0.015, +0.011] |
| flat365 + adj 20%/yr | 96% | 0.738 [0.689, 0.802] | +0.028 [+0.014, +0.043] |
| flat730 + adj 10%/yr | 97% | 0.726 [0.680, 0.783] | +0.018 [-0.006, +0.041] |
| flat730 + adj 20%/yr | 97% | 0.803 [0.745, 0.873] | +0.086 [+0.057, +0.118] |

Lower Winkler is better. The paired change is on sales both schemes priced, so it can differ from the gap between the two unpaired means when coverage differs.

- **History beats the 90-day window by far.** `w90` is +0.133 worse than the shipped weights and prices 7 points fewer sales. This is the value of reaching back to 365 days, not of any weighting. Of the 12 alternatives, none beats the shipped rule except flat365.
- **flat365's edge comes from the older, thinner half of the data.** It wins in 2025 (-0.037 [-0.067, -0.012]) and ties in 2026 (-0.007 [-0.016, +0.002]). By era it wins Silver (-0.015 [-0.027, -0.003]), Bronze (-0.039 [-0.091, -0.011]), and Copper (2 books), and loses Modern (+0.012 [-0.018, +0.043], CI spans zero). eBay-only is the same as all (-0.018).
- **Why adjustment hurts.** Every scheme's bias is positive over the full window (+0.03 to +0.14), and +0.106 on the shipped weights in 2025 when prices were rising (-0.019 in 2026). The band already sits above the typical sale, so raising old comps adds to the error. Measured drift is a mean log slope, and the band's problem here is width and ladder interpolation, not stale level.
- **Per-era cells are thin.** Copper is 2 books, so its CIs are not credible. Silver, Bronze, and Modern are 21, 13, and 11 books. No per-era result changes the verdict.

## Caveats

- **Selection.** "Best of 12" overstates flat365's gain. The 12 schemes share sales, so their CIs are not independent. Treat -0.018 as an upper bound on a real effect.
- **Regime.** The score window (2025 to 2026) is mostly a rising market, so a drift adjustment had its best chance and still lost. A falling market would favor older-comp discounts more, which also argues against a fixed rate.
- **Comps are PC history, not the live pool.** The live slab pool mixes sold-comps.com sales with ledger rows from August 2026. PC history is denser and includes Heritage (19% of sales; they score worse than eBay per BUI-1229). The eBay-only run reaches the same conclusion.
- **Drift windows are choices.** The three windows were fixed before scoring. The sign flip between them is the finding.

## Out-of-scope findings

- **flat365 is a possible ledger-pool experiment, not a change.** If anyone revisits the 0.5 stale weight (`GRADED_STALE_WEIGHT`), test it on the live pool (ledger comps, post-BUI-948 `as_of`) first. This result is on PC history only.
- **The Winkler bias is positive everywhere.** Mean `(mid - y) / y` is +0.03 on the shipped weights over this window. A level shift is a different lever from recency and belongs in the width and bias work.
