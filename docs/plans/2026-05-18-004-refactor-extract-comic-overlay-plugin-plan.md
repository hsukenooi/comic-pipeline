---
title: "refactor: Extract Comic Overlay as comic-pipeline Plugin (PER-30)"
type: refactor
status: active
date: 2026-05-18
---

# refactor: Extract Comic Overlay as comic-pipeline Plugin (PER-30)

**Target repos:** `gixen-cli` (decontamination) and `comic-pipeline` (plugin implementation)

## Overview

All comic-specific code currently in `gixen-cli` is extracted into the `comic-pipeline` monorepo as the `gixen-overlay` pluggy plugin. After this change, a clean `gixen-cli` install has no comic surface; users who install `gixen-overlay` alongside it regain the full comic workflow via the plugin hook system established in PER-25–PER-29.

## Problem Frame

`gixen-cli` is the sixth issue of the 13-issue "Separate gixen-cli from Comics Use Case" refactor. PER-25–PER-29 (plugin entry-points, route registration, DB table hook, dashboard tab hook, comic-pipeline bootstrap) are all merged to `feat/plugin-refactor`. This issue executes the actual extraction and decontamination.

Origin context: `docs/refactor-split-handoff.md` — full inventory of contamination, architectural decisions, and known protocol gaps.

## Requirements Trace

