# Grading the holdout from listing text alone (BUI-1015)

**Date:** 2026-09-29. **Source:** `docs/audit/2026-09-29-text-grader.py` (prepare, validate, jev, score) over the 150-comp BUI-1012 holdout cache. No Browse calls, no DB or ledger writes.

**Decision: NO-GO.** Listing text alone, with every stated grade redacted, puts Haiku within ±1.0 on 31% of the holdout (raw 35%, slab 22%) and Jev on 34% (raw 40%, slab 22%), against the 80% bar. The selective version fails the second bar too: Jev's best coverage-ranked slice is 45% at 25% coverage, and the 40% slice is 43%, against 85%. Text is a weaker signal than one front photo, and far weaker than the BUI-1011 and BUI-1013 embedding heads (64% and 59%).

## Method

- **Text:** eBay `title`, `conditionDescription`, and `description` (HTML stripped, capped at 2,500 characters). eBay's categorical `condition` field (Very Good, Like New, ...) is itself a grade word, so it is withheld.
- **Redaction (`redact` in the script):** numeric grades (`9.8`, `4.5`, `8/10`), CGC, CBCS, PGX plus a number, cert numbers (7 to 12 digits), slab label words (universal, qualified, signature series, certified), abbreviations (NM, VF, FN, VG, GD, FR, PR, MT, GM, and F or G in a sign or slash combo) with any `+`, `-`, or `/` combination, spelled-out grades (near mint, very fine, very good, fine, good, fair, poor, mint, like new), and "grade 8" style phrases. A second pass removes stubs of half-removed combos (`F/`, `( /M)`).
- **Independent leak check:** a separate residual regex set runs on every final text. It found 3 leaks in the first pass (`F/`, `G/`, and `( /M)` stubs that reveal the base grade letter), which I fixed and re-ran. The 5 batches whose text changed were re-graded by Haiku, and Jev was re-run on all 150. The final texts have 0 residual matches.
- **Haiku:** `general-purpose` subagent, model `haiku`, through the Agent tool, 10 batches of 15. Each agent Reads `batches/batch-NN.json` and Writes one JSON line per listing (`{rid, grade, confidence}`) to a file. `validate` checks every file: complete, well formed, grade in range, confidence in the enum. The rid-to-grade key never reached a grader.
- **Jev:** one Choice question over 9 grade bands (Poor/Fair up to Near Mint/Mint), each described by wear wording. The prediction is the argmax band's midpoint (pre-declared; the probability-weighted expectation is shown as a second row). `jev-latest`, 150 of 150 answered.
- **Selective rule (fixed before scoring):** Haiku confidence high > medium > low, ties in a fixed seeded order. Jev uses its Choice confidence. Nothing was tuned on the holdout.

## Coverage after redaction

Body means the seller condition note plus the description; the title is excluded because it holds the series and issue, not condition. A listing "keeps condition prose" when its body has at least one wear word (crease, spine, corner, gloss, stain, tear, tape, and so on).

| Pool | n | Any body words | Condition word, at least 1 | At least 3 | Median body words (at least 1) |
|---|---|---|---|---|---|
| Raw | 100 | 99 | 57 | 39 | 308 |
| Slab | 50 | 49 | 28 | 12 | 146 |
| All | 150 | 148 | 85 | 51 | 273 |

Only 17 listings (14 raw, 3 slab) have a seller condition note at all. Most bodies are shipping and store boilerplate, so "has a wear word" overstates real condition prose; the 51 with three or more is the better number. Both counts clear the 40-listing floor.

**Eyeball check:** 20 random redacted texts (seed 7, in the scratch `sample20.txt`) contain 0 stated grades, abbreviations, or numbers. One (`91cffa9c`) keeps the seller's phrase "High Grade Condition", and four more listings in the full set keep a soft "high grade" or "sharp" claim. I left these in because they are prose, not a stated grade. Slab texts also keep the word "graded", which tells the model the book is slabbed but not the grade.

## Results (n = 150)

| Model | Cut | MAE | Within ±1.0 | Within ±0.5 | Bias | r | QWK |
|---|---|---|---|---|---|---|---|
| Haiku text | Raw | 1.84 | 35% | 17% | -0.21 | 0.51 | 0.40 |
| Haiku text | Slab | 2.09 | 22% | 20% | -0.06 | 0.32 | 0.28 |
| Haiku text | All | 1.92 | 31% | 18% | -0.16 | 0.44 | 0.36 |
| Jev text (argmax) | Raw | 1.90 | 40% | 25% | +0.09 | 0.44 | 0.44 |
| Jev text (argmax) | Slab | 2.31 | 22% | 12% | +0.60 | 0.17 | 0.13 |
| Jev text (argmax) | All | 2.03 | 34% | 21% | +0.26 | 0.36 | 0.35 |
| Jev text (expectation) | All | 1.84 | 35% | 19% | +0.15 | 0.46 | 0.41 |
| Haiku photo (BUI-1006) | All | 1.79 | 43% | 31% | +0.36 | 0.44 | 0.40 |
| BUI-1011 L-14 | All | 1.27 | 64% | 46% | -0.01 | 0.71 | 0.70 |
| BUI-1013 both | All | 1.34 | 59% | 42% | +0.17 | 0.69 | 0.68 |

