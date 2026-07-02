---
name: comic:collection-add
description: Record won Gixen auctions into the collection on the comics server, then export a CSV ready to upload to LOCG. No Playwright, no LOCG network access required.
---

# Comic Collection Add

Record won Gixen auctions into the collection on the comics server (on the Mac
Mini; BUI-87) in one batch, then export a CSV for LOCG upload. The server is the single source of
truth across machines, so a win recorded here is visible on the other machine
immediately — no git round-trip (R8). No Playwright, no live LOCG session needed.

**Gixen CLI:** `gixen` (a uv-installed console script on PATH; run `./scripts/install.sh` if not found).

## Step 0: Resolve the server + bootstrap guard

Resolve `COMICS_SERVER_URL` (env var, with a hostname fallback — same as
`/comic:fmv`) and confirm the server is up before writing:

```bash
echo "${COMICS_SERVER_URL:-UNSET}"; hostname
# unset → MacBook (Hsus-MacBook-Air.local): http://mac-mini.tail9b7fa5.ts.net:8080
#         Mac Mini: http://localhost:8080 ; neither → stop
curl -sf "$COMICS_SERVER_URL/health" || { echo "server unreachable"; exit 1; }
curl -sf "$COMICS_SERVER_URL/api/comics/collection/status"
```

**If the health gate fails:** STOP — do not record wins against an unreachable
server.

**If `last_full_import` is null:** Stop immediately with:
> Collection empty on the server — run a full LOCG import before recording wins.

## Step 1: Pull won auctions

There are **two pull sources**. They return the *same kind* of data (ended
snipes) in **different shapes**, so the WON filter and field mapping differ —
pick the source first, then apply the matching filter below.

**Decision rule — which source to pull:**

1. **PREFER `/api/comics/history`** (bounded ~7-day window, server-side enriched,
   tiny payload). Use it whenever the window you need is covered — i.e. the last
   successful collection-add run was within the past 7 days. The seen-set state
   note (`last collection-add run` date in memory / the prior run's cutoff) is
   the simplest signal: if you can establish the last run was ≤7 days ago, the
   history endpoint covers every win since then.
2. **FALL BACK to `gixen list --json`** (full history, ~310 items) **only** when
   the needed window predates the endpoint's 7-day horizon — a first run, a run
   after a long gap, or any case where you **cannot** establish that the last
   run was within the past 7 days. When in doubt, use the CLI fallback: a
   slightly larger pull is harmless (the seen-set in Step 1b still excludes
   already-recorded wins), but a missed older win is the worse failure.

Don't over-engineer the date math — this choice only affects **completeness** of
the pull, not correctness. The seen-set (Step 1b) is what actually prevents
double-recording; the window choice just decides how far back you can see.

**Source A — history endpoint (preferred):**

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/history"
```

Returns a JSON **array** of row objects. Filter to wins by **`status` only**:
- `status` contains `WON` (case-insensitive)

No `time_to_end` filter is needed for this source: the endpoint already returns
only auctions that have **ended** in the past 7 days, and excludes the
tombstone, so `status` contains `WON` is the complete filter. (Every history row
actually carries `time_to_end == "ENDED"` — `iso_to_relative` returns `"ENDED"`
for any past auction, and the endpoint only surfaces past auctions — so adding
that check would be redundant, not necessary. The relevant difference from the
CLI source below is *scope*, not the `time_to_end` value.)

**Source B — CLI fallback (`gixen list --json`):**

```bash
gixen list --json 2>/dev/null
```

This dumps the entire snipe list, **including still-live (PENDING) snipes** whose
auctions haven't ended. So here you need **both** conditions — the
`time_to_end == "ENDED"` check is what excludes the live snipes that the history
endpoint never returns in the first place:
- `time_to_end == "ENDED"`
- `status` contains `WON` (case-insensitive)

**Then, for whichever source you used, deduplicate by `item_id`** (keep the first
occurrence) before continuing. The history endpoint already dedups server-side,
but the CLI fallback does not and the general contract requires it — so apply
this normalization unconditionally.

If no wins, print "No won auctions to add." and stop.

### Field mapping (history row → record-win entry)

The history row shape **differs** from `gixen list --json`. When you pulled from
the history endpoint, read these fields to build each Step 2 entry:

| History row field | Used as | Notes |
|---|---|---|
| `item_id` | `item_id` | string |
| `current_bid` | `current_bid` | e.g. `"222.50 USD"` — maps directly |
| `end_date_iso` | `end_date_iso` | ISO timestamp — maps directly |
| `title` | source for parsing `identify_data` | series/issue/year/variant, same as the CLI path |

(`gixen list --json` exposes the same logical fields under its own shape; the
`title`-parsing and JSON-build steps below apply to both.)

## Step 1b: Fetch already-processed wins (BUI-121)

Fetch the seen set so this run can skip wins already recorded in a prior run.
Best-effort — a failed call falls back to processing all wins (same pattern as
seller-scan):

```bash
SEEN_IDS=$(curl -sf "$COMICS_SERVER_URL/api/comics/collection/record-win/seen" \
  | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)['item_ids']))" \
  2>/dev/null || echo "")
