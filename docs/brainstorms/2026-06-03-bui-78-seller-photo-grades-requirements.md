# BUI-78 — Store seller-stated and photo-assessed grades per snipe

**Date:** 2026-06-03
**Linear:** BUI-78
**Status:** In review — pass-2 factual corrections applied; 4 open decisions (A–D) pending

## Problem

The snipe pipeline captures a single grade per bid, collapsing two distinct
signals: the grade the seller *claimed* and the grade *photo assessment*
produced. Some sellers consistently over-grade (e.g. beatlebluecat claimed
VF/NM on Thor #171; photo assessment returned FN/VF). Storing both grades per
snipe lets us measure each seller's grade deviation over time.

## Goal

Build the **data-collection foundation plus a first-pass advisory** for seller
reliability:

1. Persist `seller_grade`, `photo_grade`, and `photo_grade_confidence` per bid.
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

Three nullable columns on `bids` (`packages/gixen-cli/server/db.py`):

```sql
ALTER TABLE bids ADD COLUMN seller_grade            REAL  -- seller's stated grade, CGC float
ALTER TABLE bids ADD COLUMN photo_grade             REAL  -- photo-assessment consensus point estimate, CGC float
-- photo_grade_confidence: provisional — see Open Decision E (keep vs defer)
ALTER TABLE bids ADD COLUMN photo_grade_confidence  TEXT  -- 'high'|'medium'|'low'; NULL if not graded
```

> **Confidence levels:** the grader emits **four** bands (HIGH / MEDIUM /
> MEDIUM-LOW / LOW). If this column is kept (Open Decision E), define the collapse
> to the stored set (e.g. MEDIUM-LOW → `medium`), lowercase before writing, and
> add `CHECK(photo_grade_confidence IN ('high','medium','low'))` so a casing/level
> mismatch fails loudly instead of silently storing an off-enum string.

- `seller_grade` and `photo_grade` are CGC-scale floats, matching the existing
  `--grade` representation.
- `photo_grade` stores the grader's **consensus point estimate** (BUI-51 emits a
  range + confidence; we keep the point estimate and capture the confidence band
  separately — see below). It is the *raw* photo assessment, independent of any
  user override at the Step 2.5 gate.
- `photo_grade_confidence` is captured **because it is unrecoverable later** — the
  grader's range/confidence is ephemeral. Confidence-*weighting* of the deviation
  is a documented follow-up; storing it now avoids permanently losing the signal.
- Added to the `_COLUMN_MIGRATIONS` list (same mechanism that added `fmv_id`),
  appended **after** the existing entries.
- **Also** added to `_BIDS_TABLE_SQL` — required, not optional. `_rebuild_bids_table`
  (BUI-79) copies only the columns it introspects from the renamed old table into
  the new table, so a rebuild preserves the new columns *only if* `_BIDS_TABLE_SQL`
  already declares them. Updating `_COLUMN_MIGRATIONS` alone is insufficient: a
  rebuild before/without the `_BIDS_TABLE_SQL` change would silently drop the
  columns. Both edits land together, and a test must assert they survive a rebuild.

## Seller identity (write + read must use the same key) — ⚠️ see Open Decision A

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

**The unresolved problem (Open Decision A):** `cache_gixen_data` updates with
`seller = COALESCE(?, seller)` (server/db.py ~489), so the next background sync
**overwrites** the INSERT-time username with whatever Gixen scrapes — which is the
**store display name**, not the username (the BUI-68 alias map exists precisely
because these differ). Writing the username at INSERT is therefore clobbered on
the first sync, and an advisory keyed on the username finds zero rows. This must
be resolved before the feature works — see Open Decision A.

## Population — write at bid creation (INSERT only)

Grades are bid attributes set when the bid is created, independent of FMV
linking, so they are written on the `POST /api/bids` path (which always runs),
not the conditional `link-fmv` path. Grades are written **once on INSERT** and
never updated in place (see Non-goals).

**CLI layer**
1. `cli.py add` gains optional options: `--seller-grade` (float), `--photo-grade`
   (float), `--photo-grade-confidence` (`high|medium|low`). Existing `--grade` is
   unchanged — it remains the grade used for FMV linking.

**HTTP transport**
2. The grades, confidence, and resolved `seller` are added to the `POST /api/bids`
   JSON payload.

