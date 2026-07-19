---
title: "/comic:buy orchestrator mechanics: EXECUTOR/ORCHESTRATOR dispatch, sub-agent reuse, and the comic_id capture chain"
date: 2026-07-19
category: workflow-issues
module: ".claude/commands/comic/buy.md (orchestrator) + collection-check.md / verify.md / fmv.md"
problem_type: workflow_issue
component: development_workflow
severity: medium
related_components:
  - "service_object"
applies_when:
  - "Editing /comic:buy's Execution Pattern section, or a leaf skill's EXECUTOR CONTRACT / ORCHESTRATOR NOTES split"
  - "Deciding whether a follow-up question mid-run should respawn a sub-agent or SendMessage an existing one"
  - "Tracing why bids.comic_id / bids.fmv_id ended up NULL for a row added via /comic:buy (PER-140)"
  - "Wondering whether comic-fmv's --max-age-days DB-FMV reuse applies inside the orchestrated buy flow"
tags: [comic-buy, orchestrator, executor-contract, sendmessage, sub-agent-reuse, comic_id, PER-140, BUI-361, BUI-366, BUI-153, PER-99]
---

# /comic:buy orchestrator mechanics

## Context

`/comic:buy` is a long-running orchestrator that dispatches four leaf skills
in sequence, each gated on user approval. Several of its design decisions are
not self-evident from the imperative steps alone, and used to be explained at
length inline (BUI-434 trimmed the body from 377 lines toward sub-300 by
moving that rationale here). This doc is the "why"; `buy.md` keeps only the
compact "how."

## Guidance

### EXECUTOR CONTRACT / ORCHESTRATOR NOTES split (BUI-361)

Leaf skills that run in sub-agents (`collection-check.md`, `verify.md`) are
split into two marked sections: an **EXECUTOR CONTRACT** (everything the
executing sub-agent needs, self-contained) and **ORCHESTRATOR NOTES** (gates,
decision guidance, carry-forward data shapes). The orchestrator dispatches a
step by pointing the sub-agent at the skill file and its EXECUTOR CONTRACT,
and reads *only* the ORCHESTRATOR NOTES section itself.

Why split at all: a sub-agent reading its own EXECUTOR CONTRACT from the
skill file, rather than having the orchestrator re-serialize that content
into the dispatch prompt, avoids paying for the same content twice (once in
the skill file, once copy-pasted into the prompt) and avoids the copy
drifting from the file as the skill evolves. The orchestrator reading only
ORCHESTRATOR NOTES (never the EXECUTOR CONTRACT) keeps its own context
proportional to "what a coordinator needs to know," not "everything an
executor needs to know."

`verify.md` uses a third variant: it has the same EXECUTOR CONTRACT /
ORCHESTRATOR NOTES split, but `/comic:buy` Step 6 never dispatches an
executor for it — `gixen add-batch --verify` (Step 5) already performed the
equivalent call inline, so Step 6 reads only `verify.md`'s ORCHESTRATOR NOTES
to interpret verdicts already embedded in that CLI's JSON output. Treat it
like reading the ORCHESTRATOR NOTES of a dispatched skill with no matching
executor dispatch to pair it with.

### Sub-agent reuse — SendMessage, not respawn (BUI-366)

Sub-agents spawned during a run stay addressable for its duration (if named
at spawn time — an unnamed agent silently degrades reuse back to a respawn).
When a follow-up question or an incremental unit of work lands on data an
existing agent **already holds**, route it via `SendMessage({to: <name>,
message: ...})` instead of spawning fresh: a respawn re-fetches and
re-instructs for data already sitting in the first agent's context, paying
the token cost twice.

Concretely, in `/comic:buy`:

- **The identifier agent (Step 1)** holds the full `ebay_fetch.py` JSON for
  every listing — item specifics, description text, printing/variant evidence
  that never entered the orchestrator's own context. Route follow-ups like
  "is item N a first print or a later printing?" to it via SendMessage — see
  `identify.md` § Follow-ups for the worked 2026-07-16 example. Current Price
  and Bids are *not* a reason to message it (BUI-359 already emits them in
  the Step 1 table).
