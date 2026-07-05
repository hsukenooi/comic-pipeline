---
title: "refactor: collection-add simplification & token efficiency"
status: active
date: 2026-07-05
type: refactor
linear_project: "Collection-Add: Simplification & Token Efficiency"
tickets: [BUI-291, BUI-292, BUI-293, BUI-294, BUI-295]
---

# refactor: collection-add simplification & token efficiency

## Summary

Complete the five Linear tickets in the **Collection-Add: Simplification & Token Efficiency** project. These make `/comic:collection-add` cheaper and simpler for an agent to run — the recommendations came out of the 2026-07-05 run that processed 102 wins and confirmed three concrete costs: a ~4–5k-token skip-detail blob dumped into agent context, a documented per-title `comic-identify` fan-out (102 invocations), and ~60–70 lines of dual-source pull logic that has no correctness payoff. Two tickets are pure skill-body edits, one is a one-liner, and two are additive `comic_identify.py` changes with a test.

Work lands as **two branches / two PRs**:
- **Branch A** (`refactor/collection-add-skill-BUI-291-293-294`) — the three edits to `.claude/commands/comic/collection-add.md`, as three commits (one per issue). Grouped because they touch the same file; separate branches would rebase-conflict.
- **Branch B** (`feat/comic-identify-batch-variant-BUI-292-295`) — the two `apps/ebay/src/comic_identify.py` changes (batch mode + `variant_text`) plus the Step 2 skill edit that consumes batch mode.

---

## Problem Frame

`/comic:collection-add` is agent-run every time comics are won. Its per-run token cost and prose complexity scale badly:

- **Context bloat (BUI-291):** the `record-win` 200 response carries `skipped_already_owned_detail` (one object per already-owned win — 83 this run). The skill reads the whole response into context, and its documented success example (Step 3, ~line 207) shows only 5 scalar fields, so nothing warns the agent the real payload is large or tells it to filter. Worst-case grows with collection size.
- **Fan-out (BUI-292 / BUI-295):** Step 2 documents `comic-identify "<one title>"` — one invocation per snipe. A literal reading issues ~102 Bash round-trips, each echoing a JSON line into context. There is a BUI-204 batch collection-check endpoint but no batch identify. Separately, `comic-identify` emits no `variant_text`, so the agent hand-writes a Newsstand/Direct regex each run (ephemeral, inconsistent).
- **Dead complexity (BUI-293):** Step 1 spends ~75 lines plus two Common-Mistakes rows explaining `/api/comics/history` vs `gixen list --json` and a `time_to_end=="ENDED"` subtlety the skill itself calls "redundant, not harmful." The skill states the source choice "only affects completeness, not correctness" because the Step 1b seen-set is the real dedup — so the branch is reader tax with no correctness value.
- **Minor uncleanliness (BUI-294):** Step 3b builds the seen-set `item_ids` list without dedup; a lot that expands to N entries sharing one `item_id` POSTs that id N times (idempotent, harmless, just noisy).

## Scope Boundaries

**In scope:** the five tickets above — edits to `.claude/commands/comic/collection-add.md`, `apps/ebay/src/comic_identify.py`, and `apps/ebay/tests/test_comic_identify.py`.

**Deferred to Follow-Up Work:**
- Server-side `?verbose=true` gating of `skipped_already_owned_detail` on `api_record_win` (BUI-291 names it as the heavier alternative; the skill-side fix is preferred and sufficient). File a ticket only if the skill-side filter proves insufficient.

**Non-goals:** no change to record-win semantics, the seen-set contract, the export path, or the `/api/comics/history` endpoint itself. `comic_identify.py` changes are strictly additive — the single-title and stdin signatures that `/comic:identify` depends on must keep working byte-for-byte.

---

## Key Technical Decisions

