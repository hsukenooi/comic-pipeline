---
title: "feat: Secondary sold-comps provider (sold-comps.com) behind fetch() (BUI-545)"
date: 2026-07-28
type: feat
status: draft
linear: BUI-545
module: apps/ebay
tags: [fmv, sold-comps, serpapi, provider-fallback, circuit-breaker]
---

# Secondary eBay sold-comps provider fallback (BUI-545)

> **Context.** eBay login-walled the Sold/Completed search filters ~2026-07-23
> (SerpApi public-roadmap#4064), killing every logged-out scraper including
> SerpApi's eBay engine. sold-comps.com (the caffein.dev developer's direct
> API) is the sole provider with verified post-wall sold data: the 2026-07-28
> smoke test passed all five checks against pre-wall cached SerpApi controls
> (66/70 exact price matches, post-wall `endedAt` dates, structurally distinct
> sold/active modes). See BUI-545's comments for the full research trail.
> This plan wires it in as the secondary tier behind `sold_comps.fetch()`.

## 0. The one architectural fact everything hangs on

comic-fmv consumes ebay-sold-comps **only through the batch JSON contract**
(`{input, queries_used, comps, slab_comps, breaker_tripped, error?}`), and all
of fmv_runner's failure handling keys on generic properties of that contract:
`_is_fetch_error` (fmv_runner.py:1660) checks "0 comps AND every
`queries_used` entry carries an `error` key"; `breaker_tripped` is a passthrough
boolean. **Therefore the entire failover lives in
`apps/ebay/src/sold_comps.py`; apps/fmv needs zero code changes.** A
mixed-provider trail (SerpApi error entry + sold-comps.com success entry)
already classifies correctly: not a fetch-err, comps present, rows upsert.
Both-providers-fail yields all-`error` entries → fetch-err → BUI-536 row guard
holds, satisfying that AC by construction.

`fmv_math._parse_sold_date` (fmv_math.py:359) already accepts ISO-8601, so
sold-comps.com's `endedAt` (`YYYY-MM-DD`) needs **no format conversion**.
`buying_format` is stored but never consumed downstream — verbatim passthrough
is fine.

## 1. Design decisions

### D1. Failover is per-query, in a new `_fetch_with_fallback()` — reusing BUI-537 superseded-attempt semantics

> *(As-built deviation: the draft renamed `fetch()` → `_fetch_serpapi()` and
> made `fetch()` the orchestrator. Implementation instead left `fetch()` —
> name, signature, 2-tuple return — completely untouched and added a private
> `_fetch_with_fallback()` orchestrator that `fetch_book_comps._run` calls.
> Reason: dozens of existing tests unpack `fetch()`'s 2-tuple or monkeypatch
> `sc.fetch` with 2-tuple fakes; keeping the surface frozen preserved the
> entire 192-test suite unmodified, which is itself the behavior-parity
> evidence.)*

`_fetch_with_fallback()` orchestrates providers in order:

1. SerpApi cache → hit? return.
2. SerpApi live (breaker-gated, existing retry policy) → success? return.
3. On SerpApi failure (SerpApiError / RequestException / BreakerTrippedError):
   record the failed SerpApi attempt via the **existing `record_attempt` hook**
   — it is now a *superseded* attempt in exactly the BUI-537 sense (a further
   attempt follows), so the trail mechanism extends rather than forks. The
   hook signature gains a `provider` argument (internal, single call site in
   `fetch_book_comps._run`).
4. sold-comps.com cache → hit? return.
5. sold-comps.com live (own breaker, see D3) → success? return.
6. Both failed → raise the secondary's exception chained `from` the primary's.
   `_run`'s existing except-branch records it as the terminal error entry.

It returns `(data, cache_hit, provider)` so `_run` can parse per-provider and
tag the success entry. Failover triggers **on exception only** — a SerpApi
200 with 0 `organic_results` stays a genuine n=0 (BUI-536's
error-key-vs-empty distinction), never a second-provider probe; anything else
double-spends on every legitimately thin vintage query.

### D2. Provider identifiers and trail tagging

Every `queries_used` entry (success, terminal error, and superseded-attempt)
gains `"provider": "serpapi" | "sold-comps.com"`. No other trail field
changes; BUI-536's `_is_fetch_error` keys only on `error` presence and is
untouched. Provider-absent entries = pre-BUI-545 rows (historical `--out`
files), not a live case.

### D3. The secondary gets its own breaker — counting terminal failures only

