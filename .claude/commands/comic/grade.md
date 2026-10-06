---
name: comic:grade
description: Grade the physical condition of raw comics from eBay listing photos using value-gated independent sub-agents (1 grader for cheap/unambiguous lots, escalating to a 3-grader panel for high-value or boundary-ambiguous ones) and CGC/Overstreet criteria. Use when the user wants a condition assessment before bidding or evaluating a listing. Produces CGC-scale numeric grades with a confidence level, grade range, and defect breakdowns.
---

# Comic Grade

Grade raw (ungraded) comics from eBay seller photos. A first grader runs on every comic; high-value or boundary-ambiguous comics escalate to a 3-grader panel whose independent grades are synthesized into a consensus. Each grade carries a coverage-driven confidence level and grade range. Outputs match `/comic:fmv` input format.

## Input

One or more eBay listing URLs or item IDs. No seller-stated grade needed — this skill derives it from photos.

**Skip a certified (CGC/CBCS) listing outright (BUI-923)** — `grade_source: "certified"` from `/comic:identify` means the grade is sealed by the grading company, not a seller's word to verify. Photo-grading a slab has nothing to add: there's no condition risk to assess and no seller-deviation signal (BUI-78) to accumulate against a company grade. If a mixed batch of URLs includes both raw and certified listings, grade only the raw ones and pass the certified ones straight through with their certified grade untouched.

## Step 1: Download Listing Photos

Use the eBay Browse API via `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` — the `get_item_by_legacy_id` endpoint returns `image` and `additionalImages` with direct `i.ebayimg.com` URLs that are downloadable without bot detection. Do not scrape eBay HTML pages (returns 400/CAPTCHA).

Run the installed `grade-photos` console script (BUI-279 — it owns the OAuth, Browse API, and download logic so none of it is read into context):

```bash
grade-photos 178057470740 178057488707 ...
```

Item IDs are labeled `comic-1`, `comic-2`, ... in the order given. When `--workdir` is not passed, each run gets its own fresh directory under `/tmp/comic-grading` (BUI-440) so a prior run's `comic-N` dirs never leak into a smaller later run, and two overlapping runs never collide on `comic-1`/`comic-2`. The script prints the resolved directory as its first line — read that line to get the base path for Step 2's IMAGE FOLDER inputs:

```
WORKDIR: /tmp/comic-grading/run-a1b2c3
comic-1: FETCH FAILED — <error>
comic-1: Fantastic Four #48 (1966) 9.0 VF/NM — 8 images — current bid $42.50 (5 bids) — tier: not-cheap — est_close: $42.50
comic-2: Fantastic Four #29 (1964) Marvel — 6 images — current bid $4.25 (2 bids) — tier: not-cheap (estimated: vintage 1964, 4d left) — est_close: unbounded — vintage 1964, 4d left
```

The printed **tier** (`cheap` / `not-cheap`) is the **value signal for the Step 2 value gate** — record it alongside the item id from the printed line so escalation can branch on listing value without re-deriving anything. Read the token right after `tier: `, **ignore any trailing `(...)`** — that parenthetical is the reason, not part of the verdict — and stop at the ` — est_close: ` marker that always follows; that final field is separate (see below), not part of the tier.

**The tier is a verdict on the listing's *close* price, not its current one (BUI-917).** `grade-photos` owns the threshold ($25) and everything below it:

- **A price that is already final decides on its own, with no parenthetical.** That means a fixed-price (BIN) listing's Buy-It-Now price at any level, and any price already at or above the threshold (a $40 BIN prints `not-cheap` just like a $40 auction).
- **A live auction below the threshold gets an estimated close instead,** because its current bid is where bidding has *reached*, not where it ends. A 1964 Fantastic Four #29 at $4.25 with 2 bids and four days left used to print `cheap` — the rigor a book received depended on *when* in its auction it happened to be graded. The estimate uses only what the listing already carries: time remaining, bid count, and the book's age (title year / `Publication Year` / `Era`).
- **Ambiguity resolves to `not-cheap`,** since one extra grader is the cheap error and a thin grade on a valuable book is the expensive one. So: a pre-1992 book, or one whose age can't be read, is `not-cheap` while it has more than an hour left; an unknown end time or no price field at all is `not-cheap` (the latter reverses BUI-165's old "absent price counts as cheap").
- **The cheap tier still exists** for a modern book whose projected close stays under the threshold — most reliably one close to ending with little bidding on it, where the current bid genuinely is the close.

Don't re-derive any of this here, and don't second-guess a printed tier from the price on the same line — the two disagree by design on a young auction.

**Every line also carries its own `est_close` field (BUI-992)** — a dollar figure, or `unbounded — <reason>` when no trustworthy number exists (an unknown end time, or a vintage/age-unknown book still more than `_PRICE_NEARLY_FINAL_HOURS` from close — the same ambiguity that makes the tier `not-cheap`). It's printed on every comic, including a BIN listing or a nearly-final auction where it equals the current price outright. This is the field to compare a bid cap against wherever a decision needs to know where the auction will *land*, not where it happens to sit right now — the decision-sensitivity gate below is its first consumer.

