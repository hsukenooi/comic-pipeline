"""Excel import and collection merge pipeline for the local collection cache."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from locg.config import wish_list_cache_path
from locg.parsing import trailing_issue_token

from locg.collection_cache import (
    LOCG_COLUMNS,
    USER_MANAGED_COLUMNS,
    CollectionCache,
    _next_seq,
    _normalize_series_key,
    _utcnow_iso,
    make_identity,
    rebuild_series_name_index,
)

logger = logging.getLogger("locg")

# Maximum file size accepted before parsing (R10)
MAX_XLSX_BYTES = 10 * 1024 * 1024  # 10 MB

# Expected Excel header row in canonical order
LOCG_XLSX_HEADERS: tuple[str, ...] = (
    "Publisher Name",
    "Series Name",
    "Full Title",
    "Release Date",
    "In Collection",
    "In Wish List",
    "Marked Read",
    "My Rating",
    "Media Format",
    "Price Paid",
    "Date Purchased",
    "Condition",
    "Notes",
    "Tags",
    "Storage Box",
    "Owner",
    "Purchase Store",
    "Signature",
    "Slabbing",
    "Grading",
    "Grading Company",
)

# Map from Excel header to snake_case field name
_HEADER_TO_FIELD: dict[str, str] = dict(zip(LOCG_XLSX_HEADERS, LOCG_COLUMNS))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_xlsx(path: Path) -> list[dict[str, Any]]:
    """Parse a LOCG Excel export into a list of row dicts.

    Validates file size and header row before reading any data.
    Returns rows with LOCG_COLUMNS keys and raw cell values.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel file not found: {path}")

    size = path.stat().st_size
    if size > MAX_XLSX_BYTES:
        raise RuntimeError(
            f"Excel file is {size / (1024 * 1024):.1f} MB — exceeds the 10 MB limit."
        )

    import openpyxl  # Lazy import

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    try:
        rows_iter = ws.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if header_row is None:
            raise RuntimeError("Excel file is empty.")

        actual_headers = tuple(str(h).strip() if h is not None else "" for h in header_row)
        if actual_headers != LOCG_XLSX_HEADERS:
            raise RuntimeError(
                f"Excel header row does not match expected LOCG format.\n"
                f"Expected: {LOCG_XLSX_HEADERS}\n"
                f"Got:      {actual_headers}"
            )

        rows: list[dict[str, Any]] = []
        for raw in rows_iter:
            row: dict[str, Any] = {}
            for header, field in _HEADER_TO_FIELD.items():
                idx = LOCG_XLSX_HEADERS.index(header)
                row[field] = raw[idx] if idx < len(raw) else None
            rows.append(row)
    finally:
        wb.close()

    return rows


# ---------------------------------------------------------------------------
# Reconciliation heuristic (R60)
# ---------------------------------------------------------------------------

_VOL_ANNOTATION_RE = re.compile(r"\(Vol\.\s*\d+\)", re.IGNORECASE)

# BUI-189: the trailing issue-token extractor is the shared parser in
# locg.parsing — its narrow local copy here still had the BUI-175 truncation
# (decimal/point issues like "#1.MU" parsed to None in reconciliation). The
# shared version keeps the same end-anchored "trailing #N only" semantics while
# capturing the full token.
_issue_token = trailing_issue_token


def _publisher_matches(a: str, b: str) -> bool:
    # A missing publisher on either side is a wildcard, not a mismatch. agent_win
    # rows are written with publisher_name=None (record-win has no publisher), so
    # a strict compare against LOCG's canonical "Marvel Comics" would score every
    # such row 0 and block reconciliation entirely (BUI-122). Series + issue +
    # year still gate the match, so the publisher wildcard can't merge across
    # genuinely different books. Only reject when BOTH sides name a publisher and
    # they differ.
    na = (a or "").strip().lower()
    nb = (b or "").strip().lower()
    if not na or not nb:
        return True
    return na == nb


def _series_normalized_matches(a: str, b: str) -> bool:
    return _normalize_series_key(a) == _normalize_series_key(b)


