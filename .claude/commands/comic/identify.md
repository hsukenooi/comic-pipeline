---
name: comic:identify
description: Identify comics from eBay listing URLs. Extracts series, issue, grade, variant, and listing type (auction vs Buy It Now). Use when the user provides eBay listing URLs and needs them identified before pricing or bidding.
---

# Comic Identify

Take eBay listing URLs and turn them into a structured table of comic identifications.

## Step 1: Dispatch the identifier subagent

Extract item IDs from URLs (the number after `/itm/`) or accept raw IDs directly. Then
dispatch the **`comic-identifier` subagent** with:

- **ITEM IDS** — the IDs (or full URLs) you extracted, space-separated
- **CURRENT UTC TIME** — current UTC time in ISO-8601 format (compute it now via
  `date -u +"%Y-%m-%dT%H:%M:%SZ"`)

The subagent runs `ebay_fetch.py --json`, parses the JSON, and returns **only** the
formatted identification table. Raw JSON and intermediate parse steps never appear in
this context.

## Output

The subagent returns the identification table directly. Present it to the user:

```
| # | Comic | Issue | Grade | Variant | Type | Seller | Ends | Notes |
|---|---|---|---|---|---|---|---|---|
| [1](https://www.ebay.com/itm/298217294954) | Amazing Spider-Man | #300 | NM- | — | Auction | beatlebluecat | 2d | — |
| [2](https://www.ebay.com/itm/318141695576) | Amazing Spider-Man | #300 | — | Newsstand | Auction | comicsRus | ⚠️ 47m | ⚠️ Grade not stated |
| [3](https://www.ebay.com/itm/555555555) | Batman | #608 | VF | — | BIN | someseller | — | ⚠️ Buy It Now |
```

- The `#` column links directly to the eBay listing (`https://www.ebay.com/itm/{item_id}`).
  No separate Item ID column.
- **Ends** shows time remaining, not the end date: `<60 min → "47m"`, `<24h → "18h"`,
  `≥1 day → "2d"`. Mark with ⚠️ in the Ends cell if under 24h.
- Flag Buy It Now listings — they're skipped at the Gixen step.
- Carry the `seller` username and the stated `grade` forward — `/comic:buy` uses
  the seller for its reliability advisory (Step 1) and stores both the seller
  grade and (if graded) the photo grade on the snipe.

**Ask user to confirm identifications are correct.**

This table is the input for `/comic:collection-check` and `/comic:fmv`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Running `ebay_fetch.py` inline instead of dispatching the subagent | Dispatch `comic-identifier` — keeps raw JSON out of this context |
| Using firecrawl browser on eBay | `ebay_fetch.py` calls the Browse API directly, no bot detection |
| Assuming grade when `grade_source` is `"missing"` | The subagent flags it — don't override without evidence |
| Missing variants | The subagent checks both `variant` field and `item_specifics` |
| Treating `condition` field as grade | `condition` is eBay's generic label (e.g. "Like New"); the subagent uses the parsed `grade` field |