```

From the filtered, deduped WON snipes in Step 1 (whichever pull source you used),
exclude any whose `item_id` appears in `SEEN_IDS`. Call the remaining wins
**new wins**.

**The two mechanisms compose.** Step 1's source choice *narrows the pull* (how
far back you can see — 7 days via the endpoint, or all of history via the CLI);
the seen-set here *excludes already-recorded wins*. The seen-set remains the
**primary dedup mechanism** and runs identically regardless of pull source — so a
larger CLI-fallback pull never re-records anything the seen-set already covers.

If there are no new wins, print:
> All won auctions already processed. Nothing new to record.

…and stop (skip Steps 2–5 entirely).

## Step 2: Build the record-win JSON

For each **new** won snipe, build one entry in this format:

```json
{
  "item_id": "318318338906",
  "current_bid": "222.50 USD",
  "end_date_iso": "2026-05-24T18:14:48.929921+00:00",
  "identify_data": {
    "series": "Ghost Rider",
    "issue": "1",
    "year": 1973,
    "variant_text": "Newsstand"
  }
}
```

`identify_data` fields:
- `series` — series name without publisher prefix (e.g. `"Amazing Spider-Man"`, not `"Marvel: Amazing Spider-Man"`)
- `issue` — issue number as a string (e.g. `"300"`, `"Annual 1"`)
- `year` — publication year as an integer; omit if unknown
- `variant_text` — variant description if the listing is explicitly a variant (e.g. `"Newsstand"`, `"Direct Edition"`); omit or `""` otherwise

**Source priority for `identify_data`:**

1. **In-session context** — if this skill is being called from `/comic:buy` and you already identified the comics in Step 1, use those identifications directly.
2. **Parse from gixen title via `comic-identify` (BUI-253)** — run the canonical
   title-parser on the snipe's `title` field instead of parsing it yourself:

   ```bash
   comic-identify "Ghost Rider #1 Marvel 1973 Newsstand"
   # {"series": "Ghost Rider", "issue": "1", "year": 1973, "edition": "single-issue",
   #  "is_lot": false, "constituent_issues": [], "reject_reasons": [], "confidence": 1.0, ...}
   ```

   Map its output straight into `identify_data`: `series` → `series`, `issue` → `issue`,
   `year` → `year` (omit if `null`). There's no direct `variant_text` field — infer it
   from the title as before (Newsstand/Direct/etc. aren't part of ComicIdentity).

   **For lots** (`"is_lot": true`): build one `identify_data` entry per issue number in
   `constituent_issues`, all sharing the extracted `series`. If `constituent_issues` is
   empty (a bundle whose contents couldn't be parsed — e.g. "Marvel Silver Age Lot") or
   `confidence` is low, ask the user once before proceeding rather than guessing.

Do not leave `series` or `issue` blank — if `comic-identify` can't determine them
(`series`/`issue` are `null`, or `confidence` is low), ask the user for that specific
snipe rather than guessing.

## Step 3: Record wins

Wrap the entries array as `{"wins": [...]}`, write it to a temp file, and POST it
to the server's record-win endpoint (`curl -sf` so a non-200 fails loudly):

```bash
# /tmp/wins.json contains: {"wins": [ {entry}, {entry}, ... ]}
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/record-win" \
  -H 'content-type: application/json' \
  -d @/tmp/wins.json
