---
name: comic:buy
description: "End-to-end comic bidding workflow. Takes eBay URLs, identifies comics, checks your collection, calculates FMV, and adds snipes to Gixen. Orchestrates /comic:identify, /comic:collection-check, /comic:fmv, and /comic:snipe-add sequentially."
---

# Comic Buy

End-to-end workflow for sniping eBay comic book auctions. This is the orchestrator — it runs the four leaf skills in sequence, passing output between them and stopping at each step for user confirmation.

Each leaf skill is also usable standalone. Use this when the user provides eBay URLs and wants the full flow.

---

## Execution Pattern

Leaf skills that run in sub-agents are split into two marked sections (BUI-361):
an **EXECUTOR CONTRACT** (everything the executing agent must do, self-contained)
and **ORCHESTRATOR NOTES** (gates, decision guidance, carry-forward). Dispatch
those steps as:

> Read `<skill path>` and execute its EXECUTOR CONTRACT with this
> input: \<the step's working-list input\>

Spell out the full path the same way the dispatch bullets below do (e.g.
`~/Projects/comic-pipeline/.claude/commands/comic/collection-check.md`) — not a
bare filename the sub-agent would have to search for.

The sub-agent reads the skill file itself — do **not** re-serialize or digest
the skill body into the dispatch prompt (that pays for the content twice and
drifts from the file). As the orchestrator, read **only** the ORCHESTRATOR
NOTES section of those skills, never their full body.

Steps without a sub-agent dispatch stay inline: read the skill file and follow
it (`identify.md`, `grade.md`) or call the CLI directly (Steps 3 and 5) as each
step says. This keeps the orchestrator in sync with any updates to the leaf
skills without hand-copying their content.

A third pattern applies to `verify.md` (post-BUI-360): it's split into
EXECUTOR CONTRACT / ORCHESTRATOR NOTES the same as `collection-check.md`, but
Step 6 never dispatches an executor and never reads its EXECUTOR CONTRACT at
all — `gixen add-batch --verify` (Step 5) already performed the equivalent of
the executor's call inline, so Step 6 reads *only* `verify.md`'s ORCHESTRATOR
NOTES section, inline, to interpret the verdicts already embedded in that
CLI's output. Treat it like reading the ORCHESTRATOR NOTES of a dispatched
skill — there's just no matching executor dispatch to pair it with.

At each step, present results to the user and wait for approval before proceeding.

### Sub-agent reuse — SendMessage, not respawn (BUI-366)

Sub-agents spawned during a run stay addressable for the whole run. When a
follow-up question or an incremental unit of work lands on context an existing
agent **already holds**, route it to that agent via
`SendMessage({to: <name>, message: ...})` instead of spawning fresh — a respawn
re-fetches and re-instructs for data already sitting in the first agent's
context. This depends on the agent having been named at spawn (an unnamed
agent silently degrades reuse back to a respawn) — the dispatch bullets below
(Step 1, Step 2) already tell you to name the agent when you spawn it.

Reuse targets in this flow:

- **The identifier agent (Step 1)** holds the full `ebay_fetch.py` JSON for
  every listing — item specifics, description text, printing/variant evidence
  that never entered your context. Route follow-ups like "is item N a first
  print or a later printing?" to it via SendMessage instead of a fresh spawn —
  see identify.md § Follow-ups for the full rationale and the 2026-07-16
  example. Current Price and Bids are **not** a reason to message it — BUI-359
  already emits them in the Step 1 table; the pattern is for evidence the
  table doesn't carry.
- **The collection-check executor (Step 2)** holds its loaded EXECUTOR CONTRACT
  and the run's verdicts. A comic added to the working list mid-run =
  SendMessage it the new `{series, issue, year?, variant?}` row for an
  incremental check, rather than respawning an agent that re-reads the
  contract from scratch.

(There is no snipe-add sub-agent to reuse — BUI-360 made Step 5 an inline
`gixen add-batch` call. A late "add one more snipe" is just another inline
`gixen add`/`add-batch` invocation, or an ad-hoc executor per snipe-add.md.)

Spawn fresh when the context *isn't* already held — a concern none of the live
agents has data for. Reuse is about the data an agent holds, not about avoiding
spawns on principle. And reuse never skips a gate: work routed via SendMessage
still goes through the same user approvals as first-pass work.

---

## Step 0: Resolve the server

