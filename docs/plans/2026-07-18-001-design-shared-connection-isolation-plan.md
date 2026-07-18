---
title: "design: Comics-server shared-singleton SQLite connection isolation (BUI-400)"
date: 2026-07-18
type: design
status: draft
linear: BUI-400
depth: deep
folds_in: BUI-405
supersedes_convention: docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md
---

# Comics-server shared-singleton SQLite connection isolation

> **Status: DRAFT for review.** This is step 1 (the design) of BUI-400's staged
> plan. BUI-400's description already made the topology findings and picked
> Approach B (per-cycle connection) as the preferred end-state; this doc *decides
> the open questions that step 1 named* — connection lifecycle, read-vs-write
> strategy, `busy_timeout`/retry policy, and the overlay-coupling story — and
> sequences the rollout into reviewable stages. It does not change code. The
> BUI-386/391/399 rollback convention stays the safety net until this lands.

---

## 1. The exposure (settled)

The server holds ONE module-global `sqlite3.Connection` (`main._db`, opened once
at `db.py:606-620`: WAL, `foreign_keys=ON`, `row_factory=Row`, default
`isolation_level`), handed to everything via `_get_db()` and `app.state.db` —
including the overlay plugin. Python's `sqlite3` auto-opens a transaction on the
first DML and requires an explicit `commit()`; both `commit()` and `rollback()`
are **connection-global**. So when coroutine A runs some DML, hits an `await`, and
yields, coroutine B's `commit()` flushes A's half-finished writes early, or B's
`rollback()` discards them.

**The reframe that makes this cheap:** the server runs under uvicorn with a single
worker (`server/install.sh:60`, no `--workers`). This is not an OS-thread race —
it is one event loop, and the *only* thing that causes interleaving is an `await`
sitting **between a DML statement and its commit/rollback**. All network/subprocess
work is already offloaded via `asyncio.to_thread` and touches no DB.

## 2. Verified topology

**Lock-free writers on the shared connection:** `_sync_loop → _sync_gixen`
(lockless *by design* — separate `_sync_client` so its slow scrape doesn't contend
on `_api_lock`, `main.py:141`), `_sniper_loop → set_local_snipe_result`, and the
overlay's `api_link_locg` (`routes.py:465`, writes + `commit()`s the shared `_db`
through `app.state.db` with **no** lock).

**Writers that hold uncommitted DML across an `await` (the bleed windows):**

| Writer | Lock | Bleed |
|---|---|---|
| `_sync_gixen` | none (via `_sync_loop`) / `_api_lock` (via `_ensure_fresh_sync`,`api_sync`) | DML in the transition loop, then `await` at `main.py:600` (`_record_listed_win_evidence`) and `:653` (`_resolve_vanished_null_end_bids`), commit at `:655` |
| `_run_ebay_fallback` | `_ebay_fallback_lock` only | per-row `UPDATE` interleaved with `await to_thread(fetch)` + `sleep(1.5)` across the whole cycle, one commit at `fallback.py:605` |

**Two premise-breaking findings (from the BUI-400 investigation, re-verified):**

1. **Scope crosses the package boundary.** The overlay's `api_link_locg`
   (`routes.py:465`) mutates the same `_db` with no lock. Any correct fix must
   route overlay writes through the same mechanism — deepening the documented
   overlay → gixen-cli coupling.
2. **The "single shared transaction" is already leaky.** `insert_bid` (`db.py:643`)
   and `set_auction_end_time` (`db.py:1025`) **self-commit**, and both are called
   mid-`_sync_gixen`. So "one batched commit at 655" is a fiction — the transaction
   is already fragmented. Re-establishing a coherent transaction boundary is a
   prerequisite, not an afterthought.

## 3. Approach decision: B (per-cycle write connection) + a short-held write lock

BUI-400 already ruled Approach A (one unifying write lock around the whole
batch-then-commit region) out as the end-state: to actually close the bleed the
lock must be held across `_run_ebay_fallback`'s ~150s of eBay calls and
`_sync_gixen`'s scrape, which **serializes all request latency behind background
work** — reversing the deliberate lockless-background design. Avoiding that means
committing before each await, which is transaction restructuring anyway.

This doc commits to **Approach B**, sharpened with one addition that removes B's
main cost. The synthesis:

- **Reads** keep using the long-lived `_db` (demoted to reads + migrations +
  WAL-checkpoint teardown + `app.state.db`). WAL readers never block a writer.
- **Writes** go through a `write_transaction()` context manager that opens a
  fresh short-lived connection (WAL, `foreign_keys=ON`, `busy_timeout`), runs the
  DML, commits, and closes — **held open only for the fast local write, never
  across a network `await`.** The slow network work happens *before* the block is
  entered (gather-then-apply).
- A single async `_write_lock` is held **only around the `write_transaction()`
  block**, not across the network awaits. Because it guarantees one writer app-wide
  at any instant, two write connections never overlap → **`SQLITE_BUSY` between our
  own writers cannot occur**, so `busy_timeout` becomes a safety margin (against
  the WAL checkpoint / an external `sqlite` process), not a required per-write
  retry loop. That directly answers BUI-400's worry that B introduces a new
  BUSY failure mode with large blast radius: with the short-held lock, it doesn't.

**Why the lock still, if B isolates transactions?** Two ephemeral write connections
committing concurrently on a single event loop would still race on the WAL writer
and, worse, a blocking `commit()` that hit BUSY would stall the *entire* event loop
during the busy-timeout spin. The short-held lock makes writes non-overlapping and
keeps them off the slow path — you get isolation (B) + non-blocking background
(the original design) + no BUSY, which neither A nor plain B delivers alone.

