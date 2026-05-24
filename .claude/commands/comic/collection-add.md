---
name: comic:collection-add
description: Record won Gixen auctions into the local collection cache, then export a CSV ready to upload to LOCG. No Playwright, no LOCG network access required.
requires_locg_cli: ">=0.2.0"
---

# Comic Collection Add

Record won Gixen auctions into the local collection cache in one batch, then export a CSV for LOCG upload. No Playwright, no live LOCG session required.

**Gixen CLI:** `cd ~/Projects/gixen-cli && .venv/bin/python cli.py`

## Step 0: Bootstrap guard

Before any other work, verify the cache is populated and `locg-cli` is current.

```bash
locg collection status --pretty
```

**If `last_full_import` is null:** Stop immediately with:
> Cache empty — run `locg collection doctor` for setup instructions.

**If `locg_cli_version` is older than `0.2.0`:** Stop immediately with:
> locg-cli version >=0.2.0 required (installed: X.Y.Z). Upgrade via `cd ~/Projects/locg-cli && pip install -e .` and retry.

## Step 1: Pull won auctions

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py list --json 2>/dev/null
```

Filter to wins:
- `time_to_end == "ENDED"`
- `status` contains `WON` (case-insensitive)

If no wins, print "No won auctions to add." and stop.

## Step 2: Build the record-win JSON

For each won snipe, build one entry in this format:

```json
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
```

`identify_data` fields:
- `series` — series name without publisher prefix (e.g. `"Amazing Spider-Man"`, not `"Marvel: Amazing Spider-Man"`)
- `issue` — issue number as a string (e.g. `"300"`, `"Annual 1"`)
- `year` — publication year as an integer; omit if unknown
- `variant_text` — variant description if the listing is explicitly a variant (e.g. `"Newsstand"`, `"Direct Edition"`); omit or `""` otherwise

**Source priority for `identify_data`:**

1. **In-session context** — if this skill is being called from `/comic:buy` and you already identified the comics in Step 1, use those identifications directly.
2. **Parse from gixen title** — extract series, issue, and year from the snipe's `title` field. For lots, build one entry per issue. If the title is ambiguous (e.g., "Marvel Silver Age Lot"), ask the user once before proceeding.

Do not leave `series` or `issue` blank — if you cannot determine them, ask the user for that specific snipe.

## Step 3: Record wins

Write the JSON to a temp file and pipe to `locg collection record-win`:

```bash
locg collection record-win --from-gixen-json /tmp/wins.json --pretty
```

Or pipe directly from stdin:

```bash
echo '<json array>' | locg collection record-win --from-gixen-json - --pretty
```

The command commits in batches of 25. On success it returns:

```json
{
  "rows_written": 3,
  "manual_variant_count": 0,
  "manual_series_count": 1,
  "metron_lookups_succeeded": 2
}
```

`manual_series_count > 0` means those rows have `needs_manual_series_canonical=true` and will appear in the export's `.notes.md` for follow-up.

## Step 4: Export to CSV

```bash
locg collection export --pretty
```

This generates a CSV at `~/Downloads/locg-bulk-import-<timestamp>.csv` plus a `.notes.md` sidecar listing any rows that need manual attention (unknown variant, unknown series canonical).

## Step 5: Report

Print a summary:

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
| Using Playwright to add comics directly to LOCG | Use `locg collection record-win` + `locg collection export` — no Playwright needed |
| Passing LOCG IDs as part of record-win input | `record-win` does not take LOCG IDs; it resolves series via Metron and the local cache |
| Leaving `series` or `issue` blank in `identify_data` | Ask the user for the specific snipe — do not guess |
