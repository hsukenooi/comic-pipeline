---
date: 2026-06-03
topic: bui-87-collection-server-api
---

# Serve Collection + Wish-List From the Server API (BUI-87)

## Summary

Make the comic collection and wish-list a single server-authoritative source on the Mac Mini gixen server, served over `/api/comics/*`. The overlay plugin calls the existing `locg-cli` matching logic against canonical JSON held server-side; `/comic:collection-check` and `/comic:seller-scan` are rewired to the API, and the repo's `data/locg/` is retired as the source of truth. Both machines see the same collection instantly, with no git churn and no worktree coupling.

## Problem Frame

BUI-84 versioned the collection cache in git (`data/locg/`, PR #47). That buys *availability* but not a *live* cross-machine source of truth: git only converges on commit → push → pull. Multi-machine use is now confirmed (MacBook + Mac Mini), which triggers BUI-84's documented residuals — a ~1.8 MB `collection.json` rewrite churns git on every import, and checkouts diverge until someone pulls.

The buying workflow already depends on the Mac Mini server for FMV (`comic-fmv`) and snipe-add (`gixen`). The collection is the one piece *not* on the server, and that asymmetry is the smell: a `collection-check` run on the MacBook can disagree with the Mac Mini until a git sync reconciles them. The cost lands exactly when it's most expensive — a stale or divergent "not owned" answer buys a duplicate.

## Key Decisions

- **Wrap the existing `locg-cli` logic; do not build collection DB tables.** The hard part of `collection-check` is the series-name matching — `_normalize_series_key`, the `series_name_index`, leading-article stripping, and the BUI-46 alias fallback. That logic is accumulated correctness (four documented bug fixes live in it). And it's not just reads: *every* collection/wish-list operation already exists in `locg-cli` against JSON — the read matcher, plus all three writes (full import, `record-win` append, wish-list append). The overlay declares `locg-cli` as a workspace dependency and exposes endpoints that call the existing functions against canonical JSON on the server, rather than porting any of it to SQL. No schema, no migration, no re-derivation of solved bugs. A survey of all `/comic:*` skills confirmed **zero of them need a relational JOIN** between the collection/wish-list and the bids/comics/fmv tables — the only thing a DB would buy over a flat snapshot is unused.

- **Server-authoritative now (Phase B); defer offline triage (Phase C).** A full buy already needs the server, so an offline read-through cache is marginal. Build the live source of truth first; add the offline fallback later only if disconnected triage actually annoys in practice.

- **Provider-neutral names on the new surface; keep `locg-cli` as-is.** The served data is a *collection* and a *wish-list*, not "the locg collection" — LOCG is one import source, not the data's identity. New endpoints use neutral names (`/api/comics/collection/check`, `/api/comics/wish-list`), never `/api/comics/locg/*`. The `locg-cli` package and `locg` console script keep their names: they genuinely are the League of Comic Geeks integration (Playwright login, XLSX import, the `record-win → CSV → LOCG Bulk Import` round-trip), and renaming carries broad churn for no accuracy gain.

- **The endpoints live in the overlay plugin, not gixen-cli core.** Comic-specific coupling stays in `plugins/gixen-overlay`, consistent with where the existing `/api/comics/*` endpoints and comic tables live. The overlay already imports host internals; adding a `locg-cli` workspace dependency follows the same pattern.

- **No new auth.** The new endpoints match the existing server's exposure — Tailscale-only, no API key — consistent with the FMV and snipe endpoints already served from the Mac Mini.

## Requirements

**Server source of truth**

- R1. The Mac Mini server holds a canonical copy of the collection and wish-list at a server-owned location (replacing `data/locg/` as the authority). The path is not named for LOCG.
- R2. The server exposes a collection-ownership check over `/api/comics/*` that returns the same verdict shape `collection-check` relies on today (match status, matched title, cache age).
- R3. The server exposes the wish-list over `/api/comics/*` for `seller-scan` to match against.
- R4. The read and write endpoints call the existing `locg-cli` logic — the read matcher (series-key normalization, leading-article stripping, year gating, alias fallback) and the write operations (import replace, `record-win` append, wish-list append) all behave identically to the current local `locg` commands.

**Writes to the server**

- R5. The full collection/wish-list import stays local and interactive (it needs a real-Chrome Playwright login to League of Comic Geeks). The import result is sent to the server rather than written into the repo.
- R6. `/comic:collection-add` (`record-win`) appends won auctions to the collection *on the server*, not to a local file. The Metron series resolution and batch behavior are unchanged.
- R7. `/comic:wishlist-add` appends issues to the wish-list *on the server*, not to a local file. The BUI-47 semantics carry over: server-side wish-list appends are still overwritten by the next full import unless exported to LOCG first.
- R8. After any write (import, `record-win`, or wish-list append) lands on the server, both machines see the change on their next API read — no git commit, push, or pull required.

**Rewiring the consumers**

- R9. `/comic:collection-check` queries the API instead of reading `data/locg/*.json`.
- R10. `/comic:seller-scan` matches wish-list entries via the API instead of reading `data/locg/*.json`. Note that the matcher lives in the standalone `apps/ebay` app (`src/seller_scan.py`), which is *not* a uv workspace member and cannot import `locg-cli` — it must fetch the wish-list from the API over HTTP, a heavier change than rewiring `collection-check`.
- R11. When the server is unreachable, `collection-check` fails loudly with a clear connection error. It must never silently return "not owned" — a silent miss buys a duplicate. (Phase C later replaces this hard-fail with a cached fallback.)

**Retiring the repo cache**

- R12. `data/locg/` is retired as the source of truth. Whatever remains in the repo is no longer authoritative and no longer the path the consumers read.
- R13. The `locg-cli` package and `locg` console script are not renamed.

## Key Flows

- F1. Cross-machine ownership check
  - **Trigger:** A buy/triage run calls `collection-check` for a candidate comic, from either machine.
  - **Steps:** Consumer calls the collection-check endpoint with series/issue/(year/variant) → server runs the existing matching logic against its canonical collection → returns ownership verdict + cache age → consumer renders the same table it does today.
  - **Outcome:** Both machines get the same answer from one authority; no divergence window.
  - **Covered by:** R2, R4, R9

- F2. Writes → server publish
  - **Trigger:** Any of the three write paths fires — a full interactive LOCG import, a `record-win` after winning auctions, or a `wishlist-add`.
  - **Steps:** The operation runs its existing local logic (Playwright import / Metron resolution / issue parsing) → the result is sent to the server → server applies it to its canonical copy using the same `locg-cli` write functions, serialized server-side.
  - **Outcome:** Next API read on either machine reflects the write; no git round-trip.
  - **Covered by:** R5, R6, R7, R8

- F3. Offline check (degraded, pre-Phase C)
  - **Trigger:** `collection-check` runs on a machine that can't reach the server.
  - **Steps:** API call fails → consumer surfaces a clear connection error and stops.
  - **Outcome:** No verdict is rendered; the user is told the check could not run, rather than being given a false "not owned."
  - **Covered by:** R11

## Acceptance Examples

- AE1. Owned comic, both machines
  - **Covers R2, R4, R9.**
  - **Given:** A comic the user owns, with the leading-article / alias edge cases that the local matcher handles today.
  - **When:** `collection-check` runs from the MacBook and from the Mac Mini against the server.
  - **Then:** Both return "in collection" with the same matched title — matching parity with the current local `locg collection check`.

- AE2. A write is visible cross-machine without git
  - **Covers R6, R8.**
  - **Given:** A `record-win` on one machine appends a just-won issue to the collection on the server.
  - **When:** `collection-check` is then run on the *other* machine for that issue, with no git pull in between.
  - **Then:** The other machine reports it as owned. (Same property holds for a full import and for a wish-list append.)

- AE3. Server down does not buy a dupe
  - **Covers R11.**
  - **Given:** The server is unreachable.
  - **When:** `collection-check` runs.
  - **Then:** It reports a connection failure and renders no ownership verdict — it does not report "not owned."

## Scope Boundaries

**Deferred for later**

- Phase C local read-through cache: mirror each API response to a local file and fall back to it on connection error with a "using cached collection from \<date\>" warning. Revisit only if offline triage proves annoying.
- `ids.json` (the derived LOCG lookup-ID cache) handling — move it server-side vs leave it local/gitignored. Pure derived cache; resolve during planning, not load-bearing for B.

**Outside this change's identity**

- Renaming the `locg-cli` package or `locg` console script. The new server surface gets neutral names; the existing LOCG client keeps its accurate name.
- Collection DB tables, or normalizing the collection against the `comics` fact table for SQL JOINs. No requirement needs ownership joined against bids/FMV; the collection is a different population than the FMV fact table.
- A new auth layer for the endpoints. They inherit the existing server's Tailscale-only exposure.

## Dependencies / Assumptions

- The gixen server is a uv workspace member alongside `locg-cli`, so the overlay can take a workspace dependency on `locg-cli` and import its read/write functions directly (same pattern as the existing overlay → gixen-cli coupling).
- `apps/ebay` (home of `seller_scan.py`) is **not** a workspace member — apps are `uv tool install`-managed and shell out on PATH. So `seller-scan` can't import `locg-cli`; rewiring it (R10) means adding an HTTP fetch of the wish-list to the standalone app, which is a larger change than the in-process rewire of `collection-check`.
- Writes are serialized server-side: the server is a single process and `CollectionCache` already does atomic flock + tempfile-rename, so concurrent appends from both machines are safe at this volume without a DB.
- The server-side canonical store is writable by the server process on the Mac Mini; the existing `LOCG_DATA_DIR` resolution gives a seam for pointing the matcher at a server-owned path.
- Endpoints are reachable from the MacBook over Tailscale (`mac-mini.tail9b7fa5.ts.net`), matching the existing FMV/snipe endpoints.
- Re-parsing the ~1.8 MB collection per request is acceptable at solo-user scale; if it isn't, an in-memory cache on the server is an internal optimization, not a scope change.

## Outstanding Questions

**Deferred to planning**

- Exact transport for the import publish (file upload vs structured POST) and the server write path / naming.
- Whether `ids.json` moves server-side or stays local.
- Whether to add an in-memory parse cache on the server, and its invalidation on import.

## Sources / Research

- BUI-87 (tracking issue) — supersedes the BUI-84 storage approach (cache → repo `data/locg/`, PR #47).
- `packages/locg-cli/src/locg/commands.py` (`cmd_collection_check`), `collection_cache.py` (`_normalize_series_key`, `series_name_index`), `config.py` (`_cache_dir` resolution) — the matching logic to be reused.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py`, `db.py`, `plugin.py` — existing `/api/comics/*` surface and the `register_routes` / `register_db_tables` hookspecs.
- `.claude/commands/comic/collection-check.md`, `seller-scan.md` — the read consumers to rewire; `collection-add.md` (`record-win`), `wishlist-add.md` — the write paths to repoint at the server.
- `apps/ebay/src/seller_scan.py` — the standalone (non-workspace) wish-list matcher that must fetch over HTTP.
- Matching-correctness history (the bugs Option 1 preserves): GSFF false positive, leading-article false negative (17 owned Hulks), ASM series-name ambiguity, BUI-46 alias fallback.
