---
title: "feat: Capital-Commitment Layer at Order Entry"
type: feat
status: active
date: 2026-08-02
origin: docs/brainstorms/2026-08-02-capital-commitment-layer-requirements.md
---

# feat: Capital-Commitment Layer at Order Entry

## Summary

Add a pre-trade policy layer to the comics server: every `max_bid` write through `POST /api/bids` and `PATCH /api/bids/{item_id}` runs a set of advisory checks (aggregate exposure, over-FMV, recomputed cap, staleness, unpriced entry, duplicate comic outside a bid group) and appends an immutable decisions-ledger row. v1 blocks nothing; blocking is a later per-check config flip with an explicit, audited bypass. Implements BUI-609 from the origin requirements doc.

---

## Problem Frame

`gixen add`'s only validation on `max_bid` is a Decimal parse; FMV linking happens after the add and fails silently; `fmv.confidence`/`fmv.comps` are never consulted at bid time. Policy lives in skill prose, which this repo has already learned drifts (BUI-168). The full framing is in the origin doc (see origin: `docs/brainstorms/2026-08-02-capital-commitment-layer-requirements.md`).

---

## Requirements

Origin R1–R18 carry forward unchanged except where research forced a refinement. Refinements:

- R1 (scope of "every max_bid write") — covered paths: `api_add_bid` create + BUI-67 upsert-modify, `api_edit_bid` (which also serves both dashboard edit surfaces), and every `add-batch` row (they route through `api_add_bid`). Out of trigger scope but **counted in exposure sums**: the lock-free `_sync_loop` paths that mirror Gixen-authoritative state (`insert_bid` for web-added snipes at `server/main.py:686`, `mirror_gixen_max_bid`) — they mirror intent already committed on gixen.com, they don't originate it. Out entirely: the startup dedup merge, group-only changes (`gixen group`), direct-Gixen mode (origin R18).
- R16 (advisories at the approval point) — v1 renders advisories at **commit time** in the write response; there is no dry-run/preview call. The skills surface them immediately after the add with remediation guidance (`gixen edit`/`remove`). A preview endpoint is deferred (see Scope Boundaries).
- R5 (unpriced entry) — overlay-owned like all FMV-aware checks; a standalone gixen-cli server without the overlay has only the exposure check, and R5 does not fire (see KTD1).

---

## Key Technical Decisions