- **The collection-check executor (Step 2)** holds its loaded EXECUTOR
  CONTRACT and the run's verdicts. A comic added to the working list mid-run
  is a SendMessage of the new `{series, issue, year?, variant?}` row, not a
  fresh respawn that re-reads the contract from scratch.

There is no snipe-add sub-agent to reuse — BUI-360 made Step 5 an inline
`gixen add-batch` call; a late "add one more snipe" is just another inline
`gixen add`/`add-batch` invocation, or an ad-hoc executor per `snipe-add.md`.

Reuse is about which agent already holds the relevant data, not about
avoiding spawns on principle — spawn fresh when no live agent has it. And
reuse never skips a gate: work routed via SendMessage still goes through the
same user approvals as first-pass work.

### The comic_id capture chain (PER-140)

`/comic:buy` Step 3 (`comic-fmv --brief`) returns one compact JSON line per
row: `{item_id, comic_id, fmv_id, max_bid, flag_reason, confidence}`. Carrying
`comic_id` (and `fmv_id`) forward from that line through Step 5's
`gixen add-batch` rows is what populates `bids.comic_id` / `bids.fmv_id` via
the `bid_fmvs` junction — the recurring **PER-140** bug is exactly those
columns coming back NULL because some intermediate step dropped the id. If
`comic_id` is dropped at any point, the snipe still records (the bid itself
never depends on FMV linkage), but the dashboard permanently loses condition
and FMV data for that bid, because there is no later step that re-derives it.
Step 5 (assembling the add-batch rows) is where past sessions most often
broke the chain — see `snipe-add.md`'s "Canonical post-FMV invocation"
section for the `--comic-id` vs `--catalog-id` distinction that also feeds
this bug class. (BUI-435 replaced the hand-assembled Step 5 JSON with a
tested builder specifically to make dropping `comic_id` a structural
impossibility rather than a prose warning — see
`packages/gixen-cli/add_batch.py`.)

**BUI-153 aside — DB-FMV reuse is inert inside `/comic:buy`.** `comic-fmv`'s
`--max-age-days N` flag reuses an FMV already in the comics server's DB when
`fmv_updated_at` is recent, but that reuse only fires for books carrying a
`locg_id`. The orchestrated buy flow derives series/issue from the eBay
listing title and never resolves a `locg_id`, so this cache-skip path never
engages in `/comic:buy` — every run recomputes FMV from comps (the
`ebay-sold-comps` SerpApi response cache still applies underneath).
`--max-age-days` does engage on the standalone `comic-fmv` CLI path when the
batch explicitly carries `locg_id`s.

### Origin of the Step 6 warn-only design (PER-99)

Step 6's "warn, don't block" verification design exists because
`/comic:buy` previously ran to apparent success in real incidents (PER-99)
while silently leaving rows partially populated — a missing comic row, an FMV
stub, or a null `bids.fmv_id` — that only surfaced later when
`/comic:collection-add` choked on the gap. Surfacing gaps immediately, right
after the batch add, is cheaper than discovering them when the auction ends.

## Related

- `.claude/commands/comic/buy.md` — Execution Pattern, Step 3, Step 5, Step 6
- `.claude/commands/comic/collection-check.md`, `verify.md` — the EXECUTOR
  CONTRACT / ORCHESTRATOR NOTES split this doc explains
- `.claude/commands/comic/snipe-add.md` — the `--comic-id`/`--catalog-id`
  distinction and Canonical post-FMV invocation
- `packages/gixen-cli/add_batch.py` — the BUI-435 builder that structurally
  prevents the PER-140 comic_id drop
- Tickets: BUI-361, BUI-366, BUI-360, BUI-153, PER-140, PER-99, BUI-434 (this
  slimming pass)
