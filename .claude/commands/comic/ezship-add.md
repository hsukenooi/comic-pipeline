---
name: comic:ezship-add
description: Submit won comic orders to EZShip for consolidation and forwarding. Use after winning eBay auctions when the seller has shipped and you have a tracking number.
---

# Comic EZShip Add

Submit shipment orders to EZShip. Typically run after a won eBay auction has shipped and you have a tracking number from the seller.

**EZShip CLI:** `~/Projects/comic-pipeline/apps/ezship`

**Run command:**
```bash
cd ~/Projects/comic-pipeline/apps/ezship && npx tsx src/cli.ts new -t {tracking} -c {carrier} [options]
```

**Auth:** Session cookie at `~/.config/ezship/config.json`. If you get a session expiration error, ask the user to re-extract their cookie from the browser and update the config.

## Input

One or more orders, each with:
- Tracking number
- Carrier (UPS, FedEx, USPS, DHL, Amazon, Ontrac, Other)
- Declared value in dollars (optional — default $10)
- Product name (optional — use comic title + issue, e.g. "Venom #3")
- Remark (optional)

Warehouse defaults to `usa` — only override if the seller is shipping from China or Taiwan.

## Defaults for Comics

| Field | Default | Notes |
|---|---|---|
| `--warehouse` | `usa` | Most eBay sellers ship from US |
| `--category` | `Books` | Correct for comics |
| `--declared-value` | omit (CLI default $10) | Never calculate from winning bids. Only pass `-d` if the user explicitly states a value. |
| `--product` | Comic title + issue | e.g. `"Amazing Spider-Man #300"` |

## Submit Orders

```bash
cd ~/Projects/comic-pipeline/apps/ezship && npx tsx src/cli.ts new \
  -t {tracking_number} \
  -c {carrier} \
  -p "{comic title and issue}" \
  --category Books
```

Omit `-d` unless the user explicitly provides a declared value. The CLI defaults to $10.

Run one at a time — confirm each succeeds before submitting the next.

## Output

```
| # | Comic | Tracking | Carrier | Declared | Status |
|---|---|---|---|---|---|
| 1 | Venom #3 | 1Z8X330WYN40500797 | UPS | $25 | ✅ Submitted |
| 2 | Daredevil #29 | 9400111899220851283741 | USPS | $18 | ✅ Submitted |
```

## Common Mistakes

| Mistake | Fix |
|---|---|
| Declared value as dollars | CLI takes cents — multiply by 100 |
| Wrong carrier name | Must match exactly: UPS, FedEx, USPS, DHL, Amazon, Ontrac, Other |
| Session expired error | Ask user to re-extract cookie from browser and update `~/.config/ezship/config.json` |
| Non-US seller | Set `-w guangzhou`, `-w shanghai`, or `-w taiwan` as appropriate |
