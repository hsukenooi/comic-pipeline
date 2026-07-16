---
name: comic:wishlist-add
description: Look up a series on Metron and add each of its issues to the wish-list on the comics server, skipping any issues you already own. Only Metron is queried for the lookup; LOCG is not. Use to wish-list a whole run at once.
---

# Comic Wishlist Add

Add every issue of a series (or a sub-range) to the wish-list on the comics server
(on the Mac Mini; BUI-87). The issue count comes from **Metron** (metron.cloud); **no LOCG network
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

Also source the shared Metron call convention
(`docs/conventions/metron-api-best-practices.md`, BUI-262) — every Metron call
in this skill routes through `metron_curl`/`metron_paginate` rather than a
hand-rolled `curl` (that doc has the rate-limit/retry rationale):

```bash
source "$(git rev-parse --show-toplevel)/scripts/metron-curl.sh"
```

Also resolve and health-gate the comics server (the wish-list now lives there)
through the shared comics-server convention
(`docs/conventions/comics-server-call.md`, BUI-172):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
comics_health_gate     || exit 1
```

**If either fails:** Stop — adds can't be written to an unreachable server.

## Step 1: Look up the series on Metron

```bash
metron_curl "https://metron.cloud/api/series/?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "<SERIES>")"
```

**Filter by start year server-side when you know it (BUI-204).** The bare `name`
search can return hundreds of rows (an unfiltered `X-Men` returned 327) — the
full payload is the single biggest token cost of this skill. When the user
supplied a year, or you otherwise know the run's start year, add Metron's
`year_began` query param so the server returns only the matching series instead
of every same-named title:

```bash
metron_curl "https://metron.cloud/api/series/?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "<SERIES>")&year_began=<YEAR>"
```

Each result has `id`, `series` (display name incl. year, e.g.
`"Children of the Vault (2023)"`), `year_began`, and `issue_count`.

**Disambiguation:**
- Exactly one result → use it.
- Multiple results → show the user the candidates (`series` + `year_began` +
  `issue_count`) and ask which one. If the user supplied a year, prefer the
  series whose `year_began` matches (or just add `&year_began=<YEAR>` above to
  narrow the search before you ever see the candidates).
- Zero results → stop and report that Metron has no series by that name (suggest
  the user check spelling or supply the exact Metron title). If you used a
  `year_began` filter, retry once without it before declaring zero — the year may
  have been wrong, not the name.

Record `issue_count` and the chosen `series` display name.

## Step 2: Resolve the issue list (with per-issue cover years)

Fetch the series' issues from Metron so each carries its **cover date**. The
per-issue cover year is what the BUI-184 ownership check needs (Step 3) and is
the ONLY safe year to pass it — never `year_began` (BUI-129). Paginate
`/api/issue/` with `metron_paginate`, which walks `next` sequentially (never
parallel, per Metron's best practices) and emits one result per line:

```bash
metron_paginate "https://metron.cloud/api/issue/?series_id=<SERIES_ID>" | while IFS= read -r issue; do
  number="$(printf '%s' "$issue" | jq -r '.number')"
  cover_date="$(printf '%s' "$issue" | jq -r '.cover_date')"
  # ... accumulate number -> cover_year into your map
