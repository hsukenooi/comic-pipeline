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

Read `~/Projects/comic-pipeline/.claude/commands/comic/identify.md` and follow it.

**Input:** eBay URLs from the user.
**Output:** Identification table (comic, issue, grade, variant, auction vs BIN, **seller**).

Gate: user confirms identifications are correct. Flag Buy It Now listings — they're skipped at the Gixen step.

### Seller reliability advisory (BUI-78)

For each distinct seller in the table, query the seller's grading track record
(cheap local GET; best-effort — on any error treat as no history and proceed):

```bash
curl -s "$GIXEN_SERVER_URL/api/seller-reliability?seller=<seller_username>"
```

When `sample_size >= 1`, surface a line before the user decides whether to grade
(Step 2.5) or how aggressively to bid:

> ⚠️ Seller `beatlebluecat` has over-stated condition by ~**+1.5** grade points (n=4 prior assessments). Consider photo-grading this listing to verify condition before bidding.

- `avg_deviation` is `seller_grade − photo_grade`; **positive = over-grades**. Render with an explicit sign.
- For `sample_size` of 1–2, prefix **"early signal —"** and soften the wording (one or two data points, not an established pattern).
- `sample_size == 0` (or any error / no server): show nothing.
- Advisory only — it never changes the grade, FMV, or max bid automatically.

---

## Step 2: Collection Check

Read `~/Projects/comic-pipeline/.claude/commands/comic/collection-check.md` and follow it.

**Input:** Comic identification table from Step 1.
**Output:** Table showing collection membership from the local cache. Cache may lag LOCG by up to N days (shown as "Cache age" in the results).

Gate: user decides whether to skip duplicates or continue (condition upgrades are legitimate). For any `⚠️ Not in cache (cache stale)` results, surface them separately so the user can decide whether to bid before verifying manually.

Remove skipped comics from the working list before Step 2.5.

---

## Step 2.5: Grade Ungraded Comics (conditional)

**Run after the collection check** — only grade comics that survived the collection check. No point assessing condition on a comic the user already owns and is skipping.

Inspect the surviving working list for comics where `grade_source` is `"missing"` AND no grade signal appeared in the title or description.

**If any such comics remain:**

Read `~/Projects/comic-pipeline/.claude/commands/comic/grade.md` and follow it for those listings only.

- Pass only the ungraded item IDs — already-graded comics skip this step
- The skill downloads photos via the eBay Browse API and dispatches graders per comic (value-gated: 1 grader for cheap/unambiguous lots, a 3-grader panel for high-value or boundary-ambiguous ones)
- **Decision-sensitivity gate (U8):** `/comic:buy` is the flow that *has* the current price and can compute FMV, so it can short-circuit grade.md's escalation. For any comic that would escalate to the 3-grader panel, first probe FMV at the **endpoints of the first grader's GRADE RANGE** (a 2-row `comic-fmv --batch` at range-low and range-high, same `grade_confidence` haircut) and compare the resulting bid caps. If both ends give the **same buy/no-buy call** against the current price and bid caps within `CAP_DECISION_TOLERANCE` (10%), the grade's imprecision can't move the bid — **keep the single grade, skip the panel**, and note it. Only escalate when the range straddles a decision boundary. This trades two cheap FMV probes for two vision-grader agents and skips grading precision that the bid never sees.
- Use the consensus grade output as the grade for Step 3 — and carry the **Confidence** column forward as `grade_confidence` (FMV uses it to haircut the bid cap when grade confidence is low)

Present the photo-assessed grades to the user before proceeding:

```
| # | Comic | Seller Grade | Photo Grade | Confidence | Source |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | NM- | — | — | seller stated |
| 2 | Fantastic Four #48 | ⚠️ not stated | 5.0 VG/FN | MEDIUM-LOW | photo assessed |
```

Gate: user confirms the assessed grades (or overrides any) before FMV. Map the grade skill's confidence to `grade_confidence` for Step 3, preserving all four levels: HIGH → `high`, MEDIUM → `medium`, MEDIUM-LOW → `medium-low`, LOW → `low` (MEDIUM-LOW and LOW haircut differently — 0.70 vs 0.60 — so don't collapse them).

**Preserve the raw photo consensus.** Keep the grader's consensus point estimate as its own working-list field (`photo_grade`), distinct from the grade the user confirms/overrides for FMV (`grade`). Step 5 stores `photo_grade` for seller-deviation analytics — it must be the *raw* assessment, never the override.

**If all comics already have a stated grade:** skip grading by default. **Optionally offer to "grade anyway"** for a stated-grade listing — recommended when the Step 1 advisory flagged the seller as an over-grader, or for a high-value book — since photo-grading a stated-grade listing is the only way the seller-deviation signal (BUI-78) accumulates for over-graders (who almost always state a grade). Keep it user-gated, not automatic.

---

## Step 3: FMV

Run `comic-fmv` directly — do not read `fmv.md` mid-flow. The CLI handles fetch (via `ebay-fetch sold-comps`), cache, dedup, IQR, quartiles, confidence rubric, self-exclusion, and DB upsert.

```bash
comic-fmv --batch <working_list.json> --out <results.json>
```

**Input:** Working list JSON: `[{item_id, title, issue, year, grade, grade_confidence?, locg_id?, locg_variant_id?, notes?}, ...]` for the comics that survived collection check (with photo-assessed grades from Step 2.5 if applicable). Include `grade_confidence` (`high`|`medium`|`low`) for comics graded from photos in Step 2.5; omit it for seller-stated grades — an absent `grade_confidence` means no bid haircut (standard 80% max bid).

