---
name: comic:identify
description: Identify comics from eBay listing URLs. Extracts series, issue, grade, variant, and listing type (auction vs Buy It Now). Use when the user provides eBay listing URLs and needs them identified before pricing or bidding.
---

# Comic Identify

Take eBay listing URLs and turn them into a structured table of comic identifications.

## Step 1: Dispatch the identifier subagent

Extract item IDs from URLs (the number after `/itm/`) or accept raw IDs directly. Then
dispatch the **`comic-identifier` subagent** with:

- **ITEM IDS** — the IDs (or full URLs) you extracted, space-separated
- **CURRENT UTC TIME** — current UTC time in ISO-8601 format (compute it now via
  `date -u +"%Y-%m-%dT%H:%M:%SZ"`)
- **NAME** — give the subagent a name at spawn (e.g. `comic-identifier`, BUI-366)
  so it stays addressable for follow-ups later in the run (see § Follow-ups below)

The subagent runs `ebay_fetch.py --json`, parses the JSON, and returns **only** the
formatted identification table. Raw JSON and intermediate parse steps never appear in
this context.

## Output

The subagent returns the identification table directly. Present it to the user:

```
| # | Comic | Issue | Year | Grade | Variant | Type | Current Price | Bids | Seller | Ends | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| [1](https://www.ebay.com/itm/298217294954) | Amazing Spider-Man | #300 | 1988 | NM- | — | Auction | $102.50 | 12 | beatlebluecat | 2d | — |
| [2](https://www.ebay.com/itm/318141695576) | Amazing Spider-Man | #300 | — | — | Newsstand | Auction | $5.00 | 0 | comicsRus | ⚠️ 47m | ⚠️ Grade not stated |
| [3](https://www.ebay.com/itm/555555555) | Batman | #608 | — | VF | — | BIN | $250.00 | — | someseller | — | ⚠️ Buy It Now |
```

- The `#` column links directly to the eBay listing (`https://www.ebay.com/itm/{item_id}`).
  No separate Item ID column.
- **Year** is the confidence-gated per-issue cover year (BUI-316). It's populated only
  when the title's parenthesized year and eBay's item-specifics `Publication Year`
  corroborate each other (and the listing isn't a facsimile/reprint) — otherwise `—`.
  A blank is the common, safe case. `/comic:collection-check` forwards this exact value
  as the per-issue `year` (see its EXECUTOR CONTRACT § Input for the full
  BUI-316/BUI-129 forwarding rule); a blank stays year-agnostic there too.
- **Current Price** and **Bids** (BUI-359) come straight from the fetch the subagent
  already made (`current_price` / `bid_count` in the `ebay_fetch.py` JSON — no extra
  API call). Current Price is the current bid for an auction, the buy price for a BIN;
  Bids is the auction bid count (`—` for BIN). Carry both forward — `/comic:buy`
  Steps 4–5 use them for the current-bid-vs-max pre-flight and urgency context
  instead of re-fetching or re-asking this subagent mid-flow.
- **Ends** shows time remaining, not the end date: `<60 min → "47m"`, `<24h → "18h"`,
  `≥1 day → "2d"`. Mark with ⚠️ in the Ends cell if under 24h.
- Flag Buy It Now listings — they're skipped at the Gixen step.
- Carry the `seller` username, the stated `grade`, and the **Year** forward —
  `/comic:buy` uses the seller for its reliability advisory (Step 1) and stores both
  the seller grade and (if graded) the photo grade on the snipe; the Year flows into
  `/comic:collection-check` as the per-issue cover year (BUI-316).

**Ask user to confirm identifications are correct.**

This table is the input for `/comic:collection-check` and `/comic:fmv`.

## Follow-ups: message the same agent (BUI-366)

The identifier agent keeps the full `ebay_fetch.py` JSON in its context after it
returns the table — item specifics, description text, printing/variant evidence
none of which entered the caller's context. For a follow-up question about a
listing it already fetched (e.g. "does item N's item specifics say first
printing?", "what does the description say about the variant?"), SendMessage
the **same named** agent (§ Step 1 — naming it at spawn is the precondition
that makes this addressable) rather than dispatching a fresh one — the answer
is one tool call from JSON it already holds; a fresh spawn re-fetches and
re-parses everything (in the 2026-07-16 run: 1 tool call vs 9). Current Price
and Bids never need a follow-up — they are already columns in the table
(BUI-359).

## Common Mistakes

| Mistake | Fix |
|---|---|
| Running `ebay_fetch.py` inline instead of dispatching the subagent | Dispatch `comic-identifier` — keeps raw JSON out of this context |
| Using firecrawl browser on eBay | `ebay_fetch.py` calls the Browse API directly, no bot detection |
| Assuming grade when `grade_source` is `"missing"` | The subagent flags it — don't override without evidence |
| Missing variants | The subagent checks both `variant` field and `item_specifics` |
| Treating `condition` field as grade | `condition` is eBay's generic label (e.g. "Like New"); the subagent uses the parsed `grade` field |
