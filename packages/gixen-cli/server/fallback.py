"""eBay-fallback / cancel-evidence cluster, extracted from server/main.py (BUI-389).

The BUI-367..382 arc concentrated the eBay Browse-API fallback (winning-bid
capture for ENDED auctions) and its BUI-371 cancel-evidence classification in
server/main.py, growing that file past ~1900 lines. This module lifts that
cluster out verbatim (mechanical move, no behavior change) — see BUI-389.

Import direction / why `import server.main as main`:
This module and server.main are genuinely mutually dependent, not a clean
one-way split:
  - server.main's `_spawn_fallback_task` (stays there — it's the overlay's
    canary-pinned surface) must call `_run_ebay_fallback`, defined here.
  - server.main's `_sync_gixen` (stays there — the BUI-277 precedent already
    decomposed it in place, and it is not part of this cluster) directly
    calls several helpers defined here (`_group_won_before`,
    `_cancelled_before_end`, `_mark_cancelled_tombstone`,
    `_record_vanish_observations`, `_record_listed_win_evidence`,
    `_parse_snipe_group`) — server.main re-imports them back (see the bottom
    of that file's classification-section replacement).
  - This module needs server.main's app-state (`_get_db`, `logger`,
    `_ebay_fallback_lock`, `_ebay_cooldown_until`) and its eBay-fetch helpers
    (`_fetch_ebay_item_sync`, `_ebay_fetch_bin`, `_parse_end_iso`).
`import server.main as main` (module reference, not `from server.main import
X`) defers every attribute lookup to call time, which (a) sidesteps the
partial-initialization ordering problem a circular `from...import` would hit,
and (b) is required for correctness: several tests monkeypatch these names
directly on `server.main` (e.g. `monkeypatch.setattr(m, "_ebay_cooldown_until",
...)`, `m._ebay_fallback_lock`, `m._fetch_ebay_item_sync`) — a `from` import
here would have captured a stale copy immune to that patching.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from server.db import (
    CANCELLED_TOMBSTONE_NOTE, DEDUP_TOMBSTONE_NOTE, GROUP_WIN_SOURCE_LISTED_WIN,
    TOMBSTONE_STATUSES_SQL, record_group_win, update_bid_status,
)

import server.main as main

# ---------------------------------------------------------------------------
# Cancelled-before-end classification (BUI-371)
# ---------------------------------------------------------------------------
# A snipe cancelled while its auction was still live (user removal on Gixen's
# web UI, or Gixen's bid-group auto-cancel after a sibling won) never places a
# bid — so it must resolve to the REMOVED tombstone, never ENDED/LOST, or the
# eBay price fallback can stamp a phantom WON on it (BUI-146). Per the BUI-146
# decision the WON inference itself is never gated; instead these helpers
# supply POSITIVE evidence of a pre-end cancellation, checked wherever a
# PENDING row is about to take a terminal status. Anything ambiguous falls
# through to today's WON-permissive behavior.
#
# Margin rationale: a snipe Gixen actually executes stays on Gixen's list until
# its auction ends, so it can only be observed vanished *after* the end; and
# Gixen cancels a group's remaining bids promptly after a win (its FAQ's
# dual-win caveat is auctions ending within ~2 minutes of each other). The
# margin only needs to absorb auction_end_at estimation error (computed from
# Gixen's minute-granular countdown) plus clock skew — 10 minutes is
# comfortably past both, while real cancel-to-end gaps are hours or days.
_CANCEL_EVIDENCE_MARGIN = timedelta(minutes=10)

# BUI-382: cap on how long the eBay fallback stays cooled down after a
# rate-limit failure storm. Exclusive to this module — server.main's
# `_ebay_cooldown_until` timestamp it governs is read elsewhere (see the
# module docstring), but the duration constant itself has no other reader.
_EBAY_COOLDOWN = 300.0  # seconds; suppress eBay fallback after a rate-limit storm


def _parse_iso_utc(value: str | None) -> datetime | None:
    """_parse_end_iso, but tolerating SQLite's naive 'YYYY-MM-DD HH:MM:SS'
    (the bids.added_at column default) by assuming UTC — every timestamp this
    server writes is UTC. Needed because comparing a naive datetime against
    the aware ones _parse_end_iso returns raises TypeError."""
    dt = main._parse_end_iso(value)
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_snipe_group(value: str | int | None) -> int | None:
    """Parse a Gixen-reported snipe_group to an int, or None when the value
    is absent, blank, or unparseable (a scrape quirk like 'N/A'). Callers
    must treat None as 'unknown' — never coerce it to 0, because group 0 is
    a positive claim ('no group') that clears membership / suppresses
    evidence. Since BUI-383 the scraper honors the same contract: a regex
    miss arrives as None (unknown), so a listed '0' really is Gixen saying
    'no group' — the refresh mirror below may trust it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _vanished_while_live(vanished_at_iso: str | None, end_dt: datetime | None) -> bool:
    """True when the snipe was observed missing from a healthy Gixen list at
    least _CANCEL_EVIDENCE_MARGIN before its auction end — it was cancelled
    while live, not executed at end."""
    vanished_dt = main._parse_end_iso(vanished_at_iso)
    if vanished_dt is None or end_dt is None:
        return False
    return vanished_dt <= end_dt - _CANCEL_EVIDENCE_MARGIN


