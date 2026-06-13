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
     `pushed_to_locg_at` set, `source` → `locg_export`. A missing publisher on the
     win (record-win rows have none) is treated as a wildcard, so LOCG populating
     its canonical publisher doesn't block the match. **Multiple candidates, or a
     target identity another row already holds → left pending and logged
     `ambiguous_reconciliation`** (a visible non-clear is preferred over a silent
     wrong merge — reconciliation never creates a duplicate-identity row).
     **Different year → not reconciled** (volume reboots stay distinct).
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
  real-store dry-run produced +33 duplicate rows / 33 stuck. The year-tolerant,
  publisher-wildcard reconciliation now clears wins for books not already owned,
  and **never creates a duplicate-identity row**. Wins for a book the collection
  already owns (different identity) stay pending and are surfaced as collisions —
  see "Duplicate win-records cleanup."
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

### Wish-list adds DO reach LOCG — but the export is owned-safe (BUI-122)

Empirically (2026-06-13), LOCG Bulk Import **adds a wish-list row by title alone**
— a name-only add (blank Series Name / Release Date) lands as "Added to Wish List"
fine. (The "Release Date required → Not Found" rule in the bulk-import recipe
applies to *collection* matches, not wish adds.)

The danger was the opposite: wish rows carry `In Collection=0`, so re-dumping the
**whole** wish list told LOCG to **remove** any wished book that was actually
owned — this deleted 18 owned books during testing. Two fixes now prevent it:

- **Export is owned-safe + diff-only** (`wish_rows_for_export`): it pushes only
  **local-only adds that are not owned**. Derived wishes (already on LOCG) and any
  owned book are excluded, so the CSV can never carry `In Collection=0` for a book
  you own.
- **`wishlist-add` skips owned issues** up front (collection-check per issue), so
  owned books stop polluting the wish list in the first place.

Net: genuine new wishes still sync to LOCG; owned books are never touched.

## Duplicate win-records cleanup (pre-existing)

A dry-run against a copy of the production store (2026-06-13) found two classes
of duplicate records that predate this work and are **independent of the sync
mechanics** — the reconciliation fix deliberately leaves them pending (visible)
rather than merge or duplicate them:

1. **Exact-identity duplicates** — two rows sharing the full identity tuple
   (`Thor (Vol. 1) (1966 - 1996) #137`, `Uncanny X-Men (Vol. 1) (1980 - 2011)
   #210`, both blank publisher). `identity_to_idx` maps an identity to one index,
   so the redundant twin never clears.
2. **Same-book, different-identity duplicates** — a pending `agent_win` win for a
   book the collection **already owns** as a `locg_export` row, where the two rows
   differ only by publisher (None vs canonical) and/or fabricated vs canonical
   date. On a real sync these surface as `ambiguous_reconciliation` /
   collision warnings and stay pending (the dry-run found ~13 such rows). They are
   leftover wins that record-win's already-owned dedup (BUI-34) didn't catch.

Clean up once, backup-gated — manual, not automated:

```bash
# On the server host. Back up first.
cp -r ~/.gixen-server/collection-store ~/.gixen-server/collection-store.bak.dedup.$(date +%Y%m%d-%H%M%S)
# After a sync, inspect the store's import-history.jsonl for ambiguous_reconciliation
# records and `locg collection status --verbose` for the residual pending count.
# For each duplicate, drop the pending (pushed_to_locg_at == null, source=agent_win)
# row when an owned locg_export row for the same book already exists; keep the owned
# row. Re-check that row_count and pending_push_count fall as expected.
```

After cleanup, a sync reconciles every remaining pushed win with zero duplicates
and zero stuck rows (verified: a deduped, production-faithful dry-run reconciled
the clean wins with `row_count` delta 0 and **no duplicate-identity rows in any
scenario**). The duplicate win-records are the residual the post-import safety
check is designed to surface — a follow-up could strengthen record-win's
already-owned dedup so they stop accumulating.

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
- BUI-47 marked fixed (local wish adds survive import).
- Import reconciliation extended to pending `agent_win` rows (publisher-wildcard,
  exact-year) so LOCG re-dating a pushed win no longer duplicates it or strands it
  pending; reconciliation never creates a duplicate-identity row.
- **Export is owned-safe and diff-only.** First-run testing uploaded the whole
  wish list with `In Collection=0` and **deleted 18 owned books** from the LOCG
  collection (recovered by re-uploading them as `In Collection=1`). The export now
  pushes only local-only, not-owned wishes — it can never delete an owned book.
- **`wishlist-add` skips issues you already own** (the upstream cause: owned books
  were being wish-listed, then deleted on export).
- The round-trip is driven by `/comic:collection-sync` with a mandatory backup and
  a post-import safety check.
