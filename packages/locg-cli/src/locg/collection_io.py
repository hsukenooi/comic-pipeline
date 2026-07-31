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
    _coerce_year,
    _next_seq,
    _normalize_series_key,
    _utcnow_iso,
    build_volume_candidates,
    identity_series_key,
    make_identity,
    owned_match_keys,
    rebuild_series_name_index,
    resolve_series_for_win,
    verified_copy_bytes,
)
from locg.parsing import normalize_issue_key, split_series_issue_for_ownership

# BUI-470: the reconciler's destructive auto-heal must judge "same book" by
# the SAME test record-win's own dedup uses (BUI-267), not a narrower local
# one — a variant/newsstand distinction record-win treats as a DIFFERENT book
# must not be invisible here. Safe to import at module load: commands.py
# never imports collection_io at its own top level (only inside function
# bodies, deferred), so this is a one-directional dependency, not a cycle.
from locg.commands import (
    _dedup_era_compatible,
    _dedup_variant_compatible,
    _owned_row_variant_suffix,
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


# Generic corporate words a publisher's trading name carries in one provider's
# vocabulary and drops in another's (BUI-548). Metron says "Marvel"; LOCG says
# "Marvel Comics" — same company, and the difference is pure boilerplate.
_PUBLISHER_GENERIC_WORDS: frozenset[str] = frozenset({
    "book", "books", "co", "comic", "comics", "company", "entertainment",
    "group", "inc", "llc", "ltd", "media", "press", "productions",
    "publication", "publications", "publishing", "studio", "studios",
})


def _normalize_publisher(name: str) -> str:
    """Fold a publisher name to its distinguishing tokens (BUI-548).

    Lowercases, drops punctuation, and removes the generic corporate words in
    :data:`_PUBLISHER_GENERIC_WORDS`, so the SAME company named by two
    providers lands on one key: ``"Marvel"`` / ``"Marvel Comics"`` -> ``marvel``,
    ``"Boom! Studios"`` / ``"BOOM! Studios"`` -> ``boom``, ``"DC Comics"`` ->
    ``dc``. Falls back to the punctuation-folded name when stripping the
    generic words would empty it (a publisher genuinely called "Comics").

    Deliberately NOT an imprint table: ``Skybound`` does not fold to ``Image
    Comics`` here. An imprint relation is a fact about the world that a word
    list cannot derive, so it is left to the corroboration path in
    :func:`_reconcile_score` instead of guessed at.
    """
    folded = re.sub(r"[^0-9a-z]+", " ", (name or "").lower()).strip()
    if not folded:
        return ""
    kept = [w for w in folded.split() if w not in _PUBLISHER_GENERIC_WORDS]
    return " ".join(kept) if kept else folded


def _publisher_matches(a: str, b: str) -> bool:
    # A missing publisher on either side is a wildcard, not a mismatch. Series +
    # issue + release date still gate the match, so the publisher wildcard can't
    # merge across genuinely different books (BUI-122). Only reject when BOTH
    # sides name a publisher and they differ.
    #
    # BUI-548: compare NORMALIZED names. Before BUI-458 an agent_win row carried
    # publisher_name=None and took the wildcard; since BUI-458 record-win stamps
    # Metron's publisher label, which uses a different vocabulary from LOCG's
    # ("Marvel" vs "Marvel Comics"). That turned the wildcard into a hard
    # mismatch and silently blocked reconciliation for 34 of the 41 pending wins
    # in the 2026-07-27 sync — every one of them re-imported as a duplicate
    # owned row. A provider naming the same company differently is not evidence
    # of a different book.
    na = _normalize_publisher(a)
    nb = _normalize_publisher(b)
    if not na or not nb:
        return True
    return na == nb


def build_series_publishers(payload: dict[str, Any]) -> dict[str, set[str]]:
    """Map each canonical ``series_name`` -> the normalized publishers on its rows.

    Drawn from ``source='locg_export'`` rows only (R61) — the SAME population
    :func:`locg.collection_cache.build_volume_candidates` draws its volume names
    from, so every candidate that resolver can return has an entry here (or
    none at all, when LOCG left the publisher blank).

    Values are :func:`_normalize_publisher` output rather than raw labels, so a
    caller comparing against another provider's vocabulary ("Marvel" vs "Marvel
    Comics") is not defeated by the naming drift BUI-548 already measured. A
    series legitimately holds MORE than one publisher over its life (an imprint
    move), hence a set and not a single value.
    """
    out: dict[str, set[str]] = {}
    for row in payload.get("comics", []):
        if row.get("source") != "locg_export":
            continue
        series = row.get("series_name") or ""
        publisher = _normalize_publisher(row.get("publisher_name") or "")
        if series and publisher:
            out.setdefault(series, set()).add(publisher)
    return out


def series_publisher_conflicts(
    series_name: str, publisher: str, series_publishers: dict[str, set[str]]
) -> bool:
    """True when every publisher known for ``series_name`` disagrees with ``publisher``.

    Answers "is this volume someone else's edition of the book?" and nothing
    more. Returns False — no conflict — whenever the question cannot be settled:
    an unknown ``publisher``, or a volume LOCG left publisher-less. Positive
    evidence of disagreement is the only thing that counts, which is what lets
    callers use it as a trigger rather than a filter.
    """
    known = series_publishers.get(series_name) or set()
    if not known or not publisher:
        return False
    return not any(_publisher_matches(publisher, name) for name in known)


def publisher_scoped_volume_candidates(
    volume_candidates: dict[str, list[str]],
    series_publishers: dict[str, set[str]],
    publisher: str,
) -> dict[str, list[str]]:
    """``volume_candidates`` with volumes of a DIFFERENT publisher dropped (BUI-564).

    :func:`locg.collection_cache.resolve_series_for_win` picks a win's volume by
    era alone. That is publisher-blind, and the pool it chooses from is whatever
    LOCG's export holds — which includes the foreign licensed editions our own
    record-win push put there. Handing it a pool already scoped to the win's
    publisher lets every one of its era/alias/split rules run unchanged over
    only the volumes that could actually be the book.

    Fails OPEN, per key: when NO volume under a key agrees with ``publisher``
    the key keeps its full list. A disagreement can mean "wrong volume", but it
    can equally mean a publisher label :func:`_normalize_publisher` cannot fold
    (an imprint, a rebrand), and this must never be the reason a win stops
    resolving. A volume with no known publisher at all is likewise kept.
    """
    scoped: dict[str, list[str]] = {}
    for key, names in volume_candidates.items():
        kept = [
            name
            for name in names
            if not series_publisher_conflicts(name, publisher, series_publishers)
        ]
        scoped[key] = kept or names
    return scoped


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


def _same_book_confirmed(
    cache_row: dict[str, Any], xlsx_row: dict[str, Any]
) -> tuple[bool, str, str]:
    """``(confirmed, audit reason, operator-facing detail)`` for the
    destructive auto-heal branch (BUI-470).

    Unifies the reconciler's same-book judgment with record-win's own dedup
    test (:func:`locg.commands._dedup_era_compatible` /
    :func:`locg.commands._dedup_variant_compatible`, BUI-267) so a
    newsstand/variant distinction record-win treats as a DIFFERENT book is no
    longer invisible here — the reconciler could otherwise retire a row
    record-win deliberately kept distinct.

    This only ADDS conditions to :func:`_era_confirmed`'s existing fail-closed
    gate; it never replaces or loosens it (BUI-462's own hard requirement).
    :func:`_era_confirmed` stays the primary, first-checked gate because it is
    already STRICTER than record-win's own era test: it requires an EXACT
    4-digit-year match (or the TPB/HC/OGN identical-title branch), where
    :func:`_dedup_era_compatible` allows record-win's ±1 window and treats an
    unparseable win year as permissive (the right bias for record-win, which
    only ever *creates* a row — the wrong one for a branch that *retires*
    one). Layering :func:`_dedup_era_compatible` on top after
    :func:`_era_confirmed` already passed adds one thing it does not check —
    a series' own declared ``(YYYY - YYYY)`` year range — without ever being
    able to accept something :func:`_era_confirmed` rejected.
    :func:`_dedup_variant_compatible` closes the actual gap this ticket exists
    for: a base win must not heal against an owned Newsstand/Direct/Facsimile
    copy (or a different print run) of the same issue, matching exactly what
    stops record-win from deduping them at write time.
    """
    if not _era_confirmed(cache_row, xlsx_row):
        reason, detail = _era_decline_reason(cache_row, xlsx_row)
        return (False, reason, detail)

    win_suffix = _owned_row_variant_suffix(cache_row.get("full_title") or "")
    candidate_suffix = _owned_row_variant_suffix(xlsx_row.get("full_title") or "")
    if not _dedup_variant_compatible((win_suffix or "").lower(), candidate_suffix):
        return (
            False,
            "heal_declined_variant_mismatch",
            f"the win's print/variant edition ({win_suffix or 'base, no suffix'}) "
            f"and the export row's ({candidate_suffix or 'base, no suffix'}) are "
            "distinct editions per record-win's own dedup test — not the same book",
        )

    win_year = _coerce_year(cache_row.get("release_date"))
    if not _dedup_era_compatible(win_year, xlsx_row):
        return (
            False,
            "heal_declined_era_range_mismatch",
            f"the win's year ({win_year}) falls outside the export row's own "
            "declared series year range per record-win's own dedup test — not "
            "the same book",
        )

    return (True, "", "")


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


def _is_established_row(row: dict[str, Any]) -> bool:
    """True when a row already exists on LOCG's side, not only in our store.

    Either it came FROM an export, or it is a win we have already pushed. The
    complement is a *pending* win — a row LOCG has never seen — which is the
    reconciler's exclusive business (Phase 1), judged there with era and
    print/variant evidence no other pass carries.

    Shared by Phase 1's auto-heal collision guard and Phase 2's date-drift
    detector (BUI-554) so the two cannot drift apart on what "established"
    means; both would otherwise re-derive the same two-clause test inline.
    """
    return row.get("source") == "locg_export" or bool(row.get("pushed_to_locg_at"))


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


def _vol_annotation_relation(a: str, b: str) -> tuple[bool, bool]:
    """``(conflict, one_sided)`` for two series names' ``(Vol. N)`` annotations.

    BUI-548 splits what was a single "differs" verdict, because the two halves
    carry different weight and only one of them is negotiable:

    * ``conflict`` — BOTH sides declare a volume and the two disagree
      (``Silver Surfer (Vol. 3)`` vs ``The Silver Surfer (Vol. 4)``). Positive
      evidence of different books; a hard mismatch under every widening.
    * ``one_sided`` — exactly one side declares a volume. Merely missing
      information: record-win writes the bare Metron masthead whenever
      ``resolve_series_for_win`` misses, so absence is silence, not
      disagreement. :func:`_reconcile_score` widens past this one, but only
      with corroboration.
    """
    va = _VOL_ANNOTATION_RE.search(a or "")
    vb = _VOL_ANNOTATION_RE.search(b or "")
    if va is None or vb is None:
        return (False, va is not None or vb is not None)
    return (va.group(0).lower() != vb.group(0).lower(), False)


# LOCG stores a book's ON-SALE date; record-win stamps Metron's COVER date,
# which the industry sets 2-3 months later. The skew is systematic and strictly
# one-directional (LOCG earlier), so the tolerance is asymmetric. The widest gap
# in the 2026-07-27 backlog was 91 days (Tales of Suspense #98: cover
# 1968-02-01 vs on-sale 1967-11-02); 120 days keeps headroom while staying far
# short of the ~180 days that would let two issues of a bimonthly title meet.
_COVER_TO_ONSALE_MAX_DAYS = 120


def _parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _release_dates_compatible(cache_row: dict[str, Any], xlsx_row: dict[str, Any]) -> bool:
    """Do a pending win's release date and an export row's name the same issue?

    Fails OPEN when either side has no date (unchanged from the original year
    compare — a dateless win must still be reconcilable; the *destructive*
    branch's fail-closed complement lives in :func:`_era_confirmed`).

    Accepts on either signal:

    * **Same 4-digit year** — the pre-BUI-548 rule, kept verbatim so nothing
      that reconciled before stops reconciling.
    * **LOCG's date earlier by at most** :data:`_COVER_TO_ONSALE_MAX_DAYS` —
      the cover-vs-on-sale skew crossing a year boundary (BUI-548). Strictly
      one-directional: an export date LATER than the win's is not this skew and
      gets no tolerance at all.
    """
    cache_year = (cache_row.get("release_date") or "")[:4]
    xlsx_year = (xlsx_row.get("release_date") or "")[:4]
    if not cache_year or not xlsx_year:
        return True
    if cache_year == xlsx_year:
        return True

    cache_date = _parse_iso_date(cache_row.get("release_date"))
    xlsx_date = _parse_iso_date(xlsx_row.get("release_date"))
    if cache_date is None or xlsx_date is None:
        return False
    delta = (cache_date - xlsx_date).days
    return 0 <= delta <= _COVER_TO_ONSALE_MAX_DAYS


def _release_dates_compatible_either_way(
    row_a: dict[str, Any], row_b: dict[str, Any]
) -> bool:
    """:func:`_release_dates_compatible` with neither row cast as the win.

    That function's tolerance is one-directional on purpose: it compares a
    pending win (Metron's COVER date, later) against an export row (LOCG's
    ON-SALE date, earlier), and a later export date is not that skew. Its
    callers know which row is which.

    The BUI-554 callers do not. Two ``locg_export`` rows imported under two
    different date conventions are the same book with no win/export roles to
    assign, and the live store holds the drift in both directions, so the
    caller would otherwise get an answer that depended on row order in the
    JSON file. Symmetric here, asymmetric there — the difference is whether the
    caller can name which side is the cover date.
    """
    return _release_dates_compatible(row_a, row_b) or _release_dates_compatible(
        row_b, row_a
    )


def _price_equal(a: Any, b: Any) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return fa > 0 and round(fa, 2) == round(fb, 2)


def _same_copy_corroborated(cache_row: dict[str, Any], xlsx_row: dict[str, Any]) -> str:
    """Independent evidence that two rows describe the same physical copy.

    Returns the name of the corroborating signal, or ``""`` for none — the name
    lands in the ``reconciliation`` audit record, so a later "did this merge two
    different books?" audit can go straight to the widened matches instead of
    re-deriving which ones they were. Used by
    :func:`_reconcile_score` to authorize the *widened* comparisons only
    (BUI-548) — every widening must clear one of these before it fires, which is
    why loosening the volume/publisher/series tests there cannot merge two books
    that merely resemble each other.

    * ``price+date_purchased`` — LOCG only knows what we paid and when because
      THIS pipeline uploaded it on a previous sync. An exact agreement on both
      is a round-trip fingerprint of one purchase, and it was identical across
      every duplicate pair in the 2026-07-27 backlog. Far stronger same-copy
      evidence than ``release_date``, which the providers disagree about by
      construction. A zero/blank price is not evidence (the CSV writes ``0.00``
      for a price-less win and LOCG echoes it back), so it never corroborates.
    * ``release_date`` — an exact match on the very field the two providers
      most often skew. When they nonetheless agree to the day, a whitespace or
      volume-annotation difference in the name is a spelling difference, not a
      different book (the ``Dawn Runner`` / ``Dawnrunner`` class).

    Neither signal can bridge two different ISSUES: :func:`_reconcile_score`
    still demands an exact issue-token match, which is what keeps a lot bought
    in one purchase (identical price AND date across several books — BUI-500)
    from cross-matching within itself.
    """
    if _price_equal(cache_row.get("price_paid"), xlsx_row.get("price_paid")):
        cache_purchased = (cache_row.get("date_purchased") or "")[:10]
        xlsx_purchased = (xlsx_row.get("date_purchased") or "")[:10]
        if cache_purchased and cache_purchased == xlsx_purchased:
            return "price+date_purchased"

    cache_release = (cache_row.get("release_date") or "")[:10]
    xlsx_release = (xlsx_row.get("release_date") or "")[:10]
    if cache_release and cache_release == xlsx_release:
        return "release_date"

    return ""


def _duplicate_check_title_key(full_title: str) -> str:
    """Normalized ``full_title`` for the post-import owned-duplicate check.

    Punctuation-, whitespace- and leading-article-insensitive, because that is
    exactly the drift that produced the duplicate pairs (``Infinity Gauntlet
    #2`` / ``The Infinity Gauntlet #2``, ``Dawn Runner #1`` / ``Dawnrunner
    #1``). Deliberately keeps the ``#N`` token and any trailing edition suffix,
    so a base issue and its ``3rd Printing`` are NOT reported as duplicates of
    each other — they are two books, and flagging them would train the operator
    to ignore this check.
    """
    # _normalize_title already lowercases, folds dashes and strips the leading
    # article on the SPACED form — which is the order that matters, since
    # stripping the article after collapsing whitespace would eat the first
    # three letters of "Theatre #1".
    return re.sub(r"[^0-9a-z#]+", "", _normalize_title(full_title))


def _cross_edition_twin_signal(rows: list[dict[str, Any]]) -> str:
    """Corroboration name for a foreign-licensed-edition twin in ``rows``, or ``""``.

    The BUI-563 shape: two owned rows spelling the SAME issue, whose release
    dates are far enough apart that :func:`_release_dates_compatible_either_way`
    (and so the ``owned_duplicate_identities`` hard stop) cannot see them, and
    that name DIFFERENT publishers. Panini DC Italia's Italian edition trails
    the US original by 147-211 days — an order of magnitude past
    :data:`_COVER_TO_ONSALE_MAX_DAYS`, and always in the same direction.

    A differing publisher is NOT sufficient on its own, which is why this also
    demands :func:`_same_copy_corroborated`. Measured over the 2026-07-28 store,
    the publisher test alone returns 8 titles, two of which are false: ``The
    Transformers (1984 - 1991) #13``/``#14`` (Marvel) against ``Transformers
    (2023 - Present) #13``/``#14`` (Image) are a masthead a different publisher
    picked up four decades later — two genuinely different books, legitimately
    owned side by side. Requiring the round-trip fingerprint drops exactly those
    two and keeps the six generated rows: a shared ``price_paid`` AND
    ``date_purchased`` means LOCG only knows those values because THIS pipeline
    uploaded them, so the pair is one purchase filed twice, not two purchases.
    (``_same_copy_corroborated``'s other signal, an exact ``release_date``
    match, cannot fire here by construction — these pairs are selected for
    having incompatible dates.)
    """
    for i in range(len(rows)):
        for other in rows[i + 1:]:
            left = _normalize_publisher(rows[i].get("publisher_name") or "")
            right = _normalize_publisher(other.get("publisher_name") or "")
            if not (left and right) or left == right:
                continue
            signal = _same_copy_corroborated(rows[i], other)
            if signal:
                return signal
    return ""


def _series_squash(series_name: str) -> str:
    """:func:`_normalize_series_key` with intra-name whitespace removed.

    ``"Dawn Runner"`` and ``"Dawnrunner"`` are the same book spelled two ways —
    but BUI-546 deliberately did NOT widen ``_normalize_series_key`` to strip
    inner whitespace, because that key also drives the ownership matcher, where
    collapsing word boundaries would create false ``in_collection`` verdicts
    (the R11 direction). So the squash lives HERE, in the reconciler, where it
    is only ever consulted alongside :func:`_same_copy_corroborated`.
    """
    return re.sub(r"\s+", "", _normalize_series_key(series_name or ""))


def _reconcile_score(cache_row: dict[str, Any], xlsx_row: dict[str, Any]) -> int:
    """Reconciliation score for Phase 1.

    Returns -1 for hard mismatch (do not reconcile), 0 for no match,
    positive for a match (higher = stronger).

    BUI-548 widens three of these tests, each gated on
    :func:`_same_copy_corroborated` so that no widening can fire on name
    resemblance alone:

    * a **one-sided** ``(Vol. N)`` annotation (two DECLARED volumes that
      disagree stay a hard ``-1`` regardless);
    * a publisher naming disagreement that survives
      :func:`_normalize_publisher` (the imprint case, e.g. Skybound / Image);
    * a series name differing only by intra-word whitespace
      (``Dawn Runner`` / ``Dawnrunner``).

    The issue-token compare is deliberately NOT widened: it is what keeps two
    printings or editions of one issue apart, since LOCG spells them as a
    trailing suffix (``"... #2 3rd Printing"``, ``"... #196 Newsstand
    Edition"``) that the end-anchored token extractor reads as "no token" —
    so a base win scores 0 against them and cannot merge.
    """
    cache_series = cache_row.get("series_name", "") or ""
    xlsx_series = xlsx_row.get("series_name", "") or ""

    vol_conflict, vol_one_sided = _vol_annotation_relation(cache_series, xlsx_series)
    if vol_conflict:
        return -1  # Hard mismatch per R60

    publisher_agrees = _publisher_matches(
        cache_row.get("publisher_name", ""), xlsx_row.get("publisher_name", "")
    )
    series_agrees = _series_normalized_matches(cache_series, xlsx_series)

    if vol_one_sided or not publisher_agrees or not series_agrees:
        # At least one comparison would have to be widened to match these two
        # rows, so demand independent same-copy evidence first. Computed only on
        # this branch: the scorer runs once per (pending row x export row) pair,
        # and the overwhelming majority agree outright.
        if not _same_copy_corroborated(cache_row, xlsx_row):
            # Hard mismatch per R60 when a volume is declared on one side only.
            return -1 if vol_one_sided else 0
        if not series_agrees and _series_squash(cache_series) != _series_squash(xlsx_series):
            # Corroborated, but the names differ by more than inner whitespace.
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

    # Release dates must be compatible if both present (BUI-548: same year, or
    # LOCG's on-sale date earlier within the cover-date skew window)
    if not _release_dates_compatible(cache_row, xlsx_row):
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
# Drift detectors: each holds steady the fields the other lets move
# ---------------------------------------------------------------------------
#
# `make_identity` is (publisher, series, full_title, release_date), and LOCG is
# free to relabel any of them. Each detector below drops exactly ONE of those
# components and holds the rest, so a row whose single volatile field moved is
# still recognized as the row it already is instead of inserting a twin
# (BUI-554). Both use the SAME folded `series_name` the identity key does, so a
# detector can never be defeated by the drift class the fold already absorbs.
#
#   _partial_identity  drops full_title    -> catches a title rename (R67)
#   _title_identity    drops release_date  -> catches a date-convention change
#
# `publisher_name` is deliberately left in BOTH. Dropping it from a detector
# would leave a key of two free-text fields, which is not enough to assert "same
# book" — and BUI-559 went looking for the publisher drift class that would have
# justified paying that price, and found it does not exist.
#
# The pairs it was filed for do NOT share a `release_date`. Measured over the
# 2026-07-28 store, all five of them:
#
#   Absolute Flash #3          DC 2025-05-21  vs  Panini 2025-12-18   (211d)
#   Absolute Green Lantern #3  DC 2025-06-04  vs  Panini 2025-11-06   (155d)
#   Absolute Flash #10         DC 2025-12-17  vs  Panini 2026-06-04   (169d)
#   Absolute Green Lantern #8  DC 2025-11-05  vs  Panini 2026-04-01   (147d)
#   Absolute Green Lantern #9  DC 2025-12-03  vs  Panini 2026-05-07   (155d)
#
# Panini trails DC by 147-211 days, every time, in the same direction. That is
# not one row relabelled; it is Panini DC Italia's Italian licensed edition — a
# genuinely different book with its own LOCG catalog entry — and the offset is
# an order of magnitude past `_COVER_TO_ONSALE_MAX_DAYS`. So a third detector
# gated on identical series + `full_title` + `release_date` with only the
# publisher differing would have matched ZERO rows: that shape occurs nowhere in
# the store, current or in the pre-BUI-556 backup. It was not built.
#
# What these rows are is a duplicate LOCG itself holds, which no import-side
# detector can fix. Every Panini row carries our own `price_paid` +
# `date_purchased` (and the same-generator `Kamite` row carries a
# `gixen_item_id`), so by :func:`_same_copy_corroborated`'s own reasoning LOCG
# knows them only because the record-win round-trip pushed them — and resolved
# them onto the foreign edition's entry instead of the US one. Merging the twin
# here would assert two different catalog entries are one book AND still not
# help: LOCG holds both ownerships, so the twin returns on the next export. The
# fix belongs at the push end, not in this matcher.

def _partial_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """(publisher_name, series_name, release_date) — used to detect full_title renames."""
    return (
        row.get("publisher_name") or "",
        identity_series_key(row.get("series_name") or ""),
        row.get("release_date") or "",
    )


def _title_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """(publisher_name, series_name, full_title) — used to detect date drift.

    The exact complement of :func:`_partial_identity`, and the reason the pair
    is not redundant: a rename detector that shares a field with the rename it
    detects fails in exactly the cases the identity key fails. When the field
    that moved IS ``release_date`` — LOCG switching a run from cover date to
    on-sale date, or rewriting a just-pushed row's date to its own canonical
    value — ``_partial_identity`` moves with it and both guards miss the same
    row. That produced 23 of the live store's 60 duplicate identities.

    This key alone is NOT sufficient to merge on: it is the *candidate lookup*,
    and :func:`_release_dates_compatible` still has to rule on the pair. Same
    publisher + same volume + same ``full_title`` (issue number AND any
    printing/variant suffix, verbatim) narrows the field to one book already;
    the date predicate is what stops it reaching across eras.
    """
    return (
        row.get("publisher_name") or "",
        identity_series_key(row.get("series_name") or ""),
        row.get("full_title") or "",
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

def _post_import_series_index(
    comics: list[dict[str, Any]], xlsx_rows: list[dict[str, Any]]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """The series index as it will exist AFTER this import (BUI-547).

    ``import_xlsx`` rebuilds ``series_name_index`` at the very end, so the
    stored index Phase 1 would otherwise see is the PREVIOUS import's — it
    cannot know about a series arriving in the export being merged right now.
    Reconciling against the stale index is what makes the manual-resolution
    backlog monotonic: an operator adds the missing series to LOCG precisely to
    unstick a flagged win, the import brings it back, and the flag still doesn't
    clear because the series only lands in the index one import too late.

    Both canonical builders already apply the ``source == "locg_export"``
    filter (R61) themselves, so the store's rows are handed over whole rather
    than pre-filtered here — restating that predicate in a second file is how
    it drifts. The incoming export rows are LOCG-sourced by definition and are
    synthesized with only the two fields those builders read.
    """
    index_payload = {
        "comics": [
            *comics,
            *(
                {"source": "locg_export", "series_name": xr.get("series_name")}
                for xr in xlsx_rows
            ),
        ]
    }
    return (
        rebuild_series_name_index(index_payload),
        build_volume_candidates(index_payload),
    )


def _reresolve_manual_series_flags(
    comics: list[dict[str, Any]],
    flagged_indices: list[int],
    identity_to_idx: dict[tuple, int],
    partial_to_idx: dict[tuple, int],
    series_name_index: dict[str, str],
    volume_candidates: dict[str, list[str]],
    audit_records: list[dict[str, Any]],
    summary: dict[str, Any],
    now: str,
) -> None:
    """Re-check stale ``needs_manual_series_canonical`` flags (BUI-547).

    The flag is written once, by ``_build_win_row`` at record-win time, and is
    a snapshot of what the collection knew at that instant. When the collection
    LATER gains the data that would resolve the series — a new LOCG import, a
    manual add, an alias-table change — nothing recomputes it, so the row stays
    excluded from the CSV export and sits in ``pending_push_count`` forever.
    Rows enter the manual bucket and never leave it.

    **One-way on purpose: this only ever CLEARS the flag, never sets it.** A row
    that stops resolving (an import drops a volume, a normalizer change reshapes
    a key) must not be silently re-flagged out of the export — that is a
    different failure with a different blast radius, and the export dropping
    rows without saying so is the direction that loses data.

    Runs BEFORE candidate scoring, and writes the resolved canonical
    ``series_name`` onto the row, so a row that unsticks here can also reconcile
    in the SAME import rather than waiting a cycle: the canonical name is what
    carries LOCG's ``(Vol. N)`` decoration, which ``_reconcile_score``'s volume
    test needs. ``flagged_indices`` is computed from the ORIGINAL flags and
    passed in, so clearing a flag here can never remove a row from this phase's
    own candidate set. Rewriting ``series_name`` changes a row's identity, so
    ``identity_to_idx`` / ``partial_to_idx`` are re-keyed in step.

    Chosen over the alternative BUI-547 floats — deriving the flag at export
    time instead of storing it — because the "computed view" framing does not
    survive contact: the export needs the resolved canonical ``series_name`` in
    the CSV too, so a derive-at-export pass would still have to write, only from
    a read path (``generate_csv`` runs from the server's export endpoint with no
    cache lock held). It would also recompute in BOTH directions on every
    export, which is exactly the silent re-flagging this ticket forbids.

    **BUI-586:** the plain lookup above is publisher-BLIND — the pool it draws
    on (``volume_candidates``) holds every volume LOCG's export carries under a
    key, including the foreign licensed editions our own record-win push put
    there (BUI-564). This mirrors BUI-564's trigger-and-rescope, minus the
    Metron fetch: the row's own ``publisher_name`` is already on hand, so no
    network call is needed. ``series_publishers`` is built once from ``comics``
    (the STORE's rows, passed in whole) rather than from the ``index_payload``
    ``_post_import_series_index`` synthesizes — that payload carries only
    ``source``/``series_name`` for xlsx rows and is not a publisher source.
    Fail open throughout: a re-resolution is only ever ATTEMPTED when
    ``series_publisher_conflicts`` shows a demonstrated disagreement between
    the row's publisher and the unscoped answer, and a rescope that returns
    ``None`` (empty scoped pool, or genuinely unresolvable) keeps the unscoped
    answer rather than blanking it.
    """
    # BUI-586: same population `build_volume_candidates` draws its volume
    # names from (`comics` is the store's rows, handed over whole) — see the
    # docstring above for why this is NOT built from `index_payload`.
    series_publishers = build_series_publishers({"comics": comics})

    for ci in flagged_indices:
        row = comics[ci]
        if not row.get("needs_manual_series_canonical"):
            continue
        if not _is_pending_push_row(row):
            continue

        series_name = row.get("series_name") or ""
        if not series_name:
            continue

        norm_key = _normalize_series_key(series_name)
        issue_token = _issue_token(row.get("full_title") or "")
        release_year = _release_year(row) or None

        resolved = resolve_series_for_win(
            norm_key,
            issue_token,
            release_year,
            series_name_index,
            volume_candidates,
        )

        # BUI-586: trigger a publisher-scoped rescope only on a DEMONSTRATED
        # conflict between the row's own publisher and the publisher(s) known
        # for the volume the unscoped lookup just picked. A None `resolved`
        # (nothing to rescope) or a missing/unknown publisher both leave
        # `resolved` exactly as the unscoped lookup produced it.
        publisher_name = row.get("publisher_name") or None
        if (
            resolved
            and publisher_name
            and series_publishers
            and series_publisher_conflicts(resolved, publisher_name, series_publishers)
        ):
            rescoped = resolve_series_for_win(
                norm_key,
                issue_token,
                release_year,
                series_name_index,
                publisher_scoped_volume_candidates(
                    volume_candidates, series_publishers, publisher_name
                ),
            )
            # A rescope that comes back None (empty scoped pool, or otherwise
            # unresolvable) keeps the unscoped `resolved` — never blanks or
            # downgrades an existing answer.
            if rescoped:
                resolved = rescoped

        if not resolved or resolved == series_name:
            continue

        # `series_name` is part of BOTH index keys, so re-key before anything
        # reads them again — the scoring loop's collision guard and all of
        # Phase 2 look rows up by identity. Leaving a stale entry behind would
        # let a later export row claim this index slot as a rename target and
        # apply its own columns to a row it has nothing to do with (the same
        # hazard BUI-462 fixed for the auto-heal drop).
        old_identity = make_identity(row)
        old_partial = _partial_identity(row)

        row["needs_manual_series_canonical"] = False
        row["series_name"] = resolved

        if identity_to_idx.get(old_identity) == ci:
            del identity_to_idx[old_identity]
        if partial_to_idx.get(old_partial) == ci:
            del partial_to_idx[old_partial]
        identity_to_idx[make_identity(row)] = ci
        partial_to_idx[_partial_identity(row)] = ci

        summary["manual_series_flags_cleared"] += 1
        audit_records.append({
            "type": "manual_series_flag_cleared",
            "ts": now,
            "command": "import",
            "details": {
                "full_title": row.get("full_title"),
                "previous_series_name": series_name,
                "resolved_series_name": resolved,
            },
        })


def _reconcile_phase(
    comics: list[dict[str, Any]],
    xlsx_rows: list[dict[str, Any]],
    identity_to_idx: dict[tuple, int],
    partial_to_idx: dict[tuple, int],
    healed_drop: dict[int, int],
    second_copy_credits: dict[int, int],
    audit_records: list[dict[str, Any]],
    summary: dict[str, Any],
    now: str,
) -> None:
    """Phase 1 of import_xlsx's do_merge: reconcile flagged/pending agent_win
    rows against the incoming export via the relaxed heuristic
    (_reconcile_score), rewriting identity in place and clearing manual flags.
    See import_xlsx's docstring for the two-phase pipeline this implements.

    Mutates `comics` row dicts, `identity_to_idx` / `partial_to_idx`,
    `healed_drop`, `second_copy_credits`, `audit_records`, and `summary` in
    place (same containers do_merge already builds/holds) — no return value,
    matching do_merge's existing mutate-in-place style. Must run before
    _standard_merge_phase, which relies on the identity rewrites and index
    updates made here.

    `second_copy_credits` (BUI-470): {kept row index -> copies to add to
    in_collection}, keyed by the SURVIVING row's index (never a dropped
    index — mirrors `healed_drop`'s values, not its keys). do_merge must
    apply these to `comics` AFTER _standard_merge_phase (Phase 2), which
    overwrites the survivor's in_collection wholesale from the export row —
    applying any earlier would be silently clobbered.

    Ordering within the phase (BUI-547 / BUI-548 meet here):
    ``flagged_indices`` is computed first, from the flags as they stand on
    entry; then :func:`_reresolve_manual_series_flags` clears the stale ones and
    writes each row's resolved canonical series name; then candidate scoring
    runs. That order is deliberate in both directions — computing the index set
    first means clearing a flag can never drop a row out of this phase, and
    re-resolving before scoring means a row that unsticks can reconcile in the
    same import instead of exporting one more duplicate first.
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

    # BUI-547: unstick rows whose series became resolvable since record-win
    # stamped them. Runs against the index as it will exist AFTER this import,
    # so a series arriving in THIS export counts.
    series_name_index, volume_candidates = _post_import_series_index(comics, xlsx_rows)
    _reresolve_manual_series_flags(
        comics,
        flagged_indices,
        identity_to_idx,
        partial_to_idx,
        series_name_index,
        volume_candidates,
        audit_records,
        summary,
        now,
    )

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
            target_established = _is_established_row(target_row)
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
                # BUI-462/BUI-470: same-book must be PROVED before deleting
                # anything — see _same_book_confirmed. Without it a dateless
                # win can fuzzy-match a different volume of the same masthead,
                # or a base win can fuzzy-match a distinct print/variant
                # edition record-win itself would never have deduped, and
                # either gets dropped as if it were a duplicate.
                confirmed, reason, detail = _same_book_confirmed(cache_row, xlsx_row)
                if not confirmed:
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
                # BUI-470: `in_collection` is a copy COUNT, not a flag — a
                # pending agent_win row is always created with in_collection=1
                # (one gixen win == one physical copy), so a heal that drops
                # it without crediting the survivor silently loses a genuine
                # second copy or condition upgrade whenever the target's own
                # ownership is independent of THIS win. The first fold onto a
                # target that was NOT already owned pre-import is the
                # ownership transition itself (a wish becoming owned via this
                # exact win, or a previously-unrecorded win becoming the
                # export's first reflection of it) and must not be
                # double-counted on top of the export's own in_collection;
                # every fold beyond that — including any fold onto a target
                # that WAS already owned before this win existed — is
                # evidence of a distinct physical copy. `target_row` is never
                # itself mutated during Phase 1, so re-checking `_is_owned`
                # here always reads the same pre-import value regardless of
                # how many prior wins already folded into this same `collide`
                # target this phase.
                if _is_owned(target_row) or collide in second_copy_credits:
                    second_copy_credits[collide] = second_copy_credits.get(collide, 0) + 1
                else:
                    second_copy_credits.setdefault(collide, 0)
                audit_records.append({
                    "type": "auto_healed_duplicate_win",
                    "ts": now,
                    "command": "import",
                    "details": {
                        "full_title": cache_row.get("full_title"),
                        "kept_identity": list(target_identity),
                        "dropped_identity": list(make_identity(cache_row)),
                        "gixen_item_id": cache_row.get("gixen_item_id"),
                        "second_copy_credited": second_copy_credits[collide] > 0,
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
        # BUI-548: which signal (if any) authorized a widened match, recorded
        # before the identity rewrite destroys the evidence. A reconcile that
        # fired on an exact agreement carries "", so an audit of "did this
        # merge two different books?" can go straight to the widened ones.
        corroboration = _same_copy_corroborated(cache_row, xlsx_row)

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
                "corroboration": corroboration,
            },
        })


def _apply_export_row(
    existing: dict[str, Any],
    xlsx_row: dict[str, Any],
    now: str,
    audit_records: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    """Apply a matched export row onto its cache row, however it was matched.

    All three of Phase 2's update paths — exact identity, ``full_title`` rename
    (R67), and release-date drift (BUI-554) — end the same way: copy LOCG's
    columns across (holding an ownership downgrade, BUI-124), stamp the
    sighting, and report as ``behavioral_drift`` any user-managed column LOCG
    just overwrote. They used to spell that out separately, and the copies had
    already drifted apart in both directions:

    * the rename path compared user columns AFTER the copy, where every one of
      them necessarily equals the export's — so its ``changed`` list was always
      empty and the audit record it meant to write was never written;
    * the date-drift path had no drift check at all, so a hand-edited
      ``condition`` or ``notes`` could be overwritten with nothing recorded.

    One implementation, so all three paths audit identically and a fourth
    cannot be added that quietly doesn't.
    """
    pre_user_values = {col: existing.get(col) for col in USER_MANAGED_COLUMNS}
    pre_checksum = _user_column_checksum(existing)

    _apply_locg_columns_held(existing, xlsx_row, now, audit_records, summary)
    existing["last_seen_in_export_at"] = now
    existing["source"] = "locg_export"
    if existing.get("pushed_to_locg_at") is None:
        existing["pushed_to_locg_at"] = now

    if _user_column_checksum(existing) == pre_checksum:
        return
    # Compare each user column's PRE value against what the export carried.
    # Reading `existing` here instead is what made the rename path's check
    # vacuous: the copy above has already made them equal by construction.
    changed = [
        col for col in USER_MANAGED_COLUMNS
        if pre_user_values.get(col) != xlsx_row.get(col)
    ]
    if not changed:
        return
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


def _pick_date_drift_match(
    xlsx_row: dict[str, Any],
    comics: list[dict[str, Any]],
    title_to_indices: dict[tuple, list[int]],
    claimed: set[int],
    pre_import_count: int,
) -> int | None:
    """Index of the pre-import row ``xlsx_row`` is a date-drifted twin of (BUI-554).

    Two gates, both required: the row must share ``_title_identity`` (same
    publisher, same volume, same ``full_title`` verbatim — issue number and any
    printing/variant suffix included), and the pair must clear
    :func:`_release_dates_compatible_either_way`. The title key alone would
    sweep the three volumes of ``X-Men #17`` into one book; the date predicate
    is what refuses that, and it is the reconciler's OWN predicate, so "the
    matcher should have caught this" and "this is a duplicate" stay one
    judgment.

    Candidates already ``claimed`` this phase are skipped — an export row must
    never be folded into a store row another export row has spoken for, which
    would silently drop a book LOCG says exists. Post-import inserts (index
    ``>= pre_import_count``) are skipped for the same reason rename detection
    skips them: a row this import just created cannot be a pre-existing twin.

    **Only ESTABLISHED rows are eligible** (``locg_export``, or an
    ``agent_win`` already pushed) — the same predicate Phase 1's collision
    guard uses. A *pending* win is Phase 1's business, and Phase 1 judges it
    with gates this one does not have: era evidence, print/variant parity, and
    an ambiguity check that deliberately leaves a win pending when two export
    rows both plausibly match it. Letting this pass claim such a row would
    silently overturn that decision with a weaker test. The duplicate class
    BUI-554 exists for is export↔export; it has no business adjudicating wins.

    When several candidates qualify the closest date wins, ties broken by the
    lower index, so the choice never depends on dict iteration order. Several
    candidates means the store ALREADY holds duplicates of this title; merging
    into one of them is still strictly better than adding a third, and BUI-556
    is what resolves the pre-existing pile.
    """
    candidates = title_to_indices.get(_title_identity(xlsx_row))
    if not candidates:
        return None

    xlsx_date = _parse_iso_date(xlsx_row.get("release_date"))
    best: tuple[int, int] | None = None
    best_idx: int | None = None
    for idx in candidates:
        if idx >= pre_import_count or idx in claimed:
            continue
        existing = comics[idx]
        if not _is_established_row(existing):
            continue
        if not _release_dates_compatible_either_way(existing, xlsx_row):
            continue
        existing_date = _parse_iso_date(existing.get("release_date"))
        # A missing date on either side sorts last: it only reached here via
        # the predicate's fail-open branch, so it is the weakest evidence in
        # the set, not the strongest.
        delta = (
            abs((existing_date - xlsx_date).days)
            if existing_date is not None and xlsx_date is not None
            else 10**6
        )
        rank = (delta, idx)
        if best is None or rank < best:
            best = rank
            best_idx = idx
    return best_idx


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

    Runs in two passes (BUI-554). Pass A handles every export row whose
    identity matches exactly; pass B handles the rest, trying — in order —
    `full_title` rename detection, then release-date drift detection, then a
    genuine insert. Exact matches drain first because both inexact paths CLAIM
    a store row, and a claim is only safe once no export row can still match
    that row exactly.

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

    # BUI-554: two passes, exact matches first. The inexact paths below (rename
    # detection, date-drift detection) both CLAIM a pre-import row, and a claim
    # is only safe once every row an export row matches EXACTLY is off the
    # table — otherwise which row wins depends on the order LOCG happened to
    # emit its spreadsheet. Draining the exact matches first also completes what
    # the `partial_to_idx` retraction below was always reaching for: it used to
    # protect a row only from *later* xlsx rows, so an earlier inexact row could
    # still claim a row a later export row would have matched exactly.
    exact_pairs: list[tuple[dict[str, Any], int]] = []
    inexact_rows: list[dict[str, Any]] = []
    for xr in xlsx_rows:
        row_identity = make_identity(xr)
        xlsx_identities.add(row_identity)
        ci = identity_to_idx.get(row_identity)
        if ci is None:
            inexact_rows.append(xr)
        else:
            exact_pairs.append((xr, ci))

    # Rows an exact match has already spoken for. The inexact passes must never
    # merge into one of these: the exact row and the inexact row are two
    # DIFFERENT rows in LOCG's export, so folding the second into the first
    # would drop a book LOCG says exists (the R11 direction).
    claimed: set[int] = {ci for _xr, ci in exact_pairs}

    # ----- Pass A: exact identity ------------------------------------------
    for xr, ci in exact_pairs:
        existing = comics[ci]

        # Remove from partial_to_idx so it won't be found as a rename
        # candidate by a later xlsx row with the same partial identity
        old_partial = _partial_identity(existing)
        if partial_to_idx.get(old_partial) == ci:
            del partial_to_idx[old_partial]

        _apply_export_row(existing, xr, now, audit_records, summary)
        summary["updated"] += 1

    # ----- Pass B: rename, then date drift, then insert ---------------------
    # Candidate index for the date-drift detector, projected from the LIVE
    # identity index rather than rebuilt from `comics`. That projection is what
    # makes it correct by construction: Phase 1 retracts the index entries of
    # every row it reconciled or auto-healed away, so a row the reconciler has
    # already disowned can never be offered here as a merge target — a bug that
    # a fresh scan of `comics` (which still physically holds the healed rows
    # until do_merge filters them) would have reintroduced.
    title_to_indices: dict[tuple, list[int]] = {}
    for _identity, idx in identity_to_idx.items():
        title_to_indices.setdefault(_title_identity(comics[idx]), []).append(idx)

    for xr in inexact_rows:
        # Check for rename: same (publisher, series, release_date), different
        # full_title (R67) — only against pre-import rows, never new inserts
        row_partial = _partial_identity(xr)
        if row_partial in partial_to_idx:
            ci = partial_to_idx[row_partial]
            if ci < pre_import_count and ci not in claimed:
                existing = comics[ci]
                old_title = existing.get("full_title") or ""
                new_title = xr.get("full_title") or ""
                if old_title and new_title and old_title != new_title:
                    old_identity = make_identity(existing)
                    existing["previous_full_title"] = old_title

                    _apply_export_row(existing, xr, now, audit_records, summary)
                    new_identity = make_identity(existing)

                    if old_identity in identity_to_idx:
                        del identity_to_idx[old_identity]
                    identity_to_idx[new_identity] = ci
                    claimed.add(ci)
                    # Consume the partial slot so it won't match again
                    del partial_to_idx[row_partial]

                    audit_records.append({
                        "type": "renamed_full_title",
                        "ts": now,
                        "command": "import",
                        "details": {
                            "old_title": old_title,
                            "new_title": new_title,
                            "identity": list(new_identity),
                        },
                    })
                    summary["updated"] += 1
                    continue

        # BUI-554: same book, new date convention. LOCG rewrites release dates
        # on its own — a run re-catalogued from cover date to on-sale date, or
        # a just-pushed row snapped to LOCG's canonical value — and both the
        # identity key and the rename detector above carry `release_date`, so
        # both miss together and the row inserts as a twin. 23 of the live
        # store's 60 duplicate identities are this. Runs LAST of the match
        # paths: it only ever fires where the code would otherwise have
        # inserted.
        drift_ci = _pick_date_drift_match(
            xr, comics, title_to_indices, claimed, pre_import_count
        )
        if drift_ci is not None:
            existing = comics[drift_ci]
            old_identity = make_identity(existing)
            old_partial = _partial_identity(existing)
            old_release_date = existing.get("release_date")

            _apply_export_row(existing, xr, now, audit_records, summary)
            new_identity = make_identity(existing)

            # Re-key: `release_date` is a LOCG column, so it has just been
            # overwritten and the row no longer lives under `old_identity`.
            # The partial slot is retired without a replacement, matching the
            # exact-match pass — a row this phase has already merged into must
            # not stay eligible as a rename target.
            if identity_to_idx.get(old_identity) == drift_ci:
                del identity_to_idx[old_identity]
            identity_to_idx[new_identity] = drift_ci
            if partial_to_idx.get(old_partial) == drift_ci:
                del partial_to_idx[old_partial]
            claimed.add(drift_ci)

            summary["updated"] += 1
            summary["release_date_drift_merged"] += 1
            audit_records.append({
                "type": "release_date_drift_merged",
                "ts": now,
                "command": "import",
                "details": {
                    "full_title": existing.get("full_title"),
                    "old_release_date": old_release_date,
                    "new_release_date": existing.get("release_date"),
                    "identity": list(new_identity),
                },
            })
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
    auto_healed_duplicates, second_copies_credited, null_release_date_owned,
    manual_series_flags_cleared, owned_duplicate_identities,
    release_date_drift_merged, warnings}.

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
        # gates the drop on confirmed same-book evidence (_same_book_confirmed,
        # BUI-470 — era AND print/variant edition, unified with record-win's
        # own dedup test).
        "auto_healed_duplicates": 0,
        # BUI-470: of the auto-healed duplicates above, how many were credited
        # as a genuine extra physical copy — in_collection incremented on the
        # surviving row rather than silently dropped along with the win.
        "second_copies_credited": 0,
        # BUI-412: owned rows with no release_date, post-import. Non-blocking —
        # a data-quality count only, never used to reject/alter/drop a row.
        "null_release_date_owned": 0,
        # BUI-547: pending rows whose stale needs_manual_series_canonical flag
        # was re-checked and cleared because the series resolves now. One-way —
        # this pass never sets the flag.
        "manual_series_flags_cleared": 0,
        # BUI-548: books left owned TWICE post-import — two owned rows claiming
        # the same title on date-compatible release dates. The semantic
        # duplicate check the sync's row-count arithmetic structurally cannot
        # make. Reported only; never alters or drops a row. BUI-554 dropped the
        # win-vs-export partition that made this read a vacuous 0 once every
        # win had round-tripped back as an export row.
        "owned_duplicate_identities": 0,
        # BUI-554: export rows merged into an existing row whose release_date
        # LOCG had rewritten (cover date vs on-sale date), instead of inserting
        # a duplicate. Each one is a row NOT added, so the sync's
        # `ROWS_BEFORE + added - auto_healed_duplicates` arithmetic still
        # balances exactly — `added` is simply lower.
        "release_date_drift_merged": 0,
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
        # BUI-470: {kept row index -> copies to credit onto in_collection} —
        # see _reconcile_phase's docstring for why this is keyed by survivor,
        # not by dropped index, and why it must be applied after Phase 2.
        second_copy_credits: dict[int, int] = {}

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
            second_copy_credits,
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
            # BUI-470: credit each survivor's in_collection for the genuine
            # extra copies folded into it — for the SAME reason as the
            # provenance carry above, this must run after Phase 2, which has
            # just overwritten in_collection wholesale from the export row
            # (_apply_locg_columns_held); crediting any earlier would be
            # silently clobbered right back down.
            credited_copies = 0
            for kept_idx, credit in second_copy_credits.items():
                if credit <= 0:
                    continue
                row = comics[kept_idx]
                before = _coerce_count_cell(row.get("in_collection"))
                row["in_collection"] = before + credit
                credited_copies += credit
                audit_records.append({
                    "type": "second_copy_credited",
                    "ts": now,
                    "command": "import",
                    "details": {
                        "identity": list(make_identity(row)),
                        "full_title": row.get("full_title"),
                        "in_collection_before": before,
                        "in_collection_after": row["in_collection"],
                        "credited": credit,
                    },
                })
            summary["second_copies_credited"] += credited_copies
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
            if credited_copies:
                summary["warnings"].append(
                    f"{credited_copies} of those healed win(s) were credited as "
                    "genuine extra copies (BUI-470) — in_collection was "
                    "incremented on the kept row rather than silently dropped; "
                    "see import-history.jsonl (type=second_copy_credited)."
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

        # ----- Semantic duplicate check: owned twins (BUI-548, BUI-554) --------
        # The sync's row-count arithmetic (ROWS_BEFORE + added -
        # auto_healed_duplicates) is exact and cannot see this: on 2026-07-27 it
        # balanced to the row while the import silently created a SECOND owned
        # row for 28 books. Counted, named, and surfaced so the sync can
        # hard-stop on it rather than report clean. Non-destructive: this only
        # reports.
        #
        # BUI-554 removed the win-vs-export partition this used to require. It
        # grouped owned rows by `source` and reported a title only when an
        # `agent_win` and a `locg_export` row COLLIDED — which encoded the
        # failure the author was chasing (a reconcile miss on a pushed win), not
        # what makes two rows a violation. A [[Collection Sync]] later
        # round-tripped every pending win back through LOCG as an export row,
        # draining the `agent_win` partition to zero, and the cross-product
        # silently started iterating nothing: the check reported 0 while 60
        # identities collided, and the healthy reading and the blind reading
        # were the same number. All-pairs now, with the predicate deciding.
        owned_groups: dict[str, list[dict[str, Any]]] = {}
        for row in comics:
            if not _is_owned(row):
                continue
            title = _duplicate_check_title_key(row.get("full_title") or "")
            if not title:
                continue
            owned_groups.setdefault(title, []).append(row)
        # Require date compatibility on the pair, not just a shared title: two
        # VOLUMES of one masthead spell the same issue identically (an
        # `X-Men #128` from 2002 and a `The X-Men #128` from 1979 are two books
        # legitimately owned side by side), and a check that cried duplicate on
        # those would be trained away within a sync or two. Uses the reconciler's
        # own predicate, so "the matcher should have caught this" and "this is a
        # duplicate" stay the same judgment.
        owned_duplicates = sorted(
            title
            for title, rows in owned_groups.items()
            if len(rows) > 1
            and any(
                _release_dates_compatible_either_way(rows[i], other)
                for i in range(len(rows))
                for other in rows[i + 1:]
            )
        )
        summary["owned_duplicate_identities"] = len(owned_duplicates)
        if owned_duplicates:
            shown = ", ".join(owned_duplicates[:10])
            more = "" if len(owned_duplicates) <= 10 else f" (+{len(owned_duplicates) - 10} more)"
            summary["warnings"].append(
                f"{len(owned_duplicates)} book(s) are now owned TWICE — two "
                "owned rows claim the same title and the reconciler's own date "
                f"predicate says they are the same book (BUI-548/BUI-554): "
                f"{shown}{more}. The row-count arithmetic cannot see this; "
                "treat it as a failed reconcile, not a clean sync."
            )
        elif not owned_groups:
            # Liveness assertion (BUI-554). A check that has lost the ability to
            # fail is itself news, and its 0 is indistinguishable from a clean
            # one — which is exactly how the pre-BUI-554 counter certified a
            # store holding 60 collisions. Say so rather than let a vacuous 0
            # satisfy the sync's `owned_duplicate_identities == 0` hard-stop.
            summary["warnings"].append(
                "owned_duplicate_identities is VACUOUS: no owned row with a "
                "usable full_title survived this import, so the duplicate check "
                "had nothing to compare and its 0 means 'unable to check', not "
                "'clean' (BUI-554). Verify the import actually landed before "
                "trusting any post-import counter."
            )

        # ----- Cross-edition owned twins: ADVISORY, never a hard stop (BUI-563)
        # The same "owned twice" fact as above, for the pairs the date predicate
        # structurally cannot see: a foreign licensed edition trails the US
        # original by 147-211 days, so `_release_dates_compatible_either_way`
        # rejects every one of them and the hard-stop count reports 0 while six
        # books are owned twice right now.
        #
        # Reported SEPARATELY and deliberately NOT folded into
        # `owned_duplicate_identities`, because widening that counter would make
        # `/comic:collection-sync` (which asserts it is 0) refuse to run — and
        # unlike a failed reconcile, this is not fixable from here. LOCG holds
        # both ownerships, so deleting the local row does not stick (it returns
        # on the next export) and clearing the ownership deliberately runs the
        # BUI-122 `In Collection=0` data-loss path. A hard stop over a condition
        # the operator has no local remedy for would simply block every sync
        # indefinitely; the generator is fixed upstream instead, in record-win
        # (BUI-564). The two lists are kept disjoint — a title already reported
        # as a hard-stop duplicate is not repeated here — so the counts can be
        # read independently.
        already_hard_stopped = set(owned_duplicates)
        cross_edition_twins = sorted(
            title
            for title, rows in owned_groups.items()
            if len(rows) > 1
            and title not in already_hard_stopped
            and _cross_edition_twin_signal(rows)
        )
        summary["owned_duplicate_identities_cross_edition"] = len(cross_edition_twins)
        if cross_edition_twins:
            shown = ", ".join(cross_edition_twins[:10])
            more = (
                ""
                if len(cross_edition_twins) <= 10
                else f" (+{len(cross_edition_twins) - 10} more)"
            )
            summary["warnings"].append(
                f"ADVISORY (not a sync blocker): {len(cross_edition_twins)} book(s) "
                "are owned TWICE across editions — a foreign licensed edition "
                "carrying the same price_paid + date_purchased as its US twin, "
                "which means our own record-win push created it (BUI-563/BUI-564): "
                f"{shown}{more}. The release dates are months apart, so the "
                "owned_duplicate_identities hard stop cannot see these. Do NOT "
                "fix by deleting the local row — LOCG re-emits it, and clearing "
                "the ownership runs the BUI-122 In Collection=0 data-loss path."
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

def _is_pending_push_row(row: dict[str, Any]) -> bool:
    """True when a row is pending push to LOCG: pushed_to_locg_at IS NULL OR
    local_added_at > pushed_to_locg_at (a re-pend after an earlier push).

    The single definition of "pending" — shared by :func:`_pending_push_rows`
    (the export's row set) and, since BUI-471,
    ``locg.commands._is_backfill_target`` (the backfill's target set). The two
    used to diverge (backfill required the stricter ``pushed_to_locg_at IS
    NULL``), so a row re-pended after a push was exported but silently never
    remediated. Sharing this predicate makes the two provably match.
    """
    pushed = row.get("pushed_to_locg_at")
    added = row.get("local_added_at") or ""
    return pushed is None or bool(added and added > pushed)


def _pending_push_rows(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition pending-push rows into (ready, manual_variant, manual_series_canonical).

    Pending: :func:`_is_pending_push_row`.
    Ready: pending AND not flagged.
    Manual: pending AND flagged (excluded from CSV).
    """
    ready: list[dict[str, Any]] = []
    manual_variant: list[dict[str, Any]] = []
    manual_series: list[dict[str, Any]] = []

    for row in payload.get("comics", []):
        if not _is_pending_push_row(row):
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
