---
name: comic:buy
description: "End-to-end comic bidding workflow. Takes eBay URLs, identifies comics, checks your collection, calculates FMV, and adds snipes to Gixen. Orchestrates /comic:identify, /comic:collection-check, /comic:fmv, and /comic:snipe-add sequentially."
---

# Comic Buy

End-to-end workflow for sniping eBay comic book auctions. This is the orchestrator — it runs the four leaf skills in sequence, passing output between them and stopping at each step for user confirmation.

Each leaf skill is also usable standalone. Use this when the user provides eBay URLs and wants the full flow.

---

## Execution Pattern

Leaf skills split into **EXECUTOR CONTRACT** (self-contained sub-agent
instructions) and **ORCHESTRATOR NOTES** (gates, decision guidance) — BUI-361.
Dispatch a step as:

> Read `<skill path>` and execute its EXECUTOR CONTRACT with this
> input: \<the step's working-list input\>

Use the full path (e.g.
`~/Projects/comic-pipeline/.claude/commands/comic/collection-check.md`), not a
bare filename. As orchestrator, read only the dispatched skill's ORCHESTRATOR
NOTES yourself — never its EXECUTOR CONTRACT, and never re-serialize the skill
body into the dispatch prompt.

Steps without a sub-agent dispatch stay inline: read the skill file and follow
it (`identify.md`, `grade.md`), or call the CLI directly (Steps 3 and 5).
`verify.md` is dispatched neither way at Step 6 — see Step 6 below for how
its ORCHESTRATOR NOTES get read without a matching executor call.

At each step, present results to the user and wait for approval before proceeding.

### Sub-agent reuse — SendMessage, not respawn (BUI-366)

Route follow-up work that lands on data an already-spawned agent holds to that
agent via `SendMessage({to: <name>, message: ...})` instead of spawning fresh
— this only works if the agent was named at spawn (Steps 1 and 2 do this).
Reuse targets:

- **Identifier agent (Step 1)** — holds the full `ebay_fetch.py` JSON (item
  specifics, description text, printing/variant evidence). Route follow-ups
  like "is item N a first print?" to it; see identify.md § Follow-ups.
- **Collection-check executor (Step 2)** — holds its EXECUTOR CONTRACT and
  verdicts. A comic added mid-run = SendMessage it the new row, not a respawn.

There is no snipe-add sub-agent to reuse (Step 5 is inline). Reuse never skips
a gate — routed work still goes through the same approvals as first-pass work.
Full BUI-361/BUI-366 rationale: `docs/solutions/workflow-issues/buy-orchestrator-dispatch-pattern-and-comic-id-chain.md`.

---

## Step 0: Resolve the server