Resolve `COMICS_SERVER_URL` **once, up front** via the shared comics-server
convention (BUI-172), so every server-touching step — including Step 1's seller
advisory — uses the same resolved URL (BUI-154):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # sets + exports COMICS_SERVER_URL
```

Without this, Step 1's `curl` ran against an unset `COMICS_SERVER_URL` (empty
host) and the seller-reliability advisory silently no-op'd for the whole run —
suppressing the over-grader signal that informs the Step 2.5 photo-grade
decision. The leaf skills still health-gate the server at their own steps.

---

## Step 1: Identify

Read `~/Projects/comic-pipeline/.claude/commands/comic/identify.md` and follow
it — name the identifier subagent it dispatches (e.g. `comic-identifier`) at
spawn time (BUI-366) so it stays addressable for follow-ups later in the run
(see § Sub-agent reuse above and identify.md § Follow-ups).

**Input:** eBay URLs from the user.
**Output:** Identification table (comic, issue, grade, variant, auction vs BIN, **current price**, **bid count**, **seller**).

Gate: user confirms identifications are correct. Flag Buy It Now listings — they're skipped at the Gixen step.

**Keep the Current Price and Bids columns in the working list (BUI-359)** — Steps 4–5
read them for the current-bid-vs-max pre-flight and urgency context. They came free
with the identify fetch; re-asking the identifier subagent for prices mid-flow costs
a round-trip and ~26k tokens for nothing.

### Seller reliability advisory (BUI-78)

For each distinct seller in the table, query the seller's grading track record
(cheap local GET; best-effort — on any error treat as no history and proceed).
`COMICS_SERVER_URL` is already resolved in Step 0, so this advisory actually runs:

```bash
curl -s "$COMICS_SERVER_URL/api/seller-reliability?seller=<seller_username>"
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

Dispatch a sub-agent, naming it at spawn (e.g. `collection-checker`, BUI-366)
so it stays addressable for incremental follow-up checks:

> Read `~/Projects/comic-pipeline/.claude/commands/comic/collection-check.md`
> and execute its EXECUTOR CONTRACT with this input: \<the working list — one
> row per comic: series, issue, the Step 1 Year column exactly as emitted (a
> blank stays blank — never backfill it; see collection-check.md's EXECUTOR
> CONTRACT § Input for the full BUI-316/BUI-129 forwarding rule), and variant
> when present\>

Read only that skill's **ORCHESTRATOR NOTES** yourself — they carry the
dispatch input shape, the hard-STOP rule, and the Step 4 decision gate. Do not
ingest its EXECUTOR CONTRACT.

**Input:** Comic identification table from Step 1.
**Output:** the executor returns the skill's Step 3 table + status banners — collection membership from the server-side cache. Cache may lag LOCG by up to N days (shown as "Cache age" in the results).

**Hard STOP (R11):** if the executor reports it STOPPED, halt the run at this step — never treat a STOP as "not in collection" and never proceed to bidding without real verdicts. Full STOP conditions and the incremental-reuse blast radius are collection-check.md's ORCHESTRATOR NOTES § Hard STOP (R11); this is a pointer, not a restatement.

