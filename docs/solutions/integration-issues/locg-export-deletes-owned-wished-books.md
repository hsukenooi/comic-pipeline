---
title: "LOCG Collection Export Deletes Owned Books (Wish Rows Carry In Collection=0)"
date: 2026-06-13
category: docs/solutions/integration-issues
module: locg-cli
problem_type: integration_issue
component: service_object
related_components:
  - locg-cli
  - gixen-overlay
  - comics
symptoms:
  - "LOCG Bulk Import reports 'Deleted from Collection, Added to Wish List' for books you own after a collection sync"
  - "18 owned books removed from the LOCG collection on the first real sync"
  - "wishlist-add adds issues you already own to the wish list"
  - "re-import inserts duplicate collection rows when LOCG canonicalizes a just-pushed book's Release Date"
root_cause: logic_error
resolution_type: code_fix
severity: critical
tags:
  - locg
  - bulk-import
  - wish-list
  - collection-sync
  - data-loss
  - in-collection
  - owned-safe
  - csv
  - bui-122
---

# LOCG Collection Export Deletes Owned Books (Wish Rows Carry In Collection=0)

## Problem

The collection export builds a LOCG bulk-import CSV in which **wish-list rows are written with `In Collection=0, In Wish List=1`**. The export appended the **entire** wish list every time, so any book that was both owned and on the wish list went up as `In Collection=0` — and LOCG's Bulk Import reads `In Collection=0` as "remove this from the collection." The first real sync **deleted 18 owned books** from the LOCG collection. The gixen server (the source of truth) was never touched; the damage was on LOCG and fully recoverable, but it is exactly the record-keeping corruption the sync was supposed to avoid.

## Symptoms

