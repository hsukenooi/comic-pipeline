---
title: "A partition-scoped guard goes vacuous when one partition empties; a mutable identity key manufactures duplicates"
date: 2026-07-28
category: architecture-patterns
module: "locg-cli collection store (packages/locg-cli/src/locg/collection_io.py duplicate counter + _partial_identity; collection_cache.py make_identity)"
problem_type: architecture_pattern
component: database
severity: high
related_components:
  - "service_object"
applies_when:
  - "A correctness counter or invariant check partitions rows by a mutable field (a source/status/kind column) and only reports a violation when the partitions collide"
  - "Rows migrate between those partitions over time — a round-trip, a state machine, a backfill — so a partition can drain to empty"
  - "An identity/dedup key is built from provider-supplied strings that the provider is free to relabel"
  - "A rename detector exists whose key shares fields with the identity key it is meant to protect"
  - "A metric reads healthy and is being taken as evidence the underlying data is clean"
symptoms:
  - "A duplicate/violation counter reports 0 while the violations demonstrably exist in the store"
  - "Re-running the same predicates with a different pairing finds violations the shipped counter cannot see"
  - "An upstream relabel (a volume end-year, a date convention) silently produces a second row instead of updating the first"
root_cause: logic_error
tags:
  - data-integrity
  - identity-key
  - invariant-checks
  - dedup
  - locg-cli
  - vacuous-guard
---

# A partition-scoped guard goes vacuous when one partition empties; a mutable identity key manufactures duplicates

## Context

The collection store gained a duplicate counter (`owned_duplicate_identities`) to catch a real failure: a [[Win-Sourced Entry]] and an [[Import-Sourced Entry]] both claiming the same owned book, the signature of a failed reconcile. It worked when it was written.

Some weeks later a [[Collection Sync]] round-tripped every pending win back through LOCG. Each win became an import-sourced row. The live store reached 2903 rows, **all** `locg_export`, **zero** `agent_win`.

The counter now reports `0`. Not because the store is clean — pairing the same predicates export↔export finds **60 duplicate identities** across 100 owned title keys that carry more than one row. It reports 0 because it lost the ability to report anything else.

Two independent defects converged here, and both are reusable lessons.

## Guidance

### 1. A partition-scoped check must assert its partitions are still populated

The counter groups owned rows into two buckets and only reports a title present in **both**:

```python
# collection_io.py — the shipped check, abridged
owned_wins, owned_exports = {}, {}
for row in comics:
    if not _is_owned(row):
        continue
    title = _duplicate_check_title_key(row.get("full_title") or "")
    if not title:
        continue
    target = owned_wins if row.get("source") == "agent_win" else owned_exports
    target.setdefault(title, []).append(row)

owned_duplicates = sorted(
    title for title, wins in owned_wins.items()
    if any(_release_dates_compatible(win, export)
           for win in wins for export in owned_exports.get(title, ()))
)
```

When `owned_wins` is empty the comprehension iterates nothing and yields `[]`. **The healthy reading and the blind reading are the same value.** Nothing in the output distinguishes "checked, found none" from "structurally unable to find any."

Two fixes, and prefer the second:

- **Assert liveness.** Emit a warning when a partition the check depends on is empty — the check has stopped being able to fail, and that is itself news.
- **Let the predicate decide, not the partition.** Compare all pairs sharing a title key and let `_release_dates_compatible` rule. The `source` split was never doing correctness work; it encoded an assumption about *which* pairing was interesting, and that assumption expired.

A guard's partitioning should reflect what makes two rows a violation, not which failure the author happened to be chasing.

### 2. An identity key must not be built from strings the provider can relabel

```python
# collection_cache.py:118
def make_identity(row):
    return (row.get("publisher_name") or "", row.get("series_name") or "",
            row.get("full_title") or "", row.get("release_date") or "")
```

Four raw, unnormalized, provider-supplied strings. Import matches on this tuple; a miss inserts a new row. So any upstream relabel manufactures a duplicate. Both observed classes are ordinary upstream behavior, not corruption:

| Class | Count | Signature |
|---|---|---|
| Series relabel | 37 | `Absolute Martian Manhunter (2025 - Present)` vs `(2025 - 2026)` — same title, same date |
| Date convention drift | 23 | `Crisis on Infinite Earths #1` at `1985-01-03` vs `1984-12-11` — cover date vs on-sale date |

