---
title: "A durable evidence store must encode unknowns and identity precisely, WON-permissively"
date: 2026-07-17
category: architecture-patterns
module: "gixen-cli (server/db.py group_wins ledger + refresh_snipe_group; gixen_client._parse_snipe_table)"
problem_type: architecture_pattern
component: database
severity: high
related_components:
  - "background_job"
applies_when:
  - "A permissive classifier (WON-permissive on ambiguity) is backed by a durable evidence store — a value it mirrors, or an append-only ledger — rather than only live in-memory rows"
  - "A scraped/parsed value is written back into that store by a bidirectional mirror, so a parse miss durably overwrites real state"
  - "A UNIQUE constraint or index is being chosen or re-keyed on an evidence table — the key silently defines what counts as a duplicate"
  - "Re-keying an existing SQLite index via a migration that must be idempotent and safe on an already-populated DB"
symptoms:
  - "Real group membership durably cleared to a positive 'no group' claim (N to 0) by a scraper regex miss"
  - "A genuine re-listed re-win at a distinct auction end silently collapsed into the first win by a too-narrow UNIQUE key"
  - "A CREATE UNIQUE INDEX IF NOT EXISTS under a changed column list silently no-ops (SQLite idempotency is by index name)"
root_cause: logic_error
resolution_type: code_fix
tags:
  - "evidence-layer"
  - "won-permissive"
  - "sentinel-values"
  - "unique-key-semantics"
  - "sqlite-migration"
  - "idempotent-by-name"
  - "bid-groups"
  - "phantom-won"
---

# A durable evidence store must encode unknowns and identity precisely, WON-permissively

## Context

The evidence-layer doc (`evidence-layer-disambiguation-vs-heuristic-gating.md`) established the BUI-371 pattern: classify phantom-WON false positives on *positive upstream evidence* (vanish-time, group-win) and subtract those rows from the permissive eBay price-inference heuristic, rather than gating the heuristic. That fix depended on the evidence living somewhere durable and correct. The BUI-381→BUI-385 arc hardened exactly that substrate — a bidirectional `snipe_group` mirror onto DB rows, and a new append-only `group_wins` ledger that survives the winner row being purged — and each step surfaced the same lesson from a different angle: **once a classifier's evidence is durable, the store's own representation choices become correctness. A value's encoding and a table's key are not bookkeeping; they decide whether real evidence is preserved, and they must fail in the WON-permissive direction (weaken evidence, never fabricate a false REMOVED).**

