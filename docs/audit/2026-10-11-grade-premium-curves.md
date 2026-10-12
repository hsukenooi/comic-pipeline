# BUI-1232: Pooled grade-premium curves for thin slab rungs

Date: 2026-10-11. Analysis only, no production code. Script: `2026-10-11-grade-premium-curves.py`. Test set: BUI-1229, built 2026-10-11 (47 books, 9,460 CGC universal sales).

**Re-run 2026-10-13 (BUI-1260).** Every number below is re-stated against the rebuilt test set (`meta.built` 2026-10-13, 47 books, 9,516 CGC universal sales, 32 `price_outlier` rows after BUI-1243's adjacent-grade rule, up from 18). The rebuild also picks up main since 2026-10-11, including the BUI-1234 and BUI-1247 best-offer comp scaling and the BUI-1242 published-band widening, so the "current" ladder numbers moved with code as well as data. **The NO-GO verdict holds. Two supporting claims flipped:** the ledger window no longer points toward the curve (paired diff −0.095 → +0.068, CI still spans zero), and the "mean is meaningless" caveat is dropped.

## Verdict: NO-GO as a refusal rescue. Promising as a ladder replacement, not proven on the live ledger

- **Pre-registered test fails on the production-shaped population.** On the ledger window, where both methods price, the pooled curve no longer beats the ladder tier: Winkler +0.068 [−0.126, +0.244] (was −0.095 [−0.321, +0.098]). The CI spans zero (criterion a).
- **Rescued refusals price worse than the ladder tier.** The curve prices 133 of 166 refused thin-rung sales at Winkler 1.188 [0.966, 1.477]. The ladder tier scores 0.844 on its own rows (criterion b). 62% of rescued sales are more than 15% off.
- **The earlier NO-GO stands for rescues.** Pooling by era and price tier over PC's history doesn't fix neighbour scaling. The miss rate (62% more than 15% off) matches the depth brainstorm's 59%.
- **Over PC's deep history, the curve does beat the ladder interpolation.** On 1,042 ladder-tier sales (2024-01..2026-10, PC anchors), Winkler falls from 1.391 to 1.035, diff −0.355 [−0.566, −0.154] (was −0.636 [−0.867, −0.415]). The gain is smaller because the current ladder tier improved (BUI-1242 band). The ledger window no longer points the same way (+0.035 [−0.222, +0.255], was −0.136), so the live-ledger evidence is neutral, not supportive.

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
- **Score.** BUI-1229's Winkler (alpha 0.5, scaled by sale price) and its book-clustered bootstrap (2,000 reps, seed 1229). A point price's Winkler is 4 × |error| / price, the same shape as a ladder price. Truth is PC's SOLD (accepted) price, with `price_outlier` rows dropped. Ledger anchors hold LIST price on best-offer sales (BUI-1222). The list-basis paired diff is +0.100 [−0.070, +0.260].

## Results, population 1 (ledger anchors, 906 sales, 493 thin rungs, 46 books)

| Cut | Priced | Winkler [95% CI] | Median Winkler | >15% off |
|---|---|---|---|---|
| Direct tier (reference, IQR band) | 413/413 | 0.570 [0.519, 0.619] | 0.342 | 45% |
| Current ladder/lone, thin rungs | 327/493 (66%) | 0.791 [0.642, 0.983] | 0.368 | 52% |
| Curve LOBO, thin rungs | 436/493 (88%) | 0.954 [0.813, 1.128] | 0.694 | 58% |
| Curve OWN, thin rungs | 436/493 (88%) | 0.905 [0.769, 1.072] | 0.660 | 56% |
| Curve LOBO, rescued refusals | 133/166 | 1.188 [0.966, 1.477] | 0.823 | 62% |

| Paired, same sales | n (books) | Curve | Current | Diff [95% CI] |
|---|---|---|---|---|
| **LOBO point, all thin** | 303 (38) | 0.851 | 0.783 | **+0.068 [−0.126, +0.244]** |
| OWN point | 303 (38) | 0.805 | 0.783 | +0.023 [−0.163, +0.185] |
| LOBO IQR band | 303 (38) | 0.762 | 0.783 | −0.021 [−0.220, +0.153] |
| LOBO, ladder-tier rows only | 231 (37) | 0.872 | 0.837 | +0.035 [−0.222, +0.255] |
| LOBO, Silver | 179 (20) | 0.807 | 0.754 | +0.053 [−0.136, +0.209] |
| LOBO, Bronze | 102 (10) | 0.784 | 0.789 | −0.005 [−0.749, +0.371] |
| LOBO, Modern | 20 (7) | 1.589 | 1.043 | +0.546 [−0.460, +1.583] |
| LOBO, eBay source | 263 (37) | 0.853 | 0.742 | +0.112 [−0.077, +0.279] |
| LOBO, Heritage source | 40 (19) | 0.834 | 1.051 | −0.217 [−0.705, +0.285] |

Coverage on thin rungs rises from 66% to 88%. Error grows with anchor distance: 52% of sales miss by more than 15% at 0.5 grade or less, 64% at 1.0, and 64% beyond. Era cells are thin. Copper has 3 thin sales from 1 book, and Modern's 20 paired sales come from 7 books. Heritage is the only cut where the curve still leads (CI spans zero). On eBay alone the curve is now worse than the ladder (+0.112, CI spans zero).

## Results, population 2 (PC-history anchors, 7,520 sales, 1,846 thin rungs, 47 books)

| Paired, same sales | n (books) | Curve | Current | Diff [95% CI] |
|---|---|---|---|---|
| **LOBO point, all thin** | 1,170 (46) | 1.027 | 1.302 | **−0.274 [−0.461, −0.093]** |
| LOBO, ladder-tier rows only | 1,042 (46) | 1.035 | 1.391 | −0.355 [−0.566, −0.154] |
| LOBO, eBay source | 999 (46) | 1.000 | 1.167 | −0.167 [−0.314, −0.026] |
| LOBO, Silver / Bronze / Modern | 754 / 357 / 47 | 1.02 / 1.01 / 1.23 | 1.24 / 1.44 / 1.29 | Bronze CI below 0 (−0.436 [−0.737, −0.125]). Silver [−0.488, +0.006] and Modern [−0.278, +0.164] span 0 |

Rescued refusals: 613 of 669 priced, median Winkler 1.000 [0.902, 1.107] against 0.556 for the ladder tier's own rows. 69% miss by more than 15%. The mean is now usable (1.247 [1.152, 1.350]), so the earlier "mean is meaningless" caveat is dropped. It rested on two unflagged truth rows: a Silver Surfer #4 CGC 9.8 "sold" for $5 (it alone scored 19,667) and an Invincible #1 CGC 9.8 at $415. Both are now `price_outlier` = True (BUI-1243's adjacent-grade fallback). Other $415 9.8 rows (Invincible #84 and #120) are different issues and stay unflagged.

## Why the curve still fails on rescues

- **Rescues sit far from dense rungs.** `outside_ladder` and `ladder_too_thin` sales have no bracket, so the anchor sits further out. On the ledger, 53% of rescued sales anchor 1.0+ grade away, against 42% of ladder-tier sales (PC history: 38% vs 26%; not re-derived on 2026-10-13, the script no longer prints these shares). That is where the curve misses most.
- **Pooled curves are still non-monotone before smoothing.** Only 167 of 605 fits (28%) came out monotone before the isotonic pass. Pooling narrows the noise but doesn't remove it. Per-book grade premiums differ more than era and tier explain.
- **The ladder tier's weakness is its interpolation, not its coverage.** Where a bracket exists, a pooled curve read off one dense rung tracks the sale better than linear interpolation between two thin rungs. That is a different change from the one this ticket tested.

## Out-of-scope findings

- **Candidate follow-up: replace the ladder interpolation with the pooled curve, without widening coverage.** The deep test still supports it (−0.355 [−0.566, −0.154] on ladder-tier rows, smaller since BUI-1242 widened the live band). The ledger window is now neutral (+0.035, CI spans zero). Re-measure on the ledger after another quarter of capture before building anything.
- **`price_outlier` sparse-grade gap is closed (BUI-1243).** The Silver Surfer #4 9.8 $5 row and the Invincible #1 9.8 $415 row are now flagged, and mean Winkler is usable on PC history.
- **Copper is unscoreable** in this set (2 books). Modern thin rungs are 7 to 11 books with CIs over a full Winkler point wide.