Resolve `COMICS_SERVER_URL` **once, up front** via the shared comics-server
convention (BUI-172), so every server-touching step — including Step 1's seller
advisory — uses the same resolved URL (BUI-154):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # sets + exports COMICS_SERVER_URL
```

Without this, Step 1's seller-reliability advisory silently no-ops for the whole run (BUI-154). The leaf skills still health-gate the server at their own steps.

---

## Step 1: Identify

Read `~/Projects/comic-pipeline/.claude/commands/comic/identify.md` and follow it — name the identifier subagent (e.g. `comic-identifier`) at spawn time (BUI-366) so it stays addressable for follow-ups (§ Sub-agent reuse above; identify.md § Follow-ups).

**Input:** eBay URLs from the user.
**Output:** Identification table (comic, issue, grade, variant, auction vs BIN, **current price**, **bid count**, **seller**).

Gate: user confirms identifications are correct. Flag Buy It Now listings — they're skipped at the Gixen step.

**Keep the Current Price and Bids columns in the working list (BUI-359)** — Steps 4–5 need them for the current-bid-vs-max pre-flight and urgency context; they're already in the Step 1 fetch, so don't re-ask the identifier subagent for prices mid-flow.

### Seller reliability advisory (BUI-78)

For each distinct seller in the table, query their grading track record (cheap local GET; best-effort — on error treat as no history and proceed). `COMICS_SERVER_URL` is already resolved in Step 0, so this actually runs:

```bash
curl -s "$COMICS_SERVER_URL/api/seller-reliability?seller=<seller_username>"
```

When `sample_size >= 1`, surface a line before the grade (Step 2.5) / bid-aggressiveness decision:

> ⚠️ Seller `beatlebluecat` has over-stated condition by ~**+1.5** grade points (n=4 prior assessments). Consider photo-grading this listing to verify condition before bidding.

- `avg_deviation` is `seller_grade − photo_grade` (**positive = over-grades**; render with an explicit sign). For `sample_size` 1–2, prefix **"early signal —"** and soften the wording.
- `sample_size == 0` (or any error / no server): show nothing.
- Advisory only — it never changes the grade, FMV, or max bid automatically.

---

## Step 2: Collection Check

The check is **one CLI call** (BUI-504) — no executor sub-agent. Build
`items.json` from the working list (one entry per comic: `series`, `issue`, the
Step 1 **Year column exactly as emitted** — a blank stays blank, never backfill
it; the CLI owns the BUI-316/BUI-129 cover-year forwarding rule — and `variant`
when present), then:

```bash
locg collection check-batch items.json --table
```

`items.json` is `{"items":[{"series","issue","year"?,"variant"?}]}`. The CLI
resolves the comics server, health-gates it, runs the batch check, applies the
stale-cache downgrade, and computes the advisory false-match flags (Patterns
A / C / D / D2 / D3 / E) in the `Notes` column. Present the `--table` output —
collection membership from the server-side cache (may lag LOCG by up to N days,
shown as "Cache Age").

**Hard STOP (R11) is the exit code:** a **non-zero exit** means the check
failed (unreachable server, non-200, timeout, never-imported 409) and rendered
NO verdicts. Halt the run at this step and tell the user — never treat a
non-zero exit as "not in collection", and never proceed to bidding without real
verdicts (a `0` exit).

Gate: user decides whether to skip duplicates or continue (condition upgrades
are legitimate). Route stale-cache rows and flagged rows (Patterns A / C / D /
D2 / D3 / E in the `Notes` column) through collection-check.md's § Step 4
decision gate — the user resolves each; the flags FLAG, never DECIDE, so never
act on the raw verdict of a flagged row.

Remove skipped comics from the working list before Step 2.5.

### Duplicate listings of the same comic → Gixen bid group (BUI-363)

When the working list has **2+ listings of the same comic** and the user wants
**at most one copy**, keep every copy in the working list (don't drop
later-ending ones) and mark them a bid-group candidate — each copy still flows
through grade/FMV individually (per-copy max bids may legitimately differ by
grade). At Step 5, give every copy the same `"group": N`; omit it if the user
genuinely wants multiple copies (distinct variants, intentional multi-buy).

The four bid-group rules (picking N, the ~2 min end-time caveat, and why
`gixen purge` is hygiene rather than the safety net) live in `snipe-add.md`
§ Bid groups and `docs/solutions/conventions/bid-group-purge-is-hygiene-not-safety-net.md`
— this section only carries the buy-flow-specific part: candidates surface
here at Step 2, get applied at Step 5.

---

## Step 2.5: Grade Ungraded Comics (conditional)

**Run after the collection check** — only grade comics that survived it (no point assessing condition on a comic the user is skipping). Inspect the surviving working list for comics where `grade_source` is `"missing"` AND no grade signal appeared in the title or description.

**If any such comics remain:** read `~/Projects/comic-pipeline/.claude/commands/comic/grade.md` and follow it for those listings only.

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

**Output:** Human FMV table to stdout, followed by one compact JSON line per row (`--brief`, BUI-362): `{item_id, comic_id, fmv_id, max_bid, flag_reason, confidence}`. The full structured JSON still lands at `--out`, but **do not read the `--out` file into context** — it's dominated by `queries_used`/`trimmed_pool` (~6k tokens for 7 rows). The brief lines carry everything Steps 4–5 need; keep `--out` on disk for deep dives only (e.g. inspecting the comp pool when CV >100%).

Each brief line includes the internal `comic_id` (and `fmv_id`) returned by `POST /api/comics`. These IDs are how `bids.comic_id` / `bids.fmv_id` get populated downstream — capture them now so Step 5 can thread them into `gixen add-batch` (via `gixen build-batch`, BUI-435). A row with `comic_id: null` means the DB upsert was skipped (no FMV computed, e.g. `n=0`) — that row will not be linkable; flag it before the user approves a max bid. Full rationale for why this capture matters (PER-140): `docs/solutions/workflow-issues/buy-orchestrator-dispatch-pattern-and-comic-id-chain.md`.

**Needs-manual rows (BUI-86):** a row whose `flag_reason` is set (`one_sided`, `too_wide`, or `too_sparse` — surfaced directly on its brief line) could not be honestly auto-priced — its `fmv_low`/`fmv_high`/`max_bid` are all `null`. It still has a real `comic_id` (the comic stub was written), so the `comic_id: null` check above will **not** catch it. Gate on `flag_reason` instead: surface these rows as **needs-manual** and do not auto-propose a max bid. The user either hand-prices them (via the `fmv.md` interpolation / CGC-proxy methods) or skips them — never bid the absent number.

Flags worth knowing:
- `--max-age-days N` (default 7) — reuses FMVs already in the comics server's DB if `fmv_updated_at` is recent; rarely engages inside `/comic:buy` since this flow never resolves a `locg_id` (BUI-153 — see the doc link above for why).
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

This step calls `gixen add-batch` inline (BUI-360) instead of dispatching `snipe-add.md`'s per-item loop — the BUI-168 mid-batch failure semantics (failed-row marking, health re-check, halt-on-dead-server) are enforced by the CLI itself; see `packages/gixen-cli/add_batch.py`. `snipe-add.md` documents the same `add` flags this batch input maps to, for a standalone/ad-hoc add outside this flow.

If `gixen` isn't found, install the monorepo CLIs first: `./scripts/install.sh`.

**1. Bid sanity check:** same rule as `snipe-add.md`'s pre-flight — compare each approved auction's current bid against its proposed max bid using the Step 1 Current Price column (BUI-359), don't re-fetch. If current bid ≥ max bid, surface it and ask whether to raise the max or skip before proceeding.

**2. Build the rows JSON with `gixen build-batch`** (BUI-435) — a tested,
deterministic transform, not a hand-merge:

```bash
gixen build-batch fmv_brief.json working_list.json --overrides overrides.json --out rows.json
```

- `fmv_brief.json` — Step 3's `--brief` output, saved as-is (the raw captured
  stdout works too — the builder extracts the JSON lines itself).
- `working_list.json` — the working list carried forward: one object per
  comic with `item_id`, `grade` (numeric, or a CGC letter grade like `NM-` —
  the builder converts it), `listing_type` (`Auction`/`BIN` from Step 1 —
  BIN rows are skipped automatically), `seller`/`seller_grade`/`photo_grade`
  (Steps 1/2.5), and `group` (the Step 2 bid-group candidate, if any).
- `--overrides overrides.json` — only needed when Step 4's gate changed
  anything from the CLI-computed default: `{"<item_id>": {"max_bid": ...,
  "group": ..., "skip": true}}`. Omit the flag when nothing was overridden.

The builder never drops `comic_id` (PER-140) and never fabricates a max bid
for a needs-manual row — it hard-fails (`AddBatchError`, nonzero exit) instead
if a working-list item has no matching brief row, or a needs-manual row has
neither an override nor `skip`. Fix the input and re-run rather than patching
`rows.json` by hand.

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
> After one copy in the group wins, `gixen purge` will clean up the group's cancelled sibling snipes on Gixen and in the DB. Handy for a tidy list, but not required for correctness — the server now classifies an unpurged sibling `REMOVED` on its own once its auction ends (see snipe-add.md § Bid groups).

---

## Step 6: Verify

Step 5's `--verify` already appended a `verify` verdict — and, as of BUI-507, a server-provided `guidance` string — to every landed row in its JSON output. This step is now just **interpreting that data**: no second call, and no need to read `verify.md`.

- **`rows[].verify` is null** — either the row didn't land (`failed`/`not_attempted`, nothing to verify) or it landed without a `grade` (add-batch's `--verify` only submits landed rows that carry a grade). Treat a gradeless landed row as **unverified**, not as an implicit `fully_linked` — say so rather than staying silent.
- **`rows[].verify.verdict != "fully_linked"`** — surface the row in a table plus its `rows[].verify.guidance` string, verbatim.
- **top-level `verify_error` is non-null** — the `/api/comics/verify` call itself failed (server hiccup after the adds already landed). Do **not** report an all-clear for verification in this case — say verification could not be confirmed for the landed rows and point at `/comic:verify` as a manual follow-up. A failed call is "verification failed", never "nothing to flag" (BUI-169).

Warn-only — don't block. The pipeline is done; the goal is to tell the user *now* about gaps so they don't discover them when the auction ends and `/comic:collection-add` chokes (PER-70 cascade). Origin (PER-99) and full context: `docs/solutions/workflow-issues/buy-orchestrator-dispatch-pattern-and-comic-id-chain.md`.

---

## Subagent Strategy (30+ books)

`comic-fmv` fans out internally and caches hits, so one CLI call handles 50+ books in seconds — reach for subagents only if eBay identification itself is rate-limited/large, or you want the orchestrator's context tight above 50+ books. FMV always stays a single CLI call regardless (shared SerpApi cache) — parallelize identification, not FMV.
