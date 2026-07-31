---
name: comic:fmv
description: Calculate fair market value for a raw (ungraded) comic from eBay sold listings. Use when the user wants to price a comic, set a bid cap, or validate an auction's current price.
---

# Comic FMV

Compute fair market value from real eBay sold transactions. No multiplier math — just recent comps in the target condition.

The full math spec — grade parsing, pool-building/widening, IQR trim, grade-curve interpolation, the CGC-proxy fallback, the confidence rubric, caching internals, the stale-install (`comic-fmv --version`) check, the manual fallback (CLI unavailable), and the CLI-debugging Common Mistakes table — lives in `docs/conventions/fmv-math-spec.md`. Read it only when debugging the CLI, building a new consumer, or doing a manual fallback computation. Everything below is what the default path needs.

## How to run

**Default path: `comic-fmv`.** It handles fetch (via `ebay-sold-comps`), cache, dedup, hard-excludes, grade parsing, IQR + quartiles, confidence rubric, self-exclusion, and DB upsert.

Before running, ensure `SOLD_COMPS_KEY` and `SERPAPI_KEY` are set (BUI-545: sold-comps.com is the default primary provider, SerpApi the fallback tier) — source the canonical env file if not:

```bash
set -a && source ~/Projects/comic-pipeline/apps/ebay/.env && set +a
```

Then resolve and health-gate the comics server — **every run, on this default path, not just as a manual fallback** (BUI-439: `comic-fmv` reads `COMICS_SERVER_URL` from env only and hard-fails "must be set" if it's unset — a Mac Mini/MacBook shell that hasn't exported it needs the hostname fallback below, or the CLI dies before it ever queries anything). `comic-fmv` is a **child process**, not an HTTP call this shell makes itself, so it needs the var actually exported into this shell's env (not just resolved inside a one-off `comics-api` subprocess) — `comics_resolve_server` still does that part. Route the health-check itself through `comics-api` (BUI-510) rather than the raw `comics_health_gate` call, so it shares the exact same call path every other skill's server check uses:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # exports COMICS_SERVER_URL for comic-fmv below
comics-api GET /health >/dev/null || exit 1   # the server must answer
```

If either step fails, **stop immediately** — the comics server is unreachable or the machine is unrecognised, so FMV data cannot be saved. Do not proceed with any queries.

```bash
comic-fmv --batch <working_list.json> --out <results.json> --brief
```

`--batch` JSON shape: `[{item_id, title, issue, year, publisher?, variant?, grade, grade_confidence?, locg_id?, locg_variant_id?, notes?}, ...]`

Literal example (build it directly — the shape is documented here, don't grep `apps/fmv` source for it): `[{"item_id": "115834720199", "title": "Fantastic Four", "issue": "16", "year": 1963, "publisher": "Marvel", "grade": "VG 4.0", "grade_confidence": "medium"}]`

`publisher` and `variant` are optional but **load-bearing** (BUI-161). `variant` (e.g. `Newsstand`, `Direct`) gives base vs variant editions distinct `comic_id`s (BUI-28), so omitting it conflates two sub-markets onto one comic.

**Pass `publisher` whenever you know it — including Marvel and DC (BUI-566).** This corrects the older "only for non-Marvel/DC titles" advice, which silently disabled a shipped fix: `ebay-sold-comps` decides per-publisher what to do with the field in **one** place (`_publisher_qualifier`), so there is no publisher you should deliberately withhold.

- **Marvel** → appends the canonical `marvel comics` (BUI-315). This is what keeps a **year-less** query like `"X-Men 97"` from pulling in non-comic merchandise (the 2024 *X-Men '97* show). Omitting `publisher` disables it with no warning.
- **DC**, and DC/Marvel imprints (Vertigo, Wildstorm, Epic, …) → appends **nothing**. A two-token `dc comics` measurably narrows recall (Batman #232: 34 comps → 12; Detective #400: 38 → 21), so DC short-circuits to no qualifier at all (BUI-315/BUI-321). Passing `DC` is a safe no-op, not a regression — the query is byte-for-byte the same as omitting it.
- **Indie** (Image, Dark Horse, Valiant, …) → appends the name verbatim. This is the primary noise filter that keeps trading cards, toys, and unrelated goods out of the pool (BUI-161).

**Don't guess it, though.** An absent `publisher` is safe (the base query passes through untouched); a *wrong* one is not — a misattributed indie or Marvel name appends a term that narrows the pool to nothing or to the wrong market. Pass it when you actually know the publisher; leave it out when you don't.

**What the Marvel qualifier does *not* do:** it cannot separate two *Marvel* volumes that share an issue number — X-Men Vol. 1 #85 and Vol. 2 #85 are both `marvel comics`. Only `year` disambiguates a same-publisher relaunch, so a **year-less** pre-2000 book on a relaunched masthead can still price off a pool polluted with cheap modern issues (the signature: a suspiciously low range and a LOW/degenerate confidence, while a sibling issue above the relaunch's issue ceiling prices cleanly and HIGH). Treat that as a signal to supply `year`, not as a reason to re-run. Also note BUI-315's recall measurements predate the BUI-545 provider switch — they were taken on SerpApi, and the qualifier's effect on sold-comps.com is unmeasured.

`grade_confidence` (optional, `high`|`medium`|`medium-low`|`low` — **four** levels, BUI-162) is the photo-coverage confidence from `/comic:grade`. When present and low, it haircuts the max bid — `medium-low` and `low` haircut **differently** (0.70 vs 0.60), so don't collapse them. Absent → standard 80% bid, no haircut (back-compat for seller-stated grades and manual runs).

**`title` is normalized automatically (BUI-346)** — you don't need to hand-clean it before building the working list. `comic-fmv` strips a leading article (`The`/`A`/`An`) and an embedded `#<issue>` (or bare trailing issue number) that duplicates the separate `issue` field, before the title ever reaches `ebay-sold-comps`. Real incident: `"The Amazing Spider-Man #50"` alongside `issue: "50"` built the doubled, malformed query `"The Amazing Spider-Man #50 50"` — 0 results on every tier (ASM #50, 2026-07-13). `ebay-sold-comps`' `build_query` carries the same normalization as a second, independent layer, so a title that reaches it un-normalized (e.g. a direct `--title` CLI call) is still safe.