- LOCG Bulk Import result shows `Deleted from Collection, Added to Wish List` for owned books (e.g. Fantastic Four #86, owned Marvel Tales, X-Men reprints).
- After upload, those books are `In Collection=0, In Wish List=1` in a fresh LOCG export.
- `wishlist-add` had added issues already owned (the user wish-listed a Marvel Tales run they mostly owned; nothing stopped it).
- Re-importing the LOCG export created duplicate collection rows and left the originals stuck "pending" forever.

## What Didn't Work

- **Trusting green unit tests + a "passing" dry-run.** The import-reconciliation fix (U1) and the export both looked correct, but the verification gave **false positives**:
  - Unit tests used `make_agent_win_row(publisher="Marvel")`, a real publisher string. **Production `record-win` rows have `publisher_name=None`.**
  - The dry-run harness built the simulated LOCG re-export *from the cache rows themselves*, so both sides shared the same blank publisher — masking the real mismatch.
  - In reality, `agent_win` rows have no publisher while LOCG's export carries `"Marvel Comics"`, so the reconciliation's publisher gate (`_publisher_matches("", "Marvel Comics")`) scored 0 and the fix was a **no-op in production**. An adversarial code review caught this; a `publisher=None` regression test now locks it in.
- **Title-only matching for recovery.** Computing "which owned books were deleted" by `full_title` string match both over- and under-counted: it missed the Batman *One Bad Day* books (LOCG uses an en-dash `–`, the server a hyphen `-`) and falsely flagged owned-vs-wished different printings that share a title (e.g. an owned facsimile vs a wished original). The reliable method was an **identity-level diff** (title + series + release date) between a pre-upload and post-upload LOCG export, plus the importer's own "Deleted from Collection" labels.
- **Assuming name-only wish adds wouldn't import ("Not Found").** They *do* — LOCG adds a wish-list row by **title alone**. The "Release Date required → Not Found" rule from the bulk-import recipe applies to *collection* matches, not *wish* adds. So the blank-column wish rows were not harmless; the owned ones among them actively deleted collection entries.

## Solution

Three fixes (shipped together in PR #59 / BUI-122), defense in depth:

**B — Owned-safe, diff-only export** (`collection_io.py` `wish_rows_for_export`). The export now emits a wish row only when it is BOTH (1) a local-only add (no `series_name` — the diff LOCG doesn't already have; derived wishes are dropped) AND (2) **not owned** (`full_title` not in the collection's `in_collection` set). Matching is title-based and deliberately generous (dash- and leading-article-insensitive) — owned-safe by design, since over-matching only drops a wish from the push while under-matching could delete an owned book.

```python
def wish_rows_for_export(payload):
    owned = {_normalize_title(r.get("full_title"))
             for r in payload.get("comics", []) if r.get("in_collection")}
    out = []
    for item in _load_wish_list_items():
        if item.get("series_name"):                       # derived — LOCG already has it
            continue
        if _normalize_title(item.get("full_title")) in owned:  # owned — never In Collection=0
            continue
        out.append(item)
    return out
```

**A — `wishlist-add` ownership check** (`.claude/commands/comic/wishlist-add.md`). Collection-checks each resolved issue (`GET /api/comics/collection/check`) and skips ones already owned, surfacing them in the preview. Stops owned books from polluting the wish list in the first place.

**U1 — owned-safe import reconciliation** (`collection_io.py` `import_xlsx`). Re-import reconciles a just-pushed `agent_win` win even when LOCG canonicalizes its Release Date: a **publisher wildcard** (a missing publisher on either side matches — `agent_win` rows have none), **exact-year** date matching, and an **identity-collision guard** so reconciliation never creates a duplicate-identity row.

Effect on a copy of the production store: the export dropped from **621 rows (18 owned-book leaks) → 99 rows (0 leaks)**; the date-canonicalization case that produced +33 duplicate rows now reconciles with **0 duplicate-identity rows**.

## Why This Works

LOCG's bulk import is **stateful per column**: `In Collection=0` is an instruction to un-collect, not a no-op. The only safe way to push a wish list that may contain owned books is to never send `In Collection=0` for a book you own. Excluding owned books (and derived wishes LOCG already has) makes that guarantee structural — the dangerous row simply cannot be generated. The publisher-wildcard fix matters because `record-win` writes `publisher_name=None`, so any strict publisher comparison silently disables reconciliation for the exact rows it is meant to handle.

## Prevention

- **Never emit `In Collection=0` for an owned book.** Treat the LOCG wish-export as owned-gated, not a blind dump. Regression tests: `test_wish_export_excludes_owned_book`, `test_wish_export_owned_match_is_dash_and_article_insensitive`.
- **Test with production-shaped data.** `agent_win` rows have `publisher_name=None` and fabricated `YYYY-01-01` dates — fixtures must reflect that or they hide real bugs. Added `test_pending_agent_win_with_null_publisher_reconciles`.
- **Verify the LOCG side empirically, not just by simulation.** A dry-run that builds the "LOCG export" from your own cache rows can't reveal canonicalization mismatches (publisher, date). Use a real before/after LOCG export diff at identity level (title + series + release date), and watch the importer's per-row labels.
- **Upload in ≤20-row batches; retries are safe.** LOCG's bulk importer times out past ~20 rows (and was fully degraded during testing — `queue_import_comic` XHR `(canceled)`, multi-minute page loads). The owned-safe CSV is idempotent (`In Collection=1` / `In Wish List=1` re-applies as a no-op), so retry freely.
- **The pull list is never touched.** The LOCG bulk-import format has no pull-list column, so the sync cannot add/remove pull-list membership.
- **`record-win` should dedup against already-owned books** so duplicate win-records stop accumulating (tracked separately).

## Related Issues

- `packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md` — the operational runbook these fixes produced (the safe round-trip via `/comic:collection-sync`). Canonical companion; keep this doc and that one consistent.
- `integration-issues/locg-bulk-import-recipe-2026-05-22.md` — the 21-column CSV recipe; its Test 5 finding (LOCG silently rewrites Release Date) is the upstream root cause of the U1 duplicate-row bug.
- `packages/locg-cli/docs/solutions/logic-errors/collection-check-false-matches-2026-05-30.md` — collection-check matcher semantics (`in_collection` = copies-owned count); `wishlist-add`'s owned-skip relies on this matcher being correct.
- Linear: BUI-122 (umbrella), BUI-124 (import downgrade hardening), BUI-125 (data-hygiene cleanup of duplicate win-records).