def _vol_annotation_differs(a: str, b: str) -> bool:
    """True if the (Vol. N) annotations are both present but differ, or only one has one."""
    va = _VOL_ANNOTATION_RE.search(a or "")
    vb = _VOL_ANNOTATION_RE.search(b or "")
    if va is None and vb is None:
        return False
    if va is None or vb is None:
        return True
    return va.group(0).lower() != vb.group(0).lower()


def _reconcile_score(cache_row: dict[str, Any], xlsx_row: dict[str, Any]) -> int:
    """Reconciliation score for Phase 1.

    Returns -1 for hard mismatch (do not reconcile), 0 for no match,
    positive for a match (higher = stronger).
    """
    if _vol_annotation_differs(
        cache_row.get("series_name", ""), xlsx_row.get("series_name", "")
    ):
        return -1  # Hard mismatch per R60

    if not _publisher_matches(
        cache_row.get("publisher_name", ""), xlsx_row.get("publisher_name", "")
    ):
        return 0

    if not _series_normalized_matches(
        cache_row.get("series_name", ""), xlsx_row.get("series_name", "")
    ):
        return 0

    cache_title = (cache_row.get("full_title") or "").strip()
    xlsx_title = (xlsx_row.get("full_title") or "").strip()

    # TPB / HC / OGN: no '#N' token → case-insensitive full_title match
    if _issue_token(cache_title) is None and _issue_token(xlsx_title) is None:
        return 10 if cache_title.lower() == xlsx_title.lower() else 0

    cache_token = _issue_token(cache_title)
    xlsx_token = _issue_token(xlsx_title)

    if cache_token is None or xlsx_token is None:
        return 0  # One has a token, the other doesn't

    # String compare per R60 — "Annual 1" ≠ "1"
    if cache_token != xlsx_token:
        return 0

    # Year must match if both present
    cache_year = (cache_row.get("release_date") or "")[:4]
    xlsx_year = (xlsx_row.get("release_date") or "")[:4]
    if cache_year and xlsx_year and cache_year != xlsx_year:
        return 0

    return 5


# ---------------------------------------------------------------------------
# Behavioral drift checksum (F5)
# ---------------------------------------------------------------------------

