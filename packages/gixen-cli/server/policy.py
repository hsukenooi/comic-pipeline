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
plugin_manager`, None-tolerant) now so both call sites in `server/main.py`
won't need to change signature again once U3 (a later wave, NOT implemented
here) adds the `check_bid_write` hookspec for overlay-contributed FMV-aware
checks. `pm` is unused in this phase.
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
    - `comic_identities` is unpopulated in this phase (U5/U4, a later wave,
      wire it up) — carried now so the intent shape is stable across waves.
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
# via the U3 `check_bid_write` hookspec (a later wave) — not wired in here.
_CHECKS: tuple = (
    _check_exposure,
)


def run_checks(
    conn: sqlite3.Connection,
    intent: PolicyIntent,
    pm: Any = None,
) -> tuple[list[dict], list[dict]]:
    """Run every host-owned check for this write and return
    `(advisories, check_results)`.

    - `advisories` is the KTD4 response-envelope list: `{code, severity,
      message, data}` per non-`pass` outcome. Handlers attach this verbatim
      under the `advisories` key on every 2xx branch.
    - `check_results` is the full tri-state record of every check that ran
      (including `pass`), as plain dicts — reserved for the U6 decisions
      ledger (a later wave, not yet wired here).

    Never raises. Checks are gather-phase reads only (no writes) and any
    exception one of them raises is caught here, logged loudly, and turned
    into an `unevaluable` result — v1 must never let a check bug turn into a
    failed or delayed bid write (KTD6 / the guard-strictness learning).

    `pm` is accepted for forward compatibility with U3's `check_bid_write`
    hookspec (a later wave); it is not invoked in this phase.
    """
    del pm  # reserved for U3; overlay hookspec doesn't exist yet.

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

    advisories = [a for r in results if (a := r.to_advisory()) is not None]
    check_results = [asdict(r) for r in results]
    return advisories, check_results
