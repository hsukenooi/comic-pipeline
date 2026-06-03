---
title: "feat: Store seller + photo grades per snipe and surface seller reliability"
type: feat
status: active
date: 2026-06-03
origin: docs/brainstorms/2026-06-03-bui-78-seller-photo-grades-requirements.md
---

# feat: Store seller + photo grades per snipe and surface seller reliability (BUI-78)

## Overview

Persist two grades per bid — the seller's *stated* grade and the *photo-assessed*
grade — so we can measure each seller's average grade deviation over time. Add a
read-only `GET /api/seller-reliability` endpoint to the overlay, and wire
`/comic:buy` to (a) capture both grades when adding a snipe and (b) show a
**first-pass advisory** for the listing's seller before bidding. No automatic
FMV/bid adjustment in this ticket.

## Problem Frame

The snipe pipeline records a single grade per bid, collapsing "what the seller
claimed" and "what photo assessment produced." Some sellers consistently
over-grade (beatlebluecat claimed VF/NM on Thor #171; photos returned FN/VF).
Storing both per snipe lets `/comic:buy` flag unreliable sellers before a bid and
lays the data foundation for future automatic calibration (BUI-81).
(see origin: docs/brainstorms/2026-06-03-bui-78-seller-photo-grades-requirements.md)

## Requirements Trace

- **R1** — Store `seller_grade` and `photo_grade` per snipe when available
  (null-safe; photo grade optional). Origin AC #1, #3.
- **R2** — A query can report average `(seller_grade − photo_grade)` grouped by
  seller. Origin AC #2 → `GET /api/seller-reliability`.
- **R3** — `/comic:buy` captures both grades at snipe-add and surfaces an advisory
  for the listing's seller (advisory-only).
- **R4 (A1)** — Seller identity is a single canonical key: the lowercased eBay
  username, written at INSERT and never clobbered by sync.
- **R5 (C2)** — A re-run fills NULL grade columns on an existing snipe without
  overwriting already-set grades (not user-facing grade editing).
- **R6 (D2)** — Advisory fires at `sample_size ≥ 1`, labelling thin (n=1–2)
  samples as an "early signal."

## Scope Boundaries

- No automatic FMV calibration or max-bid adjustment (advisory-only).
- No dashboard view/UI — the endpoint is consumed programmatically by `/comic:buy`.
- No all-sellers ranked endpoint and no `max_deviation` field (no consumer).
- No user-facing update-in-place grade editing (only fill-NULL completion, R5).
- No backfill of historical bids — pre-existing rows stay `NULL`.

### Deferred to Separate Tasks

- **B1 — `photo_grade_confidence` column + confidence-weighted deviation:** deferred
  to the confidence-weighting follow-up (couples with BUI-81). Ship two columns now.
- Automatic FMV/grade calibration from accumulated deviation: BUI-81 follow-up.

## Context & Research

### Relevant Code and Patterns

- **Column migration:** `packages/gixen-cli/server/db.py` — `_COLUMN_MIGRATIONS`
  (append `ALTER TABLE bids ADD COLUMN ...`; `_apply_migrations` swallows only
  "duplicate column", so ALTERs are idempotent) **and** `_BIDS_TABLE_SQL` (the
  authoritative schema `_rebuild_bids_table` copies into — a column missing here is
  silently dropped on any future rebuild). Both edit together.
- **Write path:** `insert_bid(conn, item_id, max_bid, bid_offset, snipe_group, seller)`
  (db.py ~386, all positional, `seller` already present); `update_bid` (~428, writes
  only max_bid/offset/group); `cache_gixen_data` (~463) with `seller=COALESCE(?, seller)`
  at ~489. `server/main.py`: `_add_bid_row` (~805, hardcodes `seller=None` at ~824,
  `IntegrityError` → `update_bid` recovery), `_modify_and_update_bid` (~787),
  `api_add_bid` (~834, forwards neither seller nor grades), `_sync_gixen` (~322 already
  passes `snipe.get("seller")`).
- **Request model:** `AddBidRequest` (main.py ~683) is `extra="ignore"` — undeclared
  fields are silently dropped, so new fields MUST be declared. `@field_validator`
  uses `re.match(r"^\d+$", v)`.
- **CLI:** `packages/gixen-cli/cli.py` `add` — `@click.option("--flag", type=float,
  default=None)`; payload built as a dict then `_server_request("post","/api/bids",json=payload)`.
- **Overlay routes:** `plugins/gixen-overlay/src/gixen_overlay/routes.py` — `db =
  request.app.state.db`, `?`-placeholder queries, inline `re.match`/422 validation,
  `status NOT IN ('PURGED','REMOVED')` on every `bids` query, `router = APIRouter()`.
- **Seller source:** `apps/ebay/src/ebay_fetch.py` `parse_item` (~325) returns
  `seller` = `seller_data.get("username")`. `load_seller_aliases` (~378) lowercases
  keys (case-normalization precedent). `.claude/commands/comic/identify.md` does NOT
  currently surface `seller`.
- **Tests:** `packages/gixen-cli/tests/test_server_db.py` — `init_db(tmp_path/...)`
  fixture, `PRAGMA table_info(bids)` for column checks, raw-sqlite legacy-schema seeds
  for migration tests. `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py` —
  the `api` TestClient fixture (real plugin + mocked `GixenClient`, `DB_PATH` env),
  direct `sqlite3.connect(os.environ["DB_PATH"])` to seed. Run with `uv run pytest`
  from each package dir (per CLAUDE.md).

### Institutional Learnings

- **BUI-50 endpoint parity** (`docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`):
  every `bids` query needs `status NOT IN ('PURGED','REMOVED')`, and if a query has an
  inner dedup subquery the filter must be **inside** it. The seller-reliability query is
  a single aggregate (no dedup subquery), so one WHERE suffices — but it must be present.
- **Plugin-owned read endpoints** (`docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md`):
  overlay endpoints typically call `_ensure_fresh_sync` + `_spawn_fallback_task` or
  must justify skipping. **Decision:** seller-reliability reads *historical, locally
  written* grades (not live Gixen state), so it does **not** trigger a sync — justified.
- **SQLite FK-rename/SAVEPOINT** (`docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md`):
  BUI-78 only *adds columns*; it does not introduce a new rebuild. The only obligation
  is keeping `_BIDS_TABLE_SQL` in sync so a future rebuild preserves the columns.
- **Stub-FMV COALESCE contract** (`docs/solutions/database-issues/stub-fmv-null-after-extract-comics-2026-05-23.md`):
  `upsert_fmv` uses `COALESCE(excluded.x, x)` to avoid clobbering with NULL — mirror
  this for the fill-NULL grade update (R5/C2).
- **Seller alias (BUI-68)** (`docs/plans/2026-05-24-001-feat-seller-scan-plan.md`):
  eBay's Browse API returns the username (`parse_item` confirms), while Gixen scrapes the
  store display name — the reason A1 must pin the username at INSERT and stop sync overwrite.

## Key Technical Decisions

- **A1 — canonical seller key:** flip `cache_gixen_data` to `seller = COALESCE(seller, ?)`
  so a non-NULL (INSERT-time) seller is never overwritten; write the **lowercased** eBay
  username at INSERT. Only buy-flow (graded) rows feed the deviation query, so they carry
  the username consistently; seller-per-item is immutable, making "don't overwrite" safe.
- **B1 — defer `photo_grade_confidence`:** ship two columns; the follow-up adds the column
  (with a 4→3 level collapse + `CHECK`) when confidence-weighting has a consumer.
- **C2 — fill-NULL upsert:** a re-run/`IntegrityError`-recovery writes grade columns only
  where currently NULL (COALESCE-preserve), completing an incomplete insert — not editing.
- **D2 — advisory floor:** fire at `sample_size ≥ 1`; prefix "early signal —" and soften
  wording for n=1–2. Recommend photo-grading the *current* listing (not circular — the
  deviation came from *prior* listings).
- **New `insert_bid` params are keyword-only with `None` defaults** so the two existing
  call sites (`_add_bid_row`, `_sync_gixen`) keep working without edits beyond intent.
- **Endpoint skips `_ensure_fresh_sync`** (justified above) — grades are local writes.

## Open Questions

### Resolved During Planning

- Where do grades get written? — `POST /api/bids` (always runs); not `link-fmv`.
- Which seller string is the key? — lowercased eBay username from identify (A1).
- How is the noisy/4-level grader confidence handled? — deferred (B1).
- Does the endpoint need a fresh sync? — no (reads local historical grades).

### Deferred to Implementation

- **Verify the A1 COALESCE flip doesn't regress the dashboard seller column for
  web-added snipes** (they start NULL and should still fill on first sync).
- Exact label→float conversion: inline a small static CGC label→float table in the
  buy/identify skill (the `comic-fmv` map is a private `apps/fmv` dict, not importable),
  or add a `comic-fmv --grade-to-float` helper — pick during implementation; unmappable → NULL.
- Final SQL text of the fill-NULL update and the aggregate query (settle against real code).

## Implementation Units

- [ ] **Unit 1: Add `seller_grade` / `photo_grade` columns to `bids`**

**Goal:** Two nullable REAL columns on `bids`, migration-safe and rebuild-safe.

**Requirements:** R1

**Dependencies:** None.

**Files:**
- Modify: `packages/gixen-cli/server/db.py` (`_COLUMN_MIGRATIONS`, `_BIDS_TABLE_SQL`)
- Test: `packages/gixen-cli/tests/test_server_db.py`

**Approach:**
- Append `ALTER TABLE bids ADD COLUMN seller_grade REAL` and `... photo_grade REAL`
  to `_COLUMN_MIGRATIONS` (after existing entries).
- Add the same two columns to `_BIDS_TABLE_SQL` so `_rebuild_bids_table` preserves them.
- No new rebuild logic — additive only.

**Patterns to follow:** the `fmv_id` column addition (the last `_COLUMN_MIGRATIONS` entry)
and its presence in `_BIDS_TABLE_SQL`.

**Test scenarios:**
- Happy path: after `init_db`, `PRAGMA table_info(bids)` includes `seller_grade` and `photo_grade`.
- Edge case: re-running migrations on an already-migrated DB is a no-op (idempotent ALTER).
- Integration: both columns survive a `bids` rebuild — seed a row with both grades set,
  force the rebuild path, assert the values persist (guards the `_BIDS_TABLE_SQL` sync).

**Verification:** columns exist on a fresh DB and on a migrated DB, and survive a rebuild.

---

- [ ] **Unit 2: Persist grades + canonical seller at bid creation**

**Goal:** Write both grades and the lowercased username on INSERT; fill NULL grades on
re-run; stop sync from overwriting the seller.

**Requirements:** R1, R4 (A1), R5 (C2)

**Dependencies:** Unit 1.

**Files:**
- Modify: `packages/gixen-cli/server/db.py` (`insert_bid`, new `update_bid_grades`, `cache_gixen_data`)
- Modify: `packages/gixen-cli/server/main.py` (`AddBidRequest`, `_add_bid_row`, `api_add_bid`)
- Modify: `packages/gixen-cli/cli.py` (`add` options + payload)
- Test: `packages/gixen-cli/tests/test_server_db.py`, `packages/gixen-cli/tests/test_server_api.py` (or the existing API test module)

**Approach:**
- `insert_bid`: add keyword-only `seller_grade=None`, `photo_grade=None` params + the two
  columns to the INSERT list (`seller` already present). Existing positional call sites unaffected.
- `AddBidRequest`: add `seller: str | None = None`, `seller_grade: float | None = None`,
  `photo_grade: float | None = None`. (Recall `extra="ignore"` — undeclared fields are dropped.)
- `_add_bid_row`: accept + forward `seller`/grades; replace the hardcoded `seller=None`.
  `api_add_bid`: pass `req.seller` (lowercased) + grades into `_add_bid_row`.
- **C2 fill-NULL:** add `update_bid_grades(conn, item_id, seller, seller_grade, photo_grade)`
  that sets each column via `COALESCE(existing, ?)`-style logic so it only fills NULLs.
  Call it on the existing-PENDING-snipe path (after `_modify_and_update_bid`) and on the
  `_add_bid_row` `IntegrityError` recovery branch.
- **A1:** change `cache_gixen_data`'s `seller=COALESCE(?, seller)` → `COALESCE(seller, ?)`.
- `cli.py add`: add `--seller-grade` / `--photo-grade` float options; lowercase the
  resolved `seller` and add `seller`/grades to the POST payload (omit unset).

**Patterns to follow:** `cache_gixen_data` COALESCE idiom; `upsert_fmv`'s COALESCE-preserve
(stub-fmv learning) for the fill-NULL semantics; existing click option declarations in `add`.

**Test scenarios:**
- Happy path: `POST /api/bids` with seller + both grades persists all three on the bids row.
- Null-safe: add with no grades stores `seller_grade`/`photo_grade` NULL, no error.
- C2 fill-NULL: add without grades → re-add same item with grades fills the NULLs; a re-add
  with *different* grades does **not** overwrite already-set grades.
- A1 no-overwrite: a `cache_gixen_data` cycle after a seller-bearing INSERT leaves `seller`
  unchanged; a row that started NULL still gets filled by sync.
- Edge/integration: `_sync_gixen` insert path still works (relies on the new `None` defaults);
  `extra="ignore"` regression — assert the values actually land in the DB, not just `POST` 200.
- Case-normalization: a mixed-case username is stored lowercased.

**Verification:** a buy-flow add records seller (lowercased) + both grades; re-runs complete
NULLs without clobbering; sync never rewrites a set seller.

---

- [ ] **Unit 3: `GET /api/seller-reliability` endpoint**

**Goal:** Return a seller's average grade deviation and sample size.

**Requirements:** R2

**Dependencies:** Unit 1 (columns). (Independent of Unit 2, but tests seed rows directly.)

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/routes.py`
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- `GET /api/seller-reliability?seller=<name>` (param **required**). Validate non-empty and
  ≤128 chars else 422; bind as `WHERE b.seller = ?` (lowercase the input to match stored keys).
- Aggregate over rows where `seller_grade IS NOT NULL AND photo_grade IS NOT NULL AND
  status NOT IN ('PURGED','REMOVED')`: `avg_deviation = AVG(seller_grade - photo_grade)`
  (positive = over-grades), `sample_size = COUNT(*)`.
- Unknown/zero-sample → `{"seller": <name>, "avg_deviation": null, "sample_size": 0}`, HTTP 200.
- Does **not** call `_ensure_fresh_sync` (justified — local historical data).

**Patterns to follow:** existing routes' `db = request.app.state.db`, `?`-placeholder binding,
inline 422 validation, and the `status NOT IN ('PURGED','REMOVED')` filter (BUI-50 learning).

**Test scenarios:**
- Happy path: seed 3 dual-graded rows for a seller → correct signed `avg_deviation` and `sample_size`.
- Edge case: rows missing either grade are excluded; tombstoned (`PURGED`/`REMOVED`) rows excluded.
- Edge case: unknown / zero-sample seller → `sample_size: 0`, `avg_deviation: null`, 200.
- Error path: missing `seller` → 422; >128-char `seller` → 422.
- Security: an injection-y `seller` string is treated as a literal (parameterized) and returns zero-sample.
- Edge case: case-insensitive match — stored `beatlebluecat` is found by query `BeatleBluecat`.

**Verification:** the endpoint returns correct per-seller deviation, excludes the right rows,
and validates input.

---

- [ ] **Unit 4: Wire `/comic:buy` (capture grades + Step 1 advisory)**

**Goal:** Capture both grades through the buy flow and surface the seller advisory.

**Requirements:** R3, R6 (D2)

**Dependencies:** Unit 2 (CLI flags), Unit 3 (endpoint).

**Files:**
- Modify: `.claude/commands/comic/identify.md` (surface lowercased seller username + stated grade)
- Modify: `.claude/commands/comic/grade.md` / `.claude/commands/comic/buy.md` (Step 2.5 opt-in
  "grade anyway" for stated-grade listings; preserve the raw consensus as its own field)
- Modify: `.claude/commands/comic/snipe-add.md` (add `--seller-grade`/`--photo-grade` to the
  canonical "available flags" table)
- Modify: `.claude/commands/comic/buy.md` (Step 1 advisory read; pass grades + seller at Step 5)

**Approach:**
- identify.md: add `seller` (lowercased username, from `parse_item`) to the output so
  downstream steps have it; surface the stated grade label.
- buy.md Step 1: after identify, `GET /api/seller-reliability?seller=<username>`; on
  `sample_size ≥ 1` show the advisory (early-signal label for n=1–2); best-effort —
  a non-200/connection error is treated as zero-sample (never blocks the flow).
- buy.md Step 2.5: offer an opt-in "grade anyway (to record seller deviation)" for
  stated-grade listings; keep the **raw** photo consensus as a distinct working-list field
  so a user override at the gate (which flows to `--grade`/FMV) doesn't clobber `photo_grade`.
- buy.md Step 5 / snipe-add.md: pass `--seller-grade`, `--photo-grade` (omit if absent), and
  the lowercased `seller`. Convert the seller's label grade to a CGC float via a small inline
  static table (see deferred note); unmappable → omit `--seller-grade`.

**Execution note:** these are prose skill files (no unit tests). Verify by a dry walk-through
of `/comic:buy` against a sample listing and by confirming the `gixen add` invocation includes
the new flags.

**Patterns to follow:** existing identify.md field/output tables; snipe-add.md "available flags
(canonical)" table; buy.md Step 2.5 gate prose.

**Test scenarios:** Test expectation: none — prose orchestration skills with no automated suite.
Manual verification: a `/comic:buy` dry run surfaces the advisory at Step 1 and the Step 5
`gixen add` command carries `--seller-grade`/`--photo-grade` + the lowercased seller.

**Verification:** running `/comic:buy` on a known over-grader shows the early-signal advisory,
and an added snipe records both grades + the username in `bids`.

## System-Wide Impact

- **Interaction graph:** `cli.py add` → `POST /api/bids` (`api_add_bid` → `_add_bid_row`/
  `update_bid_grades`) writes grades; `_sync_gixen` → `cache_gixen_data` (A1 COALESCE) keeps
  seller; overlay `GET /api/seller-reliability` reads. `/comic:buy` Steps 1/2.5/5 orchestrate.
- **Error propagation:** the Step 1 advisory GET is best-effort (zero-sample on failure, never
  blocks bidding). `POST /api/bids` validation errors surface as today.
- **State lifecycle risks:** A1 changes which seller value "wins" on sync — verify web-added
  snipes still populate (deferred-to-impl check). Fill-NULL must never overwrite a set grade.
- **API surface parity:** the new endpoint reuses the `status NOT IN ('PURGED','REMOVED')`
  filter (BUI-50). No other endpoint needs the new columns yet.
- **Integration coverage:** Unit 2's tests must prove the POST→insert_bid→DB write end-to-end
  (not just model acceptance) given `extra="ignore"`.
- **Unchanged invariants:** `bids.fmv_id`/`bid_fmvs`/`link-fmv` linkage, the FMV `--grade`
  path, and the dashboard endpoints are untouched. `update_bid` (max-bid edits) keeps its
  current behavior; grade fill is a separate helper.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A1 COALESCE flip regresses the dashboard seller column for web-added snipes | Deferred-to-impl verification; web-added rows start NULL so first sync still fills them; covered by an A1 no-overwrite + NULL-fill test |
| `extra="ignore"` silently drops new fields if `AddBidRequest` isn't updated | Test asserts values land in the DB, not just 200; declare fields explicitly |
| Future `bids` rebuild drops the new columns | `_BIDS_TABLE_SQL` updated in the same unit + rebuild-survival test (Unit 1) |
| Seller stored as store-name (not username) splits history | A1 pins the lowercased username at INSERT from `parse_item`; advisory queries the same key |
| Dual-graded population stays near-empty (over-graders state grades → grading skipped) | Step 2.5 opt-in "grade anyway"; D2 floor of n≥1 with early-signal label makes the signal usable sooner |

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-03-bui-78-seller-photo-grades-requirements.md](docs/brainstorms/2026-06-03-bui-78-seller-photo-grades-requirements.md)
- Related learnings: `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`,
  `docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md`,
  `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md`,
  `docs/solutions/database-issues/stub-fmv-null-after-extract-comics-2026-05-23.md`
- Related code: `packages/gixen-cli/server/db.py`, `packages/gixen-cli/server/main.py`,
  `packages/gixen-cli/cli.py`, `plugins/gixen-overlay/src/gixen_overlay/routes.py`,
  `apps/ebay/src/ebay_fetch.py`
