---
title: "Wish-list data-safety lives in the year-blind export filter, not the conflicts audit — enforcement vs surfacing"
date: 2026-07-18
category: docs/solutions/architecture-patterns
module: locg-cli (wish-list conflicts audit + collection export)
problem_type: architecture_pattern
component: service_object
severity: high
related_components:
  - locg-cli
  - gixen-overlay
  - comics
applies_when:
  - "Changing the wish-list conflicts audit (cmd_wish_list_conflicts / remove-conflicts) or adding year/variant scoping to it"
  - "Tempted to add year or printing scoping to the collection EXPORT owned-safe filter"
  - "Two coupled tickets both need to thread new match signal through one shared wish-list function"
tags: [wish-list, data-loss, bui-122, bui-387, cover-year, enforcement-layer, conflicts-audit, coupled-tickets]
---

# Wish-list data-safety lives in the year-blind export filter, not the conflicts audit — enforcement vs surfacing

## Context

The wish-list has two code paths that both reason about "is this wished book already owned?", and they look similar enough to be confused for one another:

1. **The conflicts audit** — `cmd_wish_list_conflicts` / `cmd_wish_list_remove_conflicts` (`packages/locg-cli/src/locg/commands.py`). It scans the wish-list for entries the collection already owns so `/comic:collection-sync` can drop them before a wish push. It **surfaces** candidates and (via remove-conflicts) deletes wishes.
2. **The collection export owned-safe filter** — `wish_rows_for_export` (`packages/locg-cli/src/locg/collection_io.py`, via `_owned_series_issue_index`). It builds the LOCG bulk-import CSV and independently refuses to emit an `In Collection=0` row for any owned `(series, issue)`.

The BUI-122 incident (18 owned books deleted from LOCG — see `../integration-issues/locg-export-deletes-owned-wished-books.md`) came from the **export** path writing `In Collection=0` for an owned+wished book. The fix and the durable safety guarantee therefore live in `wish_rows_for_export`, and that filter is deliberately **year-blind**: it excludes any owned `(series, issue)` regardless of year/variant.

BUI-387 then wanted to add an optional per-issue Cover Year to the audit (so a vintage grail wish stops flagging against an owned modern volume — the cross-volume decoys). That raised an obvious fear: *year-scoping an ownership check is the BUI-129 direction (a wrong year hides owned books → data loss).* The change shipped safely anyway — and understanding **why** is the reusable learning.

## Guidance

**Keep the data-safety invariant (never let an owned book be exported for deletion) in the export enforcement layer, which stays year-blind. The conflicts audit is only a surfacing layer — scope it freely.**

Concretely:

- **`wish_rows_for_export` is the enforcement layer.** It is the single point that can cause BUI-122 data loss, and it must stay year-blind: exclude every owned `(series, issue)` from `In Collection=0`, no year/variant gating. Do **not** add year-scoping here — a year gate on the export filter is exactly how you reintroduce BUI-122 (a mis-scoped year would let an owned book through as `In Collection=0`).
- **The conflicts audit is the surfacing layer.** It decides which wishes to *show* (and, via remove-conflicts, delete from the wish-list — never from the collection). Adding a per-issue year here changes *which owned volume a wish is compared against*; a mis-scoped year only makes the audit **fail to surface** a wish for cleanup. It can never delete an owned collection book, because the collection's deletion protection is enforced elsewhere, independently, and year-blind.

