---
title: "Order records by the timestamp each record carries, not by the name or mtime of its container"
date: 2026-08-04
category: logic-errors
module: "apps/fmv (scripts/backfill_comps_ledger.py — capture_segments, read_capture, main), apps/ebay (src/sold_comps.py — _compress_rotated_segment, _sweep_compressible_segments, _rotate_capture_if_needed)"
problem_type: logic_error
component: tooling
severity: medium
related_components:
  - "apps/ebay"
  - "comps-ledger"
applies_when:
  - "Ordering records read out of rotated, sharded, or chunked files whose names embed a timestamp"
  - "A first-wins or last-wins rule (KTD4 immutability, dedupe, upsert precedence) depends on read order"
  - "Reaching for mtime as a chronological key on files a later stage rewrites — compression, re-encoding, atomic os.replace"
  - "Widening a timestamp's resolution in a filename format that already has files on disk"
  - "Concatenating two corpora whose relative order is itself a deliberate precedence decision"
symptoms:
  - "Two rotations inside one second sort by their random hex suffix, so segment order is uncorrelated with retirement order"
  - "The same product_id observed twice at different prices is frozen at the LATER price under a first-observation-wins rule"
  - "A docstring asserts lexical filename order equals chronological order, and downstream code is built on that assertion"
root_cause: logic_error
resolution_type: code_fix
mechanized_by: test
enforced_by_test:
  - apps/fmv/tests/test_backfill_comps_ledger.py::test_same_second_rotations_order_by_record_timestamp_not_segment_name
  - apps/fmv/tests/test_backfill_comps_ledger.py::test_equal_capture_timestamps_keep_a_stable_deterministic_order
  - apps/fmv/tests/test_backfill_comps_ledger.py::test_capture_still_precedes_the_cache_when_its_timestamp_is_later
tags:
  - "record-ordering"
  - "first-observation-wins"
  - "rotated-segments"
  - "mtime-is-not-creation-time"
  - "stable-sort"
  - "sort-scope"
  - "comps-ledger"
  - "ktd4"
---

# Order records by the timestamp each record carries, not by the name or mtime of its container

## Problem

`capture_segments()` in `apps/fmv/scripts/backfill_comps_ledger.py` ordered the rotating
tier-0 capture segments by filename, and its docstring asserted that this order was
chronological — "their names carry a timestamp, so lexical order is chronological".
Segment names are minted as `raw_responses.<UTC-stamp-to-the-second>-<8 random hex>.jsonl`,
so within a shared second the sort compares two random tokens. That arbitrary order was
what KTD4's first-observation-wins tiebreak was built on (BUI-680, PR #453).

## Symptoms

This was found by reading, not by a failed run. Two rotations inside one second is
unreachable at the **default** `CAPTURE_ROTATE_BYTES = 10_000_000`
(`apps/ebay/src/sold_comps.py:99`) — but that default is operator-overridable via
`EBAY_SOLD_COMPS_CAPTURE_ROTATE_BYTES`, so "latent" is a property of the current
configuration, not of the code. It is routine at test scale: BUI-677's tests drive
rotation at `monkeypatch.setattr(sc, "CAPTURE_ROTATE_BYTES", 1)`.

What fires when it does fire is one level down from the backfill's own output.
`main` iterates `responses` in list order and POSTs one batch per response, and
`upsert_comps` (`plugins/gixen-overlay/src/gixen_overlay/db.py:1781`) freezes the
observation on first insert:

```sql
ON CONFLICT(provider, product_id, COALESCE(comic_id, -1), pool)
DO UPDATE SET
    last_seen_at   = excluded.last_seen_at,
    seen_count     = seen_count + 1,
    conflict_count = conflict_count + ?
```

`price`, `sold_date`, `observed_at`, and `provenance` are never rewritten. So the same
`product_id` appearing in two same-second segments at different prices is frozen at
whichever segment the random-token sort happened to put first — potentially the **later**
price, permanently, with `observed_at` stamped from the later observation too.

**The failure is not fully silent, and the trace is misleading rather than absent.**
`upsert_comps` logs `comps conflict: ... stored(price=...) incoming(price=...)` and bumps
`conflict_count`. What that trace does *not* reveal is that the surviving side is the
wrong one — it reports a disagreement, not a misordering. Worse, `conflict_count` is the
same diagnostic BUI-675 mined to discover the foreign-currency bug, so a mis-ordered
import degrades a working detector while looking like normal noise.

## What Didn't Work

The ticket proposed three fixes. All three were wrong or dominated, and the disproofs are
the transferable part of this learning.

**1. Sort segments by mtime — unsound, and wrong by a whole segment rather than by a second.**

