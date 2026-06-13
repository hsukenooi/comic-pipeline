---
name: comic:collection-sync
description: Run the full LOCG collection round-trip safely — backup, export pending wins to a CSV, upload to LOCG (manual), re-import the fresh LOCG export to reconcile and clear pending. Includes a post-import safety check. No data is pushed without a backup first.
---

# Comic Collection Sync

Push pending collection wins up to League of Comic Geeks and reconcile them back,
on the server-backed store (BUI-87/93: the **gixen server** is the source of
truth across machines, not `data/locg/`). This is the only flow that closes the
loop — `/comic:collection-add` records wins and exports a CSV, but nothing
re-imports the LOCG export to clear "pending" until you run this.

**Why the round-trip:** `export` deliberately does **not** mark rows pushed.
Only re-importing the fresh LOCG export sets `pushed_to_locg_at` and drops a row
out of "pending" (BUI-122). Skipping the re-import and just re-exporting later
**re-emits the same rows as duplicate uploads**.

**Two steps are manual and yours alone:** uploading the CSV to LOCG Bulk Import
and downloading the fresh XLSX afterward both require the LOCG web UI (Playwright
login). This skill drives everything else and gates hard around them.

## Step 0: Resolve the server + bootstrap guard

Resolve `GIXEN_SERVER_URL` (env var, hostname fallback — same as `/comic:fmv`)
and confirm the server is healthy and has a collection before touching anything:

```bash
echo "${GIXEN_SERVER_URL:-UNSET}"; hostname
# unset → MacBook (Hsus-MacBook-Air.local): http://mac-mini.tail9b7fa5.ts.net:8080
#         Mac Mini: http://localhost:8080 ; neither → stop
curl -sf "$GIXEN_SERVER_URL/health" || { echo "server unreachable"; exit 1; }
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/status"
```

**If the health gate fails:** STOP — never sync against an unreachable server.

**If `last_full_import` is null:** STOP with:
> Collection empty on the server — run a full LOCG import before syncing.

Record `row_count` and `pending_push_count` from the status response as
`ROWS_BEFORE` and `PENDING_BEFORE` — the post-import safety check (Step 6) needs
them.

## Step 1: Back up the server store

Back up the canonical store **before** any write, so any surprise is fully
reversible. The store lives beside the gixen DB on the server host
(`~/.gixen-server/collection-store/`):

```bash
# On the Mac Mini (GIXEN_SERVER_URL → localhost): local copy.
# From the MacBook: run it over ssh on the mini.
BACKUP="collection-store.bak.$(date +%Y%m%d-%H%M%S)"
case "$(hostname)" in
  *MacBook*|*macbook*) ssh mini "cp -r ~/.gixen-server/collection-store ~/.gixen-server/$BACKUP && echo backed up to ~/.gixen-server/$BACKUP" ;;
  *)                   cp -r ~/.gixen-server/collection-store ~/.gixen-server/"$BACKUP" && echo "backed up to ~/.gixen-server/$BACKUP" ;;
esac
```

**If the backup fails:** STOP. Do not proceed without a backup.

## Step 2: Export pending rows to a CSV

The export reads the **server** collection (read-only — it does not mark rows
pushed) and returns the file contents; save them locally for the upload:

```bash
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/export" -o /tmp/sync-export.json
ts=$(date +%Y-%m-%dT%H%M%S)
python3 -c "import json,os; d=json.load(open('/tmp/sync-export.json')); \
  base=os.path.expanduser(f'~/Downloads/locg-bulk-import-$ts'); \
  open(base+'.csv','w').write(d['csv']); open(base+'.notes.md','w').write(d['notes_md']); \
  print('csv:', base+'.csv'); print('ready:', d['ready_count'], '| manual_series:', d['manual_series_count'], '| wish:', d['wish_list_count'])"
```

Surface `ready_count` (collection rows that will upload), `manual_series_count`
(rows withheld from the CSV — they stay pending until you resolve them in
`.notes.md`), and `wish_list_count`.

**Expected, not an error:** local-only wish-list adds (name only, no Series Name
or Release Date) export with blank columns and LOCG will report them
"Not Found." They are **not** lost — they persist on the server and feed
seller-scan/collection-check. To get a wish onto LOCG itself, add it in the LOCG
UI directly. (See `packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md`.)

## Step 3: Upload to LOCG (manual — you)

Open League of Comic Geeks → **My Comics → Bulk Import** and upload the CSV from
`~/Downloads/locg-bulk-import-<ts>.csv`. Expect the collection rows to match and
the blank wish rows to report "Not Found" (expected).

**This is a manual step. Tell me when the upload is done.**

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
curl -sf -X POST "$GIXEN_SERVER_URL/api/comics/collection/import" \
  -F "file=@<XLSX>"
```

Surface `added` / `updated` / `reconciled` from the response. **If the POST
fails, STOP** — do not report success; the backup from Step 1 is intact.

## Step 6: Post-import safety check

Re-read status and compare against the Step 0 snapshot:

```bash
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/status"
```

Assert all of:
- **Pending dropped:** `pending_push_count` < `PENDING_BEFORE` (the sync's whole
  point). Expect it to fall by roughly `ready_count`; the `manual_series_count`
  rows stay pending by design.
- **No duplicate insertion:** `row_count` should grow only by books you genuinely
  added directly in the LOCG UI. The import summary's `added` is the tell — if
  `added` is large (close to `ready_count`), the re-import failed to reconcile
  and inserted duplicates instead. **STOP and investigate** (restore from the
  Step 1 backup if needed) — do not claim success.

**If either assertion fails**, report the discrepancy and the backup path; do not
proceed.

## Step 7: Report

```
**LOCG collection sync complete**

Backup:           ~/.gixen-server/collection-store.bak.<ts>
Exported (ready): N rows  (+ M withheld needs-manual-series — see .notes.md)
Re-import:        added=A  updated=U  reconciled=R
Pending:          PENDING_BEFORE → PENDING_AFTER  (cleared ~N)
Row count:        ROWS_BEFORE → ROWS_AFTER  (Δ = genuine LOCG-side adds)
Wish-list:        unchanged (local-only adds preserved)
```

Escalate if `pending_push_count` is still high after the sync (rows that didn't
reconcile — check `.notes.md` and the server's `import-history.jsonl` for
`ambiguous_reconciliation`).

## Common Mistakes

| Mistake | Fix |
|---|---|
| Re-export → re-upload without the intervening re-import | Always finish Step 5. Export does not mark rows pushed, so skipping the re-import re-emits the same rows as duplicate uploads |
| Syncing without a backup | Step 1 is mandatory and hard-stops on failure |
| Treating "Not Found" wish rows as an error | Expected — name-only wish adds don't bulk-import; they live on the server and still drive seller-scan/collection-check |
| Claiming success when `added` is large | A large `added` means the re-import inserted duplicates instead of reconciling — STOP, investigate, restore from backup if needed |
| Uploading the `.notes.md` rows | Only the `.csv` goes to LOCG; `.notes.md` lists rows withheld for manual resolution |
| Running the LOCG web steps for the user | Steps 3–4 are manual (Playwright login + web UI) — wait for the user to confirm |
