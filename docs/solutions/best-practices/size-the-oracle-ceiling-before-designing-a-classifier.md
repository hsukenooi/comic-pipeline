---
title: "Size the oracle ceiling before designing a classifier"
date: 2026-08-03
category: best-practices
module: "apps/ebay/src/comic_identity.py, apps/fmv (FMV comp-exclusion signals)"
problem_type: best_practice
component: tooling
severity: medium
mechanized_by: advice-only
advice_only_reason: "Whether a proposed detector is worth building is a measurement the
  author must choose to run before writing code; no repo predicate can tell that a ticket
  skipped its own feasibility bound, because the skipped step leaves no artifact behind."
applies_when:
  - "A ticket proposes detecting or excluding a class of inputs (a lot comp, a bad row, an outlier)"
  - "About to enumerate candidate signals, thresholds, or heuristics for a classifier"
  - "Triaging a comp-pollution or FMV-signal ticket"
  - "A precision/recall bar is being set before the value of perfect precision is known"
related_components:
  - "fmv"
tags:
  - "measurement"
  - "classifier"
  - "precision-recall"
  - "fmv"
  - "feasibility"
---

# Size the oracle ceiling before designing a classifier

## Context

BUI-629 asked for a second signal to exclude ~30 sold-comp lot listings that carry a bare
issue range with no run/set keyword. The premise was the one that made its predecessor
(BUI-598) worth shipping: **a lot comp inflates FMV, and an inflated FMV is real money on a
purchase decision.**

Seventeen candidate signals were measured against the 493-response offline corpus. None
reached the precision bar. But the measurement that actually closed the ticket was not any
of the seventeen — it was the one run *first*:

**What would a perfect detector buy?** An oracle excluding all 42 genuine lots (hand-checked;
more than the ~30 estimated) with precision 1.00 — strictly better than any rule could
achieve — moved `fmv_high` in **3 of 493 pools**, worst case **9.6%** ($6.79 → $6.14).

That number closed the ticket before signal design mattered at all.

## Guidance

**Before asking "can I detect X?", ask "what would a *perfect* detector buy me?"**

Build the oracle: label the target class by hand, exclude all of it with precision 1.00, and
measure the downstream effect on whatever the ticket claims to protect. That bound is the
ceiling on every rule you could ever write. If the ceiling is not worth having, no amount of
signal engineering redeems it, and you have learned this in one afternoon rather than after
seventeen dead ends.

**Find the mechanism that makes the ceiling low.** A low bound is not a mystery, and naming
the mechanism is what makes the result transferable. Here: **a polluting comp only costs
money if it survives into a priced pool.** 35 of the 42 lots carried no parseable grade, so
`fmv_math.build_pool` dropped them before pricing; IQR-trim removed 4 more. Only 3 ever
reached a priced pool, all cheap and near the median.

**The sharp version of the test:** of the comps that survive above Q75 at ≥3× the pool median
— the only comps that can inflate an FMV — how many are in your class? Here: **zero**. The 112
that do qualify are genuine high-grade single-issue keys (DD #16, FF #46, X-Men #91).

**Beware a premise that is true of the motivating case and false of the residual.** BUI-598's
class was expensive *and* grade-tagged (a $6500 comp at 9.4 that manufactured a $3825 bid
cap). BUI-629's residual is cheap (median $49.48) and mostly grade-less. Both are "lot comps".
The shared sentence — *a lot comp inflates FMV* — is true of the first and measurably false of
the second. Inheriting a predecessor's justification without re-measuring is how a ticket gets
filed for a problem that no longer exists.

## Why This Matters

Seven consecutive FMV signal tickets in this repo have now been Canceled on measurement
(BUI-578, 582, 590, 592, 594, 597, and 629). Every one of them spent its effort on classifier
precision. Not one of them established first that a perfect classifier was worth having.

An oracle bound is cheap, it is an upper bound rather than an estimate, and it either kills
the ticket outright or tells you the ceiling you are optimizing toward. It is the highest-value
measurement in the sequence and it is the one routinely skipped, because "can I detect it?" is
the more interesting question.

A negative result measured to this standard is a deliverable. It is worth more than a shipped
weak heuristic, because it closes the question rather than deferring it — and it leaves the
number behind so the next person does not re-open it.

## When to Apply

- Any ticket proposing a new detector, exclusion rule, or filter over a population.
- Any ticket inheriting its justification from a predecessor that shipped successfully —
  re-measure whether the mechanism still applies.
- When a precision bar is being negotiated before anyone has asked what precision 1.00 is worth.

## Examples

The measurement that closed BUI-629, in the order it should be run:

```
1. Hand-label the class            -> 42 genuine lots of 65 candidates
2. Oracle-exclude all 42 @ P=1.00  -> fmv_high moves in 3 of 493 pools, max 9.6%
3. Name the mechanism              -> 35/42 have no parseable grade; build_pool
                                      drops them pre-pricing, IQR-trim takes 4 more
4. The sharp test                  -> of 112 comps above Q75 at >=3x pool median,
                                      0 are in this class
--> STOP. Do not enumerate candidate signals.
```

Only after step 4 fails to kill the ticket is signal design worth starting. In BUI-629 it did
kill it, and the seventeen signals measured afterwards merely confirmed what step 2 already
established.

The result is recorded in the design essay in `apps/ebay/src/comic_identity.py` rather than
only in the ticket, because the previous wording there — *"Left open on purpose"* — read as an
invitation and had already drawn one attempt.

## See also

- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` — the same
  discipline applied to a ticket's stated facts rather than to its value.
- `docs/solutions/best-practices/mutation-test-each-check-against-the-break-it-claims-to-catch.md`
  — the companion question for checks: not "is it worth building" but "does it actually work".
