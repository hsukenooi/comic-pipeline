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
- LOCG comic ID (optional but strongly preferred — see "Resolve LOCG ID" below)

## Max Bid Formula (if not already computed)

If the user hasn't set max bids, default to:

> `max_bid = 80% × top of FMV range`

Round to a clean number (e.g., $136 → $135). User can override per comic.

## Resolve LOCG ID (run before adding)

The bid is most useful when it carries the canonical LOCG comic ID — otherwise `/comic:collection-add` has to look it up after the auction wins. Resolve it once, here, while we still have full context.

For each auction, resolve a `locg_id` (and `locg_variant_id` if a specific variant applies):

1. **Reuse from `/comic:collection-check`**: if that step ran earlier and carried a LOCG ID for this comic, use it directly. No second lookup needed.
2. **`locg lookup` for fresh resolutions**: batch every still-unresolved auction in a single call. Series names with internal colons (e.g. `"Batman: The Long Halloween:9"`) parse correctly; variants pass as the trailing token.
   ```bash
   cd ~/Projects/locg-cli && PYTHONPATH=src python3 -m locg lookup \
     "Uncanny X-Men:185" \
     "Amazing Spider-Man:265:Newsstand" \
     --no-collection --pretty 2>/dev/null
   ```
   Each row returns `locg_id`, `locg_variant_id`, `series_id`, and `from_cache`. The on-disk cache means repeat resolutions of the same comic are essentially free across runs.

**Variant disambiguation** (handled by `lookup`, but verify):
- If the eBay listing clearly indicates a variant (`Newsstand`, `Cover H`, `Direct Edition`, etc.), pass it as the trailing token of the spec. `lookup` returns a non-null `locg_variant_id` only when LOCG has a distinct entry for that variant; otherwise the canonical `locg_id` covers it.
- If multiple plausible candidates remain ambiguous (rare), ask the user once before adding.

If `lookup` returns an `error` for a row, proceed without `--locg-id` for that auction — `/comic:collection-add` will fall back to series+issue lookup at win time.

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

If `GIXEN_SERVER_URL` is set in the environment, pass the comic identifier so the bid links to the existing `comics` row written by `/comic:fmv`. Include `--locg-id` (and `--locg-variant-id` if a variant applies) so the won snipe carries its LOCG ID without a second lookup at `/comic:collection-add` time:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid} \
  --comic "{title}" --issue "{issue}" --year {year} --grade {grade_numeric} \
  --locg-id {locg_id} [--locg-variant-id {locg_variant_id}]
```

Do not re-pass FMV fields (`--fmv-low`, `--fmv-high`, etc.) — those are already in the `comics` table from the FMV step and the upsert will preserve them.

Omit `--locg-id` only if LOCG resolution failed entirely (covered above). The bid will still register; `/comic:collection-add` will fall back to lookup at win time.

If `GIXEN_SERVER_URL` is not set (direct Gixen mode):

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid}
```


Use the grade numeric value only (e.g. `9.2`, not `"NM 9.2"`). If the grade is unknown, omit `--grade`.

Omit `--locg-id` if the LOCG ID was not resolved for this comic. Include `--locg-variant-id {locg_variant_id}` only if a separate variant ID was found.

After all adds, verify:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py list
```

## Output

```
| # | Comic | Item ID | Max Bid | LOCG ID | Status |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | 123456789 | $800 | 6977652 | ✅ Added |
| 2 | Invincible #1 | 987654321 | $256 | 4242 | ✅ Added |
| 3 | Batman #608 | 555555555 | — | — | ⏭️ Skipped (BIN) |
```

Show `—` in the LOCG ID column if resolution failed; show the variant ID after a slash (e.g. `6977652 / 6977699`) if `locg_variant_id` differs from `locg_id`.

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
| Passing --locg-id 0 or null | Omit the flag entirely if the ID was not resolved |