def _group_won_before(
    db: sqlite3.Connection, item_id: str, snipe_group: str | int | None,
    end_dt: datetime | None, added_at_iso: str | None,
    group_changed_at_iso: str | None,
) -> bool:
    """True when another snipe in the same non-zero bid group WON an auction
    that ended during this row's group membership (at or after
    max(added_at, group_changed_at)) and at least _CANCEL_EVIDENCE_MARGIN
    before this row's end — Gixen had cancelled this snipe by then, so no bid
    was ever placed on it.

    The lifetime lower bound is what makes group-number reuse safe: Gixen
    groups are small integers (1-10) that get recycled across unrelated
    campaigns, and a WON row keeps its group number forever. Without the
    bound, a months-old win in a reused group would count as cancel evidence
    for a brand-new unrelated snipe — worst case suppressing a real win the
    eBay fallback would otherwise recover. A win that predates this snipe's
    creation cannot have group-cancelled it.

    group_changed_at tightens that bound to group MEMBERSHIP, not row
    lifetime (BUI-384): a snipe joined to a group AFTER that group's win
    (retroactive `gixen group N` on the web UI landing via the BUI-381 sync
    mirror, or an edit) has an added_at that predates the win, so the
    lifetime bound alone would falsely classify it REMOVED — the one residual
    in the false-REMOVED direction. Gixen's own FAQ frames the group cancel
    as an event at win time ("remaining bids canceled once an item in the
    group is won"), applied to bids then in the group; it says nothing about
    late joins, so per the BUI-371 policy the ambiguity resolves
    WON-permissive: a pre-membership win is not cancel evidence. NULL
    group_changed_at (group unchanged since insert) keeps the added_at
    bound; a present-but-unparseable stamp means membership start is
    unknowable → no evidence.

    DB-side sibling of gixen_client.find_sibling_cleanup_targets (which finds
    the same won-group siblings on the *live* Gixen list for purge): this one
    works from stored bids rows and adds the timing bounds, because here the
    question is retrospective — was this snipe already cancelled by its own
    auction's end?

    Consults two sources under identical bounds (BUI-381): live WON bids rows,
    and the durable group_wins ledger — which survives the winner row's
    destruction (mark_bids_purged sweeps WON → REMOVED) and covers winners
    that never got a bids row (first seen already-terminal via the web-add
    path). The live-row arm looks redundant now that every WON writer also
    records to the ledger, but it is deliberate skew tolerance (the
    TOMBSTONE_STATUSES_SQL convention): a WON written by an older installed
    gixen-cli lacks a ledger entry until the next server restart backfills
    it, and evidence must not silently weaken in that window."""
    if end_dt is None:
        return False
    group = _parse_snipe_group(snipe_group)
    if not group:
        return False  # no group, or unparseable → no evidence (WON-permissive)
    added_dt = _parse_iso_utc(added_at_iso)
    if added_dt is None:
        return False  # can't scope to a lifetime → no evidence (WON-permissive)
    member_since = added_dt
    if group_changed_at_iso:
        changed_dt = _parse_iso_utc(group_changed_at_iso)
        if changed_dt is None:
            # A stamp exists but can't be parsed: the membership start is
            # unknowable, so any win might predate the join → no evidence
            # (WON-permissive), matching the added_at-unparseable case.
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