**Output:** Human FMV table to stdout + structured JSON at `--out`. Carry the JSON forward to Step 4 **and Step 5**.

Each row in the output JSON includes the internal `comic_id` (and `fmv_id`) returned by `POST /api/comics`. These IDs are how `bids.comic_id` / `bids.fmv_id` get populated downstream — capture them now so Step 5 can thread them into `gixen add`. A row with `comic_id: null` means the DB upsert was skipped (no FMV computed, e.g. `n=0`) — that row will not be linkable; flag it before the user approves a max bid.

### The ID chain (why we capture `comic_id`)

This is the chain that fixes the recurring "bids.comic_id and bids.fmv_id are NULL" bug (PER-140):

```
fmv_runner.py → result["comic_id"]
              → /comic:buy carries it forward
              → gixen add --comic-id <id> --grade <float>
              → POST /api/bids/{item_id}/link-fmv {comic_id, grade}
              → bids.comic_id + bids.fmv_id populated via bid_fmvs junction
```

If the `comic_id` is dropped at any step the snipe still records, but the dashboard loses condition and FMV data for that bid. Step 5 is where most past sessions broke the chain.

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

If the CLI fails, fall back to the manual procedure in `~/Projects/comic-pipeline/.claude/commands/comic/fmv.md`. Otherwise present the table directly.

---

## Step 4: Compute Max Bids

The CLI returns `max_bid = round_clean(bid_factor × fmv_high)` per row. `bid_factor` is the standard `0.80` **unless** a low `grade_confidence` (from a photo grade) or low comp confidence triggers a haircut (`0.70` at MEDIUM-LOW, `0.60` at LOW combined) — see Step 2.5. When a haircut applied, the row's Notes carry `bid_haircut=…`. Present the proposed bids:

```
| # | Comic | Grade | FMV Range | Max Bid | Notes |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | NM | $800–1000 | $800 | |
| 2 | Invincible #1 | NM (assumed) | $270–320 | $192 | LOW conf, n=2, bid_haircut=0.60 |
| 3 | Batman #609 | NM | $40–50 | $40 | ⚠️ ends 2026-05-11 |
```

Clean-number rounding: $5 step below $50, $10 step from $50–$200, $25 step above $200.

Gate: user approves or overrides each max bid.

---

## Step 5: Snipe Add

Read `~/Projects/comic-pipeline/.claude/commands/comic/snipe-add.md` and follow it.

**Input:** Approved auctions with max bids **plus the `comic_id` and numeric grade for each row from the Step 3 FMV JSON**, and the `seller` username + grades captured earlier. Without `comic_id`, the bid will be added but `bids.comic_id` / `bids.fmv_id` will stay NULL — see PER-140.

For each approved auction call:

```bash
gixen add {item_id} {max_bid} \
  --comic-id {comic_id} --grade {grade_numeric} \
  --seller {seller_username} --seller-grade {seller_grade} --photo-grade {photo_grade}
```

- `--seller` is the username from Step 1 (stored lowercased; the seller-reliability key).
- `--seller-grade` is the seller's stated grade as a CGC float (convert "VF/NM" → `9.0`); omit if the seller stated no grade or it's unmappable.
- `--photo-grade` is the **raw** Step 2.5 consensus (BUI-78) — omit unless the book was photo-graded; never pass a user-overridden value here.
- These three are independent of FMV linkage — they populate `bids.seller`/`seller_grade`/`photo_grade` for the seller-reliability advisory and don't affect the bid.

If `comic_id` is null for a row (FMV upsert was skipped, e.g. `n=0`), omit `--comic-id` and surface that this bid will not have FMV linkage in the dashboard. Do **not** pass the internal `comic_id` to `--catalog-id` — that flag expects an LOCG id and silently fails to link.

**Output:** Gixen confirmation table.

Skip Buy It Now listings. Run sequentially (not in parallel) — Gixen session is stateful.

When snipes are added, remind the user:
> Run `/comic:collection-add` after auctions close to record wins. Check `locg collection status` for pending push count before uploading.

---

## Step 6: Verify

Read `~/Projects/comic-pipeline/.claude/commands/comic/verify.md` and follow it.

**Input:** Working list of `{item_id, grade, locg_id?}` for every comic that was sniped in Step 5. Skip Buy It Now listings (they have no bid to verify).

**Output:** Verification table with a verdict per row.

Warn-only — don't block. If `summary.issues > 0`, surface the table and the per-verdict guidance from `verify.md`. The pipeline is done; the goal is to tell the user *now* about gaps so they don't discover them when the auction ends and `/comic:collection-add` chokes (PER-70 cascade).

Born from PER-99 — `/comic:buy` ran to apparent success in past incidents while leaving rows partially populated (missing comic row, FMV stub, `bids.fmv_id` null).

---

## Subagent Strategy (30+ books)

`comic-fmv` already does async fan-out internally and serves cache hits without API calls, so the previous "split FMV across subagents at 30 books" rule mostly doesn't apply anymore — one CLI invocation handles 50+ books in a few seconds. Reach for subagents only when:

- The eBay identification step is bottlenecked (Browse API rate limit or large batch)
- You're processing 50+ unique books and want to keep the orchestrator's context tight

Even then, FMV stays as one CLI call from the main agent (so the SerpApi cache is shared). Parallelize identification, not FMV.