- **Group the three skill-only edits on one branch (KTD-1).** BUI-291/293/294 all edit `collection-add.md`. Per-issue branches off `main` would each modify the same file and conflict on rebase/merge. One branch with three commits preserves commit-per-issue traceability (CLAUDE.md) without the conflict tax.
- **Batch mode is additive, not a replacement (KTD-2).** `comic_identify.py` currently takes a single positional `title` (or stdin). Add a batch input mode (a `--batch` flag reading newline-delimited titles from a file/stdin, emitting one JSON result per line — JSONL) alongside the existing single-title path. `/comic:identify` and any other caller of the single-title form must be unaffected — this is the back-compat gate BUI-292 calls out.
- **Reuse existing Newsstand/Direct logic for `variant_text` (KTD-3).** `comic_identity.py` already detects Newsstand/Direct distribution variants (≈ lines 584–606). BUI-295 should surface that as a `variant_text` field on `ComicIdentity` (or in the CLI dict projection) rather than adding a second, divergent detector. `variant_text` is empty string when no variant marker is present — matching the skill's `identify_data.variant_text` "omit or empty" contract.
- **Standardize Step 1 on `gixen list --json` (KTD-4).** It is always-correct and needs zero date reasoning; the seen-set already excludes prior wins. Collapsing to one source deletes the field-mapping table and both history-related Common-Mistakes rows. Keep a one-sentence optional note that `/api/comics/history` is a lighter fast-path when the window is known-covered — as an optimization aside, not a co-equal branch the agent must reason about.

---

## Implementation Units

### U1. BUI-294 — dedup item_ids before the seen-set POST

**Goal:** Stop POSTing a lot's shared `item_id` N times in Step 3b.
**Ticket:** BUI-294. **Dependencies:** none (do first — smallest, zero-risk warm-up).
**Files:** `.claude/commands/comic/collection-add.md` (Step 3b snippet, ≈ line 229).
**Approach:** Change the list comprehension to preserve-order-dedup: `ids=list(dict.fromkeys(w['item_id'] for w in ...))`. Endpoint is already idempotent so behavior is unchanged; this is a cleanliness/correctness-of-intent fix.
**Verification:** Re-read the Step 3b snippet — the built `item_ids` list has no duplicates for a multi-entry lot sharing one `item_id`. No test suite for the skill body.
**Test expectation:** none — one-line skill-body edit, no runtime code under test.

### U2. BUI-291 — filter record-win response to a file + summary scalars

**Goal:** Keep the large `skipped_already_owned_detail` blob out of agent context while retaining it on disk.
**Ticket:** BUI-291. **Dependencies:** none.
**Files:** `.claude/commands/comic/collection-add.md` (Step 3, ≈ lines 197–219).
**Approach:** Mirror Step 4's export pattern — write the POST response to a temp file (`-o /tmp/record_win_response.json`, `-w '%{http_code}'` to preserve the loud-fail behavior `curl -sf` gives today), then `jq`/`python3` out only `{rows_written, skipped_already_owned, manual_variant_count, manual_series_count, metron_lookups_succeeded, partial_failure}` for context. **Critical:** the partial_failure (HTTP 500) branch must still read `rows_written` from the saved file so the "which wins didn't commit" signal survives — don't let the file-redirect swallow the 500 detail the skill relies on today (BUI-137). Update the documented success example so it reflects the filtered shape and notes the full detail is on disk.
**Approach note:** verify the `curl -sf` → `-o` + `-w` swap keeps a non-2xx as a hard stop (today `-sf` exits non-zero on 500). If `-w` alone doesn't preserve that, keep `-f` and capture the file in both success and failure paths.
**Verification:** Re-read Step 3 — success path surfaces only scalars; failure path (500/partial_failure) still surfaces `rows_written`; full response is retained at a stable temp path.
**Test expectation:** none — skill-body edit.

### U3. BUI-293 — collapse the dual-source pull to one source