done
```

Each result has `number` and `cover_date` (e.g. `"1968-07-01"`). Build a map
`number → cover_year` (the 4-digit year of `cover_date`; leave it empty for an
issue Metron has no `cover_date` for). `metron_paginate` itself stops once
`next` is null — no manual loop-until-null logic needed.

- No range given → every issue `number` Metron returned.
- Range given → parse it (`1-4` → 1,2,3,4; `1,3,5` → those; `5-` → 5…last) and
  intersect with the returned numbers; warn about any requested number Metron
  doesn't list.

## Step 3: Skip issues you already own

Wish-listing a book you already own is the bug that deleted real collection rows
(BUI-122): an owned-but-wished book gets pushed to LOCG with `In Collection=0`,
which removes it from the collection. Filter owned issues out **before** adding.

**First, reconcile the Metron series name to the LOCG catalog spelling (BUI-171),**
the same defense `/comic:collection-check` uses. The matcher already neutralizes
leading articles (`The`/`A`/`An`), `(Vol. N)`, and year suffixes, but a genuine
alt-spelling (punctuation, abbreviation, Metron-vs-LOCG word choice) makes every
owned issue return a false `not_in_cache` — which Step 3 reads as "not owned" and
wish-lists a book you already own. Fetch the catalog's actual series names and
match the Metron `series` against them:

```bash
comics_curl "$COMICS_SERVER_URL/api/comics/collection/series-names" || exit 1
```

Normalized-match the Metron `series` (strip a leading article, lowercase) against
the returned names. If a confident catalog match exists, use **that catalog
spelling** as the `series` param in the per-issue check below. If none matches,
proceed with the Metron name but note that ownership for this series couldn't be
reconciled — a `not_in_cache` here may be a spelling miss, not genuinely un-owned.

Check every resolved issue against the server's collection in **one batch call**
(BUI-204), not a serial per-issue loop. The batch endpoint runs the exact same
matcher the single-item `check` does, so the verdicts are identical — it just
collapses N round-trips into one and cuts the token cost of the per-issue
`curl` chatter.

**Pass each issue's COVER year from Step 2 (BUI-184) — never `year_began`
(BUI-129).** The `year` param is gated on `release_date.startswith(year)`, so it
must be the issue's own cover year (Step 2's `cover_date`), NOT the series start
year: forwarding `year_began` (e.g. `1963` for a run whose issues actually shipped
1975–1991) filters out every owned mid-run issue and returns a false
`not_in_cache` for the whole series — wish-listing books you already own, the
exact BUI-122 deletion path Step 3 exists to prevent. With the *correct* cover
year, the check's year-gated masthead fallback also catches a book stored under
its base masthead (you ask for "The Mighty Thor #154", you own "Thor #154"). If
Metron had no `cover_date` for an issue, **omit `year` for that item** (the
pre-184 behavior — safe; it just can't catch the masthead case).

Build the request body as a list of `{series, issue, year?}` items — `series` is
the reconciled catalog name, `issue` is each `<N>`, `year` is THAT issue's cover
year (drop the key entirely for an issue with no `cover_date`):

```bash
# Build items.json programmatically from your number→cover_year map, e.g.:
#   {"items":[{"series":"Uncanny X-Men","issue":"185","year":"1984"},
#             {"series":"Uncanny X-Men","issue":"186","year":"1984"}, ...]}
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/check/batch" \
  -H 'content-type: application/json' \
  -d @items.json
