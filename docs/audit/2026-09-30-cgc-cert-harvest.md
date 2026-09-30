# CGC Cert Harvest (BUI-1016): Stopped on CGC's Terms of Use

**Verdict:** No lookups were made. CGC's terms forbid automated access, so the harvest stopped before any CGC contact. The Wave 3 gate (300 noted certs with photos) is not evaluated: it fails by policy, not by data.

## Terms of Use Clause

Source: `https://www.cgccomics.com/legal/terms-of-use/` (Certified Collectibles Group, LLC), section 2, "Additionally, you agree not to":

- **Automated access** – "Use any robot, spider, or other automatic device, process, or means to access the Website for any purpose, including monitoring or copying any of the material on the Website."
- **Manual copying** – "Use any manual process to monitor or copy any of the material on the Website or for any other unauthorized purpose without our prior written consent."
- **Load** – "Use the Website in any manner that could disable, overburden, damage, or impair the site."

Firecrawl is an automatic process, so paced lookups do not cure this. The clause has no carve-out for public cert pages. The observed "exceeded our limits" banner on 2026-09-29 shows CGC enforces it technically as well.

## Counts (no CGC contact, no Browse calls)

- **Slab comps** – 1,010 rows (1,006 CGC, 4 CBCS), 998 distinct listings, 0 undated.
- **Inside the 90-day photo window** (sold on or after 2026-07-02) – 805 rows, 799 listings.
- **Certs found in ledger titles** – 53 distinct 10-digit certs (50 with a "cert"/"CGC"/"#" keyword within 25 characters).
- **Certs found in the 50 holdout slabs' listing text** – 15 distinct (8 with keyword context); the EM counted 14, so at most one is a false positive. Not reviewed by hand.
- **Item specifics and label OCR** – not run. Browse was not called, and the holdout `item.json` files carry no `localizedAspects`.
- **Resolved, noted, granularity, grade and title mismatches** – not measured (0 lookups).

Cert text hits are about 5% of the ledger's slab titles, so even with consent, title text alone would not reach 300 certs. Reaching the gate would need label OCR on roughly 800 in-window listings (up to 800 Browse calls, within the 1,000 cap).

## Rate Limit

Not measured today. From 2026-09-29: the lookup page showed "Your search activity has exceeded our limits" after a handful of lookups. The banner also renders above pages that still carry data, so the parser treats a block as "no Title field", never "banner present". The parser was checked offline on the four saved 2026-09-29 pages (three with data, one "cannot be found"): it reads grade, issue, and notes, and returns nothing for the not-found page.

## Options

- **Ask CGC for written consent (recommended)** – Pros: legal, and an API or data license may also cover the grader-note labels. Cons: slow, may be refused.
- **Harvest anyway** – Pros: fast. Cons: breaches the terms and the standing rate limit blocks it after a few lookups.
- **Drop CGC notes as labels** – Pros: no legal exposure. Cons: loses the only free defect labels.

## Caveats

- `lookup` in the script refuses unless `CGC_WRITTEN_CONSENT=1` is set. It is untested against live CGC.
- Holdout listing text is on disk only for the 50 slabs; ledger slabs have titles only.
- Fixture at `~/comic-grader-fixtures/cgc-certs/extracted.json` holds the extracted certs. No index yet.
