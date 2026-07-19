---
title: "Enrich win rows from Metron's full-Issue detail, not the lightweight lookup"
date: 2026-07-19
category: design-patterns
module: locg-cli
problem_type: design_pattern
component: service_object
severity: medium
applies_when:
  - "Adding any full-Issue field (publisher, cover date, credits, variants) to a record-win row in packages/locg-cli"
  - "A ticket proposes a one-line fix that assumes the win's metron_data already carries the field you need"
  - "Enriching data on a write path whose output is later uploaded to an external system (LOCG bulk import)"
tags:
  - metron
  - record-win
  - locg-cli
  - collection-cache
related_docs:
  - "docs/solutions/design-patterns/metron-5xx-detection-trips-batch-breaker.md"
  - "docs/solutions/best-practices/collection-add-record-win-tail-rationale-2026-07-19.md"
---

# Enrich win rows from Metron's full-Issue detail, not the lightweight lookup

## Context

record-win builds each collection row in `_build_win_row` (`packages/locg-cli/src/locg/commands.py`). BUI-458 needed the row to carry a real `publisher_name` (e.g. `Marvel Comics`) instead of the hardcoded `None` that made every recorded win import to LOCG bulk-import as **Not Found** and fail the pre-upload `audit-pending` "no publisher" check.

The ticket's proposed one-line fix — "extend `lookup_issue_detail` to return the publisher, then read it into the row" — looked complete but would have populated **only variant wins**. Tracing the actual call graph is what revealed the gap.

## Guidance

**Trace which Metron call actually populates `metron_data` before assuming a field is available.** In record-win there are two tiers:

- `metron.lookup_issue` — the **lightweight** path. Hits the list endpoints (`series_list` → `BaseSeries`, `issues_list` → `BaseIssue`), whose mokkari schemas carry **no** publisher and generally no detail fields. This is what populates `metron_data` for every win.
- `metron.lookup_issue_detail` — fetches the full `Issue` (`session.issue(id)`), which *does* carry `publisher`, `variants`, `credits`. Previously record-win called this **only on the variant path**, so a non-variant win never saw any detail field.

To add a full-Issue field to the row, **hoist a single shared `lookup_issue_detail` fetch**:

1. Gate it on a resolved `metron_id` (`metron_data.get("metron_id")`) and `not metron_disabled`.
2. Place it **after** the dedup early-return (BUI-34) so a skipped already-owned win spends no Metron call.
3. **Reuse** that same `issue_detail` in the variant block instead of fetching again — a variant win still makes exactly **one** detail call (no latency regression).

**Fail-soft to null, never to a guess.** On any Metron miss / `None` field / network error / `MetronCredentialError`, leave the field null. A null fails the pre-upload audit (the intended backstop that a human sees); a *wrong* value imports silently to LOCG and is far harder to detect. Extract with an `isinstance` + non-blank guard so a missing attribute, a blank string, or a bare `MagicMock` in tests all degrade to `None`:

```python
@staticmethod
def _extract_publisher(issue: Any) -> Optional[str]:
    publisher = getattr(issue, "publisher", None)
    name = getattr(publisher, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None
```

## Why This Matters

- **The ticket's "obvious" fix was structurally incomplete.** Only a call-graph trace showed that the lightweight lookup feeds the row and the detail lookup was variant-only. A literal executor would have shipped a fix that populated a small minority of wins and looked done.
- **One shared fetch keeps the change free of a latency regression.** Naively adding a second `lookup_issue_detail` for the publisher would double the detail calls for every variant win. Hoisting once and reusing keeps it at one call per metron-resolved win.
- **The write path feeds an external upload.** A fabricated/defaulted publisher would sail past the audit and create silently-wrong LOCG entries. Null is the safe failure — it trips the visible backstop.

## When to Apply

Any time you add a field to a record-win row (or a similar write path) that is only present on Metron's full `Issue`, not the lightweight list responses. Also whenever a ticket's stated fix assumes `metron_data` already carries detail — verify by reading which lookup populated it.

## Examples

**Before (BUI-458):** row hardcoded `"publisher_name": None`; detail fetched only inside the variant branch.

**After:** one hoisted `issue_detail = metron.lookup_issue_detail(detail_metron_id)` gated on `detail_metron_id is not None and not metron_disabled`, run after the dedup return; `publisher_name = issue_detail.get("publisher") or None`; the variant block reuses `issue_detail` (asserted `lookup_issue_detail.assert_called_once()` for a variant win). Regression tests cover: publisher persists on a hit, stays null on a plain miss (with **no** wasted detail call), stays null on a hit-with-no-publisher, and an end-to-end record → CSV "Publisher Name" → `audit-pending` no longer flags "no publisher".

**Note on an adjacent stat:** the diagnostic counter `metron_variant_lookups_attempted` (surfaced in `cmd_collection_record_win`'s result) now increments whenever a variant win has a resolved `metron_id`, even if Metron went disabled and no fetch actually ran — a harmless diagnostic drift, not a data-safety concern. Don't wire correctness onto that counter.
