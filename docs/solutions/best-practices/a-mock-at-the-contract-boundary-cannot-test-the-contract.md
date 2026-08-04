---
title: "A mock at the contract boundary cannot test the contract — and a fixture wrong toward the desired answer manufactures confidence"
date: 2026-08-04
category: best-practices
module: "apps/fmv (fmv_runner.py — producer of POST /api/comics/comps) + gixen-overlay (models.py CompItem — the validator) (BUI-658 / BUI-673)"
problem_type: best_practice
component: testing_framework
severity: high
mechanized_by: test
enforced_by_test: plugins/gixen-overlay/tests/test_comps_contract.py
related_components:
  - "apps/fmv"
  - "gixen-overlay"
applies_when:
  - "Writing tests for a payload that crosses a boundary with no import edge — an HTTP call, a queue message, a subprocess argv"
  - "The natural seam to mock is the transport function itself (requests.post, a _post_json helper, a client wrapper)"
  - "A test fixture claims in its name or docstring to reproduce a shape another module actually produces"
  - "Reviewing a diff against a schema — it is not enough to check that the field NAMES line up"
  - "A validator rejects the whole request body rather than the single offending field"
tags:
  - "mock-fidelity"
  - "cross-package-contract"
  - "no-import-edge"
  - "test-fixtures"
  - "vacuous-test"
  - "pydantic"
  - "fails-green"
  - "contract-drift"
---

# A mock at the contract boundary cannot test the contract

## Context

`comic-fmv` (`apps/fmv`) posts comps to the overlay's `POST /api/comics/comps`. BUI-658
shipped that producer with **30 new tests, all green**, plus a reviewed diff and a green
CI run. Every post it would ever make was going to 422.

`observed_at` is stamped by BUI-657 as a raw epoch **float** — `time.time()` on a live
fetch, `st_mtime` on a cache hit. The validator declares it `str | None`, and pydantic v2
does not coerce float to str. Because the validator rejects the *request body*, not the
offending field, all 514 batches would have been discarded whole.

`apps/fmv` is deliberately outside the uv workspace, so there is no import edge and
nothing at build time could notice — the same structural gap documented in
[HTTP-only contracts need a source-parsing canary](../architecture-patterns/http-only-contracts-need-a-source-parsing-canary.md).
That doc covers a producer emitting an unrecognized **value**. This one covers a producer
emitting the right field with the wrong **type**, and — the part worth reading — why a
full test suite sailed straight past it.

## Guidance

**1. A mock at the contract boundary cannot test the contract.**

The BUI-658 suite patched `_post_json`, the function that serializes and sends. That is
the obvious seam — it avoids the network and makes tests fast. It also means the payload
never reached the model that would reject it. The tests proved the producer builds *some*
dict and hands it to *something*; the only claim that mattered — that the receiver
accepts it — was the one claim mocked out of existence.

Mock the **transport**, not the **validation**. If the seam you are patching sits on the
far side of the schema, you have mocked the thing under test.

**2. A fixture wrong in the direction of the desired answer is worse than no fixture.**

```python
def _make_comp(price, grade, ..., observed_at="2026-08-01T00:00:00+00:00", ...):
    """A comp in the exact shape `parse_comp`/`parse_comp_sold_comps` +
    BUI-657's provenance stamping produce — everything `_comp_to_ledger_item` reads."""
```

The docstring asserts fidelity to production. The default value is an ISO **string**.
Production emits a **float**. The fixture encoded what the author *wanted* the producer
to emit, and every test built on it inherited that wish.

This is worse than having no fixture at all, because it manufactures confidence. An
absent test is a known gap; a green test over a wrong fixture is a false negative that
actively argues against looking further.

When a fixture claims to reproduce another module's output, derive the value from that
module or pin it with a comment naming the real source:

```python
# BUI-673: the real BUI-657 stamp. sold_comps returns response_fetched_at as a raw
# epoch FLOAT (time.time() live, st_mtime on a cache hit). This fixture originally
# defaulted to an ISO string — what the wire contract wants, NOT what the producer
# emits — so every test passed while every real post 422'd. Keep this a float.
_STAMPED_OBSERVED_AT = 1785587400.0  # 2026-08-01T12:30:00+00:00
```

**3. Assert the wire TYPE, not `expected == actual` between two mocks.**

```python
assert item["observed_at"] == comp["observed_at"]   # passes for ANY type
```

Both sides came from the same fixture, so this asserts the projection copied a value —
never that the value is the type the receiver requires. Assert the property the contract
depends on:

```python
assert isinstance(item["observed_at"], str)
assert item["observed_at"] == "2026-08-01T12:30:00+00:00"
```

**4. Verifying a contract means types, not just names.**

