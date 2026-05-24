---
title: "feat: Add wish-list cache to locg-cli for offline access"
type: feat
status: active
date: 2026-05-24
---

# feat: Add wish-list cache to locg-cli for offline access

**Target repo:** locg-cli (`~/Projects/locg-cli`)

## Overview

`locg wish-list` currently always fetches from the live LOCG web app via Playwright. When the session expires the command fails, which blocks `seller-scan`. The ComicGeeks XLSX export already contains an `In Wish List` column that populates every row in `collection.json` — the data is there, just never written to a separate cache. This plan adds a `wish-list.json` cache populated as a side-effect of `collection import`, and updates `locg wish-list` to serve from it when available, so `seller-scan` can run without an active session.

## Problem Frame

`seller-scan` calls `locg wish-list` via subprocess on every run. If the LOCG session has expired (common — Playwright cookies time out), the subprocess errors and the scan aborts. The user then has to re-login before scanning, even though their wish-list data was already imported as part of their last XLSX import.

## Requirements Trace

- R1. `locg wish-list` serves from `~/.cache/locg/wish-list.json` when the cache file exists.
- R2. The wish-list cache is populated (or refreshed) automatically during every `locg collection import` run.
- R3. `seller-scan` works without an active LOCG session once the cache exists.
- R4. Cache writes follow the existing atomic write + `chmod 600` pattern.
- R5. When no cache exists, `locg wish-list` falls back to the current live path (preserving existing behavior).

## Scope Boundaries

- Does not add a standalone `locg wish-list import` command — cache is written as a side-effect of `collection import`.
- Does not backfill the LOCG `id` field in the cache — the XLSX export does not include it; `id` will be `null` in cached items. This is safe: `seller-scan` uses `id` only as a passthrough field in match output.
- Does not fix the hardcoded `cwd` path in `seller_scan.py` — pre-existing portability issue, out of scope.
- Does not change `locg wish-list`'s live-fetch behavior when no cache exists.

## Context & Research

### Relevant Code and Patterns

- `src/locg/config.py` — `collection_cache_path()` and `_cache_dir()`: model for the new `wish_list_cache_path()`.
- `src/locg/collection_io.py` — `parse_xlsx()` and `import_xlsx()` / `do_merge()`: the import pipeline where the wish-list write belongs. Every row already carries `in_wish_list` (int 0/1) after `parse_xlsx`.
- `src/locg/collection_cache.py` — `_write_payload_atomic()`: tempfile + `os.replace` + `os.fsync` + `chmod 600` pattern to mirror.
- `src/locg/cache.py` — `IDCache._save()`: simpler atomic-write example appropriate for the wish-list cache (no flock, no backup rotation needed).
- `src/locg/commands.py` — `cmd_wish_list()`: current live implementation; `_get_user_list()` is the live path.
- `src/locg/cli.py` — `_LOCAL_COLLECTION_SUBCMDS` set and the pre-routing check pattern for avoiding Playwright launch.
- `tests/conftest.py` — `_isolate_id_cache` and `_isolate_collection_cache` autouse fixtures: model for the new `_isolate_wish_list_cache` fixture.
- `apps/ebay/src/seller_scan.py` — `fetch_wish_list()`: calls `locg wish-list` via subprocess; automatically benefits once the CLI serves from cache.

### Institutional Learnings

- The LOCG API `?list=wish` parameter is silently ignored — live wish-list filtering is done by client-side bitmask parsing. The XLSX `in_wish_list` column is authoritative and avoids this issue entirely.
- `full_title` in the XLSX (e.g. `"Amazing Spider-Man #300"`) maps directly to the `name` field that `seller_scan._parse_wish_name` expects — no transformation needed.
- The GSFF false positive (Giant-Size Fantastic Four conflated with FF Annual in the live API) does not affect the XLSX-derived cache. Do not add a collection-check guard in seller-scan that calls the live `locg check` endpoint.

## Key Technical Decisions

