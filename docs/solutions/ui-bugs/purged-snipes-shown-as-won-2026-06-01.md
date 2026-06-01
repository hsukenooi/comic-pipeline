---
title: /comics Dashboard Shows Removed (PURGED) Auctions as "Won" in Recently-Ended
date: 2026-06-01
category: docs/solutions/ui-bugs
module: gixen-overlay (comics dashboard)
problem_type: ui_bug
component: gixen-overlay (comics dashboard)
severity: high
symptoms:
  - '"Recently ended" table shows "won" pills for auctions the user never bid on'
  - All affected rows are status=PURGED with status_mirror=null and local_snipe_at=null
  - winning_bid on these rows is a stale pre-removal current-bid snapshot below max_bid
root_cause: scope_issue
resolution_type: code_fix
status: diagnosis-only (fix tracked in BUI-50)
tags: [dashboard, gixen, purged, status-filter, recently-ended, won-pill, comics, false-positive]
related_files:
  - plugins/gixen-overlay/src/gixen_overlay/routes.py
  - plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html
  - ../../../gixen-cli/server/db.py
  - ../../../gixen-cli/server/main.py
related_docs:
  - ../best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md
---

# /comics Dashboard Shows Removed (PURGED) Auctions as "Won" in Recently-Ended

## Problem

The `/comics` dashboard's "recently ended" table showed **won** for the entire
`The Incredible Hulk #330–346` McFarlane run — 17 auctions that were never bid on
and never won. All 17 had been removed from Gixen before they ended (owned-dupe
cleanup from BUI-45), yet the dashboard painted them green "won".

This is a pure display-layer fabrication. It is **not** a Gixen defect and **not**
a sniping defect: Gixen never marked these won (they were cancelled before close),
and the local backup sniper never fired them.

## Symptoms

- "Recently ended" table shows "won" pills for auctions the user did not win.
- Affected rows all share this signature in `GET /api/comics/history`:
  `status: "PURGED"`, `status_mirror: null`, `local_snipe_at: null`.
- `winning_bid` on these rows is below `max_bid` (looks like a winning snipe) but is
  actually a stale current-bid snapshot cached ~1 day before the auction ended.
- Legitimately-bid losses on the same page (e.g. Hulk #171/#172/#250/#255/#300)
  render correctly as "outbid" — they carry `status: "LOST"` with `status_mirror`
  and `local_snipe_at` set.

## What Didn't Work

- **Initial hypothesis that Gixen was mis-reporting wins.** Ruled out: the Gixen API
  on the dev machine is even disabled (`501: API DISABLED`); the authoritative
  account lives on the mac-mini, and nothing in Gixen ever marked these won. Checking
  Gixen first was the right call but the defect was downstream of it.
- **Hypothesis that the snipes "failed to fire" (current bid exceeded max).** Ruled
  out: a snipe that fires under market still *fires* and shows `LOST`, not `PURGED`.
  All 17 were `PURGED` with `local_snipe_at: null` — removed before end, so they
  never entered the firing path at all. (collection-check leading-article false
  negative → owned dupes sniped → later removed; see BUI-45. *(auto memory [claude])*)

## Solution

Diagnosis-only — fix tracked in **BUI-50**. Two changes, either of which stops the
symptom; do both (defense in depth):

**1. Filter PURGED out of the history endpoint** —
`plugins/gixen-overlay/src/gixen_overlay/routes.py` (`api_comics_history`, ~line 597).
The active-snipes endpoint already excludes PURGED; the history endpoint does not.

```python
# api_comics_snipes (routes.py:581) — already correct:
WHERE b.status != 'PURGED'

# api_comics_history (routes.py:597) — the inner latest-per-item subquery has NO
# status filter, so PURGED rows pass through. Add the same guard.
```

**2. Give outcome() an explicit PURGED branch** —
`plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` (~line 311).

```js
function outcome(r) {
  if (r.status === "PENDING") return '<span class="pill pending">pending</span>';
  if (r.status === "WON")  return '<span class="pill won">won</span>';
  if (r.status === "LOST") return '<span class="pill lost">outbid</span>';
  // BUG: PURGED falls through to the heuristic below.
  const w = r.winning_bid != null ? parseFloat(r.winning_bid) : null;
  const m = numericMax(r);
  if (w != null && m != null) return w <= m   // stale snapshot < max → false "won"
    ? '<span class="pill won">won</span>'
    : '<span class="pill lost">outbid</span>';
  ...
}
```

Add a `PURGED` case that renders "removed" (or "—") so a leaked row can never be
painted "won" by the value heuristic.

## Why This Works

Causal chain:

1. Owned-dupe snipe removed before the auction ends (BUI-45 cleanup) → `delete_bid`
   soft-deletes the row to `status='PURGED'` (`gixen-cli/server/db.py:232`).
2. `GET /api/comics/history` has no `status != 'PURGED'` filter → the removed row is
   still returned in "recently ended".
3. The row's `winning_bid` is a stale pre-removal **current**-bid snapshot (cached
   the day before close), which is below `max_bid`.
4. `outcome()` handles PENDING/WON/LOST then falls through to
   `winning_bid <= max_bid ? "won" : "outbid"` → stale-low bid < max → **false "won"**.

Filtering at the endpoint removes the row from the table entirely (it was removed —
it doesn't belong in "recently ended"). The `outcome()` branch is a backstop: even
if a PURGED row leaks again, it can't be guessed into a "won".

**Why "removed" guarantees "never won":** two independent skips. Gixen won't bid a
cancelled snipe, and the local backup sniper only selects `status='PENDING'`
(`get_bids_ready_to_snipe`, `gixen-cli/server/db.py:270`), so PURGED rows are never
fired locally either.

**Deeper smell (tracked in BUI-49):** `PURGED` is a soft-delete tombstone written by
two different events — a *live* snipe the user cancelled (`delete_bid`, db.py:232) and
*completed* bids swept up after the fact (`mark_bids_purged`, db.py:249). Conflating
"I pulled out" with "this finished and got tidied" into one status is what lets a
removed-while-live snipe masquerade as a finished auction downstream.

## Prevention

- **Endpoints serving the same table must apply consistent status filters.** The
  active and history endpoints read the same `bids` table; `/api/comics/snipes`
  filtered PURGED and `/api/comics/history` didn't. When adding a status-aware
  endpoint, diff its WHERE clause against sibling endpoints over the same table.
- **Display functions should handle every known status explicitly, not fall through
  to value-based guessing on possibly-stale data.** A `winning_bid <= max_bid`
  heuristic is only safe for statuses where `winning_bid` is a true final price;
  it must never run for soft-deleted/never-resolved rows.
- **Tests to add with the fix (BUI-50):**
  - Overlay route test: a PURGED bid within the 7-day window is excluded from
    `GET /api/comics/history`.
  - JS `outcome()` unit test: a row with `status="PURGED"` never returns the "won"
    pill, even when `winning_bid <= max_bid`.
- **Don't reuse a soft-delete status to mean two things.** See BUI-49 — distinguishing
  user-cancel from completed-sweep would make this class of bug structurally
  impossible.

## Related

- **BUI-50** — fix: history PURGED filter + outcome() PURGED branch (this doc's fix).
- **BUI-49** — follow-up: rename/split PURGED (soft-delete that conflates user-cancel
  with completed-sweep).
- **BUI-45** — originating incident: collection-check leading-article false negative
  sniped the owned #330–346 run, which was then removed — surfacing this display bug.
