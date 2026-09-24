# Above-band misses (BUI-978)

**Date:** 2026-09-24. **Source:** `GET /api/comics/accuracy?include_rows=true` (586 resolved auctions, the BUI-977 baseline), joined read-only to the live comics DB for book year, grade source, pricing basis, `max_bid`, and the FMV notes each band was built from. Reproduce with `docs/audit/2026-09-24-above-band-misses.py <rows.json>`.

**Answer:** no kind of book is systematically underpriced by a margin a multiplier would fix. The misses come from band shape, not band level. A quarter of them sit on zero-width bands (`low == high`), a class that BUI-528 already fixed on 2026-07-24. Most of the rest sit on narrow bands (under 30% of the midpoint), which miss on both sides. One fix is worth testing: a minimum band width.

## Method

- **Primary cut:** `band_source='history'` (n=445), scored against the `fmv_history` snapshot taken before the bid was added. The all-586 cut ranks every slice the same way, and the script prints it in full.
- **Recent cut:** history rows whose snapshot was recorded on or after 2026-07-25, after BUI-528 shipped (n=132).
- **Oracle in-band:** the in-band rate if every above-band miss in that slice had landed in the band.
- **Band-shift test:** the best single multiplier on a slice's band, fitted in-sample. It reports the net gain, because a shift also pushes in-band rows below the band.

## Results

| Slice | History n | In band | Above | Share of misses | Recent n | In band | Above |
|---|---|---|---|---|---|---|---|
| **All** | **445** | **40%** | **38%** | **100%** | **132** | **44%** | **33%** |
| Comps 0–2 | 56 | 21% | 52% | 17% | 15 | 40% | 47% |
| Comps 3–5 | 215 | 38% | 38% | 49% | 73 | 42% | 29% |
| Comps 6+ | 163 | 47% | 35% | 34% | 44 | 48% | 34% |
| Confidence low | 276 | 38% | 40% | 66% | 95 | 41% | 33% |
| Confidence medium/high | 169 | 43% | 33% | 34% | 37 | 51% | 32% |
| Era before 1970 | 63 | 38% | 35% | 13% | 17 | 47% | 35% |
| Era 1970–84 | 134 | 49% | 34% | 28% | 15 | 47% | 27% |
| Era 1985–99 | 135 | 36% | 42% | 34% | 27 | 56% | 22% |
| Era 2000+ | 92 | 33% | 36% | 20% | 70 | 39% | 36% |
| Grade: seller-stated | 193 | 44% | 35% | 40% | 31 | 65% | 23% |
| Grade: photo grade | 33 | 15% | 42% | 8% | 17 | 18% | 29% |
| Grade: not recorded | 219 | 40% | 39% | 51% | 84 | 42% | 37% |
| Tier under $20 (band mid) | 207 | 34% | 47% | 58% | 30 | 53% | 33% |
| Tier $20–50 | 163 | 45% | 32% | 31% | 61 | 38% | 33% |
| Tier $50+ | 75 | 44% | 24% | 11% | 41 | 46% | 32% |
| **Width zero (low = high)** | **64** | **6%** | **66%** | **25%** | **2** | **0%** | **100%** |
| **Width under 30%** | **106** | **27%** | **40%** | **25%** | **54** | **26%** | **37%** |
| Width 30–50% | 69 | 41% | 38% | 16% | 18 | 44% | 39% |
| Width 50%+ | 206 | 57% | 28% | 34% | 58 | 62% | 24% |

Rows with unknown comp count (11) or book year (21) are left out of those slices. **How far above:** 53% of the 167 misses cleared within 25% of the band top, and 30% cleared more than 50% above it.

## Findings

1. **Zero-width bands (already fixed).** 64 history rows, 6% in band, 25% of the misses; 34 of them are $5/$5 bands. Without them, history is 46% in band and 33% above. This class is most of the under-$20 excess: the non-zero-width under-$20 rows are 43% in band. BUI-528 widens collapsed bands, and only 2 recent rows have one. Oracle: 40% to 49%. No new fix.
2. **Narrow bands, under 30% (worth testing).** 106 history rows and 54 recent rows (41% of recent bands) catch only 26–27% of prices, compared with 57–62% for bands of 50% or more. They miss both ways (40% above, 33% below), so the problem is overconfidence, not underpricing. Most are 3–5-comp pools with a low CV, where a Q25–Q75 band of five sales understates the spread. A 30% width floor around the same midpoint moves history from 40% to 49% in band and recent from 44% to 52%, and the median width stays at 40%. Oracle for the slice: 49%.
3. **No level fix anywhere.** The best in-sample multiplier on all rows (k=1.05) gains 3 points. The best on any slice (under $20, k=1.10) gains 4, and that slice is mostly the zero-width class. Low confidence, 0–2 comps, and 1985–99 each gain 1–3 points.
4. **Photo-graded rows (watch).** 15% in band (n=33), missing on both sides. Too few rows for a finding.
5. **Pricing basis can't be sliced.** 581 of 586 rows are `direct`, and `fmv_history` has no basis column. **Grade source** is recorded on 298 of 586 bids (none before June), and no note marks panel grades, so a panel can't be told apart from a single photo grade. The ±1.5 grade window reads 65% above (n=17), but 10 of those rows are one Invincible run.
6. **LOST versus the band.** eBay's final price is the clearing price whoever wins. We win only when it clears at or below our `max_bid`, which sits under the band top, so above-band rows are LOST by construction (94%). A low `max_bid` doesn't create a band miss; it only decides who wins. In 38 of the 157 LOST misses the price landed within one increment of our own `max_bid`, so it is a lower bound on what the winner would pay. In 33 we bid above the band top ourselves and still lost, so the operator had already judged those bands low.

## Recommended follow-ups

- **Test a Minimum FMV Band Width on Narrow Pools.** Replay a 30–40% floor on new auctions and score hit rate, band width, and overpay on won auctions. BUI-528 rejected a blanket floor because it can raise `fmv_high` (and `max_bid`) above the highest real comp. This data measures the outcome cost of not having one.
- **Add Band Width and a Post-BUI-528 Cut to the Accuracy Report.** The zero-width legacy rows depress the baseline by about 6 points, and the report should show that apart from current pricing.