**Goal:** Delete the ~60–70 lines of history-vs-CLI branching and the redundant `time_to_end` explanation.
**Ticket:** BUI-293. **Dependencies:** none (independent of U1/U2 within the same file — sequence after them to keep diffs clean).
**Files:** `.claude/commands/comic/collection-add.md` (Step 1 ≈ lines 39–114; Common-Mistakes rows ≈ lines 299–300).
**Approach:** Rewrite Step 1 to a few lines: pull `gixen list --json`, filter to `time_to_end=='ENDED'` AND `status` contains `WON` (case-insensitive), dedup by `item_id`. Drop the "Decision rule" block, the Source A/Source B split, and the history field-mapping table. Keep one optional sentence that `/api/comics/history` is a lighter fast-path when the last run is known ≤7 days ago (optimization aside only). Remove both history-related Common-Mistakes rows (the `time_to_end` redundancy row and the ">7-day window" row) since the branch they warn about is gone. Ensure Step 1b's seen-set text still reads coherently now that "source choice" language upstream is simplified.
**Verification:** Re-read Steps 1 → 1b → 2 end-to-end: a fresh agent can pull, filter, dedup, and reach Step 2 without reasoning about which source or about `time_to_end` semantics. Confirm no dangling reference to "Source A/B", the field-mapping table, or the removed Common-Mistakes rows elsewhere in the file.
**Test expectation:** none — skill-body edit.

### U4. BUI-292 + BUI-295 — comic-identify batch mode and variant_text

