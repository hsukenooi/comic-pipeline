---
title: "A fetch-time exclusion is undone by an archive that re-admits what was stored before the guard (BUI-946 comps ledger)"
date: 2026-09-21
category: best-practices
module: apps/fmv (fmv_runner._merge_slab_pool), apps/ebay (sold_comps.fetch_book_comps), any pool built from a live fetch plus a stored archive
problem_type: best_practice
component: service_object
severity: high
mechanized_by: test
enforced_by_test:
  - apps/fmv/tests/test_graded_pool_guards.py::TestLedgerHonoursGuards
related_components:
  - ebay-sold-comps
  - comic-fmv
  - comps ledger (POST /api/comics/comps)
applies_when:
  - "Adding or tightening a comp exclusion (lot, cross-title, variant, printing, label) that runs on the live fetch"
  - "Pricing from a pool that merges live results with rows stored by earlier runs"
  - "Closing a pool-shape ticket on the strength of unit and replay tests alone"
symptoms:
  - "Every unit and replay test is green, the guard's drop counters are non-zero on the live fetch, and the deployed price is unchanged"
  - "`slab_pool=N (live=L ledger=M)` in fmv_notes shows N equal to the pre-guard pool size while L shrank"
tags: [comps-ledger, exclusions, graded-mode, sold-comps, BUI-946, BUI-922, BUI-938, em-batch]
---

# A fetch-time exclusion is undone by an archive that re-admits what was stored before the guard

## What happened

BUI-922 and BUI-938 added three graded-mode comp exclusions to `ebay-sold-comps` (ampersand multi-book lots, cross-title comps, store variants), each measured over the offline corpus and pinned by replay tests that priced Batman #227 CGC 4.5 at $775 and Invincible #1 CGC 9.4 at $3,650 instead of refusing `ladder_non_monotone`. PR #523 merged green and deployed clean.

The first deployed run refused both books exactly as before. The guards had fired: the live slab comps went from 14 to 12 and from 9 to 5. But `comic-fmv` prices a slab from the live fetch merged with the `pool='slab'` rows in the comps ledger, and the ledger still held the same seven listings, stored by that morning's pre-guard fetches. `_merge_slab_pool` dedupes on `product_id` with live winning, so a listing the guard had just dropped from `live` had no live copy to win, and its ledger copy walked straight back into the pool. Any exclusion applied at fetch time was undone for as long as the listing stayed archived (up to 365 days at half weight).

## The rule

A guard that runs on one source of a merged pool is not a guard on the pool. Either every source passes through it, or the guarded source reports what it rejected and the merge honours that report.

Concretely, for the comps ledger:

1. The fetch reports its rejections by identity, not just by count. `fetch_book_comps` returns `graded_identity_dropped_ids: [{product_id, code}]` beside the existing counters, covering every graded-mode exclusion including the printing guard.
2. The merge skips archived rows carrying a rejected identity. `_merge_slab_pool(live, ledger, *, dropped_ids)` compares product ids as strings (the two sources disagree on type) and only ever removes from the archive side.
3. The row says what happened. `fmv_notes` carries `ledger_dropped=<n>` when non-zero, so a pool that is smaller than the archive is explained rather than merely observed.
4. The residual is named. A listing that has aged past the provider window is never re-fetched, so no fetch reports it; the archive copy leaks until it ages out. That needs the archive itself stamped (BUI-947), and every future guard inherits the same tail until it is.

## How it was found

Not by review and not by tests. The replay tests used the guarded pool on both sides of the merge, which is the one shape production never has. The defect surfaced only because the EM re-ran the deployed build on the same eight listings before reporting the batch done, and the two bands that should have appeared did not. For a pool-shape change, the gate is a live re-run after deploy; the suite proves the math, not the plumbing.