- **KTD1 — Host orchestrates; overlay contributes FMV-aware checks via new request-time pluggy hooks.** The check point lives in the host's two write handlers (inside the existing `_api_lock` acquisition). Core check (exposure) is host-owned — it reads only `bids`. FMV-aware checks (over-FMV, recomputed cap, staleness, unpriced, duplicate-comic) are overlay-owned, contributed through two new `GixenPluginSpec` hookspecs — the first request-time hooks (the existing three fire once in lifespan): `check_bid_write` (pre-write, read-only, returns advisories) and `on_bid_write_committed` (post-write notification, lets the overlay persist the FMV link resolved during checks — the host cannot import overlay code to do it). The invoker follows the `_collect_dashboard_tabs` defensive pattern: a hook exception becomes an `unevaluable` outcome plus a loud log, never an exception into the money path. This is the sanctioned coupling direction (host defines hookspec, overlay implements) and reduces the documented reach-into-host-internals smell rather than adding to it.
- **KTD2 — Thresholds are env vars read per request, not at import; unset ceiling = check disabled.** `POLICY_FMV_MULTIPLE` (default 1.0), `POLICY_FMV_STALE_DAYS` (default 30), `POLICY_EXPOSURE_CEILING` (no default — unset disables the exposure check), later `POLICY_BLOCK_*` flags. Read inside the request (avoiding the `GIXEN_SYNC_INTERVAL` import-time trap) so an operator edits `~/.comics-server/.env` + `launchctl kickstart` — no deploy. No config table: the repo has no precedent for one and a restart is acceptable. Every ledger row records the values consulted, so mid-batch config changes are self-describing.
- **KTD3 — Ledger is a host-owned append-only table modeled on `group_wins`.** `bid_decisions` anchors on `bids.id` via a **nullable** `bid_row_id` FK (never `item_id` alone — non-unique by design): NULL for outcomes evaluated before any bid row exists (a blocked create, a Gixen-failed create), where the denormalized `item_id` plus `trigger` is the anchor. It carries a closed-vocabulary `outcome` (`committed` / `unconfirmed` / `gixen_failed` / `blocked`) enforced at the write boundary, a bypass flag, the config snapshot, and the advisory/check results as JSON (keeps the host comic-agnostic). One row per check-point evaluation regardless of the Gixen result — the maybe-money-moved cases (`applied: False`, reconcile-503) get rows too. Written in its own `write_transaction` after the bid write, own try/except, loud log on failure; the crash window between bid commit and ledger append is accepted (folding it into the bid transaction would let a ledger failure abort the bid, violating origin R13). Retention: rows are kept indefinitely, mirroring `group_wins` (append-only, never swept by purge); revisit only if the soak shows unexpected volume.
- **KTD4 — One advisory envelope everywhere.** Every 2xx write response (all POST branches including `applied: False`, both PATCH branches) gains an `advisories: [...]` key: `{code, severity, message, data}` per advisory. Blocking mode (U9) returns 409 with the same structure inside `detail`. Old clients ignore the extra key; `AddBidRequest`'s `extra: "ignore"` gives the same skew tolerance in the other direction.
- **KTD5 — Rung constants are duplicated into the overlay with a source-parsing canary.** The recomputed-cap check needs `bid_factor`'s ladder (0.80 / 0.70 / 0.60), but `apps/fmv` is outside the workspace — no import edge exists or should be created. Following the documented HTTP-only-contract pattern (BUI-588/593), the overlay carries its own constants and a canary test parses `apps/fmv/src/fmv_math.py` source, asserts the extraction found the values (guarded — proven able to fail), and compares. An in-repo precedent exists: `plugins/gixen-overlay/tests/test_skill_contracts.py` already reads `apps/fmv` source via `REPO_ROOT`. Extraction into a shared package was rejected: `apps/*` stay non-workspace by design.
- **KTD6 — Every check returns a tri-state** (`pass` / `advise` / `unevaluable`), and `unevaluable` is visible in the ledger row. "Check errored" must never collapse into "check found nothing" (the Metron-breaker learning).
- **KTD7 — Duplicate-comic matches on `comics.id` across grades**, names the existing item, group, and both grades in the advisory, and exempts same-`snipe_group` siblings (BUI-363 sanctioned pattern). Advisory-only makes deliberate cross-grade collecting tolerable noise; the message text lets the operator judge.
- **KTD8 — Lot semantics.** `bid_fmvs` supports multiple links per bid (`is_primary=False` secondaries; lots share one `gixen_item_id` across issues, BUI-500). FMV checks always compare `max_bid` against the **sum of resolved FMV highs**: on PATCH, the sum over existing links; on POST, the sum over the identities supplied in the payload — the payload carries a list (single-element in the common case) so a lot add is not compared against one issue's high and false-advised. The ledger row records the identity/link count. Residual: a caller that supplies one identity for a genuine multi-book lot can still draw a false over-FMV advisory; that gap closes only when briefs decompose lots (noted in Risks).
- **KTD9 — Rung-demotion inputs (comps n, range widths) are measurement-gated.** Four pool-shape signals were falsified on measurement (BUI-578/582/592/590; precision bar ≥0.80). U8 measures candidate demotion thresholds against historical `fmv`+`bids` data first; only clearing signals get implemented. A failed measurement is a recorded falsification, not a soft launch.

---

## High-Level Technical Design

```mermaid
sequenceDiagram
    participant C as gixen CLI / add-batch / dashboard
    participant H as api_add_bid / api_edit_bid (host, under _api_lock)
    participant P as policy check point (server/policy.py)
    participant O as overlay hook (FMV-aware checks)
    participant G as Gixen
    participant L as bid_decisions ledger

    C->>H: max_bid write (+ optional comic_id/grade)
    H->>P: run checks (config snapshot from env)
    P->>P: exposure check (bids only)
    P->>O: check_bid_write hook (if overlay registered)
    O-->>P: advisories / unevaluable (never raises)
    P-->>H: advisories (v1: never blocks)
    H->>G: add / modify
    G-->>H: result (ok / unconfirmed / error)
    H->>O: on_bid_write_committed (overlay persists FMV link; never raises)
    H->>L: append decision row (own transaction, failure logged, never blocks)
    H-->>C: 2xx response + advisories[]
```

Checks, owners, and inputs:

| Check | Owner | Inputs | Config | Fires on |
|---|---|---|---|---|
| Exposure ceiling | host | PENDING `bids` (group-aware projection) | `POLICY_EXPOSURE_CEILING` | POST + PATCH |
| Over-FMV (N×high) | overlay | linked/supplied FMV row(s) | `POLICY_FMV_MULTIPLE` | POST + PATCH |
| Recomputed cap | overlay | FMV row + rung constants | — | POST + PATCH |
| Staleness | overlay | `fmv.updated_at` | `POLICY_FMV_STALE_DAYS` | POST + PATCH |
| Unpriced entry | overlay | identity resolution result | — | POST + PATCH |
| Duplicate comic | overlay | `bid_fmvs`+`bids` live snipes | — | POST |