The safety argument that let BUI-387 ship: *worst case of a wrong audit year = reduced audit completeness (a decoy isn't cleaned), never a deleted owned book.* That argument is only valid **because the two layers are separate**. If you ever collapse them — route the audit's year into the export filter, or move the owned-safe check into the audit — you forfeit it.

### Companion pattern: coupled tickets adding signal to one shared function

BUI-379 (wish-side printing marker) and BUI-387 (wish per-issue year) both needed to thread **new match signal** through the same wish-list machinery, and both touched `_split_wish_list_name`. Neither widened it. The rule they followed, worth reusing:

**When two in-flight tickets both need to thread new signal through one shared function, add each signal as a separate stored field or sibling function — do not widen the shared function's signature.** Widening serializes the tickets on each other's return shape and invites merge/rebase conflicts; separate fields let them ship independently.

- BUI-379 added a **sibling** `_wish_list_name_printing_variant(name)` that re-detects the marker from the raw name (shared `_PRINTING_MARKER_RE`), leaving `_split_wish_list_name`'s 2-tuple untouched.
- BUI-387 stored the year as a **separate entry field** (`it.get("year")`), never encoded into the name, so the parser was untouched again.

Result: two coupled tickets shipped in sequence with zero parser-signature collision and a clean rebase.

## Why This Matters

Two ownership-checking paths that *look* interchangeable are not: one is load-bearing for data-safety and one is cosmetic-ish (audit hygiene). A future engineer who "improves" the audit's precision by pushing its year into the export filter, or who moves the owned-safe check into the audit "to have one place", silently turns a safe change into a BUI-122 data-loss change. Naming which layer enforces the invariant — and that it must stay year-blind — is what keeps the safety proof intact across future edits.

The companion parser pattern matters because the wish-list match surface is a recurring hotspot (printing, year, variant, and more will come); each new scoping ticket that widens the shared parser makes the next one conflict. Separate fields keep that surface stable.

## When to Apply

- Editing `cmd_wish_list_conflicts` / `cmd_wish_list_remove_conflicts` — remember you are in the **surfacing** layer; a mistake reduces cleanup completeness, it does not delete owned books.
- Editing `wish_rows_for_export` / `_owned_series_issue_index` — you are in the **enforcement** layer; keep it year-/variant-blind; a mistake here is a BUI-122-class data-loss bug.
- Adding any new per-issue scoping signal (year, printing, edition) to the wish-list — store it as a separate field / detect it via a sibling; do not widen `_split_wish_list_name`.
- Reviewing a change that claims year-scoping the audit is "risky like BUI-129" — check whether the *export enforcement layer* is untouched and year-blind. If it is, the audit change cannot cause data loss (it can still be wrong per BUI-129 — see `../best-practices/collection-check-cover-year-forwarding-vs-bui129.md` — but wrong here means "misses a decoy", not "deletes an owned book").

## Examples

**Safe (BUI-387): year scopes the audit only; enforcement untouched.**

```python
# cmd_wish_list_conflicts (surfacing layer): forward the wish's OWN stamped
# Cover Year (or None → today's year-blind behavior).
wish_year = it.get("year")
result = cmd_collection_check(series=series, issue=issue, year=wish_year, variant=variant)
# wish_rows_for_export (enforcement layer) is NOT touched by this change and
# stays year-blind: it excludes every owned (series, issue) from In Collection=0
# regardless of year. So a mis-scoped wish_year only fails to surface a decoy —
# it can never emit an owned book for deletion.
```

**Would reintroduce BUI-122 (do not do this): year-gating the export filter.**

```python
# ANTI-PATTERN — a year gate on the deletion-enforcement layer.
# A wrong/missing year now lets an owned book pass the owned-safe check and
# go up as In Collection=0 → LOCG deletes it. The safety proof is gone.
if owned_index.contains(series, issue, year=wish_year):   # ← never add year here
    skip_export(row)
```

**Companion pattern: separate field / sibling, not a widened parser.**

```python
# BUI-379: sibling, not a wider _split_wish_list_name return.
def _wish_list_name_printing_variant(name: str) -> Optional[str]:
    m = _PRINTING_MARKER_RE.search(name or "")
    return m.group(0) if m else None

# BUI-387: separate stored field; name parser untouched.
if year:                       # validated 4-digit Cover Year
    entry["year"] = year       # never encoded into entry["name"]
```

## Related

- `../integration-issues/locg-export-deletes-owned-wished-books.md` — the BUI-122 deletion incident; `wish_rows_for_export` is the enforcement layer named there.
- `../integration-issues/wishlist-conflict-scoped-removal-2026-07-02.md` — the conflicts audit's provenance fields + scoped removal. **Note:** its "the audit matches on masthead + issue number only — it has no per-issue year" statement predates BUI-387, which added optional per-issue year scoping to the audit.
- `../best-practices/collection-check-cover-year-forwarding-vs-bui129.md` — why a *correct* per-issue cover year is Pareto-better than no year, and why the fix belongs at the input, not in the matcher's volume model.
- BUI-379 (wish printing-marker preservation), BUI-387 (year-scoped wish entries), BUI-122 (export deletes owned), BUI-129 (series-start-year hides owned books).
