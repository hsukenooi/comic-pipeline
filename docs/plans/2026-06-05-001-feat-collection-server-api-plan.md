---
title: "feat: Serve collection + wish-list from the server API (BUI-87 project)"
type: feat
status: completed
date: 2026-06-05
issue: BUI-91, BUI-92, BUI-88, BUI-89, BUI-93
brainstorm: docs/brainstorms/2026-06-03-bui-87-collection-server-api-requirements.md
---

# feat: Serve collection + wish-list from the server API (BUI-87 project)

## Summary

Implements the BUI-87 project (sub-issues BUI-88..BUI-93): move the comic
collection and wish-list off the per-checkout `data/locg/` cache and onto the
Mac Mini gixen server, served over `/api/comics/*`. The gixen-overlay plugin
takes a workspace dependency on `locg-cli` and exposes endpoints that call the
existing `locg-cli` functions against a server-owned canonical JSON store; the
`/comic:*` consumers are rewired to the API; `data/locg/` is retired as the
source of truth.

This plan is the implementation companion to the recovered requirements
brainstorm. It resolves that doc's three "deferred to planning" questions and
lays out the concrete endpoint surface, consumer rewires, and retirement steps.

## Resolved open questions (from the brainstorm's "Outstanding Questions")

1. **Import transport → multipart XLSX file upload.** The local side keeps doing
   the interactive Playwright login + XLSX download, then POSTs the *file* to
   `POST /api/comics/collection/import`; the server saves it to a temp path and
   calls the existing `cmd_collection_import(path)`. Rationale: `import_xlsx`
   performs a full merge (agent_win reconciliation, identity-tuple upsert, rename
   detection, wish-list rebuild). Re-implementing that from structured JSON rows
   would duplicate ~200 lines of accumulated, tested logic. Uploading the file
   the server already knows how to import reuses it exactly. record-win and
   wish-list-add use small structured JSON bodies (no merge complexity, so no
   reuse argument for a file).

2. **`ids.json` stays local / gitignored.** Confirmed by code: `ids.json`
   (the `IDCache` in `cache.py`) is written/read *only* by `cmd_lookup` (the
   online LOCG-website ID resolver). None of the three local-first operations we
   wrap — `cmd_collection_check`, `cmd_collection_record_win`, `cmd_wish_list_add`
   — import or use it. It is a pure derived cache; it does not move server-side.

3. **No in-memory parse cache (yet).** Per the brainstorm's own assumption,
   re-parsing the ~1.8 MB `collection.json` per request is acceptable at
   solo-user scale. Read fresh each request. An in-memory cache + import-time
   invalidation is an internal optimization to add later only if latency bites.

## Architecture

### Server-owned canonical store (R1)

`locg.config._cache_dir()` resolves `LOCG_DATA_DIR` env → `<repo>/data/locg` →
`~/.cache/locg`. On the Mac Mini the editable checkout would otherwise resolve to
the repo's `data/locg/` — the very location we're retiring as authority. So the
overlay routes call a helper `_ensure_collection_store()` that, when
`LOCG_DATA_DIR` is unset, points it at a **server-owned, provider-neutral**
directory derived from the gixen DB path: `<dir(DB_PATH)>/collection-store`
(default `~/.gixen-server/collection-store`). The directory name is not "locg"
(R1: "the path is not named for LOCG"). An explicitly-set `LOCG_DATA_DIR` always
wins (tests set it to a temp dir). The helper is called at the top of every
collection/wish-list endpoint so the store is server-owned regardless of launch
env.

### Endpoint surface (provider-neutral names — R-decision)

Reads (BUI-91):
| Method | Path | Wraps | Returns |
| --- | --- | --- | --- |
| GET | `/api/comics/collection/check` | `cmd_collection_check(series, issue, variant?, year?)` | `{match_status, full_title_matched, cache_age_days}` |
| GET | `/api/comics/collection/status` | `cmd_collection_status()` | status metrics dict |
| GET | `/api/comics/wish-list` | `cmd_wish_list_from_cache(title?)` | `[{name, id, ...}]` |
| GET | `/api/comics/collection/export` | `cmd_collection_export(tmp)` | `{csv, notes_md, ready_count, ...}` |

