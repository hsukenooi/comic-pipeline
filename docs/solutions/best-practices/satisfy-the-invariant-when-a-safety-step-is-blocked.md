---
title: "When a safety step is blocked, satisfy its invariant — never skip it, never weaken it"
date: 2026-08-03
category: best-practices
module: "Wave 5 live-data ops (BUI-649 quarantine, BUI-651 rekey_sweep); packages/locg-cli CollectionCache.apply; plugins/gixen-overlay POST /api/comics/collection/quarantine"
problem_type: best_practice
component: tooling
severity: high
mechanized_by: advice-only
advice_only_reason: "Which invariant a blocked step protects is a property of that step's
  purpose, not of any string in the repo. No predicate can separate a legitimate stronger
  substitution from a rationalized skip without knowing what the step was for, so this
  closes on the operator naming the invariant, not on a check."
applies_when:
  - "A documented safety step in a production-data ritual is unavailable — permission denied, service cannot be stopped, no maintenance window"
  - "About to record a deviation of the form 'skipped step X'"
  - "A ticket's acceptance criteria name a procedure rather than a property"
  - "Deciding whether a verification step is warranted for a given risk class"
related_components:
  - "database"
  - "service_object"
tags:
  - "production-data"
  - "remediation"
  - "invariants"
  - "locking"
  - "em-batch"
  - "deviations"
---

# When a safety step is blocked, satisfy its invariant — never skip it, never weaken it

## Context

Wave 5 of the BUI-611 batch was two remediations against the live collection store on the
Mac Mini: BUI-649 (quarantine 6 cross-edition Panini twins) and BUI-651 (merge 3 identity
collision groups via `rekey_sweep`). The recorded ritual, inherited from BUI-626/636, is:

> quiesce the server → durable backup outside the store → re-measure → apply → diff proving
> only intended rows/fields changed → row-count check

The session's permission classifier denied `launchctl bootout`. (It also denied
`scripts/deploy.sh` and every production write, so those were handed to the user — a
separate matter.) The quiesce was unavailable for the entire session, not momentarily.

Two tempting moves are both wrong:

- **Skip it and note the deviation** — "the store's mtime is six days old, nothing is
  writing to it." True, and still an argument from probability about the exact data class
  (BUI-122 / BUI-200) where being wrong deletes owned books.
- **Reconstruct the blocked command from parts** — run the individual `uv tool install`
  lines instead of `deploy.sh`, or `LOCG_DATA_DIR=... locg collection quarantine` instead
  of the endpoint. That defeats the denial rather than respecting it, and in this case the
  substitute was also the *weaker* path (writing the file behind the running server's back).

## Guidance

**Name the invariant the blocked step protects, then satisfy that invariant — ideally more
strongly — and assert it by measurement.**

Quiescing is not the goal. The invariant it buys is BUI-626/636's: *every changed field is
attributable to a named writer.* Stopping the service is merely one way to get there. Two
better ones were available:

**1. Serialize through the single writer instead of stopping it.** BUI-649's writes went
through the shipped `POST /api/comics/collection/quarantine`; BUI-651's went through
`CollectionCache.apply` directly. Both run the full read-mutate-write cycle inside an
exclusive `flock`, and the endpoint additionally re-resolves the row identity and re-runs
the last-owned-row guard *inside the lock*.

This is strictly stronger than quiescing. Quiescing prevents the writer you thought of; the
lock serializes against every writer, including the one you didn't.

**2. Assert the invariant directly instead of inferring it from the ritual.** Enumerate the
only permitted delta in advance, then diff the whole store against the durable backup:

```python
# The ONLY permitted per-row delta is the `quarantined` key appearing.
# Anything else is unattributed drift and fails loudly.
before_by_id = {}
for r in backup_rows:
    before_by_id.setdefault(make_identity(r), []).append(r)

drift = []
for r in live_rows:
    stripped = {k: v for k, v in r.items() if k != "quarantined"}
    candidates = before_by_id.get(make_identity(r), [])
    if any({k: v for k, v in c.items() if k != "quarantined"} == stripped
           for c in candidates):
        continue
    drift.append(r)

assert not drift, f"{len(drift)} rows changed fields nobody claimed"
```

Both ops reported **0 rows** of unattributed drift. That is the property the quiesce existed
to make likely — measured, not assumed.

## Why This Matters

A ritual is a proxy for a property. Proxies are cheap to follow and easy to perform without
understanding, which is how a step outlives the reason for it, and why skipping one feels
either harmless or catastrophic with no way to tell which from the inside. Naming the
invariant converts *"did I perform the steps?"* into *"is the property true?"* — and only
the second one is checkable.

It also converts a blocked step from a dead end into a design question. Had quiesce been
treated as the requirement rather than as a means, both Wave 5 ops would have been blocked
for the whole session, and the six cross-edition twins would still be double-owned.

The discipline cuts both ways, and the second direction is the one that quietly wastes time:
**do not perform a ritual step whose invariant is already guaranteed.** Re-running a Python
suite for a markdown-only diff looks like diligence and buys nothing.

## When to Apply

- A documented safety step is unavailable and the work is otherwise ready to proceed
- You are about to write "deviation: skipped X" — instead write what X protected and how
  that property is covered now
- Acceptance criteria name a procedure (*"take the diff with the server quiesced"*) rather
  than a property (*"every changed field is attributable"*)
- Choosing verification depth for a risk class, in either direction

## Examples

**The deviation as recorded on BUI-649 and BUI-651** — the shape worth copying, because it
survives an audit:

> **Deviation — the server was not quiesced.** `launchctl bootout` was blocked by the
> session's permission classifier. Rather than substitute something weaker, writes were
> routed through the shipped endpoint, whose `CollectionCache.apply` takes an exclusive
> flock and re-resolves the identity + re-runs the guard inside the lock. Serializing
> through the single writer is a stronger guarantee than stopping the service, and the
> BUI-626/636 invariant it protects — every changed field attributable to a named writer —
> was asserted directly (0-drift whole-store diff), not assumed.

Note what makes it auditable: it names the blocked step, the invariant, the substitute, why
the substitute is *not weaker*, and the measurement that confirms it. A deviation missing
any of those five is a skip wearing a deviation's clothes.

**A same-session instance of the same reflex.** BUI-649 drove
`owned_duplicate_identities_cross_edition` from 6 to 0. The acceptance criterion is a number
reaching zero, but the property is *the rows were dispositioned, not the detector blinded* —
so the verification also asserts that the detector still finds all six when quarantine is
ignored. That is an instance of
[mutation-test-each-check-against-the-break-it-claims-to-catch](mutation-test-each-check-against-the-break-it-claims-to-catch.md);
recorded here only as evidence the two halves are the same habit.

## Related

- [`best-practices/a-probe-of-a-write-endpoint-is-a-write.md`](a-probe-of-a-write-endpoint-is-a-write.md)
  — the adjacent rule for the deploy-verification half of the same ritual
- [`best-practices/mutation-test-each-check-against-the-break-it-claims-to-catch.md`](mutation-test-each-check-against-the-break-it-claims-to-catch.md)
  — prove a check can still fail before citing it as evidence
- [`architecture-patterns/vacuous-partition-guards-and-mutable-identity-keys.md`](../architecture-patterns/vacuous-partition-guards-and-mutable-identity-keys.md)
  — `status: corrected`; the counter-goes-vacuous class these twins were first found under
- BUI-626 / BUI-636 — where the attributable-field invariant was established, and why the
  ritual says "quiesce"
- BUI-649, BUI-651 — the two ops this was applied to
