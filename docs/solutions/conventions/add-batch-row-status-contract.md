---
title: "gixen add-batch's row-status contract: five statuses, two that halt, one that blocks without halting"
date: 2026-08-03
category: conventions
module: "gixen-cli (add_batch.py — RowResult, BatchOutcome, run_batch, exit_code; cli.py's add-batch command)"
problem_type: convention
component: tooling
severity: medium
related_components:
  - "policy_checks"
applies_when:
  - "Adding, reviewing, or consuming `gixen add-batch` output — a skill (snipe-add.md, buy.md), a script, or a human reading the JSON summary"
  - "Deciding what a non-zero add-batch exit code means, or whether a batch 'looks clean'"
  - "Extending add_batch.py with a new row outcome — a new status must earn a place in this contract (halt semantics, exit-code membership, JSON shape), not bypass it"
  - "Distinguishing a policy BLOCK (BUI-623) from a genuine add FAILURE (BUI-168) when writing or reviewing code that branches on row status"
symptoms:
  - "A caller checks `$?` (or `summary.failed == 0`) alone and treats a batch with blocked or not_attempted rows as full success"
  - "A reader assumes a BLOCKED row halts the batch the same way a server-down FAILED row does"
  - "A consumer treats `row.error is not None` as 'this row did not land' and misclassifies a merely-unlinked-but-added row"
mechanized_by: test
enforced_by_test:
  - packages/gixen-cli/tests/test_add_batch.py::test_run_batch_ae9_blocked_row_continues_no_halt
  - packages/gixen-cli/tests/test_add_batch.py::test_batch_outcome_exit_code_nonzero_on_blocked_alone
  - packages/gixen-cli/tests/test_add_batch.py::test_run_batch_halts_and_reports_not_attempted_when_server_down
  - packages/gixen-cli/tests/test_add_batch.py::test_add_one_row_link_failure_does_not_demote_status
  - packages/gixen-cli/tests/test_cli_add_batch.py::test_add_batch_ae9_blocked_row_continues_batch_with_two_added
  - plugins/gixen-overlay/tests/test_skill_contracts.py::test_snipe_add_documents_failed_add_policy
  - plugins/gixen-overlay/tests/test_skill_contracts.py::test_snipe_add_documents_blocked_status_and_remediation
tags: [add-batch, row-status, exit-code, BUI-360, BUI-168, BUI-623, policy-block, gixen-cli]
---

# gixen add-batch's row-status contract: five statuses, two that halt, one that blocks without halting

## Context

`packages/gixen-cli/add_batch.py` (BUI-360) backs `gixen add-batch`, encoding the BUI-168
mid-batch-failure prose spec (`.claude/commands/comic/snipe-add.md` § "Handling a failed
add") as deterministic code instead of an LLM-followed loop: a failed row is marked and the
batch keeps going, but a *dead server* halts it and marks every remaining row unattempted.
BUI-623 then added a wrinkle — `POLICY_BLOCK_<CODE>` (see
`packages/gixen-cli/CLAUDE.md` § "Policy checks") can turn an `advise`-level policy check
into a hard 409 at write time. That 409 needed its own status, because it is not the same
kind of "row did not land" as a server fault: **`BLOCKED` is a deliberate, working-as-
designed rejection, not evidence the server or Gixen is broken.** Getting the two conflated
would either (a) trip BUI-168's halt-on-failure logic on a healthy server just because one
book was over budget, or (b) let a batch report itself as a clean success while a real-money
bid silently never landed. This doc is the row-status contract that keeps them apart, for
whoever next touches `add_batch.py`, writes a script against `--json-out`, or edits a skill
that renders the human table.

## The five statuses

| status | constant | means |
|---|---|---|
| `added` | `STATUS_ADDED` | `POST /api/bids` created a new PENDING row (`created: true`) |
| `updated` | `STATUS_UPDATED` | `POST /api/bids` upserted an existing PENDING row (`created: false`, BUI-67) |
| `failed` | `STATUS_FAILED` | the row's own add did not land — a client-side validation failure (missing/invalid `item_id`/`max_bid`, an already-ended auction per BUI-567) **or** a server/Gixen fault (non-2xx that is not a policy block) |
| `not_attempted` | `STATUS_NOT_ATTEMPTED` | the batch halted before reaching this row — no network call was made for it |
| `blocked` | `STATUS_BLOCKED` | the server 409'd with a policy block (BUI-623) — `resp.get("blocked")` was truthy |

`added`/`updated` are the only two statuses `verify_items()` (the optional `--verify` pass)
and `_TERMINAL_OK_STATUSES` treat as "landed." Everything else is some flavor of "this
book's bid did not commit."

**`error` vs `link_error` — deliberately separate fields, not one overloaded string.**
`error` is set when the row's own add did not land: `status == FAILED` (a server/Gixen
fault or client-side validation) or `status == BLOCKED` (the block's human-readable
message). `link_error` is set only when the add *succeeded* (`ADDED`/`UPDATED`) but the
follow-up `POST /api/bids/{item_id}/link-fmv` call failed — the snipe landed, it's just
unlinked. A consumer scanning for `error is not None` to mean "this row did not land" must
not also catch a merely-unlinked-but-added row; the two fields exist precisely so that
scan is correct by construction rather than by convention.

