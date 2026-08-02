---
title: "Every solutions doc declares what part of its learning is a check (mechanized_by:)"
date: 2026-08-02
category: conventions
module: "docs/solutions (frontmatter contract), scripts/solutions-lint (the pack + runner), .github/workflows/ci.yml (solutions-lint job)"
problem_type: convention
component: documentation
severity: medium
mechanized_by: lint
lint:
  - solutions-frontmatter
applies_when:
  - "Writing a new docs/solutions doc (/ce-compound, or by hand) — it must declare mechanized_by:"
  - "Editing an existing docs/solutions doc that is still listed in docs/solutions/UNMECHANIZED.txt"
  - "Adding a lint to the pack in scripts/solutions-lint (BUI-606, BUI-607)"
  - "A review or post-mortem produces a 'grep for X' heuristic — that heuristic is a lint, not a paragraph"
related_components:
  - "ci"
tags:
  - "mechanized-by"
  - "solutions-frontmatter"
  - "lint-pack"
  - "learning-to-lint"
  - "docs-solutions"
  - "ci-gate"
---

# Every solutions doc declares what part of its learning is a check (`mechanized_by:`)

## Context

`docs/solutions/` holds 47 learned-trap docs. Each one protects exactly the sessions
that happen to re-read it. Several of them already contain a mechanical check written
out in prose — the row-id doc's *"grep status-writing SQL for `WHERE item_id`"*, the
skill-state doc's `|| echo ""` detector — and every one of those is closed by discipline
alone. The `WHERE item_id` class has five incidents across two packages *after* being
documented.

The gap is not that the learnings are wrong; it is that they are inert. A learning that
runs is unconditional — it protects every future commit whether or not anyone read the
doc. So the completion criterion for an incident shifts: an incident is closed when the
learning is **enforced**, not when it is **documented**.

`mechanized_by:` is the frontmatter key that makes that criterion visible per doc, and
`scripts/solutions-lint` is where the enforcement actually lives.

## Guidance

**Every `*.md` under `docs/solutions/` carries `mechanized_by:`.** Its value is one of
a closed three-word vocabulary, or a list of them. Each value requires a companion key,
because a claim of enforcement that does not name the enforcer is worth nothing:

| `mechanized_by:` | means | companion key (required) |
|---|---|---|
| `lint` | a lint in the pack enforces this | `lint:` — list of registered lint ids |
| `test` | an existing automated test enforces this | `enforced_by_test:` — `path` or `path::node_id` |
| `advice-only` | no executable check can enforce this | `advice_only_reason:` — a sentence (≥20 chars) saying why |

`lint` and `test` may be combined (a doc's grep heuristic becomes a lint; its regression-test
shape is already a test). **`advice-only` is exclusive** — a learning that is mechanized is
not advice-only.

Both mechanized arms are verified, not taken on faith. `lint:` ids must resolve to a lint
registered in `scripts/solutions-lint`; `enforced_by_test:` paths must exist and, when a
node id is given, that node must still appear in the file. A renamed-away test silently
stops enforcing its learning, which is exactly the failure this key exists to catch.

**`advice-only` is a legitimate answer, and it is meant to be argued.** Some learnings
genuinely resist mechanization — *"prefer the pool-depth signal over dispersion"* is a
judgment call, not a predicate. The required reason is the whole value of the tag: it
forces the author to say *why* rather than skip the question, and it leaves a re-readable
record for whoever later finds an angle that does mechanize.

**Pre-contract docs live on a shrinking ledger.** The 47 docs that predate this contract
are listed in `docs/solutions/UNMECHANIZED.txt` and exempted. That file is **append-never**:
a new doc must never be added to it, classifying a doc means deleting its line, and the
lint fails if a listed doc has since gained the key — so the ratchet tightens itself and
cannot silently loosen.

**Writing a new doc? Answer the question at write time.** The moment to decide what part
of a learning is a check is while the incident is fresh and the reproduction is still in
your head — not months later during a backlog sweep. If the doc contains the words "grep
for", "always check that", or "never call X directly", that sentence is a lint waiting to
be written; file it (a `lint:` id can be added in the same PR as the lint) rather than
reaching for `advice-only`.

**Interaction with the status axis (BUI-608).** `status:`/`superseded_by:` is a separate,
orthogonal axis: `mechanized_by:` says *how a claim is enforced*, `status:` says *whether
the claim still holds*. They do not overlap and neither key constrains the other's
vocabulary. The natural cross-rule — a falsified doc must not keep a live lint pointed at
it — belongs to BUI-608 and is deliberately not implemented here.

