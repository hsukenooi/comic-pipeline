---
title: "fix: Verify and complete seller-scan Finding API switch (BUI-68)"
type: fix
status: active
date: 2026-06-01
issue: BUI-68
branch: hsukenooi/bui-68
---

# fix: Verify and complete seller-scan Finding API switch (BUI-68)

## Summary

The BUI-68 fix is already committed to this branch (`6f3dc8e`): `search_seller_listings`
switched from the eBay **Browse API** (`item_summary/search`, `sellers:{...}` filter) to the
**Finding API** `findItemsIneBayStores` operation, which accepts an eBay *store name* directly.
A new `_finding_item_to_browse` reshapes the Finding API response into the Browse-shaped dict
`parse_item_summary` expects, and `fetch_wish_list` was repointed from the dead
`/Users/hsukenooi/Projects/locg-cli` path to the installed `locg` console script.

The commit was never finished: it changed the implementation but **left its unit tests mocking
the old Browse API shape** (4 of 6 `TestSearchSellerListings` tests now fail), added **no test**
for `_finding_item_to_browse`, and was **never run end-to-end** because the Finding API hit its
daily rate limit during investigation. A field-mapping audit against the confirmed Finding API
response shape also surfaced one correctness bug: auctions with an active Buy It Now
(`listingType == "AuctionWithBIN"`) are misclassified as fixed-price.

This plan closes those three gaps — tests, the mapping bug, and live verification — and ships the
result as a PR. It does **not** migrate off the (deprecated-but-operational) Finding API or re-add
comic-keyword narrowing; both are recorded as follow-ups.

---

## Problem Frame

`/comic:seller-scan tunerscomics` returned matches from random sellers, not tunerscomics. Root
cause (documented in BUI-68): `tunerscomics` is an eBay **store name**, not a seller username, so
the Browse API `sellers:{...}` filter was rejected ("invalid username") and silently fell back to
returning all ~260K results. The branch fixes this by using `findItemsIneBayStores`, which takes a
store name natively. The fix is plausible but unverified, and its test suite is red — so we cannot
currently trust it or merge it. The work here is to make the change *provably* correct: green
tests that exercise the real Finding API shape, a corrected reshaper, and a clean end-to-end run.

---

## Requirements

- **R1** — `TestSearchSellerListings` must exercise the Finding API request/response shape (not the
  retired Browse API shape) and pass. Source: BUI-68 next-steps item 3; current red suite.
- **R2** — `_finding_item_to_browse` must have direct unit coverage proving it maps `itemId`,
  `title`, `viewItemURL`, price, `endTime`, and listing type into the Browse-shaped dict that
  `parse_item_summary` consumes. Source: BUI-68 next-steps item 3.
- **R3** — Auction-with-BIN listings (`listingType == "AuctionWithBIN"`) must be classified as
  auctions, not fixed-price. Source: field-mapping audit (this plan).
- **R4** — A live `/comic:seller-scan tunerscomics` run must return listings **only** from
  tunerscomics, with URLs pointing to actual tunerscomics listings. Source: BUI-68 next-steps
  items 1-2. *(Gated on Finding API daily quota having reset.)*
- **R5** — The change ships as a PR against `main` (repo convention: branch-per-issue, no direct
  commits to `main`), with BUI-68 referenced.

---

## Key Technical Decisions

- **KTD1 — Rewrite the stale tests against the Finding API shape rather than reverting the code.**
  The Browse API path is the *known-broken* one (store-name filter silently dropped). The Finding
  API switch is the fix; the tests are simply lagging. So tests move to the new shape. Rationale:
  the implementation is the source of truth here, confirmed correct by external API-shape research
  (envelope `findItemsIneBayStoresResponse[0]`, single-element-array fields, `storeName` request
  param, `SECURITY-APPNAME` auth).

- **KTD2 — Fix `AuctionWithBIN` classification inside `_finding_item_to_browse`.** The Finding API
  returns `listingType == "Chinese"` for a plain auction but `"AuctionWithBIN"` for an auction that
  still has a live Buy It Now (it flips to `"Chinese"` once a bid lands). The current reshaper maps
  only `("Chinese", "Auction")` to `AUCTION`, so an auction-with-BIN is mislabeled `BIN` and loses
  its `currentBidPrice`. Add `"AuctionWithBIN"` to the auction set. Rationale: these listings *are*
  auctions the user can snipe; mislabeling drops them from auction-only views and shows the wrong
  price. (See Sources & Research.)

