---
name: comic:snipe-show
description: Show active and recently ended Gixen snipes in formatted tables with eBay links and seller info. Use when the user wants to see their current snipes or auction status.
---

# Comic Snipe Show

Display active and recently ended Gixen snipes as formatted tables with eBay links and seller info.

**Gixen CLI:** `gixen` (a uv-installed console script on PATH; run `./scripts/install.sh` if not found).

## Fetch Data

```bash
gixen list --json 2>/dev/null
```

## Build Tables

Split results into two groups based on `time_to_end`:
- **Active** — `time_to_end` is not `"ENDED"`
- **Recently Ended** — `time_to_end` is `"ENDED"`

### Table Formatting Rules

To prevent markdown table rendering issues in the terminal:

- **ID column**: Use only the last 6 digits as link text: `[…361459](https://www.ebay.com/itm/306877361459)`
- **Title column**: Truncate to 35 characters max (add `…` if cut)
- **Seller column**: Display as plain text, no link

### Active Table

Sort by time left ascending (soonest ending first).

Flag any row where `current_bid >= max_bid` with ⚠️ on the Max Bid cell — the snipe will fire below market and won't win.

eBay item URL format: `https://www.ebay.com/itm/{item_id}`

```
**Active (N):**

| ID | Title | Current | Max Bid | Time Left | Seller |
|---|---|---|---|---|---|
| […361459](https://www.ebay.com/itm/306877361459) | Tales to Astonish #87 | $9.50 | $18.00 | 3h 3m | beatlebluecat |
| […440836](https://www.ebay.com/itm/306877440836) | Fantastic Four #35 | $66.02 | **$55.00** ⚠️ | 4h 34m | beatlebluecat |
```

### Recently Ended Table

Sort by most recently ended first. If end timestamps are unavailable from the CLI, display in the order returned and note that sort order is approximate.

**Result column** — derive from the `status` field (not bid comparison):

| `status` value | Result label |
|---|---|
| `BID UNDER ASKING PRICE` | Outbid |
| `NETWORK ERROR` | Not Bid ❌ |
| `NETWORK ERROR: EBAY BID INCREMENT RULE NOT MET` | Not Bid ❌ |
| Anything containing `WON` | Won ✅ |
| Anything else | show raw status |

If `status_mirror` differs from `status` and is not `N/A`, append it in parens: `Not Bid ❌ (mirror: EBAY BID INCREMENT RULE NOT MET)`.

```
**Recently Ended (N):**

| ID | Title | Winning | Max Bid | Result | Seller |
|---|---|---|---|---|---|
| […550531](https://www.ebay.com/itm/298217550531) | Daredevil #29 | $16.28 | $22.00 | Won ✅ | beatlebluecat |
| […294954](https://www.ebay.com/itm/298217294954) | Amazing Spider-Man #300 (NM-) | $455.00 | $440.00 | Outbid | beatlebluecat |
| […440836](https://www.ebay.com/itm/306877440836) | Fantastic Four #35 | $66.02 | $55.00 | Not Bid ❌ | beatlebluecat |
```

## Flags to Surface

After the tables, call out anything that needs attention:

- Any active snipe where current bid ≥ max bid (won't win — ask if user wants to raise the max or cancel)
- Any recently ended with `NETWORK ERROR` on both main and mirror (Gixen failed to bid at all — consider re-sniping if relisted, or Gixen Plus for priority queue)
- Any recently ended with `BID UNDER ASKING PRICE` where the gap between max bid and winning bid is small (might be worth raising max on future similar listings)
