---
date: 2026-06-03
topic: comic-grader-accuracy-efficiency
linear: BUI-51
---

# Comic Photo Grader: Accuracy and Efficiency

## Problem Frame

A June 2026 qualitycomix seller scan graded 8 ungraded listings and surfaced three failures in `.claude/commands/comic/grade.md` (BUI-51):

1. **Printed credits read as signatures.** All 3 graders flagged the printed artist/writer credits on X-Men #24 (1993) as a handwritten signature and capped the grade at 9.0. Early-'90s Marvel/DC covers routinely print creator names into the cover art.
2. **Two photos is a hard ceiling, ignored.** Every listing had exactly 2 photos. Graders noted they couldn't assess staples, spine splits, interiors, or corners — then returned a single point-precise grade anyway. FMV downstream treats that 2-photo guess identically to a 6-photo assessment.
3. **24 agents for 8 comics.** The skill always fans out 3 graders per comic regardless of value or certainty. Most books in a seller scan are cheap; the rigor is wasted on them.

The user also asked for *other* ideas beyond the three, across both accuracy and token efficiency.

### Organizing principle: value-gated balance

When accuracy and efficiency conflict on a given comic, **the auction's dollar value decides**. Cheap lots take the fast, coarse path (fewer agents, looser grade); expensive lots get full 3-agent rigor and tight grades. One knob — auction value — governs both the fan-out count (Problem 3) and how hard we push for grade precision. This is the spine the requirements below hang on.

### External grounding (CGC, 2026)

A targeted research pass confirmed both contested premises with primary sources:

- **A1 is how grading actually works, not a heuristic.** CGC's official Grader Notes taxonomy lists *Writing* and *Name Written on Cover* as **substance defects** (post-print additions to the paper). Printed cover elements — including printed creator credits and facsimile signatures reproduced in the printing — appear **nowhere** in the defect taxonomy. If a mark is in the print layer, it has no defect classification and cannot affect grade.
- **The 2-photo ceiling is structural, not laziness.** The defects that separate high grades — non-color-breaking spine stress lines, canvassing/cockling, finger bends, centerfold attachment, staple rust migration, page brittleness, cover fade — are each **un-assessable without a specific view** (raking light, interior spread, page edge, front+back comparison). Two cover photos structurally cannot see them, so a confident high grade from 2 photos is unsupportable by construction.

Sources captured in [Dependencies / Reference](#dependencies--reference).

## Scope

All ideas are in scope, sequenced into two phases so the cheap, high-certainty wins ship without waiting on the coupling-heavy ones.

- **Phase 1 (closes BUI-51):** A1 print-layer rule · B1+B2 coverage-based confidence that FMV consumes · C1 value-gated escalation.
- **Phase 2 (the "other ideas" / stretch):** A2 seller-grade-as-prior · A3 structured defect extraction · C2 decision-sensitivity gating · C3 batch cheap lots · C4 triage pre-pass.

## Requirements

### A — Accuracy: print-layer rule (Phase 1) — closes BUI-51 #1

- **R1.** Add a **print-layer rule** to the grader prompt, generalizing beyond signatures: anything in the original print layer — printed creator credits, facsimile/printed signatures, barcodes, price boxes, cover-art text, logos — is **never** a grading defect. Only marks physically added to the paper *after* printing (pen, marker, pencil, post-print stamps, stickers) count.
- **R2.** Give the grader CGC's discriminating test so it can tell the two apart from a photo: a print-layer mark is identical across all copies, shows no paper indentation/pressure groove, sits flush with the surrounding surface, and its ink color and 45°-reflection match adjacent printed text; an authentic autograph shows a pressure groove, variable ink density, darker stroke crossings, and reflection distinct from the printed ink.
- **R3.** Align grade impact with CGC labels: a printed/facsimile signature has **zero** grade effect; an authentic post-print autograph is a *Writing* / *Name Written on Cover* substance defect that reduces grade (unless witnessed Signature Series / authenticated — out of scope for raw photo grading, but the grader should not assume a signature is authentic without post-print evidence per R2).
- **R4.** When the grader cannot tell from the photo whether a mark is print-layer or post-print, it must **default to print-layer (no cap)** and flag the uncertainty in PHOTO LIMITATIONS — the BUI-51 failure was false *positives* (real print mis-capped), so the safe default is not to cap.

### B — Confidence: coverage-based, FMV-consumed (Phase 1) — closes BUI-51 #2

- **R5.** Replace the image-*count* heuristic with a **coverage assessment**. The grader maps which condition-bearing views are actually present — front cover, back cover, spine (straight + raking), all four corners, staples close-up, interior/centerfold spread, page edge — not merely how many images exist. (Two photos of front+back cover are far more gradeable than two of the front.)
- **R6.** Derive a **confidence level** (`HIGH` / `MEDIUM` / `LOW`) from coverage, anchored to the view→defect mapping: missing the views that reveal grade-separating defects (raking-light spine, interior, page edge) **caps confidence** regardless of how clean the visible surfaces look. A 2-cover-photo listing cannot exceed `MEDIUM-LOW`.
- **R7.** Emit confidence as a **first-class field** in the grader output (alongside GRADE), plus an optional grade **range** (e.g. `5.0–6.0 VG/FN–FN`) when coverage is too thin for a point estimate. The point grade remains the primary number; the range and confidence qualify it.
- **R8.** **FMV consumes confidence** — this is the load-bearing requirement. `/comic:fmv` (and the snipe-add bid cap) must apply a haircut to the max bid when grader confidence is `LOW`, so a thinly-photographed guess does not bid as aggressively as a well-photographed assessment. Confidence dying in the grader's text output (today's behavior) is the actual BUI-51 #2 bug; the fix lives in the handoff.
- **R9.** Confidence must survive the consensus step: when graders disagree because of *missing views* (epistemic uncertainty) rather than a *named physical defect*, widen the range / lower confidence instead of silently taking the median.