Flags:
- `--max-age-days N` (default 7): reuse FMVs already in the comics server's DB if `fmv_updated_at` is within N days
- `--force`: bypass both the SerpApi cache and the DB cache and recompute everything. **It cannot clear a `one_sided`/`too_wide` flag** — it refetches the *same* market and re-flags identically. To move a flagged book, change the input (`title`/`publisher`/`year`) or widen `--grade-window`; a bare `--force` retry is a wasted no-op. **Hand-priced rows (BUI-533):** a row whose `fmv_notes` starts with `hand §` or `hand OVERRIDE` (an operator's manual override, e.g. Batman #251) is skipped entirely by a default run — reported as `skipped N hand-priced row(s) (use --force to overwrite)` and a `skipped_hand_priced` source on that row — even when it's stale enough that normal cache logic would otherwise recompute it. Only `--force` overwrites one, and it echoes the old hand notes to stderr first so they aren't silently lost.
- `--grade-window N` (default 2.0): raise or lower the comp-pool widening ceiling — does **not** bypass the one-sided/too-wide guards (a guarded book still flags `needs_manual`)
- `--brief`: after the table, print one compact JSON object per row on stdout (`item_id`, `comic_id`, `fmv_id`, `max_bid`, `flag_reason`, `confidence`, `fmv_low`, `fmv_high`, `fmv_notes` — BUI-505; `source` — BUI-549) — the linkage + pricing fields to carry forward, without re-reading the full `--out` file. `source: "skipped_lookup_error"` marks a row the comics-server lookup FAILED for — left completely untouched, not a genuine zero-comps book — distinguishable from an ordinary unpriced row, which has the same null pricing fields but a different `source`.
- `--quiet`: suppress the human table on stdout (combine with `--brief` for JSON lines only)
- `--server-url URL`: override `COMICS_SERVER_URL`/`GIXEN_SERVER_URL` for this run
- `--version`: print the installed version plus the git SHA/date the binary was built from, then exit

The CLI prints a human-readable table to stdout and writes the full structured result to `--out` on disk. Present the table to the user. **Carry the `--brief` JSON lines forward to Step 4 of `/comic:buy`** (`item_id`, `comic_id`, `fmv_id`, `max_bid`, `flag_reason`, `confidence`, plus `fmv_low`/`fmv_high`/`fmv_notes` for the range + haircut presentation (BUI-505) and `source` to tell a lookup-error skip apart from an ordinary unpriced row (BUI-549)) — don't re-read the full `--out` JSON for linkage; the `--out` file on disk stays available if you need a full row (`queries_used`, `trimmed_pool`, etc.) for debugging.

**`--out` row schema** (one object per book; use these exact keys — do **not** guess `comp_pool`/`pool`/`prices`):
- Top-level: `input`, `fmv`, `comp_count_total`, `queries_used`, `db_row`, `comic_id`, `fmv_id`, `source` (`fresh`|`cached`|`cgc-proxy`|`error`), `breaker_tripped` (BUI-535, on rows that ran a live fetch — see § Provider request budget). `comic_id`/`fmv_id` are top-level on fresh/proxy rows; on `cached` rows read them off `db_row` (`id`/`fmv_id`).
- The surviving comps are **nested** at `fmv.trimmed_pool`, alongside `fmv.median`/`fmv_low`/`fmv_high`/`max_bid`/`bid_factor`/`flag_reason`/`confidence`/`window`.

When you do need a pool field, **project it in one shot — never Read the whole `--out` file** (it's dominated by `queries_used`/`trimmed_pool`):

```bash
python3 -c "import json; print([(x['input']['item_id'], (x.get('fmv') or {}).get('trimmed_pool'), x['queries_used']) for x in json.load(open('<results.json>'))])"
```

**`fetch-err` ≠ `n/a` (BUI-143):** a row whose FMV column reads `fetch-err` (and the loud post-table warning) means the **sold-comps fetch failed** for that book — since BUI-545 that means **both providers** (sold-comps.com primary AND the SerpApi fallback) errored — **not** that the book has no comps. Treat a `fetch-err` row (or a whole batch that comes back all `fetch-err`/`n/a`) as a provider failure: check `SOLD_COMPS_KEY`/quota first (free tier is 100 req/mo), then SerpApi, and re-run. Never tell the user these books are illiquid or bid on them as if priced.

## Provider request budget (BUI-570)

The sold-comps providers are a **shared, finite budget**, and a batch spends far more than one request per book. Read this before running anything above ~50 books.

**A book is not one request.** `ebay-sold-comps` runs a tiered strategy per book: a base query always, plus a gated page-2 fetch, a broadened (year-dropped) tier, a grade-targeted tier, and a vintage "inclusive" tier — each conditional, up to five live queries for a thin vintage book. Each of those can additionally cost **two** provider requests, since a failing sold-comps.com query fails over to SerpApi (BUI-545). A 66-book batch is a low-hundreds-of-requests batch, not a 66-request one.

**The 7-day query cache is what makes a re-run cheap — but only for an *identical* query.** Re-running the same books inside 7 days is served from cache and costs nothing. Two things void that: `--force` bypasses the cache and re-spends the whole batch, every tier; and **changing any query input re-prices from scratch** — adding a `publisher`, filling in a `year`, or correcting a `title` builds a different query string, so it misses the cache by design. Budget a corrected batch as a full-price run, not a cheap retry. (Per the `--force` note above it also cannot clear a `one_sided`/`too_wide` flag, so a bare `--force` retry is a wasted no-op that drains the budget for nothing.)

**Re-runs compound; the breaker resets.** The BUI-535 circuit breaker is scoped to **one** `ebay-sold-comps` invocation, so every fresh `comic-fmv` call starts with a clean, untripped breaker — but the providers' rate limit and monthly quota do **not** reset. This is the part that misleads operators: a new run re-arms the safety, not the budget. Four runs in a row can each pay the breaker's 5-consecutive-error trip cost on the way down while the quota drains straight through.

**So don't chop a batch up, and don't probe-then-re-run.** Because the breaker is per-invocation, splitting 60 books into three runs of 20 re-arms the trip cost three times instead of paying it once. Run the full set in **one** `comic-fmv --batch` call and read the result. When investigating a suspected pricing problem, design that single pass to answer the question — a small probe followed by a large re-run is exactly what exhausts the budget.

> **Incident (2026-07-31):** ~107 book-queries across four runs in a few minutes — a 66-book batch, a 6-book re-run, a 5-book hypothesis test, then a 30-book fetch — tripped **both** providers' breakers. The last fetch returned `comps=0` for 27 of 30 books (the three with data were cache hits), and 30 books were dropped from the run.

**There is no `--max-workers` on `comic-fmv`.** That flag exists on `ebay-sold-comps`, which `comic-fmv` invokes with a fixed command line, so it cannot be set from this path — do not offer it to the user. Batch composition and `--force` discipline are the only levers here; concurrency is not one of them (and sold-comps.com is separately capped at 4 concurrent requests internally, so the knob would be near-inert for the default primary anyway).

**A budget-exhausted zero looks exactly like an illiquid book.** A breaker-tripped fetch returns `comps=0` — shape-identical to a genuine no-comps book. After any large or repeated run, check `breaker_tripped` before telling the user a book is illiquid, and watch stderr for `<provider> appears down — N consecutive errors, circuit breaker tripped (BUI-535)`. Same discipline as the `fetch-err` ≠ `n/a` rule above.

```bash
python3 -c "import json; r=json.load(open('<results.json>')); print(sum(1 for x in r if x.get('breaker_tripped')), 'of', len(r), 'rows affected by a tripped breaker')"
```

## Reading the output table

```
| # | Comic | Grade | FMV Range | Median | n | Window | CV | Confidence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | X-Men #31 (1967) | VF+ 8.5 | $100-175 | $135 | 9 | ±0.5 | 22% | HIGH | — |
| 4 | FF #63 (1967) | NM+ 9.6 | needs_manual | — | 5 | ±1.0 | n/a | — | manual_review=one_sided |
```

- **`needs_manual` reasons** — `too_sparse` (fewer than 2 comps survive IQR trim), `one_sided` (every comp sits on one side of the target grade, no bracket), `too_wide` (the pool brackets the target but spans more than 2.0 grade points), `variant_dropped` (BUI-588 — no comps existed until the book's `variant` term was dropped, so the pool prices the **base cover**, not that variant; the dropped text is named in the notes as `variant_dropped=<text>`, and unlike the other three this one clears by fixing/removing the input `variant`, not by `--force`). A flagged book still gets a linked, traceable comic stub (`comic_id`, `manual_review=<reason>` in notes) but emits **no bid-able number** — never invent one from the smeared/one-sided pool; hand-price via the math spec's §7/§7a or leave it for manual review.
- **`first_party=<count>` token** in `fmv_notes` — `fmv_comps`/`N` isn't purely a SerpApi count; it may fold in first-party comps from your own resolved WON/LOST auctions for that `(comic, grade)`. Check for this token before assuming `N` is all SerpApi.
- **CGC-proxy rows** — notes carry `CGC proxy: … n=<count> is graded-ladder comps, not raw-market depth`. Never read a proxy row's `N` as raw-market liquidity.

When presenting the table to the user, always surface: the window the pool was built at, N and CV, whether the book flagged `needs_manual` (and why) vs. auto-priced, whether grade-curve interpolation was applied, suspect comps (with reason), and a hot-market signal if the current bid already exceeds the computed Q75.

**Hot-market signal → flag only; never re-derive (BUI-530).** The signal fires when a live auction's current bid (from `/comic:identify`) already exceeds the computed Q75 (= `fmv.fmv_high`). The response is a **fixed rule, not a judgment call**: surface it to the user as a flag and leave `max_bid` exactly as `comic-fmv` computed it — **apply zero bid-factor adjustment**. Do **not** re-fetch comps and do **not** hand-rebuild or re-derive the comp pool to "justify" a bump — the pool `comic-fmv` already priced is the pool (the FF #16 run, 2026-07-16, burned ~11 tool calls re-deriving it by hand to nudge the bid factor 0.60→0.70, for zero change to the FMV). Bidding above the computed cap on a hot auction is the user's explicit call, never an automatic skill adjustment.

**When to dig into a pool.** Open `fmv.trimmed_pool` / `queries_used` only when (a) `flag_reason` is set (`one_sided`/`too_wide`/`too_sparse`), or (b) the table numerically contradicts the live bid (the hot-market signal above — and even then, dig only to *report*, per the rule above, never to re-price). A `LOW` confidence **alone is not a reason to dig** — LOW is already `comic-fmv`'s verdict on that pool; reproducing the pool by hand won't change it.

**`anchor_diverges` token → flag only; never re-derive (BUI-534).** `fmv_notes` may carry an `ungraded_anchor=$X (n=N raw)` token (the median of the grade-less comps the pool dropped, BUI-522) followed by `anchor_diverges=1` when the priced range sits far outside that anchor (e.g. Batman #251, 2026-07-24: pool $400–425 vs anchor $224.8 off 36 raw sales — the graded pool was pricing a different market than the bulk of raw trades). Same fixed rule as the hot-market signal above: surface it to the user as a flag and leave `fmv_low`/`fmv_high`/`max_bid` exactly as `comic-fmv` computed them. Do **not** re-fetch comps, re-derive the pool, or treat the anchor as a truer price — it's a coarse sanity/liquidity signal, not a repricing input. If the divergence looks wrong on inspection, that's a candidate for a hand-priced override (see the math spec), not a `comic-fmv` re-run.

## Save to DB

`comic-fmv` upserts each priced comic into the comics server's `comics` table automatically (`POST /api/comics`) right after computing FMV — this is the authoritative comic-metadata write `/comic:snipe-add` later links a bid to; no manual step is needed on the default path. See `docs/conventions/fmv-math-spec.md` for the manual `curl` equivalent and full field semantics (needed only if `comic-fmv` is unavailable).