A second `_CircuitBreaker` instance per `run_batch()`, threaded alongside the
existing one. Asymmetry, deliberate: the SerpApi breaker counts every charged
attempt including interim retries (each is a real SerpApi charge); the
sold-comps.com breaker counts **terminal failures only**. Rationale: their
60/min rate limit means a concurrent batch can 429 transiently and recover on
the existing backoff — an interim 429 is neither charged nor evidence of an
outage, and counting it would trip the secondary breaker spuriously on any
large batch. Terminal signals that do count: retries-exhausted 5xx/502,
network errors, 401/403 (bad key / quota gone), and D5's sold-shape violation.
Both breakers tripped → step 5 short-circuits → both-fail fetch-err (AC).
`breaker_tripped` in the output stays the OR of the two (its meaning — "this
book was affected by an outage" — is unchanged for consumers).

### D4. Per-provider cache keys via a second canonical URL

`canonical_sold_comps_url(nkw, ...)` mirrors `canonical_serpapi_url`
(sold_comps.py:385): endpoint + sorted params, **no API key** (the key rides
the Authorization header and never enters the URL). Same
`_cache_path`/`_cache_get`/`_cache_put` machinery, same 7-day TTL, same cache
dir — the differing endpoint host makes the sha256 keys disjoint from every
existing SerpApi entry, so nothing is invalidated. Each cache file stores the
**raw provider response** (not a normalized shape), preserving the
debug/audit property the SerpApi cache has today.

### D5. Normalization + an R11-style runtime sold-shape guard

A per-provider result extractor feeds the existing dedupe/exclude loop in
`_run`:

| comp field | SerpApi (`parse_comp`, today) | sold-comps.com |
|---|---|---|
| `product_id` | `product_id` or `item_id` | `itemId` |
| `title` | `title` | `title` |
| `price` | `price.extracted`/`raw` | `soldPrice` (see D6) |
| `grade` | `parse_grade(title)` | same |
| `sold_date` | `"Jul 20, 2026"` free text | `endedAt` ISO, verbatim |
| `buying_format` | `buying_format` | `buyingFormat` verbatim |
| `link` | `link` | `url` |

The SerpApi path verifies `LH_Sold=1` in `ebay_url` (sold_comps.py:633). The
sold-comps.com analog is structural: **every** item in a `sold=true` response
must have `listingType == "sold"` plus `soldPrice` and `endedAt`; any
violation raises `SoldCompsError` (new, sibling of `SerpApiError`) — a
provider failure, not a partial parse. Strict-any is the money-safe choice:
one active listing blended into a comp pool corrupts FMV silently (the
generalized `LH_Sold=1` trap the AC names). The smoke test measured 0/79
violations; if this ever proves flaky in practice, loosening is a deliberate
follow-up decision, not a default.

### D6. Price semantics: `soldPrice` in v1; `bestOfferAccepted` deferred

`soldPrice` matched SerpApi's `price.extracted` exactly on 66/70 overlapping
items (the 4 misses were <0.5% FX-requote drift). Using it keeps comp
semantics provider-independent — the same sale prices identically whichever
provider served it. `bestOfferAccepted` (the *actual* accepted amount, which
SerpApi never had — eBay shows the inflated listing price for OBO sales) is a
genuine FMV-accuracy improvement but changes pool semantics mid-provider, so
it's a separate evaluation ticket (see §5), not a rider on the failover.

### D7. Request parameters (documented so they're deliberate)

`keyword=<nkw>` (the existing `build_query` output verbatim — same phrases,
same `-cgc -cbcs` excludes; the smoke test already validated this exact query
shape end-to-end) · `count=240` (max; one request replaces SerpApi's ~60/page,
so the BUI-523 page-2 logic stays SerpApi-only and `_has_next_page` is false
for this provider) · `daysToScrape=90` (eBay's sold-search window — SerpApi
parity) · site/`includeCompleteListing` left at API defaults (the smoke test
ran defaults and passed fidelity; D5's guard enforces sold-shape regardless).

### D8. Key loading: the secondary is optional, degrading to today's behavior

`load_sold_comps_key()` mirrors `load_serpapi_key()` (env `SOLD_COMPS_KEY`,
then `apps/ebay/.env`) but returns `None` instead of `sys.exit(2)` when
absent: no key → failover steps 4–5 are skipped and behavior is byte-for-byte
today's (SerpApi-only, fetch-err on outage), plus a **once-per-batch** stderr
note the first time failover would have fired ("SOLD_COMPS_KEY not set — no
secondary provider"). The key is already on this machine's
`apps/ebay/.env`; the Mini needs it added at deploy (§4).

### D9. Rate limiting: a small semaphore, not a scheduler

Module-level `threading.Semaphore(4)` around sold-comps.com live calls.
`DEFAULT_MAX_WORKERS=10` books × up to 4 tiers can burst past 60/min on a big
batch; the semaphore plus the existing `retry_request` backoff (2/4/8s,
429-retryable) rides the window out without a token-bucket. Deliberately
crude — a real FMV run is 5–20 searches and rarely touches the limit at all.

### D10. Provider order is env-overridable; the default satisfies the AC

`EBAY_SOLD_COMPS_PROVIDERS` (comma-ordered, default `serpapi,sold-comps.com`)
selects and orders providers. The AC's "failover triggered by outage signals,
not a manual flag alone" is satisfied by the default; the override exists
because SerpApi's sold engine may be dead *indefinitely* — without it, every
batch pays ~5 charged 32-second SerpApi errors before the breaker trips,
forever. Setting `EBAY_SOLD_COMPS_PROVIDERS=sold-comps.com` on the Mini is an
operational choice to make **after** watching a few real runs (documented in
the ticket, not flipped in this PR). No auto-demotion/persistent-health state
in v1 — cross-run state is complexity the env var defers.

## 2. Implementation order (single PR on `hsukenooi/bui-545-sold-comps-provider-fallback`)

All in `apps/ebay/src/sold_comps.py` unless noted:

1. **Provider plumbing:** `SOLD_COMPS_ENDPOINT`, `SoldCompsError`,
   `load_sold_comps_key()`, `canonical_sold_comps_url()`,
   `_fetch_sold_comps_live()` (requests.get + Bearer header + semaphore +
   `retry_request` with the same 429/5xx policy + D5 shape guard), extractor
   `parse_comp_sold_comps(item)`.
2. **Rename** current `fetch()` → `_fetch_serpapi()` (internal callers only;
   the name `fetch` stays the public orchestrator so external references hold).
3. **Orchestrating `fetch()`** per D1, returning `(data, cache_hit, provider)`;
   provider-ordered loop driven by D10's env parse.
4. **`fetch_book_comps._run`:** thread `provider` through the
   `record_attempt` closure and both entry-builders; branch result extraction
   on the returned provider; `_has_next_page` only for serpapi data.
5. **`run_batch`:** construct the second breaker, thread both.
6. **`_print_human`:** provider tag in the tier summary when a non-primary
   served (e.g. `base[sold-comps.com]`), and the aggregate breaker line names
   which provider(s) tripped.
7. **Tests** (§3), **docs** (CLAUDE.md's FMV bullet + ticket cost note §4).

## 3. Test plan (`apps/ebay/tests/test_sold_comps.py`, mocked requests — no live calls)

- **Failover happy path:** SerpApi raises → sold-comps.com serves → comps
  present; trail = serpapi `error` entry + sold-comps.com `hit|live` entry,
  both provider-tagged.
- **Both fail:** all entries carry `error` → feeding the trail to a copy of
  fmv's `_is_fetch_error` predicate logic classifies fetch-err (contract test
  for the BUI-536 guard).
- **Breaker separation:** SerpApi breaker tripped → fetch skips straight to
  secondary (no BreakerTrippedError surfaced, no SerpApi charge); secondary
  breaker trips independently on terminal failures; interim 429-then-success
  does NOT count toward it; both tripped → immediate both-fail.
- **Cache keying:** same nkw → distinct cache paths per provider; a SerpApi
  cache hit never satisfies a sold-comps.com lookup and vice versa; page-1
  SerpApi canonical URL byte-identical to pre-change (existing cache
  preserved).
- **Shape guard:** one `listingType:"active"` item in a sold response →
  `SoldCompsError`, entry recorded as provider error, breaker incremented.
- **Extractor mapping:** itemId/soldPrice/endedAt/url/buyingFormat → comp
  fields; `endedAt` parses through a vendored copy of `_parse_sold_date`'s
  ISO branch expectations; grade parsing and `hard_exclude` apply identically.
- **No-key degradation:** `SOLD_COMPS_KEY` absent → SerpApi failure surfaces
  exactly as today (single error entry, no secondary attempt), warning printed
  once per batch.
- **Provider order override:** `EBAY_SOLD_COMPS_PROVIDERS=sold-comps.com` →
  SerpApi never called.
- **Regression:** full existing suite (128 tests) green — SerpApi-only paths
  byte-for-byte unaffected when the secondary never fires.

Run: `cd apps/ebay && uv run --with pytest pytest` (plain `uv run pytest`
false-passes in apps/*).

## 4. Deploy + validation (Mac Mini)

1. Merge → on the Mini: `./scripts/install.sh` (its `--reinstall` busts the
   wheel cache; do NOT rely on bare `--force`, per BUI-455).
2. Add `SOLD_COMPS_KEY=sc_...` to the Mini's `apps/ebay/.env`.
3. Live validation: one real `comic-fmv` run on a small batch; confirm
   provider tags in `--out` trail, comps populated, no `source=error`, spend
   visible on the sold-comps.com dashboard (~free-tier scale).
4. Record final cost note on BUI-545 (AC): free 100 req/mo now; $9/mo = 2,000
   if exceeded; per-search pricing; ~1–4 searches/book.
5. After a few runs, decide the D10 env override on the Mini and note the
   decision on the ticket.

## 5. Out of scope / follow-ups

- **`bestOfferAccepted` evaluation** — filed as its own BUI issue (decision +
  possible implementation; changes comp semantics, needs its own
  bias analysis à la BUI-525).
- **Logged-in Playwright hedge** — stays documentation in BUI-545's research
  comment; only worth building if sold-comps.com dies.
- **Auto-demotion of a persistently dead primary** — D10's env var is the v1
  answer; revisit only if manual toggling proves annoying.
- **SerpApi removal** — never; if eBay reverts the wall, the default order
  makes recovery automatic.

## 6. Linear structure verdict

**No project.** The work is one focused change to one file plus its tests —
one PR, one issue (BUI-545, already In Progress), ~1–2 days. The
linear-method bar for a Project (multi-week finite effort with sub-issues
worth tracking separately) isn't met; the only independently meaningful unit
is the `bestOfferAccepted` follow-up, which becomes a single loose issue.
