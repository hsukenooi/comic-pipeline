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

Resolve and health-gate the server through the **shared comics-server call
convention** (BUI-172, `docs/conventions/comics-server-call.md`) — never
hand-roll URL resolution or `curl` error handling here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # GIXEN_SERVER_URL (env var, hostname fallback)
comics_health_gate     || exit 1   # the process is up
# BUI-157: route the status read through comics_curl too. /health is static, so
# it passes even when the collection store is corrupt and /collection/status
# 500s — comics_curl hard-fails on that 500 so it can't slip past the gate below.
comics_curl "$GIXEN_SERVER_URL/api/comics/collection/status" \
  || { echo "status check failed"; exit 1; }
```

**If the health gate or status call fails:** STOP — never sync against an
unreachable or erroring server.

**If `last_full_import` is absent or null:** STOP with:
> Collection empty on the server — run a full LOCG import before syncing.

(Assert the field is *present* and non-null — a 500 body has no `last_full_import`
key at all, which `comics_curl`'s hard-fail already catches above.)

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
# BUI-138: a FRESH temp file per run + a hard-fail BEFORE the parse. With the
# old fixed temp path, a failed export (server down / 500) left the prior run's
# file untouched and the next line happily built a CSV from STALE data.
# comics_curl hard-fails on non-200, and mktemp guarantees no stale reuse.
EXPORT_JSON="$(mktemp -t sync-export.XXXXXX)"
comics_curl "$GIXEN_SERVER_URL/api/comics/collection/export" -o "$EXPORT_JSON" \
  || { echo "export failed — not generating a CSV from stale data"; exit 1; }
ts=$(date +%Y-%m-%dT%H%M%S)
CSV="$HOME/Downloads/locg-bulk-import-$ts.csv"   # BUI-158: bind for Step 3's split
python3 -c "import json,os; d=json.load(open('$EXPORT_JSON')); \
  base=os.path.expanduser(f'~/Downloads/locg-bulk-import-$ts'); \
  open(base+'.csv','w').write(d['csv']); open(base+'.notes.md','w').write(d['notes_md']); \
  print('csv:', base+'.csv'); print('ready:', d['ready_count'], '| manual_series:', d['manual_series_count'], '| wish:', d['wish_list_count'])"
```

Surface `ready_count` (collection rows that will upload), `manual_series_count`
(rows withheld from the CSV — they stay pending until you resolve them in
`.notes.md`), and `wish_list_count`.

**Owned-safe export (BUI-122):** the CSV's wish rows are only local-only adds you
**don't** already own — derived wishes (already on LOCG) and owned books are
excluded. This matters because wish rows carry `In Collection=0`; dumping the
whole wish list previously **deleted owned-but-wished books** from the LOCG
collection. With the fix the CSV can never remove a book you own. Genuine new
wishes still import fine (LOCG adds a wish by title).

**Optional pre-sync hygiene (BUI-130):** the export filter keeps owned books out
of the *CSV*, but the stale owned-but-wished entries still sit in the wish-list
itself. To clean them out at the source, audit first
(`GET /api/comics/wish-list/conflicts` — dry run) and, if it surfaces any,
`POST /api/comics/wish-list/remove-conflicts` to clear them in one call (no SSH).
Both 409 if the collection was never imported.

## Step 3: Upload to LOCG (manual — you)

Open League of Comic Geeks → **My Comics → Bulk Import** and upload the CSV from
`~/Downloads/locg-bulk-import-<ts>.csv`. Expect the collection rows to be added and
the new-wish rows to be "Added to Wish List." No "Deleted from Collection" should
appear — if it does, **stop** and report it (the export safety filter failed).

**LOCG import is flaky — upload in small batches.** LOCG's importer times out on
larger files (observed: ~20 rows succeeds; ~100 fails at 0% with "Error: timeout").
If the CSV is more than ~20 rows, split it and upload one chunk at a time
(`$CSV` is the path bound in Step 2 — BUI-158):

```bash
python3 - "$CSV" <<'PY'
import csv,sys; rows=list(csv.reader(open(sys.argv[1]))); h,d=rows[0],rows[1:]
for i in range(0,len(d),20):
    p=sys.argv[1].replace(".csv",f"-batch-{i//20+1:02d}.csv")
    csv.writer(open(p,"w",newline="")).writerows([h]+d[i:i+20]); print(p)
PY
```

Re-uploading is **safe** — the CSV is owned-safe and idempotent (`In Collection=1`
/ `In Wish List=1` re-applies as a no-op, never a delete), so retry freely.

**If even small batches time out at 0%:** that's a LOCG-side outage, not your file.
Check DevTools → Network for a `queue_import_comic` XHR showing `(canceled)` and a
slow page load — both mean LOCG's import backend is degraded. Wait and retry later;
nothing to fix on our end.

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

Some pending rows may legitimately remain: wins for a book the collection
**already owns** (under a different identity) are left pending and logged
`ambiguous_reconciliation` rather than merged or duplicated. Check `.notes.md`
and the server's `import-history.jsonl`; resolve them via the duplicate
win-records cleanup in
`packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Re-export → re-upload without the intervening re-import | Always finish Step 5. Export does not mark rows pushed, so skipping the re-import re-emits the same rows as duplicate uploads |
| Syncing without a backup | Step 1 is mandatory and hard-stops on failure |
| Seeing "Deleted from Collection" on upload | The export should never emit In Collection=0 for an owned book (BUI-122) — if you see deletions, STOP and report; the owned-safe filter regressed |
| Uploading a large CSV in one shot | LOCG times out past ~20 rows; split into ≤20-row batches. Retry is safe (the CSV is idempotent/owned-safe) |
| "Error: timeout" at 0% on small batches | LOCG's import backend is degraded (a `queue_import_comic` XHR shows `(canceled)`); wait and retry later — not a file problem |
| Claiming success when `added` is large | A large `added` means the re-import inserted duplicates instead of reconciling — STOP, investigate, restore from backup if needed |
| Uploading the `.notes.md` rows | Only the `.csv` goes to LOCG; `.notes.md` lists rows withheld for manual resolution |
| Running the LOCG web steps for the user | Steps 3–4 are manual (Playwright login + web UI) — wait for the user to confirm |
