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

**Auth:** Session cookie at `~/.config/ezship/config.json`. On a session-expiration error the CLI itself prints `Run: ezship set-cookie "<paste from DevTools>"` — use that subcommand (BUI-159); it edits the cookie field in place and writes valid JSON. Have the user paste a fresh cookie from DevTools and run:

```bash
cd ~/Projects/comic-pipeline/apps/ezship && npx tsx src/cli.ts set-cookie "<paste from DevTools>"
```

Only hand-create `config.json` if it doesn't exist yet — `set-cookie` requires an existing config.

## Input

One or more orders, each with:
- Tracking number
- Carrier (UPS, FedEx, USPS, DHL, Amazon, Ontrac, Other)
- Declared value in **cents** (optional — CLI default `1000` = $10). If the user gives a dollar amount, multiply by 100 before passing `-d` (e.g. $25 → `-d 2500`).
- Product name (optional — use comic title + issue, e.g. "Venom #3")
- Remark (optional)

Warehouse defaults to `usa` — only override if the seller is shipping from China or Taiwan.

## Defaults for Comics

| Field | Default | Notes |
|---|---|---|
| `--warehouse` | `usa` | Most eBay sellers ship from US |
| `--category` | `Books` | Correct for comics |
| `--declared-value` | omit (CLI default `1000` = $10) | Never calculate from winning bids. Only pass `-d` if the user explicitly states a value — and pass it in **cents** (multiply the dollar amount by 100). |
| `--product` | Comic title + issue | e.g. `"Amazing Spider-Man #300"` |

## Submit Orders

```bash
cd ~/Projects/comic-pipeline/apps/ezship && npx tsx src/cli.ts new \
  -t {tracking_number} \
  -c {carrier} \
  -p "{comic title and issue}" \
  --category Books
```

Omit `-d` unless the user explicitly provides a declared value. `-d` is in **cents** — multiply a dollar amount by 100 (e.g. $25 → `-d 2500`); the CLI default `1000` = $10.

Run one at a time. **Confirm success before the next (BUI-141):** mark `✅ Submitted` only if the command exits 0 *and* the printed `Response` does not carry `result: false` or an error `msg`/`message`. A non-zero exit or an error response means EZShip **rejected** the order — surface the message and mark it failed, never `✅ Submitted` (the CLI now exits non-zero on a `result: false` rejection).

**Re-running is safe (BUI-180):** order submission is deduplicated by **tracking number** — a successfully-submitted tracking number is recorded locally (`~/.config/ezship/submitted-orders.json`), so re-running `new -t {same tracking}` is a no-op that refuses to create a second real shipment (it exits with an "already submitted" error). To deliberately re-submit a tracking number, remove its entry from that ledger file.

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
| Session expired error | Run `npx tsx src/cli.ts set-cookie "<paste from DevTools>"` — don't hand-edit `config.json` |
| Non-US seller | Set `-w guangzhou`, `-w shanghai`, or `-w taiwan` as appropriate |
