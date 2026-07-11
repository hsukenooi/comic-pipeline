---
name: comic:fmv
description: Calculate fair market value for a raw (ungraded) comic from eBay sold listings. Use when the user wants to price a comic, set a bid cap, or validate an auction's current price.
---

# Comic FMV

Compute fair market value from real eBay sold transactions. No multiplier math — just recent comps in the target condition.

## How to run

**Default path: `comic-fmv`.** It handles fetch (via `ebay-sold-comps`), cache, dedup, hard-excludes, grade parsing, IQR + quartiles, confidence rubric, self-exclusion, and DB upsert.

Before running, ensure `SERPAPI_KEY` and `COMICS_SERVER_URL` are set. If either is missing, source the canonical env file first:

```bash
set -a && source ~/Projects/comic-pipeline/apps/ebay/.env && set +a
```

`SERPAPI_KEY` lives in `~/Projects/comic-pipeline/apps/ebay/.env`. `COMICS_SERVER_URL` is machine-dependent — see the Server Health Check section below.

```bash
comic-fmv --batch <working_list.json> --out <results.json>
```

`--batch` JSON shape: `[{item_id, title, issue, year, publisher?, variant?, grade, grade_confidence?, locg_id?, locg_variant_id?, notes?}, ...]`

`publisher` and `variant` are optional but **load-bearing** (BUI-161): `ebay-sold-comps` appends `publisher` to the eBay search query — strongly recommended for non-Marvel/DC titles, where it's the primary noise filter that keeps trading cards / unrelated matches out of the comp pool — and `variant` (e.g. `Newsstand`, `Direct`) gives base vs variant editions distinct `comic_id`s (BUI-28), so omitting it conflates two sub-markets onto one comic.

`grade_confidence` (optional, `high`|`medium`|`medium-low`|`low` — **four** levels, BUI-162) is the photo-coverage confidence from `/comic:grade`. When present and low, it haircuts the max bid (see Step 6) — `medium-low` and `low` haircut **differently** (0.70 vs 0.60), so don't collapse them. Absent → standard 80% bid, no haircut (back-compat for seller-stated grades and manual runs).

Flags:
- `--max-age-days N` (default 7): reuse FMVs already in the comics server's DB if `fmv_updated_at` is within N days
- `--force`: bypass both the SerpApi cache and the DB cache and recompute everything
- `--version`: print the installed version plus the git SHA/date the binary was built from, then exit

The CLI prints a human-readable table to stdout and writes the full structured result to `--out`. Present the table to the user and carry the JSON forward to Step 4 of `/comic:buy`.

**Stale install risk (BUI-305):** `apps/fmv` is `uv tool install`-managed, not a workspace member kept current by `uv sync` — same category of risk as the eBay tools' stale-wrapper issue (BUI-27, documented in `scripts/install.sh` and `CLAUDE.md`). A `comic-fmv` binary that's behind the repo silently runs old pricing logic (missing safety guards, bugfixes, etc.) with no error to signal it. If pricing looks off, or after pulling changes to `apps/fmv/src/`, run `comic-fmv --version` and compare the git SHA/date to `git log -1 --format='%h %cd' --date=short` (the build hook stamps whole-repo HEAD, not just `apps/fmv/`, so compare against unfiltered HEAD — a path-scoped `-- apps/fmv/src` comparison will false-positive on every unrelated commit elsewhere in the monorepo); if they don't match, re-run `./scripts/install.sh`.

**`fetch-err` ≠ `n/a` (BUI-143):** a row whose FMV column reads `fetch-err` (and the loud post-table warning) means the **SerpApi fetch failed** for that book — quota exhausted or an outage — **not** that the book has no comps. Treat a `fetch-err` row (or a whole batch that comes back all `fetch-err`/`n/a`) as a SerpApi failure: check the `SERPAPI_KEY`/quota and re-run. Never tell the user these books are illiquid or bid on them as if priced.

**The rest of this file is the spec for the math the CLI implements** — read only when debugging the CLI, building a new consumer, or doing manual fallback computation.

---

## Manual fallback (only if the CLI is broken)

If `comic-fmv` is unavailable, you can run the steps below by hand. SerpApi access requires `SERPAPI_KEY`; canonical location is `~/.config/ebay-fetch/config.json`.

## Server Health Check

Before doing any research, verify the server is configured and up.

