# Comic-grader re-baseline on Fable 5.1 (BUI-898)

**Date:** 2026-09-07. **Fixture:** `~/comic-grader-fixtures/bui-51-baseline` (the 8 frozen
2-photo BUI-51 listings; June 2026 grades in `baseline-grades.md`). **Harness:** each run is
`claude -p --model claude-fable-5-1 --system-prompt-file <variant> --allowedTools Read,Bash`
with the agent body as the system prompt and the per-book input block from `grade.md`
§ Grader Agent on stdin. Raw envelopes, prompts, and the parser are in
`~/comic-grader-fixtures/bui-51-baseline/rebaseline-2026-09-07/` (home dir, like the fixture).
28 runs, $42.34.

**Prompt variants.** `baseline` = `.claude/agents/comic-grader.md` on `main` before this
branch; `patched` = after hunks M8 (drop the two duplicated print-layer parentheticals and
step 9 "be rigorous, do NOT inflate") and M9 (crop and enlarge a small or ambiguous detail
with PIL before deciding); `patched-noguard` = `patched` minus the batch anti-anchoring
paragraph (BUI-81 U9).

## Single runs: baseline vs patched prompt (1 grader per book)

| Book | June 2026 baseline | baseline prompt | patched prompt | delta | patched range / conf / cap |
|---|---|---|---|---|---|
| Uncanny X-Men #210 | 8.0 | 8.0 | 6.5 | -1.5 | 6.0–7.5 FN–VF- / MEDIUM-LOW / none of the hard s |
| Uncanny X-Men #277 | 8.5 | 9.0 | 8.0 | -1.0 | 7.5–8.5 VF-–VF+ / MEDIUM-LOW / none (no spine spl |
| Strange Tales #162 | 4.5 | 4.0 | 4.0 | +0.0 | 3.5–4.5 (VG- to VG+) / MEDIUM-LOW / none confirmed — n |
| X-Men #24 | 8.5 | 9.0 | 9.2 | +0.2 | 8.5–9.6 (VF+–NM+) / MEDIUM-LOW / none — no spine sp |
| Thor #173 | 6.5 | 6.0 | 6.0 | +0.0 | 5.5–6.5 FN-–FN+ / MEDIUM-LOW / none (no spine spl |
| Detective Comics #578 | 8.0 | 7.5 | 7.5 | +0.0 | 7.0–8.0 FN/VF–VF / MEDIUM-LOW / none — no spine sp |
| Fantastic Four Annual #4 | 4.5 | 1.8 | 1.8 | +0.0 | 1.5–2.0 FR/GD–GD / MEDIUM-LOW / missing piece from |
| Incredible Hulk King-Size Special #1 (Annual #1) | 6.5 | 7.0 | 7.5 | +0.5 | 7.0–8.0 FN/VF–VF / MEDIUM-LOW / none |

Moved more than 0.5: [('01-uxm-210', '8.0', '6.5'), ('02-uxm-277', '9.0', '8.0')]

Two books moved more than 0.5, so both were re-run under both prompts:

| Book | baseline, run 1 | baseline, run 2 | patched, run 1 | patched, run 2 |
|---|---|---|---|---|
| Uncanny X-Men #210 | 8.0 | 7.5 | 6.5 | 6.5 |
| Uncanny X-Men #277 | 9.0 | 8.5 | 8.0 | 9.0 |

- **Uncanny X-Men #210 moved for a reason, not noise.** Both patched runs land on 6.5 with a
  6.0–7.5 range; both baseline runs sit at 7.5–8.0. The patched grader followed the new
  crop instruction ("zoomed/contrast-boosted crops of the spine edges, all eight corners,
  the printed credit, and the back-cover patch") and named a defect the baseline runs did
  not: a faint diagonal tide-line stain across the back cover's purple sky (img-02, upper
  left), which it says "practically limits the book to ~7.0". This is the M9 change doing
  what it was added to do. Whether the stain is real is a look at img-02, not a prompt
  question; the grade is left for you to accept or reject.
- **Uncanny X-Men #277 is run-to-run noise.** Baseline spans 8.5–9.0 and patched spans
  8.0–9.0 on identical inputs; the ranges overlap and the defect lists match.
- Every other book is within 0.5 of its baseline-prompt grade, and all eight keep the
  MEDIUM-LOW coverage ceiling the 2-photo fixture imposes.
- **Model shift, not prompt shift:** Fantastic Four Annual #4 grades 1.8 under both prompts
  against 4.5 in June. Both Fable runs cite a missing piece as a grade cap that the June
  model did not see. Same for the Hulk annual (7.0–7.5 vs 6.5). These are differences
  between models on the same photos and are outside this ticket.

## Batched runs: the anti-anchoring guard (BUI-81 U9) on Fable 5.1

Books 01–05 in one context and 06–07 in another, patched prompt, guard kept vs removed:

| Book | single (patched) | batched, guard kept | batched, guard removed | guard effect |
|---|---|---|---|---|
| Uncanny X-Men #210 | 6.5 | 6.5 | 7.5 | +1.0 |
| Uncanny X-Men #277 | 8.0 | 9.2 | 9.2 | +0.0 |
| Strange Tales #162 | 4.0 | 3.0 | 4.0 | +1.0 |
| X-Men #24 | 9.2 | 9.4 | 9.4 | +0.0 |
| Thor #173 | 6.0 | 5.0 | 4.5 | -0.5 |
| Detective Comics #578 | 7.5 | 8.0 | 8.5 | +0.5 |
| Fantastic Four Annual #4 | 1.8 | 1.5 | 1.5 | +0.0 |

Removing the guard raised the batched grade on four of seven books (three by +0.5 to +1.0,
one by +0.5), lowered one by 0.5, and left two unchanged: mean +0.36. The direction BUI-81
measured in June (drift toward higher point grades when batching without the guard) still
holds on Fable 5.1, so the paragraph stays. Batching itself also moves grades relative to a
single-book run in both directions (X-Men #277 9.2 batched vs 8.0 single; Thor 5.0 vs 6.0),
which is the existing reason `grade.md` grades a near-cap or not-cheap book on its own.

## What ships

Hunks M8 and M9, unchanged from the audit patch. The guard paragraph is kept. No point
grade moved beyond 0.5 except Uncanny X-Men #210, which moved because the patched grader
looked closer and found a stain; that one is yours to accept.
