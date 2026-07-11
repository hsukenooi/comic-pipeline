---
name: comic:seller-scan
description: Scan an eBay seller's active listings and surface any that match your LOCG wish list. Use when you want to check if a specific seller has comics you're looking for.
---

# Comic Seller Scan

Fetch all active listings from an eBay seller and fuzzy-match them against your LOCG wish list. Outputs a match table you can feed directly into `/comic:buy`.

## Input

A store name, a username URL, or a raw login username. **An eBay store name is
not the same as the seller's login username** — the Browse API only filters by
login username, so store names are resolved through an alias map committed in
the repo at `apps/ebay/src/seller_aliases.json` (BUI-68). It ships with the
code, so there's nothing to set up per machine; `--add-alias` edits this
tracked file, so commit it after adding a new seller.

- `beatlebluecat` — bare name; resolved via the alias map (seeded names map to themselves)
- `https://www.ebay.com/usr/<username>` — trusted login username
- `https://www.ebay.com/sch/i.html?_ssn=<username>` — the `_ssn` value is the login username
- `tunerscomics --username tuners_comics_2011` — pass a known username directly (one-off)
- `tunerscomics --add-alias tuners_comics_2011` — register the username, then scan

**Finding a seller's username:** open one of their listings, click **"See other
items"**, and copy the `_ssn=` value from the resulting URL — that's the login
username the filter needs. (The public `/str/<slug>` store URL is *not*
guaranteed to be the username.)

If you scan an unknown store name, the run aborts with these instructions rather
than silently returning every seller's listings (in a multi-seller batch, only
that seller's slot is affected — see "Scanning multiple / known sellers" below).

**Multiple sellers:** pass 2+ of the above as separate positional args to scan
them all in one invocation (BUI-298) — see "Scanning multiple / known sellers".

## Run the scan

```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller-username-or-url>
```

If the venv doesn't exist yet:
```bash
cd ~/Projects/comic-pipeline/apps/ebay && python3 -m venv .venv && .venv/bin/pip install -e . -q
```

For JSON output (useful for piping to `/comic:buy`):
```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller> --json
```

**Scan multiple sellers in ONE invocation** (BUI-298) — pass them all as
positional args to a single call. This fetches the wish list + OAuth token
ONCE and loops internally, instead of redoing both per seller:

```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller1> <seller2> <seller3> --json
```

`--username`/`--add-alias` only make sense for exactly one seller (they'd be
ambiguous across a batch) — the script refuses (exit 2) if you pass either
alongside 2+ sellers.

## Scanning multiple / known sellers (BUI-298)

When the user asks to scan several sellers, or to scan "all known sellers",
pass **every** seller as a positional arg to **one** `seller_scan.py`
invocation in a **single Bash tool call** — do **not** spawn a `Task`/`Agent`
subagent per seller.

- Pull the store names straight from `apps/ebay/src/seller_aliases.json`'s
  keys when the user says "scan all known sellers."
- **Why not subagents:** each seller's scan is just one deterministic
  `seller_scan.py` invocation — there's no reasoning step a subagent adds.
  Fanning out one Agent subagent per seller previously meant N separate LLM
  reasoning loops, N idle-notifications, and manual aggregation of N
  free-text reports. Worse, it's unreliable: a subagent asked to retry a
  scan (after a verification timeout) once returned a stale/hallucinated
  duplicate of its first report instead of actually re-executing. A
  deterministic Bash re-run of the batched script cannot fabricate a
  result — it either ran and produced real output, or it didn't run.

```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py beatlebluecat blissard comichunterlv \
    comics4less davesvintagecomics hodagent ka-761233 punkscrapscomics \
    tunerscomics --json
```

## Only-new-matches by default (BUI-113)

By default the scan **hides wish-list matches it has already surfaced in a prior
run**, so a repeat scan shows only listings you haven't seen before. The seen
set is owned by the comics server (`/api/comics/seller-scan/seen`), so the
MacBook and Mac Mini share one memory — scanning the same seller from either
machine won't re-surface the same matches.

- Only the genuine **matches** are recorded as seen (a handful of item_ids per
  run, not every listing).
- `--all` shows every match again, including already-seen ones. It still records
  newly-surfaced matches — `--all` means "show me everything," not "forget."

