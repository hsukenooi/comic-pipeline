---
title: "Null FMV on dashboard when bid_fmvs points to stub fmv rows"
date: 2026-05-23
problem_type: logic_error
component: database
severity: medium
root_cause: missing_workflow_step
resolution_type: seed_data_update
tags: [fmv, bid_fmvs, extract-comics, stub-rows, comic-fmv, dashboard]
module: gixen-overlay
related_issues: [PER-118]
related_docs:
  - docs/solutions/fmv-bid-linkage-gap-2026-05-23.md
  - docs/solutions/database-issues/fmv-stub-row-case-mismatch-2026-05-23.md
---

## Problem

Active snipes show `cond_grade` (grade exists) but null `fmv_low`/`fmv_high` on the `/comics` dashboard, even though `bid_fmvs` rows exist and `lot_count >= 1`. The FMV values were never computed for the fmv rows that the bids link to.

## Symptoms

- `/comics` dashboard displays `—` for FMV column on specific rows
- `GET /api/comics/snipes` returns `lot_count=1`, `cond_grade` set, but `fmv_low=null`, `fmv_high=null`
- The affected bids were added before `comic-fmv` ran, or the bids link to stub fmv rows created by `POST /api/extract-comics` rather than to canonical comic rows

## What Didn't Work

**Using `POST /api/bids/{item_id}/link-fmv` to re-link:** When a bid already has a `bid_fmvs` entry (lot_count=1), calling `link-fmv` demotes the existing primary entry to non-primary and inserts a new one. This raises `lot_count` to 2 with one null fmv row still present. `_build_comics_row()` in `routes.py` nulls out FMV when `null_count >= 1`, so the dashboard still shows `—`. Only use `link-fmv` when `lot_count=0`.

**Verifying FMV via POST /api/comics response:** The `POST /api/comics` endpoint returns only the `comics` table row (not joined with `fmv`). `r.get('fmv_low')` will return `None` even when the fmv row was successfully updated. Use `GET /api/comics/snipes` or `GET /api/comics?locg_id=&grade=` to verify actual state.

## Solution

Two patterns cause this problem; each requires a different fix strategy.

### Pattern A — Stub fmv rows with correct comic identity (Spawn-style)

The bid links to an `fmv` row under a comic that has the correct `locg_id` but `fmv_low=null` because `comic-fmv` was never run. Run `comic-fmv --batch`:

```json
[
  {
    "item_id": "287654321098",
    "title": "Spawn",
    "issue": "227",
    "year": 0,
    "grade": 9.6,
    "locg_id": 123456,
    "publisher": "Image Comics"
  }
]
```

```bash
export GIXEN_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080
export SERPAPI_KEY=...
comic-fmv --batch batch.json --out results.json
```

**Year field:** Use `year=0` (not `null`) for comics where the overlay stores the row with `year=0`. The DB lookup for yearless comics uses `WHERE year=0`. Sending `year=null` in the batch causes `comic-fmv` to skip the DB cache check and still compute fresh FMV, but is semantically correct only when the DB row truly has `year IS NULL`.

`upsert_fmv()` uses `COALESCE(excluded.low, low)` — it safely overwrites a null `low`/`high` with the newly computed value, and preserves existing non-null values if the new computation returns null.

### Pattern B — Bid links to stub comic row (extract-comics-style)

`POST /api/extract-comics` creates stub comic rows with mangled eBay titles (e.g., `"Avengers Bronze age Vision Declares his love for Wanda"`). The bid's `bid_fmvs` entry points to that stub fmv row, not to the canonical `"Avengers"` row. Running `comic-fmv` with the canonical title creates or updates the canonical row — but `bid_fmvs` still points to the stub.

**Fix:** After running `comic-fmv` to get the computed FMV values, directly update the stub fmv row in place by posting with the stub's exact stored title/issue/year:

```bash
# Find the stub title from the DB or GET /api/comics/snipes
curl -s "$GIXEN_SERVER_URL/api/comics/snipes" | jq '.[] | select(.item_id == "287654321098") | {title, issue, year, cond_grade}'

# Patch the stub row using its stored title
curl -s -X POST "$GIXEN_SERVER_URL/api/comics" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Avengers Bronze age Vision Declares his love for Wanda",
    "issue": "81",
    "year": null,
    "grade": 7.0,
    "fmv_low": 15.0,
    "fmv_high": 25.0,
    "fmv_comps": 8,
    "fmv_confidence": "medium"
  }'
```

This hits `upsert_comic()` with the exact stub title, finds the existing row, and calls `upsert_fmv()` to update its FMV in place. The `bid_fmvs` link is unchanged and the dashboard now reads non-null FMV.

### LOCG ID resolution when locg-cli is blocked

`locg-cli` returns empty responses when Cloudflare blocks LOCG (HTTP 403). Resolve `locg_id` via firecrawl instead:

```bash
# Find the issue on LOCG via Google
firecrawl search site:leagueofcomicgeeks.com "Spawn" "227" comic issue 1994 -o .firecrawl/spawn-227.json --json

# Navigate from a known anchor if nearby issues are known
# e.g., Spawn #299 locg_id=2776658 → navigate back to #269 via pagination
```

The `locg_id` appears in LOCG URLs as the first numeric segment: `leagueofcomicgeeks.com/comics/2776123/spawn-227`.

## Why This Works

`upsert_fmv()` uses `INSERT OR REPLACE … ON CONFLICT(comic_id, grade) DO UPDATE SET low=COALESCE(excluded.low, low)`. This means:

- Calling it with `fmv_low=15.0` on a row where `low` was `null` → overwrites null with 15.0
- Calling it with `fmv_low=null` on a row where `low` was `15.0` → preserves 15.0

The `bid_fmvs` link is never touched. Once `fmv.low` and `fmv.high` are non-null, `SUM(f.low)` in `_COMICS_AGGREGATES` returns a real value and the dashboard populates correctly.

For Pattern B: posting with the stub's exact title causes `upsert_comic()` to find the existing stub row (title is the match key), so the fmv update lands on the row that `bid_fmvs` already points to.

## Prevention

1. **Run `comic-fmv` before `gixen-cli add`** — the `/comic:buy` workflow order guarantees this, but ad-hoc snipe adds may skip it. If `gixen-cli add --locg-id` + `--grade` is provided, the overlay endpoint `POST /api/bids/{item_id}/link-fmv` will link on add (only useful when `lot_count=0`).

2. **Avoid `POST /api/extract-comics` as a primary FMV path** — it creates stub rows with eBay titles that diverge from canonical LOCG titles. Use it only as a recovery tool to get a bid linked; then follow up with the stub-patching strategy here.

3. **Detect stubs before running batch** — query `GET /api/comics/snipes` and filter for rows where `lot_count >= 1` and `fmv_low IS NULL`. These need the stub-patch fix, not just a fresh `comic-fmv` run on the canonical title.

4. **Always verify FMV via `/api/comics/snipes`** — `POST /api/comics` returns only the `comics` table row, not the joined fmv values. A `null` in the POST response does not mean the fmv update failed.
