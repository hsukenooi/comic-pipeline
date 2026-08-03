"""Pre-trade policy check point (BUI-609 Phase A / BUI-615 / BUI-616).

Owns the single check point both write handlers call — `api_add_bid` and
`api_edit_bid` in `server/main.py` — inside their existing `_api_lock`
acquisition, before the Gixen call (KTD1). v1 is **advisory-only**: nothing
in this module may block, fail, or delay a bid write. Every 2xx write
response gains an `advisories: [...]` key built from this module's output
(KTD4), even when the list is empty.

Config (KTD2): policy env vars (`POLICY_EXPOSURE_CEILING` today) are read
**per request**, inside the check functions themselves — never cached at
import time. This deliberately avoids the `GIXEN_SYNC_INTERVAL` import-time
trap (see the plan's KTD2): an operator edits `~/.comics-server/.env` and
`launchctl kickstart`s, no deploy needed, and a value changed mid-session is
picked up by the very next request.

    - unset (absent, or empty/whitespace-only) -> the check is disabled and
      does not appear in the results at all (not `pass`, not `unevaluable`).
    - malformed (present, non-numeric) -> the check returns `unevaluable`,
      carrying the raw string in `data["raw_config"]`, plus one loud log. A
      config typo must never present as "check found nothing" (KTD6).

Tri-state (KTD6): every check returns `pass` / `advise` / `unevaluable`.
"Errored" must never collapse into "found nothing" — that's exactly the
Metron-breaker-class mistake this repo has already learned from. An
exception raised *inside* a check function is caught by `run_checks` itself
and downgraded to `unevaluable` + a loud log; it never propagates into the
handler (the guard-strictness learning: a check bug must degrade to a
warning, not become a 5xx on the money path).

`run_checks(conn, intent, pm)` accepts a plugin manager (`app.state.
plugin_manager`, None-tolerant). Since BUI-617 (U3), `pm` is used: overlay-
contributed FMV-aware checks arrive via the `check_bid_write` hookspec
(`gixen/plugins.py`) and merge into the same tri-state result list host
checks build — a raising plugin degrades to a single `unevaluable` result
(KTD1/KTD6), never an exception into the write path. `notify_bid_write_
committed` (below) fires the companion post-write hookspec after a bid row
has actually landed, so a plugin (the overlay, in a later wave) can persist
state its checks resolved — the FMV link, per U4/U5 — now that a row exists.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

from server.db import TOMBSTONE_STATUSES_SQL

logger = logging.getLogger("server.policy")

Trigger = Literal["create", "upsert", "edit", "batch-row"]
Outcome = Literal["pass", "advise", "unevaluable"]


@dataclass
class PolicyIntent:
    """Read-only snapshot of a single write's shape, gathered by the calling
    handler before Gixen is contacted.

    - `item_id` / `target_max_bid` / `snipe_group` describe the write being
      made — `target_max_bid` is always the NEW amount (AE6: an upsert-modify
      of a live item runs checks against the new amount, not the old one).
    - `trigger` distinguishes a POST create, a POST upsert-modify (BUI-67),
      a PATCH edit, and a batch row (add-batch still routes through
      `api_add_bid` per plan R1, so it is a `create`/`upsert` at this layer;
      `batch-row` is reserved for a future caller that wants to tag it
      distinctly and is not produced by either handler today).
    - `prior_row` is the live PENDING row this write replaces, or None when
      there isn't one (a genuine create, or a PATCH against an item never
      ingested — the "no_prior_row" case the U6 ledger will mark; the
      exposure check below derives this from the DB directly rather than
      from this field, so it stays correct even if a caller leaves it None).
    - `comic_identities` is populated on the POST path only (BUI-619/U5):
      `api_add_bid` threads `AddBidRequest.comic_identities` straight
      through — the payload's `{comic_id|locg_id, grade}` list, list-capable
      for a future lot caller (KTD8). `api_edit_bid` leaves it at this
      field's own `[]` default; PATCH resolves identity from the bid's
      existing FMV links instead of request-supplied identity (U4
      territory), matching `EditBidRequest`, which never gained the field.
    """

    item_id: str
    target_max_bid: float
    snipe_group: int
    trigger: Trigger
    prior_row: sqlite3.Row | dict | None = None
    comic_identities: list[dict] = field(default_factory=list)


@dataclass
class CheckResult:
    """One check's tri-state outcome (KTD6)."""

    code: str
    outcome: Outcome
    message: str
    data: dict = field(default_factory=dict)

    def to_advisory(self) -> dict | None:
        """Project onto the KTD4 response-envelope shape.

        A clean `pass` never surfaces as an advisory — the envelope only
        carries things worth a human's attention. `unevaluable` DOES surface
        (with its own severity, distinct from `advise`) precisely so a
        malformed config value is visible to the caller now, in the absence
        of a decisions ledger (U6, a later wave) to record it instead.
        """
        if self.outcome == "pass":
            return None
        severity = "warning" if self.outcome == "advise" else "unevaluable"
        return {
            "code": self.code,
            "severity": severity,
            "message": self.message,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# U2 — group-aware PENDING exposure ceiling check
# ---------------------------------------------------------------------------


def _sum_grouped_pending(rows: list[dict]) -> float:
    """The group-aware projection formula, as a pure function of already-
    fetched rows (execution note: implement test-first — the group math is
    the bug surface).

    Each row is a mapping with `snipe_group` and `max_bid`. Ungrouped rows
    (`snipe_group == 0`) sum in full — every ungrouped snipe is independent
    committed capital. Rows sharing a nonzero `snipe_group` count ONCE, at
    their group's maximum `max_bid` — a bid group fires as a unit (only one
    sibling ever wins), so only the highest stake in the group is capital
    actually at risk. AE3: ungrouped $100 + $50 plus a group of $200/$180 ->
    100 + 50 + 200 = 350.
    """
    ungrouped_total = 0.0
    group_max: dict[int, float] = {}
    for row in rows:
        snipe_group = row["snipe_group"]
        max_bid = row["max_bid"]
        if not snipe_group:
            ungrouped_total += max_bid
        elif max_bid > group_max.get(snipe_group, float("-inf")):
            group_max[snipe_group] = max_bid
    return ungrouped_total + sum(group_max.values())


def _project_exposure(conn: sqlite3.Connection, intent: PolicyIntent) -> float:
    """Aggregate PENDING exposure this write would produce.

    Reads every live PENDING row (tombstones excluded via
    TOMBSTONE_STATUSES_SQL — belt-and-suspenders: `status='PENDING'` alone
    already can't overlap a tombstone status per the `bids.status` CHECK
    constraint, but the exclusion documents the invariant this query relies
    on and survives a future status-vocabulary change), then REPLACES the
    target item's own previous contribution with `intent.target_max_bid`
    rather than summing it: a create has no prior row to exclude (adds);
    an upsert/edit's existing row for `intent.item_id` is excluded from the
    fetched set before the new value is folded in (replaces) — U2
    acceptance. This also makes "PATCH on a not-yet-ingested row" fall out
    for free: no row matches `intent.item_id`, so the prior contribution is
    naturally 0 (the `no_prior_row` case; the U6 ledger will mark it
    explicitly, a later wave).
    """
    rows = conn.execute(
        "SELECT item_id, snipe_group, max_bid FROM bids "
        f"WHERE status='PENDING' AND status NOT IN ({TOMBSTONE_STATUSES_SQL})"
    ).fetchall()
    others = [dict(r) for r in rows if r["item_id"] != intent.item_id]
    target_row = {
        "item_id": intent.item_id,
        "snipe_group": intent.snipe_group,
        "max_bid": intent.target_max_bid,
    }
    return _sum_grouped_pending([*others, target_row])


def _check_exposure(conn: sqlite3.Connection, intent: PolicyIntent) -> CheckResult | None:
    """Advisory when the projected aggregate PENDING exposure exceeds
    `POLICY_EXPOSURE_CEILING`. Unset ceiling disables the check (returns
    None, not a result) — KTD2."""
    raw = os.getenv("POLICY_EXPOSURE_CEILING")
    if raw is None or not raw.strip():
        return None

    try:
        ceiling = float(raw)
    except ValueError:
        logger.warning(
            "policy: POLICY_EXPOSURE_CEILING=%r does not parse as a number; "
            "exposure check unevaluable for this request (a config typo "
            "must never present as 'check found nothing')",
            raw,
        )
        return CheckResult(
            code="exposure_ceiling",
            outcome="unevaluable",
            message=f"POLICY_EXPOSURE_CEILING={raw!r} is not a valid number.",
            data={"raw_config": raw},
        )

    projected = _project_exposure(conn, intent)
    data = {"projected": projected, "ceiling": ceiling, "item_id": intent.item_id}
    if projected > ceiling:
        return CheckResult(
            code="exposure_ceiling",
            outcome="advise",
            message=(
                f"Projected PENDING exposure ${projected:.2f} would exceed "
                f"the configured ceiling ${ceiling:.2f}."
            ),
            data=data,
        )
    return CheckResult(
        code="exposure_ceiling",
        outcome="pass",
        message="Projected PENDING exposure is within the configured ceiling.",
        data=data,
    )


# ---------------------------------------------------------------------------
# U1 — check point
# ---------------------------------------------------------------------------

# Host-owned checks, in evaluation order. Overlay-contributed checks arrive
# separately, via the U3 `check_bid_write` hookspec — see
# _invoke_plugin_checks below; they are not registered in this tuple because
# they come from a plugin manager, not a fixed host-side list.
_CHECKS: tuple = (
    _check_exposure,
)

# The tri-state outcomes a plugin's check_bid_write contribution may declare
# (KTD6). A plugin-returned outcome outside this set is itself downgraded to
# unevaluable — a cooperative plugin still shouldn't need to raise just to
# report "I couldn't evaluate this," but a malformed value must never read
# as a silent pass either.
_VALID_OUTCOMES = frozenset({"pass", "advise", "unevaluable"})


def _invoke_plugin_checks(
    pm: Any, conn: sqlite3.Connection, intent: PolicyIntent,
) -> list[CheckResult]:
    """Invoke the `check_bid_write` hookspec (KTD1/U3) and normalize every
    plugin's contribution into `CheckResult`s that merge into the same
    tri-state list `run_checks` builds from host-owned checks.

    Defensive per the `_collect_dashboard_tabs` pattern in `gixen/plugins.py`:
    pluggy halts a bulk hook call's impl chain on the FIRST plugin that
    raises (see `_invoke_register_routes`'s docstring there), so this wraps
    the ENTIRE `pm.hook.check_bid_write(...)` call in one try/except — a
    raising plugin degrades to exactly ONE `unevaluable` result for the whole
    call (not per-plugin isolation; a raising plugin also loses every OTHER
    plugin's contribution for this call, same tradeoff `_collect_dashboard_
    tabs` already accepts for the bulk dashboard-tabs hook). Never lets a
    plugin exception reach the caller — v1 must never let a check bug block,
    fail, or delay a bid write (KTD6 / the guard-strictness learning).

    `pm=None` (a standalone gixen-cli with no overlay registered) returns
    `[]` without attempting any hook call — R5 in the plan: a standalone
    server has only the host-owned exposure check.
    """
    if pm is None:
        return []
    try:
        per_plugin_lists = pm.hook.check_bid_write(conn=conn, intent=intent)
    except Exception:
        logger.exception(
            "policy: check_bid_write hook raised while evaluating item_id=%s; "
            "downgrading to a single unevaluable result — a plugin check bug "
            "must never block, fail, or delay a bid write",
            intent.item_id,
        )
        return [CheckResult(
            code="check_bid_write",
            outcome="unevaluable",
            message="A plugin's check_bid_write hook raised an unexpected error.",
            data={},
        )]

    results: list[CheckResult] = []
    for lst in per_plugin_lists:
        if lst is None:
            continue
        if not isinstance(lst, list):
            logger.error(
                "policy: check_bid_write returned %s, expected list[dict]; "
                "skipping this plugin's contribution",
                type(lst).__name__,
            )
            continue
        for item in lst:
            if not isinstance(item, dict):
                logger.error(
                    "policy: check_bid_write list element is %s, expected "
                    "dict; skipping",
                    type(item).__name__,
                )
                continue
            outcome = item.get("outcome")
            if outcome not in _VALID_OUTCOMES:
                logger.error(
                    "policy: check_bid_write result has outcome=%r, expected "
                    "one of %s; downgrading to unevaluable",
                    outcome, sorted(_VALID_OUTCOMES),
                )
                outcome = "unevaluable"
            results.append(CheckResult(
                code=str(item.get("code") or "plugin_check"),
                outcome=outcome,
                message=str(item.get("message") or ""),
                data=item.get("data") or {},
            ))
    return results


def notify_bid_write_committed(
    pm: Any,
    conn: sqlite3.Connection,
    intent: PolicyIntent,
    bid_row_id: int | None,
    check_results: list[dict],
) -> None:
    """Fire the post-write `on_bid_write_committed` hookspec (KTD1/U3), after
    a bid write has actually committed to the DB.

    Call sites (`server/main.py`'s `api_add_bid`/`api_edit_bid`) invoke this
    ONLY on a genuine committed write — never for an unconfirmed upsert/edit
    or a Gixen failure, where no new row landed. Notification-only: no return
    value is consumed.

    Same coarse-grained defensive wrapping as `_invoke_plugin_checks` /
    `_collect_dashboard_tabs` — one try/except around the whole bulk hook
    call. The write has already committed by the time this runs, so a
    raising plugin can only be logged, never surfaced to the caller (there is
    nothing left to roll back or degrade).

    `pm=None` is a no-op.
    """
    if pm is None:
        return
    try:
        pm.hook.on_bid_write_committed(
            conn=conn, intent=intent, bid_row_id=bid_row_id,
            check_results=check_results,
        )
    except Exception:
        logger.exception(
            "policy: on_bid_write_committed hook raised for item_id=%s "
            "bid_row_id=%s; the write already committed, so only this "
            "notification is lost",
            intent.item_id, bid_row_id,
        )


# ---------------------------------------------------------------------------
# U6 — config snapshot for the decisions ledger
# ---------------------------------------------------------------------------


def config_snapshot() -> dict:
    """Snapshot of the policy env vars consulted for a request (KTD2/KTD3),
    read fresh on every call — same per-request-read discipline as the
    checks themselves, never cached at import time. Recorded verbatim (raw
    string, not parsed) on the `bid_decisions` ledger row so a malformed
    value or a mid-soak env change is self-describing per row instead of
    needing log correlation.

    Only lists vars this phase's checks consult (today: just
    POLICY_EXPOSURE_CEILING). Later waves (U4's FMV checks, U9's blocking
    flags) extend this dict without changing the ledger schema — config_json
    is opaque JSON.
    """
    return {"POLICY_EXPOSURE_CEILING": os.getenv("POLICY_EXPOSURE_CEILING")}


def run_checks(
    conn: sqlite3.Connection,
    intent: PolicyIntent,
    pm: Any = None,
) -> tuple[list[dict], list[dict]]:
    """Run every host-owned check plus every plugin-contributed check
    (KTD1/U3's `check_bid_write` hookspec) for this write, and return
    `(advisories, check_results)`.

    - `advisories` is the KTD4 response-envelope list: `{code, severity,
      message, data}` per non-`pass` outcome. Handlers attach this verbatim
      under the `advisories` key on every 2xx branch.
    - `check_results` is the full tri-state record of every check that ran
      (including `pass`), as plain dicts — carried into the U6 decisions
      ledger by the caller.

    Never raises. Host checks are gather-phase reads only (no writes) and any
    exception one of them raises is caught here, logged loudly, and turned
    into an `unevaluable` result — v1 must never let a check bug turn into a
    failed or delayed bid write (KTD6 / the guard-strictness learning).
    Plugin-contributed checks go through `_invoke_plugin_checks`, which
    applies the same never-raises guarantee at the hook-call boundary.

    `pm` is `app.state.plugin_manager` (None-tolerant — a standalone
    gixen-cli without the overlay registered runs only the host-owned
    checks).
    """
    results: list[CheckResult] = []
    for check in _CHECKS:
        name = getattr(check, "__name__", "unknown_check").lstrip("_") or "unknown_check"
        try:
            result = check(conn, intent)
        except Exception:
            logger.exception(
                "policy: check %r raised while evaluating item_id=%s; "
                "downgrading to unevaluable — v1 is advisory-only and a "
                "check bug must never block or fail a bid write",
                name, intent.item_id,
            )
            result = CheckResult(
                code=name,
                outcome="unevaluable",
                message=f"{name} raised an unexpected error and could not be evaluated.",
                data={},
            )
        if result is not None:
            results.append(result)

    results.extend(_invoke_plugin_checks(pm, conn, intent))

    advisories = [a for r in results if (a := r.to_advisory()) is not None]
    check_results = [asdict(r) for r in results]
    return advisories, check_results
