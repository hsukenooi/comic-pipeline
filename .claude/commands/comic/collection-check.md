---
name: comic:collection-check
description: Check if identified comics are already in your collection via the gixen server API. Use when deciding whether to buy a comic to avoid duplicates.
---

# Comic Collection Check

Check whether identified comics are already in your collection by querying the
gixen server's collection API (`/api/comics/collection/*`). The server is the
single source of truth across machines (BUI-87), so both the MacBook and the Mac
Mini see the same answer.

> **Hard-fail rule (R11):** if the server is unreachable or any check call fails,
> **STOP** and tell the user — never render "Not in collection" from a failed
> call. A silent miss buys a duplicate. This is the whole point of the check.

## Input

A list of identified comics (series + issue, optionally variant and year). Either
from the `/comic:identify` output table or provided directly by the user.

## Step 0: Resolve the server + bootstrap guard

Resolve `GIXEN_SERVER_URL` (env var, with a hostname fallback) and confirm the
server is up before any checks — same pattern as `/comic:fmv` and
`/comic:snipe-add`:

```bash
echo "${GIXEN_SERVER_URL:-UNSET}"; hostname
```

If `GIXEN_SERVER_URL` is unset, infer it:
- `Hsus-MacBook-Air.local` → `http://mac-mini.tail9b7fa5.ts.net:8080`
- a Mac Mini hostname → `http://localhost:8080`
- neither → **stop** ("machine is unrecognised — set GIXEN_SERVER_URL").

Health gate, then read collection status:

```bash
curl -sf "$GIXEN_SERVER_URL/health" || { echo "server unreachable"; exit 1; }
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/status"
```

**If the health gate or status call fails:** STOP immediately — the collection
cannot be checked, so do not proceed to bidding. Do not report any comic as "not
in collection".

**If `last_full_import` is null:** Stop with:
> Collection empty on the server — run a full LOCG import (`/comic:collection-add`
> import flow) before checking.

Save `cache_age_days`, `pending_push_count`, and `oldest_pending_days` from the
response — you need them for output banners.

## Step 1: Check each comic against the server

For each comic, call the check endpoint. **Always pass `year`** when the
identification has one — it disambiguates volumes and enables the masthead-alias
fallback (BUI-46): e.g. a listing identified as "The Mighty Thor #154" (1968)
only resolves to the owned catalog entry "Thor #154" when the year is supplied.
Without a year that fallback is suppressed (to avoid colliding with same-masthead
reboots like *The Mighty Thor* Vol. 3), so an owned comic can slip through.

Use `curl -sf -G --data-urlencode` so series names with spaces are encoded and a
non-200 makes curl exit non-zero:

```bash
curl -sf -G "$GIXEN_SERVER_URL/api/comics/collection/check" \
  --data-urlencode "series=Amazing Spider-Man" \
  --data-urlencode "issue=300" \
  --data-urlencode "year=1988"

curl -sf -G "$GIXEN_SERVER_URL/api/comics/collection/check" \
  --data-urlencode "series=Uncanny X-Men" \
  --data-urlencode "issue=179" \
  --data-urlencode "year=1984" \
  --data-urlencode "variant=Newsstand"
```

Each call returns:
```json
{
  "match_status": "in_collection",
  "full_title_matched": "Amazing Spider-Man #300",
  "cache_age_days": 3
}
```

> **If any check call fails (curl non-zero / connection error / non-200): STOP
> the entire check.** Report the server error to the user and render NO verdicts
> — not even for the comics that already succeeded. A partial run invites a
> "not in collection" misread on the comics that never got checked (R11).

**Variant flag-through (R42):** If the listing has a variant (e.g. "Newsstand")
but the check with `variant=` returns `not_in_cache`, re-run without `variant` to
check the canonical entry. If the canonical matches, record the verdict as
`✅ In collection (canonical)` and add the note `⚠️ canonical match — listing
variant not disambiguated`.

## Step 2: Apply stale-cache verdict downgrade

**When `cache_age_days > 14` AND `match_status == "not_in_cache"`:** downgrade the
verdict from confident "Not in collection" to:

> ⚠️ Not in cache (cache N days stale — manual LOCG check recommended before bidding)

A stale import may be missing recently added comics. This prevents a snipe going
through on a comic you already own.

## Step 3: Output table

```
| # | Comic | In Cache? | Full Title Matched | Cache Age | Notes |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | ❌ Not in collection | — | 3 days | |
| 2 | Invincible #1 | ✅ In collection | Invincible #1 | 3 days | |
| 3 | Uncanny X-Men #179 (Newsstand) | ✅ In collection (canonical) | Uncanny X-Men #179 | 3 days | ⚠️ canonical match — listing variant not disambiguated |
| 4 | Batman #608 | ⚠️ Not in cache | — | 16 days | cache stale — manual LOCG check recommended |
```

Cache age is the same value for every row (it's a property of the import date,
not the comic).

**Status banners** (below the table):

- If `cache_age_days > 14`: `⚠️ Cache is N days old — consider re-importing from LOCG (leagueofcomicgeeks.com → My Comics → Export).`
- Pending push: `N rows pending push to LOCG; oldest pending = X days.` Escalate tone when `oldest_pending_days > 21` or `pending_push_count > 25`.

## Step 4: Decision gate

Ask the user how to handle results:

- **Skip** comics already in collection (most common)
- **Continue anyway** (condition upgrade — they want a better copy)
- **Stale-cache cases**: surface separately so the user can manually verify before bidding

Remove skipped comics from the working list before passing to `/comic:fmv`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Treating an unreachable server (or a failed check call) as "not in collection" | **STOP** — never render a "not owned" verdict from a failed call (R11). A silent miss buys a duplicate. |
| Omitting `year` | Always pass the identified year — it disambiguates volumes and enables the masthead-alias fallback (BUI-46); without it an owned comic listed by its cover title can slip through |
| Running checks before the `/health` gate passes | Health-gate first; a check against a down server is worthless and dangerous |
| Treating a stale-cache `not_in_cache` as confident "not in collection" | Apply the stale-cache downgrade when `cache_age_days > 14` |
