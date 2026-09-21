---
date: 2026-09-21
topic: slab-and-raw-fmv-depth
origin: docs/brainstorms/2026-09-20-cgc-slab-support-requirements.md
---

# Slab and Raw FMV Depth

## Summary

Make more slab rows price, and make every priced row say what it rests on. Age slab comps against the calendar, measure the raw path before doing the same there, replace the collapsed confidence label with provenance tags in the table and brief, and deepen the slab comp pools for a small watch set of books worth slabbing instead of the whole wish list.

## Problem frame

After the 2026-09-21 punt batch (BUI-922, 938, 939, 940, 946) the eight spike listings went from three punts to one. The corpus behind them is still thin. Across 56 slab pools and 204 grade rungs, 42% of rows refuse to price and 65% of rungs hold fewer than two sales. Of the priced rows, 86% carry LOW confidence, and every one of those is LOW because the pool has fewer than three sales, not because prices disagree (median coefficient of variation 24%).

Two findings changed the design. First, on a certified row the bid factor is 0.80 at every confidence label, because certified rows carry no photo grade confidence and the factor only reads the label when one is present. A label upgrade on a slab moves no money. Only two transitions matter there: a refused row that prices, and a ladder row that reaches the exact tier. On a raw row that went through photo grading the label still drives the haircut (LOW 0.60, MEDIUM-LOW 0.70), so raw provenance has to name that driver. Second, the pool ages every comp against its own newest sale, not the calendar. A pool whose newest sale is eight months old treats that sale as fresh. On the corpus 36% of comps are over-weighted this way, and re-aging on the calendar drops the number of rungs with two or more sales from 71 to 44. The current numbers overstate depth.

The 2026-09-20 requirements ruled out scheduled comp collection because it would spend provider quota across 946 wish-list books. That reasoning holds for the whole list and fails for a subset: the user does not buy a slab worth under $100, and only 65 wish-list books carry a raw FMV high at or above that line.

## Key decisions

- **Calendar aging on the graded path first.** The reference date for slab comp aging becomes an explicit as-of date the runner supplies, not the pool's newest sale. Tests pass a fixed date, so the math stays clock-free and fixtures stay stable. This reverses the BUI-925 design on purpose: determinism was the goal, and an injected date keeps it. Ships first, because every later slab measurement depends on it. The raw path keeps its BUI-287 newest-comp half-life until a measurement on the raw sentinel books shows how far calendar aging moves a raw band; the 36% figure came from slab ledger pools, and the raw pool is live-only inside the provider's 90-day window, so its drift is bounded and unmeasured. The 2026-09-20 promise that no raw price changes holds until that number exists.

- **Provenance tags, not a better label.** The table and brief show what a row rests on: sale count at the exact grade, rung count on a ladder, page-quality widening, ledger drops, lone-sale bracketing, and the active-ask ceiling. On a raw row with a photo grade the tags also name what set the haircut. The LOW, MEDIUM, and HIGH label stays stored for the bid factor and is no longer the thing the reader is asked to trust. The tags serve two reads: the Buy It Now stop, where the user decides by hand, and the auction approval gate, where the user overrides the bid. Pick the token set for those two reads, not for completeness.

- **A slab watch set, not the wish list.** Scheduled slab comp collection runs over books whose raw FMV high is at or above $100 (about 65 today), plus books the user marks by hand with a slab_watch flag. A book can also be removed by hand. The threshold is configurable. Raw understates a vintage slab by about half, so this line admits slabs worth about $200 and up; the user chose it over a $50 raw line (about 212 books) on 2026-09-21, and a slab between $100 and $200 enters by the hand flag. The selector is the highest raw FMV high across a book's fmv rows at any grade, hand-priced or pooled, matched to the wish list by identity in Python (the wish list is a JSON store, not a SQL table) and excluding owned books. A wish-list book with no raw FMV row never qualifies by threshold (140 of 946 today) and enters only by the hand flag. The 2026-09-20 no-scheduled-collection decision stands for the rest of the list.

