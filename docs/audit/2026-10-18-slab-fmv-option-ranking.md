# BUI-1226: Rank the slab FMV options from the PriceCharting project

Date: 2026-10-18. Analysis only, no production code. Script: `2026-10-18-slab-fmv-option-ranking.py`. Test set: BUI-1229 artifact, `meta.built` 2026-10-13 (47 books, 9,516 CGC universal sales). Ledger read from `~/.comics-server/db.sqlite` with `mode=ro`.

## Recommendation: one build ticket, append PriceCharting sales to the slab pool

- **Append is the only unshipped option that clears the bar.** Against the post-ship baseline it cuts Winkler 0.634 to 0.509 on 745 paired sales in 46 books. The paired delta is **-0.125 [-0.198, -0.071]**, book-clustered.
- **It survives the drop-top-book rerun.** Without Fantastic Four #48 (16% of the summed gain) it is -0.111 [-0.184, -0.063]. Without FF #48 and Avengers #87 it is -0.091 [-0.136, -0.055]. No single dropped book lifts the CI's upper bound above -0.063.
- **It survives a truth that is not PriceCharting (PC).** Scored on 326 paired sold-comps.com ledger sales without a best-offer flag, the delta is -0.093 [-0.157, -0.036]. It's -0.079 [-0.129, -0.029] without its top book. Pricing PC best-offer comps at list (scaled x0.89 like ledger comps) keeps it at -0.122, so the gain is depth, not price basis.
- **Access is legitimate for this pipeline, with conditions.** PC's terms allow Price Data "for your personal needs" and bar only use in a system "accessible to third parties" (BUI-1218 terms check). No clause bars automated access. The paid API returns aggregate tier prices only (BUI-525), so sale rows come from the public product page through firecrawl. That's the one runtime dependency.

**Proposed ticket.** Title: "Append PriceCharting CGC sales to the slab comp pool". Done when: `comic-fmv` graded mode adds PC CGC universal sales (accepted price, `best_offer` false, deduped against the ledger by `same_sale`) for slab targets, with each book page cached and a failed scrape falling back to ledger-only pricing. A refused ledger-only row stays refused unless the appended pool prices it `direct`. A live re-run on the Mac Mini reproduces the paired gain within this doc's CI, and PC data stays off any page shown to someone else. Hsu Ken confirms the personal-use reading before merge.

## Ranking (Winkler delta, negative is better; 95% CIs clustered by book)

| Option | Ticket | Delta [95% CI] | Baseline it was measured on | Status |
|---|---|---|---|---|
| Widen the published band to +/-15% | BUI-1230 | -0.157 [-0.193, -0.119] | Pre-ship, 0.843 (holdout) | Shipped BUI-1242 |
| **Append PC sales** | BUI-1218, this doc | **-0.125 [-0.198, -0.071]** | **Post-ship, 0.634** | **Recommended build** |
| Best-offer x0.89 slab scale | BUI-1234 | -0.047 [-0.074, -0.030] | Pre-ship, 0.765 | Shipped (+BUI-1247), see below |
| Recency flat365 | BUI-1231 | -0.018 [-0.033, -0.006] | Shipped weights | NO-GO, best of 12 |
| Drift adjustment, 5 to 20%/yr | BUI-1231, BUI-1260 | +0.008 to +0.038 | Shipped weights | NO-GO, worse at every rate |
| Pooled grade-premium curve | BUI-1232, BUI-1260 | +0.068 [-0.126, +0.244] | Ledger thin rungs | NO-GO |
| Label and page-quality premiums | BUI-1233 | Not Winkler-scored | | NO-GO, CIs span 1 |

- Only the append row is measured on the post-ship baseline. The shipped gains don't add: each was measured against the pre-ship model.
- The grade-premium curve does beat the ladder on PC-history ladder rows, -0.355 [-0.566, -0.154]. That needs PC data at runtime, so it's a candidate follow-up once the append ships. It isn't combined here: the curve harness refits per quarter and per book, which isn't a cheap compose.

## Post-ship baseline and append detail (906 PC sales, 2026-08-01..2026-10-10, outliers dropped)

| Arm | Priced | Winkler [95% CI] | In band | Bias |
|---|---|---|---|---|
| Shared harness today (band in, no best-offer flag) | 740 | 0.667 [0.593, 0.772] | 56% | +0.086 |
| Production flags only (`comps.best_offer` as stored) | 740 | 0.664 [0.591, 0.766] | 56% | +0.084 |
| **Post-ship baseline (stored flag, else BUI-614 capture)** | 746 | **0.633 [0.566, 0.726]** | 60% | +0.050 |
| Append, all prior PC sales | 877 | 0.534 [0.492, 0.587] | 65% | -0.006 |
| Append, PC sales from the last 90 days only | 830 | 0.597 [0.540, 0.681] | 63% | +0.038 |
| Append, eBay-source PC sales only | 875 | 0.538 [0.494, 0.592] | 65% | +0.000 |
| **Append with the refusal guard (the build)** | 860 | **0.516 [0.478, 0.565]** | 66% | -0.012 |

- **Per book**, the append is better on 38 books, worse on 7, and tied on 1. Per sale it's better on 330 and worse on 241. Silver (-0.116) and Modern (-0.062) gain clearly. Copper gains on its 2-book mean but loses per sale, 6 better and 11 worse.
- **Newly priced rows.** The append prices 132 sales the baseline refuses, at Winkler 0.676 and bias +0.019. The guard keeps 115 of them at 0.564 and refuses 17 that PC rungs alone would open, which score about 1.4. BUI-1218's 7 newly priced misses don't recur at scale.
- **The 90-day window loses.** It gains only -0.056 [-0.114, -0.004] and fails the drop-top-book rerun: -0.039 [-0.082, +0.005] without Avengers #87. `graded_pool` already ages comps out past 365 days, so the full-history append is the 365-day append.
- **Method.** PC sales are added only if dated strictly before the test sale. The test sale and its `same_sale` twins are hidden from both pools. A PC row that `same_sale` matches a ledger comp is dropped and the ledger copy stays. Bootstrap: 2,000 reps over books, seed 1229.

## Out-of-scope findings

- **Production realizes a tenth of the best-offer gain.** `comps.best_offer` is True on 25 of 922 ledger slab rows on these books. The BUI-614 capture flags 243. Stored flags alone move Winkler -0.003. A capture backfill would add -0.030 [-0.046, -0.018]. It's worth its own ticket, since `upsert_comps` fills a flag only on a later sighting.
- **The shared harness never selects `comps.best_offer`.** `pc_slab_test_set.load_ledger` drops it, so `2026-10-11-pc-slab-test-set.py` scores without the x0.89 scale. Its 0.780 came from the 2026-10-12 rebuild, before BUI-1242 merged. Today it prints 0.667. This doc's script selects the column itself and leaves the shared module unchanged.
- **The bid side isn't measured.** `max_bid` rides `bid_high`, not the published band. The append lowers bias from +0.050 to -0.006, so expect bid caps to drop slightly. The build should re-check `recomputed_cap` parity.
