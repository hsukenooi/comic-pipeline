# BUI-1232: Pooled grade-premium curves for thin slab rungs

Date: 2026-10-11. Analysis only, no production code. Script: `2026-10-11-grade-premium-curves.py`. Test set: BUI-1229, built 2026-10-11 (47 books, 9,460 CGC universal sales).

## Verdict: NO-GO as a refusal rescue. Promising as a ladder replacement, not proven on the live ledger

- **Pre-registered test fails on the production-shaped population.** On the ledger window, where both methods price, the pooled curve beats the ladder tier by Winkler −0.095 [−0.321, +0.098]. The CI spans zero (criterion a).
- **Rescued refusals price worse than the ladder tier.** The curve prices 134 of 169 refused thin-rung sales at Winkler 1.177 [0.961, 1.462]. The ladder tier scores 1.011 on its own rows (criterion b). 60% of rescued sales are more than 15% off.
- **The earlier NO-GO stands for rescues.** Pooling by era and price tier over PC's history doesn't fix neighbour scaling. The miss rate (60% more than 15% off) matches the depth brainstorm's 59%.
- **Over PC's deep history, the curve does beat the ladder interpolation.** On 1,037 ladder-tier sales (2024-01..2026-10, PC anchors), Winkler falls from 1.676 to 1.041, diff −0.636 [−0.867, −0.415]. The ledger window points the same way (−0.136 [−0.424, +0.100]) but is too thin to confirm it.

## What the earlier NO-GO measured

The 2026-09-21 depth brainstorm (`docs/brainstorms/2026-09-21-slab-and-raw-fmv-depth-requirements.md`, line 37) fit **per-book** ladders on the live comps ledger. The 2026-09-21 corpus held 344 slab comps in 56 pools and 204 rungs, with 65% of rungs under two sales. No pool fit at R² 0.9, and 11 of 12 ladders were non-monotone. Neighbour-rung scaling missed the observed **rung median** by more than 15% in 59% of cases. BUI-974 (`docs/conventions/fmv-math-spec.md` §7b) then backtested one-step extrapolation on held-out end rungs. A flat carry missed by a median 18% downward (9 of 16 more than 15% off) and 44 to 49% upward. No ticket recorded the brainstorm's own script.

