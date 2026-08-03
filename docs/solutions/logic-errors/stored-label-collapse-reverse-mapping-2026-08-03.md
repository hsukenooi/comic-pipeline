---
title: "Before reverse-mapping a stored label to behavior, read the writer that produced it"
date: 2026-08-03
category: logic-errors
module: "gixen-overlay (src/gixen_overlay/policy.py — _RUNG_BY_CONFIDENCE), apps/fmv (src/fmv_runner.py — _confidence_to_db_label, src/fmv_math.py — bid_factor)"
problem_type: logic_error
component: policy_checks
severity: high
related_components:
  - "fmv_pipeline"
applies_when:
  - "A check, advisory, migration, or report derives thresholds or behavior from a stored enum/label column (e.g. fmv.confidence, a status vocabulary, a source tag)"
  - "The stored vocabulary is narrower than the vocabulary of the pipeline that writes it — any many-to-one _to_db_label-style collapse function exists"
  - "Reviewing a diff that maps label values to numeric constants (a reverse map) without citing the writer function"
symptoms:
  - "An advisory or gate fires on the standard, correctly-computed workflow output — systematic false positives on the most common cohort"
  - "A label→constant map that looks self-evident from the column's CHECK constraint but disagrees with the factor the writer actually applied"
root_cause: logic_error
resolution_type: code_fix
mechanized_by: test
enforced_by_test:
  - plugins/gixen-overlay/tests/test_policy_checks.py::test_recomputed_cap_stored_medium_passes_at_standard_080_bid
  - plugins/gixen-overlay/tests/test_policy_checks.py::test_recomputed_cap_stored_low_caps_at_070
tags:
  - "stored-label-collapse"
  - "reverse-mapping"
  - "fmv-confidence"
  - "recomputed-cap"
  - "bid-factor-rungs"
  - "false-positive-advisory"
  - "policy-checks"
  - "alert-fatigue"
---

# Before reverse-mapping a stored label to behavior, read the writer that produced it

## Context

BUI-620 (2026-08-03) added the overlay's recomputed-cap policy check: recompute the
bid cap from `fmv.high` × a rung constant chosen by `fmv.confidence`, and advise when
`max_bid` exceeds it. The first implementation mapped the column's CHECK-constrained
vocabulary one-to-one onto the rung ladder — `'high'→0.80, 'medium'→0.70, 'low'→0.60`.
It read as self-evident, passed the implementing agent's review, and passed CI.

It was wrong for two entire workflows. The column's **writer**,
`fmv_runner._confidence_to_db_label`, collapses the brief path's finer ladder:
`{MEDIUM-HIGH, MEDIUM} → 'medium'` and `{MEDIUM-LOW, LOW} → 'low'`. Meanwhile
`fmv_math.bid_factor` pays BASE (0.80) for MEDIUM and above, and the CGC-proxy tier is
capped at 0.70 with confidence MEDIUM-LOW — which stores as `'low'`. So:

- every standard medium-confidence bid, computed at 0.80 × high, would trip
  "exceeds the confidence-adjusted cap (0.70 × high)" — a false advisory on the
  single most common priced cohort;
- every CGC-proxy bid, legitimately computed at 0.70 × high and stored `'low'`,
  would trip against a 0.60 cap.

The blast radius was worse than noise: the BUI-623 blocking flags are gated on a soak
review of these advisories (`GET /api/decisions`), so systematic false positives would
have corrupted the very signal that decides whether checks may block money.

## The trap

A stored label that came through a many-to-one writer is **ambiguous per row**. The
CHECK constraint tells you the vocabulary; it tells you nothing about the meaning. A
reverse map built from the label *names* silently picks one branch of the ambiguity —
and the picked branch is usually wrong for at least one large cohort, because the
collapse existed precisely to squeeze several behaviors into one word.

## The fix that shipped (e68bce9, PR #409)

Map each stored label to the **laxest** value the writer's collapsed band contains:
`'high'/'medium'/NULL → 0.80`, `'low' → 0.70`. The advisory then fires only on a bid
**no** legitimate writer path could have produced — zero false positives by
construction. The cost is deliberate: a sub-rung overshoot inside a collapsed band
(a genuine-LOW book bid between 0.60× and 0.70×) is not flagged. Tightening below the
lax bound is a rung-demotion signal, and rung demotion is measurement-gated (KTD9;
BUI-622 then falsified it at the oracle bound — see the falsification table in the
project memory). The corollary worth keeping: **an unmeasured strict reverse-map is an
invented demotion signal**, the exact class that has now died seven consecutive times
on measurement.

## How to apply

1. Before writing (or approving) any `label → constant` map over a stored column,
   grep for the function that **writes** the column and read its mapping. If it is
   many-to-one, your reverse map must be chosen per-band, not per-name.
2. For an advisory/gate, choose the laxest value in each band — fire only on values
   no writer path produces. Tighten later only with measurement.
3. Pin the two at-risk cohorts with regression tests named after the workflow they
   protect (here: the standard 0.80 medium bid, and the 0.70 CGC-proxy bid) so the
   next remap cannot silently reintroduce the false positives.
