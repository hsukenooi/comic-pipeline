---
title: "A shipped guard is not a running guard — check its trigger against real inputs"
date: 2026-08-12
category: best-practices
module: "apps/fmv/src/fmv_runner.py (_HAND_PRICE_MARKERS, BUI-533); apps/ebay/src/comic_identity.py (vintage-key query hardening, BUI-347/350); any guard whose trigger reads caller-supplied data"
problem_type: best_practice
component: tooling
severity: high
mechanized_by: advice-only
advice_only_reason: "The failure is a mismatch between a guard's trigger and the shape of
  real production inputs, so it is only visible by running the guard's own predicate over
  live data. No repo-source predicate can see it: the code, the tests and the fixtures are
  all internally consistent — it is the data the repo does not contain that disagrees."
applies_when:
  - "Auditing whether a protection, guard, or exclusion rule that already shipped is actually doing its job"
  - "A ticket's premise is 'X is unprotected' and a search shows protection for X already shipped"
  - "A guard's trigger is a string prefix, marker, or convention a human types by hand"
  - "About to conclude a class of bug is closed because the fix is merged and tested"
symptoms:
  - "A guard has passing tests and a shipped ticket, yet the incident it prevents keeps happening"
  - "A SQL LIKE audit says a population is covered, but calling the real predicate says otherwise"
  - "A protection's own author writes a new row that the protection does not cover"
root_cause: logic_error
related_components:
  - "database"
tags:
  - guard-never-fires
  - trigger-vs-input
  - provenance-marker
  - false-coverage
  - bui-533
  - bui-759
  - bui-717
---

# A shipped guard is not a running guard — check its trigger against real inputs

## Context

Two instances in three days, in unrelated subsystems, with the same shape.

**BUI-347/350 (vintage-key query hardening).** The guard that keeps modern
same-numbered relaunches out of a vintage comp pool is correct and deployed —
tested directly, `build_query` does the right thing. It never fired on the
polluted pools because the contaminating query arrived carrying no year, no
publisher and no exclusion terms. The guard was fine; **its input never
arrived**.

**BUI-533 / BUI-759 (hand-priced FMV protection).** `comic-fmv` skips a
hand-priced `fmv` row on a default run, so a batch refresh cannot silently
overwrite a human's judgement. It works. It matches on:

```python
_HAND_PRICE_MARKERS = ("hand §", "hand OVERRIDE")   # apps/fmv/src/fmv_runner.py
def _is_hand_priced(notes): return notes is not None and notes.startswith(_HAND_PRICE_MARKERS)
```

BUI-533 formalized "the de-facto provenance marker operators already use" —
but sampled only half the convention. Operators also write `manual:` and
`Manual:`. Measured on the Mini 2026-08-12: **7 of 12 operator-priced rows
were unprotected**, including the ASM #50 row at $600–680. The guard was
correct, deployed, tested, and did not cover most of the rows it exists for.

The tell that it is a *pattern* rather than two accidents: in both cases the
code review would pass. There is nothing wrong with the guard.

## Guidance

**Distinguish three separate questions, and never let an answer to one stand
in for another:**

1. Is the guard's logic correct? (tests answer this)
2. Is the guard deployed? (the SHA answers this)
3. **Does its trigger match the inputs that actually occur?** (only live data
   answers this)

Question 3 is the one that goes unasked, because 1 and 2 both come back green
and feel like coverage. A ticket that says "add protection for X" is closed by
1 and 2; whether X is *protected in practice* is question 3.

**Call the guard's own predicate over live rows. Do not approximate it.** The
BUI-759 gap is invisible to a SQL `LIKE 'hand%'` audit and to reading the code.
It appears the moment you import the real function:

```python
from fmv_runner import _is_hand_priced
for row in conn.execute("SELECT id, notes FROM fmv WHERE notes LIKE 'manual%' OR notes LIKE 'hand%'"):
    print(row["id"], _is_hand_priced(row["notes"]))
```

An approximation of a predicate shares none of the predicate's bugs, which is
exactly why it cannot find them.

**Treat a hand-typed marker in a freetext field as unprotected by default.**
Prefix-as-provenance fails open in every direction: a reword, a capital letter,
a translated phrase, a well-meaning edit. If the marker is load-bearing, say so
where it is written (BUI-720's repaired row carries *"the `hand §` prefix is
load-bearing … do not reword"* in its own notes), and prefer a real column that
a reword cannot destroy.

**When a ticket's premise is "X is unprotected", search before building.** The
duplicate-check is what turned this from a wrong fix into the right one: filing
BUI-759 surfaced BUI-533 as already Done, which reframed the work from *add
protection* to *the protection exists and does not fire*. Those need opposite
patches.

## Why This Matters

A guard that never fires is worse than a missing one, because it is *believed*.
The missing guard shows up in a backlog; the silent one shows up as a ticket
marked Done, and everything downstream is planned as though the class is closed.
Here that meant an FMV row a human had deliberately priced at $600–680 sitting
one default `comic-fmv` run away from being replaced by the pooled answer the
human had already rejected — the BUI-533 incident, replayed on a costlier book,
*after* BUI-533 shipped.

## When to Apply

- Before closing any "add a guard/protection/exclusion" ticket: run its
  predicate over production and report the covered/uncovered split, not just
  the test count.
- When a documented-and-fixed bug class recurs — ask whether the fix runs on
  the failing input, before assuming the fix is wrong.
- When auditing coverage of anything keyed on a string convention.
- Whenever you are about to write "already handled by <ticket>" — check that
  it is handled for the *specific* input in front of you.

## Examples

**Before:** "Hand-priced rows are protected — BUI-533 shipped it, the tests
pass, the skip count surfaces in the run summary." True, and it left 7 of 12
rows exposed.

**After:** `_is_hand_priced` called against each stored row prints
`protected=False` for `fmv 767` (`Manual: CGC-proxy + genuine raw comps (ASM
#50…`, $600–680). The split — 4 protected, 7 not — is the finding, and it is
one import away from any audit that bothered to use the real predicate.

## Related

- `docs/solutions/conventions/an-endpoint-success-report-is-not-a-write.md` —
  the write-side sibling: a response field that reports intent rather than
  outcome. Same root posture: verify the effect, not the claim.
- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md` —
  the general class of checks whose passing looks like their breaking.
- `docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md` —
  measure before designing; here, measure before believing.
- Tickets: BUI-533 (the guard), BUI-759 (the gap, open), BUI-720 (found it),
  BUI-717 (the vintage-query instance).
