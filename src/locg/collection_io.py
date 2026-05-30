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

_ISSUE_TOKEN_RE = re.compile(r"#\s*(\d+[A-Za-z]?)\s*$")
_VOL_ANNOTATION_RE = re.compile(r"\(Vol\.\s*\d+\)", re.IGNORECASE)


def _issue_token(full_title: str) -> str | None:
    """Extract the issue number token, e.g. 'ASM #300' → '300'."""
    m = _ISSUE_TOKEN_RE.search(full_title)
    return m.group(1).lower() if m else None


def _publisher_matches(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


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
    possibly_removed, behavioral_drift_count, warnings}.
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
        flagged_indices = [
            i for i, r in enumerate(comics)
            if r.get("needs_manual_variant") or r.get("needs_manual_series_canonical")
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

                for col in LOCG_COLUMNS:
                    existing[col] = xr[col]
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
                            for col in LOCG_COLUMNS:
                                existing[col] = xr[col]
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
            except Exception as exc:
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


def _row_to_csv_dict(row: dict[str, Any], in_wish_list: bool = False) -> dict[str, str | int]:
    """Map a cache row to the 21-column LOCG CSV recipe (R21–R31)."""
    return {
        "Publisher Name": row.get("publisher_name") or "",
        "Series Name": row.get("series_name") or "",
        "Full Title": row.get("full_title") or "",
        "Release Date": row.get("release_date") or "",
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
