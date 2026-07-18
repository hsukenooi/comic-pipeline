---
title: Comics-server write isolation — gather-then-apply through write_transaction() under _write_lock
date: 2026-07-18
category: conventions
module: comics-server (gixen-cli server + gixen-overlay)
problem_type: convention
component: database
severity: high
applies_when:
  - Adding or changing any code path that writes to the comics server's SQLite DB
  - A writer needs slow network/subprocess work (eBay fetch, Gixen scrape) and a DB write in the same operation
  - Reviewing concurrency or transaction-boundary changes on the single-process FastAPI comics server
tags: [sqlite, concurrency, write-transaction, write-lock, gather-then-apply, toctou, comics-server, async]
---

# Comics-server write isolation — gather-then-apply through write_transaction() under _write_lock

## Context

The comics server (gixen-cli's FastAPI app + the gixen-overlay plugin) runs under a **single uvicorn worker** — one event loop, one thread. Historically it used ONE process-global `sqlite3.Connection` (`_db`) for everything: API handlers, the background Gixen sync loop, the sniper loop, the eBay-fallback pricer, and the overlay plugin all shared it. In `sqlite3`, `commit()` and `rollback()` are **connection-global**, so when one coroutine held uncommitted DML across an `await` and another coroutine committed/rolled back in that window, the first coroutine's half-finished writes were flushed early or discarded. This sat directly on the cancelled-sibling evidence path (phantom-WON / false-REMOVED), the most correctness-sensitive code in the repo.

BUI-400 (Stages 0-3 = BUI-407/408/409/410) replaced that with the model below. This doc is the durable convention any **new** writer must follow; it replaces the interim per-caller rollback net (see Related).

## Guidance

**Reads** stay on the long-lived `_db` connection (also used for migrations, the WAL-checkpoint teardown, and `app.state.db`). WAL readers never block a writer.

**Every write** follows this exact shape:

```python
# 1. GATHER — all slow network/subprocess work first, NO DB write held.
evidence = {}
for row in rows:
    evidence[row["id"]] = await asyncio.to_thread(fetch_from_ebay, row)  # awaits OK here
    await asyncio.sleep(pace)                                            # no write open

# 2. APPLY — one await-free block; the lock wraps ONLY this.
async with _write_locked():                          # the single app-wide write lock
    with write_transaction(_get_db_path()) as conn:  # fresh short-lived WAL connection
        for row in rows:                             # NO await anywhere inside
            apply_row(conn, row, evidence[row["id"]])
    # write_transaction commits once on clean exit, rolls back + closes on exception
```

Rules, all load-bearing:

1. **`_write_lock` is held ONLY around the await-free apply block — NEVER across a network `await`.** The whole point is to keep the fast local write off the slow path; a lock held across an eBay fetch would serialize all request latency behind background work (and, worse, a blocking `commit()` that hit BUSY would stall the entire single-threaded event loop).
2. **All slow work happens in a gather phase first**, keyed by row id; the apply phase only touches the DB.
3. **`write_transaction(_get_db_path())`** opens a fresh short-lived connection (WAL, `foreign_keys=ON`, `busy_timeout`), commits once on clean exit, rolls back + closes on exception. It owns the *only* commit — the underlying write helpers (`insert_bid`, `update_bid`, `set_auction_end_time`, `delete_bid`, `mark_bids_purged`, `set_local_snipe_result`, …) are commit-free and take a connection.
4. **Use `_get_db_path()`, not `server.db.DB_PATH` directly** — the module-level default is bound at import time and does not see the runtime `DB_PATH` env override, so a bare default can write to the wrong DB file.
5. **Both entry classes route through the same path**: the lockless background loops (`_sync_loop`/`_sync_gixen`, `_sniper_loop`, `_run_ebay_fallback`, `refresh_snipe_group`) AND the `_api_lock`-held handlers (`api_add_bid`/`api_edit_bid`/`api_purge`, `api_sync`). `_api_lock` stays the OUTER lock, `_write_lock` the leaf — never invert.
6. **The overlay writes through it too.** `write_transaction` is a stable `server.db` export; `_write_locked`/`_get_db_path` are imported from `server.main`. The `plugins/gixen-overlay/tests/test_workspace_imports.py` canary pins that surface.
7. **Sniper/purge writes are per-item, not batched into one transaction.** A partial failure that rolled back an already-fired bid's write would let the next tick re-fire it (duplicate bid). One `write_transaction()` per item.
8. **Read a just-written row back on the write connection, inside the transaction** — not on `_db`, which a concurrent open transaction can pin to a stale snapshot.

Regression net: a debug/test invariant guard fails if `commit()` is ever called while `not _write_lock.locked()`.

## Why This Matters

This gives transaction isolation (each writer's rollback only ever discards its own connection's work), preserves the deliberate non-blocking-background design (slow awaits sit outside any lock), and — because the short-held lock makes two write connections never overlap — keeps our own writers from ever hitting `SQLITE_BUSY`. It protects auction win/loss classification from silent corruption without slowing the dashboard or the time-critical snipe-fire path.

## When to Apply

- Any time you add or change a code path that writes to the comics server DB.
- Any time a single operation needs both slow network work and a DB write — split it gather-then-apply.
- When reviewing a concurrency or transaction-boundary change on the server.

## Examples

**The TOCTOU this model does NOT close (open follow-up BUI-417).** Serializing writers' *writes* under `_write_lock` does not close a read-then-write race:

```
writer A (fallback): gather → reads status = PENDING   (lock-free read, before the lock)
writer B (sync):     acquires _write_lock, commits a genuine WON, releases
writer A (fallback): acquires _write_lock, applies a stale eBay-price inference
                     whose terminal write guards on status CLASS (NOT IN tombstones),
                     not equality-vs-gather → silently overwrites the real WON
```

Serializing the applies cannot undo a decision already made from a **pre-lock read**. Closing it needs a status re-check *inside* the apply transaction — and that re-check must handle the vanished-null-end re-add variant (a re-added snipe keeps `status='PENDING'`, so status-equality alone is insufficient; also check `gixen_vanished_at` cleared / end now non-NULL). **Takeaway: a shared write lock isolates transactions, not decisions. Any write derived from a pre-lock read must re-validate the row's state under the lock.**

**Consolidating commit scope widens blast radius.** BUI-410 moved `_insert_web_added_bids` (previously an independent commit) inside `_sync_gixen`'s single transaction. That turned a pre-existing *local* failure — an unguarded `int(bid_offset)` on one malformed web-add scrape — into a **whole-cycle abort** that would roll back that cycle's WON/REMOVED transitions and `group_wins` evidence: a phantom-WON vector. Tests were green; only the adversarial review caught it. **Takeaway: when you pull previously-independent writes into one transaction, audit each newly co-transacted write's failure modes — a local exception now aborts everything sharing the transaction.**

## Related

- `docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md` — the interim per-caller rollback convention this model **supersedes** (now carries a SUPERSEDED banner).
- `docs/solutions/design-patterns/scope-status-writes-to-row-id-not-item-id.md` — sibling write-hygiene rule on the same `server/main.py` / `server/fallback.py` write paths (row-id vs item_id scoping); different failure class, same surface.
- `docs/plans/2026-07-18-001-design-shared-connection-isolation-plan.md` — the BUI-400 design (approach decision, staged rollout, test strategy).
- BUI-417 — open follow-up to close the read-then-write TOCTOU described above.
- BUI-418 — `get_all_bids` full-table scan now inside the write critical section (latency).
