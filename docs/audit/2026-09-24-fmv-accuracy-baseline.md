# FMV accuracy baseline (BUI-977)

**Date:** 2026-09-24. **Source:** `comics-api GET /api/comics/accuracy` on the comics server at 969a325, all resolved primary auctions, no day window.

This is the starting point for the FMV Accuracy Baseline and Calibration project. Re-run `/comic:accuracy-report` and compare against these numbers. Band width is reported alongside the hit rate so that a wider band can't pass as a better estimate.

## Overall

| Scope | n | In band | Above | Below | Within ±10% | Within ±20% | MdAPE | Mean signed error | Median band width |
|---|---|---|---|---|---|---|---|---|---|
| All | 586 | 43.5% | 35.3% | 21.2% | 27.8% | 46.6% | 23.1% | +15.7% | 40% |
| WON | 187 | 49.2% | 6.4% | 44.4% | 23.0% | 38.0% | 26.7% | −19.5% | 67% |
| LOST | 399 | 40.9% | 48.9% | 10.3% | 30.1% | 50.6% | 20.0% | +32.1% | 40% |

"Within ±N%" and MdAPE measure the final price against the band midpoint. A positive signed error means the price cleared above the midpoint.

Band source: 445 auctions score against the `fmv_history` snapshot at or before the bid was added, and 141 fall back to the current `fmv` row. The fallback rows can include the auction's own price in the comp pool, so the history-only subset is the stricter baseline. That subset (n=445) is 40.0% in band, 37.5% above, 22.5% below, and MdAPE 24.2%.

## By month

| Month | n | In band | Above | Below | Within ±10% | Within ±20% | MdAPE |
|---|---|---|---|---|---|---|---|
| 2026-05 | 139 | 50.4% | 33.1% | 16.5% | 26.6% | 46.8% | 24.0% |
| 2026-06 | 141 | 26.2% | 45.4% | 28.4% | 22.7% | 36.2% | 32.0% |
| 2026-07 | 170 | 51.8% | 30.6% | 17.6% | 32.9% | 53.5% | 16.9% |
| 2026-08 | 117 | 45.3% | 32.5% | 22.2% | 29.1% | 52.1% | 18.4% |
| 2026-09 | 19 | 36.8% | 36.8% | 26.3% | 21.1% | 26.3% | 30.0% |

## Reading it

- The main miss is above the band: 35% of all auctions and 49% of lost ones cleared above it. BUI-978 diagnoses those.
- Won auctions sit below the midpoint on average (−19.5%), which is expected: we win when the market clears low.
- Won auctions carry wider bands (67% vs 40%), so their in-band rate reads higher partly because of width.

## 2026-09-27 update (BUI-981): pre-end snapshot for 99 late-first-snapshot auctions

BUI-979 found 99 resolved auctions whose first `fmv_history` snapshot was recorded after the bid was added but before the auction ended. `_fmv_accuracy_rows`'s at-added lookup found nothing at or before `added_at` for these and fell back to the current `fmv` row, which can already carry the auction's own price (see the leakage note above). Fixed: when no at-added snapshot exists, use the earliest snapshot recorded before the auction ended instead — still leakage-free for that auction, since it hadn't resolved when the snapshot was recorded. Each row now also reports `in_force_source` (`at_added` | `pre_end` | `current`), tallied in `in_force_source_counts`; `band_source` keeps its original `history`/`current_fmv` shape (`at_added` and `pre_end` both count as `history`).

Re-run against a 2026-09-27 backup of the live DB, the same snapshot scored with the old and new code (`days=None`, n=586 both times):

| Metric | Before | After |
|---|---|---|
| In band | 43.5% | 43.5% |
| Above | 35.3% | 35.3% |
| Below | 21.2% | 21.2% |
| Within ±10% | 27.8% | 27.6% |
| Within ±20% | 46.8% | 46.6% |
| MdAPE | 23.0% | 23.1% |
| Mean signed error | +15.6% | +15.6% |
| Median band width | 40% | 40% |

(These figures are 0.1-0.2pp off the 2026-09-24 table above because the live DB moved in the three days between runs — normal auction resolution, not this fix. The before/after pair here is the load-bearing comparison; both columns are drawn from the same backup.)

`in_force_source_counts` after the fix: 445 `at_added`, 99 `pre_end`, 42 `current` — under the old two-tier fallback this was 445 `history` / 141 `current_fmv`; the 99 `pre_end` rows are exactly the ones that moved off `current_fmv`.

The stricter no-leakage subset (`band_source == 'history'`, called out above as the tighter baseline) grows from n=445 (40.0% in band, 37.5% above, 22.5% below, MdAPE 24.2%) to n=544 (43.4% in band, 35.1% above, 21.5% below, MdAPE 22.8%).

**Why the headline barely moves:** 97 of the 99 `pre_end` rows have a band numerically identical to the current `fmv` row they'd otherwise have fallen back to — these are books priced once and never repriced since, so the fallback's target value didn't actually change, only its provenance (a pinned snapshot instead of a mutable live row that could drift later). Only 2 rows' `fmv` row had since drifted from the pre-end snapshot, and those are the only two whose in/above/below bucket changed (one above→in, one in→above), which is why the aggregate coverage split is bit-for-bit unchanged. This fix closes a formal leakage risk more than it moves the current numbers — most of the 99 rows were already scoring against the right values, just without anything guaranteeing that.
