---
title: "Every solutions doc marks whether its claim still holds (status: / superseded_by:)"
date: 2026-08-03
category: conventions
module: "docs/solutions (frontmatter contract), scripts/solutions-lint (the pack + runner)"
problem_type: convention
component: documentation
severity: medium
mechanized_by: lint
lint:
  - solutions-status
applies_when:
  - "A measured result, a shipped fix, or a Linear ticket comment shows a docs/solutions doc's claim is wrong, partly wrong, or has been replaced"
  - "Writing a new docs/solutions doc that documents a correction to an earlier one"
  - "Reading a docs/solutions doc before relying on it — check status: before applies_when"
  - "A doc already carries a free-text status:-shaped key whose meaning predates this contract (see the reconciliation note below before reusing it)"
related_components:
  - "ci"
tags:
  - "status-supersession"
  - "solutions-frontmatter"
  - "learning-to-lint"
  - "docs-solutions"
  - "falsified-signal"
  - "ci-gate"
related_docs:
  - "docs/solutions/conventions/mechanized-by-frontmatter-contract.md"
  - "docs/solutions/conventions/verify-ticket-premise-before-implementing.md"
---

# Every solutions doc marks whether its claim still holds (`status:` / `superseded_by:`)

## Context

`mechanized_by:` (BUI-605) answers *how* a learning is enforced. It says nothing about
whether the learning is still **true**. `docs/solutions/architecture-patterns/
vacuous-partition-guards-and-mutable-identity-keys.md` named a `publisher_name` relabel as
a "live generator" of duplicate identities (BUI-559) — the same day a ticket comment
measured that the proposed gate matches **zero** live rows and closed it Won't Do. The doc
was correct when written and wrong an hour later, and nothing in its frontmatter said so.
A future reader (or an agent retrieving it as a "past learning") has no signal that the
claim in front of them has been overtaken, short of reading every related ticket by hand.

Separately, `docs/solutions/conventions/verify-ticket-premise-before-implementing.md`
independently accumulated a catalog of exactly this failure mode at Linear-ticket
granularity — a falsified ticket's "the signal that *does* work is X" becoming the next
ticket's unquestioned premise, twice (BUI-582 after BUI-578; BUI-594 after BUI-592). That
doc's own fix was prose: an inline "Since resolved — do not read the next sentence as live
guidance" banner. Prose correction works if the reader gets that far. `status:` makes the
same signal visible at the top of the file, before `applies_when`, and machine-checkable.

## Guidance

**`status:` is a closed, single-valued vocabulary, orthogonal to `mechanized_by:`.** They
answer different questions — `mechanized_by:` is *how is this enforced*, `status:` is *does
this still hold* — and neither constrains the other. A doc can be `mechanized_by: lint` and
`status: falsified` at once: the lint still runs, it is just enforcing a claim that turned
out to be wrong, which is itself worth knowing.

| `status:` | means | `superseded_by:` |
|---|---|---|
| `active` | the claim is believed current (the **default** — see below) | not required |
| `corrected` | the doc's overall thesis still holds, but a specific claim in it was wrong and has since been fixed or clarified | **required** |
| `falsified` | the doc's core claim was tested and shown false | **required** |
| `superseded` | the claim was true but has been replaced by a newer mechanism or convention | **required** |

**`status:` is optional and defaults to `active` when absent — unlike `mechanized_by:`,
there is no ledger and no obligation to backfill all 47 pre-existing docs.** Add the key
only when a doc's claim is known to need flagging, at the moment the correction is
discovered — the same write-time-not-backlog-sweep discipline `mechanized_by:` argues for,
applied to a smaller, narrower axis. A bulk pass stamping `status: active` on every doc that
needs no correction would be pure noise: it multiplies the diff by 47 without encoding any
new information, since absence already means active.

**Any non-`active` status requires `superseded_by:`, a string of at least 10 characters
naming what corrected, falsified, or superseded the doc.** It may be a doc path, a Linear
ticket id, or a sentence — whichever actually locates the current truth. This mirrors
`mechanized_by:`'s "a claim of enforcement that does not name the enforcer is worth
nothing": a claim of correction that does not name the correction is the same failure one
level up. An optional `superseded_date:` (`YYYY-MM-DD`) records when the status changed;
recommended whenever the date is known, not required.

**Ground the correction before stamping it — do not stamp `falsified` on inference alone.**
A doc's claim is corrected/falsified/superseded when there is verifiable evidence: a Linear
ticket's closing comment with a measured result, a shipped fix that changed the code the
doc describes, or another doc that supersedes it. If a doc's status is ambiguous, read the
ticket (`linear issue view <ID>`) or the current code before deciding — the same standard
`verify-ticket-premise-before-implementing.md` already asks of ticket premises, applied to
solutions docs' premises.

### Reconciliation: three docs already used a free-text `status:` key before this contract

Three pre-contract docs carry a bare `status:` key with values outside this vocabulary,
predating BUI-608. Silently redefining that key's meaning would corrupt whatever already
reads it, so each was reconciled explicitly rather than overwritten:

- **`conventions/shared-singleton-connection-rollback-on-unexpected-exception.md`** already
  used `status: superseded` with `superseded_by:` (free text: "BUI-400 (staged rollout
  BUI-407..BUI-410), landed 2026-07-18") and `superseded_date:`. Its value happens to match
  this contract's vocabulary exactly and its `superseded_by:` is free text, which this
  contract explicitly allows — so it needed **no change**. It is the doc this contract
  formalizes, not a hazard.
