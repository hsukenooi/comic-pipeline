# Embedding plus ordinal-head grader on labeled sold comps (BUI-1011)

**Date:** 2026-09-28. **Source:** `docs/audit/2026-09-28-embedding-grader.py` (sample, fetch, embed, fit, curve, metrics) against the live comics DB opened read-only, and BUI-1006's 150 holdout comps with their exact prepped images.

**Decision: NO-GO.** The best grader (a frozen OpenCLIP ViT-L-14 plus an ordinal head, picked by cross-validation) puts 64% of the 150 holdout books within ±1.0 (raw 66%, slab 60%) against the 80% bar. It beats Haiku by 21 points (43%) and halves Haiku's bias, but it stops well short of the bar. A learning curve is flat from about 580 to 2,300 training images, so fetching more photos won't close the gap.

**What closes:** single-front-photo grading as a comp-labeling route, whether by a language model (BUI-1006) or a trained head (this ticket). No model-grade storage, freshness job, or pool admission rules should be planned on it.

## Method

- **Test set:** the 150 BUI-1006 comps (100 raw, 50 slab, balanced across six true-grade bands), scored from the same image files Haiku saw. Nothing in the pipeline reads a holdout label until `metrics`.
- **Training candidates:** every other labeled comp with no `excluded_code`, under BUI-1006's title filters and slab rule (CGC/CBCS, universal label). The candidate list is deduped on `product_id` across the ledger. Rows for the same listing under different `comic_id`s merge into one example, and the one `product_id` with two different grades is dropped. The 150 holdout ids and BUI-1006's rejected id 287549812509 are excluded. That leaves 5,282 raw and 935 slab candidates.
- **Fetch:** the Browse API (`fetch_item_with_status`) takes the first image, prepped exactly as in BUI-1006 (slab top 27% cropped, 768 px, JPEG q80). The run makes 3,500 Browse calls, all 935 slabs first and then raw newest-first. It gets 2,894 images (raw 2,103, slab 791).
- **Encoders (frozen, MPS, L2-normalised):** OpenCLIP ViT-B-32 (`laion2b_s34b_b79k`), ViT-L-14 (`laion2b_s32b_b82k`), and DINOv2 ViT-S/14 (`timm`). A 0/1 slab feature is appended.
- **Heads:** ridge regression snapped to the nearest ladder rung, and a cumulative-link (CORAL) ordinal head. The ordinal head has one linear score, 22 monotone thresholds over the 23-rung ladder (0.5 to 9.0 in 0.5 steps, then 9.2 to 10.0), and Adam weight decay. Each head was tried with and without class-balanced weights (inverse pool × band frequency).
- **Selection, done on training data only:** 5-fold `StratifiedGroupKFold` (grouped by `comic_id`, stratified by pool × band). The selection metric is band-balanced within ±1.0, the mean over the 12 pool × band cells, which matches the holdout's stratification instead of the ledger's high-skewed mix. The CV winner is refit on all training rows and scores the holdout once. Grids were widened twice after the CV winner sat on a grid edge. Both widenings used CV numbers only, before any holdout score existed.

## Leakage controls

| Control | Training rows dropped (L-14 run) |
|---|---|
| `product_id` in holdout | 0 (already excluded at sample) |
| Exact SHA-256 of prepped image equals a holdout image | 0 |
| 64-bit dHash within 6 bits of a holdout image (relist or reused photo) | 46 |
| Embedding cosine ≥ 0.95 to a holdout image | 24 (B-32 8, DINOv2 91) |
| Same raw image bytes already in training | 26 |

That leaves 2,798 training rows for L-14 (raw 2,049, slab 749). By band (raw / slab): ≤3.5 137/49, 4.0–5.5 343/140, 6.0–7.5 392/208, 8.0–8.5 335/78, 9.0–9.2(9.4) 329/114, 9.4(9.6)+ 513/150.

**Same-issue sensitivity:** `--strict-issue` also drops every training comp that shares a `comic_id` with a holdout comp (730 rows, 397 of them slabs). Combined falls to 57%, raw holds at 63%, and slab drops to 46%. The slab result depends partly on other copies of the same key issues in training. That is either legitimate (production grades comps of issues it has seen) or a shortcut on cover identity, or the same physical slab resold with a different photo. Either way the strict bound is further from 80%.

## Fetch failures

606 of 3,500 calls failed (17.3%): 602 were Browse 404 and 4 were Browse 400. No image download failed. Source: `train_failures.json`.

| Sold age | Fetched | Failed | Failure rate |
|---|---|---|---|
| ≤ 60 days | 729 | 0 | 0% |
| 61–90 days | 1,904 | 4 | 0.2% |
| 91–120 days | 261 | 524 | 67% |
| > 120 days (slab only) | 0 | 78 | 100% |

