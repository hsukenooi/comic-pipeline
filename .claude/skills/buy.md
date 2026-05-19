---
name: comic:buy
description: "End-to-end comic bidding workflow. Takes eBay URLs, identifies comics, checks your collection, calculates FMV, and adds snipes to Gixen. Orchestrates /comic:identify, /comic:collection-check, /comic:fmv, and /comic:snipe-add sequentially."
---

# Comic Buy

End-to-end workflow for sniping eBay comic book auctions. This is the orchestrator — it runs the four leaf skills in sequence, passing output between them and stopping at each step for user confirmation.

Each leaf skill is also usable standalone. Use this when the user provides eBay URLs and wants the full flow.

---

## Execution Pattern

Read each leaf skill file from disk and follow its instructions inline. This ensures the orchestrator stays in sync with any updates to the leaf skills.

At each step, present results to the user and wait for approval before proceeding.

---

## Step 1: Identify

Read `~/Projects/comic-pipeline/skills/identify.md` and follow it.

**Input:** eBay URLs from the user.
**Output:** Identification table (comic, issue, grade, variant, auction vs BIN).

Gate: user confirms identifications are correct. Flag Buy It Now listings — they're skipped at the Gixen step.

---

## Step 2: Collection Check

Read `~/Projects/comic-pipeline/skills/collection-check.md` and follow it.

**Input:** Comic identification table from Step 1.
**Output:** Table with `in_collection` flag per comic, and a resolved `locg_id` (and `locg_variant_id` if applicable) for each comic where one was found.

`/comic:collection-check` performs LOCG lookups to test for duplicates anyway — capture the resolved IDs from that pass and carry them on the working list for Step 5. This avoids a second lookup.

Gate: user decides whether to skip duplicates or continue (condition upgrades are legitimate).

Remove skipped comics from the working list before Step 2.5.5.

---

## Step 2.5: Grade Ungraded Comics (conditional)

**Run after the collection check** — only grade comics that survived the collection check. No point assessing condition on a comic the user already owns and is skipping.

Inspect the surviving working list for comics where `grade_source` is `"missing"` AND no grade signal appeared in the title or description.

**If any such comics remain:**

Read `~/Projects/comic-pipeline/skills/grade.md` and follow it for those listings only.

- Pass only the ungraded item IDs — already-graded comics skip this step
- The skill downloads photos via the eBay Browse API and dispatches 3 independent grader agents per comic
- Use the consensus grade output as the grade for Step 3

Present the photo-assessed grades to the user before proceeding:

```
| # | Comic | Seller Grade | Photo Grade | Source |
|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | NM- | — | seller stated |
| 2 | Fantastic Four #48 | ⚠️ not stated | 5.0 VG/FN | photo assessed |
```

Gate: user confirms the assessed grades (or overrides any) before FMV.

**If all comics already have a stated grade:** skip this step entirely.

---

## Step 3: FMV

Run `gixen-cli fmv` directly — do not read `fmv.md` mid-flow. The CLI handles fetch (via `ebay-fetch sold-comps`), cache, dedup, IQR, quartiles, confidence rubric, self-exclusion, and DB upsert.

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py fmv --batch <working_list.json> --out <results.json>
```

**Input:** Working list JSON: `[{item_id, title, issue, year, grade, locg_id?, locg_variant_id?, notes?}, ...]` for the comics that survived collection check (with photo-assessed grades from Step 2.5 if applicable).

**Output:** Human FMV table to stdout + structured JSON at `--out`. Carry the JSON forward to Step 4.

Flags worth knowing:
- `--max-age-days N` (default 7) — reuses FMVs already in the Gixen DB if `fmv_updated_at` is recent
- `--force` — bypasses both SerpApi and DB caches; use only when you suspect a stale comp pool

**Confidence rubric** (CLI returns these labels; surface them in your presentation):

| n (trimmed pool) | CV | Confidence |
|---|---|---|
| ≥8 | <25% | HIGH |
| ≥6 | <30% | HIGH |
| ≥5 | <35% | MEDIUM-HIGH |
| ≥4 | <45% | MEDIUM |
| ≥3 | any | MEDIUM-LOW |
| <3 | — | LOW |

**Flagging rules** (apply when presenting the table to the user):
- LOW or MEDIUM-LOW with `n ≤ 3` → call out explicitly; user may want to skip or set a conservative max
- Auction ends within 24h → mark with **⚠️ ends <date>** in the Notes column. Always surface urgency before max-bid approval
- CV >100% → suspect a wild outlier survived; check the comp pool in the JSON output
- "CGC proxy" in Notes → confidence is capped at MEDIUM-LOW regardless of comp count

If the CLI fails, fall back to the manual procedure in `~/Projects/comic-pipeline/skills/fmv.md`. Otherwise present the table directly.

---

## Step 4: Compute Max Bids

The CLI returns `max_bid = round_clean(0.80 × fmv_high)` per row. Present the proposed bids:

```
| # | Comic | Grade | FMV Range | Max Bid (80%) | Notes |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | NM | $800–1000 | $800 | |
| 2 | Invincible #1 | NM (assumed) | $270–320 | $256 | LOW conf, n=2 |
| 3 | Batman #609 | NM | $40–50 | $40 | ⚠️ ends 2026-05-11 |
```

Clean-number rounding: $5 step below $50, $10 step from $50–$200, $25 step above $200.

Gate: user approves or overrides each max bid.

---

## Step 5: Snipe Add

Read `~/Projects/comic-pipeline/skills/snipe-add.md` and follow it.

**Input:** Approved auctions with max bids and the resolved `locg_id` (and `locg_variant_id` if applicable) carried forward from Step 2.

If a comic was added since Step 2 (e.g. user override) and has no `locg_id` yet, follow the "Resolve LOCG ID" section in `snipe-add.md` to fill it in before running `gixen-cli add`. Pass `--locg-id` (and `--locg-variant-id` if applicable) on every `add` call so the won snipe is self-contained for `/comic:collection-add`.

**Output:** Gixen confirmation table.

Skip Buy It Now listings. Run sequentially (not in parallel) — Gixen session is stateful.

---

## Subagent Strategy (30+ books)

`gixen-cli fmv` already does async fan-out internally and serves cache hits without API calls, so the previous "split FMV across subagents at 30 books" rule mostly doesn't apply anymore — one CLI invocation handles 50+ books in a few seconds. Reach for subagents only when:

- The eBay identification step is bottlenecked (Browse API rate limit or large batch)
- You're processing 50+ unique books and want to keep the orchestrator's context tight

Even then, FMV stays as one CLI call from the main agent (so the SerpApi cache is shared). Parallelize identification, not FMV.