The `(YYYY - Present)` → `(YYYY - YYYY)` transition fires for **every ongoing series the January after it ends**. This is a recurring annual duplicate generator, not a one-off.

### 3. A rename detector must not share fields with the rename it detects

```python
# collection_io.py:739
def _partial_identity(row):
    """(publisher_name, series_name, release_date) — used to detect full_title renames."""
```

It catches a `full_title` rename by holding the other three fields steady. But when the field that changed **is** `series_name` or `release_date`, the detector's own key moves too. Both guards miss the same row for the same reason. A detector keyed on fields that can themselves drift is not a second line of defense — it fails in exactly the cases the first one does.

## Why This Matters

A metric that cannot fail is worse than no metric. No metric leaves you appropriately uncertain; a metric pinned at 0 actively certifies the thing it stopped measuring. Here the sync reported clean while 60 identities collided, and the store had been quietly accumulating them for months.

The compounding shape is worth naming: defect 2 **creates** duplicates, defect 1 **hides** them. Fixing only the key stops new duplicates while leaving the existing 60 invisible; fixing only the counter surfaces 60 and then watches the number climb every January. Both are needed, and neither is sufficient.

## When to Apply

- Before trusting a green invariant metric, ask *what data shape would make this unable to fail* — then check whether the store has drifted into that shape
- When any column that a check partitions on is also a column rows can migrate between
- When choosing a dedup/identity key: prefer normalized or immutable components; if a volatile string must participate, fold the volatile part (a volume end-year) or compare with tolerance (a date within a window) rather than by equality
- When adding a rename/drift detector, verify its key is disjoint from the fields it is protecting against

## Examples

**Re-deriving the truth — run the module's own predicates with a different pairing.** Read the raw store (top-level key `comics`), never the check endpoint, which is lossy for audits:

```python
from locg.collection_io import (
    _duplicate_check_title_key, _is_owned, _release_dates_compatible,
)

groups = {}
for row in comics:
    if _is_owned(row):
        t = _duplicate_check_title_key(row.get("full_title") or "")
        if t:
            groups.setdefault(t, []).append(row)

# all-pairs, no source partition — the predicate decides
dupes = [t for t, rows in groups.items()
         if any(_release_dates_compatible(a, b)
                for i, a in enumerate(rows) for b in rows[i + 1:])]
```

Using the module's own predicates matters: "the matcher should have caught this" and "this is a duplicate" stay the same judgment, so the audit cannot disagree with the code by accident.

**A guard test that fails when the guard goes blind:**

```python
def test_duplicate_check_can_still_fail(store):
    """The counter is only meaningful while both partitions are populated."""
    owned = [r for r in store["comics"] if _is_owned(r)]
    assert any(r.get("source") == "agent_win" for r in owned), (
        "no agent_win rows remain — owned_duplicate_identities can no longer "
        "detect anything and its 0 is vacuous"
    )
```

**Distinguishing real duplicates from legitimate multi-row holdings.** 40 of the 100 multi-row title keys are genuine and must survive any cleanup — `X-Men #17` legitimately exists three times (Vol. 1 1965-12-02, Vol. 2 1992-12-15, Vol. 6 2021-01-27), and a [[Printing]] is a distinct collectible, so `Batman: The Dark Knight Returns #2` and `#2 3rd Printing` are two books. The date predicate is what separates these from the drift classes; a title-key match alone would sweep them.

**If you clean this up:** it is production data, so use the backup → apply → diff → row-count ritual. The collection DELETE API is unsafe here (alias and cross-volume ambiguity); use `CollectionCache.apply` keyed on a genuinely unique field or asserting exactly-one-match. `gixen_item_id` is **not** unique — a lot bought together shares it across issues. And [[Copy Count]] is a count, not a flag: merging two genuine copies must increment the survivor, not drop the loser.

## Related

- BUI-554 — the filed fix for both defects (diagnosis verified, not yet shipped)
- BUI-548 — added the counter; correct when written, outlived its partitioning assumption
- `docs/solutions/architecture-patterns/durable-evidence-store-encode-unknowns-and-identity-precisely.md` — the sibling lesson that a UNIQUE key silently defines what counts as a duplicate, on the gixen-cli evidence ledger
- `docs/solutions/design-patterns/guard-strictness-must-match-consequence.md` — guard design in the same reconcile path
- `docs/solutions/integration-issues/locg-sync-unified-model-2026-06-22.md` — the sync round-trip that drained the `agent_win` partition