The photo window closes between 90 and 120 days, sharper than BUI-1006's 60-listing probe suggested.

## Results (holdout, n = 150)

CV-chosen configuration per encoder, plus ridge on the same embedding. Band-balanced CV is the training-only selection metric.

| Encoder, head | CV band-bal. ±1.0 | Holdout ±1.0 | MAE | ±0.5 | Bias | Pearson r | QWK |
|---|---|---|---|---|---|---|---|
| **L-14, ordinal (wd 10, balanced)** | **57%** | **64%** | **1.27** | **46%** | **−0.01** | **0.71** | **0.70** |
| L-14, ridge (α 3000) | 53% | 57% | 1.37 | 33% | +0.07 | 0.72 | 0.63 |
| DINOv2, ordinal (wd 0.1) | 53% | 59% | 1.31 | 42% | +0.51 | 0.75 | 0.69 |
| B-32, ordinal (wd 1, balanced) | 53% | 55% | 1.41 | 41% | −0.05 | 0.66 | 0.66 |
| Haiku (BUI-1006 `joined.json`) | – | 43% | 1.79 | 31% | +0.36 | 0.44 | 0.40 |
| Median guess (7.5) | – | 29% | 2.18 | – | – | – | – |

**Chosen model by pool:** raw 66% within ±1.0, MAE 1.21, QWK 0.73. Slab 60%, MAE 1.40, QWK 0.64.

**Ordinal against ridge:** the ordinal head beats ridge on every encoder, by 4 to 7 points within ±1.0 and 9 to 16 points within ±0.5. The ordinal structure helps, but only at the margin.

**Class balancing:** it won CV only for L-14 and B-32, and only by one point. On the natural-mix CV metric it always cost 1 to 5 points. It didn't help in any meaningful way.

**Paired against Haiku:** the model fixes 48 of Haiku's 86 misses and breaks 16 of Haiku's 64 hits. 38 books defeat both.

### Where the misses sit (chosen model)

| True grade | Raw ±1.0 | Raw bias | Slab ±1.0 | Slab bias |
|---|---|---|---|---|
| ≤ 3.5 | 41% | +1.97 | 50% | +2.19 |
| 4.0–5.5 | 71% | +0.21 | 56% | +0.94 |
| 6.0–7.5 | 47% | −0.04 | 44% | −1.06 |
| 8.0–8.5 | 76% | −0.89 | 75% | −0.64 |
| 9.0–9.4 (raw 9.0–9.2) | 81% | −0.67 | 75% | −0.48 |
| 9.4+ (slab 9.6+) | 81% | −0.71 | 62% | −0.94 |

The squeeze toward the middle is weaker than Haiku's (predicted standard deviation 2.14 against a true 2.53, where Haiku's was 1.61) but has the same shape. Worn books are over-graded by about 2 points, and 6.0 to 7.5 is a coin flip. Within-band Pearson r is below 0.35 in every band, and negative in most slab bands, so the model separates coarse tiers but can't rank books inside a band.

### Would more data help?

`curve` refits the chosen head on 25%, 50%, 75%, and 100% of each CV fold's training rows. Band-balanced within ±1.0 reads 55.7%, 54.3%, 54.8%, and 55.2%. The curve is flat, so the limit is the signal in one front photo, not the sample size. The photo window caps the training set at about 3,000 images at a time anyway.

## Wall-clock

sample < 1 s; fetch 743 s (3,500 calls, 4 threads); embed B-32 31 s, L-14 209 s, DINOv2 168 s; fit about 30 s per encoder (30 CV configurations × 5 folds). Zero model tokens.

## Limitations

- **Raw labels are seller-stated.** They are noisy and skew high, so raw truth caps any grader. Slab labels are exact, and slabs score no better (60%).
- **Holdout size:** each band cell holds 8 to 17 books, so a band figure moves about 6 to 12 points per book.
- **Holdout choices:** the four encoders and two heads were all scored on the holdout for this report. The headline is the CV-selected one, not the best holdout number. No holdout number beats 64%.
- **The dHash (≤ 6 bits) and cosine (≥ 0.95) filters are conservative.** They may drop a few genuinely different copies of the same cover. They can only lower training size, not inflate the score.
- **Seller style is not controlled.** Training and holdout comps sold in the same weeks, and the ledger has no seller column, so a seller whose photos and grading habits recur on both sides could lift the score. That can only inflate 64%, not hide a go.
- **Not tried:** fine-tuning the encoder, several photos per listing, and higher-resolution crops of the spine and corners. Every one of these needs more than the single front photo that the comp-labeling route depends on.
