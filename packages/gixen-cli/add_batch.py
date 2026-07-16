"""add_batch.py — BUI-360: `gixen add-batch`, encoding the BUI-168 mid-batch
failure semantics as deterministic code instead of an LLM-followed skill loop.

`.claude/commands/comic/snipe-add.md`'s "Handling a failed add (BUI-168)"
section is the prose spec this module makes literal:

  1. A non-zero `gixen add` marks that row FAILED with the error — never
     silently skipped, never recorded as added.
  2. Re-check server health before the next row. If the server is down, HALT
     the batch and report every remaining row as NOT_ATTEMPTED rather than
     keep firing adds at a dead server.
  3. Never emit an all-success summary when any row failed.

This module is pure logic (no click, no sys.exit) so it's independently
testable and reusable — `cli.py`'s `add-batch` command wires it to the real
`_server_request_result` HTTP call and prints the human table + JSON summary.
It deliberately reuses the same server-mode request shape as `cli.py`'s
existing `add` command (POST /api/bids, then POST /api/bids/{id}/link-fmv
when grade+comic_id are both given) rather than re-deriving it, so the two
code paths cannot silently drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

# Row outcome statuses.
STATUS_ADDED = "added"
STATUS_UPDATED = "updated"
STATUS_FAILED = "failed"
STATUS_NOT_ATTEMPTED = "not_attempted"

_TERMINAL_OK_STATUSES = (STATUS_ADDED, STATUS_UPDATED)


class AddBatchError(Exception):
    """The input file itself is unusable (not a hard-stop condition of an
    individual row — that's a per-row FAILED result instead, so one bad row
    doesn't block every other row in an otherwise-good batch)."""


class ServerRequestFn(Protocol):
    """Structural type for the injected request callable: same shape as
    `cli._server_request_result` — returns (ok, data, error_message) instead
    of raising/exiting, so this module can decide per-row how to react."""

    def __call__(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[bool, Any, str | None]: ...


@dataclass
class RowResult:
    """`error` and `link_error` are deliberately separate fields, not one
    overloaded string: `error` is set only when the add itself failed
    (status == STATUS_FAILED — the row did not land). `link_error` is set
    when the add succeeded but the subsequent link-fmv call failed (status
    stays ADDED/UPDATED — the snipe landed, it's just unlinked). A consumer
    scanning for `error is not None` to mean "this row failed" must not
    catch a merely-unlinked-but-added row."""

    item_id: str | None
    status: str
    max_bid: float | None = None
    grade: float | None = None
    created: bool | None = None
    link_attempted: bool = False
    link_ok: bool | None = None
    error: str | None = None
    link_error: str | None = None
    verify: dict | None = None

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "status": self.status,
            "max_bid": self.max_bid,
            "grade": self.grade,
            "created": self.created,
            "link_attempted": self.link_attempted,
            "link_ok": self.link_ok,
            "error": self.error,
            "link_error": self.link_error,
            "verify": self.verify,
        }


@dataclass
class BatchOutcome:
    rows: list[RowResult] = field(default_factory=list)
    halted: bool = False
    verify_error: str | None = None

    def summary(self) -> dict:
        counts = {
            STATUS_ADDED: 0,
            STATUS_UPDATED: 0,
            STATUS_FAILED: 0,
            STATUS_NOT_ATTEMPTED: 0,
        }
        for r in self.rows:
            counts[r.status] = counts.get(r.status, 0) + 1
        return {"total": len(self.rows), **counts}

    def exit_code(self) -> int:
        """Non-zero if ANY row failed to land — a not-attempted row (batch
        halted before reaching it) is just as much a non-success as a failed
        one, so it counts too."""
        s = self.summary()
        return 0 if (s[STATUS_FAILED] == 0 and s[STATUS_NOT_ATTEMPTED] == 0) else 1

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "halted": self.halted,
            "verify_error": self.verify_error,
            "rows": [r.to_dict() for r in self.rows],
        }


