# Multi-photo, native-resolution grader with selective coverage (BUI-1013)

**Date:** 2026-09-29. **Source:** `docs/audit/2026-09-29-multiphoto-grader.py` (sample, fetch, prep, embed, fit, metrics, sheet) against the live comics DB opened read-only, and the BUI-1012 holdout cache (150 comps, every photo at native resolution).

**Decision: double NO-GO.**

- **Full grader: NO-GO.** The CV-selected configuration (every photo pooled plus front-cover tiles) puts 59% of the 150 holdout books within ±1.0 (raw 57%, slab 62%), against the 80% bar. No arm reaches 62%.
- **Selective grader: NO-GO.** At 40% coverage the CV-selected configuration scores 77% within ±1.0, against the 85% bar. The best arm on the holdout reaches 83% (whole-photo pooling), which is still short. A confidence threshold set on training CV lands 20% to 27% coverage at 93% to 97%, which is below the 40% coverage floor.
- **The new inputs didn't carry a gain.** On training CV the arms tie: front photo only 58.7% band-balanced, all photos pooled 57.8%, tiles 55.3%, both 58.8%. Tiles don't help even where the book crop worked cleanly. The spine, corners, and back cover don't reach the grade through a frozen CLIP embedding.

Per the ticket, this closes photo grading of comps on these inputs (one front photo, every photo, or front tiles) with a frozen encoder and a CORAL head.

## Method

