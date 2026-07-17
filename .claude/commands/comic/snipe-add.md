---
name: comic:snipe-add
description: Add approved comic snipes to Gixen with a computed max bid. Use when the user has decided on auctions and max bids and wants them added to Gixen.
---

# Comic Snipe Add

Add snipes to Gixen. Typically the final step after `/comic:fmv` has produced FMV ranges and the user has approved max bids.

**Gixen CLI:** `gixen` (a uv-installed console script on PATH).

If `gixen` isn't found, install the monorepo CLIs:
```bash
./scripts/install.sh
```

## Role in /comic:buy (BUI-360 / BUI-361)

The orchestrated buy flow does **not** dispatch this skill as a sub-agent:
`/comic:buy` Step 5 calls `gixen add-batch` inline, with this skill's pre-flight
bid sanity check and user approval gate folded into that step and the BUI-168
failure semantics enforced by the CLI itself. This file therefore has **no
EXECUTOR CONTRACT / ORCHESTRATOR NOTES split** (unlike `collection-check.md` and
`verify.md`, BUI-361) — with no orchestrated dispatch, an ORCHESTRATOR NOTES
section would have no reader. The whole file is the contract for its two
remaining callers:

- a **standalone `/comic:snipe-add`** invocation (user-approved item_ids + max
  bids, outside the buy flow), and
- an **ad-hoc dispatched executor** (e.g. "add one more snipe" after a run) —
  point it at this file; everything it must do is here.

`buy.md` references specific parts of this file (§ Bid groups, the CGC-float
grade conversion under *Available `add` flags*, the pre-flight bid sanity
check); keep those anchors stable when editing.

## Input

A list of approved auctions, each with:
- eBay item ID
- Max bid (dollars)

## Max Bid Formula (if not already computed)

If the user hasn't set max bids, default to:

> `max_bid = 80% × top of FMV range`

Round to a clean number (e.g., $136 → $135). User can override per comic.

