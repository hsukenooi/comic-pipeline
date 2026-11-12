---
title: Report Non-eBay Orders Shipped to EZShip
ticket: BUI-1376
status: done
date: 2026-11-11
---

# Report Non-eBay Orders Shipped to EZShip

## Goal

A new read-only command, `ezship-bound`, finds non-eBay orders shipped to the EZShip warehouse. For each order, it proposes the fields for `ezship new`. It submits nothing.

Done when: the report lists Hart #75151 with its tracking number and proposed fields, and lists #1702 and #US-3108 as waiting for tracking.

## Design

### 1. Find Orders Shipped to EZShip

- **Gmail Query** – `newer_than:Nd ("12826" OR "Airport Way") -from:me -from:ebay.com -from:members.ebay.com -from:ezbuy.com -from:ezbuy.sg`. EZShip sends from `noreply@ezbuy.com`. `-from:me` drops replies that quote a confirmation.
- **Two Windows** – `--days` (default 45) sets the shipment window. Order confirmations and the step 3 search use `--order-days` (default 180), because presales ship months after the order. An order whose newest shipping email is older than `--days` counts as handled, and the report gives only a count of these orders.
- **Body Check** – Keep a message only if its text holds `12826` and then `Airport Way` (either `NE` or `Northeast`). "Portland, OR" alone doesn't count. Image Comics and adidas newsletters print a Portland office address.
- **Order Email** – Keep only order confirmations, which have an order number in the subject or body. The regex starts with the word Order: `Order(\s+Confirmation)?\s*#\s*([A-Z0-9-]+)`, so `#US-3108` matches and `#2` in an item name doesn't. A message with the address and no order number goes to a Skipped list in the report.
- **Shipping Email** – Set aside messages that `is_shipping_mail` flags. Step 3 matches them to orders. A shipping email with a tracking number and no matching order becomes an `orphan_shipment` row with its tracking number. It never drops out of the report.

### 2. Parse the Order

The parser reads the Shopify order template. All three test orders use it. It reads the `text/plain` part only. `decode_body` joins every MIME part, so reading all of them would count each item twice. A message with no `text/plain` part is `unparsed`.

- **Merchant** – The display name in `From`, through the existing `parse_merchant`.
- **Order Number** – `Order #X` from the subject, with the body as a fallback.
- **Items** – The `Name × qty` lines and their prices.
- **Total** – The amount on the line that is exactly `Total`, never `Subtotal` or `Total savings`.
- **Currency** – The total must be in USD. Any other currency, or none, gives `unparsed`.

Any other template gives an `unparsed` row with a reason. The parser never guesses a field.

### 3. Link the Shipping Email

- **Search** – For each order, run `newer_than:Nd from:<full sender address> "<order number>"`. A sender domain isn't enough, because every Shopify store shares `t.shopifyemail.com`.
- **Check Order** – Trust a match only if its body holds `Order #<number>` as a whole token. Gmail drops the `#`, so a bare `1702` can match a price or another store.
- **Search Failure** – If one search fails, the whole run exits 1. If Gmail truncates one search, the run exits 2. A row that can't be checked must never read as "waiting for tracking".
- **Tracking** – Read each match with the existing `extract_shipments` (carrier URL first, then the tracking label). Hart's "USPS tracking number:" line already passes.
- **No Tracking** – Report the order as `waiting_for_tracking`.
- **Split Shipment** – More than one tracking number for an order gives `hold`, with every number listed. The report can't tell which items are in which parcel, so it proposes no value. `classify` in `shipped_submit.py` holds eBay orders the same way.

### 4. Propose the EZShip Fields

