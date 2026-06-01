# Safe LOCG Collection & Wish-List Sync Process

How to keep the local cache (`~/.cache/locg/collection.json`,
`~/.cache/locg/wish-list.json`) and your League of Comic Geeks (LOCG) account in
sync without losing data, given that edits happen in **two places**: locally (via
`record-win`, `collection import`, `wish-list add/remove`) and directly in the
LOCG web UI.

This documents **observed behavior** as of 2026-05-30 (BUI-35), verified against
the code in `collection_io.py` / `commands.py` and reproduced against the real
cache. Where the behavior is unsafe, the gap is called out and filed as a
follow-on issue.

## Mental model

- The **local cache is the source of truth** for your workflow (see ADR
  `docs/decisions/0001-pivot-locg-cli-to-local-first.md`). LOCG is a bulk
  **import (XLSX in) / export (CSV out)** sync target.
- A collection row carries provenance: `source` (`agent_win` for `record-win`
  rows, `locg_export` for imported rows) and `pushed_to_locg_at`.
- A row is **pending push** when `pushed_to_locg_at IS NULL` OR
  `local_added_at > pushed_to_locg_at`. `collection export` emits pending rows.

## How import merges (collection)

`locg collection import <export.xlsx>` runs a two-phase merge — it **does not**
wipe the cache:

1. **Reconciliation** — `record-win` rows flagged `needs_manual_variant` /
   `needs_manual_series_canonical` are matched against incoming export rows by a
   relaxed heuristic (publisher + normalized series + issue token + year). On a
   single confident match the row's identity is rewritten, manual flags cleared,
   and `source` flips to `locg_export`. Multiple candidates → left flagged, logged
   as `ambiguous_reconciliation`.
2. **Standard merge** — insert-or-update by identity tuple
   `(publisher, series, full_title, release_date)`; `full_title` renames are
   detected via the partial identity `(publisher, series, release_date)` and the
   old title is preserved in `previous_full_title` for one cycle. User-managed
   column changes from LOCG are logged as `behavioral_drift`.

**Consequences (answers to the BUI-35 questions):**

- *Does import overwrite or merge pending `record-win` rows?* **Merge.** Pending
  rows survive; once they appear in a later export they are reconciled/updated and
  `pushed_to_locg_at` is set, so they drop out of "pending."
- *Does the pending row get cleaned up after upload + re-import?* **Yes** — the
  re-import matches the row and sets `pushed_to_locg_at`, clearing pending.
- *Do issues added directly in LOCG appear on the next import?* **Yes** — they
  arrive as new `locg_export` rows (`added`).
- *Re-import before uploading the CSV?* **Safe** — pending rows are not in the
  export yet, so they remain pending and untouched (no data loss). They are also
  checked for `possibly_removed` only when `source=agent_win` AND already pushed.

## How export works (collection)

`locg collection export [out.csv]` is **read-only**. It writes a 21-column
LOCG-compatible CSV of pending **ready** rows (flagged rows are excluded and
listed in the `.notes.md` companion), then appends wish-list rows with
`In Collection=0, In Wish List=1`. It does **not** set `pushed_to_locg_at`.

⚠️ Because export does not mark rows pushed, **exporting twice without an
intervening import re-emits the same rows.** Rely on the import reconcile to clear
pending; do not upload the same CSV twice.

## Safe sequence — collection

1. **Start a session:** `locg collection status --pretty` (confirm
   `last_full_import` is set and the cache isn't stale).
2. **Add wins:** `record-win` (via `/comic:*`) appends `agent_win` rows (pending).
3. **Export:** `locg collection export` → CSV in `~/Downloads` + `.notes.md`.
   Resolve anything in the manual-handling sections first.
4. **Upload to LOCG:** My Comics → Bulk Import → upload the CSV.
5. **Re-sync:** export a fresh XLSX from LOCG (My Comics → Export) and
   `locg collection import <that file>`. This reconciles your `agent_win` rows to
   `locg_export` and clears pending.
6. **Do not** run `export` → upload → `export` → upload again without the
   intervening `import` (step 5), or you will upload duplicates.

## Wish-list model

- Stored locally in `~/.cache/locg/wish-list.json` (`{name, id, ...}` items).
- `locg collection import` **rebuilds** `wish-list.json` from the imported
  collection rows where `in_wish_list == 1`. So wish-list items added **directly
  in LOCG** do appear locally after an import.
- `locg wish-list add "<title>"` appends a local-only entry
  `{"name": title, "id": None}`.
- `locg wish-list remove "<title>"` removes the first exact-`name` match locally.
- Wish-list rows are included in the collection `export` CSV
  (`In Collection=0, In Wish List=1`).

### ⚠️ Gap: local `wish-list add` is lost on the next import (BUI-47)

Because `import` **overwrites** `wish-list.json` from the export's
`in_wish_list==1` rows, any local-only `wish-list add` that has not yet
round-tripped through LOCG is **silently lost** on the next `collection import`.
Reproduced 2026-05-30: `wish-list add "Saga #1"` then `collection import`
(an export without Saga) → `Saga #1` gone.

Secondary: local entries store only `name` (no series/publisher/release_date), so
even when exported, those CSV columns are blank and LOCG Bulk Import may not
ingest them cleanly. Tracked in **BUI-47**.

## Safe sequence — wish-list (until BUI-47 is fixed)

1. Prefer adding wishes **in the LOCG web UI**, then `locg collection import` to
   pull them into the local cache. This is lossless.
2. If you use `locg wish-list add` locally:
   - `locg collection export` and upload the CSV to LOCG **before** running any
     `collection import`.
   - Never `wish-list add` and then `collection import` from a LOCG export that
     predates the add — the add will be wiped.
3. Wish-list **removals** when a wish is won: `record-win` adds the won book to
   the collection, but it does **not** auto-remove the wish-list entry. Remove it
   in LOCG (or `locg wish-list remove`) and re-import to keep both sides clean.

## Recovery if a sequence is violated

- **Uploaded the same CSV twice:** LOCG Bulk Import is keyed by title/series, so
  duplicates usually collapse; verify in the LOCG UI and delete extras. Then
  export a fresh XLSX and `import` to realign `pushed_to_locg_at`.
- **Lost a local `wish-list add` to an import:** re-add it in the LOCG UI (or
  `wish-list add` again) and, until BUI-47 lands, follow the safe sequence above.
- **Pending rows look stuck:** check `locg collection status --verbose` and the
  `import-history.jsonl` audit log for `ambiguous_reconciliation` /
  `possibly_removed` records; resolve the flagged rows and re-import.

## Gaps filed as follow-ons

- **BUI-47** — `wish-list add` entries silently lost on next `collection import`
  (overwrite, not merge) + local entries lack series/publisher/release_date.
