---
name: comic:collection-sync
description: Run the full LOCG collection round-trip safely — backup, export pending rows, sync the add-only wins by default (wish-list push is a separate opt-in gated on conflict-cleaning), probe + preview every upload and abort on any "Deleted from Collection.", re-import the fresh LOCG export to reconcile and clear pending. Includes a pre-sync data-quality audit and a post-import safety check. No data is pushed without a backup first.
---

# Comic Collection Sync

Push pending collection wins up to League of Comic Geeks and reconcile them back,
on the server-backed store (BUI-87/93: the **comics server** on the Mac Mini is
the source of truth across machines, not `data/locg/`). This is the only flow that closes the
loop — `/comic:collection-add` records wins and exports a CSV, but nothing
re-imports the LOCG export to clear "pending" until you run this.

**Why the round-trip:** `export` deliberately does **not** mark rows pushed.
Only re-importing the fresh LOCG export sets `pushed_to_locg_at` and drops a row
out of "pending" (BUI-122). Skipping the re-import and just re-exporting later
**re-emits the same rows as duplicate uploads**.

**Two steps are manual and yours alone:** uploading the CSV to LOCG Bulk Import
and downloading the fresh XLSX afterward both require the LOCG web UI (Playwright
login). This skill drives everything else and gates hard around them.

