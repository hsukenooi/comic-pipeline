---
title: "A probe of a write endpoint is a write — probe only the refused shape"
date: 2026-08-03
category: best-practices
module: "scripts/deploy.sh post-deploy verification, plugins/gixen-overlay POST /api/comics"
problem_type: best_practice
component: tooling
severity: high
mechanized_by: advice-only
advice_only_reason: "Whether a given payload triggers a side effect depends on the endpoint's
  own reconciliation logic, not on any property of the command being run; a lint cannot tell a
  safe probe payload from a destructive one without executing the handler."
applies_when:
  - "Live-probing a newly deployed endpoint to confirm it exists (the deploy checklist step)"
  - "About to POST/PUT/PATCH against production to verify a change shipped"
  - "Writing a verification step for a ticket whose surface is a write endpoint"
  - "A decision about a data population has been deferred and must not be disturbed"
related_components:
  - "ci"
  - "database"
tags:
  - "deploy-verification"
  - "production-data"
  - "probes"
  - "side-effects"
  - "em-batch"
---

# A probe of a write endpoint is a write — probe only the refused shape

## Context

The deploy checklist for this repo ends with a live probe: after installs and restarts,
empirically verify each new endpoint or subcommand actually exists on the target. Trusting
the install is how `uv tool install --force` once silently served a stale cached wheel with
the new subcommands missing (BUI-455). The probe is a good rule.

BUI-625 added a **422 refusal** at `POST /api/comics` for multi-issue lot listings. To probe
it after deploy, an invented lot title was POSTed:

```json
{"title": "Amazing Spider-Man #18 #19 #20 #21 #22 lot", "issue": "18"}
```

That title uses **spaces** as separators. The overlay's detector requires a `[,/&+]`
separator or the phrase `lot of N`, so it correctly did not fire — the probe simply used a
shape outside the rule's documented scope. But the request did not fail. It **succeeded**,
and because it carried no `year`, it entered `upsert_comic`'s yearless-orphan
reconciliation: `comics` row 448 (`Amazing Spider-man` #18, yearless) was collapsed into
canonical yeared row 497, cascading away `fmv` row 491.

The outcome was verified lossless — `fmv` 491 was an empty shell (`low`/`high` NULL,
`comps` 0), redundant with row 497's identical-grade shell 542; `bid_fmvs` stayed
byte-identical at 613; zero `bids.fmv_id` references; zero dangling refs. It is exactly what
`POST /api/sweep-orphans` does deliberately.

**But it was not authorized.** Row 448 was one of 16 yearless-orphan pairs on which the user
had *explicitly deferred* a decision an hour earlier. The probe silently made that
population 15.

## Guidance

**"Read-only probe" is a property of the endpoint, not of your intent.** A probe of a write
endpoint is a write. Naming it a probe changes nothing about what the handler does.

**Probe only the shape guaranteed to be REFUSED.** When verifying that a new refusal exists,
send the payload that triggers the refusal — a 422 writes no row, and the rejection is
itself the evidence you wanted. In this case the correct probe was the detector's own
docstring example:

```json
{"title": "Amazing Spider-man #18,19,20,21,22 lot of 5", "issue": "18"}
```

which returned 422 with a useful message and landed in the BUI-601 rejections ledger,
confirming two tickets composed end-to-end in production while writing nothing.

**If you must probe a shape that succeeds, do it against a throwaway DB**, not the live one.

**A probe that "did nothing visible" is a claim, not an observation.** The success response
here looked entirely benign — it returned an existing row, with an existing `created_at`.
Nothing in the response mentioned a deletion. The side effect was only found by diffing row
counts against the pre-existing backup, which was taken for an unrelated remediation.

**Beware the payload that omits a field.** Omitting `year` is what routed the request into
reconciliation rather than a plain lookup. The most dangerous probe payload is a plausible,
under-specified one, because under-specification is what activates the merge, promote, and
heal paths that exist to clean up incomplete data.

## Why This Matters

Deploy verification is performed at the end of a long session, under the belief that the
risky work is already done. That is exactly when a write is least expected and least
scrutinized. The probe here ran *after* a production-data remediation had been completed
under a full backup → apply → diff ritual — the careful part was over, and the casual part
did the unplanned write.

The damage was bounded only by luck: the reconciliation this triggered happens to be
lossless by design. Had the invented payload matched a different code path, the same
casualness would have produced a real loss with no backup taken immediately beforehand.

The generalizable rule is that the ritual which governs deliberate production writes —
backup, apply, diff, attribute every changed field — has to extend to any request that
*could* write, including the ones framed as verification.

## When to Apply

- Every post-deploy live probe of a `POST` / `PUT` / `PATCH` / `DELETE` surface.
- Whenever a verification step is being written into a ticket or a runbook.
- Especially when a data-population decision has been deferred — a probe must not resolve it.

## Examples

Wrong — succeeds, and reconciles live rows:

```sh
comics-api POST /api/comics -H 'Content-Type: application/json' \
  -d '{"title":"Amazing Spider-Man #18 #19 #20 #21 #22 lot","issue":"18"}'
# 200 OK, returns existing row 497 — and silently deletes yearless orphan 448
```

Right — exercises the new surface, writes nothing:

```sh
comics-api POST /api/comics -H 'Content-Type: application/json' \
  -d '{"title":"Amazing Spider-man #18,19,20,21,22 lot of 5","issue":"18"}'
# 422, refusal recorded in the rejections ledger, no comics row created
```

Detecting the unplanned write afterwards (how it was actually caught):

```sh
sqlite3 ~/.comics-server/db.sqlite "SELECT COUNT(*) FROM comics;"
# 661 — expected 662 after the planned remediation. One row unaccounted for.
```

Then diff live against the backup and attribute **every** changed row and field to a named
writer before concluding anything.

## See also

- `docs/solutions/integration-issues/locg-export-deletes-owned-wished-books.md` — the other
  class where a well-intentioned, correct-looking call deletes data.
- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md`
  — a probe whose side effect is invisible in its own response is the same shape: the check
  reports success either way.
- `CLAUDE.md` → the deploy checklist's live-probe requirement, which this doc constrains
  rather than contradicts.
