# BUI-1229: PriceCharting slab test set and ledger-only baseline

Date: 2026-10-11. Analysis only, no production code.

- `pc_slab_test_set.py` fetches, parses, verifies, and loads the test set (the module the follow-ups import).
- `2026-10-11-pc-slab-test-set.py` scores the baseline and runs the overlap check.

## Verdict

- **Test set built.** 47 books, 9,889 graded PriceCharting (PC) sales, 9,460 of them CGC universal sales the live slab pool would admit. Sales are dated 2002 to 2026-10-10, 80% from 2024 on.
- **Baseline.** Ledger-only `graded_fmv` scores Winkler **0.765 [0.670, 0.901]** against 904 PC sales in 2026-08-01..2026-10-10. In-band is 15% [11%, 19%] and bias is +0.081 [+0.054, +0.121]. Coverage is 81%.
- **Overlap check: PC sales score like ledger sales.** On the 16 BUI-1218 books, PC truth at list basis scores Winkler 0.701 [0.591, 0.866]. Ledger truth over the same window scores 0.742 [0.593, 0.989]. In-band is 14% vs 13%, bias +0.034 vs +0.037. The CIs overlap almost fully.
- **Heritage sales don't score like eBay sales.** Heritage-sourced PC sales score Winkler 1.060 with bias +0.243, against 0.723 and +0.058 for eBay. Follow-ups should split by `source` or score eBay only.

## How to use it

```python
sys.path.insert(0, "<repo>/docs/audit")
from pc_slab_test_set import load_test_set, universal_sales, same_sale, load_ledger
ts = load_test_set()                     # reads the JSON artifact, read-only
rows = universal_sales(ts, comic_id=28)  # admitted CGC universal sales for one book
```

- **Artifact:** `~/comic-grader-fixtures/bui-1229-pc-slab-test-set/test_set.json` (6.4 MB).
- **Page cache:** `~/comic-grader-fixtures/bui-1229-pc-slab-test-set/pages/`, one firecrawl markdown file per book. Neither is committed: PC price data stays out of the repo.
- **Rebuild:** `uv run --project plugins/gixen-overlay python docs/audit/pc_slab_test_set.py fetch` scrapes missing pages only. Then run `... build`. A rebuild after new PC sales changes the counts, so record `meta.built` in any doc that cites a number.

## Schema (`schema_version` 1)

