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

When the user asks to scan several sellers, or to scan "all known sellers", pass **every** seller as a positional arg to **one** `seller_scan.py` invocation in a **single Bash tool call** — do **not** spawn a `Task`/`Agent` subagent per seller. (Rationale for why a subagent-per-seller fan-out is both wasteful and unreliable: `docs/solutions/workflow-issues/seller-scan-verification-batching-seen-tracking-rationale.md`.)

- Pull the store names straight from `apps/ebay/src/seller_aliases.json`'s
  keys when the user says "scan all known sellers."

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
- `--all` shows every match again, including already-seen ones (it still records newly-surfaced matches — `--all` means "show me everything," not "forget"), **and bypasses the 14-day rejected-candidate cache** (BUI-317), force-re-verifying every candidate — use it to recheck a seller when you think a past rejection was wrong.

```bash
.venv/bin/python src/seller_scan.py <seller>          # only new matches
.venv/bin/python src/seller_scan.py <seller> --all    # every match
```

Seen-tracking is **best-effort**: if the comics server is unreachable, the scan warns and shows all matches rather than aborting. This is deliberately the opposite of the **wish-list fetch below, which hard-fails** — see `docs/solutions/workflow-issues/seller-scan-verification-batching-seen-tracking-rationale.md` for why each side made the opposite choice.

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
      "skipped_cached_candidates": 0,
      "incomplete": false,
      "error": null,
      "crashed": false
    }
  ]
}
```

`sellers[*].skipped_cached_candidates` (BUI-317) counts candidates skipped entirely — no Claude CLI call — because that exact (listing, wish) pair was already rejected within the last 14 days. Always `0` when `--all` is passed (it bypasses the cache). See `docs/solutions/workflow-issues/seller-scan-verification-batching-seen-tracking-rationale.md` for why a nonzero count here is expected/healthy rather than a problem.

**Parse exit-code-first, then drill in:**

| Exit code | Meaning | What to do |
|---|---|---|
| `0` | Every seller scanned cleanly | Read `sellers[*].matches` as usual |
| `1` | **Global verifier failure** — the `claude` CLI is missing/unauthenticated, or every chunk failed transport. The run is truncated: the failing seller's slot has `error: "claude verifier globally unavailable ..."`, sellers after it were not attempted | Fix the `claude` CLI/auth and re-run the whole batch |
| `2` | At least one seller couldn't be resolved/fetched (unknown seller name, listing-fetch transport error), but none was incomplete or crashed | Check each `sellers[*].error` — sellers with `error: null` still have usable `matches`. This is a normal, expected failure (stale alias, transient eBay hiccup), not a bug |
| `3` | At least one seller was **incomplete** (`sellers[*].incomplete: true`, top-level `incomplete: true`) — some candidates for that seller were NEVER verified | Surface the `INCOMPLETE` stderr banner; re-run to pick up the never-verified candidates (they were NOT marked seen, so they'll resurface) |
| `4` | At least one seller's **worker crashed mid-scan** (BUI-324; `sellers[*].crashed: true`, `error` starts with `"seller scan crashed: ..."` and carries the exception repr) — the batch still completed for every other seller | This is an unexpected bug isolated to that seller, not a resolvable input problem — worth filing/investigating. The rest of the batch's results are intact and still worth acting on |
| `2` (usage) | An **argparse usage error** (e.g. `--username`/`--add-alias` with 2+ sellers) — the message goes to **stderr** and **no JSON is emitted** | Fix the invocation and re-run |

Priority when a batch hits several conditions: **1 > 3 > 4 > 2 > 0** (verifier-down
is most severe; incomplete beats a crash, which beats a bare resolvable seller
error).

**Exit `2` is overloaded** — a per-seller resolve/fetch error emits the full
`--json` object (with that seller's `error` populated), whereas an argparse
usage error exits `2` with **nothing on stdout**. So parse exit-code-first,
but on a `2` also check whether stdout is a parseable object: object → seller
error(s); empty → usage error (read stderr).

`sellers[*].filtered` carries the "Filtered N false positive(s)" reasons
inline (BUI-298) — no need to scrape stderr for them under `--json`.

## Verification is already done inside the script (BUI-149)

**`seller_scan.py` already guards the seller-scan → `/comic:buy` seam itself — do not run a second verifier from the skill.** Before emitting anything, it runs an internal Claude (haiku) pass over **every** candidate and keeps only the genuine matches, so the rows in the table/JSON are already post-verified. A `general-purpose` subagent here would just re-verify an already-verified set.

**No silent drops:** rejected candidates are printed to stderr as `Filtered N likely false positive(s)` (one-line reason each) and returned inline per-seller in `--json` as `sellers[*].filtered` (`{item_id, title, wish_name, reason}`) — surface this to the user alongside the match table so they can override a wrong rejection. Run without `2>/dev/null` so you see the stderr version too.

A candidate that was **never verified** (timeout/transport failure) is not the same as a model rejection — see exit code `3` in the Output section above. Every surfaced match clears the `match_score ≥ 0.65` floor (see *Matching algorithm* below); the 0.65–0.69 band is borderline and worth a user's eyeball even though Claude already passed it.

Full rationale for why a second verifier is redundant, and why false positives leak at this specific seam: `docs/solutions/workflow-issues/seller-scan-verification-batching-seen-tracking-rationale.md`.

## Feed matches into /comic:buy

Copy the eBay URLs from the URL column (MATCH rows, plus any UNCERTAIN rows the user cleared) and pass them to `/comic:buy`. The buy workflow will identify, check your collection, calculate FMV, and add snipes.

## Matching algorithm

- Fetches up to 1000 seller listings (override with `--max-results N`)
- Parses each wish list item `name` (e.g., "Amazing Spider-Man #300") into series + issue number
- Matches when: issue number appears in the listing title AND ≥50% of series name tokens match
- Emits a match only when the score is **≥ 0.65** (the floor in `seller_scan.py`); anything below 0.65 is discarded and never surfaced. Scores close to 1.0 are exact series matches; the 0.65–0.69 band is borderline. (A "0.5" score is never emitted — it's below the floor.)

## Troubleshooting

Exit-code-specific failure modes (usage errors, INCOMPLETE/exit 3, worker crashes/exit 4) are the Output section's exit-code table above — that table is the single source for those. Remaining issues not tied to a specific exit code:

| Issue | Fix |
|---|---|
| `unknown seller '<name>'` (exit 2) | The store name isn't in your alias map. Find the username (`_ssn=` in the seller's "See other items" URL) and re-run with `--add-alias <username>` |
| eBay rejected the seller filter | The resolved username isn't a valid eBay login username — re-check the `_ssn=` value and update the alias |
| `Dropped N listing(s) from other sellers` | Safety net fired: eBay returned foreign sellers and they were filtered out. Usually means the alias points at the wrong/stale username |
| 0 listings fetched | Seller may have no active auction listings; check their eBay page |
| Expected a match but got nothing new | It was already surfaced in a prior scan and hidden by default. Re-run with `--all` to see every match |
| `could not fetch/record seen item IDs` warning | Best-effort seen-tracking couldn't reach the server, so the scan showed all matches (safe fallback). Check `$COMICS_SERVER_URL` is reachable if you want only-new filtering back |
| Wish list empty | seller-scan fetches the wish-list from the comics server (`GET /api/comics/wish-list`), not a local `locg` call. Check `curl -sf "$COMICS_SERVER_URL/api/comics/wish-list"` returns items; if empty, run the LOCG import flow |
| `COMICS_SERVER_URL is not set` | The wish-list fetch **hard-fails** without it (apps/ebay can't import locg, so it must reach the server over HTTP). Set `COMICS_SERVER_URL` (MacBook → `http://mac-mini.tail9b7fa5.ts.net:8080`) and re-run |
| Rate limit error | Re-run after a few seconds; the Browse API allows retries |

For false-positive filtering, see "Verification is already done inside the script" above.
