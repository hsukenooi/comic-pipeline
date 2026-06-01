---
date: 2026-05-22
topic: locg-collection-cache
---

# Local Collection Cache to Decouple `/comic:buy` from LOCG / Cloudflare

## Problem Frame

`/comic:buy` orchestrates `/comic:identify` → `/comic:collection-check` → `/comic:fmv` → `/comic:snipe-add`, with `/comic:collection-add` running post-win. Every step except `collection-check` and `collection-add` is already CF-independent. The two collection steps depend on live LOCG access via `locg-cli` (Playwright + persistent Chrome profile, PER-38).

When the workspace's egress IP is blocked by Cloudflare's perimeter — which happened on 2026-05-21 with IP `202.83.99.131` returning `<title>Restricted</title>` from the LOCG homepage — `/comic:collection-check` cannot answer "do I already own this?" and the agent loop stalls. The stall is unrecoverable in-session (PER-71's CF-retry warm-up does not help against IP-level blocks) and the existing skill has no documented fallback. A real session stalled for 176 messages of thrashing before the agent prompted the user to skip collection-check.

LOCG's bulk Excel import/export endpoint is browser-mediated and not subject to the same IP block — the user can manually export + upload from a non-blocked network. This creates an asymmetry the design exploits: LOCG is reachable to *the user* via web UI, but not to *the agent* via API. Pushing the agent's LOCG dependency off the hot path and onto user-mediated sync removes the failure mode entirely.

A related observation from PER-71's brainstorm explicitly deferred a local collection cache: "the cache may come later as a separate effort." This is that effort.

## Goals

- G1. `/comic:collection-check` answers membership entirely from local data — never blocks on LOCG or Cloudflare.
- G2. `/comic:collection-add` records won comics to LocalStore immediately at win-time. No live LOCG call required.
- G3. Pushing the agent's local additions to LOCG is **user-mediated** (manual CSV upload via LOCG's Bulk Import page) and works regardless of whether the agent's network can reach LOCG.
- G4. The cache learns LOCG's canonical naming conventions over time from each round-trip export, so subsequent pushes get more precise.
- G5. Variant edge cases the agent can't confidently resolve are surfaced clearly with enough context for the user to fix manually, rather than silently producing wrong matches.

## Non-Goals (Explicit Out-of-Scope)

- N1. **No phone interface, no read UI.** Cache is a JSON file. LOCG's own mobile site remains the user's read surface.
- N2. **No two-way conflict resolution.** LOCG remains the canonical source of truth. Local edits to fields LOCG also tracks are not supported — only adds-from-wins.
- N3. **No automated variant disambiguation for unknown patterns.** Variants the agent can't resolve with high confidence are flagged for manual handling, not guessed.
- N4. **No automatic egress workaround on the hot path.** No Tailscale tunnel, residential proxy, or `cf_clearance` cookie import as a hot-path requirement. The cache makes egress immaterial for `/comic:buy` and its sub-skills — no skill invokes Playwright. Programmatic sync via Playwright + persistent Chrome profile (R63, R64) is permitted as best-effort **user-invoked** CLI commands that leverage existing PER-38 infrastructure. Sync commands fail gracefully when CF clearance expires; the manual upload/download workflow remains the documented floor.
- N5. **No agent-side comic-collection app** — not aggregating Gixen snipes, FMV, and collection in a unified view. That was the bigger reframing considered and rejected for v1.
- N6. **Metron is optional metadata enrichment, not the canonical identity layer.** The cache schema is keyed on LOCG canonical fields. Metron `metron_id` is a useful optional column but not required.
- N7. **Pull list and wanted list management stays manual.** The cache tracks "In Collection" only. Pull/wish list operations remain in LOCG web UI.

## Architecture Overview

**LocalStore is the agent's database. LOCG is a sync peer reached via user-mediated Excel/CSV round-trips. Metron provides release-date metadata and an optional stable identifier.**

```
                                          ┌──────────────────────┐
   ┌─────────────────┐   import          │  User's browser      │
   │  LOCG Excel     │ ─────────────────▶│  uploads/downloads   │
   │  export (.xlsx) │                    │  via LOCG web UI     │
   └─────────────────┘                    └──────────────────────┘
            ▲                                       │
            │ user clicks "Export My Comics"        │
            │                                       │ user clicks "Ready, I'm Ready"
            │                                       ▼
   ┌─────────────────┐                    ┌──────────────────────┐
   │   LOCG          │                    │  Bulk Import CSV     │
   │   (canonical)   │                    │  (generated locally) │
   └─────────────────┘                    └──────────────────────┘
            │                                       ▲
            │ user manages pull/wish list           │
            │ manually via web UI                   │ agent generates
            ▼                                       │ from pending rows
   ┌──────────────────────────────────────────────────────────────┐
   │                       LocalStore                              │
   │  ~/.cache/locg/collection.json                                │
   │  - one row per LOCG canonical (series, Full Title)            │
   │  - tracks pending pushes + last confirmed-in-LOCG timestamps  │
   │  - includes Metron metadata when resolved                     │
   └──────────────────────────────────────────────────────────────┘
            ▲                                       ▲
            │ writes from wins                      │ reads for membership
            │                                       │
   ┌─────────────────┐                    ┌──────────────────────┐
   │ /comic:         │                    │  /comic:             │
   │ collection-add  │                    │  collection-check    │
   └─────────────────┘                    └──────────────────────┘
            ▲
            │
   ┌─────────────────┐                    ┌──────────────────────┐
   │   Gixen wins    │                    │   Metron (mokkari)   │
   │ (cli list)      │                    │   release dates +    │
   │                 │                    │   metron_id (opt)    │
   └─────────────────┘                    └──────────────────────┘
```