Resolve and health-gate the server through the **shared comics-server call
convention** (BUI-172, `docs/conventions/comics-server-call.md`) — don't
hand-roll URL resolution or the health check here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # COMICS_SERVER_URL (env var, hostname fallback)
comics_health_gate     || exit 1   # the server must answer
```

If either step fails, **stop immediately** — the comics server is unreachable or
the machine is unrecognised, so FMV data cannot be saved. Do not proceed with
any queries.

## Input

One or more comics, each with:
- Title + issue + year (e.g., "X-Men #31 1967")
- Target condition (e.g., "VF+ 8.5", "NM 9.2", "FN- 5.5")

## Query eBay Sold Listings

**Default query pattern (use this first):**

```bash
curl -s "https://serpapi.com/search.json?engine=ebay&_nkw=%22{title}+{issue}%22+{year}+-cgc+-cbcs+-graded+-slab&show_only=Sold&api_key=$SERPAPI_KEY"
```

**Quote the series title + issue number** (`"Spawn 98"`, `"Amazing Spider-Man 300"`). This is the primary noise-reduction technique — eBay's index otherwise matches "Spawn" and "98" independently, pulling in Curse of Spawn, Spawn trading cards, and unrelated issues. URL-encode the quotes as `%22`.

> ⚠️ **SerpApi gotcha — only `show_only=Sold` triggers the sold filter.**
> Despite eBay's URL syntax, SerpApi's eBay engine **silently drops** the `LH_Sold=1` and `LH_Complete=1` params if you pass them directly. The only param that gets translated through to the eBay search is `show_only=Sold` (which sets `LH_Sold=1` server-side). After your first query, **verify** that `data["search_metadata"]["ebay_url"]` contains `LH_Sold=1` — if it doesn't, you're looking at active listings, not sold ones, and the FMV will be wrong (typically far too low).

**Sanity check:** if the median price for a non-junk book in the target grade comes back implausibly low (e.g. <$5 for a Bronze-Age VF), suspect that the sold filter didn't apply. Re-verify the eBay URL before trusting the number.

- Always exclude graded copies with `-cgc -cbcs -graded -slab` — they sell at very different prices
- **Do not include "raw" as a keyword** — most sellers don't use it and it drops comps to near zero
- **For non-Marvel/DC publishers (Image, Dark Horse, Valiant, etc.), add the publisher name to the query.** Titles like "Invincible", "Spawn", "Saga" match sports cards, trucks, and trading card sets. Adding `image+comics` or `dark+horse` scopes results to actual comics and prevents FMV contamination.

**Tiered query strategy** (the CLI does this automatically; replicate it manually if falling back):

1. **Base** — always run: `"{title} {issue}" {year} {publisher_if_indie} -cgc -cbcs -graded -slab` + `show_only=Sold`. Dedupe by `product_id`.
2. **Auto-broaden** — only if base returns <5 total results: re-run without the year (`"{title} {issue}"` only). Common for thin-trade modern keys and oddball one-shots.
3. **Grade-targeted** — only if base returns <10 grade-tagged comps after parsing: add a grade-label query (`"{title} {issue}" VG` or `FN` etc.). For Silver/Bronze keys this surfaces extra comps; for Copper-and-newer it almost always overlaps the base query and is wasted spend.

Skip tiers 2 and 3 by default — they're conditional, not always-on.

**Self-exclusion (best-effort — BUI-160):** if an active auction is being priced, drop any comp whose `item_id` matches the listing being valued. **Caveat:** comps are keyed by SerpApi `product_id`, which is a *different identifier namespace* from the eBay `item_id` the batch carries — so in the automated `comic-fmv --batch` path the seeded `item_id` usually won't match any comp's `product_id`, and a re-listed self-auction can survive into the comp pool and mildly self-bias the FMV upward. Self-exclusion is therefore reliable only when SerpApi happens to surface a matching `item_id` (or when you pass the actual SerpApi `product_id`); IQR trimming + the 80% bid haircut bound the residual bias. Don't rely on it as a hard guarantee.

**Parse results:**

```bash
curl -s "..." | python3 -c "
import json,sys
data=json.load(sys.stdin)
for r in data.get('organic_results',[]):
    p=r.get('price',{})
    print(f\"  {p.get('raw','?'):>10}  {r.get('sold_date',''):>15}  {r.get('title','')[:75]}\")
"
```

## Filter Results

Not every result is a valid comp. Three tiers:

### Hard exclude (drop entirely)

**Deterministic identity excludes — via `comic-identify` (BUI-253):** when filtering
manually (not running the `comic-fmv` CLI, which applies its own hard-exclude regex —
see note below), pipe each candidate title through the canonical title-parser instead of
eyeballing it for lot/reprint/foreign-edition/trading-card keywords:

```bash
comic-identify "Amazing Spider-Man #48-50 Lot"
# {"is_lot": true, "constituent_issues": ["48","49","50"], ...}
```

Drop the comp if:
- `"is_lot"` is `true` — lots / multi-issue bundles (`"lot"`, `#48-50`, `#15 & #16`,
  comma/slash/dash chains, "N through M" — every BUI-261 format is already handled).