---

## Implementation Units

Units are ordered by dependency, not by U-ID — U-IDs are stable identifiers, not a reading order (U6 sits in Phase A because it depends only on U1; U5 precedes U4 because U4 consumes its payload fields).

### Phase A — host core

### U1. Check-point scaffolding and advisory envelope

- **Goal:** One policy check point called from both write handlers, with a shared predicate home, per-request config snapshot, tri-state outcomes, and the `advisories` response key on every 2xx branch.
- **Requirements:** R1, R8, R14, R16 refinement; KTD2, KTD4, KTD6.
- **Dependencies:** none.
- **Files:** `packages/gixen-cli/server/policy.py` (new), `packages/gixen-cli/server/main.py` (`api_add_bid`, `api_edit_bid`), `packages/gixen-cli/tests/test_server_policy.py` (new), `packages/gixen-cli/tests/test_server_api.py`.
- **Approach:** `policy.py` owns the advisory shape, config snapshot (env read per request), and `run_checks(conn, intent, pm)` returning `(advisories, check_results)` — handlers pass `app.state.plugin_manager` (None-tolerant), matching how the lifespan threads `pm` into `_collect_dashboard_tabs`; importing `server.main` from `policy.py` would be circular. Both handlers call it inside their existing `_api_lock` acquisition, before the Gixen call — one helper, two call sites (the guard-strictness learning: duplicated predicates drift). Checks read from the shared handler connection, the same snapshot the handlers already read. `intent` captures item_id, target max_bid, snipe_group, trigger kind (create/upsert/edit/batch-row), prior row if any, and optional comic identities. Checks are gather-phase reads only — no writes, per the write-transaction convention. Config parsing per KTD6: an **unset** var disables its check (absent from results); a **malformed** value produces an `unevaluable` result carrying the raw string in the ledger config snapshot, plus one loud log — a config typo must never present as "check found nothing".
- **Patterns to follow:** `_collect_dashboard_tabs` defensive invocation; `_serialize_snipe_row` as the single-source-response precedent; BUI-573 test-fixture env flags.
- **Test scenarios:**
  - Happy path: POST create with no advisories → response carries `advisories: []`; PATCH likewise.
  - Covers AE6. Upsert-modify of a live item runs checks against the new amount (assert via a forced advisory).
  - `applied: False` branch and the GixenSnipeNotFoundError fallback branch still carry the envelope.
  - Config: env values read per request (change env between two requests in one TestClient session → second request sees new value); malformed env value → `unevaluable` result with the raw string recorded, one loud log, not a crash and not a silent disable; unset → check absent.
  - Advisory present → response still 2xx and the bid row is written (v1 never blocks).
- **Verification:** both handlers emit the envelope on every 2xx branch; no check can raise into the handler (exception in a check function → `unevaluable` result, bid proceeds).

### U2. Group-aware exposure check

- **Goal:** Advisory when a write's projected aggregate PENDING exposure exceeds the configured ceiling.
- **Requirements:** R7; KTD2.
- **Dependencies:** U1.
- **Files:** `packages/gixen-cli/server/policy.py`, `packages/gixen-cli/tests/test_server_policy.py`.
- **Approach:** Projection = sum of ungrouped PENDING `max_bid` + per-group `MAX(max_bid)`, with the target row's previous contribution replaced by the new value (create adds; upsert/edit replaces). `status='PENDING'` filter only (tombstone parity by construction). Unset `POLICY_EXPOSURE_CEILING` disables the check. PATCH fallback on a not-yet-ingested row treats the prior contribution as 0 and marks the ledger row `no_prior_row`. Web-added and mirrored rows count in the sum even though they never triggered checks (R1 refinement).
- **Execution note:** implement the projection function test-first — the group math is the bug surface.
- **Test scenarios:**
  - Covers AE3. Ungrouped $100 + $50 plus a group of $200/$180 → projection $350.
  - In-group edit raising a non-max member to below the group max → projection unchanged, no advisory.
  - Upsert raising a live item's bid → old value replaced, not double-counted.
  - Create that crosses the ceiling → advisory with projected total and ceiling in `data`.
  - Ceiling unset → check absent from results (disabled, not `unevaluable`).
  - Ceiling set to a malformed value → `unevaluable` result, no silent disable.
  - Seeded web-added PENDING row (no ledger history) inflates the projection.
- **Verification:** projection math matches the acceptance examples; exposure query hits only PENDING rows.

### U3. Request-time policy hookspecs

