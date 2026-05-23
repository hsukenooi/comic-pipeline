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

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid} \
  --comic "{title}" --issue "{issue}" --year {year} --grade {grade_numeric}
```

Use the grade numeric value only (e.g. `9.2`, not `"NM 9.2"`). If the grade is unknown, omit `--grade`.

If `GIXEN_SERVER_URL` is not set (direct Gixen mode):

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid}
```

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