```

The response is `{"count": N, "results": [{series, issue, match_status,
full_title_matched, cache_age_days, printing_conflict, printing_candidates},
...]}` — one entry per input item, echoing its `series`/`issue` so you can
correlate by key (don't rely on order). Per item:

- `{"match_status": "in_collection", "printing_conflict": false}` → **owned,
  skip it** (don't wish-list).
- `{"match_status": "ambiguous_cross_volume", "printing_conflict": false}` →
  **owned, skip it** (don't wish-list) — same as `in_collection` (BUI-284:
  owned under >1 volume, no year to disambiguate — still an ownership
  signal). This is backstopped by the per-issue `POST /api/comics/wish-list`
  owned-guard, which also treats it as owned and 409s (so a slip-through here
  isn't a data-loss risk, just a wasted round-trip) — but skip it client-side
  anyway (BUI-302) so it never reaches the to-add list in the first place.
- **`printing_conflict: true` (either match_status above) → do NOT auto-skip
  (BUI-372, Pattern E from `/comic:collection-check` — BUI-364).** The match
  was satisfied by a row whose `full_title` names a printing this query never
  asked for ("2nd Printing", "Reprint", …). Printings are distinct
  collectibles — owning the reprint is NOT owning the base printing (the
  confirmed AMM #1 incident: *Absolute Martian Manhunter #1* read as owned off
  an owned "2nd Printing" row while the base sat wish-listed; unpatched, Step 3
  would have silently skipped wish-listing that explicitly wanted base
  printing). Put this issue in a THIRD bucket — **printing-conflict (needs a
  decision)** — instead of already-owned, carrying `full_title_matched` and
  `printing_candidates` forward to Step 4. Do not decide for the user; the
  candidates list (with each row's `printing_ordinal`/`in_collection`/
  `in_wish_list`) is what lets them see whether *their* printing is
  untracked/wishlisted before choosing.
- `{"match_status": "not_in_cache"}` → not owned, keep it.

The batch call's HTTP status is the whole-batch signal:

- **HTTP 409** (store never imported) → ownership can't be verified for ANY item.
  Warn the user ("couldn't check ownership — collection not imported"), and
  proceed with all issues. (The export fix is the safety net: even a wrongly-
  wished owned book is no longer deleted — but flag it so the user knows the
  check was skipped.) Because `curl -sf` exits non-zero on the 409, capture the
  status explicitly (e.g. `-o body -w '%{http_code}'`) so you can distinguish
  this expected case from a real network failure rather than silently treating
  every issue as un-owned (R11 — a failed call must never read as "not owned").
- **Any other non-200** (500, network error) → hard-fail; do not wish-list
  anything from a failed check (R11).

Carry forward three lists: **to-add** (not owned), **already-owned** (skipped,
no printing conflict), and **printing-conflict** (BUI-372 — owned match, but
under a different printing; needs the user's decision at Step 4, never
auto-skipped and never auto-added).

## Step 3b: Skip issues already on the wish-list (single in-memory scan)

The wish-list endpoint is idempotent as of BUI-285 (a re-added series+issue is a
200 no-op returning `{"status": "exists", ...}`, not a duplicate row), but still
filter duplicates out up front — the client-side scan avoids N redundant POSTs
and keeps the "already wished" list accurate for the Step 6 report. Fetch the
wish-list **once** and scan it **in memory** (BUI-204), not re-fetched or
re-grepped per issue. A real wish-list is large (685 items in the motivating
run); a per-issue grep over that payload is the redundant work this step removes.

```bash
comics_curl "$COMICS_SERVER_URL/api/comics/wish-list" || exit 1
```

Parse the returned `[{name, id, ...}]` **once** into a set keyed by
`(series, issue)` — split each `name` on the trailing `#<N>` the same way Step 4
forms titles, normalize (lowercase, strip a leading article) so the key matches
your to-add titles. Then for each to-add issue do an O(1) lookup against that set:
already present → drop it from **to-add** (note it as "already wished"); absent →
keep it. Do not call the wish-list endpoint again inside the loop.

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
owned (and none are printing-conflict), say so and stop — nothing to add.

**Printing-conflict bucket (BUI-372).** Render these separately from both
"already owned" and "will add" — they are neither, until the user decides:

```
Printing conflict — needs your decision (1):
  #300 — matched "Amazing Spider-Man #300 2nd Printing" (a different printing
    than this wish); per printing_candidates the base printing is untracked
    (not owned, not wish-listed). Add the base printing anyway? (yes / no)
```

For each flagged issue, show `full_title_matched` and, from
`printing_candidates`, the query's own printing's state (owned / wish-listed /
untracked, via `printing_ordinal` — 1 is the base). Ask the user per issue
(or as a reviewed batch) whether to add it. An issue the user confirms moves
into the same add list Step 5 writes; one they decline moves to "already
owned" for the report. Never auto-resolve this bucket either way — a false
"add" risks a redundant wish-list entry the owned-guard would 409 anyway (safe
direction), but a false "skip" reproduces the AMM #1 incident (a missed wish
for a book actually wanted).

## Step 5: Add each issue

On confirmation, add one issue per call (`curl -sf` so a non-200 fails loudly)
— this includes both the original to-add list and any printing-conflict
issues the user confirmed adding in Step 4:

Include each issue's **cover year** (Step 2) in the body so the server-side
owned-guard's masthead fallback (BUI-184) gets the same catch the Step 3 filter
does; omit `year` only for an issue Metron had no `cover_date` for.

```bash
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/wish-list" \
  -H 'content-type: application/json' \
  -d '{"title": "The Mighty Thor #154", "year": "1968"}'
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/wish-list" \
  -H 'content-type: application/json' -d '{"title": "Children of the Vault #2"}'  # no cover_date → no year
# …
```