- **KTD3 — Open a PR, do not commit to `main` directly.** The ticket says "merge to main," but
  CLAUDE.md mandates branch-per-issue with a PR. PR wins; it also gives the live-verification
  result a place to be recorded before merge. Branch `hsukenooi/bui-68` already carries the fix
  commit.

- **KTD4 — Gate live verification on quota, don't block the code work on it.** The Finding API
  allows ~5,000 calls/day per app, shared across operations; the BUI-68 investigation exhausted it.
  If the quota is still drained, land the test + mapping fixes (which need no network) and run R4
  as the final pre-merge step once quota resets. Rationale: keeps deterministic work unblocked by a
  rate limit we don't control.

- **KTD5 — Keep store-name handling as-is, but assert case-sensitivity in verification.** eBay
  store names are case-sensitive in `findItemsIneBayStores`. `_extract_seller_username` does not
  normalize case (correctly — it must not). The live run uses the exact casing `tunerscomics`; if
  it returns zero results, suspect store-name casing before suspecting the code.

---

## Implementation Units

### U1. Rewrite `TestSearchSellerListings` against the Finding API shape

**Goal:** Replace the 4 stale Browse-API-shaped tests with tests that mock the Finding API JSON
envelope, so the suite proves the new request params and pagination logic.

**Requirements:** R1

**Dependencies:** none

**Files:**
- `apps/ebay/tests/test_seller_scan.py` (modify — `TestSearchSellerListings` class)

**Approach:** Replace the `_mock_page(items, total)` helper, which builds
`{"itemSummaries": items, "total": total}`, with one that builds the Finding API envelope:
`{"findItemsIneBayStoresResponse": [{"ack": ["Success"], "searchResult": [{"item": items}],
"paginationOutput": [{"totalPages": [str(n)]}]}]}`. Items in the mock are Finding-API-shaped
(single-element arrays per field) so they flow through `_finding_item_to_browse`. Update assertions:
- `test_single_page` / `test_empty_seller` — adjust to the new envelope; assert returned dicts are
  Browse-shaped (have `itemId`, `title`, `buyingOptions`).
- `test_paginates` — drive multi-page via `totalPages`, `paginationInput.pageNumber` increments.
- `test_stops_at_max_results` — confirm `max_results` truncation and single-call behavior.
- `test_username_extracted_from_url` — replace the `call_params["filter"]` assertion (filter is
  gone) with one asserting `call_params["storeName"] == "beatlebluecat"` and
  `call_params["OPERATION-NAME"] == "findItemsIneBayStores"`.
- Add a test for the `ack not in ("Success", "Warning")` error branch (e.g. `ack=["Failure"]` with
  an `errorMessage`) returning the items gathered so far.

**Patterns to follow:** Existing `@patch("ebay_fetch.requests.get")` + `MagicMock` response style
already in this file. Mirror the single-element-array item shape used in U2's fixtures.

**Test scenarios:**
- Single page of N store items returns N Browse-shaped dicts. *(happy path)*
- Multi-page: `totalPages=2` triggers a second request with `pageNumber=2`; results concatenated. *(happy path)*
- `max_results` smaller than one page truncates the result and makes exactly one request. *(edge)*
- Empty store (`searchResult.item` absent/empty) returns `[]` without erroring. *(edge)*
- Store name extracted from a `/str/<name>` URL is passed as `storeName`, not as a `filter`. *(edge)*
- `ack=["Failure"]` with an `errorMessage` returns items-so-far and prints to stderr. *(error path)*

**Verification:** `cd apps/ebay && uv run pytest tests/test_seller_scan.py::TestSearchSellerListings`
passes; no test references `itemSummaries` or a `filter` param.

---

### U2. Add unit coverage for `_finding_item_to_browse`

**Goal:** Prove the reshaper maps every field correctly from a realistic Finding API item into the
Browse-shaped dict, and that the reshaped dict survives `parse_item_summary`.

**Requirements:** R2

**Dependencies:** none (can land with U1)

**Files:**
- `apps/ebay/tests/test_seller_scan.py` (modify — add a `TestFindingItemToBrowse` class)

