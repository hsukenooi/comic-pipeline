# Photo-grading accuracy on labeled sold comps (BUI-1006)

**Date:** 2026-09-28. **Source:** `docs/audit/2026-09-28-photo-grading-accuracy.py` (sample, fetch, batches, metrics) against the live comics DB opened read-only, plus 15 Haiku grader subagents run through Claude Code's Agent tool.

**Decision: NO-GO.** Haiku grades 43% of books within ±1.0 from one front-cover photo (raw 41%, slab 46%), against the 80% bar. It compresses every book toward the middle of the scale (predictions span 3.0 to 9.4, standard deviation 1.61 against a true 2.53), so it over-grades worn books by about +3 points and under-grades 9.x books by about 1.5. Its own confidence separates better from worse, but even its high-confidence calls reach only 51%.

## Method

- **Sample:** comps with no `excluded_code`, newest `sold_date` first, stratified into six grade bands. Raw: 100 books with a seller-stated grade (17/17/17/17/16/16 across 0.5–3.5, 4.0–5.5, 6.0–7.5, 8.0–8.5, 9.0–9.2, 9.4–10.0), titles matching a lot/set/run pattern or naming CGC/CBCS/slab dropped. Slab: 50 CGC/CBCS universal-label books from 2.0 to 9.8 (8 or 9 per band). Sold dates run from 2026-08-14 to 2026-09-25 (raw) and 2026-09-04 to 2026-09-23 (slab).
- **Image:** the first listing image only, from the Browse API (`get_item_by_legacy_id`, the call `grade_photos.download_listing` makes), downscaled to 768 px on the long edge at JPEG quality 80. Slabs lose the top 27% of the frame, which removes the certifier label and the grade.
- **Grader:** Claude Haiku as a `general-purpose` subagent, 10 images per agent, 15 agents in parallel. The prompt (in the script's `GRADER_PROMPT`) calls it a raw-comic grader, gives a five-line 9.4/8.0/6.0/4.0/2.0 rubric, and asks for one JSON line per image with a grade, a confidence, and defects. 11 of 15 agents returned only a summary on the first pass. Each was asked once to resend its existing grades as JSON, without re-reading the images.
- **Optional embedding and regression model:** skipped. Training and validating one on the Mac Mini would take more than the hour the ticket allows.

## Fetch failures

**0 of 150** fetches failed: every sampled listing, all sold in the last 45 days, still returned its photos. A separate 60-listing probe across the ledger shows the ~90-day expiry is real but not a clean cutoff:

| Sold | Photos returned |
|---|---|
| Last 60 days | 15 of 15 |
| 61–120 days | 11 of 15 |
| 121–240 days | 0 of 15 |
| Older than 240 days | 11 of 15 |

Why some older listings still serve photos wasn't investigated. For lazy grading, a comp is gradeable while it is recent; a four-month-old comp likely is not.

## Leakage controls

- **File names:** random 8-hex ids. The id-to-comp key lives only in the scratch `key.json`. The prompt holds no title, item id, or grade.
- **Label crop:** all 50 slab crops were checked by eye on contact sheets: no label, grade digits, or certifier text survived. One CGC-studio scan and one date-stamp overlay remain, neither carrying a grade.
- **Raw images:** all 100 checked the same way. One raw photo showed the book's bag card with a handwritten "5.5" (the seller's grade); it was dropped and refilled from the same band.
- **Known shifts:** case glare on slabs (one grader noted it limited detail), and repeated keys (five FF #49 slabs, five ASM #194 slabs), which could let a grader anchor across a batch. Mixing raw and slab images in random batches limits that.

## Results

| Cut | n | MAE | Within ±1.0 | Within ±0.5 | Bias (pred − true) | Pearson r |
|---|---|---|---|---|---|---|
| Raw | 100 | 1.78 | 41% | 29% | +0.18 | 0.47 |
| Slab | 50 | 1.83 | 46% | 34% | +0.72 | 0.38 |
| Combined | 150 | 1.79 | 43% | 31% | +0.36 | 0.44 |

For comparison, guessing the sample median for every book lands 20% (raw) and 30% (slab) within ±1.0. The stratified sample over-weights low grades. Reweighting raw to the ledger's own grade mix raises it to 46%, still far below 80%.

| True grade | Raw n | Raw MAE | Raw ±1.0 | Raw bias | Slab n | Slab MAE | Slab ±1.0 | Slab bias |
|---|---|---|---|---|---|---|---|---|
| ≤ 3.5 | 17 | 3.35 | 0% | +3.35 | 8 | 3.55 | 12% | +3.55 |
| 4.0–5.5 | 17 | 1.89 | 35% | +1.72 | 9 | 3.09 | 11% | +3.09 |
| 6.0–7.5 | 17 | 1.26 | 65% | −0.44 | 9 | 0.83 | 67% | +0.28 |
| 8.0–8.5 | 17 | 1.13 | 53% | −0.75 | 8 | 0.59 | 88% | −0.16 |
| 9.0–9.4 (raw 9.0–9.2) | 16 | 1.69 | 38% | −1.66 | 8 | 1.89 | 50% | −1.69 |
| 9.4+ (slab 9.6+) | 16 | 1.30 | 56% | −1.30 | 8 | 1.00 | 50% | −1.00 |

The slab rows are the clean test, because the labels are exact. They show the same shape as raw, so seller optimism doesn't explain the misses: the grader can't see what puts a book at 2.0 or at 9.6 in one 768 px front photo. A three-tier check (under 5.0, 5.0–8.5, 9.0 and up) agrees only 47% (raw) and 48% (slab) of the time.

### Does confidence predict the misses?

Partly. Haiku never said "low", so the signal is high against medium:

| Confidence | n | MAE | Within ±1.0 | Within ±0.5 |
|---|---|---|---|---|
| High | 102 | 1.53 | 51% | 39% |
| Medium | 48 | 2.36 | 25% | 12% |

Medium confidence doubles the miss rate, so it is a usable gate. But the high-confidence subset still sits 29 points below the bar, so the gate can't rescue the method.

## Token usage

The harness reported about 900,000 subagent tokens for the 15 first-pass graders (about 60,000 each), plus 11 short resend turns. Most of that is the `general-purpose` agent's own system prompt and tool definitions, largely cache reads. The image payload itself is about 150,000 to 180,000 input tokens (150 images at 1,000 to 1,200 each), within the ticket's 250,000 budget. The harness doesn't split cached from uncached input, so treat 900,000 as an upper bound.

## Go/no-go

**NO-GO: 43% within ±1.0 (raw 41%, slab 46%) against an 80% bar.** Lazy grading in comic-fmv should not start. Could a cheap fix close the gap?

- A second image or a rubric tweak probably won't. The errors are systematic compression, not noise. Low-grade books need spine, back, and interior evidence that a front photo lacks, and the model plainly doesn't use the full scale.
- A stronger model on medium-confidence rows only touches those 48 rows. Even if it got every one right, the total would be 100 of 150 (67%), because the high-confidence rows stay at 51%.
- A calibration layer (a remap from Haiku's grade to the true grade) might recover some of the band bias, but it can't create the resolution a Pearson r of 0.44 lacks.

Treat photo grading as closed for comp labeling unless a new approach (a stronger model on several photos per comp, or a trained regression head) is measured first.

## Out of scope

- **Photo availability:** comps are fetchable for at least 60 days and gone by about 120 days, with a long tail of older listings still served. Any design that grades comps later has to grade them soon after they sell.
- **Graders' JSON discipline:** 11 of 15 Haiku agents summarized instead of returning their JSON in the hand-back. A grading harness would need a structured-output check and a retry, not just a prompt instruction.
