---
title: "Add a safety gate as a fail-closed pre-check — don't relocate an unrecoverable invariant to save a call"
date: 2026-07-22
category: architecture-patterns
module: "gixen-cli (record_win_prep.py) + gixen-overlay (routes.py) + locg-cli (commands.py) — null-year record-win era gate (BUI-498)"
problem_type: architecture_pattern
component: service_object
severity: high
related_components:
  - gixen-cli
  - locg-cli
  - gixen-overlay
applies_when:
  - "Adding a new confirmation/safety gate to a flow that already owns a dangerous, effectively-unrecoverable invariant (mark-seen, idempotency ledger, an irreversible external write)"
  - "The signal that would relax an over-conservative default lives in a different layer/service than the code that owns the invariant"
  - "Choosing between a client-side pre-check (extra call) and moving the decision into the invariant-owning path (fewer calls)"
  - "A boolean verdict crosses a service/network boundary from a possibly-stale or divergent server"
  - "Building a containment/era gate whose window could be sourced from the very thing it judges"
tags:
  - fail-closed
  - data-safety
  - mark-seen
  - invariant-blast-radius
  - record-win
  - null-year
  - pre-check
  - degrade-to-safe-default
  - anti-tautology
  - bui-498
---

# Add a safety gate as a fail-closed pre-check — don't relocate an unrecoverable invariant to save a call

## Context

BUI-475 conservatively **held ALL null-year record-wins** for review: a win whose `comic-identify` parse produced no year can't have its era confirmed, and mis-filing it under the wrong volume is the BUI-421 mis-file. Safe, but it over-holds cheap modern fillers that would resolve fine.

BUI-498 recovered the safe auto-record path by gating on the one signal that *can* confirm a null-year win's era — the issue's actual **Metron cover year** — but that signal lives server-side (locg-cli + Metron), while the hold/record **decision** lives client-side in `record_win_prep.entries_for_win` (gixen-cli), which has no collection store and no Metron access. That split forced a design fork:

- **A′ — fail-closed pre-check (chosen):** a new read-only endpoint `POST /api/comics/collection/record-win/era-evidence` the client pre-calls per null-year win; era-confirmed wins move `needs_review → wins`. The commit path — which owns the **mark-seen** invariant — is untouched.
- **B′ — relocate the decision:** relax the client gate, let the wins flow into the commit, and have `_build_win_row` classify held-vs-recorded inline, reusing its single existing Metron lookup (no extra call). But this touches mark-seen and the commit contract.

A′ was chosen even though it pays a **redundant Metron lookup** for auto-recorded null-year wins (the pre-check and the eventual commit each build their own `MetronClient`, so the BUI-473 per-series cache does not span them).

## Guidance

**When adding a safety gate to a flow that owns a dangerous, effectively-unrecoverable invariant, add the gate as a fail-closed pre-check *outside* that flow — do not relocate the decision *into* it, even to save a call.** The invariant's blast radius, not the call budget, sets the cost function.

Three properties make this concrete:

1. **Leave the invariant-owning code untouched.** In A′, held wins never enter `wins`, so the commit still marks seen exactly the wins it recorded — the mark-seen logic (a win wrongly marked seen is *never* re-processed; BUI-121) is byte-for-byte unchanged. B′ would have made a held win's mark-seen state depend on a new classification branch inside the commit — the most dangerous edit in the flow.

2. **Degrade onto the proven-safe default on ANY uncertainty.** Every failure mode of the pre-check — endpoint 404/timeout/non-200/malformed body, Metron outage, ambiguous resolution — returns "not confirmed," which is exactly BUI-475's hold-all. The new path can only ever *release* a win the old behavior would have held; it can never wrongly record one. `fetch_era_evidence` therefore **never raises** (a hard-stop on a soft signal is strictly worse than holding) and validates the 200 body's *shape* (non-list `results`, non-dict rows) before indexing.

3. **Trust a cross-boundary verdict only on strict identity.** A boolean confirmation from a possibly-stale/divergent server is trusted only on a real JSON `true` (`is True`, not truthiness). A truthy non-`True` (`"false"`, `1`) must never flip a HOLD into a wrong-era auto-record.

