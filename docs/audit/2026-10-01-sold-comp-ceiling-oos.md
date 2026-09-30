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

## Results

**Source:** `uv run --python 3.12 docs/audit/2026-10-01-sold-comp-ceiling-oos.py`, live comics DB opened read-only (`mode=ro`), run pinned to comps first seen before `2026-09-30T17:04` UTC. No provider calls, no writes.

**Decision: SHIP B@0.60** (behind a flag, default off). It is the only arm that passes every gate on both halves. B@0.80 passes in-sample and fails both exposure gates out of sample; B@1.00 fails both halves.

### Out-of-sample population

- 880 graded raw comps first seen after the cutoff, all from one `comic-fmv` run on 2026-09-30 (06:11 to 06:24 UTC). 18 are copies of pre-cutoff sales and are dropped. 755 become held-out sales (sold 2026-07-03 to 2026-09-29).
- Outcome: 464 priced, 93 `one_sided`, 111 `too_wide`, 87 `too_sparse`. Of the 204 shape refusals, 131 (64%) get a ceiling pool: 47 `one_sided` and 84 `too_wide`. 46 `one_sided` pools sit entirely below the target and 27 `too_wide` pools have one comp above.
- **Minimum met:** 131 capped sales (minimum 100) from 49 books (minimum 30), and 464 accepted sales (minimum 100).

### Scores

Mean and P90 overpay are in dollars, `max(0, cap − price)` over all capped sales. Caps are floored to the clean step.

| Half | Arm | Capped | Mean overpay $ | Overpay $ P90 | Overpay rate | Room lost | Median cap/price | $0 caps |
|---|---|---|---|---|---|---|---|---|
| OOS | **B@0.60** | 131 | **3.90** | **11.91** | 22.1% | 77.1% | 0.58 | 16 |
| OOS | B@0.80 | 131 | 11.03 | 25.01 | 38.2% | 61.1% | 0.80 | 9 |
| OOS | B@1.00 | 131 | 20.91 | 57.29 | 51.9% | 45.8% | 1.00 | 6 |
| OOS | Accepted path | 464 | 8.44 | 20.00 | 45.9% | 51.3% | 0.97 | 0 |
| In-sample | **B@0.60** | 752 | **5.46** | **8.00** | 19.8% | 79.0% | 0.51 | 161 |
| In-sample | B@0.80 | 752 | 10.27 | 22.00 | 33.9% | 64.8% | 0.74 | 138 |
| In-sample | B@1.00 | 752 | 17.59 | 42.00 | 46.0% | 52.4% | 0.95 | 106 |
| In-sample | Accepted path | 2,398 | 15.30 | 26.00 | 56.9% | 40.7% | 1.07 | 0 |

E1 gap (arm − accepted, mean overpay $, 95% book-clustered CI) against the limit of +10% of the accepted mean:

| Arm | OOS gap (limit +0.84) | In-sample gap (limit +1.53) | Verdict |
|---|---|---|---|
| B@0.60 | −4.55 (−10.39 to −0.44) | −9.84 (−19.44 to −4.66) | Pass both halves |
| B@0.80 | +2.59 (−4.50 to +10.16), fails E1 and E2 | −5.03 (−12.63 to −0.29), passes | Cancel |
| B@1.00 | +12.47 (+3.08 to +24.21), fails E1 and E2 | +2.29 (−3.42 to +6.92), fails E1 and E2 | Cancel |

T1 coverage (64% OOS, 65% in-sample) and T5 cap/price pass for every arm.

### What shipping B@0.60 buys

A bid on about two thirds of the shape-refused books that today get none, at a cap that wins roughly one auction in five (overpay rate 22%, which in a second-price auction is mostly the win rate) and exposes less money per capped sale than the accepted path does on its own books. Most capped auctions (77%) are lost, and 16 of 131 OOS caps floor to $0, so the rule is a conservative floor bid, not a pricing fix.

## Review notes

- **Order:** the gates section was committed (`1d3762f`) before the scorer existed or ran.
- **Leakage, first seen:** the script asserts every OOS sale's own comp was first seen at or after the cutoff. Copies of pre-cutoff sales (same book, price within $0.01, sold within 7 days, any grade) are dropped: 18. The ceiling pool also drops such near-copies within the post-cutoff set (BUI-1025's `ceiling_pool`, unchanged).
- **Leakage, caps:** the accepted-path cap and every arm use only comps sold strictly before the sale (the harness's `training`), so no cap sees its own sale or a later one. Training can include comps first seen after the cutoff but sold before the sale, the same for arms and yardstick.
- **Rounding:** every cap is floored; the script asserts no cap exceeds its own ceiling, closing BUI-1025's `clean_round` money trap.
- **Caveats:** the OOS half is one 13-minute fetch batch (the 2026-09-30 re-run of previously unpriced rows), not a calendar sample, so it leans toward books the current path refused, and its accepted-path yardstick covers only 52 books (mean overpay $8.44 against $15.30 in-sample). In-sample T5 passes at 0.51 against a 0.50 floor. The live re-run after deploy is the gate that proves the pool change in production.

## Next step

Implementation is a separate change on a fresh `main`: a `ceiling` pricing basis behind a flag, default off; the `pricing_basis` CHECK migration (like `lone_sale`'s); caps floored, never `clean_round`; and a live re-run after deploy.
