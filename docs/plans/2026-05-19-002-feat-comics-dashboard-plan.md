---
title: "feat: Build Comics Dashboard with Condition and FMV Data"
type: feat
status: active
date: 2026-05-19
origin: docs/brainstorms/2026-05-19-comics-dashboard-requirements.md
---

# feat: Build Comics Dashboard with Condition and FMV Data

## Overview

Rewrite the `/comics` plugin tab from a thin read-only comics-table view into a full snipes dashboard enriched with condition and FMV data. The dashboard supports active-auction monitoring (the primary use case — raise/lower max-bid decisions based on a value-vs-FMV signal) and post-mortem on ended auctions (close-call analysis vs the user's max).

A new pair of plugin-owned endpoints does the join server-side, so gixen-cli's core stays comic-free per PER-39. The dashboard itself is implemented independently from `/` — patterns from `/`'s snipes-table logic are referenced but not shared at runtime. The two dashboards drift; each owns its own rendering.

## Problem Frame

The `/comics` tab today is alphabetically sorted, has no auction lifecycle, no inline editing, no FMV/condition signal in context. The `/` dashboard post-PER-39 is generic with no comic awareness. For a user who only snipes comics, neither view supports the actual decision loop ("is this bid still a deal?") in a single place. (see origin: `docs/brainstorms/2026-05-19-comics-dashboard-requirements.md`)

## Requirements Trace

Carries forward all 21 requirements from the origin doc:

- R1, R2, R3 — Filter scope and row model (all snipes, one row per bid, `ebay_title` as title)
- R4, R5, R6, R7, R8 — Active section (structure, inline-edit, remove, cond/fmv/value columns, needs-linking treatment)
- R9, R10 — Ended section + spread column
- R11, R12, R13, R17 — Lot rendering (badge, primary-grade cond + "(+N more)", aggregated FMV, blank value %)
- R14, R15, R21 — Navigation (dynamic `/api/dashboard-tabs`), auto-refresh cadence, no user-sortable columns
- R16 — Data path: new plugin endpoints `/api/comics/snipes` + `/api/comics/history` doing the join server-side
- R18, R19 — Empty states for both sections
- R20 — Overbid row highlight takes visual precedence over value-column red

Success criteria (see origin) — active value signal, ended spread visibility, lot row clarity, unlinked snipes still visible.

## Scope Boundaries

- gixen-cli core endpoints (`/api/snipes`, `/api/history`) stay untouched. Plugin's new endpoints do their own join.
- No shared JS/CSS modules between `/` and `/comics`. Each dashboard owns its rendering.
- No FMV `confidence` column in v1.
- No user-sortable columns in v1 (fixed sort order per section).
- No expandable per-row lot drilldown.

### Deferred to Separate Tasks

- Threshold tuning for the green/neutral/red value % bands — post-launch, once real comp data accumulates. May warrant confidence-weighted bands. Not blocking v1.
- Investigating whether gixen-cli's history captures `max_bid` at auction close vs the live value — if live, mid-auction max edits make the ended `spread` (R10) slightly misleading after the fact. See R10 deferred note in origin. Address separately if it bites in practice.

## Context & Research

### Relevant Code and Patterns

- **Existing plugin static page**: `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` — full file gets rewritten. Lines 17 and 163 contain stale `/v2/comics` URLs to remove during the rewrite.
- **Existing plugin routes**: `plugins/gixen-overlay/src/gixen_overlay/routes.py` — `variant_v2_comics()` at line 27 serves the HTML; new endpoints go here.
- **Existing plugin DB helpers**: `plugins/gixen-overlay/src/gixen_overlay/db.py` — has `list_comics()`, `upsert_fmv()`, etc. New join query is endpoint-local for now; promote to a helper if it gets reused.
- **Plugin DB-access pattern**: `request.app.state.db` (sqlite3 connection from gixen-cli core). The plugin shares the same connection and can JOIN `bids` (gixen-cli) with `comics`/`fmv`/`bid_fmvs` (plugin).
- **gixen-cli `/api/snipes` shape**: `Projects/gixen-cli/server/main.py:777-817`. Authoritative source for base snipe fields the new endpoints should return alongside the enriched comic fields.
- **gixen-cli `/api/history` shape**: `Projects/gixen-cli/server/main.py:820-866`. Same shape as `/api/snipes` plus `winning_bid` for ended rows.
- **Snipes-dashboard JS reference**: `Projects/gixen-cli/server/static/index.html` — patterns for inline-edit max_bid (line ~562), two-click remove (line ~661), attention flags (~466), refresh-bar sweep (~127), suppress-refresh guard (~554). Read but copy independently into `v2-comics.html`. Do not import or link at runtime.
- **Dynamic dashboard tabs**: `Projects/gixen-cli/server/main.py:743` exposes `/api/dashboard-tabs`. Use this client-side in `v2-comics.html` instead of hard-coding tab links.
- **Shared CSS**: `/static/v2.css` served by gixen-cli at `server/main.py:729-734`. Already loaded by `v2-comics.html:10`. New page-specific styles (overbid row, urgency cell, value-cell colors, needs-linking dim/icon, lot badge) live in a `<style>` block in `v2-comics.html` mirroring the per-page-style pattern in `index.html:11-152`.
- **Plugin test pattern**: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py` — uses `fastapi.testclient.TestClient` against the real gixen-cli `server.main.app` with the plugin loaded via entry-point discovery. Same harness applies to the new endpoints.

### Institutional Learnings

- `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md` — historical migration constraints for the fmv-split work. Not directly applicable to this plan (no schema changes here) but documents the shape of cross-table joins in the shared SQLite.

### External References

None used. All patterns are local.

## Key Technical Decisions

- **New plugin endpoints, not extension of `/api/snipes`** (see origin: data path): Keeps gixen-cli core comic-free per PER-39. Plugin owns the JOIN against shared SQLite.
- **`value_pct` computed server-side**: The endpoint returns the precomputed `value_pct` field alongside `fmv_low`/`fmv_high`/`cond_grade` so the JS only renders, not calculates. Trade-off: client can't easily re-color with different thresholds without an endpoint change — acceptable for v1.
- **Endpoint emits raw numeric fields alongside formatted strings**: gixen-cli's `/api/snipes` returns `max_bid` as the formatted string `"475.00 USD"` (`server/main.py:803`). For JS math (overbid detection, spread arithmetic) and server-side `value_pct` to work cleanly, the new endpoints additionally return `max_bid_numeric` (float) and `current_bid_numeric` (float, parsed from the eBay scrape). JS preferentially reads the `_numeric` fields and falls back to `parseAmt()` if absent.
- **Server-side conditional aggregates, not WHERE-inside-aggregate**: `MAX(CASE WHEN bid_fmvs.is_primary=1 THEN fmv.grade END)` returns the primary grade in a single GROUP BY query alongside the lot-wide `SUM(fmv.low)` / `SUM(fmv.high)` / `COUNT()`. Avoids subqueries and the (invalid) pseudo-SQL `MAX(x) WHERE y`.
- **`cond_grade` is raw float, JS formats via `gradeLabel()`**: No server-side grade-to-label mapping. Reuses the existing JS helper.
- **`cond_extra_count` is clamped non-negative** (`max(0, lot_count - 1)`): protects against the `lot_count=0` (unlinked) case from leaking `-1` into the `(+N more)` template.
- **Active/ended split is client-side**: `/api/comics/snipes` mirrors `/api/snipes`'s filter (`status != 'PURGED'`, no end-date predicate). JS uses `isEnded(r)` to partition, so a snipe ticking past T=0 mid-session moves into the ended table without re-fetch.
- **`/api/comics/history` mirrors `/api/history`** (7-day OR-clause with `resolved_at` fallback + `MAX(id) per item_id` dedup).
- **`needs_linking` flag returned by endpoint**: Boolean indicating no `bid_fmvs` row for the bid. JS uses this for the dim-row + needs-link-icon treatment. Distinct from "linked but no FMV pricing data" (which renders `value_pct=null` but doesn't apply the dim treatment in v1; revisit if it conflates two states the user cares about distinguishing).
- **Lot partial-null nulls the FMV aggregate**: when any `bid_fmvs` row for a lot has NULL `fmv.low` or NULL `fmv.high`, the endpoint returns `fmv_low=null`/`fmv_high=null` to prevent silently understating the lot's FMV (SQLite `SUM` ignores NULLs by default).
- **`value_pct` thresholds (explicit boundaries)**: `value_pct < 80` → green; `80 ≤ value_pct ≤ 100` → neutral; `value_pct > 100` → red. v1 placeholder; tunable post-launch.
- **Single-comic partial FMV → `value_pct=null`**: when `fmv_low XOR fmv_high` is null, don't pick a side for the midpoint; render `—`.
- **`_ensure_fresh_sync` reused via import from `server.main`**: the new `/api/comics/snipes` calls `await _ensure_fresh_sync()` + `_spawn_fallback_task()` at the top, identical to gixen-cli's `/api/snipes`. This adds a tight cross-repo coupling to private (`_`-prefixed) helpers in `server.main`, but the plugin already imports from `server.*` and a break would surface loudly at import time. Alternative considered: have JS dual-fetch `/api/snipes` (sync trigger) + `/api/comics/snipes` (enrichment) — rejected because it doubles network calls and complicates merging.
- **Two endpoints, not one**: `/api/comics/snipes` (active) and `/api/comics/history` (ended) match the gixen-cli core split. Same DB connection, same JS call site shape.
- **Dashboard JS is monolithic in `v2-comics.html`**: No shared module with `/`. Copy patterns; accept duplication.
- **Dynamic tabs via `/api/dashboard-tabs`**: filter by `tab.path !== window.location.pathname` (not hardcoded `/comics`) so a future rename doesn't break current-page suppression.

## Open Questions

### Resolved During Planning

- **JSON shape of the new endpoints**: Build on `/api/snipes` shape (item_id, title, current_bid, max_bid, end_date_iso, status, status_mirror, winning_bid, seller, cached_at, bid_offset, snipe_group, local_snipe_at, local_snipe_result — see gixen-cli `server/main.py:799-816`). Crucially, **emit raw numeric `max_bid_numeric` and `current_bid_numeric` fields alongside the formatted strings** — gixen-cli's `/api/snipes` returns `max_bid` as the formatted string `"475.00 USD"` (`server/main.py:803`), and `current_bid` as the raw scraped string from eBay. JS math (overbid, spread) and server-side `value_pct` both need numeric values. Add: `cond_grade` (float or null — the raw primary grade, e.g. 9.4), `cond_extra_count` (int, 0 for non-lots and unlinked rows, ≥1 for lots), `fmv_low` (float or null), `fmv_high` (float or null), `value_pct` (float or null), `lot_count` (int — 0 for unlinked, 1 for single comic, ≥2 for lots), `needs_linking` (bool). The JS uses the existing client-side `gradeLabel()` helper to format `cond_grade` into "9.4 NM+" — no Python-side grade-to-label mapping is needed.
- **Sort order**: Active sorts by `end_date_iso` asc (handled client-side via `isEnded` split + sort, mirroring `/`); ended sorts by `end_date_iso` desc server-side. Match gixen-cli core's behavior.
- **Refresh cadence**: 30s API refresh + 1s repaint (verified: gixen-cli `server/static/index.html:185` has `REFRESH_MS = 30000`). The existing `v2-comics.html:41` value of 60000 is replaced by the rewrite.
- **Empty state copy**: `// no active snipes` for active, `// none` for ended. The existing single-section pattern at `v2-comics.html:99-103` is replaced (its message `// no comics yet — add a snipe via cli.py then run extract-comics` no longer applies once active/ended are split into separate panels).
- **Value % threshold boundaries (explicit rule)**: `value_pct < 80` → green; `80 ≤ value_pct ≤ 100` → neutral; `value_pct > 100` → red. Boundary values (exactly 80, exactly 100) fall into neutral.
- **`value_pct` for partial FMV data (single comic)**: when `fmv_low XOR fmv_high` is null, set `value_pct = null` (render as `—`). Don't pick a side — half-data shouldn't drive a color signal.
- **Active vs ended split is client-side, not server-side**: `/api/comics/snipes` mirrors `/api/snipes`'s filter exactly (`status != 'PURGED'`, no end-date filter). The JS uses the existing `isEnded(r)` helper to partition into the two tables, so a row that ticks past its end time during a session moves from active to ended on the next 1s repaint without needing a re-fetch.
- **`/api/comics/history` filter**: mirror `/api/history`'s 7-day window with the resolved_at fallback (`server/main.py:830-841`): `(auction_end_at NOT NULL AND ended within 7 days) OR (auction_end_at IS NULL AND resolved_at NOT NULL AND resolved within 7 days)`. Apply gixen-cli's dedup pattern — `INNER JOIN (SELECT item_id, MAX(id) FROM bids GROUP BY item_id)` — so a re-added snipe doesn't appear twice.

### Deferred to Implementation

- **Lot FMV partial-null handling** (open product question, presented to user during review): when any `bid_fmvs` row for a lot has NULL `low` or NULL `high`, should the aggregate be (a) NULL (signal incomplete), (b) the partial sum with a `fmv_complete: false` flag, or (c) the partial sum silently. See Risks section for the recommended interim behavior.
- **Lot composition for `cond_extra_count`**: the field is `max(0, (bid_fmvs.count for bid_id) - 1)`. Whether to additionally surface other linked grades (e.g., for a future tooltip) is out of v1 scope.
- **JS test approach for dashboard interactions**: there is no JS test harness in this repo today. Manual browser verification is the v1 acceptance bar for JS behavior; backend endpoints get full pytest coverage. If interaction bugs prove costly, add Playwright in a separate PR.
- **`_ensure_fresh_sync` strategy** (architectural choice, see Risks): the new endpoint by itself does not trigger gixen-cli's Gixen pull-on-visit (`server/main.py:784-785`). Without coordination, `/comics` serves cached-only data and can drift behind `/`. See Unit 1 Approach for the chosen strategy.

## Implementation Units

- [ ] **Unit 1: Plugin endpoints `/api/comics/snipes` and `/api/comics/history`**

**Goal:** Server-side join of gixen-cli's `bids` with the plugin's `comics`/`fmv`/`bid_fmvs` tables, exposed as two read endpoints that return snipe rows enriched with comic data, ready to drop into the dashboard's existing fetch pattern.

**Requirements:** R1, R2, R7 (data shape), R10 (spread inputs), R11, R12, R13, R16, R17, R8 (`needs_linking` flag), R9 (history shape)

**Dependencies:** None.

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/routes.py` (add the two endpoints)
- Modify: `plugins/gixen-overlay/src/gixen_overlay/db.py` (optional: extract the JOIN as a helper if it would otherwise be inlined twice)
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py` (add a new test class for these endpoints)

**Approach:**
- One SQL query per endpoint. JOIN `bids` LEFT JOIN `bid_fmvs` LEFT JOIN `fmv` LEFT JOIN `comics`, GROUP BY `bids.id`, aggregating across the multi-row case (lots). Use conditional aggregates (not WHERE filters inside aggregates):
  - `MAX(CASE WHEN bid_fmvs.is_primary = 1 THEN fmv.grade END) AS primary_grade`
  - `SUM(fmv.low) AS fmv_low_sum`, `SUM(fmv.high) AS fmv_high_sum`
  - `COUNT(bid_fmvs.fmv_id) AS lot_count`
  - For lot FMV partial-null safety: also `SUM(CASE WHEN fmv.low IS NULL THEN 1 ELSE 0 END) AS fmv_low_nulls` (used to decide whether to expose the sum or null it out — see partial-null risk).
- Build response in Python from these aggregates:
  - `cond_grade = primary_grade` (raw float)
  - `cond_extra_count = max(0, lot_count - 1)` (clamps the unlinked case to 0, not -1)
  - `fmv_low`, `fmv_high`: the SUM values, or NULL when any component low/high was NULL (per the deferred partial-null decision; see Risks)
  - `lot_count`, `needs_linking = (lot_count == 0)`
  - `value_pct`: compute against numeric `max_bid` from `bids.max_bid` REAL column (not the formatted response field), null when `lot_count != 1` (lots or unlinked), null when `fmv_low XOR fmv_high` is null, null when both null
  - `max_bid_numeric = bids.max_bid` (raw float from the column), `current_bid_numeric = parseFloat(strip(cached_current_bid))` — Python regex-strip the eBay scrape string the same way the JS `parseAmt` does
- **Freshness**: import `_ensure_fresh_sync` and `_spawn_fallback_task` from `server.main` and call them at the top of `/api/comics/snipes` (the same pattern as `/api/snipes`). The plugin already imports `from server.db import get_bid_by_item_id` (`routes.py:19`), so importing the sync helpers from `server.main` is consistent. `/api/comics/history` is pure DB read (no sync) — matches `/api/history`.
- Active endpoint filter: `WHERE status != 'PURGED'` only — mirror `/api/snipes` exactly. Do not server-side filter by "auction not ended" — let the JS partition via `isEnded()` so a row aging past T=0 mid-session moves into the ended section on the next repaint.
- History endpoint filter: mirror `/api/history`'s OR-clause and the `MAX(id) per item_id` dedup subquery (`server/main.py:830-841`).

**Patterns to follow:**
- `Projects/gixen-cli/server/main.py:777-817` (`/api/snipes` response shape)
- `Projects/gixen-cli/server/main.py:820-866` (`/api/history` shape + filter logic)
- `plugins/gixen-overlay/src/gixen_overlay/db.py:list_comics` for LEFT JOIN style + sqlite3 Row dict conversion

**Test scenarios:**
- **Happy path:** A bid linked to one comic with grade=9.4, fmv_low=100, fmv_high=200, current_bid="120.00 USD", max_bid=125 → endpoint returns `cond_grade=9.4`, `cond_extra_count=0`, `fmv_low=100`, `fmv_high=200`, `value_pct=80.0` (= 120 / 150 * 100), `lot_count=1`, `needs_linking=false`, `max_bid_numeric=125`, `current_bid_numeric=120`.
- **Happy path (lot, all priced):** Bid linked to three comics (primary grade 9.4 + two more), all with fmv_low/high populated, sums = (low=150, high=300), current_bid="200.00 USD" → returns `cond_grade=9.4`, `cond_extra_count=2`, `fmv_low=150`, `fmv_high=300`, `value_pct=null` (R17 — lots don't get a value signal), `lot_count=3`.
- **Edge case (needs linking):** Bid exists in `bids` table but has no `bid_fmvs` row → returns `cond_grade=null`, `cond_extra_count=0` (clamped, not -1), `fmv_low=null`, `fmv_high=null`, `value_pct=null`, `lot_count=0`, `needs_linking=true`.
- **Edge case (FMV partial null, single comic):** Bid has linked fmv with `fmv_low=100` and `fmv_high=null` → endpoint returns `fmv_low=100`, `fmv_high=null`, `value_pct=null` (per the "don't pick a side" rule resolved during planning).
- **Edge case (lot partial null):** Bid linked to three comics where one has `fmv_low=NULL` → SQL `SUM` would silently drop it. Expected behavior depends on the partial-null product call (see Risks). The recommended interim is `fmv_low=null, fmv_high=null, value_pct=null` (treat partial lot as incomplete), but the test should be updated once the product decision lands.
- **Edge case (purged):** Snipe with `status='PURGED'` is excluded from both endpoints. (Mirror `/api/snipes` + `/api/history`.)
- **Edge case (ended-but-still-in-bids):** A snipe whose `auction_end_at` has passed but whose `status` is still PENDING appears in `/api/comics/snipes` (mirror `/api/snipes` behavior). The JS's `isEnded()` will move it to the ended table client-side.
- **Edge case (history dedup):** A bid for the same `item_id` exists in two rows (re-snipe after purge), both within the 7-day window → `/api/comics/history` returns one row (the latest by `MAX(id)`), matching `/api/history`.
- **Edge case (history resolved_at fallback):** A snipe with `auction_end_at IS NULL` but `resolved_at` within 7 days appears in `/api/comics/history` (mirror `/api/history`'s OR-clause).
- **Edge case (winning_bid surfacing on ended):** Ended snipe with `winning_bid=412` is returned by `/api/comics/history` with `winning_bid` populated. (Foundation for the JS `spread` computation in unit 3.)
- **Edge case (freshness):** Hitting `/api/comics/snipes` triggers `_ensure_fresh_sync` (same TTL behavior as `/api/snipes`). Two rapid successive calls deduplicate via `_sync_lock`. Verify by mocking `_sync_gixen` and asserting it was called.
- **Error path:** Database connection failure surfaces as a 500 with FastAPI's default handler (same as `/api/snipes`). No custom error handling needed.
- **Integration:** Endpoint returns dicts that JSON-serialize cleanly (sqlite3.Row → dict conversion); response passes through FastAPI's response model without sentinel values.

**Verification:**
- Both endpoints return 200 with correctly shaped JSON for all scenarios above.
- A row that appears in `/api/snipes` also appears in `/api/comics/snipes` with the same base fields (item_id, max_bid, status, etc.) — no rows are lost.
- A bid linked to N comics returns one row with `lot_count=N`, not N rows.
- `/api/comics/snipes` triggers the Gixen pull-on-visit identical to `/api/snipes`.

---

- [ ] **Unit 2: Rewrite `v2-comics.html` — active section, all columns, basic interactions**

**Goal:** Replace the existing thin `v2-comics.html` with a full active-snipes dashboard table including the new `cond`/`fmv`/`value` columns, urgency styling, overbid row highlight + attention banner, inline-edit max_bid, two-click remove, and refresh loop. Drives the primary use case (live bidding decisions).

**Requirements:** R3 (ebay_title), R4 (active structure), R5 (inline-edit), R6 (remove), R7 (cond/fmv/value with v1 thresholds), R8 (needs-linking visual), R15 (refresh cadence), R20 (overbid > value-red precedence), R21 (no user-sortable columns)

**Dependencies:** Unit 1 (endpoints must exist).

**Files:**
- Rewrite: `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` (complete rewrite of HTML + page-specific `<style>` + JS)
- Test: manual browser verification (no JS test harness in this repo; document acceptance steps in the unit's verification section)
- Test (smoke): `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py` (add a smoke test that GET `/comics` returns 200 with `Content-Type: text/html` and the page references the new endpoint paths)

**Approach:**
- HTML scaffold: `topbar` (with dynamic tab placeholder), `refresh-bar`, `wrap > session`, `flags-container`, `active-section`, `ended-section` (placeholder, populated by Unit 3), `keybar`. Mirror `index.html`'s top-level structure.
- Page-specific `<style>` block at the top of `v2-comics.html` — copy the overbid/urgent-cell/editable-max/remove-btn/refresh-bar/flags styles from `index.html:13-152` verbatim, then add new rules for:
  - `.value-green` (color: var(--green)), `.value-neutral` (color: var(--fg-dim)), `.value-red` (color: var(--red))
  - `.needs-linking` (row treatment: `opacity: 0.55`)
  - `.needs-link-icon::before { content: "⛓ "; color: var(--fg-dim); }` adjacent to the title cell
  - `.lot-badge` (small inline badge: `font-size: 10px; color: var(--fg-dim); border: 1px solid var(--line); padding: 0 4px; border-radius: 3px; margin-left: 6px;`)
  - `.cond-extra` (inline dim text: `color: var(--fg-dim); font-size: 11px;`)
  - `.spread-over` (color: var(--red); font-weight: 700;) — used by Unit 3
  - `.spread-under` (color: var(--fg-dim);) — used by Unit 3
- **Column headers** (active table, in order): `item`, `title`, `cond`, `current`, `max`, `fmv`, `value`, `t-minus`, `seller`. Lowercase, terse — matches `index.html`'s style.
- JS: fetch `/api/comics/snipes` (active list). On render:
  - For each row, parse numerics: `const cur = r.current_bid_numeric ?? parseAmt(r.current_bid); const max = r.max_bid_numeric ?? parseAmt(r.max_bid);` — prefer the explicit numeric fields from the endpoint, fall back to `parseAmt` for safety.
  - Render columns per the header list. `title` cell includes the needs-link icon (when `needs_linking`) and the `lot of N` badge (when `lot_count > 1`).
  - `cond` cell: if `cond_grade != null`, render `${cond_grade} ${gradeLabel(cond_grade)}`. If `cond_extra_count > 0`, append `<span class="cond-extra">(+${n} more)</span>`. If `needs_linking`, render `—` (the icon already lives on the title cell).
  - `value` cell: blank (`—`) when `value_pct == null`; otherwise `${pct.toFixed(0)}% of FMV` with class `.value-green` for `pct < 80`, `.value-neutral` for `80 <= pct <= 100`, `.value-red` for `pct > 100`. Boundary values (80, 100) fall into neutral.
  - **`fmv` cell format**: `${fmtUSD(fmv_low)}–${fmtUSD(fmv_high)}` with an en-dash (`U+2013`). When both sides are non-null and round-equal, render a single value. When one side is null, render `—`. Reuse `fmtUSD` (already in `index.html:287`).
  - **R20 precedence**: if `cur != null && max != null && cur >= max`, the row gets `class="overbid"` (red row tint from copied CSS). The value cell still has its own class but is visually subordinate. Verify by hand that `.overbid` background contrast wins over `.value-red` text color.
  - **Urgency styling**: when `end_date_iso - now < 3600000ms` (1 hour), the `t-minus` cell gets `class="urgent-cell"` (orange, bold — copied verbatim from `index.html:26`).
- **Inline-edit max_bid**: copy `startMaxEdit` (`index.html:498-529`), `saveMaxBid` (`index.html:531-584`), and the `suppressRefresh` mechanic (`index.html:494`). Hits gixen-cli's `PATCH /api/bids/{item_id}` unchanged.
  - **Error state**: cell gets `class="save-error"` (red text), `title="save failed: ${msg}"`, reverts to `$${prev.toFixed(2)}` after 3500ms timeout (match `index.html:638-643` exactly).
- **Per-row remove**: copy `confirmRemove` (`index.html:597-620`) and the two-click click-handler (`index.html:622-657`). 250ms post-DELETE optimistic fade then refresh. Hits `DELETE /api/bids/{item_id}` unchanged.
- **Refresh loop**: 30s API `setInterval` + 1s repaint tick (`index.html:464-485` for the API loop; `index.html:736-744` for the repaint guard). Guard the 1s repaint against `.editable-max.editing/.saving`, `.remove-btn.confirming`, AND `.removing` to prevent a re-render from reviving a row mid-removal.
- Keep `_inFlight` flag and AbortController timeout from the original.
- **Status pill on error**: keybar status pill shows `${err.message}` (e.g., "HTTP 503") on fetch failure, reverts to row count + "synced HH:MM:SS" on next success — match `index.html:472-479`.
- **Responsive policy**: desktop-first. On viewports narrower than ~900px the table scrolls horizontally inside `.scroll` (already in `v2.css`). No columns hidden or reordered.

**Patterns to follow** (file: `Projects/gixen-cli/server/static/index.html`, referenced by function name to avoid line-number drift):
- `startMaxEdit`, `saveMaxBid`, `suppressRefresh` declaration (inline-edit pattern)
- `confirmRemove` + the global click-handler (two-click remove pattern)
- `buildFlags`, `renderFlags`, `renderActive`, `renderEnded` (active/ended render shape; flags are wired up in Unit 4)
- `outcome()` (won/outbid/missed pill rendering — used by Unit 3)
- `isEnded`, `parseAmt`, `fmtUSD`, `gradeLabel`, `escapeHtml`, `displayTitle`, `timeLeft` (helpers — copy as-is)
- `repaint` + refresh-bar `setInterval(load, REFRESH_MS)` (refresh/repaint cycle)
- CSS animation block for `.refresh-bar` (verbatim CSS copy)

**Test scenarios:**
- **Happy path (manual):** Open `/comics` with a snipe in the DB → active table renders, cond/fmv/value populated, urgency styling applies under 1hr, t-minus counts down each second without re-fetch.
- **Happy path (smoke test):** GET `/comics` returns 200, HTML body includes `/api/comics/snipes` and `/api/dashboard-tabs` strings, page-specific CSS classes present.
- **Edge case (manual):** Overbid row (current ≥ max) shows red row tint AND attention banner appears at top.
- **Edge case (manual):** Needs-linking row renders dim with the icon, cond/fmv/value all show `—`.
- **Edge case (manual):** Value cell colors at the threshold boundaries — 79% green, 80% neutral, 100% neutral, 101% red.
- **Error path (manual):** Inline-edit save fails (network error or 5xx) → cell shows save-error state, reverts to previous max after timeout, suppress-refresh clears.
- **Integration (manual):** PATCH `/api/bids/{item_id}` succeeds on the comics page same as on `/` — the gixen-cli endpoint is shared, so identical behavior is expected.

**Verification:**
- Page loads at `/comics` with no console errors against a real plugin-loaded gixen-cli server.
- Active table renders all snipes with `auction_end_at` in the future and `status != 'PURGED'`.
- Inline-edit and remove both work and the dashboard auto-refreshes 30s later showing the persisted state.
- Value % column color matches the locked v1 thresholds for representative rows.

---

- [ ] **Unit 3: Ended section + spread column**

**Goal:** Add the ended-snipes table below the active table, with the sniping-specific `spread` column for post-mortem framing (loss = winner − max, win = max − winner, missed = —).

**Requirements:** R9 (ended structure), R10 (spread column)

**Dependencies:** Unit 1 (`/api/comics/history` endpoint), Unit 2 (the dashboard scaffold).

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` (add `ended-section` div, fetch + render code, styles for the spread cell)

**Approach:**
- Add `<div id="ended-section"></div>` below `active-section` in the HTML scaffold from Unit 2.
- JS: extend the existing fetch in Unit 2 to also call `/api/comics/history`, render ended rows.
- **Ended column headers** (in order): `item`, `title`, `cond`, `winning`, `max`, `fmv`, `spread`, `result`, `seller`.
- Spread cell logic — **compute against numeric fields, render with explicit sign prefix**:
  ```
  const max = r.max_bid_numeric ?? parseAmt(r.max_bid);
  const win = r.winning_bid != null ? parseFloat(r.winning_bid) : null;
  if (win == null) { cell = '—'; }
  else if (r.status === 'WON' || win <= max) {
    cell = `−$${Math.abs(max - win).toFixed(2)} under`;  // class .spread-under (dim)
  } else {
    cell = `+$${(win - max).toFixed(2)} over`;  // class .spread-over (red, bold)
  }
  ```
  The `−` and `+` are hardcoded display prefixes; the arithmetic uses `abs` to keep the sign explicit (an outbid where `win == max` would render `+$0.00 over` — match `index.html`'s behavior of trusting backend status, not the numeric tie).
- **Outcome pill**: trust `r.status` first (matches `outcome()` in `index.html` — `WON` → won, `LOST` → outbid, otherwise heuristic). Don't infer purely from `win <= max` because eBay's bid-increment rule means a tied bid still loses.
- Empty-state copy: `// none` (R19).

**Patterns to follow** (file: `Projects/gixen-cli/server/static/index.html`):
- `renderEnded` (table render shape — adapt for the new `spread` and `cond`/`fmv` columns)
- `outcome()` (status pill rendering — trust `status` before heuristics; verbatim copy is fine)

**Test scenarios:**
- **Happy path (manual):** Ended snipe with `status='WON'`, max=$475, winning=$412 → spread cell shows `−$63.00 under` (dim), result pill = won.
- **Happy path (manual):** Outbid snipe, max=$300, winning=$435 → spread shows `+$135.00 over` (red), result pill = outbid.
- **Edge case (manual):** Missed snipe (no winning_bid, status='LOST' or 'FAILED') → spread cell = `—`, result pill = missed (or matching label).
- **Edge case (manual):** Tie loss (winning_bid == max_bid, eBay bid-increment rule lost it) → status='LOST' from the backend (gixen-cli's authoritative status); spread computes as `0` over but the lost pill still renders. Don't infer from numeric tie alone — trust backend status as `index.html` does.
- **Integration (manual):** `/api/comics/history` returns the same set of rows as gixen-cli's `/api/history` (within 7-day window), just with comic enrichment fields added.

**Verification:**
- Ended table renders all ended snipes from the past 7 days with the correct spread sign, value, and color.
- Missed rows have no spread number (just `—`), distinguishing them from $0 wins.

---

- [ ] **Unit 4: Lot rendering, edge states, attention banner, refresh-bar**

**Goal:** Polish the dashboard with the remaining product requirements — lot badges, `(+N more)` cond hint, empty states, attention-required banner for overbid rows, and the refresh-bar sweep during in-flight fetches.

**Requirements:** R11 (lot badge), R12 (cond + N more), R13 (aggregated FMV — already handled by endpoint, JS just renders), R17 (blank value for lots), R18 (empty active), R19 (empty ended), R4 attention banner

**Dependencies:** Units 2 + 3 (core dashboard exists).

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html`

**Approach:**
- Lot badge: in the title cell, if `lot_count > 1`, render the title followed by `<span class="lot-badge">lot of ${n}</span>` (style defined in Unit 2).
- Cond `(+N more)` hint: handled in Unit 2's cond rendering; verify it works with `cond_extra_count` from the endpoint.
- Empty states (replacing the legacy single-section message): when active list is empty render `<div class="empty">// no active snipes</div>` inside the active panel; when ended is empty render `<div class="empty">// none</div>` inside the ended panel.
- Attention banner: `<div id="flags-container"></div>` above active section. JS computes flags by scanning active snipes for `cur != null && max != null && cur >= max` (using parsed numerics) and renders one `<li>` per overbid row: `#${item_id} "${title}" — current $X ≥ max $Y. raise or cancel?`. The `▲` glyph is added via CSS `.flags li::before` rule (copied from `index.html`), not the JS template. Match `buildFlags` + `renderFlags`.
- Refresh-bar: `<div class="refresh-bar" id="refresh-bar"></div>` between topbar and wrap. Toggle `.is-active` class during in-flight fetches. CSS animation block copied verbatim from `index.html` (already in the page-style block from Unit 2).

**Patterns to follow** (file: `Projects/gixen-cli/server/static/index.html`):
- `buildFlags`, `renderFlags` (attention banner — one entry per overbid row)
- `.empty` rendering pattern in `renderActive` / `renderEnded`
- `.refresh-bar` CSS animation block

**Test scenarios:**
- **Happy path (manual):** Snipe linked to 3 comics → title shows `lot of 3` badge after the ebay_title; cond cell shows primary grade + `(+2 more)`; fmv column shows aggregated range; value cell is blank.
- **Edge case (manual):** Empty active section → `// no active snipes` rendered, panel header present.
- **Edge case (manual):** Overbid snipe (current ≥ max) → attention banner appears at top with the item_id and current/max values; row tinted red.
- **Edge case (manual):** Refresh-bar sweep visible at the top of the page during a `/api/comics/snipes` fetch; disappears when fetch completes.

**Verification:**
- All lot-related visuals match the brainstorm's product decisions.
- Empty states never render a confusingly blank panel.
- Attention banner correctly fires only when at least one row is overbid.

---

- [ ] **Unit 5: Dynamic nav via `/api/dashboard-tabs` + URL cleanup**

**Goal:** Replace the hardcoded nav links in `v2-comics.html` with a dynamic fetch from `/api/dashboard-tabs`, matching how `/` is rendered. Fix the stale `/v2/comics` URLs at lines 17 and 163 of the pre-rewrite file (already noted as in-scope cleanup in the brainstorm's scope boundaries).

**Requirements:** R14 (nav matches `/` with dynamic tab fetch), scope-boundary cleanup of stale URLs

**Dependencies:** Units 2–4 (dashboard exists in some form; this is polish).

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` (nav section + keyboard handler)
- Test: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py` (smoke test for nav fetch — see below)

**Approach:**
- Topbar nav: keep the static `▎ gixen` brand and the `snipes` link to `/`. The active `▸ comics` segment stays inline (since this is the current page). For any other plugin-registered tab, fetch `/api/dashboard-tabs` on load and inject as additional `<a class="seg nav" href="${path}">${label}</a>` links.
- Filter the dynamic fetch by `tab.path !== window.location.pathname` so the current-page suppression isn't hardcoded to the string `/comics` (survives a future rename).
- Keyboard handler: change `if (e.key === "c") location.href = "/v2/comics"` to `"/comics"`.
- **`/v2/bids` is still live** (verified at gixen-cli `server/main.py:721`), so the existing `v2-comics.html:18` and the `b`-key keyboard handler that reference `/v2/bids` are correct — leave them.

**Patterns to follow:**
- `Projects/gixen-cli/server/main.py:707-718` shows how `/` injects tabs server-side. The comics page does it client-side via the API endpoint — same result, different mechanism. Either pattern is acceptable; client-side is simpler from a plugin perspective (no server-side HTML rewriting).

**Test scenarios:**
- **Happy path (manual):** Open `/comics` → nav shows `snipes / comics (active) / bids / [any other plugin tabs]` in correct order.
- **Edge case (smoke test):** GET `/comics` HTML body does NOT contain the string `/v2/comics` anywhere (confirms the cleanup).
- **Edge case (manual):** Pressing `c` on the comics page navigates to `/comics` (not 404).
- **Integration (manual):** Adding a hypothetical new plugin that registers a tab via `register_dashboard_tabs` makes that tab appear in `/comics`'s nav on next refresh.

**Verification:**
- The HTML file has zero occurrences of `/v2/comics`.
- The nav stays in sync with `/` if other plugins register tabs.

## System-Wide Impact

- **Interaction graph:** New plugin endpoints (`/api/comics/snipes`, `/api/comics/history`) read from gixen-cli's `bids` table + plugin's `comics`/`fmv`/`bid_fmvs` via the shared SQLite connection. No new writes. Inline-edit and remove from the dashboard hit gixen-cli's existing `PATCH/DELETE /api/bids/{item_id}` — same authoritative path as `/`.
- **Error propagation:** Fetch failures in the comics dashboard JS surface in the keybar status pill (same pattern as `/`). PATCH/DELETE errors from gixen-cli (404 GixenSnipeNotFoundError, 503 GixenError) propagate to the cell-level save-error / remove-error states.
- **State lifecycle risks:** None new — the dashboard is a read view + delegations to existing mutation endpoints. The suppress-refresh guard prevents an auto-refresh from clobbering an optimistic edit, same as `/`.
- **API surface parity:** `/api/snipes` and `/api/history` (gixen-cli core) are untouched. Plugin's new endpoints are net additions. Plugin entry-point registration via `register_routes` (existing pattern) covers route mounting.
- **Integration coverage:** The dashboard JS interactions need real browser verification; the endpoint tests use `TestClient` with the actual plugin entry-point harness already in `test_gixen_overlay_routes.py`.
- **Unchanged invariants:** gixen-cli core endpoints, the `bids` schema, the `dashboard_tabs` API, the `PATCH/DELETE /api/bids/{item_id}` contract, and the existing `/api/comics` plugin endpoint (still used by other consumers like the CLI) all stay as-is.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| The dashboard JS grows large and duplicates `/` patterns without a sharing mechanism — bugs may need fixing twice. | Accepted per the brainstorm's "drop parity criterion" decision. Patterns are referenced by function name in each unit's "Patterns to follow" section so a bug-fixer can locate the parallel implementation quickly. No drift-detection automation in v1. |
| `value_pct` thresholds (80% / 100%) ship with no real-data validation. | Document them as v1 placeholders in the endpoint code with a comment pointing to the deferred-research note. Easy to tune without an endpoint contract change once data accumulates. |
| Ended `spread` column can be misleading if max_bid was edited mid-auction (gixen-cli history stores the live max, not close-time). | Deferred to separate investigation per the origin doc. Not blocking v1. If it bites, capture a `max_bid_at_close` field at status-transition time. |
| Cross-repo coupling: the comics dashboard's mutation calls depend on gixen-cli endpoints staying compatible. Plugin also imports private helpers `_ensure_fresh_sync` and `_spawn_fallback_task` from `server.main`. | Existing assumption — gixen-cli is the upstream; the plugin tracks its API. The plugin already imports `from server.db import get_bid_by_item_id`, so the additional import from `server.main` is consistent. If those helpers are refactored, the plugin breaks at import time (loud, not silent). |
| Manual browser testing as the v1 acceptance bar for JS interactions leaves room for regressions in inline-edit, remove, and refresh behavior. | Document the manual acceptance checklist in each unit's verification section. Smoke test verifies HTML loads and endpoints are referenced. Add Playwright later if regressions appear. |
| **Lot FMV aggregation silently understates when components have NULL low/high.** SQLite `SUM` ignores NULLs, so a lot of 3 comics where 1 lacks pricing data returns sum-of-2 with no marker. | **Recommended interim**: when any component has NULL low or NULL high, set the aggregate `fmv_low`/`fmv_high` to NULL (mark lot as incomplete via `value_pct=null`, `fmv` cell renders `—`). Test scenario captures this. Revisit if it hides too many lots in practice — alternative is a `fmv_complete: false` flag exposed to JS. |
| **Stale `cached_at` on the comics dashboard if `_ensure_fresh_sync` import path changes.** | The plan opts to import `_ensure_fresh_sync`/`_spawn_fallback_task` from `server.main` (matching the existing `from server.db import get_bid_by_item_id` pattern). If gixen-cli renames these or refactors them out of `server.main`, the plugin breaks loudly at import. Document this coupling in `Projects/comic-pipeline/README.md` under "Cross-repo dependencies". |

## Documentation / Operational Notes

- Update the plugin's `README.md` (if it exists) or `Projects/comic-pipeline/README.md` to mention the new endpoints and reflect the redesigned `/comics` page.
- No new env vars, secrets, or deployment changes.
- No migrations.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-19-comics-dashboard-requirements.md](../brainstorms/2026-05-19-comics-dashboard-requirements.md)
- Related code (plugin): `plugins/gixen-overlay/src/gixen_overlay/routes.py`, `plugins/gixen-overlay/src/gixen_overlay/db.py`, `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html`, `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`
- Related code (gixen-cli, reference only): `Projects/gixen-cli/server/main.py` (lines 707-718 tab injection, 729-734 v2.css, 743 dashboard-tabs, 777-817 `/api/snipes`, 820-866 `/api/history`, 899-956 PATCH/DELETE bids), `Projects/gixen-cli/server/static/index.html` (snipes-dashboard JS reference)
- Related commits: gixen-cli `4d490dd` (PER-39 — comic-free /), `5e5b171` (route rename), `b2c77f7` (server-side tab injection)
- Linear: PER-40
