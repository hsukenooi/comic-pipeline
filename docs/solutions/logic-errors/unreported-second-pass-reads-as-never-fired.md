---
title: "An unreported second pass reads as a fetch that never fired, and a target-gated guard leaves the sibling pass unguarded"
date: 2026-09-21
category: logic-errors
module: "apps/fmv (src/fmv_runner.py — _record_graded_pass, _drop_multibook_graded_lots, _is_multibook_graded_lot, _slab_comps_only; src/fmv_math.py — monotonicity_violations, cgc_cross_check)"
problem_type: logic_error
component: fmv_pipeline
severity: high
related_components:
  - "fmv_pipeline"
applies_when:
  - "A live-repro ticket claims a code path 'never fires' — confirm the pass is RECORDED in the emitted output before chasing the predicates that gate it; an unrecorded pass that ran and was refused is byte-identical to one that never ran"
  - "A guard or exclusion rule was added for one target type (a certified target) — check whether the sibling pass over the other target type (an include_graded second pass on a raw target) reaches the same data class and needs the same guard"
  - "A multi-item listing is parsed into one price at one grade — check whether it can violate an assumed monotonic sequence and zero out a whole pool"
symptoms:
  - "Three comic-fmv runs on a raw vintage book (Batman #227, 1970) show only the raw query in queries_used, never a graded one, although every documented gate passes and a hand-run graded fetch returns 38 comps"
  - "A two-book lot listing parses into a single 4.0 @ $1,399.99 point above the genuine 4.5/5.5/6.0 rungs, so monotonicity_violations refuses the whole CGC ladder and both graded tiers return None"
  - "slab_comps is empty and cgc_cross_check is null on every run, even though the graded fetch executed twice"
root_cause: logic_error
resolution_type: code_fix
mechanized_by: test
enforced_by_test:
  - apps/fmv/tests/test_fmv_runner.py::test_graded_query_slab_comps_and_verdict_all_land
tags:
  - "fmv-pipeline"
  - "cgc-ladder"
  - "multibook-lot"
  - "monotonicity-violation"
  - "unreported-pass"
  - "observability"
related_issues: [BUI-921, BUI-922, BUI-929, BUI-961]
---

# An unreported second pass reads as a fetch that never fired, and a target-gated guard leaves the sibling pass unguarded

## Problem

`comic-fmv`'s CGC-proxy rescue (BUI-348) and always-on cross-check (BUI-529) both run a graded-only second fetch. One bad comp could make that pass discard its whole ladder, and the pass wrote nothing to the emitted row, so a fetch that fired and was refused looked identical to a fetch that never ran. BUI-921 was filed on the wrong premise ("never issues the graded-only fetch") and its own trace could not have found the bug.

## Symptoms

- Three live runs on Batman #227 (1970), 2026-09-19: `slab_comps: 0`, `cgc_cross_check: None`, and `queries_used` holding exactly one entry each time, the raw `-cgc -cbcs -graded -slab` query.
- A hand-run graded fetch found 38 comps, far above `CGC_PROXY_MIN_LADDER_COMPS`, and the raw band ($850–1200) diverged from the certified 6.0 sale ($900), exactly the divergence the cross-check exists to catch.

## What Didn't Work

The ticket traced every gate in front of the fetch (`_is_vintage`, `_is_thin_or_low_confidence_priced`, `_is_unpriced_raw`) and found them all passing. That diagnosis checked whether the fetch *fired*, not what happened *after* it fired. It could not succeed: `queries_used` had no graded-pass entries to inspect because nothing ever wrote them there, whether the pass succeeded or not. The offline reproduction against the cached provider responses (`~/.cache/ebay-sold-comps`) showed `_fetch_comps` called twice on every run the ticket cited.

## Solution

