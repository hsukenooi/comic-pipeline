---
name: comic:seller-scan
description: Scan an eBay seller's active listings and surface any that match your LOCG wish list. Use when you want to check if a specific seller has comics you're looking for.
---

# Comic Seller Scan

Fetch all active listings from an eBay seller and fuzzy-match them against your LOCG wish list. Outputs a match table you can feed directly into `/comic:buy`.

## Input

An eBay seller username or store URL:
- `beatlebluecat`
- `https://www.ebay.com/usr/beatlebluecat`
- `https://www.ebay.com/str/beatlebluecat`

## Run the scan

```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller-username-or-url>
```

If the venv doesn't exist yet:
```bash
cd ~/Projects/comic-pipeline/apps/ebay && python3 -m venv .venv && .venv/bin/pip install -e . -q
```

For JSON output (useful for piping to `/comic:buy`):
```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller> --json
```

## Output

```
Listing Title                             Wish List Item               Price      Ends          URL
--------------------------------------------------------------------------------------------------------
AMAZING SPIDER-MAN #300 NM Marvel 1988…  Amazing Spider-Man #300      $299.99    2026-05-28…   https://www.ebay.com/itm/…
FANTASTIC FOUR #48 VF+ Silver Surfer …   Fantastic Four #48           $450.00    2026-05-29…   https://www.ebay.com/itm/…
```

Progress info (listing count, match count) prints to stderr. Redirect to suppress:
```bash
seller_scan.py <seller> 2>/dev/null
```

## Feed matches into /comic:buy

Copy the eBay URLs from the URL column and pass them to `/comic:buy`. The buy workflow will identify, check your collection, calculate FMV, and add snipes.

## Matching algorithm

- Fetches up to 500 seller listings (override with `--max-results N`)
- Parses each wish list item `name` (e.g., "Amazing Spider-Man #300") into series + issue number
- Matches when: issue number appears in the listing title AND ≥50% of series name tokens match
- Reports match score — scores close to 1.0 are exact series matches; 0.5 means partial series overlap (verify manually)

## Common issues

| Issue | Fix |
|---|---|
| 0 listings fetched | Seller may have no active auction listings; check their eBay page |
| False positives (wrong comic) | Check match_score — scores near 0.5 with short series names can be ambiguous |
| Wish list empty | Run `locg wish-list` to verify authentication and list contents |
| Rate limit error | Re-run after a few seconds; the Browse API allows retries |