The pre-merge review of BUI-658 *did* check the payload against the model: it confirmed
`pool` and `provenance` matched the closed vocabularies, that the projection tuple held
exactly the model's declared fields, and — a genuine catch — that the producer posted
`comps` rather than `pool_comps`, which would have 422'd on first-party rows that carry
no `provider`. It read every field **name** and no field **type**. The diff was declared
clean an hour before it turned out to be totally broken.

A field-name check is the easy half and feels like diligence. The types are where a
no-import-edge boundary actually drifts.

**5. Parse the peer's source with AST, not regex.**

The predecessor canary uses `re.findall` and its own doc flags regex fragility as a known
cost. A structural read removes it — a reformat, a line-wrap, or a trailing comma cannot
silently turn the canary into a no-op:

```python
for node in ast.parse(_fmv_runner_source()).body:
    if not isinstance(node, ast.Assign):
        continue
    if "_COMP_LEDGER_FIELDS" not in [t.id for t in node.targets if isinstance(t, ast.Name)]:
        continue
    if not isinstance(node.value, ast.Tuple):
        pytest.fail("_COMP_LEDGER_FIELDS is no longer a tuple literal")
    return {e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
pytest.fail("_COMP_LEDGER_FIELDS not found in fmv_runner — did it move?")
```

Both `pytest.fail` calls matter. A canary that returns an empty set when the producer
moves passes while checking nothing.

**6. Pin the constraint on the validator's side, so the next fix lands on the producer.**

```python
def test_observed_at_rejects_a_raw_epoch_stamp():
    """If someone 'fixes' a recurrence by widening this to `float | str | None`, the
    422 goes away and the column holds two encodings that neither sort nor parse as a
    unit. Fix the producer, not this model."""
    with pytest.raises(ValidationError):
        CompItem(pool="raw", provider="serpapi", product_id="x",
                 provenance="live", observed_at=time.time())
```

Widening the model is the tempting fix — it turns the error off. It also puts epoch
floats and ISO strings in one `observed_at` column that sits under `idx_comps_observed`
beside ISO `first_seen_at`/`last_seen_at`, and which a separate backfill already writes
as ISO. A test that pins the *rejection* makes the wrong fix fail loudly.

**7. Prove the new tests can fail.** Remove the fix, confirm the count of failures, restore
it. Here: 8 failures. A test that has only been observed passing is indistinguishable from
one that cannot fail — the same non-vacuity discipline the predecessor doc argues for.

## Why This Matters

**Green CI, a reviewed diff, and 30 targeted tests all agreed on a wrong answer.** None
of the three was negligent. The tests covered the failure paths thoroughly — 422, 500,
connection refused, empty lists, `comic_id` of None — they just never sent a real payload
to a real model. Coverage of the *error handling* is not coverage of the *contract*.

**It would have been loud, and still wrong.** BUI-658's deliberate design prints a
comps-write-failure count whenever a post fails, so this would have surfaced on the first
real run rather than silently writing nothing. That is the design working. But loudness
after deploy is a worse outcome than a red test before merge, and the ledger would have
been empty for however long it took someone to read the summary line.

**The class is recurrent in this repo.** BUI-588 was the same boundary and the same
blast radius (a 422 discarding a whole upsert), found only when an unrelated ticket
tripped over it — and that ticket had a completely wrong theory of its own blockage.
Two incidents on one contract in five weeks is a structural signal, not bad luck.

## When to Apply

- Any payload crossing a boundary the type checker cannot see — HTTP, queue, subprocess argv
- Any fixture whose name or docstring claims to mirror another module's output
- Any review of a diff against a schema: check the types, not only the field names
- Reach for a source-parsing canary when there is genuinely nothing to import. If an
  import edge exists, share the constant and let the type system do this work instead.

## Examples

**Before** — the projection, and the tests that could not see it:

```python
item = {field: comp.get(field) for field in _COMP_LEDGER_FIELDS}
# observed_at rides through as a float; CompItem.observed_at is `str | None`
```

**After** — normalize at the wire boundary, ISO-8601 UTC rather than `str(float)` so the
column sorts chronologically under its own index:

```python
item["observed_at"] = _observed_at_iso(item["observed_at"])
```

**And the canary that makes the next drift fail in CI** —
`plugins/gixen-overlay/tests/test_comps_contract.py` asserts the producer projects only
declared fields, covers every required one, and cannot send a raw epoch.

## Related

- [HTTP-only contracts need a source-parsing canary](../architecture-patterns/http-only-contracts-need-a-source-parsing-canary.md) — the same boundary, the vocabulary-drift half; this doc is the type-drift half and supplies the AST improvement to that doc's regex caveat
- [Cross-package regressions escape per-package test runs](../developer-experience/cross-package-regressions-escape-per-package-test-runs.md) — a test that existed and was not run, versus this one, which ran and proved nothing
- [A probe of a write endpoint is a write](a-probe-of-a-write-endpoint-is-a-write.md) — the deploy-verification counterpart; BUI-673's live check used the refused-shape probe it prescribes
