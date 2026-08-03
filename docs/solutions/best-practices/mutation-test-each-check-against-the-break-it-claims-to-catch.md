---
title: "Mutation-test each check against the specific break it claims to catch"
date: 2026-08-03
category: best-practices
module: "scripts/premise-check, scripts/solutions-lint, any self-test or CI gate"
problem_type: best_practice
component: testing_framework
severity: high
mechanized_by: test
enforced_by_test:
  - scripts/solutions-lint
applies_when:
  - "Adding a --selftest / --self-test mode, or wiring an existing one into CI"
  - "Adding a check to a lint pack, or a regression test for a documented learning"
  - "Reviewing a PR whose value is 'this is now covered' rather than a behavior change"
  - "A green check is about to be cited as evidence that something is safe"
related_components:
  - "ci"
  - "tooling"
tags:
  - "mutation-testing"
  - "vacuous-tests"
  - "selftest"
  - "ci"
  - "coverage"
---

# Mutation-test each check against the specific break it claims to catch

## Context

`scripts/premise-check` (BUI-613) shipped with a `--selftest` mode. It exited 0 and printed
`SELFTEST OK — all checks passed.` BUI-632 was filed to wire it into CI, on the reasonable
assumption that a passing self-test meant a working tool.

Before wiring it, the self-test was mutation-tested: a hard `raise AssertionError` was
injected immediately before the `difflib.SequenceMatcher` call that drives near-miss
detection. **`--selftest` still exited 0.** The injected line was never reached.

Every `check(...)` in `selftest()` covered **extraction** — parsing symbols, file paths and
line-specs out of ticket prose. **Zero** covered **classification**: the
`confirmed` / `drifted` / `absent` / `renamed-near-miss` / `unverifiable` verdicts. That is
where the tool's own stated design principle lives — *"false confidence is the only real
failure mode"* — and `renamed-near-miss` is the verdict another ticket (BUI-630) cited as
load-bearing.

Wiring it into CI as filed would have added a green check over a detector nothing tested.

## Guidance

**A check that passes is not evidence until you have watched it fail.** For any self-test,
lint, or CI gate, break the logic it claims to cover and confirm the check goes red.

**Mutation-test each check against its *own* target break, not the suite as a whole.** This
is the part that is easy to skip and where the real finding was. After classification
checks were added for endpoint near-miss detection, the suite failed correctly under
mutation — but testing each check separately showed that **check #1 would not have caught
the regression on its own**. Its similarity score cleared the 0.5 threshold whether the
IDF-weighting was present or not. Check #2 — the one asserting a *false* near-miss is
**not** produced despite shared boilerplate — was doing all the work.

Had only check #1 been written, the suite would have passed, looked like coverage, and
caught nothing. A suite-level mutation test would not have revealed that; only
per-check attribution did.

**Require a named failure, not an uncaught crash.** A first revision of these checks failed
under mutation by raising, which technically goes red but reports nothing about *which*
property broke. Fixed so the mutation produces two named `check()` failures. A crash tells
you something is wrong; a named failure tells you what stopped being true.

**The three questions, in order:**

1. What break is this check supposed to catch?
2. Does introducing exactly that break turn it red?
3. Is *this* check the one that goes red, or is a neighbour covering for it?

## Why This Matters

A vacuous check is worse than no check, because it is cited as evidence. This is the
mechanism behind
`docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md`
— that doc diagnoses the class ("a verification whose failure looks identical to success"),
where this one supplies the method for proving you are not in it.

The failure is quiet by construction: the tool prints OK, the CI badge is green, and the
gap is invisible until the thing you thought you were protected against actually happens.
`premise-check` exists to stop stale ticket premises reaching implementation; its untested
detector meant it could have silently stopped doing that at any point with no signal.

This repo already mechanizes the discipline for exactly one population, and it is the reason
that population is trustworthy: `scripts/solutions-lint` refuses to register a lint without
non-empty `SelfTest(must_flag=…, must_pass=…)` fixtures, and `--self-test` runs them in CI
ahead of the pack itself. You cannot add a lint here that is unable to fail. That is what
`mechanized_by: test` on this doc points at, and its scope is **lints in the pack, and
nothing else**.

Note precisely what is *not* covered, because the distinction is this doc's own subject:
`scripts/premise-check --selftest` is deliberately **not** listed as an enforcing test. It
runs in CI, but nothing forces its checks to be non-vacuous — delete the classification
checks and it goes green again, which is the exact bug described above. Citing it as
enforcement would reproduce the error the doc exists to prevent. For every check outside the
lint pack, the mutation test is discipline, performed by a person, and it leaves no artifact
behind that a lint could look for.

## When to Apply

- Before wiring any self-test into CI. "It passes" is the beginning of the check, not the end.
- When a ticket's deliverable is coverage rather than behavior — there is no failing test to
  turn green, so mutation is the only evidence available.
- When adding a check to `scripts/solutions-lint` — the pack's `SelfTest(must_flag=…,
  must_pass=…)` fixtures are mandatory for exactly this reason.
- When reviewing someone else's "now covered" claim.

## Examples

Confirming the gap, before any fix (BUI-632):

```python
# inject before the only near-miss call site in scripts/premise-check
raise AssertionError("MUTANT: near-miss path reached")
```

```
$ ./scripts/premise-check --selftest
SELFTEST OK — all checks passed.
EXIT: 0          # <- the injected raise was never reached; the path is untested
```

Confirming the fix, per check (BUI-632, PR #406):

```
# neutralize the IDF weighting -> naive segment Jaccard
def _distinctive(segs): return segs

$ ./scripts/premise-check --selftest
# two NAMED check() failures, not a crash:
#   endpoint: /api/comics/rejections must not be a false near-miss  FAILED
#   ...
EXIT: 1
```

And the attribution that mattered: run each check alone against that same mutation. Check 1
(`/api/health` -> NEAR_MISS `/health`) still passes — its score clears 0.5 either way.
Only check 2 (the must-*not*-match assertion) actually detects the regression.

## See also

- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md`
  — the diagnosis this doc supplies the method for; read both.
- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` — BUI-632's own
  premise ("it has no tests") was drifted; the tool had a self-test, it was just vacuous.
- `docs/solutions/conventions/mechanized-by-frontmatter-contract.md` — why a learning that
  merely gets documented is considered still-open.
- `docs/solutions/architecture-patterns/vacuous-partition-guards-and-mutable-identity-keys.md`
  — the same "a metric that cannot fail is worse than no metric" shape, in the DB-invariant
  domain rather than the test-harness one.