def _cancelled_before_end(
    db: sqlite3.Connection, item_id: str, row: sqlite3.Row,
    end_dt: datetime | None,
) -> bool:
    """Combined cancel-evidence test used by the vanished-ended resolver and
    the eBay fallback. `row` must carry gixen_vanished_at, snipe_group,
    local_snipe_result, added_at, and group_changed_at.

    An 'OK:' local_snipe_result is first-party proof our local sniper fired a
    bid on this auction — whatever the vanish/group signals suggest, we DID
    bid, so 'cancelled, never bid' cannot apply."""
    if (row["local_snipe_result"] or "").startswith("OK:"):
        return False
    return _vanished_while_live(row["gixen_vanished_at"], end_dt) or _group_won_before(
        db, item_id, row["snipe_group"], end_dt, row["added_at"],
        row["group_changed_at"],
    )


def _mark_cancelled_tombstone(db: sqlite3.Connection, row_id: int) -> None:
    """Stamp the BUI-371 marker on a freshly-classified tombstone so it can be
    told apart from user-cancel / completed-sweep tombstones in a later audit
    (the BUI-67 DEDUP_TOMBSTONE_NOTE convention). COALESCE keeps any
    pre-existing note. Caller commits."""
    db.execute(
        "UPDATE bids SET notes=COALESCE(notes, ?) WHERE id=?",
        (CANCELLED_TOMBSTONE_NOTE, row_id),
    )


def _mark_no_price_checked(db: sqlite3.Connection, row_id: int, checked_at: str) -> None:
    """Stamp ebay_no_price_at (BUI-382) once eBay has given a definitive "no
    usable price" answer for an already-tombstoned (REMOVED/PURGED) row, so it
    stops re-entering _ebay_fallback_rows' 7-day tombstone window. Only ever
    called for tombstoned rows — never for a live PENDING/ENDED row, which
    must stay eligible for the WON inference on every future sync (see
    _ebay_fallback_rows' docstring). Caller commits."""
    db.execute(
        "UPDATE bids SET ebay_no_price_at = ? WHERE id = ?",
        (checked_at, row_id),
    )


def _record_vanish_observations(
    db: sqlite3.Connection, gixen_item_ids: set[str], now: str,
    scrape_started_at: str,
) -> None:
    """Track when PENDING rows vanish from Gixen's list (BUI-371).

    Caller must guard on a non-empty snipes list — a row missing from an empty
    list is far more likely a scrape glitch than a removal. A stamp is cleared
    the moment the row reappears, so a transient per-row scrape miss heals on
    the next sync (10-min cadence) and can't later masquerade as cancel
    evidence. Caller commits.

    scrape_started_at guards the stamp against rows added while the scrape was
    in flight: the background _sync_loop holds no _api_lock, so a concurrent
    POST /api/bids can insert a PENDING row that is legitimately absent from a
    list snapshot taken before it existed — that absence is not a vanish.
    """
    if not gixen_item_ids:
        return  # defensive: `item_id IN ()` is a SQLite syntax error
    placeholders = ",".join("?" * len(gixen_item_ids))
    ids = list(gixen_item_ids)
    db.execute(
        "UPDATE bids SET gixen_vanished_at = NULL "
        "WHERE status = 'PENDING' AND gixen_vanished_at IS NOT NULL "
        f"AND item_id IN ({placeholders})",
        ids,
    )
    db.execute(
        "UPDATE bids SET gixen_vanished_at = ? "
        "WHERE status = 'PENDING' AND gixen_vanished_at IS NULL "
        f"AND item_id NOT IN ({placeholders}) "
        "AND datetime(added_at) <= datetime(?)",
        [now, *ids, scrape_started_at],
    )


