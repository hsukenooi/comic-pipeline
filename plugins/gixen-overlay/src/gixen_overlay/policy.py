"""Overlay-contributed FMV-aware pre-trade checks (BUI-620, plan unit U4).

`check_bid_write` below is the function `plugin.py`'s `check_bid_write`
hookimpl (BUI-617/U3) dispatches to — the overlay's contribution to the
host's `server.policy.run_checks` check point (`POST /api/bids` and
`PATCH /api/bids/{item_id}`, inside `_api_lock`, before the Gixen call).
Five checks, each returning the tri-state `{code, outcome, message, data}`
shape `server.policy.CheckResult` documents (KTD6: `pass` / `advise` /
`unevaluable`) as a plain dict — the hookspec contract is `list[dict]`, not
the host's dataclass, so this module never imports `server.policy` at all.

**Read-only (v1 is advisory-only, KTD1).** No function in this module writes
to the DB. The FMV link persist that used to live in this ticket's text is
`on_bid_write_committed` in `plugin.py` — that shipped in BUI-619 (U5) and is
untouched here.

Resolution (KTD8 — both arms sum resolved FMV highs, never one issue's high
for a lot):

- **POST** (`intent.trigger in {"create", "upsert"}`) resolves each entry of
  `intent.comic_identities` via `gixen_overlay.routes._resolve_fmv_for_link`
  (the same three-strategy resolver `POST /api/bids/{item_id}/link-fmv`
  uses: `comic_id` direct, then `locg_id` JOIN — no series/issue fallback,
  since the add payload never carries those; see `on_bid_write_committed`'s
  own docstring).
- **PATCH** (`intent.trigger == "edit"`) resolves from the bid's *existing*
  `bid_fmvs` links (`intent.prior_row["id"]`) — a PATCH payload carries no
  identity at all (`EditBidRequest` never gained the field).

Both arms land on the same `list[dict]` shape (the `fmv` table's own
columns), so every check below is written once, against that shape, keyed
only by which resolver populated it — a check function has no idea (and
does not need to know) whether it is looking at a POST or a PATCH.

**Priced vs resolved.** A resolved fmv row can still be an unpriced *stub*
(`high IS NULL` — `/comic:fmv` never ran, or a comp lookup returned n=0).
Over-FMV, recomputed-cap, and staleness all operate on the sum of *priced*
(non-NULL-`high`) rows only, and go quiet (return `None`, same as a
disabled host check) when that set is empty — `unpriced_entry` is the
single check responsible for surfacing "there is nothing priced to check
against," so the other three staying silent avoids four advisories
reporting the same underlying fact (the plan's "alert fatigue" risk).

**Per-check isolation (binding constraint).** The host's own
`_invoke_plugin_checks` wraps the *entire* `pm.hook.check_bid_write(...)`
call in one try/except — a raise anywhere loses every plugin's whole
contribution for that call (`_collect_dashboard_tabs`-style bulk-call
semantics; see its docstring in `server/policy.py`). This module does not
rely on that alone: `check_bid_write` below wraps *each* of the five checks
in its own try/except, mirroring the host's own `run_checks` loop over
`_CHECKS`, so a bug in one check (e.g. a NULL `confidence` value, a
malformed identity dict) degrades only that check to `unevaluable` rather
than silently discarding the other four.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from gixen_overlay.models import LinkFmvRequest
from gixen_overlay.routes import _resolve_fmv_for_link
from server.db import TOMBSTONE_STATUSES_SQL

logger = logging.getLogger("gixen_overlay.policy")

# ---------------------------------------------------------------------------
# KTD5 — rung constants, duplicated from apps/fmv/src/fmv_math.py.
#
# apps/fmv is not a uv workspace member (root CLAUDE.md — apps/* stay
# uv-tool-install-managed by design), so there is no import edge to reuse
# `bid_factor` here; creating one would be the extraction KTD5 explicitly
# rejects. Instead these three numbers are copied and pinned by a
# source-parsing canary (tests/test_fmv_rung_parity.py) that fails loudly if
# fmv_math.py's constants ever drift out from under this copy.
#
# fmv_math.bid_factor combines TWO confidence axes (fmv_confidence from the
# comp pool, grade_confidence from /comic:grade's photo read) and takes the
# more conservative of the two. grade_confidence is never persisted on the
# `fmv` row — only fmv_confidence (`fmv.confidence`, three levels: high /
# medium / low; fmv_math's 4-level "medium-low" has no `fmv.confidence`
# equivalent) survives to request time here. So this recompute is
# necessarily a narrower approximation of the brief-path haircut — exact
# parity (folding in a stored grade_confidence) is U8 territory, not this
# one. The recomputed-cap check's advisory data says so explicitly.
# ---------------------------------------------------------------------------
RUNG_HIGH_CONFIDENCE = 0.80    # == fmv_math.BASE_BID_FACTOR
RUNG_MEDIUM_CONFIDENCE = 0.70  # == the literal MEDIUM-LOW-branch return in fmv_math.bid_factor
RUNG_LOW_CONFIDENCE = 0.60     # == fmv_math.INTERPOLATED_BID_FACTOR

# fmv.confidence is CHECK-constrained to 'high' / 'medium' / 'low' / NULL
# (gixen_overlay/db.py's create_tables), and the writer is fmv_runner's
# _confidence_to_db_label, which COLLAPSES the brief path's finer ladder:
# HIGH → 'high', {MEDIUM-HIGH, MEDIUM} → 'medium', {MEDIUM-LOW, LOW} → 'low'.
# fmv_math.bid_factor pays BASE (0.80) for MEDIUM and above, so a stored
# 'medium' row was bid at 0.80 by the brief path — mapping 'medium' to 0.70
# here would false-advise every standard medium-confidence bid. Likewise a
# stored 'low' can be a MEDIUM-LOW row (0.70 — the entire CGC-proxy tier,
# whose factor is capped at 0.70 and whose confidence stores as 'low'), so
# it maps to 0.70, not 0.60. The cap is therefore the LAXEST rung the brief
# path could have applied to that stored label: the advisory only fires on
# a bid NO rung would have produced, never on a legitimately-computed one.
# A sub-rung overshoot inside the collapsed band (a genuine-LOW book bid
# between 0.60x and 0.70x) is deliberately not flagged — tightening below
# this lax bound is a rung-demotion signal, which KTD9 gates on U8's
# measurement, never on an unmeasured reverse-map guess. NULL/unrecognized
# maps to 0.80 because bid_factor's own default for an absent confidence
# signal is BASE_BID_FACTOR (no haircut without a signal).
_RUNG_BY_CONFIDENCE = {
    "high": RUNG_HIGH_CONFIDENCE,
    "medium": RUNG_HIGH_CONFIDENCE,   # stored 'medium' ⊇ {MEDIUM-HIGH, MEDIUM} → brief factor 0.80
    "low": RUNG_MEDIUM_CONFIDENCE,    # stored 'low' ⊇ {MEDIUM-LOW, LOW} → laxest brief factor 0.70
}


def _rung_for_confidence(confidence: Any) -> float:
    if isinstance(confidence, str):
        return _RUNG_BY_CONFIDENCE.get(confidence.strip().lower(), RUNG_HIGH_CONFIDENCE)
    return RUNG_HIGH_CONFIDENCE


def _result(code: str, outcome: str, message: str, data: dict | None = None) -> dict:
    """Build one tri-state result in the hookspec's `list[dict]` shape."""
    return {"code": code, "outcome": outcome, "message": message, "data": data or {}}


