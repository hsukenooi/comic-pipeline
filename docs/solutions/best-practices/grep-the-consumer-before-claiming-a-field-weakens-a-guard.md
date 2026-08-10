---
title: "A stored field's blast radius is its readers — grep the consumer before claiming it weakens a guard"
date: 2026-08-10
category: best-practices
module: "plugins/gixen-overlay (policy.py _priceable/over_fmv/recomputed_cap); apps/fmv (fmv_math clean_round, max_bid)"
problem_type: best_practice
component: development_workflow
severity: medium
mechanized_by: test
enforced_by_test:
  - plugins/gixen-overlay/tests/test_policy_checks.py::test_null_fmv_high_row_excluded_from_priced_sum
  - plugins/gixen-overlay/tests/test_policy_checks.py::test_recomputed_cap_stored_low_caps_at_070
  - apps/fmv/tests/test_fmv_math.py::test_rounding
applies_when:
  - "A ticket claims a stored field is polluted and therefore weakens a downstream guard or money path"
  - "Sizing the impact of a data-quality defect before designing a cleanup or exclusion rule"
  - "A field looks wrong on the dashboard ($0, blank, placeholder) and the wrongness is assumed to propagate"
symptoms:
  - "A row count of bad values is cited as the impact, with no consumer read-site named"
  - "A $0 or placeholder value is read as pollution rather than a rounding/display artifact"
related_components:
  - "fmv"
  - "policy"
tags:
  - premise-verification
  - blast-radius
  - fmv-low
  - clean-round
  - money-guard
  - bui-717
  - oracle-bound
---

# A stored field's blast radius is its readers — grep the consumer before claiming it weakens a guard

## Context

BUI-717 was filed on a premise that read as obviously true: 21 production `fmv` rows
store `low = $0`, and `fmv` rows feed the pre-trade money guards, so those 21 rows
must be weakening `over_fmv` / `recomputed_cap`. The ticket proposed a cross-title
exclusion rule to stop producing them. Nobody had checked whether `fmv.low` is an
input to any guard. It is not — every guard reads `high`. The premise died to a
single grep, before any measurement ran; the oracle measurement that followed (all
21 rows, both directions) showed the proposed exclusion changes 1 of 19 measurable
rows and **loosens** the guard on that one. Canceled on measurement (2026-08-10);
the lone genuinely mispriced row became BUI-720.

The false premise had a documented source: the BUI-714 doc
(`modern-cgc-proxy-factor-is-unmeasurable.md`) asserted in passing that "`fmv.low`
is read by the overlay's policy checks" — itself written without the grep, and
stamped `status: corrected` alongside this doc.

## Guidance

Before writing "field X weakens/feeds/breaks behavior Y", grep the consumer of Y
for X. One line, run first, before opening the ticket:

```sh
grep -n "low" plugins/gixen-overlay/src/gixen_overlay/policy.py
```

Every hit is a *confidence-label* string (`"low": RUNG_MEDIUM_CONFIDENCE` at
`policy.py:114`, `"low": RUNG_LOW_CONFIDENCE` at `:354`) or comment prose. Zero
reads of an FMV row's `low`. The actual inputs, all `high`:

```python
def _priceable(rows):                                    # policy.py:256
    return [r for r in rows if r.get("high") is not None]

summed_high = sum(r["high"] for r in priceable)          # policy.py:301  (over_fmv)
"cap": _rung_for_confidence(r.get("confidence")) * r["high"],   # policy.py:342 (recomputed_cap)
max_bid = clean_round(fmv_high * factor)                 # fmv_math.py:1343, BASE_BID_FACTOR=0.80
```

Three habits:

1. **Grep the consumer, not the producer.** A field's existence, its column
   comment, and its writer tell you nothing about whether anything downstream
   reads it. `fmv.low` is written on the standard path and the CGC-proxy path,
   is shown on the dashboard, and is a real input to `anchor_diverges` — none of
   which is a money guard. (The mirror discipline — read the WRITER before
   reverse-mapping a stored enum — is
   `docs/solutions/logic-errors/stored-label-collapse-reverse-mapping-2026-08-03.md`;
   together they say: a column's meaning lives at its read/write sites, not in
   its name.)
