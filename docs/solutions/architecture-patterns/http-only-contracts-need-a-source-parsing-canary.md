---
title: "A vocabulary shared over HTTP has no import edge and no CI gate — guard it with a source-parsing canary, and prove the canary can fail"
date: 2026-07-31
category: architecture-patterns
module: "apps/fmv (fmv_runner.py — sole producer of forced_flag_reason) + gixen-overlay (models.py — the validator at POST /api/comics) (BUI-588 / BUI-593)"
problem_type: architecture_pattern
component: service_object
severity: high
mechanized_by: test
enforced_by_test: plugins/gixen-overlay/tests/test_flag_reason_contract.py
related_components:
  - apps/fmv
  - gixen-overlay
applies_when:
  - "A vocabulary, enum, or set of string literals is shared across a boundary with NO import edge — the apps/* console-script boundary, an HTTP integration, a queue message type, a webhook payload"
  - "One side is the sole producer of values and the other validates them (a pydantic allow-list, a CHECK constraint, a match/case, a schema)"
  - "Adding a new literal on the producer side, where no compiler or import graph can catch a mismatch"
  - "Writing a guard test that asserts one package's behavior against another package's source"
  - "A validator rejects the whole request body rather than the single unrecognized field"
tags: [cross-package-contract, no-import-edge, http-boundary, enum-vocabulary, source-parsing-canary, non-vacuous-test, ci-gate, contract-drift, fail-loud-fail-total]
---

# A vocabulary shared over HTTP has no import edge and no CI gate

## Context

`comic-pipeline` deliberately keeps `apps/*` **out** of the uv workspace. `apps/fmv` and `apps/ebay` are `uv tool install`ed and reach everything else by shelling out or over HTTP. That is a real architectural benefit — they deploy independently and can't drag the workspace's dependency graph around.

It also means a whole class of contract has **no enforcement point at all**.

`comic-fmv` (`apps/fmv`) is the sole producer of `fmv_flag_reason` values. `plugins/gixen-overlay` validates them at `POST /api/comics`. Nothing links the two: no import, so no compile-time failure; no shared module, so no test naturally spans them; and CI's `workspace` and `apps-python` jobs each pass happily while the contract between them is broken.

BUI-588 added a new reason, `variant_dropped`, on the producer side:

```python
# apps/fmv/src/fmv_runner.py
forced_flag_reason="variant_dropped" if dropped_variant else None,
```

It also documented the new value in **four** places — `.claude/commands/comic/verify.md`, `comic/buy.md`, `comic/fmv.md`, and `docs/conventions/fmv-math-spec.md`. Every artifact a human would read was updated. The one artifact a *machine* reads was not:

```python
# plugins/gixen-overlay/src/gixen_overlay/models.py — unchanged by BUI-588
if v is not None and v not in ("one_sided", "too_wide", "too_sparse"):
    raise ValueError("fmv_flag_reason must be one_sided, too_wide, or too_sparse")
```

## Guidance

**1. When a boundary has no import edge, write a canary that parses the other side's source.**

This is the only enforcement mechanism available when there is nothing to import. Locate the peer by path, read it, extract the literals, and assert the contract:

```python
def _fmv_runner_source() -> str:
    """The producer's source, or skip if apps/fmv isn't checked out beside us."""
    repo_root = Path(__file__).resolve().parents[3]
    runner = repo_root / "apps" / "fmv" / "src" / "fmv_runner.py"
    if not runner.is_file():
        pytest.skip(f"apps/fmv not present at {runner}; cross-package canary skipped")
    return runner.read_text(encoding="utf-8")


def test_validator_accepts_every_reason_fmv_runner_can_emit():
    emitted = set(re.findall(
        r'forced_flag_reason\s*=\s*["\']([a-z_]+)["\']', _fmv_runner_source()
    ))
    assert emitted, "found no forced_flag_reason literals — did the producer move?"
    missing = sorted(emitted - set(FMV_FLAG_REASONS))
    assert not missing, f"fmv_runner emits {missing} but the validator rejects it."
```

Note the `assert emitted` line. Without it, a producer-side rename makes `emitted` empty, the subtraction yields the empty set, and the test **passes while checking nothing**. Guard the extraction itself, not just the comparison.

**2. Prove the canary can fail before you trust it.** A canary that has only ever been observed passing is indistinguishable from a canary that cannot fail. Verify it against the *pre-fix* state:

```
regex finds: {'variant_dropped'}   # non-vacuous: it fails against the old allow-list
```

