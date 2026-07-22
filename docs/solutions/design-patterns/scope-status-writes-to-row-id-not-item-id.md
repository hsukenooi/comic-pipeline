---
title: "Scope status-transition writes to row id, not item_id, on tables where item_id isn't unique"
date: 2026-07-17
last_updated: 2026-07-22
category: design-patterns
module: "gixen-cli (server/main.py, server/db.py — bids table status writes: _run_ebay_fallback, _sync_gixen, update_bid_status)"
problem_type: design_pattern
component: database
severity: high
related_components:
  - "background_job"
applies_when:
  - "Adding or reviewing any status-transition write (UPDATE ... SET status=...) against the bids table, or any table where item_id is not a unique key"
  - "A row-processing loop already holds the row's own id but the status write is keyed only on item_id"
  - "Introducing a permanent negative-result cache or terminal-state write path (the ebay_no_price_at shape) — safe only on already-tombstoned rows, never live PENDING ones"
  - "Grep-auditing server/main.py / server/db.py for WHERE item_id inside status-writing SQL"
symptoms:
  - "A live PENDING snipe silently flips to WON/LOST/ENDED/REMOVED when a different row sharing its item_id transitions"
  - "update_bid_status called without only_id= inside a per-row loop that already has the row's id in scope"
  - "A snipe that should have fired never does, or a phantom result appears for an auction still live"
root_cause: scope_issue
resolution_type: code_fix
tags:
  - "item-id-not-unique"
  - "status-write-scoping"
  - "row-id-vs-item-id"
  - "bids-table"
  - "update-bid-status"
  - "collateral-status-write"
  - "recurring-bug-class"
  - "gixen-server"
---

# Scope status-transition writes to row id, not item_id, on tables where item_id isn't unique

## Context

The `bids` table deliberately allows multiple rows per `item_id` — a listing can be re-added after resolving, and BUI-67's dedup produces tombstoned duplicates. `item_id` is therefore *listing* identity (which eBay auction), while `id` is *row* identity (which bid-tracking record). Any status write keyed only on `item_id` stamps **every** row sharing it — including a live `PENDING` row that has nothing to do with the auction being resolved.

This class has now bitten three times:

1. **BUI-178** (original): `mark_bids_purged`'s completed-sweep tombstoned a live `PENDING` row sharing an `item_id` with a resolved one — the still-running snipe never fired.
2. **BUI-371** (PR #205) introduced `update_bid_status`'s `only_id=` kwarg — but used it only for its own new `REMOVED` classification writes, leaving every pre-existing legacy write in `_run_ebay_fallback` item_id-wide.
3. **BUI-382** (PR #211, 2026-07-17) narrowed all of `_run_ebay_fallback`'s writes (title/end-date cache, purged-branch `winning_bid`, ENDED-no-price, WON/LOST-inference) to id-targeted — and its adversarial review immediately found the identical exposure in `_sync_gixen`'s terminal-transition write, filed as **BUI-388** (unfixed as of this writing).

Point-fixing "the write that broke this time" has not closed the class; each ticket fixed only what it happened to be touching.

## Guidance

**Anchor every status-transition write on row identity.** Carry the row's `id` through the processing loop and write `WHERE id = ?` — for `update_bid_status`, pass `only_id=row["id"]`. Never key a lifecycle transition on `item_id` alone; that implicitly assumes "one row per listing," which is false for this table *by design*.

**Detection heuristic (for reviews and audits):** grep status-writing SQL for `WHERE item_id` (and `update_bid_status(...)` calls missing `only_id=`), and for each hit ask: *is this table actually unique per item_id?* For `bids` the answer is no, so every hit is a finding, not a style nit — this exact shape has three prior incidents.

**Sibling instance on the collection store (BUI-500, 2026-07-22).** The same "an id you'd assume unique isn't" class also lives on the *collection* side, in a different package and operation: a `comics`-row remediation "keyed on `gixen_item_id`" (via `CollectionCache.apply`) is unsafe because a run/lot bought as ONE eBay purchase shares one `gixen_item_id` across every constituent issue — Godzilla: The Half-Century War #1/#2 + an `agent_win` "Full Run #1" all carried `168397474507` (1 id, 3 rows). A `mutate_fn` keyed on `gixen_item_id` silently hits all three. Remediation must key on a genuinely-unique field (`metron_id` there) **or assert exactly-one-match on a full-identity predicate** (`full_title` + `source` + the field being changed) before writing, aborting on any mismatch. Same lesson, different table: never key a mutation on an id whose uniqueness you have not proven.

**Companion rule — permanent negative-result caches only on dead rows.** BUI-382's first draft stamped its `ebay_no_price_at` cache ("eBay confirmed no usable price") on live `PENDING`/`ENDED` rows too. Review rolled that back: a permanent stamp on a live row forecloses a genuine WON if eBay's price data simply hadn't settled at check time, violating the never-gate-the-WON-inference invariant (see the evidence-layer doc under Related). The shipped rule: the permanent stamp goes only on already-tombstoned (`REMOVED`/`PURGED`) rows — already known dead, already inside a bounded 7-day re-scan window — while live rows keep unbounded retry as documented accepted risk.

## Why This Matters

- **The corruption is silent.** A collateral stamp is a plain successful `UPDATE` — no error, no log. It surfaces later as a snipe that never fired or a phantom WON/LOST in views feeding record-win, history, and calibration: wrong data with money attached.
- **The class outlives any single fix.** Three tickets in the same shape prove that only the keying *discipline* — plus the grep heuristic and the regression-test shape below — actually closes it.

## When to Apply

- Any new or edited status-writing SQL or `update_bid_status` call in `server/db.py` / `server/main.py`.
- Any table where the "natural" external key (listing id, order id, …) is intentionally non-unique per row.
- Any proposal to stamp a permanent negative-result marker: require proof the target row is already terminal.

## Examples

```python
# before — item_id-wide: collateral-stamps a live PENDING row sharing the item_id
db.execute(
    "UPDATE bids SET ebay_title = COALESCE(?, ebay_title), "
    "auction_end_at = COALESCE(auction_end_at, ?) WHERE item_id = ?",
    (ebay_title, ebay_end_iso, iid),
)
update_bid_status(db, iid, inferred_status, winning_bid=final_amount, resolved_at=now_iso)
```

```python
# after — id-targeted (BUI-371 pattern, applied across the fallback in BUI-382)
db.execute(
    "UPDATE bids SET ebay_title = COALESCE(?, ebay_title), "
    "auction_end_at = COALESCE(auction_end_at, ?) WHERE id = ?",
    (ebay_title, ebay_end_iso, row["id"]),
)
update_bid_status(db, iid, inferred_status, winning_bid=final_amount,
                  resolved_at=now_iso, only_id=row["id"])
```

**Regression-test shape** (established in `packages/gixen-cli/tests/test_ebay_fallback.py`): seed two rows sharing one `item_id` — one old resolved/tombstoned row the write under test will classify, and one live `PENDING` row — run the write, assert the `PENDING` row is untouched (e.g. `test_fallback_won_write_spares_live_pending_sharing_item_id`). Every new status-writing path gets this test before merging.

## Related

- `docs/solutions/architecture-patterns/evidence-layer-disambiguation-vs-heuristic-gating.md` — the BUI-146→BUI-371 arc in the same function; owns the never-gate-the-WON-inference invariant this doc's companion rule defends. That doc's "Follow-ups: BUI-382" pointer resolves here.
- `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md` — read-side ancestor incident (display filtering), same status/tombstone domain, different layer.
- Tickets: BUI-178 (original), BUI-371 / PR #205 (`only_id=` introduced), BUI-382 / PR #211 (fallback sweep + `ebay_no_price_at` scope rollback), BUI-388 (known remaining site: `_sync_gixen`'s terminal-transition write).
