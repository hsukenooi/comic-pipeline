---
title: "A timed-out write is indeterminate — reconcile before any dependent action, including editing the queue that produced it"
date: 2026-08-08
category: conventions
module: "gixen-cli (cli.py _server_request_result/_server_timeout, add_batch.py reconcile_indeterminate_rows); any client of the comics server"
problem_type: convention
component: tooling
severity: high
related_components:
  - "gixen_sync"
applies_when:
  - "A write to the comics server (or to Gixen through it) times out client-side — add-batch, `gixen add`/`edit`/`remove`, or a hand-rolled call"
  - "Deciding what to do next after a reported-failed write during a Gixen slowdown: retry, edit, remove, or rewrite the queue file"
  - "Writing or reviewing code that maps a transport exception to an outcome status"
  - "Raising a client timeout and believing that closes the problem"
symptoms:
  - "A batch reports every row failed, but live PENDING count went up while it ran"
  - "Landed rows carry `link_attempted: false` and render `—` for title/grade/FMV on the dashboard"
  - "A snipe goes live at an amount nobody intended — the amount from a superseded version of the queue file"
  - "A retry of 'failed' rows produces no duplicates and no errors, because the originals had already landed"
mechanized_by: test
enforced_by_test:
  - packages/gixen-cli/tests/test_add_batch.py::test_add_one_row_timeout_is_indeterminate_not_failed
  - packages/gixen-cli/tests/test_add_batch.py::test_reconcile_found_live_upgrades_to_landed_and_attempts_fmv_link
  - packages/gixen-cli/tests/test_add_batch.py::test_reconcile_absent_stays_indeterminate_and_is_reported_not_landed
  - packages/gixen-cli/tests/test_add_batch.py::test_reconcile_call_failure_never_upgrades_a_row
  - packages/gixen-cli/tests/test_add_batch.py::test_reconcile_found_live_at_a_different_max_bid_stays_indeterminate
  - packages/gixen-cli/tests/test_cli_add_batch.py::test_add_batch_timeout_not_live_reports_indeterminate_not_failed
  - plugins/gixen-overlay/tests/test_skill_contracts.py::test_snipe_add_documents_indeterminate_status_and_remediation
tags:
  - "indeterminate"
  - "timeout"
  - "reconcile"
  - "add-batch"
  - "real-money"
  - "BUI-697"
  - "gixen-cli"
related_docs:
  - "docs/solutions/conventions/add-batch-row-status-contract.md"
---

# A timed-out write is indeterminate — reconcile before any dependent action

## Context

On 2026-08-07, during a Gixen slowdown, `gixen add-batch` reported **36 of 36 rows
`failed`** with `"Server timed out."`. Eleven of them had committed — live PENDING went
62 → 73 while the batch claimed nothing landed. The retry of the remaining 25 also
reported all-failed; **all 25 later appeared live**, at the right caps, with no
duplicates.

The cause is one line: `cli._server_request_result` mapped `requests.Timeout` to
`(False, None, "Server timed out.")`, and `add_batch.py` recorded that as `STATUS_FAILED`.
But a timeout says nothing about the write. It says the **client stopped waiting**. The
comics server keeps working past that deadline, finishes its Gixen round-trip, and
commits.

That misreport cost money. A timed-out (reported-`failed`) write was treated as dead, so
the rows file it came from was edited underneath it — and the stale write committed
overnight. **ASM #61 went live at $1.09 instead of the instructed $20.** A second cost was
quieter: every one of those rows carried `link_attempted: false`, so the post-add FMV link
never ran for any that landed. They sat permanently unlinked, rendering `—` for title,
grade and FMV range on the dashboard, with no retry path.

## Guidance

**Map a transport timeout to a third outcome, not to failure.** `failed` and
`indeterminate` are different facts and they license different actions. `failed` means the
write did not land, so retry. `indeterminate` means *unknown*, and unknown licenses
exactly one next step: **read**.

**Reconcile before any dependent action.** After a timeout, do not re-add, do not
`gixen edit`, do not `gixen remove`, and — the one that actually cost money — **do not
edit the queue file that produced the write**. Every one of those is a write predicated on
a state you have not observed. Re-read live state first (`gixen list`, or
`GET /api/comics/snipes`), then act on what you see:

