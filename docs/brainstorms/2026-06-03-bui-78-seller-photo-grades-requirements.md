# BUI-78 — Store seller-stated and photo-assessed grades per snipe

**Date:** 2026-06-03
**Linear:** BUI-78
**Status:** Design approved; ready for implementation plan

## Problem

The snipe pipeline captures a single grade per bid, collapsing two distinct
signals: the grade the seller *claimed* and the grade *photo assessment*
produced. Some sellers consistently over-grade (e.g. beatlebluecat claimed
VF/NM on Thor #171; photo assessment returned FN/VF). With both grades stored
per snipe we can compute each seller's average grade deviation over time and
surface unreliable sellers during `/comic:buy` before a bid is placed.

## Goal

Persist `seller_grade` and `photo_grade` per bid, expose a read endpoint that
reports per-seller average deviation, and surface that signal advisorily inside
`/comic:buy`. No automatic FMV/bid adjustment in this ticket.

## Non-goals (YAGNI)

- No dashboard view or UI — the signal is consumed programmatically by `/comic:buy`.
- No automatic FMV calibration or max-bid adjustment (future follow-up; couples
  with BUI-81 grader-calibration work).
- No backfill of existing bids — pre-existing rows stay `NULL` until re-added.

## Data model (gixen-cli)

Two nullable numeric columns on `bids` (`packages/gixen-cli/server/db.py`),
CGC-scale floats, matching the existing `--grade` representation:

```sql
ALTER TABLE bids ADD COLUMN seller_grade REAL  -- seller's stated grade
ALTER TABLE bids ADD COLUMN photo_grade  REAL  -- photo-assessed consensus; NULL if grading skipped
```

- Added to the `_COLUMN_MIGRATIONS` list (same mechanism that added `fmv_id`).
- Also added to `_BIDS_TABLE_SQL` so a freshly created DB matches a migrated one.
- Rebuild-safe: the BUI-79 column-introspection copy carries any `bids` columns
  across the rename/rebuild rebuilds, so no extra handling is needed there.

## Population — write at bid creation

Grades are bid attributes set when the bid is created, independent of FMV
linking, so they are written on the `POST /api/bids` path (which always runs),
not the conditional `link-fmv` path.

1. `cli.py add` gains two optional float options: `--seller-grade`,
   `--photo-grade`. The existing `--grade` is unchanged — it remains the grade
   used for FMV linking.
2. The two grades are added to the `POST /api/bids` JSON payload.
3. `AddBidRequest` (server/main.py) gains `seller_grade: float | None` and
   `photo_grade: float | None`.
4. They are threaded through `_add_bid_row` → `insert_bid` to write on INSERT.
5. On the update-in-place path (`_modify_and_update_bid`, taken when a live
   snipe already exists), refresh each grade **only when the request provides
   it** (a `NULL`/omitted value leaves the stored value untouched). This lets a
   re-add after re-grading update the grades without clobbering them on an
   unrelated max-bid edit.

Null-safety: both columns are optional everywhere. A bid added without grading
(no `--photo-grade`) stores `photo_grade = NULL` and is excluded from deviation
averages.

## Read endpoint (overlay)

New `GET /api/seller-reliability` in
`plugins/gixen-overlay/src/gixen_overlay/routes.py`. The overlay already shares
the gixen-cli DB (`request.app.state.db`) and queries `bids` directly elsewhere.

- `GET /api/seller-reliability?seller=<name>` → a single object for that seller.
- `GET /api/seller-reliability` → a list of all sellers, ranked by
  `avg_deviation` descending (most over-grading first).

Per-seller shape:

```json
{
  "seller": "beatlebluecat",
  "avg_deviation": 1.5,
  "max_deviation": 2.0,
  "sample_size": 4
}
```

Semantics:

- `avg_deviation = AVG(seller_grade - photo_grade)` over this seller's bids
  where **both** grades are non-null. Positive = seller claims higher than the
  photo grade (over-grades).
- `max_deviation = MAX(seller_grade - photo_grade)` over the same rows.
- `sample_size` = count of rows with both grades.
- No server-side minimum-sample cutoff — the caller decides whether to trust
  thin data.
- Unknown seller, or a seller with no dual-graded bids: return
  `{"seller": <name>, "avg_deviation": null, "max_deviation": null,
  "sample_size": 0}` (HTTP 200, not 404), so `/comic:buy` branches on
  `sample_size` rather than handling an error.

## `/comic:buy` integration — advisory only

- **Step 5 (snipe-add):** pass `--seller-grade` (from the Step 1 identify parse)
  and `--photo-grade` (from the Step 2.5 grade consensus; omit when grading was
  skipped) to `cli.py add`, alongside the existing `--comic-id`/`--grade`.
- **Advisory read:** near the grade/identify presentation, before max-bid
  approval, call `/api/seller-reliability?seller=<listing seller>`. When
  `sample_size > 0`, surface a line such as:

  > ⚠️ Seller `beatlebluecat` historically over-grades by **+1.5** (n=4) — consider grading from photos before bidding.

  When `sample_size == 0`, show nothing (or a neutral "no history" note). No
  automatic change to grade, FMV, or max bid.

- **Seller-name normalization:** `bids.seller` holds the eBay *username*. The
  listing's displayed seller may be a store name. The advisory lookup must
  query by the same username key the bids are stored under — reuse the BUI-68
  store-name → username alias map if the buy flow only has a store name.
  Otherwise a known over-grader silently looks like a zero-sample seller.

## Testing

**gixen-cli (`packages/gixen-cli/tests`):**
- Migration adds `seller_grade`/`photo_grade`; columns present after
  `connect()`/`_apply_migrations`.
- Columns survive a `bids` rebuild (the BUI-79 path).
- `insert_bid` / `POST /api/bids` persist both grades when supplied.
- Update-in-place refreshes a grade when provided and leaves it untouched when
  omitted.
- Null-safe: add without grades stores `NULL`, no error.

**overlay (`plugins/gixen-overlay/tests`):**
- `/api/seller-reliability?seller=X` returns correct `avg_deviation` (sign and
  value), `max_deviation`, and `sample_size`.
- Rows missing either grade are excluded from the average.
- No-arg form returns all sellers ranked by `avg_deviation` desc.
- Unknown / zero-sample seller returns `sample_size: 0`, `avg_deviation: null`,
  HTTP 200.

## Affected files (anticipated)

- `packages/gixen-cli/server/db.py` — migration + `_BIDS_TABLE_SQL` + `insert_bid`.
- `packages/gixen-cli/server/main.py` — `AddBidRequest`, `_add_bid_row`,
  `_modify_and_update_bid`.
- `packages/gixen-cli/cli.py` — `--seller-grade` / `--photo-grade` options + payload.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py` — `GET /api/seller-reliability`.
- `.claude/commands/comic/buy.md` (and `snipe-add.md`) — pass both grades + advisory read.
- Tests in both packages.

## Open follow-ups (not this ticket)

- Automatic FMV/bid calibration from accumulated deviation data.
- Optional backfill of historical bids if seller-grade can be recovered from
  cached `ebay_title`.
