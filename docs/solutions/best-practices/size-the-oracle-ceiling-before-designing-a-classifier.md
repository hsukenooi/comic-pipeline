---
title: "Size the oracle ceiling before designing a classifier"
date: 2026-08-03
category: best-practices
module: "apps/ebay/src/comic_identity.py, apps/fmv/src/fmv_math.py (FMV comp-exclusion signals)"
problem_type: best_practice
component: tooling
severity: medium
status: corrected
superseded_by: "BUI-646 (2026-08-03) measured the same 493-response corpus and falsified
  this doc's claim that the full-class oracle is 'the ceiling on every rule you could ever
  write'. Excluding comps does not move fmv_high monotonically: over 13,717 leave-one-out
  removals fmv_high ROSE 351 times (2.56%), and on the BUI-637 class a proper subset of the
  exclusion drove fmv_high lower than the full-class oracle at 3 of 110 grade-points. The
  oracle is a magnitude, not a bound. See the section 'The oracle is a magnitude, not a
  bound' below; the doc's central discipline (measure before designing) is unchanged."
superseded_date: 2026-08-03
mechanized_by: advice-only
advice_only_reason: "Whether a proposed detector is worth building is a measurement the
  author must choose to run before writing code; no repo predicate can tell that a ticket
  skipped its own feasibility bound, because the skipped step leaves no artifact behind."
applies_when:
  - "A ticket proposes detecting or excluding a class of inputs (a lot comp, a bad row, an outlier)"
  - "About to enumerate candidate signals, thresholds, or heuristics for a classifier"
  - "Triaging a comp-pollution or FMV-signal ticket"
  - "A precision/recall bar is being set before the value of perfect precision is known"
  - "About to claim an exclusion rule 'can only lower' a price, cap, or score"
  - "Reporting the measured effect of an exclusion rule without stating its direction"
  - "A class is proposed for exclusion because it is the WRONG ITEM, without checking
    whether it sits above or below the pool median"
related_components:
  - "fmv"
tags:
  - "measurement"
  - "classifier"
  - "precision-recall"
  - "fmv"
  - "feasibility"
  - "non-monotonicity"
  - "oracle-bound"
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
measure the downstream effect on whatever the ticket claims to protect. That number is what
perfect precision buys — the size of the prize, measured rather than assumed. If it is not
worth having, no amount of signal engineering redeems it, and you have learned this in one
afternoon rather than after seventeen dead ends.

**Read the oracle as a magnitude, not as a bound.** An earlier version of this doc called it
"the ceiling on every rule you could ever write." On this pipeline that is measurably false —
excluding comps does not move `fmv_high` monotonically, so a *subset* rule can move a pool the
full-class oracle leaves alone, or move it further, or move it the other way. Use the oracle to
decide whether the prize is worth chasing; do not use it to bound what a specific rule will do.
See "The oracle is a magnitude, not a bound" below.

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

## The oracle is a magnitude, not a bound

BUI-646 re-measured the same 493-response corpus asking a question neither BUI-629 nor BUI-637
had asked: **when you remove a comp, which way does `fmv_high` go?** The premise every
comp-exclusion ticket runs on — *removing a polluting comp can only lower the cap* — turns out
to be false in general and true exactly where the money is. Both halves are worth carrying.

### Removal is not monotone

Over **13,717 single-comp leave-one-out removals** (493 pools, each priced at its own median
comp grade), `fmv_high` **rose in 351 of them (2.56%)**, touching **101 of 493 pools**. Four
distinct mechanisms produce this, and the one that is easiest to guess is not the most common.
Counting the 34 up-moves where the removed comp was priced above the published `fmv_high`:

| # | Mechanism | Count |
|---|---|---|
| A | `build_pool` progressive widening — losing a comp drops the ±0.5 window below `MIN_NARROW_POOL` (5), forcing a wider window that admits farther-grade, pricier comps | 8 |
| B | **IQR fence re-admit** — removing a comp changes Q1/Q3, so the ±1.5·IQR fence moves outward and a comp previously trimmed as an outlier re-enters the pool | 7 |
| C | Ordinary quantile arithmetic — the removed comp sat below the pool's raw Q75, so dropping it shifts the remaining ranks up | 13 |
| D | The publish layer — raw Q75 is flat or falling, but `clean_round` plus BUI-528's collapsed-range widening lifts the published band (a removal that raises `cv` opens a wider band) | 6 |

Mechanism A is the one the ticket predicted, and it accounts for under a quarter of the moves.
**B is the dangerous one and it is invisible from the window:** the pool size, the grade window
and the trimmed count can all be unchanged while the pool's *membership* silently changes.

### Where the money is, the intuition holds