async def _record_listed_win_evidence(
    db: sqlite3.Connection, snipe: dict, now: str
) -> bool:
    """Record group-win evidence for a listed WON snipe whose win is not in
    the bids table (BUI-381). Returns True when an eBay fetch was performed
    (consuming the caller's per-sync budget), False on any early skip.

    The web-add path never inserts a snipe first seen already-terminal
    (_insert_web_added_bids skips those), so such a winner's WON transition is
    a no-op and the win would otherwise leave no trace for _group_won_before —
    its cancelled siblings would fall through to the fallback's phantom-WON
    window. Winners that DO transition WON on a bids row are recorded inside
    update_bid_status; this covers the row-less (or tombstoned-row) case, and
    the case where the row's stored group diverges from the listed one (the
    WON-row guard is group-aware, so a winner that resolved before its group
    was known still gets eBay-backed evidence under the listed group).

    The win's true end time is fetched from eBay — Gixen's list only says
    'ENDED' — because recording an observation-time proxy would be unsound
    against the classifier's lifetime bound: a win that actually predates a
    sibling's added_at could falsely group-cancel it (the recycled-group
    hazard from the BUI-371 review). No end, no evidence (WON-permissive), and
    the next sync retries naturally: the winner stays on Gixen's list until
    purged, while the already-recorded check keeps this to at most one eBay
    call per (group, item). An end in the future (past the estimation margin)
    is self-contradictory for a WON — eBay is describing a different,
    re-listed auction under the same item id — and records nothing. Caller
    commits.

    Deliberately awaited inline in the sync (same blocking trade as the
    BUI-85 _resolve_vanished_null_end_bids resolver, same cooldown gate,
    same per-sync cap) rather than deferred to the fire-and-forget fallback
    task: the evidence must exist before the sibling's classification runs
    later in this same sync — a deferred write races the fallback's WON
    inference, and a WON stamped with a price is never revisited."""
    group = _parse_snipe_group(snipe.get("snipe_group"))
    if not group:
        return False
    iid = snipe["item_id"]
    if db.execute(
        "SELECT 1 FROM bids WHERE item_id=? AND status='WON' AND snipe_group=? "
        "LIMIT 1",
        (iid, group),
    ).fetchone() is not None:
        return False  # update_bid_status recorded this win at transition time
    if db.execute(
        "SELECT 1 FROM group_wins WHERE snipe_group=? AND item_id=?",
        (group, iid),
    ).fetchone() is not None:
        return False
    if main._ebay_fetch_bin() is None:
        return False
    if datetime.now(timezone.utc).timestamp() < main._ebay_cooldown_until:
        return False
    ebay = await asyncio.to_thread(main._fetch_ebay_item_sync, iid)
    end_iso = (ebay or {}).get("end_date_iso")
    end_dt = main._parse_end_iso(end_iso)
    if end_dt is None or end_dt > datetime.now(timezone.utc) + _CANCEL_EVIDENCE_MARGIN:
        return True  # no usable end → record nothing (WON-permissive); fetch spent
    record_group_win(
        db, iid, group, end_iso, recorded_at=now,
        source=GROUP_WIN_SOURCE_LISTED_WIN,
    )
    main.logger.info(
        "_sync_gixen: recorded group-win evidence for row-less winner %s "
        "(group %d, ended %s)", iid, group, end_iso,
    )
    return True