def parse_rows(raw: Any) -> list[dict]:
    """Validate the top-level shape of the input file. Accepts either a bare
    JSON list of rows, or an object with a top-level "rows" list (forgiving
    of whichever shape the caller produces). Raises AddBatchError for a
    structurally unusable file — a single row's missing/invalid fields is a
    per-row FAILED result at add time instead (see `add_one_row`), not a
    hard stop here.

    Duplicate `item_id`s across rows ARE a structural problem worth a hard
    stop, though: the server upserts on `item_id` (BUI-67), so two rows for
    the same item_id would silently collapse into one bid while both rows
    get reported as independently landed — misleading for a real-money bid
    report. A row missing `item_id` entirely isn't included in this check;
    that's a per-row validation failure in `add_one_row` instead."""
    if isinstance(raw, dict) and "rows" in raw:
        raw = raw["rows"]
    if not isinstance(raw, list):
        raise AddBatchError(
            "input must be a JSON list of rows (or an object with a "
            'top-level "rows" list)'
        )
    rows = []
    seen_item_ids: set[str] = set()
    duplicate_item_ids: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise AddBatchError(f"row {i} is not a JSON object: {row!r}")
        item_id = row.get("item_id")
        if item_id:
            if item_id in seen_item_ids:
                duplicate_item_ids.add(item_id)
            seen_item_ids.add(item_id)
        rows.append(row)
    if duplicate_item_ids:
        raise AddBatchError(
            "duplicate item_id(s) in rows file (the server upserts on "
            "item_id, so duplicates would silently collapse into one bid "
            f"while both rows report as landed): {sorted(duplicate_item_ids)}"
        )
    return rows


class _RowValidationError(Exception):
    """Internal only — caught by `add_one_row` to build a FAILED RowResult
    without ever touching the network."""


def _require(row: dict, field_name: str) -> Any:
    value = row.get(field_name)
    if value is None:
        raise _RowValidationError(f"missing required field: {field_name}")
    return value


def _optional_float(row: dict, field_name: str) -> float | None:
    value = row.get(field_name)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise _RowValidationError(f"invalid {field_name}: {value!r}")
    if not math.isfinite(result):
        # NaN/Infinity pass `float()` and Decimal() without error and can
        # slip past a naive server-side "v <= 0" positivity check (NaN
        # compares False either way under IEEE-754) — reject client-side
        # before a degenerate numeric value reaches a real-money bid field.
        raise _RowValidationError(f"invalid {field_name}: {value!r} (not finite)")
    return result