**Goal:** Add a batch input mode to `comic-identify` and emit `variant_text`, then point Step 2 at batch mode.
**Tickets:** BUI-292 (batch), BUI-295 (variant_text) — folded per the project note (same file). **Dependencies:** U1–U3 land first on Branch A; this is Branch B.
**Files:**
- `apps/ebay/src/comic_identify.py` (add `--batch` mode; add `variant_text` to the emitted dict)
- `apps/ebay/src/comic_identity.py` (expose the existing Newsstand/Direct detection as a `variant_text` value if the field lives on `ComicIdentity`; **prefer** projecting it in the CLI dict to keep the library contract untouched — decide during implementation)
- `apps/ebay/tests/test_comic_identify.py` (new scenarios)
- `.claude/commands/comic/collection-add.md` (Step 2: replace the per-title `comic-identify "<one title>"` example with a single batch invocation writing JSONL to a file; drop the "infer variant_text from the title as before" instruction now that the field is emitted)
**Approach:**
- *Batch (BUI-292):* add a `--batch` flag (or a `--batch-file PATH` / stdin-of-many mode) that reads newline-delimited titles and prints one JSON object per line (JSONL), preserving input order. Keep the existing single positional `title` and bare-stdin single-title path exactly as-is (KTD-2). Blank lines skipped; each line parsed independently so one bad title doesn't abort the batch.
- *variant_text (BUI-295):* surface the existing Newsstand/Direct detection (comic_identity.py ≈ 584–606) as `variant_text` on the CLI output. Empty string when absent (KTD-3). Do not add a second detector.
- *Skill wiring:* Step 2 builds a titles file from the new wins, runs `comic-identify --batch` once → JSONL → file, and maps each line into `identify_data` (including `variant_text` directly, no hand-regex). Preserve the existing lot-expansion handling (`is_lot` / `constituent_issues`) and the low-confidence/null-series user-gate.
**Patterns to follow:** the current argparse + `identity_to_dict` structure in `comic_identify.py`; the JSONL-one-object-per-line output already implied by `test_output_is_single_line_json`.
**Execution note:** add the batch-mode and variant_text tests before wiring, then implement until green.
**Test scenarios** (`apps/ebay/tests/test_comic_identify.py`):
- Batch happy path: a file of 3 titles → 3 JSONL lines, order preserved, each a valid identity dict.
- Batch order/independence: a batch containing one rejectable title (e.g. a CGC-slab title) still emits a line for it with `reject_reasons` populated, and does not drop the other lines.
- Batch blank-line handling: blank/whitespace lines are skipped, not emitted as empty identities.
- Back-compat: existing single-title arg mode and stdin single-title mode still pass unchanged (the current tests must stay green — do not edit them to accommodate batch).
- variant_text present: `"... Newsstand ..."` title → `variant_text == "Newsstand"` (or the canonical value the detector yields); `"... Direct Edition ..."` → the Direct value.
- variant_text absent: a plain title → `variant_text == ""` (matches the skill's omit/empty contract).
- variant_text does not leak into single-title regressions: existing `test_arg_in_json_out` still asserts its known fields.
**Verification:** `cd apps/ebay && uv run --with pytest pytest tests/test_comic_identify.py` is green (plain `uv run pytest` silently no-ops — false pass; the `--with pytest` form is mandatory here). Manually run `comic-identify --batch` on a 3-title file and confirm JSONL out. Re-read Step 2 of the skill: a fresh agent runs identify once, not per-title, and reads `variant_text` off the output.

---

## Sequencing & Dependencies

1. **Branch A** off `main`: U1 → U2 → U3, one commit each (BUI-294, BUI-291, BUI-293). Same file, so in-order keeps diffs clean. Open PR A.
2. **Branch B** off `main` (independent of A — different files, except the Step 2 skill edit which does not overlap Steps 1/3/3b touched by A; if A is unmerged, branch B off A to avoid a transient conflict on `collection-add.md`, otherwise off `main`). U4. Open PR B.
3. Run `/ce-simplify-code` on each branch's diff before its PR, then `/ce-code-review` — heavier review weight on Branch B (it has runtime code + a test); Branch A is prose-only so review focuses on flow coherence and that no removed reference dangles.

**Note on the Step 2 edit spanning both concerns:** the Step 2 skill change belongs with U4 (Branch B) because it depends on batch mode + `variant_text` existing. It does not touch Step 1/3/3b, so it won't conflict with Branch A's edits to the same file — but if Branch A is still open when Branch B starts, branch B off A so `collection-add.md` has a single lineage.

---

## Discovered-Improvement Protocol

While executing, if a sub-agent (`/ce-simplify-code`, `/ce-code-review`, or an implementation agent) surfaces a genuine improvement or fix beyond these five tickets:

1. **Judge scope.** In-scope trivia (a typo in a line being edited, an obviously-better variable name in the batch code) is fixed inline in the current commit — no ticket.
2. **Out-of-scope but real** (a latent bug, an adjacent simplification, a missing test elsewhere) → **create a Linear ticket in the same project** ("Collection-Add: Simplification & Token Efficiency", BUI team) via the `linear` CLI, following the `linear-method` skill (Title Case, description with problem/change/why/risk). Link it to the ticket that surfaced it.
3. **Fold-in decision.** If the new ticket is small, safe, and on a branch already open, implement it there as its own commit and note it in the PR. If it is larger or riskier, leave it in the project backlog and mention it in the final report — do not expand a PR's scope silently.
4. **Report.** The final summary lists any tickets created and whether each was implemented now or deferred.

---

## Verification Strategy

- **Skill-body units (U1–U3, and the Step 2 part of U4):** no automated suite. Verify by re-reading the edited flow end-to-end as a fresh agent would, confirming (a) the intended behavior is now documented, (b) no dangling references to removed content, (c) the loud-fail / stop-on-error guarantees (unreachable server, POST failure, partial_failure) are preserved.
- **Code unit (U4):** `cd apps/ebay && uv run --with pytest pytest tests/test_comic_identify.py` green, plus a manual `--batch` smoke run. The existing single-title/stdin tests staying green **is** the back-compat gate.
- **Cross-cutting:** after both branches, a dry re-read of the whole `collection-add.md` to confirm Steps 0→5 still compose (Step 1 collapse didn't orphan Step 1b's seen-set language; Step 3 filter didn't drop the partial_failure stop; Step 2 batch wiring feeds Step 3 the same `{"wins": [...]}` shape).

## Risks & Mitigations

- **R1 — BUI-291 file-redirect swallows the 500 signal.** Mitigation: KTD/U2 explicitly require the partial_failure branch to read `rows_written` from the saved file and keep a hard stop on non-2xx; verify the `-sf`→`-o`/`-w` swap still exits non-zero on 500 (fall back to keeping `-f`).
- **R2 — Batch mode regresses `/comic:identify`.** Mitigation: additive flag only; the existing single-title + stdin tests are a locked back-compat gate and must not be edited to accommodate batch.
- **R3 — `variant_text` divergence.** Mitigation: reuse the existing Newsstand/Direct detector (KTD-3), don't write a second one; test both present and absent cases.
- **R4 — Step 1 collapse orphans downstream prose.** Mitigation: U3 verification re-reads Steps 1→1b→2 and greps the file for removed references ("Source A/B", field-mapping table, deleted Common-Mistakes rows).
