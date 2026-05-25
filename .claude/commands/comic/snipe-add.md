---
name: comic:snipe-add
description: Add approved comic snipes to Gixen with a computed max bid. Use when the user has decided on auctions and max bids and wants them added to Gixen.
---

# Comic Snipe Add

Add snipes to Gixen. Typically the final step after `/comic:fmv` has produced FMV ranges and the user has approved max bids.

**Gixen CLI:** `cd ~/Projects/gixen-cli && .venv/bin/python cli.py`

If the venv doesn't exist yet:
```bash
cd ~/Projects/gixen-cli && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -q
```

## Input

A list of approved auctions, each with:
- eBay item ID
- Max bid (dollars)

## Max Bid Formula (if not already computed)

If the user hasn't set max bids, default to:

> `max_bid = 80% × top of FMV range`

Round to a clean number (e.g., $136 → $135). User can override per comic.

## Pre-flight Check

**1. Server health**

Before doing anything else, verify the server is configured and up.

Check that `GIXEN_SERVER_URL` is set:

```bash
echo "${GIXEN_SERVER_URL:-UNSET}"
```

If it is not set, **stop immediately** with: "`GIXEN_SERVER_URL` is not set. Snipes cannot be recorded in the DB. Set the variable and confirm the server is running before continuing." Do not proceed.

Verify the server is responding:

```bash
curl -sf "$GIXEN_SERVER_URL/health"
```

If this fails or returns non-200, **stop immediately** with: "The Gixen server at `$GIXEN_SERVER_URL` is not responding. Snipes cannot be recorded in the DB. Confirm the server is running before continuing." Do not proceed.

**2. Bid amounts**

Compare each auction's current bid against the proposed max bid. If current bid ≥ max bid, surface it to the user — Gixen will still register the snipe but it fires below market and won't win. Ask whether to raise the max or skip before proceeding.

## Add to Gixen

**Run sequentially** — Gixen sessions are stateful and parallel adds will fail.

### Available `add` flags (canonical)

These are the flags that exist in `gixen-cli/cli.py` today. Anything else (no `--comic`, `--issue`, or `--year`) is fictional — do not invent flags.

| Flag | Type | Purpose |
|---|---|---|
| `--offset N` | int | Seconds before end to place bid (1–15, default 6) |
| `--group N` | int | Snipe group (0=none, 1–10, default 0) |
| `--grade X.Y` | float | Numeric condition grade for post-bid FMV linking (e.g. `9.2`, not `"NM 9.2"`) |
| `--comic-id N` | int | Internal `comics.id` from gixen-overlay — preferred, used by `/comic:buy` after FMV |
| `--catalog-id N` | int | External LOCG catalog id (`locg_id`) — only when you have the LOCG id, not the internal id |

`--comic-id` and `--catalog-id` are mutually preferential: if both are given, the CLI uses `--comic-id` and warns that `--catalog-id` was ignored. Either flag triggers a `POST /api/bids/{item_id}/link-fmv` call **only when `--grade` is also present**.

### Canonical post-FMV invocation

After `/comic:fmv` (or `/comic:buy`) has produced a row with `comic_id` and a numeric `grade`:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid} \
  --comic-id {comic_id} --grade {grade_numeric}
```

This is the path that populates `bids.comic_id` / `bids.fmv_id` via the `bid_fmvs` junction (see PER-140). Do **not** pass the internal `comic_id` into `--catalog-id` — that flag is for LOCG ids and the server will look it up as `locg_id`, silently fail, and leave the bid unlinked.

### Fallback invocations

If the grade is unknown, omit `--grade` (and `--comic-id` — link-fmv only fires when both are present):

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid}
```

If `GIXEN_SERVER_URL` is not set (direct Gixen mode, no overlay DB), the same minimal form applies — linking is a no-op without the server.

After all adds, verify:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py list
```

## Output

```
| # | Comic | Item ID | Max Bid | Status |
|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | 123456789 | $800 | ✅ Added |
| 2 | Invincible #1 | 987654321 | $256 | ✅ Added |
| 3 | Batman #608 | 555555555 | — | ⏭️ Skipped (BIN) |
```

## Editing Existing Snipes

Same CLI, different subcommand:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py edit {item_id} {new_max_bid}
```

Useful when FMV analysis shows an existing bid is too low.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Running Gixen adds in parallel | Run sequentially — Gixen session is stateful |
| Attempting to snipe a BIN listing | Skip — Gixen is for auctions only |
| Max bid = FMV top | Use 80% × top — leaves margin for bidder competition |
| Odd number bids ($137.43) | Round to clean numbers — doesn't materially change outcomes |