- **Collection every four weeks with a budget guard and a heartbeat.** One launchd job, same shape as the sentinel probe, fetches slab comps for the watch set and posts them to the ledger. The provider returns eBay's 90-day sold window with no date parameter, so a four-week cadence sees every sale at least twice and costs a quarter of what weekly would. One CGC query per book (CBCS only by hand flag) puts a run near 65 requests, minutes under the BUI-701 pacing. The job caps requests per run and pings the heartbeat contract only when the fetch and the ledger write both succeeded (the BUI-593 lesson). Depth per book is bounded by the 365-day exclusion, so the pools plateau after a year rather than grow forever.

- **Two pool-shape changes wait on a measurement after aging.** A lone exact sale bracketed by rungs within a 3x ratio prices at 0.70 with a tag (23 rungs today), and a two-rung ladder prices at the ladder tier (28 rungs today). Both counts were taken before calendar aging and shrink after it. Each ticket re-measures first and cancels if fewer than ten rungs benefit; a canceled ticket reopens if the count after the first collection cycles clears ten.

- **Active asks show on refused rows and never enter a pool.** A refused row shows the lowest current Buy It Now ask for the same identity as a tagged ceiling, on both paths. Display only, like the ungraded anchor (BUI-712): it moves no guard and sets no cap.

- **Excluded rows stay excluded (BUI-947).** A ledger row the graded guards drop at fetch time leaks back into the pool once its listing ages out of the provider window and no fetch reports it (docs/solutions/best-practices/a-fetch-time-exclusion-is-undone-by-an-archive-that-re-admits.md). Stamping the archive closes that, and it rides with the collection work because both touch the ledger merge.

- **Measured no-go, not to reopen.** Ladder curve fitting: no pool fits at R squared 0.9 and 11 of 12 ladders are non-monotone. Neighbour-rung scaling: disagrees with the observed rung by more than 15% in 59% of cases. One-rung extrapolation: out, it has no bracket. Signature Series pricing stays a loose ticket (BUI-945). The GoCollect Pro trial is parked, not filed.

## Requirements

- R1. The graded path ages comps against an as-of date the runner passes in; the spec records the change and the reason.
- R1a. The raw path switches to the same as-of aging only after a measurement on the raw sentinel books records how many bands move and by how much.
- R2. Every priced or refused row in the fmv table and brief carries provenance tokens a reader can act on without opening the notes.
- R3. The comics server can list the slab watch set and accept a hand override in either direction.
- R4. A weekly job deepens the watch set's slab pools in the ledger and reports through the heartbeat contract.
- R5. Lone-sale bracketing and the two-rung ladder each ship or cancel on a post-aging count.
- R6. Refused rows show an active-ask ceiling that no pricing rule reads.
- R7. Rows the graded guards exclude stay excluded once their listing has aged out of the fetch window (BUI-947).

## Success criteria

The baseline today, on the 56-pool offline corpus, is 42% of rows refused and 65% of rungs under two sales. Calendar aging raises both by design (the rung share lands near 78%), so the numbers measured right after it lands become the baseline every later wave is judged against. Measure on that same corpus each time, report the watch-set subset and the eight spike listings separately, and set a numeric target per metric once the post-aging numbers exist. The spike set stays at no more than one punt in eight.

## Scope boundaries

Deferred: raw-path calendar aging until its drift is measured (R1a), GoCollect Pro as a certified-only provider ($19.99 a month, per-sale export), Signature Series pricing (BUI-945), any raw-path scheduled collection.

Outside this effort: a bid factor that varies by label, any exclusion rule justified by pool shape alone (see the oracle-ceiling practice doc), and dedupe of flipped slabs by cert number.

## Outstanding questions

- Deferred to the raw measurement ticket: whether the raw path keeps its 75-day half-life once the reference is the calendar, or moves to the graded step weights. Measure both on the raw sentinel books before choosing.
- Deferred to the aging ticket: whether the graded path keeps its hard 365-day exclusion once anchored to the calendar. Under the newest-comp rule a lone 13-month-old exact sale still prices; on the calendar it drops and the row refuses with no new data. Count the priced rows that would flip before deciding.
- Deferred to the aging ticket: the as-of date applies when a row is computed. A cached row served inside the reuse window keeps the weights of its compute date, which the 30-day staleness advisory already tolerates.
- Deferred to the watch-set ticket: where the hand override lives (a column on comics or a small table). Either satisfies R3.
