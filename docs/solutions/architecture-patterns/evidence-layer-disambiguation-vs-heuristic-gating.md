---
title: "Evidence-layer disambiguation beats heuristic-gating for permissive classifiers"
date: 2026-07-17
category: architecture-patterns
module: "gixen-cli (server/fallback.py — bids WON-inference / classification pipeline)"
problem_type: architecture_pattern
component: background_job
severity: high
related_components:
  - "database"
  - "service_object"
applies_when:
  - "A permissive downstream heuristic (e.g. WON-permissive on ambiguity) produces occasional false positives"
  - "The fix under consideration would add an evidence requirement directly to the heuristic, risking suppression of true positives"
  - "An accepted-risk closure exists whose premise could later be invalidated by a new feature"
  - "A new feature changes cross-row state (e.g. bid groups cancelling siblings) and may reopen a previously accepted-risk classification bug"
symptoms:
  - "Cancelled snipe later stamped WON on an auction it was never bid on (phantom-WON)"
  - "A new feature turns a near-unreachable accepted-risk trigger into an expected, designed-in state"
root_cause: logic_error
resolution_type: code_fix
tags:
  - "won-inference"
  - "evidence-layer"
  - "heuristic-gating"
  - "upstream-evidence"
  - "bid-groups"
  - "accepted-risk"
  - "phantom-won"
  - "money-path"
---

# Evidence-layer disambiguation beats heuristic-gating for permissive classifiers

## Context

BUI-50 (2026-06-01) found purged/removed snipes rendering as "won" in the dashboard — the immediate fix was endpoint-level tombstone filtering (`docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`). The deeper issue was upstream: `_run_ebay_fallback` in `packages/gixen-cli/server/fallback.py` infers WON/LOST from eBay's final sold price by comparing it to `max_bid` — a heuristic needed because Gixen sometimes drops an ended snipe from its list before syncing its true WON status, and the local sniper is disabled (so `local_snipe_result` is always NULL).

BUI-146 examined the case where a snipe was *cancelled* while still live: the auction still ends, eBay still reports a final price, and if that price came in under `max_bid`, the heuristic stamps a phantom WON on an auction never actually bid on. It was closed **accepted-risk** on an explicit premise — live-snipe cancellation was a rare, near-unreachable path — with the sanctioned fix direction (vanish-time disambiguation, BUI-85 style) recorded in a comment at the inference site.

Two evidence-*requirement* fixes were considered then and rejected (this is the "what didn't work" of the arc): (1) requiring a non-NULL `OK:`-prefixed `local_snipe_result` before allowing WON — always NULL with the local sniper disabled, so it suppresses every genuine recovered win; (2) never inferring WON for a vanished row — same failure mode, since a legitimately-won auction that Gixen dropped before sync also "vanished." Both add a requirement to the heuristic itself, and the heuristic's whole reason for existing is to recover wins it has no other evidence for.

