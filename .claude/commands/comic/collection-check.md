---
name: comic:collection-check
description: Check if identified comics are already in your collection via the comics server API. Use when deciding whether to buy a comic to avoid duplicates.
---

# Comic Collection Check

Check whether identified comics are already in your collection by querying the
comics server's collection API (`/api/comics/collection/*`). The server is the
single source of truth across machines (BUI-87), so both the MacBook and the Mac
Mini see the same answer.

> **Hard-fail rule (R11):** if the server is unreachable or any check call fails,
> **STOP** and tell the user — never render "Not in collection" from a failed
> call. A silent miss buys a duplicate. This is the whole point of the check.

## How to read this file (BUI-361)

This skill is split into two sections:

- **EXECUTOR CONTRACT** — everything the agent performing the check must do,
  self-contained. `/comic:buy` dispatches a sub-agent with *"Read this file and
  execute its EXECUTOR CONTRACT with this input: \<working list\>"*; the
  executor reads the whole file and follows the contract.
- **ORCHESTRATOR NOTES** — what a dispatching orchestrator reads *instead of*
  the contract: dispatch input, hard-STOP handling, the Step 4 decision gate,
  and carry-forward. The orchestrator never needs to ingest the contract body.

**Standalone invocation** (`/comic:collection-check` run directly, no
orchestrator): you are both roles — execute the EXECUTOR CONTRACT, then apply
the ORCHESTRATOR NOTES yourself (present the table and run the Step 4 decision
gate with the user).

---

## EXECUTOR CONTRACT

### Input

A list of identified comics (series + issue, optionally variant and year). Either
from the `/comic:identify` output table or provided directly by the user.

**The `/comic:identify` table's Year column is a confidence-gated per-issue cover
year (BUI-316):** it's populated only when the listing title's parenthesized year and
eBay's item-specifics `Publication Year` corroborate each other within ±1 (and the
listing isn't a facsimile/reprint). When that Year is present, **forward it as `year=`
in Step 1** — it's a trustworthy per-issue cover year, so it safely disambiguates
volumes (the whole point of BUI-316: it lets the matcher's year gate reject a match
against the wrong volume of a rebootable masthead). When the Year column is blank,
**omit `year`** — a blank means the identify step was *not* confident, so forwarding a
guessed year would risk the BUI-129 false-negative. Never fabricate a year to fill a
blank; the blank is the safe, year-agnostic default.

### Step 0: Resolve the server + bootstrap guard

Resolve and health-gate the comics server through the **shared comics-server
call convention** (BUI-172, `docs/conventions/comics-server-call.md`) — don't
hand-roll URL resolution or the health check here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # COMICS_SERVER_URL (env var, hostname fallback)
comics_health_gate     || exit 1   # the server must answer
```

**If either step fails: STOP immediately** — the collection cannot be checked,
so do not proceed to bidding. Do not report any comic as "not in collection".

> **Editor note — fenced blocks don't share shell state (BUI-375):** each
> fenced bash block below runs in its own fresh shell — a freshly-spawned
> executor invokes them as separate Bash tool calls, so `$COMICS_SERVER_URL`
> and the sourced `comics_*` functions from Step 0 do **not** carry forward.
> Every later block that touches `$COMICS_SERVER_URL` re-sources
> `comics-server.sh` and re-runs `comics_resolve_server` at its own top —
> keep that pattern on any block you add. This is the exact BUI-352 trap: an
> un-resourced block curls an empty host, and a swallowing fallback can turn
> that into a silent false "not owned" (R11).

Then read collection status:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
curl -sf "$COMICS_SERVER_URL/api/comics/collection/status"
```

**If the status call fails:** STOP immediately — same rule as above.

**If `last_full_import` is null:** Stop with:
> Collection empty on the server — run a full LOCG import (`/comic:collection-add`
> import flow) before checking.

Save `cache_age_days`, `pending_push_count`, and `oldest_pending_days` from the
response — you need them for output banners.

### Step 1: Check each comic against the server

Build one request covering the whole working list, not one call per comic —
the batch endpoint (`POST /api/comics/collection/check/batch`, BUI-204) runs
the exact same matcher the single-item `GET .../collection/check` endpoint
does per pair, so the verdicts are identical; it just collapses N round-trips
into one call and cuts the per-issue `curl` token cost.