## Halt vs continue: only FAILED-plus-server-down halts

`run_batch()` runs every row strictly sequentially (Gixen sessions are stateful — parallel
adds fail) and applies exactly one halt rule:

> On any row `FAILED`, re-check server health before the next row; if the server is down,
> halt and mark every remaining row `NOT_ATTEMPTED` without another network call.

**`BLOCKED` does not trigger this check at all.** The health re-check only fires on
`status == STATUS_FAILED`:

```python
if result.status == STATUS_FAILED and not health_check():
    halted = True
```

A `BLOCKED` row never calls `health_check()` — cheaper (no extra round trip) and, more
importantly, correct: a policy block proves the server is up and evaluating checks
correctly. Treating it as a possible-outage signal would be exactly backwards. The batch
proceeds straight to the next row, and `BatchOutcome.halted` stays `False` for the entire
run no matter how many rows come back `BLOCKED`. That asymmetry — `BLOCKED` continues,
trips a non-zero exit, and never sets `halted` — is the point of this doc's title, and it
is the one fact most likely to be assumed wrong by a reader coming from the pre-BUI-623
FAILED-only mental model.

## Exit-code rationale

`BatchOutcome.exit_code()`'s own docstring is the source of truth here (`add_batch.py:146-166`)
— quoted rather than re-derived, per its own instruction that a caller checking `$?` must
see every non-landed row, deliberate block or genuine fault alike:

> Non-zero if ANY row failed to land — a not-attempted row (batch halted before reaching
> it) is just as much a non-success as a failed one, so it counts too.
>
> BUI-623 (U9): a BLOCKED row counts too, by the SAME rationale — its write did not land
> either, even though the block was a deliberate policy decision rather than a technical
> fault and the batch continues past it (STATUS_BLOCKED never sets `halted`). Exit code
> communicates row-level completeness to whatever script/skill invoked `add-batch`; "2
> added + 1 blocked" must not look like full success to a caller checking `$?` any more
> than "2 added + 1 not_attempted" does — both mean the caller has follow-up to do (retry,
> `--ack-policy`, or a manual decision) before treating the batch as done.

Concretely: `exit_code()` returns `0` only when `failed == 0 and not_attempted == 0 and
blocked == 0`. `added`/`updated` never count against it, and neither do advisories (a
passed batch can still exit `0` while carrying advisories a human should review — see
below).

## The JSON summary shape skills consume

`--json-out results.json` (and the same shape on stdout as the final JSON line) is
`BatchOutcome.to_dict()`:

```json
{
  "summary": {
    "total": 3, "added": 2, "updated": 0,
    "failed": 0, "not_attempted": 0, "blocked": 1,
    "advisories": 1
  },
  "halted": false,
  "verify_error": null,
  "rows": [
    {"item_id": "1", "status": "added", "max_bid": 10.0, "advisories": [], "...": "..."},
    {"item_id": "2", "status": "blocked", "max_bid": 999.0,
     "error": "Blocked by policy check(s): over_fmv.",
     "advisories": [{"code": "over_fmv", "severity": "warning", "...": "..."}]},
    {"item_id": "3", "status": "added", "max_bid": 10.0, "advisories": [], "...": "..."}
  ]
}
```

Two shape details matter more than they look:

- **`summary` always has all five status keys, even at zero** (`BatchOutcome.summary()`
  seeds the `counts` dict with every status before counting rows). A skill or script
  reading just the summary — not iterating every row — always sees the complete
  added/updated/failed/not_attempted/blocked picture, never a `KeyError` on a status that
  happened not to occur.
- **`summary.advisories` is a total count across every row**
  (`sum(len(r.advisories) for r in self.rows)`), not a boolean. A batch with `failed: 0,
  not_attempted: 0, blocked: 0` can still carry a nonzero `advisories` count — the summary
  dict itself is what proves a batch never "looks clean" just because every row's status
  landed; a caller must check this field too, not only the status counts (BUI-621/U7).