- **Goal:** Two new `GixenPluginSpec` hooks — pre-write checks and post-write notification — with per-plugin error isolation.
- **Requirements:** R1, R17; KTD1, KTD6.
- **Dependencies:** U1.
- **Files:** `packages/gixen-cli/gixen/plugins.py`, `packages/gixen-cli/server/policy.py`, `packages/gixen-cli/server/main.py` (post-commit invocation), `plugins/gixen-overlay/src/gixen_overlay/plugin.py`, `packages/gixen-cli/tests/test_server_policy.py`, `plugins/gixen-overlay/tests/test_workspace_imports.py`.
- **Approach:** `@hookspec check_bid_write(conn, intent) -> list[dict]` (read-only, returns advisories/check results) and `@hookspec on_bid_write_committed(conn, intent, bid_row_id, check_results)` (fired by the host after a successful bid write, same request — the overlay uses it in U4 to persist the FMV link its check phase resolved; the persist is not a check, so U4's read-only invariant stands). Both synchronous (handlers already do same-thread DB reads). `policy.py` invokes via the passed `pm` with a defensive wrapper: per-plugin try/except; a check-hook exception → `unevaluable` result + loud log; a post-commit-hook exception → loud log only, the write is already committed. Overlay's `plugin.py` gains both `@hookimpl`s as stubs (return `[]` / no-op) so U3 lands green independently; U4 fills them in. Extend the workspace-imports canary for any new overlay→host import.
- **Patterns to follow:** `_invoke_db_tables_isolated` / `_collect_dashboard_tabs` isolation helpers; `make_plugin_manager()` + `_install_plugins` test seams.
- **Test scenarios:**
  - Fake plugin returning one advisory → advisory lands in the envelope and check results.
  - Fake check hook raising → bid commits, `unevaluable` result recorded, log emitted.
  - Fake post-commit hook raising → bid commits, response 2xx, log emitted.
  - No plugin registered → only core checks run; no `unevaluable` noise.
- **Verification:** `pm.check_pending()` passes (hookspec/hookimpl names match); no hook crash can 5xx a write.

### U6. Decisions ledger and audit read

- **Goal:** Append-only `bid_decisions` table written for every check-point evaluation, plus a read endpoint for audits.
- **Requirements:** R11, R12, R13; KTD3.
- **Dependencies:** U1.
- **Files:** `packages/gixen-cli/server/db.py` (schema + append/read helpers), `packages/gixen-cli/server/main.py` (append call sites, `GET /api/decisions`), `packages/gixen-cli/tests/test_server_db.py`, `packages/gixen-cli/tests/test_server_api.py`.
- **Approach:** Columns: autoinc id, `bid_row_id` (**nullable** FK `bids(id)` — NULL when no bid row exists at evaluation: blocked create, Gixen-failed create; `item_id` + `trigger` anchor those), `item_id` (denormalized for lookups), `evaluated_at`, `trigger`, `outcome` (frozenset-checked closed vocabulary: `committed` / `unconfirmed` / `gixen_failed` / `blocked`), `bypass` (0/1), `requested_max_bid`, `config_json` (values consulted: multiple, stale days, ceiling, blocking flags — raw string preserved when malformed), `checks_json` (per-check tri-state + data), `advisories_json`, `source`. This unit also adds the optional `source` field (cli/batch/dashboard) to `AddBidRequest`/`EditBidRequest`; U7 populates it from the CLI and add-batch. Append after the bid write in its own `write_transaction` via `_get_db_path()` (never the module-level `DB_PATH`), own try/except, loud log on failure. Rows for the unconfirmed/`applied: False` and Gixen-503 paths record those outcomes — the check ran, money maybe moved. `GET /api/decisions?item_id=&limit=` returns newest-first rows.
- **Patterns to follow:** `group_wins` (append-only, closed vocabulary, provenance tagging, `INSERT OR IGNORE` idempotency where retried); the per-bid `write_transaction` isolation precedent in `_sniper_loop`; row-id-scoping learning (seed two rows sharing an item_id in tests).
- **Test scenarios:**
  - Covers AE5. Ledger table locked / append raises → write response still 2xx, bid row present, loud log.
  - Gixen 503 on add → ledger row with `outcome=gixen_failed`; unconfirmed modify → `unconfirmed`.
  - Gixen-failed **create** (no bid row ever inserted) → ledger row with `bid_row_id IS NULL` and the requested item_id.
  - Two bids rows sharing one item_id → ledger row anchors to the correct row id.
  - Closed vocabulary enforced: unknown outcome value raises at the write boundary (in tests), never persists.
  - `GET /api/decisions?item_id=` returns the rows newest-first with parsed JSON fields.
- **Verification:** no UPDATE path exists on the table; every write-handler exit path (success, unconfirmed, Gixen-fail) appends exactly one row.

### Phase B — comic-aware checks and surfaces

### U5. Comic identity at add time

- **Goal:** Comic identity travels in the add payload so FMV checks run pre-trade; the overlay persists the link post-commit so later duplicate checks can see it.
- **Requirements:** R17; KTD8 (POST arm).
- **Dependencies:** U1, U3.
- **Files:** `packages/gixen-cli/add_batch.py` (`build_bid_payload`, `add_one_row`), `packages/gixen-cli/cli.py` (`add`), `packages/gixen-cli/server/main.py` (`AddBidRequest`), `plugins/gixen-overlay/src/gixen_overlay/db.py` (link idempotency if needed), `packages/gixen-cli/tests/test_add_batch.py`, `packages/gixen-cli/tests/test_server_api.py`, `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`.
- **Approach:** `build_bid_payload` gains optional identity kwargs mapped to a payload list of `{comic_id | locg_id, grade}` entries — single-element in the common case, list-capable for lots (KTD8), and covering both identity forms the link path already accepts (`--comic-id` and `--catalog-id`/`locg_id`, so a catalog-id add doesn't false-fire the unpriced advisory). One shared payload builder — `add` and `add-batch` can't drift (BUI-360); `add_one_row` threads the row fields it already carries. `AddBidRequest` gains the optional field. Link persistence happens in the overlay's `on_bid_write_committed` hookimpl (U3/U4), using the FMV row(s) resolved during checks. The CLI's post-add `link-fmv` call stays (old-server compatibility); linking must be idempotent when both run. `EditBidRequest` unchanged — PATCH resolves identity from existing links.
- **Test scenarios:**
  - New CLI → old server: extra payload fields ignored (`extra: "ignore"`), add succeeds.
  - Old CLI → new server: no identity in payload → unpriced-entry advisory fires only when the overlay is registered.
  - Identity present and resolvable → `bid_fmvs` link exists after the add (written by the post-commit hook), before any `link-fmv` call.
  - Add-time link then CLI `link-fmv` call → one link, no duplicate/error.
  - `locg_id`-form identity resolves and links, and does not draw an unpriced advisory.
- **Verification:** `/comic:verify`'s linkage chain passes for a book added with payload identity and no explicit link call.

### U4. Overlay FMV-aware checks

- **Goal:** Over-FMV, recomputed-cap, staleness, unpriced-entry, and duplicate-comic checks behind the U3 hook.
- **Requirements:** R2, R3, R4, R5, R6; KTD5, KTD7, KTD8.
- **Dependencies:** U3, U5.
- **Files:** `plugins/gixen-overlay/src/gixen_overlay/policy.py` (new), `plugins/gixen-overlay/src/gixen_overlay/plugin.py`, `plugins/gixen-overlay/tests/test_policy_checks.py` (new), `plugins/gixen-overlay/tests/test_fmv_rung_parity.py` (new canary).
- **Approach:** Resolution: POST with identity list → reuse `_resolve_fmv_for_link` per entry (its attempted-strategies list feeds the unpriced advisory's `data`); PATCH → `get_primary_fmv_for_bid` + all links. Both arms sum resolved highs per KTD8. Over-FMV compares against `N × summed high`; recomputed cap applies the duplicated rung constants to the stored `confidence` (grade confidence isn't stored on the fmv row — the recompute uses the fmv-side rungs and says so in the advisory; exact parity with the brief's haircut is U8 territory). Staleness compares `updated_at` age to K. Duplicate-comic: live PENDING snipes on other item_ids linked to the same `comics.id`, exempting same-`snipe_group`, tombstone-filtered via the shared constant. This unit also fills in the `on_bid_write_committed` hookimpl: persist the resolved link(s) via `link_fmv_to_bid`. Rung canary per KTD5: parse `apps/fmv/src/fmv_math.py`, assert extraction non-empty, compare constants (precedent: `test_skill_contracts.py` already reads that file via `REPO_ROOT`).
- **Execution note:** implement check predicates test-first; each is a pure function over rows.
- **Test scenarios:**
  - Covers AE1. Second copy of comic X added into the same group → no duplicate advisory.
  - Covers AE2. Second copy ungrouped → duplicate advisory naming item A and group 2.
  - Covers AE4. Identity supplied, FMV resolution fails → snipe commits, unpriced advisory, ledger records absence + strategies attempted.
  - Over-FMV: `max_bid` $150 vs high $100, N=1.0 → advisory with ratio; N=2.0 via env → no advisory.
  - Staleness: `updated_at` 45 days old, K=30 → advisory naming age; fresh row → none.
  - Recomputed cap: low-confidence row → cap at 0.60×high; bid above it → advisory.
  - PATCH on multi-linked lot → comparison against sum of highs; ledger row notes link count.
  - POST with a two-identity lot payload → comparison against the sum of both highs, not one issue's high.
  - Tombstoned sibling on the same comic → no duplicate advisory (REMOVED filtered).
  - Canary: rung values in `fmv_math.py` change → parity test fails; extraction regex matching nothing → test fails loudly (guarded).
- **Verification:** all five checks return tri-state results; no check writes to the DB.

### U7. CLI, add-batch, and skill advisory rendering

- **Goal:** Advisories visible at every approval surface; skill prose updated to the rung formula.
- **Requirements:** R9, R16 refinement.
- **Dependencies:** U1 (envelope), U4 (real advisories to render).
- **Files:** `packages/gixen-cli/cli.py` (`add`, `edit`), `packages/gixen-cli/add_batch.py` (row results + summary), `packages/gixen-cli/tests/test_cli_add_batch.py`, `packages/gixen-cli/tests/test_add_batch.py`, `.claude/commands/comic/snipe-add.md`, `.claude/commands/comic/buy.md`.
- **Approach:** `gixen add`/`edit` print each advisory (code + message) to stderr after the success line — visible, non-fatal, and both populate the `source` payload field U6 added (`cli` / `batch`). `add-batch` threads `advisories` into each row result, renders a per-row marker in the human table and full advisories in the JSON summary; the end-of-run summary counts advisories so a batch can't end looking clean when rows carried challenges. Skill edits: snipe-add.md's flat "80% × top of FMV range" fallback becomes the rung formula (0.80 base; 0.70/0.60 per the confidence haircut that comic-fmv already applies), plus an advisory-handling section: surface every advisory from the add output at the gate before declaring the batch done, with remediation options (`gixen edit`, `gixen remove`, acknowledge). buy.md Step 5 points at that section. Check the BUI-173 skill-contract tests for pinned anchors before editing.
- **Test scenarios:**
  - Row with advisories → human table marks it and JSON summary carries them verbatim.
  - All-clean batch → no advisory noise in output.
  - Advisory-carrying batch → summary line includes advisory count (never an all-clean summary).
  - `gixen add` direct-mode (no server) → no advisory rendering path errors.
- **Verification:** an out-of-policy add is visibly challenged at every surface a human approves from.

### Phase C — measurement-gated discount and blocking

### U8. Rung-demotion measurement gate

- **Goal:** Measure candidate demotion inputs (comps n, FMV range width, grade range width) against historical data; implement only signals clearing the precision bar.
- **Requirements:** R10; KTD9.
- **Dependencies:** U4 (rung recompute exists to extend).
- **Files:** `packages/gixen-cli/scripts/` or `apps/fmv/scripts/` (measurement script, location per data access), then `apps/fmv/src/fmv_math.py` + `plugins/gixen-overlay/src/gixen_overlay/policy.py` + the parity canary for any clearing signal.
- **Approach:** For each candidate (e.g. `comps < n₀`, `(high-low)/high > w₀`, grade range width > g₀): over historical `fmv` rows joined to resolved bids, would the demotion have fired, and was the outcome consistent with genuine overpricing risk (won near/above fmv_high, calibration-report exceedance)? Precision ≥0.80 to ship, mirroring the BUI-578→594 bar. Clearing signals land as rung demotions in `bid_factor` (brief path) and the overlay recompute (check path) together, with the canary updated. Non-clearing candidates are recorded as falsifications in the Linear issue.
- **Test scenarios (for any shipped demotion):** threshold boundary cases in both `fmv_math` and overlay policy tests; parity canary covers the new constants. Measurement script itself: `Test expectation: none — one-shot diagnostic, results recorded in the issue.`
- **Verification:** no demotion ships without a recorded measurement; brief-path and check-path rungs stay in lockstep via the canary.

### U9. Blocking mode with audited bypass

- **Goal:** Per-check opt-in blocking; explicit bypass; batch semantics that distinguish policy blocks from failures.
- **Requirements:** R14, R15; KTD4.
- **Dependencies:** U1, U4, U6, U7.
- **Files:** `packages/gixen-cli/server/policy.py`, `packages/gixen-cli/server/main.py`, `packages/gixen-cli/cli.py` (bypass flag), `packages/gixen-cli/add_batch.py` (row `policy_bypass`, `BLOCKED` status), `packages/gixen-cli/server/static/index.html` + `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` (minimal 409 alert), tests across `test_server_policy.py`, `test_add_batch.py`, `test_cli_add_batch.py`.
- **Approach:** `POLICY_BLOCK_<CHECK>` env flags, default off. A blocking check without bypass → 409, structured advisories in `detail`, ledger `outcome=blocked`, no Gixen call. An `unevaluable` result **never blocks** (fail-closed is reserved for a check that affirmatively fired, per the guard-strictness learning) — but when its `POLICY_BLOCK_*` flag is set, the response and ledger row carry an explicit `unevaluable_while_blocking` marker so a blocking check that has gone blind is visible rather than silently permissive. When the blocked write was an upsert-modify or edit of a live row, the 409 detail names the surviving live snipe and its current max_bid ("existing snipe at $100 remains active") — a BLOCKED batch row must not read as "no money committed" when a prior commitment survives. Bypass: CLI flag (e.g. `--ack-policy`) → payload field `policy_bypass: true` → snipe commits, ledger `bypass=1`; the bypass is per-invocation and blanket, and the ledger's full advisory set records exactly what was acknowledged. add-batch: a 409 marks the row `BLOCKED` (distinct from `FAILED`) and **continues** — a policy block is not a server fault, so BUI-168's halt semantics don't apply; summary reports added/blocked/failed/remaining. Dashboard PATCH renders the 409 detail in a plain alert. Ships only after an advisory soak (see Operational Notes).
- **Test scenarios:**
  - Blocking flag on, advisory fires, no bypass → 409, no Gixen call, no bid row change, ledger `blocked`.
  - Same with `policy_bypass` → commits, ledger `bypass=1`.
  - Blocked upsert of a live $100 snipe → 409 detail names the surviving snipe and its max_bid.
  - Blocking flag on, check `unevaluable` → write proceeds, `unevaluable_while_blocking` marker in response and ledger.
  - add-batch: blocked row → `BLOCKED`, batch continues, health check not tripped, summary never all-clean.
  - Blocking flag off (default) → identical behavior to v1 (regression guard).
- **Verification:** no silent override path exists; every block and bypass is a ledger row.

---

## Acceptance Examples

Origin AE1–AE6 carry forward verbatim and are mapped to units above (`Covers AE<N>` prefixes). Plan-added:

- AE7. **Covers R7 (edit projection).** Given a group holding $200 and $180 snipes, when the $180 member is edited to $190, projected exposure is unchanged ($200 still the group max) and no exposure advisory fires.
- AE8. **Covers KTD1/KTD6.** Given the overlay hook raises on a write, the snipe commits, the response carries no fabricated advisories, and the ledger row marks the FMV check class `unevaluable`.
- AE9. **Covers U9.** Given blocking mode on the over-FMV check, an add-batch with one over-FMV row and two clean rows ends with 2 added + 1 `BLOCKED`, and the batch does not halt.
- AE10. **Covers R1 refinement.** Given a snipe added on gixen.com's own UI and ingested by the sync loop, it appears in exposure projections but has no ledger row and triggered no checks.

---

## Scope Boundaries

Carried from origin: WON-inference and status classification untouched; no silent bid rewriting; no new FMV-quality pool-shape advisory signals; direct-Gixen mode unenforced; portfolio analytics out (survivor 6); nothing enforced on Gixen's side.

### Deferred to Follow-Up Work

- Dry-run/preview endpoint so skills can fetch advisories before the add commits (v1 renders at commit time; revisit after advisory soak).
- Dashboard advisory column/history view beyond the minimal U9 409 alert (`GET /api/decisions` covers audits meanwhile).
- Group-membership changes (`gixen group`) as a check trigger — exposure reflects regrouping at the next evaluated write.
- Observe-only checks on the sync-loop ingestion paths (web-added snipes, cap mirror) — advisory ledger rows for writes that originate on gixen.com.
- `/ce-compound` doc capturing BUI-168 add-batch semantics (no docs/solutions entry exists today; U9 touches those semantics).

---

## Risks & Dependencies

- **Money-path regression risk:** the check point sits inside both write handlers. Mitigation: checks are read-only, wrapped, and tri-state; a check or ledger failure degrades to warn-and-proceed (the fail-closed direction belongs only to U9's explicitly flagged blocking branch, per the guard-strictness learning).
- **Alert fatigue:** deterministic checks (ratio, age, group membership, sum) are facts, not heuristics — precision risk concentrates in U8, which is measurement-gated. If advisory volume in the soak is high, thresholds are env-tunable without a deploy.
- **Version skew:** old CLI ↔ new server and new CLI ↔ old server both tolerated (KTD4/U5). The Mini deploy runs `scripts/deploy.sh` (BUI-612), which performs the full ritual (`--force --no-cache` reinstalls, `uv sync --all-packages`, `launchctl kickstart`) and asserts deployed SHAs match merged HEAD.
- **Lot advisories:** KTD8's list-form payload removes the structural false-fire, but a caller supplying one identity for a genuine multi-book lot still draws a false over-FMV advisory; closing it fully needs briefs to decompose lots (out of scope here — advisory-only tolerates it, and blocking mode should not be enabled for over-FMV while lot briefs stay single-identity).
- **`apps/fmv` rung drift:** canary-covered (KTD5); the canary must be proven able to fail before merge.
- **Advisory-only checks get ignored** (the non-required-CI lesson): mitigated by the add-batch summary counts (a batch can't end clean with advisories) and the `GET /api/decisions` audit surface; the soak review (Operational Notes) is the human backstop.

---

## Documentation / Operational Notes

- New env vars documented in `packages/gixen-cli/CLAUDE.md` and the server install notes: `POLICY_FMV_MULTIPLE`, `POLICY_FMV_STALE_DAYS`, `POLICY_EXPOSURE_CEILING`, `POLICY_BLOCK_*` (U9). All optional; the layer ships inert except exposure/FMV advisories with defaults.
- Rollout: Phase A+B deploy is advisory-only by construction (`scripts/deploy.sh` on the Mini, BUI-612). Before any `POLICY_BLOCK_*` flip, run an advisory soak — review `GET /api/decisions` after a few real buys; a check that advises falsely gets tuned or stays advisory.
- CONCEPTS.md already defines Pre-Trade Check and Decisions Ledger (added with the origin doc).

---

## Assumptions

- Commit-time advisory rendering satisfies the origin's "approval moment" intent for v1; the dry-run preview is deferred, not dropped.
- Env-var + `launchctl kickstart` counts as "tunable without a deploy" (origin's operator-tunable ask); no config table is introduced.
- The ledger lives host-side with JSON payloads for comic-specific detail; the overlay does not own a parallel ledger.
- Proposed defaults (N=1.0, K=30 days, ceiling unset/disabled) stand until the advisory soak suggests otherwise.
- The sync-loop mirror and web-added ingestion stay check-free in v1 because they mirror decisions already made on gixen.com; their exposure contribution is the part that must not be lost, and isn't.

---

## Sources / Research

- Origin: `docs/brainstorms/2026-08-02-capital-commitment-layer-requirements.md` (BUI-609; seeded from `docs/ideation/2026-08-01-repo-improvements-ideation.md` survivor 5).
- Write paths and lock coverage: `packages/gixen-cli/server/main.py` (`api_add_bid` :1628, `api_edit_bid` :2037, web-added insert :686, `_reconcile_after_unconfirmed_modify` :1941), `packages/gixen-cli/server/db.py` (`mirror_gixen_max_bid` :1130, `_dedup_pending_and_index` :528, `TOMBSTONE_STATUSES_SQL` :42).
- Plugin mechanics: `packages/gixen-cli/gixen/plugins.py` (`GixenPluginSpec`, `load_plugins`, isolation helpers, `make_plugin_manager`); overlay registration in `plugins/gixen-overlay/src/gixen_overlay/plugin.py`.
- FMV resolution reuse: `plugins/gixen-overlay/src/gixen_overlay/routes.py` (`_resolve_fmv_for_link` :366), `plugins/gixen-overlay/src/gixen_overlay/db.py` (`get_primary_fmv_for_bid` :1334, `link_fmv_to_bid` :1272).
- Rung ladder: `apps/fmv/src/fmv_math.py:613-675` (`bid_factor`, `BASE_BID_FACTOR`, `INTERPOLATED_BID_FACTOR`).
- Payload sharing: `packages/gixen-cli/add_batch.py` (`build_bid_payload` :247, `add_one_row` :282).
- Institutional learnings applied: `docs/solutions/design-patterns/guard-strictness-must-match-consequence.md`, `docs/solutions/architecture-patterns/add-a-safety-gate-as-a-fail-closed-precheck-not-by-relocating-an-invariant.md`, `docs/solutions/architecture-patterns/durable-evidence-store-encode-unknowns-and-identity-precisely.md`, `docs/solutions/conventions/write-transaction-per-cycle-isolation.md`, `docs/solutions/design-patterns/scope-status-writes-to-row-id-not-item-id.md`, `docs/solutions/architecture-patterns/http-only-contracts-need-a-source-parsing-canary.md`, `docs/solutions/design-patterns/metron-5xx-detection-trips-batch-breaker.md`, `docs/solutions/conventions/bid-group-purge-is-hygiene-not-safety-net.md`, `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`, `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` (§5i precision bar).
