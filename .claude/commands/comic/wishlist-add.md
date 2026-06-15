---
name: comic:wishlist-add
description: Look up a series on Metron and add each of its issues to the wish-list on the gixen server, skipping any issues you already own. Only Metron is queried for the lookup; LOCG is not. Use to wish-list a whole run at once.
---

# Comic Wishlist Add

Add every issue of a series (or a sub-range) to the wish-list on the gixen server
(BUI-87). The issue count comes from **Metron** (metron.cloud); **no LOCG network
access is required** — adds go to the server's canonical wish-list via
`POST /api/comics/wish-list`, so they're visible on both machines immediately.

## Input

- **Series** (required) — e.g. `"Children of the Vault"`.
- **Issue range** (optional) — e.g. `1-4`, `1,3,5`, or `5-` (from #5 to the end).
  When omitted, add every issue `1 … issue_count`.

## Step 0: Metron credentials guard

Metron requires credentials. They live in `~/.config/locg/.env` as
`METRON_USERNAME` / `METRON_PASSWORD`.

```bash
set -a; . ~/.config/locg/.env 2>/dev/null; set +a
[ -n "$METRON_USERNAME" ] && [ -n "$METRON_PASSWORD" ] && echo "metron creds ok" || echo "MISSING"
```

**If `MISSING`:** Stop with:
> Metron credentials not found. Add `METRON_USERNAME` and `METRON_PASSWORD` to
> `~/.config/locg/.env` and retry.

Also resolve the gixen server (the wish-list now lives there) and health-gate it
— same `GIXEN_SERVER_URL` env + hostname fallback as `/comic:fmv`:

```bash
echo "${GIXEN_SERVER_URL:-UNSET}"; hostname   # unset → MacBook: http://mac-mini.tail9b7fa5.ts.net:8080
curl -sf "$GIXEN_SERVER_URL/health" || { echo "server unreachable"; exit 1; }
```

**If the health gate fails:** Stop — adds can't be written to an unreachable server.

## Step 1: Look up the series on Metron

```bash
curl -s -u "$METRON_USERNAME:$METRON_PASSWORD" \
  "https://metron.cloud/api/series/?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "<SERIES>")"
```

Each result has `id`, `series` (display name incl. year, e.g.
`"Children of the Vault (2023)"`), `year_began`, and `issue_count`.

**Disambiguation:**
- Exactly one result → use it.
- Multiple results → show the user the candidates (`series` + `year_began` +
  `issue_count`) and ask which one. If the user supplied a year, prefer the
  series whose `year_began` matches.
- Zero results → stop and report that Metron has no series by that name (suggest
  the user check spelling or supply the exact Metron title).

Record `issue_count` and the chosen `series` display name.

## Step 2: Resolve the issue list

- No range given → issues `1 … issue_count`.
- Range given → parse it (`1-4` → 1,2,3,4; `1,3,5` → those; `5-` → 5…issue_count).
  Clamp to `1 … issue_count`; warn if the user asked for issues beyond
  `issue_count`.

## Step 3: Skip issues you already own

Wish-listing a book you already own is the bug that deleted real collection rows
(BUI-122): an owned-but-wished book gets pushed to LOCG with `In Collection=0`,
which removes it from the collection. Filter owned issues out **before** adding.

For each resolved issue, ask the server's collection (no LOCG network needed):

```bash
# series = plain name (e.g. "Marvel Tales"); year = Metron year_began
curl -sf "$GIXEN_SERVER_URL/api/comics/collection/check?series=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "<SERIES>")&issue=<N>&year=<YEAR>"
```

- `{"match_status": "in_collection"}` → **owned, skip it** (don't wish-list).
- `{"match_status": "not_in_cache"}` → not owned, keep it.
- **HTTP 409** (store never imported) → ownership can't be verified. Warn the user
  ("couldn't check ownership — collection not imported"), and proceed with all
  issues. (The export fix is the safety net: even a wrongly-wished owned book is
  no longer deleted — but flag it so the user knows the check was skipped.)

Carry forward two lists: **to-add** (not owned) and **already-owned** (skipped).

## Step 4: Preview (dry run) and confirm

Before writing anything, print the issues that will be added, call out any already
owned, and ask the user to confirm. **Stopping here is the dry run.**

```
Marvel Tales #223–239 (17 issues):
  Already owned — skipping (8): #223, #226, #227, #228, #229, #231, #232, #234, #235
  Will add to wish-list (9): #224, #225, #230, #233, #236, #237, #238, #239
Proceed? (yes / no)
```

Use the series name as the user typed it for the `#<N>` titles (the simplest,
LOCG-searchable form). Mention that the Metron canonical name is
`<series display name>` in case they prefer that. If **all** issues are already
owned, say so and stop — nothing to add.

## Step 5: Add each issue

On confirmation, add one issue per call (`curl -sf` so a non-200 fails loudly):

```bash
curl -sf -X POST "$GIXEN_SERVER_URL/api/comics/wish-list" \
  -H 'content-type: application/json' -d '{"title": "Children of the Vault #1"}'
curl -sf -X POST "$GIXEN_SERVER_URL/api/comics/wish-list" \
  -H 'content-type: application/json' -d '{"title": "Children of the Vault #2"}'
# …
```

Each call appends `{name: "<title>", id: null}` to the server wish-list and
returns `{"status": "ok", ...}`. It is not deduped, so don't re-run a title that
already succeeded (it would create a duplicate). Stop and report if any call
returns a non-200.

**Owned-title guard (BUI-130):** `POST /api/comics/wish-list` now rejects an
already-owned title with **409** at the API boundary (defense in depth behind
Step 3's per-issue filter). If Step 3 was done correctly you'll never hit it; a
409 here means the book is owned — skip it. To wish-list an owned book on
purpose (a different printing/variant), pass `"force": true` in the body.

## Step 6: Report

```
**Wish-listed 9 issues of Marvel Tales:**
  #224, #225, #230, #233, #236–239  →  server wish-list (N items total)
  Skipped 8 already-owned issues.
```

**Sync note:** local wish-list adds **survive** a `collection import` (BUI-47 is
fixed — they're re-appended, not wiped). To get them onto LOCG itself, run
`/comic:collection-sync`; the export pushes only genuine new wishes and never
touches owned books. See
`packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Hitting LOCG to get the issue count | Use the Metron series API — `issue_count` is in the series result; LOCG is not needed |
| Guessing the issue count | Always read `issue_count` from Metron; don't assume a run length |
| Adding issues without a preview | Always show the title list and confirm first (Step 3) — that's the dry run |
| Re-running the whole range after a partial failure | Re-add only the issues that didn't succeed; the wish-list endpoint does not dedupe |
| Writing to `data/locg/wish-list.json` directly | Adds go to the server via `POST /api/comics/wish-list` — the repo file is no longer the source of truth (BUI-93) |
| Wish-listing issues you already own | Collection-check each issue first (Step 3) and skip owned ones — wishing an owned book is what deleted collection rows in BUI-122 |