**The redundant call is the right price.** It is bounded (null-year subset, auto-recorded subset only) and mitigable later — return the resolved data, or share an issue cache — *without re-touching mark-seen*. Optimizing it away by relocating the decision trades a cheap, reversible cost for an expensive, irreversible risk.

### Companion invariant — a containment guard needs independent provenance (BUI-496)

The era signal is `resolve_series_for_win(...) is not None AND _metron_release_date(<lookup>, None, window) is not None`. The window fed to the containment gate must come from the **local** `series_name_index`, never from the Metron hit being judged — a window derived from the same hit gates the candidate against itself and always passes (a tautology). The load-bearing `resolve_series_for_win(...) is not None` clause is what routes the ambiguous multi-volume / unknown-series case to HOLD instead of to Metron's own ungated step-2 date.

## Why This Matters

The tempting optimization (B′) is *locally* cleaner: one Metron call instead of two, signal and decision in one place. But it pays for that with the one thing you can't buy back — it puts the unrecoverable invariant inside the change's blast radius. A′'s "waste" (a second lookup) is fully recoverable; B′'s risk (a held win wrongly marked seen, never revisited) is not. When the two costs are of different *kinds* — reversible vs irreversible — you do not trade them off on magnitude. A data-safety-first system spends calls to keep its dangerous invariants outside every diff.

This also composes with an existing fail-*open* downstream: the commit still files the win under the sole-owned volume, but that filing is now *safe precisely because* the pre-check already confirmed the era — the gate closes the open door from the outside rather than rebuilding the door.

## When to Apply

- Any new gate on a flow with an irreversible or effectively-unrecoverable side effect: a seen/idempotency ledger, a "processed" mark, an external write with no undo.
- The relaxing signal lives in a different service/layer than the invariant. Prefer a read-only pre-check there and thread its verdict in; don't pull the invariant toward the signal.
- Weigh reversible costs (extra latency, redundant calls) against irreversible ones (invariant corruption) by *kind*, not magnitude — pay the reversible one.
- Cross-boundary booleans: fail closed, never raise on a soft signal, validate shape, and require strict `is True`.
- Containment/era gates: source the comparison window independently of the judged candidate (see [../design-patterns/enrich-win-rows-from-full-issue-detail-not-lightweight-lookup.md](../design-patterns/enrich-win-rows-from-full-issue-detail-not-lightweight-lookup.md) and the anti-tautology note above).

## Examples

Client classification stays fail-closed by *default value*, so any caller that doesn't supply the signal holds exactly as before:

```python
# record_win_prep.entries_for_win(win, identity, *, era_confirmed=False)
# A null-year regular win HOLDS unless the server positively confirmed its era.
if identity.get("year") is None and not era_confirmed:
    return [], _build_review_entry(win, identity, REASON_MISSING_YEAR)
```

The wire verdict is trusted only on strict `True`, and the reader never raises:

```python
# fetch_era_evidence: never raises; validates shape; strict identity.
if not isinstance(results, list):
    return {}                      # malformed 200 body -> hold all
for row in results:
    if not isinstance(row, dict):
        continue
    item_id = row.get("item_id")
    if item_id is not None:
        out[item_id] = row.get("era_confirmed") is True   # not bool(), not truthiness
```

Server signal composes the local anchor with the Metron containment gate, failing closed at each step:

```python
# cmd_collection_record_win_era_evidence
window = _resolve_null_year_window(series_raw, issue, series_name_index, volume_candidates)
era_confirmed = False
if window is not None and not metron_disabled:        # ambiguous/unknown series -> window None -> HOLD (no Metron call)
    ...
    if metron_data is not None:
        era_confirmed = _metron_release_date(metron_data, None, window) is not None
```

**Related:** the enforcement-vs-surfacing companion ([wish-list-data-safety-enforcement-vs-surfacing-layer.md](wish-list-data-safety-enforcement-vs-surfacing-layer.md)) — where a data-safety guarantee should *live*; [evidence-layer-disambiguation-vs-heuristic-gating.md](evidence-layer-disambiguation-vs-heuristic-gating.md) — gating on evidence rather than heuristics; and [../design-patterns/guard-strictness-must-match-consequence.md](../design-patterns/guard-strictness-must-match-consequence.md) — how hard a guard should fail given its downstream consequence.
