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

## Step 3: Record + mark-seen + status (one call, BUI-428)

Steps 3/3b/5 used to be three separate calls glued together by hand-authored
client code: an inline `a + b` merge of Step 1's `wins` and Step 2's
`resolved_reviews` (an earlier draft silently dropped the resolved rows),
then a mark-seen POST that re-derived its item_id set from `/tmp/wins.json`
independently of what record-win actually committed — so a wrong merge could
key record-win and mark-seen off two *different* sets. BUI-428 moved the
merge, the record, the mark-seen, and the status re-read into one endpoint —
`POST /api/comics/collection/record-win/commit` — that does all four
atomically server-side: it merges `wins` + `resolved_reviews` itself, records
via the unchanged Metron/BUI-34 path, marks seen **exactly** the item_ids it
just merged and submitted (never a client re-derivation), and — only on full
success — folds in a fresh `pending_push_count`/`oldest_pending_days` read.
On a partial or failed commit it marks nothing seen at all (record + mark-seen
are atomic), so there is no separate best-effort mark-seen step to run.

Pass Step 1's `wins` and Step 2's `resolved_reviews` as **two separate
arrays** in the request body — do not merge them yourself; that merge is
exactly what this endpoint now owns:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1

python3 -c "import json; \
  a=json.load(open('/tmp/prep.json'))['wins']; \
  b=json.load(open('/tmp/resolved_reviews.json'))['wins']; \
  json.dump({'wins': a, 'resolved_reviews': b}, open('/tmp/commit_request.json','w'))"

# rm the response file FIRST: on a connection failure curl leaves any prior
# run's file in place (it only truncates -o once bytes arrive), and reading a
# stale body would fabricate a "committed before failure" count for a request
# that never left the machine.
rm -f /tmp/commit_response.json
# Capture body + status separately (not `curl -f`, which discards the error
# body): on a partial_failure we still need rows_written out of the 500 body.
# curl still prints %{http_code} == 000 when it never connects.
code=$(curl -sS -o /tmp/commit_response.json -w '%{http_code}' \
  -X POST "$COMICS_SERVER_URL/api/comics/collection/record-win/commit" \
  -H 'content-type: application/json' \
  -d @/tmp/commit_request.json)

if [ "${code:-000}" -ge 200 ] && [ "${code:-000}" -lt 300 ]; then
  # Success — pull only the summary scalars into context (the full response
  # also carries skipped_already_owned_titles/_detail, which on a large batch
  # is thousands of tokens you don't need in the loop; it stays on disk at
  # /tmp/commit_response.json if you need to inspect it).
  # A 200 with an unparseable/truncated body is NOT success — exit non-zero.
  python3 -c "import json; d=json.load(open('/tmp/commit_response.json')); \
    print(json.dumps({k: d.get(k) for k in ['rows_written','skipped_already_owned','manual_variant_count','manual_series_count','metron_lookups_succeeded','marked_seen','pending_push_count','oldest_pending_days']}))" \
    || { echo "record-win/commit returned HTTP $code but the body could not be parsed — see /tmp/commit_response.json; do NOT assume success."; exit 1; }
else
  # Failure — STOP with a NON-ZERO exit. Nothing was marked seen (the server
  # never reaches its mark-seen step on a non-2xx — see BUI-428/BUI-137), so
  # there is nothing to undo; just report and stop before Step 4's export.
  echo "record-win/commit FAILED (HTTP $code) — STOP. Nothing was recorded or marked seen."
  python3 -c "import json; d=json.load(open('/tmp/commit_response.json')); det=d.get('detail', d); \
    print('error:', det.get('error'), '| rows_written (committed before failure):', det.get('rows_written'))" 2>/dev/null \
    || echo "(no parseable response body — the request likely never reached the server)"
  exit 1
