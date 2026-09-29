---
title: "Grade-adjusted pool backtest (BUI-1005)"
date: 2026-09-28
status: corrected
superseded_by: "BUI-1019: the fresh-batch 'no bid yet' claim used a NULL column; via the FMV link 2 of the 24 books had a post-batch bid (see Correction 2026-09-30). The cancel decision is unchanged."
superseded_date: 2026-09-30
---

# Grade-adjusted pool backtest (BUI-1005)

**Date:** 2026-09-28. **Source:** `uv run --project plugins/gixen-overlay python docs/audit/2026-09-28-grade-adjusted-pool-backtest.py`, against the live comics DB opened read-only. No provider calls and no `comic-fmv` runs. The comps ledger is the only input.

**Decision: cancel.** On the sales the current guards refuse, the grade-adjusted pool scores a median Winkler/price of 1.184, worse than the current method's 1.100 on the sales it accepts, and it is worse or tied at every price level and in two of three eras. The slope is real but noisy (per-book median +11.5%, IQR −3% to +25%), and the refused pools are thin and mixed-grade, so shifting them prices a real share of books but not to the standard the accepted books meet.

## Method

- **Ledger:** `comps` with `pool='raw'`, a parsed grade, no `excluded_code`, and a price above 0. Rows from the `no-variant` and `broader` query tiers are dropped, because they can be the base cover of a variant book. Cross-provider copies are removed by `(comic_id, product_id)`, then by `(comic_id, grade, price, sold date)`. 5,276 rows load and 4,894 remain.
- **Held-out sales:** every remaining sale with at least one same-book comp within ±2.0 grades sold in the 90 days before it. **N = 4,114.** 780 sales have none and are excluded. Neither method can price them.
- **Training:** the same `comic_id`'s comps sold in `[d − 90, d)`. Strictly before d, so no later or same-day sale enters. A comp with the held-out sale's grade and price, sold within 7 days before it, is treated as a copy of that sale with a drifted date and dropped (61 dropped).
- **Current:** `fmv_math.compute_fmv` on the training comps: `build_pool` widening, `_classify_pool`, the BUI-306 bracket rescue, the BUI-528 collapse split, and the BUI-990 width floor.
- **Proposed:** each training comp within ±2.0 of the target g is shifted to g at price × (1 + s)^(g − grade). At least 3 comps are required, and the shifted pool goes through the same `compute_fmv` quartile machinery. s = 15% (the ticket's slope) and s = 11.5% (measured). An unshifted ±2.0 pool is the control that isolates what the slope adds.
- **Scoring:** BUI-979's `winkler_score` with alpha 0.5, divided by the sale price. The error ratio is max(est/price, price/est) on the band median. Width = (high − low) / midpoint.

## Slope

OLS of ln(price) on grade per book. Books need at least 6 comps, 3 distinct grades, and a span of at least 2.0 (228 books).

| Group | Books | Median per grade point | Q1 | Q3 |
|---|---|---|---|---|
| All | 228 | +11.5% | −3.2% | +25.3% |
| Pre-1980 | 87 | +15.3% | −8.7% | +31.3% |
| 1980–1999 | 70 | +15.2% | +4.3% | +26.1% |
| 2000+ | 26 | +14.1% | +5.7% | +24.1% |

The ticket's "+15%, steady across eras" holds for the median only. 68 of 228 books have a flat or negative slope. The within-book pooled slope over the same books is −1.5%, because several of the books with the most comps are flat. The slope is a weak signal on this ledger, not a constant.

## Results

Current outcome over the 4,114 sales: 2,273 priced, 44 priced by the bracket rescue, 597 `one_sided`, 553 `too_wide`, and 647 `too_sparse`. The guards refuse 28% on shape (1,150 sales), plus 16% as sparse.

| Cut | Method | Priced | Median W/price | Error ratio | Median est/price | In band | Width |
|---|---|---|---|---|---|---|---|
| Refused (1,150) | Adjusted +15% | 845 (73%) | 1.184 | ×1.47 | ×1.00 | 48.0% | 67% |
| | Adjusted +11.5% | 845 | 1.179 | ×1.47 | ×1.00 | 48.3% | 67% |
| | Unshifted ±2.0 | 845 | 1.222 | ×1.54 | ×1.00 | 47.7% | 67% |
| Accepted (2,317) | **Current** | 2,317 | **1.100** | ×1.43 | ×1.00 | 48.6% | 67% |
| | Adjusted +15% | 2,113 (91%) | 1.020 | ×1.42 | ×1.00 | 49.3% | 67% |
| All (4,114) | Current | 2,317 (56%) | 1.100 | ×1.43 | ×1.00 | 48.6% | 67% |
| | Hybrid (adjusted on refusals only) | 3,162 (77%) | 1.120 | ×1.43 | ×1.00 | 48.4% | 67% |
| `too_sparse` (647) | Any | 0 | – | – | – | – | – |

- **The decision gap:** refused-adjusted minus accepted-current is +0.084. A 95% book-clustered bootstrap gives −0.049 to +0.183, so the gap isn't significant, but no stratum shows the adjusted pool ahead.
- **By price level** (bucketed on the training pool's median, not on the sale price, which would condition on the outcome): under $20, 1.669 against 1.319 for accepted; $20–$100, 1.000 against 1.000; $100 and up, 0.761 against 0.667.
- **By era:** pre-1980, 1.063 against 1.019; 1980–1999, 1.667 against 1.151; 2000+, 0.740 against 0.878. Only the 132-sale 2000+ cut passes.
- **No systematic overpricing:** the median estimate/price is ×1.00 on the refused cut, with 26.7% of sales above the band and 25.2% below.
- **The slope earns something:** +15% beats the unshifted ±2.0 pool on the refused cut (1.184 against 1.222) and most clearly at $100 and up (0.761 against 1.001). +15% and +11.5% are indistinguishable.

The ticket's inputs differ from these: it had 2,647 held-out sales, 52% refused, 31% of refusals priced, and in-band shares of 32% and 42%. This run removes cross-provider duplicates and scores through the full `compute_fmv` path, including the width floor. The 40% floor on both methods likely explains most of the higher in-band shares here.

## Fresh batch (2026-09-26)

The batch left 24 books unpriced (21 flagged, 3 with no comps). Re-priced from ledger comps known on 2026-09-26 and sold in the 90 days before it, the adjusted pool prices 6. No book has a later ledger sale (two books did get a bid; see the correction below), and the batch stored no active asks, so the bands are compared against the ungraded anchor (grade-less sales, typically lower grade).

| Book | Grade | Batch flag | Adjusted +15% | Ungraded anchor |
|---|---|---|---|---|
| Amazing Spider-Man #63 (1968) | 4.0 | too_wide | $35–60 | $43.75 (n=29) |
| Amazing Spider-Man #74 (1969) | 4.0 | too_wide | $20–45 | $34.95 (n=15) |
| Amazing Spider-Man #94 (1971) | 6.0 | too_wide | $30–50 | $24.50 (n=9) |
| Captain America Annual #8 (1986) | 5.5 | too_wide | $10–20 | $19.99 (n=16) |
| Thor #129 (1966) | 5.0 | too_wide | $30–60 | $24 (n=9) |
| Thor #157 (1968) | 5.5 | too_wide | $10–35 | $10.06 (n=8) |

The other 18 have fewer than 3 ledger comps within ±2.0. The ticket's 16 of 24 needed comps from the query recall fix, which aren't in the ledger yet.

**Thor #159 and #175 (the warning signs):** the pool, not the slope. At grade 8.0, the ledger holds 2 comps within ±2.0 for each: #159 sold at $29 (8.5) and $22.50 (9.0), and #175 at $15 (6.0) and $20 (7.0). Shifted at +15%, they land at $17–25 and $20–23. At a 0% slope they are still $15–29. A third comp (first-party or recall) makes them priceable at that level. The $7.99 asks match the ledger's low-grade and ungraded sales of the same books ($7.95 at 2.0 and $7.99 ungraded for #159, $7 at 4.0 for #175). The asks look like lower-grade copies measured against a VF target, not evidence the band overprices. The asks' grades aren't stored, so this can't be confirmed.

## Review notes

- **Leakage:** training is strictly before each sale. Duplicate copies of the sale are removed. The +15% slope is fixed ahead of time. The measured slope uses the whole ledger, but its results match +15%.
- **Identity:** `comic_id` separates variants (Newsstand has its own id). Later printings can still mix into a book, which affects both methods equally.
- **Survivorship:** 780 sales with no nearby comps and 647 `too_sparse` sales are priced by neither method. The adjusted pool leaves 27% of refusals unpriced. Scores cover priced sales only.
- **Population:** these are ledger sales priced from ledger-only pools with no first-party comps, so the absolute Winkler values aren't comparable to BUI-982's auction replay (0.73).

## Out of scope

- On the 2,113 accepted sales both methods price, the +15% adjusted pool beats the current pool: 1.020 against 1.132, and 588 against 445 in a paired comparison, with 1,080 ties. That's a replacement for the accepted path, not a rescue of refusals. It needs its own ticket and a live re-run before anyone acts on it.

## Correction 2026-09-30 (BUI-1019)

The claim that no fresh-batch book had a bid came from `FROM bids WHERE comic_id = ?`. `bids.comic_id` is NULL on all 812 rows, so that query could never match. Recomputed through the FMV link, reading both paths directly (`bid_fmvs` bid to fmv, and `bids.fmv_id`, each joined to `fmv.comic_id`). Neither path has dangling links, and both return the same 21 bids on 12 of the 24 books.

- **Bids since the batch (`added_at >= 2026-09-26`):** 2 of 24 books. Avengers #87 (bid 799, PENDING, max $28, grade 6.0) and Thor #175 (bid 802, ENDED, no winning bid, max $14, grade 7.0). Both were added 2026-09-28, after this run.
- **Bids before the batch:** 10 of the 24 books had a non-tombstone bid at some point (mostly LOST). Books 306 and 575 have only REMOVED tombstones, which are reported here but not counted as "had a bid".

**What it changes:** the fresh-batch sentence only. Thor #175 is one of the two warning-sign books above, so it now has a real outcome to compare against (bid 802 ended with no win at a $14 max). The cancel decision rests on the 4,114-sale Winkler backtest, not on the fresh batch, so it stands.

Reproduce: the script's `fresh_batch` query now reads both link paths through `fmv.comic_id`.
