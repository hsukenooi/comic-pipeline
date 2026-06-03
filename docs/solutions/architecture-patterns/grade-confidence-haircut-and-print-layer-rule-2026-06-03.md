---
title: Comic grader — print-layer rule + confidence-haircut-on-the-envelope
date: 2026-06-03
category: architecture-patterns
module: comic grader / FMV bid pipeline
problem_type: architecture_pattern
component: tooling
severity: medium
related_components:
  - apps/fmv
  - .claude/commands/comic
applies_when:
  - Editing the photo grader (.claude/commands/comic/grade.md) or its accuracy rules
  - Threading a new signal from a grader/LLM skill into the comic-fmv CLI or the bid cap
  - Deciding how grade or photo quality should affect the max bid
  - Adding any LLM-authored field to the comic-fmv --batch envelope
tags:
  - comic-grader
  - grade-confidence
  - bid-haircut
  - print-layer
  - photo-grading
  - value-gated-fanout
  - batch-envelope
---

# Comic grader — print-layer rule + confidence-haircut-on-the-envelope

Two reusable patterns from BUI-51 (PR #40). They live in the same feature and reference each other, but solve different problems: one is a **grading-accuracy heuristic**, the other is a **grade→fmv→snipe integration shape**. This is the first `docs/solutions/` entry for grader-prompt design and the grader→bid handoff — see also [fmv-bid-linkage-gap](../fmv-bid-linkage-gap-2026-05-23.md) for the linkage chain these ride on.

## Context

A June 2026 qualitycomix seller scan graded 8 ungraded listings and exposed three grader failures:

1. **Printed cover credits were read as handwritten signatures and capped the grade.** Early-'90s Marvel/DC covers print the artist/writer credits *into the cover art*; all three graders on X-Men #24 (1993) treated the printed credit as a signature and held the grade at 9.0.
2. **Two photos but point-precise grades.** Every listing had exactly 2 photos. Graders noted they couldn't assess staples/spine/interior — then returned a single number anyway, and FMV bid on it as if it were a 6-photo assessment. The confidence *died in the grader's text output*.
3. **24 agents for 8 comics.** Three graders fired per comic regardless of value.

## Guidance

### Pattern 1 — The print-layer rule (grading accuracy)

State the rule in terms of the **print layer**, not signatures specifically:

> Anything reproduced in the original printing — creator credits, printed/facsimile signatures, barcodes, price boxes, cover text, logos — is part of the cover art and **never** affects the grade. Only marks physically **added after printing** (pen, marker, pencil, post-print stamps, stickers) can be defects.

This is grounded in CGC's actual defect taxonomy: *Writing* and *Name Written on Cover* are **substance defects** (post-print additions to the paper); printed cover elements appear nowhere in the taxonomy.

Give the grader the discriminating test so it can tell them apart from a photo:

- **Print-layer (not a defect):** identical on every copy; no paper indentation/pressure groove; ink flush with the surface; ink color and 45°-reflection match the surrounding printed text.
- **Post-print autograph (a defect):** visible pressure groove; variable ink density; darker where strokes overlap; reflection distinct from the printed ink.

**Key decision — when ambiguous, DEFAULT TO PRINT-LAYER (do not cap), and flag the uncertainty.** The failure mode was false *positives* (a real printed credit mis-capped), so the safe direction is *not* to cap.

### Pattern 2 — Confidence rides the envelope and haircuts the bid (integration)

The fix for "2 photos but point-precise grades" is not in the grade number — it's in the **handoff**. Three moves:

1. **Derive confidence from photo *coverage*, not image count.** Which *views* are present (front / back / spine-straight / spine-raking / corners / staples / interior-centerfold / page-edge) determines what's assessable. The defects that separate high grades — non-color-breaking spine stress (needs raking light), centerfold attachment (needs interior), paper brittleness (needs page-edge) — are structurally invisible from cover photos. **A 2-cover-photo lot caps at MEDIUM-LOW no matter how clean it looks.**
2. **Emit the confidence on the `comic-fmv --batch` envelope** as an optional `grade_confidence` field (`high|medium|medium-low|low`), alongside `grade`.
3. **Haircut the bid cap in the CLI.** `apps/fmv/src/fmv_math.py:bid_factor()` lowers the standard `0.80 × fmv_high` multiplier to `0.70` (MEDIUM-LOW) or `0.60` (LOW), taking the **more conservative** of `grade_confidence` (photo coverage) and the pre-existing `fmv_confidence` (comp-pool quality). The two are **orthogonal axes** — keep them separate; do not average them into one number.

Companion lever — **value-gated fan-out** (`grade.md` Step 2): run 1 grader first, escalate to a 3-grader panel only when auction value ≥ a threshold or the grade is boundary-ambiguous. Fan-out is driven by **price**; confidence is driven by **coverage**. They are independent knobs — validated live: a $2.22 book → 1 grader, a $241 key → 3 graders, *both still MEDIUM-LOW* because both had only 2 photos.

**Key decisions:**
- **Confidence is NOT persisted to a DB column.** It stays in the handoff envelope. The cache-reuse path applies the haircut at *read* time (`_fmv_from_db_row` combines the request's `grade_confidence` with the row's stored `fmv_confidence`), so a cache hit on a freshly low-confidence grade still bids low.
- **Absent `grade_confidence` → unchanged 0.80** (back-compat for seller-stated grades and manual runs). Presence is the opt-in switch for the haircut.
- **Untrusted input is hardened.** `grade_confidence` is LLM-authored via JSON: a non-string, blank, or typo'd value must neither crash nor fail open. Blank → treated as absent; unrecognized → conservative LOW (bid less when unsure); never an `AttributeError`.

## Why This Matters

- The print-layer rule kills a whole *class* of false caps (credits, facsimile signatures, barcodes, price boxes), not just the one X-Men #24 case.
- A grade is only as useful as its confidence **downstream**. If confidence dies in the grader's prose, a thinly-photographed mystery book bids exactly like a confirmed NM — which is the most expensive way to be wrong on a high-value key. The haircut is the discipline that stops you chasing a $241 auction on a grade two cover photos can't support.
- Per [fmv-bid-linkage-gap](../fmv-bid-linkage-gap-2026-05-23.md): when you add a field to the `--batch` envelope, define the field **and** its consumer (the parsing/haircut logic) in the **same change**, or the handoff fails silently — a skill emits the field and the CLI quietly ignores it.

## When to Apply

- Editing `grade.md` accuracy rules → reach for the print-layer framing before adding a signature-specific special case.
- Threading any grader/LLM signal into `comic-fmv` → ride the `--batch` envelope, define field + consumer together, and treat the value as untrusted.
- Letting grade/photo quality affect the bid → haircut the multiplier by the conservative min of the relevant confidence axes; keep orthogonal axes separate.

## Examples

**Print-layer — before vs after (X-Men #24, 1993):**

```
Before: GRADE 9.0  (capped — "handwritten signature" on cover)   ← false cap
After:  GRADE 9.0  GRADE RANGE 8.0–9.4  GRADE CAP: none
        SIGNATURE/CREDIT CHECK: printed/facsimile — no effect
```

The point grade is the same, but the *ceiling* is gone: the range now opens **upward to 9.4 (NM)**, which the phantom signature cap forbade.

**The haircut combine (conservative min of two orthogonal axes):**

```python
# apps/fmv/src/fmv_math.py
def bid_factor(fmv_confidence, grade_confidence):
    if grade_confidence is None:            # absent → back-compat, no haircut
        return 0.80
    # ...normalize untrusted grade_confidence: blank→absent, unknown→LOW...
    combined = min(rank(fmv_confidence), rank(grade_confidence))
    if combined <= LOW:        return 0.60
    if combined == MEDIUM_LOW: return 0.70
    return 0.80
```

**Gotcha — preserve all four confidence levels end-to-end.** Don't collapse the grader's `MEDIUM-LOW` into `low` on the handoff (an easy over-aggregation). They haircut differently — `0.70` vs `0.60` — so map `HIGH→high`, `MEDIUM→medium`, `MEDIUM-LOW→medium-low`, `LOW→low` through `grade.md` → `buy.md` → `comic-fmv`, or the 0.70 tier is unreachable from the orchestrated path.

## Related

- [fmv-bid-linkage-gap](../fmv-bid-linkage-gap-2026-05-23.md) — the `bids → bid_fmvs → fmv → comics` chain and the `0.80×fmv_high` bid path this haircut modifies; also the "define CLI flag + consumer in the same change or it fails silently" rule.
- [stub-fmv-null-after-extract-comics](../database-issues/stub-fmv-null-after-extract-comics-2026-05-23.md) — the `--batch` JSON shape and the existing `fmv_confidence` field that `grade_confidence` now rides alongside.
- Plan: `docs/plans/2026-06-03-001-feat-comic-grader-accuracy-efficiency-plan.md` · Brainstorm: `docs/brainstorms/2026-06-03-comic-grader-accuracy-efficiency-requirements.md` · Linear: BUI-51 · PR: #40
