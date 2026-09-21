# shipped_orders fixtures

Sanitized captures of the real mail shapes `ebay-shipped` reads, in Gmail API
`users.messages.get?format=full` form (the exact JSON `ebay-shipped` consumes).
The first group is the eBay-only set from BUI-807; the second is the shapes
BUI-916 added a second source for, all of which came from senders the old
`from:ebay.com` query never looked at, or from an eBay subject class the old
`is_shipping_mail` classified `ignore`.

Everything identifying has been replaced:

- message/thread ids are synthetic (`fixture-*`), not the real Gmail ids;
- the `members.ebay.com` sender local-part is synthetic;
- tracking numbers are **invented** — the UPS ones were generated to carry a
  valid 1Z check digit so they exercise the validator, but they belong to no
  real shipment;
- the delivery address, buyer name, and buyer eBay handle are placeholders.

The HTML bodies are trimmed to the load-bearing structure (the carrier
tracking anchor, the `Shipped Via` block, the order number). The real messages
are ~98 KB of table markup; the parser only ever reads these regions.

| file | shape | what it proves |
| --- | --- | --- |
| `members_shipped_ups.json` | `eBay - <seller> @members.ebay.com` | the happy path: carrier URL → carrier + tracking, seller from the From display name |
| `members_delivered_ups.json` | same seller, later lifecycle mail | the same tracking number recurs across lifecycle mails and must dedupe to one row |
| `generic_carrier_no_tracking.json` | `eBay <ebay@ebay.com>` | the shape that carries **no** tracking number — must be reported unparsed, never guessed |
| `members_bad_check_digit.json` | `members.ebay.com`, corrupted number | a carrier URL whose number fails validation must degrade to unparsed |
| `members_shipped_usps.json` | `members.ebay.com`, USPS URL | the unverified-carrier path: a USPS tracking URL parses and validates |
| `not_a_shipping_mail.json` | saved-search blast | ordinary eBay mail is neither a row nor an unparsed warning |

## BUI-916: the labelled-field source

| file | shape | what it proves |
| --- | --- | --- |
| `generic_delivery_update_labeled.json` | `eBay <ebay@ebay.com>` "Delivery Update" | the number is the *text* of a link whose href is the authenticated order page. The markup is copied from the real message — nested spans, an inline `<style>` between the label and the digits, `mso` conditional comments — because that is exactly what defeats a naive anchor-text reader. Also the only fixture carrying `itemId`+`transactionId` with `&amp;` separators |
| `human_seller_labeled.json` | a seller typing by hand | extraction runs *before* the shipping-mail gate: the subject (`Re: Want List For …`) asserts nothing about shipping, so gating on it would lose a real number outright |
| `merchant_shipping_confirmation.json` | a non-eBay merchant | the ledger is a consolidation ledger across every merchant; this shape has no eBay order identity at all |
| `merchant_unknown_carrier.json` | a labelled `SPXSG…` number | a real tracking number for a carrier with no rule here is reported as `unknown_carrier`, never emitted unvalidated |
| `label_prose_not_a_number.json` | "all tracking numbers are forwarded at time of shipping" | prose after a tracking label is not a shipment and not even a warning — the digit floor rejects it before validation |