**Note:** when the bid comes from `comic-fmv` (the `/comic:buy` path), the CLI may have already applied a **confidence haircut** below 80% — `0.70` or `0.60` × FMV high — when the photo `grade_confidence` or comp confidence was low (look for `bid_haircut=…` in the row's Notes). That lowered number is intentional; don't "correct" it back up to 80% without a reason. The 80% formula above is the fallback for manually-set bids.

## Pre-flight Check

**1. Server health**

Before doing anything else, verify the server is configured and up.

Check that `COMICS_SERVER_URL` is set:

```bash
echo "${COMICS_SERVER_URL:-UNSET}"
```

If it is not set, **stop immediately** with: "`COMICS_SERVER_URL` is not set. Snipes cannot be recorded in the DB. Set the variable and confirm the server is running before continuing." Do not proceed.

Verify the server is responding:

```bash
curl -sf "$COMICS_SERVER_URL/health"
```

If this fails or returns non-200, **stop immediately** with: "The comics server at `$COMICS_SERVER_URL` is not responding. Snipes cannot be recorded in the DB. Confirm the server is running before continuing." Do not proceed.

**2. Bid amounts**

Compare each auction's current bid against the proposed max bid. If current bid ≥ max bid, surface it to the user — Gixen will still register the snipe but it fires below market and won't win. Ask whether to raise the max or skip before proceeding.

## Add to Gixen

**Run sequentially** — Gixen sessions are stateful and parallel adds will fail.

### Handling a failed add (BUI-168)

The pre-flight health check can't catch a server that dies *between* adds. In
server mode a Gixen error or a transient outage makes `gixen add` print an error
to stderr and **exit non-zero** (the CLI `sys.exit(1)`s on a 503/connection
error). On any non-zero `gixen add`:

1. **Do not** record that item as `✅ Added` or silently continue as if it
   succeeded — mark it `❌ Failed` in the output table with the error.
2. **Re-check server health** before the next item (`curl -sf "$COMICS_SERVER_URL/health"`).
   If the server is down, STOP the batch and report which items were added and
   which remain unattempted — don't keep firing adds at a dead server.
3. At the end, **summarize added vs. failed vs. remaining** so the user knows
   exactly which snipes landed and which to retry. Never emit an all-`✅` table
   when an add failed.

### Available `add` flags (canonical)

These are the flags that exist in `packages/gixen-cli/cli.py` today. Anything else (no `--comic`, `--issue`, or `--year`) is fictional — do not invent flags.

| Flag | Type | Purpose |
|---|---|---|
| `--offset N` | int | Seconds before end to place bid (1–15, default 6) |
| `--group N` | int | Snipe group (0=none, 1–10, default 0) |
| `--grade X.Y` | float | Numeric condition grade for post-bid FMV linking (e.g. `9.2`, not `"NM 9.2"`) |
| `--comic-id N` | int | Internal `comics.id` from gixen-overlay — preferred, used by `/comic:buy` after FMV |
| `--catalog-id N` | int | External LOCG catalog id (`locg_id`) — only when you have the LOCG id, not the internal id |
| `--seller NAME` | str | eBay seller username (from `/comic:identify`). Stored lowercased on the snipe; the key for the seller-reliability advisory (BUI-78) |
| `--seller-grade X.Y` | float | Seller's *stated* grade as a CGC float (convert "VF/NM" → `9.0`). Stored for deviation analytics (BUI-78) |
| `--photo-grade X.Y` | float | Photo-assessed *consensus* grade as a CGC float — the **raw** Step 2.5 assessment, not any user override (BUI-78) |

`--comic-id` and `--catalog-id` are mutually preferential: if both are given, the CLI uses `--comic-id` and warns that `--catalog-id` was ignored. Either flag triggers a `POST /api/bids/{item_id}/link-fmv` call **only when `--grade` is also present**.

`--seller` / `--seller-grade` / `--photo-grade` are independent of FMV linking — they're written straight to the `bids` row at add time (omit any that are absent). They feed `/comic:buy`'s seller-reliability advisory; they do not affect the bid or FMV.

### Bid groups — duplicate listings of the same comic (BUI-363)

When the approved list has **2+ listings of the same comic** and the user wants
**at most one copy**, add them all with the same `--group N` (1–10) instead of
sniping only the earliest-ending copy. Gixen bid groups mean "win at most one":
per Gixen's own FAQ, *"all items with the same group number will be grouped
together and remaining bids canceled once an item in the group is won."* This
buys the win probability of every copy without dual-win risk. Per-copy max bids
may differ by grade — a group is about the *comic*, not the price.

- **Pass-through is real:** `--group` (and the `group` field on a `gixen
  add-batch` row) travels row → `snipe_group` in `POST /api/bids` → the
  `newsnipegroup` form field POSTed to gixen.com, and is stored on the local
  `bids.snipe_group` column. `gixen list` shows each snipe's group, parsed back
  from Gixen's own snipe table — confirm your adds landed grouped there.
  (The win→auto-cancel behavior itself is verified from Gixen's documentation,
  not exercised live by this repo's tests.)
- **Pick an unused N:** check `gixen list` for groups already in use by live
  snipes; reuse of a live group would merge unrelated books into one
  win-at-most-one set.
- **End-time caveat (Gixen FAQ):** don't group auctions ending **within ~2
  minutes of each other** — cancellation happens after a win, so
  near-simultaneous endings can win multiple copies. Warn the user and have
  them pick one copy in that case.
- **Retroactive grouping:** `gixen group N <item_id>...` assigns existing
  snipes to a group (0 = ungroup). It's direct-Gixen only (no server-mode
  branch) — but as of BUI-381, the sync (`refresh_snipe_group`) mirrors
  Gixen's listed `snipe_group` back onto the DB's row on every sync, in both
  directions (0→N and N→0), as long as the row is still `PENDING`. So the
  DB's `snipe_group` genuinely does catch up on the next sync — no
  server-mode add required.
- **Purge is hygiene, not the safety net (BUI-371 / BUI-381).** `gixen purge`
  detects "sibling snipes from groups with a win" and removes them from
  Gixen, tombstoning them `REMOVED` in the DB — that's the status
  `/comic:collection-add` and the results views correctly ignore (removed ≠
  lost). Running it keeps the live Gixen list and dashboard tidy, but it's no
  longer what prevents a **phantom WON** (the BUI-146 accepted-risk class:
  final price below your max on an auction you never actually bid). The
  server itself now classifies a group-cancelled sibling `REMOVED` from
  vanish-time/group-win evidence before the eBay price fallback ever sees it
  — structurally closing the window instead of depending on purge timing.
  That evidence is durable, too: BUI-381's append-only `group_wins` ledger
  records the group win at classification time and survives `mark_bids_purged`
  destroying the winner's own row, and covers a winner first seen
  already-terminal via the web-add path. Purge whenever it's convenient;
  safety doesn't ride on when (or whether) you do.

After `/comic:fmv` (or `/comic:buy`) has produced a row with `comic_id` and a numeric `grade`:

```bash
gixen add {item_id} {max_bid} \
  --comic-id {comic_id} --grade {grade_numeric} \
  --seller {seller_username} --seller-grade {seller_grade} --photo-grade {photo_grade}
```

(Omit `--seller-grade`/`--photo-grade` when not available — e.g. `--photo-grade`
only when Step 2.5 photo-graded the book. `--photo-grade` is the raw consensus,
not a value the user overrode at the gate.)

This is the path that populates `bids.comic_id` / `bids.fmv_id` via the `bid_fmvs` junction (see PER-140). Do **not** pass the internal `comic_id` into `--catalog-id` — that flag is for LOCG ids and the server will look it up as `locg_id`, silently fail, and leave the bid unlinked.

### Fallback invocations

If the grade is unknown, omit `--grade` (and `--comic-id` — link-fmv only fires when both are present):

```bash
gixen add {item_id} {max_bid}
```

If `COMICS_SERVER_URL` is not set (direct Gixen mode, no overlay DB), the same minimal form applies — linking is a no-op without the server.

After all adds, verify:

```bash
gixen list
```

## Output

```
| # | Comic | Item ID | Max Bid | Status |
|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | 123456789 | $800 | ✅ Added |
| 2 | Invincible #1 | 987654321 | $256 | ❌ Failed (server 503 — not added) |
| 3 | Batman #608 | 555555555 | — | ⏭️ Skipped (BIN) |
```

Status values: `✅ Added`, `❌ Failed (<reason> — not added)`, `⏭️ Skipped (BIN)`, and `⏸️ Not attempted` for items after a batch-halting failure. End with an added/failed/remaining count.

## Editing Existing Snipes

Same CLI, different subcommand:

```bash
gixen edit {item_id} {new_max_bid}
```

Useful when FMV analysis shows an existing bid is too low.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Running Gixen adds in parallel | Run sequentially — Gixen session is stateful |
| Attempting to snipe a BIN listing | Skip — Gixen is for auctions only |
| Max bid = FMV top | Use 80% × top — leaves margin for bidder competition |
| Odd number bids ($137.43) | Round to clean numbers — doesn't materially change outcomes |
| Sniping only the earliest-ending copy when 2+ listings of the same comic are approved | Add all copies with the same `--group N` — Gixen cancels the rest after one wins (BUI-363); skip grouping only when end times are within ~2 minutes |
| Leaving a group's cancelled siblings on Gixen after a win | No longer a safety risk (BUI-371/BUI-381 classify them `REMOVED` regardless of purge timing) — run `gixen purge` anyway to keep the live Gixen list and dashboard tidy |
