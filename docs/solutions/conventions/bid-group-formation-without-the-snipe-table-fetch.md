---
title: "Form a bid group through the add/edit path, not `gixen group` — and count home_2.php round trips, not 'does this path read the table'"
date: 2026-08-08
category: conventions
module: "gixen-cli (cli.py group_cmd/edit, gixen_client.add_snipe/modify_snipe, server/main.py api_add_bid/api_edit_bid, add_batch.build_bid_payload)"
problem_type: workflow
component: bidding
severity: medium
mechanized_by: test
enforced_by_test:
  - "packages/gixen-cli/tests/test_gixen_client.py::TestNewSnipeGroupOnEveryWritePath"
  - "packages/gixen-cli/tests/test_server_api.py::test_upsert_of_a_live_row_applies_group_end_to_end"
  - "packages/gixen-cli/tests/test_server_api.py::test_gixen_failure_on_a_group_upsert_writes_no_group"
  - "packages/gixen-cli/tests/test_server_api.py::test_upsert_omitting_group_silently_ungroups_a_grouped_row"
applies_when:
  - "Gixen's home_2.php is stalling and a bid group needs to be formed or changed"
  - "`gixen group` fails and you are looking for another way to group snipes"
  - "Re-running an add-batch rows.json against snipes that are already grouped"
  - "Reasoning about which Gixen write path is 'lighter' during an outage"
related_components:
  - "gixen-cli"
tags:
  - "bid-groups"
  - "gixen-outage"
  - "home-2-php"
  - "snipe-group"
  - "add-batch"
  - "retry-and-reconcile"
---

# Form a bid group through the add/edit path, not `gixen group`

## Context

During the 2026-08-07/08 Gixen outage, `home_2.php` returned healthy headers and then
stalled mid-body on our ~400KB / 91-row snipe page. `gixen group` failed 100% for
~18 hours — groups 1 and 5 never formed — while `gixen add-batch` kept landing rows
under persistent retry. Two duplicate copies of the same comic stayed ungrouped, and
the ~$148 duplicate-win exposure had to be resolved by *removing* snipes instead.

An attempt to route around it — re-POSTing the rows through `add-batch` with
`"group": 1` / `"group": 5` — reported "Server timed out." and left the caps correct
but the groups at 0. That left an open question worth answering precisely, because the
two possible causes call for opposite fixes: **did the upsert never commit, or does the
server's upsert path fail to apply `group` when updating an existing row?**

## Guidance

### 1. The upsert path applies `group`. The 2026-08-08 attempt simply never committed.

