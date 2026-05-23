---
title: "feat: /comic:seller-scan — Match eBay Seller Listings Against LOCG Wish List"
type: feat
status: active
date: 2026-05-24
origin: docs/brainstorms/2026-05-24-seller-scan-requirements.md
---

# feat: /comic:seller-scan

## Overview

A new skill that accepts an eBay seller username or store URL, fetches all their active auction listings via the Browse API, fuzzy-matches them against the user's LOCG wish list, surfaces matches in a numbered table, and hands selected rows directly to `/comic:buy`. End-to-end validated by prototype against `beatlebluecat` (180 listings, 3 matches, all correct, 0 false negatives, well under the <20-row success criterion).

## Requirements Trace

- **R1, R2.** `extract_seller_username()` accepts bare username, `/str/`, or `/usr/` URL formats.
- **R3.** `search_seller_listings()` paginates the Browse API search endpoint until all results retrieved.
- **R4.** Auction filter applied at API time via `filter=sellers:{username},buyingOptions:{AUCTION}` — not post-filtered (see origin: §Dependencies).
- **R5.** `parse_title()` from `title_parser.py` + `normalize_series()` strip Vol./year qualifiers. Character-name leakage from `_clean_series()` is tolerated — fuzzy threshold handles minor drift; major leakage degrades to false positive (acceptable: user reviews the table).
- **R6.** Fuzzy match: `difflib.SequenceMatcher` on normalized series + exact issue number. Threshold 0.72 confirmed by prototype.
- **R6a.** Guard: abort before any API call if wish list is empty, with message directing user to run `locg import`.
- **R7.** Report total listings scanned and total matches; flag explicitly if matches > 30.
- **R8.** Match table columns: index, parsed comic (series + issue), eBay listing title, current bid, auction end date, listing URL.
- **R9.** Prompt user to select rows by index number or "all".
- **R10.** Skill directly invokes `/comic:buy` with selected listing URLs — user never copy-pastes.

## Scope Boundaries

- Auctions only — BIN excluded at API level.
- One seller per invocation.
- On-demand only — no scheduled re-scan.
- Seller scan does not place bids or snipes directly; it hands off to `/comic:buy`.
- Does not modify `collection.json` or the LOCG wish list.

### Deferred to Separate Tasks

- Multi-seller batch scanning.
- Stripping character-name tokens from `_clean_series()` (the fuzzy threshold handles this well enough for v1 — revisit if false-positive rate proves unacceptable in practice).

## Context & Research

### Relevant Code and Patterns

- **`apps/ebay/src/ebay_fetch.py`** — `load_config()` (line 58), `get_token()` (line 90), `fetch_item()` (line 153), `parse_item()` (line 256), `print_table()` (line 353). Token cache pattern, retry/backoff on 429, marketplace header (`X-EBAY-C-MARKETPLACE-ID: EBAY_US`) are all reusable as-is.
- **`plugins/gixen-overlay/src/gixen_overlay/title_parser.py`** — `parse_title(title)` returns `ParsedTitle(series, issue, issues, grade, year, confidence)`. `_clean_series()` strips publisher words, edition tags, grade tokens, year, and `cut_markers` like `1st app`, `key`, `cover`, but does NOT strip character names. This is the correct entry point for parsing eBay listing titles.
- **`~/.cache/locg/collection.json`** — `comics` array, `in_wish_list` is int (0/1 not bool), `series_name` format is `"Series Name (Vol. N) (YYYY - YYYY)"`, `full_title` format is `"Series Name #N"`. `series_name_index` maps lowercase normalized name → display string (normalization aid only; not the match corpus).
- **`.claude/skills/buy.md`** — orchestrator skill; accepts eBay URLs as input. The seller-scan skill passes selected URLs to it directly.
- **Existing skills pattern** — YAML front matter with `name: comic:<slug>`, steps separated by `---`, shell commands in fenced blocks, no emoji in headers, "Common Mistakes" table at end.

### Institutional Learnings