## Requirements

### Storage

- R1. Cache lives at `~/.cache/locg/collection.json` alongside the existing `~/.cache/locg/ids.json` (the `locg lookup` ID cache from PER-71).
- R2. Cache is owned by `locg-cli`. The `comic-pipeline` skills read/write it through `locg-cli` commands — not by direct file manipulation. This keeps a single owner for cache invariants.
- R3. Cache file is JSON for human inspectability and minimal tooling. No SQLite or other format in v1.
- R4. Top-level shape: `{ "schema_version": 1, "last_full_import": "<ISO timestamp>", "last_import_source": "<filename or 'api'>", "series_name_index": { ... }, "comics": [ ... ] }`. `series_name_index` is defined in R61.
- R5. Each row in `comics` mirrors LOCG's 21-column Excel export schema verbatim, plus tracking and optional enrichment fields. See R8 for the full schema. **All 21 LOCG columns are stored for round-trip fidelity, even though agent skills only read a subset (membership flags, identity tuple) — user-managed fields (Condition, Notes, Tags, Storage Box, Owner, Grading, Grading Company) are written by the user via LOCG web UI and must round-trip through cache imports/exports without being silently wiped.**

### Cache Row Schema

- R6. The 21 columns from LOCG's export are stored verbatim as JSON fields with snake-case names: `publisher_name`, `series_name`, `full_title`, `release_date`, `in_collection`, `in_wish_list`, `marked_read`, `my_rating`, `media_format`, `price_paid`, `date_purchased`, `condition`, `notes`, `tags`, `storage_box`, `owner`, `purchase_store`, `signature`, `slabbing`, `grading`, `grading_company`.
- R7. Boolean-coded columns (`in_collection`, `in_wish_list`, `marked_read`, `signature`, `slabbing`) are stored as integers (`0` or `1`) to match LOCG's wire format and avoid round-trip drift.
- R8. Each row additionally carries tracking and enrichment fields:
  - `local_added_at` — ISO timestamp when this row was first written to LocalStore (e.g., when a `/comic:collection-add` win was recorded).
  - `pushed_to_locg_at` — ISO timestamp of the last LOCG Excel export that confirmed this row. `null` until first confirmed.
  - `last_seen_in_export_at` — refresh on every import, even if the row was unchanged. Used to detect rows the user removed from LOCG manually.
  - `source` — `"locg_export"` (came from an Excel import) or `"agent_win"` (came from `/comic:collection-add`) or `"manual"` (user-edited).
  - `needs_manual_variant` — boolean; `true` when the agent couldn't resolve LOCG's variant Full Title with high confidence and is asking the user to add manually.
  - `needs_manual_series_canonical` — boolean; `true` when the agent couldn't determine LOCG's canonical Series Name (neither cache index nor Metron resolved it). Surfaced separately from `needs_manual_variant` because the user remediation differs: seed the canonical Series Name once for the series (then all future issues resolve cleanly) vs resolve a specific variant cover.
  - `metron_id` — optional integer; populated lazily when Metron lookup succeeds for the row.
  - `gixen_item_id` — optional string; the eBay item ID that won this comic, for traceability.
- R9. Row identity for dedupe within the cache is `(publisher_name, series_name, full_title, release_date)` — the same tuple LOCG's bulk-import matcher uses for confirmed rows. Two rows differing only in any other field (e.g., grade, price) are still the "same comic" for membership purposes. **On import, the reconciliation pass (R60) may rewrite the identity tuple of best-guess `agent_win` rows (those with `needs_manual_variant=true` or `needs_manual_series_canonical=true`) to match incoming LOCG-canonical rows. After reconciliation the tuple is again authoritative.** This handles LOCG's silent corrections to Release Date (Test 5) and Full Title changes from manual variant handling (R34) without leaving orphan cache rows.

### Sync from LOCG → LocalStore (read sync, primary path)

- R10. New `locg collection import <path-to-xlsx>` command. Parses LOCG's native Excel export and merges into LocalStore. Adds `openpyxl` as a new `locg-cli` dependency (read-only Excel parsing, no native binary requirement, MIT-licensed). Reject files larger than 10 MB before parsing and reject anything whose first sheet header row doesn't exactly match the 21-column LOCG export schema in order.
- R11. Import runs in two phases — **reconciliation first**, then standard merge.

  **Phase 1 — Reconciliation** (per R60): best-guess cache rows (`needs_manual_variant=true` OR `needs_manual_series_canonical=true`) are matched against incoming Excel rows via a relaxed heuristic. Matched rows have their identity tuple migrated to the canonical values and their manual flags cleared.

  **Phase 2 — Standard merge** (non-destructive on agent-tracking fields):
  - Rows present in the Excel that already exist in cache (post-reconciliation identity): update LOCG-owned columns from the file; set `pushed_to_locg_at = now` and `last_seen_in_export_at = now`. Preserve `local_added_at`, `metron_id`, `gixen_item_id`. Manual flags are managed by reconciliation (Phase 1).
  - Rows present in the Excel that are NOT yet in cache (and not picked up by reconciliation): insert with `local_added_at = pushed_to_locg_at = now`, `source = "locg_export"`.
  - Rows in cache that are NOT in this Excel: leave them. If the cache row has `pushed_to_locg_at != null` and was not seen this import, log a warning ("possibly removed from LOCG outside the agent") but do not delete. v1 errs on the side of preserving local state. v2 may add a `--prune` flag.
