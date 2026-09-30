# Grade-adjusted pool on the accepted path (BUI-1007)

**Date:** 2026-09-29. **Source:** `uv run --project plugins/gixen-overlay python docs/audit/2026-09-29-grade-adjusted-accepted-path.py`, against the live comics DB opened read-only. The ledger is the only input. The script imports the BUI-1005 backtest module for its training window, near-duplicate rule, and pricing functions, so both methods run the production `compute_fmv`.

**Decision: cancel.** The adjusted pool wins more paired comparisons than it loses, but its wins are small and its losses are large, so the paired gap itself is not distinguishable from zero. The win also depends on a slope fixed in advance at +15%: every slope fitted from data available at the time of sale gives a smaller gain or a loss.

## Method

- **Held-out sales:** the BUI-1005 leave-one-out, reproduced exactly (4,894 deduplicated graded raw comps, 4,114 held-out sales, 2,317 accepted by the current method, 2,113 of them priced by the adjusted pool, and 588/445/1,080 paired wins, losses, and ties at +15%).
- **Pairs:** the 2,113 accepted sales that every adjusted variant prices. Every method is scored on the same sales against the same current band. The other 204 accepted sales have fewer than 3 comps within ±2.0 grades. A shipped replacement would fall back to the current band on those, adding ties.
- **Paired gap:** (Winkler_adjusted − Winkler_current) / sale price, with BUI-979's `winkler_score` at alpha 0.5. A negative gap favors the adjusted pool.
- **Uncertainty:** book-clustered bootstrap, resampling `comic_id`s, 2,000 reps, 95% percentile CIs on the mean, 5%-trimmed mean, and median paired gap, on the difference of medians, and on the adjusted win share among non-tied pairs.
- **Strata:** era by comic year (pre-1970, 1970–84, 1985–99, 2000+, and unknown year), and quartiles of the training pool's median price. The quartiles use the training pool, not the sale price, because bucketing on the sale price conditions on the outcome.
- **Slopes:** fixed +15%; 0% (the unshifted ±2.0 pool, as a control); a pooled within-book slope fitted only on comps sold before the held-out sale; the book's own slope fitted only on its earlier comps; that slope shrunk toward the pooled slope (empirical Bayes) and toward +15%; and a half-shrunk slope (prior weight = the median book's Σ(g − ḡ)²).
- **Grades:** stored ledger grades, and grades re-parsed from each comp title with the BUI-1003-fixed `sold_comps.parse_grade`.

## Results (stored grades)

| Slope | Median slope applied | Median W/price, current → adjusted | Adjusted/current/tie | Win share of non-ties | 5%-trimmed mean gap | Mean gap | Difference of medians |
|---|---|---|---|---|---|---|---|
| +15% fixed | +15.0% | 1.132 → 1.020 | 588/445/1,080 | 56.9% [53.5, 60.5] | −0.021 [−0.055, +0.025] | −0.544 [−2.172, +0.349] | −0.111 [−0.146, −0.012] |
| 0% control | 0% | 1.132 → 1.100 | 374/341/1,398 | 52.3% [48.6, 56.1] | −0.016 [−0.037, +0.003] | −0.821 [−2.749, +0.191] | −0.032 [−0.082, +0.003] |
| Pooled, time-respecting | +5.0% | 1.132 → 1.073 | 466/388/1,259 | 54.6% [50.9, 58.4] | −0.024 [−0.047, −0.001] | −0.759 [−2.626, +0.216] | −0.059 [−0.105, −0.002] |
| Per-book, time-respecting | +7.8% (IQR −4.5 to +24.6) | 1.132 → 1.170 | 521/550/1,042 | 48.6% [44.9, 52.4] | +0.022 [−0.012, +0.059] | −0.985 [−3.136, +0.127] | +0.038 [−0.028, +0.089] |
| Per-book, half-shrunk | +6.5% | 1.132 → 1.105 | 520/441/1,152 | 54.1% [50.3, 57.9] | −0.022 [−0.047, +0.004] | −1.041 [−3.070, −0.002] | −0.027 [−0.079, +0.033] |

