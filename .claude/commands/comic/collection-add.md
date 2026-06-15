---
name: comic:collection-add
description: Record won Gixen auctions into the collection on the gixen server, then export a CSV ready to upload to LOCG. No Playwright, no LOCG network access required.
---

# Comic Collection Add

Record won Gixen auctions into the collection on the gixen server (BUI-87) in one
batch, then export a CSV for LOCG upload. The server is the single source of
truth across machines, so a win recorded here is visible on the other machine
immediately — no git round-trip (R8). No Playwright, no live LOCG session needed.

**Gixen CLI:** `gixen` (a uv-installed console script on PATH; run `./scripts/install.sh` if not found).

## Step 0: Resolve the server + bootstrap guard

Resolve `GIXEN_SERVER_URL` (env var, with a hostname fallback — same as
`/comic:fmv`) and confirm the server is up before writing:

```bash
echo "${GIXEN_SERVER_URL:-UNSET}"; hostname
# unset → MacBook (Hsus-MacBook-Air.local): http://mac-mini.tail9b7fa5.ts.net:8080
#         Mac Mini: http://localhost:8080 ; neither → stop
curl -sf "$GIXEN_SERVER_URL/health" || { echo "server unreachable"; exit 1; }
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/status"
```

**If the health gate fails:** STOP — do not record wins against an unreachable
server.

**If `last_full_import` is null:** Stop immediately with:
> Collection empty on the server — run a full LOCG import before recording wins.

## Step 1: Pull won auctions

```bash
gixen list --json 2>/dev/null
```

Filter to wins:
- `time_to_end == "ENDED"`
- `status` contains `WON` (case-insensitive)

If no wins, print "No won auctions to add." and stop.

## Step 2: Build the record-win JSON

For each won snipe, build one entry in this format:

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
2. **Parse from gixen title** — extract series, issue, and year from the snipe's `title` field. For lots, build one entry per issue. If the title is ambiguous (e.g., "Marvel Silver Age Lot"), ask the user once before proceeding.

Do not leave `series` or `issue` blank — if you cannot determine them, ask the user for that specific snipe.

## Step 3: Record wins

Wrap the entries array as `{"wins": [...]}`, write it to a temp file, and POST it
to the server's record-win endpoint (`curl -sf` so a non-200 fails loudly):

```bash
# /tmp/wins.json contains: {"wins": [ {entry}, {entry}, ... ]}
curl -sf -X POST "$GIXEN_SERVER_URL/api/comics/collection/record-win" \
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

`manual_series_count > 0` means those rows have `needs_manual_series_canonical=true` and will appear in the export's `.notes.md` for follow-up. **If the POST fails, STOP** — do not report success.

**Partial failures are non-200 (BUI-137):** the server commits in chunks of 25; if a later chunk fails to write, the endpoint returns **HTTP 500** with `{"detail": {"error": "partial_failure", "rows_written": <only-committed>, ...}}` rather than a misleading 200. Because Step 3 uses `curl -sf`, this halts the skill automatically — never report success on a partial_failure, and surface the `rows_written` so the user knows which wins still need recording.

## Step 4: Export to CSV

The export reads the *server* collection and returns the file contents; save them
locally for the LOCG upload:

```bash
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/export" -o /tmp/export.json
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
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/status"
# read pending_push_count and oldest_pending_days from this fresh response
```

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