| what you see | what it means | do |
|---|---|---|
| live at the amount you sent | it landed | nothing |
| live at a *different* amount | an earlier or later write won | decide which amount you want, then `gixen edit` |
| absent | probably didn't land — but see below | re-add |

**Reconciliation is the load-bearing part; a bigger timeout is only a mitigation.** BUI-697
raised `_DEFAULT_SERVER_TIMEOUT` from 15s to 60s and made it `COMICS_SERVER_TIMEOUT`-
overridable, and that helps — 15s sat below the CLI→server→Gixen chain's worst case, and
`GixenClient(timeout=15.0)` is a *separate, inner* timeout that could consume the whole
outer budget on its own. But **no bound is safe**, because a stalled transfer can exceed
any of them: the same outage had `home_2.php` returning healthy TTFB (2–8s) and then
stalling mid-body on a ~400KB page. If you find yourself closing a ticket by picking a
larger number, the ticket is not closed.

**Absence at reconcile time is evidence, not proof.** The reconcile pass upgrades a row
found live, but a row it does *not* find stays indeterminate rather than being demoted to
`failed` — demoting it would reintroduce the same false claim, 10 seconds later. The
25-row retry is the proof: it reported all-failed and all 25 appeared live afterwards.

**A failed reconcile call upgrades and demotes nothing.** If the read itself errors, the
row stays indeterminate with `reconcile.checked: false`. A server that cannot answer tells
you nothing; deriving a verdict from a call that never answered is the same mistake one
level up.

**A row confirmed landed by reconcile must get every side effect a normally-landed row
gets.** In `add-batch` that is the FMV link (`attempt_fmv_link`). Half-landing a row —
recording the bid but skipping its linkage — is how the incident produced 25 dashboard
rows with no title, grade, or FMV.

## Why This Matters

The misreport was the defect that made a whole outage hard to read: the operator's ground
truth said "nothing landed" while the server's said "38 things landed," and every
subsequent decision was made against the wrong one. Two separate real costs followed from
the same false claim — a wrong bid amount going live (money) and 25 unlinked rows (silent
data loss with no retry path).

The general shape outlives the specific bug. **Any client-side deadline on a write
produces three outcomes, not two**, and code that only models two will always resolve the
third one wrongly — usually toward "failed", because that is the branch the exception
lands in. The tell is a status vocabulary in which every non-success value means "it
didn't happen." Ask what the *client* actually observed: a 4xx/5xx, a refused connection,
or a validation failure is an **answer**; a timeout is the absence of one.

## When to Apply

- Writing or reviewing any `except requests.Timeout` (or equivalent deadline) branch on a
  write path. Decide explicitly whether the code below it is entitled to say the write did
  not happen.
- Handling a reported-failed write during a Gixen slowdown, by hand or in a skill. The
  live-state read comes first, every time — see `.claude/commands/comic/snipe-add.md`
  § "Handling an indeterminate row".
- Adding a new row outcome to `add_batch.py` — it must earn its halt semantics, exit-code
  membership and JSON shape per
  `docs/solutions/conventions/add-batch-row-status-contract.md` (`indeterminate` did:
  it takes FAILED's health-re-check rule, counts against the exit code, and gets its own
  summary bucket).
- Any future proposal to "fix" a timeout class by raising a timeout. Raise it if the old
  value was genuinely too low, but ship the reconcile too.

## Related

- `docs/solutions/conventions/add-batch-row-status-contract.md` — the six-status contract,
  stamped `status: corrected` by BUI-697 (its `failed` row used to claim every non-landed
  status meant "this book's bid did not commit").
- `packages/gixen-cli/add_batch.py` — `STATUS_INDETERMINATE`,
  `reconcile_indeterminate_rows()`, `attempt_fmv_link()`.
- `packages/gixen-cli/cli.py` — `_server_request_result`'s `requests.Timeout` branch,
  `_server_timeout()` / `COMICS_SERVER_TIMEOUT`, `_reconcile_settle_seconds()` /
  `ADD_BATCH_SETTLE_SECONDS`.
- `docs/retrospectives/2026-08-07-gixen-outage-session.md` — finding F1, the session this
  came from.
- Tickets: BUI-697 (this fix), BUI-699 (the timeout value itself), BUI-562 (the same
  outage's retry-timing analysis).
