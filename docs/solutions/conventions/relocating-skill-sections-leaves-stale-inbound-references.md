---
title: "Slimming a skill body: grep inbound references before relocating a section"
date: 2026-07-19
category: conventions
module: ".claude/commands/comic/*.md (skill bodies) + docs/ (relocation targets) + CONCEPTS.md"
problem_type: convention
component: documentation
severity: low
related_components:
  - "documentation"
applies_when:
  - "Slimming a /comic:* skill body by moving a section (math spec, incident narrative, BUI rationale) into docs/"
  - "Any doc-slimming ticket in the BUI token-efficiency class (e.g. the BUI-429/431/434/437/438/441/445/448 wave)"
  - "Renaming or splitting a section that other files cite by number or name (e.g. 'fmv.md §7a')"
symptoms:
  - "A sibling skill or CONCEPTS.md points at a section (by § number or name) that no longer lives where the pointer says"
  - "A relocation PR looks self-contained but silently orphans a cross-reference one hop away"
tags: [skill-slimming, docs-relocation, stale-reference, cross-reference, token-efficiency, BUI-438, BUI-452, em-batch]
---

# Slimming a skill body: grep inbound references before relocating a section

## Context

The BUI-420..449 em-batch (26 tickets, 2026-07-19) ran a large token-efficiency wave that slimmed `/comic:*` skill bodies by relocating rationale, math specs, and incident narratives out to `docs/` and linking them: BUI-429 (collection-add), BUI-431 (collection-sync), BUI-434 (buy), BUI-437 (snipe-add + buy), BUI-438 (fmv), BUI-441 (seller-scan), BUI-445 (collection-check), BUI-448 (wishlist-add). Each move was locally correct — the moved content survived verbatim in a new `docs/` file, and the source skill linked it.

The trap is one hop away: **other files reference the moved section by § number or name, and those pointers go stale silently.** A relocation diff that only touches the source skill + the new doc looks complete, and CI stays green (there is no cross-reference linter), so the orphaned pointer ships unnoticed.

Concrete instance: BUI-438 moved `fmv.md` §1–§10 math into `docs/conventions/fmv-math-spec.md`. `CONCEPTS.md:131` ("the fmv.md §7a step reads realized graded prices…") and `verify.md:151` ("see `/comic:fmv` §7/§7a") still pointed at `fmv.md §7a`, which no longer exists there. Not broken (fmv.md links onward), but a reader now takes an extra, misdirected hop. Filed as BUI-452.

## Guidance

Before finishing any ticket that **relocates or renames a section** of a skill body:

1. Grep the repo for inbound references to the section you are moving — by its § number, its heading text, and the skill path. For a move out of `fmv.md`:
   ```bash
   grep -rn -e 'fmv\.md' -e '§7a' -e '§7' -e 'CGC-proxy' \
     .claude/commands CONCEPTS.md docs/ | grep -v docs/conventions/fmv-math-spec.md
   ```
2. For every hit **outside** the file you just wrote, update the pointer to the new location, or hand it to the follow-up ticket that owns the concrete edits (in this batch, BUI-452 owned the `CONCEPTS.md`/`verify.md` fixes so the compound step stayed scoped to the principle).
3. Callers cite sections by number/name, not just by file path — a plain `grep 'fmv.md'` misses `see §7a`. Search the section identifiers too.

Historical snapshots under `docs/audit/`, `docs/plans/`, `docs/brainstorms/` are dated records — leave their stale line numbers alone; only live cross-references need fixing.

## Why This Matters

The whole point of skill-slimming is to cut the fixed per-invocation token cost while keeping every load-bearing pointer intact. A silently-orphaned reference is the small tax you pay for skipping the grep: the next agent that follows the pointer wastes a hop (or gives up), which erodes the very navigability the relocation was supposed to preserve. It is cheap to prevent at move time and annoying to discover later (you have to re-derive where the content went).

The same batch also surfaced the load-bearing inverse — a section that must **not** be relocated. Data-loss safety gates (collection-sync's "abort on any Deleted from Collection.", the In-Collection=0 warning, backup-first hard-stop) stay inline verbatim; only the *rationale* moves. BUI-431 deliberately trimmed only ~7% rather than the ticket's "roughly half" target because the rest of the file was safety gates and executable steps. A conservative trim that preserves every safety gate is the correct outcome, not a failure.

## When to Apply

- Any doc-slimming / token-efficiency ticket that moves content out of a skill body.
- Section renames or splits, even without a slim (renumbering §7 → §7a orphans `see §7`).
- **Not** for pure prose edits inside a section that stays put, and **not** for the dated historical docs above.

## Examples

Related reinforced pattern (established BUI-353 class, not new): six tickets this batch moved hand-authored LLM glue into tested code, each exposed as **both** a `locg` CLI command and an overlay endpoint reusing `CollectionCache.apply`'s flock + `.bak` rotation + audit — BUI-449 (series-name resolve), BUI-432 (audit-pending), BUI-435 (add-batch builder), BUI-428 (record-win/commit), BUI-433 (backup/restore), BUI-427 (remediation). Two durable sub-lessons for that class of endpoint:

- **Re-resolve the target inside the exclusive lock and self-verify the match count** before mutating (TOCTOU-safe) — the store can change shape between the cheap pre-check and the lock. BUI-427/428/433 all mirror the BUI-254/BUI-417 pattern.
- **Surgically correcting a mis-filed row requires matcher-bypass-by-stable-identity** (`gixen_item_id`, or exact `full_title`+`release_date`+`source`), never the fuzzy ownership check — the fuzzy matcher's masthead-alias / X-Men-split / leading-article normalization is exactly what can't disambiguate the mis-file (BUI-427, replacing BUI-424's one-off script).

See also `docs/solutions/conventions/bid-group-purge-is-hygiene-not-safety-net.md` (a sibling relocation from BUI-437 this batch).
