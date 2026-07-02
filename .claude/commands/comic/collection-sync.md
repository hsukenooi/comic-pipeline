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

## Step 0: Resolve the server + bootstrap guard

Resolve and health-gate the server through the **shared comics-server call
convention** (BUI-172, `docs/conventions/comics-server-call.md`) — never
hand-roll URL resolution or `curl` error handling here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # COMICS_SERVER_URL (env var, hostname fallback)
comics_health_gate     || exit 1   # the process is up
# BUI-157: route the status read through comics_curl too. /health is static, so
# it passes even when the collection store is corrupt and /collection/status
# 500s — comics_curl hard-fails on that 500 so it can't slip past the gate below.
comics_curl "$COMICS_SERVER_URL/api/comics/collection/status" \
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
reversible. The store lives beside the comics server's DB on the server host
(`~/.gixen-server/collection-store/`):

```bash
# On the Mac Mini (COMICS_SERVER_URL → localhost): local copy.
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
comics_curl "$COMICS_SERVER_URL/api/comics/collection/export" -o "$EXPORT_JSON" \
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

**Owned-safe export — what is and is NOT guaranteed (BUI-122/BUI-200):** the
CSV's wish rows are only local-only adds (derived wishes already on LOCG are
dropped), and the export excludes a wish when it can match an owned copy on
normalized *(series, issue)* rather than literal title. **Read these limits
carefully — this is a delete-capable workflow and the guarantee is partial.**

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

## Step 2b: Pre-sync data-quality audit (BUI-199)

Before uploading anything, audit the rows that will go up. record-win can write
garbage that LOCG silently rejects ("Not Found") or, worse, mis-files —
decorated full_titles, placeholder/blank dates, volume mislabels (BUI-199). A
single bad row can also hang a whole batch. Inspect the wins file:

```bash
python3 - "$CSV" <<'PY'
import csv, re, sys
path = sys.argv[1]   # the export is already wins-only (Step 2a)
rows = list(csv.DictReader(open(path)))
bad = []
for r in rows:
    pub, ser, ft, dt = r["Publisher Name"], r["Series Name"], r["Full Title"], r["Release Date"]
    issues = []
    if not pub.strip():                       issues.append("no publisher")
    if not ser.strip():                       issues.append("no series")
    if not ft.strip():                        issues.append("no full_title")
    if "(Vol." in ft or re.search(r"\(\d{4}", ft):
        issues.append("decorated full_title (LOCG full_title carries no (Vol.)/(year))")
    if dt and re.match(r"^\d{4}-01-01$", dt):  issues.append("Jan-1 placeholder date (will read Not Found)")
    if issues:
        bad.append((ft or "(blank)", issues))
print(f"{len(rows)} win rows; {len(bad)} flagged")
for ft, iss in bad:
    print(" -", ft, "::", "; ".join(iss))
# Dateless rows HANG LOCG's importer. A single blank-date row matches fine, but a
# batch that is all (or nearly all) dateless spins the importer at 0% — backfill first.
dateless = [r["Full Title"] for r in rows if not r["Release Date"].strip()]
if dateless:
    print(f"DATELESS: {len(dateless)}/{len(rows)} rows lack a Release Date — backfill before upload:")
    for ft in dateless: print("  -", ft)