- `parse_item()` expects the full Browse API item shape (`localizedAspects`, `currentBidPrice`). The search endpoint returns `itemSummaries` — a different, smaller shape. A companion `parse_item_summary()` is needed; do not try to reuse `parse_item()` for search results.
- Title-case normalization matters downstream: `upsert_comic()` uses exact-match SQL on `title`. ALL-CAPS eBay titles passed through `title_parser.py` and then to `/comic:buy`'s DB upsert path will create duplicate stub rows. The `parse_title()` output is already title-cased by the parser, so this is safe as long as we use the parser output — don't pass raw eBay titles downstream.
- The `/comic:buy` handoff must preserve issue number and series through the URL — the URL alone is sufficient because `/comic:buy` re-identifies from the URL. No need to pre-resolve LOCG IDs in the seller scan; collection-check handles that inside the buy flow.

## Key Technical Decisions

### 1. Browse API Search Endpoint and Category Filter
Use `GET /buy/browse/v1/item_summary/search` with `filter=sellers:{username},buyingOptions:{AUCTION}` and `category_ids=63` (Comic Books & Memorabilia — broadest comics umbrella on eBay US). Validated live against `beatlebluecat`, `ka-761233`, `hodagent`. (see origin: §Dependencies)

`q` is not needed when `category_ids` is present. Using `category_ids=63` rather than subcategory `259103` avoids excluding magazines, TPBs, or annuals that may be on the wish list.

### 2. `parse_item_summary()` — Companion to `parse_item()`
The Browse API search response (`itemSummaries`) is a summary shape distinct from the item detail shape. `parse_item_summary()` extracts: `legacy_item_id` (from `legacyItemId` field, not the encoded `v1|...|0` itemId), `title`, `current_bid` (`currentBidPrice.value`), `end_date` (`itemEndDate`), `listing_url` (`itemWebUrl`), `seller` (`seller.username`). Place alongside `parse_item()` in `ebay_fetch.py`.

### 3. Series Normalization for Matching
`normalize_series(s)`: lowercase → strip `(Vol. N)` → strip `(YYYY - YYYY)` and `(YYYY)` → strip leading `the ` → collapse whitespace. Applied to both parser output and wish list `series_name` before comparison. This bridges the gap between `_clean_series()` output (`"Amazing Spider-Man"`) and wish list entries (`"The Amazing Spider-Man (Vol. 1) (1962 - 1998)"`).

### 4. Fuzzy Match Strategy
`difflib.SequenceMatcher(None, norm_parsed_series, norm_wish_series).ratio() >= 0.72` AND `parsed_issue == wish_issue`. Both conditions required. Prototype confirmed ratio=1.0 for all 3 correct matches on beatlebluecat; threshold 0.72 gives comfortable headroom while filtering out unrelated titles.

Issue number extracted from wish list `full_title` using `re.search(r'#\s*(\d+)', full_title)`.

### 5. Pagination Failure Mode
On a 5xx error mid-pagination: surface whatever was fetched with a warning note ("Scan incomplete — fetched N of ~M listings before error; matches may be missing"). Do not abort silently. (see origin: §Deferred to Planning)

### 6. `/comic:buy` Invocation
The seller-scan skill invokes `/comic:buy` with the selected listing URLs as its argument — same as if the user had typed the URLs directly. No changes to `buy.md` required. (see origin: §Deferred to Planning)

### 7. Implementation Location for Main Script
Add `apps/ebay/src/seller_scan.py` as a standalone script importable by the skill. It owns: username extraction, wish list loading, title matching, and table presentation. Matching logic (`normalize_series`, `extract_issue`, `fuzzy_match_wish_list`) lives here, not in `ebay_fetch.py` — `ebay_fetch.py` stays focused on API I/O.

## Implementation Units

### Unit 1: Browse API Seller Search — `apps/ebay/src/ebay_fetch.py`

Add two functions after the existing `fetch_item()` block:

**`search_seller_listings(seller_username, token, base_url, *, category_id="63", limit=200, retries=3) → list[dict]`**

Paginates `GET /buy/browse/v1/item_summary/search` with:
- `filter=sellers:{seller_username},buyingOptions:{AUCTION}`
- `category_ids={category_id}`
- `limit={limit}`, `offset` incremented per page

