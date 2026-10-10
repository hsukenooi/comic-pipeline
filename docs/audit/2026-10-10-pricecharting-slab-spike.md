# BUI-1218: Does PriceCharting improve CGC slab FMV accuracy?

Date: 2026-10-10. Script: `2026-10-10-pricecharting-slab-spike.py` (scrape step: `2026-10-10-pricecharting-slab-spike-fetch.sh`). Analysis only, no production code.

## Verdict: GO, marginal and conditional

The decision bar is met: Winkler improves and no upward bias is added in aggregate. The gain is small once one outlier book is removed, and the 7 books PC newly prices are poor. Details below.

## Terms check

PriceCharting's terms (`/page/terms-of-service`, "Price Data Acceptable Use") say Price Data is PC's property and may be used "for your personal needs or internal business purposes" (the long form ties internal business use to a Legendary subscription). It may not be used "in any software, application, or system that is accessible to third parties" without written permission. `robots.txt` disallows only `/stripe-connect`, `/publish-offer`, and `/buy`. No clause bars automated access, but plain fetches hit a Cloudflare challenge, so any build depends on a rendering scraper. Result: not barred for a single-user pipeline, a gray area for anything shown to others. Confirm the subscription question before the build.

## Method

- 16 books (vintage and modern, deep and thin), 96 held-out sales: every sold-comps.com CGC universal-label sale in the ledger dated 2026-09-10 to 2026-10-09.
- Each sale is priced twice with `fmv_math.graded_fmv`, as of the day before: ledger comps sold before that date, versus the same plus PC CGC universal sales (grade, label, and page quality parsed from titles; PC tier summary prices ignored). The sale is hidden from both sources.
- PC sales are deduped against the ledger by eBay item id, else grade plus date (within a day) plus price (within 3%). PC supplied 4 to 341 CGC sales per page (about 250 per book on the deep ones), almost all absent from the ledger.
- Score: Winkler (alpha 0.5, scaled by sale price), in-band rate, coverage, signed bias ((mid - sale) / sale). Truth is the ledger sale price.

## Scores

| Run | Priced | Winkler | In band | Mean bias | Median bias |
|---|---|---|---|---|---|
| Ledger only | 87 / 96 | 0.703 | 22% | +0.033 | -0.019 |
| Ledger + PC | 93 / 96 | 0.530 | 43% | -0.044 | -0.068 |
| Ledger + PC, eBay-source rows only | 92 / 96 | 0.502 | 42% | -0.052 | -0.072 |
| Ledger + PC, best-offer rows at list price | 93 / 96 | 0.523 | 44% | -0.028 | -0.050 |

- **Paired (86 sales priced both ways):** Winkler 0.706 to 0.466, in-band 22% to 47%. Bootstrap 95% CI of the improvement is [0.04, 0.59]. PC is better on 40 sales, worse on 33, tied on 13.
- **One outlier drives the mean.** Avengers #87 (ledger-only band $1,575 for a $390 sale) accounts for most of it. Without it: paired Winkler 0.551 to 0.463, overall 0.549 to 0.528, median paired difference 0.
- **Coverage rises 87 to 93.** The 7 newly priced sales score Winkler 1.31, 0 of 7 in band, mean bias +0.22 (ASM #50 9.0 +0.78, ASM #78 4.0 +0.70, FF #48 9.6 +0.57). Two came from the ladder tier (ASM #50 8.5 at +0.09, FF #48 9.6 at +0.57). One sale went priced to refused.
- **Bias moves down, not up.** Part of that is basis: PC rows carry the accepted offer (see below). The list-price control run keeps most of the Winkler gain (0.523), so the gain comes from depth, not from the price basis.

## Per-book table (base / with PC)

| Book | Sales | Winkler | In band | Bias | PC sales kept |
|---|---|---|---|---|---|
| Amazing Spider-Man #194 | 8 | 0.41 / 0.43 | 0.50 / 0.25 | -0.05 / -0.10 | 314 |
| Giant-Size X-Men #1 | 16 | 0.42 / 0.42 | 0.20 / 0.44 | -0.04 / -0.06 | 341 |
| Amazing Spider-Man #50 | 11 | 0.45 / 0.57 | 0.11 / 0.18 | -0.01 / +0.04 | 264 |
| X-Men #101 | 6 | 0.81 / 0.69 | 0.00 / 0.50 | +0.03 / -0.17 | 308 |
| Fantastic Four #49 | 6 | 0.56 / 0.50 | 0.50 / 0.83 | -0.12 / -0.12 | 261 |
| Iron Man #55 | 6 | 0.81 / 0.47 | 0.00 / 0.40 | +0.11 / +0.04 | 329 |
| X-Men #96 | 7 | 0.47 / 0.57 | 0.29 / 0.50 | -0.09 / -0.10 | 236 |
| Amazing Spider-Man #78 | 4 | 1.62 / 1.45 | 0.00 / 0.25 | +0.42 / +0.25 | 213 |
| Fantastic Four #48 | 9 | 0.73 / 0.55 | 0.14 / 0.62 | -0.05 / +0.05 | 323 |
| Batman #227 | 5 | 0.64 / 0.46 | 0.00 / 0.40 | +0.09 / -0.12 | 229 |
| Batman #655 | 1 | 0.46 / 0.75 | 0.00 / 0.00 | -0.19 / -0.24 | 127 |
| Ultimate Fallout #4 | 4 | 0.09 / 0.09 | 0.50 / 0.50 | +0.02 / +0.02 | 4 |
| Invincible #84 | 2 | 0.29 / 0.65 | 1.00 / 0.50 | -0.10 / -0.20 | 40 |
| X-Force #11 | 3 | 0.69 / 0.54 | 0.00 / 0.67 | +0.09 / -0.10 | 145 |
| Avengers #87 | 2 | 7.25 / 0.59 | 0.00 / 0.00 | +1.81 / +0.04 | 201 |
| X-Men #12 | 6 | 0.34 / 0.37 | 0.33 / 0.50 | -0.07 / -0.10 | 257 |

Better on 8 books, worse on 6 (ASM #194, ASM #50, X-Men #96, Batman #655, Invincible #84, X-Men #12), flat on 2. Per-sale rows print from the script.

## Price conflict

PC's page labels two prices on best-offer sales: "best offer accepted price" and "best offer list price". Of 295 sales present in both sources, 92 differ: 8 single-price rows by about 1% (FX), and 84 best-offer rows. In all 84 where PC shows two prices, the ledger holds the list price (79 exactly, the other 5 within 1%), never the accepted amount. Batman #227 6.5 (Sep 22): PC accepted $1,087, list $1,275, ledger $1,275. The accepted amount is a median 0.89 of list (range 0.65 to 0.97). PC accepted prices look like real offers: 94% whole dollars and 81% multiples of $5, versus 77% and 60% for the list prices.

This contradicts the BUI-552 close ("`soldPrice` already is the accepted amount"). The eBay listing cannot settle it: `ebay-fetch` on 6 of the conflicting items shows only the list price (for example $1,275 for Batman #227), because eBay does not publish the accepted amount. So PC's figure is the only evidence of the real price, and the ledger overstates best-offer sales by about 11%. That matters beyond this spike (BUI-552 should be reopened).

## Recommendation for the build ticket

If filed: slab-only PC provider, use PC's accepted price, and do not let PC rungs alone open ladder-tier or `outside_ladder` rows (the newly priced rows were out of band 7 of 7). Settle the terms question first.
