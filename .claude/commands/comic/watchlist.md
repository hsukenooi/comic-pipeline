---
name: comic:watchlist
description: Hand live watched eBay auctions to /comic:buy. Reads the eBay watchlist, drops auctions that already have a snipe, separates auctions ending soon as likely too late, and passes the auctions you pick to /comic:buy. Never bids.
---

# Comic Watchlist

Turn the eBay watchlist into `/comic:buy` input without copying URLs by hand. This skill reads and filters. `/comic:buy` keeps its own identify, collection-check, FMV, and snipe gates.

## Input

Optional `--min-minutes <N>` (default 30). An auction ending within N minutes goes in the "likely too late" list. Gixen has no add cutoff, but identify, grade, and FMV take minutes.

## Step 1: Read the watchlist

```bash
WATCH_JSON="$(ebay-watchlist --type auction --json)"; rc=$?
```

`ebay-watchlist` is a console script (run `./scripts/install.sh` if it is not on PATH). It returns live auctions only. Read the exit code before anything else:

- **0:** parse the JSON array. Each item has `item_id`, `title`, `listing_type`, `end_time`, `current_price`, `bid_count`, `seller`, and `url`. An empty array `[]` means the watchlist has no live auctions, and nothing else.
- **3:** stop. Tell the user to run `ebay-auth login`, then rerun this skill.
- **4 or 5:** stop and show the stderr message. Never treat a failure as an empty watchlist.

## Step 2: Drop auctions that already have a snipe

```bash
SNIPES_JSON="$(gixen list --json)" || { echo "gixen list failed; NOT showing un-deduped items." >&2; exit 1; }
```

If `gixen list` fails or returns unparseable JSON, stop and report the error. Showing un-deduped items risks a duplicate snipe. A genuine "no snipes" result is `[]` with exit 0.

Drop every watched item whose `item_id` equals the `item_id` of any row in `SNIPES_JSON`. Compare as strings. The snipes list excludes tombstoned (`REMOVED`/`PURGED`) rows, so a cancelled snipe correctly reappears here. Report how many you dropped.

## Step 3: Split by time left

Compute minutes to `end_time` from the current UTC time. Drop any item whose end time has already passed.

- **Candidates:** more than `--min-minutes` left.
- **Likely too late:** `--min-minutes` or fewer left.

## Step 4: Show the lists

Lead with the counts: watched, already sniped, candidates, likely too late. Then show each list as a table sorted by end time, soonest first, with columns `#`, Title, Ends, Current Price, Bids, Seller, and Link (`url`). Number rows continuously across both lists so the user can pick by number.

If there are no candidates and no flagged auctions, say so and stop.

## Step 5: Hand off to /comic:buy

Ask which rows to buy. Candidates are offered by default. A likely-too-late row goes forward only if the user names it.

Read `buy.md` and follow it, passing each chosen `url` as the Step 1 input (see `buy.md` Step 1). The watchlist data is an `item_id` source only. `/comic:buy` fetches each listing fresh, so never carry forward the title, price, bid count, or end time shown here (the BUI-572 staleness trap).

Identify flags non-comics. The watchlist holds novels and other items. Drop any row identify flags as a non-comic before pricing, and tell the user which rows you dropped.

## Rules

- **Never bid, snipe, or buy.** `/comic:buy` owns every gate and the user approves each snipe there.
- **A failed read is never an empty list.** Exit 3, 4, or 5 from `ebay-watchlist`, or any `gixen list` failure, stops the skill.
- **Do not skip `/comic:buy` gates** because an item came from the watchlist.
