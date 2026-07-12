---
title: "Per-call-unique atomic-write tmp names: sweeping orphans must be age-gated, never eager"
date: 2026-07-12
category: docs/solutions/design-patterns
module: apps/ebay + packages/locg-cli
problem_type: design_pattern
component: service_object
related_components:
  - ebay_fetch.py
  - locg/_atomic.py
severity: medium
applies_when:
  - "An atomic write uses a per-call-unique tmp name (tempfile.mkstemp / <name>.<uuid4>.tmp) so concurrent writers to one path never share a tmp"
  - "You are tempted to sweep leftover orphan .tmp files a crash left behind"
  - "Independent OS processes (separate console-script entry points) write to a shared cache directory"
tags: [atomic-write, tmp-file, concurrency, thread-pool, race-condition, ttl, cleanup, ebay_fetch, mkstemp]
---

# Per-call-unique atomic-write tmp names: sweeping orphans must be age-gated, never eager

## Context

Both `apps/ebay/src/ebay_fetch.py` (`atomic_write_json()`) and, since BUI-339,
`packages/locg-cli/src/locg/_atomic.py` (`atomic_write()` / `atomic_write_json()`)
write to disk atomically: write to a temp file in the target's own directory,
then `os.replace()` it into place so a reader never observes a partial file.

The tmp filename went through a deliberate evolution that leaves a non-obvious
trap for anyone who later touches this code:

- Originally the tmp name was **deterministic** — `path.with_suffix('.tmp')`.
- **BUI-335** changed it to **per-call-unique** — `<name>.<uuid4>.tmp` — because
  two `ThreadPoolExecutor` workers under `sold_comps.run_batch()` could fetch
  duplicate cache keys and write the same `path` at the same time. Sharing one
  deterministic tmp name, they clobbered each other's in-flight tmp (a silent
  lost write); worse, after BUI-333 added a failure-cleanup `unlink`, one
  writer's cleanup could delete a *different* writer's still-in-flight tmp,
  turning the silent race into an active `FileNotFoundError`.

That fix is correct, but it has a two-part tail that BUI-338 surfaced and that
future work in this area will re-hit.

## Guidance

### The tail of per-call-unique tmp names

**1. They are never self-healing.** A deterministic tmp name was quietly
self-cleaning: the *next* write to the same path reused (overwrote) the single
orphan a mid-write crash left behind. Per-call-unique names are never reused, so
each crash orphans a `<name>.<uuid4>.tmp` that nothing ever removes — they
accumulate a few stray KB per crash, forever.

**2. The obvious cleanup reintroduces the race you just fixed.** The tempting
fix — "sweep the orphan `<name>.*.tmp` files" — is a trap:

```python
# WRONG: an eager sweep deletes a *live* concurrent writer's in-flight tmp,
# reintroducing exactly the BUI-335 race the unique names were added to fix.
for tmp in path.parent.glob(f"{path.name}.*.tmp"):
    tmp.unlink()
```

A "sweep once at process startup" is **also** not provably safe here: `apps/ebay`
exposes several console-script entry points (`ebay-fetch`, `ebay-sold-comps`,
`seller-scan`) that run as **independent OS processes** writing to a **shared**
cache path. There is no moment in any one process that is guaranteed to precede
"any concurrent writer anywhere," so no startup hook can know another process
isn't mid-write.

### The safe design: an age gate (TTL)

Sweep only tmp files whose mtime is **older than a TTL that comfortably exceeds
the longest realistic write** — a small JSON dump completes in well under a
second, so a 1-hour TTL is safe *by construction*: a genuinely in-flight tmp can
never be an hour old, no matter which process/thread wrote it or when the sweep
runs.