### Open questions from BUI-400 step 1 — decided

- **Connection lifecycle:** long-lived read connection (`_db`); ephemeral
  per-transaction write connection via `write_transaction()`. Not per-request, not
  per-cycle — per *write transaction unit*, so a background loop that writes N times
  opens N short connections rather than holding one across its slow cycle.
- **Read-vs-write strategy:** `_db` is read-only by convention outside lifecycle
  boundaries (migrations, WAL checkpoint). All DML goes through `write_transaction()`.
- **`busy_timeout`/retry:** `PRAGMA busy_timeout=5000` on every connection. No
  per-write retry loop in v1 (the write lock makes our own BUSY unreachable);
  revisit only if BUSY shows up in logs.
- **Overlay coupling:** expose `write_transaction()` as a public surface from
  gixen-cli (`server.db`), import it in `routes.py:api_link_locg`, and extend
  `plugins/gixen-overlay/tests/test_workspace_imports.py` to cover it so the
  coupling is explicit and canary-guarded (same pattern as `_ensure_fresh_sync` /
  `_spawn_fallback_task`).

## 4. Prerequisite: a coherent transaction boundary

Finding 2 must be fixed first. Make the write helpers **commit-free** — `insert_bid`,
`set_auction_end_time`, and any other helper that currently self-`commit()`s take
a connection and do *not* commit; the `write_transaction()` block owns exactly one
commit. This is pure refactor (single-writer today, so no behavior change), lands
behind existing tests, and is the foundation every later stage stands on.

## 5. Staged rollout (each stage = one PR = one sub-ticket)

Sequenced so the highest-stakes code (the BUI-371/381 evidence path) is last and
alone. Each stage lands behind the `test_workspace_imports.py` canary and the
per-package suites.

- **Stage 0 — foundation (no behavior change).** Commit-free write helpers (§4);
  add `write_transaction()` factory + `busy_timeout`; demote `_db` to reads +
  lifecycle. Single writer still, so semantics are unchanged; this is the safety
  floor.
- **Stage 1 — route the already-await-free writers.** `api_add_bid` / `api_edit_bid`
  / `api_purge`, `_sniper_loop`'s `set_local_snipe_result`, and the overlay's
  `api_link_locg` go through `write_transaction()` under `_write_lock`. Immediate
  partial win, low risk (these transactions are already await-free).
- **Stage 2 — `_run_ebay_fallback` gather-then-apply.** Do all eBay fetches first
  (awaits, no DB), collect per-row prices, then one `write_transaction()` block.
  Removes the cross-await transaction; the `_ebay_fallback_lock` keeps its
  fallback-vs-fallback job.
- **Stage 3 — `_sync_gixen` gather-then-apply + fold BUI-405.** Hoist
  `_record_listed_win_evidence` / `_resolve_vanished_null_end_bids` eBay lookups
  ahead of the DML loop; apply writes in one `write_transaction()`. Fold in
  BUI-405 (`_sync_loop`'s lock-free `refresh_snipe_group` PENDING writes). Full
  lifecycle validation. **This is the scary one — ships alone.**

## 6. Test strategy (the genuinely hard part)

Async interleaving needs deterministic control, not luck:

- **Freeze-mid-cycle harness:** monkeypatch the eBay fetch / `to_thread` to a
  coroutine gated on an `asyncio.Event` the test owns, pause a batcher mid-cycle,
  run `api_purge` concurrently, and assert the batcher's rows are neither
  prematurely committed nor rolled back by the purge.
- **Rollback-spy** (reuse the BUI-391 technique) to assert a caller's rollback
  touches only its own writes.
- **Invariant guard (debug/test build):** fail if `commit()` is ever called while
  `not _write_lock.locked()`. This is the regression net that keeps the model true
  as the code evolves.
- **Pure lock-free regression:** lockless `_sync_loop` write interleaved with the
  lockless overlay `api_link_locg` commit — the zero-lock case — must show
  isolation after Stage 1.
- **Full-lifecycle (Stage 3):** add → sync → fallback classification → edit →
  purge → win/loss transitions, plus the phantom-WON (BUI-146/371) and
  false-REMOVED (BUI-384) regression suites, run against the `_sync_gixen` rewrite.

## 7. Risks & rollout

- **Highest-stakes surface** is the `_sync_gixen` rewrite (Stage 3) — it sits on
  the cancelled-sibling evidence path. Isolate it; make the regression run the
  review's focus.
- **Sniper timing:** `set_local_snipe_result` moves behind `_write_lock`. The
  write is tiny and await-free (sub-ms), but confirm the time-critical fire path
  doesn't stall.
- **Deploy:** server change → `uv sync --all-packages` + `launchctl kickstart`.
  ⚠️ `install.sh` labels the launchd job **`com.comics.server`**, but the
  CLAUDE.md / BUI-399 / BUI-402 deploy notes say `com.gixen.server`. Verify the
  live label before kickstart — that drift will bite the deploy (worth its own
  cleanup).
- **Convention supersession:** once Stage 3 lands, the per-caller
  rollback-on-unexpected-exception convention becomes unnecessary (each write's
  rollback only ever discards its own connection's work). Update
  `docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md`
  then, not before.

## 8. Sub-tickets

Children of BUI-400 (created 2026-07-18):

- Stage 0 — foundation (commit-free helpers + `write_transaction()` + `busy_timeout`)
- Stage 1 — route await-free writers (API + sniper + overlay `api_link_locg`)
- Stage 2 — `_run_ebay_fallback` gather-then-apply
- Stage 3 — `_sync_gixen` gather-then-apply + fold BUI-405 + full-lifecycle validation
