# CGC proxy factor vs ledger raw/slab pairs (BUI-976)

**Date:** 2026-09-24. **Question:** the CGC proxy tier prices raw at 0.50–0.55 × the eBay CGC price
at the target grade (`apps/fmv/src/fmv_math.py:730-731`, calibrated on ASM #50 alone), and bids
0.70 × the top of that band, so 0.385 × slab. Is that factor right?

**Answer:** the factor is low for the typical book, but the ratio varies too much across books to
support a better single constant. Keep the factor, and add a flag-only guard for when the proxy
band's top falls below the book's own ungraded anchor. The data can't support a split by tier or
grade yet.

## Method

- **Source:** the live comps ledger, opened read-only (`mode=ro`). Rows with `excluded_code` set
  are dropped.
- **Window:** sales on or after 2026-03-01 in both pools. All slab rows fall in May–Sep 2026 and
  1,054 of 1,058 graded raw rows on slab books fall in 2026, so the two pools are contemporaneous.
- **Slab:** universal label and CGC/CBCS only (all 978 live slab rows qualify). Rows whose title
  prints a year more than 2 off the book's year are dropped (for example, Marvel Feature #1 1971
  Defenders slabs filed on the 1975 Red Sonja book), as are facsimiles and reprints.
- **Raw:** a single copy with a parsed grade. A row is dropped when any of these fire: the
  production reject chain (`comic_identity.is_comp_excluded` and `should_reject`), a marker list for
  wrong-book and not-raw titles (CGC, lot, variant, facsimile, Ultimate, Astonishing, Milestone,
  `#50B`, and similar), or a title year more than 2 off the book's year.
- **Duplicates:** 907 raw and 14 slab rows repeat a sale that the other provider already stored
  (serpapi writes `Jul 5, 2026` and sold-comps.com writes `2026-07-05` for the same sale). Each is
  counted once.
- **Pairing:** the unit is a (book, grade) cell with at least one sale in each pool. The cell ratio
  is the median raw price over the median slab price. Exact-grade pairing is the primary cut, and
  ±0.5 is the sensitivity check. Cuts with fewer than 3 cells aren't findings.

## Results (exact grade)

| Cut | Cells | Books | Median ratio | IQR | Band too high (<0.50) | In band | Band too low (>0.55) | Max bid > raw median (<0.385) |
|---|---|---|---|---|---|---|---|---|
| All | 153 | 60 | 0.69 | 0.45–0.93 | 43 | 6 | 104 | 32 |
| Slab $0–100 | 46 | 30 | 0.65 | 0.40–0.84 | 16 | 0 | 30 | 11 |
| Slab $100–400 | 75 | 39 | 0.73 | 0.48–1.02 | 20 | 5 | 50 | 16 |
| **Slab ≥ $400 (proxy floor)** | **32** | **12** | **0.74** | **0.57–0.89** | **7** | **1** | **24** | **5** |
| Grade < 4.0 | 13 | 10 | 0.71 | 0.45–0.78 | 4 | 0 | 9 | 2 |
| Grade 4.0–5.5 | 40 | 26 | 0.83 | 0.66–1.00 | 3 | 0 | 37 | 2 |
| Grade 6.0–7.5 | 43 | 29 | 0.60 | 0.46–0.86 | 13 | 3 | 27 | 8 |
| Grade 8.0+ | 57 | 36 | 0.57 | 0.30–0.93 | 23 | 3 | 31 | 20 |
| Year < 1970 | 86 | 30 | 0.68 | 0.47–0.91 | 23 | 4 | 59 | 16 |
| Year 1970–1984 | 42 | 14 | 0.72 | 0.34–0.91 | 13 | 1 | 28 | 12 |
| Year 1985+ | 25 | 16 | 0.71 | 0.43–1.08 | 7 | 1 | 17 | 4 |
| ≥2 sales per side | 30 | 15 | 0.68 | 0.40–0.85 | 10 | 1 | 19 | 7 |

In the proxy-floor subset, the ratio by grade is 0.85 below 6.0 (7 cells, 4 books), 0.67 at
6.0–7.5 (12 cells, 7 books), and 0.71 at 8.0+ (13 cells, 8 books, min 0.26). The per-book median
ratios for the 12 books range from 0.26 to 0.91.

**Sensitivity (±0.5 grade):** 305 cells over 77 books, with a median of 0.61. Among proxy-floor
cells (78) the median is 0.59. The lower figure comes from the top of the scale: a raw "NM" sale
(9.2–9.4) is paired with 9.6–9.8 slabs, which cost 2–5× as much. So ±0.5 is only valid below 9.0.
The exact-grade cut is the one to trust.

## Findings

1. **The current band mostly misses low, which loses auctions.** Among proxy-floor cells, 24 of 32
   sit above 0.55 and 27 of 32 have a raw median above the 0.385 × slab max bid. At the median
   ratio (0.74), the proxy bids about 52% of the raw market. Batman #227 fits this pattern:
   ledger cells 0.86 (6.5) and 0.95 (5.5), and a 0.85 hammer.
