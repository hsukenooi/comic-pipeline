---
title: "collection-add's record-win tail: why the invariants exist (BUI-352/353/354/137/428)"
date: 2026-07-19
category: docs/solutions/best-practices
module: comic-collection-add
problem_type: best_practice
component: development_workflow
severity: medium
applies_when:
  - Editing `.claude/commands/comic/collection-add.md`
  - Wondering why a terse inline warning in that skill exists before trimming or "simplifying" it away
  - Debugging a wrong committed/skipped count from `/comic:collection-add`
related_components:
  - gixen-cli
  - gixen-overlay
  - comic-collection-add
tags:
  - collection-add
  - record-win
  - bui-352
  - bui-353
  - bui-354
  - bui-137
  - bui-428
  - workflow
---

# collection-add's record-win tail: why the invariants exist

`.claude/commands/comic/collection-add.md` carries a handful of terse inline
warnings that look like they could be trimmed or "optimized away" by a future
editor — re-source the server helper in every block, `rm -f` a response file
before POSTing, never report success on a non-2xx. Each one is scar tissue
from a real incident. This doc is the detailed history; the skill itself
keeps only the short version (BUI-429, 2026-07-19 slimming pass).

## Re-resolving the server (and, since BUI-430, the scratch dir) in every block

**Full incident writeup:**
`docs/solutions/workflow-issues/multi-block-skill-shell-state-loss-fallback-swallow.md`.

Short version: every `## Step` in a Claude skill is its own fenced bash
block, which the harness runs as an independent shell. Nothing exported in
one block — `COMICS_SERVER_URL`, a scratch-dir path, a sourced function — is
visible in the next. BUI-352 found this the hard way: an empty
`$COMICS_SERVER_URL` in a later block made a `curl` call fail with exit 3
("no host part in the URL"), and a defensive-looking `|| echo ""` swallowed
that as an empty BUI-121 seen-set, misclassifying every already-recorded win
as new (130 wins fed through the pipeline instead of the real 8). BUI-353
moved the affected logic into a single tested helper (`gixen
record-win-prep`) so the failure policy lives in one place instead of being
re-derived (and re-risked) in hand-authored shell every edit. The remaining
per-block re-source calls (`comics_resolve_server`, and now
`comics_scratch_dir`) are cheap, idempotent, and must not be skipped just
because an earlier step already ran them.

## The stale-response-body trap and the `rm -f` guard

`curl -o file` only truncates `file` once response bytes start arriving. On
a connection failure (server down, DNS failure, timeout — the request never
leaves the machine), curl exits non-zero and the target file is untouched.
If a *prior* run's response file is still sitting at that path, code that
blindly parses "the response file" after a failed POST will happily read
last run's *successful* body and report its `rows_written` as if this run's
request had partially succeeded — a fabricated "committed before failure"
count for a request that never reached the server.

This is why `collection-add.md`'s Step 3 does `rm -f
$SCRATCH/commit_response.json` **before** issuing the POST, not after: the
guard only works if the stale file is gone before there's any chance of
reading it. BUI-430 (2026-07-19) additionally routes all of the skill's temp
files into a per-run scratch directory (`comics_scratch_dir`, defined in
`scripts/comics-server.sh`) instead of fixed `/tmp/prep.json`-style names, so
a file left behind by a crashed or abandoned run in a *different* session
can't even be found by a later, unrelated run — the `rm -f` guard is now
belt-and-suspenders against same-session reuse, not the only thing standing
between a crashed run and a fabricated count.

## `partial_failure` is a 500, not a 200 (BUI-137)

`record-win/commit` writes in batches of 25. Before BUI-137, a chunk failing
partway through a batch could surface as a plain HTTP 200 carrying only the
rows actually written — indistinguishable, from the skill's perspective, from
a run where every row succeeded. BUI-137 changed a mid-batch failure to
return **HTTP 500** with `{"detail": {"error": "partial_failure",
"rows_written": <only-committed>}}` instead. BUI-428 layered its atomic
merge/record/mark-seen/status-refresh behavior on top of the same
status-code contract: a partial failure still returns non-2xx, and marks
nothing seen (record and mark-seen are atomic together), so there's no
separate best-effort mark-seen step left to run after a failure. The skill's
status-code check treats *any* non-2xx as the failure branch for exactly this
reason — a 200 must never be assumed to mean "fully written."

## The confidence-gate removal (BUI-354)

An earlier draft of Step 2 had the LLM ask the user for clarification
whenever `comic-identify`'s reported confidence was "low." In practice this
clause fired on nearly every real title: `comic-identify` returns a baseline
confidence of `0.5` for any cleanly-parsed title that has no publication year
to cross-check against Metron — which describes most eBay listing titles,
not just ambiguous ones. A per-title confidence gate at that baseline would
have interrupted the batch for almost every win, defeating the point of
batching at all. BUI-354 dropped the clause rather than tuning the
threshold: the only gate that survived is `needs_review` itself — a null
`series`/`issue`, a `comic-identify` error, or a lot with
empty/unparseable `constituent_issues`.

(BUI-422 later added one more targeted gate — a null `year` on a win priced
at or above $25 — because vintage, no-year titles are disproportionately
prone to a downstream volume mis-resolution. That gate is unrelated to the
confidence clause BUI-354 removed; it's a narrow, price-scoped addition, not
a return of "ask if confidence is low." See the skill body's own gate list
for the authoritative current set — this doc is history, not the source of
truth for what gates today.)

## The record-win/commit consolidation (BUI-428)

Before BUI-428, what's now one endpoint call was three separate steps glued
together by hand-authored client-side code:

1. An inline `a + b` merge of Step 1's `wins` array and Step 2's
   `resolved_reviews` array. An earlier draft of this merge silently dropped
   the resolved rows in one case.
2. A record-win POST.
3. A mark-seen POST that **re-derived its own item_id set** from a
   client-side file (`/tmp/wins.json`) independently of what the record-win
   call had actually committed — so a bug in the merge step could key
   record-win and mark-seen off two *different* sets of item_ids, marking
   something "seen" that was never actually recorded (or vice versa).
4. A separate `GET /api/comics/collection/status` call to refresh
   `pending_push_count`/`oldest_pending_days` for the final report.

BUI-428 moved all four operations server-side into one atomic call,
`POST /api/comics/collection/record-win/commit`: it merges `wins` +
`resolved_reviews` itself, records via the unchanged Metron/BUI-34 path,
marks seen **exactly** the item_ids it just merged and submitted (never a
client re-derivation), and — only on full success — folds in a fresh
`pending_push_count`/`oldest_pending_days` read. On a partial or failed
commit it marks nothing seen at all. This removed `/tmp/wins.json` and
`/tmp/record_win_response.json` from the skill entirely and collapsed what
used to be Steps 3/3b/5 into one Step 3.

## Related

- `docs/solutions/workflow-issues/multi-block-skill-shell-state-loss-fallback-swallow.md`
  — the full BUI-352/353 shell-state-loss and fallback-swallow incident.
- `docs/solutions/best-practices/collection-add-workflow-patterns-2026-06-18.md`
  — pending-push semantics (`oldest_pending_days`) and manual-resolution
  clearing, the other load-bearing-but-non-obvious behavior in this skill.
- Tickets: BUI-137/156 (partial-failure + post-write status re-fetch, PR #74),
  BUI-352/353/354 (seen-set hardening + record-win-prep + confidence-gate
  removal, PR #187), BUI-422 (missing-year gate, PR #244), BUI-428 (atomic
  record-win/commit endpoint, PR #261), BUI-429/430 (this slimming +
  scratch-dir pass).