This repo already had precedent for source-scanning guard tests (`test_skill_migration.py` asserts on `grade_photos.py`'s source text) — the technique is established here; what was missing was applying it to a *vocabulary* rather than to a behavior.

**3. Give the vocabulary one home.** Replace the inline tuple with a named module constant so the allow-list has a single definition the canary can import and the error message can derive from:

```python
FMV_FLAG_REASONS = ("one_sided", "too_wide", "too_sparse", "variant_dropped")
...
if v is not None and v not in FMV_FLAG_REASONS:
    raise ValueError("fmv_flag_reason must be one of: " + ", ".join(FMV_FLAG_REASONS))
```

**4. Updating the docs is not updating the contract.** BUI-588 updated four documentation files and still shipped a broken system. Treat a doc edit as evidence that a machine-readable definition exists somewhere and probably needs the same change.

**5. Check what a validator rejects — the field, or the whole request.** See below; this is what turns a small omission into data loss.

## Why This Matters

**The failure was total, not partial.** Pydantic rejects the *request body*, so a single unrecognized field value 422'd the entire `POST /api/comics`. The book's FMV — a real pool that had just cost provider quota across six query tiers — was written nowhere. Not "stored without the flag": **not stored at all**.

**It inverted the feature's purpose.** `variant_dropped` exists to route a book into the needs-manual channel so a human prices it with the bid cap withheld. Because the validator rejected the mark, the mechanism built to *surface* those books is precisely what *blocked* them from being recorded. A safety feature that fails closed against its own storage layer is worse than no feature.

**It was invisible for the entire lifetime of the previous batch.** BUI-588 merged, CI was green, and every variant-dropped book silently failed to persist until an unrelated ticket (BUI-585) tried to re-price one and surfaced the 422. The ticket that found it had a completely wrong theory of its own blockage — it was filed as "retry once the sold-comps providers recover," and the providers had already recovered.

**A loud error is not a caught error.** This threw a 422 every time — maximally loud at the HTTP layer, and still invisible, because the only consumer was a CLI whose operator reads a summary table, and because nothing aggregated the failures. Loudness only helps when something is listening.

## When to Apply

Reach for a source-parsing canary when **all** of these hold:

- Two components must agree on a set of literal values
- There is no import edge, so neither a type checker nor an import-time failure can catch drift
- The mismatch surfaces only at runtime, in production, on real data

If an import edge *does* exist, don't do this — share the constant and let the type system work. This technique is a fallback for boundaries that deliberately forbid coupling, and it carries real costs: it is regex-fragile, it breaks on producer-side refactors, and it needs the `pytest.skip` escape hatch for checkouts where the peer is absent.

**Update (BUI-673, 2026-08-04): the regex fragility above is avoidable — parse with `ast` instead.** A sibling canary on this same boundary reads the producer's tuple structurally (`ast.parse` → find the `Assign` → read `ast.Constant` elements), so a reformat, a line-wrap, or a trailing comma cannot silently turn the check into a no-op. Same `pytest.fail` guards on both the not-found and the wrong-shape paths. Prefer AST for anything that is a literal collection; regex remains necessary only for scattered call-site literals like `forced_flag_reason=` above, which have no single node to read.

**Also on this boundary, and not covered here: the same 422 can come from a field's TYPE rather than its value.** BUI-673 sent `observed_at` as an epoch float where the validator wanted `str | None` — right field, wrong type, whole batch discarded. See [a mock at the contract boundary cannot test the contract](../best-practices/a-mock-at-the-contract-boundary-cannot-test-the-contract.md) for why a green 30-test suite did not catch it.

Also worth separating from its neighbor: [cross-package regressions escaping per-package test runs](../developer-experience/cross-package-regressions-escape-per-package-test-runs.md) is about a test that **existed and wasn't run**. This is about a test that **could not have existed** until someone wrote the canary. The remedy for the first is running the full CI matrix; the remedy for this one is creating the enforcement point in the first place.

## Examples

**Before** — the vocabulary defined inline, in one place, silently authoritative:

```python
if v is not None and v not in ("one_sided", "too_wide", "too_sparse"):
    raise ValueError("fmv_flag_reason must be one_sided, too_wide, or too_sparse")
```

Producer adds a fourth value → 422 on every such request → whole upsert discarded → no CI signal anywhere.

**After** — named constant plus a canary that reads the producer:

```python
FMV_FLAG_REASONS = ("one_sided", "too_wide", "too_sparse", "variant_dropped")
```

Producer adds a fifth value → `test_validator_accepts_every_reason_fmv_runner_can_emit` fails in the `workspace` CI job → fixed in the same PR that introduced it.

**A note on where the gate actually was.** Before changing anything, check whether other layers enforce the same vocabulary. Here the live `fmv.flag_reason` column is plain `TEXT` with **no CHECK constraint**, so the pydantic validator was the *only* gate — which meant the fix needed no migration and no schema change. Had a CHECK existed, widening the validator alone would have moved the failure one layer down and produced an identical symptom from a different cause.
