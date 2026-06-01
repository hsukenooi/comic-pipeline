---
title: Local LOCG Collection Cache to Decouple `/comic:buy` from LOCG/Cloudflare
type: feat
status: active
date: 2026-05-22
origin: docs/brainstorms/2026-05-22-locg-collection-cache-requirements.md
deepened: 2026-05-22
---

# Local LOCG Collection Cache to Decouple `/comic:buy` from LOCG/Cloudflare

## Overview

Move the agent's LOCG dependency off the `/comic:buy` hot path by introducing a local JSON cache (`~/.cache/locg/collection.json`) owned by `locg-cli`. `/comic:collection-check` becomes fully offline; `/comic:collection-add` records wins to the cache instead of driving Playwright per comic. Pushing those wins back to LOCG happens via user-mediated Excel/CSV round-trips, with optional best-effort Playwright sync commands for users with fresh CF clearance. The cache learns LOCG's canonical naming conventions over time so subsequent pushes get more precise.

## Problem Frame

`/comic:buy` orchestrates `/comic:identify` → `/comic:collection-check` → `/comic:fmv` → `/comic:snipe-add`, with `/comic:collection-add` running post-win. Today only the two collection steps depend on live LOCG (Playwright + persistent Chrome profile from PER-38). On 2026-05-21 the workspace egress IP was blocked at Cloudflare's perimeter (`<title>Restricted</title>` from LOCG homepage), stalling a real session for 176 messages of thrashing before the agent prompted the user to skip collection-check. PER-71's CF-retry warm-up does not help against IP-level blocks. The brainstorm establishes that LOCG's Excel import/export endpoint is browser-mediated and reachable to the *user* even when blocked to the *agent*, creating an asymmetry the design exploits. See origin document for the full problem statement.

## Multi-Repo Scope

This plan spans three repositories. Each implementation unit declares its target repo. Repo-relative paths in `**Files:**` lists refer to the unit's target repo, not this plan's home repo.

- **`comic-pipeline`** (this repo) — skill files in `.claude/skills/`, plugin route in `plugins/gixen-overlay/`, docs.
- **`locg-cli`** (`~/Projects/locg-cli/`) — owns the cache (R2), all `locg collection ...` commands, openpyxl/mokkari deps, Playwright sync commands.
- **`gixen-cli`** (`~/Projects/gixen-cli/`) — no required changes in v1. Existing `bids.locg_id` column is preserved (R47); legacy values stay, new snipes leave it blank.

Each unit ships as its own PR in its target repo. Cross-repo dependency: `comic-pipeline` skill changes (Phase 3) require `locg-cli` Phase 1+2 to be released and the user's `~/Projects/locg-cli` editable install refreshed.

## Requirements Trace

This plan executes against requirements R1–R68 in the origin document. Mapping by phase:

- **Phase 1 — Cache foundation:** R1–R14, R60–R61, R68
- **Phase 2 — Win recording + Metron:** R15–R34, R51–R55, R62, R67
- **Phase 3 — Skill migration:** R38–R50, R65–R66
- **Phase 4 — Optional Playwright sync (gated):** R63–R64
- **Phase 5 — Cleanup:** plugin route deprecation, solution doc promotion

Goals satisfied: G1 (offline membership), G2 (offline win recording), G3 (user-mediated push), G4 (canonical naming learned over time via `series_name_index`), G5 (unresolved variants surfaced explicitly).

## Scope Boundaries

- **N1.** No phone interface, no read UI. Cache is a JSON file; LOCG mobile UI remains the read surface.
- **N2.** No two-way conflict resolution. LOCG is canonical for user-managed fields.
- **N3.** No automated variant disambiguation for unknown patterns.
- **N4.** No automatic egress workaround on the hot path. No skill invokes Playwright.
- **N5.** No agent-side comic-collection app.
- **N6.** Metron is optional metadata enrichment, not canonical identity.
- **N7.** Pull list and wanted list management stays manual.

### Deferred to Separate Tasks

- **`--prune` for rows missing from LOCG export (L5):** deferred to v2. v1 preserves local state with a warning.
- **Cross-variant pattern generalization (L1):** v1 round-trips each distinct variant once; learning to predict *other* variants of the same series is out of scope.
- **`locg-cli` rename / restructure (L7):** revisit when a second sync target or unified-collection feature lands.
- **Post-mortem solutions docs for PER-38 / PER-71 / PER-98–104:** worth writing but tracked separately; not a blocker for shipping the cache.

## Context & Research

### Relevant Code and Patterns

**`locg-cli`** (`~/Projects/locg-cli/`):
- `src/locg/cache.py` — `IDCache` class is the exact pattern for the new collection cache: atomic temp-file + `os.replace`, 0600 file / 0700 dir chmod, `CACHE_VERSION` constant in payload. Clone the shape into a sibling `collection_cache.py` module (distinct invariants — row store vs. slug index).
- `src/locg/config.py` — XDG paths. Already exposes `playwright_profile_dir()`, `env_path()` (`~/.config/locg/.env`), `ensure_config_dir()` / `ensure_cache_dir()` with chmod 0700. Extend with `collection_cache_path()` and `import_history_path()`.
- `src/locg/cli.py` — argparse-based, one flat `create_parser()` with nested `add_subparsers`. The existing `cache` and `collection` top-level groups already use `dest=...` nested verbs — add `collection import/export/record-win/resolve-series/sync-from-locg/sync-to-locg` under the same pattern.
- `src/locg/commands.py` — 1,178-line module with all `cmd_*` functions. Brainstorm O1 resolved: extend inline (consistent with house style).
- `src/locg/client.py` — `LOCGClient` Playwright + persistent Chrome context (PER-38). `_warm_up_cloudflare()` and 403 retry (PER-71). For R63/R64 sync commands, use `headless=False`.
- `tests/conftest.py` — `_isolate_id_cache` autouse fixture monkeypatches `locg.cache.cache_path` to `tmp_path`. **Replicate this for the new collection cache** so tests never touch the developer's real cache.
- Fixtures: `tests/fixtures/` (HTML + JSON). For Excel: import the user's existing real exports as golden fixtures (see below).

**`comic-pipeline`** (this repo):
- `.claude/skills/collection-check.md` — current flow calls `gixen cli list --json` + `locg lookup` + write-back. Rewrite per R38/R39.
- `.claude/skills/collection-add.md` — currently embeds inline Python Playwright for dedupe + add + wish-list cleanup (Steps 4–6). Rewrite per R43–R45.
- `.claude/skills/snipe-add.md` — references `--locg-id` flag on `gixen-cli add` that **does not exist in upstream gixen-cli**. R46 removes this; no gixen-cli change needed.
- `.claude/skills/buy.md` — orchestrator; update Step 2 gate copy per R48–R50.
- `plugins/gixen-overlay/src/gixen_overlay/routes.py:95` — `POST /api/bids/{item_id}/comics/locg`. Becomes dead code for new snipes after R38+R46. Decision below: leave in place to avoid breaking any legacy callers; mark deprecated in route docstring.
- `plugins/gixen-overlay/src/gixen_overlay/db.py` — `_migrate_year_nullable`, marker-row guard (`migration_state` table), `_assert_no_migration_marker` → `_set_migration_marker` → `_clear_migration_marker` pattern (PER-102). **Adopt the equivalent for cache schema_version bumps and crash-mid-import recovery.**

**Real Excel/CSV fixtures already in repo** (use as golden fixtures, do not synthesize):
- LOCG exports: `.context/attachments/Jhv32a/ComicGeeks-2026-05-21-12-08-13.xlsx`, plus `cEMcVJ/`, `6EZ3jf/`, `dksNRt/` siblings.
- Validated bulk-import CSVs: `.context/locg-import-test.csv` through `.context/locg-import-test-6-batch.csv` (the test recipes documented in the brainstorm Appendix).

### Institutional Learnings