**`year` is a per-issue cover year, not a series start year — pass it only when
you have the cover date of *this exact issue*, and NEVER forward Metron's
`year_began` / the series' first-published year (BUI-129).** The server gates
a match on `release_date.startswith(year)`, so
passing a long-running series' start year (e.g. `1963` for *Uncanny X-Men*, whose
issues actually shipped 1975–1991) filters out every owned row and returns a
false `not_in_cache` for the whole run. When all you have is the series start
year, **omit `year`** — a correct ownership verdict beats the year-gated extras.

When you *do* have the right per-issue year, it disambiguates volumes and enables
the masthead-alias fallback (BUI-46): e.g. a listing identified as "The Mighty
Thor #154" with that issue's cover year (1968) resolves to the owned catalog
entry "Thor #154". Without a year that fallback is suppressed (to avoid colliding
with same-masthead reboots like *The Mighty Thor* Vol. 3) — an acceptable trade
versus the false-negative-for-the-whole-series risk of passing the wrong year.

Build the request body as a list of `{series, issue, year?, variant?}` items —
one entry per comic in the working list, `year` present only when the Input
section's forwarding rule applies (omitted otherwise), `variant` present only
when the listing is a variant (each `year` below is the **cover year of that
specific issue** — ASM #300 shipped 1988, Uncanny X-Men #179 shipped 1984):

```bash
# Build items.json from the working list, e.g.:
#   {"items":[{"series":"Amazing Spider-Man","issue":"300","year":"1988"},
#             {"series":"Uncanny X-Men","issue":"179","year":"1984","variant":"Newsstand"}]}
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/check/batch" \
  -H 'content-type: application/json' \
  -d @items.json
```

The batch call returns one entry per input item, echoing its `series`/`issue`
so you can correlate by key (don't rely on order) — otherwise each result is
the exact same verdict shape the single-item endpoint returns (see the
fallback at the end of this step); the batch is a fan-out, not a
reimplementation:
```json
{
  "count": 2,
  "results": [
    {
      "series": "Amazing Spider-Man",
      "issue": "300",
      "match_status": "in_collection",
      "full_title_matched": "Amazing Spider-Man #300",
      "matched_series_name": "The Amazing Spider-Man (1963 - 1998)",
      "matched_release_date": "1988-05-01",
      "match_kind": "exact",
      "in_wish_list": false,
      "printing_conflict": false,
      "cache_age_days": 3
    },
    {
      "series": "Uncanny X-Men",
      "issue": "179",
      "match_status": "not_in_cache",
      "full_title_matched": null,
      "matched_series_name": null,
      "matched_release_date": null,
      "match_kind": null,
      "in_wish_list": false,
      "printing_conflict": false,
      "cache_age_days": 3
    }
  ]
}
```

`matched_series_name`/`matched_release_date`/`match_kind` are the matched row's
provenance (BUI-249): `matched_series_name` is the LOCG catalog's *decorated*
series name (carries volume + year), `matched_release_date` is that row's stored
date, and `match_kind` is `"exact"` (series key matched directly) or `"alias"`
(matched only via the cross-masthead fallback, e.g. Thor ↔ The Mighty Thor). All
three are `null` when `match_status` is `not_in_cache`. See Step 2.5 Pattern D.

`in_wish_list` (BUI-250) is always a plain boolean, present on every verdict.
`match_status: "not_in_cache"` conflates two different states — a genuinely
untracked issue, and a row that exists but is catalogued with zero owned copies
(on the wish list / pull list / read list). `in_wish_list: true` on a
`not_in_cache` result means the second case: **treat it as "already on your
wish list, not owned" in the output table, not as "untracked."** This is a
direct field read, not a heuristic — it needs no Step 2.5 disambiguation.

> **If the batch call fails (curl non-zero / connection error / non-200 —
> including the 409 the store returns when it was never imported): STOP
> the entire check.** The 409 is the same R11 refusal the single-item endpoint
> makes, lifted to the whole batch — the server is declining to answer for
> every item, not just some. Report the server error to the user and render NO
> verdicts — not even for comics whose row would otherwise have looked fine. A
> partial run invites a "not in collection" misread on comics that never got a
> real answer (R11).

**Variant flag-through (R42):** If the listing has a variant (e.g. "Newsstand")
but the batch result for its `variant=` item comes back `not_in_cache`, re-run
that row without `variant` to check the canonical entry. If the canonical
matches, record the verdict as `✅ In collection (canonical)` and add the note
`⚠️ canonical match — listing variant not disambiguated`.

**Fallback — single-comic check:** for a one-off check outside the full
working-list flow (spot-checking a single book), the single-item `GET`
endpoint still works and returns the same verdict shape shown above minus the
echoed `series`/`issue`. Use `curl -sf -G --data-urlencode` so series names
with spaces are encoded and a non-200 makes curl exit non-zero:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
curl -sf -G "$COMICS_SERVER_URL/api/comics/collection/check" \
  --data-urlencode "series=Amazing Spider-Man" \
  --data-urlencode "issue=300" \
  --data-urlencode "year=1988"
```

Same R11 rule applies here too: a failed call is a hard STOP, never a silent
"not owned".

### Step 2: Apply stale-cache verdict downgrade

**When `cache_age_days > 14` AND `match_status == "not_in_cache"`:** downgrade the
verdict from confident "Not in collection" to:

> ⚠️ Not in cache (cache N days stale — manual LOCG check recommended before bidding)

A stale import may be missing recently added comics. This prevents a snipe going
through on a comic you already own.

### Step 2.5: Disambiguate known false-match patterns (advisory only)

The matcher has documented blind spots that produce **false positives** (reports
owned when it isn't → you skip a book you wanted) and **false negatives** (reports
not-owned when you do own it → you snipe a duplicate). Before rendering the table,
scan the verdicts for the patterns below and **flag** the suspect rows.

> **This pass is advisory — it FLAGS, it never DECIDES (R11).** It must NEVER:
> - invent ownership or flip a verdict on its own — only the user resolves a flag;
> - turn a `not_in_cache` into "owned" (or vice-versa) automatically;
> - weaken the Step 1 hard-fail. If any re-query call below is unreachable / non-200,
>   that is an R11 **STOP**, not a fallback to "not owned" — abort the whole check.
> A flag changes how the row is *presented*, not what was found.

**Pattern A — Giant-Size / Annual / King-Size conflation (false positive).**
When a row returns `in_collection` AND the series is a *distinct line that shares a
masthead* with a base/annual series — `Giant-Size Fantastic Four`, `… Annual`,
`King-Size …`, `… Special` — the cache may have matched the wrong series (a
confirmed, repeating case: *Giant-Size Fantastic Four #N* falsely matching an owned
*Fantastic Four Annual #N* — different books). **Do not auto-skip it.** Flag:
> ⚠️ possible false positive — "Giant-Size/Annual" line may be conflated with the base/annual series; confirm before skipping

**Pattern B — leading-article false negative (The / A / An).**
When a row returns `not_in_cache` AND the series name does or could carry a leading
article (the cache may store `The Incredible Hulk` while `/comic:identify` emitted
`Incredible Hulk`, or vice-versa — a real incident sniped 17 owned Hulks), collect
every row that matches this pattern and re-check them **together in one follow-up
batch call** (same endpoint as Step 1) with the article toggled on each affected
row's `series` (add `The ` if absent; strip it if present), passing the same
`issue`/`year`/`variant` per row:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/check/batch" \
  -H 'content-type: application/json' \
  -d '{"items": [
    {"series": "The Incredible Hulk", "issue": "330", "year": "1987"},
    {"series": "Incredible Hulk", "issue": "181", "year": "1974"}
  ]}'
```

- A successful re-query with alternate series keys is R11-safe (it's a new call,
  not a fallback from a failed one). If this follow-up batch call itself fails /
  can't reach the server → **STOP** (R11), don't proceed.
- For each row whose toggled query returns `in_collection`, surface **both**
  results and flag — do **not** silently flip the verdict:
  > ⚠️ owned under series key "The Incredible Hulk" — identify dropped/added a leading article; confirm before bidding
- For rows still `not_in_cache`, leave the original verdict and add no flag.

**Pattern C — ambiguous / unrecognized series name (wrong-volume or silent-miss risk).**
A series name that isn't the LOCG catalog's exact spelling can yield a silent
`not_in_cache` even when owned (BUI-129/171). When a `not_in_cache` row's series
name is short/generic, could be the wrong volume, or looks like a Metron-style
name, resolve it against the catalog (BUI-449) — this returns a scalar per name,
never the full catalog array. Batch every suspect row's series name into one call:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
curl -sf -X POST "$COMICS_SERVER_URL/api/comics/collection/series-names/resolve" \
  -H 'content-type: application/json' \
  -d '{"names": ["Uncanny X-Men (Vol. 1)", "<other suspect series names>"]}'
```

The response is `{"results": [{"query", "resolved", "match_kind"}, ...]}` in
request order — `match_kind` is `"exact"`, `"fuzzy"`, or `null` ("no confident
match", `resolved` is also `null`). If `resolved` is non-null and differs from
the row's original series name, the `not_in_cache` is suspect — flag and
recommend re-checking under the resolved catalog spelling:
> ⚠️ ambiguous/unrecognized series — "Uncanny X-Men (Vol. 1)" is not the catalog spelling; did you mean "Uncanny X-Men"? Re-check under the catalog name before trusting this verdict

If `resolved` is `null`, there is no confident catalog spelling to reconcile
against — leave the verdict as-is.

**Pattern D — masthead-alias match, unconfirmed volume (false positive, BUI-249).**
This one is mechanized, not heuristic: when a row returns `in_collection` AND
`match_kind == "alias"`, the verdict only matched because the query's masthead
(e.g. "The Mighty Thor") aliases to a differently-named owned row ("Thor") —
`_MASTHEAD_ALIAS_PAIRS` has no notion of *which* volume/era, so it can land on
an owned issue from the wrong run (a real case: owning `Thor #5` Vol. 1 (1966)
falsely satisfies a "The Mighty Thor #5" Vol. 3 (2015) query). No re-query is
needed — read the volume/year straight off the response and flag:
> ⚠️ alias match — matched "{matched_series_name}" ({matched_release_date}); confirm this is the same volume as the listing before skipping

If `matched_release_date`'s year is clearly the wrong era for the listing (e.g.
a 1966 match against a 2015-era query), treat the flag as a likely false
positive; if the era is ambiguous, still flag for the user rather than guessing.
A `match_kind == "exact"` row needs no such *alias* flag — the series key
matched directly (but a no-year exact match can still be the wrong volume — see
Pattern D3).