- **`ui-bugs/collection-check-alias-and-printing-false-positives.md`** used `status:` to
  record the underlying bug's *resolution progress* ("mitigated (advisory flags in the
  skill; ...)") — a different axis than "does the claim still hold." Renamed to
  `resolution_status:` (value unchanged) and given a fresh `status: active` (the documented
  false-positive blind spots are still live and correctly described).
- **`ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`** used `status:` the same way
  ("diagnosis-only (fix tracked in BUI-50)"). Same treatment: renamed to
  `resolution_status:`, given `status: active` (the doc remains the canonical incident
  record CLAUDE.md's "Endpoint parity matters" section points at — that reference is what
  keeps the doc active regardless of the underlying ticket's current state).
  `resolution_status:` value is carried over **verbatim** and not re-verified against
  BUI-50's current state (BUI-50 is now Done) — correcting stale resolution narrative is a
  separate concern from this axis and is out of scope here.

The rule going forward: `status:` means *epistemic status of this doc's claim*, never
*resolution progress of the underlying bug*. A doc that wants to record the latter uses
`resolution_status:` (free text, no closed vocabulary, no lint) instead.

### Retrieval filtering — the in-repo half

`./scripts/solutions-lint --status [--only=corrected,falsified,superseded]` lists every
`docs/solutions` doc with its status and, for non-active ones, its `superseded_by:`
pointer. It is read-only and always exits 0 (a filter, not a gate) — the mechanism any
retrieval consumer, in-repo or not, can point at to avoid surfacing a dead claim as live
guidance.

**What this does not cover.** Two consumers named in BUI-608 live outside this repository
and are not implemented here: `/ce-compound` (a plugin skill) is meant to stamp a doc
`corrected`/`superseded` when it writes the doc that replaces it, and the
`ce-learnings-researcher` plugin agent is meant to filter or flag non-active docs when it
surfaces search results. Neither file is reachable from this repo's working tree — editing
either would not appear in this PR's diff, would not run under this repo's CI, and would
not be reviewable here. `--status` is the interface those consumers would call; wiring them
to call it is out-of-repo follow-up work, the same shape BUI-605 left for its own
plugin-side half.

## Why This Matters

- **A stale claim read as live guidance is the exact failure this whole contract exists to
  prevent.** `vacuous-partition-guards-and-mutable-identity-keys.md` named a generator that
  had already been measured not to exist, the same day it was measured. Nothing in the
  frontmatter said so; only a reader who also happened to read BUI-559's ticket comment
  would know.
- **The failure compounds under retrieval.** `docs/solutions/` is designed to be searched
  by frontmatter metadata (`ce-learnings-researcher`'s stated method), not read cover to
  cover. Metadata that cannot say "this claim died" is metadata that will keep serving the
  claim.
- **Prose corrections are necessary but not sufficient.** The inline banner in
  `verify-ticket-premise-before-implementing.md` works for a human reading top to bottom;
  it is invisible to anything that indexes frontmatter, greps for `status:`, or reads only
  the first screen.

## When to Apply

- Any time evidence surfaces that a docs/solutions doc's claim is wrong, partly wrong, or
  has been replaced — stamp it at that moment, not in a later sweep.
- Writing a new doc that documents a correction to an existing one: stamp the *old* doc
  `corrected`/`falsified`/`superseded` with `superseded_by:` naming the new doc, in the same
  change.
- Reading a docs/solutions doc before relying on its guidance: check `status:` first.

## Examples

```yaml
# the reconciled precedent (already used status: superseded before this contract existed)
status: superseded
superseded_by: "BUI-400 (staged rollout BUI-407..BUI-410), landed 2026-07-18"
superseded_date: 2026-07-18
```

```yaml
# this ticket's grounded backfill: a specific claim was corrected, doc's thesis still holds
status: corrected
superseded_by: "BUI-559 (closed Won't Do 2026-07-28) measured the proposed publisher-relabel
  gate against the live store: it matches zero rows. The DC/Panini pairs are Italian
  licensed editions, not a relabel. See docs/solutions/conventions/
  verify-ticket-premise-before-implementing.md, Example 9, for the full account; the real
  generator is tracked as BUI-563/BUI-564."
superseded_date: 2026-07-28
```

```yaml
# invalid: non-active status with no pointer to what corrected it
status: falsified   # solutions-lint: requires the companion key `superseded_by:`
```

### Running the pack

```sh
./scripts/solutions-lint              # includes this lint in the pack; exit 1 on any finding
./scripts/solutions-lint --self-test  # proves it can both fail and stay quiet
./scripts/solutions-lint --status     # list every doc's status (read-only, always exit 0)
./scripts/solutions-lint --status --only=falsified,corrected,superseded
```

## Related

- `docs/solutions/conventions/mechanized-by-frontmatter-contract.md` — the sibling axis
  this one is orthogonal to; its "Interaction with the status axis (BUI-608)" section
  anticipated this doc.
- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` — the doc that
  independently discovered the same failure mode at ticket granularity, including the
  Example 9 / BUI-559 finding this contract's backfill grounds itself in, and the "seventh
  failure mode" naming the exact laundering risk `status:` closes.
- `docs/solutions/architecture-patterns/vacuous-partition-guards-and-mutable-identity-keys.md`
  — the doc backfilled `status: corrected` under this contract.
- `scripts/solutions-lint` — the runner; `solutions-status` is registered in its `LINTS`
  tuple alongside `solutions-frontmatter`.
- Tickets: BUI-608 (this contract), BUI-605 (the `mechanized_by:` contract this extends),
  BUI-559 (the grounded correction backfilled here), BUI-578/582/592/590/579 (named in
  BUI-608 as corrections to backfill; investigated and found to have **no** docs/solutions
  doc asserting them as live guidance — see the ticket's PR description for the full
  grounding of that finding).