def _read_policy_float(code: str, env_name: str, default: float) -> tuple[float | None, dict | None]:
    """Read an FMV-check threshold env var, per request (KTD2).

    Unlike the host's `POLICY_EXPOSURE_CEILING` (no default — unset disables
    the check entirely), `POLICY_FMV_MULTIPLE` and `POLICY_FMV_STALE_DAYS`
    both have documented defaults: unset does NOT disable these checks, it
    falls back to the default. A present-but-non-numeric value still
    degrades to `unevaluable`, carrying the raw string — never a crash,
    never a silent fallback to the default (a config typo must be visible).

    Returns `(value, None)` on success (unset-with-default or a parseable
    override), or `(None, error_result)` on a malformed value.
    """
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default, None
    try:
        return float(raw), None
    except ValueError:
        logger.warning(
            "gixen_overlay.policy: %s=%r does not parse as a number; %s "
            "unevaluable for this request", env_name, raw, code,
        )
        return None, _result(
            code, "unevaluable",
            f"{env_name}={raw!r} is not a valid number.",
            {"raw_config": raw},
        )


# ---------------------------------------------------------------------------
# Resolution (KTD8) — shared by unpriced_entry / over_fmv / recomputed_cap /
# fmv_staleness. Each check calls this independently (not hoisted into the
# dispatcher) so a raise inside resolution, caught by the dispatcher's
# per-check try/except, only takes down the one check it happened in.
# ---------------------------------------------------------------------------