Each call appends `{name: "<title>", id: null}` to the server wish-list and
returns `{"status": "ok", ...}`. As of BUI-285 the endpoint is idempotent: a
re-added series+issue returns `{"status": "exists", ...}` with 200 (no duplicate
row), so a retried title is safe. Stop and report if any call returns a non-200.

**Owned-title guard (BUI-130/BUI-184):** `POST /api/comics/wish-list` rejects an
already-owned title with **409** at the API boundary (defense in depth behind
Step 3's per-issue filter). With the per-issue `year` in the body it now also
catches a masthead-stored owned book (BUI-184). If Step 3 was done correctly
you'll never hit it; a 409 here means the book is owned — skip it. To wish-list
an owned book on purpose (a different printing/variant), pass `"force": true`.

**Printing-conflict adds need `force=true` (BUI-372).** Any issue you're
posting because the user confirmed a Step 4 printing-conflict decision WILL
409 again here without `force: true` — the owned-guard re-runs the exact same
check and matches the exact same reprint row. The 409's `detail` now carries
`printing_conflict`/`printing_candidates` alongside the message (additive
JSON fields, BUI-372) precisely so you can tell "this 409 is the printing
decoy Step 4 already resolved — retry with force=true" apart from a genuine
duplicate you should actually skip.

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

**Removing a wished issue:** the wish-list also has a DELETE endpoint (BUI-128),
so you no longer need to SSH into the Mac Mini to run `locg wish-list remove`:

```bash
curl -sf -X DELETE "$COMICS_SERVER_URL/api/comics/wish-list?title=Children+of+the+Vault+%231"
```

Pass the exact `name` of the wished entry as the `title` query param (URL-encoded).
Returns `{"status": "ok", "removed": {...}, "items": N}` on success, **404** if no
entry matches that title, **422** if the title is blank. Like an add, a removal is
overwritten by the next full `collection import` unless pushed to LOCG first.

## Creator runs (BUI-134): "add X's run on series Y"

"Add John Romita Jr.'s run on Uncanny X-Men to the wish-list" has **no
ground-truth source in model memory** — and memory silently drops DISCONTINUOUS
runs. Asked for JR JR's Uncanny X-Men pencils, an agent recalls only #175–211
and misses his ~1993 second stint (#287, #300–311).

**Never enumerate a creator run from memory — for ANY claim, not just a
wish-list write.** This includes a bare conversational question ("what was
Erik Larsen's Spider-Man run?") with no wish-list intent at all. BUI-340: asked
exactly that as a plain question, an agent answered from model memory (said
#19–43) instead of grounding it in Metron, because invoking the full
wish-list-add flow felt like the wrong tool for "just answer a question." The
Metron-credited run was actually #18–23. The fix isn't "remember to ground
it" — it's reaching for the right tool, which now exists:

- **Just answering a question, no write intended** → `locg creator-run`
  (below) — read-only, zero cache/file writes, prints the resolved issue
  list/range. This is the tool to reach for by default whenever a creator-run
  claim is needed, wish-list-add or not.
- **Actually adding the run's gap issues to the wish-list** → `locg wish-list
  add --creator …` (this section) — resolves the same way, then writes.

### Read-only lookup: `locg creator-run`

For a plain question — no wish-list write intended — use the read-only
counterpart instead of the write path:

```bash
locg creator-run "The Amazing Spider-Man" \
  --creator "Erik Larsen" --series-id <METRON_SERIES_ID> --role penciller
```

It calls the same `resolve_creator`/`resolve_creator_run` Metron methods
described below (same id-pinning, same discontinuous-stint handling, same
per-issue role confirmation) and prints `{creator, creator_id, issues,
issue_numbers, warnings, ...}` — no collection check, no wish-list dedup, no
cache read or write of any kind. Use this whenever the ask is "what was X's
run" rather than "add X's run."

### Writing the run to the wish-list

To actually add the run's gap issues to the wish-list, ground it in Metron's
per-issue creator credits via the `locg` resolver:

```bash
# series = the LOCG-searchable title used for the "<series> #<N>" wish entries
# --series-id = the Metron series id (from the Step 1 series lookup)
# --creator   = the EXACT Metron creator name (disambiguates JR vs Sr by id)
# --role      = credit role to filter by (default: penciller)
locg wish-list add "Uncanny X-Men" \
  --creator "John Romita Jr." --series-id <METRON_SERIES_ID> --role penciller
```

What it does, in order:
1. **Pins the creator's Metron id** (`/creator/?name=`). "John Romita Jr." and
   "John Romita" (Sr.) are distinct ids — the resolver matches by id, never a
   loose name string. An ambiguous or unknown name is a **hard error**, not a
   guess; pass the exact Metron creator name.
2. **Resolves the EXACT issue set** the creator holds `--role` on, from each
   issue's Metron credits. The candidate set comes from Metron's issue-list
   `creator` filter (so BOTH stints are in scope), then each issue's credits
   confirm the role. This returns the discontinuous #287/#300–311 stint that
   memory drops.
3. **Filters owned + already-wishlisted issues** before any write — owned via
   the same per-issue collection check (by that issue's **cover year**, never
   `year_began`, BUI-129), already-wishlisted via the local cache.
4. Appends the remaining `"<series> #<N>"` titles to the wish-list cache.

**Role is EXPLICIT.** The default `penciller` matches **only** a `Penciller`
credit — it does NOT auto-include `Breakdowns`, `Layouts`, `Co-Penciller`, etc.
To widen the run, pass that role name explicitly (`--role breakdowns`); you can
only request one role per call.

**Low-confidence WARNING on thin credits.** Metron's credit data is sparse/
occasionally wrong on older Silver/Bronze books. An issue in the candidate set
that Metron has **no credits at all** for is reported in the result's `warnings`
(not silently treated as "not in the run"). Surface these to the user — the run
membership for those issues is unverified and may need a manual eyeball.

The result JSON carries `added`, `already_owned`, `already_wishlisted`,
`warnings`, `creator`/`creator_id` (the pinned Metron id), and `run_issue_count`.
Show the user the preview (added vs skipped vs warnings) and confirm before this
is treated as final, same as the numeric-range path.

> Note: the `locg wish-list add` path writes the **local** cache. The
> server-backed `POST /api/comics/wish-list` flow above (Steps 1–6) is the
> machine-visible path; the creator-run resolver is currently a local-cache
> convenience for enumerating the exact issue set. When adding to the server,
> feed the resolved issue numbers into the Step 5 `POST` loop.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Re-running the whole range after a partial failure | Safe to re-add — the wish-list endpoint is idempotent (BUI-285): a duplicate series+issue is a 200 no-op (`{"status": "exists"}`), not a second row |
| Writing to `data/locg/wish-list.json` directly | Adds go to the server via `POST /api/comics/wish-list` — the repo file is no longer the source of truth (BUI-93) |
| Wish-listing issues you already own | Collection-check each issue first (Step 3) and skip owned ones — wishing an owned book is what deleted collection rows in BUI-122 |
| Passing `year` (Metron's `year_began`) to `collection/check` | `year` is a *per-issue cover year* gated on `release_date.startswith(year)`, not a series disambiguator. Forwarding a series start-year filters out every owned mid-run issue and returns a false `not_in_cache`, so an owned book gets wish-listed (BUI-129/BUI-131). Check by series + issue only |
| Enumerating a creator's run from memory — even for a plain question, not just a wish-list add | Memory silently drops DISCONTINUOUS stints (JR JR's 1993 Uncanny X-Men return; BUI-340's Erik Larsen Spider-Man #19–43 vs. actual #18–23). Just answering a question → `locg creator-run --creator … --series-id …` (read-only, BUI-340). Actually adding to the wish-list → `locg wish-list add --creator … --series-id …` (BUI-134) |
| Treating a `printing_conflict: true` match as owned and auto-skipping it | Step 3/Step 4 (BUI-372) — the match is a different printing, not the queried one; move it to the printing-conflict bucket and let the user decide, don't fold it into "already owned" |
| Retrying a Step 4-confirmed printing-conflict add without `force: true` | It will 409 again for the same reason Step 3 flagged it (BUI-372) — the owned-guard matches the same reprint row; pass `force: true` on that specific POST |
| Conflating same-name creators | "John Romita Jr." vs "John Romita" (Sr.) are distinct Metron ids; the resolver pins the id. Always pass the exact Metron creator name |
