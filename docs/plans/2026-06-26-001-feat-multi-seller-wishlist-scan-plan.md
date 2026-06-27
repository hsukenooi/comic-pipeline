---
title: "feat: /comic:wishlist-sellers — Discover eBay Sellers Holding Multiple Wish-List Books"
type: feat
status: active
date: 2026-06-26
linear: BUI-221
---

# feat: /comic:wishlist-sellers (multi-seller wish-list scan)

## Overview

A new, schedule-friendly skill that scans eBay for **sellers holding two or more books from the user's wish-list**, so the user can buy several from one seller and save on combined shipping. It is the inverse of the existing `/comic:seller-scan` (which checks *one known seller* against the wish-list): this skill *discovers* sellers — including unknown ones — by fanning out a keyword search over the wish-list and grouping the matches by seller.

It is designed to run **unattended on a recurring schedule**. Wall-clock time is explicitly a non-goal (a cold run is ~20+ min at eBay's recommended pace); LLM token cost and incremental cheapness on re-runs are the optimization targets.

Working skill name: `/comic:wishlist-sellers` (alternatives: `/comic:seller-discover`, `/comic:combine-shipping` — decide at implementation).

## Requirements Trace

- **R1.** Load the wish-list from the gixen server (`GET $GIXEN_SERVER_URL/api/comics/wish-list`). **Hard-fail** on unreachable/empty server — never run against an empty list (R11 discipline; an empty list would silently yield zero sellers). **Note (review C-fix):** the endpoint catches `FileNotFoundError`/`json.JSONDecodeError` and returns HTTP 200 with `[]` (`routes.py:1032`), and the existing `fetch_wish_list()` (`seller_scan.py:52`) does a bare `return resp.json()` with no empty check. So R1 is NOT satisfied by reusing that fetch — the new code must add `if not wish_list: error + sys.exit(1)` explicitly. A corrupt wish-list file on the night of a scheduled run must error loudly, never silently report "no sellers found."
- **R2.** Parse each wish-list `name` string into `(series, issue)` via the existing `_parse_wish_name()` regex. The endpoint returns only `{name, id}`; `name` is the sole matchable field. **Known coverage gap (review N3):** `prepare_wish_items()` (`seller_scan.py:163`) skips items with no `#N` (GNs, HCs, TPBs like "…Secret Wars HC"). True scan coverage is therefore < the raw item count — document this in the skill's "Common Mistakes" table so the user isn't surprised those books never surface.
- **R3.** Incremental search: skip any wish-list item whose eBay search result is in the disk cache and fresh (7-day TTL). Only (re)search new/stale items. First run searches all; later runs are mostly cache hits.
- **R3a.** **Filter ended listings on cache consumption (review C2):** a 7-day cache will hold auctions that end during that window. On every cache hit, drop any item whose `end_date_iso` has passed *before* matching/grouping — otherwise the report surfaces sellers based on listings that no longer exist, and (worse) those dead `item_id`s get written to the seen-cache, blocking re-surface if the item relists. `end_date_iso` is already in `parse_item_summary()` output (`ebay_fetch.py:508`).
- **R4.** One eBay Browse API keyword search per searched item (`GET /buy/browse/v1/item_summary/search`, `q="<series> <issue>"`), paginated. **Must** set `filter=buyingOptions:{AUCTION|FIXED_PRICE}` — the API returns *only* FIXED_PRICE by default, so auctions are silently dropped otherwise. Three load-bearing details when cloning `search_seller_listings()` (review C4/I2/I5/I7):
  - **URL-encode `q` separately:** `search_seller_listings()` builds the query as a raw string to keep `{}` unencoded, but its `q=comic` is encoding-safe. A real keyword like `Amazing Spider-Man #129` contains `#` (a fragment separator) and spaces — `q` must be `quote(keyword, safe="")` before embedding, while the filter's `{}` stays literal. A naive clone sends a malformed URL where `#129` is dropped as a fragment.
  - **The filter token changes:** the source has `buyingOptions:{AUCTION}` (auction-only, `ebay_fetch.py:579`). Changing it to `buyingOptions:{AUCTION|FIXED_PRICE}` is a *distinct, explicit step* — not a free consequence of "clone."
  - **Cap pagination per item:** popular series (ASM, X-Men, Batman) return 500–1,000+ results; uncapped, a single series can burn a large share of the ~5,000/day quota. Cap `max_results` per wish item (e.g. 500 / 3 pages) so the cold-run call total stays bounded.
- **R4a.** **Pace explicitly:** `search_seller_listings()` has no inter-page `sleep` (only error-triggered backoff). Add `time.sleep(2)` after each successful page fetch in `search_by_keyword()` to honor eBay's ~1 call/2s recommendation across 685+ searches.
- **R5.** Each result carries `seller.username` (an **opaque immutable ID** for US sellers since 2025-09-26), `title`, price, end time, `itemWebUrl`. Group by this opaque ID — stable per seller, so grouping is reliable even without a readable handle.
- **R5a.** **Dedup before grouping (review C3):** a single listing can appear in the result set of *multiple* wish-item searches (e.g. "Spider-Man #1 (1990)" surfaced by both a "Spider-Man #1" and an "Amazing Spider-Man #1" search). Counted naively, that one physical listing inflates a seller to ≥2 even though there's only one book to buy. **Deduplicate matches by `(seller_id, listing_id)` before the ≥2 gate** — each listing contributes at most one match per seller. Keep the wish-item association for display, but the gate counts distinct listings.
- **R6.** Match each search's results back to *its* wish-list item with the hardened deterministic matcher (R7), then the Claude Haiku verify pass — but run verify **last** (R8 ordering).
- **R7.** Harden the free token matcher with deterministic hard-reject rules (Annual/Giant-Size/King-Size/Special/Treasury unless the wish item itself says so; multi-comic lots; require the exact issue number present in the title). These kill the exact look-alikes Haiku would otherwise be paid to reject — no real matches lost.
- **R8.** **Verify ordering (load-bearing, token efficiency):** run the Haiku verify pass *after* the cheap filters and the `≥2-matches-per-seller` gate, not before. The ≥2 gate is the most aggressive filter; verifying before it wastes ~20–80× the LLM calls.
- **R8a.** **Chunk the Haiku call (review C1):** `verify_with_claude()` (`seller_scan.py:242`) sends all candidates in one `max_tokens=8096` call, and on a truncated/unparseable response its fallback **returns all candidates as genuine** (`seller_scan.py:280`). At ~30–50 output tokens/verdict that caps a single call at ~150–270 candidates before silent truncation passes false positives through. A cold run's post-gate survivor set can exceed that, so **chunk at ~100 candidates/call and merge** — never assume "one call/run" is safe by count.
- **R8b.** **Partial-cache index remapping + dict contract (review I6):** `verify_with_claude()` correlates Claude's responses to inputs by 1-based list position and reads `m["title"]` and `m["wish_name"]` on each dict. The verdict-cache wrapper passes only *uncached* candidates, then merges verdicts back — so the new funnel must (a) build match dicts carrying the exact `title` + `wish_name` keys (assembled in `seller_scan.py`'s `main()` loop, line ~459, not in the standalone matchers), and (b) remap indices across the cached/uncached split.
- **R9.** **Verdict cache (load-bearing, token efficiency):** cache each listing's verify result keyed by `(listing_id, wish_item)`. A listing's "is this the book?" answer is stable for its ~3–10 day life, so re-runs skip re-verifying → steady-state LLM cost ≈ zero.
- **R10.** Drop books already owned via `POST /api/comics/collection/check/batch` (free, non-LLM). 409s on an un-imported store (R11) — treat as hard-fail, never as "not owned."
- **R11.** Drop listings surfaced in prior runs via the `/api/comics/seller-scan/seen` endpoints, so a recurring scan only ever shows *new* finds. **Seen model (review I4):** the table is keyed by `item_id TEXT PRIMARY KEY` (`db.py:1246`) and the GET with no `seller` param returns *all* seen IDs globally (`db.py:1241`). This tool **records with `seller=None`** and **reads the global seen set** (no `seller` param) — a deliberate choice that shares the seen set with named per-seller scans (a listing already shown by either tool won't re-surface). Document this so it isn't mistaken for a bug.
- **R12.** Surface only sellers with **≥2** confirmed, un-owned, unseen matches.
- **R13.** Resolve readable seller handles for the **final shortlist only**, since search returns opaque IDs. **No clean API path exists (review I3):** both `parse_item()` and `parse_item_summary()` return the opaque username, and nothing in the repo scrapes a listing page for a store name. So readable-handle resolution is **best-effort and deferred to the SerpAPI fallback** (SerpApi's eBay engine returns readable seller fields). **v1 ships without readable handles** — it groups by opaque ID and emits the listing links (the user opens any one to see the seller). Do not block the feature on HTML scraping.
- **R14.** Report per-seller: seller handle, the matched wish-list books, listing titles, prices, auction end times, links. Notify on a non-empty result (it runs scheduled).
- **R15.** No raw eBay JSON or multi-thousand-row candidate set ever flows through an orchestrating LLM's context — all filtering/grouping happens inside one script that emits only a compact final table.

## Scope Boundaries

- **Discovery + grouping only** — this skill surfaces sellers; it does not bid, snipe, or hand off to `/comic:buy` automatically (the user reviews the per-seller report and decides). A future enhancement can add a `/comic:buy`-of-a-seller handoff.
- Auctions **and** BIN (combined shipping applies to both); both are pulled via the `AUCTION|FIXED_PRICE` filter.
- Does not modify the wish-list, collection, or any LOCG state. Read-only against the user's data; writes only to the seen-tracking store and local caches.
- US marketplace only (`X-EBAY-C-MARKETPLACE-ID: EBAY_US`).

### Deferred to Separate Tasks

- The SerpAPI fallback adapter (R-fallback below) — build the interface seam now, implement the adapter only if the opaque-ID change obscures something the Browse API can't supply.
- A `/comic:buy`-an-entire-seller handoff.
- Surfacing *near-miss* sellers (exactly 1 match) as a separate low-priority list.

## Context & Research

### Relevant Code and Patterns (reuse, don't reinvent)

- **`apps/ebay/src/ebay_fetch.py`**
  - `load_config()`, `get_token()` — client_credentials OAuth with disk token cache + 429/5xx backoff. Reuse as-is.
  - `search_seller_listings()` (line ~555) — the pagination + retry loop against `item_summary/search`. **Clone it into `search_by_keyword(q, token, base_url, *, max_results, filters)` that drops the `sellers:{}` filter and sets a real `q`.** Structurally identical otherwise.
  - `parse_item_summary()` (line ~473) — already extracts `seller` (the opaque username), `item_id`, `title`, `current_price`, `end_date`, `listing_url`. Reuse unchanged.
  - `PRODUCTION_BASE`, marketplace header, token header constants — reuse.
- **`apps/ebay/src/seller_scan.py`**
  - `_parse_wish_name()` (line ~155), `_series_tokens()` (line ~150), `_strip_grades()` (line ~134), `match_listing()` (line ~182, 0.65 floor) — the deterministic matcher. **Extend** `match_listing` (or wrap it) with the R7 hard-reject rules.
  - `verify_with_claude()` (line ~242) — the Haiku verify pass. Reuse, but call it **once per run** on the post-`≥2`-gate survivor set (minus verdict-cached listings), not per seller.
  - Wish-list fetch + hard-fail pattern (lines ~28–58); seen endpoints (lines ~63–104). Reuse.
- **`apps/ebay/src/sold_comps.py`** — the SHA-256-of-canonical-URL → `~/.cache/...` disk cache with 7-day TTL and retry/backoff (`fetch()`, line ~148). **Copy this pattern** for both the eBay search-result cache and the new verdict cache.
- **`plugins/gixen-overlay` endpoints** — `POST /api/comics/collection/check/batch` (`routes.py:944`, request schema `models.py:108`) for the owned-filter; `/api/comics/seller-scan/seen` for seen-tracking; `GET /api/comics/wish-list` (`routes.py:1023`) for the wish-list.
- **`scripts/comics-server.sh`** — `comics_resolve_server` / `comics_health_gate` / `comics_curl` helpers; the canonical server-call convention (`docs/conventions/comics-server-call.md`). The skill body uses these.
- **`.claude/commands/comic/seller-scan.md`, `wishlist-add.md`** — skill-body conventions: YAML front matter (`name: comic:<slug>`), `---`-separated steps, fenced shell blocks, no emoji headers, a "Common Mistakes" table at the end.

### Institutional Learnings (carry forward)

- **eBay Finding API is permanently decommissioned** (Feb 2025) — build on the Browse API only. Confirms the memory note `finding-api-dead-for-keyset`: it's gone for everyone, not a keyset quota.
- **`apps/ebay` is NOT a uv workspace member** (BUI-88 R10) — it cannot import locg-cli or the overlay. All cross-component access is over HTTP (`requests`), exactly as `seller_scan.py` already does. The new code lives in `apps/ebay` and follows this rule.
- **Browse API rate limit** is ~5,000 calls/day default — ~685 searches sits comfortably under it. eBay recommends ~1 call/2s; pace accordingly (this is why a cold run is ~20 min, which is fine for a scheduled job).
- **Opaque seller IDs** (2025-09-26): `seller.username` is now an immutable ID for US sellers. Group on it (stable); resolve the human handle only for the shortlist.
- **Matcher false positives** (PER-132 reference set): the deterministic matcher's known misses are Annual/Giant-Size/wrong-series/lots — precisely the R7 hard-reject targets. The Haiku pass is the backstop for whatever R7 can't catch deterministically.

## Key Technical Decisions

### 1. The funnel runs in one script; the skill body is a thin dispatcher
A single Python entry point (e.g. `apps/ebay/src/wishlist_sellers.py`, console script `wishlist-sellers`) does: load wish-list → incremental search → match → seen/owned filter → group → ≥2 gate → verdict-cached Haiku verify → re-gate → emit compact JSON/table. The `/comic:wishlist-sellers` skill body only resolves the server, invokes the script, and renders the compact result. **Raw eBay JSON never crosses into an LLM context** (R15). This mirrors how `seller_scan.py` already encapsulates its work behind a CLI.

### 2. Verify last, and cache verdicts (the two token levers)
Pipeline order inside the script:
```
search results (cache hits filtered for ended listings, R3a)
  → drop CGC-titled listings              # free pre-filter, mirrors seller_scan.py main() (N5)
  → deterministic match (hardened, R7)     # free
  → dedup by (seller_id, listing_id)       # free (R5a)
  → drop already-seen                      # free, global seen set (R11)
  → drop owned (batch check)               # free, HTTP (R10)
  → group by opaque seller_id
  → drop sellers with <2 matches           # THE gate
  → split survivors by verdict cache
  → Haiku verify uncached survivors        # chunked ~100/call, merged (R8a/R8b)
  → write new verdicts to cache
  → re-apply ≥2 gate (post-verify)
  → emit compact per-seller table
```
First run: a few hundred Haiku-verified listings (vs. ~20k naive), chunked into calls of ≤100. Re-runs: ≈0 (verdict cache + seen-filter). The hardened matcher (R7) reduces the inputs *to* the ≥2 gate; combined with the gate it cuts the post-gate Haiku candidate pool by a further ~30–50%.

### 3. Hardened deterministic matcher (R7)
Before the 0.65 token score, apply conservative hard-rejects (only drop, never add):
- skip listings with "cgc" in the title — replicate the existing pre-`match_listing` skip in `seller_scan.py`'s `main()` (line ~449); a from-scratch funnel would otherwise miss it;
- reject `Annual|Giant-?Size|King-?Size|Special|Treasury` unless the wish item's series contains that word;
- reject lots: `lot of|collection|complete run|set of|#\d+\s*-\s*#?\d+`;
- require the wish item's exact issue number present as a bounded token in the title.
These rules shrink the candidate pool that *reaches* the ≥2 gate (and thus the post-gate Haiku set) by ~30–50%, for free — they run *before* the gate, not after. All are conservative (drop obvious non-matches only); regression-gate against the PER-132 true-positive set to confirm no real match is lost.

### 4. Caches (two, both 7-day, SHA-256 keyed, sold_comps pattern)
- **Search cache:** key = hash of the canonical search URL → raw `item_summary/search` JSON. Drives R3 incrementality.
- **Verdict cache:** key = `(listing_id, wish_item)` → `{pass|fail}`. Drives R9. **Prefer a small SQLite table** (not a flat JSON) under `~/.cache/wishlist-sellers/` — at steady state this holds thousands of entries and a JSON rewrite per run is O(n); SQLite is the existing pattern in the overlay's `db.py`. The `(listing_id, wish_item)` compound key is correct *because* one listing can legitimately match more than one wish item (see R5a) — the verdict is per (listing, wish-item) pair, while the ≥2 *count* dedups by listing.

### 5. Fallback seam (deferred impl)
Define the search behind a narrow interface (`search_by_keyword(...) -> list[ParsedSummary]`) so a **SerpApi adapter** (free 250 searches/mo, returns readable seller fields) can be dropped in without touching the funnel. Build the seam now; implement the adapter only if needed.

## Implementation Phases

1. **`search_by_keyword()`** in `ebay_fetch.py` — clone `search_seller_listings()`, drop the seller filter, parameterize `q` and `filters`; default `buyingOptions:{AUCTION|FIXED_PRICE}`. Unit-test against a recorded fixture; live-smoke against one query and assert `seller`, price, `itemWebUrl` present.
2. **Search cache layer** — adapt the `sold_comps.py` cache (SHA-256 URL key, 7-day TTL, backoff). Test cache hit/miss + expiry.
3. **Hardened matcher** — extend `match_listing` with R7 hard-rejects behind a flag/new function; regression-test against the PER-132 false-positive set + a handful of true positives to confirm no real matches lost.
4. **Funnel script** `wishlist_sellers.py` (console script `wishlist-sellers`) — wire load → search(cached) → match → seen → owned(batch) → group → ≥2 gate → verdict-cached Haiku verify → re-gate → emit compact JSON + a `--table` human view. Verdict cache module here.
5. **Skill body** `.claude/commands/comic/wishlist-sellers.md` — `comics_resolve_server`/health gate, run the script, render the per-seller table, resolve readable handles for the shortlist, notify on non-empty. "Common Mistakes" table (forgetting the AUCTION|FIXED_PRICE filter; treating opaque ID as a handle; running against an unreachable server).
6. **Schedule hook + notification (review N2)** — document the recurring-run invocation (e.g. via `/schedule` or cron) and confirm idempotent/incremental behavior across two consecutive runs (second run does ≈0 LLM work). **Specify the notification channel explicitly** — for an unattended job the result is invisible without one. Default: the script writes the compact per-seller report to a known log/artifact path and emits a push notification on a non-empty result (R14); decide the exact channel (push vs. the existing notification path used elsewhere in the repo) before marking the feature complete. An empty result is silent.

## Testing

- **Unit:** `search_by_keyword` query construction + pagination; cache hit/miss/expiry; R7 hard-reject rules; verdict-cache read/write; grouping + ≥2 gate.
- **Regression:** hardened matcher against the PER-132 reference set (zero new false negatives) and a true-positive set (all still pass).
- **Integration (manual / live, gated `-m integration`):** one real run against the live wish-list; assert (a) sellers reported all have ≥2 un-owned matches, (b) a second immediate run does ≈0 Haiku calls (verdict + seen caches hit), (c) no owned book appears, (d) no raw JSON in the skill's rendered output.
- Run each touched package's suite from its own dir (no repo-wide runner). `apps/ebay` is `uv tool install`-managed — verify the console script resolves on PATH after install.

## Risks & Mitigations

- **Opaque seller ID can't be resolved to a handle for some listings** → report the listing links regardless; the user can open any one to see the seller. Grouping still correct.
- **Browse API throttle / daily cap on a cold first run** → pace ~1 call/2s; the search cache means it's a one-time cost. If capped, the run resumes next schedule from cache.
- **Hardened matcher over-rejects a real edition** → rules only reject obvious look-alikes and are regression-gated against true positives; the Haiku pass remains the backstop, not the first line.
- **Owned/seen endpoints unreachable mid-run** → hard-fail the run (R11), never degrade to surfacing duplicates or re-showing seen listings.
- **Scope creep into a buy handoff** → explicitly deferred; v1 is discovery + report only.
- **Haiku response truncation silently passing false positives** (review C1) → chunk verify at ≤100/call; additionally consider asserting the parsed verdict count equals the input count and failing closed (reject) rather than the current fail-open (accept-all) on a parse miss.
- **Stale/ended listings surfaced from cache** (review C2) → filter on `end_date_iso` at cache-read time (R3a) before any matching or seen-recording.
- **One listing double-counting a seller to a false ≥2** (review C3) → dedup by `(seller_id, listing_id)` before the gate (R5a).
- **Malformed search URL dropping `#129` as a fragment** (review C4) → `quote(q, safe="")`; add a `#N`-keyword test case in Phase 1.

## Review Findings (BUI-221 plan review) — Disposition

A code-grounded review (verified against the repo) produced 16 findings; all are incorporated above. Map for traceability:

- **Critical:** C1 → R8a + Risks; C2 → R3a + Risks; C3 → R5a + Risks; C4 → R4 + Risks.
- **Important:** I1 → R1; I2 → R4; I3 → R13; I4 → R11; I5 → R4 (pagination cap); I6 → R8b; I7 → R4a.
- **Nice-to-have:** N1 → Decision 4 (SQLite); N2 → Phase 6; N3 → R2; N4 → Decision 2/3 wording; N5 → Decision 3 + pipeline diagram.
