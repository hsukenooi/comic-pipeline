---
date: 2026-09-21
topic: slab-and-raw-fmv-depth
origin: docs/brainstorms/2026-09-20-cgc-slab-support-requirements.md
---

# Slab and Raw FMV Depth

## Summary

Make more slab rows price, and make every priced row say what it rests on. Age comps against the calendar on both paths, replace the collapsed confidence label with provenance tags in the table and brief, and deepen the slab comp pools for a small watch set of books worth slabbing instead of the whole wish list.

## Problem frame

After the 2026-09-21 punt batch (BUI-922, 938, 939, 940, 946) the eight spike listings went from three punts to one. The corpus behind them is still thin. Across 56 slab pools and 204 grade rungs, 42% of rows refuse to price and 65% of rungs hold fewer than two sales. Of the priced rows, 86% carry LOW confidence, and every one of those is LOW because the pool has fewer than three sales, not because prices disagree (median coefficient of variation 24%).

Two findings changed the design. First, the exact tier pins the bid factor at 0.80 for every confidence label, so a label upgrade moves no money. Only two transitions matter: a refused row that prices, and a ladder row that reaches the exact tier. Second, the pool ages every comp against its own newest sale, not the calendar. A pool whose newest sale is eight months old treats that sale as fresh. On the corpus 36% of comps are over-weighted this way, and re-aging on the calendar drops the number of rungs with two or more sales from 71 to 44. The current numbers overstate depth.

The 2026-09-20 requirements ruled out scheduled comp collection because it would spend provider quota across 946 wish-list books. That reasoning holds for the whole list and fails for a subset: the user does not buy a slab worth under $100, and only 65 wish-list books carry a raw FMV high at or above that line.

## Key decisions

- **Calendar aging on both paths.** The reference date for comp aging becomes an explicit as-of date the runner supplies, not the pool's newest sale. Tests pass a fixed date, so the math stays clock-free and fixtures stay stable. This reverses the BUI-287 and BUI-925 design on purpose: determinism was the goal, and an injected date keeps it. Ships first, because every later measurement depends on it.

- **Provenance tags, not a better label.** The table and brief show what a row rests on: sale count at the exact grade, rung count on a ladder, page-quality widening, ledger drops, lone-sale bracketing, and the active-ask ceiling. The LOW, MEDIUM, and HIGH label stays stored for the bid factor and is no longer the thing the reader is asked to trust.

- **A slab watch set, not the wish list.** Scheduled slab comp collection runs over books whose raw FMV high is at or above $100 (about 65 today), plus books the user marks by hand with a slab_watch flag. A book can also be removed by hand. The threshold is configurable. The 2026-09-20 no-scheduled-collection decision stands for the rest of the list.

- **Weekly collection with a budget guard and a heartbeat.** One launchd job, same shape as the sentinel probe, fetches slab comps for the watch set and posts them to the ledger. It caps requests per run, paces under the provider ceiling (BUI-701), and pings the heartbeat contract only when the fetch and the ledger write both succeeded (the BUI-593 lesson).

- **Two pool-shape changes wait on a measurement after aging.** A lone exact sale bracketed by rungs within a 3x ratio prices at 0.70 with a tag (23 rungs today), and a two-rung ladder prices at the ladder tier (28 rungs today). Both counts were taken before calendar aging and shrink after it. Each ticket re-measures first and cancels if fewer than ten rungs benefit.

- **Active asks show on refused rows and never enter a pool.** A refused row shows the lowest current Buy It Now ask for the same identity as a tagged ceiling, on both paths. Display only, like the ungraded anchor (BUI-712): it moves no guard and sets no cap.

- **Measured no-go, not to reopen.** Ladder curve fitting: no pool fits at R squared 0.9 and 11 of 12 ladders are non-monotone. Neighbour-rung scaling: disagrees with the observed rung by more than 15% in 59% of cases. One-rung extrapolation: out, it has no bracket. Signature Series pricing stays a loose ticket (BUI-945). The GoCollect Pro trial is parked, not filed.

## Requirements

- R1. Both FMV paths age comps against an as-of date the runner passes in; the spec records the change and the reason.
- R2. Every priced or refused row in the fmv table and brief carries provenance tokens a reader can act on without opening the notes.
- R3. The comics server can list the slab watch set and accept a hand override in either direction.
- R4. A weekly job deepens the watch set's slab pools in the ledger and reports through the heartbeat contract.
- R5. Lone-sale bracketing and the two-rung ladder each ship or cancel on a post-aging count.
- R6. Refused rows show an active-ask ceiling that no pricing rule reads.
- R7. Rows the graded guards exclude stay excluded once their listing has aged out of the fetch window (BUI-947).

## Success criteria

Re-run the eight spike listings and the watch set after each wave against the baseline: 42% of rows refused and 65% of rungs under two sales, both re-measured after calendar aging lands, since that step alone raises them.

## Scope boundaries

Deferred: GoCollect Pro as a certified-only provider ($19.99 a month, per-sale export), Signature Series pricing (BUI-945), any raw-path scheduled collection.

Outside this effort: a bid factor that varies by label, any exclusion rule justified by pool shape alone (see the oracle-ceiling practice doc), and dedupe of flipped slabs by cert number.

## Outstanding questions

- Deferred to the aging ticket: whether the raw path keeps its 75-day half-life once the reference is the calendar, or moves to the graded step weights. Measure both on the raw sentinel books before choosing.
- Deferred to the watch-set ticket: where the hand override lives (a column on comics or a small table). Either satisfies R3.
