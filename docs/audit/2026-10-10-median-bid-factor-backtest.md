---
title: "Median bid factor backtest (BUI-1219 phase 1)"
date: 2026-10-10
---

# Median bid factor backtest (BUI-1219 phase 1)

**Date:** 2026-10-10. **Source:** `uv run --project plugins/gixen-overlay python docs/audit/2026-10-10-median-bid-factor-backtest.py`. Reads the comics server over HTTP through `comics-api` only. No provider calls, no DB file, no writes. Reuses BUI-1213's ledger loader and pool reconstruction.

**Finding: the median anchor works for the top tiers and breaks for the low tiers.** For HIGH, MEDIUM-HIGH, and MEDIUM books, fmv_high sits 1.18 to 1.30 times the median, so today's 0.80 x high already equals about 0.95 to 1.04 x median. A median factor below 0.95 is a cut for these tiers, not a raise. For MEDIUM-LOW and LOW books, fmv_high sits 1.46 to 2.27 times the median. At 1.00 x median they lose 13% (LOO) to 24% (OURS) of the books today's cap wins, and lower factors lose more. The ticket's "Low 0.60 to 0.70" on the median would cut our low-tier win rate from 44% to 12 to 14%.

## Method

- **Populations:** LOO = 1,425 ledger auction sales priced from the book's other graded raw comps sold in the prior 90 days. OURS = 355 of our resolved raw auctions, priced the same way with our own listing left out.
- **Pricing:** `fmv_math.compute_fmv`, unchanged, gives the median, fmv_high, the fine label, and the interpolation marker.
- **Today:** 0.80 x high (0.60 x high when interpolated). No grade_confidence, which matches the live bids at 0.80 to 0.83 x high in every tier.
- **Win:** cap at or above the sale price (proxy bidding treated as second price). **Paid/med:** mean (price / median - 1) on wins. **+won:** sales today loses and the rule wins, with their paid/med. **Lost:** sales today wins and the rule loses.

## Results (OURS, stored tier; LOO in brackets)

| Tier (n) | high/med | Today win | 0.90 x med | 1.00 x med | 1.10 x med |
|---|---|---|---|---|---|
| high 30 [88] | 1.18 | 20% [58%] | 23% [56%] | **40% [62%]**, +6 at -12%, lost 0 | 57% [74%], +11 at -4% |
| medium 101 [348] | 1.24 | 48% [72%] | 39% [67%], lost 12 | 50% [74%], +6 at -13%, lost 4 | 60% [77%], +16 at -2% |
| low 218 [958] | 1.46 [2.05] | 44% [72%] | 30% [58%], lost 35 | 40% [64%], lost 23 | 50% [67%], lost 16 |
| interp 6 [31] | 1.15 | 33% [42%] | 50% [58%] | 50% [68%] | 50% [68%] |

- **Low at 0.60 / 0.70 x median:** 12% / 14% win on OURS (lost 70 / 66 of 218). LOO: 35% / 43%, against 72% today.
- **Interp at 0.70 x median:** identical to today's 0.60 x high (lost 0, +0).
- **0.95 equals 1.00:** `clean_round` rounds to the nearest $5/$10/$25 step. Since the median is already a clean step, 0.95 x median rounds back to the median on almost every row. Phase 2 must floor, or pick factors at least one step apart.
- **Ticket live data agrees:** the reconstruction gives today's win rate as high 20%, medium 48%, low 44%. The last 50 bids gave 0/4, 42%, and 41%.

## Five tiers: does each deserve its own coefficient?

Tiers use the fine label `compute_fmv` returns at hold-out time (not the stored collapse), plus the window (`<= 0.5`) and cv it returns. VERY HIGH = HIGH + window +/-0.5 + cv <25%. HIGH = other HIGH. MEDIUM = MEDIUM-HIGH + MEDIUM. LOW = MEDIUM-LOW. VERY LOW = LOW. Interpolated books stay a separate row. The script sweeps cap = k x median for k 0.80 to 1.20 (step 0.05) with `clean_round`; full tables are in its output.

**Suggested k** = the highest k whose pooled wins (ours + ledger) pay at most the median on average AND whose share of wins above the median is at or below today's cap's share.

