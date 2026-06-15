---
name: comic:identify
description: Identify comics from eBay listing URLs. Extracts series, issue, grade, variant, and listing type (auction vs Buy It Now). Use when the user provides eBay listing URLs and needs them identified before pricing or bidding.
---

# Comic Identify

Take eBay listing URLs and turn them into a structured table of comic identifications.

## Step 1: Fetch Listings

Extract item IDs from URLs (the number after `/itm/`) or accept raw IDs directly.

Run all items in a single call:

```bash
cd ~/Projects/comic-pipeline/apps/ebay && python src/ebay_fetch.py --json <id1> <id2> <id3>
```

Also accepts full URLs:

```bash
cd ~/Projects/comic-pipeline/apps/ebay && python src/ebay_fetch.py --json https://www.ebay.com/itm/298217294954 https://www.ebay.com/itm/318141695576
```

If the venv isn't set up yet:
```bash
cd ~/Projects/comic-pipeline/apps/ebay && pip install -e . -q
```

## Step 2: Parse the JSON

**Reconcile the array against your input first (BUI-166):** the array has one
object per *successfully fetched* item — an item that 404s, hits a non-200, or
exhausts its 429 retries is silently dropped (the error goes to **stderr**, exit
stays 0). So always compare the returned object count to the number of IDs you
passed, and for any input with no corresponding row, surface it as a fetch
failure (quote the stderr line) rather than omitting it without comment. If the
array is **empty**, treat it as a hard fetch failure — show the stderr and stop;
do **not** present an empty identification table as a valid "nothing found."

The response is a JSON array. Each object contains:

| Field | What to use it for |
|---|---|
| `item_id` | Row identifier |
| `title` | Full listing title |
| `listing_type` | `"Auction"` or `"BIN"` |
| `current_price` | Current bid (auctions) or buy price (BIN) |
| `end_date` | When the auction ends |
| `grade` | Parsed condition — `(NM-)`, `(VF)`, etc. Null if not found |
| `grade_source` | `"item_specifics"`, `"title"`, or `"missing"` |
| `grade_from_description` | A grade the script found **only** in the body description (not the title/specifics). Populated when `grade_source` is `"missing"` but the description still states a grade — surface it as a weak, description-sourced grade. Null when no description grade exists. |
| `variant` | Newsstand, Direct, Whitman, etc. Null if not found |
| `item_specifics` | Full key-value pairs from the listing |
| `seller` | eBay seller **username** (not the store display name). Carry it forward — it's the key for `/comic:buy`'s seller-reliability advisory and is stored on the snipe. |

Flag items where `grade_source` is `"missing"` — but check the title first. If the grade appears explicitly in the title (e.g. "NM", "VF+", "FVF", "Fine+"), use it as the stated grade with a light note. **Then check `grade_from_description` (BUI-148): if it is non-null, the script found a grade in the listing body — surface that as a weak, description-sourced grade with a light note, not "no grade."** Reserve a strong ⚠️ only for listings with no grade signal *anywhere* — i.e. `grade` is null **and** `grade_from_description` is null.

## Output

Present to user for confirmation:

```
| # | Comic | Issue | Grade | Variant | Type | Seller | Ends | Notes |
|---|---|---|---|---|---|---|---|---|
| [1](https://www.ebay.com/itm/298217294954) | Amazing Spider-Man | #300 | NM- | — | Auction | beatlebluecat | 2d | — |
| [2](https://www.ebay.com/itm/318141695576) | Amazing Spider-Man | #300 | — | Newsstand | Auction | comicsRus | 47m | ⚠️ Grade not stated |
| [3](https://www.ebay.com/itm/555555555) | Batman | #608 | VF | — | BIN | someseller | — | ⚠️ Buy It Now |
```

- The `#` column links directly to the eBay listing (`https://www.ebay.com/itm/{item_id}`). No separate Item ID column.
- **Ends** shows time remaining, not the end date: `<60 min → "47m"`, `<24h → "18h"`, `≥1 day → "2d"`. Compute from current UTC time vs `end_date_iso`. Mark with ⚠️ in the Ends cell if under 24h.

- Flag any listing where `grade_source` is `"missing"` **and** `grade_from_description` is null (a true no-grade listing); if `grade_from_description` is present, treat it as a weak description-sourced grade instead of a strong warning
- Flag Buy It Now listings — they're skipped at the Gixen step
- Carry the `seller` username and the stated `grade` forward — `/comic:buy` uses
  the seller for its reliability advisory (Step 1) and stores both the seller
  grade and (if graded) the photo grade on the snipe.
- Ask user to confirm identifications are correct

Series and issue are not returned directly by the API — derive them from the `title` field.

This table is the input for `/comic:collection-check` and `/comic:fmv`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Using firecrawl browser on eBay | Use `ebay_fetch.py` — it calls the Browse API directly, no bot detection |
| Assuming grade when `grade_source` is `"missing"` | Flag it — don't guess. The downstream FMV step handles unknowns explicitly. |
| Missing variants | Check both `variant` field and `item_specifics` for "newsstand", "direct", "whitman", "price variant" |
| Treating `condition` field as grade | `condition` is eBay's generic label (e.g. "Like New"). Use `grade` which is parsed from the actual listing content. |
