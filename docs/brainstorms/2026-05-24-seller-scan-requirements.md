---
date: 2026-05-24
topic: seller-scan
---

# Seller Scan: Match eBay Seller Listings Against Wish List

## Problem Frame

Finding wish list comics requires manually browsing seller storefronts or hoping auctions surface in search. A seller scan automates this: given a seller username, fetch all their active auctions and surface any that match the user's LOCG wish list — then hand off matched listings directly into `/comic:buy`.

## Flow

```
User: seller username or store URL
  │
  ▼
Fetch active auction listings (Browse API, paginated)
  │
  ▼
Parse each listing title → (series, issue)
  │
  ▼
Fuzzy-match against wish list (series name + issue number)
  │
  ▼
Present match table → user selects rows
  │
  ▼
/comic:buy (identify → collection check → FMV → snipe)
```

## Requirements

**Input**
- R1. Accept an eBay seller username or store URL as the only required input.
- R2. Extract seller username from a store URL when provided — handles both `/str/` (store) and `/usr/` (profile) formats (e.g. `https://www.ebay.com/str/sellername` or `https://www.ebay.com/usr/sellername` → `sellername`). Also accepts a bare username with no URL.

**Listing Fetch**
- R3. Fetch all active listings for the seller via the Browse API search endpoint, paginating until all results are retrieved.
- R4. Filter to auction-type listings only at fetch time; exclude Buy It Now.

**Matching**
- R5. Parse each listing title to extract a candidate series name and issue number. Series extraction must strip character names, story descriptors, and grade tokens that the title parser appends (e.g., "Amazing Spider-Man Venom" → "Amazing Spider-Man"; "Thor 1st Beta Ray Bill" → "Thor").
- R6. Match parsed (series, issue) against wish list entries using loose fuzzy matching — prefer recall over precision. Series name normalization must handle leading "The" (e.g., parser output "X-Men" must match wish list entry "The X-Men (Vol. 1)"). Surface anything plausible; the user reviews.
- R6a. If the wish list cache is missing or contains zero `in_wish_list` entries, abort with a clear message directing the user to run `locg import` first.
- R7. Report total listings scanned and total matches found. If the match count exceeds 30, note it explicitly so the user knows the table is large before scrolling.

**Output Table**
- R8. Present matched listings as a numbered table with columns: index, parsed comic (series + issue), eBay listing title, current bid, auction end date, listing URL.

**Handoff**
- R9. After presenting the table, prompt the user to select which rows to send to `/comic:buy` (by index number or "all").
- R10. Directly invoke `/comic:buy` with the selected listing URLs (not just display them) to run the full identify → collection check → FMV → snipe workflow. The skill owns the invocation — the user does not need to copy-paste.

## Success Criteria

- Given a seller with 200+ active auctions, the skill surfaces wish list matches in < 60s (fetch + match time, assuming ≤ 5 paginated API requests at ~200 items/page).
- False negatives (real wish list matches missed) are rare enough that the user doesn't need to manually browse the seller page.
- For a typical seller with 200 listings against a 507-item wish list, the match table contains fewer than 20 rows. A larger table signals the matching precision needs tightening.
- Matched listings flow into `/comic:buy` without the user needing to copy-paste URLs.

## Scope Boundaries

- Auctions only. BIN listings are excluded.
- One seller per invocation. Multi-seller batch scanning is a non-goal for v1.
- No scheduled / periodic re-scan. On-demand only.
- The skill surfaces matches and hands off to `/comic:buy` — it does not place bids or snipes directly.

## Key Decisions

- **Loose matching over tight matching:** User has high tolerance for false positives and prefers recall. Tighter matching risks missing real wish list comics with non-standard title formatting.
- **Auctions only:** Keeps results directly actionable via `/comic:buy`'s snipe flow. BIN listings don't fit the existing bid workflow. Known tradeoff: sellers who list desirable back-issues as BIN will produce false negatives the skill cannot surface.
- **Auto-handoff to `/comic:buy`:** User selects matching rows by index; the skill pipes selected URLs into `/comic:buy` rather than stopping at the table. Avoids manual copy-paste.

## Dependencies / Assumptions

- The Browse API search endpoint supports `filter=sellers:{username}` with auction-type filtering. This is a standard Browse API capability but is not yet implemented in `apps/ebay/src/ebay_fetch.py` — it will need to be added. The new scope in `apps/ebay/src/ebay_fetch.py` is one paginated search function — `parse_item`, `get_token`, retry logic, and `print_table` are directly reusable for R8's required output columns.
- The LOCG collection cache at `~/.cache/locg/collection.json` is the authoritative wish list source (507 items with `in_wish_list: true` as of 2026-05-24). The cache is a point-in-time snapshot — items added to the LOCG wish list after the last `locg import` run will not appear. The skill should surface the cache's `last_full_import` timestamp so the user can judge freshness.
- The existing title parser in `plugins/gixen-overlay/src/gixen_overlay/title_parser.py` is available for reuse in parsing listing titles.

## Outstanding Questions

### Resolve Before Planning

- [Affects R3, R4][Needs research] Does the Browse API `filter=sellers:{username}` accept the seller's display username, or does it require a different identifier? Does auction-type filtering work via this endpoint? **This is the load-bearing technical assumption for the entire feature — validate with a live API test before planning.**

### Deferred to Planning

- [Affects R5][Technical] Confirm the specific character/story tokens that `title_parser._clean_series` leaves in the series output for eBay-style titles. The series stripping rule in R5 must be implemented to handle whatever the parser leaves behind.
- [Affects R6][Technical] What fuzzy matching strategy best satisfies the < 20-row false positive target? Note: `series_name_index` maps normalized series names to display strings only (e.g., `"the amazing spider-man" → "The Amazing Spider-Man (Vol. 1)"`); it is a normalization aid, not the match corpus. The actual wish list entries must be drawn from the `comics` array where `in_wish_list == 1`, using each entry's `series_name` and `full_title` fields.
- [Affects R3][Technical] What is the behavior when a paginated fetch fails mid-scan (e.g., page 3 of 5 returns a 5xx)? Options: abort with partial-result warning, or present whatever was fetched with a note.
- [Affects R10][Technical] Does `/comic:buy` currently accept a listing URL as a non-interactive argument, or only via interactive prompt? If interactive-only, adding a URL argument to `/comic:buy` is in-scope for this feature.

## Next Steps

`-> /ce:plan` for structured implementation planning (after resolving the Browse API filter question above)
