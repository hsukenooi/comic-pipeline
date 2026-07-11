---
title: "Guarding grade-curve interpolation against over-bids when automating a documented pricing heuristic"
date: 2026-07-11
category: docs/solutions/best-practices
module: comic-fmv (apps/fmv)
problem_type: best_practice
component: service_object
severity: high
related_components:
  - comic-fmv
  - gixen-overlay
applies_when:
  - "Automating fmv.md §7 grade-curve interpolation or any documented pricing heuristic that was previously hand-run"
  - "Deriving a target-grade price from comps at neighbouring grades"
  - "Emitting a computed (non-direct-comp) price into a table, JSON, or cache that a bid cap reads"
tags: [fmv, interpolation, grade-curve, over-bid, bid-cap, money-path, low-confidence]
---

# Guarding grade-curve interpolation against over-bids when automating a documented pricing heuristic

## Context

fmv.md §7 grade-curve interpolation had lived as a hand-run heuristic: when a raw comic has no comps *at* its grade, you eyeball comps at neighbouring grades and read a price off the implied curve. BUI-306 (PR #141) automated it in `apps/fmv/src/fmv_math.py` (`interpolate_grade_curve`, `bucket_medians`, `monotonicity_violations`) so `comic-fmv` applies it inline. FMV is a **money path**: `max_bid = 0.80 x fmv_high`, so an interpolated number that comes out too high directly over-bids. Automating the heuristic introduced two distinct over-bid bugs that only code review caught — both now pinned by exact-arithmetic regression tests in `apps/fmv/tests/` (`test_fmv_math.py`, `test_fmv_runner.py`). This captures the traps so the next person automating a documented pricing rule does not silently reopen them.

## Guidance

**1. Never interpolate across a *populated* target-grade bucket — use the real comps that are already there.**

The pool being flagged `one_sided` or `too_wide` (the BUI-86 sparse/one-sided safety flags) does **not** mean there are no comps at the target grade — it describes the *shape* of the whole pool. If real comps exist AT the target grade, price off those directly; interpolating off distant brackets while ignoring a populated bucket is what produced a **6× over-bid ($105 → $650 cap)**. Interpolation is a fallback for an *empty* target bucket, never a substitute for comps that are actually there.

**2. Require n≥3 comps in the interpolation input before interpolating at all.**

A 2-comp pool `[$50 @ grade 5, $5000 @ grade 9]` sailed past the BUI-179 `too_wide` wild-ratio guard (that guard bounds a single trimmed pool, not the two-endpoint bracket interpolation reads from) and interpolated a ~$2k cap off two wildly-separated points. An `n≥3` floor closes it: interpolation needs enough support that the curve is not being drawn between two noise points.

**3. An interpolated value MUST be marked as interpolated everywhere, at LOW confidence, and the marker must survive a cache round-trip.**

An interpolated point estimate (`fmv_low == fmv_high`) must never be conflated with a real direct-comp price:
- **Human table:** render it as `$180 interp`, not a bare `$180`.
- **JSON:** set `interpolated: true` plus an `interpolation` sub-dict (`grade_below`/`grade_above` and the endpoint prices) so a downstream consumer can see how it was derived.
- **Confidence:** LOW — an interpolated cap is a weaker signal than a direct comp and should read that way to the human gating the bid.
- **Cache reuse:** the marker has to survive a persisted-then-reloaded round-trip. `comic-fmv` recovers it from the `fmv_notes` token (`interpolated=grade …`) via `_interpolated_from_notes`, so a re-displayed or re-served cached row still says "interpolated," not "direct comp." A marker that only exists on the fresh-compute path silently launders into a direct-comp price on the next cache hit.

## Why This Matters

Both bugs pass tests that only assert "a number came out." Neither announces itself — a 6× or 4× over-bid just quietly raises a bid cap, and you overpay on the one auction where it mattered. The guards are cheap to write and expensive to rediscover after a real over-bid. More generally: **automating any documented human heuristic inherits the judgment the human was silently applying** — "obviously use the comps that are right there," "obviously don't draw a curve through two points," "obviously flag it as a guess." Encode that judgment as explicit gates and exact-arithmetic tests, or the automation over-fires exactly where the human never would.

## When to Apply

- Adding or changing grade-curve interpolation, monotonicity checks, or bucket medians in `fmv_math.py` / `fmv_runner.py`.
- Automating any previously hand-run pricing heuristic (recency weighting, sparse-pool fallbacks, cross-grade estimation) — gate it against these traps first.
- Emitting any computed (non-direct-comp) price into a table, JSON payload, or cache that a bid cap or downstream consumer reads — carry a provenance marker through every representation, including cache reuse.

## Examples

Populated-bucket precedence (trap #1) — the flag describes pool shape, not target-bucket emptiness:

```
pool flagged too_wide, but bucket_medians has a real entry at the target grade
  WRONG: interpolate off grade 5 + grade 9 brackets → $650 cap   (6× over-bid)
  RIGHT: price off the target-grade comps that are already in the bucket → $105
```

Sample-floor (trap #2):

```
[$50 @ grade 5, $5000 @ grade 9]   # n = 2
  n < 3 → do NOT interpolate (the ~$2k cap this produced bypassed the too_wide guard)
```

Provenance that survives cache reuse (trap #3):

```
table:  $180 interp            (not "$180")
json:   {"interpolated": true, "interpolation": {"grade_below": 6.0, "grade_above": 9.0, ...}, "confidence": "low"}
cache:  fmv_notes carries "interpolated=grade 6→9"; _interpolated_from_notes recovers the flag on reload
```

## Related

- **BUI-306** — this work (PR #141): auto-apply §7 grade-curve interpolation + §5 monotonicity check.
- **BUI-179** — the `too_wide` wild-ratio guard that the 2-comp bracket bypassed (motivates the n≥3 floor).
- **BUI-86** — the sparse / `one_sided` / `too_wide` safety flags (a shape flag is not a target-bucket-empty signal).
- `docs/solutions/best-practices/fmv-self-referential-feedback-deflation-guard.md` — sibling FMV money-path guard (clock-free math, wins-AND-losses).
- fmv.md §5 (monotonicity) / §7 (grade-curve interpolation) — the documented heuristic this automates.
