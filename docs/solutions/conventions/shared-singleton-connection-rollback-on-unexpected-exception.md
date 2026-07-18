---
title: "Shared singleton sqlite connection: every _db-mutating coroutine must rollback on unexpected exception"
date: 2026-07-17
status: superseded
superseded_by: BUI-400 (staged rollout BUI-407..BUI-410), landed 2026-07-18
superseded_date: 2026-07-18
category: conventions
module: packages/gixen-cli/server (comics server sync + eBay fallback + API write paths)
problem_type: convention
component: database
severity: medium
applies_when:
  - "HISTORICAL — retained for context; do NOT re-apply this convention to new code (see the SUPERSEDED banner)"
related_components:
  - background_job
  - gixen-cli
tags:
  - sqlite
  - singleton-connection
  - rollback
  - async-concurrency
  - transaction
  - error-handling
  - gixen-cli
  - superseded
---

# Shared singleton sqlite connection: every _db-mutating coroutine must rollback on unexpected exception

> ## ⚠️ SUPERSEDED (2026-07-18, BUI-400 staged rollout BUI-407..BUI-410)
>
> **This convention is retired. Do NOT add per-caller `_db.rollback()` guards to
> new code, and do NOT cite this as a live rule in review.** It is kept for
> historical context (it explains why the pre-2026-07-18 code looked the way it
> did, and the data-integrity hazard the new model closes structurally).
>
> **What replaced it.** BUI-400's staged rollout removed the shared-transaction
> root cause the guards were mitigating. `_db` is now a **read-only + lifecycle**
> connection (migrations, WAL checkpoint, the one immediate-commit
> `api_remove_bid` `delete_bid()`); **every batched writer** — the API write
> paths and overlay `api_link_locg` (Stage 1, BUI-408), `_run_ebay_fallback`
> (Stage 2, BUI-409), and `_sync_gixen` + `_sync_loop`'s `refresh_snipe_group`
> (Stage 3, BUI-410) — now runs its DML on its **own short-lived
> `write_transaction()` connection**, opened under the app-wide `_write_lock`
> and held only across an **await-free** apply block (network work is hoisted
> ahead of it, gather-then-apply). `write_transaction()` (in `server/db.py`)
> **rolls back and closes its own connection on any exception inside its `with`
> block**, so a failed cycle discards *only its own* writes — it can no longer
> strand a partial batch on a shared connection for an unrelated coroutine's
> `commit()` to flush. The cross-coroutine "stray write flushed later" hazard is
> therefore **structurally impossible**, not merely mitigated.
>
> **Consequence for the old guards.** The per-caller `_db.rollback()` calls in
> `api_sync`, `api_purge`, `api_edit_bid`, `_ensure_fresh_sync`, `_sync_loop`,
> and `_run_ebay_fallback` were **removed** in BUI-410 (they had become dead
> no-ops — `_db` no longer carries uncommitted batched writes). Their `except`
> handlers stay (they still degrade / return a structured 500 / log), just
> without the rollback.
>
> **The one honesty rule below still applies** (it is not about rollback):
> keep `except` log wording accurate about **suppressed vs reraised**
> (BUI-391) — see that subsection.
>
> Everything below this banner is the **original (now-historical)** convention.

## Context

The comics server (`packages/gixen-cli/server/`) is a FastAPI app that owns **one process-wide singleton sqlite connection**, `_db`, shared by every coroutine that touches the database: the background sync cycle (`_sync_gixen`, driven by `_sync_loop` and `_ensure_fresh_sync`), the eBay-fallback task (`_run_ebay_fallback` in `server/fallback.py`), and the request-handling API paths (`api_sync`, `api_purge`, `api_modify_bid`, ...). These run concurrently under **disjoint** locks — `_sync_loop` runs lockless, `_run_ebay_fallback` holds `_ebay_fallback_lock`, the API paths hold `_api_lock` — so no single lock serializes all writes to the shared connection.

Each of these entry points **batches** its DML and commits once at the end of its cycle rather than committing per statement (`_sync_gixen` accumulates a cycle of classification/status writes and commits at the end; `_run_ebay_fallback` writes per-row across `await asyncio.sleep(...)` pauses and commits once at the end of the batch). Because the transaction is shared, a `commit()` or `rollback()` by any one coroutine acts on **all** uncommitted writes currently on the connection.

The hazard this creates: if an entry point raises an unexpected exception mid-cycle and returns **without** rolling back, its partial, uncommitted writes linger on the shared connection. The next coroutine to `commit()` — a completely unrelated sync cycle or API call — flushes those stray writes as a side effect. This surfaced repeatedly across the BUI-386 / BUI-388 / BUI-391 hardening arc.

## Guidance

**Every coroutine that mutates `_db` and batches its DML must call `db.rollback()` on its generic / unexpected-exception path**, discarding partial writes before it returns. Treat this as a load-bearing invariant of the shared-connection design, not per-caller politeness: it is what keeps the shared transaction clean for the next coroutine to commit.