- R12. Import sets the top-level `last_full_import` and `last_import_source` to the filename.
- R13. Import emits a summary: rows added, rows updated, rows untouched, possibly-removed rows (if any).
- R14. No API-based full sync command in v1. The Excel import is the only LOCG-to-local sync path. API-based sync is rejected because (a) it's the exact path that fails under CF block, and (b) the Excel export is reliable.

### Sync from LocalStore → LOCG (write sync, user-mediated)

- R15. New `locg collection export <path>` command. Generates a LOCG-compatible CSV containing only **pending push rows** — rows where `pushed_to_locg_at` is `null` OR `local_added_at > pushed_to_locg_at` (i.e., added locally since the last confirmed LOCG export).
- R16. Export excludes rows with `needs_manual_variant = true` OR `needs_manual_series_canonical = true`. Those rows are surfaced in a separate report (see R18).
- R17. Generated CSV uses **the validated bulk-import recipe** (R23–R29 below) — all 21 columns, explicit blank `My Rating`, explicit `Marked Read=0`, Metron-derived `Release Date`, exact canonical `Series Name` from cache.
- R18. Alongside the CSV, `locg collection export` writes a sibling `*.notes.md` report with two sections:
  - **Ready to upload** — number of rows in the CSV
  - **Needs manual handling — variants** — rows with `needs_manual_variant = true`. Each row: series, issue, eBay item ID, win price, the listing's variant text, suggested LOCG search query
  - **Needs manual handling — series canonical** — rows with `needs_manual_series_canonical = true`. Each row: series (bare), issue, eBay item ID, win price, suggestion to run `locg collection resolve-series <name>` or to add the series via LOCG web UI
- R19. After upload, the user is expected to re-export from LOCG and run `locg collection import` on the new file. This clears the pending-push flag for confirmed rows and learns the exact `full_title` for any rows the agent had only best-guessed (see R32).
- R20. No automation of the LOCG web upload step in v1. Playwright-driven form-fill against the Bulk Import page is rejected for v1 because the form is multi-step (preset selection, upload, preview, confirm) and brittle. User uploads manually.

### Bulk Import CSV Recipe (validated 2026-05-22 against LOCG)

The following rules were empirically validated by uploading test CSVs against LOCG and inspecting the resulting Excel re-exports. Tests 1–6 documented in the Appendix.

- R21. CSV must include **all 21 columns** in the exact header order LOCG's export uses. Omitting columns triggers LOCG's "auto-enrich" path which sets defaults the user does not want (e.g., `My Rating=5.0`, `Marked Read=1`) on matched-existing rows.
- R22. `Publisher Name` must match LOCG's canonical convention (`Marvel Comics`, `DC Comics`, `Image Comics`, etc.). Bare publisher names from Metron need a small mapping table (e.g., Metron `Marvel` → LOCG `Marvel Comics`).
- R23. `Series Name` must match LOCG's exact canonical form, which is **inconsistent across series**:
  - Some have `(Vol. 1)`: `Fantastic Four (Vol. 1) (1961 - 1996)`, `The Amazing Spider-Man (Vol. 1) (1962 - 1998)`, `Daredevil (Vol. 1) (1964 - 1998)`, `Batman (Vol. 1) (1940 - 2011)`
  - Some don't: `Spawn (1992 - Present)`, `Doctor Strange, Sorcerer Supreme (1988 - 1996)`, `Haunt (2009 - 2012)`
  - **Canonical Series Name must be looked up in the cache (learned from prior exports), not derived algorithmically.** Net-new series the cache has never seen need a one-time LOCG search to learn — see R34.
- R24. `Full Title` follows LOCG's canonical pattern:
  - For canonical issues: `{series_short_name} #{issue}` — where `series_short_name` keeps the "The" prefix but drops year/Vol annotations (e.g., `The Amazing Spider-Man #197`, `Fantastic Four #257`).
  - For Newsstand variants: `{series_short_name} #{issue} Newsstand Edition` — confirmed pattern from your collection.
  - For cover variants: `{series_short_name} #{issue} Cover {X} {Artist} {Variant Type} Variant` — pattern observed in collection (e.g., `Spawn #313 Cover C Greg Capullo Variant`).