`_compress_rotated_segment` (`apps/ebay/src/sold_comps.py:335`) does not compress in place.
It writes a brand-new file and renames it over the destination:

```python
with gzip.open(tmp_path, "wb") as f:
    f.write(raw)
...
os.replace(tmp_path, gz_path)
```

A `.gz` segment's mtime is therefore its *compression* time, not its last-append time.
That alone would be survivable if everything were compressed at rotation — but BUI-677
deliberately stopped doing that, so straggling appends survive rotation.
`_sweep_compressible_segments(just_rotated)` skips the segment this pass retired and
compresses only older quiescent ones. The newest retired segment sits plaintext with an
old mtime while an *older* segment gets a `.gz` whose mtime is minutes newer. An mtime
sort routinely inverts a whole segment — strictly worse than the bug it was meant to fix.

**2. Widen the rotation stamp to sub-second — actively wrong across the deploy boundary.**

The name is minted at `_rotate_capture_if_needed` (`apps/ebay/src/sold_comps.py:485-488`):

```python
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
token = uuid.uuid4().hex[:8]
rotated = CAPTURE_DIR / f"raw_responses.{ts}-{token}.jsonl"
```

Widening `%S` to `%S%f` gives `20260804T120000123456Z`. Compared lexically against an
already-on-disk `20260804T120000Z`, the two diverge at the character after `...120000`:
`'1'` (0x31) vs `'Z'` (0x5A). Since `'1' < 'Z'`, the **new, later** segment sorts
**before** the **old, earlier** one. Narrow — both formats must appear inside one second,
i.e. exactly at the deploy boundary — but a real regression, and it does nothing for the
segments already on disk.

**3. Sort each segment by its own first record — correct but strictly dominated.**

It opens and gunzips every segment an extra time to learn a timestamp `read_capture` is
about to parse anyway in the same pass, and after all that it still only orders
*containers*, which is coarser than the data already in hand.

## Solution

Sort the capture **records** by the timestamp each one already carries — stably, at the
end of `read_capture`. Every record is written with `time.time()` by
`_capture_raw_response` (`apps/ebay/src/sold_comps.py:557`) and surfaced as
`RawResponse.observed_at`.

The whole functional change is three lines
(`apps/fmv/scripts/backfill_comps_ledger.py:400-404`):

```python
# BUI-680: the ordering KTD4 actually relies on. Stable, so equal
# timestamps keep segment-then-line order. See the docstring for why this
# is done here and not by ordering the segments.
out.sort(key=lambda r: r.observed_at)
```

The docstring's false guarantee was replaced with an accurate one. Before:

```
Retired segments sort before the live one (their names carry a timestamp,
so lexical order is chronological), so the earliest observation of a comp
is the one ``upsert_comps`` keeps (KTD4).
```

After:

```
That order is DETERMINISTIC — a re-run reads the corpus the same way — but
it is only APPROXIMATELY chronological. ... Do not build a
first-observation rule on this order. KTD4's earliest-observation-wins is
enforced one level down, in ``read_capture``, which sorts the RECORDS by
the timestamp each one carries.
```

No `None` fallback is needed on the sort key, and that is load-bearing rather than
incidental: `observed_at` is parsed inside the per-line `try` alongside the other required
fields, so a missing key (`KeyError`) or a non-numeric value (`ValueError`/`TypeError`)
counts the line malformed and `continue`s. A `RawResponse` on the capture path therefore
always carries a real float.

**The sort is scoped strictly to the capture corpus.** `main`
(`backfill_comps_ledger.py:858-871`) concatenates the whole capture list before the whole
cache list *on purpose* — capture's real fetch timestamp is better provenance than
`read_cache`'s mtime fallback — and `upsert_comps` freezes whichever posts first. A sort
spanning both corpora would silently hand the surviving row to the weaker source whenever
the cache file's mtime happened to be earlier.

## Why This Works

The old code was ordering *containers* as a proxy for the thing it actually meant: the
order in which observations happened. Every candidate fix in the ticket kept that proxy
and tried to make it more faithful — a better clock (mtime), a finer stamp (sub-second),
a cheaper oracle (first record per segment) — and each inherited a fresh defect from the
proxy's own mechanics.

Ordering the records dissolves the class instead of patching an instance. The timestamp is
the datum KTD4 is about; it is exact at record granularity rather than segment
granularity; it is already parsed in the same pass, so the fix costs no I/O; it needs no
filename change and no migration of segments already on disk; and it is immune to any
future change in how segments are named, rotated, compressed, or swept. Stability
preserves the pre-existing tie behavior — equal timestamps keep segment-then-line order —
so a re-run stays byte-identical, which the backfill's idempotency test depends on.