- R1. `pytest tests/` passes in `gixen-cli` on the decontaminated branch
- R2. `grep -r "comic\|locg\|cgc\|fmv" server/ gixen_client.py --include="*.py" -l` returns no results (test_server_api.py's comic-stub fixture is the only allowed exception)
- R3. Plugin installed in `comic-pipeline/plugins/gixen-overlay/` with all three hookimpls wired and tested
- R4. `GET /api/dashboard-tabs` returns `[{"label": "comics", "path": "/v2/comics"}]` when plugin is installed
- R5. `GET /v2/comics` serves the comics dashboard HTML when plugin is installed
- R6. All comic routes (`/api/comics`, `/api/bids/{item_id}/comics/locg`, `/api/extract-comics`) work when plugin is installed

## Scope Boundaries

- `cli.py locg` subcommand and `cli.py extract-comics` command are **kept as-is** — they call plugin-only routes (will 404 without the plugin; this is acceptable)
- `bids.comic_id` column in the `bids` table stays in core (it's an opaque int the plugin interprets, per PER-27 decision)
- `insert_bid(conn, ..., comic_id: int | None)` stays in `server/db.py` — nullable int param is generic
- No new `register_cli_commands` hookspec in this issue (flagged as a future protocol gap)
- No `register_bid_enrichment` hookspec (the enrichment hook that would let the plugin re-add comic columns to `/api/snipes` is out of scope; deferred)

### Deferred to Separate Tasks

- Re-enriching `/api/snipes` response with comic columns via a plugin hook: PER-38 (future)
- `cli.py add --comic/--issue/--grade` flags: stripped in this issue; a `register_cli_commands` hook to restore them is PER-38 or later
- Plugin CI, versioning, and PyPI publication: post-PER-30

## Context & Research

### Relevant Code and Patterns

- Plugin hook dispatch: `gixen/plugins.py` — `_invoke_db_tables_isolated`, `_invoke_register_routes`, `_collect_dashboard_tabs`
- Existing plugin stub: `plugins/gixen-overlay/src/gixen_overlay/plugin.py`
- Static file serving pattern: `server/comic_routes.py` line 80–85 — `FileResponse(Path(__file__).parent / "static" / "v2-comics.html")`
- DB functions to move: `server/db.py` — `upsert_comic` (~line 134), `link_comic_to_bid` (~204), `get_comics_for_bid` (~236), `get_primary_comic_for_bid` (~252), `list_comics` (~342)
- Canonical DDL: `tests/test_server_db.py` — `_COMICS_DDL` and `_BID_COMICS_DDL` (inline constants, lines 18–45)
- Comic-stub fixture pattern: `tests/test_server_api.py` — `_make_comic_schema_plugin()` (lines 25–74)
- Route tests pattern: `tests/test_plugin_integration.py` and `tests/test_comic_routes.py`

### Institutional Learnings

- `docs/solutions/` not yet created; this is the first extraction — patterns established here become the canonical reference
- PER-27 decision: `bids.comic_id` is an opaque int (no FK) that the plugin interprets; existing user DBs are migrated by `_apply_migrations` in core
- Plugin entry-point value must be a module-level object instance (`plugin = GixenOverlayPlugin()`), not the class

## Key Technical Decisions

- **Comic DB functions move to `gixen_overlay/db.py`**: The plugin owns its own data layer; importing from `server.db` would create comic-aware surface in core. The plugin already depends on `gixen-cli` so it can import `server.db` generic functions (`insert_bid`, `get_bid_by_item_id`, `get_all_bids`) but owns its own comic-specific ones.
- **`v2-comics.html` updated to not rely on comic columns in `/api/snipes`**: After decontamination, `/api/snipes` no longer JOINs `comics`. The plugin's `v2-comics.html` fetches `GET /api/comics` directly and correlates by `comic_id` from `GET /api/snipes` (which still returns the opaque `bids.comic_id` int).
- **`api_add_bid` loses comic fields**: One-shot "add bid + link comic" becomes two API calls. Acceptable regression — the plugin provides `POST /api/comics` and `POST /api/extract-comics` to cover the workflow.
- **`cli.py add` strips 11 comic flags**: They become dead code once `AddBidRequest` strips them. Dead CLI flags are worse than missing ones.
- **`FileResponse(__file__-relative)` for plugin's HTML**: Same pattern as current `comic_routes.py`. `importlib.resources` is overkill for a single HTML file in an editable install.
- **`test_comic_routes.py` deleted from gixen-cli**: The structural invariants it tests move with the routes. Plugin gets its own test file.

## Output Structure

```
comic-pipeline/
└── plugins/gixen-overlay/
    └── src/gixen_overlay/
        ├── __init__.py          (unchanged)
        ├── plugin.py            (wired hookimpls — was stub)
        ├── db.py                (new — comic DB functions)
        ├── models.py            (new — UpsertComicRequest, LocgLinkRequest)
        ├── routes.py            (new — moved from server/comic_routes.py)
        ├── title_parser.py      (new — moved from server/title_parser.py)
        └── static/
            └── v2-comics.html   (new — moved from server/static/)

gixen-cli/ (files removed or modified)
├── server/
│   ├── main.py                  (decontaminated)
│   ├── db.py                    (5 comic functions removed)
│   ├── comic_routes.py          (DELETED)
│   ├── title_parser.py          (DELETED)
│   └── static/
│       ├── index.html           (comic JS stripped)
│       └── v2-comics.html       (DELETED)
├── cli.py                       (11 comic flags stripped from add/edit)
└── tests/
    ├── test_server_api.py       (updated)
    ├── test_server_db.py        (updated — remove comic function tests)
    └── test_comic_routes.py     (DELETED)
```

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification.*

```
Request: GET /v2/comics
  → register_routes wired → app.include_router(comic_router)
  → gixen_overlay/routes.py GET /v2/comics
  → FileResponse(Path(__file__).parent / "static" / "v2-comics.html")

Request: POST /api/extract-comics
  → gixen_overlay/routes.py handler
  → gixen_overlay/db.py: upsert_comic(), link_comic_to_bid()
  → server/db.py: get_bid_by_item_id()  [generic — still in core]
  → request.app.state.db  [shared connection from lifespan]

Lifespan startup:
  1. init_db() → creates bids table only (no comics/bid_comics)
  2. _invoke_db_tables_isolated(pm, _db)
     → gixen_overlay.register_db_tables(conn)
     → CREATE TABLE IF NOT EXISTS comics (...)
     → CREATE TABLE IF NOT EXISTS bid_comics (...)
  3. _invoke_register_routes(pm, app)
     → gixen_overlay.register_routes(app)
     → app.include_router(comic_router)  [5 routes registered]
  4. _collect_dashboard_tabs(pm)
     → gixen_overlay.register_dashboard_tabs()
     → [{"label": "comics", "path": "/v2/comics"}]
```

## Implementation Units

- [ ] **Unit 1: Plugin DB Layer**

**Goal:** Create `gixen_overlay/db.py` with all comic-specific DB functions. Wire `register_db_tables` to create `comics` and `bid_comics` tables.

**Requirements:** R3, R6

**Dependencies:** None (can start immediately; plugin scaffold exists)

**Files (comic-pipeline repo):**
- Create: `plugins/gixen-overlay/src/gixen_overlay/db.py`
- Modify: `plugins/gixen-overlay/src/gixen_overlay/plugin.py`
- Create: `plugins/gixen-overlay/tests/test_gixen_overlay_db.py`

**Approach:**
- Copy `upsert_comic`, `link_comic_to_bid`, `get_comics_for_bid`, `get_primary_comic_for_bid`, `list_comics` verbatim from `server/db.py` — zero behavior change
- Use `_COMICS_DDL` and `_BID_COMICS_DDL` from `tests/test_server_db.py` as the authoritative DDL for `register_db_tables`
- Include `CREATE INDEX IF NOT EXISTS idx_bid_comics_bid ON bid_comics(bid_id)` from the test fixture
- `register_db_tables` must use `conn.execute()` only — never `conn.executescript()` (hookspec requirement)
- `plugin.py` `register_db_tables(conn)` calls `create_tables(conn)` from `gixen_overlay.db`

**Patterns to follow:**
- `_make_comic_schema_plugin()` in `tests/test_server_api.py` — exact DDL
- `_COMICS_DDL` / `_BID_COMICS_DDL` in `tests/test_server_db.py`
- `test_plugin_creates_table` in `tests/test_plugin_integration.py` — fixture pattern for testing table creation

**Test scenarios:**
- Happy path: `register_db_tables(conn)` creates `comics` and `bid_comics` tables on a fresh SQLite connection
- Happy path: `register_db_tables(conn)` is idempotent (`CREATE TABLE IF NOT EXISTS` on existing tables)
- Happy path: `upsert_comic()` inserts new record, returns id
- Happy path: `upsert_comic()` on duplicate `(title, issue, year, grade)` updates existing record (ON CONFLICT DO UPDATE)
- Happy path: `link_comic_to_bid()` populates `bid_comics` and sets `bids.comic_id` for primary
- Happy path: `list_comics()` returns all comics; filters by title/issue/year/grade when provided
- Edge case: `upsert_comic()` with `grade=None` (nullable) — unique constraint uses `COALESCE`
- Integration: `register_db_tables` table creation verified via `sqlite_master` query after hook fires

**Verification:**
- `pytest plugins/gixen-overlay/tests/test_gixen_overlay_db.py` passes
- `comics` and `bid_comics` tables appear in `sqlite_master` after `register_db_tables` fires

---

- [ ] **Unit 2: Plugin Routes, Models, title_parser, and Static Asset**

**Goal:** Move `comic_routes.py`, `title_parser.py`, `v2-comics.html`, and Pydantic models into the plugin. Wire `register_routes` and `register_dashboard_tabs`.

**Requirements:** R3, R4, R5, R6

**Dependencies:** Unit 1 (db functions must exist before routes import them)

**Files (comic-pipeline repo):**
- Create: `plugins/gixen-overlay/src/gixen_overlay/routes.py`
- Create: `plugins/gixen-overlay/src/gixen_overlay/models.py`
- Create: `plugins/gixen-overlay/src/gixen_overlay/title_parser.py`
- Create: `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html`
- Modify: `plugins/gixen-overlay/src/gixen_overlay/plugin.py`
- Create: `plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`

**Approach:**
- `models.py`: move `UpsertComicRequest` and `LocgLinkRequest` verbatim from `server/comic_routes.py`
- `title_parser.py`: copy `server/title_parser.py` verbatim — no imports to update
- `routes.py`: copy `server/comic_routes.py`, update imports:
  - `from server.db import ...` → `from gixen_overlay.db import ...` (for comic functions)
  - `from server.db import get_bid_by_item_id` stays as-is (generic function, still in core)
  - `from server.title_parser import parse_title` → `from gixen_overlay.title_parser import parse_title`
  - Models import from `gixen_overlay.models`
  - Static path: `Path(__file__).parent / "static" / "v2-comics.html"`
- `static/v2-comics.html`: copy from `server/static/v2-comics.html`, then update to fetch comic data from `GET /api/comics` instead of relying on comic columns in `/api/snipes`. The updated JS should: (1) fetch `/api/snipes` for bid data including opaque `comic_id`, (2) fetch `/api/comics` for comic details, (3) correlate client-side by `comic_id`
- `plugin.py` `register_routes(app)`: `app.include_router(router)` from `gixen_overlay.routes`
- `plugin.py` `register_dashboard_tabs()`: return `[{"label": "comics", "path": "/v2/comics"}]`

**Patterns to follow:**
- `server/comic_routes.py` — source file, copy with import updates
- `test_plugin_registers_route` in `tests/test_plugin_integration.py` — route registration test pattern
- `test_plugin_dashboard_tabs_collected` in `tests/test_plugin_integration.py` — tabs test pattern

**Test scenarios:**
- Happy path: `GET /v2/comics` returns 200 when plugin is installed
- Happy path: `GET /api/comics` returns empty list on fresh DB
- Happy path: `POST /api/comics` creates a comic and returns it with `id`
- Happy path: `POST /api/comics` twice with same `(title, issue, year, grade)` upserts (second call updates FMV, same `id` returned)
- Happy path: `POST /api/extract-comics` with a bid whose `ebay_title` parses to a known pattern links it and returns `linked: 1`
- Happy path: `POST /api/extract-comics` re-run is idempotent (already-linked bid: `processed: 0`)
- Happy path: `POST /api/bids/{item_id}/comics/locg` with a valid item_id that has a `comic_id` sets `locg_id`
- Error path: `POST /api/comics` missing `year` returns 422
- Error path: `POST /api/bids/{item_id}/comics/locg` for non-existent item returns 404
- Integration: `GET /api/dashboard-tabs` returns `[{"label": "comics", "path": "/v2/comics"}]` when plugin is loaded via lifespan

**Verification:**
- `pytest plugins/gixen-overlay/tests/` passes
- `GET /api/dashboard-tabs` returns expected tab when plugin is installed in integration test

---

- [ ] **Unit 3: Decontaminate `server/db.py`**

**Goal:** Remove the 5 comic-specific functions from `server/db.py`. Update imports in `server/main.py`.

**Requirements:** R2

**Dependencies:** Unit 1 (plugin DB layer must exist before removing from core)

**Files (gixen-cli repo):**
- Modify: `server/db.py`
- Modify: `server/main.py` (remove `upsert_comic` from import)
- Delete: `server/comic_routes.py`
- Delete: `server/title_parser.py`

**Approach:**
- Remove from `server/db.py`: `upsert_comic`, `link_comic_to_bid`, `get_comics_for_bid`, `get_primary_comic_for_bid`, `list_comics`
- Remove `upsert_comic` from the `from server.db import ...` line in `server/main.py`
- Remove `from server.comic_routes import router as comic_router` and `app.include_router(comic_router)` from `server/main.py`
- Delete `server/comic_routes.py` and `server/title_parser.py`
- Delete `server/static/v2-comics.html`

**Patterns to follow:**
- `server/db.py` existing structure — keep `insert_bid(conn, ..., comic_id: int | None)` and `get_bid_by_item_id` intact

**Test scenarios:**
- Integration: `pytest tests/test_server_db.py` passes after removing comic functions (the `upsert_comic` and `link_comic_to_bid` tests in `test_server_db.py` should also be removed in Unit 6)
- Integration: `from server.db import upsert_comic` raises `ImportError` (verifying removal)
- Regression: `pytest tests/test_route_organization.py` passes (route count will change — update expected count)

**Verification:**
- `server/comic_routes.py`, `server/title_parser.py`, `server/static/v2-comics.html` no longer exist
- `grep -r "upsert_comic\|link_comic_to_bid\|list_comics" server/db.py` returns nothing

---

- [ ] **Unit 4: Decontaminate `server/main.py`**

**Goal:** Strip all comic contamination from `AddBidRequest`, `EditBidRequest`, `api_add_bid`, `api_edit_bid`, and the three generic list endpoints.

**Requirements:** R1, R2

**Dependencies:** Unit 3 (comic routes and db functions removed)

**Files (gixen-cli repo):**
- Modify: `server/main.py`
- Modify: `tests/test_server_api.py`

**Approach:**
- `AddBidRequest`: remove 11 comic fields (`comic`, `issue`, `year`, `grade`, `fmv_low`, `fmv_high`, `fmv_comps`, `fmv_confidence`, `fmv_notes`, `locg_id`, `locg_variant_id`) and the `fmv_confidence` field validator
- `EditBidRequest`: remove `locg_id` and `locg_variant_id`
- `api_add_bid`: remove the `comic_id = None` block and the `upsert_comic(...)` call; pass `comic_id=None` to `insert_bid` always (or remove the param if `insert_bid` is updated — keep it for now, plugin still needs it)
- `api_edit_bid`: remove the `locg_id / locg_variant_id` block (lines 1025–1039)
- `GET /api/snipes`: replace `LEFT JOIN comics c ON b.comic_id = c.id` with plain `FROM bids b`; remove all `c.*` columns from SELECT; remove the second `bid_comics` join query and `comics_by_bid` dict; strip 12 comic fields from the response dict; strip `"comics"` list field; keep `"comic_id": item.get("comic_id")` — it is an opaque int that clients may use to correlate with the plugin's `GET /api/comics`
- `GET /api/history`: same decontamination as `/api/snipes` — remove JOIN, remove comic columns from response, keep `comic_id`
- `GET /api/bids`: remove JOIN, remove comic columns from response, keep `comic_id`
- Title fallback: `title = item.get("ebay_title") or ""` (remove `or item.get("comic_title")`)

**Patterns to follow:**
- Remaining clean `api_remove_bid` and `api_sync` handlers as style reference

**Test scenarios:**
- Happy path: `POST /api/bids` with only `item_id` and `max_bid` succeeds (no comic fields)
- Error path: `POST /api/bids` with comic fields (`comic`, `issue`) returns 422 (extra fields rejected) — OR they are silently ignored depending on Pydantic `model_config`. Choose: use `model_config = ConfigDict(extra="ignore")` to avoid breaking callers; document the decision
- Happy path: `GET /api/snipes` response does NOT include `comic_title`, `fmv_low`, `fmv_high`, etc.
- Happy path: `GET /api/snipes` response DOES include `comic_id` (opaque int, may be null)
- Happy path: `GET /api/history` response matches same shape
- Happy path: `GET /api/bids` response matches same shape
- Regression: `pytest tests/test_server_api.py` passes — update any tests asserting comic fields in snipes responses

**Verification:**
- `grep "comic_title\|fmv_low\|comic_grade\|locg_id" server/main.py` returns nothing
- `pytest tests/test_server_api.py` passes

---

- [ ] **Unit 5: Decontaminate `server/static/index.html` and `cli.py`**

**Goal:** Strip comic JS from the snipes dashboard. Remove dead comic CLI flags from `cli.py add` and `cli.py edit`.

**Requirements:** R2

**Dependencies:** Unit 4 (API no longer returns comic columns)

**Files (gixen-cli repo):**
- Modify: `server/static/index.html`
- Modify: `cli.py`

**Approach:**
- `index.html`: remove `fmtFmv(r)` function, `gradeLabel(g)` function, `displayCondition(r)` function (replaces with empty string or just remove the cell); update `displayTitle(r)` to return only `ebay_title` or `item_id`; remove `fmv` column header and cell from both active and ended snipes tables; strip `fmv_low`, `fmv_high`, `fmv_confidence` from demo mock data
- `cli.py`: remove `--comic`, `--issue`, `--year`, `--grade`, `--fmv-low`, `--fmv-high`, `--fmv-comps`, `--fmv-confidence`, `--fmv-notes`, `--locg-id`, `--locg-variant-id` from `add` command; remove `--locg-id`, `--locg-variant-id` from `edit` command; keep `locg` subcommand group and `extract-comics` command unchanged (they target plugin routes)

**Patterns to follow:**
- Remaining non-comic columns in `index.html` tables as style reference

**Test scenarios:**
- Test expectation: none for `index.html` changes — visual/functional verification only
- Happy path: `python cli.py add --help` no longer lists `--comic`, `--grade` flags
- Regression: `pytest tests/test_server_api.py` still passes after cli.py change

**Verification:**
- `grep "gradeLabel\|fmtFmv\|displayCondition\|comic_grade\|fmv_low" server/static/index.html` returns nothing
- `grep "\-\-comic\|\-\-grade\|\-\-fmv" cli.py` returns nothing (in `add`/`edit` command defs)

---

- [ ] **Unit 6: Update gixen-cli Tests**

**Goal:** Remove deleted modules' tests. Update remaining tests to match decontaminated API shapes.

**Requirements:** R1

**Dependencies:** Units 3–5

**Files (gixen-cli repo):**
- Delete: `tests/test_comic_routes.py`
- Delete: `tests/test_title_parser.py`
- Modify: `tests/test_server_db.py`
- Modify: `tests/test_server_api.py`
- Modify: `tests/test_route_organization.py`

**Approach:**
- `test_server_db.py`: remove tests for `upsert_comic`, `link_comic_to_bid`, `get_comics_for_bid`, `get_primary_comic_for_bid`, `list_comics`; remove `db_with_comics` fixture and `_COMICS_DDL`/`_BID_COMICS_DDL` constants; keep `test_init_creates_tables` assertion that `comics` not in tables (already passes after PER-27)
- `test_server_api.py`: keep the `_make_comic_schema_plugin()` fixture (needed for `api` fixture to create comic tables for comic route tests that still run — wait, those tests test the comic routes which no longer exist in gixen-cli). After Unit 3, the `test_extract_comics_*`, `test_upsert_comic_*`, `test_add_bid_with_comic_links_fmv`, `test_locg_link_*` tests reference routes that no longer exist. These tests must be **removed** from gixen-cli (they migrate to the plugin repo's test suite in Unit 2). `_make_comic_schema_plugin()` can be removed too once no test depends on it. Keep only the generic tests: `test_health`, `test_add_bid_no_comic`, `test_add_bid_invalid_item_id`, etc.
- `test_route_organization.py`: update expected route count to reflect removed comic routes

**Test scenarios:**
- Integration: `pytest tests/` passes with zero failures and zero skips after all removals
- Regression: `test_add_bid_no_comic` still passes (core bid flow unaffected)
- Regression: `test_health` still passes

**Verification:**
- `pytest tests/` passes — 0 failures
- No remaining test imports `from server.comic_routes import ...` or `from server.title_parser import ...`

---

## System-Wide Impact

- **Interaction graph:** `app.include_router(comic_router)` at module scope is removed; plugin routes are registered during lifespan via `_invoke_register_routes`. This means comic routes are absent from the OpenAPI schema until after lifespan startup.
- **API surface parity:** `v2-comics.html` depends on comic columns in `/api/snipes`; after decontamination it must fetch from `GET /api/comics` instead. This is coordinated within Unit 2.
- **Opaque `comic_id` in responses:** `/api/snipes`, `/api/history`, `/api/bids` still return `"comic_id"` as a nullable int. Clients (like `v2-comics.html`) can use it to correlate with `GET /api/comics`.
- **Breaking API change:** callers that read `comic_title`, `comic_grade`, `fmv_low`, etc. from `/api/snipes` will get `null` / missing fields after this change. This is intentional and expected — the feature was already plugin-gated by PER-25–PER-29.
- **Integration coverage:** Plugin tests (Unit 2) use `TestClient` with `lifespan=True` to verify the full hook dispatch chain, not just isolated function calls.
- **Unchanged invariants:** Core `bids` table schema, `insert_bid` signature, `GET /api/snipes` route path and generic fields (item_id, max_bid, status, etc.) are unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `v2-comics.html` breakage (relies on comic columns in `/api/snipes`) | Unit 2 updates the HTML to fetch from `GET /api/comics` before Unit 3 deletes the columns |
| Plugin circular import (`gixen_overlay` imports `server.db`) | `gixen_overlay` imports only generic functions from `server.db`; no import back from `server` into `gixen_overlay` |
| Test suite shrinks significantly; gaps introduced | Unit 6 explicitly audits and migrates all removed tests to the plugin repo |
| `test_route_organization.py` count assertion breaks | Expected count updated in Unit 6 |
| `AddBidRequest` extra fields behavior | Decide: `model_config = ConfigDict(extra="ignore")` to avoid breaking existing callers vs. strict 422. Document choice. |

## Documentation / Operational Notes

- Update `CLAUDE.md` in gixen-cli: remove comic route mentions from Architecture section; update test command list
- After this PR merges, run `pip install -e .` then `pip install -e ~/Projects/comic-pipeline/plugins/gixen-overlay` to verify the plugin loads cleanly
- `docs/refactor-split-handoff.md` should be updated to mark PER-30 items as extracted

## Sources & References

- Origin doc: `docs/refactor-split-handoff.md`
- Prior plans: `docs/plans/2026-05-18-001-*`, `docs/plans/2026-05-18-002-*`, `docs/plans/2026-05-18-003-*`
- Plugin stub: `plugins/gixen-overlay/src/gixen_overlay/plugin.py` (comic-pipeline repo)
- Comic DB DDL: `tests/test_server_db.py` lines 18–45
- Comic stub fixture: `tests/test_server_api.py` lines 25–74
