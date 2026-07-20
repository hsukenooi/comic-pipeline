"""Excel import and collection merge pipeline for the local collection cache.

BUI-257: LOCG is programmatically inaccessible. The BUI-208 unified sync
functions here (``import_xlsx``, ``generate_csv``, ``migrate_wish_list_source``,
etc.) must only be driven by the manual, user-invoked /comic:collection-sync
skill — never called automatically or on a timer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from locg._atomic import atomic_write_json
from locg.config import wish_list_cache_path
from locg.parsing import trailing_issue_token

from locg.collection_cache import (
    LOCG_BOOLEAN_COLUMNS,
    LOCG_COLUMNS,
    USER_MANAGED_COLUMNS,
    CollectionCache,
    _next_seq,
    _normalize_series_key,
    _utcnow_iso,
    make_identity,
    owned_match_keys,
    rebuild_series_name_index,
    verified_copy_bytes,
)
from locg.parsing import normalize_issue_key, split_series_issue_for_ownership

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

# Columns holding a date: openpyxl returns a date-formatted cell as a
# `datetime`/`date` object rather than a string (BUI-469).
_DATE_COLUMNS: frozenset[str] = frozenset({"release_date", "date_purchased"})


def _coerce_date_cell(value: Any) -> Any:
    """Normalize a raw date-column cell to a ``YYYY-MM-DD`` string.

    openpyxl returns date-formatted cells as ``datetime``/``date`` objects, but
    every downstream consumer (``_reconcile_score``, ``_release_year``,
    ``make_identity``'s identity tuple, ...) expects a string or ``None`` —
    ``(row.get("release_date") or "")[:4]`` raises ``TypeError`` on a
    ``datetime``. Text-formatted date cells already arrive as plain strings
    (or ``None`` for a blank cell) and pass through unchanged.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _coerce_count_cell(value: Any, *, field: str | None = None) -> int:
    """Normalize a raw ``LOCG_BOOLEAN_COLUMNS`` cell to ``int`` — never ``bool``.

    ``in_collection`` is a copies-owned COUNT (0, 1, 2+), not a flag, so this
    must land on ``int`` rather than collapse to ``bool`` (see the
    collection-store composition convention). Without this, a text-formatted
    cell arrives as a ``str`` — and ``bool("0")`` is ``True``, which is
    dangerous wherever an ownership read authorizes deleting a row (BUI-469).

    Handles native ``int``/``float``/``bool`` cells (a checkbox-styled column
    can come back as ``bool``) and text-formatted numeric strings — routing
    every non-bool, non-``None`` shape through the same ``float(str(...))``
    parse (rather than trusting ``int()`` directly on a raw ``float``) so a
    stray ``NaN``/``inf`` cell hits the ``except`` below instead of raising
    ``ValueError``/``OverflowError`` out of ``int()`` uncaught. Blank
    (``None``) or unparseable input reads as ``0`` — the same "not present"
    value these columns already use — and, when ``field`` is given (the
    ``parse_xlsx`` ingest path, not the in-memory ``_is_owned`` read), an
    unparseable non-blank cell logs a warning: silently defaulting a garbled
    ownership cell to "not owned" is the R11-dangerous direction (a hidden
    duplicate-buy risk), so the anomaly must stay visible rather than
    disappear into a 0.
    """
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return 0
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        if field is not None:
            logger.warning(
                "parse_xlsx: unparseable %s cell %r — defaulting to 0", field, value
            )
        return 0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_xlsx(path: Path) -> list[dict[str, Any]]:
    """Parse a LOCG Excel export into a list of row dicts.

    Validates file size and header row before reading any data. Each cell is
    coerced to its declared type on the way in (BUI-469): date columns
    (``release_date``, ``date_purchased``) normalize to ``YYYY-MM-DD``
    strings, and ``LOCG_BOOLEAN_COLUMNS`` (``in_collection``, ``in_wish_list``,
    ``marked_read``, ``signature``, ``slabbing``) normalize to ``int`` — never
    ``bool``, since ``in_collection`` is a copies-owned count, not a flag.
    Every other column keeps its raw openpyxl cell value. Returns rows with
    LOCG_COLUMNS keys.
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
                value = raw[idx] if idx < len(raw) else None
                if field in _DATE_COLUMNS:
                    value = _coerce_date_cell(value)
                elif field in LOCG_BOOLEAN_COLUMNS:
                    value = _coerce_count_cell(value, field=field)
                row[field] = value
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


_YEAR4_RE = re.compile(r"\d{4}")


def _release_year(row: dict[str, Any]) -> str:
    """The 4-digit release-date year of a row, or "" when absent/unparseable."""
    year = (row.get("release_date") or "")[:4].strip()
    return year if _YEAR4_RE.fullmatch(year) else ""


def _era_confirmed(cache_row: dict[str, Any], xlsx_row: dict[str, Any]) -> bool:
    """Positive same-era evidence for the *destructive* auto-heal branch (BUI-462).

    ``_reconcile_score``'s year compare fails **OPEN** — it only rejects when
    *both* sides name a year and they differ, so a dateless (or Jan-1
    placeholder-blanked) row matches any era. That is the right call for the
    non-destructive paths, which only ever rewrite a row's identity. It is the
    wrong call for the auto-heal branch, which *retires* a row: the same
    fail-open lets a modern win fuzzy-match a vintage volume of the same
    masthead (``_normalize_series_key`` strips the ``(YYYY - YYYY)`` /
    ``(Vol. N)`` decoration, so two volumes of one masthead normalize to the
    SAME key) and be folded into a book it is not.

    Confirmed by either:

    * **Same 4-digit year on both sides.** ``_reconcile_score`` already rejects
      a year *disagreement* on its issue-numbered branch, so on that branch this
      is exactly the fail-closed complement — requiring *presence*. A dateless
      win is never healed; it is left pending for the operator (visible
      non-clear over silent wrong drop) until its release_date is backfilled
      (BUI-210 / BUI-461).
    * **Identical full_title with no issue token on either side** — the
      TPB/HC/OGN branch, where ``_reconcile_score`` matches on the title string
      itself and never compares years at all. There is no ``#N``-across-volumes
      ambiguity for a year to resolve there, so requiring a year would have
      newly stranded dateless trade wins that BUI-211 healed. This clause keeps
      that branch's behavior exactly as it was.

    What this deliberately does NOT prove: that the win's year is *correct*. A
    Jan-1 placeholder (BUI-105) carries a real identified cover year, and the
    record-win no-year misresolution class can stamp a wrong volume's real year.
    That residual is what the provenance carry-over
    (:func:`_carry_win_provenance`) and the full-row audit trail exist to make
    survivable rather than fatal — a mis-fired heal costs a row merge that the
    append-only log can reverse, never local-only purchase data.
    """
    cache_year = _release_year(cache_row)
    if cache_year and cache_year == _release_year(xlsx_row):
        return True

    cache_title = (cache_row.get("full_title") or "").strip()
    xlsx_title = (xlsx_row.get("full_title") or "").strip()
    return (
        bool(cache_title)
        and cache_title.lower() == xlsx_title.lower()
        and _issue_token(cache_title) is None
        and _issue_token(xlsx_title) is None
    )


def _era_decline_reason(
    cache_row: dict[str, Any], xlsx_row: dict[str, Any]
) -> tuple[str, str]:
    """``(audit reason, operator-facing detail)`` for a declined heal (BUI-462).

    :func:`_era_confirmed` fails for three different reasons and the operator
    acts on the message: telling them to backfill a release_date that is already
    present sends them after nothing and strands the row on every subsequent
    sync, so name the side that actually lacks the evidence.
    """
    win_year = _release_year(cache_row)
    export_year = _release_year(xlsx_row)
    if not win_year:
        return (
            "heal_declined_win_has_no_year",
            "the pending win carries no release-date year, so its era cannot "
            "be confirmed — backfill its release_date",
        )
    if not export_year:
        return (
            "heal_declined_export_has_no_year",
            "the incoming export row carries no release date, so the era "
            "cannot be confirmed — fix the date on LOCG",
        )
    return (
        "heal_declined_year_conflict",
        f"the release years disagree ({win_year} vs {export_year})",
    )


def _is_owned(row: dict[str, Any]) -> bool:
    """Strict read of ``in_collection`` (a copy count, not a flag).

    ``parse_xlsx`` coerces ``in_collection`` to ``int`` on ingest (BUI-469),
    so a fresh xlsx row is already safe to read with plain truthiness. This
    stays strict as a defense-in-depth backstop for any row this coercion
    doesn't cover — one constructed in-process rather than parsed, or one
    already persisted to the on-disk cache from an import that predates
    BUI-469 and still carries a raw ``str`` (where ``bool("0")`` is ``True``).
    A truthiness read is harmless in ``_apply_locg_columns_held``, where the
    only consequence is an over-conservative ownership *hold* (the safe
    direction). On the auto-heal branch the same cell authorizes retiring a
    row, so it is parsed strictly: anything that does not resolve to a count
    >= 1 is not ownership.
    """
    return _coerce_count_cell(row.get("in_collection")) >= 1


# Local-only provenance an agent_win row carries that a LOCG export row never
# supplies (LOCG has no idea what you paid or which eBay item it came from).
_WIN_PROVENANCE_FIELDS: tuple[str, ...] = (
    "price_paid",
    "date_purchased",
    "gixen_item_id",
    "metron_id",
)


def _carry_win_provenance(
    dropped: dict[str, Any],
    kept: dict[str, Any],
    now: str,
    audit_records: list[dict[str, Any]],
) -> None:
    """Move an auto-healed win's local-only provenance onto the row that
    survives it (BUI-462).

    Without this the heal is a genuine data *loss*, not a dedup: the wish twin
    that survives a wished-then-won book has by definition never carried a
    purchase price, and ``price_paid`` / ``date_purchased`` are LOCG columns
    that Phase 2 blanks from the export. That put the auto-heal on the wrong
    side of the module's own rule (``commands.py``: "a win stuck pending is
    recoverable; a win dropped on import is not"), and it is what makes it
    acceptable for an LOCG-sourced ``In Collection`` to authorize the drop at
    all — nothing irreversible rides on it.

    **Must run after** :func:`_standard_merge_phase`: that phase overwrites every
    ``LOCG_COLUMNS`` value on the kept row from the export, which would clobber
    a carry-over done during reconciliation.

    Only fills fields the kept row leaves empty — it never overwrites a value
    LOCG supplied or one the kept row already held.
    """
    carried: dict[str, Any] = {}
    for field in _WIN_PROVENANCE_FIELDS:
        value = dropped.get(field)
        if value is None or value == "":
            continue
        existing = kept.get(field)
        if existing is not None and existing != "":
            continue
        kept[field] = value
        carried[field] = value

    if carried:
        audit_records.append({
            "type": "auto_healed_win_provenance_carried",
            "ts": now,
            "command": "import",
            "details": {
                "full_title": kept.get("full_title"),
                "kept_identity": list(make_identity(kept)),
                "carried": carried,
            },
        })


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
# Wish-list source classification + migration (BUI-208)
# ---------------------------------------------------------------------------

def _wish_source(item: dict[str, Any]) -> str:
    """Classify a wish-list entry as ``"local"`` or ``"export"`` (BUI-208).

    ``wish-list.json`` is the single source of truth for wish state, keyed on an
    explicit ``source`` field. Prefer the explicit value when present; otherwise
    fall back to the legacy "absence of ``series_name``" sentinel so un-migrated
    entries keep working: an export-derived entry always carried a
    ``series_name``, a local ``wish-list add`` never did.
    """
    return item.get("source") or ("export" if item.get("series_name") else "local")


def migrate_wish_list_source() -> dict[str, Any]:
    """Backfill an explicit ``source`` field onto every wish-list entry (BUI-208).

    Backup-gated, idempotent field-stamp: writes a verified ``.bak`` copy of
    ``wish-list.json`` (and aborts before any mutation if the backup doesn't
    read back byte-for-byte identical), then stamps ``item["source"]`` on every
    item that lacks an explicit one (via :func:`_wish_source`), bumps
    ``updated_at`` and rewrites atomically (via :func:`locg._atomic.atomic_write_json`,
    chmod 600).

    Returns ``{"migrated": <stamped count>, "backup": <path|None>, "total": <n>}``;
    if the cache is absent, returns ``{"migrated": 0, "backup": None}``.
    """
    path = wish_list_cache_path()
    if not path.exists():
        return {"migrated": 0, "backup": None}

    ts = datetime.now(timezone.utc).isoformat().replace(":", "")
    backup = path.with_name(f"{path.name}.bak.{ts}")
    try:
        original = verified_copy_bytes(path, backup, mode=stat.S_IRUSR | stat.S_IWUSR)
    except RuntimeError as exc:
        raise RuntimeError(f"wish-list migration aborted: {exc}") from exc

    data = json.loads(original.decode())
    items: list[dict[str, Any]] = data.get("items") or []
    migrated = 0
    for item in items:
        if not item.get("source"):
            item["source"] = _wish_source(item)
            migrated += 1

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }

    atomic_write_json(
        path,
        payload,
        mode=stat.S_IRUSR | stat.S_IWUSR,  # 600
        tmp_prefix=".wish-list-",
    )

    return {"migrated": migrated, "backup": str(backup), "total": len(items)}


# ---------------------------------------------------------------------------
# Main import orchestration
# ---------------------------------------------------------------------------

def _reconcile_phase(
    comics: list[dict[str, Any]],
    xlsx_rows: list[dict[str, Any]],
    identity_to_idx: dict[tuple, int],
    partial_to_idx: dict[tuple, int],
    healed_drop: dict[int, int],
    audit_records: list[dict[str, Any]],
    summary: dict[str, Any],
    now: str,
) -> None:
    """Phase 1 of import_xlsx's do_merge: reconcile flagged/pending agent_win
    rows against the incoming export via the relaxed heuristic
    (_reconcile_score), rewriting identity in place and clearing manual flags.
    See import_xlsx's docstring for the two-phase pipeline this implements.

    Mutates `comics` row dicts, `identity_to_idx` / `partial_to_idx`,
    `healed_drop`, `audit_records`, and `summary` in place (same containers
    do_merge already builds/holds) — no return value, matching do_merge's
    existing mutate-in-place style. Must run before _standard_merge_phase,
    which relies on the identity rewrites and index updates made here.
    """
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
            # BUI-211: auto-heal the safe case (folds in cleanup_duplicates.py
            # class 1 — same-book/different-identity dup wins). If the collision
            # target is an *established owned* row (locg_export or already
            # pushed, AND owned) and THIS row is a pending agent_win, the two
            # are the same owned book: the win is a redundant leftover that
            # record-win's dedup missed (the owned copy was usually imported
            # after the win was recorded). Drop the pending win, keep the
            # established owned row — no "left pending" warning needed.
            #
            # BUI-462: ownership is read POST-import, not pre-import. The
            # collision target holds `target_identity` == make_identity(xlsx_row)
            # *exactly*, so Phase 2 will apply this same export row's LOCG
            # columns to it; and `_apply_locg_columns_held` only ever holds an
            # ownership DOWNGRADE, never a downgrade-to-owned. So the target is
            # owned after this import iff it is owned now OR the export row says
            # owned — no heuristic involved. That is the wish-twin case: a
            # wished-then-won book's twin sits at in_collection=0 until Phase 2
            # flips it, which made the pre-import read bail to "left pending" and
            # strand every one of the 27 collisions on the 2026-07-19 sync.
            target_row = comics[collide]
            target_established = (
                target_row.get("source") == "locg_export"
                or bool(target_row.get("pushed_to_locg_at"))
            )
            target_owned_after_import = _is_owned(target_row) or _is_owned(xlsx_row)
            # The dropped row is ALWAYS the pending win, never the collision
            # target — so no wish row in the store can be removed by this
            # branch. `not in_wish_list` makes that a syntactic property of the
            # drop itself rather than an inference about what record-win writes:
            # a row carrying wish state is structurally ineligible to be
            # dropped, which is what keeps the intentional cross-volume decoy
            # holds (owned under one volume, deliberately wished under another)
            # safe by construction. wish-list.json is not touched at all (BUI-208).
            cache_row_pending_win = (
                cache_row.get("source") == "agent_win"
                and cache_row.get("pushed_to_locg_at") is None
                and not cache_row.get("in_wish_list")
            )
            if target_established and target_owned_after_import and cache_row_pending_win:
                # BUI-462: the era must be PROVED before deleting anything —
                # see _era_confirmed. Without it a dateless win can fuzzy-match
                # a different volume of the same masthead and be dropped.
                if not _era_confirmed(cache_row, xlsx_row):
                    reason, detail = _era_decline_reason(cache_row, xlsx_row)
                    audit_records.append({
                        "type": "ambiguous_reconciliation",
                        "ts": now,
                        "command": "import",
                        "details": {
                            "full_title": cache_row.get("full_title"),
                            "reason": reason,
                            "gixen_item_id": cache_row.get("gixen_item_id"),
                            "win_release_date": cache_row.get("release_date"),
                            "export_release_date": xlsx_row.get("release_date"),
                        },
                    })
                    summary["warnings"].append(
                        f"Reconciliation collision for '{cache_row.get('full_title')}' "
                        f"— a row with that identity already exists; left pending "
                        f"({detail}; not safe to retire it as a duplicate)"
                    )
                    continue

                healed_drop[ci] = collide
                summary["auto_healed_duplicates"] += 1
                audit_records.append({
                    "type": "auto_healed_duplicate_win",
                    "ts": now,
                    "command": "import",
                    "details": {
                        "full_title": cache_row.get("full_title"),
                        "kept_identity": list(target_identity),
                        "dropped_identity": list(make_identity(cache_row)),
                        "gixen_item_id": cache_row.get("gixen_item_id"),
                        # The WHOLE dropped row, so this append-only log alone
                        # is genuinely enough to reconstruct it — the identity
                        # tuple omits exactly the local-only fields (price_paid,
                        # date_purchased, condition, ...) that make a wrong drop
                        # unrecoverable.
                        "dropped_row": dict(cache_row),
                    },
                })
                # BUI-462: retract the healed row's index entries. It is about
                # to disappear, but its pre-heal identity/partial entries would
                # otherwise stay live for all of Phase 2 — long enough for the
                # R67 rename path to claim `ci` as the rename target for an
                # unrelated export row, apply that row's columns to it, and then
                # have the whole thing removed by the post-phase filter. The
                # export row would then exist nowhere: an owned book silently
                # lost from the store (the R11 direction). Retracting them makes
                # such a row fall through to a genuine insert instead.
                healed_identity = make_identity(cache_row)
                if identity_to_idx.get(healed_identity) == ci:
                    del identity_to_idx[healed_identity]
                healed_partial = _partial_identity(cache_row)
                if partial_to_idx.get(healed_partial) == ci:
                    del partial_to_idx[healed_partial]
                # Skip the rest of this iteration (like the leave-pending path):
                # do NOT rewrite identity for a dropped row.
                continue

            # Not an established-owned collision (e.g. two pending rows): keep
            # the existing leave-pending behavior exactly.
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


def _standard_merge_phase(
    comics: list[dict[str, Any]],
    xlsx_rows: list[dict[str, Any]],
    identity_to_idx: dict[tuple, int],
    partial_to_idx: dict[tuple, int],
    audit_records: list[dict[str, Any]],
    summary: dict[str, Any],
    now: str,
) -> set[tuple]:
    """Phase 2 of import_xlsx's do_merge: insert-or-update each export row by
    identity tuple, detecting renames (R67) via partial-identity match against
    pre-import rows only. See import_xlsx's docstring for the two-phase
    pipeline this implements.

    Mutates `comics`, `identity_to_idx` / `partial_to_idx`, `audit_records`,
    and `summary` in place. Must run after _reconcile_phase (Phase 1), whose
    identity rewrites this phase's identity_to_idx lookups depend on. Returns
    the set of xlsx row identities seen, which the possibly-removed check
    (run by do_merge after both phases) needs.
    """
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

    return xlsx_identities


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
    auto_healed_duplicates, null_release_date_owned, warnings}.

    `null_release_date_owned` (BUI-412) is a non-blocking data-quality count of
    owned rows (`in_collection` truthy) whose `release_date` is null/empty,
    post-import; a corresponding message is appended to `warnings` when > 0.
    This never rejects the import or alters/drops any row — it only surfaces
    the gap, since a null-dated owned row silently defeats the year-scoped
    wish-list conflicts audit.
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
        # BUI-211: pending agent_win rows auto-healed away because the book is
        # already owned under an established locg_export identity (folds in
        # cleanup_duplicates.py class 1 — same-book/different-identity dup wins).
        # BUI-462 extends this to the wish-twin case (the identity is owned by
        # the *incoming* export row rather than already owned in the store) and
        # gates the drop on confirmed-era evidence (_era_confirmed).
        "auto_healed_duplicates": 0,
        # BUI-412: owned rows with no release_date, post-import. Non-blocking —
        # a data-quality count only, never used to reject/alter/drop a row.
        "null_release_date_owned": 0,
        "warnings": [],
    }

    # Collect audit records to append after each merge step.
    # append_audit is called inside the mutate_fn (safe: uses a different file).
    audit_records: list[dict[str, Any]] = []

    def do_merge(payload: dict[str, Any]) -> None:
        comics = payload["comics"]

        # BUI-211: indices of pending agent_win rows auto-healed away (redundant
        # duplicates of an established owned row). We cannot delete from `comics`
        # mid-loop — indices feed identity_to_idx, Phase 2, and possibly-removed
        # — so we collect them here and filter once, after all phases complete.
        # BUI-462: {dropped index -> index of the row kept in its place}, so the
        # dropped win's local-only provenance can be carried onto the survivor.
        healed_drop: dict[int, int] = {}

        # Full identity index: (publisher, series, full_title, release_date) → idx
        identity_to_idx: dict[tuple, int] = {}
        # Partial identity index for rename detection: (publisher, series, release_date) → idx
        partial_to_idx: dict[tuple, int] = {}
        for i, row in enumerate(comics):
            identity_to_idx[make_identity(row)] = i
            partial_to_idx[_partial_identity(row)] = i

        _reconcile_phase(
            comics,
            xlsx_rows,
            identity_to_idx,
            partial_to_idx,
            healed_drop,
            audit_records,
            summary,
            now,
        )

        xlsx_identities = _standard_merge_phase(
            comics,
            xlsx_rows,
            identity_to_idx,
            partial_to_idx,
            audit_records,
            summary,
            now,
        )

        # ----- Drop auto-healed duplicate wins (BUI-211) ----------------------
        # Filter the redundant pending agent_win rows now that all index-bearing
        # phases (reconcile, standard merge, rename) are done. Doing it here —
        # before possibly-removed, the series-name index rebuild, and the write —
        # guarantees the persisted/returned collection excludes the dropped rows
        # and that row_count reflects the drops. identity_to_idx / partial_to_idx
        # are not used past this point, so they need no rebuild; the only
        # remaining consumers iterate `comics` directly. A healed row is a pending
        # agent_win (pushed_to_locg_at is None), so it can never satisfy the
        # possibly-removed predicate below — it is a dedup heal, not a removal.
        if healed_drop:
            # BUI-462: carry each dropped win's local-only provenance onto the
            # row kept in its place FIRST — Phase 2 has just overwritten the
            # kept row's LOCG columns from the export, so this has to run after
            # it or price_paid/date_purchased would be clobbered right back to
            # the export's blanks. Phase 2 only ever appends, so both indices
            # are still valid here.
            for dropped_idx, kept_idx in healed_drop.items():
                _carry_win_provenance(
                    comics[dropped_idx], comics[kept_idx], now, audit_records
                )
            comics = [r for i, r in enumerate(comics) if i not in healed_drop]
            payload["comics"] = comics
            # Deleting rows from the collection must never be a silent success.
            # `possibly_removed` deliberately excludes healed drops, and a
            # shrinking row_count reads as "no duplicates inserted" to the sync
            # runbook's safety check — so say it out loud where the operator
            # already looks.
            summary["warnings"].append(
                f"{len(healed_drop)} pending win row(s) auto-healed away as "
                "duplicates of an owned LOCG row (BUI-211/BUI-462). Purchase "
                "provenance was carried onto the kept rows; the full dropped "
                "rows are recorded in import-history.jsonl "
                "(type=auto_healed_duplicate_win) if any needs reversing."
            )

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

        # ----- Data-quality report: owned rows missing release_date (BUI-412) --
        # A null/empty release_date on an OWNED row silently defeats the
        # year-scoped wish-list conflicts audit: the year-gate can't confirm two
        # years differ against a null-dated owned row, so it conservatively keeps
        # flagging a real match as a conflict (the BUI-122-safe over-flag
        # direction, but still noisy). Non-blocking by design (per BUI-412's
        # decision) — this only counts and surfaces the gap; it never rejects the
        # import, drops a row, or alters release_date (or any other field).
        # `in_collection` truthy is the established "owned" predicate elsewhere
        # in this module (see _owned_series_issue_index / wish_rows_for_export)
        # — it excludes rows the export carries only because they're wish-listed
        # (in_collection=0), which must never inflate this count.
        null_release_date_owned = sum(
            1
            for row in comics
            if row.get("in_collection") and not (row.get("release_date") or "").strip()
        )
        summary["null_release_date_owned"] = null_release_date_owned
        if null_release_date_owned:
            summary["warnings"].append(
                f"{null_release_date_owned} owned collection row(s) have no "
                "release_date — this silently defeats the year-scoped wish-list "
                "conflicts audit (BUI-412). Consider backfilling release_date "
                "on these rows."
            )

        # ----- Rebuild series_name_index --------------------------------------
        payload["series_name_index"] = rebuild_series_name_index(payload)
        payload["last_full_import"] = now
        payload["last_import_source"] = str(path)

        # BUI-208: the import no longer touches wish-list.json. wish-list.json is
        # the single source of truth for wish state (keyed on `source`), so a
        # server-side wish removal stays durable across an import. The raw
        # in_wish_list LOCG column is still stored verbatim on collection rows.

        # Flush audit records while still inside apply (append_audit uses a
        # separate file so it does not need the cache lock)
        for record in audit_records:
            try:
                cache.append_audit(record)
            except Exception as exc:  # noqa: BLE001  # best-effort audit log; I/O failure must not abort import
                logger.warning("Failed to write audit record: %s", exc)

    cache.apply(do_merge, command="import")
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
            "source": _wish_source(item),
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


def _owned_series_issue_index(payload: dict[str, Any]) -> set[tuple[str, str]]:
    """Set of ``(normalized_series_key, normalized_issue_key)`` for owned rows.

    BUI-200: the owned-safe check must match on normalized (series, issue), not
    the literal title, because an owned copy can be filed under a different
    series-name variant than the wish (the X-Men split, leading-article / Vol /
    year decoration). Each owned row is indexed under EVERY masthead variant it
    could be matched against (:func:`owned_match_keys` adds the cross-masthead
    key for the classic X-Men split), so a wish written under either masthead
    finds it.
    """
    index: set[tuple[str, str]] = set()
    for r in payload.get("comics", []):
        if not r.get("in_collection"):
            continue
        # BUI-197: use the permissive ownership split so an owned row with a
        # non-digit-led token (e.g. "Thor Annual #A1") is still indexed and can
        # exclude a wish under an alias name — the digit-led split returned None
        # here, leaving only the non-alias-aware title-string fallback.
        series_portion, issue_token = split_series_issue_for_ownership(
            r.get("full_title") or ""
        )
        if issue_token is None:
            continue  # TPB/OGN/special — handled by the title-string path below
        issue_key = normalize_issue_key(issue_token)
        for key in owned_match_keys(series_portion, issue_token):
            index.add((key, issue_key))
    return index


def wish_rows_for_export(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Wish-list rows safe to include in the LOCG bulk-import CSV (BUI-122/BUI-200).

    The CSV writes wish rows with ``In Collection=0``, which tells LOCG to *remove*
    the book from the collection if it matches one. Re-dumping the whole wish list
    therefore (a) re-uploads the LOCG-derived wishes LOCG already has, and worse
    (b) deletes any wished book that is actually owned. This caused real
    collection deletions during BUI-122 testing, and the BUI-200 incident deleted
    26 owned X-Men when the owned copy was filed under a different masthead.

    So the export now includes only:
      - **local-only adds** (no ``series_name`` — the diff LOCG doesn't have yet;
        derived wishes are already on LOCG and are dropped), AND
      - that are **not owned under ANY name variant** — checked two ways, both
        owned-safe (over-exclusion only drops a wish, under-exclusion deletes a
        book): a normalized ``(series, issue)`` match (BUI-200 — catches the
        X-Men split + article/Vol/year variants), and the older generous
        title-string match (BUI-122 — dash/article-insensitive, and the only
        path for issueless TPB/OGN rows).

    Owned-but-wished books are simply not pushed; the wish stays local.
    """
    owned_titles = {
        _normalize_title(r.get("full_title"))
        for r in payload.get("comics", [])
        if r.get("in_collection")
    }
    owned_series_issue = _owned_series_issue_index(payload)
    out: list[dict[str, Any]] = []
    for item in _load_wish_list_items():
        if item.get("source") == "export":
            continue  # source==export — LOCG already has it; re-emitting risks deletion
        full_title = item.get("full_title") or ""
        if _normalize_title(full_title) in owned_titles:
            continue  # owned (title match) — never emit In Collection=0 for it
        if _wish_owned_by_series_issue(full_title, owned_series_issue):
            continue  # owned under a different name variant (BUI-200)
        out.append(item)
    return out


def _wish_owned_by_series_issue(
    full_title: str, owned_series_issue: set[tuple[str, str]]
) -> bool:
    """True if ``full_title`` is owned under any normalized (series, issue) variant.

    Parses the wish title into (series, issue) via the shared permissive
    ownership split (BUI-197 — so non-digit-led tokens like ``#A1`` are compared,
    not skipped), then checks every normalized key the issue could be owned under
    (:func:`owned_match_keys`) against the owned index. Owned-safe: a title with
    no ``#`` token at all returns False here and is left to the title-string path,
    which never under-matches an owned book.
    """
    series_portion, issue_token = split_series_issue_for_ownership(full_title)
    if issue_token is None:
        return False
    issue_key = normalize_issue_key(issue_token)
    return any(
        (key, issue_key) in owned_series_issue
        for key in owned_match_keys(series_portion, issue_token)
    )


# BUI-105 placeholder: when no Metron data backs a win, record-win stamps
# release_date = "{identify_year}-01-01" so a year-gated collection-check still
# matches. That placeholder is correct in the STORE, but LOCG Bulk Import
# matches on the EXACT Release Date — a wrong Jan-1 date reads as "Not Found",
# whereas a BLANK date still matches by publisher+series+title (and the
# year-precise round-trip restores LOCG's canonical date on re-import).
#
# This blanking is the whole reason the placeholder costs the export nothing:
# a placeholder row and a dateless row emit the SAME empty Release Date. See
# _build_win_row's BUI-105 block for why the stamp must stay in the store even
# so (BUI-210's reopen proposed deleting it; it deletes wins).
_PLACEHOLDER_DATE_RE = re.compile(r"^\d{4}-01-01$")


def _is_placeholder_release_date(row: dict[str, Any]) -> bool:
    """True only for a BUI-105 placeholder date, detected by INTENT not shape.

    record-win stamps the ``YYYY-01-01`` placeholder ONLY when no Metron data
    backed the win (``metron_data is None`` -> stored ``metron_id is None``). A
    Metron-sourced ``cover_date`` for a genuine January book is also
    ``YYYY-01-01`` but is a REAL date and must be kept (R66, BUI-199 finding 5).
    So require both an agent_win row AND a missing metron_id before treating a
    Jan-1 date as a placeholder — the shape alone would silently delete real
    January dates, and ``metron_id`` is the only thing separating the two.
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
    *,
    allow_uncollect: bool = False,
) -> None:
    """Write ready-to-upload rows to a LOCG-compatible 21-column CSV.

    Uses csv.QUOTE_MINIMAL. My Rating column always present with blank body (R27).
    Wish-list rows are appended with In Collection=0, In Wish List=1.

    BUI-208 machine gate: a wish row carries ``In Collection=0``, which tells
    LOCG to *remove* the title from the collection on upload — the data-loss
    trigger. So if ``wish_rows`` is non-empty this refuses to write unless the
    caller passes ``allow_uncollect=True`` (an explicit, owned-safe wish push).
    The default wins-only export can therefore never emit an ``In Collection=0``
    row.
    """
    import csv as _csv

    if wish_rows and not allow_uncollect:
        raise ValueError(
            "Refusing to emit In Collection=0 rows in a wins-only export "
            "(BUI-208 machine gate): a wish row tells LOCG to DELETE the title "
            "from the collection. Pass allow_uncollect=True for an explicit, "
            "owned-safe wish push."
        )

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