2. **It misses high rarely and by little.** In 5 of 32 cells the max bid sits above the raw median,
   by about $17–65 each: ASM #82 9.4, FF #67 9.0, DD #16 8.5, Iron Man #55 9.4, and FF #49 7.0.
   All but FF #49 are 8.5+ cells, where a raw copy can't prove its grade.
3. **Recalibrating the constant only trades one miss for the other.** At 0.65–0.70, band-too-low
   cells drop from 24 to 18, but band-too-high cells rise from 7 to 10, and max-bid-above-median
   cells rise from 5 to 7. The spread across books (0.26–0.91) is larger than any shift in the
   constant.
4. **The tier and grade splits are too thin to act on.** The proxy floor has 7 cells from 4 books
   below grade 6.0, and only 9 proxy-floor cells from 5 books have 2 or more sales on each side.
5. **Neither existing warning can fire on a proxy row.** `cgc_proxy_fmv` hard-codes
   `ungraded_anchor=None`, `anchor_diverges=False`, and `cgc_cross_check=None`
   (`fmv_math.py:960-968`). In the paired data:
   - The `cgc_cross_check` DIVERGES test (ratio outside 0.375–0.875) fires on 14 of 32
     proxy-floor cells in both directions. It would not fire at Batman's 0.85.
   - `anchor_diverges` at T=0.5 fires on 13 of 32 cells, every time for the band sitting *above*
     the anchor. That's noise: the anchor is a median across all grades, and these slabs are
     high-grade.
6. **Band top below the anchor is a clean guard for the low miss.** "fmv_high < ungraded anchor
   (n ≥ 8)" fires on 10 of 32 proxy-floor cells, and all 10 have a ratio above 0.55 (precision
   10/10, recall 10/24). At ±0.5 it's 15 of 16. It also fires on Batman #227: band top $500 vs
   anchor $609 (n=18). At 0.8 × anchor it fires on 6 of 6, and at 0.5 × anchor it never fires.
7. **The rule has priced far fewer rows than the ticket's count.** Of the 5 proxy `fmv` rows, only 2
   come from the rule (Batman #227 6.0 and 4.5, fmv 1222 and 1235). The other 3 are hand-priced on
   the Heritage basis from §7a (10–15% discount). There are two resolved hammers:
   - Batman #227 6.0 (rule): LOST at $761 against a $900 slab, ratio 0.85, in line with finding 1.
   - FF #16 6.5 (hand): WON at $233.50 against about $510 Heritage, ratio 0.46. It sits inside the
     ledger's spread; FF #16's 6.0 cell is 0.67.

## Recommendation

| Option | Pros | Cons |
|---|---|---|
| Keep the factor | Stays money-safe: the max bid tops the raw median in only 5 of 32 cells, by $17–65 | Loses most proxy auctions (27 of 32) |
| Recalibrate to 0.65–0.70 | Moves 6 cells into range | Adds 3 band-too-high cells and 2 max-bid-too-high cells, and the spread is still 0.26–0.91 |
| Split by tier or grade | Grade < 6.0 reads 0.85 | Only 7 cells from 4 books, so it isn't a finding |
| Per-book ratio, pooled fallback | Matches the real spread | Only 15 books have a cell with 2 or more sales per side, and proxy books are sparse on raw by definition |
| **Keep the factor and flag band top below the anchor** | Precision 10/10 (15/16 at ±0.5), and it fires on Batman #227 | Recall is 10/24, and it depends on the anchor reaching n ≥ 8 |

**Recommended:** keep 0.50–0.55, and add a flag-only `proxy_below_anchor` check that fires when
`fmv_high < ungraded_anchor` with anchor n ≥ 8, T = 1.0. The flag never re-prices the band. To
support it, compute the ungraded anchor on the proxy path instead of hard-coding `None`. Revisit a
grade split below 6.0 once the monthly slab collection gives 3 or more proxy-floor cells in each
band, from at least 8 books.

**Caveat:** raw grades are the sellers' own descriptors ("FN+" parses to 6.5), which run
optimistic. A ratio measured against a certified grade would likely come out lower, and that
argues against raising the constant.

## Appendix: re-running

The script is `docs/audit/2026-09-24-cgc-proxy-ratio.py` (read-only; imports
`apps/ebay/src/comic_identity`). Run it from the repo root:

```sh
python3 docs/audit/2026-09-24-cgc-proxy-ratio.py 0 1      # exact grade (tables above)
python3 docs/audit/2026-09-24-cgc-proxy-ratio.py 0.5 1    # ±0.5 sensitivity
python3 docs/audit/2026-09-24-cgc-proxy-ratio.py 0 2 -v   # ≥2 sales per side, per-cell listing
```

To pull the proxy rows and their hammers:

```sql
SELECT f.id, f.comic_id, f.grade, f.low, f.high, f.ungraded_anchor, b.status, b.prior_status, b.winning_bid
FROM fmv f LEFT JOIN bid_fmvs bf ON bf.fmv_id = f.id LEFT JOIN bids b ON b.id = bf.bid_id
WHERE f.pricing_basis = 'proxy' OR f.notes LIKE '%CGC proxy%';
```
