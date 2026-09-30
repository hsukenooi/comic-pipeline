---
title: "Sold-comp ceiling, out-of-sample re-test with an exposure gate (BUI-1028)"
date: 2026-10-01
---

# Sold-comp ceiling, out-of-sample re-test with an exposure gate (BUI-1028)

**Date:** 2026-10-01. **Follows:** `docs/audit/2026-09-30-sold-comp-ceiling.md` (BUI-1025), which canceled on a conditional overpay-P90 gate it then showed to be ill-posed. This ticket re-tests the same rule with gates written before scoring.

## Gates (declared before scoring)

This section is committed before the scorer runs; the commit history shows the order.

### Rule under test

On a `one_sided`-above or `too_wide` refusal with at least two comps at or above the target grade (BUI-1025's ceiling pool, unchanged), cap the bid at `factor × ceiling`, where the ceiling is variant B: the median sale price of the lowest grade rung in the pool. The cap is **rounded down** to the clean step (`floor(v / step) × step`, with `fmv_math._clean_step`), never `clean_round` to nearest, so no cap exceeds its own ceiling. A cap that floors to $0 counts as capped and loses the auction.

Arms: B at factor 0.60, 0.80, and 1.00, all rounded down.

### Populations

- **In-sample:** BUI-1025's population exactly: BUI-1005's leave-one-out sales, every ledger read pinned to comps first seen before `2026-09-30T01:00` UTC.
- **Out-of-sample (OOS):** held-out sales from the same harness whose own comp row was first seen at or after `2026-09-30T01:00` UTC. Training for an OOS sale may use any comp dated before the sale, first seen before or after the cutoff, as production would. An OOS sale is **dropped** if any comp first seen before the cutoff on the same book has a price within $0.01 and a sold date within 7 days, at any grade (a provider copy of an in-sample sale is not new evidence). Every read is also pinned to comps first seen before the run's start time, recorded in the results.
- **Yardstick:** the accepted path's own `max_bid` from the unchanged `compute_fmv` (no photo `grade_confidence`) on the accepted (unflagged) sales of the **same** population: OOS arms against OOS accepted, in-sample against in-sample.

### Minimum sample (below this, the OOS result is inconclusive)

- At least **100 OOS capped refused sales**, from at least **30 distinct books**, and at least **100 OOS accepted sales**. With 100 capped sales, the dollar P90 rests on the top 10; below that, one sale moves it.
- If OOS falls short, OOS is not scored for a verdict. The doc records the count, the in-sample re-run with these gates, and **INCONCLUSIVE** with the date by which the minimum should be met at the observed arrival rate.

### Gates (every one must pass, per arm)

- **E1, mean exposure:** mean overpay in dollars per capped sale, `mean(max(0, cap − price))` over **all** capped sales, is at most the accepted path's, and the upper bound of the 95% book-clustered bootstrap CI (seed 1028, 1,000 reps) on the gap (arm − accepted) is at most **+10% of the accepted path's mean**.
- **E2, tail exposure:** the P90 of `max(0, cap − price)` in dollars over **all** capped sales (zeros included) is at most the accepted path's.
- **T1, coverage:** the arm caps at least 20% of the refused sales (BUI-1025's T1).
- **T5, usefulness:** median cap / realized price is at least 0.50 (BUI-1025's T5), so the cap wins something.

The conditional overpay P90 (BUI-1025's T3) and the overpay rate (T2, T4) are reported, not gated.

### Decision rule

- **SHIP** the highest-factor arm that passes every gate on OOS and on in-sample. A pass on one half and a fail on the other is a CANCEL for that arm.
- **CANCEL** if no arm passes both halves.
- **INCONCLUSIVE** if the OOS minimum is not met, whatever the in-sample result.
