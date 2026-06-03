# BUI-78 — Store seller-stated and photo-assessed grades per snipe

**Date:** 2026-06-03
**Linear:** BUI-78
**Status:** Approved — reviewed (2 passes); decisions A1/B1/C2/D2 resolved; ready for implementation plan

## Problem

The snipe pipeline captures a single grade per bid, collapsing two distinct
signals: the grade the seller *claimed* and the grade *photo assessment*
produced. Some sellers consistently over-grade (e.g. beatlebluecat claimed
VF/NM on Thor #171; photo assessment returned FN/VF). Storing both grades per
snipe lets us measure each seller's grade deviation over time.

## Goal

Build the **data-collection foundation plus a first-pass advisory** for seller
reliability:

1. Persist `seller_grade` and `photo_grade` per bid.
2. Expose a read endpoint returning a seller's average grade deviation.
3. Surface that signal advisorily inside `/comic:buy` (no automatic FMV/bid
   change — see Non-goals).

This is deliberately scoped as foundation + advisory. The high-leverage payoff
(automatic FMV/grade calibration from accumulated deviation) is a follow-up that
couples with BUI-81; this ticket makes that future work *possible* and gives the
user a human-readable signal in the meantime. The goal is stated this way (not
"automatic calibration") so success is judged against what ships.

## Non-goals (YAGNI)

- No automatic FMV calibration or max-bid adjustment (follow-up; couples with
  BUI-81). The user confirmed advisory-only for this ticket.
- No dashboard view or UI — the signal is consumed programmatically by `/comic:buy`.
- No all-sellers ranked endpoint — `/comic:buy` only ever queries one seller
  (cut as YAGNI; add when a consumer exists).
- No `max_deviation` field — no consumer; trivially recomputable later if needed.
- No update-in-place grade editing — grades are written once at bid creation
  (see Population). Re-grading an existing live snipe is a follow-up if it ever
  becomes a real workflow.
- No backfill of existing bids — pre-existing rows stay `NULL`.

## Data model (gixen-cli)

Two nullable columns on `bids` (`packages/gixen-cli/server/db.py`):

```sql
ALTER TABLE bids ADD COLUMN seller_grade  REAL  -- seller's stated grade, CGC float
ALTER TABLE bids ADD COLUMN photo_grade   REAL  -- photo-assessment consensus point estimate, CGC float
```

- `seller_grade` and `photo_grade` are CGC-scale floats, matching the existing
  `--grade` representation.
- `photo_grade` stores the grader's **consensus point estimate**. It is the *raw*
  photo assessment, independent of any user override at the Step 2.5 gate.
- **Decision B1:** `photo_grade_confidence` is **deferred** to the
  confidence-weighting follow-up. The grader's confidence is already carried in
  session state (`grade_confidence`), so it is not unrecoverable; adding the column
  now (with no consumer) would mirror the very YAGNI we cut `max_deviation` for.
  When the follow-up needs it, it adds the column then (with the 4→3 level collapse
  + a `CHECK` constraint).
- Added to the `_COLUMN_MIGRATIONS` list (same mechanism that added `fmv_id`),
  appended **after** the existing entries.
- **Also** added to `_BIDS_TABLE_SQL` — required, not optional. `_rebuild_bids_table`
  (BUI-79) copies only the columns it introspects from the renamed old table into
  the new table, so a rebuild preserves the new columns *only if* `_BIDS_TABLE_SQL`
  already declares them. Updating `_COLUMN_MIGRATIONS` alone is insufficient: a
  rebuild before/without the `_BIDS_TABLE_SQL` change would silently drop the
  columns. Both edits land together, and a test must assert they survive a rebuild.

## Seller identity (write + read use the same key)

The deviation analytics group by `bids.seller`, so the write key and the advisory
lookup key must match. Verified code facts:

- `insert_bid` **already** accepts a `seller` param and `_BIDS_TABLE_SQL` already
  declares the column — no schema/signature change for `seller`.
- `_sync_gixen` **already** passes `snipe.get("seller")` to `insert_bid`.
- The actual NULL-at-INSERT source is `_add_bid_row`, which hardcodes
  `seller=None` (server/main.py ~824). `api_add_bid` also does not forward a
  seller. **Fix:** `AddBidRequest` gains `seller`, `_add_bid_row` gains a `seller`
  param and forwards it to `insert_bid`, and `api_add_bid` passes `req.seller`.
- The Step 1 identify call already resolves the eBay **username** (`parse_item`
  returns `seller.username`), so `/comic:buy` can pass the same username to both
  the write payload and the advisory GET.

**Sync-overwrite fix (Decision A1).** `cache_gixen_data` currently updates with
`seller = COALESCE(?, seller)` (server/db.py ~489), so the next sync **overwrites**
the INSERT-time username with Gixen's scraped **store display name** (the BUI-68
alias map exists because these differ). Fix: change it to `COALESCE(seller, ?)`
(or guard: only write the scraped seller when `seller IS NULL`). Seller-per-item is
immutable, so never overwriting a non-NULL `seller` is safe. Effect: buy-flow rows
keep the username; web-added snipes (no INSERT username) still get their store name
on first sync — and they carry no grades, so they never enter the deviation query.