Gate: user decides whether to skip duplicates or continue (condition upgrades are legitimate). Route stale-cache rows, R42 canonical-match rows (`✅ In collection (canonical)` — the listing's specific variant wasn't in cache but the canonical edition is), and disambiguator-flagged rows (Patterns A–E in the executor's Notes column) through collection-check.md's ORCHESTRATOR NOTES § Step 4: Decision gate — the user resolves each; never act on the raw verdict.

Remove skipped comics from the working list before Step 2.5.

### Duplicate listings of the same comic → Gixen bid group (BUI-363)

When the working list contains **2+ listings of the same comic** (same series +
issue + variant tier) and the user wants **at most one copy**, don't drop the
later-ending copies — keep them all in the working list and mark them as one
**bid-group candidate**. Gixen bid groups make "snipe every copy, win at most
one" safe: per Gixen's documented semantics, once one snipe in a group wins,
the remaining snipes in that group are automatically cancelled.

- Each copy still flows through grade/FMV individually — **per-copy max bids
  may legitimately differ by grade** (a VF copy and a VF+ copy of the same book
  get different caps).
- At Step 5, give every copy the same `"group": N` in its batch row (pick the
  lowest N from 1–10 not already used by a live snipe — check `gixen list`,
  which shows each snipe's group).
- **End-time caveat (from Gixen's FAQ):** groups are only safe when the
  grouped auctions **do not end within ~2 minutes of each other** — Gixen
  cancels siblings *after* a win, so two copies ending near-simultaneously can
  BOTH be bid and both won. If two copies end that close together, group them
  anyway but warn the user and let them pick one to snipe instead.
- If the user wants multiple copies (genuinely distinct variants, or an
  intentional multi-buy), no group — omit the field.
- After a group win, remind the user to run `gixen purge` promptly (see
  snipe-add.md § Bid groups for why this matters for `/comic:collection-add`).

---

## Step 2.5: Grade Ungraded Comics (conditional)

**Run after the collection check** — only grade comics that survived the collection check. No point assessing condition on a comic the user already owns and is skipping.

Inspect the surviving working list for comics where `grade_source` is `"missing"` AND no grade signal appeared in the title or description.

**If any such comics remain:**

Read `~/Projects/comic-pipeline/.claude/commands/comic/grade.md` and follow it for those listings only.

- Pass only the ungraded item IDs — already-graded comics skip this step
- The skill downloads photos via the eBay Browse API and dispatches the **`comic-grader` subagent** by type per comic (value-gated: 1 grader for cheap/unambiguous lots, a 3-grader panel for high-value or boundary-ambiguous ones). The grader persona + OUTPUT FORMAT contract lives in `.claude/agents/comic-grader.md` (scoped `Read, Bash`); grade.md passes it only the dynamic per-comic inputs
- **Decision-sensitivity gate:** grade.md's Step 2 owns this rule (it's written to anticipate exactly this caller) — `/comic:buy` is the flow that *has* the current price and can compute FMV, so it can short-circuit grade.md's escalation to the 3-grader panel by probing FMV at the grade range's endpoints and comparing bid caps within `CAP_DECISION_TOLERANCE`. See grade.md Step 2 for the trigger conditions and constants.
- Use the consensus grade output as the grade for Step 3 — and carry the **Confidence** column forward as `grade_confidence` (FMV uses it to haircut the bid cap when grade confidence is low)

Present the photo-assessed grades to the user before proceeding:

```
| # | Comic | Seller Grade | Photo Grade | Confidence | Source |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | NM- | — | — | seller stated |
| 2 | Fantastic Four #48 | ⚠️ not stated | 5.0 VG/FN | MEDIUM-LOW | photo assessed |
```

Gate: user confirms the assessed grades (or overrides any) before FMV. Map the grade skill's confidence to `grade_confidence` for Step 3, preserving all four levels: HIGH → `high`, MEDIUM → `medium`, MEDIUM-LOW → `medium-low`, LOW → `low` — don't collapse MEDIUM-LOW into `low`; fmv.md owns the haircut each level applies.

**Preserve the raw photo consensus.** Keep the grader's consensus point estimate as its own working-list field (`photo_grade`), distinct from the grade the user confirms/overrides for FMV (`grade`). Step 5 stores `photo_grade` for seller-deviation analytics — it must be the *raw* assessment, never the override.

**If all comics already have a stated grade:** skip grading by default. **Optionally offer to "grade anyway"** for a stated-grade listing — recommended when the Step 1 advisory flagged the seller as an over-grader, or for a high-value book — since photo-grading a stated-grade listing is the only way the seller-deviation signal (BUI-78) accumulates for over-graders (who almost always state a grade). Keep it user-gated, not automatic.

---

## Step 3: FMV

Run `comic-fmv` directly — do not read `fmv.md` mid-flow. The CLI handles fetch (via `ebay-fetch sold-comps`), cache, dedup, IQR, quartiles, confidence rubric, self-exclusion, and DB upsert.

```bash
comic-fmv --batch <working_list.json> --out <results.json> --brief
```

**Input:** Working list JSON: `[{item_id, title, issue, year, publisher?, variant?, grade, grade_confidence?, locg_id?, locg_variant_id?, notes?}, ...]` for the comics that survived collection check (with photo-assessed grades from Step 2.5 if applicable). Pass `publisher` for non-Marvel/DC titles and `variant` for non-base editions — both feed FMV accuracy (BUI-161). Include `grade_confidence` (`high`|`medium`|`medium-low`|`low` — all four levels; fmv.md owns the haircut each applies) for comics graded from photos in Step 2.5; omit it for seller-stated grades — an absent `grade_confidence` means no bid haircut (standard 80% max bid).

**Output:** Human FMV table to stdout, followed by one compact JSON line per row
(`--brief`, BUI-362): `{item_id, comic_id, fmv_id, max_bid, flag_reason,
confidence}`. The full structured JSON still lands at `--out`, but **do not
read the `--out` file into context** — it's dominated by `queries_used` and
`trimmed_pool` (~6k tokens for 7 rows). The brief lines carry everything
Steps 4–5 thread forward; keep `--out` on disk for deep dives only (e.g.
inspecting the comp pool when CV >100%).

Each brief line includes the internal `comic_id` (and `fmv_id`) returned by `POST /api/comics`. These IDs are how `bids.comic_id` / `bids.fmv_id` get populated downstream — capture them now so Step 5 can thread them into `gixen add-batch`. A row with `comic_id: null` means the DB upsert was skipped (no FMV computed, e.g. `n=0`) — that row will not be linkable; flag it before the user approves a max bid.

**Needs-manual rows (BUI-86):** a row whose `flag_reason` is set (`one_sided`, `too_wide`, or `too_sparse` — surfaced directly on its brief line) could not be honestly auto-priced — its `fmv_low`/`fmv_high`/`max_bid` are all `null`. It still has a real `comic_id` (the comic stub was written), so the `comic_id: null` check above will **not** catch it. Gate on `flag_reason` instead: surface these rows as **needs-manual** and do not auto-propose a max bid. The user either hand-prices them (via the `fmv.md` interpolation / CGC-proxy methods) or skips them — never bid the absent number.

### Why Step 3 captures `comic_id`

Carrying `comic_id` forward from here through Step 5 is what fixes the recurring "bids.comic_id and bids.fmv_id are NULL" bug (PER-140) — snipe-add.md owns the full chain (its "Canonical post-FMV invocation" section) and the `--comic-id` vs `--catalog-id` distinction. If the `comic_id` is dropped at any step the snipe still records, but the dashboard loses condition and FMV data for that bid. Step 5 is where most past sessions broke the chain.

Flags worth knowing:
- `--max-age-days N` (default 7) — reuses FMVs already in the comics server's DB if `fmv_updated_at` is recent. **Note (BUI-153):** DB-FMV reuse only fires for books that carry a `locg_id`, but the orchestrated buy flow derives series/issue from the eBay title and never resolves one — so this cache-skip is effectively inert in `/comic:buy` and every run recomputes FMV from comps (the ebay-sold-comps SerpApi response cache still applies). `--max-age-days` engages on the standalone `comic-fmv` path when the batch carries explicit `locg_id`s.
- `--force` — bypasses both SerpApi and DB caches; use only when you suspect a stale comp pool

**Confidence rubric:** fmv.md §8 owns the n/CV thresholds and the wide-window MEDIUM cap. The CLI returns these labels directly (in the human table and on each brief line; the `window` used lives in the `--out` JSON) — surface them as-is in your presentation.

**Flagging rules** (apply when presenting the table to the user):
- `flag_reason` set (on the brief line) → present as **needs-manual (`<reason>`)**, no max bid; user hand-prices or skips (see the needs-manual note in Step 3)
- LOW or MEDIUM-LOW with `n ≤ 3` → call out explicitly; user may want to skip or set a conservative max
- Auction ends within 24h → mark with **⚠️ ends <date>** in the Notes column. Always surface urgency before max-bid approval
- CV >100% → suspect a wild outlier survived; check the comp pool in the JSON output
- "CGC proxy" in Notes → confidence is capped at MEDIUM-LOW regardless of comp count

If the CLI fails, fall back to the manual procedure in `~/Projects/comic-pipeline/.claude/commands/comic/fmv.md`. Otherwise present the table directly.

---

## Step 4: Compute Max Bids

The CLI returns `max_bid = round_clean(bid_factor × fmv_high)` per row. `bid_factor` is `0.80` by default; fmv.md §6 owns the haircut logic that lowers it when grade or comp confidence is low (see Step 2.5 for where `grade_confidence` comes from). When a haircut applied, the row's Notes carry `bid_haircut=…` — that notes string lives only in the `--out` JSON (`db_row.fmv_notes`), so when the brief line's `max_bid` is visibly below 80% of the table's FMV high, extract just that row's notes from the `--out` file (one-line python/jq) rather than reading the whole file. Present the proposed bids:

```
| # | Comic | Grade | FMV Range | Max Bid | Notes |
|---|---|---|---|---|---|
| 1 | Amazing Spider-Man #300 | NM | $800–1000 | $800 | |
| 2 | Invincible #1 | NM (assumed) | $270–320 | $192 | LOW conf, n=2, bid_haircut=0.60 |
| 3 | Batman #609 | NM | $40–50 | $40 | ⚠️ ends 2026-05-11 |
| 4 | Fantastic Four #63 | NM+ 9.6 | needs-manual | — | manual_review=one_sided — hand-price or skip |
```

Clean-number rounding: $5 step below $50, $10 step from $50–$200, $25 step above $200.

A `needs-manual` row (`flag_reason` set) has no CLI-computed max bid. Present it without a proposed number; the user supplies a hand-derived max bid (via the `fmv.md` interpolation / CGC-proxy methods) or skips it. Don't fabricate a max from the absent FMV.

**Current-price context (BUI-359):** the Step 1 table already carries each
auction's **Current Price** and **Bids** — use those columns here, do **not**
re-fetch listings or re-ask the identifier subagent for prices. Flag any row
whose current price is already at or above the proposed max (the user should
raise or skip — see Step 5's pre-flight), and pair Bids with the Ends column
for urgency context (e.g. `12 bids, ends 18h` = contested; `0 bids` = the
seller can still end the auction early).

Gate: user approves or overrides each max bid.

---

## Step 5: Snipe Add (Batch)

**BUI-360:** this step calls `gixen add-batch` **inline** — one CLI invocation for the whole approved list — instead of dispatching to `snipe-add.md`'s per-item prose loop. `gixen add-batch` reuses the exact same server-mode code path as `gixen add` (POST `/api/bids`, then `/api/bids/{id}/link-fmv` when grade+comic_id are both given) and has the BUI-168 mid-batch failure semantics built into the CLI itself: a failed row is marked failed with its error, server health is re-checked before the next row, and the batch halts (remaining rows reported `not_attempted`) if the server goes down mid-run — never an all-✅ summary after a partial failure. `snipe-add.md` is still the right read for a standalone/ad-hoc add outside this flow (it documents the same `add` flags this batch input maps to); it is unchanged by this step.

If `gixen` isn't found, install the monorepo CLIs first: `./scripts/install.sh`.

**1. Bid sanity check (unchanged from `snipe-add.md`'s pre-flight):**

Compare each approved auction's current bid against its proposed max bid, **using the Current Price column already in the Step 1 identification table (BUI-359)** — do not re-fetch listings or re-dispatch the identifier subagent for prices. If current bid ≥ max bid, surface it to the user — Gixen will still register the snipe but it fires below market and won't win. Ask whether to raise the max or skip before proceeding. (Caveat: the Step 1 price is as-of-identify; for an auction that was already contested — high Bids count — and hours have passed, it can lag reality. It's still the right default; only re-fetch if the user asks.)

**2. Build the rows JSON** from the working list carried forward — `item_id` + approved `max_bid` from Step 4, `comic_id` from the Step 3 **brief lines** (BUI-362), numeric `grade` as a CGC float from the working list (Step 2.5 photo grades are already numeric; convert a seller-stated letter grade, e.g. NM- → 9.2, the same mapping snipe-add.md documents for `--seller-grade`), and the `seller`/`seller_grade`/`photo_grade` captured in Steps 1/2.5. Skip Buy It Now listings — Gixen is for auctions only. For a Step 2 bid-group candidate (2+ copies of the same comic, user wants one — BUI-363), set the same `"group": N` on every copy's row; omit `group` otherwise (it defaults to 0 = no group). Write it to a scratch file, e.g.:

```json
[
  {
    "item_id": "123456789",
    "max_bid": 800,
    "comic_id": 42,
    "grade": 9.2,
    "seller": "some_seller",
    "seller_grade": 9.0,
    "photo_grade": 8.5,
    "group": 1
  }
]
```

- Omit `comic_id`/`grade` for a row whose Step 3 `comic_id` was null (FMV upsert skipped, e.g. `n=0`) — the bid still adds, just without FMV linkage (PER-140); surface that to the user rather than fabricating an id.
- Omit any of `seller`/`seller_grade`/`photo_grade` that weren't captured for that comic.
- `--comic-id`'s CLI caveat still applies conceptually: never put the internal `comic_id` where an LOCG id is expected — the batch row schema only has `comic_id` (no `catalog_id`), so this can't happen by construction here.

**Gate: user approves the full batch** (the item_id + max_bid list) before any add is attempted — same approval point `snipe-add.md` gates on, just moved ahead of one CLI call instead of N.

**3. Call the batch add, with verification folded in:**

```bash
gixen add-batch <rows.json> --verify --json-out <results.json>
```

Adds run strictly sequentially inside the CLI (Gixen sessions are stateful — parallel adds fail; this is now enforced in code, not by agent discipline). `--verify` POSTs every landed row **that carries a grade** to `/api/comics/verify` and appends a verdict per row (see Step 6) in the same call — this is what collapses the old separate Step 6 sub-agent into this one invocation.

A non-zero exit code means at least one row did not land (failed or was left not-attempted after a mid-batch halt) — **read the JSON, don't treat non-zero as total failure.** Present the human table the CLI printed (or reformat it with the comic names from Steps 1–4, joined by `item_id`, for readability) and call out any `failed`/`not_attempted` rows explicitly.

**Output:** the add-batch JSON (also at `--json-out`): `{"summary": {...}, "halted": bool, "verify_error": ..., "rows": [{"item_id", "status", "max_bid", "grade", "created", "link_attempted", "link_ok", "error", "link_error", "verify"}, ...]}`. `error` is set only when the row itself failed to add (`status: "failed"`); `link_error` is a separate field set when the add landed but the FMV link call failed (`status` stays `"added"`/`"updated"`) — don't conflate the two when scanning for failures.

When at least one row landed (`added`/`updated`), remind the user:
> Run `/comic:collection-add` after auctions close to record wins. Check `locg collection status` for pending push count before uploading.

If any rows shared a `group` (BUI-363), also remind the user:
> After one copy in the group wins, run `gixen purge` promptly — it removes the group's cancelled sibling snipes from Gixen and tombstones them `REMOVED` in the DB. Left in place past their own auction end, siblings can be mislabeled as results (see snipe-add.md § Bid groups).

---

## Step 6: Verify

Step 5's `--verify` already appended a `verify` verdict to every landed row in its JSON output — this step is now just **interpreting that data**, not making another call. Read **only the ORCHESTRATOR NOTES section** of `~/Projects/comic-pipeline/.claude/commands/comic/verify.md` — it carries the verdict ladder, the per-verdict guidance, and the no-false-all-clear rule (BUI-361; no executor dispatch, the contract half is for standalone runs) — and apply them here:

- **`rows[].verify` is null** — either the row didn't land (`failed`/`not_attempted`, nothing to verify) or it landed without a `grade` (add-batch's `--verify` only submits landed rows that carry a grade, since verify.md's endpoint needs one to match an `fmv` row). Treat a gradeless landed row as **unverified**, not as an implicit `fully_linked` — say so rather than staying silent.
- **`rows[].verify.verdict != "fully_linked"`** — surface the row in a table plus its one-line guidance from `verify.md`'s per-verdict mapping (`needs_manual`, `fmv_stub`, `no_fmv_at_grade`, `no_comic`, `partial`, `no_bid`).
- **top-level `verify_error` is non-null** — the `/api/comics/verify` call itself failed (server hiccup after the adds already landed). Do **not** report an all-clear for verification in this case — say verification could not be confirmed for the landed rows and point at `/comic:verify` as a manual follow-up.

Warn-only — don't block. The pipeline is done; the goal is to tell the user *now* about gaps so they don't discover them when the auction ends and `/comic:collection-add` chokes (PER-70 cascade).

Born from PER-99 — `/comic:buy` ran to apparent success in past incidents while leaving rows partially populated (missing comic row, FMV stub, `bids.fmv_id` null).

---

## Subagent Strategy (30+ books)

`comic-fmv` already does async fan-out internally and serves cache hits without API calls, so the previous "split FMV across subagents at 30 books" rule mostly doesn't apply anymore — one CLI invocation handles 50+ books in a few seconds. Reach for subagents only when:

- The eBay identification step is bottlenecked (Browse API rate limit or large batch)
- You're processing 50+ unique books and want to keep the orchestrator's context tight

Even then, FMV stays as one CLI call from the main agent (so the SerpApi cache is shared). Parallelize identification, not FMV.