On each response, extend a results list from `data.get("itemSummaries", [])`. Stop when `offset >= data["total"]` or `itemSummaries` is absent/empty. On 429: retry with `2 ** attempt` backoff (matching `fetch_item`'s pattern, line 162–183). On 5xx after all retries: return partial results with a `_fetch_error` sentinel entry so the caller can warn the user.

**`parse_item_summary(summary: dict) → dict`**

Extracts from a Browse API search result item:
- `legacy_item_id`: `summary["legacyItemId"]`
- `title`: `summary["title"]`
- `current_bid`: `summary.get("currentBidPrice", {}).get("value", "0.00")`
- `end_date`: `summary.get("itemEndDate", "")`  
- `listing_url`: `summary.get("itemWebUrl", "")`
- `seller`: `summary.get("seller", {}).get("username", "")`

Returns a flat dict. Does not call `_extract_grade()` or `extract_variant()` — those are deferred to `/comic:buy`'s identify step.

**Test scenarios:**
- Seller with 0 listings: returns empty list, no exception raised
- Seller with exactly 200 listings: exactly 1 API call, correct item count
- Seller with 201 listings: 2 API calls, 201 total items returned
- Invalid seller username: API returns `{itemSummaries: [], total: 0}` or a warning; function returns empty list without raising
- 429 on page 2 of 3: retries page 2; if all retries exhausted, returns partial (pages 1) with `_fetch_error` sentinel
- 5xx on page 1: returns partial with sentinel

---

### Unit 2: Seller Scan Script — `apps/ebay/src/seller_scan.py`

**`extract_seller_username(input_str: str) → str`**

Regex extracts from eBay URL patterns `/str/<username>` and `/usr/<username>`. Strips trailing slashes and query strings. Falls through to bare username if no URL pattern matches.

**`load_wish_list() → tuple[list[dict], str]`**

Reads `~/.cache/locg/collection.json`. Returns `(wish_list_entries, last_full_import_ts)`. Aborts with clear message if file missing or `in_wish_list` count is 0. Filters `comics` array to entries where `in_wish_list == 1` (int comparison, not bool).

**`normalize_series(s: str) → str`**

Lowercase → strip `(Vol. N)` pattern → strip `(YYYY - YYYY)` and `(YYYY)` patterns → strip leading `the ` → `re.sub(r'\s+', ' ', s).strip()`.

**`build_wish_index(wish_entries: list[dict]) → list[tuple[str, str, dict]]`**

Returns list of `(normalized_series, issue_number, entry)` tuples. Issue extracted from `full_title` via `re.search(r'#\s*(\d+)', full_title)`. Entries without a parseable issue number are included with `issue = ""` (to support future matching; not matched on issue in v1 — skip them for now to keep precision).

**`find_matches(listings: list[dict], wish_index) → list[dict]`**

For each listing:
1. Call `parse_title(listing["title"])` → `ParsedTitle`
2. Skip if `parsed.series` is empty or `parsed.issue` is None
3. `norm_parsed = normalize_series(parsed.series)`
4. For each `(norm_wish_series, wish_issue, entry)` in wish_index: if `SequenceMatcher(None, norm_parsed, norm_wish_series).ratio() >= 0.72` AND `parsed.issue == wish_issue` → match
5. De-duplicate: if a listing matches multiple wish list entries (e.g., reprints), include once with the highest-ratio match

Returns list of match dicts: `{listing, parsed, matched_entry, ratio}`.

**`print_match_table(matches: list[dict]) → None`**

Numbered table with columns: `#`, `Comic` (series + `#` + issue), `eBay Title` (truncated to 50 chars), `Current Bid`, `Ends`, `URL`. Uses `print_table()` from `ebay_fetch.py` or formats inline — whichever fits the column spec.

**`main()`**

Orchestrates: load config → get token → extract username → load wish list (abort if empty) → fetch listings (warn if partial) → parse + match → print stats (`N listings scanned, M matches found`) → print table → prompt for selection → hand off to `/comic:buy`.

**Test scenarios:**
- `extract_seller_username("https://www.ebay.com/str/sellername")` → `"sellername"`
- `extract_seller_username("https://www.ebay.com/usr/sellername?tab=all")` → `"sellername"`
- `extract_seller_username("sellername")` → `"sellername"`
- `load_wish_list()` with missing file → clear error message, exits
- `load_wish_list()` with zero `in_wish_list` entries → clear error message, exits
- `normalize_series("The Amazing Spider-Man (Vol. 1) (1962 - 1998)")` → `"amazing spider-man"`
- `normalize_series("X-Men (1991)")` → `"x-men"`
- `find_matches()` with parser output `"Amazing Spider-Man"` issue `"300"` vs wish list `"The Amazing Spider-Man #300"` → match (ratio = 1.0)
- `find_matches()` with parser output `"Amazing Spider-Man Venom"` issue `"300"` vs wish list `"The Amazing Spider-Man #300"` → match (SequenceMatcher("amazing spider-man venom", "amazing spider-man") ≈ 0.82, above threshold)
- `find_matches()` with correct series but wrong issue → no match
- `find_matches()` with unrelated series → no match
- Zero matches: print stats, no table, no selection prompt
- Matches > 30: explicit note before table

---

### Unit 3: Skill File — `.claude/skills/seller-scan.md`

New skill file orchestrating the end-to-end flow. Model: single-purpose (like `identify.md`), not an orchestrator (like `buy.md`).

**Steps:**

1. **Extract seller username** from input (R1, R2)
2. **Guard: check wish list** — abort if empty with `locg import` message (R6a), surface `last_full_import` timestamp
3. **Fetch listings** — run `seller_scan.py` (or inline steps using `ebay_fetch.py`'s new function), note if partial fetch (R3, R4)
4. **Match and present table** — report scan stats, flag if > 30 matches (R7, R8)
5. **Selection gate** — prompt for row selection by index or "all" (R9)
6. **Hand off to `/comic:buy`** — invoke with selected listing URLs (R10)

**Test scenarios:**
- Skill receives a store URL → username extracted, flow continues
- Wish list cache empty → user sees actionable error before any API call
- Seller has 0 auction listings in comics category → user sees "0 listings found" message
- Seller has > 200 listings → pagination fires, all fetched
- User selects "all" → all matched URLs fed to `/comic:buy`
- User selects `"1,3"` → listings 1 and 3 fed to `/comic:buy`
- Partial fetch → warning note shown with match table of what was retrieved

## Dependencies and Sequencing

1. **Unit 1** (`ebay_fetch.py` additions) must be implemented first — Unit 2 imports from it.
2. **Unit 2** (`seller_scan.py`) depends on Unit 1 and `title_parser.py` (no changes to title_parser needed).
3. **Unit 3** (skill file) depends on both units being present and tested.

No circular dependencies. All three units can land in a single PR.

`title_parser.py` is used as-is — no modifications required. `ebay_fetch.py`'s existing `get_token()`, `load_config()` are reused unchanged.

## Open Questions

### Resolved During Planning

- **Does `filter=sellers:{username}` accept display username?** Yes — validated against `beatlebluecat`, `ka-761233`, `hodagent`. (see origin: §Resolve Before Planning)
- **Does `/comic:buy` need a URL argument change?** No — it already accepts eBay URLs as input; the skill passes them as arguments.

### Deferred to Implementation

- **Fuzzy threshold calibration:** 0.72 validated on beatlebluecat (180 listings). Implementer should test on a second seller with different catalog composition before shipping to confirm the <20-row false-positive criterion holds across catalog styles.
- **Character name leakage false-positive rate:** Parser leaves tokens like "Venom", "Beta Ray Bill" in series output. SequenceMatcher("amazing spider-man venom", "amazing spider-man") ≈ 0.82 — above threshold, so character leakage creates false positives, not false negatives. This is the acceptable failure mode. Measure empirically; if rate exceeds ~5 false positives per 180 listings, add a post-parse strip step.
- **Pagination failure behavior:** Partial-result-with-warning approach chosen (see §Key Technical Decisions). Implementer should verify the sentinel pattern doesn't surface confusingly in the match table.
- **`print_table()` reuse vs. inline formatting:** `print_table()` in `ebay_fetch.py` takes a columns spec — implementer should determine whether mapping `parse_item_summary` output to `print_table`'s field names is cleaner than writing a dedicated table formatter in `seller_scan.py`.
