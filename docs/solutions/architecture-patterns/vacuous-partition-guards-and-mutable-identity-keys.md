---
title: "A partition-scoped guard goes vacuous when one partition empties; a mutable identity key manufactures duplicates"
date: 2026-07-28
status: corrected
superseded_by: "BUI-559 (closed Won't Do 2026-07-28) measured the proposed publisher-relabel
  gate against the live store: it matches zero rows. The DC/Panini pairs are Italian
  licensed editions, not a relabel. See
  docs/solutions/conventions/verify-ticket-premise-before-implementing.md, Example 9, for
  the full account; the real generator is tracked as BUI-563/BUI-564."
superseded_date: 2026-07-28
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

#### 2a. Fold only what the provider rewrites on its own — the obvious normalizer is too wide

Two traps sit between "the key drifts" and a working fix. Both were hit live during BUI-554; the second was hit *by the plan*, and only caught because the implementer re-checked against real data.

**Trap one: do not reuse `_normalize_series_key`.** It exists for the *ownership matcher*, where it deliberately widens so eBay's punctuation-stripped spelling can **meet** the catalog spelling. It also strips `(Vol. N)` and the leading article. Correct for lookup; catastrophic for identity — it collapses `X-Men (Vol. 1) #17` and `X-Men (Vol. 2) #17`, which are legitimately owned side by side. **Matcher normalization and identity normalization are different functions with opposite pressures**: one wants to find a match, the other wants to keep distinct things distinct.

**Trap two: folding the whole year range is also too wide.** The tempting narrow fix — reuse `_YEAR_RANGE_RE`, which already matches both `(YYYY - YYYY)` and `(YYYY - Present)` — is still wrong, because **LOCG reuses a `Vol. N` label across genuinely different volumes**:

| | |
|---|---|
| `X-Men (Vol. 2) (1991 - 2001)` | `X-Men (Vol. 2) (2001 - 2013)` |
| `Spawn (1992 - Present)` | `Spawn (2012 - Present)` |

`(Vol. N)` is not a unique volume discriminator, so the **start year is load-bearing**. Stripping the range merges both pairs.

The shipped fold rewrites the **end year only**:

```python
# collection_cache.py — identity_series_key
_YEAR_RANGE_CAPTURE_RE.sub(r"(\1 - )", series_name)   # (1992 - Present) -> (1992 - )
```

**The rule: fold exactly what the provider rewrites on its own, and nothing else.** Every other difference is still a different series. Verified against the live store — the fold collapsed exactly the 31 same-book groups and neither legitimate pair.

Still uncovered: a bare `(YYYY)` when a series starts and ends in one calendar year (`Knull (2026 - Present)` vs `Knull (2026)`, BUI-560).

> **Correction (BUI-559, 2026-07-28 — see the frontmatter `status:` above).** This doc
> originally named a `publisher_name` relabel (`DC Comics` vs `Panini Comics`) as a second
> live generator. It isn't one. BUI-559 measured the proposed triple-equality gate
> (identical `series_name`/`full_title`/`release_date`, publisher differing) against the
> live store: the DC/Panini pairs never share a `release_date` — they trail by a monotone
> 147–211 days — so the gate would have matched **zero** rows. These are Italian **licensed
> editions**, not a relabel; the real generator behind them is tracked separately as
> BUI-563/BUI-564. Full account:
> `docs/solutions/conventions/verify-ticket-premise-before-implementing.md`, Example 9.

#### 2b. A key with tolerance is not a key

The two duplicate classes in the table above look symmetrical and are not. The series relabel is a **pure tuple change** — normalize the field, done. Date-convention drift cannot be fixed that way: the correct comparison is *approximate* (`_release_dates_compatible` allows a ≤120-day cover-vs-on-sale gap), and **tolerance is not transitive and has no canonical form**, so it cannot back a dict lookup.

Tolerant matching needs a **second pass**, not a wider key: drain exact identities first, then re-scan the leftovers with the tolerant predicate. Order matters — the exact pass must claim its rows before the tolerant one runs, or an approximate match steals a row that something else matched exactly.

