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

import json
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
    # BUI-506: display-only — the comic's title, threaded through from the
    # row (which build_batch_rows populated from the working list, or a
    # hand-written add-batch ROWS_FILE that includes it directly). Never sent
    # to the server (POST /api/bids has no such field); it exists purely so
    # add-batch's human table and JSON summary can show a name instead of a
    # bare item_id, replacing the /comic:buy Step 5 orchestrator's old
    # in-context "reformat with the comic names ... joined by item_id" pass.
    title: str | None = None

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
            "title": self.title,
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
        raise _RowValidationError(f"invalid {field_name}: {value!r}") from None
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
        raise _RowValidationError(f"invalid {field_name}: {value!r}") from None


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
    # BUI-506: display-only, never part of the POST /api/bids payload — read
    # unconditionally (no validation) so it survives even a row that fails
    # validation before reaching the network.
    title = row.get("title")

    try:
        item_id = _require(row, "item_id")
        max_bid_raw = _require(row, "max_bid")
        try:
            bid = Decimal(str(max_bid_raw))
        except InvalidOperation:
            raise _RowValidationError(f"invalid max_bid: {max_bid_raw!r}") from None
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
        return RowResult(item_id=item_id, status=STATUS_FAILED, error=str(e), title=title)

    payload = build_bid_payload(
        item_id, bid, offset, group,
        seller=seller, seller_grade=seller_grade, photo_grade=photo_grade,
    )

    ok, resp, err = server_request("post", "/api/bids", json=payload)
    if not ok:
        return RowResult(
            item_id=item_id, status=STATUS_FAILED, max_bid=float(bid),
            grade=grade, error=err, title=title,
        )

    created = created_from_response(resp)
    status = STATUS_ADDED if created else STATUS_UPDATED
    result = RowResult(
        item_id=item_id, status=status, max_bid=float(bid), grade=grade,
        created=created, title=title,
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
                RowResult(item_id=row.get("item_id"), status=STATUS_NOT_ATTEMPTED,
                          title=row.get("title"))
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


# ─── BUI-435: gixen build-batch — deterministic Step-5 row assembly ─────────
#
# /comic:buy Step 5 used to have the model hand-merge three sources in
# context — the comic-fmv --brief lines (item_id -> comic_id/max_bid), the
# working list (grade/seller/seller_grade/photo_grade/listing type), and the
# Step 4 user-approved overrides (max_bid/group/skip) — into the add-batch
# rows schema. Dropping comic_id during that hand-merge is exactly the
# recurring PER-140 "bids.comic_id/fmv_id NULL" bug. This section makes the
# merge a tested, deterministic transform instead: `build_batch_rows` never
# drops comic_id silently, never fabricates a max_bid for a needs-manual row,
# and fails loudly (AddBatchError) on any input it cannot honestly resolve.

# The standard CGC/Overstreet letter-grade scale, intentionally duplicated
# (not imported) from apps/fmv/src/fmv_runner.py's `_LETTER_GRADE_MAP`:
# gixen-cli is a uv workspace member, apps/fmv is not (uv-tool-install-managed,
# PATH-only console script — see root CLAUDE.md "The FMV pipeline shells out
# across package boundaries"), so there is no importable path between them.
# This is a fixed external industry scale, not app logic, so duplicating the
# table is lower-risk than reaching across the workspace boundary for it.
_LETTER_GRADE_MAP: dict[str, float] = {
    "NM/M": 9.8, "NM+": 9.6, "NM": 9.4, "NM-": 9.2,
    "VF/NM": 9.0, "VFNM": 9.0,
    "VF+": 8.5, "VF": 8.0, "VF-": 7.5,
    "FN/VF": 7.0, "FN+": 6.5, "FN": 6.0, "FN-": 5.5,
    "VG/FN": 5.0, "VG+": 4.5, "VG": 4.0, "VG-": 3.5,
    "GD/VG": 3.0, "GD+": 2.5, "GD": 2.0,
    "FR": 1.0, "PR": 0.5,
}

_BIN_TYPE_VALUES = {"bin", "buy it now", "buy-it-now"}


def _coerce_grade_value(value: Any, field_name: str) -> float | None:
    """Numeric passthrough, or a CGC letter grade (e.g. "NM-") -> float.
    `None` passes through as `None` (grade not supplied). Raises
    _RowValidationError for anything else — a grade that is neither numeric
    nor a recognized letter grade must fail loudly, not silently vanish
    (an unlinked grade breaks the FMV link the same way a dropped comic_id
    does). Delegates the actual numeric parse/finite-check to
    `_optional_float` (one source of truth for "coerce a JSON value to a
    finite float") and only adds the letter-grade fallback on top."""
    if isinstance(value, str):
        s = value.strip()
        try:
            return _optional_float({field_name: s}, field_name)
        except _RowValidationError:
            mapped = _LETTER_GRADE_MAP.get(s.upper())
            if mapped is None:
                raise _RowValidationError(
                    f"unrecognized {field_name}: {value!r} (not numeric and "
                    "not a known CGC letter grade)"
                ) from None
            return mapped
    return _optional_float({field_name: value}, field_name)


def _is_bin_listing(row: dict) -> bool:
    value = row.get("listing_type", row.get("type"))
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _BIN_TYPE_VALUES


def _resolve_max_bid(brief_row: dict, row_overrides: dict) -> float | None:
    """The override's max_bid always wins when present and non-null;
    otherwise fall back to the brief's CLI-computed max_bid (which is null
    for a needs-manual row — the caller decides what null means). Delegates
    the parse/finite-check to `_optional_float` and adds only the
    incremental positivity check a real-money bid needs."""
    override_value = row_overrides.get("max_bid")
    raw = override_value if override_value is not None else brief_row.get("max_bid")
    value = _optional_float({"max_bid": raw}, "max_bid")
    if value is not None and value <= 0:
        raise _RowValidationError(
            f"invalid max_bid: {raw!r} (must be a positive finite number)"
        )
    return value


def _resolve_group(row: dict, row_overrides: dict) -> int:
    """The override's group always wins when present and non-null;
    otherwise the working-list row's own default (from a Step 2 bid-group
    candidate marking), defaulting to 0 (no group). Delegates the parse to
    `_optional_int` and adds only the incremental 0-10 range check."""
    override_value = row_overrides.get("group")
    raw = override_value if override_value is not None else row.get("group", 0)
    value = _optional_int({"group": raw}, "group", 0)
    if not (0 <= value <= 10):
        raise _RowValidationError(f"invalid group: {raw!r} (must be 0-10)")
    return value


def _index_rows_by_item_id(rows: list, *, singular: str, plural: str) -> dict[str, dict]:
    """Validate and index a list of row dicts by item_id: every row must be
    a JSON object with a non-empty item_id, unique within this list. Shared
    by `parse_brief_rows` and both of `build_batch_rows`'s validation passes
    (brief rows, working-list rows) so "must be a dict / must have item_id /
    item_id must be unique" isn't retyped at each call site. `singular`
    names one row in an error (e.g. "brief row"); `plural` names the whole
    list in the duplicate-item_id error (e.g. "brief rows")."""
    by_item_id: dict[str, dict] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AddBatchError(f"{singular} {i} is not a JSON object: {row!r}")
        item_id = row.get("item_id")
        if not item_id:
            raise AddBatchError(f"{singular} {i} is missing item_id: {row!r}")
        if item_id in by_item_id:
            raise AddBatchError(f"duplicate item_id in {plural}: {item_id!r}")
        by_item_id[item_id] = row
    return by_item_id


def parse_brief_rows(raw: Any) -> list[dict]:
    """Parse `comic-fmv --brief` output into a list of row dicts. Accepts:

      - a bare JSON list of row dicts, or a dict with a top-level "rows" or
        "brief" list (mirrors `parse_rows`' forgiving shape), or
      - the raw captured stdout of `comic-fmv --brief` as a single string
        (human table + one JSON object per line) — each line is checked: a
        line starting with "{" is parsed as JSON; anything else is treated
        as human-table text and ignored. A line that *starts* with "{" but
        fails to parse is a hard AddBatchError (almost certainly a
        truncated/corrupted brief line — silently dropping it would
        silently drop that row's comic_id, PER-140), not a soft skip.

    Raises AddBatchError for a structurally unusable input, a row missing
    `item_id`, or duplicate `item_id`s across brief rows (ambiguous which
    comic_id/max_bid is authoritative for that item)."""
    rows: list[Any]

    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = None
        for key in ("rows", "brief"):
            if key in raw:
                rows = raw[key]
                break
        if rows is None:
            raise AddBatchError(
                'brief input object must have a top-level "rows" or "brief" list'
            )
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return parse_brief_rows(parsed)
        collected: list[dict] = []
        for i, line in enumerate(raw.splitlines()):
            stripped = line.strip()
            if not stripped or not stripped.startswith("{"):
                continue  # human-readable table output, not a brief line
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise AddBatchError(
                    f"brief input line {i} looks like JSON but failed to "
                    f"parse (likely truncated): {stripped!r} ({e})"
                ) from None
            collected.append(obj)
        if not collected:
            raise AddBatchError(
                "no brief JSON lines found in input (expected one JSON "
                "object per line, as printed by `comic-fmv --brief`)"
            )
        rows = collected
    else:
        raise AddBatchError(
            "brief input must be a JSON list, an object with a rows/brief "
            "list, or comic-fmv --brief's captured stdout"
        )

    if not isinstance(rows, list):
        raise AddBatchError('brief input "rows"/"brief" value must be a list')

    by_item_id = _index_rows_by_item_id(rows, singular="brief row", plural="brief rows")
    return list(by_item_id.values())


@dataclass
class BuildBatchResult:
    rows: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # {"item_id","reason"}
    unlinked: list[dict] = field(default_factory=list)  # {"item_id","reason"}


def build_batch_rows(
    brief_rows: list[dict],
    working_list: list[dict],
    overrides: dict[str, dict] | None = None,
) -> BuildBatchResult:
    """Deterministically assemble `gixen add-batch` rows (BUI-435) from the
    three sources /comic:buy's Step 5 used to hand-merge in context:

      - `brief_rows` — comic-fmv --brief output (see `parse_brief_rows`):
        one dict per item_id with {item_id, comic_id, fmv_id, max_bid,
        flag_reason, confidence}.
      - `working_list` — the buy-flow working list, one dict per surviving
        comic: {item_id, title?, grade?, listing_type?/type?, seller?,
        seller_grade?, photo_grade?, group?}. `grade`/`seller_grade`/
        `photo_grade` may be numeric or a CGC letter grade (e.g. "NM-").
        `group` here is the Step 2 bid-group-candidate default. A row whose
        `listing_type`/`type` is "BIN" (case-insensitive) is skipped
        entirely — Gixen is for auctions only. `title` (BUI-506), when
        present, is carried straight into the output row unchanged and
        purely for display — it lets `add-batch` show the comic's name in
        its human table and JSON summary instead of a bare item_id, closing
        the /comic:buy Step 5 orchestrator's old in-context "reformat with
        the comic names ... joined by item_id" pass. Absent `title` is fully
        backward-compatible: the output row simply omits the key, exactly as
        it did before this field existed.
      - `overrides` — optional {item_id: {"max_bid": ..., "group": ...,
        "skip": bool}}, the Step 4 user-approval gate's per-row overrides.
        A present, non-null `max_bid`/`group` here always wins over the
        brief/working-list default; `skip: true` drops that item_id from
        the batch entirely (and from every other check below, including the
        "must have a matching brief row" check).

    Never drops `comic_id` silently (PER-140): a working-list item_id with
    no matching brief row is a hard AddBatchError unless overridden `skip`.
    A brief row whose `comic_id` is null (FMV upsert skipped, e.g. n=0)
    still lands the bid — `comic_id`/`grade` are simply omitted from that
    row (matching the existing add/add-batch contract: FMV linking only
    fires when both are present) — and it is reported in `.unlinked`, never
    silently.

    A needs-manual row (`flag_reason` set, brief `max_bid` null) with no
    override `max_bid` and no `skip` is a hard AddBatchError — there is no
    honest number to bid, and this function never fabricates one.

    Raises AddBatchError for any structurally invalid input: a working-list
    row (or override) missing/duplicating `item_id`, an override key that
    doesn't match any working-list row, or an unresolvable max_bid/group/
    grade value.
    """
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise AddBatchError(f"overrides must be a JSON object keyed by item_id: {overrides!r}")
    for item_id, ov in overrides.items():
        if not isinstance(ov, dict):
            raise AddBatchError(
                f"override for item_id {item_id!r} must be a JSON object: {ov!r}"
            )

    if not isinstance(brief_rows, list):
        raise AddBatchError(f"brief_rows must be a list of row objects: {brief_rows!r}")
    if not isinstance(working_list, list):
        raise AddBatchError(f"working_list must be a list of row objects: {working_list!r}")

    brief_by_item = _index_rows_by_item_id(brief_rows, singular="brief row", plural="brief rows")
    working_by_item = _index_rows_by_item_id(
        working_list, singular="working-list row", plural="working list"
    )
    working_item_ids = set(working_by_item.keys())

    unknown_overrides = set(overrides) - working_item_ids
    if unknown_overrides:
        raise AddBatchError(
            "override(s) reference item_id(s) not present in the working "
            f"list: {sorted(unknown_overrides)}"
        )

    result = BuildBatchResult()

    for row in working_list:
        item_id = row["item_id"]
        row_overrides = overrides.get(item_id) or {}

        if row_overrides.get("skip"):
            result.skipped.append({"item_id": item_id, "reason": "user_skip"})
            continue

        if _is_bin_listing(row):
            result.skipped.append({"item_id": item_id, "reason": "bin"})
            continue

        brief_row = brief_by_item.get(item_id)
        if brief_row is None:
            raise AddBatchError(
                f"item_id {item_id!r} has no matching comic-fmv --brief row "
                "(never priced) — pass an override with skip=true if this "
                "row is intentionally excluded; never omit it silently"
            )

        try:
            max_bid = _resolve_max_bid(brief_row, row_overrides)
            group = _resolve_group(row, row_overrides)
            grade = _coerce_grade_value(row.get("grade"), "grade")
            seller_grade = _coerce_grade_value(row.get("seller_grade"), "seller_grade")
            photo_grade = _coerce_grade_value(row.get("photo_grade"), "photo_grade")
        except _RowValidationError as e:
            raise AddBatchError(f"item_id {item_id!r}: {e}") from None

        if max_bid is None:
            raise AddBatchError(
                f"item_id {item_id!r} is needs-manual (flag_reason="
                f"{brief_row.get('flag_reason')!r}) with no override max_bid "
                "and no skip — hand-price it (override.max_bid) or skip it "
                "explicitly (override.skip); a needs-manual row is never "
                "auto-priced"
            )

        out_row: dict[str, Any] = {"item_id": item_id, "max_bid": max_bid}

        title = row.get("title")
        if title is not None:
            out_row["title"] = title

        comic_id = brief_row.get("comic_id")
        if comic_id is not None:
            out_row["comic_id"] = comic_id
            if grade is not None:
                out_row["grade"] = grade
        else:
            result.unlinked.append({"item_id": item_id, "reason": "comic_id_null"})

        if group:
            out_row["group"] = group

        seller = row.get("seller")
        if seller is not None:
            out_row["seller"] = seller
        if seller_grade is not None:
            out_row["seller_grade"] = seller_grade
        if photo_grade is not None:
            out_row["photo_grade"] = photo_grade

        result.rows.append(out_row)

    return result