**Approach:** Build a canonical Finding API item fixture (single-element arrays throughout):
`itemId: ["298217294954"]`, `title: [...]`, `viewItemURL: [...]`,
`listingInfo: [{"listingType": ["Chinese"], "endTime": ["2026-05-28T12:00:00.000Z"]}]`,
`sellingStatus: [{"currentPrice": [{"@currencyId": "USD", "__value__": "150.00"}]}]`. Assert the
reshaped dict has the Browse keys (`itemId`, `title`, `buyingOptions`, `currentBidPrice`, `price`,
`itemEndDate`, `itemWebUrl`, `seller`) with correctly unwrapped values. Then pipe the reshaped dict
through `parse_item_summary` and assert the end-to-end parsed output (`item_id == "298217294954"`,
`listing_type == "Auction"`, `current_price == "$150.00"`, `end_date_iso` preserved, `seller` set).

**Patterns to follow:** `TestParseItemSummary` in the same file already validates the Browse-shaped
dict path — chain into it rather than re-asserting its internals.

**Test scenarios:**
- `"Chinese"` listing → `buyingOptions == ["AUCTION"]`, `currentBidPrice` populated from
  `currentPrice`. *(happy path)*
- Fixed-price (`listingType == "FixedPrice"`) → `buyingOptions == ["FIXED_PRICE"]`,
  `currentBidPrice is None`, `price` populated. *(happy path)*
- `itemId` is the plain numeric string and survives `parse_item_summary`'s `v1|..|0` regex as the
  bare ID. *(edge)*
- Missing `viewItemURL` falls back to the `https://www.ebay.com/itm/{item_id}` URL. *(edge)*
- `endTime` ISO string maps to `itemEndDate` and round-trips through `parse_item_summary`'s
  `end_date_iso`. *(edge)*
- Reshaped dict fed to `parse_item_summary` yields the expected `current_price` and `seller`. *(integration)*

**Verification:** New `TestFindingItemToBrowse` passes; covers both auction and fixed-price branches.

---

### U3. Fix `AuctionWithBIN` classification in `_finding_item_to_browse`

**Goal:** Classify auction-with-Buy-It-Now listings as auctions, not fixed-price.

**Requirements:** R3

**Dependencies:** U2 (test scaffold for the reshaper exists)

**Files:**
- `apps/ebay/src/ebay_fetch.py` (modify — `_finding_item_to_browse`, the `buying_options` line)
- `apps/ebay/tests/test_seller_scan.py` (modify — add scenario to `TestFindingItemToBrowse`)

**Approach:** Change the auction membership test from
`listing_type in ("Chinese", "Auction")` to include `"AuctionWithBIN"`. Keep `"Auction"` in the set
defensively even though the response side uses `"Chinese"`. One-line logic change plus a guarding
test.

**Patterns to follow:** Same conditional already in the function; just widen the tuple.

**Test scenarios:**
- `listingType == "AuctionWithBIN"` → `buyingOptions == ["AUCTION"]` and `currentBidPrice`
  populated (regression guard for the bug this unit fixes). *(happy path / regression)*
- `listingType == "StoreInventory"` → `buyingOptions == ["FIXED_PRICE"]` (confirms only true
  auction types flip to AUCTION). *(edge)*

**Verification:** New scenario passes; full `apps/ebay` suite green
(`cd apps/ebay && uv run pytest -m "not integration"`).

---

### U4. Live end-to-end verification of `/comic:seller-scan tunerscomics`

**Goal:** Confirm the fix works against the real eBay Finding API: results are only from
tunerscomics and URLs point to actual tunerscomics listings.

**Requirements:** R4

**Dependencies:** U1, U2, U3 (code correct and tested before spending a live quota call)

**Files:** none (operational verification, not a code change)

**Approach:** Run `seller-scan tunerscomics --json` (or via the `/comic:seller-scan` skill) with both
`locg` and `seller-scan` console scripts installed (already confirmed on PATH). Inspect output:
every returned listing's seller/store is tunerscomics; spot-check 2-3 `listing_url`s resolve to
tunerscomics listings. If the run errors with a rate-limit / quota message, **stop and record** —
this unit is gated on quota reset (KTD4); the code units (U1-U3) still merge, and U4 completes once
quota frees up. If zero results come back, check store-name casing first (KTD5).

**Execution note:** This is the only unit that consumes the live Finding API quota — run it last,
once, after the suite is green. Avoid repeated runs that re-exhaust the daily limit.

**Test scenarios:** *Test expectation: none — operational verification, not an automated test.* The
acceptance check is manual inspection of the live output per R4.

**Verification:** Output lists only tunerscomics listings; sampled URLs open tunerscomics auction
pages. Record the result (pass / quota-blocked) in the PR description and BUI-68.

---

### U5. Open the PR and record verification

**Goal:** Ship the change for review against `main`, referencing BUI-68 and the verification result.