When an identity key "needs to be fuzzier," check first whether you are actually being asked for a *second matching stage*. Usually you are.

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

**A guard test that fails when the guard goes blind.** The shipped fix removed the `agent_win` partition entirely, so the check no longer *has* a partition that can empty — the strongest possible version of this guard. Where a partition is genuinely required, assert it is populated rather than assuming it:

```python
def test_duplicate_check_can_still_fail(store):
    """A partition-scoped counter is only meaningful while its partition is populated."""
    owned = [r for r in store["comics"] if _is_owned(r)]
    assert len(owned) >= 2, (
        "fewer than two owned rows — owned_duplicate_identities cannot detect "
        "anything and its 0 is vacuous"
    )
```

Note what changed between the original guard and this one: the first asserted a *specific source value* still existed, which tied the test to the very assumption that broke. Assert the **minimum condition under which a positive result is possible**, not the shape the data happened to have when you wrote it.

**Distinguishing real duplicates from legitimate multi-row holdings.** 40 of the 100 multi-row title keys are genuine and must survive any cleanup — `X-Men #17` legitimately exists three times (Vol. 1 1965-12-02, Vol. 2 1992-12-15, Vol. 6 2021-01-27), and a [[Printing]] is a distinct collectible, so `Batman: The Dark Knight Returns #2` and `#2 3rd Printing` are two books. The date predicate is what separates these from the drift classes; a title-key match alone would sweep them.

**If you clean this up:** it is production data, so use the backup → apply → diff → row-count ritual. The collection DELETE API is unsafe here (alias and cross-volume ambiguity); use `CollectionCache.apply` keyed on a genuinely unique field or asserting exactly-one-match. `gixen_item_id` is **not** unique — a lot bought together shares it across issues. And [[Copy Count]] is a count, not a flag: merging two genuine copies must increment the survivor, not drop the loser.

**What the cleanup actually took** (BUI-556, 2026-07-28, all 60 groups). Three findings worth reusing:

- **Choosing the survivor.** The live row is the one with the most recent `last_seen_in_export_at` — it equals the store's `last_full_import`, while its orphaned twin is stale by however long ago the key drifted. Do **not** sort by `local_added_seq`: it is a *within-import* counter, not a global ordering, and it selects the stale row in 35 of 60 groups.
- **Copy count.** The rule above ("merging two genuine copies must increment") cuts both ways: these 60 were **one book recorded twice**, so the survivor kept `in_collection=1` and summing would have invented 60 phantom copies. Decide it from evidence, not from the row count — assert that no group holds two *different* non-null `gixen_item_id` or `price_paid` values, and abort if one does, because that is the signature of a real second copy.
- **Merge fields, don't just delete.** 3 of the 60 carried purchase data (`price_paid`, `date_purchased`, `grading`, `purchase_store`, `gixen_item_id`) on one side only. A plain delete destroys it. Fold any field the survivor lacks from the twin, excluding per-row bookkeeping (`local_added_*`, `last_seen_in_export_at`, `pushed_to_locg_at`, `source`, `in_collection`).

## Related

- BUI-554 — shipped 2026-07-28 (PR #349): end-year-only fold, `source` partition removed, two-pass tolerant match
- BUI-556 — the 60-row cleanup this doc's audit snippet found; shipped the same day
- BUI-560 — still uncovered: the bare-`(YYYY)` fold generator. BUI-559 (publisher relabel)
  was investigated and found **not** to be a generator — see the correction above.
- BUI-561 — a sibling instance: BUI-546 changed `_normalize_series_key` without rebuilding the persisted `series_name_index`, leaving 277/307 keys stale with no check able to notice
- BUI-548 — added the counter; correct when written, outlived its partitioning assumption
- `docs/solutions/architecture-patterns/durable-evidence-store-encode-unknowns-and-identity-precisely.md` — the sibling lesson that a UNIQUE key silently defines what counts as a duplicate, on the gixen-cli evidence ledger
- `docs/solutions/design-patterns/guard-strictness-must-match-consequence.md` — guard design in the same reconcile path
- `docs/solutions/integration-issues/locg-sync-unified-model-2026-06-22.md` — the sync round-trip that drained the `agent_win` partition
