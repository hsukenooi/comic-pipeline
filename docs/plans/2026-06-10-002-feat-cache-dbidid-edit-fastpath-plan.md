---
title: "feat: Cache Gixen dbidid to Remove the List Round-Trip From Edits"
type: feat
status: active
date: 2026-06-10
issue: BUI-116
depends_on: BUI-115
---

# feat: Cache Gixen dbidid for the Edit Fast-Path (BUI-116)

## Summary

`modify_snipe` / `remove_snipe` need Gixen's internal `dbidid`, which today forces a `list_snipes()` GET on every edit just to resolve it — the single most failure-prone call in the edit path (see BUI-115). We already pull `dbidid` into memory on every sync. This persists it on the `bids` row and lets edit/remove POST directly with the cached id, falling back to the list lookup only on a cache miss or a stale-id failure. BUI-115's verify-after-modify is the safety net that makes using a possibly-stale cached id safe.

Scope: persist `dbidid`, use it on the happy path, fall back cleanly. No change to the `bids.status` lifecycle or the BUI-115 recovery/verify logic.

---

## Problem Frame

`modify_snipe` (and `remove_snipe`) call `self.list_snipes()` → `_get_home_page()` (a GET) purely to find the snipe's `dbidid` before POSTing. That GET is the dominant edit failure surface. The `dbidid` is already present in every snipe dict the sync loop iterates (`server/main.py` `_sync_gixen`, passed to `cache_gixen_data`), so it can be persisted at no extra fetch cost and read back on the next edit.

**Staleness risk:** a `dbidid` can change if a snipe is removed and re-added on Gixen. A cached-but-stale id would make the modify POST target a wrong/nonexistent row. BUI-115's post-POST verification catches this (the new `max_bid` won't be live), which is the trigger for the fallback to a fresh list lookup.

---

## Requirements

- **R1** — `dbidid` is persisted per `bids` row during sync and survives restarts (schema column + migration).
- **R2** — `modify_snipe` / `remove_snipe` accept an optional `dbidid` that, when supplied, skips the pre-POST `list_snipes()` lookup. The BUI-115 post-POST verify still runs.
- **R3** — On a cache miss (NULL `dbidid`) the edit path behaves exactly as today (list-based lookup). On a cached-id failure (unconfirmed modify / still-present remove), the server falls back to the list-based path with a fresh id and clears the stale cache.
- **R4** — No regression to BUI-115 (GET recovery, verify-after-modify), the status lifecycle, or the `REMOVED`/`PURGED` tombstone filtering.

---

## Key Technical Decisions

### KTD1 — `dbidid` is a nullable `TEXT` column added via the existing migration list

Add `ALTER TABLE bids ADD COLUMN dbidid TEXT` to `_COLUMN_MIGRATIONS` (applied idempotently, "duplicate column" tolerated) and include `dbidid TEXT` in `_SCHEMA` and `_BIDS_TABLE_SQL` for fresh DBs. Nullable so existing rows and web-added snipes start NULL (→ cache miss → list fallback) until the next sync fills them.

### KTD2 — Persist `dbidid` in the sync loop via `cache_gixen_data`, written unconditionally

Extend `cache_gixen_data` with an optional `dbidid` that writes in its own statement **before** the `has_data` early-return, so a SCHEDULED snipe with no `current_bid` still gets its `dbidid` cached (it would otherwise skip the write). Same `status NOT IN ('PURGED','REMOVED')` guard as the other cache writes.

Rationale: one writer, one place, no extra fetch — `dbidid` rides along with data the sync already has.

### KTD3 — Client takes an optional `dbidid`; the server owns the cache and its invalidation

`modify_snipe(..., dbidid=None)` and `remove_snipe(item_id, dbidid=None)`: when provided, build the POST with that id and skip the pre-POST `list_snipes()`. The client stays cache-agnostic — it does **not** re-resolve internally. The server (`api_edit_bid` / `api_remove_bid`) reads the cached `dbidid` from the row, passes it, and on a cached-id failure retries via the dbidid-less path and clears the stale cache.

Rationale: separation of concerns — the cache lives in the DB (server), so cache invalidation belongs there, and the client method stays a thin toggle. Keeps BUI-115's verify/retry untouched.

---

## Implementation Units

### U1. Schema: `dbidid` column + persist during sync

**Goal:** `bids.dbidid` exists and is populated for live snipes on every sync. Advances R1.

**Files:**
- `packages/gixen-cli/server/db.py` — `_SCHEMA`, `_BIDS_TABLE_SQL`, `_COLUMN_MIGRATIONS`, `cache_gixen_data`
- `packages/gixen-cli/server/main.py` — the `cache_gixen_data(...)` call in `_sync_gixen` (pass `snipe.get("dbidid")`)
- `packages/gixen-cli/tests/test_server_db.py`, `packages/gixen-cli/tests/test_server_api.py`

**Approach:** Add the column to both schema strings and the migration list. Extend `cache_gixen_data` to write `dbidid` unconditionally (own UPDATE, before `has_data`). In `_sync_gixen`, pass the snipe's `dbidid`. Web-added snipes inserted later in the same sync get their `dbidid` on the *next* sync (acceptable — NULL until then just means cache-miss/list-fallback); note this rather than thread `dbidid` through `insert_bid`.

**Patterns to follow:** the existing `_COLUMN_MIGRATIONS` entries and `cache_gixen_data`'s COALESCE writes.

**Test scenarios:**
- Migration applies idempotently on an existing DB (run init twice → no error, column present).
- Fresh DB has the `dbidid` column.
- `cache_gixen_data` with a `dbidid` writes it even when title/seller/current_bid are all None (no `has_data`).
- `cache_gixen_data` does not write `dbidid` onto a `REMOVED`/`PURGED` row.
- After a sync (e.g. via the purge/sync path) a live bid's row has the expected `dbidid`.