**Requirements:** R5

**Dependencies:** U1-U4 (or U1-U3 with U4 explicitly noted as quota-gated)

**Files:** none (PR creation)

**Approach:** Push `hsukenooi/bui-68` and open a PR with `gh pr create --base main`. Body summarizes:
the two original bugs, the test rewrite, the `AuctionWithBIN` fix, and the live-verification outcome
(or its quota-gated status). Link BUI-68. Note the deferred follow-ups (see Scope Boundaries).

**Test scenarios:** *Test expectation: none — PR creation.*

**Verification:** PR exists against `main`, CI green (CI only AST-parses `plugin.py`; run
`apps/ebay` tests locally as the real gate — see CLAUDE.md).

---

## Scope Boundaries

### In scope
- Rewriting `TestSearchSellerListings` to the Finding API shape (U1).
- New `_finding_item_to_browse` coverage (U2).
- The `AuctionWithBIN` classification fix (U3).
- Live verification of tunerscomics (U4) and the PR (U5).

### Deferred to Follow-Up Work
- **Re-add comic-keyword narrowing.** The old Browse path passed `q=comic`; the Finding path omits
  `keywords`, so it fetches *all* of a store's auctions before wish-list matching filters them. Fine
  for an all-comics store like tunerscomics, but a mixed-inventory store could hit `max_results`
  before reaching comics. Add an optional `keywords` param if this surfaces.
- **Migrate off the Finding API.** It was officially decommissioned Feb 5, 2025 and is operational
  only on borrowed time, with no support guarantee. A future move back to the Browse API (with
  proper store-name → username resolution, e.g. via a commerce/identity lookup) would be the
  durable fix. Out of scope here — BUI-68 is about making seller-scan work *now*.

### Out of scope
- Any change to the `/comic:*` skill orchestration or the matching algorithm (`match_listing`,
  `prepare_wish_items`) — untouched by this fix.

---

## Risks & Dependencies

- **Live quota (R4/U4).** ~5,000 Finding API calls/day per app, shared across operations; each
  seller-scan run makes up to `ceil(store_auctions / 100)` calls. If still drained from the BUI-68
  investigation, U4 is blocked — mitigated by KTD4 (land U1-U3, gate U4).
- **Store-name casing (KTD5).** A zero-result live run most likely means the store name casing
  doesn't match eBay's exact store name, not a code defect. Check casing before debugging code.
- **Finding API deprecation.** Operational but unsupported since Feb 2025; could disappear without
  notice. Tracked as a deferred follow-up, not a blocker for this fix.
- **Tooling dependency.** Both `locg` and `seller-scan` console scripts must be on PATH for U4
  (confirmed present at `~/.local/bin/`). A stale wrapper shadowing the uv-installed binary is a
  known failure mode (see install.sh / BUI-27).

---

## Sources & Research

- **eBay Finding API `findItemsIneBayStores` response shape** (external research, 2026-06-01):
  Confirmed the JSON envelope `findItemsIneBayStoresResponse[0]` with single-element-array fields;
  `itemId` is plain numeric (not Browse's `v1|..|0`); `sellingStatus[0].currentPrice[0]` carries
  `@currencyId` + `__value__`; `storeName` is a direct request param; `SECURITY-APPNAME` is the App
  ID (client_id) auth param; pagination via `paginationInput`/`paginationOutput.totalPages`.
  - Load-bearing finding (KTD2): `listingInfo[0].listingType` returns `"Chinese"` for a plain
    auction but `"AuctionWithBIN"` for an auction with a live Buy It Now — the request-side filter
    value `"Auction"` is an umbrella over both. The reshaper's auction set must include
    `"AuctionWithBIN"`.
  - Sources: [findItemsIneBayStores reference](https://developer.ebay.com/DevZone/finding/CallRef/findItemsIneBayStores.html),
    [ListingInfo type](https://developer.ebay.com/devzone/finding/callref/types/ListingInfo.html),
    [JSON access KB 1227](https://developer.ebay.com/support/kb-article?KBid=1227),
    [API call limits](https://developer.ebay.com/develop/get-started/api-call-limits).
- **BUI-68** (Linear) — root-cause analysis, the fix commit `6f3dc8e`, and the four next-step items
  this plan executes.
- **Current test failures** — `cd apps/ebay && uv run pytest tests/test_seller_scan.py` shows 4
  failures in `TestSearchSellerListings` (Browse-shape mocks against Finding-API code), confirming
  R1's premise.
