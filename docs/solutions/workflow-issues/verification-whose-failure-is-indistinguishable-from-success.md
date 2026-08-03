---
title: "A verification whose failure looks identical to success is worse than no verification"
date: 2026-08-03
category: workflow-issues
module: "EM/agent verification steps (any shell-based check), git conflict resolution across parallel agents"
problem_type: workflow
component: tooling
severity: medium
mechanized_by: advice-only
advice_only_reason: "The failure mode is a judgment error about what a check proves, not a code pattern — the same shell line is correct or catastrophic depending on what conclusion is drawn from it. One sub-case is mechanizable and noted below as a candidate, but the general discipline (derive the expected value before running the check) cannot be expressed as a repo lint."
applies_when:
  - "Verifying an agent's reported work before merging (EM step: 'match the check to the risk class')"
  - "Reading an exit code from a shell command that is piped, or whose globs may not expand"
  - "Resolving a git conflict where two parallel agents appended to the same registry/list"
  - "Any moment where an empty result is about to be read as 'nothing wrong'"
related_components:
  - "ci"
tags:
  - "verification"
  - "exit-codes"
  - "shell-quoting"
  - "merge-conflicts"
  - "parallel-agents"
  - "em-batch"
---

# A verification whose failure looks identical to success is worse than no verification

## Context

During the BUI-605..608 batch, three separate checks *appeared* to pass while proving
nothing. Each one would have been reported as verified. All three shared a shape: **the
broken form of the check and the passing form produce the same output.** No error, no
warning — just a clean-looking result that licenses a wrong conclusion.

This is strictly worse than skipping the check. A skipped check leaves you uncertain and
appropriately cautious; a silently-broken one leaves you confident and wrong.

## The three instances

**1. A piped exit code reports the pipe's last command, not yours.**

```sh
./scripts/solutions-lint 2>&1 | tail -4; echo "EXIT=$?"   # always 0 — that is tail's status
./scripts/solutions-lint >/dev/null 2>&1; echo "EXIT=$?"  # the lint's actual status
```

The first form was used to confirm a new CI lint *fails* on bad input. It printed
`EXIT=0`, which — had it been believed — would have meant the lint was decorative and the
CI gate enforced nothing. The lint was in fact correct (it exits 1); the *measurement* was
broken. Note the trap is worst when you expect success: an expected-0 that reads 0 is
never questioned.

**2. A glob that fails to expand aborts the command, and the empty output reads as "clean".**

```sh
grep -rn --include=*.py "pattern" .     # zsh: "no matches found" — grep NEVER RAN
grep -rn --include="*.py" "pattern" .   # correct
```

Unquoted `*.py` is expanded by zsh before `grep` sees it. With no matching file in the
working directory the shell errors and the command never executes. The output is empty —
identical to a genuine "no occurrences found", which was about to be read as *"nothing
consumes this key, so the rename is safe."*

**3. Keeping "both sides" of a conflict that only looks like two independent appends.**

Two parallel agents each registered a new entry in the same `LINTS` tuple. The conflict
presents as append-vs-append, so the obvious resolution is to keep both:

```sh
sed -e '/^<<<<<<< /d' -e '/^=======$/d' -e '/^>>>>>>> /d' file   # union resolution
```

Git had split the hunks **mid-`Fixture(`**, so concatenating the sides produced a spliced
half-structure. Here the failure was loud (a `SyntaxError`), but only by luck of the file
being Python. In a data file — YAML, JSON, a fixture list, a CSV — the same union
resolution yields a *syntactically valid* file that silently lost or duplicated entries.

## Guidance

**Derive the expected value before running the check, not after.** The single most
effective habit here. When merging two agents' additions to one registry, the pass
condition was computed in advance from what each contributed: BUI-605 gave 1 lint / 13
fixtures, BUI-608 added 1 / 10, BUI-606 added 2 / 15 — therefore a correct merge must
self-test at **4 lints and 38 fixtures**. That number is falsifiable and cannot be
rationalized. "It runs and looks right" can be.

**Make the check fail once, on purpose.** Before trusting that a gate blocks, break its
input and confirm it goes red. A gate never observed failing is an assumption, not a gate.

**Treat an empty result as unproven, not as proven-negative.** Empty output has at least
three causes: genuinely nothing found, the command never ran, or it searched the wrong
tree. Distinguish them — echo a marker, check the exit code unpiped, or run the same
search against a known-positive control.

**Never read `$?` through a pipe.** Redirect to `/dev/null` and check the exit code, or
use `PIPESTATUS`/`pipefail`. If output is needed too, capture it first, then test.

**When resolving a conflict between parallel agents, reconstruct structurally.** Do not
resolve textually hunk-by-hunk. Take one side's complete, valid file as the base and
splice the other's blocks in at clean boundaries, then parse-verify (`ast.parse`, a YAML
load, a schema check) *and* confirm the derived count. If the reconstruction is beyond
you, hand it back to the agent that wrote the code — it has the structure in context. A
correct rebase is worth more than a saved resume.

## Mechanization candidate

Instance 1 is the one genuinely expressible as a check: flag a `$?` read (or an `EXIT=`
echo) immediately following a pipeline, in skills and scripts. It is not in the pack
today. Instances 2 and 3 are judgment, which is why this doc is `advice-only` — the same
`sed` union resolution is correct for an append-only ledger and catastrophic for a
structured list, and no lint can tell those apart.

## See also

- `docs/solutions/workflow-issues/multi-block-skill-shell-state-loss-fallback-swallow.md` —
  the adjacent class where a shell block's *state* silently fails to carry, and the
  swallow (`|| echo`) that hides it.
- `docs/solutions/conventions/mechanized-by-frontmatter-contract.md` — why a learning that
  merely gets documented is considered still-open.