PY
```

**If any row is flagged, STOP and fix it at the source** (re-run `record-win`
with a canonical series + exact full_title + accurate release date) before
uploading. Partial or wrong rows import as "Not Found"; an all-dateless batch
hangs. **Rows must be complete and exact: publisher + canonical series + exact
full_title (no decoration) + accurate release date.**

**If any rows are DATELESS, backfill their Release Date before uploading** — do
**not** upload a dateless batch (it hangs the importer at 0%). The durable fix is
record-win populating dates (BUI-210); until then, follow the tiered procedure in
**`references/date-backfill.md`** (cadence/Metron first, web-research sub-agent only
for the residual). Fill the dates into the already-generated CSV (don't re-export —
the export re-blanks placeholders), then continue.

Then also clean the
wish-list itself so no owned-but-wished entry survives to be pushed in Step 3b:

```bash
# Dry-run audit (BUI-130). Each conflict now carries the matched owned row's
# provenance — `series_name` + `release_date` (BUI-249/BUI-266) — because this
# audit is year/variant-blind by necessity (a wish-list name has no per-issue
# year), so it can land on the WRONG volume/era of a same-numbered issue.
comics_curl "$COMICS_SERVER_URL/api/comics/wish-list/conflicts"
```

**Review each conflict's provenance before removing anything (BUI-266).** For
every conflict, compare the wished book's real era/edition against the matched
owned row's `series_name`/`release_date`:

- **Genuine conflict** — the owned row is the *same* book you wished (same
  volume/era, same print edition). Safe to drop the wish (you now own it).
- **Decoy — do NOT remove** — a *different* comic that only shares a masthead +
  issue number: a cross-era match (e.g. a wished 1968 *Avengers* #52 matched
  against an owned UK-reprint `The Avengers (1973 - 1976)` #52) or the opposite
  print edition (a base `Uncanny X-Men #201` wish matched against an owned
  *Newsstand* copy). Removing these is the **BUI-259 114-item over-removal
  bug** — leave them wishlisted.

Then remove **only the reviewed genuine conflicts**, scoped by their exact
`name` values from the audit:

```bash
# Scoped removal (BUI-266). Each name is re-checked against a FRESH audit, so a
# stale/non-conflict name errors out instead of removing the wrong book.
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
not block the push, and must not be removed). This conflicts audit + scoped
remove is the sync's **fulfillment-drop** (BUI-208 U2): a wished book you now own
has its wish dropped and the owned copy kept; it touches only wish state (never a
collection row), and each drop is logged with the matched owned identity.

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
  never delete. If you see one, STOP, do not upload the rest, and report it; the
  owned-safe export regressed and the Step 1 backup must be restored.

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
  restore from the Step 1 backup if a deletion already landed.

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

Surface `added` / `updated` / `reconciled` from the response. **If the POST
fails, STOP** — do not report success; the backup from Step 1 is intact.

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
| Trying to push wishes in the default sync | The export is wins-only (BUI-208 machine gate — it refuses to emit `In Collection=0`); wishes are a separate opt-in export (`?push_wishes=true`, Step 3b), gated on a clean conflicts audit. Wins can only add; wishes can delete |
| Splitting the CSV in ≤20-row batches | No row limit — that was a misdiagnosis (the hangs were incomplete/dateless rows). Upload complete-and-exact rows at any size |
| Pushing wishes without cleaning conflicts first | Step 3b is gated on Step 2b reporting **zero** wish-list conflicts. An owned-but-wished entry pushed as In Collection=0 deletes the owned copy |
| Skipping the probe / not reading the import preview | Always probe ≤5 rows and read LOCG's per-row result first. **ABORT on any "Deleted from Collection."** — that is the data-loss signal |
| Skipping the pre-sync data-quality audit | Run Step 2b. Decorated full_titles, Jan-1 placeholder dates, and volume mislabels (BUI-199) read as "Not Found"; an all-dateless batch hangs the importer |
| Uploading partial/wrong rows | Rows must be complete and exact: publisher + canonical series + exact full_title (no `(Vol.)`/year decoration) + accurate release date — else LOCG returns "Not Found" |
| Re-export → re-upload without the intervening re-import | Always finish Step 5. Export does not mark rows pushed, so skipping the re-import re-emits the same rows as duplicate uploads |
| Syncing without a backup | Step 1 is mandatory and hard-stops on failure |
| Seeing "Deleted from Collection" on upload | A win/wish row should never delete (BUI-122/BUI-200) — STOP, do not upload the rest, report it, restore from the Step 1 backup if a deletion landed |
| "Error: timeout" at 0% on small batches | LOCG's import backend is degraded (a `queue_import_comic` XHR shows `(canceled)`); wait and retry later — not a file problem |
| Claiming success when `added` is large | A large `added` means the re-import inserted duplicates instead of reconciling — STOP, investigate, restore from backup if needed |
| Uploading the `.notes.md` rows | Only the `.csv` files go to LOCG; `.notes.md` lists rows withheld for manual resolution |
| Running the LOCG web steps for the user | Steps 3, 3b and 4 are manual (Playwright login + web UI) — wait for the user to confirm |