**A `FETCH FAILED` line is not an image-less listing (BUI-147).** If the download script prints `FETCH FAILED` for a comic (a down/429/404 eBay API), do **not** feed it to the triage pre-pass or drop it as "un-gradeable" — that's an API failure, not a photo-quality verdict. Re-run that item (the script retries 429 automatically); surface the failure to the user rather than silently grading 0 images.

Output directory layout (under the printed `WORKDIR:` path):
```
<workdir>/
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

Don't fan out 3 graders for every comic — plenty of listings in a seller scan are cheap, and 3-per-comic burns agents where 1 will do. **Run 1 grader first, then escalate to a 3-grader panel only when the comic earns it.** Each grader is the **`comic-grader` subagent** (`.claude/agents/comic-grader.md`) — it carries the full grading persona, criteria, and OUTPUT FORMAT contract, scoped to `Read, Bash` (it writes nothing but scratch crops, and only inside the CROP DIRECTORY you assign it below — BUI-911). You only pass it the dynamic inputs (see [Grader Agent](#grader-agent) below); the persona is identical whether you run 1 or 3, so panel grades stay independent and comparable.

**Tunable gate constants** (stated here so they're easy to adjust):
- **Value tier** — `grade-photos` (Step 1) owns `VALUE_THRESHOLD` ($25), the estimated-close rule, and its constants (`_PRICE_NEARLY_FINAL_HOURS`, `_CLOSE_HEADROOM`, `_MODERN_AGE_START_YEAR` — tune them there, not here), and prints `tier: cheap|not-cheap` per comic. Read the printed tier directly; don't re-derive the split from `current_price` here — on a young auction the tier deliberately disagrees with the price beside it (see Step 1). A `not-cheap` tier gets the full 3-grader panel (unless `SENSITIVITY_GATE=on` and the probe proves the decision insensitive); a book that will cost real money justifies the rigor. Because BUI-917 pushes an early-graded vintage auction into `not-cheap`, a scan of young vintage listings escalates more than it used to — the decision-sensitivity gate below is the intended damper, not a lower threshold (though on young vintage auctions it rarely fires, see "The gate rarely fires" below). The same run also prints `est_close` per comic (BUI-992, see Step 1) — read that field, never `current_price`, wherever a bid cap needs comparing against where the auction will land; `_PRICE_NEARLY_FINAL_HOURS`/`_CLOSE_HEADROOM` drive it too, so tuning them tunes both the tier and the gate together.
- `CAP_BAND = 0.5` — if the single grader's grade sits within this many points of a grade-capping threshold (the spine-split / missing-piece / detached-cover ceilings), treat it as boundary-ambiguous.
- `BATCH_MAX = 5` — how many cheap-tier books one grader agent grades in a single context before opening another. Caps context bleed / grader fatigue across books.
- `SEAT_DISPATCH = headless` — how each grader seat runs (BUI-1090). `headless` runs every seat as its own background `claude -p` process (see Headless seats below, run by the `grade-seats` script); `agent` is the fallback, which spawns the `comic-grader` subagent through the Agent tool as before BUI-1090. Use `agent` when `claude` or `grade-seats` is not on PATH or a headless launch is refused outright; when one headless seat fails twice, dispatch only that seat through the Agent tool rather than switching the whole run.
- `SENSITIVITY_GATE = off` — whether the decision-sensitivity gate (below) can suppress a value-only escalation. Standalone runs keep it off; a caller that can compute FMV passes `on` (buy.md Step 2.5 does, BUI-1085).
- `CAP_DECISION_TOLERANCE = 10%` — in the decision-sensitivity gate (below), two bid caps computed at the ends of a grade range count as "the same decision" if they're within this much of each other (and the buy/no-buy call doesn't flip).

**Escalate the single-grader result to a full 3-grader panel when ANY of these hold:**
1. **Value:** the printed tier is `not-cheap` (or the listing is a known key regardless of tier).
2. **Boundary-ambiguous grade:** the grader identified a grade-capping defect (`GRADE CAP` ≠ none) and the grade sits within `CAP_BAND` of that ceiling — or is unsure whether a capping defect is present — OR a possible-restoration flag fired, OR the grader gave a wide GRADE RANGE (≥1.5 pts) **at MEDIUM confidence or higher**. (Proximity to a round grade with `GRADE CAP: none` is **not** a near-cap trigger — the cap must be an observed/suspected *defect*, not a number.) A wide range only escalates when it signals disagreement over *visible* evidence that more graders can resolve. A wide range at MEDIUM-LOW/LOW confidence is **coverage-driven** — the photos can't show the deciding surfaces (spine stress, interior, page edge), so adding graders cannot narrow it; it does **not** escalate on its own. (Near-cap and restoration still escalate regardless of coverage, since a second look can confirm a *visible* capping defect.)
3. **Decision-relevant:** a half-grade swing would plausibly cross the buy/no-buy line the user cares about (if known at grade time).

**Stay at the single grader when** the printed tier is `cheap`, no cap/restoration flag fired, and any wide range is coverage-driven (see trigger 2 above). The common case: a cheap 2-cover-photo lot hits exactly that coverage-driven case — expected MEDIUM-LOW output, not an escalation signal. Escalating it would burn 3 graders on photos that structurally can't resolve the spread (the failure mode that negates the value gate on a typical thin-photo seller scan).

**Decision-sensitivity gate (opt-in, `SENSITIVITY_GATE`; `/comic:buy` passes it on).** Standalone `/comic:grade` runs with the gate **off** and behaves as above: a `not-cheap` book goes to the panel. A caller that can compute FMV turns it on by passing `SENSITIVITY_GATE=on`; buy.md Step 2.5 does (BUI-1085). With the gate on, a book that tripped a **value** trigger (trigger 1, a `not-cheap` tier) stays at 1 grader unless the first grader's grade RANGE, priced at both ends, crosses the bid cap against the comic's printed `est_close` (BUI-992, see Step 1). Triggers 2 and 3 (a visible capping defect, a restoration flag, a wide range at MEDIUM confidence or higher) still escalate as written; the gate only replaces the value-only escalation.

**The probe (exact commands).** Run it per book after the first grader returns its GRADE RANGE `lo-hi` and CONFIDENCE. Use the no-write FMV mode, never a normal `comic-fmv` run: a normal run stores a price for each grade nobody confirmed (comic-fmv has no other read-only mode; fmv.md § Probe mode). Build one probe batch with two rows, the same book at `lo` and at `hi`, carrying the same `grade_confidence` (the first grader's confidence, mapped as buy.md Step 2.5 maps it) so the haircut is identical at both ends:

```bash
cat > /tmp/probe.json <<'JSON'
[{"item_id": "<id>-lo", "title": "<series>", "issue": "<n>", "year": <year>, "publisher": "<publisher>", "grade": <lo>, "grade_confidence": "<conf>"},
 {"item_id": "<id>-hi", "title": "<series>", "issue": "<n>", "year": <year>, "publisher": "<publisher>", "grade": <hi>, "grade_confidence": "<conf>"}]
