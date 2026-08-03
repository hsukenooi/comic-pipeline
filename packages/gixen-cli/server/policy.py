"""Pre-trade policy check point (BUI-609 Phase A / BUI-615 / BUI-616 / BUI-623).

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


def config_snapshot(check_results: list[dict] | None = None) -> dict:
    """Snapshot of the policy env vars consulted for a request (KTD2/KTD3),
    read fresh on every call — same per-request-read discipline as the
    checks themselves, never cached at import time. Recorded verbatim (raw
    string, not parsed) on the `bid_decisions` ledger row so a malformed
    value or a mid-soak env change is self-describing per row instead of
    needing log correlation.

    Always includes the host-owned vars every request consults (today: just
    POLICY_EXPOSURE_CEILING). `check_results` is optional and additive
    (BUI-623/U9): when a caller passes the same `check_results` list
    `run_checks` returned, this ALSO records the raw `POLICY_BLOCK_<CODE>`
    value (or None if unset) for every code that actually ran this request —
    not a fixed/exhaustive list of every check that could theoretically
    exist, so config_json only ever documents flags that were live for
    THIS evaluation. Omitting `check_results` (every pre-U9 call site, and
    any future caller that doesn't have it handy) reproduces the exact
    pre-U9 shape byte-for-byte — the existing `test_config_snapshot_reads_
    current_env`-style equality assertions keep passing unmodified.
    """
    snapshot: dict = {"POLICY_EXPOSURE_CEILING": os.getenv("POLICY_EXPOSURE_CEILING")}
    if check_results:
        for result in check_results:
            code = result.get("code")
            if not code:
                continue
            var = _policy_block_env_var(code)
            snapshot.setdefault(var, os.getenv(var))
    return snapshot


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


# ---------------------------------------------------------------------------
# U9 — blocking mode with audited bypass (BUI-623)
#
# Everything below is called AFTER `run_checks` returns, from the SAME two
# call sites (`api_add_bid`/`api_edit_bid` in server/main.py), still inside
# `_api_lock`, still before any Gixen call — one shared implementation so
# the two handlers can't drift on what "blocked" means (the same anti-drift
# rule U1's `run_checks` itself already applies to the check point).
#
# Design recap (plan KTD4/R14/R15):
#   - `POLICY_BLOCK_<CODE>` (CODE = a check's own `code`, upper-cased) is an
#     opt-in per-check boolean flag, default OFF, read PER REQUEST (KTD2
#     discipline extended to a boolean flag, not just a numeric threshold).
#   - A check that came back `advise` AND whose flag is on AND the caller
#     did not set `policy_bypass` -> this write is BLOCKED: 409, no Gixen
#     call, no bid-row change, ledger `outcome=blocked`.
#   - A check that came back `unevaluable` NEVER blocks by itself — fail-
#     closed is reserved for a check that affirmatively fired (the guard-
#     strictness learning: a check that couldn't run is not evidence of a
#     problem). But if THAT check's own flag is on, the check has gone
#     blind while configured to gate money — silently proceeding would be
#     "permissive without saying so," so the response and ledger row both
#     carry an explicit `unevaluable_while_blocking` marker instead.
#   - `policy_bypass=True` on the request suppresses blocking entirely for
#     this one call (per-invocation, and blanket — it does not name which
#     check(s) it acknowledges); the full advisory set still lands in the
#     ledger (`bypass=1`) so what was overridden is always reconstructable.
# ---------------------------------------------------------------------------

# Boolean parsing for POLICY_BLOCK_<CODE>. Deliberately narrow and fail-safe
# in the "stays off" direction: this flag starts blocking real-money writes,
# so an operator typo (or a stray non-boolean value) must never be silently
# interpreted as "on." Unlike POLICY_EXPOSURE_CEILING's numeric KTD2 handling
# (malformed -> `unevaluable`, because that check has nothing sane to do
# without a real ceiling), a malformed boolean flag has an obvious safe
# fallback — treat it as unset/off — so this degrades to "blocking stays
# disabled" rather than inventing an unevaluable-flag concept of its own.
_BLOCK_FLAG_TRUTHY = frozenset({"1", "true", "yes", "on"})
_BLOCK_FLAG_FALSY = frozenset({"0", "false", "no", "off", ""})


def _policy_block_env_var(code: str) -> str:
    """The env var name a check's `code` maps to. Codes are already
    snake_case (`exposure_ceiling`, and the overlay's `over_fmv` /
    `recomputed_cap` / `staleness` / `unpriced_entry` / `duplicate_comic`
    per BUI-620/U4) so upper-casing alone produces exactly the
    `POLICY_BLOCK_<CHECK>` shape the plan/ticket specify — no separate
    registry to keep in sync with whatever checks exist today or are added
    later, host- or plugin-owned alike."""
    return f"POLICY_BLOCK_{code.upper()}"


def _block_flag_on(code: str) -> bool:
    """Read `POLICY_BLOCK_<CODE>` fresh (KTD2 discipline — never cached).
    Unset, empty, or a value outside the recognized truthy/falsy vocabulary
    all resolve to `False` — the safe default for a flag that starts
    blocking the money path. An unrecognized non-empty value gets one loud
    log (an operator typo that silently fails to enable blocking is still
    worth a signal, even though the safe behavior is "stays disabled")."""
    var = _policy_block_env_var(code)
    raw = os.getenv(var)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in _BLOCK_FLAG_TRUTHY:
        return True
    if normalized not in _BLOCK_FLAG_FALSY:
        logger.warning(
            "policy: %s=%r is not a recognized boolean value; treating as "
            "off (blocking stays disabled for check %r) — use one of %s to "
            "enable",
            var, raw, code, sorted(_BLOCK_FLAG_TRUTHY),
        )
    return False


@dataclass
class BlockDecision:
    """The U9 blocking verdict for one write's already-computed check
    results.

    `blocked` is the only field either handler needs to branch on — True
    means raise the 409 instead of proceeding to Gixen. `blocking_codes`
    names every check whose `advise` outcome had its block flag on (plural:
    more than one blocking check can fire on the same write); it is
    populated even when `bypass` suppressed the block, so the ledger/409
    body can still say what WOULD have blocked. `unevaluable_while_
    blocking_codes` names checks that are `unevaluable` with their block
    flag ALSO on — `evaluate_block` has already stamped `data
    ["unevaluable_while_blocking"] = True` onto the matching check_results/
    advisories dicts by the time this is returned (both the response and
    the ledger read those same dicts), so this list is just the code-only
    summary callers use to build the 409 body / assert on in tests.
    """

    blocked: bool
    blocking_codes: list[str] = field(default_factory=list)
    unevaluable_while_blocking_codes: list[str] = field(default_factory=list)


def evaluate_block(
    check_results: list[dict],
    advisories: list[dict],
    *,
    bypass: bool,
) -> BlockDecision:
    """Decide block/bypass/markers from this write's own check_results
    (KTD1: called once, right after `run_checks`, by both `api_add_bid` and
    `api_edit_bid` — the single shared implementation the module docstring
    above promises).

    Mutates matching entries of `check_results` AND `advisories` in place
    (adding `data["unevaluable_while_blocking"] = True`) rather than
    returning new lists — both are the SAME objects the caller is about to
    hand to `_append_bid_decision` (ledger) and the HTTP response, so
    mutating in place is what makes "response AND ledger row carry the
    marker" true by construction instead of by two call sites remembering
    to apply it identically.

    A check whose own `POLICY_EXPOSURE_CEILING`-style config is unset never
    appears in `check_results` at all (KTD2: disabled, not unevaluable) —
    there is nothing here for that check's block flag to act on, which is
    consistent with "the operator disabled this check" rather than a gap:
    U9 does not invent an evaluable state for a check the operator never
    turned on in the first place.
    """
    blocking_codes: list[str] = []
    unevaluable_while_blocking_codes: list[str] = []

    for result in check_results:
        code = result.get("code")
        outcome = result.get("outcome")
        if not code or not _block_flag_on(code):
            continue
        if outcome == "advise":
            blocking_codes.append(code)
        elif outcome == "unevaluable":
            unevaluable_while_blocking_codes.append(code)
            result.setdefault("data", {})["unevaluable_while_blocking"] = True

    if unevaluable_while_blocking_codes:
        marked = set(unevaluable_while_blocking_codes)
        for advisory in advisories:
            if advisory.get("code") in marked:
                advisory.setdefault("data", {})["unevaluable_while_blocking"] = True

    blocked = bool(blocking_codes) and not bypass
    return BlockDecision(
        blocked=blocked,
        blocking_codes=blocking_codes,
        unevaluable_while_blocking_codes=unevaluable_while_blocking_codes,
    )


def build_block_detail(
    decision: BlockDecision,
    advisories: list[dict],
    *,
    surviving_snipe: dict | None = None,
) -> dict:
    """The HTTPException(status_code=409, detail=...) body — one builder so
    `api_add_bid`/`api_edit_bid` can't drift on its shape (KTD4: the 409
    carries the same structured advisory envelope a 2xx response does,
    inside `detail`).

    `surviving_snipe` is `{"item_id", "max_bid"}` for the live PENDING row
    this write would have upserted/edited, or None for a genuine create —
    R14/U9 acceptance: a BLOCKED write on top of an existing live snipe must
    never read as "no money committed" when a prior commitment survives, so
    the message names it explicitly (not just buried in `surviving_snipe`'s
    structured data) and the caller (main.py) is expected to pass it
    whenever `intent.prior_row`/`existing`/`current` was not None.
    """
    parts = [f"Blocked by policy check(s): {', '.join(decision.blocking_codes)}."]
    if surviving_snipe is not None:
        parts.append(
            f"Existing snipe at ${surviving_snipe['max_bid']:.2f} remains active."
        )
    return {
        "blocked": True,
        "message": " ".join(parts),
        "blocking_codes": decision.blocking_codes,
        "unevaluable_while_blocking": decision.unevaluable_while_blocking_codes,
        "advisories": advisories,
        "surviving_snipe": surviving_snipe,
    }
