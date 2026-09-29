# Folding ungraded "provider copies" into graded twins (BUI-1020)

**Date:** 2026-09-30. **Source:** `uv run --project plugins/gixen-overlay python docs/audit/2026-09-30-provider-copy-fold.py`, against the live comics DB opened read-only through BUI-1014's `connect`, with BUI-1014's loaders and its `AS_OF` pin (2026-09-29T09:00 UTC). No provider calls and no writes.

**Verdict: reject. The premise is broken.** The 187 rows aren't provider copies. They are distinct sales that share a round price with a graded sale of the same book by chance. Folding them also can't move a production band, because `build_pool` never admits a grade-less comp. Winkler/price on the affected books' sales is 1.135 before and 1.135 after, with 0 of 1,758 bands changed.

## The rows aren't copies

BUI-1014's `ceiling` rule matched a grade-less raw row to a graded held-out sale of the same book when the price agrees within $0.01 and the grade-less row sold in the 7 days before. The script reproduces the count exactly: 187 pairs, 171 distinct grade-less rows, and 91 books.

- **Identity:** 0 of 187 pairs share a `product_id`, and 0 of 187 share a title. Titles are copied verbatim off the eBay page by both providers, so different titles mean different listings.
- **Provider:** 181 of 187 pairs come from the same provider on both sides (108 SerpApi, 73 sold-comps.com). A cross-provider copy needs two providers. For sold-comps.com, `product_id` is the eBay item number, so its 73 pairs are provably different items. The 6 cross-provider pairs are different listings too, for example a Marvel Legends figure matched to an X-Men #25 lot at $25.
- **Prices:** the matches sit on round prices: $4.99 (24), $9.99 (19), $5.00 (14), and $19.99 (11).
- **Chance control:** the same-book price-match rate between grade-less rows and graded sales is flat across lags. It is 1.54% at 1 to 7 days, and 1.11% to 1.60% in every 7-day bin out to 84 days. At the 8 to 84 day rate, 169 matches are expected at 1 to 7 days. 187 are observed, an excess of 18, which is within about 1.4 standard deviations of chance. A real copy class would pile up at short lags.

## Production never double counts them

The ticket asked whether each copy enters the raw pool beside its twin. It doesn't, by construction:

- **Pool:** `fmv_math.build_pool` keeps only comps with a parsed grade. A grade-less comp feeds `ungraded_market_anchor` alone. That anchor is flag-only: it sets `anchor_diverges` and `proxy_below_anchor` and the stored `ungraded_anchor` columns, and it never moves a band or a guard.
- **Dedup:** production's cross-provider dedup is `_dedupe_pool_by_identity` in `apps/fmv/src/fmv_runner.py`. It keys on `product_id`, then on grade, price, sold date, and title together. A grade-less row can never form an identity key, so it could never fold into a graded twin. With no shared titles, it wouldn't match anyway.
- **Pool side:** all 187 twins are raw-pool rows (the harness loads raw only), and every grade-less row is raw. The pair never spans the raw and slab pools.

## Measurement

The BUI-1005 harness (`2026-09-28-grade-adjusted-pool-backtest.py`) is the comparison. Its `price_current` arm calls the production `fmv_math.compute_fmv` unchanged, including widening, guards, the bracket rescue, and the width floor. BUI-1007 scores a grade-adjusted replacement pool, which isn't the production path. The only change here is to pass each book's grade-less comps in the 90-day window as `grade=None`, which is what a live fetch hands `compute_fmv`.

| Cut | Sales | Priced | Median W/price | Bands changed | Flags changed |
|---|---|---|---|---|---|
| Affected books, before fold | 1,758 | 1,214 | 1.135 | – | – |
| Affected books, after fold | 1,758 | 1,214 | 1.135 | 0 | 0 |

- **Oracle bound:** the maximum band effect is zero in both directions, so the median-versus-Q75 question from BUI-770 doesn't arise, and there is no per-book direction to report. Every book is unchanged.
- **Anchor:** the fold changes the ungraded anchor on 1,198 sales and flips `anchor_diverges` on 13. Those changes come from deleting genuine sales from the anchor, so they would make it less accurate, not more.
- **In band:** unchanged, because no band changes.

## Review notes

- **Leakage:** there is no fitting. Training is the harness's own `[d − 90, d)` window, and grade-less comps are restricted to the same window.
- **Production path:** `compute_fmv` is called directly, as in production. The ledger stands in for the live fetch, which both prior harnesses accept.
- **Window:** the chance control spans lags from 1 to 84 days, and the rate is flat across all of them. A different pricing window can't turn distinct items into copies, so the verdict doesn't depend on the window or the pin.
- **Residual:** if some of the 18 excess matches were real copies, the identity evidence can't find them, and they still couldn't enter a band.

## Out of scope

- **BUI-1014's `ceiling` drop is chance matches, not copies:** its imputation arms dropped 187 genuine grade-less sales. Its imputation was a pool-shape lever it didn't propose, and its verdict rests on the `bound` arm, which doesn't use this rule. The doc's "provider copies" wording is wrong.
- **The harness's graded near-duplicate rule is half chance:** same grade and price within 7 days matches 61 pairs at 1 to 7 days, against 28 expected at the 8 to 84 day rate. About half of BUI-1005's 61 dropped "copies" are distinct sales. That changes training pools slightly in BUI-1005, BUI-1007, and BUI-1014. It's a harness issue, not a production one, because production dedups on title as well.
- **Future grader:** if a photo grader (BUI-1016) ever assigns grades to grade-less comps, production dedup gains an identity key for them. Real cross-provider copies would then fold on title, as they should. No change is needed ahead of that.