```bash
.venv/bin/python src/seller_scan.py <seller>          # only new matches
.venv/bin/python src/seller_scan.py <seller> --all    # every match
```

Seen-tracking is **best-effort**: if the server is unreachable, the scan warns
and shows all matches rather than aborting (a duplicate is harmless; silently
hiding a real match is not). This is deliberately *not* the wish-list's
hard-fail behavior.

## Output

**Human-readable (default):**
```
Listing Title                             Wish List Item               Price      Ends          URL
--------------------------------------------------------------------------------------------------------
AMAZING SPIDER-MAN #300 NM Marvel 1988…  Amazing Spider-Man #300      $299.99    2026-05-28…   https://www.ebay.com/itm/…
FANTASTIC FOUR #48 VF+ Silver Surfer …   Fantastic Four #48           $450.00    2026-05-29…   https://www.ebay.com/itm/…
```

With 2+ sellers, each gets an `=== <seller> (<username>) ===` header before
its table. A single-seller run prints the same bare table as always (no
forced header).

Progress info (listing count, match count) prints to stderr. Redirect to suppress:
```bash
seller_scan.py <seller> 2>/dev/null
```

**`--json` (BUI-298 — always a top-level object, never a bare array):**
```json
{
  "incomplete": false,
  "sellers": [
    {
      "seller": "comics4less",
      "username": "comics4less",
      "matches": [ { "title": "...", "wish_name": "...", "item_id": "...", "...": "..." } ],
      "dropped_candidates": [],
      "filtered": [ { "item_id": "...", "title": "...", "wish_name": "...", "reason": "..." } ],
      "incomplete": false,
      "error": null
    }
  ]
}
```

**Parse exit-code-first, then drill in:**

