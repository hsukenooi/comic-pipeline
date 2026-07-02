---
name: comic:grade
description: Grade the physical condition of raw comics from eBay listing photos using value-gated independent sub-agents (1 grader for cheap/unambiguous lots, escalating to a 3-grader panel for high-value or boundary-ambiguous ones) and CGC/Overstreet criteria. Use when the user wants a condition assessment before bidding or evaluating a listing. Produces CGC-scale numeric grades with a confidence level, grade range, and defect breakdowns.
---

# Comic Grade

Grade raw (ungraded) comics from eBay seller photos. A first grader runs on every comic; high-value or boundary-ambiguous comics escalate to a 3-grader panel whose independent grades are synthesized into a consensus. Each grade carries a coverage-driven confidence level and grade range. Outputs match `/comic:fmv` input format.

## Input

One or more eBay listing URLs or item IDs. No seller-stated grade needed — this skill derives it from photos.

## Step 1: Download Listing Photos

Use the eBay Browse API via `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` — the `get_item_by_legacy_id` endpoint returns `image` and `additionalImages` with direct `i.ebayimg.com` URLs that are downloadable without bot detection. Do not scrape eBay HTML pages (returns 400/CAPTCHA).

Run the downloader script (BUI-279 — extracted from this skill so the OAuth + Browse API logic isn't re-read into context on every grade run; same OAuth flow, same `get_item_by_legacy_id` calls, same image-download logic):

```bash
python3 ~/Projects/comic-pipeline/apps/ebay/src/grade_photos.py 178057470740 178057488707 ...
```

Item IDs are labeled `comic-1`, `comic-2`, ... in the order given, downloaded into `/tmp/comic-grading/<label>/` (override with `--workdir`). It prints one line per comic:

```
comic-1: FETCH FAILED — <error>
comic-1: Fantastic Four #48 (1966) 9.0 VF/NM — 8 images — current bid $42.50 (5 bids)
```

The printed current-bid figure per comic is the **value signal for the Step 2 value gate** — record it alongside the item id from the printed line so escalation can branch on listing value. It is the auction's current bid or, for a fixed-price (BIN) listing, its Buy-It-Now price — **both count toward the value gate** (a $40 BIN is a high-value lot and escalates the same as a $40 auction). It is `None` only when no price field is present at all; treat a genuinely absent value as "below threshold" (single grader) unless other signals say otherwise (BUI-165).

**A `FETCH FAILED` line is not an image-less listing (BUI-147).** If the download script prints `FETCH FAILED` for a comic (a down/429/404 eBay API), do **not** feed it to the triage pre-pass or drop it as "un-gradeable" — that's an API failure, not a photo-quality verdict. Re-run that item (the script retries 429 automatically); surface the failure to the user rather than silently grading 0 images.

Output directory layout:
```
/tmp/comic-grading/
  comic-1/
    img-01.jpg   ← front cover (first image returned by API)
    img-02.jpg   ← additional images
    ...
```

No `listing.html` is produced. In the grader prompt, note "no seller description available" unless the seller's grade is known from the listing title (retrieved by `ebay_fetch.py`).

## Step 1.5: Triage Pre-Pass (optional — for raw scans)

Before fanning out any grader, kill the no-hopers. Grading is the expensive step; spending it on a book that is un-gradeable, mis-listed, or an obvious beater the user would never buy is pure waste. **Use this step when grading a raw, uncurated seller scan** (many listings, unknown quality). **Skip it** for a short, already-curated list (user-supplied URLs, confirmed wish-list matches) — the pre-pass costs more than it saves there.

Run **one** cheap agent over the whole candidate list (title + first image + photo count per listing — not a full grade) that sorts each into **KEEP / DROP / FLAG**:

- **DROP** (no grading) — only the *unambiguous* no-hopers:
  - **Un-gradeable photos:** 0–1 usable images, or all images blurry / obstructed / not the comic.
  - **Confirmed non-match:** the listing is plainly not the wish-listed book (wrong series/issue/title) — i.e. it should not have been in the scan.
- **FLAG** (grade only if the user confirms) — a **suspected obvious beater** (visible heavy damage — large missing piece, detached/ split cover, water damage). There is **no FMV floor at grade time**, so do NOT auto-kill on condition; surface it and let the user decide.
- **KEEP** — everything else proceeds to Step 2.

**Conservative default:** when unsure which bucket a listing belongs in, **KEEP it** — the failure to avoid is silently dropping a book the user wanted (mirrors the no-cap-when-ambiguous principle elsewhere in this skill).

**No silent drops:** output a triage table — every candidate with its bucket and a one-line reason — so the user sees exactly what was skipped and why before any grading runs.

```
| Item ID | Title | Photos | Triage | Reason |
|---------|-------|--------|--------|--------|
| 1780... | FF #48 (1966) | 2 | KEEP | gradeable, on wish list |
| 1781... | (blurry) | 1 | DROP | single blurry photo — un-gradeable |
| 1782... | Hulk #1 (beater) | 4 | FLAG | large back-cover chunk missing — confirm before grading |
```

## Step 2: Dispatch Grader Agents (value-gated)

Don't fan out 3 graders for every comic — most listings in a seller scan are cheap, and 3-per-comic burns agents where 1 will do. **Run 1 grader first, then escalate to a 3-grader panel only when the comic earns it.** Each grader is the **`comic-grader` subagent** (`.claude/agents/comic-grader.md`) — it carries the full grading persona, criteria, and OUTPUT FORMAT contract, scoped read-only (`Read, Bash`) so a grader can never write. You only pass it the dynamic inputs (see [Grader Agent](#grader-agent) below); the persona is identical whether you run 1 or 3, so panel grades stay independent and comparable.

**Tunable gate constants** (stated here so they're easy to adjust):
- `VALUE_THRESHOLD = $25` — `current_price` (from Step 1) at or above this always gets the full 3-grader panel; an expensive book justifies the rigor.
- `CAP_BAND = 0.5` — if the single grader's grade sits within this many points of a grade-capping threshold (the spine-split / missing-piece / detached-cover ceilings), treat it as boundary-ambiguous.
- `BATCH_MAX = 5` — how many sub-threshold (cheap) books one grader agent grades in a single context before opening another. Caps context bleed / grader fatigue across books.
- `CAP_DECISION_TOLERANCE = 10%` — in the decision-sensitivity gate (below), two bid caps computed at the ends of a grade range count as "the same decision" if they're within this much of each other (and the buy/no-buy call doesn't flip).

**Escalate the single-grader result to a full 3-grader panel when ANY of these hold:**
1. **Value:** `current_price ≥ VALUE_THRESHOLD` (or the listing is a known key regardless of current bid).
2. **Boundary-ambiguous grade:** the grader identified a grade-capping defect (`GRADE CAP` ≠ none) and the grade sits within `CAP_BAND` of that ceiling — or is unsure whether a capping defect is present — OR a possible-restoration flag fired, OR the grader gave a wide GRADE RANGE (≥1.5 pts) **at MEDIUM confidence or higher**. (Proximity to a round grade with `GRADE CAP: none` is **not** a near-cap trigger — the cap must be an observed/suspected *defect*, not a number.) A wide range only escalates when it signals disagreement over *visible* evidence that more graders can resolve. A wide range at MEDIUM-LOW/LOW confidence is **coverage-driven** — the photos can't show the deciding surfaces (spine stress, interior, page edge), so adding graders cannot narrow it; it does **not** escalate on its own. (Near-cap and restoration still escalate regardless of coverage, since a second look can confirm a *visible* capping defect.)
3. **Decision-relevant:** a half-grade swing would plausibly cross the buy/no-buy line the user cares about (if known at grade time).

**Stay at the single grader when** the auction is below `VALUE_THRESHOLD`, no cap/restoration flag fired, and any wide range is coverage-driven (MEDIUM-LOW/LOW confidence). Unknown `current_price` counts as below-threshold. Note the common case: a cheap 2-cover-photo lot draws a wide range *because* coverage is thin — that is the expected MEDIUM-LOW output, not an escalation signal. Escalating it would burn 3 graders on photos that structurally can't resolve the spread (the failure mode that negates the value gate on a typical thin-photo seller scan).

**Decision-sensitivity gate (optional — suppresses escalation when the grade can't change the buy).** When grading inside a flow that knows the auction's current price and can compute FMV (e.g. `/comic:buy`), a book that tripped an escalation trigger above can still **stay at 1 grader** if a tighter grade wouldn't change the decision. Before escalating, probe FMV at the **endpoints of the grade RANGE** (low and high) and compute the bid cap at each (same `grade_confidence` haircut at both). If **both** endpoints give the **same buy/no-buy call** against the current price **and** bid caps within `CAP_DECISION_TOLERANCE` of each other, the extra grader precision cannot move the outcome → do **not** escalate; report e.g. `1 grader (decision-insensitive: bid cap $34–$37 across 6.0–7.5, same buy call)`. Escalate only when the range straddles a decision boundary — the buy/no-buy call flips, or the bid-cap swing exceeds the tolerance. This is the highest-leverage skip on a value-gated scan: it stops a 3-grader panel from pinning a grade whose imprecision doesn't reach the bid. (The value trigger still escalates a high-value book whose decision *is* sensitive; the gate only suppresses escalation that provably can't matter.)

**Dispatch mechanics:**
- Split the candidates into **cheap** (`current_price < VALUE_THRESHOLD`, or unknown price) and **not-cheap** (≥ `VALUE_THRESHOLD`, or a known key).
- **Cheap books → batch them (U9).** A cheap book only ever earns 1 grade unless a gate trips, so there is no cross-grader independence to preserve — grade several in **one** agent context instead of one agent each. Group the cheap books into batches of up to `BATCH_MAX` and give each batch a single grader agent that grades every book in the group **independently** and returns one full OUTPUT FORMAT block per book (clearly delimited, labelled by item id). This is the main first-pass cost saver on a thin-photo seller scan (e.g. 7 cheap books → 2 agents, not 7).
- **Not-cheap books → 1 grader each, first pass**, run in parallel (separate agents).
- **Escalation (both kinds).** After the first pass, any book that tripped a gate (Step 2 triggers) gets the **remaining 2** graders as separate, independent agents — dispatched together in one parallel batch. A batched cheap book's first grade counts as grader A; pull it out and add B + C. (The grader prompt and criteria are identical across passes so the panel grades stay independent and comparable.)
- **Batching guardrails:** keep each book's images and OUTPUT FORMAT block fully separate in the batched prompt; never let one book's defects bleed into another's grade; if a batch would exceed `BATCH_MAX`, open another agent. When in doubt about a specific cheap book (e.g. it looks near a cap), grade it on its own rather than in the batch.
- **Anti-anchoring (batched grades must not drift):** grade every book against the **absolute** CGC/Overstreet scale, exactly as if it were the only book in front of you. Do **not** let the overall quality of the batch raise or lower any single grade — a clean book in a batch of beaters is not a 9.6, and a rough book among clean ones is not a 2.0. A measured drift toward higher point grades on clean books was seen when batching vs. one-agent-each (BUI-81 U9 validation); counter it by re-anchoring each book on its own visible defects before naming a number.

**Required per-comic reporting (no silent caps):** for every comic, state how many graders ran and why — e.g. `1 grader (──$6, range wide but coverage-driven at MEDIUM-LOW)` or `1 grader (──$6, unambiguous)` or `3 graders (──$40 ≥ $25 value threshold)` or `3 graders (grade 5.0 within 0.5 of the 1/2" spine-split cap)`. The user must be able to see where rigor was and wasn't spent.

### Grader Agent

The grading persona — the full CGC/Overstreet scale, criteria, PRINT-LAYER / WRITING / GRADE-CAPPING / restoration rules, coverage-driven CONFIDENCE, the SELLER-STATED-GRADE prior, the procedure, and the **OUTPUT FORMAT contract** — lives in the **`comic-grader` subagent** (`.claude/agents/comic-grader.md`), scoped to `Read, Bash` so a grader can never write. Invoke it **by type** (`comic-grader`); do not paste a grading prompt inline. This keeps the most-tuned prompt (BUI-81 anti-anchoring) in one place and its OUTPUT FORMAT aligned with `/comic:fmv` input.

The OUTPUT FORMAT block the agent returns (`GRADE`, `GRADE RANGE`, `CONFIDENCE`, `GRADE CAP`, defects, etc.) is the contract Step 3 parses — if you ever change it, change it in the agent def, not here.

**Dynamic inputs to pass per invocation** (the only per-comic data the agent doesn't already carry):

- **COMIC + YEAR** — e.g. `Fantastic Four #48 (1966)`
- **IMAGE FOLDER** — e.g. `/tmp/comic-grading/comic-1`
- **IMAGES** — `img-01.jpg` through `img-{N:02d}.jpg` (N photos)
- **SELLER-STATED GRADE** — from the listing title/description if present, else `none stated`
- **Item id label** — so batched output blocks are traceable

For a **batched cheap-book agent** (see Dispatch mechanics), hand it the per-comic block above for each book in the group (up to `BATCH_MAX`) and tell it to grade each independently and return one OUTPUT FORMAT block per book, labelled by item id. The agent's own anti-anchoring guard keeps batched grades on the absolute scale.

## Step 3: Synthesize Consensus

After all agents return for a given comic (1 grader if not escalated — see Step 2):

1. Collect the numeric grades, plus each grader's GRADE RANGE and CONFIDENCE.
2. Compute the average; note the spread.
3. If the graders agree within 0.5 pts → use the median as the point grade.
4. If spread is 1.0+ pts → read the outlier's rationale before defaulting to median, and classify WHY they disagree:
   - **Named-defect disagreement** (the outlier cites a *specific physical defect* the others missed — e.g. "spine split ~3/8"", "writing on story page 7"): the defect is likely real and the median is too high. Adopt the outlier's grade and flag the defect in the consensus.
   - **Lighting/reflectivity-only disagreement** (no named physical defect, just a brighter/duller read): discard the outlier and use the median.
   - **Epistemic disagreement** (the graders diverge because the photos don't *show* the deciding surface — nobody can name the defect because nobody can see that view): this is uncertainty, not a defect. Do NOT just take the median — set the consensus GRADE RANGE to span the disagreement and LOWER the consensus confidence accordingly. The point grade stays the median, but it travels with a wider range and a reduced confidence label.
5. **Consensus CONFIDENCE is capped by coverage** (which is identical across graders since they share the photos): start from the per-grader coverage ceiling, then lower it further if epistemic disagreement (case above) is present. A 2-cover-photo lot is MEDIUM-LOW at best no matter how tightly the graders agreed — agreement on insufficient data is not high confidence.
6. **Consensus GRADE RANGE** is the union of the graders' ranges, widened (not narrowed) by any epistemic disagreement.
7. Combine the defect lists (union, deduplicated) to produce a master defect summary.

### Consensus Table

```
| Grader | Grade | Range | Confidence |
|--------|-------|-------|------------|
| A      | X.X   | X.X–X.X | … |
| B      | X.X   | X.X–X.X | … |
| C      | X.X   | X.X–X.X | … |
| **Consensus** | **X.X (label)** | **X.X–X.X** | **HIGH/MEDIUM/MEDIUM-LOW/LOW** |
```

(Single-grader case: one row + the consensus row carrying that grader's grade, range, and coverage-capped confidence.)

## Output

Present one block per comic:

```
### Comic Title (Year) — Item ID
| Grader | Grade | Range | Confidence |
| A | 5.0 | 5.0–6.0 | MEDIUM-LOW |
| B | 5.0 | 5.0–5.5 | MEDIUM-LOW |
| C | 4.5 | 4.5–6.0 | MEDIUM-LOW |
| **Consensus** | **5.0 (VG/FN)** | **4.5–6.0** | **MEDIUM-LOW** |

Key defects: [2-3 sentence summary of the most important ones]
Positives: [brief]
Caveats: [what photos couldn't show]
```

Then a summary table at the end:

```
| # | Comic | Item ID | Consensus Grade | Range | Confidence |
|---|-------|---------|-----------------|-------|------------|
| 1 | FF #48 (1966) | 178057470740 | 5.0 VG/FN | 4.5–6.0 | MEDIUM-LOW |
| 2 | ASM #300 (1988) | 123456789 | 8.5 VF+ | 8.5 | HIGH |
```

The summary table is the input for `/comic:fmv`. **Carry the Confidence column forward** — `comic-fmv` consumes it (as `grade_confidence`) to haircut the bid cap when grade confidence is low. Map the label to lowercase, preserving all four levels: `HIGH → high`, `MEDIUM → medium`, `MEDIUM-LOW → medium-low`, `LOW → low`. (Don't collapse MEDIUM-LOW into `low` — they haircut differently: MEDIUM-LOW → 0.70, LOW → 0.60.)

## Integration with /comic:buy

`/comic:buy` accepts grades from this skill. After running `/comic:grade`, pass the consensus grade column directly into the FMV step:

> "Using these grades, for these URLs" → triggers `/comic:buy` to skip Step 1's seller-stated grade and use the photo-assessed grades instead.

## Caveats to Always State

These are structural limitations of any photo-based assessment:

- No close-up of staple shanks → rust unknown
- No centerfold spread photo → attachment confidence only
- No raking-light shot → subtle color-breaking creases may be missed
- No flex test → brittleness unknown
- No black-light → color touch / restoration not detectable
- Actual CGC grade could land ±0.5 from this assessment; restoration discovery would drop it more

Always note these. Do not claim CGC accuracy.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Scraping eBay HTML for images | Use the Browse API (`get_item_by_legacy_id`) — returns `image` + `additionalImages` URLs directly, no bot detection |
| Using firecrawl/WebFetch for eBay images | Both are blocked by eBay bot detection — use Browse API only |
| Grading from WebFetch text output | WebFetch returns markdown text, not images — useless for visual grading |
| Giving all 3 agents the same agent name | Use distinct names (e.g., `grader-c1-a`, `grader-c1-b`) so results are traceable |
| Running graders sequentially | Dispatch in a single message. First pass: cheap books batched (≤`BATCH_MAX` per agent, U9), not-cheap books one grader each; escalation pass: the extra 2 panel graders for any book that tripped a gate, as separate independent agents |
| Batching a near-cap or escalation-bound book with the cheap group | Grade it on its own — a book heading for a 3-grader panel needs an independent grader A, not a shared batched context |
| Fanning out 3 graders for every comic | Value-gate it (Step 2): 1 grader first, escalate to 3 only on value ≥ threshold or an ambiguous/near-cap grade. State the grader count + reason per comic |
| Escalating every 2-photo lot because its range is wide | A wide range at MEDIUM-LOW/LOW confidence is coverage-driven — more graders can't see the missing views, so it does NOT escalate (Step 2 trigger 2). Only escalate on a wide range at MEDIUM+ confidence, a near-cap grade, restoration, or value |
| Auto-dropping a book in triage because it looks like a beater | Condition is not a triage kill — there's no FMV floor at grade time. FLAG suspected beaters for the user; only DROP un-gradeable photos or confirmed non-matches (Step 1.5). When unsure, KEEP |
| Including related-listing images | Extract carousel IDs from the `ux-image-carousel-container` section only |
| Inflating grade because it's a key issue | Grade physical condition only — key issue premium belongs in FMV, not grade |
| Capping the grade over a printed credit/signature | Printed credits, facsimile signatures, barcodes, and price boxes are in the print layer — never defects (PRINT-LAYER RULE). Only a post-print autograph caps. When unsure, do NOT cap |
| Skipping the caveat section | Always disclaim photo-based limitations so user knows the confidence level |