This retest differs in two ways. The curve is **pooled across books** by era and price tier from PC history (PC holds about 10x the ledger's sales). The truth is **each sale**, not a rung median. A sale-level miss rate is harsher: the direct tier itself misses by more than 15% on 44% of sales.

## Method

- **Curve.** Log price = book-quarter level + f(grade), fitted by alternating least squares on (book, quarter, grade) median log prices. Then a weighted isotonic pass keeps f non-decreasing. Grades seen in fewer than 2 books are dropped. f is linear between kept grades and never extrapolated.
- **Cells.** Era × price tier. Tier is the book's median PC sale over the prior 4 quarters: under $200, $200 to $700, or $700 and up. A cell needs 3+ training books, else it falls back to era, then all books. 70% of LOBO prices used an era × tier cell, 29% era only.
- **Leakage.** A sale in quarter Q uses a curve fitted only on the 12 complete quarters before Q. **LOBO** (primary) drops the scored book from its own curve. **OWN** keeps the book's prior quarters.
- **Thin rung.** Any sale the current `graded_fmv` doesn't price `direct`: ladder, lone sale, or a refusal.
- **Pricing.** Take the nearest dense rung (effective n of 2 or more under `graded_pool` calendar aging, the exact tier's own gate). Point = rung weighted median × exp(f(g) − f(d)). A secondary band variant scales the rung's weighted IQR.
- **Populations.** (1) **Ledger**: PC sales 2026-08-01..10-10, anchors and current price from ledger comps, built as in the BUI-1229 baseline. (2) **PC history**: PC sales 2024-01-01..2026-10-10, anchors and current price from the book's earlier PC sales only.
- **Score.** BUI-1229's Winkler (alpha 0.5, scaled by sale price) and its book-clustered bootstrap (2,000 reps, seed 1229). A point price's Winkler is 4 × |error| / price, the same shape as a ladder price. Truth is PC's SOLD (accepted) price, with `price_outlier` rows dropped. Ledger anchors hold LIST price on best-offer sales (BUI-1222). The list-basis paired diff is −0.051 [−0.252, +0.122].

## Results, population 1 (ledger anchors, 904 sales, 491 thin rungs, 46 books)

| Cut | Priced | Winkler [95% CI] | Median Winkler | >15% off |
|---|---|---|---|---|
| Direct tier (reference, IQR band) | 413/413 | 0.618 [0.556, 0.686] | 0.400 | 44% |
| Current ladder/lone, thin rungs | 322/491 (66%) | 0.954 [0.786, 1.166] | 0.637 | 52% |
| Curve LOBO, thin rungs | 434/491 (88%) | 0.955 [0.812, 1.131] | 0.690 | 58% |
| Curve OWN, thin rungs | 434/491 (88%) | 0.904 [0.766, 1.073] | 0.660 | 56% |
| Curve LOBO, rescued refusals | 134/169 | 1.177 [0.961, 1.462] | 0.812 | 60% |

| Paired, same sales | n (books) | Curve | Current | Diff [95% CI] |
|---|---|---|---|---|
| **LOBO point, all thin** | 300 (38) | 0.856 | 0.951 | **−0.095 [−0.321, +0.098]** |
| OWN point | 300 (38) | 0.808 | 0.951 | −0.143 [−0.359, +0.035] |
| LOBO IQR band | 300 (38) | 0.766 | 0.951 | −0.184 [−0.414, +0.008] |
| LOBO, ladder-tier rows only | 229 (37) | 0.875 | 1.011 | −0.136 [−0.424, +0.100] |
| LOBO, Silver | 179 (20) | 0.807 | 0.952 | −0.146 [−0.357, +0.029] |
| LOBO, Bronze | 99 (10) | 0.797 | 0.895 | −0.098 [−0.943, +0.303] |
| LOBO, Modern | 20 (7) | 1.589 | 1.249 | +0.340 [−0.786, +1.215] |
| LOBO, eBay source | 260 (37) | 0.859 | 0.885 | −0.026 [−0.232, +0.150] |
| LOBO, Heritage source | 40 (19) | 0.833 | 1.377 | −0.544 [−1.113, +0.052] |

Coverage on thin rungs rises from 66% to 88%. Error grows with anchor distance: 52% of sales miss by more than 15% at 0.5 grade or less, 63% at 1.0, and 65% beyond. Era cells are thin. Copper has 3 thin sales from 1 book, and Modern's 20 paired sales come from 7 books. Most of the paired gain comes from Heritage sales. On eBay alone the curve and ladder tie.

## Results, population 2 (PC-history anchors, 7,490 sales, 1,848 thin rungs, 47 books)

| Paired, same sales | n (books) | Curve | Current | Diff [95% CI] |
|---|---|---|---|---|
| **LOBO point, all thin** | 1,165 (46) | 1.029 | 1.571 | **−0.541 [−0.752, −0.346]** |
| LOBO, ladder-tier rows only | 1,037 (46) | 1.041 | 1.676 | −0.636 [−0.867, −0.415] |
| LOBO, eBay source | 994 (46) | 0.999 | 1.407 | −0.408 [−0.566, −0.255] |
| LOBO, Silver / Bronze / Modern | 757 / 348 / 48 | 1.03 / 1.00 / 1.23 | 1.50 / 1.72 / 1.60 | all CIs below 0 |

Rescued refusals: 612 of 672 priced, median Winkler 0.990 [0.900, 1.106] against 0.864 for the ladder tier's own rows. 69% miss by more than 15%. The mean (33.5) is meaningless here. One unflagged truth row, a Silver Surfer #4 CGC 9.8 "sold" for $5, scores 19,667 alone. `price_outlier` can't flag it with fewer than 3 same-grade neighbours.

## Why the curve still fails on rescues

- **Rescues sit far from dense rungs.** `outside_ladder` and `ladder_too_thin` sales have no bracket, so the anchor sits further out. On the ledger, 53% of rescued sales anchor 1.0+ grade away, against 42% of ladder-tier sales (PC history: 38% vs 26%). That is where the curve misses most.
- **Pooled curves are still non-monotone before smoothing.** Only 191 of 606 fits (32%) came out monotone before the isotonic pass. Pooling narrows the noise but doesn't remove it. Per-book grade premiums differ more than era and tier explain.
- **The ladder tier's weakness is its interpolation, not its coverage.** Where a bracket exists, a pooled curve read off one dense rung tracks the sale better than linear interpolation between two thin rungs. That is a different change from the one this ticket tested.

## Out-of-scope findings

- **Candidate follow-up: replace the ladder interpolation with the pooled curve, without widening coverage.** The deep test supports it (−0.636 [−0.867, −0.415] on ladder-tier rows). The ledger window is directional only. Re-measure on the ledger after another quarter of capture before building anything.
- **`price_outlier` misses sparse-grade junk.** The Silver Surfer #4 9.8 $5 row and an Invincible #1 9.8 $415 row both pass. A floor based on a cross-grade reference would catch them. Any PC-history scoring with mean Winkler needs it.
- **Copper is unscoreable** in this set (2 books). Modern thin rungs are 7 to 11 books with CIs over a full Winkler point wide.