def _ebay_fallback_rows(db: sqlite3.Connection, now_iso: str) -> list:
    """Rows needing eBay price resolution. Two sets:
    1. PENDING/ENDED — auction ended, status not yet terminal.
    2. The soft-delete tombstone (REMOVED, or legacy PURGED) resolved without a
       winning_bid (e.g. bulk-removed before the fallback ran), within 7 days.

    Excludes BUI-67 dedup losers (REMOVED with notes=DEDUP_TOMBSTONE_NOTE): they
    are not real ended auctions, and the 7-day window matches on their freshly-set
    resolved_at, so without this guard they'd burn an eBay call and could get a
    phantom winning_bid/WON stamp. The 'IS NOT' comparison keeps NULL-notes rows.

    Set 2 also excludes rows already stamped ebay_no_price_at (BUI-382): a
    prior fallback run got a definitive "eBay has no usable final price"
    answer for an already-tombstoned row (reserve not met / unsold), which
    will not change on a later check, so without this it would burn an eBay
    call every sync for the rest of its 7-day window on an auction already
    conclusively classified. Set 1 (PENDING/ENDED, not yet tombstoned) is
    deliberately NOT given this exclusion: a row there is still eligible for
    the WON inference on a future sync, and this single "no price" answer
    could be eBay's data not having settled yet rather than a genuine no-sale
    — nothing here can tell the two apart, so permanently excluding it would
    risk foreclosing a real win (forbidden by the BUI-146 policy). Its
    unbounded re-scan cost is accepted risk, not fixed by this ticket — see
    the comment at its _run_ebay_fallback call site.
    """
    return db.execute(
        f"""
        SELECT item_id, id, max_bid, local_snipe_result, auction_end_at,
               gixen_vanished_at, snipe_group, added_at, group_changed_at,
               0 AS is_purged FROM bids
        WHERE status IN ('PENDING', 'ENDED')
          AND auction_end_at IS NOT NULL
          AND auction_end_at <= ?
          AND winning_bid IS NULL
        UNION ALL
        SELECT item_id, id, max_bid, local_snipe_result, auction_end_at,
               gixen_vanished_at, snipe_group, added_at, group_changed_at,
               1 AS is_purged FROM bids
        WHERE status IN ({TOMBSTONE_STATUSES_SQL})
          AND winning_bid IS NULL
          AND notes IS NOT ?
          AND ebay_no_price_at IS NULL
          AND datetime(COALESCE(auction_end_at, resolved_at)) >= datetime('now', '-7 days')
        """,
        (now_iso, DEDUP_TOMBSTONE_NOTE),
    ).fetchall()


