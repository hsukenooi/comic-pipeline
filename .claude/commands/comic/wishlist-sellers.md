---
name: comic:wishlist-sellers
description: Discover eBay sellers holding two or more books from your wish list — combine-shipping candidates. Fans out a keyword search across the entire wish list, groups results by seller, and surfaces only sellers with ≥2 genuine un-owned unseen matches. Designed to run unattended on a recurring schedule.
---

# Comic Wishlist Sellers

Fan out a keyword search across every wish-list item, group the results by eBay seller, and surface only sellers holding **two or more** genuine, un-owned, unseen matches — so you can buy several books from one seller and save on combined shipping.

This is the **inverse of `/comic:seller-scan`**: instead of checking one known seller against your wish list, this skill *discovers* sellers (including unknown ones) by searching eBay at scale. It is designed to run **unattended on a recurring schedule**; wall-clock time is not the goal (a cold run is ~20+ min at eBay's pacing recommendation), but re-runs are cheap because search results are cached for 7 days and verified listings are cached indefinitely.

The skill is **read-only against your collection and wish list** — it writes only to the seen-tracking store and local caches.

## Prerequisites

**`COMICS_SERVER_URL` must be set.** The script fetches your wish list over HTTP and hard-fails if the server is unreachable. Set it once in `~/.zshrc`:

```bash
# MacBook (connects to Mac Mini over Tailscale)
export COMICS_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080

# Mac Mini (running locally)
export COMICS_SERVER_URL=http://localhost:8080
```

`GIXEN_SERVER_URL` is a deprecated alias — it still works but emits a warning. Migrate to `COMICS_SERVER_URL`.

**The `wishlist-sellers` console script must be on PATH.** After pulling this feature, re-run `./scripts/install.sh` to install it:

```bash
cd ~/Projects/comic-pipeline && ./scripts/install.sh
which wishlist-sellers   # should resolve
```

## Run the scan

**Installed console script** (after `./scripts/install.sh`):

```bash
wishlist-sellers                 # human table, progress to stderr
wishlist-sellers --json          # compact JSON for programmatic use
```

**Dev form** (no install required):

```bash
cd ~/Projects/comic-pipeline/apps/ebay && uv run wishlist-sellers
# or
cd ~/Projects/comic-pipeline/apps/ebay && .venv/bin/python src/wishlist_sellers.py
```

Progress (item count, cache hit/miss, match counts, verify counts) prints to **stderr**. Suppress it if you want only the table:

```bash
wishlist-sellers 2>/dev/null
```

**Environment flag** (rare — overrides the config file default):

```bash
wishlist-sellers --env production    # force the production eBay endpoint
wishlist-sellers --env sandbox       # force sandbox
```

## Only new finds by default

The script shares the **global seen set** (`/api/comics/seller-scan/seen`, keyed by `item_id`) with `/comic:seller-scan`. A listing that was surfaced by either tool — regardless of which seller it was grouped under — is recorded as seen and will not re-appear in future runs.

- A re-run on the same day shows only listings that appeared *after* the previous run.
- The seen set is stored on the comics server, so running from either the MacBook or the Mac Mini respects the same memory.
- There is no `--all` override (unlike `/comic:seller-scan`). The seen set is the primary incremental mechanism; clearing it is a server-side operation if you ever need a full reset.

Because new listings flow in continuously, a weekly scheduled run is typically enough to catch everything before auctions end.

## Output

The script emits a table grouped by seller. Each seller block lists its opaque seller ID, the total match count, and one row per matched listing:

```
Seller: a4k92xbp7... (3 matches)
  Wish Item                  Listing Title                            Price      Ends             URL
  ─────────────────────────────────────────────────────────────────────────────────────────────────────
  Amazing Spider-Man #129    AMAZING SPIDER-MAN 129 1ST PUNISHER …   $299.99    2026-07-03 …     https://www.ebay.com/itm/…
  Fantastic Four #48         FANTASTIC FOUR 48 VF SILVER SURFER …    $450.00    2026-07-04 …     https://www.ebay.com/itm/…
  X-Men #94                  X-MEN 94 BRONZE AGE KEY NM- …           $185.00    2026-07-05 …     https://www.ebay.com/itm/…

Seller: m9zr3pq1y... (2 matches)
  …
```

**eBay seller IDs are opaque** (US sellers since late 2025 — see R13 in the plan). The script groups by the stable immutable ID; to see a readable seller name or store, open any listing link and look at the seller profile. v1 does not resolve human-readable handles.

Progress counts (wish-list size, cache hits/misses, match counts before/after each filter stage, Haiku verify count, final seller count) all go to stderr so the stdout table stays clean for piping.

When run as a skill, present the per-seller blocks clearly so the user can decide which seller to pursue for a combined purchase. Note the ending times so they can prioritize sellers whose auctions close soonest.

## Scheduling and recurring runs

`/comic:wishlist-sellers` is designed to run on a **recurring schedule** — daily or weekly — and notify you only when new multi-match sellers are found. An empty result is always silent.

### Why re-runs are cheap

Three layers make steady-state runs near-free:

1. **7-day eBay search cache** — keyword search results are stored under `~/.cache/wishlist-sellers/`. A second run within the week skips all eBay calls for items whose cache is still fresh; only new or expired items hit the API.
2. **Verdict cache** — Haiku's "is this really the book?" verdict for each `(listing_id, wish-item)` pair is stored in a SQLite DB at `~/.cache/wishlist-sellers/verdicts.db`. A listing that survived the first run's verify step is not re-verified; neither is a listing that was rejected. On a warm re-run, Haiku is called only for listings that appeared since the last run.
3. **Seen-item filter** — listings already surfaced to you are dropped before grouping and before verify, so they contribute zero LLM cost and zero output noise.

A typical weekly re-run does: full cache hit on searches → zero eBay calls → a small verify pass on new listings only → output only if a seller crosses the ≥2 threshold with new material.

### Setting up a recurring run

**Option A — `/schedule` cloud agent (recommended for unattended notification):**

Ask Claude to schedule this as a recurring cloud agent:

```
/schedule
Run /comic:wishlist-sellers every Sunday at 9 AM. Notify me only if sellers are found.
```

The cloud agent runs `wishlist-sellers` on the Mac Mini (where `COMICS_SERVER_URL=http://localhost:8080` is already set), captures the output, and delivers a completion notification when the run finishes. An empty result (no sellers with ≥2 matches) is silent; a non-empty result surfaces the full per-seller table in the notification.

**Option B — local cron** (if you prefer a local trigger):

```bash
# Run every Sunday at 9 AM; notify via terminal-notifier on non-empty output
0 9 * * 0 COMICS_SERVER_URL=http://localhost:8080 wishlist-sellers 2>/dev/null \
  | tee /tmp/wishlist-sellers-last.txt \
  | grep -q "Seller:" && terminal-notifier -title "Wish List Sellers" \
      -message "$(wc -l < /tmp/wishlist-sellers-last.txt) lines — check terminal"
```

Adjust the URL and notification command to match your machine and preferred alerting tool.

### Notification behavior

- **Non-empty result** → notify. The per-seller table is the notification payload when run via a cloud agent; route it to whatever channel reaches you (Slack, push notification, email).
- **Empty result** → silent. No sellers with ≥2 matches means nothing actionable; the run exits 0 with no output.

The script itself does not push notifications — it only writes to stdout/stderr. The scheduling layer (cloud agent or cron wrapper) is responsible for detecting non-empty output and routing it.

## After you find a seller

`/comic:wishlist-sellers` is **discovery only** — it does not place bids or snipes. When you find a seller worth pursuing:

1. Open one of their listing links to see the seller's storefront and confirm combined shipping is offered.
2. Pick the listings you want and pass their eBay URLs to `/comic:buy`. The buy workflow will identify each comic, check your collection, calculate FMV, and add snipes.

Run the listings from one seller sequentially through `/comic:buy` — Gixen sessions are stateful and parallel snipe-adds fail.

## Common mistakes

| Mistake | Fix |
|---|---|
| Wish-list books without an issue number never appear | Items like "Secret Wars HC" or "Watchmen TPB" have no `#N` in the name and are silently skipped by the matcher. This is by design — they are not searchable as individual issues. They will never surface here regardless of how many sellers carry them. |
| Treating the opaque seller ID as a contactable handle | The ID (e.g. `a4k92xbp7…`) is an immutable internal identifier, not a store name or username. Open any listing link to see the seller's profile and contact them. |
| `COMICS_SERVER_URL is not set` error | The script hard-fails before fetching the wish list. Set `COMICS_SERVER_URL` in `~/.zshrc` (MacBook → `http://mac-mini.tail9b7fa5.ts.net:8080`; Mac Mini → `http://localhost:8080`) and re-run. |
| Expecting the skill to place bids or snipes | It is discovery and reporting only. Take the listing URLs from the output and hand them to `/comic:buy` yourself. |
| `wishlist-sellers: command not found` | The console script was not installed. Re-run `./scripts/install.sh` from the repo root after pulling this feature — `uv tool install` on `apps/ebay` is what puts it on PATH. |
| 409 from the collection-check endpoint | The collection was never imported on this server. Run the LOCG import flow (`/comic:collection-add`) before re-running — the script hard-fails on a 409 rather than treating ownership as "unknown" (that would surface books you already own). |
| First run seems to hang | A cold first run searches eBay for every matchable wish-list item (~685 items at ~2 s each = ~20 min). This is expected. Progress prints to stderr — run without `2>/dev/null` to watch the cache-fill. Subsequent runs are far faster. |
| Re-run shows no new sellers even though auctions changed | Check that the 7-day search cache has not expired and the seen-item filter is not hiding results from a prior run. To debug, confirm `COMICS_SERVER_URL` is set and check `~/.cache/wishlist-sellers/`. |

---

Plan: `docs/plans/2026-06-26-001-feat-multi-seller-wishlist-scan-plan.md` — BUI-221.
