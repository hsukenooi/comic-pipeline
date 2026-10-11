# BUI-1234: Price best-offer slab comps at the accepted basis

Date: 2026-10-11. Script: `2026-10-11-best-offer-comp-basis.py`. Test set: BUI-1229 artifact, `meta.built` 2026-10-11. Ledger read through `comics-api GET /api/comics/comps/all?pool=slab`.

## Verdict

- **Ship option (b), the ratio, for the slab pool.** A best-offer comp is scaled by 0.89 inside `fmv_math.graded_pool`. Winkler falls from 0.765 to 0.716 (paired -0.047 [-0.074, -0.030] on 734 sales). Bias falls from +0.081 to +0.048. Coverage rises from 81% to 82%.
- **Option (a), dropping flagged comps, loses.** Winkler rises to 0.802 (paired +0.081 [+0.029, +0.136]) and coverage falls to 76%. On slabs, 27% of pool rows are best-offer, so dropping them thins rungs that are already one sale deep.
- **Option (c), PriceCharting's accepted price, helps less and can't run live.** Winkler is 0.725 (paired -0.037). Only 70% of flagged comps join a PC sale, and PC is a firecrawl scrape, not a pricing-path source.
- **Most of the gain is a downward shift.** A uniform 0.969 scale on every comp (the same average size) scores 0.725, paired -0.040. The targeted ratio beats it by -0.010 [-0.028, +0.006], so the best-offer targeting itself isn't significant on 47 books. The ratio still ships: it corrects a proven measurement error (BUI-1222) with an independently measured size, where a blanket shift would be a fudge.
- **The raw pool is not scaled.** It has no accepted-price truth set to backtest against. About 21% of raw comps carry the badge.

## Results (sold basis, 904 PC CGC universal sales, 2026-08-01..2026-10-10, outliers dropped)

| Basis | Priced | Winkler [95% CI] | In band | Bias | Paired vs baseline |
|---|---|---|---|---|---|
| Baseline (stored list price) | 735 | 0.765 [0.670, 0.901] | 15% | +0.081 | |
| (a) Drop flagged | 690 | 0.802 [0.702, 0.925] | 12% | +0.083 | +0.081 [+0.029, +0.136] |
| (b) Ratio 0.889 | 742 | 0.713 [0.629, 0.832] | 15% | +0.047 | -0.050 [-0.077, -0.032] |
| (c) PC accepted price | 743 | 0.725 [0.635, 0.851] | 14% | +0.057 | -0.037 [-0.056, -0.023] |
| (a) capture flag only | 688 | 0.803 [0.704, 0.929] | 12% | +0.083 | +0.082 [+0.031, +0.137] |
| (b) capture flag only | 741 | 0.716 [0.630, 0.836] | 16% | +0.048 | -0.047 [-0.074, -0.029] |
| **Shipped code path** | 741 | **0.716 [0.630, 0.836]** | 16% | +0.048 | **-0.047 [-0.074, -0.030]** |
| Control: ratio on unflagged | 722 | 0.730 [0.644, 0.840] | 14% | -0.004 | -0.033 [-0.093, +0.018] |
| Control: uniform 0.969 | 735 | 0.725 [0.638, 0.846] | 14% | +0.048 | -0.040 [-0.063, -0.022] |

By era (Winkler, baseline to shipped): Silver 0.819 to 0.743, Bronze 0.724 to 0.698, Copper 0.660 to 0.642 (2 books), Modern 0.724 to 0.692. eBay-source sales: 0.723 to 0.688. The shipped path leaves `fmv_high` unchanged at the median and lowers it on 288 of 734 priced sales.

## Method

- **Rows and scoring.** These are the BUI-1229 baseline rows and scorer unchanged: each sale is priced as of the day before from ledger comps sold strictly before it, with every `same_sale` twin hidden. CIs are 2,000-rep book-clustered bootstraps, seed 1229. Paired deltas use sales both bases price.
- **The flag.** The ledger has no best-offer column before this change, so the script joins `bestOfferAccepted` back by item id from the BUI-614 raw capture (21,667 items). Where the capture lacks an item, PC's `best_offer` is used if the item joins PC. That covers 829 of 917 test-set ledger rows. "Capture flag only" rows use just the capture, the flag production can persist. The shipped row runs the real `graded_pool` with that flag on each comp, and matches the capture-only ratio to within 0.001.
- **The ratio.** It's the median accepted/list over 907 PC eBay best-offer sales dated before the window: 0.889, so it never sees a scored sale. The in-window median is 0.897 (n=221). Production uses 0.89.

## What shipped

- `parse_comp_sold_comps` emits `best_offer` (True, False, or None) and keeps the provider's price. Its docstring no longer calls `soldPrice` the accepted amount.
- `comps.best_offer` (nullable INTEGER) is added by an idempotent migration. `upsert_comps` fills a NULL on a later sighting and never overwrites a stored flag. Stored prices are never rewritten.
- `fmv_runner` carries the flag into the ledger POST and the slab pool. `fmv_math.graded_pool` scales flagged copies once. It keeps `best_offer_list_price` and skips any copy that already has it, so a re-read can't double-adjust.
- **Deploy order is free.** An old server ignores the new key, and an old client ignores the new column. Until the server is deployed, ledger re-reads stay at list price and only live comps scale.

## Owed after deploy

- **A live re-run.** Stored `fmv` rows keep their list-basis band until `comic-fmv` recomputes them.
- **Ledger flags.** About 40% of slab ledger rows (636 of 1,578) have no capture record. A live fetch fills rows still inside eBay's 90-day window. Older rows need `apps/fmv/scripts/backfill_comps_ledger.py` re-run over the capture, which re-posts each comp. That fills the flag through the same COALESCE and bumps `seen_count`.

## Out-of-scope findings

- **Slab comps are best-offer far more often than raw.** The share is 43% of flagged-known slab ledger rows against 21% raw, so the list-price bias weighs on slabs about twice as much.
- **PC and the capture disagree on 22 joined items.** PC shows one price on 21 sales the capture badges as best offer, and 2 prices on 1 it doesn't. Most likely PC shows a single price when accepted equals list.