- **`meta`:** `built`, `n_books`, `n_sales`, `n_universal`, `pages_dir`, and `skipped` (`comic_id`, `slug`, `reason`, `h1`).
- **`books[]`:** `comic_id`, `series`, `issue`, `year`, `era` (silver <1970, bronze <1985, copper <1992, modern), `known` (one of the 16 BUI-1218 books), `slug`, `pc_h1`, `n_sales`, `n_universal`, `n_in_ledger`, `n_ledger_universal`, `first_sale`, `last_sale`, `parse_stats`.
- **`sales[]`:** one row per graded (CGC or CBCS) sale, deduped by eBay item id within the page.
  - **Identity:** `comic_id`, `product_id` (eBay item id, else a stable `pc:` hash), `title`, `url`, `source` (`ebay` or `heritage`).
  - **Price:** `sold_date` (ISO), `price` (PC's sold amount, the ACCEPTED offer on best-offer sales), `ask` (the best-offer LIST price, else null), `best_offer`.
  - **Slab:** `grade`, `certifier`, `label` (`universal`, `qualified`, `restored`, `conserved`, `signature_series`), `page_quality` (`white`, `ow_w`, `ow`, `c_ow`, `cream`, `unknown`).
  - **Admission:** `exclude_code` (null, or why the live graded pool drops the title: `hard_exclude`, `cross_title`, `store_variant`, plus `issue_mismatch` from this script). `universal` is true when certifier is CGC, label is universal, and `exclude_code` is null.
  - **Hygiene:** `price_outlier` (below 0.25x or above 4x the median of the book's other same-grade universal sales within 365 days, null with fewer than 3 neighbours). It looks both ways in time, so use it to drop bad truth rows, never to filter comps.
  - **Ledger overlap (universal rows only):** `in_ledger`, `ledger_product_id`, `ledger_price` (`ledger_match`: same item id, else same grade, date within a day, and price within 3% of PC's sold OR list price).

Rows outside the universal label stay in the set, marked, for BUI-1233. They are few: 21 qualified, 4 conserved or restored, 1 Signature Series, and 191 CBCS. PC's own titles rarely carry SS or restored slabs on these pages.

## Method

- **Pages.** The book list is the 48 Hsu Ken approved. Each page's H1 (series, issue, year, and the PC console name) is checked against the book.
  - Two guessed slugs hit PC's search page and took one obvious fix: X-Men #108 needed `the-x-men-108-1977`, and Captain America Annual #8 lives under `captain-america-annual/`.
  - The BUI-1218 spike's Ultimate Fallout #4 page was the **Facsimile Edition** console, a different product. This set uses the original `ultimate-fallout/ultimate-fallout-4-2011` (210 universal sales, against the spike's 4).
  - Uncanny X-Men #281 is PC's base page (1st printing). None of its graded sale titles names a gold or variant printing.
  - Marvel Feature #1 is dropped: the PC page and the ledger (comic 748) both mix the 1971 Defenders book with the 1975 Red Sonja book.
- **Baseline.** Each PC universal sale dated 2026-08-01..2026-10-10 is priced with `fmv_math.graded_fmv` as of the day before. Comps are ledger CGC universal slab comps sold strictly before the sale date. Every ledger row that `same_sale` matches is hidden, so the sale can't price itself. The window starts in August because ledger capture began 2026-08-04 with about 90 days of backfill. Earlier PC sales have no ledger history to price from.
- **Basis.** Ledger comps hold the LIST price on best-offer sales (BUI-1222). The headline scores against PC's SOLD price, which is the accepted offer: the real transaction. A list-basis score (truth = `ask` when present) runs beside it. The basis gap moves bias by about 3 points (+0.081 vs +0.050) and barely moves Winkler (0.765 vs 0.763).
- **Dedup.** `same_sale` also matches PC's list price. The spike's `is_dup` compared only the sold price, so it missed best-offer twins, which the ledger stores at list.
- **CIs.** These are 95% percentile bootstraps (2,000 reps, seed 1229). Each rep resamples BOOKS with replacement and takes the sale-weighted mean.
- **Truth hygiene.** The baseline drops 3 `price_outlier` truth rows in the window. With them kept, Winkler is 1.168 [0.712, 2.104], driven by one raw X-Force #11 titled "CGC likely 9.6" that sold for $0.71.

## Scores (ledger-only pricing, 2026-08-01..2026-10-10)

| Cut | Sales | Priced | Winkler [95% CI] | In band | Bias |
|---|---|---|---|---|---|
| **All, sold basis** | 904 | 81% | **0.765 [0.670, 0.901]** | 15% | +0.081 |
| All, list basis | 904 | 81% | 0.763 [0.676, 0.885] | 13% | +0.050 |
| Silver (21 books) | 398 | 83% | 0.819 [0.697, 0.947] | 8% | +0.093 |
| Bronze (12 books) | 290 | 77% | 0.724 [0.547, 1.264] | 16% | +0.096 |
| Copper (2 books) | 21 | 95% | 0.660 [0.571, 0.770] | 55% | +0.061 |
| Modern (11 books) | 195 | 83% | 0.724 [0.633, 1.020] | 20% | +0.039 |
| PC-only (not in ledger) | 520 | 85% | 0.727 [0.633, 0.849] | 16% | +0.074 |
| eBay source | 789 | 81% | 0.723 [0.636, 0.849] | 16% | +0.058 |
| Heritage source | 115 | 81% | 1.060 [0.790, 1.341] | 6% | +0.243 |

The per-era cells are thin. Copper is 2 books and 21 sales, so its CI is not credible. The set has no Silver book above about $2k a copy in the ledger, no Copper book above $500, and no Modern book with more than 21 ledger comps. Refusals: `outside_ladder` 103, `ladder_too_thin` 33, `ladder_non_monotone` 26, `no_certifier_pool` 7.

## Overlap check (16 BUI-1218 books, same pricing, same window)

| Truth | Sales | Priced | Winkler [95% CI] | In band | Bias |
|---|---|---|---|---|---|
| PC, sold basis | 557 | 84% | 0.706 [0.586, 0.909] | 15% | +0.066 |
| PC, list basis | 557 | 84% | 0.701 [0.591, 0.866] | 14% | +0.034 |
| PC, eBay only, list basis | 487 | 85% | 0.669 [0.577, 0.823] | 14% | +0.009 |
| PC sales not in ledger | 350 | 86% | 0.659 [0.540, 0.817] | 16% | +0.052 |
| Ledger (sold-comps.com, list basis) | 316 | 81% | 0.742 [0.593, 0.989] | 13% | +0.037 |

PC holds 557 sales on these books in the window where the ledger holds 316. The extra sales score no worse than the ledger's own.

## Secondary: PC-history pricing

This isn't the spec baseline. Each universal sale is priced from earlier PC sales only. It gives the follow-ups a starting point on the full set: Winkler 0.862 [0.788, 0.939], in-band 33%, bias +0.085, 7,568 of 9,394 sales priced. Pre-2021 sales are almost all refused (too few earlier sales). Winkler falls by sale year: 1.91 (2022), 1.51 (2023), 1.17 (2024), 0.89 (2025), 0.62 (2026). That drift is BUI-1231's question.

## Out-of-scope findings

- **The spike's Ultimate Fallout #4 numbers came from the Facsimile Edition page.** That page held 4 kept sales against 210 on the original, so the BUI-1218 per-book row for that book compared the wrong product.
- **Comic 748 (Marvel Feature #1) mixes two volumes in the ledger.** Ledger slab comps include 1971 Defenders copies ($1,900 at 9.6) and 1975 Red Sonja copies ($93 at 9.6) under one comic.
- **`hard_exclude` drops "retold".** It removes 50 of 156 Amazing Spider-Man #94 sales, 49 of them titled "origin retold", the book's key. The live pool likely loses a third of that book's comps the same way.
- **PC mis-files a few sales.** 12 rows name another issue on the page, for example "Amazing Spider-Man 105" on the #45 page. These are flagged `issue_mismatch`.
