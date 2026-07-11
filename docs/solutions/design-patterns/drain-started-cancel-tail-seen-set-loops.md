---
title: "Parallelizing a seen-set loop: drain every started worker, cancel only the not-yet-started tail"
date: 2026-07-11
category: docs/solutions/design-patterns
module: seller-scan (apps/ebay)
problem_type: design_pattern
component: service_object
related_components:
  - seller-scan
  - comic-wishlist-sellers
severity: high
applies_when:
  - "Parallelizing a loop whose iterations record persistent \"seen\" state as a side effect"
  - "A global/fatal failure mid-batch tempts an early break out of a worker pool"
  - "A later run is seen-filtered and will hide anything a dropped iteration already marked seen"
tags: [concurrency, seen-set, thread-pool, data-loss, cancellation, seller-scan, drain]
---

# Parallelizing a seen-set loop: drain every started worker, cancel only the not-yet-started tail

## Context

BUI-307 (PR #143) parallelized seller-scan's per-seller verification with a bounded `ThreadPoolExecutor`, replacing a strictly-sequential loop where one stuck 180s verify timeout blocked every seller behind it. Each seller's scan records its genuine wish-list matches as **seen** (`record_items_seen`) *before* returning — that seen-set is what a later run filters against to avoid re-surfacing the same book. This combination — a parallelized loop whose iterations have a persistent seen-set side effect — has a non-obvious data-loss trap on the failure path. This is a real BUI-297-class near-miss (silent lost wish-list match) that adversarial review caught.

## Guidance

**On a global/fatal failure mid-batch, drain all *started* workers and cancel only the *not-yet-started* tail. Never early-`break` out of the completion loop.**

The instinct on a global failure (e.g. the verifier is down for every seller, so fanning out the rest is pointless) is to `break` out of the `as_completed` loop. That is the bug: a worker that already finished (or is mid-flight) has **already recorded its matches as seen**. If you break before collecting that worker's result, the caller emits nothing for it — yet the seen-marks persist. The re-run that the failure prompts is seen-filtered, so it finds those item_ids already seen and hides them. The match is silently lost forever. This is exactly the BUI-297 lost-match class.

The safe shape:
- Keep draining `as_completed` to the end — collect every future that actually **ran** (including the one that raised the global failure; record its error slot).
- Cancel only the futures that have **not started** (`future.cancel()` succeeds only on not-yet-running futures). A cancelled future recorded nothing, so its empty slot drops out cleanly.
- Let the started-but-cancelled-attempt futures finish and be collected on later `as_completed` turns.

The invariant: **anything that ran and recorded a side effect must have its result collected; only work that recorded nothing may be dropped.**

## Why This Matters

The bug is invisible in the happy path and even in most failure tests — it only bites when a global failure lands *after* at least one worker has recorded seen-state, and the loss shows up a run later as "a wish-list match that should have appeared never did." Sequential code didn't have this trap (a `break` after a sequential iteration only skips *future* iterations, which recorded nothing); parallelism introduces it because started work outlives the decision to stop. Any parallelization of a loop with persistent per-iteration side effects inherits this exact hazard.

## When to Apply

- Converting a sequential loop with persistent per-iteration side effects (seen-sets, dedup marks, cursor writes, partial-commit rows) to a worker pool.
- Adding a fatal/global short-circuit to an existing worker pool — audit whether started workers have already committed state that the short-circuit would strand.
- Any dedup/seen-filtered pipeline where "we showed it once" is recorded before the surfacing to the user is confirmed complete.

## Examples

The trap and the fix (seller-scan's `as_completed` drain):

```python
verifier_down = False
with ThreadPoolExecutor(max_workers=N) as executor:
    future_to_meta = {executor.submit(_scan_one_seller, ...): meta for ...}
    for future in as_completed(future_to_meta):
        try:
            slots[idx] = future.result()          # collect EVERY started worker
        except CancelledError:
            continue                              # never started, recorded nothing → drop
        except SystemExit as e:                   # global verifier failure
            verifier_down = True
            slots[idx] = _seller_result(..., error="verifier globally unavailable")
            for f in future_to_meta:
                f.cancel()                         # cancels only not-yet-started futures
            # NO break — keep draining; started workers already recorded seen-state
```

Wrong version (the near-miss):

```python
        except SystemExit:
            break   # BUG: strands started workers that already record_items_seen()'d
                    #      → seen-filtered re-run hides those matches → silent loss
```

## Related

- **BUI-307** — this work (PR #143): bounded-pool per-seller verification with the drain-all / cancel-tail failure path.
- **BUI-297** — the lost-match class this preserves (a dropped-but-seen wish-list match a seen-filtered re-run then hides).
- **BUI-298** — the global-verifier-down reliability contract (`sys.exit` → future exception) that this failure path handles.
- `docs/solutions/developer-experience/cross-package-regressions-escape-per-package-test-runs.md` — sibling seller-scan reliability learning.