Three distinct traps, one theme. (The temporal sibling — bounding group-win evidence by membership start, not row lifetime, via `group_changed_at` — lives in the evidence-layer doc's `_group_won_before` section; tracked separately under BUI-393 and not repeated here.)

## Guidance

**1. "Unknown" and "known-to-be-empty" are different values — never share an encoding when a downstream writer trusts the value.** `gixen_client._parse_snipe_table` encoded a `snipe_group` regex miss as the string `"0"`. But `0` is a *positive claim* — "this snipe is genuinely ungrouped" — and BUI-381's `refresh_snipe_group` mirrors the listed value onto PENDING rows in **both** directions on every sync. So a parse miss collapsed to `"0"` didn't just fail to learn the group; it durably **cleared** a real membership (`N → 0`), silently deleting the group-cancel evidence for that row. The fix is to give "unknown" its own encoding (`None`) distinct from the `0` claim, and make every consumer skip unknowns rather than act on them. A sentinel that collides with a real, actionable value is a latent data-loss bug the moment any writer trusts it.

**2. A UNIQUE key *is* the store's dedup policy — choose it to match what "the same fact" means.** The `group_wins` ledger keyed `UNIQUE(snipe_group, item_id)` with `INSERT OR IGNORE`. That silently collapsed a genuine re-listed re-win — the *same* eBay item, *same* recycled group number, winning again at a *different* auction end — into the first win, losing the later genuine end as evidence. Re-keying to `(snipe_group, item_id, won_end_at)` records the second win as a distinct fact. This is only safe because of two invariants that must hold before you widen a dedup key: every stored `won_end_at` is a **genuine auction end** (never an observation-time proxy — the recycled-group hazard from the evidence-layer doc), and the consuming query is a **boolean-over-ends** (`_group_won_before` asks "did any qualifying win exist," dup-insensitive), so an extra distinct-end row is more true evidence, never a double-count that could tip a false REMOVED. Widening a UNIQUE key is a semantic change to what the table considers duplicate — reason about it as such, not as a schema tweak.

**Companion rule — re-keying a SQLite index is idempotent by NAME, not by column list.** `CREATE UNIQUE INDEX IF NOT EXISTS <same_name> ON t(new, cols)` **silently no-ops** if an index of that name already exists, even under a different column list — so a migration that keeps the old name never actually re-keys an existing DB. The reliable idempotent re-key is an explicit `DROP INDEX IF EXISTS <old_name>` followed by `CREATE UNIQUE INDEX IF NOT EXISTS <new_name>` — and give the new index a **new name** so intent is unambiguous. Prove the new index can never fail to build on existing data before shipping: here the old 2-col uniqueness trivially implies 3-col uniqueness, so the `CREATE` is guaranteed. Single-source the index DDL (one constant used by both the fresh-DB path and the migration) so a fresh install and a migrated install converge on one definition.

**Cross-cutting — every provenance/source value gets a closed vocabulary enforced at the write boundary.** When BUI-385 added a `source` tag so the ledger is auditable ("which win classified this row REMOVED"), it made the tag a `frozenset`-checked closed vocabulary, raised at the `record_group_win` write boundary on an out-of-vocab value, and backfilled pre-column rows to an explicit `legacy` tag so no `NULL` source persists. A forensics field is only trustworthy if a typo'd or missing tag cannot silently land in the permanent store.

## Why This Matters

- **Silent evidence loss in a money path.** Every trap here weakens the group-cancel evidence that keeps a cancelled sibling from being phantom-WON — and does so with a plain successful write, no error. Wrong data feeding record-win, history, and calibration.
- **Durability raises the stakes of representation.** A wrong value in a live in-memory row is corrected on the next sync. A wrong value mirrored into the DB, or a genuine fact dropped by a too-narrow UNIQUE key, persists until something notices — which, for absence-of-evidence, nothing does.
- **The safe direction is asymmetric.** Each fix is deliberately WON-permissive: an unknown encoding suppresses nothing it isn't sure of; a widened key only ever *adds* genuine evidence; a proxy end is never stored. The costly failure is a false REMOVED (phantom classification), so every ambiguous representation choice resolves toward "record/act on nothing."

## When to Apply

- A parser/scraper feeds a value into a store that a bidirectional mirror or downstream writer will act on: give "unparsed/unknown" an encoding distinct from every actionable value, and make consumers skip it.
- Choosing or changing a UNIQUE constraint/index on any evidence or ledger table: state what "the same fact" means, confirm the stored columns are genuine (not proxies), and confirm the consumer is dup-insensitive before widening.
- Writing a SQLite migration that re-keys an index: `DROP old` + `CREATE new-name`, never a bare `CREATE IF NOT EXISTS` under the same name; prove the new index builds on existing rows; single-source the DDL.
- Adding any provenance/source/type tag to a durable record: make it a closed vocabulary enforced at the write boundary, and backfill existing rows to an explicit value.

## Examples

Unknown vs. empty encoding (`gixen_client._parse_snipe_table`):

```python
# before — a regex miss becomes a positive "no group" claim the mirror trusts,
# durably clearing real membership (N -> 0) on the next sync:
snipe["snipe_group"] = m.group(1) if m else "0"

# after — a miss is encoded as unknown; "0"/""/real values pass through verbatim,
# and the server's mirror skips None (server.main._parse_snipe_group):
snipe["snipe_group"] = m.group(1) if m else None
```

Idempotent index re-key (`server/db.py`, single-sourced in `_apply_migrations`):

```python
# DROP the OLD name explicitly — a bare CREATE UNIQUE INDEX IF NOT EXISTS under
# the OLD name would silently no-op (SQLite idempotency is by index NAME, not
# column list). The old 2-col uniqueness implies 3-col, so CREATE can't fail.
conn.execute("DROP INDEX IF EXISTS idx_group_wins_group_item")
conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_group_wins_group_item_end "
    "ON group_wins(snipe_group, item_id, won_end_at)"
)
```

Closed-vocabulary source enforced at the write boundary (`record_group_win`):

```python
if source not in GROUP_WIN_SOURCES:  # frozenset — typo'd tag can't land in the ledger
    raise ValueError(f"record_group_win: unknown source {source!r}")
```

## Related

- `docs/solutions/architecture-patterns/evidence-layer-disambiguation-vs-heuristic-gating.md` — the parent pattern (evidence-over-gating). This doc is its substrate corollary: the evidence store's representation is itself correctness. That doc's `_group_won_before` section also owns the temporal-membership bound (`group_changed_at`, BUI-384) — the fourth trap in this arc, tracked under BUI-393.
- `docs/solutions/design-patterns/scope-status-writes-to-row-id-not-item-id.md` — sibling representation-discipline doc in the same tables (row identity vs listing identity on status writes); same "silent successful write corrupts a money path" failure shape.
- Tickets: BUI-381 (durable `group_wins` ledger + `refresh_snipe_group` mirror — the substrate these harden), BUI-383 (unknown-vs-empty `snipe_group` encoding), BUI-385 (ledger unique-key re-key + provenance vocabulary), BUI-384/BUI-393 (temporal membership bound, in the evidence-layer doc). BUI-389 extracted this cluster into `server/fallback.py` (behavior-preserving).
- `CONCEPTS.md` → Bidding & Snipes cluster (Bid Group, Tombstone, Phantom WON).