def _fetch_fmv_row(conn: sqlite3.Connection, fmv_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM fmv WHERE id=?", (fmv_id,)).fetchone()
    return dict(row) if row is not None else None


def _resolve_post_identities(conn: sqlite3.Connection, identities: list) -> tuple[list[dict], list[str]]:
    """POST arm: resolve each `intent.comic_identities` entry via the same
    resolver `link-fmv` uses. Returns (resolved fmv rows, attempted
    strategies across every entry) — the attempted list feeds the
    unpriced-entry advisory's `data` (ticket + AE4).

    Entries are untrusted input carried straight from the request body
    (`AddBidRequest.comic_identities: list[dict]`, unvalidated past "is a
    list of dicts" — see that field's own docstring in server/main.py): a
    non-dict entry, a missing grade/comic_id/locg_id, or a value pydantic's
    `LinkFmvRequest` rejects all degrade to "this entry didn't resolve",
    never a crash.
    """
    if not identities:
        return [], ["no comic identity supplied in the add payload"]

    resolved: list[dict] = []
    attempted: list[str] = []
    for entry in identities:
        if not isinstance(entry, dict):
            attempted.append(f"malformed identity entry (not a dict): {entry!r}")
            continue
        grade = entry.get("grade")
        comic_id = entry.get("comic_id")
        locg_id = entry.get("locg_id")
        if grade is None or (comic_id is None and locg_id is None):
            attempted.append(f"identity missing grade or comic_id/locg_id: {entry!r}")
            continue
        try:
            req = LinkFmvRequest(grade=grade, comic_id=comic_id, locg_id=locg_id)
        except Exception as exc:  # noqa: BLE001  # untrusted per-entry payload input (pydantic ValidationError or any other bad-shape value) — record it in attempted and keep resolving the rest of the list, never crash the whole write
            attempted.append(f"identity failed validation ({exc.__class__.__name__}): {entry!r}")
            continue
        fmv_ref, strategies = _resolve_fmv_for_link(conn, req)
        attempted.extend(strategies)
        if fmv_ref is None:
            continue
        full = _fetch_fmv_row(conn, fmv_ref["fmv_id"])
        if full is not None:
            resolved.append(full)
    return resolved, attempted


def _resolve_patch_links(conn: sqlite3.Connection, bid_row_id: int | None) -> list[dict]:
    """PATCH arm: every existing `bid_fmvs` link's fmv row for this bid.

    `bid_row_id` is None for a genuine "no_prior_row" PATCH (an edit against
    an item never ingested locally — a web-added snipe the sync loop hasn't
    picked up yet) — returns `[]`, same as a never-linked bid; both are
    "nothing to compare the bid against" and unpriced_entry (below) is the
    single check that says so.
    """
    if bid_row_id is None:
        return []
    rows = conn.execute(
        "SELECT f.* FROM bid_fmvs bf JOIN fmv f ON f.id = bf.fmv_id WHERE bf.bid_id = ?",
        (bid_row_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _resolve_identities(conn: sqlite3.Connection, intent: Any) -> tuple[list[dict], list[str]]:
    """Dispatch to the POST or PATCH resolution arm by `intent.trigger`.

    Only `"edit"` is the PATCH arm — `"create"`/`"upsert"`/the reserved-but-
    unused `"batch-row"` (PolicyIntent's own docstring: add-batch still
    routes through `api_add_bid`, so it never actually produces this trigger
    today) all carry a payload identity, so they take the POST arm.
    """
    if intent.trigger == "edit":
        bid_row_id = None
        prior = getattr(intent, "prior_row", None)
        if prior is not None:
            try:
                bid_row_id = prior["id"]
            except (KeyError, IndexError, TypeError):
                bid_row_id = None
        resolved = _resolve_patch_links(conn, bid_row_id)
        attempted = [] if resolved else ["no existing FMV link for this bid (PATCH arm)"]
        return resolved, attempted
    identities = getattr(intent, "comic_identities", None) or []
    return _resolve_post_identities(conn, identities)


def _priceable(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("high") is not None]


# ---------------------------------------------------------------------------
# Check 1 — unpriced entry (R5): identity missing or unresolvable.
# ---------------------------------------------------------------------------


def _check_unpriced_entry(conn: sqlite3.Connection, intent: Any) -> dict | None:
    resolved, attempted = _resolve_identities(conn, intent)
    priceable = _priceable(resolved)
    data = {
        "resolved_count": len(resolved), "priced_count": len(priceable),
        "attempted": attempted,
    }
    if priceable:
        return _result(
            "unpriced_entry", "pass",
            "At least one linked FMV row has a priced high.", data,
        )
    return _result(
        "unpriced_entry", "advise",
        "No priced FMV is linked to this write — the comic identity is "
        "missing, unresolvable, or resolved to an unpriced stub. See "
        "data.attempted for the resolution strategies tried.",
        data,
    )


# ---------------------------------------------------------------------------
# Check 2 — over-FMV: max_bid > N x summed resolved fmv_high.
# ---------------------------------------------------------------------------


def _check_over_fmv(conn: sqlite3.Connection, intent: Any) -> dict | None:
    resolved, _ = _resolve_identities(conn, intent)
    priceable = _priceable(resolved)
    if not priceable:
        return None  # unpriced_entry already reports "nothing priced"

    multiple, err = _read_policy_float("over_fmv", "POLICY_FMV_MULTIPLE", 1.0)
    if err is not None:
        return err

    summed_high = sum(r["high"] for r in priceable)
    cap = multiple * summed_high
    ratio = (intent.target_max_bid / summed_high) if summed_high else None
    data = {
        "max_bid": intent.target_max_bid, "summed_high": summed_high,
        "multiple": multiple, "cap": cap, "ratio": ratio,
        "link_count": len(priceable),
    }
    if intent.target_max_bid > cap:
        return _result(
            "over_fmv", "advise",
            f"max_bid ${intent.target_max_bid:.2f} exceeds {multiple:g}x "
            f"summed FMV high (${summed_high:.2f} across {len(priceable)} "
            f"link(s)) = ${cap:.2f}.",
            data,
        )
    return _result(
        "over_fmv", "pass",
        f"max_bid ${intent.target_max_bid:.2f} is within {multiple:g}x "
        f"summed FMV high (${cap:.2f}).",
        data,
    )


# ---------------------------------------------------------------------------
# Check 3 — recomputed cap: rung constants applied to fmv.confidence.
# ---------------------------------------------------------------------------


def _check_recomputed_cap(conn: sqlite3.Connection, intent: Any) -> dict | None:
    resolved, _ = _resolve_identities(conn, intent)
    priceable = _priceable(resolved)
    if not priceable:
        return None

    per_link = [
        {
            "fmv_id": r.get("id"),
            "confidence": r.get("confidence"),
            "high": r["high"],
            "factor": _rung_for_confidence(r.get("confidence")),
            "cap": _rung_for_confidence(r.get("confidence")) * r["high"],
        }
        for r in priceable
    ]
    recomputed_cap = sum(row["cap"] for row in per_link)
    data = {
        "max_bid": intent.target_max_bid, "recomputed_cap": recomputed_cap,
        "link_count": len(priceable), "per_link": per_link,
        "basis": "fmv.confidence only — grade_confidence is not stored on "
                 "the fmv row (see U8)",
        "rungs": {
            "high": RUNG_HIGH_CONFIDENCE, "medium": RUNG_MEDIUM_CONFIDENCE,
            "low": RUNG_LOW_CONFIDENCE,
        },
    }
    if intent.target_max_bid > recomputed_cap:
        return _result(
            "recomputed_cap", "advise",
            f"max_bid ${intent.target_max_bid:.2f} exceeds the confidence-"
            f"adjusted cap ${recomputed_cap:.2f}.",
            data,
        )
    return _result(
        "recomputed_cap", "pass",
        f"max_bid ${intent.target_max_bid:.2f} is within the confidence-"
        f"adjusted cap ${recomputed_cap:.2f}.",
        data,
    )


# ---------------------------------------------------------------------------
# Check 4 — staleness: fmv.updated_at older than POLICY_FMV_STALE_DAYS.
# ---------------------------------------------------------------------------


def _fmv_age_days(updated_at: Any, ref: datetime) -> float | None:
    """Age in days, or None if `updated_at` is missing/unparseable — mirrors
    `gixen_overlay.db._heartbeat_age_hours`'s "a corrupt timestamp must not
    read as recent" contract: callers treat None as unknown, never as fresh."""
    if not isinstance(updated_at, str):
        return None
    try:
        seen = datetime.fromisoformat(updated_at)
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (ref - seen).total_seconds() / 86400.0


def _check_staleness(conn: sqlite3.Connection, intent: Any) -> dict | None:
    resolved, _ = _resolve_identities(conn, intent)
    priceable = _priceable(resolved)
    if not priceable:
        return None

    stale_days, err = _read_policy_float("fmv_staleness", "POLICY_FMV_STALE_DAYS", 30.0)
    if err is not None:
        return err

    ref = datetime.now(timezone.utc)
    stale: list[dict] = []
    unknown_age_count = 0
    for r in priceable:
        age = _fmv_age_days(r.get("updated_at"), ref)
        if age is None:
            unknown_age_count += 1
            continue
        if age > stale_days:
            stale.append({"fmv_id": r.get("id"), "age_days": round(age, 1)})

    data = {
        "stale_days_threshold": stale_days, "stale": stale,
        "unknown_age_count": unknown_age_count, "link_count": len(priceable),
    }
    if stale:
        oldest = max(s["age_days"] for s in stale)
        return _result(
            "fmv_staleness", "advise",
            f"{len(stale)} linked FMV row(s) are older than {stale_days:g} "
            f"days (oldest {oldest:.1f} days).",
            data,
        )
    return _result(
        "fmv_staleness", "pass",
        f"All priced FMV rows are within {stale_days:g} days.", data,
    )


# ---------------------------------------------------------------------------
# Check 5 — duplicate comic (R6/KTD7): a live PENDING snipe on another
# item_id, linked to the same comics.id, outside this write's bid group.
# POST only (the table in the plan's design section: PATCH doesn't
# introduce a new comic identity to compare — the linkage a PATCH sees is
# whatever a prior POST already established, so any duplicate it could
# reveal was already caught then).
# ---------------------------------------------------------------------------


def _identity_to_comic_id(conn: sqlite3.Connection, entry: Any) -> int | None:
    """Resolve an identity entry to a `comics.id`, WITHOUT requiring an fmv
    row to exist at any particular grade (unlike `_resolve_fmv_for_link`,
    which is grade-narrowed by design). `comic_id` in the payload already
    IS `comics.id` (see `_resolve_fmv_for_link`'s own docstring: "the
    caller already knows the internal DB id") — validated against the table
    so a junk value degrades to "no comic," never a crash or a false match.
    """
    if not isinstance(entry, dict):
        return None
    comic_id = entry.get("comic_id")
    if comic_id is not None:
        row = conn.execute("SELECT id FROM comics WHERE id=?", (comic_id,)).fetchone()
        return row["id"] if row is not None else None
    locg_id = entry.get("locg_id")
    if locg_id is not None:
        row = conn.execute("SELECT id FROM comics WHERE locg_id=? LIMIT 1", (locg_id,)).fetchone()
        return row["id"] if row is not None else None
    return None


def _check_duplicate_comic(conn: sqlite3.Connection, intent: Any) -> dict | None:
    if intent.trigger not in ("create", "upsert"):
        return None
    identities = getattr(intent, "comic_identities", None) or []
    if not identities:
        return None

    # AE1 exemption key: same snipe_group as intent.snipe_group, NONZERO
    # groups only — group 0 is "ungrouped" and never exempts (binding
    # constraint / BUI-363's sanctioned pattern).
    exempt_group = intent.snipe_group or None

    duplicates: list[dict] = []
    checked_comic_ids: list[int] = []
    for entry in identities:
        comic_id = _identity_to_comic_id(conn, entry)
        if comic_id is None:
            continue
        checked_comic_ids.append(comic_id)
        new_grade = entry.get("grade") if isinstance(entry, dict) else None
        rows = conn.execute(
            f"""
            SELECT b.item_id, b.snipe_group, f.grade
            FROM bids b
            JOIN bid_fmvs bf ON bf.bid_id = b.id
            JOIN fmv f ON f.id = bf.fmv_id
            WHERE f.comic_id = ?
              AND b.status = 'PENDING'
              AND b.status NOT IN ({TOMBSTONE_STATUSES_SQL})
              AND b.item_id != ?
            """,
            (comic_id, intent.item_id),
        ).fetchall()
        for row in rows:
            if exempt_group is not None and row["snipe_group"] == exempt_group:
                continue
            duplicates.append({
                "comic_id": comic_id,
                "new_grade": new_grade,
                "existing_item_id": row["item_id"],
                "existing_snipe_group": row["snipe_group"],
                "existing_grade": row["grade"],
            })

    if not checked_comic_ids:
        return None
    if not duplicates:
        return _result(
            "duplicate_comic", "pass",
            "No live PENDING snipe on another item is linked to the same "
            "comic outside this bid's group.",
            {"comic_ids_checked": checked_comic_ids},
        )

    first = duplicates[0]
    message = (
        f"This comic already has a live PENDING snipe on item "
        f"{first['existing_item_id']} (group {first['existing_snipe_group']}, "
        f"grade {first['existing_grade']}) outside this bid's group "
        f"(new grade {first['new_grade']})."
    )
    if len(duplicates) > 1:
        message += f" {len(duplicates) - 1} more duplicate(s) found."
    return _result("duplicate_comic", "advise", message, {"duplicates": duplicates})


# ---------------------------------------------------------------------------
# Dispatcher — plugin.py's check_bid_write hookimpl calls this.
# ---------------------------------------------------------------------------

_CHECKS: tuple = (
    ("unpriced_entry", _check_unpriced_entry),
    ("over_fmv", _check_over_fmv),
    ("recomputed_cap", _check_recomputed_cap),
    ("fmv_staleness", _check_staleness),
    ("duplicate_comic", _check_duplicate_comic),
)


def check_bid_write(conn: sqlite3.Connection, intent: Any) -> list[dict]:
    """The overlay's `check_bid_write` hookspec contribution (KTD1/U3/U4).

    Never raises: each of the five checks runs in its own try/except, so a
    bug in one (a NULL confidence, a malformed identity dict, a database
    hiccup) degrades only that check to `unevaluable` — the host's own
    `_invoke_plugin_checks` wraps the whole hook call defensively too, but a
    cooperative plugin does not lean on that alone (binding constraint: one
    raise there loses every OTHER check's contribution as well).

    `_CHECKS` pairs each function with its OWN code string (rather than
    deriving one from `__name__`, as the host's `run_checks` does for its
    single check) so a crash mid-check reports under the SAME `code` its
    normal `pass`/`advise` results use — a consumer of the ledger filtering
    by `code="over_fmv"` must see the crashed evaluations too, or KTD6's
    "errored must never collapse into found nothing" quietly fails for
    anyone grouping by code.
    """
    results: list[dict] = []
    for code, check in _CHECKS:
        try:
            result = check(conn, intent)
        except Exception:
            logger.exception(
                "gixen_overlay.policy: check %r raised while evaluating "
                "item_id=%s; downgrading to unevaluable — a check bug must "
                "never block, fail, or delay a bid write (KTD1/KTD6)",
                code, getattr(intent, "item_id", None),
            )
            result = _result(
                code, "unevaluable",
                f"{code} raised an unexpected error and could not be evaluated.",
                {},
            )
        if result is not None:
            results.append(result)
    return results