```python
# apps/ebay/src/ebay_fetch.py (BUI-338)
_ORPHAN_TMP_TTL_SECONDS = 3600  # >> the sub-second cost of a real write

def _sweep_orphan_tmp_files(path, *, ttl_seconds=_ORPHAN_TMP_TTL_SECONDS):
    try:
        candidates = list(path.parent.glob(f"{path.name}.*.tmp"))
    except OSError:
        return
    now = time.time()
    for candidate in candidates:
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            continue  # vanished between glob() and stat() — another writer's, not ours
        if age < ttl_seconds:
            continue  # too young to be confidently orphaned — could be live
        try:
            candidate.unlink()
        except OSError:
            pass  # another sweep/writer already removed it — best-effort only
```

Two supporting invariants make it fully safe:

- **Name-scoped glob.** `f"{path.name}.*.tmp"` only ever matches *this path's own*
  orphans, never a sibling cache file's in-flight tmp.
- **Never raises into the caller.** Every failure (missing dir, a file vanishing
  mid-sweep, transient FS error) is swallowed. Opportunistic cleanup must never
  become a new way for the write the caller actually asked for to fail. The sweep
  is invoked from inside `atomic_write_json()` before it creates its own tmp, so
  every write opportunistically cleans its own directory.

### Consolidate the idiom, but not across the package boundary

The same mkstemp+replace idiom was hand-rolled in **four** places in
`packages/locg-cli` (`cache.py`, `collection_cache.py`, `collection_io.py`,
`commands.py`). BUI-339 consolidated them onto one shared `locg/_atomic.py`,
preserving each site's exact semantics via keyword args (`compact` / `fsync` /
`mode` / `tmp_prefix`) rather than homogenizing behavior. Note the deliberate
non-consolidation: `apps/ebay`'s helper and `locg-cli`'s helper are conceptual
siblings but are **intentionally not unified** — `apps/*` are not uv-workspace
members and can't import `packages/*`, so they each get maintained independently.

## Why This Matters

This is a data-safety idiom on a hot path (every cache write). Getting the
cleanup wrong doesn't just leak a few KB — an eager or mis-placed sweep silently
deletes another live writer's in-flight tmp and reintroduces the concurrent-writer
data-loss/`FileNotFoundError` class that BUI-335 was created to eliminate. The
failure is invisible in normal single-process runs and only manifests under the
exact concurrency the unique names exist to protect, so tests that don't
reproduce two simultaneous writers to one path will pass while the bug ships.

The meta-lesson from the BUI-338/339 em-batch: for subtle concurrency/data-safety
work, the safety net is the **adversarial review of the actual risk surface** ("what
could be mid-write at the moment this sweep runs?"), not the base model tier. The
TTL invariant only reads as safe once you have explicitly asked that question.

## When to Apply

- **Any time you add cleanup for per-call-unique tmp files.** Age-gate it. Never
  eagerly delete every match, and never assume a startup sweep is safe without
  proving no other process can be mid-write.
- **When picking a TTL,** choose one that dwarfs the longest realistic write, not
  one that's merely "probably long enough." The whole point is safety *by
  construction*, not by likelihood.
- **When adding a new hand-rolled `mkstemp`+`os.replace` write** in `locg-cli`,
  call `locg/_atomic.py` instead — the fifth copy is how these drift apart.

## Examples

Deterministic (self-healing, but racy under concurrency) vs per-call-unique
(concurrency-safe, but accumulates orphans that need an age-gated sweep):

```python
# BEFORE (pre-BUI-335): deterministic — next write self-heals the lone orphan,
# but two concurrent writers to `path` share this name and clobber each other.
tmp = path.with_suffix(".tmp")

# AFTER (BUI-335): per-call-unique — concurrent writers never collide, but a
# crash-orphaned tmp is never reused, so cleanup must be handled explicitly
# (and, per BUI-338, that cleanup must be age-gated — see Guidance).
tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
```

## Related

- `docs/solutions/design-patterns/oauth-token-refresh-retry-pattern.md` — the
  other `ebay_fetch.py` design pattern (token cache + retry); same file, different
  concern.
- `docs/solutions/design-patterns/drain-started-cancel-tail-seen-set-loops.md` —
  the sibling concurrency/data-loss learning in `apps/ebay` (seller-scan): a
  parallelized side-effecting loop whose safety also turns on a concurrency
  invariant that ordinary tests don't reproduce.