**Pattern D2 — cross-volume ambiguity, no year given (false-positive guard, BUI-284).**
Also mechanized: when `match_status == "ambiguous_cross_volume"` (equivalently
`match_kind == "cross_volume"`), the same issue number is owned under **more than
one volume of the same masthead** (e.g. Fantastic Four #18 in both the 1961
Vol. 1 and the 2022 Vol. 7) and no `year` was supplied, so the matcher refused to
guess which volume you meant. This is NEITHER owned nor not-owned — do **not**
skip and do **not** buy on it. Read the colliding volumes off the `candidates`
list and re-check WITH the listing's per-issue cover `year`
(`?year=<YYYY>`), which resolves the collision via the release-date gate:
> ⚠️ cross-volume ambiguity — "{series} #{issue}" is owned in multiple volumes ({candidate series_names}); re-check with the listing's cover year before deciding

Never resolve an `ambiguous_cross_volume` verdict yourself by picking a volume —
supply the year and let the matcher decide.

**Pattern D3 — single-owned-wrong-volume (false positive, BUI-308 → fixed by BUI-316).**
The one case D/D2 cannot catch at the key level: when a masthead has multiple volumes
but you own the queried issue in only **one** of them, a no-`year` query returns a
confident `in_collection` with `match_kind == "exact"` — even when that single owned
volume is the *wrong* one (e.g. you own *Fantastic Four* (Vol. 7) #18 but meant
Kirby's Vol. 1 #18). Only one owned row matches, so there is no detectable ambiguity
(unlike D2), and the year gate fails open with no year. Direction is dangerous: it
reports owned when you don't own the volume you meant → a **missed purchase**.

**The real fix is upstream (BUI-316), not a manual re-check here:** when `/comic:identify`
is confident of the per-issue cover year, its Year column is populated and you forward
it as `year=` (see Input + Step 1). That per-issue year is exactly what the matcher's
year gate needs to reject the wrong volume, so this false positive never reaches the
table. **So the primary defense is simply: always forward the identify Year when it's
present.** The residual is now narrow — it only survives when the Year column is
*blank* (the identify confidence gate didn't fire, e.g. the title had no parenthesized
year to corroborate the Publication Year). For that blank-year case only, keep the old
operator vigilance for long-running rebootable mastheads (Fantastic Four,
Amazing/Uncanny X-Men, Avengers, Thor, Iron Man, Hulk, Captain America, Batman,
Superman, Wonder Woman, …): do **not** trust a no-year `in_collection`/`exact`
blindly — eyeball the **Matched Volume** column against the era/volume the listing is
for, and re-check with the listing's cover `year` (`?year=<YYYY>`) if you can source
one and they might differ.

**Pattern E — printing conflict (false positive, BUI-364).**
Mechanized, not heuristic: when a row returns `in_collection` AND
`printing_conflict: true`, the verdict was satisfied by a row whose
`full_title` names a printing the query never asked for (`2nd Printing`,
`Third Printing`, …). Printings are distinct collectibles — owning the reprint
is NOT owning the base printing (confirmed incident, 2026-07-16: *Absolute
Martian Manhunter #1* first print read as owned off the owned "2nd Printing"
row while the base printing sat wish-listed; the orchestrator skipped an
explicitly wanted $30 book). No re-query is needed — the response's
`printing_candidates` list shows every same-era printing of the issue with its
owned/wish state and a `printing_ordinal` (1 = base printing, 2+ = a
specifically-numbered reprint, `null` = a same-era row labeled with a bare
"Reprint"/"Re-Print" and no explicit number — BUI-373; match it against
the listing's printing instead of re-parsing `full_title`). Render the
conflict in the Notes column:
> ⚠️ printing conflict — matched "{full_title_matched}", a different printing than the listing; the listing's printing is {wishlisted / not owned / untracked} per printing_candidates; confirm before skipping

A `printing_candidates` row for the query's own printing with
`in_wish_list: true` and `in_collection: false` is the strongest signal the
book is explicitly wanted — say so in the note. Do **not** auto-flip the
verdict to "not owned" (R11): the reprint genuinely is owned, and only the
user decides whether the listing's printing matters. The reverse direction is
flagged the same way (a `2nd Printing` listing matched only by the owned base
row).

Carry every flag into the Notes column of the Step 3 table and surface flagged rows
separately at the Step 4 decision gate (ORCHESTRATOR NOTES). The user decides; the
disambiguator only makes the ambiguity visible.

### Step 3: Output table

```
| # | Comic | In Cache? | Full Title Matched | Matched Volume | Cache Age | Notes |
|---|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | ❌ Not in collection | — | — | 3 days | |
| 2 | Invincible #1 | ✅ In collection | Invincible #1 | Invincible (2021) | 3 days | |
| 3 | Uncanny X-Men #179 (Newsstand) | ✅ In collection (canonical) | Uncanny X-Men #179 | Uncanny X-Men (1981) | 3 days | ⚠️ canonical match — listing variant not disambiguated |
| 4 | Batman #608 | ⚠️ Not in cache | — | — | 16 days | cache stale — manual LOCG check recommended |
| 5 | The Mighty Thor #5 | ✅ In collection | Thor #5 | Thor (Vol. 1) (1966 - 1996) | 3 days | ⚠️ alias match — confirm same volume as listing |
| 6 | Hulk (Vol. 5) #9 | 📋 Wishlisted (not owned) | — | — | 3 days | |
```

**Matched Volume** is `matched_series_name` (falls back to `—` when `not_in_cache`)
— it's the decorated catalog name (carries volume + year), so a Pattern D flag is
visible right in the table without opening the raw response.

**In Cache?** has four renderings, not two (BUI-250, BUI-284): `✅ In collection`
for `match_status: "in_collection"`, `📋 Wishlisted (not owned)` for
`match_status: "not_in_cache"` with `in_wish_list: true`, `❌ Not in
collection` for `match_status: "not_in_cache"` with `in_wish_list: false`, and
`⚠️ Ambiguous (cross-volume)` for `match_status: "ambiguous_cross_volume"` — the
issue is owned under more than one volume and no year was given (Step 2.5 Pattern
D2). Row 6 is untracked at Full Title Matched / Matched Volume regardless — those
columns only ever come from an `in_collection` match.

Cache age is the same value for every row (it's a property of the import date,
not the comic).

**Status banners** (below the table):

- If `cache_age_days > 14`: `⚠️ Cache is N days old — consider re-importing from LOCG (leagueofcomicgeeks.com → My Comics → Export).`
- Pending push: `N rows pending push to LOCG; oldest pending = X days.` Escalate tone when `oldest_pending_days > 21` or `pending_push_count > 25`.

### What to return to the caller

When dispatched by an orchestrator, return the Step 3 table + status banners
(flags included in the Notes column). If you hit an R11 STOP anywhere above,
return the STOP report instead — state explicitly that **no verdicts were
produced** and why (server unreachable / failed call / never-imported 409).
Never return a partial table.

### Common Mistakes

| Mistake | Fix |
|---|---|
| Treating an unreachable server (or a failed check call) as "not in collection" | **STOP** — never render a "not owned" verdict from a failed call (R11 — see the callout at the top of this skill) |
| Passing the series start year (`year_began`) as `year` | `year` is a *per-issue cover year* gated on `release_date.startswith(year)`. Forwarding a series' first-published year (e.g. `1963` for *Uncanny X-Men*) filters out every owned issue and returns a false `not_in_cache` for the whole run (BUI-129). Pass `year` only with this issue's actual cover year; otherwise omit it |
| Auto-skipping a `Giant-Size`/`Annual` book that came back `in_collection` | Step 2.5 Pattern A — a confirmed, repeating false-positive (Giant-Size Fantastic Four vs. an owned Fantastic Four Annual); flag and let the user confirm, don't silently skip |
| Letting the disambiguator flip a verdict on its own | It's advisory — it flags ambiguity for the user, it never invents ownership or overrides the hard-fail (R11) |

---

## ORCHESTRATOR NOTES

### Dispatch input

Pass the working list — one row per comic: `series`, `issue`,
the `/comic:identify` table's **Year exactly as emitted** (a blank Year stays
blank — never backfill it with a guessed or series-start year; the executor's
contract owns the BUI-316/BUI-129 forwarding rule), and `variant` when the
listing is a variant. The executor resolves the server, runs the batch check,
applies the stale-cache downgrade and the Pattern A–E disambiguation scan, and
returns the Step 3 table + status banners.

### Executor reuse (BUI-366)

The executor stays addressable for the rest of
the run — for an incremental check (a comic added to the working list mid-run),
SendMessage the same executor the new `{series, issue, year?, variant?}` row
instead of respawning one that re-reads this contract from scratch (see buy.md
§ Sub-agent reuse).

### Hard STOP (R11)

If the executor reports it STOPPED (server unreachable,
failed/non-200 check call, or the never-imported 409), the check produced **no
verdicts** — halt the flow at this step and tell the user. Never reinterpret a
STOP as "not in collection", and never proceed to bidding without real verdicts.
A partial or missing answer is a stop, not a "not owned".

**Blast radius during incremental reuse (BUI-366):** if a SendMessage'd
follow-up row (a comic added to the working list mid-run — § Executor reuse
above) hits this STOP, it invalidates only that new row's verdict. Verdicts
already rendered from the original batch dispatch stand — they came from a
successful earlier call and don't need to be re-litigated. Halt only the
addition of the new row (tell the user its verdict couldn't be produced)
rather than discarding or re-running the table already presented.

### Step 4: Decision gate

Ask the user how to handle results:

- **Skip** comics already in collection (most common)
- **Continue anyway** (condition upgrade — they want a better copy)
- **Wishlisted-not-owned (`📋`)**: not a duplicate risk — proceed like any other `not_in_cache` comic — but worth a callout since the user has already flagged it as wanted
- **Stale-cache cases**: surface separately so the user can manually verify before bidding
- **Canonical-match rows (R42, Step 1)**: `✅ In collection (canonical)` means the listing's specific variant wasn't in cache but the canonical edition is — surface the `⚠️ canonical match — listing variant not disambiguated` note and let the user confirm the variant isn't a distinct wanted collectible before skipping
- **Disambiguator-flagged cases (Step 2.5)**: surface separately and do **not** act on the raw verdict — a Pattern-A `⚠️ possible false positive` or Pattern-E printing conflict should not be auto-skipped, and a Pattern-B/C/D flag should not be auto-bid. Let the user resolve each before the row leaves this skill.

### Carry-forward

Remove skipped comics from the working list before passing it to `/comic:fmv`.
Kept rows carry forward their identify-emitted fields plus this step's
verdict-driven flags (the R42 canonical-match note, Pattern A–E notes) so
whoever reads the row next (Step 2.5 grading, FMV) can see why it's still in
play.