```

The server commits in batches of 25 using the same locg-cli logic (Metron series
resolution + BUI-34 already-owned dedup). On success it returns:

```json
{
  "rows_written": 3,
  "manual_variant_count": 0,
  "manual_series_count": 1,
  "metron_lookups_succeeded": 2,
  "skipped_already_owned": 0
}
```

`manual_series_count > 0` means those rows have `needs_manual_series_canonical=true` and will appear in the export's `.notes.md` for follow-up. **If the POST fails, STOP** — do not report success and do not mark the item IDs seen.

**Partial failures are non-200 (BUI-137):** the server commits in chunks of 25; if a later chunk fails to write, the endpoint returns **HTTP 500** with `{"detail": {"error": "partial_failure", "rows_written": <only-committed>, ...}}` rather than a misleading 200. Because Step 3 uses `curl -sf`, this halts the skill automatically — never report success on a partial_failure, and surface the `rows_written` so the user knows which wins still need recording.

## Step 3b: Mark new wins seen (BUI-121)

Only after a **successful** record-win POST, mark the new item IDs as processed
so future runs skip them. Best-effort — a failed mark is non-fatal (the
already-owned dedup will catch a re-POST):

```bash
# Build {"item_ids": ["111", "222", ...]} from the item_ids of the new wins
python3 -c "import json; ids=[w['item_id'] for w in json.load(open('/tmp/wins.json'))['wins']]; print(json.dumps({'item_ids': ids}))" \
  | curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/record-win/seen" \
    -H 'content-type: application/json' \
    -d @- \
  || echo "Warning: could not mark wins seen (non-fatal)"
```

## Step 4: Export to CSV

The export reads the *server* collection and returns the file contents; save them
locally for the LOCG upload:

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/collection/export" -o /tmp/export.json
ts=$(date +%Y-%m-%dT%H%M%S)
python3 -c "import json,sys,os; d=json.load(open('/tmp/export.json')); \
  base=os.path.expanduser(f'~/Downloads/locg-bulk-import-$ts'); \
  open(base+'.csv','w').write(d['csv']); open(base+'.notes.md','w').write(d['notes_md']); \
  print('csv:', base+'.csv', '| ready:', d['ready_count'])"
```

This writes a CSV at `~/Downloads/locg-bulk-import-<timestamp>.csv` plus a `.notes.md` sidecar listing any rows that need manual attention (unknown variant, unknown series canonical).

## Step 5: Report

Re-fetch status **after** the write — `pending_push_count` is only produced by
`/api/comics/collection/status`, never by the export, and the Step 0 read
predates this run's wins, so it would undercount by exactly the rows you just
added (BUI-156):

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/collection/status"
# read pending_push_count and oldest_pending_days from this fresh response
```

Note: `oldest_pending_days` is the age of the oldest uncleared pending item — it
is **not** "days since last sync." Items with `needs_manual_series_canonical=true`
never get cleared by an automated CSV export; they require a manual LOCG add
followed by a full `/comic:collection-sync` round-trip.

Print a summary (source `pending_push_count` from the fresh status read above,
not from Step 0):

```
**Added to local cache (N rows):**

Rows written: 3
Ready to push to LOCG: 3
Needs manual variant: 0
Needs manual series canonical: 1 (see .notes.md)

CSV exported to: ~/Downloads/locg-bulk-import-2026-05-23T14:30:00.csv

**Next step:** Upload the CSV at leagueofcomicgeeks.com → My Comics → Import.
Pending push total: N rows; oldest pending = X days.
```

Escalate the pending-push message when `oldest_pending_days > 21` or `pending_push_count > 25`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Using Playwright to add comics directly to LOCG | POST to `/api/comics/collection/record-win` then GET `/api/comics/collection/export` — no Playwright needed |
| POSTing the bare entries array | The endpoint expects `{"wins": [ ... ]}`, not a top-level array |
| Recording wins against an unreachable server | Health-gate first; if the POST fails, STOP and report — don't claim success |
| Passing LOCG IDs as part of record-win input | `record-win` does not take LOCG IDs; it resolves series via Metron and the server collection |
| Leaving `series` or `issue` blank in `identify_data` | Ask the user for the specific snipe — do not guess |
| Marking wins seen before the POST succeeds | Only mark seen after a successful record-win POST — a failed call means wins weren't recorded |
| Confusing `oldest_pending_days` with "days since last sync" | `oldest_pending_days` = age of oldest uncleared item; use `last_full_import` from status for sync recency |
| Assuming `/api/comics/history` needs the `time_to_end == "ENDED"` filter | It doesn't, and not for the reason you might think: every history row is *already* ended, so `time_to_end` is always `"ENDED"` there (`iso_to_relative` returns `"ENDED"` for past auctions). The filter would be redundant, not harmful. Use `status` contains `WON` only. The `"ENDED"` check matters only for `gixen list --json`, which also dumps still-live PENDING snipes |
| Pulling from `/api/comics/history` when the needed window predates 7 days | The endpoint only returns snipes ended in the past 7 days. On a first run or after a >7-day gap (any time you can't confirm the last run was within 7 days), fall back to `gixen list --json` so older wins aren't missed |
