---
name: comic:collection-check
description: Check if identified comics are already in your local collection cache. Fully offline — no LOCG network access required. Use when deciding whether to buy a comic to avoid duplicates.
requires_locg_cli: ">=0.2.0"
---

# Comic Collection Check

Check whether identified comics are in the local collection cache. Fully offline — no LOCG login or network access required.

## Input

A list of identified comics (series + issue, optionally variant and year). Either from the `/comic:identify` output table or provided directly by the user.

## Step 0: Bootstrap guard

Before any other work, verify the cache is populated and `locg-cli` is current.

```bash
locg collection status --pretty
```

**If `last_full_import` is null:** Stop immediately with:
> Cache empty — run `locg collection doctor` for setup instructions.

**If `locg_cli_version` is older than `0.2.0`:** Stop immediately with:
> locg-cli version >=0.2.0 required (installed: X.Y.Z). Upgrade via `cd ~/Projects/locg-cli && pip install -e .` and retry.

Save `cache_age_days`, `pending_push_count`, and `oldest_pending_days` from the response — you need them for output banners.

## Step 1: Check each comic against the cache

For each comic in the input list, run one `locg collection check` call:

```bash
locg collection check --series "Amazing Spider-Man" --issue 300 --pretty
locg collection check --series "Uncanny X-Men" --issue 179 --variant Newsstand --pretty
```

Each call returns:
```json
{
  "match_status": "in_collection",
  "full_title_matched": "Amazing Spider-Man #300",
  "cache_age_days": 3
}
```

**Variant flag-through (R42):** If the listing has a variant (e.g., "Newsstand") but `locg collection check --variant Newsstand` returns `not_in_cache`, re-run without `--variant` to check whether the canonical entry is in the cache. If the canonical matches, record the verdict as `✅ In collection (canonical)` and add the note `⚠️ canonical match — listing variant not disambiguated`.

## Step 2: Apply stale-cache verdict downgrade

**When `cache_age_days > 14` AND `match_status == "not_in_cache"`:** downgrade the verdict from confident "Not in collection" to:

> ⚠️ Not in cache (cache N days stale — manual LOCG check recommended before bidding)

A stale cache may be missing recently added comics. This prevents a snipe going through on a comic you already own.

## Step 3: Output table

Present results to the user:

```
| # | Comic | In Cache? | Full Title Matched | Cache Age | Notes |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | ❌ Not in collection | — | 3 days | |
| 2 | Invincible #1 | ✅ In collection | Invincible #1 | 3 days | |
| 3 | Uncanny X-Men #179 (Newsstand) | ✅ In collection (canonical) | Uncanny X-Men #179 | 3 days | ⚠️ canonical match — listing variant not disambiguated |
| 4 | Batman #608 | ⚠️ Not in cache | — | 16 days | cache stale — manual LOCG check recommended |
```

Cache age is the same value for every row (it's a property of the import date, not the comic).

**Status banners** (below the table):

- If `cache_age_days > 14`: `⚠️ Cache is N days old — consider re-exporting from LOCG (leagueofcomicgeeks.com → My Comics → Export).`
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
| Running `locg lookup` to check collection status | Use `locg collection check` — fully offline, no LOCG network hit |
| Treating a stale-cache `not_in_cache` as confident "not in collection" | Apply the stale-cache downgrade when `cache_age_days > 14` |
| Fetching the full LOCG collection list to check 5 titles | Use `locg collection check` per comic — one call each, no login needed |
