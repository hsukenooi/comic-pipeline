# shipped_orders fixtures

Sanitized captures of the two real eBay mail shapes, in Gmail API
`users.messages.get?format=full` form (the exact JSON `ebay-shipped` consumes).

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