def _user_column_checksum(row: dict[str, Any]) -> str:
    values = {col: row.get(col) for col in USER_MANAGED_COLUMNS}
    blob = json.dumps(values, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Partial identity for rename detection (R67)
# ---------------------------------------------------------------------------

def _partial_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """(publisher_name, series_name, release_date) — used to detect full_title renames."""
    return (
        row.get("publisher_name") or "",
        row.get("series_name") or "",
        row.get("release_date") or "",
    )


# ---------------------------------------------------------------------------
# Ownership-downgrade guard (BUI-124)
# ---------------------------------------------------------------------------

def _apply_locg_columns_held(
    existing: dict[str, Any],
    xlsx_row: dict[str, Any],
    now: str,
    audit_records: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    """Copy an export row's LOCG columns onto an existing cache row, but HOLD an
    ownership downgrade (BUI-124).

    The gixen server is the source of truth (BUI-87), so a stale or bad LOCG
    state must not silently un-own a book: if the existing row is owned
    (``in_collection`` truthy) and the incoming export says not-owned
    (``in_collection`` falsy), the previous value is preserved, an
    ``ownership_downgrade_held`` audit record is written, and the run counter is
    bumped — rather than overwriting and risking a duplicate buy via
    ``collection-check``. Every other column still updates normally, and a
    legitimate count change that stays owned (e.g. 2 → 1) copies through.

    A genuine un-collect (you sold a book and un-collected it on LOCG) is a real
    downgrade; it surfaces in the audit log / ``collection status`` for review
    rather than being auto-applied. ``in_collection`` is not a user-managed
    column, so holding it does not affect behavioral-drift detection.
    """
    prev_in_collection = existing.get("in_collection")
    held = bool(prev_in_collection) and not bool(xlsx_row.get("in_collection"))

    for col in LOCG_COLUMNS:
        existing[col] = xlsx_row[col]

    if held:
        existing["in_collection"] = prev_in_collection
        audit_records.append({
            "type": "ownership_downgrade_held",
            "ts": now,
            "command": "import",
            "details": {
                "identity": list(make_identity(existing)),
                "full_title": existing.get("full_title"),
                "previous_in_collection": prev_in_collection,
                "incoming_in_collection": xlsx_row.get("in_collection"),
            },
        })
        summary["ownership_downgrades_held"] += 1


# ---------------------------------------------------------------------------
# Wish-list cache write
# ---------------------------------------------------------------------------

def _local_only_wish_items(path: Path) -> list[dict[str, Any]]:
    """Existing wish-list entries that were added locally (BUI-47).

    Entries created by ``locg wish-list add`` carry only ``name``/``id``;
    export-derived entries always carry a ``series_name``. Anything without a
    ``series_name`` is therefore a local-only add that hasn't round-tripped
    through a LOCG export yet.
    """
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return [it for it in data.get("items", []) if not it.get("series_name")]


def _write_wish_list_cache(wish_rows: list[dict[str, Any]]) -> None:
    """Atomically (re)write wish-list.json from imported collection rows.

    Preserves local-only ``wish-list add`` entries (BUI-47): the export-derived
    set is rebuilt from the incoming rows, then any local-only entry (no
    ``series_name``) whose name isn't already covered is re-appended, so manual
    adds survive imports instead of being silently dropped. Once an add
    round-trips through a LOCG export it appears in ``wish_rows`` and the
    local copy is deduped out by name.

    Follows the IDCache._save() pattern: tempfile + os.replace + chmod 600.
    No fsync needed — this is a best-effort read-only cache.
    """
    path = wish_list_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    items = [
        {
            "name": row.get("full_title") or "",
            "id": None,
            "series_name": row.get("series_name"),
            "publisher_name": row.get("publisher_name"),
            "release_date": row.get("release_date"),
            "media_format": row.get("media_format"),
        }
        for row in wish_rows
    ]
    covered = {it["name"] for it in items}
    for local in _local_only_wish_items(path):
        name = local.get("name") or ""
        if name and name not in covered:
            items.append(local)
            covered.add(name)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    fd, tmp = tempfile.mkstemp(
        prefix=".wish-list-", suffix=".json.tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Main import orchestration
# ---------------------------------------------------------------------------

def import_xlsx(path: Path, cache: CollectionCache) -> dict[str, Any]:
    """Parse a LOCG Excel export and merge it into the cache.

    Two-phase pipeline:
    1. Reconciliation: match flagged agent_win rows against incoming rows via
       relaxed heuristic; rewrite identity and clear manual flags.
    2. Standard merge: insert-or-update by identity tuple; detect renames (R67)
       and preserve previous_full_title for one cycle.

    Appends audit records to import-history.jsonl.

    Returns a summary dict: {added, updated, untouched, reconciled,
    possibly_removed, ownership_downgrades_held, behavioral_drift_count,
    warnings}.
    """
    # Validate and parse outside the lock — bad files abort cleanly
    xlsx_rows = parse_xlsx(path)
    now = _utcnow_iso()

    summary: dict[str, Any] = {
        "added": 0,
        "updated": 0,
        "untouched": 0,
        "reconciled": 0,
        "possibly_removed": 0,
        "ownership_downgrades_held": 0,
        "behavioral_drift_count": 0,
        "warnings": [],
    }

    # Collect audit records to append after each merge step.
    # append_audit is called inside the mutate_fn (safe: uses a different file).
    audit_records: list[dict[str, Any]] = []
    # Wish-list rows are captured inside do_merge and written after apply() succeeds,
    # so both caches only update when the full import completes successfully.
    wish_rows: list[dict[str, Any]] = []

    def do_merge(payload: dict[str, Any]) -> None:
        nonlocal wish_rows
        comics = payload["comics"]

        # Full identity index: (publisher, series, full_title, release_date) → idx
        identity_to_idx: dict[tuple, int] = {}
        # Partial identity index for rename detection: (publisher, series, release_date) → idx
        partial_to_idx: dict[tuple, int] = {}
        for i, row in enumerate(comics):
            identity_to_idx[make_identity(row)] = i
            partial_to_idx[_partial_identity(row)] = i

        # ----- Phase 1: Reconciliation ----------------------------------------
        # Manually-flagged best-guess rows always get the relaxed (exact-year)
        # heuristic. BUI-122: also run it for *unflagged* pending agent_win rows
        # whose exact identity is absent from this export. LOCG silently rewrites
        # a just-pushed row's Release Date to its own canonical value (see
        # docs/solutions/integration-issues/locg-bulk-import-recipe-2026-05-22.md),
        # which breaks the Phase-2 exact make_identity match; the row would then
        # insert as a duplicate while the original stayed pending forever. The
        # `make_identity(r) not in exact_ids` guard preserves Phase-2 exact-match
        # primacy — a win whose date round-tripped unchanged is handled by the
        # standard merge, not routed through year-tolerant scoring (which would
        # mis-flag it ambiguous against a same-year variant).
        exact_ids = {make_identity(xr) for xr in xlsx_rows}
        flagged_indices = [
            i for i, r in enumerate(comics)
            if r.get("needs_manual_variant")
            or r.get("needs_manual_series_canonical")
            or (
                r.get("source") == "agent_win"
                and r.get("pushed_to_locg_at") is None
                and make_identity(r) not in exact_ids
            )
        ]

        for ci in flagged_indices:
            cache_row = comics[ci]
            candidates: list[tuple[int, int, dict]] = []

            for xi, xr in enumerate(xlsx_rows):
                score = _reconcile_score(cache_row, xr)
                if score > 0:
                    candidates.append((score, xi, xr))

            if not candidates:
                continue

            if len(candidates) > 1:
                # Multi-match: leave all flagged, log ambiguous
                audit_records.append({
                    "type": "ambiguous_reconciliation",
                    "ts": now,
                    "command": "import",
                    "details": {
                        "full_title": cache_row.get("full_title"),
                        "candidate_count": len(candidates),
                    },
                })
                summary["warnings"].append(
                    f"Ambiguous reconciliation for '{cache_row.get('full_title')}'"
                )
                continue

            _score, _xi, xlsx_row = candidates[0]

            # Collision guard (BUI-122): rewriting this row's identity to the
            # matched export row's identity must not land on an identity another
            # cache row already holds — that would create a duplicate-identity
            # pair. This happens when the row is a win for a book already owned
            # under LOCG's canonical identity (the agent_win row and the existing
            # locg_export row are the same comic). Leave it pending and surface it
            # rather than silently merging or duplicating (visible non-clear over
            # silent wrong merge). The pre-existing duplicate-records condition is
            # then resolved out-of-band (see the sync runbook's cleanup section).
            target_identity = make_identity(xlsx_row)
            collide = identity_to_idx.get(target_identity)
            if collide is not None and collide != ci:
                audit_records.append({
                    "type": "ambiguous_reconciliation",
                    "ts": now,
                    "command": "import",
                    "details": {
                        "full_title": cache_row.get("full_title"),
                        "reason": "identity_collision_with_existing_row",
                    },
                })
                summary["warnings"].append(
                    f"Reconciliation collision for '{cache_row.get('full_title')}' "
                    "— a row with that identity already exists; left pending"
                )
                continue

            old_identity = make_identity(cache_row)
            old_partial = _partial_identity(cache_row)

            cache_row["publisher_name"] = xlsx_row["publisher_name"]
            cache_row["series_name"] = xlsx_row["series_name"]
            cache_row["full_title"] = xlsx_row["full_title"]
            cache_row["release_date"] = xlsx_row["release_date"]
            cache_row["needs_manual_variant"] = False
            cache_row["needs_manual_series_canonical"] = False
            cache_row["source"] = "locg_export"
            cache_row["last_seen_in_export_at"] = now
            cache_row["pushed_to_locg_at"] = cache_row.get("pushed_to_locg_at") or now

            # Update indices
            if old_identity in identity_to_idx:
                del identity_to_idx[old_identity]
            if old_partial in partial_to_idx:
                del partial_to_idx[old_partial]
            identity_to_idx[make_identity(cache_row)] = ci
            partial_to_idx[_partial_identity(cache_row)] = ci

            summary["reconciled"] += 1
            audit_records.append({
                "type": "reconciliation",
                "ts": now,
                "command": "import",
                "details": {
                    "old_identity": list(old_identity),
                    "new_identity": list(make_identity(cache_row)),
                },
            })

        # ----- Phase 2: Standard merge ----------------------------------------
        # Record how many comics existed BEFORE this import so we only check
        # pre-import rows for rename detection — new insertions in this same
        # loop must never trigger spurious renames.
        pre_import_count = len(comics)
        xlsx_identities: set[tuple] = set()

        for xr in xlsx_rows:
            row_identity = make_identity(xr)
            xlsx_identities.add(row_identity)

            if row_identity in identity_to_idx:
                # Update existing row
                ci = identity_to_idx[row_identity]
                existing = comics[ci]

                # Capture user-managed values BEFORE overwriting with xlsx data
                pre_user_values = {col: existing.get(col) for col in USER_MANAGED_COLUMNS}
                pre_checksum = _user_column_checksum(existing)

                # Remove from partial_to_idx so it won't be found as a rename
                # candidate by a later xlsx row with the same partial identity
                old_partial = _partial_identity(existing)
                if partial_to_idx.get(old_partial) == ci:
                    del partial_to_idx[old_partial]

                _apply_locg_columns_held(existing, xr, now, audit_records, summary)
                existing["last_seen_in_export_at"] = now
                existing["source"] = "locg_export"
                if existing.get("pushed_to_locg_at") is None:
                    existing["pushed_to_locg_at"] = now

                post_checksum = _user_column_checksum(existing)
                if pre_checksum != post_checksum:
                    changed = [
                        col for col in USER_MANAGED_COLUMNS
                        if pre_user_values.get(col) != xr.get(col)
                    ]
                    if changed:
                        audit_records.append({
                            "type": "behavioral_drift",
                            "ts": now,
                            "command": "import",
                            "details": {
                                "identity": list(row_identity),
                                "columns_changed": changed,
                            },
                        })
                        summary["behavioral_drift_count"] += 1

                summary["updated"] += 1

            else:
                # Check for rename: same (publisher, series, release_date), different
                # full_title (R67) — only against pre-import rows, never new inserts
                row_partial = _partial_identity(xr)
                if row_partial in partial_to_idx:
                    ci = partial_to_idx[row_partial]
                    if ci < pre_import_count:
                        existing = comics[ci]
                        old_title = existing.get("full_title") or ""
                        new_title = xr.get("full_title") or ""
                        if old_title and new_title and old_title != new_title:
                            old_identity = make_identity(existing)
                            existing["previous_full_title"] = old_title

                            pre_checksum = _user_column_checksum(existing)
                            _apply_locg_columns_held(
                                existing, xr, now, audit_records, summary
                            )
                            existing["last_seen_in_export_at"] = now
                            existing["source"] = "locg_export"
                            if existing.get("pushed_to_locg_at") is None:
                                existing["pushed_to_locg_at"] = now
                            post_checksum = _user_column_checksum(existing)

                            if old_identity in identity_to_idx:
                                del identity_to_idx[old_identity]
                            identity_to_idx[make_identity(existing)] = ci
                            # Consume the partial slot so it won't match again
                            del partial_to_idx[row_partial]

                            if pre_checksum != post_checksum:
                                changed = [
                                    col for col in USER_MANAGED_COLUMNS
                                    if existing.get(col) != xr.get(col)
                                ]
                                if changed:
                                    audit_records.append({
                                        "type": "behavioral_drift",
                                        "ts": now,
                                        "command": "import",
                                        "details": {
                                            "identity": list(make_identity(existing)),
                                            "columns_changed": changed,
                                        },
                                    })
                                    summary["behavioral_drift_count"] += 1

                            audit_records.append({
                                "type": "renamed_full_title",
                                "ts": now,
                                "command": "import",
                                "details": {
                                    "old_title": old_title,
                                    "new_title": new_title,
                                    "identity": list(make_identity(existing)),
                                },
                            })
                            summary["updated"] += 1
                            continue

                # Genuine new row from LOCG — do NOT add to partial_to_idx to
                # avoid triggering rename detection for subsequent xlsx rows
                new_row: dict[str, Any] = dict(xr)
                new_row["local_added_at"] = now
                new_row["local_added_seq"] = _next_seq()
                new_row["pushed_to_locg_at"] = now
                new_row["last_seen_in_export_at"] = now
                new_row["source"] = "locg_export"
                new_row["needs_manual_variant"] = False
                new_row["needs_manual_series_canonical"] = False
                new_row["metron_id"] = None
                new_row["gixen_item_id"] = None
                new_row["previous_full_title"] = None
                comics.append(new_row)
                identity_to_idx[make_identity(new_row)] = len(comics) - 1
                summary["added"] += 1

        # ----- Possibly-removed rows ------------------------------------------
        for row in comics:
            row_identity = make_identity(row)
            if (
                row.get("pushed_to_locg_at") is not None
                and row.get("source") == "agent_win"
                and row_identity not in xlsx_identities
            ):
                audit_records.append({
                    "type": "possibly_removed",
                    "ts": now,
                    "command": "import",
                    "details": {
                        "identity": list(row_identity),
                        "full_title": row.get("full_title"),
                    },
                })
                summary["possibly_removed"] += 1

        # ----- Rebuild series_name_index --------------------------------------
        payload["series_name_index"] = rebuild_series_name_index(payload)
        payload["last_full_import"] = now
        payload["last_import_source"] = str(path)

        # Capture wish-list rows for writing after apply() completes.
        wish_rows = [
            r for r in payload["comics"] if r.get("in_wish_list") == 1
        ]

        # Flush audit records while still inside apply (append_audit uses a
        # separate file so it does not need the cache lock)
        for record in audit_records:
            try:
                cache.append_audit(record)
            except Exception as exc:  # noqa: BLE001  # best-effort audit log; I/O failure must not abort import
                logger.warning("Failed to write audit record: %s", exc)

    cache.apply(do_merge, command="import")
    _write_wish_list_cache(wish_rows)
    return summary


# ---------------------------------------------------------------------------
# CSV export (Unit 3)
# ---------------------------------------------------------------------------

def _pending_push_rows(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition pending-push rows into (ready, manual_variant, manual_series_canonical).

    Pending: pushed_to_locg_at IS NULL OR local_added_at > pushed_to_locg_at.
    Ready: pending AND not flagged.
    Manual: pending AND flagged (excluded from CSV).
    """
    ready: list[dict[str, Any]] = []
    manual_variant: list[dict[str, Any]] = []
    manual_series: list[dict[str, Any]] = []

    for row in payload.get("comics", []):
        pushed = row.get("pushed_to_locg_at")
        added = row.get("local_added_at") or ""
        is_pending = pushed is None or (added and added > pushed)
        if not is_pending:
            continue

        if row.get("needs_manual_variant"):
            manual_variant.append(row)
        elif row.get("needs_manual_series_canonical"):
            manual_series.append(row)
        else:
            ready.append(row)

    return ready, manual_variant, manual_series


def _format_price(value: Any) -> str:
    """Format a price as 'NN.NN'. Returns '0.00' for missing/invalid/negative."""
    try:
        f = float(value)
        return "0.00" if f < 0 else f"{f:.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _format_date(value: Any) -> str:
    """Return value as an ISO date string (first 10 chars). Falls back to today."""
    from datetime import date
    if value is None:
        return date.today().isoformat()
    s = str(value)
    return s[:10] if len(s) >= 10 else date.today().isoformat()


def _load_wish_list_items() -> list[dict[str, Any]]:
    """Load wish-list cache items as normalized dicts for CSV export."""
    path = wish_list_cache_path()
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return [
        {
            "publisher_name": item.get("publisher_name") or "",
            "series_name": item.get("series_name") or "",
            "full_title": item.get("name") or "",
            "release_date": item.get("release_date") or "",
            "price_paid": None,
            "date_purchased": None,
        }
        for item in data.get("items", [])
    ]


def _normalize_title(title: str) -> str:
    """Loose full-title key for owned-vs-wished matching (dash + leading-article
    insensitive, whitespace-collapsed). Deliberately generous: over-matching only
    drops a wish from the export, while under-matching could let In Collection=0
    delete an owned book — so we err toward exclusion."""
    t = (title or "").strip().lower().replace("–", "-").replace("—", "-")
    t = re.sub(r"^(the|a|an)\s+", "", t)
    return re.sub(r"\s+", " ", t)


def wish_rows_for_export(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Wish-list rows safe to include in the LOCG bulk-import CSV (BUI-122).

    The CSV writes wish rows with ``In Collection=0``, which tells LOCG to *remove*
    the book from the collection if it matches one. Re-dumping the whole wish list
    therefore (a) re-uploads the LOCG-derived wishes LOCG already has, and worse
    (b) deletes any wished book that is actually owned. This caused real
    collection deletions during BUI-122 testing.

    So the export now includes only:
      - **local-only adds** (no ``series_name`` — the diff LOCG doesn't have yet;
        derived wishes are already on LOCG and are dropped), AND
      - that are **not owned** (title not in the collection's ``in_collection``
        set), so an ``In Collection=0`` row can never delete an owned book.

    Owned-but-wished books are simply not pushed; the wish stays local. Matching is
    title-based and generous (see ``_normalize_title``) — owned-safe by design.
    """
    owned = {
        _normalize_title(r.get("full_title"))
        for r in payload.get("comics", [])
        if r.get("in_collection")
    }
    out: list[dict[str, Any]] = []
    for item in _load_wish_list_items():
        if item.get("series_name"):
            continue  # derived wish — LOCG already has it; re-emitting risks deletion
        if _normalize_title(item.get("full_title")) in owned:
            continue  # owned — never emit In Collection=0 for it
        out.append(item)
    return out


# BUI-105 placeholder: when no Metron data backs a win, record-win stamps
# release_date = "{identify_year}-01-01" so a year-gated collection-check still
# matches. That placeholder is correct in the STORE, but LOCG Bulk Import
# matches on the EXACT Release Date — a wrong Jan-1 date reads as "Not Found",
# whereas a BLANK date still matches by publisher+series+title (and the
# year-precise round-trip restores LOCG's canonical date on re-import).
_PLACEHOLDER_DATE_RE = re.compile(r"^\d{4}-01-01$")


def _is_placeholder_release_date(row: dict[str, Any]) -> bool:
    """True only for a BUI-105 placeholder date, detected by INTENT not shape.

    record-win stamps the ``YYYY-01-01`` placeholder ONLY when no Metron data
    backed the win (``metron_data is None`` -> stored ``metron_id is None``). A
    Metron-sourced ``cover_date`` for a genuine January book is also
    ``YYYY-01-01`` but is a REAL date and must be kept (R66, BUI-199 finding 5).
    So require both an agent_win row AND a missing metron_id before treating a
    Jan-1 date as a placeholder.
    """
    if row.get("source") != "agent_win":
        return False
    if row.get("metron_id") is not None:
        return False
    return bool(_PLACEHOLDER_DATE_RE.match(str(row.get("release_date") or "")))


def _row_to_csv_dict(row: dict[str, Any], in_wish_list: bool = False) -> dict[str, str | int]:
    """Map a cache row to the 21-column LOCG CSV recipe (R21–R31)."""
    # BUI-199 Cause 2: omit the Release Date for placeholder-dated agent_win rows
    # so LOCG matches by title+series instead of rejecting a wrong exact date.
    release_date = "" if _is_placeholder_release_date(row) else (row.get("release_date") or "")
    return {
        "Publisher Name": row.get("publisher_name") or "",
        "Series Name": row.get("series_name") or "",
        "Full Title": row.get("full_title") or "",
        "Release Date": release_date,
        "In Collection": 0 if in_wish_list else 1,
        "In Wish List": 1 if in_wish_list else 0,
        "Marked Read": 0,
        "My Rating": "",  # Present-but-blank (R27 — critical; controls Marked Read default)
        "Media Format": "Print",
        "Price Paid": _format_price(row.get("price_paid")),
        "Date Purchased": _format_date(row.get("date_purchased")),
        "Condition": "",
        "Notes": "",
        "Tags": "",
        "Storage Box": "",
        "Owner": "",
        "Purchase Store": "eBay",
        "Signature": 0,
        "Slabbing": 0,
        "Grading": "",
        "Grading Company": "",
    }


def generate_csv(
    ready_rows: list[dict[str, Any]],
    out_path: Path,
    wish_rows: list[dict[str, Any]] | None = None,
) -> None:
    """Write ready-to-upload rows to a LOCG-compatible 21-column CSV.

    Uses csv.QUOTE_MINIMAL. My Rating column always present with blank body (R27).
    Wish-list rows are appended with In Collection=0, In Wish List=1.
    """
    import csv as _csv

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    headers = list(LOCG_XLSX_HEADERS)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=headers, quoting=_csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in ready_rows:
            writer.writerow(_row_to_csv_dict(row))
        for row in (wish_rows or []):
            writer.writerow(_row_to_csv_dict(row, in_wish_list=True))


def generate_notes_md(
    ready_rows: list[dict[str, Any]],
    manual_variant_rows: list[dict[str, Any]],
    manual_series_rows: list[dict[str, Any]],
    out_path: Path,
) -> None:
    """Write the .notes.md companion report (R18).

    Three sections: Ready to upload, Needs manual handling — variants,
    Needs manual handling — series canonical.
    """
    def _manual_table(rows: list[dict[str, Any]]) -> list[str]:
        lines = [
            "| Series | Full Title | eBay Item ID | Price |",
            "|--------|------------|--------------|-------|",
        ]
        for row in rows:
            series = (row.get("series_name") or "").replace("|", "\\|")
            title = (row.get("full_title") or "").replace("|", "\\|")
            item_id = row.get("gixen_item_id") or ""
            price = _format_price(row.get("price_paid"))
            lines.append(f"| {series} | {title} | {item_id} | ${price} |")
        return lines

    sections: list[str] = [
        "# locg collection export — manual handling notes",
        "",
        f"## Ready to upload ({len(ready_rows)} rows)",
        "",
        "These rows are included in the CSV and ready to upload via LOCG Bulk Import."
        if ready_rows else "No rows ready to upload.",
        "",
    ]

    if manual_variant_rows:
        sections += [
            f"## Needs manual handling — variants ({len(manual_variant_rows)} rows)",
            "",
            "These rows have unresolved variant text and were excluded from the CSV.",
            "Add them manually via the LOCG web UI.",
            "",
        ] + _manual_table(manual_variant_rows) + [""]
    else:
        sections += [
            "## Needs manual handling — variants (0 rows)",
            "",
            "No rows with unresolved variant text.",
            "",
        ]

    if manual_series_rows:
        sections += [
            f"## Needs manual handling — series canonical ({len(manual_series_rows)} rows)",
            "",
            "These rows have unresolved canonical series names and were excluded from the CSV.",
            "Add them manually via the LOCG web UI.",
            "",
        ] + _manual_table(manual_series_rows) + [""]
    else:
        sections += [
            "## Needs manual handling — series canonical (0 rows)",
            "",
            "No rows with unresolved series names.",
            "",
        ]

    Path(out_path).write_text("\n".join(sections), encoding="utf-8")