`POST /api/bids` threads `snipe_group` all the way through on both branches:
`api_add_bid` → `_modify_and_update_bid` → `modify_snipe(snipe_group=...)` (which POSTs
Gixen's `newsnipegroup` form field) → `update_bid(..., snipe_group)`; the create branch
does the same via `_add_bid_row` → `add_snipe`.

The live ledger settles it. `bid_decisions` rows 278–281 (2026-08-08 01:51:25–01:52:32)
are the four group-via-upsert calls: every one is `trigger=upsert`, `outcome=gixen_failed`.
That outcome is written by `api_add_bid`'s `except GixenError` arm, which is only reached
when the Gixen call itself raised — `update_bid` never ran. All four rows still carry
`snipe_group=0` with `group_changed_at IS NULL`. The same upsert path committed twice for
another item in the same window (rows 275, 277), so the path was working; those four calls
just landed on stalled fetches.

The positive proof is on the other side: items `137585980944` and `147474835165` were
created **already grouped** (`snipe_group=3`) by the 2026-08-07 05:37 add-batch, and are
still group 3 a day later. Because `_sync_gixen` mirrors Gixen's listed group back onto
every PENDING row each cycle (`refresh_snipe_group`), a local-only group claim would have
been erased within minutes. Surviving means **Gixen itself accepted `newsnipegroup`
from the add path.**

**Corollary:** never read a group off the DB as evidence that a group was formed. The
mirror makes `bids.snipe_group` a *reflection* of Gixen's state, not a record of your
intent. Confirm with `gixen list` (or `/api/comics/snipes` after a sync).

**And do not take the write's own success as proof either.** `modify_snipe`'s post-POST
confirmation compares `max_bid` only; `add_snipe`'s just checks the item appears in the
list. Neither verifies `snipe_group`, so a `True` return means *the cap is live*, not
*the group is*. During an outage that distinction is the whole question.

### 2. No write path is `home_2.php`-free. Count round trips instead.

It is tempting to say "the add path doesn't read the snipe table." It does — every
Gixen write in this client is a form POST to `home_2.php` whose response body is that
same large page, and each write additionally re-reads the page to confirm itself
(`add_snipe` → `_verify_present`, `modify_snipe` → `_confirmed`). The useful question is
not *whether* a path touches `home_2.php` but **how many times per item, and whether the
caller can retry and reconcile**:

| mechanism | `home_2.php` round trips for N items | retry / reconcile |
|---|---|---|
| `gixen group N id1..idN` (direct-Gixen only) | **1 + 3N** — one list up front, then per item a pre-POST list, the POST, and the confirm list | none: one shot, `sys.exit(1)` on failure |
| `gixen add-batch` row with `"group": N` (`POST /api/bids` upsert) | **3N** | BUI-697 `INDETERMINATE` + `reconcile_indeterminate_rows` |
| `gixen edit <id> <cap> --group N` in server mode (`PATCH /api/bids/{id}`) | **2N** when the row has a cached `dbidid` (94/102 live PENDING rows did) — the fast path skips the pre-POST lookup | server-side stale-`dbidid` retry only |

`gixen group` is the worst of the three *and* the only one with no retry machinery,
which is exactly why it failed 100% while adds landed. Its per-item `modify_snipe` call
does not reuse the `dbidid` it already resolved in its own opening `list_snipes()` — and
should not start to: a stale `dbidid` addresses a *different* Gixen row, so reusing a
several-round-trips-old one risks writing a cap onto the wrong snipe.

### 3. During an outage, prefer group-at-add; fall back to server-mode `edit --group`.

- **New snipes:** put `"group": N` on the `add-batch` row (or `gixen add --group N`).
  The group rides the add you were going to make anyway — zero extra round trips — and
  inherits add-batch's retry/reconcile semantics.
- **Existing snipes:** `gixen edit <item_id> <current_max_bid> --group N` with
  `COMICS_SERVER_URL` set. Read `<current_max_bid>` from `/api/comics/snipes` (local DB,
  no Gixen call) — **`max_bid` is required on `PATCH` and is re-POSTed to Gixen, so
  passing the wrong number silently changes a real bid.** Omit `--offset`; the server
  passes the current value through from the DB.
- Either way, apply the outage rule that actually worked: **persistent bounded retry +
  reconcile** — write, wait 30–90s, re-read `/api/comics/snipes`, diff, retry the
  stragglers. A single-shot verdict during a mid-body stall is meaningless in both
  directions ("Server timed out." rows frequently committed anyway).
- **Do not shrink `home_2.php` by purging** to make the fetch cheaper — that destroys
  first-party comps.

### 4. The retry trap: an add-batch re-run un-groups already-grouped snipes.

`AddBidRequest.snipe_group` defaults to **0** with no `None` passthrough (unlike
`EditBidRequest.snipe_group`, which gained one in BUI-392), and
`add_batch.build_bid_payload` **always** sends the key. So re-running a rows.json whose
row lacks `"group"` against a snipe that is already grouped POSTs `newsnipegroup=0` and
**un-groups it**. `0` is a positive claim ("ungrouped"), never "unknown" (BUI-383).

This is characterized, not fixed: `gixen add --group` omitted legitimately means group 0
for a fresh add. The operational rule is **carry `group` on every re-run of a grouped
row**, or regroup with `PATCH`, which does pass through an omitted `snipe_group`.

## Ticket answer (BUI-700)

A group *can* be formed while `home_2.php` is stalling — through `POST /api/bids`
(group-at-add) or `PATCH /api/bids/{id}` (`gixen edit --group`), both of which already
carry `snipe_group` end to end and both of which need fewer round trips than
`gixen group`, with retry/reconcile available. What is *not* available is a
`home_2.php`-free write of any kind; the mitigation is fewer round trips plus persistent
bounded retry, not a different endpoint.

## References

- Ticket: BUI-700. Source incident: `docs/retrospectives/2026-08-07-gixen-outage-session.md`
  (F6, §5c-4). Adjacent: BUI-697 (timeout → `INDETERMINATE` + reconcile), BUI-699
  (60s `GixenClient` timeout), BUI-383 (`0` vs unknown `snipe_group`), BUI-392
  (`EditBidRequest` group passthrough), BUI-384 (`group_changed_at`), BUI-381
  (`refresh_snipe_group` mirror + `group_wins` ledger).
- `docs/solutions/conventions/bid-group-purge-is-hygiene-not-safety-net.md` — the other
  half of safe bid-group use.
- `.claude/commands/comic/snipe-add.md` § Bid groups.
- `CONCEPTS.md` → Bidding & Snipes cluster (Bid Group, Snipe).
