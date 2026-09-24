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
