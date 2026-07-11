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

## Input

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

## Step 0: Resolve the server + bootstrap guard

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

Then read collection status:

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/collection/status"
```

**If the status call fails:** STOP immediately — same rule as above.

**If `last_full_import` is null:** Stop with:
> Collection empty on the server — run a full LOCG import (`/comic:collection-add`
> import flow) before checking.

Save `cache_age_days`, `pending_push_count`, and `oldest_pending_days` from the
response — you need them for output banners.

## Step 1: Check each comic against the server

For each comic, call the check endpoint. **`year` is a per-issue cover year, not
a series start year — pass it only when you have the cover date of *this exact
issue*, and NEVER forward Metron's `year_began` / the series' first-published
year (BUI-129).** The server gates a match on `release_date.startswith(year)`, so
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

Use `curl -sf -G --data-urlencode` so series names with spaces are encoded and a
non-200 makes curl exit non-zero (each `year` below is the **cover year of that
specific issue** — ASM #300 shipped 1988, Uncanny X-Men #179 shipped 1984):

```bash
curl -sf -G "$COMICS_SERVER_URL/api/comics/collection/check" \
  --data-urlencode "series=Amazing Spider-Man" \
  --data-urlencode "issue=300" \
  --data-urlencode "year=1988"

curl -sf -G "$COMICS_SERVER_URL/api/comics/collection/check" \
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
  "matched_series_name": "The Amazing Spider-Man (1963 - 1998)",
  "matched_release_date": "1988-05-01",
  "match_kind": "exact",
  "in_wish_list": false,
  "cache_age_days": 3
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

## Step 2.5: Disambiguate known false-match patterns (advisory only)

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
`Incredible Hulk`, or vice-versa — a real incident sniped 17 owned Hulks), re-run
the check **once** with the article toggled (add `The ` if absent; strip it if
present), passing the same `issue`/`year`/`variant`:

```bash
curl -sf -G "$COMICS_SERVER_URL/api/comics/collection/check" \
  --data-urlencode "series=The Incredible Hulk" \
  --data-urlencode "issue=330" \
  --data-urlencode "year=1987"
```

- A successful re-query with an alternate series key is R11-safe (it's a new call,
  not a fallback from a failed one). If the re-query itself fails / can't reach
  the server → **STOP** (R11), don't proceed.
- If the toggled query returns `in_collection`, surface **both** results and flag —
  do **not** silently flip the verdict:
  > ⚠️ owned under series key "The Incredible Hulk" — identify dropped/added a leading article; confirm before bidding
- If it still returns `not_in_cache`, leave the original verdict and add no flag.

**Pattern C — ambiguous / unrecognized series name (wrong-volume or silent-miss risk).**
The matcher requires an *exact* normalized series-key match, so a series name that
differs from the LOCG catalog spelling (e.g. Metron's `Uncanny X-Men (Vol. 1)` vs.
the catalog's `Uncanny X-Men`) yields a silent `not_in_cache` even when owned
(BUI-129). When a `not_in_cache` row's series name is short/generic, could be the
wrong volume, or looks like a Metron-style name, fetch the cache's actual series
names and check whether the queried name is present (or close to one):

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/collection/series-names"
```

If the queried series is **absent** from that list, the `not_in_cache` is suspect —
flag and recommend re-checking under the matching catalog name:
> ⚠️ ambiguous/unrecognized series — "Uncanny X-Men (Vol. 1)" is not a cache series name; did you mean "Uncanny X-Men"? Re-check under the catalog name before trusting this verdict

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

Carry every flag into the Notes column of the Step 3 table and surface flagged rows
separately at the Step 4 decision gate. The user decides; the disambiguator only
makes the ambiguity visible.

## Step 3: Output table

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

## Step 4: Decision gate

Ask the user how to handle results:

- **Skip** comics already in collection (most common)
- **Continue anyway** (condition upgrade — they want a better copy)
- **Wishlisted-not-owned (`📋`)**: not a duplicate risk — proceed like any other `not_in_cache` comic — but worth a callout since the user has already flagged it as wanted
- **Stale-cache cases**: surface separately so the user can manually verify before bidding
- **Disambiguator-flagged cases (Step 2.5)**: surface separately and do **not** act on the raw verdict — a Pattern-A `⚠️ possible false positive` should not be auto-skipped, and a Pattern-B/C/D flag should not be auto-bid. Let the user resolve each before the row leaves this skill.

Remove skipped comics from the working list before passing to `/comic:fmv`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Treating an unreachable server (or a failed check call) as "not in collection" | **STOP** — never render a "not owned" verdict from a failed call (R11 — see the callout at the top of this skill) |
| Passing the series start year (`year_began`) as `year` | `year` is a *per-issue cover year* gated on `release_date.startswith(year)`. Forwarding a series' first-published year (e.g. `1963` for *Uncanny X-Men*) filters out every owned issue and returns a false `not_in_cache` for the whole run (BUI-129). Pass `year` only with this issue's actual cover year; otherwise omit it |
| Auto-skipping a `Giant-Size`/`Annual` book that came back `in_collection` | Step 2.5 Pattern A — a confirmed, repeating false-positive (Giant-Size Fantastic Four vs. an owned Fantastic Four Annual); flag and let the user confirm, don't silently skip |
| Letting the disambiguator flip a verdict on its own | It's advisory — it flags ambiguity for the user, it never invents ownership or overrides the hard-fail (R11) |