Add the guard wherever a broad `except Exception` wraps a *batch-then-commit* body. Known/expected errors that raise **before** any DML runs (e.g. a `GixenError` from the network step at the top of the cycle) do not need a rollback — nothing was written yet — so the rollback belongs specifically on the **generic** exception path that can fire *after* writes have started. Mirror the exact discipline `api_sync` established in BUI-386.

A related error-path subtlety (BUI-391): keep the log wording honest about whether the exception is being **suppressed** vs **reraised**. A handler that reraises after logging must not log `"...(suppressed)"`, and the rollback+log must run exactly once (`_sync_loop` relies on the single-log contract).

## Why This Matters

The writes this protects are the **evidence-store and bid-status writes** — the same rows the WON/REMOVED classification and the `group_wins` ledger depend on. A stray, half-written cycle flushed later by an unrelated commit is a silent cross-cycle data-integrity hazard on exactly the data whose integrity the BUI-38x arc has been hardening.

In practice it has been benign so far — cycles are short and the next commit usually succeeds cleanly — but it is a latent correctness hazard **and a recurring review miss**: each new `_db`-mutating entry point silently inherits the partial-write exposure unless someone remembers to add the guard. The arc bears this out: BUI-386 added the guard to `api_sync`, BUI-391 to `_ensure_fresh_sync` / `_sync_loop`, and BUI-399 was filed for the still-missing guards in `_run_ebay_fallback`, `api_purge`, and `api_modify_bid`.

**The rollback guards are a mitigation, not a cure.** The root cause is the single shared connection + single shared transaction across concurrent coroutines. A structural fix — one unifying write lock around all `_db` mutations, or a short-lived per-cycle connection so each coroutine's transaction is isolated — would make the per-caller convention unnecessary. That is tracked separately (BUI-400) and is deliberately larger/riskier; until it lands, this convention is the safety net and must be applied to every new caller. Consider centralizing the rollback in a small helper so new callers inherit it rather than re-deriving it (a candidate raised in BUI-399).

## When to Apply

- Adding or reviewing any async entry point in `server/main.py` or `server/fallback.py` that writes to `_db`.
- Any body that batches DML and commits once at the end (as opposed to a single autocommitted statement).
- Writing a broad `except Exception` around a write path on the shared connection.

## Examples

Before — partial writes linger on the shared connection:

```python
try:
    for row in batch:
        update_bid_status(db, ..., only_id=row["id"])
        await asyncio.sleep(1.5)
    db.commit()
except Exception:
    log.exception("fallback cycle failed")
    # no rollback -> the partial, uncommitted writes stay on _db and are
    # flushed later by the next unrelated coroutine's commit()
```

After — discard partial writes before returning:

```python
try:
    for row in batch:
        update_bid_status(db, ..., only_id=row["id"])
        await asyncio.sleep(1.5)
    db.commit()
except Exception:
    db.rollback()   # clear partial writes so the next coroutine commits cleanly
    log.exception("fallback cycle failed")
```

Reraise/log honesty (BUI-391) — do not claim suppression on the reraise path:

```python
except GixenError as e:
    if reraise:
        log.warning("GixenError (reraised to caller): %s", e)
        raise
    log.warning("GixenError (suppressed): %s", e)
    return []
```

## Related

- **BUI-386** — established the rollback-on-unexpected-exception guard for `api_sync`.
- **BUI-391** — extended it to `_ensure_fresh_sync` / `_sync_loop` and fixed the misleading "suppressed" log.
- **BUI-399** — remaining gaps (`_run_ebay_fallback`, `api_purge`, `api_modify_bid`); candidate to centralize the discipline in a helper.
- **BUI-400** — the structural root cause: one process-wide singleton connection + shared transaction across coroutines. **Now resolved** by the staged rollout below, which **supersedes this convention** (see the banner at the top). Design: `docs/plans/2026-07-18-001-design-shared-connection-isolation-plan.md`.
- **BUI-407 (Stage 0)** — commit-free write helpers + the `write_transaction()` factory + `busy_timeout`; demoted `_db` to reads/lifecycle.
- **BUI-408 (Stage 1)** — routed the already-await-free writers (API paths, sniper, overlay `api_link_locg`) through `write_transaction()` under `_write_lock`.
- **BUI-409 (Stage 2)** — `_run_ebay_fallback` gather-then-apply.
- **BUI-410 (Stage 3)** — `_sync_gixen` gather-then-apply (folding in BUI-405's lock-free `refresh_snipe_group`); **removed the per-caller rollback guards** this doc prescribed, retiring the convention.
- `../architecture-patterns/durable-evidence-store-encode-unknowns-and-identity-precisely.md` — sibling piece of the same shared-connection hardening arc (sentinel encoding + `group_wins` identity); a reader fixing one write-safety class in these files will likely need the other.
- `../design-patterns/scope-status-writes-to-row-id-not-item-id.md` — companion write-hygiene discipline for the same `server/main.py` / `server/fallback.py` write paths (id-target terminal writes, don't write item_id-wide).
