# Safe LOCG Collection & Wish-List Sync Process

How to push the collection up to League of Comic Geeks (LOCG) and reconcile it
back without losing data or creating duplicates, on the **server-backed**
architecture (BUI-87/93).

This supersedes the pre-BUI-87 version of this doc (which described
`data/locg/*.json` as the repo-versioned source of truth and listed BUI-47 as an
open gap). Behavior here was verified 2026-06-13 (BUI-122) against the code in
`collection_io.py` and a read-only dry-run on a copy of the production store.

## Mental model

- **The gixen server is the source of truth**, not `data/locg/`. The canonical
  store lives on the server host at `~/.gixen-server/collection-store/`
  (`collection.json` + rotating `.bak.0/1/2`, `wish-list.json`). `data/locg/` is
  now a gitignored local working cache, not repo-versioned.
- All sync goes through the server API (`/api/comics/collection/*`,
  `/api/comics/wish-list`) and the **`/comic:collection-sync`** skill — never by
  editing `data/locg/*.json` or committing it. A write on either machine is
  visible to the other on its next API read (R8) — no git round-trip.
- LOCG has **no write API**. The only way up is the manual **CSV upload** on
  LOCG's Bulk Import page; the only way back is the manual **XLSX export**. Those
  two steps require the LOCG web UI (Playwright login) and are done by you.
- A collection row carries provenance: `source` (`agent_win` for `record-win`
  rows, `locg_export` for imported rows) and `pushed_to_locg_at`.
- A row is **pending push** when `pushed_to_locg_at IS NULL` OR
  `local_added_at > pushed_to_locg_at`. `collection export` emits pending rows.

## The safe sequence — use `/comic:collection-sync`

Run the skill; it performs the whole round-trip with a backup and a post-import
safety check. The steps it drives (and you can do by hand against the API):

1. **Backup** the server store (`~/.gixen-server/collection-store.bak.<ts>`).
   Mandatory; the skill hard-stops if it fails.
2. **Export** (`GET /api/comics/collection/export`) → CSV + `.notes.md` in
   `~/Downloads`. Read-only; resolve anything in `.notes.md` first.
3. **Upload to LOCG** (manual): My Comics → Bulk Import → upload the CSV.
4. **Re-export from LOCG** (manual): My Comics → Export → download a fresh XLSX.
5. **Re-import** (`POST /api/comics/collection/import`, multipart file): runs the
   merge, reconciles your pushed wins, clears pending.
6. **Verify**: pending dropped; `row_count` grew only by genuine LOCG-side adds
   (a large import `added` signals duplicate insertion — stop and restore).

**Do not** export → upload → export → upload again without the intervening
re-import (step 5). Export does **not** mark rows pushed, so skipping the
re-import re-emits the same rows as duplicate uploads.

## How import merges (collection)

`POST /api/comics/collection/import` (and `locg collection import`) run a
two-phase merge — they **merge, never wipe**:

1. **Reconciliation** matches rows against the incoming export by a relaxed,
   **exact-year** (not exact-date) heuristic (publisher → normalized series →
   exact issue token → same year). It runs for:
   - `record-win` rows flagged `needs_manual_variant` /
     `needs_manual_series_canonical`, and
   - **(BUI-122)** unflagged **pending `agent_win`** rows whose exact identity is
     absent from the export. This is what makes a just-pushed win reconcile even
     though **LOCG silently rewrites its Release Date to LOCG's canonical value**
     on re-export (e.g. FF #86 `1969-05-01` → `1969-02-11`; see
     `docs/solutions/integration-issues/locg-bulk-import-recipe-2026-05-22.md`).
     On a single confident match the row's identity is rewritten, flags cleared,
     `pushed_to_locg_at` set, `source` → `locg_export`. **Multiple candidates →
     left pending and logged `ambiguous_reconciliation`** (a visible non-clear is
     preferred over a silent wrong merge). **Different year → not reconciled**
     (volume reboots stay distinct).
2. **Standard merge** — insert-or-update by identity tuple
   `(publisher, series, full_title, release_date)`; `full_title` renames are
   detected via the partial identity `(publisher, series, release_date)` and the
   old title is preserved in `previous_full_title` for one cycle. Exact-identity
   matches take precedence over year-tolerant reconciliation, so a win whose date
   round-tripped unchanged is matched here directly.

**Consequences:**

- *Does import overwrite or merge pending `record-win` rows?* **Merge.** Pending
  rows survive; they clear pending once they appear in a later export and
  reconcile, which sets `pushed_to_locg_at`.
- *Are wins lost if LOCG re-dates them?* **No (since BUI-122).** Before the fix, a
  re-dated win inserted as a duplicate and the original stayed pending forever; a
  real-store dry-run produced +33 duplicate rows / 33 stuck. Now the year-tolerant
  reconciliation clears them with zero duplicates.