Writes (BUI-92):
| Method | Path | Wraps | Body |
| --- | --- | --- | --- |
| POST | `/api/comics/collection/import` | `cmd_collection_import(tmp_xlsx)` | multipart file |
| POST | `/api/comics/collection/record-win` | `cmd_collection_record_win(wins)` | JSON `{wins: [...]}` |
| POST | `/api/comics/wish-list` | `cmd_wish_list_add(title)` | JSON `{title}` |

`status` and `export` are read endpoints the consumer rewires need (the
collection-check bootstrap guard reads status; the collection-add round-trip
reads export). They are reads, so they live with BUI-91; they're a deliberate,
documented addition slightly beyond the strict R1–R4 wording, justified by R9/R6
consumer parity.

### Error mapping (the R11 stakes live here)

- `cmd_collection_check` never raises for a missing cache (returns
  `not_in_cache` + `cache_age_days: None`). `CollectionCache.load()` can raise
  `RuntimeError` on corrupt/crashed/newer-schema state → map to HTTP 500. A 500
  or a connection failure must surface to the consumer as a hard error, never a
  silent "not owned" (R11/AE3) — enforced consumer-side by `curl -sf`.
- `cmd_wish_list_from_cache` raises `FileNotFoundError` when the wish-list was
  never imported → return `[]` (an empty wish-list is a correct, non-dangerous
  answer for seller-scan; a miss only fails to surface a wanted book, it cannot
  buy a dupe).
- Input validation via FastAPI required query params + `HTTPException(422)`.

## Issue breakdown & sequencing

- **BUI-91 — read endpoints** (R1–R4): add `locg` workspace dep to the overlay;
  `_ensure_collection_store()`; the four GET routes; Pydantic/response handling;
  add `cmd_collection_check` import to the workspace-imports canary; tests.
- **BUI-92 — write endpoints** (R5–R8): the three POST routes (import upload,
  record-win, wish-list-add); tests. Depends on BUI-91's dep + store helper.
- **BUI-88 — rewire reads** (R9–R11): `collection-check.md` → curl the check +
  status endpoints with `GIXEN_SERVER_URL` + health gate + `-sf` hard-fail;
  `apps/ebay/src/seller_scan.py` `fetch_wish_list()` → HTTP GET (requests, loud
  failure); `seller-scan.md` prose. Depends on BUI-91.
- **BUI-89 — rewire writes** (R6–R7 consumer side): `collection-add.md`
  (record-win → POST, export → GET) and `wishlist-add.md` (add → POST). Depends
  on BUI-92.
- **BUI-93 — retire `data/locg`** (R12–R13): `git rm --cached data/locg`, add to
  `.gitignore` (kills the 1.8 MB churn; keeps local files), document the
  one-time server-store seed, update `CLAUDE.md` + docs. `locg` package/console
  script unchanged. `ids.json` stays local.
- **BUI-90 — DEFERRED** (Phase C offline cache). Not built.

## Testing

Per-package, locally (CI only AST-parses). Overlay endpoint tests mirror the
existing `api` TestClient fixture in
`plugins/gixen-overlay/tests/test_gixen_overlay_routes.py`, setting
`LOCG_DATA_DIR` to a temp dir and seeding a small `collection.json` /
`wish-list.json`. The R11 hard-fail gets an explicit test asserting the consumer
path treats an unreachable server / non-200 as an error, never `not_in_cache`.

## Out of scope

- Renaming the `locg-cli` package or `locg` console script (R13).
- New auth (endpoints inherit the server's Tailscale-only exposure).
- Collection DB tables / normalization against the `comics` fact table.
- The Phase C offline read-through cache (BUI-90).
