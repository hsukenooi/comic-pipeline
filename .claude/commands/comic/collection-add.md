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
`comics_resolve_server` (and, per BUI-430, `comics_scratch_dir`) itself** —
each `## Step` is a separate bash block with no shared shell state; don't
skip this because an earlier step already ran it (BUI-352). Full history
behind every inline warning in this skill:
`docs/solutions/best-practices/collection-add-record-win-tail-rationale-2026-07-19.md`.

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

`gixen record-win-prep` (BUI-353) owns the ENDED+WON filter, BUI-121 seen-set
dedup, and `comic-identify --batch` join in one tested place — don't
re-derive this by hand (rationale doc has the incident this replaced):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
SCRATCH="$(comics_scratch_dir)" || exit 1
gixen record-win-prep --output "$SCRATCH/prep.json" \
  || { echo "record-win-prep FAILED — see message above. STOP."; exit 1; }
python3 -c "import json; d=json.load(open('$SCRATCH/prep.json')); \
  print(json.dumps({k: d[k] for k in ['total_ended_won','new_win_count'] } | {'wins_ready': len(d['wins']), 'needs_review': len(d['needs_review'])}))"
```

Writes `$SCRATCH/prep.json` in this shape (`needs_review` rows carry
`title`/`current_bid`/`end_date_iso`/`reason`/`identity` instead of
`identify_data` — see Step 2):

```json
{
  "wins": [
    {"item_id": "318318338906", "current_bid": "222.50 USD",
     "end_date_iso": "2026-05-24T18:14:48+00:00",
     "identify_data": {"series": "Ghost Rider", "issue": "1", "year": 1973, "variant_text": "Newsstand"}}
  ],
  "needs_review": [],
  "total_ended_won": 9,
  "new_win_count": 2
}
```

**`total_ended_won` vs `new_win_count`** — the counter you check decides which
message to print:
- `total_ended_won == 0`: print "No won auctions to add." and stop (no ended+won snipes exist at all).
- `new_win_count == 0` (but `total_ended_won > 0`): print "All won auctions already processed. Nothing new to record." and stop (skip Steps 2–5) — everything ended+won was already recorded in a prior run.
- Otherwise: continue to Step 2.

**If `record-win-prep` exits non-zero, STOP** — do not process wins without a
real seen-set. It only falls back to an empty seen-set on a genuine 5xx
(BUI-34's already-owned dedup is the safety net there); any connectivity
failure or other unexpected status is a hard stop (BUI-352).

> **Called from another caller that already identified the comics** (e.g. a
> future `/comic:buy` integration): skip this step and hand-build
> `{"wins": [...]}` yourself in the shape above.

## Step 2: Resolve `needs_review` entries (BUI-354, BUI-422)

`needs_review` is the **only** gate. An entry lands here when
`comic-identify` returned a null `series`/`issue`, an `"error"`, a lot with
empty/unparseable `constituent_issues`, **or** a null `year` on a win priced
at/above $25 (`REASON_MISSING_YEAR`, BUI-422 — vintage no-year titles are
disproportionately prone to a downstream volume mis-resolution). There is
deliberately no confidence threshold — `comic-identify`'s baseline confidence
(0.5) would fire on nearly every real title (BUI-354; rationale doc has the
full story of both).

Resolve the same scratch dir Step 1 used:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_scratch_dir
```

If that dir's `prep.json`'s `needs_review` array is non-empty, ask the user
for `series`/`issue` per item (its `title`/`current_bid`/`identity` are
included, so no need to re-fetch the snipe list). Build each resolved entry
in Step 1's `wins` shape (reuse `item_id`/`current_bid`/`end_date_iso` from
the `needs_review` row, add the user-supplied `identify_data`) and write the
full list to `<scratch-dir>/resolved_reviews.json` as `{"wins": [...]}`.
**Always write the file, even if empty** (`{"wins": []}`) — Step 3 expects
it to exist.

## Step 3: Record + mark-seen + status (one call, BUI-428)

One atomic endpoint (BUI-428) merges `wins`+`resolved_reviews`, records,
marks seen exactly what it committed, and refreshes status — replacing three
hand-glued calls that used to drift from each other (rationale doc has the
history). Pass the two arrays **separately** — do not merge them yourself:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
SCRATCH="$(comics_scratch_dir)" || exit 1

python3 -c "import json; \
  a=json.load(open('$SCRATCH/prep.json'))['wins']; \
  b=json.load(open('$SCRATCH/resolved_reviews.json'))['wins']; \
  json.dump({'wins': a, 'resolved_reviews': b}, open('$SCRATCH/commit_request.json','w'))"

# rm -f BEFORE the POST — a stale response file must never be misread as
# this run's result (belt-and-suspenders on BUI-430's scratch dir).
rm -f "$SCRATCH/commit_response.json"
# Not `curl -f` (discards the error body) — a partial_failure needs
# rows_written out of the 500 body. %{http_code} is 000 if never connected.
code=$(curl -sS -o "$SCRATCH/commit_response.json" -w '%{http_code}' \
  -X POST "$COMICS_SERVER_URL/api/comics/collection/record-win/commit" \
  -H 'content-type: application/json' \
  -d @"$SCRATCH/commit_request.json")