BUI-363 then invalidated the accepted-risk premise: Gixen **bid groups** (`snipe_group`) make sibling cancellation the *expected*, designed-in behavior — when one snipe in a group wins, Gixen auto-cancels the other members before their own auctions end. A rare edge case became a routine occurrence; the interim mitigation was a documented manual `gixen purge` after each group win. BUI-371 (PR #205, merged to main at `fcb45ff`, 2026-07-17) is the structural fix this doc generalizes from.

## Guidance

**When a permissive downstream heuristic produces false positives, add positive upstream evidence that removes the offending rows from its input — never add evidence requirements to the heuristic itself.** A heuristic like the eBay price-inference exists precisely because there is no reliable first-party signal for the common case it recovers. Evidence-absence gates cannot distinguish the false positive from the true positive — both share the same absence — so tightening the heuristic indiscriminately suppresses the real outcomes it exists to catch. The false positives, by contrast, often carry *positive* evidence of their own (they vanished early; a sibling already won): classify on that evidence upstream, and subtract those rows from the heuristic's input.

BUI-371's two upstream evidence signals, both sharing one safety margin (`_CANCEL_EVIDENCE_MARGIN`):

1. **Vanish-time evidence** (`gixen_vanished_at`): a PENDING row observed missing from a *healthy, non-empty* Gixen list is stamped with a vanish timestamp (cleared on reappearance or re-add/edit; never stamped against an empty list, which is more likely a scrape glitch). A vanish at least the margin before the auction's end proves a pre-end cancel — an executed snipe stays on Gixen's list until its auction ends, so it can only be observed vanished *after* end.
2. **Group-win evidence** (`_group_won_before`): a sibling in the same non-zero `snipe_group` won an auction that ended at least the margin before this row's end, AND that win falls within this row's own group *membership* — at or after `max(added_at, group_changed_at)` — and no later than the cutoff. The `added_at` half of that bound is what makes the signal safe against Gixen's small, recycled group numbers: without it, a months-old win in a reused group would falsely count as cancel evidence for an unrelated new snipe.

   BUI-384 added the `group_changed_at` half. A snipe can join a group *after* that group already won it — a retroactive `gixen group N` applied on Gixen's web UI (mirrored into the DB by the BUI-381 sync's `refresh_snipe_group`), or a plain edit — so its `added_at` alone predates the win even though it was never a member of the group when the win happened. Left unbounded, that pre-membership win would falsely group-cancel a snipe that only joined the group afterward — the one residual in the false-REMOVED direction (BUI-371's other evidence gaps are all WON-permissive; this one ran the other way). `group_changed_at` is a nullable column stamped with the change time whenever a row's `snipe_group` actually changes: in the edit path (`update_bid`, via a `CASE` keyed off the pre-UPDATE value, so an edit that *keeps* the same group does not re-stamp and needlessly narrow the window) and in the sync mirror (`refresh_snipe_group`, guarded by the same `snipe_group != ?` check). A row whose group has never changed keeps `group_changed_at` NULL and falls back to the `added_at`-only bound — **not retroactive**: a row whose group changed *before* this migration shipped has no historical change-time to backfill, so it keeps the wider `added_at` bound until its *next* group change.

Rows with matching evidence are tombstoned `REMOVED` (with a cause marker in `notes`) instead of ever reaching WON/LOST inference. The inference function's own logic is untouched. Enforcement happens at **every** point a row can take a terminal classification — the still-listed terminal mapping in the sync, the vanished-ended resolver, and the eBay fallback loop itself (which also heals rows wrongly marked ENDED before the fix shipped). A multi-entry-point classification pipeline that enforces evidence at only one entry point keeps the bug on the other paths.

**Corollary — record the premise behind an accepted-risk closure.** BUI-146 accepted the phantom-WON risk on the explicit, written-down premise that live-snipe cancellation was rare. That record is exactly what let BUI-363 (bid groups) be recognized as *invalidating* a closed ticket rather than shipping as an unrelated feature. An accepted-risk closure without its premise recorded is a silent time bomb — nothing signals when a later change reopens it.

## Why This Matters

- **Silent wrong data with money attached.** A phantom WON means the system believes a purchase happened (feeding record-win, history, and calibration) when no bid was ever placed. Data-integrity bug with a financial dimension, not a UI glitch.
- **The naive fix is worse than the bug.** Both rejected gates would have suppressed every genuine recovered win. Evidence-absence gates on a permissive heuristic don't discriminate when false and true positives share the same absence.
- **Designed-in features can invalidate accepted risk.** BUI-146 was a reasonable call given its premise; BUI-363 silently turned the edge case into the common case. The recorded premise is what made the connection findable.
- **Evidence must not overreach — apply the same discipline to the fix.** The group-evidence signal without a lifetime bound would itself have been a bug: a stale WON in a recycled group number suppressing a brand-new genuine win. This was caught as a 4-way-corroborated P0 in review, mid-flight.

## When to Apply

- A downstream heuristic infers an outcome from an ambiguous signal (price comparison, status string, timing window) and is deliberately permissive because a stricter version would suppress the true positives it exists to catch.
- A new feature or workflow change introduces a *routine* path producing the same signal shape as a known false-positive case (here: bid groups making cancelled siblings expected).
- You're about to close an issue as "accepted risk" — write down the premise that makes the risk acceptable so a later change can be checked against it.
- You're adding evidence to reduce false positives and are tempted to gate the inference call site rather than filtering its input rows.
- Any classification pipeline with multiple entry points to a terminal state — evidence-based reclassification must be enforced at every entry point.

## Examples

The margin and the vanish-time proof (`packages/gixen-cli/server/fallback.py`):

```python
_CANCEL_EVIDENCE_MARGIN = timedelta(minutes=10)

def _vanished_while_live(vanished_at_iso: str | None, end_dt: datetime | None) -> bool:
    """True when the snipe was observed missing from a healthy Gixen list at
    least _CANCEL_EVIDENCE_MARGIN before its auction end — it was cancelled
    while live, not executed at end."""
    vanished_dt = _parse_end_iso(vanished_at_iso)
    if vanished_dt is None or end_dt is None:
        return False
    return vanished_dt <= end_dt - _CANCEL_EVIDENCE_MARGIN
```

The lifetime bound that closed the review's P0 (group-number reuse), since tightened from row lifetime to group membership by BUI-384:

```python
def _group_won_before(db, item_id, snipe_group, end_dt, added_at_iso,
                       group_changed_at_iso) -> bool:
    ...
    added_dt = _parse_iso_utc(added_at_iso)
    if added_dt is None:
        return False  # can't scope to a lifetime → no evidence (WON-permissive)
    member_since = added_dt
    if group_changed_at_iso:
        changed_dt = _parse_iso_utc(group_changed_at_iso)
        if changed_dt is None:
            # unparseable stamp: membership start unknowable → no evidence
            return False
        member_since = max(member_since, changed_dt)
    cutoff = end_dt - _CANCEL_EVIDENCE_MARGIN
    rows = db.execute(
        "SELECT COALESCE(auction_end_at, resolved_at) AS won_end_at FROM bids "
        "WHERE snipe_group = ? AND status = 'WON' AND item_id != ? "
        "UNION ALL "
        "SELECT won_end_at FROM group_wins "
        "WHERE snipe_group = ? AND item_id != ?",
        (group, item_id, group, item_id),
    ).fetchall()
    for row in rows:
        won_end = _parse_iso_utc(row["won_end_at"])
        if won_end is not None and member_since <= won_end <= cutoff:
            return True
    return False
```

The membership stamp, keyed off the pre-UPDATE value so an edit that keeps the same group doesn't re-stamp and narrow the window for no reason (`packages/gixen-cli/server/db.py`, `update_bid`):

```python
conn.execute(
    "UPDATE bids SET max_bid=?, bid_offset=?, "
    "group_changed_at=CASE WHEN snipe_group != ? THEN ? "
    "ELSE group_changed_at END, "
    "snipe_group=?, "
    "gixen_vanished_at=NULL WHERE item_id=? AND status='PENDING'",
    (max_bid, bid_offset, snipe_group, now, snipe_group, item_id),
)
```

A classification call site — the eBay fallback, the last-line enforcement point. The `continue` is the entire fix here: remove the row from the heuristic's input; don't touch the heuristic (the WON/LOST inference below is verbatim what BUI-146 examined):

```python
end_dt = _parse_end_iso(row["auction_end_at"])
if _cancelled_before_end(db, iid, row, end_dt):
    update_bid_status(db, iid, "REMOVED", ..., only_id=row["id"])
    _mark_cancelled_tombstone(db, row["id"])
    continue

...
local_result = row["local_snipe_result"] or ""
if local_result.startswith("ERR:") or final_amount >= float(row["max_bid"]):
    inferred_status = "LOST"
else:
    inferred_status = "WON"
```

The exemption that protects the calibration report — positive proof Gixen processed the bid means any LOST is a genuine contested loss and must stay LOST:

```python
# Gixen statuses that are positive evidence Gixen actually processed our bid:
# OUTBID means our bid was placed and beaten; BID UNDER ASKING PRICE means
# Gixen evaluated the snipe at fire time. A snipe carrying one of these was
# not group-cancelled, so its LOST is a genuine contested loss and is exempt
# from the BUI-371 group-cancel reclassification.
_BID_PROCESSED_STATUSES: frozenset[str] = frozenset({"OUTBID", "BID UNDER ASKING PRICE"})
```

## Related

- `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md` — the originating phantom-WON incident (BUI-50): endpoint-level tombstone filtering, the display-layer ancestor of this upstream fix.
- `docs/solutions/best-practices/plugin-owned-read-endpoints-cross-repo-2026-05-19.md` — the endpoint-parity discipline for the same tombstone; downstream mitigation for the same disease, still valid practice one level below this doc.
- `docs/solutions/best-practices/collection-check-cover-year-forwarding-vs-bui129.md` — conceptual cousin in an unrelated domain: same "fix is upstream disambiguation, not downstream gating" shape.
- Tickets: BUI-50 (incident), BUI-85 (vanish handling), BUI-146 (accepted-risk closure with recorded premise), BUI-363 (bid groups — premise invalidated), BUI-371 (the fix, PR #205). Follow-ups: BUI-381 (group-evidence durability), BUI-382 (fallback write hygiene), BUI-384 (membership bound — late-group-join false-REMOVED, PR #214).
- `CONCEPTS.md` → Bidding & Snipes cluster (Snipe, Bid Group, Tombstone, Phantom WON).
