---
title: "Raw ledger merge backtest (BUI-1213)"
date: 2026-10-10
---

# Raw ledger merge backtest (BUI-1213)

**Date:** 2026-10-10. **Source:** `uv run --project plugins/gixen-overlay python docs/audit/2026-10-10-raw-ledger-merge-backtest.py`. Reads the comics server over HTTP through `comics-api` only (`/api/comics/comps/all?pool=raw`, `/api/comics/accuracy`, `/api/comics`). No provider calls, no DB file, no writes.

**Decision: keep the raw path live-only for now.** Merging graded ledger comps aged 91 to 365 days at weight 0.5 rescues 9 of 229 refused raw rows and changes 36 of 350 auction bands. Accuracy is a wash. Pollution matches the live window. The ledger only began on 2026-08-04, so few rows have aged past 90 days yet.

## Method

- **Reconstruction assumption:** neither pool comes from a live fetch. Both come from the comps ledger, which archives every past fetch. Live-only means the book's graded raw ledger comps sold in [D − 90, D). Merged adds the book's graded raw comps sold in [D − 365, D − 90) at weight 0.5 (`graded_comp_weight`). The ledger can hold more comps than one fetch returned.
- **Evaluation date D:** the auction's end date, the held-out sale's date, or the refused row's `fmv_updated_at`. Every comp sold on or after D is excluded. An auction's own listing (`product_id == item_id`) never enters its pool. Rescue pools also require `first_seen_at <= D`.
- **Pricing:** `fmv_math.compute_fmv`, unchanged. Its exponential recency weight is multiplied by each comp's age weight (1.0 for live-window comps), so a live-only pool prices exactly as production does. The grade guards stay unweighted, as they are in production.
- **Ledger load:** graded raw rows with a price and a parseable date (10,582). The script drops rows that carry an exclusion stamp, including every copy of a stamped listing (8), come from a `no-variant` or `broader` tier (391), fail the current `hard_exclude` (5), or fail `graded_identity_exclude` (49). It then dedupes by product id, then by grade, price, and date. 9,597 rows remain.
- **Scoring:** BUI-979's `winkler_score` (alpha 0.5) divided by the sale price.

## Results

| Measure | Live-only | Merged |
|---|---|---|
| **Rescue:** stored refused rows priced (229: 125 sparse, 64 wide, 40 one-sided) | 12 (reconstruction drift) | **+9** (8 `too_wide`, 1 `one_sided`, 0 `too_sparse`) |
| **Accuracy, our resolved raw auctions** (N=682) | 351 priced, mean 1.319, median 0.720 | 364 priced, mean 1.261, median 0.732 |
| Same 350 rows both price | mean 1.320, median 0.718 | mean 1.276, median 0.732 (15 better, 17 worse, 318 unchanged) |
| Rescued subset (14 auctions) | refused | mean 0.888, median 0.738, 57% in band |
| **Accuracy, leave-one-out ledger auction sales** (N=2,581) | 1,417 priced, median 1.525 | 1,444 priced, median 1.538 |
| Rescued subset (30 sales) | refused | mean 7.652, median 2.172, 33% in band |
| **Pollution:** unique ledger rows the merge would add | n/a | 723: 35 dropped by guards (1 stamp, 27 unsafe tier, 5 cross-title, 2 store variant), 52 of the 688 kept (7.6%) are year-mismatched wrong books |

- **Reconstruction check:** the live-only reconstruction refuses 217 of the 229 stored refusals. It prices the other 12 because the ledger holds more comps than the refusing fetch returned.
- **Rescued books:** Batman #227 at 5.0 and 6.0, the ticket's motivating case ($500–825 and $775–1,200), X-Men #54, #60, and #73, Detective Comics #403, Uncanny X-Men #238, Invincible #68, and World's Finest #203.
- **Pollution baseline:** the same year-mismatch check flags 374 of 5,565 kept live-window rows (6.7%). The merge adds wrong books at the rate the live pool already carries them (X-Men '97 in X-Men #97 pools, later series in vintage pools). It isn't a new class.

## Verdict

The merge is safe but small. It can't touch `too_sparse`, which is 55% of refusals, because those books have no older comps either. On auctions the rescued bands score in line with accepted ones (median 0.738 against 0.720). On the larger leave-one-out set they score worse (median 2.17 against 1.53). The mean gain on auctions comes from a few outliers: 15 bands improve and 17 get worse.

**Follow-on:** keep the raw path live-only and re-run this script around 2027-01 (scripts are reproducible over HTTP). By then the ledger holds 5 or more months of comps past the live window. Ship the merge only if the rescue reaches about 10% of shape refusals and the rescued subset's median Winkler/price stays within 0.05 of the accepted path on both populations. If shipped, use these parameters: graded raw ledger comps only, `graded_comp_weight` steps (1.0 to 90 days, 0.5 to 365, excluded after) multiplied into the recency weight, every exclusion stamp honored per listing, `no-variant` and `broader` tiers dropped, and current `hard_exclude` plus `graded_identity_exclude` re-applied.

## Caveats

- **Ledger as proxy:** the live-only pool is a reconstruction, not the fetch that actually ran, and it drifts by 12 of 229 on the refused rows.
- **Young ledger:** most 91–365-day rows are backfilled captures. The rescue count is a lower bound on what an older ledger gives.
- **Known-by timing:** accuracy pools filter on sale date, not on `first_seen_at`, so they include comps the ledger learned about later. The rescue pools do filter on `first_seen_at`.
- **Wrong-book check:** the year heuristic misses wrong books with no year in the title. Treat 7.6% as a floor on both sides.
- **No near-duplicate rule:** BUI-1005's 7-day same-price drop is approximate (BUI-1020), so it is omitted. Product-id and value dedupe only.