| Exit code | Meaning | What to do |
|---|---|---|
| `0` | Every seller scanned cleanly | Read `sellers[*].matches` as usual |
| `1` | **Global verifier failure** — the `claude` CLI is missing/unauthenticated, or every chunk failed transport. The run is truncated: the failing seller's slot has `error: "claude verifier globally unavailable ..."`, sellers after it were not attempted | Fix the `claude` CLI/auth and re-run the whole batch |
| `2` | At least one seller couldn't be resolved/fetched, but none was incomplete | Check each `sellers[*].error` — sellers with `error: null` still have usable `matches` |
| `3` | At least one seller was **incomplete** (`sellers[*].incomplete: true`, top-level `incomplete: true`) — some candidates for that seller were NEVER verified | Surface the `INCOMPLETE` stderr banner; re-run to pick up the never-verified candidates (they were NOT marked seen, so they'll resurface) |
| `2` (usage) | An **argparse usage error** (e.g. `--username`/`--add-alias` with 2+ sellers) — the message goes to **stderr** and **no JSON is emitted** | Fix the invocation and re-run |

Priority when a batch hits several conditions: **1 > 3 > 2 > 0** (verifier-down
is most severe; incomplete beats a bare seller error).

**Exit `2` is overloaded** — a per-seller resolve/fetch error emits the full
`--json` object (with that seller's `error` populated), whereas an argparse
usage error exits `2` with **nothing on stdout**. So parse exit-code-first,
but on a `2` also check whether stdout is a parseable object: object → seller
error(s); empty → usage error (read stderr).

`sellers[*].filtered` carries the "Filtered N false positive(s)" reasons
inline (BUI-298) — no need to scrape stderr for them under `--json`.

## Verification is already done inside the script (BUI-149)

The fuzzy matcher (issue-number-in-title + ≥50% series-token overlap) is deliberately loose so it doesn't miss a wish-list book — but that means a short or generic series name can produce a **false positive** (e.g. wish-list "Daredevil #1" matching a "Daredevil Annual #1" or an unrelated reprint). Those false positives are the leak at the **seller-scan → /comic:buy seam**: once a wrong URL flows into `/comic:buy`, identify + FMV will happily price the wrong book.

**`seller_scan.py` already guards this seam itself** — do **not** run a second verifier from the skill. Before emitting anything, the script runs an internal Claude (haiku) pass over **every** candidate and keeps only the genuine matches, so the rows you see in the table/JSON are already post-verified. Spawning a `general-purpose` subagent here would just re-verify an already-verified set.

**No silent drops:** the rejected candidates are printed to **stderr** as a `Filtered N likely false positive(s)` block with the model's one-line reason for each — and (BUI-298) the same data is also returned inline per-seller in `--json` output as `sellers[*].filtered` (`{item_id, title, wish_name, reason}`), so a caller piping `--json` into another tool doesn't have to scrape stderr to see why something was filtered. Surface that info to the user alongside the match table so they can override if the verifier was wrong — don't discard it. (Run without `2>/dev/null` so you actually see the stderr version too.)

Separately, a candidate that was **never verified** at all (a `claude` CLI timeout/transport failure that survived retries) is NOT the same as a model rejection — it lands in `sellers[*].dropped_candidates` and flips that seller's `incomplete` to `true` (exit code 3 overall). See the Output section's exit-code table.

Every surfaced match has a `match_score ≥ 0.65` (the script's emit floor — see *Matching algorithm*); the **0.65–0.69** band is the genuinely-borderline range a user may still want to eyeball on the listing page, even though Claude already passed it.

## Feed matches into /comic:buy

Copy the eBay URLs from the URL column (MATCH rows, plus any UNCERTAIN rows the user cleared) and pass them to `/comic:buy`. The buy workflow will identify, check your collection, calculate FMV, and add snipes.

## Matching algorithm

- Fetches up to 1000 seller listings (override with `--max-results N`)
- Parses each wish list item `name` (e.g., "Amazing Spider-Man #300") into series + issue number
- Matches when: issue number appears in the listing title AND ≥50% of series name tokens match
- Emits a match only when the score is **≥ 0.65** (the floor in `seller_scan.py`); anything below 0.65 is discarded and never surfaced. Scores close to 1.0 are exact series matches; the 0.65–0.69 band is borderline. (A "0.5" score is never emitted — it's below the floor.)

## Common issues

| Issue | Fix |
|---|---|
| `unknown seller '<name>'` | The store name isn't in your alias map. Find the username (`_ssn=` in the seller's "See other items" URL) and re-run with `--add-alias <username>`. In a multi-seller batch this does NOT abort the other sellers — that seller's slot gets `error: "unknown seller '<name>'"` and the batch exits 2 (or 3 if another seller was also incomplete) |
| `--username`/`--add-alias apply to exactly one seller` (exit 2) | You passed `--username` or `--add-alias` alongside 2+ sellers — re-run with a single seller when using either flag |
| eBay rejected the seller filter | The resolved username isn't a valid eBay login username — re-check the `_ssn=` value and update the alias |
| `Dropped N listing(s) from other sellers` | Safety net fired: eBay returned foreign sellers and they were filtered out. Usually means the alias points at the wrong/stale username |
| 0 listings fetched | Seller may have no active auction listings; check their eBay page |
| False positives (wrong comic) | Every surfaced match (score ≥ 0.65) has already passed the script's internal Claude verification; the 0.65–0.69 band is borderline and worth eyeballing on the listing page. Rejected candidates are printed to stderr (`Filtered N likely false positive(s)`) and, under `--json`, listed inline per-seller in `sellers[*].filtered` — check there if a real match seems missing |
| Exit code 3 / `INCOMPLETE` | A candidate for at least one seller was NEVER verified (claude CLI timeout/transport failure). That seller's `sellers[*].dropped_candidates` lists them; they are NOT recorded as seen, so simply re-running the scan will retry them |
| Expected a match but got nothing new | It was already surfaced in a prior scan and hidden by default. Re-run with `--all` to see every match. |
| `could not fetch/record seen item IDs` warning | Best-effort seen-tracking couldn't reach the server, so the scan showed all matches (safe fallback). Check `$COMICS_SERVER_URL` is reachable if you want only-new filtering back. |
| Wish list empty | seller-scan now fetches the wish-list from the comics server (`GET /api/comics/wish-list`), not a local `locg` call. Check `curl -sf "$COMICS_SERVER_URL/api/comics/wish-list"` returns items; if empty, run the LOCG import flow. |
| `COMICS_SERVER_URL is not set` | seller-scan fetches the wish-list over HTTP (apps/ebay can't import locg). Set `COMICS_SERVER_URL` (MacBook → `http://mac-mini.tail9b7fa5.ts.net:8080`) and re-run. |
| Rate limit error | Re-run after a few seconds; the Browse API allows retries |