fi
```

The server merges the two arrays, then commits in batches of 25 using the
same locg-cli logic (Metron series resolution + BUI-34 already-owned dedup).
On a 2xx the scalar extraction above prints just:

```json
{
  "rows_written": 3,
  "skipped_already_owned": 0,
  "manual_variant_count": 0,
  "manual_series_count": 1,
  "metron_lookups_succeeded": 2,
  "marked_seen": 3,
  "pending_push_count": 7,
  "oldest_pending_days": 4
}
```

`marked_seen` is the count of item_ids the server just marked processed — the
BUI-121 seen-set — keyed off exactly the wins+resolved_reviews it merged and
committed, not a re-derivation. `pending_push_count`/`oldest_pending_days` are
the SAME fresh status fields Step 5 used to fetch with a separate GET — carry
them straight into Step 5's report; **do not** re-fetch status after Step 4's
export (the export never mutates pending/pushed state, so this read is
already current).

The full response — including the `skipped_already_owned_titles` /
`skipped_already_owned_detail` arrays and `committed_item_ids` — is preserved
at `/tmp/commit_response.json` if the user wants to inspect which owned titles
were skipped.

`manual_series_count > 0` means those rows have `needs_manual_series_canonical=true` and will appear in the export's `.notes.md` for follow-up. **If the POST fails (non-2xx), STOP** — do not report success; nothing was recorded or marked seen.

**Partial failures are non-200 (BUI-137):** the server commits in chunks of 25; if a later chunk fails to write, the endpoint returns **HTTP 500** with `{"detail": {"error": "partial_failure", "rows_written": <only-committed>, ...}}` rather than a misleading 200 — and, per BUI-428, marks nothing seen. The status-code check above routes this to the failure branch, which surfaces `rows_written` so the user knows which wins still need recording — never report success on a partial_failure.

**If both `wins` and `resolved_reviews` are empty** (everything from Step 1 landed in `needs_review` and none were resolved), still POST — the endpoint returns zero-valued scalars plus the current `pending_push_count`/`oldest_pending_days` in one call, so Step 5 still has fresh numbers to report.

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

No separate status call needed (BUI-428 collapsed it into Step 3): read
`pending_push_count` and `oldest_pending_days` straight out of Step 3's
`/tmp/commit_response.json` — they were read fresh, right after the record-win
write, and the Step 4 export never mutates pending/pushed state, so that
number is still current. (The Step 0 status read predates this run's wins and
would undercount by exactly the rows you just added — BUI-156 — which is why
Step 3's own post-write read, not Step 0's, is the source here.)

Note: `oldest_pending_days` is the age of the oldest uncleared pending item — it
is **not** "days since last sync." Items with `needs_manual_series_canonical=true`
never get cleared by an automated CSV export; they require a manual LOCG add
followed by a full `/comic:collection-sync` round-trip.

Print a summary (source `pending_push_count` from Step 3's response,
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
| Using Playwright to add comics directly to LOCG | POST to `/api/comics/collection/record-win/commit` then GET `/api/comics/collection/export` — no Playwright needed |
| POSTing the bare entries array | The endpoint expects `{"wins": [...], "resolved_reviews": [...]}`, not a top-level array |
| Recording wins against an unreachable server | Health-gate first; if the POST fails, STOP and report — don't claim success |
| Passing LOCG IDs as part of record-win input | `record-win` does not take LOCG IDs; it resolves series via Metron and the server collection |
| Leaving `series` or `issue` blank in `identify_data` | Ask the user for the specific snipe — do not guess |
| Hand-merging Step 1's `wins` with Step 2's `resolved_reviews` before POSTing | Don't — pass them as two separate arrays and let `/api/comics/collection/record-win/commit` merge them server-side (BUI-428); an earlier inline `a + b` draft silently dropped resolved rows |
| Re-deriving the mark-seen item_id set from a client-side file (e.g. `/tmp/wins.json`) | Don't — the commit endpoint marks seen exactly the item_ids it merged and committed itself (BUI-428); a client re-derivation can key off a different set than what was actually recorded |
| Confusing `oldest_pending_days` with "days since last sync" | `oldest_pending_days` = age of oldest uncleared item; use `last_full_import` from status for sync recency |
| Re-fetching `/api/comics/collection/status` after the Step 4 export | Not needed — Step 3's commit response already carries a fresh `pending_push_count`/`oldest_pending_days`, and export never mutates pending/pushed state (BUI-428) |
| Assuming `$COMICS_SERVER_URL` carries over between Steps | Each `## Step` is a separate bash block with its own shell — re-source `scripts/comics-server.sh` and call `comics_resolve_server` in every block that calls the server, even if an earlier step already did (BUI-352) |
| Re-deriving the ENDED+WON filter / dedup / seen-subtract / positional-identify-mapping by hand | Use `gixen record-win-prep` (BUI-353) — it owns that join in one tested place instead of ~40 lines of inline Python re-authored (and re-risked) every run |
| Asking the user "if confidence is low" | Not a real gate — `comic-identify`'s baseline confidence is 0.5 for every clean parse. The only gate is `needs_review` (null series/issue, or an unparseable lot) — BUI-354 |