- **`docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md`** — pre-write snapshot + crash gate + idempotent merge + survivor selection on collapse. Transferable patterns for cache `schema_version` evolution, reconciliation tie-breaks, and crash recovery.
- **`docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md`** — "one owner of invariants" discipline. Codifies R2: skills must shell out to `locg-cli`, never touch `collection.json` directly. Also: defensive arithmetic on counts derived from the cache (R40, R44 displays).
- **Migration crash recovery (PER-98/99/102/103/104)** in `plugins/gixen-overlay/src/gixen_overlay/db.py` — marker-row guard table is directly transferable to a cache `migration_in_progress` field; `sweep_orphan_yearless_comics(dry_run=True)` pattern informs default-dry-run behavior for any future cache reconcile/prune.
- **Greenfield areas** with no prior solutions doc: openpyxl, mokkari, atomic JSON writes (PER-71's `~/.cache/locg/ids.json` is undocumented prior art only), IP-level CF block recovery, bulk LOCG upload/download. Brainstorm Appendix is the canonical record until promoted (Phase 5).

### External References

- LOCG Bulk Import recipe — empirically validated in the brainstorm Appendix (Tests 1–6, 2026-05-22). No external docs needed; LOCG's behavior is undocumented and only the empirical recipe is reliable.
- `openpyxl` — read-only Excel parsing, MIT-licensed, no native binary requirement. Read-only mode (`load_workbook(filename, read_only=True, data_only=True)`) is sufficient.
- `mokkari` — Metron API client. Exposes `Series.year_began`, `Series.year_end: Optional[int]`, `Issue.cover_date`, `Issue.store_date`. Local retry handles 20 req/min limit.

## Key Technical Decisions

- **Decision (R2 boundary):** Skills shell out to `locg-cli` for all cache reads/writes — no direct JSON access from skill source. **Rationale:** single owner of cache invariants; consistent with the plugin-owned-endpoints best-practice doc; makes the CLI's JSON output contract the API surface. **Cost:** one subprocess invocation per win record, which is acceptable given win batches are typically small (~1–30 per session).
- **Decision (O1 resolved):** `locg collection ...` commands live inline in `src/locg/commands.py` as new `cmd_collection_*` functions, alongside the existing `cmd_*` style. No new `commands/` package. **Rationale:** matches the 1,178-line house style; avoids cross-cutting refactor.
- **Decision (boundary inversion for win recording):** Add a new `locg collection record-win` CLI subcommand that accepts a Gixen-derived JSON blob on stdin (or `--from-gixen-json <path>`). The `/comic:collection-add` skill builds the JSON and shells out per batch, not per comic. **Rationale:** preserves R2 (cache owned by CLI) while keeping the skill simple and the subprocess count bounded.
- **Decision (cache module placement):** New file `src/locg/collection_cache.py` rather than extending `cache.py`. **Rationale:** row store vs. slug index have different invariants (reconciliation, multi-row merge, audit log). Two distinct modules keep tests targeted.
- **Decision (Excel parsing module):** New file `src/locg/collection_io.py` for both Excel import (openpyxl) and CSV export. **Rationale:** parsing concerns are separate from cache mutation concerns; lets us test the wire format independently of cache merge logic.
- **Decision (plugin route deprecation):** Leave `POST /api/bids/{item_id}/comics/locg` in place; mark deprecated in docstring. **Rationale:** legacy snipes with populated `locg_id` remain valid (R47); removing the route risks breaking unknown callers. Garbage-collect in v2 when bid-creation flow no longer carries `locg_id`.
- **Decision (Playwright sync commands gated):** R63/R64 (`sync-from-locg`, `sync-to-locg`) ship after Phase 1–3 land and stabilize. R64's empirical validation gate (multi-step CF challenge across preset→upload→preview→confirm) is a hard precondition. If gate fails, R64 ships as preview-only (auto-stop at the confirm screen for the user to click manually).
- **Decision (first-run bootstrap):** `/comic:collection-check` and `/comic:collection-add` both check `last_full_import` at startup and refuse to operate with a clear error when null (R65). Not a soft warning — a hard precondition with documented remediation.
- **Decision (cache schema migrations):** Adopt the `migration_state` marker pattern from `plugins/gixen-overlay/src/gixen_overlay/db.py` adapted for JSON: a top-level `migration_in_progress` boolean is set before destructive merge ops, cleared after success. On startup, presence of the flag aborts with a clear "previous import crashed; restore from `.bak` or repair manually" message rather than re-running and corrupting state.

## Open Questions

### Resolved During Planning

- **O1 (commands.py vs commands/):** Extend `src/locg/commands.py` inline. Matches house style.
- **O2 (CSV default location):** Default to `~/Downloads/locg-bulk-import-<YYYY-MM-DD-HHMMSS>.csv`; override via `--out <path>`. Companion `*.notes.md` lands next to the CSV.
- **O3 (cache size acceptable):** ~600KB JSON at 1,795 rows + 26 fields. Direct read on each operation is fine; no indexing in v1.
- **O4 (cache staleness warning):** Always show `Cache age` column in `/comic:collection-check` (the simpler v1 behavior). Per-row "might be wrong" detection is v2.
- **O5 (gixen `bids.locg_id` backfill):** Leave column, leave values, ignore in agent flow. No migration. Documented in skill rewrite.
- **Aspirational CLI flag cleanup:** `gixen-cli locg link` and `--locg-id` on `gixen-cli add` are referenced by current skill text but do not exist in upstream `~/Projects/gixen-cli/cli.py`. R38 and R46 remove the skill-side references; no gixen-cli code change is needed (the flags were never implemented; they were aspirational in the skill).
- **Plugin route disposition:** Leave `POST /api/bids/{item_id}/comics/locg` in place, mark deprecated in docstring. v2 may remove.
- **Win-write boundary:** Skill calls `locg collection record-win --from-gixen-json -` with batched JSON on stdin. One subprocess per batch.

### Deferred to Implementation

- **Exact `series_name_index` normalization edge cases.** R60 + R61 specify the rule (strip `(Vol. N)` and year-range parens); the precise regex set may need adjustment after running against the user's real 1,795-row export. Acceptance: characterization tests against the real export drive the normalization.
- **`locg collection record-win` stdin schema.** Plan calls for a Gixen-derived JSON blob; the exact shape (list of `{item_id, current_bid, end_date_iso, identify_data: {...}}` objects) is finalized during Unit 6 implementation against the actual `gixen-cli list --json` output.
- **Mokkari best-guess Series Name format.** R62 specifies `"{name} ({year_began} - {end_year})"`. Implementation will discover which real wins fall through to `needs_manual_series_canonical` and may surface tuning opportunities for v1.1.
- **R64 multi-step CF challenge resilience.** The empirical validation gate determines whether full automation or preview-only ships. Outcome is unknowable without running it against live LOCG with a fresh profile.
- **`STALE_THRESHOLD_DAYS` default for the F1 stale-cache verdict downgrade.** Plan defaults to 14 days; tunable via `~/.config/locg/.env`. Final default may shift after a few weeks of real use shows the actual lag distribution.
- **Unit 7 (`resolve-series`) ship/drop decision.** Deepening review (F10) argued sync-from-locg subsumes the use case. Implementer must check with user before starting Unit 7.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

### Cache lifecycle (read sync — primary path)

```
User clicks "Export My Comics" in LOCG web UI
    │
    ▼
ComicGeeks-YYYY-MM-DD-HH-MM-SS.xlsx downloaded to ~/Downloads
    │
    ▼
User runs: locg collection import ~/Downloads/<file>.xlsx
    │
    ▼
collection_io.parse_xlsx() ─┐
                            ▼
                  21-column schema check (R10, R68)
                            │
                            ▼
              Phase 1 — Reconciliation (R60)
              best-guess cache rows matched against
              incoming rows via relaxed heuristic;
              matched rows have identity tuple rewritten,
              manual flags cleared, audit record appended
                            │
                            ▼
              Phase 2 — Standard merge (R11)
              incoming rows insert-or-update by identity tuple;
              cache-only rows logged but preserved (no prune)
                            │
                            ▼
              series_name_index rebuilt from
              source="locg_export" rows only (R61)
                            │
                            ▼
              collection.json atomic write + .bak snapshot
              import-history.jsonl append
```

### Cache lifecycle (write sync — user-mediated)

```
/comic:collection-add records wins
    │
    ▼
locg collection record-win --from-gixen-json -
  (per-batch, one subprocess)
    │
    ▼
series resolution chain (R36):
  cache hit → metron best-guess → manual flag
    │
    ▼
cache rows written with:
  source="agent_win", pushed_to_locg_at=null,
  local_added_at=now, needs_manual_* set per R32/R36
    │
    ▼
locg collection export ~/Downloads/<file>.csv
    │
    ▼
CSV body: pending-push rows minus needs_manual_* rows
.notes.md companion: variants + series-canonical
manual queues (R18)
    │
    ▼
User uploads CSV via LOCG Bulk Import web UI
    │
    ▼
User re-exports + runs locg collection import
  (reconciliation resolves agent_win → locg_export,
   future wins hit cache directly)
```

### Series Name resolution (at win time, CF-free)

```
              ┌──────────────────────────────┐
              │ /comic:collection-add input  │
              │ (series, issue, year?)        │
              └──────────────────────────────┘
                           │
              ┌────────────▼──────────────┐
              │  series_name_index hit?   │ ── yes ──▶ canonical Series Name (confidence: high)
              └────────────┬──────────────┘
                          no
                           │
              ┌────────────▼──────────────┐
              │  Metron lookup succeeds?  │ ── yes ──▶ "{name} ({year_began} - {end_year})" (confidence: medium)
              └────────────┬──────────────┘
                          no
                           │
                           ▼
              flag needs_manual_series_canonical = true
              write row with bare series name
              surface in next .notes.md report
```

The implementing agent should treat the diagrams above as the shape of data flow and decision points, not as code to translate line-for-line.

## Output Structure

This plan creates several new files in `locg-cli`. The expected shape (in the `locg-cli` repo, repo-relative paths):

```
src/locg/
  collection_cache.py     # NEW — row store, atomic write, reconciliation, audit log
  collection_io.py        # NEW — openpyxl Excel import + CSV export + 21-column schema
  metron.py               # NEW — mokkari wrapper, rate-limit batching, best-guess Series Name
  config.py               # MODIFIED — add collection_cache_path(), import_history_path()
  cli.py                  # MODIFIED — add 'collection' subparser with import/export/record-win/resolve-series/sync-from-locg/sync-to-locg
  commands.py             # MODIFIED — add cmd_collection_* functions inline

tests/
  conftest.py             # MODIFIED — add _isolate_collection_cache autouse fixture
  test_collection_cache.py    # NEW — row schema, identity, reconciliation, schema_version, .bak recovery
  test_collection_io.py       # NEW — xlsx parse, 21-column header validation, CSV recipe round-trip
  test_collection_commands.py # NEW — CLI dispatch, import/export end-to-end, record-win JSON contract
  test_metron.py              # NEW — mokkari wrapper, rate-limit batching, best-guess format
  fixtures/
    collection_export_sample.xlsx   # NEW — copy of .context/attachments/Jhv32a/...xlsx
    locg_import_test_recipe.csv     # NEW — copy of .context/locg-import-test-6-batch.csv
```

In `comic-pipeline` (this repo):

```
.claude/skills/
  collection-check.md   # REWRITE — fully offline, cache-only membership
  collection-add.md     # REWRITE — shell out to locg collection record-win + export
  snipe-add.md          # MODIFIED — drop --locg-id reference
  buy.md                # MODIFIED — Step 2 gate copy, no CF branch
```

The implementer may adjust this layout if implementation reveals a better split (e.g., a `collection/` subpackage if any module exceeds ~600 lines). Per-unit `**Files:**` lists remain authoritative.

## Implementation Units

Units are ordered by dependency. Phase 1 (Units 1–4) lands first as one or more PRs in `locg-cli`; Phase 2 (Units 5–7) follows; Phase 3 (Units 8–11) lands in `comic-pipeline` once Phase 1+2 is released; Phase 4 (Units 12–13) is gated and optional.

### Phase 1 — Cache Foundation (`locg-cli`)

- [ ] **Unit 1: Collection cache module + config**

**Target repo:** `locg-cli`

**Goal:** Establish `~/.cache/locg/collection.json` as a row store with atomic writes, 0600/0700 perms, schema_version guard, `.bak` snapshot, and crash-marker pattern.

**Requirements:** R1, R2, R3, R4, R6, R7, R8, R9, R68

**Dependencies:** None.

**Files:**
- Create: `src/locg/collection_cache.py`
- Modify: `src/locg/config.py` (add `collection_cache_path()`, `import_history_path()`)
- Test: `tests/test_collection_cache.py`
- Modify: `tests/conftest.py` (add `_isolate_collection_cache` autouse fixture)

**Approach:**
- Clone the atomic-write + chmod discipline from `src/locg/cache.py` (tempfile + `os.replace`), tightened per the deepening pass:
  - `fsync(tempfile_fd)` before `os.replace`; `fsync(parent_dir_fd)` after `os.replace`. Standard atomic-write hygiene; prevents stale-cache surprises after power loss.
  - `.bak` is itself written via tempfile + `os.replace` (never `shutil.copy`) so a crash mid-backup cannot leave a half-written `.bak`.
- Concurrent-writer safety: acquire `fcntl.flock(LOCK_EX)` on a sentinel file `~/.cache/locg/collection.lock` for the full read-mutate-write cycle. Required because Phase 4's `sync-from-locg && sync-to-locg` cron can race against an interactive `record-win` or `import`. Document the lock; brief blocking is preferred over rejection.
- Top-level payload: `{schema_version, last_full_import, last_import_source, migration_in_progress, last_writer, series_name_index, comics: [...]}`.
- Row dataclass mirrors LOCG's 21 columns (snake_case) + tracking fields (`local_added_at`, `local_added_seq`, `pushed_to_locg_at`, `last_seen_in_export_at`, `source`, `needs_manual_variant`, `needs_manual_series_canonical`, `metron_id`, `gixen_item_id`).
- `local_added_seq` is a per-process monotonic counter appended to `local_added_at` for tiebreaking. Wall-clock microsecond uniqueness is not guaranteed across rows added in the same batch — R60's tiebreak must use `(local_added_at, local_added_seq)` as a composite key so multi-match resolution is deterministic.
- Identity tuple `(publisher_name, series_name, full_title, release_date)` is the dedupe key (R9).
- **Single-atomic-write merge discipline (tightened to close crash windows surfaced during deepening):**
  - Step A: acquire `flock`. Load live file. Write `.bak.N` (N=3 rolling backups, see below) snapshotting the *pre-merge* state. Only refresh `.bak.0` at the *start* of the next merge, never on success — this preserves the last known-good state across a bad import.
  - Step B: mutate in memory. Set `migration_in_progress=true` and the new merged data into the same in-memory payload.
  - Step C: single atomic write of the merged payload with `migration_in_progress=false` (i.e., flag is set and cleared in-memory only — there is no on-disk state of "merged but flagged"). This eliminates the kill-between-merge-and-flag-clear window.
  - Step D: release `flock`.
- Rolling backups: keep `~/.cache/locg/collection.json.bak.0` (most recent pre-merge), `.bak.1`, `.bak.2`. Rotate at the *start* of each merge before writing `.bak.0`. v1 ships with N=3; a bad import that overwrites .bak.0 still leaves .bak.1 and .bak.2 from prior known-good states. Cheap insurance.
- `last_writer` records `{pid, ts, command}` per write to make audit log triage easier.
- On load (no merge): if `migration_in_progress=true` AND `last_full_import` differs from `.bak.0`'s value → abort with "previous operation crashed; restore from `.bak.0` or re-import from `<last_import_source>`" message. If `migration_in_progress=true` AND `last_full_import` matches `.bak.0` → auto-clear flag and log (this is the "killed-before-merge-began" case; nothing was actually corrupted). If `schema_version > known` → abort with explicit downgrade guidance: `"locg-cli out of date relative to this cache; upgrade locg-cli OR delete '~/.cache/locg/collection.json' and re-import from your most recent LOCG export (path in last_import_source)."` The xlsx is the canonical source; `.bak` is not version-portable.
- Audit log (`~/.cache/locg/import-history.jsonl`) envelope shape locked in this unit (not deferred): every record has `{type, ts, command, details: {...}}` where `type` is one of `"reconciliation"`, `"renamed_full_title"`, `"possibly_removed"`, `"ambiguous_reconciliation"`, `"likely_stale"`, `"behavioral_drift"` (Unit 2 checksum mismatch — see F5 below), `"sync_to_locg_result"` (Unit 13). Per-record write is a single `os.write(fd, payload+'\n')` of <4KB, which POSIX guarantees atomic. fsync after each append.

**Execution note:** Test-first. Start with a failing test for the load/store round-trip on a fresh tmp_path, then incrementally add reconciliation in Unit 2.

**Patterns to follow:**
- `src/locg/cache.py` IDCache (atomic write, chmod)
- `plugins/gixen-overlay/src/gixen_overlay/db.py` marker-row guard (adapted to JSON top-level flag)

**Test scenarios:**
- Happy path: empty cache load returns default payload; insert+save+reload round-trips identically.
- Happy path: identity tuple `(publisher, series, full_title, release_date)` deduplicates inserts.
- Edge case: boolean columns stored as int 0/1 (R7) — assert via JSON inspection.
- Edge case: 0600 file / 0700 dir perms verified after every write on a fresh tmp_path.
- Edge case: schema_version 1 cache loads cleanly; schema_version 99 cache raises a clear `RuntimeError` mentioning re-import from xlsx as the canonical recovery (not `.bak`, since backups aren't version-portable).
- Edge case: cache load when `migration_in_progress=true` AND `last_full_import != .bak.0.last_full_import` raises a clear "previous operation crashed" error.
- Edge case: cache load when `migration_in_progress=true` AND `last_full_import == .bak.0.last_full_import` auto-clears flag and logs (killed-before-merge-began case).
- Error path: corrupt JSON file raises `RuntimeError` with rolling-backup restore guidance.
- Edge case: `.bak.0` snapshot is written before merge mutates the live file (verify by simulating mid-merge crash and inspecting `.bak.0` contents — must match pre-merge state).
- Edge case: rolling backup rotation — three successive merges leave `.bak.0`, `.bak.1`, `.bak.2` with monotonically older contents.
- Edge case: `.bak.0` is NOT refreshed on successful merge (only at start of next merge). Verify by snapshotting `.bak.0` mtime across a successful merge cycle.
- Edge case: simulated crash between merge-write and (hypothetical) flag-clear is impossible because flag-clear happens in-memory inside the same atomic write — assert by inspecting any partial on-disk state during the merge cycle.
- Edge case: two `local_added_at` values from the same batch with identical microsecond stamps differ in `local_added_seq` and order deterministically under R60 tiebreak.
- Edge case: concurrent merge attempt via two processes — second process blocks on `flock` until first releases, then proceeds; no lost updates.
- Edge case: fsync verified on tempfile fd before `os.replace` (mock `os.fsync` to assert call).
- Error path: `flock` acquisition timeout (test with short timeout override) returns a clear "another locg-cli operation in progress" error.
- Edge case: audit log envelope shape — every record is a single line, parses as JSON, has `{type, ts, command, details}` keys.
- Integration: `series_name_index` is rebuilt from `source="locg_export"` rows only — `agent_win` rows do not contribute (R61).

**Verification:** Round-trip a synthetic 50-row cache through save → reload → identity check; all 50 rows preserved with no perm/schema drift. Crash-recovery test: kill -9 a process mid-merge → next load triggers the correct recovery path based on `.bak.0` state.

---

- [ ] **Unit 2: Excel import + reconciliation (Phase 1 logic of R11)**

**Target repo:** `locg-cli`

**Goal:** Parse LOCG's 21-column Excel export and merge into the cache via a two-phase pipeline (reconciliation + standard merge).

**Requirements:** R5, R10, R11, R12, R13, R14, R60, R61, R67

**Dependencies:** Unit 1.

**Files:**
- Create: `src/locg/collection_io.py`
- Modify: `src/locg/collection_cache.py` (add `import_xlsx()` orchestration entry point)
- Modify: `pyproject.toml` (add `openpyxl>=3.1` to deps)
- Test: `tests/test_collection_io.py`
- Test fixture: `tests/fixtures/collection_export_sample.xlsx` (copy from `comic-pipeline/.context/attachments/Jhv32a/`)

**Approach:**
- Use `openpyxl.load_workbook(path, read_only=True, data_only=True)`. Validate first-sheet header row exactly matches the expected 21-column ordered list before any row read; reject with clear error otherwise.
- Reject files >10 MB pre-parse (R10).
- Phase 1 reconciliation per R60: iterate cache rows with `needs_manual_variant=true` OR `needs_manual_series_canonical=true`, attempt match against each incoming row using the relaxed heuristic (publisher mapping → series normalization → string-token issue match → exact-year date match → TPB/HC/OGN fallback). Multi-match tiebreak: `max(local_added_at)` then `gixen_item_id ASC`; remaining ties leave all rows flagged.
- Phase 2 standard merge: insert-or-update by post-reconciliation identity tuple; preserve `local_added_at`, `local_added_seq`, `metron_id`, `gixen_item_id`. Detect renamed `full_title` (same `(publisher, series, release_date)`, different `full_title`) and persist `previous_full_title` for one cycle (R67).
- Rebuild `series_name_index` at end from `source="locg_export"` rows only.
- Append a JSON record per reconciliation and per warning to `import-history.jsonl` using the locked envelope shape from Unit 1.
- **Behavioral-drift detection (closes F5 from deepening review).** For each cache row that the import touched (matched-existing, not net-insert), compute a checksum of the user-managed columns *that locg-cli does not write* — `my_rating`, `marked_read`, `condition`, `notes`, `tags`, `storage_box`, `owner`, `grading`, `grading_company`. If the post-import checksum differs from the pre-import checksum for that row, log a `type: "behavioral_drift"` audit record with `{identity_tuple, columns_changed: [...]}`. The header check catches schema drift; this catches the silent-default-mutation drift class evidenced in Test 1 (`My Rating=5.0` flip) and Test 5 (LOCG date corrections). Test 5 silent date corrections are *expected* and not flagged; only user-managed columns are checksum-compared.

**Patterns to follow:**
- `plugins/gixen-overlay/src/gixen_overlay/db.py` PER-103/104 orphan reparenting (reconciliation is conceptually identical: pick canonical, reparent tracking fields, drop the duplicate).

**Test scenarios:**
- Happy path: parse `collection_export_sample.xlsx` into row list; row count matches openpyxl's max_row - 1.
- Happy path: insert 100 new rows on empty cache → all present with `source="locg_export"`, `pushed_to_locg_at=now`.
- Happy path: re-import the same xlsx → row count unchanged, `last_seen_in_export_at` updated on all rows.
- Edge case: header row mismatch (column missing, reordered, renamed) raises `RuntimeError` before any merge.
- Edge case: file >10 MB rejected with size error before parse.
- Edge case: best-guess row with `needs_manual_series_canonical=true` and matching `(publisher, series, issue_token, year)` is reconciled — identity tuple rewritten, flags cleared, `local_added_at`/`gixen_item_id` preserved.
- Edge case: best-guess row whose `(Vol. N)` annotation differs from incoming is NOT reconciled (hard mismatch per R60).
- Edge case: TPB row (no `#N` token) reconciles via case-insensitive `full_title` exact match.
- Edge case: incoming row issue token `Annual 1` does not reconcile against cache row token `1` (string compare, not numeric).
- Edge case: multi-match — two best-guess cache rows match same incoming row; tiebreak by `max(local_added_at)` then `gixen_item_id ASC`; remaining ties leave all flagged (and surface in summary).
- Edge case: LOCG renamed `full_title` event detected (R67); `previous_full_title` persisted for one import cycle.
- Edge case: cache-only row (`source="agent_win"`, `pushed_to_locg_at=null`) survives an unrelated import unchanged.
- Edge case: cache-only row with `pushed_to_locg_at!=null` not in current import logs a "possibly removed" warning, is NOT deleted (v1 errs on preservation per R11).
- Error path: simulated openpyxl read error mid-import sets `migration_in_progress=false` on cleanup (or leaves it on for explicit recovery — pick one and assert it).
- Integration: end-to-end import of real `collection_export_sample.xlsx` produces a non-empty `series_name_index` keyed on normalized series names.

**Verification:** Importing the user's real 1,795-row export round-trips through cache → re-export CSV → re-import without identity churn beyond the documented LOCG corrections.

---

- [ ] **Unit 3: CSV export + `.notes.md` companion report**

**Target repo:** `locg-cli`

**Goal:** Generate a LOCG-compatible 21-column CSV from pending-push cache rows plus a sibling `.notes.md` manual-handling report.

**Requirements:** R15, R16, R17, R18, R21, R22, R23, R24, R25, R26, R27, R28, R29, R30, R31

**Dependencies:** Unit 1.

**Files:**
- Modify: `src/locg/collection_io.py` (add `generate_csv()`, `generate_notes_md()`)
- Test: `tests/test_collection_io.py`
- Test fixture: `tests/fixtures/locg_import_test_recipe.csv` (copy from `comic-pipeline/.context/locg-import-test-6-batch.csv`)

**Approach:**
- Pending-push selection: rows where `pushed_to_locg_at IS NULL` OR `local_added_at > pushed_to_locg_at` (R15), excluding rows with `needs_manual_variant=true` OR `needs_manual_series_canonical=true` (R16).
- CSV writer uses Python `csv.writer` with quoting=QUOTE_MINIMAL, header row exactly matches LOCG's 21-column order, all rows include all 21 columns (R21).
- Empirical recipe (R21–R31): `In Collection=1`, `In Wish List=0`, `Marked Read=0`, `My Rating` present-but-blank (critical — R27), `Media Format="Print"`, `Purchase Store="eBay"`, `Signature=0`, `Slabbing=0`, remaining columns blank or from row data.
- Companion `.notes.md` has three sections (R18): Ready to upload (count), Needs manual handling — variants, Needs manual handling — series canonical. Each manual row lists series, issue, eBay item ID, win price, listing variant text or suggestion.

**Patterns to follow:**
- Brainstorm Appendix Empirical Recipe Summary; `comic-pipeline/.context/locg-import-test-6-batch.csv` is the golden reference output.

**Test scenarios:**
- Happy path: 10 pending-push rows produce a 10-row CSV with exact 21-column header order.
- Happy path: `My Rating` column appears in header AND in body as an empty string (R27 — critical) — assert by inspecting CSV raw bytes.
- Happy path: `.notes.md` correctly counts ready/manual-variant/manual-series rows.
- Edge case: zero pending-push rows produces a CSV with header only and a `.notes.md` noting empty queue.
- Edge case: rows with `needs_manual_variant=true` appear in `.notes.md` variants section, NOT in CSV body.
- Edge case: rows with `needs_manual_series_canonical=true` appear in `.notes.md` series-canonical section, NOT in CSV body.
- Edge case: `Price Paid` formatted as float without currency suffix (R29); negative or missing values default to `0.00`.
- Edge case: `Date Purchased` formatted as ISO date from Gixen `end_date_iso` (R30); missing value falls back to today.
- Edge case: row with both manual flags appears in BOTH `.notes.md` sections (or pick one canonical section — document the rule).
- Integration: bit-for-bit comparison against `tests/fixtures/locg_import_test_recipe.csv` for a fixed input row set (acceptance: the validated recipe round-trips).

**Verification:** Generated CSV uploads cleanly against LOCG bulk import in a manual test against a small fresh batch.

---

- [ ] **Unit 4: `locg collection import`, `export`, `status`, `check`, and `doctor` CLI subcommands**

**Target repo:** `locg-cli`

**Goal:** Wire Units 2–3 into the CLI surface, plus add the supporting commands skills depend on: `status` (used by R65 bootstrap guard + F9 observability), `check` (used by `/comic:collection-check`), and `doctor` (used for first-run discoverability per F2 from deepening review).

**Requirements:** R10, R11, R12, R13, R15, R18, R38, R65 (skill-side guard support)

**Dependencies:** Units 1, 2, 3.

**Files:**
- Modify: `src/locg/cli.py` (add `collection` subparser with `import`, `export`, `status`, `check`, `doctor` verbs)
- Modify: `src/locg/commands.py` (add `cmd_collection_import`, `cmd_collection_export`, `cmd_collection_status`, `cmd_collection_check`, `cmd_collection_doctor`)
- Test: `tests/test_collection_commands.py`

**Approach:**
- `locg collection import <path>` → `cmd_collection_import(path) -> dict` returning `{added, updated, untouched, reconciled, possibly_removed, behavioral_drift_count, warnings}`.
- `locg collection export [--out <path>]` → `cmd_collection_export(out_path) -> dict` returning `{csv_path, notes_md_path, ready_count, manual_variant_count, manual_series_count, oldest_pending_days}`.
- `locg collection status [--verbose]` → returns `{last_full_import, last_import_source, row_count, cache_age_days, pending_push_count, oldest_pending_days, locg_cli_version, schema_version}`. With `--verbose`: also `{agent_win_count, locg_export_count, needs_manual_variant_count, needs_manual_series_canonical_count, median_agent_win_age_days, reconciliation_success_rate_last_5_imports, behavioral_drift_events_last_5_imports}`. The verbose metrics close F9's lag-window observability gap — without them you cannot detect cache rot.
- `locg collection check --series <s> --issue <i> [--variant <v>] [--year <y>]` (or batch via stdin JSON) → returns per-query `{match_status, full_title_matched, cache_age_days}`. Skill rewrite (Unit 9) consumes this.
- `locg collection doctor` → prints a stepped first-run walkthrough (closes F2 from deepening review): (1) link to LOCG's "Export My Comics" page, (2) where the xlsx downloads, (3) the exact `locg collection import <path>` command, (4) optional Metron credentials setup. Runs the same checks `status` does and explains the next remediation. R65's empty-cache error from Unit 8 references this command by name: `"Cache empty — run 'locg collection doctor' for setup instructions."`
- Default out: `~/Downloads/locg-bulk-import-<YYYY-MM-DD-HHMMSS>.csv` (per resolved O2).
- All commands respect the shared `--pretty/--verbose/--debug` common parent.
- JSON-by-default output (matches existing `cmd_*` style); `--pretty` enables human-readable summary.

**Patterns to follow:**
- Existing subparser nesting in `src/locg/cli.py` (the `cache` and `collection` groups already use `dest=...`).

**Test scenarios:**
- Happy path: `locg collection import <fixture.xlsx>` returns success dict with non-zero `added` on empty cache.
- Happy path: `locg collection export` returns CSV + notes paths, both files exist on disk.
- Happy path: `locg collection status` on a populated cache returns non-null `last_full_import` and `row_count > 0`.
- Happy path: `locg collection status --verbose` returns the extended observability metrics dict.
- Happy path: `locg collection check --series X --issue 1` on a cached row returns `match_status: "in_collection"`.
- Happy path: `locg collection doctor` on an empty cache prints the stepped walkthrough and exits 0.
- Edge case: `import` with malformed xlsx returns non-zero exit and prints clear error.
- Edge case: `import` when `migration_in_progress=true` returns exit 1 with recovery instructions.
- Edge case: `export` with empty pending queue returns success dict with `ready_count=0`.
- Edge case: `status` on a never-initialized cache returns `last_full_import: null`, `row_count: 0`.
- Edge case: `check` on a comic not in cache returns `match_status: "not_in_cache"` AND surfaces `cache_age_days` so the caller can apply F1's stale-cache downgrade.
- Error path: `import` of nonexistent file returns exit 2 with "file not found".
- Integration: full pipeline test — empty cache → `doctor` (prints walkthrough) → `import xlsx` → `status --verbose` (shows metrics) → `check` (cache hit) → `export csv` → assert CSV is empty (all rows just imported are already `pushed_to_locg_at != null`).

**Verification:** All commands run cleanly via `PYTHONPATH=src python3 -m locg collection ...` against a fixture.

---

### Phase 2 — Win Recording, Metron, Series Resolution (`locg-cli`)

- [ ] **Unit 5: Metron wrapper (mokkari)**

**Target repo:** `locg-cli`

**Goal:** Thin mokkari wrapper for series + issue lookup. Used by Unit 6 to populate Release Date and best-guess canonical Series Name.

**Requirements:** R51, R52, R53, R54, R55, R62

**Dependencies:** None (independent of cache work).

**Files:**
- Create: `src/locg/metron.py`
- Modify: `pyproject.toml` (add `mokkari>=3.0` to deps)
- Modify: `src/locg/config.py` (read `METRON_USERNAME`, `METRON_PASSWORD` from `~/.config/locg/.env`)
- Test: `tests/test_metron.py`

**Approach:**
- Lazy client init — only construct mokkari session when first method called; surface a clear "credentials missing" error when env vars absent.
- `lookup_issue(series_query, issue_number) -> dict | None` returns `{metron_id, cover_date, store_date, series_year_began, series_year_end}`.
- `format_series_name(series_data) -> str` per R62: `"{name} ({year_began} - {end_year})"` where `end_year` = `year_end` if non-null else `"Present"`.
- Rate-limit handling delegated to mokkari's local retry (R55); batch lookups during win recording rather than spreading them.
- All methods are non-blocking — caught exceptions return `None` so the caller falls through to the manual queue (R53).

**Patterns to follow:**
- `src/locg/client.py` lazy-init style for the Playwright context.

**Test scenarios:**
- Happy path: mocked mokkari client returns issue data; wrapper returns expected dict shape.
- Happy path: `format_series_name` produces `"Fantastic Four (1961 - 1996)"` for a finite series and `"Spawn (1992 - Present)"` for ongoing.
- Edge case: missing `METRON_USERNAME`/`PASSWORD` raises a clear, named error on first call (not at import).
- Edge case: mokkari rate-limit exception is swallowed; method returns `None`.
- Edge case: 404 / no-match returns `None` (does not raise).
- Integration: end-to-end credentials test guarded by env-var presence — skip when not set; otherwise verify a known issue ID round-trips.

**Verification:** A known Marvel issue ID returns populated dict with both `cover_date` and `store_date`.

---

- [ ] **Unit 6: `locg collection record-win` CLI subcommand**

**Target repo:** `locg-cli`

**Goal:** Accept a Gixen-derived JSON blob (batch of wins) on stdin or `--from-gixen-json <path>`, resolve canonical Series Name via the R36 chain, write rows to LocalStore with appropriate manual flags.

**Requirements:** R8, R32, R36, R43

**Dependencies:** Units 1, 5.

**Files:**
- Modify: `src/locg/cli.py` (add `collection record-win` verb)
- Modify: `src/locg/commands.py` (add `cmd_collection_record_win`)
- Modify: `src/locg/collection_cache.py` (add `record_win()` method)
- Test: `tests/test_collection_commands.py`

**Approach:**
- Stdin/file accepts JSON list: `[{item_id, current_bid, end_date_iso, identify_data: {series, issue, year, variant_text?}}, ...]`.
- For each win, run the R36 resolution chain:
  1. `series_name_index` lookup using normalized key (strip `(Vol. N)`, year-range parens) — high confidence.
  2. `metron.lookup_issue` → `format_series_name` — medium confidence, set best-guess.
  3. Manual fallback — bare series name, set `needs_manual_series_canonical=true`.
- Variant handling per R32: exact `full_title` cache hit → high; known suffix pattern (`newsstand` → `Newsstand Edition`) → medium; otherwise low, set `needs_manual_variant=true`.
- Write rows with `source="agent_win"`, `local_added_at=now()` (microsecond), `local_added_seq=<per-process monotonic counter>` (closes the deepening-pass concern about microsecond ties on APFS — counter guarantees deterministic ordering within a batch), `pushed_to_locg_at=null`, `gixen_item_id=item_id`.
- **Chunked commit to bound blast radius on large batches** (closes F7 from deepening review): internally split the input list into chunks of 25 rows. For each chunk: do all Metron lookups for that chunk (subject to mokkari's local retry on rate limit), acquire `flock`, set `migration_in_progress`, write the chunk, clear flag, release `flock`. A crash mid-chunk leaves earlier chunks safely committed; only the in-flight chunk is rolled back. Document max-batch limit in the help text (no hard reject — chunking handles arbitrary size, but the user should know latency scales with Metron rate limit).
- Returns `{rows_written, chunks_committed, manual_variant_count, manual_series_count, metron_lookups_attempted, metron_lookups_succeeded, partial_failure: bool}`.

**Patterns to follow:**
- R36's series-resolution chain.

**Test scenarios:**
- Happy path: win for a series in `series_name_index` → row written with confident `Series Name`, no manual flag.
- Happy path: win for a series NOT in index but Metron succeeds → row written with best-guess Series Name, no manual flag.
- Happy path: win for a series NOT in index AND Metron fails → row written with bare series name, `needs_manual_series_canonical=true`.
- Edge case: variant text matches a known suffix pattern (`newsstand`) → `Full Title` ends with `Newsstand Edition`.
- Edge case: variant text present but no known pattern → `Full Title` is bare canonical, `needs_manual_variant=true`.
- Edge case: identical win recorded twice (same `gixen_item_id`) → second record updates the existing row's tracking fields, does not insert a duplicate.
- Edge case: Metron returns no `store_date` but has `cover_date` → `Release Date` uses `cover_date`.
- Edge case: Metron returns neither date → row written with blank `Release Date`, no `needs_manual_variant` (per R66 fix).
- Edge case: two wins in the same batch with identical `local_added_at` microsecond stamps have distinct `local_added_seq` and order deterministically.
- Edge case: 60-row batch chunks into 3 commits of 25/25/10; simulated crash mid-chunk-2 leaves chunks 1 fully committed, chunk 2 rolled back via flag/`.bak.0`.
- Error path: malformed JSON stdin returns exit code 2 with parse error.
- Integration: batch of 5 wins (mix of cache-hit / metron-hit / manual-fallback) writes 5 rows with the expected `needs_manual_*` distribution.

**Verification:** A real `gixen-cli list --json` payload of recent wins flows through `record-win` and produces the expected mix per the user's collection coverage.

---

- [ ] **Unit 7: `locg collection resolve-series` CLI subcommand (user-invoked Playwright) — v1.1 candidate**

**Target repo:** `locg-cli`

**Goal:** User-invoked, Playwright-driven (PER-38 persistent profile, `headless=False`) command to query LOCG live for a canonical Series Name, learn it into `series_name_index`, and reconcile any cache rows with matching `needs_manual_series_canonical=true`.

**Status note (added during deepening):** The deepening review (F10) observed that `sync-from-locg` (Unit 12) achieves the same outcome via a full round-trip: a fresh export will contain the canonical Series Name and reconciliation will learn it. If the user is willing to run any Playwright command, `sync-from-locg` likely covers the use case. Ship Unit 7 only if Phase 3 reveals a concrete narrow story for "user wants one specific series resolved without a full export." Otherwise defer to v1.1 or drop entirely. **Implementer should check with user before starting Unit 7.**

**Requirements:** R37, R61

**Dependencies:** Units 1, 6.

**Files:**
- Modify: `src/locg/cli.py` (add `collection resolve-series` verb)
- Modify: `src/locg/commands.py` (add `cmd_collection_resolve_series`)
- Modify: `src/locg/collection_cache.py` (add `update_series_index` + `reconcile_pending_series`)
- Test: `tests/test_collection_commands.py` (mock the LOCGClient call)

**Approach:**
- Reuse the existing `LOCGClient` Playwright context with `headless=False` overridden for visibility during the manual challenge if needed.
- Search LOCG by the bare series name + (optional year); extract the canonical `Series Name` string from the search result page.
- Update `series_name_index[normalized_key] = canonical_name`.
- Scan cache for rows where `needs_manual_series_canonical=true` AND normalized series matches; rewrite those rows' `series_name` and clear the flag.
- Three-class failure contract (mirrors R63/R64): exit 3 (UI/selector change), exit 4 (Turnstile unsolved), exit 5 (network/IP block).
- **Never invoked by any agent skill** (R37) — opt-in user cleanup tool.

**Patterns to follow:**
- `src/locg/client.py` Playwright persistent-context pattern; PER-38 recipe.

**Test scenarios:**
- Happy path: mocked LOCGClient returns canonical Series Name; index updated; 3 pending rows reconciled.
- Edge case: no search match → index not updated, no reconciliation, exit 0 with "no canonical match found" message.
- Edge case: search returns multiple candidates → present them to user (CLI prompt); only on selection update index.
- Error path: simulated UI selector miss → exit 3 with selector name.
- Error path: simulated Turnstile challenge present → exit 4 with `locg login` guidance.
- Error path: simulated `<title>Restricted</title>` → exit 5 with IP-block guidance.
- Integration: end-to-end run against real LOCG (manual smoke test, not CI) for a series the user has never owned.

**Verification:** A net-new series resolved via this command shows up in `series_name_index`; subsequent `record-win` for that series hits the cache branch.

---

### Phase 3 — Skill Migration (`comic-pipeline`)

- [ ] **Unit 8: First-run bootstrap guard + version-pin enforcement in `/comic:collection-check` and `/comic:collection-add`**

**Target repo:** `comic-pipeline`

**Goal:** Both skills check `last_full_import` AND the installed `locg-cli` version at startup, refusing to operate with a clear remediation when either condition fails. Hard precondition.

**Requirements:** R65 + F3 cross-repo-version-pin (added during deepening)

**Dependencies:** Phase 1 (Units 1–4) released — specifically `locg collection status` from Unit 4.

**Files:**
- Modify: `.claude/skills/collection-check.md`
- Modify: `.claude/skills/collection-add.md`

**Approach:**
- Both skills' first step is a single `locg collection status` shell-out. The response includes `locg_cli_version` (from Unit 4) and `last_full_import`.
- The skill body declares the minimum required `locg-cli` version machine-readably (e.g., a frontmatter field `requires_locg_cli: ">=0.X.Y"`). The skill compares the response's version against this pin and refuses with: `"locg-cli version >=0.X.Y required (installed: 0.A.B). Upgrade via 'cd ~/Projects/locg-cli && pip install -e .' and retry."`
- If `last_full_import` is null, abort with: `"Cache empty — run 'locg collection doctor' for setup instructions."` (References Unit 4's `doctor` command per F2 from deepening review — more actionable than the bare import-command remediation.)
- Both checks happen BEFORE any other skill work (no `/comic:identify`, no Gixen pull) so failure is at the top, not buried mid-orchestration.

**Test scenarios:**
- Test expectation: skill markdown not directly testable; acceptance is the `locg collection status` JSON contract (covered in Unit 4) and manual session tests below.
- Manual: empty cache → both skills refuse with the `doctor` remediation. Populated cache + outdated `locg-cli` → both skills refuse with the upgrade remediation. Populated cache + current `locg-cli` → both skills proceed.

**Verification:** Empty-cache simulation produces the documented `doctor` error; downgraded-CLI simulation produces the documented upgrade error; healthy state proceeds.

---

- [ ] **Unit 9: Rewrite `/comic:collection-check` skill — fully offline**

**Target repo:** `comic-pipeline`

**Goal:** Replace live-LOCG membership check with cache-only lookup. Remove the "Persist back to Gixen" step entirely.

**Requirements:** R38, R39, R40, R41, R42

**Dependencies:** Unit 8.

**Files:**
- Modify: `.claude/skills/collection-check.md`

**Approach:**
- Replace existing `locg lookup` and write-back instructions with a single `locg collection check ...` call per listing (Unit 4 provides the verb).
- Output table includes new `Cache age` column (R40), derived from `last_full_import`. Same value for every row in a single check (per R40).
- **Stale-cache verdict downgrade (closes F1 — the highest-impact deepening finding).** R41 says a "not in cache" result is reported as "Not in collection" confidently. The deepening review surfaced a real money-wasting failure mode: user wins issue X on Monday via `/comic:buy`, doesn't upload until Friday, manually adds another copy via LOCG mobile UI on Wednesday; on Thursday `/comic:buy` runs against another X listing and the cache still says "Not in collection" confidently — the snipe goes through and money is wasted. **Mitigation:** when `cache_age_days > STALE_THRESHOLD_DAYS` (v1 default: 14) AND result is `not_in_cache`, downgrade verdict to `"⚠️ Not in cache (cache N days stale — manual LOCG check recommended before bidding)"` rather than confident-negative. This is a buy-time decision input, not just an after-the-fact banner. The threshold is configurable via `~/.config/locg/.env`.
- After the table, also surface the two existing banners:
  - If `cache_age_days > 14` → soft suggestion to re-export from LOCG.
  - Pending-push status line: `N rows pending push to LOCG; oldest pending = X days.` Escalate when `X>21` or `N>25`.
- Remove all references to `locg_id` resolution and `gixen-cli locg link`.
- Variant flag-through per R42: when canonical matched but listing variant text not disambiguated, append `⚠️ canonical match — listing variant not disambiguated`.

**Patterns to follow:**
- Existing skill markdown structure (YAML frontmatter `name`/`description` + numbered steps + output table format).

**Test scenarios:**
- Test expectation: skill markdown not directly testable. Acceptance is:
  - The `locg collection check` CLI is added to Unit 4 (or this unit) with its own unit coverage.
  - Manual smoke test: a session with 5 known-collection + 5 known-not-in-collection comics produces the right verdicts.
  - Manual smoke test: a 0-day-old cache shows `Cache age: <1 day`; a manipulated `last_full_import` 20 days ago triggers the staleness banner.

**Verification:** Skill no longer references `locg lookup`, `gixen-cli locg link`, or `locg_id` anywhere; manual session test agrees with cache contents.

---

- [ ] **Unit 10: Rewrite `/comic:collection-add` skill — shell out to `locg collection record-win` + `export`**

**Target repo:** `comic-pipeline`

**Goal:** Replace inline Playwright per-comic with batched record-win + a single export call. Skill no longer requires LOCG network access.

**Requirements:** R43, R44, R45

**Dependencies:** Phase 2 (Units 5–6) released; Unit 8.

**Files:**
- Modify: `.claude/skills/collection-add.md`

**Approach:**
- New skill flow:
  1. Bootstrap check (Unit 8).
  2. Pull wins from `gixen-cli list --json` filtered to `status` contains `WON` and `time_to_end == "ENDED"`.
  3. Build JSON list per the Unit 6 stdin schema (per win: `{item_id, current_bid, end_date_iso, identify_data}`).
  4. `cat <json> | locg collection record-win --from-gixen-json -` — single subprocess for the entire batch.
  5. `locg collection export` — generate CSV + `.notes.md`.
  6. Report per R44: rows added, ready to push, needs manual variant, needs manual series canonical, total pending, oldest pending age, upload instruction.
- Remove all inline Playwright Python from the skill body.
- Remove `gixen-cli locg link` write-back instructions (the command doesn't exist upstream anyway).

**Test scenarios:**
- Test expectation: skill markdown not directly testable. Acceptance:
  - `locg collection record-win` + `export` already covered in Units 4 and 6.
  - Manual: a session with 3 fresh wins produces a 3-row CSV (or fewer if any flagged manual) and the `.notes.md` reports the expected counts.

**Verification:** Skill runs end-to-end against real Gixen wins without invoking Playwright; CSV ready to upload to LOCG.

---

- [ ] **Unit 11: `/comic:snipe-add` + `/comic:buy` orchestrator updates**

**Target repo:** `comic-pipeline`

**Goal:** Stop carrying `locg_id` into Gixen snipes; update buy-orchestrator gate copy; remove all "what if LOCG unreachable" branches.

**Requirements:** R46, R47, R48, R49, R50

**Dependencies:** Units 9, 10.

**Files:**
- Modify: `.claude/skills/snipe-add.md`
- Modify: `.claude/skills/buy.md`

**Approach:**
- `snipe-add`: drop `--locg-id` flag from the documented `gixen-cli add` invocation. **Correction from deepening review (F4):** the flag was NOT aspirational — it was a real `gixen-cli` command removed during the PER-34 plugin extraction (verified in `~/Projects/gixen-cli/CHANGELOG.md` and the PER-34 refactor plan). The skill text simply wasn't audited when the flag was deleted. This is an institutional gap, not a one-off.
- **Skill-vs-CLI audit (closes F4 — broader than just the two known flags).** Add a verification step to this unit: grep all `.claude/skills/*.md` for any `gixen-cli` or `locg` command/flag references, cross-check each against the current `gixen-cli --help` and `locg --help` output, and remove or update any that no longer exist. Output the full audit in the PR description so reviewers can confirm coverage. Recommend institutionalizing this audit as a periodic check (e.g., a `make audit-skills` target) — out of scope to build here, but flag in the PR for follow-up.
- `buy.md`: Step 2 gate copy → `"membership from local cache; LOCG state may lag by up to N days."` Remove the "if LOCG unreachable" branch entirely.
- At end of `buy.md` flow when `/comic:collection-add` ran, append: `"N rows pending push to LOCG; run 'locg collection export' and upload at your convenience."`

**Test scenarios:**
- Test expectation: none — markdown updates only; acceptance is text grep + a manual session test that snipes still create without `--locg-id` and buy completes without any CF-unreachable branch firing.

**Verification:** Grep confirms no remaining `--locg-id` or `locg_id` references in skill files; manual buy flow completes with the new gate copy.

---

### Phase 4 — Optional Playwright Sync (`locg-cli`, gated)

> **Phase 4 entry criteria (HARD GATE — closes F6 from deepening review).** Do not start Unit 12 or Unit 13 until ALL of the following are true. Implementers running the plan top-to-bottom **must explicitly check this box before proceeding**:
>
> - [ ] Phase 3 (Units 8–11) has been merged AND used in real `/comic:buy` sessions for at least 2 weeks with no cache-corruption incidents.
> - [ ] User has explicitly opted into Playwright-driven sync (this is not the default workflow; the manual upload/download floor remains documented).
> - [ ] For Unit 13 specifically: the empirical validation gate inside Unit 13 (manual `preset → upload → preview → confirm` walkthrough against live LOCG with a fresh persistent profile) has been completed and either (a) the full flow succeeded — proceed with `--auto-confirm` support, or (b) any step failed — Unit 13 ships preview-only with `--auto-confirm` rejected at the CLI layer.
> - [ ] Regression guard: a grep across `.claude/skills/*.md` confirms no skill invokes `locg collection sync-from-locg` or `locg collection sync-to-locg`. These commands are user-invoked only. A skill that shells out to them defeats the entire CF-decoupling premise of the plan.
>
> If any criterion is not met, leave Phase 4 unchecked and revisit after the blocker clears. This is not optional process — the criteria exist because Phase 4 commands re-introduce CF dependency on an opt-in basis, and the failure mode is "agent loop re-stalls inside Phase 4 helpers" if accidentally invoked from a skill.


- [ ] **Unit 12: `locg collection sync-from-locg`**

**Target repo:** `locg-cli`

**Goal:** User-invoked, Playwright-driven command that loads LOCG, clicks "Export My Comics", downloads the xlsx, then invokes `locg collection import` on it. Cron-safe with bounded retries and a three-class failure contract.

**Requirements:** R63

**Dependencies:** Phase 1 complete, real-world hands-on validation of the LOCG export UI selectors.

**Files:**
- Modify: `src/locg/cli.py` (add `collection sync-from-locg` verb)
- Modify: `src/locg/commands.py` (add `cmd_collection_sync_from_locg`)
- Modify: `src/locg/client.py` (add `download_excel_export()` method)
- Test: `tests/test_collection_commands.py` (mock client)

**Approach:**
- `headless=False` (allow user to see Turnstile if it appears).
- Navigate to LOCG export page; click export; wait for download; pass the resulting `.xlsx` to `cmd_collection_import`.
- Three-class failure exits: 3 (UI/selector change), 4 (Turnstile unsolved), 5 (network/IP block, `<title>Restricted</title>`).
- Bounded retry: stop after 2 consecutive non-success exits per invocation.
- Cron-safety: verify UID matches `~/.config/locg/playwright-profile/` owner; verify dir mode 0700; refuse to run otherwise.
- **Never invoked by any agent skill.**

**Test scenarios:**
- Happy path (mocked): client returns xlsx path → import runs → success summary.
- Edge case: profile dir mode > 0700 → refuses with mode error.
- Edge case: profile dir owner mismatch → refuses with owner error.
- Error path: simulated selector miss → exit 3 with selector name.
- Error path: simulated Turnstile present → exit 4 with `locg login` guidance.
- Error path: simulated `<title>Restricted</title>` → exit 5 with IP-block guidance.
- Edge case: two consecutive failures → third invocation refuses to run with "retry budget exhausted" message.

**Verification:** Manual smoke test from the user's normal workstation produces a fresh xlsx + import summary.

---

- [ ] **Unit 13: `locg collection sync-to-locg` (gated)**

**Target repo:** `locg-cli`

**Goal:** User-invoked, Playwright-driven command that navigates LOCG's Bulk Import page, uploads the most recent export CSV (or a `--csv <path>` override), clicks through preset/preview, **pauses at the confirm screen by default** for the user to click, then scrapes the success page for counts.

**Requirements:** R64

**Dependencies:** Unit 12; empirical validation gate (see below).

**Files:**
- Modify: `src/locg/cli.py` (add `collection sync-to-locg` verb + `--auto-confirm` flag)
- Modify: `src/locg/commands.py` (add `cmd_collection_sync_to_locg`)
- Modify: `src/locg/client.py` (add `upload_bulk_import_csv()` method)
- Test: `tests/test_collection_commands.py` (mock client)

**Empirical validation gate (hard precondition before shipping):**
- Manually run the full sequence `preset → upload → preview → confirm` against LOCG with a fresh persistent profile.
- Verify `cf_clearance` lifetime survives all four steps.
- If multi-step CF challenges break the flow, **R64 ships preview-only**: auto-stop at the confirm screen, user clicks "Yes, I'm Ready" manually. Do not ship full automation under this failure mode.

**Approach:**
- `headless=False`.
- Defaults: CSV path = the most recent `locg collection export` output (tracked in cache metadata or `~/.cache/locg/`).
- Default behavior: pause at preview/confirm. User clicks "Yes, I'm Ready". Auto-confirm requires explicit `--auto-confirm`.
- After confirm: scrape success page for `Added to Collection`, `Set ...`, `Not Found` counts; persist a record (where? — likely the `import-history.jsonl` audit log, with type `sync-to-locg`).
- Same three-class failure contract as R63.
- **Never invoked by any agent skill.**

**Test scenarios:**
- Happy path (mocked, preview-only mode): upload step completes, command pauses for user, success-page counts parsed correctly.
- Happy path (mocked, --auto-confirm): full pipeline completes without user input.
- Edge case: empirical gate fails → command shipped as preview-only, `--auto-confirm` flag rejects with "auto-confirm disabled — manual click required at preview screen" until gate passes.
- Error path: simulated preview-page UI change → exit 3.
- Error path: simulated mid-flow Turnstile → exit 4.
- Error path: simulated IP block → exit 5.
- Edge case: CSV path doesn't exist → exit 2 with "no recent export found; run `locg collection export` first".

**Verification:** Manual end-to-end push of a small CSV (1–3 rows) against LOCG followed by re-export + import reconciles cleanly.

---

### Phase 5 — Cleanup

- [ ] **Unit 14: Plugin route deprecation + solutions doc promotion**

**Target repo:** `comic-pipeline`

**Goal:** Mark the now-dead-for-new-snipes `POST /api/bids/{item_id}/comics/locg` deprecated; promote the brainstorm Appendix into a permanent solutions doc.

**Requirements:** (none — operational cleanup)

**Dependencies:** Phase 3 released and stable for ≥2 weeks in real use.

**Files:**
- Modify: `plugins/gixen-overlay/src/gixen_overlay/routes.py` (update docstring on the `POST /api/bids/{item_id}/comics/locg` handler with deprecation notice + v2 removal pointer)
- Create: `docs/solutions/integration-issues/locg-bulk-import-recipe-2026-05-22.md` (lift the brainstorm Appendix + R21–R31 recipe + Test 1–6 evidence into a permanent solutions doc with the standard frontmatter)

**Approach:**
- Route stays functional for legacy snipes (R47 preserves existing `bids.locg_id` values).
- Solutions doc structure follows `docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md` (title, date, category, module, problem_type, severity, related_components, tags).

**Test scenarios:**
- Test expectation: none — documentation and docstring updates only.

**Verification:** Solutions doc visible at the expected path; deprecation comment visible in route docstring.

---

## System-Wide Impact

- **Interaction graph:** Skills move from "skill → Playwright → LOCG" to "skill → CLI subprocess → cache file". The plugin route `POST /api/bids/{item_id}/comics/locg` becomes legacy-only. No new background jobs, no new middleware.
- **Error propagation:** All CF/network failures now surface in user-invoked sync commands (R63/R64) with distinct exit codes (3/4/5). The hot path (`/comic:buy`) has no CF-blocking code path — any CF failure that does occur is in the optional sync commands and is non-fatal to the buy flow.
- **State lifecycle risks:**
  - Cache `migration_in_progress` flag + `.bak` snapshot guards against crash-mid-import corruption (R68).
  - Reconciliation may false-positive across volume reboots (L8) — bounded by R60's strict `(Vol. N)` mismatch rule; audit log is the user's defense.
  - Practice drift on manual upload (L6) — surface a "last successful manual sync N days ago" hint once telemetry exists.
- **API surface parity:** No external API surfaces change in v1. `locg-cli` adds a new top-level command family (`collection`); existing commands unchanged. `gixen-cli` is untouched.
- **Integration coverage:** End-to-end cache-to-LOCG round-trip exercising the validated bulk-import recipe (Tests 1–6) is the canonical integration test. Suite must include at least one full round-trip against a real fixture xlsx.
- **Unchanged invariants:**
  - `bids.locg_id` column in Gixen DB stays — existing values preserved (R47).
  - `~/.cache/locg/ids.json` (PER-71's `locg lookup` ID cache) is untouched — it serves a different purpose (lookup-time slug resolution) than the new `collection.json` (membership store). Both live as siblings.
  - The existing `LOCGClient` Playwright + persistent profile (PER-38) is reused by R37/R63/R64 — no breaking change.
  - The `/comic:fmv` and `/comic:identify` skills are unchanged.

## Risks & Dependencies

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LOCG silently changes its 21-column Excel header order | Low | High | Header exact-match check rejects with a clear error before merge; user resorts to manual edit + retry. Failure mode is loud, not silent. |
| LOCG's bulk-import recipe drifts (the validated 2026-05-22 recipe stops working) | Low–Medium | High | Six empirical tests documented in brainstorm Appendix; promote to solutions doc (Unit 14). Round-trip integration test catches regressions in our generator; LOCG-side drift surfaces as `Not Found` rows in the re-export. |
| Reconciliation false-positives across same-year volume reboots (L8) | Low | Medium | R60's strict `(Vol. N)` mismatch rule + audit log (`import-history.jsonl`). Periodic user review of audit log. |
| Mokkari rate-limit hit during a large win batch | Medium | Low | Mokkari local retry; rows that fail Metron flow to manual fallback non-blocking (R66). |
| Cache crash mid-import corrupts state | Low | High | `migration_in_progress` flag set/cleared inside a single atomic write (no on-disk "merged-but-flagged" state); N=3 rolling `.bak` snapshots; `fcntl.flock` for concurrent-writer safety; `fsync` on tempfile fd + parent dir. All tightened during deepening pass. Refuse-to-load with smart auto-clear when `.bak.0.last_full_import == cache.last_full_import` (killed-before-merge case is a non-event). |
| **Stale cache returns confident "Not in collection" → snipe goes through → money wasted** | Medium | High | F1 fix in Unit 9: when `cache_age_days > 14` AND result is `not_in_cache`, downgrade verdict to `⚠️ Not in cache (cache N days stale — manual LOCG check recommended)`. This converts a silent wrong-answer into a buy-time decision input. Threshold configurable. |
| Concurrent `record-win` and `sync-from-locg` (cron) corrupt cache | Medium (with Phase 4) | High | `fcntl.flock(LOCK_EX)` on `~/.cache/locg/collection.lock` for the full read-mutate-write cycle. Brief blocking on contention; no lost updates. |
| Skill silently invokes a removed `gixen-cli` flag (the pattern that put us here) | Medium | Medium | F4 audit in Unit 11: grep all skills against current `--help` output, surface findings in PR. Recommend institutionalizing as `make audit-skills`. |
| Implementer accidentally ships Phase 4 commands before the empirical gate | Medium | High (re-introduces CF dependency on the hot path if any skill invokes them) | Hard gate hoisted above the unit table; explicit checkbox criteria including a regression grep ensuring no skill invokes the sync commands. |
| Behavioral drift in LOCG bulk-import (silent default mutation of user-managed columns) | Low | Medium | F5 fix in Unit 2: per-row checksum of user-managed columns pre/post import; mismatches log `type: "behavioral_drift"` audit record. Catches the class of regression that the 21-column header check cannot see. |
| Unbounded `record-win` batch blocks for tens of minutes under Metron rate-limit + locks cache for the duration | Low | Medium | F7 fix in Unit 6: chunked commit in 25-row batches. Crash mid-batch loses only the in-flight chunk; earlier chunks safely committed. |
| R64 multi-step CF challenge breaks full automation | Medium | Medium | Empirical validation gate before shipping; fallback path is preview-only (user clicks confirm). Manual upload remains the documented floor. |
| User skips R65 first-run bootstrap, runs `/comic:buy` against an empty cache | Medium | High (every comic flags as "not in collection") | Both skills refuse to run with empty cache + clear remediation (R65 → Unit 8). |
| Aspirational gixen-cli flags (`--locg-id`, `gixen-cli locg link`) silently kept in skill text | High before this plan | Medium | R38/R46 + Unit 9/11 explicitly remove them. |
| Cross-repo install drift (user's editable `locg-cli` lags behind needed CLI verbs) | Medium | Medium | Each skill rewrite (Units 9–11) lands AFTER the corresponding `locg-cli` release. Document required `locg-cli` version in skill frontmatter or skill body. |
| Practice drift if R63/R64 work and then later break (L6) | Medium | Low | Surface "last successful manual sync N days ago" hint once data exists; revisit in v2 if drift incidents accumulate. |

## Documentation / Operational Notes

- **First-run setup (one-time):**
  1. Export current collection from LOCG (`Export My Comics`) → save xlsx.
  2. Run `locg collection import <path>.xlsx` to seed cache.
  3. (Optional) Set `METRON_USERNAME` + `METRON_PASSWORD` in `~/.config/locg/.env` for Release Date resolution.
  4. Confirm cache populated: `locg collection status` shows `row_count > 0` and a recent `last_full_import`.
- **Round-trip rhythm (after each `/comic:collection-add` session):**
  1. Skill output instructs: `Upload <csv> via LOCG Bulk Import, then re-export and run 'locg collection import <new-xlsx>'`.
  2. User uploads, re-exports, runs import. Done.
- **Audit log:** `~/.cache/locg/import-history.jsonl` records every reconciliation and warning. Worth tailing periodically to sanity-check unattended (cron) imports.
- **Phase 4 (R63/R64) operational notes:**
  - Cron entry: `0 4 * * * locg collection sync-from-locg && locg collection sync-to-locg` (illustrative).
  - When CF clearance expires (typically every ~30 days), exit 4 surfaces; remedy is `locg login` interactively to refresh the profile.
- **Plugin route deprecation (Unit 14):** Document in `plugins/gixen-overlay/CHANGELOG.md` (if it exists; otherwise commit message) the v2 removal pointer.

## Sources & References

- **Origin document:** `docs/brainstorms/2026-05-22-locg-collection-cache-requirements.md`
- **Code patterns:**
  - `locg-cli` repo: `src/locg/cache.py`, `src/locg/config.py`, `src/locg/cli.py`, `src/locg/commands.py`, `src/locg/client.py`, `tests/conftest.py`
  - `comic-pipeline` repo: `.claude/skills/collection-check.md`, `.claude/skills/collection-add.md`, `.claude/skills/snipe-add.md`, `.claude/skills/buy.md`, `plugins/gixen-overlay/src/gixen_overlay/db.py` (marker-row pattern), `plugins/gixen-overlay/src/gixen_overlay/routes.py:95`
- **Real fixtures available in this repo for `locg-cli` test reuse:**
  - `.context/attachments/Jhv32a/ComicGeeks-2026-05-21-12-08-13.xlsx` (and `cEMcVJ/`, `6EZ3jf/`, `dksNRt/` siblings) — real LOCG 21-column exports.
  - `.context/locg-import-test.csv` through `.context/locg-import-test-6-batch.csv` — six validated bulk-import CSVs from the 2026-05-22 LOCG experiments.
- **Institutional learnings:**
  - `docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md` (migration disciplines applicable to cache schema evolution)
  - `docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md` (single-owner-of-invariants discipline)
- **Related Linear identifiers (referenced in brainstorm):** PER-38 (Playwright + persistent profile), PER-71 (CF-retry warm-up + `~/.cache/locg/ids.json`).
- **External docs:**
  - `openpyxl` — read-only mode (`load_workbook(filename, read_only=True, data_only=True)`)
  - `mokkari` — Metron API client; `Series.year_began`, `Series.year_end: Optional[int]`