**Server model + DB write**
3. `AddBidRequest` (server/main.py) gains `seller_grade: float | None`,
   `photo_grade: float | None`, `photo_grade_confidence: str | None`, and
   `seller: str | None`. Note `AddBidRequest` is configured `extra="ignore"`, so
   a field missing from the model is silently dropped — a test must assert the
   values actually land in the DB, not just that `POST` returns 200.
4. Threaded through `_add_bid_row` → `insert_bid` to write on INSERT.
   - `insert_bid` already has a `seller` param; it gains **three** new ones
     (`seller_grade`, `photo_grade`, `photo_grade_confidence`) plus the three new
     columns in its INSERT list. Add the new params **keyword-only with `None`
     defaults** so the two existing call sites don't break.
   - `_add_bid_row` gains the new params (incl. `seller`) and forwards them;
     `api_add_bid` passes `req.seller` + the grades into it (today it forwards
     neither).
   - `_sync_gixen` already passes `snipe.get("seller")`; for the new grade params
     it relies on the `None` defaults — no change needed.
   - **Known gap:** `_add_bid_row`'s `IntegrityError` recovery branch calls
     `update_bid` (which writes none of these columns), so a snipe added during a
     concurrent-sync collision is recorded without grades/seller. Acceptable for
     v1 (rare race); documented rather than handled. See also Open Decision D
     (re-run).
   - The direct-Gixen `cli.py add` path (no server URL) never hits `POST /api/bids`,
     so grades aren't captured there — acceptable; the buy flow always runs the server.

**Grade value normalization**
5. `seller_grade` is a CGC float, but identify returns a label/range ("VF/NM",
   "FN/VF", a range, or null). `comic-fmv`'s mapping is a *private* dict in
   `apps/fmv` and is **not importable** by a markdown skill, so the orchestrator
   can't "reuse" it directly. Instead, inline a small static label→float table in
   the skill (it's static data), or add a `comic-fmv --grade-to-float <label>`
   helper to shell out to. An ambiguous/unmappable label stores `NULL` (never a
   guess). `photo_grade` is the grader's consensus point estimate.

Null-safety: every new column is optional. A bid added without grading stores
`photo_grade = NULL` / `photo_grade_confidence = NULL` and is excluded from
deviation averages. An unmappable seller grade stores `seller_grade = NULL`.

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

(`photo_grade_confidence` is stored but not yet used to weight the average —
confidence-weighted deviation is the documented follow-up.)

## `/comic:buy` integration — advisory only

- **Advisory read at Step 1 (identify), before the expensive grading step.** Once
  the listing's seller username is resolved, call
  `/api/seller-reliability?seller=<username>`. Surfacing the signal *before* Step
  2.5 lets the user decide whether photo-grading is worth running. The lookup is a
  cheap local GET. Surface only when `sample_size >= 3` (below that it is anecdote,
  not pattern):

  > ⚠️ Seller `beatlebluecat` has over-stated condition by ~**+1.5** grade points
  > on average (n=4 prior assessments). Consider photo-grading this listing to
  > verify condition before bidding.

  (Recommending photo-grading the *current* listing is **not** circular — the
  deviation was computed from *prior* listings; grading this one is how you verify
  it. Render `avg_deviation` with an explicit sign.) When `sample_size` is below
  the floor (Open Decision F), show nothing.

- **Bootstrapping the signal (addresses the near-empty-population problem).** Photo
  grading (Step 2.5) only runs for listings with *no* stated grade, but
  over-graders almost always state a grade — so under the default flow the very
  sellers this feature targets accumulate zero dual-graded rows. To let the signal
  build: Step 2.5's gate offers an **opt-in** "grade anyway (to record seller
  deviation)" for stated-grade listings, recommended when the seller is already
  flagged (`avg_deviation` high) or the book is high-value. Grading stays optional
  and user-gated — not forced on every stated-grade book.

- **Step 5 (snipe-add):** pass `--seller-grade`, `--photo-grade`,
  `--photo-grade-confidence` (omit any that are absent), and the resolved
  `seller`, alongside the existing `--comic-id`/`--grade`. `photo_grade` is the
  raw Step 2.5 consensus — **not** any value the user overrode at the gate (the
  override flows to `--grade`/FMV; the deviation must compare against the raw
  assessment).

## Testing

**gixen-cli (`packages/gixen-cli/tests`):**
- Migration adds the three columns; present after `connect()`/`_apply_migrations`.
- Columns survive a `bids` rebuild (the BUI-79 path) — explicit assertion.
- `insert_bid` / `POST /api/bids` persist all three grade fields + `seller` when supplied.
- Null-safe: add without grades stores `NULL`, no error.
- `_sync_gixen` insert path still works (passes `None` for the new fields).