def _optional_int(row: dict, field_name: str, default: int) -> int:
    value = row.get(field_name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _RowValidationError(f"invalid {field_name}: {value!r}")


def build_bid_payload(
    item_id: str,
    max_bid: Decimal | float,
    offset: int,
    group: int,
    *,
    seller: str | None = None,
    seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> dict[str, Any]:
    """The POST /api/bids payload shape, shared by cli.py's single-item
    `add` command and `add_one_row` below so the two request paths cannot
    silently drift on a future field addition to one and not the other."""
    payload: dict[str, Any] = {
        "item_id": item_id,
        "max_bid": float(max_bid),
        "bid_offset": offset,
        "snipe_group": group,
    }
    if seller is not None:
        payload["seller"] = seller
    if seller_grade is not None:
        payload["seller_grade"] = seller_grade
    if photo_grade is not None:
        payload["photo_grade"] = photo_grade
    return payload


def created_from_response(resp: Any) -> bool:
    """POST /api/bids upserts (BUI-67); `created=False` means an existing
    live snipe was updated in place. Shared by `add` and `add_one_row` so
    the "missing key defaults to True" rule lives in exactly one place."""
    return resp.get("created", True) if isinstance(resp, dict) else True


def add_one_row(row: dict, *, server_request: ServerRequestFn) -> RowResult:
    """Add exactly one row through the server-mode path — mirrors cli.py's
    `add` command's server branch (POST /api/bids, then a conditional
    POST .../link-fmv), but returns a RowResult instead of printing +
    sys.exit'ing, so the batch loop can keep going after a row fails."""
    item_id = row.get("item_id")

    try:
        item_id = _require(row, "item_id")
        max_bid_raw = _require(row, "max_bid")
        try:
            bid = Decimal(str(max_bid_raw))
        except InvalidOperation:
            raise _RowValidationError(f"invalid max_bid: {max_bid_raw!r}")
        if not bid.is_finite():
            # Decimal("nan")/Decimal("inf") parse without error (unlike
            # InvalidOperation above) and can bypass a naive server-side
            # "v <= 0" positivity check (NaN compares False either way) —
            # reject client-side before it reaches a real-money bid field.
            raise _RowValidationError(f"invalid max_bid: {max_bid_raw!r} (not finite)")

        offset = _optional_int(row, "offset", 6)
        group = _optional_int(row, "group", 0)
        comic_id = row.get("comic_id")
        if comic_id is not None:
            comic_id = _optional_int(row, "comic_id", 0)
        grade = _optional_float(row, "grade")
        seller = row.get("seller")
        seller_grade = _optional_float(row, "seller_grade")
        photo_grade = _optional_float(row, "photo_grade")
    except _RowValidationError as e:
        return RowResult(item_id=item_id, status=STATUS_FAILED, error=str(e))

    payload = build_bid_payload(
        item_id, bid, offset, group,
        seller=seller, seller_grade=seller_grade, photo_grade=photo_grade,
    )

    ok, resp, err = server_request("post", "/api/bids", json=payload)
    if not ok:
        return RowResult(
            item_id=item_id, status=STATUS_FAILED, max_bid=float(bid),
            grade=grade, error=err,
        )

    created = created_from_response(resp)
    status = STATUS_ADDED if created else STATUS_UPDATED
    result = RowResult(
        item_id=item_id, status=status, max_bid=float(bid), grade=grade,
        created=created,
    )

    link_attempted = grade is not None and comic_id is not None
    if link_attempted:
        result.link_attempted = True
        link_ok, _link_resp, link_err = server_request(
            "post",
            f"/api/bids/{item_id}/link-fmv",
            json={"comic_id": comic_id, "grade": grade},
        )
        result.link_ok = link_ok
        if not link_ok:
            # A link failure is tracked separately from `error` (which is
            # reserved for an add-call failure / STATUS_FAILED) — the snipe
            # itself landed (matches `gixen add`'s single-item behavior,
            # which still exits 0 when only the link-fmv call fails), so a
            # consumer scanning for `error is not None` as "this row failed"
            # must not catch a merely-unlinked-but-added row.
            result.link_error = link_err

    return result


def run_batch(
    rows: list[dict],
    *,
    server_request: ServerRequestFn,
    health_check: Callable[[], bool],
) -> BatchOutcome:
    """Run every row strictly sequentially (Gixen sessions are stateful —
    parallel adds fail). On any row FAILED, re-check server health before
    the next row; if the server is down, halt and mark every remaining row
    NOT_ATTEMPTED without another network call (BUI-168)."""
    results: list[RowResult] = []
    halted = False

    for row in rows:
        if halted:
            results.append(
                RowResult(item_id=row.get("item_id"), status=STATUS_NOT_ATTEMPTED)
            )
            continue

        result = add_one_row(row, server_request=server_request)
        results.append(result)

        if result.status == STATUS_FAILED and not health_check():
            halted = True

    return BatchOutcome(rows=results, halted=halted)


def verify_items(outcome: BatchOutcome) -> list[dict]:
    """Build /api/comics/verify's `items` payload for every landed row that
    carries a grade — a row with no grade can't be matched to an fmv row
    (verify.md's own schema requires it), so gradeless rows are skipped
    rather than sent in as a guaranteed no_fmv_at_grade."""
    return [
        {"item_id": r.item_id, "grade": r.grade}
        for r in outcome.rows
        if r.status in _TERMINAL_OK_STATUSES and r.grade is not None
    ]


def apply_verify_results(outcome: BatchOutcome, verify_response: dict) -> None:
    """Splice /api/comics/verify's per-item verdicts back onto the matching
    RowResult by item_id (first match wins — a working list has at most one
    row per item_id in the add-batch context)."""
    by_item_id = {}
    for r in verify_response.get("results", []):
        by_item_id.setdefault(r.get("item_id"), r)
    for row in outcome.rows:
        if row.item_id in by_item_id:
            row.verify = by_item_id[row.item_id]