if [ "${code:-000}" -ge 200 ] && [ "${code:-000}" -lt 300 ]; then
  # Pull only the summary scalars (full response can run to thousands of
  # tokens on a large batch; it stays on disk if you need it). An
  # unparseable/truncated 200 body is NOT success — exit non-zero.
  python3 -c "import json; d=json.load(open('$SCRATCH/commit_response.json')); \
    print(json.dumps({k: d.get(k) for k in ['rows_written','skipped_already_owned','manual_variant_count','manual_series_count','metron_lookups_succeeded','marked_seen','pending_push_count','oldest_pending_days']}))" \
    || { echo "record-win/commit returned HTTP $code but the body could not be parsed — see $SCRATCH/commit_response.json; do NOT assume success."; exit 1; }
else
  # STOP, non-zero exit. Nothing was marked seen (non-2xx never reaches the
  # server's mark-seen step, BUI-428/137) — nothing to undo, just report.
  echo "record-win/commit FAILED (HTTP $code) — STOP. Nothing was recorded or marked seen."
  python3 -c "import json; d=json.load(open('$SCRATCH/commit_response.json')); det=d.get('detail', d); \
    print('error:', det.get('error'), '| rows_written (committed before failure):', det.get('rows_written'))" 2>/dev/null \
    || echo "(no parseable response body — the request likely never reached the server)"
  exit 1
fi
```

Commits in batches of 25 (Metron resolution + BUI-34 already-owned dedup).
On 2xx, the scalar extraction above prints:

```json
{"rows_written": 3, "skipped_already_owned": 0, "manual_variant_count": 0,
 "manual_series_count": 1, "metron_lookups_succeeded": 2, "marked_seen": 3,
 "pending_push_count": 7, "oldest_pending_days": 4}
```

`marked_seen` is the BUI-121 seen-set count, keyed off exactly what was
merged and committed (never a client re-derivation). Carry
`pending_push_count`/`oldest_pending_days` straight into Step 5's report —
**do not** re-fetch status after Step 4's export (export never mutates
pending/pushed state).

The full response (including `skipped_already_owned_titles`/`_detail` and
`committed_item_ids`) is preserved at `$SCRATCH/commit_response.json`.
`manual_series_count > 0` means those rows have
`needs_manual_series_canonical=true` and will appear in the export's
`.notes.md`. **If the POST fails (non-2xx), STOP** — do not report success.

**Partial failures are non-200, never a misleading 200 (BUI-137):** a
mid-batch chunk failure returns HTTP 500 with `rows_written` (only-committed),
and marks nothing seen (BUI-428). The status check above already routes
this to the failure branch — never report success on a `partial_failure`.

**If both arrays are empty**, still POST — the endpoint returns zero-valued scalars plus current pending counts, so Step 5 still has fresh numbers.

## Step 4: Export to CSV

The export reads the *server* collection and returns the file contents; save them
locally for the LOCG upload:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
SCRATCH="$(comics_scratch_dir)" || exit 1

curl -sf "$COMICS_SERVER_URL/api/comics/collection/export" -o "$SCRATCH/export.json"
ts=$(date +%Y-%m-%dT%H%M%S)
python3 -c "import json,sys,os; d=json.load(open('$SCRATCH/export.json')); \
  base=os.path.expanduser(f'~/Downloads/locg-bulk-import-$ts'); \
  open(base+'.csv','w').write(d['csv']); open(base+'.notes.md','w').write(d['notes_md']); \
  print('csv:', base+'.csv', '| ready:', d['ready_count'])"
```

This writes a CSV at `~/Downloads/locg-bulk-import-<timestamp>.csv` plus a `.notes.md` sidecar listing any rows that need manual attention (unknown variant, unknown series canonical).

## Step 5: Report

No separate status call needed (BUI-428 collapsed it into Step 3): read
`pending_push_count`/`oldest_pending_days` straight out of Step 3's response.
**Do not use Step 0's status read** — it predates this run's wins and would
undercount by exactly the rows you just added (BUI-156).

`oldest_pending_days` is the age of the oldest uncleared item — **not** "days
since last sync." `needs_manual_series_canonical=true` rows never clear via
CSV export; they need a manual LOCG add + a `/comic:collection-sync` round-trip.

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
| Hand-merging Step 1's `wins` with Step 2's `resolved_reviews` before POSTing | Pass them as two separate arrays; `record-win/commit` merges server-side (BUI-428) |
| Re-deriving the mark-seen item_id set from a client-side file (e.g. `wins.json`) | The commit endpoint marks seen exactly what it merged and committed itself (BUI-428) |
| Confusing `oldest_pending_days` with "days since last sync" | `oldest_pending_days` = age of oldest uncleared item; use `last_full_import` from status for sync recency |
| Re-fetching `/api/comics/collection/status` after the Step 4 export | Not needed — Step 3's response already has fresh `pending_push_count`/`oldest_pending_days` (BUI-428) |
| Assuming `$COMICS_SERVER_URL` (or the scratch dir) carries over between Steps | Re-source `scripts/comics-server.sh` and call `comics_resolve_server`/`comics_scratch_dir` in every block that needs them (BUI-352, BUI-430) |
| Re-deriving the ENDED+WON filter / dedup / seen-subtract / positional-identify-mapping by hand | Use `gixen record-win-prep` (BUI-353) — it owns that join in one tested place |
| Asking the user "if confidence is low" | Not a real gate — baseline confidence is 0.5 for every clean parse; `needs_review` (Step 2) is the only gate (BUI-354) |
| Assuming `needs_review` only covers null series/issue/lot parsing | It also gates a null `year` at/above $25 (`REASON_MISSING_YEAR`, BUI-422 — vintage-key mis-resolution risk) |