- R25. `Release Date` is required for net-new matches. Use Metron `store_date` when available, fall back to `cover_date`. LOCG silently overrides our date with its own canonical value on import — so "close enough" is sufficient.
- R26. `In Collection=1`, `In Wish List=0`, `Marked Read=0` always set explicitly.
- R27. `My Rating` must be **explicitly present-but-blank**. Omitting the column triggers LOCG to set a default rating of `5.0` on matched-existing rows AND flip `Marked Read` to `1`. This is the single most important non-obvious rule.
- R28. `Media Format = "Print"` always (LOCG's default for physical issues).
- R29. `Price Paid` from the Gixen `current_bid` (parsed as float, currency suffix stripped).
- R30. `Date Purchased` = the date the auction ended, sourced from the Gixen `end_date_iso` field (verified field name on `gixen-cli list --json` output). Falls back to today's date if unavailable.
- R31. `Purchase Store = "eBay"`. `Signature = 0`. `Slabbing = 0`. Everything else (Condition, Notes, Tags, Storage Box, Owner, Grading, Grading Company) left blank — LOCG handles blanks correctly (does not wipe pre-existing values on matched-existing rows; defaults blanks for new rows).

### Variant Handling

- R32. At `/comic:collection-add` time, variant resolution proceeds in this confidence-graded order:
  - **High confidence** — exact `Full Title` already exists in cache for a row with `pushed_to_locg_at > 0`. Use that string verbatim.
  - **Medium confidence** — heuristic match: eBay listing title contains a variant keyword the agent knows a pattern for (e.g., "newsstand" → `Newsstand Edition` suffix, matching Marvel's canonical convention). Use the heuristic-built Full Title, but log it for verification post-export.
  - **Low confidence** — listing has variant text but no known pattern (e.g., a generic "homage variant" without specific artist/cover-letter context). Add row to LocalStore with the agent's best-guess `Full Title` (often bare canonical) AND set `needs_manual_variant = true`. Excluded from the next export CSV.
- R33. The `*.notes.md` companion file from `locg collection export` (R18) is how `needs_manual_variant` rows reach the user. Each row includes its Gixen item ID, win price, the eBay listing's variant text, and a suggested LOCG search query.
- R34. After the user manually adds the variant via LOCG web UI, the next `locg collection import` triggers the reconciliation pass (R60) which migrates the cache row's tracking fields to the LOCG-canonical identity and clears `needs_manual_variant`. **Future wins of the same exact variant** hit R32's high-confidence branch (exact cache match) and import cleanly. Cross-variant pattern generalization (learning to handle *other* variants of the same series) is **not** in scope for v1 — each distinct variant requires its own round-trip.

### Net-New Series Handling

- R35. When a `/comic:buy` win is for a series the cache has never seen — no canonical `Series Name` from any prior export — the agent cannot generate a guaranteed-matching bulk-import row. v1 handles this with a CF-free cache+Metron resolution chain, falling through to a manual-flag fallback that never blocks `/comic:buy`.
- R36. **`/comic:collection-add` never invokes live LOCG.** The canonical Series Name resolution chain at win time is, in order:
  - **Cache hit** — lookup in `series_name_index` (R61). For the user's existing 1,795-row collection this covers every series the user has previously owned and handles the vast majority of wins.
  - **Metron-derived best-guess** — construct the canonical Series Name from Metron data per R62. Mark the row's resolution confidence as `medium`. Write the row to LocalStore; the next bulk-import CSV will attempt this best-guess string.
  - **Manual fallback** — if neither cache nor Metron resolves the series, write the row to LocalStore with bare series name and `needs_manual_series_canonical = true`. Surface the row in the next `*.notes.md` report.
- R37. `locg collection resolve-series <name>` — explicit, **user-invoked** CLI command. Uses Playwright + the persistent Chrome profile from PER-38 to query LOCG live and learn the canonical Series Name. Updates `series_name_index` and reconciles any cache rows with matching `needs_manual_series_canonical=true`. **Not invoked by any agent skill.** Designed as an opt-in cleanup tool the user runs when CF clearance is fresh. Net-new series resolution failures (no cache, no Metron) do not block `/comic:buy` — the win is recorded locally and the FMV/snipe path completes normally; only the LOCG push is deferred for that specific row until manual or `resolve-series` cleanup.

### `/comic:collection-check` Skill Changes

- R38. Skill becomes **fully offline**. Membership lookup happens against `~/.cache/locg/collection.json` only — no `locg lookup` calls, no live LOCG. The existing "Persist back to Gixen" step (writing `locg_id` back via `gixen-cli locg link`) is removed — `locg_id` is no longer resolved at check time (R46 also stops populating it at snipe time).
- R39. Match strategy:
  - Primary: exact `(series_name, full_title)` match after the agent constructs candidate Full Titles from `/comic:identify` output using the R24 conventions.
  - Fallback: canonical bare title match (`{short_series_name} #{issue}`) against the cache — catches the "I own the canonical, the listing is a variant" case.
- R40. Output table includes a new column: `Cache age` showing how stale the source data is (`<N> days since last LOCG export`, computed as `now - last_full_import` from R4 — same value for every row in a given check, not per-row). If `> 14 days`, surface a soft banner suggesting the user re-export from LOCG. Independently, every `/comic:collection-check` summary also includes a pending-push status line: `N rows pending push to LOCG; oldest pending = X days`. If `X > 21` or `N > 25`, escalate to a warning suggesting `locg collection export` (or `sync-to-locg`). These thresholds are empirical and tunable — the goal is to keep the round-trip ritual visible without nagging on every win.
- R41. For comics not in cache: report `Not in collection` confidently (the cache is the source of truth for membership). No fallback to live LOCG.
- R42. Variant flag-through: if the candidate Full Title matched canonical but the listing has variant text, surface a "⚠️ canonical match — listing variant not disambiguated" annotation. User decides whether that matters for the duplicate check.

### `/comic:collection-add` Skill Changes

- R43. Skill no longer uses Playwright per comic. The existing Step 5b (`gixen-cli locg link` write-back of resolved `locg_id`) is removed — no `locg_id` resolution happens at win time. Each won snipe is written to LocalStore with:
  - `source = "agent_win"`, `local_added_at = now` (microsecond precision), `pushed_to_locg_at = null`, `gixen_item_id = <item>`
  - Best-effort `Series Name`, `Full Title`, `Release Date` per R23–R25 and R32
  - `needs_manual_variant` set per R32 if variant confidence is low; `needs_manual_series_canonical` set per R36 if series resolution falls through to manual
- R44. After all wins are recorded, skill runs `locg collection export` and reports:
  - Number of rows added to LocalStore
  - Number of rows ready to push (in the generated CSV)
  - Number of rows needing manual variant handling (in the `*.notes.md` — variants section)
  - Number of rows needing manual series-canonical resolution (in the `*.notes.md` — series canonical section)
  - Number of pending-push rows (total across this and any prior unconfirmed wins); flag if oldest pending > 21 days
  - Instruction to the user: "Upload `<path>.csv` via LOCG Bulk Import, then re-export and run `locg collection import <new-xlsx>` to confirm."
- R45. Skill no longer requires LOCG network access at all. CF block doesn't affect it. The user-mediated upload step gates eventual LOCG state, but it can happen any time later.

### `/comic:snipe-add` Skill Changes

- R46. Stops carrying `locg_id` into Gixen. Snipes record `(series, issue, variant?, year?)` only — `locg_id` was an implementation artifact that the cache-keyed-on-canonical-Full-Title makes unnecessary.
- R47. Existing Gixen snipes with `locg_id` populated remain valid (no migration needed). New snipes simply leave it blank.

### `/comic:buy` Orchestrator Changes

- R48. Step 2 (`collection-check`) gate copy updated to reflect cache-driven behavior — "membership from local cache; LOCG state may lag by up to N days."
- R49. No "what if LOCG is unreachable" branch — there is no longer a code path that hits LOCG synchronously during `/comic:buy`.
- R50. Optional `locg collection export` reminder at the end of the workflow when the skill records new wins via `/comic:collection-add` ("N rows pending push to LOCG; run `locg collection export` and upload at your convenience.")

### Metron Integration (Optional Enrichment)

- R51. `locg-cli` adds optional Metron integration via `mokkari`. Credentials live in `~/.config/locg/.env` (`METRON_USERNAME`, `METRON_PASSWORD`).
- R52. Metron's role is **release-date resolution + stable identifier**, not canonical identity. Specifically:
  - At `/comic:collection-add` time, the agent looks up `(series, issue)` in Metron to get `cover_date` (and `store_date` if available) for the `Release Date` CSV column.
  - On successful Metron resolution, the cache row is enriched with `metron_id` for future cross-referencing.
- R53. Metron is **not blocking**: if Metron is unreachable, rate-limited, or doesn't cover the comic, the agent leaves `Release Date` blank in the CSV and surfaces the row as `needs_manual_variant` (LOCG won't match it without a date — R25).
- R54. Metron does **not** drive `Series Name`. LOCG's canonical Series Name format is too inconsistent to derive from Metron's data; it must come from prior LOCG exports.
- R55. Metron's rate limit (20 req/min, 5000/day) is enforced by mokkari's local retry logic. The agent batches lookups during `/comic:collection-add` rather than spreading them throughout the session.

### P0 Refinements (added during document review, 2026-05-22)

These requirements were added during document-review to address P0 findings around CF residual surface (P0 #1) and identity stability under round-trip (P0 #2). They cross-reference and extend earlier requirements rather than fitting cleanly into a single sub-section.

- R60. **Reconciliation pass** (referenced from R9, R11 Phase 1, R34). During `locg collection import`, before the standard merge, scan cache rows with `needs_manual_variant=true` OR `needs_manual_series_canonical=true`. For each such row, search incoming Excel rows for a match using the relaxed heuristic:
  - **`publisher_name` match** — case-insensitive, applying the same publisher mapping table as R22 (Metron `Marvel` → LOCG `Marvel Comics`, etc.) to both sides before comparing.
  - **`series_name` normalized match** — strip year-range parens from both sides. Strip `(Vol. N)` from one side ONLY when the other side lacks a Vol. annotation entirely (handles the asymmetric Metron-vs-LOCG case). If BOTH sides carry `(Vol. N)` annotations and they differ, treat as a hard mismatch — Vol. 1 and Vol. 2 of the same series are not the same comic.
  - **Issue token match** — extract the issue token from each row's `full_title` as a **string** (not numeric). Token may be `0`, `½`, `-1`, `150.1`, `Annual N`, `1.MU`, etc. Compare tokens as exact strings after whitespace normalization. Reject reconciliation if the tokens differ in any non-whitespace character.
  - **`release_date` exact-year match** — corrections observed in Test 5 stayed within the same year (`1969-05-01` → `1969-02-11`). The earlier ±1-year tolerance is removed as speculative — strict equal-year match reduces false positives across volume reboots (e.g., Action Comics Annual #1 1987 vs #1 2012).
  - **TPs/HCs/OGNs/Trades** — when neither row's `full_title` contains a `#N` issue token (e.g., `Batman: The Long Halloween TP`, `All-Star Superman: The Deluxe Edition HC`), fall back to case-insensitive exact match on the full `full_title` string. If neither has a token and titles don't match, the row is not a reconciliation candidate.

  Reconciliation actions when a match is found:
  - Update the cache row's `publisher_name`, `series_name`, `full_title`, `release_date` to the incoming canonical values
  - Preserve `local_added_at`, `gixen_item_id`, `metron_id`
  - Set `pushed_to_locg_at = now`, `source = "locg_export"` (was best-guess, now confirmed)
  - Clear both `needs_manual_variant` and `needs_manual_series_canonical`
  - Log the reconciliation in a persistent audit file `~/.cache/locg/import-history.jsonl` (one JSON object per reconciliation) AND in the import summary; persistent log enables sanity-checking unattended (cron) imports
  - The incoming Excel row is considered "consumed" — Phase 2 standard merge skips it

  Phase 2 dedup logic uses post-reconciliation identity tuples exclusively — reconciled rows are not re-matched in Phase 2 under their old identity.

  Multi-match handling: if multiple best-guess cache rows match the same incoming row, reconcile only the most recent (`max(local_added_at)` with `gixen_item_id ASC` as secondary tiebreak; `local_added_at` recorded at microsecond precision to minimize ties). If a tie still remains after both keys, leave ALL tied rows flagged and surface them in the import summary as "ambiguous reconciliation". Conservative: a missed reconciliation surfaces as a duplicate cache row the user can clean up, not a silent merge into the wrong canonical entry.

  No-match path: best-guess rows that do not match any incoming Excel row remain in cache untouched (manual flag preserved, `pushed_to_locg_at=null`). They reappear in the next `*.notes.md` report's Needs Manual section (R18) but never in the CSV body until the user resolves them via web UI, runs `locg collection resolve-series`, or directly edits the cache. Aging policy: a best-guess row that has survived ≥2 import cycles without reconciliation is surfaced in a dedicated "Likely stale" subsection of the report, suggesting the user check LOCG for a canonical sibling and delete the local best-guess.

- R61. **`series_name_index`** — derived top-level cache structure. `Dict[normalized_series_key, canonical_series_name]` where `normalized_series_key` strips `(Vol. N)` and year-range parens using the same normalization as R60. Populated **only from rows where `source = "locg_export"`** — best-guess `agent_win` rows do NOT contribute to the index (prevents self-reinforcing bad guesses). Rebuilt in full at the end of each `locg collection import` from the post-merge `comics` list. Read by `/comic:collection-add` at Series Name resolution time (R36 cache-hit step). O(1) lookup without scanning the full row list.

- R62. **Metron-derived canonical Series Name format** (referenced from R36 best-guess step). Construct `"{series.name} ({series.year_began} - {end_year})"` where `end_year = series.year_end` if Metron exposes a non-null value, else `"Present"`. Mokkari's `Series` schema exposes `year_began` and `year_end: Optional[int]` directly — no N+1 issue list query needed.

  Known mismatch axes between Metron `series.name` and LOCG canonical Series Name (best-guess may fail; affected rows self-heal via reconciliation on next round-trip):
  - `(Vol. N)` annotation: present in many LOCG canonical names (e.g., `Fantastic Four (Vol. 1)`, `Batman (Vol. 1)`), never present in Metron's `name`. The user's existing collection has 100+ series with `(Vol. N)` annotations.
  - `"The"` prefix: LOCG often keeps it (e.g., `The Amazing Spider-Man`); Metron may not.
  - Creator credit prefixes: e.g., `John Constantine, Hellblazer` in LOCG vs `Hellblazer` in Metron.
  - Punctuation: ampersand vs `and`, em-dash vs en-dash, article casing.

  Given the prevalence of `(Vol. N)` in the user's collection, expect this best-guess to fail for many net-new modern Marvel/DC series and fall through to `needs_manual_series_canonical=true`. Bronze-Age and Image series are more likely to match.

- R63. **`locg collection sync-from-locg`** — user-invoked CLI command. Uses Playwright + persistent Chrome profile (PER-38) with `headless=False` to load LOCG, click "Export My Comics", download the resulting `.xlsx`, then invoke `locg collection import` on the downloaded file. **Three distinct failure classes, each with its own exit code and message:**
  - Exit 3 — UI/selector change (LOCG redesigned the export page). Message: which selector failed; suggest manual export.
  - Exit 4 — Turnstile challenge unsolved (CF JS challenge present, profile lacks fresh `cf_clearance`). Message: run `locg login` to open the profile interactively and pass the challenge, then re-run.
  - Exit 5 — Network / HTTP failure including IP-level block (homepage returns `<title>Restricted</title>`, no Turnstile shown). Message: workspace egress likely on a CF deny-list; run from a non-blocked network or use the manual workflow.

  Cron safety: invoking process must be the user's own UID; `~/.config/locg/playwright-profile/` must be mode 0700. Bound retries — after 2 consecutive non-success exits, stop and surface the failure rather than thrashing. Falls back to the documented manual export workflow. **Never invoked by any agent skill** — designed for cron/scheduled use.

- R64. **`locg collection sync-to-locg [<csv-path>]`** — user-invoked CLI command. Uses Playwright + persistent Chrome profile (`headless=False`) to navigate LOCG's Bulk Import page, upload the CSV (defaults to the most recent `locg collection export` output), click through preset selection / preview, then **pause at the preview/confirm screen by default** — the user reviews LOCG's "Bulk Import - Confirm" page in the visible browser window and clicks "Yes, I'm Ready" themselves. Auto-confirm requires an explicit `--auto-confirm` flag (opt-in). After confirm, scrape the success page for `Added to Collection`, `Set ...`, and `Not Found` row counts and persist a record. Same three-class failure contract as R63 (UI change / CF challenge / network).

  **Empirical validation gate**: R64 must be tested end-to-end against LOCG and the cf_clearance lifetime across preset → upload → preview → confirm verified before this command ships. If multi-step CF challenges break the flow, R64 ships as preview-only (user finishes the manual step) rather than full automation.

  Designed as a companion to R63 so the user can chain `sync-from-locg && sync-to-locg` in a single cron entry. **Never invoked by any agent skill.**

- R65. **First-run bootstrap.** Before first `/comic:buy` use, the user must run `locg collection import <existing-locg-export>.xlsx` to seed the cache (rows) and `series_name_index`. Without this seed:
  - `/comic:collection-check` returns "Not in collection" confidently for every comic (R41) — the opposite of correct for a 1,795-row brownfield collection.
  - `/comic:collection-add` flags every win `needs_manual_series_canonical=true` because the `series_name_index` is empty.

  Both skills must check `last_full_import` at startup; if it is null (cache never populated), refuse to operate with a clear message: `"Cache empty — run 'locg collection import <your-locg-export.xlsx>' first."` This is a hard prerequisite, not a soft warning.

- R66. **R53 flag-overload fix.** Rows with missing `Release Date` (Metron unreachable, rate-limited, or doesn't cover the comic) are NOT flagged `needs_manual_variant`. Two options:
  - **Preferred** — include the row in the export CSV with `Release Date` left blank, relying on LOCG's fuzzy match for series the user already owns (Test 1 evidence: pre-existing rows matched without a date). The next post-import re-export reveals whether LOCG accepted or rejected; rejected rows auto-flag `needs_manual_series_canonical=true` via R60's no-match path.
  - **Fallback** — if the row is also net-new at the series level (would already be `needs_manual_series_canonical=true`), the missing date is moot — it's already in the manual queue. R53 is updated to say "leave Release Date blank; do not set `needs_manual_variant` for date-only gaps."

- R67. **LOCG renamed Full Title detection.** During Phase 2 standard merge (R11), when an existing identity-tuple row's `full_title` differs from the incoming Excel row's `full_title` (same `(publisher, series, release_date)` tuple, different title), log a "LOCG renamed" event in the import summary and the persistent import-history log (R60). Persist `previous_full_title` on the cache row for one import cycle so the next CSV-generation pass can emit both forms if a pending push would re-fail under the new name. This handles LOCG occasionally normalizing variant titles server-side (e.g., dropping creator first names, shortening suffix conventions).

- R68. **Cache integrity safeguards.**
  - File mode 0600 on `~/.cache/locg/collection.json` and `~/.cache/locg/import-history.jsonl`; mode 0700 on `~/.cache/locg/` itself.
  - `~/.config/locg/.env` (Metron credentials) created at mode 0600.
  - `~/.config/locg/playwright-profile/` directory at mode 0700 (auth session lives here).
  - Before any write to `collection.json`, take an atomic snapshot to `collection.json.bak` (single rolling backup). On read, if `schema_version` is higher than known, abort with a clear "locg-cli is out of date relative to this cache; upgrade or restore from `.bak`" message rather than silently mis-parsing.
  - `locg collection import` validates the incoming xlsx header row against the expected 21-column schema before merging; reject with a clear error on mismatch.

## Known Limitations (v1)

- L1. **Variant disambiguation requires prior LOCG export history (for that *specific* variant Full Title) or manual intervention.** Each distinct variant requires its own round-trip. Acceptable: the queue is small per session; it shrinks only for repeat wins of variants the user has already round-tripped. One-off variants (SDCC exclusives, retailer-specific covers, etc.) remain manual every time — there is no cross-variant generalization.
- L2. **Net-new series require a one-time live LOCG lookup or manual seed.** Acceptable: rare in practice (the user collects within known series).
- L3. **LOCG state lags LocalStore by one push cycle.** Until the user uploads the export CSV, LOCG's public profile won't show the new wins. Acceptable: the user articulated this trade-off explicitly.
- L4. **Bulk import is non-destructive but not idempotent in a useful way for LOCG fields the user manages elsewhere.** If the user adds something to LOCG via web UI between exports, the agent won't see it until the next import. Acceptable: web-UI adds are infrequent and will be picked up on the next round-trip.
- L5. **No `--prune` for rows the user removed from LOCG.** v1 preserves local state on missing-from-export. The cache could drift over time. v2 may add a prune flag with a dry-run.
- L6. **Programmatic sync (R63/R64) introduces practice-drift risk.** When R63/R64 work, the user stops practicing the manual upload/download workflow. When they later break (CF rotation, LOCG UI change), the manual fallback is documented but unrehearsed. Acceptable for v1 — surface a "last successful manual sync N days ago" reminder once the data exists; revisit if drift incidents occur in practice.
- L7. **`locg-cli` is now a comic-collection data layer wearing an LOCG-client name.** R2 keeps it as the single owner of cache invariants, which is correct for v1. The naming will become misleading if v2 adds Gixen-aggregation or unified collection features. Rename trigger: revisit if a second sync target (beyond LOCG) materializes, or if more than half of `locg-cli`'s commands become local-only.
- L8. **Reconciliation may false-positive across volume reboots.** R60's series_name normalization strips year-range parens; combined with same-year + same-issue-token + same-publisher matching, a best-guess row could in principle reconcile against a different Volume's same-numbered issue if both share a publication year. R60's strict `(Vol. N)` mismatch rule (when both sides carry the annotation) bounds this risk, but is not fully eliminated. Reconciliation audit log (`~/.cache/locg/import-history.jsonl`) is the user's defense — review periodically.

## Open Questions for Planning

- O1. Where exactly does the `locg collection import/export` CLI sit relative to PR #11–#13 (PER-71's recent landings)? Likely a new `commands.py` function and a new `commands/collection_io.py` or inline.
- O2. Should `locg collection export` write the CSV with the user's `~/Downloads` as default location, or `.context/`, or a configurable path? Suggested: configurable with `~/Downloads` default.
- O3. Cache file size at the user's scale (1,795 rows + new wins, ~21 columns each + ~5 tracking fields) is ~600KB JSON. Acceptable for direct read on each operation, no need for indexing in v1.
- O4. Should `/comic:collection-check` warn when cache is stale at all (R40), or only when a check might be wrong (i.e., a "not in collection" result on a comic from a series with recent web-UI adds)? Probably the former for simplicity in v1.
- O5. Backfill plan for the existing Gixen `bids.locg_id` column once `/comic:snipe-add` stops populating it: leave the column, leave values, ignore in agent flow. No migration needed per R47.

## Appendix: Test Methodology and Evidence

Six bulk-import tests were run against LOCG between 2026-05-22 10:30 and 11:50 (UTC+8). Each test isolated one or two variables. Results below are from the LOCG `Bulk Import - Success` screen and the user's subsequent Excel re-exports.

### Test 1 — Probe defaults

- **CSV**: 4 rows, minimal columns (Publisher, Series, Full Title blank-release-date, In Collection=1, In Wish List=0, Marked Read=0, Price Paid, Grading), no `My Rating` column.
- **Result**: 3 rows `Not Found`. ASM #151 matched and was tagged "Set Slabbing, Set Grading, Rated".
- **Finding**: ASM #151 succeeded because it was pre-existing in the user's collection (the bulk importer can fuzzy-match against prior library state). Comics not in prior library require more disambiguation.

### Test 2 — Probe punctuation hypothesis

- **CSV**: Same 3 failures from test 1, with `Doctor Strange, Sorcerer Supreme` (comma added).
- **Result**: All 3 still `Not Found`.
- **Finding**: Punctuation alone wasn't the issue.

### Test 3 — Probe exact canonical Series Name hypothesis

- **CSV**: Same 3 failures, with exact canonical Series Names pulled from user's existing export (e.g., `Doctor Strange, Sorcerer Supreme (1988 - 1996)`, `Fantastic Four (Vol. 1) (1961 - 1996)`, `Spawn (1992 - Present)`).
- **Result**: All 3 still `Not Found`.
- **Finding**: Exact Series Name necessary but not sufficient.

### Test 4 — Add Release Date

- **CSV**: Same 3 rows + Metron `cover_date` in Release Date column.
- **Result**: All 3 matched. DSSS #44 tagged "Set Slabbing, Set Grading, Rated" (was matched as duplicate to user's pre-existing entry); FF #257 and Spawn #313 tagged "Added to Collection, Rated".
- **Finding 1**: **Release Date is the missing variable for net-new matches.**
- **Finding 2**: Spawn #313 matched to BOTH `Spawn #313` (Cover A canonical) and the user's pre-existing `Spawn #313 Cover C Greg Capullo Variant`, marking both as in-collection. **Bare Full Title variant-spreads.**

### Test 5 — All 21 fields explicit, isolate Marked Read

- **CSV**: 2 fresh comics (FF #86, ASM #83) with all 21 columns populated, including explicit blank `My Rating`.
- **Result**: Both matched cleanly. FF #86 "Added to Collection, Set Media Format, Set Price Paid, Set Date Purchased, Set Purchase Store, Set Slabbing, Deleted from Wish List". ASM #83 "Set Media Format, Set Date Purchased, Set Purchase Store, Set Slabbing, Set Grading" (matched existing — dedupe missed the "The" prefix).
- **Finding 1**: **`Marked Read=0` sticks when `My Rating` column is present-but-blank.** With `My Rating` omitted entirely (tests 1, 4), LOCG defaulted Marked Read to 1 on matched-existing rows. With `My Rating` explicitly blank, Marked Read stays at the value we set.
- **Finding 2**: LOCG silently corrected Release Date on every test 5 row from our Metron-derived dates to LOCG's canonical dates (e.g., FF #86 sent `1969-05-01`, stored `1969-02-11`). **Close-enough dates are sufficient; LOCG corrects.**
- **Finding 3**: "Deleted from Wish List" — adding to collection auto-removed FF #86 from the user's wish list. No separate wish-list management needed.
- **Finding 4**: Bulk import is **non-destructive** of pre-existing values for fields the CSV doesn't explicitly set. ASM #151's pre-existing `My Rating=5.0` and `Marked Read=1` were preserved through later tests until the user manually corrected.

### Test 6 — Scale + variant patterns

- **CSV**: 28 rows covering the remaining un-imported `/comic:collection-add` wins, all with all 21 fields, exact known variant Full Titles from prior export patterns.
- **Result**: 28/28 matched. All 28 ended up with `Marked Read=0`. The two Spawn #299 rows (one with explicit Virgin Variant Full Title, one with bare canonical) resolved to TWO distinct LOCG entries with no variant-spread.
- **Finding**: The validated recipe **scales** and **eliminates the variant-spread bug** when exact variant Full Title is supplied. Bare Full Titles still risk spreading (as in test 4), confirming the value of the cache learning exact variant strings from prior exports.

### Empirical Recipe Summary (validated)

For a CSV row to bulk-import cleanly against LOCG:

1. All 21 columns present, in LOCG's export header order
2. `Publisher Name` = LOCG canonical convention (`Marvel Comics`, `DC Comics`, `Image Comics`, …)
3. `Series Name` = exact LOCG canonical, including year range and `(Vol. N)` where present
4. `Full Title` = canonical short-form for canonical issues; **exact** variant string for variants (else spread risk)
5. `Release Date` = present (Metron `store_date`/`cover_date`); LOCG corrects
6. `In Collection=1`, `In Wish List=0`, `Marked Read=0`
7. `My Rating` = **explicitly present-but-blank** (critical — controls Marked Read default)
8. `Media Format = "Print"`, `Purchase Store = "eBay"`, `Signature=0`, `Slabbing=0`
9. `Price Paid` from win record, `Date Purchased` from auction-end timestamp
10. All other columns blank

This recipe is the basis for `locg collection export` CSV generation (R17, R21–R31).