### C — Efficiency: value-gated escalation (Phase 1) — closes BUI-51 #3

- **R10.** Default to **1 grader first**, not 3. Escalate to the full 3-agent panel only when a gate trips.
- **R11.** Escalation gates: fan out to 3 when **(a)** the first grade sits near a decision boundary (ambiguous, or close to a grade-capping threshold), **or (b)** the auction's value clears a threshold (a high-value key always gets the full panel). Sub-threshold, unambiguous books stay at 1 grader.
- **R12.** The skill must **state what it did** per comic — 1 grader vs 3, and why it did/didn't escalate — so the user can see where rigor was spent. No silent coverage caps.
- **R13.** Escalation requires the auction's current price as grader input, which it does not have today (grading runs before FMV). Wiring price/value into the grade step is a prerequisite for C1 (and for C2). *(Mechanism only; wiring detail is for planning.)*

### D — Stretch set (Phase 2 — the "other ideas")

- **R14. Seller-grade-as-prior (A2).** The skill already retrieves the seller's title grade but treats it as a passive note. Instead hand it to the grader as an **anchor it must argue away from**: if the assessment lands 2+ grades off the seller's claim, it must justify the gap with a named defect. Catches both wishful sellers and over-harsh graders cheaply.
- **R15. Structured defect extraction (A3).** Have the (first) grader **enumerate defects per zone before naming a number**, then map defects → grade. Reduces vibe-grading, makes the cap logic auditable, and makes R1 land harder by forcing an explicit "is this mark in the print layer?" step per observed mark.
- **R16. Decision-sensitivity gating (C2).** The deeper version of C1: precision only matters where it changes the buy. If FMV reaches the same buy/no-buy and bid-cap decision at *both* ends of the plausible grade range, stop grading — the exact number is irrelevant. Highest token upside, but couples grading to FMV in the loop; sequenced last for that reason.
- **R17. Batch cheap lots (C3).** For sub-threshold books, grade several in a **single agent context** instead of 3-per-book. Trades cross-agent independence (low value at that price) for a large token cut on the long tail of a seller scan.
- **R18. Triage pre-pass (C4).** A cheap first agent answers "is this even worth grading?" (obvious beater, blurry/insufficient photos, not actually on the wish list) and kills no-hopers before any expensive grading fans out.

## Non-Goals

- Not changing the underlying Heritage/Overstreet numeric scale or per-grade defect criteria already embedded in `grade.md` — they're solid and well-sourced.
- Not handling witnessed CGC Signature Series / authenticated-autograph workflows — irrelevant to raw photo grading for bidding.
- Not building a general "grading guidelines" knowledge base — the research confirmed the embedded criteria already cover the scale; only the two narrow gaps (print-layer, photo coverage) needed external grounding.

## Success Criteria

- The X-Men #24 (1993) class of failure no longer caps: re-running the qualitycomix scan, printed creator credits produce **no** signature cap and no grade reduction.
- A 2-photo listing never returns `HIGH` confidence, and its thin coverage measurably reduces the downstream bid cap versus an otherwise-identical 6-photo listing.
- On a representative seller scan, agent count drops by roughly **~60%** (BUI-51's target) versus always-3, with **no** escalation skipped on a book whose grade is genuinely ambiguous or whose value clears the threshold.
- For every comic, the output states how many graders ran and why — a reviewer can audit where rigor was spent.

## Dependencies / Reference

- **Prerequisite:** auction price/value must reach the grade step (R13) — it currently runs before FMV. Enables C1 and C2.
- **Downstream coupling:** R8 requires a change in `/comic:fmv` (and the snipe-add bid-cap computation) to consume the new confidence field. The grader change alone is inert without it.
- **Research sources (CGC, 2026):**
  - CGC Grader Notes — Substance Defects: https://www.cgcgrading.com/en-US/resources/comics-grader-notes-guide/substance (Writing / Name Written on Cover are substance defects; printed elements absent from taxonomy)
  - CGC Grader Notes — Distortion: https://www.cgcgrading.com/en-US/resources/comics-grader-notes-guide/distortion (cockling/canvassing/pebbling/rippling need raking light)
  - CGC Grader Notes — Crease: https://www.cgcgrading.com/en-US/resources/comics-grader-notes-guide/crease (stress lines, finger bends require raking light)
  - CGC Label Descriptions: https://www.cgccomics.com/grading/labels/ (blue universal / yellow Signature Series / green qualified)
  - Tamino Autographs — facsimile vs. authentic signature detection: https://www.taminoautographs.com/blogs/autograph-blog/facsimile-autograph-how-to-detect-printed-signatures
  - Appraisily Comic Grading Guide 2026: https://appraisily.com/articles/comic-book-grading-guide-cgc-basics-condition-checklist/ (8–10 view photo checklist)