- `"edition"` is `"facsimile"` or `"reprint"` — facsimile editions, Marvel Tales/True
  Believers reprints, 2nd printings, "retold" editions.
- `reject_reasons` contains `"foreign-language/-market edition"` — La Prensa / Spanish-
  language reprints. **Narrower than the classic FMV markers below** — see the Foreign
  editions (supplement) bullet.
- `reject_reasons` contains `"trading card / TCG product"` — trading cards, Upper Deck,
  Fleer, etc.

**Manual/condition excludes — not identity concerns, no CLI for these:**

- **Coverless / damaged structurally** — "coverless", "no cover", "cover torn", "cvr off", "detached"
- **Missing content** — "missing pin-up", "missing wrap", "missing pages", "non story page missing"
- **Foreign editions (supplement)** — "rare uk", "rare brazil", "rare mexico", "norway", "australia", "italian", "spain", "ebal", "pence", "9d variant" — `comic-identify`'s foreign-edition lexicon (La Prensa/Spanish-focused) doesn't cover these yet; keep checking for them manually until BUI-253 widens it
- **Wrong volume** — "vol 2/3/4/5/6/7", later-run issues with same number (e.g., for ASM #5 1963 exclude `#75 vol 5`, `#288`). `comic-identify` reports a `volume` field per title, but there's no wish item here to compare it against — use judgment against the target book's known volume/year
- **Other graders** — "psa", "pgx" (alongside the existing `-cgc -cbcs -graded -slab`)
- **McFarlane Toys / figures** — "1:6 scale", "collectible figure", "action figure"
- **Premium-distorting** — "signed by", "stan lee" (autograph), "Signature Series"
- **WW Live Sale results** — titles starting with "WW LIVE SALE" swing erratically
- **Junk listings** — "space filler", "single panel", "production acetate"
- **Restored copies / waterstain** unless target is also in that state

**Note:** the default `comic-fmv` CLI path applies its own hard-exclude regex
(`apps/ebay/src/sold_comps.py`, `HARD_EXCLUDE_RE`) automatically — you only need the
`comic-identify` calls above when filtering comps by hand (debugging the CLI, or a
manual fallback run with no `comic-fmv` available). That regex independently duplicates
some of the same lot/reprint/foreign/trading-card detection `comic-identify` now
canonicalizes; consolidating it is tracked as a BUI-253 follow-up, not done here.

### Suspect (flag, manual review — do NOT auto-include in FMV)

Comps that pass IQR filter but are clearly inconsistent with the grade curve. Examples:
- A `VG 4.0` selling for $2500 in a market where 4.0 typically sells for $800
- A `GD 2.0` selling for $1300 in a market where 2.0 typically sells for $400
- A `FN+ 6.5` raw selling for $242 when graded 6.5 copies sell for $3000

These are usually heated bidding, mis-listed grades, or graded copies whose slab keywords are missing from the title. Flag them in output but exclude from the FMV computation. If the user disagrees, they can override.

### Keep

Single-issue, unrestored, US first-print, raw comps in the target condition neighborhood.

## Compute FMV Range

### 1. Parse grades from titles

Order matters — match most-specific first:

```
Numeric: \b([0-9]\.[02-9])\b   (e.g., "4.5", "(5.0)", "VG 4.0", "9.2", "9.4", "9.6", "9.8", "9.9")
Letter (specific → general):
  vg/fn+ → 5.5    fn/vf → 7.0    vf/nm → 9.0
  vg/fn  → 5.0    fn-   → 5.5    vf-   → 7.5
  vg+    → 4.5    fn+   → 6.5    vf+   → 8.5
  vg-    → 3.5    fn    → 6.0    vf    → 8.0
  vg     → 4.0    nm-   → 9.2    nm/m  → 9.6
  gd/vg  → 3.0    nm    → 9.4    nm+   → 9.6
  gd+    → 2.5
  gd     → 2.0
  fr/gd  → 1.5
  fr     → 1.0
  poor   → 0.5
```

Treat seller-grade `F` (loose "Fine") as suspect — sellers often misuse it for both Fair and Fine.

### 2. Bucket comps by parsed grade

Build a grade → [prices] map. Compute median per bucket.

### 3. Build the comp pool (progressive widening + honesty guards)

Start at ±0.5 grades from target and widen in 0.5 steps (±0.5 → ±1.0 → ±1.5 → ±2.0) until you have ≥5 grade-bearing comps or hit the ±2.0 ceiling. The `comic-fmv` CLI does this automatically; `--grade-window <n>` raises or lowers the ceiling without bypassing the guards below.

After widening, a pool is **not** auto-priced — it is flagged `needs_manual` — when any of these hold (precedence: sparse → one-sided → too-wide):

- **`too_sparse`** — fewer than 2 comps survive IQR trim. A lone comp is not a price.
- **`one_sided`** — every comp sits strictly above OR strictly below the target grade (no bracket). Widening only reaches one direction, so the estimate would be biased (e.g. a NM+ 9.6 target whose comps top out at 9.0 — widening only drags it *down*). This is the case for hand-pricing via §7/§7a, not an automated number.
- **`too_wide`** — the pool brackets the target but spans more than 2.0 grade points (e.g. comps at 5.0 and 9.0 for a 7.0 target). The median of a grade-smeared pool is meaningless because price is monotonic in grade.

A flagged book emits no bid-able number; `comic-fmv` still writes the linked comic stub (so it's traceable) with `manual_review=<reason>` in the notes. **When you see `needs_manual`, either hand-price it via grade-curve interpolation (§7) or the CGC proxy (§7a), or leave it for manual review — do not invent a number from the smeared/one-sided pool.** Only a bracketed, bounded, ≥2-comp pool auto-prices.

### 4. IQR outlier removal

On the chosen pool, drop values outside `Q1 − 1.5×IQR` to `Q3 + 1.5×IQR`. Don't eyeball — use the math.

**Quartile method:** use `statistics.quantiles(data, n=4, method='inclusive')` for both IQR trim and the FMV range step below. The default Python method (`'exclusive'`) places quartiles between data points and over-dilates IQR on small samples (n=5 IQR can be ~10× the data spread), which lets clear outliers survive trimming. Inclusive method matches Excel's `QUARTILE.INC` and behaves predictably for the small comp pools (5–15 points) we typically see.

### 5. Sanity-check the grade curve

Bucket medians should rise monotonically with grade. If 4.0 median > 4.5 median, something's wrong — re-examine the data (likely a damaged 4.0 or graded 4.5 leaked through).

### 6. Compute FMV range

- **Median** = median of trimmed pool
- **FMV range** = Q25 to Q75 of trimmed pool (same `method='inclusive'` as the IQR step), rounded to clean numbers (`$25` step above $200, `$10` step from $50–$200, `$5` step below)
- **Max bid** = `bid_factor` × FMV high (round to clean number). `bid_factor` is `0.80` by default. When `grade_confidence` is supplied (photo grade), the haircut takes the **more conservative** of the grade confidence and the comp confidence and lowers the factor: MEDIUM-LOW combined → `0.70`, LOW combined → `0.60`. This is why a thinly-photographed comic bids below 80% of FMV — the bid reflects how sure we are of the grade, not just the price.

### 7. Grade-curve interpolation when direct comps are sparse

If target grade has <3 direct comps, interpolate linearly between bracketing grade-bucket medians:

```
target_price = median_below + (target_grade − grade_below) / (grade_above − grade_below) × (median_above − median_below)
```

State explicitly that interpolation was used and confidence is reduced.

### 7a. CGC Proxy Fallback (high-value keys with sparse raw comps)

**Trigger — both conditions must be true:**
1. Raw eBay comps in the target grade window: n < 3 after filtering
2. Estimated book value: > $200

Below $200 the two markets diverge too much (certification cost is proportionally too large, raw buyers discount heavily). Above $200 the cost of CGC submission (~$50–150) is small relative to value, so rational buyers pay near-CGC prices for clean raw copies — making CGC realized prices a reliable anchor.

**Why this beats grade-curve interpolation for keys:**
Raw eBay comps for keys are sparse and noisy (sellers misgrade, titles lack condition info). CGC Heritage/GoCollect sales are grade-certain and drawn from the same buyer pool. For high-value keys, CGC data is more representative of true demand than a handful of raw eBay listings.

**Step 1 — Find CGC realized prices**

Run Google SerpApi queries targeting Heritage Auctions and Key Collector:

```bash
# Heritage Auctions realized prices
curl -s "https://serpapi.com/search.json?engine=google&q={title}+{issue}+{year}+CGC+{grade}+realized+heritage+auctions&api_key=$SERPAPI_KEY"

# Key Collector Comics Hot 10 or averages
curl -s "https://serpapi.com/search.json?engine=google&q={title}+{issue}+{year}+CGC+average+price+key+collector+comics&api_key=$SERPAPI_KEY"

# GoCollect
curl -s "https://serpapi.com/search.json?engine=google&q={title}+{issue}+{year}+CGC+{grade}+raw+value+gocollect&api_key=$SERPAPI_KEY"
```

Extract realized prices from snippets. Target the grade nearest your raw target (within ±0.5) and bracket grades above and below for interpolation if needed.

**Step 2 — Apply raw discount**

| Estimated value | Raw discount |
|---|---|
| > $500 | 10–15% below CGC equivalent |
| $200–$500 | 15–25% below CGC equivalent |
| < $200 | Do not use this method |

Use the midpoint of the range by default. Adjust toward the tighter end (10% / 15%) if the book is a major key with active raw demand; toward the wider end (15% / 25%) if seller grading is unreliable for this title or condition is uncertain.

**Step 3 — State the result clearly**

- Label the FMV as "CGC proxy" in the Notes column
- State the CGC source price, the grade, and the discount applied
- Cap confidence at MEDIUM-LOW regardless of how many CGC comps you found — the discount estimate itself introduces irreducible uncertainty

### 8. Confidence rubric

| n (trimmed pool) | CV | Confidence |
|---|---|---|
| ≥8 | <25% | HIGH |
| ≥6 | <30% | HIGH |
| ≥5 | <35% | MEDIUM-HIGH |
| ≥4 | <45% | MEDIUM |
| ≥3 | any | MEDIUM-LOW |
| <3 | — | LOW |

Where `CV = stdev / median`. State which window the pool was built at.

**Wide-window cap:** a pool built at a window **wider than ±1.0** caps at MEDIUM confidence regardless of n and CV — a pool stitched together across ±1.5–±2.0 of grade can't claim HIGH/MEDIUM-HIGH no matter how many comps it has. (A genuinely thin or grade-mixed pool is flagged `needs_manual` per §3 and emits no number at all; the cap applies only to pools that still price.)

### 9. Within-grade adjustments

Note these in `fmv_notes` but don't bake into the number unless the target page-quality / variant is known:

- **Page quality**: "OW pages" (off-white) sells materially higher than "tan/cream" at same grade
- **Newsstand vs direct**: distinct sub-markets — match target variant, don't blend
- **Eye appeal**: a "nice" 4.0 with bright cover commonly outsells a dull 4.5

### 10. Real-time signal: current bid

For an active auction with 30+ bids that has already crossed your computed Q75, treat that as evidence the market disagrees with the comps (key is hot, comps stale, or target grade undershot). Surface it — don't quietly bid above your discipline.

## Caching layers

Two caches insulate this skill from SerpApi's 250/month free tier and from re-running compute we already did. The CLI handles both automatically; the manual fallback should respect them.

1. **SerpApi response cache (`ebay-fetch sold-comps`)** — cache key `sha256(canonical_query_url)`, stored at `~/.cache/ebay-sold-comps/<sha>.json`, TTL 7 days. eBay sold prices for older books move slowly; one fresh fetch per book per week is plenty. Bypass with `--force`.
2. **DB FMV cache (the comics server's `comics` table)** — before any SerpApi call, look up the existing row by `(locg_id, grade)` and reuse if `fmv_updated_at` is within `--max-age-days N` (default 7). Bypass with `--force`. The `POST /api/comics` endpoint always touches `fmv_updated_at` on FMV-field updates, so the freshness check is reliable.

Manual fallback: skip these unless you're explicitly recomputing — re-running by hand spends API calls that the CLI would have served from cache.

## Output

```
| # | Comic | Grade | FMV Range | Median | n | Window | CV | Confidence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | X-Men #31 (1967) | VF+ 8.5 | $100-175 | $135 | 9 | ±0.5 | 22% | HIGH | — |
| 2 | ASM #5 (1963) | VG+ 4.5 | $1100-1300 | $1240 | 1 + curve | ±0.5 | n/a | LOW | Single direct 4.0 OW comp; interpolated |
| 3 | MS #5 (1972) | VG 4.0 | $575-650 | $610 | CGC proxy | n/a | n/a | MEDIUM-LOW | CGC proxy: Heritage 4.0 avg $658; 10% raw discount |
| 4 | FF #63 (1967) | NM+ 9.6 | needs_manual | — | 5 | ±1.0 | n/a | — | manual_review=one_sided — comps top out at 9.0; hand-price or skip |
```

Always include:
- The window the pool was built at
- N and CV
- Whether the book was flagged `needs_manual` (and the reason: `one_sided` / `too_wide` / `too_sparse`) vs. auto-priced
- Whether grade-curve interpolation was applied
- Suspect comps flagged (with reason)
- Hot-market signal if current bid > Q75

## Save to DB

Upsert each comic into the `comics` table immediately after computing FMV. This is the authoritative step for comic metadata — `/comic:snipe-add` links bids to these records, not the other way around.

```bash
curl -s -X POST $COMICS_SERVER_URL/api/comics \
  -H "Content-Type: application/json" \
  -d '{
    "title": "X-Men",
    "issue": "31",
    "year": 1967,
    "grade": 8.5,
    "fmv_low": 100,
    "fmv_high": 175,
    "fmv_comps": 9,
    "fmv_confidence": "high",
    "fmv_notes": "OW pages copies excluded",
    "locg_id": 1234567
  }'
```

- `title` — series name only, no issue number (e.g. `"Amazing Spider-Man"`, not `"ASM #15"`)
- `issue` — issue number as string; use a range for lots (e.g. `"337-339"`)
- `grade` — numeric only (e.g. `8.5`); omit or `null` if unknown
- `fmv_confidence` — must be `"high"`, `"medium"`, or `"low"`
- `fmv_comps` — **(BUI-286)** the comp-pool count `N`. This is no longer a pure SerpApi/eBay-sold count: it may include first-party comps folded in from your own resolved auctions (WON and LOST outcomes for this `(comic, grade)`, pulled from the comics server). If any of `N` came from your own auctions, `fmv_notes` carries a `first_party=<count>` token — check there to see the SerpApi-vs-first-party split rather than assuming `fmv_comps` is all SerpApi.
- `fmv_flag_reason` — **(BUI-132)** set to the `needs_manual` reason (`"one_sided"`, `"too_wide"`, or `"too_sparse"`) when the book was flagged (§3); omit/`null` for an auto-priced book. This is now a **structured column**, not just a `manual_review=<reason>` token in `fmv_notes`. Posting it makes the row first-class `needs_manual`: `/comic:verify` reports it as `needs_manual` (not `fmv_stub`), and the upsert **clears any previously-cached price** for that comic+grade so a book that later flags can't keep a stale auto-priced number. (A plain n=0 no-comps stub posts with `fmv_flag_reason` omitted and so never wipes a real price.)
- `locg_id` — from the LOCG ID resolved in `/comic:collection-check`; omit entirely if unresolved (don't pass null)
- `locg_variant_id` — include only if a separate variant entry was found on LOCG

Confirm the `id` returned — that's the `comic_id` that will be linked to the bid.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Passing `LH_Sold=1` / `LH_Complete=1` to SerpApi | SerpApi's eBay engine drops them silently — use `show_only=Sold`. Verify by grepping the returned `search_metadata.ebay_url` for `LH_Sold=1` |
| Mixing quartile methods between IQR and FMV range | Use `statistics.quantiles(method='inclusive')` for both. The default `'exclusive'` over-dilates IQR on small samples and lets outliers survive |
| Numeric grade regex `\b([0-9]\.[058])\b` (old) | Use `\b([0-9]\.[02-9])\b` — the old form silently dropped 9.2/9.4/9.6/9.9 comps |
| Stating "Medium" confidence by feel | Use the rubric: n + CV decide it |
| Forcing a number out of a one-sided or grade-smeared pool | If the pool doesn't bracket the target or spans >2.0 grades, it's flagged `needs_manual` — hand-price via §7/§7a or skip, don't report the smeared median |
| Treating a `needs_manual` row like a no-comps row | A flagged book still has a linked comic stub (`manual_review=<reason>` in notes) and a real `comic_id` — it shows as `manual:<reason>` in the table, not `n/a` |