2. **When the premise survives the grep, still price the change.** Measure the
   oracle bound over the full affected population, in **both** directions, and
   read it as a magnitude, not a row count — the full recipe is
   `docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md`.
   That step mattered here even after the grep: it turned "small win" into "no
   win, wrong sign" (the cross-title comps sat *below* their pool medians, so
   excluding them raises caps).
3. **Explain the artifact before proposing to exclude the data that produced
   it.** The $0 lows are not bad comps; they are `clean_round`'s step ladder
   doing what it says:

```python
def _clean_step(value):        # fmv_math.py:679
    if value < 50: return 5    # → any Q25 < $2.50 rounds to $0
```

18 of the 19 measurable rows were $2–$7 books whose raw Q25 sat between $1.34
and $5.73. A $0 low is manufactured by the $5 step, not by pollution.

## Why This Matters

These checks gate real money. A false premise proposes changes to `over_fmv` and
`recomputed_cap` — the advisories standing between a computed cap and a live
bid — on the basis of a field they never read. And the change had the wrong
sign: it would have *raised* the effective cap on the one row it touched, to fix
a cosmetic artifact of $5-step rounding on a $3 comic.

It also bounds the one FMV exclusion family with a surviving track record.
Eleven pool-shape proposals have been Canceled on measurement; BUI-675's rule is
that ship-worthy exclusions come from a **contradiction in the data**, never a
pool-shape hypothesis. BUI-717 arrived dressed as a contradiction class ("$0 low
contradicts a $7 sale") and is the **first contradiction-class candidate to
dissolve**. Membership in the surviving family buys a hearing, not an exemption:
which-side-of-the-median and the magnitude bound still apply.

## When to Apply

- Any ticket whose premise is "stored field X is degrading behavior Y" — before
  filing, not before fixing.
- Any proposed FMV exclusion, filter, or pool-shape rule (eleven Canceled, plus
  two Canceled publish-tier variants, plus this).
- Any data-cleanup ticket justified by downstream harm rather than by the data
  itself.
- Before proposing a `POLICY_*` threshold change: confirm which fields the five
  checks in policy.py's `_CHECKS` tuple actually read.

## Examples

**Before (the filed premise):** "21 rows have `low = $0`. `fmv.low` is a
money-guard input, so `over_fmv` and `recomputed_cap` are under-protecting on
those bids. Fix: exclude the cross-title comps that produce them."

**After (one grep, ~10 seconds):** `grep -n "low" policy.py` returns 13 hits,
all confidence-label strings or comments. `_priceable` gates on
`high is not None`; `over_fmv` sums `high`; `recomputed_cap` is rung × `high`;
`max_bid` is 0.80 × `fmv_high`. The 21 rows cannot weaken any guard because no
guard reads the field. Follow-up measurement across all 21, both directions:
1 of 19 measurable rows changes, toward a *looser* cap. Canceled on
measurement, not opinion.

## Related

- `docs/solutions/logic-errors/stored-label-collapse-reverse-mapping-2026-08-03.md` —
  the mirror: read the WRITER before reverse-mapping a stored value. This doc is
  the consumer-side companion.
- `docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md` —
  the measurement recipe that runs *after* the premise survives the grep.
- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` —
  the general premise-check discipline; BUI-717 is its newest example row.
- `docs/solutions/best-practices/modern-cgc-proxy-factor-is-unmeasurable.md` —
  the doc whose aside seeded the false premise (stamped corrected).
- Tickets: BUI-717 (Canceled on measurement), BUI-720 (the N=1 residual
  reprice), BUI-675 (the contradiction-class rule), BUI-629/637/646/667 (the
  oracle/magnitude/direction recipe).
