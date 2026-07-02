---
title: "Wish-list conflict removal: provenance fields + scoped removal (BUI-249/259/266)"
date: 2026-07-02
category: docs/solutions/integration-issues
module: gixen-overlay
problem_type: logic_error
component: api_endpoint
related_components:
  - gixen-overlay
  - locg-cli
symptoms:
  - "POST /api/comics/wish-list/remove-conflicts removed 114 wishes when ~6 were intended"
  - "a genuinely-wanted wish (different era/edition of the same masthead+issue) was removed as a false 'conflict'"
  - "a wished 1968 Avengers #52 was matched against an owned UK-reprint 'The Avengers (1973 - 1976)' #52"
  - "a base 'Uncanny X-Men #201' wish was matched against an owned Newsstand copy of the same issue"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags:
  - locg
  - wish-list
  - collection-sync
  - bui-249
  - bui-259
  - bui-266
  - provenance
  - scoped-removal
---

# Wish-list conflict removal: provenance fields + scoped removal (BUI-249/259/266)

## Problem

`GET /api/comics/wish-list/conflicts` (BUI-130) audits the wish-list for entries
already owned, so `/comic:collection-sync` can drop them before a wish push
(fulfillment-drop, BUI-208 U2). The audit matches on masthead + issue number
only — it has no per-issue year or variant, because a wish-list name carries
neither. `POST /api/comics/wish-list/remove-conflicts` originally swept the
**entire** conflict set unconditionally in one call.

**BUI-259 incident:** a sync run intending to clear ~6 genuine conflicts instead
removed **114** wishes. Most of the extra 108 were decoys — the masthead+issue
match landed on the wrong book:

- a wished 1968 *Avengers* #52 matched against an owned UK-reprint
  `The Avengers (1973 - 1976)` #52 (different printing, different era);
- a base `Uncanny X-Men #201` wish matched against an owned **Newsstand** copy
  of the same issue (opposite print edition, not a fulfillment).

Both are genuine collection rows that happen to share a masthead + issue number
with the wished title — not the same book the user wanted. Removing them
silently un-wished books the user still didn't own.

## Solution

Two fixes landed together:

**BUI-249 — provenance on the match.** `cmd_collection_check`'s matcher already
returned a near-binary verdict with no way to see *which* owned row it matched.
`_match_owned_issue` now returns the full matched row (not just `full_title`),
so callers get `matched_series_name`, `matched_release_date`, and `match_kind`
(`"exact"` vs `"alias"`) — `"alias"` being the signal to confirm volume/edition
before trusting a match. `match_status`/`full_title_matched` stay
byte-identical; the new fields are `null` on `not_in_cache` (R11 — a failed/
non-match can't be dressed up as a partial match).

**BUI-266 — scoped removal, re-checked against a fresh audit.**
`cmd_wish_list_conflicts` now surfaces each conflict's matched-owned-row
provenance (the same BUI-249 fields), so a human reviewer can visually catch a
cross-era/cross-edition decoy before removing anything.
`cmd_wish_list_remove_conflicts` accepts an optional `names` list to scope
removal to a caller-reviewed subset; each name is re-checked against a fresh
audit at removal time, so a stale or non-conflict name errors out instead of
being silently accepted. The HTTP layer gates the unscoped path behind an
explicit `confirm=true` — an unscoped call with no `confirm` now returns the
same non-mutating preview the GET audit returns (`dry_run: true`), closing the
global-sweep foot-gun while keeping the original sweep-everything behavior
available as an explicit, deliberate opt-in.

## Why this works

The audit's masthead+issue matching is inherently lossy (no year/variant to
disambiguate), so *some* rate of decoy matches is unavoidable given the
wish-list's data shape. The fix doesn't try to make the matcher perfect — it
makes the **blast radius of a mistake bounded and visible**: provenance fields
let a reviewer catch a decoy by eye (compare era/edition), and scoping removal
to caller-reviewed names means a decoy that slips past review only costs one
wrongly-dropped wish, not a 114-item sweep.

## Prevention

- **Never call `remove-conflicts` unscoped without `confirm=true`.** The
  default is now a dry-run preview; treat that as the expected safety rail, not
  a bug.
- **Always read `matched_series_name`/`matched_release_date` before removing a
  conflict**, especially when `match_kind == "alias"` — the masthead-alias pass
  has no notion of which volume/era it matched.
- **`/comic:collection-sync` Step 2b** (`.claude/commands/comic/collection-sync.md`)
  is the operational procedure that applies this: it reviews the audit, splits
  genuine conflicts from decoys, and passes only the genuine names to the
  scoped removal endpoint.

## Related Issues

- `integration-issues/locg-export-deletes-owned-wished-books.md` — the original
  BUI-122 data-loss mechanism (`In Collection=0` deletes) that the conflicts
  audit exists to prevent triggering.
- `integration-issues/locg-sync-unified-model-2026-06-22.md` — the BUI-208
  fulfillment-drop model this audit implements.
- Linear: BUI-249 (provenance fields), BUI-259 (the 114-item incident), BUI-266
  (scoped removal fix), BUI-130 (original conflicts audit).
