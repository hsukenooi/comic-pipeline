---
name: comic:seller-scan
description: Scan an eBay seller's active listings and surface any that match your LOCG wish list. Use when you want to check if a specific seller has comics you're looking for.
---

# Comic Seller Scan

Fetch all active listings from an eBay seller and fuzzy-match them against your LOCG wish list. Outputs a match table you can feed directly into `/comic:buy`.

## Input

A store name, a username URL, or a raw login username. **An eBay store name is
not the same as the seller's login username** — the Browse API only filters by
login username, so store names are resolved through an alias map committed in
the repo at `apps/ebay/src/seller_aliases.json` (BUI-68). It ships with the
code, so there's nothing to set up per machine; `--add-alias` edits this
tracked file, so commit it after adding a new seller.

- `beatlebluecat` — bare name; resolved via the alias map (seeded names map to themselves)
- `https://www.ebay.com/usr/<username>` — trusted login username
- `https://www.ebay.com/sch/i.html?_ssn=<username>` — the `_ssn` value is the login username
- `tunerscomics --username tuners_comics_2011` — pass a known username directly (one-off)
- `tunerscomics --add-alias tuners_comics_2011` — register the username, then scan

**Finding a seller's username:** open one of their listings, click **"See other
items"**, and copy the `_ssn=` value from the resulting URL — that's the login
username the filter needs. (The public `/str/<slug>` store URL is *not*
guaranteed to be the username.)

If you scan an unknown store name, the run aborts with these instructions rather
than silently returning every seller's listings.

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
| `unknown seller '<name>'` | The store name isn't in your alias map. Find the username (`_ssn=` in the seller's "See other items" URL) and re-run with `--add-alias <username>` |
| eBay rejected the seller filter | The resolved username isn't a valid eBay login username — re-check the `_ssn=` value and update the alias |
| `Dropped N listing(s) from other sellers` | Safety net fired: eBay returned foreign sellers and they were filtered out. Usually means the alias points at the wrong/stale username |
| 0 listings fetched | Seller may have no active auction listings; check their eBay page |
| False positives (wrong comic) | Check match_score — scores near 0.5 with short series names can be ambiguous |
| Wish list empty | Run `locg wish-list` to verify authentication and list contents |
| Rate limit error | Re-run after a few seconds; the Browse API allows retries |