Text puts the middle bands right and misses both ends. Raw within ±1.0 by band (Haiku / Jev argmax):

| True grade | Haiku | Jev |
|---|---|---|
| 0.5-3.5 | 12% | 18% |
| 4.0-5.5 | 47% | 6% |
| 6.0-7.5 | 76% | 88% |
| 8.0-8.5 | 47% | 53% |
| 9.0-9.2 | 12% | 25% |
| 9.4-10.0 | 12% | 50% |

Both models guess the mid-scale for the many listings that say nothing about condition, so they over-grade the worn books by about +3 and under-grade the 9.x books by about 1.5 to 2, the same compression BUI-1006 found in photos. Slab is worse than raw (22% for both) because slab listings carry the least condition prose (median 146 words, 12 of 50 with three or more wear words).

**Where the text has content, it helps but does not clear the bar.** Within ±1.0 on the 85 listings with a wear word: Haiku 36%, Jev 41% (expectation 44%). On the 65 with none: 23% and 25%.

## Paired against the photo graders

Within ±1.0 per comp; "fixes" are text hits where the other grader missed, "breaks" the reverse.

| Text model | vs Haiku photo (64 hits) | vs BUI-1011 (96 hits) | vs BUI-1013 (88 hits) |
|---|---|---|---|
| Haiku text | fixes 18, breaks 36 | fixes 13, breaks 63 | fixes 16, breaks 58 |
| Jev text (argmax) | fixes 20, breaks 33 | fixes 15, breaks 60 | fixes 20, breaks 57 |

Jev against Haiku on text: fixes 22, breaks 17. The two text graders are close, and Jev is slightly ahead on raw and expectation MAE (1.84 against 1.92), so Jev does not lose to Haiku here as it did on identification (BUI-984). A 3-point gap on 150 books is inside noise, and both fail.

## Selective version

Within ±1.0 at 25, 40, 50, 75, and 100% of listings, ranked by confidence:

| Model | Pool | 25% | 40% | 50% | 75% | 100% |
|---|---|---|---|---|---|---|
| Haiku text | Raw | 36% | 40% | 36% | 40% | 35% |
| Haiku text | Slab | 42% | 35% | 32% | 29% | 22% |
| Haiku text | All | 34% | 37% | 33% | 35% | 31% |
| Jev text (argmax) | Raw | 40% | 42% | 42% | 39% | 40% |
| Jev text (argmax) | Slab | 42% | 25% | 20% | 21% | 22% |
| Jev text (argmax) | All | 45% | 43% | 36% | 36% | 34% |

Confidence barely separates hits from misses. Haiku's "high" tier (24 listings) hits 50%, and its "medium" and "low" tiers hit 27% each. Jev's confidence at or above 0.5 (17 listings) hits 53%, against 32% below. No slice comes near 85%. The n in the small slices is 12 to 60 books, so single percentage points are noise.

## Verdicts

- **Bar 1 (80% within ±1.0 on all comps):** fail. Haiku 31%, Jev 34%.
- **Bar 2 (85% at 40% coverage or more):** fail. Haiku 37%, Jev 43% at 40% coverage.
- **Should text join the photo features in the defect-grader ticket?** Not as a grader input on this evidence. Its signal is real in the 6.0 to 8.5 range and on the 85 listings with wear words, but 43% of listings (65 of 150) have no wear word at all and the pool that most needs help (slab) has the least. If the defect-grader ticket wants a cheap experiment, add a handful of wear-word flags (spine tick, crease, tear, stain, tape) as extra features next to the embedding, and test the increment on the same holdout. Text alone stays closed.

## Threats and caveats

- **Title carries priors.** The redacted title still holds the series, issue, and year. A model can guess that a 2022 modern book is high grade and a 1966 key is worn. That lifts these numbers, so the true text-only figure is at or below what is reported.
- **Boilerplate.** Many descriptions are store templates, some with a sentence about eBay condition categories. The wear-word count includes generic words ("pages", "clean"), so the prose coverage is an upper bound.
- **Holdout is band-stratified** (17/17/17/17/16/16 raw), so the absolute rates are not the ledger's natural mix. The comparison across graders on the same 150 is unaffected.
- **Haiku noise.** One run per listing, one model version. Two Haiku hand-back reports name grades ("VF+", "NM-") that the redacted text does not contain; the graders added those labels themselves. I checked the source text and found no such token.

## Haiku harness log

- **Format re-asks: 0.** All 10 first-pass files validated (150 of 150 listings). BUI-1006 got prose instead of JSON in 11 of 15 batches; having the agent Write a file and validating it on disk removed the failure.
- **Leak-fix re-grades: 5 batches** (01, 06, 07, 08, 09), after the residual check caught 3 half-redacted grade stubs. The first-pass grades for those 5 are discarded, not scored.
- **Tokens:** about 57,000 to 60,000 subagent tokens per batch (15 listings), so about 0.6M for the first pass and 0.3M for the 5 re-grades, mostly the general-purpose agent's own overhead. Jev: 150 calls, one question each.

## Out of scope

- **Text features next to the embedding:** the experiment suggested in the verdict is not run here.
- **Description length:** the 2,500-character cap could cut wear notes that sit late in a long description. Only the longest listings are affected.