`RowResult.to_dict()` (one entry per `rows[]` item) carries `item_id`, `status`, `max_bid`,
`grade`, `created`, `link_attempted`, `link_ok`, `error`, `link_error`, `verify`, `title`,
and `advisories` — the last is the row's own KTD4 advisory list (from the `POST /api/bids`
response for a landed row, or from the 409 detail body's `advisories` key for a `BLOCKED`
row — both shapes are the same structured `{code, severity, message, data}` list, so a
skill's per-row advisory rendering doesn't need to special-case which one it's looking at).
It is empty for a row that never reached the network (a client-side validation failure) or
whose add call itself failed outright (`FAILED` with no response to read advisories from).

## The duplicate-`item_id` hard stop is batch-level, not row-level

`parse_rows()` validates the whole input file's shape before `run_batch()` ever executes,
and raises `AddBatchError` — a hard stop on the entire batch, never a per-row result — for
two classes of problem: a structurally unusable file (not a list, or not an object with a
top-level `"rows"` list), and **duplicate `item_id`s across rows**. The duplicate check
exists because the server upserts on `item_id` (BUI-67): two rows for the same item_id
would silently collapse into one bid at the server while *both* rows still get reported as
independently landed in the human table and JSON summary — a misleading real-money report
that a per-row `FAILED` couldn't retroactively fix once the batch had already run. A row
that's simply *missing* `item_id` is not covered by this check; that surfaces later as an
ordinary per-row `FAILED` result at `add_one_row` time instead, alongside any other
per-row validation failure (invalid `max_bid`, an already-ended auction).

## Why This Matters

The BUI-168 halt/continue rule and the BUI-623 block/bypass rule were designed five months
apart by different tickets solving different problems (a dead server vs. an over-budget
bid), and nothing in the code forces a reader to notice they're deliberately asymmetric.
Without this doc, the natural failure mode is generalizing FAILED's halt behavior onto
BLOCKED ("any bad row should stop the batch") or generalizing BLOCKED's continue behavior
onto FAILED ("a batch always finishes every row") — either one breaks a real invariant this
module's tests already lock down (`test_run_batch_ae9_blocked_row_continues_no_halt`,
`test_run_batch_halts_and_reports_not_attempted_when_server_down`). The exit-code rule is
the second trap: a script or skill that greps `summary.failed == 0` instead of calling
`exit_code()` (or checking `failed`/`not_attempted`/`blocked` together) will report a
partially-blocked or partially-halted batch as fully successful — silently skipping the
"retry, `--ack-policy`, or manual decision" follow-up the exit code exists to force.

## When to Apply

- Writing or reviewing any code path that branches on an add-batch row's status (a skill,
  a script consuming `--json-out`, or a change inside `add_batch.py` itself).
- Adding a new row outcome to `add_batch.py` — decide explicitly whether it halts (FAILED's
  rule), continues (BLOCKED's rule), or needs a third rule of its own; don't let it default
  to whichever `run_batch()` happens to do today.
- Editing `snipe-add.md` or `buy.md`'s status-table / advisory-handling sections — keep the
  prose in sync with this contract rather than re-deriving it from memory. **Closed by
  BUI-644:** `snipe-add.md`'s Output status-values list and its "Handling Policy Advisories"
  section (renamed remediation guidance, not the heading text — `buy.md` cites it by name)
  now document `blocked`/🚫 and its distinct not-landed remediation (lower the bid and
  re-add, or clear the blocking condition — never `gixen edit`/`gixen remove`, which assume
  a live snipe a blocked row doesn't have); `buy.md`'s mirrored advisory paragraph and its
  non-zero-exit-code explanation were updated the same way.

## Related

- `packages/gixen-cli/add_batch.py` — `STATUS_*` constants, `RowResult`, `BatchOutcome`,
  `run_batch()`, `exit_code()` (:146-166), `parse_rows()` (:177+).
- `packages/gixen-cli/CLAUDE.md` § "Policy checks (env vars)" — the `POLICY_BLOCK_<CODE>`
  mechanism that produces the 409 a `BLOCKED` row reports.
- `.claude/commands/comic/snipe-add.md` § "Handling a failed add (BUI-168)" and §
  "Handling Policy Advisories (BUI-621)".
- Tickets: BUI-360 (add-batch itself), BUI-168 (the halt/continue prose spec this
  mechanizes), BUI-621/U7 (the advisories envelope), BUI-623/U9 (blocking mode + BLOCKED).