**Case-normalize.** eBay usernames are case-insensitive, so lowercase the username
on both write and the advisory query to avoid `BeatleBluecat` vs `beatlebluecat`
splitting one seller's history.

**To verify in planning:** confirm the COALESCE flip doesn't regress the dashboard
seller column for web-added snipes (it shouldn't — those start NULL and still fill).

## Population — write at bid creation (INSERT only)

Grades are bid attributes set when the bid is created, independent of FMV
linking, so they are written on the `POST /api/bids` path (which always runs),
not the conditional `link-fmv` path. Grades are written **once on INSERT** and
never updated in place (see Non-goals).

**CLI layer**
1. `cli.py add` gains optional options: `--seller-grade` (float), `--photo-grade`
   (float). Existing `--grade` is unchanged — it remains the grade used for FMV
   linking.

**HTTP transport**
2. The two grades and the resolved (lowercased) `seller` are added to the
   `POST /api/bids` JSON payload.

**Server model + DB write**
3. `AddBidRequest` (server/main.py) gains `seller_grade: float | None`,
   `photo_grade: float | None`, and `seller: str | None`. Note `AddBidRequest` is
   configured `extra="ignore"`, so a field missing from the model is silently
   dropped — a test must assert the values actually land in the DB, not just that
   `POST` returns 200.
4. Threaded through `_add_bid_row` → `insert_bid` to write on INSERT.
   - `insert_bid` already has a `seller` param; it gains **two** new ones
     (`seller_grade`, `photo_grade`) plus the two new columns in its INSERT list.
     Add the new params **keyword-only with `None` defaults** so the two existing
     call sites don't break.
   - `_add_bid_row` gains the new params (incl. `seller`) and forwards them;
     `api_add_bid` passes `req.seller` + the grades into it (today it forwards
     neither).
   - `_sync_gixen` already passes `snipe.get("seller")`; for the new grade params
     it relies on the `None` defaults — no change needed.
   - The direct-Gixen `cli.py add` path (no server URL) never hits `POST /api/bids`,
     so grades aren't captured there — acceptable; the buy flow always runs the server.

**Re-run / upsert (Decision C2 — fill-NULL only).** When `POST /api/bids` finds an
existing PENDING snipe it goes through `_modify_and_update_bid` → `update_bid`,
which writes no grades. Add a small `update_bid_grades` (or extend the update path)
that writes `seller_grade`/`photo_grade`/`seller` **only into columns that are
currently NULL** — i.e. completing an incomplete earlier insert (e.g. added without
grading, then re-run with grading). It never overwrites a non-NULL grade, so it is
not user-facing grade editing (still a non-goal). The `IntegrityError` recovery
branch in `_add_bid_row` (a rare concurrent-sync collision) uses the same fill-NULL
update so grades are not silently lost on that path either.

**Grade value normalization**
5. `seller_grade` is a CGC float, but identify returns a label/range ("VF/NM",
   "FN/VF", a range, or null). `comic-fmv`'s mapping is a *private* dict in
   `apps/fmv` and is **not importable** by a markdown skill, so the orchestrator
   can't "reuse" it directly. Instead, inline a small static label→float table in
   the skill (it's static data), or add a `comic-fmv --grade-to-float <label>`
   helper to shell out to. An ambiguous/unmappable label stores `NULL` (never a
   guess). `photo_grade` is the grader's consensus point estimate.

Null-safety: both new columns are optional. A bid added without grading stores
`photo_grade = NULL` and is excluded from deviation averages. An unmappable seller
grade stores `seller_grade = NULL`.

## Read endpoint (overlay)

New `GET /api/seller-reliability?seller=<name>` in
`plugins/gixen-overlay/src/gixen_overlay/routes.py` (the overlay shares the
gixen-cli DB and queries `bids` directly elsewhere). The `seller` query param is
**required** — there is no all-sellers form.

Response:

```json
{
  "seller": "beatlebluecat",
  "avg_deviation": 1.5,
  "sample_size": 4
}
```

Semantics:

- `avg_deviation = AVG(seller_grade - photo_grade)` over this seller's bids where
  **both** grades are non-null **and `status NOT IN ('PURGED','REMOVED')`**
  (exclude tombstones, matching the other overlay endpoints). Positive = seller
  claims higher than the photo grade (over-grades). Auction outcome is irrelevant
  to grading accuracy, so WON/LOST/ENDED/PENDING all count.
- `sample_size` = count of rows contributing to the average.
- **Query safety:** `seller` is bound as a SQL placeholder (`WHERE b.seller = ?`),
  never string-interpolated — matching every query in `routes.py`. Validate
  `seller` is non-empty and ≤128 chars, else 422. Apply the **same** validation to
  the write path (`AddBidRequest.seller` ≤128/non-empty) so both code paths agree.
- **Endpoint-down posture:** the Step 1 advisory GET is best-effort — on a
  connection error / non-200, `/comic:buy` treats it as `sample_size: 0` (show
  nothing, proceed), never blocking the buy flow.
- No server-side minimum-sample cutoff — the caller applies the floor (below).
- Unknown / zero-sample seller: return
  `{"seller": <name>, "avg_deviation": null, "sample_size": 0}` (HTTP 200, not
  404), so `/comic:buy` branches on `sample_size`.

(Confidence-weighted deviation is a documented follow-up; this endpoint weights
every dual-graded row equally.)

## `/comic:buy` integration — advisory only

- **Advisory read at Step 1 (identify), before the expensive grading step.** Once
  the listing's seller username is resolved, call
  `/api/seller-reliability?seller=<username>`. Surfacing the signal *before* Step
  2.5 lets the user decide whether photo-grading is worth running. The lookup is a
  cheap local GET. **Decision D2:** surface whenever `sample_size >= 1`, but label
  thin samples honestly so a single observation isn't read as an established
  pattern:

  > ⚠️ Seller `beatlebluecat` has over-stated condition by ~**+1.5** grade points
  > (n=4 prior assessments). Consider photo-grading this listing to verify
  > condition before bidding.

  For `sample_size` of 1–2, prefix **"early signal —"** and soften the wording
  (e.g. *"early signal — `seller` over-stated by ~+2.0 on 1 prior assessment"*).
  Render `avg_deviation` with an explicit sign. Recommending photo-grading the
  *current* listing is **not** circular — the deviation came from *prior* listings;
  grading this one is how you verify it. `sample_size == 0` → show nothing.

- **Bootstrapping the signal (addresses the near-empty-population problem).** Photo
  grading (Step 2.5) only runs for listings with *no* stated grade, but
  over-graders almost always state a grade — so under the default flow the very
  sellers this feature targets accumulate zero dual-graded rows. To let the signal
  build: Step 2.5's gate offers an **opt-in** "grade anyway (to record seller
  deviation)" for stated-grade listings, recommended when the seller is already
  flagged (`avg_deviation` high) or the book is high-value. Grading stays optional
  and user-gated — not forced on every stated-grade book.

- **Step 5 (snipe-add):** pass `--seller-grade`, `--photo-grade` (omit either if
  absent), and the resolved lowercased `seller`, alongside the existing
  `--comic-id`/`--grade`. `photo_grade` is the raw Step 2.5 consensus — **not** any
  value the user overrode at the gate (the override flows to `--grade`/FMV; the
  deviation must compare against the raw assessment). buy.md must keep the raw
  consensus as its own working-list field so an override doesn't clobber it.

## Testing

**gixen-cli (`packages/gixen-cli/tests`):**
- Migration adds `seller_grade`/`photo_grade`; present after `connect()`/`_apply_migrations`.
- Both columns survive a `bids` rebuild (the BUI-79 path) — explicit assertion.
- `insert_bid` / `POST /api/bids` persist both grades + `seller` when supplied.
- Null-safe: add without grades stores `NULL`, no error.
- `_sync_gixen` insert path still works (relies on `None` defaults for new params).
- **Sync no longer overwrites a non-NULL `seller`** (Decision A1): a sync cycle
  after a seller-bearing INSERT leaves `seller` unchanged.
- **Fill-NULL upsert** (Decision C2): add without grades → re-add with grades fills
  the NULL grade columns; a re-add never overwrites an already-set grade.

**overlay (`plugins/gixen-overlay/tests`):**
- `/api/seller-reliability?seller=X` returns correct `avg_deviation` (sign + value)
  and `sample_size`.
- Rows missing either grade, or tombstoned (`PURGED`/`REMOVED`), are excluded.
- Unknown / zero-sample seller returns `sample_size: 0`, `avg_deviation: null`, 200.
- Missing `seller` param → 422; over-long `seller` → 422; injection-y `seller`
  string is treated as a literal (parameterized), returns zero-sample.

## Affected files (anticipated)

- `packages/gixen-cli/server/db.py` — migration (2 cols) + `_BIDS_TABLE_SQL` +
  `insert_bid` (2 new keyword params; `seller` already exists) + a fill-NULL
  `update_bid_grades` (C2) + flip `cache_gixen_data` to `COALESCE(seller, ?)` (A1).
- `packages/gixen-cli/server/main.py` — `AddBidRequest` (+`seller`/grades),
  `_add_bid_row` (stop hardcoding `seller=None`; forward new params; use fill-NULL
  update on the recovery branch), `api_add_bid` (forward `req.seller`; fill-NULL on
  the existing-snipe path). No user-facing update-in-place of grades.
- `apps/ebay/...` / `.claude/commands/comic/identify.md` — `parse_item` already
  returns `seller.username`; surface it (lowercased) in identify's output (no
  column today).
