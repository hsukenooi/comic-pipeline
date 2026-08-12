---
title: "A shipped guard is not a running guard — check its trigger against real inputs"
date: 2026-08-12
last_updated: 2026-08-12
category: best-practices
module: "apps/fmv/src/fmv_runner.py (_HAND_PRICE_MARKERS + _split_by_db_cache's eligibility gate, BUI-533/759/775); apps/ebay/src/comic_identity.py (vintage-key query hardening, BUI-347/350; _marker_hit lexicons, BUI-766/770); any guard whose trigger reads caller-supplied data"
problem_type: best_practice
component: tooling
severity: high
mechanized_by: advice-only
advice_only_reason: "The failure is a mismatch between a guard's trigger and the shape of
  real production inputs, so it is only visible by running the guard's own decision path over
  live data. No repo-source predicate can see it: the code, the tests and the fixtures are
  all internally consistent — it is the data the repo does not contain that disagrees. Worse,
  a test can PIN the wrong belief: two pre-existing tests asserted the false premise behind
  BUI-775 verbatim and passed for years. Individual instances are mechanizable once found
  (BUI-775 added tests asserting the bucketing, not the predicate), but the audit practice
  that finds them is not."
applies_when:
  - "Auditing whether a protection, guard, or exclusion rule that already shipped is actually doing its job"
  - "A ticket's premise is 'X is unprotected' and a search shows protection for X already shipped"
  - "A guard's trigger is a string prefix, marker, or convention a human types by hand"
  - "About to conclude a class of bug is closed because the fix is merged and tested"
  - "A guard's trigger was just fixed — the fix does not establish that the guard now runs"
  - "About to report an acceptance criterion met on the strength of a leaf predicate returning the right answer"
symptoms:
  - "A guard has passing tests and a shipped ticket, yet the incident it prevents keeps happening"
  - "A SQL LIKE audit says a population is covered, but calling the real predicate says otherwise"
  - "A protection's own author writes a new row that the protection does not cover"
  - "The guard's predicate returns True for every row it should, and the rows are still unprotected"
  - "A guard reads one identity key while the write it protects against uses a different one"
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

**BUI-775 — the same guard again, after its trigger was fixed.** This is the
instance that generalizes the lesson, so it is worth following precisely.
BUI-759 widened the marker set and *verified it against live data*: the new
predicate returns True for exactly the 12 operator rows out of all 999 stored
`fmv` rows, zero false positives. Criterion met. The guard still protected
**1 of 12** rows.

The trigger was now correct and was never consulted, because an eligibility
test upstream of it disqualified the rows:

```python
# _split_by_db_cache, before BUI-775
eligible = bool(book.get("locg_id")) and book.get("grade") is not None
if not eligible:
    needs.append({"_idx": i, **book})   # -> straight to recompute
    continue                            # the hand-priced branch below is never reached
```

11 of the 12 hand-priced books carry no `locg_id`. And missing `locg_id` did
not make the *write* miss: `upsert_comic` keys row identity on
`(title, issue, variant)` — `locg_id` is optional metadata, never identity —
and `upsert_fmv` then does `ON CONFLICT(comic_id, grade) DO UPDATE`. **The book
was invisible to the guard on the way in and fully visible to the upsert on the
way out.** The fix was to key the guard on the write's own identity, demoting
`locg_id` from gate to shortcut.

Two pre-existing tests encoded the false premise verbatim — *"a book with no
`locg_id` … can't target an existing row either"* — and passed for years. **A
test pins a wrong belief exactly as firmly as a right one.**

The tell that it is a *pattern* rather than three accidents: in every case the
code review would pass. There is nothing wrong with the guard.

**The fourth shape, for contrast — BUI-766's audit.** 23 string-keyed guards
measured against live data found 3 new under-coverers, and the pipeline-level
check came back *clean*: re-running `hard_exclude()` over all 15,235 stored comp
rows rejects 0, so the gate provably runs on the write path. Those three were
**not** "the input never arrives" cases. The gate ran, and the spelling missed
(`UKPV` vs `UK Price Variant`; `Action Figures` vs `action figure`;
`2024FACSIMILE` fused to a digit). Distinguishing "not reached" from "reached and
missed" changed the fix and the priority.

## Guidance

**Distinguish four separate questions, and never let an answer to one stand
in for another:**

1. Is the guard's logic correct? (tests answer this)
2. Is the guard deployed? (the SHA answers this)
3. **Does its trigger match the inputs that actually occur?** (only live data
   answers this)
4. **Is the trigger reached at all for those inputs?** (only the *decision path*
   answers this — not the trigger)

Questions 3 and 4 are the ones that go unasked, because 1 and 2 come back green
and feel like coverage. A ticket that says "add protection for X" is closed by
1 and 2; whether X is *protected in practice* is 3 and 4.

**A guard fails to run in three distinguishable ways. Name which one you have
before patching, because the fixes are different:**

| Failure mode | What is wrong | How you see it |
|---|---|---|
| **Never reached** | The guard's input never arrives in the shape that routes to it | The gate's own re-run rejects nothing on stored data (BUI-347/350) |
| **Reached, trigger misses** | The guard runs; its pattern does not match real spelling | Predicate over live rows: covered/uncovered split (BUI-759, BUI-766/770) |
| **Trigger correct, eligibility gate excludes** | The pattern is right and is never consulted | **Predicate says protected, decision function says recompute** (BUI-775) |

The third is the hardest to see, because every narrower check passes.

**Proving the predicate is not proving the decision.** This is the load-bearing
correction. In BUI-759 the predicate was verified against all 999 live rows —
exactly the right 12 matched, zero false positives — and reported as the
acceptance criterion met. That was *true and insufficient*. A row is protected
only if the **decision function** buckets it as protected:

```python
# Not this — the leaf predicate. It answers a question nobody's money depends on.
_is_hand_priced(row["notes"])                      # True for all 12. Proves nothing.

# This — the decision. GETs only; safe against a live server.
cached, needs, hand, force_notes, lookup_err = _split_by_db_cache(
    books, server_url=..., max_age_days=0, force=False)
assert not needs                                   # before BUI-775: needs=11, hand=1
```

**And neutralize every short-circuit upstream of the check, or you will test the
wrong path.** `max_age_days=0` is not incidental: at the default, a row fresh
enough to be a cache hit never reaches the guard at all, so the run looks
protected for a reason that has nothing to do with the guard. Force every input
down the path you are trying to exercise.

Generalized: **a correct predicate reached through a disqualifying gate is
exactly as useless as a wrong predicate.** Test the whole path from input to
decision, and assert on the decision.

**Check that the guard's key is the write's key.** BUI-775's root cause in one
line: the guard looked rows up by `locg_id` while the write it protects against
resolved them by `(title, issue, variant)`. Whenever a guard and the mutation it
guards identify their target *separately*, they can disagree — and the guard
will report a clean verdict about a row the write never touches. Derive the
guard's lookup from the same identity the writer uses, and fail closed when it
cannot be resolved ("don't know" is not "not protected").

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

**And then replayed again after BUI-759 shipped.** That is the part worth
internalizing: fixing a guard's trigger produced a merged PR, a green suite, a
live-data measurement, and a ticket that *looked* closeable — while 11 of 12
rows stayed exposed. The only thing that caught it was probing the decision on
the deployed build after merge. Had the ticket been closed on the strength of
the PR and the passing tests, the $600–680 row would still be one batch run
from destruction, with the class marked Done twice over.

The compounding failure is specific: **each fix made the belief stronger while
the exposure stayed the same.** BUI-533 created the belief, BUI-759 reinforced
it with live-data evidence about the wrong function, and only a decision-level
probe dislodged it.

## When to Apply

- Before closing any "add a guard/protection/exclusion" ticket: run its
  **decision function** over production and report the protected/unprotected
  split, not just the test count and not just the predicate.
- **Immediately after fixing a guard's trigger** — the fix is evidence about the
  trigger and no evidence at all about whether the guard runs. Re-probe the
  decision.
- **After deploy, not at merge.** A merged PR and a green suite cannot see any of
  failure modes 1, 3, or 4. BUI-775 was found by a post-deploy probe of a ticket
  that was otherwise ready to close.
- When a documented-and-fixed bug class recurs — ask whether the fix runs on
  the failing input, before assuming the fix is wrong.
- When auditing coverage of anything keyed on a string convention.
- Whenever you are about to write "already handled by <ticket>" — check that
  it is handled for the *specific* input in front of you.
- When a guard and the mutation it guards resolve their target independently —
  compare the two identity keys directly.

## Examples

**Before:** "Hand-priced rows are protected — BUI-533 shipped it, the tests
pass, the skip count surfaces in the run summary." True, and it left 7 of 12
rows exposed.

**After:** `_is_hand_priced` called against each stored row prints
`protected=False` for `fmv 767` (`Manual: CGC-proxy + genuine raw comps (ASM
#50…`, $600–680). The split — 4 protected, 7 not — is the finding, and it is
one import away from any audit that bothered to use the real predicate.

**Before (round two, the subtler one):** "The widened predicate returns True for
exactly the 12 operator rows across all 999 stored rows, zero false positives —
criterion met." Accurate, live-data-backed, and it left 11 of 12 rows exposed.

**After (round two):** the decision function, same population:

```
before BUI-775: cached=0 needs=11 skipped_hand=1  lookup_err=0
after  BUI-775: cached=0 needs=0  skipped_hand=12 lookup_err=0
```

And then the end-to-end check the predicate can never stand in for — a real
default `comic-fmv` run over all 12 books: every row reported
`skipped_hand_priced`, summary `skipped 12 hand-priced row(s)`, and the 12 rows
**byte-identical** before and after including `updated_at` (untouched, not
rewritten with the same values). That last distinction matters: comparing only
`low`/`high` would have passed even if the guard had failed and the recompute
happened to land on the same numbers.

**Falsification, so the test cannot rot into decoration:** reintroducing the old
eligibility gate fails 9 of the new tests. A regression test for this class must
assert the *bucketing*; a predicate-level test passes against the bug.

## Related

- `docs/solutions/conventions/an-endpoint-success-report-is-not-a-write.md` —
  the write-side sibling: a response field that reports intent rather than
  outcome. Same root posture: verify the effect, not the claim.
- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md` —
  the general class of checks whose passing looks like their breaking.
- `docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md` —
  measure before designing; here, measure before believing.
- Tickets: BUI-533 (the guard), BUI-759 (the trigger gap — **closed** 2026-08-12,
  markers widened to `hand`/`manual`/`manually` case-insensitively),
  **BUI-775** (the eligibility gate — the guard still protected 1 of 12 after
  BUI-759; keyed on the write's own identity, closed 2026-08-12),
  BUI-720 (found the original gap), BUI-717 (the vintage-query instance),
  **BUI-766** (the 23-guard audit that produced the failure-mode taxonomy),
  BUI-770 (`_marker_hit` typography under-coverage), BUI-769 (`fmv.provenance`
  column — the durable fix for prefix-as-provenance), BUI-777 (guard key vs
  write key: expose `variant`, close the TOCTOU window).