- **Write wish-list cache inside `import_xlsx`'s `do_merge()`**: the write happens while the collection lock is already held, so no additional locking is needed and the two caches stay in sync.
- **Cache-first with live fallback**: `locg wish-list` prefers the cache when it exists; falls back to the live path when not. This is non-breaking and requires no user action beyond their existing import workflow.
- **Short-circuit Playwright in `cli.py`**: check for the cache file before the client is constructed, so no browser process is launched when the cache is available. Follow the `_LOCAL_COLLECTION_SUBCMDS` pattern or an equivalent pre-routing guard.
- **Cache shape**: each entry includes at minimum `name` (mapped from `full_title`) and `id` (`null`). Including additional XLSX fields (`series_name`, `publisher_name`, `release_date`, `media_format`) makes the terminal output more useful and costs nothing; the implementer should include whichever fields add value without over-engineering.
- **Simple atomic write (no flock, no backup rotation)**: the wish-list cache is written only during `collection import` (which holds the collection lock) and read at wish-list time. The simpler `IDCache._save()` pattern is sufficient — no need for the full `CollectionCache.apply` machinery.

## Open Questions

### Resolved During Planning

- **Does the XLSX already contain wish-list data?** Yes. The `in_wish_list` column (index 5) is present in every XLSX export and is already parsed by `parse_xlsx()` into each row dict.
- **Does `seller_scan.py` need changes?** No. Once `locg wish-list` serves from cache, the existing subprocess call benefits automatically. The hardcoded `cwd` is a pre-existing issue deferred out of scope.
- **Will `id: null` break seller-scan?** No. `seller_scan.prepare_wish_items` reads `id` only as a passthrough to the output `wish_id` field. Matching logic uses only `name`. `null` wish_id in output is acceptable.

### Deferred to Implementation

- **Exact XLSX fields to include in cache entries beyond `name` and `id`**: decide at implementation time based on what makes `locg wish-list` output most useful.
- **Whether to emit a warning when falling back to the live path**: a stderr notice ("No wish-list cache found; fetching live…") may be helpful but is not required.

## Implementation Units

- [x] **Unit 1: Add `wish_list_cache_path()` to config**

  **Goal:** Establish the canonical path for the wish-list cache, consistent with existing cache path helpers.

  **Requirements:** R4

  **Dependencies:** None

  **Files:**
  - Modify: `src/locg/config.py`
  - Test: `tests/test_config.py`

  **Approach:**
  - Add `wish_list_cache_path() -> Path` returning `_cache_dir() / "wish-list.json"` — a one-liner that mirrors `collection_cache_path()`.
  - Inherits XDG_CACHE_HOME support automatically via `_cache_dir()`.

  **Patterns to follow:**
  - `src/locg/config.py` — `collection_cache_path()` (the only other path helper in config.py; `ids_cache_path()` does not exist there)

  **Test scenarios:**
  - Happy path: `wish_list_cache_path()` returns a `Path` ending in `wish-list.json` under `_cache_dir()`.
  - Edge case: when `XDG_CACHE_HOME` is set to a custom value, `wish_list_cache_path()` reflects it (consistent with existing path helper tests).

  **Verification:**
  - `wish_list_cache_path()` is importable from `locg.config` and returns the correct path under both default and XDG_CACHE_HOME-overridden environments.

---

