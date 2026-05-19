---
name: comic:collection-check
description: Check if identified comics are already in your League of Comic Geeks (LOCG) collection. Use when deciding whether to buy a comic to avoid duplicates.
---

# Comic Collection Check

Check whether identified comics are already in the user's collection AND resolve their canonical LOCG IDs in one batch call. Typically run after `/comic:identify`.

## Input

A list of identified comics (series + issue, optionally variant). Either from the `/comic:identify` output table or provided directly by the user.

## Reuse via Gixen first (when item_ids known)

If the input rows include eBay item IDs (i.e., these comics already correspond to active Gixen snipes — common when called from `/comic:buy`), avoid duplicate work and prime the cache for downstream skills.

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py list --json 2>/dev/null
```

Each snipe has a `comics` array shaped like:
```json
"comics": [
  {"comic_id": 33, "title": "...", "issue": "1", "year": 1993,
   "locg_id": 1931243, "locg_variant_id": null, "is_primary": true},
  ...
]
```

For every input row, look up the matching `(item_id, issue)` in that array. If `locg_id` is already populated, **reuse it** and drop the row from the lookup batch below. This is the compounding payoff — a previous run of `/comic:collection-add` or `/comic:snipe-add` already paid the resolution cost.

If the input does NOT include item IDs (pure pre-snipe identification, no Gixen state yet), skip this section.

## Lookup + collection check (single batch)

Run `locg lookup` for every remaining (un-Gixen-cached) row in one call. It groups by series, resolves each canonical series_id once, then uses a title-filtered query per issue — the small response sidesteps the 140-issue page limit on plain `series` fetches. It also fetches your collection once and reports `in_collection` per row.

```bash
cd ~/Projects/locg-cli && PYTHONPATH=src python3 -m locg lookup \
  "Uncanny X-Men:185" \
  "Batman:224" \
  "Amazing Spider-Man:142" \
  "Uncanny X-Men:179:Newsstand" \
  --pretty 2>/dev/null
```

Spec format: `"Series:Issue[:Variant]"`. Series names with internal colons (e.g. `"Batman: The Long Halloween:9"`) parse correctly because the trailing token is treated as a variant only when the second-to-last token looks like an issue number.

Output per row:
```json
{
  "series_name": "Uncanny X-Men", "issue_number": "185", "variant": null,
  "series_id": 108806,
  "locg_id": 1081721,
  "locg_variant_id": null,
  "issue_name": "Uncanny X-Men #185",
  "in_collection": false,
  "from_cache": false
}
```

Resolved IDs are cached on disk at `~/.cache/locg/ids.json`, so repeat lookups (same comic, future runs) are essentially free. Pass `--no-cache` to bypass; pass `--no-collection` if you only need IDs and don't care about collection state.

## Match nuances

`locg lookup` already handles:

- Case-insensitive series name match
- Leading "The " prefix (`"The Amazing Spider-Man"` ≡ `"Amazing Spider-Man"`)
- Picking the canonical run when many series share a name (e.g. `"Batman"` → DC 1940 run, not a recent one-shot)

What it does NOT handle and you may need to flag:

- "Uncanny X-Men" vs "X-Men" for pre-#142 issues (LOCG splits these into separate series; same run though, so a duplicate check on issue alone may miss)
- Newsstand vs Direct edition: when a listing is specifically for a newsstand variant but LOCG has only a single canonical entry, `locg_variant_id` will be null and `in_collection` reflects the canonical issue. Surface this so the user can decide whether to keep or skip.

## Output

Present to the user:

```
| # | Comic | In Collection? | LOCG ID | Notes |
|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | ❌ No | 6977652 | |
| 2 | Invincible #1 | ✅ Yes — already own | 4242 | |
| 3 | Uncanny X-Men #179 (Newsstand) | ✅ Yes — canonical in collection | 7480697 | ⚠️ may be Direct ed. |
| 4 | Batman #608 | ❌ No | ⚠️ not found | |
```

For each row carry forward:
- `locg_id` — canonical issue-level LOCG ID
- `locg_variant_id` — set only when a distinct variant entry exists in LOCG; otherwise null
- `in_collection` — boolean

If `locg lookup` returned an `error` for a row, leave the LOCG ID blank and continue. Downstream skills will fall back to a fresh lookup at win time.

## Persist back to Gixen (after fresh lookups, when item_ids known)

For every comic that needed a fresh resolution (i.e. wasn't reused from Gixen above and didn't error out), write the resolved ID back so the next run gets it for free:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py locg link <item_id> <locg_id> [--issue N] [--variant-id V] 2>/dev/null
```

- For **single-issue snipes**: omit `--issue`.
- For **lots**: pass `--issue N` per comic. The endpoint auto-creates a comic row + junction entry if the parser hadn't expanded the lot to that issue yet.
- Pass `--variant-id` only when a distinct variant was disambiguated.
- On non-zero exit, log it but do NOT abort the check — write-back is best-effort.

(The on-disk `locg lookup` cache already covers cross-session ID stability. Gixen write-back additionally links the ID to a specific eBay item, which is what `/comic:collection-add` consumes after a win.)

## Decision Gate

Ask the user how to handle duplicates:
- **Skip** comics already owned (most common)
- **Continue anyway** (they may want a second copy in better condition)
- **Newsstand-only-different cases**: surface separately so the user can pick

Remove skipped comics from the working list before passing to `/comic:fmv`. Carry the LOCG IDs on every surviving row through the rest of the pipeline.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Fetching the full `locg collection` to check 5 specific titles | Use `locg lookup` instead — it returns `in_collection` per row and only fetches the collection once |
| Running `locg search` + `locg series` per comic to resolve IDs | Use `locg lookup` — one call resolves all of them |
| Hitting the 140-issue page limit on `locg series` and missing older issues | `locg lookup` uses a title-filtered query per issue, so the page size doesn't apply |
| Treating #142+ "Uncanny X-Men" as different from "X-Men" | They're the same run after #141; flag for user if disambiguation matters |
| Assuming "I already own it" = skip | Ask — condition upgrades and Newsstand-vs-Direct are legitimate reasons to keep |
| Passing `locg_id=null` or `0` downstream | Omit the field entirely if unresolved |