| Field | Rule |
|---|---|
| Tracking, carrier | From step 3. `normalize_carrier` maps the carrier to the seven EZShip carriers. Any other carrier (for example Shopee) gives `unsupported_carrier` and no proposal. |
| Product | Item names through `product_name` in `shipped_submit.py` (200-character limit). |
| Category | A keyword rule on item names (see below). The proposal gives both name and ID, because `ezship new` takes `--category` and `--category-id` separately. No rule match means no proposal. |
| Declared value | 50% of the order total, in integer cents, rounded half up with `Decimal`. Raised to the category's `declaredMin` if lower. `declaredMin` is in cents: Books is 200 ($2), and the CLI default of 1000 is $10. |
| Remark | `<Merchant> <order number>`. |
| Ledger | `already_submitted` if the tracking number is in `submitted-orders.json`. Every other row says the ledger covers CLI submissions only. |
| Status | `ready` rows also print the full `ezship new` command. A tracked order with no category is `needs_category`. |

- **Category Rules** – A small table maps whole words to subcategory IDs. "Comic", "cvr", "batman", "book", and a `1:N ratio` phrase map to Books (1063). "Socks" maps to Socks (875), and "cable" to Cables (1013). Whole-word matching keeps "notebook" out of Books.
- **Category Conflict** – An order whose items match two categories gets no category proposal. EZShip takes one category per order. A test checks each ID against a saved copy of EZShip's category list.
- **Category List** – The saved copy is one response from `ezShipOrder.OrderPublic/GetDeclaredCategories` (POST, body `{}`), read on 2026-11-11.

### 5. Output and Exit Codes

- **Formats** – A text table by default. `--json` gives the full payload.
- **Exit Codes** – Same as `ebay-shipped`: 0 ok, 1 Gmail or ledger failure, 2 truncated window, 3 some orders unparsed.

## Files

- `apps/ebay/src/ezship_bound.py` – The new module. It imports the Gmail and extraction helpers from `shipped_orders.py`.
- `apps/ebay/pyproject.toml` – Adds the `ezship-bound` script. Adds `src/ezship_categories.json` to the wheel `include`, or the installed CLI can't find it (BUI-332).
- `apps/ebay/src/ezship_categories.json` – The saved category list.
- `apps/ebay/tests/test_ezship_bound.py` and `tests/fixtures/ezship_bound/` – Tests with the three real emails. The fixtures drop the billing address, card digits, and order-page keys.

## Tests

1. Hart order plus its shipping email gives USPS 92001902673388000127757663, Cables (1013), $54.50, and the remark "Hart Audio Cables 75151".
2. found / objects #1702 gives Books (1063), $34.44, and `waiting_for_tracking`.
3. Near Earth #US-3108 gives Socks (875), $25.50, and `waiting_for_tracking`.
4. The report skips an Image Comics newsletter with "Portland, OR".
5. The report lists a non-Shopify order email as `unparsed`.
6. An order with two tracking numbers gives `hold`.
7. A shipping email from another Shopify store with the same order digits doesn't link.
8. A shipping email with no matching confirmation gives `orphan_shipment`.
9. An order with a sock and a comic gets no category.
10. A total in SGD gives `unparsed`.
11. The report lists a tracking number in the ledger as `already_submitted`.

## Verification

1. Run `scripts/gates.sh`.
2. Run `ezship-bound --days 90` from source against live Gmail.
3. Check the output against the Done when line. The report lists Hart as ready to submit, because it isn't in the CLI ledger. The plan expects this (see Out of Scope).

## Out of Scope

- **Auto-Submit** – A later ticket, after the report proves accurate.
- **Website Submissions** – The CLI ledger misses orders submitted on the EZShip website (Hart is one). Reading EZShip's own order list fixes this. That is a follow-up ticket.
- **Other Templates** – Amazon and other non-Shopify formats wait for a real example.
- **Category Minimum on Submit** – `submitNewOrder` in `apps/ezship/src/api.ts` always sends the Books `declaredMin` (200). The auto-submit ticket must send each category's own minimum.
- **`ezship categories` Command** – Deferred. The category list rarely changes, and the saved copy is enough for a report.