- [x] **Unit 2: Write wish-list cache during `collection import`**

  **Goal:** Populate `wish-list.json` as a side-effect of every `locg collection import` run, so the cache is always fresh after an import.

  **Requirements:** R2, R4

  **Dependencies:** Unit 1

  **Files:**
  - Modify: `src/locg/collection_io.py`
  - Test: `tests/test_collection_io.py`

  **Approach:**
  - Inside `do_merge()` (the inner function of `import_xlsx`), after the merge updates `payload["comics"]`, filter for rows where `in_wish_list == 1` and store them in a nonlocal variable (e.g., `wish_rows`). Do not write to disk inside `do_merge()`.
  - After `cache.apply(do_merge, command="import")` returns successfully in `import_xlsx`, write `wish_rows` atomically to `wish_list_cache_path()`. Writing after `apply()` returns ensures `collection.json` and `wish-list.json` only both update when the full import succeeds; writing inside `do_merge()` would allow the two caches to diverge if `apply()`'s own atomic write later fails.
  - Map each row to a wish-list entry: `name` from `full_title`, `id` as `null`, plus any additional XLSX fields deemed useful.
  - Write an envelope dict to `wish_list_cache_path()`: `{"updated_at": "<ISO 8601 UTC timestamp>", "items": [<entries>]}`. Using an envelope (consistent with `collection.json`'s top-level metadata fields like `last_full_import`) gives staleness visibility without a sidecar file and allows future fields without breaking readers.
  - Atomic write: `tempfile.mkstemp` in the same directory → `json.dump` → `os.replace` → `chmod(0o600)`. **Do not call `os.fsync`** — `IDCache._save()` does not include fsync, and the wish-list cache does not require it (it is a read-only best-effort cache, not a durable record). R4's "atomic write + chmod 600" refers to the tempfile+replace+chmod pattern, not the fsync behavior of `_write_payload_atomic`.
  - This write occurs after the existing collection flock is released (the flock covers `apply()`). No additional locking is needed.

  **Patterns to follow:**
  - `src/locg/cache.py` — `IDCache._save()` for atomic write (tempfile + replace + chmod, no flock, no fsync, no backup rotation).

  **Test scenarios:**
  - Happy path: after `import_xlsx(fixture_path, cache)`, `wish_list_cache_path()` exists and its `items` list contains exactly the rows where `in_wish_list == 1` (expected: ~508 rows against the fixture).
  - Happy path: each entry in `items` has a `name` field equal to the row's `full_title`.
  - Happy path: each entry in `items` has `id` equal to `null`.
  - Happy path: the envelope includes an `updated_at` field that is a valid ISO 8601 UTC timestamp close to the import time.
  - Edge case: re-importing overwrites the previous `wish-list.json` (not appended).
  - Edge case: an XLSX with zero wish-list rows writes `{"updated_at": "...", "items": []}` (not an error).
  - Integration: `wish-list.json` is only written when `import_xlsx` succeeds; a corrupt XLSX should not produce a partial cache file.

  **Verification:**
  - Running `locg collection import <fixture>` creates `wish-list.json` in `_cache_dir()` with the correct row count, correct `name` values, and file mode `0600`.
  - Each cache entry is a dict with at minimum `{"name": str, "id": null}` — the shape `seller_scan.prepare_wish_items` expects. Passing the parsed list to `prepare_wish_items` should not raise.

---

- [x] **Unit 3: Serve `locg wish-list` from cache with live fallback**

  **Goal:** Make `locg wish-list` (and therefore `seller-scan`) work without an active LOCG session when the cache exists.

  **Requirements:** R1, R3, R5

  **Dependencies:** Units 1 and 2

  **Files:**
  - Modify: `src/locg/commands.py`
  - Modify: `src/locg/cli.py`
  - Modify: `tests/conftest.py`
  - Test: `tests/test_commands.py`

  **Approach:**
  - Add `cmd_wish_list_from_cache(title: Optional[str] = None) -> list[dict]` in `commands.py`: reads `wish_list_cache_path()`, raises `FileNotFoundError` if the file is missing. Parses the envelope dict and reads from `cache["items"]`. Applies `title` filter if provided (case-insensitive substring match on `name`, consistent with how the live command handles `--title`).
  - In `cli.py`, add a conditional pre-routing guard: extend the `_needs_client` expression to also be `False` when `args.command == "wish-list"` and `wish_list_cache_path().exists()`. Then in the command dispatch block add a branch that calls `cmd_wish_list_from_cache` when `client is None` (i.e., cache path was taken). Do NOT add `"wish-list"` unconditionally to `_LOCAL_COLLECTION_SUBCMDS` — that would break R5 by preventing a Playwright launch when no cache exists. The `_LOCAL_COLLECTION_SUBCMDS` set (currently `{"import", "export", "status", "check", "doctor", "record-win"}`) routes commands that never need a client; `wish-list` only avoids a client when the cache exists, so it requires a conditional expression, not a set membership addition.
  - TOCTOU handling: wrap the `cmd_wish_list_from_cache` call in `cli.py` with `try/except FileNotFoundError` and fall through to the live path on that exception. This covers the race where the cache file is deleted between the `.exists()` check and the read, and also covers malformed JSON (which raises `json.JSONDecodeError`, a subclass of `ValueError` — catch that too and fall through). Without this, a missing or corrupt cache would exit code 4 instead of fetching live.
  - Add `_isolate_wish_list_cache` autouse fixture to `tests/conftest.py`. The fixture must monkeypatch `wish_list_cache_path` in **every module that imports it** with a `from locg.config import wish_list_cache_path` binding: `locg.collection_io` (for the write path), `locg.commands` (for `cmd_wish_list_from_cache`), and `locg.cli` (for the pre-routing guard). Patching only `locg.config.wish_list_cache_path` will not intercept calls in modules that have already bound the name locally. Model after `_isolate_collection_cache` in `conftest.py` which patches `locg.collection_cache` module-level bindings, not `locg.config` directly.

  **Patterns to follow:**
  - `src/locg/cli.py` — `_needs_client` expression and pre-routing guard pattern (extend the conditional to include cache-existence check for `wish-list`).
  - `tests/conftest.py` — `_isolate_collection_cache` for the per-module monkeypatching approach.
  - `src/locg/commands.py` — existing `cmd_wish_list()` for the `--title` filter behavior to mirror.

  **Test scenarios:**
  - Happy path: when `wish-list.json` exists and is populated, `cmd_wish_list_from_cache()` returns the full `items` list (envelope is unwrapped before returning).
  - Happy path: `cmd_wish_list_from_cache(title="Spider-Man")` returns only entries whose `name` contains "Spider-Man" (case-insensitive).
  - Edge case: `cmd_wish_list_from_cache()` on an empty cache (`[]`) returns an empty list without error.
  - Error path: `cmd_wish_list_from_cache()` when `wish-list.json` does not exist raises `FileNotFoundError`.
  - Integration: `cli.py` with `wish-list` command and cache present does not construct a `LOCGClient` (assert no Playwright launch; mock `LOCGClient.__init__` and assert it is not called).
  - Integration: `cli.py` with `wish-list` command and no cache present falls through to the existing live path (mock `cmd_wish_list` is called).

  **Verification:**
  - After `locg collection import`, running `locg wish-list` succeeds even when the LOCG session is expired (no `AuthRequired` raised).
  - `locg wish-list` output is a JSON list of wish-list items matching the imported data.
  - `seller-scan` completes a dry run without prompting for LOCG login.

## System-Wide Impact

- **Unchanged invariants:** The live `locg wish-list` path (Playwright fetch) is untouched; it remains the behavior when no cache exists, preserving the existing contract for users who haven't imported an XLSX.
- **Integration coverage:** `seller_scan.py` benefits with no code change — the subprocess call to `locg wish-list` gets the cached result transparently.
- **State lifecycle risks:** `wish-list.json` always reflects the last import; if the user's wish list changed on LOCG since the last import, the cache will be stale. This is the same trade-off already accepted for `collection.json` and is acceptable.
- **API surface parity:** No other CLI commands depend on `wish-list` output. When serving from cache, `locg wish-list` returns `null` for every `id` field — a deliberate schema change from the live path which returns real LOCG IDs. This is visible to any script or agent parsing the output; it is not signaled separately. The change is documented in Scope Boundaries and is acceptable because `seller-scan` (the only known caller) uses `id` only as a passthrough output field. Any future caller that relies on non-null `id` values must run `locg wish-list` against a live session, not from cache.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `id: null` in cache output surprises callers that expect a real LOCG ID | Documented in scope boundaries; `seller-scan` is the only known caller and uses `id` only as a passthrough |
| Wish-list data in cache is stale if user modifies their list without re-importing | Accepted trade-off (same as `collection.json`); user must re-run `collection import` to refresh |
| Atomic write fails on a full disk mid-import, leaving a partial temp file | `os.replace` is atomic on POSIX; partial temp file in same directory will be cleaned up on next import |

## Sources & References

- Linear issue: PER-130
- Related issue: PER-129 (seller-scan implementation that depends on this)
- Key files: `src/locg/config.py`, `src/locg/collection_io.py`, `src/locg/commands.py`, `src/locg/cli.py`, `src/locg/cache.py`