| Tier | n ours / ledger | Pricing error | Today win (ours / ledger) | Today above-med | Suggested k (sales) | Win at k (ours / ledger) | Paid/med at k |
|---|---|---|---|---|---|---|---|
| VERY HIGH | 18 / 42 | 29% (19% ours) | 28% / 67% | 0% | 1.00 (60) | 50% / 76% | -29% |
| HIGH | 12 / 46 | 30% (19%) | 8% / 50% | 4% | 1.05 (58) | 25% / 50% | -36% |
| MEDIUM | 101 / 348 | 37% (23%) | 48% / 72% | 3% | 1.05 (449) | 50% / 74% | -36% |
| LOW | 146 / 700 | 46% (25%) | 43% / 70% | 18% | 1.20* (846) | 53% / 66% | -35% |
| VERY LOW | 72 / 258 | 44% (29%) | 46% / 78% | 7% | 1.05 (330) | 46% / 76% | -38% |
| INTERP | 6 / 31 | 43% (44%) | 33% / 42% | 0% | 1.15 (37) | 50% / 68% | -34% |

Pricing error = median |sale - median| / sale on the held-out sales (pooled; ours alone in brackets).

- **Different coefficients are justified by error, not by tier name:** error rises from 29% (VERY HIGH) to 46% (LOW), but VERY LOW (44%) is no worse than LOW, so the five tiers collapse to about three error bands (VERY HIGH/HIGH ~30%, MEDIUM 37%, LOW/VERY LOW/INTERP ~45%).
- **Too thin to set its own coefficient (<30 sales):** none pooled. On our auctions alone (the population the decision is about), HIGH (12), INTERP (6) and VERY HIGH (18) are under 30. Only MEDIUM, LOW and VERY LOW have 30+ of our own.
- **The suggested-k rule is weak:** paid/med is negative at every k (wins are skewed to cheap sales), so the share-above-median condition is the only binding one. For LOW that condition is loose because today's cap is already ~1.5-2x median (18% above), so k hits the top of the sweep (*). Read LOW as "k >= 1.20 passes", not a measured optimum.
- **1.00 and 1.05 give near-identical caps** under `clean_round` (VERY HIGH, HIGH, MEDIUM, VERY LOW), so the practical choice among 1.00 to 1.05 is one rounding step.
- **Win-rate effect on our auctions at the suggested k:** up in every tier (HIGH 8% to 25%, VERY HIGH 28% to 50%, MEDIUM 48% to 50%, LOW 43% to 53%, VERY LOW 46% to 46%). LOW at 1.00 would win 38%, below today.

## Strong tier

Strong uses only stored fields: notes `label=HIGH`, notes `window=±0.5`, and notes `cv` below 25%. The row stores no exact-grade count and no median.

| Rule | LOO n (share with 3+ exact-grade comps) | OURS n | Stored rows now |
|---|---|---|---|
| S1 label=HIGH | 88 (69%) | 30 | 57 |
| **S2 S1 + window ±0.5 + cv <25%** | **42 (83%)** | **18** | **31** |
| S3 S2 + comps 8+ | 39 (90%) | 18 | 22 |

S2 at 1.00 x median: LOO 67% to 76% win, OURS 28% to 50%, lost 0, new wins pay 9 to 13% under the median. 207 stored rows predate the `label=` token and can't qualify until re-priced.

## Recommended tier table (for decision)

| Tier | Factor | Why |
|---|---|---|
| Strong (S2) | 1.00 x median | Never loses a book today wins. New wins still clear below the median. |
| HIGH, MEDIUM-HIGH, MEDIUM (rest) | 1.00 x median | Neutral to today (+6, lost 4). 0.85 to 0.90 is a cut. |
| MEDIUM-LOW, LOW | keep 0.80 x high | 1.00 x median loses 13 to 24% of today's wins. Lower factors lose more. |
| Interpolated | 0.70 x median (= today) | Same caps as 0.60 x high. |
| Ceiling, CGC proxy, graded | unchanged | No raw-pool median. Out of scope. |

## Phase 2 blockers

- **Median storage:** `fmv` has no median column. The cached runner path (`fmv_runner`, the `"median": None` row rebuild) recomputes max_bid from fmv_high only. A cached book would bid on the old rule until it is re-priced. Phase 2 needs a `median` column, the server deployed first, and a high-anchor fallback for NULL-median rows.
- **policy.py recompute:** `_check_recomputed_cap` uses rung x `_effective_high`. A 1.00 x median cap trips it on 16 of 30 high and 54 of 101 medium auctions (OURS). It must read the stored median and the fine label. Use the notes `label=` token or a new column, because `fmv.confidence` collapses MEDIUM-HIGH and MEDIUM into `medium`. `over_fmv` (1.0 x high) never fires.
- **Rounding:** 0.95 and 1.00 are indistinguishable under `clean_round`.

## Caveats

- **Pool reconstruction:** pools come from the ledger, not the fetch that ran. Today's caps are reconstructed, not the stored ones.
- **Second price:** winning assumes we pay the observed price. The real winner's max is unknown and at least that price, so win rates are upper bounds at every factor equally.
- **Small top tiers:** OURS has 30 high and 18 Strong rows. Weight the LOO column.