Restrict to removals of a comp priced above the published `fmv_high` — the only removals an
exclusion rule is ever claimed to be protecting the cap from — and split by how genuinely dear
the comp is relative to its pool:

| Removed comp | UP moves | Removals | Rate |
|---|---|---|---|
| above published `fmv_high`, but ≤ the pool's raw Q75 (a `clean_round` artifact, not really top-quartile) | 21 | 194 | 10.82% |
| genuinely above the pool's raw Q75 | 13 | 3,003 | 0.43% |
| …and ≥ 2× the pool median | 3 | 1,496 | 0.20% |
| …and ≥ 3× the pool median | **0** | **890** | **0.00%** |

The decay is monotone and it bottoms out at exactly the class the sharp test above already
named as *the only comps that can inflate an FMV*. **In 890 measured removals, a comp expensive
enough to actually inflate a pool has never once gotten cheaper to remove.** An exclusion rule
aimed at genuine pollution is not fighting the pipeline. A rule that also sweeps up ordinary
mid-priced comps is — and that is where its surprises will come from.

### A shipped rule that raised a real bid cap

BUI-637's rule fired on the `"X-Men 54"` pool at grade 8.5, dropping exactly one comp — a
genuine 17-issue run, `X-Men 54 55 56 … 70 1996 VF/NM Newsstand`, at $20.00. Correct exclusion,
textbook pollution. The result:

```
PRE  : IQR fence (-74.71, 133.63)  ->  $139.99 trimmed as an outlier
       kept [1.49, 2.84, 3.99, 12.00, 20.00, 91.00]     fmv 5-30   max_bid 25
POST : IQR fence (-99.06, 173.43)  ->  $139.99 SURVIVES
       kept [1.49, 2.84, 3.99, 12.00, 91.00, 139.99]    fmv 5-80   max_bid 60
```

Removing the $20 lot comp widened the pool's interquartile range, which pushed the outlier
fence out far enough to re-admit a $139.99 comp it had been suppressing. `fmv_high` +167%,
`max_bid` $25 → $60. The grade window never changed. **A polluting comp can be load-bearing:
it was holding the outlier fence shut.**

### Why this makes the oracle a magnitude rather than a bound

If removal is non-monotone, excluding *all* of a class need not be the extreme of excluding
*some* of it. Tested directly on the BUI-637 class: across the 10 corpus pools where that class
has ≥2 members, at 110 grade-points, a **proper subset** of the exclusion drove `fmv_high`
lower than the full-class oracle did at **3 of them** — including `"Marvel Feature 1"`, where
the baseline is 90, the full-class oracle *raises* it to 100, and dropping just 1 of the 5
class members leaves it at 90.

So the full-class oracle is one point in a non-monotone space, not a supremum over the rules
you might write. It still does the job this doc was written for — it sizes the prize, and a
prize too small to chase stays too small. It just is not a guarantee about any particular rule.

### Verdict: by design, not a bug

All four mechanisms are deliberate, documented behaviors doing exactly what their own tickets
specified — BUI-86's widening (reach ≥5 comps), the IQR trim (drop outliers *relative to the
pool's own dispersion*), the weighted quantiles (BUI-287), BUI-528's collapse-widen (reveal
real dispersion). Every one of them is a pure function of the *resulting* pool and none of them
knows what was removed. No invariant anywhere in `fmv_math` claims monotonicity under comp
removal, and none of these functions could honor one without abandoning its own purpose: an
IQR fence frozen from a pre-removal pool is not an IQR fence, and there is no canonical
"pre-removal" pool anyway — the pool is whatever the query returned.

**The correct response is measurement discipline, not a code change.**

### What to do

- **Report the direction, not just the count.** "`fmv_high` moved in N pools" is an incomplete
  result. Say how many moved up, how many down, and how many became needs-manual.
- **Inspect the up-moves before shipping.** They are where a rule's real surprises live, and
  they are rare enough to read by hand — BUI-637's whole ladder sweep produced 3 up-moves
  across 2 of 493 pools.
- **Check the trimmed-pool membership, not its size.** Mechanism B changes *which* comps are
  in the pool while `n`, the window, and the flags all hold still.
- **Do not claim an exclusion "can only lower" a price.** On this pipeline that sentence is
  measurably false; scope it to the ≥3× class if you need it.

## Which side of the pool median does the class sit on?

BUI-667 proposed excluding reprint-*format* comics — `Classic X-Men`, `Marvel's Greatest
Comics`, `Marvel Super Action`, `Marvel Milestone`, `JC Penney` reprints — from the pools of
the originals they reprint. The premise is unimpeachable on identity grounds: a `Classic
X-Men #41` (1989) genuinely is a different book from `X-Men #41` (1968), so pricing one
against the other is wrong.

The oracle killed it anyway. Hand-labelled 86 reprint-format titles, excluded all of them at
precision 1.000, measured `fmv_high` in both directions:

| Sampling | UP | DOWN | NULLED | REVIVED |
|---|---|---|---|---|
| at each pool's median comp grade (2 of 493 pools move) | 1 | **0** | 1 | 0 |
| across a 13-point CGC-ladder sweep | 23 | 5 | 15 | 6 |

**Zero pools where the exclusion lowers a cap at the headline sampling, and the ladder sweep
is 23 up against 5 down.** Worst case is `"X-Men 83"` at grade 7.0, where the "fix" takes
`fmv_high` from 45 to 180. The sharp test agrees: 0 class members sit above their pool's Q75
at ≥3× the pool median.

The mechanism generalizes past this ticket, and it is the cheapest question to ask after the
oracle number itself:

> **Correct-on-identity and profitable-to-exclude are independent properties. A polluting
> comp only costs money if it sits ABOVE the pool median; a class that sits below it is
> depressing the cap, and removing it RAISES the cap.**

Reprint editions are cheap — that is what makes them recognizable as reprints and it is
exactly what makes excluding them a cap-raising change with no downside protection to buy.
Contrast BUI-598's class (a $6500 slab manufacturing a $3825 cap) and BUI-637's (a 4-issue
Damian Wayne set priced as if it were one issue): both sat *above* their pools. Same word —
"pollution" — opposite sign on the money.

This is the same shape that made BUI-645 ship its ordinal half and withhold its bare half
(UP 29 / DOWN 2 / 19 nulled). Two independent classes, one rule of thumb.

## A load-bearing removal is usually load-bearing for a good reason

BUI-665 chased the one live consequence in "A shipped rule that raised a real bid cap" above:
was the re-admitted $139.99 comp itself pollution the fence had been suppressing for the
wrong reason? Hand-checked: **no.** It is a genuine raw, single-issue, first-print copy of
the very book, parsed at 7.5, bracketed by the same corpus's graded pool (CGC 8.0 $160/$125,
raw 7.0 $103, raw 8.0 $91) — which also holds a raw VF+ 8.5 at $159.99. The $60 cap is
conservative for an 8.5 copy; the pre-BUI-637 $25 cap was the wrong number.

Characterizing the whole class rather than the anecdote is what settled it. Re-running the
leave-one-out sweep on the post-BUI-645 tree gives 38 up-moves whose removed comp was priced
above the published `fmv_high`, **11 via IQR-fence re-admit across 8 pools** (a re-measurement
on a tree carrying two more exclusions than BUI-646's 7-of-34, not a contradiction of it).
Read by hand: 6 re-admit a genuine raw comp of the right book, 1 re-admits a 1:100 virgin
variant, 1 re-admits a different book entirely, 1 is a graded pool behaving as designed, and
**2 — one pool — are an actual exclusion gap** (a signed/COA copy; `LOCAL_EXCLUDE_RE` matches
`signed by` but not a bare `Signed <name>`). Largest single move in the class: `fmv_high`
40 → 70 on `"X-Men 78"`.

The lesson is the direction one, sharpened: **when an exclusion rule's removal widens a fence
and re-admits something, check what got re-admitted before assuming the rule misfired.** Most
of the time the pool simply got more honest.

## Why This Matters

Eight consecutive FMV signal tickets in this repo have now been Canceled on measurement
(BUI-578, 582, 590, 592, 594, 597, 629, and 667). Every one of them spent its effort on classifier
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

Step 2 has a direction half that BUI-629 and BUI-637 both skipped, added by BUI-646:

```
2b. Split the movement by direction -> how many pools UP, how many DOWN,
                                       how many to needs-manual?
2c. Read every UP move by hand      -> rare enough to afford (BUI-637: 3
                                       up-moves in 2 of 493 pools); it is
                                       where the surprises are
```

And a step 4b added by BUI-667, which is step 4's cheap precursor — it needs no
per-comp quantile arithmetic, just the class's prices against the pools':

```
4b. Which side of the median?       -> if the class is systematically CHEAPER
                                       than the pools it pollutes, excluding it
                                       RAISES caps.  Correct-on-identity is not
                                       profitable-to-exclude (BUI-667: UP 1 /
                                       DOWN 0 at 493 pools; UP 23 / DOWN 5 on
                                       the CGC ladder)
```

The result is recorded in the design essay in `apps/ebay/src/comic_identity.py` rather than
only in the ticket, because the previous wording there — *"Left open on purpose"* — read as an
invitation and had already drawn one attempt.

## See also

- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` — the same
  discipline applied to a ticket's stated facts rather than to its value.
- `docs/solutions/best-practices/mutation-test-each-check-against-the-break-it-claims-to-catch.md`
  — the companion question for checks: not "is it worth building" but "does it actually work".