- **The median paired gap is 0.000 [0.000, 0.000] for every slope.** 1,000 of the 1,080 ties at +15% are identical bands, which are genuine: `compute_fmv` rounds bands to $5 steps, and a small shift lands on the same step.
- **The adjusted pool wins often but by little.** On the 1,113 sales where the bands differ, the median gap is −0.070 [−0.109, 0.000], but the trimmed mean is +0.047 [−0.095, +0.269]: its losses outweigh its wins in size.
- **The mean is decided by one book.** Amazing Spider-Man #50 (1967, `comic_id` 685) carries −1,502 of the −1,150 summed gap, because its ledger mixes 2020 variants sold at $0.99 with the 1967 key. Without that book, the mean gap at +15% flips to +0.168 [−0.048, +0.428], against the adjusted pool.
- **Empirical Bayes finds no slope heterogeneity.** The spread of per-book slopes (variance 0.075 in log terms, 325 books) is smaller than its sampling noise (0.084), so full shrinkage collapses every book to the prior. "Shrunk to pooled" equals the pooled row and "shrunk to +15%" equals the +15% row, so the table omits them.

## Strata (+15%, 5%-trimmed mean gap and win share)

| Stratum | Pairs | Trimmed mean gap | Win share |
|---|---|---|---|
| Pre-1970 | 339 | +0.183 [−0.151, +1.319] | 55.2% [47.7, 64.0] |
| 1970–84 | 345 | +0.002 [−0.069, +0.150] | 54.2% [48.1, 59.5] |
| 1985–99 | 691 | −0.047 [−0.082, −0.004] | 60.7% [54.0, 68.3] |
| 2000+ | 377 | −0.018 [−0.060, −0.001] | 58.5% [47.6, 70.9] |
| Price Q1 (<$10) | 493 | −0.020 [−0.070, +0.042] | 54.8% [46.0, 67.4] |
| Price Q2 ($10–24) | 563 | −0.067 [−0.116, −0.020] | 59.1% [53.6, 65.2] |
| Price Q3 ($24–46) | 522 | −0.030 [−0.069, +0.032] | 61.0% [55.7, 66.8] |
| Price Q4 ($46+) | 535 | +0.203 [−0.036, +0.734] | 52.0% [45.5, 58.7] |

The win is concentrated in 1985–99 books and $10–24 pools. Pre-1970 books and the top price quartile, where a mispriced cap costs the most, lean against the adjusted pool on the trimmed mean.

## Grade re-parse

Re-parsing titles with the fixed `parse_grade` changes 174 of 5,276 stored grades and none to "no grade". The result doesn't change: at +15%, 2,111 pairs, 575/439/1,097, win share 56.7% [53.5, 60.1], trimmed mean gap −0.026 [−0.055, +0.011], and the same slope ordering (per-book time-respecting 49.7%, pooled 53.8%).

## Decision against the rule

| Condition | Result |
|---|---|
| Paired-gap CI excludes zero, favoring adjusted | **No.** Mean, trimmed mean, and median CIs all include zero. Only the win share and the difference of medians clear zero. |
| Win not confined to one stratum | **Partly.** Significant in 1985–99, 2000+, and $10–24 only. Pre-1970 and $46+ lean the other way. |
| Survives slope sensitivity | **No.** A per-book slope fitted at sale time loses (48.6% win share). The pooled slope keeps about half the win. Only the +15% constant, taken from the whole-ledger per-book median (a mild look-ahead), shows the full effect. |

## Review notes

- **Leakage:** training comps and every fitted slope use only sales strictly before the held-out sale. The held-out sale's near-duplicates (same grade, price within $0.01, within 7 days) are dropped from both (this rule is approximate: 61 matches vs about 28 expected by chance, so about half are distinct sales; it shifts the training pool, not production; BUI-1020, BUI-1022). Two exceptions: the +15% constant and the empirical-Bayes prior weight come from the whole ledger. The pooled slope also uses comps first seen after the sale date, which production wouldn't have.
- **Same set:** every row of the slope table scores the same 2,113 sales against the same current band.
- **Population:** ledger-only pools with no first-party comps, as in BUI-1005. Wrong-book pollution (ASM #50) hits both methods, but it dominates any mean, so the decision relies on the trimmed mean and win share.