## Why This Matters

- **Documented ≠ closed.** Five `WHERE item_id` incidents landed after the doc existed.
  Discipline is not a control; a merge gate is.
- **The pack's real failure mode is a lint that stopped firing**, not a missing lint. A
  green lint that can no longer fail is worse than no lint — it launders risk. Hence the
  proven-able-to-fail fixture requirement below.
- **`advice-only` keeps the pack honest.** Without an explicit escape hatch the contract
  would push authors to invent weak lints for unmechanizable learnings, and noisy lints
  erode trust in the whole pack faster than missing ones do.

## When to Apply

- Any new `docs/solutions/` doc — including ones written by `/ce-compound`.
- Any lint added to the pack (BUI-606: `WHERE item_id` non-unique-key mutations, the
  `|| echo ""` fallback-swallow; BUI-607: the skill-lint family).
- Any review or post-mortem that produces a grep heuristic.

## Examples

```yaml
# mechanized both ways — the grep heuristic became a lint, the regression shape is a test
mechanized_by:
  - lint
  - test
lint:
  - non-unique-key-mutation
enforced_by_test:
  - packages/gixen-cli/tests/test_ebay_fallback.py::test_fallback_won_write_spares_live_pending_sharing_item_id
```

```yaml
# not mechanizable — and the reason says why, so the next reader can challenge it
mechanized_by: advice-only
advice_only_reason: "Which of two same-named volumes a vintage book belongs to is a
  judgment call over cover art and publication gaps; no textual predicate decides it."
```

### Running the pack

```sh
./scripts/solutions-lint              # run the pack; exit 1 on any finding
./scripts/solutions-lint --self-test  # prove every lint is able to fail
./scripts/solutions-lint --list       # show the registered pack
```

CI runs `--self-test` and then the pack in the `solutions-lint` job
(`.github/workflows/ci.yml`), modeled on the `lint:` ruff job from BUI-185.

### Adding a lint (the registration seam)

Append one `Lint(...)` to the `LINTS` tuple at the bottom of `scripts/solutions-lint`:

```python
Lint(
    id="non-unique-key-mutation",              # kebab-case; docs reference this id
    summary="status writes on bids are keyed on row id, not item_id",
    doc="docs/solutions/design-patterns/scope-status-writes-to-row-id-not-item-id.md",
    check=check_non_unique_key_mutation,       # (root: Path) -> list[Finding]
    self_test=SelfTest(
        must_flag=(Fixture("item_id-wide UPDATE", {"server/db.py": "..."}),),
        must_pass=(Fixture("id-targeted UPDATE", {"server/db.py": "..."}),),
    ),
)
```

Two rules make a lint reviewable:

1. **`check` takes the tree root and resolves every path under it.** No `__file__`, no
   `os.getcwd()`. That is what lets the same function run against a fixture tree.
2. **Fixtures are mandatory, in both directions.** `--self-test` fails if `must_flag` or
   `must_pass` is empty, and fails if a `must_flag` fixture produces no finding or a
   `must_pass` fixture produces one. A lint that cannot demonstrate its own failure does
   not ship.

`doc` must point at a real `docs/solutions/` doc: a lint with no documented learning
behind it has no business being a merge gate, and the reverse pointer is what lets a
developer who trips the lint read the incident that caused it.

## Related

- `scripts/solutions-lint` — the runner and the pack. Its module docstring is the
  implementation-side copy of the registration seam above.
- `docs/solutions/UNMECHANIZED.txt` — the shrinking backlog of pre-contract docs.
- `docs/solutions/conventions/verify-ticket-premise-before-implementing.md` — the sibling
  "mechanize the discipline" tool (`scripts/premise-check`), and the precedent for a
  Python tool living in the bash-conventioned `scripts/` dir.
- `docs/ideation/2026-08-01-repo-improvements-ideation.md` — survivor 2, "Learning-to-Lint",
  where this originated.
- Tickets: BUI-605 (this contract + the CI job), BUI-606 (seed lints: non-unique-key
  mutation, fallback-swallow), BUI-607 (skill-lint family), BUI-608 (the orthogonal
  `status:`/`superseded_by:` axis), BUI-185 (the `lint:` ruff job this is modeled on).
