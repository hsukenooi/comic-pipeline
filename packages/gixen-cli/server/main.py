"""Gixen backend server — FastAPI app with SQLite storage and Gixen proxy."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from gixen_client import (
    GixenClient, GixenError, GixenConnectionError, GixenSnipeNotFoundError,
    GixenAddNotConfirmedError, GixenModifyNotConfirmedError,
    find_sibling_cleanup_targets, parse_listed_max_bid,
    GIXEN_TERMINAL_MAP,
)
from gixen.plugins import (
    load_plugins,
    _invoke_db_tables_isolated,
    _invoke_register_routes,
    _invoke_sync_observed,
    _collect_dashboard_tabs,
)
from server.db import (
    DB_PATH, init_db, insert_bid, update_bid_grades, get_bid_by_item_id,
    get_bid_by_id, get_pending_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    mark_bids_purged, cache_gixen_data,
    set_auction_end_time, get_bids_ready_to_snipe, set_local_snipe_result,
    refresh_snipe_group, mirror_gixen_max_bid,
    TOMBSTONE_STATUSES_SQL,
    write_transaction,
    record_bid_decision, list_bid_decisions,
    BID_DECISION_OUTCOME_COMMITTED, BID_DECISION_OUTCOME_UNCONFIRMED,
    BID_DECISION_OUTCOME_GIXEN_FAILED, BID_DECISION_OUTCOME_BLOCKED,
)
# BUI-389: the eBay-fallback/cancel-evidence cluster (_run_ebay_fallback and
# its BUI-371 cancel-evidence helpers) was extracted to server/fallback.py to
# keep this file's growth in check. Re-imported here (rather than only
# referenced via `server.fallback.X`) because: (1) _sync_gixen and
# _insert_web_added_bids below call several of these as bare names and are
# themselves out of scope for this extraction (BUI-277 already decomposed
# _sync_gixen in place), and (2) this is the overlay-canary re-export
# convention (see _ensure_fresh_sync/_spawn_fallback_task/iso_to_relative
# above) applied to the cluster's own test-import surface — test_server_api.py
# and test_ebay_fallback.py import/patch several of these names directly on
# `server.main`. See server/fallback.py's module docstring for why that
# module imports back from here (`import server.main as main`) instead of
# this being a one-way dependency.
from server.fallback import (
    _CANCEL_EVIDENCE_MARGIN, _parse_iso_utc, _parse_snipe_group,
    _vanished_while_live, _group_won_before, _cancelled_before_end,
    _mark_cancelled_tombstone, _mark_no_price_checked,
    _record_vanish_observations,
    _listed_win_evidence_already_covered, _apply_listed_win_evidence,
    _ebay_fallback_rows, _run_ebay_fallback,
)
from server.policy import (
    PolicyIntent, run_checks, notify_bid_write_committed, config_snapshot,
    evaluate_block, build_block_detail,
)
import ebay_bidder

# The eBay Browse-API fallback (winning-bid capture for ENDED auctions) shells
# out to the `ebay-fetch` console script from apps/ebay rather than importing
# ebay_fetch as a module (BUI-66). apps/* are NOT uv workspace members, so the
# module's transitive deps aren't in the server venv — a subprocess against the
# installed console script sidesteps that, and inherits the server's eBay
# credentials from the environment.
def _ebay_fetch_bin() -> str | None:
    """Resolve the `ebay-fetch` console script to an invocable path, or None.

    Resolved at CALL time, never at import time: EBAY_FETCH_BIN comes from the
    server .env (loaded in the lifespan, *after* this module is imported), and
    the LaunchAgent's PATH may not include ~/.local/bin where uv installs the
    script — so an import-time shutil.which would spuriously report it missing.
    A value containing a path separator is used verbatim if executable; a bare
    name is looked up on PATH.
    """
    name = os.getenv("EBAY_FETCH_BIN", "ebay-fetch")
    if os.sep in name or (os.altsep and os.altsep in name):
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name)


logger = logging.getLogger(__name__)

# The host configures the plugin subsystem's logger explicitly so the audit
# trail emitted by load_plugins() (plugin discovery, registration, validation
# errors) is visible at INFO. Uvicorn does not configure the root logger by
# default, so propagation alone wouldn't show these messages — attach a
# stream handler with a uvicorn-style prefix so the lines blend into the
# normal startup log.
_plugin_logger = logging.getLogger("gixen.plugins")
_plugin_logger.setLevel(logging.INFO)
if not _plugin_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     gixen.plugins: %(message)s"))
    _plugin_logger.addHandler(_h)
# Note: propagate stays True so pytest's caplog (which attaches to root) can
# capture these records in tests. Uvicorn's default config attaches no root
# handler, so propagation does not cause double-logging in production.

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

# BUI-407 (Stage 0 of BUI-400's shared-connection isolation rollout): _db is
# a long-lived, module-global connection, handed out via _get_db()/app.state.db
# to every request handler and background loop — reads, migrations (init_db),
# and lifespan teardown's WAL checkpoint are its legitimate uses. It is
# DEMOTED to that role by convention as of this ticket: every write helper in
# server.db is now commit-free (the caller owns the commit — see e.g.
# insert_bid's docstring), and server.db.write_transaction() exists as the
# write-side counterpart (a fresh short-lived connection per transaction).
# Stage 0 does not yet route any caller through write_transaction() — _db
# still owns every write in this file, unchanged from before this ticket.
#
# BUI-408 (Stage 1): the already-await-free writers (api_add_bid, api_edit_bid,
# api_purge, _sniper_loop's set_local_snipe_result, and the overlay's
# api_link_locg) now route through write_transaction() under the short-held
# _write_lock below instead of _db — see _write_locked()'s docstring.
#
# BUI-409 (Stage 2): _run_ebay_fallback (server/fallback.py) is now ALSO
# routed through write_transaction()/_write_lock, gather-then-apply (every
# eBay fetch first, no DB write held across an await; one write_transaction()
# block applies the whole cycle's writes after).
#
# BUI-410 (Stage 3, landed): _sync_gixen (below) is now gather-then-apply
# too — every eBay evidence lookup (_listed_win_evidence / _resolve_vanished_
# null_end) is hoisted into a read-only gather phase ahead of the writes, and
# ALL its DML (including _sync_loop's formerly lock-free refresh_snipe_group
# PENDING mirror — BUI-405, folded in here — and the web-added inserts that
# used to self-commit) lands on ONE write_transaction() connection under
# _write_lock. No sync writer holds an open transaction on _db across an
# await any more, so _db is now used ONLY for reads + lifecycle (migrations,
# WAL checkpoint) + the single api_remove_bid delete_bid()/commit() that Stage
# 1 left in place (an immediate-commit write, never a cross-await bleed).
# The interim "≤5s single-event-loop stall" a Stage-1/2 write_transaction()
# could hit while _sync_gixen held an open _db transaction across a network
# await (the shape that made two writers contend on SQLite's single-writer
# constraint) is GONE with that open-transaction-across-await shape removed —
# both halves of design §7's sniper-timing worry are now closed.
# See docs/plans/2026-07-18-001-design-shared-connection-isolation-plan.md.
#
# BUI-417 (closes the BUI-409/410 cross-writer read-then-write TOCTOU): both
# _run_ebay_fallback and _sync_gixen snapshot a row's status with a LOCK-FREE
# read at gather time, BEFORE acquiring _write_lock; the terminal writes in
# each apply guard on status CLASS (NOT IN tombstones), not status EQUALITY vs
# the gather snapshot. So a writer whose apply lands after a concurrent writer
# committed a genuine WON/terminal transition to the same row (a non-tombstone
# status the class guard does not catch) could still overwrite it with a
# now-stale inference. Serializing the two APPLIES under _write_lock (Stage 3)
# did NOT close this, because the stale DECISION was made from a pre-lock read.
# The fix re-reads the row FRESH under the lock in each apply (via
# get_bid_by_id) and re-validates every precondition the gather-time decision
# rested on before writing:
#   - _run_ebay_fallback (server/fallback.py): skip the terminal write unless
#     the row is still in the actionable {PENDING, ENDED} set, and re-evaluate
#     the cancel evidence on the fresh row (closing the re-add).
#   - _apply_vanished_null_end (below): skip unless still PENDING, and — for
#     the REMOVED (user-removed) branch — tombstone only when gixen_vanished_at
#     predates THIS cycle's scrape. A mere "gixen_vanished_at still set" check
#     is INSUFFICIENT here: _record_vanish_observations, run earlier in the
#     SAME apply, RE-STAMPS gixen_vanished_at=now on a row a concurrent re-add
#     just cleared — so a re-added snipe looks identically vanished and only the
#     timestamp's age (predates the scrape => a sustained absence, not a re-add)
#     separates them. See both apply sites for the full rationale.
_db: sqlite3.Connection | None = None
# BUI-408: the resolved runtime DB path (the same value passed to init_db()
# below), stashed so write_transaction() callers can target the correct file.
# server.db.DB_PATH's own bare default is fixed at db.py's IMPORT time via
# resolve_server_dir() and does NOT see the DB_PATH env-var override lifespan
# applies (install.sh's .env pins DB_PATH explicitly to ~/.comics-server —
# on a Mac Mini that hasn't physically migrated off the legacy
# ~/.gixen-server yet, resolve_server_dir() would still resolve there,
# diverging from the .env value). A write_transaction() opened on the wrong
# path would silently write to a DIFFERENT database file than every reader —
# always resolve through _get_db_path(), never write_transaction()'s bare
# default.
_db_path: Path | None = None
_api_client: GixenClient | None = None
_api_lock: asyncio.Lock | None = None
_sync_lock: asyncio.Lock | None = None
# BUI-408 (Stage 1): the single app-wide write lock — see _write_locked().
_write_lock: asyncio.Lock | None = None
_last_sync_at: float = 0.0
_SYNC_TTL = 5.0  # concurrent dashboard loads within this window share one Gixen pull
_ebay_fallback_lock: asyncio.Lock | None = None
_ebay_cooldown_until: float = 0.0
# _EBAY_COOLDOWN (the cooldown duration) moved to server/fallback.py (BUI-389)
# — it has no reader left in this file, only in _run_ebay_fallback there.
# _ebay_cooldown_until (the timestamp it governs) stays here: it's read by
# _sync_gixen's gather phase below (the listed-win + _gather_vanished_null_end
# eBay fetches) as well as by server.fallback, so it remains server.main's own
# app-state global (server.fallback reads/writes it via
# `main._ebay_cooldown_until`, not a stale copy — see that module's docstring).
# BUI-85: cap eBay lookups for vanished PENDING rows with no captured end time,
# so a backlog of them can't flood the rate-limited eBay budget in one sync.
_VANISHED_NULL_END_MAX_PER_SYNC = 5
# BUI-381: same discipline for row-less listed-winner evidence lookups — a
# post-outage catch-up sync with several unrecorded group winners must not
# serialize unbounded 30s-timeout eBay subprocess calls inside the sync
# (which api callers hold _api_lock across). Unrecorded winners retry on
# later syncs; they stay on Gixen's list until purged.
_LISTED_WIN_FETCH_MAX_PER_SYNC = 5
# Tracked so the lifespan teardown can cancel + await any in-flight fallback
# task before _db.close() runs. Without this the task can hit a closed DB.
_ebay_fallback_task: asyncio.Task | None = None
# Local-eBay bidder (per-snipe direct-HTTP bid placement). Initialized in
# lifespan; used by the Gixen-side state machine to fire the timed bid.
_bidder: "ebay_bidder.EbayBidder | None" = None

# Separate Gixen client for the background sync loop, so its long-running scrapes
# don't contend on _api_lock with request-handler writes.
_sync_client: GixenClient | None = None
SYNC_INTERVAL = int(os.getenv("GIXEN_SYNC_INTERVAL", "600"))  # 10 min default
_SYNC_BACKOFF_MAX = 3600  # cap exponential backoff at 1 hour

# BUI-562: how long to wait after the FIRST failure. The backoff was always
# exponential, but its base was SYNC_INTERVAL, so the exponent effectively
# started at 1 and the very first failure cost 2x the interval — 1200s. The
# loop could never back off shorter than 20 minutes, even for a blip that
# cleared in seconds. Since BUI-555 this loop is the self-healing mechanism for
# bids.max_bid (what _sniper_loop fires real money from), so those 20 minutes
# are a window where a stale max_bid goes uncorrected.
#
# Measured on ~/.comics-server/server.error.log (1418 sync attempts over 30d):
# Gixen is a FLAPPING HOST, not a rate limiter. Conditioned on the previous
# attempt succeeding, failure rate does not rise as our gap shrinks — it is
# LOWEST (4.7%, n=149) at 30-60s spacing and HIGHEST (54%, n=39) after gaps
# over an hour. And a retry within 30s of a failure recovered 100% of the time
# (12/12, excluding the 2026-07-24 curl-28 timeout storm), where waiting the
# current 1200s recovered only 50%. Retrying sooner is not punished.
_SYNC_BACKOFF_FIRST = 30

# The unexpected-exception class keeps the OLD schedule (1200s, 2400s, 3600s).
# The evidence above is about Gixen connectivity only; an unexpected exception
# is a bug on our side, with no reason to believe it clears in 30 seconds, and
# retrying one fast just spins on a full traceback. SYNC_INTERVAL * 2 as the
# first delay reproduces the pre-BUI-562 SYNC_INTERVAL * 2**n exactly.
_SYNC_BACKOFF_FIRST_UNEXPECTED = SYNC_INTERVAL * 2

# ---------------------------------------------------------------------------
# BUI-604: per-snipe outcome watchdog state
# ---------------------------------------------------------------------------
# Wall-clock stamps the watchdog reads to decide whether it is entitled to an
# opinion. NOTHING else in this server branches on them — see
# _build_snipe_watchdog_report()'s docstring for why that is the whole safety
# argument for this feature.
#
# _last_sync_ok_at is stamped at the very END of _sync_gixen, after its apply
# phase committed and every terminal transition it was going to make has been
# made. That is the same site the BUI-602 heartbeat contract nominates for the
# (still unwired, follow-up-owned) `gixen-sync` ping — see
# docs/reference/job-heartbeat-contract.md.
_process_started_at: float = 0.0
_last_sync_ok_at: float = 0.0
_last_sync_snipe_count: int | None = None


def _get_db() -> sqlite3.Connection:
    assert _db is not None, "DB not initialized"
    return _db


def _get_db_path() -> Path:
    """The resolved runtime DB path (BUI-408) — see _db_path's module-global
    docstring for why this must be used instead of write_transaction()'s bare
    default. Same call-time-lookup shape as _get_db()."""
    assert _db_path is not None, "DB path not initialized"
    return _db_path


@asynccontextmanager
async def _write_locked():
    """BUI-408 (Stage 1 of BUI-400's shared-connection isolation rollout):
    acquire the single app-wide ``_write_lock`` — the "small helper" BUI-408
    calls for, implementing the short-held-lock discipline
    docs/plans/2026-07-18-001-design-shared-connection-isolation-plan.md §3
    decides on. Bracket ONLY a ``write_transaction()`` block with this,
    entered AFTER any network ``await`` has already completed::

        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                ...  # fast, await-free local DML

    Because at most one writer can hold ``_write_lock`` at a time, two
    ephemeral ``write_transaction()`` connections can never overlap —
    SQLITE_BUSY between our own writers becomes unreachable (see
    ``write_transaction``'s docstring and
    docs/plans/2026-07-18-001-design-shared-connection-isolation-plan.md §3).
    NEVER hold this across a network ``await`` — that would serialize request
    latency behind background work, exactly what Approach A was rejected for.

    Exposed as a module-level function (not inlined at each call site)
    because the overlay's ``api_link_locg`` (BUI-400 finding 1: it mutates
    the same shared DB with no lock today) must serialize through this SAME
    lock instance as every gixen-cli writer — a plain
    ``from server.main import _write_lock`` would freeze on the pre-lifespan
    ``None`` (the import happens before lifespan runs and reassigns the
    global), so, like ``_get_db()`` does for ``_db``, this reads the live
    module global at CALL time instead.
    """
    if _write_lock is None:
        raise RuntimeError(
            "_write_lock not initialized — server not started via lifespan"
        )
    async with _write_lock:
        yield


async def _append_bid_decision(
    *,
    item_id: str,
    trigger: str,
    outcome: str,
    bid_row_id: int | None,
    requested_max_bid: float,
    check_results: list[dict],
    advisories: list[dict],
    source: str | None = None,
    bypass: bool = False,
) -> None:
    """BUI-618 (U6/KTD3): append one `bid_decisions` ledger row for this
    write's check-point evaluation.

    Own `write_transaction()` (via `_get_db_path()`, never the module-level
    `DB_PATH` — same BUI-408 discipline as every other write site) under
    `_write_locked()`, own try/except, loud log on failure — a ledger write
    must NEVER block, delay, or fail the snipe write itself (origin AE5).
    Callers invoke this AFTER the write's outcome (Gixen + the local DB) is
    already known — a fire-and-forget tail call, entered with no pending
    network await, matching the "enter _write_locked() only after any await
    has completed" discipline every other write site in this module follows.

    BUI-623 (U9): `config_snapshot(check_results)` — not the bare
    `config_snapshot()` — so the ledger's `config_json` also records the raw
    `POLICY_BLOCK_<CODE>` value for every check that actually ran this
    request (see that function's own docstring); `check_results` is always
    the same list this call's caller already has from `run_checks`.
    """
    try:
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                record_bid_decision(
                    wconn,
                    item_id=item_id, trigger=trigger, outcome=outcome,
                    bid_row_id=bid_row_id, requested_max_bid=requested_max_bid,
                    source=source, bypass=bypass,
                    config=config_snapshot(check_results),
                    check_results=check_results, advisories=advisories,
                )
    except Exception:
        logger.exception(
            "bid_decisions: failed to append ledger row for item_id=%s "
            "trigger=%s outcome=%s — the bid write itself is unaffected",
            item_id, trigger, outcome,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Gixen reports many ended-auction states the original 4-status set misses.
# Map every Gixen status we've observed in production to our internal terminal
# set {WON, LOST, FAILED, ENDED}. Keys are normalized (upper-case, stripped).
#
# OUTBID and BID UNDER ASKING PRICE are both losses: in OUTBID Gixen placed
# our bid but eBay's proxy revealed a higher standing max; in BID UNDER ASKING
# PRICE the current price already exceeded our max at snipe time so Gixen
# skipped the submission. Different mechanics, same outcome — we lost, and
# current_bid is the price that beat us.
#
# BUI-595: the map itself now lives in gixen_client.py (the scrape/parse
# module both this server and cli.py's direct-mode `purge` command already
# import, with no FastAPI dependency) so the two stop drifting apart — cli.py's
# purge dry-run count previously re-listed a stale 4-status subset inline.
#
# BUI-589 deleted this module's own copy of that stale subset
# (`_TERMINAL_GIXEN_STATUSES = {"WON", "LOST", "FAILED", "ENDED"}`), which had
# no remaining references. The 2026-08-02 live capture confirmed
# `Status (main)` really does emit "BID UNDER ASKING PRICE" on a finished
# snipe, so any future consumer reaching for a bare 4-status set would miss a
# genuine terminal state. Use GIXEN_RAW_TERMINAL_STATUSES for "is this snipe
# done?" over raw scraped statuses; never re-introduce a hand-listed subset.
# Re-exported here under the original name: this module's own
# _map_terminal_status still reads it as `_GIXEN_TERMINAL_MAP`, and it stays
# importable from `server.main` for any external caller/test expecting it here.
_GIXEN_TERMINAL_MAP: dict[str, str] = GIXEN_TERMINAL_MAP

# Gixen statuses that are positive evidence Gixen actually processed our bid:
# OUTBID means our bid was placed and beaten; BID UNDER ASKING PRICE means
# Gixen evaluated the snipe at fire time. A snipe carrying one of these was
# not group-cancelled, so its LOST is a genuine contested loss and is exempt
# from the BUI-371 group-cancel reclassification (the calibration report
# depends on real losses staying LOST).
_BID_PROCESSED_STATUSES: frozenset[str] = frozenset({"OUTBID", "BID UNDER ASKING PRICE"})


def _map_terminal_status(gixen_status: str, time_to_end: str) -> str | None:
    """Map a Gixen snipe to our internal terminal status when its auction is done.

    `time_to_end == 'ENDED'` is Gixen's authoritative signal that the auction
    is over. If Gixen reports a recognized terminal status, return its mapped
    value. If only `time_to_end` says ENDED (status string we don't recognize),
    fall back to ENDED — the eBay fallback path can later refine it to WON/LOST.
    Returns None for active snipes (no transition needed).
    """
    mapped = _GIXEN_TERMINAL_MAP.get(gixen_status.upper().strip())
    if mapped:
        return mapped
    if time_to_end.upper().strip() == "ENDED":
        return "ENDED"
    return None

# ebay_fetch.load_config calls sys.exit(1) on missing credentials. Detect that
# eagerly with explicit env-var checks so a misconfiguration shows up as a
# clean log line rather than getting laundered into a fake "fetch failed".
# Once we've logged the problem once we suppress the spam — credentials don't
# get fixed by this process.
_EBAY_CREDS_OK: bool | None = None  # tri-state: None=unchecked, True=ok, False=missing


def _ebay_creds_available() -> bool:
    global _EBAY_CREDS_OK
    if _EBAY_CREDS_OK is not None:
        return _EBAY_CREDS_OK
    has_creds = bool(os.getenv("EBAY_CLIENT_ID")) and bool(os.getenv("EBAY_CLIENT_SECRET"))
    if not has_creds:
        logger.warning(
            "_fetch_ebay_item_sync: EBAY_CLIENT_ID and/or EBAY_CLIENT_SECRET not set; "
            "skipping eBay fallback (silently from here on)"
        )
    _EBAY_CREDS_OK = has_creds
    return has_creds


def _fetch_ebay_item_sync(item_id: str) -> dict | None:
    bin_path = _ebay_fetch_bin()
    if bin_path is None:
        return None
    if not _ebay_creds_available():
        return None
    try:
        proc = subprocess.run(
            [bin_path, item_id, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            logger.warning(
                "_fetch_ebay_item_sync %s: ebay-fetch exited %d: %s",
                item_id, proc.returncode, (proc.stderr or "").strip()[:200],
            )
            return None
        results = json.loads(proc.stdout)
        if results:
            return results[0]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logger.warning("_fetch_ebay_item_sync %s: %s", item_id, e)
    return None


def _parse_end_iso(end_iso: str | None) -> datetime | None:
    """Parse an eBay itemEndDate ('2025-05-01T12:34:56.000Z') into an aware
    datetime, or None if unparseable."""
    if not end_iso:
        return None
    try:
        return datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def iso_to_relative(end_date_iso: str | None) -> str:
    if not end_date_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        diff = dt - datetime.now(timezone.utc)
        total_seconds = diff.total_seconds()
        if total_seconds <= 0:
            return "ENDED"
        days = int(total_seconds // 86400)
        hours = int((total_seconds % 86400) // 3600)
        minutes = int((total_seconds % 3600) // 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        return " ".join(parts) if parts else "<1m"
    except (ValueError, TypeError):
        return "—"


# ---------------------------------------------------------------------------
# Gixen sync helper (used by api_purge and ended-bid resolution)
# ---------------------------------------------------------------------------

async def _gather_vanished_null_end(
    db: sqlite3.Connection,
    gixen_item_ids: set,
    now_dt: datetime,
    snapshot_max_bid_id: int,
) -> list[tuple[str, int, dict | None]]:
    """BUI-410 gather half of the BUI-85 vanished-with-NULL-end resolver.

    PENDING rows that vanished from Gixen but never had an end time captured
    (auction_end_at IS NULL) escape the vanished_ended query in _sync_gixen —
    it requires a non-NULL end. They are ambiguous on their own ("the auction
    ended and Gixen dropped it" vs "the user removed the snipe via Gixen's web
    UI before any sync ran"), so eBay's listing end time is fetched here as the
    disambiguating signal; _apply_vanished_null_end below turns it into the
    ENDED / REMOVED / leave-PENDING decision.

    This is the READ + FETCH phase only — no DB write — so it can run in the
    gather stage before _sync_gixen opens its write transaction. The candidate
    set is write-independent: a vanished row (not in gixen_item_ids) is touched
    by NONE of the apply-phase writes (loop-1 set_auction_end_time / cache /
    refresh and the terminal transitions all key off snipes that ARE in Gixen's
    list; the vanished_ended pass keys off non-NULL ends). So this pre-cycle
    read returns exactly what the apply would have selected. Gated by the eBay
    cooldown and capped per sync to bound rate-limited I/O. Returns
    (item_id, row_id, ebay) tuples — row_id lets the apply id-target its write
    (BUI-390), never stamping a sibling sharing the item_id.

    BUI-584: `snapshot_max_bid_id` restricts candidates to rows that already
    existed when `gixen_item_ids` was snapshotted (before this same async
    function's own eBay-fetch awaits, which can race a concurrent insert). A
    row inserted mid-tick with a NULL auction_end_at has, by construction,
    never been asked about on Gixen — `iid in gixen_item_ids` can only ever be
    False for it, so without this filter it would always look "vanished" and
    would be queued for an eBay lookup that could resolve it straight to
    ENDED (via _apply_vanished_null_end) despite Gixen never having been
    consulted."""
    if _ebay_fetch_bin() is None or now_dt.timestamp() < _ebay_cooldown_until:
        return []
    candidates = db.execute(
        "SELECT item_id, id FROM bids "
        "WHERE status = 'PENDING' AND auction_end_at IS NULL AND id <= ?",
        (snapshot_max_bid_id,),
    ).fetchall()
    resolved: list[tuple[str, int, dict | None]] = []
    checked = 0
    for row in candidates:
        iid = row["item_id"]
        if iid in gixen_item_ids:
            continue  # still live on Gixen; the time_to_end path sets end
        if checked >= _VANISHED_NULL_END_MAX_PER_SYNC:
            break
        checked += 1
        ebay = await asyncio.to_thread(_fetch_ebay_item_sync, iid)
        resolved.append((iid, row["id"], ebay))
    return resolved


def _apply_vanished_null_end(
    conn: sqlite3.Connection,
    resolved: list[tuple[str, int, dict | None]],
    snipes: list,
    now_dt: datetime,
    now: str,
    scrape_started_at: str,
) -> None:
    """BUI-410 apply half of the BUI-85 resolver — sync + await-free, runs
    inside _sync_gixen's single write_transaction(). For each row the gather
    phase fetched eBay for:
      - end in the past   → the auction genuinely ended → ENDED (the eBay
        fallback then fills winning_bid). Glitch-safe: a still-live snipe has
        a future end, so it can never wrongly land here.
      - end in the future → the auction is still live but the snipe is gone →
        the user removed it → tombstone REMOVED (never ENDED/WON/LOST). Only
        when Gixen returned a non-empty list this sync (`snipes`), so an
        empty-list scrape glitch can't mass-cancel live snipes.
      - no eBay data      → leave PENDING and retry a later sync.
    Every write id-targets its row (only_id=, BUI-390) — the BUI-178 class of
    blast radius the REMOVED branch (BUI-371), the vanished-ended write
    (BUI-388) and every _run_ebay_fallback write (BUI-382) already guard.

    BUI-417 TOCTOU guards. `resolved` (the candidate set + each row's eBay end)
    was built by _gather_vanished_null_end in the LOCK-FREE gather phase, so a
    concurrent writer may have transitioned or re-added a row into the
    gather->apply window. Two guards, both keyed on a FRESH re-read of the row
    under _write_lock (get_bid_by_id on `conn`):

      1. Status re-check (both branches): skip unless the row is still PENDING.
         A terminal outcome committed since gather (WON is not a tombstone, so
         update_bid_status's class guard would miss it) must not be overwritten.

      2. Re-add guard (REMOVED branch only): a snipe RE-ADDED between gather and
         apply keeps status='PENDING', so guard 1 does NOT catch it — and the
         obvious "gixen_vanished_at still set" check does NOT either, because
         _record_vanish_observations (run earlier in THIS same apply) RE-STAMPS
         gixen_vanished_at=now on a row a re-add just cleared. The only reliable
         signal is the stamp's AGE: tombstone REMOVED only when the vanish was
         observed BEFORE this cycle's scrape (`gixen_vanished_at <
         scrape_started_at`) — a sustained absence. A same-cycle stamp (a
         genuine first-observation OR a re-add re-stamp — indistinguishable
         here) defers the REMOVED one sync: next cycle a real removal is still
         gone (→ REMOVED then), while a re-add has reappeared in the list (→
         gixen_vanished_at cleared, never tombstoned). Safe to defer: a NULL-end
         PENDING row is inert to the eBay fallback (its set-1 needs a non-NULL
         end) and to the local sniper (which fires on auction_end_at), so the
         one-cycle deferral can leak neither a phantom-WON nor a stray bid."""
    scrape_started_dt = _parse_iso_utc(scrape_started_at)
    for iid, row_id, ebay in resolved:
        end_iso = (ebay or {}).get("end_date_iso")
        end_dt = _parse_end_iso(end_iso)
        if end_dt is None:
            continue  # can't disambiguate yet — leave PENDING, retry later
        # BUI-417 guard 1: re-read the row FRESH under the lock. A terminal
        # outcome (or a delete) landed since the gather selected this candidate
        # → leave it to that outcome; do not overwrite from a stale decision.
        fresh = get_bid_by_id(conn, row_id)
        if fresh is None or fresh["status"] != "PENDING":
            if fresh is not None:
                logger.info(
                    "_sync_gixen: %s status is %s (not PENDING) since gather — "
                    "skipping stale vanished-null-end write (BUI-417 TOCTOU)",
                    iid, fresh["status"],
                )
            continue
        # set_auction_end_time is deliberately NOT called up here (it was, pre-
        # BUI-417). It must run ONLY alongside a terminal write below, never on
        # the guard-2 defer path: a deferred row has to stay genuinely NULL-end
        # so it (a) remains a _gather_vanished_null_end candidate next cycle
        # (that query requires auction_end_at IS NULL) and (b) stays inert to the
        # local sniper (get_bids_ready_to_snipe fires on PENDING rows with a
        # NON-NULL end). Writing a future end on the defer path would strand the
        # row PENDING and expose it to a stray bid on an auction the user
        # cancelled — the exact leak this branch's docstring promises the
        # deferral cannot cause.
        if end_dt <= now_dt:
            # Genuinely ended: record the eBay end (the fallback keys its
            # winning_bid backfill on a non-NULL auction_end_at) and flip ENDED.
            set_auction_end_time(conn, iid, end_iso)
            update_bid_status(
                conn, iid, "ENDED", winning_bid=None, resolved_at=now,
                only_id=row_id,  # BUI-390: id-target, don't stamp siblings
            )
            logger.info(
                "_sync_gixen: %s vanished w/ NULL end; eBay end %s is past → ENDED",
                iid, end_iso,
            )
        elif snipes:
            # BUI-417 guard 2 (re-add): tombstone only a SUSTAINED vanish —
            # gixen_vanished_at stamped before this cycle's scrape. A same-cycle
            # stamp (first-observation or a re-add re-stamped by
            # _record_vanish_observations, indistinguishable) defers one cycle.
            vanished_dt = _parse_iso_utc(fresh["gixen_vanished_at"])
            if (
                scrape_started_dt is None
                or vanished_dt is None
                or vanished_dt >= scrape_started_dt
            ):
                # DEFER — leave the row PENDING and NULL-end (see the note above
                # on why auction_end_at must not be written here).
                logger.info(
                    "_sync_gixen: %s vanished w/ NULL end + future eBay end, but its "
                    "vanish was not observed before this scrape (possible re-add) — "
                    "deferring REMOVED one cycle (BUI-417)", iid,
                )
                continue
            # Sustained vanish → the user removed it. Record the eBay end (history
            # parity with the pre-BUI-417 write) and tombstone REMOVED.
            set_auction_end_time(conn, iid, end_iso)
            update_bid_status(
                conn, iid, "REMOVED", winning_bid=None, resolved_at=now,
                only_id=row_id,  # BUI-390: id-target, don't stamp siblings
            )
            logger.info(
                "_sync_gixen: %s vanished w/ NULL end; eBay end %s still future "
                "→ removed from Gixen, tombstoned REMOVED", iid, end_iso,
            )


def _insert_web_added_bids(
    conn: sqlite3.Connection, snipes: list, existing_ids: set[str],
) -> None:
    # Insert any Gixen snipes not yet in the DB (e.g. added via web UI). Use
    # the full bids table — not just PENDING — so a snipe we already
    # transitioned to a terminal status earlier in this same sync run isn't
    # re-inserted as a fresh PENDING duplicate.
    #
    # BUI-410: runs on `conn` (_sync_gixen's single write_transaction()
    # connection) inside the apply block, not on the shared _db, and no longer
    # self-commits — the write_transaction() owns the one commit for the whole
    # cycle.
    #
    # BUI-418: existing_ids used to be recomputed here every cycle via
    # get_all_bids(conn) — an unindexed full scan of the never-pruned bids
    # history table, run on `conn` (== wconn) INSIDE the apply phase's single
    # write_transaction(), extending how long the app-wide _write_lock is
    # held. It's now computed once by the caller in _sync_gixen's read-only
    # GATHER phase (a plain get_all_bids(db) read on the shared singleton,
    # BEFORE write_transaction()/_write_lock is ever taken) and passed in.
    # This is provably equivalent to the old same-connection read: every
    # apply-phase write ahead of this call (cache_gixen_data,
    # refresh_snipe_group, set_auction_end_time, update_bid_status,
    # _apply_listed_win_evidence) only UPDATEs a row for an item_id Gixen
    # already returned this cycle — none of them INSERTs a new bids row — so
    # the set of existing item_ids can't change between the gather-time read
    # and this apply-time use. The original BUI-410 concern (a snipe this
    # same cycle already transitioned to a terminal status must not be
    # re-inserted as PENDING) still holds: that snipe's row already existed
    # in the table — as PENDING — before this sync began, so it was already
    # captured by the gather-phase read. Same-cycle visibility is preserved;
    # only the timing of the read moved earlier.
    for snipe in snipes:
        snipe_terminal = _map_terminal_status(
            snipe.get("status", ""), snipe.get("time_to_end", "")
        )
        if snipe["item_id"] not in existing_ids and snipe_terminal is None:
            # BUI-555: was `float(snipe.get("max_bid") or 0)`, which raises on
            # any formatting Gixen actually renders — "25.00 USD", a thousands
            # separator — and silently inserted the snipe at a cap of 0.00.
            # parse_listed_max_bid reads those correctly and still returns None
            # (-> 0.0, the pre-existing conservative fallback) for a genuinely
            # unreadable cell, so a malformed row still inserts rather than
            # vanishing. The row is inserted BELOW the per-snipe mirror loop
            # above, so without this it would carry the wrong cap for a whole
            # sync cycle before mirror_gixen_max_bid could repair it.
            max_bid = parse_listed_max_bid(snipe.get("max_bid"))
            if max_bid is None:
                max_bid = 0.0
            # BUI-410: guard bid_offset the same way max_bid (above) and
            # snipe_group (below) already are. Before Stage 3, _insert_web_added_bids
            # ran AFTER _sync_gixen's commit, so an int('N/A') ValueError here only
            # failed the (already-committed) web-add. Now this loop runs INSIDE the
            # single write_transaction() apply, so an unguarded crash would abort
            # the WHOLE cycle — discarding every terminal transition and BUI-371
            # REMOVED classification + BUI-381 group_wins evidence already applied,
            # and a persistently-malformed snipe would crash every sync, leaving a
            # cancelled sibling exposed to the independent fallback's phantom-WON
            # window. Fall back to the default offset instead (as with an unknown
            # group), so one scrape quirk never costs the cycle its evidence.
            try:
                bid_offset = int(snipe.get("bid_offset", 6))
            except (ValueError, TypeError):
                bid_offset = 6
            try:
                insert_bid(
                    conn, snipe["item_id"], max_bid, bid_offset,
                    # BUI-381: never int()-crash the sync batch on a scrape
                    # quirk ('N/A'); an unknown group inserts as 0 and the
                    # per-sync refresh corrects it once it parses.
                    _parse_snipe_group(snipe.get("snipe_group")) or 0,
                    snipe.get("seller"),
                )
                logger.info("_sync_gixen: inserted web-added snipe %s", snipe["item_id"])
            except sqlite3.IntegrityError:
                # existing_ids is snapshotted in the GATHER phase, right after
                # the list_snipes scrape completes (BUI-418) — a concurrent
                # api_add_bid can still insert a PENDING row for this item_id
                # any time after that snapshot, including during the eBay
                # lookups/vanished-null-end gather and the write_transaction()
                # that follow (a wider window than the pre-BUI-418 snapshot
                # point, which was taken even later, at apply-phase entry).
                # This loop runs unlocked-against-Gixen (_sync_loop uses a
                # separate client, no _api_lock), so the partial unique index
                # is what actually prevents the duplicate — catch its violation
                # and skip rather than aborting the whole sync run (BUI-67
                # U4/KTD6).
                #
                # BUI-410: do NOT conn.rollback() here (the pre-Stage-3 code,
                # which self-committed each insert on the shared _db, did). A
                # SQLite constraint violation rolls back only the failed
                # statement, NOT the open transaction — the connection stays
                # usable and every earlier successful insert + every apply-phase
                # write in this same write_transaction() remains pending for the
                # single end-of-block commit. Calling rollback() here would
                # instead discard the WHOLE cycle's apply. Just skip this row.
                logger.debug(
                    "_sync_gixen: %s already present (concurrent add); skipping insert",
                    snipe["item_id"],
                )


async def _sync_gixen(db: sqlite3.Connection, client: GixenClient, *, reraise: bool = False) -> list:
    """Pull current Gixen state and update DB. Returns the snipes list.

    For every snipe Gixen returns, refresh the cached title/seller/current_bid
    on the matching DB row (cache_gixen_data) and apply terminal status
    transitions (WON/LOST/...). Insert new snipes that arrived via Gixen's
    web UI. For PENDING DB rows that have vanished from Gixen's response and
    whose auction_end_at is in the past, flip status to ENDED so the eBay
    fallback can backfill winning_bid — unless there is positive evidence the
    snipe was cancelled while still live (BUI-371: vanished from a healthy
    list well before its end, or a bid-group sibling won well before its end),
    in which case it is tombstoned REMOVED so the fallback can't infer a
    phantom WON on an auction we never bid. Vanished-but-still-in-future rows
    stay PENDING, but the first sync that observes one missing from a
    non-empty list stamps gixen_vanished_at — the timestamp that later
    disambiguates "cancelled before end" from "executed at end".

    `reraise` lets a caller (namely `_sync_loop`, BUI-263) distinguish "Gixen
    genuinely unreachable" from "Gixen reached fine, zero live snipes right
    now" — both used to collapse to an empty list, which made a quiet week
    of no active snipes look identical to a sustained outage.

    BUI-410 (Stage 3 of BUI-400's shared-connection isolation rollout):
    gather-then-apply. The scrape and every eBay evidence lookup
    (_listed_win_evidence, _resolve_vanished_null_end) run FIRST, in a
    read-only GATHER phase that holds no DB write across any `await`; their
    results are keyed by item_id. Then a single APPLY phase runs ALL the DML
    on ONE short-lived write_transaction() connection under the app-wide
    _write_lock, with ZERO awaits inside — so the lock is never held across a
    network call, and no uncommitted sync DML ever bleeds across an await on
    the shared singleton `_db` (design §2's bleed window, now closed). This
    also folds in BUI-405: _sync_loop's formerly lock-free refresh_snipe_group
    PENDING mirror now routes through the same _write_lock path. The apply
    replays the terminal transitions in the SAME WON-first order as before, on
    ONE connection with no intervening commit, so every same-cycle
    read-after-write dependency the BUI-371/381 evidence path relies on (a
    winner's WON row / group_wins entry feeding a sibling's _group_won_before
    check) is preserved bit-for-bit.

    BUI-418 folds one more read into the same GATHER phase:
    _insert_web_added_bids' existing-item_id dedup set, previously
    recomputed every cycle via a full unindexed scan of the bids table INSIDE
    the apply phase's write_transaction() (extending how long _write_lock was
    held). It's now read once, write-free, on the shared `db` before the lock
    is ever taken, and passed into the apply phase.
    """
    # Captured before the scrape so vanish stamping can exclude rows added
    # while the (lockless) scrape was in flight — see _record_vanish_observations.
    scrape_started_at = datetime.now(timezone.utc).isoformat()
    # BUI-584: snapshot the newest bids.id that exists at this same instant —
    # before the scrape, and therefore before every await in the gather phase
    # below (the listed-win-evidence loop and _gather_vanished_null_end's eBay
    # fetches). `id` is an INTEGER PRIMARY KEY (SQLite ROWID), monotonically
    # assigned on insert, so "this row existed when gixen_item_ids was
    # snapshotted" is exactly "id <= snapshot_max_bid_id" — cheaper than
    # re-scraping Gixen after the gather phase finishes, and immune to clock
    # skew, unlike a timestamp comparison. Both the vanished-while-live sweep
    # and _gather_vanished_null_end filter their candidate rows on it, so a
    # row a concurrent request inserts mid-tick — for an auction whose
    # auction_end_at happens to already be in the past — can never be judged
    # "vanished from Gixen" purely from being absent from a snapshot taken
    # before the row existed. Gixen was never even asked about it.
    snapshot_row = db.execute("SELECT MAX(id) FROM bids").fetchone()
    snapshot_max_bid_id = snapshot_row[0] if snapshot_row and snapshot_row[0] is not None else 0
    try:
        snipes = await asyncio.to_thread(client.list_snipes)
    except GixenConnectionError as e:
        # Gixen unreachable at the network layer (BUI-77) — distinct, honest
        # signal so the operator isn't sent chasing credentials.
        logger.warning("_sync_gixen: Gixen unreachable (connectivity, not creds): %s", e)
        if reraise:
            raise
        return []
    except GixenError as e:
        # BUI-391: the disposition depends on reraise — say "suppressed" only
        # when we actually swallow it. On reraise=True (api_sync, _sync_loop)
        # the exception propagates, so "suppressed" would be a lie; log it as
        # reraised. Either way it's logged exactly once here, which _sync_loop
        # relies on (it deliberately doesn't re-log the reraised reason).
        logger.warning(
            "_sync_gixen: GixenError (%s): %s",
            "reraised to caller" if reraise else "suppressed", e,
        )
        if reraise:
            raise
        return []

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    gixen_item_ids = {s["item_id"] for s in snipes}

    # Terminal transitions, computed up front (pure — no DB) so the gather
    # phase can iterate winners WON-first for the listed-win eBay fetches and
    # the apply phase can replay them in the identical order.
    terminal_transitions: list[tuple[dict, str]] = []
    for snipe in snipes:
        internal_status = _map_terminal_status(
            snipe.get("status", ""), snipe.get("time_to_end", ""),
        )
        if internal_status is not None:
            terminal_transitions.append((snipe, internal_status))
    # Apply WON transitions before the rest: a WON row in the DB (and the
    # group_wins ledger entry it writes) is the group-cancel evidence
    # _group_won_before consults when classifying its siblings' ENDED/LOST
    # below (BUI-371) — after a sync gap, the winner and a cancelled sibling
    # can arrive in the same list pull. Stable sort keeps Gixen's list order
    # within each status class.
    terminal_transitions.sort(key=lambda pair: pair[1] != "WON")

    # ---- GATHER PHASE (reads on `db`, network awaits, NO DB write held) ----
    # BUI-418: the _insert_web_added_bids dedup set, read once here (plain,
    # write-free, on the shared singleton `db`) instead of re-scanning the
    # full bids table on `wconn` inside the apply phase's write_transaction()
    # every cycle — see _insert_web_added_bids' docstring comment for the
    # equivalence argument.
    existing_ids = {b["item_id"] for b in get_all_bids(db)}

    # BUI-410: hoist every eBay lookup ahead of the write transaction so no
    # uncommitted DML is ever held across a network await (design §2/§5).
    # eBay bin + cooldown gate the whole phase (matching the pre-BUI-410
    # per-call checks); results are consulted, write-free, in the apply phase.
    ebay_ready = (
        _ebay_fetch_bin() is not None
        and now_dt.timestamp() >= _ebay_cooldown_until
    )

    # BUI-381 listed-win evidence for row-less / tombstoned / divergent-group
    # winners. _listed_win_evidence_already_covered reproduces, from pre-cycle
    # state, the exact skip the pre-BUI-410 in-loop _record_listed_win_evidence
    # applied AFTER the WON write — so a Case-A winner (whose evidence
    # update_bid_status records for free) neither fetches nor spends the
    # per-sync budget, keeping the budget for genuinely row-less winners later
    # in the same sync (a wrong skip either way reintroduces a phantom-WON —
    # see that helper's docstring). Keyed by item_id (unique within snipes).
    listed_win_ebay: dict[str, dict | None] = {}
    if ebay_ready:
        listed_win_fetches = 0
        for snipe, internal_status in terminal_transitions:
            if internal_status != "WON":
                continue
            if listed_win_fetches >= _LISTED_WIN_FETCH_MAX_PER_SYNC:
                break
            group = _parse_snipe_group(snipe.get("snipe_group"))
            if not group:
                continue
            iid = snipe["item_id"]
            if _listed_win_evidence_already_covered(db, iid, group):
                continue
            listed_win_ebay[iid] = await asyncio.to_thread(_fetch_ebay_item_sync, iid)
            listed_win_fetches += 1

    # BUI-85 vanished-with-NULL-end resolution (candidate set is write-
    # independent — see _gather_vanished_null_end).
    vanished_null_end = await _gather_vanished_null_end(
        db, gixen_item_ids, now_dt, snapshot_max_bid_id,
    )

    # ---- APPLY PHASE (one write_transaction() under _write_lock, NO awaits) ----
    # BUI-410 + BUI-405: every write below lands on ONE short-lived connection
    # committed once, with _write_lock held only across this await-free block
    # (never across the gather fetches above) — the same short-held-lock
    # discipline Stage 1/2 wired up for the already-await-free writers.
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            # BUI-371: vanish bookkeeping. Only against a non-empty list — an
            # empty scrape is more likely a glitch (BUI-85's guard), and mass-
            # stamping live snipes as vanished could later mislabel them.
            if snipes:
                _record_vanish_observations(wconn, gixen_item_ids, now, scrape_started_at)

            for snipe in snipes:
                iid = snipe["item_id"]
                cache_gixen_data(
                    wconn, iid,
                    snipe.get("title") or None,
                    snipe.get("seller") or None,
                    snipe.get("current_bid") or None,
                    snipe.get("dbidid") or None,  # BUI-116: warm the edit fast-path cache
                )
                # BUI-381/BUI-405: mirror the list's snipe_group onto the live
                # row on every sync (see refresh_snipe_group), now under
                # _write_lock. Runs before the terminal transitions below, so a
                # winner whose group was applied retroactively on Gixen's web UI
                # carries it by the time its WON is recorded as group evidence.
                # An unparseable value (None) is skipped. A real change stamps
                # group_changed_at=now (BUI-384), which bounds _group_won_before
                # so this retroactive join can't be backdated to added_at and
                # swallow a pre-join win as cancel evidence.
                listed_group = _parse_snipe_group(snipe.get("snipe_group"))
                if listed_group is not None:
                    refresh_snipe_group(wconn, iid, listed_group, changed_at=now)
                # BUI-555: mirror the list's max_bid the same way. The scrape
                # has always carried it; the sync just discarded it, so a
                # modify that landed on Gixen but failed its confirm READ (503
                # from api_edit_bid, update_bid skipped) diverged permanently.
                # _sniper_loop fires the local backup bidder straight off
                # bids.max_bid, so a stale cap is real money, not a display
                # bug. Runs BEFORE the terminal loop below, so a snipe that
                # resolves in this same cycle carries the corrected cap into
                # history. mirror_gixen_max_bid owns the PENDING gate and the
                # scrape-vs-local-write ordering guard; None here means the
                # cell was blank/unparseable and the DB keeps its value.
                listed_max_bid = parse_listed_max_bid(snipe.get("max_bid"))
                if listed_max_bid is not None:
                    repaired = mirror_gixen_max_bid(
                        wconn, iid, listed_max_bid,
                        observed_at=now, scrape_started_at=scrape_started_at,
                    )
                    if repaired is not None:
                        # WARNING, not INFO: a divergence means an earlier
                        # local write was lost, so each line is a defect
                        # sighting on the money path — worth finding in a log.
                        logger.warning(
                            "_sync_gixen: max_bid for item=%s diverged from Gixen "
                            "(local %s, Gixen %s) — mirrored Gixen's value (BUI-555)",
                            iid, repaired[0], repaired[1],
                        )
                # Refresh auction_end_at from Gixen's relative time string on
                # every sync (Gixen only gives "21 h, 30 m, 43 s") so the local
                # sniper has a current end time without depending on eBay.
                time_to_end = snipe.get("time_to_end", "")
                if time_to_end and time_to_end.upper() != "ENDED":
                    delta = _parse_time_to_end(time_to_end)
                    if delta is not None:
                        set_auction_end_time(wconn, iid, (now_dt + delta).isoformat())

            for snipe, internal_status in terminal_transitions:
                iid = snipe["item_id"]
                gixen_status = snipe.get("status", "")
                # BUI-390: the DB row this Gixen snipe transitions, read fresh
                # on wconn (so it reflects this cycle's loop-1 writes above).
                # Reused by both the BUI-371 REMOVED branch and the terminal
                # write so both id-target (only_id=). A live PENDING row is
                # always the newest for its item_id (the partial unique index +
                # upsert path forbid a second row while a PENDING one exists),
                # so id DESC LIMIT 1 returns the exact row to transition.
                db_row = get_bid_by_item_id(wconn, iid)
                # For WON/LOST, current_bid is the final price. For
                # ENDED/FAILED with an unknown status string there's no reliable
                # price — leave winning_bid None for the eBay fallback.
                winning_bid = None
                if internal_status in ("WON", "LOST"):
                    current_bid = snipe.get("current_bid", "")
                    if current_bid:
                        try:
                            winning_bid = float(current_bid.split()[0])
                        except (ValueError, IndexError):
                            pass
                # BUI-371: a still-listed snipe reaching its end as ENDED
                # (unrecognized status) or a plain LOST may be a group-cancelled
                # sibling that was never bid on — resolve it REMOVED so the eBay
                # fallback can't phantom-WON it and the calibration report
                # doesn't count a loss we never contested. Exempt: statuses
                # proving Gixen processed our bid, an 'OK:' local snipe result,
                # and already-tombstoned rows. WON is never reclassified.
                if (
                    internal_status in ("ENDED", "LOST")
                    and gixen_status.upper().strip() not in _BID_PROCESSED_STATUSES
                ):
                    if (
                        db_row is not None
                        and db_row["status"] not in ("PURGED", "REMOVED")
                        and not (db_row["local_snipe_result"] or "").startswith("OK:")
                    ):
                        # No stored end → no evidence test. `now` is only an
                        # upper bound on the true end; substituting it would
                        # WIDEN the window. Skipping is safe: the row resolves
                        # ENDED below and the eBay fallback re-runs this check
                        # with eBay's true end time.
                        end_dt = _parse_end_iso(db_row["auction_end_at"])
                        if end_dt is not None and _group_won_before(
                            wconn, iid, snipe.get("snipe_group"), end_dt,
                            db_row["added_at"], db_row["group_changed_at"],
                        ):
                            update_bid_status(
                                wconn, iid, "REMOVED", None, now,
                                snipe.get("status_mirror"), only_id=db_row["id"],
                            )
                            _mark_cancelled_tombstone(wconn, db_row["id"])
                            logger.info(
                                "_sync_gixen: %s group-cancelled before its end → REMOVED "
                                "(Gixen showed %s/%s)", iid, gixen_status or "?", internal_status,
                            )
                            continue
                # BUI-390: id-target the terminal write. Item_id-wide, this
                # could collateral-stamp an older resolved-but-unpurged sibling
                # sharing the item_id — and, for WON, record a false group_wins
                # entry for that stale row. db_row is None only for a snipe first
                # seen already-terminal (the web-add insert below skips ALL
                # terminal snipes); only_id=None keeps the item_id-wide form, a
                # harmless no-op there. For a WON that's the row-less-winner case
                # the BUI-381 evidence path below then handles.
                update_bid_status(
                    wconn, iid, internal_status, winning_bid, now,
                    snipe.get("status_mirror"),
                    only_id=db_row["id"] if db_row is not None else None,
                )
                if internal_status == "WON" and iid in listed_win_ebay:
                    # BUI-381: this winner is row-less / tombstoned / divergent-
                    # group (the gather phase decided so via
                    # _listed_win_evidence_already_covered and fetched its eBay
                    # end time), so update_bid_status above recorded no ledger
                    # evidence — record it here from that fetched end. Runs in
                    # the WON-first portion of the loop, so the ledger entry
                    # exists before any sibling's _group_won_before check below.
                    # Case-A winners are never in listed_win_ebay (update_bid_status
                    # recorded those), so this is a no-op for them.
                    _apply_listed_win_evidence(wconn, snipe, now, listed_win_ebay[iid])

            # Vanished + ended → flip to ENDED. The eBay fallback then picks
            # these up (ENDED rows with NULL winning_bid). Read fresh on wconn
            # so it excludes rows this cycle already transitioned.
            #
            # BUI-584: `id <= snapshot_max_bid_id` restricts the candidate set
            # to rows that already existed when `gixen_item_ids` was
            # snapshotted, at the top of this tick. Without it, a row a
            # concurrent request inserts during this tick's gather phase
            # (after the snapshot, before this fresh read) — for an auction
            # whose auction_end_at already happens to be in the past — is
            # absent from gixen_item_ids purely because Gixen was never asked
            # about it, not because it "vanished." The `iid in gixen_item_ids`
            # check below can't tell those apart on its own; this filter
            # removes the row from consideration entirely, deferring it to a
            # later sync where the snapshot and the fresh read are both
            # current.
            vanished_ended = wconn.execute(
                """
                SELECT item_id, id, auction_end_at, gixen_vanished_at, snipe_group,
                       local_snipe_result, added_at, group_changed_at FROM bids
                WHERE status = 'PENDING'
                  AND auction_end_at IS NOT NULL
                  AND auction_end_at <= ?
                  AND id <= ?
                """,
                (now, snapshot_max_bid_id),
            ).fetchall()
            for row in vanished_ended:
                iid = row["item_id"]
                if iid in gixen_item_ids:
                    continue  # still on Gixen, will resolve via Gixen path
                # BUI-371: disambiguate before flipping ENDED (which feeds the
                # eBay WON inference). Positive evidence the snipe was cancelled
                # while its auction was still live — observed vanished from a
                # healthy Gixen list >= margin before end, or a bid-group
                # sibling won >= margin earlier — means we never bid: REMOVED.
                # No evidence → ENDED as before.
                end_dt = _parse_end_iso(row["auction_end_at"])
                if _cancelled_before_end(wconn, iid, row, end_dt):
                    update_bid_status(
                        wconn, iid, "REMOVED", winning_bid=None, resolved_at=now,
                        only_id=row["id"],
                    )
                    _mark_cancelled_tombstone(wconn, row["id"])
                    logger.info(
                        "_sync_gixen: %s vanished from Gixen while still live "
                        "(cancelled, never bid) → REMOVED", iid,
                    )
                    continue
                # BUI-388: id-targeted, matching the REMOVED branch above and
                # the BUI-382 pattern in _run_ebay_fallback — an item_id-wide
                # write here could collateral-stamp an unrelated non-tombstoned
                # sibling sharing this item_id (the BUI-178 class of blast
                # radius).
                update_bid_status(
                    wconn, iid, "ENDED", winning_bid=None, resolved_at=now,
                    only_id=row["id"],
                )
                logger.info(
                    "_sync_gixen: %s vanished from Gixen and auction has ended → ENDED",
                    iid,
                )

            # BUI-85 vanished-with-NULL-end: apply the gather-phase eBay results.
            # BUI-417: scrape_started_at lets the REMOVED branch tell a
            # sustained vanish (stamped before this scrape) from a same-cycle
            # stamp that a concurrent re-add may have caused.
            _apply_vanished_null_end(
                wconn, vanished_null_end, snipes, now_dt, now, scrape_started_at,
            )

            # Web-added inserts join the same transaction (BUI-410).
            # BUI-418: existing_ids was already computed in the GATHER phase
            # above (write-free, off the shared `db`) — see
            # _insert_web_added_bids' docstring comment for why that's still
            # equivalent to reading it here on wconn.
            _insert_web_added_bids(wconn, snipes, existing_ids)

            # BUI-624: the `gixen-sync` heartbeat, fired as the LAST statement
            # inside the apply transaction rather than beside
            # _stamp_sync_observed below. Both the mechanism and the placement
            # are forced, for different reasons.
            #
            # MECHANISM. gixen-cli cannot import the overlay that owns the
            # heartbeat contract (the dependency runs overlay → gixen-cli,
            # never back), so the ping has to cross the boundary outward.
            # Three ways existed:
            #
            #   (a) an HTTP self-call to the overlay's own heartbeat endpoint
            #       (deliberately not spelled out here: the contract-doc test
            #       greps for that literal to decide whether a job is really
            #       wired, and a rejected option must not read as a call site)
            #       — rejected: the server would have to know its own bind URL,
            #       and a blocking POST from the event loop to itself deadlocks
            #       under a single-worker uvicorn. It also makes a purely local
            #       write depend on the network stack.
            #   (b) writing the overlay's `heartbeats` table directly — works
            #       (one SQLite file), but gixen-cli would encode a
            #       plugin-owned table name and schema, inverting the one
            #       import direction the plugin system exists to protect.
            #   (c) a pluggy hookspec — chosen. Matches the existing
            #       check_bid_write / on_bid_write_committed hooks (BUI-617/U3)
            #       and leaves the host ignorant of heartbeats entirely.
            #
            # PLACEMENT. A heartbeat is I/O, and the comment below forbids I/O
            # after the commit. Firing inside the transaction removes the
            # post-commit I/O rather than arguing about it, and binds the
            # heartbeat to the fate of this cycle's own writes.
            #
            # MUST STAY LAST in this block. _invoke_sync_observed brackets the
            # hook in a SAVEPOINT, and a savepoint taken when no DML is pending
            # is the OUTERMOST one — its RELEASE commits. Harmless here (a
            # cycle with nothing to change has no writes for the heartbeat to
            # contradict), but a statement added below it could fail with the
            # heartbeat already committed. See _invoke_sync_observed's docstring.
            _invoke_sync_observed(
                getattr(app.state, "plugin_manager", None),
                wconn, len(snipes), logger=logger,
            )

    # BUI-604: a completed pass — the scrape reached the apply phase and the
    # write transaction committed without raising. Stamped HERE, after every
    # write above, so the watchdog can never claim "this snipe never resolved"
    # from a cycle that died before it had the chance to resolve it. Both
    # assignments are pure observer state: no code path in this server reads
    # them except _build_snipe_watchdog_report(), so nothing about the
    # WON-inference, the PENDING lifecycle, or the sniper changes shape here.
    #
    # Deliberately NOT stamped on the `return []` paths above — a
    # GixenConnectionError/GixenError is exactly the case the watchdog must
    # notice as "I am blind", not treat as a healthy observation.
    #
    # Placing anything after a committed apply phase invites the question "what
    # if it raises here?" — an exception past the commit would leave the DML
    # intact but turn a healthy cycle into a _sync_loop backoff / an api_sync
    # 500. It cannot: _stamp_sync_observed only reads the clock, calls len() on
    # a list, and assigns two module globals. No I/O, no parsing, no lookup that
    # can miss. Keep it that way if it ever grows — BUI-624 needed a heartbeat
    # ping at exactly this moment and deliberately did NOT add it here, putting
    # it inside the transaction above instead, precisely to honour this line.
    _stamp_sync_observed(len(snipes))
    return snipes


def _stamp_sync_observed(snipe_count: int) -> None:
    """Record that a full _sync_gixen pass completed (BUI-604).

    A function rather than two inline assignments so `global` bookkeeping stays
    out of _sync_gixen (whose body is the WON-inference path and should not
    grow module-state mutation), and so tests can assert the stamp without
    reaching into the sync internals.
    """
    global _last_sync_ok_at, _last_sync_snipe_count
    _last_sync_ok_at = datetime.now(timezone.utc).timestamp()
    _last_sync_snipe_count = snipe_count


def _sync_backoff_delay(consecutive_failures: int, *, first_delay: int) -> int:
    """Seconds to sleep before the next sync attempt (BUI-562).

    Zero failures is the healthy cadence, SYNC_INTERVAL — never the short
    retry base, so a working loop can't spin hot. From the first failure on,
    the delay is `first_delay` doubling per additional consecutive failure,
    capped at _SYNC_BACKOFF_MAX. The exponent is `consecutive_failures - 1`,
    so `first_delay` is literally the delay after the first failure and not
    twice it — that off-by-one is the whole bug this fixes.
    """
    if consecutive_failures <= 0:
        return SYNC_INTERVAL
    # Clamp the exponent: consecutive_failures is unbounded (it has reached
    # 177 historically) and nothing above the cap can change the result.
    exponent = min(consecutive_failures - 1, 32)
    return min(first_delay * (2 ** exponent), _SYNC_BACKOFF_MAX)


# Background sync loop — primarily for the local sniper, which needs fresh
# auction_end_at to fire bids at the right time. The dashboard does its own
# pull-on-visit (_ensure_fresh_sync) and doesn't depend on this loop, but the
# loop keeps state fresh enough that the sniper can act when nobody's looking.
async def _sync_loop() -> None:
    consecutive_failures = 0
    last_error: Exception | None = None
    # Always reassigned by whichever except handler ran before it is read;
    # initialized so the schedule has a defined base regardless.
    first_delay = _SYNC_BACKOFF_FIRST
    while True:
        try:
            if _sync_client is not None:
                db = _get_db()
                # reraise=True: a call that *returns* (even an empty list —
                # e.g. no live snipes right now) is success. Only a raised
                # GixenConnectionError/GixenError counts as a failure; BUI-263
                # found the old "falsy result == failure" check was mistaking
                # a quiet week of zero snipes for 177+ hours of outage.
                await _sync_gixen(db, _sync_client, reraise=True)
            consecutive_failures = 0
            last_error = None
        except (GixenConnectionError, GixenError) as e:
            # Already logged with the specific reason inside _sync_gixen —
            # don't also dump a full traceback here on every retry.
            consecutive_failures += 1
            last_error = e
            # BUI-562: Gixen flaps; probe again in seconds, not 20 minutes.
            first_delay = _SYNC_BACKOFF_FIRST
        except Exception as e:
            # BUI-410 (Stage 3 landed): the BUI-391 `_db.rollback()` that used
            # to live here is retired. _sync_gixen no longer batches DML on the
            # shared singleton `_db` — every write goes through its own
            # write_transaction() (which rolls back + closes itself on any
            # exception), and the gather phase only READS `_db`. So an
            # unexpected failure can no longer leave stray uncommitted sync
            # writes for the next cycle's commit to absorb; there is nothing to
            # roll back. Per-caller rollback net superseded — see
            # docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md.
            logger.exception("_sync_loop: unexpected error, continuing")
            consecutive_failures += 1
            last_error = e
            # A bug on our side, not a flapping host — keep the old, slower
            # schedule rather than re-raising a traceback every 30 seconds.
            first_delay = _SYNC_BACKOFF_FIRST_UNEXPECTED

        # Exponential backoff, capped at 1 hour. The base depends on which
        # failure class we last saw — see _sync_backoff_delay (BUI-562).
        delay = _sync_backoff_delay(consecutive_failures, first_delay=first_delay)
        if consecutive_failures:
            logger.warning(
                "_sync_loop: %d consecutive failure(s) (%s: %s), sleeping %ds",
                consecutive_failures, type(last_error).__name__, last_error, delay,
            )
        await asyncio.sleep(delay)


def _parse_time_to_end(s: str) -> timedelta | None:
    """Parse Gixen relative time string like '1 d, 20 h, 59 m' into a timedelta."""
    total = 0
    matched = False
    for part in s.split(","):
        part = part.strip()
        if m := re.match(r"(\d+)\s*d", part):
            total += int(m.group(1)) * 86400
            matched = True
        elif m := re.match(r"(\d+)\s*h", part):
            total += int(m.group(1)) * 3600
            matched = True
        elif m := re.match(r"(\d+)\s*m", part):
            total += int(m.group(1)) * 60
            matched = True
        elif m := re.match(r"(\d+)\s*s", part):
            total += int(m.group(1))
            matched = True
    # BUI-184: gate on "did any part parse", not "is total truthy". A snipe seen
    # at exactly "0 s" parses to 0 seconds (auction about to end) and must yield
    # timedelta(0) so auction_end_at is set and the local sniper fires it — only a
    # genuinely unparseable/empty string returns None.
    return timedelta(seconds=total) if matched else None


SNIPER_INTERVAL = 10  # check every 10 seconds


async def _sniper_loop() -> None:
    while True:
        try:
            if _bidder is not None:
                db = _get_db()
                now_iso = datetime.now(timezone.utc).isoformat()
                ready = get_bids_ready_to_snipe(db, now_iso)
                if ready:
                    fired_at = datetime.now(timezone.utc).isoformat()
                    logger.info("_sniper_loop: firing %d bid(s) concurrently", len(ready))
                    bids = [{"item_id": b["item_id"], "max_bid": b["max_bid"]} for b in ready]
                    results = await _bidder.place_bids_concurrent(bids)
                    # BUI-408: one write_transaction() PER bid, not one
                    # bundling all N — these bids were already fired
                    # (irreversibly) on eBay above, and get_bids_ready_to_snipe
                    # re-selects anything with local_snipe_at still NULL. A
                    # single batched transaction would mean one bid's write
                    # failure (e.g. a transient BUSY against the still-lock-free
                    # _sync_gixen/_run_ebay_fallback writers) rolls back every
                    # OTHER bid's already-successful result in the same batch,
                    # so the next 10s tick would re-fire bids that already
                    # placed a real bid — a duplicate submission. Each bid's
                    # write is still fully await-free (no yield point inside
                    # the loop body — the one await already happened above),
                    # so per-bid locking stays cheap; matches api_purge's
                    # sibling-removal loop, which already does one
                    # write_transaction() per iteration for the same reason.
                    for bid, result in zip(ready, results):
                        result_str = ("OK: " if result["success"] else "ERR: ") + result["message"]
                        async with _write_locked():
                            with write_transaction(_get_db_path()) as wconn:
                                set_local_snipe_result(wconn, bid["item_id"], fired_at, result_str)
                        logger.info("_sniper_loop: %s — %s", bid["item_id"], result_str)
        except Exception:
            logger.exception("_sniper_loop: unexpected error, continuing")
        await asyncio.sleep(SNIPER_INTERVAL)


# ---------------------------------------------------------------------------
# Per-snipe outcome watchdog (BUI-604) — READ-ONLY
# ---------------------------------------------------------------------------
# Every window below is a multiple of SYNC_INTERVAL, not a round number of
# minutes: the only thing that resolves a snipe is a completed _sync_gixen
# pass (or the eBay fallback a sync's ENDED write arms), so "how long is too
# long without an outcome" is measured in sync cycles by construction. At the
# 600s default they land at 30/10/40/30 minutes.
#
# _WATCHDOG_PRE_END_WINDOW (3 cycles) — how close to its end a snipe has to be
#   before its absence from Gixen is worth interrupting a human over. Three
#   cycles is the smallest window that still contains at least two independent
#   healthy scrapes after the one-cycle confirmation below, and BUI-562's
#   measurement (a retry within 30s of a Gixen failure recovered 12/12) means a
#   flapping host does NOT stretch a 3-cycle window into fewer observations.
#
# _WATCHDOG_VANISH_CONFIRM (1 cycle) — a vanish stamp younger than one cycle is
#   not yet evidence. _record_vanish_observations clears the stamp the moment
#   the row reappears, so a transient per-row scrape miss heals on the next
#   pass; requiring the stamp to outlive a full cadence is what keeps a single
#   glitch from becoming a banner. This is deliberately NOT "the stamp predates
#   _last_sync_ok_at": the stamping sync sets that same variable a few
#   milliseconds later, so such a check would pass after ONE observation and
#   de-blip nothing.
#
# _WATCHDOG_POST_END_DEADLINE (4 cycles) — how long after its auction ends a
#   snipe may stay PENDING before that silence is the alert. Sized from what
#   the resolution machinery legitimately needs: one cycle to observe the end,
#   one more for the deliberate BUI-417 one-cycle deferral, plus two cycles of
#   slack for the documented flapping (the 30/60/120/240/480s backoff spends
#   ~16 minutes on six consecutive failures).
#
# _WATCHDOG_SYNC_STALE_AFTER (3 cycles) — how old our newest observation may be
#   before the watchdog stops vouching for anything. Strictly LESS than the
#   post-end deadline, and that ordering is load-bearing: it makes "no outcome
#   for 4 cycles" unstateable from observations older than 3, so the watchdog
#   can never blame Gixen for a silence that is really our own sync being dead.
#
# _WATCHDOG_UNKNOWN_END_DEADLINE (4 cycles, BUI-627) — how long a PENDING row
#   may sit with NULL auction_end_at before that absence becomes an alert in
#   its own right, clocked on `added_at` rather than `auction_end_at` (there is
#   no end to measure against). Sized identically to _WATCHDOG_POST_END_DEADLINE
#   and for the same reasons, because the two normal paths that fill an end
#   share its structure: one cycle for the ordinary case (Gixen's own
#   time_to_end string is read on the first sync after add), one more for the
#   BUI-417 one-cycle re-add-safety deferral the vanished-with-NULL-end path
#   uses, plus two cycles of flapping slack (BUI-562: ~16 minutes across six
#   consecutive failures). Fewer cycles would fire on rows still working
#   through that ordinary path; more would leave the BUI-627 hole (ebay-fetch
#   unavailable) unreported for longer than the structurally identical
#   missing_outcome deadline tolerates for "no evidence arrived yet."
_WATCHDOG_PRE_END_WINDOW = timedelta(seconds=3 * SYNC_INTERVAL)
_WATCHDOG_VANISH_CONFIRM = timedelta(seconds=SYNC_INTERVAL)
_WATCHDOG_POST_END_DEADLINE = timedelta(seconds=4 * SYNC_INTERVAL)
_WATCHDOG_SYNC_STALE_AFTER = timedelta(seconds=3 * SYNC_INTERVAL)
_WATCHDOG_UNKNOWN_END_DEADLINE = timedelta(seconds=4 * SYNC_INTERVAL)


def _classify_watchdog_row(
    row: sqlite3.Row, now_dt: datetime, uptime_floor_dt: datetime | None,
) -> tuple[str, dict | None]:
    """Bucket one PENDING bids row. Pure: no DB, no clock, no writes.

    Returns ``(bucket, alert_or_None)`` where bucket is one of:

    ``missing_outcome``
        The auction ended and no terminal status arrived within the deadline.
        THE ticket's alert: the absence of a transition, not an error.
    ``vanished_before_end``
        The auction is still live and inside the pre-end window, but the snipe
        has been absent from a healthy Gixen list for at least one cycle.
    ``unknown_end``
        PENDING with no captured auction_end_at. Neither check can run — a
        NULL-end row is exactly the BUI-85/BUI-417 deferral case, which is
        *supposed* to sit PENDING for a cycle. Reported as context, never as an
        alert; the row becomes judgeable the moment an end is captured.
    ``stale_unknown_end``
        PENDING with no captured auction_end_at AND has stayed that way for at
        least _WATCHDOG_UNKNOWN_END_DEADLINE since `added_at` (BUI-627). The
        BUI-85/BUI-417 deferral above is supposed to resolve within a cycle or
        two; still NULL-end after the deadline means neither the ordinary
        Gixen-scrape path nor the eBay-fallback disambiguation path ever ran to
        completion (commonly: ebay-fetch is unavailable) — THIS check's alert.
    ``ok``
        Live, present on Gixen (or not yet near enough to its end to care).

    `uptime_floor_dt` is when this process started. The overdue clock runs from
    ``max(auction_end_at, uptime_floor)``, so an auction that ended while the
    server was down does not alarm the instant it comes back: the resolution
    machinery gets its full deadline of actual uptime to do its job first.

    The stale_unknown_end clock (below) applies the identical uptime-floor
    fairness to `added_at` instead, for the identical reason: the sync loop has
    to be UP to ever fill an end, so a NULL-end row that predates a restart
    gets the restart's full uptime before its silence counts against it.
    """
    end_dt = _parse_iso_utc(row["auction_end_at"])
    if end_dt is None:
        # BUI-627: clock the NULL-end row on `added_at` — the only timestamp it
        # has — rather than on auction_end_at, which is exactly what's missing.
        added_dt = _parse_iso_utc(row["added_at"])
        if added_dt is None:
            return "unknown_end", None  # can't clock it — fail open, no alarm
        stale_from = added_dt
        if uptime_floor_dt is not None and uptime_floor_dt > stale_from:
            stale_from = uptime_floor_dt
        if now_dt - stale_from < _WATCHDOG_UNKNOWN_END_DEADLINE:
            return "unknown_end", None
        seconds_since_added = int((now_dt - added_dt).total_seconds())
        return "stale_unknown_end", {
            "bid_id": row["id"],
            "item_id": row["item_id"],
            "title": row["ebay_title"],
            "auction_end_at": row["auction_end_at"],
            "local_snipe_result": row["local_snipe_result"],
            "kind": "stale_unknown_end",
            "added_at": row["added_at"],
            "seconds_since_added": seconds_since_added,
            "detail": (
                f"added {seconds_since_added // 60}m ago and still has no "
                "captured auction end — ebay-fetch may be unavailable, or the "
                "snipe vanished from Gixen before any end time was ever "
                "observed. Check the listing on eBay directly."
            ),
        }
    base = {
        "bid_id": row["id"],
        "item_id": row["item_id"],
        "title": row["ebay_title"],
        "auction_end_at": row["auction_end_at"],
        "local_snipe_result": row["local_snipe_result"],
    }

    if end_dt > now_dt:
        vanished_dt = _parse_iso_utc(row["gixen_vanished_at"])
        if (
            vanished_dt is not None
            and now_dt >= end_dt - _WATCHDOG_PRE_END_WINDOW
            and now_dt - vanished_dt >= _WATCHDOG_VANISH_CONFIRM
        ):
            return "vanished_before_end", {
                **base,
                "kind": "vanished_before_end",
                "gixen_vanished_at": row["gixen_vanished_at"],
                "seconds_to_end": int((end_dt - now_dt).total_seconds()),
                # Worded as evidence, not as a conclusion: what we actually
                # know is that more than one healthy scrape has failed to list
                # it. Telling a human "Gixen will not fire this" would invite
                # them to tear down a snipe that a scrape hiccup only made look
                # gone — the reverse of the mistake this is here to prevent.
                "detail": (
                    "absent from Gixen's list since "
                    f"{row['gixen_vanished_at']} (more than one sync cycle) "
                    "while its auction is still live. Gixen has nothing left "
                    "to fire; the local backup bidder is now the only thing "
                    "that will bid, and it bids off this PENDING row even if "
                    "the snipe was cancelled on Gixen deliberately."
                ),
            }
        return "ok", None

    # Ended. The overdue clock starts at the later of the auction end and this
    # process's start (see the docstring) — never earlier than we could have
    # observed anything.
    overdue_from = end_dt
    if uptime_floor_dt is not None and uptime_floor_dt > overdue_from:
        overdue_from = uptime_floor_dt
    if now_dt - overdue_from <= _WATCHDOG_POST_END_DEADLINE:
        return "ok", None
    overdue_s = int((now_dt - end_dt).total_seconds())
    return "missing_outcome", {
        **base,
        "kind": "missing_outcome",
        "seconds_overdue": overdue_s,
        "detail": (
            f"auction ended {overdue_s // 60}m ago and the snipe is still "
            "PENDING — no WON/LOST/ENDED/FAILED ever arrived. Check the "
            "listing on eBay before assuming it was lost."
        ),
    }


def _build_snipe_watchdog_report(
    conn: sqlite3.Connection,
    now_dt: datetime,
    *,
    process_started_at: float,
    last_sync_ok_at: float,
    last_sync_snipe_count: int | None,
) -> dict:
    """The BUI-604 watchdog verdict. READ-ONLY, by construction and on purpose.

    This function issues exactly one statement against the DB — a SELECT — and
    returns a dict derived from it. It writes no row, sets no status, opens no
    write_transaction, takes no lock, and triggers no Gixen scrape (in
    particular it must never call _ensure_fresh_sync/_spawn_fallback_task: an
    observer that perturbs the thing it observes is not an observer, and a
    dashboard poll must not be able to schedule work on the money path).

    That is the entire safety argument, and it is why this ticket is allowed to
    exist next to the eBay WON-inference:

      * The alert is a PURE FUNCTION of current state. Nothing is stored, so
        nothing can go stale, be missed on restart, or need acknowledging — and
        an alert clears by the state changing, i.e. the moment the snipe
        resolves, reappears on Gixen, or is removed. There is no dismiss button
        to leave a real alarm permanently silenced.
      * The candidate set is `status = 'PENDING'` only. Terminal rows are
        excluded because they HAVE their outcome; the REMOVED/PURGED tombstones
        are excluded by the same clause — a tombstone is not a terminal auction
        outcome (BUI-49), and it is equally not a missing one, so it must
        neither be counted as resolved evidence nor alerted on. No
        TOMBSTONE_STATUSES_SQL filter is needed to get that right; it falls out.
      * Nothing here can flip a row out of PENDING, so it cannot rob the local
        sniper of a real bid (get_bids_ready_to_snipe fires on PENDING rows) nor
        expose a stray one.

    Blind spot, stated rather than hidden: _record_vanish_observations only
    stamps gixen_vanished_at when Gixen returned a NON-empty list (BUI-85's
    anti-glitch guard), so a scrape in which EVERY snipe disappeared at once
    stamps nothing and the pre-end check stays silent. That case degrades to
    the post-end `missing_outcome` check, which does not depend on the stamp.
    `sync.last_snipe_count` is reported so a human can see a zero-snipe scrape
    for what it is.
    """
    sync_age = None if last_sync_ok_at <= 0 else now_dt.timestamp() - last_sync_ok_at
    if sync_age is None:
        sync_state = "never"
    elif sync_age > _WATCHDOG_SYNC_STALE_AFTER.total_seconds():
        sync_state = "stale"
    else:
        sync_state = "ok"

    uptime_floor_dt = (
        datetime.fromtimestamp(process_started_at, timezone.utc)
        if process_started_at > 0
        else None
    )

    # The one statement this function issues. `WHERE status = 'PENDING'` is
    # served by the BUI-67 partial unique index (bids(item_id) WHERE
    # status='PENDING'), so this scans the live snipes, NOT the never-pruned
    # bids history table — the cost BUI-418 moved a full get_all_bids() scan
    # out of the write lock to avoid. Verified via EXPLAIN QUERY PLAN:
    # "SCAN bids USING INDEX idx_bids_pending_item_id".
    #
    # `added_at` (BUI-627) is the one existing-line touch this ticket makes to
    # this query — it's the clock the new stale_unknown_end check runs on, and
    # it's a column already on every bids row (DEFAULT (datetime('now')),
    # never NULL on insert), so adding it costs nothing extra: still one
    # statement, same index, same scan.
    rows = conn.execute(
        "SELECT id, item_id, ebay_title, auction_end_at, gixen_vanished_at, "
        "local_snipe_result, added_at FROM bids WHERE status = 'PENDING'"
    ).fetchall()

    alerts: list[dict] = []
    # BUI-627 seeds `stale_unknown_end` here rather than letting the
    # setdefault below conjure it, because a counter that only APPEARS once it
    # is non-zero cannot distinguish "checked, found none" from "this build
    # doesn't have the check" — absence-as-zero is the same fails-green shape
    # BUI-602's heartbeat_report avoids by iterating its contract instead of
    # its stored rows. The ticket's own evidence was read this way (`curl
    # /api/snipe-watchdog | jq .counts` reporting `unknown_end: 0`), so the key
    # has to be there when the answer is zero. Touching this line is safe: it
    # is a counter in the read-only report, nowhere near the eBay WON-inference
    # the zero-modification bar exists to protect.
    counts = {
        "ok": 0,
        "unknown_end": 0,
        "vanished_before_end": 0,
        "missing_outcome": 0,
        "stale_unknown_end": 0,
    }
    for row in rows:
        bucket, alert = _classify_watchdog_row(row, now_dt, uptime_floor_dt)
        # Kept as a guard for any FUTURE bucket added to _classify_watchdog_row
        # without a matching seed above — a KeyError here would take down the
        # whole report over a counter.
        counts.setdefault(bucket, 0)
        counts[bucket] += 1
        if alert is not None:
            alerts.append(alert)
    alerts.sort(key=lambda a: (a["kind"], a["item_id"]))

    # A watchdog that reports on snipes using observations it knows are stale
    # is the fails-green bug it exists to prevent, so when our own sync is not
    # healthy the per-snipe alerts are withheld and replaced by ONE alert about
    # the sync. Their count is still reported (never silently dropped) so the
    # information is de-escalated, not hidden.
    suppressed = 0
    if sync_state != "ok":
        suppressed = len(alerts)
        alerts = [{
            "kind": "sync_stale",
            "detail": (
                "the comics server has not completed a Gixen sync "
                + ("at all since it started" if sync_state == "never"
                   else f"in {int(sync_age // 60)}m")
                + " — snipe outcomes cannot be vouched for until it does."
            ),
        }]

    return {
        "generated_at": now_dt.isoformat(),
        "healthy": not alerts,
        "alerts": alerts,
        "counts": counts,
        "suppressed_alert_count": suppressed,
        "pending_watched": len(rows),
        "sync": {
            "state": sync_state,
            "last_ok_at": (
                None if last_sync_ok_at <= 0
                else datetime.fromtimestamp(last_sync_ok_at, timezone.utc).isoformat()
            ),
            "age_seconds": None if sync_age is None else int(sync_age),
            "stale_after_seconds": int(_WATCHDOG_SYNC_STALE_AFTER.total_seconds()),
            "last_snipe_count": last_sync_snipe_count,
        },
        "windows": {
            "sync_interval_seconds": SYNC_INTERVAL,
            "pre_end_window_seconds": int(_WATCHDOG_PRE_END_WINDOW.total_seconds()),
            "vanish_confirm_seconds": int(_WATCHDOG_VANISH_CONFIRM.total_seconds()),
            "post_end_deadline_seconds": int(_WATCHDOG_POST_END_DEADLINE.total_seconds()),
            "unknown_end_deadline_seconds": int(_WATCHDOG_UNKNOWN_END_DEADLINE.total_seconds()),
        },
    }


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _ensure_fresh_sync() -> None:
    """Pull latest state from Gixen if our last pull was older than _SYNC_TTL.

    Called at the top of /api/snipes. Concurrent dashboard loads share one
    in-flight Gixen scrape via _sync_lock, then return immediately if the
    just-completed pull is still fresh enough.
    """
    global _last_sync_at
    if not _sync_lock or not _api_lock:
        return

    async with _sync_lock:
        now_ts = datetime.now(timezone.utc).timestamp()
        if now_ts - _last_sync_at < _SYNC_TTL:
            return

        db = _get_db()
        try:
            async with _api_lock:
                await _sync_gixen(db, _api_client)
        except Exception:
            # BUI-410 (Stage 3 landed): the BUI-391 `db.rollback()` here is
            # retired — _sync_gixen no longer writes the shared `_db` (all DML
            # goes through its own write_transaction(), the gather phase only
            # reads), so there is no stranded batch to roll back. Superseded
            # per-caller rollback convention — see
            # docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md.
            logger.exception("_ensure_fresh_sync: gixen pull failed")
            return
        _last_sync_at = datetime.now(timezone.utc).timestamp()


def _spawn_fallback_task() -> None:
    """Schedule _run_ebay_fallback as a tracked task. The function itself
    short-circuits if a fallback is already running or the cooldown is
    active, so it's safe to fire on every dashboard load. Tracking the
    reference here lets lifespan teardown cancel + await it cleanly."""
    global _ebay_fallback_task
    if _ebay_fallback_task is not None and not _ebay_fallback_task.done():
        return  # one already in flight; let it finish
    _ebay_fallback_task = asyncio.create_task(_run_ebay_fallback())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _db_path, _api_client, _sync_client, _api_lock, _sync_lock, _write_lock, _ebay_fallback_lock, _bidder
    global _process_started_at, _last_sync_ok_at, _last_sync_snipe_count
    # BUI-604: the watchdog's uptime floor. Reset (not merely initialized) here
    # so a restart genuinely restarts the grace window and does not inherit a
    # previous run's "we synced fine" stamp from the same interpreter — which is
    # exactly what a test suite booting the app twice would otherwise hand it.
    _process_started_at = datetime.now(timezone.utc).timestamp()
    _last_sync_ok_at = 0.0
    _last_sync_snipe_count = None
    if env_file := os.getenv("ENV_FILE"):
        load_dotenv(env_file)
    # Resolve the eBay fallback binary now that the .env (EBAY_FETCH_BIN, PATH)
    # is loaded — a missing script silently disables ENDED-auction winning-bid
    # capture, so log it loudly once at startup (BUI-66).
    if _ebay_fetch_bin() is None:
        logger.warning(
            "ebay-fetch console script not found (EBAY_FETCH_BIN=%r, PATH lookup failed) "
            "— live eBay fallback disabled. Install apps/ebay via scripts/install.sh, "
            "or set EBAY_FETCH_BIN to its absolute path in the server .env.",
            os.getenv("EBAY_FETCH_BIN", "ebay-fetch"),
        )
    db_path = Path(os.getenv("DB_PATH", str(DB_PATH)))
    _db = init_db(db_path)
    _db_path = db_path  # BUI-408: write_transaction() callers resolve through this
    app.state.db = _db
    _api_client = GixenClient()
    _api_lock = asyncio.Lock()
    _sync_lock = asyncio.Lock()
    _write_lock = asyncio.Lock()
    _ebay_fallback_lock = asyncio.Lock()

    # Plugin loading: discover entry-point plugins, then fire startup hooks.
    # Helpers live in gixen/plugins.py (PER-26 M-01); they accept an injected
    # logger so log records appear under the "server.main" logger name that
    # PER-25 regression tests assert on.
    pm = load_plugins()
    app.state.plugin_manager = pm
    _invoke_db_tables_isolated(pm, _db, logger=logger)
    _invoke_register_routes(pm, app, logger=logger)
    app.state.dashboard_tabs = _collect_dashboard_tabs(pm, logger=logger)

    # BUI-257 invariant: the only background tasks started here are the eBay
    # fallback (fire-and-forget, spawned on demand via _spawn_fallback_task),
    # this Gixen snipe-sync loop (_sync_loop, gated by GIXEN_SYNC_ENABLED), and
    # the sniper loop below — all Gixen/eBay, never LOCG. There is intentionally
    # NO automatic/background LOCG access anywhere in this server: LOCG is
    # programmatically inaccessible, and the only path to it is the manual,
    # user-invoked /comic:collection-sync skill (see locg-cli's client.py and
    # collection_io.py).
    sync_task = None
    sniper_task = None
    if os.getenv("GIXEN_SYNC_ENABLED", "true") != "false":
        # Separate client so the loop's long scrape doesn't fight _api_lock.
        _sync_client = GixenClient()
        sync_task = asyncio.create_task(_sync_loop())
    if os.getenv("LOCAL_SNIPER_ENABLED", "true") != "false":
        _bidder = ebay_bidder.EbayBidder()
        await _bidder.start()
        sniper_task = asyncio.create_task(_sniper_loop())

    yield

    # Cancel + await any in-flight eBay fallback so its DB writes complete (or
    # cleanly abort) before we close the connection. Bounded await — if the
    # task is wedged on a slow eBay call we don't want to block shutdown.
    if _ebay_fallback_task is not None and not _ebay_fallback_task.done():
        _ebay_fallback_task.cancel()
        try:
            await asyncio.wait_for(_ebay_fallback_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as e:  # noqa: BLE001  # lifespan shutdown — log any stray error from background task
            logger.warning("lifespan: fallback task raised on cancel: %s", e)

    if sniper_task:
        sniper_task.cancel()
    if _bidder:
        await _bidder.stop()
    if sync_task:
        sync_task.cancel()

    row = _db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row and row[0]:
        logger.warning("WAL checkpoint incomplete: busy=%s", row[0])
    _db.close()
    app.state.db = None


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TabSpec(BaseModel):
    label: str
    path: str


class AddBidRequest(BaseModel):
    model_config = {"extra": "ignore"}

    item_id: str
    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0
    # BUI-78: optional seller + grades captured by the buy flow at add time.
    seller: str | None = None
    seller_grade: float | None = None
    photo_grade: float | None = None
    # BUI-618 (U6): which caller made this write (cli/batch/dashboard),
    # recorded verbatim on the bid_decisions ledger row for this request.
    # Optional and unvalidated here — U7 (a later wave) populates it from the
    # CLI and add-batch; an old client that omits it just gets a NULL source.
    source: str | None = None
    # BUI-619 (U5): comic identity carried at add time — `{comic_id|locg_id,
    # grade}` per entry — so pre-trade FMV-aware checks (U4) see it before
    # the Gixen call, instead of only learning it from the post-add
    # link-fmv call. List-capable for a future lot caller (KTD8); every
    # caller today sends 0 or 1 entries. Optional with an empty-list
    # default (Pydantic v2 deep-copies a bare mutable default per instance,
    # so this needs no Field(default_factory=...)): an old CLI that omits
    # this field is byte-identical to one that sends `comic_identities: []`
    # — both land on PolicyIntent's own empty-list default, so nothing
    # fires. Not validated further here — an inner dict missing grade/
    # comic_id/locg_id degrades to "no link resolved" downstream (the
    # overlay's on_bid_write_committed), never a 422 on the money path.
    comic_identities: list[dict] = []
    # BUI-623 (U9): the audited bypass — `gixen add --ack-policy` sets this.
    # Default False means an old client (or a new one that never passes
    # --ack-policy) is byte-identical to v1: with every POLICY_BLOCK_* flag
    # also off by default, `evaluate_block` never blocks anything, so this
    # field being present-but-false changes nothing. When True, it suppresses
    # a block that would otherwise fire for THIS request only (per-
    # invocation, and blanket across every blocking check that fired) — the
    # commit still records `bypass=1` and the full advisory set on the
    # ledger row, so what was overridden is always reconstructable later.
    policy_bypass: bool = False

    @field_validator("item_id")
    @classmethod
    def item_id_numeric(cls, v: str) -> str:
        if not re.match(r"^\d+$", v):
            raise ValueError("item_id must be numeric")
        return v

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v

    @field_validator("seller")
    @classmethod
    def normalize_seller(cls, v: str | None) -> str | None:
        # BUI-78: canonical key = lowercased eBay username. Normalize once here so
        # the write key matches the read endpoint (which lowercases too) and the
        # 1-128 char bound is enforced on both sides. Empty/whitespace -> NULL.
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 128:
            raise ValueError("seller must be 1-128 characters")
        return v.lower()


class EditBidRequest(BaseModel):
    model_config = {"extra": "ignore"}

    max_bid: float
    # BUI-401: None means "leave bid_offset unchanged" (passthrough) — same
    # latent bug snipe_group had pre-BUI-392: a max_bid-only PATCH that omits
    # this field must not silently reset a tuned fire-offset back to 6. See
    # update_bid's passthrough and this route's Gixen-side resolution below.
    bid_offset: int | None = None
    # BUI-392: None means "leave snipe_group unchanged" (passthrough) — a
    # max_bid-only PATCH that omits this field must not silently un-group the
    # snipe. Explicit 0 still means "un-group". See update_bid's None branch
    # and this route's Gixen-side resolution below for the two halves of the
    # fix (local DB state vs. the live Gixen snipe).
    snipe_group: int | None = None
    # BUI-618 (U6): same optional provenance tag as AddBidRequest.source —
    # see its docstring.
    source: str | None = None
    # BUI-623 (U9): the audited bypass — `gixen edit --ack-policy` sets
    # this. Same semantics as AddBidRequest.policy_bypass above (both
    # request models gained this field together, per the plan).
    policy_bypass: bool = False

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v


class PurgeRequest(BaseModel):
    model_config = {"extra": "ignore"}

    sibling_ids: list[str] = []

    @field_validator("sibling_ids")
    @classmethod
    def validate_sibling_ids(cls, v: list[str]) -> list[str]:
        for item_id in v:
            if not re.match(r"^\d+$", item_id):
                raise ValueError(f"sibling_ids contains non-numeric value: {item_id}")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# Force browsers to revalidate static files on every load. Without this, a fix
# pushed to the dashboard HTML/CSS can sit invisible behind heuristic caching
# until the user knows to hard-reload. The dashboard is small and fetched
# rarely; the cost of revalidation is negligible.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/")
def root(request: Request):
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    tabs = getattr(request.app.state, "dashboard_tabs", [])
    if tabs:
        tab_links = "".join(
            f'  <a class="seg nav" href="{t["path"]}">{t["label"]}</a>\n'
            for t in tabs
        )
        html = html.replace('  <div class="spacer"></div>', f'{tab_links}  <div class="spacer"></div>', 1)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers=dict(_NO_CACHE_HEADERS))


@app.get("/v2/bids")
def variant_v2_bids():
    return FileResponse(
        Path(__file__).parent / "static" / "v2-bids.html",
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/static/v2.css")
def static_v2_css():
    return FileResponse(
        Path(__file__).parent / "static" / "v2.css",
        media_type="text/css",
        headers=_NO_CACHE_HEADERS,
    )


def _resolve_git_sha() -> str:
    """BUI-612: SHA attestation for the comics server.

    apps/fmv (BUI-305), apps/ebay (BUI-314), and packages/locg-cli
    (BUI-612) each stamp their git SHA into the wheel at BUILD time (see
    their respective hatch_build.py) because their console scripts are
    `uv tool install`ed hatchling packages, where that stamp survives
    reliably. The comics server instead runs from the editable *workspace*
    `.venv` (source, not a frozen wheel — see BUI-377), so it reads git
    HEAD directly at process startup rather than needing a build-time
    stamp. packages/gixen-cli/cli.py also reads git HEAD at runtime rather
    than via a build-time stamp, for a related but distinct reason: it's
    always `uv tool install --editable`ed, and setuptools' editable
    strategy (unlike hatchling's) has no mechanism to materialize a
    separately-generated file into site-packages, so a build-time stamp
    module would never be reachable there. Computed once at import time
    (not per-request): a redeploy restarts this process via `launchctl
    kickstart` (scripts/deploy.sh), which re-imports the module and
    re-resolves HEAD. Falls back to "unknown" if `git` is missing or this
    isn't a git checkout, rather than failing server startup over a
    diagnostics field.
    """
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return output or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


_SERVER_GIT_SHA = _resolve_git_sha()


@app.get("/health")
async def health():
    return {"status": "ok", "git_sha": _SERVER_GIT_SHA}


@app.get("/api/dashboard-tabs", response_model=list[TabSpec])
def api_dashboard_tabs(request: Request) -> list[dict]:
    return getattr(request.app.state, "dashboard_tabs", [])


async def _modify_and_update_bid(
    item_id: str, max_bid: float,
    bid_offset: int, snipe_group: int,
    seller: str | None = None, seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> sqlite3.Row:
    """Gixen modify_snipe (off-thread) + local update_bid. Re-raises
    GixenSnipeNotFoundError so the caller owns the not-found *policy* (add falls
    back; edit 404s). Caller must already hold _api_lock — this does NOT acquire
    it, so the lookup→Gixen→DB-write sequence stays atomic (BUI-67 KTD6/KTD7).

    BUI-408: no longer takes a `db` parameter — every read/write below now
    happens on `wconn` inside the write_transaction() block (see the
    read-after-write staleness note there), so the shared `_db` connection
    has no legitimate use left in this function.
    """
    await asyncio.to_thread(
        _api_client.modify_snipe,
        item_id, Decimal(str(max_bid)),
        bid_offset=bid_offset, snipe_group=snipe_group,
    )
    # BUI-408: both writes are await-free once we get here (the network call
    # above already completed), so they land as ONE write_transaction() under
    # _write_lock instead of two separate commits on the shared _db. The
    # read-back also happens on `wconn`, INSIDE the block — not on the
    # shared `db` afterward: `db` can have its own open transaction from an
    # unrelated in-flight _sync_gixen cycle (commit-free DML, then an await,
    # per its own design), which pins `db` to a snapshot that predates this
    # commit — a `db` read right after would risk returning stale/missing
    # data for the row this function JUST wrote. `wconn` always sees its own
    # writes regardless of `db`'s transaction state.
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            update_bid(wconn, item_id, max_bid, bid_offset, snipe_group)
            # BUI-78 C2: fill any NULL seller/grade columns from this request
            # without overwriting values a prior add already set.
            update_bid_grades(wconn, item_id, seller=seller, seller_grade=seller_grade,
                              photo_grade=photo_grade)
            return get_pending_bid_by_item_id(wconn, item_id) or get_bid_by_item_id(wconn, item_id)


async def _add_bid_row(
    item_id: str, max_bid: float,
    bid_offset: int, snipe_group: int,
    seller: str | None = None, seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> tuple[sqlite3.Row, bool]:
    """Gixen add_snipe (off-thread) + insert_bid; returns (row, created=True).

    On a partial-unique-index collision — a racing unlocked _sync_loop insert for
    the same item landed first (BUI-67 KTD6) — recover by updating the existing
    live row and return (row, created=False) instead of 500. Caller holds
    _api_lock.

    BUI-408: no longer takes a `db` parameter — same reasoning as
    _modify_and_update_bid's docstring.
    """
    await asyncio.to_thread(
        _api_client.add_snipe,
        item_id, Decimal(str(max_bid)),
        bid_offset=bid_offset, snipe_group=snipe_group,
    )
    # BUI-408: both the insert and the integrity-recovery update are
    # await-free once we get here — each lands on its own short-lived
    # write_transaction() under _write_lock instead of the shared _db. A
    # failed insert's write_transaction() rolls back and closes wconn on its
    # own (see write_transaction's docstring), so no explicit db.rollback()
    # is needed here anymore — the failed attempt never touched _db at all.
    #
    # Read-back also happens on `wconn`, INSIDE each block — not on the
    # shared `db` afterward: `db` can have its own open transaction from an
    # unrelated in-flight _sync_gixen cycle (commit-free DML, then an await,
    # per its own design), which pins `db` to a snapshot that predates this
    # commit — a `db` read right after would risk returning stale/missing
    # data (even a None where a row was just inserted) for the row this
    # function JUST wrote. `wconn` always sees its own writes regardless of
    # `db`'s transaction state.
    try:
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                bid_id = insert_bid(
                    wconn, item_id=item_id, max_bid=max_bid,
                    bid_offset=bid_offset, snipe_group=snipe_group, seller=seller,
                    seller_grade=seller_grade, photo_grade=photo_grade,
                )
                return wconn.execute(
                    "SELECT * FROM bids WHERE id=?", (bid_id,)
                ).fetchone(), True
    except sqlite3.IntegrityError:
        # A racing unlocked _sync_loop insert for the same item won the
        # partial unique index first (BUI-67 KTD6) — recover in a fresh
        # write_transaction().
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                update_bid(wconn, item_id, max_bid, bid_offset, snipe_group)
                # BUI-78 C2: a racing sync insert won the row; still fill its
                # NULL grades.
                update_bid_grades(wconn, item_id, seller=seller, seller_grade=seller_grade,
                                  photo_grade=photo_grade)
                row = get_pending_bid_by_item_id(wconn, item_id) or get_bid_by_item_id(wconn, item_id)
                return row, False


@app.post("/api/bids")
async def api_add_bid(req: AddBidRequest):
    db = _get_db()
    # BUI-78: req.seller is already normalized (lowercased, validated) by
    # AddBidRequest.normalize_seller.
    seller = req.seller
    # Populated inside the lock below, before any Gixen call can raise — see
    # the outer except clauses, which read these back for the gixen_failed
    # ledger row. Defaults cover the (unreachable in practice) case where an
    # exception lands before they're assigned.
    existing = None
    intent: PolicyIntent | None = None
    advisories: list[dict] = []
    check_results: list[dict] = []
    try:
        # Lookup + Gixen call + DB write all under _api_lock so the add/modify
        # decision is atomic against other request handlers (BUI-67 KTD6). The
        # unlocked background _sync_loop is the remaining concurrent writer; the
        # partial unique index (+ _add_bid_row's recovery) guards that race.
        async with _api_lock:
            existing = get_pending_bid_by_item_id(db, req.item_id)
            # BUI-615/616: one check-point evaluation per request, BEFORE any
            # Gixen call — the same advisories apply to whichever branch below
            # this request ultimately returns through (KTD1). AE6: trigger is
            # "upsert" (not "create") when a live row already exists, and
            # target_max_bid is always req.max_bid — the NEW amount — so an
            # upsert-modify of a live item is checked against the new value,
            # never the stale existing one.
            intent = PolicyIntent(
                item_id=req.item_id,
                target_max_bid=req.max_bid,
                snipe_group=req.snipe_group,
                trigger="upsert" if existing is not None else "create",
                prior_row=existing,
                # BUI-619 (U5): POST-only — api_edit_bid's PolicyIntent below
                # leaves this at PolicyIntent's own [] default; PATCH resolves
                # identity from the bid's existing FMV links instead (U4).
                comic_identities=req.comic_identities,
            )
            advisories, check_results = run_checks(db, intent, app.state.plugin_manager)

            # BUI-623 (U9): the blocking verdict for THIS evaluation, decided
            # right after run_checks and BEFORE any Gixen call below — still
            # inside _api_lock. `req.policy_bypass` never suppresses
            # evaluation itself, only the block; the full advisory set still
            # lands in the ledger either way. When blocked, this returns
            # (via the 409 raise) without ever reaching _modify_and_update_
            # bid/_add_bid_row — no Gixen call, no bid-row change.
            block_decision = evaluate_block(
                check_results, advisories, bypass=req.policy_bypass,
            )
            if block_decision.blocked:
                # R14/U9: an upsert-modify of a live row must not read as
                # "no money committed" — name the surviving snipe and its
                # current max_bid when one exists (None for a genuine create).
                surviving_snipe = (
                    {"item_id": existing["item_id"], "max_bid": existing["max_bid"]}
                    if existing is not None else None
                )
                detail = build_block_detail(
                    block_decision, advisories, surviving_snipe=surviving_snipe,
                )
                await _append_bid_decision(
                    item_id=req.item_id, trigger=intent.trigger,
                    outcome=BID_DECISION_OUTCOME_BLOCKED,
                    bid_row_id=(existing["id"] if existing is not None else None),
                    requested_max_bid=req.max_bid,
                    check_results=check_results, advisories=advisories,
                    source=req.source, bypass=False,
                )
                raise HTTPException(status_code=409, detail=detail)

            # BUI-617/618 (U3/U6): one helper per outcome class, defined here
            # so every branch below records the SAME shape instead of each
            # branch hand-rolling its own call (the duplicated-predicates-
            # drift lesson U1's approach already applied to run_checks
            # itself). Closures capture req/db/intent/check_results/
            # advisories from this call's own scope.
            async def _record_committed(bid_row_id: int) -> None:
                # Notification fires ONLY on a genuine committed write — the
                # overlay's on_bid_write_committed persists state (a future
                # wave's FMV link) that only makes sense once a row exists.
                # Synchronous (KTD1: same-thread DB reads, like the checks) —
                # no await.
                notify_bid_write_committed(
                    app.state.plugin_manager, db, intent, bid_row_id, check_results,
                )
                await _append_bid_decision(
                    item_id=req.item_id, trigger=intent.trigger,
                    outcome=BID_DECISION_OUTCOME_COMMITTED,
                    bid_row_id=bid_row_id, requested_max_bid=req.max_bid,
                    check_results=check_results, advisories=advisories,
                    source=req.source, bypass=req.policy_bypass,
                )

            async def _record_unconfirmed(bid_row_id: int | None) -> None:
                await _append_bid_decision(
                    item_id=req.item_id, trigger=intent.trigger,
                    outcome=BID_DECISION_OUTCOME_UNCONFIRMED,
                    bid_row_id=bid_row_id, requested_max_bid=req.max_bid,
                    check_results=check_results, advisories=advisories,
                    source=req.source, bypass=req.policy_bypass,
                )

            if existing is not None:
                # A live snipe exists → update in place. Gixen rejects a re-add of
                # an already-sniped item (code 202), so modify, not add.
                try:
                    row = await _modify_and_update_bid(
                        req.item_id, req.max_bid, req.bid_offset, req.snipe_group,
                        seller=seller, seller_grade=req.seller_grade,
                        photo_grade=req.photo_grade,
                    )
                    await _record_committed(row["id"])
                    return {**dict(row), "created": False, "advisories": advisories}
                except GixenSnipeNotFoundError:
                    # DB has a live row but Gixen lost it (state skew). Intent is
                    # "add" → fall back. If Gixen can't confirm the add, keep the
                    # existing row visible rather than a bare 503 that hides it.
                    try:
                        row, created = await _add_bid_row(
                            req.item_id, req.max_bid, req.bid_offset, req.snipe_group,
                            seller=seller, seller_grade=req.seller_grade,
                            photo_grade=req.photo_grade,
                        )
                        await _record_committed(row["id"])
                        return {**dict(row), "created": created, "advisories": advisories}
                    except GixenAddNotConfirmedError:
                        await _record_unconfirmed(existing["id"])
                        return {
                            **dict(existing), "created": False, "applied": False,
                            "advisories": advisories,
                        }

            row, created = await _add_bid_row(
                req.item_id, req.max_bid, req.bid_offset, req.snipe_group,
                seller=seller, seller_grade=req.seller_grade,
                photo_grade=req.photo_grade,
            )
            await _record_committed(row["id"])
            return {**dict(row), "created": created, "advisories": advisories}
    except GixenError as e:
        # No row landed from THIS request — `existing` (possibly None) is
        # whatever was already there before we tried. BUI-618: a create that
        # Gixen rejected outright has no bid row at all (bid_row_id=None);
        # an upsert whose modify raised a non-GixenSnipeNotFoundError
        # GixenError leaves the existing row exactly as it was.
        await _append_bid_decision(
            item_id=req.item_id,
            trigger=(intent.trigger if intent is not None else "create"),
            outcome=BID_DECISION_OUTCOME_GIXEN_FAILED,
            bid_row_id=(existing["id"] if existing is not None else None),
            requested_max_bid=req.max_bid,
            check_results=check_results, advisories=advisories,
            source=req.source, bypass=req.policy_bypass,
        )
        raise HTTPException(status_code=503, detail=str(e)) from e
    except requests.HTTPError as e:
        await _append_bid_decision(
            item_id=req.item_id,
            trigger=(intent.trigger if intent is not None else "create"),
            outcome=BID_DECISION_OUTCOME_GIXEN_FAILED,
            bid_row_id=(existing["id"] if existing is not None else None),
            requested_max_bid=req.max_bid,
            check_results=check_results, advisories=advisories,
            source=req.source, bypass=req.policy_bypass,
        )
        raise HTTPException(status_code=503, detail=f"Gixen HTTP error: {e}") from e


def _serialize_snipe_row(item: dict) -> dict:
    """Shared row shape for /api/snipes and /api/history (BUI-273). The two
    endpoints differ only in their WHERE filter — this is the parity surface
    that BUI-50 drifted on, so keep it as the single source of truth.
    """
    end_date_iso = item.get("auction_end_at")
    return {
        "item_id": item["item_id"],
        "title": item.get("ebay_title") or None,
        "current_bid": item.get("cached_current_bid"),
        "max_bid": f"{item['max_bid']:.2f} USD",
        "bid_offset": item["bid_offset"],
        "snipe_group": item["snipe_group"],
        "time_to_end": iso_to_relative(end_date_iso),
        "end_date_iso": end_date_iso,
        "status": item["status"],
        "status_mirror": item.get("status_mirror"),
        "winning_bid": item.get("winning_bid"),
        "seller": item.get("seller"),
        "cached_at": item.get("cached_at"),
        "local_snipe_at": item.get("local_snipe_at"),
        "local_snipe_result": item.get("local_snipe_result"),
    }


@app.get("/api/snipes")
async def api_get_snipes():
    """Pull-on-visit. Synchronously refreshes from Gixen (deduped within
    _SYNC_TTL across concurrent calls), then returns cached DB rows. eBay is
    invoked only as a fire-and-forget fallback for ended bids that never got
    a winning_bid captured — never blocks this response.
    """
    await _ensure_fresh_sync()
    _spawn_fallback_task()

    db = _get_db()

    rows = db.execute(f"""
        SELECT * FROM bids
        WHERE status NOT IN ({TOMBSTONE_STATUSES_SQL})
        ORDER BY added_at DESC
    """).fetchall()

    return [_serialize_snipe_row(dict(row)) for row in rows]


@app.get("/api/history")
async def api_get_history():
    """Recently ended bids from the DB (past 7 days), including removed
    (REMOVED/PURGED) rows. Pure DB read — no Gixen sync.
    """
    db = _get_db()
    rows = db.execute("""
        SELECT b.* FROM bids b
        INNER JOIN (
            SELECT item_id, MAX(id) AS max_id
            FROM bids
            WHERE (
              auction_end_at IS NOT NULL
              AND datetime(auction_end_at) <= datetime('now')
              AND datetime(auction_end_at) >= datetime('now', '-7 days')
            ) OR (
              auction_end_at IS NULL
              AND resolved_at IS NOT NULL
              AND datetime(resolved_at) >= datetime('now', '-7 days')
            )
            GROUP BY item_id
        ) latest ON b.id = latest.max_id
        ORDER BY COALESCE(b.auction_end_at, b.resolved_at) DESC
    """).fetchall()

    return [_serialize_snipe_row(dict(row)) for row in rows]


@app.get("/api/snipe-watchdog")
async def api_snipe_watchdog():
    """BUI-604: which PENDING snipes are missing an outcome they should have.

    Pure DB read — deliberately no _ensure_fresh_sync() and no
    _spawn_fallback_task(), unlike /api/snipes. This endpoint observes the
    snipe lifecycle; it must not drive it. See
    _build_snipe_watchdog_report()'s docstring for the full read-only argument.
    """
    return _build_snipe_watchdog_report(
        _get_db(),
        datetime.now(timezone.utc),
        process_started_at=_process_started_at,
        last_sync_ok_at=_last_sync_ok_at,
        last_sync_snipe_count=_last_sync_snipe_count,
    )


@app.get("/api/bids")
async def api_get_all_bids(item_id: str | None = None, snipe_group: int | None = None):
    """All bids from the DB, newest first. Pure DB read — no Gixen sync.

    BUI-394: optional ?item_id= and/or ?snipe_group= filter the returned
    slice (mirroring /api/group-wins' filter semantics) so an agent can pull
    a correlated slice instead of the whole table when tracing a
    /api/group-wins ledger entry back to the bids row(s) it classified. No
    filter params → unchanged full-table behavior. /api/bids is an
    established contract (agents rely on its field names/shapes), so this
    change is additive only — every existing field stays as-is.
    """
    db = _get_db()
    clauses: list[str] = []
    params: list = []
    if item_id is not None:
        clauses.append("item_id = ?")
        params.append(item_id)
    if snipe_group is not None:
        clauses.append("snipe_group = ?")
        params.append(snipe_group)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"SELECT * FROM bids {where} "
        "ORDER BY COALESCE(auction_end_at, added_at) DESC",
        params,
    ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        result.append({
            "id": item["id"],
            "item_id": item["item_id"],
            "title": item.get("ebay_title") or None,
            "max_bid": item["max_bid"],
            "bid_offset": item["bid_offset"],
            "snipe_group": item["snipe_group"],
            "end_date_iso": item.get("auction_end_at"),
            "added_at": item.get("added_at"),
            "status": item["status"],
            "status_mirror": item.get("status_mirror"),
            "winning_bid": item.get("winning_bid"),
            "seller": item.get("seller"),
            "local_snipe_at": item.get("local_snipe_at"),
            "local_snipe_result": item.get("local_snipe_result"),
            # BUI-371: expose the vanish observation + tombstone-cause note so
            # a REMOVED row's classification is auditable over HTTP (agents
            # have no sqlite access to the Mac Mini) — parity with the
            # server-log evidence trail.
            "gixen_vanished_at": item.get("gixen_vanished_at"),
            "notes": item.get("notes"),
            # BUI-382: same auditability rationale — exposes why a tombstoned
            # row stopped being re-fetched by the eBay fallback.
            "ebay_no_price_at": item.get("ebay_no_price_at"),
            # BUI-384: same auditability rationale — the group-membership
            # start that bounds _group_won_before's cancel evidence.
            "group_changed_at": item.get("group_changed_at"),
        })
    return result


@app.get("/api/group-wins")
async def api_get_group_wins(
    item_id: str | None = None, snipe_group: int | None = None
):
    """BUI-385 forensics: the durable group_wins evidence ledger (BUI-381), the
    thing that classifies a cancelled bid-group sibling REMOVED. Pure DB read —
    no Gixen sync. Answers "which win classified this row REMOVED": pass
    ?snipe_group=N (and optionally ?item_id=) to see the wins in that group,
    each with its recorded_at and source provenance
    (status-transition / startup-backfill / listed-win / legacy — the
    GROUP_WIN_SOURCES vocabulary). Agents have no sqlite access to the Mac
    Mini, so this is the only way to audit the ledger over HTTP — parity with
    the /api/bids evidence-trail fields (gixen_vanished_at, notes,
    group_changed_at).

    This is a gixen bidding-side concept (snipe groups, wins) owned by
    gixen-cli's own table, so it lives here beside /api/history and /api/bids —
    NOT on the overlay's provider-neutral /api/comics/* surface, which is for
    comic collection/FMV data.
    """
    db = _get_db()
    clauses: list[str] = []
    params: list = []
    if item_id is not None:
        clauses.append("item_id = ?")
        params.append(item_id)
    if snipe_group is not None:
        clauses.append("snipe_group = ?")
        params.append(snipe_group)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        "SELECT id, snipe_group, item_id, won_end_at, recorded_at, source "
        f"FROM group_wins {where} ORDER BY recorded_at DESC, id DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


# BUI-618 (U6): sensible bound on ?limit= — /api/decisions is an operator
# audit surface (the soak review in the plan's Operational Notes), not a bulk
# export; an unbounded limit could pull the entire ledger over HTTP.
_MAX_DECISIONS_LIMIT = 500


@app.get("/api/decisions")
async def api_get_decisions(item_id: str | None = None, limit: int = 50):
    """BUI-618 (U6) audit read: the append-only `bid_decisions` ledger, one
    row per pre-trade policy check-point evaluation (server/policy.py's
    `run_checks`, called from api_add_bid/api_edit_bid) — written regardless
    of what Gixen/the write ultimately does, so the maybe-money-moved cases
    (an unconfirmed upsert, an unconfirmed edit) get rows here too, alongside
    a clean committed write and a pre-write Gixen failure. Pure DB read,
    newest-first (`ORDER BY id DESC` — evaluated_at has only second
    granularity, so id is the tiebreaker that actually orders same-second
    rows deterministically).

    Optional ?item_id= filters to one item's decisions (mirrors /api/bids and
    /api/group-wins' filter convention); ?limit= is clamped to [1, 500] —
    this is an audit surface for the soak review, not a bulk export. Agents
    have no sqlite access to the Mac Mini, so this is the only way to audit
    the ledger over HTTP — same rationale as /api/group-wins above.
    """
    db = _get_db()
    clamped_limit = max(1, min(limit, _MAX_DECISIONS_LIMIT))
    return list_bid_decisions(db, item_id=item_id, limit=clamped_limit)


def _cached_dbidid(db: sqlite3.Connection, item_id: str) -> str | None:
    """BUI-116: the cached Gixen dbidid for a bid, or None on a cache miss.

    Reads the live (PENDING) row first, falling back to any row. NULL until a
    sync has warmed the cache, which simply means the edit takes the list path.
    """
    row = get_pending_bid_by_item_id(db, item_id) or get_bid_by_item_id(db, item_id)
    if row is None:
        return None
    try:
        return row["dbidid"]
    except (KeyError, IndexError):
        return None


def _clear_cached_dbidid(db: sqlite3.Connection, item_id: str) -> None:
    """Caller must commit (BUI-408) — both call sites below now wrap this in
    a write_transaction() under _write_lock instead of self-committing on
    the shared _db (same commit-free contract BUI-407 gave insert_bid et al.,
    see its docstring)."""
    db.execute("UPDATE bids SET dbidid=NULL WHERE item_id=?", (item_id,))


async def _modify_with_cache_fallback(
    db: sqlite3.Connection, item_id: str, max_bid: Decimal,
    bid_offset: int, snipe_group: int,
) -> None:
    """BUI-116: modify using the cached dbidid (fast path, no pre-POST list). If
    a cached id was used but the modify couldn't be confirmed (stale id — the
    snipe was re-created with a new dbidid), clear the cache and retry once via
    the list-based lookup. Exceptions propagate to the caller for HTTP mapping.

    BUI-402: caller must ALREADY HOLD _api_lock — this no longer acquires it.
    api_edit_bid resolves the bid_offset/snipe_group passthrough from the live
    DB row, calls this, and writes update_bid all under one acquisition, so the
    resolve read, the Gixen modify, and the local write stay atomic against a
    concurrent group/offset-changing PATCH (asyncio.Lock is not reentrant, so
    acquiring here would deadlock that single-acquisition caller). Both attempts
    below still run under that held lock, keeping the retry sequence atomic."""
    cached = _cached_dbidid(db, item_id)
    try:
        await asyncio.to_thread(
            _api_client.modify_snipe, item_id, max_bid,
            bid_offset=bid_offset, snipe_group=snipe_group, dbidid=cached,
        )
        return
    except GixenModifyNotConfirmedError:
        if cached is None:
            raise  # already used the list path — genuinely unconfirmable
        logger.warning(
            "modify with cached dbidid for %s unconfirmed; clearing cache "
            "and retrying via list lookup", item_id,
        )
        # BUI-408: await-free write, entered after the network await above —
        # its own short-lived write_transaction() under _write_lock.
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                _clear_cached_dbidid(wconn, item_id)
        await asyncio.to_thread(
            _api_client.modify_snipe, item_id, max_bid,
            bid_offset=bid_offset, snipe_group=snipe_group,  # dbidid=None
        )


async def _remove_with_cache_fallback(db: sqlite3.Connection, item_id: str) -> None:
    """BUI-116: remove using the cached dbidid, falling back to the list-based
    lookup if a cached id failed (stale id left the item in the list, or a
    transient error). Holds _api_lock across both attempts."""
    cached = _cached_dbidid(db, item_id)
    async with _api_lock:
        try:
            await asyncio.to_thread(_api_client.remove_snipe, item_id, dbidid=cached)
            return
        except GixenError:
            if cached is None:
                raise
            logger.warning(
                "remove with cached dbidid for %s failed; clearing cache and "
                "retrying via list lookup", item_id,
            )
            # BUI-408: _clear_cached_dbidid is now commit-free (shared with
            # _modify_with_cache_fallback above) — route this call site
            # through the same write_transaction()/_write_lock pattern so it
            # still actually commits.
            async with _write_locked():
                with write_transaction(_get_db_path()) as wconn:
                    _clear_cached_dbidid(wconn, item_id)
            await asyncio.to_thread(_api_client.remove_snipe, item_id)  # dbidid=None


async def _reconcile_after_unconfirmed_modify(
    item_id: str, requested_max_bid: float, exc: Exception,
) -> str:
    """BUI-555: after a modify_snipe that raised, re-read Gixen and persist what
    it ACTUALLY holds. Returns the 503 detail string for the caller to raise.

    modify_snipe POSTs first and confirms second, so every GixenError out of it
    covers two indistinguishable worlds: the POST never landed, or it landed and
    only the confirm read failed. The pre-BUI-555 handler assumed the first —
    it 503'd, skipped update_bid, and told the user the edit failed. When the
    second world was the true one, Gixen held the new cap, the DB held the old
    one, and nothing ever healed it.

    So: take one bounded extra read (under _api_lock, like every other Gixen
    call from a request handler) and write down the truth. Persisting Gixen's
    value is safe in BOTH worlds — if the POST never landed, Gixen still holds
    the pre-edit cap and the mirror is a no-op; if it did land, the DB stops
    lying. It is also the safe direction for _sniper_loop, which should fire at
    whatever cap the authoritative service holds.

    The status code stays 503 either way. We did not confirm the edit through
    the normal path, so claiming success would be the inverse lie — but the
    detail now says the POST may have applied, and names Gixen's current value.
    Every failure inside here is swallowed: the caller still gets its 503, and
    _sync_gixen's per-cycle mirror is the durable backstop.
    """
    # GixenConnectionError embeds the request URL, which carries a live
    # ?sessionid=<id>. This detail goes back over HTTP to the caller. BUI-555
    # redacted it here with a local regex; BUI-558 moved the redaction into
    # GixenError.__str__ itself, so str(exc) is already safe for every
    # GixenError subclass — this site no longer needs its own copy. (`exc`
    # can also be requests.HTTPError per the caller's except clause, but this
    # client's curl-based transport never embeds the URL in that exception's
    # message, so there is nothing left to strip.)
    exc_text = str(exc)
    logger.warning(
        "api_edit_bid: modify for item=%s (requested max_bid=%s) did not confirm "
        "(%s) — the POST MAY HAVE APPLIED on Gixen; re-reading to reconcile",
        item_id, requested_max_bid, exc_text,
    )
    unconfirmed = (
        f"Gixen edit for item {item_id} could not be confirmed and MAY HAVE "
        f"APPLIED: {exc_text}"
    )
    read_started_at = datetime.now(timezone.utc).isoformat()
    try:
        if _api_lock is None:  # lifespan never ran (shouldn't happen in-request)
            raise RuntimeError("_api_lock unset")
        async with _api_lock:
            snipes = await asyncio.to_thread(_api_client.list_snipes)
    except Exception:
        logger.exception(
            "api_edit_bid: reconciling re-read for item=%s also failed", item_id,
        )
        return (
            f"{unconfirmed}. Gixen could not be re-read, so the local max_bid was "
            f"left unchanged — the next Gixen sync will reconcile it."
        )

    listed = next((s for s in snipes if s.get("item_id") == str(item_id)), None)
    actual = parse_listed_max_bid(listed.get("max_bid")) if listed else None
    if actual is None:
        return (
            f"{unconfirmed}. Gixen re-read returned no usable max_bid for this "
            f"item, so the local value was left unchanged — the next Gixen sync "
            f"will reconcile it."
        )

    # scrape_started_at=read_started_at reuses the mirror's own ordering guard:
    # a PATCH that committed while this read was in flight wins over it.
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                repaired = mirror_gixen_max_bid(
                    wconn, item_id, actual,
                    observed_at=now, scrape_started_at=read_started_at,
                )
    except Exception:
        logger.exception(
            "api_edit_bid: reconciling write for item=%s failed", item_id,
        )
        return f"{unconfirmed}. Gixen holds max_bid={actual} for this item."

    if repaired is not None:
        logger.warning(
            "api_edit_bid: reconciled local max_bid for item=%s from %s to Gixen's "
            "%s after an unconfirmed modify (BUI-555)",
            item_id, repaired[0], repaired[1],
        )
    return (
        f"{unconfirmed}. Gixen currently holds max_bid={actual} for this item "
        f"and the local row now matches it."
    )


@app.patch("/api/bids/{item_id}")
async def api_edit_bid(item_id: str, req: EditBidRequest):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()

    # BUI-392/401: req.bid_offset / req.snipe_group being None means "leave
    # unchanged" (a max_bid-only PATCH). update_bid's passthrough handles our
    # local DB state, but Gixen's own modify form (GixenClient.modify_snipe) has
    # no passthrough concept — it always submits an explicit newbidoffset /
    # newsnipegroup — so resolve the snipe's current values from our DB and send
    # those, keeping Gixen's state in sync with "unchanged" intent instead of
    # resetting the offset to 6 / un-grouping it there too.
    #
    # Deliberately PENDING-only (not the get_bid_by_item_id fallback used for
    # dbidid caching below): get_bid_by_item_id has no status filter, so it
    # can return a stale terminal row from an earlier bidding cycle on the
    # same item_id (BUI-178-style re-listing) — using THAT row's values
    # would leak an unrelated old value into the live snipe instead of
    # preserving it. Falls back to the plain defaults (6 / 0) only when there's
    # no live row at all (e.g. a web-added snipe never ingested), matching the
    # pre-existing default in that edge case.
    #
    # BUI-402: the resolve read, the Gixen modify, AND the local update_bid all
    # run under ONE _api_lock acquisition. Resolving OUTSIDE the lock let a
    # concurrent group/offset-changing PATCH land between the read and the
    # modify and get silently reverted on Gixen's side while our DB kept the
    # newer value — a divergence that, unlike the stale-dbidid case, does NOT
    # self-heal via a confirm-retry. update_bid is inside the same lock too, so
    # the resolve can't read a state a concurrent edit already committed to
    # Gixen but not yet to the DB. _modify_with_cache_fallback no longer
    # acquires the lock (asyncio.Lock is not reentrant); this is the single
    # acquisition.
    # Populated inside the lock below, before any Gixen call can raise — see
    # the except clauses, which read `current` back for their ledger rows.
    current = None
    intent: PolicyIntent | None = None
    advisories: list[dict] = []
    check_results: list[dict] = []
    try:
        async with _api_lock:
            # BUI-615/616: fetch the live row once, up front — it feeds both
            # the pre-existing offset/group passthrough resolution below AND
            # the policy check point's PolicyIntent.prior_row (None when this
            # item was never ingested, e.g. a web-added snipe — the
            # "no_prior_row" case the U6 ledger marks via bid_row_id=None).
            current = get_pending_bid_by_item_id(db, item_id)
            gixen_bid_offset = req.bid_offset
            gixen_snipe_group = req.snipe_group
            if gixen_bid_offset is None:
                gixen_bid_offset = current["bid_offset"] if current is not None else 6
            if gixen_snipe_group is None:
                gixen_snipe_group = current["snipe_group"] if current is not None else 0
            # One check-point evaluation per request, BEFORE the Gixen call
            # (KTD1) — same advisories apply to whichever return path below
            # this request takes.
            intent = PolicyIntent(
                item_id=item_id,
                target_max_bid=req.max_bid,
                snipe_group=gixen_snipe_group,
                trigger="edit",
                prior_row=current,
            )
            advisories, check_results = run_checks(db, intent, app.state.plugin_manager)

            # BUI-623 (U9): same blocking verdict as api_add_bid, decided
            # right after run_checks and BEFORE the Gixen modify call below
            # — still inside _api_lock. Blocked -> 409, no Gixen call, no
            # bid-row change; `current` (the live PENDING row, if any) is
            # the surviving snipe named in the 409 detail (R14/U9: an edit
            # blocked on a live row must not read as "no money committed").
            block_decision = evaluate_block(
                check_results, advisories, bypass=req.policy_bypass,
            )
            if block_decision.blocked:
                surviving_snipe = (
                    {"item_id": current["item_id"], "max_bid": current["max_bid"]}
                    if current is not None else None
                )
                detail = build_block_detail(
                    block_decision, advisories, surviving_snipe=surviving_snipe,
                )
                await _append_bid_decision(
                    item_id=item_id, trigger="edit",
                    outcome=BID_DECISION_OUTCOME_BLOCKED,
                    bid_row_id=(current["id"] if current is not None else None),
                    requested_max_bid=req.max_bid,
                    check_results=check_results, advisories=advisories,
                    source=req.source, bypass=False,
                )
                raise HTTPException(status_code=409, detail=detail)

            await _modify_with_cache_fallback(
                db, item_id, Decimal(str(req.max_bid)),
                gixen_bid_offset, gixen_snipe_group,
            )
            # Passthrough (None) values go to update_bid so a max_bid-only edit
            # leaves both fields untouched locally; explicit values write through.
            # BUI-408: await-free write, entered after the Gixen modify await
            # above — its own short-lived write_transaction() under _write_lock
            # instead of a commit on the shared _db. The read-back also
            # happens on `wconn`, INSIDE the block — not on the shared `db`
            # afterward: `db` can have its own open transaction from an
            # unrelated in-flight _sync_gixen cycle (commit-free DML, then an
            # await, per its own design), which pins `db` to a snapshot that
            # predates this commit — a `db` read right after would risk
            # returning stale/missing data for the row this just wrote.
            async with _write_locked():
                with write_transaction(_get_db_path()) as wconn:
                    update_bid(wconn, item_id, req.max_bid, req.bid_offset, req.snipe_group)
                    row = get_bid_by_item_id(wconn, item_id)
    except GixenSnipeNotFoundError as e:
        # Nothing was POSTed — the pre-POST lookup found no such snipe — so
        # there is no "may have applied" ambiguity to reconcile here. BUI-618:
        # a clean pre-write failure -> gixen_failed, anchored to whatever
        # local row (if any) existed before this request.
        await _append_bid_decision(
            item_id=item_id, trigger="edit",
            outcome=BID_DECISION_OUTCOME_GIXEN_FAILED,
            bid_row_id=(current["id"] if current is not None else None),
            requested_max_bid=req.max_bid,
            check_results=check_results, advisories=advisories,
            source=req.source, bypass=req.policy_bypass,
        )
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in Gixen") from e
    except (GixenError, requests.HTTPError) as e:
        # BUI-555: both of these can be raised AFTER modify_snipe's POST has
        # already mutated Gixen (the confirm read is what failed), so neither
        # may be reported as a clean no-op failure. Reconcile, then 503.
        # BUI-618: this is the "maybe-money-moved" / unconfirmed-modify case
        # the ledger must still record (never gixen_failed — Gixen may well
        # hold the new value even though this request can't confirm it).
        detail = await _reconcile_after_unconfirmed_modify(item_id, req.max_bid, e)
        await _append_bid_decision(
            item_id=item_id, trigger="edit",
            outcome=BID_DECISION_OUTCOME_UNCONFIRMED,
            bid_row_id=(current["id"] if current is not None else None),
            requested_max_bid=req.max_bid,
            check_results=check_results, advisories=advisories,
            source=req.source, bypass=req.policy_bypass,
        )
        raise HTTPException(status_code=503, detail=detail) from e

    if row is None:
        # Gixen accepted the modify, so this snipe lives there — but our DB
        # has no row, meaning the snipe was added via Gixen's web UI and we
        # haven't ingested it yet. Run one sync (which has the web-added
        # insert path) so the response shape matches every other PATCH.
        try:
            async with _api_lock:
                await _sync_gixen(db, _api_client)
        except Exception as e:
            # BUI-410 (Stage 3 landed): the BUI-399 `db.rollback()` here is
            # retired — this post-modify _sync_gixen no longer writes the
            # shared `_db` (its DML is on its own write_transaction(); the
            # gather phase only reads), so there is no stranded batch to roll
            # back. reraise=False (the default) still keeps
            # GixenConnectionError/GixenError out of this except, so only a
            # genuine unexpected bug lands here. Superseded per-caller rollback
            # convention — see
            # docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md.
            logger.exception(
                "api_edit_bid: unexpected error during post-modify sync"
            )
            raise HTTPException(
                status_code=500, detail="edit failed: internal error"
            ) from e
        # _sync_gixen ingests with the snipe's existing max_bid from Gixen,
        # but we want the user-supplied value to win. Re-apply locally.
        # BUI-408: await-free write, entered after _sync_gixen's await above
        # completed — its own short-lived write_transaction() under _write_lock.
        # (_sync_gixen is itself now gather-then-apply under _write_lock too —
        # BUI-410 — so this re-apply serializes cleanly behind the sync's own
        # commit.) Read-back on `wconn` too, inside the block — same staleness
        # reasoning as the main write above.
        async with _write_locked():
            with write_transaction(_get_db_path()) as wconn:
                update_bid(wconn, item_id, req.max_bid, req.bid_offset, req.snipe_group)
                row = get_bid_by_item_id(wconn, item_id)
        if row is None:
            raise HTTPException(
                status_code=500,
                detail=f"Item {item_id} not in DB after sync — Gixen state unexpectedly empty",
            )

    # BUI-617/618: the modify committed (whether via the direct path above or
    # the web-added sync-and-reapply recovery just above) — notify plugins
    # and append the ledger row using the FINAL resolved row, exactly once.
    # notify_bid_write_committed is synchronous (KTD1) — no await.
    notify_bid_write_committed(
        app.state.plugin_manager, db, intent, row["id"], check_results,
    )
    await _append_bid_decision(
        item_id=item_id, trigger="edit",
        outcome=BID_DECISION_OUTCOME_COMMITTED,
        bid_row_id=row["id"], requested_max_bid=req.max_bid,
        check_results=check_results, advisories=advisories,
        source=req.source, bypass=req.policy_bypass,
    )
    return {**dict(row), "advisories": advisories}


@app.delete("/api/bids/{item_id}")
async def api_remove_bid(item_id: str):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()
    try:
        await _remove_with_cache_fallback(db, item_id)
    except GixenSnipeNotFoundError:
        # BUI-164: the item is already absent from Gixen's list — the desired
        # end state of a remove (snipe gone) is already true. Fall through to
        # tombstone the local row instead of 404ing and leaving it PENDING,
        # where it lingers in /api/snipes and, if never locally sniped, could
        # still be re-fired by the local sniper.
        logger.info(
            "remove: %s already absent from Gixen — tombstoning REMOVED", item_id,
        )
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    delete_bid(db, item_id)
    # BUI-407: delete_bid no longer self-commits — commit here, at the same
    # point its self-commit used to fire.
    db.commit()
    # Response status mirrors the soft-delete tombstone, renamed PURGED ->
    # REMOVED in BUI-49. No in-repo consumer string-matches the old value.
    return {"item_id": item_id, "status": "REMOVED"}


@app.post("/api/sync")
async def api_sync():
    """Pull live Gixen state and insert any web-added snipes missing from the DB.

    BUI-386: this is the only sync entry point that used to propagate an
    exception straight to FastAPI's generic 500 handler. The other two
    (_sync_loop, _ensure_fresh_sync) already catch and degrade gracefully —
    but both are best-effort background refreshers where swallowing the
    error and continuing with stale-but-present data is the right call. This
    endpoint is a user-triggered action, so a failure must be reported
    honestly instead of degrading silently: reraise=True (the _sync_loop
    pattern, BUI-263) surfaces a genuine Gixen-side failure as a 503 rather
    than letting it collapse into a misleadingly-successful
    ``{"synced": 0}``, and any other exception is a genuine server bug,
    logged in full and reported as a structured 500 instead of an unhandled
    traceback.
    """
    db = _get_db()
    try:
        async with _api_lock:
            snipes = await _sync_gixen(db, _api_client, reraise=True)
    except (GixenConnectionError, GixenError) as e:
        logger.warning("api_sync: Gixen sync failed: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        # BUI-410 (Stage 3 landed): the BUI-386 `db.rollback()` here is retired
        # — _sync_gixen no longer batches DML on the shared singleton `_db`
        # (all writes go through its own write_transaction(), the gather phase
        # only reads), so a genuine server bug can no longer strand a partial
        # cycle's writes for the next sync to smuggle in. Superseded per-caller
        # rollback convention — see
        # docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md.
        logger.exception("api_sync: unexpected error during sync")
        raise HTTPException(
            status_code=500, detail="sync failed: internal error"
        ) from e
    return {"synced": len(snipes)}


@app.post("/api/purge")
async def api_purge(req: PurgeRequest):
    db = _get_db()

    # 1. Sync first to capture any outstanding WON/LOST transitions;
    #    reuse the snipes list for sibling detection (avoids a second Gixen call)
    try:
        async with _api_lock:
            gixen_snipes = await _sync_gixen(db, _api_client)
    except Exception as e:
        # BUI-410 (Stage 3 landed): the BUI-399 `db.rollback()` here is retired
        # — _sync_gixen no longer batches DML on the shared singleton `_db`
        # (all writes go through its own write_transaction(), the gather phase
        # only reads), so an unexpected bug can no longer strand a partial
        # cycle's writes on `_db`. reraise=False (the default) still keeps
        # GixenConnectionError/GixenError out of this except (_sync_gixen
        # swallows those and returns []), so only a genuine unexpected bug
        # lands here. Superseded per-caller rollback convention — see
        # docs/solutions/conventions/shared-singleton-connection-rollback-on-unexpected-exception.md.
        logger.exception("api_purge: unexpected error during pre-purge sync")
        raise HTTPException(
            status_code=500, detail="purge failed: internal error"
        ) from e

    # 2. Detect siblings server-side (client may also pass explicit IDs)
    server_siblings = find_sibling_cleanup_targets(gixen_snipes)
    all_sibling_ids = list({s["item_id"] for s in server_siblings} | set(req.sibling_ids))

    # 3. Collect completed bid item_ids before purging Gixen
    completed = db.execute(
        "SELECT item_id FROM bids WHERE status IN ('WON','LOST','ENDED','FAILED')"
    ).fetchall()
    completed_ids = [r["item_id"] for r in completed]

    # 4. Purge completed on Gixen
    try:
        async with _api_lock:
            await asyncio.to_thread(_api_client.purge_completed)
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    # 5. Mark completed bids with the soft-delete tombstone (REMOVED) in DB.
    # BUI-408: await-free write, entered after the purge_completed await
    # above — its own short-lived write_transaction() under _write_lock.
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            mark_bids_purged(wconn, completed_ids)

    # 6. Remove sibling snipes (best-effort)
    removed = 0
    for sibling_id in all_sibling_ids:
        try:
            async with _api_lock:
                await asyncio.to_thread(_api_client.remove_snipe, sibling_id)
        except GixenError as e:
            # Gixen-side removal itself failed — nothing changed on either
            # side for this sibling, so it's safe to just move on to the
            # next one (unchanged from before BUI-416).
            logger.warning(
                "api_purge: remove_snipe failed for sibling %s; leaving it "
                "tracked locally: %s", sibling_id, e,
            )
            continue

        # BUI-408: await-free write, entered after this iteration's
        # remove_snipe await — its own short-lived write_transaction()
        # under _write_lock (the next iteration's await happens under a
        # fresh _api_lock acquisition, after this commit has landed).
        #
        # BUI-416: remove_snipe above already succeeded — Gixen no longer has
        # this sibling. Any exception here (e.g. sqlite3.OperationalError
        # from SQLITE_BUSY on write_transaction()'s busy_timeout) is a WORSE
        # outcome than the GixenError branch above: it leaves local/Gixen
        # state actually diverged (removed upstream, still tracked as live
        # locally), not just "nothing happened yet". Scope the broad catch to
        # only this DB write (not the remove_snipe call, and not the whole
        # per-sibling block) so a real bug elsewhere still surfaces normally;
        # log it loudly (logger.exception, full traceback) so the divergence
        # is visible for a later audit/retry instead of silently stranding
        # it — and keep going, since the point of this fix is that one
        # sibling's DB hiccup must not abort every remaining sibling in the
        # loop.
        try:
            async with _write_locked():
                with write_transaction(_get_db_path()) as wconn:
                    delete_bid(wconn, sibling_id)
            removed += 1
        except Exception:
            # Note: this sibling won't be re-offered to THIS loop on the next
            # purge — find_sibling_cleanup_targets() only re-detects it from
            # a fresh Gixen snipe list, and remove_snipe above already took
            # it off Gixen. The row is left PENDING/live locally, so it's the
            # general vanished-row handling in _sync_gixen (this same
            # endpoint's step 1, and the background _sync_loop) that
            # eventually resolves it via the vanished-from-Gixen path — a
            # different mechanism than this loop, but one that does still run
            # on every subsequent sync.
            logger.exception(
                "api_purge: sibling %s removed from Gixen but local "
                "delete_bid failed — local DB now diverged from Gixen for "
                "this item (still tracked as live); expect the next sync's "
                "vanished-row handling to resolve it",
                sibling_id,
            )

    return {"purged_completed": len(completed_ids), "removed_siblings": removed}