One residual is stated honestly in the docstring rather than claimed away: the clock is
the writer's wall clock, so a backwards NTP step can still misorder two records. That is
the *same* clock the segment names were minted from, so it is no worse than what it
replaced, and the blast radius shrinks from a whole segment to a single fetch.

## Prevention

Three regression tests, all in `apps/fmv/tests/test_backfill_comps_ledger.py`:

- **`test_same_second_rotations_order_by_record_timestamp_not_segment_name`** — writes two
  segments named `...20260804T120000Z-ffffffff.jsonl` and `...20260804T120000Z-00000000.jsonl`
  so the name sort is the exact reverse of retirement order, asserts that reversal
  explicitly, *then* asserts the records come out oldest-first. The first assertion is what
  makes the test sharp: it pins that the containers are mis-ordered, so a passing record
  order can only come from the sort.
- **`test_equal_capture_timestamps_keep_a_stable_deterministic_order`** — pins stability,
  so the new sort cannot make a re-run reorder itself.
- **`test_capture_still_precedes_the_cache_when_its_timestamp_is_later`** — pins the scope
  constraint from the opposite side: a capture timestamp of `1_800_000_000.0` against a
  cache mtime of `1_700_000_000.0`, asserting capture still posts first. A sort widened to
  span both corpora fails this while the pre-existing `test_capture_is_imported_before_the_cache`
  still passes, so the pair distinguishes the fix from its overreach.

The generalizable rules:

- **mtime is last-WRITE time, so read every writer before ordering by it.** Any process
  that rewrites a file — compression, atomic-replace, a repair pass, a re-download —
  resets it. Here `os.replace(tmp_path, gz_path)` made an *older* segment's mtime *newer*
  than a younger one's. The contrast worth holding onto: mtime is sound as an **age gate**
  on files nothing rewrites (see the related `.tmp`-sweep doc below) and unsound as an
  **order key** on files a later stage rewrites.
- **Order records, not containers, when the records carry their own timestamps.** Container
  order is a proxy, only ever as good as the naming and rotation scheme, and coarser than
  data already parsed.
- **A second-resolution timestamp in a filename is not a total order.** A collision-avoidance
  token added to make names unique makes the *sort* arbitrary within its resolution — the
  opposite of what a chronological reading assumes. And mixing two stamp *formats* is worse
  than coarse: it makes lexical compare actively wrong across the boundary (`'1' < 'Z'`).
- **Scope a correctness sort to the corpus you are fixing.** A deliberate ordering elsewhere
  in the pipeline — here capture-before-cache, chosen on provenance quality rather than
  time — is exactly the kind of invariant a well-meaning global sort erases silently. Pin
  it with a test that fails on the widened version.

**Process note.** All three options the ticket listed were wrong or dominated. The
disproofs were established *before* an agent was spawned to implement, and handed over as
settled facts; the fourth option — the one that shipped — was then found by looking at what
data was already on hand. A ticket's proposed solutions are a hypothesis, not a spec. See
`docs/solutions/conventions/verify-ticket-premise-before-implementing.md`.

**One accepted behavior change is not mechanized.** `main` applies `responses[:args.limit]`
after `read_capture` returns sorted, so `--limit` now takes the oldest N rather than the
first N in segment order. No test covers it and `--help` does not say which N.

## Related Issues

- `docs/solutions/best-practices/a-mock-at-the-contract-boundary-cannot-test-the-contract.md`
  — the same `observed_at` field, the wire-type half (BUI-658/673); this doc is the
  ordering half.
- `docs/solutions/architecture-patterns/vacuous-partition-guards-and-mutable-identity-keys.md`
  (line 213) — the same class in the collection store: `local_added_seq` is a
  *within-import* counter, not a global ordering, and sorting by it selects the stale row
  in 35 of 60 groups. A plausible-looking key that is not a total order. (That doc carries
  `status: corrected` for an unrelated claim.)
- `docs/solutions/design-patterns/atomic-write-unique-tmp-sweep-must-be-age-gated.md` — the
  useful contrast: mtime as an age gate on files nothing rewrites is sound.
- `docs/plans/2026-08-03-002-feat-comps-data-flywheel-plan.md` (lines 83-90) — the KTD4
  immutability invariant this sort protects.
- Tickets: BUI-680 (this fix, PR #453), BUI-677 (deferred segment compression — the change
  that made mtime ordering unsound), BUI-628 (the tier-0 capture corpus), BUI-675 (the
  `conflict_count` diagnostic this bug would degrade).
