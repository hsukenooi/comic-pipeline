---
title: "seller-scan design rationale: in-script verification, no per-seller subagents, best-effort seen-tracking"
date: 2026-07-19
category: workflow-issues
module: "apps/ebay/src/seller_scan.py + .claude/commands/comic/seller-scan.md"
problem_type: workflow_issue
component: development_workflow
severity: low
related_components:
  - "service_object"
applies_when:
  - "Deciding whether to run a second verifier/subagent over seller-scan's output"
  - "Deciding whether to fan out a Task/Agent subagent per seller instead of one batched seller_scan.py call"
  - "Wondering why seen-tracking warns-and-continues on server failure while wish-list fetch aborts"
  - "Interpreting sellers[*].filtered, sellers[*].dropped_candidates, or sellers[*].skipped_cached_candidates in --json output"
tags: [seller-scan, verification, subagent, seen-tracking, rejected-candidate-cache, BUI-149, BUI-298, BUI-113, BUI-317]
---

# seller-scan design rationale

## Context

`/comic:seller-scan`'s body used to carry three long "why" essays inline,
reloaded on every invocation even though the reasoning doesn't change
run-to-run: why the skill must never run a second verifier over the script's
output (BUI-149), why a multi-seller scan is one batched script call rather
than one subagent per seller (BUI-298), and why seen-tracking is best-effort
while the wish-list fetch is not (BUI-113/317). BUI-441 trimmed the skill body
down to the load-bearing one-liners and moved the rationale here — same
pattern as BUI-434 did for `/comic:buy` (see
`docs/solutions/workflow-issues/buy-orchestrator-dispatch-pattern-and-comic-id-chain.md`).

## Guidance

### Verification is already done inside the script — never run a second one (BUI-149)

The fuzzy matcher (issue-number-in-title + ≥50% series-token overlap) is
deliberately loose so it doesn't miss a wish-list book — but that looseness
means a short or generic series name can produce a **false positive** (e.g.
wish-list "Daredevil #1" matching a "Daredevil Annual #1" or an unrelated
reprint). Those false positives are the leak at the **seller-scan →
`/comic:buy`** seam: once a wrong URL flows into `/comic:buy`, identify + FMV
will happily price the wrong book.

`seller_scan.py` already guards this seam itself. Before emitting anything,
it runs an internal Claude (haiku) pass over **every** candidate and keeps
only the genuine matches, so the rows in the table/JSON are already
post-verified (`verify_with_claude`, `seller_scan.py:726`). Spawning a
`general-purpose` subagent from the skill to re-check the match table would
just re-verify an already-verified set — it adds latency and cost with no
correctness gain, since the subagent has no signal the in-script verifier
lacked.

No silent drops: rejected candidates are printed to stderr as a `Filtered N
likely false positive(s)` block with the model's one-line reason for each,
and (BUI-298) the same data is returned inline per-seller in `--json` as
`sellers[*].filtered` (`{item_id, title, wish_name, reason}`) — so a caller
piping `--json` into another tool doesn't have to scrape stderr to see why
something was filtered.

A candidate that was **never verified** at all (a `claude` CLI
timeout/transport failure that survived retries) is not the same as a model
rejection — it lands in `sellers[*].dropped_candidates` and flips that
seller's `incomplete` to `true` (exit code 3). Conflating "rejected by the
model" with "never reached the model" would hide a real gap in coverage
behind what looks like a clean, fully-verified run.

Every surfaced match clears `match_score ≥ 0.65` (the script's emit floor,
`seller_scan.py:237`); the 0.65–0.69 band is the genuinely-borderline range a
user may still want to eyeball on the listing page, even though Claude
already passed it.

### Why not one subagent per seller (BUI-298)

Each seller's scan is just one deterministic `seller_scan.py` invocation —
there's no reasoning step a subagent adds. Fanning out one `Agent` subagent
per seller previously meant N separate LLM reasoning loops, N idle
notifications, and manual aggregation of N free-text reports. Worse, it's
unreliable: a subagent asked to retry a scan (after a verification timeout)
once returned a stale/hallucinated duplicate of its first report instead of
actually re-executing. A deterministic Bash re-run of the batched script
cannot fabricate a result — it either ran and produced real output, or it
didn't run. Passing every seller as a positional arg to one invocation also
fetches the wish list + OAuth token once instead of N times.

### Seen-tracking is best-effort; the wish-list fetch is not (BUI-113/317)

Seen-tracking (BUI-113) trades a small false-negative risk for safety: if the
comics server is unreachable, the scan warns and shows **all** matches rather
than aborting, because a duplicate row the user has seen before is harmless,
but silently hiding a real match because the seen-check failed is not. This
is the opposite trade-off from `fetch_wish_list()`, which hard-fails on an
unreachable server — running the whole scan against a stale-or-empty
wish-list would produce confidently-wrong "no matches" output, which is worse
than refusing to run at all. Same server-unreachable trigger, deliberately
opposite response, because the cost of being wrong differs: a duplicate match
vs. a phantom "nothing found."

`--all` bypasses the same-day noise in two independent ways that are easy to
conflate: it re-shows matches already recorded as seen (BUI-113), *and*
(BUI-317) it force-re-verifies every candidate by bypassing the
rejected-candidate cache — a normal run skips re-verifying a `(listing,
wish)` pair Claude already rejected within the last 14 days
(`_rejected_cache_key`, `seller_scan.py:356`). `--all` still records
newly-surfaced matches as seen; it means "show me everything," not "forget
what's been shown."

`sellers[*].skipped_cached_candidates` (BUI-317) counts candidates skipped
entirely — no Claude CLI call — because that exact `(listing, wish)` pair was
already rejected within the last 14 days. It's lower stakes than
`dropped_candidates` (those were never verified at all): this is coverage
info for unattended monitoring, and a nonzero count is expected/healthy on a
repeat scan of the same seller. It's always `0` when `--all` is passed, since
`--all` bypasses the cache and force-re-verifies everything.

## When to Apply

- Reviewing or editing `/comic:seller-scan` and tempted to re-add a
  verification, batching, or seen-tracking explanation inline — link here
  instead.
- Deciding whether a new failure mode belongs in the skill's exit-code table
  (mechanics: what code, what JSON shape) or here (why the mechanism exists
  the way it does).

## Related

- `.claude/commands/comic/seller-scan.md` — the slimmed skill; exit-code
  table is the canonical list of failure modes.
- `apps/ebay/src/seller_scan.py` — `verify_with_claude` (BUI-149),
  `fetch_wish_list`/seen-tracking (BUI-113), `_rejected_cache_key` (BUI-317).
- Tickets: BUI-149, BUI-298, BUI-113, BUI-317, BUI-441 (this slimming pass).
- `docs/solutions/workflow-issues/buy-orchestrator-dispatch-pattern-and-comic-id-chain.md`
  — sibling slimming pass (BUI-434) for `/comic:buy`, same pattern.