**Verification:** New DB tests pass; existing `test_server_db.py` green; a synced bid row exposes `dbidid`.

### U2. Client: optional `dbidid` skips the pre-POST lookup

**Goal:** `modify_snipe` / `remove_snipe` can POST without a pre-POST `list_snipes()` when given a `dbidid`. Advances R2.

**Dependencies:** none (independent of U1; server wires them in U3).

**Files:**
- `packages/gixen-cli/gixen_client.py` — `modify_snipe`, `remove_snipe`
- `packages/gixen-cli/tests/test_gixen_client.py`

**Approach:** Add `dbidid: str | None = None`. When `None`, current behavior (list + `_find_snipe`). When provided, use it directly and skip the lookup list. Leave the BUI-115 post-POST verify (modify) and the existing post-delete verify (remove) exactly as-is — they still re-list, which is the staleness safety net. Do not add internal re-resolution (the server owns that).

**Patterns to follow:** the existing `modify_snipe`/`remove_snipe` bodies; keep the `data` dict construction identical apart from the id source.

**Test scenarios:**
- `modify_snipe(..., dbidid="5001")` with a confirming post-POST list → succeeds and `list_snipes` is called **once** (the verify), not twice (no pre-POST lookup). Assert via call count.
- `modify_snipe(...)` with no `dbidid` → unchanged: lists first (pre-POST) then verifies.
- `modify_snipe(..., dbidid="stale")` whose verify never confirms → raises `GixenModifyNotConfirmedError` (no internal re-resolve).
- `remove_snipe(item_id, dbidid="5001")` → POSTs delete with that id, skips the pre-POST lookup, still verifies removal.
- `remove_snipe` with stale `dbidid` where the item remains → raises (existing "still in list" behavior).

**Verification:** New tests pass; existing `TestModifySnipe` / remove tests green.

### U3. Server: use cached `dbidid`, fall back + invalidate on staleness

**Goal:** `api_edit_bid` / `api_remove_bid` use the cached `dbidid` on the happy path and recover from a stale one. Advances R3.

**Dependencies:** U1 (column), U2 (client param).

**Files:**
- `packages/gixen-cli/server/main.py` — `api_edit_bid`, `api_remove_bid`, and `_modify_and_update_bid` (the add→modify fallback helper, which should also pass a cached id when available)
- `packages/gixen-cli/server/db.py` — a small helper to clear a stale `dbidid` (e.g. set it NULL) if not trivially inlined
- `packages/gixen-cli/tests/test_server_api.py`

**Approach:** Read the row's `dbidid` before the Gixen call; pass it to `modify_snipe`/`remove_snipe`. Wrap the cached-id attempt: on `GixenModifyNotConfirmedError` (modify) or the remove "still present" failure **when a cached id was used**, clear the cached `dbidid` and retry once via the dbidid-less path (fresh list). If the cached id was NULL to begin with, behavior is exactly today's. Preserve all existing status-code mappings (404 not-found, 503 GixenError, the not-in-DB self-heal sync).

**Approach note (amplification):** the fallback adds at most one extra list-based attempt and only on the stale-cache path. Keep it to a single fallback — do not loop.

**Test scenarios:**
- Bid with a cached `dbidid`: PATCH → `modify_snipe` called with that `dbidid`; 200; DB updated.
- Bid with NULL cached `dbidid`: PATCH → `modify_snipe` called with `dbidid=None`; behaves as today.
- Stale cached `dbidid`: first (cached) modify raises `GixenModifyNotConfirmedError`, server retries dbidid-less and succeeds → 200; cached `dbidid` was cleared/refreshed.
- Genuinely unconfirmable modify (both cached and fresh fail) → 503, DB unchanged (BUI-115 invariant preserved).
- `api_remove_bid` with cached `dbidid` → remove called with it; stale-id remove falls back to list-based remove.

**Verification:** New integration tests pass; all existing `test_server_api.py` edit/remove tests green; the BUI-115 unconfirmed-modify-leaves-DB-unchanged test still passes.

---

## Scope Boundaries

**In scope:** schema column + sync persistence, the optional client param, the server cached-path + staleness fallback, tests.

**Out of scope (non-goals):**
- BUI-115's recovery/verify internals — reused, not modified.
- Threading `dbidid` through `insert_bid` for same-sync web-added snipes — they fill on the next sync; not worth the surface.
- The `bids.status` lifecycle and `REMOVED`/`PURGED` tombstone handling.

### Deferred to Follow-Up Work
- The login-amplification mitigation surfaced in BUI-115's review (a login-dedup throttle) is independent of this change.

---

## Risks & Dependencies

- **Risk: stale `dbidid` silently targets a wrong row.** Mitigated by BUI-115's post-POST verify (catches an unconfirmed modify) + the server fallback that re-resolves and clears the cache. This is the load-bearing safety net — U3 must not ship without it.
- **Risk: added amplification on the stale-cache path.** Bounded to one extra list-based attempt, only on failure. No loop.
- **Dependency:** stacked on BUI-115 (verify-after-modify is the safety net) and BUI-114.

---

## Verification Strategy

Run from `packages/gixen-cli`: `uv run pytest -m "not integration"` — full suite stays green (244 on the BUI-115 branch). New tests in `test_server_db.py` (schema/persistence), `test_gixen_client.py` (client param), `test_server_api.py` (server cached-path + fallback). Optional post-deploy smoke: edit a snipe twice in a row and confirm the second edit issues no pre-POST list (visible as fewer GETs in the now-timestamped logs from BUI-114).
