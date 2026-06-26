---
name: comic:snipe-show
description: Show active and recently ended Gixen snipes in formatted tables with eBay links and seller info. Use when the user wants to see their current snipes or auction status.
---

# Comic Snipe Show

Display active and recently ended Gixen snipes as formatted tables with eBay links and seller info.

**Gixen CLI:** `gixen` (a uv-installed console script on PATH; run `./scripts/install.sh` if not found).

## Fetch Data

Per the shared comics-server convention (BUI-172,
`docs/conventions/comics-server-call.md`), **a failed fetch must hard-fail
loudly — never render an empty/degraded result from a failed call.** Do **not**
blanket-`2>/dev/null` the fetch (BUI-151): in server/thin-client mode a
server-down error goes to stderr with a non-zero exit and **no** stdout, so
swallowing it and parsing the empty output renders "Active (0) / Recently Ended
(0)" — falsely telling the user they have no snipes when the server is simply
unreachable.

```bash
SNIPES_JSON="$(gixen list --json)" || {
  echo "gixen list failed — the comics server (or Gixen in direct mode) is unreachable or errored. NOT rendering empty tables." >&2
  exit 1
}
```

**If the command exits non-zero or produces no parseable JSON, STOP and report
the error** — don't render empty tables. A genuine "no snipes" result is the
JSON array `[]` with exit 0; only that renders the zero-snipes case.

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

**Result column** — derive from the `status` field (not bid comparison).

`gixen list --json` returns **two different `status` contracts** depending on mode (BUI-150), so the table must cover both:
- **Server / thin-client mode** (`COMICS_SERVER_URL` set — the production MacBook + Mac Mini setup): the server returns the **internal mapped** status, one of `WON` / `LOST` / `FAILED` / `ENDED`. The raw Gixen failure reason survives in `status_mirror`.
- **Direct mode** (no server): the raw Gixen string (`BID UNDER ASKING PRICE`, `NETWORK ERROR`, …).

| `status` value | Result label |
|---|---|
| `WON`, or anything containing `WON` | Won ✅ |
| `LOST`, `BID UNDER ASKING PRICE`, `OUTBID` | Outbid |
| `FAILED`, `NETWORK ERROR`, `NETWORK ERROR: …` | Not Bid ❌ |
| `ENDED` | Ended (outcome unresolved — the eBay fallback may still refine it to Won/Outbid) |
| Anything else | show raw status |

If `status_mirror` differs from `status` and is not `N/A`, append it in parens — in server mode this is where the `FAILED` reason lives: `Not Bid ❌ (mirror: EBAY BID INCREMENT RULE NOT MET)`.

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
- Any recently ended that resolved to **Not Bid ❌** (`FAILED` in server mode, or `NETWORK ERROR` in direct mode — check `status_mirror` for the reason): Gixen failed to bid at all — consider re-sniping if relisted, or Gixen Plus for priority queue
- Any recently ended that resolved to **Outbid** (`LOST` in server mode, or `BID UNDER ASKING PRICE` in direct mode) where the gap between max bid and winning bid is small (might be worth raising max on future similar listings)
