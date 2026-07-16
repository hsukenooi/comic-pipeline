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

**Every bash block below re-sources `scripts/comics-server.sh` and calls
`comics_resolve_server` itself.** Each `## Step` is a *separate* bash block —
they do not share shell state — so `COMICS_SERVER_URL` set in one block is
gone by the next. This bit BUI-352: an empty `$COMICS_SERVER_URL` used to make
a later `curl` fail with exit 3 ("No host part in the URL"), which an
`|| echo ""` fallback silently swallowed as an *empty seen-set*, misclassifying
every already-recorded win as new. Re-resolving in every block is cheap and
idempotent — do not skip it because "Step 0 already did this."

## Step 0: Resolve the server + bootstrap guard

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

## Step 1: Build the new-wins payload (`gixen record-win-prep`)

Steps 1/1b/2 used to be ~40 lines of hand-authored inline Python every run —
filter `gixen list --json` to ENDED+WON, dedup by `item_id`, subtract the
seen-set, then positionally zip `comic-identify --batch` output back onto the
new wins (an off-by-one there would silently mis-attribute an identity to the
wrong won auction). BUI-353 moved all of that into one tested subcommand:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
gixen record-win-prep --output /tmp/prep.json \
  || { echo "record-win-prep FAILED — see message above. STOP."; exit 1; }
python3 -c "import json; d=json.load(open('/tmp/prep.json')); \
  print(json.dumps({k: d[k] for k in ['total_ended_won','new_win_count'] } | {'wins_ready': len(d['wins']), 'needs_review': len(d['needs_review'])}))"
```

`gixen record-win-prep` does, in one call: pull `gixen list --json` → filter to
`time_to_end == "ENDED"` + `status` contains `WON` → dedup by `item_id` → fetch
the BUI-121 seen-set and subtract it → run `comic-identify --batch` on the
remaining titles → build `identify_data` for each (expanding lots across
`constituent_issues`) → write `/tmp/prep.json`:

```json
{
  "wins": [
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
  ],
  "needs_review": [
    {
      "item_id": "444555666",
      "title": "Marvel Silver Age Lot",
      "current_bid": "45.00 USD",
      "end_date_iso": "2026-05-20T10:00:00+00:00",
      "reason": "lot with unparseable contents",
      "identity": {"series": "Marvel", "issue": null, "is_lot": true, "constituent_issues": [], "error": null}
    }
  ],
  "total_ended_won": 9,
  "new_win_count": 2
}
```

**`total_ended_won` vs `new_win_count`** — the counter you check decides which
message to print:
- `total_ended_won == 0`: print "No won auctions to add." and stop (no ended+won snipes exist at all).
- `new_win_count == 0` (but `total_ended_won > 0`): print "All won auctions already processed. Nothing new to record." and stop (skip Steps 2–5) — everything ended+won was already recorded in a prior run.
- Otherwise: continue to Step 2.

**BUI-352 hardening lives inside the seen-set fetch this command makes:** a
local/connectivity failure (bad or missing `COMICS_SERVER_URL`, connection
refused, timeout) makes the command exit non-zero — **STOP**, do not process
wins without a real seen-set. Only a genuine 5xx from the seen-set endpoint
falls back to an empty seen-set (the server's own already-owned dedup, BUI-34,
is the safety net for that one case); any other unexpected status is also a
hard stop. You should never see the old silent "130 wins classified as new"
failure mode from this command.

> **In-session context override:** if this skill is being invoked from a
> caller that already identified the comics (e.g. a future `/comic:buy`
> integration), skip this step and hand-build the `{"wins": [...]}` array
> yourself in the shape shown above — `gixen record-win-prep` exists for the
> standalone path where titles still need parsing.

## Step 2: Resolve `needs_review` entries (BUI-354)

`needs_review` is the **only** gate — an entry lands here when
`comic-identify` returned a null `series`/`issue`, an `"error"`, or a lot with
empty/unparseable `constituent_issues`. There is deliberately no confidence
threshold: `comic-identify` returns `confidence: 0.5` as its baseline for
every cleanly-parsed title with no year to cross-check, so "ask if confidence
is low" would fire on nearly every real title and defeat the batch automation
— that clause has been dropped, not tightened.

If `/tmp/prep.json`'s `needs_review` array is non-empty, read it (small — one
row per unresolved title) and ask the user for `series`/`issue` for each
specific item (its `title`, `current_bid`, and `identity` are included so you
don't need to go back to the raw snipe list). For each one the user resolves,
build an entry in the same shape as Step 1's `wins` array (`item_id`,
`current_bid`, `end_date_iso` — all already present on the `needs_review` row
— plus the user-supplied `identify_data`) and write the full list to
`/tmp/resolved_reviews.json` as `{"wins": [...]}`. **If nothing was resolved
(no `needs_review`, or the user skipped some), still write the file** —
`{"wins": []}` for "nothing to add" — so Step 3's merge below always has a
file to read rather than branching on whether one exists.

## Step 3: Record wins

Merge Step 1's `wins` with Step 2's `/tmp/resolved_reviews.json`, wrap as
`{"wins": [...]}`, write it to a temp file, and POST it to the server's
record-win endpoint. **Capture the response body to a file and read only the
summary scalars into context** — the full response also carries
`skipped_already_owned_titles` and `skipped_already_owned_detail` (one object per
already-owned win), which on a large batch is thousands of tokens of detail you
don't need in the loop. Keep it on disk; surface the scalars:

```bash
# /tmp/wins.json: Step 1's wins + Step 2's resolved_reviews, concatenated —
# this is the actual merge; do not skip it even when resolved_reviews.json is
# {"wins": []} (an earlier draft of this step only ever copied prep.json's
# wins and silently dropped anything resolved in Step 2 — don't repeat that).
python3 -c "import json; \
  a=json.load(open('/tmp/prep.json'))['wins']; \
  b=json.load(open('/tmp/resolved_reviews.json'))['wins']; \
  json.dump({'wins': a + b}, open('/tmp/wins.json','w'))"

source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1

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

**If `/tmp/wins.json`'s `wins` array is empty** (everything from Step 1 landed in `needs_review` and none were resolved), skip the POST — there is nothing to record — and go straight to Step 4/5 to still report current collection status.

## Step 3b: Mark new wins seen (BUI-121)

Only after a **successful** record-win POST, mark the new item IDs as processed
so future runs skip them. Best-effort — a failed mark is non-fatal (the
already-owned dedup will catch a re-POST):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1

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
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1

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
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1

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
| Assuming `$COMICS_SERVER_URL` carries over between Steps | Each `## Step` is a separate bash block with its own shell — re-source `scripts/comics-server.sh` and call `comics_resolve_server` in every block that calls the server, even if an earlier step already did (BUI-352) |
| Re-deriving the ENDED+WON filter / dedup / seen-subtract / positional-identify-mapping by hand | Use `gixen record-win-prep` (BUI-353) — it owns that join in one tested place instead of ~40 lines of inline Python re-authored (and re-risked) every run |
| Asking the user "if confidence is low" | Not a real gate — `comic-identify`'s baseline confidence is 0.5 for every clean parse. The only gate is `needs_review` (null series/issue, or an unparseable lot) — BUI-354 |