**DATA-LOSS WARNING (BUI-122/BUI-200, read before running):** LOCG Bulk Import is
**stateful per column**. A wish row carries `In Collection=0`, and LOCG reads
`In Collection=0` as *"un-collect this book"* — not as a no-op. The first BUI-122
sync deleted 18 owned books this way; a later run deleted **26 owned X-Men**
because the owned copy was filed under a different masthead than the wish
(`The X-Men #107` owned vs `Uncanny X-Men #107` wished — LOCG files X-Men #1-141
under `The X-Men` and #142+ under `Uncanny X-Men`). The export is now owned-safe
across name variants (BUI-200), but the procedure below adds defense in depth:
**wins-only by default, wish push is a separate opted-in step, and every upload is
previewed and aborted on any unexpected "Deleted from Collection."**

Full incident post-mortem and the current sync architecture:
`docs/solutions/integration-issues/locg-export-deletes-owned-wished-books.md`
and `docs/solutions/integration-issues/locg-sync-unified-model-2026-06-22.md`.

## Step 0: Resolve the server + bootstrap guard

Resolve and health-gate the server through the **shared comics-server call
convention** (BUI-172, `docs/conventions/comics-server-call.md`) — never
hand-roll URL resolution or `curl` error handling here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # COMICS_SERVER_URL (env var, hostname fallback)
comics_health_gate     || exit 1   # the process is up
comics_curl "$COMICS_SERVER_URL/api/comics/collection/status" \
  || { echo "status check failed"; exit 1; }   # BUI-157: /health alone doesn't prove the store is healthy
```

**If the health gate or status call fails:** STOP — never sync against an
unreachable or erroring server. (Why the status read needs its own hard-fail,
not just `/health`: BUI-157 in `docs/audit/2026-06-15-seam-audit.md`.)

**If `last_full_import` is absent or null:** STOP with:
> Collection empty on the server — run a full LOCG import before syncing.

(Assert the field is *present* and non-null — a 500 body has no `last_full_import`
key at all, which `comics_curl`'s hard-fail already catches above.)

Record `row_count` and `pending_push_count` from the status response as
`ROWS_BEFORE` and `PENDING_BEFORE` — the post-import safety check (Step 6) needs
them.

## Step 1: Back up the server store

Back up the canonical store **before** any write, so any surprise is fully
reversible (BUI-433). The comics server already has filesystem access to its
own store, so this is one API call — no hostname detection, no `ssh`; it
works identically whether you're running from the Mac Mini or the MacBook:

```bash
BACKUP_JSON="$(mktemp -t collection-backup.XXXXXX)"
comics_post "$COMICS_SERVER_URL/api/comics/collection/backup" -o "$BACKUP_JSON" \
  || { echo "backup failed — do not proceed without a backup"; exit 1; }
BACKUP_PATH="$(python3 -c "import json; print(json.load(open('$BACKUP_JSON'))['backup_path'])")"
python3 -c "import json; d=json.load(open('$BACKUP_JSON')); \
  print('backup:', d['backup_path'], '| comics:', d['comics_count'], '| wish_list:', d['wish_list_count'])"
```

**If the backup fails: STOP.** The endpoint itself hard-fails (non-2xx)
rather than reporting success on an empty or unverifiable copy — a backup
that captured zero rows across `collection.json`/`wish-list.json` is
indistinguishable from a broken one, so `comics_post` never treats it as
success. Do not proceed to Step 2 without a completed backup.

Keep `$BACKUP_PATH` for the rest of the run — Steps 3/3b's abort path restores
from it via `POST /api/comics/collection/restore`.

## Step 2: Export pending rows to a CSV

The export reads the **server** collection (read-only — it does not mark rows
pushed) and returns the file contents; save them locally for the upload:

```bash
# BUI-138: fresh temp file per run + hard-fail before the parse (never reuse stale data)
EXPORT_JSON="$(mktemp -t sync-export.XXXXXX)"
comics_curl "$COMICS_SERVER_URL/api/comics/collection/export" -o "$EXPORT_JSON" \
  || { echo "export failed — not generating a CSV from stale data"; exit 1; }
ts=$(date +%Y-%m-%dT%H%M%S)
CSV="$HOME/Downloads/locg-bulk-import-$ts.csv"   # BUI-158: bind for Step 3's split
python3 -c "import json,os; d=json.load(open('$EXPORT_JSON')); \
  base=os.path.expanduser(f'~/Downloads/locg-bulk-import-$ts'); \
  open(base+'.csv','w').write(d['csv']); open(base+'.notes.md','w').write(d['notes_md']); \
  print('csv:', base+'.csv'); print('ready:', d['ready_count'], '| manual_series:', d['manual_series_count'], '| wish:', d['wish_list_count'])"
```

(Why a fresh `mktemp` path and a hard-fail before parsing: BUI-138 in
`docs/audit/2026-06-15-seam-audit.md` — the old fixed temp path let a failed
export silently build a CSV from a prior run's stale data.)

Surface `ready_count` (collection rows that will upload), `manual_series_count`
(rows withheld from the CSV — they stay pending until you resolve them in
`.notes.md`), and `wish_list_count`.

**Owned-safe export coverage (BUI-122/BUI-200) — partial, not complete:** the
CSV's wish rows are only local-only adds (derived wishes already on LOCG are
dropped), and the export excludes a wish when it can match an owned copy on
normalized *(series, issue)* rather than literal title.

**Structurally owned-safe now** (the export will NOT emit `In Collection=0` for an
owned copy in these cases):
- leading-article variants (`The X-Men` ↔ `X-Men`),
- `(Vol. N)` and year-range / bare-year decoration (`Fantastic Four (Vol. 3) (1997 - 2012)` ↔ `Fantastic Four`),
- the **classic X-Men main-run split only**: `The X-Men #1–141` ↔ `Uncanny X-Men #142+`.

**NOT yet structurally covered** — the export can still emit `In Collection=0` for
a book you own when the wish and the owned copy differ by any of these, so they
rely entirely on the import preview (below):
- other masthead aliases — `The Mighty Thor` ↔ `Thor`, `Invincible Iron Man` ↔ `Iron Man`, `Tales of Suspense` → `Iron Man`,
- subtitle / adjective relaunches — `X-Men: Legacy`, `Astonishing X-Men`, etc.,
- Annuals (`X-Men Annual` vs `X-Men`),
- spelling differences — `&` vs `and`, accents, dotted abbreviations (`Marvel Two-In-One` vs `Marvel Two in One`).

These uncovered cases are tracked under **BUI-197**. Until it lands, the **LOCG
import preview + abort-on-"Deleted from Collection." (Steps 3 / 3b) is the
load-bearing defense, not optional.** Keep the wish push opt-in and off by default
(Step 3b); for wins alone, deletion is structurally impossible, so the bulk of a
normal sync is safe.

## Step 2a: The export is wins-only by default (machine gate)

**No client-side split is needed.** As of BUI-208 the export ships **only wins**
(`In Collection=1`): the Mac Mini's `generate_csv` *refuses* to emit any
`In Collection=0` row unless an explicit owned-safe wish push is requested
(`?push_wishes=true`). So the file from Step 2 **is** your wins file, and the
default sync is structurally incapable of deleting a collection book — wins can
only add. Wishes (the only rows that can delete) are pushed solely via the
separate, opt-in Step 3b, and only after the wish-list has been conflict-cleaned
(Step 2b). `wish_list_count` from Step 2 will be `0` on a default (wins-only)
export — that is expected.

## Step 2b: Pre-sync data-quality audit (BUI-199, BUI-432)

Before uploading anything, audit the rows that will go up. record-win can write
garbage that LOCG silently rejects ("Not Found") or, worse, mis-files —
decorated full_titles, placeholder/blank dates, volume mislabels (BUI-199). A
single bad row can also hang a whole batch. Audit the already-exported wins
file with the tested `locg collection audit-pending` subcommand (BUI-432 moved
this off hand-authored inline Python — never re-export before auditing, since
the export re-blanks placeholder dates):

```bash
locg collection audit-pending "$CSV" --pretty
```

Read `row_count`, `flagged_count`, and `flagged_rows` (each entry has
`full_title` and a human-readable `issues` list — missing
publisher/series/full_title, decorated full_title, or a Jan-1 placeholder
date) from the JSON response.

**If `flagged_count` is non-zero, STOP and fix each flagged row at the source**
(re-run `record-win` with a canonical series + exact full_title + accurate
release date) before uploading. Partial or wrong rows import as "Not Found";
an all-dateless batch hangs. **Rows must be complete and exact: publisher +
canonical series + exact full_title (no decoration) + accurate release date.**

**If `dateless_count` is non-zero, backfill those rows' Release Date before
uploading** — do **not** upload a dateless batch (`all_dateless: true` is the
importer-hang scenario at 0%; surface the response's `dateless_warning` and
`dateless_titles`). The durable fix is record-win populating dates (BUI-210);
until then, follow the tiered procedure in **`references/date-backfill.md`**
(cadence/Metron first, web-research sub-agent only for the residual). Fill the
dates into the already-generated CSV (don't re-export — the export re-blanks
placeholders), then continue.

Then also clean the
wish-list itself so no owned-but-wished entry survives to be pushed in Step 3b:

```bash
# BUI-130: dry-run audit — conflicts carry matched-owned-row provenance (BUI-249/266)
comics_curl "$COMICS_SERVER_URL/api/comics/wish-list/conflicts"
```

**Review each conflict's provenance before removing anything (BUI-266) — the
audit is year/variant-blind by necessity (a wish-list name has no per-issue
year), so it can land on the WRONG volume/era of a same-numbered issue.** For
every conflict, compare the wished book's real era/edition against the matched
owned row's `series_name`/`release_date`:

- **Genuine conflict** — the owned row is the *same* book you wished (same
  volume/era, same print edition). Safe to drop the wish (you now own it).
- **Decoy — do NOT remove** — a *different* comic that only shares a masthead +
  issue number (a cross-era match, or the opposite print edition of the same
  issue). Removing decoys caused a 114-item over-removal incident (BUI-259);
  see `docs/solutions/integration-issues/wishlist-conflict-scoped-removal-2026-07-02.md`
  for the worked examples. Leave decoys wishlisted.

**A third bucket — `printing_conflicts` — needs your decision too (BUI-372/380).**
The audit response carries `printing_conflicts` alongside `conflicts`: same
provenance fields (`name`, `series`, `issue`, `full_title_matched`, `series_name`,
`release_date`) plus `printing_candidates`. An entry here means the wish matched
an owned row that is a **different printing** of the same series+issue (e.g. you
own the "2nd Printing" but wished the base issue, or vice versa) — printings are
distinct collectibles, so this is neither a genuine conflict nor a decoy. Render
it as a Pattern E-style advisory, mirroring `/comic:wishlist-add` Step 3/4's
BUI-372 handling, and let the user decide rather than folding it into either
bucket above:

```
Printing conflict — needs your decision (1):
  Amazing Spider-Man #300 — wish-list entry matches an owned "Amazing Spider-Man
  #300 2nd Printing" (full_title_matched); per printing_candidates the base
  printing (printing_ordinal: 1) is <owned/wish-listed/untracked>. Keep the wish
  (you still want the base) or drop it (you're satisfied with the reprint you
  own)?
```

**These are never auto-removed.** `remove-conflicts` derives its removal set
ONLY from `conflicts` — a `printing_conflicts` entry can't be swept, scoped or
unscoped (naming one in `names` below returns an explicit error, not a silent
no-op). If the user decides they no longer want the wish, remove it directly
with `DELETE /api/comics/wish-list?title=<name>` (BUI-128) — not through
`remove-conflicts`.

Then remove **only the reviewed genuine conflicts**, scoped by their exact
`name` values from the audit:

```bash
# BUI-266: scoped removal, each name re-checked against a FRESH audit
comics_curl -X POST "$COMICS_SERVER_URL/api/comics/wish-list/remove-conflicts" \
  -H 'Content-Type: application/json' \
  -d '{"names": ["<exact name from the audit>", "..."]}'
```

**The unscoped POST (no body) no longer removes anything (BUI-266 foot-gun
guard)** — it returns a non-mutating dry-run preview (`dry_run: true`). Passing
`{"confirm": true}` still performs the original *remove-every-conflict* global
sweep, but that reintroduces the decoy risk above, so use it **only** after
reviewing the full audit and confirming there are no decoys. Prefer scoped
`names`.

Both endpoints 409 if the collection was never imported. **Do not proceed to a
wish push (Step 3b) until every *genuine* conflict has been dropped** (decoys
left in the audit are false positives, not owned-but-wished entries — they must
not block the push, and must not be removed; `printing_conflicts` entries are the
same — they are advisory, not blocking, and must not be removed via
`remove-conflicts` either). This conflicts audit + scoped remove is the sync's
**fulfillment-drop** (BUI-208 U2): a wished book you now own has its wish dropped
and the owned copy kept; it touches only wish state (never a collection row),
and each drop is logged with the matched owned identity.

**A clean conflicts audit is a strong signal, not a proof of owned-safety.** The
audit and the export parse issue tokens slightly differently and apply the same
partial variant coverage as above, so a zero-conflict result does NOT guarantee
the export carries no `In Collection=0` for an owned book. The LOCG import preview
remains the final gate. (Parser parity between the audit and the export is being
unified in BUI-197.)

## Step 3: Probe, then upload the wins CSV to LOCG (manual — you)

Open League of Comic Geeks → **My Comics → Bulk Import**. Upload the CSV from
Step 2 (it is wins-only).

**Probe first.** Before the full upload, take a small mixed batch (≤5 rows) and
upload it alone. LOCG shows an import **preview/result** — read it row by row:

- Expect every row to be **"Added to Collection"** (or already present).
- **ABORT immediately on ANY "Deleted from Collection." line** — a win row should
  never delete. If you see one, STOP, do not upload the rest, report it, and
  restore the Step 1 backup (BUI-433 — the owned-safe export regressed):

```bash
comics_post "$COMICS_SERVER_URL/api/comics/collection/restore" \
  -H 'Content-Type: application/json' \
  -d "{\"backup_path\": \"$BACKUP_PATH\"}"
```

Only after a clean probe, upload the rest. **The constraint that matters is data
completeness, not row count** — there is no row limit. (The earlier "≤20 rows per
batch" belief was a misdiagnosis: the hangs were caused by incomplete/dateless
rows, not batch size.) Every row must be complete and exact — publisher +
canonical series + exact full_title (no decoration) + accurate Release Date —
which Step 2b already audited. An **incomplete or all-dateless batch hangs** the
importer; a complete batch uploads fine at any size.

Re-uploading is **safe** — wins are idempotent (`In Collection=1` re-applies as a
no-op, never a delete), so retry freely. **Watch the preview/result and abort on
any "Deleted from Collection."**

**If a complete upload still times out at 0%:** that's a LOCG-side outage, not your
file. Check DevTools → Network for a `queue_import_comic` XHR showing `(canceled)`
and a slow page load — both mean LOCG's import backend is degraded. Wait and retry
later; nothing to fix on our end.

**This is a manual step. Tell me when the wins upload is done.**

## Step 3b: (Optional, opt-in, deferred) Push the wish-list file

**Skip this unless the user explicitly asks to push new wishes.** Wish rows carry
`In Collection=0` and are the ONLY rows that can delete a book; the wish→LOCG
mirror is **deferred by default** (BUI-208 OQ-3). Push wishes only after **all** of:

1. Step 2b reported **zero** wish-list conflicts (no owned-but-wished entries).
2. The user has explicitly opted in.

Generate the owned-safe wishes CSV with the **opt-in** export — the only path that
emits `In Collection=0` (the machine gate otherwise refuses it):

```bash
comics_curl "$COMICS_SERVER_URL/api/comics/collection/export?push_wishes=true" -o "$EXPORT_JSON" \
  || { echo "wish export failed"; exit 1; }
python3 -c "import json,os; d=json.load(open('$EXPORT_JSON')); \
  p=os.path.expanduser(f'~/Downloads/locg-wishes-$ts.csv'); \
  open(p,'w').write(d['csv']); print('wishes csv:', p, '| wish rows:', d['wish_list_count'])"
```

Probe it the same way (≤5 rows), reading LOCG's preview:

- Expect every row to be **"Added to Wish List."**
- **ABORT on ANY "Deleted from Collection."** — that means a wished book is owned
  under a variant the export missed. STOP, do not upload the rest, report it, and
  if a deletion already landed, restore the Step 1 backup (BUI-433):

```bash
comics_post "$COMICS_SERVER_URL/api/comics/collection/restore" \
  -H 'Content-Type: application/json' \
  -d "{\"backup_path\": \"$BACKUP_PATH\"}"
```

Only after a clean probe, upload the rest (complete-and-exact, no row-count limit).
The wishes CSV is owned-safe by construction (BUI-200), but the preview is the last
line of defense — never skip it.

## Step 4: Re-export from LOCG (manual — you)

In LOCG → **My Comics → Export**, download a fresh XLSX (this carries LOCG's
canonical Series Name / Release Date for the rows you just pushed). Give me the
path to the downloaded `.xlsx`.

## Step 5: Re-import to reconcile and clear pending

POST the fresh XLSX back to the server. This runs the full merge: it reconciles
your just-pushed wins (tolerant of LOCG re-dating them within the same year —
BUI-122), sets `pushed_to_locg_at`, and re-appends local-only wish adds:

```bash
# Replace <XLSX> with the path from Step 4.
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/import" \
  -F "file=@<XLSX>"
```

Surface `added` / `updated` / `reconciled` from the response. **If `warnings`
is non-empty, surface every entry too** — each is a plain human-readable
string (not a structured object with separate fields), covering both the
BUI-412 `null_release_date_owned` data-quality notice and any pre-existing
`ambiguous_reconciliation` / reconciliation-collision notices. List them for
the operator; don't drop them just because they fall outside the
added/updated/reconciled counts. **If the POST fails, STOP** — do not report
success; the backup from Step 1 is intact.

## Step 6: Post-import safety check

Re-read status and compare against the Step 0 snapshot:

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/collection/status"
```

Assert all of:
- **Pending dropped:** `pending_push_count` < `PENDING_BEFORE` (the sync's whole
  point). Expect it to fall by roughly `ready_count`; the `manual_series_count`
  rows stay pending by design.
- **No duplicate insertion:** `row_count` should grow only by books you genuinely
  added directly in the LOCG UI. The import summary's `added` is the tell — if
  `added` is large (close to `ready_count`), the re-import failed to reconcile
  and inserted duplicates instead. **STOP and investigate** — do not claim
  success. Restore the Step 1 backup if needed (BUI-433):

```bash
comics_post "$COMICS_SERVER_URL/api/comics/collection/restore" \
  -H 'Content-Type: application/json' \
  -d "{\"backup_path\": \"$BACKUP_PATH\"}"
```

**If either assertion fails**, report the discrepancy and the backup path; do not
proceed.

## Step 7: Report

```
**LOCG collection sync complete**

Backup:           $BACKUP_PATH  (comics=N, wish_list=M — see Step 1)
Exported (ready): N rows  (+ M withheld needs-manual-series — see .notes.md)
Re-import:        added=A  updated=U  reconciled=R
Pending:          PENDING_BEFORE → PENDING_AFTER  (cleared ~N)
Row count:        ROWS_BEFORE → ROWS_AFTER  (Δ = genuine LOCG-side adds)
Wish-list:        unchanged (local-only adds preserved)
Warnings:         W warning(s) from the re-import (see below) — or "none"
```

**If the Step 5 response's `warnings` array is non-empty, list every entry
below the summary block** — each is already a complete, human-readable
string (not a structured object to reformat), so just enumerate them:

```
Warnings (2):
  - 3 owned collection row(s) have no release_date — this silently defeats
    the year-scoped wish-list conflicts audit (BUI-412). Consider
    backfilling release_date on these rows.
  - Ambiguous reconciliation for 'Amazing Spider-Man #300'
```

These are advisories, not failures — they don't change Step 6's pass/fail
assertions, but they flag real data-quality gaps (e.g. BUI-412's
`null_release_date_owned`) or rows that need manual follow-up, so they must
reach the operator rather than stay buried in the raw JSON.

Some pending rows may legitimately remain: wins for a book the collection
**already owns** (under a different identity) are left pending and logged
`ambiguous_reconciliation` rather than merged or duplicated — this is one of
the `warnings` entries above, not just an `import-history.jsonl` detail.
Check `.notes.md` and the server's `import-history.jsonl` for the full
audit trail; resolve them via the duplicate win-records cleanup in
`packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Trying to push wishes in the default sync | The export is wins-only (BUI-208 machine gate — it refuses to emit `In Collection=0`); wishes are a separate opt-in export (`?push_wishes=true`, Step 3b), gated on a clean conflicts audit. Wins can only add; wishes can delete |
| Pushing wishes without cleaning conflicts first | Step 3b is gated on Step 2b reporting **zero** wish-list conflicts. An owned-but-wished entry pushed as In Collection=0 deletes the owned copy |
| Syncing without a backup | Step 1 is mandatory and hard-stops on failure (`POST /api/comics/collection/backup`, BUI-433) |
| Seeing "Deleted from Collection" on upload | A win/wish row should never delete (BUI-122/BUI-200) — STOP, do not upload the rest, report it, restore the Step 1 backup if a deletion landed (`POST /api/comics/collection/restore` with `backup_path=$BACKUP_PATH`, BUI-433) |
| "Error: timeout" at 0% on small batches | LOCG's import backend is degraded (a `queue_import_comic` XHR shows `(canceled)`); wait and retry later — not a file problem |
| Claiming success when `added` is large | A large `added` means the re-import inserted duplicates instead of reconciling — STOP, investigate, restore the backup if needed (`POST /api/comics/collection/restore`) |
| Uploading the `.notes.md` rows | Only the `.csv` files go to LOCG; `.notes.md` lists rows withheld for manual resolution |