- *Issues added directly in LOCG?* Arrive as new `locg_export` rows (`added`).
- *Re-import before uploading the CSV?* **Safe** — pending rows aren't in the
  export yet, so they remain pending and untouched.

## How export works (collection)

`GET /api/comics/collection/export` (and `locg collection export`) is
**read-only**. It writes a 21-column LOCG-compatible CSV of pending **ready**
rows (flagged rows are excluded and listed in the `.notes.md` companion), then
appends wish-list rows with `In Collection=0, In Wish List=1`. It does **not**
set `pushed_to_locg_at`.

⚠️ Because export does not mark rows pushed, **exporting twice without an
intervening import re-emits the same rows.** Rely on the import reconcile to
clear pending; do not upload the same CSV twice.

## Wish-list model

- Stored in the server store's `wish-list.json` (`{name, id, series_name?, ...}`
  items). `seller-scan` and `collection-check` read it over the API — **the LOCG
  web UI is not in the buy-workflow read path.**
- `POST /api/comics/wish-list` (and `locg wish-list add`) appends a local-only
  entry `{name, id: null}` (no series/publisher/release_date).
- `collection import` **rebuilds** the export-derived wish entries from the
  import's `in_wish_list==1` rows, then **re-appends local-only adds**.

### ✅ BUI-47 fixed: local wish-list adds survive imports

Local-only `wish-list add` entries (those with no `series_name`) are **preserved**
across `collection import` (`_write_wish_list_cache` re-appends them after
rebuilding from the export; commit `52d1e31`). Verified on real data 2026-06-13:
69 local-only adds survived a full import unchanged. They are **not** silently
lost.

### ⚠️ Limitation: local wish-list adds don't bulk-import to LOCG

A name-only add exports with blank Series Name / Publisher / Release Date. Per
the bulk-import recipe, Series Name + Release Date are **required** for a match,
so LOCG reports these rows "Not Found" and does **not** ingest them. This is
non-destructive: they persist on the server and keep driving seller-scan /
collection-check. **If you want a wish to appear in the LOCG web UI**, add it
there directly (then `collection import` pulls it back as a full `locg_export`
wish row). There is no tooling to enrich a name-only add with the columns LOCG
needs.

## One-time cleanup: pre-existing duplicate-identity rows

The store contains two rows that share an identity tuple with a sibling
(`Thor (Vol. 1) (1966 - 1996) #137` and `Uncanny X-Men (Vol. 1) (1980 - 2011)
#210`, both blank publisher). Because `identity_to_idx` maps an identity to a
single index, the redundant twin never clears pending and cannot reconcile under
date canonicalization (it goes ambiguous). Clean up once, backup-gated — this is
a manual edit, not automated:

```bash
# On the server host. Back up first.
cp -r ~/.gixen-server/collection-store ~/.gixen-server/collection-store.bak.dedup.$(date +%Y%m%d-%H%M%S)
# Identify the duplicate-identity pairs and, for each, drop the pending
# (pushed_to_locg_at == null) twin while keeping the pushed locg_export row.
# Verify with `locg collection status` (LOCG_DATA_DIR=<store>) that row_count
# and pending_push_count each drop by 2.
```

After cleanup, a sync reconciles every pushed win with zero duplicates and zero
stuck rows (verified: deduped dry-run reconciled all 33 date-shifted rows,
`row_count` delta 0).

## Recovery if something goes wrong

- **Restore the store:** copy the most recent `~/.gixen-server/collection-store.bak.<ts>`
  back over `collection-store/`. The skill makes one before every sync.
- **Uploaded the same CSV twice:** LOCG Bulk Import is keyed by title/series, so
  duplicates usually collapse on LOCG's side; verify in the LOCG UI and delete
  extras. Then re-export a fresh XLSX and import to realign `pushed_to_locg_at`.
- **Pending rows look stuck:** check `locg collection status --verbose` and the
  store's `import-history.jsonl` for `ambiguous_reconciliation` /
  `possibly_removed` records; resolve the flagged rows (`.notes.md`) and re-import.
- **Import `added` was unexpectedly large:** the re-import inserted duplicates
  rather than reconciling. Restore from backup and investigate (likely a LOCG
  canonicalization beyond Release Date, e.g. a Series Name rewrite, that the
  exact-year reconciliation couldn't match).

## What changed (BUI-122)

- Source of truth corrected: gixen server store, not `data/locg/`.
- BUI-47 marked fixed (local wish adds survive import); the wish-list "gap" is now
  a documented LOCG bulk-import limitation, not data loss.
- Import reconciliation extended to pending `agent_win` rows so LOCG re-dating a
  pushed win no longer duplicates it or strands it pending.
- The round-trip is now driven by `/comic:collection-sync` with a mandatory
  backup and post-import safety check.