async def _run_ebay_fallback() -> None:
    """Fire-and-forget: ask eBay for the final selling price of any auction
    that's ended without a captured winning_bid. One eBay call per row once
    winning_bid is set. For an already-tombstoned (REMOVED/PURGED) row,
    ebay_no_price_at additionally short-circuits a definitive "no usable
    price" answer (BUI-382) out of the 7-day re-scan window — a tombstoned
    row is already known dead, so this is pure waste reduction. A live
    (PENDING/ENDED) row gets no such permanent stamp: it stays eligible for
    the WON inference on every future sync, by design (see _ebay_fallback_rows'
    docstring).

    Every write in the loop below is id-targeted (only_id= / WHERE id=), not
    item_id-wide (BUI-382, matching the pattern BUI-371 introduced for its
    REMOVED classification): a re-listed/re-added item can carry a live
    PENDING row sharing an item_id with an old resolved/tombstoned row, and an
    item_id-wide write would collateral-stamp the live row too (the BUI-178
    class of blast radius).

    Skipped if a fallback is already running or if we're in rate-limit
    cooldown from a recent failure storm.
    """
    if not main._ebay_fallback_lock:
        return
    if main._ebay_fallback_lock.locked():
        return
    if datetime.now(timezone.utc).timestamp() < main._ebay_cooldown_until:
        return

    async with main._ebay_fallback_lock:
        try:
            db = main._get_db()
            now_iso = datetime.now(timezone.utc).isoformat()
            rows = _ebay_fallback_rows(db, now_iso)

            if not rows:
                return

            failures = 0
            for row in rows:
                iid = row["item_id"]
                is_purged = bool(row["is_purged"])
                ebay = await asyncio.to_thread(main._fetch_ebay_item_sync, iid)
                if not ebay:
                    failures += 1
                    await asyncio.sleep(1.5)
                    continue

                # Write title and end_date_iso for all rows regardless of
                # status. update_bid_status / cache_gixen_data both skip the
                # tombstone (PURGED/REMOVED) rows, so use direct SQL here.
                # BUI-382: id-targeted, like every other write below — a
                # re-listed/re-added item can carry a live PENDING row
                # sharing this item_id (the BUI-178 class of collateral
                # damage), and an item_id-wide write here would leak an
                # unrelated auction's end time onto it, corrupting the
                # local sniper's fire-time calculation for a still-live
                # snipe.
                ebay_title = ebay.get("title") or None
                ebay_end_iso = ebay.get("end_date_iso") or None
                db.execute(
                    "UPDATE bids SET "
                    "ebay_title = COALESCE(?, ebay_title), "
                    "auction_end_at = COALESCE(auction_end_at, ?) "
                    "WHERE id = ?",
                    (ebay_title, ebay_end_iso, row["id"]),
                )

                final_amount: float | None = None
                price = ebay.get("current_price")
                if price:
                    try:
                        final_amount = float(str(price).lstrip("$").strip())
                    except (ValueError, TypeError):
                        final_amount = None
                has_usable_price = final_amount is not None and final_amount > 0

                if is_purged:
                    if has_usable_price:
                        # id-targeted (BUI-382): multiple tombstoned rows can
                        # share an item_id (dedup losers, re-listed items), so
                        # an item_id-wide write here could stamp this price
                        # onto an unrelated tombstoned sibling.
                        db.execute(
                            f"UPDATE bids SET winning_bid = ? WHERE id = ? AND status IN ({TOMBSTONE_STATUSES_SQL})",
                            (final_amount, row["id"]),
                        )
                        main.logger.info(
                            "_run_ebay_fallback: %s (purged) winning_bid=$%.2f",
                            iid, final_amount,
                        )
                    else:
                        # BUI-382: eBay answered but this tombstone has no
                        # usable price (reserve not met / unsold) — that
                        # won't change, so stamp it out of the 7-day re-scan
                        # set instead of re-fetching every sync until it ages
                        # out of the window.
                        _mark_no_price_checked(db, row["id"], now_iso)
                    await asyncio.sleep(1.5)
                    continue

                # BUI-371: positive evidence this snipe was cancelled while its
                # auction was still live (vanished from a healthy Gixen list
                # >= margin before end, or a bid-group sibling won >= margin
                # earlier) means we never bid — resolve REMOVED and never feed
                # it to the WON/LOST price inference below. Normally the sync
                # classifies these first, but the fallback can reach a row
                # ahead of a successful sync (Gixen outage), and rows flipped
                # ENDED before this fix landed are healed here too. Recording
                # the final price keeps history parity with the purged branch.
                end_dt = main._parse_end_iso(row["auction_end_at"])
                if _cancelled_before_end(db, iid, row, end_dt):
                    update_bid_status(
                        db, iid, "REMOVED",
                        winning_bid=final_amount if has_usable_price else None,
                        resolved_at=now_iso,
                        only_id=row["id"],
                    )
                    _mark_cancelled_tombstone(db, row["id"])
                    if not has_usable_price:
                        # BUI-382: this REMOVED row would otherwise still
                        # match _ebay_fallback_rows' tombstone set (NULL
                        # winning_bid, notes carries CANCELLED_TOMBSTONE_NOTE
                        # not DEDUP_TOMBSTONE_NOTE) and get re-fetched from
                        # eBay on every sync for the rest of its 7-day window
                        # even though "no price" here is just as definitive
                        # as in the purged branch below.
                        _mark_no_price_checked(db, row["id"], now_iso)
                    main.logger.info(
                        "_run_ebay_fallback: %s cancelled before end (never bid) "
                        "→ REMOVED", iid,
                    )
                    await asyncio.sleep(1.5)
                    continue

                if not has_usable_price:
                    # eBay returns the high-water bid for reserve-not-met or
                    # unsold listings, which is often 0 or well below our max
                    # — falsely stamping WON. Treat as ENDED with no winning
                    # claim instead. id-targeted (BUI-382), matching every
                    # other write in this loop — a re-listed/re-added item can
                    # carry a live PENDING row sharing this item_id.
                    update_bid_status(
                        db, iid, "ENDED",
                        winning_bid=None,
                        resolved_at=now_iso,
                        only_id=row["id"],
                    )
                    # BUI-382 review (reliability/adversarial): deliberately
                    # NOT stamping ebay_no_price_at here, unlike the two
                    # REMOVED-producing branches above/below. Those tombstone
                    # a row that is already known dead (cancelled-before-end,
                    # or a completed sweep) inside a 7-day window, so a
                    # permanent stamp only trims already-bounded waste. This
                    # branch's row is a genuinely-ended auction still eligible
                    # for the WON inference below on some *future* sync — this
                    # single eBay answer could be a transient "price not
                    # settled yet" read rather than a genuine no-sale, and
                    # nothing here can tell the two apart. Permanently
                    # excluding it would risk foreclosing a real win the
                    # inference exists to recover, which the BUI-146 policy
                    # forbids. Left on its pre-existing unbounded forever-retry
                    # semantics; the resulting waste is accepted risk, not
                    # fixed by this ticket (see BUI-146's own accepted-risk
                    # precedent for the same "correctness over efficiency"
                    # trade-off).
                    main.logger.info(
                        "_run_ebay_fallback: %s -> ENDED (no final price; max=$%.2f)",
                        iid, row["max_bid"],
                    )
                    await asyncio.sleep(1.5)
                    continue

                # Heuristic: 0 < final_price < our max_bid suggests we outbid
                # everyone; final_price >= max_bid means someone matched or beat
                # us. Two additional guards against false positives:
                #   1. Tie at final_price == max_bid → eBay's first-bidder rule
                #      means we likely lost (strict < instead of <=).
                #   2. local_snipe_result starts with "ERR:" → our bid never
                #      landed; mark LOST regardless of price.
                #
                # BUI-146 (do NOT "fix" this inference naively): a snipe
                # cancelled while still live (user removal on Gixen's web UI,
                # or a bid-group auto-cancel — BUI-371) also reaches here once
                # its auction ends, and a final price below max_bid then stamps
                # a phantom WON even though we never bid. Crucially, this same
                # inference is how genuine wins are recovered when Gixen drops
                # an ended snipe before sync reads its WON status — with the
                # local sniper disabled, local_snipe_result is always NULL, so
                # requiring local bid-evidence (or never inferring WON for
                # vanished rows) would SUPPRESS REAL WINS. BUI-371 implemented
                # the sanctioned vanish-time/group-win disambiguation UPSTREAM
                # (positive cancel evidence → REMOVED before a row ever gets
                # here — see _vanished_while_live/_group_won_before and the
                # guard above). The residual evidence-less case (e.g. a live
                # cancel never observed by any sync) remains accepted risk;
                # never gate the inference itself. See BUI-146 for the full
                # analysis.
                local_result = row["local_snipe_result"] or ""
                if local_result.startswith("ERR:") or final_amount >= float(row["max_bid"]):
                    inferred_status = "LOST"
                else:
                    inferred_status = "WON"
                # id-targeted (BUI-382): a re-listed/re-added item can carry a
                # live PENDING row sharing this item_id — an item_id-wide
                # write here would collateral-stamp WON/LOST onto it (the
                # BUI-178 class of blast radius).
                update_bid_status(
                    db, iid, inferred_status,
                    winning_bid=final_amount,
                    resolved_at=now_iso,
                    only_id=row["id"],
                )
                main.logger.info(
                    "_run_ebay_fallback: %s -> %s @ $%.2f (max=$%.2f)",
                    iid, inferred_status, final_amount, row["max_bid"],
                )
                await asyncio.sleep(1.5)

            db.commit()

            # Threshold is 1 when there's a single ended-unresolved item, else
            # half the batch. Without the floor, a single persistently-failing
            # item is retried on every dashboard load forever.
            if failures >= max(1, len(rows) // 2):
                main._ebay_cooldown_until = (
                    datetime.now(timezone.utc).timestamp() + _EBAY_COOLDOWN
                )
                main.logger.warning(
                    "_run_ebay_fallback: %d/%d failed; cooling %ds",
                    failures, len(rows), int(_EBAY_COOLDOWN),
                )
        except Exception:  # noqa: BLE001  # fire-and-forget task-level safety net (same shape as this file's other bare excepts and server.main's own, e.g. its lifespan-shutdown one); ruff's logging-call exemption doesn't recognize the two-level `main.logger.exception(...)` this module's `import server.main as main` pattern requires (BUI-389), unlike the identical bare `logger.exception(...)` this had verbatim in server/main.py
            # BUI-399: roll back the shared singleton connection, matching
            # api_sync (BUI-386) and _ensure_fresh_sync/_sync_loop (BUI-391).
            # The loop above batches its DML into one end-of-cycle commit (see
            # db.commit() a few lines up), so an unexpected mid-loop bug can
            # leave stray uncommitted writes on this process-wide connection
            # for whatever the *next* successful cycle's commit happens to
            # absorb. Read the global directly (not the local `db`, unbound
            # if main._get_db() itself raised before the local assignment) —
            # same reasoning as _sync_loop's identical guard.
            if main._db is not None:
                main._db.rollback()
            main.logger.exception("_run_ebay_fallback: error")