Two independent defects, fixed together in `apps/fmv/src/fmv_runner.py` (commit 000053b, PR #538).

**Guard the pass, not just the target.** One cached comp, `Batman #227 CGC 4.0 … & Batman #232 CGC 6.0 …`, priced two books under one parsed grade (4.0 @ $1,399.99) above the genuine 4.5/5.5/6.0 rungs, and `fmv_math.monotonicity_violations` refused the whole ladder as inverted. BUI-922 had already written a guard for this shape, but gated it on `graded_target` (a certified target, BUI-929); the proxy/cross-check pass runs `include_graded` on a *raw* target and never reached it. The fix restates the rule locally (`_is_multibook_graded_lot`, regexes copied verbatim because `comic-fmv` does not import eBay code; 0 disagreements with `sold_comps` over 16,033 cached titles, fires on exactly 1) and applies it through `_drop_multibook_graded_lots`, both inside `_slab_comps_only` and before the `CGC_PROXY_MIN_LADDER_COMPS` count on the reused BUI-524 ladder in `_apply_cgc_cross_check`, so a lot can never clear the floor on a rung that is not one.

**Report the pass.** Before the fix the graded pass's queries and comps were computed and then used only to decide the next step:

```python
# before: computed, used for the decision, never attached to the row
graded_comps = _slab_comps_only(result.get("comps") or [])
```

After, `_record_graded_pass(row, result, ladder)` appends the pass's `queries_used` and merges its ladder into `row["slab_comps"]` (deduped on `product_id`), called right after the fetch in both `_apply_cgc_proxy_rescue` and `_apply_cgc_cross_check`, before any branch can `continue` past it. `comp_count_total` is deliberately left alone: `_is_fetch_error` (BUI-143) reads it as the *raw* pool's size, and folding a graded pass into it could misclassify a healthy raw fetch as a provider outage.

Post-fix on the ticket's book: the graded query is emitted, `slab_comps: 11`, and `cgc_cross_check` diverges at 51% (slab 6.0 $900 → implied raw $475 vs raw median $975). Verified live on the deployed build the same day.

## Why This Works

Two bugs stacked so that each hid the other's symptom. The ladder-poisoning bug made the pass produce nothing usable; the missing-report bug made "produced nothing" and "never ran" indistinguishable in the output. Fixing only the guard would have left the next contaminated comp undiagnosable; fixing only the reporting would have surfaced a pass that still priced nothing.

The two reusable traps:

- **An unreported second pass reads as "never fired."** Every pass of a multi-pass pipeline must leave a trace in the emitted output. Reporting is not optional plumbing; it is the only way an operator or a later session can tell "ran and was refused" from "did not run". This is the fifth question to add to the four in `a-shipped-guard-is-not-a-running-guard.md`: is the result *recorded*?
- **A guard keyed on target type leaves the sibling pass unguarded.** Multi-book-lot contamination is a property of the *comp*, not the *target*. Any `include_graded` pass can meet it, certified or raw, so the guard belongs on the data class, not on the caller.

## Prevention

- The regression test `TestRunCrossChecksAThinPricedVintageBook::test_graded_query_slab_comps_and_verdict_all_land` asserts on the **reported trail** (`row["queries_used"]`, `row["slab_comps"]`, a non-null `cgc_cross_check`) at the `run()` level, not on the fetcher's call count. On the unfixed code `fetch_count == 2` already passed while `len(queries_used) == 1`; asserting on the trail is what makes the test fail on the bug. Write the test that would have caught the *diagnosis* failure, not only the pricing failure.
- When adding a data-shape guard (lot detection, malformed comp, cross-title identity), key it on the data class it protects against, never on which caller or target type happens to invoke the pass.
- Before chasing gate predicates on a "never fires" report, check whether the pass is recorded anywhere. If it is not, add the recording first; the trace then tells you which predicate, if any, is at fault.
- Follow-up BUI-961 moves BUI-922/938's lot and cross-title guards into `apps/ebay` so every `include_graded` pass gets them, closing the DRY gap this fix worked around locally.

## Related Issues

- BUI-921 (this fix, PR #538), BUI-348 (proxy rescue), BUI-529 (cross-check), BUI-922/BUI-924/BUI-929 (lot guard origin and certified-target gating), BUI-674/BUI-676 (comps-ledger posting this shares plumbing with), BUI-961 (DRY follow-up).
- `docs/solutions/best-practices/a-shipped-guard-is-not-a-running-guard.md`: the inverse polarity (believed to fire, does not); this doc adds "fires, result unrecorded".
- `docs/solutions/best-practices/a-fetch-time-exclusion-is-undone-by-an-archive-that-re-admits.md`: same file and same day, a different mechanism by which a correct guard's effect is lost downstream.
- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md`: the general class; absent evidence is not a proven negative.
- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md`: BUI-921 is one of three wrong premises in the same batch.