JSON
comic-fmv --probe --batch /tmp/probe.json --brief --quiet
```

Pass `variant` too when the listing has one, and run the server resolve and health gate from fmv.md § How to run first. Read `max_bid` off the two `--brief` lines (stdout is JSON Lines; the `PROBE` notice is on stderr). Never read `comic_id`/`fmv_id`: a probe leaves them `null` by design.

**Decide.** Let `cap_lo` and `cap_hi` be the two `max_bid` values and `est_close` the printed field (never `current_price`: that is the not-yet-final number BUI-917 stopped the *tier* from trusting). If **both** caps give the **same buy/no-buy call** against `est_close` (buy means `est_close` below the cap) **and** `cap_lo` and `cap_hi` are within `CAP_DECISION_TOLERANCE` of each other, the extra precision cannot move the outcome: do **not** escalate, and report e.g. `1 grader (decision-insensitive: bid cap $34-$37 across 6.0-7.5 vs est_close $22, same buy call; probe)`. Otherwise escalate to the panel.

**Escalate by default whenever the probe cannot prove insensitivity:** `est_close` reads `unbounded - <reason>`; either probe row has `max_bid: null` (a `flag_reason`, a `needs_manual` refusal, a fetch error); either row's `source` starts with `skipped_`; or the range is a single point (`lo` equals `hi` gives no range to probe, so the gate has nothing to test and the value trigger stands). Quote the two probe `--brief` lines in the report. This is the highest-leverage skip on a value-gated scan: it stops a 3-grader panel from pinning a grade whose imprecision doesn't reach the bid. (The gate only suppresses escalation that provably can't matter.)

**Dispatch mechanics:**
- Split the candidates by the printed **tier** from Step 1 (`cheap` / `not-cheap`) — a known key always counts as not-cheap regardless of its printed tier.
- **Cheap books → batch them.** A cheap book only ever earns 1 grade unless a gate trips, so there is no cross-grader independence to preserve — grade several in **one** agent context instead of one agent each. Group the cheap books into batches of up to `BATCH_MAX` and give each batch a single grader agent that grades every book in the group **independently** and returns one full OUTPUT FORMAT block per book (clearly delimited, labelled by item id). This is the main first-pass cost saver on a thin-photo seller scan (e.g. 7 cheap books → 2 agents, not 7).
- **Not-cheap books, gate on (`SENSITIVITY_GATE=on`, BUI-1085) → one seat first.** A not-cheap book whose only trigger is value (trigger 1) is dispatched as a single seat, exactly like a not-cheap single: one seat name, no `policy`. Probe it (the decision-sensitivity gate above), then escalate only if the gate says escalate: a second `grade-seats` call lists the remaining seats B and C (no `policy`, as for a cheap-book escalation; the first seat counts as A). Two kinds of not-cheap book bypass the gate and take the two-first path below as written: a known key (value is not its only trigger) and a pre-1980 book with eight or more photos (the `grade-seats` up-front trigger 5, known before any seat runs). A book that also trips trigger 2 or 3 after its first seat escalates without a probe.
- **Not-cheap books, gate off, and the bypass cases above → a two-first panel (BUI-1098).** With the gate off, a not-cheap book always earns the panel (trigger 1), but its third seat is dispatched only when the first two disagree or doubt themselves. Give the book three seat names and `"policy": "two-first"` in the seat spec (Headless seats below): `grade-seats` runs seats A and B in parallel, then runs C only when a third-seat trigger fires:
  1. A and B differ by 0.5 or more;
  2. either seat names a `GRADE CAP` (anything other than none);
  3. either seat reports CONFIDENCE at or below MEDIUM-LOW;
  4. a seat failed twice (one grade cannot show agreement);
  5. the book is pre-1980 with eight or more photos (post-triage). This one is known before any seat runs, so `grade-seats` dispatches all three at once. It is not a value-only trigger, so it bypasses the decision-sensitivity gate.

  When none fires, the third seat is skipped and the two seats' consensus follows Step 3's two-seat rule. On the 2026-10-01 benchmarks a third seat on an agreeing book (ASM #194) never moved the result, and each seat costs about 200k cache-read.
- **Escalation (cheap books).** After the first pass, a batched cheap book that tripped a gate (Step 2 triggers) gets the **remaining 2** graders as separate, independent agents, dispatched together in one parallel batch. Its first grade counts as grader A; pull it out of the batch and add B + C (no `policy`: list only B and C). (The grader prompt and criteria are identical across passes so the panel grades stay independent and comparable.)
- **Crop directories (BUI-911).** Every dispatched grader agent — a not-cheap single grader, a batched cheap-book agent, or one seat of the escalation panel — gets its own **CROP DIRECTORY** input (see [Grader Agent](#grader-agent) below): that book's `IMAGE FOLDER` plus a subdirectory suffixed with the agent's own distinct name (see Common Mistakes below on naming), e.g. `<workdir>/comic-1/crops-grader-c1-a`. Keying by book alone is not enough — the escalation panel's B and C are dispatched together and grade the **same book concurrently**, so a directory named only after the book would still let them collide on the same generic zone filenames (`f_spine_bot.jpg`, etc.); keying by agent name too is what prevents that. A batched cheap-book agent gets one such subdirectory per book in its batch (same agent name, different `comic-N` parent), so books within a batch never collide with each other or with any other agent's crops.
- **Headless seats (BUI-1090/1095, `SEAT_DISPATCH = headless`).** Every dispatched grader in this section (a not-cheap single, a batched cheap-book agent, or a panel seat) runs as a background `claude -p` process, not an Agent tool spawn (about 15k tokens of fixed prompt per call against about 36k). The whole seat loop is one script, `grade-seats` (BUI-1095), so Step 2 is **one Bash turn** and the orchestrator never carries seat envelopes in context. Run it from the repo root:
  1. Write the seat spec to `<workdir>/seats.json`. One entry per book, with the seat names this section assigns (distinct per seat, see Common Mistakes), the seller-stated grade or `null`, and a shared `batch` string for cheap books that go to one batched seat (the batch's first seat name is used):
     ```json
     {"workdir": "<workdir>",
      "books": [{"item_id": "178057470740", "comic": "Fantastic Four #48 (1966)", "folder": "comic-1",
                 "seats": ["grader-c1-a", "grader-c1-b", "grader-c1-c"], "seller_grade": null,
                 "policy": "two-first", "batch": null}]}
     ```
     `folder` is relative to `<workdir>` or absolute. A first pass lists each not-cheap book with its three seats and `"policy": "two-first"` (exactly three seats, never with `batch`; the script decides whether the third runs) and one batch seat per cheap group; an escalation call lists the remaining seats of the cheap books that tripped a gate, with no `policy`. The script reads the year for the pre-1980 trigger from the last year in `comic` (or an optional `"year"` field), so always put the year in `comic`.
  2. Run `grade-seats <workdir>/seats.json` **in the foreground** (Bash timeout 600000 ms, never `run_in_background`; add `--json` for machine-readable output). The seats are children of that process: if you background it and end your turn, a headless run exits and kills every seat, leaving zero-byte seat JSON (2026-10-02, an Opus orchestrator; Fable had happened to run it in the foreground). Wait for it to return before the next step. It does all of the following, so none of it is done by hand:
     - runs `grade-crops` once per book into `<folder>/crops-shared` and passes the paths to every seat as SHARED CROPS (BUI-1084; omitted when `grade-crops` is not on PATH);
     - writes `empty-mcp.json` and `grader-body.md` (`.claude/agents/comic-grader.md` with frontmatter stripped) into `<workdir>`, and each seat's job text to `<folder>/job-<seat>.txt`: the dynamic inputs in [Grader Agent](#grader-agent), then the `HARNESS:` line that overrides the agent body's SendMessage step;
     - launches every seat in parallel with `claude -p --model claude-fable-5-1 --system-prompt-file <workdir>/grader-body.md --tools Read,Bash --strict-mcp-config --mcp-config <workdir>/empty-mcp.json --max-turns 8 --output-format json` (never `--allowedTools`, BUI-1089), with a per-seat timeout (`GRADE_SEATS_TIMEOUT`, default 900 s);
     - **applies the two-first policy** (BUI-1098) to each `"policy": "two-first"` book: dispatches A and B (all three up front for a pre-1980 book with eight or more photos), reads both after their retries, and dispatches C only when a third-seat trigger fires (Dispatch mechanics above);
     - **retries a failed seat once.** A seat has failed on a non-zero exit, a timeout, an unreadable or `is_error` envelope, or no OUTPUT FORMAT block (for a batched seat, a missing book counts as failed for that book). The retry reuses the job text under `job-<seat>-retry.txt` (`job-adjudicator-retry.txt` for the adjudicator), so the failed envelope survives for the usage table;
     - **runs one adjudicator seat** (BUI-1100, replacing BUI-1090's per-seat re-runs) on every book whose seats split by 1.0 or more, after a two-first book's third seat when one ran: copies each seat's ad hoc crops to `<folder>/xexam/`, then launches one more headless process, seat name `adjudicator`, with the same grader body and a job text (`job-adjudicator.txt`) carrying every first-pass seat's full OUTPUT FORMAT block, each seat's `xexam/` crop paths, and the shared crops, plus an ADJUDICATION instruction. It returns one reconciled OUTPUT FORMAT block after a `RECONCILED BLOCK` line, then a `SEAT FINDINGS` line with one line per first-pass seat naming which of that seat's defect claims it confirmed (each visible in a named photo or crop) or rejected and why. At most once per book. A batched book gets no adjudicator in the call that graded it: escalate it in a later call with its own seats. If the adjudicator fails twice, the first-pass grades stand and the block says so;
     - parses every envelope and prints the compact blocks below.
  3. Read the output. Each book prints one line per seat with its first-pass result (`grade | range | confidence | cap | defect`); on a split book an `ADJ adjudicator:` line in the same shape follows, with its per-seat confirm/reject lines indented under it. Then `Seats: N of M returned`; for a two-first book, `Two-first: agreed, third seat skipped; two-seat consensus <grade> | <range> | <confidence>`, `Two-first: third seat dispatched (trigger: <triggers>)`, or `Two-first: dispatched up front (trigger: pre-1980 (<year>) with N photos)`; then `Adjudicated: split X, adjudicator grade Y` (or `adjudicator FAILED twice, first-pass grades kept`, or `no`), a `Caveats:` line from the adjudicator's PHOTO LIMITATIONS (else the first seat's), then one usage table across every attempt, with an `adj` pass row for each adjudicator attempt. A failed seat prints `FAILED`. Exit code 0 means every book has at least one seat; 1 means a book has none.
  **Never adjudicate a book with fewer seats than you dispatched without saying so:** the book's block names each failed seat, and the consensus is marked as resting on the seats that returned. If a book has a `FAILED` seat, dispatch only that seat through the Agent tool (below) or report it as failed with its stderr's last line (`<folder>/seat-<seat>-retry.stderr`).
  If `grade-seats` is not on PATH, use `SEAT_DISPATCH = agent`.
- **Agent fallback (`SEAT_DISPATCH = agent`).** Run `grade-crops <workdir>/comic-N <workdir>/comic-N/crops-shared` once per book first and pass the printed paths as SHARED CROPS. Spawn the `comic-grader` subagent by type with the dynamic inputs in [Grader Agent](#grader-agent); the subagent returns its block through SendMessage (its own step 9). When a book's panel splits by 1.0 or more, adjudicate it once: copy each seat's `crop-*.jpg` to `<workdir>/comic-N/xexam/` and spawn ONE fresh `comic-grader` (name `grader-cN-adj`) with the book's dynamic inputs plus the same ADJUDICATION inputs `grade-seats` writes (every seat's full OUTPUT FORMAT block, its `xexam/` crop paths, the shared crops, and the reply shape: `RECONCILED BLOCK`, one block, `SEAT FINDINGS`, one confirm/reject line per seat). Never adjudicate by messaging the first-pass seats, and never re-run them. Agent tool spawns report no per-seat usage, so the usage table marks those rows `n/a`.
- **Batching guardrails:** keep each book's images and OUTPUT FORMAT block fully separate in the batched prompt; never let one book's defects bleed into another's grade; if a batch would exceed `BATCH_MAX`, open another agent. When in doubt about a specific cheap book (e.g. it looks near a cap), grade it on its own rather than in the batch.
- **Anti-anchoring:** batched grades must not drift toward the batch's overall quality. The `comic-grader` agent def owns this rule (grade each book on the **absolute** CGC scale; BUI-81) and its own guard enforces it — nothing to restate here.

**Photo triage (BUI-1084).** `grade-photos` drops near-duplicate photos (perceptual hash), images too small or too extreme in aspect to be a cover view, and anything past a 24-photo cap (listing order wins; 24 is the most any holdout listing had, so the cap is a backstop, not a budget), then renumbers the survivors `img-01..img-NN`. It never drops the first image. When it drops any, it prints a `triage kept X of Y photos ... dropped (original numbers): ...` line under that comic. Quote that line in your report (no silent drops); the `N images` count on the comic's line is the post-triage count and is what the seats see.

**Gate status (BUI-1085).** The decision-sensitivity gate is off for standalone `/comic:grade` and on for `/comic:buy` Step 2.5, which passes `SENSITIVITY_GATE=on`. It runs on `comic-fmv --probe` (BUI-1085), which writes nothing to the comics server.

**The gate rarely fires on a typical buy (BUI-1119).** `est_close` is unbounded for any live auction more than an hour from close unless the listing proves the book is Modern Age (1992 or later). `/comic:buy` grades days before close, and most books bought are pre-1992, so nearly every gated book escalates. Measured on 2026-10-04 over the 76 most recent bids: 74 (97%) were unbounded at grading time, 62 for `age unknown` and 12 for `vintage`; 2 Modern books were bounded (ended bids classified from the title alone, every bid assumed graded more than an hour before close). All 40 live listings fetched came from one seller whose listings carry no `Publication Year` or `Era` item specifics and no year in the title. Supplying the year would barely help: of the 58 live books only Amazing Spider-Man #361 (1992) is Modern; the rest are 1966-1988 and would read `vintage`, still unbounded. The unbounded rule stays: a vintage auction's close has no trustworthy multiple (see `estimated_close()` in `grade_photos.py`), and a false bound under-grades an expensive book. Expect the gate to suppress escalation only on Modern auctions, BIN listings, and auctions within an hour of close; other gated books run one seat, a probe that escalates, then seats B and C.

**Token totals (BUI-1084/1095).** Read usage from the headless JSON envelope (`modelUsage`: `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens` per model), not the transcript. Seat processes are separate, so the outer envelope excludes them: take seat usage from the `grade-seats` usage table and add it for a whole-run figure. A headless orchestrator should itself launch lean: `--tools Read,Bash,Write --strict-mcp-config --mcp-config <empty-mcp.json>`, not `--allowedTools`, because under `SEAT_DISPATCH = headless` it needs no other tool (BUI-1107, `docs/reference/headless-grader-runs.md`; runner `scripts/grade-benchmark.sh`). Any headless seat run must pass `--tools Read,Bash --strict-mcp-config --mcp-config <empty-mcp.json>`, never `--allowedTools` (BUI-1089, `docs/reference/headless-grader-runs.md`).

**Required per-comic reporting (no silent caps):** for every comic, state how many graders ran and why — e.g. `1 grader (──$6, range wide but coverage-driven at MEDIUM-LOW)` or `1 grader (──$6, unambiguous)` or `3 graders (──$40 ≥ $25 value threshold)` or `3 graders (grade 5.0 within 0.5 of the 1/2" spine-split cap)`. A not-cheap two-first book reports `2 seats (agreed)` or `3 seats (trigger: <the Two-first line's triggers>)`, e.g. `3 seats (trigger: split 0.5)` or `3 seats (trigger: pre-1980 (1974) with 16 photos)`. When the tier came from an estimated close rather than a final price, pass Step 1's printed reason through verbatim — e.g. `3 graders (──$4.25 but not-cheap: estimated vintage 1964, 4d left)` — so a reader can see that the $25 line was crossed by an estimate, not by the price on the line. The user must be able to see where rigor was and wasn't spent.

### Grader Agent

The grading persona — the full CGC/Overstreet scale, criteria, PRINT-LAYER / WRITING / GRADE-CAPPING / restoration rules, coverage-driven CONFIDENCE, the SELLER-STATED-GRADE prior, the procedure, and the **OUTPUT FORMAT contract** — lives in the **`comic-grader` subagent** (`.claude/agents/comic-grader.md`), scoped to `Read, Bash`: it writes nothing except scratch crops, and only inside the CROP DIRECTORY you assign it (BUI-911, see below). Under `SEAT_DISPATCH = headless` that file, frontmatter stripped, is each seat's `--system-prompt-file`; under `agent`, invoke it **by type** (`comic-grader`). Either way, do not paste a grading prompt inline. This keeps the most-tuned prompt (BUI-81 anti-anchoring) in one place and its OUTPUT FORMAT aligned with `/comic:fmv` input.

The OUTPUT FORMAT block the agent returns (`GRADE`, `GRADE RANGE`, `CONFIDENCE`, `GRADE CAP`, defects, etc.) is the contract Step 3 parses — if you ever change it, change it in the agent def, not here.

**Dynamic inputs to pass per invocation** (the only per-comic data the agent doesn't already carry; a headless seat's job file holds these plus the harness line from Dispatch mechanics):

- **COMIC + YEAR** — e.g. `Fantastic Four #48 (1966)`
- **IMAGE FOLDER** — e.g. `<workdir>/comic-1`, where `<workdir>` is the path from Step 1's printed `WORKDIR:` line
- **CROP DIRECTORY** (BUI-911) — e.g. `<workdir>/comic-1/crops-grader-c1-a`: this book's `IMAGE FOLDER` plus a subdirectory suffixed with **this agent's own distinct name** (see Common Mistakes below — every dispatched grader needs a distinct name regardless of this input, and that same name is what keeps this directory unique). Where this grader must write any crop it makes; see Dispatch mechanics above for why the agent-name suffix matters (it's what keeps two panel seats grading the same book from colliding).
- **SHARED CROPS** (BUI-1084) — the overview and sheet paths printed by the one `grade-crops` run for this book (see Dispatch mechanics), one per line. The seat Reads them instead of running `grade-crops` itself.
- **IMAGES** — `img-01.jpg` through `img-{N:02d}.jpg` (N photos, after `grade-photos` triage)
- **SELLER-STATED GRADE** — from the listing title/description if present, else `none stated`
- **Item id label** — so batched output blocks are traceable

For a **batched cheap-book agent** (see Dispatch mechanics), hand it the per-comic block above for each book in the group (up to `BATCH_MAX`) and tell it to grade each independently and return one OUTPUT FORMAT block per book, labelled by item id. Each book's block carries its own `IMAGE FOLDER`/`CROP DIRECTORY` pair (`<workdir>/comic-N/crops-<this-agent's-name>`, same agent name across the whole batch, different `comic-N` per book) — never one CROP DIRECTORY shared across the batch's books. The agent's own anti-anchoring guard keeps those batched grades on the absolute scale (§ Step 2 pointer above).

## Step 3: Synthesize Consensus

Adjudicate each comic from the compact blocks `grade-seats` printed (Step 2): the per-seat grade, range, confidence, grade cap, and defect lines are the inputs (1 grader if not escalated). Escalate a book that tripped a gate by a second `grade-seats` call listing its remaining seats.

**When a `Two-first: agreed, third seat skipped` line is present** (BUI-1098), the book rests on two seats that agreed: under 0.5 apart, no `GRADE CAP`, and both above MEDIUM-LOW. Its consensus is the lower of the two grades as the point grade, the union of the two ranges, and the lower of the two confidences (still capped by coverage, item 5 below); `grade-seats` prints it on that line. Combine the two defect lists as in item 7. Two seats that are 0.5 or more apart never reach this rule: the third seat ran, and the three-seat rules below apply.

**When an `ADJ adjudicator:` line is present** (the seats split by 1.0 or more, BUI-1100), adjudicate from it: its grade is the consensus point grade, its range and confidence are the consensus range and confidence (still capped by coverage, item 5 below), and its confirm/reject lines decide the defect summary (keep confirmed claims, drop rejected ones). Do not average it with the first-pass grades; they stay in the table for traceability only. When the block reads `adjudicator FAILED twice`, run items 1 to 7 on the first-pass grades and say the split was not adjudicated.

Otherwise:

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

Report the result per comic using the single table format under **Output** below — there is no separate consensus-only table; that same block *is* the consensus report.

## Output

One format, used for every comic. Present one block per comic:

```
### Comic Title (Year) — Item ID
| Grader | Grade | Range | Confidence |
| A | 5.0 | 5.0–6.0 | MEDIUM-LOW |
| B | 5.0 | 5.0–5.5 | MEDIUM-LOW |
| C | 4.5 | 4.5–6.0 | MEDIUM-LOW |
| **Consensus** | **5.0 (VG/FN)** | **4.5–6.0** | **MEDIUM-LOW** |

Key defects: [2-3 sentence summary of the most important ones]
Positives: [brief]
Caveats: [pulled from the graders' PHOTO LIMITATIONS — what the photos couldn't show]
```

(Single-grader case: one row + the consensus row carrying that grader's grade, range, and coverage-capped confidence — same table, no separate spec. A two-first book whose third seat was skipped has rows A and B, a consensus row from Step 3's two-seat rule, and the line `2 seats (agreed)`; when the third seat ran, the block adds `3 seats (trigger: ...)`.)

When the adjudicator ran, the grader rows keep the first-pass grades, an `Adj` row follows them with the adjudicator's grade, range, and confidence, and the block adds `Adjudicated: split 1.5` plus one line per seat with what the adjudicator confirmed or rejected; the Consensus row carries the adjudicator's result. A failed seat keeps its row with `FAILED` and its stderr's last line.

After every comic's block, compile their **Consensus** rows into one final list — this is the input for `/comic:fmv`:

```
| # | Comic | Item ID | Consensus Grade | Range | Confidence |
|---|-------|---------|-----------------|-------|------------|
| 1 | FF #48 (1966) | 178057470740 | 5.0 VG/FN | 4.5–6.0 | MEDIUM-LOW |
| 2 | ASM #300 (1988) | 123456789 | 8.5 VF+ | 8.5 | HIGH |
```

Then add the per-seat usage table (BUI-1090), one row per seat attempt, copied from the `grade-seats` usage table (first pass, retry, and adjudicator each get a row; the adjudicator's Pass reads `adj`), summed across models, plus a total row:

```
| Book | Seat | Pass | Turns | Output | Cache-read | Cache-create |
|------|------|------|-------|--------|------------|--------------|
| ASM #194 | grader-c2-a | 1 | 5 | 9,120 | 81,400 | 31,200 |
| ASM #194 | grader-c2-a | 2 | 5 | 8,770 | 84,100 | 30,950 |
| **Total** | | | **10** | **17,890** | **165,500** | **62,150** |
```

`Turns` is the envelope's `num_turns`, which counts messages, not API calls (the BUI-1090 benchmark seats reported 34 to 74 under `--max-turns 8`). Count every attempt once; the outer `/comic:grade` envelope never contains seat usage, so nothing here is double-counted against it (see Token totals in Step 2). Agent-fallback rows read `n/a`.

**Carry the Confidence column forward** — `comic-fmv` consumes it (as `grade_confidence`) to haircut the bid cap when grade confidence is low. Map the label to lowercase, preserving all four levels: `HIGH → high`, `MEDIUM → medium`, `MEDIUM-LOW → medium-low`, `LOW → low`. (Don't collapse MEDIUM-LOW into `low` — they haircut differently: MEDIUM-LOW → 0.70, LOW → 0.60.)

Structural photo-based caveats (staple rust, brittleness, restoration, the inherent ±0.5 CGC gap, etc.) are the `comic-grader` subagent's job to always state in its PHOTO LIMITATIONS field (`.claude/agents/comic-grader.md`) — this skill's Caveats line above just passes that field through.

## Integration with /comic:buy

`/comic:buy` accepts grades from this skill. After running `/comic:grade`, pass the consensus grade column directly into the FMV step:

> "Using these grades, for these URLs" → triggers `/comic:buy` to skip Step 1's seller-stated grade and use the photo-assessed grades instead.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Grading from WebFetch text output | WebFetch returns markdown text, not images — useless for visual grading |
| Giving two dispatched agents the same agent name | Use distinct names (e.g., `grader-c1-a`, `grader-c1-b`) for every grader you dispatch — panel seats, not-cheap singles, and batched-cheap agents alike — so results are traceable and each agent's CROP DIRECTORY (derived from its name) never collides with another's (BUI-911) |
| Letting a grader write crops to a shared or hardcoded path (e.g. `/tmp/crop.jpg`) | Every grader gets its own CROP DIRECTORY (Dynamic inputs above); crops must be written there and nowhere else (BUI-911) — the write-side counterpart to the read-side `WORKDIR` namespacing (BUI-440) |
| Escalating every 2-photo lot because its range is wide | A wide range at MEDIUM-LOW/LOW confidence is coverage-driven and does NOT escalate on its own (see Step 2 trigger 2) |
| Overriding a `not-cheap` tier because the current bid on the same line is a few dollars | That disagreement is the point (BUI-917) — a young auction's bid is not its close price. Take the printed tier; the parenthetical says which signal decided |
| Running the decision-sensitivity gate against `current_price` instead of the printed `est_close` | Same BUI-917 trap, one layer up (BUI-992) — `current_price` is not-yet-final, so a comfortable buy call against it can flip by close. Compare bid caps against `est_close`; when it's `unbounded — <reason>`, don't suppress escalation at all |
| Auto-dropping a book in triage because it looks like a beater | Condition is not a triage kill — there's no FMV floor at grade time. FLAG suspected beaters for the user; only DROP un-gradeable photos or confirmed non-matches (Step 1.5). When unsure, KEEP |
| Inflating grade because it's a key issue | Grade physical condition only — key issue premium belongs in FMV, not grade (criteria live in the `comic-grader` subagent, not this skill) |
| Capping the grade over a printed credit/signature | Printed credits, facsimile signatures, barcodes, and price boxes are in the print layer — never defects (PRINT-LAYER RULE, in the `comic-grader` subagent). Only a post-print autograph caps. When unsure, do NOT cap |
| Dispatching all three seats for every not-cheap book | List three seats with `"policy": "two-first"`; `grade-seats` runs the third only on a trigger (Step 2, BUI-1098). Don't list two seats and add the third by hand: the script owns the triggers |
| Averaging or taking the median of two agreeing seats | Two seats that agreed take the **lower** grade and the union of ranges (Step 3, BUI-1098) |
| Re-running every seat of a split panel, or messaging the seats | `grade-seats` runs one adjudicator seat with every seat's block and crops (Step 2, BUI-1100); the agent fallback spawns one fresh grader with the same inputs. Re-running all three cost three times as much for the same correction |
| Adjudicating with a seat missing because its envelope had no OUTPUT FORMAT block | Re-run the seat once, then fall back or report it as failed; the book's block names every failed seat (BUI-1090) |
| Launching a headless seat with `--allowedTools` | Use `--tools Read,Bash --strict-mcp-config --mcp-config <workdir>/empty-mcp.json`; `--allowedTools` ships about 30k of unused tool schemas on every call (BUI-1089) |
| Running `grade-seats` with `run_in_background`, or ending the turn before it returns | Run it in the foreground with a 600000 ms Bash timeout (Step 2). A headless run that exits while it is backgrounded kills all seats and leaves zero-byte seat JSON (BUI-1107) |
