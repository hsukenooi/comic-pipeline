---
title: "FMV Stub Row Created by extract-comics via ALL-CAPS Title Case Mismatch"
date: 2026-05-23
category: docs/solutions/database-issues/
module: comic-pipeline
problem_type: database_issue
component: database
severity: high
symptoms:
  - Dashboard shows Cond grade but permanently null FMV after using POST /api/extract-comics as recovery
  - DB contains duplicate comics rows — one properly-cased with FMV prices, one ALL-CAPS yearless with null low/high
  - POST /api/bids/{item_id}/link-fmv returns 404 for affected bids because the stub row has no locg_id
  - Snipes response shows locg_id=None for the bid's comic
root_cause: logic_error
resolution_type: code_fix
related_components:
  - tooling
tags:
  - fmv
  - upsert-comic
  - extract-comics
  - case-mismatch
  - duplicate-rows
  - bid-linkage
  - stub-rows
---

# FMV Stub Row Created by extract-comics via ALL-CAPS Title Case Mismatch

## Problem

When `POST /api/extract-comics` is used as a recovery tool against bids that already have properly-saved `comics` + `fmv` rows, it creates duplicate ALL-CAPS yearless `comics` rows and links `bid_fmvs` to stub `fmv` entries with null prices. The dashboard then shows Cond grade permanently but null FMV, with no error raised anywhere in the chain.

## Symptoms

- Dashboard shows Cond grade (junction row exists in `bid_fmvs`) but null FMV values permanently
- DB contains two `comics` rows for the same series: one properly-cased with `locg_id` and valid `fmv.low`/`fmv.high`, one ALL-CAPS yearless with `locg_id=None` and stub `fmv` row where `low=NULL, high=NULL`
- `POST /api/bids/{item_id}/link-fmv` returns 404 for affected bids because the stub row has `locg_id=NULL` (the endpoint searches `WHERE c.locg_id = ?`)
- `GET /api/comics` returns two rows for the same comic with different casing and `year`

## What Didn't Work

- Suspecting Pydantic field name mismatch (`low` vs `fmv_low`) in the `POST /api/comics` save path — field names in the session transcript were correct
- Running `gixen-cli add --catalog-id {locg_id} --grade {grade}` to re-link — failed because the stub `comics` rows lack `locg_id`, so `POST /api/bids/{item_id}/link-fmv` returns 404

## Solution

### Immediate recovery (patch existing stubs)

Call `POST /api/comics` using the **exact ALL-CAPS title** of the stub row, plus the correct `fmv_low`/`fmv_high` from the original FMV computation. `upsert_fmv` matches on `(comic_id, grade)` with COALESCE semantics, so it finds the existing stub and patches the null prices in-place without disturbing `bid_fmvs`:

```bash
# Must use the EXACT ALL-CAPS title to hit the stub row, not the canonical title
curl -s -X POST "$GIXEN_SERVER_URL/api/comics" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "THE MIGHTY THOR",
    "issue": "154",
    "grade": 6.5,
    "fmv_low": 20,
    "fmv_high": 30,
    "fmv_confidence": "low"
  }'
```

To identify stub titles, query the live server and cross-reference `GET /api/comics` (which returns `comics.title`) with the `item_id` from snipes that have `lot_count > 0` and `fmv_low=null`.

### Permanent fix (PER-123)

Add `LOWER()` normalization to all four SELECT queries in `upsert_comic` and to `sweep_orphan_yearless_comics`:

```python
# plugins/gixen-overlay/src/gixen_overlay/db.py

# Before (exact match — case-sensitive):
conn.execute(
    "SELECT id FROM comics WHERE title=? AND issue=? AND year=?",
    (title, issue, year),
)

# After (case-insensitive):
conn.execute(
    "SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year=?",
    (title, issue, year),
)
```

Apply to all four lookup paths in `upsert_comic`: yeared exact, yeared yearless-conflict guard, canonical-yeared yearless-insert, and yearless-exact. Also apply to the orphan-finder query in `sweep_orphan_yearless_comics`.

## Why This Works

`upsert_comic` uses exact-match SQL on `title` at every lookup. eBay titles from ALL-CAPS listings (e.g., "THE MIGHTY THOR # 154") produce ALL-CAPS series names after `parse_title()`. These never match properly-saved rows ("The Mighty Thor") in any of the four lookup paths, so a new `comics` row is created as a yearless stub. `upsert_fmv` is then called with only `grade` and `notes` (no FMV prices), creating a stub `fmv` row. `link_fmv_to_bid` links `bid_fmvs` to this stub. The properly-valued row is now orphaned from the dashboard's perspective.

The COALESCE recovery works because `upsert_fmv` never overwrites existing non-null values:

```sql
ON CONFLICT(comic_id, grade) DO UPDATE SET
    low  = COALESCE(excluded.low,  low),
    high = COALESCE(excluded.high, high),
    ...
```

Patching with the correct prices updates the stub in-place. The `bid_fmvs` junction stays intact and now points to a row with prices.

**Prior-session context:** This risk was identified as "F1 — title fragility + NULL shadow" during the adversarial review of the PER-98 plan (May 2026). Title normalization was explicitly deferred as out of scope because the common case (LOCG lookup succeeds) produces Title-Case names. ALL-CAPS eBay titles were not called out specifically but the category of "title string mismatch" was understood as a gap at that time. (session history)

## Prevention

1. **Normalize titles in `upsert_comic`** (PER-123) — use `LOWER()` in all four lookup paths. Add a unit test confirming `upsert_comic("THE MIGHTY THOR", "154", 1968)` returns the same `id` as `upsert_comic("The Mighty Thor", "154", 1968)`.

2. **`extract-comics` should prefer well-valued rows** (PER-124) — before calling `upsert_fmv` with null prices, check whether a matching `fmv` row with non-null `low` already exists (case-insensitive title + issue + grade). If yes, link `bid_fmvs` to it directly instead of creating a stub.

3. **Run `/comic:verify` after every `/comic:buy` session** (PER-121) — the verify endpoint surfaces `fmv_stub` state (junction exists, prices null) and `needs_linking` state. Running it as the final step catches these gaps before the user leaves the session.

4. **Surface FMV link failures in snipe-add output** (PER-122) — if the `link-fmv` call fails, show `⚠️ Added (FMV link failed)` instead of `✅ Added` so the user knows immediately which items need recovery.

## Related

- `docs/solutions/fmv-bid-linkage-gap-2026-05-23.md` — parent investigation: missing `bid_fmvs` junction mechanism (the original root cause; this doc is the follow-up finding)
- `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md` — schema foundations for `comics`/`fmv`/`bid_fmvs` tables
- PER-118 — Run comic-fmv for 14 remaining stub comics (Spawn/Avengers)
- PER-119 — Fix 4 unlinked bids with no `bid_fmvs` junction
- PER-120 — Clean up duplicate ALL-CAPS yearless comics rows
- PER-121 — `/comic:buy` end-to-end FMV verification gate
- PER-122 — `gixen-cli add` FMV link status in output table
- PER-123 — `upsert_comic` case-insensitive title matching (the permanent fix)
- PER-124 — `extract-comics` should not create stubs when properly-valued row exists
- commit `0786617` (PER-103/104) — adjacent: yearless orphan row cleanup infrastructure
