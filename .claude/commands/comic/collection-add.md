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

Resolve and health-gate the comics server through the **shared comics-server
call convention** (BUI-172, `docs/conventions/comics-server-call.md`) — don't
hand-roll URL resolution or the health check here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # COMICS_SERVER_URL (env var, hostname fallback)
comics_health_gate     || exit 1   # the server must answer
```

**If either step fails:** STOP — do not record wins against an unreachable
server.

Then read collection status:

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/collection/status"
```

**If `last_full_import` is null:** Stop immediately with:
> Collection empty on the server — run a full LOCG import before recording wins.

## Step 1: Pull won auctions

Pull the full snipe list from the Gixen CLI and filter it to genuine wins:

```bash
gixen list --json 2>/dev/null
```

This dumps **every** snipe, including still-live (PENDING) ones whose auctions
haven't ended. Filter to wins with **both** conditions:
- `time_to_end == "ENDED"` (excludes the live snipes)
- `status` contains `WON` (case-insensitive)

Then **deduplicate by `item_id`** (keep the first occurrence) before continuing.
If no wins, print "No won auctions to add." and stop.

Each snipe object exposes the fields Step 2 needs directly: `item_id`,
`current_bid` (e.g. `"222.50 USD"`), `end_date_iso` (ISO timestamp), and `title`
(the source for parsing `identify_data`).

> **Optional fast-path (bandwidth only):** if you can establish the last
> collection-add run was ≤7 days ago, `GET /api/comics/history` returns the same
> wins in a smaller, server-enriched payload — already ended, already deduped,
> tombstone excluded — so you'd filter by `status` contains `WON` alone (no
> `time_to_end` check needed there). This is purely an optimization: the Step 1b
> seen-set is what prevents double-recording regardless of source, and a missed
> older win is the worse failure, so when the window is uncertain just use the
> CLI above.

## Step 1b: Fetch already-processed wins (BUI-121)

Fetch the seen set so this run can skip wins already recorded in a prior run.
Best-effort — a failed call falls back to processing all wins (same pattern as
seller-scan):

```bash
SEEN_IDS=$(curl -sf "$COMICS_SERVER_URL/api/comics/collection/record-win/seen" \
  | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)['item_ids']))" \
  2>/dev/null || echo "")
```

From the filtered, deduped WON snipes in Step 1, exclude any whose `item_id`
appears in `SEEN_IDS`. Call the remaining wins **new wins**.

The seen-set is the **primary dedup mechanism**: it excludes already-recorded
wins, so pulling the full `gixen list` (rather than a narrower window) never
re-records anything a prior run already committed.

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
to the server's record-win endpoint. **Capture the response body to a file and
read only the summary scalars into context** — the full response also carries
`skipped_already_owned_titles` and `skipped_already_owned_detail` (one object per
already-owned win), which on a large batch is thousands of tokens of detail you
don't need in the loop. Keep it on disk; surface the scalars:

```bash
# /tmp/wins.json contains: {"wins": [ {entry}, {entry}, ... ]}
# rm the response file FIRST: on a connection failure curl leaves any prior
# run's file in place (it only truncates -o once bytes arrive), and reading a
# stale body would fabricate a "committed before failure" count for a request
# that never left the machine.
rm -f /tmp/record_win_response.json
# Capture body + status separately (not `curl -f`, which discards the error
# body): on a partial_failure we still need rows_written out of the 500 body.
# curl still prints %{http_code} == 000 when it never connects.
code=$(curl -sS -o /tmp/record_win_response.json -w '%{http_code}' \
  -X POST "$COMICS_SERVER_URL/api/comics/collection/record-win" \
  -H 'content-type: application/json' \
  -d @/tmp/wins.json)

if [ "${code:-000}" -ge 200 ] && [ "${code:-000}" -lt 300 ]; then
  # Success — pull only the summary scalars into context (drops the big
  # skipped_already_owned_* arrays, which stay in /tmp/record_win_response.json).
  # A 200 with an unparseable/truncated body is NOT success — exit non-zero so
  # the run stops rather than silently proceeding to mark-seen.
  python3 -c "import json; d=json.load(open('/tmp/record_win_response.json')); \
    print(json.dumps({k: d.get(k) for k in ['rows_written','skipped_already_owned','manual_variant_count','manual_series_count','metron_lookups_succeeded']}))" \
    || { echo "record-win returned HTTP $code but the body could not be parsed — see /tmp/record_win_response.json; do NOT assume success or mark wins seen."; exit 1; }
else
  # Failure — STOP with a NON-ZERO exit (this replaces the hard-fail that
  # `curl -sf` used to give and that BUI-137's 500 relies on). The exit 1 is
  # load-bearing: without it, anything keying off this block's exit status would
  # treat the failure as success and run Step 3b, permanently marking uncommitted
  # wins seen. Surface rows_written from the partial_failure detail so the user
  # knows which wins DID commit before continuing.
  echo "record-win FAILED (HTTP $code) — STOP. Do not mark wins seen."
  python3 -c "import json; d=json.load(open('/tmp/record_win_response.json')); det=d.get('detail', d); \
    print('error:', det.get('error'), '| rows_written (committed before failure):', det.get('rows_written'))" 2>/dev/null \
    || echo "(no parseable response body — the request likely never reached the server)"
  exit 1
fi
```

The server commits in batches of 25 using the same locg-cli logic (Metron series
resolution + BUI-34 already-owned dedup). On a 2xx the scalar extraction above
prints just:

```json
{
  "rows_written": 3,
  "skipped_already_owned": 0,
  "manual_variant_count": 0,
  "manual_series_count": 1,
  "metron_lookups_succeeded": 2
}
```

The full response — including the `skipped_already_owned_titles` /
`skipped_already_owned_detail` arrays — is preserved at
`/tmp/record_win_response.json` if the user wants to inspect which owned titles
were skipped.

`manual_series_count > 0` means those rows have `needs_manual_series_canonical=true` and will appear in the export's `.notes.md` for follow-up. **If the POST fails (non-2xx), STOP** — do not report success and do not mark the item IDs seen.

**Partial failures are non-200 (BUI-137):** the server commits in chunks of 25; if a later chunk fails to write, the endpoint returns **HTTP 500** with `{"detail": {"error": "partial_failure", "rows_written": <only-committed>, ...}}` rather than a misleading 200. The status-code check above routes this to the failure branch, which surfaces `rows_written` so the user knows which wins still need recording — never report success on a partial_failure.

## Step 3b: Mark new wins seen (BUI-121)

Only after a **successful** record-win POST, mark the new item IDs as processed
so future runs skip them. Best-effort — a failed mark is non-fatal (the
already-owned dedup will catch a re-POST):

```bash
# Build {"item_ids": ["111", "222", ...]} from the item_ids of the new wins.
# dict.fromkeys dedups while preserving order — a lot that expanded into several
# entries shares one item_id, so a plain comprehension would POST it N times.
python3 -c "import json; ids=list(dict.fromkeys(w['item_id'] for w in json.load(open('/tmp/wins.json'))['wins'])); print(json.dumps({'item_ids': ids}))" \
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
| Forgetting the `time_to_end == "ENDED"` filter on the `gixen list --json` pull | `gixen list` also dumps still-live PENDING snipes; filter on **both** `time_to_end == "ENDED"` and `status` contains `WON`, or you'll try to record auctions that haven't ended |