- `packages/gixen-cli/cli.py` — `--seller-grade` / `--photo-grade` options +
  lowercased `seller` in payload.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py` — `GET /api/seller-reliability`.
- `.claude/commands/comic/grade.md` / `buy.md` Step 2.5 — opt-in "grade anyway"
  affordance for stated-grade listings; emit + preserve the raw consensus point
  estimate (separate from any user override).
- `.claude/commands/comic/snipe-add.md` — add the new flags to its canonical
  "available flags" table (a standalone session reads that table as ground truth).
- `.claude/commands/comic/buy.md` — Step 1 advisory read; pass grades/seller at Step 5.
- Tests in both packages.

## Decisions resolved in review (pass 2)

- **A1** — Stop sync overwriting the INSERT username: flip `cache_gixen_data` to
  `COALESCE(seller, ?)`; store the lowercased eBay username as the canonical key.
- **B1** — Defer `photo_grade_confidence` to the confidence-weighting follow-up;
  ship two columns now.
- **C2** — Fill-NULL-only upsert: a re-run completes an incomplete insert without
  overwriting set grades (not user-facing grade editing).
- **D2** — Advisory fires at `sample_size >= 1` with an explicit "early signal"
  label for n of 1–2.

## Open follow-ups (not this ticket)

- Confidence-weighted deviation: add the `photo_grade_confidence` column (deferred
  here, Decision B1) and use it to down-weight/exclude low-confidence assessments.
- Automatic FMV/grade calibration from accumulated deviation (couples with BUI-81).
- Re-grading an existing live snipe (a dedicated update path, if ever needed).
- Optional backfill of historical bids if seller-grade is recoverable from cached
  `ebay_title`.