- **Training candidates:** BUI-1011's `cmd_sample` rules (no `excluded_code`, the same lot and slab-title filters, CGC/CBCS universal slabs, deduped on `product_id`), limited to sales at most 90 days old (the Browse photo window). The 150 holdout ids and BUI-1006's rejected id are excluded. **Every comp sharing a `comic_id` with a holdout comp is also excluded (752 comps), so the strict same-issue bound is the headline here, not a variant.** That leaves 1,529 raw and 334 slab candidates.
- **Fetch:** one Browse call per listing returns every image URL, and all images are downloaded at native resolution (almost all are eBay's 1600 px `s-l1600`). The order is slabs first, then raw newest-first, capped at 2,500 calls.
- **Label leakage in extra photos:** slab listings carry label close-ups with the grade in large print, so every photo gets checked. Slab photos lose the top 27% (BUI-1006's verified label crop). Apple Vision OCR then reads the rest, and a photo whose text still carries a decimal grade token, a certifier word (CGC, CBCS, universal, grade, pages), a grade adjective, or a grade abbreviation (NM, VF, VG) is dropped. The one exception is the front photo, where the matching text boxes are blacked out instead, because the tiles need that photo. The rule is label-free and runs the same way on training and holdout.
- **Features (OpenCLIP ViT-L-14 `laion2b_s32b_b82k`, frozen, MPS, L2-normalised; a 0/1 slab feature is appended):**
  - **front:** the front photo alone, which is the BUI-1011 input on this training set.
  - **whole:** mean and max pooling of every kept photo.
  - **tiles:** from the native front photo, four corner squares (a quarter of the book's width) plus a spine strip (14% of the width, split into three segments whose embeddings are averaged). Each tile is squashed to 224 px.
  - **both:** whole and tiles together.
- **Book-in-frame crop:** a largest-rectangle heuristic. It takes the largest convex four-sided contour (OpenCV Canny plus `approxPolyDP`) covering 20% to 97% of the frame with a book-like aspect. If none qualifies, it tries a border-colour foreground box, and if that fails too it uses the whole frame.
- **Head and selection:** BUI-1011's CORAL ordinal head (22 thresholds on the 23-rung ladder). Weight decay (0.01 to 100) and class balancing are picked per arm by grouped 5-fold `StratifiedGroupKFold` on training only (groups `comic_id`, band-balanced within ±1.0). The pick is refit on all training rows and scores the holdout once. The grid was widened below 0.1 after the first run's `both` pick sat on the edge, using CV numbers only, before any holdout metric was read.
- **Confidence:** the CORAL probability mass within ±1.0 of the predicted rung.

## Training set obtained

| | Raw | Slab |
|---|---|---|
| Candidates (≤ 90 days, strict) | 1,529 | 334 |
| Fetched, id and title matched | 715 | 334 |
| After leakage filters | 704 | 325 |
| By band, ≤ 3.5 / 4.0–5.5 / 6.0–7.5 / 8.0–8.5 / 9.0–9.2 (9.4) / 9.4 (9.6)+ | 40 / 115 / 139 / 95 / 121 / 194 | 20 / 57 / 77 / 27 / 43 / 96 |

The 1,029 training listings carry 4,415 photos (a mean of 4.2). Of those, 571 were dropped for leaked text (raw 9%, slab 24%), and 16 fronts had boxes masked. Twelve recurring seller banners (the same dHash in 3 or more listings) are ignored everywhere.

**Fetch failures: 1,450 of 2,500 (58%), but only 2 were about the listing** (one 404, one 400). The other 1,448 were Browse **429s: the app's daily call quota ran out** after about 1,050 calls, following BUI-1011 and BUI-1012's calls earlier the same day. Every slab candidate was fetched. The quota cut off 812 raw candidates aged 76 to 90 days, and every same-issue comp, so no loose (same-issue-included) sensitivity run exists. The script now stops spending calls after the first 429 and retries 429s on the next run. Among listings that answered, photo expiry cost almost nothing: 2 failures up to 90 days. BUI-1011's learning curve was flat from 580 to 2,300 images, so the smaller set (1,029 against BUI-1011's 2,798) shouldn't cap the result, but it is one of the limits listed below.

**Listing identity:** all 1,050 responses had `legacyItemId` equal to the requested id, and all titles matched the ledger once whitespace was normalized (43 differed only in trailing or doubled spaces).

## Leakage controls

| Control | Training listings dropped |
|---|---|
| Shares a `comic_id` with a holdout comp (at sample) | 752 candidates, plus 1 fetched |
| `product_id` in holdout | 0 |
| Any photo's raw SHA-256 equals any holdout photo | 3 |
| Any kept photo within 6 bits (64-bit dHash) of a holdout photo | 4 |
| Front embedding cosine ≥ 0.95 to a holdout front | 0 |
| Front photo byte-identical to another training listing | 13 |

**Label check:** 40 randomly drawn kept non-front slab photos from the holdout were checked by eye, and none showed a grade, a certifier grade line, or a label's front. The one label remnant was a label's back, with creator notes and no grade. The holdout fronts needed no masking because the 27% crop cleared every label.

## Crop failure rate

The heuristic took the four-sided contour on 881 of 1,200 fronts, a border-colour box on 15, and the whole frame on 304 (a 25% fallback, 42% of slabs, where the case edges defeat it). The whole frame is usually right, because the book fills most eBay fronts. **Manual check: 4 of 40 crops (10%) put the box off the book's corners** (20 holdout, 20 training, drawn at random): a slab among toys, a book on a wooden floor, a slab box that runs onto the table, and a small slab in a wide frame. Separately, **on most slab fronts the top corner tiles show the book's upper edge, not its corners**, because the label crop removes the top 27% of the frame.

The crop failures don't explain the tile result. On the 563 cleanly cropped raw training listings ("quad"), tiles score 63% within ±1.0 on CV against 62% for the front photo alone.

## Results (holdout, n = 150)

| Arm (CV pick) | CV band-bal. ±1.0 | ±1.0 raw / slab / all | MAE | ±0.5 | Bias | Pearson r | QWK |
|---|---|---|---|---|---|---|---|
| front (wd 3, balanced) | 58.7% | 54% / 56% / 55% | 1.40 | 41% | −0.23 | 0.68 | 0.68 |
| whole (wd 3, balanced) | 57.8% | 64% / 54% / 61% | 1.27 | 45% | +0.04 | 0.72 | 0.71 |
| tiles (wd 3) | 55.3% | 56% / 48% / 53% | 1.35 | 36% | +0.46 | 0.73 | 0.68 |
| **both (wd 0.1), CV-selected** | **58.8%** | **57% / 62% / 59%** | **1.34** | **42%** | **+0.17** | **0.69** | **0.68** |
| BUI-1011 L-14, same-issue kept | 57% | 66% / 60% / 64% | 1.27 | 46% | −0.01 | 0.71 | 0.70 |
| BUI-1011 L-14, strict | – | 63% / 46% / 57% | – | – | – | – | – |
| Haiku (BUI-1006) | – | 41% / 46% / 43% | 1.79 | 31% | +0.36 | 0.44 | 0.40 |

The CV spread across the front, whole, and both arms (1 point) is smaller than the fold-to-fold noise in the grids (2 to 5 points between neighbouring weight decays). The holdout spread (55% to 61%) sits inside the holdout's own noise too: each book is 0.7 points.

**Paired, CV-selected arm (both):** against Haiku it fixes 44 misses and breaks 20 hits. Against BUI-1011 strict (the like-for-like bound) it fixes 21 and breaks 19. Against BUI-1011 with same-issue training kept, it fixes 19 and breaks 27.

### By band (CV-selected arm)

| True grade | Raw ±1.0 | Raw bias | Slab ±1.0 | Slab bias |
|---|---|---|---|---|
| ≤ 3.5 | 41% | +1.91 | 12% | +3.38 |
| 4.0–5.5 | 59% | +0.56 | 44% | +1.67 |
| 6.0–7.5 | 41% | −0.49 | 67% | +0.19 |
| 8.0–8.5 | 59% | −1.24 | 88% | −0.05 |
| 9.0–9.4 (raw 9.0–9.2) | 56% | −0.98 | 75% | −0.31 |
| 9.4+ (slab 9.6+) | 88% | −0.48 | 88% | −0.49 |

The shape is the same as BUI-1006 and BUI-1011: worn books are over-graded by 2 to 3 points, and within-band Pearson r is at most 0.62 and usually near zero. Adding back-cover and spine photos didn't pull the worn books down.

## Selective coverage

Within ±1.0 on the holdout, ranked by confidence within each cut:

| Arm | Cut | 25% | 40% | 50% | 75% | 100% |
|---|---|---|---|---|---|---|
| **both (CV-selected)** | raw | 80% | 70% | 64% | 60% | 57% |
| | slab | 100% | 90% | 84% | 66% | 62% |
| | **all** | **84%** | **77%** | **71%** | **62%** | **59%** |
| front | all | 95% | 82% | 76% | 63% | 55% |
| whole | all | 92% | 83% | 79% | 69% | 61% |
| tiles | all | 84% | 73% | 67% | 59% | 53% |

- **Honest operating point:** a threshold set at 40% coverage on training CV confidences selects 30 holdout books (20%) at 93% for both, 40 (27%) at 95% for front, and 33 (22%) at 97% for whole. The confidence ranks well, but the holdout is less confident than training, so the covered share falls below the 40% floor.
- **Calibration:** accuracy rises with confidence in the top two quintiles on CV and on the holdout for every arm. The bottom three quintiles are flat on CV (both: 48, 50, 57, 82, 89) and noisy on the holdout (both: 53, 47, 40, 60, 93), so the curve separates "sure" from "not sure" but doesn't rank the uncertain majority.
- **Is the curve a pool or band artifact?** Partly. The top 40% of the holdout for both is 36 raw and 24 slab, and 47 of 60 sit at a true grade of 8.0 or higher. Confidence still ranks within those bands: among the 73 holdout books at 8.0 and up, the more confident half scores 97% against 49% for the rest. Because the holdout is stratified and the ledger is skewed high, the same grader on the ledger's own mix does better. CV out-of-fold (the recent ledger's mix) reads 82% to 86% at 40% coverage across arms. The holdout reweighted to that mix reads 84% (both) to 92% (front). These are sensitivities, not the bar: they were computed after the holdout was scored, and they depend on cells of 8 to 17 books. They are also no better for the multi-photo arms than for the front photo alone.

## Wall-clock and usage

fetch 611 s (2,500 calls, 4 threads; about 1,050 answered), prep with OCR and crop 544 s, embed 913 s (4,400 photos and 8,400 tiles), fit 404 s (4 arms × 18 configurations × 5 folds). Zero model tokens.

## Limitations

- **The training set was quota-truncated to 1,029 listings** (BUI-1011 had 2,798, though it kept same-issue comps). BUI-1011's flat curve makes more data unlikely to matter, but this run didn't re-measure the curve.
- **The holdout was scored once per arm, four times in total.** The headline is the CV pick, not the best holdout arm (whole, 61% and 83%). Neither of those reaches its bar either.
- **The frozen encoder sees 224 px.** The tiles restore native resolution per region, but CLIP's pretraining isn't tuned for crease and corner wear. DINOv2 and a fine-tuned encoder weren't tried.
- **The OCR drop rule is conservative.** It over-drops pages that print "fine" or a decimal price, which can only cost signal, not leak labels.
- **Raw labels are seller-stated,** as in BUI-1011.

## Out of scope

- **The Browse daily quota is shared across every audit in a day.** BUI-1011, BUI-1012, and this ticket together exhausted it. Any future photo job inside the 90-day window has to budget for it.
