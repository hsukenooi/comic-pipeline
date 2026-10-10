# BUI-1222: The comps ledger stores list price on best-offer sales

Date: 2026-10-11. Script: `2026-10-11-best-offer-list-price.py`. Analysis only, no production code.

## Verdict: proven (direction), magnitude from PriceCharting only

On best-offer sales, sold-comps.com `soldPrice` is the listing's asking price, not the accepted offer. BUI-552's close ("`soldPrice` already is the accepted amount") is wrong. The size of the overstatement (accepted is a median 0.89 of list) still rests on PriceCharting alone.

## Evidence

eBay's Trading API `GetItem` (free, user token) returns the listing's own `StartPrice` (asking price) and `BestOfferCount` for items that ended within about 90 days. It does not return the accepted amount to a third party. `GetItemTransactions` returns zero transactions for a non-party.

| Sample (seed 1222, ended 2026-07-20 or later, USD) | soldPrice equals eBay StartPrice |
|---|---|
| 25 sales with `bestOfferAccepted: true` | 25 / 25, to the cent |
| 8 straight Buy It Now controls | 8 / 8 |

- **Odd asks match exactly.** Examples: $597.08, $10.20, $2.80, $1,800. An accepted offer equal to an odd asking price to the cent, on every best-offer sale, is not credible.
- **Offers existed.** `BestOfferCount` is 1 or more on 20 of the 25 (up to 15 on ASM #129 CGC 4.0, sold at the $1,100 ask).
- **PriceCharting ties to eBay.** Batman #227 CGC 6.5 (item 287597808992): eBay `StartPrice` $1,275, `BestOfferCount` 2. The ledger and sold-comps.com both hold $1,275. PriceCharting labels $1,275 "best offer list price" and $1,087 "best offer accepted price".
- **The vendor contradicts itself.** Its docs say the asking price "(higher) was visible as a strikethrough". eBay's record shows `soldPrice` is that asking price.

## Why BUI-552 missed it

Its two tests could not separate list from accepted. The `includeCompleteListing` toggle moved only the badge. The SerpApi overlap (65/67 matches) showed both providers read the same rendered number, which is the strikethrough ask. The round-number signature (66.9% vs 50.2%) fits sellers who price round and accept offers, not negotiated amounts.

## Limits

- No record in reach gives the accepted amount itself. The user's own eBay WonList (60 days) holds 38 auction wins and no best-offer buys. Gmail offer receipts were not read.
- The sample spans raw and slab, $2.80 to $1,800. It is random within the capture, not stratified by pool.

## Implication

About 19% of captured sales (4,499 of 21,640 unique items) carry the badge. Each is priced at list, so pools that include them run high on those rows. PriceCharting's median puts that at about 11% per row.