**overlay (`plugins/gixen-overlay/tests`):**
- `/api/seller-reliability?seller=X` returns correct `avg_deviation` (sign + value)
  and `sample_size`.
- Rows missing either grade, or tombstoned (`PURGED`/`REMOVED`), are excluded.
- Unknown / zero-sample seller returns `sample_size: 0`, `avg_deviation: null`, 200.
- Missing `seller` param → 422; over-long `seller` → 422; injection-y `seller`
  string is treated as a literal (parameterized), returns zero-sample.

## Affected files (anticipated)

- `packages/gixen-cli/server/db.py` — migration + `_BIDS_TABLE_SQL` + `insert_bid`
  (3 new keyword params; `seller` already exists). Decision A touches
  `cache_gixen_data`'s COALESCE; Decision C may add a fill-NULL `update_bid_grades`.
- `packages/gixen-cli/server/main.py` — `AddBidRequest` (+`seller`/grades),
  `_add_bid_row` (stop hardcoding `seller=None`; forward new params), `api_add_bid`
  (forward `req.seller`). No user-facing update-in-place of grades.
- `apps/ebay/...` / `.claude/commands/comic/identify.md` — `parse_item` already
  returns `seller.username`; surface it in identify's output table (no column today).
- `packages/gixen-cli/cli.py` — `--seller-grade` / `--photo-grade` /
  `--photo-grade-confidence` options + `seller` in payload.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py` — `GET /api/seller-reliability`.
- `.claude/commands/comic/grade.md` / `buy.md` Step 2.5 — opt-in "grade anyway"
  affordance for stated-grade listings; emit the consensus point estimate +
  confidence.
- `.claude/commands/comic/snipe-add.md` — add the new flags to its canonical
  "available flags" table (a standalone session reads that table as ground truth).
- `.claude/commands/comic/buy.md` — Step 1 advisory read; pass grades/seller at Step 5.
- Tests in both packages.

## Open decisions (raised in review pass 2 — resolve before planning)

**A. Seller key: stop sync from overwriting the INSERT username.** `cache_gixen_data`
does `seller = COALESCE(?, seller)`, so a sync overwrites the INSERT username with
Gixen's scraped store display name. Options: (A1) flip to `COALESCE(seller, ?)` /
guard so a non-NULL existing `seller` is never overwritten (INSERT username wins);
(A2) accept Gixen's store-name as the stored key and normalize via the BUI-68 alias
map on the *read* side. **Recommended: A1** (simplest, keeps one canonical key) —
needs a test asserting a sync cycle doesn't change a seller-bearing row.

**B. `photo_grade_confidence`: keep now or defer?** scope-guardian notes it has no
consumer in this ticket (weighting is deferred) and that `grade_confidence` is
already in session state, so the "unrecoverable" rationale is weak — inconsistent
with cutting `max_deviation`/all-sellers on the same YAGNI logic. Options: (B1)
**defer** the column to the confidence-weighting follow-up (ship 2 columns now);
(B2) keep it, with the 4→3 collapse + CHECK above. **Recommended: B1** (defer).

**C. Re-run / upsert grades.** A re-run of buy for an existing PENDING snipe goes
through `update_bid`, which writes no grades, leaving stale/NULL grade columns.
Options: (C1) document "first write wins" as a known limitation; (C2) add a
fill-NULL-only partial update (write grade columns only when the existing row's are
NULL — completing an incomplete insert, not user-editing). **Recommended: C2.**

**D. Advisory floor / scope.** The `n≥3` floor + opt-in bootstrap may rarely fire
for a solo user's infrequent sellers; product-lens questions whether the advisory
delivers value within this ticket. Options: (D1) keep `n≥3`; (D2) lower to `n≥1`
with an explicit "early signal — single observation" label; (D3) drop the advisory
from BUI-78 and ship the data-collection foundation only, with the advisory as the
follow-up. **Recommended: D2** (useful from day one, honestly labelled).

## Open follow-ups (not this ticket)

- Confidence-weighted deviation (use `photo_grade_confidence` to down-weight or
  exclude low-confidence assessments).
- Automatic FMV/grade calibration from accumulated deviation (couples with BUI-81).
- Re-grading an existing live snipe (a dedicated update path, if ever needed).
- Optional backfill of historical bids if seller-grade is recoverable from cached
  `ebay_title`.
